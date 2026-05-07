#!/usr/bin/env bash
# review_conflict_resolve.sh — run Codex resolver, validate, stage, and
# create the [ai-merge-resolve] commit for review_autofix.yml.
#
# Extracted from the "Run Codex resolver, validate, stage, commit" step of
# review_autofix.yml to keep the `run:` block under GitHub Actions' 21,000-
# char template-expression limit. Consumes artefacts produced by
# review_conflict_prepare.sh and shares that step's short-circuit conditions.
#
# Inputs (environment):
#   RUNTIME_DIR                     Ephemeral per-run directory.
#   CONFLICT_RESOLVER_PROMPT_FILE   Rendered resolver prompt.
#   CONFLICT_RESOLVER_SUMMARY_FILE  Path resolver summary is written to.
#   CONFLICT_RESOLVER_REASONING_EFFORT  Reasoning level applied to ~/.codex/config.toml
#                                   before the retry loop. One of xhigh|high|medium|none;
#                                   defaults to medium. Decoupled from EDITOR_REASONING_EFFORT
#                                   so the smoke-test override (which sets editor reasoning
#                                   to "none") doesn't starve the resolver.
#   MODEL_EDITOR                    Codex model id used for resolution.
#   IS_WORKFLOW_SOURCE_REPO         "true" on the coding-workflows repo itself.
#   SUPPORT_SCRIPTS_DIR             Path to check_resolver_diff.sh / verify_integration_fingerprints.py.
#   IS_INTEGRATION_SYNC             "true" when acting on an orchestrator integration branch.
#   INTEGRATION_FINGERPRINTS_FILE   Fingerprints payload written by the prepare step.
#   INTEGRATION_BRANCH_NAME / TARGET_BRANCH  Branch identifiers used by the verifier.
#   GH_PAT                          GitHub token used to rewrite the origin remote URL.
#   GITHUB_REPOSITORY               owner/repo slug (auto-set).
#
# Outputs:
#   $GITHUB_ENV: CONFLICT_RESOLVED.
#   Creates a single [ai-merge-resolve] commit on success (push deferred).
#
# Failure modes:
#   - Exits 1 on resolver retry exhaustion, allowlist violation,
#     check_resolver_diff.sh failure, or integration-fingerprint hard violation.
#   - Exits 0 with CONFLICT_RESOLVED=false when no changes remain to commit.

set -euo pipefail

# Re-derive runtime paths set in the prepare step. Shell variables
# do not cross step boundaries; RUNTIME_DIR itself is in
# $GITHUB_ENV so it survives, and both paths are deterministic
# functions of RUNTIME_DIR.
PRE_RESOLVER_STATE_FILE="${RUNTIME_DIR}/pre_resolver_state.tsv"
CONFLICTED_PATHS_FILE="${RUNTIME_DIR}/conflicted_paths.txt"
RESOLVER_ALLOWLIST_FILE="${RUNTIME_DIR}/resolver_unmerged_allowlist.txt"

# ----------------------------------------------------------------------
# Immediate orchestrator-poll dispatch on resolver bail (Q3: A).
#
# When the resolver path cannot deliver a clean [ai-merge-resolve]
# commit (loop exhaustion, no-progress detection, allowlist guard
# failure, check_resolver_diff rejection), the integration-sync
# unattended escalation is the orchestrator integration judge,
# which lives inside scripts/orchestrate_poll_process.sh and is
# invoked on each */5 cron tick of internal-orchestrate-poll.yml.
# Cron lag is up to 5 min; for an integration branch already
# blocked on a contradictory merge, that is 5 min of wasted human
# attention if alerts fired.  Fire a workflow_dispatch immediately
# instead so the judge picks up on the next available scheduling
# slot.
#
# Concurrency: orchestrate_poll.yml has
# `concurrency.group: ai-orchestrate-poll-${{ github.repository }}`
# with `cancel-in-progress: false`, so a dispatched run queues
# behind any active cron-tick run rather than racing it.  To avoid
# stacking dispatches when several PRs bail at once, dedup against
# already in_progress / queued poller runs before firing.
#
# Fail-open: any failure in the dispatch path (missing GH_PAT,
# rate limit, unknown workflow name on a consumer repo) is a
# warning, not a hard failure — the cron tick remains the safety
# net so unattendedness is preserved either way.
#
# Gate on IS_INTEGRATION_SYNC=true: the integration judge only
# applies to orchestrator/project-* branches.  Non-integration
# resolver failures continue along the existing path
# (synchronize-event re-trigger of the review pipeline).
_RESOLVER_DISPATCH_FIRED=0
_dispatch_integration_judge_now() {
  [ "${_RESOLVER_DISPATCH_FIRED}" -eq 1 ] && return 0
  _RESOLVER_DISPATCH_FIRED=1

  if [ "${IS_INTEGRATION_SYNC:-false}" != "true" ]; then
    return 0
  fi
  if [ -z "${GH_PAT:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
    echo "::warning::Skipping immediate orchestrator-poll dispatch: GH_PAT or GITHUB_REPOSITORY unset. Cron tick will pick up the integration-sync stall within 5 min."
    return 0
  fi

  local _poll_workflow="${ORCHESTRATE_POLL_WORKFLOW_FILE:-internal-orchestrate-poll.yml}"

  # Dedup: the poller's per-repo concurrency group already
  # serialises runs, but firing while one is queued/active just
  # adds to the queue.  Skip when an existing run will pick up
  # the new state on the next slot.  Uses gh's built-in --jq
  # for consistency with the rest of the codebase (no embedded
  # python3 -c snippet) and so a non-JSON / empty response from
  # gh falls open via the trailing `|| echo 0` rather than
  # silently miscounting.
  local _active_count
  _active_count="$(GH_TOKEN="${GH_PAT}" gh run list \
      --workflow="${_poll_workflow}" \
      --repo "${GITHUB_REPOSITORY}" \
      --status in_progress \
      --limit 1 \
      --json databaseId \
      --jq 'length' 2>/dev/null || echo 0)"
  if [ "${_active_count}" != "0" ]; then
    echo "Skipping immediate orchestrator-poll dispatch: a poller run is already in_progress (will scan integration-sync state on the next concurrency slot)."
    return 0
  fi
  _active_count="$(GH_TOKEN="${GH_PAT}" gh run list \
      --workflow="${_poll_workflow}" \
      --repo "${GITHUB_REPOSITORY}" \
      --status queued \
      --limit 1 \
      --json databaseId \
      --jq 'length' 2>/dev/null || echo 0)"
  if [ "${_active_count}" != "0" ]; then
    echo "Skipping immediate orchestrator-poll dispatch: a poller run is already queued (will scan integration-sync state when it starts)."
    return 0
  fi

  if GH_TOKEN="${GH_PAT}" gh workflow run "${_poll_workflow}" \
       --repo "${GITHUB_REPOSITORY}" 2>/dev/null; then
    echo "Dispatched ${_poll_workflow} on ${GITHUB_REPOSITORY} to fast-track integration-judge escalation (resolver could not produce a clean commit)."
  else
    echo "::warning::Failed to dispatch ${_poll_workflow}; cron tick will pick up the integration-sync stall within 5 min."
  fi
}

# EXIT trap — fires the dispatch on any non-zero exit from this
# script (resolver loop exhaustion, no-progress, allowlist guard,
# check_resolver_diff failure).  Idempotent (function early-returns
# after first call).  The exit-0 paths (no staged changes after
# resolver) intentionally bypass the dispatch — the resolver
# succeeded in deciding no commit was needed.
_resolver_exit_trap() {
  local _rc=$?
  if [ "${_rc}" -ne 0 ]; then
    _dispatch_integration_judge_now || true
  fi
  return "${_rc}"
}
trap _resolver_exit_trap EXIT

# ----------------------------------------------------------------------
# Retry-loop hardening (fix for the recurring integration-sync
# "fingerprint verification FAILED" class of run failures).
#
# Previously the retry loop only retried when `codex exec` itself
# failed or produced empty output — both rare.  Real quality gates
# (residual conflict markers, fingerprint regressions) were evaluated
# post-loop, so a bad-but-well-formed model output terminated the
# run with no retry consumed on the actual failure mode.  Large
# integration PRs hit this reliably.
#
# The restructured loop below runs the soft quality gates (marker
# pre-scan + fingerprint verify) INSIDE the retry loop.  On a soft
# failure the working tree is restored to the post-merge-replay
# state captured here, and the next attempt is given a reflexion
# prompt naming the exact violations it must fix.  Hard gates
# (workflow-file allowlist, check_resolver_diff.sh) still run once
# post-loop on the accepted attempt — retrying them is unsafe (a
# hallucinated workflow edit must never be handed back to the
# model as "try again, here's what went wrong").
#
# INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS bounds the loop; kept at
# 3 to match the pre-restructure codex-liveness retry count.
# ----------------------------------------------------------------------
INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS=3
RESOLVER_ATTEMPT_BASE_DIR="${RUNTIME_DIR}/resolver_attempt_base"

# Pin the resolver's Codex reasoning effort independent of the editor's.
# review_autofix.yml's "Detect smoke test PR" step rewrites
# ~/.codex/config.toml's model_reasoning_effort to "low" (smoke-run
# override — both reviewers and editor use low for the bait-line task).
# The conflict resolver runs against the same config and would inherit
# that value; for smoke PRs that's fine since "low" doesn't trip the
# empty-stdout failure mode. The pin here exists so that future changes
# to the smoke override level don't accidentally starve the resolver:
# CONFLICT_RESOLVER_REASONING_EFFORT (workflow env, defaults "medium")
# overrides config.toml unconditionally before the resolver runs.
# PR #2058 / run 25300219172 originally hit this with override=none.
#
# Override config.toml here using CONFLICT_RESOLVER_REASONING_EFFORT
# (set by the workflow step's env, defaults to "medium"). Also ensure
# model_reasoning_summary = "auto" is present — same diagnostic +
# anti-empty-stdout rationale as the editor step.
_resolver_reasoning_effort="${CONFLICT_RESOLVER_REASONING_EFFORT:-medium}"
# Validate against known reasoning levels before interpolating into sed —
# CONFLICT_RESOLVER_REASONING_EFFORT comes from a repo var
# (vars.THINKING_LEVEL_CONFLICT_RESOLVER) so an unexpected value would
# corrupt the TOML or break sed under set -e. Mirrors the pattern used
# in scripts/review_consolidate.sh for REVIEW_CONSOLIDATOR_REASONING.
# Allowed levels match README.md ("Thinking levels"): xhigh|high|medium|none.
case "${_resolver_reasoning_effort}" in
  xhigh|high|medium|none) ;;
  *)
    echo "::warning::Invalid CONFLICT_RESOLVER_REASONING_EFFORT='${_resolver_reasoning_effort}'; falling back to medium."
    _resolver_reasoning_effort="medium"
    ;;
esac
_codex_config="${HOME}/.codex/config.toml"
# Fail-open guard around the rewrite: a sed/grep failure (permissions,
# unexpected file shape, missing GNU sed) should fall through to a
# ::warning:: rather than abort the resolver step under set -e — same
# spirit as the missing-config branch below. Robust grep/sed patterns
# tolerate whitespace and quoting variants so a non-canonical config
# (e.g. unquoted value, extra spaces) is still updated rather than
# silently no-op'd, which would re-introduce the empty-stdout failure
# this fix is meant to prevent. Post-edit verification reads the file
# back and emits a warning if the resolver value is missing.
if [ -f "${_codex_config}" ]; then
  _rewrite_ok=1
  {
    if grep -qE '^[[:space:]]*model_reasoning_effort[[:space:]]*=' "${_codex_config}"; then
      sed -i "s|^[[:space:]]*model_reasoning_effort[[:space:]]*=.*|model_reasoning_effort = \"${_resolver_reasoning_effort}\"|" "${_codex_config}"
    else
      printf '\nmodel_reasoning_effort = "%s"\n' "${_resolver_reasoning_effort}" >> "${_codex_config}"
    fi
    if grep -qE '^[[:space:]]*model_reasoning_summary[[:space:]]*=' "${_codex_config}"; then
      sed -i 's|^[[:space:]]*model_reasoning_summary[[:space:]]*=.*|model_reasoning_summary = "auto"|' "${_codex_config}"
    else
      sed -i '/^[[:space:]]*model_reasoning_effort[[:space:]]*=/a model_reasoning_summary = "auto"' "${_codex_config}"
    fi
  } || _rewrite_ok=0

  if [ "${_rewrite_ok}" -eq 0 ]; then
    echo "::warning::Codex config rewrite failed for ${_codex_config}; resolver will run with whatever reasoning the editor step left in place."
  else
    # Independent checks (not elif) so both mismatches surface together
    # if the rewrite somehow produced neither expected line.
    _verify_ok=1
    if ! grep -qE "^model_reasoning_effort = \"${_resolver_reasoning_effort}\"$" "${_codex_config}"; then
      echo "::warning::Codex config rewrite did not produce the expected model_reasoning_effort = \"${_resolver_reasoning_effort}\" line; resolver may run with stale reasoning."
      _verify_ok=0
    fi
    if ! grep -qE '^model_reasoning_summary = "auto"$' "${_codex_config}"; then
      echo "::warning::Codex config rewrite did not produce the expected model_reasoning_summary = \"auto\" line; the anti-empty-stdout safeguard may be unset."
      _verify_ok=0
    fi
    if [ "${_verify_ok}" -eq 1 ]; then
      echo "Conflict resolver reasoning effort set to ${_resolver_reasoning_effort} (model_reasoning_summary=auto)."
    fi
  fi
else
  echo "::warning::Codex config ${_codex_config} not found before resolver loop; reasoning effort override skipped."
fi

RESOLVER_ATTEMPT_BASE_MISSING_FILE="${RUNTIME_DIR}/resolver_attempt_base_missing.txt"
RESOLVER_RETRY_PROMPT_FILE="${RUNTIME_DIR}/resolver_retry_prompt.txt"
RESOLVER_MARKER_VIOLATIONS_FILE="${RUNTIME_DIR}/resolver_marker_violations.txt"
RESOLVER_FP_VIOLATIONS_FILE="${RUNTIME_DIR}/resolver_fp_violations.txt"
RESOLVER_FP_VIOLATIONS_PREV_FILE="${RUNTIME_DIR}/resolver_fp_violations_prev.txt"
RESOLVER_FP_VERIFIER_OUTPUT_FILE="${RUNTIME_DIR}/resolver_fp_verifier_output.txt"

# Snapshot every in-scope file (the resolver's allowlist, which
# prepare step populated with git-marked unmerged paths plus the
# fingerprint-violation expansion set) so retries re-start from
# the same post-merge-replay state instead of layering new edits
# on top of the previous rejected output.  Files outside the
# allowlist are off-limits to the resolver anyway (enforced by
# the post-loop allowlist guard + check_resolver_diff.sh), so a
# per-file allowlist snapshot is sufficient.
#
# Delete/modify conflicts: an allowlist path is listed as unmerged
# even when the working-tree file is absent (e.g. both-sides-deleted,
# or modify/delete resolved to deletion).  Record those paths in
# RESOLVER_ATTEMPT_BASE_MISSING_FILE so the restore function can
# `rm -f` them between retries — otherwise a file created by a
# failed attempt at such a path would leak into the next attempt's
# tree and silently violate the "retry starts from post-merge-replay
# state" contract.
: > "${RESOLVER_ATTEMPT_BASE_MISSING_FILE}"

# ── Smoke-fixture deterministic pre-resolution ────────────────────
# PR #2095 added a smoke-only override block to the resolver prompt
# instructing the model to apply_patch on tests/e2e_smoke_canary.txt
# (keep HEAD `run_id:`, drop the `alt-<run_id>` bait line). PR #2112 /
# run 25370025320 confirmed the override is rendered correctly but
# the legacy gpt-5.3-codex default still hit the documented empty-stdout
# failure mode on this trivial fixture (3 attempts, all reading the file
# then exiting with 0 bytes on stdout, no apply_patch invoked, retry
# loop bails). Kept as defense-in-depth on the gpt-5.4 default.
#
# Apply the override's specified resolution deterministically before
# entering the codex loop, gated on (a) IS_SMOKE_TEST=true, (b) the
# canary path is in the resolver's unmerged allowlist (so we never
# touch the canary on integration-sync runs that don't involve it),
# and (c) the canary file currently carries the well-known smoke
# conflict shape (`<<<<<<< HEAD` … `>>>>>>> origin/...`). The block
# is a no-op on production runs (IS_SMOKE_TEST unset) and on smoke
# runs where the merge auto-resolved the canary upstream.
#
# Snapshot capture below picks up the resolved file, so retries
# (which restore from the snapshot) continue to see no markers
# instead of re-introducing the conflict every attempt.
if [ "${IS_SMOKE_TEST:-false}" = "true" ] \
   && [ -f "${RESOLVER_ALLOWLIST_FILE:-/nonexistent}" ] \
   && [ -s "${RESOLVER_ALLOWLIST_FILE:-/nonexistent}" ]; then
  _smoke_canary="tests/e2e_smoke_canary.txt"
  if grep -Fxq "${_smoke_canary}" "${RESOLVER_ALLOWLIST_FILE}" \
     && [ -f "${_smoke_canary}" ] \
     && grep -qE '^<<<<<<< HEAD$' "${_smoke_canary}" \
     && grep -qE '^>>>>>>> origin/' "${_smoke_canary}"; then
    _smoke_resolved_tmp="$(mktemp)"
    if awk '
      /^<<<<<<< HEAD$/ { in_head=1; in_other=0; next }
      /^=======$/      { if (in_head) { in_head=0; in_other=1; next } }
      /^>>>>>>> /      { if (in_other) { in_other=0; next } }
      in_other         { next }
                       { print }
    ' "${_smoke_canary}" > "${_smoke_resolved_tmp}" \
       && ! grep -qE '^(<<<<<<< |=======$|>>>>>>> )' "${_smoke_resolved_tmp}"; then
      mv "${_smoke_resolved_tmp}" "${_smoke_canary}"
      echo "Smoke fixture: applied deterministic resolution to ${_smoke_canary} before codex resolver loop (kept HEAD-side run_id: line, dropped origin/main alt- bait; mirrors PR #2095 override directive). Empty-stdout failure mode on this fixture (documented under the legacy gpt-5.3-codex default, openai/codex#11151) would otherwise exhaust MAX_ATTEMPTS without committing."
    else
      rm -f "${_smoke_resolved_tmp}"
      echo "::warning::Smoke fixture: deterministic resolution of ${_smoke_canary} did not produce a marker-free file; falling back to model-driven resolution."
    fi
  fi
fi

if [ -f "${RESOLVER_ALLOWLIST_FILE}" ] && [ -s "${RESOLVER_ALLOWLIST_FILE}" ]; then
  mkdir -p "${RESOLVER_ATTEMPT_BASE_DIR}"
  while IFS= read -r _snap_path; do
    [ -z "${_snap_path}" ] && continue
    if [ ! -f "${_snap_path}" ]; then
      printf '%s\n' "${_snap_path}" >> "${RESOLVER_ATTEMPT_BASE_MISSING_FILE}"
      continue
    fi
    _snap_dst_dir="${RESOLVER_ATTEMPT_BASE_DIR}/$(dirname "${_snap_path}")"
    mkdir -p "${_snap_dst_dir}"
    cp -a "${_snap_path}" "${RESOLVER_ATTEMPT_BASE_DIR}/${_snap_path}"
  done < "${RESOLVER_ALLOWLIST_FILE}"
  _snap_count="$(find "${RESOLVER_ATTEMPT_BASE_DIR}" -type f 2>/dev/null | wc -l | tr -d '[:space:]')"
  _snap_missing_count="$(wc -l < "${RESOLVER_ATTEMPT_BASE_MISSING_FILE}" | tr -d '[:space:]')"
  echo "Resolver retry-base snapshot captured: ${_snap_count} file(s), ${_snap_missing_count} allowlist path(s) absent at snapshot time (delete/rename conflicts — will be re-deleted on restore)."
fi

# _restore_attempt_base: restore every snapshotted allowlist file to
# its post-merge-replay content.  Used between retries so each
# attempt sees the same starting tree.  Falls open on a missing
# snapshot (the pre-loop snapshot step only runs when the allowlist
# is non-empty; an empty allowlist means there was nothing to
# resolve anyway).
_restore_attempt_base() {
  if [ ! -d "${RESOLVER_ATTEMPT_BASE_DIR}" ]; then
    echo "::warning::Resolver retry-base snapshot missing at ${RESOLVER_ATTEMPT_BASE_DIR}; retry will layer on top of previous attempt's output."
    return 0
  fi
  local _rc=0
  while IFS= read -r _rpath; do
    [ -z "${_rpath}" ] && continue
    local _src="${RESOLVER_ATTEMPT_BASE_DIR}/${_rpath}"
    if [ -f "${_src}" ]; then
      mkdir -p "$(dirname "${_rpath}")" 2>/dev/null || true
      cp -a "${_src}" "${_rpath}" || _rc=1
    fi
  done < "${RESOLVER_ALLOWLIST_FILE}"
  # Re-delete allowlist paths that were absent at snapshot time
  # (delete/modify, both-sides-deleted, or rename conflicts).  A
  # failed first attempt may have created files at those paths
  # trying to resolve the conflict; leaving them in place would
  # mean the retry's starting tree silently differs from the
  # post-merge-replay state.  Restricted to the missing-list
  # captured before attempt 1, so we never touch anything outside
  # the resolver's allowlist scope.
  if [ -s "${RESOLVER_ATTEMPT_BASE_MISSING_FILE:-/nonexistent}" ]; then
    while IFS= read -r _missing_rpath; do
      [ -z "${_missing_rpath}" ] && continue
      [ -e "${_missing_rpath}" ] && rm -f -- "${_missing_rpath}"
    done < "${RESOLVER_ATTEMPT_BASE_MISSING_FILE}"
  fi
  return "${_rc}"
}

# _build_retry_prompt: render the reflexion prelude with the
# previous attempt's violations substituted in, then concatenate
# with the original resolver prompt.  Writes the combined prompt
# to ${RESOLVER_RETRY_PROMPT_FILE} and echoes that path.
#
# Only applied when IS_INTEGRATION_SYNC=true — the prelude text
# references INTENT_FINGERPRINTS and merged-sub-issue semantics
# that only the integration-sync resolver prompt carries.  For
# non-integration runs (generic conflict-resolver prompt) the
# retry uses the original prompt verbatim, which is still a
# strict improvement over today's one-shot behaviour because the
# working tree is now reset between attempts.
#
# Fail-open: if the prelude template is missing (older script_ref
# on a consumer repo), retry with the original prompt verbatim
# and emit a warning.  The soft retry itself is the core win; the
# reflexion content is an optimisation on top of it.
_build_retry_prompt() {
  local _prev_attempt="$1"
  local _marker_file="$2"
  local _fp_file="$3"
  local _prelude_tpl="${SUPPORT_PROMPTS_DIR:-prompts}/integration-sync-conflict-resolver-retry-prelude.txt"
  if [ "${IS_INTEGRATION_SYNC:-false}" != "true" ] || [ ! -f "${_prelude_tpl}" ]; then
    if [ "${IS_INTEGRATION_SYNC:-false}" = "true" ]; then
      echo "::warning::Resolver retry prelude template missing at ${_prelude_tpl}; retrying with original prompt verbatim (no reflexion)."
    fi
    cp -a "${CONFLICT_RESOLVER_PROMPT_FILE}" "${RESOLVER_RETRY_PROMPT_FILE}"
    return 0
  fi
  local _marker_count=0 _fp_count=0
  local _marker_list="(none)" _fp_details="(none)"
  if [ -s "${_marker_file}" ]; then
    _marker_count="$(wc -l < "${_marker_file}" | tr -d '[:space:]')"
    _marker_list="$(sed 's/^/          - /' "${_marker_file}")"
  fi
  if [ -s "${_fp_file}" ]; then
    _fp_count="$(wc -l < "${_fp_file}" | tr -d '[:space:]')"
    _fp_details="$(sed 's/^/          - /' "${_fp_file}")"
  fi
  # Render via python3 (same substitution pattern as
  # review_conflict_prepare.sh) so multi-line values with shell
  # metacharacters do not need quoting gymnastics.
  PRELUDE_TPL="${_prelude_tpl}" \
    ORIGINAL_PROMPT_FILE="${CONFLICT_RESOLVER_PROMPT_FILE}" \
    PREVIOUS_ATTEMPT_NUMBER="${_prev_attempt}" \
    MAX_ATTEMPTS="${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}" \
    MARKER_VIOLATION_COUNT="${_marker_count}" \
    MARKER_VIOLATION_FILES="${_marker_list}" \
    FINGERPRINT_VIOLATION_COUNT="${_fp_count}" \
    FINGERPRINT_VIOLATION_DETAILS="${_fp_details}" \
    python3 -c "import os,sys; tpl=open(os.environ['PRELUDE_TPL'],encoding='utf-8').read(); keys=['PREVIOUS_ATTEMPT_NUMBER','MAX_ATTEMPTS','MARKER_VIOLATION_COUNT','MARKER_VIOLATION_FILES','FINGERPRINT_VIOLATION_COUNT','FINGERPRINT_VIOLATION_DETAILS']; [tpl := tpl.replace('{{'+k+'}}', os.environ.get(k,'')) for k in keys]; orig=open(os.environ['ORIGINAL_PROMPT_FILE'],encoding='utf-8',errors='replace').read(); sys.stdout.write(tpl + orig)" \
    > "${RESOLVER_RETRY_PROMPT_FILE}"
}

# _scan_residual_markers: write the list of in-scope files that
# still contain unresolved Git conflict markers to
# ${RESOLVER_MARKER_VIOLATIONS_FILE}.  Scans only files in the
# allowlist — the resolver has no business editing anything else,
# and a stray marker inside an unrelated file would be a
# pre-existing repo state, not a resolver regression.
_scan_residual_markers() {
  : > "${RESOLVER_MARKER_VIOLATIONS_FILE}"
  [ -s "${RESOLVER_ALLOWLIST_FILE}" ] || return 0
  while IFS= read -r _mpath; do
    [ -z "${_mpath}" ] && continue
    [ -f "${_mpath}" ] || continue
    # Match only the canonical start/end markers.  The `=======`
    # separator is intentionally omitted — every conflict hunk
    # has a matching start and end, so scanning for either is
    # sufficient, and a bare `=======` line could legitimately
    # appear in prose/documentation files.  Same pattern
    # `review_conflict_prepare.sh` uses for its belt-and-suspenders
    # git-grep conflicted-paths capture.
    if grep -qE '^(<<<<<<< |>>>>>>> )' -- "${_mpath}" 2>/dev/null; then
      printf '%s\n' "${_mpath}" >> "${RESOLVER_MARKER_VIOLATIONS_FILE}"
    fi
  done < "${RESOLVER_ALLOWLIST_FILE}"
}

# _verify_fingerprints_soft: run verify_integration_fingerprints.py
# with annotations suppressed (routed to a capture file) on
# intermediate attempts so retried-away failures do not spam the
# GHA log with false-positive error annotations.  On the final
# attempt the caller re-runs the verifier at normal verbosity so
# the abort annotation is visible exactly as today.
#
# Sets RESOLVER_FP_EXIT to the verifier exit code and writes the
# parsed violation list (one per line, without `::error::  - `
# prefix) to ${RESOLVER_FP_VIOLATIONS_FILE}.
RESOLVER_FP_EXIT=0
_verify_fingerprints_soft() {
  RESOLVER_FP_EXIT=0
  : > "${RESOLVER_FP_VIOLATIONS_FILE}"
  : > "${RESOLVER_FP_VERIFIER_OUTPUT_FILE}"
  if [ "${IS_INTEGRATION_SYNC:-false}" != "true" ]; then
    return 0
  fi
  if [ -z "${INTEGRATION_FINGERPRINTS_FILE:-}" ] || [ ! -f "${INTEGRATION_FINGERPRINTS_FILE}" ]; then
    return 0
  fi
  local _fp_size
  _fp_size="$(wc -c < "${INTEGRATION_FINGERPRINTS_FILE}" 2>/dev/null || echo 0)"
  if [ "${_fp_size}" -le 2 ]; then
    return 0
  fi
  if [ ! -f "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" ]; then
    return 0
  fi
  INTEGRATION_BRANCH_NAME="${INTEGRATION_BRANCH_NAME:-${TARGET_BRANCH:-}}" \
    python3 "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" \
      "${INTEGRATION_FINGERPRINTS_FILE}" \
      > "${RESOLVER_FP_VERIFIER_OUTPUT_FILE}" 2>&1 || RESOLVER_FP_EXIT=$?
  # Extract per-violation lines and strip the annotation prefix so
  # the retry prelude gets clean, human-readable bullets.  Only
  # exit 1 (real hard violations) populates the violations file;
  # exit 2 is a plumbing failure (malformed fingerprints JSON,
  # missing verifier binary elsewhere) and retrying against it
  # cannot make progress, so we fail-open just like the
  # pre-restructure post-loop case statement did
  # (`2|*) echo "::warning::..."`).  The loop's success check
  # treats exit 0 and exit 2 symmetrically.
  if [ "${RESOLVER_FP_EXIT}" -eq 1 ] && [ -s "${RESOLVER_FP_VERIFIER_OUTPUT_FILE}" ]; then
    grep -E '^::error::  - ' "${RESOLVER_FP_VERIFIER_OUTPUT_FILE}" \
      | sed 's/^::error::  - //' \
      > "${RESOLVER_FP_VIOLATIONS_FILE}" || true
  elif [ "${RESOLVER_FP_EXIT}" -eq 2 ]; then
    echo "::warning::Integration fingerprint verification could not run (exit 2 — plumbing failure); continuing without intent guard for this commit. See ${RESOLVER_FP_VERIFIER_OUTPUT_FILE} for details."
  fi
}

# Pre-load the conflicted files into the resolver prompt so the model
# sees the actual conflict-marker bytes without spending a tool call to
# read them. RESOLVER_ALLOWLIST_FILE is the canonical in-scope list (one
# path per line) populated by review_conflict_prepare.sh from
# `git diff --name-only --diff-filter=U` plus any fingerprint-violation
# expansions. The script's per-file cap skips big files (e.g. large
# generated YAML) with a "read with read tool" marker so the model
# fetches them properly. Fail-open: any error here continues the
# resolver loop without the targeted-context block.
TARGETED_FILES_CONTEXT_FILE="${RUNTIME_DIR}/targeted_files_context.txt"
: > "${TARGETED_FILES_CONTEXT_FILE}"
if [ -s "${RESOLVER_ALLOWLIST_FILE:-}" ] && [ -f "scripts/targeted_file_context.py" ]; then
  python3 scripts/targeted_file_context.py \
    --paths-file "${RESOLVER_ALLOWLIST_FILE}" \
    --repo-root "${GITHUB_WORKSPACE:-$(pwd)}" \
    --max-files "${TARGETED_FILE_CONTEXT_MAX_FILES:-10}" \
    --max-bytes "${TARGETED_FILE_CONTEXT_MAX_BYTES:-102400}" \
    --header-text "These are the conflicted files you must resolve. Their current contents (with Git conflict markers) are inlined below so you can edit immediately without re-reading them. Files marked \"would overflow total budget\" or listed under \"Deferred\" must be read with the read tool — never assume their content is in this block." \
    --output "${TARGETED_FILES_CONTEXT_FILE}" || \
    echo "::warning::targeted_file_context.py failed; continuing without targeted-context block"
fi
# Append the targeted-context block to the rendered prompt once, so
# every retry (which copies CONFLICT_RESOLVER_PROMPT_FILE into
# RESOLVER_RETRY_PROMPT_FILE at the loop top) inherits it.
if [ -s "${TARGETED_FILES_CONTEXT_FILE}" ]; then
  printf '\n' >> "${CONFLICT_RESOLVER_PROMPT_FILE}"
  cat "${TARGETED_FILES_CONTEXT_FILE}" >> "${CONFLICT_RESOLVER_PROMPT_FILE}"
fi

attempt=1
while [ "${attempt}" -le "${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}" ]; do
  # On retries: restore the working tree to its post-merge-replay
  # state (so retries don't compound the previous attempt's bad
  # edits) and build a reflexion prompt naming the previous
  # attempt's violations.
  if [ "${attempt}" -gt 1 ]; then
    _restore_attempt_base || echo "::warning::Resolver retry-base restore reported a non-fatal error; continuing."
    _build_retry_prompt "$((attempt - 1))" \
      "${RESOLVER_MARKER_VIOLATIONS_FILE}" \
      "${RESOLVER_FP_VIOLATIONS_FILE}"
    _effective_prompt_file="${RESOLVER_RETRY_PROMPT_FILE}"
    if [ -f "${RESOLVER_MARKER_VIOLATIONS_FILE}" ]; then
      _prev_marker_count="$(wc -l < "${RESOLVER_MARKER_VIOLATIONS_FILE}" | tr -d '[:space:]')"
      _prev_marker_count="${_prev_marker_count:-0}"
    else
      _prev_marker_count=0
    fi
    if [ -f "${RESOLVER_FP_VIOLATIONS_FILE}" ]; then
      _prev_fp_violation_count="$(wc -l < "${RESOLVER_FP_VIOLATIONS_FILE}" | tr -d '[:space:]')"
      _prev_fp_violation_count="${_prev_fp_violation_count:-0}"
    else
      _prev_fp_violation_count=0
    fi
    echo "Conflict resolver retry ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: rebuilt reflexion prompt (prev markers=${_prev_marker_count}, prev fingerprint_violations=${_prev_fp_violation_count})."
  else
    _effective_prompt_file="${CONFLICT_RESOLVER_PROMPT_FILE}"
  fi

  tmp_output="$(mktemp)"
  if ! codex --ask-for-approval never exec --model "${MODEL_EDITOR}" --sandbox danger-full-access "$(cat "${_effective_prompt_file}")" > "${tmp_output}"; then
    rm -f "${tmp_output}"
    if [ "${attempt}" -eq "${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}" ]; then
      echo "Conflict resolver failed after retries."
      exit 1
    fi
    attempt=$((attempt + 1))
    sleep 2
    continue
  fi
  # Empty stdout is no longer treated as a hard failure on its own.
  # The editor model can invoke apply_patch tool calls without emitting
  # any final assistant text (PR #2086 documents the same signature on
  # the editor side; PR #2112 / run 25370025320 hit it on the resolver
  # side under the legacy gpt-5.3-codex default after PR #2095's
  # override was already in place).
  # Working-tree state is the source of truth: if the soft-validation
  # gates pass (no residual markers, fingerprints satisfied) the
  # resolution is real regardless of an empty summary, and if they
  # fail the existing retry / no-progress / exhaustion paths handle
  # it. Write a placeholder summary so downstream commit / posting
  # logic always has parseable text.
  _empty_stdout_observed=0
  if [ ! -s "${tmp_output}" ]; then
    _empty_stdout_observed=1
    printf 'Resolver produced no stdout on attempt %s; working-tree state validated directly (markers + fingerprint gates).\n' "${attempt}" > "${tmp_output}"
  fi
  mv "${tmp_output}" "${CONFLICT_RESOLVER_SUMMARY_FILE}"
  if [ "${_empty_stdout_observed}" -eq 1 ]; then
    echo "Conflict resolver produced no stdout on attempt ${attempt}; using placeholder summary and running soft validation against working tree."
  else
    echo "Conflict resolver produced output on attempt ${attempt}; running soft validation."
  fi

  # Soft validation #1: residual Git conflict markers.  A
  # resolver output that leaves markers behind never parses
  # correctly downstream and always represents a regression
  # from the post-merge state — retry is strictly better than
  # abort here.  Cheap (single grep per in-scope file), runs
  # before the Python-heavy fingerprint verifier.
  _scan_residual_markers
  _marker_count="$(wc -l < "${RESOLVER_MARKER_VIOLATIONS_FILE}" 2>/dev/null | tr -d '[:space:]')"

  # Soft validation #2: integration-sync fingerprint verification.
  # On intermediate attempts the verifier output is captured
  # (no annotations); on the final attempt we re-run it at
  # normal verbosity below so the operator-facing error messages
  # match today's log shape exactly.
  _verify_fingerprints_soft
  _fp_count="$(wc -l < "${RESOLVER_FP_VIOLATIONS_FILE}" 2>/dev/null | tr -d '[:space:]')"

  # Success: no residual markers AND verifier was non-rejecting.
  # Treat exit 0 (all fingerprints satisfied) and exit 2 (plumbing
  # failure — fail-open per the verifier's contract, same as the
  # pre-restructure `2|*) echo "::warning::..."` branch) as both
  # passing the soft gate.  Only exit 1 is a real violation
  # worth retrying against.
  if [ "${_marker_count}" -eq 0 ] && { [ "${RESOLVER_FP_EXIT}" -eq 0 ] || [ "${RESOLVER_FP_EXIT}" -eq 2 ]; }; then
    echo "Conflict resolver succeeded on attempt ${attempt} (soft validation passed)."
    # Re-emit the verifier's info line at normal verbosity so
    # operators see the "must_contain satisfied N/M" summary
    # that exists today.  Plumbing-exit (2) was not a soft
    # failure; just pass through any captured output.
    if [ -s "${RESOLVER_FP_VERIFIER_OUTPUT_FILE}" ]; then
      cat "${RESOLVER_FP_VERIFIER_OUTPUT_FILE}" || true
    fi
    break
  fi

  echo "::warning::Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS} failed soft validation: residual_markers=${_marker_count} fingerprint_violations=${_fp_count}."
  if [ "${_marker_count}" -gt 0 ]; then
    echo "Files with residual Git conflict markers (first 10):"
    head -10 "${RESOLVER_MARKER_VIOLATIONS_FILE}" | sed 's/^/  - /' || true
  fi
  if [ "${_fp_count}" -gt 0 ]; then
    echo "Fingerprint violations (first 10):"
    head -10 "${RESOLVER_FP_VIOLATIONS_FILE}" | sed 's/^/  - /' || true
  fi

  # No-progress detection: if attempt N's fingerprint violation set
  # is identical to attempt N-1's, the model has already seen the
  # reflexion prompt and reproduced the same output — additional
  # retries are extremely unlikely to make progress.  Promote the
  # attempt counter to MAX so the existing exhaustion block runs
  # (re-emits the annotated verifier output, sets CONFLICT_RESOLVED=
  # false, exit 1).  This keeps the abort shape symmetric with the
  # natural-exhaustion path, so downstream tooling (orchestrator
  # poller integration-judge dispatch, telegram failure alert,
  # ai:integration-judge-failed transitions) sees the same signal
  # whether the loop bailed early or ran to MAX_ATTEMPTS.
  #
  # Restricted to IS_INTEGRATION_SYNC=true: only fingerprint
  # violations carry stable per-pattern identity.  Residual-marker
  # failures are excluded — marker presence/absence routinely
  # changes between attempts and is not a reliable progress signal.
  # Comparison is sort-then-cmp so any future verifier-output
  # reordering does not cause a false negative.
  if [ "${attempt}" -gt 1 ] \
     && [ "${IS_INTEGRATION_SYNC:-false}" = "true" ] \
     && [ "${_fp_count}" -gt 0 ] \
     && [ -s "${RESOLVER_FP_VIOLATIONS_PREV_FILE}" ]; then
    _np_cur="$(mktemp)"
    _np_prev="$(mktemp)"
    sort -- "${RESOLVER_FP_VIOLATIONS_FILE}"     > "${_np_cur}"  || true
    sort -- "${RESOLVER_FP_VIOLATIONS_PREV_FILE}" > "${_np_prev}" || true
    if cmp -s "${_np_cur}" "${_np_prev}"; then
      echo "::warning::Conflict resolver no-progress detected: attempt ${attempt} produced the same ${_fp_count} fingerprint violation(s) as attempt $((attempt - 1)). Escalating to orchestrator integration judge instead of continuing the retry loop."
      attempt="${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}"
    else
      # Count-not-decreasing fallback: even if the violation text
      # changed (different patterns regressing each attempt), if the
      # count did not strictly decrease the model is not converging
      # toward zero violations.  Observed in 2026-04-25 PR #1533
      # (project-1479) where every attempt produced 25 violations
      # from a different subset of fingerprints, so cmp -s saw three
      # different files and the strict early-exit did not fire.
      _fp_count_prev="$(wc -l < "${RESOLVER_FP_VIOLATIONS_PREV_FILE}" 2>/dev/null | tr -d '[:space:]')"
      _fp_count_prev="${_fp_count_prev:-0}"
      if [ "${_fp_count}" -ge "${_fp_count_prev}" ]; then
        echo "::warning::Conflict resolver no-progress detected: attempt ${attempt} produced ${_fp_count} fingerprint violation(s), not fewer than the ${_fp_count_prev} from attempt $((attempt - 1)). Escalating to orchestrator integration judge instead of continuing the retry loop."
        attempt="${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}"
      fi
    fi
    rm -f "${_np_cur}" "${_np_prev}" 2>/dev/null || true
  fi

  if [ "${attempt}" -eq "${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}" ]; then
    # Final attempt exhausted.  Re-run the verifier at normal
    # verbosity so the annotated ::error:: lines (including the
    # "Aborting [ai-merge-resolve] commit: integration
    # fingerprint verification rejected the resolver output."
    # string consumed by downstream tooling) land in the log
    # exactly as today.  The IS_INTEGRATION_SYNC gate keeps
    # non-integration runs on their current path.
    if [ "${IS_INTEGRATION_SYNC:-false}" = "true" ] \
       && [ -n "${INTEGRATION_FINGERPRINTS_FILE:-}" ] \
       && [ -f "${INTEGRATION_FINGERPRINTS_FILE}" ] \
       && [ -f "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" ]; then
      _final_fp_exit=0
      INTEGRATION_BRANCH_NAME="${INTEGRATION_BRANCH_NAME:-${TARGET_BRANCH:-}}" \
        python3 "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" \
          "${INTEGRATION_FINGERPRINTS_FILE}" || _final_fp_exit=$?
      if [ "${_final_fp_exit}" -eq 1 ]; then
        echo "::error::Aborting [ai-merge-resolve] commit: integration fingerprint verification rejected the resolver output."
        echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
        exit 1
      fi
    fi
    if [ "${_marker_count}" -gt 0 ]; then
      echo "::error::Conflict resolver exhausted ${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS} attempts; ${_marker_count} file(s) still contain unresolved Git conflict markers. Aborting [ai-merge-resolve] commit."
      echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
      exit 1
    fi
    # Any other soft-failure class falls through to a generic
    # abort so the orchestrator integration judge takes over on
    # the next poll tick.
    echo "::error::Conflict resolver exhausted ${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS} attempts with soft-validation failures that could not be auto-recovered."
    echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
    exit 1
  fi
  # Snapshot this attempt's fingerprint violations so the next
  # iteration's no-progress check has a reference.  cp -a preserves
  # an empty file if the verifier produced none.
  if [ -f "${RESOLVER_FP_VIOLATIONS_FILE}" ]; then
    cp -a "${RESOLVER_FP_VIOLATIONS_FILE}" "${RESOLVER_FP_VIOLATIONS_PREV_FILE}" || true
  fi
  attempt=$((attempt + 1))
  sleep 2
done

# Remove root-level workflow-generated artifacts so they are never
# committed to caller repos.  Skip when running on coding-workflows
# itself — these files are actual source code there, not artifacts.
# NOTE: prompts/ is cleaned up in the final "Cleanup temporary
# artifacts" step because later notification steps (Telegram,
# labeling) still need scripts/tg_helpers.sh and
# scripts/label_helpers.sh.  prompts/ is excluded from git add via
# ':!prompts' patterns; fetched scripts are excluded via the
# bootstrap-generated scripts/.gitignore.
if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ]; then
  rm -f ./pre_assembled_static.txt
  rm -f unattended_system_instructions.md ai_pipeline.md agents.md probably_unnecessary_but_read_if_stuck.md
fi

if [ -n "$(git status --porcelain)" ]; then
  git config user.name "codex-bot"
  git config user.email "codex@users.noreply.github.com"
  git rm -r --cached node_modules 2>/dev/null || true
  if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ]; then
    # On the workflow source repo, identify the files Codex actually
    # wrote during conflict resolution so we can surface them in
    # the job log and (below) stage them on top of the live merge
    # index.  Auto-merged files the editor did NOT touch stay in
    # the index as git merged them — we deliberately do NOT strip
    # them, because stripping them broke merge ancestry (see the
    # comment block below the pre-snapshot diff for the full
    # rationale).
    RESOLVER_TOUCHED_FILE="${RUNTIME_DIR}/codex_touched_resolver.txt"
    : > "${RESOLVER_TOUCHED_FILE}"
    if [ -f "${PRE_RESOLVER_STATE_FILE:-/nonexistent}" ]; then
      PRE_UNTRACKED_LIST="$(mktemp)"
      POST_UNTRACKED_LIST="$(mktemp)"
      while IFS=$'\t' read -r diff_kind diff_a diff_b diff_c; do
        case "${diff_kind}" in
          T)
            old_sha="${diff_a}"
            old_exec="${diff_b}"
            diff_path="${diff_c}"
            [ -z "${diff_path}" ] && continue
            if [ ! -e "${diff_path}" ]; then
              printf '%s\n' "${diff_path}" >> "${RESOLVER_TOUCHED_FILE}"
              continue
            fi
            new_sha="$(git hash-object -- "${diff_path}" 2>/dev/null || true)"
            if [ -x "${diff_path}" ]; then
              new_exec=1
            else
              new_exec=0
            fi
            if { [ -n "${new_sha}" ] && [ "${new_sha}" != "${old_sha}" ]; } || [ "${new_exec}" != "${old_exec}" ]; then
              printf '%s\n' "${diff_path}" >> "${RESOLVER_TOUCHED_FILE}"
            fi
            ;;
          U)
            printf '%s\n' "${diff_a}" >> "${PRE_UNTRACKED_LIST}"
            ;;
        esac
      done < "${PRE_RESOLVER_STATE_FILE}"
      git ls-files --others --exclude-standard > "${POST_UNTRACKED_LIST}" || true
      sort -o "${PRE_UNTRACKED_LIST}" "${PRE_UNTRACKED_LIST}"
      sort -o "${POST_UNTRACKED_LIST}" "${POST_UNTRACKED_LIST}"
      comm -13 "${PRE_UNTRACKED_LIST}" "${POST_UNTRACKED_LIST}" >> "${RESOLVER_TOUCHED_FILE}"
      rm -f "${PRE_UNTRACKED_LIST}" "${POST_UNTRACKED_LIST}"
    else
      echo "::warning::pre-resolver snapshot missing; falling back to full status."
      git ls-files --modified --others --exclude-standard >> "${RESOLVER_TOUCHED_FILE}" || true
    fi
    sort -u -o "${RESOLVER_TOUCHED_FILE}" "${RESOLVER_TOUCHED_FILE}"
    touched_count="$(wc -l < "${RESOLVER_TOUCHED_FILE}" | tr -d '[:space:]')"
    echo "Resolver-touched files (${touched_count}):"
    sed 's/^/ - /' "${RESOLVER_TOUCHED_FILE}" || true

    # Allowlist validation (hallucination guard):
    # The conflict-resolver prompt explicitly forbids editing
    # files that don't contain conflict markers.  Compare the
    # files Codex actually touched against the unmerged-paths
    # allowlist captured right after the merge replay.  Any
    # .github/workflows/*.y(a)ml file touched outside the
    # allowlist is treated as a hallucinated modification —
    # the highest-risk class of editor drift, because a
    # corrupt workflow file poisons every subsequent run's
    # bootstrap (see PR #912 post-mortem where the resolver
    # added 300 lines referencing files that never existed).
    # Non-workflow out-of-allowlist files emit a warning only,
    # since the merge-staged index may legitimately contain
    # auto-resolved files Codex lightly touched.
    if [ -s "${RESOLVER_ALLOWLIST_FILE:-/nonexistent}" ] || [ -f "${RESOLVER_ALLOWLIST_FILE:-/nonexistent}" ]; then
      WORKFLOW_VIOLATIONS_FILE="${RUNTIME_DIR}/resolver_workflow_violations.txt"
      OTHER_VIOLATIONS_FILE="${RUNTIME_DIR}/resolver_other_violations.txt"
      : > "${WORKFLOW_VIOLATIONS_FILE}"
      : > "${OTHER_VIOLATIONS_FILE}"
      while IFS= read -r _touched_path; do
        [ -z "${_touched_path}" ] && continue
        if grep -Fxq "${_touched_path}" "${RESOLVER_ALLOWLIST_FILE}"; then
          continue
        fi
        case "${_touched_path}" in
          .github/workflows/*.yml|.github/workflows/*.yaml)
            printf '%s\n' "${_touched_path}" >> "${WORKFLOW_VIOLATIONS_FILE}"
            ;;
          *)
            printf '%s\n' "${_touched_path}" >> "${OTHER_VIOLATIONS_FILE}"
            ;;
        esac
      done < "${RESOLVER_TOUCHED_FILE}"

      if [ -s "${OTHER_VIOLATIONS_FILE}" ]; then
        echo "::warning::Conflict resolver touched non-workflow files that were not in the unmerged set. This may be benign (e.g. auto-merged index updates) but is worth noting:"
        sed 's/^/ - /' "${OTHER_VIOLATIONS_FILE}" || true
      fi

      if [ -s "${WORKFLOW_VIOLATIONS_FILE}" ]; then
        echo "::error::Conflict resolver modified GitHub workflow files that were NOT in the unmerged set. This is the signature of a hallucinated [ai-merge-resolve] edit and must not be committed — a corrupt workflow file would break every subsequent run's bootstrap. Refusing to commit."
        echo "Unmerged paths captured before codex exec (allowlist):"
        if [ -s "${RESOLVER_ALLOWLIST_FILE}" ]; then
          sed 's/^/ - /' "${RESOLVER_ALLOWLIST_FILE}" || true
        else
          echo "  (allowlist is empty — the merge replay produced no unmerged paths)"
        fi
        echo "Workflow files touched by resolver but NOT in allowlist:"
        sed 's/^/ - /' "${WORKFLOW_VIOLATIONS_FILE}" || true
        exit 1
      fi
    else
      echo "::warning::Resolver allowlist file missing; skipping hallucination guard. Falling through to existing commit-gate protections."
    fi

    # Hard guardrail against hallucinated merge resolutions.
    # check_resolver_diff.sh enforces three invariants:
    #   1. touched ⊆ conflicted  — the resolver may only edit
    #      files that actually had merge markers.  This is what
    #      catches the PR #912 failure mode where the resolver
    #      added 300 lines + references to nonexistent helper
    #      scripts under the guise of "merge resolution".
    #   2. bash -n / py_compile / json.load on every touched
    #      file — catches truncated heredocs and similar.
    #   3. Workflow → script reference integrity for any
    #      modified .github/workflows/*.yml — would have caught
    #      build_repo_overview.sh / protected_paths.txt.
    # On failure: skip the merge-resolve commit and exit 1 so
    # the run goes to ai:review-blocked instead of pushing a
    # broken commit that breaks every subsequent autofix run.
    if [ ! -f "${CONFLICTED_PATHS_FILE:-/nonexistent}" ]; then
      echo "::error::Conflicted-paths snapshot missing; refusing to create [ai-merge-resolve] commit without resolver validation."
      echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
      rm -f "${RESOLVER_TOUCHED_FILE}"
      exit 1
    fi

    if [ ! -x "${SUPPORT_SCRIPTS_DIR}/check_resolver_diff.sh" ]; then
      echo "::error::check_resolver_diff.sh is missing or not executable; refusing to create [ai-merge-resolve] commit without resolver validation."
      echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
      rm -f "${RESOLVER_TOUCHED_FILE}"
      exit 1
    fi

    if ! "${SUPPORT_SCRIPTS_DIR}/check_resolver_diff.sh" \
        --conflicted-set "${CONFLICTED_PATHS_FILE}" \
        --touched-set    "${RESOLVER_TOUCHED_FILE}" \
        --repo-root      "${PWD}"; then
      echo "::error::Conflict resolver output failed validation; skipping [ai-merge-resolve] commit."
      echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
      rm -f "${RESOLVER_TOUCHED_FILE}"
      exit 1
    fi

    # Stage the editor's conflict resolutions on top of the
    # existing merge index.  Crucially, we KEEP .git/MERGE_HEAD
    # in place AND we do NOT reset the index to HEAD.  Both
    # are required for correctness:
    #
    # 1. Keeping MERGE_HEAD makes `git commit` below produce a
    #    real merge commit with two parents (HEAD and base).
    #    Without the second parent, git's merge-base walker
    #    rewinds to the original merge base on every subsequent
    #    `git merge base` attempt and re-raises the identical
    #    conflict — observed on PR #908, where the previous
    #    single-parent resolve commit left the PR in
    #    mergeable_state=dirty with the same conflict line.
    #
    # 2. NOT resetting the index preserves git merge's
    #    auto-merged content for files the editor did not
    #    touch.  An earlier attempt at this fix did `git
    #    read-tree HEAD` to keep the commit's first-parent
    #    diff minimal, but that silently reverted base-side
    #    changes to auto-merged files: when the PR was
    #    eventually merged into BASE, git saw PR HEAD's old
    #    content as "theirs" against base's unchanged content
    #    and applied the revert.  Keeping the merged index
    #    lets base's changes flow through the merge commit
    #    unchanged, so the eventual merge-to-BASE is a clean
    #    fast-forward-equivalent.
    #
    # The previous per-file `git add`/`git rm` loop below still
    # runs on top of the merge index: for conflicted paths
    # whose conflict markers the editor removed, `git add`
    # replaces the unmerged index entries with the editor's
    # resolved content.  Auto-merged paths the editor did not
    # touch stay in the index as git merged them.
    git rm -r --cached --ignore-unmatch -- node_modules 2>/dev/null || true
    while IFS= read -r touched_path; do
      [ -z "${touched_path}" ] && continue
      case "${touched_path}" in
        node_modules|node_modules/*|*/node_modules|*/node_modules/*) continue ;;
      esac
      if [ -e "${touched_path}" ]; then
        git add -- "${touched_path}" 2>/dev/null || true
      else
        git rm -q -- "${touched_path}" 2>/dev/null || true
      fi
    done < "${RESOLVER_TOUCHED_FILE}"
    rm -f "${RESOLVER_TOUCHED_FILE}"
  else
    # Build per-file exclusions from scripts/.gitignore when present.
    # If it is absent, keep exclusions empty so consumer-owned scripts/
    # changes are still staged.
    _rs_script_excludes=()
    if [ -f scripts/.gitignore ]; then
      while IFS= read -r _ign_entry; do
        [[ -z "${_ign_entry}" || "${_ign_entry}" == \#* ]] && continue
        _rs_script_excludes+=(":!scripts/${_ign_entry}")
      done < scripts/.gitignore
    fi
    git add -u -- ':!node_modules' "${_rs_script_excludes[@]}" ':!prompts' ':!ai-memory' ':!.codex-workflow-src' ':!.codex-workflow-src-main' ':!.github/prompts' ':!.github/scripts'
    git ls-files --others --exclude-standard -z -- ':!node_modules' "${_rs_script_excludes[@]}" ':!prompts' ':!ai-memory' ':!.codex-workflow-src' ':!.codex-workflow-src-main' ':!.github/ai' ':!.github/prompts' ':!.github/scripts' | xargs -0 -r git add --
  fi
  echo "Staged files before commit:"
  STAGED_FILES="$(git diff --cached --name-only || true)"
  printf '%s\n' "${STAGED_FILES}" | sed '/^$/d; s/^/ - /' || true
  # Soft guardrail: if workflow runtime/helper artifacts leaked into
  # the staging area (e.g. merge auto-stage of conflicted paths,
  # codex exec writing to a protected path, or leaked tracked files
  # on the consumer branch), unstage them and continue.  A previous
  # hard `exit 1` here silently threw away reviewer+editor work on
  # what is usually a recoverable condition.
  PROTECTED_LEAKED=false
  if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ] && printf '%s\n' "${STAGED_FILES}" | grep -Eq '^\.github/(prompts|scripts)/'; then
    echo "::warning::.github/prompts or .github/scripts was staged in consumer repo; unstaging protected paths and continuing."
    git reset -q HEAD -- '.github/prompts' '.github/scripts' 2>/dev/null || true
    PROTECTED_LEAKED=true
  fi
  if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ] && printf '%s\n' "${STAGED_FILES}" | grep -Eq '^(prompts/|\.github/scripts/|\.github/prompts/|ai-memory/|\.codex-workflow-src/|\.codex-workflow-src-main/)'; then
    echo "::warning::workflow runtime/helper artifacts were staged in consumer repo; unstaging protected paths and continuing."
    git reset -q HEAD -- 'prompts' '.github/scripts' '.github/prompts' 'ai-memory' '.codex-workflow-src' '.codex-workflow-src-main' 2>/dev/null || true
    PROTECTED_LEAKED=true
  fi
  if [ "${PROTECTED_LEAKED}" = "true" ]; then
    STAGED_FILES="$(git diff --cached --name-only || true)"
    echo "Staged files after protected-path reset:"
    printf '%s\n' "${STAGED_FILES}" | sed '/^$/d; s/^/ - /' || true
    if git diff --cached --quiet; then
      echo "No repository changes remain after protected-path reset; skipping merge-resolve commit."
      echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
      exit 0
    fi
  fi
  if git diff --cached --quiet; then
    echo "No staged merge resolution changes remain; skipping merge-resolve commit."
    echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
    exit 0
  fi

  # ============================================================
  # Integration-sync intent fingerprint verification now runs
  # INSIDE the retry loop above (see the "Retry-loop hardening"
  # block at the top of this script).  Moving it from here to
  # inside the loop is the fix for the recurring
  # "Aborting [ai-merge-resolve] commit: integration
  # fingerprint verification rejected the resolver output."
  # failure on large integration PRs — previously the verifier
  # ran once post-loop and any violation terminated the run
  # with zero retries consumed on the real failure class.
  #
  # IS_INTEGRATION_SYNC gate, fingerprints-file gate, size
  # check, and the fail-open-on-plumbing-error (exit 2)
  # semantics all live in _verify_fingerprints_soft and the
  # exhausted-retry tail in the loop above.  The hard error
  # string ("Aborting [ai-merge-resolve] commit: integration
  # fingerprint verification rejected the resolver output.")
  # is preserved verbatim at the end of the loop's final
  # attempt so downstream tooling (and
  # tests/test_orchestrate_poll_process.py) continues to
  # match.
  # ============================================================

  git commit -m "[ai-merge-resolve] resolve merge conflicts"
  git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}
  # NOTE: push deferred to final "Push all pending commits" step.
  echo "CONFLICT_RESOLVED=true" >> "$GITHUB_ENV"
  echo "Conflicts resolved and committed (push deferred)"
else
  echo "No conflict resolution changes to commit"
fi


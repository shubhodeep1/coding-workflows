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
#                                   defaults to high (lowered from xhigh after runs
#                                   25627236793 / 25627316961 hit `timeout`-killed retries
#                                   on degenerate orchestrator-stack integrations — see the
#                                   comment block on review_autofix.yml's
#                                   CONFLICT_RESOLVER_REASONING_EFFORT env var). Decoupled
#                                   from EDITOR_REASONING_EFFORT so the smoke-test override
#                                   (which sets editor reasoning to "none") doesn't starve
#                                   the resolver.
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

SUPPORT_SCRIPTS_DIR="${SUPPORT_SCRIPTS_DIR:-scripts}"
CODEX_HEARTBEAT_HELPER="${SUPPORT_SCRIPTS_DIR:-scripts}/codex_heartbeat.sh"
CODEX_STALL_GUARD_HELPER="${SUPPORT_SCRIPTS_DIR:-scripts}/codex_stall_guard.sh"
WORKSPACE_SAFETY_CHECK_HELPER="${SUPPORT_SCRIPTS_DIR:-scripts}/workspace_safety_check.sh"
ORCHESTRATE_FORCE_TICK_HELPER="${SUPPORT_SCRIPTS_DIR:-scripts}/orchestrate_force_tick.sh"
CODEX_THREAD_REUSE_ENABLED="${CODEX_THREAD_REUSE_ENABLED:-false}"
CODEX_THREAD_REUSE_HELPER=""
for _thread_reuse_candidate in \
  "${SUPPORT_SCRIPTS_DIR:-scripts}/codex_thread_reuse.sh" \
  "scripts/codex_thread_reuse.sh" \
  ".codex-workflow-src/scripts/codex_thread_reuse.sh" \
  ".codex-workflow-src-main/scripts/codex_thread_reuse.sh"; do
  if [ -f "${_thread_reuse_candidate}" ]; then
    CODEX_THREAD_REUSE_HELPER="${_thread_reuse_candidate}"
    break
  fi
done
export CODEX_THREAD_REUSE_ENABLED
export CODEX_THREAD_REUSE_RUNTIME_DIR="${CODEX_THREAD_REUSE_RUNTIME_DIR:-${RUNTIME_DIR}}"
if [ -n "${CODEX_THREAD_REUSE_HELPER}" ]; then
  # shellcheck disable=SC1090
  source "${CODEX_THREAD_REUSE_HELPER}"
fi

resolve_conflict_thread_reuse_asset() {
  local repo_path="$1"
  local candidate=""

  for candidate in \
    "${repo_path}" \
    ".codex-workflow-src/${repo_path}" \
    ".codex-workflow-src-main/${repo_path}"; do
    if [ -f "${candidate}" ]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  return 1
}

conflict_thread_reuse_enabled() {
  [ -n "${CODEX_THREAD_REUSE_HELPER:-}" ] || return 1
  declare -F codex_thread_reuse_truthy >/dev/null 2>&1 || return 1
  codex_thread_reuse_truthy "${CODEX_THREAD_REUSE_ENABLED:-false}"
}

render_conflict_thread_reuse_continuation() {
  local previous_attempt="$1"
  local failure_kind="$2"
  local marker_file="$3"
  local fp_file="$4"
  local continuation_source="$5"
  local continuation_rendered="$6"
  local render_prompt_script="${SUPPORT_SCRIPTS_DIR:-scripts}/render_prompt.sh"
  local marker_count="0"
  local marker_list="(none)"
  local fp_count="0"
  local fp_details="(none)"

  [ -n "${continuation_source}" ] || return 1
  [ -f "${render_prompt_script}" ] || return 1

  if [ -s "${marker_file}" ]; then
    marker_count="$(wc -l < "${marker_file}" | tr -d '[:space:]')"
    marker_list="$(sed 's/^/          - /' "${marker_file}")"
  fi
  if [ -s "${fp_file}" ]; then
    fp_count="$(wc -l < "${fp_file}" | tr -d '[:space:]')"
    fp_details="$(sed 's/^/          - /' "${fp_file}")"
  fi

  PREVIOUS_ATTEMPT_NUMBER="${previous_attempt}" \
    MAX_ATTEMPTS="${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}" \
    PREVIOUS_ATTEMPT_FAILURE_KIND="${failure_kind}" \
    MARKER_VIOLATION_COUNT="${marker_count}" \
    MARKER_VIOLATION_FILES="${marker_list}" \
    FINGERPRINT_VIOLATION_COUNT="${fp_count}" \
    FINGERPRINT_VIOLATION_DETAILS="${fp_details}" \
    SERENA_TOOL_HINTS_RESOLVER="${RESOLVER_SERENA_TOOL_HINTS:-}" \
    bash "${render_prompt_script}" "${continuation_source}" > "${continuation_rendered}"
}

resolve_ledger_substate_helper() {
  local candidate
  for candidate in \
    "${SUPPORT_SCRIPTS_DIR:-scripts}/ledger_emit_substate.sh" \
    ".codex-workflow-src/scripts/ledger_emit_substate.sh" \
    "scripts/ledger_emit_substate.sh"; do
    if [ -f "${candidate}" ]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

LEDGER_SUBSTATE_HELPER="$(resolve_ledger_substate_helper || true)"

emit_conflict_resolver_substate() {
  local event_or_substate="$1"
  local attempt_number="$2"
  local args=()

  [ -f "${LEDGER_SUBSTATE_HELPER:-}" ] || return 0

  args=(
    --run-id "${GITHUB_RUN_ID:-}"
    --workflow "review_autofix"
    --phase "review_conflict_resolve"
    --mode "resolver"
    --attempt "${attempt_number}"
    --model "${MODEL_EDITOR:-}"
    --pr-number "${PR_NUMBER:-}"
    --actor "${GITHUB_ACTOR:-codex-bot}"
    --repo-root "$(pwd)"
  )
  case "${event_or_substate}" in
    codex_stall_observed|codex_stall_killed)
      args+=(--event-type "${event_or_substate}")
      ;;
    *)
      args+=(--substate "${event_or_substate}")
      ;;
  esac

  bash "${LEDGER_SUBSTATE_HELPER}" "${args[@]}" || true
}

read_codex_stall_guard_state() {
  local status_file="$1"
  local state=""

  [ -s "${status_file}" ] || return 1
  state="$(sed -n 's/^state=//p' "${status_file}" | head -n 1)"
  case "${state}" in
    observed|killed)
      printf '%s\n' "${state}"
      return 0
      ;;
  esac

  return 1
}

# Source gh_helpers.sh for sanitize_codex_prompt_file (and the broader
# gh_retry / rate-limit helpers if they are needed later in this
# script). Best-effort: a missing helpers file leaves the helper
# undefined, and the caller block below guards against that.
if [ -f "${SUPPORT_SCRIPTS_DIR:-scripts}/gh_helpers.sh" ]; then
  # shellcheck source=gh_helpers.sh
  source "${SUPPORT_SCRIPTS_DIR:-scripts}/gh_helpers.sh" 2>/dev/null || true
fi

if [ -f "${SUPPORT_SCRIPTS_DIR:-scripts}/semble_helpers.sh" ]; then
  # shellcheck source=/dev/null
  source "${SUPPORT_SCRIPTS_DIR:-scripts}/semble_helpers.sh"
fi

# Re-derive runtime paths set in the prepare step. Shell variables
# do not cross step boundaries; RUNTIME_DIR itself is in
# $GITHUB_ENV so it survives, and both paths are deterministic
# functions of RUNTIME_DIR.
PRE_RESOLVER_STATE_FILE="${RUNTIME_DIR}/pre_resolver_state.tsv"
CONFLICTED_PATHS_FILE="${RUNTIME_DIR}/conflicted_paths.txt"
RESOLVER_ALLOWLIST_FILE="${RUNTIME_DIR}/resolver_unmerged_allowlist.txt"
CONFLICT_RESOLVER_SEMBLE_QUERY_FILE="${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE:-${RUNTIME_DIR}/conflict_resolver_semble_query.txt}"
RESOLVER_SERENA_TOOL_HINTS="$({
  if [ "${SERENA_AVAILABLE:-false}" = "true" ]; then
    printf '%s\n' \
      'Resolver Serena hints:' \
      '- Serena MCP is available in this run. Prefer Serena read/navigation tools when they materially reduce shell reads while resolving a conflict (for example: activate_project, get_symbols_overview, find_symbol, find_referencing_symbols, search_for_pattern).' \
      '- Use Serena for lookup/navigation only; keep repository writes in the normal apply_patch/shell paths rather than a broad symbol-write workflow.'
  fi
}; )"

_RESOLVER_DISPATCH_FIRED=0
_dispatch_integration_judge_now() {
  [ "${_RESOLVER_DISPATCH_FIRED}" -eq 1 ] && return 0
  _RESOLVER_DISPATCH_FIRED=1

  if [ "${IS_INTEGRATION_SYNC:-false}" != "true" ]; then
    return 0
  fi
  if [ ! -f "${ORCHESTRATE_FORCE_TICK_HELPER}" ]; then
    echo "::warning::Skipping immediate orchestrator-poll dispatch: ${ORCHESTRATE_FORCE_TICK_HELPER} is unavailable. Cron tick will pick up the integration-sync stall within 5 min."
    return 0
  fi

  local dispatch_token="${GH_PAT:-${GH_TOKEN:-}}"
  local repo_slug="${GITHUB_REPOSITORY:-}"

  if ! [[ "${repo_slug}" =~ ^[^/]+/[^/]+$ ]]; then
    echo "::warning::Skipping immediate orchestrator-poll dispatch: GITHUB_REPOSITORY is missing or invalid (${repo_slug:-<unset>}). Cron tick will pick up the integration-sync stall within 5 min."
    return 0
  fi

  GH_PAT="${dispatch_token}" \
  GH_TOKEN="${dispatch_token}" \
  GITHUB_REPOSITORY="${repo_slug}" \
  bash "${ORCHESTRATE_FORCE_TICK_HELPER}" \
    --repo "${repo_slug}" \
    --issue "${PR_NUMBER:-}" \
    --reason "resolver-failed" \
    --source-workflow "review_conflict_resolve" \
    --run-id "${GITHUB_RUN_ID:-}" || echo "::warning::Immediate orchestrator-poll dispatch helper failed; cron tick will pick up the integration-sync stall within 5 min."
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
# CONFLICT_RESOLVER_REASONING_EFFORT (workflow env, defaults "high"
# repo-wide) overrides config.toml unconditionally before the resolver
# runs. PR #2058 / run 25300219172 originally hit this with override=none.
# Default was lowered from "xhigh" to "high" after runs 25627236793 and
# 25627316961 hit a hung-thinking failure mode at "xhigh" — see the
# comment block on review_autofix.yml's CONFLICT_RESOLVER_REASONING_EFFORT
# env var for the full rationale.
#
# Override config.toml here using CONFLICT_RESOLVER_REASONING_EFFORT
# (set by the workflow step's env, defaults to "high"). Also ensure
# model_reasoning_summary = "auto" is present — same diagnostic +
# anti-empty-stdout rationale as the editor step.
_resolver_reasoning_effort="${CONFLICT_RESOLVER_REASONING_EFFORT:-high}"
# Validate against known reasoning levels before interpolating into sed —
# CONFLICT_RESOLVER_REASONING_EFFORT comes from a repo var
# (vars.THINKING_LEVEL_CONFLICT_RESOLVER) so an unexpected value would
# corrupt the TOML or break sed under set -e. Mirrors the pattern used
# in scripts/review_consolidate.sh for REVIEW_CONSOLIDATOR_REASONING.
# Allowed levels match README.md ("Thinking levels"): xhigh|high|medium|none.
case "${_resolver_reasoning_effort}" in
  xhigh|high|medium|none) ;;
  *)
    echo "::warning::Invalid CONFLICT_RESOLVER_REASONING_EFFORT='${_resolver_reasoning_effort}'; falling back to high."
    _resolver_reasoning_effort="high"
    ;;
esac
_codex_config="${HOME}/.codex/config.toml"

# _apply_resolver_reasoning_effort <level>
#
# Rewrite ~/.codex/config.toml so the next codex invocation runs at
# <level> (one of xhigh|high|medium|none) with
# model_reasoning_summary = "auto" (the anti-empty-stdout safeguard).
#
# Extracted from the once-per-step prelude so the retry loop can
# downgrade the level after a per-attempt-timer kill — see
# _next_lower_reasoning_effort below and the timeout step-down
# block inside the resolver loop. The original PR-#2453 prelude
# rewrote config.toml exactly once; an attempt that timed out at
# `high` would then retry at the same `high`, repeating the
# hung-thinking failure mode rather than giving the next attempt
# a real chance to finish under the per-attempt budget.
#
# Fail-open: sed/grep failure (permissions, unexpected file shape,
# missing GNU sed) surfaces a ::warning:: rather than aborting the
# resolver step under set -e. Robust grep/sed patterns tolerate
# whitespace and quoting variants so a non-canonical config is
# still updated rather than silently no-op'd, which would
# re-introduce the empty-stdout failure mode this fix prevents.
# Post-edit verification reads the file back and emits a warning
# if either expected line is missing.
_apply_resolver_reasoning_effort()
{
  local _arr_level="${1:?reasoning level required}"
  local _arr_rewrite_ok
  local _arr_verify_ok
  case "${_arr_level}" in
    xhigh|high|medium|none) ;;
    *)
      echo "::warning::_apply_resolver_reasoning_effort: invalid level '${_arr_level}'; falling back to high."
      _arr_level="high"
      ;;
  esac
  if [ ! -f "${_codex_config}" ]; then
    echo "::warning::Codex config ${_codex_config} not found before resolver loop; reasoning effort override (${_arr_level}) skipped."
    return 0
  fi
  _arr_rewrite_ok=1
  {
    if grep -qE '^[[:space:]]*model_reasoning_effort[[:space:]]*=' "${_codex_config}"; then
      sed -i "s|^[[:space:]]*model_reasoning_effort[[:space:]]*=.*|model_reasoning_effort = \"${_arr_level}\"|" "${_codex_config}"
    else
      printf '\nmodel_reasoning_effort = "%s"\n' "${_arr_level}" >> "${_codex_config}"
    fi
    if grep -qE '^[[:space:]]*model_reasoning_summary[[:space:]]*=' "${_codex_config}"; then
      sed -i 's|^[[:space:]]*model_reasoning_summary[[:space:]]*=.*|model_reasoning_summary = "auto"|' "${_codex_config}"
    else
      sed -i '/^[[:space:]]*model_reasoning_effort[[:space:]]*=/a model_reasoning_summary = "auto"' "${_codex_config}"
    fi
  } || _arr_rewrite_ok=0

  if [ "${_arr_rewrite_ok}" -eq 0 ]; then
    echo "::warning::Codex config rewrite failed for ${_codex_config}; resolver will run with whatever reasoning the editor step left in place."
    return 0
  fi
  # Independent checks (not elif) so both mismatches surface together
  # if the rewrite somehow produced neither expected line.
  _arr_verify_ok=1
  if ! grep -qE "^model_reasoning_effort = \"${_arr_level}\"$" "${_codex_config}"; then
    echo "::warning::Codex config rewrite did not produce the expected model_reasoning_effort = \"${_arr_level}\" line; resolver may run with stale reasoning."
    _arr_verify_ok=0
  fi
  if ! grep -qE '^model_reasoning_summary = "auto"$' "${_codex_config}"; then
    echo "::warning::Codex config rewrite did not produce the expected model_reasoning_summary = \"auto\" line; the anti-empty-stdout safeguard may be unset."
    _arr_verify_ok=0
  fi
  if [ "${_arr_verify_ok}" -eq 1 ]; then
    echo "Conflict resolver reasoning effort set to ${_arr_level} (model_reasoning_summary=auto)."
  fi
}

# _next_lower_reasoning_effort <level>
#
# Echo the next-lower level on the README.md "Thinking levels"
# ladder: xhigh → high → medium → none. At the floor, echo "none"
# unchanged so the caller can detect "already at floor" via string
# comparison without a separate sentinel. Defensive default for
# unexpected input returns "high" because that is the resolver's
# repo-wide default and matches the validation fallback above.
_next_lower_reasoning_effort()
{
  case "${1:-}" in
    xhigh)  printf '%s\n' high ;;
    high)   printf '%s\n' medium ;;
    medium) printf '%s\n' none ;;
    none)   printf '%s\n' none ;;
    *)      printf '%s\n' high ;;
  esac
}

# _current_reasoning_effort tracks the level used by the most-
# recent codex invocation. It starts at the validated env-provided
# value and is mutated by the in-loop step-down block when the
# previous attempt was timeout-killed. The original
# _resolver_reasoning_effort is preserved as the "initial" level
# for log-line provenance.
_current_reasoning_effort="${_resolver_reasoning_effort}"
_apply_resolver_reasoning_effort "${_current_reasoning_effort}"

RESOLVER_ATTEMPT_BASE_MISSING_FILE="${RUNTIME_DIR}/resolver_attempt_base_missing.txt"
RESOLVER_RETRY_PROMPT_FILE="${RUNTIME_DIR}/resolver_retry_prompt.txt"
RESOLVER_MARKER_VIOLATIONS_FILE="${RUNTIME_DIR}/resolver_marker_violations.txt"
RESOLVER_FP_VIOLATIONS_FILE="${RUNTIME_DIR}/resolver_fp_violations.txt"
RESOLVER_FP_VIOLATIONS_PREV_FILE="${RUNTIME_DIR}/resolver_fp_violations_prev.txt"
RESOLVER_FP_VERIFIER_OUTPUT_FILE="${RUNTIME_DIR}/resolver_fp_verifier_output.txt"
RESOLVER_FP_BASELINE_STATE_FILE="${RUNTIME_DIR}/resolver_fp_baseline_state.json"
RESOLVER_RETRY_STATE_ARTIFACT_FILE="${RUNTIME_DIR}/resolver_retry_state_artifact.json"

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
# the legacy editor default still hit the documented empty-stdout
# failure mode on this trivial fixture (3 attempts, all reading the file
# then exiting with 0 bytes on stdout, no apply_patch invoked, retry
# loop bails). Kept as defense-in-depth on the default editor model.
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
      echo "Smoke fixture: applied deterministic resolution to ${_smoke_canary} before codex resolver loop (kept HEAD-side run_id: line, dropped origin/main alt- bait; mirrors PR #2095 override directive). Empty-stdout failure mode on this fixture (documented under the legacy editor default, openai/codex#11151) would otherwise exhaust MAX_ATTEMPTS without committing."
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

_capture_fingerprints_baseline()
{
  rm -f "${RESOLVER_FP_BASELINE_STATE_FILE}" 2>/dev/null || true
  if [ "${IS_INTEGRATION_SYNC:-false}" != "true" ]; then
    return 0
  fi
  if [ -z "${INTEGRATION_FINGERPRINTS_FILE:-}" ] || [ ! -f "${INTEGRATION_FINGERPRINTS_FILE}" ]; then
    return 0
  fi
  if [ ! -f "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" ]; then
    return 0
  fi
  _fp_size="$(wc -c < "${INTEGRATION_FINGERPRINTS_FILE}" 2>/dev/null || echo 0)"
  if [ "${_fp_size}" -le 2 ]; then
    return 0
  fi
  local _baseline_capture_exit=0
  INTEGRATION_BRANCH_NAME="${INTEGRATION_BRANCH_NAME:-${TARGET_BRANCH:-}}" \
    python3 "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" \
      --baseline-fingerprints-state "${RESOLVER_FP_BASELINE_STATE_FILE}" \
      "${INTEGRATION_FINGERPRINTS_FILE}" || _baseline_capture_exit=$?
  if [ "${_baseline_capture_exit}" -ne 0 ] || [ ! -s "${RESOLVER_FP_BASELINE_STATE_FILE}" ]; then
    rm -f "${RESOLVER_FP_BASELINE_STATE_FILE}" 2>/dev/null || true
    if [ "${_baseline_capture_exit}" -ne 0 ]; then
      echo "::warning::baseline capture failed (exit ${_baseline_capture_exit}); falling back to absolute fingerprint verification."
    else
      echo "::warning::baseline capture unavailable; falling back to absolute fingerprint verification."
    fi
  fi
}

_capture_fingerprints_baseline

RESOLVER_FP_VERIFICATION_TIER="strict"
RESOLVER_FP_VERIFICATION_TIER_REASON="default_strict"

_select_fingerprint_verification_tier()
{
  RESOLVER_FP_VERIFICATION_TIER="strict"
  RESOLVER_FP_VERIFICATION_TIER_REASON="default_strict"
  if [ "${IS_INTEGRATION_SYNC:-false}" != "true" ]; then
    return 0
  fi
  if [ ! -f "${PR_PAYLOAD_FILE:-/nonexistent}" ]; then
    RESOLVER_FP_VERIFICATION_TIER_REASON="missing_pr_payload"
    return 0
  fi

  local _tier_json
  if ! _tier_json="$(RESOLVER_ESCAPE_THRESHOLD_N="${RESOLVER_ESCAPE_THRESHOLD_N:-5}" PR_PAYLOAD_FILE="${PR_PAYLOAD_FILE}" python3 - <<'PY'
from __future__ import annotations

import json
import os
import re


RETRY_STATE_BLOCK_PATTERN = re.compile(
    r"<!-- AUTOFIX_RESOLVER_RETRY_STATE_V1\n(.*?)\n-->",
    flags=re.S,
)


def _parse_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _parse_nonnegative_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default
    return parsed if parsed >= 0 else default


def _extract_retry_state(body: str) -> dict[str, object] | None:
    normalized = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    matches = RETRY_STATE_BLOCK_PATTERN.findall(normalized)
    for raw in reversed(matches):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _select_tier(count: int, threshold: int) -> str:
    if count >= threshold * 3:
        return "warn_only"
    if count >= threshold * 2:
        return "count_only"
    if count >= threshold:
        return "ratio"
    return "strict"


threshold = _parse_positive_int(os.environ.get("RESOLVER_ESCAPE_THRESHOLD_N"), 5)
payload_path = os.environ.get("PR_PAYLOAD_FILE", "")
try:
    with open(payload_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
except Exception:
    print(json.dumps({"tier": "strict", "reason": "unreadable_pr_payload"}, ensure_ascii=True))
    raise SystemExit(0)

if not isinstance(payload, dict):
    print(json.dumps({"tier": "strict", "reason": "malformed_pr_payload"}, ensure_ascii=True))
    raise SystemExit(0)

head = payload.get("head") or {}
head_sha = str(head.get("sha", "") or "").strip()
if not head_sha:
    print(json.dumps({"tier": "strict", "reason": "missing_head_sha"}, ensure_ascii=True))
    raise SystemExit(0)

retry_state = _extract_retry_state(str(payload.get("body", "") or ""))
if not isinstance(retry_state, dict):
    print(json.dumps({"tier": "strict", "reason": "no_retry_state"}, ensure_ascii=True))
    raise SystemExit(0)

retry_state_head_sha = str(retry_state.get("head_sha", "") or "").strip()
if retry_state_head_sha != head_sha:
    print(json.dumps({"tier": "strict", "reason": "retry_state_head_sha_mismatch"}, ensure_ascii=True))
    raise SystemExit(0)

count = _parse_nonnegative_int(retry_state.get("consecutive_failure_count"), 0)
print(
    json.dumps(
        {
            "tier": _select_tier(count, threshold),
            "reason": f"retry_state_count={count} threshold={threshold}",
        },
        ensure_ascii=True,
    )
)
PY
)"; then
    echo "::warning::Failed to select fingerprint verification tier from retry state; defaulting to strict."
    RESOLVER_FP_VERIFICATION_TIER="strict"
    RESOLVER_FP_VERIFICATION_TIER_REASON="selection_error"
    return 0
  fi

  RESOLVER_FP_VERIFICATION_TIER="$(printf '%s' "${_tier_json}" | jq -r '.tier // "strict"' 2>/dev/null || echo strict)"
  RESOLVER_FP_VERIFICATION_TIER_REASON="$(printf '%s' "${_tier_json}" | jq -r '.reason // "default_strict"' 2>/dev/null || echo default_strict)"
  case "${RESOLVER_FP_VERIFICATION_TIER}" in
    strict|ratio|count_only|warn_only) ;;
    *)
      RESOLVER_FP_VERIFICATION_TIER="strict"
      RESOLVER_FP_VERIFICATION_TIER_REASON="invalid_tier_fallback"
      ;;
  esac
}

_select_fingerprint_verification_tier
echo "Integration fingerprint verification tier selected: ${RESOLVER_FP_VERIFICATION_TIER} (${RESOLVER_FP_VERIFICATION_TIER_REASON})."

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
# Three prelude variants are selected by ${_failure_kind}:
#   - "validation" (default): the standard prelude listing the
#     previous attempt's residual markers + fingerprint violations.
#   - "timeout": a separate prelude (missing-template fail-open
#     same as below) telling the model the previous attempt was
#     killed by the per-attempt timer before completing — any
#     partial apply_patch calls were discarded by the working-tree
#     restore — and to be DECISIVE this time, picking the smallest
#     convergent resolution and calling apply_patch early instead
#     of deliberating.  Without this branch the retry log +
#     reflexion text print "(prev markers=0, prev fingerprint_
#     violations=0)" because soft-validation never ran on a
#     timed-out attempt, falsely telling the model it succeeded.
#   - "exec_error": codex itself exited non-zero (config / auth /
#     model error — distinguished from "timeout" by the captured
#     `timeout` exit code not being 124 or 137).  No reflexion
#     data is available, so neither prelude's framing is honest;
#     fall through to the same "retry with original prompt
#     verbatim" path used when the prelude template is missing.
#
# Fail-open: if either prelude template is missing (older script_ref
# on a consumer repo), retry with the original prompt verbatim
# and emit a warning.  The soft retry itself is the core win; the
# reflexion content is an optimisation on top of it.
# _build_retry_prompt sets ${_retry_prompt_outcome} to one of:
#   - "validation-prelude": the standard violations-listing prelude
#     was rendered.
#   - "timeout-prelude": the timeout-aware prelude was rendered.
#   - "verbatim:exec_error": exec_error path; the original prompt was
#     copied verbatim with no prelude.
#   - "verbatim:fallback": prelude template missing or
#     IS_INTEGRATION_SYNC=false; the original prompt was copied
#     verbatim with no prelude.
# The caller reads this to log what was actually rendered, rather
# than inferring it from _prev_attempt_failure_kind alone (which
# would lie when the prelude template is missing on a consumer-repo
# script_ref pin).
_retry_prompt_outcome=""

_build_retry_prompt() {
  local _prev_attempt="$1"
  local _marker_file="$2"
  local _fp_file="$3"
  local _failure_kind="${4:-validation}"
  if [ "${_failure_kind}" = "exec_error" ]; then
    cp -a "${CONFLICT_RESOLVER_PROMPT_FILE}" "${RESOLVER_RETRY_PROMPT_FILE}"
    _retry_prompt_outcome="verbatim:exec_error"
    return 0
  fi
  local _prelude_basename="integration-sync-conflict-resolver-retry-prelude.txt"
  if [ "${_failure_kind}" = "timeout" ]; then
    _prelude_basename="integration-sync-conflict-resolver-retry-timeout-prelude.txt"
  fi
  local _prelude_tpl="${SUPPORT_PROMPTS_DIR:-prompts}/${_prelude_basename}"
  if [ "${IS_INTEGRATION_SYNC:-false}" != "true" ] || [ ! -f "${_prelude_tpl}" ]; then
    if [ "${IS_INTEGRATION_SYNC:-false}" = "true" ]; then
      echo "::warning::Resolver retry prelude template missing at ${_prelude_tpl}; retrying with original prompt verbatim (no reflexion)."
    fi
    cp -a "${CONFLICT_RESOLVER_PROMPT_FILE}" "${RESOLVER_RETRY_PROMPT_FILE}"
    _retry_prompt_outcome="verbatim:fallback"
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
  # metacharacters do not need quoting gymnastics.  The set of
  # `{{KEY}}` placeholders to substitute is auto-derived from the
  # template body itself rather than maintained as a hardcoded list
  # — that way a future template that adds a new placeholder
  # (e.g. `{{INTEGRATION_BRANCH_NAME}}`) does not silently render
  # the literal `{{...}}` text into the prelude on a stable
  # script_ref pin.  Keys are matched against `[A-Za-z_][A-Za-z0-9_]*`
  # with optional interior whitespace (`{{ KEY }}` / `{{key}}` /
  # `{{ key }}` all match), so future templates authored with a
  # different convention still substitute cleanly.  Env-var lookup
  # uses the uppercased key, matching the convention every existing
  # caller binds against; any key whose corresponding env var is
  # unset is replaced with the empty string.
  PRELUDE_TPL="${_prelude_tpl}" \
    ORIGINAL_PROMPT_FILE="${CONFLICT_RESOLVER_PROMPT_FILE}" \
    PREVIOUS_ATTEMPT_NUMBER="${_prev_attempt}" \
    MAX_ATTEMPTS="${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}" \
    MARKER_VIOLATION_COUNT="${_marker_count}" \
    MARKER_VIOLATION_FILES="${_marker_list}" \
    FINGERPRINT_VIOLATION_COUNT="${_fp_count}" \
    FINGERPRINT_VIOLATION_DETAILS="${_fp_details}" \
    SERENA_TOOL_HINTS_RESOLVER="${RESOLVER_SERENA_TOOL_HINTS:-}" \
    PER_ATTEMPT_TIMEOUT_SECS="${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS:-3000}" \
    python3 -c "import os,re,sys; tpl=open(os.environ['PRELUDE_TPL'],encoding='utf-8',errors='replace').read(); tpl=re.sub(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}', lambda m: os.environ.get(m.group(1).upper(), ''), tpl); orig=open(os.environ['ORIGINAL_PROMPT_FILE'],encoding='utf-8',errors='replace').read(); sys.stdout.write(tpl + orig)" \
    > "${RESOLVER_RETRY_PROMPT_FILE}"
  if [ "${_failure_kind}" = "timeout" ]; then
    _retry_prompt_outcome="timeout-prelude"
  else
    _retry_prompt_outcome="validation-prelude"
  fi
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
  local _verifier_args=()
  if [ -s "${RESOLVER_FP_BASELINE_STATE_FILE:-/nonexistent}" ]; then
    _verifier_args+=(
      --compare-against-baseline "${RESOLVER_FP_BASELINE_STATE_FILE}"
    )
  fi
  if [ "${RESOLVER_FP_VERIFICATION_TIER:-strict}" != "strict" ]; then
    _verifier_args+=(
      --verification-tier "${RESOLVER_FP_VERIFICATION_TIER}"
    )
  fi
  INTEGRATION_BRANCH_NAME="${INTEGRATION_BRANCH_NAME:-${TARGET_BRANCH:-}}" \
    python3 "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" \
      "${_verifier_args[@]}" \
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

# _build_resolver_retry_state_artifact: emit a machine-readable JSON
# payload describing the current fingerprint failure set plus the
# updated AUTOFIX_RESOLVER_RETRY_STATE_V1 PR-body block.  The helper
# deliberately reuses the verifier module's baseline/dedup logic so the
# state block is keyed off the same normalized fp_key set that
# compare-mode verification actually enforced.
_build_resolver_retry_state_artifact()
{
  python3 - <<'PY'
# AUTOFIX_RESOLVER_RETRY_STATE_PY_BEGIN
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any


RETRY_STATE_BLOCK_PATTERN = re.compile(
    r"<!-- AUTOFIX_RESOLVER_RETRY_STATE_V1\n(.*?)\n-->",
    flags=re.S,
)
ESCALATION_COMMENT_MARKER = "<!-- AUTOFIX_RESOLVER_ESCALATED_V1 -->"
VERIFICATION_TIER_LADDER = ("strict", "ratio", "count_only", "warn_only")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _parse_nonnegative_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default
    return parsed if parsed >= 0 else default


def select_verification_tier(consecutive_failure_count: object, threshold: int) -> str:
    threshold = max(1, _parse_positive_int(threshold, 5))
    count = _parse_nonnegative_int(consecutive_failure_count, 0)
    if count >= threshold * 3:
        return "warn_only"
    if count >= threshold * 2:
        return "count_only"
    if count >= threshold:
        return "ratio"
    return "strict"


def select_verification_tier_from_pr_payload(pr_payload: dict[str, Any], threshold: int) -> dict[str, Any]:
    head = (pr_payload or {}).get("head") or {}
    head_sha = str(head.get("sha", "") or "").strip()
    if not head_sha:
        return {"tier": "strict", "reason": "missing_head_sha", "consecutive_failure_count": 0}
    retry_state = extract_retry_state_from_body(str((pr_payload or {}).get("body", "") or ""))
    if not isinstance(retry_state, dict):
        return {"tier": "strict", "reason": "no_retry_state", "consecutive_failure_count": 0}
    retry_state_head_sha = str(retry_state.get("head_sha", "") or "").strip()
    if retry_state_head_sha != head_sha:
        return {"tier": "strict", "reason": "retry_state_head_sha_mismatch", "consecutive_failure_count": 0}
    consecutive_failure_count = _parse_nonnegative_int(retry_state.get("consecutive_failure_count"), 0)
    return {
        "tier": select_verification_tier(consecutive_failure_count, threshold),
        "reason": f"retry_state_count={consecutive_failure_count}",
        "consecutive_failure_count": consecutive_failure_count,
    }


def build_tier_downgrade_marker(
    previous_tier: str,
    current_tier: str,
    consecutive_failure_count: int,
    threshold: int,
) -> str:
    if previous_tier not in VERIFICATION_TIER_LADDER or current_tier not in VERIFICATION_TIER_LADDER:
        return ""
    if VERIFICATION_TIER_LADDER.index(current_tier) <= VERIFICATION_TIER_LADDER.index(previous_tier):
        return ""
    return (
        f"::warning::FINGERPRINT_TIER_DOWNGRADED_V1 from={previous_tier} to={current_tier} "
        f"reason=consecutive_failure_count_{consecutive_failure_count}_threshold_{threshold}"
    )


def _load_json_path(path: str, expected_type: type, default: Any) -> tuple[Any, str | None]:
    if not path:
        return default, None
    if not os.path.isfile(path):
        return default, f"JSON file missing: {path}"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - fail-open for state write path
        return default, f"JSON file unreadable ({path}): {exc}"
    if not isinstance(data, expected_type):
        return default, f"JSON file at {path} had unexpected type {type(data).__name__}; expected {expected_type.__name__}"
    return data, None


def load_verifier_module(support_scripts_dir: str):
    script_path = os.path.join(support_scripts_dir or "scripts", "verify_integration_fingerprints.py")
    if not os.path.isfile(script_path):
        raise FileNotFoundError(script_path)
    spec = importlib.util.spec_from_file_location(
        "resolver_retry_state_verify_integration_fingerprints",
        script_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load module spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_retry_state_from_body(body: str) -> dict[str, Any] | None:
    if not isinstance(body, str) or not body:
        return None
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    matches = RETRY_STATE_BLOCK_PATTERN.findall(normalized)
    for raw in reversed(matches):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def render_retry_state_block(state: dict[str, Any]) -> str:
    return (
        "<!-- AUTOFIX_RESOLVER_RETRY_STATE_V1\n"
        + json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n-->"
    )


def upsert_retry_state_block(body: str, state: dict[str, Any]) -> str:
    normalized = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = RETRY_STATE_BLOCK_PATTERN.sub("", normalized)
    block = render_retry_state_block(state)
    if cleaned.strip():
        return cleaned.rstrip() + "\n\n" + block + "\n"
    return block + "\n"


def _fp_key_json(fp_key: list[str]) -> str:
    return json.dumps(fp_key, separators=(",", ":"), ensure_ascii=True)


def _sorted_failure_entries(entries_by_key: dict[tuple[str, ...], dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries_by_key.values(),
        key=lambda item: (
            tuple(item.get("fp_key", [])),
            str(item.get("kind", "")),
            str(item.get("path", "")),
            str(item.get("issue", "")),
        ),
    )


def compute_failure_sets(
    fingerprints: dict[str, Any],
    baseline_state: dict[str, Any],
    verifier_module: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    baseline_index = verifier_module._baseline_satisfied_index(baseline_state)
    cross_issue_exact_drops, _cross_issue_exact_warnings = verifier_module._cross_issue_exact_conflict_drops(fingerprints)
    file_cache: dict[str, tuple[str | None, str | None]] = {}
    exists_cache: dict[str, tuple[bool, str | None]] = {}
    regressed_by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    pre_existing_by_key: dict[tuple[str, ...], dict[str, Any]] = {}

    for issue_key, entry in sorted(fingerprints.items()):
        if not isinstance(entry, dict):
            continue
        issue_num = entry.get("issue", issue_key)
        pr_num = entry.get("pr", "?")
        must_contain, must_not_contain, _shared_keys, _substring_drops, _exact_conflict_drops = verifier_module._dedup_issue_patterns(
            issue_key,
            entry,
            cross_issue_exact_drops,
        )
        must_not_exist = entry.get("must_not_exist", []) or []
        for kind, fps in (
            ("must_contain", must_contain),
            ("must_not_contain", must_not_contain),
            ("must_not_exist", must_not_exist),
        ):
            for fp in fps:
                state = verifier_module._evaluate_fp_state(
                    fp,
                    kind,
                    issue_num,
                    pr_num,
                    file_cache,
                    exists_cache,
                    ref=None,
                )
                if state is None or state.get("satisfied"):
                    continue
                fp_key = tuple(state.get("fp_key") or ())
                if not fp_key:
                    continue
                item = {
                    "issue": issue_num,
                    "pr": pr_num,
                    "kind": kind,
                    "path": state.get("path", ""),
                    "fp_key": list(fp_key),
                }
                baseline_satisfied = baseline_index.get((str(issue_key), kind, fp_key))
                if baseline_satisfied is False:
                    pre_existing_by_key.setdefault(fp_key, item)
                else:
                    regressed_by_key.setdefault(fp_key, item)

    return _sorted_failure_entries(regressed_by_key), _sorted_failure_entries(pre_existing_by_key)


def find_existing_escalation_comment_id(pr_issue_comments: list[dict[str, Any]]) -> int | None:
    latest_id: int | None = None
    for comment in pr_issue_comments or []:
        if not isinstance(comment, dict):
            continue
        if ESCALATION_COMMENT_MARKER not in str(comment.get("body", "")):
            continue
        try:
            comment_id = int(comment.get("id"))
        except Exception:
            continue
        if latest_id is None or comment_id > latest_id:
            latest_id = comment_id
    return latest_id


def build_resolver_escalation_comment(
    *,
    pr_number: str,
    head_sha: str,
    failure_signature_sha256: str,
    consecutive_failure_count: int,
    threshold: int,
    escalation_threshold: int,
    verification_tier: str,
    regressed_by_resolver: list[dict[str, Any]],
    pre_existing_drift: list[dict[str, Any]],
    run_url: str,
    max_items: int,
) -> str:
    lines = [
        ESCALATION_COMMENT_MARKER,
        "## ⚠️ Integration resolver escalated",
        "",
        (
            f"The integration-sync conflict resolver hit the escape threshold on PR #{pr_number} "
            f"for head `{head_sha[:12]}`. The orchestrator poller will stop re-dispatching "
            "automated resolver attempts until the PR head changes."
        ),
        "",
        (
            f"- Consecutive identical-signature failures: {consecutive_failure_count} "
            f"(tier step threshold {threshold}; escalation threshold {escalation_threshold})"
        ),
        f"- Final verification tier: {verification_tier}",
        f"- Failure signature: `{failure_signature_sha256}`",
        f"- Regressed fingerprints: {len(regressed_by_resolver)}",
        f"- Pre-existing drift: {len(pre_existing_drift)}",
    ]
    if regressed_by_resolver:
        lines.extend(["", "Sample regressed fp_keys:"])
        for item in regressed_by_resolver[:max_items]:
            lines.append(f"- `{_fp_key_json(item['fp_key'])}`")
    if pre_existing_drift:
        lines.extend(["", "Sample pre-existing drift fp_keys:"])
        for item in pre_existing_drift[:max_items]:
            lines.append(f"- `{_fp_key_json(item['fp_key'])}`")
    if run_url:
        lines.extend(["", f"Run: {run_url}"])
    return "\n".join(lines)


def build_resolver_retry_state_artifact(
    *,
    pr_payload: dict[str, Any],
    pr_issue_comments: list[dict[str, Any]],
    fingerprints: dict[str, Any],
    baseline_state: dict[str, Any],
    threshold: int,
    repository: str,
    pr_number: str,
    run_url: str,
    verifier_module: Any,
    max_items: int = 10,
) -> dict[str, Any]:
    body = str(pr_payload.get("body", "") or "")
    head = pr_payload.get("head") or {}
    head_sha = str(head.get("sha", "") or "").strip()
    if not head_sha:
        return {"ok": False, "reason": "missing PR head SHA in PR_PAYLOAD_FILE"}

    regressed_by_resolver, pre_existing_drift = compute_failure_sets(
        fingerprints,
        baseline_state,
        verifier_module,
    )
    if not regressed_by_resolver and not pre_existing_drift:
        return {"ok": False, "reason": "no fingerprint failures detected for retry-state persistence"}

    signature_members = sorted(
        {
            _fp_key_json(item["fp_key"])
            for item in regressed_by_resolver + pre_existing_drift
        }
    )
    failure_signature_sha256 = hashlib.sha256(
        json.dumps(signature_members, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()

    previous_state = extract_retry_state_from_body(body) or {}
    previous_head_sha = str(previous_state.get("head_sha", "") or "")
    previous_signature = str(
        previous_state.get("failure_signature_sha256", previous_state.get("last_failure_signature", "")) or ""
    )
    previous_count = _parse_nonnegative_int(previous_state.get("consecutive_failure_count"), 0)

    if previous_head_sha == head_sha and previous_signature == failure_signature_sha256:
        consecutive_failure_count = previous_count + 1
    else:
        consecutive_failure_count = 1

    threshold = max(1, _parse_positive_int(threshold, 5))
    escalation_threshold = threshold * len(VERIFICATION_TIER_LADDER)
    max_items = max(1, _parse_positive_int(max_items, 10))
    now_iso = _utc_now_iso()
    previous_verification_tier = "strict"
    if previous_head_sha == head_sha and previous_signature == failure_signature_sha256:
        previous_verification_tier = select_verification_tier(previous_count, threshold)
    verification_tier = select_verification_tier(consecutive_failure_count, threshold)
    tier_downgrade_marker = build_tier_downgrade_marker(
        previous_verification_tier,
        verification_tier,
        consecutive_failure_count,
        threshold,
    )
    escalated = consecutive_failure_count >= escalation_threshold
    if (
        escalated
        and previous_head_sha == head_sha
        and previous_signature == failure_signature_sha256
        and bool(previous_state.get("escalated"))
        and str(previous_state.get("escalated_at", "") or "")
    ):
        escalated_at = str(previous_state.get("escalated_at"))
    elif escalated:
        escalated_at = now_iso
    else:
        escalated_at = ""

    retry_state = {
        "schema_version": 1,
        "head_sha": head_sha,
        "failure_signature_sha256": failure_signature_sha256,
        "last_failure_signature": failure_signature_sha256,
        "consecutive_failure_count": consecutive_failure_count,
        "threshold": threshold,
        "escalation_threshold": escalation_threshold,
        "verification_tier": verification_tier,
        "regressed_by_resolver_count": len(regressed_by_resolver),
        "pre_existing_drift_count": len(pre_existing_drift),
        "last_regressed_by_resolver": regressed_by_resolver[:max_items],
        "last_pre_existing_drift": pre_existing_drift[:max_items],
        "escalated": escalated,
        "escalated_at": escalated_at,
        "updated_at": now_iso,
    }

    return {
        "ok": True,
        "repository": repository,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "failure_signature_sha256": failure_signature_sha256,
        "consecutive_failure_count": consecutive_failure_count,
        "verification_tier": verification_tier,
        "tier_downgrade_marker": tier_downgrade_marker,
        "escalated": escalated,
        "existing_escalation_comment_id": find_existing_escalation_comment_id(pr_issue_comments),
        "retry_state": retry_state,
        "body": upsert_retry_state_block(body, retry_state),
        "summary_comment_body": build_resolver_escalation_comment(
            pr_number=pr_number,
            head_sha=head_sha,
            failure_signature_sha256=failure_signature_sha256,
            consecutive_failure_count=consecutive_failure_count,
            threshold=threshold,
            escalation_threshold=escalation_threshold,
            verification_tier=verification_tier,
            regressed_by_resolver=regressed_by_resolver,
            pre_existing_drift=pre_existing_drift,
            run_url=run_url,
            max_items=max_items,
        ) if escalated else "",
    }


def main() -> int:
    support_scripts_dir = os.environ.get("SUPPORT_SCRIPTS_DIR", "scripts")
    verifier_module = None
    try:
        verifier_module = load_verifier_module(support_scripts_dir)
    except Exception as exc:  # noqa: BLE001 - fail-open in shell caller
        print(json.dumps({"ok": False, "reason": f"failed to load verifier module: {exc}"}, ensure_ascii=True))
        return 0

    pr_payload, pr_payload_err = _load_json_path(
        os.environ.get("PR_PAYLOAD_FILE", ""),
        dict,
        {},
    )
    if pr_payload_err is not None:
        print(json.dumps({"ok": False, "reason": pr_payload_err}, ensure_ascii=True))
        return 0

    pr_issue_comments, pr_issue_comments_err = _load_json_path(
        os.environ.get("PR_ISSUE_COMMENTS_FILE", ""),
        list,
        [],
    )
    if pr_issue_comments_err is not None and os.environ.get("PR_ISSUE_COMMENTS_FILE", ""):
        print(json.dumps({"ok": False, "reason": pr_issue_comments_err}, ensure_ascii=True))
        return 0

    fingerprints, fingerprints_err = _load_json_path(
        os.environ.get("INTEGRATION_FINGERPRINTS_FILE", ""),
        dict,
        {},
    )
    if fingerprints_err is not None:
        print(json.dumps({"ok": False, "reason": fingerprints_err}, ensure_ascii=True))
        return 0

    baseline_state, baseline_err = _load_json_path(
        os.environ.get("RESOLVER_FP_BASELINE_STATE_FILE", ""),
        dict,
        {},
    )
    if baseline_err is not None:
        print(json.dumps({"ok": False, "reason": baseline_err}, ensure_ascii=True))
        return 0

    run_url = ""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if repository and run_id:
        run_url = f"{server_url}/{repository}/actions/runs/{run_id}"

    result = build_resolver_retry_state_artifact(
        pr_payload=pr_payload,
        pr_issue_comments=pr_issue_comments,
        fingerprints=fingerprints,
        baseline_state=baseline_state,
        threshold=_parse_positive_int(os.environ.get("RESOLVER_ESCAPE_THRESHOLD_N"), 5),
        repository=repository,
        pr_number=os.environ.get("PR_NUMBER", ""),
        run_url=run_url,
        verifier_module=verifier_module,
        max_items=_parse_positive_int(os.environ.get("RESOLVER_RETRY_STATE_MAX_ITEMS"), 10),
    )
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# AUTOFIX_RESOLVER_RETRY_STATE_PY_END
PY
}

_sync_local_pr_body_from_file()
{
  local _body_file="${1:?body file required}"
  local _tmp
  if [ -f "${PR_PAYLOAD_FILE:-/nonexistent}" ]; then
    _tmp="${PR_PAYLOAD_FILE}.tmp"
    jq --rawfile body "${_body_file}" '.body = $body' "${PR_PAYLOAD_FILE}" > "${_tmp}" \
      && mv "${_tmp}" "${PR_PAYLOAD_FILE}" || true
  fi
  if [ -f "${PR_META_FILE:-/nonexistent}" ]; then
    _tmp="${PR_META_FILE}.tmp"
    jq --rawfile body "${_body_file}" '.body = $body' "${PR_META_FILE}" > "${_tmp}" \
      && mv "${_tmp}" "${PR_META_FILE}" || true
  fi
}

_persist_resolver_retry_state_from_current_failure()
{
  if [ "${IS_INTEGRATION_SYNC:-false}" != "true" ]; then
    return 0
  fi
  if [ "${RESOLVER_FP_EXIT:-0}" -ne 1 ] && [ "${RESOLVER_FP_VERIFICATION_TIER:-strict}" != "warn_only" ]; then
    return 0
  fi
  if ! [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]]; then
    echo "::warning::Resolver retry-state persistence skipped: PR_NUMBER is missing or non-numeric."
    return 0
  fi
  if [ -z "${GITHUB_REPOSITORY:-}" ]; then
    echo "::warning::Resolver retry-state persistence skipped: GITHUB_REPOSITORY is unset."
    return 0
  fi
  if [ ! -f "${PR_PAYLOAD_FILE:-/nonexistent}" ]; then
    echo "::warning::Resolver retry-state persistence skipped: PR_PAYLOAD_FILE is missing."
    return 0
  fi
  if [ ! -f "${INTEGRATION_FINGERPRINTS_FILE:-/nonexistent}" ]; then
    echo "::warning::Resolver retry-state persistence skipped: INTEGRATION_FINGERPRINTS_FILE is missing."
    return 0
  fi
  local _retry_state_baseline_file="${RESOLVER_FP_BASELINE_STATE_FILE:-}"
  if [ ! -s "${_retry_state_baseline_file:-/nonexistent}" ]; then
    # Baseline capture can fail-open earlier in the resolver loop. In
    # that case we still persist retry state so identical failure sets
    # on the same head SHA count toward the escape threshold; the
    # embedded helper treats the current failures as regressed when no
    # baseline is available.
    echo "::warning::Resolver retry-state persistence continuing without baseline fingerprints state; treating current failures as regressed for retry-state accounting."
    _retry_state_baseline_file=""
  fi
  if [ ! -f "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" ]; then
    echo "::warning::Resolver retry-state persistence skipped: verify_integration_fingerprints.py unavailable."
    return 0
  fi

  if ! RESOLVER_FP_BASELINE_STATE_FILE="${_retry_state_baseline_file}" _build_resolver_retry_state_artifact > "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}.tmp"; then
    echo "::warning::Resolver retry-state artifact builder failed; skipping retry-state persistence."
    rm -f "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}.tmp" 2>/dev/null || true
    return 0
  fi
  mv "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}.tmp" "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}"

  local _artifact_ok _artifact_reason
  _artifact_ok="$(jq -r '.ok // false' "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}" 2>/dev/null || echo false)"
  if [ "${_artifact_ok}" != "true" ]; then
    _artifact_reason="$(jq -r '.reason // "unknown"' "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}" 2>/dev/null || echo unknown)"
    echo "::warning::Resolver retry-state artifact unavailable; skipping persistence (${_artifact_reason})."
    return 0
  fi

  local _body_file _count _signature _verification_tier _tier_downgrade_marker
  _body_file="$(mktemp)"
  jq -r '.body // ""' "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}" > "${_body_file}"
  if [ ! -s "${_body_file}" ]; then
    echo "::warning::Resolver retry-state artifact produced an empty PR body; skipping persistence."
    rm -f "${_body_file}"
    return 0
  fi
  if ! gh_retry gh pr edit "${PR_NUMBER}" --repo "${GITHUB_REPOSITORY}" --body-file "${_body_file}" >/dev/null; then
    echo "::warning::Failed to persist AUTOFIX_RESOLVER_RETRY_STATE_V1 to PR #${PR_NUMBER}; skipping escalation side effects so poller state does not drift from GitHub."
    rm -f "${_body_file}"
    return 0
  fi
  _sync_local_pr_body_from_file "${_body_file}"
  rm -f "${_body_file}"

  _count="$(jq -r '.consecutive_failure_count // 0' "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}" 2>/dev/null || echo 0)"
  _signature="$(jq -r '.failure_signature_sha256 // ""' "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}" 2>/dev/null || echo "")"
  _verification_tier="$(jq -r '.verification_tier // .retry_state.verification_tier // "strict"' "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}" 2>/dev/null || echo strict)"
  echo "Persisted AUTOFIX_RESOLVER_RETRY_STATE_V1 to PR #${PR_NUMBER} (count=${_count}, signature=${_signature}, tier=${_verification_tier})."
  _tier_downgrade_marker="$(jq -r '.tier_downgrade_marker // empty' "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}" 2>/dev/null || echo "")"
  if [ -n "${_tier_downgrade_marker}" ]; then
    echo "${_tier_downgrade_marker}"
  fi

  if [ "$(jq -r '.escalated // false' "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}" 2>/dev/null || echo false)" != "true" ]; then
    return 0
  fi

  echo "RESOLVER_ESCALATED=true" >> "$GITHUB_ENV"

  if [ -f "${SUPPORT_SCRIPTS_DIR:-scripts}/label_helpers.sh" ]; then
    # shellcheck source=/dev/null
    source "${SUPPORT_SCRIPTS_DIR:-scripts}/label_helpers.sh" 2>/dev/null || true
  fi
  if type ensure_label_exists >/dev/null 2>&1; then
    ensure_label_exists "ai:resolver-escalated" "${GITHUB_REPOSITORY}" || true
  fi
  gh_retry gh issue edit "${PR_NUMBER}" --repo "${GITHUB_REPOSITORY}" --add-label "ai:resolver-escalated" >/dev/null 2>&1 \
    || echo "::warning::Failed to apply ai:resolver-escalated to PR #${PR_NUMBER}."

  local _comment_id _comment_file _comment_payload _comment_present=false
  _comment_id="$(jq -r '.existing_escalation_comment_id // empty' "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}" 2>/dev/null || echo "")"
  _comment_file="$(mktemp)"
  jq -r '.summary_comment_body // ""' "${RESOLVER_RETRY_STATE_ARTIFACT_FILE}" > "${_comment_file}"
  if [ -n "${_comment_id}" ] && [[ "${_comment_id}" =~ ^[0-9]+$ ]]; then
    _comment_payload="$(mktemp)"
    jq -n --rawfile body "${_comment_file}" '{body: $body}' > "${_comment_payload}"
    if gh_retry gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/comments/${_comment_id}" --input "${_comment_payload}" >/dev/null 2>&1; then
      _comment_present=true
    else
      echo "::warning::Failed to refresh resolver escalation summary comment #${_comment_id} on PR #${PR_NUMBER}."
    fi
    rm -f "${_comment_payload}"
  elif [ -s "${_comment_file}" ]; then
    _comment_payload="$(mktemp)"
    jq -n --rawfile body "${_comment_file}" '{body: $body}' > "${_comment_payload}"
    if gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" --input "${_comment_payload}" >/dev/null 2>&1; then
      _comment_present=true
    else
      echo "::warning::Failed to post resolver escalation summary comment on PR #${PR_NUMBER}."
    fi
    rm -f "${_comment_payload}"
  fi
  rm -f "${_comment_file}"

  if [ "${_comment_present}" = "true" ]; then
    echo "RESOLVER_ESCALATED=true" >> "$GITHUB_ENV"
  fi
}

# Pre-load the conflicted files into the resolver prompt so the model
# sees the actual conflict-marker bytes without spending a tool call to
# read them. RESOLVER_ALLOWLIST_FILE is the canonical in-scope list (one
# path per line) populated by review_conflict_prepare.sh from
# `git diff --name-only --diff-filter=U` plus any fingerprint-violation
# expansions. Files are processed in source order until the cumulative
# byte budget (TARGETED_FILE_CONTEXT_MAX_BYTES) is exhausted; a file
# that would overflow the remaining budget gets a "read with read tool"
# marker so the model fetches it properly instead of seeing a misleading
# head-truncated copy. Fail-open: any error here continues the resolver
# loop without the targeted-context block.
TARGETED_FILES_CONTEXT_FILE="${RUNTIME_DIR}/targeted_files_context.txt"
CONFLICT_RESOLVER_SEMBLE_CONTEXT_FILE="${RUNTIME_DIR}/conflict_resolver_semble_context.txt"
TARGETED_FILE_CONTEXT_SCRIPT="${SUPPORT_SCRIPTS_DIR:-scripts}/targeted_file_context.py"
: > "${TARGETED_FILES_CONTEXT_FILE}"
if [ -s "${RESOLVER_ALLOWLIST_FILE:-}" ] && [ -f "${TARGETED_FILE_CONTEXT_SCRIPT}" ]; then
  targeted_file_context_args=(
    python3 "${TARGETED_FILE_CONTEXT_SCRIPT}"
    --paths-file "${RESOLVER_ALLOWLIST_FILE}"
    --repo-root "${GITHUB_WORKSPACE:-$(pwd)}"
    --max-bytes "${TARGETED_FILE_CONTEXT_MAX_BYTES:-102400}"
    --header-text "These are the conflicted files you must resolve. Their current contents (with Git conflict markers) are inlined below so you can edit immediately without re-reading them. Files marked \"would overflow total budget\" must be read with the read tool — never assume their content is in this block."
    --output "${TARGETED_FILES_CONTEXT_FILE}"
  )
  if [ "${SEMBLE_INDEX_AVAILABLE:-false}" = "true" ] && [ -s "${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE:-}" ]; then
    targeted_file_context_args+=(
      --semble-bin "${SEMBLE_BIN:-}"
      --semble-index "${SEMBLE_INDEX_PATH:-}"
      --semble-query-from "${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE}"
      --semble-max-chunks "${SEMBLE_TARGETED_CONTEXT_MAX_CHUNKS:-6}"
      --semble-fallback marker
    )
  fi
  "${targeted_file_context_args[@]}" || \
    echo "::warning::targeted_file_context.py failed; continuing without targeted-context block"
fi
# Append the targeted-context block to the rendered prompt once, so
# every retry (which copies CONFLICT_RESOLVER_PROMPT_FILE into
# RESOLVER_RETRY_PROMPT_FILE at the loop top) inherits it. The
# thread-reuse marker itself is only needed when the feature is on.
CONFLICT_RESOLVER_THREAD_REUSE_MARKER="=== THREAD REUSE LIVE CONTEXT ==="
if conflict_thread_reuse_enabled; then
  printf '\n%s\n' "${CONFLICT_RESOLVER_THREAD_REUSE_MARKER}" >> "${CONFLICT_RESOLVER_PROMPT_FILE}"
fi
if [ -s "${TARGETED_FILES_CONTEXT_FILE}" ]; then
  printf '\n' >> "${CONFLICT_RESOLVER_PROMPT_FILE}"
  cat "${TARGETED_FILES_CONTEXT_FILE}" >> "${CONFLICT_RESOLVER_PROMPT_FILE}"
fi
if [ "${SEMBLE_INDEX_AVAILABLE:-false}" = "true" ] \
   && [ -s "${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE:-}" ] \
   && declare -F semble_query_block >/dev/null 2>&1; then
  semble_query_block \
    "$(cat "${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE}")" \
    "${SEMBLE_CONFLICT_PROMPT_CHUNKS:-8}" \
    "Conflict Resolver Context" \
    > "${CONFLICT_RESOLVER_SEMBLE_CONTEXT_FILE}" || true
fi
if [ -s "${CONFLICT_RESOLVER_SEMBLE_CONTEXT_FILE}" ]; then
  printf '\n' >> "${CONFLICT_RESOLVER_PROMPT_FILE}"
  cat "${CONFLICT_RESOLVER_SEMBLE_CONTEXT_FILE}" >> "${CONFLICT_RESOLVER_PROMPT_FILE}"
fi

CONFLICT_RESOLVER_PLAIN_PATH="${PATH}"
CONFLICT_RESOLVER_CODEX_PATH="${CONFLICT_RESOLVER_PLAIN_PATH}"
CONFLICT_RESOLVER_CONTINUATION_RENDERED_FILE="${RUNTIME_DIR}/mode-review-conflict-resolver-continuation.rendered.txt"
CONFLICT_RESOLVER_CONTINUATION_SOURCE=""
conflict_wrapper_dir=""
if conflict_thread_reuse_enabled; then
  CONFLICT_RESOLVER_CONTINUATION_SOURCE="$(resolve_conflict_thread_reuse_asset 'prompts/mode-review-conflict-resolver-continuation.txt' 2>/dev/null || true)"
  if [ -z "${CONFLICT_RESOLVER_CONTINUATION_SOURCE}" ]; then
    echo "::warning::Review conflict-resolver continuation prompt not found; resolver will use the full prompt path."
  elif render_conflict_thread_reuse_continuation \
    "0" \
    "initial" \
    "${RESOLVER_MARKER_VIOLATIONS_FILE}" \
    "${RESOLVER_FP_VIOLATIONS_FILE}" \
    "${CONFLICT_RESOLVER_CONTINUATION_SOURCE}" \
    "${CONFLICT_RESOLVER_CONTINUATION_RENDERED_FILE}"; then
    if conflict_wrapper_dir="$(codex_thread_reuse_install_wrapper \
      'review-conflict-resolver' \
      "${CONFLICT_RESOLVER_CONTINUATION_RENDERED_FILE}" \
      'replace-prefix' \
      "${CONFLICT_RESOLVER_THREAD_REUSE_MARKER}")"; then
      CONFLICT_RESOLVER_CODEX_PATH="${conflict_wrapper_dir}:${CONFLICT_RESOLVER_PLAIN_PATH}"
    else
      echo "::warning::Failed to install review conflict-resolver thread-reuse wrapper; resolver will use the full prompt path."
    fi
  else
    echo "::warning::Failed to render review conflict-resolver continuation prompt; resolver will use the full prompt path."
  fi
fi

# _prev_attempt_failure_kind tracks why the previous attempt left
# the loop without breaking out via success.  Three values:
#   - "validation": codex returned 0 but soft validation (residual
#     markers / fingerprint verifier) rejected its output.  The
#     marker/fingerprint violation files are populated and accurate,
#     so the standard prelude lists them and the model can react.
#   - "timeout": the `timeout` wrapper killed codex before it
#     completed.  Detected via `timeout`'s exit codes:
#       * 124 — unconditional timer expiry (SIGTERM after duration).
#       * 137 — SIGKILL.  Disambiguated against OOM kill / external
#         SIGKILL by elapsed wall-clock time: a `timeout`-driven
#         SIGKILL fires at duration + ~30s (the `--kill-after=30s`
#         backstop), so elapsed >= duration is treated as a real
#         timeout.  Elapsed << duration on exit 137 routes to
#         "exec_error" instead.  See the `case` block at the
#         classification site.
#     On a real timeout, soft validation never ran (any partial
#     apply_patch calls were discarded by the per-attempt working-
#     tree restore on the next iteration), so the violation files
#     are stale / empty and the standard prelude would falsely
#     report "0 markers, 0 fingerprint violations".  We render a
#     timeout-aware prelude instead — see _build_retry_prompt.
#   - "exec_error": codex itself exited non-zero (config / auth /
#     model / network errors that are not the timer firing).  The
#     script has no useful reflexion data; we retry with the
#     original prompt verbatim rather than render either prelude
#     with misleading wording.
_prev_attempt_failure_kind=""

# Per-attempt cap so a runaway first attempt can't burn the full
# 170-min step budget (review_autofix.yml's resolver step cap)
# before retries get a turn.  50 min × 3 attempts = 150 min,
# leaving ~20 min for soft validation, commit, and the EXIT-trap
# dispatch within the 170-min step cap.  Default raised from 18
# min to 50 min after run 25629086684 (PR #2865 on
# tele-funtoken-msg-scoring) where every one of the 3 attempts
# hit the previous 18-min ceiling without ever producing
# apply_patch on a 7-file mixed-implementation merge — symptom in
# log: 3 × "Conflict resolver retry … (prev markers=0, prev
# fingerprint_violations=0)" then "Conflict resolver failed after
# retries."  Override via CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS
# for per-PR tuning.
#
# Default + validation + clamp run ONCE here, before the retry
# loop, so the value enforced by the `timeout` wrapper, the value
# substituted into the retry-prompt template, and the value
# printed in the retry log are guaranteed to agree.  Doing this
# inside the loop body created a window (loop top, on retry
# attempts) where _build_retry_prompt and the retry-log line
# could see an unclamped override.
: "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS:=3000}"
# Defensive: an empty / non-numeric / leading-'-' override would
# either be rejected by `timeout` outright or parsed as an option,
# burning the 3-attempt budget on env-config errors instead of
# real model work.  Restrict to positive integer seconds (matches
# the README contract).
if ! [[ "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "::warning::CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS=${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS} is not a positive integer; falling back to 3000."
  CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS=3000
fi
# Upper bound: the default of 3000s (50 min) is sized for 3
# attempts × 50 min = 150 min inside the 170-min step cap
# (review_autofix.yml's resolver step), leaving ~20 min for soft
# validation, commit, and the EXIT-trap dispatch.  Overrides above
# 3000s would consume that headroom and risk SIGKILL'ing attempt 3
# mid-flight (or starving the EXIT-trap dispatch), so any value
# above the default is clamped back to 3000s with a `::warning::`.
# In other words: this env var is a knob for shrinking the budget
# on a specific PR (e.g. forcing earlier retries on a small conflict
# set), not for enlarging it.  If a particular conflict set
# legitimately needs longer attempts, raise BOTH the step cap
# (`timeout-minutes: 170`) and the outer job cap
# (`timeout-minutes: 240`) in review_autofix.yml first, then bump
# this max accordingly and re-do the 3 × per_attempt + ~20m
# headroom math.  Keeping the max equal to the default makes that
# coupling explicit: you cannot accidentally raise this env var
# alone and have it take effect.
CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_MAX_SECS=3000
if [ "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}" -gt "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_MAX_SECS}" ]; then
  echo "::warning::CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS=${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS} exceeds the upper bound of ${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_MAX_SECS}s (would eat the soft-validation / commit / dispatch headroom under the 170-min step cap); clamping to ${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_MAX_SECS}."
  CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS="${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_MAX_SECS}"
fi

attempt=1
while [ "${attempt}" -le "${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}" ]; do
  _attempt_codex_path="${CONFLICT_RESOLVER_CODEX_PATH}"
  emit_conflict_resolver_substate "PreparingWorkspace" "${attempt}"
  # On retries: restore the working tree to its post-merge-replay
  # state (so retries don't compound the previous attempt's bad
  # edits) and build a reflexion prompt naming the previous
  # attempt's violations (or, on a timed-out previous attempt,
  # the timeout-aware variant).
  if [ "${attempt}" -gt 1 ]; then
    _restore_attempt_base || echo "::warning::Resolver retry-base restore reported a non-fatal error; continuing."
    _build_retry_prompt "$((attempt - 1))" \
      "${RESOLVER_MARKER_VIOLATIONS_FILE}" \
      "${RESOLVER_FP_VIOLATIONS_FILE}" \
      "${_prev_attempt_failure_kind:-validation}"
    _effective_prompt_file="${RESOLVER_RETRY_PROMPT_FILE}"
    # Log what was actually rendered, not what we hoped to render —
    # _build_retry_prompt may have fallen back to the original prompt
    # verbatim (template missing, IS_INTEGRATION_SYNC=false) even when
    # _prev_attempt_failure_kind suggests a prelude would be ideal.
    # Read marker / fingerprint counts only on the validation-prelude
    # branch, since that is the only branch whose log message uses them
    # and the only branch where they were freshly populated by the
    # previous attempt's soft-validation.  On the timeout / exec_error
    # paths the violation files are stale from earlier (or empty), so
    # surfacing those numbers would mislead.
    case "${_retry_prompt_outcome}" in
      timeout-prelude)
        echo "Conflict resolver retry ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: previous attempt was killed by the per-attempt timer (${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}s) before completing; any partial edits were discarded by the working-tree restore and no soft-validation data was captured. Rendered timeout-aware reflexion prompt."
        ;;
      validation-prelude)
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
        ;;
      verbatim:exec_error)
        echo "Conflict resolver retry ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: previous attempt exited non-zero before validation could run; retrying with original prompt verbatim (no reflexion data to feed back)."
        ;;
      verbatim:fallback)
        # Prelude template absent or non-integration-sync run: log the
        # underlying failure context AND that we fell back to verbatim
        # so operators reading the log do not assume a prelude was used.
        case "${_prev_attempt_failure_kind}" in
          timeout)
            echo "Conflict resolver retry ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: previous attempt was killed by the per-attempt timer (${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}s) before completing (any partial edits were discarded by the working-tree restore); timeout-aware prelude template unavailable, retrying with original prompt verbatim."
            ;;
          *)
            echo "Conflict resolver retry ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: prelude template unavailable, retrying with original prompt verbatim."
            ;;
        esac
        ;;
      *)
        # Defensive: if a future failure_kind is added without
        # extending the prelude builder, log the bare retry rather
        # than printing a stale message.
        echo "Conflict resolver retry ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: rebuilt retry prompt (prev failure_kind=${_prev_attempt_failure_kind:-unset})."
        ;;
    esac
    # Step-down reasoning effort after a per-attempt-timer kill.
    #
    # The timeout-aware prelude (PR #2453) only changes the prompt:
    # it nudges the model to be decisive and call apply_patch
    # early. It does not change codex's reasoning level. Production
    # runs (most recently the review_autofix run linked to PR #155
    # of shubhodeep1/bitsafe.io, log at 2026-05-11) show xhigh/high
    # attempts routinely consume the entire 50-min per-attempt
    # budget on multi-file conflicts, so attempt 2 at the same
    # level reliably hits the same wall.
    #
    # Walk the README.md "Thinking levels" ladder one step on each
    # consecutive timeout: xhigh → high → medium → none. At the
    # floor (none) we keep retrying at none and log that the
    # ladder is exhausted; never raise the level on a recovery, so
    # a single transient timeout doesn't cascade the rest of the
    # run into floor-level reasoning. Non-timeout failure kinds
    # (exec_error, validation) leave the level unchanged — they're
    # not evidence that thinking budget was the bottleneck.
    if [ "${_prev_attempt_failure_kind:-}" = "timeout" ]; then
      _next_level="$(_next_lower_reasoning_effort "${_current_reasoning_effort}")"
      if [ "${_next_level}" != "${_current_reasoning_effort}" ]; then
        echo "Conflict resolver retry ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: stepping reasoning effort down ${_current_reasoning_effort} → ${_next_level} after timeout-killed previous attempt (initial level ${_resolver_reasoning_effort})."
        _current_reasoning_effort="${_next_level}"
        _apply_resolver_reasoning_effort "${_current_reasoning_effort}"
      else
        echo "Conflict resolver retry ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: previous attempt timed out at reasoning level ${_current_reasoning_effort} (ladder floor reached); not stepping down further."
      fi
    fi
    if [ "${CONFLICT_RESOLVER_CODEX_PATH}" != "${CONFLICT_RESOLVER_PLAIN_PATH}" ] \
       && [ -n "${CONFLICT_RESOLVER_CONTINUATION_SOURCE}" ]; then
      if render_conflict_thread_reuse_continuation \
        "$((attempt - 1))" \
        "${_prev_attempt_failure_kind:-validation}" \
        "${RESOLVER_MARKER_VIOLATIONS_FILE}" \
        "${RESOLVER_FP_VIOLATIONS_FILE}" \
        "${CONFLICT_RESOLVER_CONTINUATION_SOURCE}" \
        "${CONFLICT_RESOLVER_CONTINUATION_RENDERED_FILE}"; then
        echo "Conflict resolver retry ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: using same-run thread reuse continuation prompt."
      else
        echo "::warning::Failed to render review conflict-resolver continuation prompt for retry ${attempt}; falling back to the full prompt path."
        _attempt_codex_path="${CONFLICT_RESOLVER_PLAIN_PATH}"
      fi
    fi
  else
    _effective_prompt_file="${CONFLICT_RESOLVER_PROMPT_FILE}"
  fi

  emit_conflict_resolver_substate "BuildingPrompt" "${attempt}"

  tmp_output="$(mktemp)"
  _stall_status_file="$(mktemp)"
  _stall_state=""
  # Pass the prompt via stdin (consistent with every other codex
  # invocation in this repo) rather than as a single positional argv
  # string. The `"$(cat …)"` shape risks ARG_MAX truncation on large
  # prompts (Linux default is ~2 MiB but environment varies) and drops
  # trailing newlines from command substitution. stdin redirection has
  # neither failure mode and matches the codex CLI's default `[PROMPT]`
  # contract: "If not provided as an argument (or if `-` is used),
  # instructions are read from stdin".
  #
  # `--` terminates `timeout`'s option parsing so a leading '-' in
  # DURATION cannot be mistaken for an option (defence-in-depth on top
  # of the regex check that runs before the loop).  Default + validation
  # + clamp on CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS happens once
  # before the loop so this expansion is the same value used by
  # _build_retry_prompt and the retry-log line for every iteration.
  _codex_exit=0
  _attempt_started_at=$(date +%s)
  # Strip any invalid UTF-8 bytes that may have leaked into the
  # retry-prompt (rebuilt inside the loop, so we sanitise each
  # iteration). See sanitize_codex_prompt_file in scripts/gh_helpers.sh.
  if command -v sanitize_codex_prompt_file >/dev/null 2>&1; then
    sanitize_codex_prompt_file "${_effective_prompt_file}"
  fi
  _run_codex=true
  if [ -x "${WORKSPACE_SAFETY_CHECK_HELPER}" ]; then
    if ! bash "${WORKSPACE_SAFETY_CHECK_HELPER}"; then
      _codex_exit=$?
      _run_codex=false
    fi
  fi
  if [ "${_run_codex}" = "true" ]; then
    emit_conflict_resolver_substate "LaunchingAgentProcess" "${attempt}"
    emit_conflict_resolver_substate "InitializingSession" "${attempt}"
    emit_conflict_resolver_substate "StreamingTurn" "${attempt}"
    if [ -x "${CODEX_STALL_GUARD_HELPER}" ]; then
      PATH="${_attempt_codex_path}" timeout --signal=TERM --kill-after=30s -- "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}" \
        "${CODEX_STALL_GUARD_HELPER}" \
        --phase review_conflict_resolve \
        --stdout-file "${tmp_output}" \
        --status-file "${_stall_status_file}" \
        -- codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${_effective_prompt_file}" \
        || _codex_exit=$?
    elif [ -x "${CODEX_HEARTBEAT_HELPER}" ]; then
      PATH="${_attempt_codex_path}" timeout --signal=TERM --kill-after=30s -- "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}" \
        "${CODEX_HEARTBEAT_HELPER}" \
        --phase review_conflict_resolve \
        --stdout-file "${tmp_output}" \
        -- codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${_effective_prompt_file}" \
        || _codex_exit=$?
    else
      PATH="${_attempt_codex_path}" timeout --signal=TERM --kill-after=30s -- "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}" \
        codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${_effective_prompt_file}" > "${tmp_output}" \
        || _codex_exit=$?
    fi
  fi
  _attempt_elapsed=$(( $(date +%s) - _attempt_started_at ))
  emit_conflict_resolver_substate "Finishing" "${attempt}"
  if _stall_state="$(read_codex_stall_guard_state "${_stall_status_file}" 2>/dev/null)"; then
    :
  elif [ -s "${_stall_status_file}" ]; then
    echo "::warning::Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: could not parse codex stall guard status from ${_stall_status_file}."
  fi
  rm -f "${_stall_status_file}"
  if [ "${_stall_state}" = "observed" ]; then
    echo "Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: codex_stall_observed recorded (observe-only mode)."
    emit_conflict_resolver_substate "codex_stall_observed" "${attempt}"
  fi
  if [ "${_codex_exit}" -eq 78 ]; then
    echo "::error::Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: workspace_safety_violation."
    emit_conflict_resolver_substate "Failed" "${attempt}"
    exit 78
  fi
  # Graceful-SIGTERM-at-timer-boundary diagnostic.  If codex installs
  # a SIGTERM handler that completes cleanup and exits 0 within the
  # 30s `--kill-after` window, `timeout` propagates the child's 0
  # exit and `_codex_exit` stays 0 — the timeout-classification
  # block below is skipped and execution falls through to soft
  # validation.  This is intentional: the working tree is the source
  # of truth for whether a useful resolution landed.  Three cases
  # follow naturally from the existing soft-validation gates without
  # special-casing the failure kind:
  #   1. Tree clean (no residual markers, fingerprints satisfied) —
  #      the attempt succeeded and we `break` out of the retry loop.
  #      Treating this as a timeout would force a retry on a
  #      legitimately-resolved conflict, which is a regression.
  #   2. Tree dirty (markers or fingerprint violations) —
  #      _prev_attempt_failure_kind="validation" fires (after the
  #      ::warning::) and the standard prelude renders with the REAL
  #      post-codex marker/fingerprint data captured by the soft
  #      gates.  That is more actionable than the timeout prelude's
  #      generic "be decisive, call apply_patch early" guidance,
  #      because it names specific files / regexes the model needs
  #      to fix on the next attempt.
  #   3. Tree dirty AND no progress vs the previous attempt — the
  #      no-progress detection promotes the attempt counter to MAX
  #      and the run aborts with the orchestrator-poll dispatch,
  #      same as a natural exhaustion.
  # The diagnostic log is informational only — it surfaces the edge
  # case for operators reading the log without changing the retry
  # path.  Multi-reviewer feedback on PR #2453 flagged this case
  # repeatedly; this comment block documents why no classification
  # change is warranted.
  if [ "${_codex_exit}" -eq 0 ] && [ "${_attempt_elapsed}" -ge "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}" ]; then
    echo "Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}: codex exited 0 with elapsed ${_attempt_elapsed}s ≥ per-attempt budget ${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}s — likely a graceful SIGTERM-handler exit at the timer boundary; soft validation will inspect the post-codex working-tree state directly (no failure-kind change is needed — see comment block above)."
  fi
  if [ "${_codex_exit}" -ne 0 ]; then
    rm -f "${tmp_output}"
    # `timeout` documents two specific exit codes:
    #   - 124: the child was sent SIGTERM after DURATION expired.
    #     Definitively a timer expiry.
    #   - 137: the child received SIGKILL (128 + 9). `timeout
    #     --kill-after=30s` produces this when the child ignored the
    #     SIGTERM long enough for the kill-after backstop to fire,
    #     BUT 137 is also produced by the OOM killer, by an external
    #     `kill -9`, and by other SIGKILL sources that have nothing
    #     to do with the per-attempt timer.  We disambiguate by
    #     comparing the wall-clock elapsed time against the
    #     configured duration: a `timeout`-driven SIGKILL fires at
    #     duration + ~30s, so elapsed >= duration is a strong signal
    #     of timer expiry; an OOM kill at minute 2 of a 50-min
    #     budget produces elapsed << duration and gets routed to
    #     exec_error (where the standard fail-open path retries
    #     with the original prompt verbatim).
    # Anything else came from codex itself — config / auth / model
    # / network errors that have nothing to do with the per-attempt
    # timer.  Distinguishing matters because the standard / timeout
    # preludes would actively mislead the model on the wrong path:
    #   - On a real timeout, soft-validation never ran and the
    #     marker/fingerprint violation files are stale / empty;
    #     the timeout-aware prelude tells the model to be decisive
    #     and call apply_patch early on the next attempt.
    #   - On an exec error, the script has no reflexion data to
    #     hand back; falling back to the original prompt verbatim
    #     is the conservative choice — same handling as a missing
    #     prelude template.
    if [ "${_stall_state}" = "killed" ]; then
      _prev_attempt_failure_kind="stall_guard"
      echo "Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS} recorded codex_stall_killed (exit ${_codex_exit}; elapsed ${_attempt_elapsed}s; per-attempt timer budget ${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}s)."
      emit_conflict_resolver_substate "codex_stall_killed" "${attempt}"
    else
      case "${_codex_exit}" in
        124)
          _prev_attempt_failure_kind="timeout"
          echo "Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS} killed by per-attempt timer (timeout exit 124 = SIGTERM after ${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}s; elapsed ${_attempt_elapsed}s)."
          ;;
        137)
          if [ "${_attempt_elapsed}" -ge "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}" ]; then
            _prev_attempt_failure_kind="timeout"
            echo "Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS} killed by per-attempt timer (timeout exit 137 = SIGKILL after kill-after backstop; elapsed ${_attempt_elapsed}s ≥ budget ${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}s)."
          else
            _prev_attempt_failure_kind="exec_error"
            echo "Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS} crashed (SIGKILL exit 137 but elapsed ${_attempt_elapsed}s < budget ${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}s — likely OOM kill or external SIGKILL, not the per-attempt timer)."
          fi
          ;;
        *)
          _prev_attempt_failure_kind="exec_error"
          echo "Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS} failed with non-timeout exit ${_codex_exit} (codex itself returned non-zero; elapsed ${_attempt_elapsed}s)."
          ;;
        esac
    fi
    case "${_prev_attempt_failure_kind}" in
      stall_guard)
        emit_conflict_resolver_substate "Stalled" "${attempt}"
        ;;
      timeout)
        emit_conflict_resolver_substate "TimedOut" "${attempt}"
        ;;
      *)
        emit_conflict_resolver_substate "Failed" "${attempt}"
        ;;
    esac
    if [ "${attempt}" -eq "${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}" ]; then
      if [ "${_prev_attempt_failure_kind}" = "timeout" ]; then
        echo "Conflict resolver failed after retries (final attempt killed by per-attempt timer)."
      elif [ "${_prev_attempt_failure_kind}" = "stall_guard" ]; then
        echo "Conflict resolver failed after retries (final attempt killed by codex stall guard)."
      else
        echo "Conflict resolver failed after retries (final attempt: codex non-zero exit ${_codex_exit})."
      fi
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
  # side under the legacy editor default after PR #2095's
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
    emit_conflict_resolver_substate "Succeeded" "${attempt}"
    # Re-emit the verifier's info line at normal verbosity so
    # operators see the "must_contain satisfied N/M" summary
    # that exists today.  Plumbing-exit (2) was not a soft
    # failure; just pass through any captured output.
    if [ -s "${RESOLVER_FP_VERIFIER_OUTPUT_FILE}" ]; then
      cat "${RESOLVER_FP_VERIFIER_OUTPUT_FILE}" || true
    fi
    break
  fi

  # Codex returned 0 but the soft gates rejected its output —
  # mark the failure kind so the next iteration's reflexion prompt
  # uses the standard violations-listing prelude (the violation
  # files are populated and accurate).
  _prev_attempt_failure_kind="validation"
  echo "::warning::Conflict resolver attempt ${attempt}/${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS} failed soft validation: residual_markers=${_marker_count} fingerprint_violations=${_fp_count}."
  if [ "${_marker_count}" -gt 0 ]; then
    echo "Files with residual Git conflict markers (first 10):"
    head -10 "${RESOLVER_MARKER_VIOLATIONS_FILE}" | sed 's/^/  - /' || true
  fi
  if [ "${_fp_count}" -gt 0 ]; then
    echo "Fingerprint violations (first 10):"
    head -10 "${RESOLVER_FP_VIOLATIONS_FILE}" | sed 's/^/  - /' || true
  fi
  emit_conflict_resolver_substate "Failed" "${attempt}"

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
    if [ "${RESOLVER_FP_EXIT}" -eq 1 ] || [ "${RESOLVER_FP_VERIFICATION_TIER:-strict}" = "warn_only" ]; then
      _persist_resolver_retry_state_from_current_failure || true
    fi
    if [ "${IS_INTEGRATION_SYNC:-false}" = "true" ] \
       && [ -n "${INTEGRATION_FINGERPRINTS_FILE:-}" ] \
       && [ -f "${INTEGRATION_FINGERPRINTS_FILE}" ] \
       && [ -f "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" ]; then
      _final_fp_exit=0
      _final_verifier_args=()
      if [ -s "${RESOLVER_FP_BASELINE_STATE_FILE:-/nonexistent}" ]; then
        _final_verifier_args+=(
          --compare-against-baseline "${RESOLVER_FP_BASELINE_STATE_FILE}"
        )
      fi
      if [ "${RESOLVER_FP_VERIFICATION_TIER:-strict}" != "strict" ]; then
        _final_verifier_args+=(
          --verification-tier "${RESOLVER_FP_VERIFICATION_TIER}"
        )
      fi
      INTEGRATION_BRANCH_NAME="${INTEGRATION_BRANCH_NAME:-${TARGET_BRANCH:-}}" \
        python3 "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" \
          "${_final_verifier_args[@]}" \
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
  git remote set-url origin "https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"
  # NOTE: push deferred to final "Push all pending commits" step.
  echo "CONFLICT_RESOLVED=true" >> "$GITHUB_ENV"
  echo "Conflicts resolved and committed (push deferred)"
else
  echo "No conflict resolution changes to commit"
fi

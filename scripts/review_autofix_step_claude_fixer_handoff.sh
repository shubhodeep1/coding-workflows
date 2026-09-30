#!/usr/bin/env bash
# Body of the "Hand review round to Claude session (Claude-fixer mode)" step
# in .github/workflows/review_autofix.yml. The step sources this file in its
# own shell, so the step's if:, env: and continue-on-error: stay in the
# workflow; edit those there.
#
# Claude-fixer mode (gate output `claude_fixer`, every PR-backed `claude/*`
# head): the reviewer panel runs exactly as for every PR, but the GPT editor,
# the conflict resolver and the push / re-trigger tail do not. The Claude
# session that owns the PR fixes the findings instead (the /implement-plan-claude
# stage session, or under CLAUDE.md §26 the session that pushed the PR, a
# `/fix-claude-pr` session, or the catch-all sweep's session), and this step
# is the hand-off:
#
#   * pre-review content conflict (AUTOFIX_PRE_REVIEW_RESOLVE=true)
#       -> one comment carrying `kind=conflict` and the unmerged paths;
#   * reviewer findings or failing check runs
#       -> the consensus ledger through scripts/post_review_comment.sh
#          (chunked PR comments), then one comment carrying `kind=findings`;
#   * nothing at all (no finding in any ledger block, fresh ready check snapshot)
#       -> no comment; CLAUDE_FIXER_ZERO_FINDINGS=true is exported so the
#          workflow's own auto-merge step runs, as it does when the editor
#          finds nothing to commit.
#
# Rejected single-reviewer findings (issue #4586): before counting, a
# well-formed ledger goes through scripts/review_claude_fixer_nonblocking.py,
# which moves every consensus finding raised by exactly one reviewer and
# rejected (REJECTED_FINDING lines in the raw pass-2 outputs under
# PREVIOUS_REVIEWS_DIR) by a strict majority of the other successful
# reviewers, at least two, into a NON-BLOCKING FINDINGS block. A rejection
# counts only when it names a finding ID from this run's
# PREVIOUS_REVIEWS_DIR/rejection_ids_pass1.json (issue #4688), so a line
# quoted from the PR never counts and no manifest means no demotion. The ID
# binds to the finding by its pass-1 consensus_id, never by file and line
# proximity, and an ambiguous match stays blocking (issue #4687). A rejection
# also counts only when its `evidence: <file>:<range> | quote: <text>` fields
# verify against the reviewed commit HEAD_SHA, read with local git from the
# PR checkout in GITHUB_WORKSPACE (issue #4976); a free-text reason alone
# never demotes a finding, and an unreadable commit demotes nothing. Votes are
# necessary but never sufficient (issue #5582): the filter also requires an
# independent automated disproof of the finding, and this step supplies none,
# so every entry stays blocking and the filter logs
# CLAUDE_FIXER_NONBLOCKING_KEPT ... reason=no_automated_proof for an entry
# that met every vote condition. The filtered
# copy is what this step counts, digests, and posts; a round whose only
# entries are non-blocking still posts the ledger, then takes the
# zero-findings path. A missing or failing filter keeps the original ledger,
# so every finding stays blocking.
#
# The hand-off marker is
#   <!-- ai:claude-fixer-handoff:v1 kind=<findings|conflict> head=<sha> round=<n> -->
# and .claude/scripts/check_in_status.py reads it (plus the session's
# `ai:claude-fixer-verdict:v1` reply) to start the next stage session or to
# hand the round back to the session that pushed the PR (--hand-back).
#
# Inputs (environment): PR_NUMBER, GH_TOKEN, GITHUB_REPOSITORY, HEAD_SHA,
# HEAD_REF, CLAUDE_FIXER_ROUND_INDEX (consecutive [ai-autofix] /
# [claude-autofix] commits on the head; round = index + 1),
# AUTOFIX_PRE_REVIEW_RESOLVE,
# AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED, REVIEWER_CONSENSUS_FILE,
# PREVIOUS_REVIEWS_DIR, PR_CHECK_RUNS_CONTEXT_FILE, SUPPORT_SCRIPTS_DIR,
# GITHUB_RUN_ID, GITHUB_SERVER_URL, RUNTIME_DIR, GITHUB_WORKSPACE (the PR
# checkout that holds HEAD_SHA).
# API calls: on a clean candidate, the existing check-run collector refreshes
# its paginated check-runs GET; on findings, the ledger chunks from
# post_review_comment.sh and one hand-off comment are posted; on a clean
# round with non-blocking entries, only the ledger chunks are posted.
set -euo pipefail

# shellcheck source=/dev/null
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

if ! [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] || ! [[ "${HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::Claude-fixer hand-off needs a numeric PR_NUMBER and a 40-hex HEAD_SHA (PR_NUMBER=${PR_NUMBER:-} HEAD_SHA=${HEAD_SHA:-})."
  exit 1
fi
claude_fixer_round_index="${CLAUDE_FIXER_ROUND_INDEX:-0}"
[[ "${claude_fixer_round_index}" =~ ^[0-9]+$ ]] || claude_fixer_round_index=0
claude_fixer_round="$((claude_fixer_round_index + 1))"
claude_fixer_run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID:-0}"
claude_fixer_body_file="$(mktemp "${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_handoff.XXXXXX")"

claude_fixer_post_marker_comment()
{
  local payload_file
  payload_file="$(mktemp "${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_handoff_payload.XXXXXX")"
  jq -n --rawfile body "${claude_fixer_body_file}" '{body: $body}' > "${payload_file}"
  gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" --input "${payload_file}" >/dev/null
  rm -f "${payload_file}"
}

if [ "${AUTOFIX_PRE_REVIEW_RESOLVE:-false}" = "true" ]; then
  if [ "${CLAUDE_FIXER_VERIFICATION:-false}" = "true" ]; then
    echo "CLAUDE_FIXER_VERIFICATION_FAILED=true" >> "$GITHUB_ENV"
  fi
  {
    echo "## Review round ${claude_fixer_round}: merge conflict, handed to the Claude session"
    echo
    echo "The head \`${HEAD_SHA}\` conflicts with its base branch, so the reviewer panel did not run ([workflow run](${claude_fixer_run_url}))."
    echo "Claude-fixer mode skips the GPT conflict resolver: the Claude session that owns this PR merges the base branch into \`${HEAD_REF:-the head branch}\`, resolves the conflict keeping both sides' intent, commits it as \`[claude-merge-resolve] <summary>\`, and pushes. The push starts the next review round."
    if [ -n "${AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED:-}" ]; then
      echo
      echo "Unmerged paths: \`${AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED//,/\`, \`}\`"
    fi
    echo
    echo "<!-- ai:claude-fixer-handoff:v1 kind=conflict head=${HEAD_SHA} round=${claude_fixer_round} -->"
    if [ "${CLAUDE_FIXER_VERIFICATION:-false}" = "true" ]; then
      echo "<!-- ai:claude-fixer-verification:v1 head=${HEAD_SHA} result=unresolved -->"
    fi
  } > "${claude_fixer_body_file}"
  claude_fixer_post_marker_comment
  echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=conflict"
  exit 0
fi

# Count ledger entries: every "- " bullet inside a CONSENSUS FINDINGS,
# CONSENSUS TASK GAPS or FINDINGS FROM <slug> block. A missing or empty
# ledger fails closed (the round is handed off, never auto-merged).
claude_fixer_ledger_well_formed()
{
  [ -s "$1" ] \
    && grep -Fxq '=== CONSENSUS FINDINGS ===' "$1" \
    && grep -Fxq '=== END CONSENSUS FINDINGS ===' "$1" \
    && grep -Fxq '=== CONSENSUS TASK GAPS ===' "$1" \
    && grep -Fxq '=== END CONSENSUS TASK GAPS ===' "$1" \
    && grep -Eq '^=== FINDINGS FROM .+ ===$' "$1"
}

claude_fixer_ledger_state="missing"
claude_fixer_finding_count=0
claude_fixer_ledger_digest=""
claude_fixer_ledger_file="${REVIEWER_CONSENSUS_FILE:-}"
claude_fixer_nonblocking_count=0
if [[ "${REVIEWERS_SUCCESSFUL:-}" =~ ^[1-9][0-9]*$ ]] \
  && claude_fixer_ledger_well_formed "${REVIEWER_CONSENSUS_FILE:-}"; then
  claude_fixer_ledger_state="ok"
  # Demote rejected single-reviewer findings (issue #4586). Votes alone never
  # demote (issue #5582), and any failure keeps the original ledger, so every
  # finding stays blocking.
  claude_fixer_nonblocking_script="${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_nonblocking.py"
  [ -f "${claude_fixer_nonblocking_script}" ] || claude_fixer_nonblocking_script="${GITHUB_WORKSPACE:-}/.codex-workflow-src/scripts/review_claude_fixer_nonblocking.py"
  claude_fixer_filtered_ledger="${RUNTIME_DIR:-${TMPDIR:-/tmp}}/reviewer_consensus_claude_fixer.txt"
  rm -f "${claude_fixer_filtered_ledger}"
  if [ ! -f "${claude_fixer_nonblocking_script}" ]; then
    echo "::warning::review_claude_fixer_nonblocking.py not found; every ledger entry stays blocking."
  elif [ -z "${PREVIOUS_REVIEWS_DIR:-}" ] || [ ! -d "${PREVIOUS_REVIEWS_DIR}" ]; then
    echo "::warning::PREVIOUS_REVIEWS_DIR is unset or missing; every ledger entry stays blocking."
  elif claude_fixer_nonblocking_out="$(PYTHONDONTWRITEBYTECODE=1 python3 "${claude_fixer_nonblocking_script}" \
      --ledger "${REVIEWER_CONSENSUS_FILE}" --reviews-dir "${PREVIOUS_REVIEWS_DIR}" --output "${claude_fixer_filtered_ledger}" \
      --source-root "${GITHUB_WORKSPACE:-${PWD}}" --source-commit "${HEAD_SHA:-}")" \
    && claude_fixer_ledger_well_formed "${claude_fixer_filtered_ledger}"; then
    printf '%s\n' "${claude_fixer_nonblocking_out}"
    claude_fixer_nonblocking_count="$(printf '%s\n' "${claude_fixer_nonblocking_out}" | sed -n 's/^CLAUDE_FIXER_NONBLOCKING demoted=\([0-9][0-9]*\) .*/\1/p' | head -n 1)"
    [[ "${claude_fixer_nonblocking_count}" =~ ^[0-9]+$ ]] || claude_fixer_nonblocking_count=0
    claude_fixer_ledger_file="${claude_fixer_filtered_ledger}"
  else
    echo "::warning::review_claude_fixer_nonblocking.py failed; every ledger entry stays blocking."
    claude_fixer_nonblocking_count=0
  fi
  claude_fixer_ledger_digest="$(sha256sum "${claude_fixer_ledger_file}" | cut -d ' ' -f 1)"
  claude_fixer_finding_count="$(awk '
    /^=== (CONSENSUS FINDINGS|CONSENSUS TASK GAPS|FINDINGS FROM .*) ===$/ { in_block = 1; next }
    /^=== END / { in_block = 0; next }
    in_block && /^- / { total++ }
    END { printf "%d\n", total + 0 }
  ' "${claude_fixer_ledger_file}")"
fi
claude_fixer_failed_checks=""
if [ -s "${PR_CHECK_RUNS_CONTEXT_FILE:-}" ]; then
  claude_fixer_failed_checks="$(sed -n 's/^failed\[[0-9]*\]\.name: //p' "${PR_CHECK_RUNS_CONTEXT_FILE}" | paste -sd, - || true)"
fi

claude_fixer_clean_ledger="false"
if [ "${claude_fixer_ledger_state}" = "ok" ] && [ "${claude_fixer_finding_count}" -eq 0 ] \
  && awk '
    /^=== CONSENSUS FINDINGS ===$/ { expected = "(No findings reported.)"; block = 1; entries = 0; next }
    /^=== CONSENSUS TASK GAPS ===$/ { expected = "(No task gaps reported.)"; block = 1; entries = 0; next }
    /^=== FINDINGS FROM .+ ===$/ { expected = "(No findings reported.)"; block = 1; entries = 0; next }
    /^=== END / { if (block) { if (entries != 1) invalid = 1; blocks++; block = 0 }; next }
    block && NF { entries++; if ($0 != expected) invalid = 1 }
    END { if (invalid || block || blocks < 3) exit 1 }
  ' "${claude_fixer_ledger_file}"; then
  claude_fixer_clean_ledger="true"
fi

if [ "${claude_fixer_clean_ledger}" = "true" ] && [ -z "${claude_fixer_failed_checks}" ]; then
  # Reviewers can run for hours after the first snapshot. Refresh using the
  # existing collector, not the earlier reviewer context, before authorizing
  # a merge; a disabled, timed-out, or malformed snapshot fails closed.
  claude_fixer_checks_refreshed="false"
  if [ -f "${SUPPORT_SCRIPTS_DIR}/collect_pr_check_runs_context.py" ]; then
    SELF_RUN_ID="${GITHUB_RUN_ID:-}" CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT=true PYTHONDONTWRITEBYTECODE=1 \
      python3 "${SUPPORT_SCRIPTS_DIR}/collect_pr_check_runs_context.py"
    claude_fixer_checks_refreshed="true"
  fi
  if [ "${claude_fixer_checks_refreshed}" = "true" ] \
    && [ -s "${PR_CHECK_RUNS_CONTEXT_FILE:-}" ] \
    && [ "$(sed -n '1p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "PR_CHECK_RUNS_CONTEXT" ] \
    && [ "$(sed -n '2p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "head_sha: ${HEAD_SHA}" ] \
    && [ "$(sed -n '3p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "collection_status: ready" ] \
    && grep -Eq '^total_check_runs: [1-9][0-9]*$' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && grep -Fxq 'failed_count: 0' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && grep -Fxq 'incomplete_count: 0' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && ! grep -Eq '^(failed|incomplete)\[[0-9]+\]\.' "${PR_CHECK_RUNS_CONTEXT_FILE}"; then
    if [ "${claude_fixer_nonblocking_count}" -gt 0 ]; then
      # Keep the non-blocking entries visible on the PR (issue #4586). The
      # post is best-effort: the run log carries them either way.
      REVIEWER_CONSENSUS_FILE="${claude_fixer_ledger_file}" REPOSITORY="${GITHUB_REPOSITORY}" \
        bash "${SUPPORT_SCRIPTS_DIR}/post_review_comment.sh" \
        || echo "::warning::Could not post the ledger with ${claude_fixer_nonblocking_count} non-blocking entries; see the CLAUDE_FIXER_NONBLOCKING lines above."
    fi
    echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=none findings=0 failed_checks=0 action=auto_merge nonblocking=${claude_fixer_nonblocking_count}"
    echo "CLAUDE_FIXER_ZERO_FINDINGS=true" >> "$GITHUB_ENV"
    exit 0
  fi
  echo "::warning::Claude-fixer clean review has no fresh ready same-head check-run snapshot; auto-merge disabled."
fi

if [ "${CLAUDE_FIXER_VERIFICATION:-false}" = "true" ]; then
  # A same-head verdict gets one independent review. Unresolved findings or
  # unavailable verification require intervention, not another verdict loop.
  echo "CLAUDE_FIXER_VERIFICATION_FAILED=true" >> "$GITHUB_ENV"
fi

if [ "${claude_fixer_ledger_state}" = "ok" ]; then
  REVIEWER_CONSENSUS_FILE="${claude_fixer_ledger_file}" REPOSITORY="${GITHUB_REPOSITORY}" bash "${SUPPORT_SCRIPTS_DIR}/post_review_comment.sh"
fi

{
  echo "## Review round ${claude_fixer_round}: findings handed to the Claude session"
  echo
  echo "Reviewed head: \`${HEAD_SHA}\` ([workflow run](${claude_fixer_run_url}))."
  if [ "${claude_fixer_ledger_state}" = "ok" ]; then
    echo "Reviewer ledger entries: ${claude_fixer_finding_count} (posted above)."
    if [ "${claude_fixer_nonblocking_count}" -gt 0 ]; then
      echo "Non-blocking entries: ${claude_fixer_nonblocking_count} (the ledger's NON-BLOCKING FINDINGS block: each was raised by one reviewer, rejected by a majority of the others, and proved false by an independent automated check; no fix or verdict is needed for them)."
    fi
  else
    echo "The consensus ledger was not produced; the per-reviewer outputs are in the run's \`reviewer-logs-*\` artifact."
  fi
  if [ -n "${claude_fixer_failed_checks}" ]; then
    echo "Failing check runs on this head: \`${claude_fixer_failed_checks//,/\`, \`}\`."
  fi
  if [ -n "${claude_fixer_ledger_digest}" ]; then
    echo "Ledger SHA-256: \`${claude_fixer_ledger_digest}\`."
  fi
  echo
  echo "Claude-fixer mode: the GPT editor did not run. The Claude session that owns this PR judges each finding against the code, then either"
  echo "- fixes the valid ones in **one** commit whose subject starts with \`[claude-autofix]\` and pushes it (the push starts the next review round; the commits count toward \`MAX_AUTOFIX_ITERATIONS\` like \`[ai-autofix]\` ones), or"
  echo "- when nothing valid is left, has the dedicated fixer bot (not GH_PAT or a human) post a separate comment listing rejections and ending with both \`ai:claude-fixer-verdict:v1\` and \`ai:claude-fixer-verdict:v2\` markers for this head, round and ledger SHA-256, then dispatches this workflow with \`claude_fixer_converged_head=${HEAD_SHA}\`. That dispatch re-runs the reviewers; only a clean review with a fresh ready check snapshot can enable auto-merge. If the bot cannot post, leave the PR blocked."
  echo
  echo "<!-- ai:claude-fixer-handoff:v1 kind=findings head=${HEAD_SHA} round=${claude_fixer_round} -->"
  if [ -n "${claude_fixer_ledger_digest}" ]; then
    echo "<!-- ai:claude-fixer-handoff:v2 head=${HEAD_SHA} round=${claude_fixer_round} ledger=${claude_fixer_ledger_digest} -->"
  fi
  if [ "${CLAUDE_FIXER_VERIFICATION:-false}" = "true" ]; then
    echo "<!-- ai:claude-fixer-verification:v1 head=${HEAD_SHA} result=unresolved -->"
  fi
} > "${claude_fixer_body_file}"
claude_fixer_post_marker_comment
echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=findings findings=${claude_fixer_finding_count} ledger=${claude_fixer_ledger_state} failed_checks=${claude_fixer_failed_checks:-none} nonblocking=${claude_fixer_nonblocking_count}"

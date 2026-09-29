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
#          finds nothing to commit;
#   * nothing but check runs still running on the same head (clean ledger,
#     `ready` or `timeout` snapshot, none failed; issue #4900)
#       -> one comment carrying the pending-checks markers
#            <!-- ai:claude-fixer-pending-checks:v1 head=<sha> round=<n> ledger=<sha256> -->
#            <!-- ai:claude-fixer-pending-checks:v2 head=<sha> round=<n> ledger=<sha256> base_sha=<sha> base_ref_sha256=<sha256> -->
#          which is not a hand-off: the gate skips dispatched re-runs on
#          that head while the PR's base still matches the v2 line, and the
#          claude-pr-catch-all sweep (scripts/claude_fixer_pending_checks.py)
#          enables head-bound auto-merge once the checks finish green. The
#          v2 line binds the clean review to the base it reviewed
#          (PR_PAYLOAD_FILE's base.sha, and sha256 of its base.ref, since a
#          ref name may contain `-->`); a retargeted PR fails that binding,
#          so it is never merged on this marker and gets a fresh review
#          (issue #5147). Without a valid payload base the step posts no
#          pending-checks comment and hands off as below.
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
# PR_CHECK_RUNS_CONTEXT_FILE, PR_PAYLOAD_FILE (the review run's REST PR
# object; base.ref / base.sha bind a pending-checks marker), SUPPORT_SCRIPTS_DIR,
# GITHUB_RUN_ID, GITHUB_SERVER_URL, RUNTIME_DIR.
# API calls: on a clean candidate, the existing check-run collector refreshes
# its paginated check-runs GET; on findings, the ledger chunks from
# post_review_comment.sh and one hand-off comment are posted; on pending
# checks, one pending-checks comment is posted (no ledger chunks).
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
claude_fixer_ledger_state="missing"
claude_fixer_finding_count=0
claude_fixer_ledger_digest=""
if [[ "${REVIEWERS_SUCCESSFUL:-}" =~ ^[1-9][0-9]*$ ]] \
  && [ -s "${REVIEWER_CONSENSUS_FILE:-}" ] \
  && grep -Fxq '=== CONSENSUS FINDINGS ===' "${REVIEWER_CONSENSUS_FILE}" \
  && grep -Fxq '=== END CONSENSUS FINDINGS ===' "${REVIEWER_CONSENSUS_FILE}" \
  && grep -Fxq '=== CONSENSUS TASK GAPS ===' "${REVIEWER_CONSENSUS_FILE}" \
  && grep -Fxq '=== END CONSENSUS TASK GAPS ===' "${REVIEWER_CONSENSUS_FILE}" \
  && grep -Eq '^=== FINDINGS FROM .+ ===$' "${REVIEWER_CONSENSUS_FILE}"; then
  claude_fixer_ledger_state="ok"
  claude_fixer_ledger_digest="$(sha256sum "${REVIEWER_CONSENSUS_FILE}" | cut -d ' ' -f 1)"
  claude_fixer_finding_count="$(awk '
    /^=== (CONSENSUS FINDINGS|CONSENSUS TASK GAPS|FINDINGS FROM .*) ===$/ { in_block = 1; next }
    /^=== END / { in_block = 0; next }
    in_block && /^- / { total++ }
    END { printf "%d\n", total + 0 }
  ' "${REVIEWER_CONSENSUS_FILE}")"
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
  ' "${REVIEWER_CONSENSUS_FILE}"; then
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
    echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=none findings=0 failed_checks=0 action=auto_merge"
    echo "CLAUDE_FIXER_ZERO_FINDINGS=true" >> "$GITHUB_ENV"
    exit 0
  fi
  # Issue #4900: the reviewers often finish before CI (PR #4869). When the
  # only obstacle is incomplete check runs (same head, well-formed snapshot,
  # none failed), there is nothing for a Claude session to fix: post the
  # pending-checks marker instead of a findings hand-off. It is not a
  # hand-off, so check_in_status.py keeps waiting, and the claude-pr-catch-all
  # sweep (scripts/claude_fixer_pending_checks.py) enables head-bound
  # auto-merge once the checks finish green. A check that fails later is a
  # `ci-failed` Claude fix, as for any claude/* head.
  # Issue #5147: the marker is also bound to the base this run reviewed
  # (the PR snapshot's base.ref and base.sha), so a PR retargeted after the
  # review is never merged on it. No valid base, no pending-checks comment.
  claude_fixer_base_ref=""
  claude_fixer_base_sha=""
  claude_fixer_base_ref_digest=""
  if [ -s "${PR_PAYLOAD_FILE:-}" ]; then
    claude_fixer_base_ref="$(jq -r '.base.ref // "" | if type == "string" then . else "" end' "${PR_PAYLOAD_FILE}" 2>/dev/null || true)"
    claude_fixer_base_sha="$(jq -r '.base.sha // "" | if type == "string" then . else "" end' "${PR_PAYLOAD_FILE}" 2>/dev/null || true)"
  fi
  if [ -n "${claude_fixer_base_ref}" ] && [[ "${claude_fixer_base_ref}" != *$'\n'* ]]; then
    claude_fixer_base_ref_digest="$(printf '%s' "${claude_fixer_base_ref}" | sha256sum | cut -d ' ' -f 1)"
  fi
  if ! [[ "${claude_fixer_base_sha}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${claude_fixer_base_ref_digest}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "::warning::Claude-fixer pending-checks marker disabled: the review run's PR snapshot has no valid base (PR_PAYLOAD_FILE base.ref / base.sha), so a clean review cannot be bound to the reviewed base (issue #5147)."
  fi
  if [ "${claude_fixer_checks_refreshed}" = "true" ] \
    && [[ "${claude_fixer_base_sha}" =~ ^[0-9a-f]{40}$ ]] \
    && [[ "${claude_fixer_base_ref_digest}" =~ ^[0-9a-f]{64}$ ]] \
    && [ -s "${PR_CHECK_RUNS_CONTEXT_FILE:-}" ] \
    && [ "$(sed -n '1p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "PR_CHECK_RUNS_CONTEXT" ] \
    && [ "$(sed -n '2p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "head_sha: ${HEAD_SHA}" ] \
    && grep -Exq 'collection_status: (ready|timeout)' <(sed -n '3p' "${PR_CHECK_RUNS_CONTEXT_FILE}") \
    && grep -Eq '^total_check_runs: [1-9][0-9]*$' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && grep -Fxq 'failed_count: 0' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && grep -Eq '^incomplete_count: [1-9][0-9]*$' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && ! grep -Eq '^failed\[[0-9]+\]\.' "${PR_CHECK_RUNS_CONTEXT_FILE}"; then
    claude_fixer_incomplete_checks="$(sed -n 's/^incomplete\[[0-9]*\]\.name: //p' "${PR_CHECK_RUNS_CONTEXT_FILE}" | paste -sd, - || true)"
    {
      echo "## Review round ${claude_fixer_round}: clean review, waiting for check runs"
      echo
      echo "Reviewed head: \`${HEAD_SHA}\` ([workflow run](${claude_fixer_run_url}))."
      echo "Reviewed base: \`${claude_fixer_base_ref}\` at \`${claude_fixer_base_sha}\`."
      echo "Reviewer ledger entries: 0. Check runs still running on this head: \`${claude_fixer_incomplete_checks:-unnamed}\`."
      echo "Ledger SHA-256: \`${claude_fixer_ledger_digest}\`."
      echo
      echo "Claude-fixer mode: the reviewer panel found nothing, so no Claude session is needed. Auto-merge waits for the check runs above: the hourly \`claude-pr-catch-all\` sweep re-reads this head's check runs and enables head-bound auto-merge once every one has completed without a failure. The reviewers do not run again. A check that fails is handed to the Claude session as a \`ci-failed\` fix, and a push starts a new review round. If the PR's base changes before then, this review no longer counts: nothing is merged on it, and the next review sweep reviews the PR again."
      echo
      echo "<!-- ai:claude-fixer-pending-checks:v1 head=${HEAD_SHA} round=${claude_fixer_round} ledger=${claude_fixer_ledger_digest} -->"
      echo "<!-- ai:claude-fixer-pending-checks:v2 head=${HEAD_SHA} round=${claude_fixer_round} ledger=${claude_fixer_ledger_digest} base_sha=${claude_fixer_base_sha} base_ref_sha256=${claude_fixer_base_ref_digest} -->"
    } > "${claude_fixer_body_file}"
    claude_fixer_post_marker_comment
    echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=pending-checks findings=0 failed_checks=0 incomplete_checks=${claude_fixer_incomplete_checks:-unnamed} base_sha=${claude_fixer_base_sha} action=wait_for_checks"
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
  REPOSITORY="${GITHUB_REPOSITORY}" bash "${SUPPORT_SCRIPTS_DIR}/post_review_comment.sh"
fi

{
  echo "## Review round ${claude_fixer_round}: findings handed to the Claude session"
  echo
  echo "Reviewed head: \`${HEAD_SHA}\` ([workflow run](${claude_fixer_run_url}))."
  if [ "${claude_fixer_ledger_state}" = "ok" ]; then
    echo "Reviewer ledger entries: ${claude_fixer_finding_count} (posted above)."
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
echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=findings findings=${claude_fixer_finding_count} ledger=${claude_fixer_ledger_state} failed_checks=${claude_fixer_failed_checks:-none}"

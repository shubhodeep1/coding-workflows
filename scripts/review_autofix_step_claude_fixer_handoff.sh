#!/usr/bin/env bash
# Body of the "Hand review round to Claude session (Claude-fixer mode)" step
# in .github/workflows/review_autofix.yml. The step sources this file in its
# own shell, so the step's if:, env: and continue-on-error: stay in the
# workflow; edit those there.
#
# Claude-fixer mode (gate output `claude_fixer`, PR head ref starting with
# `claude/implement-plan-`): the reviewer panel runs exactly as for every PR,
# but the GPT editor, the conflict resolver and the push / re-trigger tail do
# not. The /implement-plan-claude session that owns the PR fixes the findings
# instead, and this step is the hand-off:
#
#   * pre-review content conflict (AUTOFIX_PRE_REVIEW_RESOLVE=true)
#       -> one comment carrying `kind=conflict` and the unmerged paths;
#   * reviewer findings or failing check runs
#       -> the consensus ledger through scripts/post_review_comment.sh
#          (chunked PR comments), then one comment carrying `kind=findings`;
#   * nothing at all (no finding in any ledger block, no failing check)
#       -> no comment; CLAUDE_FIXER_ZERO_FINDINGS=true is exported so the
#          workflow's own auto-merge step runs, as it does when the editor
#          finds nothing to commit.
#
# The hand-off marker is
#   <!-- ai:claude-fixer-handoff:v1 kind=<findings|conflict> head=<sha> round=<n> -->
# and .claude/scripts/check_in_status.py reads it (plus the session's
# `ai:claude-fixer-verdict:v1` reply) to start the next stage session.
#
# Inputs (environment): PR_NUMBER, GH_TOKEN, GITHUB_REPOSITORY, HEAD_SHA,
# HEAD_REF, CLAUDE_FIXER_ROUND, AUTOFIX_PRE_REVIEW_RESOLVE,
# AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED, REVIEWER_CONSENSUS_FILE,
# PR_CHECK_RUNS_CONTEXT_FILE, SUPPORT_SCRIPTS_DIR, GITHUB_RUN_ID,
# GITHUB_SERVER_URL, RUNTIME_DIR.
# API calls: the ledger chunks post_review_comment.sh posts, plus one comment.
set -euo pipefail

# shellcheck source=/dev/null
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

if ! [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] || ! [[ "${HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::Claude-fixer hand-off needs a numeric PR_NUMBER and a 40-hex HEAD_SHA (PR_NUMBER=${PR_NUMBER:-} HEAD_SHA=${HEAD_SHA:-})."
  exit 1
fi
claude_fixer_round="${CLAUDE_FIXER_ROUND:-1}"
[[ "${claude_fixer_round}" =~ ^[0-9]+$ ]] || claude_fixer_round=1
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
  {
    echo "## Review round ${claude_fixer_round}: merge conflict, handed to the Claude session"
    echo
    echo "The head \`${HEAD_SHA}\` conflicts with its base branch, so the reviewer panel did not run ([workflow run](${claude_fixer_run_url}))."
    echo "Claude-fixer mode skips the GPT conflict resolver: the \`/implement-plan-claude\` session merges the base branch into \`${HEAD_REF:-the head branch}\`, resolves the conflict keeping both sides' intent, commits it as \`[claude-merge-resolve] <summary>\`, and pushes. The push starts the next review round."
    if [ -n "${AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED:-}" ]; then
      echo
      echo "Unmerged paths: \`${AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED//,/\`, \`}\`"
    fi
    echo
    echo "<!-- ai:claude-fixer-handoff:v1 kind=conflict head=${HEAD_SHA} round=${claude_fixer_round} -->"
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
if [ -s "${REVIEWER_CONSENSUS_FILE:-}" ]; then
  claude_fixer_ledger_state="ok"
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

if [ "${claude_fixer_ledger_state}" = "ok" ] && [ "${claude_fixer_finding_count}" -eq 0 ] && [ -z "${claude_fixer_failed_checks}" ]; then
  echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=none findings=0 failed_checks=0 action=auto_merge"
  echo "CLAUDE_FIXER_ZERO_FINDINGS=true" >> "$GITHUB_ENV"
  exit 0
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
  echo
  echo "Claude-fixer mode: the GPT editor did not run. The \`/implement-plan-claude\` session judges each finding against the code, then either"
  echo "- fixes the valid ones in **one** commit whose subject starts with \`[claude-autofix]\` and pushes it (the push starts the next review round; the commits count toward \`MAX_AUTOFIX_ITERATIONS\` like \`[ai-autofix]\` ones), or"
  echo "- when nothing valid is left, replies with a comment listing each finding as rejected with a reason and ending in \`<!-- ai:claude-fixer-verdict:v1 head=${HEAD_SHA} -->\`, then dispatches this workflow with \`claude_fixer_converged_head=${HEAD_SHA}\`, which enables auto-merge on this head."
  echo
  echo "<!-- ai:claude-fixer-handoff:v1 kind=findings head=${HEAD_SHA} round=${claude_fixer_round} -->"
} > "${claude_fixer_body_file}"
claude_fixer_post_marker_comment
echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=findings findings=${claude_fixer_finding_count} ledger=${claude_fixer_ledger_state} failed_checks=${claude_fixer_failed_checks:-none}"

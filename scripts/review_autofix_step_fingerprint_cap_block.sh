#!/usr/bin/env bash
# Body of the "Apply identical-failure cap outcome" step
# (job `fingerprint-cap-block`) in .github/workflows/review_autofix.yml, moved out of the
# workflow to keep it at or under 380,000 bytes, well under the
# 430,000-byte contract-test ceiling and GitHub's 512,000-byte workflow file
# limit (#10166). The job has no staged support bundle: the step sources
# this file only from the job's own .codex-workflow-src checkout, after
# "Verify workflow support source" checked it against review_support_sha
# and the wrapper re-checked HEAD against WORKFLOW_SUPPORT_REF. The step's
# name: stays in the workflow; edit it there.
# The inline body opened with `set -uo pipefail` under the runner's default
# `bash -e`, so -e was already on; the step-script contract's
# `set -euo pipefail` keeps the same options.
set -euo pipefail

if [[ ! "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
  echo "::warning::Skipping fingerprint-cap-block: invalid PR number '${PR_NUMBER}'."
  exit 0
fi
if [ "${FINGERPRINT_CAP_ALREADY_APPLIED}" = "true" ]; then
  echo "AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED pr=${PR_NUMBER} head=${PR_HEAD_SHA} fp=${FINGERPRINT_CAP_FP} count=${FINGERPRINT_CAP_COUNT}"
  exit 0
fi

# Only the verified workflow checkout may supply executable support.
support_file()
{
  local name="$1"
  if [ -f ".codex-workflow-src/scripts/${name}" ]; then
    printf '%s' ".codex-workflow-src/scripts/${name}"
  fi
}
helper="$(support_file gh_helpers.sh)"
# shellcheck disable=SC1090
[ -n "${helper}" ] && source "${helper}" 2>/dev/null
helper="$(support_file label_helpers.sh)"
# shellcheck disable=SC1090
[ -n "${helper}" ] && source "${helper}" 2>/dev/null
helper="$(support_file tg_helpers.sh)"
# shellcheck disable=SC1090
[ -n "${helper}" ] && source "${helper}" 2>/dev/null
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
if ! type ensure_label_exists >/dev/null 2>&1; then
  ensure_label_exists() {
    gh_retry gh label create "$1" --repo "$2" \
      --color "e11d48" --description "PR review/autofix could not resolve all issues — needs human intervention" \
      >/dev/null 2>&1 || true
  }
fi
if ! type set_issue_phase_label_resilient >/dev/null 2>&1; then
  set_issue_phase_label_resilient() {
    ensure_label_exists "$2" "$3" || true
    GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST "repos/$3/issues/$1/labels" -f "labels[]=$2" >/dev/null 2>&1 \
      || echo "::warning::Fallback label add failed for #$1."
  }
fi

work_dir="${RUNNER_TEMP:-/tmp}/fingerprint_cap"
mkdir -p "${work_dir}"
# The gate snapshot can predate another queued cap job. The
# per-PR concurrency lock above serializes writers; re-read comments
# after acquiring it so only the first job applies side effects.
# The PR read below cannot provide comments or their author (§15).
fresh_cap_comments_file="${work_dir}/fresh_comments.json"
if [ -z "${FINGERPRINT_CAP_MARKER_AUTHOR_LOGIN}" ] \
  || ! gh_retry gh api --paginate -X GET "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" -f per_page=100 \
    --jq '[.[] | {author_login: (.user.login // ""), body: (.body // "")}]' \
    | jq -c -s 'add // []' > "${fresh_cap_comments_file}"; then
  echo "AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED pr=${PR_NUMBER} head=${PR_HEAD_SHA} reason=fresh_cap_marker_lookup_failed"
  exit 0
fi
# #6625: a cap marker of another support version does not count; the
# support SHA must be a field of a marker on its own line, not text
# elsewhere (an inline "First error" quote can hold a whole marker).
cap_support=""
if [[ "${REVIEW_SUPPORT_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
  cap_support="${REVIEW_SUPPORT_SHA}"
elif [ -n "${REVIEW_SUPPORT_SHA:-}" ]; then
  echo "::warning::REVIEW_SUPPORT_SHA is not a 40-hex SHA; the cap marker is written without support=."
fi
if jq -e --arg author "${FINGERPRINT_CAP_MARKER_AUTHOR_LOGIN}" --arg head "${PR_HEAD_SHA}" --arg support "${cap_support}" \
  'any(.[]; ((.author_login // "" | ascii_downcase) == ($author | ascii_downcase)) and ((.body // "") | contains("<!-- review-autofix-failure-cap:v1 head=" + $head + " ")) and ($support == "" or ((.body // "") | test("(^|\\n)<!-- review-autofix-failure-cap:v1 head=" + $head + "( [^ >\\n]+)* support=" + $support + "( [^ >\\n]+)* -->[ \\r]*(\\n|$)"))))' \
  "${fresh_cap_comments_file}" >/dev/null; then
  echo "AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED pr=${PR_NUMBER} head=${PR_HEAD_SHA} fp=${FINGERPRINT_CAP_FP} count=${FINGERPRINT_CAP_COUNT}"
  exit 0
fi
pr_json="${work_dir}/pr_payload.json"
if ! gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" > "${pr_json}" 2>/dev/null; then
  echo "::warning::Could not fetch PR #${PR_NUMBER}; labeling the PR itself ai:review-blocked without linked-issue resolution."
  printf '{}' > "${pr_json}"
fi
pr_state="$(jq -r '.state // ""' "${pr_json}" 2>/dev/null || echo "")"
current_head="$(jq -r '.head.sha // ""' "${pr_json}" 2>/dev/null || echo "")"
if [ "${pr_state}" = "closed" ]; then
  echo "AUTOFIX_FINGERPRINT cap=skipped pr=${PR_NUMBER} head=${PR_HEAD_SHA} reason=pr_closed"
  exit 0
fi
if [ -n "${current_head}" ] && [ "${current_head}" != "${PR_HEAD_SHA}" ]; then
  # A push landed after the gate: the new head has no identical
  # failures yet and gets its own run.
  echo "AUTOFIX_FINGERPRINT cap=skipped pr=${PR_NUMBER} head=${PR_HEAD_SHA} current_head=${current_head} reason=head_moved"
  exit 0
fi

# Linked issues: strict current-repo closing-keyword refs and
# repo-scoped issue URLs in the PR title/body (the same fallback the
# workflow-failure review-blocked step uses).
issue_numbers=""
pr_text="$(jq -r '[.title // "", .body // ""] | join(" ")' "${pr_json}" 2>/dev/null || echo "")"
if type extract_repo_scoped_issue_refs_from_text >/dev/null 2>&1; then
  issue_numbers="$(extract_repo_scoped_issue_refs_from_text "${REPOSITORY}" "${pr_text}" || true)"
else
  echo "::warning::extract_repo_scoped_issue_refs_from_text unavailable; labeling the PR itself."
fi

ensure_label_exists "ai:review-blocked" "${REPOSITORY}" || true
labeled_summary=""
if [ -n "${issue_numbers}" ]; then
  while IFS= read -r issue_number; do
    [ -n "${issue_number}" ] || continue
    echo "Setting ai:review-blocked on issue #${issue_number} (identical-failure cap)"
    set_issue_phase_label_resilient "${issue_number}" "ai:review-blocked" "${REPOSITORY}"
    labeled_summary="${labeled_summary:+${labeled_summary}, }#${issue_number}"
  done <<< "${issue_numbers}"
  labeled_summary="linked issue(s) ${labeled_summary}"
else
  if ! GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST "repos/${REPOSITORY}/issues/${PR_NUMBER}/labels" -f "labels[]=ai:review-blocked" >/dev/null 2>&1; then
    echo "::warning::Failed to add ai:review-blocked to PR #${PR_NUMBER}."
  fi
  labeled_summary="this pull request (no linked issue found)"
fi

cap_marker="<!-- review-autofix-failure-cap:v1 head=${PR_HEAD_SHA} fp=${FINGERPRINT_CAP_FP} reason=${FINGERPRINT_CAP_REASON} count=${FINGERPRINT_CAP_COUNT}${cap_support:+ support=${cap_support}} -->"
cap_title="identical failure repeated"
cap_first_error=""
cap_lead="The last ${FINGERPRINT_CAP_COUNT} review/autofix runs on head \`${PR_HEAD_SHA:0:12}\` failed the same way"
if [ "${FINGERPRINT_CAP_NON_RETRYABLE:-false}" = "true" ]; then
  # One non-retryable failure is enough (PR #6438). Quote the
  # failed run's "First error" line, which names e.g. the host-only
  # conflicted paths, from the trusted author's newest failure
  # comment for this head (already fetched above, §15).
  cap_title="non-retryable failure"
  cap_lead="The last review/autofix run on head \`${PR_HEAD_SHA:0:12}\` failed with a non-retryable reason"
  cap_first_error="$(jq -r --arg author "${FINGERPRINT_CAP_MARKER_AUTHOR_LOGIN}" --arg head "${PR_HEAD_SHA}" \
    '[.[] | select((.author_login // "" | ascii_downcase) == ($author | ascii_downcase)) | (.body // "") | select(test("(\\A|\\n)<!-- review-autofix-failure:v1 head=" + $head + "( [^ >\\n]+)* -->\\s*\\z"))] | last // "" | (capture("[*][*]First error:[*][*] `(?<e>[^`\n]{1,400})`")? // {e: ""}).e' \
    "${fresh_cap_comments_file}" 2>/dev/null || true)"
fi
BODY="$(cat <<EOF
**AI review/autofix stopped: ${cap_title}**

${cap_lead} (failure reason \`${FINGERPRINT_CAP_REASON}\`, fingerprint \`${FINGERPRINT_CAP_FP:0:16}\`). Another run on this head would fail again, so this run stopped in the gate before any reviewer or editor model call.${cap_first_error:+

**First error:** \`${cap_first_error}\`}

- Labeled \`ai:review-blocked\`: ${labeled_summary}.
- This failure is also sent to workflow failure heal (run log line \`WORKFLOW_HEAL_AUTOFIX_REPORT\`).
- A push to this branch (a new head) resets the count.
- Operators: set the repository variable \`REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED=false\` to disable the cap, or raise \`REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL\` (currently ${FINGERPRINT_CAP_MAX}).

[View workflow run](${REPORT_RUN_URL})

${cap_marker}
EOF
)"
if ! gh_retry gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${BODY}" >/dev/null 2>&1; then
  echo "::warning::Failed to post the identical-failure cap comment on PR #${PR_NUMBER}."
fi
echo "AUTOFIX_FINGERPRINT cap=applied pr=${PR_NUMBER} head=${PR_HEAD_SHA} fp=${FINGERPRINT_CAP_FP} count=${FINGERPRINT_CAP_COUNT} labeled=$(printf '%s' "${labeled_summary}" | tr -c 'A-Za-z0-9#,_.-' '_')"

if type tg_send_msg >/dev/null 2>&1; then
  MSG="⚠️ Review/autofix stopped: identical failure repeated ${FINGERPRINT_CAP_COUNT}x on #${PR_NUMBER}"
  if [ "${FINGERPRINT_CAP_NON_RETRYABLE:-false}" = "true" ]; then
    MSG="⚠️ Review/autofix stopped: non-retryable failure on #${PR_NUMBER}"
  fi
  MSG+=$'\n'
  MSG+="Reason: ${FINGERPRINT_CAP_REASON} — labeled ai:review-blocked (${labeled_summary})"
  MSG+=$'\n'
  MSG+="PR: ${GITHUB_SERVER_URL:-https://github.com}/${REPOSITORY}/pull/${PR_NUMBER}"
  MSG+=$'\n'
  MSG+="Run: ${REPORT_RUN_URL}"
  # DEBUG: the ai:review-blocked label hands the PR to the
  # review-blocked judge, whose outcome alert pages when a human
  # is needed.
  tg_send_msg "${MSG}" "DEBUG" >/dev/null || true
fi

# Heal report: the cap is the streak, so the reporter sends it on
# the first call. The evidence is the gate's marker data, written to
# a *log*.txt file because the reporter tails those from RUNTIME_DIR.
# The comments fetched above carry the failed runs' markers; with the
# marker author the reporter lists those runs in run_refs, so the
# intake reads their logs instead of this run's (which has no failed
# job). No extra API call (§15).
reporter="$(support_file workflow_failure_heal_autofix_report.sh)"
heal_py="$(support_file workflow_failure_heal.py)"
if [ -z "${reporter}" ] || [ -z "${heal_py}" ]; then
  echo "WORKFLOW_HEAL_AUTOFIX_REPORT skip reason=reporter_missing path=${reporter:-workflow_failure_heal_autofix_report.sh}"
else
  {
    echo "identical_failure_cap: ${FINGERPRINT_CAP_COUNT} identical review/autofix failures on head ${PR_HEAD_SHA} (max ${FINGERPRINT_CAP_MAX})"
    echo "repeated failure_reason=${FINGERPRINT_CAP_REASON}"
    echo "failure_fingerprint=${FINGERPRINT_CAP_FP}"
  } > "${work_dir}/fingerprint_cap_log.txt"
  AUTOFIX_FAILURE_REASON="identical_failure_cap" \
    AUTOFIX_FAILURE_FP="${FINGERPRINT_CAP_FP}" \
    WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK="1" \
    WORKFLOW_HEAL_PY="${heal_py}" \
    RUNTIME_DIR="${work_dir}" \
    PR_PAYLOAD_FILE="${pr_json}" \
    PR_ISSUE_COMMENTS_FILE="${fresh_cap_comments_file}" \
    AUTOFIX_FAILURE_MARKER_AUTHOR="${FINGERPRINT_CAP_MARKER_AUTHOR_LOGIN}" \
    REPORT_WRAPPER_SHA="${WORKFLOW_SUPPORT_REF}" \
    REPORT_SUMMARY_LINE_FILE="${work_dir}/no_summary_line.txt" \
    bash "${reporter}" || true
fi

{
  echo "## Identical-failure cap tripped"
  echo ""
  echo "- **PR:** #${PR_NUMBER} at \`${PR_HEAD_SHA}\`"
  echo "- **Identical failures:** ${FINGERPRINT_CAP_COUNT} (max ${FINGERPRINT_CAP_MAX}), reason \`${FINGERPRINT_CAP_REASON}\`"
  echo "- **Labeled ai:review-blocked:** ${labeled_summary}"
  echo "- **Reviewer panel + editor cycle:** SKIPPED"
} >> "${GITHUB_STEP_SUMMARY}"

#!/usr/bin/env bash
# Initialize runtime workspace for review_autofix.yml.
set -euo pipefail

if [[ "${RUNNER_TEMP:-}" != /* ]] || [ ! -d "${RUNNER_TEMP}" ] || [ ! -w "${RUNNER_TEMP}" ]; then
  echo "::error::RUNNER_TEMP must be an available absolute writable directory." >&2
  exit 1
fi
RUNTIME_DIR="$(umask 077 && mktemp -d "${RUNNER_TEMP%/}/codex-pr-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.XXXXXX")"
PREVIOUS_REVIEWS_DIR="${RUNTIME_DIR}/previous_reviews"
RUNTIME_CONTEXT_DIR="${RUNTIME_DIR}/runtime_context"
job_start_epoch="$(date +%s)"
review_soft_deadline_minutes="${REVIEW_SOFT_DEADLINE_MINUTES:-210}"
review_soft_deadline_minutes_raw="${review_soft_deadline_minutes}"

case "${review_soft_deadline_minutes}" in
  ''|*[!0-9]*)
    echo "::warning::Invalid REVIEW_SOFT_DEADLINE_MINUTES='${review_soft_deadline_minutes}'; defaulting to 210." >&2
    review_soft_deadline_minutes="210"
    ;;
  *)
    review_soft_deadline_minutes="$(( 10#${review_soft_deadline_minutes} ))"
    if [ "${review_soft_deadline_minutes}" -le 0 ]; then
      echo "::warning::Invalid REVIEW_SOFT_DEADLINE_MINUTES='${review_soft_deadline_minutes_raw}'; defaulting to 210." >&2
      review_soft_deadline_minutes="210"
    fi
    ;;
esac

codex_run_budget_total_secs="$(( review_soft_deadline_minutes * 60 ))"
codex_run_budget_soft_deadline_epoch="$(( job_start_epoch + codex_run_budget_total_secs ))"

mkdir -p "${PREVIOUS_REVIEWS_DIR}" "${RUNTIME_CONTEXT_DIR}"

{
  echo "JOB_START_EPOCH=${job_start_epoch}"
  echo "REVIEW_SOFT_DEADLINE_MINUTES=${review_soft_deadline_minutes}"
  echo "CODEX_RUN_BUDGET_START_EPOCH=${job_start_epoch}"
  echo "CODEX_RUN_BUDGET_SOFT_DEADLINE_EPOCH=${codex_run_budget_soft_deadline_epoch}"
  echo "CODEX_RUN_BUDGET_TOTAL_SECS=${codex_run_budget_total_secs}"
  echo "RUNTIME_DIR=${RUNTIME_DIR}"
  echo "PREVIOUS_REVIEWS_DIR=${PREVIOUS_REVIEWS_DIR}"
  echo "RUNTIME_CONTEXT_DIR=${RUNTIME_CONTEXT_DIR}"
  echo "PR_PAYLOAD_FILE=${RUNTIME_DIR}/pr_payload.json"
  echo "PR_META_FILE=${RUNTIME_DIR}/pr_meta.json"
  echo "PR_DIFF_FILE=${RUNTIME_DIR}/pr_diff.patch"
  echo "ORIGINAL_PR_DIFF_FILE=${RUNTIME_DIR}/original_pr_diff.patch"
  echo "PR_ISSUE_COMMENTS_FILE=${RUNTIME_DIR}/pr_issue_comments.json"
  echo "AGENTS_MD_MATERIALITY_RESULT_FILE=${RUNTIME_DIR}/agents_md_materiality_result.json"
  echo "AGENTS_MD_MATERIALITY_COMMENT_FILE=${RUNTIME_DIR}/agents_md_materiality_comment.md"
  echo "PR_REVIEWS_FILE=${RUNTIME_DIR}/pr_reviews.json"
  echo "PR_REVIEW_COMMENTS_FILE=${RUNTIME_DIR}/pr_review_comments.json"
  echo "PR_ALL_COMMENTS_CONTEXT_FILE=${RUNTIME_DIR}/pr_all_comments_context.txt"
  echo "PR_CHECK_RUNS_CONTEXT_FILE=${RUNTIME_DIR}/pr_check_runs_context.txt"
  echo "LAST_RUN_DIFF_FILE=${RUNTIME_DIR}/last_run_diff.patch"
  echo "LAST_RUN_CHANGED_FILES_FILE=${RUNTIME_DIR}/last_run_changed_files.txt"
  echo "PR_CHANGED_FILES_FILE=${RUNTIME_DIR}/pr_changed_files.txt"
  echo "LAST_COMMIT_STAT_FILE=${RUNTIME_DIR}/last_commit_stat.txt"
  echo "LAST_RUN_DIFF_STAT_FILE=${RUNTIME_DIR}/last_run_diff_stat.txt"
  echo "SLOP_SCAN_FINDINGS_FILE=${GITHUB_WORKSPACE}/.ai/slop_scan/findings.json"
  echo "REVIEWER_PROMPT_BODY_FILE=${RUNTIME_DIR}/reviewer_prompt_body.txt"
  echo "EDITOR_PROMPT_BODY_FILE=${RUNTIME_DIR}/editor_prompt_body.txt"
  echo "REVIEWER_PROMPT_FILE=${RUNTIME_DIR}/reviewer_prompt.txt"
  echo "EDITOR_PROMPT_FILE=${RUNTIME_DIR}/editor_prompt.txt"
  echo "EDITOR_SUMMARY_FILE=${RUNTIME_DIR}/editor_summary.txt"
  echo "PR_EDITOR_COMMENT_FILE=${RUNTIME_DIR}/pr_editor_comment.txt"
  echo "REVIEWER_CONSENSUS_FILE=${RUNTIME_DIR}/reviewer_consensus.txt"
  echo "LINKED_ISSUE_CONTEXT_FILE=${RUNTIME_DIR}/linked_issue_context.txt"
  echo "LINKED_ISSUE_METADATA_FILE=${RUNTIME_DIR}/linked_issue_metadata.json"
  echo "MEMORY_CONTEXT_FILE=${RUNTIME_DIR}/memory_context.txt"
  echo "REVIEWER_SEMBLE_QUERY_FILE=${RUNTIME_DIR}/reviewer_semble_query.txt"
  echo "EDITOR_SEMBLE_QUERY_FILE=${RUNTIME_DIR}/editor_semble_query.txt"
  echo "CONFLICT_RESOLVER_SEMBLE_QUERY_FILE=${RUNTIME_DIR}/conflict_resolver_semble_query.txt"
  echo "CONFLICT_RESOLVER_PROMPT_FILE=${RUNTIME_DIR}/conflict_resolver_prompt.txt"
  echo "CONFLICT_RESOLVER_SUMMARY_FILE=${RUNTIME_DIR}/conflict_resolver_summary.txt"
  echo "COMMITTED_FILES_FILE=${RUNTIME_DIR}/committed_files.txt"
  echo "SEMBLE_AVAILABLE=false"
  echo "SEMBLE_BIN="
  echo "SEMBLE_INDEX_AVAILABLE=false"
  echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"
  echo "SERENA_AVAILABLE=false"
  echo "SERENA_PROJECT_PREEXISTED=false"
  echo "SERENA_PROJECT_BOOTSTRAP_HASH="
  echo "HAS_PR_DIFF=false"
  echo "PR_DIFF_SOURCE=uninitialized"
  echo "PR_DIFF_ATTEMPTED_PATHS=gh_pr_diff:${RUNTIME_DIR}/pr_diff.patch"
} >> "$GITHUB_ENV"

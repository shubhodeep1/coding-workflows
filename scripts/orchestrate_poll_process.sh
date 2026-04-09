#!/usr/bin/env bash
# orchestrate_poll_process.sh — Process active orchestrator tracking issues.
# Extracted from orchestrate_poll.yml to stay within GitHub Actions
# expression length limits (21 000 chars max per run block).
#
# Required env vars (set by the workflow step):
#   RUNTIME_DIR, STATE_FILE, JUDGE_PROMPT_FILE, JUDGE_OUTPUT_FILE,
#   GH_TOKEN, OPENROUTER_API_KEY, GITHUB_REPOSITORY,
#   MODEL_EDITOR, MODEL_REASONING_EFFORT_JUDGE,
#   TG_BOT_SECRET, TG_ADMIN_CHAT_ID, TOOL_CALL_BUDGET_JUDGE,
#   SERENA_VERSION, SERENA_LANGUAGES, SERENA_DISABLED, SERENA_IGNORED_DIRS

set -euo pipefail

# ---------------------------------------------------------------
# Helper: Telegram (tracked via tg_helpers.sh)
# ---------------------------------------------------------------
# shellcheck source=gh_helpers.sh
if [ -f "scripts/gh_helpers.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/gh_helpers.sh
fi
# shellcheck source=tg_helpers.sh
if [ -f "scripts/tg_helpers.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/tg_helpers.sh
fi

# _gh_url constructs a full GitHub URL for the current repository.
_gh_url() {
  if [ -z "${GITHUB_REPOSITORY:-}" ]; then
    printf ''
    return
  fi
  printf '%s/%s/%s' "${GITHUB_SERVER_URL:-https://github.com}" "${GITHUB_REPOSITORY}" "$1"
}

# tg_notify wraps tg_send_tracked using the current TRACKING_NUM.
# TRACKING_NUM is set inside the main per-issue loop below.
# Automatically appends tracking issue link and Actions run link.
tg_notify() {
  local msg="$1"
  local level="${2:-CRITICAL}"
  local tracking_url run_url

  if [ -n "${TRACKING_NUM:-}" ] && [ "${TRACKING_NUM}" != "0" ]; then
    tracking_url="$(_gh_url "issues/${TRACKING_NUM}")"
    if [ -n "${tracking_url}" ]; then
      msg+=$'\n'"Tracking: ${tracking_url}"
    fi
  fi
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    run_url="$(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
    if [ -n "${run_url}" ]; then
      msg+=$'\n'"Run: ${run_url}"
    fi
  fi
  if [ -n "${TRACKING_NUM:-}" ]; then
    tg_send_tracked "${TRACKING_NUM}" "${msg}" "${level}"
  else
    # Fallback: untracked send (no issue context yet)
    tg_send_msg "${msg}" "${level}" >/dev/null
  fi
}

# ---------------------------------------------------------------
# Helper: GitHub API with retry
# ---------------------------------------------------------------
# gh_retry is provided by scripts/gh_helpers.sh (rate-limit-aware).
# Fallback definition in case gh_helpers.sh was not sourced.
if ! type gh_retry >/dev/null 2>&1; then
  gh_retry() { "$@"; }
fi

is_truthy() {
  local value
  value="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "${value}" in
    1|true|yes|on)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

# ---------------------------------------------------------------
# Helper: Check whether all check-runs on a PR's head commit have
# completed.  Returns 0 when every check-run has status "completed"
# and an acceptable conclusion (success/neutral/skipped/cancelled),
# 1 otherwise (including API errors).  Callers should skip the merge
# when this returns non-zero so we never merge while checks (e.g.
# autofix) are still running.
# Usage:  _pr_checks_completed <PR_NUMBER>
# ---------------------------------------------------------------
_pr_checks_completed()
{
	local pr_number="$1"
	local head_sha
	head_sha="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}" \
		--jq '.head.sha' 2>/dev/null || echo "")"
	if [ -z "${head_sha}" ] || [ "${head_sha}" = "null" ]; then
		echo "  [check-runs] Could not resolve head SHA for PR #${pr_number}. Skipping merge."
		return 1
	fi

	local incomplete
	incomplete="$(gh api "repos/${GITHUB_REPOSITORY}/commits/${head_sha}/check-runs?per_page=100" \
		--jq '[.check_runs[] | select(.status != "completed" or (.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped" and .conclusion != "cancelled"))] | length' 2>/dev/null || echo "")"
	if [ -z "${incomplete}" ] || [ "${incomplete}" = "null" ]; then
		echo "  [check-runs] Could not query check-runs for PR #${pr_number} (SHA ${head_sha:0:7}). Skipping merge."
		return 1
	fi

	if [ "${incomplete}" -gt 0 ]; then
		echo "  [check-runs] PR #${pr_number} has ${incomplete} blocking check-run(s) (SHA ${head_sha:0:7}). Skipping merge."
		return 1
	fi

		echo "  [check-runs] All check-runs completed and acceptable for PR #${pr_number} (SHA ${head_sha:0:7}). Proceeding with merge."
	return 0
}

ENABLE_VALIDATION_RAW="${ENABLE_VALIDATION:-true}"
ENABLE_VALIDATION="false"
if is_truthy "${ENABLE_VALIDATION_RAW}"; then
  ENABLE_VALIDATION="true"
fi

MAX_VALIDATE_CYCLES="${MAX_VALIDATE_CYCLES:-3}"
if ! [[ "${MAX_VALIDATE_CYCLES}" =~ ^[0-9]+$ ]] || [ "${MAX_VALIDATE_CYCLES}" -lt 1 ]; then
  echo "::warning::MAX_VALIDATE_CYCLES must be a positive integer; defaulting to 3"
  MAX_VALIDATE_CYCLES="3"
fi

# Stall detection settings
STALL_THRESHOLD_MINUTES="${STALL_THRESHOLD_MINUTES:-120}"
if ! [[ "${STALL_THRESHOLD_MINUTES}" =~ ^[0-9]+$ ]] || [ "${STALL_THRESHOLD_MINUTES}" -lt 1 ]; then
  echo "::warning::STALL_THRESHOLD_MINUTES must be a positive integer; defaulting to 120"
  STALL_THRESHOLD_MINUTES="120"
fi

# Per-phase stall thresholds (override the global fallback above).
# Each var maps to a pipeline phase label.  Unset vars use the built-in
# defaults in orchestrate_lib.py (60 min for lightweight phases, 120 min
# for heavy phases).
_validate_phase_threshold() {
  local var_name="$1"
  local val="${!var_name:-}"
  if [ -n "${val}" ]; then
    if ! [[ "${val}" =~ ^[0-9]+$ ]] || [ "${val}" -lt 1 ]; then
      echo "::warning::${var_name} must be a positive integer; ignoring invalid value '${val}'"
      eval "${var_name}="
    fi
  fi
}

STALL_THRESHOLD_NO_LABELS_MINUTES="${STALL_THRESHOLD_NO_LABELS_MINUTES:-}"
STALL_THRESHOLD_CLARIFICATION_MINUTES="${STALL_THRESHOLD_CLARIFICATION_MINUTES:-}"
STALL_THRESHOLD_PLANNING_MINUTES="${STALL_THRESHOLD_PLANNING_MINUTES:-}"
STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES="${STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES:-}"
STALL_THRESHOLD_IMPLEMENTING_MINUTES="${STALL_THRESHOLD_IMPLEMENTING_MINUTES:-}"
STALL_THRESHOLD_DONE_MINUTES="${STALL_THRESHOLD_DONE_MINUTES:-}"
STALL_THRESHOLD_READY_TO_MERGE_MINUTES="${STALL_THRESHOLD_READY_TO_MERGE_MINUTES:-}"

_validate_phase_threshold STALL_THRESHOLD_NO_LABELS_MINUTES
_validate_phase_threshold STALL_THRESHOLD_CLARIFICATION_MINUTES
_validate_phase_threshold STALL_THRESHOLD_PLANNING_MINUTES
_validate_phase_threshold STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES
_validate_phase_threshold STALL_THRESHOLD_IMPLEMENTING_MINUTES
_validate_phase_threshold STALL_THRESHOLD_DONE_MINUTES
_validate_phase_threshold STALL_THRESHOLD_READY_TO_MERGE_MINUTES

# Build the JSON dict for per-phase overrides (only include vars that are set).
_build_phase_thresholds_json() {
  local parts=()
  [ -n "${STALL_THRESHOLD_NO_LABELS_MINUTES:-}" ] && parts+=("\"no_labels\":${STALL_THRESHOLD_NO_LABELS_MINUTES}")
  [ -n "${STALL_THRESHOLD_CLARIFICATION_MINUTES:-}" ] && parts+=("\"ai:clarification\":${STALL_THRESHOLD_CLARIFICATION_MINUTES}")
  [ -n "${STALL_THRESHOLD_PLANNING_MINUTES:-}" ] && parts+=("\"ai:planning\":${STALL_THRESHOLD_PLANNING_MINUTES}")
  [ -n "${STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES:-}" ] && parts+=("\"ai:awaiting-approval\":${STALL_THRESHOLD_AWAITING_APPROVAL_MINUTES}")
  [ -n "${STALL_THRESHOLD_IMPLEMENTING_MINUTES:-}" ] && parts+=("\"ai:implementing\":${STALL_THRESHOLD_IMPLEMENTING_MINUTES}")
  [ -n "${STALL_THRESHOLD_DONE_MINUTES:-}" ] && parts+=("\"ai:done\":${STALL_THRESHOLD_DONE_MINUTES}")
  [ -n "${STALL_THRESHOLD_READY_TO_MERGE_MINUTES:-}" ] && parts+=("\"ai:ready-to-merge\":${STALL_THRESHOLD_READY_TO_MERGE_MINUTES}")

  if [ "${#parts[@]}" -eq 0 ]; then
    echo ""
    return
  fi

  local IFS=','
  echo "{${parts[*]}}"
}

PHASE_THRESHOLDS_JSON="$(_build_phase_thresholds_json)"

MAX_STALL_RECOVERIES_PER_ISSUE="${MAX_STALL_RECOVERIES_PER_ISSUE:-5}"
if ! [[ "${MAX_STALL_RECOVERIES_PER_ISSUE}" =~ ^[0-9]+$ ]] || [ "${MAX_STALL_RECOVERIES_PER_ISSUE}" -lt 1 ]; then
  echo "::warning::MAX_STALL_RECOVERIES_PER_ISSUE must be a positive integer; defaulting to 5"
  MAX_STALL_RECOVERIES_PER_ISSUE="5"
fi

MAX_RECOVERY_ATTEMPTS="${MAX_RECOVERY_ATTEMPTS:-3}"
if ! [[ "${MAX_RECOVERY_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${MAX_RECOVERY_ATTEMPTS}" -lt 1 ]; then
  echo "::warning::MAX_RECOVERY_ATTEMPTS must be a positive integer; defaulting to 3"
  MAX_RECOVERY_ATTEMPTS="3"
fi

MAX_VALIDATION_RECOVERY_ATTEMPTS="${MAX_VALIDATION_RECOVERY_ATTEMPTS:-2}"
if ! [[ "${MAX_VALIDATION_RECOVERY_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${MAX_VALIDATION_RECOVERY_ATTEMPTS}" -lt 0 ]; then
  echo "::warning::MAX_VALIDATION_RECOVERY_ATTEMPTS must be a non-negative integer; defaulting to 2"
  MAX_VALIDATION_RECOVERY_ATTEMPTS="2"
fi

post_tracking_comment() {
  local comment_body="$1"
  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
    -f body="${comment_body}" >/dev/null || true
}

post_state_comment() {
  local state_comment
  state_comment="<!-- ORCHESTRATOR_STATE_V1
$(cat "${STATE_FILE}")
ORCHESTRATOR_STATE_V1 -->"
  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
    -f body="${state_comment}" >/dev/null || true
}

ensure_label_exists() {
  local label_name="$1"
  local contract_file=".github/ai/label_contract.v1.json"
  local color="1d76db"
  local description="AI workflow label"
  local contract_color=""
  local contract_description=""

  if [ "${label_name}" = "ai:closed" ]; then
    color="6a737d"
    description="Linked PR closed without merge"
  elif [ "${label_name}" = "ai:ready-to-merge" ]; then
    color="0e8a16"
    description="PR review complete and ready to merge"
  fi

  if [ -f "${contract_file}" ]; then
    contract_color="$(jq -r --arg lbl "${label_name}" '.labels[$lbl].color // empty' "${contract_file}" 2>/dev/null || echo "")"
    contract_description="$(jq -r --arg lbl "${label_name}" '.labels[$lbl].description // empty' "${contract_file}" 2>/dev/null || echo "")"
    [ -n "${contract_color}" ] && color="${contract_color}"
    [ -n "${contract_description}" ] && description="${contract_description}"
  fi

  # Check if label already exists to avoid futile retries (gh label create
  # returns a non-zero exit code for existing labels, which is not transient).
  local encoded_name
  encoded_name="$(printf '%s' "${label_name}" | jq -sRr @uri)"
  if gh api "repos/${GITHUB_REPOSITORY}/labels/${encoded_name}" >/dev/null 2>&1; then
    return 0
  fi

  gh_retry gh label create "${label_name}" \
    --repo "${GITHUB_REPOSITORY}" \
    --color "${color}" \
    --description "${description}" >/dev/null || true
}

set_tracking_phase_label() {
  local phase_label="$1"
  local contract_file=".github/ai/label_contract.v1.json"

  ensure_label_exists "${phase_label}"

  if [ -f "${contract_file}" ]; then
    local phase_changes
    if phase_changes="$(python3 scripts/ai_labels.py resolve-phase --contract-file "${contract_file}" --phase "${phase_label}" 2>/dev/null)"; then
      while IFS= read -r remove_label; do
        [ -n "${remove_label}" ] || continue
        gh_retry gh issue edit "${TRACKING_NUM}" \
          --repo "${GITHUB_REPOSITORY}" \
          --remove-label "${remove_label}" >/dev/null || true
      done < <(echo "${phase_changes}" | jq -r '.remove[]?')

      while IFS= read -r add_label; do
        [ -n "${add_label}" ] || continue
        ensure_label_exists "${add_label}"
        gh_retry gh issue edit "${TRACKING_NUM}" \
          --repo "${GITHUB_REPOSITORY}" \
          --add-label "${add_label}" >/dev/null || true
      done < <(echo "${phase_changes}" | jq -r '.add[]?')
      return 0
    fi
  fi

  gh_retry gh issue edit "${TRACKING_NUM}" \
    --repo "${GITHUB_REPOSITORY}" \
    --add-label "${phase_label}" >/dev/null || true
}

get_issue_labels_json() {
  local issue_num="$1"
  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/labels" --jq '[.[].name]' 2>/dev/null || echo '[]'
}

has_label() {
  local labels_json="$1"
  local label="$2"
  echo "${labels_json}" | jq -e --arg label "${label}" 'index($label) != null' >/dev/null 2>&1
}

integration_branch_exists() {
  local branch_name="$1"
  [ -n "${branch_name}" ] || return 1
  local branch_ref
  local gh_error
  branch_ref="$(printf '%s' "${branch_name}" | jq -sRr @uri)"

  if gh_error="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${branch_ref}" 2>&1 >/dev/null)"; then
    return 0
  fi

  if printf '%s' "${gh_error}" | grep -Eqi '(^gh: Not Found|HTTP 404|404 Not Found|status code 404|\bnot found\b)'; then
    return 1
  fi

  echo "::warning::Unable to verify integration branch '${branch_name}' due to GitHub API error; assuming it still exists." >&2
  return 0
}

mark_integration_branch_missing_failed() {
  local integration_branch="$1"
  local reason="Integration branch '${integration_branch}' is missing. It may have been deleted externally. Manual intervention required."
  jq --arg reason "${reason}" --arg branch "${integration_branch}" \
    '.status = "failed" |
     .final_merge_status = "failed" |
     .integration_branch = $branch |
     .final_merge_error = $reason' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment
  post_tracking_comment "## ❌ Integration branch missing\n\n${reason}"
  tg_notify "❌ Project #${TRACKING_NUM} failed: missing integration branch '${integration_branch}'."
  tg_cleanup_msgs "${TRACKING_NUM}"
}

sync_default_into_integration_branch() {
  local integration_branch="$1"
  local default_branch="$2"

  if [ -z "${integration_branch}" ]; then
    return 0
  fi

  if ! integration_branch_exists "${integration_branch}"; then
    mark_integration_branch_missing_failed "${integration_branch}"
    return 1
  fi

  local merge_error
  if merge_error="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/merges" \
    -f base="${integration_branch}" \
    -f head="${default_branch}" \
    -f commit_message="chore: sync ${default_branch} into ${integration_branch}" 2>&1 >/dev/null)"; then
    return 0
  fi

  if ! printf '%s' "${merge_error}" | grep -Eqi '(HTTP 409|status code 409|merge conflict|conflict)'; then
    echo "::warning::Unable to sync '${default_branch}' into '${integration_branch}' due to transient GitHub API error; will retry next poll." >&2
    return 0
  fi

  post_tracking_comment "## ⚠️ Integration sync warning\n\nUnable to sync \\`${default_branch}\\` into \\`${integration_branch}\\`. This is usually a merge conflict. The project can continue, but final merge may require manual conflict resolution."
  tg_notify "⚠️ Sync warning for #${TRACKING_NUM}: could not merge '${default_branch}' into '${integration_branch}'."
  return 0
}

finalize_integration_merge_if_needed() {
  local integration_branch="$1"
  local default_branch="$2"
  local project_title="$3"
  local final_pr

  if [ -z "${integration_branch}" ]; then
    return 0
  fi

  local final_merge_status
  final_merge_status="$(jq -r '.final_merge_status // "pending"' "${STATE_FILE}")"
  if [ "${final_merge_status}" = "merged" ]; then
    return 0
  fi

  final_pr="$(jq -r '.final_merge_pr // empty' "${STATE_FILE}")"
  if [ -n "${final_pr}" ] && [ "${final_pr}" != "null" ]; then
    local existing_pr_state
    local existing_pr_merged
    existing_pr_state="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' 2>/dev/null || echo "")"
    existing_pr_merged="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged' 2>/dev/null || echo "")"
    if [ "${existing_pr_state}" = "closed" ] && [ "${existing_pr_merged}" = "true" ]; then
      jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "merged"' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment
      return 0
    fi
  fi

  if ! integration_branch_exists "${integration_branch}"; then
    mark_integration_branch_missing_failed "${integration_branch}"
    return 1
  fi

  if [ -z "${final_pr}" ] || [ "${final_pr}" = "null" ]; then
    final_pr="$(gh pr list \
      --repo "${GITHUB_REPOSITORY}" \
      --state open \
      --base "${default_branch}" \
      --head "${integration_branch}" \
      --json number \
      --jq '.[0].number // empty' 2>/dev/null || true)"
  fi

  if [ -z "${final_pr}" ]; then
    local final_pr_url
    final_pr_url="$(gh pr create \
      --repo "${GITHUB_REPOSITORY}" \
      --base "${default_branch}" \
      --head "${integration_branch}" \
      --title "feat: ${project_title}" \
      --body "Squash merge of orchestrator project #${TRACKING_NUM}.\n\nRefs #${TRACKING_NUM}" 2>/dev/null || true)"
    final_pr="$(printf '%s\n' "${final_pr_url}" | grep -oE '/pull/[0-9]+' | tail -n1 | cut -d/ -f3 || true)"
  fi

  if [ -z "${final_pr}" ]; then
    post_tracking_comment "## ⚠️ Final merge could not start\n\nUnable to create or locate the final integration PR from \\`${integration_branch}\\` to \\`${default_branch}\\`."
    return 1
  fi

  jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "pending"' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment

  local pr_state
  local pr_mergeable
  local pr_merged
  pr_state="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' 2>/dev/null || echo "")"
  pr_mergeable="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' 2>/dev/null || echo "")"
  pr_merged="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged' 2>/dev/null || echo "")"

  if [ "${pr_state}" = "closed" ] && [ "${pr_merged}" = "true" ]; then
    jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "merged"' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    return 0
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" = "false" ]; then
    jq --argjson final_pr "${final_pr}" \
      '.status = "merge_conflict" | .final_merge_pr = $final_pr | .final_merge_status = "conflict"' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    post_tracking_comment "## ⚠️ Final merge conflict\n\nFinal PR #${final_pr} from \\`${integration_branch}\\` to \\`${default_branch}\\` has merge conflicts. Resolve conflicts manually, then re-run the poller."
    tg_notify "⚠️ Final merge conflict for #${TRACKING_NUM} (PR #${final_pr})."
    return 1
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" != "true" ]; then
    echo "  [final-merge] PR #${final_pr} mergeability is '${pr_mergeable:-unknown}'. Will retry next poll."
    return 1
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" = "true" ] && ! _pr_checks_completed "${final_pr}"; then
    echo "  [final-merge] Required checks not complete for PR #${final_pr}. Will retry next poll."
    return 1
  fi

  if gh pr merge "${final_pr}" --repo "${GITHUB_REPOSITORY}" --squash --delete-branch >/dev/null 2>&1; then
    jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "merged"' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    post_tracking_comment "## ✅ Final merge complete\n\nIntegration branch \\`${integration_branch}\\` was squash-merged into \\`${default_branch}\\` via PR #${final_pr}."
    return 0
  fi

  pr_state="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' 2>/dev/null || echo "")"
  pr_mergeable="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' 2>/dev/null || echo "")"
  pr_merged="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged' 2>/dev/null || echo "")"

  if [ "${pr_state}" = "closed" ] && [ "${pr_merged}" = "true" ]; then
    jq --argjson final_pr "${final_pr}" '.final_merge_pr = $final_pr | .final_merge_status = "merged"' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    return 0
  fi

  if [ "${pr_mergeable}" = "false" ]; then
    jq --argjson final_pr "${final_pr}" \
      '.status = "merge_conflict" | .final_merge_pr = $final_pr | .final_merge_status = "conflict"' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    post_tracking_comment "## ⚠️ Final merge conflict\n\nFinal PR #${final_pr} from \\`${integration_branch}\\` to \\`${default_branch}\\` could not be squash-merged due to conflicts. Resolve manually, then re-run the poller."
    tg_notify "⚠️ Final merge conflict for #${TRACKING_NUM} (PR #${final_pr})."
    return 1
  fi

  if [ "${pr_state}" = "open" ] && [ "${pr_mergeable}" != "true" ]; then
    echo "  [final-merge] PR #${final_pr} mergeability is '${pr_mergeable:-unknown}' after merge attempt. Will retry next poll."
    return 1
  fi

  post_tracking_comment "## ⚠️ Final merge blocked\n\nFinal PR #${final_pr} could not be merged automatically. Review branch protections/checks and merge manually if needed."
  return 1
}

dispatch_validation_workflow() {
  local validation_cycle="$1"
  local validation_ref="${2:-}"
  local wf_name="${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}"
  echo "Dispatching ${wf_name} for tracking #${TRACKING_NUM} (cycle ${validation_cycle})"
  local run_args=("${wf_name}" "--repo" "${GITHUB_REPOSITORY}")
  if [ -n "${validation_ref}" ]; then
    run_args+=("--ref" "${validation_ref}")
  fi
  run_args+=("-f" "tracking_issue=${TRACKING_NUM}")

  if gh_retry gh workflow run "${run_args[@]}" >/dev/null 2>&1; then
    return 0
  fi
  # Fallback: try internal-validate.yml (coding-workflows repo convention)
  if [ "${wf_name}" != "internal-validate.yml" ]; then
    echo "Primary dispatch failed; trying internal-validate.yml fallback"
    run_args=("internal-validate.yml" "--repo" "${GITHUB_REPOSITORY}")
    if [ -n "${validation_ref}" ]; then
      run_args+=("--ref" "${validation_ref}")
    fi
    run_args+=("-f" "tracking_issue=${TRACKING_NUM}")
    if gh_retry gh workflow run "${run_args[@]}" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

dispatch_validation_if_needed() {
  local validation_cycle="$1"
  local integration_branch
  integration_branch="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
  local last_dispatch_cycle
  local last_dispatch_ts
  local now_epoch
  local stale_threshold_secs=3600  # 1 hour: if no label appears after dispatch, allow redispatch

  last_dispatch_cycle="$(jq -r '.validation_last_dispatch_cycle // 0' "${STATE_FILE}")"
  if [ "${last_dispatch_cycle}" = "${validation_cycle}" ]; then
    # Check for staleness: if dispatched but no label change for >1h, allow redispatch
    last_dispatch_ts="$(jq -r '.validation_last_dispatch_ts // 0' "${STATE_FILE}")"
    now_epoch="$(date +%s)"
    if [ "${last_dispatch_ts}" -gt 0 ] 2>/dev/null && [ $(( now_epoch - last_dispatch_ts )) -lt "${stale_threshold_secs}" ]; then
      echo "Validation workflow already dispatched for cycle ${validation_cycle} ($(( now_epoch - last_dispatch_ts ))s ago, threshold ${stale_threshold_secs}s)."
      return 0
    fi
    echo "Validation workflow for cycle ${validation_cycle} appears stale (dispatched >$(( stale_threshold_secs / 60 ))m ago with no label). Redispatching..."
  fi

  if dispatch_validation_workflow "${validation_cycle}" "${integration_branch}"; then
    jq --argjson cycle "${validation_cycle}" --argjson ts "$(date +%s)" \
      '.validation_last_dispatch_cycle = $cycle | .validation_last_dispatch_ts = $ts' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    if [ -n "${integration_branch}" ]; then
      post_tracking_comment "## 🧪 Runtime validation dispatched\n\n- Cycle: ${validation_cycle}\n- Workflow: \`${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}\`\n- Ref: \`${integration_branch}\`"
    else
      post_tracking_comment "## 🧪 Runtime validation dispatched\n\n- Cycle: ${validation_cycle}\n- Workflow: \`${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}\`"
    fi
    tg_notify "🧪 Validation dispatched for project #${TRACKING_NUM} (cycle ${validation_cycle})." "DEBUG"
    return 0
  fi

  return 1
}

mark_validation_failed() {
  local reason="$1"

  # Check validation recovery budget before going terminal
  local val_recovery_count
  val_recovery_count="$(jq -r '.validation_recovery_count // 0' "${STATE_FILE}")"
  if ! [[ "${val_recovery_count}" =~ ^[0-9]+$ ]]; then
    val_recovery_count="0"
  fi

  if [ "${val_recovery_count}" -lt "${MAX_VALIDATION_RECOVERY_ATTEMPTS}" ]; then
    echo "Validation failed but recovery budget remains ($((val_recovery_count + 1))/${MAX_VALIDATION_RECOVERY_ATTEMPTS}). Transitioning back to judge."
    jq --arg reason "${reason}" --argjson count "$((val_recovery_count + 1))" \
      '.status = "in_progress" |
       .validation_recovery_count = $count |
       .validation_failure_reason = $reason |
       .validation_active_fix_issues = [] |
       .validation_cycle = 1 |
       .validation_last_dispatch_cycle = 0 |
       .validation_completed_cycle = null' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    set_tracking_phase_label "ai:validation-recovery"
    post_tracking_comment "## 🔄 Validation failed — recovery attempt $((val_recovery_count + 1))/${MAX_VALIDATION_RECOVERY_ATTEMPTS}\n\n${reason}\n\nTransitioning back to judge for re-evaluation."
    tg_notify "Validation recovery ($((val_recovery_count + 1))/${MAX_VALIDATION_RECOVERY_ATTEMPTS}) for #${TRACKING_NUM}: transitioning back to judge." "WARNING"
    return 0
  fi

  # Recovery budget exhausted — terminal failure
  jq --arg reason "${reason}" '.status = "failed" | .validation_failure_reason = $reason | .validation_active_fix_issues = []' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment
  set_tracking_phase_label "ai:validation-failed"
  post_tracking_comment "## ❌ Runtime validation failed\n\n${reason}\n\nValidation recovery exhausted (${val_recovery_count}/${MAX_VALIDATION_RECOVERY_ATTEMPTS}). Manual intervention required."
  tg_notify "Project #${TRACKING_NUM} validation failed after ${val_recovery_count} recovery attempt(s). Manual intervention required." "CRITICAL"
  tg_cleanup_msgs "${TRACKING_NUM}"
}

mark_validation_complete() {
  local validation_cycle="$1"
  local integration_branch
  local default_branch
  local project_title

  integration_branch="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
  default_branch="$(gh api "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo "main")"
  project_title="$(jq -r '.project_title // "Orchestrator project"' "${STATE_FILE}")"

  if ! finalize_integration_merge_if_needed "${integration_branch}" "${default_branch}" "${project_title}"; then
    return 0
  fi

  jq --argjson cycle "${validation_cycle}" '.status = "complete" | .validation_completed_cycle = $cycle' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment
  set_tracking_phase_label "ai:validated"
  post_tracking_comment "Project completed successfully after runtime validation passed (cycle ${validation_cycle}). Issue kept open for manual review."
  tg_cleanup_msgs "${TRACKING_NUM}"
  MSG="Project #${TRACKING_NUM} completed after validation pass (cycle ${validation_cycle})."
  MSG+=$'\n'"Tracking: $(_gh_url "issues/${TRACKING_NUM}")"
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    MSG+=$'\n'"Run: $(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
  fi
  tg_send_msg "${MSG}" "DEBUG" >/dev/null
}

extract_fix_issues_from_comment() {
  local comment_body="$1"
  # Normalise literal \n sequences (produced by post_tracking_comment) into
  # real newlines so the line-anchored sed below can match "- #<num>" items.
  echo "${comment_body}" | sed 's/\\n/\n/g' | sed -n 's/^- #\([0-9][0-9]*\).*$/\1/p' | awk '!seen[$0]++'
}

sync_validation_fix_issues_from_comments() {
  local comments_json="$1"
  local latest_fix_comment_json
  local fix_comment_id
  local fix_comment_body
  local last_fix_comment_id
  local new_fix_issues_json
  local new_fix_count

  latest_fix_comment_json="$(echo "${comments_json}" | jq -c '[.[] | select(.body | startswith("## 🧪 Runtime validation found fixable issues"))] | last // empty')"
  if [ -z "${latest_fix_comment_json}" ]; then
    return 0
  fi

  fix_comment_id="$(echo "${latest_fix_comment_json}" | jq -r '.id // 0')"
  if ! [[ "${fix_comment_id}" =~ ^[0-9]+$ ]]; then
    return 0
  fi

  last_fix_comment_id="$(jq -r '.validation_last_fix_comment_id // 0' "${STATE_FILE}")"
  if ! [[ "${last_fix_comment_id}" =~ ^[0-9]+$ ]]; then
    last_fix_comment_id="0"
  fi

  if [ "${fix_comment_id}" -le "${last_fix_comment_id}" ]; then
    return 0
  fi

  fix_comment_body="$(echo "${latest_fix_comment_json}" | jq -r '.body // ""')"
  new_fix_issues_json="$(extract_fix_issues_from_comment "${fix_comment_body}" | jq -R 'select(length > 0) | tonumber' | jq -s '.')"
  new_fix_count="$(echo "${new_fix_issues_json}" | jq 'length')"

  if [ "${new_fix_count}" -gt 0 ]; then
    jq --argjson comment_id "${fix_comment_id}" --argjson active_fix_issues "${new_fix_issues_json}" \
      '.status = "validation-fixing" |
       .validation_last_fix_comment_id = $comment_id |
       .validation_active_fix_issues = $active_fix_issues |
       .validation_seen_fix_issues = ((.validation_seen_fix_issues // []) + $active_fix_issues | unique)' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  else
    echo "::warning::Validation fix comment ${fix_comment_id} did not include extractable issue numbers; treating as validation failure."
    mark_validation_failed "Validation workflow produced a fixable-issues comment with no extractable issue numbers (comment ${fix_comment_id})."
    return 0
  fi

  set_tracking_phase_label "ai:validation-fixing"
  post_state_comment
}

# ---------------------------------------------------------------
# Stall recovery: workflow run status checks
# ---------------------------------------------------------------

# Build a set of issue numbers that have *genuinely active* (in_progress or
# queued) workflow runs — i.e., runs that started recently enough that they
# could still be making progress.
#
# Runs that have been in_progress for longer than STALL_THRESHOLD_MINUTES
# are treated as zombie/hung runs and excluded. This prevents a stuck
# Actions runner from blocking stall recovery indefinitely.
#
# Outputs a newline-separated list of issue numbers.
build_active_issue_set() {
  local now_epoch
  now_epoch="$(date +%s)"
  local stall_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))

  # Fetch in_progress + queued runs (recent, max 50)
  local runs_json
  runs_json="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs?status=in_progress&per_page=50" \
    --jq '.workflow_runs' 2>/dev/null || echo '[]')"
  local queued_json
  queued_json="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs?status=queued&per_page=50" \
    --jq '.workflow_runs' 2>/dev/null || echo '[]')"

  # Merge both lists
  local all_runs
  all_runs="$(echo "${runs_json}" "${queued_json}" | jq -s 'add // []' 2>/dev/null || echo '[]')"

  # Filter out zombie runs: any run that has been active for longer than
  # the stall threshold is considered hung and should not block recovery.
  # Uses run_started_at (actual execution start) with created_at as fallback.
  local fresh_runs
  fresh_runs="$(echo "${all_runs}" | jq --argjson now "${now_epoch}" --argjson threshold "${stall_secs}" '
    [.[] |
     # Parse the start timestamp (ISO 8601 → epoch)
     (.run_started_at // .created_at // "1970-01-01T00:00:00Z") as $ts |
     ($ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) as $start_epoch |
     select(($now - $start_epoch) < $threshold)
    ]
  ' 2>/dev/null || echo '[]')"

  local fresh_count
  fresh_count="$(echo "${fresh_runs}" | jq 'length')"
  local total_count
  total_count="$(echo "${all_runs}" | jq 'length')"
  if [ "${total_count}" -gt "${fresh_count}" ]; then
    echo "  Active runs: ${total_count} total, ${fresh_count} fresh ($(( total_count - fresh_count )) zombie runs excluded as >$(( stall_secs / 60 ))m old)." >&2
  fi

  # Extract issue numbers from fresh runs via head_branch patterns.
  # Implement branches typically follow patterns like "ai/issue-42",
  # "ai/42-feature-name", or "ai-implement-42".
  local issue_nums
  issue_nums="$(echo "${fresh_runs}" | jq -r '
    [.[] |
     .head_branch // "" |
     select(test("(?:^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)[0-9]+(?:$|[^0-9])")) |
     capture("(?:^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)(?<num>[0-9]+)(?:$|[^0-9])") | .num
    ] | unique | .[]
  ' 2>/dev/null || true)"

  # Fallback extraction for AI-prefixed branches (single issue number only)
  local branch_nums
  branch_nums="$(echo "${fresh_runs}" | jq -r '
    [.[] | .head_branch // "" |
     select(test("(^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)[0-9]+(?:$|[^0-9])")) |
     capture("(^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)(?<num>[0-9]+)(?:$|[^0-9])") | .num
    ] | unique | .[]
  ' 2>/dev/null || true)"

  # Combine and deduplicate
  printf '%s\n%s\n' "${issue_nums}" "${branch_nums}" | sort -u | grep -E '^[0-9]+$' || true
}

# Check if a specific issue has an active (fresh) workflow run.
# Uses the pre-built active issue set (ACTIVE_WORKFLOW_ISSUES).
issue_has_active_workflow() {
  local issue_num="$1"
  echo "${ACTIVE_WORKFLOW_ISSUES}" | grep -qxF "${issue_num}"
}

# Cancel zombie workflow runs for a specific issue.
# A zombie is a run that has been in_progress for longer than the stall
# threshold. Cancelling prevents resource waste and avoids conflicts with
# the recovery action (e.g., two implement runs on the same branch).
cancel_zombie_runs_for_issue() {
  local issue_num="$1"
  local now_epoch
  now_epoch="$(date +%s)"
  local stall_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))

  # Re-fetch in_progress runs and find zombies matching this issue
  local runs_json
  runs_json="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs?status=in_progress&per_page=50" \
    --jq '.workflow_runs' 2>/dev/null || echo '[]')"

  local zombie_run_ids
  zombie_run_ids="$(echo "${runs_json}" | jq -r --argjson now "${now_epoch}" --argjson threshold "${stall_secs}" --arg issue "${issue_num}" '
    [.[] |
     (.run_started_at // .created_at // "1970-01-01T00:00:00Z") as $ts |
     ($ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) as $start_epoch |
     select(($now - $start_epoch) >= $threshold) |
     select(.head_branch // "" | test("(^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)" + $issue + "(?:[^0-9]|$)")) |
     .id
    ] | .[]
  ' 2>/dev/null || true)"

  if [ -n "${zombie_run_ids}" ]; then
    for run_id in ${zombie_run_ids}; do
      echo "  Cancelling zombie workflow run ${run_id} for issue #${issue_num}..."
      gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/cancel" -X POST 2>/dev/null || true
    done
  fi
}

# Find and close the PR linked to an issue (if any).
close_linked_pr() {
  local issue_num="$1"
  local close_reason="${2:-Closed by orchestrator stall recovery.}"
  local pr_num
  pr_num="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" \
    --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
    2>/dev/null || echo "")"
  if [ -n "${pr_num}" ] && [ "${pr_num}" != "null" ]; then
    local pr_state
    pr_state="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}" --jq '.state' 2>/dev/null || echo "")"
    if [ "${pr_state}" = "open" ]; then
      echo "  Closing linked PR #${pr_num} for issue #${issue_num}..."
      gh_retry gh pr close "${pr_num}" --repo "${GITHUB_REPOSITORY}" \
        --comment "${close_reason}" 2>/dev/null || true
    fi
  fi
}

# ---------------------------------------------------------------
# Implementation no-op tracking helpers
# ---------------------------------------------------------------

# Read the impl_noop_count for a local_id from the state file.
get_impl_noop_count() {
  local lid="$1"
  STATE_FILE="${STATE_FILE}" IMPL_NOOP_LID="${lid}" python3 -c "
import json, os, sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import get_impl_noop_count

with open(os.environ['STATE_FILE']) as f:
    state = json.load(f)

value = get_impl_noop_count(state, os.environ['IMPL_NOOP_LID'])
try:
    print(int(value))
except (TypeError, ValueError):
    print(0)
" 2>/dev/null || echo "0"
}

# Increment the impl_noop_count for a local_id in the state file.
bump_impl_noop_count() {
  local lid="$1"
  STATE_FILE="${STATE_FILE}" IMPL_NOOP_LID="${lid}" python3 -c "
import json, os, sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import increment_impl_noop_count

with open(os.environ['STATE_FILE']) as f:
    state = json.load(f)

increment_impl_noop_count(state, os.environ['IMPL_NOOP_LID'])

with open(os.environ['STATE_FILE'], 'w') as f:
    json.dump(state, f, indent=2)
" || true
}

MAX_IMPL_NOOP_REISSUES="${MAX_IMPL_NOOP_REISSUES:-2}"
if ! [[ "${MAX_IMPL_NOOP_REISSUES}" =~ ^[1-9][0-9]*$ ]]; then
  MAX_IMPL_NOOP_REISSUES=2
fi

# ---------------------------------------------------------------
# Helper: extract (RECOMMENDED) answers from clarification comments
# ---------------------------------------------------------------

# Fetches the latest clarification-questions comment for an issue and
# parses (RECOMMENDED) choices to build a "Q1: A\nQ2: B\n..." answer
# string.  Falls back to an empty string if no questions are found.
extract_recommended_answers() {
  local issue_num="$1"

  # Fetch recent comments (50 is the same limit used by clarify.yml)
  local comments_json
  comments_json="$(gh_retry gh api \
    "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=50" \
    2>/dev/null || echo "[]")"

  # Find the latest clarification comment (HTML marker or legacy prefix).
  # Search from newest to oldest (direction=desc).
  local clarify_body
  clarify_body="$(printf '%s' "${comments_json}" | jq -r '
    [ .[]
      | select(
          (.user.login // "" | test("\\[bot\\]$")) and
          ((.body // "") | test("<!-- ai:clarification-questions -->|^Clarification required"))
        )
    ]
    | if length > 0 then .[0].body // "" else "" end
  ')"

  if [ -z "${clarify_body}" ]; then
    echo ""
    return
  fi

  # Parse Q-blocks and pick the (RECOMMENDED) letter(s) for each.
  # When multiple options are recommended, combine with "+" (e.g. "A+C").
  # Expected format per question:
  #   **Q1: <question>**
  #   Choices:
  #   - **A** — <desc> (RECOMMENDED)
  #   - **B** — <desc>
  printf '%s' "${clarify_body}" | perl -ne '
    BEGIN { @order = (); %rec = (); $qid = undef; }
    if (/^\s*\*?\*?Q(\d+)/i) {
      # New question block — flush previous if it had recommendations
      if (defined $qid && exists $rec{$qid}) {
        push @order, $qid unless grep { $_ eq $qid } @order;
      }
      $qid = $1;
    }
    if (defined $qid && /^\s*-\s*\*\*([A-Z])\*\*\s*.*\(RECOMMENDED\)/i) {
      push @{$rec{$qid}}, $1;
    }
    END {
      # Flush the last question
      if (defined $qid && exists $rec{$qid}) {
        push @order, $qid unless grep { $_ eq $qid } @order;
      }
      for my $q (@order) {
        print "Q${q}: " . join("+", @{$rec{$q}}) . "\n";
      }
    }
  '
}

# ---------------------------------------------------------------
# Stall recovery: phase-specific healing actions
# ---------------------------------------------------------------

recover_stalled_issue() {
  local issue_num="$1"
  local phase="$2"
  local action="$3"
  local recovery_count="$4"
  local local_id="$5"
  local stall_minutes="$6"

  echo "  [stall-recovery] Issue #${issue_num} stuck in '${phase}' for ${stall_minutes}m (attempt $((recovery_count + 1))). Action: ${action}"

  # ---- Guard: skip recovery if a *fresh* workflow is actively processing this issue ----
  if [ "${action}" != "skip" ] && issue_has_active_workflow "${issue_num}"; then
    echo "  [stall-recovery] Issue #${issue_num} has a recent active workflow run — skipping recovery (workflow is slow, not stalled)."
    return 1  # Signal: no action taken (caller should not increment counter)
  fi

  # Cancel any zombie runs for this issue before retrying — prevents
  # conflicts (e.g., two implement runs on the same branch) and frees
  # runner capacity.
  if [ "${action}" != "skip" ] && [ "${action}" != "attempt_merge" ]; then
    cancel_zombie_runs_for_issue "${issue_num}"
  fi

  case "${action}" in
    retrigger_pipeline)
      # No AI labels — the pipeline never started. Post /reclarify to trigger
      # the clarify workflow for this issue.
      echo "  Re-triggering pipeline for issue #${issue_num}..."
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/reclarify

_Orchestrator stall recovery: this issue never entered the AI pipeline.
Re-triggering the clarification phase. If the issue description is
sufficient, proceed directly to planning and implementation._
STALL_EOF
)" >/dev/null 2>&1 || true
      tg_notify "Stall recovery: re-triggered pipeline for issue #${issue_num} (stuck ${stall_minutes}m with no labels, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      ;;

    auto_respond_clarify)
      # Stuck in ai:clarification — auto-respond to move to planning.
      # Extract (RECOMMENDED) answers from the clarification comment so the
      # planning LLM receives real Q1/Q2/... answers instead of a bare /answer.
      echo "  Auto-responding to clarification for issue #${issue_num}..."
      local recommended_answers
      recommended_answers="$(extract_recommended_answers "${issue_num}")"

      local answer_body
      if [ -n "${recommended_answers}" ]; then
        answer_body="/answer [auto-answered-by-orchestrator]

_Orchestrator stall recovery: this issue has been in clarification for
too long. Auto-selecting recommended answers and proceeding with
planning and implementation._

${recommended_answers}"
        echo "  Extracted recommended answers for issue #${issue_num}:"
        echo "${recommended_answers}" | sed 's/^/    /'
      else
        answer_body="/answer [auto-answered-by-orchestrator]

_Orchestrator stall recovery: this issue has been in clarification for
too long. No recommended answers could be extracted — the issue
description is deemed sufficient. Proceed with planning and
implementation._"
        echo "  No recommended answers found for issue #${issue_num}; posting bare /answer."
      fi

      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="${answer_body}" >/dev/null 2>&1 || true
      tg_notify "Stall recovery: auto-responded to clarification on issue #${issue_num} (stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      ;;

    retrigger_plan)
      # Stuck in ai:planning — re-trigger plan generation.
      echo "  Re-triggering plan for issue #${issue_num}..."
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/answer

_Orchestrator stall recovery: planning phase stalled. Re-triggering
plan generation._
STALL_EOF
)" >/dev/null 2>&1 || true
      tg_notify "Stall recovery: re-triggered planning for issue #${issue_num} (stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      ;;

    auto_approve)
      # Stuck in ai:awaiting-approval — auto-approve for orchestrator issues.
      # Guard: close issue if this task has already hit the no-op cap
      # (re-approving would just trigger another no-op cycle; closing
      # lets the wave-completion judge verify instead of deadlocking).
      local noop_cnt
      noop_cnt="$(get_impl_noop_count "${local_id}")"
      if [ "${noop_cnt}" -ge "${MAX_IMPL_NOOP_REISSUES}" ]; then
        echo "  [stall-recovery] Issue #${issue_num} (${local_id}) hit impl no-op cap (${noop_cnt}/${MAX_IMPL_NOOP_REISSUES}) — closing to let judge verify."
        gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
          --remove-label 'ai:awaiting-approval' --add-label 'ai:closed' 2>/dev/null || true
        gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
          -c "Closing: implementation produced no changes ${noop_cnt} time(s). The code described in this issue likely already exists on the default branch. The wave-completion judge will verify." 2>/dev/null || true
        tg_notify "Stall recovery: issue #${issue_num} (${local_id}) hit impl no-op cap (${noop_cnt}). Closed — judge will verify."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
        return 0
      fi
      echo "  Auto-approving plan for issue #${issue_num}..."
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/approved

_Orchestrator stall recovery: auto-approving plan. This is an
orchestrator-managed issue that does not require human approval._
STALL_EOF
)" >/dev/null 2>&1 || true
      tg_notify "Stall recovery: auto-approved plan for issue #${issue_num} (stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      ;;

    retrigger_implement)
      # Stuck in ai:implementing — re-trigger implementation.
      # Guard: skip if this task has already hit the no-op implementation cap.
      local noop_cnt_impl
      noop_cnt_impl="$(get_impl_noop_count "${local_id}")"
      if [ "${noop_cnt_impl}" -ge "${MAX_IMPL_NOOP_REISSUES}" ]; then
        echo "  [stall-recovery] Issue #${issue_num} (${local_id}) hit impl no-op cap (${noop_cnt_impl}/${MAX_IMPL_NOOP_REISSUES}) — closing to let judge verify."
        gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
          --remove-label 'ai:implementing' --add-label 'ai:closed' 2>/dev/null || true
        gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
          -c "Closing: implementation produced no changes ${noop_cnt_impl} time(s). The code described in this issue likely already exists on the default branch. The wave-completion judge will verify." 2>/dev/null || true
        tg_notify "Stall recovery: issue #${issue_num} (${local_id}) hit impl no-op cap (${noop_cnt_impl}). Closed — judge will verify."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
        return 0
      fi
      echo "  Re-triggering implementation for issue #${issue_num}..."
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
        -f body="$(cat <<'STALL_EOF'
/approved

_Orchestrator stall recovery: implementation phase appears stalled.
Re-triggering implementation. If a previous attempt crashed or timed
out, start fresh from the approved plan._
STALL_EOF
)" >/dev/null 2>&1 || true
      tg_notify "Stall recovery: re-triggered implementation for issue #${issue_num} (stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      ;;

    retrigger_review)
      # Stuck at ai:done — PR exists but review never started or stalled.
      # Find linked PR and push empty commit to trigger synchronize event.
      echo "  Re-triggering review for issue #${issue_num}..."
      local pr_num
      pr_num="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/timeline" \
        --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
        2>/dev/null || echo "")"
      if [ -n "${pr_num}" ] && [ "${pr_num}" != "null" ]; then
        local head_ref
        head_ref="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr_num}" --jq '.head.ref' 2>/dev/null || echo "")"
        if [ -n "${head_ref}" ] && [ "${head_ref}" != "null" ]; then
          if git fetch origin "${head_ref}:refs/remotes/origin/${head_ref}" 2>/dev/null && \
             git checkout "origin/${head_ref}" 2>/dev/null; then
            git config user.name "codex-bot"
            git config user.email "codex@users.noreply.github.com"
            git commit --allow-empty -m "[orchestrator] stall recovery: re-trigger review for issue #${issue_num}" 2>/dev/null || true
            if git push origin "HEAD:${head_ref}" 2>/dev/null; then
              echo "  Pushed empty commit to PR #${pr_num} to re-trigger review."
              tg_notify "Stall recovery: re-triggered review for PR #${pr_num} (issue #${issue_num}, stuck ${stall_minutes}m, attempt $((recovery_count + 1)))."$'\n'"PR: $(_gh_url "pull/${pr_num}")"$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
            else
              echo "::warning::Could not push to re-trigger review for PR #${pr_num}."
            fi
            git checkout --detach HEAD 2>/dev/null || true
          else
            echo "::warning::Could not fetch/check out head ref ${head_ref} for PR #${pr_num}; skipping review re-trigger."
          fi
        else
          echo "::warning::Could not determine head ref for PR #${pr_num}."
        fi
      else
        echo "  No linked PR found for issue #${issue_num}. Treating as implementation incomplete."
        # Re-trigger implementation since no PR was produced
        gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments" \
          -f body="$(cat <<'STALL_EOF'
/approved

_Orchestrator stall recovery: issue is marked done but no PR was found.
Re-triggering implementation._
STALL_EOF
)" >/dev/null 2>&1 || true
        tg_notify "Stall recovery: re-triggered implement for issue #${issue_num} (ai:done but no PR, stuck ${stall_minutes}m)."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      fi
      ;;

    attempt_merge)
      # Stuck at ai:ready-to-merge — retry the merge. The main merge logic
      # already handles this each poll cycle, so just log for diagnostics.
      echo "  Issue #${issue_num} stuck at ready-to-merge. Main merge loop will retry."
      tg_notify "Stall recovery: issue #${issue_num} stuck at ready-to-merge for ${stall_minutes}m (attempt $((recovery_count + 1))). Merge loop will retry."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      ;;

    close_and_reissue)
      # Nuclear option: close the linked PR (if any) and the issue, then
      # create a fresh replacement. The new issue enters the pipeline from
      # scratch with a clean slate.
      echo "  Closing and re-issuing stalled issue #${issue_num}..."

      # Close any linked PR first so the replacement issue starts clean
      close_linked_pr "${issue_num}" \
        "Closed by orchestrator stall recovery — issue #${issue_num} was stuck in '${phase}' for ${stall_minutes}m. A replacement issue will be created."

      local orig_title orig_body
      orig_title="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.title' 2>/dev/null || echo "")"
      orig_body="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}" --jq '.body' 2>/dev/null || echo "")"

      ensure_label_exists "ai:closed"
      gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        --remove-label 'ai:done' --remove-label 'ai:implementing' \
        --remove-label 'ai:planning' --remove-label 'ai:clarification' \
        --remove-label 'ai:awaiting-approval' \
        --add-label 'ai:closed' 2>/dev/null || true
      gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        -c "Closing: orchestrator stall recovery. Issue was stuck in '${phase}' for ${stall_minutes} minutes after $((recovery_count + 1)) recovery attempt(s). Re-issuing with additional guidance." 2>/dev/null || true

      local new_body
      new_body="$(cat <<REISSUE_EOF
${orig_body}

---

**⚠️ Re-issued from #${issue_num}** — the previous issue stalled in the \`${phase}\` phase for ${stall_minutes} minutes despite $((recovery_count + 1)) recovery attempt(s).

**Guidance for AI agents:**
- This issue has been re-created by the orchestrator stall recovery system.
- Previous attempt stalled at phase: \`${phase}\`
- Proceed through the full pipeline (clarify → plan → implement → review).
- If the task encounters the same blocker, explain the specific failure in a comment.
REISSUE_EOF
)"

      local new_url new_url_clean new_num
      ensure_label_exists "ai:clarification"
      new_url="$(gh issue create --repo "${GITHUB_REPOSITORY}" \
        --title "${orig_title}" \
        --body "${new_body}" \
        --label "ai:clarification" 2>/dev/null || echo "")"
      if [ -n "${new_url}" ]; then
        new_url_clean="$(printf '%s\n' "${new_url}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
        new_num="$(basename "${new_url_clean%%[?#]*}")"
        echo "  Created replacement issue #${new_num} for stalled #${issue_num}."

        # Update state: remap the local_id to the new issue number
        if [[ "${new_num}" =~ ^[0-9]+$ ]] && [ -n "${local_id}" ] && [ "${local_id}" != "null" ]; then
          jq --arg lid "${local_id}" --argjson new_num "${new_num}" --argjson wave_idx "${WAVE_IDX}" \
            '.issue_number_map[$lid] = $new_num |
             (.waves[$wave_idx].issues[] | select(.id == $lid)) |=
               (.github_issue = $new_num | .status = "pending" | .last_seen_phase = "" | .status_since_ts = (now | floor) | .stall_recovery_count = 0)' \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        fi

        tg_notify "Stall recovery: closed stalled issue #${issue_num} and re-issued as #${new_num} (phase: ${phase}, stuck ${stall_minutes}m)."$'\n'"Old issue: $(_gh_url "issues/${issue_num}")"$'\n'"New issue: $(_gh_url "issues/${new_num}")" "WARNING"
      else
        echo "::warning::Could not create replacement issue for stalled #${issue_num}."
      fi
      ;;

    skip)
      # Max recoveries exhausted — skip the issue so the wave can advance.
      # The judge will see it as closed/failed at wave completion.
      echo "  Skipping issue #${issue_num} after ${recovery_count} recovery attempts."

      # Close any linked PR before skipping
      close_linked_pr "${issue_num}" \
        "Closed by orchestrator: stall recovery exhausted (${recovery_count} attempts). The judge will evaluate this gap."

      ensure_label_exists "ai:closed"
      gh_retry gh issue edit "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        --add-label 'ai:closed' 2>/dev/null || true
      gh_retry gh issue close "${issue_num}" --repo "${GITHUB_REPOSITORY}" \
        -c "Closing: orchestrator stall recovery exhausted (${recovery_count} attempts over $((stall_minutes)) minutes in '${phase}' phase). The judge will evaluate this gap at wave completion." 2>/dev/null || true

      post_tracking_comment "## ⏭️ Issue #${issue_num} skipped (stall recovery exhausted)

- **Phase:** \`${phase}\`
- **Stalled for:** ${stall_minutes} minutes
- **Recovery attempts:** ${recovery_count}
- **Local ID:** \`${local_id}\`

The judge will evaluate this gap when the wave completes and decide whether to reissue, accept, or adjust the project."

      tg_notify "Issue #${issue_num} skipped after ${recovery_count} stall recovery attempts (${stall_minutes}m in '${phase}'). Judge will handle at wave completion."$'\n'"Issue: $(_gh_url "issues/${issue_num}")" "WARNING"
      ;;

    *)
      echo "::warning::Unknown stall recovery action: ${action} for issue #${issue_num}"
      ;;
  esac
}

# ---------------------------------------------------------------
# Helper: Check if an active autofix run already exists for a PR branch
# ---------------------------------------------------------------
# Queries the GitHub Actions API for in_progress or queued runs of
# review/autofix workflows on the given branch.  A new dispatch would
# cancel the existing run (cancel-in-progress concurrency) and trigger
# a spurious "cancelled/timed out" Telegram alert.
#
# Usage: _has_active_autofix_run <pr_number> <head_ref>
# Returns 0 if an active run exists (skip dispatch), 1 otherwise.
_has_active_autofix_run()
{
	local pr_number="$1"
	local head_ref="$2"
	local log_prefix="[conflict-dispatch] PR #${pr_number}"

	for wf_candidate in ai-review.yml internal-review.yml review_autofix.yml; do
		local active
		active="$(gh run list --repo "${GITHUB_REPOSITORY}" \
			--workflow "${wf_candidate}" \
			--branch "${head_ref}" \
			--limit 5 \
			--json status \
			--jq '[.[] | select(.status == "in_progress" or .status == "queued")] | length' \
			2>/dev/null || echo "0")"
		if [ "${active}" -gt 0 ]; then
			echo "  ${log_prefix} Active autofix run found (workflow=${wf_candidate}, count=${active}). Skipping dispatch."
			return 0
		fi
	done

	return 1
}

# Helper: Dispatch review workflow for merge conflict resolution
# ---------------------------------------------------------------
# Instead of resolving conflicts locally with Codex, dispatch the
# review_autofix workflow via workflow_dispatch.  The review workflow
# has its own Codex-based conflict resolver that runs on a dedicated
# runner with a clean checkout — more reliable than the shared
# orchestrator environment.
#
# workflow_dispatch resolves from the target ref (the PR branch),
# which always exists, bypassing the unbuildable merge-ref problem
# that affects pull_request synchronize events.
#
# Usage: _dispatch_review_for_conflicts <pr_number> <head_ref>
# Returns: 0 = dispatched, 1 = dispatch failed, 2 = skipped (active run exists).
_dispatch_review_for_conflicts()
{
	local pr_number="$1"
	local head_ref="$2"
	local log_prefix="[conflict-dispatch] PR #${pr_number}"

	# Guard: skip dispatch if an autofix run is already active for this PR.
	# A new dispatch would cancel the running job (cancel-in-progress
	# concurrency) and fire a spurious "cancelled/timed out" alert.
	if _has_active_autofix_run "${pr_number}" "${head_ref}"; then
		return 2
	fi

	echo "  ${log_prefix} Dispatching review workflow for conflict resolution..."

	for wf_candidate in ai-review.yml internal-review.yml review_autofix.yml; do
		if gh workflow run "${wf_candidate}" \
			--repo "${GITHUB_REPOSITORY}" \
			--ref "${head_ref}" \
			-f pr_number="${pr_number}" 2>/dev/null; then
			echo "  ${log_prefix} Dispatched ${wf_candidate} on ${head_ref}."
			return 0
		fi
	done

	echo "::warning::${log_prefix} Could not dispatch review workflow via workflow_dispatch."
	return 1
}

# ---------------------------------------------------------------
# Normalize truthy env vars (case-insensitive 1/true/yes/on)
# ---------------------------------------------------------------
_is_truthy() {
  case "$(echo "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}
if _is_truthy "${ENABLE_VALIDATION:-true}"; then
  ENABLE_VALIDATION="true"
else
  ENABLE_VALIDATION="false"
fi

# Sanitize MAX_VALIDATE_CYCLES
if ! [[ "${MAX_VALIDATE_CYCLES:-3}" =~ ^[0-9]+$ ]] || [ "${MAX_VALIDATE_CYCLES:-3}" -lt 1 ]; then
  MAX_VALIDATE_CYCLES="3"
fi

# ---------------------------------------------------------------
# Process each tracking issue
# ---------------------------------------------------------------
TRACKING_ISSUES="$(cat "${RUNTIME_DIR}/tracking_issues.json")"
COUNT="$(echo "${TRACKING_ISSUES}" | jq 'length')"

for tidx in $(seq 0 $(( COUNT - 1 ))); do
  TRACKING_NUM="$(echo "${TRACKING_ISSUES}" | jq -r ".[${tidx}].number")"
  TRACKING_TITLE="$(echo "${TRACKING_ISSUES}" | jq -r ".[${tidx}].title")"
  echo "========================================"
  echo "Processing tracking issue #${TRACKING_NUM}: ${TRACKING_TITLE}"
  echo "========================================"

  # ---------------------------------------------------------------
  # Extract state from the tracking issue's comments
  # ---------------------------------------------------------------
  COMMENTS="$(gh api --paginate \
    "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments?per_page=100" \
    | jq -s 'add // []')"

  # Find the latest state comment (search from the end)
  STATE_JSON="$(echo "${COMMENTS}" | jq -r '
    [.[] | select(.body | contains("ORCHESTRATOR_STATE_V1"))] | last |
    .body |
    split("<!-- ORCHESTRATOR_STATE_V1")[1] |
    split("ORCHESTRATOR_STATE_V1 -->")[0] |
    ltrimstr("\n") | rtrimstr("\n")
  ' 2>/dev/null || echo "")"

  if [ -z "${STATE_JSON}" ] || [ "${STATE_JSON}" = "null" ]; then
    echo "::warning::No state found for tracking issue #${TRACKING_NUM}. Attempting state reconstruction..."

    # ---------------------------------------------------------------
    # State reconstruction: the orchestrate.yml workflow created the
    # tracking issue and child issues but failed before posting the
    # initial state comment.  Recover by parsing the tracking body
    # and searching for child issues that reference this tracker.
    # ---------------------------------------------------------------
    TRACKING_BODY="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}" --jq '.body' 2>/dev/null || echo "")"
    if [ -z "${TRACKING_BODY}" ]; then
      echo "::warning::Could not fetch body for tracking issue #${TRACKING_NUM}, skipping."
      continue
    fi

    # Search for child issues whose body contains the tracking reference
    CHILD_ISSUES="$(gh api "search/issues" \
      -f q="repo:${GITHUB_REPOSITORY} \"Tracking issue: #${TRACKING_NUM}\" in:body" \
      --jq '.items // []' 2>/dev/null || echo '[]')"

    # Build issue_number_map from child issue bodies: extract Local ID metadata
    ISSUE_MAP_JSON="$(echo "${CHILD_ISSUES}" | jq '
      reduce .[] as $issue ({};
        ($issue.body | capture("Local ID: `(?<id>[^`]+)`") // null) as $cap |
        if $cap != null then
          . + {($cap.id): $issue.number}
        else . end
      )
    ' 2>/dev/null || echo '{}')"

    echo "  Discovered issue map: ${ISSUE_MAP_JSON}"

    # Write tracking body to temp file for the Python helper
    REBUILD_BODY_FILE="${RUNTIME_DIR}/rebuild_body_${TRACKING_NUM}.txt"
    printf '%s\n' "${TRACKING_BODY}" > "${REBUILD_BODY_FILE}"

    if python3 scripts/orchestrate_lib.py rebuild-state \
      --body-file "${REBUILD_BODY_FILE}" \
      --issue-map-json "${ISSUE_MAP_JSON}" \
      --tracking-issue "${TRACKING_NUM}" > "${STATE_FILE}" 2>/dev/null; then

      if [ -s "${STATE_FILE}" ] && jq -e '.schema_version' "${STATE_FILE}" >/dev/null 2>&1; then
        STATE_JSON="$(cat "${STATE_FILE}")"
        # Post the reconstructed state so future poll cycles find it
        post_state_comment
        echo "  State reconstructed and posted for tracking issue #${TRACKING_NUM}."
        tg_notify "Auto-recovery: rebuilt missing orchestrator state for tracking issue #${TRACKING_NUM}." "DEBUG"
      else
        echo "::warning::State reconstruction produced invalid output for #${TRACKING_NUM}, skipping."
        continue
      fi
    else
      echo "::warning::State reconstruction failed for tracking issue #${TRACKING_NUM}, skipping."
      continue
    fi
  fi

  echo "${STATE_JSON}" > "${STATE_FILE}"
  PROJECT_STATUS="$(jq -r '.status' "${STATE_FILE}")"

  DEFAULT_BRANCH_TRACKING="$(gh api "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo "main")"
  INTEGRATION_BRANCH_TRACKING="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
  if [ -n "${INTEGRATION_BRANCH_TRACKING}" ] \
    && [ "${PROJECT_STATUS}" != "complete" ] \
    && [ "${PROJECT_STATUS}" != "failed" ] \
    && [ "${PROJECT_STATUS}" != "merge_conflict" ] \
    && [ "${PROJECT_STATUS}" != "validation-failed" ]; then
    if ! sync_default_into_integration_branch "${INTEGRATION_BRANCH_TRACKING}" "${DEFAULT_BRANCH_TRACKING}"; then
      continue
    fi
    PROJECT_STATUS="$(jq -r '.status' "${STATE_FILE}")"
    if [ "${PROJECT_STATUS}" = "failed" ]; then
      continue
    fi
  fi

  TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"

  if [ "${PROJECT_STATUS}" = "merge_conflict" ]; then
    FINAL_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
    FINAL_DEFAULT_BRANCH="$(gh api "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo "main")"
    FINAL_PROJECT_TITLE="$(jq -r '.project_title // "Orchestrator project"' "${STATE_FILE}")"

    if ! finalize_integration_merge_if_needed "${FINAL_INTEGRATION_BRANCH}" "${FINAL_DEFAULT_BRANCH}" "${FINAL_PROJECT_TITLE}"; then
      continue
    fi

    if has_label "${TRACKING_LABELS}" "ai:validated"; then
      VALIDATION_CYCLE="$(jq -r '.validation_cycle // 1' "${STATE_FILE}")"
      mark_validation_complete "${VALIDATION_CYCLE}"
      continue
    fi

    echo "Project complete!"
    jq '.status = "complete" | .judge_cycle += 1' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    set_tracking_phase_label "ai:done"
    post_tracking_comment "Project completed successfully. Issue kept open for manual review."
    tg_cleanup_msgs "${TRACKING_NUM}"
    MSG="✅ Project #${TRACKING_NUM} completed successfully."
    MSG+=$'\n'"Tracking: $(_gh_url "issues/${TRACKING_NUM}")"
    if [ -n "${GITHUB_RUN_ID:-}" ]; then
      MSG+=$'\n'"Run: $(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
    fi
    tg_send_msg "${MSG}" >/dev/null
    continue
  fi

  if [ "${PROJECT_STATUS}" = "validating" ] || [ "${PROJECT_STATUS}" = "validation-fixing" ]; then
    sync_validation_fix_issues_from_comments "${COMMENTS}"
    PROJECT_STATUS="$(jq -r '.status' "${STATE_FILE}")"
    TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"
  fi

  if [ "${PROJECT_STATUS}" = "validating" ] || [ "${PROJECT_STATUS}" = "validation-fixing" ]; then
    VALIDATION_CYCLE="$(jq -r '.validation_cycle // 1' "${STATE_FILE}")"
    if ! [[ "${VALIDATION_CYCLE}" =~ ^[0-9]+$ ]] || [ "${VALIDATION_CYCLE}" -lt 1 ]; then
      VALIDATION_CYCLE="1"
    fi

    if has_label "${TRACKING_LABELS}" "ai:validation-failed"; then
      # Extract the detailed failure diagnosis from the most recent validation
      # comment posted by validate_process.sh (matches headings like
      # "Runtime validation failed", "Runtime validation harness error",
      # "Runtime validation infeasible", or "Runtime validation found fixable issues").
      VALIDATION_FAIL_BODY="$(echo "${COMMENTS}" | jq -r '
        [.[] | select(.body | test("## [❌🧪⚠️]+ Runtime validation"))] | last | .body // ""
      ')"
      if [ -n "${VALIDATION_FAIL_BODY}" ] && [ "${VALIDATION_FAIL_BODY}" != "" ]; then
        mark_validation_failed "${VALIDATION_FAIL_BODY}"
      else
        mark_validation_failed "Validation workflow reported failure (label ai:validation-failed)."
      fi
      continue
    fi

    if has_label "${TRACKING_LABELS}" "ai:validated"; then
      mark_validation_complete "${VALIDATION_CYCLE}"
      continue
    fi

    if [ "${PROJECT_STATUS}" = "validating" ]; then
      if has_label "${TRACKING_LABELS}" "ai:validation-fixing"; then
        jq '.status = "validation-fixing"' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        post_state_comment
        continue
      fi

      if ! dispatch_validation_if_needed "${VALIDATION_CYCLE}"; then
        mark_validation_failed "Unable to dispatch ${VALIDATE_WORKFLOW_NAME:-ai-validate.yml} for cycle ${VALIDATION_CYCLE}. Ensure consumer wrapper workflow exists and GH token has actions:write."
      fi
      continue
    fi

    ACTIVE_FIX_ISSUES_JSON="$(jq -c '.validation_active_fix_issues // []' "${STATE_FILE}")"
    ACTIVE_FIX_COUNT="$(echo "${ACTIVE_FIX_ISSUES_JSON}" | jq 'length')"

    if [ "${ACTIVE_FIX_COUNT}" -le 0 ]; then
      echo "Validation is in fixing state but no active fix issues are tracked yet."
      continue
    fi

    FIX_ANY_CLOSED="false"
    FIX_ALL_MERGED="true"
    CLOSED_FIX_NUMS=""

    while IFS= read -r fix_num; do
      [ -n "${fix_num}" ] || continue
      FIX_LABELS="$(get_issue_labels_json "${fix_num}")"
      if has_label "${FIX_LABELS}" "ai:closed"; then
        FIX_ANY_CLOSED="true"
        CLOSED_FIX_NUMS="${CLOSED_FIX_NUMS} #${fix_num}"
      fi
      if ! has_label "${FIX_LABELS}" "ai:merged"; then
        FIX_ALL_MERGED="false"
      fi
    done < <(echo "${ACTIVE_FIX_ISSUES_JSON}" | jq -r '.[]')

    if [ "${FIX_ANY_CLOSED}" = "true" ]; then
      mark_validation_failed "Validation fix-up issue(s) closed without merge:${CLOSED_FIX_NUMS}"
      continue
    fi

    if [ "${FIX_ALL_MERGED}" != "true" ]; then
      echo "Validation fix-up issues are still in progress."
      continue
    fi

    NEXT_VALIDATION_CYCLE=$(( VALIDATION_CYCLE + 1 ))
    if [ "${NEXT_VALIDATION_CYCLE}" -gt "${MAX_VALIDATE_CYCLES}" ]; then
      mark_validation_failed "Validation exceeded MAX_VALIDATE_CYCLES=${MAX_VALIDATE_CYCLES} without passing."
      continue
    fi

    tg_notify "Attempting to re-dispatch validation for project #${TRACKING_NUM} (cycle ${NEXT_VALIDATION_CYCLE}) after fix-up issues merged." "DEBUG"

    jq --argjson cycle "${NEXT_VALIDATION_CYCLE}" \
      '.status = "validating" |
       .validation_cycle = $cycle |
       .validation_active_fix_issues = []' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    set_tracking_phase_label "ai:validating"

    if ! dispatch_validation_if_needed "${NEXT_VALIDATION_CYCLE}"; then
      mark_validation_failed "Unable to dispatch ${VALIDATE_WORKFLOW_NAME:-ai-validate.yml} for cycle ${NEXT_VALIDATION_CYCLE}. Ensure consumer wrapper workflow exists and GH token has actions:write."
    fi
    continue
  fi

  # ---------------------------------------------------------------
  # /revalidate — manual reset from validation-failed
  # ---------------------------------------------------------------
  # When a project is in terminal validation-failed state (status="failed"
  # with ai:validation-failed label), a /revalidate comment posted AFTER
  # the latest state comment resets counters and re-dispatches validation.
  if [ "${PROJECT_STATUS}" = "failed" ] && has_label "${TRACKING_LABELS}" "ai:validation-failed"; then
    REVALIDATE_REQUESTED="$(echo "${COMMENTS}" | jq -r '
      (to_entries | map(select(.value.body | contains("ORCHESTRATOR_STATE_V1"))) | last | .key // -1) as $last_state_idx |
      [to_entries[] | select(.key > $last_state_idx and (.value.body | test("^\\s*/revalidate(\\s|$)"; "m")))] | length > 0
    ')"

    if [ "${REVALIDATE_REQUESTED}" = "true" ]; then
      echo "  /revalidate requested for project #${TRACKING_NUM}. Resetting validation state."
      jq \
        '.status = "validating" |
         .validation_cycle = 1 |
         .validation_recovery_count = 0 |
         .validation_active_fix_issues = [] |
         .validation_last_dispatch_cycle = 0 |
         .validation_completed_cycle = null |
         del(.validation_failure_reason)' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment
      set_tracking_phase_label "ai:validating"
      post_tracking_comment "## 🔁 Validation reset via /revalidate\n\nAll validation counters cleared. Re-dispatching validation (cycle 1)."
      tg_notify "/revalidate: project #${TRACKING_NUM} reset from validation-failed. Dispatching validation cycle 1." "DEBUG"
      if ! dispatch_validation_if_needed 1; then
        mark_validation_failed "Unable to dispatch ${VALIDATE_WORKFLOW_NAME:-ai-validate.yml} after /revalidate reset."
      fi
      continue
    fi
  fi

  if [ "${PROJECT_STATUS}" = "complete" ] || [ "${PROJECT_STATUS}" = "failed" ] || [ "${PROJECT_STATUS}" = "validation-failed" ]; then
    echo "Project already ${PROJECT_STATUS}, skipping."
    continue
  fi

  CURRENT_WAVE="$(jq -r '.current_wave' "${STATE_FILE}")"
  TOTAL_WAVES="$(jq -r '.total_waves' "${STATE_FILE}")"
  JUDGE_CYCLE="$(jq -r '.judge_cycle' "${STATE_FILE}")"
  # Backward compat: read recovery_count (new) or migrate from recovery_attempted (old)
  RECOVERY_COUNT="$(jq -r '.recovery_count // (if .recovery_attempted == true then 1 else 0 end)' "${STATE_FILE}")"

  echo "Current wave: ${CURRENT_WAVE}/${TOTAL_WAVES}, Judge cycle: ${JUDGE_CYCLE}, Recovery count: ${RECOVERY_COUNT}/${MAX_RECOVERY_ATTEMPTS}"

  # ---------------------------------------------------------------
  # Backward scan: check prior waves for non-terminal issues
  # ---------------------------------------------------------------
  # Safety net: if a fix-up issue was added to a prior wave (or a
  # status update was missed), detect it here and update state /
  # attempt auto-merge so the issue doesn't stay orphaned forever.
  WAVE_IDX=$(( CURRENT_WAVE - 1 ))

  PRIOR_WAVE_REMEDIATED="false"
  if [ "${WAVE_IDX}" -gt 0 ]; then
    for prior_idx in $(seq 0 $(( WAVE_IDX - 1 ))); do
      PRIOR_NON_TERMINAL="$(jq -r --argjson wi "${prior_idx}" \
        '.waves[$wi].issues[] | select(.status != "merged" and .status != "closed" and .status != "skipped") | .github_issue' \
        "${STATE_FILE}" 2>/dev/null || echo "")"
      for pw_inum in ${PRIOR_NON_TERMINAL}; do
        [ -n "${pw_inum}" ] && [ "${pw_inum}" != "null" ] || continue
        echo "  [backward-scan] Prior wave $((prior_idx + 1)) issue #${pw_inum} is non-terminal. Checking labels..."
        PW_LABELS="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/labels" --jq '[.[].name]' 2>/dev/null || echo '[]')"
        [ -n "${PW_LABELS}" ] || PW_LABELS='[]'
        PW_LOCAL_ID="$(jq -r --argjson wi "${prior_idx}" --arg inum "${pw_inum}" \
          '.waves[$wi].issues[] | select((.github_issue | tostring) == $inum) | .id' "${STATE_FILE}" | head -n 1)"

        if echo "${PW_LABELS}" | jq -e 'index("ai:merged")' >/dev/null 2>&1; then
          echo "  [backward-scan] #${pw_inum} is now ai:merged. Updating state."
          jq --argjson wi "${prior_idx}" --arg inum "${pw_inum}" \
            '(.waves[$wi].issues[] | select((.github_issue | tostring) == $inum)).status = "merged"' \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          PRIOR_WAVE_REMEDIATED="true"
        elif echo "${PW_LABELS}" | jq -e 'index("ai:closed")' >/dev/null 2>&1; then
          echo "  [backward-scan] #${pw_inum} is now ai:closed. Updating state."
          jq --argjson wi "${prior_idx}" --arg inum "${pw_inum}" \
            '(.waves[$wi].issues[] | select((.github_issue | tostring) == $inum)).status = "closed"' \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          PRIOR_WAVE_REMEDIATED="true"
        elif echo "${PW_LABELS}" | jq -e 'index("ai:ready-to-merge")' >/dev/null 2>&1; then
          echo "  [backward-scan] #${pw_inum} is ai:ready-to-merge. Attempting auto-merge..."
          PW_PR="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/timeline" \
            --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
            2>/dev/null || echo "")"
          if [ -n "${PW_PR}" ] && [ "${PW_PR}" != "null" ]; then
            PW_PR_STATE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PW_PR}" --jq '.state' 2>/dev/null || echo "")"
            PW_PR_MERGEABLE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PW_PR}" --jq '.mergeable' 2>/dev/null || echo "")"
			if [ "${PW_PR_STATE}" = "open" ] && [ "${PW_PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${PW_PR}"; then
			  gh pr merge "${PW_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto 2>/dev/null \
			    || gh pr merge "${PW_PR}" --repo "${GITHUB_REPOSITORY}" --squash 2>/dev/null || true
            elif [ "${PW_PR_STATE}" = "open" ] && [ "${PW_PR_MERGEABLE}" = "false" ]; then
              gh api "repos/${GITHUB_REPOSITORY}/pulls/${PW_PR}/update-branch" \
                -X PUT -f expected_head_sha="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PW_PR}" --jq '.head.sha' 2>/dev/null)" \
                2>/dev/null || true
            fi
          fi
        else
          echo "  [backward-scan] #${pw_inum} (${PW_LOCAL_ID}) in prior wave $((prior_idx + 1)) still non-terminal. Will be caught by next poll cycle."
        fi
      done
    done
    if [ "${PRIOR_WAVE_REMEDIATED}" = "true" ]; then
      post_state_comment
    fi
  fi

  # ---------------------------------------------------------------
  # Collect label states for all child issues in the current wave
  # ---------------------------------------------------------------
  ISSUE_NUMS="$(jq -r ".waves[${WAVE_IDX}].issues[].github_issue" "${STATE_FILE}" 2>/dev/null || echo "")"

  # ---------------------------------------------------------------
  # Orphan sweep: find review-blocked issues that belong to this
  # project but are not tracked in the current wave.  Without this,
  # the in-workflow judge skips orchestrator-managed issues (exit 0)
  # expecting the poller to handle them, but the poller only scans
  # issues listed in the state file's current wave — orphans get
  # stuck with ai:review-blocked forever.
  # ---------------------------------------------------------------
  ORPHAN_RB_JSON="[]"
  if ! ORPHAN_RB_JSON="$(gh_retry gh issue list --repo "${GITHUB_REPOSITORY}" \
    --label "ai:review-blocked" --state open \
    --json number,body --limit 50)"; then
    echo "::warning::Failed to list ai:review-blocked issues during orphan sweep; skipping orphan injection for this pass."
    ORPHAN_RB_JSON="[]"
  fi

  if [ -n "${ORPHAN_RB_JSON}" ] && [ "${ORPHAN_RB_JSON}" != "[]" ]; then
    ORPHAN_COUNT="$(printf '%s' "${ORPHAN_RB_JSON}" | jq -r 'if type=="array" then length else 0 end' 2>/dev/null || echo 0)"
    if ! [[ "${ORPHAN_COUNT}" =~ ^[0-9]+$ ]]; then
      echo "::warning::Orphan sweep received invalid issue array length '${ORPHAN_COUNT}'; skipping orphan injection for this pass." >&2
      ORPHAN_COUNT=0
    fi

    if [ "${ORPHAN_COUNT}" -gt 0 ]; then
      for oidx in $(seq 0 $(( ORPHAN_COUNT - 1 ))); do
        orphan_num="$(echo "${ORPHAN_RB_JSON}" | jq -r ".[${oidx}].number")"
        orphan_body="$(echo "${ORPHAN_RB_JSON}" | jq -r ".[${oidx}].body")"

        case "${orphan_num}" in
          ''|null|*[!0-9]*)
            echo "  [orphan-sweep] Skipping orphan entry at index ${oidx}: invalid issue number '${orphan_num}'." >&2
            continue
            ;;
        esac

        # Skip if already tracked in the current wave
        already_tracked="false"
        for inum in ${ISSUE_NUMS}; do
          if [ "${inum}" = "${orphan_num}" ]; then
            already_tracked="true"
            break
          fi
        done
        [ "${already_tracked}" = "false" ] || continue

        # Skip if not part of this project (body must reference our tracking issue)
        if ! printf '%s\n' "${orphan_body}" | grep -qE "^- Tracking issue: #${TRACKING_NUM}[[:space:]]*$"; then
          continue
        fi

        # Skip if not orchestrator-managed
        if ! printf '%s' "${orphan_body}" | grep -qF "Managed by: AI Orchestrator"; then
          continue
        fi

        echo "  [orphan-sweep] Injecting orphan review-blocked issue #${orphan_num} into wave ${CURRENT_WAVE}."

        # Inject into the state file's current wave
        if jq "(.waves[${WAVE_IDX}].issues) += [{\"id\": \"orphan-rb-${orphan_num}\", \"github_issue\": ${orphan_num}, \"status\": \"in_progress\"}]" \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"; then
          ISSUE_NUMS="${ISSUE_NUMS} ${orphan_num}"
        else
          echo "::warning::Orphan sweep failed to update state file for issue #${orphan_num}; skipping orphan injection for this pass." >&2
          rm -f "${STATE_FILE}.tmp"
        fi
      done
    fi
  fi

  if [ -z "${ISSUE_NUMS}" ]; then
    echo "::warning::No issues in wave ${CURRENT_WAVE}, skipping."
    continue
  fi

  LABELS_JSON="{"
  first=true
  for inum in ${ISSUE_NUMS}; do
    # Skip null/empty entries (not-yet-created issues in deferred waves)
    if [ -z "${inum}" ] || [ "${inum}" = "null" ]; then
      continue
    fi
    LABELS="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${inum}/labels" --jq '[.[].name]' 2>/dev/null || echo '[]')"
    if [ "${first}" = true ]; then
      first=false
    else
      LABELS_JSON+=","
    fi
    LABELS_JSON+="\"${inum}\":${LABELS}"
  done
  LABELS_JSON+="}"

  # ---------------------------------------------------------------
  # Update issue phase timestamps for stall tracking
  # ---------------------------------------------------------------
  STATE_HASH_BEFORE="$(sha256sum "${STATE_FILE}" 2>/dev/null | awk '{print $1}' || true)"
  python3 scripts/orchestrate_lib.py update-timestamps \
    --state-file "${STATE_FILE}" \
    --labels-json "${LABELS_JSON}" || true
  STATE_HASH_AFTER="$(sha256sum "${STATE_FILE}" 2>/dev/null | awk '{print $1}' || true)"
  TIMESTAMP_STATE_CHANGED="false"
  if [ "${STATE_HASH_BEFORE}" != "${STATE_HASH_AFTER}" ]; then
    TIMESTAMP_STATE_CHANGED="true"
  fi

  # ---------------------------------------------------------------
  # Check wave status
  # ---------------------------------------------------------------
  WAVE_STATUS="$(python3 scripts/orchestrate_lib.py check-wave-status \
    --state-file "${STATE_FILE}" \
    --labels-json "${LABELS_JSON}")"

  echo "Wave status: ${WAVE_STATUS}"
  WAVE_COMPLETE="$(echo "${WAVE_STATUS}" | jq -r '.wave_complete')"
  ANY_FAILED="$(echo "${WAVE_STATUS}" | jq -r '.any_failed')"
  PROJECT_COMPLETE="$(echo "${WAVE_STATUS}" | jq -r '.project_complete')"

  # ---------------------------------------------------------------
  # Auto-merge: merge PRs that are ready-to-merge
  # ---------------------------------------------------------------
  echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "ready-to-merge") | .github_issue' | while read -r rtm_issue; do
    [ -n "${rtm_issue}" ] && [ "${rtm_issue}" != "null" ] || continue
    echo "  Issue #${rtm_issue} is ready-to-merge, finding linked PR..."
    RTM_PR="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${rtm_issue}/timeline" \
      --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
      2>/dev/null || echo "")"
    if [ -n "${RTM_PR}" ] && [ "${RTM_PR}" != "null" ]; then
      PR_STATE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RTM_PR}" --jq '.state' 2>/dev/null || echo "")"
      PR_MERGEABLE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RTM_PR}" --jq '.mergeable' 2>/dev/null || echo "")"
      if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ]; then
		if _pr_checks_completed "${RTM_PR}"; then
		  echo "  Merging PR #${RTM_PR} (squash)..."
		  if gh pr merge "${RTM_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto; then
		    echo "  PR #${RTM_PR} merge initiated."
		  elif gh pr merge "${RTM_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
		    echo "  PR #${RTM_PR} merged directly."
		  else
		    echo "::warning::Could not merge PR #${RTM_PR} for issue #${rtm_issue}. May need manual merge or branch protection prevents it."
		  fi
		fi
      elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
        echo "  PR #${RTM_PR} is not mergeable (mergeable=${PR_MERGEABLE}). Attempting branch update..."
        if gh api "repos/${GITHUB_REPOSITORY}/pulls/${RTM_PR}/update-branch" \
          -X PUT -f expected_head_sha="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RTM_PR}" --jq '.head.sha' 2>/dev/null)" \
          2>/dev/null; then
          echo "  PR #${RTM_PR} branch updated via API. The synchronize event will re-trigger review (including conflict resolution)."
        else
          echo "  API branch update failed for PR #${RTM_PR}. Dispatching review workflow for conflict resolution..."

          RTM_HEAD_REF="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RTM_PR}" --jq '.head.ref' 2>/dev/null || echo "")"
          if [ -n "${RTM_HEAD_REF}" ] && [ "${RTM_HEAD_REF}" != "null" ]; then
            _dispatch_rc=0
            _dispatch_review_for_conflicts "${RTM_PR}" "${RTM_HEAD_REF}" || _dispatch_rc=$?
            if [ "${_dispatch_rc}" -eq 0 ]; then
              tg_notify "PR #${RTM_PR} (issue #${rtm_issue}) has merge conflicts. Review workflow dispatched for resolution."$'\n'"PR: $(_gh_url "pull/${RTM_PR}")"$'\n'"Issue: $(_gh_url "issues/${rtm_issue}")" "WARNING"
            elif [ "${_dispatch_rc}" -eq 2 ]; then
              echo "  PR #${RTM_PR}: autofix already in progress, skipping dispatch."
            else
              tg_notify "PR #${RTM_PR} (issue #${rtm_issue}) has merge conflicts. Could not dispatch review workflow."$'\n'"PR: $(_gh_url "pull/${RTM_PR}")"$'\n'"Issue: $(_gh_url "issues/${rtm_issue}")" "WARNING"
            fi
          else
            echo "::warning::Could not determine head ref for PR #${RTM_PR}."
          fi
        fi
      else
        echo "  PR #${RTM_PR} state=${PR_STATE} mergeable=${PR_MERGEABLE}, skipping."
      fi
    else
      echo "  No linked PR found for issue #${rtm_issue}."
    fi
  done

  # ---------------------------------------------------------------
  # Auto-resolve merge conflicts on in-progress PRs
  # ---------------------------------------------------------------
  # When the base branch advances (e.g. another PR merges), existing
  # PRs may develop merge conflicts.  The review workflow already has
  # Codex-based conflict resolution, but it only runs on PR events
  # (opened/synchronize/reopened).  No event fires when the *base*
  # branch moves, so the review workflow is never re-triggered.
  #
  # This block detects in-progress issues whose linked PRs have become
  # unmergeable.  First tries the GitHub API update-branch endpoint
  # (handles clean merges).  If that fails (real conflicts), dispatches
  # the review workflow via workflow_dispatch so it can resolve
  # conflicts on a dedicated runner with a clean environment.
  # ---------------------------------------------------------------
  echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "in_progress") | .github_issue' | while read -r ip_issue; do
    [ -n "${ip_issue}" ] && [ "${ip_issue}" != "null" ] || continue
    IP_PR="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${ip_issue}/timeline" \
      --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
      2>/dev/null || echo "")"
    if [ -z "${IP_PR}" ] || [ "${IP_PR}" = "null" ]; then
      continue
    fi
    IP_PR_STATE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${IP_PR}" --jq '.state' 2>/dev/null || echo "")"
    IP_MERGEABLE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${IP_PR}" --jq '.mergeable' 2>/dev/null || echo "")"
    if [ "${IP_PR_STATE}" != "open" ] || [ "${IP_MERGEABLE}" != "false" ]; then
      continue
    fi
    echo "  In-progress issue #${ip_issue} has PR #${IP_PR} with merge conflicts. Running Codex conflict resolution..."

    # Try the GitHub API update-branch first (creates a merge commit
    # if the merge is clean; fails when there are real conflicts).
    if gh api "repos/${GITHUB_REPOSITORY}/pulls/${IP_PR}/update-branch" \
      -X PUT -f expected_head_sha="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${IP_PR}" --jq '.head.sha' 2>/dev/null)" \
      2>/dev/null; then
      echo "  PR #${IP_PR} branch updated via API. Synchronize event will re-trigger review."
      tg_notify "PR #${IP_PR} (issue #${ip_issue}) had merge conflicts. Branch updated via API to re-trigger review."$'\n'"PR: $(_gh_url "pull/${IP_PR}")"$'\n'"Issue: $(_gh_url "issues/${ip_issue}")" "WARNING"
      continue
    fi

    # API update failed — real conflicts exist.  Dispatch review workflow.
    IP_HEAD_REF="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${IP_PR}" --jq '.head.ref' 2>/dev/null || echo "")"
    if [ -n "${IP_HEAD_REF}" ] && [ "${IP_HEAD_REF}" != "null" ]; then
      _dispatch_rc=0
      _dispatch_review_for_conflicts "${IP_PR}" "${IP_HEAD_REF}" || _dispatch_rc=$?
      if [ "${_dispatch_rc}" -eq 0 ]; then
        tg_notify "PR #${IP_PR} (issue #${ip_issue}) has merge conflicts. Review workflow dispatched for resolution."$'\n'"PR: $(_gh_url "pull/${IP_PR}")"$'\n'"Issue: $(_gh_url "issues/${ip_issue}")" "WARNING"
      elif [ "${_dispatch_rc}" -eq 2 ]; then
        echo "  PR #${IP_PR}: autofix already in progress, skipping dispatch."
      else
        tg_notify "PR #${IP_PR} (issue #${ip_issue}) has merge conflicts. Could not dispatch review workflow."$'\n'"PR: $(_gh_url "pull/${IP_PR}")"$'\n'"Issue: $(_gh_url "issues/${ip_issue}")" "WARNING"
      fi
    else
      echo "::warning::Could not determine head ref for in-progress PR #${IP_PR}."
    fi
  done

  # ---------------------------------------------------------------
  # Handle review-blocked issues: invoke judge to decide
  # ---------------------------------------------------------------
  ANY_REVIEW_BLOCKED="$(echo "${WAVE_STATUS}" | jq -r '.any_review_blocked')"
  if [ "${ANY_REVIEW_BLOCKED}" = "true" ]; then
    echo "Detected review-blocked issues in wave ${CURRENT_WAVE}. Invoking judge to unblock..."

    # Ensure codex config exists for the judge
    mkdir -p ~/.codex
    CATALOG_PATH="$(pwd)/scripts/codex_model_catalog.json"
    {
      echo 'web_search = "live"'
      echo 'model_provider = "openrouter"'
      echo "model = \"${MODEL_EDITOR}\""
      echo "model_reasoning_effort = \"${MODEL_REASONING_EFFORT_JUDGE}\""
      if [ -f "${CATALOG_PATH}" ]; then
        echo "model_catalog_json = \"${CATALOG_PATH}\""
      fi
      echo
      echo '[model_providers.openrouter]'
      echo 'name = "OpenRouter"'
      echo 'base_url = "https://openrouter.ai/api/v1"'
      echo 'env_key = "OPENROUTER_API_KEY"'
      echo 'wire_api = "responses"'
      echo 'stream_idle_timeout_ms = 600000'
      echo 'stream_max_retries = 5'
      echo 'request_max_retries = 3'
      echo
      echo '[sandbox_workspace_write]'
      echo 'network_access = true'
    } > ~/.codex/config.toml

    # Setup Serena for judge
    bash scripts/setup_serena.sh --mode editing --context codex || true

    MAX_REVIEW_BLOCKED_RETRIES="${MAX_REVIEW_BLOCKED_RETRIES:-2}"
    REVIEW_BLOCKED_STATE_CHANGED=false

    while read -r rb_issue; do
      [ -n "${rb_issue}" ] && [ "${rb_issue}" != "null" ] || continue
      echo "  Processing review-blocked issue #${rb_issue}..."

      # Track retries per issue
      RETRY_COUNT="$(jq -r ".review_blocked_retries[\"${rb_issue}\"] // 0" "${STATE_FILE}")"
      echo "  Retry count for #${rb_issue}: ${RETRY_COUNT}/${MAX_REVIEW_BLOCKED_RETRIES}"

      # Find linked PR
      RB_PR="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${rb_issue}/timeline" \
        --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
        2>/dev/null || echo "")"
      if [ -z "${RB_PR}" ] || [ "${RB_PR}" = "null" ]; then
        echo "  No linked PR found for issue #${rb_issue}, skipping."
        continue
      fi
      echo "  Linked PR: #${RB_PR}"

      # Guard: check PR state before invoking the judge
      RB_PR_STATE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.state' 2>/dev/null || echo "")"
      RB_PR_MERGED="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.merged' 2>/dev/null || echo "false")"
      if [ "${RB_PR_STATE}" != "open" ] && [ "${RB_PR_MERGED}" != "true" ]; then
        # PR is closed (not merged) — skip entirely
        echo "  PR #${RB_PR} is closed (not merged). Cleaning up labels and skipping."
        ensure_label_exists "ai:closed"
        gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
          --remove-label 'ai:review-blocked' --remove-label 'ai:in-progress' \
          --add-label 'ai:closed' 2>/dev/null || true
        REVIEW_BLOCKED_STATE_CHANGED=true
        continue
      elif [ "${RB_PR_MERGED}" = "true" ]; then
        # PR was merged — judge should still run; fixes will go into a follow-up PR
        echo "  PR #${RB_PR} is already merged. Judge fixes will target a follow-up PR against ${DEFAULT_BRANCH:-main}."
      fi

      # Collect full PR context for the judge
      PR_DIFF="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" \
        -H 'Accept: application/vnd.github.diff' 2>/dev/null || echo "(diff unavailable)")"
      PR_COMMENTS="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${RB_PR}/comments" \
        | jq -s 'add // [] | [.[] | {author: .user.login, body: .body, created_at: .created_at}]' 2>/dev/null || echo "[]")"
      PR_REVIEW_COMMENTS="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}/comments" \
        | jq -s 'add // [] | [.[] | {author: .user.login, path: .path, line: .line, body: .body}]' 2>/dev/null || echo "[]")"
      PR_META="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" \
        --jq '{title: .title, body: .body, head_ref: .head.ref, base_ref: .base.ref, head_sha: .head.sha}' 2>/dev/null || echo "{}")"
      ISSUE_BODY="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${rb_issue}" --jq '.body' 2>/dev/null || echo "")"

      # Determine if this is a final decision (retries exhausted) or a fix attempt
      IS_FINAL="false"
      if [ "${RETRY_COUNT}" -ge "${MAX_REVIEW_BLOCKED_RETRIES}" ]; then
        IS_FINAL="true"
        echo "  Retries exhausted — judge will make final decision (merge or close+reissue)."
      fi

      # Build the judge prompt for review-blocked evaluation
      RB_JUDGE_PROMPT_FILE="${RUNTIME_DIR}/rb_judge_prompt_${rb_issue}.txt"
      RB_JUDGE_OUTPUT_FILE="${RUNTIME_DIR}/rb_judge_output_${rb_issue}.txt"

      {
        if [ -f "${RUNTIME_DIR}/judge_static.txt" ]; then
          cat "${RUNTIME_DIR}/judge_static.txt"
        else
          # Build static context if not already assembled
          if [ -f codex_system_instructions.md ]; then
            echo "=== SYSTEM INSTRUCTIONS ==="
            cat codex_system_instructions.md
            echo
          fi
          if [ -f ai_pipeline.md ]; then
            echo "=== AI PIPELINE ==="
            cat ai_pipeline.md
            echo
          fi
        fi
        echo
        echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE}"
        echo
        echo "=== REVIEW-BLOCKED JUDGE TASK ==="
        echo
        cat prompts/mode-judge-review-blocked.txt
        echo
        echo "=== ISSUE #${rb_issue} (original requirement) ==="
        echo
        echo "${ISSUE_BODY}"
        echo
        echo "=== PR #${RB_PR} METADATA ==="
        echo
        echo "${PR_META}" | jq '.'
        echo
        echo "=== PR #${RB_PR} DIFF ==="
        echo
        head -1000 <<< "${PR_DIFF}"
        echo
        echo "=== PR #${RB_PR} COMMENTS (editor summaries, reviewer findings) ==="
        echo
        echo "${PR_COMMENTS}" | jq '.'
        echo
        echo "=== PR #${RB_PR} INLINE REVIEW COMMENTS ==="
        echo
        echo "${PR_REVIEW_COMMENTS}" | jq '.'
        echo
        echo "=== ORCHESTRATOR CONTEXT ==="
        echo "Review-blocked retry: $((RETRY_COUNT + 1)) of ${MAX_REVIEW_BLOCKED_RETRIES}"
        echo "Retries exhausted: ${IS_FINAL}"
        if [ "${IS_FINAL}" = "true" ]; then
          echo
          echo "IMPORTANT: This is the FINAL attempt. You MUST choose either 'merge' or"
          echo "'close_and_reissue'. The 'fix' option is NOT available because previous"
          echo "fix attempts did not resolve the issues. Pick the action that best serves"
          echo "the project: merge if the PR is good enough, or close and reissue if the"
          echo "approach is fundamentally wrong."
        fi
      } > "${RB_JUDGE_PROMPT_FILE}"

      # Run the judge
      RB_JUDGE_SUCCESS=false
      for attempt in 1 2; do
        echo "  Review-blocked judge attempt ${attempt}/2..."
        if cat "${RB_JUDGE_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${RB_JUDGE_OUTPUT_FILE}" 2>/dev/null; then
          if grep -q '[^[:space:]]' "${RB_JUDGE_OUTPUT_FILE}"; then
            RB_JUDGE_SUCCESS=true
            break
          fi
        fi
        if [ "${attempt}" -lt 2 ]; then
          sleep 10
        fi
      done

      if [ "${RB_JUDGE_SUCCESS}" != "true" ]; then
        echo "::warning::Review-blocked judge failed for issue #${rb_issue}"
        tg_notify "Review-blocked judge failed for issue #${rb_issue} (PR #${RB_PR}). Will retry next poll cycle."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
        continue
      fi

      # Parse judge output
      RB_JUDGE_JSON="$(python3 -c "
import json, re, sys

raw = open('${RB_JUDGE_OUTPUT_FILE}', 'r').read()

try:
    data = json.loads(raw.strip())
    json.dump(data, sys.stdout)
    sys.exit(0)
except json.JSONDecodeError:
    pass

cleaned = re.sub(r'\`\`\`(?:json)?\s*', '', raw)
cleaned = re.sub(r'\`\`\`\s*$', '', cleaned, flags=re.MULTILINE)

brace_depth = 0
start = None
for i, ch in enumerate(cleaned):
    if ch == '{':
        if brace_depth == 0:
            start = i
        brace_depth += 1
    elif ch == '}':
        brace_depth -= 1
        if brace_depth == 0 and start is not None:
            candidate = cleaned[start:i+1]
            try:
                data = json.loads(candidate)
                json.dump(data, sys.stdout)
                sys.exit(0)
            except json.JSONDecodeError:
                start = None

print('Could not parse review-blocked judge JSON', file=sys.stderr)
sys.exit(1)
" 2>/dev/null || echo "")"

      if [ -z "${RB_JUDGE_JSON}" ]; then
        echo "::warning::Could not parse review-blocked judge output for #${rb_issue}"
        continue
      fi

      RB_ACTION="$(echo "${RB_JUDGE_JSON}" | jq -r '.action')"
      RB_JUSTIFICATION="$(echo "${RB_JUDGE_JSON}" | jq -r '.justification // "no justification"')"
      RB_FIX_DESC="$(echo "${RB_JUDGE_JSON}" | jq -r '.fix_description // ""')"
      RB_REMAINING="$(echo "${RB_JUDGE_JSON}" | jq -r '.remaining_issues_summary // ""')"

      echo "  Judge decision for #${rb_issue}: ${RB_ACTION}"
      echo "  Justification: ${RB_JUSTIFICATION}"

      # Post judge assessment to PR
      RB_COMMENT="## Orchestrator Review-Blocked Judge — Issue #${rb_issue}

**Decision:** ${RB_ACTION}
**Retry:** $((RETRY_COUNT + 1)) of ${MAX_REVIEW_BLOCKED_RETRIES}
**Justification:** ${RB_JUSTIFICATION}

**Remaining issues:** ${RB_REMAINING}"

      gh api "repos/${GITHUB_REPOSITORY}/issues/${RB_PR}/comments" \
        -f body="${RB_COMMENT}" >/dev/null 2>&1 || true

      case "${RB_ACTION}" in
        merge)
          echo "  Judge says merge PR #${RB_PR} as-is."
          # Remove review-blocked, set ready-to-merge
          ensure_label_exists "ai:ready-to-merge"
          gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
            --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true

          # Attempt squash merge (with branch update if needed)
          RB_MERGED="false"
          PR_STATE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.state' 2>/dev/null || echo "")"
          PR_MERGEABLE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.mergeable' 2>/dev/null || echo "")"
		  if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${RB_PR}"; then
		    if gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto; then
		      echo "  PR #${RB_PR} merge initiated (auto)."
		      RB_MERGED="true"
		    elif gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
		      echo "  PR #${RB_PR} merged directly."
		      RB_MERGED="true"
		    else
		      echo "::warning::Could not merge PR #${RB_PR}."
		    fi
          elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
            echo "  PR #${RB_PR} is not mergeable. Attempting branch update..."
            if gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}/update-branch" \
              -X PUT -f expected_head_sha="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.head.sha' 2>/dev/null)" \
              2>/dev/null; then
              echo "  PR #${RB_PR} branch updated. Synchronize event will re-trigger review + conflict resolution."
            else
              echo "  API branch update failed for review-blocked PR #${RB_PR}. Dispatching review workflow for conflict resolution..."
              RB_HEAD_REF="$(echo "${PR_META}" | jq -r '.head_ref')"
              if [ -n "${RB_HEAD_REF}" ] && [ "${RB_HEAD_REF}" != "null" ]; then
                _dispatch_rc=0
                _dispatch_review_for_conflicts "${RB_PR}" "${RB_HEAD_REF}" || _dispatch_rc=$?
                if [ "${_dispatch_rc}" -eq 0 ]; then
                  tg_notify "Review-blocked PR #${RB_PR} (issue #${rb_issue}) has merge conflicts. Review workflow dispatched for resolution."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
                elif [ "${_dispatch_rc}" -eq 2 ]; then
                  echo "  PR #${RB_PR}: autofix already in progress, skipping dispatch."
                else
                  tg_notify "Review-blocked PR #${RB_PR} (issue #${rb_issue}) has merge conflicts. Could not dispatch review workflow."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
                fi
              else
                echo "::warning::Could not determine head ref for review-blocked PR #${RB_PR}."
              fi
            fi
          else
            echo "  PR #${RB_PR} state=${PR_STATE} mergeable=${PR_MERGEABLE}, cannot merge yet."
          fi

          REVIEW_BLOCKED_STATE_CHANGED=true
          if [ "${RB_MERGED}" = "true" ]; then
            tg_notify "Orchestrator judge merged review-blocked PR #${RB_PR} (issue #${rb_issue}): ${RB_JUSTIFICATION}"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
          fi
          ;;

        fix)
          if [ "${IS_FINAL}" = "true" ]; then
            echo "  Judge returned 'fix' but retries exhausted — treating as merge."
            ensure_label_exists "ai:ready-to-merge"
            gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
              --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true
            RB_FORCE_MERGED="false"
            PR_STATE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.state' 2>/dev/null || echo "")"
            PR_MERGEABLE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.mergeable' 2>/dev/null || echo "")"
				if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${RB_PR}"; then
				  if gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto \
				    || gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
				    RB_FORCE_MERGED="true"
				  else
				    echo "::warning::Could not merge PR #${RB_PR} in force-merge path."
				  fi
            elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
              echo "  PR #${RB_PR} is not mergeable (force-merge path). Attempting branch update..."
              if gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}/update-branch" \
                -X PUT -f expected_head_sha="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.head.sha' 2>/dev/null)" \
                2>/dev/null; then
                echo "  PR #${RB_PR} branch updated. Will retry force-merge on next poll cycle."
              else
                echo "  API branch update failed for force-merge PR #${RB_PR}. Dispatching review workflow for conflict resolution..."
                RB_HEAD_REF="$(echo "${PR_META}" | jq -r '.head_ref')"
                if [ -n "${RB_HEAD_REF}" ] && [ "${RB_HEAD_REF}" != "null" ]; then
                  _dispatch_rc=0
                  _dispatch_review_for_conflicts "${RB_PR}" "${RB_HEAD_REF}" || _dispatch_rc=$?
                  if [ "${_dispatch_rc}" -eq 0 ]; then
                    echo "  PR #${RB_PR} review workflow dispatched. Will retry force-merge on next poll cycle."
                  elif [ "${_dispatch_rc}" -eq 2 ]; then
                    echo "  PR #${RB_PR}: autofix already in progress, skipping dispatch. Will retry force-merge on next poll cycle."
                  else
                    tg_notify "Force-merge PR #${RB_PR} (issue #${rb_issue}) has merge conflicts. Could not dispatch review workflow."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
                  fi
                else
                  echo "::warning::Could not determine head ref for force-merge PR #${RB_PR}."
                fi
              fi
            fi
            REVIEW_BLOCKED_STATE_CHANGED=true
            if [ "${RB_FORCE_MERGED}" = "true" ]; then
              tg_notify "Orchestrator force-merged review-blocked PR #${RB_PR} (retries exhausted, issue #${rb_issue})"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
            fi
          else
            echo "  Judge is applying fixes to PR #${RB_PR}..."
            # Re-check PR state before expensive fix+push (race condition safety net)
            RB_PR_STATE_NOW="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.state' 2>/dev/null || echo "")"
            RB_PR_MERGED_NOW="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.merged' 2>/dev/null || echo "false")"
            if [ "${RB_PR_STATE_NOW}" != "open" ] && [ "${RB_PR_MERGED_NOW}" != "true" ]; then
              # PR was closed without merge — skip
              echo "::warning::PR #${RB_PR} is closed (not merged). Skipping fix application."
              gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                --remove-label 'ai:review-blocked' 2>/dev/null || true
              REVIEW_BLOCKED_STATE_CHANGED=true
            else
            # Determine target: push to PR branch (open) or create follow-up PR (merged)
            RB_TARGET_MERGED="false"
            if [ "${RB_PR_MERGED}" = "true" ] || [ "${RB_PR_MERGED_NOW}" = "true" ]; then
              RB_TARGET_MERGED="true"
            fi

            HEAD_REF="$(echo "${PR_META}" | jq -r '.head_ref')"
            BASE_REF="$(echo "${PR_META}" | jq -r '.base_ref')"
            : "${BASE_REF:=${DEFAULT_BRANCH:-main}}"

            if [ "${RB_TARGET_MERGED}" = "true" ]; then
              # PR already merged — work on a follow-up branch from the base branch
              FOLLOWUP_BRANCH="fix/${rb_issue}-followup-$(date +%s)"
              echo "  PR already merged. Creating follow-up branch ${FOLLOWUP_BRANCH} from ${BASE_REF}."
              git fetch --no-tags origin "refs/heads/${BASE_REF}:refs/remotes/origin/${BASE_REF}" 2>/dev/null || true
              git checkout -B "${FOLLOWUP_BRANCH}" "refs/remotes/origin/${BASE_REF}" 2>/dev/null || true
            elif [ -n "${HEAD_REF}" ] && [ "${HEAD_REF}" != "null" ]; then
              # PR is open — push to existing PR branch
              git fetch --no-tags origin "refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF}" 2>/dev/null || true
              git checkout -B "${HEAD_REF}" "refs/remotes/origin/${HEAD_REF}" 2>/dev/null || true
            else
              echo "::warning::Cannot determine PR head branch for #${RB_PR}."
              git checkout "${DEFAULT_BRANCH:-main}" 2>/dev/null || git checkout - 2>/dev/null || true
              REVIEW_BLOCKED_STATE_CHANGED=true
              # Skip to retry counter via nested-fi + fi
            fi

            if [ "${RB_TARGET_MERGED}" = "true" ] || { [ -n "${HEAD_REF}" ] && [ "${HEAD_REF}" != "null" ]; }; then
              # Re-run the judge in editing mode on the target branch
              RB_FIX_PROMPT_FILE="${RUNTIME_DIR}/rb_fix_prompt_${rb_issue}.txt"
              RB_FIX_OUTPUT_FILE="${RUNTIME_DIR}/rb_fix_output_${rb_issue}.txt"
              {
                cat "${RB_JUDGE_PROMPT_FILE}"
                echo
                echo "=== APPLY FIXES NOW ==="
                if [ "${RB_TARGET_MERGED}" = "true" ]; then
                  echo "You are on a follow-up branch based on ${BASE_REF}."
                  echo "The original PR #${RB_PR} was already merged."
                  echo "Apply only the fixes identified during review — do not re-apply the original PR's changes."
                else
                  echo "You are now on the PR branch (${HEAD_REF})."
                fi
                echo "Apply the fixes you identified directly to the repository files."
                echo "Focus only on the issues that blocked the review."
                echo "Do not create new files unless absolutely required."
                echo "After applying fixes, output the same JSON with action='fix' and"
                echo "fix_description describing what you changed."
              } > "${RB_FIX_PROMPT_FILE}"

              if cat "${RB_FIX_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${RB_FIX_OUTPUT_FILE}" 2>/dev/null; then
                echo "  Fix codex completed."
              else
                echo "::warning::Fix codex failed for PR #${RB_PR}."
              fi

              # Remove workflow-generated/fetched artifacts so they are never
              # committed to caller repos.
              if [[ "${GITHUB_REPOSITORY}" != *"/coding-workflows" ]]; then
                rm -f ./pre_assembled_static.txt
                rm -f codex_system_instructions.md ai_pipeline.md unattended_llm_system_instructions.md agents.md
                rm -f scripts/setup_serena.sh scripts/git_ref_health_check.sh scripts/serena_efficiency_report.py \
                  scripts/generate_symbol_diff_summary.py scripts/label_helpers.sh scripts/tg_helpers.sh \
                  scripts/codex_model_catalog.json scripts/orchestrate_poll_process.sh scripts/orchestrate_lib.py
                rm -rf .serena prompts
                rm -f .github/ai/orchestrate_schema.v1.json
              fi

              # Check if there are changes to commit
              if [ -n "$(git status --porcelain)" ]; then
                git config user.name "codex-bot"
                git config user.email "codex@users.noreply.github.com"
                if [ "${ALLOW_WORKFLOW_EDITS:-false}" = "true" ]; then
                  git add -u -- ':!node_modules' ':!.serena'
                  git ls-files --others --exclude-standard -z -- ':!node_modules' ':!.serena' | xargs -0 -r git add --
                else
                  git add -u -- ':!node_modules' ':!scripts' ':!prompts' ':!.github/ai' ':!.serena'
                  git ls-files --others --exclude-standard -z -- ':!node_modules' ':!.serena' ':!scripts' ':!prompts' ':!.github/ai' | xargs -0 -r git add --
                fi
                git commit -m "[orchestrator-fix] address review-blocked issues for #${rb_issue}

Orchestrator judge applied fixes to unblock the review pipeline.
Retry $((RETRY_COUNT + 1)) of ${MAX_REVIEW_BLOCKED_RETRIES}.

${RB_FIX_DESC}" || true

                git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}"

                if [ "${RB_TARGET_MERGED}" = "true" ]; then
                  # Push follow-up branch and create a new PR
                  if git push origin "HEAD:${FOLLOWUP_BRANCH}" 2>/dev/null; then
                    echo "  Pushed follow-up branch ${FOLLOWUP_BRANCH}."
                    FOLLOWUP_PR_URL="$(gh pr create \
                      --repo "${GITHUB_REPOSITORY}" \
                      --base "${BASE_REF}" \
                      --head "${FOLLOWUP_BRANCH}" \
                      --title "[orchestrator-fix] follow-up fixes for #${rb_issue}" \
                      --body "Follow-up fixes for issues identified during review of PR #${RB_PR} (already merged).

Closes #${rb_issue}

**Original issue:** #${rb_issue}
**Original PR:** #${RB_PR}

${RB_FIX_DESC}

---
*Created automatically by the orchestrator judge.*" 2>/dev/null || echo "")"
                    if [ -n "${FOLLOWUP_PR_URL}" ]; then
                      echo "  Created follow-up PR: ${FOLLOWUP_PR_URL}"
                      gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                        --remove-label 'ai:review-blocked' 2>/dev/null || true
                      tg_notify "Orchestrator judge created follow-up PR for merged PR #${RB_PR} (issue #${rb_issue}): ${FOLLOWUP_PR_URL}"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
                    else
                      echo "::warning::Failed to create follow-up PR for merged PR #${RB_PR}."
                    fi
                  else
                    echo "::warning::Failed to push follow-up branch ${FOLLOWUP_BRANCH}."
                  fi
                else
                  # Push to existing open PR branch
                  if git push origin "HEAD:${HEAD_REF}" 2>/dev/null; then
                    echo "  Pushed [orchestrator-fix] commit to ${HEAD_REF}."
                    # Remove review-blocked label — the push triggers synchronize
                    # which re-runs review_autofix with a reset autofix counter.
                    gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                      --remove-label 'ai:review-blocked' 2>/dev/null || true
                    tg_notify "Orchestrator judge pushed fix for review-blocked PR #${RB_PR} (issue #${rb_issue}, retry $((RETRY_COUNT + 1))/${MAX_REVIEW_BLOCKED_RETRIES})"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
                  else
                    echo "::warning::Failed to push orchestrator fix for PR #${RB_PR}."
                  fi
                fi
              else
                echo "  Judge produced no file changes."
                if [ "${RB_TARGET_MERGED}" = "true" ]; then
                  echo "  No follow-up needed — merged PR has no outstanding fixes."
                  gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                    --remove-label 'ai:review-blocked' 2>/dev/null || true
                  tg_notify "Orchestrator judge found no fixes needed for merged PR #${RB_PR} (issue #${rb_issue})"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
                else
                  echo "  Treating as merge decision."
                  ensure_label_exists "ai:ready-to-merge"
                  gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                    --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true
                  PR_STATE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.state' 2>/dev/null || echo "")"
                  PR_MERGEABLE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.mergeable' 2>/dev/null || echo "")"
                  if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ] && _pr_checks_completed "${RB_PR}"; then
                    if gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto \
                      || gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash; then
                      tg_notify "Orchestrator judge merged PR #${RB_PR} (no fix changes needed, issue #${rb_issue})"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "DEBUG"
                    else
                      echo "::warning::Could not merge PR #${RB_PR} in no-fix merge path."
                    fi
                  elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
                    echo "  PR #${RB_PR} is not mergeable in no-fix merge path. Skipping merge notification."
                  fi
                fi
              fi

              # Switch back to default branch for remaining processing
              git checkout "${DEFAULT_BRANCH:-main}" 2>/dev/null || git checkout - 2>/dev/null || true
            fi

            # Increment retry counter
            jq ".review_blocked_retries[\"${rb_issue}\"] = $((RETRY_COUNT + 1))" \
              "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
            REVIEW_BLOCKED_STATE_CHANGED=true
          fi
          fi
          ;;

        close_and_reissue)
          echo "  Judge says close PR #${RB_PR} and reissue."
          # Close the PR
          gh pr close "${RB_PR}" --repo "${GITHUB_REPOSITORY}" \
            --comment "Closed by orchestrator judge — the approach needs rework. A new issue will be created with refined guidance." \
            2>/dev/null || true

          # Label issue as closed
          ensure_label_exists "ai:closed"
          gh_retry gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
            --remove-label 'ai:review-blocked' --remove-label 'ai:done' \
            --add-label 'ai:closed' 2>/dev/null || true

          # Create replacement issue
          NEW_ISSUE_TITLE="$(echo "${RB_JUDGE_JSON}" | jq -r '.new_issue.title // empty')"
          NEW_ISSUE_BODY="$(echo "${RB_JUDGE_JSON}" | jq -r '.new_issue.body // empty' | sed 's/\\n/\n/g')"
          if [ -n "${NEW_ISSUE_TITLE}" ] && [ -n "${NEW_ISSUE_BODY}" ]; then
            RB_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
            FULL_NEW_BODY="${NEW_ISSUE_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Integration branch: ${RB_INTEGRATION_BRANCH}
- Replaces: #${rb_issue} (PR #${RB_PR} closed — approach rework)
- Type: review-blocked-reissue
- Managed by: AI Orchestrator"

            ensure_label_exists "ai:clarification"
            NEW_URL="$(gh issue create \
              --repo "${GITHUB_REPOSITORY}" \
              --title "${NEW_ISSUE_TITLE}" \
              --body "${FULL_NEW_BODY}" \
              --label "ai:clarification")"
            NEW_URL_CLEAN="$(printf '%s\n' "${NEW_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
            NEW_NUM="$(basename "${NEW_URL_CLEAN%%[?#]*}")"
            echo "  Created replacement issue #${NEW_NUM}: ${NEW_ISSUE_TITLE}"

            # Get local_id for the blocked issue and remap it
            LOCAL_ID="$(echo "${WAVE_STATUS}" | jq -r ".issues[] | select(.github_issue == \"${rb_issue}\") | .id")"
            if [[ "${NEW_NUM}" =~ ^[0-9]+$ ]] && [ -n "${LOCAL_ID}" ] && [ "${LOCAL_ID}" != "null" ]; then
              jq ".issue_number_map[\"${LOCAL_ID}\"] = ${NEW_NUM}" \
                "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
              # Update the wave entry
              jq "(.waves[${WAVE_IDX}].issues[] | select(.id == \"${LOCAL_ID}\")) |= (.github_issue = ${NEW_NUM} | .status = \"pending\")" \
                "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
            fi

            tg_notify "Orchestrator closed PR #${RB_PR} and reissued as #${NEW_NUM} (issue #${rb_issue}): ${RB_JUSTIFICATION}"$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"New issue: $(_gh_url "issues/${NEW_NUM}")"$'\n'"Old issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
          else
            echo "::warning::Judge chose close_and_reissue but provided no new issue details."
            tg_notify "Orchestrator closed PR #${RB_PR} (issue #${rb_issue}) but could not create replacement issue."$'\n'"PR: $(_gh_url "pull/${RB_PR}")"$'\n'"Issue: $(_gh_url "issues/${rb_issue}")" "WARNING"
          fi

          REVIEW_BLOCKED_STATE_CHANGED=true
          ;;

        *)
          echo "::warning::Unknown review-blocked judge action: ${RB_ACTION}"
          ;;
      esac
    done < <(echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "review-blocked") | .github_issue')

    # Persist updated state if any review-blocked issues were handled
    if [ "${REVIEW_BLOCKED_STATE_CHANGED}" = "true" ]; then
      post_state_comment

      # Re-check wave status after handling review-blocked issues
      # (some may have been merged or reissued)
      echo "Re-checking wave status after review-blocked handling..."
      LABELS_JSON="{"
      first=true
      for inum in ${ISSUE_NUMS}; do
        if [ -z "${inum}" ] || [ "${inum}" = "null" ]; then
          continue
        fi
        # Re-read labels since we may have changed them
        LABELS="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${inum}/labels" --jq '[.[].name]' 2>/dev/null || echo '[]')"
        if [ "${first}" = true ]; then
          first=false
        else
          LABELS_JSON+=","
        fi
        LABELS_JSON+="\"${inum}\":${LABELS}"
      done
      LABELS_JSON+="}"

      # Also check any reissued issue numbers
      REISSUED_NUMS="$(jq -r '.waves['"${WAVE_IDX}"'].issues[].github_issue' "${STATE_FILE}" 2>/dev/null | sort -u)"
      for rnum in ${REISSUED_NUMS}; do
        if [ -z "${rnum}" ] || [ "${rnum}" = "null" ]; then continue; fi
        if echo "${LABELS_JSON}" | jq -e --arg key "${rnum}" 'has($key)' >/dev/null 2>&1; then
          continue
        fi
        LABELS="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${rnum}/labels" --jq '[.[].name]' 2>/dev/null || echo '[]')"
        [ -z "${LABELS}" ] && LABELS='[]'
        LABELS_JSON="$(echo "${LABELS_JSON}" | jq -c --arg key "${rnum}" --argjson labels "${LABELS}" '. + {($key): $labels}')"
      done

      WAVE_STATUS="$(python3 scripts/orchestrate_lib.py check-wave-status \
        --state-file "${STATE_FILE}" \
        --labels-json "${LABELS_JSON}")"
      WAVE_COMPLETE="$(echo "${WAVE_STATUS}" | jq -r '.wave_complete')"
      ANY_FAILED="$(echo "${WAVE_STATUS}" | jq -r '.any_failed')"
      echo "Updated wave status after review-blocked handling: complete=${WAVE_COMPLETE}, failed=${ANY_FAILED}"
    fi
  fi

  # ---------------------------------------------------------------
  # Handle implementation-failed issues: close and re-issue
  # ---------------------------------------------------------------
  IMPL_FAILED_STATE_CHANGED=false
  while read -r if_issue; do
    [ -n "${if_issue}" ] && [ "${if_issue}" != "null" ] || continue

    # Look up local_id for this issue so we can track no-op count
    IF_LOCAL_ID="$(jq -r --arg if_issue "${if_issue}" --argjson wave_idx "${WAVE_IDX}" \
      '.waves[$wave_idx].issues[] | select((.github_issue | tostring) == $if_issue) | .id' \
      "${STATE_FILE}" | head -n 1)"

    # ---- No-op loop guard: if this task has already been re-issued
    # MAX_IMPL_NOOP_REISSUES times without producing changes, the code
    # likely already exists on main.  Close the issue and let the
    # wave-completion judge verify instead of looping forever.
    NOOP_COUNT="$(get_impl_noop_count "${IF_LOCAL_ID}")"
    # The current failure is itself a no-op, so the observed count
    # includes this cycle even though we haven't bumped yet.
    OBSERVED_NOOP_COUNT=$((NOOP_COUNT + 1))
    # Cap semantics: MAX_IMPL_NOOP_REISSUES controls how many re-issues
    # are allowed after prior no-op failures.
    if [ "${NOOP_COUNT}" -ge "${MAX_IMPL_NOOP_REISSUES}" ]; then
      echo "  Issue #${if_issue} (${IF_LOCAL_ID}) hit implementation no-op cap (${OBSERVED_NOOP_COUNT}/${MAX_IMPL_NOOP_REISSUES}). Closing as likely already resolved — judge will verify."
      bump_impl_noop_count "${IF_LOCAL_ID}"
      gh issue edit "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
        --remove-label 'ai:implementation-failed' --add-label 'ai:closed' 2>/dev/null || true
      gh issue close "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
        -c "Closing: implementation produced no changes ${OBSERVED_NOOP_COUNT} time(s). The code described in this issue likely already exists on the default branch. The wave-completion judge will verify." 2>/dev/null || true
      tg_notify "Issue #${if_issue} (${IF_LOCAL_ID}) hit impl no-op cap (${OBSERVED_NOOP_COUNT}). Closed as likely already resolved — judge will verify."$'\n'"Issue: $(_gh_url "issues/${if_issue}")" "WARNING"
      IMPL_FAILED_STATE_CHANGED=true
      continue
    fi

    echo "  Issue #${if_issue} has implementation-failed (no-op ${OBSERVED_NOOP_COUNT}/${MAX_IMPL_NOOP_REISSUES}). Closing and re-issuing..."

    # Increment no-op counter before re-issuing
    bump_impl_noop_count "${IF_LOCAL_ID}"

    # Read the original issue to preserve its content
    IF_TITLE="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.title' 2>/dev/null || echo "")"
    IF_BODY="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.body' 2>/dev/null || echo "")"

    # Close the failed issue
    ensure_label_exists "ai:closed"
    gh_retry gh issue edit "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
      --remove-label 'ai:implementation-failed' --add-label 'ai:closed' 2>/dev/null || true
    gh_retry gh issue close "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
      -c "Closing: implementation produced no changes. Re-issuing with additional guidance." 2>/dev/null || true

    # Create replacement issue with extra guidance
    NEW_BODY="$(cat <<REISSUE_EOF
${IF_BODY}

---

**⚠️ Re-issued from #${if_issue}** — the previous implementation attempt produced no repository changes.

**Guidance for the implementation model:**
- You MUST create or modify files as described in the plan. Do NOT just describe changes.
- If the plan requires creating files under \`.github/workflows/\`, the consumer repo must have \`ALLOW_WORKFLOW_EDITS=true\` set as a repository variable.
- Verify your changes exist on disk before finishing (e.g. \`ls -la\` the target path).
- If the task genuinely requires no code changes, explain why in a comment instead of silently producing no output.
REISSUE_EOF
)"

    ensure_label_exists "ai:clarification"
    NEW_ISSUE_URL="$(gh issue create --repo "${GITHUB_REPOSITORY}" \
      --title "${IF_TITLE}" \
      --body "${NEW_BODY}" \
      --label "ai:clarification" 2>/dev/null || echo "")"
    if [ -n "${NEW_ISSUE_URL}" ]; then
      NEW_ISSUE_URL_CLEAN="$(printf '%s\n' "${NEW_ISSUE_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
      NEW_ISSUE_NUM="$(basename "${NEW_ISSUE_URL_CLEAN%%[?#]*}")"
      echo "  Created replacement issue #${NEW_ISSUE_NUM} for failed #${if_issue}."

      # Update state file: replace the old issue number with the new one
      # (impl_noop_count is preserved on the issue entry since we only
      # change github_issue, not the issue object itself)
      if [[ "${NEW_ISSUE_NUM}" =~ ^[0-9]+$ ]]; then
        jq --arg if_issue "${if_issue}" --arg new_issue_num "${NEW_ISSUE_NUM}" --arg local_id "${IF_LOCAL_ID}" --argjson wave_idx "${WAVE_IDX}" '(.waves[$wave_idx].issues[] | select((.github_issue | tostring) == $if_issue)).github_issue = $new_issue_num | if ($local_id != "" and $local_id != "null") then .issue_number_map[$local_id] = $new_issue_num else . end' \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      fi

      tg_notify "Re-issued implementation-failed issue #${if_issue} as #${NEW_ISSUE_NUM}: ${IF_TITLE}"$'\n'"Old issue: $(_gh_url "issues/${if_issue}")"$'\n'"New issue: $(_gh_url "issues/${NEW_ISSUE_NUM}")" "WARNING"
      IMPL_FAILED_STATE_CHANGED=true
    else
      echo "::warning::Could not create replacement issue for #${if_issue}."
    fi
  done < <(echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "implementation-failed") | .github_issue')

if [ "${IMPL_FAILED_STATE_CHANGED}" = "true" ]; then
  post_state_comment

  # Add labels for any replacement issues created in this cycle.
  REISSUED_NUMS="$(jq -r '.waves['"${WAVE_IDX}"'].issues[].github_issue' "${STATE_FILE}" 2>/dev/null | sort -u)"
  for rnum in ${REISSUED_NUMS}; do
    if [ -z "${rnum}" ] || [ "${rnum}" = "null" ]; then continue; fi
    if echo "${LABELS_JSON}" | jq -e --arg key "${rnum}" 'has($key)' >/dev/null 2>&1; then continue; fi
    LABELS="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${rnum}/labels" --jq '[.[].name]' 2>/dev/null || echo '[]')"
    [ -z "${LABELS}" ] && LABELS='[]'
    LABELS_JSON="$(echo "${LABELS_JSON}" | jq -c --arg key "${rnum}" --argjson labels "${LABELS}" '. + {($key): $labels}')"
  done
fi

  if [ "${WAVE_COMPLETE}" != "true" ]; then
    echo "Wave ${CURRENT_WAVE} not yet complete."

    # Update individual issue statuses in state
    echo "${WAVE_STATUS}" | jq -r '.issues[] | "\(.id) \(.status)"' | while read -r local_id status; do
      echo "  ${local_id}: ${status}"
    done

    # ---------------------------------------------------------------
    # Stall detection and self-healing
    # ---------------------------------------------------------------
    _stall_check_args=(
      --state-file "${STATE_FILE}"
      --labels-json "${LABELS_JSON}"
      --threshold-minutes "${STALL_THRESHOLD_MINUTES}"
      --max-recoveries "${MAX_STALL_RECOVERIES_PER_ISSUE}"
    )
    if [ -n "${PHASE_THRESHOLDS_JSON:-}" ]; then
      _stall_check_args+=(--phase-thresholds-json "${PHASE_THRESHOLDS_JSON}")
    fi
    STALLS_JSON="$(python3 scripts/orchestrate_lib.py check-stalls \
      "${_stall_check_args[@]}" 2>/dev/null || echo '{"ok":false,"stalls":[],"count":0}')"

    STALL_COUNT="$(echo "${STALLS_JSON}" | jq -r '.count')"

    STALL_STATE_CHANGED=false

    if [ "${STALL_COUNT}" -gt 0 ]; then
      echo "Detected ${STALL_COUNT} stalled issue(s). Checking for active workflow runs..."

      # Build the set of issues with active workflows (one API call batch,
      # reused across all stalled issues to avoid per-issue API calls).
      ACTIVE_WORKFLOW_ISSUES="$(build_active_issue_set)"
      if [ -n "${ACTIVE_WORKFLOW_ISSUES}" ]; then
        echo "Issues with active workflow runs: $(echo "${ACTIVE_WORKFLOW_ISSUES}" | tr '\n' ' ')"
      fi

      while IFS= read -r stall_entry; do
        [ -n "${stall_entry}" ] || continue
        STALL_ISSUE="$(echo "${stall_entry}" | jq -r '.github_issue')"
        STALL_PHASE="$(echo "${stall_entry}" | jq -r '.phase')"
        STALL_ACTION="$(echo "${stall_entry}" | jq -r '.recovery_action')"
        STALL_LOCAL_ID="$(echo "${stall_entry}" | jq -r '.id')"
        STALL_RECOVERY_COUNT="$(echo "${stall_entry}" | jq -r '.stall_recovery_count')"
        STALL_DURATION="$(echo "${stall_entry}" | jq -r '.stall_duration_minutes')"

        if recover_stalled_issue \
          "${STALL_ISSUE}" "${STALL_PHASE}" "${STALL_ACTION}" \
          "${STALL_RECOVERY_COUNT}" "${STALL_LOCAL_ID}" "${STALL_DURATION}"; then

          STALL_STATE_CHANGED=true

          # Increment recovery counter in state (and reset status_since_ts)
          # for recovery actions that keep the same issue in place.
          if [ "${STALL_ACTION}" != "close_and_reissue" ] && [ "${STALL_ACTION}" != "skip" ]; then
            python3 -c "
import json, time, sys
sys.path.insert(0, 'scripts')
from orchestrate_lib import increment_stall_recovery

with open('${STATE_FILE}') as f:
    state = json.load(f)

increment_stall_recovery(state, '${STALL_LOCAL_ID}')

with open('${STATE_FILE}', 'w') as f:
    json.dump(state, f, indent=2)
" || true
          fi
        else
          echo "  [stall-recovery] No action taken for #${STALL_ISSUE} (active workflow or guard)."
        fi
      done < <(echo "${STALLS_JSON}" | jq -c '.stalls[]')

    else
      if [ -n "${PHASE_THRESHOLDS_JSON:-}" ]; then
        echo "No stalled issues detected (fallback: ${STALL_THRESHOLD_MINUTES}m, per-phase overrides active)."
      else
        echo "No stalled issues detected (threshold: ${STALL_THRESHOLD_MINUTES}m)."
      fi
    fi

    if [ "${STALL_STATE_CHANGED}" = "true" ] || [ "${TIMESTAMP_STATE_CHANGED}" = "true" ]; then
      post_state_comment
    fi

    continue
  fi

  echo "Wave ${CURRENT_WAVE} complete!"

  # ---------------------------------------------------------------
  # Guard: cap judge cycles to prevent infinite loops
  # ---------------------------------------------------------------
  MAX_JUDGE="${MAX_JUDGE_CYCLES:-25}"
  if ! [[ "${MAX_JUDGE}" =~ ^[0-9]+$ ]] || [ "${MAX_JUDGE}" -lt 1 ]; then
    MAX_JUDGE="25"
  fi
  if [ "$((JUDGE_CYCLE + 1))" -gt "${MAX_JUDGE}" ]; then
    echo "::error::Judge cycle limit reached ($((JUDGE_CYCLE + 1)) > ${MAX_JUDGE}). Marking project as failed."
    jq '.status = "failed"' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
      -f body="## Project Failed — Judge cycle limit exceeded

Judge has run ${JUDGE_CYCLE} cycle(s) without reaching completion. MAX_JUDGE_CYCLES=${MAX_JUDGE}.
Manual intervention required." >/dev/null
    tg_notify "Project #${TRACKING_NUM} FAILED: judge cycle limit (${MAX_JUDGE}) exceeded." "CRITICAL"
    tg_cleanup_msgs "${TRACKING_NUM}"
    continue
  fi

  # ---------------------------------------------------------------
  # Run judge (full repo checkout + Codex call)
  # ---------------------------------------------------------------
  echo "Running judge evaluation (cycle $((JUDGE_CYCLE + 1)))..."

  # Setup Codex config for judge
  mkdir -p ~/.codex
  JUDGE_INVOCATION_CYCLE=$((JUDGE_CYCLE + 1))
  EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE="${MODEL_REASONING_EFFORT_JUDGE}"
  if [ "${JUDGE_INVOCATION_CYCLE}" -gt 3 ] && [ "${MODEL_REASONING_EFFORT_JUDGE}" = "xhigh" ]; then
    EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE="high"
  fi
  echo "Judge reasoning effort for cycle ${JUDGE_INVOCATION_CYCLE}: ${EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE}"
  CATALOG_PATH="$(pwd)/scripts/codex_model_catalog.json"
  {
    echo 'web_search = "live"'
    echo 'model_provider = "openrouter"'
    echo "model = \"${MODEL_EDITOR}\""
    echo "model_reasoning_effort = \"${EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE}\""
    if [ -f "${CATALOG_PATH}" ]; then
      echo "model_catalog_json = \"${CATALOG_PATH}\""
    fi
    echo
    echo '[model_providers.openrouter]'
    echo 'name = "OpenRouter"'
    echo 'base_url = "https://openrouter.ai/api/v1"'
    echo 'env_key = "OPENROUTER_API_KEY"'
    echo 'wire_api = "responses"'
    echo 'stream_idle_timeout_ms = 600000'
    echo 'stream_max_retries = 5'
    echo 'request_max_retries = 3'
    echo
    echo '[sandbox_workspace_write]'
    echo 'network_access = true'
  } > ~/.codex/config.toml

  # Setup Serena for judge
  bash scripts/setup_serena.sh --mode planning --context codex || true

  # Collect merged PR diffs for context
  PR_SUMMARIES=""
  ISSUE_MAP="$(jq -r '.issue_number_map // {}' "${STATE_FILE}")"
  for inum in ${ISSUE_NUMS}; do
    PR_NUM="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${inum}/timeline" \
      --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
      2>/dev/null || echo "")"
    if [ -n "${PR_NUM}" ] && [ "${PR_NUM}" != "null" ]; then
      PR_DIFF="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUM}" \
        -H 'Accept: application/vnd.github.diff' 2>/dev/null | head -500 || echo "(diff unavailable)")"
      PR_SUMMARIES+="
--- PR #${PR_NUM} (Issue #${inum}) ---
${PR_DIFF}

"
    fi
  done

  # Fetch CI status on default branch
  DEFAULT_BRANCH="$(gh api "repos/${GITHUB_REPOSITORY}" --jq '.default_branch')"
  CI_STATUS="$(gh api "repos/${GITHUB_REPOSITORY}/commits/${DEFAULT_BRANCH}/check-runs" \
    --jq '[.check_runs[] | {name: .name, conclusion: .conclusion}]' 2>/dev/null || echo "[]")"

  # Get original project description from tracking issue body
  PROJECT_BODY="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}" --jq '.body')"

  # Assemble static context
  {
    echo "=== SYSTEM INSTRUCTIONS ==="
    cat codex_system_instructions.md
    echo
    echo "=== AI PIPELINE ==="
    cat ai_pipeline.md
    echo
    if [ -f agents.md ]; then
      echo "=== AGENTS.MD ==="
      cat agents.md
      echo
    fi
    if [ -f README.md ]; then
      echo "=== README.MD ==="
      cat README.md
      echo
    fi
  } > "${RUNTIME_DIR}/judge_static.txt"

  # Build judge prompt
  {
    cat "${RUNTIME_DIR}/judge_static.txt"
    echo
    echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE}"
    echo
    echo "=== JUDGE TASK ==="
    echo
    cat prompts/mode-judge.txt
    echo
    echo "=== PROJECT SPEC ==="
    echo
    echo "${PROJECT_BODY}"
    echo
    echo "=== WAVE ${CURRENT_WAVE} COMPLETION STATUS ==="
    echo
    echo "${WAVE_STATUS}" | jq '.'
    echo
    echo "=== MERGED PR DIFFS (truncated) ==="
    echo
    echo "${PR_SUMMARIES}"
    echo
    echo "=== CI STATUS ON ${DEFAULT_BRANCH} ==="
    echo
    echo "${CI_STATUS}" | jq '.'
    echo
    echo "=== ORCHESTRATOR STATE ==="
    echo
    echo "Judge cycle: $((JUDGE_CYCLE + 1))"
    echo "Current wave just completed: ${CURRENT_WAVE} of ${TOTAL_WAVES}"
    echo "Project complete (all waves dispatched and merged): ${PROJECT_COMPLETE}"
    echo "Recovery count: ${RECOVERY_COUNT}/${MAX_RECOVERY_ATTEMPTS}"
    PENDING_DEFS_COUNT="$(jq '.pending_issue_defs | length' "${STATE_FILE}")"
    echo "Pending issue definitions (not yet created): ${PENDING_DEFS_COUNT}"
    if [ "${PENDING_DEFS_COUNT}" -gt 0 ]; then
      echo
      echo "=== REMAINING WAVES (issues not yet created) ==="
      jq -r '.pending_issue_defs | to_entries[] | "- \(.key): \(.value.title)"' "${STATE_FILE}"
    fi
    if [ "${ANY_FAILED}" = "true" ]; then
      echo "WARNING: Some issues in this wave were closed without merge."
    fi
    echo
    echo "IMPORTANT: If current wave < total waves, the project is NOT complete."
    echo "Return in_progress to advance to the next wave."
  } > "${JUDGE_PROMPT_FILE}"

  # Run judge via Codex
  JUDGE_SUCCESS=false
  max_attempts=2
  for attempt in $(seq 1 "${max_attempts}"); do
    echo "Judge attempt ${attempt}/${max_attempts}..."
    if cat "${JUDGE_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${JUDGE_OUTPUT_FILE}" 2> >(tee -a "${RUNTIME_DIR}/judge_log.txt" >&2); then
      if grep -q '[^[:space:]]' "${JUDGE_OUTPUT_FILE}"; then
        JUDGE_SUCCESS=true
        break
      fi
    fi
    if [ "${attempt}" -lt "${max_attempts}" ]; then
      sleep $(( 10 * attempt + RANDOM % 10 ))
    fi
  done

  if [ "${JUDGE_SUCCESS}" != "true" ]; then
    echo "::error::Judge failed for tracking issue #${TRACKING_NUM}"
    tg_notify "Orchestrator Judge failed for #${TRACKING_NUM}. Manual review needed." "CRITICAL"
    continue
  fi

  # ---------------------------------------------------------------
  # Parse judge output
  # ---------------------------------------------------------------
  JUDGE_JSON="$(python3 -c "
import json, re, sys

raw = open('${JUDGE_OUTPUT_FILE}', 'r').read()

try:
    data = json.loads(raw.strip())
    json.dump(data, sys.stdout)
    sys.exit(0)
except json.JSONDecodeError:
    pass

cleaned = re.sub(r'\`\`\`(?:json)?\s*', '', raw)
cleaned = re.sub(r'\`\`\`\s*$', '', cleaned, flags=re.MULTILINE)

brace_depth = 0
start = None
for i, ch in enumerate(cleaned):
    if ch == '{':
        if brace_depth == 0:
            start = i
        brace_depth += 1
    elif ch == '}':
        brace_depth -= 1
        if brace_depth == 0 and start is not None:
            candidate = cleaned[start:i+1]
            try:
                data = json.loads(candidate)
                json.dump(data, sys.stdout)
                sys.exit(0)
            except json.JSONDecodeError:
                start = None

print('Could not parse judge JSON', file=sys.stderr)
sys.exit(1)
" 2>/dev/null || echo "")"

  if [ -z "${JUDGE_JSON}" ]; then
    echo "::error::Could not parse judge output for #${TRACKING_NUM}"
    tg_notify "Orchestrator Judge output unparseable for #${TRACKING_NUM}. Manual review needed." "CRITICAL"
    continue
  fi

  JUDGE_STATUS="$(echo "${JUDGE_JSON}" | jq -r '.status')"
  JUDGE_JUSTIFICATION="$(echo "${JUDGE_JSON}" | jq -r '.justification // "no justification provided"')"
  JUDGE_ASSESSMENT="$(echo "${JUDGE_JSON}" | jq -r '.assessment // ""')"
  NEW_ISSUES_COUNT="$(echo "${JUDGE_JSON}" | jq '.new_issues | length')"
  REVERT_COUNT="$(echo "${JUDGE_JSON}" | jq '.issues_to_revert | length')"

  echo "Judge verdict: ${JUDGE_STATUS}"
  echo "Justification: ${JUDGE_JUSTIFICATION}"
  echo "New issues: ${NEW_ISSUES_COUNT}, Reverts: ${REVERT_COUNT}"

  # Notify operator after every judge evaluation
  tg_notify "Judge evaluated #${TRACKING_NUM} (cycle $((JUDGE_CYCLE + 1))): ${JUDGE_STATUS}. ${JUDGE_JUSTIFICATION}" "DEBUG"

  # Post judge assessment to tracking issue
  JUDGE_COMMENT="## Judge Evaluation — Cycle $((JUDGE_CYCLE + 1))

**Status:** ${JUDGE_STATUS}
**Justification:** ${JUDGE_JUSTIFICATION}

${JUDGE_ASSESSMENT}

New fix-up issues: ${NEW_ISSUES_COUNT}
PRs to revert: ${REVERT_COUNT}"

  gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
    -f body="${JUDGE_COMMENT}" >/dev/null

  # ---------------------------------------------------------------
  # Hard guard: judge cannot declare "complete" while waves remain
  # ---------------------------------------------------------------
  if [ "${JUDGE_STATUS}" = "complete" ] && [ "${PROJECT_COMPLETE}" != "true" ]; then
    echo "::warning::Judge returned 'complete' but project_complete=${PROJECT_COMPLETE} (wave ${CURRENT_WAVE}/${TOTAL_WAVES}). Overriding to 'in_progress'."
    JUDGE_STATUS="in_progress"
    gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
      -f body="⚠️ Judge verdict overridden: \`complete\` → \`in_progress\` because wave ${CURRENT_WAVE}/${TOTAL_WAVES} is not the final wave. Advancing to next wave." >/dev/null
  fi

  # ---------------------------------------------------------------
  # Handle judge verdict
  # ---------------------------------------------------------------
  case "${JUDGE_STATUS}" in
    complete)
      if [ "${ENABLE_VALIDATION}" != "true" ]; then
        FINAL_INTEGRATION_BRANCH="$(jq -r '.integration_branch // ""' "${STATE_FILE}")"
        FINAL_DEFAULT_BRANCH="$(gh api "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo "main")"
        FINAL_PROJECT_TITLE="$(jq -r '.project_title // "Orchestrator project"' "${STATE_FILE}")"

        if ! finalize_integration_merge_if_needed "${FINAL_INTEGRATION_BRANCH}" "${FINAL_DEFAULT_BRANCH}" "${FINAL_PROJECT_TITLE}"; then
          continue
        fi

        echo "Project complete!"
        jq '.status = "complete" | .judge_cycle += 1' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        post_state_comment

        set_tracking_phase_label "ai:validated"
        post_tracking_comment "Project completed successfully after $((JUDGE_CYCLE + 1)) judge cycle(s). Issue kept open for manual review."

        tg_cleanup_msgs "${TRACKING_NUM}"
        MSG="Project #${TRACKING_NUM} completed! All waves merged and judge approved."
        MSG+=$'\n'"Tracking: $(_gh_url "issues/${TRACKING_NUM}")"
        if [ -n "${GITHUB_RUN_ID:-}" ]; then
          MSG+=$'\n'"Run: $(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
        fi
        tg_send_msg "${MSG}" "DEBUG" >/dev/null
        continue
      fi

      VALIDATION_CYCLE="$(jq -r '.validation_cycle // 1' "${STATE_FILE}")"
      if ! [[ "${VALIDATION_CYCLE}" =~ ^[0-9]+$ ]] || [ "${VALIDATION_CYCLE}" -lt 1 ]; then
        VALIDATION_CYCLE="1"
      fi

      if [ "${VALIDATION_CYCLE}" -gt "${MAX_VALIDATE_CYCLES}" ]; then
        mark_validation_failed "Validation cycle ${VALIDATION_CYCLE} exceeds MAX_VALIDATE_CYCLES=${MAX_VALIDATE_CYCLES}."
        continue
      fi

      jq --argjson cycle "${VALIDATION_CYCLE}" \
        '.status = "validating" |
         .judge_cycle += 1 |
         .validation_cycle = $cycle |
         .validation_active_fix_issues = [] |
         .validation_seen_fix_issues = (.validation_seen_fix_issues // []) |
         .validation_last_fix_comment_id = (.validation_last_fix_comment_id // 0) |
         .validation_last_dispatch_cycle = 0 |
         .validation_failure_reason = null |
         .validation_completed_cycle = null' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      post_state_comment
      set_tracking_phase_label "ai:validating"

      if ! dispatch_validation_if_needed "${VALIDATION_CYCLE}"; then
        mark_validation_failed "Unable to dispatch ${VALIDATE_WORKFLOW_NAME:-ai-validate.yml} for cycle ${VALIDATION_CYCLE}. Ensure consumer wrapper workflow exists and GH token has actions:write."
      fi
      ;;

    failed)
      echo "Judge declared failure."

      # ---------------------------------------------------------------
      # Auto-recovery: configurable attempts (replaces single-shot boolean)
      # ---------------------------------------------------------------
      if [ "${RECOVERY_COUNT}" -ge "${MAX_RECOVERY_ATTEMPTS}" ]; then
        echo "Recovery attempts exhausted (${RECOVERY_COUNT}/${MAX_RECOVERY_ATTEMPTS}). Stopping."
        jq '.status = "failed" | .judge_cycle += 1' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

        post_state_comment

        gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
          -f body="## Project Failed

Recovery was attempted ${RECOVERY_COUNT} time(s) (max ${MAX_RECOVERY_ATTEMPTS}) but the judge still reports failure. Manual intervention required.

**Assessment:** ${JUDGE_ASSESSMENT}" >/dev/null

        tg_notify "Project #${TRACKING_NUM} FAILED after ${RECOVERY_COUNT} recovery attempt(s). Manual intervention needed." "CRITICAL"
        tg_cleanup_msgs "${TRACKING_NUM}"
        continue
      fi

      echo "Attempting auto-recovery ($((RECOVERY_COUNT + 1))/${MAX_RECOVERY_ATTEMPTS})..."

      # Revert problematic PRs if judge requested
      if [ "${REVERT_COUNT}" -gt 0 ]; then
        echo "Reverting ${REVERT_COUNT} PR(s)..."
        echo "${JUDGE_JSON}" | jq -r '.issues_to_revert[]' | while read -r revert_issue; do
          # Find PR linked to this issue
          PR_TO_REVERT="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${revert_issue}/timeline" \
            --jq '[.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number] | last' \
            2>/dev/null || echo "")"
          if [ -n "${PR_TO_REVERT}" ] && [ "${PR_TO_REVERT}" != "null" ]; then
            echo "  Reverting PR #${PR_TO_REVERT} (issue #${revert_issue})..."
            # Create revert PR via gh
            gh api "repos/${GITHUB_REPOSITORY}/pulls" \
              -f title="Revert PR #${PR_TO_REVERT} (orchestrator auto-recovery)" \
              -f head="revert-${PR_TO_REVERT}-$(date +%s)" \
              -f base="${DEFAULT_BRANCH}" \
              -f body="Automated revert of PR #${PR_TO_REVERT} by orchestrator judge.

**Reason:** ${JUDGE_JUSTIFICATION}" >/dev/null 2>&1 || {
              # If API-based revert fails, create a revert via git
              echo "  API revert failed; creating revert commit..."
              MERGE_SHA="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_TO_REVERT}" --jq '.merge_commit_sha' 2>/dev/null || echo "")"
              if [ -n "${MERGE_SHA}" ] && [ "${MERGE_SHA}" != "null" ]; then
                REVERT_BRANCH="revert-${PR_TO_REVERT}-$(date +%s)"
                git checkout -b "${REVERT_BRANCH}" "${DEFAULT_BRANCH}"
                if git revert --no-edit "${MERGE_SHA}"; then
                  git push -u origin "${REVERT_BRANCH}"
                  gh pr create \
                    --repo "${GITHUB_REPOSITORY}" \
                    --title "Revert PR #${PR_TO_REVERT} (orchestrator auto-recovery)" \
                    --body "Automated revert of PR #${PR_TO_REVERT} by orchestrator judge.

**Reason:** ${JUDGE_JUSTIFICATION}" \
                    --base "${DEFAULT_BRANCH}" \
                    --head "${REVERT_BRANCH}"
                else
                  echo "::warning::Git revert of ${MERGE_SHA} failed (conflicts). Manual revert needed."
                fi
                git checkout "${DEFAULT_BRANCH}" 2>/dev/null || true
              fi
            }
          fi
        done
      fi

      # Create fix-up issues from judge
      if [ "${NEW_ISSUES_COUNT}" -gt 0 ]; then
        echo "Creating ${NEW_ISSUES_COUNT} fix-up issue(s)..."
        echo "${JUDGE_JSON}" | jq -c '.new_issues[]' | while read -r fix_issue; do
          FIX_TITLE="$(echo "${fix_issue}" | jq -r '.title')"
          FIX_BODY="$(echo "${fix_issue}" | jq -r '.body' | sed 's/\\n/\n/g')"
          FIX_ID="$(echo "${fix_issue}" | jq -r '.id')"

          # --- Dedup guard: skip if this local ID already has a GitHub issue ---
          if [ -n "${FIX_ID}" ] && [ "${FIX_ID}" != "null" ]; then
            EXISTING_NUM="$(jq -r --arg fix_id "${FIX_ID}" '.issue_number_map[$fix_id] // empty' "${STATE_FILE}")"
            if [ -n "${EXISTING_NUM}" ]; then
              EXISTING_LABELS="$(get_issue_labels_json "${EXISTING_NUM}")"
              if ! has_label "${EXISTING_LABELS}" "ai:merged" && ! has_label "${EXISTING_LABELS}" "ai:closed"; then
                jq --arg fix_id "${FIX_ID}" --argjson existing_num "${EXISTING_NUM}" --argjson wave_idx "${WAVE_IDX}" \
                  '.issue_number_map[$fix_id] = $existing_num | .waves[$wave_idx].issues |= map(select(.id != $fix_id)) | .waves[$wave_idx].issues += [{"id": $fix_id, "github_issue": $existing_num, "status": "pending"}]' \
                  "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
                echo "  ${FIX_ID}: already exists as #${EXISTING_NUM} and is still open, skipping duplicate fix-up."
                continue
              fi
              echo "  ${FIX_ID}: prior issue #${EXISTING_NUM} is already merged/closed, allowing new fix-up."
            fi
          fi

          FULL_FIX_BODY="${FIX_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Integration branch: $(jq -r '.integration_branch // ""' "${STATE_FILE}")
- Local ID: \`${FIX_ID}\`
- Type: judge-fix-up (cycle $((JUDGE_CYCLE + 1)))
- Managed by: AI Orchestrator"

          ensure_label_exists "ai:clarification"
          FIX_URL="$(gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${FIX_TITLE}" \
            --body "${FULL_FIX_BODY}" \
            --label "ai:clarification")"
          echo "  Created fix-up: ${FIX_URL}"

          # Record in state so subsequent cycles/iterations won't recreate,
          # and add to the current wave so the poller tracks merge progress.
          FIX_URL_CLEAN="$(printf '%s\n' "${FIX_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
          FIX_NEW_NUM="$(basename "${FIX_URL_CLEAN%%[?#]*}")"
          if [[ "${FIX_NEW_NUM}" =~ ^[0-9]+$ ]] && [ -n "${FIX_ID}" ] && [ "${FIX_ID}" != "null" ]; then
            jq --arg fix_id "${FIX_ID}" --argjson fix_new_num "${FIX_NEW_NUM}" --argjson wave_idx "${WAVE_IDX}" \
              '.issue_number_map[$fix_id] = $fix_new_num | .waves[$wave_idx].issues |= map(select(.id != $fix_id)) | .waves[$wave_idx].issues += [{"id": $fix_id, "github_issue": $fix_new_num, "status": "pending"}]' \
              "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          fi
        done
      fi

      # Update state — increment recovery_count (replaces old recovery_attempted boolean)
      jq '.judge_cycle += 1 | .recovery_count = ((.recovery_count // (if .recovery_attempted == true then 1 else 0 end)) + 1) | .status = "in_progress"' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

      post_state_comment

      tg_notify "Orchestrator auto-recovery ($((RECOVERY_COUNT + 1))/${MAX_RECOVERY_ATTEMPTS}) started for #${TRACKING_NUM}: ${NEW_ISSUES_COUNT} fix-up issues, ${REVERT_COUNT} reverts." "WARNING"
      ;;

    in_progress)
      echo "Project in progress."

      # Create new issues if judge found gaps
      WAVE_ISSUE_COUNT_BEFORE="$(jq --argjson widx "${WAVE_IDX}" '.waves[$widx].issues | length' "${STATE_FILE}")"
      WAVE_ISSUE_TRACKING_BEFORE="$(jq -c --argjson widx "${WAVE_IDX}" '.waves[$widx].issues | map({id, github_issue, status}) | sort_by(.id)' "${STATE_FILE}")"
      if [ "${NEW_ISSUES_COUNT}" -gt 0 ]; then
        echo "Creating ${NEW_ISSUES_COUNT} new issue(s) from judge..."
        echo "${JUDGE_JSON}" | jq -c '.new_issues[]' | while read -r new_issue; do
          NEW_TITLE="$(echo "${new_issue}" | jq -r '.title')"
          NEW_BODY="$(echo "${new_issue}" | jq -r '.body' | sed 's/\\n/\n/g')"
          NEW_ID="$(echo "${new_issue}" | jq -r '.id')"

          # --- Dedup guard: skip if this local ID already has a GitHub issue ---
          if [ -n "${NEW_ID}" ] && [ "${NEW_ID}" != "null" ]; then
            EXISTING_NUM="$(jq -r --arg new_id "${NEW_ID}" '.issue_number_map[$new_id] // empty' "${STATE_FILE}")"
            if [ -n "${EXISTING_NUM}" ]; then
              EXISTING_LABELS="$(get_issue_labels_json "${EXISTING_NUM}")"
              if ! has_label "${EXISTING_LABELS}" "ai:merged" && ! has_label "${EXISTING_LABELS}" "ai:closed"; then
                jq --arg new_id "${NEW_ID}" --argjson existing_num "${EXISTING_NUM}" --argjson wave_idx "${WAVE_IDX}" \
                  '.issue_number_map[$new_id] = $existing_num | .waves[$wave_idx].issues |= map(select(.id != $new_id)) | .waves[$wave_idx].issues += [{"id": $new_id, "github_issue": $existing_num, "status": "pending"}]' \
                  "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
                echo "  ${NEW_ID}: already exists as #${EXISTING_NUM} and is still open, skipping duplicate addition."
                continue
              fi
              echo "  ${NEW_ID}: prior issue #${EXISTING_NUM} is already merged/closed, allowing new addition."
            fi
          fi

          FULL_NEW_BODY="${NEW_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Integration branch: $(jq -r '.integration_branch // ""' "${STATE_FILE}")
- Local ID: \`${NEW_ID}\`
- Type: judge-addition (cycle $((JUDGE_CYCLE + 1)))
- Managed by: AI Orchestrator"

          ensure_label_exists "ai:clarification"
          NEW_URL="$(gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${NEW_TITLE}" \
            --body "${FULL_NEW_BODY}" \
            --label "ai:clarification")"
          echo "  Created: ${NEW_URL}"

          # Record in state so subsequent cycles/iterations won't recreate,
          # and add to the current wave so the poller tracks merge progress.
          NEW_URL_CLEAN="$(printf '%s\n' "${NEW_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
          ADD_NEW_NUM="$(basename "${NEW_URL_CLEAN%%[?#]*}")"
          if [[ "${ADD_NEW_NUM}" =~ ^[0-9]+$ ]] && [ -n "${NEW_ID}" ] && [ "${NEW_ID}" != "null" ]; then
            jq --arg new_id "${NEW_ID}" --argjson add_new_num "${ADD_NEW_NUM}" --argjson wave_idx "${WAVE_IDX}" \
              '.issue_number_map[$new_id] = $add_new_num | .waves[$wave_idx].issues |= map(select(.id != $new_id)) | .waves[$wave_idx].issues += [{"id": $new_id, "github_issue": $add_new_num, "status": "pending"}]' \
              "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          fi
        done
      fi

      # ---------------------------------------------------------------
      # Guard: do not advance if fix-up/new issues were added to the
      # current wave during this judge cycle. Those issues are still
      # pending and the poller only monitors current_wave, so advancing
      # would orphan them.
      # ---------------------------------------------------------------
      WAVE_ISSUE_COUNT_AFTER="$(jq --argjson widx "${WAVE_IDX}" '.waves[$widx].issues | length' "${STATE_FILE}")"
      WAVE_ISSUE_TRACKING_AFTER="$(jq -c --argjson widx "${WAVE_IDX}" '.waves[$widx].issues | map({id, github_issue, status}) | sort_by(.id)' "${STATE_FILE}")"
      if [ "${WAVE_ISSUE_COUNT_AFTER}" -gt "${WAVE_ISSUE_COUNT_BEFORE}" ] || [ "${WAVE_ISSUE_TRACKING_AFTER}" != "${WAVE_ISSUE_TRACKING_BEFORE}" ]; then
        if [ "${WAVE_ISSUE_COUNT_AFTER}" -gt "${WAVE_ISSUE_COUNT_BEFORE}" ]; then
          ADDED_COUNT=$(( WAVE_ISSUE_COUNT_AFTER - WAVE_ISSUE_COUNT_BEFORE ))
          echo "Current wave gained ${ADDED_COUNT} new issue(s) from judge. Staying on wave ${CURRENT_WAVE} until they complete."
        else
          echo "Current wave issue tracking changed from judge output. Staying on wave ${CURRENT_WAVE} until updated issues complete."
        fi
        jq '.judge_cycle += 1' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        post_state_comment
        # Skip wave advancement — next poll cycle will re-check this wave
      else

      # Advance to next wave
      NEXT_WAVE=$(( CURRENT_WAVE + 1 ))
      if [ "${NEXT_WAVE}" -le "${TOTAL_WAVES}" ]; then
        echo "Advancing to wave ${NEXT_WAVE}..."
        NEXT_WAVE_IDX=$(( NEXT_WAVE - 1 ))

        # -----------------------------------------------------------
        # Deferred issue creation: create issues for this wave now.
        # This triggers clarify.yml via the issues.opened event.
        # -----------------------------------------------------------
        CREATED_NUMS=""
        NEXT_WAVE_ISSUE_IDS="$(jq -r ".waves[${NEXT_WAVE_IDX}].issues[].id" "${STATE_FILE}")"
        for local_id in ${NEXT_WAVE_ISSUE_IDS}; do
          # Check if already created (has a github_issue number)
          EXISTING_NUM="$(jq -r ".issue_number_map[\"${local_id}\"] // empty" "${STATE_FILE}")"
          if [ -n "${EXISTING_NUM}" ]; then
            echo "  ${local_id}: already exists as #${EXISTING_NUM}"
            CREATED_NUMS="${CREATED_NUMS} ${EXISTING_NUM}"
            continue
          fi

          # Get issue definition from pending_issue_defs in state
          ISSUE_DEF="$(jq -c ".pending_issue_defs[\"${local_id}\"] // empty" "${STATE_FILE}")"
          if [ -z "${ISSUE_DEF}" ]; then
            echo "::warning::No pending definition for ${local_id}, skipping."
            continue
          fi

          DEF_TITLE="$(echo "${ISSUE_DEF}" | jq -r '.title')"
          DEF_BODY="$(echo "${ISSUE_DEF}" | jq -r '.body' | sed 's/\\n/\n/g')"
          DEF_PRIORITY="$(echo "${ISSUE_DEF}" | jq -r '.priority')"

          FULL_BODY="${DEF_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Integration branch: $(jq -r '.integration_branch // ""' "${STATE_FILE}")
- Local ID: \`${local_id}\`
- Priority: ${DEF_PRIORITY}
- Managed by: AI Orchestrator"

          ensure_label_exists "ai:clarification"
          NEW_URL="$(gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${DEF_TITLE}" \
            --body "${FULL_BODY}" \
            --label "ai:clarification")"

          NEW_URL_CLEAN="$(printf '%s\n' "${NEW_URL}" | grep -oE 'https://[^ ]+' | tail -n1 || true)"
          NEW_NUM="$(basename "${NEW_URL_CLEAN%%[?#]*}")"
          if ! [[ "${NEW_NUM}" =~ ^[0-9]+$ ]]; then
            echo "::warning::Could not parse numeric issue number for pending issue ${local_id}; skipping state update."
            continue
          fi
          echo "  Created #${NEW_NUM}: ${DEF_TITLE} (${local_id})"
          CREATED_NUMS="${CREATED_NUMS} ${NEW_NUM}"

          # Update state: record the new issue number and remove from pending
          jq ".issue_number_map[\"${local_id}\"] = ${NEW_NUM} | del(.pending_issue_defs[\"${local_id}\"])" \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

          # Update the wave entry with the github issue number
          jq "(.waves[${NEXT_WAVE_IDX}].issues[] | select(.id == \"${local_id}\")) |= (.github_issue = ${NEW_NUM} | .status = \"pending\")" \
            "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        done

        jq ".current_wave = ${NEXT_WAVE} | .judge_cycle += 1" \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

        WAVE_COMMENT="## Wave ${NEXT_WAVE} Dispatched

Dependencies from Wave ${CURRENT_WAVE} are met. Created and dispatched:

$(for inum in ${CREATED_NUMS}; do echo "- #${inum}"; done)

These issues will enter the AI pipeline (clarify → plan → implement → review)."

        gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
          -f body="${WAVE_COMMENT}" >/dev/null
      else
        # All waves dispatched but judge says in_progress with new issues
        jq '.judge_cycle += 1' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      fi

      # Post updated state
      post_state_comment

      fi  # end: PENDING_IN_WAVE guard
      ;;

    *)
      echo "::warning::Unknown judge status: ${JUDGE_STATUS}"
      ;;
  esac
done

# ---------------------------------------------------------------
# Standalone PR conflict sweep
# ---------------------------------------------------------------
# The tracking-issue loop above only handles PRs linked to
# orchestrator-managed issues.  Standalone AI PRs (created by the
# pipeline without an orchestrator tracking issue) can also develop
# merge conflicts when the base branch advances.  The review
# workflow already has Codex-based conflict resolution, but it only
# runs on PR synchronize events — no event fires when the *base*
# branch moves forward.
#
# This section scans all open PRs on AI-generated branches
# (ai/issue-*) that are not mergeable and resolves conflicts
# directly using a dedicated Codex instance, then pushes so the
# review workflow can trigger on a clean merge ref.
# ---------------------------------------------------------------
echo ""
echo "========================================"
echo "Standalone PR conflict sweep"
echo "========================================"

# Collect all open PRs on ai/* branches with their mergeable status.
# gh pr list does not expose mergeable, so we fetch the full list and
# then query each candidate via the REST API.
STANDALONE_PRS="$(gh pr list \
	--repo "${GITHUB_REPOSITORY}" \
	--state open \
	--json number,headRefName,baseRefName \
	--limit 100 2>/dev/null || echo "[]")"

STANDALONE_COUNT="$(echo "${STANDALONE_PRS}" | jq 'length')"
echo "Found ${STANDALONE_COUNT} open PR(s) to scan."

CONFLICT_SWEEP_FIXED=0
DEFAULT_BRANCH="$(gh api "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo "main")"

for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do
	S_PR="$(echo "${STANDALONE_PRS}" | jq -r ".[${sidx}].number")"
	S_HEAD="$(echo "${STANDALONE_PRS}" | jq -r ".[${sidx}].headRefName")"
	S_BASE="$(echo "${STANDALONE_PRS}" | jq -r ".[${sidx}].baseRefName")"
	if [ -z "${S_PR}" ] || [ -z "${S_HEAD}" ] || [ "${S_PR}" = "null" ] || [ "${S_HEAD}" = "null" ]; then
		continue
	fi

	if [[ "${S_BASE}" == orchestrator/project-* ]]; then
		continue
	fi

	# Only process AI-generated branches (ai/issue-*)
	if [[ "${S_HEAD}" != ai/issue-* ]]; then
		continue
	fi

	# Check mergeable state via REST API (dirty == merge conflicts)
	S_PR_JSON="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${S_PR}" 2>/dev/null || echo '{}')"
	S_MERGEABLE_STATE="$(echo "${S_PR_JSON}" | jq -r '.mergeable_state // ""')"
	if [ -z "${S_MERGEABLE_STATE}" ] || [ "${S_MERGEABLE_STATE}" = "unknown" ]; then
		continue
	fi

	if [ "${S_MERGEABLE_STATE}" != "dirty" ]; then
		continue
	fi

	echo "  PR #${S_PR} (${S_HEAD}) is in conflicted mergeable state. Running Codex conflict resolution..."

	# Stage 1: Try the GitHub API update-branch endpoint (clean merge)
	S_HEAD_SHA="$(echo "${S_PR_JSON}" | jq -r '.head.sha // ""')"
	if [ -n "${S_HEAD_SHA}" ] && gh api "repos/${GITHUB_REPOSITORY}/pulls/${S_PR}/update-branch" \
		-X PUT -f expected_head_sha="${S_HEAD_SHA}" 2>/dev/null; then
		echo "  PR #${S_PR} branch updated via API. Synchronize event will re-trigger review."
		CONFLICT_SWEEP_FIXED=$(( CONFLICT_SWEEP_FIXED + 1 ))
		continue
	fi

	# Stage 2: Dispatch review workflow for conflict resolution.
	_dispatch_rc=0
	_dispatch_review_for_conflicts "${S_PR}" "${S_HEAD}" || _dispatch_rc=$?
	if [ "${_dispatch_rc}" -eq 0 ]; then
		CONFLICT_SWEEP_FIXED=$(( CONFLICT_SWEEP_FIXED + 1 ))
		tg_send_msg "Standalone PR #${S_PR} has merge conflicts. Review workflow dispatched for resolution."$'\n'"PR: $(_gh_url "pull/${S_PR}")" "WARNING" >/dev/null 2>&1 || true
	elif [ "${_dispatch_rc}" -eq 2 ]; then
		echo "  PR #${S_PR}: autofix already in progress, skipping dispatch."
	else
		tg_send_msg "Standalone PR #${S_PR} has merge conflicts. Could not dispatch review workflow."$'\n'"PR: $(_gh_url "pull/${S_PR}")" "WARNING" >/dev/null 2>&1 || true
	fi
done

echo "Standalone conflict sweep complete. Fixed: ${CONFLICT_SWEEP_FIXED}."

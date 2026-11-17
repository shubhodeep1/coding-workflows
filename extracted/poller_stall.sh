set -euo pipefail

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

normalize_stall_guard_thresholds() {
  CODEX_STALL_GUARD_ENABLED="${CODEX_STALL_GUARD_ENABLED:-false}"
  if is_truthy "${CODEX_STALL_GUARD_ENABLED}"; then
    CODEX_STALL_GUARD_ENABLED="true"
  else
    CODEX_STALL_GUARD_ENABLED="false"
  fi

  if [ -z "${STALL_THRESHOLD_IMPLEMENTING_MINUTES:-}" ] && [ "${CODEX_STALL_GUARD_ENABLED}" = "true" ]; then
    STALL_THRESHOLD_IMPLEMENTING_MINUTES="90"
  fi
}

workflow_run_cache_load() {
  local run_json="$1"
  local parsed workflow_name workflow_path head_branch run_id start_epoch

  if [ "${WORKFLOW_RUN_CACHE_KEY:-}" = "${run_json}" ]; then
    return 0
  fi

  parsed="$(printf '%s' "${run_json}" | jq -r '
    def parse_epoch(value):
      ((value // empty | tostring | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601?) // empty);
    [
      (.name // ""),
      (.path // ""),
      (.head_branch // ""),
      ((.id // "") | tostring),
      ((parse_epoch(.run_started_at) // parse_epoch(.created_at) // "") | tostring)
    ] | @tsv
  ' 2>/dev/null || true)"
  if [ -z "${parsed}" ]; then
    WORKFLOW_RUN_CACHE_KEY=""
    WORKFLOW_RUN_CACHE_NAME=""
    WORKFLOW_RUN_CACHE_PATH=""
    WORKFLOW_RUN_CACHE_HEAD_BRANCH=""
    WORKFLOW_RUN_CACHE_ID=""
    WORKFLOW_RUN_CACHE_START_EPOCH=""
    return 1
  fi

  IFS=$'\t' read -r workflow_name workflow_path head_branch run_id start_epoch <<< "${parsed}"
  WORKFLOW_RUN_CACHE_KEY="${run_json}"
  WORKFLOW_RUN_CACHE_NAME="${workflow_name}"
  WORKFLOW_RUN_CACHE_PATH="${workflow_path}"
  WORKFLOW_RUN_CACHE_HEAD_BRANCH="${head_branch}"
  WORKFLOW_RUN_CACHE_ID="${run_id}"
  WORKFLOW_RUN_CACHE_START_EPOCH="${start_epoch}"
  return 0
}

workflow_run_is_implement() {
  local run_json="$1"
  local workflow_name workflow_path

  workflow_run_cache_load "${run_json}" || return 1
  workflow_name="${WORKFLOW_RUN_CACHE_NAME:-}"
  workflow_path="${WORKFLOW_RUN_CACHE_PATH:-}"

  case "${workflow_path}" in
    */implement.yml|*/internal-implement.yml|implement.yml|internal-implement.yml)
      return 0
      ;;
  esac

  case "${workflow_name}" in
    *AI\ Implement*|*Internal\ Implement*)
      return 0
      ;;
  esac

  return 1
}

workflow_run_is_review_family() {
  local run_json="$1"
  local workflow_name workflow_path

  workflow_run_cache_load "${run_json}" || return 1
  workflow_name="${WORKFLOW_RUN_CACHE_NAME:-}"
  workflow_path="${WORKFLOW_RUN_CACHE_PATH:-}"

  case "${workflow_path}" in
    */ai-review.yml|*/internal-review.yml|*/review_autofix.yml|ai-review.yml|internal-review.yml|review_autofix.yml)
      return 0
      ;;
  esac

  case "${workflow_name}" in
    AI\ Review|Internal\ Review|Review\ Autofix)
      return 0
      ;;
  esac

  return 1
}

workflow_run_stall_threshold_seconds() {
  local run_json="$1"
  local stall_minutes="${STALL_THRESHOLD_MINUTES:-120}"

  workflow_run_cache_load "${run_json}" || {
    printf '%s' "$(( stall_minutes * 60 ))"
    return 0
  }

  if workflow_run_is_review_family "${run_json}"; then
    stall_minutes="${REVIEW_RUN_MAX_RUNTIME_MINUTES:-${stall_minutes}}"
  elif [ -n "${STALL_THRESHOLD_IMPLEMENTING_MINUTES:-}" ] && workflow_run_is_implement "${run_json}"; then
    stall_minutes="${STALL_THRESHOLD_IMPLEMENTING_MINUTES}"
  fi

  printf '%s' "$(( stall_minutes * 60 ))"
}

workflow_run_is_fresh() {
  local run_json="$1"
  local now_epoch="$2"
  local start_epoch threshold_secs

  workflow_run_cache_load "${run_json}" || return 1
  start_epoch="${WORKFLOW_RUN_CACHE_START_EPOCH:-}"
  [[ "${start_epoch}" =~ ^[0-9]+$ ]] || return 1

  threshold_secs="$(workflow_run_stall_threshold_seconds "${run_json}")"
  [[ "${threshold_secs}" =~ ^[0-9]+$ ]] || threshold_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))

  [ $(( now_epoch - start_epoch )) -lt "${threshold_secs}" ]
}

build_active_issue_set() {
  local now_epoch
  now_epoch="$(date +%s)"
  local stall_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))
  local review_stall_secs=$(( REVIEW_RUN_MAX_RUNTIME_MINUTES * 60 ))

  # Fetch active runs from the shared per-tick actions-runs loader.
  # This preserves one conditional API retrieval per tick.
  local actions_runs_blob
  actions_runs_blob="$(_load_actions_runs_cached)"

  # Preserve historical status selection semantics (in_progress/queued, max 50
  # each) while sourcing from one shared payload.
  local runs_json
  runs_json="$(printf '%s' "${actions_runs_blob}" | jq -c '[.workflow_runs[]? | select((.status // "") == "in_progress")] | .[:50]' 2>/dev/null || echo '[]')"
  [ -n "${runs_json}" ] || runs_json='[]'
  local queued_json
  queued_json="$(printf '%s' "${actions_runs_blob}" | jq -c '[.workflow_runs[]? | select((.status // "") == "queued")] | .[:50]' 2>/dev/null || echo '[]')"
  [ -n "${queued_json}" ] || queued_json='[]'


  # Merge both lists
  local all_runs
  all_runs="$(echo "${runs_json}" "${queued_json}" | jq -s 'add // []' 2>/dev/null || echo '[]')"

  # Filter out zombie runs using the effective per-run threshold.
  # Review-family runs use REVIEW_RUN_MAX_RUNTIME_MINUTES inside
  # workflow_run_stall_threshold_seconds(), so they stay in the active set
  # longer than other workflows without duplicating the jq filter here.
  local fresh_runs='[]'
  local -a fresh_run_rows=()
  local run_json
  while IFS= read -r run_json; do
    [ -n "${run_json}" ] || continue
    if workflow_run_is_fresh "${run_json}" "${now_epoch}"; then
      fresh_run_rows+=("${run_json}")
    fi
  done < <(printf '%s' "${all_runs}" | jq -c '.[]?' 2>/dev/null || true)

  if [ "${#fresh_run_rows[@]}" -gt 0 ]; then
    fresh_runs="$(printf '%s\n' "${fresh_run_rows[@]}" | jq -s '.' 2>/dev/null || echo '[]')"
  fi

  local fresh_count
  fresh_count="${#fresh_run_rows[@]}"
  local total_count
  total_count="$(printf '%s' "${all_runs}" | jq 'length' 2>/dev/null || echo '0')"
  [[ "${total_count}" =~ ^[0-9]+$ ]] || total_count=0
  if [ "${total_count}" -gt "${fresh_count}" ]; then
    echo "  Active runs: ${total_count} total, ${fresh_count} fresh ($(( total_count - fresh_count )) zombie runs excluded; general stale cutoff >$(( stall_secs / 60 ))m, review-family cutoff >$(( review_stall_secs / 60 ))m)." >&2
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

cancel_zombie_runs_for_issue() {
  local issue_num="$1"
  local now_epoch
  now_epoch="$(date +%s)"

  # Reuse the shared actions-runs blob and preserve prior in_progress+50 scope.
  local actions_runs_blob
  actions_runs_blob="$(_load_actions_runs_cached)"

  local runs_json
  runs_json="$(printf '%s' "${actions_runs_blob}" | jq -c '[.workflow_runs[]? | select((.status // "") == "in_progress")] | .[:50]' 2>/dev/null || echo '[]')"
  [ -n "${runs_json}" ] || runs_json='[]'

  local -a zombie_run_ids=()
  local run_json run_id head_branch
  while IFS= read -r run_json; do
    [ -n "${run_json}" ] || continue
    if workflow_run_is_fresh "${run_json}" "${now_epoch}"; then
      continue
    fi

    workflow_run_cache_load "${run_json}" || continue
    head_branch="${WORKFLOW_RUN_CACHE_HEAD_BRANCH:-}"
    if ! printf '%s\n' "${head_branch}" | grep -Eq "(^|/)(ai/(issue-)?|ai-(implement-)?)${issue_num}([^0-9]|$)"; then
      continue
    fi

    if workflow_run_is_review_family "${run_json}"; then
      continue
    fi

    run_id="${WORKFLOW_RUN_CACHE_ID:-}"
    if [[ "${run_id}" =~ ^[0-9]+$ ]]; then
      zombie_run_ids+=("${run_id}")
    fi
  done < <(printf '%s' "${runs_json}" | jq -c '.[]?' 2>/dev/null || true)

  if [ "${#zombie_run_ids[@]}" -gt 0 ]; then
    for run_id in "${zombie_run_ids[@]}"; do
      echo "  Cancelling zombie workflow run ${run_id} for issue #${issue_num}..."
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/cancel" -X POST 2>/dev/null || true
    done
  fi
}

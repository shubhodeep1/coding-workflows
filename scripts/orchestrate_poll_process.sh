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
# shellcheck source=tg_helpers.sh
if [ -f "scripts/tg_helpers.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/tg_helpers.sh
fi

# tg_notify wraps tg_send_tracked using the current TRACKING_NUM.
# TRACKING_NUM is set inside the main per-issue loop below.
tg_notify() {
  local msg="$1"
  if [ -n "${TRACKING_NUM:-}" ]; then
    tg_send_tracked "${TRACKING_NUM}" "${msg}"
  else
    # Fallback: untracked send (no issue context yet)
    tg_send_msg "${msg}" >/dev/null
  fi
}

# ---------------------------------------------------------------
# Helper: GitHub API with retry
# ---------------------------------------------------------------
gh_retry() {
  local max_attempts=5
  local attempt=1
  while [ "${attempt}" -le "${max_attempts}" ]; do
    if "$@" 2>/dev/null; then
      return 0
    fi
    local wait_secs=$(( 2 ** (attempt - 1) ))
    echo "::warning::gh command failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s..."
    sleep "${wait_secs}"
    attempt=$(( attempt + 1 ))
  done
  echo "::error::gh command failed after ${max_attempts} attempts: $*"
  return 1
}

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

  if [ -f "${contract_file}" ]; then
    color="$(jq -r --arg lbl "${label_name}" '.labels[$lbl].color // "1d76db"' "${contract_file}" 2>/dev/null || echo "1d76db")"
    description="$(jq -r --arg lbl "${label_name}" '.labels[$lbl].description // "AI workflow label"' "${contract_file}" 2>/dev/null || echo "AI workflow label")"
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

dispatch_validation_workflow() {
  local validation_cycle="$1"
  local wf_name="${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}"
  echo "Dispatching ${wf_name} for tracking #${TRACKING_NUM} (cycle ${validation_cycle})"
  if gh_retry gh workflow run "${wf_name}" \
    --repo "${GITHUB_REPOSITORY}" \
    -f tracking_issue="${TRACKING_NUM}" >/dev/null 2>&1; then
    return 0
  fi
  # Fallback: try internal-validate.yml (coding-workflows repo convention)
  if [ "${wf_name}" != "internal-validate.yml" ]; then
    echo "Primary dispatch failed; trying internal-validate.yml fallback"
    if gh_retry gh workflow run "internal-validate.yml" \
      --repo "${GITHUB_REPOSITORY}" \
      -f tracking_issue="${TRACKING_NUM}" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

dispatch_validation_if_needed() {
  local validation_cycle="$1"
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

  if dispatch_validation_workflow "${validation_cycle}"; then
    jq --argjson cycle "${validation_cycle}" --argjson ts "$(date +%s)" \
      '.validation_last_dispatch_cycle = $cycle | .validation_last_dispatch_ts = $ts' \
      "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    post_state_comment
    post_tracking_comment "## 🧪 Runtime validation dispatched\n\n- Cycle: ${validation_cycle}\n- Workflow: \`${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}\`"
    tg_notify "🧪 Validation dispatched for project #${TRACKING_NUM} (cycle ${validation_cycle})."
    return 0
  fi

  return 1
}

mark_validation_failed() {
  local reason="$1"
  jq --arg reason "${reason}" '.status = "failed" | .validation_failure_reason = $reason | .validation_active_fix_issues = []' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment
  set_tracking_phase_label "ai:validation-failed"
  post_tracking_comment "## ❌ Runtime validation failed\n\n${reason}"
  tg_notify "❌ Project #${TRACKING_NUM} validation failed: ${reason}"
  tg_cleanup_msgs "${TRACKING_NUM}"
}

mark_validation_complete() {
  local validation_cycle="$1"
  jq --argjson cycle "${validation_cycle}" '.status = "complete" | .validation_completed_cycle = $cycle' \
    "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  post_state_comment
  set_tracking_phase_label "ai:validated"
  gh_retry gh issue close "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" \
    --comment "Project completed successfully after runtime validation passed (cycle ${validation_cycle})." || true
  tg_cleanup_msgs "${TRACKING_NUM}"
  tg_send_msg "✅ Project #${TRACKING_NUM} completed after validation pass (cycle ${validation_cycle})." >/dev/null
}

extract_fix_issues_from_comment() {
  local comment_body="$1"
  echo "${comment_body}" | sed -n 's/^- #\([0-9][0-9]*\).*$/\1/p' | awk '!seen[$0]++'
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
    echo "::warning::No state found for tracking issue #${TRACKING_NUM}, skipping."
    continue
  fi

  echo "${STATE_JSON}" > "${STATE_FILE}"
  PROJECT_STATUS="$(jq -r '.status' "${STATE_FILE}")"

  TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"

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
      mark_validation_failed "Validation workflow reported failure (label ai:validation-failed)."
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

    tg_notify "🔁 Attempting to re-dispatch validation for project #${TRACKING_NUM} (cycle ${NEXT_VALIDATION_CYCLE}) after fix-up issues merged."

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

  if [ "${PROJECT_STATUS}" = "complete" ] || [ "${PROJECT_STATUS}" = "failed" ] || [ "${PROJECT_STATUS}" = "validation-failed" ]; then
    echo "Project already ${PROJECT_STATUS}, skipping."
    continue
  fi

  CURRENT_WAVE="$(jq -r '.current_wave' "${STATE_FILE}")"
  TOTAL_WAVES="$(jq -r '.total_waves' "${STATE_FILE}")"
  JUDGE_CYCLE="$(jq -r '.judge_cycle' "${STATE_FILE}")"
  RECOVERY_ATTEMPTED="$(jq -r '.recovery_attempted' "${STATE_FILE}")"

  echo "Current wave: ${CURRENT_WAVE}/${TOTAL_WAVES}, Judge cycle: ${JUDGE_CYCLE}, Recovery attempted: ${RECOVERY_ATTEMPTED}"

  # ---------------------------------------------------------------
  # Collect label states for all child issues in the current wave
  # ---------------------------------------------------------------
  WAVE_IDX=$(( CURRENT_WAVE - 1 ))
  ISSUE_NUMS="$(jq -r ".waves[${WAVE_IDX}].issues[].github_issue" "${STATE_FILE}" 2>/dev/null || echo "")"

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
        echo "  Merging PR #${RTM_PR} (squash)..."
        if gh pr merge "${RTM_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto 2>/dev/null; then
          echo "  PR #${RTM_PR} merge initiated."
        elif gh pr merge "${RTM_PR}" --repo "${GITHUB_REPOSITORY}" --squash 2>/dev/null; then
          echo "  PR #${RTM_PR} merged directly."
        else
          echo "::warning::Could not merge PR #${RTM_PR} for issue #${rtm_issue}. May need manual merge or branch protection prevents it."
        fi
      elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
        echo "  PR #${RTM_PR} is not mergeable (mergeable=${PR_MERGEABLE}). Attempting branch update..."
        if gh api "repos/${GITHUB_REPOSITORY}/pulls/${RTM_PR}/update-branch" \
          -X PUT -f expected_head_sha="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RTM_PR}" --jq '.head.sha' 2>/dev/null)" \
          2>/dev/null; then
          echo "  PR #${RTM_PR} branch updated via API. The synchronize event will re-trigger review (including conflict resolution)."
        else
          echo "  API branch update failed for PR #${RTM_PR}. Attempting local merge + conflict resolution..."

          # Fetch the PR head branch and base branch
          RTM_HEAD_REF="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RTM_PR}" --jq '.head.ref' 2>/dev/null || echo "")"
          if [ -n "${RTM_HEAD_REF}" ] && [ "${RTM_HEAD_REF}" != "null" ]; then
            git fetch origin "${RTM_HEAD_REF}:refs/remotes/origin/${RTM_HEAD_REF}" 2>/dev/null || true
            DEFAULT_BRANCH="$(gh api "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo "main")"
            git fetch origin "${DEFAULT_BRANCH}:refs/remotes/origin/${DEFAULT_BRANCH}" 2>/dev/null || true
            git checkout "origin/${RTM_HEAD_REF}" 2>/dev/null || true
            git config user.name "codex-bot"
            git config user.email "codex@users.noreply.github.com"

            if git merge --no-commit --no-ff "origin/${DEFAULT_BRANCH}" 2>/dev/null; then
              # Clean merge — no conflicts
              git commit -m "[orchestrator-merge] update branch with ${DEFAULT_BRANCH}" 2>/dev/null || true
              git push origin "HEAD:${RTM_HEAD_REF}" 2>/dev/null || true
              echo "  PR #${RTM_PR} branch updated via local merge (no conflicts)."
            elif [ -n "$(git ls-files --unmerged 2>/dev/null)" ]; then
              git merge --abort 2>/dev/null || true
              echo "  PR #${RTM_PR} has real merge conflicts — cannot auto-resolve from poller."
              echo "  The review workflow's Codex conflict resolver handles this on synchronize events."
              tg_notify "⚠️ PR #${RTM_PR} (issue #${rtm_issue}) has real merge conflicts. Attempting to re-trigger review workflow."

              # Force a synchronize event by creating an empty commit on the PR branch
              git checkout "origin/${RTM_HEAD_REF}" 2>/dev/null || true
              git config user.name "codex-bot"
              git config user.email "codex@users.noreply.github.com"
              git commit --allow-empty -m "[orchestrator] re-trigger review for conflict resolution" 2>/dev/null || true
              git push origin "HEAD:${RTM_HEAD_REF}" 2>/dev/null || {
                echo "::warning::Could not push empty commit to re-trigger review for PR #${RTM_PR}."
              }
            else
              git merge --abort 2>/dev/null || true
              echo "::warning::Unexpected merge state for PR #${RTM_PR}."
            fi

            # Return to detached HEAD / original state
            git checkout --detach HEAD 2>/dev/null || true
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
  # unmergeable and pushes an empty commit to force a synchronize
  # event, which re-triggers the review workflow (including its merge
  # conflict resolver).
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
    echo "  In-progress issue #${ip_issue} has PR #${IP_PR} with merge conflicts. Attempting to re-trigger review..."

    # Try the GitHub API update-branch first (creates a merge commit
    # if the merge is clean; fails when there are real conflicts).
    if gh api "repos/${GITHUB_REPOSITORY}/pulls/${IP_PR}/update-branch" \
      -X PUT -f expected_head_sha="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${IP_PR}" --jq '.head.sha' 2>/dev/null)" \
      2>/dev/null; then
      echo "  PR #${IP_PR} branch updated via API. Synchronize event will re-trigger review."
      continue
    fi

    # API update failed — real conflicts exist.  Push an empty commit
    # on the PR branch to force a synchronize event so the review
    # workflow's Codex conflict resolver can handle it.
    IP_HEAD_REF="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${IP_PR}" --jq '.head.ref' 2>/dev/null || echo "")"
    if [ -n "${IP_HEAD_REF}" ] && [ "${IP_HEAD_REF}" != "null" ]; then
      git fetch origin "${IP_HEAD_REF}:refs/remotes/origin/${IP_HEAD_REF}" 2>/dev/null || true
      git checkout "origin/${IP_HEAD_REF}" 2>/dev/null || true
      git config user.name "codex-bot"
      git config user.email "codex@users.noreply.github.com"
      git commit --allow-empty -m "[orchestrator] re-trigger review for conflict resolution" 2>/dev/null || true
      if git push origin "HEAD:${IP_HEAD_REF}" 2>/dev/null; then
        echo "  Pushed empty commit to PR #${IP_PR} to re-trigger review workflow."
        tg_notify "⚠️ PR #${IP_PR} (issue #${ip_issue}) has merge conflicts. Re-triggered review for auto-resolution."
      else
        echo "::warning::Could not push empty commit to re-trigger review for PR #${IP_PR}."
      fi
      git checkout --detach HEAD 2>/dev/null || true
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
        echo "${PR_DIFF}" | head -1000
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
        tg_notify "⚠️ Review-blocked judge failed for issue #${rb_issue} (PR #${RB_PR}). Will retry next poll cycle."
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
          gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
            --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true

          # Attempt squash merge (with branch update if needed)
          PR_STATE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.state' 2>/dev/null || echo "")"
          PR_MERGEABLE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.mergeable' 2>/dev/null || echo "")"
          if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ]; then
            if gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto 2>/dev/null; then
              echo "  PR #${RB_PR} merge initiated (auto)."
            elif gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash 2>/dev/null; then
              echo "  PR #${RB_PR} merged directly."
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
              echo "  API branch update failed for review-blocked PR #${RB_PR}. Pushing empty commit to re-trigger review workflow..."
              RB_HEAD_REF="$(echo "${PR_META}" | jq -r '.head_ref')"
              if [ -n "${RB_HEAD_REF}" ] && [ "${RB_HEAD_REF}" != "null" ]; then
                git fetch origin "${RB_HEAD_REF}:refs/remotes/origin/${RB_HEAD_REF}" 2>/dev/null || true
                git checkout "origin/${RB_HEAD_REF}" 2>/dev/null || true
                git config user.name "codex-bot"
                git config user.email "codex@users.noreply.github.com"
                git commit --allow-empty -m "[orchestrator] re-trigger review for conflict resolution (issue #${rb_issue})" 2>/dev/null || true
                git push origin "HEAD:${RB_HEAD_REF}" 2>/dev/null || {
                  echo "::warning::Could not push to re-trigger review for PR #${RB_PR}."
                  tg_notify "⚠️ Review-blocked PR #${RB_PR} (issue #${rb_issue}) has merge conflicts that could not be auto-resolved."
                }
                git checkout --detach HEAD 2>/dev/null || true
              else
                echo "::warning::Could not determine head ref for review-blocked PR #${RB_PR}."
              fi
            fi
          else
            echo "  PR #${RB_PR} state=${PR_STATE} mergeable=${PR_MERGEABLE}, cannot merge yet."
          fi

          REVIEW_BLOCKED_STATE_CHANGED=true
          tg_notify "✅ Orchestrator judge merged review-blocked PR #${RB_PR} (issue #${rb_issue}): ${RB_JUSTIFICATION}"
          ;;

        fix)
          if [ "${IS_FINAL}" = "true" ]; then
            echo "  Judge returned 'fix' but retries exhausted — treating as merge."
            gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
              --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true
            PR_STATE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.state' 2>/dev/null || echo "")"
            PR_MERGEABLE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.mergeable' 2>/dev/null || echo "")"
            if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ]; then
              gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto 2>/dev/null \
                || gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash 2>/dev/null || true
            elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
              echo "  PR #${RB_PR} is not mergeable (force-merge path). Attempting branch update..."
              if gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}/update-branch" \
                -X PUT -f expected_head_sha="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.head.sha' 2>/dev/null)" \
                2>/dev/null; then
                echo "  PR #${RB_PR} branch updated. Will retry force-merge on next poll cycle."
              else
                echo "  API branch update failed for force-merge PR #${RB_PR}. Re-triggering review for conflict resolution..."
                RB_HEAD_REF="$(echo "${PR_META}" | jq -r '.head_ref')"
                if [ -n "${RB_HEAD_REF}" ] && [ "${RB_HEAD_REF}" != "null" ]; then
                  git fetch origin "${RB_HEAD_REF}:refs/remotes/origin/${RB_HEAD_REF}" 2>/dev/null || true
                  git checkout "origin/${RB_HEAD_REF}" 2>/dev/null || true
                  git config user.name "codex-bot"
                  git config user.email "codex@users.noreply.github.com"
                  git commit --allow-empty -m "[orchestrator] re-trigger review for conflict resolution (issue #${rb_issue})" 2>/dev/null || true
                  git push origin "HEAD:${RB_HEAD_REF}" 2>/dev/null || {
                    echo "::warning::Could not push to re-trigger review for force-merge PR #${RB_PR}."
                    tg_notify "⚠️ Force-merge PR #${RB_PR} (issue #${rb_issue}) has unresolvable merge conflicts."
                  }
                  git checkout --detach HEAD 2>/dev/null || true
                else
                  echo "::warning::Could not determine head ref for force-merge PR #${RB_PR}."
                fi
              fi
            fi
            REVIEW_BLOCKED_STATE_CHANGED=true
            tg_notify "✅ Orchestrator force-merged review-blocked PR #${RB_PR} (retries exhausted, issue #${rb_issue})"
          else
            echo "  Judge is applying fixes to PR #${RB_PR}..."
            # The judge (codex) already modified files in the working directory.
            # We need to checkout the PR branch, apply the changes, and push.
            HEAD_REF="$(echo "${PR_META}" | jq -r '.head_ref')"
            if [ -n "${HEAD_REF}" ] && [ "${HEAD_REF}" != "null" ]; then
              git fetch --no-tags origin "refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF}" 2>/dev/null || true
              git checkout -B "${HEAD_REF}" "refs/remotes/origin/${HEAD_REF}" 2>/dev/null || true

              # Re-run the judge in editing mode on the actual PR branch
              RB_FIX_PROMPT_FILE="${RUNTIME_DIR}/rb_fix_prompt_${rb_issue}.txt"
              RB_FIX_OUTPUT_FILE="${RUNTIME_DIR}/rb_fix_output_${rb_issue}.txt"
              {
                cat "${RB_JUDGE_PROMPT_FILE}"
                echo
                echo "=== APPLY FIXES NOW ==="
                echo "You are now on the PR branch (${HEAD_REF})."
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

              # Check if there are changes to commit
              if [ -n "$(git status --porcelain)" ]; then
                git config user.name "codex-bot"
                git config user.email "codex@users.noreply.github.com"
                git add -A
                git commit -m "[orchestrator-fix] address review-blocked issues for #${rb_issue}

Orchestrator judge applied fixes to unblock the review pipeline.
Retry $((RETRY_COUNT + 1)) of ${MAX_REVIEW_BLOCKED_RETRIES}.

${RB_FIX_DESC}" || true

                git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}"
                if git push origin "HEAD:${HEAD_REF}" 2>/dev/null; then
                  echo "  Pushed [orchestrator-fix] commit to ${HEAD_REF}."
                  # Remove review-blocked label — the push triggers synchronize
                  # which re-runs review_autofix with a reset autofix counter.
                  gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                    --remove-label 'ai:review-blocked' 2>/dev/null || true
                  tg_notify "🔧 Orchestrator judge pushed fix for review-blocked PR #${RB_PR} (issue #${rb_issue}, retry $((RETRY_COUNT + 1))/${MAX_REVIEW_BLOCKED_RETRIES})"
                else
                  echo "::warning::Failed to push orchestrator fix for PR #${RB_PR}."
                fi
              else
                echo "  Judge produced no file changes. Treating as merge decision."
                gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
                  --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true
                PR_STATE="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${RB_PR}" --jq '.state' 2>/dev/null || echo "")"
                if [ "${PR_STATE}" = "open" ]; then
                  gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash --auto 2>/dev/null \
                    || gh pr merge "${RB_PR}" --repo "${GITHUB_REPOSITORY}" --squash 2>/dev/null || true
                fi
                tg_notify "✅ Orchestrator judge merged PR #${RB_PR} (no fix changes needed, issue #${rb_issue})"
              fi

              # Switch back to default branch for remaining processing
              git checkout "${DEFAULT_BRANCH:-main}" 2>/dev/null || git checkout - 2>/dev/null || true
            else
              echo "::warning::Cannot determine PR head branch for #${RB_PR}."
            fi

            # Increment retry counter
            jq ".review_blocked_retries[\"${rb_issue}\"] = $((RETRY_COUNT + 1))" \
              "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
            REVIEW_BLOCKED_STATE_CHANGED=true
          fi
          ;;

        close_and_reissue)
          echo "  Judge says close PR #${RB_PR} and reissue."
          # Close the PR
          gh pr close "${RB_PR}" --repo "${GITHUB_REPOSITORY}" \
            --comment "Closed by orchestrator judge — the approach needs rework. A new issue will be created with refined guidance." \
            2>/dev/null || true

          # Label issue as closed
          gh issue edit "${rb_issue}" --repo "${GITHUB_REPOSITORY}" \
            --remove-label 'ai:review-blocked' --remove-label 'ai:done' \
            --add-label 'ai:closed' 2>/dev/null || true

          # Create replacement issue
          NEW_ISSUE_TITLE="$(echo "${RB_JUDGE_JSON}" | jq -r '.new_issue.title // empty')"
          NEW_ISSUE_BODY="$(echo "${RB_JUDGE_JSON}" | jq -r '.new_issue.body // empty')"
          if [ -n "${NEW_ISSUE_TITLE}" ] && [ -n "${NEW_ISSUE_BODY}" ]; then
            FULL_NEW_BODY="${NEW_ISSUE_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Replaces: #${rb_issue} (PR #${RB_PR} closed — approach rework)
- Type: review-blocked-reissue
- Managed by: AI Orchestrator"

            NEW_URL="$(gh issue create \
              --repo "${GITHUB_REPOSITORY}" \
              --title "${NEW_ISSUE_TITLE}" \
              --body "${FULL_NEW_BODY}")"
            NEW_NUM="$(echo "${NEW_URL}" | grep -oE '[0-9]+$')"
            echo "  Created replacement issue #${NEW_NUM}: ${NEW_ISSUE_TITLE}"

            # Get local_id for the blocked issue and remap it
            LOCAL_ID="$(echo "${WAVE_STATUS}" | jq -r ".issues[] | select(.github_issue == \"${rb_issue}\") | .id")"
            if [ -n "${LOCAL_ID}" ] && [ "${LOCAL_ID}" != "null" ]; then
              jq ".issue_number_map[\"${LOCAL_ID}\"] = ${NEW_NUM}" \
                "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
              # Update the wave entry
              jq "(.waves[${WAVE_IDX}].issues[] | select(.id == \"${LOCAL_ID}\")) |= (.github_issue = ${NEW_NUM} | .status = \"pending\")" \
                "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
            fi

            tg_notify "🔄 Orchestrator closed PR #${RB_PR} and reissued as #${NEW_NUM} (issue #${rb_issue}): ${RB_JUSTIFICATION}"
          else
            echo "::warning::Judge chose close_and_reissue but provided no new issue details."
            tg_notify "⚠️ Orchestrator closed PR #${RB_PR} (issue #${rb_issue}) but could not create replacement issue."
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
    echo "  Issue #${if_issue} has implementation-failed. Closing and re-issuing..."

    # Read the original issue to preserve its content
    IF_TITLE="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.title' 2>/dev/null || echo "")"
    IF_BODY="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${if_issue}" --jq '.body' 2>/dev/null || echo "")"

    # Close the failed issue
    gh issue edit "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
      --remove-label 'ai:implementation-failed' --add-label 'ai:closed' 2>/dev/null || true
    gh issue close "${if_issue}" --repo "${GITHUB_REPOSITORY}" \
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

    NEW_ISSUE_URL="$(gh issue create --repo "${GITHUB_REPOSITORY}" \
      --title "${IF_TITLE}" \
      --body "${NEW_BODY}" 2>/dev/null || echo "")"
    if [ -n "${NEW_ISSUE_URL}" ]; then
      NEW_ISSUE_NUM="$(echo "${NEW_ISSUE_URL}" | grep -oE '[0-9]+$')"
      echo "  Created replacement issue #${NEW_ISSUE_NUM} for failed #${if_issue}."

      # Update state file: replace the old issue number with the new one
      if [ -n "${NEW_ISSUE_NUM}" ]; then
        IF_LOCAL_ID="$(jq -r --arg if_issue "${if_issue}" --argjson wave_idx "${WAVE_IDX}" '.waves[$wave_idx].issues[] | select((.github_issue | tostring) == $if_issue) | .id' "${STATE_FILE}" | head -n 1)"
        jq --arg if_issue "${if_issue}" --arg new_issue_num "${NEW_ISSUE_NUM}" --arg local_id "${IF_LOCAL_ID}" --argjson wave_idx "${WAVE_IDX}" '(.waves[$wave_idx].issues[] | select((.github_issue | tostring) == $if_issue)).github_issue = $new_issue_num | if ($local_id != "" and $local_id != "null") then .issue_number_map[$local_id] = $new_issue_num else . end' \
          "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
      fi

      tg_notify "🔄 Re-issued implementation-failed issue #${if_issue} as #${NEW_ISSUE_NUM}: ${IF_TITLE}"
      IMPL_FAILED_STATE_CHANGED=true
    else
      echo "::warning::Could not create replacement issue for #${if_issue}."
    fi
  done < <(echo "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "implementation-failed") | .github_issue')

  if [ "${IMPL_FAILED_STATE_CHANGED}" = "true" ]; then
    post_state_comment
  fi

  if [ "${WAVE_COMPLETE}" != "true" ]; then
    echo "Wave ${CURRENT_WAVE} not yet complete. Waiting."

    # Update individual issue statuses in state
    echo "${WAVE_STATUS}" | jq -r '.issues[] | "\(.id) \(.status)"' | while read -r local_id status; do
      echo "  ${local_id}: ${status}"
    done
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
    tg_notify "❌ Project #${TRACKING_NUM} FAILED: judge cycle limit (${MAX_JUDGE}) exceeded."
    tg_cleanup_msgs "${TRACKING_NUM}"
    continue
  fi

  # ---------------------------------------------------------------
  # Run judge (full repo checkout + Codex call)
  # ---------------------------------------------------------------
  echo "Running judge evaluation (cycle $((JUDGE_CYCLE + 1)))..."

  # Setup Codex config for judge
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
    echo "Recovery previously attempted: ${RECOVERY_ATTEMPTED}"
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
    tg_notify "❌ Orchestrator Judge failed for #${TRACKING_NUM}. Manual review needed."
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
    tg_notify "❌ Orchestrator Judge output unparseable for #${TRACKING_NUM}. Manual review needed."
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
  tg_notify "🔍 Judge evaluated #${TRACKING_NUM} (cycle $((JUDGE_CYCLE + 1))): ${JUDGE_STATUS}. ${JUDGE_JUSTIFICATION}"

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
        echo "Project complete!"
        jq '.status = "complete" | .judge_cycle += 1' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
        post_state_comment

        gh issue close "${TRACKING_NUM}" --repo "${GITHUB_REPOSITORY}" \
          --comment "Project completed successfully after $((JUDGE_CYCLE + 1)) judge cycle(s)." || true

        tg_cleanup_msgs "${TRACKING_NUM}"
        tg_send_msg "✅ Project #${TRACKING_NUM} completed! All waves merged and judge approved." >/dev/null
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
      # Auto-recovery: attempt once
      # ---------------------------------------------------------------
      if [ "${RECOVERY_ATTEMPTED}" = "true" ]; then
        echo "Recovery already attempted. Stopping."
        jq '.status = "failed" | .judge_cycle += 1' "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

        post_state_comment

        gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/comments" \
          -f body="## Project Failed

Recovery was attempted but the judge still reports failure. Manual intervention required.

**Assessment:** ${JUDGE_ASSESSMENT}" >/dev/null

        tg_notify "❌ Project #${TRACKING_NUM} FAILED after recovery attempt. Manual intervention needed."
        tg_cleanup_msgs "${TRACKING_NUM}"
        continue
      fi

      echo "Attempting auto-recovery..."

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
          FIX_BODY="$(echo "${fix_issue}" | jq -r '.body')"
          FIX_ID="$(echo "${fix_issue}" | jq -r '.id')"

          # --- Dedup guard: skip if this local ID already has a GitHub issue ---
          if [ -n "${FIX_ID}" ] && [ "${FIX_ID}" != "null" ]; then
            EXISTING_NUM="$(jq -r --arg fix_id "${FIX_ID}" '.issue_number_map[$fix_id] // empty' "${STATE_FILE}")"
            if [ -n "${EXISTING_NUM}" ]; then
              EXISTING_LABELS="$(get_issue_labels_json "${EXISTING_NUM}")"
              if ! has_label "${EXISTING_LABELS}" "ai:merged" && ! has_label "${EXISTING_LABELS}" "ai:closed"; then
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
- Local ID: \`${FIX_ID}\`
- Type: judge-fix-up (cycle $((JUDGE_CYCLE + 1)))
- Managed by: AI Orchestrator"

          FIX_URL="$(gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${FIX_TITLE}" \
            --body "${FULL_FIX_BODY}")"
          echo "  Created fix-up: ${FIX_URL}"

          # Record in state so subsequent cycles/iterations won't recreate
          FIX_NEW_NUM="$(echo "${FIX_URL}" | grep -oE '[0-9]+$')"
          if [ -n "${FIX_NEW_NUM}" ] && [ -n "${FIX_ID}" ] && [ "${FIX_ID}" != "null" ]; then
            jq --arg fix_id "${FIX_ID}" --argjson fix_new_num "${FIX_NEW_NUM}" '.issue_number_map[$fix_id] = $fix_new_num' \
              "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          fi
        done
      fi

      # Update state
      jq '.judge_cycle += 1 | .recovery_attempted = true | .status = "in_progress"' \
        "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"

      post_state_comment

      tg_notify "🔄 Orchestrator auto-recovery started for #${TRACKING_NUM}: ${NEW_ISSUES_COUNT} fix-up issues, ${REVERT_COUNT} reverts."
      ;;

    in_progress)
      echo "Project in progress."

      # Create new issues if judge found gaps
      if [ "${NEW_ISSUES_COUNT}" -gt 0 ]; then
        echo "Creating ${NEW_ISSUES_COUNT} new issue(s) from judge..."
        echo "${JUDGE_JSON}" | jq -c '.new_issues[]' | while read -r new_issue; do
          NEW_TITLE="$(echo "${new_issue}" | jq -r '.title')"
          NEW_BODY="$(echo "${new_issue}" | jq -r '.body')"
          NEW_ID="$(echo "${new_issue}" | jq -r '.id')"

          # --- Dedup guard: skip if this local ID already has a GitHub issue ---
          if [ -n "${NEW_ID}" ] && [ "${NEW_ID}" != "null" ]; then
            EXISTING_NUM="$(jq -r --arg new_id "${NEW_ID}" '.issue_number_map[$new_id] // empty' "${STATE_FILE}")"
            if [ -n "${EXISTING_NUM}" ]; then
              EXISTING_LABELS="$(get_issue_labels_json "${EXISTING_NUM}")"
              if ! has_label "${EXISTING_LABELS}" "ai:merged" && ! has_label "${EXISTING_LABELS}" "ai:closed"; then
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
- Local ID: \`${NEW_ID}\`
- Type: judge-addition (cycle $((JUDGE_CYCLE + 1)))
- Managed by: AI Orchestrator"

          NEW_URL="$(gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${NEW_TITLE}" \
            --body "${FULL_NEW_BODY}")"
          echo "  Created: ${NEW_URL}"

          # Record in state so subsequent cycles/iterations won't recreate,
          # and add to the current wave so the poller tracks merge progress.
          ADD_NEW_NUM="$(echo "${NEW_URL}" | grep -oE '[0-9]+$')"
          if [ -n "${ADD_NEW_NUM}" ] && [ -n "${NEW_ID}" ] && [ "${NEW_ID}" != "null" ]; then
            jq --arg new_id "${NEW_ID}" --argjson add_new_num "${ADD_NEW_NUM}" --argjson wave_idx "${WAVE_IDX}" \
              '.issue_number_map[$new_id] = $add_new_num | .waves[$wave_idx].issues += [{"id": $new_id, "github_issue": $add_new_num, "status": "pending"}]' \
              "${STATE_FILE}" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "${STATE_FILE}"
          fi
        done
      fi

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
          DEF_BODY="$(echo "${ISSUE_DEF}" | jq -r '.body')"
          DEF_PRIORITY="$(echo "${ISSUE_DEF}" | jq -r '.priority')"

          FULL_BODY="${DEF_BODY}

---
**Orchestrator metadata** (do not edit)
- Tracking issue: #${TRACKING_NUM}
- Local ID: \`${local_id}\`
- Priority: ${DEF_PRIORITY}
- Managed by: AI Orchestrator"

          NEW_URL="$(gh issue create \
            --repo "${GITHUB_REPOSITORY}" \
            --title "${DEF_TITLE}" \
            --body "${FULL_BODY}")"

          NEW_NUM="$(echo "${NEW_URL}" | grep -oE '[0-9]+$')"
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
      ;;

    *)
      echo "::warning::Unknown judge status: ${JUDGE_STATUS}"
      ;;
  esac
done

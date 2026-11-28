#!/usr/bin/env bash
# Body of the review_autofix.yml "Mark linked issues review-blocked (autofix exhaustion)" step (sourced by
# the step; moved out to keep the workflow under the review_autofix.yml size guard).
set -euo pipefail
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
if [ -f "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" ] && source "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" 2>/dev/null; then
  :
else
  if [ -f "${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/label_helpers.sh" ]; then
    mkdir -p "${SUPPORT_SCRIPTS_DIR}"
    install -m 0755 "${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/label_helpers.sh" "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh"
    source "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" 2>/dev/null || true
  else
    type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
    ensure_label_exists() {
      local label_name="$1"
      local repo="$2"
      case "${label_name}" in
        ai:review-blocked)
          gh_retry gh label create "${label_name}" --repo "${repo}" --color "e11d48" --description "PR review/autofix could not resolve all issues — needs human intervention" 2>/dev/null || true
          ;;
        *)
          gh_retry gh label create "${label_name}" --repo "${repo}" --color "1d76db" --description "AI workflow label" 2>/dev/null || true
          ;;
      esac
    }
    set_issue_phase_label_resilient() {
      local issue_number="$1"
      local target_label="$2"
      local repo="$3"
      ensure_label_exists "${target_label}" "${repo}" || true
      if GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST "repos/${repo}/issues/${issue_number}/labels" \
        -f "labels[]=${target_label}" >/dev/null 2>&1; then
        SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME="applied"
      else
        SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME="failed"
        echo "::warning::Fallback label add failed for #${issue_number}. Label may need manual application."
      fi
    }
  fi
fi
if ! type set_issue_phase_label_resilient >/dev/null 2>&1; then
  set_issue_phase_label_resilient() {
    local issue_number="$1"
    local target_label="$2"
    local repo="$3"
    ensure_label_exists "${target_label}" "${repo}" || true
    if GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST "repos/${repo}/issues/${issue_number}/labels" \
      -f "labels[]=${target_label}" >/dev/null 2>&1; then
      SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME="applied"
    else
      SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME="failed"
      echo "::warning::Fallback label add failed for #${issue_number}. Label may need manual application."
    fi
  }
fi

ISSUE_NUMBERS="$(echo "${LINKED_ISSUES_JSON:-[]}" | jq -r '.[].number' 2>/dev/null || true)"

if [ -z "${ISSUE_NUMBERS}" ]; then
  ISSUE_NUMBERS="$(printf '%s' "${LINKED_ISSUE_FALLBACK_NUMBERS_JSON:-[]}" | jq -r '.[]' 2>/dev/null || true)"
fi

if [ -z "${ISSUE_NUMBERS}" ]; then
  # Fallback: strict current-repo closing-keyword refs and repo-scoped issue URLs/paths only.
  PR_DATA="$(jq -r '[.title // "", .body // ""] | join(" ")' "${PR_META_FILE}" 2>/dev/null || echo "")"
  if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
    PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
  fi
  if type extract_repo_scoped_issue_refs_from_text >/dev/null 2>&1; then
    ISSUE_NUMBERS="$(extract_repo_scoped_issue_refs_from_text "${REPOSITORY}" "${PR_DATA}" || true)"
  else
    ISSUE_NUMBERS=""
  fi
  if [ -z "${ISSUE_NUMBERS}" ]; then
    echo "No linked issues found for PR #${PR_NUMBER} (even after body/title fallback)."
    { echo "labelled_issue_numbers="; echo "labelled_issue_numbers_resolved=true"; } >> "$GITHUB_OUTPUT"
    exit 0
  fi
  echo "Found linked issues via PR body/title fallback: ${ISSUE_NUMBERS}"
fi

ensure_label_exists "ai:review-blocked" "${REPOSITORY}"
# Issue #7099: record the issues actually labelled so the outage
# marker step marks exactly that set (including PR-text fallbacks).
labelled=()
while IFS= read -r issue_number; do
  [ -n "${issue_number}" ] || continue
  [[ "${issue_number}" =~ ^[0-9]+$ ]] || continue
  echo "Setting ai:review-blocked on issue #${issue_number}"
  SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME=""
  set_issue_phase_label_resilient "${issue_number}" "ai:review-blocked" "${REPOSITORY}"
  case "${SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME:-}" in
    applied) labelled+=("${issue_number}") ;;
    "")
      # Helper copy without the outcome signal: keep the old "attempted" behaviour.
      echo "::notice::Label outcome unknown for #${issue_number}; treating it as labelled."
      labelled+=("${issue_number}")
      ;;
  esac
done <<< "${ISSUE_NUMBERS}"
labelled_csv="$(IFS=,; printf '%s' "${labelled[*]:-}")"
{ echo "labelled_issue_numbers=${labelled_csv}"; echo "labelled_issue_numbers_resolved=true"; } >> "$GITHUB_OUTPUT"

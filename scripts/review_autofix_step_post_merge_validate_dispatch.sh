#!/usr/bin/env bash
# Body of the "Dispatch standalone validate for orchestrator short-circuit issues" step
# (job `post-merge-validate-dispatch`) in .github/workflows/review_autofix.yml, moved out of the
# workflow to keep it at or under 380,000 bytes, well under the
# 430,000-byte contract-test ceiling and GitHub's 512,000-byte workflow file
# limit (#10166). The job has no staged support bundle: the step sources
# this file only from the job's own .codex-workflow-src checkout, after
# "Verify workflow support source" checked it against review_support_sha
# and the wrapper re-checked HEAD against WORKFLOW_SUPPORT_REF. The step's
# name: stays in the workflow; edit it there.
set -euo pipefail

if [ -f ".codex-workflow-src/scripts/gh_helpers.sh" ]; then
  source ".codex-workflow-src/scripts/gh_helpers.sh" 2>/dev/null || true
fi
if ! type gh_retry >/dev/null 2>&1; then
  # Buffers each attempt's stdout and prints only the successful
  # attempt's, like scripts/gh_helpers.sh: gh api prints a failed
  # response body to stdout, which would otherwise land in front
  # of the real response in a $(gh_retry ...) capture (#5495).
  gh_retry() {
    local n=0 max=4 delay=2
    local _gh_retry_inline_out _gh_retry_inline_bytes _gh_retry_inline_rc=0
    if ! _gh_retry_inline_out="$(mktemp "${TMPDIR:-/tmp}/gh_retry_stdout.XXXXXX" 2>/dev/null)"; then
      echo "::error::gh_retry: failed to create stdout temp file (mktemp failed); aborting without running: $*" >&2
      return 1
    fi
    while :; do
      if "$@" >"${_gh_retry_inline_out}"; then
        cat "${_gh_retry_inline_out}" || _gh_retry_inline_rc=$?
        rm -f "${_gh_retry_inline_out}"
        return "${_gh_retry_inline_rc}"
      fi
      n=$((n+1))
      if [ -s "${_gh_retry_inline_out}" ]; then
        _gh_retry_inline_bytes="$(wc -c <"${_gh_retry_inline_out}" 2>/dev/null | tr -d '[:space:]' || true)"
        echo "::warning::  gh_retry: dropped ${_gh_retry_inline_bytes:-?} bytes of stdout from failed attempt ${n}/${max}" >&2
      fi
      if [ "${n}" -ge "${max}" ]; then rm -f "${_gh_retry_inline_out}"; return 1; fi
      sleep "${delay}"
      delay=$((delay*2))
    done
  }
fi

if [[ ! "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
  echo "::warning::Skipping post-merge validate dispatch: invalid PR number '${PR_NUMBER}'."
  exit 0
fi

normalize_post_merge_issue_nodes_json() {
  printf '%s' "${1:-[]}" | jq -c 'if type == "array" then [.[] | select((.number | tonumber?) != null) | {number: (.number | tonumber), labels: (if (.labels | type) == "array" then .labels else null end)}] else [] end' 2>/dev/null || echo '[]'
}

issue_nodes_json="$(normalize_post_merge_issue_nodes_json "${POST_MERGE_LINKED_ISSUES_JSON:-[]}")"
post_merge_linked_issue_lookup_failed="false"
post_merge_pr_text_lookup_failed="false"
post_merge_pr_text_fallback_helper_unavailable="false"

if [ "${issue_nodes_json}" = "[]" ]; then
  if issue_nodes_json_live="$(gh_retry gh api graphql \
    -f owner="${REPOSITORY%/*}" \
    -f name="${REPOSITORY#*/}" \
    -F number="${PR_NUMBER}" \
    -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number labels(first: 100) { nodes { name } } } } } } }' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes // [] | map({number: .number, labels: ((.labels.nodes // []) | map(.name))})' \
    2>/dev/null)"; then
    issue_nodes_json="$(normalize_post_merge_issue_nodes_json "${issue_nodes_json_live}")"
  else
    post_merge_linked_issue_lookup_failed="true"
    issue_nodes_json='[]'
    echo "::warning::Unable to refresh closingIssuesReferences for merged PR #${PR_NUMBER} after retries; continuing with PR body/title fallback."
  fi
fi

if [ -z "${issue_nodes_json}" ] || [ "${issue_nodes_json}" = "[]" ]; then
  pr_data="$(printf '%s' "${POST_MERGE_PR_TEXT_JSON:-{}}" | jq -r 'if type == "object" then [(.title // ""), (.body // "")] | join(" ") else "" end' 2>/dev/null || echo "")"
  if [ -z "$(printf '%s' "${pr_data}" | tr -d '[:space:]')" ]; then
    if pr_data_live="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' 2>/dev/null)"; then
      pr_data="${pr_data_live}"
    else
      post_merge_pr_text_lookup_failed="true"
      pr_data=""
      echo "::warning::Unable to refresh PR title/body for merged PR #${PR_NUMBER} after retries; skipping live PR text fallback."
    fi
  fi
  issue_numbers=""
  if [ -n "$(printf '%s' "${pr_data}" | tr -d '[:space:]')" ]; then
    if type extract_repo_scoped_issue_refs_from_text >/dev/null 2>&1; then
      issue_numbers="$(extract_repo_scoped_issue_refs_from_text "${REPOSITORY}" "${pr_data}" || true)"
    else
      post_merge_pr_text_fallback_helper_unavailable="true"
      echo "::warning::Shared linked-issue fallback helper unavailable; skipping PR body/title fallback for PR #${PR_NUMBER}."
    fi
  fi
  if [ -z "${issue_numbers}" ]; then
    if [ "${post_merge_linked_issue_lookup_failed}" = "true" ] || [ "${post_merge_pr_text_lookup_failed}" = "true" ]; then
      echo "::warning::Standalone validate dispatch could not confirm linked issues for merged PR #${PR_NUMBER} because GitHub read retries were exhausted; leaving validation state unchanged."
    elif [ "${post_merge_pr_text_fallback_helper_unavailable}" = "true" ]; then
      echo "::warning::Standalone validate dispatch could not confirm linked issues for merged PR #${PR_NUMBER} because the PR body/title fallback helper was unavailable; leaving validation state unchanged."
    else
      echo "No linked issues found for merged PR #${PR_NUMBER}."
    fi
    exit 0
  fi
  echo "Found linked issues via PR body/title fallback: ${issue_numbers}"

  issue_nodes_json="$(printf '%s\n' "${issue_numbers}" | jq -Rsc 'split("\n") | map(select(length > 0)) | map({number: tonumber, labels: null})')"
fi

validation_dispatched="false"
validation_required_seen="" validation_label_unknown=""
validation_dispatch_failed=""
validate_workflow="${VALIDATE_WORKFLOW_NAME:-ai-validate.yml}"
while IFS=$'\t' read -r issue_number has_validate_label labels_known; do
  [ -n "${issue_number}" ] || continue

  if [ "${labels_known}" != "true" ]; then
    if issue_labels="$(gh_retry gh issue view "${issue_number}" --repo "${REPOSITORY}" --json labels --jq '.labels[].name' 2>/dev/null)"; then
      if echo "${issue_labels}" | grep -Fxq 'ai:orchestrator-validate-required'; then
        has_validate_label="true"
      else
        has_validate_label="false"
      fi
    else
      echo "::warning::Unable to read labels for issue #${issue_number} after retries; skipping standalone validation because ai:orchestrator-validate-required state is unknown."
      validation_label_unknown=1
      continue
    fi
  fi

  if [ "${has_validate_label}" != "true" ]; then
    echo "Issue #${issue_number} does not have ai:orchestrator-validate-required; skipping."
    continue
  fi
  validation_required_seen=1

  if [ "${validation_dispatched}" = "false" ]; then
    echo "Dispatching standalone validation for linked issue #${issue_number} (tracking_issue=0)."
    # Single attempt on purpose: a dispatch whose response was lost may already
    # have started a run, and a retry would start a duplicate validation run.
    if gh workflow run "${validate_workflow}" --repo "${REPOSITORY}" -f tracking_issue="0" -f pr_number="${PR_NUMBER}" || { [ "${validate_workflow}" != "internal-validate.yml" ] && gh workflow run "internal-validate.yml" --repo "${REPOSITORY}" -f tracking_issue="0" -f pr_number="${PR_NUMBER}"; }; then
      validation_dispatched="true"
    else
      validation_dispatch_failed=1
      echo "::warning::Failed to dispatch ${validate_workflow}/internal-validate.yml for issue #${issue_number}; the job fails after the remaining linked issues are processed if none of them dispatches validation."
      continue
    fi
  fi

  if ! gh_retry gh issue edit "${issue_number}" --repo "${REPOSITORY}" --remove-label 'ai:orchestrator-validate-required'; then
    echo "::warning::Standalone validation dispatched for issue #${issue_number}, but failed to remove ai:orchestrator-validate-required."
  fi
done < <(printf '%s' "${issue_nodes_json}" | jq -r '.[] | [.number, (if (.labels | type) == "array" then ((.labels | index("ai:orchestrator-validate-required")) != null) else false end), ((.labels | type) == "array")] | @tsv')

if [ -z "${validation_required_seen}${validation_label_unknown}" ]; then
  echo "::notice::No linked issue of merged PR #${PR_NUMBER} required standalone validation; nothing dispatched."
elif [ "${validation_dispatched}" = "false" ] && [ -n "${validation_required_seen}" ] && [ -n "${validation_dispatch_failed}" ]; then
  echo "::error::Standalone validation was required for merged PR #${PR_NUMBER} but could not be dispatched; the ai:orchestrator-validate-required label is kept, re-run this job to retry."
  exit 1
elif [ "${validation_dispatched}" = "false" ]; then
  echo "::warning::No standalone validation workflow could be dispatched for merged PR #${PR_NUMBER}."
fi

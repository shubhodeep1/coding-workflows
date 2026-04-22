#!/usr/bin/env bash
# label_helpers.sh — idempotent AI label creation helpers.
# Source this file in workflow steps that apply ai:* labels.
#
# Usage:
#   source scripts/label_helpers.sh
#   ensure_label_exists "ai:review-blocked" "${REPOSITORY}"

# Hardcoded label catalog (mirrors .github/ai/label_contract.v1.json).
# Keeps the helper self-contained so it works even when the contract
# file is not present in the checked-out repo.
# shellcheck source=gh_helpers.sh
if [ -f "scripts/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source scripts/gh_helpers.sh
fi
# Fallback if gh_helpers.sh not available
if ! type gh_retry >/dev/null 2>&1; then
	gh_retry() { "$@"; }
fi

declare -A _AI_LABEL_COLORS=(
	["ai:clarification"]="f9d0c4"
	["ai:planning"]="d4c5f9"
	["ai:awaiting-approval"]="fbca04"
	["ai:implementing"]="0052cc"
	["ai:validating"]="1d76db"
	["ai:validated"]="0e8a16"
	["ai:validation-failed"]="e11d48"
	["ai:validation-fixing"]="d93f0b"
	["ai:validation-recovery"]="e3a21a"
	["ai:clarify-failed"]="e11d48"
	["ai:clarify-respond-failed"]="e11d48"
	["ai:plan-failed"]="e11d48"
	["ai:implement-diagnose-failed"]="e11d48"
	["ai:review-autofix-failed"]="e11d48"
	["ai:validate-failed"]="e11d48"
	["ai:integration-judge-failed"]="e11d48"
	["ai:log-analysis-failed"]="e11d48"
	["ai:memory-maintenance-failed"]="e11d48"
	["ai:done"]="0e8a16"
	["ai:ready-to-merge"]="0e8a16"
	["ai:needs-human"]="e11d48"
	["ai:blocked"]="b60205"
	["ai:merged"]="5319e7"
	["ai:review-blocked"]="e11d48"
	["ai:implementation-failed"]="e11d48"
	["ai:implement-fix-up"]="d4c5f9"
	["ai:destructive-blocked"]="b60205"
	["ai:closed"]="6a737d"
	["ai:orchestrator-tracking"]="a2eeef"
	["ai:orchestrator-managed"]="bfdadc"
	["ai:orchestrator-validate-required"]="c5def5"
	["ai:comprehensive-test-pending"]="1d76db"
	["ai:needs-prompt-review"]="fbca04"
)

declare -A _AI_LABEL_DESCS=(
	["ai:clarification"]="AI clarification required before planning"
	["ai:planning"]="AI planning in progress"
	["ai:awaiting-approval"]="Awaiting human approval to implement"
	["ai:implementing"]="AI implementation in progress"
	["ai:validating"]="Runtime validation in progress (post-judge)"
	["ai:validated"]="Runtime validation passed - ready for release"
	["ai:validation-failed"]="Runtime validation failed - manual review needed"
	["ai:validation-fixing"]="Validation fix-up issues in pipeline"
	["ai:validation-recovery"]="Validation failed — judge re-evaluating before retry"
	["ai:clarify-failed"]="Clarify workflow failed before producing a valid response"
	["ai:clarify-respond-failed"]="Orchestrator clarify-respond workflow failed before posting an answer"
	["ai:plan-failed"]="Plan workflow failed before producing an approved implementation plan"
	["ai:implement-diagnose-failed"]="Implement diagnose workflow failed while generating fix-up guidance"
	["ai:review-autofix-failed"]="Review/autofix workflow failed before completing the review pass"
	["ai:validate-failed"]="Validate workflow failed before runtime validation could complete"
	["ai:integration-judge-failed"]="Integration judge workflow failed before returning a verdict"
	["ai:log-analysis-failed"]="Workflow log analysis failed before producing an analysis report"
	["ai:memory-maintenance-failed"]="AI memory maintenance workflow failed before completing maintenance"
	["ai:done"]="Implementation PR created"
	["ai:ready-to-merge"]="PR review complete and ready to merge"
	["ai:needs-human"]="Escalated for human intervention; autonomous stall recovery is paused"
	["ai:blocked"]="Orchestrator auto-answer loop blocked pending human intervention"
	["ai:merged"]="Linked PR merged"
	["ai:review-blocked"]="PR review/autofix could not resolve all issues — needs human intervention"
	["ai:implementation-failed"]="Implementation produced no changes despite an approved plan — will be re-issued"
	["ai:implement-fix-up"]="Implement-phase post-Codex fix-up issue"
	["ai:destructive-blocked"]="Implementation commit was refused for mass/destructive deletions — redispatch of this issue ID is blocked pending human review"
	["ai:closed"]="Linked PR closed without merge"
	["ai:orchestrator-tracking"]="Orchestrator project tracking issue"
	["ai:orchestrator-managed"]="Issue is managed by the AI orchestrator"
	["ai:orchestrator-validate-required"]="Orchestrator issue requires validate phase before completion"
	["ai:comprehensive-test-pending"]="Pending comprehensive release callback dispatch."
	["ai:needs-prompt-review"]="Validation prompt self-heal PR awaiting manual review"
)

_AI_PHASE_TRANSITION_LABELS=(
	"ai:done"
	"ai:implementing"
	"ai:awaiting-approval"
	"ai:planning"
	"ai:clarification"
	"ai:ready-to-merge"
	"ai:review-blocked"
	"ai:implementation-failed"
	"ai:merged"
	"ai:closed"
)

# ensure_label_exists <label_name> [repo]
#
# Creates the label if it does not already exist.  Idempotent — safe to
# call multiple times.  Uses hardcoded catalog above; falls back to
# sensible defaults for unknown labels.
#
# $1  — label name  (required)
# $2  — repo slug   (optional; defaults to GITHUB_REPOSITORY)
ensure_label_exists() {
	local label_name="${1:?ensure_label_exists: label_name required}"
	local repo="${2:-${GITHUB_REPOSITORY:-}}"
	local color="${_AI_LABEL_COLORS["${label_name}"]:-1d76db}"
	local description="${_AI_LABEL_DESCS["${label_name}"]:-AI workflow label}"

	if [ -z "${repo}" ]; then
		echo "ensure_label_exists: repo required (pass as \$2 or set GITHUB_REPOSITORY)" >&2
		return 1
	fi

	local _label_err_file
	_label_err_file="$(mktemp 2>/dev/null || echo '/dev/null')"

	if gh_retry gh label create "${label_name}" \
		--repo "${repo}" \
		--color "${color}" \
		--description "${description}" \
		>/dev/null 2>"${_label_err_file}"; then
		[ "${_label_err_file}" = "/dev/null" ] || rm -f "${_label_err_file}"
		return 0
	fi

	local _label_err=""
	_label_err="$(cat "${_label_err_file}" 2>/dev/null || true)"
	[ "${_label_err_file}" = "/dev/null" ] || rm -f "${_label_err_file}"

	if printf '%s' "${_label_err}" | grep -Eiq 'already[ _-]*exists|already_exists'; then
		echo "::debug::ensure_label_exists: label already exists, skipping '${label_name}'." >&2
		return 0
	fi

	echo "::warning::ensure_label_exists: failed to create label '${label_name}' in repo '${repo}': ${_label_err}" >&2
	return 1
}

# set_issue_phase_label_resilient <issue_number> <target_label> [repo]
#
# Best-effort phase transition for mutually-exclusive ai:* phase labels.
# - Ensures the target label exists (non-fatal on failure).
# - Removes other phase labels with resilient DELETE calls.
# - Ensures the target label is present via POST add fallback.
set_issue_phase_label_resilient() {
	local issue_number="${1:-}"
	local target_label="${2:-}"
	local repo="${3:-${GITHUB_REPOSITORY:-}}"

	if [ -z "${issue_number}" ] || [ -z "${target_label}" ] || [ -z "${repo}" ]; then
		echo "::warning::set_issue_phase_label_resilient: missing required arguments (issue='${issue_number}', target='${target_label}', repo='${repo}')." >&2
		return 0
	fi

	if ! ensure_label_exists "${target_label}" "${repo}"; then
		echo "::warning::set_issue_phase_label_resilient: ensure_label_exists failed for '${target_label}' in '${repo}' (continuing)." >&2
	fi

	local _phase_label _encoded_label _del_err_file _del_err
	for _phase_label in "${_AI_PHASE_TRANSITION_LABELS[@]}"; do
		[ "${_phase_label}" = "${target_label}" ] && continue

		# ai:* labels contain ':' and '-' only; encode ':' for URL path safety.
		_encoded_label="${_phase_label//:/%3A}"

		_del_err_file="$(mktemp 2>/dev/null || echo '/dev/null')"
		if gh_retry gh api -X DELETE "repos/${repo}/issues/${issue_number}/labels/${_encoded_label}" \
			>/dev/null 2>"${_del_err_file}"; then
			[ "${_del_err_file}" = "/dev/null" ] || rm -f "${_del_err_file}"
			continue
		fi

		_del_err="$(cat "${_del_err_file}" 2>/dev/null || true)"
		[ "${_del_err_file}" = "/dev/null" ] || rm -f "${_del_err_file}"

		if printf '%s' "${_del_err}" | grep -Eiq '404|not[[:space:]-]*found|does[[:space:]-]*not[[:space:]-]*exist|unprocessable'; then
			echo "::debug::set_issue_phase_label_resilient: delete skipped for '${_phase_label}' on #${issue_number}." >&2
			continue
		fi

		echo "::warning::set_issue_phase_label_resilient: failed deleting '${_phase_label}' from #${issue_number}: ${_del_err}" >&2
	done

	if ! gh_retry gh api -X POST "repos/${repo}/issues/${issue_number}/labels" \
		-f "labels[]=${target_label}" >/dev/null 2>&1; then
		echo "::warning::set_issue_phase_label_resilient: failed to apply '${target_label}' on issue #${issue_number} in repo '${repo}'." >&2
	fi

	return 0
}

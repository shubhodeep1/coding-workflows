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
	["ai:done"]="0e8a16"
	["ai:ready-to-merge"]="0e8a16"
	["ai:needs-human"]="e11d48"
	["ai:merged"]="5319e7"
	["ai:review-blocked"]="e11d48"
	["ai:implementation-failed"]="e11d48"
	["ai:destructive-blocked"]="b60205"
	["ai:closed"]="6a737d"
	["ai:orchestrator-tracking"]="a2eeef"
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
	["ai:done"]="Implementation PR created"
	["ai:ready-to-merge"]="PR review complete and ready to merge"
	["ai:needs-human"]="Escalated for human intervention; autonomous stall recovery is paused"
	["ai:merged"]="Linked PR merged"
	["ai:review-blocked"]="PR review/autofix could not resolve all issues — needs human intervention"
	["ai:implementation-failed"]="Implementation produced no changes despite an approved plan — will be re-issued"
	["ai:destructive-blocked"]="Implementation commit was refused for mass/destructive deletions — redispatch of this issue ID is blocked pending human review"
	["ai:closed"]="Linked PR closed without merge"
	["ai:orchestrator-tracking"]="Orchestrator project tracking issue"
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

	gh_retry gh label create "${label_name}" \
		--repo "${repo}" \
		--color "${color}" \
		--description "${description}" \
		2>/dev/null || true
}

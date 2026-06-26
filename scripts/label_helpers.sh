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
	["ai:resolver-escalated"]="b60205"
	["ai:harness-broken"]="e11d48"
	["ai:force-merge"]="fbca04"
	["ai:integration-backpressure"]="e3a21a"
	["ai:implementation-failed"]="e11d48"
	["ai:implement-fix-up"]="d4c5f9"
	["ai:destructive-blocked"]="b60205"
	["ai:scope-blocked"]="b60205"
	["ai:closed"]="6a737d"
	["ai:orchestrator-tracking"]="a2eeef"
	["ai:orchestrator-managed"]="bfdadc"
	["ai:orchestrator-validate-required"]="c5def5"
	["ai:comprehensive-test-pending"]="1d76db"
	["ai:needs-prompt-review"]="fbca04"
	["ai:review-skipped"]="c2e0c6"
	["ai:retro"]="5319e7"
	["ai:security-audit"]="5319e7"
	["ai:security"]="0e8a16"
	["force-review"]="fbca04"
	["e2e-smoke-test"]="5319e7"
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
	["ai:resolver-escalated"]="Integration conflict resolver hit the escape threshold; redispatch waits for a new PR head"
	["ai:harness-broken"]="Validation harness is broken and requires manual repair before automated validation can continue"
	["ai:force-merge"]="Operator override requested to merge despite normal automation gates"
	["ai:integration-backpressure"]="Integration automation is intentionally throttled due to downstream backpressure"
	["ai:implementation-failed"]="Implementation produced no changes despite an approved plan — will be re-issued"
	["ai:implement-fix-up"]="Implement-phase post-Codex fix-up issue"
	["ai:destructive-blocked"]="Implementation blocked for mass/destructive deletions; this issue ID now waits for human review"
	["ai:scope-blocked"]="Implementation blocked: staged files fell outside files_touched scope; human review required"
	["ai:closed"]="Linked PR closed without merge"
	["ai:orchestrator-tracking"]="Orchestrator project tracking issue"
	["ai:orchestrator-managed"]="Issue is managed by the AI orchestrator"
	["ai:orchestrator-validate-required"]="Orchestrator issue requires validate phase before completion"
	["ai:comprehensive-test-pending"]="Pending comprehensive release callback dispatch."
	["ai:needs-prompt-review"]="Validation prompt self-heal PR awaiting manual review"
	["ai:review-skipped"]="Reviewer panel + editor cycle skipped by deterministic gate (doc-only or under size threshold)"
	["ai:retro"]="Stable tracker issue for weekly workflow retrospectives"
	["ai:security-audit"]="Stable tracker issue for periodic default-branch security audits"
	["ai:security"]="Follow-up issue opened by the periodic security audit workflow"
	["force-review"]="Force a full review_autofix cycle, bypassing the deterministic pre-review skip"
	["e2e-smoke-test"]="E2E smoke test PR — review_autofix skips auto-merge"
)

_AI_PHASE_LABELS='["ai:done","ai:implementing","ai:awaiting-approval","ai:planning","ai:clarification","ai:validating","ai:validated","ai:validation-failed","ai:validation-fixing","ai:validation-recovery","ai:ready-to-merge","ai:needs-human","ai:blocked","ai:review-blocked","ai:implementation-failed","ai:merged","ai:closed"]'

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

set_issue_phase_label_resilient() {
	local issue_number="${1:-}"
	local target_label="${2:-}"
	local repo="${3:-${GITHUB_REPOSITORY:-}}"

	if [ -z "${issue_number}" ] || [ -z "${target_label}" ]; then
		echo "::warning::set_issue_phase_label_resilient: issue_number and target_label are required; skipping." >&2
		return 0
	fi
	if [ -z "${repo}" ]; then
		echo "::warning::set_issue_phase_label_resilient: repo required for issue #${issue_number}; skipping." >&2
		return 0
	fi

	ensure_label_exists "${target_label}" "${repo}" || true

	local _cur=""
	if ! _cur="$(gh_retry gh api --paginate "repos/${repo}/issues/${issue_number}/labels" \
		--jq '[.[].name]' 2>/dev/null | jq -cs 'add // []')"; then
		echo "::warning::set_issue_phase_label_resilient: GET labels failed for #${issue_number} — falling back to POST add." >&2
		if ! gh_retry gh api -X POST "repos/${repo}/issues/${issue_number}/labels" \
			-f "labels[]=${target_label}" >/dev/null 2>&1; then
			echo "::warning::set_issue_phase_label_resilient: POST fallback also failed for #${issue_number}." >&2
		fi
		return 0
	fi

	_cur="${_cur:-[]}"
	local _new=""
	if ! _new="$(printf '%s' "${_cur}" | jq -c --argjson p "${_AI_PHASE_LABELS}" --arg t "${target_label}" \
		'(. - $p) + [$t] | unique' 2>/dev/null)"; then
		echo "::warning::set_issue_phase_label_resilient: failed to compute new labels for #${issue_number} — falling back to POST add." >&2
		if ! gh_retry gh api -X POST "repos/${repo}/issues/${issue_number}/labels" \
			-f "labels[]=${target_label}" >/dev/null 2>&1; then
			echo "::warning::set_issue_phase_label_resilient: POST fallback also failed for #${issue_number}." >&2
		fi
		return 0
	fi

	if printf '{"labels":%s}' "${_new}" | \
		gh_retry gh api -X PUT "repos/${repo}/issues/${issue_number}/labels" \
			--input - >/dev/null 2>&1; then
		return 0
	fi

	echo "::warning::set_issue_phase_label_resilient: PUT labels failed for #${issue_number} — falling back to POST add." >&2
	if ! gh_retry gh api -X POST "repos/${repo}/issues/${issue_number}/labels" \
		-f "labels[]=${target_label}" >/dev/null 2>&1; then
		echo "::warning::set_issue_phase_label_resilient: POST fallback also failed for #${issue_number}." >&2
	fi
	return 0
}

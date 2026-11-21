#!/usr/bin/env bash
# review_autofix_step_det_skip_followup_guard.sh — security follow-up merge
# guard for the deterministic-skip path (#9671).
#
# Sourced (function library, not a step body) by the `deterministic-skip-merge`
# job's "Mark PR review-skipped, mark linked issues ready-to-merge, enable
# auto-merge" step in .github/workflows/review_autofix.yml, from the
# SHA-verified `.codex-freshness-src` support checkout, next to
# pr_checks_lib.sh. The step calls `det_skip_followup_merge_guard` right
# before each `gh pr merge ... --auto`, after the required-checks wait and the
# post-wait freshness re-check.
#
# A PR whose base is a standalone PR's head branch must not merge once that
# head has a clean single-issue security audit (#9479): the merge would move
# the clean head. The guard is `review_single_issue_security_pass.sh
# followup-guard`; this wrapper feeds it a payload built from the gate's
# outputs (no API call). PRs into the default or an integration branch get
# `allow` from the guard with no API call.
#
# Inputs (environment): PR_NUMBER, REPOSITORY, PR_BASE_REF, PR_HEAD_REF,
# PR_HEAD_SHA, DEFAULT_BRANCH, ORCH_INTEGRATION_BRANCH_PATTERN, GH_TOKEN,
# SINGLE_ISSUE_SECURITY_PASS_ENABLED (raw repository variable: only an
# explicit `false` turns the guard off; unset keeps it on),
# DET_SKIP_FOLLOWUP_GUARD_SUPPORT_DIR (default
# ${GITHUB_WORKSPACE}/.codex-freshness-src/scripts).
#
# Output: DET_SKIP_FOLLOWUP_GUARD_RESULT=allow|refuse|unverifiable|unavailable.
# Returns 0 only for `allow`. Anything else logs
# `AUTOFIX_DET_SKIP_MERGE_BOUND ... action=refuse
# reason=security_followup_guard_<result>` and returns 1, so the step leaves
# the PR open without merge labels. An empty PR_BASE_REF or DEFAULT_BRANCH
# fails closed as `unavailable`: the guard itself would answer `allow no_base`
# or `allow default_branch_unknown` for such a synthetic payload.

DET_SKIP_FOLLOWUP_GUARD_RESULT=""

det_skip_followup_merge_guard()
{
	local det_skip_fg_support="${DET_SKIP_FOLLOWUP_GUARD_SUPPORT_DIR:-${GITHUB_WORKSPACE:-.}/.codex-freshness-src/scripts}"
	local det_skip_fg_script det_skip_fg_file="" det_skip_fg_output=""
	det_skip_fg_script="${det_skip_fg_support}/review_single_issue_security_pass.sh"
	DET_SKIP_FOLLOWUP_GUARD_RESULT="unavailable"
	if [ -n "${PR_BASE_REF:-}" ] && [ -n "${DEFAULT_BRANCH:-}" ] && [ -f "${det_skip_fg_script}" ] \
		&& det_skip_fg_file="$(mktemp "${TMPDIR:-/tmp}/det_skip_followup_guard.XXXXXX" 2>/dev/null)"; then
		if jq -n \
			--arg base "${PR_BASE_REF}" \
			--arg default_branch "${DEFAULT_BRANCH}" \
			--arg head_ref "${PR_HEAD_REF:-}" \
			--arg head_sha "${PR_HEAD_SHA:-}" \
			--arg number "${PR_NUMBER:-}" \
			'{number: ($number | tonumber? // null), state: "open", base: {ref: $base, repo: {default_branch: $default_branch}}, head: {ref: $head_ref, sha: $head_sha}}' \
			> "${det_skip_fg_file}" 2>/dev/null; then
			det_skip_fg_output="$(REPOSITORY="${REPOSITORY:-}" PR_NUMBER="${PR_NUMBER:-}" PR_PAYLOAD_FILE="${det_skip_fg_file}" \
				DEFAULT_BRANCH="${DEFAULT_BRANCH}" SINGLE_ISSUE_SECURITY_PASS_ENABLED="${SINGLE_ISSUE_SECURITY_PASS_ENABLED:-}" \
				bash "${det_skip_fg_script}" followup-guard 2>/dev/null)" || det_skip_fg_output=""
			printf '%s\n' "${det_skip_fg_output}" | grep '^SINGLE_ISSUE_SECURITY_PASS ' || true
			DET_SKIP_FOLLOWUP_GUARD_RESULT="$(printf '%s\n' "${det_skip_fg_output}" | sed -n 's/^SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=//p' | tail -n 1)" || DET_SKIP_FOLLOWUP_GUARD_RESULT=""
			case "${DET_SKIP_FOLLOWUP_GUARD_RESULT}" in
				allow|refuse|unverifiable) ;;
				*) DET_SKIP_FOLLOWUP_GUARD_RESULT="unavailable" ;;
			esac
		fi
	fi
	if [ -n "${det_skip_fg_file}" ]; then
		rm -f "${det_skip_fg_file}"
	fi
	if [ "${DET_SKIP_FOLLOWUP_GUARD_RESULT}" = "allow" ]; then
		return 0
	fi
	echo "AUTOFIX_DET_SKIP_MERGE_BOUND pr=${PR_NUMBER:-} head_sha=${PR_HEAD_SHA:-unknown} action=refuse reason=security_followup_guard_${DET_SKIP_FOLLOWUP_GUARD_RESULT}"
	echo "::warning::Auto-merge not enabled for PR #${PR_NUMBER:-} on the deterministic-skip path: the security follow-up guard returned '${DET_SKIP_FOLLOWUP_GUARD_RESULT}' (its base branch's head passed a clean security audit, or that could not be verified). The PR stays open."
	return 1
}

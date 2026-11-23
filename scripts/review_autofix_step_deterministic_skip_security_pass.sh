#!/usr/bin/env bash
# Body of the "Single-issue security pass (deterministic skip)" step (id:
# det_skip_security_pass) in the deterministic-skip-merge job of
# .github/workflows/review_autofix.yml (#8333). Kept out of the workflow so
# the file stays under GitHub's 512,000-byte workflow limit. The step keeps
# its name:, id:, if: and env: in the workflow and sources this file in its
# own shell. The skip job runs no support staging: this file is loaded only
# from the SHA-verified .codex-freshness-src sparse checkout (pinned to
# review_support_sha), never from the PR-head checkout.
#
# A docs-only, small-diff or freshness_sync PR that is eligible for the
# single-issue security pass takes the deterministic skip when
# AUTOFIX_SKIP_RUNS_SECURITY_PASS=true. This step runs the existing pass
# (scripts/review_single_issue_security_pass.sh gate) for the gate-observed
# head before the job labels the PR or enables auto-merge:
#   hold=false  the merge step runs (clean audit marker for the head, or the
#               PR is not eligible / is a verified follow-up). This step then
#               posts the ai-review/head-gate success for the head; the gate
#               withholds it while the skip-path pass is required.
#   hold=true   the merge step is skipped. The audit the pass dispatched
#               re-runs review when it reports.
#   exhausted   hold=true, and the PR's review workflow is dispatched once
#               (review_freshness_ci_failed.sh's candidate order). The pass
#               already labelled the PR ai:security-pass-failed, so the gate
#               keeps that run on codex-agent, whose review-blocked judge
#               runs in security-exhaustion mode. This step posts no marker
#               comment of its own; the pass still posts its exhaustion
#               comment when it applies the label.
#
# Env: PR_NUMBER, PR_HEAD_SHA (gate head), REPOSITORY, DEFAULT_BRANCH,
# DET_SKIP_SECURITY_PASS_PR_JSON (gate PR snapshot), the pass's own env
# block, REVIEW_AUTOFIX_CALLER_WORKFLOW_REF, ALLOW_WORKFLOW_EDITS,
# REVIEW_HEAD_GATE_STATUS_ENABLED (default true), RUN_URL.
# DET_SKIP_SECURITY_PASS_SUPPORT_DIR (default
# ${GITHUB_WORKSPACE}/.codex-freshness-src/scripts),
# DET_SKIP_SECURITY_PASS_CHECKOUT (default ${GITHUB_WORKSPACE}/.codex-skip-pr-head),
# DET_SKIP_SECURITY_PASS_RETRY_SLEEP_SECS (default 2, 0-30): wait between
# comment-read attempts.
# Outputs: hold=true|false, hold_reason=<reason> (hold=true only),
# exhausted=true|false. Any unverifiable input writes hold=true and fails
# the step, so nothing merges.
# API budget (§14): one paginated comments read (up to 3 attempts), one
# GraphQL closingIssuesReferences read, then whatever the pass does; on
# exhaustion, one `gh workflow run` per candidate until one succeeds; on
# hold=false, one commit-status POST (ai-review/head-gate success).
# Log: AUTOFIX_DET_SKIP_SECURITY_PASS pr= head= outcome=clean|hold|exhausted|skip_input_unverifiable hold_reason= redispatch=none|dispatched|dispatch_failed [workflow=]
set -euo pipefail

det_skip_security_pass_log()
{
	echo "AUTOFIX_DET_SKIP_SECURITY_PASS pr=${PR_NUMBER:-?} head=${PR_HEAD_SHA:-?} $*"
}

# Writes key=value lines to the step's GITHUB_OUTPUT; returns 1 on failure.
det_skip_security_pass_output()
{
	if [ -z "${GITHUB_OUTPUT:-}" ] || ! printf '%s\n' "$@" >> "${GITHUB_OUTPUT}"; then
		echo "::error::Could not record the deterministic-skip security-pass decision; failing closed." >&2
		return 1
	fi
	return 0
}

det_skip_security_pass_summary()
{
	if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
		# The backticks are Markdown code spans, not command substitution.
		# shellcheck disable=SC2016
		printf '### Single-issue security pass (deterministic skip)\n\n- PR: #%s\n- Head: `%s`\n- Outcome: `%s`\n- Hold reason: `%s`\n- Review re-dispatch: `%s`\n' \
			"${PR_NUMBER:-?}" "${PR_HEAD_SHA:-?}" "$1" "$2" "$3" >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
	fi
}

# On hold=false, publish the ai-review/head-gate success the gate withheld
# for this head (#8333). Best effort: a missing helper or failed POST only
# warns, and the status stays pending.
det_skip_security_pass_post_head_status()
{
	local support_dir="$1"
	if [ ! -f "${support_dir}/review_head_gate.sh" ] || [ ! -f "${support_dir}/gh_helpers.sh" ]; then
		echo "::warning::review_head_gate.sh unavailable; leaving the ai-review/head-gate status pending for PR #${PR_NUMBER:-?}."
		return 0
	fi
	# cd keeps gh_helpers.sh's relative scripts/ fallback inside the verified
	# support directory.
	# shellcheck source=/dev/null
	(
		cd "${support_dir}" \
			&& source "${support_dir}/gh_helpers.sh" \
			&& source "${support_dir}/review_head_gate.sh" \
			&& review_head_gate_post_status "${REPOSITORY}" "${PR_HEAD_SHA}" success "deterministic skip and security pass passed"
	) || echo "::warning::Could not publish the ai-review/head-gate status for PR #${PR_NUMBER:-?}; it stays pending."
	return 0
}

det_skip_security_pass_unverifiable()
{
	echo "::error::Deterministic-skip security pass input unverifiable for PR #${PR_NUMBER:-?} ($1); holding the merge."
	det_skip_security_pass_output "hold=true" "hold_reason=skip_input_unverifiable" "exhausted=false" || true
	det_skip_security_pass_log "outcome=skip_input_unverifiable hold_reason=skip_input_unverifiable redispatch=none detail=$1"
	det_skip_security_pass_summary skip_input_unverifiable skip_input_unverifiable none
	return 1
}

det_skip_security_pass_main()
{
	local support_dir="${DET_SKIP_SECURITY_PASS_SUPPORT_DIR:-${GITHUB_WORKSPACE:-.}/.codex-freshness-src/scripts}"
	local checkout="${DET_SKIP_SECURITY_PASS_CHECKOUT:-${GITHUB_WORKSPACE:-.}/.codex-skip-pr-head}"
	local retry_sleep="${DET_SKIP_SECURITY_PASS_RETRY_SLEEP_SECS:-2}"
	local work pr_file comments_file pass_output linked_json checkout_head attempt
	local pass_rc=0 hold hold_reason exhausted outcome redispatch="none" workflow=""

	if ! [[ "${PR_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${PR_HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${REPOSITORY}" == *..* ]]; then
		det_skip_security_pass_unverifiable invalid_input
		return 1
	fi
	if ! support_dir="$(cd "${support_dir}" 2>/dev/null && pwd -P)" \
		|| [ ! -f "${support_dir}/review_single_issue_security_pass.sh" ]; then
		det_skip_security_pass_unverifiable support_missing
		return 1
	fi
	if ! checkout="$(cd "${checkout}" 2>/dev/null && pwd -P)"; then
		det_skip_security_pass_unverifiable checkout_missing
		return 1
	fi
	checkout_head="$(git -C "${checkout}" rev-parse HEAD 2>/dev/null || true)"
	if [ "${checkout_head}" != "${PR_HEAD_SHA}" ]; then
		det_skip_security_pass_unverifiable checkout_head_mismatch
		return 1
	fi
	if ! work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/det_skip_security_pass.XXXXXX" 2>/dev/null)"; then
		det_skip_security_pass_unverifiable tempdir_failed
		return 1
	fi
	# Removed on every exit path, including the early unverifiable returns.
	DET_SKIP_SECURITY_PASS_WORK_DIR="${work}"
	trap 'rm -rf "${DET_SKIP_SECURITY_PASS_WORK_DIR:-}"' EXIT
	pr_file="${work}/pr.json"
	comments_file="${work}/comments.json"
	pass_output="${work}/pass_output"
	# The gate built this snapshot from its own /pulls/{n} read; it must
	# describe the head this job audits and would merge.
	if ! printf '%s' "${DET_SKIP_SECURITY_PASS_PR_JSON:-}" > "${pr_file}" \
		|| ! jq -e --arg sha "${PR_HEAD_SHA}" 'type == "object" and (.head.sha // "") == $sha' "${pr_file}" >/dev/null 2>&1; then
		det_skip_security_pass_unverifiable pr_payload_mismatch
		return 1
	fi

	# Fresh comment read in the REST shape the pass reads (.user.login,
	# .created_at, .id); the gate's projected cache lives in another job.
	# A failed read leaves the file empty: the pass holds markers_unverifiable.
	{ [[ "${retry_sleep}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${retry_sleep}))" -le 30 ]; } || retry_sleep=2
	for attempt in 1 2 3; do
		if gh api --paginate "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments?per_page=100" 2>/dev/null \
			| jq -s 'add // []' > "${comments_file}" 2>/dev/null \
			&& jq -e 'type == "array"' "${comments_file}" >/dev/null 2>&1; then
			break
		fi
		: > "${comments_file}"
		if [ "${attempt}" -lt 3 ]; then
			sleep "$((10#${retry_sleep}))"
		fi
	done

	# Same closingIssuesReferences query as the merge step. A failed read
	# gives [], which keeps the audit required (no verified follow-up skip).
	# $owner/$name/$number are GraphQL variables, not shell expansions.
	# shellcheck disable=SC2016
	linked_json="$(gh api graphql \
		-f owner="${REPOSITORY%/*}" \
		-f name="${REPOSITORY#*/}" \
		-F number="${PR_NUMBER}" \
		-f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
		--jq '.data.repository.pullRequest.closingIssuesReferences.nodes // [] | map({number: .number})' \
		2>/dev/null || true)"
	if ! jq -e 'type == "array"' <<< "${linked_json:-null}" >/dev/null 2>&1; then
		linked_json="[]"
	fi

	# The pass reads the PR-head checkout through git plumbing only; support
	# code comes from the verified checkout. Its `python3 -c` calls run with
	# the PR head as cwd and GH_TOKEN set, so PYTHONSAFEPATH=1 keeps a
	# PR-supplied module (e.g. a root datetime.py) off sys.path. A Python
	# that ignores it fails closed, as in codex-agent.
	if ! PYTHONSAFEPATH=1 python3 -c 'import sys; raise SystemExit(0 if sys.flags.safe_path else 1)' >/dev/null 2>&1; then
		det_skip_security_pass_unverifiable python_safe_path_unavailable
		return 1
	fi
	: > "${pass_output}"
	(
		cd "${checkout}" \
			&& PYTHONSAFEPATH=1 \
			PYTHONDONTWRITEBYTECODE=1 \
			SUPPORT_SCRIPTS_DIR="${support_dir}" \
			PR_PAYLOAD_FILE="${pr_file}" \
			PR_ISSUE_COMMENTS_FILE="${comments_file}" \
			LINKED_ISSUES_JSON="${linked_json}" \
			GITHUB_OUTPUT="${pass_output}" \
			bash "${support_dir}/review_single_issue_security_pass.sh" gate
	) || pass_rc=$?

	hold="$(sed -n 's/^hold=//p' "${pass_output}" 2>/dev/null | tail -n 1)"
	hold_reason="$(sed -n 's/^hold_reason=//p' "${pass_output}" 2>/dev/null | tail -n 1)"
	exhausted="$(sed -n 's/^exhausted=//p' "${pass_output}" 2>/dev/null | tail -n 1)"
	[[ "${hold_reason}" =~ ^[a-z_]+$ ]] || hold_reason=""
	[ "${exhausted}" = "true" ] || exhausted="false"
	if ! [[ "${hold}" =~ ^(true|false)$ ]]; then
		hold="true"
		hold_reason="${hold_reason:-result_unverifiable}"
	fi
	if [ "${pass_rc}" -ne 0 ] || [ "${exhausted}" = "true" ]; then
		hold="true"
	fi
	if [ "${hold}" = "true" ]; then
		if ! det_skip_security_pass_output "hold=true" "hold_reason=${hold_reason:-unknown}" "exhausted=${exhausted}"; then
			return 1
		fi
	elif ! det_skip_security_pass_output "hold=false" "exhausted=false"; then
		return 1
	fi

	if [ "${exhausted}" = "true" ]; then
		outcome="exhausted"
		# Sourced in a subshell: only the dispatch function is needed.
		if [ -f "${support_dir}/review_freshness_ci_failed.sh" ] \
			&& workflow="$(
				# shellcheck source=/dev/null
				source "${support_dir}/review_freshness_ci_failed.sh" \
					&& freshness_ci_failed_dispatch_review_workflow
			)" && [ -n "${workflow}" ]; then
			redispatch="dispatched"
		else
			redispatch="dispatch_failed"
			workflow=""
			echo "::warning::Could not dispatch a review workflow for PR #${PR_NUMBER} after the security pass was exhausted; the PR stays held (ai:security-pass-failed) until the next review event."
		fi
	elif [ "${hold}" = "true" ]; then
		outcome="hold"
	else
		outcome="clean"
		det_skip_security_pass_post_head_status "${support_dir}"
	fi
	det_skip_security_pass_log "outcome=${outcome} hold_reason=${hold_reason:-none} redispatch=${redispatch}${workflow:+ workflow=${workflow}}"
	det_skip_security_pass_summary "${outcome}" "${hold_reason:-none}" "${redispatch}"
	if [ "${pass_rc}" -ne 0 ]; then
		echo "::error::The single-issue security pass exited ${pass_rc} for PR #${PR_NUMBER}; the merge stays held."
		return "${pass_rc}"
	fi
	return 0
}

det_skip_security_pass_rc=0
det_skip_security_pass_main || det_skip_security_pass_rc=$?
exit "${det_skip_security_pass_rc}"

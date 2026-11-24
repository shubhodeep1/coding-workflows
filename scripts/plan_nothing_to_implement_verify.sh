#!/usr/bin/env bash
# Verify a "nothing to implement" planning block and close the issue when the
# listed tests pass on the planning ref (issue #10015, Phase 8a of
# docs/plans/pipeline-human-wait-elimination-plan.md, Refs #9650).
#
# Step body of plan.yml "Verify nothing-to-implement block"
# (id: nothing_to_implement_verify). The step runs this file from the trusted
# workflow-source checkout (.codex-workflow-src/scripts/), never from the
# issue checkout, so its siblings (acceptance_check.py, heal_reproduce.sh,
# prepush_test_gate_run.sh, codex_isolated_exec.sh, gh_helpers.sh) are the
# same trusted SCRIPT_REF copies.
#
# Flow (always exits 0; any failure keeps today's blocked comment):
#   1. Only the exact planner reason
#      "work is already on the planning ref; nothing to implement" is handled
#      (same literal as UNBLOCK_NOTHING_TO_IMPLEMENT_REASON in
#      scripts/unblock_judge.sh). Any other reason: no output, no log.
#   2. PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED off: outcome=skipped.
#   3. acceptance_check.py verify-targets picks the tests: the plan's
#      ACCEPTANCE test lines, else tests/ paths in the issue body.
#   4. heal_reproduce.sh runs them in the credential-free gate container
#      with the planning HEAD as both tested commit and baseline.
#   5. Every listed test must be not_reproduced/passes_at_failing_sha.
#   6. On pass: marker comment, close as completed, then the label swap (as
#      implement.yml Guard 0), delete the progress comment. No Telegram page.
#
# Env (each defaulted here):
#   PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED  default true; 1/true/yes/on = on
#   ISSUE_NUMBER, BLOCKED_REASON, CODEX_OUTPUT_FILE, ISSUE_META_FILE  inputs
#   REPOSITORY                  default ${GITHUB_REPOSITORY}
#   REPO_DIR                    planning checkout, default ${GITHUB_WORKSPACE:-$PWD}
#   RUN_URL                     run link (default empty)
#   PLAN_PROGRESS_COMMENT_ID    progress comment to delete (default empty)
#   GITHUB_OUTPUT               step outputs (default /dev/null)
#   PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ACCEPTANCE_HELPER  default acceptance_check.py next to this script
#   PLAN_NOTHING_TO_IMPLEMENT_VERIFY_REPRODUCE_SCRIPT   default heal_reproduce.sh next to this script
#
# Outputs: outcome=closed|blocked|skipped, note=<one line, at most 300 chars>.
# Log (once per matching run):
#   PLAN_NOTHING_TO_IMPLEMENT issue=<n> outcome=closed|blocked|skipped
#     reason=passed|no_tests|failed|timeout|disabled tests=<k>[ detail=<[a-z_]+>]
set -uo pipefail

PNTI_REASON_LITERAL="work is already on the planning ref; nothing to implement"
pnti_self="$(realpath -e "${BASH_SOURCE[0]}" 2>/dev/null)" || pnti_self="${BASH_SOURCE[0]}"
pnti_dir="$(dirname "${pnti_self}")"

pnti_issue="${ISSUE_NUMBER:-}"
pnti_reason="${BLOCKED_REASON:-}"
pnti_repo="${REPOSITORY:-${GITHUB_REPOSITORY:-}}"
pnti_repo_dir="${REPO_DIR:-${GITHUB_WORKSPACE:-$PWD}}"
pnti_run_url="${RUN_URL:-}"
pnti_progress_id="${PLAN_PROGRESS_COMMENT_ID:-}"
pnti_output="${GITHUB_OUTPUT:-/dev/null}"
pnti_helper="${PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ACCEPTANCE_HELPER:-${pnti_dir}/acceptance_check.py}"
pnti_reproduce="${PLAN_NOTHING_TO_IMPLEMENT_VERIFY_REPRODUCE_SCRIPT:-${pnti_dir}/heal_reproduce.sh}"
pnti_tmp_base="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
pnti_work=""

# 1. Exact reason only (trailing whitespace and CR removed).
pnti_reason="${pnti_reason//$'\r'/}"
pnti_reason="${pnti_reason%"${pnti_reason##*[![:space:]]}"}"
if [ "${pnti_reason}" != "${PNTI_REASON_LITERAL}" ]; then
	exit 0
fi

if ! [[ "${pnti_issue}" =~ ^[1-9][0-9]{0,9}$ ]]; then
	pnti_issue="0"
fi

pnti_cleanup()
{
	if [ -n "${pnti_work}" ] && [ -d "${pnti_work}" ]; then
		rm -rf "${pnti_work}"
	fi
}
trap pnti_cleanup EXIT

# Single-line, control-character-free output value.
pnti_one_line()
{
	printf '%s' "$1" | tr -d '\000-\037\177' | cut -c1-300
}

pnti_finish()
{
	local outcome="$1" reason="$2" count="$3" detail="${4:-}" note="${5:-}"
	local line="PLAN_NOTHING_TO_IMPLEMENT issue=${pnti_issue} outcome=${outcome} reason=${reason} tests=${count}"
	detail="$(printf '%s' "${detail}" | tr -cd 'a-z_')"
	if [ -n "${detail}" ]; then
		line+=" detail=${detail}"
	fi
	echo "${line}"
	{
		printf 'outcome=%s\n' "${outcome}"
		printf 'note=%s\n' "$(pnti_one_line "${note}")"
	} >> "${pnti_output}" 2>/dev/null || true
	exit 0
}

# 2. Switch (parsed like PLAN_SELF_CHECK_ENABLED: 1/true/yes/on = on).
case "$(printf '%s' "${PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
	""|1|true|yes|on) ;;
	*) pnti_finish skipped disabled 0 ;;
esac

pnti_note_failed()
{
	printf 'Verify: %s listed test(s) did not all pass on the planning ref (%s); the block stays.' "$1" "$2"
}
PNTI_NOTE_NO_TESTS="Verify: no tests found in the plan's ACCEPTANCE lines or the issue body; the block stays."

pnti_work="$(mktemp -d "${pnti_tmp_base}/plan-nti-verify.XXXXXX" 2>/dev/null)" || pnti_work=""
if [ -z "${pnti_work}" ] || [ ! -d "${pnti_work}" ]; then
	pnti_finish blocked no_tests 0 tmp_unavailable "${PNTI_NOTE_NO_TESTS}"
fi

# 3. Targets.
pnti_targets_json="${pnti_work}/targets.json"
pnti_tests_file="${pnti_work}/tests.txt"
pnti_issue_json="${ISSUE_META_FILE:-}"
if [ -z "${pnti_issue_json}" ] || [ ! -f "${pnti_issue_json}" ]; then
	pnti_issue_json="${pnti_work}/issue_empty.json"
	printf '{}\n' > "${pnti_issue_json}"
fi
pnti_plan_file="${CODEX_OUTPUT_FILE:-}"
if [ -z "${pnti_plan_file}" ] || [ ! -f "${pnti_plan_file}" ]; then
	pnti_plan_file="${pnti_work}/plan_empty.txt"
	: > "${pnti_plan_file}"
fi
if [ ! -f "${pnti_helper}" ] || [ -L "${pnti_helper}" ]; then
	pnti_finish blocked no_tests 0 helper_absent "${PNTI_NOTE_NO_TESTS}"
fi
if ! PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${pnti_helper}" verify-targets --plan-file "${pnti_plan_file}" \
	--issue-json "${pnti_issue_json}" --repo-root "${pnti_repo_dir}" --out "${pnti_targets_json}" >/dev/null 2>&1; then
	pnti_finish blocked no_tests 0 helper_absent "${PNTI_NOTE_NO_TESTS}"
fi
if ! jq -r '.tests[]? | select(type == "string")' "${pnti_targets_json}" > "${pnti_tests_file}" 2>/dev/null; then
	: > "${pnti_tests_file}"
fi
pnti_count="$(grep -c . "${pnti_tests_file}" 2>/dev/null)" || pnti_count=0
[[ "${pnti_count}" =~ ^[0-9]+$ ]] || pnti_count=0
if [ "${pnti_count}" -eq 0 ]; then
	pnti_finish blocked no_tests 0 "" "${PNTI_NOTE_NO_TESTS}"
fi

# 4. Run the tests: planning HEAD as tested commit and baseline.
pnti_head="$(git -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "${pnti_repo_dir}" rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null)" || pnti_head=""
if ! [[ "${pnti_head}" =~ ^[0-9a-f]{40}$ ]]; then
	pnti_finish blocked failed "${pnti_count}" head_unavailable "$(pnti_note_failed "${pnti_count}" failed)"
fi
if [ ! -f "${pnti_reproduce}" ] || [ -L "${pnti_reproduce}" ]; then
	pnti_finish blocked failed "${pnti_count}" support_missing "$(pnti_note_failed "${pnti_count}" failed)"
fi
pnti_repro_json="${pnti_work}/repro.json"
WORKFLOW_HEAL_REPRODUCE_TIMEOUT_SECS=600 timeout --kill-after=30s 900 \
	bash "${pnti_reproduce}" --tests-file "${pnti_tests_file}" --sha "${pnti_head}" --baseline "${pnti_head}" \
	--repo "${pnti_repo_dir}" --out "${pnti_repro_json}" >/dev/null 2>&1
pnti_rc=$?

# 5. Decide.
if [ "${pnti_rc}" -eq 124 ] || [ "${pnti_rc}" -eq 137 ]; then
	pnti_finish blocked timeout "${pnti_count}" "" "$(pnti_note_failed "${pnti_count}" timeout)"
fi
pnti_verdict="$(jq -r --argjson want "${pnti_count}" '
	if (type != "object") or (.schema != "heal_repro.v1") then "failed:invalid_result"
	elif ((.reason // "") == "timeout") or any(.tests[]?; (.reason // "") == "timeout") then "timeout:"
	elif ((.tests | type) != "array") or ((.tests | length) == 0) then "failed:" + ((.reason // "no_tests") | tostring)
	elif (.tests | length) != $want then "failed:count_mismatch"
	elif all(.tests[]; (.status == "not_reproduced") and (.reason == "passes_at_failing_sha")) then "passed:"
	elif any(.tests[]; .status == "unverified") then "failed:" + ([.tests[] | select(.status == "unverified") | (.reason // "unverified") | tostring][0])
	else "failed:"
	end' "${pnti_repro_json}" 2>/dev/null)" || pnti_verdict="failed:invalid_result"
[ -n "${pnti_verdict}" ] || pnti_verdict="failed:invalid_result"
pnti_kind="${pnti_verdict%%:*}"
pnti_detail="$(printf '%s' "${pnti_verdict#*:}" | tr -cd 'a-z_' | cut -c1-64)"
case "${pnti_kind}" in
	passed) ;;
	timeout) pnti_finish blocked timeout "${pnti_count}" "${pnti_detail}" "$(pnti_note_failed "${pnti_count}" timeout)" ;;
	*) pnti_finish blocked failed "${pnti_count}" "${pnti_detail}" "$(pnti_note_failed "${pnti_count}" failed)" ;;
esac

# 6. Close on pass: comment, close, labels.
pnti_note_failed_pass="Verify: ${pnti_count} test(s) passed on the planning ref, but the issue could not be closed automatically; the block stays."
if [ -z "${pnti_repo}" ] || [ "${pnti_issue}" = "0" ]; then
	pnti_finish blocked passed "${pnti_count}" comment_failed "${pnti_note_failed_pass}"
fi
# shellcheck source=/dev/null
source "${pnti_dir}/gh_helpers.sh" >/dev/null 2>&1 || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

pnti_list=""
while IFS= read -r pnti_test || [ -n "${pnti_test}" ]; do
	pnti_test="$(printf '%s' "${pnti_test}" | tr -d '`\000-\037\177' | cut -c1-300)"
	[ -n "${pnti_test}" ] || continue
	pnti_list+="- \`${pnti_test}\`"$'\n'
done < "${pnti_tests_file}"
pnti_body="$(printf '%s\n' \
	'✅ **Already implemented — verified.**' \
	'' \
	'Planning found the requested work already present on the planning ref, and the listed tests passed there:' \
	'' \
	"${pnti_list%$'\n'}" \
	'' \
	"Run: ${pnti_run_url}" \
	'' \
	"<!-- ai:implement-success-noop:v1 issue=${pnti_issue} -->")"
if ! gh_retry gh api "repos/${pnti_repo}/issues/${pnti_issue}/comments" -f body="${pnti_body}" >/dev/null 2>&1; then
	pnti_finish blocked passed "${pnti_count}" comment_failed "${pnti_note_failed_pass}"
fi

# Close before labelling (as scripts/check_failure_triage.sh does): a failed
# close leaves the labels untouched, so an open issue never carries the
# terminal ai:closed label (orchestrate_lib.py checks ai:closed before phase
# labels and would read the still-blocked issue as closed).
if ! gh_retry gh issue close "${pnti_issue}" --repo "${pnti_repo}" --reason completed >/dev/null 2>&1; then
	# The marker comment exists; the Phase 8b judge closes the issue from it.
	pnti_finish blocked passed "${pnti_count}" close_failed "${pnti_note_failed_pass}"
fi

gh_retry gh label create 'ai:closed' --repo "${pnti_repo}" \
	--color '6b7280' --description 'Issue closed by automation (superseded, duplicated, or already complete)' \
	>/dev/null 2>&1 || true
# The issue is already closed (terminal for the orchestrator); a failed label
# swap is only a warning.
gh_retry gh issue edit "${pnti_issue}" --repo "${pnti_repo}" \
	--remove-label 'ai:done' --remove-label 'ai:implementing' \
	--remove-label 'ai:planning' --remove-label 'ai:clarification' \
	--remove-label 'ai:awaiting-approval' --remove-label 'ai:ready-to-merge' \
	--remove-label 'ai:implementation-failed' --remove-label 'ai:blocked' \
	--add-label 'ai:closed' >/dev/null 2>&1 || \
gh_retry gh issue edit "${pnti_issue}" --repo "${pnti_repo}" \
	--add-label 'ai:closed' >/dev/null 2>&1 || \
echo "::warning::issue #${pnti_issue} is closed but its labels could not be swapped to ai:closed"

if [[ "${pnti_progress_id}" =~ ^[0-9]+$ ]]; then
	gh_retry gh api -X DELETE "repos/${pnti_repo}/issues/comments/${pnti_progress_id}" >/dev/null 2>&1 || true
fi

pnti_finish closed passed "${pnti_count}" "" "Verify: ${pnti_count} test(s) passed on the planning ref; issue closed as completed."

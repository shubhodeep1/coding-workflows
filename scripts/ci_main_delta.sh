#!/usr/bin/env bash
# Delta suite for a push to main (issue #10156; owner decision Q201 A).
#
# A push to main runs static-checks, ci.yml's tests-main-delta job (this
# script) and `lint`; the seven gated test jobs are skipped. The full suite
# runs on main from ci.yml's `schedule` trigger every 6 hours, and in the same
# push run whenever this script cannot vouch for its selection.
#
#   ci_main_delta.sh select
#       Run by tests-main-delta (step id main_delta_select) on a full-history
#       checkout of the pushed commit. Makes one API read,
#       `actions/workflows/ci.yml/runs?branch=<branch>&status=completed&per_page=30`,
#       keeps the push and schedule runs of .github/workflows/ci.yml on the
#       branch (cancelled and skipped runs report nothing and are dropped) and
#       takes the head of the one that finished last as the base when it
#       succeeded and no red run outranks it (rule below). So the
#       range covers every push since main's last green run, including a push
#       whose waiting run `ci-push-main` replaced. Then
#       `git diff --name-status -M <base> <head>` feeds
#       workflow-templates/.claude/hooks/prepush_affected_tests.py select, and
#       the selected test files plus the CI-structure tests
#       (CI_SYNC_QUICK_WIRING_PATHS in scripts/ci_sync_quick.sh) are written
#       to the list file. Writes `full_required=false` to GITHUB_OUTPUT then.
#       Whenever the selection cannot be trusted it writes
#       `full_required=true` instead and exits 0, so the seven gated jobs run
#       in this run (their `if:` reads the job output); reasons:
#       invalid_input, head_mismatch, read_failed, invalid_response,
#       no_green_base, base_red (the run that finished last failed, or a red
#       run has no green run started after it finished), base_not_ancestor, diff_failed,
#       selector_failed, selection_invalid (also a missing or null
#       `affected_tests`), over_cap, sharded_test_selected,
#       wiring_list_missing (the CI-structure list could not be loaded),
#       list_write_failed. It never counts an unknown selection as passed.
#       The base is the run that finished last (`updated_at`, then
#       `created_at`): push and scheduled runs overlap, so a later-created
#       delta push that finished first cannot hide a red scheduled run. A red
#       run also stays the result until a green run started after it finished
#       (`run_started_at`, else `created_at`, >= its `updated_at`; a re-run,
#       `run_attempt` > 1, counts from `created_at`, because a partial re-run
#       gets a new start time but reuses the first attempt's delta selection
#       and never runs the full suite): a delta push
#       that chose its base before the red run finished cannot vouch for the
#       tree it found broken, while any run started later (including a push
#       queued behind it in `ci-push-<ref>`) saw base_red and ran the full
#       suite. A neutral or empty conclusion never keeps an earlier red
#       base red, and is never a green base itself. The
#       runs read is tried up to 3 times before read_failed; the last failure
#       logs a category (auth, not_found or other), never gh's stderr.
#       A failed GITHUB_OUTPUT write fails the step (exit 1), so `lint` never
#       reads a missing output as "delta sufficient".
#   ci_main_delta.sh run-tests
#       Step id main_delta_run, only after `select` wrote full_required=false.
#       Re-reads and re-checks the list, then runs, under one shared deadline
#       (CI_MAIN_DELTA_TEST_TIMEOUT_SECS, default 600 = 10 minutes, so the
#       timeout fallback stays under budget-watch's 15-minute warning for the
#       20-minute job):
#         - files ci.yml itself runs as `python3 <file>` the same way;
#         - the other tests/test_*.py files in one pytest call (pytest exit 5,
#           nothing collected, counts as a pass);
#         - tests/test_*.sh files with bash.
#       A tests/ file that is neither a test_* file nor run by ci.yml is a
#       helper and is skipped (the tests that import it are selected on
#       their own). The selector's `functions` narrowing is ignored, so whole
#       files run. Timeout (exit 124): writes full_required=true and exits 0
#       (reason tests_timed_out). A failure exits 1. A pass exits 0. Nothing
#       from the selector JSON is ever executed as a command.
#
# Env (every one has a default):
#   GH_TOKEN                        job token (actions: read), for `select`.
#   CI_MAIN_DELTA_REPOSITORY        owner/repo (default GITHUB_REPOSITORY).
#   CI_MAIN_DELTA_HEAD_SHA          the pushed commit (default GITHUB_SHA).
#   CI_MAIN_DELTA_BRANCH            default main.
#   MAIN_PUSH_DELTA_MAX_TEST_FILES  selected test files above which the full
#                                   suite runs (default 60, 1-10000).
#   CI_MAIN_DELTA_TESTS_FILE        list file (default
#                                   ${RUNNER_TEMP:-/tmp}/ci-main-delta-tests.txt).
#   CI_MAIN_DELTA_TEST_TIMEOUT_SECS run-tests deadline (default 600, 60-1080).
#   GITHUB_OUTPUT                   step outputs (default /dev/null).
#
# Log (stable prefix CI_MAIN_DELTA; one terminal line per subcommand):
#   CI_MAIN_DELTA mode=select|run_tests outcome=ok|full_required|fail reason=<token> base=<sha12|none> head=<sha12|none> changed=<n> tests=<n>
#   CI_MAIN_DELTA mode=output outcome=fail reason=output_write_failed base=... head=... changed=<n> tests=<n>
#     (a failed GITHUB_OUTPUT write; the step exits 1)
# Notes (not terminal): `mode=select outcome=note reason=cap_invalid_default cap=60`,
#   `mode=select outcome=note reason=read_failed_category category=auth|not_found|other`,
#   `mode=run_tests outcome=start reason=selected script=<n> pytest=<n> shell=<n>`.
# Logs hold SHAs, counts and reason tokens only.

set -uo pipefail

CI_MAIN_DELTA_SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CI_MAIN_DELTA_WORKFLOW_PATH=".github/workflows/ci.yml"
CI_MAIN_DELTA_SELECTOR="workflow-templates/.claude/hooks/prepush_affected_tests.py"
# Test files ci.yml splits across matrix legs, or across local shards as a
# single module (heal_module, mt_module): one serial run cannot fit them in
# the delta deadline, so selecting one runs the full suite at once instead of
# timing out first (tests/test_ci_poll_test_sharding.py pins the
# orchestrate-poll split). Group-sharded module sets (pc, wg1, wg2, ub) are
# split for their combined time, not per module, and stay runnable here.
CI_MAIN_DELTA_SHARDED_TESTS=(
	"tests/test_orchestrate_poll_process.py"
	"tests/test_workflow_failure_heal.py"
	"tests/test_review_merge_train.py"
)

# Same list as tests-sync-quick: the tests that pin ci.yml's structure. The
# file only defines functions and the array when sourced (BASH_SOURCE guard).
CI_SYNC_QUICK_WIRING_PATHS=()
# shellcheck source=ci_sync_quick.sh
source "${CI_MAIN_DELTA_SCRIPT_DIR}/ci_sync_quick.sh" >/dev/null 2>&1 || true

CI_MAIN_DELTA_BASE="none"
CI_MAIN_DELTA_HEAD="none"
CI_MAIN_DELTA_CHANGED=0
CI_MAIN_DELTA_TESTS=0

# A failed write exits the script with 1: the step fails, tests-main-delta
# fails and `lint` rejects the run. Returning success instead would leave the
# job output at its "false" rendering, which `lint` accepts as delta passed.
ci_main_delta_output()
{
	if ! printf '%s=%s\n' "$1" "$2" >> "${GITHUB_OUTPUT:-/dev/null}" 2>/dev/null; then
		echo "CI_MAIN_DELTA mode=output outcome=fail reason=output_write_failed base=${CI_MAIN_DELTA_BASE:0:12} head=${CI_MAIN_DELTA_HEAD:0:12} changed=${CI_MAIN_DELTA_CHANGED} tests=${CI_MAIN_DELTA_TESTS}"
		echo "::error::Could not write the ${1} step output; failing the delta job."
		exit 1
	fi
}

ci_main_delta_log()
{
	local log_mode="$1" log_outcome="$2" log_reason="$3"
	echo "CI_MAIN_DELTA mode=${log_mode} outcome=${log_outcome} reason=${log_reason} base=${CI_MAIN_DELTA_BASE:0:12} head=${CI_MAIN_DELTA_HEAD:0:12} changed=${CI_MAIN_DELTA_CHANGED} tests=${CI_MAIN_DELTA_TESTS}"
}

# `ci_main_delta_full <mode> <reason>`: the selection cannot be trusted, so the
# full suite runs in this run. Exits the step green; `lint` then requires
# every gated job to succeed.
ci_main_delta_full()
{
	ci_main_delta_output full_required true
	ci_main_delta_log "$1" full_required "$2"
	echo "::notice::Delta suite not used ($2); the full test suite runs in this run."
	return 0
}

ci_main_delta_tests_file()
{
	printf '%s' "${CI_MAIN_DELTA_TESTS_FILE:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ci-main-delta-tests.txt}"
}

# A selected path is usable only as a regular file under tests/ with a .py or
# .sh suffix and no `..` segment.
ci_main_delta_path_ok()
{
	local candidate="$1"
	[[ "${candidate}" =~ ^tests/[A-Za-z0-9_./-]+\.(py|sh)$ ]] || return 1
	[[ "${candidate}" == *..* ]] && return 1
	return 0
}

ci_main_delta_is_sharded()
{
	local candidate="$1" listed
	for listed in "${CI_MAIN_DELTA_SHARDED_TESTS[@]}"; do
		[ "${candidate}" = "${listed}" ] && return 0
	done
	return 1
}

ci_main_delta_select()
{
	local repo="${CI_MAIN_DELTA_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
	local head_sha="${CI_MAIN_DELTA_HEAD_SHA:-${GITHUB_SHA:-}}"
	local branch="${CI_MAIN_DELTA_BRANCH:-main}"
	local cap="${MAIN_PUSH_DELTA_MAX_TEST_FILES:-60}"
	local runs="" verdict="" verdict_kind="" base_sha="" work_dir="" local_head=""
	local changed_file selection_file selected_file list_file selected_path
	local -a selected=()
	local -A seen=()

	if ! [[ "${cap}" =~ ^[0-9]{1,5}$ ]] || [ "$((10#${cap}))" -lt 1 ] || [ "$((10#${cap}))" -gt 10000 ]; then
		echo "CI_MAIN_DELTA mode=select outcome=note reason=cap_invalid_default cap=60"
		cap=60
	fi
	cap=$((10#${cap}))
	[[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] && CI_MAIN_DELTA_HEAD="${head_sha}"
	if ! [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${repo}" == *..* ]] \
		|| ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${branch}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]] || [[ "${branch}" == *..* ]]; then
		ci_main_delta_full select invalid_input
		return 0
	fi
	local_head="$(git rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null || true)"
	if [ "${local_head}" != "${head_sha}" ]; then
		ci_main_delta_full select head_mismatch
		return 0
	fi

	# Up to 3 tries, 2s apart, for a transient error; then the full suite.
	# gh's stderr goes to a private file only to name a category on the last
	# failure (a missing `actions: read` must not look like a flaky API).
	local read_attempt=1 read_err_file="" read_category="other"
	read_err_file="$(mktemp "${TMPDIR:-/tmp}/ci_main_delta_read.XXXXXX" 2>/dev/null || true)"
	until runs="$(gh api "repos/${repo}/actions/workflows/ci.yml/runs?branch=${branch}&status=completed&per_page=30" 2>"${read_err_file:-/dev/null}")"; do
		if [ "${read_attempt}" -ge 3 ]; then
			if [ -n "${read_err_file}" ] && grep -Eqi 'HTTP 40[13]|bad credentials|resource not accessible' "${read_err_file}" 2>/dev/null; then
				read_category=auth
			elif [ -n "${read_err_file}" ] && grep -Eqi 'HTTP 404|not found' "${read_err_file}" 2>/dev/null; then
				read_category=not_found
			fi
			[ -z "${read_err_file}" ] || rm -f "${read_err_file}"
			echo "CI_MAIN_DELTA mode=select outcome=note reason=read_failed_category category=${read_category}"
			ci_main_delta_full select read_failed
			return 0
		fi
		read_attempt=$((read_attempt + 1))
		sleep 2
	done
	[ -z "${read_err_file}" ] || rm -f "${read_err_file}"
	verdict="$(printf '%s' "${runs}" | jq -r --arg branch "${branch}" --arg path "${CI_MAIN_DELTA_WORKFLOW_PATH}" '
		if (type == "object" and (.workflow_runs | type) == "array") then
			[.workflow_runs[] | select(type == "object")
				| select((.event // "") | IN("push", "schedule"))
				| select((.head_branch // "") == $branch)
				| select(((.path // "") | tostring | split("@") | .[0]) == $path)
				| select((.status // "") == "completed")
				| select((.head_sha // "") | tostring | test("^[0-9a-f]{40}$"))
				| select((.conclusion // "") | IN("cancelled", "skipped") | not)]
			| sort_by([(.updated_at // .created_at // ""), (.created_at // "")]) as $done
			| ([$done[] | select((.conclusion // "") | IN("success", "neutral", "") | not)] | last) as $red
			| if ($done | length) == 0 then "none"
			  elif $red != null and ([$done[] | select((.conclusion // "") == "success"
			      and ((if ((.run_attempt // 1) | type == "number" and . <= 1) then (.run_started_at // .created_at // "") else (.created_at // "") end) >= ($red.updated_at // $red.created_at // "")))] | length) == 0
			  then "red \($red.head_sha)"
			  elif ($done | last | .conclusion // "") == "success" then "green \($done | last | .head_sha)"
			  else "red \($done | last | .head_sha)" end
		else "invalid" end' 2>/dev/null | tail -n1)"
	verdict_kind="${verdict%% *}"
	case "${verdict_kind}" in
		green) ;;
		red)
			ci_main_delta_full select base_red
			return 0
			;;
		none)
			ci_main_delta_full select no_green_base
			return 0
			;;
		*)
			ci_main_delta_full select invalid_response
			return 0
			;;
	esac
	base_sha="${verdict#* }"
	if ! [[ "${base_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		ci_main_delta_full select invalid_response
		return 0
	fi
	CI_MAIN_DELTA_BASE="${base_sha}"
	if ! git merge-base --is-ancestor "${base_sha}" "${head_sha}" >/dev/null 2>&1; then
		ci_main_delta_full select base_not_ancestor
		return 0
	fi

	work_dir="$(mktemp -d "${TMPDIR:-/tmp}/ci_main_delta.XXXXXX" 2>/dev/null || true)"
	if [ -z "${work_dir}" ] || [ ! -d "${work_dir}" ]; then
		ci_main_delta_full select diff_failed
		return 0
	fi
	changed_file="${work_dir}/changed.txt"
	selection_file="${work_dir}/selection.json"
	selected_file="${work_dir}/selected.txt"
	if ! git diff --name-status -M "${base_sha}" "${head_sha}" > "${changed_file}" 2>/dev/null; then
		rm -rf "${work_dir}"
		ci_main_delta_full select diff_failed
		return 0
	fi
	CI_MAIN_DELTA_CHANGED="$(grep -c . "${changed_file}" 2>/dev/null || true)"
	[[ "${CI_MAIN_DELTA_CHANGED}" =~ ^[0-9]+$ ]] || CI_MAIN_DELTA_CHANGED=0

	if [ "${CI_MAIN_DELTA_CHANGED}" -gt 0 ]; then
		if [ ! -f "${CI_MAIN_DELTA_SELECTOR}" ] \
			|| ! PYTHONDONTWRITEBYTECODE=1 python3 "${CI_MAIN_DELTA_SELECTOR}" select --repo-root . \
				--changed-files-file "${changed_file}" --out "${selection_file}" >/dev/null 2>&1 \
			|| ! jq -e 'type == "object"' "${selection_file}" >/dev/null 2>&1; then
			rm -rf "${work_dir}"
			ci_main_delta_full select selector_failed
			return 0
		fi
		# One path per line, or the single word `invalid` for any malformed
		# entry (not a list, not an object, a path that is not a string).
		if ! jq -r '
			.affected_tests as $a
			| if ($a | type) != "array" then "invalid"
			  elif any($a[]; (type != "object") or ((.path | type) != "string") or ((.path | test("[\\n\\r]")))) then "invalid"
			  else $a[] | .path end' "${selection_file}" > "${selected_file}" 2>/dev/null; then
			rm -rf "${work_dir}"
			ci_main_delta_full select selector_failed
			return 0
		fi
		if grep -qx "invalid" "${selected_file}"; then
			rm -rf "${work_dir}"
			ci_main_delta_full select selection_invalid
			return 0
		fi
		while IFS= read -r selected_path; do
			[ -n "${selected_path}" ] || continue
			if ! ci_main_delta_path_ok "${selected_path}"; then
				rm -rf "${work_dir}"
				ci_main_delta_full select selection_invalid
				return 0
			fi
			# A test the range deleted no longer exists: nothing to run.
			[ -f "${selected_path}" ] && [ ! -L "${selected_path}" ] || continue
			[ -z "${seen[${selected_path}]:-}" ] || continue
			seen["${selected_path}"]=1
			selected+=("${selected_path}")
		done < "${selected_file}"
	fi
	rm -rf "${work_dir}"
	CI_MAIN_DELTA_TESTS="${#selected[@]}"
	if [ "${CI_MAIN_DELTA_TESTS}" -gt "${cap}" ]; then
		ci_main_delta_full select over_cap
		return 0
	fi
	for selected_path in "${selected[@]}"; do
		if ci_main_delta_is_sharded "${selected_path}"; then
			ci_main_delta_full select sharded_test_selected
			return 0
		fi
	done
	if [ "${#CI_SYNC_QUICK_WIRING_PATHS[@]}" -eq 0 ]; then
		ci_main_delta_full select wiring_list_missing
		return 0
	fi
	for selected_path in "${CI_SYNC_QUICK_WIRING_PATHS[@]}"; do
		case "${selected_path}" in
			tests/test_*.py) ;;
			*) continue ;;
		esac
		[ -f "${selected_path}" ] && [ -z "${seen[${selected_path}]:-}" ] || continue
		seen["${selected_path}"]=1
		selected+=("${selected_path}")
	done
	list_file="$(ci_main_delta_tests_file)"
	if ! { [ "${#selected[@]}" -eq 0 ] || printf '%s\n' "${selected[@]}"; } > "${list_file}" 2>/dev/null; then
		ci_main_delta_full select list_write_failed
		return 0
	fi
	ci_main_delta_output full_required false
	if [ "${CI_MAIN_DELTA_CHANGED}" -eq 0 ]; then
		ci_main_delta_log select ok no_changes
	else
		ci_main_delta_log select ok selected
	fi
	return 0
}

# `ci_main_delta_ci_runs_as_script <path>`: 0 when ci.yml runs the file as
# `python3 <path>` (a unittest main or a plain script), so it runs the same way.
ci_main_delta_ci_runs_as_script()
{
	grep -Eq "python3 $(printf '%s' "$1" | sed 's/[.[\*^$()+?{|]/\\&/g')([[:space:]]|\$)" "${CI_MAIN_DELTA_WORKFLOW_PATH}" 2>/dev/null
}

# `ci_main_delta_timed <deadline epoch> <command...>`: runs the command with
# the time left; returns 124 when none is left or the command timed out.
ci_main_delta_timed()
{
	local deadline="$1" remaining
	shift
	remaining=$(( deadline - $(date -u +%s) ))
	[ "${remaining}" -gt 0 ] || return 124
	PYTHONDONTWRITEBYTECODE=1 timeout "${remaining}s" "$@"
}

ci_main_delta_run_tests()
{
	local list_file listed_path deadline limit rc=0 step_rc=0 timed_out=false
	local -a script_tests=() pytest_tests=() shell_tests=()
	local -A seen=()
	CI_MAIN_DELTA_HEAD="${CI_MAIN_DELTA_HEAD_SHA:-${GITHUB_SHA:-none}}"
	[[ "${CI_MAIN_DELTA_HEAD}" =~ ^[0-9a-f]{40}$ ]] || CI_MAIN_DELTA_HEAD="none"
	list_file="$(ci_main_delta_tests_file)"
	if [ ! -f "${list_file}" ]; then
		ci_main_delta_log run_tests fail list_missing
		return 1
	fi
	while IFS= read -r listed_path || [ -n "${listed_path}" ]; do
		[ -n "${listed_path}" ] || continue
		if ! ci_main_delta_path_ok "${listed_path}"; then
			ci_main_delta_log run_tests fail list_invalid
			return 1
		fi
		[ -f "${listed_path}" ] && [ ! -L "${listed_path}" ] && [ -z "${seen[${listed_path}]:-}" ] || continue
		seen["${listed_path}"]=1
		case "${listed_path}" in
			*.py)
				if ci_main_delta_ci_runs_as_script "${listed_path}"; then
					script_tests+=("${listed_path}")
				elif [[ "$(basename -- "${listed_path}")" == test_*.py ]]; then
					pytest_tests+=("${listed_path}")
				fi
				;;
			*.sh)
				if [[ "$(basename -- "${listed_path}")" == test_*.sh ]] || grep -Fq "bash ${listed_path}" "${CI_MAIN_DELTA_WORKFLOW_PATH}" 2>/dev/null; then
					shell_tests+=("${listed_path}")
				fi
				;;
		esac
	done < "${list_file}"
	CI_MAIN_DELTA_TESTS=$(( ${#script_tests[@]} + ${#pytest_tests[@]} + ${#shell_tests[@]} ))
	limit="${CI_MAIN_DELTA_TEST_TIMEOUT_SECS:-600}"
	{ [[ "${limit}" =~ ^[0-9]{2,4}$ ]] && [ "$((10#${limit}))" -ge 60 ] && [ "$((10#${limit}))" -le 1080 ]; } || limit=600
	deadline=$(( $(date -u +%s) + 10#${limit} ))
	echo "CI_MAIN_DELTA mode=run_tests outcome=start reason=selected script=${#script_tests[@]} pytest=${#pytest_tests[@]} shell=${#shell_tests[@]}"

	for listed_path in "${script_tests[@]}"; do
		ci_main_delta_timed "${deadline}" python3 "${listed_path}"
		step_rc=$?
		[ "${step_rc}" -eq 124 ] && { timed_out=true; break; }
		[ "${step_rc}" -eq 0 ] || rc=1
	done
	if [ "${timed_out}" != "true" ] && [ "${#pytest_tests[@]}" -gt 0 ]; then
		ci_main_delta_timed "${deadline}" python3 -m pytest -q -p no:cacheprovider "${pytest_tests[@]}"
		step_rc=$?
		if [ "${step_rc}" -eq 124 ]; then
			timed_out=true
		elif [ "${step_rc}" -ne 0 ] && [ "${step_rc}" -ne 5 ]; then
			rc=1
		fi
	fi
	if [ "${timed_out}" != "true" ]; then
		for listed_path in "${shell_tests[@]}"; do
			ci_main_delta_timed "${deadline}" bash "${listed_path}"
			step_rc=$?
			[ "${step_rc}" -eq 124 ] && { timed_out=true; break; }
			[ "${step_rc}" -eq 0 ] || rc=1
		done
	fi
	if [ "${timed_out}" = "true" ]; then
		ci_main_delta_output full_required true
		ci_main_delta_log run_tests full_required tests_timed_out
		echo "::notice::The delta tests ran out of time; the full test suite runs in this run."
		return 0
	fi
	if [ "${rc}" -ne 0 ]; then
		ci_main_delta_output full_required false
		ci_main_delta_log run_tests fail tests_failed
		echo "::error::Delta tests failed on main."
		return 1
	fi
	ci_main_delta_output full_required false
	ci_main_delta_log run_tests ok tests_passed
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	case "${1:-}" in
		select) ci_main_delta_select ;;
		run-tests) ci_main_delta_run_tests ;;
		*)
			echo "usage: ci_main_delta.sh select | run-tests" >&2
			exit 2
			;;
	esac
fi

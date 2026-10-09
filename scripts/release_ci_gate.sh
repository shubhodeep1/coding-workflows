#!/usr/bin/env bash
# Release CI gate (security finding `release-gate-ignores-actions-ci`,
# issue #6797, re-issued from #6532).
#
# The stable release workflows (test-and-mark-stable.yml, mark-stable.yml)
# used to tag a commit after their own test jobs only, reading the legacy
# combined commit status, which never contains GitHub Actions check-runs. A
# commit whose `CI / lint` aggregate was red, cancelled or never ran could be
# tagged `stable` and shipped to every consumer repository.
#
# Usage:
#   release_ci_gate.sh <owner/repo> <40-hex-sha>
#
# Passes only when the newest `lint` check-run that
#   - was created by the github-actions app,
#   - belongs to a check suite of a `.github/workflows/ci.yml` workflow run
#     whose head_sha is <sha>, and
#   - itself reports head_sha == <sha>
# is `completed` with conclusion `success`. Everything else fails closed.
#
# Exit codes:
#   0  pass
#   1  gate failure: a completed non-success conclusion
#      (reason=conclusion_<value>), a CI check-run for a different SHA
#      (sha_mismatch), unreadable or malformed API output (api_unreadable),
#      or no passing check-run before the wait ran out (wait_timeout, with
#      last_state=missing|pending|non_actions_app)
#   2  invalid input (reason=invalid_input)
#
# Waiting (bounded, fail closed): while the qualifying check-run is missing,
# queued or in progress, or only a same-name check-run from another app or a
# non-CI workflow exists, the gate polls every RELEASE_CI_GATE_POLL_SECS
# (default 30, 1..300) until RELEASE_CI_GATE_WAIT_SECS (default 1800,
# 0..2400) have elapsed. 0 means a single check without waiting. A completed
# non-success result fails at once.
#
# Other knobs (defaults live here, §8): RELEASE_CI_GATE_CHECK_NAME (default
# `lint`, the aggregate job id in ci.yml) and RELEASE_CI_GATE_WORKFLOW_PATH
# (default `.github/workflows/ci.yml`). Invalid values fall back to the
# default with a ::warning::.
#
# API budget (§14), per poll: two logical reads, each paginated at 100 per
# page and retried by gh_retry: the CI workflow's runs for <sha>, and the
# commit's check-runs filtered to the check name. No existing release-job
# call returns check-runs, so these cannot be merged with an earlier read.
#
# Log prefix: RELEASE_CI_GATE. Only fixed tokens and sanitized API values
# ([A-Za-z0-9_.-]) are logged; the token is never printed.

set -euo pipefail

RCG_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${RCG_SCRIPT_DIR}/gh_helpers.sh" ]; then
	# shellcheck source=/dev/null
	source "${RCG_SCRIPT_DIR}/gh_helpers.sh" || true
fi
if ! type gh_retry >/dev/null 2>&1; then
	gh_retry()
	{
		"$@"
	}
fi

_rcg_repo="${1:-}"
_rcg_sha="${2:-}"
_rcg_waited=0

_rcg_log()
{
	# $1 outcome, $2 reason, $3 check_run_id, $4 status, $5 conclusion, $6 extra
	local line="RELEASE_CI_GATE repo=${_rcg_repo_log} sha=${_rcg_sha_log} outcome=$1 reason=$2 check_run_id=$3 status=$4 conclusion=$5 waited_secs=${_rcg_waited}${6:+ $6}"
	if [ "$1" = "fail" ]; then
		echo "::error::${line}"
	fi
	echo "${line}"
}

_rcg_repo_log="$(printf '%s' "${_rcg_repo}" | tr -c 'A-Za-z0-9_./-' '_' | cut -c1-200)"
_rcg_sha_log="$(printf '%s' "${_rcg_sha}" | tr -c 'A-Za-z0-9' '_' | cut -c1-64)"

if [ "$#" -ne 2 ] \
	|| ! [[ "${_rcg_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
	|| [[ "${_rcg_repo}" =~ (^|/)\.{1,2}(/|$) ]] \
	|| ! [[ "${_rcg_sha}" =~ ^[0-9a-f]{40}$ ]]; then
	_rcg_log fail invalid_input none none none
	exit 2
fi

_rcg_int_env()
{
	# $1 variable name, $2 default, $3 min, $4 max
	local raw="${!1:-}"
	if [ -z "${raw}" ]; then
		printf '%s' "$2"
		return 0
	fi
	if [[ "${raw}" =~ ^[0-9]{1,6}$ ]] && [ "$((10#${raw}))" -ge "$3" ] && [ "$((10#${raw}))" -le "$4" ]; then
		printf '%s' "$((10#${raw}))"
		return 0
	fi
	echo "::warning::RELEASE_CI_GATE $1 is not an integer in $3..$4; using the default $2." >&2
	printf '%s' "$2"
}

RCG_WAIT_SECS="$(_rcg_int_env RELEASE_CI_GATE_WAIT_SECS 1800 0 2400)"
RCG_POLL_SECS="$(_rcg_int_env RELEASE_CI_GATE_POLL_SECS 30 1 300)"

RCG_CHECK_NAME="${RELEASE_CI_GATE_CHECK_NAME:-lint}"
if ! [[ "${RCG_CHECK_NAME}" =~ ^[A-Za-z0-9_.-]{1,100}$ ]]; then
	echo "::warning::RELEASE_CI_GATE RELEASE_CI_GATE_CHECK_NAME is not a plain job name; using the default lint." >&2
	RCG_CHECK_NAME="lint"
fi
RCG_WORKFLOW_PATH="${RELEASE_CI_GATE_WORKFLOW_PATH:-.github/workflows/ci.yml}"
if ! [[ "${RCG_WORKFLOW_PATH}" =~ ^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$ ]]; then
	echo "::warning::RELEASE_CI_GATE RELEASE_CI_GATE_WORKFLOW_PATH is not a .github/workflows/<file>.yml path; using the default .github/workflows/ci.yml." >&2
	RCG_WORKFLOW_PATH=".github/workflows/ci.yml"
fi
RCG_WORKFLOW_FILE="${RCG_WORKFLOW_PATH##*/}"

RCG_TMP="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/release_ci_gate.XXXXXX")"
trap 'rm -rf "${RCG_TMP}"' EXIT

# Prints "<state>\t<reason>\t<check_run_id>\t<status>\t<conclusion>" for one
# poll. state is pass|fail|wait. API data is untrusted: anything that is not
# the documented shape is api_unreadable (fail closed).
_rcg_poll()
{
	if ! gh_retry gh api --paginate --slurp \
		"repos/${_rcg_repo}/actions/workflows/${RCG_WORKFLOW_FILE}/runs?head_sha=${_rcg_sha}&per_page=100" \
		>"${RCG_TMP}/runs.json" 2>"${RCG_TMP}/runs.err"; then
		printf 'fail\tapi_unreadable\tnone\tnone\tnone\n'
		return 0
	fi
	if ! gh_retry gh api --paginate --slurp \
		"repos/${_rcg_repo}/commits/${_rcg_sha}/check-runs?check_name=${RCG_CHECK_NAME}&filter=all&per_page=100" \
		>"${RCG_TMP}/checks.json" 2>"${RCG_TMP}/checks.err"; then
		printf 'fail\tapi_unreadable\tnone\tnone\tnone\n'
		return 0
	fi
	RCG_SHA="${_rcg_sha}" RCG_NAME="${RCG_CHECK_NAME}" RCG_PATH="${RCG_WORKFLOW_PATH}" \
		RCG_RUNS_FILE="${RCG_TMP}/runs.json" RCG_CHECKS_FILE="${RCG_TMP}/checks.json" \
		python3 -I -B - <<'PY' || printf 'fail\tapi_unreadable\tnone\tnone\tnone\n'
import json
import os
import re
import sys

sha = os.environ["RCG_SHA"]
name = os.environ["RCG_NAME"]
path = os.environ["RCG_PATH"]


def clean(value):
	text = "null" if value is None else str(value)
	return re.sub(r"[^A-Za-z0-9_.-]", "_", text)[:64] or "none"


def emit(state, reason, run_id="none", status="none", conclusion="none"):
	print("\t".join([state, reason, run_id, status, conclusion]))
	sys.exit(0)


def load_pages(file_name, key):
	with open(file_name, encoding="utf-8") as handle:
		pages = json.load(handle)
	if not isinstance(pages, list):
		raise ValueError("not a page array")
	items = []
	for page in pages:
		if not isinstance(page, dict) or not isinstance(page.get(key), list):
			raise ValueError("page without " + key)
		for item in page[key]:
			if not isinstance(item, dict):
				raise ValueError("non-object item")
			items.append(item)
	return items


try:
	runs = load_pages(os.environ["RCG_RUNS_FILE"], "workflow_runs")
	checks = load_pages(os.environ["RCG_CHECKS_FILE"], "check_runs")
except (OSError, ValueError, json.JSONDecodeError):
	emit("fail", "api_unreadable")

ci_suites = set()
for run in runs:
	run_path = run.get("path")
	suite = run.get("check_suite_id")
	if (
		run.get("head_sha") == sha
		and isinstance(run_path, str)
		and run_path.split("@", 1)[0] == path
		and isinstance(suite, int)
		and not isinstance(suite, bool)
	):
		ci_suites.add(suite)


def is_ci_actions(check):
	app = check.get("app")
	suite = check.get("check_suite")
	return (
		isinstance(app, dict)
		and app.get("slug") == "github-actions"
		and isinstance(suite, dict)
		and suite.get("id") in ci_suites
	)


named = [c for c in checks if c.get("name") == name]
ci_named = [c for c in named if is_ci_actions(c)]
candidates = [c for c in ci_named if c.get("head_sha") == sha and isinstance(c.get("id"), int)]

if not candidates:
	if ci_named:
		emit("fail", "sha_mismatch")
	if named:
		# Same name from another app or a non-CI workflow: never counted.
		# Keep waiting for the real CI check-run instead of failing a
		# release because an unrelated check shares the name.
		emit("wait", "non_actions_app")
	emit("wait", "missing")

newest = max(candidates, key=lambda c: c["id"])
run_id = clean(newest["id"])
status = newest.get("status")
conclusion = newest.get("conclusion")
if status != "completed":
	emit("wait", "pending", run_id, clean(status), clean(conclusion))
if conclusion == "success":
	emit("pass", "success", run_id, clean(status), clean(conclusion))
emit("fail", "conclusion_" + clean(conclusion), run_id, clean(status), clean(conclusion))
PY
}

_rcg_start="$(date +%s)"
while :; do
	_rcg_result="$(_rcg_poll)"
	IFS=$'\t' read -r _rcg_state _rcg_reason _rcg_id _rcg_status _rcg_conclusion <<<"${_rcg_result}" || true
	_rcg_now="$(date +%s)"
	_rcg_waited=$((_rcg_now - _rcg_start))
	case "${_rcg_state:-}" in
		pass)
			_rcg_log pass "${_rcg_reason}" "${_rcg_id}" "${_rcg_status}" "${_rcg_conclusion}"
			exit 0
			;;
		wait)
			_rcg_remaining=$((RCG_WAIT_SECS - _rcg_waited))
			if [ "${_rcg_remaining}" -le 0 ]; then
				_rcg_log fail wait_timeout "${_rcg_id}" "${_rcg_status}" "${_rcg_conclusion}" "last_state=${_rcg_reason}"
				exit 1
			fi
			_rcg_log waiting "${_rcg_reason}" "${_rcg_id}" "${_rcg_status}" "${_rcg_conclusion}"
			_rcg_sleep="${RCG_POLL_SECS}"
			if [ "${_rcg_sleep}" -gt "${_rcg_remaining}" ]; then
				_rcg_sleep="${_rcg_remaining}"
			fi
			sleep "${_rcg_sleep}"
			;;
		fail)
			_rcg_log fail "${_rcg_reason}" "${_rcg_id}" "${_rcg_status}" "${_rcg_conclusion}"
			exit 1
			;;
		*)
			_rcg_log fail api_unreadable none none none
			exit 1
			;;
	esac
done

#!/usr/bin/env python3
"""Tests that the branch-scoped list-runs probes issue a real GET.

Background (tele-funtoken-msg-scoring#3763, review run 32732281452):
``autofix_retrigger_has_inflight_peer`` and
``autofix_changes_lost_head_retry_consumed`` (scripts/gh_helpers.sh) both
query ``/repos/{repo}/actions/runs`` with ``-f branch=`` and
``-f per_page=``.  ``gh api`` infers the HTTP method from its arguments:
the default is GET, but it switches to **POST** as soon as any ``-f`` /
``-F`` parameter is supplied and no ``--method`` is given.  There is no
``POST /repos/{repo}/actions/runs`` route, so every invocation 404'd.
``_is_gh_permanent_failure`` matches HTTP 404, so ``gh_retry`` did not
retry and both probes returned ``reason=api_error`` immediately::

    AUTOFIX_PEER_QUERY_FAILED pr=3764 branch=ai/issue-3763 reason=api_error
    AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=3764 ... reason=api_error

The peer probe fails OPEN, so its breakage was invisible.  The budget
probe fails CLOSED, so the identical failure reported the per-head-SHA
retry budget as consumed on a head that had never been retried,
permanently suppressing the editor-changes-lost re-dispatch: every
occurrence dead-ended on the CRITICAL "retry unavailable" alert with
auto-merge blocked, until the orchestrator's generic stall recovery
re-triggered the review hours later.

The pre-existing suite could not catch this because its ``gh`` stub
ignores its arguments.  The stub below emulates gh's method inference,
so a future removal of ``-X GET`` fails these tests.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"

PROBES = (
	"autofix_retrigger_has_inflight_peer",
	"autofix_changes_lost_head_retry_consumed",
	# The PR-named lookup both probes call since issue #4898.
	"_autofix_pr_named_review_runs",
)

HEAD = "789e2f8a036f2236a5f10dcffab31019d328d77c"
CURRENT_RUN = "32732281452"
BRANCH = "ai/issue-3763"


def _helpers_text() -> str:
	return GH_HELPERS.read_text(encoding="utf-8")


def _probe_body(text: str, fn: str) -> str:
	m = re.search(r"^%s\(\)\n\{\n(.*?)\n\}$" % re.escape(fn), text, re.DOTALL | re.MULTILINE)
	assert m, "Failed to locate %s in gh_helpers.sh" % fn
	return m.group(0)


def _list_runs_call(body: str, fn: str) -> str:
	uncommented_body = "\n".join(
		line for line in body.splitlines()
		if not line.lstrip().startswith("#")
	)
	m = re.search(r"gh_retry gh api(?P<call>(?:(?!gh_retry gh api).)*?/actions/(?:workflows/[^\s]+/)?runs.*?2>(?:/dev/null|\"\$\{review_error_file\}\"))", uncommented_body, re.DOTALL)
	assert m, "Failed to locate %s list-runs gh api call" % fn
	return m.group("call")


def test_list_runs_probes_pin_the_get_method() -> None:
	text = _helpers_text()
	for fn in PROBES:
		body = _probe_body(text, fn)
		list_runs_call = _list_runs_call(body, fn)
		assert re.search(r"/actions/(?:workflows/[^\s]+/)?runs", list_runs_call), fn
		# gh api infers POST from the -f parameters unless the method is
		# pinned on the actual command; POST /actions/runs is not a route and 404s.
		assert re.search(r"(?:-X GET|--method GET)(?:\s*\\|\s)", list_runs_call), (
			"%s must pin GET on the list-runs call — without it `gh api` "
			"POSTs and the probe 404s" % fn
		)


# ---------------------------------------------------------------------------
# Functional tests against a gh stub that emulates gh's method inference
# ---------------------------------------------------------------------------

_RUNNER = r"""
extract_fn() {
	awk -v fn="$1" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__HELPERS__"
}

gh_retry() { "$@"; }
emit_event() { :; }

# Emulate `gh api` method inference: default GET, but POST as soon as any
# -f/-F parameter is present and no -X/--method is supplied. POST against
# the list-runs endpoint is not a route, so gh prints a 404 and fails.
gh() {
	[ "${1:-}" = "api" ] || return 1
	shift
	case " $* " in
		*" repos/owner/repo --jq .default_branch "*) printf 'main\n'; return 0 ;;
		*" /repos/owner/repo/actions/workflows/"*)
			printf '{"total_count":0,"workflow_runs":[]}\n'
			return 0
			;;
	esac
	method=""
	has_field=0
	for arg in "$@"; do
		if [ "$method" = "PENDING" ]; then method="$arg"; continue; fi
		case "$arg" in
			-X|--method) method="PENDING" ;;
			-X*) method="${arg#-X}" ;;
			--method=*) method="${arg#--method=}" ;;
			-f|-F|--field|--raw-field) has_field=1 ;;
		esac
	done
	if [ "$method" = "PENDING" ]; then
		echo "gh: flag needs an argument: -X" >&2
		return 1
	fi
	if [ -z "$method" ]; then
		if [ "$has_field" = "1" ]; then method="POST"; else method="GET"; fi
	fi
	if [ "$method" != "GET" ]; then
		echo "gh: Not Found (HTTP 404)" >&2
		return 1
	fi
	cat "${RUNS_FIXTURE}"
}

eval "$(extract_fn _autofix_pr_named_review_runs)"
eval "$(extract_fn __FN__)"
__FN__ "$@"
"""


def _run_probe(fn: str, runs_json: str, *args: str) -> subprocess.CompletedProcess:
	with tempfile.TemporaryDirectory() as tmp:
		fixture = Path(tmp) / "runs.json"
		fixture.write_text(runs_json, encoding="utf-8")
		env = dict(os.environ)
		env["GITHUB_REPOSITORY"] = "owner/repo"
		env["RUNS_FIXTURE"] = str(fixture)
		script = _RUNNER.replace("__HELPERS__", str(GH_HELPERS)).replace("__FN__", fn)
		return subprocess.run(
			["bash", "-c", script, "bash", *args],
			capture_output=True,
			text=True,
			env=env,
		)


def _runs(*entries: dict) -> str:
	return json.dumps({"workflow_runs": list(entries)})


def test_budget_probe_reaches_the_api_and_reports_budget_available() -> None:
	# A head SHA no prior run has reviewed: the budget is available, so
	# the caller may dispatch exactly one automated retry. This is the
	# real #3763 shape — head 789e2f8a appears on no other completed run;
	# its push left only the cancelled pull_request twin, whose created_at
	# is the push-time bound (issue #5523).
	proc = _run_probe(
		"autofix_changes_lost_head_retry_consumed",
		_runs(
			{
				"id": 32720851044,
				"status": "completed",
				"conclusion": "success",
				"head_sha": "549d94dd0f80955e0cf28e55be2152378e5e2937",
				"path": ".github/workflows/ai-review.yml",
				"created_at": "2026-09-20T11:00:00Z",
			},
			{
				"id": 32720851045,
				"status": "completed",
				"conclusion": "cancelled",
				"head_sha": HEAD,
				"path": ".github/workflows/ai-review.yml",
				"created_at": "2026-09-20T12:00:00Z",
			},
		),
		"3764",
		BRANCH,
		CURRENT_RUN,
		HEAD,
		# Head commit time, which the step passes since issue #4898 and the
		# helper ignores since issue #5523.
		"1790000000",
	)
	assert "reason=api_error" not in proc.stderr, proc.stderr
	assert "prior_completed=0" in proc.stdout, proc.stdout
	# rc=1 means budget available; rc=0 would suppress the re-dispatch.
	assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)


def test_budget_probe_still_fails_closed_when_the_head_was_already_retried() -> None:
	proc = _run_probe(
		"autofix_changes_lost_head_retry_consumed",
		_runs(
			{
				"id": 32700000000,
				"status": "completed",
				"conclusion": "success",
				"head_sha": HEAD,
				"path": ".github/workflows/ai-review.yml",
			}
		),
		"3764",
		BRANCH,
		CURRENT_RUN,
		HEAD,
	)
	assert "prior_completed=1" in proc.stdout, proc.stdout
	assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)


def test_peer_probe_reaches_the_api_and_detects_an_inflight_peer() -> None:
	proc = _run_probe(
		"autofix_retrigger_has_inflight_peer",
		_runs(
			{
				"id": 32799999999,
				"status": "in_progress",
				"conclusion": None,
				"head_sha": HEAD,
				"path": ".github/workflows/ai-review.yml",
			}
		),
		"3764",
		BRANCH,
		CURRENT_RUN,
	)
	assert "reason=api_error" not in proc.stderr, proc.stderr
	assert "peer_count=1" in proc.stdout, proc.stdout
	assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)


# ---------------------------------------------------------------------------
# One-shot branch-runs snapshot reuse (MERGE-001)
# ---------------------------------------------------------------------------
#
# The editor-changes-lost step runs the peer probe and then the budget probe
# in one shell. The budget probe reuses the peer probe's validated branch
# response once, so that path makes one branch `actions/runs` call, not two.

_PAIR_RUNNER = r"""
extract_fn() {
	awk -v fn="$1" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__HELPERS__"
}

gh_retry() { "$@"; }
emit_event() { :; }

gh() {
	[ "${1:-}" = "api" ] || return 1
	shift
	printf '%s\n' "$*" >> "${GH_CALLS}"
	case " $* " in
		*" --jq .default_branch "*) printf 'main\n'; return 0 ;;
		*"/actions/workflows/"*)
			printf '{"total_count":0,"workflow_runs":[]}\n'
			return 0
			;;
	esac
	method=""
	has_field=0
	for arg in "$@"; do
		if [ "$method" = "PENDING" ]; then method="$arg"; continue; fi
		case "$arg" in
			-X|--method) method="PENDING" ;;
			-X*) method="${arg#-X}" ;;
			--method=*) method="${arg#--method=}" ;;
			-f|-F|--field|--raw-field) has_field=1 ;;
		esac
	done
	if [ -z "$method" ] || [ "$method" = "PENDING" ]; then
		if [ "$has_field" = "1" ]; then method="POST"; else method="GET"; fi
	fi
	if [ "$method" != "GET" ]; then
		echo "gh: Not Found (HTTP 404)" >&2
		return 1
	fi
	count=$(( $(cat "${RUNS_FIXTURE_DIR}/count" 2>/dev/null || echo 0) + 1 ))
	printf '%s' "$count" > "${RUNS_FIXTURE_DIR}/count"
	fixture="${RUNS_FIXTURE_DIR}/${count}.json"
	if [ ! -f "$fixture" ] || [ "$(cat "$fixture")" = "__FAIL__" ]; then
		echo "gh: Server Error (HTTP 500)" >&2
		return 1
	fi
	cat "$fixture"
}

eval "$(extract_fn _autofix_pr_named_review_runs)"
eval "$(extract_fn autofix_retrigger_has_inflight_peer)"
eval "$(extract_fn autofix_changes_lost_head_retry_consumed)"

for step in "$@"; do
	kind="${step%%:*}"
	arg="${step#*:}"
	case "$kind" in
		peer)
			autofix_retrigger_has_inflight_peer 3764 "$arg" __RUN__
			echo "PEER_RC=$?"
			;;
		budget)
			autofix_changes_lost_head_retry_consumed 3764 "$arg" __RUN__ __HEAD__
			echo "BUDGET_RC=$?"
			;;
		repo)
			GITHUB_REPOSITORY="$arg"
			;;
	esac
done
"""

OTHER_BRANCH = "ai/issue-9999"


def _run_pair(fixtures: list, *steps: str) -> tuple:
	with tempfile.TemporaryDirectory() as tmp:
		fixture_dir = Path(tmp) / "fixtures"
		fixture_dir.mkdir()
		for index, content in enumerate(fixtures, start=1):
			(fixture_dir / ("%d.json" % index)).write_text(content, encoding="utf-8")
		calls = Path(tmp) / "calls.log"
		calls.write_text("", encoding="utf-8")
		env = dict(os.environ)
		env["GITHUB_REPOSITORY"] = "owner/repo"
		env["RUNS_FIXTURE_DIR"] = str(fixture_dir)
		env["GH_CALLS"] = str(calls)
		script = (
			_PAIR_RUNNER.replace("__HELPERS__", str(GH_HELPERS))
			.replace("__RUN__", CURRENT_RUN)
			.replace("__HEAD__", HEAD)
		)
		proc = subprocess.run(
			["bash", "-c", script, "bash", *steps],
			capture_output=True,
			text=True,
			env=env,
		)
		call_lines = calls.read_text(encoding="utf-8").splitlines()
	branch_calls = [
		line for line in call_lines
		if "/actions/runs" in line and "branch=" in line and "actions/workflows/" not in line
	]
	return proc, branch_calls


def _cancelled_twin_on_head() -> str:
	return _runs(
		{
			"id": 32720851045,
			"status": "completed",
			"conclusion": "cancelled",
			"head_sha": HEAD,
			"path": ".github/workflows/ai-review.yml",
			"created_at": "2026-09-20T12:00:00Z",
		}
	)


def _completed_on_head() -> str:
	return _runs(
		{
			"id": 32700000000,
			"status": "completed",
			"conclusion": "success",
			"head_sha": HEAD,
			"path": ".github/workflows/ai-review.yml",
			"created_at": "2026-09-20T12:00:00Z",
		}
	)


def test_no_peer_path_makes_one_branch_runs_call() -> None:
	proc, branch_calls = _run_pair(
		[_cancelled_twin_on_head()], "peer:" + BRANCH, "budget:" + BRANCH
	)
	assert len(branch_calls) == 1, (branch_calls, proc.stderr)
	assert "PEER_RC=1" in proc.stdout, proc.stdout
	assert "BUDGET_RC=1" in proc.stdout, (proc.stdout, proc.stderr)
	assert "prior_completed=0" in proc.stdout, proc.stdout


def test_budget_probe_counts_from_the_reused_snapshot() -> None:
	proc, branch_calls = _run_pair(
		[_completed_on_head()], "peer:" + BRANCH, "budget:" + BRANCH
	)
	assert len(branch_calls) == 1, branch_calls
	assert "BUDGET_RC=0" in proc.stdout, (proc.stdout, proc.stderr)
	assert "prior_completed=1" in proc.stdout, proc.stdout


def test_failed_peer_call_leaves_no_snapshot() -> None:
	proc, branch_calls = _run_pair(
		["__FAIL__", _cancelled_twin_on_head()], "peer:" + BRANCH, "budget:" + BRANCH
	)
	assert len(branch_calls) == 2, branch_calls
	assert "BUDGET_RC=1" in proc.stdout, (proc.stdout, proc.stderr)


def test_failed_peer_and_live_calls_fail_closed() -> None:
	proc, branch_calls = _run_pair(
		["__FAIL__", "__FAIL__"], "peer:" + BRANCH, "budget:" + BRANCH
	)
	assert len(branch_calls) == 2, branch_calls
	assert "BUDGET_RC=0" in proc.stdout, proc.stdout
	assert "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED" in proc.stderr
	assert "reason=api_error" in proc.stderr, proc.stderr


def test_malformed_peer_response_is_not_reused() -> None:
	proc, branch_calls = _run_pair(
		['{"workflow_runs":"x"}', _completed_on_head()], "peer:" + BRANCH, "budget:" + BRANCH
	)
	assert len(branch_calls) == 2, branch_calls
	assert "BUDGET_RC=0" in proc.stdout, proc.stdout
	assert "prior_completed=1" in proc.stdout, proc.stdout


def test_malformed_peer_and_live_responses_fail_closed() -> None:
	proc, branch_calls = _run_pair(
		["not json", "not json"], "peer:" + BRANCH, "budget:" + BRANCH
	)
	assert len(branch_calls) == 2, branch_calls
	assert "BUDGET_RC=0" in proc.stdout, proc.stdout
	assert "reason=jq_error" in proc.stderr, proc.stderr


def test_different_branch_does_not_reuse_the_snapshot() -> None:
	proc, branch_calls = _run_pair(
		[_completed_on_head(), _cancelled_twin_on_head()],
		"peer:" + BRANCH,
		"budget:" + OTHER_BRANCH,
	)
	assert len(branch_calls) == 2, branch_calls
	assert "branch=" + OTHER_BRANCH in branch_calls[1], branch_calls
	assert "BUDGET_RC=1" in proc.stdout, (proc.stdout, proc.stderr)


def test_second_budget_call_makes_a_live_call() -> None:
	proc, branch_calls = _run_pair(
		[_cancelled_twin_on_head(), _completed_on_head()],
		"peer:" + BRANCH,
		"budget:" + BRANCH,
		"budget:" + BRANCH,
	)
	assert len(branch_calls) == 2, branch_calls
	assert proc.stdout.count("BUDGET_RC=") == 2, proc.stdout
	# The second call read the live (completed) fixture, not the snapshot.
	assert proc.stdout.rstrip().endswith("BUDGET_RC=0"), proc.stdout


def test_peer_probe_always_calls_live() -> None:
	proc, branch_calls = _run_pair(
		[_cancelled_twin_on_head(), _cancelled_twin_on_head()],
		"peer:" + BRANCH,
		"peer:" + BRANCH,
	)
	assert len(branch_calls) == 2, branch_calls
	assert proc.stdout.count("PEER_RC=1") == 2, proc.stdout


def test_repository_change_does_not_reuse_the_snapshot() -> None:
	proc, branch_calls = _run_pair(
		[_completed_on_head(), _cancelled_twin_on_head()],
		"peer:" + BRANCH,
		"repo:other/repo",
		"budget:" + BRANCH,
	)
	assert len(branch_calls) == 2, branch_calls
	assert "/repos/other/repo/actions/runs" in branch_calls[1], branch_calls

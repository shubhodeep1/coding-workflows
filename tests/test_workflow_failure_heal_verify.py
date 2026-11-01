"""Tests for scripts/workflow_failure_heal_verify.sh (P7 post-merge heal verification, Refs #8434).

The script runs against a fake `gh` (and a fake `curl` for the Telegram
helper) that serves one GraphQL list, workflow runs, compares and jobs from a
JSON state file and records every call and write.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "workflow_failure_heal_verify.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow-failure-heal-intake.yml"
REMOVAL_REGISTRY = REPO_ROOT / "docs" / "scripts-pending-removal.md"

SELF_REPO = "shubhodeep1/coding-workflows"
LOGIN = "workflow-bot"
MERGE_SHA = "a" * 40
OTHER_SHA = "c" * 40
DESC_SHA = "d" * 40
FP = "f" * 64
ROOT = "e" * 64
NOW = datetime.now(timezone.utc).replace(microsecond=0)
MERGED_AT = NOW - timedelta(days=2)


def _iso(value: datetime) -> str:
	return value.strftime("%Y-%m-%dT%H:%M:%SZ")


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def save():
	json.dump(state, open(state_path, "w"))
def out(value):
	save()
	sys.stdout.write(value if isinstance(value, str) else json.dumps(value))
	sys.stdout.write("\n")
	sys.exit(0)
def fail():
	save()
	sys.stderr.write("HTTP 500\n")
	sys.exit(1)
if args[:1] != ["api"]:
	fail()
method = "GET"
if "--method" in args:
	method = args[args.index("--method") + 1]
fails = set(state.get("fail", []))
if "graphql" in args:
	if "graphql" in fails:
		fail()
	out(state["graphql"])
path = next((a for a in args[1:] if a.startswith("repos/")), "")
route = path.split("?", 1)[0]
if "/actions/workflows/" in route and route.endswith("/runs"):
	if "runs" in fails:
		fail()
	name = route.split("/actions/workflows/", 1)[1].rsplit("/runs", 1)[0]
	runs = state.get("runs", {}).get(name, [])
	total = state.get("runs_total", {}).get(name, len(runs))
	query = path.split("?", 1)[1] if "?" in path else ""
	if ".." in query:
		runs = state.get("runs_window", {}).get(name, runs)
		total = state.get("runs_window_total", {}).get(name, len(runs))
	out({"workflow_runs": runs, "total_count": total})
if "/compare/" in route:
	if "compare" in fails:
		fail()
	head = route.rsplit("...", 1)[1]
	out(state.get("compare", {}).get(head, "behind"))
if "/actions/runs/" in route and route.endswith("/jobs"):
	if "jobs" in fails:
		fail()
	run_id = route.split("/actions/runs/", 1)[1].split("/", 1)[0]
	out(state.get("jobs", {}).get(run_id, {"jobs": []}))
number = None
parts = route.split("/")
if len(parts) >= 5 and parts[3] == "issues":
	number = int(parts[4])
if method == "PATCH" and number is not None:
	if "reopen" in fails:
		fail()
	if "state=closed" in args:
		state.setdefault("reclosed", []).append([number, [a for a in args if a.startswith("state")]])
	else:
		state.setdefault("reopened", []).append(number)
	out("{}")
if method == "POST" and route.endswith("/comments"):
	if "comment" in fails:
		fail()
	body_arg = next(a for a in args if a.startswith("body=@"))
	state.setdefault("comments", []).append({"issue": number, "body": open(body_arg[len("body=@"):]).read()})
	out("{}")
if method == "POST" and route.endswith("/labels"):
	if "label" in fails:
		fail()
	state.setdefault("labels", []).append([number, [a for a in args if a.startswith("labels[]=")]])
	out("[]")
fail()
'''

FAKE_CURL = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_CURL_LOG}"
printf '{"result":{"message_id":1}}'
'''


def _body(*, source: str = f"{SELF_REPO}#run", workflow: str = "CI", step: str = "Run tests",
		root: str = ROOT, branch: str = "stable") -> str:
	return "\n".join([
		f"<!-- workflow-failure-heal:fp={FP} -->",
		"<!-- workflow-failure-heal:gen=1 -->",
		f"<!-- workflow-failure-heal:root={root} -->",
		f"<!-- workflow-failure-heal:source={source} -->",
		"<!-- workflow-failure-heal:classification=workflow_bug -->",
		"",
		f"- **Target branch:** `{branch}`",
		"",
		f"- **Failed run:** https://github.com/{SELF_REPO}/actions/runs/111 — workflow `{workflow}`, step `{step}`",
	])


def _closed(number: int, *, body: str | None = None, comments: list | None = None, author: str = LOGIN,
		merged_at: datetime = MERGED_AT, closed_offset: int = 0) -> dict:
	return {
		"number": number, "url": f"https://github.com/{SELF_REPO}/issues/{number}",
		"body": body if body is not None else _body(),
		"createdAt": _iso(merged_at - timedelta(days=1)),
		"closedAt": _iso(merged_at + timedelta(minutes=1 + closed_offset)),
		"stateReason": "COMPLETED", "author": {"login": author},
		"closedByPullRequestsReferences": {"nodes": [{"number": 900 + number, "merged": True, "mergedAt": _iso(merged_at),
			"baseRefName": "stable", "mergeCommit": {"oid": MERGE_SHA}, "repository": {"nameWithOwner": SELF_REPO}}]},
		"comments": {"nodes": comments or []},
	}


def _marker_comment(outcome: str, created: datetime, *, author: str = LOGIN, run: int = 5, quoted: bool = False) -> dict:
	marker = f"<!-- workflow-failure-heal:verified:v1 outcome={outcome} run={run} -->"
	body = f"> {marker}\nquoted" if quoted else f"Verification.\n\n{marker}"
	return {"body": body, "createdAt": _iso(created), "author": {"login": author}}


def _run(run_id: int, sha: str, conclusion: str, minutes: int, **extra) -> dict:
	run = {"id": run_id, "status": "completed", "conclusion": conclusion, "head_branch": "stable", "head_sha": sha,
		"event": "push", "created_at": _iso(MERGED_AT + timedelta(minutes=minutes)),
		"head_repository": {"full_name": SELF_REPO}}
	run.update(extra)
	return run


def _jobs(step: str = "Run tests") -> dict:
	return {"jobs": [{"id": 77, "name": "test", "workflow_name": "CI", "conclusion": "failure",
		"steps": [{"name": "Set up job", "conclusion": "success"}, {"name": step, "conclusion": "failure"}]}]}


def _state(closed: list, *, open_nodes: list | None = None, runs: list | None = None, **extra) -> dict:
	state = {"graphql": {"data": {"viewer": {"login": LOGIN}, "repository": {
		"closed": {"pageInfo": {"hasNextPage": False}, "nodes": closed},
		"open": {"nodes": open_nodes or []}}}},
		"runs": {"ci.yml": runs or []}}
	state.update(extra)
	return state


def _execute(tmp_path: Path, state: dict, **env_overrides) -> tuple[subprocess.CompletedProcess, dict, list[str]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	(bin_dir / "gh").write_text(FAKE_GH)
	(bin_dir / "gh").chmod(0o755)
	(bin_dir / "curl").write_text(FAKE_CURL)
	(bin_dir / "curl").chmod(0o755)
	state_file = tmp_path / "state.json"
	state_file.write_text(json.dumps(state))
	curl_log = tmp_path / "curl.log"
	curl_log.write_text("")
	env = {key: value for key, value in os.environ.items() if not key.startswith(("WORKFLOW_HEAL", "TG_", "GH_RETRY"))}
	env.update({
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"FAKE_GH_STATE": str(state_file), "FAKE_CURL_LOG": str(curl_log),
		"GITHUB_REPOSITORY": SELF_REPO, "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "4242",
		"GITHUB_RUN_NUMBER": "0",
		"WORKFLOW_HEAL_VERIFY_ENABLED": "true", "RUNTIME_DIR": str(tmp_path / "runtime"),
		"TG_BOT_SECRET": "fake-secret", "TG_CHAT_ID": "1", "TG_ADMIN_CHAT_ID": "1",
		"ALERT_MSG_LEVEL": "WARNING", "TMPDIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1",
	})
	env.update(env_overrides)
	for key in [key for key, value in env.items() if value is None]:
		env.pop(key)
	result = subprocess.run(["bash", str(SCRIPT)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
	final = json.loads(state_file.read_text())
	return result, final, curl_log.read_text().splitlines()


def _rest_calls(state: dict) -> list[list[str]]:
	return [call for call in state.get("calls", []) if "graphql" not in call]


def _graphql_calls(state: dict) -> list[list[str]]:
	return [call for call in state.get("calls", []) if "graphql" in call]


def _last_line(text: str) -> str:
	return text.rstrip().split("\n")[-1]


# --- Switch and gates ------------------------------------------------------------


@pytest.mark.parametrize("value,reason", [(None, "reason=disabled"), ("false", "reason=disabled"),
	("", "reason=disabled"), ("FALSE", "reason=disabled"), ("maybe", "reason=invalid_switch")])
def test_switch_off_changes_nothing(tmp_path: Path, value, reason) -> None:
	result, state, curl = _execute(tmp_path, _state([_closed(10)], runs=[_run(1, MERGE_SHA, "success", 5)]),
		WORKFLOW_HEAL_VERIFY_ENABLED=value)
	assert result.returncode == 0, result.stderr
	assert reason in result.stdout
	assert "maybe" not in result.stdout + result.stderr
	assert state.get("calls", []) == [] and curl == []


def test_non_schedule_event_and_heal_switch_skip(tmp_path: Path) -> None:
	result, state, _ = _execute(tmp_path, _state([_closed(10)]), GITHUB_EVENT_NAME="workflow_dispatch")
	assert result.returncode == 0 and "reason=not_scheduled" in result.stdout
	assert state.get("calls", []) == []
	result, state, _ = _execute(tmp_path, _state([_closed(10)]), WORKFLOW_HEAL_ENABLED="false")
	assert result.returncode == 0 and "reason=heal_disabled" in result.stdout
	assert state.get("calls", []) == []


# --- Acceptance criteria ------------------------------------------------------------


def test_unrelated_older_commit_is_skipped(tmp_path: Path) -> None:
	runs = [_run(2, MERGE_SHA, "success", 30), _run(1, OTHER_SHA, "failure", 5)]
	result, state, _ = _execute(tmp_path, _state([_closed(10)], runs=runs, compare={OTHER_SHA: "behind"}))
	assert result.returncode == 0, result.stderr
	assert "skip_run issue=10 run=1 reason=unrelated_commit" in result.stdout
	assert "outcome=held issue=10 run=2" in result.stdout
	assert state.get("reopened") is None
	assert _last_line(state["comments"][0]["body"]) == "<!-- workflow-failure-heal:verified:v1 outcome=held run=2 -->"


def test_failing_first_run_then_passing_gives_recurred_and_reopen(tmp_path: Path) -> None:
	runs = [_run(3, MERGE_SHA, "success", 60), _run(2, MERGE_SHA, "failure", 10)]
	result, state, curl = _execute(tmp_path, _state([_closed(10)], runs=runs, jobs={"2": _jobs()}))
	assert result.returncode == 0, result.stderr
	assert "outcome=recurred issue=10 run=2" in result.stdout
	assert state["reopened"] == [10]
	body = state["comments"][0]["body"]
	assert body.startswith("## Fix did not hold")
	assert f"https://github.com/{SELF_REPO}/actions/runs/2" in body and "step `Run tests`" in body
	assert _last_line(body) == "<!-- workflow-failure-heal:verified:v1 outcome=recurred run=2 -->"
	assert state.get("labels") is None and curl == []


def test_lineage_heal_after_merge_gives_recurred_lineage_without_reopen(tmp_path: Path) -> None:
	successor = {"number": 20, "url": f"https://github.com/{SELF_REPO}/issues/20", "body": _body(),
		"createdAt": _iso(MERGED_AT + timedelta(hours=1)), "author": {"login": LOGIN}}
	forged = dict(successor, number=21, author={"login": "someone-else"})
	result, state, _ = _execute(tmp_path, _state([_closed(10)], open_nodes=[forged, successor],
		runs=[_run(2, MERGE_SHA, "failure", 10)], jobs={"2": _jobs()}))
	assert result.returncode == 0, result.stderr
	assert "outcome=recurred_lineage issue=10 lineage=20 run=2" in result.stdout
	assert state.get("reopened") is None
	body = state["comments"][0]["body"]
	assert "#20" in body and "#21" not in body
	assert _last_line(body) == "<!-- workflow-failure-heal:verified:v1 outcome=recurred_lineage run=2 -->"


def test_second_recurrence_escalates(tmp_path: Path) -> None:
	prior = _marker_comment("recurred", MERGED_AT - timedelta(days=1), run=1)
	result, state, curl = _execute(tmp_path, _state([_closed(10, comments=[prior])],
		runs=[_run(2, MERGE_SHA, "failure", 10)], jobs={"2": _jobs()}))
	assert result.returncode == 0, result.stderr
	assert state["reopened"] == [10]
	assert state["labels"] == [[10, ["labels[]=ai:workflow-heal-escalated"]]]
	assert "alert=critical issue=10 escalated=true" in result.stdout
	sends = "\n".join(curl)
	assert sends.count("sendMessage") == 1 and "CRITICAL" in sends and "fake-secret" not in result.stdout


def test_first_recurrence_from_other_author_does_not_escalate(tmp_path: Path) -> None:
	forged = _marker_comment("recurred", MERGED_AT - timedelta(days=1), author="someone-else")
	result, state, curl = _execute(tmp_path, _state([_closed(10, comments=[forged])],
		runs=[_run(2, MERGE_SHA, "failure", 10)], jobs={"2": _jobs()}))
	assert result.returncode == 0, result.stderr
	assert state["reopened"] == [10] and state.get("labels") is None and curl == []


def test_passing_next_run_posts_held_marker(tmp_path: Path) -> None:
	result, state, _ = _execute(tmp_path, _state([_closed(10)], runs=[_run(2, MERGE_SHA, "success", 10)]))
	assert result.returncode == 0, result.stderr
	assert "outcome=held issue=10 run=2" in result.stdout
	assert _last_line(state["comments"][0]["body"]) == "<!-- workflow-failure-heal:verified:v1 outcome=held run=2 -->"
	assert len(_rest_calls(state)) == 2  # runs list + comment


def test_already_verified_heal_is_skipped(tmp_path: Path) -> None:
	done = _marker_comment("held", MERGED_AT + timedelta(hours=2))
	result, state, _ = _execute(tmp_path, _state([_closed(10, comments=[done])], runs=[_run(2, MERGE_SHA, "success", 10)]))
	assert result.returncode == 0, result.stderr
	assert "outcome=skip issue=10 reason=already_verified" in result.stdout
	assert _rest_calls(state) == []


def test_untrusted_or_quoted_markers_do_not_count(tmp_path: Path) -> None:
	comments = [_marker_comment("held", MERGED_AT + timedelta(hours=2), author="someone-else"),
		_marker_comment("held", MERGED_AT + timedelta(hours=3), quoted=True)]
	result, state, _ = _execute(tmp_path, _state([_closed(10, comments=comments)], runs=[_run(2, MERGE_SHA, "success", 10)]))
	assert result.returncode == 0, result.stderr
	assert "outcome=held issue=10 run=2" in result.stdout


def test_per_tick_cap_is_honoured(tmp_path: Path) -> None:
	closed = [_closed(10 + i, closed_offset=i) for i in range(5)]
	result, state, _ = _execute(tmp_path, _state(closed, runs=[]), WORKFLOW_HEAL_VERIFY_MAX_PER_TICK="2")
	assert result.returncode == 0, result.stderr
	runs_calls = [call for call in _rest_calls(state) if any("/runs?" in a for a in call)]
	assert len(runs_calls) == 2
	assert "examined=2" in result.stdout and "candidates=5" in result.stdout
	assert "outcome=pending issue=10 reason=no_completed_run" in result.stdout
	assert "issue=12" not in result.stdout


def test_budget_one_list_query_and_at_most_three_reads_per_heal(tmp_path: Path) -> None:
	runs = [_run(3, DESC_SHA, "failure", 20), _run(1, OTHER_SHA, "failure", 5)]
	result, state, _ = _execute(tmp_path, _state([_closed(10)], runs=runs,
		compare={OTHER_SHA: "behind", DESC_SHA: "ahead"}, jobs={"3": _jobs()}))
	assert result.returncode == 0, result.stderr
	assert len(_graphql_calls(state)) == 1
	reads = [call for call in _rest_calls(state) if "PATCH" not in call and "POST" not in call]
	assert len(reads) <= 3
	# runs list + one compare, then the second compare would leave no room for the jobs read.
	assert "outcome=pending issue=10 reason=read_budget" in result.stdout
	assert state.get("comments") is None


def test_compare_plus_jobs_path_stays_within_three_reads(tmp_path: Path) -> None:
	runs = [_run(3, DESC_SHA, "failure", 20)]
	result, state, _ = _execute(tmp_path, _state([_closed(10)], runs=runs, compare={DESC_SHA: "ahead"}, jobs={"3": _jobs()}))
	assert result.returncode == 0, result.stderr
	reads = [call for call in _rest_calls(state) if "PATCH" not in call and "POST" not in call]
	assert len(reads) == 3
	assert "outcome=recurred issue=10 run=3" in result.stdout
	assert "reads=3" in result.stdout


# --- Extra cases ---------------------------------------------------------------


def test_fork_pull_request_and_cancelled_runs_are_ignored(tmp_path: Path) -> None:
	runs = [_run(1, MERGE_SHA, "failure", 5, head_repository={"full_name": "attacker/coding-workflows"}),
		_run(2, MERGE_SHA, "failure", 6, event="pull_request"),
		_run(3, MERGE_SHA, "cancelled", 7),
		_run(4, MERGE_SHA, "failure", 8, status="in_progress"),
		_run(5, MERGE_SHA, "failure", -30)]
	result, state, _ = _execute(tmp_path, _state([_closed(10)], runs=runs, jobs={str(i): _jobs() for i in range(1, 6)}))
	assert result.returncode == 0, result.stderr
	assert "outcome=pending issue=10 reason=no_completed_run" in result.stdout
	assert state.get("comments") is None and state.get("reopened") is None


def test_different_step_is_inconclusive_and_neutralised(tmp_path: Path) -> None:
	step = "Deploy <!-- workflow-failure-heal:verified:v1 outcome=held run=1 --> @someone `x`"
	result, state, _ = _execute(tmp_path, _state([_closed(10)], runs=[_run(2, MERGE_SHA, "failure", 10)], jobs={"2": _jobs(step)}))
	assert result.returncode == 0, result.stderr
	assert "outcome=inconclusive issue=10 run=2 reason=different_step" in result.stdout
	body = state["comments"][0]["body"]
	assert body.count("<!--") == 1
	assert "`x`" not in body
	assert _last_line(body) == "<!-- workflow-failure-heal:verified:v1 outcome=inconclusive run=2 -->"
	assert state.get("reopened") is None


def test_unsupported_and_unresolved_heals_cost_no_calls(tmp_path: Path) -> None:
	closed = [_closed(10, body=_body(source="shubhodeep1/example-consumer#run")),
		_closed(11, body=_body(source=f"{SELF_REPO}#42")),
		_closed(12, body=_body(workflow="Some Other Workflow")),
		_closed(13, author="someone-else")]
	result, state, _ = _execute(tmp_path, _state(closed, runs=[_run(2, MERGE_SHA, "success", 10)]))
	assert result.returncode == 0, result.stderr
	assert "outcome=skip issue=10 reason=unsupported_source" in result.stdout
	assert "outcome=skip issue=11 reason=unsupported_source" in result.stdout
	assert "outcome=skip issue=12 reason=workflow_unresolved" in result.stdout
	assert "outcome=skip issue=13 reason=untrusted_author" in result.stdout
	assert _rest_calls(state) == [] and len(_graphql_calls(state)) == 1


@pytest.mark.parametrize("failure,reason", [("graphql", "reason=list_failed"), ("runs", "reason=runs_failed"),
	("reopen", "reason=reopen_failed"), ("jobs", "reason=jobs_failed")])
def test_api_failures_fail_open_without_writes(tmp_path: Path, failure, reason) -> None:
	result, state, curl = _execute(tmp_path, _state([_closed(10)], runs=[_run(2, MERGE_SHA, "failure", 10)],
		jobs={"2": _jobs()}, fail=[failure]))
	assert result.returncode == 0, result.stderr
	assert reason in result.stdout
	assert state.get("comments") is None and state.get("reopened") is None and state.get("labels") is None
	assert curl == []


def test_truncated_runs_list_stays_pending(tmp_path: Path) -> None:
	result, state, _ = _execute(tmp_path, _state([_closed(10)], runs=[_run(2, MERGE_SHA, "success", 10)],
		runs_total={"ci.yml": 150}, runs_window_total={"ci.yml": 120}))
	assert result.returncode == 0, result.stderr
	assert "outcome=pending issue=10 reason=runs_truncated" in result.stdout
	assert state.get("comments") is None and state.get("reopened") is None


def test_truncated_runs_list_reads_the_first_day_after_the_merge(tmp_path: Path) -> None:
	newest = [_run(9, MERGE_SHA, "success", 600)]
	window = [_run(3, MERGE_SHA, "success", 30), _run(2, MERGE_SHA, "failure", 10)]
	result, state, _ = _execute(tmp_path, _state([_closed(10)], runs=newest, runs_total={"ci.yml": 150},
		runs_window={"ci.yml": window}, jobs={"2": _jobs()}))
	assert result.returncode == 0, result.stderr
	runs_calls = [call for call in _rest_calls(state) if any("/runs?" in a for a in call)]
	assert len(runs_calls) == 2
	window_end = _iso(MERGED_AT + timedelta(days=1)).replace(":", "%3A")
	assert any(window_end in a and ".." in a for a in runs_calls[1])
	assert "outcome=recurred issue=10 run=2" in result.stdout and "reads=3" in result.stdout


def test_start_rotates_with_run_number(tmp_path: Path) -> None:
	closed = [_closed(10 + i, closed_offset=i) for i in range(5)]
	result, _, _ = _execute(tmp_path, _state(closed, runs=[]), WORKFLOW_HEAL_VERIFY_MAX_PER_TICK="2",
		GITHUB_RUN_NUMBER="1")
	assert result.returncode == 0, result.stderr
	assert "outcome=pending issue=12 reason=no_completed_run" in result.stdout
	assert "outcome=pending issue=13 reason=no_completed_run" in result.stdout
	assert "issue=10 reason=no_completed_run" not in result.stdout


def test_comment_failure_after_reopen_closes_the_heal_again(tmp_path: Path) -> None:
	result, state, curl = _execute(tmp_path, _state([_closed(10)], runs=[_run(2, MERGE_SHA, "failure", 10)],
		jobs={"2": _jobs()}, fail=["comment"]))
	assert result.returncode == 0, result.stderr
	assert state["reopened"] == [10]
	assert state["reclosed"] == [[10, ["state=closed", "state_reason=completed"]]]
	assert "outcome=error issue=10 reason=comment_failed reclosed=true" in result.stdout
	assert "outcome=recurred" not in result.stdout
	assert state.get("labels") is None and curl == []


def test_heal_without_a_recorded_step_is_skipped(tmp_path: Path) -> None:
	result, state, _ = _execute(tmp_path, _state([_closed(10, body=_body(step="unknown step"))],
		runs=[_run(2, MERGE_SHA, "failure", 10)], jobs={"2": _jobs()}))
	assert result.returncode == 0, result.stderr
	assert "outcome=skip issue=10 reason=step_unknown" in result.stdout
	assert _rest_calls(state) == []


# --- Workflow wiring and registry ------------------------------------------------


def test_heal_verify_job_wiring() -> None:
	yaml = pytest.importorskip("yaml")
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	assert set(workflow["jobs"]) >= {"intake", "recovery-candidates", "recovery-scan", "heal-verify"}
	triggers = workflow.get("on", workflow.get(True))
	assert set(triggers) == {"repository_dispatch", "workflow_run", "schedule", "workflow_dispatch"}
	assert triggers["schedule"] == [{"cron": "41 * * * *"}]
	job = workflow["jobs"]["heal-verify"]
	assert job["if"] == ("github.event_name == 'schedule' && vars.WORKFLOW_HEAL_ENABLED != 'false' && "
		"vars.WORKFLOW_HEAL_VERIFY_ENABLED == 'true'")
	assert job["permissions"] == {"contents": "read", "issues": "write", "actions": "read"}
	env = job["env"]
	assert env["WORKFLOW_HEAL_VERIFY_ENABLED"] == "${{ vars.WORKFLOW_HEAL_VERIFY_ENABLED || 'false' }}"
	assert env["WORKFLOW_HEAL_VERIFY_MAX_PER_TICK"] == "${{ vars.WORKFLOW_HEAL_VERIFY_MAX_PER_TICK || '3' }}"
	assert env["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	names = [step["name"] for step in job["steps"]]
	assert names == ["Record GH_PAT budget at heal-verify start", "Checkout repository",
		"Run heal post-merge verification", "Record GH_PAT budget at heal-verify end"]
	assert job["steps"][-1]["if"] == "always()"
	assert job["steps"][1]["with"]["persist-credentials"] is False
	assert "bash scripts/workflow_failure_heal_verify.sh" in job["steps"][2]["run"]


def test_workflow_text_wires_the_script() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	assert "\n  heal-verify:\n" in text
	assert text.count("bash scripts/workflow_failure_heal_verify.sh") == 1
	assert "WORKFLOW_HEAL_VERIFY_ENABLED: ${{ vars.WORKFLOW_HEAL_VERIFY_ENABLED || 'false' }}" in text


def test_removal_registry_entry() -> None:
	text = REMOVAL_REGISTRY.read_text(encoding="utf-8")
	start = text.index("### `scripts/workflow_failure_heal_verify.sh`")
	entry = text[start:].split("\n### ", 1)[0]
	assert "- **Type:** long-running" in entry
	assert "- **Removal trigger:** permanent — review annually" in entry
	assert "grep -rn workflow_failure_heal_verify .github/workflows scripts" in entry
	assert "heal-verify" in entry and "- **Owner:** @shubhodeep1" in entry

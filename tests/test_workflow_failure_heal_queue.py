#!/usr/bin/env python3
"""Heal intake report queue (scripts/workflow_failure_heal_queue.sh).

The intake shared one concurrency group, and GitHub cancels a group's pending
run when a newer one is queued, so bursts of heal reports were lost before
they were read (about 15 cancelled intake runs on 2026-10-21). Runs now wait
their turn in a first-come, first-served queue of ticket comments instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
QUEUE_SCRIPT = SCRIPTS_DIR / "workflow_failure_heal_queue.sh"
INTAKE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow-failure-heal-intake.yml"
sys.path.insert(0, str(SCRIPTS_DIR))

import workflow_failure_heal as heal  # noqa: E402

SELF_REPO = "shubhodeep1/coding-workflows"
LOGIN = "workflow-bot"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
	return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ticket(comment_id: int, run_id: int, *, attempt: int = 1, job: str = "intake", login: str = LOGIN, age_seconds: int = 60, now: datetime = NOW) -> dict:
	return {"id": comment_id, "user": {"login": login}, "created_at": _iso(now - timedelta(seconds=age_seconds)),
		"body": heal.render_heal_queue_ticket(run_id, attempt, job)}


def _position(comments, ticket_id: int, **kwargs) -> dict:
	return heal.heal_queue_position(comments=comments, ticket_id=ticket_id, trusted_login=LOGIN, lease_seconds=kwargs.pop("lease", 10_800), now=NOW, **kwargs)


# --- Position decisions --------------------------------------------------------


def test_ticket_round_trip_and_rejects_bad_fields() -> None:
	body = heal.render_heal_queue_ticket(42, 2, "recovery-scan")
	assert body.splitlines()[-1] == "<!-- ai:heal-intake-queue:v1 run=42 attempt=2 job=recovery-scan -->"
	tickets = heal.heal_queue_tickets([{"id": 5, "user": {"login": LOGIN}, "body": body, "created_at": _iso(NOW)}], LOGIN)
	assert tickets == [{"comment_id": 5, "run_id": "42", "attempt": 2, "job": "recovery-scan", "created_at": _iso(NOW)}]
	for run_id, attempt, job in ((0, 1, "intake"), ("x", 1, "intake"), (1, 0, "intake"), (1, 1, "other")):
		try:
			heal.render_heal_queue_ticket(run_id, attempt, job)
		except ValueError:
			continue
		raise AssertionError((run_id, attempt, job))


def test_first_ticket_goes_and_later_ones_wait_in_comment_order() -> None:
	comments = [_ticket(30, 103), _ticket(10, 101), _ticket(20, 102)]
	assert _position(comments, 10)["status"] == "go"
	second = _position(comments, 20)
	assert second["status"] == "wait" and second["position"] == 1 and second["holder"]["run_id"] == "101"
	third = _position(comments, 30)
	assert third["position"] == 2 and third["holder"]["comment_id"] == 10
	# gh --paginate --slurp pages are flattened.
	assert _position([[_ticket(10, 101)], [_ticket(20, 102)]], 20)["holder"]["run_id"] == "101"


def test_only_well_formed_pipeline_tickets_count() -> None:
	foreign = _ticket(10, 101, login="someone")
	not_last_line = dict(_ticket(11, 102), body=heal.render_heal_queue_ticket(102, 1, "intake") + "\nmore text")
	prose = {"id": 12, "user": {"login": LOGIN}, "body": "please wait", "created_at": _iso(NOW)}
	bool_id = dict(_ticket(13, 103), id=True)
	assert _position([foreign, not_last_line, prose, bool_id, _ticket(20, 200)], 20) == {"status": "go", "position": 0, "holder": None, "stale": []}
	# Login comparison ignores case.
	assert _position([_ticket(10, 101, login=LOGIN.upper()), _ticket(20, 200)], 20)["status"] == "wait"


def test_stale_tickets_do_not_hold_the_queue() -> None:
	cases = [
		("lease", [_ticket(10, 101, age_seconds=20_000)], {}),
		("completed", [_ticket(10, 101)], {"101": {"status": "completed", "run_attempt": 1}}),
		("re_attempted", [_ticket(10, 101, attempt=1)], {"101": {"status": "in_progress", "run_attempt": 2}}),
		("own_earlier_attempt", [_ticket(10, 200, attempt=1)], {}),
		("own_duplicate", [_ticket(10, 200, attempt=2)], {}),
		("no_timestamp", [dict(_ticket(10, 101), created_at=None)], {}),
	]
	for name, ahead, runs in cases:
		result = _position([*ahead, _ticket(20, 200, attempt=2)], 20, runs=runs)
		assert result == {"status": "go", "position": 0, "holder": None, "stale": [10]}, name
	# A live holder whose run is still running keeps its place.
	live = _position([_ticket(10, 101), _ticket(20, 200)], 20, runs={"101": {"status": "in_progress", "run_attempt": 1}})
	assert live["status"] == "wait" and live["stale"] == []


def test_lost_ticket() -> None:
	assert _position([_ticket(10, 101)], 20)["status"] == "lost"


def test_position_cli(tmp_path: Path) -> None:
	# The CLI measures ticket age against the wall clock with the default
	# lease, so these tickets are dated from the real time, not the fixed NOW
	# (#7994: NOW-dated tickets went stale three hours after NOW).
	wall = datetime.now(timezone.utc)
	comments = tmp_path / "comments.json"
	comments.write_text(json.dumps([[_ticket(10, 101, age_seconds=5, now=wall)], [_ticket(20, 200, age_seconds=1, now=wall)]]), encoding="utf-8")
	runs = tmp_path / "runs.json"
	runs.write_text(json.dumps({"101": {"status": "completed"}}), encoding="utf-8")
	env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
	base = [sys.executable, str(SCRIPTS_DIR / "workflow_failure_heal.py"), "heal-queue", "position", "--comments-json", str(comments), "--ticket-id", "20", "--login", LOGIN,
		"--lease-seconds", "10800"]
	waiting = json.loads(subprocess.run(base, capture_output=True, text=True, env=env, check=True).stdout)
	assert waiting["status"] == "wait" and waiting["holder"]["run_id"] == "101"
	going = json.loads(subprocess.run([*base, "--runs-json", str(runs)], capture_output=True, text=True, env=env, check=True).stdout)
	assert going["status"] == "go" and going["stale"] == [10]
	# A holder past the default 10,800-second lease no longer holds the queue.
	expired = tmp_path / "expired.json"
	expired.write_text(json.dumps([_ticket(10, 101, age_seconds=20_000, now=wall), _ticket(20, 200, age_seconds=1, now=wall)]), encoding="utf-8")
	lapsed = json.loads(subprocess.run([*base[:base.index("--comments-json") + 1], str(expired), *base[base.index("--comments-json") + 2:]],
		capture_output=True, text=True, env=env, check=True).stdout)
	assert lapsed["status"] == "go" and lapsed["stale"] == [10]
	ticket = subprocess.run([sys.executable, str(SCRIPTS_DIR / "workflow_failure_heal.py"), "heal-queue", "ticket", "--run-id", "7", "--attempt", "3", "--job", "intake"],
		capture_output=True, text=True, env=env, check=True).stdout
	assert ticket.strip().endswith("<!-- ai:heal-intake-queue:v1 run=7 attempt=3 job=intake -->")


# --- The queue script against a fake gh ---------------------------------------------

MOCK_GH = r'''#!/usr/bin/env python3
import json, os, sys
from datetime import datetime, timezone
state_path = os.environ["MOCK_GH_STATE"]
with open(state_path, encoding="utf-8") as handle:
	state = json.load(handle)
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def save():
	with open(state_path, "w", encoding="utf-8") as handle:
		json.dump(state, handle)
def out(text, code=0):
	save()
	sys.stdout.write(text)
	sys.exit(code)
def fail(message):
	save()
	sys.stderr.write(message + "\n")
	sys.exit(1)
def opt(name):
	return args[args.index(name) + 1] if name in args else None
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
if args[:2] == ["label", "create"]:
	out("")
if args[:2] == ["issue", "create"]:
	if state.get("issue_create_fail"):
		fail("HTTP 500")
	number = state.get("next_issue", 900)
	state["next_issue"] = number + 1
	state.setdefault("issues", []).append({"number": number, "user": {"login": state["login"]}, "pull_request": None, "labels": [{"name": opt("--label")}]})
	state.setdefault("created_issues", []).append(number)
	out("https://github.com/%s/issues/%d\n" % (opt("--repo"), number))
if args[:2] == ["issue", "close"]:
	state.setdefault("closed_issues", []).append(int(args[2]))
	out("")
if args[0] != "api":
	fail("unexpected gh call " + " ".join(args))
method = opt("--method") or "GET"
path = next(a for a in args[1:] if a.startswith(("repos/", "user")))
if path == "user":
	if state.get("user_fail"):
		fail("HTTP 401")
	out(state["login"] + "\n")
if path.endswith("/issues") and method == "GET":
	if state.get("issue_list_fail"):
		fail("HTTP 502")
	out(json.dumps(state.get("issues", [])))
if path.endswith("/comments") and method == "POST":
	if state.get("post_fail"):
		fail("HTTP 502")
	comment_id = state.get("next_comment", 1000)
	state["next_comment"] = comment_id + 1
	body = opt("-f")[len("body="):]
	state.setdefault("comments", []).append({"id": comment_id, "user": {"login": state["login"]}, "body": body, "created_at": now})
	out(str(comment_id) + "\n")
if "/comments?" in path and method == "GET":
	state["reads"] = state.get("reads", 0) + 1
	for rule in state.get("delete_after_reads", []):
		if state["reads"] >= rule["reads"]:
			state["comments"] = [c for c in state.get("comments", []) if c["id"] != rule["id"]]
	if state.get("comments_fail"):
		fail("HTTP 502")
	out(json.dumps([state.get("comments", [])]))
if "/issues/comments/" in path and method == "DELETE":
	comment_id = int(path.rsplit("/", 1)[-1])
	state.setdefault("deleted", []).append(comment_id)
	state["comments"] = [c for c in state.get("comments", []) if c["id"] != comment_id]
	out("")
if "/actions/runs/" in path and method == "GET":
	run = state.get("runs", {}).get(path.rsplit("/", 1)[-1])
	if run is None:
		fail("HTTP 404")
	out(json.dumps(run))
fail("unexpected api call " + " ".join(args))
'''


def _stage(tmp: Path, state: dict) -> tuple[dict[str, str], Path, Path]:
	scripts = tmp / "scripts"
	scripts.mkdir()
	for name in ("workflow_failure_heal_queue.sh", "workflow_failure_heal.py"):
		shutil.copy(SCRIPTS_DIR / name, scripts / name)
	tg_log = tmp / "tg.log"
	(scripts / "tg_helpers.sh").write_text(f"tg_send_msg() {{ printf '%s\\t%s\\n' \"${{2:-}}\" \"${{1//$'\\n'/ }}\" >> '{tg_log}'; }}\n", encoding="utf-8")
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(MOCK_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	state_file = tmp / "state.json"
	state_file.write_text(json.dumps({"login": LOGIN, **state}), encoding="utf-8")
	github_env = tmp / "github_env"
	github_env.write_text("", encoding="utf-8")
	env = {
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"HOME": str(tmp),
		"RUNNER_TEMP": str(tmp),
		"MOCK_GH_STATE": str(state_file),
		"GITHUB_REPOSITORY": SELF_REPO,
		"GITHUB_RUN_ID": "500",
		"GITHUB_RUN_ATTEMPT": "1",
		"GITHUB_ENV": str(github_env),
		"HEAL_INTAKE_QUEUE_POLL_SECONDS": "0",
		"HEAL_INTAKE_QUEUE_HOLDER_CHECK_SECONDS": "0",
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	return env, state_file, tg_log


def _run_queue(tmp: Path, state: dict, mode: str = "acquire", extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict, dict[str, str], str]:
	env, state_file, tg_log = _stage(tmp, state)
	env.update(extra_env or {})
	result = subprocess.run(["bash", str(tmp / "scripts" / "workflow_failure_heal_queue.sh"), mode], env=env, capture_output=True, text=True, check=False, timeout=60)
	exported = dict(line.split("=", 1) for line in Path(env["GITHUB_ENV"]).read_text(encoding="utf-8").splitlines() if "=" in line)
	return result, json.loads(state_file.read_text(encoding="utf-8")), exported, tg_log.read_text(encoding="utf-8") if tg_log.exists() else ""


QUEUE_ISSUE = {"number": 77, "user": {"login": LOGIN}, "pull_request": None}


def _live_ticket(comment_id: int, run_id: int) -> dict:
	return {"id": comment_id, "user": {"login": LOGIN}, "body": heal.render_heal_queue_ticket(run_id, 1, "intake"), "created_at": _iso(datetime.now(timezone.utc))}


def test_empty_queue_enters_at_once_and_release_deletes_the_ticket(tmp_path: Path) -> None:
	result, state, exported, tg = _run_queue(tmp_path, {"issues": [QUEUE_ISSUE]})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "WORKFLOW_HEAL_QUEUE enqueued issue=77 ticket=1000 job=intake run=500 attempt=1" in result.stdout
	assert "WORKFLOW_HEAL_QUEUE acquired issue=77 ticket=1000 job=intake run=500 waited_seconds=" in result.stdout
	assert exported["HEAL_INTAKE_QUEUE_ISSUE"] == "77" and exported["HEAL_INTAKE_QUEUE_TICKET_ID"] == "1000"
	assert exported["HEAL_INTAKE_QUEUE_WAITED_SECONDS"].isdigit()
	assert state["comments"][0]["body"].endswith("<!-- ai:heal-intake-queue:v1 run=500 attempt=1 job=intake -->")
	# user, issue list, ticket post, one comment read (CLAUDE.md §15).
	assert len(state["calls"]) == 4, state["calls"]
	assert tg == ""
	release_dir = tmp_path / "release"
	release_dir.mkdir()
	released, state, _, _ = _run_queue(release_dir, {"comments": state["comments"]}, "release", {"HEAL_INTAKE_QUEUE_TICKET_ID": "1000"})
	assert "WORKFLOW_HEAL_QUEUE outcome=released ticket=1000" in released.stdout
	assert state["deleted"] == [1000] and state["comments"] == []


def test_waits_for_the_holder_then_enters(tmp_path: Path) -> None:
	state = {"issues": [QUEUE_ISSUE], "comments": [_live_ticket(10, 401)], "next_comment": 20,
		"runs": {"401": {"id": 401, "status": "in_progress", "run_attempt": 1}},
		"delete_after_reads": [{"id": 10, "reads": 3}]}
	result, state, exported, _ = _run_queue(tmp_path, state)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "WORKFLOW_HEAL_QUEUE waiting issue=77 ticket=20 position=1 holder_run=401 job=intake run=500" in result.stdout
	assert "WORKFLOW_HEAL_QUEUE acquired issue=77 ticket=20" in result.stdout
	assert state["reads"] == 3
	# The holder ticket was released by its own run, not deleted here.
	assert "deleted" not in state
	assert "HEAL_INTAKE_QUEUE_WAITED_SECONDS" in exported


def test_completed_holder_is_cleared_without_waiting(tmp_path: Path) -> None:
	state = {"issues": [QUEUE_ISSUE], "comments": [_live_ticket(10, 401), _live_ticket(11, 402)], "next_comment": 20,
		"runs": {"401": {"id": 401, "status": "completed", "run_attempt": 1}, "402": {"id": 402, "status": "completed", "run_attempt": 1}}}
	result, state, _, _ = _run_queue(tmp_path, state)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "acquired issue=77 ticket=20" in result.stdout
	assert sorted(state["deleted"]) == [10, 11]
	assert "WORKFLOW_HEAL_QUEUE stale_ticket_removed ticket=10 issue=77" in result.stdout


def test_creates_the_queue_issue_when_none_is_open(tmp_path: Path) -> None:
	foreign = {"number": 5, "user": {"login": "someone"}, "pull_request": None}
	result, state, exported, _ = _run_queue(tmp_path, {"issues": [foreign], "next_issue": 901})
	assert result.returncode == 0, result.stdout + result.stderr
	assert state["created_issues"] == [901] and exported["HEAL_INTAKE_QUEUE_ISSUE"] == "901"
	assert "WORKFLOW_HEAL_QUEUE issue_created issue=901" in result.stdout
	create = next(call for call in state["calls"] if call[:2] == ["issue", "create"])
	assert create[create.index("--label") + 1] == heal.HEAL_QUEUE_LABEL


def test_never_drops_the_report_when_the_queue_is_unusable(tmp_path: Path) -> None:
	live = {"issues": [QUEUE_ISSUE], "comments": [_live_ticket(10, 401)], "next_comment": 20, "runs": {"401": {"id": 401, "status": "in_progress", "run_attempt": 1}}}
	cases = [
		("identity_unavailable", {"user_fail": True}, {}),
		("issue_list_failed", {"issue_list_fail": True}, {}),
		("issue_create_failed", {"issue_create_fail": True}, {}),
		("ticket_post_failed", {"issues": [QUEUE_ISSUE], "post_fail": True}, {}),
		("comments_unavailable", {"issues": [QUEUE_ISSUE], "comments_fail": True}, {}),
		("ticket_lost", {"issues": [QUEUE_ISSUE], "delete_after_reads": [{"id": 1000, "reads": 1}]}, {}),
		("max_wait", live, {"HEAL_INTAKE_QUEUE_MAX_WAIT_SECONDS": "0"}),
		("missing_context", {}, {"GITHUB_RUN_ID": ""}),
	]
	for index, (reason, state, extra_env) in enumerate(cases):
		case_dir = tmp_path / str(index)
		case_dir.mkdir()
		result, _, exported, tg = _run_queue(case_dir, state, extra_env=extra_env)
		assert result.returncode == 0, reason + result.stdout + result.stderr
		assert f"WORKFLOW_HEAL_QUEUE outcome=unlocked reason={reason} " in result.stdout, reason + result.stdout
		assert "HEAL_INTAKE_QUEUE_WAITED_SECONDS" in exported, reason
		assert tg.startswith("WARNING\t") and reason in tg, (reason, tg)


def test_disabled_queue_and_ticketless_release_make_no_calls(tmp_path: Path) -> None:
	for index, value in enumerate(("false", "FALSE")):
		case_dir = tmp_path / f"off-{index}"
		case_dir.mkdir()
		result, state, exported, _ = _run_queue(case_dir, {}, extra_env={"HEAL_INTAKE_QUEUE_ENABLED": value})
		assert "WORKFLOW_HEAL_QUEUE outcome=skip reason=disabled" in result.stdout
		assert "calls" not in state and exported == {}
	case_dir = tmp_path / "release"
	case_dir.mkdir()
	result, state, _, _ = _run_queue(case_dir, {}, "release")
	assert "WORKFLOW_HEAL_QUEUE outcome=skip reason=no_ticket" in result.stdout and "calls" not in state


# --- Wiring ----------------------------------------------------------------------


def test_workflow_uses_per_run_groups_and_queue_steps() -> None:
	workflow = yaml.safe_load(INTAKE_WORKFLOW.read_text(encoding="utf-8"))
	assert workflow["concurrency"]["group"] == (
		"${{ github.event_name == 'schedule' && format('workflow-failure-heal-recovery-list-{0}', github.run_id) "
		"|| vars.HEAL_INTAKE_QUEUE_ENABLED != 'false' && format('workflow-failure-heal-intake-run-{0}', github.run_id) "
		"|| 'workflow-failure-heal-intake' }}"
	)
	assert workflow["concurrency"]["cancel-in-progress"] is False
	scan = workflow["jobs"]["recovery-scan"]
	assert scan["concurrency"]["group"] == "${{ vars.HEAL_INTAKE_QUEUE_ENABLED != 'false' && format('workflow-failure-heal-recovery-scan-{0}', github.run_id) || 'workflow-failure-heal-intake' }}"
	for job_name, run_step, job_token, timeout in (("intake", "Run workflow failure heal intake", "intake", 130), ("recovery-scan", "Run heal-scope recovery scan", "recovery-scan", 175)):
		job = workflow["jobs"][job_name]
		assert job["timeout-minutes"] == timeout
		assert job["env"]["HEAL_INTAKE_QUEUE_ENABLED"] == "${{ vars.HEAL_INTAKE_QUEUE_ENABLED || 'true' }}"
		assert job["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
		names = [step.get("name") for step in job["steps"]]
		acquire = job["steps"][names.index("Wait for the heal intake queue")]
		release = job["steps"][names.index("Leave the heal intake queue")]
		assert acquire["run"] == "bash scripts/workflow_failure_heal_queue.sh acquire" and acquire["env"]["HEAL_INTAKE_QUEUE_JOB"] == job_token
		assert release["run"] == "bash scripts/workflow_failure_heal_queue.sh release" and release["env"]["HEAL_INTAKE_QUEUE_JOB"] == job_token
		assert release["if"] == "always()" and "continue-on-error" not in acquire
		# Queued before the Claude credential is resolved (it must not age while
		# waiting) and released right after the work.
		assert names.index("Checkout repository") < names.index("Wait for the heal intake queue") < names.index("Install Codex CLI") < names.index("Resolve Claude credential")
		assert names.index(run_step) + 1 == names.index("Leave the heal intake queue")
	assert workflow["jobs"]["intake"]["steps"][[s.get("name") for s in workflow["jobs"]["intake"]["steps"]].index("Wait for the heal intake queue") - 1]["id"] == "payload"


def test_intake_does_not_count_queue_time_against_the_report() -> None:
	intake = (SCRIPTS_DIR / "workflow_failure_heal_intake.sh").read_text(encoding="utf-8")
	assert 'QUEUE_WAITED_SECONDS="$(_positive_or_default "${HEAL_INTAKE_QUEUE_WAITED_SECONDS:-}" 0)"' in intake
	assert "REPORT_MAX_AGE=$((10#${REPORT_MAX_AGE} + 10#${QUEUE_WAITED_SECONDS}))" in intake
	assert intake.index("REPORT_MAX_AGE=$((10#${REPORT_MAX_AGE} + 10#${QUEUE_WAITED_SECONDS}))") < intake.index("--max-age \"${REPORT_MAX_AGE}\"")


def test_queue_script_is_staged_from_the_trusted_checkout() -> None:
	# Both jobs check out this repository at the workflow's own ref and run the
	# script from it; the queue never runs code from a report.
	text = QUEUE_SCRIPT.read_text(encoding="utf-8")
	assert "set -uo pipefail" in text and "exit 0" in text
	# gh_retry retries a DELETE by default; the idempotent marker must never ride
	# on a /comments line (tests/test_workflow_gh_retry_fallback_contract.py).
	assert "--method DELETE" in text and "GH_RETRY_IDEMPOTENT=true" not in text
	assert os.access(QUEUE_SCRIPT, os.X_OK)

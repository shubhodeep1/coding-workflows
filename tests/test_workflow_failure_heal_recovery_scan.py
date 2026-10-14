"""Tests for scripts/workflow_failure_heal_recovery_scan.sh (#7333).

The scan runs against a fake `gh` and a stub intake; the real intake's
`recovery_scan` handling is covered in tests/test_workflow_failure_heal.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCAN_SCRIPT = SCRIPTS_DIR / "workflow_failure_heal_recovery_scan.sh"
sys.path.insert(0, str(SCRIPTS_DIR))
import workflow_failure_heal as heal  # noqa: E402

SELF_REPO = "shubhodeep1/coding-workflows"
CONSUMER_REPO = "shubhodeep1/example-consumer"
LOGIN = "workflow-bot"
SHA_B = "b" * 40
REASON = "conflict_resolver_sandbox_path_unsupported"
EVIDENCE = (
	f"failure_reason={REASON}\n"
	"::error::Conflict resolver: conflicted path(s) cannot enter the sandbox: unsafe=1 host_only=0 categories=symlink depths=1\n"
)

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
if args[:2] == ["issue", "close"]:
	if state.get("close_fail"):
		fail()
	state.setdefault("closed", []).append(args[2:])
	out("")
if args[:1] != ["api"]:
	fail()
rest = [a for a in args[1:] if a not in ("--method", "GET", "--paginate")]
if "user" in rest:
	if state.get("user_fail"):
		fail()
	out(state["login"])
if rest and rest[0] == "graphql" and any("issue(number" in a for a in args):
	number = int(next(a for a in args if a.startswith("number="))[len("number="):])
	node = next((n for n in state.get("nodes", []) if n["number"] == number), None)
	if node is None or state.get("issue_read_fail"):
		fail()
	issue = state.get("fresh", {}).get(str(number), {})
	out({"data": {"repository": {"issue": {"state": issue.get("state", "OPEN"), "body": issue.get("body", node["body"]),
		"lastEditedAt": issue.get("lastEditedAt", node.get("lastEditedAt"))}}}})
if rest and rest[0] == "graphql":
	if state.get("list_fail"):
		fail()
	out({"data": {"repository": {"issues": {"nodes": state.get("nodes", [])}}}})
path = next((a for a in rest if a.startswith("repos/")), "")
if path.endswith("/actions/workflows/workflow-failure-heal-intake.yml/runs"):
	if state.get("runs_fail"):
		fail()
	out({"workflow_runs": state.get("runs", [])})
if path.endswith("/comments") and any(a.startswith("body=@") for a in args):
	if state.get("comment_post_fail"):
		fail()
	body = open(next(a for a in args if a.startswith("body=@"))[len("body=@"):]).read()
	state.setdefault("posted", []).append({"path": path, "body": body})
	out("{}")
if path.endswith("/comments"):
	save()
	for comment in state.get("comments", {}).get(path, []):
		sys.stdout.write(json.dumps(comment) + "\n")
	sys.exit(0)
if "/pulls/" in path:
	pull = state.get("pulls", {}).get(path)
	if pull is None:
		fail()
	out(pull)
fail()
'''

STUB_INTAKE = r'''#!/usr/bin/env bash
set -euo pipefail
env | grep -E '^(WORKFLOW_HEAL_REPORT_ORIGIN|WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_FP|WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_ISSUE|WORKFLOW_HEAL_PAYLOAD_FILE|RUNTIME_DIR|WORKFLOW_HEAL_REPORT_IDENTITY_STATE)=' > "${STUB_INTAKE_ENV}"
cp "${WORKFLOW_HEAL_PAYLOAD_FILE}" "${STUB_INTAKE_ENV}.payload"
printf '%s\n' "${STUB_INTAKE_OUTPUT}"
exit "${STUB_INTAKE_RC:-0}"
'''


def _pr() -> dict:
	return {"number": 4174, "title": "AI implementation for issue #4173", "body": "Automated implementation.",
		"html_url": f"https://github.com/{CONSUMER_REPO}/pull/4174", "labels": [],
		"head": {"ref": "ai/issue-4173", "sha": SHA_B, "repo": {"full_name": CONSUMER_REPO}}, "base": {"ref": "main"}}


def _heal_issue(number: int = 31, *, author: str = LOGIN, extra: str = "") -> dict:
	payload = heal.build_autofix_failure_payload(
		repo=CONSUMER_REPO, pr=_pr(), comments=[], workflow_name="AI Review", failure_reason=REASON, failure_evidence=EVIDENCE,
		failure_streak=1, run_id="500", run_url=f"https://github.com/{CONSUMER_REPO}/actions/runs/500", wrapper_sha=None, reporter_run_url=None,
	)
	fp = "c" * 64
	summaries = [{"url": f"https://github.com/{CONSUMER_REPO}/actions/runs/500", "workflow_name": "AI Review", "failing_step": "Resolve conflicts"}]
	body = heal.compose_issue_body(payload=payload, diagnosis="## Classification\nworkflow-defect\n", fp=fp, gen=1, root=fp, classification="workflow-defect",
		target_branch="stable", max_depth=3, intake_run_url=f"https://github.com/{SELF_REPO}/actions/runs/1", run_summaries=summaries)
	return {"number": number, "title": heal.compose_issue_title(payload, workflow_name="AI Review"), "body": body + extra,
		"lastEditedAt": None, "url": f"https://github.com/{SELF_REPO}/issues/{number}", "author": {"login": author}}


def _scan_comment(reason: str = "fingerprint_mismatch", author: str = LOGIN) -> dict:
	return {"id": 1, "body": "attempt\n\n" + heal.render_heal_scope_recovery_scan_marker(reason, "9"), "user": {"login": author}}


def _run(tmp: Path, state: dict, *, args: tuple[str, ...] = (), env: dict[str, str] | None = None, intake_output: str = "WORKFLOW_HEAL skip reason=provenance_rejected", intake_rc: int = 0):
	work = tmp / "work"
	(work / "scripts").mkdir(parents=True)
	shutil.copy(SCAN_SCRIPT, work / "scripts" / SCAN_SCRIPT.name)
	shutil.copy(SCRIPTS_DIR / "workflow_failure_heal.py", work / "scripts" / "workflow_failure_heal.py")
	tg_log = tmp / "tg.log"
	(work / "scripts" / "tg_helpers.sh").write_text(f"tg_send_msg() {{ printf '%s\\t%s\\n' \"${{2:-}}\" \"${{1//$'\\n'/ }}\" >> '{tg_log}'; }}\n", encoding="utf-8")
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	intake = tmp / "stub_intake.sh"
	intake.write_text(STUB_INTAKE, encoding="utf-8")
	state_file = tmp / "state.json"
	state_file.write_text(json.dumps(state), encoding="utf-8")
	output = tmp / "github_output"
	full_env = {
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"HOME": str(tmp),
		"FAKE_GH_STATE": str(state_file),
		"GITHUB_REPOSITORY": SELF_REPO,
		"GITHUB_EVENT_NAME": "schedule",
		"GITHUB_RUN_ID": "4242",
		"GITHUB_OUTPUT": str(output),
		"RUNTIME_DIR": str(tmp / "runtime"),
		"HEAL_SCOPE_RECOVERY_SCAN_INTAKE_SCRIPT": str(intake),
		"STUB_INTAKE_ENV": str(tmp / "intake_env"),
		"STUB_INTAKE_OUTPUT": intake_output,
		"STUB_INTAKE_RC": str(intake_rc),
		"WORKFLOW_HEAL_REPORT_IDENTITY_STATE": "present",
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	full_env.update(env or {})
	result = subprocess.run(["bash", str(work / "scripts" / SCAN_SCRIPT.name), *args], cwd=work, env=full_env, capture_output=True, text=True, check=False)
	return result, json.loads(state_file.read_text(encoding="utf-8")), output, tmp / "intake_env", tg_log


def _state(nodes: list[dict], comments: dict | None = None) -> dict:
	return {
		"login": LOGIN,
		"nodes": nodes,
		"comments": comments or {},
		"pulls": {f"repos/{CONSUMER_REPO}/pulls/4174": _pr()},
	}


def test_list_only_counts_markerless_issues_by_the_pipeline_login() -> None:
	nodes = [_heal_issue(31), _heal_issue(32, extra="\n<!-- ai:workflow-heal-scope:v1 paths=scripts/x.py runs=o/r:1 -->"), _heal_issue(33, author="mallory")]
	with tempfile.TemporaryDirectory(prefix="heal-scan-list-") as tmp_name:
		result, state, output, intake_env, _ = _run(Path(tmp_name), _state(nodes), args=("--list-only",))
		assert result.returncode == 0, result.stderr + result.stdout
		assert output.read_text(encoding="utf-8") == "count=1\n"
		assert not intake_env.exists()
		assert not [call for call in state["calls"] if "/comments" in " ".join(call)]


def test_list_only_drops_exhausted_issues_and_skips_while_an_intake_is_active() -> None:
	exhausted = _heal_issue(31)
	exhausted["comments"] = {"nodes": [{"body": c["body"], "author": {"login": c["user"]["login"]}} for c in (_scan_comment(), _scan_comment(), _scan_comment())]}
	two = _heal_issue(32)
	two["comments"] = {"nodes": [{"body": c["body"], "author": {"login": c["user"]["login"]}} for c in (_scan_comment(), _scan_comment(author="mallory"))]}
	with tempfile.TemporaryDirectory(prefix="heal-scan-list-exhausted-") as tmp_name:
		result, _, output, _, _ = _run(Path(tmp_name), _state([exhausted, two]), args=("--list-only",))
		assert result.returncode == 0, result.stderr + result.stdout
		assert output.read_text(encoding="utf-8") == "count=1\n"
	with tempfile.TemporaryDirectory(prefix="heal-scan-list-exhausted-only-") as tmp_name:
		result, state, output, _, _ = _run(Path(tmp_name), _state([exhausted]), args=("--list-only",))
		assert output.read_text(encoding="utf-8") == "count=0\n"
		assert not [call for call in state["calls"] if any("/runs" in a for a in call)]
	busy_cases = (
		({"runs": [{"id": 4242, "status": "in_progress"}, {"id": 7, "status": "queued"}]}, "reason=intake_busy active_runs=1", "count=0\n"),
		({"runs": [{"id": 4242, "status": "in_progress"}, {"id": 7, "status": "completed"}]}, "reason=listed count=1", "count=1\n"),
		({"runs_fail": True}, "reason=runs_unavailable", "count=0\n"),
	)
	for extra, expected_log, expected_output in busy_cases:
		with tempfile.TemporaryDirectory(prefix="heal-scan-list-busy-") as tmp_name:
			state = _state([_heal_issue(31)])
			state.update(extra)
			result, _, output, _, _ = _run(Path(tmp_name), state, args=("--list-only",))
			assert result.returncode == 0, result.stderr + result.stdout
			assert expected_log in result.stdout, (extra, result.stdout)
			assert output.read_text(encoding="utf-8") == expected_output, extra


def test_gates_skip_without_api_calls() -> None:
	cases = [
		({"GITHUB_EVENT_NAME": "workflow_dispatch"}, "reason=not_scheduled", False),
		({"HEAL_SCOPE_RECOVERY_ENABLED": "false"}, "reason=recovery_disabled", False),
		({"HEAL_SCOPE_RECOVERY_ENABLED": "maybe"}, "reason=invalid_switch", False),
		({"WORKFLOW_HEAL_ENABLED": "false"}, "reason=disabled", False),
	]
	for env, expected, _ in cases:
		with tempfile.TemporaryDirectory(prefix="heal-scan-gate-") as tmp_name:
			result, state, output, intake_env, _ = _run(Path(tmp_name), _state([_heal_issue()]), args=("--list-only",), env=env)
			assert result.returncode == 0, (env, result.stderr + result.stdout)
			assert expected in result.stdout + result.stderr, (env, result.stdout, result.stderr)
			assert output.read_text(encoding="utf-8") == "count=0\n"
			assert "calls" not in state and not intake_env.exists()
			if "invalid_switch" in expected:
				assert "maybe" not in result.stderr
	with tempfile.TemporaryDirectory(prefix="heal-scan-identity-") as tmp_name:
		state = _state([_heal_issue()])
		state["user_fail"] = True
		result, _, output, intake_env, _ = _run(Path(tmp_name), state, args=("--list-only",))
		assert result.returncode == 0 and "reason=identity_unavailable" in result.stdout
		assert output.read_text(encoding="utf-8") == "count=0\n" and not intake_env.exists()


def test_scan_runs_the_intake_with_the_reconstructed_report_and_records_a_failed_attempt() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-scan-run-") as tmp_name:
		result, state, _, intake_env, tg_log = _run(Path(tmp_name), _state([_heal_issue(31)]),
			intake_output="WORKFLOW_HEAL heal_scope_recovery outcome=skip reason=fingerprint_mismatch issue=31 fp=x expected_fp=y")
		assert result.returncode == 0, result.stderr + result.stdout
		env = dict(line.split("=", 1) for line in intake_env.read_text(encoding="utf-8").splitlines())
		assert env["WORKFLOW_HEAL_REPORT_ORIGIN"] == "recovery_scan"
		assert env["WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_FP"] == "c" * 64
		assert env["WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_ISSUE"] == "31"
		# The scan never forwards a report identity.
		assert "WORKFLOW_HEAL_REPORT_IDENTITY_STATE" not in env
		payload = json.loads(Path(f"{intake_env}.payload").read_text(encoding="utf-8"))
		assert payload["source_kind"] == "autofix_failure" and payload["issue_number"] == 4174 and payload["head_sha"] == SHA_B
		posted = state["posted"]
		assert len(posted) == 1 and posted[0]["path"] == f"repos/{SELF_REPO}/issues/31/comments"
		assert posted[0]["body"].rstrip().endswith(heal.render_heal_scope_recovery_scan_marker("fingerprint_mismatch", "4242"))
		assert "heal_scope_recovery outcome=attempt_failed reason=fingerprint_mismatch issue=31 attempts=1 max=3 marker=posted" in result.stdout
		assert not tg_log.exists()


def test_scan_counts_success_and_stops_after_the_attempt_cap() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-scan-ok-") as tmp_name:
		result, state, _, _, _ = _run(Path(tmp_name), _state([_heal_issue(31)]),
			intake_output="WORKFLOW_HEAL heal_scope_recovery outcome=replaced issue=31 repo=x replacement=901")
		assert result.returncode == 0, result.stderr + result.stdout
		assert "heal_scope_recovery outcome=scan reason=replaced issue=31" in result.stdout
		assert "posted" not in state
	comments_path = f"repos/{SELF_REPO}/issues/31/comments"
	# The third failed attempt sends one WARNING.
	with tempfile.TemporaryDirectory(prefix="heal-scan-cap-") as tmp_name:
		state = _state([_heal_issue(31)], {comments_path: [_scan_comment(), _scan_comment(), _scan_comment(author="mallory")]})
		result, state, _, _, tg_log = _run(Path(tmp_name), state, intake_output="WORKFLOW_HEAL skip reason=provenance_rejected source=x", intake_rc=0)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "attempts=3 max=3 marker=posted" in result.stdout
		warnings = [line for line in tg_log.read_text(encoding="utf-8").splitlines() if line.startswith("WARNING\t")]
		assert len(warnings) == 1 and "stopped trying" in warnings[0]
	# Once exhausted, the intake is not run.
	with tempfile.TemporaryDirectory(prefix="heal-scan-exhausted-") as tmp_name:
		state = _state([_heal_issue(31)], {comments_path: [_scan_comment(), _scan_comment(), _scan_comment()]})
		result, state, _, intake_env, _ = _run(Path(tmp_name), state)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "outcome=attempts_exhausted" in result.stdout and not intake_env.exists() and "posted" not in state


def test_scan_records_permanent_skips_and_honours_the_per_tick_cap() -> None:
	edited = _heal_issue(31)
	edited["lastEditedAt"] = "2026-10-01T00:00:00Z"
	with tempfile.TemporaryDirectory(prefix="heal-scan-edited-") as tmp_name:
		result, state, _, intake_env, _ = _run(Path(tmp_name), _state([edited, _heal_issue(32)]))
		assert result.returncode == 0, result.stderr + result.stdout
		assert "heal_scope_recovery outcome=skip reason=issue_edited issue=31" in result.stdout
		# The permanent skip ran no intake, so it does not take the tick's
		# one slot: #32 is replayed in the same run.
		assert intake_env.exists()
		assert "WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_ISSUE=32" in intake_env.read_text(encoding="utf-8")
		assert [post["path"] for post in state["posted"]] == [f"repos/{SELF_REPO}/issues/31/comments", f"repos/{SELF_REPO}/issues/32/comments"]
	# An edit after the list snapshot is seen by the per-candidate re-read.
	with tempfile.TemporaryDirectory(prefix="heal-scan-fresh-edit-") as tmp_name:
		state = _state([_heal_issue(31)])
		state["fresh"] = {"31": {"lastEditedAt": "2026-10-13T00:00:00Z"}}
		result, state, _, intake_env, _ = _run(Path(tmp_name), state)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "reason=issue_edited issue=31" in result.stdout and not intake_env.exists()
		assert not [call for call in state["calls"] if any(a.endswith("/pulls/4174") for a in call)]
	# A closed issue or a failed re-read is skipped without a marker.
	for fresh_state, expected in (({"fresh": {"31": {"state": "CLOSED"}}}, "reason=issue_closed"), ({"issue_read_fail": True}, "reason=issue_unavailable")):
		with tempfile.TemporaryDirectory(prefix="heal-scan-fresh-") as tmp_name:
			state = _state([_heal_issue(31)])
			state.update(fresh_state)
			result, state, _, intake_env, _ = _run(Path(tmp_name), state)
			assert result.returncode == 0, result.stderr + result.stdout
			assert f"{expected} issue=31" in result.stdout and not intake_env.exists() and "posted" not in state
	with tempfile.TemporaryDirectory(prefix="heal-scan-two-") as tmp_name:
		result, state, _, _, _ = _run(Path(tmp_name), _state([_heal_issue(31), _heal_issue(32)]), env={"HEAL_SCOPE_RECOVERY_SCAN_MAX_PER_TICK": "9"})
		assert result.returncode == 0, result.stderr + result.stdout
		# Clamped to 2.
		assert "processed=2 " in result.stdout
	# A failed PR read is transient: no attempt is recorded.
	with tempfile.TemporaryDirectory(prefix="heal-scan-pull-") as tmp_name:
		state = _state([_heal_issue(31)])
		state["pulls"] = {}
		result, state, _, intake_env, _ = _run(Path(tmp_name), state)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "reason=pull_unavailable issue=31" in result.stdout and "posted" not in state and not intake_env.exists()
	# Every processed intake failing turns the job red.
	with tempfile.TemporaryDirectory(prefix="heal-scan-red-") as tmp_name:
		result, _, _, _, _ = _run(Path(tmp_name), _state([_heal_issue(31)]), intake_output="WORKFLOW_HEAL error reason=scope_marker_unverified", intake_rc=1)
		assert result.returncode == 1
		assert "attempt_failed reason=scope_marker_unverified" in result.stdout


def test_scan_defers_transient_reasons_and_reports_the_specific_one() -> None:
	not_recovered = "WORKFLOW_HEAL heal_scope_recovery outcome=skip reason=not_recovered issue=31 fp=x"
	with tempfile.TemporaryDirectory(prefix="heal-scan-transient-") as tmp_name:
		output = "WORKFLOW_HEAL heal_scope_recovery outcome=skip reason=budget_budget_exhausted issue=31 repo=x fp=x\n" + not_recovered
		result, state, _, _, _ = _run(Path(tmp_name), _state([_heal_issue(31)]), intake_output=output)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "heal_scope_recovery outcome=deferred reason=budget_budget_exhausted issue=31 attempts=0 max=3" in result.stdout
		assert "posted" not in state
	# A permanent reason logged before the generic line is the one recorded.
	with tempfile.TemporaryDirectory(prefix="heal-scan-specific-") as tmp_name:
		output = "WORKFLOW_HEAL heal_scope_recovery outcome=skip reason=budget_escalate issue=31 repo=x fp=x\n" + not_recovered
		result, state, _, _, _ = _run(Path(tmp_name), _state([_heal_issue(31)]), intake_output=output)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "attempt_failed reason=budget_escalate issue=31" in result.stdout
		assert len(state["posted"]) == 1
	with tempfile.TemporaryDirectory(prefix="heal-scan-generic-") as tmp_name:
		result, state, _, _, _ = _run(Path(tmp_name), _state([_heal_issue(31)]), intake_output=not_recovered)
		assert "attempt_failed reason=not_recovered issue=31" in result.stdout


def test_scan_closes_an_issue_already_replaced() -> None:
	comments_path = f"repos/{SELF_REPO}/issues/31/comments"
	marker = {"id": 2, "body": "replaced\n\n" + heal.render_heal_scope_recovery_marker(901, SELF_REPO), "user": {"login": LOGIN}}
	with tempfile.TemporaryDirectory(prefix="heal-scan-replaced-") as tmp_name:
		result, state, _, intake_env, _ = _run(Path(tmp_name), _state([_heal_issue(31)], {comments_path: [marker]}))
		assert result.returncode == 0, result.stderr + result.stdout
		assert "heal_scope_recovery outcome=skip reason=already_replaced issue=31 close=done" in result.stdout
		assert state["closed"] == [["31", "--repo", SELF_REPO, "--reason", "not planned"]]
		assert not intake_env.exists() and "posted" not in state


def test_scan_defers_an_intake_heal_issue_list_failure() -> None:
	# The intake logs this read failure without a reason= token and exits 1.
	with tempfile.TemporaryDirectory(prefix="heal-scan-list-fail-") as tmp_name:
		result, state, _, _, _ = _run(Path(tmp_name), _state([_heal_issue(31)]), intake_output="WORKFLOW_HEAL error heal_issue_list_failed", intake_rc=1)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "heal_scope_recovery outcome=deferred reason=heal_issue_list_failed issue=31 attempts=0 max=3 intake_rc=1" in result.stdout
		assert "posted" not in state
		assert "intake_failures=0" in result.stdout


def test_scan_warns_when_an_already_replaced_issue_cannot_be_closed() -> None:
	comments_path = f"repos/{SELF_REPO}/issues/31/comments"
	marker = {"id": 2, "body": "replaced\n\n" + heal.render_heal_scope_recovery_marker(901, SELF_REPO), "user": {"login": LOGIN}}
	# A failed close records an attempt marker and sends no WARNING yet.
	with tempfile.TemporaryDirectory(prefix="heal-scan-close-fail-") as tmp_name:
		state = _state([_heal_issue(31)], {comments_path: [marker]})
		state["close_fail"] = True
		result, state, _, intake_env, tg_log = _run(Path(tmp_name), state)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "reason=already_replaced issue=31 close=failed" in result.stdout
		assert "attempt_failed reason=close_failed issue=31 attempts=1 max=3 marker=posted" in result.stdout
		assert len(state["posted"]) == 1 and state["posted"][0]["path"] == comments_path
		assert not tg_log.exists()
		assert not intake_env.exists()
	# The last attempt sends one WARNING; the attempt cap bounds the retries.
	with tempfile.TemporaryDirectory(prefix="heal-scan-close-fail-cap-") as tmp_name:
		state = _state([_heal_issue(31)], {comments_path: [marker, _scan_comment(), _scan_comment()]})
		state["close_fail"] = True
		result, _, _, _, tg_log = _run(Path(tmp_name), state)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "attempt_failed reason=close_failed issue=31 attempts=3 max=3 marker=posted" in result.stdout
		warnings = [line for line in tg_log.read_text(encoding="utf-8").splitlines() if line.startswith("WARNING\t")]
		assert len(warnings) == 1 and "stopped trying" in warnings[0]


def test_scan_deferral_gives_the_slot_back_to_a_later_candidate() -> None:
	output = "WORKFLOW_HEAL heal_scope_recovery outcome=skip reason=issue_record_missing issue=31 repo=x fp=x"
	with tempfile.TemporaryDirectory(prefix="heal-scan-defer-slot-") as tmp_name:
		result, _, _, intake_env, _ = _run(Path(tmp_name), _state([_heal_issue(31), _heal_issue(32)]), intake_output=output)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "outcome=deferred reason=issue_record_missing issue=31" in result.stdout
		# The deferral released the one per-tick slot, so #32 ran too.
		assert "WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_ISSUE=32" in intake_env.read_text(encoding="utf-8")
		assert "deferred=2 " in result.stdout


def test_scan_warns_when_neither_the_close_nor_the_attempt_marker_lands() -> None:
	comments_path = f"repos/{SELF_REPO}/issues/31/comments"
	marker = {"id": 2, "body": "replaced\n\n" + heal.render_heal_scope_recovery_marker(901, SELF_REPO), "user": {"login": LOGIN}}
	with tempfile.TemporaryDirectory(prefix="heal-scan-close-marker-fail-") as tmp_name:
		state = _state([_heal_issue(31)], {comments_path: [marker]})
		state["close_fail"] = True
		state["comment_post_fail"] = True
		result, _, _, _, tg_log = _run(Path(tmp_name), state)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "attempt_failed reason=close_failed issue=31 attempts=1 max=3 marker=failed" in result.stdout
		warnings = [line for line in tg_log.read_text(encoding="utf-8").splitlines() if line.startswith("WARNING\t")]
		assert len(warnings) == 1 and "close it by hand" in warnings[0]

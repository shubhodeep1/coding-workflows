#!/usr/bin/env python3
"""Tests for scripts/auto_release_stable.sh and its workflow wrapper."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "auto_release_stable.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-release-stable.yml"

TIP = "1" * 40
TAG_OBJECT = "2" * 40
RUN_ID = "4242"

MOCK_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path(os.environ["MOCK_GH_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def save():
    state_path.write_text(json.dumps(state))
def respond(payload):
    save()
    sys.stdout.write(json.dumps(payload))
    sys.exit(0)
if args[:3] == ["api", "-X", "POST"] and args[3].endswith("/dispatches"):
    if state.get("dispatch_error"):
        save(); sys.stderr.write("HTTP 403: Resource not accessible by integration\n"); sys.exit(1)
    body = json.loads(Path(args[args.index("--input") + 1]).read_text())
    state.setdefault("heal_dispatches", []).append(body)
    respond({})
if args[:1] == ["api"]:
    path = args[1]
    if "/git/ref/heads/" in path:
        respond({"object": {"type": "commit", "sha": state["branch_tip"]}})
    if path.endswith("/git/ref/tags/stable"):
        if state.get("tag_missing"):
            save(); sys.stderr.write("404\n"); sys.exit(1)
        if state.get("tag_lookup_error"):
            save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
        if state.get("tag_lightweight"):
            respond({"object": {"type": "commit", "sha": state["tag_commit"]}})
        respond({"object": {"type": "tag", "sha": state["tag_object"]}})
    if "/git/tags/" in path:
        tag = {"object": {"type": "commit", "sha": state["tag_commit"]}}
        if state.get("tagger_date"):
            tag["tagger"] = {"date": state["tagger_date"]}
        respond(tag)
    if "/compare/" in path:
        compare = {"status": state.get("compare_status", "ahead"), "ahead_by": 1, "behind_by": 0}
        if state.get("compare_commit_dates"):
            compare["commits"] = [{"commit": {"committer": {"date": d}}} for d in state["compare_commit_dates"]]
        if state.get("base_commit_date"):
            compare["base_commit"] = {"commit": {"committer": {"date": state["base_commit_date"]}}}
        respond(compare)
    if "/actions/workflows/promote-main-to-stable.yml/runs" in path:
        respond({"workflow_runs": state.get("promote_runs", [])})
    if "/actions/workflows/mark-stable.yml/runs" in path:
        respond({"workflow_runs": state.get("legacy_runs", [])})
    if "/actions/workflows/" in path and "/runs" in path:
        respond({"workflow_runs": state.get("runs", [])})
    save(); sys.stderr.write("unexpected api path " + path + "\n"); sys.exit(1)
if args[:2] == ["workflow", "run"]:
    state.setdefault("dispatches", []).append(args[2:])
    save(); sys.exit(0)
save(); sys.stderr.write("unexpected gh call " + " ".join(args) + "\n"); sys.exit(1)
'''


def _run(tmp: Path, state: dict, env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict]:
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(MOCK_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	state_file = tmp / "state.json"
	state_file.write_text(json.dumps(state), encoding="utf-8")
	output_file = tmp / "out.txt"
	output_file.write_text("", encoding="utf-8")
	run_env = os.environ.copy()
	run_env.update(
		{
			"PATH": f"{bin_dir}:{run_env['PATH']}",
			"MOCK_GH_STATE": str(state_file),
			"GITHUB_REPOSITORY": "owner/repo",
			"GH_TOKEN": "x",
			"GITHUB_OUTPUT": str(output_file),
			"PYTHONDONTWRITEBYTECODE": "1",
			"GITHUB_RUN_ID": RUN_ID,
			"GITHUB_RUN_ATTEMPT": "1",
			"GITHUB_WORKFLOW": "Auto release stable",
			"GH_RETRY_MAX_ATTEMPTS": "1",
		}
	)
	# Keep a CI runner's OIDC and Telegram settings out of the script under test.
	for key in ("BASH_ENV", "ENV", "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "TG_CHAT_ID", "RELEASE_BLOCKER_HEAL_ENABLED", "RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS", "RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS", "RELEASE_BLOCKER_STABLE_STALE_SECS", "RELEASE_BLOCKER_HEAL_PY", "AUTO_RELEASE_STABLE_BRANCH"):
		run_env.pop(key, None)
	if env:
		run_env.update(env)
	proc = subprocess.run(["bash", str(SCRIPT)], cwd=str(tmp), env=run_env, text=True, capture_output=True, timeout=60)
	final_state = json.loads(state_file.read_text(encoding="utf-8"))
	final_state["github_output"] = output_file.read_text(encoding="utf-8")
	return proc, final_state


def _state(**overrides: object) -> dict:
	base: dict = {"branch_tip": TIP, "tag_object": TAG_OBJECT, "tag_commit": TIP, "runs": []}
	base.update(overrides)
	return base


def test_kill_switch_skips_without_api_calls() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(), env={"AUTO_RELEASE_STABLE_ENABLED": "false"})
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=disabled" in proc.stdout
	assert state.get("calls", []) == []


def test_up_to_date_skips_after_dereferencing_annotated_tag() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state())
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_SKIPPED reason=up_to_date sha={TIP}" in proc.stdout
	assert not state.get("dispatches")
	assert "dispatched=false" in state["github_output"]


def test_branch_ahead_of_tag_dispatches_release_gate() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40))
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_DISPATCHED sha={TIP}" in proc.stdout
	assert state["dispatches"] == [["test-and-mark-stable.yml", "--repo", "owner/repo", "--ref", "refs/heads/stable"]]
	assert "dispatched=true" in state["github_output"]


def test_missing_tag_counts_as_unreleased() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_missing=True))
	assert proc.returncode == 0, proc.stderr
	assert "does not exist" in proc.stdout + proc.stderr
	assert len(state["dispatches"]) == 1


def test_tag_lookup_failure_fails_closed() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_lookup_error=True))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=guard_unavailable lookup=tag:stable" in proc.stdout
	assert not state.get("dispatches")


def test_in_flight_gate_run_skips() -> None:
	runs = [{"status": "in_progress", "conclusion": None, "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("dispatches")


def test_in_flight_gate_only_run_on_main_blocks() -> None:
	# The gate's e2e job shares one cancel-in-progress group per repository:
	# dispatching a stable gate now would cancel the cycle's smoke gate.
	runs = [{"status": "in_progress", "conclusion": None, "head_sha": TIP, "head_branch": "main", "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("dispatches")


def test_in_flight_promotion_run_skips() -> None:
	promote_runs = [{"event": "workflow_dispatch", "status": "in_progress", "conclusion": None, "head_sha": TIP, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, promote_runs=promote_runs))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("dispatches")


def test_in_flight_scheduled_promote_cycle_blocks() -> None:
	# A scheduled cycle tick is about to dispatch, or waiting on, its own gate.
	promote_runs = [{"event": "schedule", "status": "in_progress", "conclusion": None, "head_sha": TIP, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, promote_runs=promote_runs))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("dispatches")


def test_cancelled_gate_on_same_tip_is_retried() -> None:
	runs = [{"status": "completed", "conclusion": "cancelled", "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs))
	assert proc.returncode == 0, proc.stderr
	assert "last_gate_failed" not in proc.stdout
	assert len(state["dispatches"]) == 1


def test_in_flight_legacy_mark_stable_run_skips() -> None:
	legacy_runs = [{"status": "in_progress", "conclusion": None, "head_sha": TIP, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, legacy_runs=legacy_runs))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("dispatches")


def test_failed_gate_on_same_tip_skips_until_branch_moves() -> None:
	# Budget of one: a single failure holds the tip, as before the retry budget.
	runs = [
		{"status": "completed", "conclusion": "success", "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-18T00:00:00Z"},
		{"status": "completed", "conclusion": "failure", "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-19T00:00:00Z"},
	]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs), env={"AUTO_RELEASE_STABLE_MAX_ATTEMPTS": "1"})
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_SKIPPED reason=last_gate_failed sha={TIP} conclusion=failure attempts=1 max=1" in proc.stdout
	assert not state.get("dispatches")


def test_failed_gate_on_same_tip_is_retried_within_the_default_budget() -> None:
	# One failed and one timed-out gate on the tip: two of the default three
	# attempts are spent, so the next tick dispatches again.
	runs = [
		{"status": "completed", "conclusion": "failure", "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-19T00:00:00Z"},
		{"status": "completed", "conclusion": "timed_out", "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-19T06:00:00Z"},
	]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs))
	assert proc.returncode == 0, proc.stderr
	assert "last_gate_failed" not in proc.stdout
	assert f"Retrying test-and-mark-stable.yml on stable@{TIP}: 2 failed attempt(s) so far, budget 3." in proc.stdout
	assert f"AUTO_RELEASE_DISPATCHED sha={TIP}" in proc.stdout
	assert len(state["dispatches"]) == 1


def test_failed_gate_on_same_tip_holds_once_the_budget_is_spent() -> None:
	runs = [
		{"status": "completed", "conclusion": "failure", "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-19T00:00:00Z"},
		{"status": "completed", "conclusion": "cancelled", "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-19T03:00:00Z"},
		{"status": "completed", "conclusion": "startup_failure", "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-19T06:00:00Z"},
		{"status": "completed", "conclusion": "failure", "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-19T12:00:00Z"},
	]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs))
	assert proc.returncode == 0, proc.stderr
	# The cancelled run is not an attempt; the three real failures spend the budget.
	assert f"AUTO_RELEASE_SKIPPED reason=last_gate_failed sha={TIP} conclusion=failure attempts=3 max=3" in proc.stdout
	assert not state.get("dispatches")


def test_failed_gates_on_other_tips_or_branches_do_not_spend_the_budget() -> None:
	runs = [
		{"status": "completed", "conclusion": "failure", "head_sha": "9" * 40, "head_branch": "stable", "created_at": "2026-09-18T00:00:00Z"},
		{"status": "completed", "conclusion": "failure", "head_sha": TIP, "head_branch": "main", "created_at": "2026-09-19T00:00:00Z"},
		{"status": "completed", "conclusion": "failure", "head_sha": TIP, "head_branch": "stable", "created_at": "2026-09-19T06:00:00Z"},
	]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs), env={"AUTO_RELEASE_STABLE_MAX_ATTEMPTS": "2"})
	assert proc.returncode == 0, proc.stderr
	assert "1 failed attempt(s) so far, budget 2." in proc.stdout
	assert len(state["dispatches"]) == 1


def test_invalid_max_attempts_fails_fast() -> None:
	for bad in ("0", "many", "-1"):
		with tempfile.TemporaryDirectory() as tmp:
			proc, state = _run(Path(tmp), _state(tag_commit="3" * 40), env={"AUTO_RELEASE_STABLE_MAX_ATTEMPTS": bad})
		assert proc.returncode == 1, bad
		assert "AUTO_RELEASE_STABLE_MAX_ATTEMPTS must be a positive integer" in proc.stdout + proc.stderr, bad
		assert not state.get("dispatches"), bad


def test_failed_gate_only_run_on_main_with_same_tip_does_not_block() -> None:
	# The promote cycle runs the same workflow in gate_only mode on main; when
	# main and stable share a tip, its failure is not a failed stable release.
	runs = [{"status": "completed", "conclusion": "failure", "head_sha": TIP, "head_branch": "main", "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs))
	assert proc.returncode == 0, proc.stderr
	assert "last_gate_failed" not in proc.stdout
	assert len(state["dispatches"]) == 1


def test_failed_gate_on_older_tip_does_not_block() -> None:
	runs = [{"status": "completed", "conclusion": "failure", "head_sha": "9" * 40, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs))
	assert proc.returncode == 0, proc.stderr
	assert len(state["dispatches"]) == 1


def test_workflow_contract() -> None:
	wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	assert wf["on"]["schedule"] == [{"cron": "30 */6 * * *"}]
	assert "workflow_dispatch" in wf["on"]
	assert wf["concurrency"]["group"] == "promote-main-to-stable", "must share the promote job's group so check-then-dispatch never interleaves with a promotion"
	assert wf["concurrency"]["cancel-in-progress"] is False
	assert wf["permissions"] == {"contents": "read", "actions": "write", "id-token": "write"}
	run_step = next(s for s in wf["jobs"]["release-check"]["steps"] if "run" in s)
	assert "bash scripts/auto_release_stable.sh" in run_step["run"]
	assert "${{" not in run_step["run"], "bind expressions through env:, never inside run:"
	for name, default in (
		("RELEASE_BLOCKER_HEAL_ENABLED", "true"),
		("RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS", "86400"),
		("RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS", "21600"),
		("RELEASE_BLOCKER_STABLE_STALE_SECS", "86400"),
	):
		assert run_step["env"][name] == "${{ vars." + name + " || '" + default + "' }}", name
	assert run_step["env"]["TG_BOT_SECRET"] == "${{ secrets.TG_BOT_SECRET }}"
	assert run_step["env"]["TG_ADMIN_CHAT_ID"] == "${{ vars.TG_ADMIN_CHAT_ID }}"
	assert run_step["env"]["TG_CHAT_ID"] == "${{ vars.TG_ADMIN_CHAT_ID }}"
	assert run_step["env"]["AUTO_RELEASE_STABLE_ENABLED"] == "${{ vars.AUTO_RELEASE_STABLE_ENABLED || 'true' }}"
	assert run_step["env"]["AUTO_RELEASE_STABLE_MAX_ATTEMPTS"] == "${{ vars.AUTO_RELEASE_STABLE_MAX_ATTEMPTS || '3' }}"


def test_branch_behind_or_diverged_from_tag_fails_closed() -> None:
	for status in ("behind", "diverged"):
		with tempfile.TemporaryDirectory() as tmp:
			proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, compare_status=status))
		assert proc.returncode == 0, proc.stderr
		assert f"AUTO_RELEASE_SKIPPED reason=branch_not_ahead sha={TIP} tag={'3' * 40} status={status}" in proc.stdout, status
		assert not state.get("dispatches"), status


def test_compare_identical_counts_as_up_to_date() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, compare_status="identical"))
	assert "AUTO_RELEASE_SKIPPED reason=up_to_date" in proc.stdout
	assert not state.get("dispatches")


# --- Release-blocker reporters (stuck-states P4 / release-blocker Phase 2a) ---


def _ago(seconds: int) -> str:
	return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reports(state: dict) -> list[dict]:
	return [body["client_payload"]["report"] for body in state.get("heal_dispatches", [])]


def _failed_gate(updated_at: str | None, run_id: int = 777) -> dict:
	run = {"id": run_id, "status": "completed", "conclusion": "failure", "head_sha": TIP, "head_branch": "stable", "created_at": _ago(3 * 86400)}
	if updated_at is not None:
		run["updated_at"] = updated_at
	return run


def test_last_gate_failed_past_threshold_sends_one_report() -> None:
	runs = [_failed_gate(_ago(2 * 86400))]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs), env={"AUTO_RELEASE_STABLE_MAX_ATTEMPTS": "1"})
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_SKIPPED reason=last_gate_failed sha={TIP} conclusion=failure attempts=1 max=1" in proc.stdout
	assert "dispatched=false" in state["github_output"]
	assert not state.get("dispatches")
	assert len(state["heal_dispatches"]) == 1
	assert state["heal_dispatches"][0]["event_type"] == "workflow-failure-heal"
	report = _reports(state)[0]
	assert report["source_kind"] == "release_blocker"
	assert report["blocker_reason"] == "last_gate_failed"
	assert report["head_branch"] == "stable"
	assert report["head_sha"] == TIP
	assert report["run_refs"][0]["run_id"] == "777"
	assert report["reporter_run"]["run_id"] == RUN_ID
	assert report["workflow_name"] == "Auto release stable"
	assert report["blocker_evidence"].startswith("reason=last_gate_failed")
	assert "/actions/runs/777" in report["blocker_evidence"]
	assert "RELEASE_BLOCKER_REPORTED reason=last_gate_failed age=" in proc.stdout
	assert "run_ref=777" in proc.stdout
	# The report comes before the unchanged skip line.
	assert proc.stdout.index("RELEASE_BLOCKER_REPORTED") < proc.stdout.index("AUTO_RELEASE_SKIPPED")


def test_last_gate_failed_recently_does_not_report() -> None:
	runs = [_failed_gate(_ago(600))]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs), env={"AUTO_RELEASE_STABLE_MAX_ATTEMPTS": "1"})
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=last_gate_failed" in proc.stdout
	assert not state.get("heal_dispatches")


def test_last_gate_failed_without_updated_at_is_guard_unavailable() -> None:
	runs = [_failed_gate(None)]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs), env={"AUTO_RELEASE_STABLE_MAX_ATTEMPTS": "1"})
	assert proc.returncode == 0, proc.stderr
	assert "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=last_gate_failed lookup=updated_at" in proc.stdout
	assert not state.get("heal_dispatches")


def test_release_in_flight_past_threshold_reports_from_each_run_list() -> None:
	active = {"id": 991, "status": "in_progress", "conclusion": None, "head_sha": TIP, "head_branch": "stable", "created_at": _ago(8 * 3600), "run_started_at": _ago(7 * 3600)}
	for key, workflow in (("runs", "test-and-mark-stable.yml"), ("promote_runs", "promote-main-to-stable.yml"), ("legacy_runs", "mark-stable.yml")):
		with tempfile.TemporaryDirectory() as tmp:
			proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, **{key: [active]}))
		assert proc.returncode == 0, (key, proc.stderr)
		assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout, key
		assert not state.get("dispatches"), key
		reports = _reports(state)
		assert len(reports) == 1, key
		assert reports[0]["blocker_reason"] == "release_in_flight", key
		assert reports[0]["run_refs"] == [], key
		assert f"workflow={workflow}" in reports[0]["blocker_evidence"], key
		assert "/actions/runs/991" in reports[0]["blocker_evidence"], key


def test_release_in_flight_recent_run_does_not_report() -> None:
	active = {"id": 991, "status": "queued", "conclusion": None, "head_sha": TIP, "head_branch": "stable", "created_at": _ago(3600)}
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[active]))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("heal_dispatches")


def test_stable_release_stale_reports_and_still_dispatches_the_gate() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, tagger_date=_ago(3 * 86400), compare_commit_dates=[_ago(2 * 86400), _ago(3600)]))
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_DISPATCHED sha={TIP}" in proc.stdout
	assert len(state["dispatches"]) == 1
	reports = _reports(state)
	assert len(reports) == 1
	assert reports[0]["blocker_reason"] == "stable_release_stale"
	assert reports[0]["run_refs"] == []
	assert reports[0]["blocker_evidence"].startswith("reason=stable_release_stale")
	assert 2 * 86400 - 120 <= reports[0]["blocker_age_secs"] <= 2 * 86400 + 120


def test_stable_release_stale_ignores_a_fresh_patch_on_an_old_tag() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, tagger_date=_ago(30 * 86400), compare_commit_dates=[_ago(3600)]))
	assert proc.returncode == 0, proc.stderr
	assert len(state["dispatches"]) == 1
	assert not state.get("heal_dispatches")


def test_stable_release_stale_uses_the_base_commit_date_for_a_lightweight_tag() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, tag_lightweight=True, base_commit_date=_ago(3 * 86400), compare_commit_dates=[_ago(2 * 86400)]))
	assert proc.returncode == 0, proc.stderr
	assert len(state["dispatches"]) == 1
	reports = _reports(state)
	assert [r["blocker_reason"] for r in reports] == ["stable_release_stale"]


def test_stable_release_stale_without_dates_is_guard_unavailable() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40))
	assert proc.returncode == 0, proc.stderr
	assert "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=stable_release_stale lookup=tag_time" in proc.stdout
	assert len(state["dispatches"]) == 1
	assert not state.get("heal_dispatches")


def test_at_most_one_report_per_tick_prefers_release_in_flight() -> None:
	active = {"id": 991, "status": "in_progress", "conclusion": None, "head_sha": TIP, "head_branch": "stable", "created_at": _ago(7 * 3600)}
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[active], tagger_date=_ago(3 * 86400), compare_commit_dates=[_ago(2 * 86400)]))
	assert proc.returncode == 0, proc.stderr
	assert [r["blocker_reason"] for r in _reports(state)] == ["release_in_flight"]


def test_stale_not_reported_while_a_gate_run_is_active_on_the_tip() -> None:
	# A gate already started on the tip: the release is progressing, so the
	# plan's "no gate run started on the tip since" condition does not hold.
	active = {"id": 991, "status": "in_progress", "conclusion": None, "head_sha": TIP, "head_branch": "stable", "created_at": _ago(600)}
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[active], tagger_date=_ago(3 * 86400), compare_commit_dates=[_ago(2 * 86400)]))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("heal_dispatches")


def test_stale_reported_on_in_flight_path_when_no_gate_ran_on_the_tip() -> None:
	active = {"id": 992, "status": "in_progress", "conclusion": None, "head_sha": "5" * 40, "head_branch": "main", "created_at": _ago(600)}
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, promote_runs=[active], tagger_date=_ago(3 * 86400), compare_commit_dates=[_ago(2 * 86400)]))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert [r["blocker_reason"] for r in _reports(state)] == ["stable_release_stale"]


def test_stale_not_reported_on_the_retry_path() -> None:
	runs = [_failed_gate(_ago(3600))]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs, tagger_date=_ago(3 * 86400), compare_commit_dates=[_ago(2 * 86400)]))
	assert proc.returncode == 0, proc.stderr
	assert len(state["dispatches"]) == 1
	assert not state.get("heal_dispatches")


def test_last_gate_failed_startup_failure_is_not_claimed_in_run_refs() -> None:
	run = _failed_gate(_ago(2 * 86400), run_id=778)
	run["conclusion"] = "startup_failure"
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[run]), env={"AUTO_RELEASE_STABLE_MAX_ATTEMPTS": "1"})
	assert proc.returncode == 0, proc.stderr
	reports = _reports(state)
	assert [r["blocker_reason"] for r in reports] == ["last_gate_failed"]
	assert reports[0]["run_refs"] == []
	assert "/actions/runs/778" in reports[0]["blocker_evidence"]
	assert "run_ref=none" in proc.stdout


def test_reporter_kill_switch_sends_nothing() -> None:
	active = {"id": 991, "status": "in_progress", "conclusion": None, "head_sha": TIP, "head_branch": "stable", "created_at": _ago(7 * 3600)}
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[active]), env={"RELEASE_BLOCKER_HEAL_ENABLED": "false"})
	assert proc.returncode == 0, proc.stderr
	assert "RELEASE_BLOCKER_REPORT_SKIPPED reason=disabled blocker=release_in_flight" in proc.stdout
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("heal_dispatches")


def test_dispatch_denied_warns_and_keeps_the_tick_outcome() -> None:
	active = {"id": 991, "status": "in_progress", "conclusion": None, "head_sha": TIP, "head_branch": "stable", "created_at": _ago(7 * 3600)}
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[active], dispatch_error=True))
	assert proc.returncode == 0, proc.stderr
	assert "RELEASE_BLOCKER_REPORT_SKIPPED reason=dispatch_denied blocker=release_in_flight detail=" in proc.stdout
	assert "::warning::Release blocker report release_in_flight on stable could not be dispatched" in proc.stdout
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert "dispatched=false" in state["github_output"]


def test_invalid_threshold_warns_and_uses_the_default() -> None:
	active = {"id": 991, "status": "in_progress", "conclusion": None, "head_sha": TIP, "head_branch": "stable", "created_at": _ago(7 * 3600)}
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[active]), env={"RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS": "abc"})
	assert proc.returncode == 0, proc.stderr
	assert "::warning::RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS must be a non-negative integer (got 'abc'); using 21600." in proc.stdout
	assert [r["blocker_reason"] for r in _reports(state)] == ["release_in_flight"]


def test_unsupported_branch_is_payload_invalid_without_failing_the_tick() -> None:
	active = {"id": 991, "status": "in_progress", "conclusion": None, "head_sha": TIP, "head_branch": "release", "created_at": _ago(7 * 3600)}
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[active]), env={"AUTO_RELEASE_STABLE_BRANCH": "release"})
	assert proc.returncode == 0, proc.stderr
	assert "RELEASE_BLOCKER_REPORT_SKIPPED reason=payload_invalid blocker=release_in_flight stage=build" in proc.stdout
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("heal_dispatches")

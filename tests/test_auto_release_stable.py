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
import base64, json, os, sys
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
    rest = args[1:]
    method = "GET"
    if rest[:1] == ["-X"]:
        method = rest[1]
        rest = rest[2:]
    path = rest[0]
    fields = dict(f.split("=", 1) for f in rest[1:] if "=" in f and not f.startswith("-"))
    if "/contents/stable-hold.json" in path:
        # Stable rollback hold (#9342); default: no hold branch (inactive).
        hold = state.get("stable_hold", "inactive")
        if hold == "inactive":
            save(); sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
        if hold == "unknown":
            save(); sys.stderr.write("HTTP 503: Service Unavailable\n"); sys.exit(1)
        doc = {"schema": "stable_hold.v1", "active": True, "to_version": "v1.45.0", "from_version": "v1.46.0"}
        if hold == "malformed":
            doc["schema"] = "other"
        respond({"sha": "a" * 40, "encoding": "base64", "content": base64.b64encode(json.dumps(doc).encode()).decode()})
    if path == "user":
        if state.get("user_error"):
            save(); sys.stderr.write("HTTP 403\n"); sys.exit(1)
        save(); sys.stdout.write(state.get("login", "pipeline-bot") + "\n"); sys.exit(0)
    if path == "search/issues":
        if state.get("search_error"):
            save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
        query = fields.get("q", "")
        state.setdefault("search_queries", []).append(query)
        kind = "pr" if "is:pull-request" in query else "issue"
        items = [i for i in state.get("fixtures", []) if ("pull_request" in i) == (kind == "pr")]
        if not state.get("search_ignores_qualifiers"):
            items = [i for i in items if ("author:" + i["user"]["login"]) in query.split()]
            items = [i for i in items if not any(("-label:" + l["name"]) in query.split() for l in i.get("labels", []))]
        respond({"items": items})
    if method == "PATCH":
        if path.rsplit("/", 1)[-1] in [str(n) for n in state.get("close_fails", [])]:
            save(); sys.stderr.write("HTTP 422\n"); sys.exit(1)
        state.setdefault("closed", []).append([path, fields])
        respond({})
    if path.endswith("/comments"):
        state.setdefault("comments", []).append([path, fields.get("body", "")])
        respond({})
    if "/actions/runs?status=queued" in path and "created=" in path:
        state.setdefault("wedged_queries", []).append(path)
        if state.get("wedged_error"):
            save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
        respond({"total_count": state.get("wedged_runs", 0), "workflow_runs": []})
    if "/actions/runs?status=queued" in path:
        if state.get("queued_error"):
            save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
        respond({"total_count": state.get("queued_runs", 0), "workflow_runs": []})
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


def test_stable_hold_active_skips_release() -> None:
	# rollback-stable.yml moved the tag back (#9342): the branch is ahead of
	# the tag by design, and releasing it would re-ship the regression.
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, stable_hold="active"))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=stable_hold_active to=v1.45.0" in proc.stdout
	assert not state.get("dispatches")
	assert len(state["calls"]) == 1 and "/contents/stable-hold.json?ref=ai%2Fstable-hold" in state["calls"][0][1]
	assert "dispatched=false" in state["github_output"]


def test_stable_hold_unknown_skips_release() -> None:
	for hold in ("unknown", "malformed"):
		with tempfile.TemporaryDirectory() as tmp:
			proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, stable_hold=hold), env={"GH_RETRY_MAX_ATTEMPTS": "1"})
		assert proc.returncode == 0, proc.stderr
		assert "AUTO_RELEASE_SKIPPED reason=stable_hold_unknown" in proc.stdout, hold
		assert not state.get("dispatches"), hold


def test_stable_hold_inactive_keeps_existing_behaviour() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40))
	assert proc.returncode == 0, proc.stderr
	assert "stable_hold" not in proc.stdout
	assert f"AUTO_RELEASE_DISPATCHED sha={TIP}" in proc.stdout
	assert "/contents/stable-hold.json" in state["calls"][0][1]
	assert state["dispatches"] == [["test-and-mark-stable.yml", "--repo", "owner/repo", "--ref", "refs/heads/stable"]]


def _hours_ago(hours: int) -> str:
	from datetime import datetime, timedelta, timezone
	return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_wedged_queued_gate_run_does_not_hold_the_release() -> None:
	"""Issue #9473: a queued run older than ZOMBIE_RUN_MAX_QUEUED_HOURS is not in flight."""
	wedged = {"id": 43095142301, "status": "queued", "conclusion": None, "head_sha": TIP, "head_branch": "stable",
		"path": ".github/workflows/test-and-mark-stable.yml", "created_at": _hours_ago(25)}
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[wedged]))
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_DISPATCHED sha={TIP}" in proc.stdout
	assert "ZOMBIE_RUN_IGNORED run=43095142301 workflow=.github/workflows/test-and-mark-stable.yml age_h=25 guard=auto_release_gate" in proc.stderr
	fresh = dict(wedged, id=7, created_at=_hours_ago(23))
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[wedged, fresh]))
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[wedged]), env={"ZOMBIE_RUN_MAX_QUEUED_HOURS": "0"})
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout


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


def test_stale_reported_when_the_tip_only_has_cancelled_gate_runs() -> None:
	# Cancelled gates spend no attempt and no other reporter owns them, so
	# they must not suppress stable_release_stale.
	runs = [{"id": 993, "status": "completed", "conclusion": "cancelled", "head_sha": TIP, "head_branch": "stable", "created_at": _ago(3600)}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs, tagger_date=_ago(3 * 86400), compare_commit_dates=[_ago(2 * 86400)]))
	assert proc.returncode == 0, proc.stderr
	assert len(state["dispatches"]) == 1
	reports = _reports(state)
	assert [r["blocker_reason"] for r in reports] == ["stable_release_stale"]
	assert "/actions/runs/993" in reports[0]["blocker_evidence"]
	assert "conclusion=cancelled" in reports[0]["blocker_evidence"]


# Runner-queue back-off and fixture cleanup before a retry.

GATE_RUN = 43934729786


def _queue_failed_gate(run_id: int = GATE_RUN, conclusion: str = "failure", branch: str = "stable", sha: str = TIP, hours_ago: float = 3) -> dict:
	ended = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
	started = ended - timedelta(hours=3)
	return {
		"id": run_id,
		"status": "completed",
		"conclusion": conclusion,
		"head_sha": sha,
		"head_branch": branch,
		"created_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
		"updated_at": ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
	}


def _api_paths(state: dict) -> list[str]:
	return [" ".join(call) for call in state.get("calls", []) if call[:1] == ["api"]]


def test_retry_waits_while_the_queue_is_deep_after_a_recent_failed_gate() -> None:
	for conclusion in ("failure", "timed_out"):
		state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate(conclusion=conclusion)], queued_runs=375)
		with tempfile.TemporaryDirectory() as tmp:
			proc, final = _run(Path(tmp), state)
		assert proc.returncode == 0, proc.stderr
		assert f"AUTO_RELEASE_SKIPPED reason=runner_queue_deep sha={TIP} queued=375 wedged=0 max=100 last_run={GATE_RUN} last_age_hours=3 window_hours=24" in proc.stdout
		assert not final.get("dispatches")
		assert "dispatched=false" in final["github_output"]


def test_retry_goes_ahead_once_the_queue_has_drained() -> None:
	state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate()], queued_runs=99)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_QUEUE_CHECK outcome=clear queued=99 wedged=0 max=100 last_run={GATE_RUN} last_age_hours=3" in proc.stdout
	assert len(final["dispatches"]) == 1


# Issue #10032: wedged queued runs (older than ZOMBIE_RUN_MAX_QUEUED_HOURS) are
# not load, so the back-off compares only live queued runs with the limit.


def _queued_reads(state: dict) -> list[str]:
	return [path for path in _api_paths(state) if "status=queued" in path]


def test_queue_back_off_leaves_wedged_runs_out_of_the_count() -> None:
	state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate()], queued_runs=245, wedged_runs=240)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state, env={"ZOMBIE_RUN_MAX_QUEUED_HOURS": "24"})
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_QUEUE_CHECK outcome=clear queued=5 wedged=240 max=100 last_run={GATE_RUN} last_age_hours=3" in proc.stdout
	assert len(final["dispatches"]) == 1
	reads = _queued_reads(final)
	assert len(reads) == 2, reads
	assert "created=" not in reads[0]
	assert len(final["wedged_queries"]) == 1
	query = final["wedged_queries"][0]
	assert "per_page=1" in query and "created=%3C%3D" in query
	cutoff = datetime.strptime(query.split("created=%3C%3D", 1)[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
	expected = datetime.now(timezone.utc) - timedelta(hours=24)
	assert abs((cutoff - expected).total_seconds()) < 300


def test_queue_back_off_still_holds_on_live_runs_over_the_limit() -> None:
	state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate()], queued_runs=150, wedged_runs=10)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state, env={"ZOMBIE_RUN_MAX_QUEUED_HOURS": "24"})
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_SKIPPED reason=runner_queue_deep sha={TIP} queued=140 wedged=10 max=100 last_run={GATE_RUN} last_age_hours=3 window_hours=24" in proc.stdout
	assert not final.get("dispatches")
	assert "dispatched=false" in final["github_output"]


def test_queue_under_the_limit_reads_the_total_only() -> None:
	state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate()], queued_runs=99, wedged_runs=90)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state, env={"ZOMBIE_RUN_MAX_QUEUED_HOURS": "24"})
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_QUEUE_CHECK outcome=clear queued=99 wedged=0 max=100 last_run={GATE_RUN}" in proc.stdout
	assert len(_queued_reads(final)) == 1
	assert not final.get("wedged_queries")
	assert len(final["dispatches"]) == 1


def test_wedged_filter_off_compares_the_raw_count() -> None:
	for hours in ("0", "soon"):
		state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate()], queued_runs=375, wedged_runs=370)
		with tempfile.TemporaryDirectory() as tmp:
			proc, final = _run(Path(tmp), state, env={"ZOMBIE_RUN_MAX_QUEUED_HOURS": hours})
		assert proc.returncode == 0, (hours, proc.stderr)
		assert f"AUTO_RELEASE_SKIPPED reason=runner_queue_deep sha={TIP} queued=375 wedged=0 max=100" in proc.stdout, hours
		assert len(_queued_reads(final)) == 1, hours
		assert not final.get("wedged_queries"), hours
		assert not final.get("dispatches"), hours


def test_wedged_count_read_failure_fails_open() -> None:
	state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate()], queued_runs=375, wedged_error=True)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state, env={"ZOMBIE_RUN_MAX_QUEUED_HOURS": "24"})
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_QUEUE_CHECK outcome=read_failed lookup=wedged_runs last_run={GATE_RUN}" in proc.stdout
	assert "reason=runner_queue_deep" not in proc.stdout
	assert len(final["dispatches"]) == 1


def test_wedged_count_is_clamped_to_the_total() -> None:
	state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate()], queued_runs=245, wedged_runs=400)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state, env={"ZOMBIE_RUN_MAX_QUEUED_HOURS": "24"})
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_QUEUE_CHECK outcome=clear queued=0 wedged=245 max=100" in proc.stdout
	assert len(final["dispatches"]) == 1


def test_failure_older_than_the_window_gets_one_cycle_whatever_the_queue() -> None:
	# A deep queue delays a release by at most the window; it never holds it.
	state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate(hours_ago=25)], queued_runs=375)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_QUEUE_CHECK outcome=not_recent last_run={GATE_RUN} last_age_hours=25 window_hours=24" in proc.stdout
	assert not any("status=queued" in path for path in _api_paths(final))
	assert len(final["dispatches"]) == 1


def test_failed_gate_without_a_readable_end_time_does_not_hold_the_release() -> None:
	run = _queue_failed_gate()
	del run["updated_at"]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[run], queued_runs=375))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_QUEUE_CHECK outcome=not_recent" in proc.stdout
	assert len(final["dispatches"]) == 1


def test_queue_back_off_reads_nothing_when_the_last_gate_did_not_fail() -> None:
	for runs in ([], [_queue_failed_gate(conclusion="success")], [_queue_failed_gate(conclusion="cancelled")], [_queue_failed_gate(branch="main")]):
		with tempfile.TemporaryDirectory() as tmp:
			proc, final = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs, queued_runs=900))
		assert proc.returncode == 0, proc.stderr
		assert not any("status=queued" in path for path in _api_paths(final)), runs
		assert len(final["dispatches"]) == 1, runs


def test_newest_completed_stable_gate_decides_the_back_off() -> None:
	# A success after the failure means the queue problem is over; a cancelled
	# run after it decides nothing, so the failure before it still counts.
	failed = _queue_failed_gate(hours_ago=10)
	for later, held in (("success", False), ("cancelled", True)):
		runs = [failed, _queue_failed_gate(run_id=GATE_RUN + 5, conclusion=later, hours_ago=1)]
		with tempfile.TemporaryDirectory() as tmp:
			proc, final = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs, queued_runs=900))
		assert proc.returncode == 0, proc.stderr
		assert ("reason=runner_queue_deep" in proc.stdout) is held, later
		assert bool(final.get("dispatches")) is not held, later


def test_queue_back_off_fails_open_and_can_be_disabled() -> None:
	base = {"tag_commit": "3" * 40, "runs": [_queue_failed_gate()], "queued_runs": 375}
	for overrides, env, expected, dispatched in (
		({"queued_error": True}, None, f"AUTO_RELEASE_QUEUE_CHECK outcome=read_failed lookup=queued_runs last_run={GATE_RUN}", True),
		({}, {"AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS": "0"}, f"AUTO_RELEASE_DISPATCHED sha={TIP}", True),
		({}, {"AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS": "0"}, "AUTO_RELEASE_QUEUE_CHECK outcome=not_recent", True),
		({}, {"AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS": "many"}, "AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS is not a non-negative integer of at most 6 digits; using 100.", False),
		({}, {"AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS": "soon"}, "AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS is not a non-negative integer of at most 5 digits; using 24.", False),
	):
		with tempfile.TemporaryDirectory() as tmp:
			proc, final = _run(Path(tmp), _state(**base, **overrides), env=env)
		assert proc.returncode == 0, proc.stderr
		assert expected in proc.stdout, (overrides, env)
		assert bool(final.get("dispatches")) is dispatched, (overrides, env)
		assert ("reason=runner_queue_deep" in proc.stdout) is not dispatched, (overrides, env)


def _fixture(number: int, title: str, login: str = "pipeline-bot", pr: bool = False, labels: tuple[str, ...] = ()) -> dict:
	item: dict = {"number": number, "title": title, "user": {"login": login}, "labels": [{"name": name} for name in labels]}
	if pr:
		item["pull_request"] = {"url": "x"}
	return item


def _fixtures() -> list[dict]:
	return [
		_fixture(101, f"[E2E Smoke Test] Update smoke-test canary (run {GATE_RUN})"),
		_fixture(102, f"[E2E Smoke Test] Review-Blocked Simulation (run {GATE_RUN})", pr=True),
		_fixture(103, f"[E2E Orchestrate Smoke {GATE_RUN}] child task"),
		# Not closed: another author, a run this listing does not show as
		# finished, a project the poller owns, no run id, not a fixture title,
		# and the pipeline's own PR for a fixture issue (the sweep closes it).
		_fixture(104, f"[E2E Smoke Test] hand-written (run {GATE_RUN})", login="someone-else"),
		_fixture(105, "[E2E Smoke Test] Update smoke-test canary (run 55555555555)"),
		_fixture(106, f"[E2E Orchestrate Smoke {GATE_RUN}] project", labels=("ai:orchestrator-tracking",)),
		_fixture(107, f"[E2E Orchestrate Smoke {GATE_RUN}] wave issue", labels=("ai:orchestrator-managed",)),
		_fixture(108, "[E2E Smoke Test] no run id"),
		_fixture(109, f"Release gate: mentions [E2E Smoke Test] (run {GATE_RUN})"),
		_fixture(110, "AI implementation for issue #101", pr=True, labels=("e2e-smoke-test",)),
	]


def test_retry_closes_the_previous_cycles_fixtures_before_dispatching() -> None:
	# search_ignores_qualifiers: the script's own filters must hold even when
	# the search returns rows its qualifiers should have excluded.
	for ignores in (False, True):
		state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate()], fixtures=_fixtures(), search_ignores_qualifiers=ignores)
		with tempfile.TemporaryDirectory() as tmp:
			proc, final = _run(Path(tmp), state)
		assert proc.returncode == 0, proc.stderr
		assert final["closed"] == [
			["repos/owner/repo/issues/101", {"state": "closed", "state_reason": "not_planned"}],
			["repos/owner/repo/issues/103", {"state": "closed", "state_reason": "not_planned"}],
			["repos/owner/repo/pulls/102", {"state": "closed"}],
		], ignores
		# No comment: issue_comment starts eleven workflows.
		assert not final.get("comments"), ignores
		assert "AUTO_RELEASE_FIXTURE_CLEANUP outcome=done closed=3 failed=0 skipped=0" in proc.stdout
		assert f"AUTO_RELEASE_FIXTURE_CLEANUP outcome=closed number=102 type=pr gate_run={GATE_RUN}" in proc.stdout
		for query in final["search_queries"]:
			assert "author:pipeline-bot" in query and "-label:ai:orchestrator-tracking" in query and "-label:ai:orchestrator-managed" in query
		# The cleanup runs before the dispatch, and the dispatch still happens.
		calls = [" ".join(call) for call in final["calls"]]
		assert max(i for i, call in enumerate(calls) if "PATCH" in call) < next(i for i, call in enumerate(calls) if call.startswith("workflow run"))
		assert len(final["dispatches"]) == 1


def test_fixtures_are_closed_even_when_the_tick_then_waits_for_the_queue() -> None:
	state = _state(tag_commit="3" * 40, runs=[_queue_failed_gate()], fixtures=_fixtures(), queued_runs=375)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stderr
	assert len(final["closed"]) == 3
	assert "AUTO_RELEASE_SKIPPED reason=runner_queue_deep" in proc.stdout
	assert not final.get("dispatches")


def test_fixtures_of_gate_runs_on_other_branches_and_of_cancelled_runs_are_closed() -> None:
	for run in (_queue_failed_gate(branch="main"), _queue_failed_gate(conclusion="cancelled")):
		with tempfile.TemporaryDirectory() as tmp:
			proc, final = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[run], fixtures=_fixtures()))
		assert proc.returncode == 0, proc.stderr
		assert len(final["closed"]) == 3, run
		assert len(final["dispatches"]) == 1, run


def test_fixture_cleanup_is_skipped_when_no_gate_run_failed() -> None:
	for runs in ([], [_queue_failed_gate(conclusion="success")]):
		with tempfile.TemporaryDirectory() as tmp:
			proc, final = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs, fixtures=_fixtures()))
		assert proc.returncode == 0, proc.stderr
		assert "AUTO_RELEASE_FIXTURE_CLEANUP" not in proc.stdout, runs
		assert not any("search/issues" in path or path == "api user --jq .login // \"\"" for path in _api_paths(final)), runs
		assert not final.get("closed"), runs
		assert len(final["dispatches"]) == 1, runs


def test_fixture_cleanup_does_not_run_while_a_gate_is_in_flight() -> None:
	# A running gate still owns its fixtures: the in-flight skip comes before
	# the cleanup, so even a failed earlier run's fixtures wait for a later tick.
	running = {**_queue_failed_gate(run_id=GATE_RUN + 5), "status": "in_progress", "conclusion": None}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), _state(tag_commit="3" * 40, runs=[running, _queue_failed_gate()], fixtures=_fixtures()))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight" in proc.stdout
	assert not any("search/issues" in path for path in _api_paths(final))
	assert not final.get("closed")
	assert not final.get("dispatches")


def test_fixture_cleanup_never_blocks_the_dispatch() -> None:
	base = {"tag_commit": "3" * 40, "runs": [_queue_failed_gate(conclusion="cancelled")], "fixtures": _fixtures()}
	for overrides, env, expected, closed in (
		({"search_error": True}, None, "AUTO_RELEASE_FIXTURE_CLEANUP outcome=read_failed lookup=search kind=issue", 0),
		({"user_error": True}, None, "AUTO_RELEASE_FIXTURE_CLEANUP outcome=read_failed lookup=user", 0),
		({"close_fails": [101]}, None, f"AUTO_RELEASE_FIXTURE_CLEANUP outcome=close_failed number=101 type=issue gate_run={GATE_RUN}", 2),
		({}, {"AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX": "1"}, "AUTO_RELEASE_FIXTURE_CLEANUP outcome=done closed=1 failed=0 skipped=2", 1),
		({}, {"AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_ENABLED": "false"}, f"AUTO_RELEASE_DISPATCHED sha={TIP}", 0),
	):
		with tempfile.TemporaryDirectory() as tmp:
			proc, final = _run(Path(tmp), _state(**base, **overrides), env=env)
		assert proc.returncode == 0, proc.stderr
		assert expected in proc.stdout, (overrides, env, proc.stdout)
		assert len(final.get("closed", [])) == closed, (overrides, env)
		assert len(final["dispatches"]) == 1, (overrides, env)


def test_workflow_passes_the_back_off_and_cleanup_knobs() -> None:
	wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	run_step = next(s for s in wf["jobs"]["release-check"]["steps"] if "run" in s)
	assert run_step["env"]["AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS"] == "${{ vars.AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS || '100' }}"
	assert run_step["env"]["AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS"] == "${{ vars.AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS || '24' }}"
	assert run_step["env"]["AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_ENABLED"] == "${{ vars.AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_ENABLED || 'true' }}"
	assert run_step["env"]["AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX"] == "${{ vars.AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX || '30' }}"


# The wedged-queued guard contract cases (issue #9473) live in
# tests/test_zombie_queued_guard_contract.py and are collected here, since CI
# already runs this file and ci.yml was outside that issue's file grant. A
# name clash would hide a test, so it fails loudly.
def _collect_zombie_queued_guard_contract_cases() -> None:
	import importlib.util

	spec = importlib.util.spec_from_file_location(
		"zombie_queued_guard_contract_cases",
		Path(__file__).resolve().parent / "test_zombie_queued_guard_contract.py",
	)
	cases = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(cases)
	for case_name, case in sorted(vars(cases).items()):
		if not case_name.startswith("test_") or not callable(case):
			continue
		assert case_name not in globals(), case_name
		globals()[case_name] = case


_collect_zombie_queued_guard_contract_cases()

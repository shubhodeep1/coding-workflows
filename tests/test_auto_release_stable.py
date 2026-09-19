#!/usr/bin/env python3
"""Tests for scripts/auto_release_stable.sh and its workflow wrapper."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "auto_release_stable.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-release-stable.yml"

TIP = "1" * 40
TAG_OBJECT = "2" * 40

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
if args[:1] == ["api"]:
    path = args[1]
    if path.endswith("/git/ref/heads/stable"):
        respond({"object": {"type": "commit", "sha": state["branch_tip"]}})
    if path.endswith("/git/ref/tags/stable"):
        if state.get("tag_missing"):
            save(); sys.stderr.write("404\n"); sys.exit(1)
        if state.get("tag_lookup_error"):
            save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
        respond({"object": {"type": "tag", "sha": state["tag_object"]}})
    if "/git/tags/" in path:
        respond({"object": {"type": "commit", "sha": state["tag_commit"]}})
    if "/actions/workflows/promote-main-to-stable.yml/runs" in path:
        respond({"workflow_runs": state.get("promote_runs", [])})
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
		}
	)
	for key in ("BASH_ENV", "ENV"):
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
	assert state["dispatches"] == [["test-and-mark-stable.yml", "--repo", "owner/repo", "--ref", "stable"]]
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
	runs = [{"status": "in_progress", "conclusion": None, "head_sha": TIP, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("dispatches")


def test_in_flight_promotion_run_skips() -> None:
	promote_runs = [{"status": "in_progress", "conclusion": None, "head_sha": TIP, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, promote_runs=promote_runs))
	assert proc.returncode == 0, proc.stderr
	assert "AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=1" in proc.stdout
	assert not state.get("dispatches")


def test_failed_gate_on_same_tip_skips_until_branch_moves() -> None:
	runs = [
		{"status": "completed", "conclusion": "success", "head_sha": TIP, "created_at": "2026-09-18T00:00:00Z"},
		{"status": "completed", "conclusion": "failure", "head_sha": TIP, "created_at": "2026-09-19T00:00:00Z"},
	]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs))
	assert proc.returncode == 0, proc.stderr
	assert f"AUTO_RELEASE_SKIPPED reason=last_gate_failed sha={TIP} conclusion=failure" in proc.stdout
	assert not state.get("dispatches")


def test_failed_gate_on_older_tip_does_not_block() -> None:
	runs = [{"status": "completed", "conclusion": "failure", "head_sha": "9" * 40, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), _state(tag_commit="3" * 40, runs=runs))
	assert proc.returncode == 0, proc.stderr
	assert len(state["dispatches"]) == 1


def test_workflow_contract() -> None:
	wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	assert wf["on"]["schedule"] == [{"cron": "0 */6 * * *"}]
	assert "workflow_dispatch" in wf["on"]
	assert wf["concurrency"]["group"] == "auto-release-stable-${{ github.repository }}"
	assert wf["concurrency"]["cancel-in-progress"] is False
	assert wf["permissions"] == {"contents": "read", "actions": "write"}
	run_step = next(s for s in wf["jobs"]["release-check"]["steps"] if "run" in s)
	assert "bash scripts/auto_release_stable.sh" in run_step["run"]
	assert run_step["env"]["AUTO_RELEASE_STABLE_ENABLED"] == "${{ vars.AUTO_RELEASE_STABLE_ENABLED || 'true' }}"

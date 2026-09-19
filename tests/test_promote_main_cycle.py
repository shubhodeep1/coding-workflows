#!/usr/bin/env python3
"""Tests for scripts/promote_main_cycle.sh (the daily promote cycle)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "promote_main_cycle.sh"

TIP = "1" * 40
TAG_COMMIT = "2" * 40
OLD_BASELINE = "3" * 40

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
    path = next(a for a in args[1:] if not a.startswith("-") and a != "GET")
    if path.endswith("/git/ref/heads/main"):
        respond({"object": {"type": "commit", "sha": state["main_tip"]}})
    if path.endswith("/git/ref/tags/stable"):
        if state.get("tag_missing"):
            save(); sys.stderr.write("404\n"); sys.exit(1)
        if state.get("tag_lookup_error"):
            save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
        respond({"object": {"type": "tag", "sha": "9" * 40}})
    if "/git/tags/" in path:
        respond({"object": {"type": "commit", "sha": state["tag_commit"]}})
    if "/compare/" in path:
        base = path.split("/compare/")[1].split("...")[0]
        entry = state.get("compares", {}).get(base)
        if entry is None:
            respond({"status": "ahead", "total_commits": 1, "files": [{"filename": "scripts/x.sh"}]})
        respond(entry)
    if path.startswith("search/issues"):
        q = next(a for a in args if a.startswith("q="))
        if "apply-analysis-cycle-baseline-sha:" in q:
            respond({"total_count": 1 if state.get("last_cycle_issue") else 0, "items": ([{"number": state["last_cycle_issue"]}] if state.get("last_cycle_issue") else [])})
        respond({"total_count": 0, "items": []})
    if "/issues/" in path and path.endswith("/comments?per_page=100"):
        respond(state.get("cycle_comments", [{"body": "apply-analysis-source-doc: x\napply-analysis-cycle-baseline-sha: " + state.get("last_cycle_baseline", "")}]))
    if "/issues?" in path:
        respond([{"number": n, "labels": []} for n in state.get("in_flight", [])])
    if "/actions/workflows/promote-main-to-stable.yml/runs" in path:
        respond({"workflow_runs": state.get("self_runs", [])})
    if "/actions/workflows/test-and-mark-stable.yml/runs" in path:
        tick = state.get("gate_tick", 0)
        state["gate_tick"] = tick + 1
        seq = state.get("gate_runs_sequence") or []
        runs = seq[min(tick, len(seq) - 1)] if seq else []
        respond({"workflow_runs": runs})
    save(); sys.stderr.write("unexpected api path " + path + "\n"); sys.exit(1)
if args[:2] == ["workflow", "run"]:
    state.setdefault("dispatches", []).append(args[2:])
    save(); sys.exit(0)
save(); sys.stderr.write("unexpected gh call " + " ".join(args) + "\n"); sys.exit(1)
'''

STUB_DISPATCHER = r'''#!/usr/bin/env bash
set -euo pipefail
if [ "${APPLY_ANALYSIS_LIST_ONLY:-false}" = "true" ]; then
  echo "APPLY_ANALYSIS_CANDIDATES count=${STUB_CANDIDATES:-2} docs=a,b"
  exit 0
fi
env | grep -E '^(APPLY_ANALYSIS_|GITHUB_SHA)' | sort > "${STUB_ENV_OUT}"
if [ "${STUB_DISPATCH_OUTCOME:-dispatched}" = "dispatched" ]; then
  echo "APPLY_ANALYSIS_DISPATCHED doc=analysis/workflow-optimization-2026-08-30.md role=${APPLY_ANALYSIS_ROLE}"
else
  echo "APPLY_ANALYSIS_SKIPPED reason=all_docs_processed"
fi
'''


def _run(tmp: Path, state: dict, env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict, Path]:
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(MOCK_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	stub = tmp / "dispatcher_stub.sh"
	stub.write_text(STUB_DISPATCHER, encoding="utf-8")
	stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
	env_out = tmp / "dispatcher_env.txt"
	state_file = tmp / "state.json"
	base_state = {"main_tip": TIP, "tag_commit": TAG_COMMIT, "self_runs": [], "compares": {}}
	base_state.update(state)
	state_file.write_text(json.dumps(base_state), encoding="utf-8")
	output_file = tmp / "out.txt"
	output_file.write_text("", encoding="utf-8")
	run_env = os.environ.copy()
	run_env.update(
		{
			"PATH": f"{bin_dir}:{run_env['PATH']}",
			"MOCK_GH_STATE": str(state_file),
			"GITHUB_REPOSITORY": "owner/repo",
			"GH_TOKEN": "x",
			"GITHUB_RUN_ID": "777",
			"GITHUB_OUTPUT": str(output_file),
			"APPLY_ANALYSIS_DISPATCHER": str(stub),
			"STUB_ENV_OUT": str(env_out),
			"PROMOTE_CYCLE_GATE_POLL_SECS": "1",
			"PROMOTE_CYCLE_GATE_WAIT_SECS": "6",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	for key in ("BASH_ENV", "ENV"):
		run_env.pop(key, None)
	if env:
		run_env.update(env)
	proc = subprocess.run(["bash", str(SCRIPT)], cwd=str(tmp), env=run_env, text=True, capture_output=True, timeout=120)
	final = json.loads(state_file.read_text(encoding="utf-8"))
	final["github_output"] = output_file.read_text(encoding="utf-8")
	return proc, final, env_out


def _compare(files: list[str], status: str = "ahead") -> dict:
	return {"status": status, "total_commits": 1, "files": [{"filename": f} for f in files]}


GATE_SUCCESS = [
	[],
	[{"id": 500, "status": "in_progress", "conclusion": None, "head_branch": "main", "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}],
	[{"id": 500, "status": "completed", "conclusion": "success", "head_branch": "main", "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}],
]


def test_kill_switch() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {}, env={"PROMOTE_CYCLE_ENABLED": "false"})
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=disabled" in proc.stdout
	assert final.get("calls", []) == []


def test_skips_when_another_cycle_run_is_active() -> None:
	runs = [{"id": 1, "status": "in_progress", "event": "schedule", "conclusion": None, "head_sha": TIP, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"self_runs": runs})
	assert "PROMOTE_CYCLE_SKIPPED reason=another_run_active active_runs=1" in proc.stdout
	assert not final.get("dispatches")


def test_own_run_id_is_not_counted_as_active() -> None:
	runs = [{"id": 777, "status": "in_progress", "event": "schedule", "conclusion": None, "head_sha": TIP, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"self_runs": runs, "compares": {TAG_COMMIT: _compare(["docs/a.md"])}})
	assert "reason=another_run_active" not in proc.stdout


def test_skips_when_cycle_in_flight() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"in_flight": [42]})
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout
	assert not final.get("dispatches")


def test_non_code_changes_do_not_start_a_cycle() -> None:
	files = ["analysis/workflow-optimization-2026-09-19.md", "ai-memory/x.json", "docs/plans/p.md", "README.md", "CHANGELOG.md", "tests/e2e_smoke_canary.txt"]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(files)}})
	assert proc.returncode == 0, proc.stderr
	assert f"PROMOTE_CYCLE_SKIPPED reason=no_code_changes base={TAG_COMMIT} head={TIP}" in proc.stdout
	assert not final.get("dispatches")


def test_claude_md_and_dot_claude_count_as_code() -> None:
	for path in ("CLAUDE.md", "workflow-templates/CLAUDE.md", ".claude/commands/investigate-issue.md"):
		with tempfile.TemporaryDirectory() as tmp:
			proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare([path, "docs/x.md"])}, "gate_runs_sequence": GATE_SUCCESS})
		assert "reason=no_code_changes" not in proc.stdout, path
		assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout, (path, proc.stdout, proc.stderr)


def test_identical_tip_and_tag_skips() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: {"status": "identical", "total_commits": 0, "files": []}}})
	assert "PROMOTE_CYCLE_SKIPPED reason=no_code_changes" in proc.stdout


def test_no_code_change_since_last_cycle_baseline_skips() -> None:
	state = {
		"compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), OLD_BASELINE: _compare(["analysis/a.md"])},
		"last_cycle_issue": 900,
		"last_cycle_baseline": OLD_BASELINE,
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert f"PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_last_cycle base={OLD_BASELINE} head={TIP}" in proc.stdout
	assert not final.get("dispatches")


def test_non_marker_comments_do_not_break_last_cycle_baseline_lookup() -> None:
	state = {
		"compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), OLD_BASELINE: _compare(["analysis/a.md"])},
		"last_cycle_issue": 900,
		"last_cycle_baseline": OLD_BASELINE,
		"cycle_comments": [
			{"body": "unrelated orchestrator state"},
			{"body": f"apply-analysis-cycle-baseline-sha: {OLD_BASELINE}"},
			{"body": "another progress comment"},
		],
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stderr
	assert "reason=guard_unavailable" not in proc.stdout
	assert "reason=no_code_changes_since_last_cycle" in proc.stdout
	assert not final.get("dispatches")


def test_tag_lookup_failure_fails_closed() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"tag_lookup_error": True})
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=guard_unavailable lookup=tag:stable" in proc.stdout
	assert not final.get("dispatches")


def test_no_code_change_since_failed_run_skips() -> None:
	failed_head = "4" * 40
	runs = [{"id": 2, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": failed_head, "created_at": "2026-09-18T00:00:00Z"}]
	state = {"self_runs": runs, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), failed_head: _compare(["README.md"])}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert f"PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_failed_run base={failed_head} head={TIP}" in proc.stdout
	assert not final.get("dispatches")


def test_insufficient_docs_skips_before_the_smoke_gate() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}}, env={"STUB_CANDIDATES": "1"})
	assert "PROMOTE_CYCLE_SKIPPED reason=insufficient_docs available=1 required=2" in proc.stdout
	assert not final.get("dispatches")


def test_full_cycle_runs_smoke_gate_then_dispatches_proving_run() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh", "README.md"])}, "gate_runs_sequence": GATE_SUCCESS})
		env_lines = env_out.read_text(encoding="utf-8").splitlines()
	assert proc.returncode == 0, proc.stderr + proc.stdout
	assert final["dispatches"] == [["test-and-mark-stable.yml", "--repo", "owner/repo", "--ref", "main", "-f", "gate_only=true", "-f", "gate_cycle_id=777"]]
	assert "Smoke gate passed on " + TIP + " (run 500)" in proc.stdout
	assert f"PROMOTE_CYCLE_DISPATCHED doc=analysis/workflow-optimization-2026-08-30.md baseline={TIP} smoke={TIP}" in proc.stdout
	assert "APPLY_ANALYSIS_ROLE=proving" in env_lines
	assert f"APPLY_ANALYSIS_CYCLE_BASELINE_SHA={TIP}" in env_lines
	assert f"APPLY_ANALYSIS_SMOKE_SHA={TIP}" in env_lines
	assert "outcome=dispatched" in final["github_output"]
	assert "smoke_run_id=500" in final["github_output"]


def test_smoke_gate_failure_fails_the_cycle_without_dispatching_a_proving_run() -> None:
	seq = [[], [{"id": 501, "status": "completed", "conclusion": "failure", "head_branch": "main", "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}]]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": seq})
		assert not env_out.exists()
	assert proc.returncode == 1
	assert "PROMOTE_CYCLE_FAILED reason=smoke_gate_failed run=501 conclusion=failure" in proc.stdout
	assert len(final["dispatches"]) == 1


def test_smoke_gate_timeout_fails_the_cycle() -> None:
	seq = [[], [{"id": 502, "status": "in_progress", "conclusion": None, "head_branch": "main", "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}]]
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": seq}, env={"PROMOTE_CYCLE_GATE_WAIT_SECS": "3"})
	assert proc.returncode == 1
	assert "PROMOTE_CYCLE_FAILED reason=smoke_gate_timeout" in proc.stdout


def test_smoke_gate_ignores_unrelated_concurrent_dispatch() -> None:
	seq = [
		[],
		[
			{"id": 503, "status": "completed", "conclusion": "failure", "head_branch": "main", "display_title": "Test & Mark Stable Release", "created_at": "2026-09-19T00:00:01Z"},
			{"id": 504, "status": "in_progress", "conclusion": None, "head_branch": "main", "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:02Z"},
		],
		[{"id": 504, "status": "completed", "conclusion": "success", "head_branch": "main", "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:02Z"}],
	]
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": seq})
	assert proc.returncode == 0, proc.stderr
	assert "Smoke gate run: 504" in proc.stdout
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout


def test_dispatcher_skip_after_gate_is_reported_as_skip() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_SUCCESS}, env={"STUB_DISPATCH_OUTCOME": "skipped"})
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=dispatcher:all_docs_processed smoke_run=500" in proc.stdout

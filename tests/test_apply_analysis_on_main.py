#!/usr/bin/env python3
"""Tests for scripts/apply_analysis_on_main.sh and its workflow wrapper.

The script is executed with a mocked `gh` so the decision order (kill switch,
in-flight guard, doc selection, dispatch, tracking-issue labelling, marker
comment) is validated against the real shell, not a reimplementation.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "apply_analysis_on_main.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "apply-analysis-on-main.yml"

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
    path = next(a for a in args[1:] if not a.startswith("-") and a not in ("GET",))
    if path.startswith("search/issues"):
        q = next(a for a in args if a.startswith("q="))
        total = 0
        for marker, count in state.get("search_hits", {}).items():
            if marker in q:
                total = count
        if state.get("search_fails"):
            save(); sys.stderr.write("search failed\n"); sys.exit(1)
        respond({"total_count": total, "items": []})
    if path.startswith("repos/") and "/issues?" in path:
        labels = path.split("labels=")[1].split("&")[0].replace("%3A", ":").replace("%2C", ",")
        tick = state.get("issue_list_tick", 0)
        state["issue_list_tick"] = tick + 1
        rows = state["open_tracking"]
        if isinstance(rows, dict):
            rows = rows.get(str(tick), rows["default"])
        out = [r for r in rows if all(l in r["labels"] for l in labels.split(","))]
        respond([{"number": r["number"], "labels": [{"name": l} for l in r["labels"]]} for r in out])
    if path.startswith("repos/") and path.endswith("/comments"):
        body = next(a for a in args if a.startswith("body=")).split("=", 1)[1]
        state.setdefault("comments", []).append({"path": path, "body": body})
        respond({"id": 1})
    save(); sys.stderr.write("unexpected api path " + path + "\n"); sys.exit(1)
if args[:2] == ["workflow", "run"]:
    state.setdefault("dispatches", []).append(args[2:])
    save(); sys.exit(0)
if args[:2] == ["issue", "edit"]:
    state.setdefault("label_edits", []).append(args[2:])
    save(); sys.exit(0)
save(); sys.stderr.write("unexpected gh call " + " ".join(args) + "\n"); sys.exit(1)
'''


def _run(tmp: Path, state: dict, env: dict[str, str] | None = None, docs: list[str] | None = None, report: str | None = None) -> tuple[subprocess.CompletedProcess[str], dict]:
	repo = tmp / "repo"
	(repo / "scripts").mkdir(parents=True)
	(repo / "analysis").mkdir()
	# The script cds to its own parent's parent, so install it inside the temp repo.
	(repo / "scripts" / "apply_analysis_on_main.sh").write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
	for doc in docs or []:
		(repo / doc).write_text("# rec\n", encoding="utf-8")
	if report is not None:
		(repo / "analysis" / "recommendation-processing-report.md").write_text(report, encoding="utf-8")
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
			"GITHUB_SHA": "a" * 40,
			"GITHUB_RUN_ID": "42",
			"GITHUB_OUTPUT": str(output_file),
			"APPLY_ANALYSIS_TRACKING_POLL_SECS": "1",
			"APPLY_ANALYSIS_TRACKING_WAIT_SECS": "5",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	for key in ("BASH_ENV", "ENV"):
		run_env.pop(key, None)
	if env:
		run_env.update(env)
	proc = subprocess.run(
		["bash", str(repo / "scripts" / "apply_analysis_on_main.sh")],
		cwd=str(repo),
		env=run_env,
		text=True,
		capture_output=True,
		timeout=120,
	)
	final_state = json.loads(state_file.read_text(encoding="utf-8"))
	final_state["github_output"] = output_file.read_text(encoding="utf-8")
	return proc, final_state


def _tracking(number: int, *extra: str) -> dict:
	return {"number": number, "labels": ["ai:orchestrator-tracking", *extra]}


def test_kill_switch_skips_without_api_calls() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), {"open_tracking": []}, env={"APPLY_ANALYSIS_ON_MAIN_ENABLED": "false"}, docs=["analysis/workflow-optimization-2026-09-01.md"])
	assert proc.returncode == 0, proc.stderr
	assert "APPLY_ANALYSIS_SKIPPED reason=disabled" in proc.stdout
	assert state.get("calls", []) == []
	assert "dispatched=false" in state["github_output"]


def test_in_flight_project_holds() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), {"open_tracking": [_tracking(7, "ai:comprehensive-test-pending")]}, docs=["analysis/workflow-optimization-2026-09-01.md"])
	assert proc.returncode == 0, proc.stderr
	assert "APPLY_ANALYSIS_SKIPPED reason=project_in_flight tracking_issues=7" in proc.stdout
	assert not state.get("dispatches")


def test_no_docs_holds() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), {"open_tracking": []})
	assert proc.returncode == 0, proc.stderr
	assert "APPLY_ANALYSIS_SKIPPED reason=no_docs" in proc.stdout
	assert not state.get("dispatches")


def test_dispatches_oldest_unprocessed_doc_and_labels_new_tracking_issue() -> None:
	docs = [
		"analysis/workflow-optimization-2026-09-03.md",
		"analysis/workflow-optimization-2026-08-30.md",
		"analysis/workflow-optimization-2026-09-01.md",
	]
	state = {
		# tick 0: in-flight guard; tick 1: baseline; tick 2+: after dispatch.
		"open_tracking": {"0": [_tracking(3)], "1": [_tracking(3)], "default": [_tracking(3), _tracking(9)]},
		"search_hits": {"analysis/workflow-optimization-2026-08-30.md": 1},
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state, docs=docs)
	assert proc.returncode == 0, proc.stderr + proc.stdout
	# 08-30 was dispatched before (marker found) -> skipped with a warning; 09-01 is next.
	assert "workflow-optimization-2026-08-30.md was dispatched to the orchestrator before" in proc.stdout
	assert "APPLY_ANALYSIS_DISPATCHED doc=analysis/workflow-optimization-2026-09-01.md tracking_issue=9" in proc.stdout
	assert len(final["dispatches"]) == 1
	dispatch = final["dispatches"][0]
	assert dispatch[0] == "internal-orchestrate.yml"
	assert "--ref" in dispatch and dispatch[dispatch.index("--ref") + 1] == "main"
	description = next(a for a in dispatch if a.startswith("project_description="))
	assert description.startswith("project_description=Apply analysis recommendations from workflow-optimization-2026-09-01.md\n")
	assert "process ONLY the single source doc" in description
	assert "Source doc: analysis/workflow-optimization-2026-09-01.md" in description
	assert "workflow-optimization-2026-09-03.md" not in description
	assert final["label_edits"] == [["9", "--repo", "owner/repo", "--add-label", "ai:comprehensive-test-pending"]]
	assert len(final["comments"]) == 1
	assert final["comments"][0]["path"] == "repos/owner/repo/issues/9/comments"
	assert "apply-analysis-source-doc: analysis/workflow-optimization-2026-09-01.md" in final["comments"][0]["body"]
	assert "dispatched=true" in final["github_output"]
	assert "tracking_issue=9" in final["github_output"]


def test_doc_listed_in_processing_report_is_skipped_without_search() -> None:
	docs = ["analysis/workflow-optimization-2026-08-30.md"]
	report = "## Processed source docs\n- `analysis/workflow-optimization-2026-08-30.md`\n"
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), {"open_tracking": []}, docs=docs, report=report)
	assert proc.returncode == 0, proc.stderr
	assert "APPLY_ANALYSIS_SKIPPED reason=all_docs_processed docs=1" in proc.stdout
	assert not any(call[:1] == ["api"] and "search/issues" in " ".join(call) for call in final["calls"])
	assert not final.get("dispatches")


def test_search_failure_fails_closed() -> None:
	docs = ["analysis/workflow-optimization-2026-08-30.md"]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), {"open_tracking": [], "search_fails": True}, docs=docs)
	assert proc.returncode == 0, proc.stderr
	assert "APPLY_ANALYSIS_SKIPPED reason=guard_unavailable" in proc.stdout
	assert not final.get("dispatches")


def test_missing_tracking_issue_fails_loudly_after_dispatch() -> None:
	docs = ["analysis/workflow-optimization-2026-08-30.md"]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), {"open_tracking": []}, docs=docs, env={"APPLY_ANALYSIS_TRACKING_WAIT_SECS": "2"})
	assert proc.returncode == 1
	assert len(final["dispatches"]) == 1
	assert "No new tracking issue appeared" in proc.stderr + proc.stdout
	assert "dispatched=true" in final["github_output"]
	assert not final.get("label_edits")


def test_workflow_contract() -> None:
	wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	assert wf["on"]["push"]["branches"] == ["main"]
	assert "workflow_dispatch" in wf["on"]
	assert wf["concurrency"]["group"] == "apply-analysis-on-main-${{ github.repository }}"
	assert wf["concurrency"]["cancel-in-progress"] is False
	assert wf["permissions"] == {"contents": "read", "issues": "write", "actions": "write"}
	steps = wf["jobs"]["dispatch"]["steps"]
	run_step = next(s for s in steps if "run" in s)
	assert "bash scripts/apply_analysis_on_main.sh" in run_step["run"]
	assert run_step["env"]["APPLY_ANALYSIS_ON_MAIN_ENABLED"] == "${{ vars.APPLY_ANALYSIS_ON_MAIN_ENABLED || 'true' }}"
	assert run_step["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT || github.token }}"

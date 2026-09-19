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
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "promote-main-to-stable.yml"

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
    if "/actions/workflows/" in path and "/runs" in path:
        respond({"workflow_runs": state.get("orchestrate_runs", [])})
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
			"GITHUB_REF_NAME": "main",
			"GITHUB_OUTPUT": str(output_file),
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


def test_dispatches_oldest_unprocessed_doc_bound_by_label_and_marker_inputs() -> None:
	docs = [
		"analysis/workflow-optimization-2026-09-03.md",
		"analysis/workflow-optimization-2026-08-30.md",
		"analysis/workflow-optimization-2026-09-01.md",
	]
	state = {
		"open_tracking": [_tracking(3)],
		"search_hits": {"analysis/workflow-optimization-2026-08-30.md": 1},
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(
			Path(tmp),
			state,
			docs=docs,
			env={
				"APPLY_ANALYSIS_ROLE": "verifying",
				"APPLY_ANALYSIS_CYCLE_BASELINE_SHA": "c" * 40,
				"APPLY_ANALYSIS_SMOKE_SHA": "b" * 40,
				"APPLY_ANALYSIS_PROMOTE_SHA": "a" * 40,
				"APPLY_ANALYSIS_PROVING_MERGE_SHA": "d" * 40,
			},
		)
	assert proc.returncode == 0, proc.stderr + proc.stdout
	# 08-30 was dispatched before (marker found) -> skipped with a warning; 09-01 is next.
	assert "workflow-optimization-2026-08-30.md was dispatched to the orchestrator before" in proc.stderr
	assert "APPLY_ANALYSIS_DISPATCHED doc=analysis/workflow-optimization-2026-09-01.md role=verifying" in proc.stdout
	assert len(final["dispatches"]) == 1
	dispatch = final["dispatches"][0]
	assert dispatch[0] == "internal-orchestrate.yml"
	assert dispatch[dispatch.index("--ref") + 1] == "main"
	fields = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in dispatch if "=" in a and not a.startswith("--")}
	assert fields["project_description"].startswith("Apply analysis recommendations from workflow-optimization-2026-09-01.md\n")
	assert "process ONLY the single source doc" in fields["project_description"]
	assert "Source doc: analysis/workflow-optimization-2026-09-01.md" in fields["project_description"]
	assert "workflow-optimization-2026-09-03.md" not in fields["project_description"]
	assert fields["tracking_labels"] == "ai:comprehensive-test-pending"
	marker = fields["tracking_comment"]
	assert "apply-analysis-source-doc: analysis/workflow-optimization-2026-09-01.md" in marker
	assert "apply-analysis-role: verifying" in marker
	assert "apply-analysis-cycle-baseline-sha: " + "c" * 40 in marker
	assert "apply-analysis-smoke-sha: " + "b" * 40 in marker
	assert "apply-analysis-promote-sha: " + "a" * 40 in marker
	assert "apply-analysis-proving-merge-sha: " + "d" * 40 in marker
	assert not final.get("label_edits") and not final.get("comments")
	assert "dispatched=true" in final["github_output"]
	assert "role=verifying" in final["github_output"]


def test_in_flight_guard_can_exclude_the_completing_proving_issue() -> None:
	docs = ["analysis/workflow-optimization-2026-08-30.md"]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(
			Path(tmp),
			{"open_tracking": [_tracking(7, "ai:comprehensive-test-pending")]},
			docs=docs,
			env={"APPLY_ANALYSIS_IN_FLIGHT_EXCLUDE_ISSUE": "7"},
		)
	assert proc.returncode == 0, proc.stderr
	assert len(final["dispatches"]) == 1


def test_orchestrate_run_in_flight_holds() -> None:
	docs = ["analysis/workflow-optimization-2026-08-30.md"]
	state = {"open_tracking": [], "orchestrate_runs": [{"id": 5, "status": "in_progress", "conclusion": None}]}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state, docs=docs)
	assert proc.returncode == 0, proc.stderr
	assert "APPLY_ANALYSIS_SKIPPED reason=orchestrate_run_in_flight" in proc.stdout
	assert not final.get("dispatches")


def test_list_only_reports_unprocessed_docs_without_dispatching() -> None:
	docs = [
		"analysis/workflow-optimization-2026-08-30.md",
		"analysis/workflow-optimization-2026-09-01.md",
		"analysis/workflow-optimization-2026-09-03.md",
	]
	report = "- `analysis/workflow-optimization-2026-08-30.md`\n"
	state = {"open_tracking": [_tracking(7, "ai:comprehensive-test-pending")], "search_hits": {"analysis/workflow-optimization-2026-09-01.md": 1}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), state, docs=docs, report=report, env={"APPLY_ANALYSIS_LIST_ONLY": "true"})
	assert proc.returncode == 0, proc.stderr
	assert "APPLY_ANALYSIS_CANDIDATES count=1 docs=analysis/workflow-optimization-2026-09-03.md" in proc.stdout
	assert not final.get("dispatches")
	assert "candidate_count=1" in final["github_output"]
	# list-only never consults the in-flight or orchestrate-run guards
	assert not any("/issues?" in " ".join(c) for c in final["calls"])


def test_invalid_role_or_sha_is_rejected() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, _ = _run(Path(tmp), {"open_tracking": []}, docs=["analysis/workflow-optimization-2026-08-30.md"], env={"APPLY_ANALYSIS_ROLE": "other"})
	assert proc.returncode == 1
	with tempfile.TemporaryDirectory() as tmp:
		proc, _ = _run(Path(tmp), {"open_tracking": []}, docs=["analysis/workflow-optimization-2026-08-30.md"], env={"APPLY_ANALYSIS_SMOKE_SHA": "nope"})
	assert proc.returncode == 1


def test_doc_listed_in_processing_report_is_skipped_without_search() -> None:
	docs = ["analysis/workflow-optimization-2026-08-30.md"]
	report = "## Processed source docs\n- `analysis/workflow-optimization-2026-08-30.md`\n"
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), {"open_tracking": []}, docs=docs, report=report)
	assert proc.returncode == 0, proc.stderr
	assert "APPLY_ANALYSIS_SKIPPED reason=all_docs_processed" in proc.stdout
	assert not any(call[:1] == ["api"] and "search/issues" in " ".join(call) for call in final["calls"])
	assert not final.get("dispatches")


def test_search_failure_fails_closed() -> None:
	docs = ["analysis/workflow-optimization-2026-08-30.md"]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final = _run(Path(tmp), {"open_tracking": [], "search_fails": True}, docs=docs)
	assert proc.returncode == 0, proc.stderr
	assert "APPLY_ANALYSIS_SKIPPED reason=guard_unavailable" in proc.stdout
	assert not final.get("dispatches")


def test_promote_workflow_cycle_job_runs_the_cycle_script_daily() -> None:
	wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	assert wf["on"]["schedule"] == [{"cron": "0 0 * * *"}]
	assert "workflow_dispatch" in wf["on"]
	assert wf["on"]["workflow_dispatch"]["inputs"]["target_sha"]["default"] == ""
	assert "concurrency" not in wf, "workflow-level concurrency would queue promotions behind a waiting cycle"
	cycle = wf["jobs"]["cycle"]
	assert cycle["if"] == "github.event_name == 'schedule'"
	assert "concurrency" not in cycle, "job-level concurrency would queue a later scheduled tick"
	run_step = next(s for s in cycle["steps"] if "run" in s)
	assert "bash scripts/promote_main_cycle.sh" in run_step["run"]
	assert run_step["env"]["PROMOTE_CYCLE_ENABLED"] == "${{ vars.PROMOTE_CYCLE_ENABLED || 'true' }}"
	assert run_step["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT || github.token }}"
	promote = wf["jobs"]["promote"]
	assert promote["if"] == "github.event_name == 'workflow_dispatch'"
	assert promote["concurrency"] == {"group": "promote-main-to-stable", "cancel-in-progress": False}
	ff_step = next(s for s in promote["steps"] if s.get("id") == "ff")
	assert ff_step["env"]["TARGET_SHA_INPUT"] == "${{ inputs.target_sha }}"
	assert 'git merge --ff-only "${TARGET}"' in ff_step["run"]
	assert "git merge-base --is-ancestor \"${TARGET_SHA_INPUT}\" origin/main" in ff_step["run"]


def test_release_gate_only_mode_contract() -> None:
	gate = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml").read_text(encoding="utf-8"))
	assert gate["on"]["workflow_dispatch"]["inputs"]["gate_only"]["default"] is False
	assert gate["on"]["workflow_dispatch"]["inputs"]["gate_cycle_id"]["default"] == ""
	assert "[cycle:{0}]" in gate["run-name"]
	assert gate["jobs"]["release"]["if"] == "${{ success() && !inputs.gate_only }}"
	assert "!inputs.gate_only" in gate["jobs"]["sync-to-main"]["if"]
	source_run = gate["jobs"]["source"]["steps"][0]["run"]
	assert gate["jobs"]["source"]["steps"][0]["env"]["GATE_ONLY"] == "${{ inputs.gate_only }}"
	assert 'if [ "${GATE_ONLY}" = "true" ]; then' in source_run
	assert 'echo "branch=${REF}" >> "$GITHUB_OUTPUT"' in source_run
	notify_run = next(step["run"] for step in gate["jobs"]["notify"]["steps"] if step.get("id") == "tg_send")
	assert 'GATE_ONLY="${{ inputs.gate_only }}"' in notify_run
	assert '[ "$GATE_ONLY" = "true" ] && [ "$RELEASE" = "skipped" ]' in notify_run


def test_orchestrate_workflow_accepts_tracking_bindings() -> None:
	orchestrate = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "orchestrate.yml").read_text(encoding="utf-8"))
	inputs = orchestrate["on"]["workflow_call"]["inputs"]
	assert inputs["tracking_labels"]["default"] == ""
	assert inputs["tracking_comment"]["default"] == ""
	internal = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "internal-orchestrate.yml").read_text(encoding="utf-8"))
	assert internal["jobs"]["orchestrate"]["with"]["tracking_labels"] == "${{ inputs.tracking_labels }}"
	assert internal["jobs"]["orchestrate"]["with"]["tracking_comment"] == "${{ inputs.tracking_comment }}"
	text = (REPO_ROOT / ".github" / "workflows" / "orchestrate.yml").read_text(encoding="utf-8")
	assert "TRACKING_LABELS_INPUT: ${{ inputs.tracking_labels }}" in text
	assert "source scripts/label_helpers.sh" in text.split("- name: Create tracking issue", 1)[1]
	assert "TRACKING_ISSUE_COMMENT_POSTED" in text

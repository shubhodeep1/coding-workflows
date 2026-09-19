#!/usr/bin/env python3
"""Promote-cycle hooks in the orchestrator poller (README 12f).

A PROVING run's completion dispatches the VERIFYING run; a VERIFYING run's
ready-to-merge point promotes the pinned commit and holds its own final
merge until the release finishes. Driven through the real poller with the
mocked `gh` from test_orchestrate_poll_process.py.
"""

from __future__ import annotations

import stat
import tempfile
from pathlib import Path

from tests.test_orchestrate_poll_process import _base_state, _run_poller

PROMOTE_SHA = "a" * 40
SMOKE_SHA = "b" * 40
BASELINE_SHA = "c" * 40
PROVING_MERGE_SHA = "d" * 40
HUMAN_SHA = "e" * 40
BOT_SHA = "f" * 40

LABEL = "ai:comprehensive-test-pending"


def _marker(role: str, **extra: str) -> str:
	lines = [
		"<!-- apply-analysis-source-doc -->",
		"## Apply-analysis dispatch",
		"",
		"apply-analysis-source-doc: analysis/workflow-optimization-2026-09-01.md",
		f"apply-analysis-role: {role}",
	]
	for key, value in extra.items():
		lines.append(f"apply-analysis-{key.replace('_', '-')}: {value}")
	lines.append("")
	lines.append("Dispatched by the promote cycle.")
	return "\n".join(lines)


def _open_final_pr(number: int = 360) -> dict:
	return {
		"number": number,
		"state": "open",
		"baseRefName": "main",
		"headRefName": "orchestrator/project-192",
		"mergeable": True,
		"mergeable_state": "clean",
	}


def _stub_dispatcher(tmp: Path, *, outcome: str = "dispatched") -> tuple[Path, Path]:
	env_out = tmp / "dispatcher_env.txt"
	script = tmp / "dispatcher_stub.sh"
	if outcome == "dispatched":
		body = 'echo "APPLY_ANALYSIS_DISPATCHED doc=analysis/workflow-optimization-2026-09-03.md role=${APPLY_ANALYSIS_ROLE}"\n'
	else:
		body = 'echo "APPLY_ANALYSIS_SKIPPED reason=no_docs"\n'
	script.write_text(
		"#!/usr/bin/env bash\nset -euo pipefail\nenv | grep -E '^(APPLY_ANALYSIS_|GITHUB_REF_NAME|GITHUB_SHA)' | sort > \"" + str(env_out) + "\"\n" + body,
		encoding="utf-8",
	)
	script.chmod(script.stat().st_mode | stat.S_IEXEC)
	return script, env_out


def _project_state() -> dict:
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	return state


def test_proving_run_completion_dispatches_verifying_run() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		stub, env_out = _stub_dispatcher(Path(tmp))
		result = _run_poller(
			state=_project_state(),
			enable_validation="false",
			max_validate_cycles="3",
			tracking_labels=[LABEL],
			tracking_comments=[_marker("proving", cycle_baseline_sha=BASELINE_SHA, smoke_sha=SMOKE_SHA)],
			issue_labels={10: ["ai:merged"]},
			prs=[dict(_open_final_pr(), merge_commit_sha=PROVING_MERGE_SHA)],
			existing_branches=["main", "orchestrator/project-192"],
			branch_ref_shas={"main": PROMOTE_SHA},
			env_overrides={"APPLY_ANALYSIS_DISPATCHER": str(stub)},
		)
		assert result["latest_state"]["status"] == "complete"
		callback = result["latest_state"]["comprehensive_release_callback"]
		assert callback["handled"] is True
		assert callback["role"] == "proving"
		assert callback["verification"].startswith("DISPATCHED doc=analysis/workflow-optimization-2026-09-03.md")
		assert result["release_dispatches"] == []
		assert LABEL not in result["tracking_labels"]
		env_lines = env_out.read_text(encoding="utf-8").splitlines()
		assert "APPLY_ANALYSIS_ROLE=verifying" in env_lines
		assert f"APPLY_ANALYSIS_PROMOTE_SHA={PROMOTE_SHA}" in env_lines
		assert f"APPLY_ANALYSIS_SMOKE_SHA={SMOKE_SHA}" in env_lines
		assert f"APPLY_ANALYSIS_CYCLE_BASELINE_SHA={BASELINE_SHA}" in env_lines
		exclude = [line for line in env_lines if line.startswith("APPLY_ANALYSIS_IN_FLIGHT_EXCLUDE_ISSUE=")]
		assert len(exclude) == 1 and exclude[0].split("=", 1)[1].isdigit()
		assert "COMPREHENSIVE_VERIFICATION_DISPATCHED" in result["stdout"]


def test_proving_run_completion_reports_when_verifying_run_cannot_be_dispatched() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		stub, _ = _stub_dispatcher(Path(tmp), outcome="skipped")
		result = _run_poller(
			state=_project_state(),
			enable_validation="false",
			max_validate_cycles="3",
			tracking_labels=[LABEL],
			tracking_comments=[_marker("proving", cycle_baseline_sha=BASELINE_SHA, smoke_sha=SMOKE_SHA)],
			issue_labels={10: ["ai:merged"]},
			prs=[_open_final_pr()],
			existing_branches=["main", "orchestrator/project-192"],
			branch_ref_shas={"main": PROMOTE_SHA},
			env_overrides={"APPLY_ANALYSIS_DISPATCHER": str(stub)},
		)
		assert result["latest_state"]["status"] == "complete"
		assert result["latest_state"]["comprehensive_release_callback"]["verification"] == "SKIPPED reason=no_docs"
		assert result["release_dispatches"] == []
		assert "COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED" in result["stdout"]


def test_proving_run_retries_when_default_branch_ref_is_unavailable() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		stub, env_out = _stub_dispatcher(Path(tmp))
		result = _run_poller(
			state=_project_state(),
			enable_validation="false",
			max_validate_cycles="3",
			tracking_labels=[LABEL],
			tracking_comments=[_marker("proving", cycle_baseline_sha=BASELINE_SHA, smoke_sha=SMOKE_SHA)],
			issue_labels={10: ["ai:merged"]},
			prs=[_open_final_pr()],
			existing_branches=["main", "orchestrator/project-192"],
			env_overrides={"APPLY_ANALYSIS_DISPATCHER": str(stub)},
		)
		assert not env_out.exists()
	assert "comprehensive_release_callback" not in result["latest_state"]
	assert LABEL in result["tracking_labels"]
	assert "COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED" in result["stdout"]
	assert "RETRY reason=default_branch_unavailable" in result["stdout"]


def _verifying_run(state: dict, *, compare_commits: list[dict], commit_files: dict | None = None, runs_by_file: dict | None = None, prs: list[dict] | None = None):
	extra = {"compare_commits_detail": compare_commits}
	if commit_files:
		extra["commit_files"] = commit_files
	if runs_by_file:
		extra["workflow_runs_by_file"] = runs_by_file
	return _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=[LABEL],
		tracking_comments=[
			_marker(
				"verifying",
				cycle_baseline_sha=BASELINE_SHA,
				smoke_sha=SMOKE_SHA,
				promote_sha=PROMOTE_SHA,
				proving_merge_sha=PROVING_MERGE_SHA,
			)
		],
		issue_labels={10: ["ai:merged"]},
		prs=prs or [_open_final_pr()],
		existing_branches=["main", "orchestrator/project-192"],
		mock_store_extra=extra,
	)


def _commit(sha: str, message: str, author: str, committer: str = "web-flow") -> dict:
	return {"sha": sha, "commit": {"message": message}, "author": {"login": author}, "committer": {"login": committer}, "parents": [{"sha": "p"}]}


def test_verifying_ready_to_merge_promotes_pinned_sha_and_holds_merge() -> None:
	commits = [
		_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1"),
		_commit(BOT_SHA, "chore: update validation self-test status", "github-actions[bot]", "github-actions[bot]"),
		_commit("1" * 40, "Merge pull request #401 from shubhodeep1/auto/forward-merge-stable-123-1", "shubhodeep1"),
	]
	result = _verifying_run(
		_project_state(),
		compare_commits=commits,
		commit_files={BOT_SHA: ["analysis/validation-selftest-status.json", "docs/notes.md"]},
	)
	promotion = result["latest_state"]["comprehensive_promotion"]
	assert promotion["status"] == "dispatched", promotion
	assert promotion["promote_sha"] == PROMOTE_SHA
	assert result["latest_state"]["final_merge_status"] != "merged"
	assert result["latest_state"]["status"] != "complete"
	assert len(result["release_dispatches"]) == 1
	dispatch = result["release_dispatches"][0]
	assert dispatch["workflow"] == "promote-main-to-stable.yml"
	assert dispatch["ref"] == "main"
	assert dispatch["target_sha"] == PROMOTE_SHA
	assert dispatch["skip_e2e"] == "false"
	assert "COMPREHENSIVE_PROMOTION_DISPATCHED" in result["stdout"]
	assert "held by the promote cycle" in result["stdout"]


def test_verifying_hold_releases_after_successful_release_run() -> None:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	first = _verifying_run(_project_state(), compare_commits=commits)
	assert first["latest_state"]["comprehensive_promotion"]["status"] == "dispatched"
	runs = {
		"promote-main-to-stable.yml": [
			{"id": 1, "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "head_branch": "main", "created_at": "2099-01-01T00:00:00Z"}
		],
		"test-and-mark-stable.yml": [
			{"id": 2, "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "head_branch": "stable", "created_at": "2099-01-01T00:10:00Z"}
		],
	}
	second = _verifying_run(first["latest_state"], compare_commits=commits, runs_by_file=runs, prs=first["prs"])
	assert second["latest_state"]["comprehensive_promotion"]["status"] == "promoted"
	assert second["latest_state"]["final_merge_status"] == "merged"
	assert second["latest_state"]["status"] == "complete"
	assert second["release_dispatches"] == []
	assert second["latest_state"]["comprehensive_release_callback"]["role"] == "verifying"
	assert "COMPREHENSIVE_PROMOTION_DONE" in second["stdout"]


def test_verifying_hold_releases_and_marks_failed_when_release_run_fails() -> None:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	first = _verifying_run(_project_state(), compare_commits=commits)
	runs = {
		"promote-main-to-stable.yml": [
			{"id": 1, "event": "workflow_dispatch", "status": "completed", "conclusion": "failure", "head_branch": "main", "created_at": "2099-01-01T00:00:00Z"}
		],
	}
	second = _verifying_run(first["latest_state"], compare_commits=commits, runs_by_file=runs, prs=first["prs"])
	assert second["latest_state"]["comprehensive_promotion"]["status"] == "failed"
	assert second["latest_state"]["final_merge_status"] == "merged"
	assert "COMPREHENSIVE_PROMOTION_FAILED" in second["stdout"]


def test_verifying_defers_promotion_when_untested_commit_landed() -> None:
	commits = [
		_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1"),
		_commit(HUMAN_SHA, "review_autofix: bind auto-merge to head (#4110)", "shubhodeep1"),
	]
	result = _verifying_run(_project_state(), compare_commits=commits)
	promotion = result["latest_state"]["comprehensive_promotion"]
	assert promotion["status"] == "deferred"
	assert promotion["untested_count"] == 1
	assert result["release_dispatches"] == []
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert result["latest_state"]["status"] == "complete"
	assert "COMPREHENSIVE_PROMOTION_DEFERRED" in result["stdout"]


def test_bot_commit_touching_code_counts_as_untested() -> None:
	commits = [
		_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1"),
		_commit(BOT_SHA, "chore: tweak", "github-actions[bot]", "github-actions[bot]"),
	]
	result = _verifying_run(_project_state(), compare_commits=commits, commit_files={BOT_SHA: ["analysis/report.md", ".claude/commands/x.md"]})
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "deferred"
	assert result["release_dispatches"] == []


def test_legacy_label_without_marker_keeps_release_callback() -> None:
	result = _run_poller(
		state=_project_state(),
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=[LABEL],
		issue_labels={10: ["ai:merged"]},
		prs=[_open_final_pr()],
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert len(result["release_dispatches"]) == 1
	assert result["release_dispatches"][0]["workflow"] == "promote-main-to-stable.yml"
	assert "target_sha" not in result["release_dispatches"][0]

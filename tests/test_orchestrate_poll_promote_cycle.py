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


def _trusted(marker: str) -> dict:
	"""The orchestrator posts the marker with GH_PAT, i.e. as the repository owner."""
	return {"body": marker, "author_association": "OWNER", "user": {"login": "shubhodeep1"}}


def _untrusted(marker: str) -> dict:
	return {"body": marker, "author_association": "CONTRIBUTOR", "user": {"login": "drive-by"}}


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
			tracking_comments=[_trusted(_marker("proving", cycle_baseline_sha=BASELINE_SHA, smoke_sha=SMOKE_SHA))],
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
			tracking_comments=[_trusted(_marker("proving", cycle_baseline_sha=BASELINE_SHA, smoke_sha=SMOKE_SHA))],
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
			tracking_comments=[_trusted(_marker("proving", cycle_baseline_sha=BASELINE_SHA, smoke_sha=SMOKE_SHA))],
			issue_labels={10: ["ai:merged"]},
			prs=[_open_final_pr()],
			existing_branches=["main", "orchestrator/project-192"],
			env_overrides={"APPLY_ANALYSIS_DISPATCHER": str(stub)},
		)
		assert not env_out.exists()
	callback = result["latest_state"].get("comprehensive_release_callback", {})
	assert callback.get("handled") is not True
	assert callback.get("verification_retries") == 1
	assert LABEL in result["tracking_labels"]
	assert "COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED" in result["stdout"]
	assert "RETRY reason=default_branch_unavailable" in result["stdout"]


def _verifying_run(state: dict, *, compare_commits: list[dict], commit_files: dict | None = None, runs_by_file: dict | None = None, prs: list[dict] | None = None, tag_commit: str = "9" * 40, untrusted_marker: bool = False, env_overrides: dict | None = None):
	extra = {"compare_commits_detail": compare_commits, "tag_refs": {"stable": {"type": "tag", "sha": "8" * 40}}, "tag_objects": {"8" * 40: tag_commit}}
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
			(untrusted_marker and _untrusted or _trusted)(
				_marker(
					"verifying",
					cycle_baseline_sha=BASELINE_SHA,
					smoke_sha=SMOKE_SHA,
					promote_sha=PROMOTE_SHA,
					proving_merge_sha=PROVING_MERGE_SHA,
				)
			)
		],
		issue_labels={10: ["ai:merged"]},
		prs=prs or [_open_final_pr()],
		existing_branches=["main", "orchestrator/project-192"],
		mock_store_extra=extra,
		env_overrides=env_overrides,
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
		prs=[_open_final_pr(), {"number": 401, "state": "closed", "merged": True, "merged_at": "2026-09-18T00:00:00Z", "baseRefName": "main", "headRefName": "auto/forward-merge-stable-123-1", "merge_commit_sha": "1" * 40}],
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
	assert first["latest_state"]["comprehensive_promotion"]["tag_before"] == "9" * 40
	# A concurrent unrelated release run on stable is NOT enough: the tag has not moved.
	runs = {
		"test-and-mark-stable.yml": [
			{"id": 2, "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "head_branch": "stable", "created_at": "2099-01-01T00:10:00Z"}
		],
	}
	still = _verifying_run(first["latest_state"], compare_commits=commits, runs_by_file=runs, prs=first["prs"])
	assert still["latest_state"]["comprehensive_promotion"]["status"] == "dispatched"
	assert still["latest_state"]["final_merge_status"] != "merged"
	# The stable tag now points at a descendant of promote_sha: promoted.
	second = _verifying_run(still["latest_state"], compare_commits=commits, prs=first["prs"], tag_commit="7" * 40)
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


def test_forward_merge_subject_without_matching_pr_counts_as_untested() -> None:
	commits = [
		_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1"),
		_commit(HUMAN_SHA, "Merge pull request #402 from shubhodeep1/auto/forward-merge-stable-999-1", "shubhodeep1"),
	]
	# PR 402 exists but is an ordinary feature branch: the subject is spoofed.
	spoof = {"number": 402, "state": "closed", "merged": True, "baseRefName": "main", "headRefName": "feature/not-a-forward-merge", "merge_commit_sha": HUMAN_SHA}
	result = _verifying_run(_project_state(), compare_commits=commits, prs=[_open_final_pr(), spoof])
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "deferred"
	assert result["release_dispatches"] == []


def test_untrusted_marker_never_promotes_or_dispatches() -> None:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	result = _verifying_run(_project_state(), compare_commits=commits, untrusted_marker=True)
	assert "comprehensive_promotion" not in result["latest_state"]
	assert result["release_dispatches"] == []
	assert result["latest_state"]["status"] == "complete"
	callback = result["latest_state"]["comprehensive_release_callback"]
	assert callback["role"] == "untrusted"
	assert callback.get("handled") is not True
	assert callback["untrusted_marker_alerted"] is True
	# The cycle stays human-gated: the label is kept so no new cycle starts.
	assert LABEL in result["tracking_labels"]
	assert "COMPREHENSIVE_MARKER_UNTRUSTED" in result["stdout"]
	assert "alerted=false" in result["stdout"]
	# A later tick neither re-alerts nor consumes the label.
	second = _verifying_run(result["latest_state"], compare_commits=commits, untrusted_marker=True)
	assert "alerted=true" in second["stdout"]
	assert LABEL in second["tracking_labels"]
	assert second["release_dispatches"] == []
	assert second["latest_state"]["comprehensive_release_callback"].get("handled") is not True


def test_release_workflow_override_cannot_pin_and_fails_closed() -> None:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	result = _verifying_run(
		_project_state(),
		compare_commits=commits,
		env_overrides={"COMPREHENSIVE_RELEASE_WORKFLOW_FILE": "test-and-mark-stable.yml", "COMPREHENSIVE_RELEASE_WORKFLOW_REF": "stable"},
	)
	promotion = result["latest_state"]["comprehensive_promotion"]
	assert promotion["status"] == "failed"
	assert "cannot" in promotion["reason"] or "requires" in promotion["reason"]
	assert result["release_dispatches"] == []
	assert result["latest_state"]["final_merge_status"] == "merged"


def test_transient_verifying_dispatch_outcome_is_retried_then_bounded() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		stub = Path(tmp) / "dispatcher_stub.sh"
		stub.write_text("#!/usr/bin/env bash\necho 'APPLY_ANALYSIS_SKIPPED reason=orchestrate_run_in_flight workflow=internal-orchestrate.yml'\n", encoding="utf-8")
		stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
		common = dict(
			enable_validation="false",
			max_validate_cycles="3",
			tracking_labels=[LABEL],
			tracking_comments=[_trusted(_marker("proving", cycle_baseline_sha=BASELINE_SHA, smoke_sha=SMOKE_SHA))],
			issue_labels={10: ["ai:merged"]},
			prs=[_open_final_pr()],
			existing_branches=["main", "orchestrator/project-192"],
			branch_ref_shas={"main": PROMOTE_SHA},
		)
		first = _run_poller(state=_project_state(), env_overrides={"APPLY_ANALYSIS_DISPATCHER": str(stub), "COMPREHENSIVE_VERIFICATION_RETRY_MAX": "1"}, **common)
		assert first["latest_state"]["comprehensive_release_callback"].get("handled") is not True
		assert first["latest_state"]["comprehensive_release_callback"]["verification_retries"] == 1
		assert LABEL in first["tracking_labels"]
		assert "retry=1/1" in first["stdout"]
		second = _run_poller(state=first["latest_state"], env_overrides={"APPLY_ANALYSIS_DISPATCHER": str(stub), "COMPREHENSIVE_VERIFICATION_RETRY_MAX": "1"}, **dict(common, tracking_labels=first["tracking_labels"], prs=first["prs"]))
		callback = second["latest_state"]["comprehensive_release_callback"]
		assert callback["handled"] is True
		assert callback["verification"].startswith("SKIPPED reason=orchestrate_run_in_flight")
		assert "retries_exhausted=1" in callback["verification"]
		assert LABEL not in second["tracking_labels"]

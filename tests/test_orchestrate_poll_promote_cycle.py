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
FINAL_PR_HEAD_SHA = "9" * 40
# The stable BRANCH tip (heads/stable), compared with promote_sha before the
# promotion is dispatched (#7888).
STABLE_BRANCH_SHA = "3" * 40

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
		# The final merge is bound to a resolved 40-hex head (#6991,
		# poller-merge-unbound-to-checked-head); the harness default
		# `mocksha<N>` is refused before the promotion gate runs.
		"headSha": FINAL_PR_HEAD_SHA,
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


def _verifying_run(state: dict, *, compare_commits: list[dict], commit_files: dict | None = None, runs_by_file: dict | None = None, prs: list[dict] | None = None, tag_commit: str = "9" * 40, untrusted_marker: bool = False, env_overrides: dict | None = None, compare_status_by_range: dict | None = None, extra_store: dict | None = None, stable_branch_sha: str = STABLE_BRANCH_SHA, stable_status: str = "ahead"):
	extra = {"compare_commits_detail": compare_commits, "tag_refs": {"stable": {"type": "tag", "sha": "8" * 40}}, "tag_objects": {"8" * 40: tag_commit}}
	if commit_files:
		extra["commit_files"] = commit_files
	ranges = {f"{stable_branch_sha}...{PROMOTE_SHA}": stable_status}
	ranges.update(compare_status_by_range or {})
	extra["compare_status_by_range"] = ranges
	if extra_store:
		extra.update(extra_store)
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
		existing_branches=["main", "orchestrator/project-192", "stable"],
		branch_ref_shas={"stable": stable_branch_sha},
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


def test_bot_commit_touching_shipped_claude_command_counts_as_untested() -> None:
	# workflow-templates/.claude/ is what consumers receive on the @stable sync,
	# so its markdown commands are code, like the root .claude/ tree.
	commits = [
		_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1"),
		_commit(BOT_SHA, "chore: tweak", "github-actions[bot]", "github-actions[bot]"),
	]
	result = _verifying_run(_project_state(), compare_commits=commits, commit_files={BOT_SHA: ["analysis/report.md", "workflow-templates/.claude/commands/investigate-issue.md"]})
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


def test_diverged_promotion_candidate_is_deferred_even_with_no_listed_commits() -> None:
	# A reset or rewritten main lists no forward commits, which must not read
	# as "nothing untested": the candidate does not descend from smoke_sha.
	result = _verifying_run(_project_state(), compare_commits=[], compare_status_by_range={f"{SMOKE_SHA}...{PROMOTE_SHA}": "diverged"})
	promotion = result["latest_state"]["comprehensive_promotion"]
	assert promotion["status"] == "deferred"
	assert promotion["untested_count"] == 1
	assert "compare status diverged" in (result["stdout"] + result["stderr"]) or "COMPREHENSIVE_PROMOTION_DEFERRED" in result["stdout"]
	assert result["release_dispatches"] == []
	assert result["latest_state"]["final_merge_status"] == "merged"


def test_bot_commit_with_a_truncated_file_list_is_untested() -> None:
	# The commits endpoint caps `files` at 300 per page; a page that large
	# cannot prove the commit touched only non-code paths.
	commits = [
		_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1"),
		_commit(BOT_SHA, "chore: sync ai-memory", "github-actions[bot]"),
	]
	result = _verifying_run(_project_state(), compare_commits=commits, commit_files={BOT_SHA: [f"ai-memory/{i}.md" for i in range(300)]})
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "deferred"
	assert result["release_dispatches"] == []


def test_stable_tag_lookup_failure_holds_instead_of_recording_no_tag() -> None:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	result = _verifying_run(_project_state(), compare_commits=commits, extra_store={"tag_ref_lookup_error": True})
	assert "COMPREHENSIVE_PROMOTION_HOLD" in result["stdout"]
	assert "reason=stable_tag_lookup_unavailable" in result["stdout"]
	assert result["release_dispatches"] == []
	assert result["latest_state"]["final_merge_status"] != "merged"
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "pending"
	assert isinstance(result["latest_state"]["comprehensive_promotion"]["gate_started_at"], int)


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


# --- docs-only commits by any author (#7662) ---------------------------------

PIPELINE_CONTEXT_FILES = [
	"unattended_system_instructions.md",
	"README.md",
	"agents.md",
	"AGENTS.md",
	"ai_pipeline.md",
	"codex.md",
	"probably_unnecessary_but_read_if_stuck.md",
	"agents.d/123-x.md",
]


def _status_with(commit: dict, files: list[str] | None, env_overrides: dict | None = None) -> dict:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1"), commit]
	commit_files = {commit["sha"]: files} if files is not None else None
	return _verifying_run(_project_state(), compare_commits=commits, commit_files=commit_files, env_overrides=env_overrides)


def test_human_docs_only_commit_does_not_defer() -> None:
	result = _status_with(
		_commit(HUMAN_SHA, "docs: plan for synced-file security findings heal intake (#7603)", "shubhodeep1"),
		["docs/plans/synced-file-security-findings-heal-intake-plan.md"],
	)
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "dispatched"
	assert len(result["release_dispatches"]) == 1


def test_human_commit_mixing_docs_and_code_defers() -> None:
	result = _status_with(_commit(HUMAN_SHA, "feat: x (#1)", "shubhodeep1"), ["docs/x.md", "scripts/x.sh"])
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "deferred"
	assert result["release_dispatches"] == []


def test_pipeline_instruction_files_count_as_code() -> None:
	for path in PIPELINE_CONTEXT_FILES:
		result = _status_with(_commit(HUMAN_SHA, "docs: tweak (#1)", "shubhodeep1"), ["docs/x.md", path])
		assert result["latest_state"]["comprehensive_promotion"]["status"] == "deferred", path
		assert result["release_dispatches"] == [], path
	result = _status_with(_commit(BOT_SHA, "chore: fold agents", "github-actions[bot]", "github-actions[bot]"), ["agents.md"])
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "deferred"


def test_bot_changelog_only_commit_still_exempt() -> None:
	result = _status_with(_commit(BOT_SHA, "chore: fold changelog", "github-actions[bot]", "github-actions[bot]"), ["CHANGELOG.md", "changelog.d/7662-x.md"])
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "dispatched"


def test_human_commit_with_no_listed_files_is_untested() -> None:
	result = _status_with(_commit(HUMAN_SHA, "docs: empty (#1)", "shubhodeep1"), None)
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "deferred"


def test_merge_commit_with_docs_only_files_is_untested() -> None:
	merge = _commit(HUMAN_SHA, "Merge branch 'feature'", "shubhodeep1")
	merge["parents"] = [{"sha": "p1"}, {"sha": "p2"}]
	result = _status_with(merge, ["docs/x.md"])
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "deferred"
	assert result["release_dispatches"] == []


def test_bot_logins_var_no_longer_gates_docs_only() -> None:
	result = _status_with(
		_commit(BOT_SHA, "chore: update notes", "github-actions[bot]", "github-actions[bot]"),
		["docs/notes.md"],
		env_overrides={"COMPREHENSIVE_CYCLE_BOT_LOGINS": ""},
	)
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "dispatched"


# --- pinned promote cycle (phase 3) -------------------------------------------

PIN_TAG = "promote-cycle-frozen"
PIN_TAG_OBJECT = "6" * 40
MOVED_TIP = "4" * 40


def _pinned_verifying_run(
	state: dict,
	*,
	compare_commits: list[dict] | None = None,
	branch_tip: str = PROMOTE_SHA,
	pin_tag_commit: str | None = PROMOTE_SHA,
	runs_by_file: dict | None = None,
	prs: list[dict] | None = None,
	tag_commit: str = "9" * 40,
	env_overrides: dict | None = None,
	marker_shas: dict | None = None,
	stable_branch_sha: str = STABLE_BRANCH_SHA,
	stable_status: str = "ahead",
	extra_store: dict | None = None,
):
	tag_refs = {"stable": {"type": "tag", "sha": "8" * 40}}
	tag_objects = {"8" * 40: tag_commit}
	if pin_tag_commit is not None:
		tag_refs[PIN_TAG] = {"type": "tag", "sha": PIN_TAG_OBJECT}
		tag_objects[PIN_TAG_OBJECT] = pin_tag_commit
	extra = {"compare_commits_detail": compare_commits or [], "tag_refs": tag_refs, "tag_objects": tag_objects}
	extra["compare_status_by_range"] = {f"{stable_branch_sha}...{PROMOTE_SHA}": stable_status}
	if runs_by_file:
		extra["workflow_runs_by_file"] = runs_by_file
	if extra_store:
		extra.update(extra_store)
	shas = marker_shas if marker_shas is not None else {
		"cycle_baseline_sha": PROMOTE_SHA,
		"smoke_sha": PROMOTE_SHA,
		"promote_sha": PROMOTE_SHA,
		"proving_merge_sha": PROVING_MERGE_SHA,
	}
	return _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=[LABEL],
		tracking_comments=[_trusted(_marker("verifying", pinned="true", **shas))],
		issue_labels={10: ["ai:merged"]},
		prs=prs or [_open_final_pr()],
		existing_branches=["main", "orchestrator/project-192", "promote-candidate", "stable"],
		branch_ref_shas={"promote-candidate": branch_tip, "stable": stable_branch_sha},
		mock_store_extra=extra,
		env_overrides=env_overrides,
	)


def test_pinned_gate_dispatches_despite_unproven_commits_on_main() -> None:
	# A human code commit landed on main between the freeze and the promotion;
	# the pinned candidate is still the commit the smoke gate tested.
	commits = [_commit(HUMAN_SHA, "feat: landed mid-cycle (#9)", "shubhodeep1")]
	result = _pinned_verifying_run(_project_state(), compare_commits=commits)
	promotion = result["latest_state"]["comprehensive_promotion"]
	assert promotion["status"] == "dispatched", promotion
	assert len(result["release_dispatches"]) == 1
	assert result["release_dispatches"][0]["target_sha"] == PROMOTE_SHA
	assert "COMPREHENSIVE_PROMOTION_DEFERRED" not in result["stdout"]
	assert result.get("deleted_tags", []) == []
	assert not any(f"compare/{PROMOTE_SHA}..." in str(call) for call in result["api_calls"])


def test_pinned_gate_releases_when_branch_moved() -> None:
	result = _pinned_verifying_run(_project_state(), branch_tip=MOVED_TIP)
	promotion = result["latest_state"]["comprehensive_promotion"]
	assert promotion["status"] == "deferred"
	assert promotion["reason"] == "pin_released"
	assert promotion["branch_tip"] == MOVED_TIP
	assert result["release_dispatches"] == []
	assert "COMPREHENSIVE_PROMOTION_PIN_RELEASED tracking_issue=" in result["stdout"]
	assert f"branch_tip={MOVED_TIP}" in result["stdout"]
	assert result.get("deleted_tags") == [PIN_TAG]
	assert result["latest_state"]["final_merge_status"] == "merged"


def test_pinned_gate_releases_when_tag_is_missing() -> None:
	result = _pinned_verifying_run(_project_state(), pin_tag_commit=None)
	assert result["latest_state"]["comprehensive_promotion"]["reason"] == "pin_released"
	assert result["release_dispatches"] == []


def test_pin_switch_off_abandons_the_promotion() -> None:
	result = _pinned_verifying_run(_project_state(), env_overrides={"PROMOTE_CYCLE_PIN_ENABLED": "false"})
	promotion = result["latest_state"]["comprehensive_promotion"]
	assert promotion["status"] == "deferred"
	assert promotion["detail"] == "pin_disabled"
	assert result["release_dispatches"] == []
	assert result.get("deleted_tags") == [PIN_TAG]


def test_pinned_tag_is_released_when_promoted() -> None:
	first = _pinned_verifying_run(_project_state())
	assert first["latest_state"]["comprehensive_promotion"]["status"] == "dispatched"
	second = _pinned_verifying_run(first["latest_state"], prs=first["prs"], tag_commit="7" * 40)
	assert second["latest_state"]["comprehensive_promotion"]["status"] == "promoted"
	assert second.get("deleted_tags") == [PIN_TAG]
	assert "tag=deleted" in second["stdout"]


def test_pinned_tag_is_released_when_release_fails() -> None:
	first = _pinned_verifying_run(_project_state())
	runs = {"promote-main-to-stable.yml": [{"id": 1, "event": "workflow_dispatch", "status": "completed", "conclusion": "failure", "head_branch": "main", "created_at": "2099-01-01T00:00:00Z"}]}
	second = _pinned_verifying_run(first["latest_state"], prs=first["prs"], runs_by_file=runs)
	assert second["latest_state"]["comprehensive_promotion"]["status"] == "failed"
	assert second.get("deleted_tags") == [PIN_TAG]


def test_pinned_tag_is_released_on_hold_timeout() -> None:
	first = _pinned_verifying_run(_project_state())
	state = first["latest_state"]
	state["comprehensive_promotion"]["dispatched_at"] = 1
	second = _pinned_verifying_run(state, prs=first["prs"])
	assert second["latest_state"]["comprehensive_promotion"]["reason"] == "hold cap reached before the release finished"
	assert second.get("deleted_tags") == [PIN_TAG]


def test_pinned_tag_is_released_when_marker_lacks_shas() -> None:
	result = _pinned_verifying_run(_project_state(), marker_shas={"proving_merge_sha": PROVING_MERGE_SHA})
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "skipped"
	assert result.get("deleted_tags") == [PIN_TAG]


def test_pinned_tag_is_released_when_release_workflow_cannot_pin() -> None:
	result = _pinned_verifying_run(_project_state(), env_overrides={"COMPREHENSIVE_RELEASE_WORKFLOW_FILE": "test-and-mark-stable.yml"})
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "failed"
	assert result.get("deleted_tags") == [PIN_TAG]


def test_unpinned_marker_never_touches_the_freeze_tag() -> None:
	commits = [
		_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1"),
		_commit(HUMAN_SHA, "feat: x (#1)", "shubhodeep1"),
	]
	result = _verifying_run(_project_state(), compare_commits=commits)
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "deferred"
	assert "COMPREHENSIVE_PROMOTION_DEFERRED" in result["stdout"]
	assert "COMPREHENSIVE_PROMOTION_PIN_RELEASED" not in result["stdout"]
	assert result.get("deleted_tags", []) == []


def test_pinned_proving_completion_dispatches_verifying_run_on_the_candidate() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		stub, env_out = _stub_dispatcher(Path(tmp))
		result = _run_poller(
			state=_project_state(),
			enable_validation="false",
			max_validate_cycles="3",
			tracking_labels=[LABEL],
			tracking_comments=[_trusted(_marker("proving", cycle_baseline_sha=SMOKE_SHA, smoke_sha=SMOKE_SHA, pinned="true"))],
			issue_labels={10: ["ai:merged"]},
			prs=[dict(_open_final_pr(), merge_commit_sha=PROVING_MERGE_SHA)],
			existing_branches=["main", "orchestrator/project-192"],
			branch_ref_shas={"main": PROMOTE_SHA},
			env_overrides={"APPLY_ANALYSIS_DISPATCHER": str(stub)},
		)
		env_lines = env_out.read_text(encoding="utf-8").splitlines()
	assert result["latest_state"]["comprehensive_release_callback"]["role"] == "proving"
	assert f"APPLY_ANALYSIS_PROMOTE_SHA={SMOKE_SHA}" in env_lines
	assert "APPLY_ANALYSIS_PINNED=true" in env_lines
	assert f"GITHUB_SHA={PROMOTE_SHA}" in env_lines


def test_pinned_proving_completion_releases_tag_when_verifying_not_dispatched() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		stub, _ = _stub_dispatcher(Path(tmp), outcome="skipped")
		result = _run_poller(
			state=_project_state(),
			enable_validation="false",
			max_validate_cycles="3",
			tracking_labels=[LABEL],
			tracking_comments=[_trusted(_marker("proving", cycle_baseline_sha=SMOKE_SHA, smoke_sha=SMOKE_SHA, pinned="true"))],
			issue_labels={10: ["ai:merged"]},
			prs=[_open_final_pr()],
			existing_branches=["main", "orchestrator/project-192"],
			branch_ref_shas={"main": PROMOTE_SHA},
			mock_store_extra={"tag_refs": {PIN_TAG: {"type": "tag", "sha": PIN_TAG_OBJECT}}},
			env_overrides={"APPLY_ANALYSIS_DISPATCHER": str(stub)},
		)
	assert "COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED" in result["stdout"]
	assert result.get("deleted_tags") == [PIN_TAG]
	assert "tag=deleted" in result["stdout"]


def test_unpinned_proving_not_dispatched_leaves_the_freeze_tag_alone() -> None:
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
	assert "COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED" in result["stdout"]
	assert result.get("deleted_tags", []) == []


# --- stable branch must be an ancestor of promote_sha (#7888) -----------------

def _stable_compare_calls(result: dict) -> list[str]:
	return [str(call) for call in result["api_calls"] if f"compare/{STABLE_BRANCH_SHA}..." in str(call) or f"compare/{PROMOTE_SHA}...{PROMOTE_SHA}" in str(call)]


def test_stable_branch_ahead_dispatches_the_promotion() -> None:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	result = _verifying_run(_project_state(), compare_commits=commits, stable_status="ahead")
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "dispatched"
	assert len(result["release_dispatches"]) == 1
	assert any(str(call).endswith("git/ref/heads/stable") for call in result["api_calls"])
	assert len(_stable_compare_calls(result)) == 1


def test_stable_branch_identical_dispatches_without_a_compare() -> None:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	result = _verifying_run(_project_state(), compare_commits=commits, stable_branch_sha=PROMOTE_SHA)
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "dispatched"
	assert len(result["release_dispatches"]) == 1
	assert not any(f"compare/{PROMOTE_SHA}...{PROMOTE_SHA}" in str(call) for call in result["api_calls"])


def test_stable_branch_behind_or_diverged_abandons_without_dispatch() -> None:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	for status in ("behind", "diverged"):
		result = _verifying_run(_project_state(), compare_commits=commits, stable_status=status)
		promotion = result["latest_state"]["comprehensive_promotion"]
		assert promotion["status"] == "deferred", (status, promotion)
		assert promotion["reason"] == "stable_not_ancestor"
		assert promotion["stable_sha"] == STABLE_BRANCH_SHA
		assert promotion["compare_status"] == status
		assert result["release_dispatches"] == []
		assert "COMPREHENSIVE_PROMOTION_DEFERRED tracking_issue=" in result["stdout"]
		assert f"reason=stable_not_ancestor stable_sha={STABLE_BRANCH_SHA} promote_sha={PROMOTE_SHA} status={status}" in result["stdout"]
		assert result["latest_state"]["final_merge_status"] == "merged"
		assert result.get("deleted_tags", []) == []


def test_stable_branch_read_failure_holds_the_merge() -> None:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	result = _verifying_run(_project_state(), compare_commits=commits, extra_store={"fail_branch_ref_after": {"stable": 0}})
	assert "COMPREHENSIVE_PROMOTION_HOLD" in result["stdout"]
	assert "reason=stable_branch_compare_unavailable" in result["stdout"]
	assert result["release_dispatches"] == []
	assert result["latest_state"]["final_merge_status"] != "merged"
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "pending"


def test_pinned_stable_branch_diverged_abandons_and_releases_the_freeze() -> None:
	result = _pinned_verifying_run(_project_state(), stable_status="diverged")
	promotion = result["latest_state"]["comprehensive_promotion"]
	assert promotion["status"] == "deferred"
	assert promotion["reason"] == "stable_not_ancestor"
	assert result["release_dispatches"] == []
	assert result.get("deleted_tags") == [PIN_TAG]


def test_pinned_stable_branch_ahead_dispatches() -> None:
	result = _pinned_verifying_run(_project_state(), stable_status="ahead")
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "dispatched"
	assert len(result["release_dispatches"]) == 1
	assert result.get("deleted_tags", []) == []


def test_stable_branch_unrecognised_compare_status_holds_the_merge() -> None:
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	result = _verifying_run(_project_state(), compare_commits=commits, stable_status="unexpected")
	assert "reason=stable_branch_compare_unavailable" in result["stdout"]
	assert result["release_dispatches"] == []
	assert result["latest_state"]["final_merge_status"] != "merged"
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "pending"


def test_stable_branch_at_smoke_sha_reuses_the_smoke_compare() -> None:
	# smoke_sha...promote_sha already returned ahead, so a stable branch at
	# smoke_sha is a proven ancestor and needs no second compare (#7888).
	commits = [_commit(PROVING_MERGE_SHA, "Apply analysis recommendations (#400)", "shubhodeep1")]
	result = _verifying_run(_project_state(), compare_commits=commits, stable_branch_sha=SMOKE_SHA)
	assert result["latest_state"]["comprehensive_promotion"]["status"] == "dispatched"
	assert len(result["release_dispatches"]) == 1
	smoke_compares = [str(call) for call in result["api_calls"] if f"compare/{SMOKE_SHA}...{PROMOTE_SHA}" in str(call)]
	assert len(smoke_compares) == 1, smoke_compares

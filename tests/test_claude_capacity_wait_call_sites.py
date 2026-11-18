"""Claude capacity wait, Phases 2-4 (issue #7370).

Phase 2: every Claude call site treats exit 80 (capacity wait) as a stop that
never runs codex. Phase 3: review/autofix pauses as `claude_capacity`, which
the identical-failure cap and heal reports skip. Phase 4: the hourly
`provider_outage.py capacity-resume` pass re-dispatches waiting work and the
default is on (off for the `core` install profile).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str):
	spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	return module


po = _load("provider_outage")


def _text(path: Path) -> str:
	return path.read_text(encoding="utf-8")


# --- Phase 2: call sites ------------------------------------------------------


@pytest.mark.parametrize(
	("path", "needle"),
	[
		("scripts/run_plan_codex.sh", 'if [ "${plan_rc}" -eq 80 ]; then'),
		("scripts/heal_isolated_implement.sh", "outcome=capacity_wait action=stop"),
		("scripts/implement_diagnose_post_codex_failure.sh", 'if [ "${diagnose_rc}" -eq 80 ]; then'),
		("scripts/check_failure_triage.sh", 'log "skip reason=claude_capacity_wait"'),
		("scripts/unblock_judge.sh", "outcome=skip reason=claude_capacity_wait"),
		("scripts/workflow_failure_heal_intake.sh", 'log "skip reason=claude_capacity_wait"'),
		("scripts/activation_verify.sh", "outcome=skip reason=claude_capacity_wait"),
		("scripts/validate_process.sh", "exit 80"),
		(".github/workflows/clarify.yml", 'if [ "${rc}" -eq 80 ]; then'),
		(".github/workflows/orchestrate.yml", 'if [ "${decomposer_rc}" -eq 80 ]; then'),
		(".github/workflows/orchestrate_clarify_respond.yml", 'if [ "${critic_rc}" -eq 80 ]; then'),
		(".github/workflows/implement.yml", 'if [ "${repair_direct_rc}" -eq 80 ]; then'),
		(".github/workflows/validate.yml", 'if [ "${validate_process_rc}" -eq 80 ]; then'),
	],
)
def test_call_sites_handle_the_capacity_wait(path: str, needle: str) -> None:
	assert needle in _text(REPO_ROOT / path)


def test_capacity_wait_branch_runs_before_the_codex_fallback() -> None:
	plan = _text(SCRIPTS / "run_plan_codex.sh")
	assert plan.index('if [ "${plan_rc}" -eq 80 ]; then') < plan.index('if [ "${HEAL_ROUTE:-false}" != true ] && [ "${PLAN_ENGINE}" != "claude" ]; then')
	clarify = _text(WORKFLOWS / "clarify.yml")
	assert clarify.index('if [ "${rc}" -eq 80 ]; then') < clarify.index('CLARIFY_ENGINE="codex"')


def test_security_audit_waits_without_codex() -> None:
	text = _text(SCRIPTS / "security_audit.sh")
	assert "80) security_audit_claude_fallback claude_run; return 3 ;;" in text
	wait = text.index('if [ "${SECURITY_AUDIT_CLAUDE_RC}" -eq 3 ]; then')
	assert wait < text.index('security_audit_require_file "codex-preflight"')
	assert "exit 80" in text[wait:wait + 400]


def test_poller_judges_never_count_a_capacity_wait() -> None:
	text = _text(SCRIPTS / "orchestrate_poll_process.sh")
	assert "printf '%s\\n' claude_capacity_wait > \"${judge_reason_file}\"" in text
	record = text[text.index("_record_judge_isolation_failure()"):]
	assert record.index('if [ "${reason}" = claude_capacity_wait ]; then') < record.index("count=$((count + 1))")
	assert 'RB_ISOLATION_CAPACITY_WAIT}" = true ]; then' in text
	assert 'if [ "${security_pass_audit_rc}" -eq 80 ]; then' in text


@pytest.mark.parametrize(
	("workflow", "stage"),
	[("clarify.yml", "clarify"), ("plan.yml", "plan"), ("implement.yml", "implement"), ("orchestrate_clarify_respond.yml", "clarify_respond"), ("validate.yml", "validate")],
)
def test_waiting_jobs_name_their_item(workflow: str, stage: str) -> None:
	text = _text(WORKFLOWS / workflow)
	assert f"CLAUDE_CAPACITY_WAIT_STAGE: {stage}" in text
	assert "CLAUDE_CAPACITY_WAIT_ITEM_NUMBER:" in text
	assert "ai_engine_fallback_report.sh" in text


def test_poller_records_waits_as_kind_none() -> None:
	assert "CLAUDE_CAPACITY_WAIT_ITEM_KIND: none" in _text(WORKFLOWS / "orchestrate_poll.yml")
	report = _text(SCRIPTS / "ai_engine_fallback_report.sh")
	assert "issue|pr|run|none) ;;" in report


def test_clarify_capacity_replay_gate_requires_the_pat_login() -> None:
	text = _text(WORKFLOWS / "clarify.yml")
	block = text[text.index("# BEGIN capacity-wait replay classification (#7370)"):text.index("# END capacity-wait replay classification (#7370)")]
	assert '"/reclarify [auto-requeued-by-capacity-wait]"' in block
	assert '"${_rel_login,,}" = "${COMMENT_USER_LOGIN,,}"' in block
	assert 'IS_AUTOMATED_RELEASE="true"' in block
	assert "gate=capacity_wait_replay" in block


# --- Phase 3: review/autofix --------------------------------------------------


def test_pause_reasons_match_the_heal_module() -> None:
	heal = _load("workflow_failure_heal")
	assert heal.PAUSE_FAILURE_REASONS == po.PAUSE_FAILURE_REASONS == frozenset({"provider_unavailable", "claude_capacity"})
	assert heal.CLAUDE_CAPACITY_REASON == po.CLAUDE_CAPACITY_REASON == "claude_capacity"


def test_review_defers_before_the_fallback_and_records_the_wait() -> None:
	text = _text(WORKFLOWS / "review_autofix.yml")
	defer = text.index("      - name: Defer review on Claude capacity\n")
	assert text.index("      - name: Record Claude pool capacity\n") < defer < text.index("      - name: Install OS-level dependencies\n")
	assert "steps.claude_pool.outputs.reason == 'all_gated'" in text[defer:defer + 600]
	record = text.index("      - name: Record Claude capacity wait\n")
	assert "always() && env.AUTOFIX_FAILURE_REASON == 'claude_capacity'" in text[record:record + 600]
	assert text.count("!contains(fromJSON('[\"provider_unavailable\",\"claude_capacity\"]'), env.AUTOFIX_FAILURE_REASON)") == 4
	assert "AI review paused — Claude pool at capacity" in _text(SCRIPTS / "review_autofix_step_failure_comment.sh")
	assert "review_autofix_step_claude_capacity_wait.sh" in _text(SCRIPTS / "stage_workflow_support.sh")


def test_review_sweep_skips_waiting_prs() -> None:
	text = _text(WORKFLOWS / "review_autofix_sweep.yml")
	assert 'index("ai:claude-capacity-wait") != null' in text
	assert "reason=claude_capacity_wait" in text


def test_capacity_failures_never_count_toward_the_cap() -> None:
	heal = _load("workflow_failure_heal")
	assert "claude_capacity" in heal.PAUSE_FAILURE_REASONS
	report = _text(SCRIPTS / "workflow_failure_heal_autofix_report.sh")
	assert 'if [ "${FAILURE_REASON}" = "claude_capacity" ]; then' in report


# --- Phase 4: resume pass -----------------------------------------------------

LOGIN = "pipeline-bot"
TRACKER_BODY = (
	"<!-- ai:provider-outage:v1 kind=capacity provider=claude-pool status=all_gated opened_at=2026-10-14T01:00:00Z -->\n"
	"text\n\n<!-- ai:provider-outage-resume:v1 prs= labels= release_runs= -->\n"
)


def _c(body: str, login: str = LOGIN, association: str = "OWNER", **extra: Any) -> dict[str, Any]:
	return {"user": {"login": login}, "author_association": association, "body": body, **extra}


def _item(kind: str, item: str, run: str) -> str:
	return f"x\n\n<!-- ai:claude-capacity-wait-item:v1 kind={kind} item={item} workflow=clarify.yml run={run} role=CLARIFY -->"


class FakeGitHub:
	repository = "o/r"

	def __init__(self) -> None:
		self.tracker_body = TRACKER_BODY
		self.tracker_comments: list[dict[str, Any]] = []
		self.issue_comments: dict[int, list[dict[str, Any]]] = {}
		self.comment_by_id: dict[int, dict[str, Any]] = {}
		self.labelled: list[dict[str, Any]] = []
		self.prs: dict[int, dict[str, Any]] = {}
		self.posted: list[tuple[int, str]] = []
		self.removed: list[int] = []
		self.added: list[int] = []
		self.dispatched: list[int] = []
		self.reruns: list[int] = []
		self.rerun_status: dict[int, int] = {}
		self.closed: list[int] = []

	def login(self) -> str:
		return LOGIN

	def list_issues(self, label: str) -> list[dict[str, Any]]:
		if label == po.TRACKER_LABEL:
			if 1 in self.closed:
				return []
			return [{"number": 1, "user": {"login": LOGIN}, "body": self.tracker_body}]
		return self.labelled

	def comments(self, number: int) -> list[dict[str, Any]] | None:
		return self.tracker_comments if number == 1 else self.issue_comments.get(number, [])

	def get_comment(self, comment_id: int) -> dict[str, Any] | None:
		return self.comment_by_id.get(comment_id)

	def get_pr(self, number: int) -> dict[str, Any] | None:
		return self.prs.get(number)

	def comment(self, number: int, body: str) -> bool:
		self.posted.append((number, body))
		return True

	def remove_label(self, number: int, label: str) -> bool:
		self.removed.append(number)
		return True

	def add_label(self, number: int, label: str) -> bool:
		self.added.append(number)
		return True

	def dispatch(self, workflow: str, ref: str, number: int) -> bool:
		self.dispatched.append(number)
		return True

	def rerun_run(self, run_id: int) -> tuple[bool, int | None]:
		self.reruns.append(run_id)
		status = self.rerun_status.get(run_id)
		return (status is None, status)

	def patch_body(self, number: int, body: str) -> bool:
		self.tracker_body = body
		return True

	def close_issue(self, number: int) -> bool:
		self.closed.append(number)
		return True

	def ensure_label(self) -> None:
		pass


def _resume(gh: FakeGitHub, tmp_path: Path, available: bool = True) -> dict[str, Any]:
	return po.capacity_resume(
		gh, pool_available=available, items_out=str(tmp_path / "items.json"), alert_out=str(tmp_path / "alert.txt"),
		max_dispatch=50, workflow="internal-review.yml",
	)


def test_nothing_is_resumed_while_the_pool_is_gated(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("run", "none", "11"))]
	values = _resume(gh, tmp_path, available=False)
	assert values["capacity_wait"] == "true" and values["pending"] == "1"
	assert gh.reruns == [] and gh.closed == []
	items = json.loads((tmp_path / "items.json").read_text())
	assert items["count"] == 1 and items["items"][0]["run"] == "11"


def test_untrusted_tracker_markers_are_ignored(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("run", "none", "11"), login="mallory")]
	_resume(gh, tmp_path)
	assert gh.reruns == [] and gh.closed == [1]


def test_runs_are_rerun_and_old_runs_are_reported(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("run", "none", "11")), _c(_item("run", "none", "12"))]
	gh.rerun_status = {12: 403}
	_resume(gh, tmp_path)
	assert gh.reruns == [11, 12]
	assert gh.closed == [1]
	alert = (tmp_path / "alert.txt").read_text()
	assert "resumed 1 item(s)" in alert and "run 12" in alert
	assert "done=run:none:11,run:none:12" in gh.tracker_body


def test_a_failed_item_stays_pending_and_the_tracker_stays_open(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("run", "none", "11")), _c(_item("run", "none", "12"))]
	gh.rerun_status = {11: 500}
	values = _resume(gh, tmp_path)
	assert gh.closed == [] and values["capacity_wait"] == "true"
	assert "run:none:12" in gh.tracker_body and "run:none:11" not in gh.tracker_body
	gh.rerun_status = {}
	gh.reruns = []
	_resume(gh, tmp_path)
	assert gh.reruns == [11] and gh.closed == [1]


def test_clarify_issue_with_source_none_is_replayed(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("issue", "5", "21"))]
	gh.labelled = [{"number": 5}]
	gh.issue_comments[5] = [_c("paused\n<!-- ai:claude-capacity-wait:v1 stage=clarify source=none run=21 role=CLARIFY -->")]
	_resume(gh, tmp_path)
	assert gh.removed == [5]
	[(number, body)] = [entry for entry in gh.posted if entry[0] == 5]
	assert body.startswith("/reclarify [auto-requeued-by-capacity-wait]\n")
	assert body.endswith("<!-- ai:claude-capacity-wait-replay:v1 source=none run=21 -->")


def test_implement_replays_the_approved_command(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("issue", "5", "22"))]
	gh.labelled = [{"number": 5}]
	gh.issue_comments[5] = [_c("w\n<!-- ai:claude-capacity-wait:v1 stage=implement source=900 run=22 role=IMPLEMENT -->")]
	gh.comment_by_id[900] = _c("/approved\n\nlooks good", login="human", issue_url="https://api.github.com/repos/o/r/issues/5")
	_resume(gh, tmp_path)
	[(number, body)] = [entry for entry in gh.posted if entry[0] == 5]
	assert body.startswith("/approved [auto-requeued-by-capacity-wait]\n\nlooks good")
	assert "source=900 run=22" in body


def test_a_newer_trusted_command_supersedes_the_replay(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("issue", "5", "23"))]
	gh.labelled = [{"number": 5}]
	gh.issue_comments[5] = [
		_c("w\n<!-- ai:claude-capacity-wait:v1 stage=plan source=901 run=23 role=PLAN -->"),
		_c("/answer Q1: B", login="human", association="MEMBER"),
	]
	_resume(gh, tmp_path)
	assert gh.removed == [5]
	assert [entry for entry in gh.posted if entry[0] == 5] == []


def test_an_untrusted_source_is_not_replayed(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("issue", "5", "24"))]
	gh.labelled = [{"number": 5}]
	gh.issue_comments[5] = [_c("w\n<!-- ai:claude-capacity-wait:v1 stage=plan source=902 run=24 role=PLAN -->")]
	gh.comment_by_id[902] = _c("/answer Q1: A", login="outsider", association="NONE", issue_url="https://api.github.com/repos/o/r/issues/5")
	_resume(gh, tmp_path)
	assert [entry for entry in gh.posted if entry[0] == 5] == []
	assert gh.removed == [5]


def test_a_waiting_pr_is_redispatched(tmp_path: Path) -> None:
	head = "a" * 40
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("pr", "8", "31"))]
	gh.prs[8] = {"state": "open", "labels": [{"name": po.CAPACITY_WAIT_LABEL}], "head": {"sha": head}, "base": {"repo": {"default_branch": "main"}}}
	gh.issue_comments[8] = [_c(f"paused\n<!-- review-autofix-failure:v1 head={head} reason=claude_capacity fp={'b' * 64} run=31 -->")]
	_resume(gh, tmp_path)
	assert gh.dispatched == [8] and gh.removed == [8] and gh.closed == [1]


def test_capacity_tracker_close_waits_for_pending_items() -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("run", "none", "11"))]
	assert po.close_tracker(gh, kind="capacity", alert_out=None) == "pending_waits"
	gh.tracker_body = po.render_wait_done(gh.tracker_body, {"run:none:11"})
	assert po.close_tracker(gh, kind="capacity", alert_out=None) == "closed"


def test_replay_body_keeps_answers_on_the_first_line() -> None:
	body = po.replay_body("plan", "/answer Q1: A\nQ2: B", "77", "5")
	assert body is not None and body.startswith("/answer Q1: A\nQ2: B\n\n[auto-requeued-by-capacity-wait]\n")
	assert po.replay_body("plan", "not a command", "77", "5") is None
	assert po.replay_body("clarify_respond", "<!-- ai:clarification-questions -->\nQ1", "78", "6").startswith("<!-- ai:clarification-questions -->")


def test_orchestrate_poll_runs_the_hourly_resume() -> None:
	text = _text(WORKFLOWS / "orchestrate_poll.yml")
	find = text.index("        id: find_tracking\n")
	assert text.index('echo "capacity_wait=${capacity_wait}" >> "$GITHUB_OUTPUT"') < text.index("exit 0", find)
	resume = text.index("      - name: Resume Claude capacity waits\n")
	assert text.index("        id: claude_pool\n") < resume < text.index("      - name: Alert on Claude pool accounts at the usage gate\n")
	assert "capacity-resume --repo" in text[resume:resume + 2500]
	assert "(steps.find_tracking.outputs.capacity_wait == 'true' && 'all_gated')" in text


# --- Default flip -------------------------------------------------------------

CLAUDE_WORKFLOWS = [
	"check_failure_triage", "clarify", "implement", "issue_pr_status", "orchestrate", "orchestrate_clarify_respond",
	"orchestrate_poll", "plan", "review_autofix", "security-audit", "unblock_judge", "validate", "validation-refresh",
	"workflow-failure-heal-intake", "workflow-log-analysis",
]


@pytest.mark.parametrize("name", CLAUDE_WORKFLOWS)
def test_claude_workflows_default_the_wait_on_except_for_core(name: str) -> None:
	text = _text(WORKFLOWS / f"{name}.yml")
	assert "CLAUDE_POOL_CAPACITY_WAIT_ENABLED: ${{ vars.CLAUDE_POOL_CAPACITY_WAIT_ENABLED || (vars.WORKFLOW_PROFILE == 'core' && 'false') || 'true' }}" in text


def test_engine_smoke_keeps_the_codex_fallback_path() -> None:
	assert re.search(r"(?m)^  CLAUDE_POOL_CAPACITY_WAIT_ENABLED: 'false'$", _text(WORKFLOWS / "claude-engine-smoke.yml"))


# --- Review fixes (issue #7370 review round) ----------------------------------


def test_a_failed_replay_post_keeps_the_label_for_the_next_pass(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("issue", "5", "41"))]
	gh.labelled = [{"number": 5}]
	gh.issue_comments[5] = [_c("w\n<!-- ai:claude-capacity-wait:v1 stage=clarify source=none run=41 role=CLARIFY -->")]
	gh.comment = lambda number, body: False  # type: ignore[method-assign]
	values = _resume(gh, tmp_path)
	# The label stays so the next pass can replay; the tracker stays open.
	assert gh.removed == [] and gh.closed == [] and values["capacity_wait"] == "true"
	assert "issue:5:41" not in gh.tracker_body


def test_a_failed_label_removal_keeps_the_pr_pending(tmp_path: Path) -> None:
	head = "c" * 40
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("pr", "8", "42"))]
	gh.prs[8] = {"state": "open", "labels": [{"name": po.CAPACITY_WAIT_LABEL}], "head": {"sha": head}, "base": {"repo": {"default_branch": "main"}}}
	gh.issue_comments[8] = [_c(f"p\n<!-- review-autofix-failure:v1 head={head} reason=claude_capacity fp={'b' * 64} run=42 -->")]
	gh.remove_label = lambda number, label: False  # type: ignore[method-assign]
	_resume(gh, tmp_path)
	# The label goes first, so a failed removal dispatches nothing and a
	# retry never queues a duplicate review.
	assert gh.dispatched == [] and gh.closed == []
	assert "pr:8:42" not in gh.tracker_body


def test_the_items_file_lists_only_what_still_waits(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("run", "none", "51")), _c(_item("run", "none", "52"))]
	gh.rerun_status = {52: 500}
	_resume(gh, tmp_path)
	items = json.loads((tmp_path / "items.json").read_text())
	assert items["count"] == 1 and [entry["run"] for entry in items["items"]] == ["52"]
	gh.rerun_status = {}
	_resume(gh, tmp_path)
	assert json.loads((tmp_path / "items.json").read_text())["count"] == 0 and gh.closed == [1]


def test_remove_label_treats_a_missing_label_as_removed(monkeypatch: pytest.MonkeyPatch) -> None:
	import subprocess

	gh = po.GitHub("o/r")
	monkeypatch.setattr(gh, "_run", lambda args: subprocess.CompletedProcess(args, 1, "", "gh: Label does not exist (HTTP 404)"))
	assert gh.remove_label(5, po.CAPACITY_WAIT_LABEL) is True
	monkeypatch.setattr(gh, "_run", lambda args: subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 502)"))
	assert gh.remove_label(5, po.CAPACITY_WAIT_LABEL) is False


def test_stall_recovery_leaves_capacity_waiting_prs_alone() -> None:
	text = _text(SCRIPTS / "orchestrate_poll_process.sh")
	assert "_linked_pr_is_capacity_waiting()" in text
	assert text.count("reason=claude_capacity_wait pr=") == 3


def test_a_deleted_pr_or_source_comment_is_superseded(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("pr", "8", "43")), _c(_item("issue", "5", "44"))]
	gh.prs[8] = {"_gone": True}
	gh.labelled = [{"number": 5}]
	gh.issue_comments[5] = [_c("w\n<!-- ai:claude-capacity-wait:v1 stage=plan source=903 run=44 role=PLAN -->")]
	gh.comment_by_id[903] = {"_gone": True}
	_resume(gh, tmp_path)
	assert gh.dispatched == [] and gh.closed == [1]
	assert gh.removed == [5]


def test_a_wait_recorded_during_the_pass_keeps_the_tracker_open(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("run", "none", "45"))]
	original_rerun = gh.rerun_run

	def rerun_and_record(run_id: int) -> tuple[bool, int | None]:
		gh.tracker_comments.append(_c(_item("run", "none", "46")))
		return original_rerun(run_id)

	gh.rerun_run = rerun_and_record  # type: ignore[method-assign]
	_resume(gh, tmp_path)
	assert gh.closed == []
	_resume(gh, tmp_path)
	assert gh.closed == [1] and 46 in gh.reruns


@pytest.mark.parametrize(
	("path", "needle"),
	[
		("scripts/self_heal_validation.sh", 'if [ "${_self_heal_run_rc}" -eq 80 ]; then'),
		("scripts/validate_process.sh", "AI_ENGINE_CAPACITY_WAIT role=VALIDATE_SELF_HEAL outcome=deferred"),
		("scripts/review_rb_judge.sh", 'if [ "${rb_judge_claude_rc}" -eq 80 ]; then'),
		("scripts/review_rb_judge.sh", 'if [ "${rb_fix_claude_rc}" -eq 80 ]; then'),
		("scripts/review_apply_fixes.sh", 'if [ "${cmd_rc}" -eq 80 ]; then'),
		("scripts/summarize_reviewer_consensus.sh", 'if [ "${last_rc}" -eq 80 ]; then'),
		("scripts/workflow_retro_fanout.sh", 'if [ "${codex_exit}" -eq 80 ]; then'),
	],
)
def test_review_round_call_sites_stop_on_the_capacity_wait(path: str, needle: str) -> None:
	assert needle in _text(REPO_ROOT / path)


def test_log_analysis_passes_stop_on_the_capacity_wait() -> None:
	text = _text(WORKFLOWS / "workflow-log-analysis.yml")
	for var in ("RETRO_EXIT", "ANALYZE_EXIT", "AUDIT_EXIT", "API_EXIT"):
		assert f'if [ "${{{var}}}" -eq 80 ]; then' in text, var


def test_panel_slot_does_not_set_the_job_failure_reason() -> None:
	text = _text(SCRIPTS / "review_run_reviewers.sh")
	block = text[text.index("      80)\n        # Claude capacity wait (issue #7370)"):text.index("      75|76)")]
	assert 'GITHUB_ENV' not in block


# --- Review fixes (issue #7370 review round 3) --------------------------------


def test_an_unlabelled_pr_is_not_dispatched_again(tmp_path: Path) -> None:
	# The label goes before the dispatch, so an unlabelled PR was already
	# dispatched (or the sweep owns it); the pass must not queue a duplicate.
	head = "d" * 40
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("pr", "8", "61"))]
	gh.prs[8] = {"state": "open", "labels": [], "head": {"sha": head}, "base": {"repo": {"default_branch": "main"}}}
	gh.issue_comments[8] = [_c(f"p\n<!-- review-autofix-failure:v1 head={head} reason=claude_capacity fp={'b' * 64} run=61 -->")]
	_resume(gh, tmp_path)
	assert gh.dispatched == [] and gh.closed == [1]
	assert "pr:8:61" in gh.tracker_body


def test_an_unlabelled_issue_is_still_replayed(tmp_path: Path) -> None:
	# A failed label add after the tracker marker must not strand the wait.
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("issue", "5", "62"))]
	gh.labelled = []
	gh.issue_comments[5] = [_c("w\n<!-- ai:claude-capacity-wait:v1 stage=clarify source=none run=62 role=CLARIFY -->")]
	_resume(gh, tmp_path)
	[(number, body)] = [entry for entry in gh.posted if entry[0] == 5]
	assert body.endswith("<!-- ai:claude-capacity-wait-replay:v1 source=none run=62 -->")
	assert gh.closed == [1]


def test_an_already_replayed_unlabelled_issue_is_not_replayed_twice(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("issue", "5", "63"))]
	gh.labelled = []
	gh.issue_comments[5] = [
		_c("w\n<!-- ai:claude-capacity-wait:v1 stage=clarify source=none run=63 role=CLARIFY -->"),
		_c("/reclarify [auto-requeued-by-capacity-wait]\n<!-- ai:claude-capacity-wait-replay:v1 source=none run=63 -->"),
	]
	_resume(gh, tmp_path)
	assert [entry for entry in gh.posted if entry[0] == 5] == []
	assert gh.closed == [1]


def test_each_pass_records_its_probe_time(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("run", "none", "64"))]
	_resume(gh, tmp_path, available=False)
	assert re.search(r"<!-- ai:claude-capacity-wait-probe:v1 at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z -->", gh.tracker_body)
	_resume(gh, tmp_path)
	assert gh.tracker_body.count("ai:claude-capacity-wait-probe:v1") == 1
	assert "done=run:none:64" in gh.tracker_body


def test_pool_window_also_opens_an_hour_after_the_last_pass() -> None:
	text = _text(WORKFLOWS / "orchestrate_poll.yml")
	step = text[text.index("        id: pool_window\n"):text.index("      - name: Install Claude Code CLI\n")]
	assert "provider_outage_trackers.json" in step
	assert "ai:claude-capacity-wait-probe:v1" in step
	assert "-ge 3300 ]" in step


# --- Review fixes (issue #7370 review round 4) --------------------------------


def test_a_failed_pr_dispatch_restores_the_label_for_the_next_pass(tmp_path: Path) -> None:
	head = "e" * 40
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("pr", "8", "71"))]
	gh.prs[8] = {"state": "open", "labels": [{"name": po.CAPACITY_WAIT_LABEL}], "head": {"sha": head}, "base": {"repo": {"default_branch": "main"}}}
	gh.issue_comments[8] = [_c(f"p\n<!-- review-autofix-failure:v1 head={head} reason=claude_capacity fp={'b' * 64} run=71 -->")]
	gh.dispatch = lambda workflow, ref, number: False  # type: ignore[method-assign]
	_resume(gh, tmp_path)
	assert gh.removed == [8] and gh.added == [8] and gh.closed == []
	assert "pr:8:71" not in gh.tracker_body


def test_a_failed_label_listing_does_not_fail_issue_items(tmp_path: Path) -> None:
	gh = FakeGitHub()
	gh.tracker_comments = [_c(_item("issue", "5", "72"))]
	gh.issue_comments[5] = [_c("w\n<!-- ai:claude-capacity-wait:v1 stage=clarify source=none run=72 role=CLARIFY -->")]
	original = gh.list_issues
	gh.list_issues = lambda label: original(label) if label == po.TRACKER_LABEL else None  # type: ignore[method-assign]
	_resume(gh, tmp_path)
	assert [entry for entry in gh.posted if entry[0] == 5] and gh.closed == [1]


def test_check_triage_wait_exports_no_job_state() -> None:
	text = _text(SCRIPTS / "check_failure_triage.sh")
	assert "GITHUB_ENV" not in text
	assert 'log "skip reason=claude_capacity_wait"' in text


def test_security_audit_workflow_defers_on_a_capacity_wait() -> None:
	text = _text(WORKFLOWS / "security-audit.yml")
	assert 'if [ "${security_audit_rc}" -eq 80 ]; then' in text
	assert "env.AUDIT_DATA_SHA != '' && env.CLAUDE_CAPACITY_WAIT != 'true'" in text


def test_pool_health_alert_sends_waiting_items_outside_the_minute_window() -> None:
	text = _text(SCRIPTS / "claude_pool_health_alert.sh")
	assert 'if [ "${minute}" -ge "${window}" ] && [ "${waiting_count}" -eq 0 ]; then' in text

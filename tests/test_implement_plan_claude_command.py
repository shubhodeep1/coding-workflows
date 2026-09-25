"""Contract for the /implement-plan-claude command text: the rules the
end-to-end dummy run (plan #4307) showed must hold for an unattended chain."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMAND = ROOT / ".claude" / "commands" / "implement-plan-claude.md"
TEMPLATE_COMMAND = ROOT / "workflow-templates" / ".claude" / "commands" / "implement-plan-claude.md"
CLAUDE_MD = ROOT / "CLAUDE.md"
REVIEW_COMMANDS = [
	ROOT / prefix / ".claude" / "commands" / name
	for prefix in ("", "workflow-templates")
	for name in ("verify-activation.md", "deploy-activate.md")
]


def _flat(path: Path) -> str:
	return " ".join(path.read_text(encoding="utf-8").split())


@pytest.fixture(scope="module")
def text() -> str:
	return " ".join(COMMAND.read_text(encoding="utf-8").split())


def test_template_parity():
	assert TEMPLATE_COMMAND.read_text(encoding="utf-8") == COMMAND.read_text(encoding="utf-8")


def test_validation_verdict_reads_status_json_first(text):
	# A passing validate run emits no VALIDATION_FAILURE_SUMMARY line.
	status_json = text.index("(1) `validation_status.json`")
	env = text.index("(2) the `STATUS_VALUE` / `RAW_STATUS_VALUE`")
	summary_line = text.index("(3) the `VALIDATION_FAILURE_SUMMARY` line")
	assert status_json < env < summary_line
	assert "emits **only on a failing verdict**" in text


def test_failed_runs_block_instead_of_passing(text):
	assert "only a `success` run has audited anything" in text
	assert "never continue to step 9 on your own" in text


def test_completion_pr_body_always_references_an_issue_or_pr(text):
	assert "`lint-plan-archival.yml` fails an archival PR whose body references no `#N` at all" in text
	assert "`Refs #<phase PR>`" in text


def test_checker_does_not_archive_itself(text):
	assert "and finally archive_session on your own session id" not in text
	assert "Do **not** archive yourself" in text


def test_only_the_chain_archives_its_sessions(text):
	assert "**Only the chain archives its own sessions.**" in text
	assert "if it is blocked, archived, or idle with no pending check-in" in text


def test_source_revision_reads_plan_and_log_from_default_branch(text):
	assert "read both from `origin/<default>`" in text


def test_checker_is_sonnet_every_three_hours(text):
	# Haiku cannot run in Auto mode, and outside it the claude-code-remote
	# write tools prompt on every call, so a Haiku checker never runs unattended.
	assert "`model: claude-sonnet-5`" in text
	assert "claude-haiku-4-5-20251001" not in text
	assert '`model: "haiku"`' not in text
	assert "call send_later with delay_minutes 60" in text
	assert "call send_later with delay_minutes 180" not in text


def test_claude_md_section_28_scopes_auto_decisions():
	claude = _flat(CLAUDE_MD)
	assert "## §28. Unattended Auto-Decisions in `/implement-plan-claude` Projects (MANDATORY)" in claude
	# Start-up checks, failure escalations, and ask-first operations are never auto-decided.
	assert "### C) Never auto-decided — still stop and ask" in claude
	assert "§22.B (DigitalOcean mutations), §23.C" in claude
	assert "`codex_failure`, unknown payload" in claude
	# §0 and the final reminder point at the exception.
	assert "The one exception is §28" in claude
	assert "Inside §28's scope: record the RECOMMENDED pick and continue." in claude


def test_chain_auto_decides_after_startup_checks(text):
	assert "## Auto-Decisions" in text
	assert "From the end of step 3 onward" in text
	assert "Steps 0, 1, and 3 still ask" in text
	assert "`docs/plans/<slug>-plan.md — scope conformance — unattended`" in text
	assert "always ending with `— unattended` (§28)" in text
	assert "## Auto-decisions - AD-<n> [<stage>, <YYYY-MM-DD>]" in text
	assert "Uncommitted auto-decisions:" in text
	# The old hard stop on plan ambiguity is gone.
	assert "stop and ask (§0/§2) rather than shipping a guess" not in text


def test_review_commands_list_auto_decisions_without_asking():
	for path in REVIEW_COMMANDS:
		body = _flat(path)
		assert "`change AD-<n> → <letter>`" in body, path
		if path.name == "verify-activation.md":
			assert "## Unattended Runs" in body, path
			assert "Auto-decisions (for review)" in body, path
		else:
			assert "## Auto-Decisions Review" in body, path
			assert "`claude/implement-plan-<slug>-decision-changes`" in body, path
			assert "no Q/A question is asked about any entry" in body, path


def test_live_state_still_accepts_decision_review_replies(text):
	assert "the LIVE stop does not apply to a `change AD-<n> → <letter>` or `confirm` reply" in text
	for path in REVIEW_COMMANDS:
		if path.name == "deploy-activate.md":
			body = _flat(path)
			assert "`Status: LIVE` report LIVE and stop unless the current reply changes an auto-decision" in body, path
			assert "`Status: LIVE` just report LIVE and stop unless the current reply changes an auto-decision" in body, path
			rules = body.split("## Rules", 1)[1]
			assert "`Status: LIVE`, report LIVE and stop unless the current reply changes an auto-decision" in rules, path


def test_project_branch_and_draft_final_pr(text):
	assert "3a. **Open the project branch and the draft final PR**" in text
	assert "`claude/implement-plan-<slug>`" in text
	assert "**`draft: true`**" in text
	assert "mark it ready for review (`mcp__github__update_pull_request` with `draft: false`)" in text
	assert "11a. **Final merge into the default branch**" in text
	# Legacy projects (log on the default branch without a Project branch line)
	# finish the way they started.
	assert "**Legacy mode** when the log exists on the default branch without a `Project branch:` line" in text
	assert "`git merge --no-edit origin/<default>`" in text


def test_review_rounds_are_fixed_by_claude(text):
	assert "7a. **Review round — Claude fixes what the reviewer panel found.**" in text
	assert "<!-- ai:claude-fixer-handoff:v1 kind=<findings|conflict> head=<sha> round=<r> -->" in text
	assert "`[claude-autofix] review round <r>: <summary>`" in text
	assert "`<!-- ai:claude-fixer-verdict:v1 head=<sha> -->`" in text
	assert "-f claude_fixer_converged_head=<sha>" in text
	assert "`[claude-merge-resolve] merge <base branch>`" in text
	assert "`[claude-intervention] <summary>`" in text
	assert "the workflow never runs the GPT review-blocked judge" in text
	assert "if `state` is review-round / conflict use `<next stage on review round>`" in text


def test_security_dispatch_targets_project_branch(text):
	assert "`-f ref=claude/implement-plan-<slug>`" in text
	# The audit files every finding (no weekly cap), so no bypass input exists.
	assert "bypass_weekly_cap" not in text
	assert "deferred_by_weekly_cap" not in text


def test_convergence_dispatch_goes_straight_to_review_autofix_here(text):
	# internal-review.yml pins review_autofix.yml@main; forwarding a new input
	# through it made every run on the PR adding the input a startup_failure
	# (run 36095647423).
	assert "`gh workflow run review_autofix.yml -R <owner>/<repo> -f pr_number=<N> -f claude_fixer_converged_head=<sha>`" in text
	assert "gh workflow run internal-review.yml" not in text


def test_validation_dispatch_inputs(text):
	assert "plus `-f pr_number=0` for `internal-validate.yml` only" in text
	assert "`-f target_ref=claude/implement-plan-<slug>`" in text
	assert "-f tracking_issue=0 -f pr_number=0" not in text


def test_checker_starts_with_effort_low_then_one_shot_instructions(text):
	assert "the prompt `/effort low` **and nothing else**" in text
	assert "`create_trigger` with `persistent_session_id` = the checker's session id" in text
	assert "**hourly check-in by a low-effort Sonnet checker session**" in text
	assert "3-hourly" not in text
	assert "every 3h" not in text

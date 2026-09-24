"""Contract for the /implement-plan-claude command text: the rules the
end-to-end dummy run (plan #4307) showed must hold for an unattended chain."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMAND = ROOT / ".claude" / "commands" / "implement-plan-claude.md"
TEMPLATE_COMMAND = ROOT / "workflow-templates" / ".claude" / "commands" / "implement-plan-claude.md"


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


def test_checker_rearms_hourly(text):
	assert "call send_later with delay_minutes 60" in text
	assert "delay_minutes 180" not in text

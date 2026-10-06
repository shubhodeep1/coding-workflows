"""Contract for the /audit-plans command text: the merge-conflict gate screens
candidates against in-flight AI-orchestrator projects (started by
/implement-plan-ai or /implement-plan-claude) and sets in-flight plans aside."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMAND = ROOT / ".claude" / "commands" / "audit-plans.md"
TEMPLATE_COMMAND = ROOT / "workflow-templates" / ".claude" / "commands" / "audit-plans.md"


@pytest.fixture(scope="module")
def text() -> str:
	return " ".join(COMMAND.read_text(encoding="utf-8").split())


@pytest.fixture(scope="module")
def step6(text) -> str:
	start = text.index("6. **Screen candidates against in-flight project work")
	end = text.index("7. **Archive verified-complete plans.**")
	return text[start:end]


def test_template_parity():
	assert TEMPLATE_COMMAND.read_text(encoding="utf-8") == COMMAND.read_text(encoding="utf-8")


def test_command_context_reads_follow_claude_md():
	# CLAUDE.md PRE-TASK: search README.md / agents.md for relevant sections;
	# CLAUDE.md itself is already loaded, so commands do not re-read it.
	live = ROOT / ".claude" / "commands"
	template = ROOT / "workflow-templates" / ".claude" / "commands"
	targets = [
		live / f"{name}.md"
		for name in ("apply-analysis", "apply-url", "audit-plans", "implement-plan-ai", "validate-consumer-issue", "write-plan")
	] + [template / f"{name}.md" for name in ("apply-analysis", "apply-url", "audit-plans", "implement-plan-ai", "validate-consumer-issue", "write-plan")]
	for path in targets:
		command_text = path.read_text(encoding="utf-8")
		assert "search `README.md` and `agents.md`" in command_text, path
		assert "read those instead of both files end to end" in command_text, path
		assert "`CLAUDE.md` is already loaded" in command_text or "CLAUDE.md (already loaded)" in command_text, path
		assert "and `CLAUDE.md` at the repo root" not in command_text, path
		assert "read `README.md`, `agents.md`, and `CLAUDE.md`" not in command_text, path


def test_implement_issue_claude_describes_role_specific_engine():
	for path in (ROOT / ".claude/commands/implement-issue-claude.md", ROOT / "workflow-templates/.claude/commands/implement-issue-claude.md"):
		command_text = path.read_text(encoding="utf-8")
		assert "review still runs on OpenCode" in command_text, path
		assert "Engine label: ai:engine-claude added (Claude for cut-over roles; review on OpenCode)" in command_text, path


def test_gate_enumerates_orchestrator_projects(step6):
	assert "label `ai:orchestrator-tracking`" in step6
	assert "`^orchestrator/project-`" in step6


def test_gate_batches_github_reads(step6):
	# §15: one issue listing and one PR listing serve every project.
	assert "one issue listing and one PR listing serve every project" in step6


def test_in_flight_plans_are_set_aside(step6, text):
	# A plan already being implemented is neither recommended nor screened.
	assert "**Set aside plans that are already in flight.**" in step6
	assert step6.index("**Set aside plans that are already in flight.**") < step6.index("**Screen the remaining candidates")
	assert "**In-flight plans are never candidates.**" in text


def test_degraded_mode_marks_the_pick_unscreened(step6):
	assert "mark the pick **UNSCREENED**" in step6


def test_retired_session_chain_is_not_screened(text):
	# The session-driven /implement-plan-claude chain and its progress logs were retired.
	assert "docs/implement-plan/" not in text
	assert "claude/implement-plan-" not in text

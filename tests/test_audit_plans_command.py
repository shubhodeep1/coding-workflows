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

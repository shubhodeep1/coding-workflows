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


def test_bundle_rule_lives_in_the_gate_after_the_pick(step6):
	# Bundles of related plans run as ONE orchestrator project, anchored on the
	# conflict-screened pick, so they inherit the merge-conflict gate.
	assert "**Bundle related plans into one project when appropriate.**" in step6
	assert step6.index("**Pick.**") < step6.index("**Bundle related plans into one project when appropriate.**")
	assert "never an in-flight or screened-out plan" in step6


def test_bundle_requires_relatedness_and_caps(step6, text):
	assert "Unrelated plans are never bundled" in step6
	assert "at most **3 plans** and at most **8 phases**" in step6
	assert "Bundling never applies to an UNSCREENED pick" in step6
	assert "**Bundles are earned by relatedness, not size.**" in text
	assert "never recommend several separate projects at once" in text


def test_bundle_output_names_a_single_hand_off(text):
	assert "bundle of N related plans as ONE orchestrator project" in text
	assert "Hand off: /implement-plan-claude <plan path> <plan path>" in text


@pytest.mark.parametrize("base", [ROOT / ".claude", ROOT / "workflow-templates" / ".claude"])
def test_implement_plan_ai_dispatches_a_bundle_once(base):
	ipa = " ".join((base / "commands" / "implement-plan-ai.md").read_text(encoding="utf-8").split())
	assert "## Bundles" in ipa
	assert "exactly one dispatch and one tracking issue for the whole bundle — never one dispatch per plan" in ipa
	assert "never merge phases from different plans into one PR" in ipa
	assert "A bundle is only ever built from explicitly named paths" in ipa
	assert "more than 8 phases in total" in ipa
	ipc = " ".join((base / "commands" / "implement-plan-claude.md").read_text(encoding="utf-8").split())
	assert "or name a bundle of 2–3 plan paths dispatched as one project" in ipc


def test_implement_plan_template_parity():
	for name in ("implement-plan-ai", "implement-plan-claude"):
		live = ROOT / ".claude" / "commands" / f"{name}.md"
		template = ROOT / "workflow-templates" / ".claude" / "commands" / f"{name}.md"
		assert template.read_text(encoding="utf-8") == live.read_text(encoding="utf-8"), name

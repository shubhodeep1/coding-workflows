"""Contract for the /audit-plans command text: the merge-conflict gate screens
candidates against both project runners — AI-orchestrator projects and
/implement-plan-claude projects — and never archives a plan a running
/implement-plan-claude project still owns."""

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


def test_gate_enumerates_implement_plan_claude_from_logs_and_prs(step6):
	# Q1: the union of progress logs and open claude/implement-plan-* PRs.
	assert "**Progress logs.** Every `docs/implement-plan/*.md` except `README.md`" in step6
	assert "`IN_PROGRESS` or `BLOCKED`" in step6
	assert "head **or** base branch starts with `claude/implement-plan-`" in step6
	# Both branch shapes: per-phase branches and the single project branch.
	assert "`claude/implement-plan-<slug>-phase-<n>`" in step6
	assert "`claude/implement-plan-<slug>-validation-fix-<k>`" in step6
	assert "`claude/implement-plan-<slug>-complete`" in step6
	assert "the project branch `claude/implement-plan-<slug>`" in step6
	# A new project's first PR carries the log before the default branch does.
	assert "take `<slug>` from the `docs/implement-plan/<slug>.md` path in the PR's changed files" in step6


def test_gate_batches_github_reads(step6):
	# §15: one issue listing and one PR listing serve both runners.
	assert "one issue listing and one PR listing serve both runners and every project" in step6


def test_post_phase_stages_count_the_whole_plan(step6):
	# Q2: once every phase is ticked, unpredictable fix PRs land on the plan's files.
	assert "once every phase is ticked" in step6
	assert "**every file the plan declares**" in step6
	assert "regardless of `Stage:`" in step6
	assert "With no default-branch log, count every file of a uniquely identified source plan" in step6


def test_in_flight_plans_are_set_aside(step6, text):
	# Q3: a plan already being implemented is neither recommended nor screened.
	assert "**Set aside plans that are already in flight.**" in step6
	assert step6.index("**Set aside plans that are already in flight.**") < step6.index("**Screen the remaining candidates")
	assert "**In-flight plans are never candidates.**" in text
	assert "PR-only project's slug uniquely matches a plan filename" in step6
	assert "if no unique source plan matches, do not guess" in step6


def test_in_flight_implement_plan_claude_plans_are_never_archived(text):
	# Q4: the project's own completion PR moves the plan.
	step7 = text[text.index("7. **Archive verified-complete plans.**"):text.index("8. **Sweep the removal registry.**")]
	assert "**Never archive the source plan of an in-flight `/implement-plan-claude` project**" in step7
	assert "Not archived — in flight" in text
	assert "defer archiving until ownership can be verified" in text


def test_degraded_mode_still_screens_against_logs(step6):
	assert "still enumerate `/implement-plan-claude` projects from their progress logs" in step6
	assert "mark the pick **UNSCREENED**" in step6


def test_report_names_implement_plan_claude_blockers(text):
	assert "In-flight /implement-plan-claude work:" in text
	assert "log not on default branch, stage unknown" in text
	assert "PR #M, slug unknown" in text
	assert "blocked by <issue #N / PR #M / implement-plan <slug>> on <path(s)>" in text

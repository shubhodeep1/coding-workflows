#!/usr/bin/env python3
"""Tests for reviewer reasoning-effort behaviour in review_autofix.yml.

The schedule machinery (REVIEW_REASONING_SCHEDULE, REVIEW_AUTODOWNGRADE_DISABLED,
cycle-based selector step) has been removed. All reasoning stays at the
configured THINKING_LEVEL_* value for every cycle — no runtime downgrades.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


def _workflow() -> str:
	return REVIEW_AUTOFIX_WF.read_text(encoding="utf-8")


def test_no_reasoning_schedule_env_vars() -> None:
	wf = _workflow()
	assert "REVIEW_REASONING_SCHEDULE" not in wf
	assert "REVIEW_AUTODOWNGRADE_DISABLED" not in wf


def test_no_cycle_selector_step() -> None:
	wf = _workflow()
	assert "Select reviewer reasoning effort for current cycle" not in wf
	assert "smoke_override" not in wf
	assert "kill_switch_enabled" not in wf
	assert "schedule_source" not in wf


def test_no_smoke_test_reasoning_override() -> None:
	wf = _workflow()
	assert "REVIEW_SMOKE_OVERRIDE_ACTIVE" not in wf
	assert 'REVIEWER_REASONING_EFFORT=low' not in wf
	assert 'EDITOR_REASONING_EFFORT=low' not in wf
	assert 'model_reasoning_effort = "low"' not in wf


def test_editor_switch_replaces_any_reasoning_value() -> None:
	wf = _workflow()
	assert 'sed -i "s/^model_reasoning_effort = \\".*\\"/model_reasoning_effort = \\"${EDITOR_REASONING_EFFORT}\\"/" ~/.codex/config.toml' in wf
	assert 'sed -i "s/model_reasoning_effort = \\"${REVIEWER_REASONING_EFFORT}\\"/model_reasoning_effort = \\"${EDITOR_REASONING_EFFORT}\\"/" ~/.codex/config.toml' not in wf

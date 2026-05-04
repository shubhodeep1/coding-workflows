#!/usr/bin/env python3
"""Tests for reviewer reasoning-effort behaviour in review_autofix.yml.

The cycle-based schedule machinery (REVIEW_REASONING_SCHEDULE,
REVIEW_AUTODOWNGRADE_DISABLED) has been removed. Non-smoke-test runs use
the configured THINKING_LEVEL_* for every cycle. Smoke test runs override
reasoning to ``none`` for both reviewer and editor phases.
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
	assert "schedule_source" not in wf


def test_smoke_test_forces_none_reasoning() -> None:
	wf = _workflow()
	assert 'REVIEWER_REASONING_EFFORT=none' in wf
	assert 'EDITOR_REASONING_EFFORT=none' in wf
	assert 'model_reasoning_effort = "none"' in wf


def test_editor_switch_replaces_any_reasoning_value() -> None:
	wf = _workflow()
	assert 'sed -i "s/^model_reasoning_effort = \\".*\\"/model_reasoning_effort = \\"${EDITOR_REASONING_EFFORT}\\"/" ~/.codex/config.toml' in wf
	assert 'sed -i "s/model_reasoning_effort = \\"${REVIEWER_REASONING_EFFORT}\\"/model_reasoning_effort = \\"${EDITOR_REASONING_EFFORT}\\"/" ~/.codex/config.toml' not in wf


def test_conflict_resolver_reasoning_env_wired() -> None:
	"""Resolver step must pass CONFLICT_RESOLVER_REASONING_EFFORT to
	review_conflict_resolve.sh so the smoke-test editor's reasoning=none
	override doesn't starve the resolver (PR #2058 / run 25300219172).
	"""
	wf = _workflow()
	assert "CONFLICT_RESOLVER_REASONING_EFFORT:" in wf
	assert "vars.THINKING_LEVEL_CONFLICT_RESOLVER" in wf
	resolver_script = (REPO_ROOT / "scripts" / "review_conflict_resolve.sh").read_text(encoding="utf-8")
	assert "CONFLICT_RESOLVER_REASONING_EFFORT" in resolver_script
	# Validation must match README's documented levels (xhigh|high|medium|none)
	# — `low` is not a documented level and must not be silently accepted.
	assert "xhigh|high|medium|none)" in resolver_script
	assert "xhigh|high|medium|low|none" not in resolver_script
	# grep/sed must tolerate whitespace + unquoted variants so a non-canonical
	# config doesn't silently no-op and leave the resolver on stale `none`.
	assert "[[:space:]]*model_reasoning_effort[[:space:]]*=" in resolver_script

#!/usr/bin/env python3
"""Tests for reviewer reasoning-effort scheduling in review_autofix.yml."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


def _workflow() -> str:
	return REVIEW_AUTOFIX_WF.read_text(encoding="utf-8")


def test_schedule_env_defaults_are_declared() -> None:
	wf = _workflow()
	assert "REVIEW_REASONING_SCHEDULE: ${{ vars.REVIEW_REASONING_SCHEDULE || 'xhigh,high,medium' }}" in wf
	assert "REVIEW_AUTODOWNGRADE_DISABLED: ${{ vars.REVIEW_AUTODOWNGRADE_DISABLED || 'false' }}" in wf


def test_cycle_selector_step_exists_and_uses_iteration_plus_one() -> None:
	wf = _workflow()
	assert "- name: Select reviewer reasoning effort for current cycle" in wf
	assert 'autofix_iteration="${{ steps.retrigger_guard.outputs.autofix_iteration }}"' in wf
	assert "cycle_index=$((autofix_iteration + 1))" in wf


def test_selector_precedence_and_sources_are_present() -> None:
	wf = _workflow()
	assert 'echo "REVIEW_SMOKE_OVERRIDE_ACTIVE=true" >> "$GITHUB_ENV"' in wf
	assert 'if [ "${smoke_override}" = "true" ]; then' in wf
	assert 'schedule_source="smoke_override"' in wf
	assert 'elif [ "${kill_switch_enabled}" = "true" ]; then' in wf
	assert 'schedule_source="kill_switch_disabled_schedule"' in wf
	assert 'schedule_source="schedule"' in wf
	assert 'schedule_source="fallback_default"' in wf


def test_selector_validates_schedule_and_clamps_to_last_entry() -> None:
	wf = _workflow()
	assert "IFS=',' read -r -a raw_schedule <<< \"${schedule_csv}\"" in wf
	assert 'case "${normalized}" in' in wf
	assert 'xhigh|high|medium|low)' in wf
	assert 'if [ "${#invalid_schedule_entries[@]}" -gt 0 ] || [ "${#normalized_schedule[@]}" -eq 0 ]; then' in wf
	assert 'if [ "${schedule_index}" -ge "${#normalized_schedule[@]}" ]; then' in wf
	assert 'schedule_index=$((${#normalized_schedule[@]} - 1))' in wf


def test_structured_log_contains_required_fields() -> None:
	wf = _workflow()
	assert "reviewer_reasoning_selection:" in wf
	assert "cycle_index" in wf
	assert "selected_reasoning_level" in wf
	assert "schedule_source" in wf
	assert "kill_switch_enabled" in wf
	assert "smoke_override" in wf


def test_editor_switch_replaces_any_reasoning_value() -> None:
	wf = _workflow()
	assert 'sed -i "s/^model_reasoning_effort = \\".*\\"/model_reasoning_effort = \\"${EDITOR_REASONING_EFFORT}\\"/" ~/.codex/config.toml' in wf
	assert 'sed -i "s/model_reasoning_effort = \\"${REVIEWER_REASONING_EFFORT}\\"/model_reasoning_effort = \\"${EDITOR_REASONING_EFFORT}\\"/" ~/.codex/config.toml' not in wf

#!/usr/bin/env python3
"""Regression tests for implement.yml destructive-blocked recovery behavior."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WF = REPO_ROOT / ".github" / "workflows" / "implement.yml"


def _workflow() -> str:
	return IMPLEMENT_WF.read_text(encoding="utf-8")


def _step_block(step_name: str, next_step_name: str) -> str:
	wf = _workflow()
	start_marker = f"- name: {step_name}\n"
	end_marker = f"\n      - name: {next_step_name}\n"
	start = wf.find(start_marker)
	assert start != -1, f"Missing step: {step_name!r}"
	end = wf.find(end_marker, start + len(start_marker))
	assert end != -1, f"Missing next step marker after {step_name!r}: {next_step_name!r}"
	return wf[start:end]


def test_noop_failure_labeling_is_gated_on_non_destructive_failures() -> None:
	wf = _workflow()
	assert (
		"if: env.SKIP_IMPLEMENT != 'true' && steps.commit_changes.outputs.did_commit == 'false' "
		"&& steps.commit_changes.outputs.destructive_commit_blocked == ''"
	) in wf, (
		"Handle no-op implementation must be skipped when destructive_commit_blocked is set "
		"so destructive-guard failures cannot transition the issue into ai:implementation-failed"
	)


def test_failure_comment_step_skips_destructive_blocked_runs() -> None:
	wf = _workflow()
	assert (
		"if: (failure() || cancelled()) && steps.commit_changes.outputs.destructive_commit_blocked == ''"
	) in wf, (
		"Generic failure comment flow must be disabled for destructive-blocked runs to avoid "
		"re-adding ai:awaiting-approval"
	)


def test_telegram_failure_step_skips_destructive_blocked_runs() -> None:
	telegram_block = _step_block("Telegram failure notification", "Record implementation run failure event")
	assert "if: (failure() || cancelled()) && steps.commit_changes.outputs.destructive_commit_blocked == ''" in telegram_block, (
		"Post-failure Telegram flow must be skipped for destructive-blocked runs; only the dedicated "
		"destructive-guard CRITICAL alert should fire"
	)


def test_destructive_guard_path_does_not_set_implementation_failed_or_fixup_flow() -> None:
	destructive_block = _step_block("Destructive-commit guard — label + alert on rejection", "Handle no-op implementation")
	lowered = destructive_block.lower()
	assert "--add-label 'ai:destructive-blocked'" in destructive_block, (
		"Destructive guard must preserve ai:destructive-blocked human-halt signaling"
	)
	assert "ai:implementation-failed" not in destructive_block, (
		"Destructive guard block must not apply ai:implementation-failed"
	)
	assert "fix-up" not in lowered and "fixup" not in lowered, (
		"Destructive guard block must not trigger fix-up issue generation"
	)

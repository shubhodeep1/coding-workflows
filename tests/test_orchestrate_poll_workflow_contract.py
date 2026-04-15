#!/usr/bin/env python3
"""Contract tests for orchestrate_poll workflow env mapping."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE_POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"


def _workflow() -> str:
	return ORCHESTRATE_POLL_WF.read_text(encoding="utf-8")


def test_stall_control_env_defaults_are_declared() -> None:
	wf = _workflow()
	assert "STALL_JUDGE_TRIGGER_COUNT: ${{ vars.STALL_JUDGE_TRIGGER_COUNT || '2' }}" in wf
	assert "ENABLE_STALL_JUDGE: ${{ vars.ENABLE_STALL_JUDGE || 'true' }}" in wf
	assert "ENABLE_STALL_HUMAN_TERMINALIZATION: ${{ vars.ENABLE_STALL_HUMAN_TERMINALIZATION || 'false' }}" in wf


def test_stall_recovery_prompt_is_bootstrapped_with_main_fallback() -> None:
	wf = _workflow()
	assert "for pf in mode-judge.txt mode-judge-review-blocked.txt mode-judge-stall-recovery.txt serena-efficiency-block.txt; do" in wf
	assert "repos/${wf_source}/contents/prompts/${pf}?ref=${script_ref}" in wf
	assert "${pf} not found on ${script_ref}; falling back to main" in wf
	assert "repos/${wf_source}/contents/prompts/${pf}?ref=main" in wf
	assert "::warning::${pf} not found on ${script_ref}; falling back to main" in wf


def main() -> int:
	test_stall_control_env_defaults_are_declared()
	test_stall_recovery_prompt_is_bootstrapped_with_main_fallback()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

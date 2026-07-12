#!/usr/bin/env python3
"""Contract tests for orchestrate_poll workflow env mapping."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE_POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
ORCHESTRATE_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate.yml"
ORCHESTRATE_POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _workflow(path: Path = ORCHESTRATE_POLL_WF) -> str:
	return path.read_text(encoding="utf-8")


def test_stall_control_env_defaults_are_declared() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	assert "STALL_JUDGE_TRIGGER_COUNT: ${{ vars.STALL_JUDGE_TRIGGER_COUNT || '2' }}" in wf
	assert "ENABLE_STALL_JUDGE: ${{ vars.ENABLE_STALL_JUDGE || 'true' }}" in wf
	assert "ENABLE_STALL_HUMAN_TERMINALIZATION: ${{ vars.ENABLE_STALL_HUMAN_TERMINALIZATION || 'false' }}" in wf
	assert "JUDGE_REPEAT_FINGERPRINT_MAX: ${{ vars.JUDGE_REPEAT_FINGERPRINT_MAX || '2' }}" in wf


def test_stall_recovery_prompt_is_bootstrapped_with_main_fallback() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	assert "for pf in mode-judge.txt mode-judge-review-blocked.txt mode-judge-stall-recovery.txt; do" in wf
	assert "src=\".codex-workflow-src/prompts/${pf}\"" in wf
	assert "if [ ! -f \"${src}\" ] && [ -f \".codex-workflow-src-main/prompts/${pf}\" ]; then" in wf
	assert "src=\".codex-workflow-src-main/prompts/${pf}\"" in wf
	assert "::error::Missing required support file prompts/${pf}" in wf
	assert "install -m 0644 \"${src}\" \"prompts/${pf}\"" in wf


def test_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets() -> None:
	wf = _workflow()
	assert "validation_history.v1.json" in wf
	assert "operator_bypass_audit.v1.json" in wf
	assert "revalidate_events.v1.json" in wf


def test_orchestrate_workflow_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets() -> None:
	wf = _workflow(ORCHESTRATE_WF)
	assert "validation_history.v1.json" in wf
	assert "operator_bypass_audit.v1.json" in wf
	assert "revalidate_events.v1.json" in wf


def test_nag_reminder_assets_and_judge_wiring_are_present() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")

	assert "nag_reminder.sh" in wf
	assert 'nag_prompt_src=".codex-workflow-src/prompts/_nag_reminders.txt"' in wf
	assert 'install -m 0644 "${nag_prompt_src}" "prompts/_nag_reminders.txt"' in wf
	assert 'Optional nag reminder prompt asset _nag_reminders.txt is unavailable on ${SCRIPT_REF}; nag reminders will fail open for this run.' in wf
	assert "UNATTENDED_NAG_REMINDER_ENABLED: ${{ vars.UNATTENDED_NAG_REMINDER_ENABLED || 'false' }}" in wf
	assert "UNATTENDED_NAG_SILENT_ROUNDS: ${{ vars.UNATTENDED_NAG_SILENT_ROUNDS || '3' }}" in wf
	assert 'source scripts/nag_reminder.sh 2>/dev/null || true' in poller
	assert 'judge_nag_block="$(maybe_inject_nag "orchestrate-poll-judge" "${judge_silent_rounds}")"' in poller
	assert 'judge_json_candidate="$(extract_judge_json_with_status "${JUDGE_OUTPUT_FILE}")"' in poller
	assert 'cleaned = re.sub(r"```(?:json)?\\s*", "", raw)' in poller
	assert 'cleaned = re.sub(r"```\\s*$", "", cleaned, flags=re.MULTILINE)' in poller
	assert 'cleaned = re.sub(r"```(?:json)?\\\\s*", "", raw)' not in poller
	assert 'cleaned = re.sub(r"```\\\\s*$", "", cleaned, flags=re.MULTILINE)' not in poller


def main() -> int:
	test_stall_control_env_defaults_are_declared()
	test_stall_recovery_prompt_is_bootstrapped_with_main_fallback()
	test_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_orchestrate_workflow_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_nag_reminder_assets_and_judge_wiring_are_present()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

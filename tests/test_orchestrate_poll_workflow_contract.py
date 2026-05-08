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
	assert "JUDGE_REPEAT_FINGERPRINT_MAX: ${{ vars.JUDGE_REPEAT_FINGERPRINT_MAX || '2' }}" in wf


def test_stall_recovery_prompt_is_bootstrapped_with_main_fallback() -> None:
	wf = _workflow()
	assert "for pf in mode-judge.txt mode-judge-review-blocked.txt mode-judge-stall-recovery.txt; do" in wf
	assert "src=\".codex-workflow-src/prompts/${pf}\"" in wf
	assert "if [ ! -f \"${src}\" ] && [ -f \".codex-workflow-src-main/prompts/${pf}\" ]; then" in wf
	assert "src=\".codex-workflow-src-main/prompts/${pf}\"" in wf
	assert "::error::Missing required support file prompts/${pf}" in wf
	assert "install -m 0644 \"${src}\" \"prompts/${pf}\"" in wf


def test_semble_foundation_is_staged_and_fail_open() -> None:
	wf = _workflow()
	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}" in wf
	assert 'SEMBLE_AVAILABLE: "false"' in wf
	assert 'SEMBLE_INDEX_AVAILABLE: "false"' in wf
	assert "install_semble.sh semble_helpers.sh" in wf
	assert "- name: Setup uv for Semble" in wf
	assert "uses: astral-sh/setup-uv@v3" in wf
	assert "- name: Install Semble" in wf
	assert "- name: Install Semble\n        if: env.SEMBLE_ENABLED == 'true' && steps.find_tracking.outputs.has_work == 'true'\n        continue-on-error: true" in wf
	assert "bash scripts/install_semble.sh" in wf
	assert "- name: Build Semble index" in wf
	assert "- name: Build Semble index\n        if: env.SEMBLE_ENABLED == 'true' && steps.find_tracking.outputs.has_work == 'true'\n        continue-on-error: true" in wf
	assert 'workspace_root="${GITHUB_WORKSPACE:-}"' in wf
	assert 'SEMBLE_INDEX_DIR="${RUNTIME_DIR}/.semble-index"' in wf
	assert "SEMBLE_FALLBACK target=index reason=workspace_unavailable" in wf
	assert 'semble index . --out "${SEMBLE_INDEX_DIR}"' in wf
	assert 'printf \'%s\\n\' "${workspace_root}" > "${SEMBLE_INDEX_DIR}/repo_root"' in wf
	assert 'echo "SEMBLE_INDEX_DIR=${SEMBLE_INDEX_DIR}" >> "$GITHUB_ENV"' in wf
	assert "SEMBLE_INDEX target=orchestrate_poll path=${SEMBLE_INDEX_DIR}" in wf


def main() -> int:
	test_stall_control_env_defaults_are_declared()
	test_stall_recovery_prompt_is_bootstrapped_with_main_fallback()
	test_semble_foundation_is_staged_and_fail_open()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

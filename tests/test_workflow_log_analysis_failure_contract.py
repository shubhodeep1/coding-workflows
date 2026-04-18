#!/usr/bin/env python3
"""Static contract tests for workflow-log-analysis Codex failure handling."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WF_PATH = REPO_ROOT / ".github" / "workflows" / "workflow-log-analysis.yml"


def _workflow_text() -> str:
	return WF_PATH.read_text(encoding="utf-8")


def test_codex_retry_knobs_are_env_driven() -> None:
	wf = _workflow_text()
	assert "MAX_CODEX_ATTEMPTS: ${{ vars.MAX_CODEX_ATTEMPTS || '3' }}" in wf
	assert "CODEX_RETRY_BACKOFF_BASE_SECS: ${{ vars.CODEX_RETRY_BACKOFF_BASE_SECS || '10' }}" in wf
	assert "max_attempts=3" not in wf
	assert "max_attempts=\"3\"" in wf
	assert "sleep_secs=$((10 * (2 ** (attempt - 1))))" not in wf
	assert "sleep_secs=$((CODEX_RETRY_BACKOFF_BASE_SECS * (2 ** (attempt - 1))))" in wf
	assert "sleep_secs=$((backoff_base * (2 ** (attempt - 1))))" in wf


def test_issue_context_failure_marker_and_label_contract_present() -> None:
	wf = _workflow_text()
	assert "tracking_issue:" in wf
	assert "TRACKING_ISSUE: ${{ inputs.tracking_issue || '0' }}" in wf
	assert "emit_log_analysis_phase_failure()" in wf
	assert "AI_PHASE_FAILURE_V1" in wf
	assert "--add-label \"ai:log-analysis-failed\"" in wf
	assert "retrigger_log_analysis" in wf
	assert "without tracking issue context" in wf


def main() -> int:
	test_codex_retry_knobs_are_env_driven()
	test_issue_context_failure_marker_and_label_contract_present()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

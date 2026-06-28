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


def test_codex_jobs_use_heartbeat_wrapper() -> None:
	wf = _workflow_text()
	assert wf.count("bash scripts/codex_heartbeat.sh") == 4
	assert "--phase workflow_log_analysis" in wf
	assert "--phase workflow_deep_audit" in wf
	assert "--phase workflow_api_redundancy" in wf
	assert "--phase workflow_weekly_retro" in wf
	assert "2> >(tee -a /tmp/workflow-analysis-codex.log >&2)" in wf
	assert "2> >(tee -a /tmp/workflow-audit-codex.log >&2)" in wf
	assert "2> >(tee -a /tmp/workflow-api-redundancy-codex.log >&2)" in wf
	assert "2> >(tee -a /tmp/workflow-weekly-retro-codex.log >&2)" in wf


def test_weekly_retro_path_is_schedule_gated_and_default_off() -> None:
	wf = _workflow_text()
	assert "schedule:" in wf
	assert 'cron: "0 9 * * 1"' in wf
	assert "WORKFLOW_RETRO_ENABLED: ${{ vars.WORKFLOW_RETRO_ENABLED || 'false' }}" in wf
	assert "WORKFLOW_RETRO_MODEL: ${{ vars.WORKFLOW_RETRO_MODEL || 'openai/gpt-5.4-mini' }}" in wf
	assert "WORKFLOW_RETRO_REASONING: ${{ vars.WORKFLOW_RETRO_REASONING || 'medium' }}" in wf
	assert "WORKFLOW_RETRO_CRON: ${{ vars.WORKFLOW_RETRO_CRON || '0 9 * * 1' }}" in wf
	assert "github.event.schedule == (vars.WORKFLOW_RETRO_CRON || '0 9 * * 1')" in wf
	assert "(vars.WORKFLOW_RETRO_ENABLED || 'false') == 'true'" in wf
	assert "github.event_name != 'schedule' || ((vars.WORKFLOW_RETRO_ENABLED || 'false') == 'true' && github.event.schedule == (vars.WORKFLOW_RETRO_CRON || '0 9 * * 1'))" in wf
	assert "github.event_name == 'schedule' && (vars.WORKFLOW_RETRO_ENABLED || 'false') == 'true' && github.event.schedule == (vars.WORKFLOW_RETRO_CRON || '0 9 * * 1')" in wf
	assert '--json number,title,body,state,updatedAt,url > "${TRACKER_CANDIDATES_JSON}"' in wf
	assert "selected_candidates.sort(" in wf
	assert 'str(candidate.get("state") or "").upper() == "OPEN"' in wf
	assert 'str(candidate.get("updatedAt") or "")' in wf


def test_semble_wiring_is_consistent_across_four_codex_jobs() -> None:
	# workflow-log-analysis.yml uses a deliberately different Semble
	# integration pattern from the parity workflows (RUNNER_TEMP instead of
	# RUNTIME_DIR, no shared SUPPORT_SCRIPTS_DIR, embedded prefetch via
	# `head -c 6000 | semble_query_block` rather than a query-helper file).
	# tests/test_semble_workflow_parity_contract.py's TARGET_WORKFLOWS
	# list intentionally omits this workflow because its REQUIRED_SNIPPETS
	# wouldn't fit. This test is the dedicated coverage that catches
	# regressions in the workflow-log-analysis-style Semble wiring.
	wf = _workflow_text()

	# Workflow-level enablement: defaults to true via repo var.
	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'true' }}" in wf

	# Each of the 4 Codex jobs runs Install semble + Build semble index
	# (parity tests confirm the steps' name uniqueness, so a simple count
	# is a valid contract check).
	assert wf.count("- name: Install semble") == 4, \
		"workflow-log-analysis must keep an Install semble step in each of its 4 Codex jobs"
	assert wf.count("- name: Build semble index") == 4, \
		"workflow-log-analysis must keep a Build semble index step in each of its 4 Codex jobs"

	# Defense-in-depth: both step types use the shared-script call AND
	# continue-on-error: true (added per branch-review consensus).
	assert wf.count("bash scripts/install_semble.sh") == 4
	assert wf.count("bash scripts/build_semble_wrapper.sh") == 4
	# `continue-on-error: true` appears on more than just the Semble steps,
	# so check the script-call neighborhood is gated by it. Build semble
	# index pins SEMBLE_INDEX_PATH explicitly to runner.temp so self-hosted
	# runners without RUNNER_TEMP don't fall back to ${PWD}/.semble-index.
	assert wf.count("SEMBLE_INDEX_PATH: ${{ runner.temp }}/.semble-index") == 4

	# Fail-soft script-presence guard around the wrapper call (callers
	# pinned to an older reusable workflow ref may not yet have the script).
	assert wf.count("if [ -f scripts/build_semble_wrapper.sh ]; then") == 4
	assert wf.count("scripts/build_semble_wrapper.sh not present") == 4

	# Prefetch wiring: each job sources semble_helpers.sh, builds a query
	# from the analysis/report file, calls semble_query_block, and pipes
	# query bytes through iconv -c so a multi-byte split at byte 6000
	# doesn't garble the BM25 query. Each job has TWO references to
	# semble_query_block (one `type ...` gate plus one invocation).
	assert wf.count("source scripts/semble_helpers.sh || true") == 4
	assert wf.count("if type semble_query_block >/dev/null 2>&1; then") == 4
	assert wf.count("SEMBLE_PREFETCH=\"$(semble_query_block") == 4
	assert wf.count("iconv -f UTF-8 -t UTF-8 -c") == 4
	# {{SEMBLE_PREFETCH}} placeholder is substituted via sed/shell-var
	# before invoking Codex; it should never appear literally in the
	# workflow (it lives in prompts/mode-workflow-*.txt instead).
	assert wf.count("{{SEMBLE_PREFETCH}}") == 0


def main() -> int:
	test_codex_retry_knobs_are_env_driven()
	test_issue_context_failure_marker_and_label_contract_present()
	test_codex_jobs_use_heartbeat_wrapper()
	test_weekly_retro_path_is_schedule_gated_and_default_off()
	test_semble_wiring_is_consistent_across_four_codex_jobs()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

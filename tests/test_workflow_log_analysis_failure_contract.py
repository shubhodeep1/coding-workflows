#!/usr/bin/env python3
"""Static contract tests for workflow-log-analysis Codex failure handling."""

from __future__ import annotations

import re
import os
import subprocess
import tempfile
from pathlib import Path

import yaml


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


def test_all_analysis_agents_are_isolated_from_publishers() -> None:
	wf = _workflow_text()
	assert "--sandbox danger-full-access" not in wf
	assert "bash scripts/codex_heartbeat.sh" not in wf
	for job, role in (
		("weekly-retro", "WORKFLOW_WEEKLY_RETRO"),
		("analyze-commit-notify", "WORKFLOW_LOG_ANALYSIS"),
		("deep-audit", "WORKFLOW_DEEP_AUDIT"),
		("api-redundancy", "WORKFLOW_API_REDUNDANCY"),
	):
		model = wf.split(f"  {job}-model:\n", 1)[1].split(f"  {job}:\n", 1)[0]
		publisher = re.split(r"\n  [a-z-]+:\n", wf.split(f"  {job}:\n", 1)[1], maxsplit=1)[0]
		assert "contents: read" in model and "actions: read" in model
		assert "persist-credentials: false" in model
		assert f"codex {role}" in model
		assert "gh issue comment" not in model
		assert "secrets.GH_PAT" not in model
		assert "secrets.TG_BOT_SECRET" not in model
		assert "git push " not in model
		assert "git commit " not in model
		assert "bash scripts/clarify_isolated_run.sh" not in publisher
		assert "Missing isolated model result" in publisher
		assert "Invalid isolated model result size or type" in publisher
		assert "Missing or misordered model result heading" in publisher
		assert "if ! validate_result; then" in publisher
	assert "bash scripts/workflow_retro_fanout.sh generate" in wf
	assert "bash scripts/workflow_retro_fanout.sh publish" in wf


def test_publisher_rejects_missing_oversized_malformed_and_traversal_results() -> None:
	jobs = yaml.safe_load(_workflow_text())["jobs"]
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		result = root / "workflow-model-result.md"
		report_headings = (
			"Executive Summary", "Speed Optimizations", "Cost Optimizations",
			"Reliability Improvements", "AI Memory Health", "GH API Call Audit",
			"Prompt Cache & Memory System", "Orchestrator Health",
			"Pipeline Flow Bottlenecks", "Per-Repo Breakdown", "Metrics Appendix",
		)
		for kind, body in (
			("retro", "\n".join(("## Weekly Retro", "### What Worked", "### Failure Modes", "### Next Week Recommendation", "### Metrics Snapshot"))),
			("analysis", "\n".join(f"## {heading}" for heading in report_headings)),
			("audit", "\n".join(("## Deep Audit — Workflows & Scripts (2026-10-04)", "### Section 1: Bug & Correctness Sweep", "### Section 2: GitHub API Call Redundancy Audit", "### Section 3: Code Duplication & Modularization Opportunities", "### Section 4: Expression Size Limit Risk Assessment", "### Section 5: Cross-Cutting Concerns", "### Section 6: Summary & Severity Matrix", "#### 6A. Findings Summary Table", "#### 6B. Estimated Remediation Scope"))),
			("api", "\n".join(("## API Call Consolidation & Dead-Call Analysis (2026-10-04)", "### Safety Tag Legend", "### Consolidation Candidates (MERGE-###)", "### Redundant Re-Fetch (REUSE-###)", "### Dead Calls (DEAD-API-###)", "### Cross-References to Deep Audit Section", "### Summary Counts", "### Implement-Stage Handoff"))),
		):
			job = {"retro": "weekly-retro", "analysis": "analyze-commit-notify", "audit": "deep-audit", "api": "api-redundancy"}[kind]
			publisher = next(step["run"] for step in jobs[job]["steps"] if step.get("id") == {"retro": "retro", "analysis": "analyze", "audit": "audit", "api": "api_redundancy"}[kind])
			# Execute the exact publisher validator, without publisher write credentials.
			validator = "validate_result() {\n" + publisher.split("validate_result() {\n", 1)[1].split("}\nif ! validate_result; then", 1)[0] + "}\nvalidate_result\n"
			env = os.environ.copy()
			for pinned_key in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE"):
				env.pop(pinned_key, None)
			env.update(MODEL_RESULT=str(result), MODEL_KIND=kind, REPORT_FILE="analysis/workflow-optimization-2026-10-04.md" if kind != "retro" else "")
			def run_validator() -> subprocess.CompletedProcess[str]:
				return subprocess.run(["bash", "-c", validator], env=env, capture_output=True, text=True)

			result.unlink(missing_ok=True)
			assert run_validator().returncode != 0, kind
			result.write_text(body, encoding="utf-8")
			assert run_validator().returncode == 0, (kind, run_validator().stderr)
			result.write_text(body.replace("### What Worked", "### Wrong") if kind == "retro" else "## Wrong\n", encoding="utf-8")
			assert run_validator().returncode != 0, kind
			result.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
			assert run_validator().returncode != 0, kind
			result.unlink()
			result.symlink_to(root / "missing")
			assert run_validator().returncode != 0, kind
			result.unlink()
			if kind != "retro":
				result.write_text(body, encoding="utf-8")
				env["REPORT_FILE"] = "analysis/../scripts/gh_helpers.sh"
				assert run_validator().returncode != 0, kind


def test_weekly_retro_path_is_schedule_gated_and_default_on() -> None:
	wf = _workflow_text()
	assert "schedule:" in wf
	assert 'cron: "0 9 * * 1"' in wf
	assert "WORKFLOW_RETRO_ENABLED: ${{ vars.WORKFLOW_RETRO_ENABLED || 'true' }}" in wf
	assert "WORKFLOW_RETRO_MODEL: ${{ vars.WORKFLOW_RETRO_MODEL || 'openai/gpt-6-luna' }}" in wf
	assert "WORKFLOW_RETRO_REASONING: ${{ vars.WORKFLOW_RETRO_REASONING || 'medium' }}" in wf
	assert "WORKFLOW_RETRO_CRON: ${{ vars.WORKFLOW_RETRO_CRON || '0 9 * * 1' }}" in wf
	assert "WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY: ${{ vars.WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY || 'true' }}" in wf
	assert "github.event.schedule == (vars.WORKFLOW_RETRO_CRON || '0 9 * * 1')" in wf
	assert "(vars.WORKFLOW_RETRO_ENABLED || 'true') == 'true'" in wf
	assert "github.event_name != 'schedule' || ((vars.WORKFLOW_RETRO_ENABLED || 'true') == 'true' && github.event.schedule == (vars.WORKFLOW_RETRO_CRON || '0 9 * * 1'))" in wf
	assert "github.event_name == 'schedule' && (vars.WORKFLOW_RETRO_ENABLED || 'true') == 'true' && github.event.schedule == (vars.WORKFLOW_RETRO_CRON || '0 9 * * 1')" in wf
	assert "retro_gate=skip_no_activity" in wf
	assert "retro_gate=run" in wf
	assert "retro_skip_if_no_activity_normalized=\"$(printf '%s' \"${WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY:-true}\" | tr '[:upper:]' '[:lower:]')\"" in wf
	assert "[[ \"${retro_skip_if_no_activity_normalized}\" =~ ^(1|true|yes|on)$ ]]" in wf
	assert "if: steps.retro_context.outputs.retro_gate == 'run'" in wf
	assert "if: needs.weekly-retro-model.outputs.retro_gate == 'run'" in wf
	assert "WORKFLOW_RETRO_SKIP_V1:" in wf
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in wf
	assert 'REPO_REGISTRY_PATH=".github/ai/consumer_repos.json"' in wf
	assert 'if [ -n "${OVERRIDE_INPUT//[[:space:]]/}" ]; then' in wf
	assert 'if [ "${GITHUB_EVENT_NAME}" = "schedule" ]; then' not in wf
	assert '--json number,title,body,state,updatedAt,url > "${TRACKER_CANDIDATES_JSON}"' in wf
	assert "selected_candidates.sort(" in wf
	assert 'str(candidate.get("state") or "").upper() == "OPEN"' in wf
	assert 'str(candidate.get("updatedAt") or "")' in wf


def test_scenario_trace_renderer_is_flag_gated_and_local_only() -> None:
	wf = _workflow_text()
	assert "WORKFLOW_LOG_SCENARIO_TRACE_ENABLED: ${{ vars.WORKFLOW_LOG_SCENARIO_TRACE_ENABLED || 'false' }}" in wf
	assert "- name: Render workflow scenario traces" in wf
	assert "if: steps.analyze.outputs.analysis_status == 'completed' && env.WORKFLOW_LOG_SCENARIO_TRACE_ENABLED == 'true'" in wf
	assert 'TRACE_OUTPUT_DIR=".ai/workflow_traces"' in wf
	assert 'python3 scripts/render_scenario_trace.py \\' in wf
	assert '--input workflow_log_report.json \\' in wf
	assert '--output-dir "${TRACE_OUTPUT_DIR}"' in wf
	assert "WORKFLOW_SCENARIO_TRACE_WRITTEN" in wf
	assert "WORKFLOW_SCENARIO_TRACE_PARSE_FAIL" in wf


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
	test_all_analysis_agents_are_isolated_from_publishers()
	test_weekly_retro_path_is_schedule_gated_and_default_on()
	test_scenario_trace_renderer_is_flag_gated_and_local_only()
	test_semble_wiring_is_consistent_across_four_codex_jobs()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

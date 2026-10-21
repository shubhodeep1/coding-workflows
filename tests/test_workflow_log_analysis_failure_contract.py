#!/usr/bin/env python3
"""Static contract tests for workflow-log-analysis Codex failure handling."""

from __future__ import annotations

import os
import subprocess
import tempfile
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


def test_analyze_mounts_bounded_log_bundle_or_falls_back_to_context() -> None:
	wf = _workflow_text()
	analyze = wf.split("      - name: Run workflow log analysis\n", 1)[1].split("\n      - name: ", 1)[0]
	assert 'RUN_LOGS_DIR="$(realpath -e -- "${RUN_LOGS_DIR}" 2>/dev/null)"' in analyze
	assert 'python3 -I -B scripts/stage_workflow_log_bundle.py "${RUN_LOGS_DIR}"' in analyze
	assert 'wla_run_logs_include_args=(--include "${RUN_LOGS_DIR}")' in analyze
	assert 'bash scripts/codex_isolated_exec.sh run --mode read-only ${wla_run_logs_include_args[@]+"${wla_run_logs_include_args[@]}"} --' in analyze
	assert 'RUN_LOGS_DIR=""' in analyze
	assert 'BOUNDED_SUBSET.txt' in analyze


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
	assert "github.event_name != 'schedule' || github.event.schedule == '0 6 * * *' || ((vars.WORKFLOW_RETRO_ENABLED || 'true') == 'true' && github.event.schedule == (vars.WORKFLOW_RETRO_CRON || '0 9 * * 1'))" in wf
	assert "github.event_name == 'schedule' && (vars.WORKFLOW_RETRO_ENABLED || 'true') == 'true' && github.event.schedule == (vars.WORKFLOW_RETRO_CRON || '0 9 * * 1')" in wf
	assert "retro_gate=skip_no_activity" in wf
	assert "retro_gate=run" in wf
	assert "retro_skip_if_no_activity_normalized=\"$(printf '%s' \"${WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY:-true}\" | tr '[:upper:]' '[:lower:]')\"" in wf
	assert "[[ \"${retro_skip_if_no_activity_normalized}\" =~ ^(1|true|yes|on)$ ]]" in wf
	assert "if: steps.retro_context.outputs.retro_gate == 'run'" in wf
	assert "WORKFLOW_RETRO_SKIP_V1:" in wf
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in wf
	assert 'REPO_REGISTRY_PATH=".github/ai/consumer_repos.json"' in wf
	assert 'if [ "${PAT_BUDGET_DAILY}" = "true" ]; then' in wf
	assert 'elif [ -n "${OVERRIDE_INPUT//[[:space:]]/}" ]; then' in wf
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


def test_failure_label_writes_route_through_gh_retry_with_fallback() -> None:
	wf = _workflow_text()
	helper_def = '_wla_label_retry() { if type gh_retry >/dev/null 2>&1; then gh_retry "$@"; else "$@"; fi; }'
	create_def = (
		'_wla_label_create() { local _wla_err; _wla_err=$(gh label create "$@" 2>&1 >/dev/null) && return 0; '
		'case "${_wla_err}" in *"already exists"*) return 0 ;; esac; _wla_label_retry gh label create "$@"; }'
	)
	for step_name in (
		"Run workflow log analysis",
		"Run deep audit pass",
		"Run API redundancy pass",
	):
		body = wf.split(f"      - name: {step_name}\n", 1)[1].split("\n      - name: ", 1)[0]
		assert body.count(helper_def) == 1, step_name
		assert body.count(create_def) == 1, step_name
		assert '_wla_label_create "ai:log-analysis-failed"' in body, step_name
		assert 'GH_RETRY_IDEMPOTENT=true _wla_label_retry gh issue edit "${TRACKING_ISSUE_NUM}"' in body, step_name
		for line in body.splitlines():
			stripped = line.lstrip()
			assert not stripped.startswith('gh label create "ai:log-analysis-failed"'), step_name
			assert not stripped.startswith('gh issue edit "${TRACKING_ISSUE_NUM}"'), step_name
			if "_wla_label_retry" in stripped:
				assert "AI_PHASE_FAILURE_V1" not in stripped, step_name
		# The per-run AI_PHASE_FAILURE_V1 comment post stays a single attempt.
		assert "_wla_label_retry gh api" not in body, step_name
		assert "AI_PHASE_FAILURE_V1" in body, step_name


def _run_label_create_helper(tmp: str, gh_stderr: str) -> tuple[int, int]:
	wf = _workflow_text()
	defs = [
		line.strip()
		for line in wf.splitlines()
		if line.strip().startswith(("_wla_label_retry() {", "_wla_label_create() {"))
	][:2]
	assert len(defs) == 2
	calls = os.path.join(tmp, "calls")
	open(calls, "w", encoding="utf-8").close()
	script = "\n".join(
		[
			"gh() { echo call >> \"$CALLS\"; printf '%s\\n' \"$GH_STDERR\" >&2; return 1; }",
			"gh_retry() { local i; for i in 1 2 3 4 5; do \"$@\" && return 0; done; return 1; }",
			*defs,
			'_wla_label_create "ai:log-analysis-failed" --repo o/r',
		]
	)
	env = dict(os.environ, CALLS=calls, GH_STDERR=gh_stderr)
	proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
	with open(calls, encoding="utf-8") as fh:
		count = len(fh.read().splitlines())
	return proc.returncode, count


def test_label_create_helper_does_not_retry_an_existing_label() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		rc, calls = _run_label_create_helper(
			tmp, 'label with name "ai:log-analysis-failed" already exists; use `--force` to update'
		)
	assert rc == 0
	assert calls == 1
	with tempfile.TemporaryDirectory() as tmp:
		rc, calls = _run_label_create_helper(tmp, "HTTP 502: Bad Gateway")
	assert rc != 0
	assert calls == 6



GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"


def _run_label_helpers_with_real_gh_retry(tmp: str, command: str, gh_stderr: str, fail_first: int) -> tuple[int, int]:
	"""Run the workflow's label helpers against the real scripts/gh_helpers.sh
	gh_retry (sourced the way the workflow step sources it), with a stub gh
	that fails the first ``fail_first`` calls with ``gh_stderr`` and then
	succeeds, and sleep stubbed out so retries cost no wall time."""
	wf = _workflow_text()
	defs = [
		line.strip()
		for line in wf.splitlines()
		if line.strip().startswith(("_wla_label_retry() {", "_wla_label_create() {"))
	][:2]
	assert len(defs) == 2
	calls = os.path.join(tmp, "calls")
	open(calls, "w", encoding="utf-8").close()
	script = "\n".join(
		[
			f'source "{GH_HELPERS}"',
			"type gh_retry >/dev/null 2>&1 || { echo 'gh_retry missing' >&2; exit 97; }",
			"sleep() { :; }",
			'gh() { echo call >> "$CALLS"; n=$(wc -l < "$CALLS"); '
			'if [ "$n" -le "$FAIL_FIRST" ]; then printf \'%s\\n\' "$GH_STDERR" >&2; return 1; fi; return 0; }',
			*defs,
			command,
		]
	)
	env = dict(
		os.environ,
		CALLS=calls,
		GH_STDERR=gh_stderr,
		FAIL_FIRST=str(fail_first),
		GH_RETRY_MAX_ATTEMPTS="3",
		TMPDIR=tmp,
	)
	proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
	assert proc.returncode != 97, proc.stderr
	with open(calls, encoding="utf-8") as fh:
		count = len(fh.read().splitlines())
	return proc.returncode, count


def test_label_helpers_retry_through_real_gh_retry() -> None:
	create = '_wla_label_create "ai:log-analysis-failed" --repo o/r'
	add = 'GH_RETRY_IDEMPOTENT=true _wla_label_retry gh issue edit 5 --repo o/r --add-label "ai:log-analysis-failed"'
	with tempfile.TemporaryDirectory() as tmp:
		# "already exists" is success on the first raw attempt; no retry.
		rc, calls = _run_label_helpers_with_real_gh_retry(
			tmp, create, 'label with name "ai:log-analysis-failed" already exists; use `--force` to update', 99
		)
	assert (rc, calls) == (0, 1)
	with tempfile.TemporaryDirectory() as tmp:
		# A transient 502 on the raw attempt and the first retry recovers.
		rc, calls = _run_label_helpers_with_real_gh_retry(tmp, create, "HTTP 502: Bad Gateway", 2)
	assert (rc, calls) == (0, 3)
	with tempfile.TemporaryDirectory() as tmp:
		# A persistent 502: one raw attempt plus GH_RETRY_MAX_ATTEMPTS retries.
		rc, calls = _run_label_helpers_with_real_gh_retry(tmp, create, "HTTP 502: Bad Gateway", 99)
	assert rc != 0
	assert calls == 4
	with tempfile.TemporaryDirectory() as tmp:
		# The idempotent label add is retried up to GH_RETRY_MAX_ATTEMPTS.
		rc, calls = _run_label_helpers_with_real_gh_retry(tmp, add, "HTTP 502: Bad Gateway", 99)
	assert rc != 0
	assert calls == 3
	with tempfile.TemporaryDirectory() as tmp:
		rc, calls = _run_label_helpers_with_real_gh_retry(tmp, add, "HTTP 502: Bad Gateway", 1)
	assert (rc, calls) == (0, 2)

def main() -> int:
	test_codex_retry_knobs_are_env_driven()
	test_issue_context_failure_marker_and_label_contract_present()
	test_codex_jobs_use_heartbeat_wrapper()
	test_analyze_mounts_bounded_log_bundle_or_falls_back_to_context()
	test_weekly_retro_path_is_schedule_gated_and_default_on()
	test_scenario_trace_renderer_is_flag_gated_and_local_only()
	test_semble_wiring_is_consistent_across_four_codex_jobs()
	test_failure_label_writes_route_through_gh_retry_with_fallback()
	test_label_create_helper_does_not_retry_an_existing_label()
	test_label_helpers_retry_through_real_gh_retry()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

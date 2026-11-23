#!/usr/bin/env python3
"""Static contract tests for workflow-log-analysis Codex failure handling."""

from __future__ import annotations

import importlib.util
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


def _analyze_step_body(wf: str) -> str:
	return wf.split("      - name: Run workflow log analysis\n", 1)[1].split("\n      - name: ", 1)[0]


def test_analyze_runs_report_structure_check_with_kill_switch() -> None:
	wf = _workflow_text()
	analyze = _analyze_step_body(wf)
	assert "WORKFLOW_LOG_ANALYSIS_REPORT_CHECK_ENABLED: ${{ vars.WORKFLOW_LOG_ANALYSIS_REPORT_CHECK_ENABLED || 'true' }}" in analyze
	assert "${WORKFLOW_LOG_ANALYSIS_REPORT_CHECK_ENABLED:-true}" in analyze
	assert "false|0|no|off) wla_report_check_enabled=\"false\" ;;" in analyze
	assert "[ ! -f scripts/check_workflow_analysis_report.py ]" in analyze
	assert (
		'python3 -I -B scripts/check_workflow_analysis_report.py --report "${REPORT_FILE}" '
		"--collect-report workflow_log_report.json"
	) in analyze
	assert 'codex_failure_mode="report_structure_invalid"' in analyze
	assert 'elif [ "${wla_report_check_rc}" -eq 1 ]; then' in analyze
	# The existing failure modes keep their exact assignments.
	assert 'codex_failure_mode="codex_rc_nonzero"' in analyze
	assert 'codex_failure_mode="codex_empty_output"' in analyze
	assert (
		'handle_log_analysis_codex_failure "analysis_codex_pass" "report_structure_invalid" '
		'"${analyze_attempts_used}" 0 "${failure_summary}"'
	) in analyze
	assert 'echo "held_report_file=${REPORT_FILE}" >> "$GITHUB_OUTPUT"' in analyze
	# The held report gets the data-gap line before the handler exits.
	held_branch = analyze.split('if [ "${codex_failure_mode}" = "report_structure_invalid" ]; then', 1)[1]
	assert held_branch.index("--insert-data-gap") < held_branch.index("handle_log_analysis_codex_failure")
	# The held report is never marked completed: the handler exits first.
	invalid_branch = analyze.split('if [ "${codex_failure_mode}" = "report_structure_invalid" ]; then', 1)[1]
	assert invalid_branch.index("handle_log_analysis_codex_failure") < invalid_branch.index("analysis_status=completed")
	assert 'wla_logs_missing="false"' in analyze
	assert 'wla_logs_missing="true"' in analyze
	assert 'echo "WORKFLOW_LOG_ANALYSIS_COVERAGE logs=missing"' in analyze
	assert 'python3 -I -B scripts/check_workflow_analysis_report.py --insert-data-gap --report "${REPORT_FILE}"' in analyze
	assert analyze.index("--insert-data-gap") < analyze.index('echo "report_file=${REPORT_FILE}"')
	# ${{ }} never reaches the run: body for the new variable.
	run_body = analyze.split("run: |", 1)[1]
	assert "${{ vars.WORKFLOW_LOG_ANALYSIS_REPORT_CHECK_ENABLED" not in run_body


def test_analyze_report_artifact_upload_is_always_gated() -> None:
	wf = _workflow_text()
	step = wf.split("      - name: Upload workflow log analysis report\n", 1)[1].split("\n      - name: ", 1)[0]
	assert "if: always() && (steps.analyze.outputs.report_file != '' || steps.analyze.outputs.held_report_file != '')" in step
	assert "continue-on-error: true" in step
	assert "uses: actions/upload-artifact@v6" in step
	assert "name: workflow-log-analysis-report" in step
	assert "path: ${{ steps.analyze.outputs.report_file || steps.analyze.outputs.held_report_file }}" in step
	assert "if-no-files-found: warn" in step
	assert "retention-days: 30" in step
	assert "run:" not in step
	# Placed right after the analyze step and before the trace renderer.
	assert wf.index("      - name: Run workflow log analysis\n") < wf.index("      - name: Upload workflow log analysis report\n")
	assert wf.index("      - name: Upload workflow log analysis report\n") < wf.index("      - name: Render workflow scenario traces\n")


# The report-delivery-via-PR cases live in
# tests/test_workflow_log_analysis_report_pr.py and are collected here, since
# CI already runs this file and ci.yml was outside that issue's file grant. A
# name clash would hide a test, so it fails loudly.
_REPORT_PR_CASE_NAMES: list[str] = []


_COVERAGE_BEGIN = "# --- collection coverage (P3) begin ---"
_COVERAGE_END = "# --- collection coverage (P3) end ---"
_COVERAGE_PROMPT_BULLET = (
	"- If `coverage` in `analysis_context.json` lists any `repositories_failed` or "
	"`repositories_truncated`, name each such repository in `## Executive Summary` as a "
	"data gap (failed: not analysed; truncated: only part of its runs listed)."
)


def _collect_logs_step(name: str) -> str:
	wf = _workflow_text()
	job = wf.split("\n  collect-logs:\n", 1)[1].split("\n  weekly-retro:\n", 1)[0]
	return job.split(f"      - name: {name}\n", 1)[1].split("\n      - name: ", 1)[0]


def _run_coverage_block(tmp: str, report: dict | None) -> tuple[int, str, str]:
	import json
	import shutil
	import textwrap

	step = _collect_logs_step("Collect workflow logs")
	block = step.split(_COVERAGE_BEGIN, 1)[1].split(_COVERAGE_END, 1)[0]
	script = "set -euo pipefail\n" + textwrap.dedent(block)
	work = Path(tmp)
	for leftover in ("workflow_log_report.json", "out.txt"):
		(work / leftover).unlink(missing_ok=True)
	if report is not None:
		(work / "workflow_log_report.json").write_text(json.dumps(report), encoding="utf-8")
	output_file = work / "out.txt"
	output_file.write_text("", encoding="utf-8")
	env = dict(os.environ)
	env["GITHUB_OUTPUT"] = str(output_file)
	proc = subprocess.run(
		[shutil.which("bash") or "bash", "-c", script],
		cwd=tmp,
		env=env,
		capture_output=True,
		text=True,
		timeout=60,
	)
	return proc.returncode, proc.stdout, output_file.read_text(encoding="utf-8")


def test_collect_step_logs_coverage_line_and_annotations() -> None:
	import shutil

	step = _collect_logs_step("Collect workflow logs")
	assert "        id: collect\n" in step
	assert 'collect_rc=0; python3 scripts/collect_workflow_logs.py "${COLLECT_ARGS[@]}" || collect_rc=$?' in step
	assert (
		"WORKFLOW_LOG_COLLECT_COVERAGE repos=${coverage_repos} failed=${#coverage_failed_list[@]} "
		"truncated=${#coverage_truncated_list[@]} failed_repos=${coverage_failed_csv} "
		"truncated_repos=${coverage_truncated_csv}"
	) in step
	assert "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" in step
	assert step.rstrip().endswith('exit "${collect_rc}"')
	assert step.index(_COVERAGE_BEGIN) < step.index(_COVERAGE_END) < step.index('exit "${collect_rc}"')
	block = step.split(_COVERAGE_BEGIN, 1)[1].split(_COVERAGE_END, 1)[0]
	assert "${{" not in block
	if shutil.which("jq") is None or shutil.which("bash") is None:
		print("SKIP: jq or bash unavailable; coverage block not executed")
		return

	secret_text = "HTTP 403 Forbidden secret-token-xyz"
	with tempfile.TemporaryDirectory() as tmp:
		rc, out, outputs = _run_coverage_block(
			tmp,
			{
				"coverage": {
					"repositories_requested": ["o/a", "o/b", "o/c", "a/b\n::error::x"],
					"repositories_failed": [
						{"repository": "o/b", "message": secret_text},
						{"repository": "a/b\n::error::x", "message": secret_text},
					],
					"repositories_truncated": ["o/c", "o/a", "bad name"],
					"page_limit_reached": True,
				},
			},
		)
		assert rc == 0, out
		assert "WORKFLOW_LOG_COLLECT_COVERAGE repos=4 failed=1 truncated=2 failed_repos=o/b truncated_repos=o/a,o/c\n" in out
		assert out.count("::error title=Workflow log collection::Could not read workflow runs for o/b;") == 1
		assert out.count("::error") == 1
		assert "::warning title=Workflow log collection::Run listing for o/a was truncated;" in out
		assert "::warning title=Workflow log collection::Run listing for o/c was truncated;" in out
		assert "::error::x" not in out and "bad name" not in out
		assert secret_text not in out and secret_text not in outputs
		assert "coverage_failed_count=1\n" in outputs
		assert "coverage_truncated_count=2\n" in outputs
		assert "coverage_failed_repos=o/b\n" in outputs
		assert "coverage_truncated_repos=o/a,o/c\n" in outputs

		rc, out, outputs = _run_coverage_block(tmp, {"runs": []})
		assert rc == 0
		assert "WORKFLOW_LOG_COLLECT_COVERAGE" not in out
		assert "::warning::workflow_log_report.json has no coverage object; collection coverage is unknown." in out
		assert outputs == ""

		rc, out, outputs = _run_coverage_block(tmp, None)
		assert rc == 0
		assert "collection coverage is unknown" in out
		assert outputs == ""

		rc, out, outputs = _run_coverage_block(
			tmp,
			{"coverage": {"repositories_requested": [], "repositories_failed": [], "repositories_truncated": [], "page_limit_reached": False}},
		)
		assert rc == 0
		assert "WORKFLOW_LOG_COLLECT_COVERAGE repos=0 failed=0 truncated=0 failed_repos= truncated_repos=\n" in out
		assert "::error" not in out and "::warning" not in out
		assert "coverage_failed_count=0\n" in outputs


def test_collection_gap_alert_step_is_warning_and_fail_open() -> None:
	step = _collect_logs_step("Alert on workflow log collection gaps")
	assert "        id: coverage_alert\n" in step
	assert "continue-on-error: true" in step
	if_line = next(line for line in step.splitlines() if line.strip().startswith("if:"))
	assert "always()" in if_line
	assert "(steps.collect.outputs.coverage_failed_count || '0') != '0'" in if_line
	assert "(steps.collect.outputs.coverage_truncated_count || '0') != '0'" in if_line
	assert "!(github.event_name == 'schedule' && github.event.schedule == '0 6 * * *')" in if_line
	assert "TG_BOT_SECRET: ${{ secrets.TG_BOT_SECRET }}" in step
	assert "TG_ADMIN_CHAT_ID: ${{ vars.TG_ADMIN_CHAT_ID }}" in step
	run_body = step.split("        run: |\n", 1)[1]
	assert "${{" not in run_body
	assert "source scripts/tg_helpers.sh" in run_body
	assert 'tg_send_msg "${MSG}" "WARNING" >/dev/null || echo "::warning::' in run_body
	# Ordered after the artifact uploads so it cannot affect them.
	wf = _workflow_text()
	assert wf.index("- name: Upload full workflow log directory artifact") < wf.index(
		"- name: Alert on workflow log collection gaps"
	)
	# GH_PAT and the Telegram secret never share a step.
	collect_step = _collect_logs_step("Collect workflow logs")
	assert "TG_BOT_SECRET" not in collect_step
	assert "GH_PAT" not in step


def test_prompt_names_coverage_gaps_in_executive_summary() -> None:
	for rel in ("prompts/_templates/mode-workflow-analysis.txt", "prompts/mode-workflow-analysis.txt"):
		text = (REPO_ROOT / rel).read_text(encoding="utf-8")
		assert text.count(_COVERAGE_PROMPT_BULLET + "\n") == 1, rel
		anchor = "- Do not invent data. If evidence is incomplete, state the exact gap"
		assert text.index(anchor) < text.index(_COVERAGE_PROMPT_BULLET), rel


def _collect_report_pr_cases() -> None:
	spec = importlib.util.spec_from_file_location(
		"workflow_log_analysis_report_pr_cases",
		Path(__file__).resolve().parent / "test_workflow_log_analysis_report_pr.py",
	)
	assert spec is not None and spec.loader is not None, "cannot load tests/test_workflow_log_analysis_report_pr.py"
	cases = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(cases)
	for case_name in dir(cases):
		if case_name.startswith("test_") and callable(getattr(cases, case_name)):
			assert case_name not in globals(), case_name
			globals()[case_name] = getattr(cases, case_name)
			_REPORT_PR_CASE_NAMES.append(case_name)
	# A renamed or emptied cases module must fail loudly, not collect nothing.
	assert len(_REPORT_PR_CASE_NAMES) >= 10, f"only {len(_REPORT_PR_CASE_NAMES)} report PR cases collected"


_collect_report_pr_cases()


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
	test_analyze_runs_report_structure_check_with_kill_switch()
	test_analyze_report_artifact_upload_is_always_gated()
	test_collect_step_logs_coverage_line_and_annotations()
	test_collection_gap_alert_step_is_warning_and_fail_open()
	test_prompt_names_coverage_gaps_in_executive_summary()
	for case_name in _REPORT_PR_CASE_NAMES:
		globals()[case_name]()
	for case_name in _REPORT_DELIVERY_CASES:
		globals()[case_name]()
	return 0


# The report-delivery cases (#8963) live in
# tests/test_workflow_log_analysis_report_delivery.py and are collected here,
# since CI already runs this file and ci.yml was outside that issue's file
# grant. A name clash would hide a test, so it fails loudly.
_REPORT_DELIVERY_CASES: list[str] = []


def _collect_report_delivery_cases() -> None:
	spec = importlib.util.spec_from_file_location(
		"workflow_log_analysis_report_delivery_cases",
		Path(__file__).resolve().parent / "test_workflow_log_analysis_report_delivery.py",
	)
	cases = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(cases)
	for case_name in dir(cases):
		if case_name.startswith("test_") and callable(getattr(cases, case_name)):
			assert case_name not in globals(), case_name
			globals()[case_name] = getattr(cases, case_name)
			_REPORT_DELIVERY_CASES.append(case_name)
	# A renamed or emptied cases module must fail loudly, not collect nothing.
	assert len(_REPORT_DELIVERY_CASES) >= 15, f"only {len(_REPORT_DELIVERY_CASES)} report delivery cases collected"


_collect_report_delivery_cases()


if __name__ == "__main__":
	raise SystemExit(main())

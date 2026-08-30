#!/usr/bin/env python3
"""Contract tests for implement-phase Semble workflow wiring."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
DIAGNOSE_SCRIPT = REPO_ROOT / "scripts" / "implement_diagnose_post_codex_failure.sh"
COMMIT_HELPER = REPO_ROOT / "scripts" / "implement_commit_changes.sh"


def _workflow_text() -> str:
	return IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")


def _diagnose_text() -> str:
	return DIAGNOSE_SCRIPT.read_text(encoding="utf-8")


def _commit_helper_text() -> str:
	return COMMIT_HELPER.read_text(encoding="utf-8")



def _workflow_doc() -> dict[str, object]:
	doc = yaml.safe_load(_workflow_text())
	if not isinstance(doc, dict):
		raise AssertionError("Workflow did not parse into a mapping")
	return doc


def _step(step_name: str) -> dict[str, object]:
	jobs = _workflow_doc().get("jobs")
	if not isinstance(jobs, dict):
		raise AssertionError("Workflow jobs mapping is missing")
	for job in jobs.values():
		if not isinstance(job, dict):
			continue
		steps = job.get("steps")
		if not isinstance(steps, list):
			continue
		for step in steps:
			if isinstance(step, dict) and str(step.get("name", "")).strip() == step_name:
				return step
	raise AssertionError(f"Step not found in workflow: {step_name}")


def _step_run_text(step_name: str) -> str:
	run = _step(step_name).get("run")
	if not isinstance(run, str):
		raise AssertionError(f"Step does not define a run block: {step_name}")
	return run


def test_semble_repo_var_defaults_true() -> None:
	workflow = _workflow_text()
	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'true' }}" in workflow
	assert "SERENA_ENABLED: ${{ vars.SERENA_ENABLED || 'false' }}" in workflow
	assert "UNATTENDED_PHASE: implement" in workflow
	assert "EVENTS_JSONL_ENABLED: ${{ vars.EVENTS_JSONL_ENABLED || 'false' }}" in workflow


def test_runtime_workspace_exports_fail_open_semble_defaults() -> None:
	workspace_block = _step_run_text("Create runtime workspace")
	assert 'echo "SEMBLE_AVAILABLE=false"' in workspace_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in workspace_block
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in workspace_block
	assert 'echo "SERENA_AVAILABLE=false"' in workspace_block
	assert 'echo "SERENA_PROJECT_PREEXISTED=false"' in workspace_block
	assert 'echo "SERENA_PROJECT_BOOTSTRAP_HASH="' in workspace_block


def test_stage_workflow_support_files_bootstraps_serena_assets() -> None:
	stage_block = _step_run_text("Stage workflow support files")
	assert "for f in setup_serena.sh serena_stats_emit.py mcp_handshake_probe.py; do" in stage_block
	assert "for f in emit_event.sh emit_event.py; do" in stage_block
	assert "for f in transcript_archive.sh; do" in stage_block
	assert "Optional events mirror helper ${f} is unavailable" in stage_block
	assert "Optional transcript archive helper ${f} is unavailable" in stage_block
	assert 'mkdir -p scripts/templates' in stage_block
	assert 'scripts/templates/serena_project.yml.j2' in stage_block
	assert 'echo "scripts/templates/serena_project.yml.j2" >> "${FETCHED_MANIFEST}"' in stage_block
	assert "Optional Serena support asset ${f} is unavailable" in stage_block
	assert "Optional Serena template scripts/templates/serena_project.yml.j2 is unavailable" in stage_block
	assert 'git ls-files --error-unmatch -- "scripts/templates/serena_project.yml.j2"' in stage_block
	assert "preserving caller-owned Serena template" in stage_block
	assert "Serena bootstrap remains disabled" in stage_block


def test_transcript_archive_helper_is_opt_in_and_wired_for_implement() -> None:
	workflow = _workflow_text()
	stage_block = _step_run_text("Stage workflow support files")
	codex_block = _step_run_text("Run Codex implementation")
	assert "UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED: ${{ vars.UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED || 'false' }}" in workflow
	assert "for f in transcript_archive.sh; do" in stage_block
	assert 'source scripts/transcript_archive.sh 2>/dev/null || true' in codex_block
	assert 'archive_transcript "${GITHUB_RUN_ID:-local-run}" "implement" "${CODEX_OUTPUT_FILE}"' in codex_block


def test_stage_workflow_support_files_bootstraps_optional_semble_assets() -> None:
	stage_block = _step_run_text("Stage workflow support files")
	# build_semble_wrapper.sh added once the validate.yml BM25 wrapper was
	# extracted into a shared script — kept in the same optional-assets loop
	# so callers without it (consumer wrappers tracking older @stable) still
	# fail-soft to the legacy index path.
	assert "for f in install_semble.sh build_semble_wrapper.sh semble_helpers.sh; do" in stage_block
	assert "Optional Semble support script ${f} is unavailable" in stage_block
	assert "legacy path remains active" in stage_block
	assert "Optional Semble support script ${f} is not tracked in this checkout" in stage_block
	required_loop_line = next(
		line for line in stage_block.splitlines() if "for f in gh_helpers.sh" in line
	)
	assert "install_semble.sh" not in required_loop_line
	assert "build_semble_wrapper.sh" not in required_loop_line
	assert "semble_helpers.sh" not in required_loop_line
	assert "implement_commit_changes.sh" in required_loop_line


def test_render_prompt_python_is_staged_once_as_required_support() -> None:
	stage_block = _step_run_text("Stage workflow support files")
	render_prompt_loop_lines = [
		line.strip()
		for line in stage_block.splitlines()
		if line.strip().startswith("for f in ")
		and line.strip().endswith("; do")
		and "render_prompt.py" in line
	]
	assert len(render_prompt_loop_lines) == 1
	assert render_prompt_loop_lines[0].startswith("for f in gh_helpers.sh ")
	assert "for f in render_prompt.py; do" not in stage_block
	assert "Optional render_prompt.py backend unavailable" not in stage_block

	required_loop_start = stage_block.index(render_prompt_loop_lines[0])
	required_loop_end = stage_block.index("\ndone", required_loop_start)
	required_loop_block = stage_block[required_loop_start:required_loop_end]
	assert 'src=".codex-workflow-src/scripts/${f}"' in required_loop_block
	assert '[ -f ".codex-workflow-src-main/scripts/${f}" ]' in required_loop_block
	assert 'src=".codex-workflow-src-main/scripts/${f}"' in required_loop_block
	assert 'echo "::error::Missing required support script ${f}' in required_loop_block
	assert "exit 1" in required_loop_block
	assert 'install -m 0755 "${src}" "scripts/${f}"' in required_loop_block
	assert '_fetched_scripts+=("${f}")' in required_loop_block


def test_stage_workflow_support_files_bootstraps_revalidate_lifecycle_ai_memory_schemas() -> None:
	stage_block = _step_run_text("Stage workflow support files")
	assert "validation_history.v1.json" in stage_block
	assert "operator_bypass_audit.v1.json" in stage_block
	assert "revalidate_events.v1.json" in stage_block


def test_semble_bootstrap_steps_are_gated_and_fail_open() -> None:
	setup_step = _step("setup-uv")
	assert setup_step.get("if") == "env.SKIP_IMPLEMENT != 'true' && (env.SEMBLE_ENABLED == 'true' || env.SERENA_ENABLED == 'true')"
	assert setup_step.get("continue-on-error") is True
	assert setup_step.get("uses") == "astral-sh/setup-uv@v7"

	install_step = _step("Install semble")
	install_block = _step_run_text("Install semble")
	assert install_step.get("if") == "env.SKIP_IMPLEMENT != 'true' && (env.SEMBLE_ENABLED == 'true' || env.SERENA_ENABLED == 'true')"
	assert install_step.get("continue-on-error") is True
	assert 'if [ "${SEMBLE_ENABLED:-false}" != "true" ]; then' in install_block
	assert "scripts/install_semble.sh" in install_block
	assert 'if ! bash scripts/install_semble.sh; then' in install_block
	assert 'echo "SEMBLE_AVAILABLE=false" >> "$GITHUB_ENV"' in install_block
	assert 'echo "SEMBLE_AVAILABLE=true" >> "$GITHUB_ENV"' not in install_block
	assert "leaving Semble disabled for this run" in install_block

	index_step = _step("Build semble index")
	index_block = _step_run_text("Build semble index")
	assert index_step.get("if") == "env.SKIP_IMPLEMENT != 'true' && (env.SEMBLE_ENABLED == 'true')"
	assert index_step.get("continue-on-error") is True
	# Shared BM25 wrapper builder extracted to scripts/build_semble_wrapper.sh
	# (semble 0.1.3 lacks the index/query CLI, so the per-workflow inline
	# wrapper was unified once and re-used here). Inline `semble index . --out`
	# was unreachable code — delegate fully to the shared script.
	assert 'semble_index_path="${SEMBLE_INDEX_PATH:-${RUNTIME_DIR}/.semble-index}"' in index_block
	assert 'if [ -f scripts/build_semble_wrapper.sh ]; then' in index_block
	assert 'SEMBLE_INDEX_PATH="${semble_index_path}"' in index_block
	assert 'bash scripts/build_semble_wrapper.sh' in index_block
	assert 'echo "SEMBLE_INDEX_PATH=${semble_index_path}" >> "$GITHUB_ENV"' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false" >> "$GITHUB_ENV"' in index_block
	# These are now responsibilities of the shared script (delegated):
	assert '"${semble_bin}" index . --out "${semble_index_path}"' not in index_block
	assert 'if [ "${SEMBLE_AVAILABLE:-false}" != "true" ]; then' not in index_block


def test_targeted_file_context_receives_semble_inputs() -> None:
	codex_block = _step_run_text("Run Codex implementation")
	assert "python3 scripts/targeted_file_context.py" in codex_block
	assert '--semble-bin "$(command -v semble 2>/dev/null || true)"' in codex_block
	assert '--semble-index "${SEMBLE_INDEX_PATH}"' in codex_block
	assert '--semble-max-chunks "6"' in codex_block
	assert '--semble-fallback "marker"' in codex_block


def test_repair_prompt_appends_bounded_semble_context() -> None:
	repair_block = _step_run_text("Attempt post-Codex syntax repair")
	assert "source scripts/semble_helpers.sh" in repair_block
	assert 'python3 - "${CAPTURE_FILE}" "${ALLOW_LIST_FILE}" "${CAPTURED_FILES_FILE}" "${output_file}"' in repair_block
	assert '::warning::Failed to build repair Semble query' in repair_block
	assert 'REPAIR_SEMBLE_QUERY_FILE="${RUNTIME_DIR}/post_codex_repair_semble_query.txt"' in repair_block
	assert 'build_repair_semble_query "${REPAIR_SEMBLE_QUERY_FILE}"' in repair_block
	assert 'semble_query_block "$(cat "${REPAIR_SEMBLE_QUERY_FILE}")" 6 "Implement Repair Context" || true' in repair_block
	assert 'SERENA_TOOL_HINTS="${REPAIR_SERENA_TOOL_HINTS}" bash scripts/render_prompt.sh "${REPAIR_PROMPT_TEMPLATE}"' in repair_block
	assert 'Failed to render repair prompt template ${REPAIR_PROMPT_TEMPLATE}; using raw prompt.' in repair_block
	assert 'Keep apply_patch as the primary write path' in repair_block


def test_diagnose_prompt_appends_bounded_semble_context() -> None:
	diagnose = _diagnose_text()
	assert "source scripts/semble_helpers.sh" in diagnose
	assert 'python3 - "${FAILED_STEP_NAME}" "${CAPTURE_FILE}" "${output_file}"' in diagnose
	assert '::warning::Failed to build diagnose Semble query' in diagnose
	assert 'DIAGNOSE_SEMBLE_QUERY_FILE="${RUNTIME_DIR}/implement_diagnose_semble_query.txt"' in diagnose
	assert 'build_diagnose_semble_query "${DIAGNOSE_SEMBLE_QUERY_FILE}"' in diagnose
	assert 'semble_query_block "$(cat "${DIAGNOSE_SEMBLE_QUERY_FILE}")" 6 "Implement Diagnose Context" || true' in diagnose
	assert 'SERENA_TOOL_HINTS="${DIAGNOSE_SERENA_TOOL_HINTS}" bash scripts/render_prompt.sh "${DIAGNOSE_MODE_PROMPT_TEMPLATE}"' in diagnose


def test_setup_serena_step_runs_after_codex_config_and_emits_bootstrap_hash() -> None:
	workflow = _workflow_text()
	setup_step = _step("Setup Serena")
	setup_block = _step_run_text("Setup Serena")
	commit_step = _step_run_text("Commit changes")
	assert setup_step.get("if") == "env.SKIP_IMPLEMENT != 'true' && env.SERENA_ENABLED == 'true'"
	assert setup_step.get("continue-on-error") is True
	assert 'source scripts/emit_event.sh 2>/dev/null || true' in setup_block
	assert 'SERENA_FALLBACK_TARGET="implement" bash scripts/setup_serena.sh' in setup_block
	assert 'SERENA_FALLBACK target=implement reason=setup-failure' in setup_block
	assert 'echo "SERENA_AVAILABLE=false" >> "$GITHUB_ENV"' in setup_block
	assert 'echo "SERENA_PROJECT_BOOTSTRAP_HASH=${serena_project_hash}" >> "$GITHUB_ENV"' in setup_block
	assert workflow.find("- name: Create Codex config") < workflow.find("- name: Setup Serena")
	assert workflow.find("- name: Detect preexisting Serena project config") < workflow.find("- name: Setup Serena")
	assert "bash scripts/implement_commit_changes.sh" in commit_step
	assert 'if ! git ls-files --error-unmatch -- .serena >/dev/null 2>&1; then' in _commit_helper_text()


def test_detect_preexisting_serena_project_config_runs_after_checkout() -> None:
	workflow = _workflow_text()
	detect_step = _step("Detect preexisting Serena project config")
	detect_block = _step_run_text("Detect preexisting Serena project config")
	assert detect_step.get("if") == "env.SKIP_IMPLEMENT != 'true'"
	assert 'git ls-files --error-unmatch -- .serena/project.yml' in detect_block
	assert 'echo "SERENA_PROJECT_PREEXISTED=true" >> "$GITHUB_ENV"' in detect_block
	assert 'echo "SERENA_PROJECT_PREEXISTED=false" >> "$GITHUB_ENV"' in detect_block
	assert workflow.find("- name: Checkout repository") < workflow.find("- name: Detect preexisting Serena project config") < workflow.find("- name: Log checkout ref")


def test_emit_serena_stats_runs_before_cleanup_and_scans_implement_logs() -> None:
	workflow = _workflow_text()
	stats_step = _step("Emit Serena stats")
	stats_block = _step_run_text("Emit Serena stats")
	assert stats_step.get("if") == "always() && env.SKIP_IMPLEMENT != 'true'"
	assert stats_step.get("continue-on-error") is True
	assert 'python3 scripts/serena_stats_emit.py "${serena_stat_args[@]}"' in stats_block
	assert "post_codex_repair_log_attempt_*.txt" in stats_block
	assert workflow.find("- name: Emit Serena stats") < workflow.find("- name: Cleanup temporary artifacts")


def test_review_rb_judge_reissue_baseline_module_runs_clean() -> None:
	baseline_test = REPO_ROOT / "tests" / "test_review_rb_judge_reissue_baseline.py"
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	result = subprocess.run(
		["python3", str(baseline_test)],
		cwd=str(REPO_ROOT),
		env=env,
		capture_output=True,
		text=True,
		timeout=120,
	)
	assert result.returncode == 0, (
		"review-blocked baseline regression test failed\n"
		f"stdout:\n{result.stdout}\n"
		f"stderr:\n{result.stderr}"
	)


def test_review_rb_judge_self_run_exclusion_module_runs_clean() -> None:
	self_run_test = REPO_ROOT / "tests" / "test_review_rb_judge_self_run_exclusion.py"
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	result = subprocess.run(
		["python3", str(self_run_test)],
		cwd=str(REPO_ROOT),
		env=env,
		capture_output=True,
		text=True,
		timeout=120,
	)
	assert result.returncode == 0, (
		"review-blocked self-run exclusion regression test failed\n"
		f"stdout:\n{result.stdout}\n"
		f"stderr:\n{result.stderr}"
	)
	assert "passed" in result.stdout and "total" in result.stdout, (
		"review-blocked self-run exclusion regression test did not execute "
		"its direct-entrypoint harness\n"
		f"stdout:\n{result.stdout}\n"
		f"stderr:\n{result.stderr}"
	)


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())

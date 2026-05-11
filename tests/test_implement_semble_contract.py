#!/usr/bin/env python3
"""Contract tests for implement-phase Semble workflow wiring."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
DIAGNOSE_SCRIPT = REPO_ROOT / "scripts" / "implement_diagnose_post_codex_failure.sh"


def _workflow_text() -> str:
	return IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")


def _diagnose_text() -> str:
	return DIAGNOSE_SCRIPT.read_text(encoding="utf-8")



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


def test_runtime_workspace_exports_fail_open_semble_defaults() -> None:
	workspace_block = _step_run_text("Create runtime workspace")
	assert 'echo "SEMBLE_AVAILABLE=false"' in workspace_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in workspace_block
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in workspace_block
	assert 'echo "SERENA_AVAILABLE=false"' in workspace_block
	assert 'echo "SERENA_PROJECT_PREEXISTED=${serena_project_preexisted}"' in workspace_block
	assert 'echo "SERENA_PROJECT_BOOTSTRAP_HASH="' in workspace_block


def test_stage_workflow_support_files_bootstraps_serena_assets() -> None:
	stage_block = _step_run_text("Stage workflow support files")
	assert "for f in setup_serena.sh serena_stats_emit.py mcp_handshake_probe.py; do" in stage_block
	assert 'mkdir -p scripts/templates' in stage_block
	assert 'scripts/templates/serena_project.yml.j2' in stage_block
	assert 'echo "scripts/templates/serena_project.yml.j2" >> "${FETCHED_MANIFEST}"' in stage_block
	assert "Optional Serena support asset ${f} is unavailable" in stage_block
	assert "Optional Serena template scripts/templates/serena_project.yml.j2 is unavailable" in stage_block
	assert "Serena bootstrap remains disabled" in stage_block


def test_stage_workflow_support_files_bootstraps_optional_semble_assets() -> None:
	stage_block = _step_run_text("Stage workflow support files")
	assert "for f in install_semble.sh semble_helpers.sh; do" in stage_block
	assert "Optional Semble support script ${f} is unavailable" in stage_block
	assert "legacy path remains active" in stage_block
	assert "Optional Semble support script ${f} is not tracked in this checkout" in stage_block
	required_loop_line = next(
		line for line in stage_block.splitlines() if "for f in gh_helpers.sh" in line
	)
	assert "install_semble.sh" not in required_loop_line
	assert "semble_helpers.sh" not in required_loop_line


def test_semble_bootstrap_steps_are_gated_and_fail_open() -> None:
	setup_step = _step("setup-uv")
	assert setup_step.get("if") == "env.SKIP_IMPLEMENT != 'true' && (env.SEMBLE_ENABLED == 'true' || env.SERENA_ENABLED == 'true')"
	assert setup_step.get("continue-on-error") is True
	assert setup_step.get("uses") == "astral-sh/setup-uv@v3"

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
	assert index_step.get("if") == "env.SKIP_IMPLEMENT != 'true' && env.SEMBLE_ENABLED == 'true'"
	assert index_step.get("continue-on-error") is True
	assert 'echo "SEMBLE_INDEX_PATH=${semble_index_path}" >> "$GITHUB_ENV"' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false" >> "$GITHUB_ENV"' in index_block
	assert 'if [ "${SEMBLE_AVAILABLE:-false}" != "true" ]; then' in index_block
	assert 'if "${semble_bin}" index . --out "${semble_index_path}"; then' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=true" >> "$GITHUB_ENV"' in index_block


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
	assert setup_step.get("if") == "env.SKIP_IMPLEMENT != 'true' && env.SERENA_ENABLED == 'true'"
	assert setup_step.get("continue-on-error") is True
	assert 'bash scripts/setup_serena.sh' in setup_block
	assert 'echo "SERENA_AVAILABLE=false" >> "$GITHUB_ENV"' in setup_block
	assert 'echo "SERENA_PROJECT_BOOTSTRAP_HASH=${serena_project_hash}" >> "$GITHUB_ENV"' in setup_block
	assert workflow.find("- name: Create Codex config") < workflow.find("- name: Setup Serena")


def test_emit_serena_stats_runs_before_cleanup_and_scans_implement_logs() -> None:
	workflow = _workflow_text()
	stats_step = _step("Emit Serena stats")
	stats_block = _step_run_text("Emit Serena stats")
	assert stats_step.get("if") == "always() && env.SKIP_IMPLEMENT != 'true'"
	assert stats_step.get("continue-on-error") is True
	assert 'python3 scripts/serena_stats_emit.py "${serena_stat_args[@]}"' in stats_block
	assert "post_codex_repair_log_attempt_*.txt" in stats_block
	assert workflow.find("- name: Emit Serena stats") < workflow.find("- name: Cleanup temporary artifacts")


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

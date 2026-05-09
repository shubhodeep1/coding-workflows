#!/usr/bin/env python3
"""Contract tests for implement-phase Semble workflow wiring."""
from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
DIAGNOSE_SCRIPT = REPO_ROOT / "scripts" / "implement_diagnose_post_codex_failure.sh"


def _workflow_text() -> str:
	return IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")


def _diagnose_text() -> str:
	return DIAGNOSE_SCRIPT.read_text(encoding="utf-8")


def _step_block(step_name: str) -> list[str]:
	lines = _workflow_text().splitlines()
	needle = f"- name: {step_name}"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		step_indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:"):
				indent = len(candidate) - len(candidate.lstrip(" "))
				if indent == step_indent:
					end = j
					break
		return lines[idx:end]
	raise AssertionError(f"Step not found in workflow: {step_name}")


def _step_block_text(step_name: str) -> str:
	return "\n".join(_step_block(step_name))


def test_semble_repo_var_defaults_false() -> None:
	workflow = _workflow_text()
	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}" in workflow


def test_runtime_workspace_exports_fail_open_semble_defaults() -> None:
	workspace_block = _step_block_text("Create runtime workspace")
	assert 'echo "SEMBLE_AVAILABLE=false"' in workspace_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in workspace_block
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in workspace_block


def test_stage_workflow_support_files_bootstraps_optional_semble_assets() -> None:
	stage_block = _step_block_text("Stage workflow support files")
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
	setup_block = _step_block_text("setup-uv")
	assert "if: env.SKIP_IMPLEMENT != 'true' && env.SEMBLE_ENABLED == 'true'" in setup_block
	assert "continue-on-error: true" in setup_block
	assert "uses: astral-sh/setup-uv@v3" in setup_block

	install_block = _step_block_text("Install semble")
	assert "if: env.SKIP_IMPLEMENT != 'true' && env.SEMBLE_ENABLED == 'true'" in install_block
	assert "scripts/install_semble.sh" in install_block
	assert 'echo "SEMBLE_AVAILABLE=false" >> "$GITHUB_ENV"' in install_block
	assert "leaving Semble disabled for this run" in install_block

	index_block = _step_block_text("Build semble index")
	assert "if: env.SKIP_IMPLEMENT != 'true' && env.SEMBLE_ENABLED == 'true'" in index_block
	assert 'echo "SEMBLE_INDEX_PATH=${semble_index_path}" >> "$GITHUB_ENV"' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false" >> "$GITHUB_ENV"' in index_block
	assert 'if [ "${SEMBLE_AVAILABLE:-false}" != "true" ]; then' in index_block
	assert 'if "${semble_bin}" index . --out "${semble_index_path}"; then' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=true" >> "$GITHUB_ENV"' in index_block


def test_targeted_file_context_receives_semble_inputs() -> None:
	codex_block = _step_block_text("Run Codex implementation")
	assert "python3 scripts/targeted_file_context.py" in codex_block
	assert '--semble-bin "$(command -v semble 2>/dev/null || true)"' in codex_block
	assert '--semble-index "${SEMBLE_INDEX_PATH}"' in codex_block
	assert '--semble-max-chunks "6"' in codex_block
	assert '--semble-fallback "marker"' in codex_block


def test_repair_prompt_appends_bounded_semble_context() -> None:
	repair_block = _step_block_text("Attempt post-Codex syntax repair")
	assert "source scripts/semble_helpers.sh" in repair_block
	assert 'python3 - "${CAPTURE_FILE}" "${ALLOW_LIST_FILE}" "${CAPTURED_FILES_FILE}" "${output_file}"' in repair_block
	assert 'REPAIR_SEMBLE_QUERY_FILE="${RUNTIME_DIR}/post_codex_repair_semble_query.txt"' in repair_block
	assert 'build_repair_semble_query "${REPAIR_SEMBLE_QUERY_FILE}"' in repair_block
	assert 'semble_query_block "$(cat "${REPAIR_SEMBLE_QUERY_FILE}")" 6 "Implement Repair Context" || true' in repair_block


def test_diagnose_prompt_appends_bounded_semble_context() -> None:
	diagnose = _diagnose_text()
	assert "source scripts/semble_helpers.sh" in diagnose
	assert 'python3 - "${FAILED_STEP_NAME}" "${CAPTURE_FILE}" "${output_file}"' in diagnose
	assert 'DIAGNOSE_SEMBLE_QUERY_FILE="${RUNTIME_DIR}/implement_diagnose_semble_query.txt"' in diagnose
	assert 'build_diagnose_semble_query "${DIAGNOSE_SEMBLE_QUERY_FILE}"' in diagnose
	assert 'semble_query_block "$(cat "${DIAGNOSE_SEMBLE_QUERY_FILE}")" 6 "Implement Diagnose Context" || true' in diagnose


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

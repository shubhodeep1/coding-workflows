#!/usr/bin/env python3
"""Contract tests for judge-family Semble prefetch wiring."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE_POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
RENDER_PROMPT = REPO_ROOT / "scripts" / "render_prompt.sh"
ORCHESTRATE_POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
REVIEW_RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
LIVE_JUDGE_PROMPTS = (
	REPO_ROOT / "prompts" / "mode-judge.txt",
	REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt",
	REPO_ROOT / "prompts" / "mode-judge-stall-recovery.txt",
)


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _step_block(text: str, step_name: str) -> str:
	marker = f"- name: {step_name}"
	start = text.find(marker)
	assert start != -1, f"Missing workflow step: {step_name}"
	next_step = text.find("\n      - name:", start + len(marker))
	if next_step == -1:
		return text[start:]
	return text[start:next_step]


def test_render_prompt_replaces_semble_prefetch_and_guards_placeholder() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		prompt_file = Path(tmp) / "prompt.txt"
		prompt_file.write_text("Header\n{{SEMBLE_PREFETCH}}\nFooter\n", encoding="utf-8")

		result = subprocess.run(
			["bash", str(RENDER_PROMPT), str(prompt_file)],
			cwd=REPO_ROOT,
			env={
				**os.environ,
				"PYTHONDONTWRITEBYTECODE": "1",
				"SEMBLE_PREFETCH": "=== SEMBLE: Judge Context ===\nchunk one\n=== END SEMBLE ===",
			},
			capture_output=True,
			text=True,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == (
			"Header\n"
			"=== SEMBLE: Judge Context ===\n"
			"chunk one\n"
			"=== END SEMBLE ===\n"
			"Footer\n"
		)

	render_text = _read(RENDER_PROMPT)
	assert '"{{SEMBLE_PREFETCH}}")' in render_text
	assert "Unresolved SEMBLE_PREFETCH placeholder" in render_text


def test_orchestrate_poll_workflow_bootstraps_optional_semble_support_for_judges() -> None:
	workflow = _read(ORCHESTRATE_POLL_WORKFLOW)
	workspace_block = _step_block(workflow, "Create runtime workspace")
	stage_block = _step_block(workflow, "Stage workflow support files")
	setup_block = _step_block(workflow, "setup-uv")
	install_block = _step_block(workflow, "Install semble")
	index_block = _step_block(workflow, "Build semble index")

	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}" in workflow
	assert 'echo "SEMBLE_AVAILABLE=false"' in workspace_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in workspace_block
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in workspace_block

	assert "for f in install_semble.sh semble_helpers.sh; do" in stage_block
	assert "Optional Semble support script ${f} is unavailable" in stage_block
	assert "legacy path remains active" in stage_block

	assert "steps.find_tracking.outputs.has_work == 'true' && env.SEMBLE_ENABLED == 'true'" in setup_block
	assert "uses: astral-sh/setup-uv@v3" in setup_block
	assert "steps.find_tracking.outputs.has_work == 'true' && env.SEMBLE_ENABLED == 'true'" in install_block
	assert 'if [ ! -f scripts/install_semble.sh ]; then' in install_block
	assert 'echo "SEMBLE_AVAILABLE=false" >> "$GITHUB_ENV"' in install_block
	assert 'SEMBLE_BIN_PATH="$(command -v semble 2>/dev/null || true)"' in install_block
	assert 'echo "SEMBLE_BIN=${SEMBLE_BIN_PATH}" >> "$GITHUB_ENV"' in install_block
	assert "steps.find_tracking.outputs.has_work == 'true' && env.SEMBLE_ENABLED == 'true'" in index_block
	assert 'echo "SEMBLE_INDEX_PATH=${semble_index_path}" >> "$GITHUB_ENV"' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false" >> "$GITHUB_ENV"' in index_block
	assert 'if [ "${SEMBLE_AVAILABLE:-false}" != "true" ]; then' in index_block
	assert 'if "${semble_bin}" index . --out "${semble_index_path}" > "${RUNTIME_DIR}/semble_index.log" 2>&1; then' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=true" >> "$GITHUB_ENV"' in index_block

	assert workflow.find("- name: setup-uv") < workflow.find("- name: Process each tracking issue")
	assert workflow.find("- name: Install semble") < workflow.find("- name: Process each tracking issue")
	assert workflow.find("- name: Build semble index") < workflow.find("- name: Process each tracking issue")


def test_live_judge_templates_declare_semble_prefetch_placeholder() -> None:
	for prompt in LIVE_JUDGE_PROMPTS:
		assert "{{SEMBLE_PREFETCH}}" in _read(prompt), f"missing Semble placeholder in {prompt.name}"


def test_orchestrate_poll_process_wires_semble_prefetch_into_live_judges_only() -> None:
	text = _read(ORCHESTRATE_POLL_PROCESS)

	assert 'if [ -f "scripts/semble_helpers.sh" ]; then' in text
	assert 'SEMBLE_HELPERS_AVAILABLE="false"' in text
	assert 'render_judge_semble_prefetch_from_query_file()' in text
	assert 'SEMBLE_PREFETCH="${stall_judge_semble_prefetch}" bash scripts/render_prompt.sh prompts/mode-judge-stall-recovery.txt' in text
	assert 'SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge-review-blocked.txt' in text
	assert 'SEMBLE_PREFETCH="${JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge.txt' in text
	assert 'judge_semble_prefetch="$(render_judge_semble_prefetch_from_query_file "${judge_semble_query_file}" "Integration Conflict Judge Context")"' in text
	assert "printf '%s\\n' \"${judge_semble_prefetch}\"" in text
	assert 'bash scripts/render_prompt.sh prompts/mode-orchestrate-poll-judge.txt' not in text


def test_review_rb_judge_sources_semble_helpers_and_passes_prefetch_to_renderer() -> None:
	text = _read(REVIEW_RB_JUDGE)

	assert 'SUPPORT_ROOT_DIR="${SUPPORT_ROOT_DIR:-$(pwd)}"' in text
	assert 'SUPPORT_PROMPTS_DIR="${SUPPORT_PROMPTS_DIR:-${SUPPORT_ROOT_DIR}/prompts}"' in text
	assert 'if [ -f "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh" ]; then' in text
	assert 'REVIEW_RB_SEMBLE_HELPERS_AVAILABLE="false"' in text
	assert 'RB_JUDGE_SEMBLE_PREFETCH="$(render_review_rb_semble_prefetch "${RB_JUDGE_SEMBLE_QUERY_FILE}" "Review-Blocked Judge Context")"' in text
	assert 'SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"' in text


def main() -> int:
	test_render_prompt_replaces_semble_prefetch_and_guards_placeholder()
	test_orchestrate_poll_workflow_bootstraps_optional_semble_support_for_judges()
	test_live_judge_templates_declare_semble_prefetch_placeholder()
	test_orchestrate_poll_process_wires_semble_prefetch_into_live_judges_only()
	test_review_rb_judge_sources_semble_helpers_and_passes_prefetch_to_renderer()
	print("OK: judge Semble prefetch contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

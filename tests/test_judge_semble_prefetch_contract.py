#!/usr/bin/env python3
"""Contract tests for judge-family Semble prefetch wiring."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
RENDER_PROMPT = REPO_ROOT / "scripts" / "render_prompt.sh"
ORCHESTRATE = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
REVIEW_RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
MODE_JUDGE = REPO_ROOT / "prompts" / "mode-judge.txt"
MODE_JUDGE_REVIEW_BLOCKED = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"
MODE_JUDGE_STALL = REPO_ROOT / "prompts" / "mode-judge-stall-recovery.txt"


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


def _run_render_prompt(prompt_text: str, *, semble_prefetch: str = "", allow_workflow_edits: str = "false") -> subprocess.CompletedProcess:
	with tempfile.TemporaryDirectory() as tmpdir:
		prompt_file = Path(tmpdir) / "prompt.txt"
		prompt_file.write_text(prompt_text, encoding="utf-8")
		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["ALLOW_WORKFLOW_EDITS"] = allow_workflow_edits
		env["SEMBLE_PREFETCH"] = semble_prefetch
		return subprocess.run(
			["bash", str(RENDER_PROMPT), str(prompt_file)],
			cwd=REPO_ROOT,
			env=env,
			capture_output=True,
			text=True,
		)


def test_render_prompt_replaces_semble_prefetch_and_existing_placeholder() -> None:
	render_prompt = _read(RENDER_PROMPT)
	assert '"{{WORKFLOW_EDIT_RESTRICTION}}")' in render_prompt
	assert '"{{SEMBLE_PREFETCH}}")' in render_prompt
	assert "Unresolved WORKFLOW_EDIT_RESTRICTION placeholder" in render_prompt
	assert "Unresolved SEMBLE_PREFETCH placeholder" in render_prompt

	result = _run_render_prompt(
		"Header\n{{SEMBLE_PREFETCH}}\n{{WORKFLOW_EDIT_RESTRICTION}}\nFooter\n",
		semble_prefetch="=== SEMBLE: Judge Context ===\nchunk 1\n=== END SEMBLE ===",
		allow_workflow_edits="true",
	)
	assert result.returncode == 0, result.stderr
	assert "{{SEMBLE_PREFETCH}}" not in result.stdout
	assert "{{WORKFLOW_EDIT_RESTRICTION}}" not in result.stdout
	assert "=== SEMBLE: Judge Context ===\nchunk 1\n=== END SEMBLE ===" in result.stdout
	assert "CI workflow edits under .github/workflows/ are permitted" in result.stdout

	empty_result = _run_render_prompt("Header\n{{SEMBLE_PREFETCH}}\nFooter\n")
	assert empty_result.returncode == 0, empty_result.stderr
	assert empty_result.stdout == "Header\n\nFooter\n"


def test_judge_prompt_templates_have_single_top_level_semble_placeholder() -> None:
	for path in (MODE_JUDGE, MODE_JUDGE_REVIEW_BLOCKED, MODE_JUDGE_STALL):
		text = _read(path)
		lines = text.splitlines()
		matches = [idx for idx, line in enumerate(lines, start=1) if line.strip() == "{{SEMBLE_PREFETCH}}"]
		assert len(matches) == 1, f"Expected exactly one exact-line Semble placeholder in {path}"
		assert matches[0] <= 20, f"Semble placeholder should stay near the task header in {path}"


def test_orchestrate_poll_workflow_bootstraps_semble_for_judge_runs() -> None:
	workflow = _read(WORKFLOW)
	create_workspace_block = _step_block(workflow, "Create runtime workspace")
	stage_block = _step_block(workflow, "Stage workflow support files")
	uv_block = _step_block(workflow, "Setup uv for Semble")
	install_block = _step_block(workflow, "Install semble")
	index_block = _step_block(workflow, "Build semble index")

	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}" in workflow
	assert 'echo "SEMBLE_AVAILABLE=false"' in create_workspace_block
	assert 'echo "SEMBLE_BIN="' in create_workspace_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in create_workspace_block
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in create_workspace_block
	assert 'OPTIONAL_SUPPORT_SCRIPTS="install_semble.sh semble_helpers.sh"' in stage_block
	assert '_fetched_scripts+=("${f}")' in stage_block
	assert "astral-sh/setup-uv@v3" in uv_block
	assert "env.SEMBLE_ENABLED == 'true'" in uv_block
	assert 'source "scripts/install_semble.sh"' in install_block
	assert 'echo "SEMBLE_BIN=${SEMBLE_BIN_PATH}" >> "$GITHUB_ENV"' in install_block
	assert 'if "${SEMBLE_BIN_PATH}" index . --out "${SEMBLE_INDEX_PATH}"' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=true" >> "$GITHUB_ENV"' in index_block


def test_orchestrate_poll_process_wires_semble_into_live_judge_prompts() -> None:
	script = _read(ORCHESTRATE)
	assert 'if [ -f "scripts/semble_helpers.sh" ]; then' in script
	assert 'declare -F semble_query_block' in script
	assert 'build_wave_judge_semble_query()' in script
	assert 'build_stall_judge_semble_query()' in script
	assert 'build_review_blocked_judge_semble_query()' in script
	assert 'build_integration_conflict_judge_semble_query()' in script
	assert 'SEMBLE_PREFETCH="${JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge.txt' in script
	assert 'SEMBLE_PREFETCH="${stall_judge_semble_prefetch}" bash scripts/render_prompt.sh prompts/mode-judge-stall-recovery.txt' in script
	assert 'SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge-review-blocked.txt' in script
	assert 'integration_judge_semble_prefetch="$(build_semble_prefetch_block \\' in script
	assert 'if [ -n "${integration_judge_semble_prefetch}" ]; then' in script
	assert "printf '%s\\n\\n' \"${integration_judge_semble_prefetch}\"" in script


def test_review_blocked_judge_wires_semble_prefetch_into_rendered_prompt() -> None:
	script = _read(REVIEW_RB_JUDGE)
	assert 'if [ -f "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh" ]; then' in script
	assert 'build_review_blocked_semble_query()' in script
	assert 'build_review_blocked_semble_prefetch()' in script
	assert 'append_semble_query_section "PR diff excerpts:" "${pr_diff_text}" 3200' in script
	assert 'SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"' in script


def main() -> int:
	test_render_prompt_replaces_semble_prefetch_and_existing_placeholder()
	test_judge_prompt_templates_have_single_top_level_semble_placeholder()
	test_orchestrate_poll_workflow_bootstraps_semble_for_judge_runs()
	test_orchestrate_poll_process_wires_semble_into_live_judge_prompts()
	test_review_blocked_judge_wires_semble_prefetch_into_rendered_prompt()
	print("OK: judge Semble prefetch contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

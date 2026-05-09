#!/usr/bin/env python3
"""Contract tests for judge-family Semble prefetch wiring."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE_POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
ORCHESTRATE_POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
REVIEW_RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
RENDER_PROMPT = REPO_ROOT / "scripts" / "render_prompt.sh"
MODE_JUDGE = REPO_ROOT / "prompts" / "mode-judge.txt"
MODE_JUDGE_REVIEW_BLOCKED = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"
MODE_JUDGE_STALL_RECOVERY = REPO_ROOT / "prompts" / "mode-judge-stall-recovery.txt"
MODE_ORCHESTRATE_POLL_JUDGE = REPO_ROOT / "prompts" / "mode-orchestrate-poll-judge.txt"


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


def _render_prompt(prompt_text: str, semble_prefetch: str | None) -> str:
	with tempfile.TemporaryDirectory(prefix="judge_semble_render_") as td:
		tmpdir = Path(td)
		prompt_file = tmpdir / "prompt.txt"
		prompt_file.write_text(prompt_text, encoding="utf-8")

		env = os.environ.copy()
		if semble_prefetch is None:
			env.pop("SEMBLE_PREFETCH", None)
		else:
			env["SEMBLE_PREFETCH"] = semble_prefetch

		proc = subprocess.run(
			["bash", str(RENDER_PROMPT), str(prompt_file)],
			cwd=str(REPO_ROOT),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)
		assert proc.returncode == 0, (
			f"render_prompt.sh failed with {proc.returncode}\n"
			f"stdout:\n{proc.stdout}\n\n"
			f"stderr:\n{proc.stderr}"
		)
		return proc.stdout


def test_render_prompt_injects_semble_prefetch_when_set() -> None:
	rendered = _render_prompt(
		"Role: judge\n{{SEMBLE_PREFETCH}}\nFooter\n",
		"=== SEMBLE: Judge Context ===\nchunk\n=== END SEMBLE ===",
	)

	assert "{{SEMBLE_PREFETCH}}" not in rendered
	assert "=== SEMBLE: Judge Context ===" in rendered
	assert "chunk" in rendered
	assert rendered.endswith("Footer\n")


def test_render_prompt_injects_semble_prefetch_with_surrounding_whitespace() -> None:
	rendered = _render_prompt(
		"Role: judge\n  {{SEMBLE_PREFETCH}}   \nFooter\n",
		"=== SEMBLE: Judge Context ===\nchunk",
	)

	assert "{{SEMBLE_PREFETCH}}" not in rendered
	assert "=== SEMBLE: Judge Context ===" in rendered
	assert "chunk" in rendered
	assert rendered.endswith("Footer\n")


def test_render_prompt_drops_semble_prefetch_placeholder_when_empty() -> None:
	rendered = _render_prompt("Before\n{{SEMBLE_PREFETCH}}\nAfter\n", None)

	assert "{{SEMBLE_PREFETCH}}" not in rendered
	assert rendered == "Before\n\nAfter\n"


def test_render_prompt_leaves_nonstandalone_semble_marker_text_unchanged() -> None:
	rendered = _render_prompt(
		"Before {{SEMBLE_PREFETCH}} After\n",
		"=== SEMBLE: Judge Context ===\nchunk\n=== END SEMBLE ===",
	)

	assert rendered == "Before {{SEMBLE_PREFETCH}} After\n"


def test_orchestrate_poll_workflow_bootstrap_and_runtime_defaults_wire_semble() -> None:
	workflow = _read(ORCHESTRATE_POLL_WF)
	stage_block = _step_block(workflow, "Stage workflow support files")
	init_block = _step_block(workflow, "Create runtime workspace")

	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}" in workflow
	assert 'OPTIONAL_BOOTSTRAP_SCRIPTS="install_semble.sh semble_helpers.sh"' in stage_block
	assert 'echo "SEMBLE_AVAILABLE=false"' in init_block
	assert 'echo "SEMBLE_BIN="' in init_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in init_block
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in init_block


def test_orchestrate_poll_workflow_adds_gated_setup_install_and_index_steps() -> None:
	workflow = _read(ORCHESTRATE_POLL_WF)
	uv_block = _step_block(workflow, "Setup uv for Semble")
	install_block = _step_block(workflow, "Install semble")
	index_block = _step_block(workflow, "Build semble index")

	assert "astral-sh/setup-uv@v3" in uv_block
	assert "env.SEMBLE_ENABLED == 'true'" in uv_block
	assert 'source "scripts/install_semble.sh"' in install_block
	assert 'echo "SEMBLE_BIN=${SEMBLE_BIN_PATH}" >> "$GITHUB_ENV"' in install_block
	assert '"${SEMBLE_BIN_PATH}" index . --out "${SEMBLE_INDEX_PATH}"' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=true" >> "$GITHUB_ENV"' in index_block


def test_live_judge_templates_expose_semble_placeholder() -> None:
	for path in [MODE_JUDGE, MODE_JUDGE_REVIEW_BLOCKED, MODE_JUDGE_STALL_RECOVERY]:
		text = _read(path)
		assert "{{SEMBLE_PREFETCH}}" in text, f"Missing Semble placeholder in {path}"


def test_judge_scripts_wire_semble_prefetch_into_dynamic_prompt_builds() -> None:
	orchestrate = _read(ORCHESTRATE_POLL_PROCESS)
	review_rb = _read(REVIEW_RB_JUDGE)

	assert 'source scripts/semble_helpers.sh' in orchestrate
	assert '_build_judge_semble_prefetch' in orchestrate
	assert 'SEMBLE_PREFETCH="${stall_judge_semble_prefetch}" bash scripts/render_prompt.sh prompts/mode-judge-stall-recovery.txt' in orchestrate
	assert 'SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge-review-blocked.txt' in orchestrate
	assert 'SEMBLE_PREFETCH="${JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge.txt' in orchestrate
	assert 'printf \'%s\\n\' "${integration_judge_semble_prefetch}"' in orchestrate
	assert 'source "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh"' in review_rb
	assert 'SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"' in review_rb


def test_unwired_orchestrate_poll_judge_prompt_remains_unconsumed() -> None:
	orchestrate = _read(ORCHESTRATE_POLL_PROCESS)
	assert "mode-orchestrate-poll-judge.txt" not in orchestrate
	assert MODE_ORCHESTRATE_POLL_JUDGE.exists()


def main() -> int:
	test_render_prompt_injects_semble_prefetch_when_set()
	test_render_prompt_injects_semble_prefetch_with_surrounding_whitespace()
	test_render_prompt_drops_semble_prefetch_placeholder_when_empty()
	test_render_prompt_leaves_nonstandalone_semble_marker_text_unchanged()
	test_orchestrate_poll_workflow_bootstrap_and_runtime_defaults_wire_semble()
	test_orchestrate_poll_workflow_adds_gated_setup_install_and_index_steps()
	test_live_judge_templates_expose_semble_placeholder()
	test_judge_scripts_wire_semble_prefetch_into_dynamic_prompt_builds()
	test_unwired_orchestrate_poll_judge_prompt_remains_unconsumed()
	print("OK: judge Semble prefetch contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

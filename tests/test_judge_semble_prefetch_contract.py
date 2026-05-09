#!/usr/bin/env python3
"""Contract tests for judge-family Semble prefetch wiring."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROMPT = REPO_ROOT / "scripts" / "render_prompt.sh"
ORCHESTRATE_POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
ORCHESTRATE_POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
REVIEW_RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
PROMPT_FILES = [
	REPO_ROOT / "prompts" / "mode-judge.txt",
	REPO_ROOT / "prompts" / "mode-judge-stall-recovery.txt",
	REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt",
]


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _run_render_prompt(semble_prefetch: str | None) -> str:
	with tempfile.TemporaryDirectory(prefix="test_judge_semble_prefetch_") as td:
		prompt_file = Path(td) / "prompt.txt"
		prompt_file.write_text(
			"Header\n{{SEMBLE_PREFETCH}}\nFooter\n",
			encoding="utf-8",
		)
		env = os.environ.copy()
		env["ALLOW_WORKFLOW_EDITS"] = "false"
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


def test_render_prompt_supports_semble_prefetch_placeholder_and_guard() -> None:
	text = _read(RENDER_PROMPT)
	assert 'SEMBLE_PREFETCH_BLOCK="${SEMBLE_PREFETCH:-}"' in text
	assert '"{{SEMBLE_PREFETCH}}")' in text
	assert 'Unresolved SEMBLE_PREFETCH placeholder in rendered output for ${PROMPT_FILE}' in text


def test_render_prompt_renders_and_removes_semble_prefetch_block() -> None:
	empty_render = _run_render_prompt(None)
	assert empty_render == "Header\nFooter\n"
	assert "{{SEMBLE_PREFETCH}}" not in empty_render

	filled_render = _run_render_prompt("=== SEMBLE: Judge Context ===\nchunk\n=== END SEMBLE ===")
	assert filled_render == "Header\n=== SEMBLE: Judge Context ===\nchunk\n=== END SEMBLE ===\nFooter\n"
	assert "{{SEMBLE_PREFETCH}}" not in filled_render


def test_live_judge_prompt_templates_include_placeholder() -> None:
	for prompt_file in PROMPT_FILES:
		text = _read(prompt_file)
		assert "{{SEMBLE_PREFETCH}}" in text, f"missing placeholder in {prompt_file}"


def test_orchestrate_poll_workflow_bootstraps_semble_for_judge_runs() -> None:
	workflow = _read(ORCHESTRATE_POLL_WORKFLOW)
	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}" in workflow
	assert 'echo "SEMBLE_AVAILABLE=false"' in workflow
	assert 'echo "SEMBLE_BIN="' in workflow
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in workflow
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in workflow
	assert 'OPTIONAL_BOOTSTRAP_SCRIPTS="install_semble.sh semble_helpers.sh"' in workflow
	assert "- name: Setup uv for Semble" in workflow
	assert "uses: astral-sh/setup-uv@v3" in workflow
	assert "- name: Install semble" in workflow
	assert 'if [ ! -f "scripts/install_semble.sh" ]; then' in workflow
	assert 'SEMBLE_BIN_PATH="$(command -v semble 2>/dev/null || true)"' in workflow
	assert "- name: Build semble index" in workflow
	assert 'if "${SEMBLE_BIN_PATH}" index . --out "${SEMBLE_INDEX_PATH}"' in workflow
	assert 'echo "SEMBLE_INDEX_AVAILABLE=true" >> "$GITHUB_ENV"' in workflow


def test_orchestrate_poll_process_wires_semble_prefetch_into_all_live_judges() -> None:
	text = _read(ORCHESTRATE_POLL_PROCESS)
	assert 'if [ -f "scripts/semble_helpers.sh" ]' in text
	assert 'build_semble_prefetch_context() {' in text
	assert 'render_prompt_with_semble_prefetch() {' in text
	assert 'render_prompt_with_semble_prefetch prompts/mode-judge.txt "${JUDGE_SEMBLE_CONTEXT_FILE}"' in text
	assert 'render_prompt_with_semble_prefetch prompts/mode-judge-stall-recovery.txt "${stall_judge_semble_context_file}"' in text
	assert 'render_prompt_with_semble_prefetch prompts/mode-judge-review-blocked.txt "${RB_JUDGE_SEMBLE_CONTEXT_FILE}"' in text
	assert 'integration_semble_context_file="${RUNTIME_DIR}/integration_judge_semble_context_${final_pr}.txt"' in text
	assert 'cat "${integration_semble_context_file}"' in text
	assert 'append_semble_query_text_section "Wave ${CURRENT_WAVE} completion status:" "${WAVE_STATUS}" 2500' in text
	assert "append_semble_query_text_section 'Stall diagnostics JSON:' \"${diagnostics}\" 7000" in text


def test_review_blocked_judge_wires_semble_prefetch_through_support_scripts() -> None:
	text = _read(REVIEW_RB_JUDGE)
	assert 'if [ -f "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh" ]' in text
	assert 'build_review_blocked_semble_prefetch() {' in text
	assert 'RB_JUDGE_SEMBLE_QUERY_FILE="${RUNTIME_DIR}/rb_judge_semble_query.txt"' in text
	assert "append_semble_query_text_section 'PR diff:' \"${PR_DIFF}\" 6000" in text
	assert 'SEMBLE_PREFETCH="$(cat "${RB_JUDGE_SEMBLE_CONTEXT_FILE}" 2>/dev/null || true)"' in text
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"' in text


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

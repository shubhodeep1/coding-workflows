#!/usr/bin/env python3
"""Contract tests for judge-family Semble prefetch wiring."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROMPT = REPO_ROOT / "scripts" / "render_prompt.sh"
ORCHESTRATE_POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
ORCHESTRATE_POLL_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
REVIEW_RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
JUDGE_PROMPT = REPO_ROOT / "prompts" / "mode-judge.txt"
STALL_PROMPT = REPO_ROOT / "prompts" / "mode-judge-stall-recovery.txt"
REVIEW_BLOCKED_PROMPT = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"

_UNSET = object()


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


def _function_block(text: str, start_marker: str, end_marker: str) -> str:
	start = text.find(start_marker)
	assert start != -1, f"Missing function start: {start_marker}"
	end = text.find(end_marker, start + len(start_marker))
	assert end != -1, f"Missing function end marker after: {start_marker}"
	return text[start:end].rstrip()


def _render_prompt(template_text: str, *, semble_prefetch: object = _UNSET) -> subprocess.CompletedProcess[str]:
	with tempfile.TemporaryDirectory() as td:
		prompt_path = Path(td) / "prompt.txt"
		prompt_path.write_text(template_text, encoding="utf-8")
		env = dict(os.environ)
		if semble_prefetch is _UNSET:
			env.pop("SEMBLE_PREFETCH", None)
		else:
			env["SEMBLE_PREFETCH"] = str(semble_prefetch)
		return subprocess.run(
			["bash", str(RENDER_PROMPT), str(prompt_path)],
			cwd=str(REPO_ROOT),
			env=env,
			capture_output=True,
			text=True,
			check=False,
		)


def _run_bash(command: str, *, env_updates: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	env = dict(os.environ)
	if env_updates:
		env.update(env_updates)
	return subprocess.run(
		["bash", "-lc", command],
		cwd=str(REPO_ROOT),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def _run_large_pipe(function_text: str, invocation: str, *, env_updates: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	command = (
		"set -euo pipefail\n"
		f"{function_text}\n"
		f"python3 - <<'PY' | {invocation}\n"
		"import sys\n"
		"sys.stdout.write('diff --git a/src/app.py b/src/app.py\\n')\n"
		"sys.stdout.write('Use `scripts/foo.sh` here\\n')\n"
		"sys.stdout.write('Meaningful context line for Semble\\n')\n"
		"sys.stdout.write('x' * 200000)\n"
		"PY\n"
	)
	return _run_bash(command, env_updates=env_updates)


def test_render_prompt_replaces_semble_prefetch_when_value_is_supplied() -> None:
	result = _render_prompt(
		"before\n{{SEMBLE_PREFETCH}}\nafter\n",
		semble_prefetch="=== SEMBLE: Judge Context ===\nretrieved context\n=== END SEMBLE ===",
	)

	assert result.returncode == 0, result.stderr
	assert "{{SEMBLE_PREFETCH}}" not in result.stdout
	assert "retrieved context" in result.stdout

	empty_result = _render_prompt("before\n{{SEMBLE_PREFETCH}}\nafter\n", semble_prefetch="")
	assert empty_result.returncode == 0, empty_result.stderr
	assert empty_result.stdout == "before\n\nafter\n"


def test_render_prompt_rejects_unresolved_semble_prefetch_placeholder() -> None:
	result = _render_prompt("before\n{{SEMBLE_PREFETCH}}\nafter\n")

	assert result.returncode != 0, "render_prompt.sh should fail when SEMBLE_PREFETCH is not supplied"
	assert "Unresolved SEMBLE_PREFETCH placeholder" in result.stderr


def test_orchestrate_poll_workflow_bootstraps_semble_for_judge_paths() -> None:
	workflow = _read(ORCHESTRATE_POLL_WORKFLOW)
	init_block = _step_block(workflow, "Create runtime workspace")
	stage_block = _step_block(workflow, "Stage workflow support files")
	uv_block = _step_block(workflow, "Setup uv for Semble")
	install_block = _step_block(workflow, "Install semble")
	index_block = _step_block(workflow, "Build semble index")

	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}" in workflow
	assert 'echo "SEMBLE_AVAILABLE=false"' in init_block
	assert 'echo "SEMBLE_BIN="' in init_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in init_block
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in init_block
	assert 'OPTIONAL_BOOTSTRAP_SCRIPTS="install_semble.sh semble_helpers.sh"' in stage_block
	assert 'install -m 0755 "${src}" "scripts/${f}"' in stage_block
	assert "env.SEMBLE_ENABLED == 'true'" in uv_block
	assert "astral-sh/setup-uv@v3" in uv_block
	assert 'source scripts/install_semble.sh' in install_block
	assert 'echo "SEMBLE_BIN=${SEMBLE_BIN_PATH}" >> "$GITHUB_ENV"' in install_block
	assert '"${SEMBLE_BIN_PATH}" index . --out "${SEMBLE_INDEX_PATH}"' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=true" >> "$GITHUB_ENV"' in index_block


def test_judge_templates_include_semble_prefetch_near_the_header() -> None:
	judge = _read(JUDGE_PROMPT)
	stall = _read(STALL_PROMPT)
	review_blocked = _read(REVIEW_BLOCKED_PROMPT)

	assert "{{SEMBLE_PREFETCH}}" in judge
	assert judge.index("{{SEMBLE_PREFETCH}}") < judge.index("Evaluation criteria:")
	assert "{{SEMBLE_PREFETCH}}" in stall
	assert stall.index("{{SEMBLE_PREFETCH}}") < stall.index("Context provided in the prompt includes:")
	assert "{{SEMBLE_PREFETCH}}" in review_blocked
	assert review_blocked.index("{{SEMBLE_PREFETCH}}") < review_blocked.index("Inspect the PR diff")


def test_orchestrate_poll_process_wires_semble_into_all_live_judge_paths() -> None:
	script = _read(ORCHESTRATE_POLL_SCRIPT)

	assert 'if [ -f "scripts/semble_helpers.sh" ]' in script
	assert "build_judge_semble_prefetch()" in script
	assert 'python3 /dev/fd/3 "${label}" 3<<\'PY\'' in script
	assert 'SEMBLE_PREFETCH="${stall_judge_semble_prefetch}" bash scripts/render_prompt.sh prompts/mode-judge-stall-recovery.txt' in script
	assert 'SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge-review-blocked.txt' in script
	assert 'SEMBLE_PREFETCH="${JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge.txt' in script
	assert 'printf \'%s\\n\\n\' "${integration_judge_semble_prefetch}"' in script
	assert 'build_judge_semble_prefetch "integration conflict final pr ${final_pr} ${integration_branch} ${default_branch}" 2 "Integration Conflict Context"' in script


def test_orchestrate_poll_query_builder_reads_piped_context() -> None:
	script = _read(ORCHESTRATE_POLL_SCRIPT)
	function_text = _function_block(script, "build_judge_semble_query() {", "\n\nbuild_judge_semble_prefetch() {")
	pipe_text = "diff --git a/src/app.py b/src/app.py\nUse `scripts/foo.sh` here\nMeaningful context line for Semble\n"
	command = (
		f"{function_text}\n"
		f"cat <<'EOF' | build_judge_semble_query {shlex.quote('Judge Context')}\n"
		f"{pipe_text}EOF\n"
	)
	result = subprocess.run(
		["bash", "-lc", command],
		cwd=str(REPO_ROOT),
		capture_output=True,
		text=True,
		check=False,
	)

	assert result.returncode == 0, result.stderr
	assert "src/app.py" in result.stdout
	assert "scripts/foo.sh" in result.stdout
	assert "Meaningful context line for Semble" in result.stdout


def test_orchestrate_poll_query_builder_drains_large_piped_context() -> None:
	script = _read(ORCHESTRATE_POLL_SCRIPT)
	function_text = _function_block(script, "build_judge_semble_query() {", "\n\nbuild_judge_semble_prefetch() {")
	result = _run_large_pipe(function_text, f"build_judge_semble_query {shlex.quote('Judge Context')}")

	assert result.returncode == 0, result.stderr
	assert "src/app.py" in result.stdout
	assert "scripts/foo.sh" in result.stdout


def test_orchestrate_poll_prefetch_degrades_cleanly_without_helpers() -> None:
	script = _read(ORCHESTRATE_POLL_SCRIPT)
	function_text = _function_block(script, "build_judge_semble_prefetch() {", "\n\n# ---------------------------------------------------------------")
	result = _run_large_pipe(
		function_text,
		f"build_judge_semble_prefetch {shlex.quote('Judge Context')} 3 {shlex.quote('Judge Context')}",
		env_updates={"JUDGE_SEMBLE_HELPERS_AVAILABLE": "false"},
	)

	assert result.returncode == 0, result.stderr
	assert result.stdout == ""


def test_review_rb_judge_wires_semble_prefetch_from_support_scripts() -> None:
	script = _read(REVIEW_RB_JUDGE)

	assert 'if [ -f "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh" ]' in script
	assert 'source "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh"' in script
	assert "build_rb_judge_semble_prefetch()" in script
	assert 'python3 /dev/fd/3 "${label}" 3<<\'PY\'' in script
	assert 'SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"' in script
	assert 'build_rb_judge_semble_prefetch "review blocked judge pr ${PR_NUMBER} issue ${FIRST_ISSUE:-none}" 3 "Review-Blocked Context"' in script


def test_review_rb_judge_query_builder_reads_piped_context() -> None:
	script = _read(REVIEW_RB_JUDGE)
	function_text = _function_block(script, "build_rb_judge_semble_query() {", "\n\nbuild_rb_judge_semble_prefetch() {")
	pipe_text = "diff --git a/src/review.py b/src/review.py\nCheck `scripts/bar.sh` output\nReview-blocked context line\n"
	command = (
		f"{function_text}\n"
		f"cat <<'EOF' | build_rb_judge_semble_query {shlex.quote('Review Blocked Context')}\n"
		f"{pipe_text}EOF\n"
	)
	result = subprocess.run(
		["bash", "-lc", command],
		cwd=str(REPO_ROOT),
		capture_output=True,
		text=True,
		check=False,
	)

	assert result.returncode == 0, result.stderr
	assert "src/review.py" in result.stdout
	assert "scripts/bar.sh" in result.stdout
	assert "Review-blocked context line" in result.stdout


def test_review_rb_judge_query_builder_drains_large_piped_context() -> None:
	script = _read(REVIEW_RB_JUDGE)
	function_text = _function_block(script, "build_rb_judge_semble_query() {", "\n\nbuild_rb_judge_semble_prefetch() {")
	result = _run_large_pipe(function_text, f"build_rb_judge_semble_query {shlex.quote('Review Blocked Context')}")

	assert result.returncode == 0, result.stderr
	assert "src/app.py" in result.stdout
	assert "scripts/foo.sh" in result.stdout


def test_review_rb_judge_prefetch_degrades_cleanly_without_helpers() -> None:
	script = _read(REVIEW_RB_JUDGE)
	function_text = _function_block(script, "build_rb_judge_semble_prefetch() {", "\n\nif [ -f \"${SUPPORT_SCRIPTS_DIR}/label_helpers.sh\" ]")
	result = _run_large_pipe(
		function_text,
		f"build_rb_judge_semble_prefetch {shlex.quote('Review Blocked Context')} 3 {shlex.quote('Review-Blocked Context')}",
		env_updates={"RB_JUDGE_SEMBLE_HELPERS_AVAILABLE": "false"},
	)

	assert result.returncode == 0, result.stderr
	assert result.stdout == ""


def main() -> int:
	test_render_prompt_replaces_semble_prefetch_when_value_is_supplied()
	test_render_prompt_rejects_unresolved_semble_prefetch_placeholder()
	test_orchestrate_poll_workflow_bootstraps_semble_for_judge_paths()
	test_judge_templates_include_semble_prefetch_near_the_header()
	test_orchestrate_poll_process_wires_semble_into_all_live_judge_paths()
	test_orchestrate_poll_query_builder_reads_piped_context()
	test_orchestrate_poll_query_builder_drains_large_piped_context()
	test_orchestrate_poll_prefetch_degrades_cleanly_without_helpers()
	test_review_rb_judge_wires_semble_prefetch_from_support_scripts()
	test_review_rb_judge_query_builder_reads_piped_context()
	test_review_rb_judge_query_builder_drains_large_piped_context()
	test_review_rb_judge_prefetch_degrades_cleanly_without_helpers()
	print("OK: judge-family Semble prefetch contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

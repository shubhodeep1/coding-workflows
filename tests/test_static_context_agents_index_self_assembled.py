"""Self-assembled static prompts index the agents file by default (issue #8269).

The orchestrate decomposer, clarify-respond, the poller judges and validation
build their own static prefix instead of calling build_static_context.sh for
it. In index mode they list the agents file's sections; in inline mode they
keep the old output. Each site keeps its own agents.md / AGENTS.md order.

The workflow steps are extracted as text, so this file runs without PyYAML.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_static_context.sh"
ASSEMBLER = REPO_ROOT / "scripts" / "assemble_agents.py"
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
VALIDATE = REPO_ROOT / "scripts" / "validate_process.sh"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW_ENV_LINE = "STATIC_CONTEXT_AGENTS_MODE: ${{ vars.STATIC_CONTEXT_AGENTS_MODE || 'index' }}"
LOWER_TEXT = "# lower\n\n## Lower Section\nLOWER_BODY_SENTINEL\n"
UPPER_TEXT = "# upper\n\n## Upper Section\nUPPER_BODY_SENTINEL\n"
ORCHESTRATE_STEP = "Pre-assemble static context"
CLARIFY_RESPOND_STEP = "Pre-assemble static context (cacheable across runs)"

# site -> (phase value in STATIC_CONTEXT_SIZE, agents file read first)
SITES = {
	"orchestrate": ("orchestrate", "agents.md"),
	"clarify_respond": ("clarify_respond", "AGENTS.md"),
	"judge": ("judge", "AGENTS.md"),
	"validate": ("validate", "AGENTS.md"),
}


def _safe_env(**extra: str) -> dict[str, str]:
	env = os.environ.copy()
	for key in (
		"BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE",
		"GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
		"GIT_ALTERNATE_OBJECT_DIRECTORIES", "STATIC_CONTEXT_AGENTS_MODE", "RUNTIME_DIR",
	):
		env.pop(key, None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.update(extra)
	return env


def _step_block(workflow: str, step_name: str) -> list[str]:
	lines = (WORKFLOWS / workflow).read_text(encoding="utf-8").splitlines()
	for index, line in enumerate(lines):
		if line.strip() == f"- name: {step_name}":
			indent = len(line) - len(line.lstrip())
			block = [line]
			for following in lines[index + 1:]:
				stripped = following.lstrip()
				following_indent = len(following) - len(stripped)
				if stripped and following_indent <= indent:
					break
				block.append(following)
			return block
	raise AssertionError(f"step {step_name!r} not found in {workflow}")


def _step_run(workflow: str, step_name: str) -> str:
	block = _step_block(workflow, step_name)
	for index, line in enumerate(block):
		if re.match(r"^\s+run: \|$", line):
			run_indent = len(line) - len(line.lstrip())
			body = []
			for following in block[index + 1:]:
				stripped = following.lstrip()
				if stripped and len(following) - len(stripped) <= run_indent:
					break
				body.append(following)
			return textwrap.dedent("\n".join(body)) + "\n"
	raise AssertionError(f"run block of {step_name!r} not found")


def _shell_function(text: str, name: str) -> str:
	lines = text.splitlines()
	start = lines.index(f"{name}() {{")
	end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
	return "\n".join(lines[start:end + 1]) + "\n"


def _validate_block() -> str:
	lines = VALIDATE.read_text(encoding="utf-8").splitlines()
	start = next(i for i, line in enumerate(lines) if line.startswith('VALIDATE_README_CONTEXT="$(mktemp'))
	end = next(i for i in range(start, len(lines)) if lines[i] == 'echo "${VALIDATE_STATIC_SIZE_LINE}" >&2')
	return "\n".join(lines[start:end + 1]) + "\n"


def _repo(tmp: str, script_source: Path = SCRIPT, lower: bool = True, upper: bool = False) -> Path:
	root = Path(tmp)
	(root / "scripts").mkdir()
	shutil.copy2(script_source, root / "scripts" / "build_static_context.sh")
	shutil.copy2(SCRIPT, root / "scripts" / "real_build_static_context.sh")
	shutil.copy2(ASSEMBLER, root / "scripts" / "assemble_agents.py")
	(root / "unattended_system_instructions.md").write_text("system rules\n", encoding="utf-8")
	(root / "ai_pipeline.md").write_text("pipeline rules\n", encoding="utf-8")
	(root / "README.md").write_text("# Readme\ntext\n", encoding="utf-8")
	(root / "runtime").mkdir()
	if lower:
		(root / "agents.md").write_text(LOWER_TEXT, encoding="utf-8")
	if upper:
		(root / "AGENTS.md").write_text(UPPER_TEXT, encoding="utf-8")
	return root


def _run_site(site: str, root: Path, mode: str | None = None, judge_override: str = "") -> tuple[str, str]:
	env = _safe_env(RUNNER_TEMP=str(root / "runtime"), RUNTIME_DIR=str(root / "runtime"))
	if mode is not None:
		env["STATIC_CONTEXT_AGENTS_MODE"] = mode
	if site == "orchestrate":
		script, out = _step_run("orchestrate.yml", ORCHESTRATE_STEP), root / "pre_assembled_static.txt"
	elif site == "clarify_respond":
		script, out = _step_run("orchestrate_clarify_respond.yml", CLARIFY_RESPOND_STEP), root / "pre_assembled_static.txt"
	elif site == "judge":
		poller = POLLER.read_text(encoding="utf-8")
		out = root / "judge_static.txt"
		script = (
			"set -euo pipefail\n"
			+ _shell_function(poller, "_judge_static_agents_mode")
			+ _shell_function(poller, "assemble_judge_static_context")
			+ f'assemble_judge_static_context "{out}" {judge_override}\n'
		)
	elif site == "validate":
		out = root / "static_context.txt"
		env["STATIC_CONTEXT_FILE"] = str(out)
		script = "set -euo pipefail\n" + _validate_block()
	else:
		raise AssertionError(site)
	result = subprocess.run(
		["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True, check=False,
	)
	assert result.returncode == 0, result.stderr
	return out.read_text(encoding="utf-8"), result.stderr


def _size_lines(stderr: str) -> list[str]:
	return [line for line in stderr.splitlines() if line.startswith("STATIC_CONTEXT_SIZE ")]


@pytest.mark.parametrize("site", sorted(SITES))
@pytest.mark.parametrize("mode", [None, "", "index", "INDEX"])
def test_index_mode_lists_sections_instead_of_the_file(site: str, mode: str | None) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		text, stderr = _run_site(site, root, mode)
		assert "=== AGENTS.MD INDEX ===\n" in text
		assert "The full agents.md is not inlined." in text
		assert "- L3: Lower Section\n" in text
		assert "=== AGENTS.MD ===" not in text
		assert "LOWER_BODY_SENTINEL" not in text
		assert "system rules" in text and "UNTRUSTED_DATA: # Readme" in text
		phase = SITES[site][0]
		assert re.fullmatch(rf"STATIC_CONTEXT_SIZE phase={phase} mode=index bytes=\d+", _size_lines(stderr)[0])
		assert len(_size_lines(stderr)) == 1
		assert "::warning::" not in stderr


@pytest.mark.parametrize("site", sorted(SITES))
def test_inline_mode_keeps_the_full_file(site: str) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		text, stderr = _run_site(site, root, "inline")
		assert "=== AI PIPELINE ===\npipeline rules\n\n=== AGENTS.MD ===\n" + LOWER_TEXT + "\nThe README section below" in text
		assert "AGENTS.MD INDEX" not in text
		phase = SITES[site][0]
		assert re.fullmatch(rf"STATIC_CONTEXT_SIZE phase={phase} mode=inline bytes=\d+", _size_lines(stderr)[0])
		assert "::warning::" not in stderr


@pytest.mark.parametrize("site", sorted(SITES))
def test_unrecognised_mode_inlines_with_one_warning(site: str) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		text, stderr = _run_site(site, root, "bogus-SECRET-value")
		assert "=== AGENTS.MD ===\n" + LOWER_TEXT in text
		assert "AGENTS.MD INDEX" not in text
		assert stderr.count("::warning::STATIC_CONTEXT_AGENTS_MODE has an unrecognised value; using inline.") == 1
		assert "bogus-SECRET-value" not in stderr


@pytest.mark.parametrize("site", sorted(SITES))
def test_file_precedence_matches_each_site(site: str) -> None:
	preferred = SITES[site][1]
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, lower=True, upper=True)
		if not (root / "AGENTS.md").exists() or (root / "AGENTS.md").read_text(encoding="utf-8") != UPPER_TEXT:
			pytest.skip("case-insensitive file system: agents.md and AGENTS.md are one file")
		text, _ = _run_site(site, root)
		if preferred == "AGENTS.md":
			assert "The full AGENTS.md is not inlined." in text and "Upper Section" in text
			assert "Lower Section" not in text
		else:
			assert "The full agents.md is not inlined." in text and "Lower Section" in text
			assert "Upper Section" not in text
		text, _ = _run_site(site, root, "inline")
		expected, other = (UPPER_TEXT, LOWER_TEXT) if preferred == "AGENTS.md" else (LOWER_TEXT, UPPER_TEXT)
		assert "=== AGENTS.MD ===\n" + expected in text
		assert other not in text


@pytest.mark.parametrize("site", sorted(SITES))
def test_failed_index_falls_back_to_inline(site: str) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		stub = Path(tmp) / "stub.sh"
		stub.write_text(
			'#!/usr/bin/env bash\n'
			'if [ "${1:-}" = agents-index ]; then exit 2; fi\n'
			'exec bash "$(dirname "${BASH_SOURCE[0]}")/real_build_static_context.sh" "$@"\n',
			encoding="utf-8",
		)
		repo_dir = Path(tmp) / "repo"
		repo_dir.mkdir()
		root = _repo(str(repo_dir), script_source=stub)
		text, stderr = _run_site(site, root)
		assert "=== AGENTS.MD ===\n" + LOWER_TEXT in text
		assert "AGENTS.MD INDEX" not in text
		phase = SITES[site][0]
		assert re.fullmatch(
			rf"STATIC_CONTEXT_SIZE phase={phase} mode=inline bytes=\d+ fallback=inline reason=index_unavailable",
			_size_lines(stderr)[0],
		)
		assert "::warning::Static context agents index unavailable; inlining agents.md." in stderr


@pytest.mark.parametrize("site", sorted(SITES))
def test_symlinked_agents_file_is_never_read(site: str) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, lower=False)
		secret = root / "secret.txt"
		secret.write_text("## x-access-token:SENTINEL_SECRET\n", encoding="utf-8")
		for name in ("AGENTS.md", "agents.md"):
			try:
				(root / name).symlink_to(secret)
			except FileExistsError:
				pass  # case-insensitive file system
		for mode in ("index", "inline"):
			text, stderr = _run_site(site, root, mode)
			assert "SENTINEL_SECRET" not in text + stderr
			assert _size_lines(stderr)[0].endswith(" agents=absent")


@pytest.mark.parametrize("site", sorted(SITES))
def test_missing_agents_file_logs_absent(site: str) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, lower=False)
		text, stderr = _run_site(site, root)
		assert "AGENTS.MD" not in text
		assert _size_lines(stderr)[0].endswith(" agents=absent")


def test_judge_inline_override_inlines_in_index_mode() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		text, stderr = _run_site("judge", root, "index", judge_override="inline")
		assert "=== AGENTS.MD ===\n" + LOWER_TEXT in text
		assert "AGENTS.MD INDEX" not in text
		assert re.fullmatch(
			r"STATIC_CONTEXT_SIZE phase=judge mode=inline bytes=\d+ override=inline", _size_lines(stderr)[0],
		)


def test_review_blocked_and_integration_judges_stay_inline() -> None:
	poller = POLLER.read_text(encoding="utf-8")
	assert 'assemble_judge_static_context "${RUNTIME_DIR}/judge_static_rb.txt" inline' in poller
	assert 'if [ ! -s "${RUNTIME_DIR}/judge_static_rb.txt" ]; then' in poller
	assert 'cat "${RUNTIME_DIR}/judge_static_rb.txt"' in poller
	assert 'assemble_judge_static_context "${judge_static_file}" inline' in poller
	# The security-exhaustion judge runs in the audit worktree, so it inlines too.
	assert 'static_file="${RUNTIME_DIR}/judge_static_sec.txt"' in poller
	assert poller.count('assemble_judge_static_context "${static_file}" inline; then') == 1
	# The wave and stall judges follow the switch.
	assert poller.count('assemble_judge_static_context "${static_file}"; then') == 1
	assert 'assemble_judge_static_context "${RUNTIME_DIR}/judge_static.txt"\n' in poller


def _poller_steps() -> list[str]:
	text = (WORKFLOWS / "orchestrate_poll.yml").read_text(encoding="utf-8")
	names = re.findall(r"^      - name: (.+)$", text, flags=re.MULTILINE)
	# Steps that run the poller, either as a `run: |` block or a one-line `run:`.
	return [
		name for name in names
		if "bash scripts/orchestrate_poll_process.sh" in "\n".join(_step_block("orchestrate_poll.yml", name))
	]


def _env_lines(workflow: str, step_name: str) -> list[str]:
	block = _step_block(workflow, step_name)
	lines = []
	in_env = False
	for line in block:
		stripped = line.strip()
		if stripped == "env:":
			in_env = True
			continue
		if in_env and re.match(r"^\s{8}\S", line):
			break
		if in_env:
			lines.append(stripped)
	return lines


def test_workflows_forward_the_switch_with_index_fallback() -> None:
	poller_steps = _poller_steps()
	assert "Process each tracking issue" in poller_steps
	assert "Run standalone stall recovery without active projects" in poller_steps
	assert len(poller_steps) == 6, poller_steps
	targets = [
		("orchestrate.yml", ORCHESTRATE_STEP),
		("orchestrate_clarify_respond.yml", CLARIFY_RESPOND_STEP),
		("validate.yml", "Run validation process"),
	] + [("orchestrate_poll.yml", name) for name in poller_steps]
	for workflow, step in targets:
		assert WORKFLOW_ENV_LINE in _env_lines(workflow, step), (workflow, step)


def test_scripts_default_to_index() -> None:
	assert 'printf \'%s\' "${STATIC_CONTEXT_AGENTS_MODE:-}"' in VALIDATE.read_text(encoding="utf-8")
	assert 'printf \'%s\' "${STATIC_CONTEXT_AGENTS_MODE:-}"' in POLLER.read_text(encoding="utf-8")


def test_agents_fragment_documents_the_switch_and_log_prefixes() -> None:
	# Read agents.md with pending fragments folded in: the #8269 fragment is
	# deleted by the release fold, which also strips its section markers.
	sys.path.insert(0, str(Path(__file__).resolve().parent))
	from agents_doc import agents_text

	text = agents_text(REPO_ROOT)
	assert "\n## Isolated Codex agents\n" in text
	assert "\n## Stable log prefixes (contractual)\n" in text
	assert "LOG_PREFIX.name=STATIC_CONTEXT_SIZE" in text
	assert "LOG_PREFIX.name=STATIC_CONTEXT_AGENTS" in text
	assert "STATIC_CONTEXT_AGENTS_MODE" in text

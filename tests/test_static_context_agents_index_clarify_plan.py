"""clarify and plan index agents.md by default; inline keeps the old output (issue #8260)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_static_context.sh"
ASSEMBLER = REPO_ROOT / "scripts" / "assemble_agents.py"
WORKFLOW_ENV_VALUE = "${{ vars.STATIC_CONTEXT_AGENTS_MODE || 'index' }}"
AGENTS_TEXT = "# agents\n\n## Section A\nBODY_A_SENTINEL\n\n## Section B\nBODY_B_SENTINEL\n"
STEP_NAME = "Pre-assemble static context (cacheable across runs)"


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


def _repo(tmp: str, script_source: Path = SCRIPT, agents: bool = True) -> Path:
	root = Path(tmp)
	(root / "scripts").mkdir()
	shutil.copy2(script_source, root / "scripts" / "build_static_context.sh")
	shutil.copy2(ASSEMBLER, root / "scripts" / "assemble_agents.py")
	(root / "unattended_system_instructions.md").write_text("system rules\n", encoding="utf-8")
	(root / "ai_pipeline.md").write_text(
		"## Shared Runtime Behavior\nshared\n## Phase 1\none\n## Phase 2\ntwo\n## Phase 3\nthree\n## Failure Handling\nf\n",
		encoding="utf-8",
	)
	(root / "README.md").write_text("# Readme\ntext\n", encoding="utf-8")
	if agents:
		(root / "agents.md").write_text(AGENTS_TEXT, encoding="utf-8")
	return root


def _run(root: Path, phase: str, mode: str | None = None, **env: str) -> subprocess.CompletedProcess[str]:
	if mode is not None:
		env["STATIC_CONTEXT_AGENTS_MODE"] = mode
	return subprocess.run(
		["bash", "scripts/build_static_context.sh", phase, "out.txt"],
		cwd=root, env=_safe_env(**env), capture_output=True, text=True, check=False,
	)


def _size_lines(stderr: str) -> list[str]:
	return [line for line in stderr.splitlines() if line.startswith("STATIC_CONTEXT_SIZE ")]


@pytest.mark.parametrize("phase", ["clarify", "plan"])
@pytest.mark.parametrize("mode", [None, "", "index", "INDEX"])
def test_index_mode_replaces_inlined_agents(phase: str, mode: str | None) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		result = _run(root, phase, mode)
		assert result.returncode == 0, result.stderr
		data = (root / "out.txt").read_bytes()
		text = data.decode("utf-8")
		assert "=== AGENTS.MD INDEX ===\n" in text
		assert "- L3: Section A\n" in text and "- L6: Section B\n" in text
		assert "=== AGENTS.MD ===\n" not in text
		assert "BODY_A_SENTINEL" not in text and "BODY_B_SENTINEL" not in text
		assert "system rules" in text
		assert _size_lines(result.stderr) == [f"STATIC_CONTEXT_SIZE phase={phase} mode=index bytes={len(data)}"]
		assert "::warning::" not in result.stderr


def _expected_inline(phase: str) -> str:
	header = "Phase 1 — Clarification" if phase == "clarify" else "planning-trimmed"
	return f"=== AI PIPELINE ({header}) ===\n"


@pytest.mark.parametrize("phase", ["clarify", "plan"])
@pytest.mark.parametrize("mode", ["inline", "INLINE", "junk"])
def test_inline_mode_keeps_full_agents_block(phase: str, mode: str) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		result = _run(root, phase, mode)
		assert result.returncode == 0, result.stderr
		data = (root / "out.txt").read_bytes()
		text = data.decode("utf-8")
		assert "=== AGENTS.MD ===\n" + AGENTS_TEXT + "\n" in text
		assert _expected_inline(phase) in text
		assert "=== AGENTS.MD INDEX ===" not in text
		assert _size_lines(result.stderr) == [f"STATIC_CONTEXT_SIZE phase={phase} mode=inline bytes={len(data)}"]
		warnings = [line for line in result.stderr.splitlines() if line.startswith("::warning::")]
		assert len(warnings) == (1 if mode == "junk" else 0)
		if mode == "junk":
			assert "junk" not in result.stderr


@pytest.mark.parametrize("phase", ["clarify", "plan"])
def test_index_failure_falls_back_to_inline(phase: str) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		inline = _run(root, phase, "inline")
		assert inline.returncode == 0, inline.stderr
		expected = (root / "out.txt").read_bytes()
		shim_dir = root / "shim"
		shim_dir.mkdir()
		shim = shim_dir / "python3"
		# Fail only the index emitter (stdin script, `-` argument); the README
		# trimmer is a stdin script too, so key on the agents index arguments.
		real_python = shutil.which("python3")
		assert real_python
		shim.write_text(
			"#!/bin/sh\n"
			'for arg in "$@"; do case "$arg" in agents.md|AGENTS.md) exit 1 ;; esac; done\n'
			f'exec "{real_python}" "$@"\n',
			encoding="utf-8",
		)
		shim.chmod(0o755)
		result = _run(root, phase, None, PATH=f"{shim_dir}:{os.environ.get('PATH', '')}")
		assert result.returncode == 0, result.stderr
		data = (root / "out.txt").read_bytes()
		assert data == expected
		assert "::warning::Static context agents index unavailable; inlining agents.md." in result.stderr
		assert _size_lines(result.stderr) == [
			f"STATIC_CONTEXT_SIZE phase={phase} mode=inline bytes={len(data)} fallback=inline reason=index_unavailable"
		]


@pytest.mark.parametrize("phase", ["clarify", "plan"])
@pytest.mark.parametrize("mode", [None, "inline"])
def test_no_agents_file_logs_absent(phase: str, mode: str | None) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, agents=False)
		result = _run(root, phase, mode)
		assert result.returncode == 0, result.stderr
		data = (root / "out.txt").read_bytes()
		text = data.decode("utf-8")
		assert "=== AGENTS.MD" not in text
		effective = "inline" if mode == "inline" else "index"
		assert _size_lines(result.stderr) == [
			f"STATIC_CONTEXT_SIZE phase={phase} mode={effective} bytes={len(data)} agents=absent"
		]


def test_implement_phase_honours_switch() -> None:
	# Since #8262 implement indexes the local agents file too; inline mode keeps
	# the full block (tests/test_static_context_agents_index_implement.py).
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		outputs = {}
		for mode in (None, "index", "inline"):
			result = _run(root, "implement", mode)
			assert result.returncode == 0, result.stderr
			assert len(_size_lines(result.stderr)) == 1
			outputs[mode] = (root / "out.txt").read_bytes()
		assert outputs[None] == outputs["index"] != outputs["inline"]
		assert "=== AGENTS.MD INDEX ===" in outputs["index"].decode("utf-8")
		assert "BODY_A_SENTINEL" not in outputs["index"].decode("utf-8")
		inline_text = outputs["inline"].decode("utf-8")
		assert "=== REPO-SPECIFIC AGENTS.MD ===\n" + AGENTS_TEXT in inline_text
		assert "=== AGENTS.MD INDEX ===" not in inline_text


def _base_script() -> Path | None:
	"""The script from the merge base with the default branch, when git can find it."""
	for ref in ("origin/main", "isolated-base"):
		base = subprocess.run(
			["git", "merge-base", "HEAD", ref], cwd=REPO_ROOT, env=_safe_env(),
			capture_output=True, text=True, check=False,
		)
		if base.returncode != 0 or not base.stdout.strip():
			continue
		shown = subprocess.run(
			["git", "show", f"{base.stdout.strip()}:scripts/build_static_context.sh"], cwd=REPO_ROOT,
			env=_safe_env(), capture_output=True, check=False,
		)
		if shown.returncode != 0 or b"STATIC_CONTEXT_SIZE" in shown.stdout:
			continue
		with tempfile.NamedTemporaryFile(prefix="build_static_context_base", suffix=".sh", delete=False) as handle:
			handle.write(shown.stdout)
		return Path(handle.name)
	return None


@pytest.mark.parametrize("phase", ["clarify", "plan", "implement"])
def test_inline_mode_byte_identical_to_base(phase: str) -> None:
	base_script = _base_script()
	if base_script is None:
		pytest.skip("no base revision of build_static_context.sh without STATIC_CONTEXT_SIZE")
	try:
		with tempfile.TemporaryDirectory() as tmp_before, tempfile.TemporaryDirectory() as tmp_after:
			before_root = _repo(tmp_before, script_source=base_script)
			after_root = _repo(tmp_after)
			for root in (before_root, after_root):
				fragments = root / "agents.d"
				fragments.mkdir()
				(fragments / "1-x.md").write_text('<!-- agents: section="Section A" -->\nadded\n', encoding="utf-8")
			before = _run(before_root, phase, "inline")
			after = _run(after_root, phase, "inline")
			assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
			assert (before_root / "out.txt").read_bytes() == (after_root / "out.txt").read_bytes()
	finally:
		base_script.unlink()


@pytest.mark.parametrize(("workflow", "phase"), [("clarify.yml", "clarify"), ("plan.yml", "plan")])
def test_workflows_pass_the_switch_with_index_fallback(workflow: str, phase: str) -> None:
	yaml = pytest.importorskip("yaml")
	data = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
	steps = [
		step
		for job in data["jobs"].values()
		for step in job.get("steps", [])
		if step.get("name") == STEP_NAME
	]
	assert len(steps) == 1
	step = steps[0]
	assert step["env"]["STATIC_CONTEXT_AGENTS_MODE"] == WORKFLOW_ENV_VALUE
	assert f"bash scripts/build_static_context.sh {phase} ./pre_assembled_static.txt" in step["run"]


@pytest.mark.parametrize(
	"path",
	[REPO_ROOT / ".github" / "workflows" / "clarify.yml", REPO_ROOT / "scripts" / "run_plan_codex.sh"],
)
def test_prompts_allow_reading_indexed_agents_sections(path: Path) -> None:
	text = path.read_text(encoding="utf-8")
	assert "=== AGENTS.MD INDEX ===" in text
	assert "read only the agents.md sections" in text
	normalized = re.sub(r"\s+", " ", text)
	assert "Do NOT read pre_assembled_static.txt or any of the individual source files" not in normalized

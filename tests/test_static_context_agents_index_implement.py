"""The implement phase indexes agents.md by default; inline keeps the old output (issue #8262).

In index mode `scripts/build_static_context.sh implement` indexes the local
agents file (it is in the implement workspace copy) and, in consumer repos, the
canonical `${RUNTIME_DIR}/agents_canonical.md` when the isolated container can
read it at `/support/static-context/agents_canonical.md`. Whatever cannot be
indexed stays inlined, and inline mode is the pre-#8262 output byte for byte.
Check-failure triage keeps its inline canonical copy (plan D2).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_static_context.sh"
ASSEMBLER = REPO_ROOT / "scripts" / "assemble_agents.py"
SANDBOX_PATH = "/support/static-context/agents_canonical.md"
MARKER = "# supports: CODEX_ISOLATED_STATIC_CONTEXT_FILES"
STEP_NAME = "Pre-assemble static context (cacheable across runs)"
WORKFLOW_ENV_VALUE = "${{ vars.STATIC_CONTEXT_AGENTS_MODE || 'index' }}"
LOCAL_TEXT = "# local agents\n\n## Local A\nLOCAL_BODY_A\n\n## Local B\nLOCAL_BODY_B\n"
CANONICAL_TEXT = "# canonical agents\n\nintro\n## Canon One\nCANON_BODY_ONE\n\n## Canon Two\nCANON_BODY_TWO\n"

# .github/workflows/ci.yml runs this file directly; no other test module imports it.


def _safe_env(**extra: str) -> dict[str, str]:
	env = os.environ.copy()
	for key in list(env):
		if key.startswith(("STATIC_CONTEXT_", "GIT_", "CODEX_ISOLATED_")):
			env.pop(key, None)
	for key in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "RUNTIME_DIR", "HEAL_ROUTE", "IMPLEMENT_STAGED_SUPPORT_RUN_DIR", "GITHUB_ENV"):
		env.pop(key, None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.update(extra)
	return env


def _repo(tmp: str, script_source: Path = SCRIPT, local: bool = True, canonical: bool = False) -> Path:
	root = Path(tmp)
	(root / "scripts").mkdir()
	shutil.copy2(script_source, root / "scripts" / "build_static_context.sh")
	shutil.copy2(ASSEMBLER, root / "scripts" / "assemble_agents.py")
	(root / "unattended_system_instructions.md").write_text("system rules\n", encoding="utf-8")
	(root / "ai_pipeline.md").write_text(
		"## Shared Runtime Behavior\nshared\n## Phase 1\none\n## Phase 2\ntwo\n## Phase 3\nthree\n## Failure Handling\nf\n",
		encoding="utf-8",
	)
	(root / "runtime").mkdir()
	if local:
		(root / "agents.md").write_text(LOCAL_TEXT, encoding="utf-8")
	if canonical:
		(root / "runtime" / "agents_canonical.md").write_text(CANONICAL_TEXT, encoding="utf-8")
	return root


def _run(root: Path, mode: str | None = None, **env: str) -> subprocess.CompletedProcess[str]:
	if mode is not None:
		env["STATIC_CONTEXT_AGENTS_MODE"] = mode
	env.setdefault("RUNTIME_DIR", str(root / "runtime"))
	return subprocess.run(
		["bash", "scripts/build_static_context.sh", "implement", "out.txt"],
		cwd=root, env=_safe_env(**env), capture_output=True, text=True, check=False,
	)


def _out(root: Path) -> bytes:
	return (root / "out.txt").read_bytes()


def _size_lines(stderr: str) -> list[str]:
	return [line for line in stderr.splitlines() if line.startswith("STATIC_CONTEXT_SIZE ")]


def _inline_bytes(root: Path) -> bytes:
	result = _run(root, "inline")
	assert result.returncode == 0, result.stderr
	return _out(root)


def _python_shim(root: Path, fail_on: str) -> str:
	"""PATH entry whose python3 fails when an argument equals one of fail_on (space separated)."""
	real_python = shutil.which("python3")
	assert real_python
	shim_dir = root / "shim"
	shim_dir.mkdir(exist_ok=True)
	shim = shim_dir / "python3"
	patterns = "|".join(fail_on.split())
	shim.write_text(
		"#!/bin/sh\n"
		f'for arg in "$@"; do case "$arg" in {patterns}) exit 1 ;; esac; done\n'
		f'exec "{real_python}" "$@"\n',
		encoding="utf-8",
	)
	shim.chmod(0o755)
	return f"{shim_dir}:{os.environ.get('PATH', '')}"


@pytest.mark.parametrize("mode", [None, "", "index", "INDEX"])
def test_self_repo_index_mode_indexes_the_local_file(mode: str | None) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		result = _run(root, mode)
		assert result.returncode == 0, result.stderr
		data = _out(root)
		text = data.decode("utf-8")
		assert "=== AGENTS.MD INDEX ===\n" in text
		assert "- L3: Local A\n" in text and "- L6: Local B\n" in text
		assert "LOCAL_BODY_A" not in text and "LOCAL_BODY_B" not in text
		assert "=== AGENTS.MD ===" not in text and "=== REPO-SPECIFIC AGENTS.MD ===" not in text
		assert "CANONICAL AGENTS.MD INDEX" not in text
		assert _size_lines(result.stderr) == [f"STATIC_CONTEXT_SIZE phase=implement mode=index canonical=absent bytes={len(data)}"]
		assert "::warning::" not in result.stderr


def test_consumer_index_mode_indexes_both_files_with_the_sandbox_path() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, canonical=True)
		result = _run(root, STATIC_CONTEXT_CANONICAL_SANDBOX_PATH=SANDBOX_PATH)
		assert result.returncode == 0, result.stderr
		data = _out(root)
		text = data.decode("utf-8")
		canonical_at = text.index("=== CANONICAL AGENTS.MD INDEX (coding-workflows) ===\n")
		local_at = text.index("=== AGENTS.MD INDEX ===\n")
		assert canonical_at < local_at
		canonical_block = text[canonical_at:local_at]
		assert SANDBOX_PATH in canonical_block
		assert "read-only; never copy it into the working tree" in canonical_block
		assert "- L4: Canon One\n" in canonical_block and "- L7: Canon Two\n" in canonical_block
		assert "- L3: Local A\n" in text[local_at:]
		for body in ("CANON_BODY_ONE", "CANON_BODY_TWO", "LOCAL_BODY_A", "LOCAL_BODY_B"):
			assert body not in text
		assert str(root / "runtime") not in text
		assert "=== AGENTS.MD ===" not in text
		assert all(len(line) <= 200 for line in text.splitlines())
		assert _size_lines(result.stderr) == [f"STATIC_CONTEXT_SIZE phase=implement mode=index canonical=index bytes={len(data)}"]


@pytest.mark.parametrize(
	("reason_in", "reason_out"),
	[(None, "mount_unconfirmed"), ("helper_unsupported", "helper_unsupported"), ("Bad Reason!", "unknown")],
)
def test_consumer_without_a_mount_keeps_the_canonical_file_inline(reason_in: str | None, reason_out: str) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, canonical=True)
		env = {} if reason_in is None else {"STATIC_CONTEXT_CANONICAL_UNAVAILABLE_REASON": reason_in}
		result = _run(root, **env)
		assert result.returncode == 0, result.stderr
		data = _out(root)
		text = data.decode("utf-8")
		assert "=== AGENTS.MD ===\n" + CANONICAL_TEXT + "\n=== AGENTS.MD INDEX ===\n" in text
		assert "LOCAL_BODY_A" not in text and "CANONICAL AGENTS.MD INDEX" not in text
		assert _size_lines(result.stderr) == [
			f"STATIC_CONTEXT_SIZE phase=implement mode=index canonical=inline bytes={len(data)} canonical_reason={reason_out}"
		]
		assert "Bad Reason" not in result.stderr


@pytest.mark.parametrize("sandbox_path", ["/etc/passwd.md", "/support/static-context/../x.md", "/support/static-context/a b.md", "relative.md"])
def test_invalid_sandbox_path_keeps_the_canonical_file_inline(sandbox_path: str) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, canonical=True)
		result = _run(root, STATIC_CONTEXT_CANONICAL_SANDBOX_PATH=sandbox_path)
		assert result.returncode == 0, result.stderr
		text = _out(root).decode("utf-8")
		assert "=== AGENTS.MD ===\n" + CANONICAL_TEXT in text
		assert sandbox_path not in text
		assert "canonical=inline" in result.stderr and "canonical_reason=sandbox_path_invalid" in result.stderr


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("local", [False, True])
def test_inline_mode_matches_the_pre_index_layout(canonical: bool, local: bool) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, local=local, canonical=canonical)
		result = _run(root, "inline", STATIC_CONTEXT_CANONICAL_SANDBOX_PATH=SANDBOX_PATH)
		assert result.returncode == 0, result.stderr
		text = _out(root).decode("utf-8")
		expected = ""
		if canonical or local:
			expected = "=== AGENTS.MD ===\n"
			if canonical:
				expected += CANONICAL_TEXT + "\n"
			if local:
				expected += "=== REPO-SPECIFIC AGENTS.MD ===\n" + LOCAL_TEXT + "\n"
		assert expected in text
		assert ("=== AGENTS.MD ===" in text) == bool(expected)
		assert "INDEX ===" not in text
		state = "inline" if canonical else "absent"
		line = f"STATIC_CONTEXT_SIZE phase=implement mode=inline canonical={state} bytes={len(_out(root))}"
		if not local:
			line += " agents=absent"
		assert _size_lines(result.stderr) == [line]


def _base_script() -> Path | None:
	"""The script from the merge base with the default branch, before #8262."""
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
		if shown.returncode != 0 or b"STATIC_CONTEXT_CANONICAL_SANDBOX_PATH" in shown.stdout:
			continue
		with tempfile.NamedTemporaryFile(prefix="build_static_context_base", suffix=".sh", delete=False) as handle:
			handle.write(shown.stdout)
		return Path(handle.name)
	return None


@pytest.mark.parametrize("canonical", [False, True])
def test_implement_inline_mode_byte_identical_to_base(canonical: bool) -> None:
	base_script = _base_script()
	if base_script is None:
		pytest.skip("no base revision of build_static_context.sh before #8262")
	try:
		with tempfile.TemporaryDirectory() as tmp_before, tempfile.TemporaryDirectory() as tmp_after:
			before_root = _repo(tmp_before, script_source=base_script, canonical=canonical)
			after_root = _repo(tmp_after, canonical=canonical)
			for root in (before_root, after_root):
				fragments = root / "agents.d"
				fragments.mkdir()
				(fragments / "1-x.md").write_text('<!-- agents: section="Local A" -->\nadded\n', encoding="utf-8")
			before = _run(before_root, "inline", STATIC_CONTEXT_CANONICAL_SANDBOX_PATH=SANDBOX_PATH)
			after = _run(after_root, "inline", STATIC_CONTEXT_CANONICAL_SANDBOX_PATH=SANDBOX_PATH)
			assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
			assert _out(before_root) == _out(after_root)
	finally:
		base_script.unlink()


@pytest.mark.parametrize("canonical", [False, True])
def test_index_failures_everywhere_give_the_inline_output(canonical: bool) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, canonical=canonical)
		expected = _inline_bytes(root)
		path = _python_shim(root, "agents.md AGENTS.md --canonical")
		result = _run(root, PATH=path, STATIC_CONTEXT_CANONICAL_SANDBOX_PATH=SANDBOX_PATH)
		assert result.returncode == 0, result.stderr
		assert _out(root) == expected
		assert "::warning::Static context agents index unavailable; inlining agents.md." in result.stderr
		line = f"STATIC_CONTEXT_SIZE phase=implement mode=inline canonical={'inline' if canonical else 'absent'} bytes={len(expected)}"
		if canonical:
			line += " canonical_reason=index_unavailable"
		assert _size_lines(result.stderr) == [line + " fallback=inline reason=index_unavailable"]


def test_local_index_failure_with_a_canonical_index_inlines_only_the_local_file() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, canonical=True)
		path = _python_shim(root, "agents.md AGENTS.md")
		result = _run(root, PATH=path, STATIC_CONTEXT_CANONICAL_SANDBOX_PATH=SANDBOX_PATH)
		assert result.returncode == 0, result.stderr
		text = _out(root).decode("utf-8")
		assert "=== CANONICAL AGENTS.MD INDEX (coding-workflows) ===" in text
		assert "=== REPO-SPECIFIC AGENTS.MD ===\n" + LOCAL_TEXT + "\n" in text
		assert "CANON_BODY_ONE" not in text and "=== AGENTS.MD ===" not in text
		assert "mode=inline canonical=index" in result.stderr
		assert "fallback=inline reason=index_unavailable" in result.stderr


def test_canonical_index_failure_keeps_the_local_index() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, canonical=True)
		path = _python_shim(root, "--canonical")
		result = _run(root, PATH=path, STATIC_CONTEXT_CANONICAL_SANDBOX_PATH=SANDBOX_PATH)
		assert result.returncode == 0, result.stderr
		text = _out(root).decode("utf-8")
		assert "=== AGENTS.MD ===\n" + CANONICAL_TEXT + "\n=== AGENTS.MD INDEX ===\n" in text
		assert "LOCAL_BODY_A" not in text
		assert "mode=index canonical=inline" in result.stderr and "canonical_reason=index_unavailable" in result.stderr


def test_symlinked_canonical_file_is_not_indexed() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "canon-target.md").write_text(CANONICAL_TEXT, encoding="utf-8")
		(root / "runtime" / "agents_canonical.md").symlink_to(root / "canon-target.md")
		result = _run(root, STATIC_CONTEXT_CANONICAL_SANDBOX_PATH=SANDBOX_PATH)
		assert result.returncode == 0, result.stderr
		text = _out(root).decode("utf-8")
		assert "CANONICAL AGENTS.MD INDEX" not in text
		assert "canonical_reason=index_unavailable" in result.stderr


def test_agents_index_phase_is_unchanged_by_the_canonical_inputs() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp, canonical=True)
		outputs = []
		for env in ({}, {"STATIC_CONTEXT_CANONICAL_SANDBOX_PATH": SANDBOX_PATH, "RUNTIME_DIR": str(root / "runtime")}):
			result = subprocess.run(
				["bash", "scripts/build_static_context.sh", "agents-index", "out.txt"],
				cwd=root, env=_safe_env(**env), capture_output=True, text=True, check=False,
			)
			assert result.returncode == 0, result.stderr
			outputs.append(_out(root))
		assert outputs[0] == outputs[1]
		assert b"CANONICAL" not in outputs[0]


# --- implement.yml wiring ------------------------------------------------------------


def _implement_step() -> dict:
	yaml = pytest.importorskip("yaml")
	data = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "implement.yml").read_text(encoding="utf-8"))
	steps = [step for job in data["jobs"].values() for step in job.get("steps", []) if step.get("name") == STEP_NAME]
	assert len(steps) == 1
	return steps[0]


def test_implement_step_passes_the_switch_and_the_mount() -> None:
	step = _implement_step()
	assert step["env"]["STATIC_CONTEXT_AGENTS_MODE"] == WORKFLOW_ENV_VALUE
	run = step["run"]
	assert "bash scripts/build_static_context.sh implement ./pre_assembled_static.txt" in run
	assert 'HEAL_ROUTE:-false}" = true' in run
	assert f"grep -qxF '{MARKER}'" in run
	assert '"${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/codex_isolated_exec.sh"' in run
	assert f"STATIC_CONTEXT_CANONICAL_SANDBOX_PATH={SANDBOX_PATH}" in run
	assert 'echo "CODEX_ISOLATED_STATIC_CONTEXT_FILES=${RUNTIME_DIR}/agents_canonical.md" >> "$GITHUB_ENV"' in run


def _run_step(tmp: Path, *, canonical: bool, helper: str | None, heal: bool = False, mode: str = "index") -> tuple[dict[str, str], str, str]:
	"""Run the step body with a stub builder that records its environment."""
	(tmp / "scripts").mkdir()
	(tmp / "scripts" / "build_static_context.sh").write_text(
		"#!/bin/bash\n"
		'printf "MODE=%s\\nSANDBOX=%s\\nREASON=%s\\n" "${STATIC_CONTEXT_AGENTS_MODE:-}" '
		'"${STATIC_CONTEXT_CANONICAL_SANDBOX_PATH:-}" "${STATIC_CONTEXT_CANONICAL_UNAVAILABLE_REASON:-}" > builder.env\n',
		encoding="utf-8",
	)
	runtime = tmp / "runtime"
	runtime.mkdir()
	if canonical:
		(runtime / "agents_canonical.md").write_text(CANONICAL_TEXT, encoding="utf-8")
	run_dir = tmp / "run-dir"
	run_dir.mkdir()
	if helper is not None:
		(run_dir / "codex_isolated_exec.sh").write_text(helper, encoding="utf-8")
	github_env = tmp / "github_env"
	github_env.write_text("", encoding="utf-8")
	env = _safe_env(
		RUNTIME_DIR=str(runtime), GITHUB_ENV=str(github_env), IMPLEMENT_STAGED_SUPPORT_RUN_DIR=str(run_dir),
		STATIC_CONTEXT_AGENTS_MODE=mode,
	)
	if heal:
		env["HEAL_ROUTE"] = "true"
	result = subprocess.run(["bash", "-c", _implement_step()["run"]], cwd=tmp, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	recorded = dict(line.split("=", 1) for line in (tmp / "builder.env").read_text(encoding="utf-8").splitlines())
	return recorded, github_env.read_text(encoding="utf-8"), result.stdout


def test_step_mounts_the_canonical_file_when_the_helper_supports_it() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		recorded, github_env, _ = _run_step(tmp, canonical=True, helper=f"#!/bin/bash\n{MARKER}\n")
		assert recorded == {"MODE": "index", "SANDBOX": SANDBOX_PATH, "REASON": ""}
		assert github_env == f"CODEX_ISOLATED_STATIC_CONTEXT_FILES={tmp / 'runtime' / 'agents_canonical.md'}\n"


def test_step_keeps_the_canonical_file_inline_with_an_older_helper() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		recorded, github_env, _ = _run_step(Path(tmp_name), canonical=True, helper="#!/bin/bash\n# no marker\n")
		assert recorded == {"MODE": "index", "SANDBOX": "", "REASON": "helper_unsupported"}
		assert github_env == ""


@pytest.mark.parametrize("mode", ["inline", "INLINE", "junk"])
def test_step_does_not_mount_in_inline_mode(mode: str) -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		recorded, github_env, _ = _run_step(Path(tmp_name), canonical=True, helper=f"#!/bin/bash\n{MARKER}\n", mode=mode)
		assert recorded == {"MODE": mode, "SANDBOX": "", "REASON": ""}
		assert github_env == ""


def test_step_exports_nothing_in_the_self_repo() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		recorded, github_env, _ = _run_step(Path(tmp_name), canonical=False, helper=f"#!/bin/bash\n{MARKER}\n")
		assert recorded == {"MODE": "index", "SANDBOX": "", "REASON": ""}
		assert github_env == ""


def test_step_forces_inline_on_the_heal_route() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		recorded, github_env, stdout = _run_step(Path(tmp_name), canonical=True, helper=f"#!/bin/bash\n{MARKER}\n", heal=True)
		assert recorded == {"MODE": "inline", "SANDBOX": "", "REASON": ""}
		assert github_env == ""
		assert "STATIC_CONTEXT_AGENTS implement mode=inline reason=heal_route" in stdout


def test_isolation_helper_advertises_the_static_context_marker() -> None:
	helper = REPO_ROOT / "scripts" / "codex_isolated_exec.sh"
	assert MARKER in helper.read_text(encoding="utf-8").splitlines()
	assert subprocess.run(["bash", "-n", str(helper)], check=False).returncode == 0


# --- check-failure triage (plan D2) --------------------------------------------------


def test_triage_keeps_the_canonical_file_inline() -> None:
	text = (REPO_ROOT / "scripts" / "check_failure_triage.sh").read_text(encoding="utf-8")
	assert "=== REPO ARCHITECTURE (coding-workflows canonical) ===" in text
	assert "agents_canonical.md" in text
	assert "STATIC_CONTEXT_AGENTS_MODE" not in text
	workflow = (REPO_ROOT / ".github" / "workflows" / "check_failure_triage.yml").read_text(encoding="utf-8")
	assert "STATIC_CONTEXT_AGENTS_MODE" not in workflow

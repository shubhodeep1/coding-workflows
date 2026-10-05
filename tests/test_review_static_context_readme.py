"""Keep PR-controlled README content out of privileged review prompt reads."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_autofix_step_scripts import expanded_review_autofix_text  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts/review_untrusted_workspace.py"


def run_readme(root: Path, env=None):
	return subprocess.run(
		[sys.executable, str(HELPER), "readme-trimmed", str(root)],
		capture_output=True, timeout=5, env=env, check=False,
	)


@pytest.mark.parametrize("content", [
	b"Before\n\n### 2. Create wrapper workflows\nAfter\n",
	b"Before\n### 2. Create wrapper workflows extra\nAfter\n",
	b"Before\nNo marker\n",
	b"Before\nNo marker",
	b"\n\nBefore\n",
	b"\xff\n",
	b"",
])
def test_trim_matches_awk(tmp_path, content):
	(tmp_path / "README.md").write_bytes(content)
	result = run_readme(tmp_path)
	awk = subprocess.run(
		["awk", r"/^### 2\. Create wrapper workflows/{exit} {print}"],
		input=content, capture_output=True, check=True,
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout == awk.stdout
	assert result.stderr == b""


def test_real_readme_matches_awk():
	content = (ROOT / "README.md").read_bytes()
	result = run_readme(ROOT)
	awk = subprocess.run(
		["awk", r"/^### 2\. Create wrapper workflows/{exit} {print}"],
		input=content, capture_output=True, check=True,
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout == awk.stdout


def test_missing_readme(tmp_path):
	result = run_readme(tmp_path)
	assert result.returncode == 0
	assert result.stdout == b""


@pytest.mark.parametrize("target", ["file", "environ"])
def test_symlink_never_reads_target(tmp_path, target):
	secret = "review-readme-secret-sentinel"
	if target == "file":
		outside = tmp_path.parent / "private-readme-target"
		outside.write_text(secret)
	else:
		outside = Path("/proc/self/environ")
	(tmp_path / "README.md").symlink_to(outside)
	result = run_readme(tmp_path, {**os.environ, "STATIC_README_SECRET_SENTINEL": secret})
	assert result.returncode == 3
	assert result.stdout == b""
	assert b"reason=symlink_path" in result.stderr
	assert secret.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["fifo", "directory", "oversize"])
def test_non_regular_and_oversize_readmes_are_rejected(tmp_path, kind):
	path = tmp_path / "README.md"
	if kind == "fifo":
		os.mkfifo(path)
	elif kind == "directory":
		path.mkdir()
	else:
		path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
	result = run_readme(tmp_path)
	assert result.returncode == 3
	assert result.stdout == b""
	assert b"reason=unsafe_file" in result.stderr


@pytest.mark.parametrize("action", ["snapshot", "refresh", "transfer", "readme-trimmed"])
def test_subcommand_bad_arity_still_exits_two(tmp_path, action):
	result = subprocess.run(
		[sys.executable, str(HELPER), action, str(tmp_path), str(tmp_path)],
		capture_output=True, check=False, timeout=5,
	)
	assert result.returncode == 2


def test_readme_reader_reports_os_error_without_detail(monkeypatch, tmp_path, capsys):
	spec = importlib.util.spec_from_file_location("review_untrusted_workspace_readme", HELPER)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	monkeypatch.setattr(sys, "argv", [str(HELPER), "readme-trimmed", str(tmp_path)])
	monkeypatch.setattr(module, "readme_trimmed", lambda host: (_ for _ in ()).throw(OSError("sensitive path")))
	with pytest.raises(SystemExit) as error:
		module.main()
	assert error.value.code == 1
	assert "sensitive path" not in capsys.readouterr().err


def test_workflow_reads_only_verified_readme_and_refuses_output_symlinks():
	workflow = expanded_review_autofix_text()
	step = workflow.split("- name: Pre-assemble static context (cacheable across runs)", 1)[1].split("\n      - name:", 1)[0]
	assert '"${SUPPORT_SCRIPTS_DIR}/review_untrusted_workspace.py" readme-trimmed "${PWD}"' in step
	assert "awk '/^### 2\\. Create wrapper workflows/{exit} {print}' README.md" not in step
	assert '[ -L ./pre_assembled_static.txt ]' in step
	assert '[ ! -f ./pre_assembled_static.txt ]' in step
	assert 'if [ "${readme_rc}" -eq 3 ]; then' in step
	assert 'elif [ "${readme_rc}" -ne 0 ]; then' in step
	assert "outcome=omitted reason=not_regular_file" in step
	assert 'cat "${RUNTIME_DIR}/static_readme_trimmed.txt"' in step
	assert "exit 1" in step


def run_static_context_step(tmp_path):
	workflow = expanded_review_autofix_text()
	step = workflow.split("      - name: Pre-assemble static context (cacheable across runs)\n", 1)[1].split("\n      - name:", 1)[0]
	body = "\n".join(line[10:] for line in step.splitlines()[1:])
	instructions = tmp_path.parent / "static-instructions.txt"
	instructions.write_text("trusted instructions\n")
	runtime = tmp_path.parent / "static-runtime"
	runtime.mkdir(exist_ok=True)
	# The implementation runner sources BASH_ENV to cd into its own checkout.
	shell_env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "WORKSPACE_PATH")}
	return subprocess.run(
		["bash", "-e", "-c", body], cwd=tmp_path, capture_output=True, timeout=5, check=False,
		env={**shell_env, "SUPPORT_INSTRUCTIONS_FILE": str(instructions),
			"SUPPORT_AGENTS_FILE": str(tmp_path.parent / "absent-agents.md"),
			"SUPPORT_SCRIPTS_DIR": str(ROOT / "scripts"), "RUNTIME_DIR": str(runtime)},
	)


def test_static_context_overwrites_regular_output_and_omits_bad_readme(tmp_path):
	(tmp_path / "pre_assembled_static.txt").write_text("old context")
	(tmp_path / "README.md").symlink_to("/proc/self/environ")
	result = run_static_context_step(tmp_path)
	assert result.returncode == 0, result.stderr
	assert b"outcome=omitted reason=not_regular_file" in result.stdout
	assert b"README.MD (trimmed)" not in (tmp_path / "pre_assembled_static.txt").read_bytes()
	(tmp_path / "README.md").unlink()
	(tmp_path / "README.md").write_text("Safe overview\n### 2. Create wrapper workflows\nunsafe tail\n")
	result = run_static_context_step(tmp_path)
	assert result.returncode == 0, result.stderr
	assembled = (tmp_path / "pre_assembled_static.txt").read_text()
	assert "=== README.MD (trimmed) ===\nSafe overview\n\n" in assembled
	assert "unsafe tail" not in assembled


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_static_context_refuses_non_regular_output(tmp_path, kind):
	output = tmp_path / "pre_assembled_static.txt"
	if kind == "symlink":
		outside = tmp_path.parent / "do-not-overwrite"
		outside.write_text("unchanged")
		output.symlink_to(outside)
	elif kind == "fifo":
		os.mkfifo(output)
	else:
		output.mkdir()
	result = run_static_context_step(tmp_path)
	assert result.returncode == 1
	assert b"non-regular pre_assembled_static.txt" in result.stdout
	if kind == "symlink":
		assert outside.read_text() == "unchanged"

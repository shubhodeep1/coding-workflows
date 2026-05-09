#!/usr/bin/env python3
"""Contract tests for scripts/install_semble.sh and scripts/semble_helpers.sh."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "scripts" / "install_semble.sh"
HELPERS = REPO_ROOT / "scripts" / "semble_helpers.sh"


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=full_env,
		capture_output=True,
		text=True,
	)


def _write_executable(path: Path, content: str) -> None:
	path.write_text(content, encoding="utf-8")
	path.chmod(0o755)


def test_semble_helpers_source_cleanly() -> None:
	result = _run_bash(f"source {HELPERS}", REPO_ROOT)
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert result.stderr == ""


def test_semble_query_block_success_keeps_stdout_prompt_only() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		index_dir = root / ".semble-index"
		index_dir.mkdir()
		fake_semble = bin_dir / "semble"
		_write_executable(
			fake_semble,
			"#!/usr/bin/env bash\n"
			"if [ \"${1:-}\" = \"query\" ]; then\n"
			"\tprintf 'chunk 1\\nchunk 2\\n'\n"
			"\texit 0\n"
			"fi\n"
			"if [ \"${1:-}\" = \"--version\" ]; then\n"
			"\tprintf 'semble 0.1.3\\n'\n"
			"\texit 0\n"
			"fi\n"
			"printf 'unexpected args: %s\\n' \"$*\" >&2\n"
			"exit 2\n",
		)

		result = _run_bash(
			(
				f"source {HELPERS}\n"
				"semble_query_block $'issue summary\\nwith newline' 2 'Reviewer Context' --extra-flag value"
			),
			root,
			env={
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
				"SEMBLE_AVAILABLE": "true",
				"SEMBLE_INDEX_AVAILABLE": "true",
				"SEMBLE_INDEX_PATH": str(index_dir),
			},
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == "=== SEMBLE: Reviewer Context ===\nchunk 1\nchunk 2\n=== END SEMBLE ===\n"
		assert "SEMBLE_QUERY target=reviewer-context chunks=2 bytes=" in result.stderr
		assert "SEMBLE_FALLBACK" not in result.stderr
		assert "SEMBLE_QUERY" not in result.stdout
		assert "SEMBLE_FALLBACK" not in result.stdout


def test_semble_query_block_bails_out_without_index_and_keeps_stdout_empty() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		result = _run_bash(
			f"source {HELPERS}\nsemble_query_block 'summary' 2 'Editor Context'",
			root,
			env={
				"SEMBLE_AVAILABLE": "true",
				"SEMBLE_INDEX_AVAILABLE": "false",
			},
		)

		assert result.returncode != 0
		assert result.stdout == ""
		assert "SEMBLE_FALLBACK target=editor-context reason=index-unavailable" in result.stderr


def test_semble_query_block_command_failure_stays_fail_open() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		index_dir = root / ".semble-index"
		index_dir.mkdir()
		fake_semble = bin_dir / "semble"
		_write_executable(
			fake_semble,
			"#!/usr/bin/env bash\n"
			"printf 'raw failure from semble\\n' >&2\n"
			"exit 7\n",
		)

		result = _run_bash(
			f"source {HELPERS}\nsemble_query_block 'summary' 3 'Editor Context'",
			root,
			env={
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
				"SEMBLE_AVAILABLE": "true",
				"SEMBLE_INDEX_AVAILABLE": "true",
				"SEMBLE_INDEX_PATH": str(index_dir),
			},
		)

		assert result.returncode != 0
		assert result.stdout == ""
		assert "SEMBLE_FALLBACK target=editor-context reason=exit=7 raw failure from semble" in result.stderr
		assert "SEMBLE_QUERY" not in result.stdout


def test_install_semble_marks_available_when_pinned_binary_exists() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		fake_semble = bin_dir / "semble"
		github_env = root / "github.env"
		_write_executable(
			fake_semble,
			"#!/usr/bin/env bash\n"
			"if [ \"${1:-}\" = \"--version\" ]; then\n"
			"\tprintf 'semble 0.1.3\\n'\n"
			"\texit 0\n"
			"fi\n"
			"printf 'unexpected args: %s\\n' \"$*\" >&2\n"
			"exit 2\n",
		)

		result = subprocess.run(
			["bash", str(INSTALLER)],
			cwd=root,
			env={
				**os.environ,
				"PYTHONDONTWRITEBYTECODE": "1",
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
				"GITHUB_ENV": str(github_env),
			},
			capture_output=True,
			text=True,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		assert github_env.read_text(encoding="utf-8") == "SEMBLE_AVAILABLE=true\n"


def test_install_semble_fails_open_and_marks_unavailable_on_install_error() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		github_env = root / "github.env"
		fake_python = bin_dir / "fakepython"
		fake_user_base = root / "fake-user-base"
		_write_executable(
			fake_python,
			"#!/usr/bin/env bash\n"
			"if [ \"${1:-}\" = \"-\" ]; then\n"
			"\tcat >/dev/null\n"
			"\tprintf '%s/bin\\n' \"${FAKE_USER_BASE:?}\"\n"
			"\texit 0\n"
			"fi\n"
			"if [ \"${1:-}\" = \"-m\" ] && [ \"${2:-}\" = \"pip\" ] && [ \"${3:-}\" = \"install\" ]; then\n"
			"\tprintf 'simulated pip failure\\n' >&2\n"
			"\texit 9\n"
			"fi\n"
			"printf 'unexpected args: %s\\n' \"$*\" >&2\n"
			"exit 2\n",
		)

		result = subprocess.run(
			["bash", str(INSTALLER)],
			cwd=root,
			env={
				**os.environ,
				"PYTHONDONTWRITEBYTECODE": "1",
				"PATH": os.environ.get("PATH", ""),
				"GITHUB_ENV": str(github_env),
				"SEMBLE_PYTHON_BIN": str(fake_python),
				"FAKE_USER_BASE": str(fake_user_base),
			},
			capture_output=True,
			text=True,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		assert github_env.read_text(encoding="utf-8") == "SEMBLE_AVAILABLE=false\n"
		assert "pip install failed for semble==0.1.3: simulated pip failure" in result.stderr


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
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())

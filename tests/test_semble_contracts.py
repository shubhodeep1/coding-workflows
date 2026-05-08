#!/usr/bin/env python3
"""Contract tests for Semble foundation plumbing.

The implement workflow now stages two shared shell helpers:

- scripts/install_semble.sh
- scripts/semble_helpers.sh

These tests pin the fail-soft install contract, the helper's strict
stdout/stderr split, and the minimal workflow wiring that makes the new
foundation available to downstream callers.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_semble.sh"
HELPERS_SCRIPT = REPO_ROOT / "scripts" / "semble_helpers.sh"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"


def _write_executable(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_install(env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	full_env["PATH"] = full_env.get("PATH", "")
	if env_overrides:
		full_env.update(env_overrides)
	return subprocess.run(
		["bash", str(INSTALL_SCRIPT)],
		env=full_env,
		text=True,
		capture_output=True,
		check=False,
	)


def _run_helper(env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	if env_overrides:
		full_env.update(env_overrides)
	harness = f"""
		set -u
		source {HELPERS_SCRIPT}
		semble_query_block 'needle query' 3 'Implement Context'
	"""
	return subprocess.run(
		["bash", "-lc", harness],
		env=full_env,
		text=True,
		capture_output=True,
		check=False,
	)


def test_install_semble_disabled_is_noop_and_fail_soft() -> None:
	with tempfile.TemporaryDirectory() as td:
		env_file = Path(td) / "github_env.txt"
		result = _run_install(
			{
				"SEMBLE_ENABLED": "false",
				"GITHUB_ENV": str(env_file),
			}
		)
		assert result.returncode == 0, result.stderr
		body = env_file.read_text(encoding="utf-8")
		assert "SEMBLE_AVAILABLE=false" in body, body
		assert "SEMBLE_INDEX_AVAILABLE=false" in body, body
		assert "SEMBLE_FALLBACK" not in result.stdout, result.stdout


def test_install_semble_failure_exports_false_without_failing() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		env_file = tmp / "github_env.txt"
		_write_executable(
			bin_dir / "uv",
			"#!/usr/bin/env bash\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = list ]; then\n"
			"  echo 'No tools installed'\n"
			"  exit 0\n"
			"fi\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = dir ] && [ \"$3\" = --bin ]; then\n"
			f"  echo '{bin_dir}'\n"
			"  exit 0\n"
			"fi\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = install ]; then\n"
			"  echo 'simulated install failure' >&2\n"
			"  exit 42\n"
			"fi\n"
			"exit 0\n",
		)
		result = _run_install(
			{
				"SEMBLE_ENABLED": "true",
				"GITHUB_ENV": str(env_file),
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			}
		)
		assert result.returncode == 0, result.stderr
		body = env_file.read_text(encoding="utf-8")
		assert "SEMBLE_AVAILABLE=false" in body, body
		assert "SEMBLE_FALLBACK target=install reason=install_failed" in result.stderr, result.stderr
		assert result.stdout == "", result.stdout


def test_install_semble_skips_reinstall_for_matching_uv_tool() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		env_file = tmp / "github_env.txt"
		_write_executable(bin_dir / "semble", "#!/usr/bin/env bash\nexit 0\n")
		_write_executable(
			bin_dir / "uv",
			"#!/usr/bin/env bash\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = list ]; then\n"
			"  echo 'semble v0.1.3'\n"
			"  echo '- semble'\n"
			"  exit 0\n"
			"fi\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = dir ] && [ \"$3\" = --bin ]; then\n"
			f"  echo '{bin_dir}'\n"
			"  exit 0\n"
			"fi\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = install ]; then\n"
			"  echo 'unexpected reinstall' >&2\n"
			"  exit 99\n"
			"fi\n"
			"exit 0\n",
		)
		result = _run_install(
			{
				"SEMBLE_ENABLED": "true",
				"GITHUB_ENV": str(env_file),
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			}
		)
		assert result.returncode == 0, result.stderr
		body = env_file.read_text(encoding="utf-8")
		assert "SEMBLE_AVAILABLE=true" in body, body
		assert "already_installed" in result.stderr, result.stderr
		assert "unexpected reinstall" not in result.stderr, result.stderr


def test_semble_query_block_keeps_prompt_output_on_stdout_only() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		runtime_dir = tmp / "runtime"
		index_dir = runtime_dir / ".semble-index"
		index_dir.mkdir(parents=True)
		(index_dir / "repo_root").write_text(str(tmp), encoding="utf-8")
		_write_executable(
			bin_dir / "semble",
			"#!/usr/bin/env bash\n"
			"printf 'relevant prompt block\\nsecond line\\n'\n",
		)
		result = _run_helper(
			{
				"RUNTIME_DIR": str(runtime_dir),
				"SEMBLE_INDEX_AVAILABLE": "true",
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			}
		)
		assert result.returncode == 0, result.stderr
		assert "=== SEMBLE: Implement Context ===" in result.stdout, result.stdout
		assert "relevant prompt block" in result.stdout, result.stdout
		assert "SEMBLE_QUERY target=Implement Context" in result.stderr, result.stderr
		assert "SEMBLE_QUERY" not in result.stdout, result.stdout
		assert "SEMBLE_FALLBACK" not in result.stdout, result.stdout


def test_semble_query_block_falls_back_cleanly_when_index_disabled() -> None:
	result = _run_helper({"SEMBLE_INDEX_AVAILABLE": "false"})
	assert result.returncode == 1, result.returncode
	assert result.stdout == "", result.stdout
	assert "SEMBLE_FALLBACK target=Implement Context reason=index_unavailable" in result.stderr, result.stderr


def test_semble_query_block_query_failures_do_not_leak_to_stdout() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		runtime_dir = tmp / "runtime"
		index_dir = runtime_dir / ".semble-index"
		index_dir.mkdir(parents=True)
		(index_dir / "repo_root").write_text(str(tmp), encoding="utf-8")
		_write_executable(
			bin_dir / "semble",
			"#!/usr/bin/env bash\n"
			"echo 'backend exploded' >&2\n"
			"exit 7\n",
		)
		result = _run_helper(
			{
				"RUNTIME_DIR": str(runtime_dir),
				"SEMBLE_INDEX_AVAILABLE": "true",
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			}
		)
		assert result.returncode == 1, result.returncode
		assert result.stdout == "", result.stdout
		assert "SEMBLE_FALLBACK target=Implement Context reason=query_failed detail=backend exploded" in result.stderr, result.stderr


def test_implement_workflow_stages_and_gates_semble_foundation() -> None:
	body = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	assert "install_semble.sh" in body, "workflow must stage install_semble.sh"
	assert "semble_helpers.sh" in body, "workflow must stage semble_helpers.sh"
	assert "astral-sh/setup-uv@v3" in body, "workflow must install uv when Semble is enabled"
	assert "SEMBLE_ENABLED" in body, "workflow must expose SEMBLE_ENABLED"
	assert "${RUNTIME_DIR}/.semble-index" in body, "workflow must build the workspace-local index directory"


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

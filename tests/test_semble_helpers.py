#!/usr/bin/env python3
"""Contract tests for scripts/install_semble.sh and scripts/semble_helpers.sh."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "scripts" / "install_semble.sh"
HELPERS = REPO_ROOT / "scripts" / "semble_helpers.sh"


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	for key in (
		"SEMBLE_AVAILABLE",
		"SEMBLE_BIN",
		"SEMBLE_ENABLED",
		"SEMBLE_INDEX_AVAILABLE",
		"SEMBLE_INDEX_PATH",
		"SEMBLE_LOG_CONTEXT",
	):
		env.pop(key, None)
	return env


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	full_env = _base_env()
	full_env["HOME"] = str(cwd)
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	full_env.setdefault("SEMBLE_LOG_CONTEXT", "contract-test")
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
				"semble_query_block $'issue summary\\nwith newline' 999 'Reviewer Context' --extra-flag value"
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
		assert re.search(
			r"SEMBLE_QUERY target=reviewer-context chunks=20 bytes=16 ms=\d+ "
			r"query_bytes=26 returned_bytes=16 included_bytes=16 "
			r"deduplicated_bytes=unavailable avoided_prompt_bytes=unavailable",
			result.stderr,
		)
		assert "context=contract-test" in result.stderr
		assert "SEMBLE_FALLBACK" not in result.stderr
		assert "SEMBLE_QUERY" not in result.stdout
		assert "SEMBLE_FALLBACK" not in result.stdout


def test_semble_query_block_counts_multibyte_content_and_added_newline() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		index_dir = root / ".semble-index"
		index_dir.mkdir()
		fake_semble = bin_dir / "semble"
		for ends_in_newline in (False, True):
			content = "résumé" + ("\n" if ends_in_newline else "")
			printf_content = "résumé\\n" if ends_in_newline else "résumé"
			_write_executable(
				fake_semble,
				"#!/usr/bin/env bash\n"
				f"printf '{printf_content}'\n",
			)
			result = _run_bash(
				f"source {HELPERS}\nsemble_query_block 'café' 2 'Editor Context'",
				root,
				env={
					"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
					"SEMBLE_AVAILABLE": "true",
					"SEMBLE_INDEX_AVAILABLE": "true",
					"SEMBLE_INDEX_PATH": str(index_dir),
				},
			)
			assert result.returncode == 0, result.stderr
			assert result.stdout == f"=== SEMBLE: Editor Context ===\n{content}{'' if ends_in_newline else chr(10)}=== END SEMBLE ===\n"
			returned_bytes = len(content.encode("utf-8"))
			included_bytes = returned_bytes + (not ends_in_newline)
			assert re.search(
				f"SEMBLE_QUERY target=editor-context chunks=2 bytes={returned_bytes} ms=\\d+ "
				f"query_bytes=5 returned_bytes={returned_bytes} included_bytes={included_bytes} "
				r"deduplicated_bytes=unavailable avoided_prompt_bytes=unavailable",
				result.stderr,
			)
			assert "SEMBLE_FALLBACK" not in result.stderr


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
		assert "context=contract-test" in result.stderr


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
		assert "context=contract-test" in result.stderr
		assert " ms=" in result.stderr
		assert "SEMBLE_QUERY" not in result.stderr
		assert "SEMBLE_QUERY" not in result.stdout


def test_semble_query_block_timeout_reports_timeout() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		index_dir = root / ".semble-index"
		index_dir.mkdir()
		_write_executable(bin_dir / "semble", "#!/usr/bin/env bash\ntrap 'exit 143' TERM\nsleep 5\n")
		result = _run_bash(
			f"source {HELPERS}\nsemble_query_block 'summary' 3 'Editor Context'",
			root,
			env={
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
				"SEMBLE_AVAILABLE": "true",
				"SEMBLE_INDEX_AVAILABLE": "true",
				"SEMBLE_INDEX_PATH": str(index_dir),
				"SEMBLE_QUERY_TIMEOUT_SECS": "0.2",
			},
		)
		assert result.returncode != 0
		assert "SEMBLE_FALLBACK target=editor-context reason=timeout" in result.stderr
		assert "reason=exit=143" not in result.stderr


def test_semble_query_block_empty_and_whitespace_results_fall_back() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		index_dir = root / ".semble-index"
		index_dir.mkdir()
		fake_semble = bin_dir / "semble"
		for content in ("", " \t\n "):
			_write_executable(fake_semble, "#!/usr/bin/env bash\n" f"printf '%s' '{content}'\n")
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
			assert "SEMBLE_FALLBACK target=editor-context reason=empty-result" in result.stderr
			assert "SEMBLE_QUERY" not in result.stderr


def test_semble_elapsed_ms_clamps_negative_duration_to_zero() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		fake_date = bin_dir / "date"
		_write_executable(
			fake_date,
			"#!/usr/bin/env bash\n"
			"exit 1\n",
		)

		result = _run_bash(
			f"source {HELPERS}\n_semble_elapsed_ms 1715251234567",
			root,
			env={
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			},
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == "0\n"
		assert result.stderr == ""


def test_install_semble_never_runs_host_python_or_pip() -> None:
	text = INSTALLER.read_text(encoding="utf-8")
	assert "python3 -m pip" not in text
	assert "attempt_pip_install" not in text
	assert "current_semble_version" not in text
	assert "--require-hashes --no-deps --only-binary=:all:" in text
	assert "env -i PATH=" in text
	assert "SEMBLE_QUERY_TIMEOUT_SECS:-15" in HELPERS.read_text(encoding="utf-8")


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

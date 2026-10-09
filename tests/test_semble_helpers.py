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


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	for key in (
		"BASH_ENV",
		"ENV",
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


def test_lazy_bootstrap_is_shared_across_subshells_and_uses_neutral_directory() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		helpers_dir = root / "helpers"
		helpers_dir.mkdir()
		(helpers_dir / "semble_helpers.sh").write_bytes(HELPERS.read_bytes())
		_write_executable(helpers_dir / "install_semble.sh", '''#!/bin/bash
printf '%s\n' "$PWD" >> "${RUNTIME_DIR}/installs"
printf 'available\n' > "${SEMBLE_INSTALL_RESULT_FILE}"
''')
		_write_executable(helpers_dir / "build_semble_wrapper.sh", '''#!/bin/bash
mkdir -p "${RUNTIME_DIR}/semble/bin"
printf 'index' > "${SEMBLE_INDEX_PATH}"
cat > "${RUNTIME_DIR}/semble/bin/semble" <<'SH'
#!/bin/bash
printf '[1] file.py:1-2\ncontext\n'
SH
chmod +x "${RUNTIME_DIR}/semble/bin/semble"
''')
		script = f"source '{helpers_dir}/semble_helpers.sh'\nfirst=$(semble_query_block 'q' 1 'Lazy Context')\nsecond=$(semble_query_block 'q' 1 'Lazy Context')\nprintf '%s\\n' \"$first\" \"$second\"\n"
		result = _run_bash(script, root, {"RUNTIME_DIR": str(root), "SEMBLE_ENABLED": "true", "SEMBLE_BOOTSTRAP_MODE": "lazy", "SEMBLE_INDEX_PATH": str(root / ".semble-index"), "SEMBLE_BOOTSTRAP_STATE_FILE": str(root / "bootstrap.state")})
		assert result.returncode == 0, result.stderr
		assert (root / "installs").read_text().splitlines() == [str(root)]
		assert (root / "bootstrap.state").read_text().startswith("state=ready\n")
		assert result.stdout.count("=== SEMBLE: Lazy Context ===") == 2


def test_lazy_bootstrap_failure_is_not_retried() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		helpers_dir = root / "helpers"
		helpers_dir.mkdir()
		(helpers_dir / "semble_helpers.sh").write_bytes(HELPERS.read_bytes())
		_write_executable(helpers_dir / "install_semble.sh", '''#!/bin/bash
printf 'install\n' >> "${RUNTIME_DIR}/installs"
printf 'unavailable\n' > "${SEMBLE_INSTALL_RESULT_FILE}"
''')
		script = f"source '{helpers_dir}/semble_helpers.sh'\nsemble_query_block q 1 'Lazy Context' || true\nsemble_query_block q 1 'Lazy Context' || true\n"
		result = _run_bash(script, root, {"RUNTIME_DIR": str(root), "SEMBLE_ENABLED": "true", "SEMBLE_BOOTSTRAP_MODE": "lazy", "SEMBLE_INDEX_PATH": str(root / ".semble-index"), "SEMBLE_BOOTSTRAP_STATE_FILE": str(root / "bootstrap.state")})
		assert result.returncode == 0, result.stderr
		assert (root / "installs").read_text().splitlines() == ["install"]
		assert (root / "bootstrap.state").read_text().startswith("state=failed\n")
		assert "SEMBLE_BOOTSTRAP mode=lazy state=failed" in result.stderr
		assert "SEMBLE_FALLBACK target=lazy-context reason=lazy-bootstrap-failed" in result.stderr
		# One fallback per query: the failed bootstrap's, then the cached failure's.
		assert result.stderr.count("SEMBLE_FALLBACK ") == 2


def test_semble_should_query_respects_failed_lazy_state_and_eager_mode() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		state_file = root / "bootstrap.state"
		script = f"source '{HELPERS}'\nsemble_should_query && echo yes || echo no\n"
		defaults = {"RUNTIME_DIR": str(root), "SEMBLE_BOOTSTRAP_STATE_FILE": str(state_file), "SEMBLE_INDEX_AVAILABLE": "false", "SEMBLE_ENABLED": "true"}
		assert _run_bash(script, root, {**defaults, "SEMBLE_BOOTSTRAP_MODE": "lazy"}).stdout == "yes\n"
		assert _run_bash(script, root, defaults).stdout == "no\n"
		state_file.write_text("state=failed\n")
		assert _run_bash(script, root, {**defaults, "SEMBLE_BOOTSTRAP_MODE": "lazy"}).stdout == "no\n"
		assert _run_bash(script, root, {**defaults, "SEMBLE_BOOTSTRAP_MODE": "invalid"}).stdout == "no\n"
		assert _run_bash(script, root, {**defaults, "SEMBLE_INDEX_AVAILABLE": "true"}).stdout == "yes\n"


def test_lazy_first_queries_lock_install_across_concurrent_subshells() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		helpers_dir = root / "helpers"
		helpers_dir.mkdir()
		(helpers_dir / "semble_helpers.sh").write_bytes(HELPERS.read_bytes())
		_write_executable(helpers_dir / "install_semble.sh", '''#!/bin/bash
printf 'install\n' >> "${RUNTIME_DIR}/installs"
sleep 0.2
printf 'available\n' > "${SEMBLE_INSTALL_RESULT_FILE}"
''')
		_write_executable(helpers_dir / "build_semble_wrapper.sh", '''#!/bin/bash
mkdir -p "${RUNTIME_DIR}/semble/bin"
printf 'index' > "${SEMBLE_INDEX_PATH}"
printf '#!/bin/bash\n' > "${RUNTIME_DIR}/semble/bin/semble"
chmod +x "${RUNTIME_DIR}/semble/bin/semble"
''')
		script = f"source '{helpers_dir}/semble_helpers.sh'\n(semble_ensure_ready first) &\n(semble_ensure_ready second) &\nwait\n"
		result = _run_bash(script, root, {"RUNTIME_DIR": str(root), "SEMBLE_ENABLED": "true", "SEMBLE_BOOTSTRAP_MODE": "lazy", "SEMBLE_INDEX_PATH": str(root / ".semble-index"), "SEMBLE_BOOTSTRAP_STATE_FILE": str(root / "bootstrap.state")})
		assert result.returncode == 0, result.stderr
		assert (root / "installs").read_text().splitlines() == ["install"]


def test_lazy_builder_must_create_executable_wrapper_and_index() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		helpers_dir = root / "helpers"
		helpers_dir.mkdir()
		(helpers_dir / "semble_helpers.sh").write_bytes(HELPERS.read_bytes())
		_write_executable(helpers_dir / "install_semble.sh", "#!/bin/bash\nprintf 'available\\n' > \"${SEMBLE_INSTALL_RESULT_FILE}\"\n")
		_write_executable(helpers_dir / "build_semble_wrapper.sh", "#!/bin/bash\nexit 0\n")
		result = _run_bash(f"source '{helpers_dir}/semble_helpers.sh'\nsemble_ensure_ready builder || true\n", root, {"RUNTIME_DIR": str(root), "SEMBLE_ENABLED": "true", "SEMBLE_BOOTSTRAP_MODE": "lazy", "SEMBLE_BOOTSTRAP_STATE_FILE": str(root / "bootstrap.state")})
		assert result.returncode == 0, result.stderr
		assert (root / "bootstrap.state").read_text().startswith("state=failed\n")


def test_lazy_install_timeout_records_one_failed_bootstrap() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		helpers_dir = root / "helpers"
		helpers_dir.mkdir()
		(helpers_dir / "semble_helpers.sh").write_bytes(HELPERS.read_bytes())
		_write_executable(helpers_dir / "install_semble.sh", "#!/bin/bash\nsleep 2\n")
		result = _run_bash(f"source '{helpers_dir}/semble_helpers.sh'\nsemble_ensure_ready timeout || true\n", root, {"RUNTIME_DIR": str(root), "SEMBLE_ENABLED": "true", "SEMBLE_BOOTSTRAP_MODE": "lazy", "SEMBLE_LAZY_BOOTSTRAP_TIMEOUT_SECS": "1", "SEMBLE_BOOTSTRAP_STATE_FILE": str(root / "bootstrap.state")})
		assert result.returncode == 0, result.stderr
		assert (root / "bootstrap.state").read_text().startswith("state=failed\n")
		assert result.stderr.count("SEMBLE_BOOTSTRAP mode=lazy state=failed") == 1


def test_lazy_build_timeout_records_one_failed_bootstrap() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		helpers_dir = root / "helpers"
		helpers_dir.mkdir()
		(helpers_dir / "semble_helpers.sh").write_bytes(HELPERS.read_bytes())
		_write_executable(helpers_dir / "install_semble.sh", "#!/bin/bash\nprintf 'available\\n' > \"${SEMBLE_INSTALL_RESULT_FILE}\"\n")
		_write_executable(helpers_dir / "build_semble_wrapper.sh", "#!/bin/bash\nsleep 6\n")
		# The deadline has one-second granularity: a 1s budget can expire
		# before the builder starts (remaining=0 logs index_ms=0). With 3s the
		# builder always starts and is killed by the timeout.
		result = _run_bash(f"source '{helpers_dir}/semble_helpers.sh'\nsemble_ensure_ready timeout || true\n", root, {"RUNTIME_DIR": str(root), "SEMBLE_ENABLED": "true", "SEMBLE_BOOTSTRAP_MODE": "lazy", "SEMBLE_LAZY_BOOTSTRAP_TIMEOUT_SECS": "3", "SEMBLE_BOOTSTRAP_STATE_FILE": str(root / "bootstrap.state")})
		assert result.returncode == 0, result.stderr
		assert (root / "bootstrap.state").read_text().startswith("state=failed\n")
		assert result.stderr.count("SEMBLE_BOOTSTRAP mode=lazy state=failed") == 1
		# The killed builder's elapsed time is reported, not a hard-coded zero.
		assert "index_ms=0 " not in result.stderr


def test_eager_builder_emits_bootstrap_failure_without_index() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		result = _run_bash(f"bash '{REPO_ROOT / 'scripts' / 'build_semble_wrapper.sh'}'", root, {"RUNTIME_DIR": str(root), "SEMBLE_INDEX_PATH": str(root / ".semble-index"), "SEMBLE_PYTHON_BIN": "missing-python", "SEMBLE_BOOTSTRAP_MODE": "eager"})
		assert result.returncode == 0, result.stderr
		assert "SEMBLE_BOOTSTRAP mode=eager state=failed install_ms=0 index_ms=" in result.stderr


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
				"set -euo pipefail\n"
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
		assert "SEMBLE_QUERY target=reviewer-context chunks=20 bytes=" in result.stderr
		assert "context=contract-test" in result.stderr
		assert "SEMBLE_FALLBACK" not in result.stderr
		assert "SEMBLE_QUERY" not in result.stdout
		assert "SEMBLE_FALLBACK" not in result.stdout


def test_semble_query_contribution_counts_distinct_paths_and_static_lines() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / ".semble-index").write_bytes(b"index")
		static_line = "a" * 48
		(root / "static.txt").write_text(static_line + "\n")
		binary = root / "semble"
		_write_executable(binary, f"#!/bin/bash\nprintf '[1] a.py:1-2\\n{static_line}\\n[2] a.py:4-6\\nother\\n[3] b.py:1-2\\nlast\\n'\n")
		result = _run_bash(f"source '{HELPERS}'\nsemble_query_block q 3 'Judge Context'\n", root, {"SEMBLE_AVAILABLE": "true", "SEMBLE_INDEX_AVAILABLE": "true", "SEMBLE_BIN": str(binary), "SEMBLE_INDEX_PATH": str(root / ".semble-index"), "SEMBLE_STATIC_CONTEXT_FILE": str(root / "static.txt")})
		assert result.returncode == 0, result.stderr
		assert "sources=2" in result.stderr
		assert "static_dup_bytes=48" in result.stderr


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
		assert "SEMBLE_QUERY" not in result.stdout


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
			"\tprintf 'Semble CLI v0.1.3 (build 7)\\n'\n"
			"\texit 0\n"
			"fi\n"
			"printf 'unexpected args: %s\\n' \"$*\" >&2\n"
			"exit 2\n",
		)

		result = subprocess.run(
			["bash", str(INSTALLER)],
			cwd=root,
			env={
				**_base_env(),
				"HOME": str(root),
				"PYTHONDONTWRITEBYTECODE": "1",
				"PATH": f"{bin_dir}:/usr/bin:/bin",
				"GITHUB_ENV": str(github_env),
			},
			capture_output=True,
			text=True,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		assert "SEMBLE_AVAILABLE=true\n" in github_env.read_text(encoding="utf-8")


def test_install_semble_rejects_partial_version_match() -> None:
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
			"\tprintf 'semble 10.1.3\\n'\n"
			"\texit 0\n"
			"fi\n"
			"printf 'unexpected args: %s\\n' \"$*\" >&2\n"
			"exit 2\n",
		)

		result = subprocess.run(
			["bash", str(INSTALLER)],
			cwd=root,
			env={
				**_base_env(),
				"HOME": str(root),
				"PYTHONDONTWRITEBYTECODE": "1",
				"PATH": f"{bin_dir}:/usr/bin:/bin",
				"GITHUB_ENV": str(github_env),
				"SEMBLE_PYTHON_BIN": "missing-python",
			},
			capture_output=True,
			text=True,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		assert "SEMBLE_AVAILABLE=false\n" in github_env.read_text(encoding="utf-8")
		assert "found non-pinned Semble (semble 10.1.3); attempting install of semble==0.1.3." in result.stderr


def test_install_semble_rejects_multiline_non_pinned_version_output() -> None:
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
			"\tprintf 'Semble CLI v0.1.4\\nFixed 0.1.3 bug\\n'\n"
			"\texit 0\n"
			"fi\n"
			"printf 'unexpected args: %s\\n' \"$*\" >&2\n"
			"exit 2\n",
		)

		result = subprocess.run(
			["bash", str(INSTALLER)],
			cwd=root,
			env={
				**_base_env(),
				"HOME": str(root),
				"PYTHONDONTWRITEBYTECODE": "1",
				"PATH": f"{bin_dir}:/usr/bin:/bin",
				"GITHUB_ENV": str(github_env),
				"SEMBLE_PYTHON_BIN": "missing-python",
			},
			capture_output=True,
			text=True,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		assert "SEMBLE_AVAILABLE=false\n" in github_env.read_text(encoding="utf-8")


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
				**_base_env(),
				"HOME": str(root),
				"PYTHONDONTWRITEBYTECODE": "1",
				"PATH": "/usr/bin:/bin",
				"GITHUB_ENV": str(github_env),
				"SEMBLE_PYTHON_BIN": str(fake_python),
				"FAKE_USER_BASE": str(fake_user_base),
			},
			capture_output=True,
			text=True,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		assert "SEMBLE_AVAILABLE=false\n" in github_env.read_text(encoding="utf-8")
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

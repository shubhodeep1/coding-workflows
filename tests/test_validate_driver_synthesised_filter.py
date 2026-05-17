#!/usr/bin/env python3
"""Tests for validate_driver.sh synthesized-test discovery filtering."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_DRIVER = REPO_ROOT / "scripts" / "validate_driver.sh"


def _extract_shell_function(path: Path, function_name: str) -> str:
	lines = path.read_text(encoding="utf-8").splitlines()
	start = None
	for idx, line in enumerate(lines):
		if line.startswith(f"{function_name}()"):
			start = idx
			break
	if start is None:
		raise AssertionError(f"missing function {function_name} in {path}")

	brace_line = start + 1
	while brace_line < len(lines) and lines[brace_line].strip() != "{":
		brace_line += 1
	if brace_line >= len(lines):
		raise AssertionError(f"missing opening brace for {function_name}")

	in_heredoc: str | None = None
	depth = 1
	end = brace_line + 1
	while end < len(lines):
		stripped = lines[end].strip()
		if in_heredoc is not None:
			if stripped == in_heredoc:
				in_heredoc = None
			end += 1
			continue
		match = re.search(r"<<[-]?'?([A-Za-z_][A-Za-z0-9_]*)'?", lines[end])
		if match:
			in_heredoc = match.group(1)
		if stripped == "{":
			depth += 1
		elif stripped.startswith("}"):
			depth -= 1
			if depth == 0:
				return "\n".join(lines[start : end + 1]) + "\n"
		end += 1

	raise AssertionError(f"could not extract function {function_name}")


def _write_exec(path: Path) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n", encoding="utf-8")
	path.chmod(0o755)


def _run_discover_tests(workspace: Path, *, include_synthesised: str | None) -> subprocess.CompletedProcess[str]:
	function_text = _extract_shell_function(VALIDATE_DRIVER, "discover_tests")
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["TEST_DIR"] = "validation/tests"
	env["HELPER_PATTERN"] = "_*.sh"
	env["CANARY_PATTERN"] = "*canary*.sh"
	env["CANARY_REQUIRED"] = "1"
	if include_synthesised is not None:
		env["VALIDATION_INCLUDE_SYNTHESISED"] = include_synthesised

	script = (
		'VALIDATION_INCLUDE_SYNTHESISED="${VALIDATION_INCLUDE_SYNTHESISED:-true}"\n'
		+ function_text
		+ "\n"
		+ "fail_fast()\n"
		+ "{\n"
		+ "\tprintf 'FAIL:%s\\n' \"$2\" >&2\n"
		+ "\texit 99\n"
		+ "}\n"
		+ "discover_tests\n"
		+ "printf 'CANARY_TEST=%s\\n' \"${CANARY_TEST}\"\n"
		+ "printf 'TEST_FILE=%s\\n' \"${TEST_FILES[@]}\"\n"
	)

	return subprocess.run(
		["bash", "-c", script],
		cwd=workspace,
		env=env,
		capture_output=True,
		text=True,
		timeout=60,
	)


def _parse_test_files(stdout: str) -> list[str]:
	return [
		line.split("=", 1)[1]
		for line in stdout.splitlines()
		if line.startswith("TEST_FILE=") and line.split("=", 1)[1]
	]


def _parse_canary(stdout: str) -> str:
	for line in stdout.splitlines():
		if line.startswith("CANARY_TEST="):
			return line.split("=", 1)[1]
	raise AssertionError("missing CANARY_TEST line")


def _seed_test_dir(workspace: Path) -> None:
	test_dir = workspace / "validation" / "tests"
	_write_exec(test_dir / "00_canary.sh")
	_write_exec(test_dir / "20_health.sh")
	_write_exec(test_dir / "_helper.sh")
	_write_exec(test_dir / "synth_round_4_issue.sh")
	(test_dir / "synth_round_4_manifest.json").write_text("{}\n", encoding="utf-8")


def test_discover_tests_includes_synthesised_scripts_by_default() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_driver_synth_default_") as td:
		workspace = Path(td)
		_seed_test_dir(workspace)

		result = _run_discover_tests(workspace, include_synthesised=None)
		assert result.returncode == 0, result.stdout + result.stderr
		assert _parse_canary(result.stdout) == "validation/tests/00_canary.sh"
		assert _parse_test_files(result.stdout) == [
			"validation/tests/00_canary.sh",
			"validation/tests/20_health.sh",
			"validation/tests/synth_round_4_issue.sh",
		]
		assert "synth_round_4_manifest.json" not in result.stdout


def test_discover_tests_excludes_only_synthesised_scripts_when_disabled() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_driver_synth_disabled_") as td:
		workspace = Path(td)
		_seed_test_dir(workspace)

		result = _run_discover_tests(workspace, include_synthesised="false")
		assert result.returncode == 0, result.stdout + result.stderr
		assert _parse_canary(result.stdout) == "validation/tests/00_canary.sh"
		assert _parse_test_files(result.stdout) == [
			"validation/tests/00_canary.sh",
			"validation/tests/20_health.sh",
		]
		assert "validation/tests/_helper.sh" not in result.stdout
		assert "validation/tests/synth_round_4_issue.sh" not in result.stdout


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

#!/usr/bin/env python3
"""Tests for the local slop-scan helper."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "slop_scan"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import slop_scan_local


def _result_for_fixture(path: Path) -> dict:
	relative_path = path.relative_to(REPO_ROOT).as_posix()
	return slop_scan_local.collect_scan_result([relative_path], REPO_ROOT, restrict_scope=False)


def _temporary_test_dir():
	import os
	import tempfile

	try:
		return tempfile.TemporaryDirectory(prefix="slop-scan-local-")
	except (FileNotFoundError, PermissionError):
		runner_temp_dir = os.environ.get("RUNNER_TEMP")
		fallback_dir = REPO_ROOT if os.access(REPO_ROOT, os.W_OK | os.X_OK) else runner_temp_dir
		if not fallback_dir:
			raise
		prefix = ".slop-scan-local-" if fallback_dir == REPO_ROOT else "slop-scan-local-"
		return tempfile.TemporaryDirectory(prefix=prefix, dir=fallback_dir)


def test_empty_catch_around_os_unlink_fixture_emits_expected_finding() -> None:
	result = _result_for_fixture(FIXTURES_DIR / "empty_catch_around_os_unlink.py")

	assert result["collection_status"] == "ok"
	assert result["suppressed_findings"] == []
	assert any(finding["rule_id"] == "empty_catch_file_op" for finding in result["findings"])


def test_safe_unlink_quiet_cleanup_fixture_is_suppressed() -> None:
	result = _result_for_fixture(FIXTURES_DIR / "safe_unlink_quiet_cleanup.py")

	assert result["collection_status"] == "ok"
	assert result["findings"] == []
	assert any(
		finding["rule_id"] == "empty_catch_file_op"
		and finding.get("not_to_fix_reason") == "best_effort_cleanup_helper"
		for finding in result["suppressed_findings"]
	)


def test_path_variable_unlink_is_treated_as_file_operation(tmp_path: Path) -> None:
	python_file = tmp_path / "scripts" / "example.py"
	python_file.parent.mkdir(parents=True, exist_ok=True)
	python_file.write_text(
		"from pathlib import Path\n\n\n"
		"def remove_temp_file(path):\n"
		"\ttarget = Path(path)\n"
		"\ttry:\n"
		"\t\ttarget.unlink()\n"
		"\texcept:\n"
		"\t\tpass\n",
		encoding="utf-8",
	)

	result = slop_scan_local.collect_scan_result(["scripts/example.py"], tmp_path)

	assert any(finding["rule_id"] == "empty_catch_file_op" for finding in result["findings"])


def test_return_await_inside_async_with_is_not_flagged(tmp_path: Path) -> None:
	python_file = tmp_path / "scripts" / "example.py"
	python_file.parent.mkdir(parents=True, exist_ok=True)
	python_file.write_text(
		"class Response:\n"
		"\tasync def __aenter__(self):\n"
		"\t\treturn self\n\n"
		"\tasync def __aexit__(self, exc_type, exc, tb):\n"
		"\t\treturn False\n\n"
		"\tasync def json(self):\n"
		"\t\treturn {}\n\n\n"
		"async def read_response():\n"
		"\tasync with Response() as response:\n"
		"\t\treturn await response.json()\n",
		encoding="utf-8",
	)

	result = slop_scan_local.collect_scan_result(["scripts/example.py"], tmp_path)

	assert all(finding["rule_id"] != "redundant_return_await" for finding in result["findings"])


def test_python3_heredoc_findings_map_back_to_shell_line_numbers(tmp_path: Path) -> None:
	shell_file = tmp_path / "scripts" / "example.sh"
	shell_file.parent.mkdir(parents=True, exist_ok=True)
	shell_file.write_text(
		"#!/usr/bin/env bash\n"
		"python3 - <<'PY'\n"
		"def remove_temp(path):\n"
		"\ttry:\n"
		"\t\timport os\n"
		"\t\tos.unlink(path)\n"
		"\texcept:\n"
		"\t\tpass\n"
		"PY\n",
		encoding="utf-8",
	)

	result = slop_scan_local.collect_scan_result(["scripts/example.sh"], tmp_path)

	finding = next(finding for finding in result["findings"] if finding["rule_id"] == "empty_catch_file_op")
	assert finding["path"] == "scripts/example.sh"
	assert finding["line"] == 7
	assert finding["source_kind"] == "python_heredoc"


def test_temporary_test_dir_falls_back_to_repo_root_when_system_temp_is_unusable() -> None:
	import tempfile

	original_temporary_directory = tempfile.TemporaryDirectory

	def fake_temporary_directory(*args, **kwargs):
		if "dir" not in kwargs:
			raise FileNotFoundError("No usable temporary directory")
		return original_temporary_directory(*args, **kwargs)

	tempfile.TemporaryDirectory = fake_temporary_directory
	try:
		with _temporary_test_dir() as td:
			assert Path(td).parent == REPO_ROOT
	finally:
		tempfile.TemporaryDirectory = original_temporary_directory


def main() -> int:
	import inspect

	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	passed = 0
	failed = 0

	for func in test_funcs:
		name = func.__name__
		try:
			params = list(inspect.signature(func).parameters)
			if not params:
				func()
			elif params == ["tmp_path"]:
				with _temporary_test_dir() as td:
					func(Path(td))
			else:
				raise TypeError(f"unsupported test signature for {name}: {params}")
			print(f"  PASS  {name}")
			passed += 1
		except AssertionError as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
		except Exception as e:
			print(f"  ERROR {name}: {type(e).__name__}: {e}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())

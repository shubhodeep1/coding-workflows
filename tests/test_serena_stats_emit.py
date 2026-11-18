#!/usr/bin/env python3
"""Focused contract tests for scripts/serena_stats_emit.py."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
STATS_SCRIPT = SCRIPTS_DIR / "serena_stats_emit.py"

sys.path.insert(0, str(SCRIPTS_DIR))

from serena_stats_emit import parse_serena_log  # noqa: E402


def test_parse_serena_log_aggregates_json_and_plaintext_calls() -> None:
	log = """
{"event":"tool_call","server":"serena","tool":"find_symbol","duration_ms":12,"response_bytes":30}
mcp tool_result server=serena tool=find_symbol ms=5 response_bytes=7
serena.activate_project(path=/tmp/project) ms=3
serena.tools.find_symbol(path=/tmp/project) ms=6 response_bytes=4
ignored line
"""

	parsed = parse_serena_log(log)

	assert parsed == {
		"activate_project": {"calls": 1, "ms": 3, "response_bytes": 0},
		"find_symbol": {"calls": 2, "ms": 17, "response_bytes": 37},
		"tools.find_symbol": {"calls": 1, "ms": 6, "response_bytes": 4},
	}


def test_serena_stats_emit_cli_writes_rollups_to_stderr_only() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		log_a = root / "a.log"
		log_b = root / "b.log"
		log_a.write_text(
			"mcp tool_result server=serena tool=find_symbol ms=9 response_bytes=10\n"
			"mcp tool_result server=serena tool=find_symbol ms=1 response_bytes=2\n",
			encoding="utf-8",
		)
		log_b.write_text(
			'{"server":"serena","tool":"search.for=pattern","duration_ms":4,"response_bytes":11}\n',
			encoding="utf-8",
		)

		result = subprocess.run(
			[sys.executable, str(STATS_SCRIPT), "--target", "implement", "--log", str(log_a), "--log", str(log_b)],
			env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
			capture_output=True,
			text=True,
			check=False,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		stderr_lines = [line for line in result.stderr.splitlines() if line]
		assert stderr_lines == [
			"SERENA_QUERY target=implement tool=find_symbol calls=2 response_bytes=12 ms=10",
			"SERENA_QUERY target=implement tool=search.for_pattern calls=1 response_bytes=11 ms=4",
		]


def test_serena_stats_emit_cli_fails_open_on_missing_or_malformed_logs() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bad_log = root / "bad.log"
		bad_log.write_text("not json\n{broken\nserver=not-serena tool=find_symbol\n", encoding="utf-8")

		result = subprocess.run(
			[sys.executable, str(STATS_SCRIPT), "--target", "review", "--log", str(root / "missing.log"), "--log", str(bad_log)],
			env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
			capture_output=True,
			text=True,
			check=False,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		assert result.stderr == ""


def test_serena_stats_emit_cli_streams_large_log_files() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		large_log = root / "large.log"
		with large_log.open("w", encoding="utf-8") as handle:
			for _ in range(5000):
				handle.write("mcp tool_result server=serena tool=find_symbol ms=1 response_bytes=2\n")

		result = subprocess.run(
			[sys.executable, str(STATS_SCRIPT), "--target", "implement", "--log", str(large_log)],
			env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
			capture_output=True,
			text=True,
			check=False,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		assert result.stderr.splitlines() == [
			"SERENA_QUERY target=implement tool=find_symbol calls=5000 response_bytes=10000 ms=5000"
		]


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

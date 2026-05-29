#!/usr/bin/env python3
"""Focused contract test for scripts/codex_heartbeat.sh."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_SCRIPT = REPO_ROOT / "scripts" / "codex_heartbeat.sh"


def test_codex_heartbeat_emits_idle_lines_without_polluting_child_streams() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-heartbeat-") as td:
		tmp = Path(td)
		stdout_file = tmp / "child.stdout"
		stderr_file = tmp / "child.stderr"
		activity_file = tmp / "activity.txt"

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["CODEX_HEARTBEAT_ENABLED"] = "1"
		env["CODEX_HEARTBEAT_INTERVAL_SECS"] = "1"

		result = subprocess.run(
			[
				"bash",
				str(HEARTBEAT_SCRIPT),
				"--phase",
				"unit_test",
				"--stdout-file",
				str(stdout_file),
				"--stderr-file",
				str(stderr_file),
				"--activity-file",
				str(activity_file),
				"--",
				"python3",
				"-c",
				(
					"import sys, time; "
					"print('stdout-start'); sys.stdout.flush(); "
					"print('stderr-start', file=sys.stderr); sys.stderr.flush(); "
					"time.sleep(3.6); "
					"print('stdout-end'); sys.stdout.flush(); "
					"print('stderr-end', file=sys.stderr); sys.stderr.flush(); "
					"raise SystemExit(17)"
				),
			],
			env=env,
			capture_output=True,
			text=True,
			timeout=30,
		)

		assert result.returncode == 17, result.stderr
		assert result.stdout == ""

		heartbeat_lines = [
			line
			for line in result.stderr.splitlines()
			if line.startswith("CODEX_HEARTBEAT: phase=unit_test elapsed_secs=")
		]
		assert len(heartbeat_lines) == 3, result.stderr
		elapsed = [
			int(match.group(1))
			for line in heartbeat_lines
			for match in [re.fullmatch(r"CODEX_HEARTBEAT: phase=unit_test elapsed_secs=(\d+)", line)]
			if match is not None
		]
		assert len(elapsed) == 3, heartbeat_lines
		assert elapsed == sorted(elapsed), heartbeat_lines
		assert elapsed[-1] >= 3, heartbeat_lines

		assert stdout_file.read_text(encoding="utf-8") == "stdout-start\nstdout-end\n"
		child_stderr = stderr_file.read_text(encoding="utf-8")
		assert child_stderr == "stderr-start\nstderr-end\n"
		assert "CODEX_HEARTBEAT:" not in child_stderr
		assert re.fullmatch(r"\d+", activity_file.read_text(encoding="utf-8").strip())


def test_codex_heartbeat_disabled_still_tracks_child_activity() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-heartbeat-disabled-") as td:
		tmp = Path(td)
		stdout_file = tmp / "child.stdout"
		stderr_file = tmp / "child.stderr"
		activity_file = tmp / "activity.txt"

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["CODEX_HEARTBEAT_ENABLED"] = "0"
		env["CODEX_HEARTBEAT_INTERVAL_SECS"] = "1"

		result = subprocess.run(
			[
				"bash",
				str(HEARTBEAT_SCRIPT),
				"--phase",
				"disabled_test",
				"--stdout-file",
				str(stdout_file),
				"--stderr-file",
				str(stderr_file),
				"--activity-file",
				str(activity_file),
				"--",
				"python3",
				"-c",
				(
					"import sys, time; "
					"print('stdout-only'); sys.stdout.flush(); "
					"print('stderr-only', file=sys.stderr); sys.stderr.flush(); "
					"time.sleep(0.2)"
				),
			],
			env=env,
			capture_output=True,
			text=True,
			timeout=10,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		assert result.stderr == ""
		assert stdout_file.read_text(encoding="utf-8") == "stdout-only\n"
		assert stderr_file.read_text(encoding="utf-8") == "stderr-only\n"
		assert re.fullmatch(r"\d+", activity_file.read_text(encoding="utf-8").strip())


def main() -> int:
	test_codex_heartbeat_emits_idle_lines_without_polluting_child_streams()
	test_codex_heartbeat_disabled_still_tracks_child_activity()
	print("OK: codex heartbeat helper contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

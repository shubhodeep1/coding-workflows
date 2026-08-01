#!/usr/bin/env python3
"""Focused contract test for scripts/codex_heartbeat.sh."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_SCRIPT = REPO_ROOT / "scripts" / "codex_heartbeat.sh"


def _pid_is_running(pid: int) -> bool:
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except PermissionError:
		return True
	return True


def _kill_pid_if_running(pid: int | None) -> None:
	if pid is None:
		return
	for signum in (signal.SIGTERM, signal.SIGKILL):
		if not _pid_is_running(pid):
			return
		try:
			os.kill(pid, signum)
		except ProcessLookupError:
			return
		time.sleep(0.1)


def test_codex_heartbeat_emits_idle_lines_without_polluting_child_streams() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-heartbeat-") as td:
		tmp = Path(td)
		stdout_file = tmp / "child.stdout"
		stderr_file = tmp / "child.stderr"
		activity_file = tmp / "activity.txt"
		stdin_payload = "prompt-from-stdin\nsecond-line\n"

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["CODEX_HEARTBEAT_ENABLED"] = "1"
		env["CODEX_HEARTBEAT_INTERVAL_SECS"] = "1"

		started_at = time.time()
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
					"payload = sys.stdin.read(); "
					"print(payload.replace('\\n', '|')); sys.stdout.flush(); "
					"print('stderr-start', file=sys.stderr); sys.stderr.flush(); "
					"time.sleep(3.6); "
					"raise SystemExit(17)"
				),
			],
			env=env,
			input=stdin_payload,
			capture_output=True,
			text=True,
			timeout=30,
		)
		finished_at = time.time()

		assert result.returncode == 17, result.stderr
		assert result.stdout == ""

		heartbeat_lines = [
			line
			for line in result.stderr.splitlines()
			if line.startswith("CODEX_HEARTBEAT: phase=unit_test elapsed_secs=")
		]
		assert len(heartbeat_lines) >= 2, result.stderr
		elapsed = [
			int(match.group(1))
			for line in heartbeat_lines
			for match in [re.fullmatch(r"CODEX_HEARTBEAT: phase=unit_test elapsed_secs=(\d+)", line)]
			if match is not None
		]
		assert len(elapsed) == len(heartbeat_lines), heartbeat_lines
		assert elapsed == sorted(elapsed), heartbeat_lines
		assert elapsed[0] >= 1, heartbeat_lines
		assert elapsed[-1] >= 2, heartbeat_lines

		assert stdout_file.read_text(encoding="utf-8") == "prompt-from-stdin|second-line|\n"
		child_stderr = stderr_file.read_text(encoding="utf-8")
		assert child_stderr == "stderr-start\n"
		assert "CODEX_HEARTBEAT:" not in child_stderr
		activity_seen_at = int(activity_file.read_text(encoding="utf-8").strip())
		assert activity_seen_at >= int(started_at) + 2
		assert activity_seen_at <= int(finished_at)


def test_codex_heartbeat_appends_budget_fields_when_run_budget_env_present() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-heartbeat-budget-") as td:
		tmp = Path(td)
		stdout_file = tmp / "child.stdout"
		stderr_file = tmp / "child.stderr"

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["CODEX_HEARTBEAT_ENABLED"] = "1"
		env["CODEX_HEARTBEAT_INTERVAL_SECS"] = "1"
		now_epoch = int(time.time())
		env["CODEX_RUN_BUDGET_START_EPOCH"] = str(now_epoch - 5)
		env["CODEX_RUN_BUDGET_SOFT_DEADLINE_EPOCH"] = str(now_epoch + 90)
		env["CODEX_RUN_BUDGET_TOTAL_SECS"] = "95"

		result = subprocess.run(
			[
				"bash",
				str(HEARTBEAT_SCRIPT),
				"--phase",
				"budget_test",
				"--stdout-file",
				str(stdout_file),
				"--stderr-file",
				str(stderr_file),
				"--",
				"python3",
				"-c",
				(
					"import sys, time; "
					"print('stdout-budget'); sys.stdout.flush(); "
					"time.sleep(2.4)"
				),
			],
			env=env,
			capture_output=True,
			text=True,
			timeout=15,
		)

		assert result.returncode == 0, result.stderr
		heartbeat_lines = [
			line
			for line in result.stderr.splitlines()
			if line.startswith("CODEX_HEARTBEAT: phase=budget_test elapsed_secs=")
		]
		assert heartbeat_lines, result.stderr

		budget_samples: list[tuple[int, int, int]] = []
		for line in heartbeat_lines:
			match = re.fullmatch(
				r"CODEX_HEARTBEAT: phase=budget_test elapsed_secs=(\d+) budget_elapsed_secs=(\d+) budget_remaining_secs=(\d+)",
				line,
			)
			assert match is not None, heartbeat_lines
			budget_samples.append(tuple(int(match.group(i)) for i in (1, 2, 3)))

		assert budget_samples[0][1] >= 5, heartbeat_lines
		assert budget_samples[-1][1] >= budget_samples[0][1], heartbeat_lines
		assert budget_samples[-1][2] <= budget_samples[0][2], heartbeat_lines
		assert stdout_file.read_text(encoding="utf-8") == "stdout-budget\n"
		assert stderr_file.read_text(encoding="utf-8") == ""


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


def test_codex_heartbeat_keeps_emitting_while_descendant_holds_pipes_open() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-heartbeat-descendant-") as td:
		tmp = Path(td)
		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["CODEX_HEARTBEAT_ENABLED"] = "1"
		env["CODEX_HEARTBEAT_INTERVAL_SECS"] = "1"

		result = subprocess.run(
			[
				"bash",
				str(HEARTBEAT_SCRIPT),
				"--phase",
				"post_exit_stream_test",
				"--stdout-file",
				str(tmp / "child.stdout"),
				"--stderr-file",
				str(tmp / "child.stderr"),
				"--",
				"python3",
				"-c",
				(
					"import subprocess, sys, time; "
					"subprocess.Popen(['python3', '-c', 'import time; time.sleep(3.2)'], "
					"stdout=sys.stdout, stderr=sys.stderr); "
					"time.sleep(0.2)"
				),
			],
			env=env,
			capture_output=True,
			text=True,
			timeout=15,
		)

		assert result.returncode == 0, result.stdout + result.stderr
		heartbeat_lines = [
			line
			for line in result.stderr.splitlines()
			if line.startswith("CODEX_HEARTBEAT: phase=post_exit_stream_test elapsed_secs=")
		]
		assert len(heartbeat_lines) >= 2, result.stderr


def test_codex_heartbeat_timeout_kill_after_does_not_leave_child_running() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-heartbeat-timeout-") as td:
		tmp = Path(td)
		child_pid_file = tmp / "child.pid"
		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["CODEX_HEARTBEAT_ENABLED"] = "0"
		child_pid: int | None = None

		try:
			result = subprocess.run(
				[
					"timeout",
					"--signal=TERM",
					"--kill-after=1s",
					"1s",
					"bash",
					str(HEARTBEAT_SCRIPT),
					"--phase",
					"kill_after_test",
					"--",
					"python3",
					"-c",
					(
						"import os, signal, sys, time; "
						"signal.signal(signal.SIGTERM, signal.SIG_IGN); "
						"open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
						"time.sleep(1000)"
					),
					str(child_pid_file),
				],
				env=env,
				capture_output=True,
				text=True,
				timeout=10,
			)

			assert result.returncode != 0, result.stdout + result.stderr
			child_pid = int(child_pid_file.read_text(encoding="ascii").strip())
			deadline = time.time() + 5
			while time.time() < deadline and _pid_is_running(child_pid):
				time.sleep(0.1)
			assert not _pid_is_running(child_pid), f"heartbeat child still running after timeout kill-after: pid={child_pid}"
		finally:
			_kill_pid_if_running(child_pid)


def main() -> int:
	test_codex_heartbeat_emits_idle_lines_without_polluting_child_streams()
	test_codex_heartbeat_appends_budget_fields_when_run_budget_env_present()
	test_codex_heartbeat_disabled_still_tracks_child_activity()
	test_codex_heartbeat_keeps_emitting_while_descendant_holds_pipes_open()
	test_codex_heartbeat_timeout_kill_after_does_not_leave_child_running()
	print("OK: codex heartbeat helper contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

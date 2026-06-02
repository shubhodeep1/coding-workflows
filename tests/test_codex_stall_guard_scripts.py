#!/usr/bin/env python3
"""Focused contract tests for scripts/codex_stall_guard.sh and its wiring."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
STALL_GUARD_SCRIPT = REPO_ROOT / "scripts" / "codex_stall_guard.sh"


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


def _read_status_file(path: Path) -> dict[str, str]:
	rows: dict[str, str] = {}
	for line in path.read_text(encoding="utf-8").splitlines():
		if "=" not in line:
			continue
		key, value = line.split("=", 1)
		rows[key] = value
	return rows


def _find_single_heartbeat_file(directory: Path) -> Path:
	files = sorted(directory.glob("codex-*.json"))
	assert len(files) == 1, files
	return files[0]


def test_codex_stall_guard_observe_only_records_event_idle_without_killing_child() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-stall-guard-observe-") as td:
		tmp = Path(td)
		stdout_file = tmp / "child.stdout"
		status_file = tmp / "guard.status"
		heartbeat_dir = tmp / "heartbeats"
		child_pid_file = tmp / "child.pid"

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["CODEX_HEARTBEAT_ENABLED"] = "1"
		env["CODEX_HEARTBEAT_INTERVAL_SECS"] = "1"
		env["CODEX_STALL_GUARD_ENABLED"] = "false"
		env["CODEX_STALL_TIMEOUT_SECONDS"] = "1"
		env["CODEX_STALL_KILL_GRACE_SECONDS"] = "1"
		env["CODEX_STALL_HEARTBEAT_DIR"] = str(heartbeat_dir)
		env["GITHUB_RUN_ID"] = "123456"
		env["PR_NUMBER"] = "3044"

		result = subprocess.run(
			[
				"bash",
				str(STALL_GUARD_SCRIPT),
				"--phase",
				"stall_guard_observe_test",
				"--stdout-file",
				str(stdout_file),
				"--status-file",
				str(status_file),
				"--",
				"python3",
				"-c",
				(
					"import os, sys, time; "
					"open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
					"print('start'); sys.stdout.flush(); "
					"time.sleep(2.4); "
					"print('done'); sys.stdout.flush()"
				),
				str(child_pid_file),
			],
			env=env,
			capture_output=True,
			text=True,
			timeout=20,
		)

		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		assert "codex_stall_observed" in result.stderr
		assert "codex_stall_killed" not in result.stderr
		assert stdout_file.read_text(encoding="utf-8") == "start\ndone\n"

		status = _read_status_file(status_file)
		assert status["state"] == "observed"
		assert status["mode"] == "stall_guard_observe_test"
		assert status["last_event_kind"] == "stdout"

		heartbeat = json.loads(_find_single_heartbeat_file(heartbeat_dir).read_text(encoding="utf-8"))
		assert heartbeat["run_id"] == "123456"
		assert heartbeat["issue"] == "3044"
		assert heartbeat["mode"] == "stall_guard_observe_test"
		assert heartbeat["last_event_kind"] == "stdout"
		assert heartbeat["pid"] == int(child_pid_file.read_text(encoding="ascii"))
		assert isinstance(heartbeat["last_event_at"], int)


def test_codex_stall_guard_kill_mode_terminates_idle_child_and_returns_nonzero() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-stall-guard-kill-") as td:
		tmp = Path(td)
		stdout_file = tmp / "child.stdout"
		status_file = tmp / "guard.status"
		heartbeat_dir = tmp / "heartbeats"
		child_pid_file = tmp / "child.pid"
		child_pid: int | None = None

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["CODEX_HEARTBEAT_ENABLED"] = "1"
		env["CODEX_HEARTBEAT_INTERVAL_SECS"] = "1"
		env["CODEX_STALL_GUARD_ENABLED"] = "true"
		env["CODEX_STALL_TIMEOUT_SECONDS"] = "1"
		env["CODEX_STALL_KILL_GRACE_SECONDS"] = "1"
		env["CODEX_STALL_HEARTBEAT_DIR"] = str(heartbeat_dir)

		try:
			result = subprocess.run(
				[
					"bash",
					str(STALL_GUARD_SCRIPT),
					"--phase",
					"stall_guard_kill_test",
					"--stdout-file",
					str(stdout_file),
					"--status-file",
					str(status_file),
					"--",
					"python3",
					"-c",
					(
						"import os, signal, sys, time; "
						"signal.signal(signal.SIGTERM, signal.SIG_IGN); "
						"open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
						"print('start'); sys.stdout.flush(); "
						"time.sleep(1000)"
					),
					str(child_pid_file),
				],
				env=env,
				capture_output=True,
				text=True,
				timeout=20,
			)

			child_pid = int(child_pid_file.read_text(encoding="ascii"))
			assert result.returncode == 137, result.stderr
			assert result.stdout == ""
			assert "codex_stall_killed" in result.stderr
			assert stdout_file.read_text(encoding="utf-8") == "start\n"

			status = _read_status_file(status_file)
			assert status["state"] == "killed"
			assert status["mode"] == "stall_guard_kill_test"
			assert status["signal"] == "SIGKILL"

			deadline = time.time() + 5
			while time.time() < deadline and _pid_is_running(child_pid):
				time.sleep(0.1)
			assert not _pid_is_running(child_pid), f"stall guard child still running: pid={child_pid}"
		finally:
			_kill_pid_if_running(child_pid)


def test_stall_guard_script_and_callers_keep_the_expected_contract() -> None:
	expectations = {
		"scripts/codex_stall_guard.sh": [
			"codex_stall_observed",
			"codex_stall_killed",
			"last_event_kind",
			"CODEX_STALL_TIMEOUT_SECONDS",
		],
		"scripts/review_run_reviewers.sh": [
			"CODEX_STALL_GUARD_HELPER",
			"--phase review_run_reviewers",
			"codex_stall_killed",
		],
		"scripts/review_apply_fixes.sh": [
			"CODEX_STALL_GUARD_HELPER",
			"--phase review_apply_fixes",
			"codex_stall_killed",
		],
		"scripts/review_conflict_resolve.sh": [
			"CODEX_STALL_GUARD_HELPER",
			"--phase review_conflict_resolve",
			"codex_stall_killed",
		],
		"scripts/self_heal_validation.sh": [
			"CODEX_STALL_GUARD_HELPER",
			"--phase validate_self_heal",
			"codex_stall_killed",
		],
		"scripts/review_rb_judge.sh": [
			"CODEX_STALL_GUARD_HELPER",
			"--phase review_rb_judge",
			"--phase review_rb_fix",
			"codex_stall_killed",
		],
		"scripts/validate_process.sh": [
			"CODEX_STALL_GUARD_HELPER",
			"run_validate_codex_attempt",
			'"validate_discover"',
			'"validate_diagnose"',
			"codex_stall_killed",
		],
		"scripts/run_validation_repo_checks.sh": [
			"tests/test_codex_stall_guard_scripts.py",
		],
	}

	for relative_path, snippets in expectations.items():
		text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
		for snippet in snippets:
			assert snippet in text, f"missing {snippet!r} in {relative_path}"


def main() -> int:
	test_codex_stall_guard_observe_only_records_event_idle_without_killing_child()
	test_codex_stall_guard_kill_mode_terminates_idle_child_and_returns_nonzero()
	test_stall_guard_script_and_callers_keep_the_expected_contract()
	print("OK: codex stall guard helper contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

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
CALLER_CONTRACTS = [
	{
		"name": "review_run_reviewers",
		"phase": "review_run_reviewers",
		"stderr_file": True,
		"activity_file": True,
	},
	{
		"name": "review_apply_fixes",
		"phase": "review_apply_fixes",
		"stderr_file": False,
		"activity_file": True,
	},
	{
		"name": "review_conflict_resolve",
		"phase": "review_conflict_resolve",
		"stderr_file": False,
		"activity_file": False,
	},
	{
		"name": "review_rb_judge",
		"phase": "review_rb_judge",
		"stderr_file": True,
		"activity_file": False,
	},
	{
		"name": "review_rb_fix",
		"phase": "review_rb_fix",
		"stderr_file": True,
		"activity_file": False,
	},
	{
		"name": "validate_self_heal",
		"phase": "validate_self_heal",
		"stderr_file": False,
		"activity_file": False,
	},
	{
		"name": "validate_discover",
		"phase": "validate_discover",
		"stderr_file": False,
		"activity_file": False,
	},
	{
		"name": "validate_diagnose",
		"phase": "validate_diagnose",
		"stderr_file": False,
		"activity_file": False,
	},
]


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


def _run_guard_for_contract(
	contract: dict[str, object],
	*,
	stall_guard_enabled: bool,
	child_body: str,
) -> tuple[subprocess.CompletedProcess[str], Path, Path | None, Path | None, Path, Path, Path]:
	tmp = Path(tempfile.mkdtemp(prefix=f"codex-stall-contract-{contract['name']}-"))
	stdout_file = tmp / "child.stdout"
	stderr_file = tmp / "child.stderr" if contract["stderr_file"] else None
	activity_file = tmp / "activity.txt" if contract["activity_file"] else None
	status_file = tmp / "guard.status"
	heartbeat_dir = tmp / "heartbeats"
	child_pid_file = tmp / "child.pid"

	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["CODEX_HEARTBEAT_ENABLED"] = "1"
	env["CODEX_HEARTBEAT_INTERVAL_SECS"] = "1"
	env["CODEX_STALL_GUARD_ENABLED"] = "true" if stall_guard_enabled else "false"
	env["CODEX_STALL_TIMEOUT_SECONDS"] = "1"
	env["CODEX_STALL_KILL_GRACE_SECONDS"] = "1"
	env["CODEX_STALL_HEARTBEAT_DIR"] = str(heartbeat_dir)
	env["GITHUB_RUN_ID"] = "123456"
	env.pop("ISSUE_NUMBER", None)
	env.pop("TRACKING_ISSUE", None)
	env.pop("TRACKING_ISSUE_NUM", None)
	env["PR_NUMBER"] = "3044"

	cmd = [
		"bash",
		str(STALL_GUARD_SCRIPT),
		"--phase",
		str(contract["phase"]),
		"--stdout-file",
		str(stdout_file),
		"--status-file",
		str(status_file),
	]
	if stderr_file is not None:
		cmd.extend(["--stderr-file", str(stderr_file)])
	if activity_file is not None:
		cmd.extend(["--activity-file", str(activity_file)])
	cmd.extend(["--", "python3", "-c", child_body, str(child_pid_file)])

	result = subprocess.run(
		cmd,
		env=env,
		capture_output=True,
		text=True,
		timeout=20,
	)

	return (
		result,
		stdout_file,
		stderr_file,
		activity_file,
		status_file,
		_find_single_heartbeat_file(heartbeat_dir),
		child_pid_file,
	)


def test_stall_guard_caller_contracts_cover_observe_only_mode() -> None:
	child_body = (
		"import os, sys, time; "
		"open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
		"print('stdout-start'); sys.stdout.flush(); "
		"print('stderr-start', file=sys.stderr); sys.stderr.flush(); "
		"time.sleep(1.3); "
		"print('stdout-done'); sys.stdout.flush()"
	)

	for contract in CALLER_CONTRACTS:
		result, stdout_path, stderr_path, activity_path, status_path, heartbeat_path, pid_path = _run_guard_for_contract(
			contract,
			stall_guard_enabled=False,
			child_body=child_body,
		)

		assert result.returncode == 0, (contract["name"], result.stderr)
		assert "codex_stall_observed" in result.stderr, (contract["name"], result.stderr)
		assert "codex_stall_killed" not in result.stderr, (contract["name"], result.stderr)
		assert stdout_path.read_text(encoding="utf-8") == "stdout-start\nstdout-done\n"

		status = _read_status_file(status_path)
		assert status["state"] == "observed"
		assert status["mode"] == contract["phase"]

		heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
		assert heartbeat["mode"] == contract["phase"]
		assert heartbeat["pid"] == int(pid_path.read_text(encoding="ascii"))

		if stderr_path is not None:
			assert stderr_path.read_text(encoding="utf-8") == "stderr-start\n"
			assert "stderr-start" not in result.stderr
		else:
			assert "stderr-start" in result.stderr

		if activity_path is not None:
			assert activity_path.read_text(encoding="utf-8").strip().isdigit()


def test_stall_guard_caller_contracts_cover_kill_mode() -> None:
	child_body = (
		"import os, signal, sys, time; "
		"signal.signal(signal.SIGTERM, signal.SIG_IGN); "
		"open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
		"print('stdout-start'); sys.stdout.flush(); "
		"print('stderr-start', file=sys.stderr); sys.stderr.flush(); "
		"time.sleep(1000)"
	)

	for contract in CALLER_CONTRACTS:
		child_pid: int | None = None
		try:
			result, stdout_path, stderr_path, activity_path, status_path, heartbeat_path, pid_path = _run_guard_for_contract(
				contract,
				stall_guard_enabled=True,
				child_body=child_body,
			)

			child_pid = int(pid_path.read_text(encoding="ascii"))
			assert result.returncode == 137, (contract["name"], result.stderr)
			assert "codex_stall_killed" in result.stderr, (contract["name"], result.stderr)
			assert stdout_path.read_text(encoding="utf-8") == "stdout-start\n"

			status = _read_status_file(status_path)
			assert status["state"] == "killed"
			assert status["mode"] == contract["phase"]
			assert status["signal"] == "SIGKILL"

			heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
			assert heartbeat["mode"] == contract["phase"]
			assert heartbeat["pid"] == child_pid

			if stderr_path is not None:
				assert stderr_path.read_text(encoding="utf-8") == "stderr-start\n"
				assert "stderr-start" not in result.stderr
			else:
				assert "stderr-start" in result.stderr

			if activity_path is not None:
				assert activity_path.read_text(encoding="utf-8").strip().isdigit()

			deadline = time.time() + 5
			while time.time() < deadline and _pid_is_running(child_pid):
				time.sleep(0.1)
			assert not _pid_is_running(child_pid), f"stall guard child still running: contract={contract['name']} pid={child_pid}"
		finally:
			_kill_pid_if_running(child_pid)


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
		env.pop("ISSUE_NUMBER", None)
		env.pop("TRACKING_ISSUE", None)
		env.pop("TRACKING_ISSUE_NUM", None)
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
			"codex_stall_guard failed to write status file",
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
			"resolve_editor_network_probe_pid",
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
			"codex_stall_guard_kill_detected",
			"not attempting stderr JSON recovery",
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

	forbidden_snippets = {
		"scripts/review_run_reviewers.sh": ['2>> "${tmp_stderr}"'],
		"scripts/review_rb_judge.sh": ['2>>"${JUDGE_STDERR_FILE}"', '2>>"${RB_FIX_STDERR}"'],
	}
	for relative_path, snippets in forbidden_snippets.items():
		text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
		for snippet in snippets:
			assert snippet not in text, f"unexpected {snippet!r} in {relative_path}"


def main() -> int:
	test_codex_stall_guard_observe_only_records_event_idle_without_killing_child()
	test_codex_stall_guard_kill_mode_terminates_idle_child_and_returns_nonzero()
	test_stall_guard_caller_contracts_cover_observe_only_mode()
	test_stall_guard_caller_contracts_cover_kill_mode()
	test_stall_guard_script_and_callers_keep_the_expected_contract()
	print("OK: codex stall guard helper contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

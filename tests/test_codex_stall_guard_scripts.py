#!/usr/bin/env python3
"""Focused contract tests for scripts/codex_stall_guard.sh and its wiring."""

from __future__ import annotations

import json
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
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


def _stall_guard_test_env() -> dict[str, str]:
	env = os.environ.copy()
	for key in (
		"JOB_START_EPOCH",
		"REVIEW_SOFT_DEADLINE_MINUTES",
		"CODEX_RUN_BUDGET_START_EPOCH",
		"CODEX_RUN_BUDGET_SOFT_DEADLINE_EPOCH",
		"CODEX_RUN_BUDGET_TOTAL_SECS",
	):
		env.pop(key, None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


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


def _kill_process_group_if_running(pgid: int | None) -> None:
	if pgid is None:
		return
	for signum in (signal.SIGTERM, signal.SIGKILL):
		try:
			os.killpg(pgid, signum)
		except ProcessLookupError:
			return
		time.sleep(0.1)


def _write_fake_sudo(path: Path) -> None:
	path.write_text(
		"""#!/usr/bin/env python3
import os
import signal
import sys

args = sys.argv[1:]
if args[:2] == ["-n", "kill"]:
	with open(os.environ["FAKE_SUDO_LOG"], "a", encoding="utf-8") as handle:
		handle.write(" ".join(args) + "\\n")
	mode = os.environ.get("FAKE_SUDO_SIGNAL_MODE", "signal")
	if mode == "fail":
		raise SystemExit(42)
	if mode == "noop":
		raise SystemExit(0)
	signum = getattr(signal, "SIG" + args[2].removeprefix("-"))
	os.killpg(abs(int(args[4])), signum)
	raise SystemExit(0)
if len(args) >= 5 and args[:2] == ["-n", "-u"] and args[3] == "--":
	os.execvp(args[4], args[4:])
raise SystemExit(64)
""",
		encoding="utf-8",
	)
	path.chmod(0o755)


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

	env = _stall_guard_test_env()
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

		env = _stall_guard_test_env()
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

		env = _stall_guard_test_env()
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


def _run_isolated_stall_guard(signal_mode: str) -> tuple[subprocess.CompletedProcess[str], Path, Path, int]:
	tmp = Path(tempfile.mkdtemp(prefix=f"codex-stall-isolated-{signal_mode}-"))
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	_write_fake_sudo(bin_dir / "sudo")
	metadata_file = tmp / "process-group.env"
	sudo_log = tmp / "sudo.log"
	child_pid_file = tmp / "child.pid"
	env = _stall_guard_test_env()
	env.update(
		{
			"PATH": f"{bin_dir}:{env['PATH']}",
			"FAKE_SUDO_LOG": str(sudo_log),
			"FAKE_SUDO_SIGNAL_MODE": signal_mode,
			"CODEX_HEARTBEAT_ENABLED": "0",
			"CODEX_STALL_GUARD_ENABLED": "true",
			"CODEX_STALL_TIMEOUT_SECONDS": "1",
			"CODEX_STALL_KILL_GRACE_SECONDS": "1",
		}
	)
	current_user = pwd.getpwuid(os.getuid()).pw_name
	result = subprocess.run(
		[
			"bash",
			str(STALL_GUARD_SCRIPT),
			"--phase",
			"isolated_editor_test",
			"--process-group-file",
			str(metadata_file),
			"--",
			"sudo",
			"-n",
			"-u",
			current_user,
			"--",
			"python3",
			"-c",
			(
				"import os, signal, sys, time; "
				"signal.signal(signal.SIGTERM, signal.SIG_IGN); "
				"open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
				"print('start', flush=True); time.sleep(1000)"
			),
			str(child_pid_file),
		],
		env=env,
		capture_output=True,
		text=True,
		timeout=30,
	)
	return result, metadata_file, sudo_log, int(child_pid_file.read_text(encoding="ascii"))


def test_isolated_stall_guard_publishes_metadata_and_uses_privileged_group_signals() -> None:
	result, metadata_file, sudo_log, child_pgid = _run_isolated_stall_guard("signal")
	assert result.returncode == 137, result.stderr
	metadata = _read_status_file(metadata_file)
	assert stat.S_IMODE(metadata_file.stat().st_mode) == 0o600
	assert metadata["guard_uid"] == str(os.getuid())
	assert metadata["guard_start_time_ticks"].isdigit()
	assert metadata["child_pid"] == str(child_pgid)
	assert metadata["process_group_id"] == str(child_pgid)
	assert metadata["isolated_user"] == pwd.getpwuid(os.getuid()).pw_name
	assert metadata["signal_mode"] == "privileged"
	signal_lines = sudo_log.read_text(encoding="utf-8").splitlines()
	assert f"-n kill -TERM -- -{child_pgid}" in signal_lines
	assert f"-n kill -KILL -- -{child_pgid}" in signal_lines
	assert not _pid_is_running(child_pgid)


def test_isolated_stall_guard_reports_privileged_signal_failure() -> None:
	child_pgid: int | None = None
	try:
		result, _metadata_file, _sudo_log, child_pgid = _run_isolated_stall_guard("fail")
		assert result.returncode == 126, result.stderr
		assert "privileged process-group signal failed" in result.stderr
		assert _pid_is_running(child_pgid)
	finally:
		_kill_process_group_if_running(child_pgid)


def test_isolated_stall_guard_reports_survivors_after_privileged_kill() -> None:
	child_pgid: int | None = None
	try:
		result, _metadata_file, _sudo_log, child_pgid = _run_isolated_stall_guard("noop")
		assert result.returncode == 126, result.stderr
		assert "isolated process-group survivor" in result.stderr
		assert _pid_is_running(child_pgid)
	finally:
		_kill_process_group_if_running(child_pgid)


def _run_isolated_stall_guard_wrapper_exits_early() -> tuple[subprocess.CompletedProcess[str], Path, Path, int]:
	"""Launch an isolated command whose `sudo`-side wrapper exits at once while a
	TERM-ignoring descendant stays alive in the session. The tracked child is gone,
	so every kill and verification path must follow the surviving group."""
	tmp = Path(tempfile.mkdtemp(prefix="codex-stall-isolated-wrapper-exit-"))
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	_write_fake_sudo(bin_dir / "sudo")
	metadata_file = tmp / "process-group.env"
	sudo_log = tmp / "sudo.log"
	grandchild_pid_file = tmp / "grandchild.pid"
	env = _stall_guard_test_env()
	env.update(
		{
			"PATH": f"{bin_dir}:{env['PATH']}",
			"FAKE_SUDO_LOG": str(sudo_log),
			"FAKE_SUDO_SIGNAL_MODE": "signal",
			"CODEX_HEARTBEAT_ENABLED": "0",
			"CODEX_STALL_GUARD_ENABLED": "true",
			"CODEX_STALL_TIMEOUT_SECONDS": "1",
			"CODEX_STALL_KILL_GRACE_SECONDS": "1",
		}
	)
	current_user = pwd.getpwuid(os.getuid()).pw_name
	grandchild = (
		"import os, signal, sys, time; "
		"signal.signal(signal.SIGTERM, signal.SIG_IGN); "
		"open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
		"print('start', flush=True); time.sleep(1000)"
	)
	result = subprocess.run(
		[
			"bash",
			str(STALL_GUARD_SCRIPT),
			"--phase",
			"isolated_editor_test",
			"--process-group-file",
			str(metadata_file),
			"--",
			"sudo",
			"-n",
			"-u",
			current_user,
			"--",
			"bash",
			"-c",
			'python3 -c "$1" "$2" & exit 0',
			"wrapper",
			grandchild,
			str(grandchild_pid_file),
		],
		env=env,
		capture_output=True,
		text=True,
		timeout=30,
	)
	return result, metadata_file, sudo_log, int(grandchild_pid_file.read_text(encoding="ascii"))


def test_isolated_stall_guard_kills_group_survivors_after_wrapper_exit() -> None:
	grandchild_pid: int | None = None
	try:
		result, metadata_file, sudo_log, grandchild_pid = _run_isolated_stall_guard_wrapper_exits_early()
		assert result.returncode == 137, result.stderr
		metadata = _read_status_file(metadata_file)
		child_pgid = int(metadata["process_group_id"])
		assert grandchild_pid != child_pgid
		signal_lines = sudo_log.read_text(encoding="utf-8").splitlines()
		assert f"-n kill -TERM -- -{child_pgid}" in signal_lines, signal_lines
		assert f"-n kill -KILL -- -{child_pgid}" in signal_lines, signal_lines
		assert "codex_stall_killed" in result.stderr
		assert "isolated process-group survivor" not in result.stderr
		assert not _pid_is_running(grandchild_pid)
	finally:
		_kill_pid_if_running(grandchild_pid)


def test_pythonless_isolated_fallback_marks_unguarded_status() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-stall-pythonless-") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		fake_tr = bin_dir / "tr"
		fake_tr.write_text("#!/bin/sh\n/bin/cat\n", encoding="utf-8")
		fake_tr.chmod(0o755)
		status_file = tmp / "guard.status"
		metadata_file = tmp / "process-group.env"
		env = _stall_guard_test_env()
		env["PATH"] = str(bin_dir)
		result = subprocess.run(
			[
				"/bin/bash",
				str(STALL_GUARD_SCRIPT),
				"--phase",
				"pythonless_editor_test",
				"--status-file",
				str(status_file),
				"--process-group-file",
				str(metadata_file),
				"--",
				"/bin/sh",
				"-c",
				"exit 0",
			],
			env=env,
			capture_output=True,
			text=True,
			timeout=10,
		)
		assert result.returncode == 0, result.stderr
		assert status_file.read_text(encoding="utf-8") == "state=unguarded\n"
		assert not metadata_file.exists()


def test_isolated_stall_guard_ignores_zombie_only_probe_results() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-stall-zombie-probe-") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		_write_fake_sudo(bin_dir / "sudo")
		zombie_pid_file = tmp / "zombie.pid"
		zombie_parent = subprocess.Popen(
			[
				sys.executable,
				"-c",
				(
					"import os, pathlib, sys, time\n"
					"pid = os.fork()\n"
					"if pid == 0:\n\tos._exit(0)\n"
					"pathlib.Path(sys.argv[1]).write_text(str(pid), encoding='ascii')\n"
					"time.sleep(1000)\n"
				),
				str(zombie_pid_file),
			]
		)
		try:
			for _ in range(100):
				if zombie_pid_file.exists() and zombie_pid_file.read_text(encoding="ascii"):
					break
				time.sleep(0.05)
			zombie_pid = int(zombie_pid_file.read_text(encoding="ascii"))
			for _ in range(100):
				state = Path(f"/proc/{zombie_pid}/stat").read_text(encoding="ascii").rpartition(")")[2].split()[0]
				if state == "Z":
					break
				time.sleep(0.01)
			assert state == "Z"
			fake_pgrep = bin_dir / "pgrep"
			fake_pgrep.write_text("#!/bin/sh\nprintf '%s\\n' \"$FAKE_PGREP_PID\"\n", encoding="utf-8")
			fake_pgrep.chmod(0o755)
			env = _stall_guard_test_env()
			env.update(
				{
					"PATH": f"{bin_dir}:{env['PATH']}",
					"FAKE_PGREP_PID": str(zombie_pid),
					"FAKE_SUDO_LOG": str(tmp / "sudo.log"),
					"FAKE_SUDO_SIGNAL_MODE": "signal",
					"CODEX_HEARTBEAT_ENABLED": "0",
				}
			)
			result = subprocess.run(
				[
					"bash",
					str(STALL_GUARD_SCRIPT),
					"--phase",
					"zombie_probe_test",
					"--process-group-file",
					str(tmp / "process-group.env"),
					"--",
					"sudo",
					"-n",
					"-u",
					pwd.getpwuid(os.getuid()).pw_name,
					"--",
					"/bin/sh",
					"-c",
					"/bin/sleep 0.2",
				],
				env=env,
				capture_output=True,
				text=True,
				timeout=10,
			)
			assert result.returncode == 0, result.stderr
			assert "isolated process-group survivor" not in result.stderr
		finally:
			zombie_parent.kill()
			zombie_parent.wait(timeout=10)


def test_codex_stall_guard_heartbeat_appends_budget_fields_when_run_budget_env_present() -> None:
	with tempfile.TemporaryDirectory(prefix="codex-stall-guard-budget-") as td:
		tmp = Path(td)
		stdout_file = tmp / "child.stdout"
		status_file = tmp / "guard.status"
		heartbeat_dir = tmp / "heartbeats"

		env = _stall_guard_test_env()
		env["CODEX_HEARTBEAT_ENABLED"] = "1"
		env["CODEX_HEARTBEAT_INTERVAL_SECS"] = "1"
		env["CODEX_STALL_GUARD_ENABLED"] = "false"
		env["CODEX_STALL_TIMEOUT_SECONDS"] = "10"
		env["CODEX_STALL_KILL_GRACE_SECONDS"] = "1"
		env["CODEX_STALL_HEARTBEAT_DIR"] = str(heartbeat_dir)
		env["GITHUB_RUN_ID"] = "123456"
		env["PR_NUMBER"] = "3044"
		now_epoch = int(time.time())
		env["CODEX_RUN_BUDGET_START_EPOCH"] = str(now_epoch - 5)
		env["CODEX_RUN_BUDGET_SOFT_DEADLINE_EPOCH"] = str(now_epoch + 90)
		env["CODEX_RUN_BUDGET_TOTAL_SECS"] = "95"

		result = subprocess.run(
			[
				"bash",
				str(STALL_GUARD_SCRIPT),
				"--phase",
				"stall_guard_budget_test",
				"--stdout-file",
				str(stdout_file),
				"--status-file",
				str(status_file),
				"--",
				"python3",
				"-c",
				(
					"import sys, time; "
					"print('start'); sys.stdout.flush(); "
					"time.sleep(2.4)"
				),
			],
			env=env,
			capture_output=True,
			text=True,
			timeout=20,
		)

		assert result.returncode == 0, result.stderr
		heartbeat_lines = [
			line
			for line in result.stderr.splitlines()
			if line.startswith("CODEX_HEARTBEAT: phase=stall_guard_budget_test elapsed_secs=")
		]
		assert heartbeat_lines, result.stderr

		budget_samples: list[tuple[int, int, int]] = []
		for line in heartbeat_lines:
			match = re.fullmatch(
				r"CODEX_HEARTBEAT: phase=stall_guard_budget_test elapsed_secs=(\d+) budget_elapsed_secs=(\d+) budget_remaining_secs=(\d+)",
				line,
			)
			assert match is not None, heartbeat_lines
			budget_samples.append(tuple(int(match.group(i)) for i in (1, 2, 3)))

		assert budget_samples[0][1] >= 5, heartbeat_lines
		assert budget_samples[-1][1] >= budget_samples[0][1], heartbeat_lines
		assert budget_samples[-1][2] <= budget_samples[0][2], heartbeat_lines
		assert stdout_file.read_text(encoding="utf-8") == "start\n"


def test_stall_guard_script_and_callers_keep_the_expected_contract() -> None:
	expectations = {
		"scripts/codex_stall_guard.sh": [
			"codex_stall_observed",
			"codex_stall_killed",
			"codex_stall_guard failed to write status file",
			"last_event_kind",
			"CODEX_STALL_TIMEOUT_SECONDS",
			"budget_elapsed_secs",
			"budget_remaining_secs",
			'["sudo", "-n", "kill", f"-{signum.name.removeprefix(\'SIG\')}", "--", f"-{child_pgid}"]',
		],
		"scripts/review_run_reviewers.sh": [
			"CODEX_STALL_GUARD_HELPER",
			"--phase review_run_reviewers",
			"codex_stall_killed",
			"REVIEWER_ADVANCE:",
		],
		"scripts/review_apply_fixes.sh": [
			"CODEX_STALL_GUARD_HELPER",
			"--phase review_apply_fixes",
			"resolve_editor_network_probe_pid",
			"codex_stall_killed",
			"terminate_editor_attempt_process_group",
			"editor_isolation_verify_process_group_stopped",
			"_editor_process_group_from_guard",
			"EDITOR_PROCESS_GROUP_TERMINATION_FAILED",
		],
		"scripts/editor_isolation_preflight.sh": [
			'editor_isolation_signal_process_group',
			'sudo -n kill "-${requested_signal}" -- "-${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID}"',
			"EDITOR_ISOLATION_PROCESS_GROUP_SURVIVOR",
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

	editor_text = (REPO_ROOT / "scripts/review_apply_fixes.sh").read_text(encoding="utf-8")
	assert 'kill -TERM "${cpid}"' not in editor_text
	# A failed group termination must be visible; the post-attempt survivor check decides.
	assert 'terminate_editor_attempt_process_group "${cpid}" "${process_group_file}" || true' not in editor_text
	assert editor_text.count("EDITOR_PROCESS_GROUP_TERMINATION_FAILED attempt=${attempt} trigger=") == 2
	assert "[ \"${cmd_rc}\" -eq 78 ] || [ \"${cmd_rc}\" -eq 79 ]" in editor_text
	assert "{ [ \"${cmd_rc}\" -eq 0 ]" in editor_text
	assert "grep -qxF 'state=unguarded' \"${stall_status_file}\"" in editor_text
	# PR #4072: the watchdog reap tolerates an already-exited watchdog under set -e.
	assert 'kill "${wd_pid}" 2>/dev/null || true; wait "${wd_pid}" 2>/dev/null || true' in editor_text
	termination_start = editor_text.index("terminate_editor_attempt_process_group()")
	termination_end = editor_text.index("\nrun_editor_codex_attempt()", termination_start)
	termination_block = editor_text[termination_start:termination_end]
	assert termination_block.index("editor_isolation_signal_process_group") < termination_block.index('kill -TERM "${guard_pid}"')


def test_isolation_runtime_modules_run_directly() -> None:
	for module_name in (
		"test_editor_isolation_preflight.py",
		"test_review_editor_process_group_termination.py",
	):
		result = subprocess.run(
			[sys.executable, str(REPO_ROOT / "tests" / module_name)],
			env=_stall_guard_test_env(),
			capture_output=True,
			text=True,
			timeout=180,
		)
		assert result.returncode == 0, f"{module_name}\n{result.stdout}\n{result.stderr}"


def main() -> int:
	test_codex_stall_guard_observe_only_records_event_idle_without_killing_child()
	test_codex_stall_guard_kill_mode_terminates_idle_child_and_returns_nonzero()
	test_isolated_stall_guard_publishes_metadata_and_uses_privileged_group_signals()
	test_isolated_stall_guard_reports_privileged_signal_failure()
	test_isolated_stall_guard_reports_survivors_after_privileged_kill()
	test_isolated_stall_guard_kills_group_survivors_after_wrapper_exit()
	test_pythonless_isolated_fallback_marks_unguarded_status()
	test_isolated_stall_guard_ignores_zombie_only_probe_results()
	test_codex_stall_guard_heartbeat_appends_budget_fields_when_run_budget_env_present()
	test_stall_guard_caller_contracts_cover_observe_only_mode()
	test_stall_guard_caller_contracts_cover_kill_mode()
	test_stall_guard_script_and_callers_keep_the_expected_contract()
	test_isolation_runtime_modules_run_directly()
	print("OK: codex stall guard helper contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

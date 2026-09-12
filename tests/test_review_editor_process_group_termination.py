#!/usr/bin/env python3
"""Runtime tests for terminate_editor_attempt_process_group in scripts/review_apply_fixes.sh.

The function is extracted from the script and run in a bash that sources the
isolation helper, with a fake `sudo` on PATH that performs the group signal as
the current user. The "guard" is a python process that, like
codex_stall_guard.sh, starts its editor child in a new session.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EDITOR_SCRIPT = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
HELPER = REPO_ROOT / "scripts" / "editor_isolation_preflight.sh"

FAKE_SUDO = """#!/usr/bin/env python3
import os, signal, sys
args = sys.argv[1:]
if args[:2] == ["-n", "kill"]:
	with open(os.environ["FAKE_SUDO_LOG"], "a", encoding="utf-8") as handle:
		handle.write(" ".join(args) + "\\n")
	if os.environ.get("FAKE_SUDO_FAILURE") == "1":
		raise SystemExit(42)
	signum = getattr(signal, "SIG" + args[2].removeprefix("-"))
	os.killpg(abs(int(args[4])), signum)
	raise SystemExit(0)
raise SystemExit(64)
"""

GUARD = """
import os, signal, subprocess, sys, time
child = subprocess.Popen(
	[sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1000)"],
	start_new_session=True,
)
open(sys.argv[1], "w", encoding="ascii").write(str(child.pid))
time.sleep(1000)
"""


def _function_block(text: str, start_marker: str, end_marker: str) -> str:
	start = text.index(start_marker)
	end = text.index(end_marker, start)
	return text[start:end]


def _termination_functions() -> str:
	text = EDITOR_SCRIPT.read_text(encoding="utf-8")
	return _function_block(text, "# _editor_process_identity <pid>", "\neditor_isolation_exit_trap() {")


def _pid_is_running(pid: int) -> bool:
	"""True for a live process; a zombie (killed, not yet reaped by its
	parent or by an init that does not reap) does not count."""
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	try:
		stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
	except OSError:
		return True
	return stat_text.rpartition(")")[2].split()[0] != "Z"


def _write_process_group_metadata(path: Path, guard_pid: int, child_pid: int) -> None:
	current_user = pwd.getpwuid(os.getuid())
	guard_stat_fields = Path(f"/proc/{guard_pid}/stat").read_text(
		encoding="ascii", errors="replace"
	).rpartition(")")[2].split()
	stat_fields = Path(f"/proc/{child_pid}/stat").read_text(
		encoding="ascii", errors="replace"
	).rpartition(")")[2].split()
	path.write_text(
		f"guard_pid={guard_pid}\n"
		f"guard_uid={Path(f'/proc/{guard_pid}').stat().st_uid}\n"
		f"guard_start_time_ticks={guard_stat_fields[19]}\n"
		f"child_pid={child_pid}\n"
		f"process_group_id={child_pid}\n"
		f"isolated_user={current_user.pw_name}\n"
		f"isolated_uid={current_user.pw_uid}\n"
		f"child_uid={Path(f'/proc/{child_pid}').stat().st_uid}\n"
		f"child_start_time_ticks={stat_fields[19]}\n"
		"signal_mode=privileged\n",
		encoding="utf-8",
	)
	path.chmod(0o600)


def _run_terminate(
	tmp_path: Path,
	guard_pid: str,
	ledger: str,
	*,
	probe_failure: bool = False,
	malformed_probe: bool = False,
	sudo_failure: bool = False,
	guard_identity_mismatch: bool = False,
) -> subprocess.CompletedProcess[str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	fake_sudo = bin_dir / "sudo"
	fake_sudo.write_text(FAKE_SUDO, encoding="utf-8")
	fake_sudo.chmod(0o755)
	if probe_failure:
		fake_pgrep = bin_dir / "pgrep"
		fake_pgrep.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
		fake_pgrep.chmod(0o755)
	elif malformed_probe:
		fake_pgrep = bin_dir / "pgrep"
		fake_pgrep.write_text("#!/bin/sh\nprintf 'not-a-pid\\n'\n", encoding="utf-8")
		fake_pgrep.chmod(0o755)
	expected_guard_uid = ""
	expected_guard_start_time_ticks = ""
	guard_stat_path = Path(f"/proc/{guard_pid}/stat")
	if guard_pid.isdigit() and guard_stat_path.exists():
		guard_stat_fields = guard_stat_path.read_text(
			encoding="ascii", errors="replace"
		).rpartition(")")[2].split()
		expected_guard_uid = str(Path(f"/proc/{guard_pid}").stat().st_uid)
		expected_guard_start_time_ticks = guard_stat_fields[19]
	if guard_identity_mismatch:
		expected_guard_start_time_ticks = "1"
	script = tmp_path / "terminate_under_test.sh"
	script.write_text(
		"set -uo pipefail\n"
		f"source {HELPER}\n"
		f"EDITOR_ISOLATION_USER={pwd.getpwuid(os.getuid()).pw_name}\n"
		f"EDITOR_ISOLATION_EXPECTED_GUARD_UID={expected_guard_uid}\n"
		f"EDITOR_ISOLATION_EXPECTED_GUARD_START_TIME_TICKS={expected_guard_start_time_ticks}\n"
		+ _termination_functions()
		+ f'\nterminate_editor_attempt_process_group "{guard_pid}" "{ledger}"\n',
		encoding="utf-8",
	)
	env = {
		"PATH": f"{bin_dir}:/usr/bin:/bin",
		"FAKE_SUDO_LOG": str(tmp_path / "sudo.log"),
		"FAKE_SUDO_FAILURE": "1" if sudo_failure else "0",
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)


def test_fallback_locates_editor_group_through_guard_child_and_kills_it() -> None:
	with tempfile.TemporaryDirectory(prefix="editor-terminate-fallback-") as td:
		tmp_path = Path(td)
		pid_file = tmp_path / "editor.pid"
		guard = subprocess.Popen([sys.executable, "-c", GUARD, str(pid_file)])
		editor_pid: int | None = None
		try:
			for _ in range(100):
				if pid_file.exists() and pid_file.read_text(encoding="ascii"):
					break
				time.sleep(0.05)
			editor_pid = int(pid_file.read_text(encoding="ascii"))
			result = _run_terminate(tmp_path, str(guard.pid), "")
			assert result.returncode == 0, result.stderr
			assert f"EDITOR_PROCESS_GROUP_FALLBACK guard_pid={guard.pid} pgid={editor_pid} reason=ledger_unavailable" in result.stderr
			signal_lines = (tmp_path / "sudo.log").read_text(encoding="utf-8").splitlines()
			assert f"-n kill -TERM -- -{editor_pid}" in signal_lines, signal_lines
			assert f"-n kill -KILL -- -{editor_pid}" in signal_lines, signal_lines
			assert "EDITOR_ISOLATION_PROCESS_GROUP_SURVIVOR" not in result.stderr
			assert not _pid_is_running(editor_pid)
			assert guard.poll() is not None
		finally:
			if editor_pid is not None and _pid_is_running(editor_pid):
				os.killpg(editor_pid, 9)
			if guard.poll() is None:
				guard.kill()
			guard.wait(timeout=10)


def test_fallback_reports_unresolved_group_when_guard_has_no_session_child() -> None:
	with tempfile.TemporaryDirectory(prefix="editor-terminate-unresolved-") as td:
		tmp_path = Path(td)
		# A guard without any child in its own session: nothing to locate.
		guard = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1000)"])
		try:
			result = _run_terminate(tmp_path, str(guard.pid), "")
			assert result.returncode == 1, result.stderr
			assert f"EDITOR_PROCESS_GROUP_TERMINATION_UNRESOLVED guard_pid={guard.pid} reason=ledger_unavailable_and_no_guard_child" in result.stderr
			assert not (tmp_path / "sudo.log").exists()
			assert guard.poll() is not None
		finally:
			if guard.poll() is None:
				guard.kill()
			guard.wait(timeout=10)


def test_fallback_probe_failure_kills_group_but_reports_unverified() -> None:
	with tempfile.TemporaryDirectory(prefix="editor-terminate-probe-failure-") as td:
		tmp_path = Path(td)
		pid_file = tmp_path / "editor.pid"
		guard = subprocess.Popen([sys.executable, "-c", GUARD, str(pid_file)])
		editor_pid: int | None = None
		try:
			for _ in range(100):
				if pid_file.exists() and pid_file.read_text(encoding="ascii"):
					break
				time.sleep(0.05)
			editor_pid = int(pid_file.read_text(encoding="ascii"))
			result = _run_terminate(tmp_path, str(guard.pid), "", probe_failure=True)
			assert result.returncode == 1, result.stderr
			assert "EDITOR_PROCESS_GROUP_PROBE_FAILED" in result.stderr
			assert not _pid_is_running(editor_pid)
			assert guard.poll() is not None
		finally:
			if editor_pid is not None and _pid_is_running(editor_pid):
				os.killpg(editor_pid, 9)
			if guard.poll() is None:
				guard.kill()
			guard.wait(timeout=10)


def test_fallback_malformed_member_probe_kills_group_but_reports_unverified() -> None:
	with tempfile.TemporaryDirectory(prefix="editor-terminate-malformed-probe-") as td:
		tmp_path = Path(td)
		pid_file = tmp_path / "editor.pid"
		guard = subprocess.Popen([sys.executable, "-c", GUARD, str(pid_file)])
		editor_pid: int | None = None
		try:
			for _ in range(100):
				if pid_file.exists() and pid_file.read_text(encoding="ascii"):
					break
				time.sleep(0.05)
			editor_pid = int(pid_file.read_text(encoding="ascii"))
			result = _run_terminate(tmp_path, str(guard.pid), "", malformed_probe=True)
			assert result.returncode == 1, result.stderr
			assert "reason=invalid_member_pid" in result.stderr
			assert not _pid_is_running(editor_pid)
			assert guard.poll() is not None
		finally:
			if editor_pid is not None and _pid_is_running(editor_pid):
				os.killpg(editor_pid, 9)
			if guard.poll() is None:
				guard.kill()
			guard.wait(timeout=10)


def test_fallback_rejects_stale_guard_identity_before_privileged_signal() -> None:
	with tempfile.TemporaryDirectory(prefix="editor-terminate-stale-guard-") as td:
		tmp_path = Path(td)
		pid_file = tmp_path / "editor.pid"
		guard = subprocess.Popen([sys.executable, "-c", GUARD, str(pid_file)])
		editor_pid: int | None = None
		try:
			for _ in range(100):
				if pid_file.exists() and pid_file.read_text(encoding="ascii"):
					break
				time.sleep(0.05)
			editor_pid = int(pid_file.read_text(encoding="ascii"))
			result = _run_terminate(
				tmp_path, str(guard.pid), "", guard_identity_mismatch=True
			)
			assert result.returncode == 1, result.stderr
			assert "EDITOR_PROCESS_GROUP_GUARD_IDENTITY_MISMATCH" in result.stderr
			assert not (tmp_path / "sudo.log").exists()
			assert _pid_is_running(editor_pid)
			assert guard.poll() is None
		finally:
			if editor_pid is not None and _pid_is_running(editor_pid):
				os.killpg(editor_pid, 9)
			if guard.poll() is None:
				guard.kill()
			guard.wait(timeout=10)


def test_metadata_identity_and_probe_failures_are_hard_failures() -> None:
	with tempfile.TemporaryDirectory(prefix="editor-terminate-metadata-failure-") as td:
		tmp_path = Path(td)
		pid_file = tmp_path / "editor.pid"
		metadata_path = tmp_path / "process-group.env"
		guard = subprocess.Popen([sys.executable, "-c", GUARD, str(pid_file)])
		editor_pid: int | None = None
		try:
			for _ in range(100):
				if pid_file.exists() and pid_file.read_text(encoding="ascii"):
					break
				time.sleep(0.05)
			editor_pid = int(pid_file.read_text(encoding="ascii"))
			_write_process_group_metadata(metadata_path, guard.pid, editor_pid)
			probe_result = _run_terminate(
				tmp_path,
				str(guard.pid),
				str(metadata_path),
				probe_failure=True,
				sudo_failure=True,
			)
			assert probe_result.returncode == 1, probe_result.stderr
			assert "EDITOR_ISOLATION_PROCESS_GROUP_PROBE_FAILED" in probe_result.stderr

			metadata_text = metadata_path.read_text(encoding="utf-8")
			metadata_path.write_text(
				metadata_text.replace("child_start_time_ticks=", "child_start_time_ticks=9", 1),
				encoding="utf-8",
			)
			identity_result = _run_terminate(
				tmp_path, str(guard.pid), str(metadata_path), sudo_failure=True
			)
			assert identity_result.returncode == 1, identity_result.stderr
			assert "reason=process_identity_mismatch" in identity_result.stderr
		finally:
			if editor_pid is not None and _pid_is_running(editor_pid):
				os.killpg(editor_pid, 9)
			if guard.poll() is None:
				guard.kill()
			guard.wait(timeout=10)


def main() -> int:
	test_fallback_locates_editor_group_through_guard_child_and_kills_it()
	test_fallback_reports_unresolved_group_when_guard_has_no_session_child()
	test_fallback_probe_failure_kills_group_but_reports_unverified()
	test_fallback_malformed_member_probe_kills_group_but_reports_unverified()
	test_fallback_rejects_stale_guard_identity_before_privileged_signal()
	test_metadata_identity_and_probe_failures_are_hard_failures()
	print("OK: review editor process-group termination holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

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
	return _function_block(text, "# _editor_process_group_from_guard <guard_pid>", "\neditor_isolation_exit_trap() {")


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


def _run_terminate(tmp_path: Path, guard_pid: str, ledger: str) -> subprocess.CompletedProcess[str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	fake_sudo = bin_dir / "sudo"
	fake_sudo.write_text(FAKE_SUDO, encoding="utf-8")
	fake_sudo.chmod(0o755)
	script = tmp_path / "terminate_under_test.sh"
	script.write_text(
		"set -uo pipefail\n"
		f"source {HELPER}\n"
		f"EDITOR_ISOLATION_USER={pwd.getpwuid(os.getuid()).pw_name}\n"
		+ _termination_functions()
		+ f'\nterminate_editor_attempt_process_group "{guard_pid}" "{ledger}"\n',
		encoding="utf-8",
	)
	env = {
		"PATH": f"{bin_dir}:/usr/bin:/bin",
		"FAKE_SUDO_LOG": str(tmp_path / "sudo.log"),
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

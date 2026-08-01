#!/usr/bin/env bash
set -euo pipefail

usage()
{
	cat >&2 <<'EOF'
Usage: codex_heartbeat.sh --phase <phase> [--stdout-file <path>] [--stderr-file <path>] [--activity-file <path>] -- <command> [args...]
EOF
}

run_without_heartbeat()
{
	if [ -n "${stdout_file}" ] && [ -n "${stderr_file}" ]; then
		exec "$@" > "${stdout_file}" 2> "${stderr_file}"
	elif [ -n "${stdout_file}" ]; then
		exec "$@" > "${stdout_file}"
	elif [ -n "${stderr_file}" ]; then
		exec "$@" 2> "${stderr_file}"
	else
		exec "$@"
	fi
}

phase="unknown"
stdout_file=""
stderr_file=""
activity_file=""

while [ "$#" -gt 0 ]; do
	case "$1" in
		--phase)
			phase="${2:-}"
			shift 2
			;;
		--stdout-file)
			stdout_file="${2:-}"
			shift 2
			;;
		--stderr-file)
			stderr_file="${2:-}"
			shift 2
			;;
		--activity-file)
			activity_file="${2:-}"
			shift 2
			;;
		--help|-h)
			usage
			exit 0
			;;
		--)
			shift
			break
			;;
		*)
			echo "codex_heartbeat.sh: unknown option: $1" >&2
			usage
			exit 2
			;;
	esac
done

if [ "$#" -eq 0 ]; then
	echo "codex_heartbeat.sh: missing command" >&2
	usage
	exit 2
fi

heartbeat_enabled_raw="${CODEX_HEARTBEAT_ENABLED:-1}"
heartbeat_interval_raw="${CODEX_HEARTBEAT_INTERVAL_SECS:-30}"
heartbeat_enabled="true"
normalized_enabled="$(printf '%s' "${heartbeat_enabled_raw}" | tr '[:upper:]' '[:lower:]')"
case "${normalized_enabled}" in
	""|1|true|yes|on)
		heartbeat_enabled="true"
		;;
	0|false|no|off)
		heartbeat_enabled="false"
		;;
	*)
		echo "::warning::Invalid CODEX_HEARTBEAT_ENABLED='${heartbeat_enabled_raw}'; defaulting to enabled." >&2
		heartbeat_enabled="true"
		;;
esac

if ! [[ "${heartbeat_interval_raw}" =~ ^[0-9]+$ ]] || [ "${heartbeat_interval_raw}" -le 0 ]; then
	echo "::warning::Invalid CODEX_HEARTBEAT_INTERVAL_SECS='${heartbeat_interval_raw}'; running without heartbeat." >&2
	heartbeat_enabled="false"
	heartbeat_interval_raw="30"
fi

if ! command -v python3 >/dev/null 2>&1; then
	echo "::warning::python3 unavailable; running ${phase} without heartbeat wrapper support." >&2
	run_without_heartbeat "$@"
fi

export CODEX_HEARTBEAT_PHASE="${phase}"
export CODEX_HEARTBEAT_ENABLED_EFFECTIVE="${heartbeat_enabled}"
export CODEX_HEARTBEAT_STDOUT_FILE="${stdout_file}"
export CODEX_HEARTBEAT_STDERR_FILE="${stderr_file}"
export CODEX_HEARTBEAT_ACTIVITY_FILE="${activity_file}"
export CODEX_HEARTBEAT_INTERVAL_SECS_EFFECTIVE="${heartbeat_interval_raw}"

exec python3 -c "$(cat <<'PY'
from __future__ import annotations

import os
import errno
import selectors
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


PHASE = os.environ.get("CODEX_HEARTBEAT_PHASE", "unknown")
HEARTBEAT_ENABLED = os.environ.get("CODEX_HEARTBEAT_ENABLED_EFFECTIVE", "true") == "true"
STDOUT_FILE = os.environ.get("CODEX_HEARTBEAT_STDOUT_FILE", "")
STDERR_FILE = os.environ.get("CODEX_HEARTBEAT_STDERR_FILE", "")
ACTIVITY_FILE = os.environ.get("CODEX_HEARTBEAT_ACTIVITY_FILE", "")
INTERVAL_SECS = int(os.environ.get("CODEX_HEARTBEAT_INTERVAL_SECS_EFFECTIVE", "30"))
COMMAND = sys.argv[1:]

if not COMMAND:
	raise SystemExit(2)


def _open_output(path: str, fallback):
	if not path:
		return fallback, False
	return open(path, "wb"), True


def _write_activity_marker() -> None:
	if not ACTIVITY_FILE:
		return
	path = Path(ACTIVITY_FILE)
	tmp_path = path.with_name(f"{path.name}.tmp")
	tmp_path.write_text(str(int(time.time())), encoding="ascii")
	os.replace(tmp_path, path)


def _shell_rc(returncode: int) -> int:
	if returncode >= 0:
		return returncode
	return 128 + abs(returncode)


def _budget_suffix_for_now() -> str:
	start_epoch_raw = os.environ.get("CODEX_RUN_BUDGET_START_EPOCH", "")
	soft_deadline_epoch_raw = os.environ.get("CODEX_RUN_BUDGET_SOFT_DEADLINE_EPOCH", "")
	if not start_epoch_raw.isdigit() or not soft_deadline_epoch_raw.isdigit():
		return ""
	now_epoch = int(time.time())
	start_epoch = int(start_epoch_raw)
	soft_deadline_epoch = int(soft_deadline_epoch_raw)
	budget_elapsed = max(0, now_epoch - start_epoch)
	budget_remaining = max(0, soft_deadline_epoch - now_epoch)
	return f" budget_elapsed_secs={budget_elapsed} budget_remaining_secs={budget_remaining}"


stdout_handle, close_stdout = _open_output(STDOUT_FILE, sys.stdout.buffer)
stderr_handle, close_stderr = _open_output(STDERR_FILE, sys.stderr.buffer)
KILL_GRACE_SECS = 0.5
kill_timer: threading.Timer | None = None

child = subprocess.Popen(
	COMMAND,
	stdout=subprocess.PIPE,
	stderr=subprocess.PIPE,
	bufsize=0,
	start_new_session=True,
)

assert child.stdout is not None
assert child.stderr is not None

selector = selectors.DefaultSelector()
for stream, output_handle in ((child.stdout, stdout_handle), (child.stderr, stderr_handle)):
	os.set_blocking(stream.fileno(), False)
	selector.register(stream, selectors.EVENT_READ, output_handle)


def _kill_child_group_if_running() -> None:
	if child.poll() is not None:
		return
	try:
		child_pgid = os.getpgid(child.pid)
	except ProcessLookupError:
		return
	try:
		if child_pgid != os.getpgrp():
			os.killpg(child_pgid, signal.SIGKILL)
		else:
			child.kill()
	except ProcessLookupError:
		return


def _forward_signal(signum: int, _frame) -> None:
	global kill_timer
	if child.poll() is not None:
		return
	try:
		child_pgid = os.getpgid(child.pid)
	except ProcessLookupError:
		return
	try:
		if child_pgid != os.getpgrp():
			os.killpg(child_pgid, signum)
		else:
			child.send_signal(signum)
	except ProcessLookupError:
		return
	if kill_timer is None or not kill_timer.is_alive():
		# Keep the grace short so an upstream `timeout --kill-after` cannot
		# orphan a TERM-ignoring child process once the wrapper places the
		# child tree in its own session.
		kill_timer = threading.Timer(KILL_GRACE_SECS, _kill_child_group_if_running)
		kill_timer.daemon = True
		kill_timer.start()


for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
	try:
		signal.signal(signum, _forward_signal)
	except OSError:
		pass

last_output_at = time.monotonic()
next_heartbeat_at = last_output_at + INTERVAL_SECS

try:
	open_streams = 2
	while open_streams > 0:
		if HEARTBEAT_ENABLED:
			now = time.monotonic()
			timeout = max(0.0, next_heartbeat_at - now)
		else:
			timeout = 1.0
		try:
			events = selector.select(timeout)
		except InterruptedError:
			continue
		except OSError as exc:
			if exc.errno == errno.EINTR:
				continue
			raise
		if events:
			for key, _mask in events:
				stream = key.fileobj
				output_handle = key.data
				chunk = os.read(stream.fileno(), 65536)
				if not chunk:
					selector.unregister(stream)
					stream.close()
					open_streams -= 1
					continue
				last_output_at = time.monotonic()
				next_heartbeat_at = last_output_at + INTERVAL_SECS
				_write_activity_marker()
				output_handle.write(chunk)
				output_handle.flush()
			continue

		if HEARTBEAT_ENABLED:
			# Keep ticking while any inherited stdout/stderr pipe stays open.
			# Descendants can outlive the direct child briefly; gating on
			# child.poll() would let next_heartbeat_at go stale and busy-spin.
			elapsed = int(time.monotonic() - last_output_at)
			os.write(2, f"CODEX_HEARTBEAT: phase={PHASE} elapsed_secs={elapsed}{_budget_suffix_for_now()}\n".encode("utf-8"))
			_write_activity_marker()
			next_heartbeat_at += INTERVAL_SECS
			while next_heartbeat_at <= time.monotonic():
				next_heartbeat_at += INTERVAL_SECS

	returncode = child.wait()
finally:
	selector.close()
	if kill_timer is not None:
		kill_timer.cancel()
	if close_stdout:
		stdout_handle.close()
	if close_stderr:
		stderr_handle.close()

raise SystemExit(_shell_rc(returncode))
PY
)" "$@"

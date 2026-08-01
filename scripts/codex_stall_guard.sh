#!/usr/bin/env bash
set -euo pipefail

usage()
{
	cat >&2 <<'EOF'
Usage: codex_stall_guard.sh --phase <phase> [--stdout-file <path>] [--stderr-file <path>] [--activity-file <path>] [--status-file <path>] -- <command> [args...]
EOF
}

run_without_guard()
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
status_file=""

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
		--status-file)
			status_file="${2:-}"
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
			echo "codex_stall_guard.sh: unknown option: $1" >&2
			usage
			exit 2
			;;
	esac
done

if [ "$#" -eq 0 ]; then
	echo "codex_stall_guard.sh: missing command" >&2
	usage
	exit 2
fi

heartbeat_enabled_raw="${CODEX_HEARTBEAT_ENABLED:-1}"
heartbeat_interval_raw="${CODEX_HEARTBEAT_INTERVAL_SECS:-30}"
stall_guard_enabled_raw="${CODEX_STALL_GUARD_ENABLED:-false}"
stall_timeout_raw="${CODEX_STALL_TIMEOUT_SECONDS:-600}"
stall_kill_grace_raw="${CODEX_STALL_KILL_GRACE_SECONDS:-30}"
stall_heartbeat_dir="${CODEX_STALL_HEARTBEAT_DIR:-${RUNNER_TEMP:-/tmp}/codex-heartbeats}"
stall_run_id="${GITHUB_RUN_ID:-${RUN_ID:-}}"
stall_issue="${ISSUE_NUMBER:-${PR_NUMBER:-${TRACKING_ISSUE:-${TRACKING_ISSUE_NUM:-}}}}"

heartbeat_enabled="true"
normalized_heartbeat_enabled="$(printf '%s' "${heartbeat_enabled_raw}" | tr '[:upper:]' '[:lower:]')"
case "${normalized_heartbeat_enabled}" in
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

stall_guard_enabled="false"
normalized_stall_guard_enabled="$(printf '%s' "${stall_guard_enabled_raw}" | tr '[:upper:]' '[:lower:]')"
case "${normalized_stall_guard_enabled}" in
	""|0|false|no|off)
		stall_guard_enabled="false"
		;;
	1|true|yes|on)
		stall_guard_enabled="true"
		;;
	*)
		echo "::warning::Invalid CODEX_STALL_GUARD_ENABLED='${stall_guard_enabled_raw}'; defaulting to observe-only." >&2
		stall_guard_enabled="false"
		;;
esac

if ! [[ "${heartbeat_interval_raw}" =~ ^[0-9]+$ ]] || [ "${heartbeat_interval_raw}" -le 0 ]; then
	echo "::warning::Invalid CODEX_HEARTBEAT_INTERVAL_SECS='${heartbeat_interval_raw}'; running without heartbeat wrapper support." >&2
	heartbeat_enabled="false"
	heartbeat_interval_raw="30"
fi

if ! [[ "${stall_timeout_raw}" =~ ^[0-9]+$ ]] || [ "${stall_timeout_raw}" -le 0 ]; then
	echo "::warning::Invalid CODEX_STALL_TIMEOUT_SECONDS='${stall_timeout_raw}'; defaulting to 600." >&2
	stall_timeout_raw="600"
fi

if ! [[ "${stall_kill_grace_raw}" =~ ^[0-9]+$ ]] || [ "${stall_kill_grace_raw}" -le 0 ]; then
	echo "::warning::Invalid CODEX_STALL_KILL_GRACE_SECONDS='${stall_kill_grace_raw}'; defaulting to 30." >&2
	stall_kill_grace_raw="30"
fi

if ! command -v python3 >/dev/null 2>&1; then
	echo "::warning::python3 unavailable; running ${phase} without codex stall guard wrapper support." >&2
	run_without_guard "$@"
fi

export CODEX_STALL_GUARD_PHASE="${phase}"
export CODEX_STALL_GUARD_STDOUT_FILE="${stdout_file}"
export CODEX_STALL_GUARD_STDERR_FILE="${stderr_file}"
export CODEX_STALL_GUARD_ACTIVITY_FILE="${activity_file}"
export CODEX_STALL_GUARD_STATUS_FILE="${status_file}"
export CODEX_STALL_GUARD_HEARTBEAT_ENABLED="${heartbeat_enabled}"
export CODEX_STALL_GUARD_HEARTBEAT_INTERVAL_SECS="${heartbeat_interval_raw}"
export CODEX_STALL_GUARD_ENABLED_EFFECTIVE="${stall_guard_enabled}"
export CODEX_STALL_GUARD_TIMEOUT_SECONDS_EFFECTIVE="${stall_timeout_raw}"
export CODEX_STALL_KILL_GRACE_SECONDS_EFFECTIVE="${stall_kill_grace_raw}"
export CODEX_STALL_HEARTBEAT_DIR_EFFECTIVE="${stall_heartbeat_dir}"
export CODEX_STALL_RUN_ID="${stall_run_id}"
export CODEX_STALL_ISSUE="${stall_issue}"

exec python3 -c "$(cat <<'PY'
from __future__ import annotations

import errno
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


PHASE = os.environ.get("CODEX_STALL_GUARD_PHASE", "unknown")
STDOUT_FILE = os.environ.get("CODEX_STALL_GUARD_STDOUT_FILE", "")
STDERR_FILE = os.environ.get("CODEX_STALL_GUARD_STDERR_FILE", "")
ACTIVITY_FILE = os.environ.get("CODEX_STALL_GUARD_ACTIVITY_FILE", "")
STATUS_FILE = os.environ.get("CODEX_STALL_GUARD_STATUS_FILE", "")
HEARTBEAT_ENABLED = os.environ.get("CODEX_STALL_GUARD_HEARTBEAT_ENABLED", "true") == "true"
HEARTBEAT_INTERVAL_SECS = int(os.environ.get("CODEX_STALL_GUARD_HEARTBEAT_INTERVAL_SECS", "30"))
STALL_GUARD_ENABLED = os.environ.get("CODEX_STALL_GUARD_ENABLED_EFFECTIVE", "false") == "true"
STALL_TIMEOUT_SECS = int(os.environ.get("CODEX_STALL_GUARD_TIMEOUT_SECONDS_EFFECTIVE", "600"))
STALL_KILL_GRACE_SECS = int(os.environ.get("CODEX_STALL_KILL_GRACE_SECONDS_EFFECTIVE", "30"))
HEARTBEAT_DIR = Path(os.environ.get("CODEX_STALL_HEARTBEAT_DIR_EFFECTIVE", "/tmp/codex-heartbeats"))
RUN_ID = os.environ.get("CODEX_STALL_RUN_ID", "")
ISSUE = os.environ.get("CODEX_STALL_ISSUE", "")
COMMAND = sys.argv[1:]

if not COMMAND:
	raise SystemExit(2)


def _safe_write_text_atomic(path: Path, content: str) -> bool:
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
		tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
		tmp_path.write_text(content, encoding="utf-8")
		os.replace(tmp_path, path)
		return True
	except OSError:
		return False


def _open_output(path: str, fallback):
	if not path:
		return fallback, False
	return open(path, "wb"), True


def _write_activity_marker() -> None:
	if not ACTIVITY_FILE:
		return
	activity_path = Path(ACTIVITY_FILE)
	_safe_write_text_atomic(activity_path, f"{int(time.time())}\n")


def _shell_rc(returncode: int) -> int:
	if returncode >= 0:
		return returncode
	return 128 + abs(returncode)


def _emit_wrapper_stderr(line: str) -> None:
	try:
		os.write(2, line.encode("utf-8", errors="replace"))
	except OSError:
		return


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

heartbeat_path = HEARTBEAT_DIR / f"codex-{child.pid}.json"
status_path = Path(STATUS_FILE) if STATUS_FILE else HEARTBEAT_DIR / f"codex-{child.pid}.status"
if status_path.exists():
	try:
		status_path.unlink()
	except OSError:
		pass

last_event_epoch = int(time.time())
last_event_kind = "spawned"


def _write_heartbeat(event_kind: str, event_epoch: int | None = None) -> None:
	payload = {
		"run_id": RUN_ID,
		"issue": ISSUE,
		"mode": PHASE,
		"last_event_at": int(event_epoch if event_epoch is not None else time.time()),
		"last_event_kind": event_kind,
		"pid": child.pid,
	}
	try:
		HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
		_safe_write_text_atomic(heartbeat_path, json.dumps(payload, sort_keys=True) + "\n")
	except OSError:
		return


def _write_status(state: str, idle_secs: int, signal_name: str = "") -> None:
	rows = [
		f"state={state}",
		f"pid={child.pid}",
		f"mode={PHASE}",
		f"idle_secs={idle_secs}",
		f"last_event_at={last_event_epoch}",
		f"last_event_kind={last_event_kind}",
	]
	if signal_name:
		rows.append(f"signal={signal_name}")
	if not _safe_write_text_atomic(status_path, "\n".join(rows) + "\n"):
		_emit_wrapper_stderr(f"::warning::codex_stall_guard failed to write status file {status_path}\n")


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
		kill_timer = threading.Timer(0.5, _kill_child_group_if_running)
		kill_timer.daemon = True
		kill_timer.start()


for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
	try:
		signal.signal(signum, _forward_signal)
	except OSError:
		pass


def _signal_child_group(signum: signal.Signals) -> None:
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


selector = selectors.DefaultSelector()
for stream, output_handle, event_kind in (
	(child.stdout, stdout_handle, "stdout"),
	(child.stderr, stderr_handle, "stderr"),
):
	os.set_blocking(stream.fileno(), False)
	selector.register(stream, selectors.EVENT_READ, (output_handle, event_kind))

_write_heartbeat(last_event_kind, last_event_epoch)

last_child_event_monotonic = time.monotonic()
next_heartbeat_at = last_child_event_monotonic + HEARTBEAT_INTERVAL_SECS
observed_signature: tuple[int, str] | None = None
guard_term_sent_at: float | None = None
guard_signal_name = ""
wrapper_returncode: int | None = None

try:
	open_streams = 2
	while open_streams > 0:
		now = time.monotonic()
		timeout_candidates = [1.0]
		if HEARTBEAT_ENABLED:
			timeout_candidates.append(max(0.0, next_heartbeat_at - now))
		if guard_term_sent_at is not None:
			timeout_candidates.append(max(0.0, STALL_KILL_GRACE_SECS - (now - guard_term_sent_at)))
		timeout = min(timeout_candidates)
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
				output_handle, event_kind = key.data
				chunk = os.read(stream.fileno(), 65536)
				if not chunk:
					selector.unregister(stream)
					stream.close()
					open_streams -= 1
					continue
				last_child_event_monotonic = time.monotonic()
				last_event_epoch = int(time.time())
				last_event_kind = event_kind
				observed_signature = None
				next_heartbeat_at = last_child_event_monotonic + HEARTBEAT_INTERVAL_SECS
				_write_activity_marker()
				_write_heartbeat(last_event_kind, last_event_epoch)
				output_handle.write(chunk)
				output_handle.flush()

		now = time.monotonic()
		if HEARTBEAT_ENABLED and now >= next_heartbeat_at:
			elapsed = int(now - last_child_event_monotonic)
			_emit_wrapper_stderr(f"CODEX_HEARTBEAT: phase={PHASE} elapsed_secs={elapsed}{_budget_suffix_for_now()}\n")
			_write_activity_marker()
			next_heartbeat_at += HEARTBEAT_INTERVAL_SECS
			while next_heartbeat_at <= time.monotonic():
				next_heartbeat_at += HEARTBEAT_INTERVAL_SECS

		if child.poll() is None:
			idle_secs = int(now - last_child_event_monotonic)
			if idle_secs >= STALL_TIMEOUT_SECS:
				signature = (last_event_epoch, last_event_kind)
				if observed_signature != signature:
					if STALL_GUARD_ENABLED:
						_emit_wrapper_stderr(
							f"codex_stall_killed pid={child.pid} mode={PHASE} idle_secs={idle_secs} last_event_kind={last_event_kind} signal=SIGTERM\n"
						)
						_write_status("killed", idle_secs, "SIGTERM")
					else:
						_emit_wrapper_stderr(
							f"codex_stall_observed pid={child.pid} mode={PHASE} idle_secs={idle_secs} last_event_kind={last_event_kind} enabled=false\n"
						)
						_write_status("observed", idle_secs)
					observed_signature = signature
				if STALL_GUARD_ENABLED and guard_term_sent_at is None:
					guard_term_sent_at = time.monotonic()
					guard_signal_name = "SIGTERM"
					_signal_child_group(signal.SIGTERM)
			elif guard_term_sent_at is None:
				observed_signature = None

		if guard_term_sent_at is not None and child.poll() is None:
			if (time.monotonic() - guard_term_sent_at) >= STALL_KILL_GRACE_SECS and guard_signal_name != "SIGKILL":
				guard_signal_name = "SIGKILL"
				_signal_child_group(signal.SIGKILL)
				_write_status("killed", int(time.monotonic() - last_child_event_monotonic), "SIGKILL")

	returncode = child.wait()
	if guard_term_sent_at is not None:
		if guard_signal_name != "SIGKILL" and returncode < 0 and abs(returncode) == signal.SIGKILL:
			guard_signal_name = "SIGKILL"
		elif not guard_signal_name:
			guard_signal_name = "SIGTERM"
		_write_status("killed", int(max(0.0, time.monotonic() - last_child_event_monotonic)), guard_signal_name)
		wrapper_returncode = 137
	else:
		wrapper_returncode = _shell_rc(returncode)
finally:
	selector.close()
	if kill_timer is not None:
		kill_timer.cancel()
	if child.poll() is None:
		_kill_child_group_if_running()
	if close_stdout:
		stdout_handle.close()
	if close_stderr:
		stderr_handle.close()

raise SystemExit(0 if wrapper_returncode is None else wrapper_returncode)
PY
)" "$@"

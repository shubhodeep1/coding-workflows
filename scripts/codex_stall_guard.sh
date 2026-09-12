#!/usr/bin/env bash
set -euo pipefail

usage()
{
	cat >&2 <<'EOF'
Usage: codex_stall_guard.sh --phase <phase> [--stdout-file <path>] [--stderr-file <path>] [--activity-file <path>] [--status-file <path>] [--process-group-file <path>] -- <command> [args...]
EOF
}

run_without_guard()
{
	if [ -n "${process_group_file}" ] && [ -n "${status_file}" ]; then
		printf 'state=unguarded\n' > "${status_file}" \
			|| echo "::warning::Could not record the Python-less stall-guard fallback." >&2
	fi
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
process_group_file=""

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
		--process-group-file)
			process_group_file="${2:-}"
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
export CODEX_STALL_GUARD_PROCESS_GROUP_FILE="${process_group_file}"
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
import pwd
import re
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
PROCESS_GROUP_FILE = os.environ.get("CODEX_STALL_GUARD_PROCESS_GROUP_FILE", "")
HEARTBEAT_ENABLED = os.environ.get("CODEX_STALL_GUARD_HEARTBEAT_ENABLED", "true") == "true"
HEARTBEAT_INTERVAL_SECS = int(os.environ.get("CODEX_STALL_GUARD_HEARTBEAT_INTERVAL_SECS", "30"))
STALL_GUARD_ENABLED = os.environ.get("CODEX_STALL_GUARD_ENABLED_EFFECTIVE", "false") == "true"
STALL_TIMEOUT_SECS = int(os.environ.get("CODEX_STALL_GUARD_TIMEOUT_SECONDS_EFFECTIVE", "600"))
STALL_KILL_GRACE_SECS = int(os.environ.get("CODEX_STALL_KILL_GRACE_SECONDS_EFFECTIVE", "30"))
HEARTBEAT_DIR = Path(os.environ.get("CODEX_STALL_HEARTBEAT_DIR_EFFECTIVE", "/tmp/codex-heartbeats"))
RUN_ID = os.environ.get("CODEX_STALL_RUN_ID", "")
ISSUE = os.environ.get("CODEX_STALL_ISSUE", "")
COMMAND = sys.argv[1:]


def _isolated_user_from_command() -> str:
	if len(COMMAND) < 5 or COMMAND[:3] != ["sudo", "-n", "-u"] or COMMAND[4] != "--":
		return ""
	candidate = COMMAND[3]
	if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,254}\$?", candidate) is None:
		return ""
	try:
		pwd.getpwnam(candidate)
	except KeyError:
		return ""
	return candidate


ISOLATED_USER = _isolated_user_from_command()

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


def _process_identity(pid: int) -> tuple[int, int] | None:
	stat_path = Path(f"/proc/{pid}/stat")
	try:
		stat_fields = stat_path.read_text(encoding="ascii", errors="replace").rpartition(")")[2].split()
		process_uid = stat_path.stat().st_uid
	except FileNotFoundError:
		return None
	if len(stat_fields) <= 19 or not stat_fields[19].isdigit():
		raise ValueError(f"invalid /proc/{pid}/stat")
	return int(stat_fields[19]), process_uid


stdout_handle, close_stdout = _open_output(STDOUT_FILE, sys.stdout.buffer)
stderr_handle, close_stderr = _open_output(STDERR_FILE, sys.stderr.buffer)
kill_timer: threading.Timer | None = None
signal_failure = False

try:
	guard_identity = _process_identity(os.getpid())
except ValueError as exc:
	_emit_wrapper_stderr(f"::error::codex_stall_guard could not capture guard process identity: {exc}\n")
	raise SystemExit(126)
if guard_identity is None:
	_emit_wrapper_stderr("::error::codex_stall_guard could not capture guard process identity\n")
	raise SystemExit(126)
GUARD_START_TIME_TICKS, GUARD_UID = guard_identity

child = subprocess.Popen(
	COMMAND,
	stdout=subprocess.PIPE,
	stderr=subprocess.PIPE,
	bufsize=0,
	start_new_session=True,
)

child_pgid = os.getpgid(child.pid)
child_identity = _process_identity(child.pid)
if child_identity is None:
	_emit_wrapper_stderr(
		f"::error::codex_stall_guard could not capture child process identity pgid={child_pgid}; terminating group\n"
	)
	CHILD_START_TIME_TICKS, CHILD_UID = -1, -1
else:
	CHILD_START_TIME_TICKS, CHILD_UID = child_identity
ISOLATED_UID = pwd.getpwnam(ISOLATED_USER).pw_uid if ISOLATED_USER else -1


def _write_process_group_metadata() -> None:
	if not PROCESS_GROUP_FILE:
		return
	metadata_path = Path(PROCESS_GROUP_FILE)
	metadata_content = "\n".join(
		(
			f"guard_pid={os.getpid()}",
			f"guard_uid={GUARD_UID}",
			f"guard_start_time_ticks={GUARD_START_TIME_TICKS}",
			f"child_pid={child.pid}",
			f"process_group_id={child_pgid}",
			f"isolated_user={ISOLATED_USER}",
			f"isolated_uid={ISOLATED_UID}",
			f"child_uid={CHILD_UID}",
			f"child_start_time_ticks={CHILD_START_TIME_TICKS}",
			f"signal_mode={'privileged' if ISOLATED_USER else 'standard'}",
		)
	) + "\n"
	try:
		metadata_path.parent.mkdir(parents=True, exist_ok=True)
		tmp_path = metadata_path.with_name(f".{metadata_path.name}.tmp.{os.getpid()}.{time.time_ns()}")
		fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			handle.write(metadata_content)
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(tmp_path, metadata_path)
	except OSError as exc:
		# Without the ledger neither the watchdog nor cleanup can find the
		# editor's group: take the child down here rather than orphan it.
		_emit_wrapper_stderr(f"::error::codex_stall_guard failed to publish process-group metadata: {exc}\n")
		_kill_child_group_if_running()
		raise SystemExit(126)


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


def _isolated_group_has_members() -> bool:
	if not ISOLATED_USER:
		return False
	try:
		result = subprocess.run(
			["pgrep", "-u", ISOLATED_USER, "-g", str(child_pgid)],
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			text=True,
			check=False,
		)
	except OSError as exc:
		_emit_wrapper_stderr(
			f"::error::codex_stall_guard could not execute isolated process-group probe pgid={child_pgid} user={ISOLATED_USER}: {exc}\n"
		)
		return True
	if result.returncode == 0:
		for member_pid_text in result.stdout.splitlines():
			if re.fullmatch(r"[1-9][0-9]*", member_pid_text) is None:
				_emit_wrapper_stderr(
					f"::error::codex_stall_guard received invalid process-group member pid={member_pid_text!r} pgid={child_pgid}\n"
				)
				return True
			try:
				member_state_fields = Path(f"/proc/{member_pid_text}/stat").read_text(
					encoding="ascii", errors="replace"
				).rpartition(")")[2].split()
			except FileNotFoundError:
				continue
			except OSError as exc:
				_emit_wrapper_stderr(
					f"::error::codex_stall_guard could not inspect process-group member pid={member_pid_text}: {exc}\n"
				)
				return True
			if not member_state_fields or member_state_fields[0] != "Z":
				return True
		return False
	if result.returncode == 1:
		return False
	_emit_wrapper_stderr(
		f"::error::codex_stall_guard could not verify isolated process group pgid={child_pgid} user={ISOLATED_USER} rc={result.returncode}\n"
	)
	return True


def _process_group_exists() -> bool:
	try:
		result = subprocess.run(
			["pgrep", "-g", str(child_pgid)],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			check=False,
		)
	except OSError as exc:
		_emit_wrapper_stderr(
			f"::error::codex_stall_guard could not execute process-group probe pgid={child_pgid}: {exc}\n"
		)
		return True
	if result.returncode == 0:
		return True
	if result.returncode == 1:
		return False
	_emit_wrapper_stderr(
		f"::error::codex_stall_guard could not inspect process group pgid={child_pgid} rc={result.returncode}\n"
	)
	return True


def _verify_isolated_group_stopped() -> bool:
	if not ISOLATED_USER:
		return True
	for _attempt in range(20):
		if not _isolated_group_has_members():
			return True
		time.sleep(0.1)
	_emit_wrapper_stderr(
		f"::error::codex_stall_guard isolated process-group survivor pgid={child_pgid} user={ISOLATED_USER}\n"
	)
	return False


def _reap_child_after_kill() -> None:
	try:
		child.wait(timeout=2)
	except subprocess.TimeoutExpired:
		return


def _isolated_group_alive() -> bool:
	"""True while the tracked child runs or, for an isolated launch, while any
	process owned by the isolated user is still in the child's process group.
	The `sudo` wrapper can exit ahead of its `nobody` descendants (a TERM it
	relays is honoured by sudo but ignored by the editor), and every kill and
	verification path below must keep targeting the surviving group."""
	if child.poll() is None:
		return True
	return _isolated_group_has_members()


def _child_identity_matches() -> bool:
	global signal_failure
	try:
		current_identity = _process_identity(child.pid)
	except (OSError, ValueError) as exc:
		_emit_wrapper_stderr(
			f"::error::codex_stall_guard could not verify process-group identity pgid={child_pgid}: {exc}\n"
		)
		signal_failure = True
		return False
	if current_identity is None:
		return True
	if current_identity != (CHILD_START_TIME_TICKS, CHILD_UID):
		_emit_wrapper_stderr(
			f"::error::codex_stall_guard process-group identity mismatch pgid={child_pgid}; refusing privileged signal\n"
		)
		signal_failure = True
		return False
	return True


def _signal_child_group(signum: signal.Signals) -> bool:
	global signal_failure
	if not _isolated_group_alive():
		return True
	if ISOLATED_USER:
		if not _child_identity_matches():
			return False
		result = subprocess.run(
		["sudo", "-n", "kill", f"-{signum.name.removeprefix('SIG')}", "--", f"-{child_pgid}"],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		check=False,
		)
		if result.returncode == 0:
			return True
		# A concurrently-exited group is successful convergence. Any process
		# still in the group makes the privileged-command failure explicit.
		if not _process_group_exists():
			return True
		_emit_wrapper_stderr(
			f"::error::codex_stall_guard privileged process-group signal failed pgid={child_pgid} user={ISOLATED_USER} signal={signum.name} rc={result.returncode}\n"
		)
		signal_failure = True
		return False
	try:
		os.killpg(child_pgid, signum)
	except ProcessLookupError:
		return True
	except PermissionError:
		_emit_wrapper_stderr(
			f"::error::codex_stall_guard process-group signal denied pgid={child_pgid} signal={signum.name}\n"
		)
		signal_failure = True
		return False
	return True


def _kill_child_group_if_running() -> None:
	global signal_failure
	if _isolated_group_alive():
		_signal_child_group(signal.SIGKILL)
		_reap_child_after_kill()
	if not _verify_isolated_group_stopped():
		signal_failure = True


def _forward_signal(signum: int, _frame) -> None:
	global kill_timer
	if not _isolated_group_alive():
		return
	_signal_child_group(signal.Signals(signum))
	if kill_timer is None or not kill_timer.is_alive():
		kill_timer = threading.Timer(0.5, _kill_child_group_if_running)
		kill_timer.daemon = True
		kill_timer.start()


for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
	try:
		signal.signal(signum, _forward_signal)
	except OSError:
		pass


if child_identity is None:
	# Use the checked group-signal and survivor-verification path even when
	# the short-lived wrapper exited before its identity could be captured.
	_kill_child_group_if_running()
	raise SystemExit(126)


_write_process_group_metadata()

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
		if signal_failure:
			wrapper_returncode = 126
			break
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

		group_alive = _isolated_group_alive()
		if group_alive:
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

		if guard_term_sent_at is not None and group_alive:
			if (time.monotonic() - guard_term_sent_at) >= STALL_KILL_GRACE_SECS and guard_signal_name != "SIGKILL":
				guard_signal_name = "SIGKILL"
				_signal_child_group(signal.SIGKILL)
				_reap_child_after_kill()
				_write_status("killed", int(time.monotonic() - last_child_event_monotonic), "SIGKILL")
				if not _verify_isolated_group_stopped():
					signal_failure = True

	returncode = child.wait() if wrapper_returncode is None else None
	if guard_term_sent_at is not None and returncode is not None:
		if guard_signal_name != "SIGKILL" and returncode < 0 and abs(returncode) == signal.SIGKILL:
			guard_signal_name = "SIGKILL"
		elif not guard_signal_name:
			guard_signal_name = "SIGTERM"
		_write_status("killed", int(max(0.0, time.monotonic() - last_child_event_monotonic)), guard_signal_name)
		wrapper_returncode = 137
	elif returncode is not None:
		wrapper_returncode = _shell_rc(returncode)
	if ISOLATED_USER and not _verify_isolated_group_stopped():
		_emit_wrapper_stderr(
			f"::error::codex_stall_guard isolated process-group survivors after child exit pgid={child_pgid} user={ISOLATED_USER}; sending SIGKILL\n"
		)
		_signal_child_group(signal.SIGKILL)
		if not _verify_isolated_group_stopped():
			wrapper_returncode = 126
finally:
	selector.close()
	if kill_timer is not None:
		kill_timer.cancel()
	if _isolated_group_alive():
		_kill_child_group_if_running()
	if close_stdout:
		stdout_handle.close()
	if close_stderr:
		stderr_handle.close()

raise SystemExit(0 if wrapper_returncode is None else wrapper_returncode)
PY
)" "$@"

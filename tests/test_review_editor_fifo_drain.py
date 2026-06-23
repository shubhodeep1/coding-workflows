#!/usr/bin/env python3
"""Regression tests for the editor stderr-FIFO drain hang in
``scripts/review_apply_fixes.sh``.

The editor retry loop pipes codex stderr through a named pipe (FIFO) to a
background "heartbeat reader". When the watchdog stall-kills codex it signals
only the codex PID, so codex's orphaned tool-subprocesses can keep the FIFO's
write-end open. The reader's ``read`` then never sees EOF, and the unbounded
drain ``wait "${_hb_reader_pid}"`` blocks until GitHub's hard job ceiling
(~4h). This is exactly what wedged the review of PR #3095 (run 26977120613:
"Editor killed — no output for 1205s ..." followed by ~3h of silence and a
"maximum execution time of 4h" cancellation, leaving orphan ``bash`` PIDs).

The fix bounds the drain with ``EDITOR_DRAIN_GRACE_SECS`` and, on timeout,
reaps whatever still holds the FIFO via ``_reap_editor_fifo_holders`` — which
both unblocks the reader and stops any lingering ``danger-full-access`` codex
child. These tests pin the wiring and exercise the real shipped reaper against
a faithful orphan-held-FIFO reproduction.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APPLY_FIXES_SCRIPT = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
WATCHDOG_HELPERS_SCRIPT = REPO_ROOT / "scripts" / "watchdog_helpers.sh"


def _extract_function(name: str) -> str:
	"""Return the source of a top-level shell function via brace matching."""
	text = WATCHDOG_HELPERS_SCRIPT.read_text(encoding="utf-8")
	start = re.search(rf"(?m)^{re.escape(name)}\(\)\s*\{{", text)
	assert start is not None, f"function {name}() not found in {WATCHDOG_HELPERS_SCRIPT}"
	depth = 0
	i = text.index("{", start.start())
	for j in range(i, len(text)):
		if text[j] == "{":
			depth += 1
		elif text[j] == "}":
			depth -= 1
			if depth == 0:
				return text[start.start():j + 1]
	raise AssertionError(f"unbalanced braces extracting {name}()")


def _run_bash(script: str, timeout: float = 45.0) -> subprocess.CompletedProcess[str]:
	"""Run a bash snippet in its own session so a hung reproduction (the
	regression we guard against) can be force-killed instead of leaking."""
	proc = subprocess.Popen(
		["bash", "-c", script],
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		start_new_session=True,
	)
	try:
		out, err = proc.communicate(timeout=timeout)
	except subprocess.TimeoutExpired:
		try:
			os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
		except ProcessLookupError:
			pass
		out, err = proc.communicate()
		return subprocess.CompletedProcess(proc.args, -1, out, err)
	return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def test_reaper_function_is_targeted_and_has_proc_fallback() -> None:
	body = _extract_function("_reap_editor_fifo_holders")
	# Primary path uses fuser to signal FIFO holders.
	assert "fuser -k" in body
	# Fallback scans /proc fds when fuser is unavailable or ineffective.
	assert "/proc/" in body and "readlink -f" in body
	# Unexpected /proc paths are ignored before kill is attempted.
	assert 'case "${pid}" in' in body and "*[!0-9]*) continue" in body
	# Never signal the orchestrating shell itself.
	assert '"${pid}" = "$$"' in body


def test_reaper_frees_orphan_held_fifo() -> None:
	"""The real shipped reaper must free a FIFO whose write-end is held open
	by an orphaned process after the 'codex' parent is gone — otherwise the
	drain hangs until the job ceiling. A regression (reaper that misses the
	holder) leaves the reader blocked, which trips this test's timeout."""
	func = _extract_function("_reap_editor_fifo_holders")
	with tempfile.TemporaryDirectory() as td:
		td = os.path.realpath(td)
		fifo = os.path.join(td, "stderr.pipe")
		done = os.path.join(td, "reader.done")
		harness = f"""
set -u
{func}
FIFO={fifo!r}
DONE={done!r}
reader=""
orphan=""
trap 'kill "${{reader:-}}" "${{orphan:-}}" 2>/dev/null; kill -9 "${{reader:-}}" "${{orphan:-}}" 2>/dev/null; true' EXIT
mkfifo -m 600 "$FIFO"

# Heartbeat-reader stand-in: holds the read-end, marks DONE only on EOF.
( cat "$FIFO" >/dev/null 2>&1; : > "$DONE" ) &
reader=$!

# Orphaned codex child stand-in: holds the write-end open and outlives the
# test, so ONLY the reaper (not natural expiry) can release the FIFO.
( exec 9>"$FIFO"; exec sleep 600 ) &
orphan=$!

sleep 1
# Precondition: the orphan keeps the reader blocked (no EOF yet).
[ -e "$DONE" ] && {{ echo "PRECOND_FAIL: reader drained despite live holder"; exit 3; }}
kill -0 "$orphan" 2>/dev/null || {{ echo "PRECOND_FAIL: orphan not alive"; exit 3; }}

# The fix under test.
_reap_editor_fifo_holders "$FIFO" TERM
sleep 1
_reap_editor_fifo_holders "$FIFO" KILL

# With holders reaped these return promptly; a broken reaper hangs here and
# the outer timeout converts it into a failure.
wait "$orphan" 2>/dev/null || true
wait "$reader" 2>/dev/null || true

kill -0 "$orphan" 2>/dev/null && {{ echo "FAIL: orphan survived reap"; exit 4; }}
kill -0 "$reader" 2>/dev/null && {{ echo "FAIL: reader still blocked"; exit 5; }}
echo OK
"""
		res = _run_bash(harness, timeout=45.0)
	assert res.returncode == 0, (
		f"reaper did not free the FIFO (rc={res.returncode}); "
		f"stdout={res.stdout!r} stderr={res.stderr!r}"
	)
	assert "OK" in res.stdout


def test_reaper_proc_fallback_frees_orphan_when_fuser_absent() -> None:
	"""Same guarantee via the /proc fd-scan fallback (fuser hidden from PATH),
	so consumer runners without psmisc are still covered."""
	func = _extract_function("_reap_editor_fifo_holders")
	with tempfile.TemporaryDirectory() as td:
		td = os.path.realpath(td)
		fifo = os.path.join(td, "stderr.pipe")
		shim = os.path.join(td, "bin")
		harness = f"""
set -u
{func}
SHIM={shim!r}
FIFO={fifo!r}
# Build a minimal PATH that has the coreutils we need but NOT fuser, forcing
# the function down its /proc fd-scan fallback (psmisc-less consumer runner).
mkdir -p "$SHIM"
for b in cat sleep mkfifo readlink; do
	src="$(command -v "$b" 2>/dev/null)" || {{ echo "PRECOND_FAIL: missing $b"; exit 3; }}
	ln -sf "$src" "$SHIM/$b"
done
export PATH="$SHIM"
command -v fuser >/dev/null 2>&1 && {{ echo "PRECOND_FAIL: fuser still reachable"; exit 3; }}
reader=""
orphan=""
trap 'kill "${{reader:-}}" "${{orphan:-}}" 2>/dev/null; kill -9 "${{reader:-}}" "${{orphan:-}}" 2>/dev/null; true' EXIT
mkfifo -m 600 "$FIFO"

( cat "$FIFO" >/dev/null 2>&1 ) &
reader=$!
( exec 9>"$FIFO"; exec sleep 600 ) &
orphan=$!
sleep 1
kill -0 "$orphan" 2>/dev/null || {{ echo "PRECOND_FAIL: orphan not alive"; exit 3; }}

_reap_editor_fifo_holders "$FIFO" KILL
wait "$orphan" 2>/dev/null || true
wait "$reader" 2>/dev/null || true
kill -0 "$orphan" 2>/dev/null && {{ echo "FAIL: orphan survived /proc reap"; exit 4; }}
echo OK
"""
		res = _run_bash(harness, timeout=45.0)
	assert res.returncode == 0, (
		f"/proc fallback did not free the FIFO (rc={res.returncode}); "
		f"stdout={res.stdout!r} stderr={res.stderr!r}"
	)
	assert "OK" in res.stdout


def test_reaper_proc_fallback_runs_when_fuser_present_but_fails() -> None:
	"""If `fuser` exists but cannot reap the FIFO holders, the real shipped
	reaper must still fall through to the /proc sweep instead of becoming a
	silent no-op."""
	func = _extract_function("_reap_editor_fifo_holders")
	with tempfile.TemporaryDirectory() as td:
		td = os.path.realpath(td)
		fifo = os.path.join(td, "stderr.pipe")
		shim = os.path.join(td, "bin")
		harness = f"""
set -u
{func}
SHIM={shim!r}
FIFO={fifo!r}
mkdir -p "$SHIM"
for b in cat sleep mkfifo readlink; do
	src="$(command -v "$b" 2>/dev/null)" || {{ echo "PRECOND_FAIL: missing $b"; exit 3; }}
	ln -sf "$src" "$SHIM/$b"
done
cat > "$SHIM/fuser" <<'SH'
#!/bin/sh
exit 1
SH
chmod +x "$SHIM/fuser"
export PATH="$SHIM"
command -v fuser >/dev/null 2>&1 || {{ echo "PRECOND_FAIL: fuser missing"; exit 3; }}
reader=""
orphan=""
trap 'kill "${{reader:-}}" "${{orphan:-}}" 2>/dev/null; kill -9 "${{reader:-}}" "${{orphan:-}}" 2>/dev/null; true' EXIT
mkfifo -m 600 "$FIFO"

( cat "$FIFO" >/dev/null 2>&1 ) &
reader=$!
( exec 9>"$FIFO"; exec sleep 600 ) &
orphan=$!
sleep 1
kill -0 "$orphan" 2>/dev/null || {{ echo "PRECOND_FAIL: orphan not alive"; exit 3; }}

_reap_editor_fifo_holders "$FIFO" KILL
wait "$orphan" 2>/dev/null || true
wait "$reader" 2>/dev/null || true
kill -0 "$orphan" 2>/dev/null && {{ echo "FAIL: orphan survived failed-fuser fallback"; exit 4; }}
echo OK
"""
		res = _run_bash(harness, timeout=45.0)
	assert res.returncode == 0, (
		f"failed-fuser fallback did not free the FIFO (rc={res.returncode}); "
		f"stdout={res.stdout!r} stderr={res.stderr!r}"
	)
	assert "OK" in res.stdout


def test_editor_loop_bounds_the_drain_and_wires_the_reaper() -> None:
	text = APPLY_FIXES_SCRIPT.read_text(encoding="utf-8")
	# Grace bound is configurable with a safe default.
	assert 'EDITOR_DRAIN_GRACE_SECS="${EDITOR_DRAIN_GRACE_SECS:-60}"' in text
	assert "''|*[!0-9]*|0|0[0-9]*)" in text
	assert 'Invalid EDITOR_DRAIN_GRACE_SECS=' in text
	# Reader signals a clean drain via the done-marker.
	assert '_hb_reader_done="${_hb_tmpdir}/reader.done"' in text
	assert ': > "${_hb_reader_done}"' in text
	# Drain is deadline-bounded and polls the done-marker instead of an
	# unbounded `wait`.
	assert '_skip_hb_reader_wait=false' in text
	assert "_drain_deadline=$(( $(date +%s) + EDITOR_DRAIN_GRACE_SECS ))" in text
	assert 'while [ ! -e "${_hb_reader_done}" ]; do' in text
	# On timeout it reaps FIFO holders (TERM then KILL).
	assert '_reap_editor_fifo_holders "${_hb_fifo}" TERM' in text
	assert '_reap_editor_fifo_holders "${_hb_fifo}" KILL' in text
	assert 'kill -KILL "${_hb_reader_pid}" 2>/dev/null || true' in text
	assert 'skipping blocking wait' in text


def test_review_apply_fixes_script_syntax_is_valid() -> None:
	res = subprocess.run(
		["bash", "-n", str(APPLY_FIXES_SCRIPT)],
		capture_output=True,
		text=True,
	)
	assert res.returncode == 0, res.stderr


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except AssertionError as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
		except Exception as e:  # noqa: BLE001 - surface any unexpected error
			print(f"  ERROR {name}: {type(e).__name__}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())

#!/usr/bin/env python3
"""Per-cycle timing summary of the orchestrator poller.

analysis/workflow-optimization-2026-10-08.md (Speed #2) asks for a per-cycle
split of wall time before any cadence change. scripts/orchestrate_poll_process.sh
prints exactly one ``ORCH_POLL_CYCLE_SUMMARY`` line from its EXIT trap and one
``ORCH_POLL_PROJECT_TIMING`` line per tracking issue processed. The trap must
keep the script's exit status, add no gh/API calls and fail open.

These cases run in CI through tests/test_alert_levels_human_only.py, which
collects them (ci.yml was outside this issue's file grant).
"""

from __future__ import annotations

import functools
import re
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"

BLOCK_BEGIN = "# --- ORCH_POLL_CYCLE_SUMMARY helpers (begin) ---"
BLOCK_END = "# --- ORCH_POLL_CYCLE_SUMMARY helpers (end) ---"
SLEEP_CALL = '"${_ORCH_POLL_CYCLE_SLEEP_CMD:-sleep}"'
CHAINED_GATE_TRAP = (
	"trap '_orch_poll_cycle_exit_rc=$?; rm -f \"${_gate_fp_file:-}\" \"${_gate_log_file:-}\" 2>/dev/null || true; "
	"_orch_poll_cycle_on_exit \"${_orch_poll_cycle_exit_rc}\" || true' EXIT"
)
SUMMARY_RE = re.compile(
	r"^ORCH_POLL_CYCLE_SUMMARY elapsed_ms=(\d+|unknown) projects_seen=(\d+|unknown) "
	r"projects_processed=(\d+|unknown) sleep_ms=(\d+|unknown) exit_code=(\d+|unknown) "
	r"terminal_reason=(completed|no_projects|error)$"
)


@functools.lru_cache(maxsize=1)
def _script() -> str:
	return POLLER_SCRIPT.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def _helper_block() -> str:
	script = _script()
	start = script.index(BLOCK_BEGIN)
	end = script.index(BLOCK_END, start) + len(BLOCK_END)
	return script[start:end]


def _run(body: str, *, tmpdir: str | None = None) -> tuple[int, list[str], list[str]]:
	"""Run the helper block followed by ``body`` under ``set -euo pipefail``.

	Returns (exit status, summary lines, project timing lines)."""
	with tempfile.TemporaryDirectory() as tmp:
		harness = Path(tmp) / "harness.sh"
		harness.write_text("set -euo pipefail\n" + _helper_block() + "\n" + body + "\n", encoding="utf-8")
		env = {"PATH": "/usr/bin:/bin", "TMPDIR": tmpdir if tmpdir is not None else tmp}
		result = subprocess.run(["bash", str(harness)], capture_output=True, text=True, check=False, env=env)
		lines = result.stdout.splitlines()
		summaries = [line for line in lines if line.startswith("ORCH_POLL_CYCLE_SUMMARY")]
		projects = [line for line in lines if line.startswith("ORCH_POLL_PROJECT_TIMING")]
		if tmpdir is None:
			leftovers = sorted(path.name for path in Path(tmp).glob("orch_poll_cycle_sleep.*"))
			assert leftovers == [], leftovers
		return result.returncode, summaries, projects


def _fields(summary: str) -> tuple[str, ...]:
	match = SUMMARY_RE.match(summary)
	assert match, summary
	return match.groups()


# ---------------------------------------------------------------------------
# (a) Static contract
# ---------------------------------------------------------------------------


def test_cycle_summary_static_trap_and_format():
	script = _script()
	block = _helper_block()
	assert script.index(BLOCK_BEGIN) > script.index("set -euo pipefail\n")
	first_exit = re.search(r"^\s*exit\b", script, re.M)
	assert first_exit and script.index(BLOCK_END) < first_exit.start()
	assert "trap '_orch_poll_cycle_on_exit \"$?\" || true' EXIT" in block
	assert 'if [ "${BASH_SOURCE[0]:-}" = "${0:-}" ]; then\n\t_ORCH_POLL_CYCLE_ENABLED="true"' in block
	assert block.rstrip().endswith("_orch_poll_cycle_install_exit_trap || true\n" + BLOCK_END)
	assert (
		'echo "ORCH_POLL_CYCLE_SUMMARY elapsed_ms=${_orch_poll_cycle_exit_elapsed} projects_seen=${_orch_poll_cycle_exit_seen} '
		"projects_processed=${_orch_poll_cycle_exit_processed} sleep_ms=${_orch_poll_cycle_exit_sleep} "
		'exit_code=${_orch_poll_cycle_exit_code} terminal_reason=${_orch_poll_cycle_exit_reason}"'
	) in block
	assert 'echo "ORCH_POLL_PROJECT_TIMING tracking_issue=${_orch_poll_cycle_flush_num} elapsed_ms=${_orch_poll_cycle_flush_elapsed}"' in block
	assert "date +%s%3N" in block and "date +%s 2>/dev/null" in block
	# The handler never calls exit, so bash keeps the original status.
	assert not re.search(r"^\s*exit\b", block, re.M)


def test_cycle_summary_static_loop_wiring():
	script = _script()
	count_line = 'COUNT="$(echo "${TRACKING_ISSUES}" | jq \'length\')"\n'
	seen_line = 'if [[ "${COUNT}" =~ ^[0-9]+$ ]]; then _ORCH_POLL_CYCLE_PROJECTS_SEEN="${COUNT}"; _ORCH_POLL_CYCLE_PROJECTS_PROCESSED=0; fi || true\n'
	assert count_line + seen_line in script
	loop = "for ((tidx=0; tidx<COUNT; tidx++)); do\n"
	begin = '  TRACKING_NUM="$(echo "${TRACKING_ISSUES}" | jq -r ".[${tidx}].number")"\n  _orch_poll_cycle_project_begin "${TRACKING_NUM}" || true\n'
	assert script.index(loop) < script.index(begin) < script.index(loop) + len(loop) + len(begin)
	assert script.count("_orch_poll_cycle_project_begin \"${TRACKING_NUM}\"") == 1
	flush = "\ndone\n_orch_poll_cycle_project_flush || true\n"
	assert script.count(flush) == 1
	assert script.index(begin) < script.index(flush)
	# The loop end is followed by the standalone stall recovery pass.
	assert script.index("run_standalone_stall_recovery\n", script.index(flush)) - script.index(flush) < 200


def test_cycle_summary_static_gate_trap_is_chained():
	script = _script()
	assert CHAINED_GATE_TRAP in script
	assert script.index(CHAINED_GATE_TRAP) < script.index('_gate_fp_file="$(mktemp')
	assert "            trap - EXIT\n            _orch_poll_cycle_install_exit_trap || true\n" in script
	# The gate trap is the only other EXIT trap in the poller.
	assert len(re.findall(r"^\s*trap '[^\n]*' EXIT$", script, re.M)) == 2


def test_cycle_summary_static_sleep_sites_keep_plain_sleep_fallback():
	script = _script()
	# Every sleep call outside the wrapper's own passthrough goes through the
	# ledger command (no exact site count, so later sleep sites need no edit).
	assert script.count(SLEEP_CALL) >= 2
	bare_sleep = re.compile(r'(^|[;&|(]|\bthen|\bdo|\belse)\s*sleep\b')
	for line in script.splitlines():
		if line.lstrip().startswith("#") or line.strip() == 'sleep "$@"':
			continue
		assert not bare_sleep.search(line), line
	for line in script.splitlines():
		if SLEEP_CALL in line and not line.lstrip().startswith("#"):
			assert re.search(re.escape(SLEEP_CALL) + r' (\d+|\$\(\( [^)]*\)\)|"\$\{_std_backoff_sleeps\[\$\{_std_attempt\}\]\}")', line), line


# ---------------------------------------------------------------------------
# (b) Harness: exit status is preserved and exactly one line is printed
# ---------------------------------------------------------------------------


def test_cycle_summary_preserves_explicit_exit_status():
	rc, summaries, _ = _run("exit 7")
	assert rc == 7
	assert len(summaries) == 1
	assert _fields(summaries[0])[4:] == ("7", "error")


def test_cycle_summary_preserves_errexit_failure_status():
	rc, summaries, _ = _run("false\necho unreachable")
	assert rc == 1
	assert len(summaries) == 1
	assert _fields(summaries[0])[4:] == ("1", "error")


def test_cycle_summary_elapsed_ignores_a_date_clock_that_goes_backwards(tmp_path):
	# ubuntu-26.04's `date +%s%3N` gave readings that went backwards within a
	# run, so elapsed_ms printed negative and failed the summary regex. The
	# clock reads bash's EPOCHREALTIME first, so a bad `date` cannot do that.
	harness = tmp_path / "harness.sh"
	harness.write_text(
		"set -euo pipefail\n"
		# A file counter, because the clock calls `date` inside $(...) subshells.
		f"_fake_date_count_file='{tmp_path / 'date_calls'}'\n"
		"date() { echo x >> \"${_fake_date_count_file}\"; "
		"if [ \"$(wc -l < \"${_fake_date_count_file}\")\" -eq 1 ]; then echo 1700000000900; else echo 1700000000100; fi; }\n"
		+ _helper_block()
		+ "\nexit 0\n",
		encoding="utf-8",
	)
	result = subprocess.run(
		["bash", str(harness)],
		capture_output=True,
		text=True,
		check=False,
		env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)},
	)
	assert result.returncode == 0, result.stderr
	summaries = [line for line in result.stdout.splitlines() if line.startswith("ORCH_POLL_CYCLE_SUMMARY")]
	assert len(summaries) == 1, result.stdout
	assert _fields(summaries[0])[0].isdigit(), summaries[0]


def test_cycle_summary_reports_unknown_elapsed_when_start_is_in_the_future():
	# EPOCHREALTIME is wall-clock time; a backward clock step puts the start
	# after the end reading. The summary must say unknown, never a negative.
	rc, summaries, _ = _run("_ORCH_POLL_CYCLE_START_MS=99999999999999\nexit 0")
	assert rc == 0
	assert len(summaries) == 1
	assert _fields(summaries[0])[0] == "unknown", summaries[0]


def test_project_timing_reports_unknown_elapsed_when_start_is_in_the_future():
	body = (
		"_ORCH_POLL_CYCLE_PROJECTS_SEEN=1\n_ORCH_POLL_CYCLE_PROJECTS_PROCESSED=0\n"
		"_orch_poll_cycle_project_begin 42 || true\n"
		"_ORCH_POLL_CYCLE_PROJECT_START_MS=99999999999999\n"
		"_orch_poll_cycle_project_flush || true\n"
	)
	rc, summaries, projects = _run(body)
	assert rc == 0
	assert projects == ["ORCH_POLL_PROJECT_TIMING tracking_issue=42 elapsed_ms=unknown"], projects
	# The cycle's own elapsed value is independent of the project's.
	assert len(summaries) == 1
	assert _fields(summaries[0])[0].isdigit(), summaries[0]


def test_cycle_summary_sweep_only_exit_reports_unknown_projects():
	rc, summaries, projects = _run("exit 0")
	assert rc == 0 and projects == []
	elapsed, seen, processed, _, code, reason = _fields(summaries[0])
	assert elapsed.isdigit() and (seen, processed, code, reason) == ("unknown", "unknown", "0", "completed")


def test_cycle_summary_no_projects():
	rc, summaries, projects = _run('_ORCH_POLL_CYCLE_PROJECTS_SEEN=0\n_ORCH_POLL_CYCLE_PROJECTS_PROCESSED=0\n')
	assert rc == 0 and projects == []
	assert len(summaries) == 1
	assert _fields(summaries[0])[1:] == ("0", "0", "0", "0", "no_projects")


def test_cycle_summary_counts_projects_and_prints_project_timing():
	body = (
		"_ORCH_POLL_CYCLE_PROJECTS_SEEN=3\n_ORCH_POLL_CYCLE_PROJECTS_PROCESSED=0\n"
		"for n in 11 '12; rm -rf /'; do\n"
		'  _orch_poll_cycle_project_begin "${n}" || true\n'
		"  continue\n"
		"done\n"
		"_orch_poll_cycle_project_flush || true\n"
	)
	rc, summaries, projects = _run(body)
	assert rc == 0
	assert len(summaries) == 1
	assert _fields(summaries[0])[1:3] == ("3", "2")
	assert _fields(summaries[0])[5] == "completed"
	assert len(projects) == 2
	assert re.fullmatch(r"ORCH_POLL_PROJECT_TIMING tracking_issue=11 elapsed_ms=\d+", projects[0]), projects
	# A non-numeric tracking number never reaches the log line.
	assert re.fullmatch(r"ORCH_POLL_PROJECT_TIMING tracking_issue=unknown elapsed_ms=\d+", projects[1]), projects


def test_cycle_summary_flushes_pending_project_on_mid_project_exit():
	body = "_ORCH_POLL_CYCLE_PROJECTS_SEEN=1\n_ORCH_POLL_CYCLE_PROJECTS_PROCESSED=0\n_orch_poll_cycle_project_begin 42 || true\nexit 5\n"
	rc, summaries, projects = _run(body)
	assert rc == 5
	assert len(projects) == 1 and projects[0].startswith("ORCH_POLL_PROJECT_TIMING tracking_issue=42 elapsed_ms=")
	assert _fields(summaries[0])[1:] == ("1", "1", "0", "5", "error")


def test_cycle_summary_sums_sleep_ledger_including_subshells():
	body = (
		'"${_ORCH_POLL_CYCLE_SLEEP_CMD:-sleep}" 0\n'
		'_out="$("${_ORCH_POLL_CYCLE_SLEEP_CMD:-sleep}" 0; echo ok)"\n'
		'[ "${_out}" = ok ]\n'
		"_orch_poll_cycle_sleep 0 && true\n"
	)
	rc, summaries, _ = _run(body)
	assert rc == 0
	assert _fields(summaries[0])[3] == "0"
	# Durations are recorded in ms; the sleep runs unchanged.
	with tempfile.TemporaryDirectory() as tmp:
		harness = Path(tmp) / "h.sh"
		harness.write_text(
			"set -euo pipefail\n" + _helper_block() + "\n"
			'sleep() { echo "SLEPT $*"; }\n'
			'"${_ORCH_POLL_CYCLE_SLEEP_CMD:-sleep}" 3\n"${_ORCH_POLL_CYCLE_SLEEP_CMD:-sleep}" $(( 8 * 2 ))\n',
			encoding="utf-8",
		)
		result = subprocess.run(["bash", str(harness)], capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin", "TMPDIR": tmp})
		assert result.returncode == 0, result.stderr
		assert "SLEPT 3\nSLEPT 16\n" in result.stdout
		assert "sleep_ms=19000 " in result.stdout
	# A non-integer duration makes the total unknown rather than wrong.
	rc, summaries, _ = _run('"${_ORCH_POLL_CYCLE_SLEEP_CMD:-sleep}" 0.01\n')
	assert rc == 0 and _fields(summaries[0])[3] == "unknown"


def test_cycle_summary_fails_open_without_ledger():
	rc, summaries, _ = _run('"${_ORCH_POLL_CYCLE_SLEEP_CMD:-sleep}" 0\nexit 4\n', tmpdir="/nonexistent/orch_poll_cycle_missing")
	assert rc == 4
	assert len(summaries) == 1
	assert _fields(summaries[0])[3:] == ("unknown", "4", "error")


def test_cycle_summary_sourced_poller_stays_silent():
	"""Harnesses that `source` the poller as a library parse its stdout; a
	sourced copy installs no trap, makes no ledger and prints nothing."""
	with tempfile.TemporaryDirectory() as tmp:
		lib = Path(tmp) / "lib.sh"
		lib.write_text(_helper_block() + "\n", encoding="utf-8")
		body = (
			"set -euo pipefail\n"
			f"source {str(lib)!r}\n"
			"_ORCH_POLL_CYCLE_PROJECTS_SEEN=1\n_ORCH_POLL_CYCLE_PROJECTS_PROCESSED=0\n"
			"_orch_poll_cycle_project_begin 9 || true\n_orch_poll_cycle_project_flush || true\n"
			'"${_ORCH_POLL_CYCLE_SLEEP_CMD:-sleep}" 0\n'
			"_orch_poll_cycle_install_exit_trap || true\n"
			"echo payload\n"
			"exit 6\n"
		)
		result = subprocess.run(["bash", "-c", body], capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin", "TMPDIR": tmp})
		assert result.returncode == 6
		assert result.stdout == "payload\n"
		assert sorted(path.name for path in Path(tmp).iterdir()) == ["lib.sh"]


def test_cycle_summary_subshells_do_not_emit():
	body = '_x="$(exit 0)"\n( exit 0 )\n( true ) || true\n_y="$(echo hi)"\n'
	rc, summaries, _ = _run(body)
	assert rc == 0
	assert len(summaries) == 1


def test_cycle_summary_chained_gate_trap_cleans_up_and_keeps_status():
	with tempfile.TemporaryDirectory() as tmp:
		body = (
			"_gate_fp_file=\"\"\n_gate_log_file=\"\"\n"
			+ CHAINED_GATE_TRAP + "\n"
			'_gate_fp_file="$(mktemp "${TMPDIR:-/tmp}/wave_dispatch_fp.XXXXXX")"\n'
			"exit 3\n"
		)
		rc, summaries, _ = _run(body, tmpdir=tmp)
		assert rc == 3
		assert len(summaries) == 1 and _fields(summaries[0])[4:] == ("3", "error")
		assert list(Path(tmp).iterdir()) == []
	# After `trap - EXIT` the summary trap is reinstalled.
	body = CHAINED_GATE_TRAP + "\ntrap - EXIT\n_orch_poll_cycle_install_exit_trap || true\nexit 0\n"
	rc, summaries, _ = _run(body)
	assert rc == 0 and len(summaries) == 1


# ---------------------------------------------------------------------------
# (c) No new API calls; (d) the workflow grep target is intact
# ---------------------------------------------------------------------------


def test_cycle_summary_adds_no_api_calls():
	# Comment lines are prose (the header names gh_retry to say it is not counted).
	block = "\n".join(line for line in _helper_block().splitlines() if not line.lstrip().startswith("#"))
	assert not re.search(r"(^|[\s;(&|$])(gh|gh_retry|_safe_gh_jq|curl)\s", block, re.M)
	added = [line for line in _script().splitlines() if "_orch_poll_cycle" in line or "_ORCH_POLL_CYCLE" in line]
	assert added
	for line in added:
		if line.lstrip().startswith("#"):
			continue
		assert not re.search(r"\b(gh|gh_retry|_safe_gh_jq|curl)\s", line), line


def test_cycle_summary_keeps_replay_failed_reclarify_commands_definition():
	script = _script()
	assert re.search(r"^replay_failed_reclarify_commands\(\)", script, re.M)
	assert script.index(BLOCK_END) < script.index("\nreplay_failed_reclarify_commands()")

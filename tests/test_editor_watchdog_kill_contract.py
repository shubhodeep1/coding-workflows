#!/usr/bin/env python3
"""Contract test for reaping the editor / reviewer watchdog subshell.

Run 34397466777 (PR #4071): the editor watchdog killed the editor at the
55-minute wall cap on attempt 1, then the whole `review_apply_fixes.sh`
script exited 1 within milliseconds of restoring editor isolation, with no
retry attempt, no fallback summary and no archived editor stderr. The
downstream step classified the run as "editor produced no output" and the
review was deferred to the stall poller.

Cause: the watchdog subshell exits on its own (143 / 142 / 144) after it kills
the editor, so by the time the parent reaps the editor process the watchdog
may already be gone. The parent then ran

    kill "${wd_pid}" 2>/dev/null; wait "${wd_pid}" 2>/dev/null || true

and the unguarded `kill` returned 1 (ESRCH), which under `set -euo pipefail`
aborted the script. The same idiom exists at the reviewer watchdog kill
sites in `review_run_reviewers.sh`.

This test pins two things:
  1. every `kill "${wd_pid}"` in the two scripts carries `|| true`;
  2. the exact reap line from `review_apply_fixes.sh` survives a watchdog
     that has already exited, under the same `set -euo pipefail` the
     script runs with (and the pre-fix idiom does not, so the harness is
     known to detect the defect).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
REVIEW_RUN_REVIEWERS = REPO_ROOT / "scripts" / "review_run_reviewers.sh"

WD_KILL_RE = re.compile(r'kill "\$\{wd_pid\}" 2>/dev/null')
GUARDED_WD_KILL_RE = re.compile(
	r'kill "\$\{wd_pid\}" 2>/dev/null \|\| true; wait "\$\{wd_pid\}" 2>/dev/null \|\| true'
)
UNGUARDED_WD_KILL_LINE = 'kill "${wd_pid}" 2>/dev/null; wait "${wd_pid}" 2>/dev/null || true'
MARKER = "WATCHDOG_REAP_SURVIVED"


def _wd_kill_lines(script: Path) -> list[str]:
	lines = [line.strip() for line in script.read_text(encoding="utf-8").splitlines()]
	return [line for line in lines if WD_KILL_RE.search(line)]


def _run_reap_harness(reap_line: str) -> subprocess.CompletedProcess[str]:
	# Mirror the script: a backgrounded watchdog subshell that exits on its
	# own with 143 (wall-time kill), reaped only after it is already gone.
	harness = "\n".join(
		[
			"set -euo pipefail",
			"( exit 143 ) &",
			"wd_pid=$!",
			'wait "${wd_pid}" || true',
			reap_line,
			f'echo "{MARKER}"',
		]
	)
	return subprocess.run(
		["bash", "-c", harness],
		capture_output=True,
		text=True,
		timeout=30,
		check=False,
	)


def test_watchdog_kill_sites_are_guarded_against_self_exited_watchdog() -> None:
	for script, expected_sites in ((REVIEW_APPLY_FIXES, 1), (REVIEW_RUN_REVIEWERS, 2)):
		sites = _wd_kill_lines(script)
		assert len(sites) == expected_sites, (
			f"{script.name}: expected {expected_sites} watchdog kill site(s), found {len(sites)}: {sites}"
		)
		for line in sites:
			assert GUARDED_WD_KILL_RE.search(line), (
				f"{script.name}: watchdog kill must tolerate an already-exited watchdog "
				f"(`|| true` before `wait`), got: {line}"
			)


def test_editor_reap_line_survives_already_exited_watchdog() -> None:
	sites = _wd_kill_lines(REVIEW_APPLY_FIXES)
	assert len(sites) == 1, sites
	result = _run_reap_harness(sites[0])
	assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
	assert MARKER in result.stdout, (result.stdout, result.stderr)


def test_harness_detects_unguarded_reap_line() -> None:
	# Self-check: the pre-fix idiom must abort the harness the same way it
	# aborted review_apply_fixes.sh in run 34397466777.
	result = _run_reap_harness(UNGUARDED_WD_KILL_LINE)
	assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
	assert MARKER not in result.stdout, result.stdout

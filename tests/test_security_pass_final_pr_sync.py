#!/usr/bin/env python3
"""A final PR that main makes unmergeable is healed during the security pass.

The tick-level main -> integration sync in scripts/orchestrate_poll_process.sh
skips security-pass and security-pass-fixing, because the pass is bound to
the integration head. The finalizer heals an unmergeable final PR only after
the pass, so project #6664's final PR #6667 sat dirty on an agents.md
conflict from 23:12 on 2026-10-09 through its security pass. Since operator
decision Q41: A, the poller syncs during the pass only when the recorded final
PR is open and unmergeable.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _poller_text() -> str:
	return POLLER.read_text(encoding="utf-8")


def _sync_helper_text() -> str:
	match = re.search(r"^security_pass_sync_final_pr_if_unmergeable\(\) \{\n.*?^\}\n", _poller_text(), re.S | re.M)
	assert match
	return match.group(0)


def _jq_field_text() -> str:
	match = re.search(r"^_jq_field\(\)\n\{\n.*?^\}\n", _poller_text(), re.S | re.M)
	assert match
	return match.group(0)


def _run(state: dict, pr_json: dict | None, *, sync_rc: int = 0) -> subprocess.CompletedProcess:
	"""Run the helper with the real _jq_field and stub PR read / sync functions."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		state_file = tmp / "state.json"
		state_file.write_text(json.dumps(state), encoding="utf-8")
		calls = tmp / "calls"
		pr_file = tmp / "pr.json"
		pr_file.write_text(json.dumps(pr_json) if pr_json is not None else "{}", encoding="utf-8")
		script = "\n".join([
			"set -euo pipefail",
			_jq_field_text(),
			f"STATE_FILE={str(state_file)!r}",
			"TRACKING_NUM=6664",
			f'_fetch_pr_json() {{ echo "fetch $1" >> {str(calls)!r}; cat {str(pr_file)!r}; }}',
			f'sync_default_into_integration_branch() {{ echo "sync $1 $2" >> {str(calls)!r}; return {sync_rc}; }}',
			_sync_helper_text(),
			"rc=0; security_pass_sync_final_pr_if_unmergeable orchestrator/project-6664 main || rc=$?",
			f'echo "rc=${{rc}}"; cat {str(calls)!r} 2>/dev/null || true',
		])
		return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def test_unmergeable_final_pr_syncs_main_into_the_integration_branch() -> None:
	result = _run({"final_merge_pr": 6667}, {"state": "open", "mergeable": False, "mergeable_state": "dirty"})
	assert result.returncode == 0, result.stderr
	assert "SECURITY_PASS_FINAL_PR_SYNC tracking_issue=6664 pr=6667 outcome=sync reason=final_pr_unmergeable" in result.stdout
	assert "fetch 6667" in result.stdout
	assert "sync orchestrator/project-6664 main" in result.stdout
	assert "rc=0" in result.stdout


def test_sync_failure_is_returned_so_the_tick_stops_for_the_project() -> None:
	result = _run({"final_merge_pr": 6667}, {"state": "open", "mergeable": False}, sync_rc=1)
	assert result.returncode == 0, result.stderr
	assert "rc=1" in result.stdout


def test_mergeable_or_computing_pr_does_nothing() -> None:
	for mergeable, reason in ((True, "mergeable_true"), (None, "mergeable_unknown")):
		result = _run({"final_merge_pr": 6667}, {"state": "open", "mergeable": mergeable})
		assert result.returncode == 0, result.stderr
		assert f"pr=6667 outcome=skip reason={reason}" in result.stdout, result.stdout
		assert "sync " not in result.stdout
		assert "rc=0" in result.stdout


def test_closed_unreadable_or_missing_final_pr_does_nothing() -> None:
	cases = (
		({"final_merge_pr": 6667}, {"state": "closed", "mergeable": False}, "pr=6667 outcome=skip reason=pr_closed"),
		({"final_merge_pr": 6667}, None, "pr=6667 outcome=skip reason=pr_unreadable"),
		({}, {"state": "open", "mergeable": False}, "pr=none outcome=skip reason=no_final_pr"),
		({"final_merge_pr": "6667; rm -rf /"}, {"state": "open", "mergeable": False}, "pr=none outcome=skip reason=no_final_pr"),
	)
	for state, pr_json, expected in cases:
		result = _run(state, pr_json)
		assert result.returncode == 0, result.stderr
		assert expected in result.stdout, (state, result.stdout)
		assert "sync " not in result.stdout
		assert "rc=0" in result.stdout


def test_call_site_runs_only_in_security_pass_states_before_the_pass_logic() -> None:
	text = _poller_text()
	block_start = text.index('\tif [ "${PROJECT_STATUS}" = "security-pass" ] || [ "${PROJECT_STATUS}" = "security-pass-fixing" ]; then\n\t\tDEFAULT_BRANCH_TRACKING=')
	block = text[block_start:text.index("\n  # /security-pass-waive", block_start)]
	assert 'security_pass_sync_final_pr_if_unmergeable "${INTEGRATION_BRANCH_TRACKING}" "${DEFAULT_BRANCH_TRACKING}"' in block
	assert 'PROJECT_STATUS="$(jq -r \'.status\' "${STATE_FILE}")"' in block
	# Before the security-pass handlers that end the tick for the project.
	assert block_start < text.index('echo "Security-pass fix issue #${SECURITY_FIX_ISSUE} remains in progress."')
	# The tick-level sync still skips both states, so main moving without a
	# conflict does not move the audited head.
	tick_sync = text[text.index('    && [ "${PROJECT_STATUS}" != "security-pass" ] \\'):]
	assert tick_sync.index('    && [ "${PROJECT_STATUS}" != "security-pass-fixing" ] \\') < tick_sync.index('sync_default_into_integration_branch "${INTEGRATION_BRANCH_TRACKING}"')


# --- Integration sync throttle (run-volume plan item 3) -----------------------
# docs/plans/reduce-actions-run-volume-plan.md: the per-tick main -> integration
# sync ran on every poll tick, and each push started two full CI runs.
# _integration_sync_throttle_reason decides whether this tick syncs.


def _throttle_text() -> str:
	match = re.search(r"^_integration_sync_throttle_reason\(\) \{\n.*?^\}\n", _poller_text(), re.S | re.M)
	assert match
	return match.group(0)


def _sync_function_text() -> str:
	match = re.search(r"^sync_default_into_integration_branch\(\) \{\n.*?^\}\n", _poller_text(), re.S | re.M)
	assert match
	return match.group(0)


def _iso_ago(seconds: int) -> str:
	import datetime
	stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
	return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _throttle(state: dict, pr_json: dict | None = None, *, force: str = "", interval: str | None = None) -> subprocess.CompletedProcess:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		state_file = tmp / "state.json"
		state_file.write_text(json.dumps(state), encoding="utf-8")
		calls = tmp / "calls"
		pr_file = tmp / "pr.json"
		pr_file.write_text(json.dumps(pr_json) if pr_json is not None else "{}", encoding="utf-8")
		lines = [
			"set -euo pipefail",
			_jq_field_text(),
			f"STATE_FILE={str(state_file)!r}",
			f'_fetch_pr_json() {{ echo "fetch $1" >> {str(calls)!r}; cat {str(pr_file)!r}; }}',
			_throttle_text(),
		]
		if interval is not None:
			lines.append(f"INTEGRATION_SYNC_MIN_INTERVAL_SECS={interval!r}")
		lines += [
			f'rc=0; reason="$(_integration_sync_throttle_reason {force!r})" || rc=$?',
			'echo "reason=${reason} rc=${rc}"',
			f'cat {str(calls)!r} 2>/dev/null || true',
		]
		return subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True, timeout=30)


def _recent_state(**extra) -> dict:
	state = {
		"integration_sync_status": "clean",
		"sync": {"status": "active", "last_default_sync_at": _iso_ago(600)},
		"current_wave": 1,
		"waves": [{"issues": [{"id": "a", "status": "in_progress"}, {"id": "b", "status": "merged"}]}],
	}
	state.update(extra)
	return state


def test_throttle_skips_a_recent_clean_sync_mid_wave() -> None:
	result = _throttle(_recent_state())
	assert result.returncode == 0, result.stderr
	assert "reason=within_interval rc=1" in result.stdout
	assert "fetch" not in result.stdout


def test_throttle_syncs_when_needed_or_overdue() -> None:
	cases = [
		("never_synced", _recent_state(sync={"status": "active"}), None),
		("interval_elapsed", _recent_state(sync={"status": "active", "last_default_sync_at": _iso_ago(90000)}), None),
		("not_clean", _recent_state(integration_sync_status="conflict"), None),
		("not_clean", _recent_state(sync={"status": "active", "last_default_sync_at": _iso_ago(60), "last_conflict_fingerprint": "abc"}), None),
		("final_pr_unmergeable", _recent_state(final_merge_pr=7), {"state": "open", "mergeable": False}),
		("final_pr_unreadable", _recent_state(final_merge_pr=7), {}),
		("clock_unreadable", _recent_state(sync={"status": "active", "last_default_sync_at": "not-a-date"}), None),
	]
	for reason, state, pr_json in cases:
		result = _throttle(state, pr_json)
		assert result.returncode == 0, result.stderr
		assert f"reason={reason} rc=0" in result.stdout, (reason, result.stdout)


def test_throttle_skips_with_a_mergeable_or_computing_final_pr() -> None:
	for pr_json in ({"state": "open", "mergeable": True}, {"state": "open", "mergeable": None}, {"state": "closed", "mergeable": False}):
		result = _throttle(_recent_state(final_merge_pr=7), pr_json)
		assert "reason=within_interval rc=1" in result.stdout, (pr_json, result.stdout)
		assert "fetch 7" in result.stdout


def test_throttle_honours_force_and_the_interval_switch() -> None:
	assert "reason=forced rc=0" in _throttle(_recent_state(), force="force").stdout
	assert "reason=interval_disabled rc=0" in _throttle(_recent_state(), interval="0").stdout
	# A short custom interval elapses after the recorded 600 s.
	assert "reason=interval_elapsed rc=0" in _throttle(_recent_state(), interval="300").stdout
	bad = _throttle(_recent_state(), interval="soon")
	assert "reason=within_interval rc=1" in bad.stdout
	assert "INTEGRATION_SYNC_MIN_INTERVAL_SECS='soon' is not a non-negative integer; using 86400." in bad.stderr


def test_throttle_does_not_sync_every_tick_while_a_complete_wave_lingers() -> None:
	"""A complete wave can wait many ticks (judge, persist wave-check hold).

	The boundary syncs are forced where they are decided (next-wave dispatch,
	project completion), so the throttle itself ignores wave status: syncing
	on every lingering tick would restart the integration PR's checks.
	"""
	for statuses in (["merged", "skipped"], ["merged", "closed"], []):
		state = _recent_state(waves=[{"issues": [{"id": str(i), "status": st} for i, st in enumerate(statuses)]}])
		result = _throttle(state)
		assert "reason=within_interval rc=1" in result.stdout, (statuses, result.stdout)


def test_wave_advance_and_project_completion_use_the_boundary_helper() -> None:
	text = _poller_text()
	advance_at = text.index('echo "Advancing to wave ${NEXT_WAVE}..."')
	advance_block = text[text.rindex("Run-volume plan item 3", 0, advance_at): advance_at]
	assert '_integration_sync_at_boundary "${INTEGRATION_BRANCH_TRACKING}" "${DEFAULT_BRANCH_TRACKING}" "wave-${NEXT_WAVE}"; then\n            continue' in advance_block
	complete_at = text.index("    complete)\n      FINAL_INTEGRATION_BRANCH=")
	complete_block = text[complete_at: text.index("ensure_security_pass_before_completion", complete_at)]
	assert '_integration_sync_at_boundary "${FINAL_INTEGRATION_BRANCH}" "${FINAL_DEFAULT_BRANCH}" complete; then\n          continue' in complete_block
	for block in (advance_block, complete_block):
		# Only a terminal failure stops the tick; merge_conflict is never written.
		assert '[ "${PROJECT_STATUS}" = "failed" ]' in block
		assert "merge_conflict" not in block


def _boundary_helper_text() -> str:
	match = re.search(r"^_integration_sync_at_boundary\(\) \{\n.*?^\}\n", _poller_text(), re.S | re.M)
	assert match
	return match.group(0)


def _boundary(state: dict, *, key: str, attempted: str = "", sync_rc: int = 0) -> tuple[subprocess.CompletedProcess, dict]:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		state_file = tmp / "state.json"
		state_file.write_text(json.dumps(state), encoding="utf-8")
		calls = tmp / "calls"
		script = "\n".join([
			"set -euo pipefail",
			f"STATE_FILE={str(state_file)!r}",
			"TRACKING_NUM=12",
			f"INTEGRATION_SYNC_ATTEMPTED_TRACKING={attempted!r}",
			f'sync_default_into_integration_branch() {{ echo "sync $*" >> {str(calls)!r}; return {sync_rc}; }}',
			f'post_state_comment() {{ echo "post" >> {str(calls)!r}; }}',
			_boundary_helper_text(),
			f'rc=0; _integration_sync_at_boundary orchestrator/project-12 main {key} || rc=$?',
			'echo "rc=${rc}"',
			f'cat {str(calls)!r} 2>/dev/null || true',
		])
		result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
		return result, json.loads(state_file.read_text(encoding="utf-8"))


def test_boundary_forces_one_sync_and_records_it() -> None:
	result, state = _boundary({"sync": {"status": "active"}}, key="wave-2")
	assert result.returncode == 0, result.stderr
	assert "sync orchestrator/project-12 main force" in result.stdout
	assert "post" in result.stdout and "rc=0" in result.stdout
	assert state["sync"]["boundary_forced"] == "wave-2"


def test_boundary_already_forced_does_not_sync_again() -> None:
	result, _ = _boundary({"sync": {"boundary_forced": "wave-2"}}, key="wave-2")
	assert "outcome=skip reason=boundary_already_forced boundary=wave-2" in result.stdout
	assert "sync " not in result.stdout
	# A new boundary forces again.
	result, state = _boundary({"sync": {"boundary_forced": "wave-2"}}, key="complete")
	assert "sync orchestrator/project-12 main force" in result.stdout
	assert state["sync"]["boundary_forced"] == "complete"


def test_boundary_skips_when_this_tick_already_synced() -> None:
	result, state = _boundary({"sync": {}}, key="wave-3", attempted="12")
	assert "outcome=skip reason=synced_this_tick boundary=wave-3" in result.stdout
	assert "sync " not in result.stdout
	assert state["sync"]["boundary_forced"] == "wave-3"


def test_boundary_returns_a_terminal_sync_failure_without_recording() -> None:
	result, state = _boundary({"sync": {}}, key="wave-3", sync_rc=1)
	assert "rc=1" in result.stdout
	assert "boundary_forced" not in state["sync"]
	assert "post" not in result.stdout


def test_sync_marks_the_merges_attempt_for_this_tick() -> None:
	body = _sync_function_text()
	mark_at = body.index('INTEGRATION_SYNC_ATTEMPTED_TRACKING="${TRACKING_NUM:-}"')
	assert body.index('sync_throttle_reason="$(_integration_sync_throttle_reason') < mark_at < body.index('gh api "repos/${GITHUB_REPOSITORY}/merges"')


def test_sync_checks_the_throttle_before_the_merges_call_and_records_the_clock() -> None:
	body = _sync_function_text()
	throttle_at = body.index('sync_throttle_reason="$(_integration_sync_throttle_reason "${sync_force}")"')
	merges_at = body.index('gh api "repos/${GITHUB_REPOSITORY}/merges"')
	superseded_at = body.index("evaluate_sync_superseded_by_main")
	assert superseded_at < throttle_at < merges_at
	assert "outcome=skip reason=${sync_throttle_reason:-within_interval}" in body
	assert '"last_default_sync_at": $now' in body
	assert body.index('"last_default_sync_at": $now') > merges_at
	# Only a merge that created a commit (201, output on stdout) posts the state.
	assert 'if [ -s "${sync_merge_out_file}" ] && [ "${sync_status}" = "active" ]' in body


def test_security_pass_caller_forces_the_sync() -> None:
	assert 'sync_default_into_integration_branch "${integration_branch}" "${default_branch}" force || return $?' in _sync_helper_text()


def test_poll_step_passes_the_interval_with_a_default() -> None:
	workflow = (REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
	assert "INTEGRATION_SYNC_MIN_INTERVAL_SECS: ${{ vars.INTEGRATION_SYNC_MIN_INTERVAL_SECS || '86400' }}" in workflow

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

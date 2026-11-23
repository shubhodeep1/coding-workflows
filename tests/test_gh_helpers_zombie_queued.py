#!/usr/bin/env python3
"""Issue #9473: wedged-queued run helpers in scripts/gh_helpers.sh.

GitHub can leave a run in status=queued with zero jobs and refuses to cancel
or delete it. zombie_queued_cutoff_epoch / zombie_queued_jq_def /
zombie_queued_log_ignored let every active-run guard ignore such a run once
it is older than ZOMBIE_RUN_MAX_QUEUED_HOURS (default 24; 0 or invalid
disables the rule).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


def _iso(hours_ago: float) -> str:
	return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bash(body: str, env: dict[str, str] | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
	run_env = {k: v for k, v in os.environ.items() if k not in ("ZOMBIE_RUN_MAX_QUEUED_HOURS", "BASH_ENV", "ENV")}
	run_env["PYTHONDONTWRITEBYTECODE"] = "1"
	if env:
		run_env.update(env)
	script = f'set -euo pipefail\nsource "{GH_HELPERS}"\n{body}\n'
	return subprocess.run(["bash", "-c", script], env=run_env, input=stdin, capture_output=True, text=True, timeout=60)


def _cutoff(env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	return _bash("zombie_queued_cutoff_epoch", env)


def _active_ids(runs: list[dict], env: dict[str, str] | None = None) -> list:
	result = _bash(
		'c="$(zombie_queued_cutoff_epoch)"\n'
		'jq -c --argjson c "${c}" "$(zombie_queued_jq_def)"\'[.[] | select(is_wedged_queued($c) | not) | (.id // .databaseId)]\'',
		env,
		stdin=json.dumps(runs),
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def test_cutoff_defaults_to_24_hours() -> None:
	result = _cutoff()
	assert result.returncode == 0, result.stderr
	assert abs(int(result.stdout) - (int(time.time()) - 24 * 3600)) <= 30
	assert result.stderr == ""


def test_cutoff_zero_disables_the_rule() -> None:
	result = _cutoff({"ZOMBIE_RUN_MAX_QUEUED_HOURS": "0"})
	assert result.stdout == "0"
	assert result.stderr == ""


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "123456", " 24"])
def test_invalid_value_disables_the_rule_with_a_warning(value: str) -> None:
	result = _cutoff({"ZOMBIE_RUN_MAX_QUEUED_HOURS": value})
	assert result.returncode == 0
	assert result.stdout == "0"
	assert "::warning::ZOMBIE_RUN_MAX_QUEUED_HOURS is not a whole number of hours; wedged-queued filter disabled" in result.stderr


def test_queued_run_ages_out_at_the_cutoff_only() -> None:
	runs = [
		{"id": 1, "status": "queued", "created_at": _iso(23)},
		{"id": 2, "status": "queued", "created_at": _iso(25)},
	]
	assert _active_ids(runs) == [1]


@pytest.mark.parametrize("status", ["in_progress", "pending", "waiting", "requested"])
def test_other_active_statuses_never_age_out(status: str) -> None:
	assert _active_ids([{"id": 3, "status": status, "created_at": _iso(100)}]) == [3]


@pytest.mark.parametrize("created_at", [None, "", "garbage", "2026-11-11T20:17:00+08:00", 12345])
def test_missing_or_unparseable_created_at_stays_active(created_at: object) -> None:
	run: dict = {"id": 4, "status": "queued"}
	if created_at is not None:
		run["created_at"] = created_at
	assert _active_ids([run]) == [4]


def test_gh_run_list_shape_and_fractional_seconds() -> None:
	stamp = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S.123Z")
	runs = [{"databaseId": 5, "status": "QUEUED", "createdAt": stamp, "workflowName": "AI Implement"}]
	assert _active_ids(runs) == []


def test_disabled_rule_keeps_every_queued_run_active() -> None:
	runs = [{"id": 6, "status": "queued", "created_at": _iso(500)}]
	assert _active_ids(runs, {"ZOMBIE_RUN_MAX_QUEUED_HOURS": "0"}) == [6]


def test_log_ignored_reports_each_run_once_per_guard_on_stderr() -> None:
	runs = {"workflow_runs": [
		{"id": 43095142301, "status": "queued", "created_at": _iso(25), "path": ".github/workflows/implement.yml"},
		{"id": 8, "status": "queued", "created_at": _iso(1), "path": ".github/workflows/implement.yml"},
		{"id": 9, "status": "in_progress", "created_at": _iso(50), "path": ".github/workflows/implement.yml"},
	]}
	payload = json.dumps(runs)
	result = _bash(
		f"c=\"$(zombie_queued_cutoff_epoch)\"\nj='{payload}'\n"
		'zombie_queued_log_ignored guard_a "${c}" <<< "${j}"\n'
		'zombie_queued_log_ignored guard_a "${c}" <<< "${j}"\n'
		'zombie_queued_log_ignored guard_b "${c}" <<< "${j}"\n'
		'zombie_queued_log_ignored guard_c 0 <<< "${j}"\n'
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	lines = [line for line in result.stderr.splitlines() if line.startswith("ZOMBIE_RUN_IGNORED")]
	assert lines == [
		"ZOMBIE_RUN_IGNORED run=43095142301 workflow=.github/workflows/implement.yml age_h=25 guard=guard_a",
		"ZOMBIE_RUN_IGNORED run=43095142301 workflow=.github/workflows/implement.yml age_h=25 guard=guard_b",
	]


def test_log_ignored_tolerates_bad_input() -> None:
	result = _bash('zombie_queued_log_ignored g 1000 <<< "not json"\nzombie_queued_log_ignored g abc <<< "[]"\necho ok')
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "ok"


def test_jq_def_heredoc_has_no_bare_closing_brace_line() -> None:
	"""tests/test_codex_stall_guard_poller.py extracts functions up to the first ^}$ line."""
	text = GH_HELPERS.read_text(encoding="utf-8")
	start = text.index("zombie_queued_jq_def()\n")
	body = text[start:text.index("\nJQ_DEF\n", start)]
	assert "\n}\n" not in body


def test_peer_filter_ignores_only_the_wedged_queued_run(tmp_path: Path) -> None:
	runs = {"workflow_runs": [
		{"id": 101, "status": "queued", "created_at": _iso(1), "path": ".github/workflows/review_autofix.yml"},
		{"id": 102, "status": "queued", "created_at": _iso(25), "path": ".github/workflows/review_autofix.yml"},
		{"id": 103, "status": "in_progress", "created_at": _iso(2), "path": ".github/workflows/review_autofix.yml"},
	]}
	payload = tmp_path / "runs.json"
	payload.write_text(json.dumps(runs), encoding="utf-8")
	body = f'''
gh_api_retry() {{ cat "{payload}"; }}
gh_retry() {{ cat "{payload}"; }}
gh() {{ cat "{payload}"; }}
emit_event() {{ :; }}
export GITHUB_REPOSITORY=octo/example GITHUB_RUN_ID=999
rc=0
autofix_retrigger_has_inflight_peer 0 ai/issue-1 999 || rc=$?
echo "rc=${{rc}}"
'''
	result = _bash(body)
	assert "peer_count=2" in result.stdout, result.stdout + result.stderr
	assert "ZOMBIE_RUN_IGNORED run=102 workflow=.github/workflows/review_autofix.yml age_h=25 guard=autofix_inflight_peer" in result.stderr

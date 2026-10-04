#!/usr/bin/env python3
"""Unblock scan (plan Phase 7): scripts/unblock_scan.py and its poller wiring."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "unblock_scan.py"
POLLER = ROOT / "scripts" / "orchestrate_poll_process.sh"
POLL_WORKFLOW = ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
SPEC = importlib.util.spec_from_file_location("unblock_scan", SCRIPT)
scan = importlib.util.module_from_spec(SPEC)
sys.modules["unblock_scan"] = scan
SPEC.loader.exec_module(scan)

BOT = "pipeline-bot"
NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)


def _iso(hours_ago: float) -> str:
	return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(number: int, labels: list[str], updated_hours_ago: float = 5, pr: bool = False) -> dict:
	return {"number": number, "labels": labels, "pull_request": pr, "updated_at": _iso(updated_hours_ago)}


def _details(label: str, labeled_hours_ago: float, comments: list[dict] | None = None) -> dict:
	return {"labeled": [{"label": label, "created_at": _iso(labeled_hours_ago)}], "comments": comments or []}


def _select(search, details, runs=None, failed=None, limit=5) -> dict:
	return scan.select(
		search,
		details,
		runs or [],
		failed or [],
		BOT,
		NOW,
		dt.timedelta(minutes=30),
		dt.timedelta(hours=6),
		dt.timedelta(minutes=60),
		limit,
	)


def test_picks_items_blocked_long_enough_and_classifies_them() -> None:
	search = [
		_item(1, ["ai:blocked"]),
		_item(2, ["ai:needs-human"], pr=True),
		_item(3, ["ai:orchestrator-tracking", "ai:validation-failed"]),
	]
	details = {"1": _details("ai:blocked", 2), "2": _details("ai:needs-human", 3), "3": _details("ai:validation-failed", 1)}
	result = _select(search, details)
	assert result["dispatch"] == [{"item": 2, "kind": "pr"}]
	assert result["skipped"]["over_tick_cap"] == 2


def test_age_comes_from_the_label_event_not_the_last_update() -> None:
	result = _select([_item(1, ["ai:blocked"], updated_hours_ago=10)], {"1": _details("ai:blocked", 0.2)})
	assert result["dispatch"] == [] and result["skipped"] == {"blocked_too_recently": 1}


def test_recent_trusted_marker_holds_the_item_and_forged_ones_do_not() -> None:
	verdict = {"login": BOT, "body": "done\n<!-- ai:unblock:v1 item=1 stop=blocked fingerprint=0123456789ab verdict=retry_budget round=1 -->", "created_at": _iso(2)}
	wait = {"login": BOT, "body": "<!-- ai:unblock-wait:v1 item=1 fixup=9 -->", "created_at": _iso(1)}
	forged = dict(verdict, login="someone")
	quoted = dict(verdict, body=verdict["body"] + "\nquoted above")
	assert _select([_item(1, ["ai:blocked"])], {"1": _details("ai:blocked", 8, [verdict])})["skipped"] == {"recent_verdict": 1}
	assert _select([_item(1, ["ai:blocked"])], {"1": _details("ai:blocked", 8, [wait])})["skipped"] == {"recent_verdict": 1}
	assert _select([_item(1, ["ai:blocked"])], {"1": _details("ai:blocked", 8, [forged, quoted])})["dispatch"] == [{"item": 1, "kind": "issue"}]
	old = dict(verdict, created_at=_iso(7))
	assert _select([_item(1, ["ai:blocked"])], {"1": _details("ai:blocked", 8, [old])})["dispatch"] == [{"item": 1, "kind": "issue"}]


def test_running_or_recent_judge_runs_hold_the_item() -> None:
	search = [_item(1, ["ai:blocked"]), _item(2, ["ai:blocked"]), _item(3, ["ai:blocked"])]
	details = {str(n): _details("ai:blocked", 5) for n in (1, 2, 3)}
	runs = [
		{"display_title": "Unblock judge #1", "status": "in_progress", "created_at": _iso(3)},
		{"display_title": "Unblock judge #2", "status": "completed", "created_at": _iso(0.5)},
		{"display_title": "Unblock judge #3", "status": "completed", "created_at": _iso(2)},
		{"display_title": "Something else #3", "status": "queued", "created_at": _iso(0.1)},
	]
	result = _select(search, details, runs)
	assert result["dispatch"] == []
	assert result["skipped"] == {"judge_running": 3}
	# A cancelled pending run is not active; once the running job completes,
	# only its own item stays within the recent-run cooldown.
	runs[0]["status"] = "completed"
	runs[0]["created_at"] = _iso(2)
	runs[1]["status"] = "cancelled"
	assert _select(search, details, runs)["dispatch"] == [{"item": 1, "kind": "issue"}]
	assert _select([search[1]], {"2": details["2"]}, runs)["dispatch"] == [{"item": 2, "kind": "issue"}]


def test_failed_projects_without_a_label_are_judged_and_the_cap_holds() -> None:
	search = [_item(n, ["ai:blocked"]) for n in range(1, 8)]
	details = {str(n): _details("ai:blocked", 10 - n) for n in range(1, 8)}
	state = {"login": BOT, "body": "<!-- ORCHESTRATOR_STATE_V2 part=1/1 manifest=" + "0" * 64 + " -->", "created_at": _iso(12)}
	details["40"] = {"labeled": [], "comments": [state]}
	result = _select(search, details, failed=[40], limit=5)
	assert result["dispatch"][0] == {"item": 40, "kind": "project"}
	assert len(result["dispatch"]) == 1
	assert result["skipped"]["over_tick_cap"] == 7
	# With no label event and no trusted state comment the age is unknown:
	# the project is held, never guessed old.
	details["40"] = {"labeled": [], "comments": [dict(state, login="someone")]}
	result = _select(search, details, failed=[40], limit=5)
	assert {"item": 40, "kind": "project"} not in result["dispatch"]
	assert result["skipped"]["blocked_too_recently"] == 1


def test_closed_by_judge_missing_details_and_unlabelled_items_are_skipped() -> None:
	search = [_item(1, ["ai:blocked", "ai:unblock-closed"]), _item(2, ["ai:blocked"]), _item(3, ["ai:planning"])]
	result = _select(search, {"1": _details("ai:blocked", 5)})
	assert result["dispatch"] == []
	assert result["skipped"] == {"closed_by_judge": 1, "no_details": 1, "no_block_label": 1}


def test_cli_accepts_the_raw_api_shapes(tmp_path: Path) -> None:
	(tmp_path / "search.json").write_text(json.dumps({"items": [_item(1, ["ai:blocked"])]}), encoding="utf-8")
	(tmp_path / "details.json").write_text(json.dumps({"1": _details("ai:blocked", 5)}), encoding="utf-8")
	(tmp_path / "runs.json").write_text(json.dumps({"workflow_runs": []}), encoding="utf-8")
	result = subprocess.run(
		[sys.executable, str(SCRIPT), "select", "--search-file", str(tmp_path / "search.json"), "--details-file", str(tmp_path / "details.json"),
		 "--runs-file", str(tmp_path / "runs.json"), "--trusted-login", BOT, "--now", "2026-10-04T12:00:00Z"],
		capture_output=True, text=True, check=False,
	)
	assert result.returncode == 0, result.stdout
	assert json.loads(result.stdout)["dispatch"] == [{"item": 1, "kind": "issue"}]
	bad = subprocess.run([sys.executable, str(SCRIPT), "select", "--search-file", "x", "--details-file", "x", "--runs-file", "x", "--trusted-login", "bad login", "--now", "x"], capture_output=True, text=True, check=False)
	assert bad.returncode == 1


def test_poller_runs_the_scan_once_per_tick_after_the_project_loop() -> None:
	lines = POLLER.read_text(encoding="utf-8").splitlines()
	top_level = [i for i, line in enumerate(lines) if line.startswith("run_unblock_scan || echo ")]
	assert len(top_level) == 1
	release = lines.index("release_staged_support_needs_human_latches")
	sweep = lines.index("close_merged_issues_sweep")
	assert release < top_level[0] < sweep


def test_scan_budget_is_one_search_one_graphql_one_runs_list() -> None:
	text = POLLER.read_text(encoding="utf-8")
	body = text[text.index("run_unblock_scan() {"):text.index("\n}\n", text.index("run_unblock_scan() {"))]
	assert body.count('gh api --method GET "search/issues"') == 1
	assert body.count("gh api graphql") == 1
	assert body.count("unblock_judge_dispatch.yml/runs") == 1
	assert "gh workflow run unblock_judge_dispatch.yml" in body
	assert 'UNBLOCK_JUDGE_ENABLED:-true}" = "false"' in body


def test_project_hooks_run_before_the_command_handlers() -> None:
	text = POLLER.read_text(encoding="utf-8")
	hook = text.index("handle_unblock_judge_project_hooks || unblock_hook_rc=$?")
	assert hook < text.index("# /security-pass-waive <finding_id>")
	assert hook < text.index("# /re-security-pass — manual reset from security-pass exhaustion")
	assert 'echo "${TRACKING_NUM}" >> "${UNBLOCK_FAILED_PROJECTS_FILE}"' in text


def test_hand_overs_add_a_scanned_label_or_failed_state() -> None:
	text = POLLER.read_text(encoding="utf-8")
	assert text.count('unblock_handover_judge_output "llm_failed"') == 1
	assert text.count('unblock_handover_judge_output "unparseable"') == 1
	assert text.count("unblock_handover_merge_deferral ") == 2
	review = (ROOT / ".github/workflows/review_autofix.yml").read_text(encoding="utf-8")
	assert "llm_failed*|json_parse_failed*|missing_followup_details|merged_pr_unsafe_action|auto_merge_disabled)" in review
	heal = (ROOT / "scripts/workflow_failure_heal_intake.sh").read_text(encoding="utf-8")
	assert 'gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${SOURCE_REPO}" --add-label "${ESCALATED_LABEL}"' in heal


def test_poll_workflow_stages_the_scan_and_passes_its_knobs() -> None:
	text = POLL_WORKFLOW.read_text(encoding="utf-8")
	assert "for unblock_asset in scripts/unblock_scan.py scripts/unblock_ledger.py; do" in text
	for name, default in (
		("UNBLOCK_JUDGE_ENABLED", "true"),
		("UNBLOCK_JUDGE_MAX_DISPATCH_PER_TICK", "5"),
		("UNBLOCK_JUDGE_MIN_BLOCKED_MINUTES", "30"),
		("UNBLOCK_JUDGE_RETRY_HOURS", "6"),
		("UNBLOCK_JUDGE_INFLIGHT_MINUTES", "60"),
		("JUDGE_OUTPUT_FAILURE_MAX", "3"),
	):
		assert f"{name}: ${{{{ vars.{name} || '{default}' }}}}" in text
	yaml.safe_load(text)


@pytest.mark.parametrize("path", [".github/workflows/implement.yml", "scripts/implement_commit_changes.sh"])
def test_bulk_override_never_covers_canonical_deletions(path: str) -> None:
	text = (ROOT / path).read_text(encoding="utf-8")
	assert 'if [ "${UNBLOCK_BULK_DELETE_OVERRIDE:-false}" = "true" ] && [ -z "${canonical_deletions}" ] &&' in text
	assert '--argjson paths "${UNBLOCK_BULK_DELETE_PATHS:-null}"' in text
	assert '[ "${unblock_bulk_override_applies}" != "true" ]; then' in text


def test_bulk_override_spend_and_judge_log_reference_extract_scalar_captures() -> None:
	implement = (ROOT / ".github/workflows/implement.yml").read_text(encoding="utf-8")
	assert 'scan("<!-- ai:unblock-override-used:v1 comment=([0-9]+) -->") | .[0]' in implement
	judge = (ROOT / "scripts/unblock_judge.sh").read_text(encoding="utf-8")
	assert 'scan("/actions/runs/([0-9]+)") | .[0]' in judge

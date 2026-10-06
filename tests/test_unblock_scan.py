#!/usr/bin/env python3
"""Unblock scan (plan Phase 7): scripts/unblock_scan.py and its poller wiring."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
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


def test_incomplete_comment_window_cannot_prove_six_hour_cooldown() -> None:
	search = [_item(1, ["ai:blocked"])]
	info = _details("ai:blocked", 8, [{"login": BOT, "body": "newer comment", "created_at": _iso(2)}])
	info["history_incomplete"] = True
	assert _select(search, {"1": info})["skipped"] == {"unverified_marker_history": 1}
	info["comments"] = []
	assert _select(search, {"1": info})["skipped"] == {"unverified_marker_history": 1}
	info["comments"].append({"login": BOT, "body": "older comment", "created_at": _iso(7)})
	# An even older wait marker can have been refreshed after this comment
	# without moving into the creation-ordered last-30 window.
	assert _select(search, {"1": info})["skipped"] == {"unverified_marker_history": 1}
	assert _select(search, {"1": info})["verify_history"] == 1
	verified = dict(info, history_incomplete=False)
	assert _select(search, {"1": verified})["dispatch"] == [{"item": 1, "kind": "issue"}]
	# The full history exposes an older wait comment refreshed one hour ago.
	verified["comments"] = [{"login": BOT, "body": "<!-- ai:unblock-wait:v1 item=1 fixup=9 -->", "created_at": _iso(1)}]
	assert _select(search, {"1": verified})["skipped"] == {"recent_verdict": 1}


def test_incomplete_histories_rotate_one_verification_per_tick() -> None:
	search = [_item(1, ["ai:blocked"]), _item(2, ["ai:blocked"])]
	details = {str(n): dict(_details("ai:blocked", 8), history_incomplete=True) for n in (1, 2)}
	first = _select(search, details)
	second = scan.select(search, details, [], [], BOT, NOW + dt.timedelta(minutes=5),
		dt.timedelta(minutes=30), dt.timedelta(hours=6), dt.timedelta(minutes=60), 5)
	assert {first["verify_history"], second["verify_history"]} == {1, 2}
	assert first["dispatch"] == second["dispatch"] == []
	assert _select(search, details, limit=0)["verify_history"] is None


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


def test_spoofed_closed_label_does_not_hide_a_still_failed_project() -> None:
	search = [_item(40, ["ai:orchestrator-tracking", "ai:blocked", "ai:unblock-closed"])]
	details = {"40": _details("ai:blocked", 8)}
	assert _select(search, details, failed=[40])["dispatch"] == [{"item": 40, "kind": "project"}]
	assert _select(search, details)["skipped"] == {"closed_by_judge": 1}


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
	assert body.count('gh api --paginate --slurp "repos/${GITHUB_REPOSITORY}/issues/${verify_item}/comments?per_page=100"') == 1
	assert '([.[][]] | length >= $expected)' in body
	assert 'created_at: (.updated_at // .created_at)' in body
	assert "gh workflow run unblock_judge_dispatch.yml" in body
	assert 'UNBLOCK_JUDGE_ENABLED:-true}" = "false"' in body


def test_project_hooks_run_before_the_command_handlers() -> None:
	text = POLLER.read_text(encoding="utf-8")
	hook = text.index("handle_unblock_judge_project_hooks || unblock_hook_rc=$?")
	assert hook < text.index("# /security-pass-waive <finding_id>")
	assert hook < text.index("# /re-security-pass — manual reset from security-pass exhaustion")
	assert 'ai:unblock:v1 item=' in text
	assert 'action=abandoned outcome=refused reason=' in text
	assert 'echo "${TRACKING_NUM}" >> "${UNBLOCK_FAILED_PROJECTS_FILE}"' in text
	assert 'capture("^<!-- ai:unblock-fixup-request:v1 item=' in text
	assert 'id>unblock-[0-9]+-r[0-9]+) -->$")?' in text
	assert 'split("\\n") | map(rtrimstr("\\r"))' in text
	assert '($failed[0] | unique) as $all_projects' in text
	assert '((now / 300 | floor) % ($all_projects | length)) as $offset' in text


def _close_comment(comment_id: int, *, item: int = 40, stop: str = "project-failed",
	verdict: str = "close", login: str = BOT, suffix: str = "") -> dict:
	marker = (f"<!-- ai:unblock:v1 item={item} stop={stop} fingerprint=0123456789ab "
		f"verdict={verdict} round=1 -->")
	return {"id": comment_id, "user": {"login": login}, "created_at": _iso(1), "body": f"Judge verdict\n{marker}{suffix}"}


def _run_close_hook(tmp_path: Path, comments: list[dict], labels: list[str], status: str,
	login_ok: bool = True, comments_ok: bool = True) -> tuple[subprocess.CompletedProcess[str], dict, list[str], list[str]]:
	text = POLLER.read_text(encoding="utf-8")
	hook_start = text.index("handle_unblock_judge_project_hooks() {")
	has_label_start = text.index("has_label() {")
	hook = text[hook_start:text.index("\n}\n", hook_start) + 3]
	has_label = text[has_label_start:text.index("\n}\n", has_label_start) + 3]
	state_file = tmp_path / "project.json"
	state_file.write_text(json.dumps({"status": status}), encoding="utf-8")
	patch_calls = tmp_path / "patch_calls"
	state_posts = tmp_path / "state_posts"
	harness = '''
unblock_trusted_login() {
	if [ "$LOGIN_OK" = "true" ]; then UNBLOCK_TRUSTED_LOGIN=pipeline-bot; else UNBLOCK_TRUSTED_LOGIN=""; fi
}
gh_retry() { printf '%s\\n' "$*" >> "$PATCH_CALLS_FILE"; }
post_state_comment() { echo posted >> "$STATE_POSTS_FILE"; }
''' + has_label + hook + '\nhandle_unblock_judge_project_hooks\n'
	env = dict(os.environ, STATE_FILE=str(state_file), PATCH_CALLS_FILE=str(patch_calls),
		STATE_POSTS_FILE=str(state_posts), LOGIN_OK="true" if login_ok else "false",
		COMMENTS_FETCH_OK="true" if comments_ok else "false", COMMENTS=json.dumps(comments),
		TRACKING_NUM="40", TRACKING_LABELS=json.dumps(labels), PROJECT_STATUS=status,
		GITHUB_REPOSITORY="o/r", UNBLOCK_JUDGE_ENABLED="false")
	result = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, check=False)
	return (result, json.loads(state_file.read_text(encoding="utf-8")),
		patch_calls.read_text().splitlines() if patch_calls.exists() else [],
		state_posts.read_text().splitlines() if state_posts.exists() else [])


@pytest.mark.parametrize(("comments", "status", "login_ok", "comments_ok", "reason"), [
	([], "failed", True, True, "no_trusted_marker"),
	([_close_comment(1, login="mallory")], "failed", True, True, "no_trusted_marker"),
	([_close_comment(1, suffix="\nQuoted text")], "failed", True, True, "no_trusted_marker"),
	([_close_comment(1, item=5)], "failed", True, True, "no_trusted_marker"),
	([_close_comment(1), _close_comment(2, verdict="retry_budget")], "failed", True, True, "stale_marker"),
	([_close_comment(1)], "in_progress", True, True, "state_mismatch"),
	([_close_comment(1)], "failed", False, True, "login_unavailable"),
	([_close_comment(1)], "failed", True, False, "comments_unavailable"),
])
def test_project_close_refuses_unverified_labels(tmp_path: Path, comments: list[dict], status: str,
	login_ok: bool, comments_ok: bool, reason: str) -> None:
	result, state, calls, posts = _run_close_hook(tmp_path, comments, ["ai:unblock-closed"], status, login_ok, comments_ok)
	assert result.returncode == 0, result.stderr
	assert f"action=abandoned outcome=refused reason={reason}" in result.stdout
	assert state["status"] == status and calls == [] and posts == []


@pytest.mark.parametrize(("status", "stop", "labels", "expected_posts"), [
	("failed", "project-failed", ["ai:unblock-closed"], ["posted"]),
	("failed", "security-pass-failed", ["ai:unblock-closed", "ai:security-pass-failed"], ["posted"]),
	("abandoned", "project-failed", ["ai:unblock-closed"], []),
])
def test_project_close_requires_current_trusted_verdict_and_retries_failed_close(tmp_path: Path,
	status: str, stop: str, labels: list[str], expected_posts: list[str]) -> None:
	result, state, calls, posts = _run_close_hook(tmp_path, [_close_comment(1, stop=stop)], labels, status)
	assert result.returncode == 10, result.stderr
	assert "action=abandoned outcome=closed" in result.stdout
	assert state["status"] == "abandoned" and posts == expected_posts
	assert calls == ["gh api -X PATCH repos/o/r/issues/40 -f state=closed -f state_reason=not_planned"]


def test_project_close_refuses_a_removed_stop_label(tmp_path: Path) -> None:
	result, state, calls, posts = _run_close_hook(tmp_path, [_close_comment(1, stop="security-pass-failed")],
		["ai:unblock-closed"], "failed")
	assert result.returncode == 0, result.stderr
	assert "outcome=refused reason=state_mismatch" in result.stdout
	assert state["status"] == "failed" and calls == [] and posts == []


@pytest.mark.parametrize("status", ["in_progress", "security-pass", "security-pass-fixing"])
def test_project_close_refuses_a_resumed_project_even_with_stale_block_label(tmp_path: Path, status: str) -> None:
	result, state, calls, posts = _run_close_hook(tmp_path, [_close_comment(1, stop="security-pass-failed")],
		["ai:unblock-closed", "ai:security-pass-failed"], status)
	assert result.returncode == 0, result.stderr
	assert "outcome=refused reason=state_mismatch" in result.stdout
	assert state["status"] == status and calls == [] and posts == []


def test_project_close_orders_same_timestamp_verdicts_by_comment_id(tmp_path: Path) -> None:
	result, state, calls, posts = _run_close_hook(tmp_path,
		[_close_comment(12), _close_comment(11, verdict="retry_budget")], ["ai:unblock-closed"], "failed")
	assert result.returncode == 10, result.stderr
	assert state["status"] == "abandoned" and len(calls) == 1 and posts == ["posted"]


@pytest.mark.parametrize("state_header", ["<!-- ORCHESTRATOR_STATE_V1",
	"<!-- ORCHESTRATOR_STATE_V2 part=1/1 manifest=" + "0" * 64 + " -->"])
def test_project_close_refuses_a_verdict_before_a_later_failure_state(tmp_path: Path, state_header: str) -> None:
	state_body = f"{state_header}\n{{}}"
	if state_header.endswith("_V1"):
		state_body += "\nORCHESTRATOR_STATE_V1 -->"
	state_comment = {"id": 2, "user": {"login": BOT}, "body": state_body}
	result, state, calls, posts = _run_close_hook(tmp_path,
		[_close_comment(1), state_comment], ["ai:unblock-closed"], "failed")
	assert result.returncode == 0, result.stderr
	assert "outcome=refused reason=stale_marker" in result.stdout
	assert state["status"] == "failed" and calls == [] and posts == []

	result, state, calls, posts = _run_close_hook(tmp_path,
		[state_comment, _close_comment(3)], ["ai:unblock-closed"], "failed")
	assert result.returncode == 10, result.stderr
	assert state["status"] == "abandoned" and len(calls) == 1 and posts == ["posted"]


def test_project_close_retry_allows_its_own_abandoned_state_write(tmp_path: Path) -> None:
	result, state, calls, posts = _run_close_hook(tmp_path,
		[_close_comment(1), {"id": 2, "user": {"login": BOT}, "body": "<!-- ORCHESTRATOR_STATE_V1\n{}\nORCHESTRATOR_STATE_V1 -->"}],
		["ai:unblock-closed"], "abandoned")
	assert result.returncode == 10, result.stderr
	assert state["status"] == "abandoned" and len(calls) == 1 and posts == []


def _run_fixup_hook(tmp_path: Path, item: int, members: list[int], pr: dict | None = None, state: dict | None = None,
	trusted_members: list[int] | None = None, include_trusted_state_comment: bool = True,
	trusted_integration_branch: str | None = None, request_body: str | None = None) -> tuple[subprocess.CompletedProcess[str], dict, list[str], list[str]]:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index("handle_unblock_judge_project_hooks() {")
	hook = text[start:text.index("\n}\n", start) + 3]
	state_file = tmp_path / "project.json"
	current = state or {"integration_branch": "orchestrator/project-40", "current_wave": 1,
		"status": "failed", "issue_number_map": {f"issue-{n}": n for n in members}, "waves": [{"issues": []}]}
	state_file.write_text(json.dumps(current), encoding="utf-8")
	pr_calls = tmp_path / "pr_calls"
	creates = tmp_path / "creates"
	trusted_state = dict(current, issue_number_map={f"issue-{n}": n for n in (members if trusted_members is None else trusted_members)},
		integration_branch=trusted_integration_branch or current["integration_branch"])
	payload = json.dumps(trusted_state).encode("utf-8")
	manifest = hashlib.sha256(payload).hexdigest()
	comments = [{"id": 10, "user": {"login": BOT}, "body": request_body if request_body is not None else f"<!-- ai:unblock-fixup-request:v1 item={item} id=unblock-{item}-r1 -->\n### Narrow the fix\nOnly this part."}]
	if include_trusted_state_comment:
		comments.insert(0, {"user": {"login": BOT}, "body": f"<!-- ORCHESTRATOR_STATE_V2 part=1/1 manifest={manifest} -->\n{base64.b64encode(payload).decode('ascii')}\nORCHESTRATOR_STATE_V2 -->"})
	if trusted_members is not None:
		forged_payload = json.dumps(current).encode("utf-8")
		forged_manifest = hashlib.sha256(forged_payload).hexdigest()
		comments.insert(1, {"user": {"login": "mallory"},
			"body": f"<!-- ORCHESTRATOR_STATE_V2 part=1/1 manifest={forged_manifest} -->\n{base64.b64encode(forged_payload).decode('ascii')}\nORCHESTRATOR_STATE_V2 -->"})
	harness = '''
has_label() { return 1; }
unblock_trusted_login() { UNBLOCK_TRUSTED_LOGIN=pipeline-bot; }
_fetch_pr_json() { echo "$1" >> "$PR_CALLS_FILE"; printf '%s\\n' "$MOCK_PR_JSON"; }
ensure_label_exists() { :; }
engine_label_create_args() { :; }
gh_retry() {
	if [ "$1 $2 $3" = "gh issue create" ]; then
		echo create >> "$CREATES_FILE"
		echo https://github.com/o/r/issues/901
	fi
}
post_state_comment() { :; }
post_tracking_comment() { :; }
''' + hook + '\nhandle_unblock_judge_project_hooks\n'
	env = dict(os.environ, STATE_FILE=str(state_file), PR_CALLS_FILE=str(pr_calls), CREATES_FILE=str(creates),
		MOCK_PR_JSON=json.dumps(pr) if pr is not None else "{}", TRACKING_NUM="40", TRACKING_LABELS="[]",
		GITHUB_REPOSITORY="o/r", PROJECT_STATUS="failed", COMMENTS=json.dumps(comments), UNBLOCK_JUDGE_ENABLED="true")
	result = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, check=False)
	return result, json.loads(state_file.read_text(encoding="utf-8")), pr_calls.read_text().splitlines() if pr_calls.exists() else [], creates.read_text().splitlines() if creates.exists() else []


@pytest.mark.parametrize("item,members", [(5, [5]), (40, [])])
def test_fixup_hook_accepts_project_members_without_pr_read(tmp_path: Path, item: int, members: list[int]) -> None:
	result, state, calls, creates = _run_fixup_hook(tmp_path, item, members)
	assert result.returncode == 0, result.stderr
	assert calls == [] and creates == ["create"]
	assert state["issue_number_map"][f"unblock-{item}-r1"] == 901


def test_fixup_hook_reports_malformed_trusted_request(tmp_path: Path) -> None:
	result, state, calls, creates = _run_fixup_hook(tmp_path, 5, [5],
		request_body="<!-- ai:unblock-fixup-request:v1 item=5 id=bad -->\n### Narrow the fix")
	assert result.returncode == 0, result.stderr
	assert "action=fixup comment=10 outcome=invalid_request" in result.stdout
	assert calls == creates == [] and "unblock-5-r1" not in state["issue_number_map"]


def test_fixup_hook_accepts_verified_same_repo_pr(tmp_path: Path) -> None:
	pr = {"number": 7, "base": {"ref": "orchestrator/project-40"},
		"head": {"ref": "ai/issue-5", "repo": {"full_name": "O/R"}}}
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5], pr)
	assert result.returncode == 0, result.stderr
	assert calls == ["7"] and creates == ["create"]
	assert state["issue_number_map"]["unblock-7-r1"] == 901


def test_fixup_hook_uses_trusted_membership_for_known_issue(tmp_path: Path) -> None:
	result, state, calls, creates = _run_fixup_hook(tmp_path, 5, [], trusted_members=[5])
	assert result.returncode == 0, result.stderr
	assert calls == [] and creates == ["create"]
	assert state["issue_number_map"]["unblock-5-r1"] == 901


def test_fixup_hook_rejects_preexisting_request_with_only_untrusted_membership(tmp_path: Path) -> None:
	pr = {"number": 7, "base": {"ref": "orchestrator/project-40"},
		"head": {"ref": "ai/issue-5", "repo": {"full_name": "o/r"}}}
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5], pr, trusted_members=[])
	assert result.returncode == 0, result.stderr
	assert "outcome=binding_unverified reason=not_member" in result.stdout
	assert state["unblock_fixup_rejected_ids"] == ["unblock-7-r1"]
	assert calls == ["7"] and creates == []


@pytest.mark.parametrize(("pr", "reason"), [
	({"number": 7, "base": {"ref": "orchestrator/project-40"},
		"head": {"ref": "ai/issue-5", "repo": {"full_name": "evil/r"}}}, "head_repo"),
	({"number": 7, "base": {"ref": "orchestrator/project-40"},
		"head": {"ref": "ai/issue-99", "repo": {"full_name": "o/r"}}}, "not_member"),
])
def test_fixup_hook_does_not_treat_forged_state_pr_as_project_issue(tmp_path: Path, pr: dict, reason: str) -> None:
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5, 7, 99], pr, trusted_members=[5])
	assert result.returncode == 0, result.stderr
	assert f"outcome=binding_unverified reason={reason}" in result.stdout
	assert state["unblock_fixup_rejected_ids"] == ["unblock-7-r1"]
	assert calls == ["7"] and creates == []


def test_fixup_hook_retries_when_trusted_state_is_unavailable(tmp_path: Path) -> None:
	pr = {"number": 7, "base": {"ref": "orchestrator/project-40"},
		"head": {"ref": "ai/issue-5", "repo": {"full_name": "o/r"}}}
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5], pr, include_trusted_state_comment=False)
	assert result.returncode == 0, result.stderr
	assert "outcome=binding_unavailable" in result.stdout
	assert "unblock_fixup_rejected_ids" not in state
	assert "reason=trusted_state" in result.stdout
	assert calls == [] and creates == []
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5], pr, state)
	assert result.returncode == 0, result.stderr
	assert calls == ["7"] and creates == ["create"]
	assert state["issue_number_map"]["unblock-7-r1"] == 901


def test_fixup_hook_forged_membership_defers_without_trusted_state(tmp_path: Path) -> None:
	pr = {"number": 7, "base": {"ref": "orchestrator/project-40"},
		"head": {"ref": "ai/issue-5", "repo": {"full_name": "evil/r"}}}
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5, 7], pr, trusted_members=[], include_trusted_state_comment=False)
	assert result.returncode == 0, result.stderr
	assert "outcome=binding_unavailable reason=trusted_state" in result.stdout
	assert "unblock_fixup_rejected_ids" not in state
	assert calls == [] and creates == []
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5, 7], pr, state, trusted_members=[5])
	assert result.returncode == 0, result.stderr
	assert "outcome=binding_unverified reason=head_repo" in result.stdout
	assert state["unblock_fixup_rejected_ids"] == ["unblock-7-r1"]
	assert calls == ["7"] and creates == []


@pytest.mark.parametrize(("pr", "reason"), [
	({"number": 7, "base": {"ref": "orchestrator/project-40"}, "head": {"ref": "ai/issue-5", "repo": {"full_name": "evil/r"}}}, "head_repo"),
	({"number": 7, "base": {"ref": "orchestrator/project-40"}, "head": {"ref": "ai/issue-5", "repo": None}}, "head_repo"),
	({"number": 7, "base": {"ref": "orchestrator/project-40"}, "head": {"ref": "feature/x", "repo": {"full_name": "o/r"}}}, "head_ref"),
	({"number": 7, "base": {"ref": "orchestrator/project-40"}, "head": {"ref": "ai/issue-99", "repo": {"full_name": "o/r"}}}, "not_member"),
	({"number": 7, "base": {"ref": "main"}, "head": {"ref": "ai/issue-5", "repo": {"full_name": "o/r"}}}, "base"),
])
def test_fixup_hook_remembers_definitive_pr_rejection(tmp_path: Path, pr: dict, reason: str) -> None:
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5], pr)
	assert result.returncode == 0, result.stderr
	assert f"outcome=binding_unverified reason={reason}" in result.stdout
	assert state["unblock_fixup_rejected_ids"] == ["unblock-7-r1"]
	assert calls == ["7"] and creates == []
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5], pr, state)
	assert result.returncode == 0, result.stderr
	assert calls == ["7"] and creates == []  # No new read on the next tick.


def test_fixup_hook_rejects_integration_branch_drift(tmp_path: Path) -> None:
	state = {"integration_branch": "orchestrator/project-99", "current_wave": 1, "status": "failed",
		"issue_number_map": {"issue-5": 5}, "waves": [{"issues": []}]}
	pr = {"number": 7, "base": {"ref": "orchestrator/project-40"},
		"head": {"ref": "ai/issue-5", "repo": {"full_name": "o/r"}}}
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5], pr, state)
	assert result.returncode == 0, result.stderr
	assert "outcome=binding_unverified reason=base" in result.stdout
	assert state["unblock_fixup_rejected_ids"] == ["unblock-7-r1"]
	assert calls == ["7"] and creates == []


def test_fixup_hook_defers_working_state_drift_without_permanent_rejection(tmp_path: Path) -> None:
	state = {"integration_branch": "orchestrator/project-99", "current_wave": 1, "status": "failed",
		"issue_number_map": {"issue-5": 5}, "waves": [{"issues": []}]}
	pr = {"number": 7, "base": {"ref": "orchestrator/project-40"},
		"head": {"ref": "ai/issue-5", "repo": {"full_name": "o/r"}}}
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5], pr, state,
		trusted_integration_branch="orchestrator/project-40")
	assert result.returncode == 0, result.stderr
	assert "outcome=binding_unavailable reason=state_drift" in result.stdout
	assert "unblock_fixup_rejected_ids" not in state
	assert calls == ["7"] and creates == []
	state["integration_branch"] = "orchestrator/project-40"
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5], pr, state)
	assert result.returncode == 0, result.stderr
	assert calls == ["7", "7"] and creates == ["create"]
	assert state["issue_number_map"]["unblock-7-r1"] == 901


def test_fixup_hook_retries_unavailable_pr_read(tmp_path: Path) -> None:
	result, state, calls, creates = _run_fixup_hook(tmp_path, 7, [5])
	assert result.returncode == 0, result.stderr
	assert "outcome=binding_unavailable" in result.stdout
	assert "reason=pr_read" in result.stdout
	assert "unblock_fixup_rejected_ids" not in state
	assert calls == ["7"] and creates == []
	result, _, calls, _ = _run_fixup_hook(tmp_path, 7, [5], state=state)
	assert result.returncode == 0 and calls == ["7", "7"]


def test_fixup_hook_checks_head_and_remembers_rejected_ids() -> None:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index("handle_unblock_judge_project_hooks() {")
	hook = text[start:text.index("\n}\n", start)]
	assert "head.repo.full_name" in hook
	assert "unblock_fixup_rejected_ids" in hook


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
	assert 'unblock_protected_deletions="$(printf \'%s\\n\' "${deleted_staged}" | grep -iE \'^(\\.github|\\.claude|workflow-templates)(/|$)\' || true)"' in text
	assert '[ -z "${unblock_protected_deletions}" ] &&' in text
	assert '--argjson paths "${UNBLOCK_BULK_DELETE_PATHS:-null}"' in text
	assert 'UNBLOCK_BULK_DELETE_OVERRIDE outcome=skip reason=protected_automation_paths' in text
	assert '[ "${unblock_bulk_override_applies}" != "true" ]; then' in text


def test_bulk_override_spend_and_judge_log_reference_extract_scalar_captures() -> None:
	implement = (ROOT / ".github/workflows/implement.yml").read_text(encoding="utf-8")
	assert 'scan("<!-- ai:unblock-override-used:v1 comment=([0-9]+) -->") | .[0]' in implement
	assert 'last // "" | rtrimstr("\\r")' in implement
	assert 'capture("(?m)^Approved deletions: (?<paths>\\\\[[^\\\\r\\\\n]*\\\\])\\\\r?$")' in implement
	judge = (ROOT / "scripts/unblock_judge.sh").read_text(encoding="utf-8")
	assert 'reason=rejection_run_unbound' in implement
	assert 'reason=rejection_snapshot_unverified' in implement
	assert 'actions/runs/${rejection_run_id}/artifacts?per_page=100' in implement
	assert 'reason=approved_paths_not_rejected' in implement
	assert '--approved-json "${approved_deletions}"' in implement
	assert '(.created_at | type == "string") and .created_at <= $approved_at' in implement
	assert implement.index('--approved-json "${approved_deletions}"') < implement.index('ai:unblock-override-used:v1 comment=${override_comment_id}')
	assert 'scan("/actions/runs/([0-9]+)") | .[0]' in judge

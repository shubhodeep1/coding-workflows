#!/usr/bin/env python3
"""Behaviour and wiring contract for the unattended question guard (CLAUDE.md §28.G).

Covers:
  1. Interactive sessions (no remote session id, no marker, or another
     session's marker) are never blocked or denied.
  2. A marked issue-mode session ending on a §2 question or a permission ask
     is blocked; one that posted the ai:claude-blocked comment in the current
     turn is allowed; the cap allows the third stop with a structured line.
  3. AskUserQuestion is denied in a marked session and allowed otherwise.
  4. The fail-open contract, the `mark` subcommand, the settings.json wiring,
     template parity, and the prose in CLAUDE.md and the commands.

The module under test is loaded from the `workflow-templates/.claude/` twin;
`test_template_parity` pins the live `.claude/` copy to it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "unattended_question_guard.py"
TEMPLATE_HOOK_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "unattended_question_guard.py"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
TEMPLATE_SETTINGS_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"
TEMPLATE_COMMANDS = REPO_ROOT / "workflow-templates" / ".claude" / "commands"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
HOOK_COMMAND = 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/unattended_question_guard.py'

SESSION = "cse_01TESTSESSIONabcdef"
NOW = datetime(2026, 9, 29, 3, 0, 0, tzinfo=timezone.utc)


def _load_guard():
	spec = importlib.util.spec_from_file_location("unattended_question_guard", TEMPLATE_HOOK_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


guard = _load_guard()

SECTION_2_QUESTION = """Phase 1 is blocked on a conflict.

> **Q1: How should #4695's conflict be resolved?**
>
> Choices:
> - **A** — Keep both gates (RECOMMENDED)
> - **B** — Keep only the new gate
> - **C** — Drop the change
>
> Reply: `Q1: A`
"""

INLINE_QUESTION = "Stopping here. Q1: A/B/C — which resolution should I apply to #4695?"

PERMISSION_ASKS = [
	"resume blocked: needs GitHub writes (labels, comments, dispatches).",
	"Stage paused, awaiting permissions or fresh session.",
	"I am waiting for your approval before posting the comment.",
	"Please grant the permission prompt so I can continue.",
	"This needs a fresh session to continue.",
]

ORDINARY_REPORT = """Plan: Issue-mode question guard  (docs/plans/issue-4911-unattended-question-guard-plan.md)
Status: IN_PROGRESS
Stage: phase 1/1
Waiting on: PR #4970   Check-in: low-effort Sonnet checker session_01X every hour
Auto-decisions: 8 recorded (this stage: AD-1…AD-8) — listed for review at verify-activation / deploy-activate
- AD-1 [plan, 2026-09-29] Which marker? — Picked: A — a local marker. Status: pending review
Permission prompts: none
Protected-path approval: phase 1 — twin-first per #4750 Q40: A (2026-09-29)
"""


def _marker_dir(tmp_path: Path) -> Path:
	return tmp_path / "unattended-issue-mode"


def _mark(tmp_path: Path, session: str = SESSION) -> Path:
	directory = _marker_dir(tmp_path)
	guard.write_marker("shubhodeep1/coding-workflows", 4911, session, directory, NOW)
	return directory


def _env(session: str | None = SESSION) -> dict:
	return {} if session is None else {guard.ENV_SESSION: session}


def _stop(text: str | None, transcript: Path | None = None) -> dict:
	payload: dict = {"hook_event_name": "Stop", "session_id": "local-uuid", "stop_hook_active": False}
	if text is not None:
		payload["last_assistant_message"] = text
	if transcript is not None:
		payload["transcript_path"] = str(transcript)
	return payload


def _ask() -> dict:
	return {
		"hook_event_name": "PreToolUse",
		"tool_name": "AskUserQuestion",
		"tool_input": {"questions": [{"question": "Which option?", "header": "Q1", "options": []}]},
	}


def _write_transcript(path: Path, entries: list[dict]) -> Path:
	path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
	return path


def _prompt(text: str) -> dict:
	return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_use(tool_id: str, name: str, tool_input: dict) -> dict:
	return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}}


def _tool_result(tool_id: str, is_error: bool = False, content: object = "ok") -> dict:
	item = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
	if is_error:
		item["is_error"] = True
	return {"type": "user", "message": {"role": "user", "content": [item]}}


def _assistant_text(text: str) -> dict:
	return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


BLOCKED_BODY = "<!-- ai:claude-blocked:v1 -->\n🛑 Blocked at phase 1/1: conformance cap reached."

# The marker `_mark` writes targets shubhodeep1/coding-workflows#4911.
MARKED_OWNER = "shubhodeep1"
MARKED_REPO = "coding-workflows"
MARKED_ISSUE = 4911
COMMENT_HTML_URL = "https://github.com/shubhodeep1/coding-workflows/issues/4911#issuecomment-5885870678"
COMMENT_RESULT = json.dumps(
	{
		"id": 5885870678,
		"html_url": COMMENT_HTML_URL,
		"issue_url": "https://api.github.com/repos/shubhodeep1/coding-workflows/issues/4911",
	}
)
LABEL_RESULT = json.dumps([{"name": "ai:claude"}, {"name": "ai:claude-blocked"}])
GH_COMMENT_COMMAND = "gh api repos/shubhodeep1/coding-workflows/issues/4911/comments -f body='" + BLOCKED_BODY + "'"
GH_LABEL_COMMAND = "gh api repos/shubhodeep1/coding-workflows/issues/4911/labels -f 'labels[]=ai:claude-blocked'"


def _mcp_comment(tool_id: str, owner: str = MARKED_OWNER, repo: str = MARKED_REPO, issue: object = MARKED_ISSUE, body: str = BLOCKED_BODY) -> dict:
	return _tool_use(tool_id, "mcp__github__add_issue_comment", {"owner": owner, "repo": repo, "issue_number": issue, "body": body})


def _mcp_label(tool_id: str, owner: str = MARKED_OWNER, repo: str = MARKED_REPO, issue: object = MARKED_ISSUE, labels: list | None = None, method: str | None = "update") -> dict:
	tool_input = {
		"method": method,
		"owner": owner,
		"repo": repo,
		"issue_number": issue,
		"labels": ["ai:security", "ai:claude", "ai:claude-blocked"] if labels is None else labels,
	}
	if method is None:
		del tool_input["method"]
	return _tool_use(tool_id, "mcp__github__issue_write", tool_input)


def _bash(tool_id: str, command: str) -> dict:
	return _tool_use(tool_id, "Bash", {"command": command})


def _verified_label_entries(tool_id: str = "toolu_label") -> list[dict]:
	return [_mcp_label(tool_id), _tool_result(tool_id, content=LABEL_RESULT)]


def _allowed_after(tmp_path: Path, entries: list[dict], text: str = SECTION_2_QUESTION) -> bool:
	"""Evaluate a stop on `text` after `entries` in the current turn; True when allowed."""
	transcript = _write_transcript(tmp_path / "t.jsonl", [_prompt("resume"), *entries])
	result = _evaluate(_stop(text, transcript), tmp_path)
	if result is None:
		return True
	assert result["decision"] == "block"
	return False


def _evaluate(payload: dict, tmp_path: Path, env: dict | None = None) -> dict | None:
	return guard.evaluate(payload, _env() if env is None else env, _marker_dir(tmp_path), NOW)


# 1. Interactive sessions are never blocked.


@pytest.mark.parametrize("payload_factory", [lambda: _stop(SECTION_2_QUESTION), _ask])
def test_no_remote_session_id_is_never_blocked(tmp_path, payload_factory):
	_mark(tmp_path)
	assert _evaluate(payload_factory(), tmp_path, env=_env(None)) is None


@pytest.mark.parametrize("payload_factory", [lambda: _stop(SECTION_2_QUESTION), _ask])
def test_unmarked_session_is_never_blocked(tmp_path, payload_factory):
	assert _evaluate(payload_factory(), tmp_path) is None


@pytest.mark.parametrize("payload_factory", [lambda: _stop(SECTION_2_QUESTION), _ask])
def test_another_sessions_marker_does_not_apply(tmp_path, payload_factory):
	_mark(tmp_path, session="cse_01OTHERSESSION")
	assert _evaluate(payload_factory(), tmp_path) is None


def test_marker_with_mismatched_session_field_is_ignored(tmp_path):
	directory = _marker_dir(tmp_path)
	directory.mkdir(parents=True)
	guard.marker_path(SESSION, directory).write_text(json.dumps({"version": 1, "session": "cse_other"}), encoding="utf-8")
	assert _evaluate(_stop(SECTION_2_QUESTION), tmp_path) is None


def test_unrelated_events_and_tools_are_ignored(tmp_path):
	_mark(tmp_path)
	assert _evaluate({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}, tmp_path) is None
	assert _evaluate({"hook_event_name": "SubagentStop", "last_assistant_message": SECTION_2_QUESTION}, tmp_path) is None
	assert _evaluate({"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion"}, tmp_path) is None


# 2. Stop in a marked session.


def test_issue_mode_session_ending_on_a_section_2_question_is_blocked(tmp_path):
	_mark(tmp_path)
	result = _evaluate(_stop(SECTION_2_QUESTION), tmp_path)
	assert result is not None and result["decision"] == "block"
	reason = result["reason"]
	assert "§28" in reason and "RECOMMENDED" in reason and "AD-<n>" in reason
	assert "<!-- ai:claude-blocked:v1 -->" in reason and "ai:claude-blocked" in reason
	assert "#4911" in reason and "shubhodeep1/coding-workflows#4911" in reason
	assert "mcp__github__add_issue_comment" in reason and "dispatch_workflow.py" in reason
	assert "block 1 of 2" in reason


def test_inline_q_a_b_c_is_blocked(tmp_path):
	_mark(tmp_path)
	result = _evaluate(_stop(INLINE_QUESTION), tmp_path)
	assert result is not None and result["decision"] == "block"


@pytest.mark.parametrize("text", PERMISSION_ASKS)
def test_permission_asks_are_blocked(tmp_path, text):
	_mark(tmp_path)
	result = _evaluate(_stop(text), tmp_path)
	assert result is not None and result["decision"] == "block"
	assert guard.KIND_PERMISSION in result["reason"]


@pytest.mark.parametrize(
	"text",
	[
		ORDINARY_REPORT,
		"",
		"Phase 1 pushed as PR #4970; the checker takes it from here.",
		"Q-values are fine. See the queue items: A, B.",
	],
)
def test_ordinary_final_messages_are_allowed(tmp_path, text):
	_mark(tmp_path)
	assert _evaluate(_stop(text), tmp_path) is None


def test_question_without_lettered_choices_is_allowed(tmp_path):
	_mark(tmp_path)
	assert _evaluate(_stop("Q1: done, nothing else to decide."), tmp_path) is None


def test_session_that_posted_the_blocked_comment_via_mcp_is_allowed(tmp_path):
	_mark(tmp_path)
	transcript = _write_transcript(
		tmp_path / "t.jsonl",
		[
			_prompt("/implement-plan-claude docs/plans/x-plan.md — resume."),
			_mcp_comment("toolu_1"),
			_tool_result("toolu_1", content=COMMENT_RESULT),
			*_verified_label_entries(),
			_assistant_text(SECTION_2_QUESTION),
		],
	)
	assert _evaluate(_stop(SECTION_2_QUESTION, transcript), tmp_path) is None


def test_session_that_posted_the_blocked_comment_via_bash_is_allowed(tmp_path):
	_mark(tmp_path)
	transcript = _write_transcript(
		tmp_path / "t.jsonl",
		[
			_prompt("resume"),
			_bash("toolu_2", GH_COMMENT_COMMAND),
			_tool_result("toolu_2", content=COMMENT_RESULT),
			_bash("toolu_2l", GH_LABEL_COMMAND),
			_tool_result("toolu_2l", content=LABEL_RESULT),
		],
	)
	assert _evaluate(_stop("awaiting permissions or fresh session", transcript), tmp_path) is None


def test_failed_blocked_comment_post_does_not_count(tmp_path):
	_mark(tmp_path)
	transcript = _write_transcript(
		tmp_path / "t.jsonl",
		[
			_prompt("resume"),
			_mcp_comment("toolu_3"),
			_tool_result("toolu_3", is_error=True, content=COMMENT_RESULT),
			*_verified_label_entries(),
		],
	)
	result = _evaluate(_stop(SECTION_2_QUESTION, transcript), tmp_path)
	assert result is not None and result["decision"] == "block"


def test_blocked_comment_from_an_earlier_turn_does_not_count(tmp_path):
	_mark(tmp_path)
	transcript = _write_transcript(
		tmp_path / "t.jsonl",
		[
			_prompt("first wake"),
			_mcp_comment("toolu_4"),
			_tool_result("toolu_4", content=COMMENT_RESULT),
			*_verified_label_entries(),
			_assistant_text("Posted the blocker."),
			_prompt("/reclarify wake"),
			_assistant_text(SECTION_2_QUESTION),
		],
	)
	result = _evaluate(_stop(SECTION_2_QUESTION, transcript), tmp_path)
	assert result is not None and result["decision"] == "block"


def test_meta_entries_do_not_start_a_new_turn(tmp_path):
	_mark(tmp_path)
	transcript = _write_transcript(
		tmp_path / "t.jsonl",
		[
			_prompt("/implement-issue-claude https://github.com/o/r/issues/4911"),
			_mcp_comment("toolu_5"),
			_tool_result("toolu_5", content=COMMENT_RESULT),
			*_verified_label_entries(),
			{"type": "user", "isMeta": True, "message": {"role": "user", "content": [{"type": "text", "text": "skill body"}]}},
		],
	)
	assert _evaluate(_stop(SECTION_2_QUESTION, transcript), tmp_path) is None


# Issue #5082: only a verified blocker on the marker's own issue counts.


def test_echo_of_the_marker_does_not_count(tmp_path):
	_mark(tmp_path)
	entries = [
		_bash("toolu_e", "echo '" + BLOCKED_BODY + "'"),
		_tool_result("toolu_e", content=BLOCKED_BODY),
		*_verified_label_entries(),
	]
	assert not _allowed_after(tmp_path, entries)


@pytest.mark.parametrize(
	("owner", "repo", "issue"),
	[
		(MARKED_OWNER, MARKED_REPO, 4912),
		(MARKED_OWNER, "other-repo", MARKED_ISSUE),
		("someone-else", MARKED_REPO, MARKED_ISSUE),
		(MARKED_OWNER, MARKED_REPO, "4911x"),
		(MARKED_OWNER, MARKED_REPO, True),
	],
)
def test_comment_on_another_issue_or_repo_does_not_count(tmp_path, owner, repo, issue):
	_mark(tmp_path)
	other_url = f"https://github.com/{owner}/{repo}/issues/{issue}#issuecomment-1"
	entries = [
		_mcp_comment("toolu_o", owner=owner, repo=repo, issue=issue),
		_tool_result("toolu_o", content=json.dumps({"html_url": other_url})),
		*_verified_label_entries(),
	]
	assert not _allowed_after(tmp_path, entries)


def test_mcp_comment_without_owner_and_repo_does_not_count(tmp_path):
	_mark(tmp_path)
	entries = [
		_tool_use("toolu_n", "mcp__github__add_issue_comment", {"issue_number": MARKED_ISSUE, "body": BLOCKED_BODY}),
		_tool_result("toolu_n", content=COMMENT_RESULT),
		*_verified_label_entries(),
	]
	assert not _allowed_after(tmp_path, entries)


def test_bash_comment_on_another_repo_does_not_count(tmp_path):
	_mark(tmp_path)
	entries = [
		_bash("toolu_b", "gh api repos/o/r/issues/4911/comments -f body='" + BLOCKED_BODY + "'"),
		_tool_result("toolu_b", content=json.dumps({"html_url": "https://github.com/o/r/issues/4911#issuecomment-1"})),
		*_verified_label_entries(),
	]
	assert not _allowed_after(tmp_path, entries)


def test_marker_not_at_the_start_of_the_body_does_not_count(tmp_path):
	_mark(tmp_path)
	entries = [
		_mcp_comment("toolu_q", body="Quoting the blocker format: " + BLOCKED_BODY),
		_tool_result("toolu_q", content=COMMENT_RESULT),
		*_verified_label_entries(),
	]
	assert not _allowed_after(tmp_path, entries)


def test_leading_whitespace_before_the_marker_counts(tmp_path):
	_mark(tmp_path)
	entries = [
		_mcp_comment("toolu_w", body="\n  " + BLOCKED_BODY),
		_tool_result("toolu_w", content=COMMENT_RESULT),
		*_verified_label_entries(),
	]
	assert _allowed_after(tmp_path, entries)


def test_comment_without_the_label_does_not_count(tmp_path):
	_mark(tmp_path)
	entries = [_mcp_comment("toolu_c"), _tool_result("toolu_c", content=COMMENT_RESULT)]
	assert not _allowed_after(tmp_path, entries)


def test_label_without_the_comment_does_not_count(tmp_path):
	_mark(tmp_path)
	assert not _allowed_after(tmp_path, _verified_label_entries())


@pytest.mark.parametrize(
	"label_entries",
	[
		[_mcp_label("toolu_l", issue=4912), _tool_result("toolu_l", content=LABEL_RESULT)],
		[_mcp_label("toolu_l", repo="other-repo"), _tool_result("toolu_l", content=LABEL_RESULT)],
		[_mcp_label("toolu_l", labels=["ai:claude"]), _tool_result("toolu_l", content=LABEL_RESULT)],
		[_mcp_label("toolu_l"), _tool_result("toolu_l", is_error=True, content="403")],
		[_mcp_label("toolu_l")],
		[_mcp_label("toolu_l", method="create"), _tool_result("toolu_l", content=LABEL_RESULT)],
		[_mcp_label("toolu_l", method=None), _tool_result("toolu_l", content=LABEL_RESULT)],
		[_mcp_label("toolu_l", method="delete"), _tool_result("toolu_l", content=LABEL_RESULT)],
		[_bash("toolu_l", "gh api repos/shubhodeep1/coding-workflows/issues/4911/labels -f 'labels[]=ai:claude'"), _tool_result("toolu_l", content=LABEL_RESULT)],
		[_bash("toolu_l", "gh api repos/shubhodeep1/coding-workflows/issues/4912/labels -f 'labels[]=ai:claude-blocked'"), _tool_result("toolu_l", content=LABEL_RESULT)],
		[_bash("toolu_l", "gh api -X DELETE repos/shubhodeep1/coding-workflows/issues/4911/labels -f 'labels[]=ai:claude-blocked'"), _tool_result("toolu_l", content="[]")],
	],
)
def test_label_write_must_add_the_label_to_the_same_issue(tmp_path, label_entries):
	_mark(tmp_path)
	entries = [_mcp_comment("toolu_c"), _tool_result("toolu_c", content=COMMENT_RESULT), *label_entries]
	assert not _allowed_after(tmp_path, entries)


@pytest.mark.parametrize(
	"result_content",
	[
		"ok",
		"5885870678",
		json.dumps({"html_url": "https://github.com/shubhodeep1/coding-workflows/issues/49110#issuecomment-1"}),
		json.dumps({"issue_url": "https://api.github.com/repos/shubhodeep1/coding-workflows/issues/49110"}),
		json.dumps({"html_url": "https://github.com/shubhodeep1/coding-workflows-fork/issues/4911#issuecomment-1"}),
		"",
	],
)
def test_result_without_the_issue_comment_url_does_not_count(tmp_path, result_content):
	_mark(tmp_path)
	entries = [_mcp_comment("toolu_r"), _tool_result("toolu_r", content=result_content), *_verified_label_entries()]
	assert not _allowed_after(tmp_path, entries)


@pytest.mark.parametrize(
	"result_content",
	[
		COMMENT_HTML_URL,
		json.dumps({"id": 1, "url": COMMENT_HTML_URL}),
		json.dumps({"issue_url": "https://api.github.com/repos/shubhodeep1/coding-workflows/issues/4911"}),
		[{"type": "text", "text": COMMENT_RESULT}],
		COMMENT_HTML_URL.replace("shubhodeep1/coding-workflows", "Shubhodeep1/Coding-Workflows"),
	],
)
def test_result_forms_that_name_the_comment_url_count(tmp_path, result_content):
	_mark(tmp_path)
	entries = [_mcp_comment("toolu_r"), _tool_result("toolu_r", content=result_content), *_verified_label_entries()]
	assert _allowed_after(tmp_path, entries)


def test_owner_and_repo_match_case_insensitively(tmp_path):
	_mark(tmp_path)
	entries = [
		_mcp_comment("toolu_k", owner="ShubhoDeep1", repo="Coding-Workflows"),
		_tool_result("toolu_k", content=COMMENT_RESULT),
		_mcp_label("toolu_kl", owner="SHUBHODEEP1", repo="coding-workflows", issue="4911"),
		_tool_result("toolu_kl", content=LABEL_RESULT),
	]
	assert _allowed_after(tmp_path, entries)


@pytest.mark.parametrize(
	"command",
	[
		"echo hi; " + GH_COMMENT_COMMAND,
		GH_COMMENT_COMMAND + " && true",
		GH_COMMENT_COMMAND + " | head -5",
		GH_COMMENT_COMMAND + " > /tmp/out.json",
		GH_COMMENT_COMMAND + " 2>&1",
		"gh api repos/shubhodeep1/coding-workflows/issues/4911/comments -f body=\"$(cat body.md)\"",
		"gh api repos/shubhodeep1/coding-workflows/issues/4911/comments -f body=\"`cat body.md`\"",
		"gh api repos/shubhodeep1/coding-workflows/issues/4911/comments -F body=@body.md",
		"gh api repos/shubhodeep1/coding-workflows/issues/4911/comments --input body.json",
		"gh api -X GET repos/shubhodeep1/coding-workflows/issues/4911/comments -f body='" + BLOCKED_BODY + "'",
		"gh api -XGET repos/shubhodeep1/coding-workflows/issues/4911/comments -f body='" + BLOCKED_BODY + "'",
		"gh api repos/{owner}/{repo}/issues/4911/comments -f body='" + BLOCKED_BODY + "'",
		"gh api --hostname example.com repos/shubhodeep1/coding-workflows/issues/4911/comments -f body='" + BLOCKED_BODY + "'",
		"gh api repos/shubhodeep1/coding-workflows/issues/4911 -f body='" + BLOCKED_BODY + "'",
		"gh api repos/shubhodeep1/coding-workflows/issues/4911/comments extra -f body='" + BLOCKED_BODY + "'",
		"gh api repos/shubhodeep1/coding-workflows/issues/4911/comments -f body='" + BLOCKED_BODY + "' -f body='second'",
		"bash -c \"" + GH_COMMENT_COMMAND.replace("'", "") + "\"",
		"gh api repos/shubhodeep1/coding-workflows/issues/4911/comments -f 'body=unterminated",
	],
)
def test_other_bash_shapes_do_not_count(tmp_path, command):
	_mark(tmp_path)
	entries = [_bash("toolu_s", command), _tool_result("toolu_s", content=COMMENT_RESULT), *_verified_label_entries()]
	assert not _allowed_after(tmp_path, entries)


@pytest.mark.parametrize(
	"command",
	[
		GH_COMMENT_COMMAND,
		"gh api /repos/shubhodeep1/coding-workflows/issues/4911/comments -f body='" + BLOCKED_BODY + "'",
		"gh api -X POST repos/shubhodeep1/coding-workflows/issues/4911/comments --raw-field body='" + BLOCKED_BODY + "'",
		"gh api --method=post repos/shubhodeep1/coding-workflows/issues/4911/comments -F body='" + BLOCKED_BODY + "' --jq .html_url",
		'gh api repos/shubhodeep1/coding-workflows/issues/4911/comments -f body="<!-- ai:claude-blocked:v1 -->\nBlocked (see the log); cost: $5."',
		"gh api repos/shubhodeep1/coding-workflows/issues/4911/comments -f body='<!-- ai:claude-blocked:v1 -->\nQ1 (a) or (b); use `code` | pipes > here'",
	],
)
def test_accepted_bash_comment_shapes_count(tmp_path, command):
	_mark(tmp_path)
	entries = [_bash("toolu_a", command), _tool_result("toolu_a", content=COMMENT_HTML_URL), *_verified_label_entries()]
	assert _allowed_after(tmp_path, entries)


def test_and_chain_of_gh_api_comment_and_label_counts(tmp_path):
	# The shape the #5082 session itself used to post its blocker.
	_mark(tmp_path)
	command = (
		"gh api repos/shubhodeep1/coding-workflows/issues/4911/comments -X POST -f body='"
		+ BLOCKED_BODY
		+ "' --jq '[.id,.html_url]|@tsv' && gh api repos/shubhodeep1/coding-workflows/issues/4911/labels -X POST "
		"-f 'labels[]=ai:claude-blocked' --jq '[.[].name]|join(\",\")'"
	)
	entries = [
		_bash("toolu_ch", command),
		_tool_result("toolu_ch", content="5885870678\t" + COMMENT_HTML_URL + "\nai:security,ai:claude,ai:claude-blocked"),
	]
	assert _allowed_after(tmp_path, entries)


@pytest.mark.parametrize("prefix", ["cd /home/user/coding-workflows; ", "cd ~/coding-workflows && ", "  cd repo;"])
def test_leading_cd_prefix_is_allowed(tmp_path, prefix):
	_mark(tmp_path)
	entries = [
		_bash("toolu_cd", prefix + GH_COMMENT_COMMAND + " && " + GH_LABEL_COMMAND),
		_tool_result("toolu_cd", content=COMMENT_RESULT + "\n" + LABEL_RESULT),
	]
	assert _allowed_after(tmp_path, entries)


@pytest.mark.parametrize(
	"command",
	[
		"echo '" + BLOCKED_BODY + "' && " + GH_LABEL_COMMAND,
		GH_COMMENT_COMMAND + " && echo done",
		GH_COMMENT_COMMAND + " & " + GH_LABEL_COMMAND,
		GH_COMMENT_COMMAND + " || " + GH_LABEL_COMMAND,
		GH_COMMENT_COMMAND + " ; " + GH_LABEL_COMMAND,
		"cd /tmp && echo x && " + GH_COMMENT_COMMAND + " && " + GH_LABEL_COMMAND,
		"cd $(pwd); " + GH_COMMENT_COMMAND + " && " + GH_LABEL_COMMAND,
		"cd '/tmp'; echo hi; " + GH_COMMENT_COMMAND + " && " + GH_LABEL_COMMAND,
		GH_COMMENT_COMMAND + " && cd /tmp",
	],
)
def test_and_chain_with_anything_but_gh_api_does_not_count(tmp_path, command):
	_mark(tmp_path)
	entries = [_bash("toolu_cx", command), _tool_result("toolu_cx", content=COMMENT_RESULT + "\n" + LABEL_RESULT)]
	assert not _allowed_after(tmp_path, entries)


def test_bash_label_write_counts(tmp_path):
	_mark(tmp_path)
	entries = [
		_mcp_comment("toolu_c"),
		_tool_result("toolu_c", content=COMMENT_RESULT),
		_bash("toolu_bl", "gh api -X POST repos/shubhodeep1/coding-workflows/issues/4911/labels -f 'labels[]=ai:claude-blocked'"),
		_tool_result("toolu_bl", content=LABEL_RESULT),
	]
	assert _allowed_after(tmp_path, entries)


def test_other_mcp_tools_carrying_the_marker_do_not_count(tmp_path):
	_mark(tmp_path)
	entries = [
		_tool_use("toolu_u", "mcp__github__update_issue_comment", {"owner": MARKED_OWNER, "repo": MARKED_REPO, "comment_id": 1, "body": BLOCKED_BODY}),
		_tool_result("toolu_u", content=COMMENT_RESULT),
		_tool_use("toolu_x", "Write", {"file_path": "/tmp/blocked.md", "content": BLOCKED_BODY}),
		_tool_result("toolu_x", content=COMMENT_RESULT),
		*_verified_label_entries(),
	]
	assert not _allowed_after(tmp_path, entries)


def test_mismatched_tool_result_id_does_not_count(tmp_path):
	_mark(tmp_path)
	entries = [_mcp_comment("toolu_m"), _tool_result("toolu_other", content=COMMENT_RESULT), *_verified_label_entries()]
	assert not _allowed_after(tmp_path, entries)


def test_malformed_marker_target_never_counts(tmp_path):
	directory = _marker_dir(tmp_path)
	guard.write_marker("shubhodeep1/coding-workflows", 4911, SESSION, directory, NOW)
	marker = guard.read_marker(SESSION, directory)
	turn = [_mcp_comment("toolu_c"), _tool_result("toolu_c", content=COMMENT_RESULT), *_verified_label_entries()]
	assert guard.blocked_comment_posted(turn, marker) is True
	assert guard.blocked_comment_posted(turn, {**marker, "repo": "not a repo"}) is False
	assert guard.blocked_comment_posted(turn, {**marker, "issue": None}) is False


def test_block_reason_names_the_issue_and_the_label_requirement(tmp_path):
	_mark(tmp_path)
	result = _evaluate(_stop(SECTION_2_QUESTION), tmp_path)
	assert result is not None and result["decision"] == "block"
	reason = result["reason"]
	assert "shubhodeep1/coding-workflows#4911" in reason
	assert "gh api repos/shubhodeep1/coding-workflows/issues/4911/comments" in reason
	assert "labels[]=ai:claude-blocked" in reason
	assert "does not count" in reason


def test_final_text_falls_back_to_the_transcript(tmp_path):
	_mark(tmp_path)
	transcript = _write_transcript(
		tmp_path / "t.jsonl",
		[_prompt("resume"), _tool_use("toolu_6", "Bash", {"command": "ls"}), _tool_result("toolu_6"), _assistant_text(SECTION_2_QUESTION)],
	)
	result = _evaluate(_stop(None, transcript), tmp_path)
	assert result is not None and result["decision"] == "block"
	allowed = _write_transcript(tmp_path / "ok.jsonl", [_prompt("resume"), _assistant_text(ORDINARY_REPORT)])
	assert _evaluate(_stop(None, allowed), tmp_path) is None


def test_unreadable_transcript_still_blocks_a_question(tmp_path):
	_mark(tmp_path)
	result = _evaluate(_stop(SECTION_2_QUESTION, tmp_path / "missing.jsonl"), tmp_path)
	assert result is not None and result["decision"] == "block"


def test_cap_allows_the_third_stop_with_a_structured_line(tmp_path):
	directory = _mark(tmp_path)
	first = _evaluate(_stop(SECTION_2_QUESTION), tmp_path)
	second = _evaluate(_stop(INLINE_QUESTION), tmp_path)
	third = _evaluate(_stop(SECTION_2_QUESTION), tmp_path)
	assert first["decision"] == "block" and "block 1 of 2" in first["reason"]
	assert second["decision"] == "block" and "block 2 of 2" in second["reason"]
	assert "decision" not in third
	assert third["systemMessage"].startswith(guard.CAP_MESSAGE_PREFIX)
	assert SESSION in third["systemMessage"] and "shubhodeep1/coding-workflows#4911" in third["systemMessage"]
	lines = (directory / guard.CAP_LOG_NAME).read_text(encoding="utf-8").splitlines()
	record = json.loads(lines[-1])
	assert record["event"] == "cap_reached" and record["session"] == SESSION and record["issue"] == 4911 and record["blocks"] == 2


def test_allowed_stops_do_not_count_toward_the_cap(tmp_path):
	_mark(tmp_path)
	for _ in range(3):
		assert _evaluate(_stop(ORDINARY_REPORT), tmp_path) is None
	result = _evaluate(_stop(SECTION_2_QUESTION), tmp_path)
	assert result["decision"] == "block" and "block 1 of 2" in result["reason"]


def test_cap_is_per_session(tmp_path):
	_mark(tmp_path)
	other = "cse_01SECONDSESSION"
	_mark(tmp_path, session=other)
	for _ in range(2):
		_evaluate(_stop(SECTION_2_QUESTION), tmp_path)
	result = _evaluate(_stop(SECTION_2_QUESTION), tmp_path, env=_env(other))
	assert result["decision"] == "block" and "block 1 of 2" in result["reason"]


# 3. AskUserQuestion.


def test_ask_user_question_is_denied_in_a_marked_session(tmp_path):
	_mark(tmp_path)
	result = _evaluate(_ask(), tmp_path)
	output = result["hookSpecificOutput"]
	assert output["hookEventName"] == "PreToolUse" and output["permissionDecision"] == "deny"
	reason = output["permissionDecisionReason"]
	assert "AskUserQuestion is denied" in reason and "RECOMMENDED" in reason and "<!-- ai:claude-blocked:v1 -->" in reason


def test_ask_user_question_denial_is_not_capped(tmp_path):
	_mark(tmp_path)
	for _ in range(5):
		assert _evaluate(_ask(), tmp_path)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_ask_user_question_is_allowed_interactively(tmp_path):
	assert _evaluate(_ask(), tmp_path) is None
	_mark(tmp_path)
	assert _evaluate(_ask(), tmp_path, env=_env(None)) is None


# 4. Fail open.


def _run_hook(stdin_text: str, home: Path, session: str | None = SESSION) -> subprocess.CompletedProcess:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["HOME"] = str(home)
	env.pop(guard.ENV_SESSION, None)
	if session is not None:
		env[guard.ENV_SESSION] = session
	return subprocess.run(
		[sys.executable, str(TEMPLATE_HOOK_PATH)],
		input=stdin_text,
		capture_output=True,
		text=True,
		env=env,
		check=False,
	)


def _home_with_marker(tmp_path: Path) -> Path:
	home = tmp_path / "home"
	guard.write_marker("shubhodeep1/coding-workflows", 4911, SESSION, home.joinpath(*guard.MARKER_DIR_PARTS), NOW)
	return home


def test_hook_process_blocks_a_question_in_a_marked_session(tmp_path):
	home = _home_with_marker(tmp_path)
	proc = _run_hook(json.dumps(_stop(SECTION_2_QUESTION)), home)
	assert proc.returncode == 0
	assert json.loads(proc.stdout)["decision"] == "block"


def test_hook_process_denies_ask_in_a_marked_session(tmp_path):
	home = _home_with_marker(tmp_path)
	proc = _run_hook(json.dumps(_ask()), home)
	assert proc.returncode == 0
	assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_process_is_silent_interactively(tmp_path):
	home = _home_with_marker(tmp_path)
	for payload in (_stop(SECTION_2_QUESTION), _ask()):
		proc = _run_hook(json.dumps(payload), home, session=None)
		assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""


@pytest.mark.parametrize("stdin_text", ["{not json", "[]", "42", '"text"'])
def test_malformed_payload_allows_with_warning(tmp_path, stdin_text):
	proc = _run_hook(stdin_text, _home_with_marker(tmp_path))
	assert proc.returncode == 0
	assert "Unattended question guard skipped" in json.loads(proc.stdout)["systemMessage"]


@pytest.mark.parametrize("stdin_text", ["", "   \n"])
def test_empty_payload_allows_silently(tmp_path, stdin_text):
	proc = _run_hook(stdin_text, _home_with_marker(tmp_path))
	assert proc.returncode == 0 and proc.stdout == ""


def test_unreadable_marker_allows_with_warning(tmp_path):
	directory = _marker_dir(tmp_path)
	directory.mkdir(parents=True)
	guard.marker_path(SESSION, directory).write_text("{broken", encoding="utf-8")
	for payload in (_stop(SECTION_2_QUESTION), _ask()):
		result = _evaluate(payload, tmp_path)
		assert "decision" not in result and "hookSpecificOutput" not in result
		assert "Unattended question guard skipped" in result["systemMessage"]


def test_unreadable_state_allows_with_warning(tmp_path):
	directory = _mark(tmp_path)
	guard.state_path(SESSION, directory).write_text("{broken", encoding="utf-8")
	result = _evaluate(_stop(SECTION_2_QUESTION), tmp_path)
	assert "decision" not in result and "Unattended question guard skipped" in result["systemMessage"]


def test_unwritable_state_allows_with_warning(tmp_path, monkeypatch):
	_mark(tmp_path)

	def _fail(*_args, **_kwargs):
		raise guard.GuardStateError("state could not be written (read-only)")

	monkeypatch.setattr(guard, "write_blocks", _fail)
	result = _evaluate(_stop(SECTION_2_QUESTION), tmp_path)
	assert "decision" not in result and "read-only" in result["systemMessage"]


def test_internal_exception_allows_with_warning(monkeypatch, capsys):
	def _boom(*_args, **_kwargs):
		raise RuntimeError("boom")

	monkeypatch.setattr(guard, "evaluate", _boom)
	assert guard.run_hook(io.StringIO(json.dumps(_stop(SECTION_2_QUESTION)))) == 0
	assert "internal error (boom)" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_stdin_read_error_allows_with_warning(capsys):
	class _Broken:
		def read(self):
			raise OSError("closed")

	assert guard.run_hook(_Broken()) == 0
	assert "could not read" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_hook_makes_no_network_calls():
	source = TEMPLATE_HOOK_PATH.read_text(encoding="utf-8")
	# The hook parses `gh api` commands from the transcript as data (issue #5082),
	# so the text itself may appear; nothing that runs a process or opens a socket may.
	for forbidden in ("subprocess", "urllib", "http.client", "requests", "socket", "os.system", "popen", "os.exec"):
		assert forbidden not in source, forbidden


# `mark` subcommand.


def _run_mark(args: list[str], home: Path, session: str | None) -> subprocess.CompletedProcess:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["HOME"] = str(home)
	env.pop(guard.ENV_SESSION, None)
	if session is not None:
		env[guard.ENV_SESSION] = session
	return subprocess.run([sys.executable, str(TEMPLATE_HOOK_PATH), *args], capture_output=True, text=True, env=env, check=False)


def test_mark_writes_the_marker_for_this_session(tmp_path):
	proc = _run_mark(["mark", "--repo", "shubhodeep1/coding-workflows", "--issue", "4911"], tmp_path, SESSION)
	assert proc.returncode == 0
	out = json.loads(proc.stdout)
	assert out["marked"] is True and out["session"] == SESSION and out["issue"] == 4911
	marker = json.loads((tmp_path / ".claude" / "unattended-issue-mode" / f"{SESSION}.json").read_text(encoding="utf-8"))
	assert marker == {"version": 1, "session": SESSION, "repo": "shubhodeep1/coding-workflows", "issue": 4911, "marked_at": marker["marked_at"]}


def test_mark_is_a_no_op_outside_a_cloud_session(tmp_path):
	proc = _run_mark(["mark", "--repo", "shubhodeep1/coding-workflows", "--issue", "4911"], tmp_path, None)
	assert proc.returncode == 0 and json.loads(proc.stdout)["marked"] is False
	assert not (tmp_path / ".claude" / "unattended-issue-mode").exists()


@pytest.mark.parametrize("args", [["--repo", "not a repo", "--issue", "4911"], ["--repo", "o/r", "--issue", "0"]])
def test_mark_rejects_bad_arguments(tmp_path, args):
	proc = _run_mark(["mark", *args], tmp_path, SESSION)
	assert proc.returncode == 1 and json.loads(proc.stdout)["marked"] is False


def test_mark_does_not_reset_the_cap(tmp_path):
	directory = _mark(tmp_path)
	guard.write_blocks(SESSION, directory, 2)
	_mark(tmp_path)
	assert guard.read_blocks(SESSION, directory) == 2


def test_unknown_subcommand_fails(tmp_path):
	proc = _run_mark(["unmark"], tmp_path, SESSION)
	assert proc.returncode == 1


# Wiring, parity, and prose.


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_settings_wire_the_hook_on_stop_and_ask_user_question(path):
	settings = json.loads(path.read_text(encoding="utf-8"))
	hooks = settings["hooks"]
	stop_commands = [hook["command"] for entry in hooks[guard.SETTINGS_STOP_EVENT] for hook in entry["hooks"]]
	assert stop_commands.count(HOOK_COMMAND) == 1
	ask_entries = [entry for entry in hooks["PreToolUse"] if entry.get("matcher") == guard.SETTINGS_ASK_MATCHER]
	assert len(ask_entries) == 1
	assert [hook["command"] for hook in ask_entries[0]["hooks"]] == [HOOK_COMMAND]
	other_pre = [hook["command"] for entry in hooks["PreToolUse"] if entry is not ask_entries[0] for hook in entry["hooks"]]
	assert HOOK_COMMAND not in other_pre
	allow = settings["permissions"]["allow"]
	assert "Bash(python3 .claude/hooks/unattended_question_guard.py mark *)" in allow
	assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/hooks/unattended_question_guard.py mark *)" in allow


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_existing_hook_wiring_is_untouched(path):
	text = path.read_text(encoding="utf-8")
	for hook in ("pr_merge_status_guard.py", "gh_api_write_guard.py", "pr_watch_guard.py", "pr_check_in_reminder.py", "permission_prompt_logger.py", "session-start.sh"):
		assert hook in text, hook


@pytest.mark.parametrize(
	"relative",
	[
		"hooks/unattended_question_guard.py",
		"settings.json",
		"commands/implement-issue-claude.md",
		"commands/implement-plan-claude.md",
		"commands/seed-repo.md",
	],
)
def test_template_parity(relative):
	live = REPO_ROOT / ".claude" / relative
	template = REPO_ROOT / "workflow-templates" / ".claude" / relative
	assert live.read_bytes() == template.read_bytes(), relative


def test_claude_md_documents_the_enforcement():
	text = CLAUDE_MD.read_text(encoding="utf-8")
	section = text[text.index("### G) Enforcement: No Turn Ends on an Unread Question") : text.index("## FINAL REMINDER")]
	for needle in (
		".claude/hooks/unattended_question_guard.py",
		"`Stop`",
		"`AskUserQuestion`",
		"unattended_question_guard.py mark --repo <owner>/<repo> --issue <N>",
		"CLAUDE_CODE_REMOTE_SESSION_ID",
		"<!-- ai:claude-blocked:v1 -->",
		"At most 2 blocks per session",
		guard.CAP_MESSAGE_PREFIX,
		"tests/test_unattended_question_guard.py",
	):
		assert needle in section, needle
	assert guard.STOP_BLOCK_CAP == 2


def test_issue_command_marks_the_session_first():
	text = (TEMPLATE_COMMANDS / "implement-issue-claude.md").read_text(encoding="utf-8")
	step0 = text[text.index("0. **Session preflight.**") : text.index("1. **Resolve and read the issue.**")]
	mark_at = step0.index("unattended_question_guard.py mark --repo <owner>/<repo> --issue <N>")
	assert mark_at < step0.index("Then call `get_session`")


def test_plan_command_marks_issue_mode_sessions():
	text = (TEMPLATE_COMMANDS / "implement-plan-claude.md").read_text(encoding="utf-8")
	step1 = text[text.index("1. **Resolve the plan doc.**") : text.index("2. **Pick the mode")]
	assert "Source issue: <owner>/<repo>#<N>" in step1
	assert "unattended_question_guard.py mark --repo <owner>/<repo> --issue <N>" in step1
	issue_mode = text[text.index("## Issue Mode") : text.index("## Stage Sessions")]
	assert "**Unattended question guard** (CLAUDE.md §28.G)" in issue_mode
	guard_bullet = issue_mode[issue_mode.index("**Unattended question guard**") : issue_mode.index("- **Stops are reported on the issue**")]
	assert "`<!-- ai:claude-blocked:v1 -->` comment, whose result carries its URL" in guard_bullet
	assert f"the `{guard.BLOCKED_LABEL}` label on that same issue" in guard_bullet


def test_seed_repo_command_ships_the_hook():
	text = (TEMPLATE_COMMANDS / "seed-repo.md").read_text(encoding="utf-8")
	assert "`hooks/unattended_question_guard.py`" in text


def test_ci_runs_this_file():
	assert "tests/test_unattended_question_guard.py" in CI_WORKFLOW.read_text(encoding="utf-8")

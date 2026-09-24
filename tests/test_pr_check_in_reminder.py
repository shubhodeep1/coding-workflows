#!/usr/bin/env python3
"""Behaviour and wiring contract for the PR status check-in reminder (CLAUDE.md §26).

Covers the pieces that can silently detach the mechanism:
  1. The rule — every MCP PR-creation / remote-write tool and every pushing
     Bash command yields the §26 reminder; everything else stays silent.
  2. The fail-open contract for malformed payloads.
  3. The settings.json matcher, template parity, and the CLAUDE.md prose.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "pr_check_in_reminder.py"
TEMPLATE_HOOK_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "pr_check_in_reminder.py"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
TEMPLATE_SETTINGS_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
SEED_REPO_COMMAND = REPO_ROOT / ".claude" / "commands" / "seed-repo.md"
TEMPLATE_SEED_REPO_COMMAND = REPO_ROOT / "workflow-templates" / ".claude" / "commands" / "seed-repo.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PR_WRITE_TOOLS = [
	"mcp__github__create_pull_request",
	"mcp__github__push_files",
	"mcp__github__create_or_update_file",
	"mcp__some_future_server__create_pull_request",
	"create_pull_request",
]

SILENT_TOOLS = [
	"mcp__github__pull_request_read",
	"mcp__github__update_pull_request",
	"mcp__github__unsubscribe_pr_activity",
	"mcp__Claude_Code_Remote__send_later",
	"mcp__github__create_pull_request_with_copilot",
	"mcp__github__create_branch",
	"Edit",
	"Read",
	"Agent",
]

PUSH_COMMANDS = [
	"git push -u origin claude/feature",
	"git push",
	"git push --force-with-lease origin claude/feature",
	"cd /repo && git add -A && git commit -m x && git push -u origin claude/feature",
	"git -C /repo push origin HEAD",
	"gh pr create --title t --body b -R owner/repo",
	"git push origin main; echo done",
]

SILENT_COMMANDS = [
	"git status --short",
	"git fetch origin main",
	"git pull origin main",
	"git push --dry-run origin claude/feature",
	"gh pr view 12 -R owner/repo",
	"gh api repos/owner/repo/pulls/12",
	"echo 'git push' > notes.txt",
	"python3 -m pytest -q tests/test_pushdown.py",
	"ls",
	"",
]


def _load_hook():
	spec = importlib.util.spec_from_file_location("pr_check_in_reminder", HOOK_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


hook = _load_hook()


def _run_hook(stdin_text: str) -> subprocess.CompletedProcess:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		[sys.executable, str(HOOK_PATH)],
		input=stdin_text,
		capture_output=True,
		text=True,
		env=env,
		check=False,
	)


# ──────────────────────────────────────────────────────────────────
# The rule
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool_name", PR_WRITE_TOOLS)
def test_pr_write_tools_are_recognised(tool_name):
	assert hook.is_pr_write_tool(tool_name)


@pytest.mark.parametrize("tool_name", SILENT_TOOLS)
def test_other_tools_are_not_recognised(tool_name):
	assert not hook.is_pr_write_tool(tool_name)


@pytest.mark.parametrize("tool_name", [None, 42, ["mcp__github__create_pull_request"], {}])
def test_non_string_tool_name_is_not_recognised(tool_name):
	assert not hook.is_pr_write_tool(tool_name)


@pytest.mark.parametrize("command", PUSH_COMMANDS)
def test_push_commands_are_recognised(command):
	assert hook.is_push_command(command)


@pytest.mark.parametrize("command", SILENT_COMMANDS)
def test_other_commands_are_not_recognised(command):
	assert not hook.is_push_command(command)


@pytest.mark.parametrize("command", [None, 42, ["git push"], {"command": "git push"}])
def test_non_string_command_is_not_recognised(command):
	assert not hook.is_push_command(command)


@pytest.mark.parametrize("tool_name", PR_WRITE_TOOLS)
def test_evaluate_reminds_after_pr_write_tools(tool_name):
	context = hook.evaluate({"tool_name": tool_name, "tool_input": {"owner": "o", "repo": "r"}})
	assert context == hook.REMINDER
	assert "§26" in context
	assert "send_later" in context
	assert "delay_minutes=180" in context
	assert "Sonnet" in context
	assert "create_session" in context
	assert "Never subscribe" in context


@pytest.mark.parametrize("command", PUSH_COMMANDS)
def test_evaluate_reminds_after_push_commands(command):
	assert hook.evaluate({"tool_name": "Bash", "tool_input": {"command": command}}) == hook.REMINDER


@pytest.mark.parametrize("tool_name", SILENT_TOOLS)
def test_evaluate_stays_silent_for_other_tools(tool_name):
	assert hook.evaluate({"tool_name": tool_name, "tool_input": {"command": "git push"}}) == ""


@pytest.mark.parametrize("command", SILENT_COMMANDS)
def test_evaluate_stays_silent_for_other_commands(command):
	assert hook.evaluate({"tool_name": "Bash", "tool_input": {"command": command}}) == ""


def test_evaluate_stays_silent_for_bash_without_command():
	assert hook.evaluate({"tool_name": "Bash"}) == ""
	assert hook.evaluate({"tool_name": "Bash", "tool_input": "git push"}) == ""
	assert hook.evaluate({"tool_name": "Bash", "tool_input": {}}) == ""


def test_evaluate_stays_silent_for_payload_without_tool_name():
	assert hook.evaluate({}) == ""
	assert hook.evaluate({"tool_input": {"command": "git push"}}) == ""


def test_reminder_never_names_a_subscription_as_the_mechanism():
	"""§25 stays in force: the reminder must point at send_later, not subscribe_pr_activity."""
	assert "subscribe_pr_activity" not in hook.REMINDER
	assert re.search(r"\bsend_later\b", hook.REMINDER)


# ──────────────────────────────────────────────────────────────────
# End to end through the hook protocol
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
	"payload",
	[
		{"hook_event_name": "PostToolUse", "tool_name": "mcp__github__create_pull_request", "tool_input": {"owner": "o", "repo": "r", "title": "t"}, "tool_response": {"number": 7}},
		{"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "git push -u origin claude/feature"}, "tool_response": {"stdout": ""}},
	],
)
def test_hook_process_emits_additional_context(payload):
	result = _run_hook(json.dumps(payload))
	assert result.returncode == 0
	assert result.stderr == ""
	emitted = json.loads(result.stdout)
	assert emitted == {
		"hookSpecificOutput": {
			"hookEventName": "PostToolUse",
			"additionalContext": hook.REMINDER,
		}
	}


@pytest.mark.parametrize(
	"payload",
	[
		{"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "git status"}},
		{"hook_event_name": "PostToolUse", "tool_name": "mcp__github__pull_request_read", "tool_input": {}},
	],
)
def test_hook_process_stays_silent_for_other_calls(payload):
	result = _run_hook(json.dumps(payload))
	assert result.returncode == 0
	assert result.stdout == ""
	assert result.stderr == ""


def test_hook_never_blocks():
	"""A PostToolUse hook runs after the tool acted; the reminder must never exit 2 or emit a decision."""
	result = _run_hook(json.dumps({"tool_name": "mcp__github__create_pull_request", "tool_input": {}}))
	assert result.returncode == 0
	emitted = json.loads(result.stdout)
	assert "decision" not in emitted
	assert "permissionDecision" not in emitted.get("hookSpecificOutput", {})


def test_hook_has_no_environment_escape_hatch_and_no_api_calls():
	"""§26.E: no env var switches the reminder off, and the hook issues no API calls."""
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["CLAUDE_PR_MERGE_GUARD"] = "off"
	env["CLAUDE_PR_CHECK_IN_REMINDER"] = "off"
	result = subprocess.run(
		[sys.executable, str(HOOK_PATH)],
		input=json.dumps({"tool_name": "mcp__github__create_pull_request", "tool_input": {}}),
		capture_output=True,
		text=True,
		env=env,
		check=False,
	)
	assert result.returncode == 0
	assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == hook.REMINDER
	source = HOOK_PATH.read_text(encoding="utf-8")
	assert "os.environ" not in source
	assert "getenv" not in source
	assert "subprocess" not in source
	assert "urllib" not in source
	assert "requests" not in source


# ──────────────────────────────────────────────────────────────────
# Fail-open contract
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stdin_text", ["", "   ", "not json", "[1, 2, 3]", '"a string"'])
def test_malformed_payload_allows_with_warning(stdin_text):
	result = _run_hook(stdin_text)
	assert result.returncode == 0
	assert result.stderr == ""
	if stdin_text.strip():
		emitted = json.loads(result.stdout)
		assert emitted["systemMessage"].startswith("PR check-in reminder skipped:")
	else:
		# An empty payload is a valid (empty) object: nothing to warn about.
		assert result.stdout == ""


def test_stdin_read_error_allows_with_warning(monkeypatch, capsys):
	monkeypatch.setattr(hook.sys, "stdin", Mock(read=Mock(side_effect=OSError("read failed"))))
	assert hook.main() == 0
	emitted = json.loads(capsys.readouterr().out)
	assert emitted["systemMessage"] == "PR check-in reminder skipped: could not read the hook payload"


def test_internal_exception_allows_with_warning(monkeypatch, capsys):
	monkeypatch.setattr(hook.sys, "stdin", Mock(read=Mock(return_value="{}")))
	monkeypatch.setattr(hook, "evaluate", Mock(side_effect=RuntimeError("evaluate failed")))
	assert hook.main() == 0
	emitted = json.loads(capsys.readouterr().out)
	assert emitted["systemMessage"] == "PR check-in reminder skipped: internal error (evaluate failed)"


# ──────────────────────────────────────────────────────────────────
# Wiring
# ──────────────────────────────────────────────────────────────────


def _reminder_entries(path: Path) -> list[dict]:
	settings = json.loads(path.read_text(encoding="utf-8"))
	return [
		entry
		for entry in settings["hooks"].get("PostToolUse", [])
		if any("pr_check_in_reminder.py" in h.get("command", "") for h in entry.get("hooks", []))
	]


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_settings_wire_the_reminder_once_under_the_documented_matcher(path):
	entries = _reminder_entries(path)
	assert len(entries) == 1
	entry = entries[0]
	assert entry["matcher"] == hook.SETTINGS_MATCHER
	assert len(entry["hooks"]) == 1
	wired = entry["hooks"][0]
	assert wired["type"] == "command"
	assert wired["command"] == 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/pr_check_in_reminder.py'
	assert isinstance(wired.get("timeout"), int) and wired["timeout"] > 0


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_reminder_is_not_wired_as_a_pre_tool_use_hook(path):
	"""The reminder must run after the push, never gate it."""
	settings = json.loads(path.read_text(encoding="utf-8"))
	for entry in settings["hooks"]["PreToolUse"]:
		for wired in entry.get("hooks", []):
			assert "pr_check_in_reminder.py" not in wired.get("command", "")


@pytest.mark.parametrize("tool_name", ["Bash"] + [t for t in PR_WRITE_TOOLS if t.startswith("mcp__")])
def test_settings_matcher_selects_every_reminded_tool(tool_name):
	assert re.fullmatch(hook.SETTINGS_MATCHER, tool_name)


@pytest.mark.parametrize("tool_name", [t for t in SILENT_TOOLS if t.startswith("mcp__")])
def test_settings_matcher_skips_unrelated_mcp_tools(tool_name):
	# Both unanchored and anchored interpretations of the harness matcher
	# must agree that these tools are not routed through the reminder.
	assert re.fullmatch(hook.SETTINGS_MATCHER, tool_name) is None
	assert re.search(hook.SETTINGS_MATCHER, tool_name) is None


def test_existing_guard_wiring_is_untouched():
	for path in (SETTINGS_PATH, TEMPLATE_SETTINGS_PATH):
		settings = json.loads(path.read_text(encoding="utf-8"))
		matchers = [entry.get("matcher") for entry in settings["hooks"]["PreToolUse"]]
		assert "Bash" in matchers
		assert "mcp__github__push_files|mcp__github__create_or_update_file" in matchers
		assert "mcp__.*__subscribe_pr_activity" in matchers


def test_template_parity():
	assert TEMPLATE_HOOK_PATH.read_text(encoding="utf-8") == HOOK_PATH.read_text(encoding="utf-8")
	assert TEMPLATE_SETTINGS_PATH.read_text(encoding="utf-8") == SETTINGS_PATH.read_text(encoding="utf-8")


def test_claude_md_documents_the_rule():
	text = CLAUDE_MD.read_text(encoding="utf-8")
	assert "## §26. Post-Push PR Status Check-In (MANDATORY)" in text
	assert ".claude/hooks/pr_check_in_reminder.py" in text
	assert f"`{hook.SETTINGS_MATCHER}`" in text
	assert "tests/test_pr_check_in_reminder.py" in text
	joined = " ".join(text.split())
	assert "`delay_minutes: 180`" in joined
	assert "`model: claude-sonnet-5`" in joined
	assert '`model: "sonnet"`' in joined
	assert ".claude/scripts/check_in_status.py" in joined
	assert "`/implement-plan-claude` is the exception" in joined
	assert "PushNotification" in joined
	assert (
		"fails open with a `systemMessage` warning when the hook payload cannot be read, "
		"is invalid or non-object JSON, or evaluation raises an internal exception."
	) in joined
	assert (
		"Empty or whitespace-only hook input is treated as an empty object and allowed silently."
	) in joined
	# §25 stays in force and points at §26 as the allowed scheduler use.
	assert "## §25. PR Watching Is Disabled (MANDATORY)" in text
	assert "including the post-push PR status check-in §26 requires" in joined
	assert "The §26 status check-in is not a watch" in joined
	# §26 sits after §25 and before the final reminder (§6: never renumber).
	assert text.index("## §25.") < text.index("## §26.") < text.index("## FINAL REMINDER")


@pytest.mark.parametrize("path", [SEED_REPO_COMMAND, TEMPLATE_SEED_REPO_COMMAND])
def test_seed_repo_command_ships_the_hook(path):
	assert "hooks/pr_check_in_reminder.py" in path.read_text(encoding="utf-8")


def test_ci_runs_this_file():
	assert "tests/test_pr_check_in_reminder.py" in CI_WORKFLOW.read_text(encoding="utf-8")

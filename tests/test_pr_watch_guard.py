#!/usr/bin/env python3
"""Behaviour and wiring contract for the PR-watch guard (CLAUDE.md §25).

Covers the pieces that can silently detach the mechanism:
  1. The rule — every `subscribe_pr_activity` MCP tool is blocked, from any
     server; `unsubscribe_pr_activity` and unrelated tools are allowed.
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

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_PATH = REPO_ROOT / ".claude" / "hooks" / "pr_watch_guard.py"
TEMPLATE_GUARD_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "pr_watch_guard.py"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
TEMPLATE_SETTINGS_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
SEED_REPO_COMMAND = REPO_ROOT / ".claude" / "commands" / "seed-repo.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SUBSCRIBE_TOOLS = [
	"mcp__github__subscribe_pr_activity",
	"mcp__Claude_Code_Remote__subscribe_pr_activity",
	"mcp__claude-code-remote__subscribe_pr_activity",
	"mcp__some_future_server__subscribe_pr_activity",
	"subscribe_pr_activity",
]

ALLOWED_TOOLS = [
	"mcp__github__unsubscribe_pr_activity",
	"mcp__Claude_Code_Remote__unsubscribe_pr_activity",
	"unsubscribe_pr_activity",
	"mcp__github__create_pull_request",
	"mcp__github__pull_request_read",
	"mcp__Claude_Code_Remote__send_later",
	"mcp__Claude_Code_Remote__create_trigger",
	"Bash",
	"Edit",
	"subscribe_pr_activity_v2",
	"mcp__github__subscribe_pr_activity_extra",
]


def _load_guard():
	spec = importlib.util.spec_from_file_location("pr_watch_guard", GUARD_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


guard = _load_guard()


def _run_hook(stdin_text: str) -> subprocess.CompletedProcess:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		[sys.executable, str(GUARD_PATH)],
		input=stdin_text,
		capture_output=True,
		text=True,
		env=env,
		check=False,
	)


# ──────────────────────────────────────────────────────────────────
# The rule
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool_name", SUBSCRIBE_TOOLS)
def test_subscribe_tools_are_recognised(tool_name):
	assert guard.is_subscribe_tool(tool_name)


@pytest.mark.parametrize("tool_name", ALLOWED_TOOLS)
def test_other_tools_are_not_recognised(tool_name):
	assert not guard.is_subscribe_tool(tool_name)


@pytest.mark.parametrize("tool_name", [None, 42, ["mcp__github__subscribe_pr_activity"], {}])
def test_non_string_tool_name_is_not_recognised(tool_name):
	assert not guard.is_subscribe_tool(tool_name)


@pytest.mark.parametrize("tool_name", SUBSCRIBE_TOOLS)
def test_evaluate_blocks_subscribe(tool_name):
	code, message = guard.evaluate({"tool_name": tool_name, "tool_input": {"owner": "o", "repo": "r", "pullNumber": 1}})
	assert code == 2
	assert "§25" in message
	assert "unsubscribe" not in message.split("§25")[0].lower()


@pytest.mark.parametrize("tool_name", ALLOWED_TOOLS)
def test_evaluate_allows_everything_else(tool_name):
	assert guard.evaluate({"tool_name": tool_name, "tool_input": {}}) == (0, "")


def test_evaluate_allows_payload_without_tool_name():
	assert guard.evaluate({}) == (0, "")
	assert guard.evaluate({"tool_input": {"command": "git push"}}) == (0, "")


# ──────────────────────────────────────────────────────────────────
# End to end through the hook protocol
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool_name", ["mcp__github__subscribe_pr_activity", "mcp__Claude_Code_Remote__subscribe_pr_activity"])
def test_hook_process_blocks_subscribe_with_reason_on_stderr(tool_name):
	result = _run_hook(json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": {"owner": "o", "repo": "r", "pullNumber": 7}}))
	assert result.returncode == 2
	assert "CLAUDE.md §25" in result.stderr
	assert "pr_watch_guard.py" in result.stderr
	assert result.stdout == ""


@pytest.mark.parametrize("tool_name", ["mcp__github__unsubscribe_pr_activity", "mcp__github__create_pull_request"])
def test_hook_process_allows_other_tools_silently(tool_name):
	result = _run_hook(json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": {}}))
	assert result.returncode == 0
	assert result.stdout == ""
	assert result.stderr == ""


def test_hook_has_no_environment_escape_hatch():
	"""§25.D: strict by decision — no env var may switch the guard off."""
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["CLAUDE_PR_MERGE_GUARD"] = "off"
	env["CLAUDE_PR_WATCH_GUARD"] = "off"
	result = subprocess.run(
		[sys.executable, str(GUARD_PATH)],
		input=json.dumps({"tool_name": "mcp__github__subscribe_pr_activity", "tool_input": {}}),
		capture_output=True,
		text=True,
		env=env,
		check=False,
	)
	assert result.returncode == 2
	source = GUARD_PATH.read_text(encoding="utf-8")
	assert "os.environ" not in source
	assert "getenv" not in source


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
		assert emitted["systemMessage"].startswith("PR-watch guard skipped:")
	else:
		# An empty payload is a valid (empty) object: nothing to warn about.
		assert result.stdout == ""


# ──────────────────────────────────────────────────────────────────
# Wiring
# ──────────────────────────────────────────────────────────────────


def _watch_guard_entries(path: Path) -> list[dict]:
	settings = json.loads(path.read_text(encoding="utf-8"))
	return [
		entry
		for entry in settings["hooks"]["PreToolUse"]
		if any("pr_watch_guard.py" in hook.get("command", "") for hook in entry.get("hooks", []))
	]


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_settings_wire_the_guard_once_under_the_documented_matcher(path):
	entries = _watch_guard_entries(path)
	assert len(entries) == 1
	entry = entries[0]
	assert entry["matcher"] == guard.SETTINGS_MATCHER
	assert len(entry["hooks"]) == 1
	hook = entry["hooks"][0]
	assert hook["type"] == "command"
	assert hook["command"] == 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/pr_watch_guard.py'
	assert isinstance(hook.get("timeout"), int) and hook["timeout"] > 0


@pytest.mark.parametrize("tool_name", [t for t in SUBSCRIBE_TOOLS if t.startswith("mcp__")])
def test_settings_matcher_selects_every_mcp_subscribe_tool(tool_name):
	assert re.search(guard.SETTINGS_MATCHER, tool_name)
	assert re.fullmatch(guard.SETTINGS_MATCHER, tool_name)


@pytest.mark.parametrize("tool_name", [t for t in ALLOWED_TOOLS if t.startswith("mcp__")])
def test_settings_matcher_skips_unsubscribe_and_unrelated_mcp_tools(tool_name):
	# Both unanchored and anchored interpretations of the harness matcher
	# must agree that these tools are not routed through the guard.
	assert re.fullmatch(guard.SETTINGS_MATCHER, tool_name) is None
	if tool_name.endswith("subscribe_pr_activity"):
		assert re.search(guard.SETTINGS_MATCHER, tool_name) is None


def test_existing_merged_pr_guard_wiring_is_untouched():
	for path in (SETTINGS_PATH, TEMPLATE_SETTINGS_PATH):
		settings = json.loads(path.read_text(encoding="utf-8"))
		matchers = [entry.get("matcher") for entry in settings["hooks"]["PreToolUse"]]
		assert "Bash" in matchers
		assert "mcp__github__push_files|mcp__github__create_or_update_file" in matchers


def test_template_parity():
	assert TEMPLATE_GUARD_PATH.read_text(encoding="utf-8") == GUARD_PATH.read_text(encoding="utf-8")
	assert TEMPLATE_SETTINGS_PATH.read_text(encoding="utf-8") == SETTINGS_PATH.read_text(encoding="utf-8")


def test_claude_md_documents_the_rule():
	text = CLAUDE_MD.read_text(encoding="utf-8")
	assert "## §25. PR Watching Is Disabled (MANDATORY)" in text
	assert ".claude/hooks/pr_watch_guard.py" in text
	assert "`mcp__.*__subscribe_pr_activity`" in text
	assert "tests/test_pr_watch_guard.py" in text
	# §12.G is retained for §6 section stability but marked inactive.
	assert "### G) Autofix CI / Address-Comments Mode Add-ons\n\n**INACTIVE — superseded by §25.**" in text
	# The event trigger is gone from the §12 preamble.
	assert "a direct chat request, a `subscribe_pr_activity` event" not in text
	# §23.B no longer lists subscribing as a routine write.
	assert "and subscribing/unsubscribing to PR activity." not in text


def test_seed_repo_command_ships_the_hook():
	assert "hooks/pr_watch_guard.py" in SEED_REPO_COMMAND.read_text(encoding="utf-8")


def test_ci_runs_this_file():
	assert "tests/test_pr_watch_guard.py" in CI_WORKFLOW.read_text(encoding="utf-8")

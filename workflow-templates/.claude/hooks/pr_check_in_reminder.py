#!/usr/bin/env python3
"""PostToolUse reminder that arms the post-push PR status check-in.

Implements the enforcement half of CLAUDE.md §26 (post-push PR status
check-in). After a session pushes a branch, opens a pull request, or writes
to a remote branch through the GitHub MCP tools, §26 requires it to arm a
3-hourly status check-in for that pull request. The instruction sits at the
far end of the context window exactly when a session has run long enough to
push, so this hook feeds the reminder back into the model's context on every
matching tool call, regardless of what the model remembers.

Rule: after a matching tool call succeeds, emit `additionalContext` that
restates the §26 steps. Matching calls are

  * any MCP `create_pull_request` tool (`mcp__github__create_pull_request`
    and any other `mcp__<server>__create_pull_request`);
  * any MCP `push_files` / `create_or_update_file` tool, which write to a
    remote branch without a local `git push`;
  * `Bash` when the command pushes (`git push`, not a `--dry-run`) or opens
    a pull request (`gh pr create`).

Every other tool call, including a `Bash` command that does not push, passes
through silently. The hook never blocks (a PostToolUse hook runs after the
tool has already acted), issues no API calls (§15), and reads no environment
variables. It fails open with a `systemMessage` warning when the payload
cannot be read, is invalid or non-object JSON, or evaluation raises an
internal exception, the same contract as `pr_watch_guard.py`; empty or
whitespace-only input is treated as an empty object and allowed silently.

The reminder is the §26 mechanism, not a §25 subscription: it tells the
session to start a Sonnet checker session (`create_session`, re-armed with
`send_later`), never to subscribe to PR activity, and the §25 guard keeps
blocking `subscribe_pr_activity`.
"""

from __future__ import annotations

import json
import re
import sys


# Matches the trailing tool-name segment of an MCP tool that opens a pull
# request or writes to a remote branch.
PR_WRITE_TOOL_PATTERN = re.compile(r"(^|__)(create_pull_request|push_files|create_or_update_file)$")

# A `git push` (any remote, any refspec) that is not a dry run, or a
# `gh pr create`. Checked on the whole Bash command so chained commands
# (`git add ... && git commit ... && git push ...`) are recognised. Global
# git options before the subcommand may carry a value (`git -C /repo push`,
# `git -c key=value push`, `git --no-pager push`).
_GIT_GLOBAL_OPTIONS = r"(?:-\S+\s+(?:[^-\s]\S*\s+)?)*"
GIT_PUSH_PATTERN = re.compile(r"(^|[\s;&|(])git\s+" + _GIT_GLOBAL_OPTIONS + r"push\b")
GIT_PUSH_DRY_RUN_PATTERN = re.compile(r"(^|[\s;&|(])git\s+" + _GIT_GLOBAL_OPTIONS + r"push\b[^;&|]*--dry-run")
GH_PR_CREATE_PATTERN = re.compile(r"(^|[\s;&|(])gh\s+pr\s+create\b")

# The exact `.claude/settings.json` matcher this hook is wired under. Kept
# here so the tests can assert the wiring and the hook agree. Anchored so
# that an unanchored regex interpretation of the matcher cannot select
# tools that merely start with a matched name
# (`mcp__github__create_pull_request_with_copilot`).
SETTINGS_MATCHER = "^(?:Bash|mcp__.*__create_pull_request|mcp__.*__push_files|mcp__.*__create_or_update_file)$"

REMINDER = (
	"PR status check-in reminder (CLAUDE.md §26): this call pushed a branch, "
	"opened a pull request, or wrote to a remote branch. Once the pull request "
	"for the pushed branch exists, arm the 3-hourly status check-in for it "
	"unless one is already armed for that PR, /implement-plan-claude or "
	"/implement-issue-claude opened it (its own checker is the check-in), "
	"or the user opted out for this "
	"task: call create_session with a Sonnet model and a prompt naming the "
	"repository, the PR number and URL, the §26 check-in steps (run "
	".claude/scripts/check_in_status.py --terminal-only; re-arm with "
	"send_later delay_minutes=180 while the PR is open; on merged or closed, "
	"report the next steps, say whether the pushing session can be closed, "
	"and send one PushNotification), and the next steps for each terminal "
	"state. Without create_session, use send_later into this session and a "
	"Sonnet subagent for the read. Never subscribe to PR activity (§25)."
)


def is_pr_write_tool(tool_name: object) -> bool:
	"""Return True when `tool_name` is an MCP tool that opens a PR or writes a remote branch."""
	if not isinstance(tool_name, str):
		return False
	return PR_WRITE_TOOL_PATTERN.search(tool_name) is not None


def is_push_command(command: object) -> bool:
	"""Return True when a Bash command pushes a branch or opens a PR via `gh`."""
	if not isinstance(command, str):
		return False
	if GH_PR_CREATE_PATTERN.search(command):
		return True
	if not GIT_PUSH_PATTERN.search(command):
		return False
	return GIT_PUSH_DRY_RUN_PATTERN.search(command) is None


def _warn(reason: str) -> None:
	"""Emit a non-blocking warning to the user."""
	print(json.dumps({"systemMessage": f"PR check-in reminder skipped: {reason}"}))


def evaluate(payload: dict) -> str:
	"""Decide the reminder for one PostToolUse payload.

	Returns the `additionalContext` text to feed back to Claude, or an empty
	string when the tool call is not a push or PR creation.
	"""
	tool_name = payload.get("tool_name")
	if is_pr_write_tool(tool_name):
		return REMINDER
	if tool_name == "Bash":
		tool_input = payload.get("tool_input")
		command = tool_input.get("command") if isinstance(tool_input, dict) else None
		if is_push_command(command):
			return REMINDER
	return ""


def main() -> int:
	try:
		raw = sys.stdin.read()
	except (OSError, ValueError):
		_warn("could not read the hook payload")
		return 0
	try:
		payload = json.loads(raw) if raw.strip() else {}
	except ValueError:
		_warn("hook payload is not valid JSON")
		return 0
	if not isinstance(payload, dict):
		_warn("hook payload is not a JSON object")
		return 0

	try:
		context = evaluate(payload)
	except Exception as exc:  # noqa: BLE001 - the reminder must never break the session
		_warn(f"internal error ({exc})")
		return 0

	if context:
		print(
			json.dumps(
				{
					"hookSpecificOutput": {
						"hookEventName": "PostToolUse",
						"additionalContext": context,
					}
				}
			)
		)
	return 0


if __name__ == "__main__":
	sys.exit(main())

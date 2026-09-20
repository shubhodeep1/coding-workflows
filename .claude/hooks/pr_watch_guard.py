#!/usr/bin/env python3
"""PreToolUse guard that blocks PR-activity subscriptions.

Implements CLAUDE.md §25 (PR watching is disabled).

The harness's own system prompt tells Claude to offer PR watching after it
opens a pull request, and to subscribe when asked. CLAUDE.md §25 forbids both,
in this repo and in every consumer repo that receives the file via the
`@stable` sync. Prose alone cannot enforce that: the instruction is furthest
from the live context edge exactly when a session has run long enough to open
a PR and be tempted to babysit it. This hook enforces it deterministically —
the harness runs it on every matching tool call regardless of what the model
remembers.

Rule: any `subscribe_pr_activity` MCP tool call is blocked, from every MCP
server that exposes one (`mcp__github__subscribe_pr_activity`,
`mcp__Claude_Code_Remote__subscribe_pr_activity`, and any other
`mcp__<server>__subscribe_pr_activity`). `unsubscribe_pr_activity` is never
blocked — tearing a subscription down is always allowed.

There is deliberately no environment-variable escape hatch (unlike the §21
guard's `CLAUDE_PR_MERGE_GUARD`): §25 is strict by decision. Re-enabling PR
watching for a repo means changing CLAUDE.md §25 and removing this hook from
`.claude/settings.json` in a reviewed change, not flipping a session variable.

Exit codes (Claude Code hook protocol):
  0 — allow the tool call (every tool that is not a PR subscription).
  2 — block the tool call; stderr is fed back to Claude as the reason.

The guard must never break the session: an unreadable, invalid, or non-object
payload, or an internal evaluation error, allows the call with a
`systemMessage` warning, the same fail-open contract as
`pr_merge_status_guard.py`.
Empty or whitespace-only input is instead treated as an empty object and
allowed silently because there is no tool payload to evaluate.
"""

from __future__ import annotations

import json
import re
import sys


# Matches the trailing tool-name segment of an MCP subscription tool. The
# `(^|__)` alternative rules out `unsubscribe_pr_activity`, whose segment is
# preceded by `un`, not by `__` or the start of the string.
SUBSCRIBE_TOOL_PATTERN = re.compile(r"(^|__)subscribe_pr_activity$")

# The exact `.claude/settings.json` matcher this hook is wired under. Kept
# here so the tests can assert the wiring and the hook agree.
SETTINGS_MATCHER = "mcp__.*__subscribe_pr_activity"

BLOCK_MESSAGE = (
	"PR-watch guard (CLAUDE.md §25): subscribing to pull request activity is "
	"disabled in this repository. Do not subscribe to, watch, babysit, or "
	"autofix a pull request after pushing it, and do not offer to. This applies "
	"even when the user asks for it in the session: reply that CLAUDE.md §25 "
	"forbids PR watching and that the policy must be changed in CLAUDE.md (and "
	"the pr_watch_guard.py hook removed from .claude/settings.json) in a "
	"reviewed change first. "
	"Push the branch, open the pull request, report the link, and stop."
)


def is_subscribe_tool(tool_name: object) -> bool:
	"""Return True when `tool_name` is a PR-activity *subscribe* tool.

	`unsubscribe_pr_activity` never matches: the pattern requires the
	`subscribe_pr_activity` segment to sit at the start of the name or right
	after an MCP `__` separator.
	"""
	if not isinstance(tool_name, str):
		return False
	return SUBSCRIBE_TOOL_PATTERN.search(tool_name) is not None


def _warn(reason: str) -> None:
	"""Emit a non-blocking warning to the user and allow the call."""
	print(json.dumps({"systemMessage": f"PR-watch guard skipped: {reason}"}))


def evaluate(payload: dict) -> tuple[int, str]:
	"""Decide the hook outcome for one PreToolUse payload.

	Returns `(exit_code, stderr_message)`. Exit code 2 blocks the call and
	the message is what Claude sees; exit code 0 allows it silently.
	"""
	if is_subscribe_tool(payload.get("tool_name")):
		return 2, BLOCK_MESSAGE
	return 0, ""


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
		code, message = evaluate(payload)
	except Exception as exc:  # noqa: BLE001 - the guard must never break the session
		_warn(f"internal error ({exc})")
		return 0

	if message:
		print(message, file=sys.stderr)
	return code


if __name__ == "__main__":
	sys.exit(main())

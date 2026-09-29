#!/usr/bin/env python3
"""Stop an unattended issue-mode session from waiting on a question nobody reads.

Implements CLAUDE.md §28.G (enforcement of §28.B / §28.C in issue mode).

In issue mode (CLAUDE.md §28.A) nobody watches the session. A question is
either auto-decided (§28.B: take the RECOMMENDED option, record an `AD-<n>`
entry, continue) or, for a §28.C item, posted on the source issue as the
`<!-- ai:claude-blocked:v1 -->` comment with the `ai:claude-blocked` label.
Sessions have still ended their turn on an in-session "Q1: A/B/C" question,
or stalled on an `AskUserQuestion` permission prompt, and the project never
moved again (issue #4911). Prose cannot prevent that; this hook does:

- `Stop`: when the final assistant message asks a §2-format question (a
  `Q<n>:` line with lettered choices, or `Q<n>: A/B`) or asks for
  permissions, and the current turn did not post a verified blocker on the
  marker's issue, the stop is blocked with a reason that
  restates §28. At most STOP_BLOCK_CAP blocks per session; after that the
  stop is allowed with a `systemMessage` starting CAP_MESSAGE_PREFIX and one
  JSON line in `~/.claude/unattended-issue-mode/stop-guard.jsonl`.
- `PreToolUse` on `AskUserQuestion`: always denied, with the same reason.

Only **marked** sessions are affected. The issue-mode preflight of
`/implement-issue-claude` (step 0) and `/implement-plan-claude` (step 1)
runs this file's `mark` subcommand, which writes
`~/.claude/unattended-issue-mode/<CLAUDE_CODE_REMOTE_SESSION_ID>.json`. A
session is marked when `CLAUDE_CODE_REMOTE_SESSION_ID` is set and a marker
for exactly that id exists. Interactive and local sessions have no marker (a
local session has no `CLAUDE_CODE_REMOTE_SESSION_ID` at all), so the hook
prints nothing for them and allows everything.

A verified blocker (issue #5082) is, in the current turn, both of:
- a comment on the marker's exact `repo` (owner/repo, case-insensitive) and
  `issue`, whose body starts with `<!-- ai:claude-blocked:v1 -->`, posted by
  `mcp__*__add_issue_comment` or by a plain `gh api` POST to
  `repos/<owner>/<repo>/issues/<N>/comments`, with a non-error result that
  carries that issue's comment URL (`html_url` or `issue_url`);
- a non-error write adding the `ai:claude-blocked` label to that same issue
  (`mcp__*__issue_write` with `method: "update"` and `labels`, or a plain
  `gh api` POST to `repos/<owner>/<repo>/issues/<N>/labels` with
  `labels[]=ai:claude-blocked`).
A `Bash` call counts only when it is `gh api` calls alone, joined by `&&` at
most, after an optional leading `cd <path>;` (no other program, `;`, pipe,
redirect, or substitution). An `echo` of
the marker, a comment on another issue or repository, or a failed or
unverifiable call never counts.

The hook makes no GitHub API calls (§15): the blocked-comment check reads the
local session transcript (`transcript_path`). It fails open with a
`systemMessage` on an unreadable, invalid, or non-object payload, an
unreadable marker or state file, or an internal error, the same contract as
the §21/§25/§26 hooks. Empty or whitespace-only input is allowed silently.

Usage:
  unattended_question_guard.py                  hook mode (payload on stdin)
  unattended_question_guard.py mark --repo <owner>/<repo> --issue <N>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path


# The `.claude/settings.json` wiring this hook expects. Kept here so the tests
# can assert the wiring and the hook agree.
SETTINGS_STOP_EVENT = "Stop"
SETTINGS_ASK_MATCHER = "AskUserQuestion"

ENV_SESSION = "CLAUDE_CODE_REMOTE_SESSION_ID"
MARKER_DIR_PARTS = (".claude", "unattended-issue-mode")
MARKER_VERSION = 1
STOP_BLOCK_CAP = 2
BLOCKED_COMMENT_MARKER = "<!-- ai:claude-blocked:v1 -->"
CAP_LOG_NAME = "stop-guard.jsonl"
CAP_MESSAGE_PREFIX = "unattended-question-guard: cap reached"
BLOCKED_LABEL = "ai:claude-blocked"
MCP_COMMENT_TOOL_SUFFIX = "__add_issue_comment"
MCP_LABEL_TOOL_SUFFIX = "__issue_write"
# `issue_write` also takes `method: "create"`, which ignores `issue_number` and
# opens a new issue, so only an update can put the label on the marker's issue.
MCP_LABEL_WRITE_METHOD = "update"

_SESSION_FILE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# A §2 question: a `Q<n>:` line (optionally quoted or bold) ...
_QUESTION_LINE_RE = re.compile(r"^[ \t>*_#-]*Q\d+\s*[:.]", re.MULTILINE)
# ... with lettered choices (`- **A** — …`, `A) …`, `B: …`) ...
_CHOICE_LINE_RE = re.compile(
	r"^[ \t>]*(?:[-*+][ \t]+)?(?:\*\*|__)?\(?([A-H])\)?(?:\*\*|__)?[ \t]*(?:—|–|-|:|\)|\.)[ \t]*\S",
	re.MULTILINE,
)
# ... or the inline form `Q1: A/B/C`.
_INLINE_QUESTION_RE = re.compile(r"\bQ\d+\s*:\s*\**[A-H]\**\s*/\s*\**[A-H]\b")

# Asking for permissions instead of using the self-serve writes.
_PERMISSION_ASK_RES = (
	re.compile(
		r"\b(?:await|wait)(?:s|ing)?\s+(?:on\s+|for\s+)?(?:your\s+|a\s+|the\s+|human\s+|operator\s+)?(?:permissions?|approvals?)\b",
		re.IGNORECASE,
	),
	re.compile(r"\bneeds?\s+GitHub\s+writes?\b", re.IGNORECASE),
	re.compile(
		r"\b(?:grant|approve)\s+(?:the\s+|this\s+|these\s+|my\s+)?(?:permissions?|prompts?|tool\s+calls?)\b",
		re.IGNORECASE,
	),
	re.compile(r"\b(?:needs?|requires?|waiting\s+for)\s+(?:a\s+)?(?:fresh|new|watched)\s+session\b", re.IGNORECASE),
	re.compile(r"\b(?:needs?|requires?)\s+(?:your\s+|human\s+|operator\s+)?permissions?\s+(?:to|for)\b", re.IGNORECASE),
)

KIND_QUESTION = "a §2 question"
KIND_PERMISSION = "a request for permissions"


class GuardStateError(Exception):
	"""A marker or state file exists but cannot be read or written."""


def marker_dir() -> Path:
	return Path.home().joinpath(*MARKER_DIR_PARTS)


def _stem(session: str) -> str:
	return _SESSION_FILE_RE.sub("_", session)[:120]


def marker_path(session: str, directory: Path) -> Path:
	return directory / f"{_stem(session)}.json"


def state_path(session: str, directory: Path) -> Path:
	return directory / f"{_stem(session)}.state.json"


def write_marker(repo: str, issue: int, session: str, directory: Path, now: datetime) -> dict:
	marker = {
		"version": MARKER_VERSION,
		"session": session,
		"repo": repo,
		"issue": issue,
		"marked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
	}
	directory.mkdir(parents=True, exist_ok=True)
	marker_path(session, directory).write_text(json.dumps(marker) + "\n", encoding="utf-8")
	return marker


def read_marker(session: str, directory: Path) -> dict | None:
	"""Return the marker for `session`, None when there is none.

	Raises GuardStateError when a marker file exists but is unreadable or
	malformed. A marker written for another session id is treated as absent.
	"""
	path = marker_path(session, directory)
	if not path.exists():
		return None
	try:
		marker = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError) as exc:
		raise GuardStateError(f"marker {path} is unreadable ({exc})") from exc
	if not isinstance(marker, dict):
		raise GuardStateError(f"marker {path} is not a JSON object")
	if marker.get("session") != session:
		return None
	return marker


def read_blocks(session: str, directory: Path) -> int:
	path = state_path(session, directory)
	if not path.exists():
		return 0
	try:
		state = json.loads(path.read_text(encoding="utf-8"))
		blocks = int(state.get("stop_blocks", 0))
	except (OSError, ValueError, TypeError, AttributeError) as exc:
		raise GuardStateError(f"state {path} is unreadable ({exc})") from exc
	return max(blocks, 0)


def write_blocks(session: str, directory: Path, blocks: int) -> None:
	try:
		directory.mkdir(parents=True, exist_ok=True)
		state_path(session, directory).write_text(json.dumps({"stop_blocks": blocks}) + "\n", encoding="utf-8")
	except OSError as exc:
		raise GuardStateError(f"state for {session} could not be written ({exc})") from exc


def question_kind(text: str) -> str | None:
	"""Return what the message asks the absent user for, or None."""
	if not text:
		return None
	if _INLINE_QUESTION_RE.search(text):
		return KIND_QUESTION
	if _QUESTION_LINE_RE.search(text) and len(set(_CHOICE_LINE_RE.findall(text))) >= 2:
		return KIND_QUESTION
	for pattern in _PERMISSION_ASK_RES:
		if pattern.search(text):
			return KIND_PERMISSION
	return None


def load_transcript(path: object) -> list[dict]:
	"""Return the transcript entries, or [] when it cannot be read."""
	if not isinstance(path, str) or not path:
		return []
	entries: list[dict] = []
	try:
		with open(path, encoding="utf-8") as handle:
			for line in handle:
				try:
					entry = json.loads(line)
				except ValueError:
					continue
				if isinstance(entry, dict):
					entries.append(entry)
	except OSError:
		return []
	return entries


def _content(entry: dict) -> object:
	message = entry.get("message")
	return message.get("content") if isinstance(message, dict) else None


def _is_prompt(entry: dict) -> bool:
	"""True for a `user` entry that starts a turn (not a tool result)."""
	if entry.get("type") != "user" or entry.get("isMeta"):
		return False
	content = _content(entry)
	if isinstance(content, str):
		return True
	if isinstance(content, list):
		return not any(isinstance(item, dict) and item.get("type") == "tool_result" for item in content)
	return False


def current_turn(entries: list[dict]) -> list[dict]:
	start = 0
	for index, entry in enumerate(entries):
		if _is_prompt(entry):
			start = index + 1
	return entries[start:]


def final_assistant_text(entries: list[dict]) -> str:
	"""The text of the trailing assistant entries of the transcript."""
	parts: list[str] = []
	for entry in reversed(entries):
		if entry.get("type") == "assistant":
			content = _content(entry)
			if isinstance(content, str):
				parts.append(content)
			elif isinstance(content, list):
				texts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
				parts.append("\n".join(text for text in texts if isinstance(text, str)))
		elif entry.get("type") == "user":
			break
	return "\n".join(part for part in reversed(parts) if part)


def _contains_marker(value: object) -> bool:
	"""True when the marker text appears anywhere in `value`.

	Unused since issue #5082 and kept only under CLAUDE.md §6. This is the old
	marker-anywhere check that an `echo` of the marker could satisfy: never use
	it as evidence of a blocker. `blocked_comment_posted` is the check.
	"""
	if isinstance(value, str):
		return BLOCKED_COMMENT_MARKER in value
	if isinstance(value, dict):
		return any(_contains_marker(item) for item in value.values())
	if isinstance(value, list):
		return any(_contains_marker(item) for item in value)
	return False


def _issue_target(repo: object, issue: object) -> tuple[str, int] | None:
	"""Normalise an `owner/repo` + issue number pair, None when malformed."""
	if not isinstance(repo, str) or not _REPO_RE.match(repo):
		return None
	if isinstance(issue, bool):
		return None
	if isinstance(issue, int):
		number = issue
	elif isinstance(issue, str) and issue.isdigit():
		number = int(issue)
	else:
		return None
	if number <= 0:
		return None
	return repo.lower(), number


def _mcp_issue_target(tool_input: dict) -> tuple[str, int] | None:
	owner = tool_input.get("owner")
	repo = tool_input.get("repo")
	if not isinstance(owner, str) or not isinstance(repo, str):
		return None
	return _issue_target(f"{owner}/{repo}", tool_input.get("issue_number"))


def _starts_with_marker(body: object) -> bool:
	return isinstance(body, str) and body.lstrip().startswith(BLOCKED_COMMENT_MARKER)


def _and_chain_segments(command: str) -> list[str] | None:
	"""Split `command` on unquoted `&&`; None when it holds any other shell syntax.

	Single-quoted text is data. Outside quotes, `; & | < > ( )` and newlines
	join or redirect commands; outside single quotes, a backtick or `$(` runs
	one. Any of those means a call cannot be tied to what was posted. `&&` is
	kept because it stops at the first failure, so a non-error result means
	every segment succeeded.
	"""
	segments: list[str] = []
	state = None
	start = 0
	index = 0
	while index < len(command):
		char = command[index]
		if state == "'":
			if char == "'":
				state = None
		elif char == "\\":
			index += 1
		elif char == "`" or command.startswith("$(", index):
			return None
		elif state == '"':
			if char == '"':
				state = None
		elif char in "'\"":
			state = char
		elif command.startswith("&&", index):
			segments.append(command[start:index])
			index += 2
			start = index
			continue
		elif char in ";&|<>()\n":
			return None
		index += 1
	if state is not None:
		return None
	segments.append(command[start:])
	return segments


_GH_VALUE_FLAGS = {
	"-X": "method",
	"--method": "method",
	"-f": "raw_field",
	"--raw-field": "raw_field",
	"-F": "field",
	"--field": "field",
	"-H": "header",
	"--header": "header",
	"-q": "jq",
	"--jq": "jq",
	"-t": "template",
	"--template": "template",
	"-p": "preview",
	"--preview": "preview",
	"--hostname": "hostname",
	"--cache": "cache",
	"--input": "input",
}
_GH_ISSUE_ENDPOINT_RE = re.compile(r"^/?repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)/(comments|labels)$")


_CD_PREFIX_RE = re.compile(r"^\s*cd\s+[A-Za-z0-9_./~+-]+\s*(?:;|&&)\s*")


def _gh_api_calls(command: object) -> list[dict] | None:
	"""Parse a `Bash` command made only of `gh api` calls joined by `&&`.

	One leading `cd <plain path>` followed by `;` or `&&` is allowed, the way
	sessions start most commands; it can neither post nor print anything.
	Returns one `_gh_api_call` result per segment, or None when the command
	holds anything else (another program, other shell syntax, or a `gh api`
	call this parser does not accept).
	"""
	if not isinstance(command, str):
		return None
	segments = _and_chain_segments(_CD_PREFIX_RE.sub("", command, count=1))
	if segments is None:
		return None
	calls: list[dict] = []
	for segment in segments:
		call = _gh_api_call(segment)
		if call is None:
			return None
		calls.append(call)
	return calls


def _gh_api_call(segment: str) -> dict | None:
	"""Parse one plain `gh api` command, None when it is anything else.

	Returns `{"method", "target", "kind", "fields"}`: `kind` is `comments` or
	`labels`, `fields` maps each field key to its values. A command with
	`--input`, a non-github.com `--hostname`, a `-F key=@file` value (the
	posted text is not in the transcript), or an endpoint other than
	`repos/<owner>/<repo>/issues/<N>/(comments|labels)` is not a match.
	"""
	try:
		tokens = shlex.split(segment)
	except ValueError:
		return None
	if tokens[:2] != ["gh", "api"]:
		return None
	endpoint = None
	method = None
	fields: dict[str, list[str]] = {}
	index = 2
	while index < len(tokens):
		token = tokens[index]
		if token.startswith("--"):
			flag, has_inline, inline = token.partition("=")
		elif len(token) > 2 and token[:2] in _GH_VALUE_FLAGS:
			flag, has_inline, inline = token[:2], "attached", token[2:]
		else:
			flag, has_inline, inline = token, "", ""
		if flag in _GH_VALUE_FLAGS:
			if has_inline:
				value = inline
			elif index + 1 < len(tokens):
				index += 1
				value = tokens[index]
			else:
				return None
			role = _GH_VALUE_FLAGS[flag]
			if role == "input":
				return None
			if role == "hostname" and value.lower() != "github.com":
				return None
			if role == "method":
				method = value.upper()
			elif role in ("raw_field", "field"):
				key, sep, field_value = value.partition("=")
				if not sep:
					return None
				if role == "field" and field_value.startswith("@"):
					return None
				fields.setdefault(key, []).append(field_value)
		elif token.startswith("-"):
			pass
		elif endpoint is None:
			endpoint = token
		else:
			return None
		index += 1
	match = _GH_ISSUE_ENDPOINT_RE.match(endpoint or "")
	if match is None:
		return None
	target = _issue_target(match.group(1), match.group(2))
	if target is None:
		return None
	if method is None:
		method = "POST" if fields else "GET"
	return {"method": method, "target": target, "kind": match.group(3), "fields": fields}


def _blocked_comment_targets(name: object, tool_input: object) -> set[tuple[str, int]]:
	"""Every `(owner/repo, issue)` a tool call posts a blocked comment to."""
	targets: set[tuple[str, int]] = set()
	if not isinstance(name, str) or not isinstance(tool_input, dict):
		return targets
	if name.startswith("mcp__") and name.endswith(MCP_COMMENT_TOOL_SUFFIX):
		target = _mcp_issue_target(tool_input) if _starts_with_marker(tool_input.get("body")) else None
		if target is not None:
			targets.add(target)
	elif name == "Bash":
		for call in _gh_api_calls(tool_input.get("command")) or []:
			bodies = call["fields"].get("body", [])
			if call["method"] == "POST" and call["kind"] == "comments" and len(bodies) == 1 and _starts_with_marker(bodies[0]):
				targets.add(call["target"])
	return targets


def _blocked_label_targets(name: object, tool_input: object) -> set[tuple[str, int]]:
	"""Every `(owner/repo, issue)` a tool call adds the blocked label to."""
	targets: set[tuple[str, int]] = set()
	if not isinstance(name, str) or not isinstance(tool_input, dict):
		return targets
	if name.startswith("mcp__") and name.endswith(MCP_LABEL_TOOL_SUFFIX):
		labels = tool_input.get("labels")
		adds_label = tool_input.get("method") == MCP_LABEL_WRITE_METHOD and isinstance(labels, list) and BLOCKED_LABEL in labels
		target = _mcp_issue_target(tool_input) if adds_label else None
		if target is not None:
			targets.add(target)
	elif name == "Bash":
		for call in _gh_api_calls(tool_input.get("command")) or []:
			if call["method"] == "POST" and call["kind"] == "labels" and BLOCKED_LABEL in call["fields"].get("labels[]", []):
				targets.add(call["target"])
	return targets


def _result_text(item: dict) -> str:
	"""The text of a `tool_result` item's content (a string or text parts)."""
	content = item.get("content")
	if isinstance(content, str):
		return content
	if isinstance(content, list):
		parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
		return "\n".join(part for part in parts if isinstance(part, str))
	return ""


def _result_names_comment(text: str, target: tuple[str, int]) -> bool:
	"""True when a result carries a comment URL on exactly `target`'s issue."""
	repo, number = target
	pattern = re.compile(
		rf"(?:github\.com/{re.escape(repo)}/issues/{number}#issuecomment-\d|/repos/{re.escape(repo)}/issues/{number}(?![0-9/]))",
		re.IGNORECASE,
	)
	return pattern.search(text) is not None


def blocked_comment_posted(turn: list[dict], marker: dict) -> bool:
	"""True when `turn` posted a verified blocker on the marker's issue.

	Both must hold in the turn: a successful blocked comment on the marker's
	`repo#issue` whose result names that issue's comment URL, and a successful
	write adding the `ai:claude-blocked` label to the same issue.
	"""
	wanted = _issue_target(marker.get("repo"), marker.get("issue"))
	if wanted is None:
		return False
	comments: set[str] = set()
	labels: set[str] = set()
	results: dict[str, str] = {}
	for entry in turn:
		content = _content(entry)
		if not isinstance(content, list):
			continue
		for item in content:
			if not isinstance(item, dict):
				continue
			if item.get("type") == "tool_use":
				tool_id = str(item.get("id"))
				name = item.get("name")
				tool_input = item.get("input")
				if wanted in _blocked_comment_targets(name, tool_input):
					comments.add(tool_id)
				if wanted in _blocked_label_targets(name, tool_input):
					labels.add(tool_id)
			elif item.get("type") == "tool_result" and not item.get("is_error"):
				results[str(item.get("tool_use_id"))] = _result_text(item)
	comment_verified = any(tool_id in results and _result_names_comment(results[tool_id], wanted) for tool_id in comments)
	label_written = any(tool_id in results for tool_id in labels)
	return comment_verified and label_written


def _issue_ref(marker: dict) -> str:
	return f"{marker.get('repo', '<owner>/<repo>')}#{marker.get('issue', '<N>')}"


def _instructions(marker: dict) -> str:
	issue = marker.get("issue", "<N>")
	repo = marker.get("repo", "<owner>/<repo>")
	return (
		"- An intent or design question (scope, behaviour, an edge case, an interface, an ambiguous plan step): "
		"take its RECOMMENDED option, record it as an `AD-<n>` entry in the progress log's `## Auto-decisions` "
		"(CLAUDE.md §28.B, §28.D), and continue the work.\n"
		"- A §28.C item (a failure escalation or cap, an ask-first operation under §22.B / §23.C / §24.D, a "
		"protected-path edit, a question with no option that satisfies §28.B): post ONE comment on issue "
		f"{repo}#{issue} starting `{BLOCKED_COMMENT_MARKER}` that names the blocker, the options, and the recommended "
		f"one; add the `{BLOCKED_LABEL}` label to that same issue; send one PushNotification; then end the turn.\n"
		f"- The stop is allowed only after this turn did both on {repo}#{issue}: the comment through "
		f"`mcp__github__add_issue_comment`, or a plain `gh api repos/{repo}/issues/{issue}/comments -f body='…'` "
		"command whose output shows the comment URL (no `--silent` and no `--jq` that drops `html_url`); and the "
		f"label through `mcp__github__issue_write` (`method: \"{MCP_LABEL_WRITE_METHOD}\"`, `labels` including `{BLOCKED_LABEL}`) or "
		f"`gh api repos/{repo}/issues/{issue}/labels -f 'labels[]={BLOCKED_LABEL}'`. A `Bash` call counts only "
		"when it holds `gh api` calls alone, joined by `&&` at most, after an optional leading `cd <path>;` (no "
		"other program, `;`, pipe, redirect, or `$(...)`). An `echo` of the marker or a comment on another issue "
		"does not count.\n"
		"- GitHub writes are not blocked for you and need no permission: issue and PR comments and labels are "
		"§23.B routine writes (`mcp__github__add_issue_comment`, `mcp__github__update_issue_comment`, "
		"`mcp__github__issue_write`), and the chain's workflow dispatches are command-approved "
		"(`.claude/scripts/dispatch_workflow.py`). Do not ask for permission to use them."
	)


def stop_reason(marker: dict, kind: str, block_number: int) -> str:
	return (
		f"Unattended question guard (CLAUDE.md §28.G, block {block_number} of {STOP_BLOCK_CAP}): this is an "
		f"unattended issue-mode session for {_issue_ref(marker)}. Nobody reads questions in this session, and "
		f"your final message ends the turn on {kind}. Do not end the turn on it. Instead:\n"
		f"{_instructions(marker)}\n"
		"- If the final message only restates a question you already auto-decided and recorded, rewrite it "
		"without the Q/A block."
	)


def ask_deny_reason(marker: dict) -> str:
	return (
		f"Unattended question guard (CLAUDE.md §28.G): AskUserQuestion is denied in this unattended "
		f"issue-mode session for {_issue_ref(marker)}. Nobody will answer the prompt. Instead:\n"
		f"{_instructions(marker)}"
	)


def _append_cap_log(directory: Path, record: dict) -> None:
	try:
		directory.mkdir(parents=True, exist_ok=True)
		with (directory / CAP_LOG_NAME).open("a", encoding="utf-8") as handle:
			handle.write(json.dumps(record, ensure_ascii=False) + "\n")
	except OSError:
		pass


def _warn(reason: str) -> dict:
	return {"systemMessage": f"Unattended question guard skipped: {reason}"}


def evaluate(payload: dict, env: dict, directory: Path, now: datetime) -> dict | None:
	"""Decide the hook outcome for one payload.

	Returns the JSON object to print, or None to allow silently.
	"""
	event = payload.get("hook_event_name")
	is_stop = event == SETTINGS_STOP_EVENT
	is_ask = event == "PreToolUse" and payload.get("tool_name") == SETTINGS_ASK_MATCHER
	if not (is_stop or is_ask):
		return None
	session = env.get(ENV_SESSION)
	if not isinstance(session, str) or not session:
		return None
	try:
		marker = read_marker(session, directory)
	except GuardStateError as exc:
		return _warn(str(exc))
	if marker is None:
		return None

	if is_ask:
		return {
			"hookSpecificOutput": {
				"hookEventName": "PreToolUse",
				"permissionDecision": "deny",
				"permissionDecisionReason": ask_deny_reason(marker),
			}
		}

	entries = load_transcript(payload.get("transcript_path"))
	text = payload.get("last_assistant_message")
	if not isinstance(text, str) or not text.strip():
		text = final_assistant_text(entries)
	kind = question_kind(text)
	if kind is None:
		return None
	if blocked_comment_posted(current_turn(entries), marker):
		return None
	try:
		blocks = read_blocks(session, directory)
		if blocks >= STOP_BLOCK_CAP:
			_append_cap_log(
				directory,
				{
					"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
					"event": "cap_reached",
					"session": session,
					"repo": marker.get("repo"),
					"issue": marker.get("issue"),
					"kind": kind,
					"blocks": blocks,
				},
			)
			return {
				"systemMessage": (
					f"{CAP_MESSAGE_PREFIX} session={session} issue={_issue_ref(marker)} blocks={blocks}: the "
					f"session ended its turn on {kind} without posting an ai:claude-blocked comment; the stop is "
					"allowed and nothing was posted on the issue."
				)
			}
		write_blocks(session, directory, blocks + 1)
	except GuardStateError as exc:
		return _warn(str(exc))
	return {"decision": "block", "reason": stop_reason(marker, kind, blocks + 1)}


def run_hook(stdin) -> int:
	try:
		raw = stdin.read()
	except (OSError, ValueError):
		print(json.dumps(_warn("could not read the hook payload")))
		return 0
	if not raw.strip():
		return 0
	try:
		payload = json.loads(raw)
	except ValueError:
		print(json.dumps(_warn("hook payload is not valid JSON")))
		return 0
	if not isinstance(payload, dict):
		print(json.dumps(_warn("hook payload is not a JSON object")))
		return 0
	try:
		result = evaluate(payload, dict(os.environ), marker_dir(), datetime.now(timezone.utc))
	except Exception as exc:  # noqa: BLE001 - the guard must never break the session
		result = _warn(f"internal error ({exc})")
	if result is not None:
		print(json.dumps(result, ensure_ascii=False))
	return 0


def run_mark(argv: list[str]) -> int:
	parser = argparse.ArgumentParser(prog="unattended_question_guard.py mark")
	parser.add_argument("--repo", required=True, help="<owner>/<repo> of the source issue")
	parser.add_argument("--issue", required=True, type=int, help="source issue number")
	args = parser.parse_args(argv)
	if not _REPO_RE.match(args.repo) or args.issue <= 0:
		print(json.dumps({"marked": False, "error": "expected --repo <owner>/<repo> and a positive --issue"}))
		return 1
	session = os.environ.get(ENV_SESSION, "")
	if not session:
		print(json.dumps({"marked": False, "reason": f"{ENV_SESSION} is not set (not a cloud session)"}))
		return 0
	try:
		marker = write_marker(args.repo, args.issue, session, marker_dir(), datetime.now(timezone.utc))
	except OSError as exc:
		print(json.dumps({"marked": False, "error": f"could not write the marker ({exc})"}))
		return 1
	print(json.dumps({"marked": True, "path": str(marker_path(session, marker_dir())), **marker}))
	return 0


def main(argv: list[str] | None = None) -> int:
	args = sys.argv[1:] if argv is None else argv
	if args and args[0] == "mark":
		return run_mark(args[1:])
	if args:
		print(json.dumps({"error": f"unknown subcommand {args[0]!r}; expected 'mark'"}))
		return 1
	return run_hook(sys.stdin)


if __name__ == "__main__":
	sys.exit(main())

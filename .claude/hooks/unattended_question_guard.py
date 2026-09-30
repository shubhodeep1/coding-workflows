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
  permissions, and no tool call in the current turn posted an
  `ai:claude-blocked:v1` comment, the stop is blocked with a reason that
  restates §28. At most STOP_BLOCK_CAP blocks per session; after that the
  stop is allowed with a `systemMessage` starting CAP_MESSAGE_PREFIX and one
  JSON line in `~/.claude/unattended-issue-mode/stop-guard.jsonl`, and the
  hook publishes the source-issue blocker itself (see "Cap blocker" below),
  so a stop past the cap never leaves the chain stalled silently (#5083).
- `PreToolUse` on `AskUserQuestion`: always denied, with the same reason.

Only **marked** sessions are affected. The issue-mode preflight of
`/implement-issue-claude` (step 0) and `/implement-plan-claude` (step 1)
runs this file's `mark` subcommand, which writes
`~/.claude/unattended-issue-mode/<CLAUDE_CODE_REMOTE_SESSION_ID>.json`. A
session is marked when `CLAUDE_CODE_REMOTE_SESSION_ID` is set and a marker
for exactly that id exists. Interactive and local sessions have no marker (a
local session has no `CLAUDE_CODE_REMOTE_SESSION_ID` at all), so the hook
prints nothing for them and allows everything.

Below the cap the hook makes no GitHub API calls (§15): the blocked-comment
check reads the local session transcript (`transcript_path`).

Cap blocker (issue #5083). At the cap, `publish_cap_blocker` posts one
comment on the marker's issue that starts with BLOCKED_COMMENT_MARKER and
carries `<!-- ai:unattended-guard-cap:v1 session=<id> -->`, and adds the
`ai:claude-blocked` label. Its only calls, all through `gh api` against the
marker's validated `<owner>/<repo>` and issue number:
  - GET  repos/<repo>/issues/<N>/comments (paginated, 100 per page), to skip
    the POST when this session's cap marker is already there (idempotent);
  - POST repos/<repo>/issues/<N>/comments with a fixed template that never
    contains model-written text;
  - POST repos/<repo>/issues/<N>/labels with `ai:claude-blocked`.
Up to CAP_BLOCKER_ATTEMPTS attempts per hook run, backing off
CAP_BLOCKER_BACKOFF_SECONDS, each call bounded by CAP_BLOCKER_CALL_TIMEOUT
and the run by CAP_BLOCKER_BUDGET_SECONDS (inside the 30 s wiring timeout).
A publish that still fails leaves `cap_blocker: pending` in the state file
and is retried at the start of every later `Stop` in the session, question
or not. Every outcome appends one `cap_blocker_*` line to the cap log. A
failed publish never blocks the stop and never raises.

It fails open with a
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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


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

# Cap blocker (#5083): the source-issue blocker the hook publishes at the cap.
CAP_BLOCKER_MARKER_PREFIX = "<!-- ai:unattended-guard-cap:v1 session="
CAP_BLOCKER_LABEL = "ai:claude-blocked"
CAP_BLOCKER_ATTEMPTS = 3
CAP_BLOCKER_BACKOFF_SECONDS = (1.0, 2.0)
CAP_BLOCKER_CALL_TIMEOUT = 6.0
CAP_BLOCKER_BUDGET_SECONDS = 24.0
CAP_BLOCKER_POSTED = "posted"
CAP_BLOCKER_PENDING = "pending"
CAP_BLOCKER_INVALID = "invalid"

# A `gh api` runner: (args after `gh api`, timeout) -> (returncode, stdout, stderr).
GhRunner = Callable[[list, float], "tuple[int, str, str]"]

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


def read_state(session: str, directory: Path) -> dict:
	"""Return the session's state object ({} when there is none)."""
	path = state_path(session, directory)
	if not path.exists():
		return {}
	try:
		state = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError) as exc:
		raise GuardStateError(f"state {path} is unreadable ({exc})") from exc
	if not isinstance(state, dict):
		raise GuardStateError(f"state {path} is unreadable (not a JSON object)")
	return state


def _write_state(session: str, directory: Path, updates: dict) -> None:
	"""Merge `updates` into the state file, keeping every other key."""
	state = read_state(session, directory)
	state.update(updates)
	try:
		directory.mkdir(parents=True, exist_ok=True)
		state_path(session, directory).write_text(json.dumps(state) + "\n", encoding="utf-8")
	except OSError as exc:
		raise GuardStateError(f"state for {session} could not be written ({exc})") from exc


def read_blocks(session: str, directory: Path) -> int:
	path = state_path(session, directory)
	try:
		blocks = int(read_state(session, directory).get("stop_blocks", 0))
	except (ValueError, TypeError) as exc:
		raise GuardStateError(f"state {path} is unreadable ({exc})") from exc
	return max(blocks, 0)


def write_blocks(session: str, directory: Path, blocks: int) -> None:
	_write_state(session, directory, {"stop_blocks": blocks})


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
	if isinstance(value, str):
		return BLOCKED_COMMENT_MARKER in value
	if isinstance(value, dict):
		return any(_contains_marker(item) for item in value.values())
	if isinstance(value, list):
		return any(_contains_marker(item) for item in value)
	return False


def blocked_comment_posted(turn: list[dict]) -> bool:
	"""True when a tool call in `turn` carried the blocked-comment marker and succeeded."""
	candidates: set[str] = set()
	succeeded: set[str] = set()
	for entry in turn:
		content = _content(entry)
		if not isinstance(content, list):
			continue
		for item in content:
			if not isinstance(item, dict):
				continue
			if item.get("type") == "tool_use" and _contains_marker(item.get("input")):
				candidates.add(str(item.get("id")))
			elif item.get("type") == "tool_result" and not item.get("is_error"):
				succeeded.add(str(item.get("tool_use_id")))
	return bool(candidates & succeeded)


def _issue_ref(marker: dict) -> str:
	return f"{marker.get('repo', '<owner>/<repo>')}#{marker.get('issue', '<N>')}"


def _instructions(marker: dict) -> str:
	issue = marker.get("issue", "<N>")
	return (
		"- An intent or design question (scope, behaviour, an edge case, an interface, an ambiguous plan step): "
		"take its RECOMMENDED option, record it as an `AD-<n>` entry in the progress log's `## Auto-decisions` "
		"(CLAUDE.md §28.B, §28.D), and continue the work.\n"
		"- A §28.C item (a failure escalation or cap, an ask-first operation under §22.B / §23.C / §24.D, a "
		"protected-path edit, a question with no option that satisfies §28.B): post ONE comment on issue "
		f"#{issue} starting `{BLOCKED_COMMENT_MARKER}` that names the blocker, the options, and the recommended "
		"one; add the `ai:claude-blocked` label; send one PushNotification; then end the turn.\n"
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


def run_gh_api(args: list, timeout: float) -> "tuple[int, str, str]":
	"""Run `gh api <args>` without a shell; never raises."""
	try:
		proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=timeout, check=False)
	except subprocess.TimeoutExpired:
		return 124, "", f"gh api timed out after {timeout:g}s"
	except OSError as exc:
		return 127, "", f"gh could not run ({exc})"
	return proc.returncode, proc.stdout or "", proc.stderr or ""


def cap_blocker_marker(session: str) -> str:
	return f"{CAP_BLOCKER_MARKER_PREFIX}{_stem(session)} -->"


def cap_blocker_body(marker: dict, session: str, kind: str, blocks: int) -> str:
	"""The fixed cap-blocker comment. It never includes model-written text."""
	issue = marker.get("issue")
	return (
		f"{BLOCKED_COMMENT_MARKER}\n"
		f"{cap_blocker_marker(session)}\n"
		"🛑 **Unattended session stopped on an unanswered question** (CLAUDE.md §28.G)\n\n"
		f"Session `{_stem(session)}` ended its turn on {kind} without posting a blocker here. "
		f"The unattended question guard blocked {blocks} such stops, its cap, and then had to allow the stop, "
		f"so the chain for #{issue} is not moving.\n\n"
		"This comment was posted by the guard hook, not written by the session: open the session to read "
		"the question it ended on.\n\n"
		"To resume: answer the question here, then comment `/reclarify`."
	)


def _gh_error(stderr: str, returncode: int) -> str:
	lines = [line.strip() for line in stderr.splitlines() if line.strip()]
	return lines[-1][:200] if lines else f"exit {returncode}"


def publish_cap_blocker(
	marker: dict,
	session: str,
	kind: str,
	blocks: int,
	directory: Path,
	now: datetime,
	runner: GhRunner = run_gh_api,
	sleep: Callable[[float], None] = time.sleep,
	clock: Callable[[], float] = time.monotonic,
) -> dict:
	"""Publish the source-issue blocker for `session`, idempotently and with retries.

	Input: the validated marker (`repo`, `issue`), the session id, the stop's
	kind, and the block count. Output: {"status": "posted" | "exists" |
	"already_posted" | "failed" | "invalid", "attempts": n, "error": str | None}.
	API calls per attempt: one GET per 100 issue comments, at most one
	comment POST, one label POST; at most CAP_BLOCKER_ATTEMPTS attempts within
	CAP_BLOCKER_BUDGET_SECONDS. Fail open: never raises; a failure is stored
	as `cap_blocker: pending` so the next `Stop` retries it.
	"""
	state = read_state(session, directory)
	if state.get("cap_blocker") == CAP_BLOCKER_POSTED:
		return {"status": "already_posted", "attempts": 0, "error": None}
	repo = marker.get("repo")
	issue = marker.get("issue")
	if not isinstance(repo, str) or not _REPO_RE.match(repo) or not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
		_write_state(session, directory, {"cap_blocker": CAP_BLOCKER_INVALID})
		outcome = {"status": "invalid", "attempts": 0, "error": "marker has no valid <owner>/<repo> and issue"}
	else:
		outcome = _publish_attempts(repo, issue, cap_blocker_body(marker, session, kind, blocks), cap_blocker_marker(session), runner, sleep, clock)
		if outcome["status"] in ("posted", "exists"):
			_write_state(session, directory, {"cap_blocker": CAP_BLOCKER_POSTED})
		else:
			_write_state(session, directory, {"cap_blocker": CAP_BLOCKER_PENDING, "cap_blocker_kind": kind})
	_append_cap_log(
		directory,
		{
			"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
			"event": f"cap_blocker_{outcome['status']}",
			"session": session,
			"repo": repo,
			"issue": issue,
			"attempts": outcome["attempts"],
			"error": outcome["error"],
		},
	)
	return outcome


def _publish_attempts(repo: str, issue: int, body: str, cap_marker: str, runner: GhRunner, sleep, clock) -> dict:
	base = f"repos/{repo}/issues/{issue}"
	deadline = clock() + CAP_BLOCKER_BUDGET_SECONDS
	comment_done = False
	found_existing = False
	error = None
	attempts = 0
	for attempt in range(CAP_BLOCKER_ATTEMPTS):
		if attempt:
			delay = CAP_BLOCKER_BACKOFF_SECONDS[min(attempt - 1, len(CAP_BLOCKER_BACKOFF_SECONDS) - 1)]
			if clock() + delay + CAP_BLOCKER_CALL_TIMEOUT > deadline:
				break
			sleep(delay)
		attempts += 1
		if not comment_done:
			code, out, err = runner(["--paginate", f"{base}/comments?per_page=100", "--jq", ".[].body"], CAP_BLOCKER_CALL_TIMEOUT)
			if code != 0:
				error = f"reading comments failed: {_gh_error(err, code)}"
				continue
			if cap_marker in out:
				comment_done = found_existing = True
			else:
				code, _out, err = runner([f"{base}/comments", "-f", f"body={body}"], CAP_BLOCKER_CALL_TIMEOUT)
				if code != 0:
					error = f"posting the comment failed: {_gh_error(err, code)}"
					continue
				comment_done = True
		code, _out, err = runner([f"{base}/labels", "-f", f"labels[]={CAP_BLOCKER_LABEL}"], CAP_BLOCKER_CALL_TIMEOUT)
		if code != 0:
			error = f"adding the {CAP_BLOCKER_LABEL} label failed: {_gh_error(err, code)}"
			continue
		return {"status": "exists" if found_existing else "posted", "attempts": attempts, "error": None}
	return {"status": "failed", "attempts": attempts, "error": error or "no attempt fit in the time budget"}


def _cap_blocker_summary(outcome: dict, issue_ref: str) -> str:
	status = outcome["status"]
	if status == "posted":
		return f"the guard posted the blocker on {issue_ref} (comment and {CAP_BLOCKER_LABEL} label)"
	if status == "exists":
		return f"this session's blocker was already on {issue_ref}; the {CAP_BLOCKER_LABEL} label is set"
	if status == "already_posted":
		return f"the blocker for this session is already on {issue_ref}"
	if status == "invalid":
		return f"nothing was posted: {outcome['error']}"
	return (
		f"publishing the blocker on {issue_ref} failed after {outcome['attempts']} attempt(s) ({outcome['error']}); "
		"it is retried at the next stop in this session"
	)


def _warn(reason: str) -> dict:
	return {"systemMessage": f"Unattended question guard skipped: {reason}"}


def evaluate(
	payload: dict,
	env: dict,
	directory: Path,
	now: datetime,
	runner: GhRunner = run_gh_api,
	sleep: Callable[[float], None] = time.sleep,
	clock: Callable[[], float] = time.monotonic,
) -> dict | None:
	"""Decide the hook outcome for one payload.

	Returns the JSON object to print, or None to allow silently. `runner`,
	`sleep`, and `clock` are used only by the cap blocker (tests inject them).
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

	# A cap blocker that failed earlier in this session is retried at every
	# later stop, question or not (#5083).
	retried = None
	try:
		state = read_state(session, directory)
		if state.get("cap_blocker") == CAP_BLOCKER_PENDING:
			pending_kind = state.get("cap_blocker_kind")
			retried = publish_cap_blocker(
				marker,
				session,
				pending_kind if isinstance(pending_kind, str) else KIND_QUESTION,
				read_blocks(session, directory),
				directory,
				now,
				runner,
				sleep,
				clock,
			)
	except GuardStateError as exc:
		return _warn(str(exc))
	retry_message = None
	if retried is not None:
		retry_message = {"systemMessage": f"unattended-question-guard: cap blocker retry: {_cap_blocker_summary(retried, _issue_ref(marker))}."}

	entries = load_transcript(payload.get("transcript_path"))
	text = payload.get("last_assistant_message")
	if not isinstance(text, str) or not text.strip():
		text = final_assistant_text(entries)
	kind = question_kind(text)
	if kind is None:
		return retry_message
	if blocked_comment_posted(current_turn(entries)):
		return retry_message
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
			outcome = retried or publish_cap_blocker(marker, session, kind, blocks, directory, now, runner, sleep, clock)
			return {
				"systemMessage": (
					f"{CAP_MESSAGE_PREFIX} session={session} issue={_issue_ref(marker)} blocks={blocks}: the "
					f"session ended its turn on {kind} without posting an ai:claude-blocked comment; the stop is "
					f"allowed and {_cap_blocker_summary(outcome, _issue_ref(marker))}."
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

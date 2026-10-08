#!/usr/bin/env python3
"""PreToolUse guard for merged-PR writes and allowlisted API mutations.

Implements CLAUDE.md §21 and protects the §22/§24 API permission allowlist.

Long-lived Claude Code sessions (days to weeks) outlive the PRs they open. When
the PR for the working branch merges mid-session, further commits to that same
branch land on history that is already in the default branch and no longer has
an open PR carrying it anywhere — the work is silently stranded. Prose
instructions do not survive that timescale; this hook does, because the harness
runs it on every Bash tool call regardless of what the model remembers.

Each guarded Bash git invocation is checked in its own effective repository:
a preceding resolvable cd, git -C, and git-directory/work-tree overrides are
applied without executing the Bash text. Pushes with explicit branch refspecs
are checked against the destination branch and the source commit, including
when the source is a detached HEAD. Unresolved env-wrapped commit directories
require confirmation rather than checking the wrong repository. Other unknown
directories warn and check the session checkout; unresolved directory-changing
commits and pushes then require confirmation. Unresolvable explicit push targets
also require confirmation.
Repeated targets share a PR snapshot
per repository and branch, while different source tips are checked separately.
A `cd` or `exit` with a redirect that might fail (anything but a plain
`/dev/null` target) makes the directory unknown. After checking the session
checkout, a push in an unknown directory asks for confirmation.

Detection rule — all three conditions must hold before the command is blocked:

  1. A *merged* PR exists whose head ref is the judged branch, AND
  2. no *open* PR exists for that branch, AND
  3. that merged PR's head commit is an ancestor of the source tip — the pending
     commit would literally stack on already-merged history.

Condition 3 is what makes the guard self-clearing. The branch name is reused
after a `git checkout -B <branch> origin/<default>` reset, so the merged PR
keeps matching `--head <branch>` forever; ancestry is what actually
distinguishes "stacking on a corpse" from "fresh work that happens to reuse the
name". It also means the guard stops firing the moment the branch is reset,
without needing a manual override flag or a new PR to exist yet.

Condition 3 is refined for repositories that merge with merge commits. There
the merged head *is* an ancestor of the default branch, so after the reset it
is an ancestor of HEAD too and plain ancestry would block the very remediation
§21.A prescribes. The guard therefore also asks where the branch forks off the
default branch: a fork point on the default branch's first-parent chain means
the branch was rebuilt from the default branch (allow); a fork point on merged
side history means the branch is still sitting on the corpse (block).

When GitHub cannot be reached (CLAUDE.md §21.C) — no `gh`, expired token,
network failure, or Claude Code Web's agent proxy answering HTTP 403 — the
guard does not silently allow. It falls back to git history alone, fetching
the default branch through the git remote (which works in exactly the sessions
where the API does not):
  - branch sits on merge-commit side history already in the default branch and
    origin holds a tip fully contained in the default branch → BLOCK, same
    remediation as the API path;
  - anything else is inconclusive (an absent remote ref could be deleted or
    never pushed, and a squash- or rebase-merged PR leaves no git trace) → a
    `git push` or an MCP push asks the human to confirm; a bare `git commit` is
    allowed with a warning, since the work strands only on push.
Only the cases where nothing branch-shaped can be checked at all — unparseable
payload, not a git repo, detached HEAD, underivable `<owner>/<repo>` — still
allow with a warning.

The same check guards the GitHub MCP push tools (`mcp__github__push_files`,
`mcp__github__create_or_update_file`), which write to a remote branch without
touching the local checkout: the remote branch tip is fetched and takes the
role HEAD plays for `git commit`/`git push`. When the target repository is not
the local checkout, ancestry cannot be verified and a merged-PR match (or an
unreachable API) asks instead of blocking.

PR state is read over REST (`gh api repos/<slug>/pulls`) rather than
`gh pr list`, because the latter is GraphQL-backed and Claude Code Web's agent
proxy serves only a pinned set of GraphQL operations — the guard would fail open
on every commit in precisely the sessions it exists to protect. `gh pr list`
remains as a transport fallback.

Exit codes (Claude Code hook protocol):
  0 — allow the command. A warning may be emitted via `systemMessage`, or a
      `permissionDecision: ask` may route the call through the harness prompt.
  2 — block the command; stderr is fed back to Claude as the reason.
The hook prints at most one JSON object on stdout per call, merging warnings
and confirmation reasons into that object.

Escape hatch: set CLAUDE_PR_MERGE_GUARD=off to disable only the merged-PR check.
The API-write confirmation safeguard remains active.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import NamedTuple

# Guarded git subcommands. `push` is included alongside `commit` because
# amend/rebase flows reach the remote without issuing a fresh `git commit`.
GUARDED_SUBCOMMANDS = frozenset({"commit", "push"})

# GitHub MCP tools that push commits to a remote branch without a local
# `git push`. Their `tool_input` carries `owner`, `repo` and `branch`.
MCP_PUSH_TOOLS = frozenset({"mcp__github__push_files", "mcp__github__create_or_update_file"})

# Verdicts of the API-free git-history fallback (see git_history_verdict).
VERDICT_STRANDED = "stranded"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_UNAVAILABLE = "unavailable"

# git global options that consume a following argument when not given as
# `--opt=value`. Needed so `git -C /repo commit` resolves to `commit` rather
# than to the path.
GIT_GLOBAL_OPTS_WITH_VALUE = frozenset(
	{"-C", "-c", "--config-env", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path"}
)

# Shell punctuation we treat as command separators when tokenizing a Bash line.
_SHELL_PUNCTUATION_CHARS = ";&|\n<>"
_FD_PREFIX_REDIRECT_OPERATORS = frozenset({"<", ">", ">>", ">|", "<>", ">&", "<&", "<<", "<<<"})
_SHELL_CONTROL_PREFIXES = frozenset({"if", "then", "elif", "else", "do", "while", "until", "{", "(", "!"})
_SHELL_WORD_DELIMITERS = frozenset(" \t\r" + _SHELL_PUNCTUATION_CHARS)
# Commands that run a heredoc body as shell text; such a body stays visible to
# the git parser. Any other heredoc body (cat, python3, `git commit -F -`) is
# data and is removed before parsing, so prose such as `it's` cannot make the
# whole command unparseable.
_SHELL_HEREDOC_READERS = frozenset(
	{
		"bash", "sh", "zsh", "dash", "ksh", "fish", "eval", "source", ".", "ssh", "su", "sudo", "doas",
		"xargs", "parallel", "script",
	}
)
# Wrappers such as `env`, `timeout` or `nohup` are not listed: they run the
# next word, so `env bash <<EOF` still matches `bash`, while `env cat <<EOF`
# stays data.
_SHELL_HEREDOC_READER_RE = re.compile(
	r"(?:^|[\s;&|(`'\"])(?:\S*/)?(?:" + "|".join(re.escape(word) for word in sorted(_SHELL_HEREDOC_READERS)) + r")(?:[\s;&|)`'\"]|$)"
)
_HEREDOC_OPERATOR_RE = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
# Characters that may end a heredoc delimiter word; anything else (`<<EOF-1`,
# `<<E"OF"`) means the delimiter was not fully read.
_HEREDOC_DELIMITER_END = frozenset(" \t;|&<>()`")
# Shell wrappers whose script runs Git out of the hook's sight (security
# finding merged-pr-guard-misses-wrapped-git). A literal `-c` / `eval` script
# or `$(...)` / backtick / `<(...)` body is parsed and gets the normal check;
# text that cannot be read safely and could run a Git write asks instead.
# Prefix wrappers (`sudo`, `timeout`, `xargs`) and `... | bash` stay unparsed.
_SHELL_WRAPPER_INTERPRETERS = frozenset({"bash", "sh", "zsh", "dash"})
_MAX_SHELL_WRAPPER_DEPTH = 3
_MAX_SHELL_SCAN_NESTING = 32
_UNPARSED_SHELL_WRAPPER = "unparsed shell wrapper"

_API_WRITE_METHODS = frozenset({"PUT", "POST", "PATCH"})
_API_WRITE_URL_PREFIXES = (
	"https://api.digitalocean.com/",
	"https://api.cloudflare.com/",
)
_API_WRITE_ALWAYS_CONFIRM_URL_PREFIXES = ("https://api.digitalocean.com/",)
_CF_SESSION_CREDENTIAL_ENV_VARS = ("FUNTOKEN_IO_CF", "FT_GAMES_CF")
_CF_ACCOUNT_ID_RE = re.compile(r"[0-9a-fA-F]{32}")
_CF_WORKER_SCRIPT_URL_RE = re.compile(
	r"https://api\.cloudflare\.com/client/v4/accounts/([0-9a-fA-F]{32})/workers/scripts/([A-Za-z0-9_-]+)((?:/[A-Za-z0-9_-]+)*)"
)
# `-q` must remain curl's first option so ~/.curlrc cannot add hidden transfers.
_API_WRITE_COMMAND_PREFIXES = tuple(
	f"curl -q -sS -X {method} {url_prefix}"
	for method in _API_WRITE_METHODS
	for url_prefix in _API_WRITE_URL_PREFIXES
)
_API_WRITE_VALUE_OPTIONS = frozenset(
	{
		"-H",
		"--header",
		"-d",
		"--data",
		"--data-ascii",
		"--data-binary",
		"--data-raw",
		"--data-urlencode",
		"-F",
		"--form",
		"--form-string",
	}
)
_API_WRITE_INLINE_OPTION_PREFIXES = ("-H", "-d", "-F")

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+/[A-Za-z0-9._-]+$")
_REMOTE_HEAD_BRANCH_RE = re.compile(r"^ref:\s+refs/heads/([^\s]+)\s+HEAD$", re.MULTILINE)

_CACHE_TTL_SECONDS = 300
_CACHE_DIR_NAME = "claude-pr-merge-guard"

# Ceilings, not expected durations — every one of these completes in well under
# a second in practice. They are sized so a pathological run still lands inside
# the hook timeout in .claude/settings.json; overshooting it anyway is safe,
# because a killed hook is a non-blocking failure and so fails open like every
# other unanswerable case.
_GH_TIMEOUT_SECONDS = 15
_GIT_TIMEOUT_SECONDS = 5
# Network-bound git calls (`ls-remote`, `fetch`) used by the history fallback.
_GIT_REMOTE_TIMEOUT_SECONDS = 15
_GIT_ENVIRONMENT: ContextVar[dict[str, str] | None] = ContextVar("guard_git_environment", default=None)
_pending_output: dict[str, list[str]] = {"system_messages": [], "ask_reasons": []}

# Options with an argument must not turn that argument into a refspec. Unknown
# options are treated as uncertain rather than authorizing a different branch.
_PUSH_VALUE_OPTIONS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
_PUSH_BOOLEAN_OPTIONS = frozenset({
	"-u", "--set-upstream", "-f", "--force", "--force-with-lease", "--force-if-includes",
	"--follow-tags", "--atomic", "--dry-run", "-n", "--porcelain", "--quiet", "-q",
	"--verbose", "-v", "--signed", "--no-signed", "--no-verify", "--progress",
	"--ipv4", "--ipv6", "--prune", "--no-prune",
})


class _GitInvocation(NamedTuple):
	cwd: str
	environment: dict[str, str]
	subcommand: str
	arguments: list[str]
	warning: str = ""
	config_override: bool = False
	env_wrapped: bool = False
	env_directory_unresolved: bool = False
	explicit_git_directory: bool = False
	# True when the directory is unknown because of an explicit override the
	# hook could not resolve (an appended GIT_DIR+=/GIT_WORK_TREE+=, an
	# unresolvable -C / env -C, GIT_DIR or --git-dir path), as opposed to shell
	# control flow or an unknown cd. Such a write never falls back to the
	# session checkout: the override names another repository.
	explicit_directory_unresolved: bool = False


class _GuardTarget(NamedTuple):
	cwd: str
	environment: dict[str, str]
	branch: str | None
	tip: str
	reaches_remote: bool
	warning: str = ""
	bulk: str = ""
	remote: str = ""


class LookupUnavailable(Exception):
	"""Raised when PR state cannot be determined; callers must fail open."""


def _run(argv: list[str], cwd: str | None, timeout: int) -> tuple[int, str, str]:
	"""Run a subprocess, returning (returncode, stdout, stderr).

	Never raises for process-level failures — a missing binary or a timeout is
	reported as a non-zero return code so every caller funnels into the same
	fail-open path.
	"""
	try:
		overrides = _GIT_ENVIRONMENT.get()
		proc = subprocess.run(
			argv,
			cwd=cwd,
			capture_output=True,
			text=True,
			timeout=timeout,
			check=False,
			**({"env": {**os.environ, **overrides}} if overrides else {}),
		)
	except FileNotFoundError:
		return 127, "", f"{argv[0]}: not found"
	except subprocess.TimeoutExpired:
		return 124, "", f"{argv[0]}: timed out after {timeout}s"
	except OSError as exc:
		return 126, "", f"{argv[0]}: {exc}"
	return proc.returncode, proc.stdout, proc.stderr


def _shell_segments(command: str) -> list[list[str]]:
	"""Tokenize a shell command into command-sized segments.

	Uses `shlex` over the full command so quoted separators such as `"a; b"`
	stay inside their argument instead of splitting the command early.
	"""
	lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION_CHARS)
	lexer.commenters = ""
	lexer.whitespace = " \t\r"
	lexer.whitespace_split = True
	segments: list[list[str]] = []
	current_segment: list[str] = []
	for token in lexer:
		if token and set(token) <= set(_SHELL_PUNCTUATION_CHARS):
			if current_segment:
				segments.append(current_segment)
				current_segment = []
			continue
		current_segment.append(token)
	if current_segment:
		segments.append(current_segment)
	return segments


def _shell_segments_with_redirects(command: str) -> list[tuple[str, list[str], bool]]:
	"""Return simple commands, their preceding operator and redirect uncertainty.

	This is not a Bash interpreter. Unsupported control flow is marked unknown
	by the caller, never executed to infer an authorization decision.
	"""
	lexer = shlex.shlex(io.StringIO(command), posix=True, punctuation_chars=_SHELL_PUNCTUATION_CHARS)
	lexer.commenters = ""
	lexer.whitespace = " \t\r"
	lexer.whitespace_split = True
	# shlex groups adjacent punctuation (e.g. `>;`), but Bash still sees
	# a redirect without a target followed by a command separator.
	tokens: list[tuple[str, int]] = []
	for raw_token in lexer:
		if raw_token and set(raw_token) <= set(_SHELL_PUNCTUATION_CHARS):
			part_end = lexer.instream.tell() - len(raw_token)
			for part in re.findall(r"&>>|&>|&&|\|\||\|&|>>|>\||>&|<&|<<<|<<|<>|[;<>&|\n]", raw_token):
				part_end += len(part)
				tokens.append((part, part_end))
		else:
			tokens.append((raw_token, lexer.instream.tell()))
	result: list[tuple[str, list[str], bool]] = []
	segment: list[str] = []
	segment_word_end = -1
	operator = ""
	redirect_target: str | None = None
	redirect_may_fail = False
	for token, token_end in tokens:
		if redirect_target is not None and not (token and set(token) <= set(_SHELL_PUNCTUATION_CHARS)):
			if redirect_target not in (">", ">>", ">|", "&>", "&>>", "<") or token != "/dev/null":
				redirect_may_fail = True
			redirect_target = None
			continue
		if redirect_target is not None:
			redirect_may_fail = True
			redirect_target = None
		if token == ">|" or (token and set(token) <= set("<>") | {"&"} and ("<" in token or ">" in token)):
			# Bash &> and &>> take no fd prefix (#6249); keep adjacent digits as
			# arguments for these and unknown redirects so push refspecs are checked.
			if token in _FD_PREFIX_REDIRECT_OPERATORS and segment and segment[-1].isascii() and segment[-1].isdigit():
				# Only an unquoted digit immediately attached to a redirect is an fd.
				redirect_start = token_end - len(token)
				if command[redirect_start:redirect_start + len(token)] != token:
					redirect_start -= 1  # shlex may read one character ahead.
				fd_start = redirect_start - len(segment[-1])
				if fd_start >= 0 and segment_word_end == redirect_start and command[fd_start:redirect_start] == segment[-1] and (fd_start == 0 or command[fd_start - 1] in _SHELL_WORD_DELIMITERS):
					segment.pop()
					segment_word_end = -1
			redirect_target = token
			continue
		if token and set(token) <= set(_SHELL_PUNCTUATION_CHARS):
			if segment:
				result.append((operator, segment, redirect_may_fail))
				segment = []
				segment_word_end = -1
				redirect_may_fail = False
			operator = token
		else:
			segment.append(token)
			# shlex reads one delimiter ahead of a word, so use its raw end.
			segment_word_end = token_end - (token_end > 0 and command[token_end - 1] in _SHELL_WORD_DELIMITERS)
	if redirect_target is not None:
		redirect_may_fail = True
	if segment:
		result.append((operator, segment, redirect_may_fail))
	return result


def _shell_segments_with_operators(command: str) -> list[tuple[str, list[str]]]:
	"""Return simple commands and the operator preceding each one."""
	return [(operator, tokens) for operator, tokens, _ in _shell_segments_with_redirects(command)]


def _strip_data_heredoc_bodies(command: str) -> str:
	"""Remove heredoc bodies that Bash passes on as data, not as shell text.

	The `<<WORD` operator stays, so the command keeps its structure; the body
	and its closing delimiter line are dropped. A body stays when the command
	before `<<` is a shell reader (`bash`, `eval`, `ssh`, ...) or when its
	delimiter is unquoted and the body holds a `$(...)` or backtick
	substitution, because Bash runs that text and git commands in it must
	still be checked. Quote, `$(...)`, `${...}` and arithmetic context is
	tracked so a `<<` inside quotes is not read as a heredoc. A shell reader
	after the operator on the same line (`cat <<EOF | bash`) also keeps the
	body. A delimiter this parser cannot read in full, or a heredoc line
	continued with `\\`, returns the command unchanged.
	"""
	lines = command.split("\n")
	output: list[str] = []
	pending: list[tuple[str, bool, bool, str, str]] = []
	# Each context is [mode, open quote, nesting depth]; a nested $(...) has
	# its own quoting rules, even inside "...".
	contexts: list[list] = [["shell", None, 0]]
	index = 0
	while index < len(lines):
		line = lines[index]
		output.append(line)
		position = 0
		while position < len(line):
			character = line[position]
			mode, quote, depth = contexts[-1]
			if character == "\\" and quote != "'":
				position += 2
				continue
			if quote == "'":
				if character == "'":
					contexts[-1][1] = None
				position += 1
				continue
			if character == quote:
				contexts[-1][1] = None
				position += 1
				continue
			if line.startswith("$((", position):
				contexts.append(["arithmetic", None, 2])
				position += 3
				continue
			if line.startswith("$(", position):
				contexts.append(["shell", None, 0])
				position += 2
				continue
			if line.startswith("${", position):
				contexts.append(["parameter", None, 1])
				position += 2
				continue
			if mode == "parameter":
				if character == "{":
					contexts[-1][2] += 1
				elif character == "}":
					contexts[-1][2] -= 1
					if contexts[-1][2] == 0:
						contexts.pop()
				position += 1
				continue
			if mode == "arithmetic":
				if character == "(":
					contexts[-1][2] += 1
				elif character == ")":
					contexts[-1][2] -= 1
					if contexts[-1][2] == 0:
						contexts.pop()
				position += 1
				continue
			if character == "'" and quote is None:
				contexts[-1][1] = "'"
			elif character == '"':
				contexts[-1][1] = '"' if quote is None else None
			elif quote is None:
				if character == "`":
					if mode == "backtick":
						contexts.pop()
					else:
						contexts.append(["backtick", None, 0])
				elif mode == "shell" and len(contexts) > 1 and character == ")":
					if depth == 0:
						contexts.pop()
					else:
						contexts[-1][2] -= 1
				elif mode == "shell" and len(contexts) > 1 and character == "(":
					contexts[-1][2] += 1
				elif line.startswith("((", position) and (position == 0 or line[position - 1] in " \t;|&("):
					contexts.append(["arithmetic", None, 2])
					position += 2
					continue
				elif character == "#" and (position == 0 or line[position - 1] in " \t;|&()<>"):
					break
				elif line.startswith("<<", position) and not line.startswith("<<<", position) and (position == 0 or line[position - 1] != "<"):
					match = _HEREDOC_OPERATOR_RE.match(line, position)
					# A partly read delimiter (`<<EOF-1`, `<<\\EOF`) cannot be matched
					# to its closing line, so keep every line visible.
					if match is None or (match.end() < len(line) and line[match.end()] not in _HEREDOC_DELIMITER_END):
						return command
					pending.append((match.group(3), match.group(1) == "-", bool(match.group(2)), line[:position], line[match.end():]))
					position = match.end()
					continue
			position += 1
		index += 1
		while pending:
			delimiter, strip_tabs, quoted, prefix, suffix = pending.pop(0)
			if suffix.rstrip().endswith("\\"):
				return command
			body: list[str] = []
			closing: list[str] = []
			while index < len(lines):
				body_line = lines[index]
				index += 1
				if (body_line.lstrip("\t") if strip_tabs else body_line) == delimiter:
					closing.append(body_line)
					break
				body.append(body_line)
			# The rest of the line may pipe the body on (`cat <<EOF | bash`).
			runs_as_shell = bool(_SHELL_HEREDOC_READER_RE.search(prefix) or _SHELL_HEREDOC_READER_RE.search(suffix)) or suffix.rstrip().endswith("|") or (
				not quoted and any("$(" in body_line or "`" in body_line for body_line in body)
			)
			if runs_as_shell:
				output.extend(body + closing)
	return "\n".join(output)


def _command_after_control_prefix(tokens: list[str]) -> tuple[list[str], bool]:
	"""Expose a command behind shell control words without trusting its cwd."""
	control_prefix_seen = False
	while tokens:
		if tokens[0] in _SHELL_CONTROL_PREFIXES or tokens[0].endswith(")"):
			tokens = tokens[1:]
		elif tokens[0] == "case":
			for position, word in enumerate(tokens[3:], start=3):
				if word.endswith(")"):
					tokens = tokens[position + 1:]
					break
			else:
				break
		else:
			break
		control_prefix_seen = True
	return tokens, control_prefix_seen


class _ShellScan(NamedTuple):
	end: int
	bodies: list[str]
	heredoc_spans: list[tuple[int, int]]
	unreadable: bool


def _mentions_git_write(text: str) -> bool:
	"""Whether text could name a Git commit or push, also with quotes removed."""
	for candidate in (text, re.sub(r"[\"'\\]", "", text)):
		if re.search(r"\bgit\b", candidate) and re.search(r"\b(?:push|commit)\b", candidate):
			return True
	return False


def _scan_shell_text(text: str, position: int = 0, closing: str = "", nesting: int = 0) -> _ShellScan:
	"""Find substitution bodies and heredoc bodies in shell text, without running it.

	Collects `$(...)`, backtick and unquoted `<(...)` / `>(...)` bodies, skips
	`$((` arithmetic, and records heredoc body spans (terminator line included).
	`closing` is ")" inside a substitution, so the scan stops at its matching
	parenthesis. `unreadable` reports text that cannot be split reliably: an
	unterminated body, quote or heredoc, a nested backtick, nesting deeper than
	_MAX_SHELL_SCAN_NESTING, or an unquoted heredoc whose expanded body could
	run a Git write.
	"""
	bodies: list[str] = []
	heredoc_spans: list[tuple[int, int]] = []
	if nesting > _MAX_SHELL_SCAN_NESTING:
		return _ShellScan(position, bodies, heredoc_spans, True)
	pending_heredocs: list[tuple[str, bool, bool]] = []
	single_quoted = False
	double_quoted = False
	parenthesis_depth = 0
	length = len(text)
	while position < length:
		character = text[position]
		if single_quoted:
			single_quoted = character != "'"
			position += 1
			continue
		if character == "\\":
			position += 2
			continue
		if character == "\n" and pending_heredocs and not double_quoted:
			position += 1
			for delimiter, strip_tabs, quoted in pending_heredocs:
				body_start = position
				while True:
					if position >= length:
						return _ShellScan(position, bodies, heredoc_spans, True)
					line_end = text.find("\n", position)
					next_position = length if line_end == -1 else line_end + 1
					line = text[position:next_position].rstrip("\n")
					if (line.lstrip("\t") if strip_tabs else line) == delimiter:
						break
					position = next_position
				heredoc_body = text[body_start:position]
				if not quoted and ("$(" in heredoc_body or "`" in heredoc_body) and _mentions_git_write(heredoc_body):
					return _ShellScan(position, bodies, heredoc_spans, True)
				heredoc_spans.append((body_start, next_position))
				position = next_position
			pending_heredocs = []
			continue
		if character == "'" and not double_quoted:
			single_quoted = True
			position += 1
			continue
		if character == '"':
			double_quoted = not double_quoted
			position += 1
			continue
		if character == "`":
			cursor = position + 1
			while cursor < length and text[cursor] != "`":
				if text[cursor] == "\\":
					if text.startswith("`", cursor + 1):
						return _ShellScan(cursor, bodies, heredoc_spans, True)
					cursor += 2
					continue
				cursor += 1
			if cursor >= length:
				return _ShellScan(cursor, bodies, heredoc_spans, True)
			bodies.append(text[position + 1:cursor])
			position = cursor + 1
			continue
		if text.startswith("$((", position):
			# Arithmetic: its parentheses are counted below; a nested `$(` is still found.
			position += 1
			continue
		if text.startswith("$(", position) or (not double_quoted and text.startswith(("<(", ">("), position)):
			inner = _scan_shell_text(text, position + 2, ")", nesting + 1)
			if inner.unreadable:
				return _ShellScan(inner.end, bodies, heredoc_spans, True)
			bodies.append(text[position + 2:inner.end])
			heredoc_spans.extend(inner.heredoc_spans)
			position = inner.end + 1
			continue
		if not double_quoted and text.startswith("<<", position) and not text.startswith("<<<", position):
			cursor = position + 2
			strip_tabs = text.startswith("-", cursor)
			cursor += strip_tabs
			while cursor < length and text[cursor] in " \t":
				cursor += 1
			word_start = cursor
			while cursor < length and text[cursor] not in " \t\n;&|<>()":
				if text[cursor] in "'\"":
					quote_end = text.find(text[cursor], cursor + 1)
					if quote_end == -1:
						return _ShellScan(cursor, bodies, heredoc_spans, True)
					cursor = quote_end + 1
				else:
					cursor += 2 if text[cursor] == "\\" else 1
			word = text[word_start:cursor]
			delimiter = word.replace("\\", "").replace("'", "").replace('"', "")
			if not delimiter:
				return _ShellScan(cursor, bodies, heredoc_spans, True)
			pending_heredocs.append((delimiter, strip_tabs, any(char in word for char in "'\"\\")))
			position = cursor
			continue
		if not double_quoted and character == "(":
			parenthesis_depth += 1
		elif not double_quoted and character == ")":
			if parenthesis_depth == 0 and closing:
				return _ShellScan(position, bodies, heredoc_spans, bool(pending_heredocs))
			parenthesis_depth = max(parenthesis_depth - 1, 0)
		position += 1
	return _ShellScan(
		position, bodies, heredoc_spans,
		bool(closing) or single_quoted or double_quoted or bool(pending_heredocs),
	)


def _wrapped_shell_text(command: str, depth: int) -> tuple[str | None, list[str], bool]:
	"""Prepare shell text for both Git walkers at a given wrapper depth.

	Returns the text to split into segments (None when it must not be split),
	the substitution bodies to inspect at depth + 1, and whether the text must
	be treated as an unparsed Git write. Inner scripts (depth > 0) lose their
	heredoc bodies before splitting, so prose such as a commit message cannot
	break the tokenizer; the top level is split exactly as before.
	"""
	if depth > _MAX_SHELL_WRAPPER_DEPTH:
		return None, [], _mentions_git_write(command) or "$" in command or "`" in command
	scan = _scan_shell_text(command)
	if scan.unreadable:
		return (command if depth == 0 else None), [], _mentions_git_write(command)
	text = command
	if depth:
		for start, end in sorted(scan.heredoc_spans, reverse=True):
			text = text[:start] + text[end:]
	return text, scan.bodies, False


def _shell_interpreter_script(tokens: list[str], index: int) -> tuple[str | None, bool]:
	"""Return (script, recognised) for `bash|sh|zsh|dash ... -c <script>`.

	`recognised` with no script means a `-c` invocation whose script is missing.
	An interpreter without `-c` (a script file or stdin) is not recognised.
	"""
	if index >= len(tokens) or os.path.basename(tokens[index]) not in _SHELL_WRAPPER_INTERPRETERS:
		return None, False
	position = index + 1
	command_mode = False
	while position < len(tokens):
		word = tokens[position]
		if word == "--":
			position += 1
			break
		if word in ("--rcfile", "--init-file"):
			position += 2
		elif word.startswith("--"):
			position += 1
		elif re.fullmatch(r"[-+][A-Za-z]+", word):
			command_mode = command_mode or "c" in word
			position += 1 + word.count("o") + word.count("O")
		else:
			break
	if not command_mode:
		return None, False
	return (tokens[position] if position < len(tokens) else None), True


def _segment_wrapper_script(tokens: list[str], index: int) -> tuple[str | None, bool]:
	"""Return (inner script, unparsed) for an `eval` or shell `-c` segment."""
	if index >= len(tokens):
		return None, False
	if tokens[index] == "eval":
		return " ".join(tokens[index + 1:]), False
	script, recognised = _shell_interpreter_script(tokens, index)
	if recognised and script is None:
		return None, _mentions_git_write(" ".join(tokens))
	return script, False


def _unparsed_shell_wrapper_invocation(checkout: str) -> _GitInvocation:
	return _GitInvocation(checkout, {}, "push", [], _UNPARSED_SHELL_WRAPPER, True)


@contextmanager
def _git_environment(overrides: dict[str, str]):
	state = _GIT_ENVIRONMENT.set(overrides or None)
	try:
		yield
	finally:
		_GIT_ENVIRONMENT.reset(state)


def _literal_guard_path(
	path: str, cwd: str, *, shell_cd: bool = False, git_file: bool = False
) -> str | None:
	"""Resolve a path only when its spelling and destination are unambiguous."""
	if not path or path.startswith("~") or any(char in path for char in "$`*?[]{}()\\\n"):
		return None
	if shell_cd and not os.path.isabs(path) and os.environ.get("CDPATH") and not path.startswith(("./", "../")):
		return None
	resolved = os.path.realpath(os.path.join(cwd, path))
	if os.path.isdir(resolved) and os.access(resolved, os.X_OK):
		return resolved
	if git_file and os.path.isfile(resolved):
		# Linked worktrees have a .git file containing a gitdir pointer. Git
		# accepts that file as GIT_DIR; validate its target before using it.
		try:
			with Path(resolved).open(encoding="utf-8") as pointer_file:
				pointer = pointer_file.read(4097)
		except (OSError, UnicodeError):
			return None
		if len(pointer) <= 4096 and pointer.startswith("gitdir: ") and "\n" not in pointer.strip("\n"):
			actual = os.path.realpath(os.path.join(os.path.dirname(resolved), pointer[8:].strip()))
			if os.path.isdir(actual) and os.access(actual, os.X_OK):
				return resolved
	return None


def _env_wrapped_git_index(tokens: list[str], index: int) -> int:
	"""Skip a simple `env` prefix; return -1 when its command is ambiguous."""
	if index >= len(tokens) or (tokens[index] != "env" and not tokens[index].endswith("/env")):
		return index
	index += 1
	while index < len(tokens):
		word = tokens[index]
		if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=", word) or word in ("-", "-i", "--ignore-environment", "--"):
			index += 1
		elif word in ("-u", "--unset", "-C", "--chdir") and index + 1 < len(tokens):
			index += 2
		elif word.startswith(("--unset=", "--chdir=")) or (word.startswith(("-u", "-C")) and len(word) > 2):
			index += 1
		elif word in ("-S", "--split-string") or word.startswith(("-S", "--split-string=")):
			inline = word not in ("-S", "--split-string")
			if not inline and index + 1 >= len(tokens):
				return -1
			value = word.split("=", 1)[1] if word.startswith("--split-string=") else word[2:] if inline else tokens[index + 1]
			# GNU env expands backslashes and ${VAR} itself; shlex cannot safely
			# predict the resulting command in those cases.
			if "$" in value or "\\" in value:
				return -1
			try:
				split_words = shlex.split(value)
			except ValueError:
				return -1
			if not split_words:
				return -1
			tokens[index:index + (1 if inline else 2)] = split_words
		else:
			return -1 if word.startswith("-") else index
	return index


def _guarded_git_invocations(
	command: str, checkout: str, *, _depth: int = 0, _inherited_unresolved: bool = False
) -> list[_GitInvocation]:
	segment_text, wrapped_bodies, wrapped_unparsed = _wrapped_shell_text(command, _depth)
	invocations: list[_GitInvocation] = []
	if wrapped_unparsed:
		invocations.append(_unparsed_shell_wrapper_invocation(checkout))
	if segment_text is None:
		return invocations
	# The wrapper scan above reads the raw text so quoted heredocs stay intact;
	# their data bodies are dropped only for the segment parser (see
	# _strip_data_heredoc_bodies), so prose such as `it's` cannot break it.
	try:
		segments = _shell_segments_with_redirects(_strip_data_heredoc_bodies(segment_text))
	except ValueError:
		if _depth and _mentions_git_write(command):
			return [_unparsed_shell_wrapper_invocation(checkout)]
		return invocations if _depth else []
	# An inner script started from an unknown directory resolves nothing itself.
	working_directory: str | None = None if _inherited_unresolved else checkout
	conditional_cd = False
	unresolved_directory_change = _inherited_unresolved
	# A cd/pushd/popd earlier in the current `&&`/`||` list: a later `||`
	# branch may run with or without that directory change.
	list_changed_directory = False
	directory_change_seen = False
	for segment_position, (operator, tokens, redirect_may_fail) in enumerate(segments):
		tokens, control_prefix = _command_after_control_prefix(tokens)
		if control_prefix:
			working_directory = None
		if operator in ("", ";", "\n", "&"):
			list_changed_directory = False
		if not tokens:
			continue
		next_operator = segments[segment_position + 1][0] if segment_position + 1 < len(segments) else ""
		# Each pipeline element runs in its own subshell: a directory change
		# inside one ends with it, and every element starts where the list is.
		# `|&` pipes stderr too and is a pipeline separator like `|`.
		in_pipeline = operator in ("|", "|&") or next_operator in ("|", "|&")
		if operator == "||" and tokens[0] == "exit" and working_directory is not None and not redirect_may_fail:
			# If this exit runs the following git cannot; otherwise cd succeeded.
			# A failed builtin redirect means exit did not run (#6289).
			conditional_cd = False
			continue
		if operator not in ("", "&&", "|", "|&") and conditional_cd:
			working_directory = None
			unresolved_directory_change = True
			conditional_cd = False
		# `&` backgrounds the whole previous list. A `||` branch is unknown only
		# after a directory change in its own list, or when it is itself a cd.
		if operator == "&" or (operator == "||" and (list_changed_directory or (tokens[0] == "cd" and not in_pipeline))):
			working_directory = None
		if in_pipeline and tokens[0] in ("cd", "pushd", "popd"):
			continue
		if tokens[0] in ("cd", "pushd", "popd"):
			list_changed_directory = True
		# A cd after a condition may not have happened when a later list starts.
		# A wrapped script (`bash -c`, `eval`) starts where this segment starts.
		segment_start_directory = working_directory
		segment_start_unresolved = unresolved_directory_change
		if tokens[0] == "cd":
			directory_change_seen = True
			operand = tokens[1:]
			if operand[:1] == ["--"]:
				operand = operand[1:]
			# A failed builtin redirect means cd did not run (#6289).
			working_directory = (
				_literal_guard_path(operand[0], working_directory, shell_cd=True)
				if len(operand) == 1 and working_directory is not None and not redirect_may_fail else None
			)
			if working_directory is None:
				unresolved_directory_change = True
			conditional_cd = operator == "&&" or conditional_cd
			continue
		if tokens[0] in ("pushd", "popd", "eval", "source", ".", "(", "{"):
			working_directory = None
			unresolved_directory_change = True
			directory_change_seen = True
		index = 0
		environment: dict[str, str] = {}
		config_override = False
		appended_git_selector = False
		# A prior unresolved cd/pushd may select another repository.
		explicit_git_directory = unresolved_directory_change
		# Bash append assignments are prefixes too; keep the following git visible.
		while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=", tokens[index]):
			name, value = tokens[index].split("=", 1)
			if name.endswith("+"):
				# Appending to an existing Git override depends on the shell state.
				name = name[:-1]
				if name in ("GIT_DIR", "GIT_WORK_TREE"):
					working_directory = None
					explicit_git_directory = True
					appended_git_selector = True
			elif name in ("GIT_DIR", "GIT_WORK_TREE"):
				environment[name] = value
				explicit_git_directory = True
			if name == "GIT_CONFIG" or name.startswith("GIT_CONFIG_"):
				config_override = True
			index += 1
		env_index = index
		index = _env_wrapped_git_index(tokens, index)
		if index == -1:
			if any("git" in word or "$" in word for word in tokens[env_index + 1:]):
				invocations.append(_GitInvocation(checkout, {}, "push", [], "unparsed env wrapper", True))
			continue
		env_wrapped = index != env_index
		env_cwd = working_directory
		explicit_directory_unresolved = False
		env_directory_unresolved = False
		env_chdir_seen = False
		if env_wrapped:
			config_override = True
			for position in range(env_index + 1, index):
				word = tokens[position]
				if word in ("-C", "--chdir") or word.startswith(("-C", "--chdir=")):
					env_chdir_seen = True
					env_word_value = (tokens[position + 1] if word in ("-C", "--chdir") else
						word.split("=", 1)[1] if word.startswith("--chdir=") else word[2:])
					# An absolute -C path does not depend on the (possibly unknown) cwd.
					env_cwd = (_literal_guard_path(env_word_value, env_cwd or checkout)
						if env_cwd or os.path.isabs(env_word_value) else None)
					explicit_directory_unresolved |= env_cwd is None
					env_directory_unresolved |= env_cwd is None
				elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
					env_name, env_word_value = word.split("=", 1)
					if env_name in ("GIT_DIR", "GIT_WORK_TREE"):
						environment[env_name] = env_word_value
		if _depth and index < len(tokens) and ("$" in tokens[index] or "`" in tokens[index]):
			# An expansion in command position inside a wrapper cannot be read.
			invocations.append(_unparsed_shell_wrapper_invocation(checkout))
			continue
		wrapped_script, wrapper_unparsed = _segment_wrapper_script(tokens, index)
		if wrapper_unparsed:
			invocations.append(_unparsed_shell_wrapper_invocation(checkout))
			continue
		if wrapped_script is not None:
			wrapper_directory = env_cwd if index != env_index else segment_start_directory
			wrapper_unresolved = wrapper_directory is None or segment_start_unresolved
			# GIT_DIR / GIT_WORK_TREE given to the wrapper reach its Git commands.
			wrapper_git_location = any(
				re.match(r"^(?:GIT_DIR|GIT_WORK_TREE)\+?=", word) for word in tokens[:index]
			)
			for inner_invocation in _guarded_git_invocations(
				wrapped_script,
				checkout if wrapper_unresolved or wrapper_directory is None else wrapper_directory,
				_depth=_depth + 1,
				_inherited_unresolved=wrapper_unresolved,
			):
				if wrapper_git_location:
					inner_invocation = inner_invocation._replace(
						cwd=checkout, environment={}, explicit_git_directory=True,
						warning=inner_invocation.warning
						or "could not resolve git command directory; checking the session checkout instead",
					)
				if config_override:
					inner_invocation = inner_invocation._replace(config_override=True)
				invocations.append(inner_invocation)
			continue
		if index >= len(tokens) or (tokens[index] != "git" and not tokens[index].endswith("/git")):
			continue
		index += 1
		git_cwd = env_cwd
		uncertain = git_cwd is None or appended_git_selector
		# Git applies an appended GIT_DIR+=/GIT_WORK_TREE+= value, but the hook
		# cannot know the prior value, so the selected repository is unknown.
		explicit_directory_unresolved |= appended_git_selector
		while index < len(tokens) and tokens[index].startswith("-"):
			option = tokens[index]
			if option.startswith(("-C", "--git-dir", "--work-tree")):
				explicit_git_directory = True
			value = None
			if option in GIT_GLOBAL_OPTS_WITH_VALUE:
				if index + 1 >= len(tokens):
					uncertain = True
					break
				value = tokens[index + 1]
				index += 1
			elif option.startswith("-C") and option != "-C":
				value = option[2:]
			elif option.startswith("-c") and option != "-c":
				value = option[2:]
			elif option.startswith(("--git-dir=", "--work-tree=")):
				value = option.split("=", 1)[1]
			if option in ("-c", "--config-env") or option.startswith(("-c", "--config-env=")):
				# Git configuration can rewrite the push destination without changing origin's stored URL.
				config_override = True
			if value is not None:
				if option.startswith("-C"):
					# Git applies an absolute -C on its own, even after an unknown cd.
					git_cwd = (_literal_guard_path(value, git_cwd or checkout)
						if git_cwd or os.path.isabs(value) else None)
					# Git applies an appended GIT_DIR+=/GIT_WORK_TREE+= value on top
					# of the shell's, which the hook cannot read, so the directory
					# stays unresolved whatever -C selects.
					uncertain = git_cwd is None or appended_git_selector
					explicit_directory_unresolved |= uncertain
				elif option.startswith("--git-dir"):
					environment["GIT_DIR"] = value
				elif option.startswith("--work-tree"):
					environment["GIT_WORK_TREE"] = value
			index += 1
		if index >= len(tokens) or tokens[index] not in GUARDED_SUBCOMMANDS:
			continue
		if not uncertain and git_cwd is not None:
			for name, value in environment.items():
				# git -C is applied before relative git-directory/work-tree options.
				path = _literal_guard_path(value, git_cwd, git_file=name == "GIT_DIR")
				if path is None:
					uncertain = True
					explicit_directory_unresolved = True
					break
				environment[name] = path
		invocations.append(_GitInvocation(
			checkout if uncertain else git_cwd or checkout,
			{} if uncertain else environment,
			tokens[index], tokens[index + 1:],
			("could not resolve explicit git push directory; cannot check checkout PR history"
				if (explicit_directory_unresolved or (env_chdir_seen and env_cwd is None)) and tokens[index] == "push" else
			 "could not resolve git command directory (env -C/--chdir); cannot check checkout PR history"
				if env_chdir_seen and env_cwd is None else
			 "could not resolve explicit git command directory"
				if explicit_directory_unresolved and tokens[index] == "commit" else
				"could not resolve git command directory; checking the session checkout instead") if uncertain else "",
			config_override,
			env_wrapped,
			env_directory_unresolved,
			explicit_git_directory,
			explicit_directory_unresolved or (env_chdir_seen and env_cwd is None),
		))
	# Substitution bodies run where their segment runs; after any directory
	# change that position is unknown, so they are checked as unresolved.
	bodies_unresolved = _inherited_unresolved or unresolved_directory_change or directory_change_seen
	for body in wrapped_bodies:
		invocations.extend(_guarded_git_invocations(
			body, checkout, _depth=_depth + 1, _inherited_unresolved=bodies_unresolved,
		))
	return invocations


def _branch_ref(ref: str) -> str | None:
	"""Extract a literal branch ref; None means its meaning is uncertain."""
	if ref.startswith("refs/heads/"):
		ref = ref[len("refs/heads/"):]
	elif ref.startswith("refs/"):
		return ""  # tags and other namespaces do not write a branch
	if (
		not re.fullmatch(r"[A-Za-z0-9_./-]+", ref)
		or ref.startswith(("-", "/", "."))
		or ref.endswith(("/", ".", ".lock"))
		or ".." in ref or "//" in ref or "@{" in ref
	):
		return None
	return ref


def _push_repository_is_location(word: str) -> bool:
	"""True for a URL, scp-style address or path; those are checked by slug."""
	return "://" in word or ":" in word or word.startswith(("/", ".", "~"))


def _push_targets(invocation: _GitInvocation, checkout: str) -> list[_GuardTarget]:
	"""Identify the destination branches and source tips of a git push."""
	if invocation.warning:
		return [_GuardTarget(invocation.cwd, {}, "", "HEAD", True, invocation.warning)]
	positionals: list[str] = []
	remote_provided = False
	remote_value = ""
	bulk = ""
	delete = False
	tags = False
	uncertain = False
	arguments = invocation.arguments
	index = 0
	while index < len(arguments):
		word = arguments[index]
		if word == "--":
			positionals.extend(arguments[index + 1:])
			break
		if word == "-d" or word in ("--delete", "--de", "--del", "--dele", "--delet"):
			delete = True
		elif word == "--no-delete":
			delete = False
		elif word in ("--tags", "--tag", "--ta"):
			tags = True
		elif word == "--no-tags":
			tags = False
		elif word in ("--all", "--mirror", "--branches"):
			bulk = word
		elif word in _PUSH_VALUE_OPTIONS or word in ("--pu", "--push-o", "--rep", "--rece", "--e") or re.fullmatch(r"-[ufnqv]*o", word):
			if word in ("--repo", "--rep"):
				remote_provided = True
			index += 1
			if index >= len(arguments):
				uncertain = True
			elif remote_provided and word in ("--repo", "--rep"):
				remote_value = arguments[index]
		elif re.fullmatch(r"-[ufnqv]*d[ufnqv]*", word):
			delete = True
		elif word.startswith(("--push-option=", "--push-o=", "--pu=", "--repo=", "--rep=", "--receive-pack=", "--rece=", "--exec=", "--e=", "--force-with-lease=")) or (word.startswith("-o") and word != "-o"):
			if word.startswith(("--repo=", "--rep=")):
				remote_provided = True
				remote_value = word.split("=", 1)[1]
		elif word.startswith("-"):
			if word not in _PUSH_BOOLEAN_OPTIONS and not re.fullmatch(r"-[ufnqv]+", word):
				uncertain = True
		else:
			positionals.append(word)
		index += 1
	if remote_provided and not remote_value:
		uncertain = True
	if uncertain:
		return [_GuardTarget(checkout, {}, "", "HEAD", True,
			"could not resolve git push options; destination branch is unknown", remote=remote_value)]
	if remote_provided and positionals and not _push_repository_is_location(positionals[0]):
		# Git uses the positional repository over --repo; an unconfigured
		# name is a repository the guard cannot verify, not a branch.
		with _git_environment(invocation.environment):
			code, _, _ = _run(
				["git", "config", "--get", f"remote.{positionals[0]}.url"],
				invocation.cwd, _GIT_TIMEOUT_SECONDS,
			)
		if code != 0:
			return [_GuardTarget(invocation.cwd, invocation.environment, "", "HEAD", True,
				"could not resolve git push positional repository; destination branch is unknown")]
	# A positional repository overrides --repo; only later positionals are refspecs.
	if positionals:
		remote_value = positionals[0]
	if delete:
		# No merged-PR check for deletions, but a foreign destination still asks.
		return [_GuardTarget(invocation.cwd, invocation.environment, "", "", True, remote=remote_value)] if remote_value and remote_value != "origin" else []
	refspecs = positionals[1:]
	if not refspecs and tags and not bulk:
		return [_GuardTarget(invocation.cwd, invocation.environment, "", "", True, remote=remote_value)] if remote_value and remote_value != "origin" else []
	if not refspecs:
		return [_GuardTarget(invocation.cwd, invocation.environment, "", "HEAD", True, bulk=bulk, remote=remote_value)]
	targets: list[_GuardTarget] = []
	for refspec in refspecs:
		refspec = refspec.removeprefix("+")
		if refspec == ":" or "*" in refspec:
			bulk = "pattern or matching refspec"
			continue
		if ":" in refspec:
			source, destination = refspec.split(":", 1)
			if not source:
				continue  # Branch deletion.
		else:
			source = destination = refspec
			if destination == "HEAD":
				# Git pushes the checked-out branch, not a branch named HEAD.
				with _git_environment(invocation.environment):
					destination = current_branch(invocation.cwd)
		branch = _branch_ref(destination)
		if branch == "":
			continue
		if branch is None:
			targets.append(_GuardTarget(invocation.cwd, invocation.environment, None, source, True, remote=remote_value))
			continue
		targets.append(_GuardTarget(invocation.cwd, invocation.environment, branch, source, True, remote=remote_value))
	if bulk:
		targets.append(_GuardTarget(invocation.cwd, invocation.environment, "", "HEAD", True, bulk=bulk, remote=remote_value))
	if not targets and remote_value and remote_value != "origin":
		# Every refspec was a branch deletion; only the remote needs checking.
		targets.append(_GuardTarget(invocation.cwd, invocation.environment, "", "", True, remote=remote_value))
	return targets


def _contains_shell_substitution(command: str) -> bool:
	"""Return whether Bash would expand command substitution in the string."""
	single_quoted = False
	double_quoted = False
	escaped = False
	for index, character in enumerate(command):
		if escaped:
			escaped = False
			continue
		if character == "\\" and not single_quoted:
			escaped = True
			continue
		if character == "'" and not double_quoted:
			single_quoted = not single_quoted
			continue
		if character == '"' and not single_quoted:
			double_quoted = not double_quoted
			continue
		if not single_quoted and (character == "`" or command.startswith("$(", index)):
			return True
	return False


def _contains_unquoted_shell_expansion(command: str) -> bool:
	"""Return whether Bash expansion can change argv or execute a command."""
	single_quoted = False
	double_quoted = False
	escaped = False
	for index, character in enumerate(command):
		if escaped:
			escaped = False
			continue
		if character == "\\" and not single_quoted:
			escaped = True
			continue
		if character == "'" and not double_quoted:
			single_quoted = not single_quoted
			continue
		if character == '"' and not single_quoted:
			double_quoted = not double_quoted
			continue
		if double_quoted:
			parameter_expansion = re.match(r"\$\{([^}]*)\}", command[index:])
			if command.startswith("$@", index) or (
				parameter_expansion
				and (
					parameter_expansion.group(1).startswith("@")
					or "[@]" in parameter_expansion.group(1)
					or parameter_expansion.group(1).startswith("!")
					or parameter_expansion.group(1).endswith("@P")
				)
			):
				return True
		if not single_quoted and not double_quoted and character in "${*?[~":
			return True
	return False


def _cloudflare_session_account_ids() -> frozenset[str]:
	"""Read only the account IDs from well-formed session credentials."""
	accounts: set[str] = set()
	for name in _CF_SESSION_CREDENTIAL_ENV_VARS:
		account, separator, token = os.environ.get(name, "").partition(":")
		if separator and token and _CF_ACCOUNT_ID_RE.fullmatch(account):
			accounts.add(account.lower())
	return frozenset(accounts)


def _api_write_destination_reason(url: str) -> str | None:
	if url.startswith(_API_WRITE_ALWAYS_CONFIRM_URL_PREFIXES):
		return "DigitalOcean API writes always need confirmation (CLAUDE.md §22.B)."
	if url.startswith("https://api.cloudflare.com/"):
		match = _CF_WORKER_SCRIPT_URL_RE.fullmatch(url)
		if match is None:
			return (
				"Only account-scoped Worker script writes (accounts/<id>/workers/scripts/<name>) "
				"skip confirmation; other Cloudflare writes (DNS, zone, routes, account settings) "
				"need approval (CLAUDE.md §24.D)."
			)
		if match.group(1).lower() not in _cloudflare_session_account_ids():
			return "Cloudflare account in the URL does not match FUNTOKEN_IO_CF or FT_GAMES_CF (or neither is set)."
		if any("secret" in segment.lower() for segment in (match.group(2) + match.group(3)).split("/")):
			return "Worker secret writes need approval (CLAUDE.md §24.D)."
	return None


def _api_write_confirmation_reason(command: str) -> str | None:
	"""Return the reason an API write needs a harness prompt, if any.

	The settings rules are necessarily prefix matches. Keep their silent path
	limited to a Worker script in a session account and canonical curl options.
	Request bodies are not inspected: an upload can still declare secret bindings.
	"""
	noncanonical_reason = "Non-canonical API curl options can override the allowlisted HTTP method or destination."
	try:
		segments = _shell_segments(command)
	except ValueError:
		return noncanonical_reason if command.lstrip().startswith(_API_WRITE_COMMAND_PREFIXES) else None

	if not segments:
		return None
	tokens = segments[0]
	if len(tokens) < 6 or tokens[:4] != ["curl", "-q", "-sS", "-X"]:
		return None
	if tokens[4] not in _API_WRITE_METHODS:
		return None
	if not any(tokens[5].startswith(prefix) for prefix in _API_WRITE_URL_PREFIXES):
		return None
	destination_reason = _api_write_destination_reason(tokens[5])
	if destination_reason is not None:
		return destination_reason
	# The URL token is already host-gated above. Prompt only for expansions that
	# can synthesize shell words before curl sees the approved API URL shape.
	if any(marker in tokens[5] for marker in ("$", "{", "[", "*", "?")):
		return noncanonical_reason
	# Scan only following option text so literal query/path URL characters are
	# not mistaken for value expansions.
	api_write_raw_parts = command.lstrip().split(None, 6)
	if (
		len(segments) != 1
		or _contains_shell_substitution(command)
		or (
			len(api_write_raw_parts) > 6
			and _contains_unquoted_shell_expansion(api_write_raw_parts[6])
		)
	):
		return noncanonical_reason

	index = 6
	while index < len(tokens):
		token = tokens[index]
		if token in _API_WRITE_VALUE_OPTIONS:
			if index + 1 >= len(tokens):
				return noncanonical_reason
			index += 2
			continue
		if any(
			token.startswith(prefix) and len(token) > len(prefix)
			for prefix in _API_WRITE_INLINE_OPTION_PREFIXES
		):
			index += 1
			continue
		if any(
			token.startswith(f"{option}=")
			for option in _API_WRITE_VALUE_OPTIONS
			if option.startswith("--")
		):
			index += 1
			continue
		return noncanonical_reason
	return None


def _api_write_requires_confirmation(command: str) -> bool:
	"""Return whether an allowlisted API write uses non-canonical curl options.

	The settings rules are necessarily prefix matches. Keep their silent path
	limited to one explicit method and destination followed only by headers and
	request-body options; anything capable of changing curl's method, URL, or
	transfer list must go through the normal harness prompt. The destination
	policy also asks for writes outside session-owned Worker scripts.
	"""
	return _api_write_confirmation_reason(command) is not None


def git_subcommands(command: str, *, _depth: int = 0) -> set[str]:
	"""Return the set of git subcommands invoked by a shell command string.

	Only counts `git` when it is the first real token of a shell segment after
	control words, leading `VAR=value` assignments, or a simple `env` wrapper.
	That keeps `man git commit` and
	`echo "git commit"` from tripping the guard, at the cost of missing
	wrapper-prefixed invocations like `sudo git commit` — an acceptable trade,
	since a false block is more disruptive than a missed check on a rare form.

	Shell `-c` scripts (`bash`, `sh`, `zsh`, `dash`), `eval` arguments and
	`$(...)` / backtick / `<(...)` bodies are parsed the same way, up to
	_MAX_SHELL_WRAPPER_DEPTH levels. Wrapped text that cannot be read and could
	run a Git write reports `push`, so the caller asks for confirmation; this
	must stay in step with `_guarded_git_invocations`.
	"""
	found: set[str] = set()
	segment_text, wrapped_bodies, wrapped_unparsed = _wrapped_shell_text(command, _depth)
	if wrapped_unparsed:
		found.add("push")
	if segment_text is None:
		return found
	try:
		segments = _shell_segments_with_operators(segment_text)
	except ValueError:
		# Unbalanced quotes — the command is not something we can read.
		if _depth and _mentions_git_write(command):
			return {"push"}
		return found if _depth else set()
	for _separator, tokens in segments:
		tokens, _ = _command_after_control_prefix(tokens)
		# Drop leading environment assignments (`GIT_DIR=... git commit`),
		# including append assignments such as `COUNT+=1 git push`.
		index = 0
		while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=", tokens[index]):
			index += 1
		env_index = index
		index = _env_wrapped_git_index(tokens, index)
		if index == -1:
			if any("git" in word or "$" in word for word in tokens[env_index + 1:]):
				found.add("push")  # Unparseable env commands must request confirmation.
			continue
		if index >= len(tokens):
			continue
		if _depth and ("$" in tokens[index] or "`" in tokens[index]):
			found.add("push")  # An expansion in command position inside a wrapper.
			continue
		wrapped_script, wrapper_unparsed = _segment_wrapper_script(tokens, index)
		if wrapper_unparsed:
			found.add("push")
			continue
		if wrapped_script is not None:
			found |= git_subcommands(wrapped_script, _depth=_depth + 1)
			continue

		executable = tokens[index]
		if executable != "git" and not executable.endswith("/git"):
			continue

		# Walk past git's global options to the subcommand.
		index += 1
		while index < len(tokens):
			token = tokens[index]
			if not token.startswith("-"):
				found.add(token)
				break
			if token in GIT_GLOBAL_OPTS_WITH_VALUE:
				index += 2
				continue
			index += 1
	for body in wrapped_bodies:
		found |= git_subcommands(body, _depth=_depth + 1)
	return found


def extract_repo_slug(url: str) -> str:
	"""Derive `<owner>/<repo>` from a git remote URL, or "" when it is not
	derivable.

	Mirrors `extract_repo_slug` in .claude/hooks/session-start.sh, including its
	exact host whitelist: only github.com, plus Claude Code Web's local git
	proxy on 127.0.0.1/localhost where the path must start with `git/`. A
	lookalike host such as `evilgithub.com` must not yield a slug, because the
	slug is passed straight to `gh -R` and would otherwise aim the query at an
	unrelated github.com repo.
	"""
	url = url.strip()
	for suffix in ("/", ".git", "/"):
		if url.endswith(suffix):
			url = url[: -len(suffix)]

	if "://" in url:
		rest = url.split("://", 1)[1]
		if "@" in rest:
			rest = rest.rsplit("@", 1)[1]
		if "/" not in rest:
			return ""
		host, path = rest.split("/", 1)
		host = host.split(":", 1)[0]
	elif "@" in url and ":" in url.split("@", 1)[1]:
		rest = url.rsplit("@", 1)[1]
		host, path = rest.split(":", 1)
	else:
		return ""

	if host in ("127.0.0.1", "localhost"):
		if not path.startswith("git/"):
			return ""
		path = path[len("git/") :]
	elif host != "github.com":
		return ""

	if not _SLUG_RE.match(path):
		return ""
	return path


def current_branch(cwd: str) -> str:
	"""Return the checked-out branch name, or "" when detached or not a repo."""
	code, out, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd, _GIT_TIMEOUT_SECONDS)
	if code != 0:
		return ""
	branch = out.strip()
	return "" if branch in ("", "HEAD") else branch


def default_branch(cwd: str) -> str:
	"""Best-effort default branch name; returns "" when it cannot be determined."""
	code, out, _ = _run(
		["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd, _GIT_TIMEOUT_SECONDS
	)
	if code == 0:
		ref = out.strip()
		if ref.startswith("origin/"):
			ref = ref[len("origin/") :]
		if ref:
			return ref
	code, out, _ = _run(
		["env", "GIT_TERMINAL_PROMPT=0", "git", "ls-remote", "--symref", "origin", "HEAD"],
		cwd,
		_GIT_REMOTE_TIMEOUT_SECONDS,
	)
	if code == 0:
		match = _REMOTE_HEAD_BRANCH_RE.search(out)
		if match:
			return match.group(1)
	return ""


def repo_slug(cwd: str) -> str:
	code, out, _ = _run(["git", "config", "--get", "remote.origin.url"], cwd, _GIT_TIMEOUT_SECONDS)
	if code != 0:
		return ""
	return extract_repo_slug(out)


def is_ancestor_of(sha: str, tip: str, cwd: str) -> bool:
	"""True when `sha` is an ancestor of (or equal to) `tip`.

	A commit absent from the local object database cannot be an ancestor of
	anything local, so `git merge-base` erroring out is a legitimate False
	rather than an unknown — no fetch is attempted here.
	"""
	if not sha or not tip:
		return False
	code, _, _ = _run(
		["git", "merge-base", "--is-ancestor", sha, tip], cwd, _GIT_TIMEOUT_SECONDS
	)
	return code == 0


def is_ancestor(sha: str, cwd: str) -> bool:
	"""True when `sha` is an ancestor of (or equal to) HEAD."""
	return is_ancestor_of(sha, "HEAD", cwd)


def merge_base(left: str, right: str, cwd: str) -> str:
	"""Full sha of the merge base of two revisions, or "" when git cannot say."""
	if not left or not right:
		return ""
	code, out, _ = _run(["git", "merge-base", left, right], cwd, _GIT_TIMEOUT_SECONDS)
	return out.strip() if code == 0 else ""


def on_first_parent_chain(sha: str, tip: str, cwd: str) -> bool | None:
	"""Whether `sha` lies on the first-parent chain of `tip`; None when unknown.

	The first-parent chain of a default branch is the sequence of states that
	branch has actually been in. A commit that reached the default branch as
	the *second* parent of a merge commit is merged side history: it is an
	ancestor of the tip, but the branch never pointed at it.

	The walk is bounded by excluding `sha`'s parents: when `sha` is on the
	chain the walk stops right after listing it, and when it is side history
	the walk stops at the fork point where the two lines share ancestors.
	"""
	if not sha or not tip:
		return None
	code, out, _ = _run(
		["git", "rev-list", "--first-parent", tip, f"^{sha}^@"], cwd, _GIT_TIMEOUT_SECONDS
	)
	if code != 0:
		return None
	return sha in out.split()


def remote_branch_tip(branch: str, cwd: str) -> str | None:
	"""Sha origin currently holds for `branch`; "" when origin has no such
	branch; None when origin could not be queried."""
	ref = f"refs/heads/{branch}"
	code, out, _ = _run(
		["env", "GIT_TERMINAL_PROMPT=0", "git", "ls-remote", "--heads", "origin", ref],
		cwd,
		_GIT_REMOTE_TIMEOUT_SECONDS,
	)
	if code != 0:
		return None
	for line in out.splitlines():
		parts = line.split()
		if len(parts) == 2 and parts[1] == ref:
			return parts[0]
	return ""


def fetch_from_origin(refs: list[str], cwd: str) -> bool:
	"""Fetch the named branches from origin so their objects and
	`refs/remotes/origin/<name>` are current. False when the fetch failed."""
	if not refs:
		return True
	code, _, _ = _run(
		["env", "GIT_TERMINAL_PROMPT=0", "git", "fetch", "--quiet", "origin", "--", *refs],
		cwd,
		_GIT_REMOTE_TIMEOUT_SECONDS,
	)
	return code == 0


def _base_ref(base: str) -> str:
	return f"refs/remotes/origin/{base}"


def stacks_on_merged_history(merged_sha: str, tip: str, cwd: str, base: str) -> bool:
	"""Condition 3: does `tip` stack on the merged PR head `merged_sha`?

	Plain ancestry is the whole answer for squash- and rebase-merged PRs: the
	merged head never enters the default branch, so it can only be an ancestor
	of `tip` through this branch's own history. For a merge-commit merge the
	merged head *is* in the default branch, so it is also an ancestor of a
	branch that was correctly rebuilt from the default branch. The fork point
	settles it: rebuilt branches fork off the default branch's first-parent
	chain, stranded ones fork off merged side history.

	Without a usable `refs/remotes/origin/<base>` the refinement cannot run and
	plain ancestry decides, as it did before the refinement existed.
	"""
	contained = is_ancestor(merged_sha, cwd) if tip == "HEAD" else is_ancestor_of(merged_sha, tip, cwd)
	if not contained:
		return False
	if not base:
		return True
	base_ref = _base_ref(base)
	fork_point = merge_base(tip, base_ref, cwd)
	if not fork_point:
		return True
	if not is_ancestor_of(merged_sha, fork_point, cwd):
		# Merged head is absent from the default branch (squash/rebase merge).
		return True
	on_chain = on_first_parent_chain(fork_point, base_ref, cwd)
	if on_chain is None:
		return True
	return not on_chain


def git_history_verdict(tip: str, branch: str, base: str, cwd: str) -> tuple[str, str]:
	"""API-free fallback: what git alone can say about `tip` on `branch`.

	Issues two network-bound git calls (`ls-remote` for the branch, one
	`fetch` for the default branch plus the branch when origin has it) and
	returns (verdict, detail):

	VERDICT_STRANDED: `tip` forks off merged side history of the default
	branch (a merge-commit merge already absorbed this branch) and origin
	holds a tip fully contained in the default branch. Nothing an open PR
	could still be carrying.

	VERDICT_INCONCLUSIVE: git shows nothing wrong, which is also what a
	squash- or rebase-merged PR looks like. The caller must ask rather than
	allow.

	VERDICT_UNAVAILABLE: git itself could not answer (no origin, fetch
	failed, no merge base).

	A branch that legitimately forks off side history — one stacked on another
	branch that has since merged — is reported inconclusive when it keeps
	unmerged commits on origin or has never been pushed there.
	"""
	if not base:
		return VERDICT_UNAVAILABLE, "default branch unknown"
	remote_tip = remote_branch_tip(branch, cwd)
	if remote_tip is None:
		return VERDICT_UNAVAILABLE, "could not list origin's branches"
	fetch_refs = [base] + ([branch] if remote_tip else [])
	if not fetch_from_origin(fetch_refs, cwd):
		return VERDICT_UNAVAILABLE, f"could not fetch origin/{base}"
	base_ref = _base_ref(base)
	fork_point = merge_base(tip, base_ref, cwd)
	if not fork_point:
		return VERDICT_UNAVAILABLE, f"no merge base between the branch and origin/{base}"
	on_chain = on_first_parent_chain(fork_point, base_ref, cwd)
	if on_chain is None:
		return VERDICT_UNAVAILABLE, f"could not walk origin/{base}'s first-parent history"
	if on_chain:
		return (
			VERDICT_INCONCLUSIVE,
			f"the branch forks off origin/{base}'s own history, which is what an "
			f"open branch and a squash- or rebase-merged one both look like",
		)
	if not remote_tip:
		return (
			VERDICT_INCONCLUSIVE,
			f"origin has no branch named `{branch}`, so git cannot distinguish a "
			f"deleted merged branch from a never-pushed branch",
		)
	if is_ancestor_of(remote_tip, base_ref, cwd):
		return (
			VERDICT_STRANDED,
			f"the branch sits on side history that a merge commit already brought "
			f"into origin/{base}, and origin/{branch} ({remote_tip[:12]}) is fully "
			f"contained in origin/{base}",
		)
	return VERDICT_INCONCLUSIVE, f"origin/{branch} carries commits not yet in origin/{base}"


def _cache_path(slug: str, branch: str) -> Path:
	digest = hashlib.sha256(f"{slug}\n{branch}".encode("utf-8")).hexdigest()[:32]
	return Path(tempfile.gettempdir()) / _CACHE_DIR_NAME / f"{digest}.json"


def _read_cache(slug: str, branch: str) -> list[dict] | None:
	path = _cache_path(slug, branch)
	try:
		raw = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None
	if not isinstance(raw, dict):
		return None
	stamped = raw.get("fetched_at")
	if not isinstance(stamped, (int, float)) or time.time() - stamped > _CACHE_TTL_SECONDS:
		return None
	entries = raw.get("pull_requests")
	return entries if isinstance(entries, list) else None


def _write_cache(slug: str, branch: str, pull_requests: list[dict]) -> None:
	path = _cache_path(slug, branch)
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(
			json.dumps({"fetched_at": time.time(), "pull_requests": pull_requests}),
			encoding="utf-8",
		)
	except OSError:
		# A cache we cannot write is a performance loss, never a correctness one.
		pass


def _normalize_rest_pull(entry: dict) -> dict:
	"""Reshape a REST pull object into the `gh pr list --json` field names."""
	head = entry.get("head")
	head_sha = head.get("sha") if isinstance(head, dict) else None
	return {
		"number": entry.get("number"),
		"state": entry.get("state"),
		"url": entry.get("html_url"),
		"title": entry.get("title"),
		"mergedAt": entry.get("merged_at"),
		"headRefOid": head_sha,
	}


def _query_via_rest(slug: str, branch: str, cwd: str) -> list[dict]:
	"""One `gh api` REST call listing every PR whose head ref is `branch`.

	Preferred over `gh pr list` because Claude Code Web's agent proxy serves
	only a pinned set of GraphQL operations and rejects the rest — and
	`gh pr list` is GraphQL-backed, so it fails there with HTTP 403 while REST
	continues to work. Without this path the guard would fail open on every
	commit in exactly the long-running web sessions it exists to protect.

	The `head` filter wants `<head-owner>:<branch>`; the owner is taken from
	the slug, which is correct for same-repo branches (the only kind this
	guard's remediation produces). A fork-owned head ref simply returns no
	match, which fails open — the safe direction.
	"""
	owner = slug.split("/", 1)[0]
	code, out, err = _run(
		[
			"gh",
			"api",
			"-X",
			"GET",
			f"repos/{slug}/pulls",
			"-f",
			"state=all",
			"-f",
			f"head={owner}:{branch}",
			"-f",
			"per_page=20",
		],
		cwd,
		_GH_TIMEOUT_SECONDS,
	)
	if code != 0:
		raise LookupUnavailable((err or out).strip() or f"gh api exited {code}")
	try:
		parsed = json.loads(out)
	except ValueError as exc:
		raise LookupUnavailable(f"unparseable gh api output: {exc}") from exc
	if not isinstance(parsed, list):
		raise LookupUnavailable("unexpected gh api output shape")
	return [_normalize_rest_pull(entry) for entry in parsed if isinstance(entry, dict)]


def _query_via_pr_list(slug: str, branch: str, cwd: str) -> list[dict]:
	"""Fallback for environments where `gh api` is gated but GraphQL is not."""
	code, out, err = _run(
		[
			"gh",
			"pr",
			"list",
			"-R",
			slug,
			"--head",
			branch,
			"--state",
			"all",
			"--json",
			"number,state,url,title,mergedAt,headRefOid",
			"--limit",
			"20",
		],
		cwd,
		_GH_TIMEOUT_SECONDS,
	)
	if code != 0:
		raise LookupUnavailable((err or out).strip() or f"gh pr list exited {code}")
	try:
		parsed = json.loads(out)
	except ValueError as exc:
		raise LookupUnavailable(f"unparseable gh output: {exc}") from exc
	if not isinstance(parsed, list):
		raise LookupUnavailable("unexpected gh output shape")
	return parsed


def query_pull_requests(slug: str, branch: str, cwd: str) -> list[dict]:
	"""Fetch every PR whose head ref is `branch`.

	Batching contract (CLAUDE.md §15):
	  - Input:  `<owner>/<repo>` slug + head branch name.
	  - Output: list of dicts with `number`, `state`, `url`, `title`,
	            `mergedAt`, `headRefOid` — REST responses are normalized to
	            these `gh pr list --json` names so callers see one shape.
	  - Cost:   one API call. A single `state=all` request serves both the
	            merged and open questions the caller asks; splitting them
	            would double the cost for no extra information. A second call
	            is issued only when the REST path itself errors, as a
	            transport fallback — never as a second query.
	  - Fail-open: raises LookupUnavailable when neither transport can answer,
	            and every caller allows the command on that exception.
	"""
	try:
		return _query_via_rest(slug, branch, cwd)
	except LookupUnavailable as rest_error:
		try:
			return _query_via_pr_list(slug, branch, cwd)
		except LookupUnavailable as graphql_error:
			raise LookupUnavailable(f"REST: {rest_error}; GraphQL: {graphql_error}") from None


def blocking_pull_request(
	pull_requests: list[dict], cwd: str, base: str = "", tip: str = "HEAD"
) -> dict | None:
	"""Apply the three-condition detection rule; return the offending PR or None.

	`tip` is the commit the pending write would stack on: HEAD for
	`git commit`/`git push`, the fetched remote branch tip for an MCP push.
	`base` enables the merge-commit refinement in stacks_on_merged_history.
	"""
	merged = [pr for pr in pull_requests if pr.get("mergedAt")]
	if not merged:
		return None
	if any(str(pr.get("state", "")).upper() == "OPEN" for pr in pull_requests):
		return None
	for pr in merged:
		if stacks_on_merged_history(str(pr.get("headRefOid") or ""), tip, cwd, base):
			return pr
	return None


def merged_without_open(pull_requests: list[dict]) -> dict | None:
	"""Conditions 1 and 2 only — for callers that cannot verify ancestry."""
	merged = [pr for pr in pull_requests if pr.get("mergedAt")]
	if not merged:
		return None
	if any(str(pr.get("state", "")).upper() == "OPEN" for pr in pull_requests):
		return None
	return merged[0]


def _remediation_text(branch: str, base: str) -> str:
	reset_commands = (
		f"  git fetch origin {base}\n"
		f"  git checkout -B {branch} origin/{base}\n"
	)
	default_branch_note = ""
	if not base:
		reset_commands = (
			f"  git fetch origin <default-branch>\n"
			f"  git checkout -B {branch} origin/<default-branch>\n"
		)
		default_branch_note = (
			f"The guard could not determine the default branch automatically; "
			f"replace `<default-branch>` with your repo's real default branch.\n"
			f"\n"
		)
	return (
		f"A merged PR is finished — it cannot track new work and must not be "
		f"reused. Restart the branch from the default branch, keeping the same "
		f"name, then redo the commit and open a NEW pull request:\n"
		f"\n"
		f"{reset_commands}"
		f"\n"
		f"{default_branch_note}"
		f"If the branch carries unmerged commits beyond the merged history, "
		f"rebase them onto the new base instead of discarding them. Stash or "
		f"re-apply any uncommitted work as needed, then retry.\n"
		f"\n"
		f"To bypass this guard for one session, set CLAUDE_PR_MERGE_GUARD=off."
	)


def _block_message(pr: dict, branch: str, base: str, tip_label: str = "HEAD") -> str:
	number = pr.get("number")
	return (
		f"BLOCKED by the merged-PR guard (CLAUDE.md §21).\n"
		f"\n"
		f"Branch `{branch}` already had PR #{number} merged at "
		f"{pr.get('mergedAt')}, no open PR now carries this branch, and {tip_label} "
		f"still contains that merged history. Committing or pushing here would "
		f"strand the work on a dead branch.\n"
		f"\n"
		f"  merged PR: {pr.get('url')}\n"
		f"  title:     {pr.get('title')}\n"
		f"\n"
		f"{_remediation_text(branch, base)}"
	)


def _history_block_message(branch: str, base: str, detail: str, api_failure: str) -> str:
	return (
		f"BLOCKED by the merged-PR guard (CLAUDE.md §21).\n"
		f"\n"
		f"GitHub could not be reached to check PR status for `{branch}` "
		f"({api_failure}), but git history alone shows the branch is sitting on "
		f"already-merged history: {detail}. Committing or pushing here would "
		f"strand the work on a dead branch.\n"
		f"\n"
		f"{_remediation_text(branch, base)}"
	)


def _warn(reason: str) -> None:
	"""Emit a non-blocking warning to the user and allow the command."""
	message = f"merged-PR guard skipped: {reason}"
	if message not in _pending_output["system_messages"]:
		_pending_output["system_messages"].append(message)


def _request_confirmation(reason: str, prompt_reason: str | None = None) -> None:
	"""Route the call through the harness permission prompt (CLAUDE.md §21.C).

	Used when the guard cannot prove the branch is safe: the API is
	unreachable and git history is inconclusive, or ancestry cannot be
	verified for a remote-only push. The human confirms the PR is still open;
	a denial sends the reason back to Claude.
	"""
	# The prompt is read by a human: `reason` carries the full transport
	# error for the log, `prompt_reason` a one-paragraph version for the prompt.
	short_reason = prompt_reason or reason
	message = f"merged-PR guard needs confirmation: {reason}"
	if message not in _pending_output["system_messages"]:
		_pending_output["system_messages"].append(message)
	ask_reason = (
		f"merged-PR guard (CLAUDE.md §21): {short_reason} Allow only if the "
		f"pull request for this branch is still open. If it has merged, "
		f"deny — the branch must be rebuilt from the default branch and "
		f"a new PR opened."
	)
	if ask_reason not in _pending_output["ask_reasons"]:
		_pending_output["ask_reasons"].append(ask_reason)


def _request_api_write_confirmation(reason: str | None = None) -> None:
	"""Restore the harness prompt for a non-canonical API write or destination."""
	ask_reason = reason or (
		"Non-canonical API curl options can override the allowlisted "
		"HTTP method or destination."
	)
	if ask_reason not in _pending_output["ask_reasons"]:
		_pending_output["ask_reasons"].append(ask_reason)


def _reset_pending_output() -> None:
	_pending_output["system_messages"].clear()
	_pending_output["ask_reasons"].clear()


def _emit_pending_output(code: int) -> None:
	response: dict = {}
	if _pending_output["system_messages"]:
		response["systemMessage"] = "\n".join(_pending_output["system_messages"])
	if code == 0 and _pending_output["ask_reasons"]:
		response["hookSpecificOutput"] = {
			"hookEventName": "PreToolUse",
			"permissionDecision": "ask",
			"permissionDecisionReason": "\n\n".join(_pending_output["ask_reasons"]),
		}
	if response:
		print(json.dumps(response))
	_reset_pending_output()


def _payload_cwd(payload: dict) -> str:
	cwd = payload.get("cwd") or os.getcwd()
	if not isinstance(cwd, str) or not os.path.isdir(cwd):
		cwd = os.getcwd()
	return cwd


def _guard_disabled() -> bool:
	return os.environ.get("CLAUDE_PR_MERGE_GUARD", "").strip().lower() == "off"


def _unreachable_outcome(
	api_failure: str,
	tip: str,
	branch: str,
	base: str,
	cwd: str,
	reaches_remote: bool,
	target_slug: str = "",
) -> tuple[int, str]:
	"""Decide what to do when GitHub could not answer (CLAUDE.md §21.C).

	`tip` is the commit the write would stack on ("" when there is nothing
	local to inspect, e.g. an MCP push to a repository that is not the local
	checkout). `reaches_remote` says whether the write lands on origin: a push
	asks for confirmation when git history is inconclusive, a local commit is
	allowed with a warning because the work only strands once pushed.
	"""
	verdict, detail = (
		git_history_verdict(tip, branch, base, cwd)
		if tip
		else (VERDICT_UNAVAILABLE, f"no local checkout of {target_slug or 'the target repository'} to inspect")
	)
	if verdict == VERDICT_STRANDED:
		return 2, _history_block_message(branch, base, detail, api_failure)
	reason = (
		f"could not reach GitHub to check PR status for `{branch}` ({api_failure}); "
		f"git history is {verdict}: {detail}."
	)
	brief_failure = api_failure.strip().splitlines()[0][:100] if api_failure.strip() else "unknown error"
	prompt_reason = (
		f"could not reach GitHub to check PR status for `{branch}` ({brief_failure}...); "
		f"git history is {verdict}: {detail}."
	)
	if reaches_remote:
		_request_confirmation(reason, prompt_reason)
	else:
		_warn(reason)
	return 0, ""


def _evaluate_bash(payload: dict) -> tuple[int, str]:
	tool_input = payload.get("tool_input")
	command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
	if not isinstance(command, str) or not command.strip():
		return 0, ""
	# Git parsing ignores heredoc bodies that Bash passes on as data; the
	# API-write check below still reads the raw command.
	git_view_command = _strip_data_heredoc_bodies(command)
	guarded_git_subcommands = git_subcommands(git_view_command) & GUARDED_SUBCOMMANDS
	api_write_reason = _api_write_confirmation_reason(command)
	if api_write_reason is not None:
		if guarded_git_subcommands:
			return 2, (
				"BLOCKED: run the non-canonical API write and git commit/push as "
				"separate Bash calls so both permission guards can evaluate them."
			)
		_request_api_write_confirmation(api_write_reason)
		return 0, ""

	if _guard_disabled():
		return 0, ""
	if not guarded_git_subcommands:
		# Bash may execute earlier lines before a later unmatched quote. Raw-text
		# searches miss quoted/escaped spellings of git and its subcommands.
		try:
			_shell_segments_with_operators(git_view_command)
		except ValueError:
			_request_confirmation("Cannot parse the Bash command; an earlier git commit/push may still execute.")
		return 0, ""

	checkout = _payload_cwd(payload)
	# A snapshot is memoized per slug/branch, not per tip: two refspecs may
	# share a destination while pushing different commits. An API failure is
	# memoized as well, to keep the call budget bounded on repeated targets.
	pr_snapshots: dict[tuple[str, str], tuple[list[dict] | None, bool, str]] = {}
	blocks: list[str] = []
	bulk_reasons: list[str] = []
	unverified_destinations: set[str] = set()
	uncertain_push_reasons: list[str] = []
	unknown_destination_reasons: list[str] = []
	unresolved_push_sources: list[str] = []
	unresolved_push_destinations: list[str] = []
	for invocation in _guarded_git_invocations(command, checkout):
		if invocation.subcommand == "commit" and invocation.env_directory_unresolved:
			unknown_destination_reasons.append("could not resolve git commit directory; no checkout was checked; cannot verify its PR history")
			continue
		if invocation.subcommand == "push" and invocation.warning in ("unparsed env wrapper", _UNPARSED_SHELL_WRAPPER):
			unverified_destinations.add(
				"unparsed shell-wrapped Git command" if invocation.warning == _UNPARSED_SHELL_WRAPPER
				else "unparsed env-wrapped Git command"
			)
			continue
		if invocation.subcommand == "commit" and invocation.warning and invocation.env_wrapped:
			_request_confirmation("could not resolve git commit directory (env-wrapped); the session checkout may not be the commit target")
			continue
		if invocation.subcommand == "commit" and invocation.warning and invocation.explicit_git_directory:
			_request_confirmation("could not resolve git commit directory; PR status cannot be checked for the intended checkout")
			continue
		if invocation.subcommand == "commit" and invocation.warning and invocation.config_override:
			# Author decision Q30 = A: per-command configuration (git -c, --config-env,
			# GIT_CONFIG_*) may select another checkout, e.g. via core.worktree, so
			# the commit asks. The session checkout is still checked and can block.
			unknown_destination_reasons.append(
				"could not resolve git commit directory; per-command Git configuration may select another checkout"
			)
		if invocation.subcommand == "push" and invocation.warning and invocation.explicit_directory_unresolved:
			# Finding #6305: an unresolvable explicit override (GIT_DIR+=, an
			# unresolved -C / env -C / GIT_DIR) selects a repository this hook
			# cannot see. The session checkout's PR history says nothing about
			# it, so the push asks without querying it.
			unknown_destination_reasons.append(invocation.warning)
			continue
		if invocation.subcommand == "push" and invocation.warning:
			uncertain_push_reasons.append(invocation.warning)
		if invocation.subcommand == "push" and invocation.config_override:
			unverified_destinations.add("per-command Git configuration may redirect the push")
			continue  # Origin's PR history cannot authorize a push with overridden configuration.
		targets = (
			_push_targets(invocation, checkout) if invocation.subcommand == "push" else
			[_GuardTarget(invocation.cwd, invocation.environment, "", "HEAD", False, invocation.warning)]
		)
		for target in targets:
			if target.branch is None:
				unresolved_push_destinations.append(
					"could not resolve git push destination; shell expansion may change the pushed branch."
				)
				continue
			if target.bulk:
				bulk_reasons.append(target.bulk)
			if target.warning.startswith("could not resolve git push"):
				unknown_destination_reasons.append(target.warning)
				continue
			if target.warning.startswith("could not resolve explicit git command directory") and invocation.subcommand == "commit":
				unverified_destinations.add("could not resolve git commit directory; the session checkout may differ")
				continue
			if target.warning:
				_warn(target.warning)  # Implicit uncertainty: check the session checkout.
			if target.remote and target.remote != "origin":
				# Even a matching explicit URL may be rewritten by url.*.insteadOf.
				if "://" in target.remote or target.remote.startswith("git@"):
					unverified_destinations.add("explicit push URL may be rewritten by Git configuration")
					continue
				with _git_environment(target.environment):
					checkout_slug = repo_slug(target.cwd)
				push_slug = extract_repo_slug(target.remote)
				if not checkout_slug or push_slug != checkout_slug:
					# Git may push to a different repository; its PR history and
					# default branch cannot be inferred from this checkout's origin.
					unverified_destinations.add(push_slug or "an unverified remote")
					continue
			if not target.tip:
				continue  # A deletion or tag-only push cannot strand a branch commit.
			if target.tip != "HEAD":
				with _git_environment(target.environment):
					code, resolved_source_sha, _ = _run(
						["git", "rev-parse", "--verify", "--end-of-options", f"{target.tip}^{{commit}}"],
						target.cwd, _GIT_TIMEOUT_SECONDS,
					)
				if code != 0:
					unresolved_push_sources.append(
						f"could not resolve git push source for `{target.branch}`; "
						"shell expansion may change the pushed commit."
					)
					continue
				else:
					target = target._replace(tip=resolved_source_sha.strip())
			with _git_environment(target.environment):
				branch = target.branch or current_branch(target.cwd)
				if not branch:
					# Detached HEAD without a literal branch destination.
					continue
				base = default_branch(target.cwd)
				if base and branch == base:
					continue
				slug = repo_slug(target.cwd)
				if not slug:
					_warn(f"could not derive <owner>/<repo> from the git remote (branch `{branch}`)")
					continue
				tip = target.tip
				key = (slug, branch)
				if key not in pr_snapshots:
					cached = _read_cache(slug, branch)
					try:
						pull_requests = cached if cached is not None else query_pull_requests(slug, branch, target.cwd)
					except LookupUnavailable as exc:
						pr_snapshots[key] = (None, True, str(exc))
					else:
						if cached is None:
							_write_cache(slug, branch, pull_requests)
						pr_snapshots[key] = (pull_requests, cached is None, "")
				pull_requests, fresh, failure = pr_snapshots[key]
				if failure:
					outcome, message = _unreachable_outcome(
						failure, tip, branch, base, target.cwd, target.reaches_remote
					)
					if outcome == 2:
						blocks.append(message)
					continue
				if pull_requests is None:
					continue
				offender = blocking_pull_request(pull_requests, target.cwd, base, tip)
				if offender is not None and not fresh:
					try:
						pull_requests = query_pull_requests(slug, branch, target.cwd)
					except LookupUnavailable as exc:
						failure = f"could not re-verify: {exc}"
						pr_snapshots[key] = (None, True, failure)
						outcome, message = _unreachable_outcome(
							failure, tip, branch, base, target.cwd, target.reaches_remote
						)
						if outcome == 2:
							blocks.append(message)
						continue
					_write_cache(slug, branch, pull_requests)
					pr_snapshots[key] = (pull_requests, True, "")
					offender = blocking_pull_request(pull_requests, target.cwd, base, tip)
				if offender is not None:
					blocks.append(_block_message(offender, branch, base, tip_label=tip))
	if blocks:
		return 2, "\n\n".join(blocks)
	confirmation_reasons: list[str] = []
	if uncertain_push_reasons:
		confirmation_reasons.append(
			"could not resolve git push repository; the session checkout may not be the pushed repository. "
			"could not determine the directory `git push` runs in (shell control flow or redirection); "
			"checked the session checkout instead"
		)
	confirmation_reasons.extend(unresolved_push_sources)
	confirmation_reasons.extend(unresolved_push_destinations)
	confirmation_reasons.extend(unknown_destination_reasons)
	if bulk_reasons:
		confirmation_reasons.append(
			"Bulk git push may write more branches than the current branch: "
			+ ", ".join(sorted(set(bulk_reasons)))
		)
	if unverified_destinations:
		confirmation_reasons.append(
			"Git write may target an unverified repository or branch: "
			+ ", ".join(sorted(unverified_destinations))
		)
	if confirmation_reasons:
		_request_confirmation("; ".join(confirmation_reasons))
	return 0, ""


def _evaluate_mcp_push(payload: dict) -> tuple[int, str]:
	"""Guard `mcp__github__push_files` / `mcp__github__create_or_update_file`.

	These write straight to a remote branch, so the remote branch tip plays
	the role HEAD plays for a local commit. When the target repository is the
	local checkout, the tip is fetched and the full three-condition rule
	applies; otherwise ancestry cannot be verified and a merged-PR match asks
	for confirmation instead of blocking (a block could not self-clear).
	"""
	if _guard_disabled():
		return 0, ""
	tool_input = payload.get("tool_input")
	if not isinstance(tool_input, dict):
		return 0, ""
	owner = tool_input.get("owner")
	repo = tool_input.get("repo")
	branch = tool_input.get("branch")
	if not all(isinstance(value, str) and value.strip() for value in (owner, repo, branch)):
		return 0, ""
	branch = branch.strip()
	slug = f"{owner.strip()}/{repo.strip()}"
	if not _SLUG_RE.match(slug):
		return 0, ""

	cwd = _payload_cwd(payload)
	is_local_checkout = repo_slug(cwd) == slug
	base = default_branch(cwd) if is_local_checkout else ""
	if base and branch == base:
		# Pushing to the default branch is not the stranded-work scenario.
		return 0, ""

	tip = ""
	if is_local_checkout:
		remote_tip = remote_branch_tip(branch, cwd)
		if remote_tip is None:
			_request_confirmation(
				f"could not list origin's branches while looking up `{branch}` in {slug} "
				f"to verify its ancestry."
			)
			return 0, ""
		if remote_tip == "":
			# Origin has no such branch: nothing merged to stack on.
			return 0, ""
		if remote_tip:
			if not fetch_from_origin([base, branch] if base else [branch], cwd):
				_request_confirmation(
					f"could not fetch origin/{branch} in {slug} to verify its ancestry."
				)
				return 0, ""
			tip = remote_tip

	cached = _read_cache(slug, branch)
	try:
		pull_requests = cached if cached is not None else query_pull_requests(slug, branch, cwd)
	except LookupUnavailable as exc:
		return _unreachable_outcome(str(exc), tip, branch, base, cwd, True, slug)

	if not tip:
		if cached is None:
			_write_cache(slug, branch, pull_requests)
		match = merged_without_open(pull_requests)
		if match is not None:
			_request_confirmation(
				f"`{branch}` in {slug} already had PR #{match.get('number')} merged "
				f"({match.get('url')}) and no open PR carries it; ancestry cannot be "
				f"verified without a local checkout of {slug}."
			)
		return 0, ""

	offender = blocking_pull_request(pull_requests, cwd, base, tip)
	if offender is not None and cached is not None:
		try:
			pull_requests = query_pull_requests(slug, branch, cwd)
		except LookupUnavailable as exc:
			return _unreachable_outcome(
				f"could not re-verify: {exc}", tip, branch, base, cwd, True, slug
			)
		_write_cache(slug, branch, pull_requests)
		offender = blocking_pull_request(pull_requests, cwd, base, tip)
	elif cached is None:
		_write_cache(slug, branch, pull_requests)

	if offender is None:
		return 0, ""
	return 2, _block_message(offender, branch, base, tip_label=f"origin/{branch}")


def _evaluate_tool(payload: dict) -> tuple[int, str]:
	"""Dispatch the hook's decision for the requested tool."""
	tool_name = payload.get("tool_name")
	if tool_name == "Bash":
		return _evaluate_bash(payload)
	if tool_name in MCP_PUSH_TOOLS:
		return _evaluate_mcp_push(payload)
	return 0, ""


def evaluate(payload: dict) -> tuple[int, str]:
	"""Core decision. Returns (exit_code, message_for_stderr)."""
	_reset_pending_output()
	try:
		code, message = _evaluate_tool(payload)
	except Exception as exc:  # noqa: BLE001 - the guard must never break the session
		_warn(f"internal error ({exc})")
		code, message = 0, ""
	_emit_pending_output(code)
	return code, message


def main() -> int:
	try:
		raw = sys.stdin.read()
	except (OSError, ValueError):
		return 0
	try:
		payload = json.loads(raw) if raw.strip() else {}
	except ValueError:
		return 0
	if not isinstance(payload, dict):
		return 0

	try:
		code, message = evaluate(payload)
	except Exception:  # noqa: BLE001 - stdout may already contain a hook response
		return 0

	if message:
		print(message, file=sys.stderr)
	return code


if __name__ == "__main__":
	sys.exit(main())

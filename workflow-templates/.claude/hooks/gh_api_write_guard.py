#!/usr/bin/env python3
"""PreToolUse guard that decides the permission prompt for `gh api` calls.

Implements CLAUDE.md §23.H (permission guard for `gh api`).

`.claude/settings.json` used to keep `gh api` writes behind seven
`permissions.ask` rules (`gh api * -X *`, `-f`, `-F`, `--field`,
`--raw-field`, `--method`, `--input`). Allow and ask rules are prefix/glob
patterns, so they could not tell a write from a read, or a routine write from
a destructive one: `gh api -X GET search/issues -f q=...` (a read that passes
query parameters) prompted exactly like `gh api -X DELETE ...`, and an ask rule
still prompts in Auto mode and even when a hook says "allow". Unattended
`/implement-plan-claude` stage sessions stopped at those prompts.

This hook replaces the ask rules. For every `gh api` invocation in a Bash
command it works out the effective HTTP method the way `gh` does (`-X` /
`--method` when given, POST when fields or `--input` are present, GET
otherwise) and classifies the call:

  read     — GET / HEAD to any REST endpoint, or a GraphQL query that is not a
             mutation. Never prompted by this hook.
  routine  — a CLAUDE.md §23.B write to the repository of the local checkout
             (or the `{owner}/{repo}` placeholders): create a PR, edit a PR's
             or issue's title/body, add or edit an issue/PR comment, reply to a
             review thread, add or remove a label, request reviewers, and
             dispatch one of the workflows `settings.json` already allows as
             `gh workflow run <file> *` (the §23.C command-invoked carve-out).
             Each endpoint carries a field allowlist, so a `state` change
             (closing a PR or issue) is not routine. Never prompted by this
             hook.
  write    — everything else, and any call the guard cannot read (unknown flag,
             missing or extra endpoint, a method-override header, or `gh api`
             that could run hidden: inside a `$(...)` / backtick word, handed
             to an executor such as `bash -c`, `sudo`, `xargs`, `python3`, or
             in a heredoc fed to one). Forces the permission prompt, in every
             permission mode. `gh api` text handed to any other command
             (`git commit -m`, `grep`, `echo`) is data and is ignored.

Decision for the whole Bash call (a hook decides once per tool call):
  - any `write` → `permissionDecision: ask`;
  - every call is `read` or `routine` and the command contains nothing else
    but safe helpers: items joined by `;` / `&&`, each a `gh api` call
    (optionally piped into `head`/`tail -n N`, `wc -l`, or
    `sort -n -r -u -k K -t C`) or a standalone `cd <path>`, `sleep <n>`,
    `echo <text>`, `true`; `2>&1` as the only redirect; no `$`, backticks,
    globs, subshells, or loops → `permissionDecision: allow`;
  - otherwise (reads and routine writes beside other code: loops,
    `python3`, `$VAR`, file redirects) → no decision, so the allow list or
    the Auto-mode classifier decides as for any other command. An allow here
    would also approve the code the guard has not read.

Unlike the other hooks in this directory this one fails CLOSED: an unreadable,
invalid, or non-object payload, or an internal error, asks instead of allowing,
because it is the only thing standing where the ask rules were. Empty or
whitespace-only input carries no command and is allowed silently.

The hook issues no GitHub API calls (§15). The only subprocess is one
`git config --get remote.origin.url`, run only when a call could be routine.
There is deliberately no environment-variable escape hatch.

Exit code is always 0 (Claude Code hook protocol); the decision travels in the
JSON `hookSpecificOutput` on stdout.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys


# The exact `.claude/settings.json` matcher this hook is wired under. Kept
# here so the tests can assert the wiring and the hook agree.
SETTINGS_MATCHER = "Bash"

DECISION_ASK = "ask"
DECISION_ALLOW = "allow"

KIND_READ = "read"
KIND_ROUTINE = "routine"
KIND_WRITE = "write"

# Anything that looks like `gh api` in a piece of text (a token, a heredoc
# body). The fast path skips every command that contains none.
_RAW_GH_API_RE = re.compile(r"(?<![\w.-])gh\s+api(?![\w-])")

_SHELL_PUNCTUATION_CHARS = ";&|\n<>()"

# Words that may precede the command word of a segment without changing what
# runs: shell keywords and transparent wrappers.
_SEGMENT_PREFIX_WORDS = frozenset(
	{"!", "{", "do", "then", "else", "elif", "if", "while", "until", "time", "command", "exec", "nohup", "builtin", "env"}
)

# Commands that run their arguments (or their stdin) as code. `gh api` text
# handed to one of these could execute without the guard seeing it as a direct
# invocation, so it counts as a hidden call. `gh api` text handed to anything
# else (`git commit -m`, `grep`, `echo`) is data and is ignored.
_EXECUTOR_COMMANDS = frozenset(
	{
		"bash", "sh", "zsh", "dash", "ksh", "fish", "eval", "source", ".", "ssh", "su", "sudo", "doas",
		"xargs", "parallel", "watch", "timeout", "nice", "stdbuf", "script", "python", "python3",
		"node", "perl", "ruby",
	}
)
_EXECUTOR_BEFORE_HEREDOC_RE = re.compile(
	r"(?:^|[\s;&|(`])(?:\S*/)?(?:" + "|".join(re.escape(word) for word in sorted(_EXECUTOR_COMMANDS)) + r")(?:\s|$)"
)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# `gh api` flags (gh 2.x). Unknown flags make the call unreadable.
_VALUE_FLAGS = {
	"-X": "method",
	"--method": "method",
	"-F": "field",
	"--field": "field",
	"-f": "raw_field",
	"--raw-field": "raw_field",
	"-H": "header",
	"--header": "header",
	"--input": "input",
	"-q": "jq",
	"--jq": "jq",
	"-t": "template",
	"--template": "template",
	"--cache": "cache",
	"--hostname": "hostname",
	"-p": "preview",
	"--preview": "preview",
}
_BOOL_FLAGS = frozenset(
	{"-i", "--include", "--paginate", "--silent", "--slurp", "--verbose", "--allow-escape-sequences"}
)
_SHORT_VALUE_FLAGS = frozenset(flag for flag in _VALUE_FLAGS if len(flag) == 2)

_READ_METHODS = frozenset({"GET", "HEAD"})
_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"})

# Headers that cannot change what a request does. Anything else (for example
# `X-HTTP-Method-Override`) makes the call a write.
_SAFE_HEADER_RE = re.compile(r"^\s*(accept|x-github-api-version)\s*:", re.IGNORECASE)

_NUM = r"[0-9]+"
# CLAUDE.md §23.B routine writes: (method, path under repos/<owner>/<repo>/,
# allowed field keys). A field outside the allowlist, or `--input`, makes the
# call a write, so `state`, `base`, `labels` on the issue itself, and similar
# are never routine.
_ROUTINE_ENDPOINTS = (
	("POST", re.compile(r"^pulls$"), frozenset({"title", "body", "head", "base", "draft", "maintainer_can_modify", "head_repo", "issue"})),
	("PATCH", re.compile(rf"^pulls/{_NUM}$"), frozenset({"title", "body"})),
	("PATCH", re.compile(rf"^issues/{_NUM}$"), frozenset({"title", "body"})),
	("POST", re.compile(rf"^issues/{_NUM}/comments$"), frozenset({"body"})),
	("PATCH", re.compile(rf"^issues/comments/{_NUM}$"), frozenset({"body"})),
	("POST", re.compile(rf"^pulls/{_NUM}/comments/{_NUM}/replies$"), frozenset({"body"})),
	(
		"POST",
		re.compile(rf"^pulls/{_NUM}/comments$"),
		frozenset({"body", "commit_id", "path", "line", "side", "start_line", "start_side", "in_reply_to", "subject_type", "position"}),
	),
	("PATCH", re.compile(rf"^pulls/comments/{_NUM}$"), frozenset({"body"})),
	("POST", re.compile(rf"^issues/{_NUM}/labels$"), frozenset({"labels"})),
	("DELETE", re.compile(rf"^issues/{_NUM}/labels/[^/]+$"), frozenset()),
	("POST", re.compile(rf"^pulls/{_NUM}/requested_reviewers$"), frozenset({"reviewers", "team_reviewers"})),
)

# Workflows whose dispatch `.claude/settings.json` already pre-approves as
# `Bash(gh workflow run <file> *)` (the CLAUDE.md §23.C command-invoked
# carve-out for `/implement-plan-claude`). Dispatching the same file through
# `gh api .../actions/workflows/<file>/dispatches` is routine; any other
# workflow is not. `tests/test_gh_api_write_guard.py` keeps this set equal to
# the allow rules.
DISPATCHABLE_WORKFLOWS = frozenset(
	{
		"security-audit.yml",
		"ai-security-audit.yml",
		"internal-validate.yml",
		"ai-validate.yml",
		"review_autofix.yml",
		"ai-review.yml",
	}
)
_ROUTINE_ENDPOINTS += (
	(
		"POST",
		re.compile(
			r"^actions/workflows/(?:" + "|".join(re.escape(name) for name in sorted(DISPATCHABLE_WORKFLOWS)) + r")/dispatches$"
		),
		frozenset({"ref", "inputs"}),
	),
)

# Commands that may stand beside `gh api` calls in a command the hook still
# approves (CLAUDE.md §23.H). Standalone helpers run as their own list item
# (`cd <path> && ...`, `sleep 10; ...`, `echo text; ...`); pipe filters only
# read the `gh api` output and write to stdout, so their options are limited
# to ones that open no file (`sort -o` is excluded).
_STANDALONE_HELPERS = frozenset({"cd", "sleep", "echo", "true"})
_SORT_KEY_RE = re.compile(r"^[0-9][0-9,.a-zA-Z]*$")


def _is_safe_filter(words: list[str]) -> bool:
	"""True for `head`/`tail -n N`, `wc -l`, or `sort -n -r -u -k K -t C`.

	No file operand and no option that opens a file, so the filter only reads
	the piped `gh api` output and writes to stdout.
	"""
	name, args = words[0], words[1:]
	if name in ("head", "tail"):
		index = 0
		while index < len(args):
			arg = args[index]
			if arg == "-n" and index + 1 < len(args) and args[index + 1].isdigit():
				index += 2
				continue
			if not re.match(r"^-n?[0-9]+$", arg):
				return False
			index += 1
		return True
	if name == "wc":
		return all(re.match(r"^-[lwcm]+$", arg) for arg in args)
	if name == "sort":
		index = 0
		while index < len(args):
			arg = args[index]
			index += 1
			if re.match(r"^-[nrufbV]+$", arg):
				continue
			if arg in ("-k", "-t"):
				if index >= len(args):
					return False
				arg, index = arg + args[index], index + 1
			if arg.startswith("-k") and _SORT_KEY_RE.match(arg[2:]):
				continue
			if arg.startswith("-t") and len(arg) == 3:
				continue
			return False
		return True
	return False

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+/[A-Za-z0-9._-]+$")
_GIT_TIMEOUT_SECONDS = 5


class Unreadable(Exception):
	"""A `gh api` call whose effect the guard cannot determine."""


def strip_heredoc_bodies(command: str) -> tuple[str, list[tuple[str, bool, str]]]:
	"""Split heredoc bodies out of a command.

	Returns the command with every heredoc body removed (the `<<WORD` operator
	stays, so the command's structure is preserved) and one
	`(text before the operator, delimiter quoted, body)` entry per heredoc, so
	the caller can decide whether a body could run a `gh api` call.
	"""
	lines = command.split("\n")
	output: list[str] = []
	heredocs: list[tuple[str, bool, str]] = []
	pending: list[tuple[str, bool, bool, str]] = []
	index = 0
	while index < len(lines):
		line = lines[index]
		output.append(line)
		for match in re.finditer(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2", line):
			pending.append((match.group(3), match.group(1) == "-", bool(match.group(2)), line[: match.start()]))
		index += 1
		while pending:
			delimiter, strip_tabs, quoted, prefix = pending.pop(0)
			body: list[str] = []
			while index < len(lines):
				body_line = lines[index]
				index += 1
				candidate = body_line.lstrip("\t") if strip_tabs else body_line
				if candidate == delimiter:
					break
				body.append(body_line)
			heredocs.append((prefix, quoted, "\n".join(body)))
	return "\n".join(output), heredocs


def shell_segments(command: str) -> list[list[str]]:
	"""Tokenize a shell command into command-sized segments.

	Separators (`;`, `&&`, `||`, `|`, `&`, newlines, parentheses) end a
	segment. A redirect (`>`, `2>&1`, `<<EOF`, ...) stays in its segment but
	is dropped together with its target and a leading file-descriptor number,
	so it is never mistaken for an argument. Quoted text stays one token.
	Raises ValueError on unbalanced quotes.
	"""
	lexer = shlex.shlex(command.replace("\\\n", " "), posix=True, punctuation_chars=_SHELL_PUNCTUATION_CHARS)
	lexer.commenters = ""
	lexer.whitespace = " \t\r"
	lexer.whitespace_split = True
	segments: list[list[str]] = []
	current_segment: list[str] = []
	skip_redirect_target = False
	for token in lexer:
		is_punctuation = bool(token) and set(token) <= set(_SHELL_PUNCTUATION_CHARS)
		if skip_redirect_target and not is_punctuation:
			skip_redirect_target = False
			continue
		skip_redirect_target = False
		if is_punctuation and set(token) <= set("<>&") and set(token) & set("<>"):
			if current_segment and current_segment[-1].isdigit():
				current_segment.pop()
			skip_redirect_target = True
			continue
		if is_punctuation:
			if current_segment:
				segments.append(current_segment)
				current_segment = []
			continue
		current_segment.append(token)
	if current_segment:
		segments.append(current_segment)
	return segments


def _is_gh(word: str) -> bool:
	return word == "gh" or word.endswith("/gh")


def _command_word_index(tokens: list[str]) -> int:
	"""Index of the segment's command word, past keywords and assignments."""
	index = 0
	while index < len(tokens) and (tokens[index] in _SEGMENT_PREFIX_WORDS or _ASSIGNMENT_RE.match(tokens[index])):
		index += 1
	return index


def gh_api_invocations(segments: list[list[str]]) -> list[list[str]]:
	"""Return the argument list (after `gh api`) of every direct invocation."""
	invocations: list[list[str]] = []
	for tokens in segments:
		index = _command_word_index(tokens)
		if index + 1 < len(tokens) and _is_gh(tokens[index]) and tokens[index + 1] == "api":
			invocations.append(tokens[index + 2 :])
	return invocations


def has_hidden_gh_api(segments: list[list[str]], heredocs: list[tuple[str, bool, str]]) -> bool:
	"""True when a `gh api` call could run without being a direct invocation.

	Counts as hidden: `gh api` inside a `$(...)` or backtick substitution
	within one word; `gh api` text passed to an executor command (`bash -c`,
	`sudo`, `xargs`, `python3 -c`, ...) or to a command word that is itself an
	expansion; `gh api` words behind any other prefix; and a heredoc body
	that mentions `gh api` when it feeds an executor or, with an unquoted
	delimiter, contains a substitution. `gh api` text handed to any other
	command (`git commit -m`, `grep`, `echo`) is data and is not counted.
	"""
	for prefix, quoted, body in heredocs:
		if not _RAW_GH_API_RE.search(body):
			continue
		if _EXECUTOR_BEFORE_HEREDOC_RE.search(prefix):
			return True
		if not quoted and re.search(r"\$\(|`", body):
			return True
	for tokens in segments:
		index = _command_word_index(tokens)
		if index >= len(tokens):
			continue
		command_word = tokens[index]
		direct = index + 1 < len(tokens) and _is_gh(command_word) and tokens[index + 1] == "api"
		executor = os.path.basename(command_word) in _EXECUTOR_COMMANDS or command_word.startswith("$")
		for position, token in enumerate(tokens):
			if _RAW_GH_API_RE.search(token) and ("$(" in token or "`" in token or executor):
				return True
			if (
				not direct
				and position + 1 < len(tokens)
				and _is_gh(token)
				and tokens[position + 1] == "api"
			):
				return True
	return False


def parse_gh_api_args(args: list[str]) -> dict:
	"""Parse `gh api` arguments into method, endpoint, fields, headers, input.

	Raises Unreadable on an unknown flag, a missing value, or anything other
	than exactly one endpoint.
	"""
	parsed = {"method": None, "endpoints": [], "fields": [], "headers": [], "input": None}
	index = 0
	options_done = False
	while index < len(args):
		token = args[index]
		index += 1
		if options_done or not token.startswith("-") or token == "-":
			parsed["endpoints"].append(token)
			continue
		if token == "--":
			options_done = True
			continue
		if token in _BOOL_FLAGS:
			continue
		flag, value = token, None
		if token.startswith("--") and "=" in token:
			flag, value = token.split("=", 1)
		elif not token.startswith("--") and len(token) > 2 and token[:2] in _SHORT_VALUE_FLAGS:
			flag, value = token[:2], token[2:]
		role = _VALUE_FLAGS.get(flag)
		if role is None:
			raise Unreadable(f"unknown gh api flag `{flag}`")
		if value is None:
			if index >= len(args):
				raise Unreadable(f"`{flag}` has no value")
			value = args[index]
			index += 1
		if role == "method":
			parsed["method"] = value.upper()
		elif role in ("field", "raw_field"):
			if "=" not in value:
				raise Unreadable(f"field `{value}` is not key=value")
			key, field_value = value.split("=", 1)
			parsed["fields"].append((role, re.sub(r"\[.*$", "", key), field_value))
		elif role == "header":
			parsed["headers"].append(value)
		elif role == "input":
			parsed["input"] = value
	if len(parsed["endpoints"]) != 1:
		raise Unreadable(f"expected one endpoint, found {len(parsed['endpoints'])}")
	if parsed["method"] is None:
		parsed["method"] = "POST" if parsed["fields"] or parsed["input"] is not None else "GET"
	if parsed["method"] not in _KNOWN_METHODS:
		raise Unreadable(f"unknown HTTP method `{parsed['method']}`")
	return parsed


def extract_repo_slug(url: str) -> str:
	"""Derive `<owner>/<repo>` from a git remote URL, or "" when not derivable.

	Same host whitelist as `extract_repo_slug` in `pr_merge_status_guard.py`
	and `session-start.sh`: github.com, plus Claude Code Web's local git proxy
	on 127.0.0.1/localhost where the path starts with `git/`.
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
		host, path = url.rsplit("@", 1)[1].split(":", 1)
	else:
		return ""
	if host in ("127.0.0.1", "localhost"):
		if not path.startswith("git/"):
			return ""
		path = path[len("git/") :]
	elif host != "github.com":
		return ""
	return path if _SLUG_RE.match(path) else ""


def local_repo_slug(cwd: str) -> str:
	"""Return the local checkout's `<owner>/<repo>`, or "" when unknown."""
	try:
		proc = subprocess.run(
			["git", "config", "--get", "remote.origin.url"],
			cwd=cwd,
			capture_output=True,
			text=True,
			timeout=_GIT_TIMEOUT_SECONDS,
			check=False,
		)
	except (OSError, subprocess.SubprocessError):
		return ""
	return extract_repo_slug(proc.stdout) if proc.returncode == 0 else ""


def _has_expansion_outside_single_quotes(command: str) -> bool:
	"""True when `$` or a backtick appears where Bash would expand it."""
	single_quoted = False
	escaped = False
	for character in command:
		if escaped:
			escaped = False
			continue
		if character == "\\" and not single_quoted:
			escaped = True
			continue
		if character == "'":
			single_quoted = not single_quoted
			continue
		if not single_quoted and character in "$`":
			return True
	return False


_REDIRECT_TO_STDERR_RE = re.compile(r"(?<=\s)2>&1(?=\s|;|\||&&|$)")
_SLEEP_DURATION_RE = re.compile(r"^[0-9]+(\.[0-9]+)?[smh]?$")


def _has_unsafe_shell_syntax(command: str) -> bool:
	"""True when Bash could expand, glob, redirect, or nest anything.

	Rejects `$` and backticks outside single quotes, and outside any quotes
	newlines, `<`, parentheses, globs, tilde, and brace expansion. `;`, `&`,
	`|`, and `>` are left to the token check in `_is_approvable_command`.
	"""
	single_quoted = False
	double_quoted = False
	escaped = False
	previous = " "
	for index, character in enumerate(command):
		if escaped:
			escaped = False
			previous = character
			continue
		if character == "\\" and not single_quoted:
			escaped = True
			continue
		if character == "'" and not double_quoted:
			single_quoted = not single_quoted
		elif character == '"' and not single_quoted:
			double_quoted = not double_quoted
		elif single_quoted:
			pass
		elif character in "$`":
			return True
		elif double_quoted:
			pass
		elif character in "<()\n*?[":
			return True
		elif character == "~" and previous in " \t=:":
			return True
		elif character == "{" and re.match(r"\{[^{}\s]*(,|\.\.)[^{}\s]*\}", command[index:]):
			return True
		previous = character
	return single_quoted or double_quoted or escaped


def _is_safe_standalone(words: list[str]) -> bool:
	name, args = words[0], words[1:]
	if name == "cd":
		return len(args) <= 1
	if name == "sleep":
		return len(args) == 1 and bool(_SLEEP_DURATION_RE.match(args[0]))
	if name == "true":
		return not args
	return name == "echo"


def _is_approvable_command(command: str) -> bool:
	"""True when the hook may approve the whole command (CLAUDE.md §23.H).

	The command is a list of items joined by `;` or `&&`. Each item is either
	a `gh api` call, optionally piped into safe filters (`head`/`tail -n N`,
	`wc -l`, `sort -n -r -u -k K -t C`), or a standalone helper (`cd <path>`,
	`sleep <n>`, `echo <text>`, `true`). `2>&1` is the only redirect. There is
	no expansion, glob, subshell, loop, or other command, so approving the
	call approves nothing the guard has not read.
	"""
	reduced = _REDIRECT_TO_STDERR_RE.sub("", command)
	if _has_unsafe_shell_syntax(reduced):
		return False
	lexer = shlex.shlex(reduced, posix=True, punctuation_chars=";&|>")
	lexer.commenters = ""
	lexer.whitespace = " \t\r"
	lexer.whitespace_split = True
	try:
		tokens = list(lexer)
	except ValueError:
		return False
	items: list[list[list[str]]] = [[[]]]
	for token in tokens:
		if token in (";", "&&"):
			items.append([[]])
		elif token == "|":
			items[-1].append([])
		elif set(token) <= set(";&|>"):
			return False
		else:
			items[-1][-1].append(token)
	if items[-1] == [[]]:
		items.pop()
	saw_gh_api = False
	for pipeline in items:
		if any(not words for words in pipeline):
			return False
		head = pipeline[0]
		if len(head) >= 2 and head[0] == "gh" and head[1] == "api":
			if not all(_is_safe_filter(words) for words in pipeline[1:]):
				return False
			saw_gh_api = True
		elif len(pipeline) == 1 and head[0] in _STANDALONE_HELPERS and _is_safe_standalone(head):
			continue
		else:
			return False
	return saw_gh_api


def classify(parsed: dict, command: str, repo_slug_lookup) -> tuple[str, str]:
	"""Return `(kind, description)` for one parsed `gh api` call."""
	method = parsed["method"]
	endpoint = parsed["endpoints"][0]
	description = f"{method} {endpoint}"
	for header in parsed["headers"]:
		if not _SAFE_HEADER_RE.match(header):
			return KIND_WRITE, f"{description} with header `{header.split(':', 1)[0].strip()}`"
	path = endpoint.lstrip("/")
	if path.split("?", 1)[0] == "graphql":
		query_values = [value for _role, key, value in parsed["fields"] if key == "query"]
		if (
			parsed["input"] is None
			and query_values
			and not any(value.startswith("@") for value in query_values)
			and not re.search(r"mutation", command, re.IGNORECASE)
			and not _has_expansion_outside_single_quotes(command)
		):
			return KIND_READ, f"GraphQL query {endpoint}"
		return KIND_WRITE, f"GraphQL call {endpoint} (mutation, file, or expanded query)"
	if method in _READ_METHODS:
		return KIND_READ, description
	if parsed["input"] is not None:
		return KIND_WRITE, f"{description} with --input"
	if re.search(r"[?#$`\s]|\.\.", path):
		return KIND_WRITE, description
	match = re.match(r"^repos/([^/]+)/([^/]+)/(.+)$", path)
	if not match:
		return KIND_WRITE, description
	owner, repo, rest = match.groups()
	if (owner, repo) != ("{owner}", "{repo}"):
		local_slug = repo_slug_lookup()
		if not local_slug or f"{owner}/{repo}".lower() != local_slug.lower():
			return KIND_WRITE, f"{description} (not the local checkout's repository)"
	keys = {key for _role, key, _value in parsed["fields"]}
	for routine_method, pattern, allowed_keys in _ROUTINE_ENDPOINTS:
		if method == routine_method and pattern.match(rest):
			if keys <= allowed_keys:
				return KIND_ROUTINE, description
			return KIND_WRITE, f"{description} setting {', '.join(sorted(keys - allowed_keys))}"
	return KIND_WRITE, description


def evaluate(payload: dict) -> tuple[str | None, str]:
	"""Decide the permission outcome for one PreToolUse payload.

	Returns `(decision, reason)`: decision is "ask", "allow", or None (no
	decision; the normal permission flow applies).
	"""
	if payload.get("tool_name", "Bash") != "Bash":
		return None, ""
	tool_input = payload.get("tool_input")
	command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
	if not isinstance(command, str) or not _RAW_GH_API_RE.search(command):
		return None, ""

	stripped_command, heredocs = strip_heredoc_bodies(command)
	try:
		segments = shell_segments(stripped_command)
	except ValueError:
		return DECISION_ASK, "gh api guard (CLAUDE.md §23.H): the command could not be parsed, so its gh api call is treated as a write."
	invocations = gh_api_invocations(segments)
	if has_hidden_gh_api(segments, heredocs):
		return DECISION_ASK, (
			"gh api guard (CLAUDE.md §23.H): a gh api call could run hidden inside a $(...) or backtick word, "
			"an executor (bash -c, sudo, xargs, python3, ...), or a heredoc fed to one, so it is treated as a "
			"write. Run it as its own plain command."
		)

	cwd = payload.get("cwd")
	if not isinstance(cwd, str) or not os.path.isdir(cwd):
		cwd = os.getcwd()
	slug_cache: list[str] = []

	def repo_slug_lookup() -> str:
		if not slug_cache:
			slug_cache.append(local_repo_slug(cwd))
		return slug_cache[0]

	results: list[tuple[str, str]] = []
	for args in invocations:
		try:
			results.append(classify(parse_gh_api_args(args), command, repo_slug_lookup))
		except Unreadable as exc:
			results.append((KIND_WRITE, f"unreadable call ({exc})"))

	writes = [description for kind, description in results if kind == KIND_WRITE]
	if writes:
		return DECISION_ASK, (
			"gh api guard (CLAUDE.md §23.H): not a read or a §23.B routine write: " + "; ".join(writes) + "."
		)
	if invocations and _is_approvable_command(command):
		summary = "; ".join(f"{kind} call {description}" for kind, description in results)
		return DECISION_ALLOW, f"gh api guard (CLAUDE.md §23.H): {summary}."
	return None, ""


def _emit(decision: str, reason: str) -> None:
	print(
		json.dumps(
			{
				"hookSpecificOutput": {
					"hookEventName": "PreToolUse",
					"permissionDecision": decision,
					"permissionDecisionReason": reason,
				}
			}
		)
	)


def main() -> int:
	try:
		raw = sys.stdin.read()
	except (OSError, ValueError):
		_emit(DECISION_ASK, "gh api guard (CLAUDE.md §23.H): could not read the hook payload.")
		return 0
	try:
		payload = json.loads(raw) if raw.strip() else {}
	except ValueError:
		_emit(DECISION_ASK, "gh api guard (CLAUDE.md §23.H): hook payload is not valid JSON.")
		return 0
	if not isinstance(payload, dict):
		_emit(DECISION_ASK, "gh api guard (CLAUDE.md §23.H): hook payload is not a JSON object.")
		return 0

	try:
		decision, reason = evaluate(payload)
	except Exception as exc:  # noqa: BLE001 - fail closed: prompt rather than allow
		_emit(DECISION_ASK, f"gh api guard (CLAUDE.md §23.H): internal error ({exc}).")
		return 0

	if decision:
		_emit(decision, reason)
	return 0


if __name__ == "__main__":
	sys.exit(main())

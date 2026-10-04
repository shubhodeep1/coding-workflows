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
             mutation, with no file-backed field and no `--input`. Never
             prompted by this hook.
             A complete double-quoted `$(gh api ...)` REST read is also
             approvable in an `echo` argument, including a #4786 literal-ID
             loop whose variable occurs only in the endpoint path.
  routine  — a CLAUDE.md §23.B write to the repository of the local checkout
             (or the `{owner}/{repo}` placeholders): create a PR, edit a PR's
             or issue's title/body, add or edit an issue/PR comment, reply to a
             review thread, add or remove a label, request reviewers, and
             dispatch one of the workflows `settings.json` already allows as
             `gh workflow run <file> *` (the §23.C command-invoked carve-out).
             Each endpoint carries a field allowlist, so a `state` change
             (closing a PR or issue) is not routine. Never prompted by this
             hook.
  write    — everything else, including every call with a file-backed
             `-F`/`--field` value (`@<file>`, or `@-` for stdin: `gh` reads
             it and sends its contents) or with `--input`, whatever the
             method, endpoint, or repository (issue #4619; `-f`/`--raw-field`
             values are sent literally and read no file). A `-F` word the
             shell could rewrite into one (`$`, a backtick, `~`, or a glob
             character in it, e.g. `-F body=$'@f'`) counts too, and any call the
             guard cannot read (unknown flag,
             missing or extra endpoint, a method-override header, or `gh api`
             that could run hidden: inside a backtick or double-quoted `$(...)`
             substitution Bash would run (single-quoted text is data), handed
             to an executor such as `bash -c`, `sudo`, `xargs`, `python3`, or
             in a heredoc fed to one). Forces the permission prompt, in every
             permission mode. `gh api` text handed to any other command
             (`git commit -m`, `grep`, `echo`) is data and is ignored.
             An unquoted `$` or backtick in a direct `gh api` argument also
             prompts: Bash could split it into a new flag. Quoted read paths
             keep their existing no-decision outcome in compound commands.
             The only substitution exception is one complete REST read in a
             double-quoted `echo` argument, with no other executable content.

Decision for the whole Bash call (a hook decides once per tool call):
  - any call whose `-q` / `--jq` value is one of jq's own command-line
    options (matches `^--?[A-Za-z]`: `--arg`, `-r`, `--raw-output`, `-c`)
    → `permissionDecision: deny`, with a reason that says how to fix the
    command. `gh api` has no such flags, so the call could never work; a
    deny runs nothing and needs no human (#4891). It wins over ask and
    allow, and is checked after the unparseable-command and hidden-call
    asks, which are unchanged;
  - the command (heredoc bodies aside) uses ANSI-C quoting (`$'...'`), an
    unquoted `#` comment, or brace expansion (`{a,b}`, `{a..b}`): Bash
    expands or parses these unlike the tokenizer, so a word could become a
    hidden flag or command → `permissionDecision: ask` (issue #4619);
  - a direct `gh api` argument contains an unquoted expansion that Bash
    could word-split into another flag → `permissionDecision: ask` (#5558),
    except in a literal-ID loop, where the loop validator decides;
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

  - exception: one complete `for VAR in TOKEN…; do BODY; done` over unquoted
    literal IDs is allowed when every body item is a classified `gh api`
    read, a vetted `gh run view/list` or `gh pr view` read, or a literal /
    counter `echo`, with only safe pipe filters and `2>&1`. Writes still ask;
    unvetted loops still receive no decision.

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
DECISION_DENY = "deny"

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

# A `-q` / `--jq` value that is one of jq's own command-line options
# (`--arg`, `-r`, `--raw-output`, `-c`, ...), not a jq program. `gh api` has no
# such flags, so the call can never work (#4891). A program that starts with
# `-` but not a letter (`-.size`, `-1`) is valid and does not match.
_JQ_CLI_OPTION_RE = re.compile(r"^--?[A-Za-z]")

_READ_METHODS = frozenset({"GET", "HEAD"})
_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"})

# Headers that cannot change what a request does. Anything else (for example
# `X-HTTP-Method-Override`) makes the call a write.
_SAFE_HEADER_RE = re.compile(r"^\s*(accept|x-github-api-version)\s*:", re.IGNORECASE)

# Characters that let Bash rewrite an `-F`/`--field` word before `gh` reads it:
# parameter, command, arithmetic, and ANSI-C expansion (`$`, backtick), tilde,
# and globs. Any of them can turn a value into `@<file>` that the tokenizer
# never shows starting with `@` (`-F body=$'@f'`, `-F body=$F`, a glob such as
# `'body'[=]@f` matching a file named `body=@f`). The tokenizer has already
# removed the quotes, so a quoted literal matches too; that only prompts, and
# plain strings belong in `-f` (issue #4619).
_FIELD_EXPANSION_CHARS_RE = re.compile(r"[$`~*?\[\]]")

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
		"internal-review.yml",
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

# A loop counter must not change how Bash finds commands or how gh selects
# credentials, repository, proxy, configuration, or locale on each iteration.
_LOOP_ENV_NAMES = frozenset({"PATH", "IFS", "HOME", "ENV", "SHELL", "CDPATH", "TMPDIR", "LANG", "GLOBIGNORE"})
_LOOP_ENV_PREFIXES = ("BASH_", "GH_", "GIT_", "XDG_", "LC_", "LD_", "PYTHON", "HTTP_", "HTTPS_", "ALL_", "NO_")


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


class MalformedJq(Unreadable):
	"""A `gh api` call that passes a jq command-line option to `-q`/`--jq`."""

	def __init__(self, value: str):
		super().__init__(f"`--jq` value `{value}` is a jq command-line option, not a jq program")
		self.value = value


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
	# A nested $(...) has its own shell quoting rules, even inside "...".
	# Parameter expansion and arithmetic are not shell redirection contexts.
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
					match = re.match(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2", line[position:])
					if match:
						pending.append((match.group(3), match.group(1) == "-", bool(match.group(2)), line[:position]))
						position += len(match.group())
						continue
			position += 1
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


def substitution_bodies(command: str) -> list[str]:
	"""Return the bodies of the command substitutions Bash would run inside one word.

	Covers backtick substitutions anywhere outside single quotes and `$(...)`
	inside double quotes; the tokenizer keeps both inside a single token, so
	their commands never reach a segment of their own. An unquoted `$(...)` is
	left out because the tokenizer already splits it into its own segments.
	Single-quoted text is data: Bash expands nothing in it. An unterminated
	substitution runs to the end of the command, so it still counts.
	"""
	bodies: list[str] = []
	single_quoted = False
	double_quoted = False
	index = 0
	length = len(command)
	while index < length:
		character = command[index]
		if character == "\\" and not single_quoted:
			index += 2
			continue
		if character == "'" and not double_quoted:
			single_quoted = not single_quoted
			index += 1
			continue
		if single_quoted:
			index += 1
			continue
		if character == '"':
			double_quoted = not double_quoted
			index += 1
			continue
		if character == "`":
			end = index + 1
			while end < length and command[end] != "`":
				end += 2 if command[end] == "\\" else 1
			bodies.append(command[index + 1 : end])
			index = end + 1
			continue
		if double_quoted and command.startswith("$(", index):
			# Bash parses the body as a fresh command, so quotes inside it
			# are its own: a quoted ")" does not close the substitution.
			depth = 1
			end = index + 2
			inner_single = False
			inner_double = False
			while end < length and depth:
				inner = command[end]
				if inner == "\\" and not inner_single:
					end += 2
					continue
				if inner == "`" and not inner_single:
					# A nested backtick substitution is its own command: a
					# ")" or quote inside it does not touch the outer body.
					end += 1
					while end < length and command[end] != "`":
						end += 2 if command[end] == "\\" else 1
					end += 1
					continue
				if inner == "'" and not inner_double:
					inner_single = not inner_single
				elif inner == '"' and not inner_single:
					inner_double = not inner_double
				elif not inner_single and not inner_double:
					if inner == "(":
						depth += 1
					elif inner == ")":
						depth -= 1
				end += 1
			bodies.append(command[index + 2 : end - 1 if depth == 0 else length])
			index = end
			continue
		index += 1
	return bodies


def _outside_single_quotes(command: str) -> tuple[str, bool]:
	"""Split out what Bash reads outside single quotes.

	Returns the command with every single-quoted span removed, and whether an
	unescaped backtick or `$(` (a substitution Bash would run) appears outside
	single quotes. An escaped backtick or `\\$(` is text, not a substitution.
	"""
	kept: list[str] = []
	has_substitution = False
	single_quoted = False
	double_quoted = False
	index = 0
	while index < len(command):
		character = command[index]
		if character == "\\" and not single_quoted:
			kept.append(command[index : index + 2])
			index += 2
			continue
		if character == "'" and not double_quoted:
			single_quoted = not single_quoted
		elif character == '"' and not single_quoted:
			double_quoted = not double_quoted
		elif not single_quoted and (character == "`" or command.startswith("$(", index)):
			has_substitution = True
		if not single_quoted and character != "'":
			kept.append(character)
		index += 1
	return "".join(kept), has_substitution


def has_hidden_gh_api(segments: list[list[str]], heredocs: list[tuple[str, bool, str]], command: str | None = None) -> bool:
	"""True when a `gh api` call could run without being a direct invocation.

	Counts as hidden: `gh api` inside a substitution Bash would run within one
	word (`substitution_bodies(command)`), and, as a backstop, any token
	holding `$(` or a backtick next to `gh api` when `command` has a real
	substitution and a `gh api` outside single quotes (without `command`,
	that token reading always applies); `gh api` text passed to an executor command (`bash -c`,
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
	if command is not None and any(_RAW_GH_API_RE.search(body) for body in substitution_bodies(command)):
		return True
	# Backstop for the body scanner: the older token reading still applies
	# whenever the command holds a real substitution (an unescaped backtick
	# or `$(` outside single quotes) and a `gh api` outside single quotes,
	# where Bash could run it. A scanner miss therefore still asks.
	if command is None:
		token_substitution_check = True
	else:
		outside_text, has_real_substitution = _outside_single_quotes(command)
		token_substitution_check = has_real_substitution and bool(_RAW_GH_API_RE.search(outside_text))
	for tokens in segments:
		index = _command_word_index(tokens)
		if index >= len(tokens):
			continue
		command_word = tokens[index]
		direct = index + 1 < len(tokens) and _is_gh(command_word) and tokens[index + 1] == "api"
		executor = os.path.basename(command_word) in _EXECUTOR_COMMANDS or command_word.startswith("$")
		for position, token in enumerate(tokens):
			substitution_word = token_substitution_check and ("$(" in token or "`" in token)
			if _RAW_GH_API_RE.search(token) and (substitution_word or executor):
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
		elif role == "jq" and _JQ_CLI_OPTION_RE.match(value):
			raise MalformedJq(value)
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


def _brace_expansion_at(command: str, start: int) -> bool:
	"""True when the unquoted `{` at `start` opens a Bash brace expansion.

	A brace expansion is `{...}` inside one word with an unquoted `,` or `..`
	at its own depth. Quoted text inside it (whitespace included) stays part of
	the word, which the regex in `_has_unsafe_shell_syntax` misses:
	`-F{'q=1','x=@/tmp/a b'}` becomes two `-F` words. Unquoted whitespace or a
	shell metacharacter ends the word first (`{ cmd; }` is a group).
	"""
	depth = 0
	separator_seen = False
	single_quoted = False
	double_quoted = False
	index = start
	length = len(command)
	while index < length:
		character = command[index]
		if character == "\\" and not single_quoted:
			index += 2
			continue
		if character == "'" and not double_quoted:
			single_quoted = not single_quoted
		elif character == '"' and not single_quoted:
			double_quoted = not double_quoted
		elif not single_quoted and not double_quoted:
			if character in " \t\r\n;&|<>()":
				return False
			if character == "{":
				depth += 1
			elif character == "}":
				depth -= 1
				if depth == 0:
					return separator_seen
			elif depth == 1 and (character == "," or command.startswith("..", index)):
				separator_seen = True
		index += 1
	return False


def _shell_rewrite_hazard(command: str) -> str:
	"""Name a construct Bash expands or parses unlike `shell_segments`, or "".

	Each one can hide a flag or a whole command from the guard, so a command
	with `gh api` text and one of them asks (issue #4619):
	  - ANSI-C quoting (`$'...'`): Bash reads `\\'` inside it as a quote
	    character, the tokenizer as the end of the quote;
	  - an unquoted `#` that starts a word: Bash ignores the rest of the line,
	    the tokenizer keeps it, so a quote in the comment can swallow the next
	    line into one harmless-looking token;
	  - brace expansion (`{a,b}`, `{a..b}`): one word becomes several, so
	    `{x,-Fbody=@f}` adds a file-backed `-F` flag.
	The `$'` check ignores quoting on purpose, since quoting is what the
	tokenizer gets wrong there. Pass the command with heredoc bodies removed.
	"""
	if "$'" in command:
		return "ANSI-C quoting ($'...')"
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
			previous = character
			continue
		if character == "'" and not double_quoted:
			single_quoted = not single_quoted
		elif character == '"' and not single_quoted:
			double_quoted = not double_quoted
		elif not single_quoted and not double_quoted:
			if character == "#" and previous in " \t\r\n;&|()<>":
				return "a shell comment (#)"
			if character == "{" and _brace_expansion_at(command, index):
				return "brace expansion ({a,b} or {a..b})"
		previous = character
	return ""


def _unquoted_gh_api_expansion(command: str) -> bool:
	"""Find words Bash could split into extra gh api arguments after expansion.

	Keep quote state on the raw command: shlex removes it before the gh
	argument parser runs. Only words belonging to a direct call count; an
	expansion in a neighbouring echo/loop is not a gh api argument.
	"""
	segments: list[list[tuple[str, bool]]] = []
	words: list[tuple[str, bool]] = []
	word = ""
	expands = False
	in_word = False
	quote = None
	redirect_target = False
	position = 0
	while position < len(command):
		character = command[position]
		if character == "\\" and quote != "'":
			in_word = True
			word += command[position + 1 : position + 2]
			position += 2
			continue
		if character == "'" and quote != '"':
			quote = None if quote == "'" else "'"
			in_word = True
		elif character == '"' and quote != "'":
			quote = None if quote == '"' else '"'
			in_word = True
		elif quote is None and character in " \t\r\n;&|<>()":
			if in_word:
				if not redirect_target:
					words.append((word, expands))
				else:
					redirect_target = False
				word, expands, in_word = "", False, False
			if character in "<>":
				redirect_target = True
			if character in "\n;&|()" and words:
				segments.append(words)
				words = []
		else:
			word += character
			in_word = True
			if quote is None and character in "$`":
				expands = True
		position += 1
	if in_word and not redirect_target:
		words.append((word, expands))
	if words:
		segments.append(words)
	for segment in segments:
		tokens = [value for value, _ in segment]
		index = _command_word_index(tokens)
		if (
			index + 1 < len(tokens)
			and _is_gh(tokens[index])
			and tokens[index + 1] == "api"
			and any(expansion for _value, expansion in segment[index + 2 :])
		):
			return True
	return False


def _is_safe_standalone(words: list[str]) -> bool:
	name, args = words[0], words[1:]
	if name == "cd":
		return len(args) <= 1
	if name == "sleep":
		return len(args) == 1 and bool(_SLEEP_DURATION_RE.match(args[0]))
	if name == "true":
		return not args
	return name == "echo"


def _is_approvable_command(command: str, allow_echo_only: bool = False) -> bool:
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
	return saw_gh_api or allow_echo_only


# A `for` loop over literal IDs whose body only reads (CLAUDE.md §23.H, issue
# #4786). `_READ_LOOP_RE` is the fast-path test for a loop with no `gh api`
# text; `_LOOP_HEADER_RE` is the exact frame `for VAR in TOKEN...; do ...`.
# The loop counter cannot name `PATH`, `IFS`, `GH_HOST`, `GH_TOKEN`, or
# a proxy (`https_proxy`, `no_proxy`, ...), since assigning an exported
# variable could change how `gh` runs. Tokens are
# literal and never start with `-`, so `$VAR` cannot word-split, glob, carry
# `/` or `?`, or become a flag.
_READ_LOOP_RE = re.compile(
	r"^\s*for\s[^\n]*;\s*do\s[^\n]*\bgh\s+(?:run\s+(?:view|list)|pr\s+view)\b[^\n]*;\s*done\s*$"
)
_LOOP_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOOP_TOKEN_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")
_LOOP_HEADER_RE = re.compile(r"^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in((?:\s+[A-Za-z0-9._][A-Za-z0-9._-]*)+)\s*;\s*do\s")
_LOOP_VAR_PLACEHOLDER = "__GH_API_GUARD_LOOP_VAR__"

# Flags `gh run view`, `gh run list`, and `gh pr view` may carry in an
# approved loop: (boolean flags, flags that take a value). Anything else,
# `--web` / `-w` included, makes the loop not approvable. The last item is
# the number of positional arguments the subcommand accepts.
_GH_READ_SUBCOMMAND_FLAGS = {
	("run", "view"): (
		frozenset({"--log", "--log-failed", "-v", "--verbose", "--exit-status"}),
		frozenset({"--json", "-q", "--jq", "-t", "--template", "-j", "--job", "-a", "--attempt", "-R", "--repo"}),
		1,
	),
	("run", "list"): (
		frozenset({"-a", "--all"}),
		frozenset(
			{
				"-L", "--limit", "-w", "--workflow", "-b", "--branch", "-u", "--user", "-e", "--event", "-s",
				"--status", "-c", "--commit", "--created", "--json", "-q", "--jq", "-t", "--template", "-R", "--repo",
			}
		),
		0,
	),
	("pr", "view"): (
		frozenset({"-c", "--comments"}),
		frozenset({"--json", "-q", "--jq", "-t", "--template", "-R", "--repo"}),
		1,
	),
}


def _substitute_loop_var(command: str, var: str) -> str | None:
	"""Replace every `$VAR` / `${VAR}` outside single quotes with a placeholder.

	Returns None when any other `$` or any backslash appears outside single
	quotes, so the only expansion left in the command is the loop variable.
	"""
	var_re = re.compile(r"\$(?:\{" + re.escape(var) + r"\}|" + re.escape(var) + r"(?![A-Za-z0-9_]))")
	kept: list[str] = []
	single_quoted = False
	double_quoted = False
	index = 0
	while index < len(command):
		character = command[index]
		if character == "\\" and not single_quoted:
			return None
		if character == "'" and not double_quoted:
			single_quoted = not single_quoted
		elif character == '"' and not single_quoted:
			double_quoted = not double_quoted
		elif character == "$" and not single_quoted:
			match = var_re.match(command, index)
			if not match:
				return None
			kept.append(_LOOP_VAR_PLACEHOLDER)
			index = match.end()
			continue
		kept.append(character)
		index += 1
	return "".join(kept)


def _is_loop_gh_api_read(args: list[str]) -> bool:
	"""True for `gh api` arguments with the loop variable only in the endpoint path.

	The variable may appear once, in the endpoint before any `?`, and not in
	its first path segment (so it can never choose `graphql` or another
	top-level endpoint). Whether the call is a read is decided by `classify`.
	"""
	try:
		parsed = parse_gh_api_args(args)
	except Unreadable:
		return False
	with_var = [arg for arg in args if _LOOP_VAR_PLACEHOLDER in arg]
	if not with_var:
		return True
	endpoint = parsed["endpoints"][0]
	if len(with_var) != 1 or with_var[0] != endpoint:
		return False
	path, _separator, query = endpoint.lstrip("/").partition("?")
	return _LOOP_VAR_PLACEHOLDER not in query and _LOOP_VAR_PLACEHOLDER not in path.split("/", 1)[0]


def _is_loop_gh_read_subcommand(words: list[str]) -> bool:
	"""True for `gh run view`, `gh run list`, or `gh pr view` with allowlisted flags.

	Flag values must be literal; the loop variable may only be a positional
	argument.
	"""
	spec = _GH_READ_SUBCOMMAND_FLAGS.get(tuple(words[1:3]))
	if spec is None:
		return False
	bool_flags, value_flags, max_positionals = spec
	positionals = 0
	index = 3
	while index < len(words):
		word = words[index]
		index += 1
		if not word.startswith("-"):
			if _LOOP_VAR_PLACEHOLDER in word and word != _LOOP_VAR_PLACEHOLDER:
				return False
			positionals += 1
			continue
		flag, value = word, None
		if word.startswith("--") and "=" in word:
			flag, value = word.split("=", 1)
		if flag in bool_flags and value is None:
			continue
		if flag not in value_flags:
			return False
		if value is None:
			if index >= len(words):
				return False
			value = words[index]
			index += 1
		if _LOOP_VAR_PLACEHOLDER in value:
			return False
	return positionals <= max_positionals


def _is_approvable_read_loop(command: str, results: list[tuple[str, str]]) -> bool:
	"""True for exactly `for VAR in TOKEN...; do BODY; done` whose body only reads.

	CLAUDE.md §23.H, issue #4786. VAR is a shell name that cannot override
	gh settings or a proxy; each TOKEN is
	a literal `[A-Za-z0-9._-]+` that does not start with `-`. BODY is one or
	more items joined by `;` or `&&`, each one of:
	  - a `gh api` call that `classify` marks `read` (every entry of `results`
	    must be a read; a routine write keeps today's result), with `$VAR` /
	    `${VAR}` only in the endpoint path. Every `gh api` body item must be
	    one of the calls `evaluate` classified, so a quoted or split-word call
	    (`gh 'api' ...`, `"gh" api ...`, `gh ap''i ...`) keeps today's
	    no-decision result;
	  - `gh run view`, `gh run list`, or `gh pr view` with allowlisted flags
	    and `$VAR` only as a positional argument;
	each optionally piped into the safe filters (`head`/`tail -n N`, `wc -l`,
	`sort ...`); or `echo` with literal words and `$VAR`. `2>&1` is the only
	redirect. No other `$`, backslash, backtick, glob, subshell, nested loop,
	file redirect, or command, so approving the loop approves nothing the
	guard has not read.
	"""
	if any(kind != KIND_READ for kind, _description in results):
		return False
	reduced = _REDIRECT_TO_STDERR_RE.sub("", command)
	header = _LOOP_HEADER_RE.match(reduced)
	if not header or _LOOP_VAR_PLACEHOLDER in reduced:
		return False
	var, loop_tokens = header.group(1), header.group(2).split()
	upper_var = var.upper()
	if (
		not _LOOP_VAR_RE.match(var) or "proxy" in var.lower()
		or upper_var in _LOOP_ENV_NAMES or upper_var.endswith("_PROXY")
		or upper_var.startswith(_LOOP_ENV_PREFIXES)
	):
		return False
	if not all(_LOOP_TOKEN_RE.match(token) for token in loop_tokens):
		return False
	substituted = _substitute_loop_var(reduced, var)
	if substituted is None or _has_unsafe_shell_syntax(substituted):
		return False
	lexer = shlex.shlex(substituted, posix=True, punctuation_chars=";&|>")
	lexer.commenters = ""
	lexer.whitespace = " \t\r"
	lexer.whitespace_split = True
	try:
		tokens = list(lexer)
	except ValueError:
		return False
	items: list[list[list[str]]] = [[[]]]
	separators: list[str] = []
	for token in tokens:
		if token in (";", "&&"):
			separators.append(token)
			items.append([[]])
		elif token == "|":
			items[-1].append([])
		elif set(token) <= set(";&|>"):
			return False
		else:
			items[-1][-1].append(token)
	# Frame: header item, one or more body items, then `done` as the last
	# word, with `;` right after the header and right before `done`.
	if len(items) < 3 or items[0] != [["for", var, "in", *loop_tokens]] or items[-1] != [["done"]]:
		return False
	if separators[0] != ";" or separators[len(items) - 2] != ";":
		return False
	body = [[list(words) for words in pipeline] for pipeline in items[1:-1]]
	if body[0][0][:1] != ["do"]:
		return False
	body[0][0] = body[0][0][1:]
	# Every `gh api` item must be one of the calls `evaluate` classified. The
	# fast path passes no results, and a quoted call (`gh 'api' -X DELETE ...`,
	# `"gh" api ...`) never matches `_RAW_GH_API_RE`, so it was never
	# classified and a loop holding one is not approvable.
	api_items = sum(
		1 for pipeline in body if len(pipeline[0]) >= 2 and pipeline[0][0] == "gh" and pipeline[0][1] == "api"
	)
	if api_items != len(results):
		return False
	for pipeline in body:
		if any(not words for words in pipeline):
			return False
		head = pipeline[0]
		if head[0] == "echo":
			if len(pipeline) != 1 or any(
				_LOOP_VAR_PLACEHOLDER in word and word != _LOOP_VAR_PLACEHOLDER
				and _ECHO_READ_PLACEHOLDER not in word for word in head[1:]
			):
				return False
			continue
		if head[0] != "gh" or len(head) < 2:
			return False
		if head[1] == "api":
			if not _is_loop_gh_api_read(head[2:]):
				return False
		elif not _is_loop_gh_read_subcommand(head):
			return False
		if not all(_is_safe_filter(words) for words in pipeline[1:]):
			return False
	return True


_ECHO_READ_PLACEHOLDER = "__GH_API_GUARD_ECHO_READ__"
_ECHO_READ_HINT = (
	" For reads, run the call separately, e.g. `for n in 1 2; do echo $n; "
	"gh api repos/o/r/issues/$n --jq .title; done`."
)


def _approved_echo_read_command(command: str) -> tuple[str, list[tuple[str, str]]] | None:
	"""Validate quoted echo substitutions and return their inert replacement.

	Only a complete double-quoted substitution containing one REST read is
	accepted. The #4786 loop frame validates the surrounding command and the
	loop variable; it is never expanded in flags, fields, or the query string.
	"""
	if _ECHO_READ_PLACEHOLDER in command or _shell_rewrite_hazard(command):
		return None
	loop_header = _LOOP_HEADER_RE.match(command)
	loop_var = loop_header.group(1) if loop_header else None
	rewritten: list[str] = []
	read_results: list[tuple[str, str]] = []
	single_quoted = False
	double_quoted = False
	index = 0
	while index < len(command):
		character = command[index]
		if character == "\\" and not single_quoted:
			rewritten.append(command[index : index + 2])
			index += 2
			continue
		if character == "'" and not double_quoted:
			single_quoted = not single_quoted
		elif character == '"' and not single_quoted:
			double_quoted = not double_quoted
		if double_quoted and command.startswith("$(", index):
			start = index + 2
			end = start
			inner_single = False
			inner_double = False
			while end < len(command):
				inner = command[end]
				if inner in "\\`" or command.startswith("$(", end):
					return None
				if inner == "'" and not inner_double:
					inner_single = not inner_single
				elif inner == '"' and not inner_single:
					inner_double = not inner_double
				elif inner == ")" and not inner_single and not inner_double:
					break
				end += 1
			if end == len(command):
				return None
			body = command[start:end]
			if not _RAW_GH_API_RE.search(body) or _shell_rewrite_hazard(body) or "$(" in body or "`" in body:
				return None
			if "2>&1" in body and (body.count("2>&1") != 1 or not body.rstrip().endswith("2>&1")):
				return None
			if loop_var:
				normalized_body = _substitute_loop_var(body, loop_var)
			else:
				normalized_body = body if "$" not in body else None
			if normalized_body is None or "$" in normalized_body or not _is_approvable_command(normalized_body):
				return None
			try:
				body_segments = shell_segments(normalized_body)
			except ValueError:
				return None
			if (len(body_segments) != 1 or len(body_segments[0]) < 3
				or body_segments[0][0] != "gh" or body_segments[0][1] != "api"):
				return None
			args = body_segments[0][2:]
			if loop_var and not _is_loop_gh_api_read(args):
				return None
			try:
				parsed = parse_gh_api_args(args)
			except Unreadable:
				return None
			endpoint = parsed["endpoints"][0].lstrip("/")
			if parsed["method"] not in _READ_METHODS or endpoint.split("?", 1)[0] == "graphql":
				return None
			result = classify(parsed, normalized_body, lambda: "")
			if result[0] != KIND_READ:
				return None
			read_results.append(result)
			rewritten.append(_ECHO_READ_PLACEHOLDER)
			index = end + 1
			continue
		rewritten.append(character)
		index += 1
	if not read_results or single_quoted or double_quoted:
		return None
	reduced = "".join(rewritten)
	if _shell_rewrite_hazard(reduced):
		return None
	try:
		segments = shell_segments(reduced)
	except ValueError:
		return None
	if sum(token.count(_ECHO_READ_PLACEHOLDER) for segment in segments for token in segment) != len(read_results):
		return None
	for segment in segments:
		command_index = _command_word_index(segment)
		if any(_ECHO_READ_PLACEHOLDER in token for token in segment):
			if command_index >= len(segment) or segment[command_index] != "echo":
				return None
			if any(_ECHO_READ_PLACEHOLDER in token for token in segment[: command_index + 1]):
				return None
	return reduced, read_results


def classify(parsed: dict, command: str, repo_slug_lookup) -> tuple[str, str]:
	"""Return `(kind, description)` for one parsed `gh api` call."""
	method = parsed["method"]
	endpoint = parsed["endpoints"][0]
	description = f"{method} {endpoint}"
	for header in parsed["headers"]:
		if not _SAFE_HEADER_RE.match(header):
			return KIND_WRITE, f"{description} with header `{header.split(':', 1)[0].strip()}`"
	# A file-backed `-F`/`--field` value (`@<file>`, or `@-` for stdin) and
	# `--input` make `gh` read local data and send it, on every method and
	# endpoint, GraphQL included (issue #4619). Checked before the GraphQL and
	# read-method branches so a GET or a GraphQL variable cannot carry a file.
	# `-f`/`--raw-field` values are sent literally, so `@` there reads nothing.
	# A field word the shell could rewrite (`_FIELD_EXPANSION_CHARS_RE`) could
	# become file-backed after the guard has looked, so it counts as one.
	file_backed_keys = []
	expandable_keys = []
	for field_role, field_key, field_value in parsed["fields"]:
		if field_role != "field":
			continue
		if field_value.startswith("@"):
			file_backed_keys.append(field_key)
		elif _FIELD_EXPANSION_CHARS_RE.search(f"{field_key}={field_value}"):
			expandable_keys.append(field_key)
	if file_backed_keys:
		named_fields = ", ".join(f"`-F {key}=@...`" for key in file_backed_keys)
		if len(file_backed_keys) == 1:
			return KIND_WRITE, f"{description} with file-backed field {named_fields} (gh reads a local file)"
		return KIND_WRITE, f"{description} with file-backed fields {named_fields} (gh reads local files)"
	if expandable_keys:
		named_fields = ", ".join(f"`-F {key}=...`" for key in expandable_keys)
		return KIND_WRITE, f"{description} with field {named_fields} that the shell could expand into a file-backed `@<file>` value"
	if parsed["input"] is not None:
		return KIND_WRITE, f"{description} with --input"
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

	Returns `(decision, reason)`: decision is "deny", "ask", "allow", or None
	(no decision; the normal permission flow applies).
	"""
	if payload.get("tool_name", "Bash") != "Bash":
		return None, ""
	tool_input = payload.get("tool_input")
	command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
	if not isinstance(command, str):
		return None, ""
	if not _RAW_GH_API_RE.search(command):
		if _READ_LOOP_RE.match(command) and _is_approvable_read_loop(command, []):
			return DECISION_ALLOW, "gh api guard (CLAUDE.md §23.H): for loop over literal IDs whose body only reads."
		if not command.lstrip().startswith("for "):
			return None, ""

	stripped_command, heredocs = strip_heredoc_bodies(command)
	if not heredocs:
		approved_echo = _approved_echo_read_command(command)
		if approved_echo is not None:
			rewritten, echo_results = approved_echo
			outer_invocations = gh_api_invocations(shell_segments(rewritten))
			outer_results: list[tuple[str, str]] = []
			for args in outer_invocations:
				try:
					outer_results.append(classify(parse_gh_api_args(args), rewritten, lambda: ""))
				except Unreadable:
					break
			if len(outer_results) == len(outer_invocations) and all(kind == KIND_READ for kind, _ in outer_results):
				if _is_approvable_command(rewritten, allow_echo_only=True) or _is_approvable_read_loop(rewritten, outer_results):
					return DECISION_ALLOW, "gh api guard (CLAUDE.md §23.H): echo of a read-only gh api substitution."
	try:
		segments = shell_segments(stripped_command)
	except ValueError:
		return DECISION_ASK, (
			"gh api guard (CLAUDE.md §23.H): the command could not be parsed, so its gh api call is treated as a write."
			+ (_ECHO_READ_HINT if "$(" in command or _LOOP_HEADER_RE.match(command) else "")
		)
	invocations = gh_api_invocations(segments)
	if has_hidden_gh_api(segments, heredocs, stripped_command):
		return DECISION_ASK, (
			"gh api guard (CLAUDE.md §23.H): a gh api call could run hidden inside a $(...) or backtick word, "
			"an executor (bash -c, sudo, xargs, python3, ...), or a heredoc fed to one, so it is treated as a "
			"write. Run it as its own plain command." + (_ECHO_READ_HINT if "$(" in command else "")
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
	malformed_jq_values: list[str] = []
	for args in invocations:
		try:
			results.append(classify(parse_gh_api_args(args), command, repo_slug_lookup))
		except MalformedJq as exc:
			malformed_jq_values.append(exc.value)
		except Unreadable as exc:
			results.append((KIND_WRITE, f"unreadable call ({exc})"))

	if malformed_jq_values:
		# A deny runs nothing, so it is narrower than the ask it replaces, and
		# the session can fix the command in the same turn instead of waiting
		# at a prompt nobody answers (#4891). It wins over ask and allow.
		got = ", ".join(f'"{value}"' for value in malformed_jq_values)
		return DECISION_DENY, (
			"gh api guard (CLAUDE.md §23.H): gh api --jq takes a jq program, not jq's command-line options "
			f"(got {got}). gh api has no --arg, -r, or -c: put the value into the jq program itself, or pipe "
			"the output to jq with its own options. A program that starts with a minus sign goes in "
			"parentheses, e.g. --jq '(-length)'. Nothing ran."
		)

	hazard = _shell_rewrite_hazard(stripped_command)
	# Only a fully validated literal-ID loop may bypass the shell-hazard check.
	if hazard and not (_LOOP_HEADER_RE.match(command) and _is_approvable_read_loop(command, results)):
		return DECISION_ASK, (
			f"gh api guard (CLAUDE.md §23.H): the command uses {hazard}, which Bash expands or parses differently "
			"from this guard, so a word could turn into a hidden flag (such as a file-backed -F field) or command. "
			"Write the command without it." + (_ECHO_READ_HINT if "$(" in command or _LOOP_HEADER_RE.match(command) else "")
		)
	if _unquoted_gh_api_expansion(stripped_command) and not _is_approvable_read_loop(command, results):
		return DECISION_ASK, (
			"gh api guard (CLAUDE.md §23.H): an unquoted gh api argument expansion can word-split into "
			"a new flag or command. Quote the expanded word or run it with explicit arguments."
			+ (_ECHO_READ_HINT if _LOOP_HEADER_RE.match(command) else "")
		)

	writes = [description for kind, description in results if kind == KIND_WRITE]
	if writes:
		return DECISION_ASK, (
			"gh api guard (CLAUDE.md §23.H): not a read or a §23.B routine write: " + "; ".join(writes) + "."
			+ (_ECHO_READ_HINT if _LOOP_HEADER_RE.match(command) else "")
		)
	if invocations and _is_approvable_command(command):
		summary = "; ".join(f"{kind} call {description}" for kind, description in results)
		return DECISION_ALLOW, f"gh api guard (CLAUDE.md §23.H): {summary}."
	if invocations and _is_approvable_read_loop(command, results):
		summary = "; ".join(f"{kind} call {description}" for kind, description in results)
		return DECISION_ALLOW, f"gh api guard (CLAUDE.md §23.H): for loop over literal IDs whose body only reads: {summary}."
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

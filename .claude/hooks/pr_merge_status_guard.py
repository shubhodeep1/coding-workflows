#!/usr/bin/env python3
"""PreToolUse guard for merged-PR writes and allowlisted API mutations.

Implements CLAUDE.md §21 and protects the §22/§24 API permission allowlist.

Long-lived Claude Code sessions (days to weeks) outlive the PRs they open. When
the PR for the working branch merges mid-session, further commits to that same
branch land on history that is already in the default branch and no longer has
an open PR carrying it anywhere — the work is silently stranded. Prose
instructions do not survive that timescale; this hook does, because the harness
runs it on every Bash tool call regardless of what the model remembers.

Detection rule — all three conditions must hold before the command is blocked:

  1. A *merged* PR exists whose head ref is the current branch, AND
  2. no *open* PR exists for the current branch, AND
  3. that merged PR's head commit is an ancestor of HEAD — i.e. the pending
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

"The current branch" and HEAD are those of the repository each guarded git
call actually runs in, not the session's checkout (issue #5144): the hook's
`cwd`, moved by every earlier `cd <path>` in the same command, then by
`git -C <path>`, then by `GIT_DIR=<path>` / `--git-dir`. A push that names a
refspec is judged on the branch it writes to — `git push origin HEAD:<ref>`
checks `<ref>`, with the refspec's source as the commit that would stack on
it — so a detached worktree pushing to an open PR's branch is allowed while a
worktree pushing merged history to a merged branch is blocked, whatever the
main checkout is on. Deletions and tag refspecs are not judged. When the
directory cannot be resolved (a variable, a subshell, `pushd`, a `cd` joined
by `||`, `&` or `|`, a path that does not exist yet), or a refspec cannot be
turned into one branch (a variable, a glob, a `heads/` shorthand, a source
starting with `-`), that call is judged on the session checkout as before
and a warning names the reason. Each `(slug, branch)` pair is looked up once
per hook call.

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

Escape hatch: set CLAUDE_PR_MERGE_GUARD=off to disable only the merged-PR check.
The API-write confirmation safeguard remains active.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
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
	{"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path"}
)

# Shell punctuation we treat as command separators when tokenizing a Bash line.
_SHELL_PUNCTUATION_CHARS = ";&|\n<>"

# Effective-repository resolution (issue #5144). A path word containing any of
# these needs shell expansion the guard does not perform.
_UNRESOLVABLE_PATH_MARKERS = ("$", "`", "*", "?", "[")
# Builtins that change the directory in ways the command walker does not model.
_UNMODELLED_DIRECTORY_COMMANDS = frozenset({"pushd", "popd"})
# Shell keywords that can prefix a directory change inside the same segment
# (`if cd x; then …`), which the walker cannot place in the command's flow.
_DIRECTORY_KEYWORD_PREFIXES = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "time"})
# `git push` options that consume the following word as their value.
_PUSH_OPTS_WITH_VALUE = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
_PUSH_DELETE_FLAGS = frozenset({"-d", "--delete"})
# `git push` options that write every local branch. git accepts any
# unambiguous prefix of a long option (`--al`, `--mir`), so a word of four or
# more characters that starts one of these counts as it.
_PUSH_BULK_FLAGS = ("--all", "--branches", "--mirror")
# The only `git push` option that, given with no refspec, writes no branch.
# git also accepts its unambiguous prefixes `--ta` and `--tag` (`--t` is
# ambiguous with `--thin` and refused).
_PUSH_TAGS_ONLY_FLAG = "--tags"
# A push refspec word containing any of these is a shell expansion or a
# pattern the guard cannot turn into one branch name.
_UNRESOLVABLE_REFSPEC_MARKERS = ("$", "`", "*", "?", "[", "{", "~")
# Destination shorthands git expands against the remote's refs (`heads/x` →
# `refs/heads/x`), which make the branch name ambiguous.
_AMBIGUOUS_REFSPEC_PREFIXES = ("heads/", "tags/", "remotes/")

_API_WRITE_METHODS = frozenset({"PUT", "POST", "PATCH"})
_API_WRITE_URL_PREFIXES = (
	"https://api.digitalocean.com/",
	"https://api.cloudflare.com/",
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


class LookupUnavailable(Exception):
	"""Raised when PR state cannot be determined; callers must fail open."""


def _run(argv: list[str], cwd: str | None, timeout: int) -> tuple[int, str, str]:
	"""Run a subprocess, returning (returncode, stdout, stderr).

	Never raises for process-level failures — a missing binary or a timeout is
	reported as a non-zero return code so every caller funnels into the same
	fail-open path.
	"""
	try:
		proc = subprocess.run(
			argv,
			cwd=cwd,
			capture_output=True,
			text=True,
			timeout=timeout,
			check=False,
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


def _shell_segments_with_separators(command: str) -> list[tuple[list[str], str, str]]:
	"""`_shell_segments`, keeping the separator before and after each segment.

	Returns (tokens, separator before, separator after), with "" at the ends
	of the command. The segments are exactly those of `_shell_segments`.
	"""
	lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION_CHARS)
	lexer.commenters = ""
	lexer.whitespace = " \t\r"
	lexer.whitespace_split = True
	# One pass collects alternating segments and (merged) separator runs.
	pieces: list[list[str] | str] = []
	for token in lexer:
		if token and set(token) <= set(_SHELL_PUNCTUATION_CHARS):
			if pieces and isinstance(pieces[-1], str):
				pieces[-1] += token
			else:
				pieces.append(token)
			continue
		if pieces and isinstance(pieces[-1], list):
			pieces[-1].append(token)
		else:
			pieces.append([token])
	segments: list[tuple[list[str], str, str]] = []
	for index, piece in enumerate(pieces):
		if isinstance(piece, list):
			before = pieces[index - 1] if index > 0 else ""
			after = pieces[index + 1] if index + 1 < len(pieces) else ""
			segments.append((piece, before, after))
	return segments


def _is_sequential_separator(separator: str) -> bool:
	"""True for "", `;`, `&&` and newlines: the next segment runs in the same
	shell after this one. `||`, `&`, `|` and redirections are not."""
	return separator.replace("\n", "") in ("", ";", "&&")


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


def _api_write_requires_confirmation(command: str) -> bool:
	"""Return whether an allowlisted API write uses non-canonical curl options.

	The settings rules are necessarily prefix matches. Keep their silent path
	limited to one explicit method and destination followed only by headers and
	request-body options; anything capable of changing curl's method, URL, or
	transfer list must go through the normal harness prompt.
	"""
	try:
		segments = _shell_segments(command)
	except ValueError:
		return command.lstrip().startswith(_API_WRITE_COMMAND_PREFIXES)

	if not segments:
		return False
	tokens = segments[0]
	if len(tokens) < 6 or tokens[:4] != ["curl", "-q", "-sS", "-X"]:
		return False
	if tokens[4] not in _API_WRITE_METHODS:
		return False
	if not any(tokens[5].startswith(prefix) for prefix in _API_WRITE_URL_PREFIXES):
		return False
	# The URL token is already host-gated above. Prompt only for expansions that
	# can synthesize shell words before curl sees the approved API URL shape.
	if any(marker in tokens[5] for marker in ("$", "{", "[", "*", "?")):
		return True
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
		return True

	index = 6
	while index < len(tokens):
		token = tokens[index]
		if token in _API_WRITE_VALUE_OPTIONS:
			if index + 1 >= len(tokens):
				return True
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
		return True
	return False


def git_subcommands(command: str) -> set[str]:
	"""Return the set of git subcommands invoked by a shell command string.

	Only counts `git` when it is the first real token of a shell segment, after
	any leading `VAR=value` assignments. That keeps `man git commit` and
	`echo "git commit"` from tripping the guard, at the cost of missing
	wrapper-prefixed invocations like `sudo git commit` — an acceptable trade,
	since a false block is more disruptive than a missed check on a rare form.
	"""
	found: set[str] = set()
	try:
		segments = _shell_segments(command)
	except ValueError:
		# Unbalanced quotes — the command is not something we can read.
		return found
	for tokens in segments:
		# Drop leading environment assignments (`GIT_DIR=... git commit`).
		index = 0
		while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
			index += 1
		if index >= len(tokens):
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
	return found


class GuardTarget(NamedTuple):
	"""One (repository, branch) pair a guarded git invocation writes to.

	`cwd` is the directory the guard runs git in to judge it: the effective
	repository of the invocation, or the session checkout when that could not
	be resolved (then `fallback_reason` says why). `branch` is the branch the
	write lands on, "" for the checked-out branch of `cwd`. `tip` is the
	commit that would stack on it: HEAD, or a push refspec's source.
	`bulk_reason` is set for a push that writes more branches than the one
	judged (`--all`, `--branches`, `--mirror`, the `:` matching refspec, a
	`*` pattern): the checked-out branch is judged as before and the push
	also asks for confirmation.
	"""

	subcommand: str
	cwd: str
	branch: str
	tip: str
	reaches_remote: bool
	fallback_reason: str = ""
	bulk_reason: str = ""


def _resolve_guard_path(value: str, base: str) -> tuple[str | None, str]:
	"""Resolve a `cd` / `-C` / `GIT_DIR` path word against `base`.

	Returns (absolute directory, "") or (None, reason) when the word needs
	shell expansion the guard does not perform, or names no existing
	directory this process can enter at hook time.
	"""
	if not value:
		return None, "an empty path"
	if value == "-":
		return None, "`cd -`"
	if any(marker in value for marker in _UNRESOLVABLE_PATH_MARKERS):
		return None, f"`{value}` needs shell expansion"
	if value.startswith("~"):
		if value != "~" and not value.startswith("~/"):
			return None, f"`{value}` names another user's home directory"
		value = os.path.expanduser(value)
	resolved = os.path.normpath(os.path.join(base, value))
	if not os.path.isdir(resolved):
		return None, f"`{resolved}` is not an existing directory"
	if not os.access(resolved, os.X_OK):
		# `cd` into it fails, so the commands after it run where they were.
		return None, f"`{resolved}` cannot be entered (no search permission)"
	return resolved, ""


def _cd_destination(args: list[str], base: str) -> tuple[str | None, str]:
	"""Directory a `cd <args>` segment moves to, or (None, reason)."""
	operands: list[str] = []
	options_done = False
	for arg in args:
		if not options_done and arg == "--":
			options_done = True
			continue
		if not options_done and arg.startswith("-") and arg != "-":
			continue
		operands.append(arg)
	if not operands:
		return _resolve_guard_path("~", base)
	if len(operands) > 1:
		return None, "`cd` with several operands"
	return _resolve_guard_path(operands[0], base)


def _push_refspec_targets(args: list[str], repo_dir: str, session_cwd: str) -> list[GuardTarget]:
	"""Targets for `git push <args>` run in `repo_dir`.

	`<src>:<dst>` (with an optional leading `+` and `refs/heads/` prefix) is
	judged on `<dst>` with `<src>` as the tip; a refspec without a colon on
	the branch it names; `HEAD` on the checked-out branch. Deletions
	(`--delete`, `:<dst>`) and non-branch refs (`refs/tags/…`) land no commits
	on a branch and yield no target, and so does `--tags` with no refspec,
	which pushes only tags. A push that names no refspec judges the
	checked-out branch with HEAD, as before.

	A bulk push — `--all`, `--branches`, `--mirror` (or a prefix git expands
	to one), or the `:` matching refspec — writes branches the guard cannot
	list one by one: it judges the checked-out branch and carries a
	`bulk_reason`, so the hook also asks for confirmation.

	A refspec the guard cannot turn into one branch — a shell expansion or
	pattern (`$B`, `refs/heads/*`), a `heads/` / `tags/` / `remotes/`
	shorthand git expands against the remote, or a source starting with `-`
	that git would read as an option — keeps the old behaviour: the session
	checkout's branch and HEAD, with a `fallback_reason`. A `*` pattern that
	can write several branches also carries a `bulk_reason`.
	"""
	positionals: list[str] = []
	index = 0
	options_done = False
	bulk_option_reason = ""
	pushes_tags = False
	while index < len(args):
		token = args[index]
		if not options_done:
			if token == "--":
				options_done = True
				index += 1
				continue
			if token in _PUSH_DELETE_FLAGS:
				return []
			if token in _PUSH_OPTS_WITH_VALUE:
				index += 2
				continue
			if _is_push_bulk_flag(token):
				bulk_option_reason = bulk_option_reason or f"`git push {token}` writes every local branch"
				index += 1
				continue
			if _is_push_tags_only_flag(token):
				pushes_tags = True
				index += 1
				continue
			if token.startswith("-") and len(token) > 1:
				index += 1
				continue
		positionals.append(token)
		index += 1

	if bulk_option_reason:
		# git refuses a refspec beside these options, so nothing narrows the push.
		return [GuardTarget("push", repo_dir, "", "HEAD", True, bulk_reason=bulk_option_reason)]
	refspecs = positionals[1:]
	if not refspecs:
		if pushes_tags:
			# `git push --tags <remote>` pushes refs/tags/* and no branch.
			return []
		return [GuardTarget("push", repo_dir, "", "HEAD", True)]
	targets: list[GuardTarget] = []
	for refspec in refspecs:
		spec = refspec[1:] if refspec.startswith("+") else refspec
		if spec == ":":
			# "Matching" push: it writes every branch that also exists on the
			# remote. Judge the checked-out branch, as before, and ask.
			targets.append(
				GuardTarget(
					"push",
					repo_dir,
					"",
					"HEAD",
					True,
					bulk_reason="`git push` with the `:` refspec writes every branch that also exists on the remote",
				)
			)
			continue
		if _refspec_writes_no_branch(spec):
			continue
		unresolvable_reason = _unresolvable_refspec_reason(refspec)
		if unresolvable_reason:
			targets.append(
				GuardTarget(
					"push", session_cwd, "", "HEAD", True, unresolvable_reason, _pattern_refspec_bulk_reason(spec)
				)
			)
			continue
		source, colon, destination = spec.partition(":")
		if not colon:
			destination = source
		if not source:
			continue
		# `<src>:` with an empty destination is not a valid push refspec: git
		# exits with `fatal: invalid refspec` and writes nothing. It is judged
		# as for a push without a refspec: the checked-out branch with HEAD.
		if colon and not destination:
			targets.append(GuardTarget("push", repo_dir, "", "HEAD", True))
			continue
		if destination.startswith("refs/heads/"):
			destination = destination[len("refs/heads/") :]
		elif destination.startswith("refs/"):
			continue
		if destination in ("HEAD", "@"):
			destination = ""
		tip = "HEAD" if source in ("HEAD", "@") else source
		targets.append(GuardTarget("push", repo_dir, destination, tip, True))
	return targets


def _is_push_bulk_flag(token: str) -> bool:
	"""Whether a `git push` word is `--all`, `--branches` or `--mirror`, or a
	prefix of four or more characters git would expand to one."""
	return len(token) >= 4 and any(flag.startswith(token) for flag in _PUSH_BULK_FLAGS)


def _is_push_tags_only_flag(token: str) -> bool:
	"""Whether a `git push` word is `--tags` or a prefix git expands to it
	(`--ta`, `--tag`)."""
	return len(token) >= 4 and _PUSH_TAGS_ONLY_FLAG.startswith(token)


def _pattern_refspec_bulk_reason(spec: str) -> str:
	"""The `bulk_reason` for a `*` pattern refspec (without its leading `+`)
	that can write several branches; "" for anything else.

	A word with `$` or a backtick is a shell expansion, not a pattern; a
	deletion (`:<dst>`) or a tag pattern writes no branch.
	"""
	if "*" not in spec or "$" in spec or "`" in spec or spec.startswith("-"):
		return ""
	pattern_source, pattern_colon, pattern_destination = spec.partition(":")
	if not pattern_colon:
		pattern_destination = pattern_source
	if not pattern_source or pattern_destination.startswith("refs/tags/"):
		return ""
	return f"`git push` with the pattern refspec `{spec}` can write several branches"


def _refspec_writes_no_branch(spec: str) -> bool:
	"""Whether a refspec (without its leading `+`) writes only a non-branch
	ref, such as `refs/tags/*` or `refs/tags/v*:refs/tags/v*`.

	A `*` / `?` / `[` pattern keeps its literal `refs/<kind>/` prefix, so a
	tag pattern is skipped like a single tag. A word with `$`, a backtick or
	`{` can expand into other refspecs and is left to the unresolvable check.
	"""
	if any(marker in spec for marker in ("$", "`", "{")):
		return False
	pattern_source, pattern_colon, pattern_destination = spec.partition(":")
	if not pattern_colon:
		pattern_destination = pattern_source
	return pattern_destination.startswith("refs/") and not pattern_destination.startswith("refs/heads/")


def _unresolvable_refspec_reason(refspec: str) -> str:
	"""Why a push refspec cannot be judged on a single branch; "" when it can."""
	if any(marker in refspec for marker in _UNRESOLVABLE_REFSPEC_MARKERS):
		return f"refspec `{refspec}` needs shell expansion or is a pattern"
	spec = refspec[1:] if refspec.startswith("+") else refspec
	source, colon, destination = spec.partition(":")
	if not colon:
		destination = source
	if source.startswith("-") or destination.startswith("-"):
		return f"refspec `{refspec}` starts with `-`, which git would read as an option"
	if destination.startswith(_AMBIGUOUS_REFSPEC_PREFIXES):
		return f"refspec destination `{destination}` is a shorthand git expands against the remote's refs"
	return ""


def _git_invocation_targets(
	args: list[str], assignments: dict[str, str], directory: str | None, unresolved: str, session_cwd: str
) -> list[GuardTarget]:
	"""Targets for one `git <args>` segment; [] when it is not guarded.

	`directory` is where the segment runs after earlier `cd`s (None when that
	is unknown, with `unresolved` saying why). `git -C <path>` then applies,
	then `GIT_DIR=<path>` / `--git-dir`, each relative to the directory
	reached so far, as git itself resolves them. An unresolvable directory
	falls back to the session checkout's branch and HEAD.
	"""
	repo_dir = directory
	reason = unresolved
	git_dir = assignments.get("GIT_DIR")
	subcommand = ""
	index = 0
	while index < len(args):
		token = args[index]
		if not token.startswith("-"):
			subcommand = token
			index += 1
			break
		if token == "-C":
			value = args[index + 1] if index + 1 < len(args) else ""
			if repo_dir is not None:
				repo_dir, path_reason = _resolve_guard_path(value, repo_dir)
				if repo_dir is None:
					reason = f"`git -C` names {path_reason}"
			index += 2
			continue
		if token == "--git-dir":
			git_dir = args[index + 1] if index + 1 < len(args) else ""
			index += 2
			continue
		if token.startswith("--git-dir="):
			git_dir = token.split("=", 1)[1]
			index += 1
			continue
		if token in GIT_GLOBAL_OPTS_WITH_VALUE:
			index += 2
			continue
		index += 1
	if subcommand not in GUARDED_SUBCOMMANDS:
		return []
	if git_dir is not None and repo_dir is not None:
		repo_dir, path_reason = _resolve_guard_path(git_dir, repo_dir)
		if repo_dir is None:
			reason = f"the git directory is {path_reason}"
	reaches_remote = subcommand == "push"
	if repo_dir is None:
		# A bulk push still asks when its directory is unknown. A push that
		# writes no branch (a deletion, tags only) lands no commits whatever
		# the directory, so it yields no target here either.
		fallback_bulk_reason = ""
		if reaches_remote:
			parsed_push_targets = _push_refspec_targets(args[index:], session_cwd, session_cwd)
			if not parsed_push_targets:
				return []
			fallback_bulk_reason = next(
				(target.bulk_reason for target in parsed_push_targets if target.bulk_reason),
				"",
			)
		return [GuardTarget(subcommand, session_cwd, "", "HEAD", reaches_remote, reason, fallback_bulk_reason)]
	if subcommand == "commit":
		return [GuardTarget(subcommand, repo_dir, "", "HEAD", False)]
	return _push_refspec_targets(args[index:], repo_dir, session_cwd)


def guard_targets(command: str, session_cwd: str) -> list[GuardTarget]:
	"""Every (repository, branch) pair the guarded git calls in `command` write to.

	Replays the shell segments in order. `cd <path>` moves the effective
	directory for later segments; `pushd`/`popd`, a subshell or command group,
	a shell keyword ahead of a directory change, `export GIT_DIR` or a bare
	`GIT_DIR=` assignment, and any path word that needs expansion make it
	unknown for the rest of the command, and the git calls after that point
	keep the old behaviour (the session checkout) with a `fallback_reason`.
	So does a `cd` after `&&` behind a command that may fail, once its `&&`
	chain ends (`a && cd x; git push`, `a && cd x && b || git push`), and a
	`cd` inside a list sent to the background with `&`.
	The same segments count as git calls as in `git_subcommands`, so nothing
	it ignores is judged here.
	"""
	try:
		segments = _shell_segments_with_separators(command)
	except ValueError:
		return []
	targets: list[GuardTarget] = []
	directory: str | None = session_cwd
	unresolved = ""
	# One `&&` / `||` list at a time (the segments between `;`, `&` and
	# newlines). A `cd` after `&&` that follows a command which may fail is
	# skipped when that command fails; the rest of its `&&` chain is skipped
	# with it, but whatever follows `||` or the end of the list runs in either
	# directory. A list ended by `&` runs in a background subshell, so a `cd`
	# inside it does not reach the commands after it.
	list_start_directory = directory
	list_may_fail = False
	conditional_cd = False
	for tokens, separator_before, separator_after in segments:
		list_boundary = separator_before.replace("\n", "")
		if list_boundary in ("", ";", "&"):
			if directory is not None:
				if list_boundary == "&" and directory != list_start_directory:
					directory, unresolved = None, "a `cd` in a command list run in the background by `&`"
				elif conditional_cd:
					directory, unresolved = None, "a `cd` after `&&` that may not have run"
			list_start_directory = directory
			list_may_fail = False
			conditional_cd = False
		elif list_boundary == "||" and conditional_cd and directory is not None:
			directory, unresolved = None, "a `cd` after `&&` that may not have run"
		may_fail_before_segment = list_may_fail
		list_may_fail = True
		assignments: dict[str, str] = {}
		index = 0
		while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
			name, value = tokens[index].split("=", 1)
			assignments[name] = value
			index += 1
		if index >= len(tokens):
			if "GIT_DIR" in assignments and directory is not None:
				directory, unresolved = None, "a `GIT_DIR=` assignment may apply to later commands"
			continue
		executable = tokens[index]
		args = tokens[index + 1 :]
		if directory is not None:
			if executable.startswith(("(", "{")):
				directory, unresolved = None, "a subshell or command group"
			elif executable in _UNMODELLED_DIRECTORY_COMMANDS:
				directory, unresolved = None, f"`{executable}`"
			elif executable in _DIRECTORY_KEYWORD_PREFIXES and any(
				arg == "cd" or arg in _UNMODELLED_DIRECTORY_COMMANDS for arg in args
			):
				directory, unresolved = None, f"a directory change after `{executable}`"
			elif executable == "export" and any(arg.split("=", 1)[0] == "GIT_DIR" for arg in args):
				directory, unresolved = None, "`export GIT_DIR`"
			elif executable == "cd" and not (
				_is_sequential_separator(separator_before) and _is_sequential_separator(separator_after)
			):
				# `cd x || …`, `cd x & …`, `… | cd x`: the shell may not run the
				# cd, or runs it in a subshell, so later commands' directory is
				# unknown.
				joined_by = separator_after if not _is_sequential_separator(separator_after) else separator_before
				directory, unresolved = None, f"a `cd` joined by `{joined_by.strip()}`"
				continue
			elif executable == "cd":
				destination, cd_reason = _cd_destination(args, directory)
				if destination is None:
					directory, unresolved = None, f"`cd` to {cd_reason}"
				else:
					directory = destination
					# A `cd` to an existing directory succeeds, so it does not
					# make the rest of its `&&` chain conditional by itself.
					list_may_fail = may_fail_before_segment
					if separator_before.replace("\n", "") == "&&" and may_fail_before_segment:
						conditional_cd = True
				continue
		if executable != "git" and not executable.endswith("/git"):
			continue
		targets.extend(_git_invocation_targets(args, assignments, directory, unresolved, session_cwd))
	return targets


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
	print(json.dumps({"systemMessage": f"merged-PR guard skipped: {reason}"}))


def _request_confirmation(reason: str, prompt_reason: str | None = None) -> None:
	"""Route the call through the harness permission prompt (CLAUDE.md §21.C).

	Used when the guard cannot prove the branch is safe: the API is
	unreachable and git history is inconclusive, or ancestry cannot be
	verified for a remote-only push. The human confirms the PR is still open;
	a denial sends the reason back to Claude.
	"""
	print(json.dumps(_confirmation_payload(reason, prompt_reason)))


def _confirmation_payload(reason: str, prompt_reason: str | None = None) -> dict:
	"""The hook output `_request_confirmation` prints, as a dict."""
	# The prompt is read by a human: `reason` carries the full transport
	# error for the log, `prompt_reason` a one-paragraph version for the prompt.
	short_reason = prompt_reason or reason
	return {
		"systemMessage": f"merged-PR guard needs confirmation: {reason}",
		"hookSpecificOutput": {
			"hookEventName": "PreToolUse",
			"permissionDecision": "ask",
			"permissionDecisionReason": (
				f"merged-PR guard (CLAUDE.md §21): {short_reason} Allow only if the "
				f"pull request for this branch is still open. If it has merged, "
				f"deny — the branch must be rebuilt from the default branch and "
				f"a new PR opened."
			),
		},
	}


def _request_api_write_confirmation() -> None:
	"""Restore the harness prompt for a non-canonical allowlisted API write."""
	print(
		json.dumps(
			{
				"hookSpecificOutput": {
					"hookEventName": "PreToolUse",
					"permissionDecision": "ask",
					"permissionDecisionReason": (
						"Non-canonical API curl options can override the allowlisted "
						"HTTP method or destination."
					),
				}
			}
		)
	)


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
	code, message, kind, reason, prompt_reason = _unreachable_decision(
		api_failure, tip, branch, base, cwd, reaches_remote, target_slug
	)
	if kind == "ask":
		_request_confirmation(reason, prompt_reason)
	elif kind == "warn":
		_warn(reason)
	return code, message


def _unreachable_decision(
	api_failure: str,
	tip: str,
	branch: str,
	base: str,
	cwd: str,
	reaches_remote: bool,
	target_slug: str = "",
) -> tuple[int, str, str, str, str]:
	"""`_unreachable_outcome` without the printing.

	Returns (exit code, stderr message, kind, reason, prompt reason), where
	kind is "block" (exit 2), "ask", or "warn", so a caller judging several
	targets can merge them into one hook result.
	"""
	verdict, detail = (
		git_history_verdict(tip, branch, base, cwd)
		if tip
		else (VERDICT_UNAVAILABLE, f"no local checkout of {target_slug or 'the target repository'} to inspect")
	)
	if verdict == VERDICT_STRANDED:
		return 2, _history_block_message(branch, base, detail, api_failure), "block", "", ""
	reason = (
		f"could not reach GitHub to check PR status for `{branch}` ({api_failure}); "
		f"git history is {verdict}: {detail}."
	)
	brief_failure = api_failure.strip().splitlines()[0][:100] if api_failure.strip() else "unknown error"
	prompt_reason = (
		f"could not reach GitHub to check PR status for `{branch}` ({brief_failure}...); "
		f"git history is {verdict}: {detail}."
	)
	return 0, "", "ask" if reaches_remote else "warn", reason, prompt_reason


def _evaluate_bash(payload: dict) -> tuple[int, str]:
	tool_input = payload.get("tool_input")
	command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
	if not isinstance(command, str) or not command.strip():
		return 0, ""
	guarded_git_subcommands = git_subcommands(command) & GUARDED_SUBCOMMANDS
	if _api_write_requires_confirmation(command):
		if guarded_git_subcommands:
			return 2, (
				"BLOCKED: run the non-canonical API write and git commit/push as "
				"separate Bash calls so both permission guards can evaluate them."
			)
		_request_api_write_confirmation()
		return 0, ""

	if _guard_disabled():
		return 0, ""
	if not guarded_git_subcommands:
		return 0, ""

	session_cwd = _payload_cwd(payload)

	# Each guarded git call is judged in the repository it runs in and, for a
	# push, on the branch it writes to (issue #5144), not on the session
	# checkout's branch. One merged hook result is printed at the end.
	blocks: list[str] = []
	asks: list[tuple[str, str]] = []
	notices: list[str] = []
	pull_request_memo: dict[tuple[str, str], tuple[list[dict] | LookupUnavailable, bool]] = {}
	judged: set[GuardTarget] = set()
	for target in guard_targets(command, session_cwd):
		if target in judged:
			continue
		judged.add(target)
		if target.fallback_reason:
			notices.append(
				f"merged-PR guard: could not resolve where `git {target.subcommand}` runs "
				f"({target.fallback_reason}); judged the session checkout `{target.cwd}` instead."
			)
		code, message, kind, reason, prompt_reason = _judge_guard_target(
			target, session_cwd, pull_request_memo
		)
		if code == 2:
			blocks.append(message)
			continue
		if kind == "warn":
			notices.append(f"merged-PR guard skipped: {reason}")
		if target.bulk_reason:
			# The checked-out branch passed; the other branches the push
			# writes are not listed, so a human confirms none of them is
			# stranded (issue #5144 review). No API calls, and not skipped on
			# the default branch.
			bulk_ask_reason = (
				f"{target.bulk_reason} in `{target.cwd}`, and the guard judges only the checked-out "
				"branch, so it cannot check the others. Confirm that none of them belongs to a "
				"merged pull request with no open one."
			)
			if kind == "ask":
				# One confirmation reason per push: fold the bulk reason into
				# the judge's ask instead of adding a second entry.
				asks.append((f"{reason} {bulk_ask_reason}", f"{prompt_reason or reason} {bulk_ask_reason}"))
			else:
				asks.append((bulk_ask_reason, bulk_ask_reason))
		elif kind == "ask":
			asks.append((reason, prompt_reason))

	if blocks:
		# Keep every notice (fallback and skip warnings) beside the block, so a
		# multi-target command still reports the targets it could not judge.
		return 2, "\n\n".join(blocks + notices)
	if asks:
		hook_output = _confirmation_payload(asks[0][0], asks[0][1])
		extra_asks = asks[1:]
		if extra_asks:
			hook_output["systemMessage"] += "\n" + "\n".join(
				f"merged-PR guard needs confirmation: {extra_ask_reason}" for extra_ask_reason, _prompt in extra_asks
			)
			hook_output["hookSpecificOutput"]["permissionDecisionReason"] += " " + " ".join(
				extra_prompt_reason or extra_ask_reason for extra_ask_reason, extra_prompt_reason in extra_asks
			)
		if notices:
			hook_output["systemMessage"] += "\n" + "\n".join(notices)
		print(json.dumps(hook_output))
	elif notices:
		print(json.dumps({"systemMessage": "\n".join(notices)}))
	return 0, ""


def _judge_guard_target(
	target: GuardTarget,
	session_cwd: str,
	pull_request_memo: dict[tuple[str, str], tuple[list[dict] | LookupUnavailable, bool]],
) -> tuple[int, str, str, str, str]:
	"""Apply the §21 rule to one target without printing.

	Returns (exit code, stderr message, kind, reason, prompt reason) like
	`_unreachable_decision`; kind is "" for a plain allow or block.

	Batching contract (CLAUDE.md §15, §21.D): `pull_request_memo` holds the PR
	list (or the lookup failure) per `(slug, branch)` for the current hook
	call, with whether it came from a live call, so a command that judges the
	same pair twice issues one API call.
	A fresh cache entry costs no call; a cache-derived block is re-verified
	with one live call, whose result (or failure) replaces the memo entry.
	"""
	cwd = target.cwd
	branch = target.branch or current_branch(cwd)
	if not branch:
		# Detached HEAD, or not a git repo — nothing branch-shaped to check.
		return 0, "", "", "", ""
	base = default_branch(cwd)
	if base and branch == base:
		# Writing to the default branch is not the stranded-work scenario.
		return 0, "", "", "", ""

	slug = repo_slug(cwd)
	if not slug:
		return 0, "", "warn", f"could not derive <owner>/<repo> from the git remote (branch `{branch}`)", ""

	memo_key = (slug, branch)
	if memo_key in pull_request_memo:
		memo_entry, live = pull_request_memo[memo_key]
	else:
		cached = _read_cache(slug, branch)
		live = cached is None
		if cached is not None:
			memo_entry = cached
		else:
			try:
				memo_entry = query_pull_requests(slug, branch, cwd)
			except LookupUnavailable as exc:
				memo_entry = exc
			else:
				_write_cache(slug, branch, memo_entry)
		pull_request_memo[memo_key] = (memo_entry, live)
	if isinstance(memo_entry, LookupUnavailable):
		return _unreachable_decision(str(memo_entry), target.tip, branch, base, cwd, target.reaches_remote)

	offender = blocking_pull_request(memo_entry, cwd, base, target.tip)

	# Re-verify a block against live data. Cheap allows may come from cache;
	# blocks may not, so that opening a new PR clears the guard immediately
	# rather than after the TTL expires.
	if offender is not None and not live:
		try:
			pull_requests = query_pull_requests(slug, branch, cwd)
		except LookupUnavailable as exc:
			# Remember the failed live call, so a later target for the same
			# pair does not call GitHub again (§21.D budget).
			reverify_failure = LookupUnavailable(f"could not re-verify: {exc}")
			pull_request_memo[memo_key] = (reverify_failure, True)
			return _unreachable_decision(
				str(reverify_failure), target.tip, branch, base, cwd, target.reaches_remote
			)
		_write_cache(slug, branch, pull_requests)
		pull_request_memo[memo_key] = (pull_requests, True)
		offender = blocking_pull_request(pull_requests, cwd, base, target.tip)

	if offender is None:
		return 0, "", "", "", ""
	message = _block_message(offender, branch, base, "HEAD" if target.tip == "HEAD" else f"`{target.tip}`")
	if cwd != session_cwd:
		message += f"\n\nJudged in: {cwd} (where this `git {target.subcommand}` runs)."
	return 2, message, "", "", ""


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


def evaluate(payload: dict) -> tuple[int, str]:
	"""Core decision. Returns (exit_code, message_for_stderr)."""
	tool_name = payload.get("tool_name")
	if tool_name == "Bash":
		return _evaluate_bash(payload)
	if tool_name in MCP_PUSH_TOOLS:
		return _evaluate_mcp_push(payload)
	return 0, ""


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
	except Exception as exc:  # noqa: BLE001 - the guard must never break the session
		_warn(f"internal error ({exc})")
		return 0

	if message:
		print(message, file=sys.stderr)
	return code


if __name__ == "__main__":
	sys.exit(main())

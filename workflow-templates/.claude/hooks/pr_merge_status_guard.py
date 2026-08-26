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

Fail-open by design (CLAUDE.md §21): any inability to answer the question —
no `gh`, expired token, network failure, unparseable payload, not a git repo —
allows the command and emits a warning. A guard that hard-blocks every commit
whenever a token lapses would be worse than the bug it prevents.

PR state is read over REST (`gh api repos/<slug>/pulls`) rather than
`gh pr list`, because the latter is GraphQL-backed and Claude Code Web's agent
proxy serves only a pinned set of GraphQL operations — the guard would fail open
on every commit in precisely the sessions it exists to protect. `gh pr list`
remains as a transport fallback.

Exit codes (Claude Code hook protocol):
  0 — allow the command. A warning may be emitted via `systemMessage`.
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


# Guarded git subcommands. `push` is included alongside `commit` because
# amend/rebase flows reach the remote without issuing a fresh `git commit`.
GUARDED_SUBCOMMANDS = frozenset({"commit", "push"})

# git global options that consume a following argument when not given as
# `--opt=value`. Needed so `git -C /repo commit` resolves to `commit` rather
# than to the path.
GIT_GLOBAL_OPTS_WITH_VALUE = frozenset(
	{"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path"}
)

# Shell punctuation we treat as command separators when tokenizing a Bash line.
_SHELL_PUNCTUATION_CHARS = ";&|\n<>"

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
	code, out, _ = _run(["git", "ls-remote", "--symref", "origin", "HEAD"], cwd, _GIT_TIMEOUT_SECONDS)
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


def is_ancestor(sha: str, cwd: str) -> bool:
	"""True when `sha` is an ancestor of (or equal to) HEAD.

	A commit absent from the local object database cannot be an ancestor of
	HEAD, so `git merge-base` erroring out is a legitimate False rather than an
	unknown — no fetch is attempted.
	"""
	if not sha:
		return False
	code, _, _ = _run(
		["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd, _GIT_TIMEOUT_SECONDS
	)
	return code == 0


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


def blocking_pull_request(pull_requests: list[dict], cwd: str) -> dict | None:
	"""Apply the three-condition detection rule; return the offending PR or None."""
	merged = [pr for pr in pull_requests if pr.get("mergedAt")]
	if not merged:
		return None
	if any(str(pr.get("state", "")).upper() == "OPEN" for pr in pull_requests):
		return None
	for pr in merged:
		if is_ancestor(str(pr.get("headRefOid") or ""), cwd):
			return pr
	return None


def _block_message(pr: dict, branch: str, base: str) -> str:
	number = pr.get("number")
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
		f"BLOCKED by the merged-PR guard (CLAUDE.md §21).\n"
		f"\n"
		f"Branch `{branch}` already had PR #{number} merged at "
		f"{pr.get('mergedAt')}, no open PR now carries this branch, and HEAD "
		f"still contains that merged history. Committing or pushing here would "
		f"strand the work on a dead branch.\n"
		f"\n"
		f"  merged PR: {pr.get('url')}\n"
		f"  title:     {pr.get('title')}\n"
		f"\n"
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


def _warn(reason: str) -> None:
	"""Emit a non-blocking warning to the user and allow the command."""
	print(json.dumps({"systemMessage": f"merged-PR guard skipped: {reason}"}))


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


def evaluate(payload: dict) -> tuple[int, str]:
	"""Core decision. Returns (exit_code, message_for_stderr)."""
	if payload.get("tool_name") != "Bash":
		return 0, ""

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

	if os.environ.get("CLAUDE_PR_MERGE_GUARD", "").strip().lower() == "off":
		return 0, ""
	if not guarded_git_subcommands:
		return 0, ""

	cwd = payload.get("cwd") or os.getcwd()
	if not isinstance(cwd, str) or not os.path.isdir(cwd):
		cwd = os.getcwd()

	branch = current_branch(cwd)
	if not branch:
		# Detached HEAD, or not a git repo — nothing branch-shaped to check.
		return 0, ""
	base = default_branch(cwd)
	if base and branch == base:
		# Committing on the default branch is not the stranded-work scenario.
		return 0, ""

	slug = repo_slug(cwd)
	if not slug:
		_warn(f"could not derive <owner>/<repo> from the git remote (branch `{branch}`)")
		return 0, ""

	cached = _read_cache(slug, branch)
	try:
		pull_requests = cached if cached is not None else query_pull_requests(slug, branch, cwd)
	except LookupUnavailable as exc:
		_warn(f"could not reach GitHub to check PR status for `{branch}` ({exc})")
		return 0, ""

	offender = blocking_pull_request(pull_requests, cwd)

	# Re-verify a block against live data. Cheap allows may come from cache;
	# blocks may not, so that opening a new PR clears the guard immediately
	# rather than after the TTL expires.
	if offender is not None and cached is not None:
		try:
			pull_requests = query_pull_requests(slug, branch, cwd)
		except LookupUnavailable as exc:
			_warn(f"could not re-verify PR status for `{branch}` ({exc})")
			return 0, ""
		_write_cache(slug, branch, pull_requests)
		offender = blocking_pull_request(pull_requests, cwd)
	elif cached is None:
		_write_cache(slug, branch, pull_requests)

	if offender is None:
		return 0, ""
	return 2, _block_message(offender, branch, base)


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

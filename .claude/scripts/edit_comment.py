#!/usr/bin/env python3
"""Edit one issue or PR comment in place with literal find-and-replace pairs.

`/implement-plan-claude` keeps one progress comment per issue up to date
(`<!-- ai:claude-issue-progress:v1 -->`, Issue Mode). `mcp__github__update_issue_comment`
replaces the whole body, so stage sessions fetched the body with
`gh api ... > $S/body.md`, rewrote it with a `python3` heredoc, and PATCHed it
back. Those multi-part commands stopped unattended sessions at permission
prompts. This helper does the read, the replacement, and the write in one
allowlisted command.

Usage:

  edit_comment.py --repo OWNER/REPO --comment-id ID --replacements FILE [--dry-run]
  edit_comment.py --repo OWNER/REPO --comment-id ID --body-file FILE [--dry-run]

`--replacements` is a JSON file holding a list of `{"old": "...", "new": "..."}`
objects (write it with the Write tool, no heredoc). Every `old` must occur in
the current body exactly once; otherwise nothing is written. Pairs apply in
order, each to the result of the previous one. `--body-file` replaces the
whole body with the file's text instead. `--dry-run` prints the new body and
writes nothing.

Both files are read only from a Claude Code session scratchpad
(`<temp dir>/claude-<…>/<project>/<session>/scratchpad/`, the temp dir being
`tempfile.gettempdir()` or `/tmp`), and only when the path resolves, after
symlinks, to a regular file with a single hard link and at most
`MAX_INPUT_FILE_BYTES` bytes (issue #5452). The helper is allowlisted, so
without this check one command could publish any local file, such as a
credential file, as a comment with no prompt. Any other path exits 1 before
the comment is read, and its content is never printed.

API calls (CLAUDE.md §15), REST only: one read of the comment and, unless
`--dry-run` or the body is unchanged, one PATCH.

Prints one JSON line: `updated`, `comment_id`, `updated_at`, `replaced`, and
`body` with `--dry-run`. Exit 0 on success (including "unchanged"), 1 on an
invalid argument or a replacement that does not match exactly once, 2 when a
call failed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_CHECKER_PATH = Path(__file__).resolve().with_name("check_in_status.py")
_checker_spec = importlib.util.spec_from_file_location("check_in_status", _CHECKER_PATH)
check_in_status = importlib.util.module_from_spec(_checker_spec)
_checker_spec.loader.exec_module(check_in_status)

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# GitHub caps issue comment bodies at 65,536 characters.
MAX_BODY_CHARS = 65536
# Claude Code keeps each session's scratchpad at
# <temp dir>/claude-<uid>/<project>/<session>/scratchpad/.
SCRATCHPAD_DIR_NAME = "scratchpad"
SESSION_TEMP_DIR_PREFIX = "claude-"
# Input files larger than this are rejected before they are read, so a huge
# file never reaches memory. A body over MAX_BODY_CHARS is rejected later
# anyway; 16x leaves room for JSON escaping in a --replacements file.
MAX_INPUT_FILE_BYTES = 16 * MAX_BODY_CHARS


def _temp_roots() -> list[Path]:
	"""Resolved temp directories a session scratchpad can live under."""
	roots: list[Path] = []
	for candidate in (tempfile.gettempdir(), "/tmp"):
		try:
			resolved = Path(candidate).resolve(strict=True)
		except (OSError, RuntimeError):
			continue
		if resolved not in roots:
			roots.append(resolved)
	return roots


def is_scratchpad_path(resolved: Path, roots: list[Path]) -> bool:
	"""True when `resolved` is a file below `<root>/claude-<…>/<project>/<session>/scratchpad/`.

	The scratchpad must be the fourth component below the root, exactly where
	Claude Code puts it; a shallower `claude-*/scratchpad/` or a `scratchpad`
	directory at any other depth does not count.
	"""
	for root in roots:
		try:
			parts = resolved.relative_to(root).parts
		except ValueError:
			continue
		if len(parts) >= 5 and parts[0].startswith(SESSION_TEMP_DIR_PREFIX) and parts[3] == SCRATCHPAD_DIR_NAME:
			return True
	return False


def read_input_file(flag: str, path: str) -> str:
	"""Read a `--replacements` / `--body-file` file from the session scratchpad only.

	Raises ValueError, without reading the file, when the path does not
	resolve to a regular, singly-linked file inside a session scratchpad, or
	when the file is larger than MAX_INPUT_FILE_BYTES.
	"""
	try:
		resolved = Path(path).resolve(strict=True)
	except (OSError, RuntimeError) as exc:
		raise ValueError(f"{flag} {path}: {exc}") from exc
	if not is_scratchpad_path(resolved, _temp_roots()):
		raise ValueError(
			f"{flag} {path}: only files in the Claude Code session scratchpad (<temp dir>/claude-*/<project>/<session>/scratchpad/) are read; "
			"write the file there with the Write tool, or rewrite the comment with mcp__github__update_issue_comment"
		)
	# O_NONBLOCK keeps a FIFO from blocking the open; fstat rejects it below.
	try:
		fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
	except OSError as exc:
		raise ValueError(f"{flag} {path}: {exc}") from exc
	try:
		info = os.fstat(fd)
		if not stat.S_ISREG(info.st_mode):
			raise ValueError(f"{flag} {path}: not a regular file")
		if info.st_nlink != 1:
			raise ValueError(f"{flag} {path}: has {info.st_nlink} hard links; only a file with one link is read")
		if info.st_size > MAX_INPUT_FILE_BYTES:
			raise ValueError(f"{flag} {path}: {info.st_size} bytes; files over {MAX_INPUT_FILE_BYTES} bytes are not read")
		with os.fdopen(fd, encoding="utf-8") as handle:
			fd = -1
			# Bounded read: a file that grew after fstat is still never read in full.
			# A character takes at least one byte, so more characters than the
			# byte limit means more bytes than the limit.
			text = handle.read(MAX_INPUT_FILE_BYTES + 1)
		if len(text) > MAX_INPUT_FILE_BYTES:
			raise ValueError(f"{flag} {path}: larger than {MAX_INPUT_FILE_BYTES} bytes; not read")
		return text
	except (OSError, UnicodeDecodeError) as exc:
		raise ValueError(f"{flag} {path}: {exc}") from exc
	finally:
		if fd >= 0:
			os.close(fd)


def load_replacements(path: str) -> list[tuple[str, str]]:
	"""Read and validate the replacement pairs; raise ValueError when malformed."""
	text = read_input_file("--replacements", path)
	try:
		data = json.loads(text)
	except ValueError as exc:
		raise ValueError(f"--replacements {path}: {exc}") from exc
	if not isinstance(data, list) or not data:
		raise ValueError("--replacements must be a non-empty JSON list")
	pairs: list[tuple[str, str]] = []
	for index, item in enumerate(data):
		if not isinstance(item, dict) or set(item) != {"old", "new"}:
			raise ValueError(f"replacement {index} must be an object with exactly `old` and `new`")
		old, new = item["old"], item["new"]
		if not isinstance(old, str) or not isinstance(new, str) or not old:
			raise ValueError(f"replacement {index}: `old` must be a non-empty string and `new` a string")
		pairs.append((old, new))
	return pairs


def apply_replacements(body: str, pairs: list[tuple[str, str]]) -> str:
	"""Apply each pair once, in order; raise ValueError unless each `old` occurs exactly once."""
	for index, (old, new) in enumerate(pairs):
		count = body.count(old)
		if count != 1:
			raise ValueError(f"replacement {index}: `old` occurs {count} times in the comment, expected exactly 1: {old[:80]!r}")
		body = body.replace(old, new, 1)
	return body


def _patch_comment(repo: str, comment_id: int, body: str) -> dict:
	payload_file = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
	payload_path = payload_file.name
	try:
		with payload_file:
			json.dump({"body": body}, payload_file)
		proc = subprocess.run(
			["gh", "api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "--input", payload_path],
			capture_output=True,
			text=True,
			timeout=60,
		)
	except (OSError, TypeError, ValueError, subprocess.TimeoutExpired) as exc:
		raise check_in_status.ReadError(f"PATCH of comment {comment_id} failed: {exc}") from exc
	finally:
		Path(payload_path).unlink(missing_ok=True)
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout).strip().splitlines()
		raise check_in_status.ReadError(f"PATCH of comment {comment_id} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	try:
		updated = json.loads(proc.stdout)
	except ValueError:
		updated = {}
	return updated if isinstance(updated, dict) else {}


def edit_comment(repo: str, comment_id: int, pairs: list[tuple[str, str]] | None, new_body: str | None, dry_run: bool) -> dict:
	"""Read the comment, compute its new body, and write it unless unchanged or dry-run."""
	if not REPO_RE.fullmatch(repo or ""):
		raise ValueError("--repo must be OWNER/REPO")
	if comment_id <= 0:
		raise ValueError("--comment-id must be a positive integer")
	comment = check_in_status.gh_api(f"repos/{repo}/issues/comments/{comment_id}")
	current = comment.get("body") or ""
	if new_body is None:
		new_body = apply_replacements(current, pairs or [])
		replaced = len(pairs or [])
	else:
		replaced = 0
	if len(new_body) > MAX_BODY_CHARS:
		raise ValueError(f"the new body has {len(new_body)} characters; GitHub allows {MAX_BODY_CHARS}")
	result: dict[str, object] = {"comment_id": comment_id, "replaced": replaced}
	if dry_run:
		result.update({"updated": False, "body": new_body})
		return result
	if new_body == current:
		result.update({"updated": False, "updated_at": comment.get("updated_at"), "reason": "body unchanged"})
		return result
	updated = _patch_comment(repo, comment_id, new_body)
	result.update({"updated": True, "updated_at": updated.get("updated_at"), "html_url": updated.get("html_url")})
	return result


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--repo", required=True)
	parser.add_argument("--comment-id", type=int, required=True)
	source = parser.add_mutually_exclusive_group(required=True)
	source.add_argument("--replacements")
	source.add_argument("--body-file")
	parser.add_argument("--dry-run", action="store_true")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		pairs = load_replacements(args.replacements) if args.replacements else None
		new_body = None
		if args.body_file:
			new_body = read_input_file("--body-file", args.body_file)
		result = edit_comment(args.repo, args.comment_id, pairs, new_body, args.dry_run)
	except ValueError as exc:
		print(json.dumps({"updated": False, "error": str(exc)}))
		return 1
	except (check_in_status.ReadError, KeyError, TypeError) as exc:
		print(json.dumps({"updated": False, "error": str(exc)}))
		return 2
	print(json.dumps(result))
	return 0


if __name__ == "__main__":
	sys.exit(main())

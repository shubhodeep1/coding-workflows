#!/usr/bin/env python3
"""files_touched scope-enforcement guard for the AI implement pipeline.

Invoked by the two `files_touched` scope guards in
`.github/workflows/implement.yml` (the preflight guard and the commit-time
guard). It mirrors the destructive-commit guard's design: given the staged
change set and the issue body, decide whether every staged path falls inside
the issue's declared `files_touched` allowlist, and report the out-of-scope
paths so the workflow can block the commit and alert.

Parsing reuses the exact `files_touched:` block semantics from
`scripts/review_reject_verify.sh:extract_files_touched` (the canonical
issue-body allowlist parser) so the implement-time guard and the review-time
scope verifier agree on what an allowlist looks like.

Inputs:
  --issue-body-file PATH  File containing the issue body (the allowlist source).
                          Missing / empty / unreadable is treated as "no
                          allowlist" (skip), never as an error.
  --allowlist-file PATH   Explicit allowlist entries, one per line. When set,
                          this bypasses issue-body parsing and reuses the same
                          matcher for externally-supplied scopes such as
                          `ai:scope:<glob>` labels.
  --staged-file PATH      File with the staged paths, one per line (typically
                          `git diff --cached --name-only --diff-filter=ACMRD`).
                          Reads stdin when omitted.
  --allowlist-out PATH    Optional: write the normalized allowlist (one entry
                          per line) so the caller can surface it in the alert.

Output:
  stdout — the out-of-scope staged paths, one per line (empty when none).

Exit codes (the caller maps these; ANY other code => fail open / skip):
  0   evaluated, every staged path is in scope.
  10  skipped — the issue declares no (or an empty) files_touched allowlist.
  20  one or more staged paths fall outside the allowlist (stdout lists them).

Matching semantics (allowlist entry -> staged path):
  * leading "./" is stripped from both sides; surrounding whitespace trimmed.
  * an entry containing a glob metacharacter (`*`, `?`, `[`) is matched with
    Bash-style globstar semantics for `/`-separated paths (so `frontend/**`
    allows anything under `frontend/`).
  * an entry ending in "/" is a directory prefix (`frontend/` allows
    `frontend/anything`).
  * a bare entry matches an exact path OR anything beneath it as a directory
    (`frontend` allows `frontend` and `frontend/anything`).
  * dependency lockfiles the implement prompt mandates regenerating alongside
    manifest edits are auto-allowed by basename. Compiled build outputs (e.g.
    `*.js` twins, `dist/**`) are deliberately NOT auto-allowed — sweeping those
    in was the incident this guard exists to stop.
"""

from __future__ import annotations

import argparse
import fnmatch
from functools import lru_cache
import json
from pathlib import PurePosixPath
import re
import sys


EXIT_IN_SCOPE = 0
EXIT_SKIP_NO_ALLOWLIST = 10
EXIT_OUT_OF_SCOPE = 20
EXIT_INVALID_GENERATED_ADVISORY = 30

STATUS_IN_SCOPE = "in-scope"
STATUS_SKIP_NO_ALLOWLIST = "skip-no-allowlist"
STATUS_OUT_OF_SCOPE = "out-of-scope"

GENERATED_ADVISORY_HEADER = "**Generated security advisory metadata**"
GENERATED_ADVISORY_SCHEMA = "generated-security-advisory.v1"
TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
TRUSTED_GENERATED_ADVISORY_BOT_LOGINS = frozenset({"github-actions[bot]"})
_GENERATED_ADVISORY_FOOTER_RE = re.compile(
	r"(?:^|\n)---\n"
	r"\*\*Generated security advisory metadata\*\*\n"
	r"- Schema: `generated-security-advisory\.v1`\n"
	r"- Waiver match key: `(sha256:[0-9a-f]{64})`\n"
	r"- Audited commit: `([0-9a-f]{40,64})`\n"
	r"- Cited file: `([^`\n]+)`\n"
	r"files_touched:\n"
	r"  - ([^\n]+)\n?\Z"
)

# Dependency lockfiles the implement prompt (prompts/mode-implement.txt,
# "Dependency / lockfile discipline") instructs the editor to regenerate in the
# same patch as a manifest edit. They are auto-allowed by basename so a routine
# `npm install` / `cargo update` lockfile refresh does not trip the guard even
# when the orchestrator forgot to list the lockfile in files_touched.
# Compiled outputs are intentionally excluded — they are the drift this guard
# blocks.
LOCKFILE_BASENAMES = frozenset(
	{
		"package-lock.json",
		"pnpm-lock.yaml",
		"yarn.lock",
		"Cargo.lock",
		"poetry.lock",
		"uv.lock",
		"go.sum",
		"composer.lock",
	}
)

_GLOB_CHARS = ("*", "?", "[")


def extract_files_touched(text: str) -> list[str] | None:
	"""Parse the first `files_touched:` block from an issue body.

	Ported verbatim from scripts/review_reject_verify.sh:extract_files_touched
	so the implement-time guard and the review-time scope verifier agree on the
	allowlist format. Returns the raw entries (still needing normalization), or
	None when no well-formed block is present.
	"""
	lines = text.splitlines()
	for idx, raw in enumerate(lines):
		stripped = raw.strip()
		if stripped not in {"files_touched:", "- files_touched:"}:
			continue
		base_indent = len(raw) - len(raw.lstrip(" "))
		entries: list[str] = []
		for candidate in lines[idx + 1 :]:
			if not candidate.strip():
				if entries:
					break
				continue
			indent = len(candidate) - len(candidate.lstrip(" "))
			if indent <= base_indent:
				break
			match = re.match(r"^\s*-\s+(.+?)\s*$", candidate)
			if not match:
				break
			entry = match.group(1).strip()
			if entry:
				entries.append(entry)
		return entries or None
	return None


def normalize_path(path: str) -> str:
	"""Trim whitespace and a single leading './' from a path or allowlist entry."""
	value = path.strip()
	while value.startswith("./"):
		value = value[2:]
	return value


def normalize_allowlist(entries: list[str]) -> list[str]:
	"""Normalize + de-duplicate allowlist entries, preserving first-seen order."""
	seen: set[str] = set()
	normalized: list[str] = []
	for raw in entries:
		entry = normalize_path(raw)
		if not entry or entry in seen:
			continue
		seen.add(entry)
		normalized.append(entry)
	return normalized


def is_lockfile(path: str) -> bool:
	"""True when the path's basename is an auto-allowed dependency lockfile."""
	return path.rsplit("/", 1)[-1] in LOCKFILE_BASENAMES


def _strict_repository_file(path: str) -> str:
	"""Return a canonical exact repository path or raise ValueError."""
	value = normalize_path(path)
	if not value or value == "." or value.startswith("/"):
		raise ValueError("cited file must be a non-empty repository-relative path")
	parts = PurePosixPath(value).parts
	if ".." in parts or any(character in value for character in ("*", "?", "[", "]", "`")):
		raise ValueError("cited file must be one exact repository-relative path")
	if any(ord(character) < 32 or ord(character) == 127 for character in value):
		raise ValueError("cited file contains a control character")
	return PurePosixPath(*parts).as_posix()


def parse_generated_advisory(text: str) -> dict[str, str] | None:
	"""Parse the authenticated-shape footer used by generated security tasks.

	The body remains untrusted. Presence of the header opts the issue into a
	fail-closed parser; malformed or duplicated metadata is never treated as a
	normal issue whose permissive files_touched rules can be used instead.
	"""
	if GENERATED_ADVISORY_HEADER not in text:
		return None
	if text.count(GENERATED_ADVISORY_HEADER) != 1:
		raise ValueError("generated security advisory metadata must appear exactly once")
	match = _GENERATED_ADVISORY_FOOTER_RE.search(text.replace("\r\n", "\n"))
	if match is None:
		raise ValueError("generated security advisory metadata footer is malformed")
	waiver_match_key, audited_commit, cited_file_raw, allowlist_file_raw = match.groups()
	cited_file = _strict_repository_file(cited_file_raw)
	allowlist_file = _strict_repository_file(allowlist_file_raw)
	if allowlist_file != cited_file:
		raise ValueError("generated security advisory files_touched path must equal the cited file")
	return {
		"schema": GENERATED_ADVISORY_SCHEMA,
		"waiver_match_key": waiver_match_key,
		"audited_commit": audited_commit,
		"cited_file": cited_file,
	}


def parse_linked_issue_metadata(text: str) -> tuple[str, str, str] | None:
	"""Return one generated-advisory body and author, or None when verified absent."""
	try:
		linked_issue_rows = json.loads(text)
	except (json.JSONDecodeError, TypeError) as exc:
		raise ValueError("linked-issue metadata is malformed") from exc
	if not isinstance(linked_issue_rows, list) or any(not isinstance(row, dict) for row in linked_issue_rows):
		raise ValueError("linked-issue metadata must be an array of objects")
	for linked_issue_row in linked_issue_rows:
		if "_collection_status" in linked_issue_row:
			raise ValueError("linked-issue metadata collection is unresolved")
	linked_advisory_rows = [
		row
		for row in linked_issue_rows
		if isinstance(row.get("body"), str) and GENERATED_ADVISORY_HEADER in row["body"]
	]
	if len(linked_advisory_rows) > 1:
		raise ValueError("multiple generated security advisories are linked to one PR")
	if not linked_advisory_rows:
		return None
	linked_advisory_row = linked_advisory_rows[0]
	return (
		linked_advisory_row["body"],
		str(linked_advisory_row.get("author_association") or ""),
		str(linked_advisory_row.get("author_login") or ""),
	)


def extract_plan_files(text: str) -> list[str]:
	"""Extract concrete paths from a plan's Files-to-change section."""
	paths: list[str] = []
	in_files_section = False
	for raw_line in text.replace("\r\n", "\n").splitlines():
		if re.match(
			r"^\s{0,3}(?:#{1,6}\s*|\d+[.)]\s+)Files\b.*\bchange\b[.:]?\s*$",
			raw_line,
			re.IGNORECASE,
		):
			in_files_section = True
			continue
		if in_files_section and (
			re.match(r"^\s{0,3}#{1,6}\s+\S", raw_line)
			or re.match(r"^\s{0,3}\d+[.)]\s+[^`]+[.:]?\s*$", raw_line)
		):
			break
		if not in_files_section or not re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", raw_line):
			continue
		path_match = re.search(r"`([^`]+)`", raw_line)
		if path_match is None:
			continue
		try:
			candidate = _strict_repository_file(path_match.group(1))
		except ValueError:
			continue
		if candidate not in paths:
			paths.append(candidate)
	return paths


def _path_glob_matches(path: str, pattern: str) -> bool:
	"""True when a normalized path matches a normalized glob pattern."""
	if "/" not in pattern:
		return fnmatch.fnmatchcase(path, pattern)
	path_parts = PurePosixPath(path).parts
	pattern_parts = PurePosixPath(pattern).parts

	@lru_cache(maxsize=None)
	def match_parts(path_idx: int, pattern_idx: int) -> bool:
		while pattern_idx < len(pattern_parts):
			pattern_part = pattern_parts[pattern_idx]
			if pattern_part == "**":
				if pattern_idx + 1 == len(pattern_parts):
					return True
				for next_path_idx in range(path_idx, len(path_parts) + 1):
					if match_parts(next_path_idx, pattern_idx + 1):
						return True
				return False
			if path_idx >= len(path_parts) or not fnmatch.fnmatchcase(path_parts[path_idx], pattern_part):
				return False
			path_idx += 1
			pattern_idx += 1
		return path_idx == len(path_parts)

	return match_parts(0, 0)


def entry_matches(entry: str, path: str) -> bool:
	"""True when a single normalized allowlist entry covers a staged path."""
	if not entry:
		return False
	if any(ch in entry for ch in _GLOB_CHARS):
		return _path_glob_matches(path, entry)
	if entry.endswith("/"):
		return path.startswith(entry)
	return path == entry or path.startswith(entry + "/")


def path_in_scope(path: str, allowlist: list[str], *, auto_allow_lockfiles: bool = True) -> bool:
	"""True when a staged path is auto-allowed or covered by any allowlist entry."""
	if auto_allow_lockfiles and is_lockfile(path):
		return True
	for entry in allowlist:
		if entry_matches(entry, path):
			return True
	return False


def evaluate_allowlist(
	allowlist_entries: list[str] | None,
	staged_paths: list[str],
	*,
	auto_allow_lockfiles: bool = True,
) -> tuple[str, list[str], list[str]]:
	"""Classify staged paths against an explicit allowlist using the shared matcher.

	Returns (status, allowlist, out_of_scope) where status is one of
	STATUS_IN_SCOPE / STATUS_SKIP_NO_ALLOWLIST / STATUS_OUT_OF_SCOPE.
	"""
	allowlist = normalize_allowlist(allowlist_entries or [])
	if not allowlist:
		return STATUS_SKIP_NO_ALLOWLIST, [], []

	out_of_scope: list[str] = []
	for raw in staged_paths:
		path = normalize_path(raw)
		if not path:
			continue
		if not path_in_scope(path, allowlist, auto_allow_lockfiles=auto_allow_lockfiles):
			out_of_scope.append(path)

	if out_of_scope:
		return STATUS_OUT_OF_SCOPE, allowlist, out_of_scope
	return STATUS_IN_SCOPE, allowlist, []


def evaluate(issue_body: str, staged_paths: list[str], *, allowlist_entries: list[str] | None = None) -> tuple[str, list[str], list[str]]:
	"""Classify the staged change set against issue-body or explicit allowlists.

	Returns (status, allowlist, out_of_scope) where status is one of
	STATUS_IN_SCOPE / STATUS_SKIP_NO_ALLOWLIST / STATUS_OUT_OF_SCOPE.
	"""
	if allowlist_entries is not None:
		return evaluate_allowlist(allowlist_entries, staged_paths)
	raw_allowlist = extract_files_touched(issue_body or "")
	return evaluate_allowlist(raw_allowlist or [], staged_paths)


def _read_text_file(path: str) -> str:
	try:
		with open(path, encoding="utf-8") as handle:
			return handle.read()
	except (OSError, UnicodeDecodeError):
		return ""


def _read_staged(path: str | None) -> list[str]:
	if path:
		text = _read_text_file(path)
	else:
		text = sys.stdin.read()
	return [line for line in text.splitlines() if line.strip()]


def _read_allowlist(path: str) -> list[str]:
	return [line for line in _read_text_file(path).splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="files_touched scope-enforcement guard")
	parser.add_argument("--issue-body-file", default="")
	parser.add_argument("--allowlist-file", default="")
	parser.add_argument("--staged-file", default="")
	parser.add_argument("--allowlist-out", default="")
	parser.add_argument("--plan-file", default="")
	parser.add_argument("--linked-issue-metadata-file", default="")
	parser.add_argument("--issue-author-association", default="")
	parser.add_argument("--issue-author-login", default="")
	parser.add_argument(
		"--generated-advisory-mode",
		choices=("auto", "required", "off"),
		default="off",
	)
	args = parser.parse_args(argv)

	issue_body = _read_text_file(args.issue_body_file) if args.issue_body_file else ""
	issue_author_association = args.issue_author_association
	issue_author_login = args.issue_author_login
	if args.linked_issue_metadata_file:
		try:
			linked_advisory = parse_linked_issue_metadata(_read_text_file(args.linked_issue_metadata_file))
		except ValueError as exc:
			print(f"generated security advisory rejected: {exc}", file=sys.stderr)
			return EXIT_INVALID_GENERATED_ADVISORY
		if linked_advisory is None:
			return EXIT_SKIP_NO_ALLOWLIST
		issue_body, issue_author_association, issue_author_login = linked_advisory
	staged_paths = _read_staged(args.staged_file or None)
	allowlist_entries = _read_allowlist(args.allowlist_file) if args.allowlist_file else None

	generated_advisory: dict[str, str] | None = None
	if args.generated_advisory_mode != "off":
		try:
			generated_advisory = parse_generated_advisory(issue_body)
		except ValueError as exc:
			print(f"generated security advisory rejected: {exc}", file=sys.stderr)
			return EXIT_INVALID_GENERATED_ADVISORY
		if args.generated_advisory_mode == "required" and generated_advisory is None:
			print("generated security advisory metadata is required", file=sys.stderr)
			return EXIT_INVALID_GENERATED_ADVISORY
	if generated_advisory is not None:
		issue_author_association = issue_author_association.strip().upper()
		issue_author_login = issue_author_login.strip().lower()
		if issue_author_association not in TRUSTED_AUTHOR_ASSOCIATIONS and not (
			issue_author_association == "NONE"
			and issue_author_login in TRUSTED_GENERATED_ADVISORY_BOT_LOGINS
		):
			print("generated security advisory author association is not trusted", file=sys.stderr)
			return EXIT_INVALID_GENERATED_ADVISORY
		cited_file = generated_advisory["cited_file"]
		if args.plan_file:
			plan_files = extract_plan_files(_read_text_file(args.plan_file))
			if plan_files != [cited_file]:
				print(
					"generated security advisory plan must name only its exact cited file",
					file=sys.stderr,
				)
				return EXIT_INVALID_GENERATED_ADVISORY
		allowlist = [cited_file]
		out_of_scope = [
			normalized_path
			for staged_path in staged_paths
			if (normalized_path := normalize_path(staged_path)) and normalized_path != cited_file
		]
		status = STATUS_OUT_OF_SCOPE if out_of_scope else STATUS_IN_SCOPE
	else:
		status, allowlist, out_of_scope = evaluate(
			issue_body, staged_paths, allowlist_entries=allowlist_entries
		)

	if args.allowlist_out:
		try:
			with open(args.allowlist_out, "w", encoding="utf-8") as handle:
				if allowlist:
					handle.write("\n".join(allowlist) + "\n")
		except OSError:
			# The allowlist dump is advisory (used only to enrich the alert);
			# never fail the guard because it could not be written.
			pass

	if status == STATUS_SKIP_NO_ALLOWLIST:
		return EXIT_SKIP_NO_ALLOWLIST
	if status == STATUS_OUT_OF_SCOPE:
		sys.stdout.write("\n".join(out_of_scope) + "\n")
		return EXIT_OUT_OF_SCOPE
	return EXIT_IN_SCOPE


if __name__ == "__main__":
	raise SystemExit(main())

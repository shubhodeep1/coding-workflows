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
  --emit-automation-grant PATH --issue-meta-file PATH --pipeline-login LOGIN
                          Snapshot trusted-author exact automation paths.
  --check-automation-paths --automation-grant-file PATH --staged-file PATH
                          --allow-workflow-edits VALUE --issue-number NUMBER
                          Verify staged automation paths against the snapshot.

Output:
  stdout — the out-of-scope staged paths, one per line (empty when none).

Exit codes (legacy scope callers fail open on other codes; automation callers
fail closed on any nonzero result):
  0   evaluated, every staged path is in scope.
  10  skipped — the issue declares no (or an empty) files_touched allowlist.
  20  one or more staged paths fall outside the allowlist (stdout lists them).
  30  one or more automation paths lack a grant (stdout lists them).

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
EXIT_AUTOMATION_UNGRANTED = 30

# Keep this prefix set in sync with _rb_fix_scope_is_protected in
# scripts/orchestrate_poll_process.sh. Case variants are denied here too.
AUTOMATION_PATH_PREFIXES = (".github", ".claude", "scripts", "prompts", "workflow-templates")
TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
AUTOMATION_GRANT_SCHEMA = "automation_path_grant.v1"
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$")

STATUS_IN_SCOPE = "in-scope"
STATUS_SKIP_NO_ALLOWLIST = "skip-no-allowlist"
STATUS_OUT_OF_SCOPE = "out-of-scope"

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


def is_automation_path(path: str) -> bool:
	return normalize_path(path).split("/", 1)[0].lower() in AUTOMATION_PATH_PREFIXES


def files_touched_block_state(text: str) -> tuple[str, list[str]]:
	entries = extract_files_touched(text)
	if entries is not None:
		return "ok", entries
	if any(line.strip() in {"files_touched:", "- files_touched:"} for line in text.splitlines()):
		return "malformed", []
	return "missing", []


def build_automation_grant(issue_meta: dict, pipeline_login: str) -> dict:
	"""Snapshot the author's authority and exact automation files in the issue."""
	if not isinstance(issue_meta, dict):
		raise ValueError("invalid issue metadata")
	user = issue_meta.get("user")
	author_login = user.get("login") if isinstance(user, dict) else ""
	author_login = author_login if isinstance(author_login, str) else ""
	author_association = issue_meta.get("author_association")
	author_association = author_association if isinstance(author_association, str) else ""
	record = {"schema_version": AUTOMATION_GRANT_SCHEMA, "issue_number": issue_meta.get("number"),
		"trusted": False, "reason": "grant_error", "author_login": author_login,
		"author_association": author_association, "paths": []}
	if not isinstance(pipeline_login, str) or not LOGIN_RE.fullmatch(pipeline_login):
		record["reason"] = "identity_unavailable"
	elif not LOGIN_RE.fullmatch(author_login):
		record["reason"] = "untrusted_author"
	elif author_login != pipeline_login and author_association.upper() not in TRUSTED_AUTHOR_ASSOCIATIONS:
		record["reason"] = "untrusted_author"
	else:
		body = issue_meta.get("body")
		if not isinstance(body, str):
			body = ""
		state, entries = files_touched_block_state(body)
		if state != "ok":
			record["reason"] = "no_allowlist" if state == "missing" else "malformed_allowlist"
		else:
			record["trusted"] = True
			record["reason"] = "granted"
			record["paths"] = [entry for entry in normalize_allowlist(entries)
			if is_automation_path(entry) and not entry.startswith("/") and not entry.endswith("/")
			and not any(char in entry for char in _GLOB_CHARS)
			and all(segment not in {".", ".."} for segment in entry.split("/"))]
	return record


def check_automation_paths(staged: list[str], grant: object, allow_workflow_edits: str,
	issue_number: str) -> tuple[str, list[str], str]:
	"""Deny each automation path unless a valid issue-bound exact grant covers it."""
	paths = [normalize_path(path) for path in staged if is_automation_path(path)]
	if not paths:
		return STATUS_IN_SCOPE, [], "none"
	if allow_workflow_edits != "true":
		reason = "workflow_edits_disabled"
	elif not isinstance(grant, dict) or grant.get("schema_version") != AUTOMATION_GRANT_SCHEMA \
		or type(grant.get("trusted")) is not bool or not isinstance(grant.get("paths"), list) \
		or not all(isinstance(path, str) and is_automation_path(path) and path == normalize_path(path)
			and not path.startswith("/") and not path.endswith("/")
			and not any(char in path for char in _GLOB_CHARS)
			and all(segment not in {".", ".."} for segment in path.split("/"))
			for path in grant.get("paths", [])):
		reason = "grant_unavailable"
	elif not issue_number.isdecimal() or type(grant.get("issue_number")) is not int \
		or grant["issue_number"] != int(issue_number):
		reason = "grant_issue_mismatch"
	elif not grant["trusted"]:
		reason = grant.get("reason") if grant.get("reason") in {
			"identity_unavailable", "untrusted_author", "no_allowlist", "malformed_allowlist", "grant_error"
		} else "grant_unavailable"
	else:
		denied = [path for path in paths if path not in grant["paths"]]
		return (STATUS_OUT_OF_SCOPE, denied, "path_not_granted") if denied else (STATUS_IN_SCOPE, [], "granted")
	return STATUS_OUT_OF_SCOPE, paths, reason


def is_lockfile(path: str) -> bool:
	"""True when the path's basename is an auto-allowed dependency lockfile."""
	return path.rsplit("/", 1)[-1] in LOCKFILE_BASENAMES


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


def path_in_scope(path: str, allowlist: list[str], *, allow_lockfiles: bool = True) -> bool:
	"""True when a staged path is auto-allowed or covered by any allowlist entry."""
	if allow_lockfiles and is_lockfile(path):
		return True
	for entry in allowlist:
		if entry_matches(entry, path):
			return True
	return False


def evaluate_allowlist(allowlist_entries: list[str] | None, staged_paths: list[str], *, allow_lockfiles: bool = True) -> tuple[str, list[str], list[str]]:
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
		if not path_in_scope(path, allowlist, allow_lockfiles=allow_lockfiles):
			out_of_scope.append(path)

	if out_of_scope:
		return STATUS_OUT_OF_SCOPE, allowlist, out_of_scope
	return STATUS_IN_SCOPE, allowlist, []


def evaluate(issue_body: str, staged_paths: list[str], *, allowlist_entries: list[str] | None = None, allow_lockfiles: bool = True) -> tuple[str, list[str], list[str]]:
	"""Classify the staged change set against issue-body or explicit allowlists.

	Returns (status, allowlist, out_of_scope) where status is one of
	STATUS_IN_SCOPE / STATUS_SKIP_NO_ALLOWLIST / STATUS_OUT_OF_SCOPE.
	"""
	if allowlist_entries is not None:
		return evaluate_allowlist(allowlist_entries, staged_paths, allow_lockfiles=allow_lockfiles)
	raw_allowlist = extract_files_touched(issue_body or "")
	return evaluate_allowlist(raw_allowlist or [], staged_paths, allow_lockfiles=allow_lockfiles)


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
	parser.add_argument("--strict-allowlist", action="store_true", help="Do not auto-allow lockfiles outside the explicit scope")
	parser.add_argument("--staged-file", default="")
	parser.add_argument("--allowlist-out", default="")
	parser.add_argument("--emit-automation-grant", default="")
	parser.add_argument("--issue-meta-file", default="")
	parser.add_argument("--pipeline-login", default="")
	parser.add_argument("--check-automation-paths", action="store_true")
	parser.add_argument("--automation-grant-file", default="")
	parser.add_argument("--allow-workflow-edits", default="false")
	parser.add_argument("--issue-number", default="")
	args = parser.parse_args(argv)
	if args.emit_automation_grant:
		try:
			metadata = json.loads(_read_text_file(args.issue_meta_file))
			record = build_automation_grant(metadata, args.pipeline_login)
		except (ValueError, TypeError, KeyError):
			record = {"schema_version": AUTOMATION_GRANT_SCHEMA, "issue_number": None,
				"trusted": False, "reason": "grant_error", "author_login": "", "author_association": "", "paths": []}
		try:
			with open(args.emit_automation_grant, "w", encoding="utf-8") as handle:
				json.dump(record, handle)
				handle.write("\n")
		except OSError:
			return 1
		return 0
	if args.check_automation_paths:
		try:
			grant = json.loads(_read_text_file(args.automation_grant_file))
		except (ValueError, TypeError):
			grant = None
		status, denied, reason = check_automation_paths(_read_staged(args.staged_file or None),
			grant, args.allow_workflow_edits, args.issue_number)
		if status == STATUS_OUT_OF_SCOPE:
			print(f"reason={reason}", file=sys.stderr)
			sys.stdout.write("\n".join(denied) + "\n")
			return EXIT_AUTOMATION_UNGRANTED
		return EXIT_IN_SCOPE

	issue_body = _read_text_file(args.issue_body_file) if args.issue_body_file else ""
	staged_paths = _read_staged(args.staged_file or None)
	allowlist_entries = _read_allowlist(args.allowlist_file) if args.allowlist_file else None

	status, allowlist, out_of_scope = evaluate(issue_body, staged_paths, allowlist_entries=allowlist_entries, allow_lockfiles=not args.strict_allowlist)

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

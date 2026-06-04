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
    fnmatch (so `frontend/**` allows anything under `frontend/`).
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
import re
import sys


EXIT_IN_SCOPE = 0
EXIT_SKIP_NO_ALLOWLIST = 10
EXIT_OUT_OF_SCOPE = 20

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


def is_lockfile(path: str) -> bool:
	"""True when the path's basename is an auto-allowed dependency lockfile."""
	return path.rsplit("/", 1)[-1] in LOCKFILE_BASENAMES


def entry_matches(entry: str, path: str) -> bool:
	"""True when a single normalized allowlist entry covers a staged path."""
	if not entry:
		return False
	if any(ch in entry for ch in _GLOB_CHARS):
		# fnmatchcase keeps matching deterministic across runner OSes.
		return fnmatch.fnmatchcase(path, entry)
	if entry.endswith("/"):
		return path.startswith(entry)
	return path == entry or path.startswith(entry + "/")


def path_in_scope(path: str, allowlist: list[str]) -> bool:
	"""True when a staged path is auto-allowed or covered by any allowlist entry."""
	if is_lockfile(path):
		return True
	for entry in allowlist:
		if entry_matches(entry, path):
			return True
	return False


def evaluate(issue_body: str, staged_paths: list[str]) -> tuple[str, list[str], list[str]]:
	"""Classify the staged change set against the issue's files_touched allowlist.

	Returns (status, allowlist, out_of_scope) where status is one of
	STATUS_IN_SCOPE / STATUS_SKIP_NO_ALLOWLIST / STATUS_OUT_OF_SCOPE.
	"""
	raw_allowlist = extract_files_touched(issue_body or "")
	allowlist = normalize_allowlist(raw_allowlist or [])
	if not allowlist:
		return STATUS_SKIP_NO_ALLOWLIST, [], []

	out_of_scope: list[str] = []
	for raw in staged_paths:
		path = normalize_path(raw)
		if not path:
			continue
		if not path_in_scope(path, allowlist):
			out_of_scope.append(path)

	if out_of_scope:
		return STATUS_OUT_OF_SCOPE, allowlist, out_of_scope
	return STATUS_IN_SCOPE, allowlist, []


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


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="files_touched scope-enforcement guard")
	parser.add_argument("--issue-body-file", default="")
	parser.add_argument("--staged-file", default="")
	parser.add_argument("--allowlist-out", default="")
	args = parser.parse_args(argv)

	issue_body = _read_text_file(args.issue_body_file) if args.issue_body_file else ""
	staged_paths = _read_staged(args.staged_file or None)

	status, allowlist, out_of_scope = evaluate(issue_body, staged_paths)

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

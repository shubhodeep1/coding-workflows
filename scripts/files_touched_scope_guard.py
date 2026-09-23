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
  --linked-issue-metadata-file PATH
                          Batched linked-issue body/authorship JSON.
  --linked-issue-metadata-sha256 HEX
                          Collector digest required with linked metadata.

Output:
  stdout — the out-of-scope staged paths, one per line (empty when none).

Exit codes (the caller maps these; generated-advisory callers fail closed):
  0   evaluated, every staged path is in scope.
  10  skipped — the issue declares no (or an empty) files_touched allowlist.
  20  one or more staged paths fall outside the allowlist (stdout lists them).
  30  generated-advisory metadata, authorship, or collector digest is invalid.

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
import base64
import fnmatch
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


EXIT_IN_SCOPE = 0
EXIT_SKIP_NO_ALLOWLIST = 10
EXIT_OUT_OF_SCOPE = 20
EXIT_INVALID_GENERATED_ADVISORY = 30
REVIEW_FIX_SCHEMA = "review_fix_authorization.v1"
MAX_REVIEW_FIX_INPUT_BYTES = 2_000_000
MAX_REVIEW_FIX_MANIFEST_BYTES = 32_000
MAX_REVIEW_FIX_TARGETS = 100
PROTECTED_REVIEW_FIX_PREFIXES = ("scripts/", "prompts/", ".github/ai/", ".github/workflows/")

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
		path_matches = re.findall(r"`([^`]+)`", raw_line)
		for path_candidate_raw in path_matches:
			try:
				candidate = _strict_repository_file(path_candidate_raw)
			except ValueError:
				candidate = path_candidate_raw
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


def _read_verified_linked_issue_metadata(path: str, expected_sha256: str) -> str:
	"""Read linked-issue metadata only when it matches the collector digest."""
	if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
		raise ValueError("linked-issue metadata collector digest is unavailable")
	try:
		with open(path, "rb") as handle:
			metadata_bytes = handle.read()
	except OSError as exc:
		raise ValueError("linked-issue metadata is unavailable") from exc
	if hashlib.sha256(metadata_bytes).hexdigest() != expected_sha256:
		raise ValueError("linked-issue metadata integrity check failed")
	try:
		return metadata_bytes.decode("utf-8")
	except UnicodeDecodeError as exc:
		raise ValueError("linked-issue metadata is malformed") from exc


def _read_staged(path: str | None) -> list[str]:
	if path:
		text = _read_text_file(path)
	else:
		text = sys.stdin.read()
	return [line for line in text.splitlines() if line.strip()]


def _read_allowlist(path: str) -> list[str]:
	return [line for line in _read_text_file(path).splitlines() if line.strip()]


def _file_digest(path: Path) -> str:
	data = path.read_bytes()
	if len(data) > MAX_REVIEW_FIX_INPUT_BYTES:
		raise ValueError("review-fix input exceeds size bound")
	return hashlib.sha256(data).hexdigest()


def _parse_unified_diff(diff_text: str) -> dict[str, list[tuple[int, int]]]:
	rows: dict[str, list[tuple[int, int]]] = {}
	current_path = ""
	path_rejected = False
	for line in diff_text.splitlines():
		if line.startswith("diff --git "):
			match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
			current_path = ""
			path_rejected = True
			if match is not None and match.group(1) == match.group(2):
				try:
					current_path = _strict_repository_file(match.group(2))
					path_rejected = False
				except ValueError:
					pass
			continue
		if not current_path or path_rejected:
			continue
		if line.startswith(("new file mode ", "deleted file mode ", "old mode ", "new mode ")):
			path_rejected = True
			rows.pop(current_path, None)
			continue
		match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
		if match is None:
			continue
		start = int(match.group(1))
		count = int(match.group(2) or "1")
		if count > 0:
			rows.setdefault(current_path, []).append((start, start + count - 1))
	return rows


def _citations_from_text(text: str) -> list[tuple[str, int]]:
	citations: list[tuple[str, int]] = []
	lines = text.replace("\r\n", "\n").splitlines()
	for index, line in enumerate(lines):
		field_match = re.match(r"^\s*(?:FILE|file)(?:_PATH)?\s*:\s*`?([^`\s]+)`?\s*$", line)
		if field_match is not None and index + 1 < len(lines):
			line_match = re.match(r"^\s*(?:LINE|line)\s*:\s*(\d+)\b", lines[index + 1])
			if line_match is not None:
				try:
					citations.append((_strict_repository_file(field_match.group(1)), int(line_match.group(1))))
				except ValueError:
					pass
		for match in re.finditer(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+):(\d+)\b", line):
			try:
				citations.append((_strict_repository_file(match.group(1)), int(match.group(2))))
			except ValueError:
				pass
	return list(dict.fromkeys(citations))


def _trusted_comment_evidence(path: Path, repository: str) -> str:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError) as exc:
		raise ValueError("review comments are malformed") from exc
	if not isinstance(payload, list):
		raise ValueError("review comments must be an array")
	owner = repository.partition("/")[0].lower()
	trusted_bodies: list[str] = []
	for row in payload:
		if not isinstance(row, dict):
			continue
		author = row.get("author")
		if not isinstance(author, str):
			user = row.get("user")
			author = str(user.get("login") or "") if isinstance(user, dict) else ""
		login = author.lower()
		if login != owner and login not in TRUSTED_GENERATED_ADVISORY_BOT_LOGINS:
			continue
		body = row.get("body")
		if isinstance(body, str):
			trusted_bodies.append(body)
		path_value = row.get("path")
		line_value = row.get("line") or row.get("original_line")
		if isinstance(path_value, str) and isinstance(line_value, int) and not isinstance(line_value, bool):
			trusted_bodies.append(f"file: {path_value}\nline: {line_value}")
	return "\n".join(trusted_bodies)


def _floor_authorized(path: str, line: int, floor_text: str) -> bool:
	for floor_line in floor_text.splitlines():
		match = re.match(r"^([^\t:]+(?:/[^\t:]+)*):(\d+)\t([^\t]+)", floor_line)
		if match is None or match.group(1) != path:
			continue
		if abs(int(match.group(2)) - line) <= 3 and "FLOOR_MULTI_REVIEWER" in match.group(3):
			return True
	return False


def _anchors_for_ranges(path: Path, ranges: list[tuple[int, int]]) -> list[str]:
	return _anchors_for_data(path.read_bytes(), ranges)


def _anchors_for_data(data: bytes, ranges: list[tuple[int, int]]) -> list[str]:
	if b"\0" in data:
		raise ValueError("review-fix target is binary")
	lines = data.splitlines(keepends=True)
	merged: list[tuple[int, int]] = []
	for start, end in sorted(ranges):
		start = max(1, start)
		end = min(len(lines), end)
		if start > end:
			continue
		if merged and start <= merged[-1][1] + 1:
			merged[-1] = (merged[-1][0], max(merged[-1][1], end))
		else:
			merged.append((start, end))
	if not merged:
		raise ValueError("review-fix target has no bounded current-head lines")
	offsets = [0]
	for line in lines:
		offsets.append(offsets[-1] + len(line))
	anchors: list[bytes] = []
	anchor_start = 0
	for start, end in merged:
		range_start = offsets[start - 1]
		range_end = offsets[end]
		anchors.append(data[anchor_start:range_start])
		anchor_start = range_end
	anchors.append(data[anchor_start:])
	return [base64.b64encode(anchor).decode("ascii") for anchor in anchors]


def _build_review_fix_authorization(args: argparse.Namespace) -> int:
	repo = Path(args.review_fix_repo).resolve(strict=True)
	if re.fullmatch(r"[0-9a-f]{40,64}", args.review_fix_head_sha or "") is None:
		raise ValueError("review-fix head SHA is invalid")
	current_head = subprocess.run(
		["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=False
	).stdout.strip()
	if current_head != args.review_fix_head_sha:
		raise ValueError("review-fix head SHA does not match the checkout")
	diff_path = Path(args.review_fix_diff_file)
	diff_text = diff_path.read_text(encoding="utf-8")
	hunks = _parse_unified_diff(diff_text)
	if not hunks:
		raise ValueError("review-fix diff has no eligible current-file hunks")
	evidence_parts: list[str] = []
	source_digests: dict[str, str] = {"diff": _file_digest(diff_path)}
	for index, evidence_name in enumerate(args.review_fix_evidence_file):
		evidence_path = Path(evidence_name)
		evidence_parts.append(evidence_path.read_text(encoding="utf-8"))
		source_digests[f"evidence_{index}"] = _file_digest(evidence_path)
	for index, comments_name in enumerate(args.review_fix_comments_json_file):
		comments_path = Path(comments_name)
		evidence_parts.append(_trusted_comment_evidence(comments_path, args.review_fix_repository))
		source_digests[f"comments_{index}"] = _file_digest(comments_path)
	floor_text = _read_text_file(args.review_fix_floor_tags_file) if args.review_fix_floor_tags_file else ""
	if args.review_fix_floor_tags_file:
		source_digests["floor_tags"] = _file_digest(Path(args.review_fix_floor_tags_file))
	targets: list[dict[str, object]] = []
	path_ranges: dict[str, list[tuple[int, int]]] = {}
	for cited_path, cited_line in _citations_from_text("\n".join(evidence_parts)):
		matching_hunk = next(
			((start, end) for start, end in hunks.get(cited_path, []) if start <= cited_line <= end), None
		)
		if matching_hunk is None:
			continue
		protected = cited_path.startswith(PROTECTED_REVIEW_FIX_PREFIXES)
		protected_authorized = not protected or _floor_authorized(cited_path, cited_line, floor_text)
		if not protected_authorized:
			continue
		target_id = "review_fix_" + hashlib.sha256(
			f"{args.review_fix_head_sha}\0{cited_path}\0{matching_hunk[0]}\0{matching_hunk[1]}".encode()
		).hexdigest()[:20]
		if any(row["id"] == target_id for row in targets):
			continue
		targets.append({
			"id": target_id,
			"path": cited_path,
			"line_start": matching_hunk[0],
			"line_end": matching_hunk[1],
			"provenance": "multi-reviewer-floor" if protected else "trusted-review-evidence",
			"protected_path_authorized": protected_authorized,
		})
		path_ranges.setdefault(cited_path, []).append(matching_hunk)
		if len(targets) > MAX_REVIEW_FIX_TARGETS:
			raise ValueError("review-fix target count exceeds bound")
	if not targets:
		raise ValueError("review-fix evidence produced no authorized targets")
	path_rows = []
	for path_name, ranges in sorted(path_ranges.items()):
		file_path = repo / path_name
		if not file_path.is_file() or file_path.is_symlink():
			raise ValueError("review-fix target is not a regular existing file")
		mode = "100755" if os.lstat(file_path).st_mode & 0o100 else "100644"
		path_rows.append({
			"path": path_name,
			"mode": mode,
			"anchors": _anchors_for_ranges(file_path, ranges),
			"target_ids": [row["id"] for row in targets if row["path"] == path_name],
		})
	manifest = {
		"schema_version": REVIEW_FIX_SCHEMA,
		"repository": args.review_fix_repository,
		"pr_number": int(args.review_fix_pr_number),
		"head_sha": args.review_fix_head_sha,
		"producer_run": args.review_fix_producer_run,
		"source_digests": source_digests,
		"targets": targets,
		"paths": path_rows,
	}
	encoded = json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
	if len(encoded.encode()) > MAX_REVIEW_FIX_MANIFEST_BYTES:
		raise ValueError("review-fix authorization exceeds size bound")
	Path(args.review_fix_output).write_text(encoded, encoding="utf-8")
	return EXIT_IN_SCOPE


def _validate_review_fix_authorization(args: argparse.Namespace) -> int:
	try:
		manifest = json.loads(Path(args.review_fix_authorization_file).read_text(encoding="utf-8"))
		selected = json.loads(Path(args.review_fix_selected_targets_file).read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError) as exc:
		raise ValueError("review-fix authorization input is malformed") from exc
	if not isinstance(manifest, dict) or manifest.get("schema_version") != REVIEW_FIX_SCHEMA:
		raise ValueError("review-fix authorization schema is invalid")
	if manifest.get("repository") != args.review_fix_repository:
		raise ValueError("review-fix repository binding is stale")
	if manifest.get("pr_number") != int(args.review_fix_pr_number):
		raise ValueError("review-fix PR binding is stale")
	if manifest.get("head_sha") != args.review_fix_head_sha:
		raise ValueError("review-fix head binding is stale")
	repo = Path(args.review_fix_repo).resolve(strict=True)
	current_head = subprocess.run(
		["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=False
	).stdout.strip()
	if current_head != args.review_fix_head_sha:
		raise ValueError("review-fix checkout moved after authorization")
	if not isinstance(selected, list) or not selected or any(not isinstance(item, str) for item in selected):
		raise ValueError("review-fix selected targets must be a non-empty string array")
	targets = manifest.get("targets")
	paths = manifest.get("paths")
	if not isinstance(targets, list) or not isinstance(paths, list):
		raise ValueError("review-fix authorization rows are invalid")
	target_by_id = {
		row.get("id"): row for row in targets
		if isinstance(row, dict) and isinstance(row.get("id"), str)
	}
	if len(set(selected)) != len(selected) or any(target_id not in target_by_id for target_id in selected):
		raise ValueError("review-fix selected target is unknown or duplicated")
	selected_paths = {str(target_by_id[target_id].get("path") or "") for target_id in selected}
	touched = {_strict_repository_file(path) for path in _read_staged(args.staged_file or None)}
	if not touched or not touched.issubset(selected_paths):
		raise ValueError("review-fix touched paths exceed selected targets")
	path_by_name = {
		str(row.get("path")): row for row in paths
		if isinstance(row, dict) and isinstance(row.get("path"), str)
	}
	span_manifest: dict[str, object] = {}
	for path_name in touched:
		path_row = path_by_name.get(path_name)
		if path_row is None or path_row.get("mode") not in {"100644", "100755"}:
			raise ValueError("review-fix path authorization is malformed")
		selected_ranges = [
			(int(target_by_id[target_id]["line_start"]), int(target_by_id[target_id]["line_end"]))
			for target_id in selected
			if target_by_id[target_id].get("path") == path_name
		]
		baseline = subprocess.run(
			["git", "show", f"{args.review_fix_head_sha}:{path_name}"], cwd=repo,
			capture_output=True, check=False,
		)
		if baseline.returncode != 0:
			raise ValueError("review-fix baseline content is unavailable")
		span_manifest[path_name] = {
			"mode": path_row["mode"],
			"anchors": _anchors_for_data(baseline.stdout, selected_ranges),
		}
	if set(span_manifest) != touched:
		raise ValueError("review-fix path authorization is incomplete")
	Path(args.review_fix_spans_output).write_text(
		json.dumps(span_manifest, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8"
	)
	Path(args.allowlist_out).write_text("".join(f"{path}\n" for path in sorted(touched)), encoding="utf-8")
	return EXIT_IN_SCOPE


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="files_touched scope-enforcement guard")
	parser.add_argument("--issue-body-file", default="")
	parser.add_argument("--allowlist-file", default="")
	parser.add_argument("--staged-file", default="")
	parser.add_argument("--allowlist-out", default="")
	parser.add_argument("--plan-file", default="")
	parser.add_argument("--linked-issue-metadata-file", default="")
	parser.add_argument("--linked-issue-metadata-sha256", default="")
	parser.add_argument("--issue-author-association", default="")
	parser.add_argument("--issue-author-login", default="")
	parser.add_argument(
		"--generated-advisory-mode",
		choices=("auto", "required", "off"),
		default="off",
	)
	parser.add_argument("--build-review-fix-authorization", action="store_true")
	parser.add_argument("--validate-review-fix-authorization", action="store_true")
	parser.add_argument("--review-fix-repo", default=".")
	parser.add_argument("--review-fix-repository", default="")
	parser.add_argument("--review-fix-pr-number", type=int, default=0)
	parser.add_argument("--review-fix-head-sha", default="")
	parser.add_argument("--review-fix-producer-run", default="")
	parser.add_argument("--review-fix-diff-file", default="")
	parser.add_argument("--review-fix-evidence-file", action="append", default=[])
	parser.add_argument("--review-fix-comments-json-file", action="append", default=[])
	parser.add_argument("--review-fix-floor-tags-file", default="")
	parser.add_argument("--review-fix-output", default="")
	parser.add_argument("--review-fix-authorization-file", default="")
	parser.add_argument("--review-fix-selected-targets-file", default="")
	parser.add_argument("--review-fix-spans-output", default="")
	args = parser.parse_args(argv)
	if args.build_review_fix_authorization or args.validate_review_fix_authorization:
		if args.build_review_fix_authorization == args.validate_review_fix_authorization:
			print("exactly one review-fix authorization mode is required", file=sys.stderr)
			return EXIT_INVALID_GENERATED_ADVISORY
		try:
			if args.build_review_fix_authorization:
				if not args.review_fix_diff_file or not args.review_fix_output:
					raise ValueError("review-fix build paths are required")
				return _build_review_fix_authorization(args)
			if not (
				args.review_fix_authorization_file
				and args.review_fix_selected_targets_file
				and args.review_fix_spans_output
				and args.allowlist_out
			):
				raise ValueError("review-fix validation paths are required")
			return _validate_review_fix_authorization(args)
		except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
			print(f"review-fix authorization rejected: {str(exc)[:300]}", file=sys.stderr)
			return EXIT_INVALID_GENERATED_ADVISORY

	issue_body = _read_text_file(args.issue_body_file) if args.issue_body_file else ""
	issue_author_association = args.issue_author_association
	issue_author_login = args.issue_author_login
	if args.linked_issue_metadata_file:
		try:
			linked_metadata_text = _read_verified_linked_issue_metadata(
				args.linked_issue_metadata_file,
				args.linked_issue_metadata_sha256,
			)
			linked_advisory = parse_linked_issue_metadata(linked_metadata_text)
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

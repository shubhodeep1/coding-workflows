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
                          [--origin-repo OWNER/NAME]
                          Snapshot trusted-author exact automation paths. A
                          pipeline-authored issue derived from another issue
                          (re-issue, unblock fix-up) is granted only when its
                          whole source chain is trusted; --origin-repo lets
                          the guard read those source issues (Q82).
  --check-automation-paths --automation-grant-file PATH --staged-file PATH
                          --allow-workflow-edits VALUE --issue-number NUMBER
                          Verify staged automation paths against the snapshot.
  --plan-auto-approval-gate --plan-file PATH --flag-enabled VALUE
                          [--issue-meta-file PATH --pipeline-login LOGIN
                          --allow-workflow-edits VALUE --issue-number NUMBER
                          --origin-repo OWNER/NAME
                          --pipeline-grant-body-out PATH]
                          Decide whether an automatic `/approved` may be posted
                          for a plan that lists automation paths (issue #6769).
                          Prints one JSON line; exit 0 approves, 30 holds.
                          With --pipeline-grant-body-out, a pipeline-authored
                          issue without files_touched can approve with reason
                          pipeline_plan_grant; the record then has
                          pipeline_grant_body: true and the caller must write
                          the body at PATH to the issue first.
                          With --issue-meta-file, a plan whose Files section
                          lists an exact file outside the issue's
                          files_touched list holds with reason
                          plan_files_outside_files_touched, while
                          ENFORCE_FILES_TOUCHED is "true" (default),
                          ALLOW_OUT_OF_SCOPE_FILES is not "true" and
                          PLAN_FILES_TOUCHED_GATE_ENABLED is not off
                          (all read from the environment).
  --plan-auto-approval-gate --plan-file PATH --heal-scope-file PATH
                          --flag-enabled VALUE --issue-number NUMBER
                          Workflow-heal plans (#7444): approve only when every
                          path the plan lists is inside the verified heal
                          scope (one path per line). Never writes a grant.

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
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys
import time


EXIT_IN_SCOPE = 0
EXIT_SKIP_NO_ALLOWLIST = 10
EXIT_OUT_OF_SCOPE = 20
EXIT_AUTOMATION_UNGRANTED = 30

# Keep this prefix set in sync with _rb_fix_scope_is_protected in
# scripts/orchestrate_poll_process.sh. Case variants are denied here too.
AUTOMATION_PATH_PREFIXES = (".github", ".claude", "scripts", "prompts", "workflow-templates")
TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
PIPELINE_AUTHOR_OPEN_REASON = "pipeline_author_no_allowlist"
AUTOMATION_GRANT_SCHEMA = "automation_path_grant.v1"
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$")

# Source-issue references the pipeline writes into issues it derives from
# another issue (Q82). Only a pipeline-authored issue is walked: a person's
# own issue is trusted on its author alone. Every reference counts, so text
# copied from the source can only add checks, never remove one.
ORIGIN_REFERENCE_PATTERNS = (
	re.compile(r"Re-issued from\s+#([1-9][0-9]{0,9})\b"),  # stall recovery, no-op / validation re-issues
	re.compile(r"Re-issued by the unblock judge from #([1-9][0-9]{0,9})\b"),  # unblock reissue
	re.compile(r"<!--\s*ai:unblock-fixup:v1\s+item=([1-9][0-9]{0,9})\b"),  # unblock descope / operator_step
	re.compile(r"^-\s*Replaces:\s*#([1-9][0-9]{0,9})\b", re.MULTILINE),  # review-blocked re-issue footer
	re.compile(r"^-\s*Source issue:\s*#([1-9][0-9]{0,9})\b", re.MULTILINE),  # implement-diagnose fix-up
	re.compile(r"Accepted with this follow-up by the unblock judge \(#([1-9][0-9]{0,9})\)"),  # unblock accept_with_followup
	re.compile(r"^-\s*Parent issue:\s*#([1-9][0-9]{0,9})\b", re.MULTILINE),  # review-blocked merge_with_followup
)
# A review-blocked re-issue or follow-up whose metadata footer names no issue
# names only its PR, so its source cannot be checked: it gets no automation
# paths. The named-source line must sit in the same `---`-delimited block as
# the last `- Type:` line of that kind (the pipeline-written footer); a match
# in the model-written prose above it only adds a check, never names a source.
ORIGIN_UNNAMED_PATTERN = re.compile(r"^-\s*Type:\s*review-blocked-reissue\s*$", re.MULTILINE)
ORIGIN_FOOTER_KINDS = (
	(ORIGIN_UNNAMED_PATTERN, re.compile(r"^-\s*Replaces:\s*#[1-9][0-9]{0,9}\b", re.MULTILINE)),
	(re.compile(r"^-\s*Type:\s*review-blocked-followup\b.*$", re.MULTILINE),
		re.compile(r"^-\s*Parent issue:\s*#[1-9][0-9]{0,9}\b", re.MULTILINE)),
)
ORIGIN_FOOTER_RULE = re.compile(r"^---\s*$", re.MULTILINE)
ORIGIN_MAX_DEPTH = 4
ORIGIN_MAX_ISSUES = 8
ORIGIN_REASONS = ("untrusted_origin", "origin_unverified")

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
		# YAML also lets a bare `files_touched:` key keep its list at the
		# key's own indent (`- path` lines flush with the key, as the
		# orchestrator judge wrote #7296). The first entry fixes the indent:
		# a flush list continues only with flush entries.
		flush_allowed = stripped == "files_touched:"
		flush: bool | None = None
		entries: list[str] = []
		for candidate in lines[idx + 1 :]:
			if not candidate.strip():
				if entries:
					break
				continue
			indent = len(candidate) - len(candidate.lstrip(" "))
			if flush is None:
				flush = flush_allowed and indent == base_indent
			if flush:
				if indent != base_indent:
					break
			elif indent <= base_indent:
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


def origin_references(body: str) -> tuple[list[int], bool]:
	"""Source issue numbers a pipeline-authored body names, and whether it
	also claims a source it does not name (an unverifiable review-blocked
	re-issue)."""
	numbers: list[int] = []
	for pattern in ORIGIN_REFERENCE_PATTERNS:
		for match in pattern.finditer(body):
			number = int(match.group(1))
			if number not in numbers:
				numbers.append(number)
	unnamed = False
	for type_pattern, named_pattern in ORIGIN_FOOTER_KINDS:
		type_matches = list(type_pattern.finditer(body))
		if not type_matches:
			continue
		last = type_matches[-1]
		rules_before = [rule.end() for rule in ORIGIN_FOOTER_RULE.finditer(body, 0, last.start())]
		block_start = rules_before[-1] if rules_before else 0
		rule_after = ORIGIN_FOOTER_RULE.search(body, last.end())
		block_end = rule_after.start() if rule_after else len(body)
		if not named_pattern.search(body[block_start:block_end]):
			unnamed = True
	return numbers, unnamed


def fetch_issue_with_gh(repo: str, number: int) -> dict | None:
	"""One `gh api` read of a source issue; two attempts, None on failure."""
	for attempt in range(2):
		try:
			completed = subprocess.run(["gh", "api", f"repos/{repo}/issues/{number}"],
				capture_output=True, text=True, timeout=60, check=False)
		except (OSError, subprocess.SubprocessError):
			completed = None
		if completed is not None and completed.returncode == 0:
			try:
				data = json.loads(completed.stdout)
			except ValueError:
				data = None
			if isinstance(data, dict):
				return data
		if completed is not None and "HTTP 404" in (completed.stderr or ""):
			break  # a missing source will not appear on retry; fail closed now
		if attempt == 0:
			time.sleep(2)
	return None


def _origin_author_trusted(meta: dict, pipeline_login: str) -> tuple[bool, bool]:
	"""(trusted, pipeline_authored) for a source issue's author."""
	user = meta.get("user")
	login = user.get("login") if isinstance(user, dict) else ""
	login = login if isinstance(login, str) else ""
	association = meta.get("author_association")
	association = association.upper() if isinstance(association, str) else ""
	if not LOGIN_RE.fullmatch(login):
		return False, False
	if login == pipeline_login:
		return True, True
	return association in TRUSTED_AUTHOR_ASSOCIATIONS, False


def check_origin_chain(body: str, pipeline_login: str, fetch) -> tuple[str, list[int]]:
	"""Walk the source chain of a pipeline-authored issue body.

	Returns ("ok" | "untrusted_origin" | "origin_unverified", issues read).
	A source written by a trusted person ends its branch; one written by the
	pipeline is walked in turn. At most ORIGIN_MAX_DEPTH hops and
	ORIGIN_MAX_ISSUES issue reads (one `gh api` call each, §15); anything
	that cannot be read or exceeds the bounds fails closed.
	"""
	pending, unnamed = origin_references(body)
	if unnamed:
		return "origin_unverified", []
	frontier = [(number, 1) for number in pending]
	seen: list[int] = []
	while frontier:
		number, depth = frontier.pop(0)
		if number in seen:
			continue
		if depth > ORIGIN_MAX_DEPTH or len(seen) >= ORIGIN_MAX_ISSUES or fetch is None:
			return "origin_unverified", seen
		seen.append(number)
		meta = fetch(number)
		if not isinstance(meta, dict):
			return "origin_unverified", seen
		trusted, pipeline_authored = _origin_author_trusted(meta, pipeline_login)
		if not trusted:
			return "untrusted_origin", seen
		if pipeline_authored:
			source_body = meta.get("body") if isinstance(meta.get("body"), str) else ""
			numbers, source_unnamed = origin_references(source_body)
			if source_unnamed:
				return "origin_unverified", seen
			frontier.extend((source, depth + 1) for source in numbers)
	return "ok", seen


def build_automation_grant(issue_meta: dict, pipeline_login: str, fetch_origin=None) -> dict:
	"""Snapshot the author's authority and exact automation files in the issue.

	``fetch_origin(number) -> dict | None`` reads a source issue for the
	origin-chain check of a pipeline-authored issue; without it, a
	pipeline-authored issue that names a source gets no automation paths.
	"""
	if not isinstance(issue_meta, dict):
		raise ValueError("invalid issue metadata")
	user = issue_meta.get("user")
	author_login = user.get("login") if isinstance(user, dict) else ""
	author_login = author_login if isinstance(author_login, str) else ""
	author_association = issue_meta.get("author_association")
	author_association = author_association if isinstance(author_association, str) else ""
	record = {"schema_version": AUTOMATION_GRANT_SCHEMA, "issue_number": issue_meta.get("number"),
		"trusted": False, "open": False, "reason": "grant_error", "author_login": author_login,
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
		origin_status = "ok"
		if author_login == pipeline_login:
			origin_status, origin_issues = check_origin_chain(body, pipeline_login, fetch_origin)
			record["origin_issues"] = origin_issues
		if origin_status != "ok":
			# Q82: a re-issue or fix-up the pipeline filed from an issue that an
			# untrusted author wrote must not turn that author's request into
			# an automation-path grant.
			record["reason"] = origin_status
		elif state == "missing" and author_login == pipeline_login:
			# Pipeline-generated issues (security-pass fixes, re-issues, heal
			# issues) carry no files_touched block; Q24: A grants them every
			# automation path. A declared block still grants exact entries
			# only, and a malformed block still blocks.
			record["trusted"] = True
			record["open"] = True
			record["reason"] = PIPELINE_AUTHOR_OPEN_REASON
		elif state != "ok":
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
			for path in grant.get("paths", [])) \
		or type(grant.get("open", False)) is not bool \
		or (grant.get("open", False) is True and (grant["trusted"] is not True
			or grant.get("reason") != PIPELINE_AUTHOR_OPEN_REASON or grant["paths"] != [])):
		reason = "grant_unavailable"
	elif not issue_number.isdecimal() or type(grant.get("issue_number")) is not int \
		or grant["issue_number"] != int(issue_number):
		reason = "grant_issue_mismatch"
	elif not grant["trusted"]:
		reason = grant.get("reason") if grant.get("reason") in {
			"identity_unavailable", "untrusted_author", "no_allowlist", "malformed_allowlist", "grant_error",
			*ORIGIN_REASONS,
		} else "grant_unavailable"
	elif grant.get("open", False) is True:
		return STATUS_IN_SCOPE, [], PIPELINE_AUTHOR_OPEN_REASON
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


# CLAUDE.md §30: agents.md is documented through agents.d/<pr>-<slug>.md
# fragments that release automation folds in, so a scope that covers the agents
# file covers its top-level fragments too.
AGENTS_FRAGMENT_RE = re.compile(r"^agents\.d/[^/]+\.md$")
AGENTS_DOC_PATHS = ("agents.md", "AGENTS.md")


def is_agents_fragment(path: str) -> bool:
	"""True for a top-level ``agents.d/*.md`` fragment."""
	return bool(AGENTS_FRAGMENT_RE.match(path))


def path_in_scope(path: str, allowlist: list[str], *, allow_lockfiles: bool = True) -> bool:
	"""True when a staged path is auto-allowed or covered by any allowlist entry."""
	if allow_lockfiles and is_lockfile(path):
		return True
	for entry in allowlist:
		if entry_matches(entry, path):
			return True
	if is_agents_fragment(path):
		return any(entry_matches(entry, doc) for entry in allowlist for doc in AGENTS_DOC_PATHS)
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


# --- Plan auto-approval gate (issue #6769) ---------------------------------
# Automatic `/approved` posts (plan.yml "Auto-approve clear plan" and the
# poller's stall-recovery arms) are held for plans that list automation
# paths. While AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED is false (it defaults to true)
# every such plan is held; when it is on, every protected path the plan lists
# needs a trusted, exact issue-body grant. The open pipeline-author grant
# (PIPELINE_AUTHOR_OPEN_REASON) never counts as exact here, although the
# implement-time guard (check_automation_paths) still accepts it.

PLAN_DECISION_APPROVE = "approve"
PLAN_DECISION_HOLD = "hold"
PLAN_LOG_PATHS_MAX_ENTRIES = 20
PLAN_LOG_PATH_MAX_CHARS = 200
_PLAN_FILES_LEADIN_RE = re.compile(
	r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__)?(?:\d+[.)]\s*)?(?:\*\*|__)?\s*Files\b[^\n]*\bchange\b", re.IGNORECASE)
_PLAN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+\S")
# A lead-in that is not itself a heading has no known level; only a top-level
# (`#` or `##`) heading ends its section, so nested headings never truncate it.
_PLAN_PLAIN_LEADIN_LEVEL = 2
# A protected path token: optional "./" or "/" prefix, an automation prefix in
# any letter case, a slash, then everything up to whitespace, a backtick, a
# quote or `<>|` (globs kept so they hold). The token is never cut at an
# unusual filename character: `scripts/a+b.py` must stay whole, or a grant for
# `scripts/a` would approve it. `_` is not a boundary character, so
# `_scripts/x.sh_` or `my_scripts/x.sh` still holds (fail closed).
_PLAN_PATH_TOKEN_RE = re.compile(
	r"(?:^|[^A-Za-z0-9./-])(?:\./|/)?((?:\.github|\.claude|scripts|prompts|workflow-templates)/[^\s`'\"<>|]*)",
	re.IGNORECASE)
_PLAN_PATH_TRAILING_PUNCT = ".,;:!)]}"
_PLAN_PATH_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")
_PLAN_LOG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._/-]")


def _plan_files_section(plan_text: str) -> str | None:
	"""Text of a plan's "Files ... change" section; None when no section exists.

	A heading or numbered lead-in naming "Files ... change" opens the section.
	Only a heading at the lead-in's level or shallower ends it (a plain lead-in
	ends at a `#`/`##` heading), so a nested sub-heading cannot cut the list
	short. Negated wording ("No changes to:") does not suppress any line: every
	protected path in the section counts, so the gate fails closed (#6769).
	"""
	found = False
	inside = False
	level = 0
	kept: list[str] = []
	for line in plan_text.splitlines():
		if _PLAN_FILES_LEADIN_RE.match(line):
			found = True
			inside = True
			heading = _PLAN_HEADING_RE.match(line)
			level = len(heading.group(1)) if heading else _PLAN_PLAIN_LEADIN_LEVEL
			# Paths on the lead-in line itself ("Files to change: `scripts/x.sh`") count.
			kept.append(line)
			continue
		heading = _PLAN_HEADING_RE.match(line)
		if inside and heading and len(heading.group(1)) <= level:
			inside = False
		if inside:
			kept.append(line)
	return "\n".join(kept) if found else None


def _plan_path_token(raw: str) -> str:
	"""Strip prose punctuation, a `:line[:col]` suffix and markdown bold from a token."""
	token = raw
	while True:
		previous = token
		token = _PLAN_PATH_LINE_SUFFIX_RE.sub("", token.rstrip(_PLAN_PATH_TRAILING_PUNCT))
		if token.endswith("**") and not token.endswith("/**"):
			token = token.rstrip("*")
		if token == previous:
			return token


def extract_plan_automation_paths(plan_text: str) -> list[str]:
	"""Protected paths a plan lists, scanning its Files section.

	Falls back to the whole plan text when no Files section exists, so a plan
	without the section errs toward holding. Directory and glob entries are
	kept: they can never be granted exactly, so they hold the plan.
	"""
	section = _plan_files_section(plan_text or "")
	text = section if section is not None else (plan_text or "")
	seen: set[str] = set()
	paths: list[str] = []
	for line in text.splitlines():
		for match in _PLAN_PATH_TOKEN_RE.finditer(line):
			path = normalize_path(_plan_path_token(match.group(1)))
			if not path or not is_automation_path(path) or path in seen:
				continue
			seen.add(path)
			paths.append(path)
	return paths


def evaluate_plan_auto_approval(plan_paths: list[str], grant: object, *, flag_enabled: bool,
	allow_workflow_edits: str, issue_number: str) -> tuple[str, str, list[str]]:
	"""Decide whether an automatic `/approved` may be posted for a plan."""
	protected = [path for path in (normalize_path(p) for p in plan_paths) if path and is_automation_path(path)]
	if not protected:
		return PLAN_DECISION_APPROVE, "none", []
	if not flag_enabled:
		return PLAN_DECISION_HOLD, "automation_path_plan_auto_approval_disabled", protected
	if isinstance(grant, dict) and grant.get("open") is True:
		return PLAN_DECISION_HOLD, "open_grant_not_exact", protected
	status, denied, reason = check_automation_paths(protected, grant, allow_workflow_edits, issue_number)
	if status == STATUS_IN_SCOPE and reason == "granted":
		return PLAN_DECISION_APPROVE, "granted", []
	if status == STATUS_IN_SCOPE:
		# Any other in-scope reason (the open pipeline-author grant) is not an
		# exact grant for auto-approval.
		return PLAN_DECISION_HOLD, "open_grant_not_exact", protected
	return PLAN_DECISION_HOLD, reason, denied or protected


def plan_gate_log_paths(paths: list[str]) -> str:
	"""Log-safe, comma-joined rendering of untrusted plan paths."""
	rendered: list[str] = []
	for path in paths[:PLAN_LOG_PATHS_MAX_ENTRIES]:
		safe = _PLAN_LOG_UNSAFE_RE.sub("", path)[:PLAN_LOG_PATH_MAX_CHARS]
		if safe:
			rendered.append(safe)
	return ",".join(rendered) if rendered else "none"


# Pipeline plan grant (Q108). A pipeline-authored issue with no
# files_touched block gets the open grant, which never auto-approves a plan.
# When the gate would otherwise hold for that reason alone, it proposes an
# exact block: every file the plan's Files section lists plus the companion
# entries below. The caller writes it into the issue body (the pipeline
# account owns the issue) and only then posts /approved; from then on the
# block is the issue's ordinary exact grant and its implement-time scope.
PIPELINE_PLAN_GRANT_COMPANIONS = ("tests/**", "changelog.d/*", "agents.d/*")
PIPELINE_PLAN_GRANT_MAX_PATHS = 50
PIPELINE_PLAN_GRANT_MARKER = "<!-- ai:pipeline-plan-grant:v1 -->"
PIPELINE_PLAN_GRANT_REASON = "pipeline_plan_grant"
# A plan whose Files section lists more than PIPELINE_PLAN_GRANT_MAX_PATHS
# files gets no grant. The plan stage hands it to the orchestrator as a
# project instead (scripts/plan_split_dispatch.sh, Q151); the record carries
# the count as ``plan_path_count``.
PIPELINE_PLAN_TOO_LARGE_REASON = "plan_too_large_for_grant"
_PLAN_GRANT_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_PLAN_GRANT_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(\S+)")
_PLAN_GRANT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.@+-]+$")
# A root file is any name with a letter-led extension (the same rule as a
# nested last segment), so a planned `main.c` or `app.php` is never left out of
# the grant; a harmless extra entry beats an approved plan whose implement
# scope then blocks a listed file. A root dotfile needs an extension
# (`.gitlab-ci.yml`) or one of the names below: an extensionless dot name
# (`.vscode`, `.devcontainer`) is often a directory. `.env*` files, which can
# hold credentials, are never written.
_PLAN_GRANT_ROOT_FILE_RE = re.compile(
	r"^(?:[A-Za-z0-9_][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]*"
	r"|\.(?!env(?:\.|$))[A-Za-z0-9_-][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]*"
	r"|\.(?:gitignore|gitattributes|gitmodules|editorconfig|dockerignore|npmignore|prettierrc|prettierignore"
	r"|eslintrc|eslintignore|stylelintrc|nvmrc|node-version|python-version|ruby-version|tool-versions|flake8"
	r"|pylintrc|yamllint|shellcheckrc|mailmap|markdownlint)"
	r"|Makefile|Dockerfile)$")
# Extensionless file names accepted as the last segment of a grant path. Any
# other extensionless (or `.d`) last segment may be a directory, and a
# files_touched entry without a trailing slash covers everything beneath it
# (entry_matches), so it is never written as an "exact" grant.
_PLAN_GRANT_BARE_FILE_RE = re.compile(
	r"^(?:Makefile|Dockerfile|LICENSE|COPYING|NOTICE|CODEOWNERS|Gemfile|Rakefile|Procfile|Justfile|Vagrantfile"
	r"|Pipfile|Jenkinsfile|Containerfile|Brewfile|Podfile|Tiltfile)$")
# The extension must start with a letter: a digit-led suffix (`v1.0`,
# `2024.01.15`) is usually a versioned directory, which would cover its subtree.
_PLAN_GRANT_FILE_SEGMENT_RE = re.compile(r"^(?:\.[A-Za-z0-9_-][A-Za-z0-9._-]*|[^.].*\.[A-Za-z][A-Za-z0-9]*)$")


def _plan_grant_path(raw: str) -> str:
	"""A plan token as an exact repo-relative file path, or "" when it is not one.

	Directories, globs, absolute or parent paths, URLs and anything outside a
	plain path alphabet are rejected: they can never be an exact grant.
	"""
	# Only `*` is stripped: `_` is part of names such as `__init__.py`.
	path = normalize_path(_plan_path_token(raw.strip().strip("*")))
	if not path or len(path) > 512 or path.endswith("/") or path.startswith("/") or "//" in path:
		return ""
	segments = path.split("/")
	if any(seg in {"", ".", ".."} or not _PLAN_GRANT_SEGMENT_RE.match(seg) for seg in segments):
		return ""
	if segments[0].startswith("-") or any(char in path for char in _GLOB_CHARS):
		return ""
	last = segments[-1]
	if _PLAN_GRANT_BARE_FILE_RE.match(last):
		return path
	if len(segments) == 1 and not _PLAN_GRANT_ROOT_FILE_RE.match(path):
		return ""
	if not _PLAN_GRANT_FILE_SEGMENT_RE.match(last) or last.endswith(".d"):
		return ""
	return path


def extract_plan_file_paths(plan_text: str) -> list[str]:
	"""Exact file paths a plan's Files section lists (empty without a section).

	Reads backticked spans, a bullet's first token when the bullet has no
	backticks, and bare protected-path tokens. Only tokens that are exact file
	paths are kept (see _plan_grant_path); a listed directory or glob is
	dropped here and still holds the plan through the protected-path check.
	"""
	section = _plan_files_section(plan_text or "")
	if section is None:
		return []
	seen: set[str] = set()
	paths: list[str] = []
	for line in section.splitlines():
		candidates = _PLAN_GRANT_BACKTICK_RE.findall(line)
		# The bullet's first token counts even when the line also has
		# backticked spans (`- README.md — use `parse()``): otherwise a
		# non-path span would drop the listed file from the grant.
		bullet = _PLAN_GRANT_BULLET_RE.match(line)
		if bullet:
			candidates.append(bullet.group(1))
		candidates.extend(match.group(1) for match in _PLAN_PATH_TOKEN_RE.finditer(line))
		for raw in candidates:
			path = _plan_grant_path(raw)
			if path and path not in seen:
				seen.add(path)
				paths.append(path)
	return paths


def build_pipeline_plan_grant_body(body: str, plan_paths: list[str]) -> str:
	"""Issue body with a canonical files_touched block appended after a rule."""
	entries = list(plan_paths) + [c for c in PIPELINE_PLAN_GRANT_COMPANIONS if c not in plan_paths]
	block = [
		"",
		"---",
		("**Pipeline plan grant.** The pipeline wrote this `files_touched` list from the approved plan's"
			" file list, plus tests, changelog and agents fragments. Implementation is limited to these paths."),
		PIPELINE_PLAN_GRANT_MARKER,
		"files_touched:",
	] + ["  - " + entry for entry in entries]
	return (body or "").rstrip("\n") + "\n" + "\n".join(block) + "\n"


def _flag_enabled(value: str) -> bool:
	# On by default: an unset or empty value is on, like the workflow defaults.
	return (value or "").strip().lower() in {"", "1", "true", "yes", "on"}


# Plan files outside files_touched. The implement-time scope guard refuses a
# commit that stages a path outside the issue's files_touched list and latches
# ai:scope-blocked. A plan that already names such a file in its Files section
# is held here instead, before an automatic /approved spends an implementation
# run that is certain to be refused (consumer tele-funtoken-msg-scoring #4795:
# the planner widened the scope in prose, auto-approval passed, and the
# implement run was blocked on the three files it added).
PLAN_FILES_TOUCHED_GATE_REASON = "plan_files_outside_files_touched"


def plan_files_touched_gate_active() -> bool:
	"""True when the implement guard would block, so the plan gate checks too.

	Mirrors implement.yml: ENFORCE_FILES_TOUCHED enforces only when it is
	exactly "true" (default) and ALLOW_OUT_OF_SCOPE_FILES downgrades a block
	only when exactly "true". PLAN_FILES_TOUCHED_GATE_ENABLED (default true)
	turns this plan-time check off on its own.
	"""
	if (os.environ.get("PLAN_FILES_TOUCHED_GATE_ENABLED") or "true").strip().lower() in {"false", "0", "no", "off"}:
		return False
	return (os.environ.get("ENFORCE_FILES_TOUCHED") or "true") == "true" \
		and (os.environ.get("ALLOW_OUT_OF_SCOPE_FILES") or "false") != "true"


# Implement's heal-route classifier (workflow_failure_heal.is_heal_route): the
# ai:workflow-heal label or the intake's fingerprint marker. The scope marker
# alone does not put an issue on the heal route.
_HEAL_ROUTE_FP_RE = re.compile(r"<!-- workflow-failure-heal:fp=[0-9a-f]{64} -->")


def _issue_on_heal_route(issue_body: str, labels: object) -> bool:
	names = [label.get("name") if isinstance(label, dict) else label
		for label in (labels if isinstance(labels, list) else [])]
	return "ai:workflow-heal" in names or bool(_HEAL_ROUTE_FP_RE.search(issue_body or ""))


def plan_files_outside_files_touched(plan_text: str, issue_body: str, labels: object = None) -> list[str]:
	"""Exact files in a plan's Files section that the issue's files_touched list misses.

	Uses the implement guard's matcher (path_in_scope: lockfiles and agents.d
	fragments follow the same rules). An issue without a list, or with an empty
	or malformed one, is not checked, as the implement guard skips it too. A
	directory or glob the plan lists is not an exact file and is not checked
	here; the implement guard still applies to whatever gets staged. An issue
	on the heal route (the ai:workflow-heal label or the heal fingerprint
	marker, as implement decides it) is not checked: there implement enforces
	the heal scope marker, not files_touched.
	"""
	if _issue_on_heal_route(issue_body, labels):
		return []
	allowlist = normalize_allowlist(extract_files_touched(issue_body or "") or [])
	if not allowlist:
		return []
	return [path for path in extract_plan_file_paths(plan_text) if not path_in_scope(path, allowlist)]


def run_plan_auto_approval_gate(plan_file: str, flag_value: str, issue_meta_file: str,
	pipeline_login: str, allow_workflow_edits: str, issue_number: str, origin_repo: str = "",
	grant_body_out: str = "") -> tuple[dict, int]:
	"""CLI body: returns (decision record, exit code). Errors always hold.

	``origin_repo`` (owner/name) lets a pipeline-authored issue that names a
	source issue pass the Q82 origin-chain check, as the implement-time grant
	does; without it such an issue holds with ``origin_unverified``.

	With ``grant_body_out``, a hold caused only by the open pipeline-author
	grant becomes ``approve`` with reason ``pipeline_plan_grant`` when the body
	from build_pipeline_plan_grant_body would grant every protected path. That
	body is written to ``grant_body_out`` and the record carries
	``pipeline_grant_body: true``; the caller must write it to the issue
	before it posts /approved, and hold if that write fails.
	"""
	protected: list[str] = []
	grant_proposed = False
	plan_path_count = 0
	try:
		with open(plan_file, encoding="utf-8") as handle:
			plan_text = handle.read()
		if not plan_text.strip():
			raise ValueError("empty plan")
		protected = extract_plan_automation_paths(plan_text)
		flag_on = _flag_enabled(flag_value)
		grant: object = None
		metadata = None
		if protected and flag_on:
			if not issue_meta_file:
				record = {"decision": PLAN_DECISION_HOLD, "reason": "issue_meta_unavailable",
					"protected_paths": protected, "denied_paths": protected}
				record["log_paths"] = plan_gate_log_paths(protected)
				return record, EXIT_AUTOMATION_UNGRANTED
			with open(issue_meta_file, encoding="utf-8") as handle:
				metadata = json.load(handle)
			repo = origin_repo if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", origin_repo or "") else ""
			origin_cache: dict[int, dict | None] = {}

			def fetch_origin(number: int) -> dict | None:
				# Cached so the proposed-grant re-check below reads each
				# source issue once.
				if number not in origin_cache:
					origin_cache[number] = fetch_issue_with_gh(repo, number)
				return origin_cache[number]

			grant = build_automation_grant(metadata, pipeline_login, fetch_origin if repo else None)
		decision, reason, denied = evaluate_plan_auto_approval(protected, grant, flag_enabled=flag_on,
			allow_workflow_edits=allow_workflow_edits, issue_number=issue_number)
		if (grant_body_out and decision == PLAN_DECISION_HOLD and reason == "open_grant_not_exact"
				and isinstance(grant, dict) and grant.get("open") is True
				and grant.get("reason") == PIPELINE_AUTHOR_OPEN_REASON):
			plan_paths = extract_plan_file_paths(plan_text)
			if len(plan_paths) > PIPELINE_PLAN_GRANT_MAX_PATHS:
				# The open grant hid the workflow-edits switch from the check
				# above; a plan the implement guard would refuse anyway (for
				# example workflow_edits_disabled) keeps that reason, so it is
				# never handed to a project whose children would be blocked.
				status, open_denied, open_reason = check_automation_paths(protected, grant, allow_workflow_edits,
					issue_number)
				if status != STATUS_IN_SCOPE:
					decision, reason, denied = PLAN_DECISION_HOLD, open_reason, open_denied or protected
				else:
					decision, reason = PLAN_DECISION_HOLD, PIPELINE_PLAN_TOO_LARGE_REASON
					plan_path_count = len(plan_paths)
			elif plan_paths:
				proposed_body = build_pipeline_plan_grant_body(metadata.get("body") or "", plan_paths)
				proposed_grant = build_automation_grant(dict(metadata, body=proposed_body), pipeline_login,
					fetch_origin if repo else None)
				proposed = evaluate_plan_auto_approval(protected, proposed_grant, flag_enabled=flag_on,
					allow_workflow_edits=allow_workflow_edits, issue_number=issue_number)
				if proposed[0] == PLAN_DECISION_APPROVE:
					with open(grant_body_out, "w", encoding="utf-8") as handle:
						handle.write(proposed_body)
					decision, reason, denied = PLAN_DECISION_APPROVE, PIPELINE_PLAN_GRANT_REASON, []
					grant_proposed = True
				else:
					# Report what the proposed grant still misses (a listed
					# directory or glob, or a path the plan named only in prose).
					decision, reason, denied = proposed
		# An approval the implement scope guard would refuse is held. The
		# pipeline plan grant is skipped: its block is written from this plan.
		# Without issue metadata (the poller's cache had no body) there is no
		# list to compare, so the check is skipped; implement still enforces.
		if decision == PLAN_DECISION_APPROVE and not grant_proposed and issue_meta_file \
				and plan_files_touched_gate_active():
			issue_meta = metadata
			if issue_meta is None:
				with open(issue_meta_file, encoding="utf-8") as handle:
					issue_meta = json.load(handle)
			outside = plan_files_outside_files_touched(plan_text, issue_meta.get("body") or "",
				issue_meta.get("labels"))
			if outside:
				decision, reason, denied = PLAN_DECISION_HOLD, PLAN_FILES_TOUCHED_GATE_REASON, outside
	except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError, AttributeError):
		decision, reason, denied = PLAN_DECISION_HOLD, "plan_gate_error", protected
		grant_proposed = False
		plan_path_count = 0
	record = {"decision": decision, "reason": reason, "protected_paths": protected, "denied_paths": denied,
		"log_paths": plan_gate_log_paths(denied or protected)}
	if grant_proposed:
		record["pipeline_grant_body"] = True
	if plan_path_count:
		record["plan_path_count"] = plan_path_count
	return record, (EXIT_IN_SCOPE if decision == PLAN_DECISION_APPROVE else EXIT_AUTOMATION_UNGRANTED)


# Workflow-heal plans (unblock fix-up #7444 for #7434). A heal issue's scope is
# its intake-written ai:workflow-heal-scope:v1 marker, and the implement
# preflight refuses a body that was edited. So plan.yml never writes a Q108
# grant into a heal issue: it verifies the live marker (workflow_failure_heal.py
# heal-scope verify), passes the verified paths here, and this gate approves only
# when every path the plan lists is inside them. The matcher is the one the heal
# route's commit check uses (strict: no lockfile allowance). No body is written.
HEAL_PLAN_APPROVE_REASON = "heal_scope_verified"
_HEAL_BARE_GLOB_RE = re.compile(r"[A-Za-z0-9_.*?/\[\]-]+")


def run_heal_plan_scope_gate(plan_file: str, heal_scope_file: str, flag_value: str,
	issue_number: str, allow_workflow_edits: str = "false") -> tuple[dict, int]:
	"""Approve a heal plan only inside its verified heal scope; every error holds."""
	protected: list[str] = []
	denied: list[str] = []
	try:
		with open(plan_file, encoding="utf-8") as handle:
			plan_text = handle.read()
		if not plan_text.strip():
			raise ValueError("empty plan")
		protected = extract_plan_automation_paths(plan_text)
		scope = normalize_allowlist(_read_allowlist(heal_scope_file)) if heal_scope_file else []
		files = extract_plan_file_paths(plan_text)
		if not scope:
			decision, reason, denied = PLAN_DECISION_HOLD, "heal_scope_check_unavailable", protected
		elif protected and not _flag_enabled(flag_value):
			decision, reason, denied = PLAN_DECISION_HOLD, "automation_path_plan_auto_approval_disabled", protected
		elif protected and allow_workflow_edits != "true":
			# Same switch check_automation_paths applies at implement time.
			decision, reason, denied = PLAN_DECISION_HOLD, "workflow_edits_disabled", protected
		elif _plan_files_section(plan_text) is None or (not files and not protected):
			decision, reason, denied = PLAN_DECISION_HOLD, "heal_scope_plan_paths_missing", protected
		else:
			# extract_plan_file_paths drops directory and glob tokens, so read
			# them back from the Files section: a heal plan never approves a
			# path set it cannot name exactly.
			loose: list[str] = []
			for line in (_plan_files_section(plan_text) or "").splitlines():
				raw_tokens = [(raw, True) for raw in _PLAN_GRANT_BACKTICK_RE.findall(line)]
				bullet = _PLAN_GRANT_BULLET_RE.match(line)
				if bullet:
					raw_tokens.append((bullet.group(1), False))
				for raw, backticked in raw_tokens:
					raw = raw.strip()
					if not backticked:
						if "`" in raw:
							# Its backticked spans were read above (``**`a/b.py`**``).
							continue
						# A leading bold marker is markdown, not a glob (`**Files`).
						raw = re.sub(r"^\*\*(?=[^*/])", "", raw)
					token = normalize_path(_plan_path_token(raw))
					if not token:
						continue
					globlike = token.endswith("/") or any(char in token for char in _GLOB_CHARS)
					# A bare root-level glob (`- *.py`, `- *foo*`) counts; a word
					# with prose punctuation (`*Optional:*`) does not. An
					# italic-shaped word is ambiguous, so it is checked (fail closed).
					if globlike and not backticked and "/" not in token \
						and not _HEAL_BARE_GLOB_RE.fullmatch(token):
						continue
					# Also keep nested tokens _plan_grant_path rejects (an
					# extensionless `docs/guide`) so the scope check sees them.
					if globlike or ("/" in token and "`" not in token and not _plan_grant_path(token)):
						loose.append(token)
			candidates: list[str] = []
			for path in files + protected + loose:
				if path not in candidates:
					candidates.append(path)
			denied = [path for path in candidates if path.endswith("/") or any(char in path for char in _GLOB_CHARS)
				or not path_in_scope(path, scope, allow_lockfiles=False)]
			if denied:
				decision, reason = PLAN_DECISION_HOLD, "heal_scope_plan_out_of_scope"
			else:
				decision, reason = PLAN_DECISION_APPROVE, HEAL_PLAN_APPROVE_REASON
	except (OSError, UnicodeDecodeError, ValueError, TypeError):
		decision, reason, denied = PLAN_DECISION_HOLD, "plan_gate_error", protected
	record = {"decision": decision, "reason": reason, "protected_paths": protected, "denied_paths": denied,
		"log_paths": plan_gate_log_paths(denied or protected)}
	return record, (EXIT_IN_SCOPE if decision == PLAN_DECISION_APPROVE else EXIT_AUTOMATION_UNGRANTED)


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
	parser.add_argument("--origin-repo", default="", help="owner/name to read source issues from (Q82)")
	parser.add_argument("--check-automation-paths", action="store_true")
	parser.add_argument("--automation-grant-file", default="")
	parser.add_argument("--allow-workflow-edits", default="false")
	parser.add_argument("--issue-number", default="")
	parser.add_argument("--plan-auto-approval-gate", action="store_true")
	parser.add_argument("--plan-file", default="")
	parser.add_argument("--flag-enabled", default="true")
	parser.add_argument("--pipeline-grant-body-out", default="",
		help="plan gate: write the proposed pipeline plan grant body here (Q108)")
	parser.add_argument("--heal-scope-file", default="",
		help="plan gate: verified workflow-heal scope paths; approve only inside them, never write a grant (#7444)")
	args = parser.parse_args(argv)
	if args.plan_auto_approval_gate and args.heal_scope_file:
		record, code = run_heal_plan_scope_gate(args.plan_file, args.heal_scope_file, args.flag_enabled,
			args.issue_number, args.allow_workflow_edits)
		sys.stdout.write(json.dumps(record) + "\n")
		return code
	if args.plan_auto_approval_gate:
		record, code = run_plan_auto_approval_gate(args.plan_file, args.flag_enabled, args.issue_meta_file,
			args.pipeline_login, args.allow_workflow_edits, args.issue_number, args.origin_repo,
			args.pipeline_grant_body_out)
		sys.stdout.write(json.dumps(record) + "\n")
		return code
	if args.emit_automation_grant:
		try:
			metadata = json.loads(_read_text_file(args.issue_meta_file))
			origin_repo = args.origin_repo if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.origin_repo or "") else ""
			fetch_origin = (lambda number: fetch_issue_with_gh(origin_repo, number)) if origin_repo else None
			record = build_automation_grant(metadata, args.pipeline_login, fetch_origin)
		except (ValueError, TypeError, KeyError):
			record = {"schema_version": AUTOMATION_GRANT_SCHEMA, "issue_number": None,
				"trusted": False, "open": False, "reason": "grant_error", "author_login": "", "author_association": "", "paths": []}
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

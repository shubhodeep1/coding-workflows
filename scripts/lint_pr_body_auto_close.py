#!/usr/bin/env python3
"""Lint PR title/body text (and optionally commit messages) for GitHub auto-
close keywords that target ai:orchestrator-tracking issues.

Enforces CLAUDE.md §19 (and the mirror in unattended_system_instructions.md):
no `Fixes/Closes/Resolves #N` keyword may reference an `ai:orchestrator-tracking`
issue in a PR body, PR title, or commit message that will land on the default
branch. On squash-merge GitHub uses the PR title + body for the squash commit
message and auto-closes referenced issues, which kills the orchestrator's
state machine (the poller filters tracking issues by `--state open`).

Exit codes:
  0 — no violations (no auto-close keywords, OR none reference an
      ai:orchestrator-tracking issue).
  1 — at least one violation. Each violation is printed to stderr with the
      matched text, issue number, and remediation guidance.
  2 — usage error or unrecoverable lookup failure (e.g. gh CLI unavailable
      AND `--fail-open-on-lookup-error` not set).

Usage:
  python3 scripts/lint_pr_body_auto_close.py \\
    --pr-body-file <path>            # required; path to PR title/body text file
    [--commit-messages-file <path>]  # optional; one commit message per record,
                                     #   records separated by NUL bytes
    [--repo <owner/repo>]            # default: $GITHUB_REPOSITORY
    [--fail-open-on-lookup-error]    # treat gh lookup failures as 'no label'
                                     #   rather than as exit-2. Use in local
                                     #   dev where gh may not be authenticated.

Generator-side usage (P4 in docs/postmortems/2026-05-18-project-2734-stall.md):
  call this script BEFORE submitting an AI-authored PR body and refuse to
  create the PR if exit code != 0.

Markdown note: PR bodies are scanned after masking fenced code blocks and
inline code spans so historical examples like `` `Fixes #2734` `` do not
false-positive. Commit messages remain plain-text scans.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# CLAUDE.md §19 auto-close keyword list (case-insensitive). Matches the
# exact wording GitHub uses to auto-close referenced issues on merge to the
# default branch — see https://docs.github.com/articles/closing-issues-using-keywords
# Keep this list IN SYNC with the §19 ban list in CLAUDE.md and the mirror
# in unattended_system_instructions.md; if GitHub adds a new keyword, both
# the docs and this list must be updated together.
AUTO_CLOSE_KEYWORDS = (
	"close",
	"closes",
	"closed",
	"fix",
	"fixes",
	"fixed",
	"resolve",
	"resolves",
	"resolved",
)

# The pattern catches both `#N` form and full GitHub URL form, which
# GitHub's auto-close logic treats equivalently. Three accepted shapes:
#   1. `keyword #N`                                          (same-repo short form)
#   2. `keyword owner/repo#N`                                (cross-repo short form)
#   3. `keyword https://github.com/owner/repo/issues/N`      (URL form)
# Colon forms like `Closes: #N` are intentionally NOT matched because
# GitHub does not auto-close on that syntax.
# Boundaries `(?<![\w-])` / `(?![\w-])` prevent matches inside words
# (e.g. "prefix" should not match "fix").
_KEYWORD_ALT = "|".join(AUTO_CLOSE_KEYWORDS)
AUTO_CLOSE_RE = re.compile(
	rf"(?i)(?<![\w-])(?P<keyword>{_KEYWORD_ALT})\s+"
	rf"(?:"
	rf"(?:(?P<issue_repo_short>[\w.-]+/[\w.-]+))?#(?P<issue_short>\d+)"
	rf"|"
	rf"https?://github\.com/(?P<issue_repo_url>[\w.-]+/[\w.-]+)/issues/(?P<issue_url>\d+)"
	rf")(?![\w-])"
)

# Label that marks an orchestrator tracking issue. Auto-close keywords
# against an issue with this label are forbidden because closing the
# tracking issue stops wave dispatch.
TRACKING_LABEL = "ai:orchestrator-tracking"
FENCED_CODE_RE = re.compile(r"^\s*(```+|~~~+)")
# Match inline code spans delimited by one or more backticks, reusing the
# exact same fence length for the closer so double-backtick spans like
# ``Fixes #2734`` are masked before keyword scanning.
INLINE_CODE_RE = re.compile(r"(?P<fence>`+)(?P<code>[^\n]*?)(?P=fence)")


class LintViolation:
	"""A single auto-close keyword that references an orchestrator-tracking issue."""

	def __init__(
		self,
		source: str,
		line_no: int,
		line: str,
		keyword: str,
		issue: int,
		issue_repo: str | None = None,
	):
		self.source = source
		self.line_no = line_no
		self.line = line.rstrip("\n")
		self.keyword = keyword
		self.issue = issue
		self.issue_repo = issue_repo

	def format(self) -> str:
		issue_ref = f"{self.issue_repo}#{self.issue}" if self.issue_repo else f"#{self.issue}"
		return (
			f"::error::[lint_pr_body_auto_close] {self.source}:line {self.line_no} — "
			f"auto-close keyword '{self.keyword}' references ai:orchestrator-tracking "
			f"issue {issue_ref}. On merge this would auto-close the tracking issue "
			f"and stop the orchestrator's wave dispatch (see CLAUDE.md §19 and "
			f"docs/postmortems/2026-05-18-project-2734-stall.md). Use 'Refs #' or "
			f"'Related to #' instead.\n"
			f"    Matched line: {self.line}"
		)


def _fetch_issue_labels_gh(repo: str, issue_number: int) -> list[str] | None:
	"""Return the label names for an issue via the `gh` CLI, or None on lookup
	failure. None means "could not determine"; the caller decides whether to
	treat that as fail-open or fail-closed.
	"""
	if not repo:
		return None
	cmd = [
		"gh", "issue", "view", str(issue_number),
		"--repo", repo,
		"--json", "labels",
		"--jq", "[.labels[].name]",
	]
	try:
		result = subprocess.run(
			cmd,
			capture_output=True,
			text=True,
			timeout=30,
			check=False,
		)
	except (subprocess.SubprocessError, OSError):
		return None
	if result.returncode != 0:
		return None
	out = (result.stdout or "").strip()
	if not out:
		return []
	import json as _json
	try:
		labels = _json.loads(out)
	except _json.JSONDecodeError:
		return None
	if not isinstance(labels, list):
		return None
	return [str(label) for label in labels]


def _scan_text(source: str, text: str, *, markdown: bool = False) -> list[tuple[int, str, str, str | None, int]]:
	"""Return a list of (line_no, line, keyword, referenced_repo, issue_number)
	tuples for every auto-close keyword match in `text`. line_no is 1-based.

	The regex has two named groups for the issue number — `issue_short`
	(for `#N` and `owner/repo#N` forms) and `issue_url` (for the full
	`https://github.com/.../issues/N` form). Exactly one is populated per
	match; we pick whichever fired. `referenced_repo` is None for same-repo
	short-form references and `owner/repo` for cross-repo short or URL
	forms so label lookup can target the actual referenced issue.
	"""
	matches: list[tuple[int, str, str, str | None, int]] = []
	in_fenced_code = False
	fence_char = ""
	fence_len = 0
	for line_no, raw_line in enumerate(text.splitlines(), start=1):
		line = raw_line
		if markdown:
			stripped = raw_line.lstrip()
			if in_fenced_code:
				if stripped.startswith(fence_char * fence_len):
					in_fenced_code = False
					fence_char = ""
					fence_len = 0
				continue
			fence_match = FENCED_CODE_RE.match(raw_line)
			if fence_match:
				fence_token = fence_match.group(1)
				in_fenced_code = True
				fence_char = fence_token[0]
				fence_len = len(fence_token)
				continue
			line = INLINE_CODE_RE.sub("", raw_line)
		for m in AUTO_CLOSE_RE.finditer(line):
			issue_str = m.group("issue_short") or m.group("issue_url")
			if not issue_str:
				continue
			referenced_repo = m.group("issue_repo_short") or m.group("issue_repo_url") or None
			matches.append((line_no, line, m.group("keyword"), referenced_repo, int(issue_str)))
	return matches


def _load_commit_messages(path: Path) -> list[tuple[int, str]]:
	"""Read NUL-separated commit messages; return [(record_no, message)]."""
	raw = path.read_bytes()
	# Strip a trailing NUL so a clean `--format=%B%x00` doesn't add an empty record.
	if raw.endswith(b"\x00"):
		raw = raw[:-1]
	records = raw.split(b"\x00") if raw else []
	return [(idx + 1, rec.decode("utf-8", errors="replace")) for idx, rec in enumerate(records)]


def lint(
	*,
	pr_body: str,
	commit_messages: list[tuple[int, str]],
	repo: str,
	fail_open_on_lookup_error: bool,
	label_lookup=None,
) -> tuple[list[LintViolation], list[str]]:
	"""Return (violations, lookup_errors). label_lookup is an injectable
	callable for tests (signature: (repo: str, issue_number: int) -> list[str] | None);
	default uses _fetch_issue_labels_gh.
	"""
	if label_lookup is None:
		label_lookup = _fetch_issue_labels_gh

	candidate_matches: list[tuple[str, int, str, str, str | None, int]] = []
	for line_no, line, keyword, referenced_repo, issue in _scan_text("PR body", pr_body, markdown=True):
		candidate_matches.append(("PR body", line_no, line, keyword, referenced_repo, issue))
	for rec_no, msg in commit_messages:
		for line_no, line, keyword, referenced_repo, issue in _scan_text(f"commit message #{rec_no}", msg):
			candidate_matches.append((f"commit message #{rec_no}", line_no, line, keyword, referenced_repo, issue))

	violations: list[LintViolation] = []
	lookup_errors: list[str] = []
	# Cache per-(repo, issue) lookups so a body with multiple references to the
	# same issue makes one gh call, not N (CLAUDE.md §15).
	issue_label_cache: dict[tuple[str, int], list[str]] = {}
	for source, line_no, line, keyword, referenced_repo, issue in candidate_matches:
		lookup_repo = referenced_repo or repo
		cache_key = (lookup_repo, issue)
		if cache_key in issue_label_cache:
			labels = issue_label_cache[cache_key]
		else:
			labels = label_lookup(lookup_repo, issue)
			if labels is not None:
				issue_label_cache[cache_key] = labels
		if labels is None:
			lookup_errors.append(
				f"could not fetch labels for {lookup_repo}#{issue} (matched in {source}:line {line_no}); "
				f"check gh auth and {lookup_repo!r} accessibility"
			)
			if fail_open_on_lookup_error:
				continue
			# Without fail-open, treat lookup failure as a violation candidate
			# only if we cannot prove the issue is NOT a tracking issue.
			# Surface as a lookup error which the caller maps to exit 2.
			continue
		if TRACKING_LABEL in labels:
			violations.append(LintViolation(source, line_no, line, keyword, issue, referenced_repo))
	return violations, lookup_errors


def main() -> int:
	parser = argparse.ArgumentParser(
		description="Lint PR title/body text for auto-close keywords against ai:orchestrator-tracking issues.",
	)
	parser.add_argument("--pr-body-file", required=True, type=Path,
		help="Path to the PR title/body text file.")
	parser.add_argument("--commit-messages-file", type=Path, default=None,
		help="Optional path to a file containing NUL-separated commit messages.")
	parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
		help="owner/repo for issue label lookups. Defaults to $GITHUB_REPOSITORY.")
	parser.add_argument("--fail-open-on-lookup-error", action="store_true",
		help="If a gh lookup fails, treat the issue as having no labels rather "
			"than as a hard error. Use in local dev where gh may not be authenticated.")
	args = parser.parse_args()

	if not args.pr_body_file.exists():
		print(f"::error::PR body file not found: {args.pr_body_file}", file=sys.stderr)
		return 2

	pr_body = args.pr_body_file.read_text(encoding="utf-8", errors="replace")
	commit_messages: list[tuple[int, str]] = []
	if args.commit_messages_file:
		if not args.commit_messages_file.exists():
			print(f"::error::commit messages file not found: {args.commit_messages_file}", file=sys.stderr)
			return 2
		commit_messages = _load_commit_messages(args.commit_messages_file)

	if not args.repo:
		print(
			"::error::no repo configured (pass --repo owner/repo or set GITHUB_REPOSITORY)",
			file=sys.stderr,
		)
		return 2

	violations, lookup_errors = lint(
		pr_body=pr_body,
		commit_messages=commit_messages,
		repo=args.repo,
		fail_open_on_lookup_error=args.fail_open_on_lookup_error,
	)

	for err in lookup_errors:
		print(f"::warning::[lint_pr_body_auto_close] {err}", file=sys.stderr)

	if violations:
		for v in violations:
			print(v.format(), file=sys.stderr)
		print(
			f"::error::[lint_pr_body_auto_close] {len(violations)} auto-close "
			f"keyword violation(s) against ai:orchestrator-tracking issues. "
			f"Replace 'Fixes/Closes/Resolves' with 'Refs' or 'Related to' for "
			f"these references and re-push.",
			file=sys.stderr,
		)
		return 1

	if lookup_errors and not args.fail_open_on_lookup_error:
		print(
			"::error::[lint_pr_body_auto_close] one or more issue label "
			"lookups failed; cannot prove the PR body is clean. Either fix "
			"the lookup error or re-run with --fail-open-on-lookup-error.",
			file=sys.stderr,
		)
		return 2

	# Quiet success.
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

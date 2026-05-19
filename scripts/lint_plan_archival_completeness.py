#!/usr/bin/env python3
"""Lint a PR that adds files under docs/completed/ for plan-archival completeness.

Project #2734's integration PR moved the plan to `docs/completed/` and
labelled the tracking issue `ai:merged` while 7 of 9 sub-issue checkboxes
in the tracking issue body remained unchecked (see
`docs/postmortems/2026-05-18-project-2734-stall.md` layer 4). The plan
file's presence in `docs/completed/` then became a load-bearing-but-
misleading signal: "this plan is done" when in fact most of it was
unshipped.

This lint enforces P6 from the postmortem: any PR that creates a
`docs/completed/<plan>.md` file must, for every `ai:orchestrator-tracking`
issue referenced from the PR body, either:

  A. Have all `[ ]` checkboxes in the tracking issue body checked
     (`[x]`) — i.e. the plan really is complete; OR
  B. Have a structured `## De-scoped phases` section in the PR body
     that explicitly lists every unshipped checkbox with a rationale.

Either A or B is sufficient; neither is fatal-by-omission alone. The
intent is to force the PR author to be explicit about scope when
archiving a plan, so the `docs/completed/` directory remains an
honest record.

Exit codes:
  0 — no `docs/completed/` files added; OR every referenced tracking
      issue passes A or B.
  1 — at least one tracking issue has unchecked sub-issues AND the PR
      body lacks the de-scope section. Each violation is surfaced as
      a structured `::error::[lint_plan_archival]` line.
  2 — usage error or unrecoverable lookup failure (gh CLI unavailable
      and `--fail-open-on-lookup-error` not set).

Usage:
  python3 scripts/lint_plan_archival_completeness.py \\
    --pr-body-file <path>                # required
    --added-files-file <path>            # required; one path per line
    [--repo <owner/repo>]                # default: $GITHUB_REPOSITORY
    [--fail-open-on-lookup-error]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TRACKING_LABEL = "ai:orchestrator-tracking"

# Matches both `#N` and full GitHub issue URL forms in PR bodies. We
# accept all reference forms — the §19 lint (lint_pr_body_auto_close.py)
# is responsible for forbidding auto-close keywords against tracking
# issues; here we just need to harvest every referenced issue number, then
# filter to tracking-labeled ones.
ISSUE_REF_RE = re.compile(
	r"(?:"
	r"(?<![\w-])#(?P<issue_short>\d+)(?![\w-])"
	r"|"
	r"https?://github\.com/[\w.-]+/[\w.-]+/issues/(?P<issue_url>\d+)"
	r")"
)

# Matches a GitHub markdown task list item. Capture the state ([ ] / [x] /
# [X]) and the rest of the line so we can name unchecked items in the
# error message.
CHECKBOX_RE = re.compile(r"^\s*-\s*\[(?P<state>[ xX])\]\s*(?P<text>.*)$")

# Signature for the explicit de-scope acknowledgement section in the PR
# body. The matched text must include a markdown heading (## or ###) and
# the literal phrase "De-scoped phases" — the comparison is case-
# insensitive so a contributor doesn't have to memorise capitalisation.
DESCOPE_HEADING_RE = re.compile(r"^\s{0,3}#{2,6}\s+de[- ]scoped\s+phases\b", re.IGNORECASE | re.MULTILINE)
SECTION_HEADING_RE = re.compile(r"^\s{0,3}#{2,6}\s+\S")
LIST_ITEM_RE = re.compile(r"^\s{0,3}(?:[-*+]\s+\S|\d+\.\s+\S)")

# Files under this directory trigger the check.
ARCHIVED_PLAN_DIR = "docs/completed/"


def _fetch_issue_via_gh(repo: str, issue_number: int) -> dict | None:
	"""Return {'labels': [...], 'body': '...'} for an issue, or None on failure."""
	if not repo:
		return None
	cmd = [
		"gh", "issue", "view", str(issue_number),
		"--repo", repo,
		"--json", "labels,body",
	]
	try:
		result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
	except (subprocess.SubprocessError, OSError):
		return None
	if result.returncode != 0:
		return None
	try:
		data = json.loads(result.stdout or "{}")
	except json.JSONDecodeError:
		return None
	return {
		"labels": [label["name"] for label in data.get("labels", []) if isinstance(label, dict) and "name" in label],
		"body": data.get("body", "") or "",
	}


def _extract_referenced_issues(pr_body: str) -> list[int]:
	"""Unique issue numbers referenced in the PR body, in order of first appearance."""
	seen: set[int] = set()
	ordered: list[int] = []
	for m in ISSUE_REF_RE.finditer(pr_body):
		issue_str = m.group("issue_short") or m.group("issue_url")
		if not issue_str:
			continue
		n = int(issue_str)
		if n not in seen:
			seen.add(n)
			ordered.append(n)
	return ordered


def _enumerate_unchecked_boxes(issue_body: str) -> list[str]:
	"""Return the text of every unchecked task list item in the issue body."""
	unchecked: list[str] = []
	for line in issue_body.splitlines():
		m = CHECKBOX_RE.match(line)
		if m and m.group("state") == " ":
			text = (m.group("text") or "").strip()
			unchecked.append(text or "<blank checkbox item>")
	return unchecked


def _has_descope_section_content(pr_body: str) -> bool:
	"""Return True when the de-scope heading exists and is followed by at
	least one non-empty list item before the next markdown heading."""
	match = DESCOPE_HEADING_RE.search(pr_body)
	if match is None:
		return False
	for line in pr_body[match.end():].splitlines():
		if SECTION_HEADING_RE.match(line):
			break
		if LIST_ITEM_RE.match(line):
			return True
	return False


def lint(
	*,
	pr_body: str,
	added_files: list[str],
	repo: str,
	fail_open_on_lookup_error: bool,
	issue_fetcher=None,
) -> tuple[list[str], list[str]]:
	"""Return (violations, lookup_errors). issue_fetcher is an injectable
	callable for tests (signature: (repo, issue_number) -> dict | None);
	defaults to _fetch_issue_via_gh.
	"""
	if issue_fetcher is None:
		issue_fetcher = _fetch_issue_via_gh

	archived = [f for f in added_files if f.startswith(ARCHIVED_PLAN_DIR)]
	if not archived:
		return [], []  # Not an archival PR; nothing to check.

	referenced_issues = _extract_referenced_issues(pr_body)
	if not referenced_issues:
		# The PR archives a plan but references no issues. Surface as a
		# violation — without a Refs #N the operator cannot trace the
		# archival back to a project.
		return (
			[
				"PR adds files under docs/completed/ but the PR body references no "
				"issues. Include `Refs #N` for the tracking issue this plan belonged "
				"to, so the archival can be audited against the issue's checkboxes. "
				f"Archived files: {', '.join(archived)}"
			],
			[],
		)

	has_descope_section = _has_descope_section_content(pr_body)

	violations: list[str] = []
	lookup_errors: list[str] = []
	for issue_num in referenced_issues:
		issue = issue_fetcher(repo, issue_num)
		if issue is None:
			lookup_errors.append(
				f"could not fetch issue #{issue_num} from {repo!r}; check gh auth."
			)
			continue
		if TRACKING_LABEL not in issue["labels"]:
			continue  # Not a tracking issue; not in scope for this lint.
		unchecked = _enumerate_unchecked_boxes(issue["body"])
		if not unchecked:
			continue  # Tracking issue is fully ticked — scenario A satisfied.
		if has_descope_section:
			continue  # Scenario B satisfied — PR body acknowledges the scope.
		# Neither A nor B — violation.
		preview = "\n".join(f"      - {item[:100]}" for item in unchecked[:10])
		more = f"\n      … and {len(unchecked) - 10} more" if len(unchecked) > 10 else ""
		violations.append(
			f"Tracking issue #{issue_num} has {len(unchecked)} unchecked sub-issue "
			f"checkbox(es), but PR archives plan to docs/completed/ without a "
			f"non-empty "
			f"explicit `## De-scoped phases` section in the body. Either tick the "
			f"checkboxes on issue #{issue_num} before merging, or add a "
			f"`## De-scoped phases` section to the PR body that lists every "
			f"unshipped phase with rationale.\n"
			f"    Archived files: {', '.join(archived)}\n"
			f"    Unchecked items:\n{preview}{more}"
		)
	return violations, lookup_errors


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--pr-body-file", required=True, type=Path)
	parser.add_argument("--added-files-file", required=True, type=Path,
		help="Path to a file containing one added-file path per line.")
	parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
	parser.add_argument("--fail-open-on-lookup-error", action="store_true")
	args = parser.parse_args()

	if not args.pr_body_file.exists():
		print(f"::error::PR body file not found: {args.pr_body_file}", file=sys.stderr)
		return 2
	if not args.added_files_file.exists():
		print(f"::error::added files file not found: {args.added_files_file}", file=sys.stderr)
		return 2

	pr_body = args.pr_body_file.read_text(encoding="utf-8", errors="replace")
	added_files = [line.strip() for line in args.added_files_file.read_text(encoding="utf-8").splitlines() if line.strip()]

	if not args.repo:
		print("::error::no repo (pass --repo or set GITHUB_REPOSITORY)", file=sys.stderr)
		return 2

	violations, lookup_errors = lint(
		pr_body=pr_body,
		added_files=added_files,
		repo=args.repo,
		fail_open_on_lookup_error=args.fail_open_on_lookup_error,
	)

	for err in lookup_errors:
		print(f"::warning::[lint_plan_archival] {err}", file=sys.stderr)

	if violations:
		for v in violations:
			print(f"::error::[lint_plan_archival] {v}", file=sys.stderr)
		print(
			f"::error::[lint_plan_archival] {len(violations)} plan-archival "
			f"completeness violation(s). See "
			f"docs/postmortems/2026-05-18-project-2734-stall.md (layer 4) for "
			f"why archival without scope acknowledgement is forbidden.",
			file=sys.stderr,
		)
		return 1

	if lookup_errors and not args.fail_open_on_lookup_error:
		print("::error::[lint_plan_archival] lookup errors blocked the check", file=sys.stderr)
		return 2

	return 0


if __name__ == "__main__":
	raise SystemExit(main())

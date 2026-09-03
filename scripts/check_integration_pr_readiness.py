#!/usr/bin/env python3
"""Check whether an orchestrator integration PR is ready to merge based on
the tracking issue's sub-issue checkbox state.

P3 from docs/postmortems/2026-05-18-project-2734-stall.md. Project #2734's
integration PR (#2750) was eagerly opened (the orchestrator's
continuous-drift-resolution design) and later squash-merged externally
with only Wave 1 content; Waves 2-7 of the tracking-issue body were still
unchecked at merge time. Without a gate, any human (or another Claude
session) can ship a partial integration PR as if it were complete.

This script reads the tracking issue's body, counts unticked `[ ]`
sub-issue checkboxes, and posts a commit status of:
  - success when 0 unchecked items, OR when an explicit override
    label `ai:override-incomplete-merge` is applied to the integration
    PR (auditable escape valve).
  - failure when >0 unchecked items, OR when the tracking issue body
    contains no parseable checkbox items, AND no override label.

The integration PR's head ref is the input — by convention orchestrator
integration branches are `orchestrator/project-<N>`. The script derives
the tracking issue number from the branch name. Non-matching refs get a
no-op success status so the required-check context exists on every PR
targeting the protected branch.

Exit codes:
  0 — readiness result emitted and, when not in `--dry-run`, the
      corresponding commit status POST succeeded.
  1 — the script could not fetch the tracking issue, could not post the
      required commit status, or hit another unrecoverable error.
  2 — usage error.

Usage:
  python3 scripts/check_integration_pr_readiness.py \\
    --head-ref orchestrator/project-2734 \\
    --head-sha <commit-sha-to-attach-status-to> \\
    [--pr-labels-file <path>]               # newline-separated labels on the PR
    [--tracking-body-file <path>]           # optional body override; skips gh issue fetch body lookup
    [--tracking-labels-file <path>]         # optional newline-separated tracking labels override
    [--repo <owner/repo>]                   # default: $GITHUB_REPOSITORY
    [--dry-run]                             # print status, don't post
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONTEXT = "orchestrator/integration-pr-not-ready"
TRACKING_LABEL = "ai:orchestrator-tracking"
OVERRIDE_LABEL = "ai:override-incomplete-merge"

BRANCH_RE = re.compile(r"^orchestrator/project-(?P<n>\d+)$")
CHECKBOX_RE = re.compile(r"^\s*-\s*\[(?P<state>[ xX])\]\s*(?P<text>.*)$")


def _derive_tracking_issue(head_ref: str) -> int | None:
	m = BRANCH_RE.match(head_ref.strip())
	if not m:
		return None
	return int(m.group("n"))


def _fetch_issue(repo: str, n: int) -> dict | None:
	cmd = ["gh", "issue", "view", str(n), "--repo", repo, "--json", "labels,body"]
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


def _count_checkboxes(body: str) -> tuple[list[str], int]:
	"""Return (unchecked_item_texts, total_count)."""
	unchecked: list[str] = []
	total = 0
	for line in body.splitlines():
		m = CHECKBOX_RE.match(line)
		if m:
			total += 1
			if m.group("state") == " ":
				text = (m.group("text") or "").strip()
				unchecked.append(text or "<blank checkbox item>")
	return unchecked, total


def _read_labels_file(path: Path | None) -> list[str]:
	if path is None or not path.exists():
		return []
	return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _post_commit_status(
	repo: str,
	sha: str,
	state: str,
	description: str,
	target_url: str = "",
) -> bool:
	"""POST a commit status via gh api. Returns True on HTTP 2xx."""
	args = [
		"gh", "api", "-X", "POST",
		f"repos/{repo}/statuses/{sha}",
		"-f", f"state={state}",
		"-f", f"context={CONTEXT}",
		"-f", f"description={description[:140]}",
	]
	if target_url:
		args += ["-f", f"target_url={target_url}"]
	try:
		result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
	except (subprocess.SubprocessError, OSError) as exc:
		print(f"::warning::[integration-pr-readiness] commit status POST failed: {exc}", file=sys.stderr)
		return False
	if result.returncode != 0:
		detail = (result.stderr or result.stdout or "").strip() or f"gh api exited {result.returncode}"
		print(f"::warning::[integration-pr-readiness] commit status POST failed: {detail}", file=sys.stderr)
		return False
	return True


def _post_commit_status_or_error(
	repo: str,
	sha: str,
	state: str,
	description: str,
	target_url: str = "",
) -> bool:
	if _post_commit_status(repo, sha, state, description, target_url):
		return True
	print(
		"::error::[integration-pr-readiness] failed to post readiness commit status; required status context may be missing or stale",
		file=sys.stderr,
	)
	return False


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--head-ref", required=True)
	parser.add_argument("--head-sha", required=True)
	parser.add_argument("--pr-labels-file", type=Path, default=None,
		help="Path to a newline-separated file of label names on the PR.")
	parser.add_argument("--tracking-body-file", type=Path, default=None,
		help="Optional path to the tracking issue body to evaluate instead of fetching it via gh.")
	parser.add_argument("--tracking-labels-file", type=Path, default=None,
		help="Optional path to newline-separated tracking-issue labels to evaluate instead of fetching them via gh.")
	parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
	parser.add_argument("--dry-run", action="store_true")
	args = parser.parse_args()

	if not args.repo:
		print("::error::no repo (pass --repo or set GITHUB_REPOSITORY)", file=sys.stderr)
		return 2

	tracking_num = _derive_tracking_issue(args.head_ref)
	if tracking_num is None:
		# Not an orchestrator integration branch — no readiness check
		# applies. Post a neutral success so the required-check context
		# still exists on non-orchestrator PRs when branch protection
		# marks this status required. If that POST fails, return non-zero
		# so the workflow does not silently pass without the required
		# status context.
		desc = f"head ref {args.head_ref!r} is not an orchestrator/project-* branch — readiness check does not apply"
		print(f"::notice::{desc}")
		if not args.dry_run and not _post_commit_status_or_error(args.repo, args.head_sha, "success", desc):
			return 1
		return 0

	tracking_url = f"https://github.com/{args.repo}/issues/{tracking_num}"

	pr_labels = _read_labels_file(args.pr_labels_file)

	issue: dict | None = None
	if args.tracking_body_file or args.tracking_labels_file:
		issue = {"labels": _read_labels_file(args.tracking_labels_file), "body": ""}
		if args.tracking_body_file and args.tracking_body_file.exists():
			issue["body"] = args.tracking_body_file.read_text(encoding="utf-8")

	if issue is None or not issue["body"] or not issue["labels"]:
		fetched_issue = _fetch_issue(args.repo, tracking_num)
		if fetched_issue is None:
			desc = f"could not fetch tracking issue #{tracking_num}; cannot assess readiness"
			print(f"::warning::{desc}")
			if not args.dry_run and not _post_commit_status_or_error(args.repo, args.head_sha, "error", desc, tracking_url):
				return 1
			return 1
		if issue is None:
			issue = fetched_issue
		else:
			if not issue["labels"]:
				issue["labels"] = fetched_issue["labels"]
			if not issue["body"]:
				issue["body"] = fetched_issue["body"]

	if TRACKING_LABEL not in issue["labels"]:
		# Branch matches orchestrator/project-N but the linked issue is
		# not actually a tracking issue. Surface as neutral success.
		desc = f"issue #{tracking_num} is not labeled {TRACKING_LABEL}; readiness check does not apply"
		print(f"::notice::{desc}")
		if not args.dry_run and not _post_commit_status_or_error(args.repo, args.head_sha, "success", desc, tracking_url):
			return 1
		return 0

	unchecked, total = _count_checkboxes(issue["body"])

	if OVERRIDE_LABEL in pr_labels:
		desc = (
			f"override label {OVERRIDE_LABEL!r} applied: "
			f"merging despite {len(unchecked)}/{total} unchecked sub-issues on #{tracking_num}"
		)
		print(f"::notice::{desc}")
		if not args.dry_run and not _post_commit_status_or_error(args.repo, args.head_sha, "success", desc, tracking_url):
			return 1
		return 0

	if total == 0:
		# Fail closed when the tracking issue body has no parseable task
		# list items. A vacuous success would let an incomplete
		# integration PR merge with zero completeness signal.
		desc = f"tracking issue #{tracking_num} has no checkbox items in its body; readiness check cannot verify completeness"
		print(f"::error::[integration-pr-readiness] {desc}", file=sys.stderr)
		if not args.dry_run and not _post_commit_status_or_error(args.repo, args.head_sha, "failure", desc, tracking_url):
			return 1
		return 0

	if not unchecked:
		desc = f"all {total} sub-issue(s) on #{tracking_num} are ticked — integration PR is ready"
		print(f"[ready] {desc}")
		if not args.dry_run and not _post_commit_status_or_error(args.repo, args.head_sha, "success", desc, tracking_url):
			return 1
		return 0

	# Not ready: emit failure status + structured log.
	preview = ", ".join(item[:60] for item in unchecked[:5])
	if len(unchecked) > 5:
		preview += f", … +{len(unchecked) - 5} more"
	desc = f"{len(unchecked)}/{total} sub-issues on #{tracking_num} still unchecked: {preview}"
	print(f"::warning::[integration-pr-readiness] {desc}", file=sys.stderr)
	print(
		f"::notice::To merge anyway (rare; e.g. de-scoping the remaining work), "
		f"apply the {OVERRIDE_LABEL!r} label to this PR.",
		file=sys.stderr,
	)
	if not args.dry_run and not _post_commit_status_or_error(args.repo, args.head_sha, "failure", desc, tracking_url):
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

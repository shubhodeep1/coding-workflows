#!/usr/bin/env python3
"""Recover cancelled CI from the review sweep's open, non-draft PR snapshot.

Input: CI_CANCELLED_PR_SNAPSHOT (the sweep's JSON array); output: one
CI_CANCELLED_RERUN decision per PR. Fail closed on incomplete evidence.
At most four repository-wide CI run-list GETs per tick: one completed
(`status=completed&per_page=100`) and one each for queued, in_progress,
and pending. There are no per-PR run reads. The sweep's existing cache
contains *review* workflow runs, not CI runs, so it cannot answer this query.
Eligible runs get one rerun-failed-jobs POST, with no retry or full-rerun
fallback. The bounded completed window cannot establish an absolute
once-per-head cap if an older, distinct run ID has fallen out of view.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path


SOURCE_REPO = "shubhodeep1/coding-workflows"
SHA_PATTERN = re.compile(r"[a-fA-F0-9]{40}\Z")
ACTIVE_STATUSES = ("queued", "in_progress", "pending")


def positive_number(value: object) -> bool:
	return type(value) is int and value > 0


def decision(pr: object, head: object, run: object, action: str, reason: str) -> None:
	# Never echo untrusted snapshot or API fields without validation.
	pr_text = str(pr) if positive_number(pr) else "none"
	head_text = head if isinstance(head, str) and SHA_PATTERN.fullmatch(head) else "none"
	run_text = str(run) if positive_number(run) else "none"
	print(f"CI_CANCELLED_RERUN pr={pr_text} head={head_text} run={run_text} action={action} reason={reason}")


def list_runs(repo: str, status: str) -> list[dict]:
	# Fixed per-status reads; the review-run snapshot is not a CI snapshot.
	result = subprocess.run(
		["gh", "api", "-X", "GET", f"repos/{repo}/actions/workflows/ci.yml/runs",
			"-f", f"status={status}", "-f", "per_page=100"],
		capture_output=True, text=True, timeout=30, check=False,
	)
	if result.returncode != 0:
		raise ValueError("api_unavailable")
	try:
		payload = json.loads(result.stdout)
	except ValueError as exc:
		raise ValueError("invalid_listing") from exc
	if not isinstance(payload, dict):
		raise ValueError("invalid_listing")
	runs = payload.get("workflow_runs")
	count = payload.get("total_count")
	if not isinstance(runs, list) or type(count) is not int or count < 0:
		raise ValueError("invalid_listing")
	# A full page, even with an apparently complete count, offers no room
	# for an in-flight run to have entered the list during the API read.
	if count != len(runs) or len(runs) >= 100:
		raise ValueError("listing_truncated")
	for run in runs:
		if not isinstance(run, dict) or not positive_number(run.get("id")) \
				or run.get("status") != status or run.get("event") not in ("pull_request", "push") \
				or not isinstance(run.get("head_sha"), str) or not SHA_PATTERN.fullmatch(run["head_sha"]) \
				or not isinstance(run.get("pull_requests"), list) \
				or run.get("path") != ".github/workflows/ci.yml":
			raise ValueError("invalid_listing")
		if status == "completed":
			if not positive_number(run.get("run_attempt")) or not isinstance(run.get("conclusion"), str) \
					or not isinstance(run.get("created_at"), str):
				raise ValueError("invalid_listing")
			try:
				created_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
			except ValueError as exc:
				raise ValueError("invalid_listing") from exc
			if created_at.tzinfo is None:
				raise ValueError("invalid_listing")
		for association in run["pull_requests"]:
			if not isinstance(association, dict) or not positive_number(association.get("number")) \
					or not isinstance(association.get("head"), dict) \
					or not isinstance(association["head"].get("sha"), str) \
					or not SHA_PATTERN.fullmatch(association["head"]["sha"]):
				raise ValueError("invalid_listing")
	return runs


def associated(run: dict, pr_number: int, sha: str) -> bool:
	return run["event"] == "pull_request" and any(
		item.get("number") == pr_number
		and isinstance(item.get("head"), dict)
		and item["head"].get("sha") == sha
		for item in run["pull_requests"]
	)


def rerun_failed_jobs(repo: str, run_id: int) -> bool:
	try:
		result = subprocess.run(
			["gh", "api", "-i", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs"],
			capture_output=True, text=True, timeout=30, check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		return False
	# A refused startup_failure (often no failed job exists) is not a
	# license to rerun the entire workflow. Never print the response body.
	return result.returncode == 0 and bool(re.match(r"HTTP/\S+ 201\b", result.stdout))


def process(prs: list, repo: str, enabled: bool, dry_run: bool, head_filter: str) -> None:
	# One malformed item makes the enumerated set incomplete: do not infer
	# that the remaining entries are the only open PRs on a shared head.
	if any(not isinstance(pr, dict) or not positive_number(pr.get("number"))
			or not isinstance(pr.get("head_sha"), str) or not SHA_PATTERN.fullmatch(pr["head_sha"])
			or not isinstance(pr.get("head_ref"), str) or not isinstance(pr.get("title"), str)
			or not isinstance(pr.get("body"), str) for pr in prs):
		for pr in prs:
			if isinstance(pr, dict):
				decision(pr.get("number"), pr.get("head_sha"), None, "skip", "invalid_snapshot")
			else:
				decision(None, None, None, "skip", "invalid_snapshot")
		return
	eligible = []
	for pr in prs:
		number, sha, ref = pr.get("number"), pr.get("head_sha"), pr.get("head_ref")
		if head_filter and head_filter not in ref:
			decision(number, sha, None, "skip", "filtered")
			continue
		if "[skip ai]" in pr["title"] + " " + pr["body"]:
			decision(number, sha, None, "skip", "skip_ai")
			continue
		if not enabled:
			decision(number, sha, None, "skip", "disabled")
			continue
		if repo != SOURCE_REPO:
			decision(number, sha, None, "skip", "unsupported_repo")
			continue
		eligible.append(pr)

	if not eligible:
		return
	try:
		# No CI listing is shared with the review-family active-run cache.
		completed = list_runs(repo, "completed")
		active = [run for status in ACTIVE_STATUSES for run in list_runs(repo, status)]
	except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
		reason = str(exc) if isinstance(exc, ValueError) else "api_unavailable"
		for pr in eligible:
			decision(pr["number"], pr["head_sha"], None, "skip", reason)
		return

	processed_heads: set[str] = set()
	for pr in eligible:
		number, sha = pr["number"], pr["head_sha"]
		if sha in processed_heads:
			decision(number, sha, None, "skip", "shared_head")
			continue
		matches = [run for run in completed if associated(run, number, sha)]
		if not matches:
			decision(number, sha, None, "skip", "no_attributable_run")
			continue
		processed_heads.add(sha)
		# created_at orders distinct run IDs; run_attempt belongs to an ID,
		# not the head. A visible retry on *any* ID blocks another POST.
		newest = max(matches, key=lambda run: (
			datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).timestamp(), run["id"]
		))
		run_id = newest["id"]
		if any(run["run_attempt"] > 1 and any(item["head"]["sha"] == sha
				for item in run["pull_requests"]) for run in completed):
			decision(number, sha, run_id, "skip", "already_retried")
		elif newest["conclusion"] not in ("cancelled", "startup_failure"):
			decision(number, sha, run_id, "skip", "other_conclusion")
		elif any(associated(run, number, sha) or run["head_sha"] == sha
				or (run["event"] == "pull_request" and run.get("head_branch") == pr["head_ref"])
				for run in active):
			decision(number, sha, run_id, "skip", "active_run")
		elif dry_run:
			decision(number, sha, run_id, "skip", "dry_run")
		elif rerun_failed_jobs(repo, run_id):
			decision(number, sha, run_id, "rerun", "cancelled_ci")
		else:
			decision(number, sha, run_id, "skip", "rerun_refused")


def main() -> None:
	path = os.environ.get("CI_CANCELLED_PR_SNAPSHOT", "")
	try:
		prs = json.loads(Path(path).read_text(encoding="utf-8"))
		if not isinstance(prs, list):
			raise ValueError("invalid snapshot")
	except (OSError, ValueError):
		decision(None, None, None, "skip", "invalid_snapshot")
		return
	process(
		prs, os.environ.get("REPOSITORY", ""),
		os.environ.get("CI_CANCELLED_AUTO_RERUN_ENABLED", "true").lower() == "true",
		os.environ.get("DRY_RUN", "false").lower() == "true",
		os.environ.get("HEAD_REF_FILTER", ""),
	)


if __name__ == "__main__":
	main()

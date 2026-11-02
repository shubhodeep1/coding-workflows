#!/usr/bin/env python3
"""Recover cancelled CI from the review sweep's open, non-draft PR snapshot.

Input: CI_CANCELLED_PR_SNAPSHOT (the sweep's JSON array); output: one
CI_CANCELLED_RERUN decision per PR. Fail closed on incomplete evidence.
At most four repository-wide CI run-list GETs per tick: one bounded completed
window (`status=completed&per_page=100`) and one each for queued, in_progress,
and pending, plus one paginated open-PR listing immediately before posting.
The sweep's earlier PR snapshot cannot prove a head is still current after
the review-dispatch loop, and its run cache contains *review* workflow runs,
not CI runs. The only per-run reads classify a `failure` run that passed every
other guard as a runner shutdown (issue #7109): one attempt-1 jobs GET plus at
most MAX_FAILED_JOBS_CLASSIFIED check-run annotation GETs per classified run,
and at most MAX_FAILURE_CLASSIFICATIONS_PER_TICK classified runs per tick
(worst case 55 extra GETs per tick, none when no PR head has a failed CI run).
Eligible runs get one rerun-failed-jobs POST, with no retry or full-rerun
fallback. The bounded completed window cannot establish an absolute
once-per-head cap if an older, distinct run ID has fallen out of view.
A failed job that ran no steps and whose only failure annotations are
GitHub's "Unable to resolve action ..." text (issue #8471) is also re-run
once (reason `action_resolution`), using the same reads and no extra API
calls; CI_ACTION_RESOLUTION_RERUN_ENABLED=false turns that class off.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


SOURCE_REPO = "shubhodeep1/coding-workflows"
SHA_PATTERN = re.compile(r"[a-fA-F0-9]{40}\Z")
ACTIVE_STATUSES = ("queued", "in_progress", "pending")
# Runner-shutdown classification (issue #7109). The runner writes this text as
# an `##[error]` line, which GitHub records as a failure annotation on the job.
RUNNER_SHUTDOWN_PREFIX = "The runner has received a shutdown signal"
# Action-resolution classification (issues #8470 / #8471, run 41054179361):
# a job that failed before any step ran because GitHub could not resolve an
# action tag that resolved in the run's other jobs. Used with .fullmatch()
# on the stripped message, which gives the issue's ^...$ anchoring.
ACTION_RESOLUTION_MESSAGE = re.compile(
	r"Unable to resolve action `[^`]+`, unable to find version `[^`]+`"
	r"(\. Unable to resolve action `[^`]+`, unable to find version `[^`]+`)*\.?"
)
# ci.yml's aggregate job keeps the id `lint` (CLAUDE.md §6) and fails with
# `::error::CI jobs that did not succeed: <key>, <key>` when a needed job fails.
CI_AGGREGATE_JOB_NAME = "lint"
CI_AGGREGATE_MESSAGE = re.compile(r"CI jobs that did not succeed: (.+)\Z")
GENERIC_STEP_FAILURE = "Process completed with exit code 1."
# The only other failure annotation a real runner shutdown leaves on the job.
SHUTDOWN_COMPANION_MESSAGE = "The operation was canceled."
CHECK_RUN_URL_PATTERN = re.compile(r"/check-runs/([1-9][0-9]*)\Z")
MAX_FAILED_JOBS_CLASSIFIED = 10
MAX_FAILURE_CLASSIFICATIONS_PER_TICK = 5
# Job conclusions that are not, by themselves, evidence of a real failure.
BENIGN_JOB_CONCLUSIONS = ("success", "skipped", "neutral", "cancelled", "failure")


def positive_number(value: object) -> bool:
	return type(value) is int and value > 0


def action_resolution_rerun_enabled(raw: str | None) -> bool:
	# Off switch for the action-resolution class only (issue #8471). Unset,
	# empty or any-case `true` keeps it on; `false` turns it off; any other
	# value turns it off and warns. The raw value is never echoed.
	value = (raw or "").lower()
	if value in ("", "true"):
		return True
	if value != "false":
		print("::warning::CI_ACTION_RESOLUTION_RERUN_ENABLED is not true or false; "
			"action-resolution CI re-runs are off", file=sys.stderr)
	return False


def job_ran_no_steps(job: dict) -> bool:
	# Fail closed: a missing or malformed `steps` value is not evidence
	# that the job ran no steps, so today's `genuine` result stands.
	steps = job.get("steps")
	if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
		return False
	return not any(step.get("conclusion") in ("success", "failure") for step in steps)


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
	# Completed history is deliberately bounded; active runs must be complete
	# or we cannot establish that no other run for the head is still running.
	if len(runs) > 100 or count < len(runs) or (status != "completed" and (count != len(runs) or len(runs) == 100)) \
			or (status == "completed" and len(runs) < 100 and count != len(runs)):
		raise ValueError("listing_truncated")
	for run in runs:
		if not isinstance(run, dict) or not positive_number(run.get("id")) \
				or run.get("status") != status or run.get("event") not in ("pull_request", "push") \
				or not isinstance(run.get("head_sha"), str) or not SHA_PATTERN.fullmatch(run["head_sha"]) \
				or not isinstance(run.get("pull_requests"), list) \
				or not isinstance(run.get("path"), str) \
				or run["path"].split("@", 1)[0] != ".github/workflows/ci.yml":
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
	return runs


def associated(run: dict, pr_number: int, sha: str) -> bool:
	return run["event"] == "pull_request" and any(
		isinstance(item, dict) and positive_number(item.get("number")) and item["number"] == pr_number
		and isinstance(item.get("head"), dict)
		and item["head"].get("sha") == sha
		for item in run["pull_requests"]
	)


def current_pr_heads(repo: str) -> dict[int, str]:
	# The sweep's earlier /pulls listing predates review dispatch; its
	# review-run cache has no live PR heads. Refresh once for all PRs,
	# not per PR; a failed/partial page cannot authorize a rerun.
	result = subprocess.run(
		["gh", "api", "--paginate", "--slurp", "-X", "GET",
			f"repos/{repo}/pulls?state=open&per_page=100"],
		capture_output=True, text=True, timeout=30, check=False,
	)
	if result.returncode != 0:
		raise ValueError("api_unavailable")
	try:
		pages = json.loads(result.stdout)
	except ValueError as exc:
		raise ValueError("invalid_pr_listing") from exc
	if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
		raise ValueError("invalid_pr_listing")
	heads: dict[int, str] = {}
	for page in pages:
		for item in page:
			if not isinstance(item, dict) or not positive_number(item.get("number")) \
					or not isinstance(item.get("head"), dict) \
					or not isinstance(item["head"].get("sha"), str) \
					or not SHA_PATTERN.fullmatch(item["head"]["sha"]) \
					or type(item.get("draft")) is not bool or item["number"] in heads:
				raise ValueError("invalid_pr_listing")
			if not item["draft"]:
				heads[item["number"]] = item["head"]["sha"]
	return heads


def skip_ai_body(body: str) -> bool:
	fence = ""
	for line in body.splitlines():
		line = line.removesuffix("\r")
		if fence:
			closing = re.fullmatch(r" {0,3}(`{3,}|~{3,})[ \t]*", line)
			if closing and closing[1][0] == fence[0] and len(closing[1]) >= len(fence):
				fence = ""
			continue
		opened = re.match(r"[ \t]*(`{3,}|~{3,})", line)
		if opened:
			fence = opened[1]
		elif re.fullmatch(r" {0,3}\[skip ai\][ \t]*", line):
			return True
	return False


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



def list_run_jobs(repo: str, run_id: int) -> list[dict]:
	# Attempt-scoped: `already_retried` has ruled out any attempt >= 2.
	result = subprocess.run(
		["gh", "api", "-X", "GET", f"repos/{repo}/actions/runs/{run_id}/attempts/1/jobs",
			"-f", "per_page=100"],
		capture_output=True, text=True, timeout=30, check=False,
	)
	if result.returncode != 0:
		raise ValueError("jobs_unreadable")
	try:
		payload = json.loads(result.stdout)
	except ValueError as exc:
		raise ValueError("jobs_unreadable") from exc
	if not isinstance(payload, dict):
		raise ValueError("jobs_unreadable")
	jobs = payload.get("jobs")
	count = payload.get("total_count")
	if not isinstance(jobs, list) or type(count) is not int or count != len(jobs) or len(jobs) >= 100:
		raise ValueError("jobs_unreadable")
	for job in jobs:
		if not isinstance(job, dict) or not positive_number(job.get("id")) \
				or not isinstance(job.get("name"), str) or job.get("status") != "completed" \
				or not isinstance(job.get("conclusion"), str) \
				or not isinstance(job.get("check_run_url"), str) \
				or not CHECK_RUN_URL_PATTERN.search(job["check_run_url"]):
			raise ValueError("jobs_unreadable")
	return jobs


def failure_annotations(repo: str, check_run_id: int) -> list[dict]:
	result = subprocess.run(
		["gh", "api", "-X", "GET", f"repos/{repo}/check-runs/{check_run_id}/annotations",
			"-f", "per_page=100"],
		capture_output=True, text=True, timeout=30, check=False,
	)
	if result.returncode != 0:
		raise ValueError("annotations_unreadable")
	try:
		payload = json.loads(result.stdout)
	except ValueError as exc:
		raise ValueError("annotations_unreadable") from exc
	if not isinstance(payload, list) or len(payload) >= 100 or any(
			not isinstance(item, dict) or not isinstance(item.get("message"), str)
			or not isinstance(item.get("annotation_level"), str) for item in payload):
		raise ValueError("annotations_unreadable")
	return [item for item in payload if item["annotation_level"] == "failure"]


def classify_runner_shutdown(repo: str, run_id: int, action_resolution_enabled: bool = True) -> str:
	"""Return "shutdown", "action_resolution", "genuine", or "unverified".

	Fail closed: only a run whose every failed job was killed by a runner
	shutdown or, when enabled, failed before any step ran with only
	"Unable to resolve action" annotations (or is the `lint` aggregate
	reporting only such jobs) counts. "action_resolution" means every
	non-lint failed job is in that class; a mix with shutdown jobs is
	"shutdown". Annotation text is never logged; it can be written by a
	PR's own steps.
	"""
	try:
		jobs = list_run_jobs(repo, run_id)
		if any(job["conclusion"] not in BENIGN_JOB_CONCLUSIONS for job in jobs):
			return "genuine"
		failed = [job for job in jobs if job["conclusion"] == "failure"]
		if not failed or len(failed) > MAX_FAILED_JOBS_CLASSIFIED:
			return "unverified"
		shutdown_ids: set[int] = set()
		action_resolution_ids: set[int] = set()
		aggregate_annotations: list[list[dict]] = []
		for job in failed:
			check_run_id = int(CHECK_RUN_URL_PATTERN.search(job["check_run_url"])[1])
			annotations = failure_annotations(repo, check_run_id)
			if job["name"] == CI_AGGREGATE_JOB_NAME:
				aggregate_annotations.append(annotations)
			elif any(item["message"].startswith(RUNNER_SHUTDOWN_PREFIX) for item in annotations) \
					and all(item["message"].startswith(RUNNER_SHUTDOWN_PREFIX)
						or item["message"].strip() == SHUTDOWN_COMPANION_MESSAGE for item in annotations):
				# Any other failure annotation (a failed step's exit code, a
				# test error) means the job also failed for real.
				shutdown_ids.add(job["id"])
			elif action_resolution_enabled and job_ran_no_steps(job) and annotations \
					and all(ACTION_RESOLUTION_MESSAGE.fullmatch(item["message"].strip())
						for item in annotations):
				# Transient action resolution (issue #8471): no step ran, so no
				# PR-controlled step could have written these annotations.
				shutdown_ids.add(job["id"])
				action_resolution_ids.add(job["id"])
			else:
				return "genuine"
		for annotations in aggregate_annotations:
			messages = [item["message"].strip() for item in annotations]
			if any(message != GENERIC_STEP_FAILURE and not CI_AGGREGATE_MESSAGE.fullmatch(message)
					for message in messages):
				return "genuine"
			matches = [CI_AGGREGATE_MESSAGE.fullmatch(message) for message in messages]
			matches = [match for match in matches if match]
			if len(matches) != 1:
				return "unverified"
			keys = [key.strip() for key in matches[0][1].split(",")]
			if not keys or any(not key for key in keys):
				return "unverified"
			for key in keys:
				# Successful or skipped matrix siblings (fail-fast: false) are not
				# what made the aggregate key fail; judge only the rest.
				named = [job for job in jobs
					if (job["name"] == key or job["name"].startswith(key + " ("))
					and job["conclusion"] not in ("success", "skipped", "neutral")]
				if not named or any(job["id"] not in shutdown_ids and job["conclusion"] != "cancelled"
						for job in named):
					return "genuine"
		if not shutdown_ids:
			return "unverified"
		return "action_resolution" if action_resolution_ids == shutdown_ids else "shutdown"
	except (ValueError, OSError, subprocess.TimeoutExpired):
		return "unverified"

def process(prs: list, repo: str, enabled: bool, dry_run: bool, head_filter: str,
		action_resolution_enabled: bool = True) -> None:
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
		if "[skip ai]" in pr["title"] or skip_ai_body(pr["body"]):
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
		current_heads = {} if dry_run else current_pr_heads(repo)
	except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
		reason = str(exc) if isinstance(exc, ValueError) else "api_unavailable"
		for pr in eligible:
			decision(pr["number"], pr["head_sha"], None, "skip", reason)
		return

	processed_heads: set[str] = set()
	classified = 0
	for pr in eligible:
		number, sha = pr["number"], pr["head_sha"]
		if not dry_run and current_heads.get(number) != sha:
			decision(number, sha, None, "skip", "superseded_head")
			continue
		if any(isinstance(item, dict) and positive_number(item.get("number")) and item["number"] == number
				and (not isinstance(item.get("head"), dict)
					or not isinstance(item["head"].get("sha"), str)
					or not SHA_PATTERN.fullmatch(item["head"]["sha"]))
				for run in completed if run["event"] == "pull_request" for item in run["pull_requests"]):
			decision(number, sha, None, "skip", "invalid_association")
			continue
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
		newest = max((run for run in completed if associated(run, number, sha)
			or (run["event"] == "push" and run["head_sha"] == sha
				and run.get("head_branch") == pr["head_ref"])), key=lambda run: (
			datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).timestamp(), run["id"]
		))
		run_id = newest["id"]
		if any(run["run_attempt"] > 1 and (run["event"] == "push" and run["head_sha"] == sha
				and run.get("head_branch") == pr["head_ref"]
				or run["event"] == "pull_request" and any(isinstance(item, dict) and isinstance(item.get("head"), dict)
					and item["head"].get("sha") == sha for item in run["pull_requests"]))
				for run in completed):
			decision(number, sha, run_id, "skip", "already_retried")
		elif newest["event"] == "push" and newest.get("head_branch") != pr["head_ref"]:
			decision(number, sha, run_id, "skip", "push_run")
		elif newest["conclusion"] not in ("cancelled", "startup_failure", "failure"):
			decision(number, sha, run_id, "skip", "other_conclusion")
		elif any(associated(run, number, sha)
				or (run["event"] == "push" and run["head_sha"] == sha
					and run.get("head_branch") == pr["head_ref"])
				or (run["event"] == "pull_request" and any(
					isinstance(item, dict) and item.get("number") == number
					and (not isinstance(item.get("head"), dict)
						or not isinstance(item["head"].get("sha"), str)
						or not SHA_PATTERN.fullmatch(item["head"]["sha"]))
					for item in run["pull_requests"]))
				or (run["event"] == "pull_request" and not run["pull_requests"]
					and run.get("head_branch") == pr["head_ref"])
				for run in active):
			decision(number, sha, run_id, "skip", "active_run")
		elif dry_run:
			decision(number, sha, run_id, "skip", "dry_run")
		elif newest["conclusion"] == "failure":
			# Runner-shutdown recovery (issue #7109). The completed-run,
			# active-run and open-PR listings above, and the sweep's
			# review-run cache, carry no job or annotation data, so this
			# needs its own reads. They run only for a `failure` candidate
			# that passed every other guard, capped per run and per tick.
			if classified >= MAX_FAILURE_CLASSIFICATIONS_PER_TICK:
				decision(number, sha, run_id, "skip", "runner_shutdown_unverified")
				continue
			classified += 1
			verdict = classify_runner_shutdown(repo, run_id, action_resolution_enabled=action_resolution_enabled)
			if verdict == "genuine":
				decision(number, sha, run_id, "skip", "other_conclusion")
			elif verdict not in ("shutdown", "action_resolution"):
				decision(number, sha, run_id, "skip", "runner_shutdown_unverified")
			elif rerun_failed_jobs(repo, run_id):
				decision(number, sha, run_id, "rerun",
					"action_resolution" if verdict == "action_resolution" else "runner_shutdown")
			else:
				decision(number, sha, run_id, "skip", "rerun_refused")
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
		action_resolution_enabled=action_resolution_rerun_enabled(
			os.environ.get("CI_ACTION_RESOLUTION_RERUN_ENABLED")),
	)


if __name__ == "__main__":
	main()

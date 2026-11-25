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
A failed job whose checkout step could not fetch (HTTP 408 / RPC failure,
issue #8671) and that ran nothing after checkout is re-run once too (reason
`checkout_fetch`), again with no extra API calls;
CI_CHECKOUT_FETCH_RERUN_ENABLED=false turns that class off.
A run whose only failed job is the `lint` aggregate, naming only jobs GitHub
abandoned (reported `cancelled`, with no failure annotation other than the
runner-shutdown text or "The operation was canceled.", issue #9146), is
re-run once too (reason `abandoned`). Its annotation reads for those named
jobs count against MAX_FAILED_JOBS_CLASSIFIED, so the worst cases above are
unchanged; CI_ABANDONED_JOB_RERUN_ENABLED=false turns that class off.
Final-CI runs (docs/plans/final-ci-failure-ci-fix-plan.md Phase 1): ci.yml
runs once per approved head as a `workflow_dispatch` run named
`CI [pr:<n>]`. Such a run on PR <n>'s current head is a candidate like an
associated `pull_request` run (the same listings, guards and once-per-head
cap; a `CI [pr:<n>] [sync-quick]` or manual dispatch on the head is never a
candidate, blocks while active, and as the head's newest run skips with
`not_final_ci`). A failed one is re-run only for the four infra
verdicts above; its decision line ends in `final_ci=true`.
`classify-run --repo <o/r> --run-id <id> [--ignore-job <name>]` prints one
run's verdict (one word) and exits 0; scripts/review_final_ci_failed.sh uses
it from inside the run, ignoring its own in-progress `final-ci-failed` job.
Stale workflow file (issue #10093): with `--stale-workflow-head <sha>
--stale-workflow-base <ref>`, classify-run also returns
`stale_workflow_timeout` when `lint` failed only at "Require every CI job to
succeed" and every job it names was cancelled at its `timeout-minutes` limit
(the timeout annotation, no step concluded `failure`) while
`.github/workflows/ci.yml` on the head differs from the base's copy (and the
base's limit for that job is higher, when both limits parse). That costs two
contents GETs, only on this path. The verdict is not an infra verdict: the
sweep never re-runs it (the branch's old limit would apply again).
Annotation and workflow file text are never logged.
"""

from __future__ import annotations

import base64
import importlib.util
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


# Issue #9473: wedged-queued run filter. A queued run older than
# ZOMBIE_RUN_MAX_QUEUED_HOURS (default 24; 0 or invalid disables) has zero
# jobs and cannot be cancelled, so it must not count as active. The shared
# predicate lives in orchestrate_lib.py, loaded from this script's own
# directory only (never the cwd or a PR checkout). If it cannot be loaded,
# every queued run stays active (pre-#9473 behaviour).
_ZOMBIE_LIB_STATE: dict = {"loaded": False, "lib": None, "hours": 0}


def _zombie_lib() -> object:
	if not _ZOMBIE_LIB_STATE["loaded"]:
		_ZOMBIE_LIB_STATE["loaded"] = True
		name = "_ci_cancelled_rerun_orchestrate_lib"
		try:
			spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("orchestrate_lib.py"))
			if spec is None or spec.loader is None:
				return None
			module = importlib.util.module_from_spec(spec)
			sys.modules[name] = module
			spec.loader.exec_module(module)
			if callable(getattr(module, "is_wedged_queued_run", None)) and callable(getattr(module, "log_zombie_run_ignored", None)):
				_ZOMBIE_LIB_STATE["lib"] = module
				_ZOMBIE_LIB_STATE["hours"] = int(module.zombie_run_max_queued_hours())
		except Exception:
			sys.modules.pop(name, None)
			_ZOMBIE_LIB_STATE["lib"] = None
	return _ZOMBIE_LIB_STATE["lib"]


def _is_wedged_queued(run: object, now_epoch: int, guard: str) -> bool:
	"""True (and logs ZOMBIE_RUN_IGNORED once) when ``run`` is wedged."""
	lib = _zombie_lib()
	if lib is None:
		return False
	try:
		if not lib.is_wedged_queued_run(run, int(now_epoch), _ZOMBIE_LIB_STATE["hours"]):
			return False
		lib.log_zombie_run_ignored(run, guard, int(now_epoch))
		return True
	except Exception:
		return False
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
# Checkout-fetch classification (issue #8671, run 40999073395): a job whose
# `actions/checkout` step could not fetch from GitHub (HTTP 408 / RPC
# failure) before any PR step ran. Each pattern is used with .fullmatch()
# on the stripped annotation message.
CHECKOUT_FETCH_MESSAGES = (
	re.compile(r"RPC failed; HTTP (408|429|5\d\d) curl \d+ .*"),
	re.compile(r"expected 'packfile'"),
	re.compile(r"The process '/usr/bin/git' failed with exit code 128"),
	re.compile(r"fetch-pack: unexpected disconnect while reading sideband packet"),
	re.compile(r"early EOF"),
	re.compile(r"the remote end hung up unexpectedly"),
)
# At least one annotation must name the fetch failure itself; a bare git
# exit code 128 (or `early EOF`) alone does not qualify.
CHECKOUT_FETCH_STRONG = (
	CHECKOUT_FETCH_MESSAGES[0],
	CHECKOUT_FETCH_MESSAGES[3],
	CHECKOUT_FETCH_MESSAGES[5],
)
CHECKOUT_STEP_NAME = "Checkout repository"
CHECKOUT_STEP_PREFIX = "Run actions/checkout"
# ci.yml's aggregate job keeps the id `lint` (CLAUDE.md §6) and fails with
# `::error::CI jobs that did not succeed: <key>, <key>` when a needed job fails.
CI_AGGREGATE_JOB_NAME = "lint"
CI_AGGREGATE_MESSAGE = re.compile(r"CI jobs that did not succeed: (.+)\Z")
GENERIC_STEP_FAILURE = "Process completed with exit code 1."
# The only other failure annotation a real runner shutdown leaves on the job.
SHUTDOWN_COMPANION_MESSAGE = "The operation was canceled."
CHECK_RUN_URL_PATTERN = re.compile(r"/check-runs/([1-9][0-9]*)\Z")
# ci.yml's run-name for a full (or docs-only) final-CI dispatch; a sync-quick
# run carries a ` [sync-quick]` suffix and does not match.
FINAL_CI_RUN_TITLE = re.compile(r"CI \[pr:([1-9][0-9]*)\]\Z")
INFRA_VERDICTS = ("shutdown", "action_resolution", "checkout_fetch", "abandoned")
# Stale workflow file (issue #10093). The runner's timeout annotation, in both
# wordings seen ("The job has exceeded ..." and "The job running on runner
# <name> has exceeded ..."); used with .match() on the stripped message.
STALE_WORKFLOW_TIMEOUT_MESSAGE = re.compile(
	r"The job (?:running on runner .+ )?has exceeded the maximum execution time of "
)
# The aggregate step of ci.yml's `lint` job.
CI_AGGREGATE_STEP_NAME = "Require every CI job to succeed"
STALE_WORKFLOW_PATH = ".github/workflows/ci.yml"
# Deliberately not in INFRA_VERDICTS: a failed-jobs re-run would use the
# branch's old workflow file again.
STALE_WORKFLOW_VERDICT = "stale_workflow_timeout"
STALE_WORKFLOW_REF_PATTERN = re.compile(r"[A-Za-z0-9._/-]+\Z")
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


def checkout_fetch_rerun_enabled(raw: str | None) -> bool:
	# Off switch for the checkout-fetch class only (issue #8671), parsed like
	# CI_ACTION_RESOLUTION_RERUN_ENABLED. The raw value is never echoed.
	value = (raw or "").lower()
	if value in ("", "true"):
		return True
	if value != "false":
		print("::warning::CI_CHECKOUT_FETCH_RERUN_ENABLED is not true or false; "
			"checkout-fetch CI re-runs are off", file=sys.stderr)
	return False


def abandoned_job_rerun_enabled(raw: str | None) -> bool:
	# Off switch for the abandoned-job class only (issue #9146), parsed like
	# CI_CHECKOUT_FETCH_RERUN_ENABLED. The raw value is never echoed.
	value = (raw or "").lower()
	if value in ("", "true"):
		return True
	if value != "false":
		print("::warning::CI_ABANDONED_JOB_RERUN_ENABLED is not true or false; "
			"abandoned-job CI re-runs are off", file=sys.stderr)
	return False


def shutdown_only_annotations(annotations: list[dict]) -> bool:
	# Q2 "medium" rule (issue #9146): every failure annotation is the runner
	# shutdown text or the exact cancel companion; none at all also counts.
	# A timeout-minutes cancellation leaves its own annotation and fails this.
	return all(item["message"].startswith(RUNNER_SHUTDOWN_PREFIX)
		or item["message"].strip() == SHUTDOWN_COMPANION_MESSAGE for item in annotations)


def job_failed_at_checkout(job: dict) -> bool:
	# Fail closed on a missing or malformed `steps` list. True only when the
	# first failed step is the checkout step, only `Set up job` ran (and
	# succeeded) before it, and everything after it was skipped apart from
	# cleanup steps. The step is matched by display name only, and a
	# pull_request run uses the PR's own workflow file, so a PR could name a
	# step like this and print matching errors; the once-per-head rerun cap
	# bounds that to one extra rerun of the PR's own CI.
	steps = job.get("steps")
	if not isinstance(steps, list) or not steps \
			or any(not isinstance(step, dict) or not isinstance(step.get("name"), str) for step in steps):
		return False
	failed_index = next((index for index, step in enumerate(steps) if step.get("conclusion") == "failure"), None)
	if failed_index is None:
		return False
	name = steps[failed_index]["name"]
	if name != CHECKOUT_STEP_NAME and not name.startswith(CHECKOUT_STEP_PREFIX):
		return False
	if any(step["name"] != "Set up job" or step.get("conclusion") != "success" for step in steps[:failed_index]):
		return False
	return all(step.get("conclusion") == "skipped" or step["name"].startswith("Post ")
		or step["name"] == "Complete job" for step in steps[failed_index + 1:])


def checkout_fetch_annotations(annotations: list[dict]) -> bool:
	messages = [item["message"].strip() for item in annotations]
	return bool(messages) \
		and all(any(pattern.fullmatch(message) for pattern in CHECKOUT_FETCH_MESSAGES) for message in messages) \
		and any(any(pattern.fullmatch(message) for pattern in CHECKOUT_FETCH_STRONG) for message in messages)


def _well_formed_steps(job: dict) -> list | None:
	steps = job.get("steps")
	if not isinstance(steps, list) or not steps \
			or any(not isinstance(step, dict) or not isinstance(step.get("name"), str) for step in steps):
		return None
	return steps


def job_cut_off_at_timeout(job: dict, annotations: list[dict]) -> bool:
	# Issue #10093: a cancelled job with no failed step that carries the
	# runner's timeout annotation. Other failure annotations are ignored:
	# passing tests print `##[error]` lines; step conclusions decide.
	if job.get("conclusion") != "cancelled":
		return False
	steps = _well_formed_steps(job)
	if steps is None or any(step.get("conclusion") == "failure" for step in steps):
		return False
	return any(STALE_WORKFLOW_TIMEOUT_MESSAGE.match(item["message"].strip()) for item in annotations)


def aggregate_failed_only_at_require_step(job: dict) -> bool:
	# The `lint` aggregate failed at its require step and nowhere else.
	steps = _well_formed_steps(job)
	if steps is None:
		return False
	failed = [step for step in steps if step.get("conclusion") == "failure"]
	return len(failed) == 1 and failed[0]["name"] == CI_AGGREGATE_STEP_NAME


def workflow_file_at_ref(repo: str, ref: str) -> tuple[str, str]:
	"""Return (blob sha, decoded text) of ci.yml at ``ref``; one contents GET.

	The text is "" when the payload is not base64. Raises
	ValueError("workflow_unreadable") on any read or shape error.
	"""
	result = subprocess.run(
		["gh", "api", "-X", "GET", f"repos/{repo}/contents/{STALE_WORKFLOW_PATH}?ref={ref}"],
		capture_output=True, text=True, timeout=30, check=False,
	)
	if result.returncode != 0:
		raise ValueError("workflow_unreadable")
	try:
		payload = json.loads(result.stdout)
	except ValueError as exc:
		raise ValueError("workflow_unreadable") from exc
	if not isinstance(payload, dict) or not isinstance(payload.get("sha"), str) \
			or not SHA_PATTERN.fullmatch(payload["sha"]):
		raise ValueError("workflow_unreadable")
	text = ""
	if payload.get("encoding") == "base64" and isinstance(payload.get("content"), str):
		try:
			text = base64.b64decode(payload["content"]).decode("utf-8")
		except (ValueError, UnicodeDecodeError):
			text = ""
	return payload["sha"], text


def job_timeout_minutes(text: str, job_key: str) -> int | None:
	# Regex-only scan of a job's own `timeout-minutes` (no YAML dependency).
	start = re.search(r"(?m)^  " + re.escape(job_key) + r":[ \t]*$", text)
	if not start:
		return None
	rest = text[start.end():]
	end = re.search(r"(?m)^  \S", rest)
	block = rest[:end.start()] if end else rest
	limit = re.search(r"(?m)^    timeout-minutes:[ \t]*([0-9]{1,3})[ \t]*$", block)
	return int(limit[1]) if limit else None


def stale_workflow_verdict(repo: str, head_sha: str, base_ref: str, timeout_jobs: list[dict]) -> str:
	# Two contents GETs (head, then base). A read error raises ValueError,
	# which the caller turns into "unverified".
	head_blob, head_text = workflow_file_at_ref(repo, head_sha)
	base_blob, base_text = workflow_file_at_ref(repo, base_ref)
	if head_blob == base_blob:
		return "genuine"
	for job in timeout_jobs:
		key = job["name"].split(" (", 1)[0]
		head_limit = job_timeout_minutes(head_text, key)
		base_limit = job_timeout_minutes(base_text, key)
		if head_limit is not None and base_limit is not None and base_limit <= head_limit:
			return "genuine"
	return STALE_WORKFLOW_VERDICT


def job_ran_no_steps(job: dict) -> bool:
	# Fail closed: a missing or malformed `steps` value is not evidence
	# that the job ran no steps, so today's `genuine` result stands.
	steps = job.get("steps")
	if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
		return False
	return not any(step.get("conclusion") in ("success", "failure") for step in steps)


def decision(pr: object, head: object, run: object, action: str, reason: str, final_ci: bool = False) -> None:
	# Never echo untrusted snapshot or API fields without validation.
	pr_text = str(pr) if positive_number(pr) else "none"
	head_text = head if isinstance(head, str) and SHA_PATTERN.fullmatch(head) else "none"
	run_text = str(run) if positive_number(run) else "none"
	suffix = " final_ci=true" if final_ci else ""
	print(f"CI_CANCELLED_RERUN pr={pr_text} head={head_text} run={run_text} action={action} reason={reason}{suffix}")


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
				or run.get("status") != status or run.get("event") not in ("pull_request", "push", "workflow_dispatch") \
				or (run["event"] == "workflow_dispatch" and not isinstance(run.get("display_title"), str)) \
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


def final_ci_associated(run: dict, pr_number: int, sha: str) -> bool:
	# A full final-CI dispatch for this PR on its current head (never a
	# ` [sync-quick]` run): the run name binds the PR, head_sha the head.
	if run["event"] != "workflow_dispatch" or run["head_sha"] != sha:
		return False
	match = FINAL_CI_RUN_TITLE.fullmatch(run.get("display_title") or "")
	return bool(match) and int(match[1]) == pr_number


def dispatch_on_head(run: dict, sha: str) -> bool:
	# Any ci.yml dispatch on the head (final, sync-quick or manual) blocks a
	# re-run while active. Only this PR's own final run counts as a retry:
	# scripts/review_final_ci_failed.sh has deferred to this re-run, so an
	# unrelated re-attempted dispatch must not block it.
	return run["event"] == "workflow_dispatch" and run["head_sha"] == sha


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


# Mirrored by triage_release_download_rerun_gate in scripts/check_failure_triage.sh
# (issue #9812); keep the bounds and checks of both copies in sync.
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



# Mirrored by triage_release_download_rerun_gate in scripts/check_failure_triage.sh
# (issue #9812); keep the bounds and checks of both copies in sync.
def list_run_jobs(repo: str, run_id: int, ignore_job_names: tuple[str, ...] = ()) -> list[dict]:
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
	# classify-run reads its own run: the caller's job is still running.
	jobs = [job for job in jobs if not (isinstance(job, dict) and job.get("name") in ignore_job_names)]
	for job in jobs:
		if not isinstance(job, dict) or not positive_number(job.get("id")) \
				or not isinstance(job.get("name"), str) or job.get("status") != "completed" \
				or not isinstance(job.get("conclusion"), str) \
				or not isinstance(job.get("check_run_url"), str) \
				or not CHECK_RUN_URL_PATTERN.search(job["check_run_url"]):
			raise ValueError("jobs_unreadable")
	return jobs


# Mirrored by triage_release_download_rerun_gate in scripts/check_failure_triage.sh
# (issue #9812); keep the bounds and checks of both copies in sync.
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


def classify_runner_shutdown(repo: str, run_id: int, action_resolution_enabled: bool = True,
		checkout_fetch_enabled: bool = True, abandoned_job_enabled: bool = True,
		ignore_job_names: tuple[str, ...] = (), stale_workflow_head: str = "",
		stale_workflow_base: str = "") -> str:
	"""Return "shutdown", "action_resolution", "checkout_fetch", "abandoned",
	"stale_workflow_timeout", "genuine", or "unverified".

	Fail closed: only a run whose every failed job was killed by a runner
	shutdown or, when enabled, failed before any step ran with only
	"Unable to resolve action" annotations (or is the `lint` aggregate
	reporting only such jobs) counts. "action_resolution" means every
	non-lint failed job is in that class; a mix with shutdown jobs is
	"shutdown". When enabled, a job whose checkout step failed to fetch
	(nothing ran after it, only fetch-error annotations, issue #8671) also
	counts; any run with such a job, mixed only with the other classes, is
	"checkout_fetch". Those classes make no extra API reads. When enabled
	and the `lint` aggregate is the only failed job, every non-success job
	it names is `cancelled` (GitHub reports an abandoned job that way,
	issue #9146) and each has only shutdown or "The operation was
	canceled." failure annotations (or none), the run is "abandoned". That
	class reads annotations for at most MAX_FAILED_JOBS_CLASSIFIED -
	len(failed) named cancelled jobs, checked before any extra read, so the
	per-run and per-tick worst cases are unchanged. Annotation text is never
	logged; it can be written by a PR's own steps.
	Only when both stale_workflow_head and stale_workflow_base are given
	(the classify-run CLI, issue #10093): on the same lint-only path, when
	every named job was cut off at its time limit (job_cut_off_at_timeout),
	no abandoned job is mixed in, every other non-green job is one of them
	and `lint` failed only at its require step, the result comes from
	stale_workflow_verdict (two contents GETs). Workflow file text is never
	logged either.
	"""
	stale_enabled = bool(stale_workflow_head) and bool(stale_workflow_base)
	try:
		jobs = list_run_jobs(repo, run_id, ignore_job_names)
		if any(job["conclusion"] not in BENIGN_JOB_CONCLUSIONS for job in jobs):
			return "genuine"
		failed = [job for job in jobs if job["conclusion"] == "failure"]
		if not failed or len(failed) > MAX_FAILED_JOBS_CLASSIFIED:
			return "unverified"
		shutdown_ids: set[int] = set()
		action_resolution_ids: set[int] = set()
		checkout_fetch_ids: set[int] = set()
		aggregate_annotations: list[list[dict]] = []
		# Named cancelled jobs, keyed by id so matrix keys matching the same
		# job twice are read once (issue #9146). Collected only; the
		# non-empty shutdown_ids paths below ignore it.
		aggregate_cancelled: dict[int, dict] = {}
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
			elif checkout_fetch_enabled and job_failed_at_checkout(job) \
					and checkout_fetch_annotations(annotations):
				# Checkout fetch timeout (issue #8671): nothing ran after the
				# checkout step; bounded by the once-per-head rerun cap.
				shutdown_ids.add(job["id"])
				checkout_fetch_ids.add(job["id"])
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
				for job in named:
					if job["conclusion"] == "cancelled" and job["id"] not in shutdown_ids:
						aggregate_cancelled[job["id"]] = job
		if not shutdown_ids:
			# Every failed job is the `lint` aggregate: any other failed job
			# already returned "genuine" or joined shutdown_ids.
			if not aggregate_cancelled or (not abandoned_job_enabled and not stale_enabled):
				return "unverified"
			if len(failed) + len(aggregate_cancelled) > MAX_FAILED_JOBS_CLASSIFIED:
				return "unverified"
			abandoned_count = 0
			timeout_jobs: list[dict] = []
			for job in aggregate_cancelled.values():
				check_run_id = int(CHECK_RUN_URL_PATTERN.search(job["check_run_url"])[1])
				annotations = failure_annotations(repo, check_run_id)
				if shutdown_only_annotations(annotations):
					abandoned_count += 1
				elif stale_enabled and job_cut_off_at_timeout(job, annotations):
					timeout_jobs.append(job)
				else:
					return "genuine"
			if not timeout_jobs:
				return "abandoned" if abandoned_job_enabled else "unverified"
			# Issue #10093: only a run whose failures are all time-limit
			# cut-offs qualifies; anything mixed in keeps today's "genuine".
			timeout_ids = {job["id"] for job in timeout_jobs}
			if abandoned_count \
					or any(job["name"] != CI_AGGREGATE_JOB_NAME and job["id"] not in timeout_ids
						and job["conclusion"] not in ("success", "skipped") for job in jobs) \
					or any(not aggregate_failed_only_at_require_step(job) for job in failed):
				return "genuine"
			return stale_workflow_verdict(repo, stale_workflow_head, stale_workflow_base, timeout_jobs)
		if checkout_fetch_ids:
			return "checkout_fetch"
		return "action_resolution" if action_resolution_ids == shutdown_ids else "shutdown"
	except (ValueError, OSError, subprocess.TimeoutExpired):
		return "unverified"

def process(prs: list, repo: str, enabled: bool, dry_run: bool, head_filter: str,
		action_resolution_enabled: bool = True, checkout_fetch_enabled: bool = True,
		abandoned_job_enabled: bool = True) -> None:
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
		active_now_epoch = int(datetime.now().timestamp())
		active = [
			run
			for status in ACTIVE_STATUSES
			for run in list_runs(repo, status)
			if not _is_wedged_queued(run, active_now_epoch, "ci_cancelled_rerun")
		]
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
		matches = [run for run in completed if associated(run, number, sha) or final_ci_associated(run, number, sha)]
		if not matches:
			decision(number, sha, None, "skip", "no_attributable_run")
			continue
		processed_heads.add(sha)
		# created_at orders distinct run IDs; run_attempt belongs to an ID,
		# not the head. A visible retry on *any* ID blocks another POST.
		newest = max((run for run in completed if associated(run, number, sha)
			or dispatch_on_head(run, sha)
			or (run["event"] == "push" and run["head_sha"] == sha
				and run.get("head_branch") == pr["head_ref"])), key=lambda run: (
			datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).timestamp(), run["id"]
		))
		run_id = newest["id"]
		final_ci = final_ci_associated(newest, number, sha)
		if any(run["run_attempt"] > 1 and (final_ci_associated(run, number, sha) or run["event"] == "push" and run["head_sha"] == sha
				and run.get("head_branch") == pr["head_ref"]
				or run["event"] == "pull_request" and any(isinstance(item, dict) and isinstance(item.get("head"), dict)
					and item["head"].get("sha") == sha for item in run["pull_requests"]))
				for run in completed):
			decision(number, sha, run_id, "skip", "already_retried", final_ci=final_ci)
		elif newest["event"] == "push" and newest.get("head_branch") != pr["head_ref"]:
			decision(number, sha, run_id, "skip", "push_run", final_ci=final_ci)
		elif newest["event"] == "workflow_dispatch" and not final_ci:
			# The head's newest run is a sync-quick, manual or other PR's
			# dispatch: an older full final run is not what CI reports now.
			decision(number, sha, run_id, "skip", "not_final_ci")
		elif newest["conclusion"] not in ("cancelled", "startup_failure", "failure"):
			decision(number, sha, run_id, "skip", "other_conclusion", final_ci=final_ci)
		elif any(associated(run, number, sha) or dispatch_on_head(run, sha)
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
			decision(number, sha, run_id, "skip", "active_run", final_ci=final_ci)
		elif dry_run:
			decision(number, sha, run_id, "skip", "dry_run", final_ci=final_ci)
		elif newest["conclusion"] == "failure":
			# Runner-shutdown recovery (issue #7109). The completed-run,
			# active-run and open-PR listings above, and the sweep's
			# review-run cache, carry no job or annotation data, so this
			# needs its own reads. They run only for a `failure` candidate
			# that passed every other guard, capped per run and per tick.
			if classified >= MAX_FAILURE_CLASSIFICATIONS_PER_TICK:
				decision(number, sha, run_id, "skip", "runner_shutdown_unverified", final_ci=final_ci)
				continue
			classified += 1
			verdict = classify_runner_shutdown(repo, run_id, action_resolution_enabled=action_resolution_enabled,
				checkout_fetch_enabled=checkout_fetch_enabled, abandoned_job_enabled=abandoned_job_enabled)
			if verdict == "genuine":
				decision(number, sha, run_id, "skip", "other_conclusion", final_ci=final_ci)
			elif verdict not in INFRA_VERDICTS:
				decision(number, sha, run_id, "skip", "runner_shutdown_unverified", final_ci=final_ci)
			elif rerun_failed_jobs(repo, run_id):
				decision(number, sha, run_id, "rerun",
					verdict if verdict in ("action_resolution", "checkout_fetch", "abandoned") else "runner_shutdown",
					final_ci=final_ci)
			else:
				decision(number, sha, run_id, "skip", "rerun_refused", final_ci=final_ci)
		elif rerun_failed_jobs(repo, run_id):
			decision(number, sha, run_id, "rerun", "cancelled_ci", final_ci=final_ci)
		else:
			decision(number, sha, run_id, "skip", "rerun_refused", final_ci=final_ci)


def classify_run_cli(argv: list[str]) -> int:
	"""`classify-run --repo <o/r> --run-id <id> [--ignore-job <name>]...
	[--stale-workflow-head <sha> --stale-workflow-base <ref>]`.

	Prints one word, the classify_runner_shutdown verdict for attempt 1 of
	the run, and returns 0. Bad arguments print `unverified`. The per-cause
	switches are read from the same env vars as the sweep. API reads: one
	jobs GET plus the annotation GETs classify_runner_shutdown makes, plus
	two contents GETs on the stale-workflow path (issue #10093) when both
	--stale-workflow-* values are given.
	"""
	repo, run_id, ignore = "", "", []
	stale_head, stale_base = "", ""
	args = list(argv)
	while args:
		flag = args.pop(0)
		if flag in ("--repo", "--run-id", "--ignore-job", "--stale-workflow-head", "--stale-workflow-base") and args:
			value = args.pop(0)
			if flag == "--repo":
				repo = value
			elif flag == "--run-id":
				run_id = value
			elif flag == "--stale-workflow-head":
				stale_head = value
			elif flag == "--stale-workflow-base":
				stale_base = value
			else:
				ignore.append(value)
		else:
			print("unverified")
			return 0
	if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or ".." in repo \
			or not re.fullmatch(r"[1-9][0-9]*", run_id) \
			or any(not re.fullmatch(r"[A-Za-z0-9_. ()/-]+", name) for name in ignore) \
			or (stale_head and not re.fullmatch(r"[0-9a-f]{40}", stale_head)) \
			or (stale_base and (not STALE_WORKFLOW_REF_PATTERN.match(stale_base) or ".." in stale_base
				or stale_base.startswith("-"))):
		print("unverified")
		return 0
	print(classify_runner_shutdown(repo, int(run_id),
		action_resolution_enabled=action_resolution_rerun_enabled(os.environ.get("CI_ACTION_RESOLUTION_RERUN_ENABLED")),
		checkout_fetch_enabled=checkout_fetch_rerun_enabled(os.environ.get("CI_CHECKOUT_FETCH_RERUN_ENABLED")),
		abandoned_job_enabled=abandoned_job_rerun_enabled(os.environ.get("CI_ABANDONED_JOB_RERUN_ENABLED")),
		ignore_job_names=tuple(ignore), stale_workflow_head=stale_head, stale_workflow_base=stale_base))
	return 0


def main() -> None:
	if sys.argv[1:2] == ["classify-run"]:
		raise SystemExit(classify_run_cli(sys.argv[2:]))
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
		checkout_fetch_enabled=checkout_fetch_rerun_enabled(
			os.environ.get("CI_CHECKOUT_FETCH_RERUN_ENABLED")),
		abandoned_job_enabled=abandoned_job_rerun_enabled(
			os.environ.get("CI_ABANDONED_JOB_RERUN_ENABLED")),
	)


if __name__ == "__main__":
	main()

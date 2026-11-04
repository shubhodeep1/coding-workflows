#!/usr/bin/env python3
"""Already-resolved gate for ai:check-triage issues (issue #7095).

An ai:check-triage issue is filed by ``scripts/check_failure_triage.sh`` when a
check fails on a pull request. By the time the issue reaches clarify (usually
on a later ``/reclarify``), the PR may already have merged and the failure may
already be fixed on the default branch (#6945: PR #6938 merged and PR #6941
merged the test correction the triage named). Asking for ``/answer`` and
planning such an issue only loops. The clarify route step therefore runs:

  python3 scripts/check_triage_resolution.py gate \
      --issue-json <issue.json> --repo <owner/repo> --issue-number <n> \
      --default-branch <branch> [--dry-run]

and prints one JSON object:

  {"status": "not_triage" | "disabled" | "already_resolved" | "resolved" |
   "unverified" | "write_failed", "reason": ..., "issue": n, "pr": m,
   "merge_sha": ..., "evidence_sha": ..., "evidence_url": ..., "check_name": ...,
   "evidence_ref": "default" | "integration_pr:<n>",
   "failed_jobs": [...], "traceback_posted": [...], "writes": [...],
   "path": "merged" | "open_head" (only when resolved),
   "pr_state": "merged" | "open" | "closed" (when the PR was readable)}

(the integration path also adds "integration_pr" and, once that PR merged,
"integration_compare_status").

Resolved (``path=merged``) means all of the following were read from GitHub
(never from the issue body):
- the linked PR merged (same-repository head, 40-hex merge commit);
- check-run triage: every latest run of the exact check name on the default
  branch tip completed with ``success``;
  workflow-run triage: the triage's run belongs to this repository and PR (it
  lists the PR, runs on the PR's current head, or runs on the trusted header's
  Head SHA on the PR's head branch), and
  the newest completed default-branch push run of the same workflow concluded
  ``success`` with every failed PR job name (matrix suffix included) present
  exactly once and ``success``;
- the evidence commit contains the PR's merge commit (compare API
  ``ahead``/``identical``).
Resolved (``path=open_head``, issue #8671) covers a linked PR that is still
open and whose head has moved past the trusted header's Head SHA:
- the PR is open and same-repository, and its current head (from the PR read)
  is a 40-hex SHA that differs from the header's Head SHA;
- check-run triage: every latest run of the exact check name on the current
  head completed with ``success``;
  workflow-run triage: the newest run of the same workflow on the current
  head with event ``pull_request`` (no status filter, so a newer pending run
  blocks) completed with ``success``, and every failed job name of the
  triaged run is present exactly once and ``success``.
No compare read is made on this path; the marker's sha is the current head.

Integration-branch rule (issue #8685): a PR merged into
``orchestrator/project-<N>`` reaches the default branch only inside the
project's squash commit, so its own merge commit is never an ancestor of the
default tip. When the PR's base matches that pattern, issue N must carry
``ai:orchestrator-tracking`` and the newest PR from that branch into the default
branch (the integration PR) must have a same-repository head. While the
integration PR is open, the evidence is its head: the newest completed
``pull_request`` run of the same workflow on that head must be ``success`` with
every failed job name present once and ``success`` (check-run triage: every
latest run of the check name on that head is ``success``), and the head must
contain the PR's merge commit. Once the integration PR merged, the existing
default-branch check runs, the integration PR head must contain the PR's merge
commit, and the default-branch evidence commit must contain the integration
PR's merge commit. An integration PR closed without merging is ``unverified``.
Every other base keeps the default-branch path (``evidence_ref=default``).
Any other conclusion (``neutral``, ``skipped``, pending, ...) or any read
failure is ``unverified``: a failed check is never treated as passed.

On ``resolved`` the gate posts one evidence comment ending with
``<!-- ai:check-triage-resolved:v1 issue=<n> pr=<m> sha=<sha> -->``, adds
``ai:closed`` and closes the issue as completed. It never writes to the PR. A
marker counts only when the authenticated (GH_PAT) account posted it, so a rerun
is idempotent (``already_resolved``) and a forged marker is ignored. A write
failure after verification exits 3 (``write_failed``).

On ``unverified`` the gate posts the failing job's traceback once per job
(``<!-- ai:check-triage-traceback:v1 issue=<n> job=<id> -->``), using the
heal intake's log filter and failing-test parser, and exits 0 so clarify
continues on its normal path.

API calls (only for a trusted ai:check-triage issue): 1 user read, 1 paginated
comment read, 1 PR read, 1-4 run/job/check reads, 1 compare read, up to 3 job
log reads on the unverified path, and up to 3 writes. The open-head path
replaces the default-branch and compare reads with at most 2 reads (1 for
check-run triage), made only for an open PR whose head moved. The
integration-branch path adds 1 issue read, 1 PR list read and 1-2 run/check
reads, plus 1 more compare read once the integration PR merged. ``CHECK_TRIAGE_RESOLVED_GATE_ENABLED``
(default ``true``) set to ``false`` disables the gate with no API call.
Log prefix: ``CHECK_TRIAGE_RESOLVED``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow_failure_heal as heal  # noqa: E402

TRIAGE_LABEL = "ai:check-triage"
TRACKING_LABEL = "ai:orchestrator-tracking"
CLOSED_LABEL = "ai:closed"
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PR_MARKER_RE = re.compile(r"^ {0,3}<!-- check-failure-triage:pr=([1-9][0-9]{0,9}) -->\s*$")
FAILING_CHECK_RE = re.compile(r"^- \*\*Failing check:\*\* `([^`\n]+)` \(conclusion: `[^`\n]*`\)\s*$")
CHECK_RUN_ID_RE = re.compile(r"^- \*\*Check run id:\*\* `([1-9][0-9]{0,19})`\s*$")
CHECK_DETAILS_RE = re.compile(r"^- \*\*Check details:\*\* (\S*)\s*$")
HEAD_SHA_RE = re.compile(r"^- \*\*Head SHA:\*\* `([0-9a-f]{40})`\s*$")
RESOLVED_MARKER_RE = re.compile(
	r"^<!-- ai:check-triage-resolved:v1 issue=([1-9][0-9]*) pr=([1-9][0-9]*) sha=([0-9a-f]{40}) -->$"
)
TRACEBACK_MARKER_RE = re.compile(r"^<!-- ai:check-triage-traceback:v1 issue=([1-9][0-9]*) job=([1-9][0-9]*) -->$")
INTEGRATION_BASE_RE = re.compile(r"^orchestrator/project-([1-9][0-9]{0,9})$")
TRACEBACK_START_RE = re.compile(r"Traceback \(most recent call last\)|\bFAILED\b|\bFAIL:|\bERROR:")
FAILED_JOB_CONCLUSIONS = ("failure", "timed_out")
MAX_TRACEBACK_JOBS = 3
MAX_EXCERPT_LINES = 120
MAX_EXCERPT_BYTES = 12_000
# Issue #8799: ranked excerpt anchors. The strongest failure signal wins: a
# test-failure line, then the first step "##[error]" line (with the lines
# before it), then a Python traceback, then TRACEBACK_START_RE as before.
MAX_LOG_LINE_CHARS = 2000
STEP_ERROR_AFTER_LINES = 5
TEST_FAILURE_LEAD_LINES = 10
TEST_FAILURE_RE = re.compile(
	r"\bFAIL  [A-Za-z0-9_]+"
	r"|(?:^|\s)FAIL\s+\S+\.(?:test|spec)\.[cm]?[jt]sx?\b"
	r"|(?:^|\s)[\u00d7\u2717]\s+\S"
	r"|\bAssertionError\b"
	r"|(?:^|\s)not ok \d+"
	r"|\bFAILED tests/"
	r"|TEST_CASE_EVENT: .*\"status\":\s*\"fail\""
)
STEP_ERROR_RE = re.compile(r"##\[error\]")
PY_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
EXCERPT_ANCHORS = ("test_failure", "step_error", "traceback", "legacy")
GH_TIMEOUT_SECONDS = 120


class GhApiError(RuntimeError):
	"""A gh api call failed, timed out or returned unparseable output."""


def _gh_api(args: list[str], *, input_obj: Any = None, raw: bool = False) -> Any:
	"""Run ``gh api`` once (no retries, so writes are never repeated)."""
	stdin = json.dumps(input_obj) if input_obj is not None else None
	command = ["gh", "api", *args]
	if input_obj is not None:
		command += ["--input", "-"]
	try:
		response = subprocess.run(
			command, input=stdin, capture_output=True, text=True, check=False, timeout=GH_TIMEOUT_SECONDS,
		)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise GhApiError(type(exc).__name__) from exc
	if response.returncode != 0:
		raise GhApiError(f"rc={response.returncode}")
	if raw:
		return response.stdout
	try:
		return json.loads(response.stdout) if response.stdout.strip() else None
	except ValueError as exc:
		raise GhApiError("malformed_json") from exc


def _label_names(issue: dict[str, Any]) -> list[str]:
	names: list[str] = []
	for label in issue.get("labels") or []:
		name = label.get("name") if isinstance(label, dict) else label
		if isinstance(name, str) and name:
			names.append(name)
	return names


def _author_trusted(issue: dict[str, Any]) -> bool:
	user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
	if user.get("type") == "User" and issue.get("author_association") in TRUSTED_ASSOCIATIONS:
		return True
	return user.get("type") == "Bot" and user.get("login") == "github-actions[bot]"


def _lines_outside_fences(body: str) -> list[str]:
	fence: str | None = None
	kept: list[str] = []
	for line in body.split("\n"):
		match = re.match(r" {0,3}(`{3,}|~{3,})", line)
		if fence is None:
			if match:
				fence = match.group(1)
				continue
			kept.append(line)
		elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not line[match.end():].strip():
			fence = None
	return kept


def parse_triage_issue(issue: Any, repo: str = "") -> dict[str, Any] | None:
	"""Fields of a trusted ai:check-triage issue; None for any other issue.

	Fields are read only from the header the triage script writes (before the
	first ``---`` line); a duplicate anywhere in the body is malformed, so the
	model-written diagnosis below the header cannot add or override a field.
	"""
	if not isinstance(issue, dict) or TRIAGE_LABEL not in _label_names(issue) or not _author_trusted(issue):
		return None
	body = issue.get("body")
	if not isinstance(body, str):
		return {"malformed": "body_missing"}
	lines = [line.rstrip("\r") for line in _lines_outside_fences(body)]
	markers = [match.group(1) for match in (PR_MARKER_RE.match(line) for line in lines) if match]
	if len(markers) != 1:
		return {"malformed": "pr_marker"}
	header: list[str] = []
	for line in lines:
		if line.strip() == "---":
			break
		header.append(line)

	def _one(pattern: re.Pattern[str], prefix: str, required: bool) -> str | None:
		everywhere = sum(1 for line in lines if line.lstrip().startswith(prefix))
		found = [match.group(1) for match in (pattern.match(line) for line in header) if match]
		if len(found) > 1 or everywhere > 1 or (required and len(found) != 1):
			raise ValueError
		return found[0] if found else None

	try:
		check_name = _one(FAILING_CHECK_RE, "- **Failing check:**", True)
		check_run_id = _one(CHECK_RUN_ID_RE, "- **Check run id:**", False)
		details_url = _one(CHECK_DETAILS_RE, "- **Check details:**", True)
		head_sha = _one(HEAD_SHA_RE, "- **Head SHA:**", False)
	except ValueError:
		return {"malformed": "header_fields"}
	run_id = None
	if repo and details_url:
		run_match = re.match(
			r"^https://github\.com/" + re.escape(repo) + r"/actions/runs/([1-9][0-9]{0,19})(?:[/?#]|$)", details_url, re.IGNORECASE,
		)
		if run_match:
			run_id = int(run_match.group(1))
	return {
		"pr": int(markers[0]),
		"check_name": check_name,
		"check_run_id": int(check_run_id) if check_run_id else None,
		"details_url": details_url,
		"run_id": run_id,
		"head_sha": head_sha,
	}


def _last_line(body: Any) -> str:
	if not isinstance(body, str):
		return ""
	lines = [line.strip() for line in body.strip().split("\n")]
	return lines[-1] if lines else ""


def _trusted_comments(comments: Any, pipeline_login: str) -> list[dict[str, Any]]:
	if not isinstance(comments, list) or not pipeline_login:
		return []
	return [
		comment for comment in comments
		if isinstance(comment, dict) and isinstance(comment.get("user"), dict) and comment["user"].get("login") == pipeline_login
	]


def find_resolved_marker(comments: Any, pipeline_login: str, issue_number: int) -> dict[str, Any] | None:
	"""The pipeline account's resolved marker for this issue, else None."""
	for comment in _trusted_comments(comments, pipeline_login):
		match = RESOLVED_MARKER_RE.match(_last_line(comment.get("body")))
		if match and int(match.group(1)) == issue_number:
			return {"pr": int(match.group(2)), "sha": match.group(3), "comment_id": comment.get("id")}
	return None


def traceback_jobs_posted(comments: Any, pipeline_login: str, issue_number: int) -> set[int]:
	posted: set[int] = set()
	for comment in _trusted_comments(comments, pipeline_login):
		match = TRACEBACK_MARKER_RE.match(_last_line(comment.get("body")))
		if match and int(match.group(1)) == issue_number:
			posted.add(int(match.group(2)))
	return posted


def classify_check_runs(payload: Any, expected_sha: str) -> str:
	"""``success`` only when every latest run completed with success on expected_sha."""
	if not isinstance(payload, dict) or not isinstance(payload.get("check_runs"), list):
		return "unreadable"
	runs = payload["check_runs"]
	if not runs:
		return "missing"
	total = payload.get("total_count")
	if not isinstance(total, int) or total > len(runs):
		return "incomplete"
	for run in runs:
		if not isinstance(run, dict):
			return "unreadable"
		if run.get("head_sha") != expected_sha:
			return "sha_mismatch"
		if run.get("status") != "completed":
			return "pending"
		if run.get("conclusion") != "success":
			conclusion = run.get("conclusion")
			return conclusion if isinstance(conclusion, str) and conclusion else "unknown"
	return "success"


def shard_jobs_resolved(failed_job_names: list[str], base_jobs: Any) -> bool:
	"""Every failed PR job name appears exactly once in base_jobs, with success."""
	if not failed_job_names or not isinstance(base_jobs, list):
		return False
	for name in failed_job_names:
		matches = [job for job in base_jobs if isinstance(job, dict) and job.get("name") == name]
		if len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success":
			return False
	return True


def _jobs_of(payload: Any) -> list[dict[str, Any]]:
	if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
		raise GhApiError("jobs_unreadable")
	total = payload.get("total_count")
	if isinstance(total, int) and total > len(payload["jobs"]):
		raise GhApiError("jobs_incomplete")
	return [job for job in payload["jobs"] if isinstance(job, dict)]


def _failed_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
	failed = []
	for job in jobs:
		if job.get("conclusion") in FAILED_JOB_CONCLUSIONS and isinstance(job.get("name"), str) and isinstance(job.get("id"), int):
			failed.append({"id": job["id"], "name": job["name"], "conclusion": job["conclusion"]})
	return failed


def verify_resolution(repo: str, triage: dict[str, Any], default_branch: str) -> dict[str, Any]:
	"""Verify from GitHub that the PR merged and the failure is fixed on default_branch."""
	verdict: dict[str, Any] = {"status": "unverified", "pr": triage["pr"], "check_name": triage["check_name"], "failed_jobs": []}

	def _fail(reason: str) -> dict[str, Any]:
		verdict["reason"] = reason
		return verdict

	# An unreadable PR still lets the failed jobs be collected first, so the
	# unverified path can post the failing job's traceback.
	try:
		pr = _gh_api([f"repos/{repo}/pulls/{triage['pr']}"])
	except GhApiError:
		pr = None
	pr_readable = isinstance(pr, dict) and pr.get("number") == triage["pr"]
	if not pr_readable:
		pr = {}
	head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
	base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
	head_repo = (head.get("repo") or {}).get("full_name") if isinstance(head.get("repo"), dict) else None
	base_repo = (base.get("repo") or {}).get("full_name") if isinstance(base.get("repo"), dict) else None

	workflow_id = None
	if triage.get("check_run_id") is None:
		# Workflow-run triage: the check name is the workflow name.
		run_id = triage.get("run_id")
		if not run_id:
			return _fail("run_id_unavailable")
		try:
			run = _gh_api([f"repos/{repo}/actions/runs/{run_id}"])
		except GhApiError:
			run = None
		if not isinstance(run, dict):
			# Without run metadata, list the run's jobs for the traceback only
			# when every job's own fields bind it to this run, workflow and a
			# known head: the failed run's head from the trusted triage header
			# (the PR may have advanced since) or the PR's current head. Else
			# post nothing. The result stays unverified either way.
			try:
				fallback_jobs = _jobs_of(_gh_api([f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"]))
			except GhApiError:
				fallback_jobs = []
			known_heads = {
				sha for sha in (triage.get("head_sha"), head.get("sha"))
				if isinstance(sha, str) and SHA_RE.match(sha)
			}
			if fallback_jobs and known_heads and all(
				job.get("run_id") == run_id and job.get("workflow_name") == triage["check_name"] and job.get("head_sha") in known_heads
				for job in fallback_jobs
			):
				verdict["failed_jobs"] = _failed_jobs(fallback_jobs)
			return _fail("run_unreadable")
		run_repo = (run.get("repository") or {}).get("full_name") if isinstance(run.get("repository"), dict) else None
		run_prs = [item.get("number") for item in run.get("pull_requests") or [] if isinstance(item, dict)]
		# GitHub empties a run's pull_requests once the PR merges, and the PR head
		# moves on when commits follow the failure, so a run on the trusted
		# header's failed head and on the PR's head branch also binds it (#7332).
		triage_head = triage.get("head_sha")
		on_triage_head = (
			isinstance(triage_head, str) and bool(SHA_RE.match(triage_head)) and run.get("head_sha") == triage_head
			and isinstance(head.get("ref"), str) and bool(head.get("ref")) and run.get("head_branch") == head.get("ref")
		)
		if (
			run.get("id") != run_id or run.get("name") != triage["check_name"]
			or not isinstance(run_repo, str) or run_repo.lower() != repo.lower()
			or run.get("event") != "pull_request"
			or not (
				triage["pr"] in run_prs
				or (isinstance(head.get("sha"), str) and run.get("head_sha") == head.get("sha"))
				or on_triage_head
			)
			or not isinstance(run.get("workflow_id"), int)
		):
			return _fail("run_identity_mismatch")
		workflow_id = run["workflow_id"]
		try:
			pr_jobs = _jobs_of(_gh_api([f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"]))
		except GhApiError:
			return _fail("run_jobs_unreadable")
		verdict["failed_jobs"] = _failed_jobs(pr_jobs)
	else:
		# A check-run id is an Actions job id only for a github-actions check;
		# fetch_failing_tracebacks reads the check run to pick the log source.
		verdict["failed_jobs"] = [{"id": triage["check_run_id"], "name": triage["check_name"], "conclusion": "failure", "source": "check_run"}]

	if not pr_readable:
		return _fail("pr_unreadable")
	if pr.get("merged") is True:
		verdict["pr_state"] = "merged"
	elif pr.get("state") in ("open", "closed"):
		verdict["pr_state"] = pr["state"]
	if pr.get("merged") is not True and pr.get("state") == "open":
		# Issue #8671: an open PR (for example an orchestrator integration
		# PR) whose head moved past the failure can resolve on that head.
		return _verify_open_head(repo, triage, pr, head_repo, base_repo, workflow_id, verdict)
	merge_sha = pr.get("merge_commit_sha")
	if pr.get("merged") is not True or not isinstance(merge_sha, str) or not SHA_RE.match(merge_sha):
		return _fail("pr_not_merged")
	if not isinstance(head_repo, str) or not isinstance(base_repo, str) or head_repo.lower() != base_repo.lower() or base_repo.lower() != repo.lower():
		return _fail("pr_cross_repository")
	verdict["merge_sha"] = merge_sha
	branch_path = quote(default_branch, safe="")

	def _default_branch_evidence() -> str | None:
		"""Evidence commit on the default branch, or None after _fail."""
		if workflow_id is None:
			try:
				tip = _gh_api([f"repos/{repo}/commits/{branch_path}"])
			except GhApiError:
				_fail("default_tip_unreadable")
				return None
			evidence = tip.get("sha") if isinstance(tip, dict) else None
			if not isinstance(evidence, str) or not SHA_RE.match(evidence):
				_fail("default_tip_unreadable")
				return None
			try:
				checks = _gh_api([
					f"repos/{repo}/commits/{evidence}/check-runs?check_name={quote(triage['check_name'], safe='')}&filter=latest&per_page=100",
				])
			except GhApiError:
				_fail("default_checks_unreadable")
				return None
			outcome = classify_check_runs(checks, evidence)
			if outcome != "success":
				_fail(f"default_check_{outcome}")
				return None
			verdict["evidence_url"] = f"https://github.com/{repo}/commit/{evidence}"
			verdict["matched"] = [f"{triage['check_name']}: success"]
			return evidence
		if not verdict["failed_jobs"]:
			_fail("no_failed_jobs")
			return None
		try:
			runs = _gh_api([
				f"repos/{repo}/actions/workflows/{workflow_id}/runs?branch={quote(default_branch, safe='')}&event=push&status=completed&per_page=1",
			])
		except GhApiError:
			_fail("default_run_unreadable")
			return None
		base_runs = runs.get("workflow_runs") if isinstance(runs, dict) else None
		if not isinstance(base_runs, list) or not base_runs or not isinstance(base_runs[0], dict):
			_fail("no_completed_default_run")
			return None
		base_run = base_runs[0]
		evidence = base_run.get("head_sha")
		if (
			base_run.get("status") != "completed" or base_run.get("conclusion") != "success"
			or base_run.get("head_branch") != default_branch or base_run.get("event") != "push"
			or not isinstance(base_run.get("id"), int)
			or not isinstance(evidence, str) or not SHA_RE.match(evidence)
		):
			_fail("default_run_not_success")
			return None
		try:
			base_jobs = _jobs_of(_gh_api([f"repos/{repo}/actions/runs/{base_run['id']}/jobs?filter=latest&per_page=100"]))
		except GhApiError:
			_fail("default_run_jobs_unreadable")
			return None
		names = [job["name"] for job in verdict["failed_jobs"]]
		if not shard_jobs_resolved(names, base_jobs):
			_fail("failed_jobs_not_resolved")
			return None
		verdict["evidence_url"] = base_run.get("html_url") if isinstance(base_run.get("html_url"), str) else f"https://github.com/{repo}/actions/runs/{base_run['id']}"
		verdict["matched"] = [f"{name}: success" for name in names]
		return evidence

	def _integration_evidence(branch: str, project_issue: int) -> tuple[str, str] | None:
		"""(compare base, evidence commit) for a PR merged into an orchestrator
		project branch, or None after _fail (issue #8685).

		The branch name alone is never trusted: issue <project_issue> must carry
		ai:orchestrator-tracking, and the newest PR from the branch into the
		default branch must have a same-repository head.
		"""
		try:
			tracking = _gh_api([f"repos/{repo}/issues/{project_issue}"])
		except GhApiError:
			_fail("integration_issue_unreadable")
			return None
		if not isinstance(tracking, dict) or tracking.get("number") != project_issue:
			_fail("integration_issue_unreadable")
			return None
		if "pull_request" in tracking or TRACKING_LABEL not in _label_names(tracking):
			_fail("integration_base_untrusted")
			return None
		owner = repo.split("/", 1)[0]
		try:
			listed = _gh_api([
				f"repos/{repo}/pulls?head={quote(owner, safe='')}:{quote(branch, safe='')}&base={quote(default_branch, safe='')}&state=all&per_page=5",
			])
		except GhApiError:
			_fail("integration_pr_unreadable")
			return None
		if not isinstance(listed, list):
			_fail("integration_pr_unreadable")
			return None
		candidates = [
			item for item in listed
			if isinstance(item, dict) and isinstance(item.get("number"), int) and isinstance(item.get("created_at"), str)
			and isinstance(item.get("head"), dict) and item["head"].get("ref") == branch
			and isinstance(item.get("base"), dict) and item["base"].get("ref") == default_branch
		]
		if not candidates:
			_fail("integration_pr_missing")
			return None
		integration = max(candidates, key=lambda item: item["created_at"])
		integration_number = integration["number"]
		int_head = integration["head"]
		int_head_repo = int_head["repo"].get("full_name") if isinstance(int_head.get("repo"), dict) else None
		if not isinstance(int_head_repo, str) or int_head_repo.lower() != repo.lower():
			_fail("integration_pr_cross_repository")
			return None
		int_head_sha = int_head.get("sha")
		if not isinstance(int_head_sha, str) or not SHA_RE.match(int_head_sha):
			_fail("integration_pr_unreadable")
			return None
		if not integration.get("merged_at") and integration.get("state") != "open":
			_fail("integration_pr_closed")
			return None
		# Named as evidence only once the integration PR passed its trust checks.
		verdict["evidence_ref"] = f"integration_pr:{integration_number}"
		verdict["integration_pr"] = integration_number

		if integration.get("merged_at"):
			int_merge_sha = integration.get("merge_commit_sha")
			if not isinstance(int_merge_sha, str) or not SHA_RE.match(int_merge_sha):
				_fail("integration_pr_unreadable")
				return None
			evidence = _default_branch_evidence()
			if evidence is None:
				return None
			# The project branch held the triaged PR's merge commit ...
			try:
				held = _gh_api([f"repos/{repo}/compare/{merge_sha}...{int_head_sha}"])
			except GhApiError:
				_fail("integration_compare_unreadable")
				return None
			held_status = held.get("status") if isinstance(held, dict) else None
			if held_status not in ("ahead", "identical"):
				_fail(f"integration_compare_{held_status if isinstance(held_status, str) and held_status.isalpha() else 'unreadable'}")
				return None
			verdict["integration_compare_status"] = held_status
			# ... and the final compare checks the squash reached the default tip.
			return int_merge_sha, evidence

		evidence = int_head_sha
		if workflow_id is None:
			try:
				checks = _gh_api([
					f"repos/{repo}/commits/{evidence}/check-runs?check_name={quote(triage['check_name'], safe='')}&filter=latest&per_page=100",
				])
			except GhApiError:
				_fail("integration_checks_unreadable")
				return None
			outcome = classify_check_runs(checks, evidence)
			if outcome != "success":
				_fail(f"integration_check_{outcome}")
				return None
			verdict["evidence_url"] = f"https://github.com/{repo}/commit/{evidence}"
			verdict["matched"] = [f"{triage['check_name']}: success"]
			return merge_sha, evidence
		if not verdict["failed_jobs"]:
			_fail("no_failed_jobs")
			return None
		try:
			runs = _gh_api([
				f"repos/{repo}/actions/workflows/{workflow_id}/runs?event=pull_request&head_sha={evidence}&status=completed&per_page=1",
			])
		except GhApiError:
			_fail("integration_run_unreadable")
			return None
		int_runs = runs.get("workflow_runs") if isinstance(runs, dict) else None
		if not isinstance(int_runs, list) or not int_runs or not isinstance(int_runs[0], dict):
			_fail("no_completed_integration_run")
			return None
		int_run = int_runs[0]
		if (
			int_run.get("status") != "completed" or int_run.get("conclusion") != "success"
			or int_run.get("event") != "pull_request" or int_run.get("head_sha") != evidence
			or int_run.get("head_branch") != branch or not isinstance(int_run.get("id"), int)
		):
			_fail("integration_run_not_success")
			return None
		try:
			int_jobs = _jobs_of(_gh_api([f"repos/{repo}/actions/runs/{int_run['id']}/jobs?filter=latest&per_page=100"]))
		except GhApiError:
			_fail("integration_run_jobs_unreadable")
			return None
		names = [job["name"] for job in verdict["failed_jobs"]]
		if not shard_jobs_resolved(names, int_jobs):
			_fail("failed_jobs_not_resolved")
			return None
		verdict["evidence_url"] = int_run.get("html_url") if isinstance(int_run.get("html_url"), str) else f"https://github.com/{repo}/actions/runs/{int_run['id']}"
		verdict["matched"] = [f"{name}: success" for name in names]
		return merge_sha, evidence

	# A PR merged into an orchestrator project branch reaches the default
	# branch only inside the project's squash commit, so its own merge commit
	# is never an ancestor of the default tip (issue #8685). Every other base
	# keeps the default-branch path unchanged.
	base_ref = base.get("ref")
	integration_match = (
		INTEGRATION_BASE_RE.match(base_ref) if isinstance(base_ref, str) and base_ref != default_branch else None
	)
	if integration_match is None:
		verdict["evidence_ref"] = "default"
		compare_base = merge_sha
		evidence_sha = _default_branch_evidence()
		if evidence_sha is None:
			return verdict
	else:
		bound = _integration_evidence(base_ref, int(integration_match.group(1)))
		if bound is None:
			return verdict
		compare_base, evidence_sha = bound

	try:
		compare = _gh_api([f"repos/{repo}/compare/{compare_base}...{evidence_sha}"])
	except GhApiError:
		return _fail("compare_unreadable")
	compare_status = compare.get("status") if isinstance(compare, dict) else None
	if compare_status not in ("ahead", "identical"):
		return _fail(f"compare_{compare_status if isinstance(compare_status, str) and compare_status.isalpha() else 'unreadable'}")
	verdict.update({
		"status": "resolved", "reason": "verified", "path": "merged",
		"evidence_sha": evidence_sha, "compare_status": compare_status,
	})
	return verdict


def _verify_open_head(
	repo: str, triage: dict[str, Any], pr: dict[str, Any], head_repo: Any, base_repo: Any,
	workflow_id: int | None, verdict: dict[str, Any],
) -> dict[str, Any]:
	"""Resolve an open PR whose current head passes what failed on an older head."""

	def _fail(reason: str) -> dict[str, Any]:
		verdict["reason"] = reason
		return verdict

	if not isinstance(head_repo, str) or not isinstance(base_repo, str) or head_repo.lower() != base_repo.lower() or base_repo.lower() != repo.lower():
		return _fail("pr_cross_repository")
	head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
	current = head.get("sha")
	if not isinstance(current, str) or not SHA_RE.match(current):
		return _fail("open_head_unreadable")
	triage_head = triage.get("head_sha")
	if not isinstance(triage_head, str) or not SHA_RE.match(triage_head):
		return _fail("triage_head_unavailable")
	if current == triage_head:
		return _fail("open_head_unchanged")

	if workflow_id is None:
		try:
			checks = _gh_api([
				f"repos/{repo}/commits/{current}/check-runs?check_name={quote(triage['check_name'], safe='')}&filter=latest&per_page=100",
			])
		except GhApiError:
			return _fail("open_head_checks_unreadable")
		outcome = classify_check_runs(checks, current)
		if outcome != "success":
			return _fail(f"open_head_check_{outcome}")
		verdict["evidence_url"] = f"https://github.com/{repo}/commit/{current}"
		verdict["matched"] = [f"{triage['check_name']}: success"]
	else:
		if not verdict["failed_jobs"]:
			return _fail("no_failed_jobs")
		try:
			# No status filter: a newer run still in progress must block.
			runs = _gh_api([
				f"repos/{repo}/actions/workflows/{workflow_id}/runs?head_sha={current}&event=pull_request&per_page=1",
			])
		except GhApiError:
			return _fail("open_head_run_unreadable")
		head_runs = runs.get("workflow_runs") if isinstance(runs, dict) else None
		if not isinstance(head_runs, list):
			return _fail("open_head_run_unreadable")
		head_run = head_runs[0] if head_runs else None
		if (
			not isinstance(head_run, dict) or head_run.get("head_sha") != current
			or head_run.get("event") != "pull_request" or head_run.get("workflow_id") != workflow_id
			or not isinstance(head_run.get("id"), int)
		):
			return _fail("open_head_run_missing")
		if head_run.get("status") != "completed":
			return _fail("open_head_run_pending")
		if head_run.get("conclusion") != "success":
			return _fail("open_head_run_not_success")
		try:
			head_jobs = _jobs_of(_gh_api([f"repos/{repo}/actions/runs/{head_run['id']}/jobs?filter=latest&per_page=100"]))
		except GhApiError:
			return _fail("open_head_run_jobs_unreadable")
		names = [job["name"] for job in verdict["failed_jobs"]]
		if not shard_jobs_resolved(names, head_jobs):
			return _fail("failed_jobs_not_resolved")
		verdict["evidence_url"] = head_run.get("html_url") if isinstance(head_run.get("html_url"), str) else f"https://github.com/{repo}/actions/runs/{head_run['id']}"
		verdict["matched"] = [f"{name}: success" for name in names]

	verdict.update({
		"status": "resolved", "reason": "verified", "path": "open_head",
		"evidence_sha": current, "triage_head_sha": triage_head,
	})
	return verdict


def _neutralize(text: str) -> str:
	text = heal.redact_secrets(text)
	text, _ = heal.neutralize_untrusted_routing(text)
	return text.replace("<!--", "&lt;!--")


def _fence(text: str) -> str:
	longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
	return "`" * max(3, longest + 1)


def _cap_long_lines(text: str) -> str:
	"""Cut every line over MAX_LOG_LINE_CHARS characters (issue #8799).

	A local copy of the collector's rule and marker: clarify stages only listed
	helper files, so this module cannot import the collector.
	"""
	capped = []
	for line in text.split("\n"):
		if len(line) > MAX_LOG_LINE_CHARS:
			line = line[:MAX_LOG_LINE_CHARS] + f" [\u2026 {len(line) - MAX_LOG_LINE_CHARS} chars truncated]"
		capped.append(line)
	return "\n".join(capped)


def _first_match(lines: list[str], pattern: re.Pattern[str]) -> int | None:
	return next((index for index, line in enumerate(lines) if pattern.search(line)), None)


def _forward_window(lines: list[str], start: int) -> str:
	excerpt = "\n".join(lines[start:start + MAX_EXCERPT_LINES])
	encoded = excerpt.encode("utf-8")[:MAX_EXCERPT_BYTES]
	return encoded.decode("utf-8", errors="ignore")


def _step_error_window(lines: list[str], index: int) -> str:
	"""Lines ending at the step error plus up to STEP_ERROR_AFTER_LINES, trimmed
	from the front to MAX_EXCERPT_BYTES; the error line itself always survives."""
	after = min(STEP_ERROR_AFTER_LINES, len(lines) - index - 1)
	before = lines[max(0, index + 1 + after - MAX_EXCERPT_LINES):index]
	error_line = lines[index]
	trailing = lines[index + 1:index + 1 + after]
	while trailing and len("\n".join([error_line, *trailing]).encode("utf-8")) > MAX_EXCERPT_BYTES:
		trailing.pop()
	if len(error_line.encode("utf-8")) > MAX_EXCERPT_BYTES:
		return error_line.encode("utf-8")[-MAX_EXCERPT_BYTES:].decode("utf-8", errors="ignore")
	window = [error_line, *trailing]
	used = len("\n".join(window).encode("utf-8"))
	lead: list[str] = []
	for line in reversed(before):
		cost = len(line.encode("utf-8")) + 1
		if used + cost > MAX_EXCERPT_BYTES:
			break
		lead.append(line)
		used += cost
	lead.reverse()
	return "\n".join(lead + window)


def _excerpt_with_anchor(filtered: str) -> tuple[str, str]:
	"""(excerpt, anchor) for the strongest failure signal; ("", "") if none."""
	lines = filtered.split("\n")
	index = _first_match(lines, TEST_FAILURE_RE)
	if index is not None:
		start = max(0, index - TEST_FAILURE_LEAD_LINES)
		# Drop lead lines until the matched failure line fits MAX_EXCERPT_BYTES,
		# so long lead lines can never push it out of the excerpt.
		while start < index and len("\n".join(lines[start:index + 1]).encode("utf-8")) > MAX_EXCERPT_BYTES:
			start += 1
		return _forward_window(lines, start), "test_failure"
	index = _first_match(lines, STEP_ERROR_RE)
	if index is not None:
		return _step_error_window(lines, index), "step_error"
	index = _first_match(lines, PY_TRACEBACK_RE)
	if index is not None:
		return _forward_window(lines, index), "traceback"
	index = _first_match(lines, TRACEBACK_START_RE)
	if index is not None:
		return _forward_window(lines, index), "legacy"
	return "", ""


def _excerpt(filtered: str) -> str:
	return _excerpt_with_anchor(filtered)[0]


def _actions_job_log(repo: str, job_id: int) -> Any:
	"""Raw Actions job log text.

	Job logs carry ANSI colour codes; gh refuses such a body without
	--allow-escape-sequences. The flag goes first so the path stays last.
	"""
	return _gh_api(["--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs"], raw=True)


def _check_run_log_text(repo: str, check_run_id: int) -> str:
	"""Log text for a check run: the Actions job log for a github-actions check,
	else the check run's own output (a non-Actions id is not an Actions job id)."""
	check_run = _gh_api([f"repos/{repo}/check-runs/{check_run_id}"])
	if not isinstance(check_run, dict) or check_run.get("id") != check_run_id:
		raise GhApiError("check_run_unreadable")
	app = check_run.get("app") if isinstance(check_run.get("app"), dict) else {}
	if app.get("slug") == "github-actions":
		return _actions_job_log(repo, check_run_id)
	output = check_run.get("output") if isinstance(check_run.get("output"), dict) else {}
	parts = [output.get(key) for key in ("title", "summary", "text")]
	return "\n".join(part for part in parts if isinstance(part, str) and part)


def fetch_failing_tracebacks(repo: str, failed_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Best-effort traceback excerpts for at most three failed jobs; never raises."""
	results: list[dict[str, Any]] = []
	for job in failed_jobs[:MAX_TRACEBACK_JOBS]:
		try:
			if job.get("source") == "check_run":
				log_text = _check_run_log_text(repo, job["id"])
			else:
				log_text = _actions_job_log(repo, job["id"])
		except GhApiError:
			continue
		if not isinstance(log_text, str) or not log_text:
			continue
		# Redact the full text before any line is cut, so a cut can never leave
		# a partial token that no longer matches a redaction shape (#8799).
		filtered = heal.filter_log(_cap_long_lines(heal.redact_secrets(log_text)))
		tests = heal.extract_failing_tests(filtered)
		excerpt, anchor = _excerpt_with_anchor(filtered)
		if not excerpt and not tests.get("names") and not tests.get("files"):
			continue
		results.append({
			"id": job["id"], "name": job.get("name", ""), "tests": tests, "excerpt": excerpt, "excerpt_anchor": anchor,
		})
	return results


def render_evidence_comment(issue_number: int, verdict: dict[str, Any]) -> str:
	if verdict.get("path") == "open_head":
		lines = [
			"## Check-triage failure already resolved",
			"",
			f"The PR this issue was filed for, #{verdict['pr']}, is still open, and its current head "
			f"`{verdict['evidence_sha']}` passes the failing check. This issue's failure was on the older head "
			f"`{verdict.get('triage_head_sha', '')}`.",
			"",
			f"- **Evidence commit (PR head):** `{verdict['evidence_sha']}` ({verdict['evidence_url']})",
		]
		for item in verdict.get("matched") or []:
			lines.append(f"- **Passed:** `{_neutralize(item).replace('`', chr(39))}`")
		lines += [
			"",
			"Closing this issue as completed. The PR is left unchanged, and no clarification answer or implementation is needed.",
			"",
			f"<!-- ai:check-triage-resolved:v1 issue={issue_number} pr={verdict['pr']} sha={verdict['evidence_sha']} -->",
		]
		return "\n".join(lines)
	evidence_ref = verdict.get("evidence_ref") or "default"
	if evidence_ref == "default":
		summary = (
			f"The PR this issue was filed for, #{verdict['pr']}, has merged (merge commit `{verdict['merge_sha']}`), "
			"and the failing check now passes on the default branch at a commit that contains that merge."
		)
	elif verdict.get("integration_compare_status"):
		summary = (
			f"The PR this issue was filed for, #{verdict['pr']}, merged into an orchestrator project branch "
			f"(merge commit `{verdict['merge_sha']}`). That branch's integration PR #{verdict.get('integration_pr')} "
			"has merged too, and the failing check now passes on the default branch at a commit that contains it."
		)
	else:
		summary = (
			f"The PR this issue was filed for, #{verdict['pr']}, merged into an orchestrator project branch "
			f"(merge commit `{verdict['merge_sha']}`), and the failing check now passes on the head of that branch's "
			f"integration PR #{verdict.get('integration_pr')}, which contains that merge."
		)
	lines = [
		"## Check-triage failure already resolved",
		"",
		summary,
		"",
		f"- **Evidence commit:** `{verdict['evidence_sha']}` ({verdict['evidence_url']})",
	]
	if verdict.get("integration_compare_status"):
		lines.append(f"- **Compare (merge commit...integration PR head):** `{verdict['integration_compare_status']}`")
		lines.append(f"- **Compare (integration PR merge commit...evidence commit):** `{verdict['compare_status']}`")
	else:
		lines.append(f"- **Compare (merge commit...evidence commit):** `{verdict['compare_status']}`")
	lines.append(f"- **Evidence ref:** `{evidence_ref}`")
	for item in verdict.get("matched") or []:
		lines.append(f"- **Passed:** `{_neutralize(item).replace('`', chr(39))}`")
	lines += [
		"",
		"Closing this issue as completed. The merged PR is left unchanged, and no clarification answer or implementation is needed.",
		"",
		f"<!-- ai:check-triage-resolved:v1 issue={issue_number} pr={verdict['pr']} sha={verdict['evidence_sha']} -->",
	]
	return "\n".join(lines)


def render_traceback_comment(issue_number: int, traceback: dict[str, Any], reason: str) -> str:
	tests = traceback.get("tests") or {}
	name = _neutralize(str(traceback.get("name", ""))).replace("`", "'")
	lines = [
		f"## Failing job traceback: `{name}`",
		"",
		f"The check-triage gate could not verify that this failure is fixed on the default branch (`{reason}`), "
		"so the normal fix path continues. This is the failing job's log evidence (untrusted data, redacted).",
		"",
	]
	for label, key in (("Failing tests", "names"), ("Test files", "files")):
		values = [_neutralize(str(value)).replace("`", "'") for value in tests.get(key) or []]
		if values:
			lines.append(f"- **{label}:** " + ", ".join(f"`{value}`" for value in values))
	excerpt = _neutralize(traceback.get("excerpt") or "")
	if excerpt:
		anchor = traceback.get("excerpt_anchor")
		if anchor in EXCERPT_ANCHORS:
			lines.append(f"- **Excerpt anchor:** `{anchor}`")
		fence = _fence(excerpt)
		lines += ["", fence + "text", excerpt, fence]
	lines += ["", f"<!-- ai:check-triage-traceback:v1 issue={issue_number} job={traceback['id']} -->"]
	return "\n".join(lines)


def _log(**fields: Any) -> None:
	parts = " ".join(f"{key}={'' if value is None else value}" for key, value in fields.items())
	print(f"CHECK_TRIAGE_RESOLVED {parts}", file=sys.stderr)


def _list_comments(repo: str, issue_number: int) -> list[dict[str, Any]]:
	pages = _gh_api(["--paginate", "--slurp", f"repos/{repo}/issues/{issue_number}/comments?per_page=100"])
	if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
		raise GhApiError("comments_unreadable")
	return [comment for page in pages for comment in page]


def gate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
	issue_number = args.issue_number
	result: dict[str, Any] = {"issue": issue_number, "writes": [], "traceback_posted": []}

	def _write(method: str, path: str, payload: dict[str, Any]) -> None:
		result["writes"].append({"method": method, "path": path, "payload": payload})
		if not args.dry_run:
			_gh_api(["-X", method, path], input_obj=payload)

	if os.environ.get("CHECK_TRIAGE_RESOLVED_GATE_ENABLED", "true").strip().lower() == "false":
		result.update({"status": "disabled", "reason": "disabled"})
		return result, 0
	try:
		issue = json.loads(Path(args.issue_json).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		result.update({"status": "unverified", "reason": "issue_json_unreadable"})
		return result, 0
	triage = parse_triage_issue(issue, args.repo)
	if triage is None:
		result.update({"status": "not_triage", "reason": "not_trusted_check_triage"})
		return result, 0
	if (
		issue.get("number") != issue_number or "pull_request" in issue
		or not isinstance(issue.get("repository_url"), str)
		or issue["repository_url"].lower() != f"https://api.github.com/repos/{args.repo}".lower()
	):
		result.update({"status": "unverified", "reason": "issue_identity_mismatch"})
		return result, 0
	if "malformed" in triage:
		result.update({"status": "unverified", "reason": f"malformed_{triage['malformed']}"})
		return result, 0
	result.update({"pr": triage["pr"], "check_name": triage["check_name"]})

	try:
		user = _gh_api(["user"])
		pipeline_login = user.get("login") if isinstance(user, dict) else None
		if not isinstance(pipeline_login, str) or not pipeline_login:
			raise GhApiError("identity_unavailable")
		comments = _list_comments(args.repo, issue_number)
	except GhApiError:
		result.update({"status": "unverified", "reason": "identity_or_comments_unavailable"})
		return result, 0

	issue_path = f"repos/{args.repo}/issues/{issue_number}"
	marker = find_resolved_marker(comments, pipeline_login, issue_number)
	if marker is not None:
		result.update({"status": "already_resolved", "reason": "marker_present", "pr": marker["pr"], "evidence_sha": marker["sha"]})
		try:
			if CLOSED_LABEL not in _label_names(issue):
				_write("POST", f"{issue_path}/labels", {"labels": [CLOSED_LABEL]})
			if issue.get("state") != "closed":
				_write("PATCH", issue_path, {"state": "closed", "state_reason": "completed"})
		except GhApiError:
			result.update({"status": "write_failed", "reason": "write_failed"})
			return result, 3
		return result, 0

	verdict = verify_resolution(args.repo, triage, args.default_branch)
	result.update({key: value for key, value in verdict.items() if key not in ("status", "reason")})
	result.update({"status": verdict["status"], "reason": verdict.get("reason")})
	if verdict["status"] == "resolved":
		try:
			_write("POST", f"{issue_path}/comments", {"body": render_evidence_comment(issue_number, verdict)})
			if CLOSED_LABEL not in _label_names(issue):
				_write("POST", f"{issue_path}/labels", {"labels": [CLOSED_LABEL]})
			_write("PATCH", issue_path, {"state": "closed", "state_reason": "completed"})
		except GhApiError:
			result.update({"status": "write_failed", "reason": "write_failed"})
			return result, 3
		return result, 0

	# Unverified: keep the normal fix path and attach the failing job's traceback.
	if verdict.get("reason") in ("run_identity_mismatch",):
		return result, 0
	already = traceback_jobs_posted(comments, pipeline_login, issue_number)
	pending = [job for job in verdict.get("failed_jobs") or [] if job.get("id") not in already]
	for traceback in fetch_failing_tracebacks(args.repo, pending):
		try:
			_write("POST", f"{issue_path}/comments", {"body": render_traceback_comment(issue_number, traceback, str(verdict.get("reason")))})
			result["traceback_posted"].append(traceback["id"])
		except GhApiError:
			print(f"::warning::Could not post the failing-job traceback for issue #{issue_number}", file=sys.stderr)
	return result, 0


def _cmd_gate(args: argparse.Namespace) -> int:
	result, rc = gate(args)
	_log(
		issue=args.issue_number, pr=result.get("pr"), outcome=result.get("status"), reason=result.get("reason"),
		evidence_sha=result.get("evidence_sha"), path=result.get("path"), traceback_jobs=",".join(str(job) for job in result.get("traceback_posted") or []),
		evidence_ref=result.get("evidence_ref"),
	)
	print(json.dumps(result, sort_keys=True))
	return rc


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	p_gate = sub.add_parser("gate", help="close an ai:check-triage issue whose failure is verified fixed")
	p_gate.add_argument("--issue-json", required=True)
	p_gate.add_argument("--repo", required=True)
	p_gate.add_argument("--issue-number", type=int, required=True)
	p_gate.add_argument("--default-branch", required=True)
	p_gate.add_argument("--dry-run", action="store_true")
	args = parser.parse_args(argv)
	if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", args.default_branch) or ".." in args.default_branch:
		print(json.dumps({"status": "unverified", "reason": "invalid_arguments"}))
		return 2
	return _cmd_gate(args)


if __name__ == "__main__":
	raise SystemExit(main())

#!/usr/bin/env python3
"""Deterministic "is the wait over?" check for the Sonnet check-in sessions.

Used by the `/implement-plan-claude` checker session and the CLAUDE.md §26
post-push PR status check-in. The checker model only runs this script and
acts on its one-line verdict, so no time arithmetic or label judgement is
left to the model.

Input (exactly one mode):

  --pr N [--terminal-only]   a pull request
  --run ID                   a workflow run
  --issues N[,N...]          a set of issues (e.g. ai:security follow-ups)

plus `--repo OWNER/REPO` (required).

Output: one JSON object on stdout, e.g.

  {"done": true, "reason": "PR #12 merged at 2026-09-23T04:00:00Z", ...}
  {"done": false, "reason": "PR #12 open, healthy", ...}
  {"done": false, "error": "gh api repos/o/r/pulls/12 failed: HTTP 403"}

Exit status: 0 when a verdict was reached (done or not), 2 when a read
failed (the JSON then carries `error` and `done` is false).

"Done waiting" rules (match `/implement-plan-claude` → Check-in Loop):

  * PR: merged; or closed; or labelled ai:review-blocked /
    ai:review-autofix-failed / ai:needs-human; or stuck — `mergeable_state`
    is `dirty` or a check run on the head commit failed, AND the head commit
    is older than --stuck-hours (default 6), AND no workflow run on the head
    branch is queued or in progress. With --terminal-only (the §26 status
    check-in) only merged / closed count.
  * Claude-fixer PR (head ref starts with `claude/implement-plan-`, where
    review_autofix.yml runs the reviewer panel but hands the fixes to the
    Claude session): also done, with `state: review-round` or
    `state: conflict`, when a trusted `ai:claude-fixer-handoff:v1` comment
    names the current head and no `ai:claude-fixer-verdict:v1` comment
    answers it yet; and done with `state: conflict` as soon as the PR is
    conflicted with no workflow run active (no 6-hour wait: no review run
    fires for a conflict the base branch caused).
  * Run: `status` is `completed` (any conclusion). `state` is `completed`
    only for a `success` conclusion and `failed` for any other, so a checker
    routes a failed run to its block stage.
  * Issues: every issue is closed or labelled ai:merged.

API budget (CLAUDE.md §15): REST only, never GraphQL. PR mode issues 1 call
(`pulls/N`), one call per 100 check runs when the PR is not conflicted, and
at most 3 further calls when a failure is old (head commit, queued runs,
in-progress runs). A Claude-fixer PR adds one call per 100 PR comments and,
when conflicted, the 2 active-run reads. Run mode issues 1 call. Issues mode issues one call per
issue; the checker lists at most the few follow-ups one security cycle opens.
Every call goes through `gh api`, which in Claude Code on the web is
authenticated by the session's agent proxy.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys

BLOCKING_LABELS = ("ai:review-blocked", "ai:review-autofix-failed", "ai:needs-human")
FAILED_CHECK_CONCLUSIONS = ("failure", "timed_out", "action_required", "startup_failure")
MERGED_ISSUE_LABEL = "ai:merged"
SUCCESSFUL_RUN_CONCLUSIONS = ("success",)
DEFAULT_STUCK_HOURS = 6.0
MAX_PAGINATED_API_PAGES = 10
# review_autofix.yml's Claude-fixer mode (CLAUDE_FIXER_HEAD_PREFIX there).
CLAUDE_FIXER_HEAD_PREFIX = "claude/implement-plan-"
TRUSTED_COMMENT_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
FIXER_HANDOFF_RE = re.compile(
	r"<!-- ai:claude-fixer-handoff:v1 kind=(findings|conflict) head=([0-9a-f]{40}) round=(\d+) -->"
)
FIXER_VERDICT_RE = re.compile(r"<!-- ai:claude-fixer-verdict:v1 head=([0-9a-f]{40}) -->")


class ReadError(Exception):
	"""A `gh api` read failed; the checker reports it and retries next time."""


def _gh_api_json(path: str) -> object:
	"""GET one REST path through `gh api` and return the decoded JSON value."""
	try:
		proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ReadError(f"gh api {path} failed: {exc}") from exc
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout).strip().splitlines()
		raise ReadError(f"gh api {path} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	try:
		return json.loads(proc.stdout)
	except ValueError as exc:
		raise ReadError(f"gh api {path} returned invalid JSON") from exc


def gh_api(path: str) -> object:
	"""GET one REST path through `gh api` and return the decoded JSON object."""
	payload = _gh_api_json(path)
	if not isinstance(payload, dict):
		raise ReadError(f"gh api {path} returned non-object JSON")
	return payload


def gh_api_list(path: str) -> list:
	"""GET every 100-item page of a REST list endpoint and return the items.

	One API call per page, at most MAX_PAGINATED_API_PAGES; raises `ReadError`
	on a read failure, a non-array page, or a non-object item.
	"""
	items: list = []
	for page_number in range(1, MAX_PAGINATED_API_PAGES + 1):
		separator = "&" if "?" in path else "?"
		page_items = _gh_api_json(f"{path}{separator}per_page=100&page={page_number}")
		if not isinstance(page_items, list) or any(not isinstance(item, dict) for item in page_items):
			raise ReadError(f"gh api {path} returned a non-array page")
		items.extend(page_items)
		if len(page_items) < 100:
			return items
	raise ReadError(f"gh api {path} pagination exceeded {MAX_PAGINATED_API_PAGES} pages")


def _gh_api_paginated_object(path: str, list_key: str) -> dict:
	"""Fetch every 100-item REST page and merge `list_key` into one object.

	Input is an unpaginated REST path plus its top-level array key; output
	preserves the first page's object fields with that array concatenated.
	This issues one API call per page and raises `ReadError` on any read or
	shape failure so `main` emits the structured exit-2 verdict and re-arms.
	"""
	paginated_result: dict = {}
	page_number = 1
	while True:
		if page_number > MAX_PAGINATED_API_PAGES:
			raise ReadError(f"gh api {path} pagination exceeded {MAX_PAGINATED_API_PAGES} pages")
		separator = "&" if "?" in path else "?"
		page_payload = gh_api(f"{path}{separator}per_page=100&page={page_number}")
		page_items = page_payload.get(list_key)
		if not isinstance(page_items, list) or any(not isinstance(page_item, dict) for page_item in page_items):
			raise ReadError(f"gh api {path} returned invalid {list_key!r} array")
		if not paginated_result:
			paginated_result = dict(page_payload)
			paginated_result[list_key] = []
		paginated_result[list_key].extend(page_items)
		total_count = page_payload.get("total_count")
		if len(page_items) < 100 or (isinstance(total_count, int) and len(paginated_result[list_key]) >= total_count):
			return paginated_result
		page_number += 1


def _label_names(obj: dict) -> list[str]:
	return [label.get("name", "") for label in obj.get("labels") or [] if isinstance(label, dict)]


def _parse_time(value: str) -> dt.datetime:
	if not isinstance(value, str):
		raise ValueError(f"timestamp must be a string, got {type(value).__name__}")
	return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def check_pr(repo: str, number: int, terminal_only: bool, stuck_hours: float, now: dt.datetime) -> dict:
	pr = gh_api(f"repos/{repo}/pulls/{number}")
	if pr.get("merged"):
		return {"done": True, "state": "merged", "reason": f"PR #{number} merged at {pr.get('merged_at')}", "merge_commit_sha": pr.get("merge_commit_sha")}
	if pr.get("state") == "closed":
		return {"done": True, "state": "closed", "reason": f"PR #{number} closed without merging at {pr.get('closed_at')}"}
	if terminal_only:
		return {"done": False, "state": "open", "reason": f"PR #{number} open"}

	blocking = [name for name in _label_names(pr) if name in BLOCKING_LABELS]
	if blocking:
		return {"done": True, "state": "blocked", "reason": f"PR #{number} blocked: {', '.join(blocking)}"}

	head = pr.get("head") or {}
	head_sha = head.get("sha", "")
	head_ref = head.get("ref", "")
	conflicted = pr.get("mergeable_state") == "dirty"
	if isinstance(head_ref, str) and head_ref.startswith(CLAUDE_FIXER_HEAD_PREFIX):
		fixer_verdict = _check_claude_fixer_pr(repo, number, head_sha, head_ref, conflicted)
		if fixer_verdict is not None:
			return fixer_verdict
	failed_checks: list[str] = []
	if not conflicted:
		runs = _gh_api_paginated_object(f"repos/{repo}/commits/{head_sha}/check-runs", "check_runs")
		failed_checks = sorted(
			run.get("name", "?")
			for run in runs.get("check_runs") or []
			if run.get("status") == "completed" and run.get("conclusion") in FAILED_CHECK_CONCLUSIONS
		)
	if not conflicted and not failed_checks:
		return {"done": False, "state": "open", "reason": f"PR #{number} open, no conflict and no failed check"}

	problem = "merge conflict" if conflicted else f"failed checks: {', '.join(failed_checks)}"
	commit = gh_api(f"repos/{repo}/commits/{head_sha}")
	committed_at = _parse_time(commit["commit"]["committer"]["date"])
	age_hours = (now - committed_at).total_seconds() / 3600
	if age_hours < stuck_hours:
		return {"done": False, "state": "open", "reason": f"PR #{number} has {problem}, head is {age_hours:.1f}h old (< {stuck_hours:g}h)"}

	active = _active_run_count(repo, head_ref)
	if active:
		return {"done": False, "state": "open", "reason": f"PR #{number} has {problem}, but {active} workflow run(s) on {head_ref} are still queued or running"}
	return {"done": True, "state": "stuck", "reason": f"PR #{number} stuck: {problem}, head {age_hours:.1f}h old, no workflow run active on {head_ref}"}


def _active_run_count(repo: str, head_ref: str) -> int:
	active = 0
	for status in ("queued", "in_progress"):
		listing = gh_api(f"repos/{repo}/actions/runs?branch={head_ref}&status={status}&per_page=1")
		active += int(listing.get("total_count") or 0)
	return active


def _check_claude_fixer_pr(repo: str, number: int, head_sha: str, head_ref: str, conflicted: bool) -> dict | None:
	"""Return the hand-off verdict for a Claude-fixer PR, or None to fall through.

	The review workflow posts `ai:claude-fixer-handoff:v1` (findings or
	conflict) for the head it reviewed; the stage session answers with
	`ai:claude-fixer-verdict:v1` for that head when it found nothing left to
	fix. Only comments from trusted author associations count, and only
	markers naming the current head: a push supersedes every earlier round.
	"""
	if not head_sha:
		raise ValueError("PR head sha is empty")
	comments = gh_api_list(f"repos/{repo}/issues/{number}/comments")
	latest_handoff = None
	answered = False
	for comment in comments:
		if comment.get("author_association") not in TRUSTED_COMMENT_ASSOCIATIONS:
			continue
		body = comment.get("body") or ""
		if not isinstance(body, str):
			continue
		current_head_handoff = False
		for match in FIXER_HANDOFF_RE.finditer(body):
			if match.group(2) == head_sha:
				latest_handoff = (match.group(1), int(match.group(3)))
				answered = False
				current_head_handoff = True
				break
		if current_head_handoff:
			continue
		for match in FIXER_VERDICT_RE.finditer(body):
			if match.group(1) == head_sha:
				answered = True
	if latest_handoff is not None and not answered:
		kind, round_number = latest_handoff
		state = "review-round" if kind == "findings" else "conflict"
		detail = "reviewer findings" if kind == "findings" else "a merge conflict"
		return {
			"done": True,
			"state": state,
			"round": round_number,
			"reason": f"PR #{number} review round {round_number}: {detail} on head {head_sha[:12]} handed to Claude",
		}
	if conflicted:
		active = _active_run_count(repo, head_ref)
		if active:
			return {"done": False, "state": "open", "reason": f"PR #{number} has a merge conflict, but {active} workflow run(s) on {head_ref} are still queued or running"}
		return {"done": True, "state": "conflict", "reason": f"PR #{number} has a merge conflict on head {head_sha[:12]} and no workflow run is active"}
	return None


def check_run(repo: str, run_id: int) -> dict:
	run = gh_api(f"repos/{repo}/actions/runs/{run_id}")
	if run.get("status") == "completed":
		conclusion = run.get("conclusion")
		state = "completed" if conclusion in SUCCESSFUL_RUN_CONCLUSIONS else "failed"
		return {"done": True, "state": state, "reason": f"run {run_id} completed: {conclusion}", "conclusion": conclusion}
	return {"done": False, "state": run.get("status"), "reason": f"run {run_id} {run.get('status')}"}


def check_issues(repo: str, numbers: list[int]) -> dict:
	pending = []
	for number in numbers:
		issue = gh_api(f"repos/{repo}/issues/{number}")
		if issue.get("state") != "closed" and MERGED_ISSUE_LABEL not in _label_names(issue):
			pending.append(number)
	if pending:
		return {"done": False, "state": "open", "reason": "issues still open: " + ", ".join(f"#{n}" for n in pending)}
	return {"done": True, "state": "resolved", "reason": "every issue closed or ai:merged: " + ", ".join(f"#{n}" for n in numbers)}


def _parse_issue_list(value: str) -> list[int]:
	numbers = [int(part.strip().lstrip("#")) for part in value.split(",") if part.strip()]
	if not numbers:
		raise argparse.ArgumentTypeError("at least one issue number is required")
	return numbers


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--repo", required=True, help="OWNER/REPO")
	mode = parser.add_mutually_exclusive_group(required=True)
	mode.add_argument("--pr", type=int)
	mode.add_argument("--run", type=int)
	mode.add_argument("--issues", type=_parse_issue_list)
	parser.add_argument("--terminal-only", action="store_true", help="PR mode: only merged / closed count as done")
	parser.add_argument("--stuck-hours", type=float, default=DEFAULT_STUCK_HOURS)
	return parser


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
	args = build_parser().parse_args(argv)
	if args.repo.count("/") != 1:
		print(json.dumps({"done": False, "error": f"--repo must be OWNER/REPO, got {args.repo!r}"}))
		return 2
	now = now or dt.datetime.now(dt.timezone.utc)
	try:
		if args.pr is not None:
			verdict = check_pr(args.repo, args.pr, args.terminal_only, args.stuck_hours, now)
		elif args.run is not None:
			verdict = check_run(args.repo, args.run)
		else:
			verdict = check_issues(args.repo, args.issues)
	except (ReadError, KeyError, TypeError, ValueError) as exc:
		print(json.dumps({"done": False, "error": str(exc)}))
		return 2
	print(json.dumps(verdict))
	return 0


if __name__ == "__main__":
	sys.exit(main())

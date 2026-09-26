#!/usr/bin/env python3
"""Deterministic "is the wait over?" check for the Sonnet check-in sessions.

Used by the `/implement-plan-claude` checker session and the CLAUDE.md §26
post-push PR status check-in. The checker model only runs this script and
acts on its one-line verdict, so no time arithmetic or label judgement is
left to the model.

Input (exactly one mode):

  --pr N [--terminal-only | --hand-back [--min-age-hours H]]   a pull request
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
  * Claude-fixer PR (head ref starts with `claude/implement-plan-`): also
    done for an authenticated, completed review hand-off on the current head
    with no matching dedicated-bot verdict, or for a merge conflict when no
    workflow run is active (no 6-hour wait). An empty
    CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN disables comment hand-offs.
  * PR with --hand-back (the CLAUDE.md §26 checker and the catch-all sweep,
    scripts/claude_pr_sweep.py): merged / closed are terminal. On a
    `claude/*` head, a Claude fix is also due for a block label
    (`state: blocked`), a workflow hand-off or an unhanded merge conflict
    (`review-round` / `conflict`, as for a Claude-fixer PR above), or a
    failed check run with no workflow run queued, in progress or pending on
    the branch (`ci-failed`; no age threshold, except that a
    `claude/implement-plan-*` head keeps the --stuck-hours window). A live
    `ai:claude-fix-claim` on the current head reports `claimed` and a hold
    reports `held`; both are not done, so nobody starts a second fixer.
    --min-age-hours keeps a due fix waiting until it has been visible that
    long (`since`). Every verdict carries `kind`, `head_sha`, `claim`,
    `hand_backs`, `cap` and `cap_reached` (hand-backs counted per distinct
    head and kind for conflict / ci / blocked claims; review rounds are
    bounded by the workflow's MAX_AUTOFIX_ITERATIONS instead). Any other
    head only hands back merged / closed.
  * Run: `status` is `completed` (any conclusion). `state` is `completed`
    only for a `success` conclusion and `failed` for any other, so a checker
    routes a failed run to its block stage.
  * Issues: every issue is closed or labelled ai:merged.

API budget (CLAUDE.md §15): REST only, never GraphQL. PR mode issues 1 call
(`pulls/N`), one call per 100 check runs when the PR is not conflicted, and
at most 3 further calls when a failure is old (head commit, queued runs,
in-progress runs). A Claude-fixer PR adds one call per 100 PR comments, at
most one hand-off run read, and 3 active-run reads when needed. Hand-back
mode on a `claude/*` head issues 1 call plus one per 100 PR comments, the
check-run pages, at most 1 head-commit read, 1 hand-off run read and 3
active-run reads. Run mode issues 1 call. Issues mode issues one call per
issue; the checker lists at most the few follow-ups one security cycle opens.
Every call goes through `gh api`, which in Claude Code on the web is
authenticated by the session's agent proxy.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
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
FIXER_HANDOFF_RE = re.compile(
	r"<!-- ai:claude-fixer-handoff:v1 kind=(findings|conflict) head=([0-9a-f]{40}) round=(\d+) -->"
)
FIXER_VERDICT_RE = re.compile(r"<!-- ai:claude-fixer-verdict:v1 head=([0-9a-f]{40}) -->")
FIXER_HANDOFF_LEDGER_RE = re.compile(
	r"^<!-- ai:claude-fixer-handoff:v2 head=([0-9a-f]{40}) round=(\d+) ledger=([0-9a-f]{64}) -->$",
	re.MULTILINE,
)
FIXER_VERDICT_LEDGER_RE = re.compile(
	r"^<!-- ai:claude-fixer-verdict:v2 head=([0-9a-f]{40}) round=(\d+) ledger=([0-9a-f]{64}) -->$",
	re.MULTILINE,
)
FIXER_HANDOFF_HEADER_RE = re.compile(
	r"## Review round ([1-9][0-9]*): (findings handed to the Claude session|merge conflict, handed to the Claude session)"
)
FIXER_WORKFLOW_PATHS = (
	".github/workflows/review_autofix.yml",
	".github/workflows/internal-review.yml",
	".github/workflows/ai-review.yml",
)
# --hand-back mode: review_autofix.yml runs every PR-backed claude/* head in
# Claude-fixer mode (its `claude/*)` gate case).
CLAUDE_BRANCH_PREFIX = "claude/"
# A fixer session's claim on a PR head (.claude/scripts/claude_fix_claim.py
# writes it). Only a repository owner, member or collaborator can claim, and
# the claim's time is the comment's own created_at, never text in the body.
FIX_CLAIM_RE = re.compile(
	r"^<!-- ai:claude-fix-claim:v1 head=([0-9a-f]{40}) kind=(conflict|ci|review|blocked|hold) by=([A-Za-z0-9_-]{1,80}) -->$"
)
FIX_CLAIM_TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
FIX_CLAIM_COUNTED_KINDS = ("conflict", "ci", "blocked")
DEFAULT_FIX_CLAIM_LEASE_HOURS = 3.0
DEFAULT_FIX_HAND_BACK_CAP = 3
HAND_BACK_KIND_BY_STATE = {"conflict": "conflict", "review-round": "review", "ci-failed": "ci", "blocked": "blocked"}


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


def _active_run_count(repo: str, head_ref: str, include_pending: bool = False) -> int:
	active = 0
	for status in (("queued", "in_progress", "pending") if include_pending else ("queued", "in_progress")):
		listing = gh_api(f"repos/{repo}/actions/runs?branch={head_ref}&status={status}&per_page=1")
		active += int(listing.get("total_count") or 0)
	return active


def _check_claude_fixer_pr(repo: str, number: int, head_sha: str, head_ref: str, conflicted: bool,
	comments: list | None = None) -> dict | None:
	"""Return the hand-off verdict for a Claude-fixer PR, or None to fall through.

	Only the configured workflow account's complete hand-off for the current
	head can wake a round. A bot's ledger-bound verdict must follow that
	particular hand-off. The workflow run must finish before the fixer starts.
	`comments` reuses a comment listing the caller already fetched (§15);
	a done verdict carries `since`, the hand-off comment's created_at.
	"""
	if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
		raise ValueError("PR head sha must be 40 lowercase hex characters")
	if comments is None:
		comments = gh_api_list(f"repos/{repo}/issues/{number}/comments")
	latest_handoff = None
	answered = False
	# The bot login must be configured in the checker session as well as the
	# workflow. A collaborator's head-only verdict never ends a review round.
	fixer_bot_login = os.environ.get("CLAUDE_FIXER_VERDICT_BOT_LOGIN", "")
	# Checker credentials may differ from the workflow's posting credentials.
	# Without an explicit workflow author, comments cannot authorize a hand-off.
	fixer_handoff_author_login = os.environ.get("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "")
	# Issue comments are ordered by ascending GitHub ID; never let a later
	# quoted or stale marker supersede the workflow's actual comment.
	for comment in sorted(comments, key=lambda entry: entry.get("id") if type(entry.get("id")) is int else 0):
		comment_id = comment.get("id")
		if type(comment_id) is not int or comment_id <= 0:
			continue
		body = comment.get("body") or ""
		if not isinstance(body, str):
			continue
		lines = body.splitlines()
		user = comment.get("user")
		if (fixer_handoff_author_login and isinstance(user, dict)
			and user.get("login") == fixer_handoff_author_login):
			header = FIXER_HANDOFF_HEADER_RE.fullmatch(lines[0]) if lines else None
			markers = [match for line in lines if (match := FIXER_HANDOFF_RE.fullmatch(line))]
			if header and len(markers) == 1:
				marker = markers[0]
				kind, issued_head, round_text = marker.groups()
				expected_heading = ("findings handed to the Claude session" if kind == "findings"
					else "merge conflict, handed to the Claude session")
				if (issued_head == head_sha and round_text == header.group(1)
					and header.group(2) == expected_heading):
					ledger_markers = [match for line in lines if (match := FIXER_HANDOFF_LEDGER_RE.fullmatch(line))]
					if (kind == "conflict" and not ledger_markers) or (
						kind == "findings" and len(ledger_markers) == 1
						and ledger_markers[0].group(1, 2) == (head_sha, round_text)
					):
						# Parse only the workflow's own run link, not arbitrary links in
						# reviewer text or quoted comments.
						run_line_prefix = (f"Reviewed head: `{head_sha}` (" if kind == "findings"
							else f"The head `{head_sha}` conflicts with its base branch, so the reviewer panel did not run (")
						run_lines = [line for line in lines if line.startswith(run_line_prefix)]
						run_link_pattern = re.compile(
							re.escape(run_line_prefix) + r"\[workflow run\]\((https://[^/\s)]+/"
							+ re.escape(repo) + r"/actions/runs/([1-9][0-9]*))\)\)\."
						)
						run_link = run_link_pattern.fullmatch(run_lines[0]) if len(run_lines) == 1 else None
						if run_link:
							latest_handoff = (kind, int(round_text),
								ledger_markers[0].group(3) if ledger_markers else "",
								int(run_link.group(2)), run_link.group(1), comment_id,
								comment.get("created_at") if isinstance(comment.get("created_at"), str) else None)
							answered = False
							continue
		if not latest_handoff or not latest_handoff[2] or not fixer_bot_login or comment_id <= latest_handoff[5]:
			continue
		if not isinstance(user, dict) or user.get("login") != fixer_bot_login or user.get("type") != "Bot":
			continue
		verdict_markers = [match for line in lines if (match := FIXER_VERDICT_RE.fullmatch(line))]
		verdict_ledgers = [match for line in lines if (match := FIXER_VERDICT_LEDGER_RE.fullmatch(line))]
		if (len(verdict_markers) == len(verdict_ledgers) == 1
			and verdict_markers[0].group(1) == head_sha
			and (verdict_ledgers[0].group(1), int(verdict_ledgers[0].group(2)), verdict_ledgers[0].group(3))
				== (head_sha, latest_handoff[1], latest_handoff[2])):
			answered = True
	handoff_since = latest_handoff[6] if latest_handoff is not None and not answered else None
	if latest_handoff is not None and not answered:
		kind, round_number, _, run_id, run_url, _, _ = latest_handoff
		# This is a separate read: neither the PR nor its comment establishes
		# the run's result, repository, workflow, or reviewed branch/head.
		review_run = gh_api(f"repos/{repo}/actions/runs/{run_id}")
		review_repo = review_run.get("repository")
		review_path = review_run.get("path")
		if (review_run.get("id") != run_id or review_run.get("html_url") != run_url
			or not isinstance(review_repo, dict) or review_repo.get("full_name") != repo
			or not isinstance(review_path, str) or review_path.split("@", 1)[0] not in FIXER_WORKFLOW_PATHS
			or review_run.get("head_sha") != head_sha or review_run.get("head_branch") != head_ref
			or review_run.get("status") != "completed" or review_run.get("conclusion") != "success"):
			return {"done": False, "state": "open", "reason": f"PR #{number} waiting for verified completed review run {run_id}"}
	if conflicted:
		active = _active_run_count(repo, head_ref, include_pending=True)
		if active:
			return {"done": False, "state": "open", "reason": f"PR #{number} has a merge conflict, but {active} workflow run(s) on {head_ref} are still queued or running"}
		return {"done": True, "state": "conflict", "since": handoff_since,
			"reason": f"PR #{number} has a merge conflict on head {head_sha[:12]} and no workflow run is active"}
	if latest_handoff is not None and not answered:
		active = _active_run_count(repo, head_ref, include_pending=True)
		if active:
			return {"done": False, "state": "open", "reason": f"PR #{number} has a review hand-off, but {active} workflow run(s) on {head_ref} are still queued or running"}
		state = "review-round" if kind == "findings" else "conflict"
		detail = "reviewer findings" if kind == "findings" else "a merge conflict"
		return {
			"done": True,
			"state": state,
			"round": round_number,
			"since": handoff_since,
			"reason": f"PR #{number} review round {round_number}: {detail} on head {head_sha[:12]} handed to Claude",
		}
	return None


def _env_positive_float(name: str, default: float) -> float:
	"""Read a positive number from the environment, falling back to `default`."""
	try:
		value = float(os.environ.get(name, "") or default)
	except ValueError:
		return default
	return value if value > 0 else default


def read_fix_claims(comments: list, head_sha: str, now: dt.datetime, ignore_by: tuple[str, ...] = ()) -> dict:
	"""Summarise the trusted `ai:claude-fix-claim` markers on one PR.

	Input: the PR's issue comments (REST objects) and its current head sha.
	Output: `{"claim": {"state": none|live|expired|held, "kind", "by", "at"},
	"hand_backs": n, "cap": c, "cap_reached": bool}`. The latest trusted claim
	on the current head decides `claim`; a `hold` never expires while the head
	stays the same, any other claim is live for CLAUDE_FIX_CLAIM_LEASE_HOURS
	(default 3) after its comment's created_at. Claims by a claimant in
	`ignore_by` (the caller itself, or the sweep reservation made for it) do
	not decide `claim`, but still count toward `hand_backs`. `hand_backs` counts distinct
	(head, kind) pairs of conflict / ci / blocked claims across the PR, so a
	sweep claim and the fixer session's own claim for the same event count
	once. No API calls: the caller passes the comments it already fetched.
	"""
	lease_hours = _env_positive_float("CLAUDE_FIX_CLAIM_LEASE_HOURS", DEFAULT_FIX_CLAIM_LEASE_HOURS)
	cap = int(_env_positive_float("CLAUDE_FIX_HAND_BACK_CAP", DEFAULT_FIX_HAND_BACK_CAP))
	counted: set[tuple[str, str]] = set()
	latest = None
	for comment in sorted(comments, key=lambda entry: entry.get("id") if type(entry.get("id")) is int else 0):
		if comment.get("author_association") not in FIX_CLAIM_TRUSTED_ASSOCIATIONS:
			continue
		body = comment.get("body")
		if not isinstance(body, str):
			continue
		markers = [match for line in body.splitlines() if (match := FIX_CLAIM_RE.fullmatch(line))]
		if len(markers) != 1:
			continue
		claim_head, claim_kind, claim_by = markers[0].groups()
		if claim_kind in FIX_CLAIM_COUNTED_KINDS:
			counted.add((claim_head, claim_kind))
		if claim_head == head_sha and claim_by not in ignore_by:
			latest = (claim_kind, claim_by, comment.get("created_at"))
	claim = {"state": "none"}
	if latest is not None:
		claim_kind, claim_by, claim_at = latest
		claim = {"state": "held" if claim_kind == "hold" else "expired", "kind": claim_kind, "by": claim_by, "at": claim_at}
		if claim_kind != "hold" and isinstance(claim_at, str):
			try:
				if (now - _parse_time(claim_at)).total_seconds() < lease_hours * 3600:
					claim["state"] = "live"
			except ValueError:
				pass
	return {"claim": claim, "hand_backs": len(counted), "cap": cap, "cap_reached": len(counted) >= cap}


def _hours_since(value: str | None, now: dt.datetime) -> float | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		return (now - _parse_time(value)).total_seconds() / 3600
	except ValueError:
		return None


def check_pr_hand_back(repo: str, number: int, stuck_hours: float, min_age_hours: float, now: dt.datetime,
	ignore_claim_by: tuple[str, ...] = ()) -> dict:
	"""The CLAUDE.md §26 hand-back verdict for one PR (see the module docstring).

	`done` is true for a terminal PR or for a Claude fix that is due now; the
	checker hands a due fix back to the session that pushed the PR, and the
	sweep starts a fresh fixer session for one nobody handled. A claimed or
	held head, a younger-than-min-age problem, or an active workflow run on
	the branch is not done. `ignore_claim_by` lets a fixer look past its own
	claim and the sweep reservation made for it.
	"""
	pr = gh_api(f"repos/{repo}/pulls/{number}")
	if pr.get("merged"):
		return {"done": True, "state": "merged", "reason": f"PR #{number} merged at {pr.get('merged_at')}", "merge_commit_sha": pr.get("merge_commit_sha")}
	if pr.get("state") == "closed":
		return {"done": True, "state": "closed", "reason": f"PR #{number} closed without merging at {pr.get('closed_at')}"}
	head = pr.get("head") or {}
	head_sha = head.get("sha", "")
	head_ref = head.get("ref", "")
	if not isinstance(head_ref, str) or not head_ref.startswith(CLAUDE_BRANCH_PREFIX):
		return {"done": False, "state": "open", "head_sha": head_sha,
			"reason": f"PR #{number} open; {head_ref or 'its head'} is not a claude/* branch, so only merged / closed hand back"}
	if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
		raise ValueError("PR head sha must be 40 lowercase hex characters")

	comments = gh_api_list(f"repos/{repo}/issues/{number}/comments")
	claims = read_fix_claims(comments, head_sha, now, ignore_claim_by)
	base = {"head_sha": head_sha, **claims}
	claim = claims["claim"]
	if claim["state"] == "held":
		return {"done": False, "state": "held", **base,
			"reason": f"PR #{number} is on hold on head {head_sha[:12]} ({claim['by']}): the hand-back cap was reached and a human decides next"}
	if claim["state"] == "live":
		return {"done": False, "state": "claimed", **base,
			"reason": f"PR #{number} head {head_sha[:12]} is claimed by {claim['by']} for {claim['kind']} since {claim['at']}"}

	due = None
	blocking = [name for name in _label_names(pr) if name in BLOCKING_LABELS]
	if blocking:
		due = {"state": "blocked", "since": pr.get("updated_at"), "reason": f"PR #{number} blocked: {', '.join(blocking)}"}
	conflicted = pr.get("mergeable_state") == "dirty"
	if due is None:
		fixer_verdict = _check_claude_fixer_pr(repo, number, head_sha, head_ref, conflicted, comments=comments)
		if fixer_verdict is not None and not fixer_verdict.get("done"):
			return {**fixer_verdict, **base}
		if fixer_verdict is not None:
			due = fixer_verdict
	if due is None and not conflicted:
		runs = _gh_api_paginated_object(f"repos/{repo}/commits/{head_sha}/check-runs", "check_runs")
		failed = [
			run for run in runs.get("check_runs") or []
			if run.get("status") == "completed" and run.get("conclusion") in FAILED_CHECK_CONCLUSIONS
		]
		if not failed:
			return {"done": False, "state": "open", **base, "reason": f"PR #{number} open, no conflict, hand-off, block or failed check"}
		names = ", ".join(sorted(run.get("name", "?") for run in failed))
		completed = sorted(run.get("completed_at") for run in failed if isinstance(run.get("completed_at"), str))
		if head_ref.startswith(CLAUDE_FIXER_HEAD_PREFIX):
			# /implement-plan-claude PRs keep their chain's stuck window (Q22).
			commit = gh_api(f"repos/{repo}/commits/{head_sha}")
			head_age = _hours_since(commit["commit"]["committer"]["date"], now)
			if head_age is None or head_age < stuck_hours:
				return {"done": False, "state": "open", **base,
					"reason": f"PR #{number} has failed checks: {names}, but an implement-plan head waits {stuck_hours:g}h before it counts as stuck"}
		active = _active_run_count(repo, head_ref, include_pending=True)
		if active:
			return {"done": False, "state": "open", **base,
				"reason": f"PR #{number} has failed checks: {names}, but {active} workflow run(s) on {head_ref} are still queued, running or pending"}
		due = {"state": "ci-failed", "since": completed[-1] if completed else None, "reason": f"PR #{number} failed checks with no workflow run active: {names}"}
	if due is None:
		return {"done": False, "state": "open", **base, "reason": f"PR #{number} open"}

	since = due.get("since")
	if _hours_since(since, now) is None:
		commit = gh_api(f"repos/{repo}/commits/{head_sha}")
		since = commit["commit"]["committer"]["date"]
	age = _hours_since(since, now)
	verdict = {**due, **base, "since": since, "kind": HAND_BACK_KIND_BY_STATE[due["state"]]}
	if min_age_hours > 0 and (age is None or age < min_age_hours):
		return {**verdict, "done": False, "due_state": due["state"], "state": "open",
			"reason": f"{due['reason']}; visible {0.0 if age is None else age:.1f}h (< {min_age_hours:g}h)"}
	return {**verdict, "done": True}


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
	pr_mode = parser.add_mutually_exclusive_group()
	pr_mode.add_argument("--terminal-only", action="store_true", help="PR mode: only merged / closed count as done")
	pr_mode.add_argument("--hand-back", action="store_true",
		help="PR mode: CLAUDE.md §26 hand-back verdict (terminal, or a due Claude fix on a claude/* head)")
	parser.add_argument("--stuck-hours", type=float, default=DEFAULT_STUCK_HOURS)
	parser.add_argument("--min-age-hours", type=float, default=0.0,
		help="--hand-back: a due fix must have been visible this long (the sweep passes 2)")
	parser.add_argument("--ignore-claim-by", action="append", default=[],
		help="--hand-back: a claimant whose claims do not block (the calling fixer, or its sweep reservation); repeatable")
	return parser


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
	args = build_parser().parse_args(argv)
	if args.repo.count("/") != 1:
		print(json.dumps({"done": False, "error": f"--repo must be OWNER/REPO, got {args.repo!r}"}))
		return 2
	if args.pr is None and args.hand_back:
		print(json.dumps({"done": False, "error": "--hand-back needs --pr"}))
		return 2
	now = now or dt.datetime.now(dt.timezone.utc)
	try:
		if args.pr is not None and args.hand_back:
			verdict = check_pr_hand_back(args.repo, args.pr, args.stuck_hours, args.min_age_hours, now,
				tuple(args.ignore_claim_by))
		elif args.pr is not None:
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

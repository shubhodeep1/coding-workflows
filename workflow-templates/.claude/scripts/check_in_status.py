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

  {"done": true, "reason": "PR #12 merged at 2026-09-23T04:00:00Z", ..., "action": "next_stage", "next_stage": "success"}
  {"done": false, "reason": "PR #12 open, healthy", ..., "action": "wait"}
  {"done": false, "error": "gh api repos/o/r/pulls/12 failed: HTTP 403", "action": "retry"}

Exit status: 0 when a verdict was reached (done or not), 2 when a read
failed (the JSON then carries `error`, `done` is false and `action` is
`retry`).

Every JSON line `main` prints also carries `action` (and, for
`next_stage`, `next_stage`): the routing decision the checker follows, so no
checker model interprets `state` itself. `main` adds it with `route_verdict`;
the `check_*` functions return the verdict without it, so a direct caller
(`scripts/claude_pr_sweep.py`) that needs it calls `route_verdict` itself.
`route_verdict` holds the full state → action table. In short: plain PR
mode → `wait` / `hand_back` / `next_stage` (`success`, `review`, `block`),
and a review round or conflict is never `hand_back`;
run and issue-list modes → `wait` / `next_stage`; --hand-back mode →
`wait` / `hand_back_fixer` / `hand_back_all`.

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
    `claude/implement-plan-*` head keeps the --stuck-hours window), or a
    head that was never reviewed (`review-stalled`, issue #4985: no
    hand-off, no gate skip notice, no auto-merge, no skip-AI marker, not a
    draft or `ai:merge-queued`, no active run, and older than
    CLAUDE_REVIEW_STALL_HOURS, default 2; `stall_redispatched` says a review
    run dispatched for the PR from the default branch after the head arrived
    already completed, never a claim, issue #5376; off when
    CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN is unset). A live
    `ai:claude-fix-claim` on the current head reports `claimed` and a hold
    reports `held`; both are not done, so nobody starts a second fixer.
    A claim counts only when an owner / member / collaborator posted it as
    the PR's author or as CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN (issue #4622).
    --min-age-hours keeps a due fix waiting until it has been visible that
    long (`since`). Every verdict carries `kind`, `head_sha`, `claim`,
    `hand_backs`, `cap` and `cap_reached` (hand-backs counted per distinct
    head and kind for conflict / ci / blocked claims; review rounds are
    bounded by the workflow's MAX_AUTOFIX_ITERATIONS instead). Any other
    head only hands back merged / closed.
  * Run: `status` is `completed` (any conclusion). `state` is `completed`
    only for a `success` conclusion and `failed` for any other, so a checker
    routes a failed run to its block stage.
  * Issues: every issue is closed or labelled ai:merged (`state` resolved);
    or an issue still open without ai:merged carries ai:review-blocked /
    ai:review-autofix-failed / ai:needs-human (`state` blocked), because its
    pipeline gave up and the issue will not close on its own.

API budget (CLAUDE.md §15): REST only, never GraphQL. PR mode issues 1 call
(`pulls/N`), one call per 100 check runs when the PR is not conflicted, and
at most 3 further calls when a failure is old (head commit, queued runs,
in-progress runs). A Claude-fixer PR adds one call per 100 PR comments, at
most one hand-off run read (plus 1 compare read when the run was triggered
by an older push than the head it reviewed), and 3 active-run reads when
needed. Hand-back mode on a `claude/*` head issues 1 call plus one per 100
PR comments, the check-run pages, at most 1 head-commit read, 1 hand-off run
read, 1 compare read (same condition) and 3 active-run reads (plus the
dispatched-review list, one more listing read in a consumer repo, where
internal-review.yml answers 404 before ai-review.yml); the review-stall check
reuses the PR object, the comments, the check runs and that dispatched-review
list and only then adds that head-commit read and those active-run reads.
Run mode issues 1 call. Issues mode issues one call per issue; the checker lists at
most the few follow-ups one security cycle opens.
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
# review_autofix_sweep.yml dispatches internal-review.yml from the default
# branch (issue #4618), so such a run's head_branch is the default branch and
# its head_sha a default-branch commit. internal-review.yml names every
# dispatched run with this exact title, which binds the run to its PR.
DISPATCHED_REVIEW_WORKFLOW = ".github/workflows/internal-review.yml"
DISPATCHED_REVIEW_TITLE = "Internal: AI Review & Autofix [pr:{number}]"
DISPATCHED_REVIEW_RUNS_PATH = "repos/{repo}/actions/workflows/internal-review.yml/runs?event=workflow_dispatch&per_page=100"
# Consumer repos have no internal-review.yml: their ai-review.yml wrapper
# (workflow-templates/ai-review.yml run-name, issue #5376) titles dispatched
# runs the same way, so the title binds those runs to their PR too.
CONSUMER_DISPATCHED_REVIEW_WORKFLOW = ".github/workflows/ai-review.yml"
CONSUMER_DISPATCHED_REVIEW_TITLE = "AI Review [pr:{number}]"
CONSUMER_DISPATCHED_REVIEW_RUNS_PATH = "repos/{repo}/actions/workflows/ai-review.yml/runs?event=workflow_dispatch&per_page=100"
# (workflow path, run title, runs listing), tried in order; the first
# workflow that exists (no HTTP 404) is the repository's review workflow.
DISPATCHED_REVIEW_SOURCES = (
	(DISPATCHED_REVIEW_WORKFLOW, DISPATCHED_REVIEW_TITLE, DISPATCHED_REVIEW_RUNS_PATH),
	(CONSUMER_DISPATCHED_REVIEW_WORKFLOW, CONSUMER_DISPATCHED_REVIEW_TITLE, CONSUMER_DISPATCHED_REVIEW_RUNS_PATH),
)
# A cancelled dispatched review run reviewed nothing, so it never counts as a
# re-dispatch of a stalled head (issue #5376); every other conclusion does.
REDISPATCH_UNCOUNTED_CONCLUSIONS = ("cancelled",)
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
HAND_BACK_KIND_BY_STATE = {"conflict": "conflict", "review-round": "review", "ci-failed": "ci", "blocked": "blocked",
	"review-stalled": "review"}
# The skip-AI opt-out marker (issue #4985). Only an intentional marker counts:
# anywhere in the PR title, or on a body line that holds nothing but the
# marker (up to 3 leading spaces) outside a ``` / ~~~ fenced block. A quoted,
# backticked, indented or mid-sentence mention never counts. A fence opens on
# any line starting with 3+ backticks or tildes (SKIP_AI_FENCE_RE) and closes
# only on a matching fence (SKIP_AI_FENCE_CLOSE_RE: up to 3 spaces, the same
# character, at least as long as the opener, then only blanks), so a ``` line
# inside a ```` fence stays inside it (issue #5377). The review gate
# in .github/workflows/review_autofix.yml and review_autofix_sweep.yml apply
# the same rule in awk; scripts/claude_pr_sweep.py calls has_skip_ai_marker.
# tests/test_skip_ai_marker_rule.py holds all three to one case table.
SKIP_AI_MARKER = "[skip ai]"
SKIP_AI_BODY_LINE_RE = re.compile(r"^ {0,3}\[skip ai\][ \t]*$")
SKIP_AI_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
SKIP_AI_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
# review_autofix.yml posts this once per head when it skips a PR-backed
# claude/* head for skip_ai_marker / draft_or_skip_ai (issue #4985).
REVIEW_SKIPPED_MARKER_RE = re.compile(
	r"^<!-- ai:claude-fixer-review-skipped:v1 reason=([a-z_]+) head=([0-9a-f]{40}) -->$"
)
# --hand-back: a claude/* head with no review trace this long is review-stalled.
DEFAULT_REVIEW_STALL_HOURS = 2.0
# Labels that defer a review on purpose (scripts/review_merge_train.sh).
REVIEW_DEFERRED_LABELS = ("ai:merge-queued",)


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


def has_skip_ai_marker(title: object, body: object) -> bool:
	"""True when a PR's title or body carries an intentional skip-AI marker.

	The title counts wherever it holds the marker. A body line counts only
	when it holds nothing but the marker (up to 3 leading spaces, trailing
	blanks and a trailing CR allowed) and lies outside a ``` / ~~~ fenced
	block. A fence line opens a block that only a matching closing fence
	ends: up to 3 spaces, a run of the opener's character at least as long
	as the opener, then only blanks. Any other line inside the block, a
	shorter or other-character fence included, is content, and an unclosed
	block runs to the end of the body (issue #5377). Non-string input
	counts as empty. No API calls.
	"""
	if isinstance(title, str) and SKIP_AI_MARKER in title:
		return True
	if not isinstance(body, str):
		return False
	in_fence = False
	fence_run = ""
	for line in body.split("\n"):
		if line.endswith("\r"):
			line = line[:-1]
		if in_fence:
			closing = SKIP_AI_FENCE_CLOSE_RE.match(line)
			if closing and closing.group(1)[0] == fence_run[0] and len(closing.group(1)) >= len(fence_run):
				in_fence = False
			continue
		opening = SKIP_AI_FENCE_RE.match(line)
		if opening:
			in_fence = True
			fence_run = opening.group(1)
			continue
		if SKIP_AI_BODY_LINE_RE.match(line):
			return True
	return False


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
		fixer_verdict = _check_claude_fixer_pr(repo, number, head_sha, head_ref, conflicted,
			default_branch=_pr_default_branch(pr))
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

	active = _active_run_count(repo, head_ref, pr_number=number)
	if active:
		return {"done": False, "state": "open", "reason": f"PR #{number} has {problem}, but {active} workflow run(s) on {head_ref} are still queued or running"}
	return {"done": True, "state": "stuck", "reason": f"PR #{number} stuck: {problem}, head {age_hours:.1f}h old, no workflow run active on {head_ref}"}


def _dispatched_review_runs(repo: str) -> dict:
	"""List the review workflow's recent workflow_dispatch runs.

	Input: the repository. Tries each DISPATCHED_REVIEW_SOURCES entry in
	order (internal-review.yml here, the ai-review.yml wrapper in a consumer
	repo); a workflow that does not exist (HTTP 404) moves on to the next.
	Output: `{"workflow": <path>, "title": <title template>, "runs": [<run
	objects>]}` for the first workflow that exists, or the same keys with
	empty values when none does. API calls: one listing read (the newest 100
	dispatched runs) per workflow tried, so one in coding-workflows and two
	in a consumer repo. Any failed read other than a 404 raises `ReadError`.
	"""
	for workflow_path, title, runs_path in DISPATCHED_REVIEW_SOURCES:
		try:
			listing = gh_api(runs_path.format(repo=repo))
		except ReadError as exc:
			if "HTTP 404" in str(exc):
				continue
			raise
		if not isinstance(listing.get("workflow_runs"), list) or any(
			not isinstance(run, dict) for run in listing["workflow_runs"]
		):
			raise ReadError(f"gh api {runs_path.format(repo=repo)} returned invalid workflow_runs")
		runs = listing["workflow_runs"]
		return {"workflow": workflow_path, "title": title, "runs": runs}
	return {"workflow": "", "title": "", "runs": []}


def _active_run_count(repo: str, head_ref: str, include_pending: bool = False, pr_number: int | None = None,
	dispatched_listing: dict | None = None) -> int:
	"""Count queued / running (and, with `include_pending`, pending) runs for a PR.

	One read per status filtered by the head branch. When those find nothing
	and `pr_number` is given, `_dispatched_review_runs` lists the review
	workflow's workflow_dispatch runs and counts the active ones titled for
	this PR (DISPATCHED_REVIEW_TITLE, or CONSUMER_DISPATCHED_REVIEW_TITLE in a
	consumer repo): the sweep and the fixer dispatch them from the default
	branch (issue #4618), so the head-branch filter never sees them. A
	repository with neither workflow (HTTP 404) counts 0; any other failed
	read raises `ReadError`. When `dispatched_listing` is a dict, the listing
	read here is stored in it (`_dispatched_review_runs` keys) so the caller
	can reuse it without another API call.
	"""
	statuses = ("queued", "in_progress", "pending") if include_pending else ("queued", "in_progress")
	active = 0
	for status in statuses:
		listing = gh_api(f"repos/{repo}/actions/runs?branch={head_ref}&status={status}&per_page=1")
		count = listing.get("total_count")
		if type(count) is not int or count < 0:
			raise ReadError(f"actions/runs returned invalid total_count for {status}")
		active += count
	if active or pr_number is None:
		return active
	dispatched = _dispatched_review_runs(repo)
	if dispatched_listing is not None:
		dispatched_listing.update(dispatched)
	if not dispatched["title"]:
		return 0
	title = dispatched["title"].format(number=pr_number)
	return sum(
		1 for run in dispatched["runs"]
		if run.get("status") in statuses and run.get("display_title") == title
	)


def _pr_default_branch(pr: dict) -> str | None:
	"""The PR base repository's default branch from the PR object, or None."""
	base = pr.get("base")
	repo_info = base.get("repo") if isinstance(base, dict) else None
	branch = repo_info.get("default_branch") if isinstance(repo_info, dict) else None
	return branch if isinstance(branch, str) and branch else None


def _is_pr_dispatched_review_run(review_run: dict, number: int, default_branch: str | None) -> bool:
	"""True for an internal-review.yml run the sweep dispatched for this PR.

	Such a run starts from the default branch, so its head_branch and head_sha
	describe the default branch, not the PR; the exact run title binds it to
	PR `number`, and the hand-off's own `head=` marker binds the reviewed head.
	Needs no API call. False when the default branch is unknown.
	"""
	path = review_run.get("path")
	return (
		default_branch is not None
		and review_run.get("event") == "workflow_dispatch"
		and isinstance(path, str) and path.split("@", 1)[0] == DISPATCHED_REVIEW_WORKFLOW
		and review_run.get("head_branch") == default_branch
		and review_run.get("display_title") == DISPATCHED_REVIEW_TITLE.format(number=number)
	)


def _review_run_head_on_branch_history(repo: str, run_head_sha: object, reviewed_head_sha: str) -> bool:
	"""True when a review run's triggering commit is the reviewed head or its ancestor.

	A `pull_request` run's `head_sha` is the push that triggered it, but the
	review workflow reviews the PR's head at the time it runs, so two quick
	pushes leave a run triggered by the first push that reviewed the second
	(PR #4594: run head a59fc87, hand-off head d1c6f92). Input: the run's
	`head_sha` and the hand-off head, which the caller has already matched to
	the current PR head. Output: True for an equal sha with no API call;
	otherwise one compare read (`compare/<run head>...<reviewed head>`) and
	True only for status `ahead` (the run's commit is an ancestor). A
	diverged or behind commit, or a malformed sha on either side, is False, so the caller
	keeps waiting; a failed read raises `ReadError` like every other read.
	"""
	for sha in (run_head_sha, reviewed_head_sha):
		if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
			return False
	if run_head_sha == reviewed_head_sha:
		return True
	comparison = gh_api(f"repos/{repo}/compare/{run_head_sha}...{reviewed_head_sha}?per_page=1")
	return comparison.get("status") == "ahead"


def _check_claude_fixer_pr(repo: str, number: int, head_sha: str, head_ref: str, conflicted: bool,
	comments: list | None = None, default_branch: str | None = None) -> dict | None:
	"""Return the hand-off verdict for a Claude-fixer PR, or None to fall through.

	Only the configured workflow account's complete hand-off for the current
	head can wake a round. A bot's ledger-bound verdict must follow that
	particular hand-off. The workflow run must finish before the fixer starts.
	`comments` reuses a comment listing the caller already fetched (§15);
	a done verdict carries `since`, the hand-off comment's created_at.
	`default_branch` (from the PR object) lets a sweep run dispatched from the
	default branch for this PR count as the hand-off's review run.
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
		# the run's result, repository, workflow, or reviewed branch. The run's
		# own head_sha is its triggering push, which may predate the head the
		# hand-off reviewed, so it only has to lie on that head's history. A
		# run the sweep dispatched from the default branch for this PR (issue
		# #4618) is bound by its title instead of its branch and history.
		review_run = gh_api(f"repos/{repo}/actions/runs/{run_id}")
		review_repo = review_run.get("repository")
		review_path = review_run.get("path")
		if (review_run.get("id") != run_id or review_run.get("html_url") != run_url
			or not isinstance(review_repo, dict) or review_repo.get("full_name") != repo
			or not isinstance(review_path, str) or review_path.split("@", 1)[0] not in FIXER_WORKFLOW_PATHS
			or review_run.get("status") != "completed" or review_run.get("conclusion") != "success"
			or (not _is_pr_dispatched_review_run(review_run, number, default_branch)
				and (review_run.get("head_branch") != head_ref
					or not _review_run_head_on_branch_history(repo, review_run.get("head_sha"), head_sha)))):
			return {"done": False, "state": "open", "reason": f"PR #{number} waiting for verified completed review run {run_id}"}
	if conflicted:
		active = _active_run_count(repo, head_ref, include_pending=True, pr_number=number)
		if active:
			return {"done": False, "state": "open", "reason": f"PR #{number} has a merge conflict, but {active} workflow run(s) on {head_ref} are still queued or running"}
		return {"done": True, "state": "conflict", "since": handoff_since,
			"reason": f"PR #{number} has a merge conflict on head {head_sha[:12]} and no workflow run is active"}
	if latest_handoff is not None and not answered:
		active = _active_run_count(repo, head_ref, include_pending=True, pr_number=number)
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


def _fix_claim_trusted_logins(pr: dict) -> tuple[str, ...]:
	"""Casefolded logins whose `ai:claude-fix-claim` comments count on `pr`.

	Only the identities that actually post claims (issue #4622): the PR's own
	author, whose Claude sessions push and fix it through the session proxy,
	and the configured workflow account CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN, the
	`GH_PAT` login the catch-all sweep posts its reservations with. Any other
	collaborator's claim or hold is ignored. No API calls: `pr` is the
	`pulls/N` object the caller already fetched. Empty when neither is known,
	so no claim counts (fail closed); a `pr` that is not an object counts only
	the workflow account's claims.
	"""
	logins = []
	author = pr.get("user") if isinstance(pr, dict) else None
	if isinstance(author, dict) and isinstance(author.get("login"), str) and author["login"]:
		logins.append(author["login"].casefold())
	workflow_login = os.environ.get("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "").strip()
	if workflow_login:
		logins.append(workflow_login.casefold())
	return tuple(logins)


def _trusted_fix_claims(comments: list, trusted_logins: tuple[str, ...]):
	"""Yield `(head, kind, by, created_at)` for each trusted claim, oldest first.

	A claim is trusted when its comment's author association is owner /
	member / collaborator, its author's casefolded login is in
	`trusted_logins`, and its body carries exactly one claim marker line
	(see `read_fix_claims`). No API calls.
	"""
	for comment in sorted(comments, key=lambda entry: entry.get("id") if type(entry.get("id")) is int else 0):
		if comment.get("author_association") not in FIX_CLAIM_TRUSTED_ASSOCIATIONS:
			continue
		claim_author = comment.get("user")
		if (not isinstance(claim_author, dict) or not isinstance(claim_author.get("login"), str)
			or claim_author["login"].casefold() not in trusted_logins):
			continue
		body = comment.get("body")
		if not isinstance(body, str):
			continue
		markers = [match for line in body.splitlines() if (match := FIX_CLAIM_RE.fullmatch(line))]
		if len(markers) != 1:
			continue
		claim_head, claim_kind, claim_by = markers[0].groups()
		yield claim_head, claim_kind, claim_by, comment.get("created_at")


def read_fix_claims(comments: list, head_sha: str, now: dt.datetime, ignore_by: tuple[str, ...] = (),
	trusted_logins: tuple[str, ...] = ()) -> dict:
	"""Summarise the trusted `ai:claude-fix-claim` markers on one PR.

	Input: the PR's issue comments (REST objects), its current head sha, and
	the casefolded `trusted_logins` whose claims count
	(`_fix_claim_trusted_logins`). A claim counts only when its comment's
	author association is owner / member / collaborator **and** its author's
	login is in `trusted_logins`; an empty tuple counts no claim (fail
	closed), so a collaborator cannot forge a hold or push a PR to the cap.
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
	for claim_head, claim_kind, claim_by, claim_created_at in _trusted_fix_claims(comments, trusted_logins):
		if claim_kind in FIX_CLAIM_COUNTED_KINDS:
			counted.add((claim_head, claim_kind))
		if claim_head == head_sha and claim_by not in ignore_by:
			latest = (claim_kind, claim_by, claim_created_at)
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


def _head_has_review_trace(comments: list, head_sha: str, workflow_login: str) -> str | None:
	"""Name the workflow comment that shows `head_sha` was reviewed or skipped on purpose.

	Only comments by the workflow account (`workflow_login`, the
	CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN value) count: a hand-off for the head,
	answered or not (`ai:claude-fixer-handoff:v1`), or the gate's skip notice
	for the head (`ai:claude-fixer-review-skipped:v1`). Returns a short
	description, or None. No API calls: the caller passes its comments.
	"""
	for comment in comments:
		user = comment.get("user")
		body = comment.get("body")
		if not isinstance(user, dict) or user.get("login") != workflow_login or not isinstance(body, str):
			continue
		lines = body.splitlines()
		if not lines:
			continue
		skipped = REVIEW_SKIPPED_MARKER_RE.fullmatch(lines[-1])
		if (skipped and skipped.group(2) == head_sha
			and skipped.group(1) in ("skip_ai_marker", "draft_or_skip_ai")
			and lines[0] == f"## Review skipped: `{skipped.group(1)}`"):
			return f"the gate skipped this head on purpose ({skipped.group(1)})"
		header = FIXER_HANDOFF_HEADER_RE.fullmatch(lines[0])
		if header:
			for line in lines[-2:]:
				handoff = FIXER_HANDOFF_RE.fullmatch(line)
				if (handoff and handoff.group(2) == head_sha and handoff.group(3) == header.group(1)
					and header.group(2) == ("findings handed to the Claude session" if handoff.group(1) == "findings"
						else "merge conflict, handed to the Claude session")):
					return f"a {handoff.group(1)} hand-off for this head"
	return None


def _head_arrival_time(committed: str, head_check_runs: list) -> dt.datetime:
	"""When the head reached the PR, as late as the evidence allows (issue #5376).

	Input: the head commit's committer date (`committed`, which the PR's
	author controls) and the head's check runs (their `started_at`, which
	GitHub sets when a run starts after the push). Output: the later of the
	committer date and the earliest parseable, timezone-aware check-run
	`started_at`, so a backdated commit cannot move the head's arrival
	earlier. With no usable check run it is the committer date. No API calls; an unparseable
	committer date raises `ValueError`.
	"""
	arrived = _parse_time(committed)
	started = []
	for run in head_check_runs:
		if not isinstance(run, dict):
			continue
		try:
			run_started = _parse_time(run.get("started_at"))
		except ValueError:
			continue
		if run_started.tzinfo is not None and arrived.tzinfo is not None:
			started.append(run_started)
	return max(arrived, min(started)) if started else arrived


def _verified_review_redispatch(dispatched_listing: dict, number: int, default_branch: str | None,
	arrived: dt.datetime) -> dict | None:
	"""The completed review run that re-dispatched this head's review, or None.

	Input: the `_dispatched_review_runs` listing that `_active_run_count`
	already read, the PR number, the PR base repository's default branch,
	and the head's arrival time (`_head_arrival_time`). A run counts only
	when it is completed with a conclusion outside
	REDISPATCH_UNCOUNTED_CONCLUSIONS, was a `workflow_dispatch` of the listed
	review workflow on the default branch (so it ran the default branch's
	workflow file, not one a PR branch could change), carries the exact run
	title for PR `number`, and was created at or after `arrived` (the review
	workflow reviews the PR's head at the time it runs, so a later run
	reviewed this head). Claims never count: they are leases, and a PR's
	author can post one (issue #5376). Returns the first matching run
	object; None when the default branch is unknown or nothing matches, so
	the caller retries. No API calls.
	"""
	workflow_path = dispatched_listing.get("workflow")
	title = dispatched_listing.get("title")
	if not default_branch or not workflow_path or not title:
		return None
	expected_title = title.format(number=number)
	for run in dispatched_listing.get("runs") or []:
		path = run.get("path")
		if (run.get("status") != "completed" or run.get("conclusion") in REDISPATCH_UNCOUNTED_CONCLUSIONS
			or run.get("event") != "workflow_dispatch"
			or not isinstance(path, str) or path.split("@", 1)[0] != workflow_path
			or run.get("head_branch") != default_branch or run.get("display_title") != expected_title):
			continue
		try:
			created = _parse_time(run.get("created_at"))
		except ValueError:
			continue
		if created.tzinfo is not None and arrived.tzinfo is not None and created >= arrived:
			return run
	return None


def _review_stall_verdict(repo: str, number: int, pr: dict, head_sha: str, head_ref: str, comments: list,
	now: dt.datetime, ignore_claim_by: tuple[str, ...] = (), head_check_runs: list | None = None) -> dict:
	"""Decide whether a clean `claude/*` head was never reviewed (issue #4985).

	Input: the `pulls/N` object, its comments, and the head's check runs,
	which `check_pr_hand_back` already fetched, for a head with no block
	label, conflict, pending hand-off, or failed check. Output: `{"done":
	False, "state": "open", "reason": …}`, or a due `{"state":
	"review-stalled", "since": <head commit time>, "stall_redispatched":
	bool, "reason": …}`.

	Not stalled, with no API call: CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN is unset
	(workflow comments cannot be trusted, so the check is off); the PR is a
	draft; auto-merge is enabled (a clean review or the deterministic skip);
	it carries a REVIEW_DEFERRED_LABELS label; its title or body holds an
	intentional skip-AI marker; or `_head_has_review_trace` finds a hand-off
	or gate skip notice for the head. Otherwise 1 head-commit read: younger
	than CLAUDE_REVIEW_STALL_HOURS (default 2) is not stalled. Then the
	active-run reads (`_active_run_count`, pending included, up to 4 calls,
	5 in a consumer repo): any active run is not stalled.
	`stall_redispatched` is true when `_verified_review_redispatch` finds a
	completed review run dispatched for this PR from the default branch
	after the head arrived, in the listing those reads already fetched: the
	review was re-run once and still left no trace, so the next fixer holds
	instead of looping. A claim never sets it (issue #5376), so
	`ignore_claim_by` no longer affects it; the parameter stays for callers.
	"""
	workflow_login = os.environ.get("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "").strip()
	if not workflow_login:
		return {"done": False, "state": "open",
			"reason": f"PR #{number} open; review-stall detection is off because CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN is unset"}
	quiet = None
	deferred = [name for name in _label_names(pr) if name in REVIEW_DEFERRED_LABELS]
	if pr.get("draft"):
		quiet = "it is a draft"
	elif pr.get("auto_merge"):
		quiet = "auto-merge is enabled"
	elif deferred:
		quiet = f"its review is deferred ({', '.join(deferred)})"
	elif has_skip_ai_marker(pr.get("title"), pr.get("body")):
		quiet = "it carries the skip-AI marker"
	else:
		quiet = _head_has_review_trace(comments, head_sha, workflow_login)
	if quiet:
		return {"done": False, "state": "open", "reason": f"PR #{number} open, no fix due: {quiet}"}

	stall_hours = _env_positive_float("CLAUDE_REVIEW_STALL_HOURS", DEFAULT_REVIEW_STALL_HOURS)
	commit = gh_api(f"repos/{repo}/commits/{head_sha}")
	committed = commit["commit"]["committer"]["date"]
	age = _hours_since(committed, now)
	if age is None or age < stall_hours:
		return {"done": False, "state": "open",
			"reason": f"PR #{number} head {head_sha[:12]} has no review yet, {0.0 if age is None else age:.1f}h old (< {stall_hours:g}h)"}
	dispatched_listing: dict = {}
	active = _active_run_count(repo, head_ref, include_pending=True, pr_number=number, dispatched_listing=dispatched_listing)
	if active:
		return {"done": False, "state": "open",
			"reason": f"PR #{number} head {head_sha[:12]} has no review yet, but {active} workflow run(s) on {head_ref} are still queued, running or pending"}
	if "runs" not in dispatched_listing:
		dispatched_listing.update(_dispatched_review_runs(repo))
	if not dispatched_listing.get("workflow"):
		raise ReadError("no review workflow found for review-stall verification")
	redispatch_run = _verified_review_redispatch(dispatched_listing, number, _pr_default_branch(pr),
		_head_arrival_time(committed, head_check_runs or []))
	reason = f"PR #{number} head {head_sha[:12]} has no review, hand-off, gate skip notice or active run after {age:.1f}h (>= {stall_hours:g}h)"
	if redispatch_run is not None:
		reason += (f"; review run {redispatch_run.get('id')} was dispatched for this head and completed"
			f" ({redispatch_run.get('conclusion')}) without leaving a trace")
	return {"state": "review-stalled", "since": committed, "stall_redispatched": redispatch_run is not None,
		"reason": reason}


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
	claims = read_fix_claims(comments, head_sha, now, ignore_claim_by, trusted_logins=_fix_claim_trusted_logins(pr))
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
		fixer_verdict = _check_claude_fixer_pr(repo, number, head_sha, head_ref, conflicted, comments=comments,
			default_branch=_pr_default_branch(pr))
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
			# A clean head: the review may never have happened (issue #4985).
			due = _review_stall_verdict(repo, number, pr, head_sha, head_ref, comments, now, ignore_claim_by,
				head_check_runs=runs.get("check_runs") or [])
			if due.get("state") != "review-stalled":
				return {**due, **base}
	if due is None and not conflicted:
		names = ", ".join(sorted(run.get("name", "?") for run in failed))
		completed = sorted(run.get("completed_at") for run in failed if isinstance(run.get("completed_at"), str))
		if head_ref.startswith(CLAUDE_FIXER_HEAD_PREFIX):
			# /implement-plan-claude PRs keep their chain's stuck window (Q22).
			commit = gh_api(f"repos/{repo}/commits/{head_sha}")
			head_age = _hours_since(commit["commit"]["committer"]["date"], now)
			if head_age is None or head_age < stuck_hours:
				return {"done": False, "state": "open", **base,
					"reason": f"PR #{number} has failed checks: {names}, but an implement-plan head waits {stuck_hours:g}h before it counts as stuck"}
		active = _active_run_count(repo, head_ref, include_pending=True, pr_number=number)
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
	blocked_issues = []
	for number in numbers:
		issue = gh_api(f"repos/{repo}/issues/{number}")
		issue_labels = _label_names(issue)
		if issue.get("state") != "closed" and MERGED_ISSUE_LABEL not in issue_labels:
			pending.append(number)
			issue_blocking = [name for name in issue_labels if name in BLOCKING_LABELS]
			if issue_blocking:
				blocked_issues.append(f"#{number} ({', '.join(issue_blocking)})")
	# A follow-up whose pipeline gave up never closes on its own; wake the
	# block stage instead of waiting silently until the safety net.
	if blocked_issues:
		return {"done": True, "state": "blocked", "reason": "issues blocked: " + ", ".join(blocked_issues)}
	if pending:
		return {"done": False, "state": "open", "reason": "issues still open: " + ", ".join(f"#{n}" for n in pending)}
	return {"done": True, "state": "resolved", "reason": "every issue closed or ai:merged: " + ", ".join(f"#{n}" for n in numbers)}


# The routing table `route_verdict` reads (its docstring renders it).
CHECKER_ROUTE_TABLE = {
	"pr": {
		"merged": ("next_stage", "success"),
		"review-round": ("next_stage", "review"),
		"conflict": ("next_stage", "review"),
		"blocked": ("hand_back", None),
		"closed": ("hand_back", None),
		"stuck": ("hand_back", None),
	},
	"run": {
		"completed": ("next_stage", "success"),
		"failed": ("next_stage", "block"),
	},
	"issues": {
		"resolved": ("next_stage", "success"),
		"blocked": ("next_stage", "block"),
	},
	"hand_back": {
		"conflict": ("hand_back_fixer", None),
		"review-round": ("hand_back_fixer", None),
		"ci-failed": ("hand_back_fixer", None),
		"blocked": ("hand_back_fixer", None),
		"review-stalled": ("hand_back_fixer", None),
		"merged": ("hand_back_all", None),
		"closed": ("hand_back_all", None),
	},
}


def route_verdict(verdict: dict, mode: str) -> dict:
	"""Return the routing fields (`action`, plus `next_stage`) for one verdict.

	Input: a verdict dict from `check_pr`, `check_pr_hand_back`, `check_run`
	or `check_issues`, and `mode` — `pr` (plain or --terminal-only; the
	`/implement-plan-claude` project checker), `hand_back` (the CLAUDE.md §26
	checker), `run`, or `issues`. Output: `{"action": ...}` or
	`{"action": "next_stage", "next_stage": ...}`, for the caller to merge
	into the verdict. No API calls; only `done` and `state` are read.

	A verdict with `"done": false` is always `wait`, whatever its `state`.
	For `"done": true`:

	| mode        | state                         | action          | next_stage |
	|-------------|-------------------------------|-----------------|------------|
	| `pr`        | merged                        | next_stage      | success    |
	| `pr`        | review-round, conflict        | next_stage      | review     |
	| `pr`        | blocked, closed, stuck        | hand_back       | —          |
	| `run`       | completed                     | next_stage      | success    |
	| `run`       | failed                        | next_stage      | block      |
	| `issues`    | resolved                      | next_stage      | success    |
	| `issues`    | blocked                       | next_stage      | block      |
	| `hand_back` | conflict, review-round,       | hand_back_fixer | —          |
	|             | ci-failed, blocked,           |                 |            |
	|             | review-stalled                |                 |            |
	| `hand_back` | merged, closed                | hand_back_all   | —          |

	A review round or a conflict is never `hand_back`: it starts a fresh
	review-round stage session (#4596's checker misrouted one). When a
	`hand_back` fails, the checker falls back to `next_stage` `block` itself.
	Run and issue-list waits have no hand-back. A read failure (exit 2) is
	`retry`; `main` adds it. Any other done state raises `ValueError`, so an
	unmapped state is a loud exit-2 `retry`, never a silent guess.
	"""
	if not verdict.get("done"):
		return {"action": "wait"}
	state = verdict.get("state")
	routed = CHECKER_ROUTE_TABLE.get(mode, {}).get(state)
	if routed is None:
		raise ValueError(f"no checker route for mode {mode!r} state {state!r}")
	action, next_stage = routed
	return {"action": action, "next_stage": next_stage} if next_stage else {"action": action}


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
		print(json.dumps({"done": False, "error": f"--repo must be OWNER/REPO, got {args.repo!r}", "action": "retry"}))
		return 2
	if args.pr is None and args.hand_back:
		print(json.dumps({"done": False, "error": "--hand-back needs --pr", "action": "retry"}))
		return 2
	now = now or dt.datetime.now(dt.timezone.utc)
	try:
		if args.pr is not None and args.hand_back:
			verdict = check_pr_hand_back(args.repo, args.pr, args.stuck_hours, args.min_age_hours, now,
				tuple(args.ignore_claim_by))
			route_mode = "hand_back"
		elif args.pr is not None:
			verdict = check_pr(args.repo, args.pr, args.terminal_only, args.stuck_hours, now)
			route_mode = "pr"
		elif args.run is not None:
			verdict = check_run(args.repo, args.run)
			route_mode = "run"
		else:
			verdict = check_issues(args.repo, args.issues)
			route_mode = "issues"
		verdict = {**verdict, **route_verdict(verdict, route_mode)}
	except (ReadError, KeyError, TypeError, ValueError) as exc:
		print(json.dumps({"done": False, "error": str(exc), "action": "retry"}))
		return 2
	print(json.dumps(verdict))
	return 0


if __name__ == "__main__":
	sys.exit(main())

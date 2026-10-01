"""Enable auto-merge for a clean Claude-fixer review that finished before CI.

Issue #4900. In Claude-fixer mode the reviewer panel often finishes before
the head's CI does (PR #4869: 6 reviewers clean at 00:51:30Z, `lint` green at
00:55:09Z). The review workflow's hand-off step
(`scripts/review_autofix_step_claude_fixer_handoff.sh`) then posts a
pending-checks comment instead of a findings hand-off:

  ## Review round <n>: clean review, waiting for check runs
  ...
  Reviewed head: `<sha>` ([workflow run](<run url>)).
  ...
  <!-- ai:claude-fixer-pending-checks:v1 head=<sha> round=<n> ledger=<sha256> -->
  <!-- ai:claude-fixer-pending-checks:v2 head=<sha> round=<n> ledger=<sha256> base_sha=<sha> base_ref_sha256=<sha256> -->

That comment is not a hand-off, so `.claude/scripts/check_in_status.py` keeps
waiting and wakes no Claude session. This module is the readiness half: the
hourly `claude-pr-catch-all` job (`scripts/claude_pr_sweep.py`) calls
`evaluate` for every open `claude/*` PR whose hand-back verdict is `open`,
and when the head's check runs have all completed without a failure it
enables head-bound auto-merge through `scripts/review_enable_auto_merge.sh`.
The reviewers never run again.

Fail closed. Auto-merge is enabled only when ALL of these hold:
  * the PR is open, not a draft, on a `claude/*` head, with no blocking label
    (ai:review-blocked / ai:review-autofix-failed / ai:needs-human), no merge
    conflict, and auto-merge not already enabled;
  * the latest trusted pending-checks comment names the PR's current head
    (posted by CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN, the review workflow's
    GH_PAT account; issued header; one marker; round matching the header;
    the workflow's own run link), and no later trusted hand-off for that
    head supersedes it;
  * that comment's v2 line (same head, round, and ledger as its v1 line)
    binds it to the base the review ran against, and the PR's current
    `base.sha` and sha256 of its `base.ref` still equal that binding
    (issue #5147: a PR retargeted after a clean review, with the head
    unchanged, must not merge into a base nobody reviewed). A missing
    binding is `base_unbound`, a different one `base_changed`; the review
    gate then stops skipping dispatched re-runs on the head, so the
    30-minute review sweep reviews the PR again against its new base;
  * a fresh snapshot of the head's check runs
    (`scripts/collect_pr_check_runs_context.py`, no wait, no log tails) is
    `collection_status: ready` for the same head, with at least one check
    run, `failed_count: 0` and `incomplete_count: 0`;
  * the review run the comment links completed with `success`, in this
    repository, from a review workflow, on the PR's head branch, triggered
    by the reviewed head or an earlier push to it (or dispatched for this PR
    by the review sweep from the default branch, bound by its run title,
    issue #4618);
  * no newer review of the PR is still running (issue #5148): no run on the
    head branch, no internal-review.yml dispatch titled for this PR, and no
    review_autofix.yml / ai-review.yml / review_rb_judge_dispatch.yml dispatch
    (those carry no PR binding)
    is in any status but `completed`, older runs included (each listing is
    also read with `status=` when it may go on past the pages read); the
    latest completed review run bound
    to this PR that is newer than the marker's run concluded `success`; and
    a re-read of the comments, after those run reads, still finds the same
    marker (a newer review that finished in between has posted its own
    comment by then);
  * the repository's ENABLE_AUTO_MERGE variable reads `true` (unset = `true`;
    an unreadable variable does not merge).
`review_enable_auto_merge.sh` keeps its own guards on top: `--match-head-commit`
on the reviewed head, the e2e-smoke-test label, and integration-branch heads.
It also gets the reviewed base (REVIEWED_BASE_REF / REVIEWED_BASE_SHA) and
refuses, from the PR read it makes right before the merge call, a PR that was
retargeted after the binding check above (issue #5905: that check reads the
PR minutes and a dozen calls earlier). GitHub's merge APIs bind only the head,
so after the helper enabled auto-merge the PR is read once more: a head or
base that moved in between, or a re-read that fails, disables auto-merge again
(`gh pr merge --disable-auto`), and the gate's stale-binding rule (#5147) lets
the next review sweep review the PR against its new base.

A check that finishes failed is not handled here: `check_in_status.py
--hand-back` reports it as `ci-failed` like any other `claude/*` head, and a
push starts a new review round.

API budget (CLAUDE.md §15), per evaluated PR: 1 PR read and 1 read per 100
comments. The base binding reuses the PR read (no call). With a live marker
bound to the current base, 1 paginated check-runs read (one call per 100
check runs, through the collector). Only when the snapshot is ready: 1 review
run read (+1 compare read when that run was triggered by an older push), 1
head-branch runs listing and 4 workflow_dispatch runs listings
(internal-review.yml, review_autofix.yml, ai-review.yml,
review_rb_judge_dispatch.yml; a missing workflow costs its one 404), each 1
call per 100 runs down to the marker's run (usually 1, at most 10), and,
for a listing whose last page read was full, 1 status-filtered listing
for each of the 5 run statuses but `completed` (1 call per 100 runs,
usually 1, at most 10), 1
read per 100 comments again, 1 repository-variable read, then
`review_enable_auto_merge.sh` (1 paginated labels read, 1 PR read, 1 merge
call), and after a merge call that succeeded, 1 more PR read plus, only when
the reviewed head or base no longer holds, 1 `gh pr merge --disable-auto`
(GraphQL) write. Every read goes through `gh api` with
GH_TOKEN; nothing is retried in a loop, and a failed read raises
`check_in_status.ReadError` for the caller to log (fail open per PR).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent


def _load(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


check_in_status = _load("check_in_status", ROOT / ".claude" / "scripts" / "check_in_status.py")

CHECK_RUNS_COLLECTOR = SCRIPT_DIR / "collect_pr_check_runs_context.py"
AUTO_MERGE_SCRIPT = SCRIPT_DIR / "review_enable_auto_merge.sh"
PENDING_CHECKS_MARKER_RE = re.compile(
	r"^<!-- ai:claude-fixer-pending-checks:v1 head=([0-9a-f]{40}) round=([1-9][0-9]*) ledger=([0-9a-f]{64}) -->$"
)
PENDING_CHECKS_V2_MARKER_RE = re.compile(
	r"^<!-- ai:claude-fixer-pending-checks:v2 head=([0-9a-f]{40}) round=([1-9][0-9]*) ledger=([0-9a-f]{64})"
	r" base_sha=([0-9a-f]{40}) base_ref_sha256=([0-9a-f]{64}) -->$"
)
PENDING_CHECKS_HEADER_RE = re.compile(r"## Review round ([1-9][0-9]*): clean review, waiting for check runs")
# review_autofix.yml's defaults for the variables review_enable_auto_merge.sh
# reads; only ENABLE_AUTO_MERGE is per-repo policy that can stop a claude/*
# merge, so it is the one variable read from the repository.
DEFAULT_ENABLE_AUTO_MERGE = "true"
DEFAULT_FORWARD_MERGE_FALLBACK_AUTO_MERGE = "true"
DEFAULT_ORCH_INTEGRATION_BRANCH_PATTERN = "^orchestrator/project-"
SUBPROCESS_TIMEOUT_SECS = 180
# review_enable_auto_merge.sh exits 0 on every path, including the ones that
# suppress auto-merge, so the line it prints after `gh pr merge --auto`
# succeeded is the only success signal. tests/test_claude_fixer_pending_checks.py
# pins it against the helper, so a wording change fails CI instead of turning
# every merge into `merge_failed`.
AUTO_MERGE_ENABLED_LINE_PREFIX = "Auto-merge enabled."
# The line review_enable_auto_merge.sh prints when the PR no longer targets
# the REVIEWED_BASE_REF / REVIEWED_BASE_SHA it was given (issue #5905);
# pinned against the helper by the tests like the success line above.
AUTO_MERGE_BASE_REFUSED_LINE_PREFIX = "AUTOFIX_AUTO_MERGE_HEAD_BOUND pr="
AUTO_MERGE_BASE_REFUSED_LINE_SUFFIX = " action=refuse reason=base_changed"
# The forms `gh api` (and the GitHub error body it prints to stdout) use for a
# 404; scripts/gh_helpers.sh recognises the same set.
NOT_FOUND_RE = re.compile(r"HTTP 404|gh: Not Found|404 Not Found|status code 404|\"status\":\s*\"404\"", re.IGNORECASE)
# Review workflows whose workflow_dispatch runs carry no PR in their run name
# (review_autofix.yml here: convergence and direct dispatches; ai-review.yml
# in consumers; review_rb_judge_dispatch.yml in both, which the orchestrator
# stall poller dispatches with force_rb_judge=true and which, on a
# Claude-fixer head, labels the PR ai:review-blocked). The review gate lets a
# force_rb_judge or force-review dispatch through on a pending-checks head, so
# while any such run is active it may be a newer review of this PR
# (issue #5148).
UNBOUND_DISPATCH_REVIEW_WORKFLOWS = (".github/workflows/review_autofix.yml", ".github/workflows/ai-review.yml",
	".github/workflows/review_rb_judge_dispatch.yml")
REVIEW_RUNS_PER_PAGE = 100
# Every Actions run status but `completed` (issue #5148 AD-3: any status other
# than `completed` counts as active). A listing paged down to the marker's run
# leaves older runs unread, so each of these is listed with `status=` whenever
# that listing may go on past its last page (PR #5178 review of 95d932b).
ACTIVE_REVIEW_RUN_STATUSES = ("requested", "waiting", "pending", "queued", "in_progress")


def _comment_id(comment: dict) -> int:
	value = comment.get("id")
	return value if type(value) is int else 0


def base_ref_digest(ref: str) -> str:
	"""sha256 (hex) of a base ref name, as the hand-off step writes it into the v2 marker.

	`surrogatepass` keeps a ref that JSON decoding left with a lone surrogate
	from raising `UnicodeEncodeError` mid-evaluation: its digest is simply
	one no hand-off step can have written, so `evaluate` reports
	`base_changed`. The encoding stays injective, so two different refs
	never share a digest.
	"""
	return hashlib.sha256(ref.encode("utf-8", "surrogatepass")).hexdigest()


def find_pending_marker(comments: list, repo: str, head_sha: str, author_login: str) -> dict | None:
	"""Return the live pending-checks marker for `head_sha`, or None.

	Input: the PR's issue comments (REST objects), the repository, the PR's
	current head, and the review workflow's comment account. Only that
	account's comments count; an empty login counts none (fail closed). The
	latest valid pending-checks comment for the head wins, and a later
	findings or conflict hand-off for the same head supersedes it (the head
	is then a review round, not a pending merge). No API calls.

	Output: {"comment_id", "round", "ledger", "run_id", "run_url",
	"created_at", "base_sha", "base_ref_sha256"} or None. The base binding
	comes from the comment's single v2 line whose head, round, and ledger
	equal its v1 line's; without one (none, several, or a mismatch) both
	binding fields are None, and `evaluate` never merges on it.
	"""
	if not author_login or not re.fullmatch(r"[0-9a-f]{40}", head_sha or ""):
		return None
	run_line_prefix = f"Reviewed head: `{head_sha}` ("
	run_link_pattern = re.compile(
		re.escape(run_line_prefix) + r"\[workflow run\]\((https://[^/\s)]+/" + re.escape(repo) + r"/actions/runs/([1-9][0-9]*))\)\)\."
	)
	live = None
	for comment in sorted(comments, key=_comment_id):
		if _comment_id(comment) <= 0:
			continue
		user = comment.get("user")
		body = comment.get("body")
		if not isinstance(user, dict) or user.get("login") != author_login or not isinstance(body, str):
			continue
		lines = body.splitlines()
		if not lines:
			continue
		if check_in_status.FIXER_HANDOFF_HEADER_RE.fullmatch(lines[0]) and any(
			(match := check_in_status.FIXER_HANDOFF_RE.fullmatch(line)) and match.group(2) == head_sha for line in lines
		):
			live = None
			continue
		header = PENDING_CHECKS_HEADER_RE.fullmatch(lines[0])
		markers = [match for line in lines if (match := PENDING_CHECKS_MARKER_RE.fullmatch(line))]
		if not header or len(markers) != 1:
			continue
		marker_head, round_text, ledger = markers[0].groups()
		if marker_head != head_sha or round_text != header.group(1):
			continue
		run_lines = [line for line in lines if line.startswith(run_line_prefix)]
		run_link = run_link_pattern.fullmatch(run_lines[0]) if len(run_lines) == 1 else None
		if not run_link:
			continue
		bindings = [match for line in lines if (match := PENDING_CHECKS_V2_MARKER_RE.fullmatch(line))]
		binding = bindings[0].groups() if len(bindings) == 1 else None
		if binding is not None and binding[:3] != (marker_head, round_text, ledger):
			binding = None
		live = {
			"comment_id": _comment_id(comment),
			"round": int(round_text),
			"ledger": ledger,
			"run_id": int(run_link.group(2)),
			"run_url": run_link.group(1),
			"created_at": comment.get("created_at") if isinstance(comment.get("created_at"), str) else None,
			"base_sha": binding[3] if binding else None,
			"base_ref_sha256": binding[4] if binding else None,
		}
	return live


def parse_check_snapshot(text: str, head_sha: str) -> dict:
	"""Classify one `collect_pr_check_runs_context.py` snapshot of `head_sha`.

	Output: {"state": ready | incomplete | failed | invalid, "detail": str}.
	`ready` needs the snapshot header for the same head,
	`collection_status: ready`, at least one check run, and zero failed and
	incomplete runs (the same test the hand-off step applies). A failed run
	(the collector counts `cancelled` and `stale` as failed too) is `failed`;
	a run still queued or in progress is `incomplete`; a missing, disabled,
	errored, or malformed snapshot is `invalid`. No API calls.
	"""
	lines = text.splitlines()
	if len(lines) < 6 or lines[0] != "PR_CHECK_RUNS_CONTEXT" or lines[1] != f"head_sha: {head_sha}":
		return {"state": "invalid", "detail": "snapshot missing or for another head"}
	status_match = re.fullmatch(r"collection_status: ([a-z_]+)", lines[2])
	counts = {}
	for line in lines[3:6]:
		match = re.fullmatch(r"(total_check_runs|failed_count|incomplete_count): ([0-9]+)", line)
		if match:
			counts[match.group(1)] = int(match.group(2))
	if not status_match or len(counts) != 3:
		return {"state": "invalid", "detail": "malformed snapshot header"}
	status = status_match.group(1)
	failed_names = [line.split(": ", 1)[1] for line in lines if re.match(r"^failed\[[0-9]+\]\.name: ", line)]
	incomplete_names = [line.split(": ", 1)[1] for line in lines if re.match(r"^incomplete\[[0-9]+\]\.name: ", line)]
	if status not in ("ready", "timeout"):
		return {"state": "invalid", "detail": f"collection_status {status}"}
	if counts["failed_count"] or failed_names:
		return {"state": "failed", "detail": ", ".join(failed_names) or f"{counts['failed_count']} failed"}
	if counts["incomplete_count"] or incomplete_names:
		return {"state": "incomplete", "detail": ", ".join(incomplete_names) or f"{counts['incomplete_count']} incomplete"}
	if status != "ready" or counts["total_check_runs"] < 1:
		return {"state": "invalid", "detail": f"collection_status {status}, {counts['total_check_runs']} check runs"}
	return {"state": "ready", "detail": f"{counts['total_check_runs']} check runs completed, none failed"}


def read_check_snapshot(repo: str, head_sha: str) -> dict:
	"""Snapshot `head_sha`'s check runs once and classify it (`parse_check_snapshot`).

	Runs `collect_pr_check_runs_context.py` with CHECK_RUNS_WAIT_TIMEOUT_SECS=0
	(one paginated check-runs read, no polling) and no log tails. A collector
	that cannot run leaves no snapshot, which classifies as `invalid`.
	"""
	with tempfile.TemporaryDirectory() as tmp:
		payload_path = Path(tmp) / "pr.json"
		payload_path.write_text(json.dumps({"head": {"sha": head_sha}}), encoding="utf-8")
		context_path = Path(tmp) / "check_runs_context.txt"
		env = {
			**os.environ,
			"GITHUB_REPOSITORY": repo,
			"PR_PAYLOAD_FILE": str(payload_path),
			"PR_CHECK_RUNS_CONTEXT_FILE": str(context_path),
			"CHECK_RUNS_AUTOFIX_ENABLED": "true",
			"CHECK_RUNS_WAIT_TIMEOUT_SECS": "0",
			"CHECK_RUNS_LOG_TAIL_BYTES": "0",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
		for name in ("SELF_RUN_ID", "CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT"):
			env.pop(name, None)
		try:
			subprocess.run([sys.executable, str(CHECK_RUNS_COLLECTOR)], env=env, capture_output=True, text=True,
				timeout=SUBPROCESS_TIMEOUT_SECS, check=False)
		except (OSError, subprocess.TimeoutExpired):
			return {"state": "invalid", "detail": "check-run collector did not run"}
		try:
			text = context_path.read_text(encoding="utf-8")
		except OSError:
			text = ""
	return parse_check_snapshot(text, head_sha)


def verify_review_run(repo: str, marker: dict, head_sha: str, head_ref: str,
	number: int | None = None, default_branch: str | None = None) -> str:
	"""Return "" when the marker's review run is verified, else the reason it is not.

	The run must be the linked one, in `repo`, from a review workflow
	(`check_in_status.FIXER_WORKFLOW_PATHS`), completed with `success`, and
	either on `head_ref` and triggered by the reviewed head or an earlier push
	to it, or an internal-review.yml run the review sweep dispatched for PR
	`number` from `default_branch` (issue #4618), bound by its exact title
	the same way `check_in_status.py` verifies a hand-off's run. One run
	read, plus one compare read when a head-branch run's trigger commit
	differs.
	"""
	run = check_in_status.gh_api(f"repos/{repo}/actions/runs/{marker['run_id']}")
	run_repo = run.get("repository")
	run_path = run.get("path")
	dispatched = number is not None and check_in_status._is_pr_dispatched_review_run(run, number, default_branch)
	if (run.get("id") != marker["run_id"] or run.get("html_url") != marker["run_url"]
		or not isinstance(run_repo, dict) or run_repo.get("full_name") != repo
		or not isinstance(run_path, str) or run_path.split("@", 1)[0] not in check_in_status.FIXER_WORKFLOW_PATHS
		or (not dispatched and run.get("head_branch") != head_ref)):
		return f"review run {marker['run_id']} does not match this PR's review workflow"
	if run.get("status") != "completed" or run.get("conclusion") != "success":
		return f"review run {marker['run_id']} is {run.get('status')}/{run.get('conclusion')}"
	if not dispatched and not check_in_status._review_run_head_on_branch_history(repo, run.get("head_sha"), head_sha):
		return f"review run {marker['run_id']} was not triggered from this head's history"
	return ""


def _read_review_run_listing(path: str, missing_ok: bool = False, title_bound: bool = False) -> list[dict] | None:
	"""The `workflow_runs` of one Actions runs listing; with `missing_ok`, None when the workflow does not exist (404).

	Every run must carry an integer `id` and a string `status`; a run without
	them cannot be ordered against the marker's run or classified as active,
	so the listing raises `check_in_status.ReadError` instead of being read
	around (fail closed). With `title_bound` (the internal-review.yml
	listing, whose runs are bound to a PR by title alone) every run must also
	carry a non-blank string `display_title`: a run without one could be this
	PR's review, and skipping it as unbound would fail open (PR #5178 review
	rounds 2 and 3).
	"""
	try:
		listing = check_in_status.gh_api(path)
	except check_in_status.ReadError as exc:
		if missing_ok and NOT_FOUND_RE.search(str(exc)):
			return None
		raise
	runs = listing.get("workflow_runs")
	if not isinstance(runs, list) or any(
		not isinstance(run, dict) or type(run.get("id")) is not int or not isinstance(run.get("status"), str)
		or (title_bound and not (isinstance(run.get("display_title"), str) and run["display_title"].strip()))
		for run in runs
	):
		raise check_in_status.ReadError(f"gh api {path} returned a malformed runs listing")
	return runs


def _read_review_runs_to_marker(path: str, marker_run_id: int, missing_ok: bool = False,
	title_bound: bool = False) -> list[dict] | None:
	"""Every run of one newest-first Actions runs listing down to the marker's run; with `missing_ok`, None on a 404.

	`path` ends in `per_page=REVIEW_RUNS_PER_PAGE`. Page 1 is always read,
	then page after page until one holds a run with an id at or below
	`marker_run_id` (every run newer than the marker's run has then been
	seen) or a short page ends the listing: 1 call in the usual case, at most
	`check_in_status.MAX_PAGINATED_API_PAGES`. Needing more raises
	`check_in_status.ReadError` rather than deciding on a partial listing
	(PR #5178 review round 5: 100 internal-review.yml dispatches span about
	90 minutes in coding-workflows, so a marker older than that fell off the
	one page read before). Only page 1 may 404 (`missing_ok`); each page is
	validated by `_read_review_run_listing`.
	"""
	runs: list[dict] = []
	for page_number in range(1, check_in_status.MAX_PAGINATED_API_PAGES + 1):
		page = _read_review_run_listing(f"{path}&page={page_number}", missing_ok=missing_ok and page_number == 1,
			title_bound=title_bound)
		if page is None:
			return None
		runs.extend(page)
		if len(page) < REVIEW_RUNS_PER_PAGE or any(run["id"] <= marker_run_id for run in page):
			return runs
	raise check_in_status.ReadError(f"gh api {path} listed {check_in_status.MAX_PAGINATED_API_PAGES} pages "
		f"without reaching the marker's run {marker_run_id}")


def _read_active_runs_past_listing(path: str, listing: list[dict], title_bound: bool = False) -> list[dict]:
	"""The active runs of the listing at `path` that `listing` may have left unread; [] when it read them all.

	`listing` is what `_read_review_runs_to_marker(path, …)` returned. It
	ends on a short page when it holds the whole listing (it is then empty,
	or its length is not a multiple of REVIEW_RUNS_PER_PAGE): no call.
	Otherwise older runs may lie past its last page, where a queued or
	running review would go unseen (PR #5178 review of 95d932b), so each
	ACTIVE_REVIEW_RUN_STATUSES entry is listed with `status=<status>`, page
	after page until a short page: 1 call per status in the usual case, at
	most `check_in_status.MAX_PAGINATED_API_PAGES` each. Needing more, any
	failed read (a 404 included, since page 1 of `path` was already read), and
	a malformed page (`_read_review_run_listing`, with `title_bound`) raise
	`check_in_status.ReadError` (fail closed). Returns only runs whose status is
	not `completed`.
	"""
	if not listing or len(listing) % REVIEW_RUNS_PER_PAGE:
		return []
	active: list[dict] = []
	for status in ACTIVE_REVIEW_RUN_STATUSES:
		for page_number in range(1, check_in_status.MAX_PAGINATED_API_PAGES + 1):
			page = _read_review_run_listing(f"{path}&status={status}&page={page_number}", title_bound=title_bound)
			active.extend(run for run in page if run["status"] != "completed")
			if len(page) < REVIEW_RUNS_PER_PAGE:
				break
		else:
			raise check_in_status.ReadError(f"gh api {path}&status={status} listed "
				f"{check_in_status.MAX_PAGINATED_API_PAGES} full pages of active runs")
	return active


def _run_path(run: dict) -> str:
	path = run.get("path")
	return path.split("@", 1)[0] if isinstance(path, str) else ""


def check_review_runs(repo: str, number: int, head_ref: str, marker_run_id: int) -> dict | None:
	"""Return why a newer review of PR `number` blocks the marker's merge, or None (issue #5148).

	A forced or judge review dispatched from the default branch, or any other
	review of the PR, can still be running while the marker's head has green
	checks; enabling auto-merge then would skip whatever it finds. Input: the
	PR's head branch and the id of the review run the marker links.

	Reads, 5 listings in all, each newest first and 100 runs per call:
	`actions/runs?branch=<head_ref>`, then the workflow_dispatch runs of
	internal-review.yml (`check_in_status.DISPATCHED_REVIEW_RUNS_PATH`) and
	of each of the 3 UNBOUND_DISPATCH_REVIEW_WORKFLOWS entries (a 404 on
	their first page, the workflow not existing in that repo, is no runs).
	Each listing is paged until it reaches the marker's run
	(`_read_review_runs_to_marker`): 1 call in the usual case, at most
	`check_in_status.MAX_PAGINATED_API_PAGES`, so every run newer than the
	marker's run is seen. A listing whose last page read was full may hold
	older runs past it, so for that listing each run status but `completed`
	is also listed (`_read_active_runs_past_listing`: 1 call per status in
	the usual case, at most `check_in_status.MAX_PAGINATED_API_PAGES` each),
	and an older queued or running review still counts as active (PR #5178
	review of 95d932b). Any other failed read, a 404 on the head-branch
	listing or on a status listing included, a listing that needs more
	pages than that, and a listing with a run that
	has no integer `id` or string `status` (or, in the internal-review.yml
	listing, no non-blank string `display_title`) raise
	`check_in_status.ReadError`. These reads are only part of
	`evaluate()`'s per-PR budget, which the module docstring states in full,
	the comments re-read after them included.

	Output: None, or {"state": "review_active" | "review_superseded",
	"reason": str}. `review_active`: a run on the head branch, an
	internal-review.yml dispatch titled for this PR
	(`check_in_status.DISPATCHED_REVIEW_TITLE`), or any unbound review dispatch
	is in any status but `completed`. `review_superseded`: of the completed
	review runs bound to this PR (a head-branch run of
	`check_in_status.FIXER_WORKFLOW_PATHS`, or an internal-review.yml dispatch
	titled for it), only those with an id above `marker_run_id` count, and the
	newest of them did not conclude `success`. The id filter is required: the
	marker's own run is the review it records, and older runs came before
	that review, so neither can supersede it. Gate-skipped dispatches
	conclude `success`; a newer review that posted findings or a newer marker
	is caught by `find_pending_marker`.
	"""
	branch_path = f"repos/{repo}/actions/runs?branch={quote(head_ref, safe='/')}&per_page={REVIEW_RUNS_PER_PAGE}"
	branch_runs = _read_review_runs_to_marker(branch_path, marker_run_id)
	title = check_in_status.DISPATCHED_REVIEW_TITLE.format(number=number)
	dispatched_path = check_in_status.DISPATCHED_REVIEW_RUNS_PATH.format(repo=repo)
	dispatched_runs = _read_review_runs_to_marker(dispatched_path, marker_run_id, missing_ok=True, title_bound=True)
	bound_dispatches = [run for run in dispatched_runs or [] if run.get("display_title") == title]
	active = [run for run in branch_runs + bound_dispatches if run.get("status") != "completed"]
	active.extend(_read_active_runs_past_listing(branch_path, branch_runs))
	if dispatched_runs is not None:
		active.extend(run for run in _read_active_runs_past_listing(dispatched_path, dispatched_runs, title_bound=True)
			if run.get("display_title") == title)
	for workflow_path in UNBOUND_DISPATCH_REVIEW_WORKFLOWS:
		unbound_path = (f"repos/{repo}/actions/workflows/{Path(workflow_path).name}/runs"
			f"?event=workflow_dispatch&per_page={REVIEW_RUNS_PER_PAGE}")
		listing = _read_review_runs_to_marker(unbound_path, marker_run_id, missing_ok=True)
		if listing is not None:
			active.extend(run for run in listing if run.get("status") != "completed")
			active.extend(_read_active_runs_past_listing(unbound_path, listing))
	if active:
		names = ", ".join(sorted({f"{_run_path(run) or 'unknown workflow'} run {run.get('id')} ({run.get('status')})" for run in active}))
		return {"state": "review_active", "reason": f"a newer review of this PR may still be running: {names}"}
	bound_reviews = [run for run in branch_runs if _run_path(run) in check_in_status.FIXER_WORKFLOW_PATHS] + bound_dispatches
	# Keep the id filter: the marker's own run is the review it records and
	# older runs came before it, so only a strictly newer run can supersede it.
	newer = [run for run in bound_reviews if run["id"] > marker_run_id]
	if newer:
		latest = max(newer, key=lambda run: run["id"])
		if latest.get("conclusion") != "success":
			return {"state": "review_superseded", "reason": f"the latest review run of this PR, {latest['id']} "
				f"({_run_path(latest)}), is newer than the marker's run {marker_run_id} and concluded {latest.get('conclusion')}"}
	return None


def read_enable_auto_merge(repo: str) -> str | None:
	"""The repository's ENABLE_AUTO_MERGE variable: its value, the default when unset (404), None when unreadable."""
	try:
		proc = subprocess.run(["gh", "api", f"repos/{repo}/actions/variables/ENABLE_AUTO_MERGE"],
			capture_output=True, text=True, timeout=60, check=False)
	except (OSError, subprocess.TimeoutExpired):
		return None
	if proc.returncode != 0:
		return DEFAULT_ENABLE_AUTO_MERGE if NOT_FOUND_RE.search(f"{proc.stderr}\n{proc.stdout}") else None
	try:
		payload = json.loads(proc.stdout)
	except ValueError:
		return None
	value = payload.get("value") if isinstance(payload, dict) else None
	return value if isinstance(value, str) else None


def enable_auto_merge(repo: str, number: int, head_sha: str, enable_flag: str,
	base_ref: str = "", base_sha: str = "") -> dict:
	"""Run review_enable_auto_merge.sh for the reviewed head (and base); {"enabled", "base_refused", "output"}.

	`base_ref` / `base_sha` are the base the review ran against, already
	checked against the marker's v2 binding. They go to the helper as
	REVIEWED_BASE_REF / REVIEWED_BASE_SHA, which it compares with its own PR
	read right before the merge call (issue #5905); `base_refused` is True
	when it refused for that reason. Both empty keeps the head-only helper.
	"""
	env = {
		**os.environ,
		"GITHUB_REPOSITORY": repo,
		"PR_NUMBER": str(number),
		"ENABLE_AUTO_MERGE": enable_flag,
		"FORWARD_MERGE_FALLBACK_AUTO_MERGE": DEFAULT_FORWARD_MERGE_FALLBACK_AUTO_MERGE,
		"ORCH_INTEGRATION_BRANCH_PATTERN": DEFAULT_ORCH_INTEGRATION_BRANCH_PATTERN,
		"INITIAL_HEAD_SHA": head_sha,
		"REVIEWED_BASE_REF": base_ref,
		"REVIEWED_BASE_SHA": base_sha,
	}
	# The helper records AUTO_MERGE_READY_LABELS_ALLOWED for later workflow
	# steps; the sweep has none, so keep it out of the job environment.
	env.pop("GITHUB_ENV", None)
	try:
		proc = subprocess.run(["bash", str(AUTO_MERGE_SCRIPT)], env=env, capture_output=True, text=True,
			timeout=SUBPROCESS_TIMEOUT_SECS, check=False)
	except (OSError, subprocess.TimeoutExpired) as exc:
		return {"enabled": False, "base_refused": False, "output": f"review_enable_auto_merge.sh did not run: {exc}"}
	output = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
	stdout_lines = proc.stdout.splitlines()
	enabled = proc.returncode == 0 and any(line.startswith(AUTO_MERGE_ENABLED_LINE_PREFIX) for line in stdout_lines)
	base_refused = not enabled and any(line.startswith(AUTO_MERGE_BASE_REFUSED_LINE_PREFIX) and line.endswith(AUTO_MERGE_BASE_REFUSED_LINE_SUFFIX)
		for line in stdout_lines)
	return {"enabled": enabled, "base_refused": base_refused, "output": output}


def recheck_reviewed_pair(repo: str, number: int, head_sha: str, base_ref: str, base_sha: str) -> dict:
	"""Re-read PR `number` after auto-merge was enabled and compare it with the reviewed head and base (issue #5905).

	The helper's PR read and its `gh pr merge --auto` call are separate, so
	a retarget in between still gets auto-merge for the new base. One PR
	read. Output: {"outcome": "held" | "moved" | "merged_elsewhere" |
	"unreadable", "detail": str}. `held`: the PR still has the reviewed head
	and base (merged or not). `moved`: not merged, and its head or base is no
	longer the reviewed one. `merged_elsewhere`: already merged, into a base
	other than the reviewed one. `unreadable`: the read failed or returned no
	usable head and base; the caller treats it like `moved` (fail closed).
	"""
	try:
		pr = check_in_status.gh_api(f"repos/{repo}/pulls/{number}")
	except check_in_status.ReadError as exc:
		return {"outcome": "unreadable", "detail": f"could not re-read the PR: {exc}"}
	if not isinstance(pr, dict):
		return {"outcome": "unreadable", "detail": "the PR re-read was not a JSON object"}
	head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
	base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
	current_head = head.get("sha") if isinstance(head.get("sha"), str) else ""
	current_ref = base.get("ref") if isinstance(base.get("ref"), str) else ""
	current_sha = base.get("sha") if isinstance(base.get("sha"), str) else ""
	if not current_head or not current_ref or not current_sha:
		return {"outcome": "unreadable", "detail": "the PR re-read carried no head sha, base ref, or base sha"}
	detail = f"head {current_head[:12]}, base {current_ref} at {current_sha[:12]}"
	if pr.get("merged") is True:
		# A merge may refresh the PR's base.sha snapshot to the base tip it
		# merged onto, so a merged PR is judged by its head and base ref: a
		# retarget always changes the ref.
		if current_head == head_sha and current_ref == base_ref:
			return {"outcome": "held", "detail": detail}
		return {"outcome": "merged_elsewhere", "detail": detail}
	if current_head == head_sha and current_ref == base_ref and current_sha == base_sha:
		return {"outcome": "held", "detail": detail}
	return {"outcome": "moved", "detail": detail}


def revoke_auto_merge(repo: str, number: int) -> dict:
	"""Disable auto-merge on PR `number` (`gh pr merge --disable-auto`, 1 GraphQL write); {"revoked": bool, "output": str}."""
	try:
		proc = subprocess.run(["gh", "pr", "merge", str(number), "--repo", repo, "--disable-auto"],
			capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECS, check=False)
	except (OSError, subprocess.TimeoutExpired) as exc:
		return {"revoked": False, "output": f"gh pr merge --disable-auto did not run: {exc}"}
	output = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
	return {"revoked": proc.returncode == 0, "output": output}


def evaluate(repo: str, number: int, *, author_login: str, dry_run: bool = False) -> dict:
	"""Decide (and, when ready, act) for one PR; see the module docstring.

	Output: {"state": ..., "reason": ...} plus `head_sha` once known. States:
	`not_eligible` (closed, draft, not claude/*, blocked, conflicted,
	auto-merge already on), `no_marker`, `base_unbound` (the marker has no
	v2 base binding), `base_changed` (the PR's base ref or base sha differs
	from the binding; issue #5147), `waiting` (checks still running),
	`checks_failed`, `snapshot_invalid`, `run_unverified`, `review_active`
	(a newer review of the PR may still be running), `review_superseded`
	(a newer review of the PR did not succeed, or the marker changed while
	this ran), `auto_merge_setting_unreadable`, `auto_merge_disabled`, `ready` (dry run),
	`merge_enabled`, `merge_failed`, and, after the merge call (issue #5905):
	`base_changed` again when review_enable_auto_merge.sh refused a base that
	moved since the first read, `merge_revoked` (auto-merge was enabled, but
	the re-read PR no longer had the reviewed head and base, or could not be
	read, so it was disabled again), `merge_revoke_failed` (that disable
	failed; auto-merge may still be on), and `merged_unreviewed_base` (the PR
	had already merged with another head or base ref). Only `merge_enabled`,
	`merge_revoke_failed`, and `merged_unreviewed_base` leave a change behind.
	Raises `check_in_status.ReadError` when a read fails, and `OSError` when
	the snapshot's temp directory cannot be written; the sweep logs both per PR.
	"""
	pr = check_in_status.gh_api(f"repos/{repo}/pulls/{number}")
	head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
	head_sha = head.get("sha") if isinstance(head.get("sha"), str) else ""
	head_ref = head.get("ref") if isinstance(head.get("ref"), str) else ""
	base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
	base_ref = base.get("ref") if isinstance(base.get("ref"), str) else ""
	base_sha = base.get("sha") if isinstance(base.get("sha"), str) else ""
	blocking = [name for name in check_in_status._label_names(pr) if name in check_in_status.BLOCKING_LABELS]
	ineligible = None
	if pr.get("state") != "open" or pr.get("merged"):
		ineligible = "PR is not open"
	elif pr.get("draft"):
		ineligible = "PR is a draft"
	elif not head_ref.startswith(check_in_status.CLAUDE_BRANCH_PREFIX) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
		ineligible = "not a claude/* head"
	elif blocking:
		ineligible = f"blocked: {', '.join(blocking)}"
	elif pr.get("mergeable_state") == "dirty":
		ineligible = "merge conflict"
	elif pr.get("auto_merge"):
		ineligible = "auto-merge already enabled"
	if ineligible:
		return {"state": "not_eligible", "head_sha": head_sha, "reason": ineligible}
	marker = find_pending_marker(check_in_status.gh_api_list(f"repos/{repo}/issues/{number}/comments"), repo, head_sha, author_login)
	if marker is None:
		reason = "no trusted pending-checks marker for the current head" if author_login else "CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN is unset"
		return {"state": "no_marker", "head_sha": head_sha, "reason": reason}
	if marker["base_sha"] is None or marker["base_ref_sha256"] is None:
		return {"state": "base_unbound", "head_sha": head_sha,
			"reason": "the pending-checks marker is not bound to a reviewed base; the next review sweep reviews this head again"}
	if (not base_ref or not re.fullmatch(r"[0-9a-f]{40}", base_sha)
		or base_sha != marker["base_sha"] or base_ref_digest(base_ref) != marker["base_ref_sha256"]):
		return {"state": "base_changed", "head_sha": head_sha,
			"reason": f"the PR's base ({base_ref or 'unknown'} at {base_sha[:12] or 'unknown'}) is not the reviewed base "
				f"({marker['base_sha'][:12]}); the next review sweep reviews this head again"}
	snapshot = read_check_snapshot(repo, head_sha)
	if snapshot["state"] == "incomplete":
		return {"state": "waiting", "head_sha": head_sha, "reason": f"check runs still running: {snapshot['detail']}"}
	if snapshot["state"] == "failed":
		return {"state": "checks_failed", "head_sha": head_sha, "reason": f"failed check runs (left to the ci-failed hand-back): {snapshot['detail']}"}
	if snapshot["state"] != "ready":
		return {"state": "snapshot_invalid", "head_sha": head_sha, "reason": snapshot["detail"]}
	run_problem = verify_review_run(repo, marker, head_sha, head_ref,
		number=number, default_branch=check_in_status._pr_default_branch(pr))
	if run_problem:
		return {"state": "run_unverified", "head_sha": head_sha, "reason": run_problem}
	newer_review = check_review_runs(repo, number, head_ref, marker["run_id"])
	if newer_review is not None:
		return {"state": newer_review["state"], "head_sha": head_sha, "reason": newer_review["reason"]}
	# A newer review that completed after the first comment read has already
	# posted its hand-off or marker, so authorize only a marker that is still
	# the live one now that no newer review is running (issue #5148). A review
	# that starts after its listing was read, up to the merge call below, is
	# not seen: a window of seconds, while a review takes minutes to post
	# anything, accepted by the #5148 plan (Risks); closing it would need the
	# review workflow to hold off or disable auto-merge, which the plan leaves out.
	current = find_pending_marker(check_in_status.gh_api_list(f"repos/{repo}/issues/{number}/comments"), repo, head_sha, author_login)
	if current is None:
		return {"state": "review_superseded", "head_sha": head_sha,
			"reason": f"pending-checks comment {marker['comment_id']} is no longer the live marker for this head: "
				"a later hand-off superseded it, or it was deleted"}
	if current["comment_id"] != marker["comment_id"]:
		return {"state": "review_superseded", "head_sha": head_sha,
			"reason": f"pending-checks comment {marker['comment_id']} is no longer the live marker for this head: "
				f"newer pending-checks comment {current['comment_id']} replaced it"}
	enable_flag = read_enable_auto_merge(repo)
	if enable_flag is None:
		return {"state": "auto_merge_setting_unreadable", "head_sha": head_sha,
			"reason": "could not read the repository's ENABLE_AUTO_MERGE variable (GH_PAT needs Actions variables read)"}
	if enable_flag != "true":
		return {"state": "auto_merge_disabled", "head_sha": head_sha, "reason": f"ENABLE_AUTO_MERGE={enable_flag}"}
	if dry_run:
		return {"state": "ready", "head_sha": head_sha, "reason": f"{snapshot['detail']}; dry run, auto-merge not enabled"}
	result = enable_auto_merge(repo, number, head_sha, enable_flag, base_ref=base_ref, base_sha=base_sha)
	if result["base_refused"]:
		return {"state": "base_changed", "head_sha": head_sha,
			"reason": "the PR was retargeted before the merge call; review_enable_auto_merge.sh refused it "
				f"(reviewed base {base_ref} at {base_sha[:12]}); the next review sweep reviews this head again"}
	if not result["enabled"]:
		return {"state": "merge_failed", "head_sha": head_sha, "reason": result["output"][-500:]}
	# Issue #5905: the helper's PR read and its merge call are separate, so a
	# retarget in between still got auto-merge. Re-read once and take the
	# authorization back unless the reviewed head and base still hold.
	pair = recheck_reviewed_pair(repo, number, head_sha, base_ref, base_sha)
	if pair["outcome"] == "held":
		return {"state": "merge_enabled", "head_sha": head_sha, "reason": snapshot["detail"]}
	if pair["outcome"] == "merged_elsewhere":
		return {"state": "merged_unreviewed_base", "head_sha": head_sha,
			"reason": f"the PR merged as {pair['detail']}, not the reviewed base {base_ref} at {base_sha[:12]}"}
	revoke = revoke_auto_merge(repo, number)
	if not revoke["revoked"]:
		return {"state": "merge_revoke_failed", "head_sha": head_sha,
			"reason": f"{pair['detail']}; disabling auto-merge failed: {revoke['output'][-300:]}"}
	return {"state": "merge_revoked", "head_sha": head_sha,
		"reason": f"auto-merge disabled again: {pair['detail']} after the merge call, reviewed {head_sha[:12]} "
			f"into {base_ref} at {base_sha[:12]}; the next sweep or review run decides this head again"}


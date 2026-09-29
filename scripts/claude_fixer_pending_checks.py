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
  * the repository's ENABLE_AUTO_MERGE variable reads `true` (unset = `true`;
    an unreadable variable does not merge).
`review_enable_auto_merge.sh` keeps its own guards on top: `--match-head-commit`
on the reviewed head, the e2e-smoke-test label, and integration-branch heads.

A check that finishes failed is not handled here: `check_in_status.py
--hand-back` reports it as `ci-failed` like any other `claude/*` head, and a
push starts a new review round.

API budget (CLAUDE.md §15), per evaluated PR: 1 PR read and 1 read per 100
comments. The base binding reuses the PR read (no call). With a live marker
bound to the current base, 1 paginated check-runs read (one call per 100
check runs, through the collector). Only when the snapshot is ready: 1 review
run read (+1 compare read when that run was triggered by an older push), 1
repository-variable read, then `review_enable_auto_merge.sh` (1 paginated
labels read, 1 PR read, 1 merge call). Every read goes through `gh api` with
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
# The forms `gh api` (and the GitHub error body it prints to stdout) use for a
# 404; scripts/gh_helpers.sh recognises the same set.
NOT_FOUND_RE = re.compile(r"HTTP 404|gh: Not Found|404 Not Found|status code 404|\"status\":\s*\"404\"", re.IGNORECASE)


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


def enable_auto_merge(repo: str, number: int, head_sha: str, enable_flag: str) -> dict:
	"""Run review_enable_auto_merge.sh for the reviewed head; {"enabled": bool, "output": str}."""
	env = {
		**os.environ,
		"GITHUB_REPOSITORY": repo,
		"PR_NUMBER": str(number),
		"ENABLE_AUTO_MERGE": enable_flag,
		"FORWARD_MERGE_FALLBACK_AUTO_MERGE": DEFAULT_FORWARD_MERGE_FALLBACK_AUTO_MERGE,
		"ORCH_INTEGRATION_BRANCH_PATTERN": DEFAULT_ORCH_INTEGRATION_BRANCH_PATTERN,
		"INITIAL_HEAD_SHA": head_sha,
	}
	# The helper records AUTO_MERGE_READY_LABELS_ALLOWED for later workflow
	# steps; the sweep has none, so keep it out of the job environment.
	env.pop("GITHUB_ENV", None)
	try:
		proc = subprocess.run(["bash", str(AUTO_MERGE_SCRIPT)], env=env, capture_output=True, text=True,
			timeout=SUBPROCESS_TIMEOUT_SECS, check=False)
	except (OSError, subprocess.TimeoutExpired) as exc:
		return {"enabled": False, "output": f"review_enable_auto_merge.sh did not run: {exc}"}
	output = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
	enabled = proc.returncode == 0 and any(line.startswith(AUTO_MERGE_ENABLED_LINE_PREFIX) for line in proc.stdout.splitlines())
	return {"enabled": enabled, "output": output}


def evaluate(repo: str, number: int, *, author_login: str, dry_run: bool = False) -> dict:
	"""Decide (and, when ready, act) for one PR; see the module docstring.

	Output: {"state": ..., "reason": ...} plus `head_sha` once known. States:
	`not_eligible` (closed, draft, not claude/*, blocked, conflicted,
	auto-merge already on), `no_marker`, `base_unbound` (the marker has no
	v2 base binding), `base_changed` (the PR's base ref or base sha differs
	from the binding; issue #5147), `waiting` (checks still running),
	`checks_failed`, `snapshot_invalid`, `run_unverified`,
	`auto_merge_setting_unreadable`, `auto_merge_disabled`, `ready` (dry run),
	`merge_enabled`, `merge_failed`. Only `merge_enabled` changed anything.
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
	enable_flag = read_enable_auto_merge(repo)
	if enable_flag is None:
		return {"state": "auto_merge_setting_unreadable", "head_sha": head_sha,
			"reason": "could not read the repository's ENABLE_AUTO_MERGE variable (GH_PAT needs Actions variables read)"}
	if enable_flag != "true":
		return {"state": "auto_merge_disabled", "head_sha": head_sha, "reason": f"ENABLE_AUTO_MERGE={enable_flag}"}
	if dry_run:
		return {"state": "ready", "head_sha": head_sha, "reason": f"{snapshot['detail']}; dry run, auto-merge not enabled"}
	result = enable_auto_merge(repo, number, head_sha, enable_flag)
	return {"state": "merge_enabled" if result["enabled"] else "merge_failed", "head_sha": head_sha,
		"reason": snapshot["detail"] if result["enabled"] else result["output"][-500:]}


#!/usr/bin/env python3
"""Re-run a PR's cancelled `CI` run once per head, from the review sweep.

Issue #4713. `.claude/scripts/check_in_status.py` treats `failure`,
`timed_out`, `action_required` and `startup_failure` as failed checks, so a
`CI` run that ends `cancelled` (its `timeout-minutes`, a lost runner, a manual
cancel) is invisible to the CLAUDE.md §26 checker, the Claude fixer and the
§26.H catch-all sweep. The PR stays "unstable" until a human steps in (#4633
stalled that way on 2026-09-28). This helper is the recovery path.

Driven by the `sweep` job of `.github/workflows/review_autofix_sweep.yml`
(every 30 minutes, and on `workflow_dispatch`), in this repository only:
the workflow is internal and consumer repos do not share a CI workflow name.

Rules, for each open non-draft PR in the sweep's snapshot:
  - same-repository heads only (a fork head is never re-run), and the
    sweep's `[skip ai]` opt-out and `head_ref_filter` input are honoured;
  - take the `ci.yml` runs whose `head_sha` is the PR's current head, so a
    run for a superseded head is never matched;
  - skip when any of them is not `completed` (queued, in progress, or a
    re-run already under way);
  - take the newest (by `created_at`, then `id`); re-run it only when it
    concluded `cancelled` or `startup_failure` and has `run_attempt == 1`.
    A `failure` is never re-run: it stays with the Claude fixer.
  - the re-run is `POST …/runs/<id>/rerun-failed-jobs`; for
    `startup_failure` only, a refused call falls back once to
    `POST …/runs/<id>/rerun` (such runs often have no jobs to keep). Either
    way the run's attempt becomes 2, so a head is re-run at most once.

Batching contract (CLAUDE.md §15):
  input   the sweep's PR snapshot (`--prs`): a JSON array of objects with
          `number`, `head_ref`, `head_repo`, `head_sha`, `title`, `body`,
          written by the enumerate step from its existing PR listing;
  calls   0 when the switch is off, the snapshot is missing, or no PR passes
          the local filters; otherwise exactly 1 read,
          `GET repos/<repo>/actions/workflows/ci.yml/runs?per_page=100`
          (unfiltered, so it holds completed and active runs), matched
          locally against every PR head; plus 1 POST per re-run (2 when a
          `startup_failure` re-run falls back). Never one read per PR.
  output  one `CI_CANCELLED_RERUN pr=<n> head=<sha12> run=<id|none>
          action=<rerun|skip> reason=<reason>` line per decision and one
          `CI_CANCELLED_RERUN_END` summary line;
  failure fail open: a failed listing or re-run is a `::warning::` and the
          script exits 0, so the sweep job never fails because of it.

Switch: repo variable `CI_CANCELLED_AUTO_RERUN_ENABLED` (the workflow
defaults it to `true`). `1`, `true`, `yes`, `on` (any case) enable it;
anything else disables it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

CI_WORKFLOW_FILE = "ci.yml"
RERUN_CONCLUSIONS = ("cancelled", "startup_failure")
ENABLED_VALUES = ("1", "true", "yes", "on")
SKIP_AI_MARKER = "[skip ai]"


class ApiError(Exception):
	"""A GitHub REST call failed."""


def log(message: str) -> None:
	print(f"CI_CANCELLED_RERUN {message}", flush=True)


def warn(message: str) -> None:
	print(f"::warning::CI_CANCELLED_RERUN {message}", flush=True)


def is_enabled(value: str | None) -> bool:
	return (value or "").strip().lower() in ENABLED_VALUES


def _gh(args: list[str]) -> str:
	try:
		proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ApiError(f"gh {' '.join(args[:3])} failed: {exc}") from exc
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout).strip().splitlines()
		raise ApiError(f"gh {' '.join(args[:3])} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	return proc.stdout


def api_get(path: str) -> object:
	"""GET one REST path through `gh api` and return the decoded JSON."""
	output = _gh(["api", path])
	try:
		return json.loads(output)
	except ValueError as exc:
		raise ApiError(f"gh api {path} returned invalid JSON") from exc


def api_post(path: str) -> None:
	"""POST to one REST path through `gh api` (no body)."""
	_gh(["api", "-X", "POST", path])


def load_snapshot(path: str) -> list[dict] | None:
	"""The sweep's PR snapshot, or None when it is missing or malformed."""
	try:
		data = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None
	if not isinstance(data, list):
		return None
	return [row for row in data if isinstance(row, dict)]


def _short(sha: str) -> str:
	return (sha or "")[:12] or "none"


def _decision(summary: dict, number: object, head: str, run_id: object, action: str, reason: str) -> None:
	summary["rerun" if action == "rerun" else "skipped"] += 1
	log(f"pr={number} head={_short(head)} run={run_id if run_id is not None else 'none'} action={action} reason={reason}")


def rerun(repo: str, run: dict, post: Callable[[str], None]) -> str:
	"""Re-run one run's failed jobs; returns the endpoint used. Raises ApiError."""
	run_id = run["id"]
	try:
		post(f"repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs")
		return "rerun-failed-jobs"
	except ApiError:
		if run.get("conclusion") != "startup_failure":
			raise
	post(f"repos/{repo}/actions/runs/{run_id}/rerun")
	return "rerun"


def process(repo: str, prs: list[dict] | None, *, enabled: bool, dry_run: bool = False, head_ref_filter: str = "",
	get: Callable[[str], object] | None = None, post: Callable[[str], None] | None = None) -> dict:
	"""Decide and act for every PR in the snapshot; returns the summary counters."""
	get = get or api_get
	post = post or api_post
	summary = {"candidates": 0, "rerun": 0, "skipped": 0, "errors": 0}
	if not enabled:
		log("pr=none head=none run=none action=skip reason=disabled")
		return summary
	if prs is None:
		warn("pr=none head=none run=none action=skip reason=no_pr_snapshot")
		return summary
	eligible: list[dict] = []
	for pr in prs:
		number = pr.get("number")
		head = str(pr.get("head_sha") or "")
		summary["candidates"] += 1
		if head_ref_filter and head_ref_filter not in str(pr.get("head_ref") or ""):
			_decision(summary, number, head, None, "skip", "head_ref_filter")
		elif SKIP_AI_MARKER in f"{pr.get('title') or ''} {pr.get('body') or ''}":
			_decision(summary, number, head, None, "skip", "skip_ai_marker")
		elif str(pr.get("head_repo") or "").lower() != repo.lower():
			_decision(summary, number, head, None, "skip", "fork_head")
		elif not head:
			_decision(summary, number, head, None, "skip", "no_head_sha")
		else:
			eligible.append(pr)
	if not eligible:
		return summary
	try:
		payload = get(f"repos/{repo}/actions/workflows/{CI_WORKFLOW_FILE}/runs?per_page=100")
		runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
		if not isinstance(runs, list):
			raise ApiError("runs listing returned no workflow_runs array")
	except ApiError as exc:
		summary["errors"] += 1
		warn(f"pr=none head=none run=none action=skip reason=runs_list_failed error={json.dumps(str(exc))}")
		return summary
	by_head: dict[str, list[dict]] = {}
	for run in runs:
		if isinstance(run, dict) and run.get("head_sha") and isinstance(run.get("id"), int):
			by_head.setdefault(run["head_sha"], []).append(run)
	for pr in eligible:
		number = pr.get("number")
		head = str(pr["head_sha"])
		head_runs = by_head.get(head, [])
		if not head_runs:
			_decision(summary, number, head, None, "skip", "no_ci_run")
			continue
		active = [run for run in head_runs if run.get("status") != "completed"]
		if active:
			_decision(summary, number, head, active[0]["id"], "skip", "active_run")
			continue
		newest = max(head_runs, key=lambda run: (str(run.get("created_at") or ""), run["id"]))
		conclusion = str(newest.get("conclusion") or "none")
		if conclusion not in RERUN_CONCLUSIONS:
			_decision(summary, number, head, newest["id"], "skip", f"conclusion_{conclusion}")
			continue
		if newest.get("run_attempt") != 1:
			_decision(summary, number, head, newest["id"], "skip", f"already_rerun attempt={newest.get('run_attempt')}")
			continue
		if dry_run:
			_decision(summary, number, head, newest["id"], "skip", f"dry_run would_rerun={conclusion}")
			continue
		try:
			endpoint = rerun(repo, newest, post)
		except ApiError as exc:
			summary["errors"] += 1
			summary["skipped"] += 1
			warn(f"pr={number} head={_short(head)} run={newest['id']} action=skip reason=rerun_failed error={json.dumps(str(exc))}")
			continue
		_decision(summary, number, head, newest["id"], "rerun", f"{conclusion} endpoint={endpoint}")
	return summary


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--repo", default=os.environ.get("REPOSITORY") or os.environ.get("GITHUB_REPOSITORY", ""))
	parser.add_argument("--prs", required=True, help="the sweep's PR snapshot (JSON array)")
	parser.add_argument("--head-ref-filter", default=os.environ.get("HEAD_REF_FILTER", ""))
	parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN", "") == "true")
	args = parser.parse_args(argv)
	if not args.repo:
		parser.error("--repo (or REPOSITORY / GITHUB_REPOSITORY) is required")
	summary = process(
		args.repo,
		load_snapshot(args.prs),
		enabled=is_enabled(os.environ.get("CI_CANCELLED_AUTO_RERUN_ENABLED", "true")),
		dry_run=args.dry_run,
		head_ref_filter=args.head_ref_filter,
	)
	print("CI_CANCELLED_RERUN_END " + " ".join(f"{key}={value}" for key, value in summary.items()), flush=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())

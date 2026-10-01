#!/usr/bin/env python3
"""Catch-all sweep: queue a fresh Claude fixer for claude/* PRs nobody handled.

CLAUDE.md §26.H. Every PR-backed `claude/*` head runs in Claude-fixer mode
(review_autofix.yml), so the GPT editor and resolver never touch it and a
Claude session must fix its conflicts, failed checks, review hand-offs, and
blocks. Normally the §26 checker hands those back to the session that pushed
the PR, and `/implement-plan-claude` stages handle their own PRs. This sweep
is the second line of defence for every open `claude/*` PR in this repo and
in every registered consumer repo (`.github/ai/consumer_repos.json`): a fix
that has been due for at least --min-age-hours (default 2) with no live claim
is queued as one `ai:claude-issue-queue` item (payload `claude_pr_fix.v1`,
`scripts/claude_issue_route.py`), and the Claude issue pickup session
(`.claude/commands/claude-issue-pickup.md`) starts one Opus 5.5 session at
high effort running `/fix-claude-pr <PR url>`. A claude.ai routine run cannot
start sessions (issue #4525), so the queue and the pickup are the only way in.

Driven by the `claude-pr-catch-all` job of
`.github/workflows/review_autofix_sweep.yml` (hourly). The decision is
`.claude/scripts/check_in_status.py --hand-back` (`check_pr_hand_back`), the
same verdict the §26 checker acts on, so the checker and the sweep never
disagree about what is due. `/implement-plan-claude` heads keep their 6-hour
stuck window for failed checks.

Batching contract (CLAUDE.md §15):
  input   the registry file plus this repository;
  calls   one read of this repo's open queue per run; per repo, one open-PR
          list call per 100 PRs; per `claude/*` candidate, the
          check_in_status.py hand-back reads (1 PR read, 1 per 100 comments,
          check-run pages, at most 1 commit read, 1 hand-off run read,
          1 compare read when that run was triggered by an older push than
          the head it reviewed, and 3 active-run reads); per queued fixer,
          1 queue-issue POST and the claim (1 PR read + 1 comment POST);
          the queue binding is a local file write (no call);
          per `open` candidate (not claimed, held, or due), the pending-checks
          reads of scripts/claude_fixer_pending_checks.py (1 PR read, 1 per
          100 comments, and only with a live marker the check-run pages,
          then only when ready 1 run read (+1 compare read when that run
          was triggered by an older push), 1 head-branch runs listing and 4
          workflow_dispatch runs listings, each 1 call per 100 runs down to
          the marker's run (usually 1, at most 10), for a listing whose
          last page read was full 1 status-filtered listing for each of
          the 5 run statuses but `completed` (usually 1 call, at most 10),
          and 1 per 100 comments
          again (issue #5148), 1 variable read and the auto-merge helper's
          calls, then after a successful merge call 1 PR re-read and, only
          when the reviewed head or base moved, 1 auto-merge disable,
          issue #5905);
  output  one `CLAUDE_PR_SWEEP` log line per decision plus a summary line;
  failure fail open per PR and per repo: a read error is logged and the
          sweep moves on; nothing is retried in a tight loop. A failed queue
          read turns the run into a report, so it never queues a duplicate.

Queue issues are opened with the workflow's GITHUB_TOKEN
(`CLAUDE_PR_SWEEP_QUEUE_TOKEN`), whose `github-actions[bot]` identity is the
only author the pickup trusts; reads and claims use `GH_TOKEN` (GH_PAT).
Without the queue token the sweep only reports: each due PR is logged as
`::warning::` and nothing is queued.

The sweep also finishes clean Claude-fixer reviews that ended before CI did
(issue #4900): for every `open` candidate it calls
`scripts/claude_fixer_pending_checks.py`, which enables head-bound
auto-merge once the head's check runs behind a trusted
`ai:claude-fixer-pending-checks:v1` marker, bound by its v2 line to the PR's
current base (issue #5147), have all completed without a failure. The
merge helper re-checks that base right before its merge call, and a PR
retargeted around that call has its auto-merge disabled again (issue #5905;
`merge_revoke_failed` and `merged_unreviewed_base` are also logged as
`::warning::`). `--dry-run` only logs what it would enable.

Each queue issue it opens is also recorded (number, title, payload) in this
run's binding file (`CLAUDE_PR_SWEEP_QUEUE_BINDING_FILE`, default
`<RUNNER_TEMP or the temp dir>/claude-issue-queue-binding/claude_issue_queue_binding.json`),
which the workflow uploads as the `claude-issue-queue-binding` artifact. The
pickup starts nothing that artifact does not list unchanged, so an edited
queue issue is refused (issue #4621). The "already queued" dedupe still
counts every open trusted item, bound or not, so a PR is never queued twice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = ROOT / ".claude" / "scripts"


def _load(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


check_in_status = _load("check_in_status", _SCRIPTS / "check_in_status.py")
claude_fix_claim = _load("claude_fix_claim", _SCRIPTS / "claude_fix_claim.py")
claude_issue_route = _load("claude_issue_route", ROOT / "scripts" / "claude_issue_route.py")
claude_fixer_pending_checks = _load("claude_fixer_pending_checks", ROOT / "scripts" / "claude_fixer_pending_checks.py")

DUE_STATES = ("conflict", "review-round", "ci-failed", "blocked")
# claude_fixer_pending_checks.evaluate states that leave a merge authorization
# for an unreviewed base behind (issue #5905); each gets a ::warning:: line.
PENDING_CHECKS_ALARM_STATES = ("merge_revoke_failed", "merged_unreviewed_base")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class QueueError(Exception):
	"""A queue read or write failed."""


def log(message: str) -> None:
	print(f"CLAUDE_PR_SWEEP {message}", flush=True)


def load_repos(registry_path: Path, self_repo: str) -> list[str]:
	"""This repository first, then every registered consumer, deduplicated."""
	repos: list[str] = []
	if REPO_RE.fullmatch(self_repo or ""):
		repos.append(self_repo)
	try:
		data = json.loads(registry_path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		data = []
	if isinstance(data, list):
		repos.extend(slug for slug in data if isinstance(slug, str) and REPO_RE.fullmatch(slug))
	seen: set[str] = set()
	ordered: list[str] = []
	for slug in repos:
		if slug.lower() not in seen:
			seen.add(slug.lower())
			ordered.append(slug)
	return ordered


def list_candidates(repo: str) -> list[dict]:
	"""Open, non-draft, same-repository `claude/*` PRs without a `[skip ai]` marker."""
	candidates = []
	for pr in check_in_status.gh_api_list(f"repos/{repo}/pulls?state=open"):
		head = pr.get("head") or {}
		head_repo = (head.get("repo") or {}).get("full_name") or ""
		ref = head.get("ref") or ""
		if pr.get("draft") or head_repo.lower() != repo.lower() or not ref.startswith(check_in_status.CLAUDE_BRANCH_PREFIX):
			continue
		if "[skip ai]" in f"{pr.get('title') or ''} {pr.get('body') or ''}":
			log(f"skip repo={repo} pr=#{pr.get('number')} reason=skip_ai_marker")
			continue
		if isinstance(pr.get("number"), int):
			candidates.append({"number": pr["number"], "head_ref": ref})
	return candidates


def _gh_as(token: str, args: list[str]) -> str:
	"""Run `gh` with GH_TOKEN set to `token` (the queue identity); raise QueueError on failure."""
	env = {**os.environ, "GH_TOKEN": token}
	env.pop("GITHUB_TOKEN", None)
	try:
		proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120, env=env)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise QueueError(f"gh {args[0]} failed: {exc}") from exc
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout).strip().splitlines()
		raise QueueError(f"gh {' '.join(args[:3])} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	return proc.stdout


def queued_pr_fixes(self_repo: str, token: str, allowed: list[str]) -> set[tuple[str, int]]:
	"""(repo, pr) pairs that already have an open, trusted pr_fix queue item (one REST read)."""
	raw = _gh_as(token, ["api", f"repos/{self_repo}/issues?labels={claude_issue_route.QUEUE_LABEL}&state=open&per_page=100"])
	try:
		issues = json.loads(raw)
	except ValueError as exc:
		raise QueueError("queue read returned invalid JSON") from exc
	if not isinstance(issues, list):
		raise QueueError("queue read returned a non-array")
	pending = claude_issue_route.queue_pending(issues, allowed, limit=10**6)["pending"]
	return {(entry["repo"].lower(), entry["pr_number"]) for entry in pending if entry.get("item_type") == "pr_fix"}


def queue_pr_fix(self_repo: str, token: str, repo: str, number: int, head: str, kind: str, claim: str, run_url: str) -> int | None:
	"""Open one queue issue for a PR fix as the queue identity; return its number."""
	item = claude_issue_route.build_pr_fix_queue_issue(repo, number, head, kind, claim, run_url)
	with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as payload_file:
		json.dump({"title": item["title"], "body": item["body"], "labels": [item["label"]]}, payload_file)
		payload_path = payload_file.name
	try:
		output = _gh_as(token, ["api", "-X", "POST", f"repos/{self_repo}/issues", "--input", payload_path])
	finally:
		Path(payload_path).unlink(missing_ok=True)
	try:
		created = json.loads(output)
	except ValueError:
		return None
	return created.get("number") if isinstance(created, dict) else None


def bind_pr_fix(binding_file: str, self_repo: str, run_id: str, queue_number: int | None, repo: str, number: int,
	head: str, kind: str, claim: str, run_url: str) -> None:
	"""Record one queued PR fix in this run's binding file (issue #4621); log, never raise."""
	if not binding_file:
		return
	if not isinstance(queue_number, int) or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id or ""):
		print(f"::warning::CLAUDE_PR_SWEEP binding_skipped repo={repo} pr=#{number} queue_issue={queue_number} "
			"reason=no_queue_number_or_run_id (the pickup refuses unbound items)")
		return
	try:
		item = claude_issue_route.build_pr_fix_queue_issue(repo, number, head, kind, claim, run_url)
		claude_issue_route.append_queue_binding(
			binding_file, self_repo, run_id, queue_number, item["title"], claude_issue_route.queue_payload_text(item["body"]),
		)
	except (OSError, ValueError) as exc:
		print(f"::warning::CLAUDE_PR_SWEEP binding_failed repo={repo} pr=#{number} queue_issue={queue_number} error={exc} "
			"(the pickup refuses unbound items)")
		return
	log(f"bound repo={repo} pr=#{number} queue_issue={queue_number} run_id={run_id}")


def sweep(repos: list[str], now: dt.datetime, *, min_age_hours: float, dry_run: bool, self_repo: str, queue_token: str,
	run_id: str, run_url: str = "", allowed: list[str] | None = None, queue=queue_pr_fix, queued=queued_pr_fixes,
	binding_file: str = "", pending_checks=None) -> dict:
	"""Decide and act for every candidate PR; returns the summary counters.

	`pending_checks(repo, number, dry_run)` finishes a clean review that ended
	before CI (issue #4900) for each `open` candidate; `main` passes
	`evaluate_pending_checks`, and None skips that pass.
	"""
	claimant = f"sweep-run-{run_id}" if re.fullmatch(r"[0-9]{1,20}", run_id or "") else "sweep-run-local"
	summary = {"repos": 0, "candidates": 0, "due": 0, "queued": 0, "already_queued": 0, "reported": 0, "skipped": 0, "errors": 0,
		"pending_checks_merged": 0, "pending_checks_waiting": 0}
	allowed = allowed if allowed is not None else repos
	configured = bool(queue_token) and bool(REPO_RE.fullmatch(self_repo or ""))
	already: set[tuple[str, int]] = set()
	if configured and not dry_run:
		try:
			already = queued(self_repo, queue_token, allowed)
		except QueueError as exc:
			summary["errors"] += 1
			configured = False
			print(f"::warning::CLAUDE_PR_SWEEP queue_read_failed error={exc} (reporting only this run, so nothing is queued twice)")
	for repo in repos:
		summary["repos"] += 1
		try:
			candidates = list_candidates(repo)
		except (check_in_status.ReadError, KeyError, TypeError, ValueError) as exc:
			summary["errors"] += 1
			print(f"::warning::CLAUDE_PR_SWEEP list_failed repo={repo} error={exc}")
			continue
		for candidate in candidates:
			number = candidate["number"]
			summary["candidates"] += 1
			try:
				verdict = check_in_status.check_pr_hand_back(repo, number, check_in_status.DEFAULT_STUCK_HOURS, min_age_hours, now)
			except (check_in_status.ReadError, KeyError, TypeError, ValueError) as exc:
				summary["errors"] += 1
				print(f"::warning::CLAUDE_PR_SWEEP read_failed repo={repo} pr=#{number} error={exc}")
				continue
			state = verdict.get("state")
			if not verdict.get("done") or state not in DUE_STATES:
				summary["skipped"] += 1
				log(f"skip repo={repo} pr=#{number} state={state} reason={json.dumps(verdict.get('reason', ''))}")
				if pending_checks is not None and state == "open":
					# Any exception: this pass is an add-on to the catch-all
					# above, so nothing it raises (a failed read, a malformed
					# payload, an unwritable snapshot temp directory, or a bug
					# in the pass itself) may end the sweep for every later PR
					# and repo and stop due fixers from being queued there.
					try:
						pending = pending_checks(repo, number, dry_run)
					except Exception as exc:
						summary["errors"] += 1
						print(f"::warning::CLAUDE_PR_SWEEP pending_checks_failed repo={repo} pr=#{number} error={exc}")
						continue
					if pending.get("state") == "merge_enabled":
						summary["pending_checks_merged"] += 1
					elif pending.get("state") == "waiting":
						summary["pending_checks_waiting"] += 1
					if pending.get("state") not in ("not_eligible", "no_marker"):
						log(f"pending_checks repo={repo} pr=#{number} state={pending.get('state')} "
							f"head={str(pending.get('head_sha') or '')[:12]} reason={json.dumps(pending.get('reason', ''))}")
					if pending.get("state") in PENDING_CHECKS_ALARM_STATES:
						# Issue #5905: auto-merge may still be on for a base
						# nobody reviewed, or the PR already merged into one.
						print(f"::warning::CLAUDE_PR_SWEEP pending_checks_{pending.get('state')} repo={repo} pr=#{number} "
							f"head={str(pending.get('head_sha') or '')[:12]} reason={json.dumps(pending.get('reason', ''))}")
				continue
			summary["due"] += 1
			kind = verdict["kind"]
			head = verdict["head_sha"]
			url = f"https://github.com/{repo}/pull/{number}"
			if dry_run:
				log(f"dry_run repo={repo} pr=#{number} state={state} kind={kind} head={head[:12]}")
				continue
			if not configured:
				summary["reported"] += 1
				print(f"::warning::CLAUDE_PR_SWEEP report_only repo={repo} pr=#{number} state={state} kind={kind} "
					f"url={url} — no queue token (CLAUDE_PR_SWEEP_QUEUE_TOKEN), so no fixer session was queued.")
				continue
			if (repo.lower(), number) in already:
				summary["already_queued"] += 1
				log(f"already_queued repo={repo} pr=#{number} kind={kind}")
				continue
			try:
				queue_number = queue(self_repo, queue_token, repo, number, head, kind, claimant, run_url)
			except (QueueError, ValueError) as exc:
				summary["errors"] += 1
				print(f"::warning::CLAUDE_PR_SWEEP queue_failed repo={repo} pr=#{number} kind={kind} error={exc}")
				continue
			summary["queued"] += 1
			already.add((repo.lower(), number))
			log(f"queued repo={repo} pr=#{number} state={state} kind={kind} head={head[:12]} queue_issue={queue_number}")
			bind_pr_fix(binding_file, self_repo, run_id, queue_number, repo, number, head, kind, claimant, run_url)
			try:
				_, result = claude_fix_claim.post_claim(repo, number, head, kind, claimant)
				log(f"claim repo={repo} pr=#{number} posted={result.get('posted')} reason={json.dumps(result.get('reason', ''))}")
			except (check_in_status.ReadError, KeyError, TypeError, ValueError) as exc:
				print(f"::warning::CLAUDE_PR_SWEEP claim_failed repo={repo} pr=#{number} error={exc} (the fixer session claims the head itself)")
	return summary


def evaluate_pending_checks(repo: str, number: int, dry_run: bool) -> dict:
	"""The sweep's pending-checks pass for one PR (scripts/claude_fixer_pending_checks.py).

	Trusts only the review workflow's comment account,
	CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN (the workflow step resolves it from
	GH_PAT when the repo variable is empty); unset, nothing is trusted.
	"""
	return claude_fixer_pending_checks.evaluate(repo, number,
		author_login=os.environ.get("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "").strip(), dry_run=dry_run)


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--registry", default=os.environ.get("CLAUDE_PR_SWEEP_REGISTRY", ".github/ai/consumer_repos.json"))
	parser.add_argument("--self-repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
	parser.add_argument("--min-age-hours", type=float, default=float(os.environ.get("CLAUDE_PR_SWEEP_MIN_AGE_HOURS", "") or 2))
	parser.add_argument("--dry-run", action="store_true", default=os.environ.get("CLAUDE_PR_SWEEP_DRY_RUN", "") == "true")
	args = parser.parse_args(argv)
	repos = load_repos(Path(args.registry), args.self_repo)
	now = now or dt.datetime.now(dt.timezone.utc)
	run_id = os.environ.get("GITHUB_RUN_ID", "")
	run_url = f"https://github.com/{args.self_repo}/actions/runs/{run_id}" if run_id and args.self_repo else ""
	log(f"start repos={len(repos)} min_age_hours={args.min_age_hours:g} dry_run={str(args.dry_run).lower()}")
	summary = sweep(
		repos, now,
		min_age_hours=args.min_age_hours,
		dry_run=args.dry_run,
		self_repo=args.self_repo,
		queue_token=os.environ.get("CLAUDE_PR_SWEEP_QUEUE_TOKEN", ""),
		run_id=run_id,
		run_url=run_url,
		allowed=claude_issue_route.load_allowed_repos(Path(args.registry), args.self_repo),
		binding_file=os.environ.get("CLAUDE_PR_SWEEP_QUEUE_BINDING_FILE", "") or str(
			Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "claude-issue-queue-binding" / claude_issue_route.QUEUE_BINDING_FILENAME
		),
		pending_checks=evaluate_pending_checks,
	)
	log("end " + " ".join(f"{key}={value}" for key, value in summary.items()))
	return 0


if __name__ == "__main__":
	sys.exit(main())

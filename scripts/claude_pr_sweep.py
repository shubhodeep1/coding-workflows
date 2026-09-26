#!/usr/bin/env python3
"""Catch-all sweep: start a fresh Claude fixer for claude/* PRs nobody handled.

CLAUDE.md §26.H. Every PR-backed `claude/*` head runs in Claude-fixer mode
(review_autofix.yml), so the GPT editor and resolver never touch it and a
Claude session must fix its conflicts, failed checks, review hand-offs, and
blocks. Normally the §26 checker hands those back to the session that pushed
the PR, and `/implement-plan-claude` stages handle their own PRs. This sweep
is the second line of defence for every open `claude/*` PR in this repo and
in every registered consumer repo (`.github/ai/consumer_repos.json`): a fix
that has been due for at least --min-age-hours (default 2) with no live claim
starts one fresh Opus 5.5 session, at high effort, through the "Claude issue
dispatcher" routine (`.claude/commands/claude-issue-dispatch.md`, payload
`claude_pr_fix.v1`), which runs `/fix-claude-pr <PR url>`.

Driven by the `claude-pr-catch-all` job of
`.github/workflows/review_autofix_sweep.yml` (hourly). The decision is
`.claude/scripts/check_in_status.py --hand-back` (`check_pr_hand_back`), the
same verdict the §26 checker acts on, so the checker and the sweep never
disagree about what is due. `/implement-plan-claude` heads keep their 6-hour
stuck window for failed checks.

Batching contract (CLAUDE.md §15):
  input   the registry file plus this repository;
  calls   per repo, one open-PR list call per 100 PRs; per `claude/*`
          candidate, the check_in_status.py hand-back reads (1 PR read, 1 per
          100 comments, check-run pages, at most 1 commit read, 1 hand-off run
          read and 3 active-run reads); per started fixer, 1 routine fire and
          the claim (1 PR read + 1 comment POST);
  output  one `CLAUDE_PR_SWEEP` log line per decision plus a summary line;
  failure fail open per PR and per repo: a read error is logged and the
          sweep moves on; nothing is retried in a tight loop.

A missing routine id or token turns the sweep into a report: each due PR is
logged as `::warning::` and nothing is started. The routine token is only read
from the environment and sent in the Authorization header; it is never
printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = ROOT / ".claude" / "scripts"


def _load(name: str):
	spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


check_in_status = _load("check_in_status")
claude_fix_claim = _load("claude_fix_claim")

SCHEMA_VERSION = "claude_pr_fix.v1"
DUE_STATES = ("conflict", "review-round", "ci-failed", "blocked")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT_FIRE_URL_BASE = "https://api.anthropic.com/v1/claude_code/routines"
DEFAULT_ROUTINE_BETA = "experimental-cc-routine-2026-04-01"
DEFAULT_RETRY_DELAYS = (2, 4, 8, 16)


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


def build_fire_text(repo: str, number: int, head: str, kind: str, claim: str) -> str:
	"""The routine fire text: fixed keys only, no PR prose (the dispatcher parses it strictly).

	`claim` is the claimant this sweep reserves the head as; the fixer
	session looks past it (`check_in_status.py --ignore-claim-by`).
	"""
	return "\n".join([
		SCHEMA_VERSION,
		f"repo: {repo}",
		f"pr: {number}",
		f"url: https://github.com/{repo}/pull/{number}",
		f"head: {head}",
		f"kind: {kind}",
		f"claim: {claim}",
	]) + "\n"


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


def fire_routine(routine_id: str, token: str, text: str, *, beta: str, base_url: str,
	delays: tuple[int, ...] = DEFAULT_RETRY_DELAYS, opener=urllib.request.urlopen, sleep=time.sleep) -> tuple[bool, str, str]:
	"""POST the routine's /fire endpoint; retry 408 / 429 / 5xx / network errors.

	Returns (ok, detail, session_url). The token only goes into the header.
	"""
	request_body = json.dumps({"text": text}).encode("utf-8")
	detail = ""
	for attempt, delay in enumerate((0, *delays), 1):
		if delay:
			sleep(delay)
		request = urllib.request.Request(f"{base_url}/{routine_id}/fire", data=request_body, method="POST", headers={
			"Authorization": f"Bearer {token}",
			"anthropic-beta": beta,
			"anthropic-version": "2023-06-01",
			"Content-Type": "application/json",
		})
		try:
			with opener(request, timeout=60) as response:
				payload = response.read().decode("utf-8", "replace")
			try:
				session_url = json.loads(payload).get("claude_code_session_url") or ""
			except (ValueError, AttributeError):
				session_url = ""
			return True, f"HTTP 2xx after {attempt} attempt(s)", session_url
		except urllib.error.HTTPError as exc:
			detail = f"HTTP {exc.code} after {attempt} attempt(s)"
			if exc.code not in (408, 429) and exc.code < 500:
				return False, detail, ""
		except (urllib.error.URLError, OSError) as exc:
			detail = f"network error after {attempt} attempt(s): {type(exc).__name__}"
	return False, detail, ""


def sweep(repos: list[str], now: dt.datetime, *, min_age_hours: float, dry_run: bool, routine_id: str, token: str,
	run_id: str, beta: str = DEFAULT_ROUTINE_BETA, base_url: str = DEFAULT_FIRE_URL_BASE, fire=fire_routine) -> dict:
	"""Decide and act for every candidate PR; returns the summary counters."""
	configured = bool(re.fullmatch(r"trig_[A-Za-z0-9]+", routine_id or "")) and bool(token)
	claimant = f"sweep-run-{run_id}" if re.fullmatch(r"[0-9]{1,20}", run_id or "") else "sweep-run-local"
	summary = {"repos": 0, "candidates": 0, "due": 0, "started": 0, "reported": 0, "skipped": 0, "errors": 0}
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
					f"url={url} — CLAUDE_ISSUE_ROUTINE_ID / CLAUDE_ISSUE_ROUTINE_TOKEN are not configured, so no fixer session was started.")
				continue
			ok, detail, session_url = fire(routine_id, token, build_fire_text(repo, number, head, kind, claimant), beta=beta, base_url=base_url)
			if not ok:
				summary["errors"] += 1
				print(f"::warning::CLAUDE_PR_SWEEP fire_failed repo={repo} pr=#{number} kind={kind} detail={detail}")
				continue
			summary["started"] += 1
			log(f"started repo={repo} pr=#{number} state={state} kind={kind} head={head[:12]} session={session_url or 'unknown'}")
			try:
				_, result = claude_fix_claim.post_claim(repo, number, head, kind, claimant)
				log(f"claim repo={repo} pr=#{number} posted={result.get('posted')} reason={json.dumps(result.get('reason', ''))}")
			except (check_in_status.ReadError, KeyError, TypeError, ValueError) as exc:
				print(f"::warning::CLAUDE_PR_SWEEP claim_failed repo={repo} pr=#{number} error={exc} (the fixer session claims the head itself)")
	return summary


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--registry", default=os.environ.get("CLAUDE_PR_SWEEP_REGISTRY", ".github/ai/consumer_repos.json"))
	parser.add_argument("--self-repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
	parser.add_argument("--min-age-hours", type=float, default=float(os.environ.get("CLAUDE_PR_SWEEP_MIN_AGE_HOURS", "") or 2))
	parser.add_argument("--dry-run", action="store_true", default=os.environ.get("CLAUDE_PR_SWEEP_DRY_RUN", "") == "true")
	args = parser.parse_args(argv)
	repos = load_repos(Path(args.registry), args.self_repo)
	now = now or dt.datetime.now(dt.timezone.utc)
	log(f"start repos={len(repos)} min_age_hours={args.min_age_hours:g} dry_run={str(args.dry_run).lower()}")
	summary = sweep(
		repos, now,
		min_age_hours=args.min_age_hours,
		dry_run=args.dry_run,
		routine_id=os.environ.get("CLAUDE_ISSUE_ROUTINE_ID", ""),
		token=os.environ.get("CLAUDE_ISSUE_ROUTINE_TOKEN", ""),
		run_id=os.environ.get("GITHUB_RUN_ID", ""),
		beta=os.environ.get("CLAUDE_ISSUE_ROUTINE_BETA", "") or DEFAULT_ROUTINE_BETA,
		base_url=os.environ.get("CLAUDE_ISSUE_FIRE_URL_BASE", "") or DEFAULT_FIRE_URL_BASE,
	)
	log("end " + " ".join(f"{key}={value}" for key, value in summary.items()))
	return 0


if __name__ == "__main__":
	sys.exit(main())

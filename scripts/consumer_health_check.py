#!/usr/bin/env python3
"""Daily consumer health check: decide what to file, close and re-enable.

`scripts/consumer_health_check.sh` fetches the data once per UTC day and runs
`evaluate`; this module only decides. It makes no API calls and writes nothing.

Input (`--snapshot-dir`), one directory per consumer named ``owner__repo``:
  workflows.json   GET repos/<repo>/actions/workflows?per_page=100 (object)
  poll_runs.json   GET .../workflows/<poll id>/runs?per_page=1 (object; only
                   when the poll wrapper is active)
  tracking.json    GET repos/<repo>/issues?state=open&labels=ai:orchestrator-tracking
                   &per_page=100 (array)
  error            present when a read failed; its text is the reason token
Plus `--open-issues-json`: this repository's open `ai:consumer-health` issues.

Output (stdout, one JSON object):
  problems   [{repo, problem, detail}] found this run
  enable     [{repo, workflow_id, path}] wrappers GitHub disabled for inactivity
  open       problems with no open issue yet (file one each)
  close      [{number, repo, problem}] open issues whose problem cleared
  repos      number of consumers checked

Problem keys (one issue per consumer and key):
  unreadable               a read failed (token cannot see the repo, API error)
  workflows_disabled       a wrapper is disabled by a person or token, or in any
                           state other than active / disabled_inactivity
  poller_silent            the active poll wrapper started no run in the window
  tracking_issue_comments  an open tracking issue reached the comment limit
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ISSUE_LABEL = "ai:consumer-health"
MARKER_RE = re.compile(r"<!-- ai:consumer-health:v1 repo=([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) problem=([a-z_]+) -->")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
POLL_WRAPPER = "ai-orchestrate-poll.yml"
PROBLEMS = ("unreadable", "workflows_disabled", "poller_silent", "tracking_issue_comments")


def marker(repo: str, problem: str) -> str:
	return f"<!-- ai:consumer-health:v1 repo={repo} problem={problem} -->"


def snapshot_dir_name(repo: str) -> str:
	return repo.replace("/", "__")


def _parse_time(value: str) -> datetime | None:
	try:
		return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
	except (TypeError, ValueError):
		return None


def _read_json(path: Path):
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None


def check_repo(repo: str, snap: Path, *, templates: set[str], now: datetime, silent_hours: int, comment_limit: int) -> tuple[list[dict], list[dict]]:
	"""Return (problems, enable) for one consumer from its snapshot directory."""
	error = snap / "error"
	if error.exists() or not snap.is_dir():
		reason = error.read_text(encoding="utf-8").strip() if error.exists() else "snapshot_missing"
		return [{"repo": repo, "problem": "unreadable", "detail": re.sub(r"[^a-z0-9_]", "_", reason.lower())[:60] or "unknown"}], []
	workflows = _read_json(snap / "workflows.json")
	if not isinstance(workflows, dict) or not isinstance(workflows.get("workflows"), list):
		return [{"repo": repo, "problem": "unreadable", "detail": "workflows_unparsable"}], []
	problems: list[dict] = []
	enable: list[dict] = []
	disabled: list[str] = []
	poll_active = False
	for wf in workflows["workflows"]:
		if not isinstance(wf, dict):
			continue
		path = str(wf.get("path") or "")
		name = path.rsplit("/", 1)[-1]
		if not path.startswith(".github/workflows/") or name not in templates:
			continue
		state = str(wf.get("state") or "")
		if state == "active":
			poll_active = poll_active or name == POLL_WRAPPER
		elif state == "disabled_inactivity" and isinstance(wf.get("id"), int):
			enable.append({"repo": repo, "workflow_id": wf["id"], "path": path})
		else:
			disabled.append(f"{name} ({state or 'unknown'})")
	if disabled:
		problems.append({"repo": repo, "problem": "workflows_disabled", "detail": ", ".join(sorted(disabled))})
	if poll_active:
		runs = _read_json(snap / "poll_runs.json")
		newest = None
		if isinstance(runs, dict) and isinstance(runs.get("workflow_runs"), list) and runs["workflow_runs"]:
			# The start time, as in build_state_snapshot.py: a run queued for hours
			# has a fresh created_at but has not started.
			run0 = runs["workflow_runs"][0] if isinstance(runs["workflow_runs"][0], dict) else {}
			newest = _parse_time(str(run0.get("run_started_at") or run0.get("created_at") or ""))
		if not isinstance(runs, dict):
			problems.append({"repo": repo, "problem": "unreadable", "detail": "poll_runs_unparsable"})
		elif newest is None or now - newest > timedelta(hours=silent_hours):
			last = newest.strftime("%Y-%m-%dT%H:%MZ") if newest else "never"
			problems.append({"repo": repo, "problem": "poller_silent", "detail": f"last run started {last}"})
	tracking = _read_json(snap / "tracking.json")
	if not isinstance(tracking, list):
		problems.append({"repo": repo, "problem": "unreadable", "detail": "tracking_unparsable"})
	else:
		full = sorted(
			(int(i["number"]), int(i.get("comments") or 0)) for i in tracking
			if isinstance(i, dict) and "pull_request" not in i and isinstance(i.get("number"), int)
			and isinstance(i.get("comments"), int) and i["comments"] >= comment_limit
		)
		if full:
			problems.append({"repo": repo, "problem": "tracking_issue_comments", "detail": ", ".join(f"#{n} ({c} comments)" for n, c in full)})
	return problems, enable


def open_issue_index(open_issues: list, pipeline_login: str) -> dict[tuple[str, str], int]:
	"""Map (repo, problem) to the oldest open issue the pipeline filed for it.

	Only issues authored by ``pipeline_login`` whose body starts with the marker
	count, so an issue someone else opens can neither hide a problem nor be
	closed by the check.
	"""
	index: dict[tuple[str, str], int] = {}
	for issue in open_issues if isinstance(open_issues, list) else []:
		if not isinstance(issue, dict) or "pull_request" in issue:
			continue
		if str((issue.get("user") or {}).get("login") or "") != pipeline_login or not pipeline_login:
			continue
		match = MARKER_RE.match(str(issue.get("body") or ""))
		number = issue.get("number")
		if not match or not isinstance(number, int) or match.group(2) not in PROBLEMS:
			continue
		key = (match.group(1), match.group(2))
		if key not in index or number < index[key]:
			index[key] = number
	return index


def evaluate(consumers: list[str], snapshot_root: Path, open_issues: list, *, pipeline_login: str, templates: set[str], now: datetime, silent_hours: int, comment_limit: int) -> dict:
	problems: list[dict] = []
	enable: list[dict] = []
	checked: set[str] = set()
	unreadable: set[str] = set()
	for repo in consumers:
		repo_problems, repo_enable = check_repo(repo, snapshot_root / snapshot_dir_name(repo), templates=templates, now=now, silent_hours=silent_hours, comment_limit=comment_limit)
		# Keep one entry per (repo, problem): the first detail wins.
		seen = {p["problem"] for p in problems if p["repo"] == repo}
		for p in repo_problems:
			if p["problem"] not in seen:
				problems.append(p)
				seen.add(p["problem"])
		enable.extend(repo_enable)
		checked.add(repo)
		if "unreadable" in seen:
			unreadable.add(repo)
	index = open_issue_index(open_issues, pipeline_login)
	found = {(p["repo"], p["problem"]) for p in problems}
	to_open = [p for p in problems if (p["repo"], p["problem"]) not in index]
	to_close = []
	for (repo, problem), number in sorted(index.items(), key=lambda kv: kv[1]):
		if (repo, problem) in found:
			continue
		# A repo that could not be read this run says nothing about its other
		# problems; a repo no longer in the consumer list is closed.
		if repo in unreadable and problem != "unreadable":
			continue
		to_close.append({"number": number, "repo": repo, "problem": problem})
	return {"problems": problems, "enable": enable, "open": to_open, "close": to_close, "repos": len(checked)}


def _load_consumers(path: Path) -> list[str]:
	data = _read_json(path)
	if not isinstance(data, list):
		raise SystemExit(f"consumer list unreadable: {path}")
	return [r for r in data if isinstance(r, str) and REPO_RE.match(r)]


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	ev = sub.add_parser("evaluate")
	ev.add_argument("--consumers-json", required=True, type=Path)
	ev.add_argument("--snapshot-dir", required=True, type=Path)
	ev.add_argument("--open-issues-json", required=True, type=Path)
	ev.add_argument("--templates-file", required=True, type=Path, help="wrapper file names, one per line")
	ev.add_argument("--pipeline-login", required=True)
	ev.add_argument("--now", default="", help="UTC time, %%Y-%%m-%%dT%%H:%%M:%%SZ (default: now)")
	ev.add_argument("--silent-hours", type=int, default=6)
	ev.add_argument("--comment-limit", type=int, default=2000)
	ls = sub.add_parser("consumers")
	ls.add_argument("--consumers-json", required=True, type=Path)
	args = parser.parse_args(argv)
	if args.command == "consumers":
		for repo in _load_consumers(args.consumers_json):
			print(repo)
		return 0
	now = _parse_time(args.now) if args.now else datetime.now(timezone.utc).replace(microsecond=0)
	if now is None:
		raise SystemExit("--now must be %Y-%m-%dT%H:%M:%SZ")
	templates = {line.strip() for line in args.templates_file.read_text(encoding="utf-8").splitlines() if line.strip().endswith(".yml")}
	if not templates:
		raise SystemExit("no wrapper templates listed")
	open_issues = _read_json(args.open_issues_json)
	if not isinstance(open_issues, list):
		raise SystemExit("open issue list unreadable")
	result = evaluate(
		_load_consumers(args.consumers_json), args.snapshot_dir, open_issues,
		pipeline_login=args.pipeline_login, templates=templates, now=now,
		silent_hours=max(1, args.silent_hours), comment_limit=max(1, args.comment_limit),
	)
	json.dump(result, sys.stdout, sort_keys=True)
	sys.stdout.write("\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

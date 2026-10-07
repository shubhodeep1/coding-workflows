#!/usr/bin/env python3
"""Pick the blocked items the unblock judge looks at this tick.

The poller (`run_unblock_scan` in scripts/orchestrate_poll_process.sh,
docs/plans/replace-claude-sessions-with-cli-engine-plan.md Phase 7) fetches
the candidates and hands them to this script, which decides offline:

  select --search-file PATH --details-file PATH --runs-file PATH
         [--failed-projects-file PATH] --trusted-login LOGIN --now ISO8601
         [--min-blocked-minutes 30] [--marker-hours 6]
         [--inflight-minutes 60] [--max 5]

Inputs:
  --search-file           `items` of one REST `search/issues` call for open
                          issues and PRs carrying any block label
                          (`unblock_ledger.py labels`): number, labels,
                          pull_request, created_at, updated_at.
  --details-file          JSON object keyed by item number (string): each
                          `{"labeled": [{"label", "created_at"}],
                          "comments": [{"login", "body", "created_at"}]}`,
                          from one batched GraphQL query. A missing entry is
                          judged from `updated_at` alone and is never picked
                          on a fresh-marker guess.
  --runs-file             `workflow_runs` of unblock_judge_dispatch.yml (its
                          run name is `Unblock judge #<n>`).
  --failed-projects-file  JSON array of tracking issue numbers whose project
                          state is `failed` this tick (no API call: the poller
                          already holds the state).

An item is picked when all hold:
  - it has been blocked for at least --min-blocked-minutes (the newest
    `labeled` event of a block label it still carries; for a failed project
    without one, the newest trusted orchestrator state comment, which the
    poller posts when the project fails; else `updated_at`);
  - its newest trusted unblock marker (`<!-- ai:unblock:v1 ` or
    `<!-- ai:unblock-wait:v1 `, as the comment's last non-empty line, by
    --trusted-login) is older than --marker-hours, or there is none; an
    incomplete comment window cannot rule out an older edited marker until
    the poller verifies its full comment history;
  - no judge run for it is queued or in progress, and no non-cancelled run
    started in the last --inflight-minutes.
Oldest block first, at most one dispatch per tick. --max=0 disables dispatch;
larger values are capped at one. An active judge holds the queue until it
finishes.
Output: one JSON line
`{"dispatch": [{"item", "kind"}], "skipped": {<reason>: <count>}, "verify_history": n|null}`,
where kind is `project` (label ai:orchestrator-tracking), `pr` or `issue`.
`verify_history` asks the poller to read one complete history and rerun selection.

No GitHub API calls and no network (CLAUDE.md §15). Exit 0 ok, 1 bad
arguments, 2 unreadable input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import sys
from pathlib import Path

_LEDGER_SPEC = importlib.util.spec_from_file_location("unblock_ledger", Path(__file__).with_name("unblock_ledger.py"))
unblock_ledger = importlib.util.module_from_spec(_LEDGER_SPEC)
sys.modules.setdefault("unblock_ledger", unblock_ledger)
_LEDGER_SPEC.loader.exec_module(unblock_ledger)

TRACKING_LABEL = "ai:orchestrator-tracking"
CLOSED_LABEL = "ai:unblock-closed"
MARKER_PREFIXES = ("<!-- ai:unblock:v1 ", "<!-- ai:unblock-wait:v1 ")
STATE_PREFIX = "<!-- ORCHESTRATOR_STATE_V"
RUN_NAME_RE = re.compile(r"^Unblock judge #([1-9][0-9]*)$")
ACTIVE_RUN_STATES = ("queued", "in_progress", "waiting", "requested", "pending")


class UsageError(Exception):
	"""Bad arguments: exit 1."""


class InputError(Exception):
	"""An unreadable input file: exit 2."""


class _Parser(argparse.ArgumentParser):
	def error(self, message: str) -> None:
		raise UsageError(message)


def _read_json(path: str | None, flag: str, default: object) -> object:
	if not path:
		return default
	try:
		return json.loads(Path(path).read_text(encoding="utf-8") or "null")
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise InputError(f"cannot read {flag} {path}: {exc}") from exc


def _time(value: object) -> dt.datetime | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None
	return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _label_names(item: dict) -> set[str]:
	names = set()
	for label in item.get("labels") or []:
		name = label.get("name") if isinstance(label, dict) else label
		if isinstance(name, str):
			names.add(name)
	return names


def _latest_marker(comments: object, trusted_login: str) -> dt.datetime | None:
	newest = None
	for comment in comments if isinstance(comments, list) else []:
		if not isinstance(comment, dict) or comment.get("login") != trusted_login:
			continue
		lines = [line.strip() for line in str(comment.get("body") or "").splitlines() if line.strip()]
		if not lines or not lines[-1].startswith(MARKER_PREFIXES):
			continue
		created = _time(comment.get("created_at"))
		if created and (newest is None or created > newest):
			newest = created
	return newest


def _latest_state_comment(comments: object, trusted_login: str) -> dt.datetime | None:
	newest = None
	for comment in comments if isinstance(comments, list) else []:
		if not isinstance(comment, dict) or comment.get("login") != trusted_login:
			continue
		if not str(comment.get("body") or "").startswith(STATE_PREFIX):
			continue
		created = _time(comment.get("created_at"))
		if created and (newest is None or created > newest):
			newest = created
	return newest


def _busy_items(runs: object, now: dt.datetime, inflight: dt.timedelta) -> set[int]:
	busy = set()
	for run in runs if isinstance(runs, list) else []:
		if not isinstance(run, dict):
			continue
		match = RUN_NAME_RE.match(str(run.get("display_title") or run.get("name") or ""))
		if not match:
			continue
		created = _time(run.get("created_at"))
		if run.get("status") in ("cancelled", "skipped"):
			continue
		if run.get("status") in ACTIVE_RUN_STATES or (created and now - created < inflight):
			busy.add(int(match.group(1)))
	return busy


def select(
	search: object,
	details: object,
	runs: object,
	failed_projects: object,
	trusted_login: str,
	now: dt.datetime,
	min_blocked: dt.timedelta,
	marker_age: dt.timedelta,
	inflight: dt.timedelta,
	limit: int,
) -> dict:
	block_labels = set(unblock_ledger.BLOCK_LABELS)
	details = details if isinstance(details, dict) else {}
	failed = {int(n) for n in (failed_projects if isinstance(failed_projects, list) else []) if str(n).isdigit()}
	busy = _busy_items(runs, now, inflight)
	skipped: dict[str, int] = {}
	candidates: list[tuple[dt.datetime, int, str]] = []
	unverified: list[tuple[dt.datetime, int]] = []
	seen: set[int] = set()

	def skip(reason: str) -> None:
		skipped[reason] = skipped.get(reason, 0) + 1

	items = [item for item in (search if isinstance(search, list) else []) if isinstance(item, dict)]
	known = {int(item["number"]) for item in items if str(item.get("number", "")).isdigit()}
	# A failed project the search did not return (no block label on it) is
	# still judged; its labels are unknown here, so it is a project by origin.
	items += [{"number": number, "labels": [TRACKING_LABEL], "updated_at": ""} for number in sorted(failed - known)]
	for item in items:
		if not str(item.get("number", "")).isdigit():
			continue
		number = int(item["number"])
		if number in seen:
			continue
		seen.add(number)
		labels = _label_names(item)
		if CLOSED_LABEL in labels and number not in failed:
			skip("closed_by_judge")
			continue
		carried = labels & block_labels
		if not carried and number not in failed:
			skip("no_block_label")
			continue
		if number in busy:
			skip("judge_running")
			continue
		info = details.get(str(number)) if isinstance(details.get(str(number)), dict) else None
		since = None
		if info:
			for event in info.get("labeled") or []:
				if isinstance(event, dict) and event.get("label") in carried:
					created = _time(event.get("created_at"))
					if created and (since is None or created > since):
						since = created
		if since is None and info and not carried and number in failed:
			since = _latest_state_comment(info.get("comments"), trusted_login)
		since = since or _time(item.get("updated_at"))
		if since is None or now - since < min_blocked:
			skip("blocked_too_recently")
			continue
		if info is None:
			skip("no_details")
			continue
		marker = _latest_marker(info.get("comments"), trusted_login)
		if marker and now - marker < marker_age:
			skip("recent_verdict")
			continue
		if info.get("history_incomplete"):
			unverified.append((since, number))
			skip("unverified_marker_history")
			continue
		if TRACKING_LABEL in labels or number in failed:
			kind = "project"
		elif item.get("pull_request"):
			kind = "pr"
		else:
			kind = "issue"
		candidates.append((since, number, kind))
	candidates.sort()
	# A running judge owns this scan's dispatch slot until it finishes.
	active_judge = any(
		isinstance(run, dict)
		and RUN_NAME_RE.match(str(run.get("display_title") or run.get("name") or ""))
		and run.get("status") in ACTIVE_RUN_STATES
		for run in (runs if isinstance(runs, list) else [])
	)
	effective_limit = 0 if active_judge else min(limit, 1)
	chosen = candidates[:effective_limit]
	verify_history = None
	if effective_limit and unverified and (not chosen or min(unverified) < (chosen[0][0], chosen[0][1])):
		verify_history = sorted(unverified)[int(now.timestamp() // 300) % len(unverified)][1]
		chosen = []
	if len(candidates) > effective_limit:
		reason = "judge_running" if active_judge else "over_tick_cap"
		skipped[reason] = skipped.get(reason, 0) + len(candidates) - effective_limit
	return {"dispatch": [{"item": number, "kind": kind} for _, number, kind in chosen], "skipped": skipped, "verify_history": verify_history}


def build_parser() -> argparse.ArgumentParser:
	parser = _Parser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
	cmd = sub.add_parser("select")
	cmd.add_argument("--search-file", required=True)
	cmd.add_argument("--details-file", required=True)
	cmd.add_argument("--runs-file", required=True)
	cmd.add_argument("--failed-projects-file")
	cmd.add_argument("--trusted-login", required=True)
	cmd.add_argument("--now", required=True)
	cmd.add_argument("--min-blocked-minutes", type=int, default=30)
	cmd.add_argument("--marker-hours", type=int, default=6)
	cmd.add_argument("--inflight-minutes", type=int, default=60)
	cmd.add_argument("--max", type=int, default=5)
	return parser


def run(argv: list[str] | None = None) -> dict:
	args = build_parser().parse_args(argv)
	if not unblock_ledger.LOGIN_RE.match(args.trusted_login):
		raise UsageError(f"--trusted-login is not a GitHub login: {args.trusted_login!r}")
	now = _time(args.now)
	if now is None:
		raise UsageError(f"--now is not an ISO 8601 time: {args.now!r}")
	for name in ("min_blocked_minutes", "marker_hours", "inflight_minutes", "max"):
		if getattr(args, name) < 0:
			raise UsageError(f"--{name.replace('_', '-')} must not be negative")
	search = _read_json(args.search_file, "--search-file", [])
	if isinstance(search, dict):
		search = search.get("items", [])
	runs = _read_json(args.runs_file, "--runs-file", [])
	if isinstance(runs, dict):
		runs = runs.get("workflow_runs", [])
	return select(
		search,
		_read_json(args.details_file, "--details-file", {}),
		runs,
		_read_json(args.failed_projects_file, "--failed-projects-file", []),
		args.trusted_login,
		now,
		dt.timedelta(minutes=args.min_blocked_minutes),
		dt.timedelta(hours=args.marker_hours),
		dt.timedelta(minutes=args.inflight_minutes),
		args.max,
	)


def main(argv: list[str] | None = None) -> int:
	try:
		result = run(argv)
	except UsageError as exc:
		print(json.dumps({"error": str(exc)}))
		return 1
	except InputError as exc:
		print(json.dumps({"error": str(exc)}))
		return 2
	print(json.dumps(result))
	return 0


if __name__ == "__main__":
	sys.exit(main())

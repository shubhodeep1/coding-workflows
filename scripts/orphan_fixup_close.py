#!/usr/bin/env python3
"""Pick the open unblock fix-up issues whose source issue is already closed.

The unblock judge files a fix-up issue (`descope` / `operator step`) for a
blocked item and the item waits on it (scripts/unblock_actions.py
`_fixup_body`). The fix-up's first body line records the item it serves:
`<!-- ai:unblock-fixup:v1 item=<n> round=<r> -->`. When the item is closed
some other way (the pipeline found the work already done, a person closed it,
a re-issue replaced it), nothing waits on the fix-up any more, yet it stays
open: it is blocked itself, the judge files a fix-up of that fix-up, and the
chain grows (17 deep in one consumer repo). This script decides, offline,
which of those open fix-ups the poller may close.

The poller (`close_orphan_fixup_chains_sweep` in
scripts/orchestrate_poll_process.sh, wired into `run_unblock_scan`) fetches
the inputs and executes the closes; this script only decides.

  select --issues-file PATH --trusted-login LOGIN --now ISO8601
         [--parents-file PATH] [--max 5] [--grace-minutes 60]
  timeline-check --timeline-file PATH

`select` inputs:
  --issues-file    JSON array of the open issues (no pull requests): number,
                   labels (names), author_login, body (the first 400
                   characters are enough: the marker is the first line).
                   Output of one paginated REST list of open issues.
  --parents-file   JSON object keyed by item number (string) with the state of
                   every item a fix-up names that is not in --issues-file:
                   `{"type": "Issue"|"PullRequest", "state", "state_reason",
                   "closed_at"}`; a number whose lookup returned null is
                   absent or null. From one aliased GraphQL query.

An open issue is a candidate fix-up only when ALL hold:
  - its body's first line is exactly the fix-up marker, and the item it names
    is another issue (not itself, not a pull request);
  - its author is --trusted-login, the pipeline's own identity, so a marker
    quoted by anyone else does nothing;
  - it is not an orchestrator project issue or child (`ai:orchestrator-tracking`,
    `ai:orchestrator-managed`: the project state machine owns those);
  - every label it carries is in IDLE_LABELS, so nothing is implementing,
    planning, clarifying, validating or in review on it.
A candidate is closed when its item is closed (any close reason: completed,
not planned or duplicate; the fix-up only ever existed to clear that item's
block), and the item has been closed for at least --grace-minutes (a person
who reopens a wrongly closed item keeps its fix-up). When the item is itself
an open candidate, the fix-up is closed when that item is: the whole chain
goes in one pass, nearest the closed root first, so a closed parent is never
left behind an open child. An open item that is not a candidate (a real task,
or a fix-up that is in flight) keeps its fix-up. Re-issues, follow-ups
(`Accepted with this follow-up`) and `Source issue` / `Parent issue` links are
new work, not fix-ups, and are never matched.

Output of `select`: one JSON line
`{"close": [{"number", "parent", "root", "depth", "root_reason", "chain"}],
  "need_parents": [n], "skipped": {<reason>: <count>}}`
`close` is ordered by depth then number and cut to --max (0 closes nothing;
the cut ones count as `over_cap`). `need_parents` lists the items to look up
and pass back in --parents-file; fix-ups waiting on them count as
`parent_unknown` until then. `root` is the closed issue the chain ends at.

`timeline-check` reads the issue's timeline (the array
`_issue_timeline_with_cross_refs_json` prints) and prints
`{"ok": bool, "reason": s}`. It refuses a fix-up with an open or unreadable
linked pull request (also one in another repository, recognised by its
`/pull/` html_url in the REST shape, or by a non-null state in the GraphQL
shape, which has no html_url), or a cross-reference whose source could
not be resolved (null number), or one that was closed before (it was reopened, so a
person wants it); a timeline that is not an array fails closed.

No GitHub API calls and no network (CLAUDE.md §15). Exit 0 ok, 1 bad
arguments, 2 unreadable input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

FIXUP_MARKER_RE = re.compile(r"^<!-- ai:unblock-fixup:v1 item=([1-9][0-9]{0,9}) round=[1-9][0-9]{0,3} -->[ \t]*$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$")
TRACKING_LABELS = ("ai:orchestrator-tracking", "ai:orchestrator-managed")
# Labels a stuck fix-up carries. `ai:done` is allowed because the timeline
# check below refuses it while a linked pull request is still open.
IDLE_LABELS = frozenset(
	(
		"ai:blocked",
		"ai:scope-blocked",
		"ai:destructive-blocked",
		"ai:needs-human",
		"ai:unblock-closed",
		"ai:security",
		"ai:done",
	)
)
MAX_CHAIN_DEPTH = 64


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


def fixup_item(body: object) -> int | None:
	"""The item a fix-up body's first line names, else None."""
	if not isinstance(body, str):
		return None
	first = body.lstrip("﻿").split("\n", 1)[0].rstrip("\r")
	match = FIXUP_MARKER_RE.match(first)
	return int(match.group(1)) if match else None


def _candidate_parent(issue: dict, trusted_login: str) -> tuple[int | None, str]:
	"""(parent item, "") for a closable-in-principle fix-up, else (None, reason)."""
	parent = fixup_item(issue.get("body"))
	if parent is None:
		return None, "not_fixup"
	if parent == issue.get("number"):
		return None, "self_reference"
	if issue.get("author_login") != trusted_login:
		return None, "untrusted_author"
	labels = _label_names(issue)
	if any(label in labels for label in TRACKING_LABELS):
		return None, "project_issue"
	if not labels <= IDLE_LABELS:
		return None, "in_flight"
	return parent, ""


def select(
	issues: object,
	parents: object,
	trusted_login: str,
	now: dt.datetime,
	limit: int,
	grace: dt.timedelta,
) -> dict:
	skipped: dict[str, int] = {}

	def skip(reason: str) -> None:
		skipped[reason] = skipped.get(reason, 0) + 1

	issues = [item for item in issues if isinstance(item, dict) and isinstance(item.get("number"), int)] if isinstance(issues, list) else []
	parents = parents if isinstance(parents, dict) else {}
	open_numbers = {item["number"] for item in issues}
	candidates: dict[int, int] = {}
	for issue in issues:
		parent, reason = _candidate_parent(issue, trusted_login)
		if parent is None:
			# Plain issues are not counted; only a marker-bearing one the rules refuse.
			if reason != "not_fixup":
				skip(reason)
			continue
		candidates[issue["number"]] = parent

	need_parents: set[int] = set()
	verdicts: dict[int, tuple[bool, str, int, str, list[int]]] = {}
	# number -> (closeable, why-not or "", root, root_reason, chain)

	def resolve(number: int, seen: tuple[int, ...]) -> tuple[bool, str, int, str, list[int]]:
		if number in verdicts:
			return verdicts[number]
		parent = candidates[number]
		if len(seen) >= MAX_CHAIN_DEPTH or number in seen:
			result = (False, "chain_too_deep_or_cyclic", 0, "", [])
		elif parent in open_numbers:
			if parent in candidates:
				ok, why, root, root_reason, chain = resolve(parent, seen + (number,))
				result = (ok, why, root, root_reason, [parent] + chain)
			else:
				result = (False, "parent_open", 0, "", [])
		else:
			info = parents.get(str(parent))
			if not isinstance(info, dict):
				need_parents.add(parent)
				result = (False, "parent_unknown", 0, "", [])
			elif info.get("type") != "Issue":
				result = (False, "parent_not_issue", 0, "", [])
			elif str(info.get("state", "")).upper() != "CLOSED":
				result = (False, "parent_open", 0, "", [])
			else:
				closed_at = _time(info.get("closed_at"))
				if closed_at is None or now - closed_at < grace:
					result = (False, "parent_closed_recently", 0, "", [])
				else:
					reason = str(info.get("state_reason") or "").lower() or "unknown"
					result = (True, "", parent, reason, [])
		verdicts[number] = result
		return result

	chosen = []
	for number in sorted(candidates):
		ok, why, root, root_reason, chain = resolve(number, ())
		if ok:
			chosen.append((len(chain) + 1, number, candidates[number], root, root_reason, chain))
		else:
			skip(why)
	chosen.sort()
	out = []
	for depth, number, parent, root, root_reason, chain in chosen[: max(limit, 0)]:
		out.append({"number": number, "parent": parent, "root": root, "depth": depth, "root_reason": root_reason, "chain": [number] + chain + [root]})
	if len(chosen) > len(out):
		skipped["over_cap"] = len(chosen) - len(out)
	return {"close": out, "need_parents": sorted(need_parents), "skipped": skipped}


def timeline_check(timeline: object) -> dict:
	if not isinstance(timeline, list):
		return {"ok": False, "reason": "timeline_invalid"}
	for event in timeline:
		if not isinstance(event, dict):
			return {"ok": False, "reason": "timeline_invalid"}
		if event.get("event") == "closed":
			return {"ok": False, "reason": "previously_closed"}
	for event in timeline:
		if event.get("event") != "cross-referenced":
			continue
		source = (event.get("source") or {}).get("issue") if isinstance(event.get("source"), dict) else None
		if not isinstance(source, dict):
			continue
		# The GraphQL helper turns a source it could not resolve (an
		# inaccessible or deleted subject) into a placeholder with a null
		# number; it may be a pull request, so fail closed.
		if source.get("number") is None:
			return {"ok": False, "reason": "linked_pr_unreadable"}
		# _issue_timeline_with_cross_refs_json sets `pull_request` to null for a
		# pull request in another repository. The REST shape keeps html_url
		# (it names /pull/); the GraphQL shape has no html_url and gives only
		# pull requests a state (an issue source has state null).
		html_url = source.get("html_url")
		is_pull = bool(source.get("pull_request")) or (isinstance(html_url, str) and "/pull/" in html_url)
		if not is_pull and "html_url" not in source and source.get("state") in ("open", "closed"):
			is_pull = True
		if not is_pull:
			continue
		if source.get("lookup_failed"):
			return {"ok": False, "reason": "linked_pr_unreadable"}
		state = source.get("state")
		if state == "open":
			return {"ok": False, "reason": "open_linked_pr"}
		if state != "closed":
			return {"ok": False, "reason": "linked_pr_state_unknown"}
	return {"ok": True, "reason": "ok"}


def build_parser() -> argparse.ArgumentParser:
	parser = _Parser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
	cmd = sub.add_parser("select")
	cmd.add_argument("--issues-file", required=True)
	cmd.add_argument("--parents-file")
	cmd.add_argument("--trusted-login", required=True)
	cmd.add_argument("--now", required=True)
	cmd.add_argument("--max", type=int, default=5)
	cmd.add_argument("--grace-minutes", type=int, default=60)
	check = sub.add_parser("timeline-check")
	check.add_argument("--timeline-file", required=True)
	return parser


def run(argv: list[str] | None = None) -> dict:
	args = build_parser().parse_args(argv)
	if args.command == "timeline-check":
		return timeline_check(_read_json(args.timeline_file, "--timeline-file", None))
	if not LOGIN_RE.match(args.trusted_login):
		raise UsageError(f"--trusted-login is not a GitHub login: {args.trusted_login!r}")
	now = _time(args.now)
	if now is None:
		raise UsageError(f"--now is not an ISO 8601 time: {args.now!r}")
	for name in ("max", "grace_minutes"):
		if getattr(args, name) < 0:
			raise UsageError(f"--{name.replace('_', '-')} must not be negative")
	issues = _read_json(args.issues_file, "--issues-file", [])
	if not isinstance(issues, list):
		raise InputError(f"--issues-file {args.issues_file} is not a JSON array")
	return select(
		issues,
		_read_json(args.parents_file, "--parents-file", {}),
		args.trusted_login,
		now,
		args.max,
		dt.timedelta(minutes=args.grace_minutes),
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

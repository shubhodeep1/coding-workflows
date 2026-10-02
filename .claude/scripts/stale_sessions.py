#!/usr/bin/env python3
"""Prompt-stall report (plan D12) and session archive rules (plan D7) for the Claude issue pickup.

The hourly Claude issue pickup (`.claude/commands/claude-issue-pickup.md`,
`— wake.`, step 3b) runs this script with `--stalls-only` to list the
sessions stuck on a permission prompt. Without the flag the script also
applies the archive rules below to the automation sessions (issue
implementation sessions, `/implement-plan-claude` stages and checkers,
CLAUDE.md §26 checkers and `/fix-claude-pr` fixers) and prints the ids a
caller would archive; since operator decision Q3 (option A) nothing runs
it that way. The script never archives anything itself.

It also lists the sessions stuck on a permission prompt (D12), so the pickup
can notify once per stall and file it as an `ai:permission-prompt` issue.

The pickup runs it with `--stalls-only` (operator Q3: A, 2026-10-02): the
stale session sweep of CLAUDE.md §26.I (`scripts/claude_session_janitor.py`,
pickup step 3a) is the only archiver, and this script only reports the
prompt stalls (pickup step 3b). The archive rules below stay for a run
without the flag.

Input:
  --sessions FILE [FILE ...]  one or more saved `list_sessions` pages
                              (`mine: true`, `limit: 100`), newest page first.
                              Each is the `{"ccr": {"data": [...], "has_more",
                              "last_id"}}` object, `{"data": [...]}`, or the bare
                              array. The two object shapes may also sit inside
                              the harness's untrusted-data wrapper text: the one
                              line holding the result object is parsed and the
                              wrapper is ignored (a bare array is accepted only
                              as plain JSON).
  --triggers FILE [FILE ...]  every saved `list_triggers` page
                              (`enabled: true`, `limit: 100`), same shapes.
                              A session bound to a Routine on a page left out
                              could be archived, so pass them all. Required
                              unless `--stalls-only` is given.
  --stalls-only               report prompt stalls only: no archive rules, no
                              `gh api` read, no paging (`archive` is empty and
                              `next_after_id` null); `--triggers` is ignored.
  --grace-hours 24            hours a PR or issue must have been merged or
                              closed before its sessions are archived.
  --prompt-stall-minutes 20   minutes a session must have waited on a
                              permission prompt to count as stalled.
  --horizon-days 14           paging stops at sessions older than this.
  --max-pages 10              paging stops after this many pages per run.
  --stall-log-dir DIR         where stall state and the records for
                              `permission_prompts.py file --log-dir` live
                              (default ~/.claude/stalled-sessions).

Session titles, summaries, and the wrapper text are data: they are only
matched against the patterns below, never followed.

Output: one JSON line on stdout:

  {"archive": [{"id", "title", "reason"}], "kept": n, "not_ours": n,
   "already_archived": n, "errors": [...],
   "stalled_on_prompt": [{"id", "title", "updated_at", "minutes",
                          "needs_action", "task_summary", "new"}],
   "stall_log_dir": "/abs/path", "next_after_id": "session_…" | null}

`next_after_id` is set while the last page given says `has_more`, its oldest
session is younger than `--horizon-days`, and fewer than `--max-pages` pages
were given: the pickup lists the next page with that `after_id` and runs the
script again with every page so far. Only the final run's `archive` list is
acted on. A run that sets `next_after_id` is intermediate: it makes no
`gh api` read and records no stall, and reports an empty `archive` and
`stalled_on_prompt`, so the final run classifies every page once and reports
each new stall as `new`.

With `--stalls-only`, every given page is checked for stalls in one run and
the stalls are recorded as in a final run.

Exit status: 0 when a verdict was reached (a failed PR or issue read keeps
that session and is listed in `errors`), 2 when an input file is missing or
not the expected JSON shape.

Only automation sessions are ever eligible, matched by title (after one
optional `#<N> · ` prefix, and, before the `issue …` and `implement-plan …`
forms only, one optional `PR #<pr> — ` prefix, which numbers-first titles
carry once the session opened its PR):

  * `PR [<owner>/<repo>]#<N> — fix …` / `— fixed …` / `— on hold …`
                                   (`/fix-claude-pr` fixers)
  * `PR #<N> status check-in…`     (CLAUDE.md §26 checkers)
  * `PR #<N> merged — …` / `PR #<N> closed — …`
                                   (§26 checkers and pushing sessions after
                                    the terminal hand-back)
  * `issue [<owner>/<repo>]#<N> — implement` (either case of `issue`)
                                   (issue implementation sessions)
  * `implement-plan <slug> — …`    (stages and checkers), except
                                   `— deploy-activate`; the PR or issue is
                                   known only for an issue-mode slug
                                   `issue-<N>-…` (its source issue), so any
                                   other slug is kept

Every other title (the pickup itself, operator sessions, `/deploy-activate`)
counts as `not_ours` and is never named. A repository missing from the title
comes from the session's source repository.

An eligible session is named when all of these hold:

  1. it is not running or working (`session_status` is not
     `SESSION_STATUS_RUNNING` or `SESSION_STATUS_REQUIRES_ACTION`, and
     `status_bucket` is not `SESSION_STATUS_BUCKET_WORKING`);
  2. no enabled Routine has `persistent_session_id` = the session;
  3. either
     a. its PR or issue merged or closed at least `--grace-hours` ago; a
        session waiting on a question (`post_turn_summary.status_category`
        `need_input`) is archived under this rule too, because a question
        about a finished PR is moot; or
     b. it is a fixer and a newer fixer session (a later `created_at`) exists
        for the same repository and PR, and it is not waiting on a question.

Stalled on a prompt: every session, whatever its title, that is not
archived, whose `status_bucket` is `SESSION_STATUS_BUCKET_BLOCKED`, whose
`post_turn_summary.needs_action` starts with `Approve or deny`, and whose
`updated_at` is more than `--prompt-stall-minutes` old. A stall is `new` the
first time this script sees that session with that `updated_at`; the seen
pairs are kept in `<stall-log-dir>/reported-stalls.json`, and each new stall
is appended to `<stall-log-dir>/stalled-sessions.jsonl` as a
`PermissionRequest` record that `permission_prompts.py file` groups and files
(tool `StalledSession(<tool>)`; the command itself is not visible from
outside the session, so the title and `task_summary` are the evidence).
`new` only decides the push notification and the record; whether a record
was filed is tracked by `permission_prompts.py` itself (`filed-state.json`
in the same directory, advanced only after a successful POST). The pickup
therefore runs `permission_prompts.py file --log-dir` on every wake, so a
record whose filing failed is filed on a later wake even when no stall is
`new` (that run makes no API call when nothing is left to file).

API budget (CLAUDE.md §15): REST only, never GraphQL. One `gh api
repos/<owner>/<repo>/pulls/<N>` or `…/issues/<N>` read per distinct PR or
issue, cached (a failed read too, so it is not repeated for the next session
on the same PR or issue), only for a session that passed rules 1 and 2 and is
not a superseded fixer, and only on the final run. A failed read keeps the
session and is reported in `errors` (fail safe: never archive on missing
data).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_GRACE_HOURS = 24.0
DEFAULT_PROMPT_STALL_MINUTES = 20.0
DEFAULT_HORIZON_DAYS = 14.0
DEFAULT_MAX_PAGES = 10
DEFAULT_STALL_LOG_DIR = Path.home() / ".claude" / "stalled-sessions"
STALL_STATE_FILE = "reported-stalls.json"
STALL_RECORDS_FILE = "stalled-sessions.jsonl"
MAX_STALL_STATE_KEYS = 500

_REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
TITLE_PREFIX_PATTERN = re.compile(r"^#\d+ · ")
# Numbers-first titles (#4943) put `PR #<pr> — ` in front of a stage or issue session once it opens its
# PR; strip it only before those two forms, so fixer, check-in, and terminal titles keep their own match.
NUMBERED_PR_TITLE_PREFIX_PATTERN = re.compile(r"^PR #\d+ — (?=implement-plan |[Ii]ssue )")
FIXER_TITLE_PATTERN = re.compile(rf"^PR (?:(?P<repo>{_REPO}))?#(?P<number>\d+) — (?:fix|fixed|on hold)(?![\w-])")
CHECK_IN_TITLE_PATTERN = re.compile(r"^PR #(?P<number>\d+) status check-in")
TERMINAL_TITLE_PATTERN = re.compile(r"^PR #(?P<number>\d+) (?:merged|closed) — ")
ISSUE_START_TITLE_PATTERN = re.compile(rf"^[Ii]ssue (?:(?P<repo>{_REPO}))?#(?P<number>\d+) — implement(?![\w-])")
IMPLEMENT_PLAN_TITLE_PATTERN = re.compile(r"^implement-plan (?P<slug>\S+) — (?P<stage>.+)$")
ISSUE_MODE_SLUG_PATTERN = re.compile(r"^issue-(?P<number>\d+)-")
SOURCE_URL_PATTERN = re.compile(rf"^https://github\.com/(?P<repo>{_REPO}?)(?:\.git)?/?$")
SESSION_ID_PATTERN = re.compile(r"^(?:session|cse)_(?P<suffix>[A-Za-z0-9]+)$")
APPROVE_OR_DENY_PATTERN = re.compile(r"^Approve or deny\b\s*(?P<tool>[A-Za-z0-9_.:-]+)?")

RUNNING_STATUSES = frozenset({"SESSION_STATUS_RUNNING", "SESSION_STATUS_REQUIRES_ACTION"})
WORKING_BUCKET = "SESSION_STATUS_BUCKET_WORKING"
BLOCKED_BUCKET = "SESSION_STATUS_BUCKET_BLOCKED"
ARCHIVED_STATUS = "SESSION_STATUS_ARCHIVED"


class SessionReadError(Exception):
	"""A `gh api` read failed; the session it concerned is kept."""


def gh_api(path: str) -> dict:
	"""GET one REST path through `gh api` and return the decoded JSON object."""
	try:
		proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise SessionReadError(f"gh api {path} failed: {exc}") from exc
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout).strip().splitlines()
		raise SessionReadError(f"gh api {path} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	try:
		payload = json.loads(proc.stdout)
	except ValueError as exc:
		raise SessionReadError(f"gh api {path} returned invalid JSON") from exc
	if not isinstance(payload, dict):
		raise SessionReadError(f"gh api {path} returned non-object JSON")
	return payload


def _parse_time(value: object) -> dt.datetime:
	if not isinstance(value, str):
		raise ValueError(f"timestamp must be a string, got {type(value).__name__}")
	parsed = dt.datetime.fromisoformat(re.sub(r"\.(\d{6})\d+", r".\1", value.replace("Z", "+00:00")))
	return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _load_document(path: str) -> object:
	"""Parse a saved tool result: plain JSON, or the one result object inside the harness wrapper text.

	In wrapper text only a line holding a JSON object with a `ccr` or `data` key counts, so a stray JSON
	line (`[]`, `{}`) is never read as an empty page. Anything that could hide the real result is rejected
	instead of skipped: a line starting with `{` that does not parse (a truncated result), a result-shaped
	object that `_page` refuses, and more than one result object. A stray `{"data": []}` next to the real
	page therefore never stands in for it. A lone result object is the page, empty or not: the harness
	writes the result on a line of its own, and a lone `{"data": []}` cannot be told apart from a real
	empty page (the last `after_id` page, or no enabled Routine), so it is read as one.
	"""
	with open(path, encoding="utf-8") as handle:
		text = handle.read()
	try:
		return json.loads(text)
	except ValueError:
		pass
	found: list[object] = []
	for line in text.splitlines():
		line = line.strip()
		if not line.startswith("{"):
			continue
		try:
			candidate = json.loads(line)
		except ValueError as exc:
			raise ValueError("a JSON line in the wrapper text does not parse (truncated result?)") from exc
		if not isinstance(candidate, dict) or not {"ccr", "data"} & candidate.keys():
			continue
		_page(candidate)
		found.append(candidate)
	if len(found) > 1:
		raise ValueError(f"{len(found)} result objects found in the wrapper text; expected one")
	if not found:
		raise ValueError("no JSON document found")
	return found[0]


def _page(payload: object) -> tuple[list[dict], bool, str | None]:
	"""Return (items, has_more, last_id) from one result in any accepted shape."""
	if isinstance(payload, dict) and isinstance(payload.get("ccr"), dict):
		payload = payload["ccr"]
	has_more = False
	last_id = None
	if isinstance(payload, dict):
		has_more = payload.get("has_more") is True
		last_id = payload.get("last_id") if isinstance(payload.get("last_id"), str) else None
		payload = payload.get("data")
	if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
		raise ValueError("expected the tool result or its `data` array of objects")
	return payload, has_more, last_id


def _field(session: dict, key: str) -> object:
	"""A session field, top level first, then `external_metadata`."""
	value = session.get(key)
	if value is None and isinstance(session.get("external_metadata"), dict):
		value = session["external_metadata"].get(key)
	return value


def _post_turn_summary(session: dict) -> dict:
	summary = _field(session, "post_turn_summary")
	return summary if isinstance(summary, dict) else {}


def _normalise_session_id(value: object) -> str | None:
	if not isinstance(value, str):
		return None
	match = SESSION_ID_PATTERN.match(value)
	return match.group("suffix") if match else None


def session_repo(session: dict) -> str | None:
	"""`<owner>/<repo>` of the session's first GitHub source repository, or None."""
	context = session.get("session_context")
	sources = context.get("sources") if isinstance(context, dict) else None
	for source in sources if isinstance(sources, list) else []:
		repository = source.get("git_repository") if isinstance(source, dict) else None
		url = repository.get("url") if isinstance(repository, dict) else None
		match = SOURCE_URL_PATTERN.match(url) if isinstance(url, str) else None
		if match:
			return match.group("repo")
	return None


def classify_title(title: str) -> dict | None:
	"""Return {kind, target, repo, number} for an automation title, or None when it is not ours.

	`target` is `pr`, `issue`, or None (an implement-plan slug that names no PR or issue).
	"""
	title = TITLE_PREFIX_PATTERN.sub("", title, count=1)
	title = NUMBERED_PR_TITLE_PREFIX_PATTERN.sub("", title, count=1)
	match = FIXER_TITLE_PATTERN.match(title)
	if match:
		return {"kind": "fixer", "target": "pr", "repo": match.group("repo"), "number": int(match.group("number"))}
	for pattern, kind in ((CHECK_IN_TITLE_PATTERN, "check-in"), (TERMINAL_TITLE_PATTERN, "terminal")):
		match = pattern.match(title)
		if match:
			return {"kind": kind, "target": "pr", "repo": None, "number": int(match.group("number"))}
	match = ISSUE_START_TITLE_PATTERN.match(title)
	if match:
		return {"kind": "issue-start", "target": "issue", "repo": match.group("repo"), "number": int(match.group("number"))}
	match = IMPLEMENT_PLAN_TITLE_PATTERN.match(title)
	if match:
		if match.group("stage").startswith("deploy-activate"):
			return None
		issue = ISSUE_MODE_SLUG_PATTERN.match(match.group("slug"))
		if issue:
			return {"kind": "implement-plan", "target": "issue", "repo": None, "number": int(issue.group("number"))}
		return {"kind": "implement-plan", "target": None, "repo": None, "number": None}
	return None


def _load_sessions(paths: list[str]) -> tuple[list[dict], bool, str | None]:
	sessions: list[dict] = []
	seen: set[str] = set()
	has_more, last_id = False, None
	for path in paths:
		items, has_more, last_id = _page(_load_document(path))
		for item in items:
			session_id = item.get("id")
			if isinstance(session_id, str) and session_id not in seen:
				seen.add(session_id)
				sessions.append(item)
	return sessions, has_more, last_id


def _bound_session_suffixes(triggers: list[dict]) -> set[str]:
	bound: set[str] = set()
	for trigger in triggers:
		if trigger.get("enabled") is not True:
			continue
		suffix = _normalise_session_id(trigger.get("persistent_session_id"))
		if suffix:
			bound.add(suffix)
	return bound


def _terminal_age_hours(target: str, repo: str, number: int, now: dt.datetime, cache: dict) -> tuple[str, float] | None:
	"""(`merged` | `closed`, hours since) for a finished PR or issue, or None while open (one cached read each).

	A failed read is cached too, so every later session on the same PR or issue gets the same error without
	another request.
	"""
	path = f"repos/{repo}/pulls/{number}" if target == "pr" else f"repos/{repo}/issues/{number}"
	if path not in cache:
		try:
			cache[path] = gh_api(path)
		except SessionReadError as exc:
			cache[path] = exc
	item = cache[path]
	if isinstance(item, SessionReadError):
		raise item
	if target == "pr" and item.get("merged"):
		return "merged", (now - _parse_time(item.get("merged_at"))).total_seconds() / 3600
	if item.get("state") == "closed":
		return "closed", (now - _parse_time(item.get("closed_at"))).total_seconds() / 3600
	return None


def _created(session: dict) -> dt.datetime | None:
	try:
		return _parse_time(session.get("created_at"))
	except ValueError:
		return None


def classify(sessions: list[dict], triggers: list[dict], grace_hours: float, now: dt.datetime) -> dict:
	"""Split `sessions` into the ones to archive and counts of the rest."""
	to_archive: list[dict] = []
	errors: list[str] = []
	kept = not_ours = already_archived = 0
	bound = _bound_session_suffixes(triggers)
	cache: dict = {}
	parsed: list[tuple[dict, dict | None, str | None]] = []
	newest_fixer: dict[tuple[str, int], tuple[dt.datetime, str]] = {}
	for session in sessions:
		title = session.get("title")
		info = classify_title(title) if isinstance(title, str) else None
		repo = (info.get("repo") or session_repo(session)) if info else None
		parsed.append((session, info, repo))
		created = _created(session)
		if info and info["kind"] == "fixer" and repo and created:
			key = (repo.lower(), info["number"])
			if key not in newest_fixer or created > newest_fixer[key][0]:
				newest_fixer[key] = (created, str(session.get("id")))
	for session, info, repo in parsed:
		session_id = session.get("id")
		title = session.get("title")
		if not isinstance(session_id, str) or info is None:
			not_ours += 1
			continue
		if session.get("session_status") == ARCHIVED_STATUS:
			already_archived += 1
			continue
		if (session.get("session_status") in RUNNING_STATUSES or session.get("status_bucket") == WORKING_BUCKET
				or _normalise_session_id(session_id) in bound or info["target"] is None or not repo):
			kept += 1
			continue
		need_input = _post_turn_summary(session).get("status_category") == "need_input"
		if info["kind"] == "fixer" and not need_input:
			newest = newest_fixer.get((repo.lower(), info["number"]))
			created = _created(session)
			if newest and created and newest[0] > created:
				to_archive.append({"id": session_id, "title": title, "reason": f"superseded by fixer {newest[1]} for {repo}#{info['number']}"})
				continue
		try:
			ended = _terminal_age_hours(info["target"], repo, info["number"], now, cache)
		except (SessionReadError, KeyError, TypeError, ValueError) as exc:
			errors.append(f"{session_id} ({title}): {exc}")
			kept += 1
			continue
		if ended is not None and ended[1] >= grace_hours:
			noun = "PR" if info["target"] == "pr" else "issue"
			reason = f"{noun} {repo}#{info['number']} {ended[0]} {ended[1]:.1f}h ago (>= {grace_hours:g}h)"
			to_archive.append({"id": session_id, "title": title, "reason": reason})
			continue
		kept += 1
	return {"archive": to_archive, "kept": kept, "not_ours": not_ours, "already_archived": already_archived, "errors": errors}


def stalled_on_prompt(sessions: list[dict], stall_minutes: float, now: dt.datetime) -> list[dict]:
	"""Every non-archived session blocked on a permission prompt for more than `stall_minutes`."""
	stalls: list[dict] = []
	for session in sessions:
		if session.get("session_status") == ARCHIVED_STATUS or session.get("status_bucket") != BLOCKED_BUCKET:
			continue
		needs_action = _post_turn_summary(session).get("needs_action")
		if not isinstance(needs_action, str) or not APPROVE_OR_DENY_PATTERN.match(needs_action):
			continue
		try:
			updated = _parse_time(session.get("updated_at"))
		except ValueError:
			continue
		minutes = (now - updated).total_seconds() / 60
		if minutes <= stall_minutes:
			continue
		task_summary = _field(session, "task_summary")
		stalls.append(
			{
				"id": session.get("id"),
				"title": session.get("title") if isinstance(session.get("title"), str) else "",
				"updated_at": session.get("updated_at"),
				"minutes": round(minutes),
				"needs_action": needs_action,
				"task_summary": task_summary if isinstance(task_summary, str) else "",
				"permission_mode": session.get("permission_mode") if isinstance(session.get("permission_mode"), str) else "",
			}
		)
	return stalls


def record_stalls(stalls: list[dict], log_dir: Path, now: dt.datetime) -> None:
	"""Mark each stall `new` or not, and append the new ones as `permission_prompts.py` records.

	Marking a stall seen here does not mark it filed: `permission_prompts.py file` keeps its own filed
	counts next to these records, so a filing that fails is retried by the next `file` run on this directory.
	"""
	state_path = log_dir / STALL_STATE_FILE
	try:
		seen = json.loads(state_path.read_text(encoding="utf-8"))
		seen = [key for key in seen if isinstance(key, str)] if isinstance(seen, list) else []
	except (OSError, ValueError):
		seen = []
	seen_set = set(seen)
	new_records: list[str] = []
	for stall in stalls:
		key = f"{stall['id']}@{stall['updated_at']}"
		stall["new"] = key not in seen_set
		if not stall["new"]:
			continue
		seen.append(key)
		seen_set.add(key)
		tool = APPROVE_OR_DENY_PATTERN.match(stall["needs_action"]).group("tool")
		new_records.append(
			json.dumps(
				{
					"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
					"event": "PermissionRequest",
					"session_id": stall["id"],
					"tool_name": f"StalledSession({tool})" if tool else "StalledSession",
					"tool_input": {
						"session_id": stall["id"],
						"title": stall["title"],
						"task_summary": stall["task_summary"],
						"needs_action": stall["needs_action"],
						"waiting_since": stall["updated_at"],
					},
					"reason": f"session waited on a permission prompt for {stall['minutes']} minutes (seen by the Claude issue pickup)",
					"permission_mode": stall.pop("permission_mode", ""),
				},
				ensure_ascii=False,
			)
		)
	for stall in stalls:
		stall.pop("permission_mode", None)
	if not new_records:
		return
	log_dir.mkdir(parents=True, exist_ok=True)
	with open(log_dir / STALL_RECORDS_FILE, "a", encoding="utf-8") as handle:
		handle.write("".join(record + "\n" for record in new_records))
	state_path.write_text(json.dumps(seen[-MAX_STALL_STATE_KEYS:], indent=1), encoding="utf-8")


def next_after_id(sessions: list[dict], pages: int, has_more: bool, last_id: str | None, now: dt.datetime,
		horizon_days: float, max_pages: int) -> str | None:
	"""The `after_id` for the next `list_sessions` page, or None when paging is done."""
	if not has_more or not last_id or pages >= max_pages:
		return None
	created = [value for value in (_created(session) for session in sessions) if value]
	if created and (now - min(created)).total_seconds() / 86400 > horizon_days:
		return None
	return last_id


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--sessions", required=True, nargs="+", help="saved list_sessions pages, newest first")
	parser.add_argument("--triggers", nargs="+", help="every saved list_triggers page (enabled: true); required unless --stalls-only")
	parser.add_argument("--stalls-only", action="store_true", help="report prompt stalls only; archive nothing, read nothing, page nothing")
	parser.add_argument("--grace-hours", type=float, default=DEFAULT_GRACE_HOURS)
	parser.add_argument("--prompt-stall-minutes", type=float, default=DEFAULT_PROMPT_STALL_MINUTES)
	parser.add_argument("--horizon-days", type=float, default=DEFAULT_HORIZON_DAYS)
	parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
	parser.add_argument("--stall-log-dir", default=str(DEFAULT_STALL_LOG_DIR))
	return parser


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		sessions, has_more, last_id = _load_sessions(args.sessions)
	except (OSError, ValueError) as exc:
		print(json.dumps({"archive": [], "error": f"cannot read --sessions: {exc}"}))
		return 2
	triggers: list[dict] = []
	if not args.stalls_only:
		if not args.triggers:
			print(json.dumps({"archive": [], "error": "--triggers is required unless --stalls-only is given"}))
			return 2
		try:
			triggers = [trigger for path in args.triggers for trigger in _page(_load_document(path))[0]]
		except (OSError, ValueError) as exc:
			print(json.dumps({"archive": [], "error": f"cannot read --triggers: {exc}"}))
			return 2
	now = now or dt.datetime.now(dt.timezone.utc)
	stall_log_dir = Path(args.stall_log_dir).expanduser().resolve()
	following_page = None
	if not args.stalls_only:
		following_page = next_after_id(
			sessions, len(args.sessions), has_more, last_id, now, args.horizon_days, args.max_pages
		)
	if args.stalls_only:
		# Pickup step 3b: the §26.I janitor archives; this run only reports stalls (operator Q3: A).
		result = {"archive": [], "kept": 0, "not_ours": 0, "already_archived": 0, "errors": []}
		stalls = stalled_on_prompt(sessions, args.prompt_stall_minutes, now)
		try:
			record_stalls(stalls, stall_log_dir, now)
		except OSError as exc:
			result["errors"].append(f"cannot write {stall_log_dir}: {exc}")
		result["stalled_on_prompt"] = stalls
	elif following_page is not None:
		# An intermediate run: the pickup only lists the next page and acts on the final run, so this
		# one makes no `gh api` read and records no stall (recording here would make the final run
		# report this page's stalls as `new: false` and skip their notifications).
		result = {"archive": [], "kept": 0, "not_ours": 0, "already_archived": 0, "errors": [], "stalled_on_prompt": []}
	else:
		result = classify(sessions, triggers, args.grace_hours, now)
		stalls = stalled_on_prompt(sessions, args.prompt_stall_minutes, now)
		try:
			record_stalls(stalls, stall_log_dir, now)
		except OSError as exc:
			result["errors"].append(f"cannot write {stall_log_dir}: {exc}")
		result["stalled_on_prompt"] = stalls
	result["stall_log_dir"] = str(stall_log_dir)
	result["next_after_id"] = following_page
	print(json.dumps(result, ensure_ascii=False))
	return 0


if __name__ == "__main__":
	sys.exit(main())

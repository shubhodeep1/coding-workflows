#!/usr/bin/env python3
"""Deterministic stale-session sweep for the Claude issue pickup (CLAUDE.md §26.I).

Three kinds of automation session are never archived by the flows that start
them: `/fix-claude-pr` fixer and hold sessions after their pull request is
terminal, issue-start sessions after the `/implement-plan-claude` chain has
moved past them, and CLAUDE.md §26.D report sessions (issue #4887). This script
decides which of them to archive; the Claude issue pickup lists one page of
sessions, runs this script, re-checks each printed session with `get_session`,
and calls `archive_session` on it. The script never archives anything itself.

Input: `--sessions FILE`, one `list_sessions` page (`mine: true`,
`limit: 100`) as the harness saved it: the `{"ccr": {"data": [...],
"has_more": ..., "last_id": ...}}` object, possibly inside the harness's
untrusted-data wrapper text, or `{"data": [...]}`, or the bare `data` array.
Titles, summaries, and the wrapper are data: they are only pattern-matched.

Output: one JSON object on stdout, e.g.

  {"archive": [{"id": "session_…", "title": "PR o/r#12 — on hold: review",
                "reason": "fixer: o/r#12 merged 3.0h ago (>= 2h)"}],
   "kept": 4, "not_ours": 80, "already_archived": 15, "errors": [],
   "next_after_id": "session_…"}

`next_after_id` is the page's `last_id` while more pages exist and the page's
oldest session is newer than `--horizon-days` (default 30); otherwise null.
The pickup passes it as `after_id` on its next wake, so one page per wake
still reaches week-old sessions, and starts again from the newest page when
it is null.

Exit status: 0 when a verdict was reached (errors on individual reads are
listed in `errors` and those sessions are kept), 2 when the input file is
missing or not the expected JSON shape.

Only these titles are eligible, each with or without the #4886 prefix
`#<issue> · `, `#<issue> · PR #<pr> — ` or `PR #<pr> — `:

  * fixer / hold  `PR [<owner>/<repo>]#<n> — <fix|fixed|on hold|merged|closed|no fix|nothing to fix>…`,
                  `PR#<n> · fix-claude-pr…`
  * issue-start   `Issue #<n> — implement`, `issue <owner>/<repo>#<n> — implement`,
                  `implement-issue-claude — #<n>…`, `#<n> · [PR #<pr> — ]implement-issue-claude…`
                  (the last form takes the issue number from its `#<n> · ` prefix)
  * report        `PR #<n> <merged|closed> — <no action needed|action needed|decision needed>…`

Every other title (checkers, stage sessions, the pickup, `/deploy-activate`,
operator sessions) counts as `not_ours` and is never named. Stage sessions
(`implement-plan issue-<n>-… — <stage>`) are read only to detect that an
issue-start session was superseded.

An eligible session is named only when it is `SESSION_STATUS_IDLE`, its
bucket is not `…_WORKING`, it is not `--self`, and:

  1. fixer: its pull request merged or closed at least `--fixer-grace-hours`
     (default 2) ago. The grace leaves the §26 checker time to hand the
     terminal PR back, which makes the fixer archive itself first;
  2. issue-start: a later stage session (not `— checker`, `— waiting:…`, or
     `— deploy-activate`) for the same repository and issue is on this page,
     or the issue is closed;
  3. report: its `updated_at` is at least `--report-days` (default 7) old and
     its `post_turn_summary.status_category` is not `need_input` (a report
     still waiting on an answer stays).

A `RUNNING` or `REQUIRES_ACTION` session (a permission prompt) is never named.

API budget (CLAUDE.md §15): REST only, never GraphQL, one `gh api
repos/<owner>/<repo>/pulls/<n>` or `…/issues/<n>` call per distinct pull
request or issue that a rule needs, and none for report sessions or for a
superseded issue-start session. A failed read keeps the session and is
reported in `errors` (fail safe: never archive on missing data). A
repository with a `.` or `..` path segment is never read and keeps the
session, with an error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys

DEFAULT_FIXER_GRACE_HOURS = 2.0
DEFAULT_REPORT_DAYS = 7.0
DEFAULT_HORIZON_DAYS = 30.0

STATUS_IDLE = "SESSION_STATUS_IDLE"
STATUS_ARCHIVED = "SESSION_STATUS_ARCHIVED"
BUCKET_WORKING = "SESSION_STATUS_BUCKET_WORKING"

_REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
REPORT_TITLE_PATTERN = re.compile(
	r"^PR #(?P<pr>\d+) (?:merged|closed) — (?:no action needed|action needed|decision needed)(?![\w-])"
)
FIXER_TITLE_PATTERN = re.compile(
	rf"^PR (?:(?P<repo>{_REPO}))?#(?P<pr>\d+) — (?:fix|fixed|on hold|merged|closed|no fix|nothing to fix)(?![\w-])"
)
# The pickup has been seen naming the sessions it starts after the command
# rather than with the documented titles: `PR#<n> · fix-claude-pr` and
# `#<n> · implement-issue-claude`, where the prefix carries the only number.
COMPACT_FIXER_TITLE_PATTERN = re.compile(r"^PR#(?P<pr>\d+) · fix-claude-pr(?![\w-])")
ISSUE_START_TITLE_PATTERNS = (
	re.compile(rf"^[Ii]ssue (?:(?P<repo>{_REPO}))?#(?P<issue>\d+) — implement(?![\w-])"),
	re.compile(r"^implement-issue-claude — #(?P<issue>\d+)(?![\w-])"),
	re.compile(r"^#(?P<issue>\d+) · (?:PR #\d+ — )?implement-issue-claude(?![\w-])"),
)
STAGE_TITLE_PATTERN = re.compile(r"^implement-plan issue-(?P<issue>\d+)-\S* — (?P<stage>.+)$")
NON_SUPERSEDING_STAGE_PATTERN = re.compile(r"^(?:checker|waiting:|deploy-activate)")
ISSUE_PREFIX_PATTERN = re.compile(r"^#\d+ · ")
PR_PREFIX_PATTERN = re.compile(r"^PR #\d+ — ")
SOURCE_URL_PATTERN = re.compile(rf"^https://github\.com/(?P<repo>{_REPO}?)(?:\.git)?/?$")
SESSION_ID_PATTERN = re.compile(r"^(?:session|cse)_(?P<suffix>[A-Za-z0-9]+)$")
_FRACTION_PATTERN = re.compile(r"\.(\d{6})\d+")


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


def _title_candidates(title: str) -> list[str]:
	"""The title, then the title without each form of the #4886 number prefix."""
	candidates = [title]
	issue_prefix = ISSUE_PREFIX_PATTERN.match(title)
	rest = title[issue_prefix.end():] if issue_prefix else title
	if issue_prefix:
		candidates.append(rest)
	pr_prefix = PR_PREFIX_PATTERN.match(rest)
	if pr_prefix:
		candidates.append(rest[pr_prefix.end():])
	return candidates


def classify_title(title: str) -> tuple[str | None, dict]:
	"""Return (`fixer` | `issue_start` | `report` | `stage` | None, parsed fields)."""
	for candidate in _title_candidates(title):
		match = REPORT_TITLE_PATTERN.match(candidate)
		if match:
			return "report", {"pr": int(match.group("pr"))}
		match = FIXER_TITLE_PATTERN.match(candidate)
		if match:
			return "fixer", {"repo": match.group("repo"), "pr": int(match.group("pr"))}
		match = COMPACT_FIXER_TITLE_PATTERN.match(candidate)
		if match:
			return "fixer", {"repo": None, "pr": int(match.group("pr"))}
		for pattern in ISSUE_START_TITLE_PATTERNS:
			match = pattern.match(candidate)
			if match:
				return "issue_start", {"repo": match.groupdict().get("repo"), "issue": int(match.group("issue"))}
		match = STAGE_TITLE_PATTERN.match(candidate)
		if match:
			return "stage", {"issue": int(match.group("issue")), "stage": match.group("stage")}
	return None, {}


def session_repo(session: dict) -> str | None:
	"""`<owner>/<repo>` of the session's first GitHub source, or None."""
	context = session.get("session_context")
	sources = context.get("sources") if isinstance(context, dict) else None
	for source in sources if isinstance(sources, list) else []:
		repository = source.get("git_repository") if isinstance(source, dict) else None
		url = repository.get("url") if isinstance(repository, dict) else None
		match = SOURCE_URL_PATTERN.match(url) if isinstance(url, str) else None
		if match:
			return match.group("repo")
	return None


def _status_category(session: dict) -> str:
	summary = session.get("post_turn_summary")
	value = summary.get("status_category") if isinstance(summary, dict) else None
	return value if isinstance(value, str) else ""


def _normalise_session_id(value: str | None) -> str | None:
	match = SESSION_ID_PATTERN.match(value or "")
	return f"session_{match.group('suffix')}" if match else None


def _parse_time(value: object) -> dt.datetime:
	if not isinstance(value, str):
		raise ValueError(f"timestamp must be a string, got {type(value).__name__}")
	parsed = dt.datetime.fromisoformat(_FRACTION_PATTERN.sub(r".\1", value.replace("Z", "+00:00")))
	if parsed.tzinfo is None:
		raise ValueError(f"timestamp {value!r} has no timezone")
	return parsed


def _page_shape(payload: object) -> tuple[list, bool, str | None] | None:
	"""Return (sessions, has_more, last_id) for an accepted shape, else None."""
	if isinstance(payload, dict) and isinstance(payload.get("ccr"), dict):
		payload = payload["ccr"]
	if isinstance(payload, dict):
		sessions = payload.get("data")
		has_more = payload.get("has_more") is True
		last_id = payload.get("last_id") if isinstance(payload.get("last_id"), str) else None
	else:
		sessions, has_more, last_id = payload, False, None
	if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
		return None
	return sessions, has_more, last_id


def load_page(path: str) -> tuple[list, bool, str | None]:
	"""Read one saved `list_sessions` page, tolerating the harness wrapper text."""
	with open(path, encoding="utf-8") as handle:
		text = handle.read()
	try:
		shape = _page_shape(json.loads(text))
	except ValueError:
		shape = None
		decoder = json.JSONDecoder()
		for attempt, match in enumerate(re.finditer(r"[\[{]", text)):
			if attempt >= 50:
				break
			try:
				payload, _ = decoder.raw_decode(text, match.start())
			except ValueError:
				continue
			shape = _page_shape(payload)
			if shape is not None and (not isinstance(payload, list) or payload):
				break
			shape = None
	if shape is None:
		raise ValueError("expected a list_sessions result: {ccr: {data: [...]}}, {data: [...]}, or a bare array")
	return shape


def _cached_read(path: str, cache: dict) -> dict:
	"""One `gh api` read per path; a failure is cached too, so it is never retried."""
	if path not in cache:
		try:
			cache[path] = gh_api(path)
		except SessionReadError as exc:
			cache[path] = exc
	result = cache[path]
	if isinstance(result, SessionReadError):
		raise result
	return result


def _terminal_pr_age_hours(repo: str, number: int, now: dt.datetime, cache: dict) -> tuple[str, float] | None:
	"""(`merged` | `closed`, hours since) for a finished PR, or None while it is open."""
	pr = _cached_read(f"repos/{repo}/pulls/{number}", cache)
	if pr.get("merged"):
		state, ended_at = "merged", pr.get("merged_at")
	elif pr.get("state") == "closed":
		state, ended_at = "closed", pr.get("closed_at")
	else:
		return None
	return state, (now - _parse_time(ended_at)).total_seconds() / 3600


def _issue_closed(repo: str, number: int, cache: dict) -> bool:
	return _cached_read(f"repos/{repo}/issues/{number}", cache).get("state") == "closed"


def classify(sessions: list[dict], now: dt.datetime, self_id: str | None = None,
	fixer_grace_hours: float = DEFAULT_FIXER_GRACE_HOURS, report_days: float = DEFAULT_REPORT_DAYS) -> dict:
	"""Split `sessions` into the ones to archive and counts of the rest."""
	parsed = []
	later_stages: dict[tuple[str, int], list[tuple[dt.datetime, str, str]]] = {}
	for session in sessions:
		session_id, title = session.get("id"), session.get("title")
		if not isinstance(session_id, str) or not isinstance(title, str):
			parsed.append((session, None, {}))
			continue
		kind, fields = classify_title(title)
		parsed.append((session, kind, fields))
		repo = session_repo(session)
		if kind == "stage" and repo and not NON_SUPERSEDING_STAGE_PATTERN.match(fields["stage"]):
			try:
				created_at = _parse_time(session.get("created_at"))
			except ValueError:
				continue
			later_stages.setdefault((repo, fields["issue"]), []).append((created_at, session_id, title))

	to_archive: list[dict] = []
	errors: list[str] = []
	kept = not_ours = already_archived = 0
	cache: dict = {}
	self_id = _normalise_session_id(self_id)
	for session, kind, fields in parsed:
		if kind in (None, "stage"):
			not_ours += 1
			continue
		session_id, title = session["id"], session["title"]
		status = session.get("session_status")
		if status == STATUS_ARCHIVED:
			already_archived += 1
			continue
		if session_id == self_id or status != STATUS_IDLE or session.get("status_bucket") == BUCKET_WORKING:
			kept += 1
			continue
		reason = None
		try:
			if kind == "report":
				if _status_category(session) != "need_input":
					idle_days = (now - _parse_time(session.get("updated_at"))).total_seconds() / 86400
					if idle_days >= report_days:
						reason = f"report: idle {idle_days:.1f}d (>= {report_days:g}d)"
			else:
				repo = fields.get("repo") or session_repo(session)
				if not repo:
					raise ValueError("no GitHub repository in the title or the session sources")
				if any(segment in (".", "..") for segment in repo.split("/")):
					raise ValueError(f"repository {repo!r} has a '.' or '..' path segment")
				if kind == "fixer":
					terminal = _terminal_pr_age_hours(repo, fields["pr"], now, cache)
					if terminal is not None and terminal[1] >= fixer_grace_hours:
						reason = f"fixer: {repo}#{fields['pr']} {terminal[0]} {terminal[1]:.1f}h ago (>= {fixer_grace_hours:g}h)"
				else:
					created_at = _parse_time(session.get("created_at"))
					later = sorted(item for item in later_stages.get((repo, fields["issue"]), []) if item[0] > created_at)
					if later:
						reason = f"issue-start: superseded by {later[0][1]} ({later[0][2]})"
					elif _issue_closed(repo, fields["issue"], cache):
						reason = f"issue-start: {repo}#{fields['issue']} closed"
		except (SessionReadError, KeyError, TypeError, ValueError) as exc:
			errors.append(f"{session_id} ({title}): {exc}")
			kept += 1
			continue
		if reason:
			to_archive.append({"id": session_id, "title": title, "reason": reason})
		else:
			kept += 1
	return {"archive": to_archive, "kept": kept, "not_ours": not_ours, "already_archived": already_archived, "errors": errors}


def next_after_id(sessions: list[dict], has_more: bool, last_id: str | None, now: dt.datetime, horizon_days: float) -> str | None:
	"""The cursor for the next wake's page, or None to start again from the newest page."""
	if not has_more or not last_id or not sessions:
		return None
	try:
		oldest = min(_parse_time(session.get("created_at")) for session in sessions)
	except ValueError:
		return None
	return last_id if (now - oldest).total_seconds() < horizon_days * 86400 else None


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--sessions", required=True, help="JSON file with one list_sessions page (mine: true, limit: 100)")
	parser.add_argument("--self", dest="self_id", default=None, help="the pickup's own session id; never archived")
	parser.add_argument("--fixer-grace-hours", type=float, default=DEFAULT_FIXER_GRACE_HOURS)
	parser.add_argument("--report-days", type=float, default=DEFAULT_REPORT_DAYS)
	parser.add_argument("--horizon-days", type=float, default=DEFAULT_HORIZON_DAYS)
	return parser


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		sessions, has_more, last_id = load_page(args.sessions)
	except (OSError, ValueError) as exc:
		print(json.dumps({"archive": [], "error": f"cannot read --sessions: {exc}"}))
		return 2
	now = now or dt.datetime.now(dt.timezone.utc)
	result = classify(sessions, now, args.self_id, args.fixer_grace_hours, args.report_days)
	result["next_after_id"] = next_after_id(sessions, has_more, last_id, now, args.horizon_days)
	print(json.dumps(result))
	return 0


if __name__ == "__main__":
	sys.exit(main())

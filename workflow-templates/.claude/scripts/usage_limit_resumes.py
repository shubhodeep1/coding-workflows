#!/usr/bin/env python3
"""Deterministic selector for sessions stopped by the account usage limit (issue #5660).

When the account hits its Claude usage limit, every running session fails its
turn and nothing wakes it after the limit resets: a checker's `send_later`
chain dies with the failed turn, and stage, fixer, and implementation
sessions stop mid-task. The Claude issue pickup is cron-backed, so it is the
first session that runs after a reset. On every wake it lists sessions and
triggers, runs this script, and creates one one-shot trigger per selected
session (`.claude/commands/claude-issue-pickup.md` step 1a). The script only
decides; it never calls an API and never creates anything itself.

Input:
  --sessions FILE   (repeatable) a saved `list_sessions` result (`mine: true`,
                    `limit: 100`, one file per page). The harness saves large
                    results to a file wrapped in an `<other-session …>`
                    envelope; pass the file as is. `{"ccr": {"data": [...]}}`,
                    `{"data": [...]}`, and a bare array are accepted too.
  --triggers FILE   (repeatable) a saved `list_triggers` result
                    (`enabled: true`), in the same forms.
  --pickup-session  the pickup's own session id: never selected, and its own
                    `rate_limit_info` is the account-wide hold-off.
  --handoff-author-login
                    the CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN value every resume
                    prompt names (required, never empty).
  --limit N         per-wake cap; default CLAUDE_USAGE_LIMIT_RESUME_LIMIT
                    (20), clamped to 1..40.

Output: one JSON object on stdout, e.g.

  {"resume": [{"session_id": "session_…", "kind": "checker", "issue": 5660,
               "signal": "text", "trigger_name": "Resume after usage limit (#5660)",
               "fire_offset_minutes": 2, "prompt": "…"}],
   "pending": [{"session_id": "session_…", "kind": "other", "issue": null}],
   "skipped": [{"session_id": "session_…", "signal": "text", "reason": "wake_pending"}],
   "not_reset": false, "limit": 20, "considered": 187, "errors": []}

Exit status: 0 when a verdict was reached (a malformed entry is listed in
`errors` and skipped), 2 when an input file is missing or not the expected
JSON shape, or an argument is invalid (`{"resume": [], "error": …}`).

A session is a candidate when either signal holds:

  * text: `post_turn_summary.status_detail` carries the usage-limit or
    account rate-limit error text ("You've hit your session limit …");
  * rate_limit_info (checker sessions only): its
    `external_metadata.rate_limit_info.status` is `rejected` and its
    `resetsAt` has passed, its `status_category` is not `need_input`, and no
    enabled trigger is bound to it. A checker whose chain died this way can
    still show a healthy summary. Stage sessions are left out: a turn that
    completed on overage also records `rejected`.

A candidate is skipped (listed under `skipped`) when it is the pickup
(`pickup`), archived (`archived`), not IDLE (`not_idle:<status>`), waiting
on a permission prompt (`permission_prompt`), waiting on a human answer
(`needs_input`, rate_limit_info signal only), still limited by its own
`rate_limit_info` (`not_reset`), or bound to an enabled trigger that will
wake it anyway (`wake_pending`: for a checker, any, because its triggers
are its own check-ins and one means its chain is alive; for another
session, one due within 30 minutes, overdue, or with an unreadable time,
because a stage session keeps a 7-day hand-back Routine). A pending
`Resume after usage limit (…)` trigger counts whatever its time, so a session
is never resumed twice for the same stop. A trigger's time is its
`next_run_at`, else its `run_once_at`. A resume trigger that has fired is
disabled and no longer counts: when the resumed turn fails on the limit or
on a rate limit again, the next wake picks the session again.

Selected sessions are ordered checkers first (title contains `— checker` or
`status check-in`), then oldest `updated_at`. The first `limit` go to
`resume`; the rest go to `pending` for the next wake.

Fire spacing: waking every stopped session at once makes the resumed turns
fail again ("Server is temporarily limiting requests", or the usage limit
itself; issue #5660, 2026-09-30 16:05Z). Each `resume` entry therefore
carries `fire_offset_minutes`: 2 for the first 4, then 3 minutes later for
each further group of 4 (2, 2, 2, 2, 5, 5, 5, 5, 8, …), in `resume` order,
so checkers fire first. The pickup sets each trigger's `run_once_at` to the
time it creates the trigger plus that offset.

Hold-off: `allowed` and `allowed_warning` count as allowed. A `rate_limit_info`
with any other status holds while its `resetsAt` is still in the future. When
the pickup's own entry holds, `not_reset` is true, `resume` is empty, and
every selected session goes to `pending`.

Every resume prompt is fixed text plus the login. No session title or
summary, which other sessions wrote, ever reaches a prompt.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

DEFAULT_RESUME_LIMIT = 20
RESUME_LIMIT_MIN = 1
RESUME_LIMIT_MAX = 40
RESUME_LIMIT_ENV = "CLAUDE_USAGE_LIMIT_RESUME_LIMIT"
WAKE_WINDOW_MINUTES = 30
RESUME_FIRE_FIRST_OFFSET_MINUTES = 2
RESUME_FIRE_GROUP_SIZE = 4
RESUME_FIRE_GROUP_SPACING_MINUTES = 3
RESUME_TRIGGER_PREFIX = "Resume after usage limit"
ALLOWED_RATE_LIMIT_STATUSES = frozenset({"allowed", "allowed_warning"})
SESSION_STATUS_PREFIX = "SESSION_STATUS_"
IDLE_STATUS = "SESSION_STATUS_IDLE"
ARCHIVED_STATUS = "SESSION_STATUS_ARCHIVED"
CHECKER_TITLE_MARKERS = ("— checker", "status check-in")
MAX_JSON_START_CANDIDATES = 50

LIMIT_TEXT_PATTERNS = (
	re.compile(r"\byou(?:'|’)ve hit your\b[^.\n]{0,40}?\blimit\b", re.IGNORECASE),
	re.compile(r"\busage limit reached\b", re.IGNORECASE),
	re.compile(r"\bAPI Error:?\s*429\b", re.IGNORECASE),
	re.compile(r"\brate_limit_error\b", re.IGNORECASE),
	re.compile(r"\bAPI Error\b[^\n]{0,80}?\brate[ _-]?limit", re.IGNORECASE),
)
PERMISSION_PROMPT_PATTERN = re.compile(r"^\s*approve or deny\b|\bwaiting on permission\b", re.IGNORECASE)
ISSUE_TITLE_PATTERN = re.compile(r"^#(\d+) · ")
PR_TITLE_PATTERN = re.compile(r"\bPR #(\d+)\b")
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")

SIGNAL_REASONS = {
	"text": "this session's last turn stopped with the account usage-limit error.",
	"rate_limit_info": "this session's last turn ran while the account was over its usage limit, and nothing is scheduled to wake it.",
}

CHECKER_PROMPT = (
	"Resume after usage limit (issue #5660): {why} The limit has reset. "
	"Repeat the steps in your most recent checker-instructions message now, starting at step 1 "
	"(for a `PR #<n> status check-in` checker: your instructions message together with every later subscriber message). "
	"Before any step creates a session, call list_sessions (mine: true, limit: 100), and repeat it with after_id = the "
	"previous page's last_id while has_more is true and that page's oldest session was created after your most recent "
	"checker-instructions message, at most 5 pages. If a session that is not archived has exactly the title that step "
	"would use, was created after that message, and has this repository as its source (its session_context.sources; "
	"call get_session on it when the listing omits them), the step already ran before the limit stopped you: do not "
	"create another, treat that session as the one the step created, and continue as the step says after creating it. "
	"Wherever those instructions run check_in_status.py, set CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN={login} (never empty). "
	"If a delete_trigger or archive_session call is refused, skip it and do not retry (#5068). "
	"Edit files with the Edit or Write tools, never with python3 heredocs (#4858)."
)

OTHER_PROMPT = (
	"Resume after usage limit (issue #5660): {why} The limit has reset. "
	"Continue from your latest instructions. First re-read the current state: `git status -sb` and your branch, "
	"the pull request or issue you were working on, and the progress log under docs/implement-plan/ when your task has one. "
	"Do not redo work that already landed, and before you create a session, trigger, pull request, or comment, "
	"check that it does not exist already. "
	"Use CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN={login} (never empty) in every check_in_status.py call you run and every "
	"checker-instructions message you write. "
	"If a delete_trigger or archive_session call is refused, skip it and do not retry (#5068). "
	"Edit files with the Edit or Write tools, never with python3 heredocs (#4858)."
)


class InputError(Exception):
	"""An input file or argument is unusable; the script exits 2."""


def resolve_resume_limit(cli_value: int | None, env_value: str | None) -> int:
	"""Return the per-wake cap: `--limit` wins, else the env var, else 20; clamped to 1..40."""
	if cli_value is not None:
		value = cli_value
	else:
		try:
			value = int((env_value or "").strip())
		except ValueError:
			value = DEFAULT_RESUME_LIMIT
	return max(RESUME_LIMIT_MIN, min(RESUME_LIMIT_MAX, value))


def _decode_json_text(text: str, path: str) -> object:
	"""Decode the first JSON object or array in `text`, skipping an envelope around it."""
	positions = [index for index, char in enumerate(text) if char in "{["][:MAX_JSON_START_CANDIDATES]
	decoder = json.JSONDecoder()
	for index in positions:
		try:
			payload, _ = decoder.raw_decode(text, index)
		except ValueError:
			continue
		if isinstance(payload, (dict, list)):
			return payload
	raise InputError(f"{path}: no JSON object or array found")


def _load_entries(path: str) -> list:
	"""Return the `data` entries of one saved list result."""
	try:
		with open(path, encoding="utf-8") as handle:
			text = handle.read()
	except OSError as exc:
		raise InputError(f"cannot read {path}: {exc}") from exc
	payload = _decode_json_text(text, path)
	if isinstance(payload, dict) and isinstance(payload.get("ccr"), dict):
		payload = payload["ccr"]
	entries = payload.get("data") if isinstance(payload, dict) else payload
	if not isinstance(entries, list):
		raise InputError(f"{path}: expected a list result with a `data` array")
	return entries


def _parse_time(value: object) -> dt.datetime | None:
	"""Parse an RFC 3339 string or epoch seconds (or milliseconds) as an aware UTC datetime."""
	if isinstance(value, bool):
		return None
	if isinstance(value, (int, float)):
		seconds = value / 1000 if value > 1e12 else value
		try:
			return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
		except (OverflowError, OSError, ValueError):
			return None
	if isinstance(value, str) and value.strip():
		text = value.strip()
		if re.fullmatch(r"\d+(?:\.\d+)?", text):
			return _parse_time(float(text))
		try:
			parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
		except ValueError:
			return None
		return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
	return None


def _rate_limit_info(session: dict) -> dict:
	metadata = session.get("external_metadata")
	info = metadata.get("rate_limit_info") if isinstance(metadata, dict) else None
	return info if isinstance(info, dict) else {}


def _rate_limit_status(info: dict) -> str:
	status = info.get("status")
	return status.strip().lower() if isinstance(status, str) else ""


def limit_holds(info: dict, now: dt.datetime) -> bool:
	"""True while a limited `rate_limit_info` has a `resetsAt` still in the future."""
	status = _rate_limit_status(info)
	if not status or status in ALLOWED_RATE_LIMIT_STATUSES:
		return False
	resets_at = _parse_time(info.get("resetsAt"))
	return resets_at is not None and resets_at > now


def _summary(session: dict) -> dict:
	summary = session.get("post_turn_summary")
	if not isinstance(summary, dict):
		metadata = session.get("external_metadata")
		summary = metadata.get("post_turn_summary") if isinstance(metadata, dict) else None
	return summary if isinstance(summary, dict) else {}


def _text(value: object) -> str:
	return value if isinstance(value, str) else ""


def has_limit_text(detail: str) -> bool:
	"""True when a `status_detail` carries the usage-limit or account rate-limit error text."""
	return any(pattern.search(detail) for pattern in LIMIT_TEXT_PATTERNS)


def session_kind(title: str) -> str:
	return "checker" if any(marker in title for marker in CHECKER_TITLE_MARKERS) else "other"


def session_issue(title: str) -> int | None:
	match = ISSUE_TITLE_PATTERN.match(title)
	return int(match.group(1)) if match else None


def resume_trigger_name(session_id: str, title: str) -> str:
	"""`Resume after usage limit (#<N>)`, else `(PR #<n>)`, else the session id's last 8 characters."""
	issue = session_issue(title)
	if issue is not None:
		return f"{RESUME_TRIGGER_PREFIX} (#{issue})"
	pr = PR_TITLE_PATTERN.search(title)
	if pr:
		return f"{RESUME_TRIGGER_PREFIX} (PR #{pr.group(1)})"
	return f"{RESUME_TRIGGER_PREFIX} ({session_id[-8:]})"


def resume_prompt(kind: str, signal: str, login: str) -> str:
	template = CHECKER_PROMPT if kind == "checker" else OTHER_PROMPT
	return template.format(why=SIGNAL_REASONS[signal], login=login)


def fire_offset_minutes(position: int) -> int:
	"""Minutes after its creation that the `position`-th resume trigger (0-based) fires: 4 every 3 minutes, from 2."""
	return RESUME_FIRE_FIRST_OFFSET_MINUTES + RESUME_FIRE_GROUP_SPACING_MINUTES * (position // RESUME_FIRE_GROUP_SIZE)


def _bound_wakes(triggers: list, errors: list) -> dict:
	"""Map session id → `(wake time, is a resume trigger)` for every enabled trigger bound to it.

	The wake time is `next_run_at`, else `run_once_at`, and None when neither is readable.
	"""
	wakes: dict = {}
	for position, trigger in enumerate(triggers):
		if not isinstance(trigger, dict):
			errors.append(f"trigger entry {position}: not an object")
			continue
		if trigger.get("enabled") is False or trigger.get("ended_reason"):
			continue
		session_id = trigger.get("persistent_session_id")
		if not isinstance(session_id, str) or not session_id:
			continue
		is_resume = _text(trigger.get("name")).startswith(RESUME_TRIGGER_PREFIX)
		wake_time = _parse_time(trigger.get("next_run_at") or trigger.get("run_once_at"))
		wakes.setdefault(session_id, []).append((wake_time, is_resume))
	return wakes


def _wake_pending(next_runs: list, kind: str, now: dt.datetime) -> bool:
	"""True when a bound trigger will wake the session anyway.

	Any enabled trigger counts for a checker: its triggers are its own
	check-ins (at most an hour out), so one means its chain is alive and a
	resume would start a second chain. Other sessions count a pending resume
	trigger whatever its time (fire spacing can put one more than 30 minutes
	out), and any other trigger only when it is due within 30 minutes: a
	stage session keeps a 7-day hand-back Routine.
	"""
	if not next_runs:
		return False
	if kind == "checker":
		return True
	window_end = now + dt.timedelta(minutes=WAKE_WINDOW_MINUTES)
	return any(is_resume or next_run is None or next_run <= window_end for next_run, is_resume in next_runs)


def _signal(session: dict, kind: str, next_runs: list, now: dt.datetime) -> str | None:
	summary = _summary(session)
	if has_limit_text(_text(summary.get("status_detail"))):
		return "text"
	info = _rate_limit_info(session)
	if kind == "checker" and _rate_limit_status(info) == "rejected" and not next_runs:
		resets_at = _parse_time(info.get("resetsAt"))
		if resets_at is not None and resets_at <= now:
			return "rate_limit_info"
	return None


def _skip_reason(session: dict, kind: str, signal: str, pickup_session: str, next_runs: list, now: dt.datetime) -> str | None:
	summary = _summary(session)
	status = _text(session.get("session_status"))
	if session.get("id") == pickup_session:
		return "pickup"
	if status == ARCHIVED_STATUS:
		return "archived"
	if status != IDLE_STATUS:
		short = status[len(SESSION_STATUS_PREFIX):] if status.startswith(SESSION_STATUS_PREFIX) else status
		return f"not_idle:{short.lower() or 'unknown'}"
	if PERMISSION_PROMPT_PATTERN.search(_text(summary.get("needs_action"))) or PERMISSION_PROMPT_PATTERN.search(
		_text(summary.get("status_detail"))
	):
		return "permission_prompt"
	if signal == "rate_limit_info" and _text(summary.get("status_category")) == "need_input":
		return "needs_input"
	if limit_holds(_rate_limit_info(session), now):
		return "not_reset"
	if _wake_pending(next_runs, kind, now):
		return "wake_pending"
	return None


def select(sessions: list, triggers: list, pickup_session: str, login: str, limit: int, now: dt.datetime) -> dict:
	"""Pick the sessions to resume this wake; see the module docstring for the rules."""
	errors: list[str] = []
	wakes = _bound_wakes(triggers, errors)
	seen: set[str] = set()
	selected: list[tuple] = []
	skipped: list[dict] = []
	account_holds = False
	far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
	for position, session in enumerate(sessions):
		if not isinstance(session, dict):
			errors.append(f"session entry {position}: not an object")
			continue
		session_id = session.get("id")
		if not isinstance(session_id, str) or not session_id:
			errors.append(f"session entry {position}: missing id")
			continue
		if session_id in seen:
			continue
		seen.add(session_id)
		if session_id == pickup_session:
			account_holds = limit_holds(_rate_limit_info(session), now)
		title = _text(session.get("title"))
		kind = session_kind(title)
		next_runs = wakes.get(session_id, [])
		signal = _signal(session, kind, next_runs, now)
		if signal is None:
			continue
		reason = _skip_reason(session, kind, signal, pickup_session, next_runs, now)
		if reason:
			skipped.append({"session_id": session_id, "signal": signal, "reason": reason})
			continue
		updated_at = _parse_time(session.get("updated_at")) or far_future
		selected.append((0 if kind == "checker" else 1, updated_at, session_id, kind, title, signal))
	selected.sort(key=lambda item: item[:3])
	resume: list[dict] = []
	pending: list[dict] = []
	for _, _, session_id, kind, title, signal in selected:
		issue = session_issue(title)
		if account_holds or len(resume) >= limit:
			pending.append({"session_id": session_id, "kind": kind, "issue": issue})
			continue
		resume.append(
			{
				"session_id": session_id,
				"kind": kind,
				"issue": issue,
				"signal": signal,
				"trigger_name": resume_trigger_name(session_id, title),
				"fire_offset_minutes": fire_offset_minutes(len(resume)),
				"prompt": resume_prompt(kind, signal, login),
			}
		)
	return {
		"resume": resume,
		"pending": pending,
		"skipped": skipped,
		"not_reset": account_holds,
		"limit": limit,
		"considered": len(seen),
		"errors": errors,
	}


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--sessions", action="append", required=True, help="saved list_sessions result file (repeat per page)")
	parser.add_argument("--triggers", action="append", required=True, help="saved list_triggers result file (enabled: true; repeat per page)")
	parser.add_argument("--pickup-session", required=True, help="the pickup's own session id")
	parser.add_argument("--handoff-author-login", required=True, help="CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN for the resume prompts")
	parser.add_argument("--limit", type=int, default=None, help=f"per-wake cap (default {RESUME_LIMIT_ENV} or {DEFAULT_RESUME_LIMIT}, clamped to {RESUME_LIMIT_MIN}..{RESUME_LIMIT_MAX})")
	return parser


def main(argv: list[str] | None = None, now: dt.datetime | None = None, environ: dict | None = None) -> int:
	args = build_parser().parse_args(argv)
	environ = os.environ if environ is None else environ
	try:
		login = args.handoff_author_login.strip()
		if not LOGIN_PATTERN.fullmatch(login):
			raise InputError("--handoff-author-login must be a GitHub login (never empty)")
		pickup_session = args.pickup_session.strip()
		if not pickup_session:
			raise InputError("--pickup-session must not be empty")
		sessions = [entry for path in args.sessions for entry in _load_entries(path)]
		triggers = [entry for path in args.triggers for entry in _load_entries(path)]
	except InputError as exc:
		print(json.dumps({"resume": [], "error": str(exc)}))
		return 2
	limit = resolve_resume_limit(args.limit, environ.get(RESUME_LIMIT_ENV))
	now = now or dt.datetime.now(dt.timezone.utc)
	print(json.dumps(select(sessions, triggers, pickup_session, login, limit, now), ensure_ascii=False))
	return 0


if __name__ == "__main__":
	sys.exit(main())

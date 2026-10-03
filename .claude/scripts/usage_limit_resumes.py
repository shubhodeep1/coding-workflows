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
                    `{"data": [...]}`, and a bare array are accepted too, and
                    so is one `get_session` result (`{"ccr": {"id": …}}`).
                    The pickup passes its own `get_session` result first:
                    it runs for weeks, so its own entry is rarely on the
                    3-day listing, and the first entry for an id wins.
  --triggers FILE   (repeatable) a saved `list_triggers` result
                    (`enabled: true`), in the same forms.
  --pickup-session  the pickup's own session id: never selected, and its own
                    `rate_limit_info` is the account-wide hold-off.
  --handoff-author-login
                    the CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN value every resume
                    prompt names (required, never empty).
  --limit N         per-wake cap; default CLAUDE_USAGE_LIMIT_RESUME_LIMIT
                    (20), clamped to 1..40.
  --self-repo       the pickup's own `<owner>/<repo>`; default
                    `shubhodeep1/coding-workflows`.
  --registry FILE   the consumer-repo registry (a JSON array of
                    `<owner>/<repo>`); default `.github/ai/consumer_repos.json`,
                    relative to the working directory (the repository root,
                    where the pickup runs). Unreadable or not an array → only
                    `--self-repo` is authorized, and `errors` says so.

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
    `resetsAt` has passed, the `status_category` of neither summary copy is
    `need_input`, and no enabled trigger is bound to it. A checker whose
    chain died this way can still show a healthy summary. Stage sessions are left out: a turn that
    completed on overage also records `rejected`.

Authorization (issue #6101): `list_sessions` with `mine: true` is
account-wide, so a candidate is resumed only when server-set fields place it
in one of the pickup's registered workflows. All three must hold, or it is
skipped with the first reason that applies:

  * repository: it has at least one GitHub source
    (`session_context.sources[].git_repository.url`), else `no_repo`, and
    every one is `--self-repo` or a `--registry` repository (compared
    case-insensitively), else `foreign_repo`;
  * origin: its `origin` is `claude_code_mcp_seed` (started by another
    session with `create_session`), else `unknown_origin`. A session a person
    opened in the app (`desktop_app`, …) is never resumed;
  * lineage: it carries a `parent_session_id` of the form `session_<x>` or
    `cse_<x>`, else `no_lineage`.

Anything the script cannot read fails closed. The parent chain is not required
to reach the pickup: chains are often rooted at an operator's session or at a
pickup since restarted, and ancestors older than the listing are not on it.

A candidate is skipped (listed under `skipped`) when it is the pickup
(`pickup`), not authorized (the reasons above), archived (`archived`), created more than 72 hours ago
(`too_old`: the pickup lists 3 days of sessions, but the last page it reads
can reach further back, and those older sessions stay with the manual
fallback; an unreadable `created_at` is not skipped; an old session waiting
on a human answer is listed as `needs_input` instead, and one on a permission
prompt as `permission_prompt`, so the manual fallback never resumes it past
the question), not IDLE (`not_idle:<status>`), waiting
on a permission prompt (`permission_prompt`: a leading "Approve or deny" or
"waiting on permission" in the `needs_action` or `status_detail` of either
summary copy, `post_turn_summary` or `external_metadata.post_turn_summary`),
waiting on a human answer (`needs_input`, below), still limited by its own
`rate_limit_info` (`not_reset`), or bound to an enabled trigger that will
wake it anyway (`wake_pending`: for a checker, any, because its triggers
are its own check-ins and one means its chain is alive; for another
session, one due within 30 minutes, overdue, or with an unreadable time,
because a stage session keeps a 7-day hand-back Routine). A pending
`Resume after usage limit (…)` trigger counts whatever its time, so a session
is never resumed twice for the same stop. A trigger's time is its
`next_run_at` when readable, else its `run_once_at`. A resume trigger that has fired is
disabled and no longer counts: when the resumed turn fails on the limit or
on a rate limit again, the next wake picks the session again.

`needs_input` (issue #6102) holds on both signals when the summary still
records a request to a human: a non-empty `needs_action`, in
`post_turn_summary` or `external_metadata.post_turn_summary`, that is not
only a wait for the usage limit. A limit wait is made only of limit-wait
words (`LIMIT_WAIT_VOCABULARY`: the usage-limit error wording, wait words,
times, and connectives; any other word, such as merge or push, makes it a
request, and so does "now"), it names the limit or its reset, and every
sentence in it matches one of a closed set of forms whole: the error or
status wording ("Usage limit reached", "Rate limited until 5pm"), the reset
("The limit resets at 11am"), a wait ("Wait until the limit resets, then
retry"), a retry tied to the reset in the same sentence ("Retry after the
reset", "Once the limit resets, retry"), or "then retry" right after a wait
or reset sentence. A wait or a retry takes a time only as the reset's ("Retry
after the reset at 5pm"), so "Retry after 5pm" and "Wait until Monday" are
requests. No form holds a negation or a "retry until", so "Usage
limit reached. Please retry the request. The limit resets later." and
"Retry the request, not waiting until the reset" are requests; a `needs_action` with a Q-ID (`Q1`), a question mark,
or the words reply, answer, decide, confirm, choose, or approve is never
one. Unknown
wording is a request, so the session stays stopped and is listed. On the
rate_limit_info signal a `need_input` category holds too. On the text
signal a `need_input` category with an empty `needs_action` does not: a
turn that failed on the limit can show it, and the error text decides.
Every resume prompt also says it is not an answer to a pending question.

Session ids are compared in one form: `cse_<x>` and `session_<x>` name the
same session (a trigger's `persistent_session_id` can carry either), so a
trigger bound by either form counts as that session's wake, and the pickup is
recognised by either form.

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
import itertools
import json
import os
import re
import sys

DEFAULT_RESUME_LIMIT = 20
RESUME_LIMIT_MIN = 1
RESUME_LIMIT_MAX = 40
RESUME_LIMIT_ENV = "CLAUDE_USAGE_LIMIT_RESUME_LIMIT"
WAKE_WINDOW_MINUTES = 30
SESSION_WINDOW_HOURS = 72
RESUME_FIRE_FIRST_OFFSET_MINUTES = 2
RESUME_FIRE_GROUP_SIZE = 4
RESUME_FIRE_GROUP_SPACING_MINUTES = 3
RESUME_TRIGGER_PREFIX = "Resume after usage limit"
# The same shape stale_routines.py's USAGE_LIMIT_RESUME_NAME_PATTERN deletes, so a
# hand-named trigger the sweep never cleans up never counts as a resume either.
RESUME_TRIGGER_NAME_PATTERN = re.compile(r"^Resume after usage limit \(")
ALLOWED_RATE_LIMIT_STATUSES = frozenset({"allowed", "allowed_warning"})
SESSION_STATUS_PREFIX = "SESSION_STATUS_"
IDLE_STATUS = "SESSION_STATUS_IDLE"
ARCHIVED_STATUS = "SESSION_STATUS_ARCHIVED"
CHECKER_TITLE_MARKERS = ("— checker", "status check-in")
MAX_JSON_START_CANDIDATES = 50
# Server-set `origin` of a session another session started with create_session
# (issue #6101): the only origin the pickup's workflows produce.
AUTHORIZED_ORIGINS = frozenset({"claude_code_mcp_seed"})
DEFAULT_SELF_REPO = "shubhodeep1/coding-workflows"
DEFAULT_REGISTRY_PATH = ".github/ai/consumer_repos.json"
_REPO_SLUG = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
# The URL shape claude_session_janitor.py's SOURCE_URL_PATTERN accepts.
SOURCE_URL_PATTERN = re.compile(rf"^https://github\.com/(?P<repo>{_REPO_SLUG}?)(?:\.git)?/?$")
REPO_SLUG_PATTERN = re.compile(rf"^{_REPO_SLUG}$")

LIMIT_TEXT_PATTERNS = (
	re.compile(r"\byou(?:'|’)ve hit your\b[^.\n]{0,40}?\blimit\b", re.IGNORECASE),
	re.compile(r"\busage limit reached\b", re.IGNORECASE),
	re.compile(r"\bAPI Error:?\s*429\b", re.IGNORECASE),
	re.compile(r"\brate_limit_error\b", re.IGNORECASE),
	re.compile(r"\bAPI Error\b[^\n]{0,80}?\brate[ _-]?limit", re.IGNORECASE),
)
PERMISSION_PROMPT_PATTERN = re.compile(r"^\s*approve or deny\b|\bwaiting on permission\b", re.IGNORECASE)
# A `needs_action` that only waits for the usage limit (issue #6102): a limit
# plus a wait word, and nothing that asks a human for an answer.
LIMIT_WORD_PATTERN = re.compile(r"\blimits?\b", re.IGNORECASE)
HUMAN_REQUEST_PATTERN = re.compile(
	r"\bQ\d+\b|\?|\b(?:repl\w*|answer\w*|decid\w*|decision\w*|confirm\w*|choos\w*|choice\w*|approv\w*)\b",
	re.IGNORECASE,
)
# PR #6112 review round 1: a limit wait is made only of these words, so a request
# riding on the error text ("Usage limit reached — merge PR #N") fails closed.
# Words are runs of letters (any script, apostrophes kept) and runs of digits;
# digits always pass (times, dates, error codes), symbols are not words.
LIMIT_WAIT_TOKEN_PATTERN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*|\d+")
LIMIT_WAIT_VOCABULARY = frozenset(
	(
		# the usage-limit and rate-limit error wording (LIMIT_TEXT_PATTERNS and the summaries it matches)
		"you", "you've", "hit", "your", "claude", "ai", "api", "error", "type", "message", "number", "of",
		"request", "requests", "tokens", "has", "exceeded", "server", "is", "temporarily", "limiting", "not",
		"limited", "reached", "usage", "limit", "limits", "limit's", "rate", "session", "account", "weekly",
		"daily", "hourly", "monthly", "hour", "hours", "opus", "sonnet", "model",
		# waiting for it ("now" is not one: PR #6112 review round 2, acting now is not waiting)
		"wait", "waiting", "retry", "retrying", "resume", "resuming", "resumes", "resend", "try", "again",
		"reset", "resets", "resetting", "please", "later", "soon", "automatically", "last",
		# connectives
		"the", "a", "an", "to", "for", "until", "till", "after", "once", "then", "when", "it", "its", "it's",
		"are", "have", "been", "be", "will", "should", "at", "on", "in", "by", "and", "next", "about",
		"around", "approximately",
		# times and dates
		"am", "pm", "utc", "gmt", "z", "t", "h", "m", "min", "mins", "minute", "minutes", "sec", "secs",
		"second", "seconds", "time", "local", "window", "today", "tomorrow", "tonight", "noon", "midnight",
		"january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
		"november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
		"nov", "dec", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "mon",
		"tue", "tues", "wed", "thu", "thurs", "fri", "sat", "sun",
	)
)
# PR #6112 review round 3: a word-level search for a deferral word could not tie it to the retry it defers
# (a deferral in another sentence, "retry until the reset", "not waiting until"). A limit wait is therefore a
# run of sentences, each of which matches one of the closed forms below whole; any other sentence makes the
# text a request. Sentences end at . ! ; | · • — – or a line break; inside one, the words are matched as one
# lowercase string with every time word and number run collapsed to `#`.
LIMIT_WAIT_SENTENCE_BREAK_PATTERN = re.compile(r"\.(?!\d)|[!;|\n·•—–]|\s-\s")
LIMIT_WAIT_TIME_WORDS = frozenset(
	(
		"am", "pm", "utc", "gmt", "z", "t", "h", "m", "min", "mins", "minute", "minutes", "sec", "secs", "second",
		"seconds", "hour", "hours", "local", "time", "today", "tomorrow", "tonight", "noon", "midnight",
		"january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
		"november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
		"nov", "dec", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "mon",
		"tue", "tues", "wed", "thu", "thurs", "fri", "sat", "sun",
	)
)


def _limit_wait_forms() -> tuple:
	"""Build the closed sentence forms of a limit wait: `(status, reset, wait, retry, then)` full-match patterns."""
	limit = r"(?:(?:claude|ai|usage|session|weekly|daily|hourly|monthly|account|rate|opus|sonnet|api|model) ){0,3}limits?"
	the_limit = rf"(?:(?:the|your) )?{limit}"
	time = r"(?:(?:at|on|in|by|around|about|approximately|next) )*#(?: (?:on|at|in|by|and) #)*"
	resets = r"(?:resets|will reset|has reset|has been reset|is reset|resetting)"
	reset_event = rf"(?:(?:{the_limit}|it) {resets}|(?:the|your) (?:{limit} )?reset)(?: {time})?"
	again = r"(?:it |(?:the|your) request )?again"
	retry = (
		rf"(?:(?:retry|resend|resume)(?: (?:it|(?:the|your) (?:last )?(?:request|requests|message|session)))?(?: again)?"
		rf"|try {again})"
	)
	please = r"(?:please )?"
	hit = rf"(?:you've|you have) hit (?:your|the) {limit}(?: (?:resets|will reset)(?: {time})?)?"
	reached = rf"{the_limit} (?:has been |is |has )?(?:reached|hit|exceeded)(?: (?:resets|will reset)(?: {time})?)?"
	server = r"server is temporarily limiting requests(?: not your usage limit)?"
	status = (
		hit,
		reached,
		server,
		r"rate limit error",
		rf"(?:(?:you are|you have been|you've been|it is|it's|(?:the|your) (?:account|session) is) )?(?:rate )?limited"
		rf"(?: (?:until|till) (?:{reset_event}|{time}))?",
		rf"api error(?: #)?(?: type error error type rate limit error message)?"
		rf"(?: (?:{reached}|rate limit error|number of request tokens has exceeded {the_limit}|{server}))?",
	)
	reset = (rf"(?:(?:{the_limit}|it) )?{resets}(?: (?:automatically|soon|later))?(?: {time})?",)
	# PR #6112 review round 4: a wait or a retry names the reset or the limit, never a bare time. The selector
	# cannot tell "retry after 5pm" or "wait until Monday" from a human's own deadline, and a resume fires two
	# minutes after the pickup creates it, so a time is accepted only as the reset's ("after the reset at 5pm").
	wait_for = rf"(?:(?:for|until|till) (?:{the_limit} to reset|{reset_event}|{the_limit})|on {the_limit})"
	wait = (rf"{please}(?:wait|waiting) {wait_for}(?: {time})?(?: (?:and |then |and then ){retry})?",)
	retry_forms = (
		rf"{please}{retry} (?:(?:after|once|when) {reset_event}|later)",
		rf"{please}(?:after|once|when) {reset_event}(?: then)? {retry}",
	)
	# Only after a wait or reset sentence, whose reset the "then" points to.
	then = (rf"{please}(?:then {retry}|{retry} then)",)
	return tuple(
		tuple(re.compile(form) for form in forms) for forms in (status, reset, wait, retry_forms, then)
	)


(
	LIMIT_WAIT_STATUS_FORMS,
	LIMIT_WAIT_RESET_FORMS,
	LIMIT_WAIT_WAIT_FORMS,
	LIMIT_WAIT_RETRY_FORMS,
	LIMIT_WAIT_THEN_FORMS,
) = _limit_wait_forms()
LIMIT_WAIT_NAMES_LIMIT_PATTERN = re.compile(r"\b(?:reset\w*|limited)\b", re.IGNORECASE)
# One session has two id forms: `session_<x>` (list_sessions, get_session) and
# `cse_<x>` (seen as a trigger's `persistent_session_id`, 2026-10-02).
RESUME_SESSION_ID_PATTERN = re.compile(r"^(?:session|cse)_(?P<suffix>[A-Za-z0-9]+)$")
ISSUE_TITLE_PATTERN = re.compile(r"^#(\d+) · ")
PR_TITLE_PATTERN = re.compile(r"\bPR #(\d+)\b")
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")

SIGNAL_REASONS = {
	"text": "this session's last turn stopped with the account usage-limit error.",
	"rate_limit_info": "this session's last turn ran while the account was over its usage limit, and nothing is scheduled to wake it.",
}

# Issue #6102: a resume is never the answer a stopped session may be waiting for.
NOT_AN_ANSWER_TEXT = (
	"This message is not an answer to any question or approval request of yours: if one is still "
	"unanswered, keep waiting for the human's answer and end the turn without acting on it. "
)

CHECKER_PROMPT = (
	"Resume after usage limit (issue #5660): {why} The limit has reset. "
	+ NOT_AN_ANSWER_TEXT
	+ "Repeat the steps in your most recent checker-instructions message now, starting at step 1 "
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
	+ NOT_AN_ANSWER_TEXT
	+ "Continue from your latest instructions. First re-read the current state: `git status -sb` and your branch, "
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
	starts = (index for index, char in enumerate(text) if char in "{[")
	positions = list(itertools.islice(starts, MAX_JSON_START_CANDIDATES))
	decoder = json.JSONDecoder()
	for index in positions:
		try:
			payload, _ = decoder.raw_decode(text, index)
		except ValueError:
			continue
		if isinstance(payload, (dict, list)):
			return payload
	if len(positions) == MAX_JSON_START_CANDIDATES:
		raise InputError(
			f"{path}: no JSON object or array found in the first {MAX_JSON_START_CANDIDATES} "
			"`{`/`[` start positions; later positions were not tried"
		)
	raise InputError(f"{path}: no JSON object or array found")


def _load_entries(path: str) -> list:
	"""Return the `data` entries of one saved list result, or a one-entry list for one `get_session` result."""
	try:
		with open(path, encoding="utf-8") as handle:
			text = handle.read()
	except OSError as exc:
		raise InputError(f"cannot read {path}: {exc}") from exc
	payload = _decode_json_text(text, path)
	if isinstance(payload, dict) and isinstance(payload.get("ccr"), dict):
		payload = payload["ccr"]
	if isinstance(payload, dict) and "data" not in payload and isinstance(payload.get("id"), str):
		return [payload]
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


def session_key(session_id: str) -> str:
	"""Compare form of a session id: `cse_<x>` and `session_<x>` both become `session_<x>`; other ids stay as given."""
	match = RESUME_SESSION_ID_PATTERN.match(session_id.strip())
	return f"session_{match.group('suffix')}" if match else session_id.strip()


def load_allowed_repos(registry_path: str, self_repo: str, errors: list) -> frozenset:
	"""The lowercased `<owner>/<repo>` slugs a resumed session may work in: the registry plus `self_repo`.

	An unreadable registry, or one that is not a JSON array, authorizes `self_repo` alone and adds one
	line to `errors`; entries that are not slugs are skipped. The set only ever narrows on failure.
	"""
	allowed = {self_repo.lower()}
	try:
		with open(registry_path, encoding="utf-8") as handle:
			data = json.load(handle)
	except (OSError, ValueError) as exc:
		errors.append(f"registry {registry_path}: unreadable ({exc}); only {self_repo} is authorized")
		return frozenset(allowed)
	if not isinstance(data, list):
		errors.append(f"registry {registry_path}: not a JSON array; only {self_repo} is authorized")
		return frozenset(allowed)
	allowed.update(slug.strip().lower() for slug in data if isinstance(slug, str) and REPO_SLUG_PATTERN.fullmatch(slug.strip()))
	return frozenset(allowed)


def session_repos(session: dict) -> list[str] | None:
	"""Lowercased `<owner>/<repo>` of each GitHub source in `session_context.sources`.

	None when a source is not an object, or a `git_repository` source is null or has a URL that is not a
	GitHub repository URL, so an unreadable source fails closed instead of being ignored. A source of
	another kind (an object with no `git_repository` key) is not a repository and is skipped.
	"""
	context = session.get("session_context")
	sources = context.get("sources") if isinstance(context, dict) else None
	repos: list[str] = []
	for source in sources if isinstance(sources, list) else []:
		if not isinstance(source, dict):
			return None
		if "git_repository" not in source:
			continue
		repository = source["git_repository"]
		url = repository.get("url") if isinstance(repository, dict) else None
		match = SOURCE_URL_PATTERN.match(url.strip()) if isinstance(url, str) else None
		if not match:
			return None
		repos.append(match.group("repo").lower())
	return repos


def _unauthorized_reason(session: dict, allowed_repos: frozenset | None) -> str | None:
	"""Why the session is outside the pickup's registered workflows (issue #6101), or None when it is inside.

	`allowed_repos` None authorizes nothing, so a caller that omits it fails closed.
	"""
	repos = session_repos(session)
	if not repos:
		return "no_repo" if repos == [] else "foreign_repo"
	if allowed_repos is None or any(repo not in allowed_repos for repo in repos):
		return "foreign_repo"
	if _text(session.get("origin")).strip() not in AUTHORIZED_ORIGINS:
		return "unknown_origin"
	if not RESUME_SESSION_ID_PATTERN.match(_text(session.get("parent_session_id")).strip()):
		return "no_lineage"
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


def _summary_copies(session: dict) -> list[dict]:
	"""Both summary copies that are objects: `post_turn_summary`, then `external_metadata.post_turn_summary`."""
	summaries = [session.get("post_turn_summary")]
	metadata = session.get("external_metadata")
	if isinstance(metadata, dict):
		summaries.append(metadata.get("post_turn_summary"))
	return [summary for summary in summaries if isinstance(summary, dict)]


def _needs_action_texts(session: dict) -> list[str]:
	"""The stripped, non-empty `needs_action` of both summary copies (top-level and `external_metadata`)."""
	texts = []
	for summary in _summary_copies(session):
		text = _text(summary.get("needs_action")).strip()
		if text:
			texts.append(text)
	return texts


def is_limit_wait(needs_action: str) -> bool:
	"""True when a `needs_action` only waits for the usage limit and asks a human for nothing (issue #6102)."""
	if HUMAN_REQUEST_PATTERN.search(needs_action):
		return False
	normalized = needs_action.lower().replace("’", "'")
	words = LIMIT_WAIT_TOKEN_PATTERN.findall(normalized)
	if any(not word.isdigit() and word not in LIMIT_WAIT_VOCABULARY for word in words):
		return False
	if not (
		has_limit_text(needs_action)
		or LIMIT_WORD_PATTERN.search(needs_action)
		or LIMIT_WAIT_NAMES_LIMIT_PATTERN.search(needs_action)
	):
		return False
	sentences = [_limit_wait_sentence(part) for part in LIMIT_WAIT_SENTENCE_BREAK_PATTERN.split(normalized)]
	sentences = [sentence for sentence in sentences if sentence]
	previous = ""
	for sentence in sentences:
		kind = _limit_wait_sentence_kind(sentence, previous)
		if not kind:
			return False
		previous = kind
	return bool(sentences)


def _limit_wait_sentence(text: str) -> str:
	"""One sentence as lowercase words joined by spaces, each run of numbers and time words collapsed to `#`."""
	words = []
	for word in LIMIT_WAIT_TOKEN_PATTERN.findall(text):
		if word.isdigit() or word in LIMIT_WAIT_TIME_WORDS:
			if words and words[-1] == "#":
				continue
			word = "#"
		words.append(word)
	return " ".join(words)


def _limit_wait_sentence_kind(sentence: str, previous: str) -> str:
	"""The closed form a sentence matches whole (`status`, `reset`, `wait`, `retry`, `then`), or "" for none.

	A `then` sentence ("then retry", "try again then") counts only right after a `wait` or `reset` sentence.
	"""
	for kind, forms in (
		("status", LIMIT_WAIT_STATUS_FORMS),
		("reset", LIMIT_WAIT_RESET_FORMS),
		("wait", LIMIT_WAIT_WAIT_FORMS),
		("retry", LIMIT_WAIT_RETRY_FORMS),
	):
		if any(form.fullmatch(sentence) for form in forms):
			return kind
	if previous in ("wait", "reset") and any(form.fullmatch(sentence) for form in LIMIT_WAIT_THEN_FORMS):
		return "then"
	return ""


def has_unanswered_request(session: dict) -> bool:
	"""True when either summary copy records a request to a human that is not only a limit wait."""
	return any(not is_limit_wait(text) for text in _needs_action_texts(session))


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
	"""Map session key → `(wake time, is a resume trigger)` for every enabled trigger bound to it.

	The wake time is `next_run_at` when readable, else `run_once_at`, and None when neither is readable.
	The key is `session_key(persistent_session_id)`, so a trigger bound by the `cse_` form still counts.
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
		is_resume = RESUME_TRIGGER_NAME_PATTERN.match(_text(trigger.get("name"))) is not None
		wake_time = _parse_time(trigger.get("next_run_at"))
		if wake_time is None:
			wake_time = _parse_time(trigger.get("run_once_at"))
		wakes.setdefault(session_key(session_id), []).append((wake_time, is_resume))
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


def _skip_reason(
	session: dict,
	kind: str,
	signal: str,
	pickup_session: str,
	next_runs: list,
	now: dt.datetime,
	*,
	pickup_key: str | None = None,
	allowed_repos: frozenset | None = None,
) -> str | None:
	"""Why a signalled session is not resumed, or None.

	`select` passes `pickup_key` (the pickup's `session_key`, computed once per run) and `allowed_repos`
	(`load_allowed_repos`); without `allowed_repos` no session is authorized.
	"""
	status = _text(session.get("session_status"))
	own_key = pickup_key if pickup_key is not None else session_key(pickup_session)
	if session_key(_text(session.get("id"))) == own_key:
		return "pickup"
	unauthorized = _unauthorized_reason(session, allowed_repos)
	if unauthorized:
		return unauthorized
	if status == ARCHIVED_STATUS:
		return "archived"
	# PR #6112 review round 6: like `has_unanswered_request`, the permission prompt and `need_input` read both summary
	# copies, so a marker recorded only in `external_metadata.post_turn_summary` is not missed.
	summaries = _summary_copies(session)
	needs_input = has_unanswered_request(session) or (
		signal == "rate_limit_info"
		and any(_text(summary_copy.get("status_category")) == "need_input" for summary_copy in summaries)
	)
	permission_prompt = any(
		PERMISSION_PROMPT_PATTERN.search(_text(summary_copy.get("needs_action")))
		or PERMISSION_PROMPT_PATTERN.search(_text(summary_copy.get("status_detail")))
		for summary_copy in summaries
	)
	created_at = _parse_time(session.get("created_at"))
	if created_at is not None and created_at < now - dt.timedelta(hours=SESSION_WINDOW_HOURS):
		# PR #6112 review round 1: the manual fallback resumes too_old sessions, so an old one waiting on a human says so,
		# with the same precedence as a recent one (review round 5): a permission prompt before an unanswered request.
		if permission_prompt:
			return "permission_prompt"
		return "needs_input" if needs_input else "too_old"
	if status != IDLE_STATUS:
		short = status[len(SESSION_STATUS_PREFIX):] if status.startswith(SESSION_STATUS_PREFIX) else status
		return f"not_idle:{short.lower() or 'unknown'}"
	if permission_prompt:
		return "permission_prompt"
	if needs_input:
		return "needs_input"
	if limit_holds(_rate_limit_info(session), now):
		return "not_reset"
	if _wake_pending(next_runs, kind, now):
		return "wake_pending"
	return None


def select(
	sessions: list,
	triggers: list,
	pickup_session: str,
	login: str,
	limit: int,
	now: dt.datetime,
	*,
	allowed_repos: frozenset | None = None,
	errors: list | None = None,
) -> dict:
	"""Pick the sessions to resume this wake; see the module docstring for the rules.

	`allowed_repos` is the `load_allowed_repos` set; None authorizes no session (fail closed).
	`errors` seeds the output's `errors` (the registry line from `load_allowed_repos`).
	"""
	errors = list(errors or [])
	wakes = _bound_wakes(triggers, errors)
	seen: set[str] = set()
	selected: list[tuple] = []
	skipped: list[dict] = []
	account_holds = False
	far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
	pickup_key = session_key(pickup_session)
	for position, session in enumerate(sessions):
		if not isinstance(session, dict):
			errors.append(f"session entry {position}: not an object")
			continue
		session_id = session.get("id")
		if not isinstance(session_id, str) or not session_id:
			errors.append(f"session entry {position}: missing id")
			continue
		key = session_key(session_id)
		if key in seen:
			continue
		seen.add(key)
		if key == pickup_key:
			account_holds = limit_holds(_rate_limit_info(session), now)
		title = _text(session.get("title"))
		kind = session_kind(title)
		next_runs = wakes.get(key, [])
		signal = _signal(session, kind, next_runs, now)
		if signal is None:
			continue
		reason = _skip_reason(
			session, kind, signal, pickup_session, next_runs, now, pickup_key=pickup_key, allowed_repos=allowed_repos
		)
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
	parser.add_argument("--self-repo", default=DEFAULT_SELF_REPO, help=f"the pickup's own <owner>/<repo> (default {DEFAULT_SELF_REPO})")
	parser.add_argument("--registry", default=DEFAULT_REGISTRY_PATH, help=f"consumer-repo registry JSON array (default {DEFAULT_REGISTRY_PATH})")
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
		self_repo = args.self_repo.strip()
		if not REPO_SLUG_PATTERN.fullmatch(self_repo):
			raise InputError("--self-repo must be <owner>/<repo>")
		sessions = [entry for path in args.sessions for entry in _load_entries(path)]
		triggers = [entry for path in args.triggers for entry in _load_entries(path)]
	except InputError as exc:
		print(json.dumps({"resume": [], "error": str(exc)}))
		return 2
	registry_errors: list[str] = []
	allowed_repos = load_allowed_repos(args.registry, self_repo, registry_errors)
	limit = resolve_resume_limit(args.limit, environ.get(RESUME_LIMIT_ENV))
	now = now or dt.datetime.now(dt.timezone.utc)
	result = select(sessions, triggers, pickup_session, login, limit, now, allowed_repos=allowed_repos, errors=registry_errors)
	print(json.dumps(result, ensure_ascii=False))
	return 0


if __name__ == "__main__":
	sys.exit(main())

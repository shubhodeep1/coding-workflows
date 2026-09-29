#!/usr/bin/env python3
"""Dead-checker restart for the Claude issue pickup (issue #4910, operator rule Q63).

An `/implement-plan-claude` project checker keeps its chain alive by re-arming
itself with `send_later`. When a re-arm fails, nothing is pending and the
project stalls. The hourly Claude issue pickup
(`.claude/commands/claude-issue-pickup.md`, step 3b) runs this script: it
decides, and the pickup applies the decision, the same split as
`.claude/scripts/stale_routines.py`. The script never creates, tags, or
comments on anything itself.

Two subcommands, each printing one JSON object on stdout:

  scan   --sessions-file S --triggers-file T --repo OWNER/REPO --self ID
         --state-out F
      Reads the saved newest `list_sessions` page (`mine: true`,
      `limit: 100`) and the saved `list_triggers` page (`enabled: true`,
      `limit: 100`), exactly as the harness saved them (an untrusted-data
      envelope around the JSON is fine). Lists the open `ai:claude` issues of
      `--repo` with one REST call and reads their progress logs from the
      project branches over git (one `git ls-remote`, one shallow
      `git fetch`, no REST). Writes everything `decide` needs to `F` and
      prints `{"lookup": [session ids], "candidates": n, "state": F,
      "log_projects": n, "errors": [...]}`. The pickup calls `get_session`
      once per `lookup` id.

  decide --state F [--lookup-file L]
      `L` is a JSON object mapping each looked-up session id to the
      `get_session` result as returned (`{"ccr": {...}}` or the bare
      session), to `"not_found"` when `get_session` answered that the session
      does not exist, or to `{"error": "<text>"}` for any other failure.
      Prints `{"restart": [...], "requeue": [...], "skipped": [...],
      "errors": [...]}`.

A project checker is a non-archived session titled
`implement-plan <slug> — checker`, optionally after a `#<issue> · `,
`#<issue> · PR #<pr> — `, or `PR #<pr> — ` prefix (#4886). The script finds
them three ways:

  * checker titles on the page;
  * the checker id each enabled `implement-plan <slug>: safety net` Routine's
    prompt names (`… get_session <checker id>; …`): a checker that died during
    a wait leaves the stage's 24-hour safety net pending;
  * the checker id on the `Check-in:` line of the progress log
    `docs/implement-plan/<slug>.md` on `claude/implement-plan-<slug>`, for
    every open `ai:claude` issue of `--repo` without `ai:claude-blocked`.

A checker that is not on the page and not bound to an enabled trigger is
looked up (at most `LOOKUP_CAP` per wake, rotated by the hour).

Restart (Q63, all must hold; the first that fails is the `skipped` reason):

  0. the trigger page is complete (`has_more` false);
  1. no enabled trigger has `persistent_session_id` equal to the checker
     (`has_pending_trigger`), and no other checker of the slug is bound to
     one (`sibling_checker_alive`);
  3. the checker is `SESSION_STATUS_IDLE`, its bucket is not `BLOCKED`, and
     its `status_category` is not `need_input`;
  4. no other non-archived session of the project is `RUNNING`,
     `REQUIRES_ACTION`, or `need_input`, or was created or updated in the last
     `ACTIVE_WINDOW_MINUTES` (90). A session belongs to the project when its
     `parent_session_id` is the checker, its current branch is
     `claude/implement-plan-<slug>` or starts with `claude/implement-plan-<slug>-`,
     its title contains `implement-plan <slug>`, or, for an `issue-<N>-…`
     slug, its title starts with `#<N> · ` or is the issue's start session
     (`Issue #<N> — implement`, `issue <repo>#<N> — implement`,
     `implement-issue-claude — #<N>`);
  5. the checker carries no `ai-checker-restart:<YYYYMMDDTHHMMZ>` tag younger
     than `RESTART_COOLDOWN_HOURS` (3);
  2. for an `issue-<N>-…` slug, issue #<N> is open and not labelled
     `ai:claude-blocked` (read last: it may cost one REST call).

A `restart` entry carries `checker`, `slug`, `repo`, `trigger_name`
(`implement-plan <slug>: check-in`), `prompt` (`RESTART_PROMPT`), `tag_add`,
`tag_remove`, and `reason`.

Re-queue (OWNER scope addition): a checker named only by a progress log that
`get_session` reports as not found. The issue is re-queued when it is still
open, labelled `ai:claude`, not `ai:claude-blocked`; no project session on the
page is active; no other checker of the slug is on the page; no enabled
trigger belongs to the project; and no trusted `/reclarify` comment was
posted in the last `REQUEUE_COOLDOWN_HOURS` (24). A `requeue` entry carries
`repo`, `issue`, `slug`, `checker`, and the exact `comment_body`, which
starts with `/reclarify`.

API budget (CLAUDE.md §15): REST only, never GraphQL. `scan` issues one
`issues?labels=ai:claude&state=open` read; `decide` issues one issue read per
distinct candidate issue outside that list and one paginated comment read per
re-queue candidate. The logs are read over git. Every failed read or missing
input keeps the checker (fail safe) and is listed in `errors`.

Exit status: 0 when a verdict was reached, 2 when an input file is missing or
not the expected JSON shape.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from typing import Any

ACTIVE_WINDOW_MINUTES = 90
RESTART_COOLDOWN_HOURS = 3.0
REQUEUE_COOLDOWN_HOURS = 24.0
LOOKUP_CAP = 8

RESTART_PROMPT = (
	"Check-in from the Claude issue pickup: no check-in is pending for you and no stage of this "
	"project has been active for 90 minutes. Repeat the steps in your most recent "
	"checker-instructions message now, starting at step 1 (step 0's stale-wake check still applies)."
)
RESTART_TAG_PREFIX = "ai-checker-restart:"
RESTART_TAG_TIME_FORMAT = "%Y%m%dT%H%MZ"
REQUEUE_MARKER_TEMPLATE = "<!-- ai:claude-checker-requeue:v1 checker={checker} slug={slug} -->"

CLAUDE_LABEL = "ai:claude"
BLOCKED_LABEL = "ai:claude-blocked"
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
RECLARIFY_PREFIX = "/reclarify"

STATUS_IDLE = "SESSION_STATUS_IDLE"
STATUS_ARCHIVED = "SESSION_STATUS_ARCHIVED"
ACTIVE_STATUSES = ("SESSION_STATUS_RUNNING", "SESSION_STATUS_REQUIRES_ACTION")
BUCKET_BLOCKED = "SESSION_STATUS_BUCKET_BLOCKED"
CATEGORY_NEED_INPUT = "need_input"

SLUG = r"[A-Za-z0-9._-]+"
TITLE_PREFIX = r"(?:#\d+ · (?:PR #\d+ — )?|PR #\d+ — )?"
CHECKER_TITLE_PATTERN = re.compile(rf"^{TITLE_PREFIX}implement-plan (?P<slug>{SLUG}) — checker$")
ISSUE_SLUG_PATTERN = re.compile(r"^issue-(?P<issue>\d+)-")
SAFETY_NET_PROMPT_PATTERN = re.compile(
	r"^Safety net for /implement-plan-claude (?P<plan>\S+?): .*?get_session (?P<checker>session_[A-Za-z0-9]+)",
	re.DOTALL,
)
LOG_CHECKER_PATTERN = re.compile(r"\bchecker (?P<checker>session_[A-Za-z0-9]+)")
REPO_URL_PATTERN = re.compile(r"github\.com[/:](?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
PROJECT_BRANCH_PREFIX = "claude/implement-plan-"
SUB_BRANCH_PATTERN = re.compile(
	r"-(?:(?:phase|conformance-fix|validation-fix|activation-fix|security-fix)-\d+|complete|decision-changes)(?:-\d+)?$"
)
FINISHED_ACTIVATION_PREFIXES = ("LIVE", "n/a", "deploy-activate started")
# Trailing marks a cut Routine name may carry; a cut without one matches too.
TRUNCATION_MARKERS = ("…", "...")


class ReadError(Exception):
	"""A `gh api` or git read failed; whatever it concerned is kept."""


# --- I/O seams (tests stub these) -------------------------------------------------


def gh_api(path: str, paginate: bool = False) -> Any:
	"""GET one REST path through `gh api` and return the decoded JSON.

	With `paginate`, `gh api --paginate --slurp` returns one array per page;
	they are flattened into one list.
	"""
	cmd = ["gh", "api", path]
	if paginate:
		cmd[2:2] = ["--paginate", "--slurp"]
	try:
		proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ReadError(f"gh api {path} failed: {exc}") from exc
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout).strip().splitlines()
		raise ReadError(f"gh api {path} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	try:
		payload = json.loads(proc.stdout)
	except ValueError as exc:
		raise ReadError(f"gh api {path} returned invalid JSON") from exc
	if paginate:
		if not isinstance(payload, list) or any(not isinstance(page, list) for page in payload):
			raise ReadError(f"gh api {path} returned an unexpected paginated shape")
		return [item for page in payload for item in page]
	return payload


def _git(args: list[str], timeout: int = 180) -> str:
	try:
		proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ReadError(f"git {' '.join(args)} failed: {exc}") from exc
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout).strip().splitlines()
		raise ReadError(f"git {' '.join(args)} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	return proc.stdout


def read_project_logs(branches: list[str], errors: list[str] | None = None) -> dict[str, str]:
	"""Return {branch: log text} for each `claude/implement-plan-<slug>` branch that has its log.

	One `git ls-remote` for the tips and one shallow `git fetch` for all of
	them; a branch without `docs/implement-plan/<slug>.md` is left out. When
	`git show` fails, one local `git ls-tree` tells a missing log (left out
	silently) from a failed read, which is left out and appended to `errors`.
	"""
	if not branches:
		return {}
	listing = _git(["ls-remote", "--heads", "origin", *[f"refs/heads/{branch}" for branch in branches]], timeout=60)
	tips: dict[str, str] = {}
	for line in listing.splitlines():
		sha, _, ref = line.partition("\t")
		if ref.startswith("refs/heads/"):
			tips[ref[len("refs/heads/"):]] = sha.strip()
	present = [branch for branch in branches if branch in tips]
	if not present:
		return {}
	_git(["fetch", "--quiet", "--no-tags", "--depth", "1", "origin", *[f"refs/heads/{branch}" for branch in present]])
	logs: dict[str, str] = {}
	for branch in present:
		log_path = f"docs/implement-plan/{branch[len(PROJECT_BRANCH_PREFIX):]}.md"
		try:
			logs[branch] = _git(["show", f"{tips[branch]}:{log_path}"], timeout=30)
		except ReadError as exc:
			check_failure = ""
			try:
				missing = not _git(["ls-tree", "--name-only", tips[branch], "--", log_path], timeout=30).strip()
			except ReadError as ls_exc:
				missing = False
				check_failure = f" (missing-log check also failed: {ls_exc})"
			if not missing and errors is not None:
				errors.append(f"project log {branch}: {exc}{check_failure}")
	return logs


def list_project_branches() -> list[str]:
	"""Every `claude/implement-plan-issue-*` project branch (sub-branches left out), from one `git ls-remote`."""
	listing = _git(["ls-remote", "--heads", "origin", "refs/heads/claude/implement-plan-issue-*"], timeout=60)
	branches = []
	for line in listing.splitlines():
		_, _, ref = line.partition("\t")
		branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
		if branch and not SUB_BRANCH_PATTERN.search(branch):
			branches.append(branch)
	return branches


# --- input loading ---------------------------------------------------------------------


def _load_json_value(path: str) -> Any:
	"""Load a JSON file, tolerating the harness's untrusted-data envelope around the value."""
	with open(path, encoding="utf-8") as handle:
		text = handle.read()
	try:
		return json.loads(text)
	except ValueError:
		pass
	decoder = json.JSONDecoder()
	for match in re.finditer(r"[\[{]", text):
		try:
			value, _ = decoder.raw_decode(text, match.start())
		except ValueError:
			continue
		if isinstance(value, list) or (isinstance(value, dict) and ("ccr" in value or "data" in value)):
			return value
	raise ValueError("no JSON value found")


def load_sessions(path: str) -> list[dict]:
	payload = _load_json_value(path)
	if isinstance(payload, dict) and isinstance(payload.get("ccr"), dict):
		payload = payload["ccr"]
	sessions = payload.get("data") if isinstance(payload, dict) else payload
	if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
		raise ValueError("expected the list_sessions result or its `data` array of objects")
	return sessions


def load_triggers(path: str) -> tuple[list[dict], bool]:
	"""Return (routines, has_more)."""
	payload = _load_json_value(path)
	has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
	routines = payload.get("data") if isinstance(payload, dict) else payload
	if not isinstance(routines, list) or any(not isinstance(item, dict) for item in routines):
		raise ValueError("expected the list_triggers result or its `data` array of objects")
	return routines, has_more


def load_lookups(path: str) -> dict[str, Any]:
	payload = _load_json_value(path) if path else {}
	if not isinstance(payload, dict):
		raise ValueError("expected a JSON object mapping session ids to get_session results")
	return payload


# --- views -------------------------------------------------------------------------------


def _parse_time(value: object) -> dt.datetime | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None
	return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def session_view(raw: dict) -> dict:
	"""The fields the rules read, from a `list_sessions` / `get_session` record."""
	if isinstance(raw.get("ccr"), dict):
		raw = raw["ccr"]
	external = raw.get("external_metadata") if isinstance(raw.get("external_metadata"), dict) else {}
	summary = raw.get("post_turn_summary") if isinstance(raw.get("post_turn_summary"), dict) else None
	if summary is None:
		summary = external.get("post_turn_summary") if isinstance(external.get("post_turn_summary"), dict) else {}
	branches = external.get("current_branches")
	branch = ""
	if isinstance(branches, dict):
		branch = next((value for value in branches.values() if isinstance(value, str) and value), "")
	repo = ""
	context = raw.get("session_context") if isinstance(raw.get("session_context"), dict) else {}
	for source in context.get("sources") or []:
		url = ((source or {}).get("git_repository") or {}).get("url") if isinstance(source, dict) else None
		match = REPO_URL_PATTERN.search(url) if isinstance(url, str) else None
		if match:
			repo = match.group("repo")
			break
	tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
	return {
		"id": raw.get("id") if isinstance(raw.get("id"), str) else "",
		"title": (raw.get("title") or "").strip() if isinstance(raw.get("title"), str) else "",
		"status": raw.get("session_status") or "",
		"bucket": raw.get("status_bucket") or "",
		"category": summary.get("status_category") or "",
		"created_at": raw.get("created_at") or "",
		"updated_at": raw.get("updated_at") or "",
		"parent": raw.get("parent_session_id") or "",
		"branch": branch,
		"repo": repo,
		"tags": [tag for tag in tags if isinstance(tag, str)],
	}


def trigger_view(raw: dict) -> dict:
	derived = raw.get("derived_state") if isinstance(raw.get("derived_state"), dict) else {}
	prompt = derived.get("prompt") if isinstance(derived.get("prompt"), str) else raw.get("prompt")
	return {
		"id": raw.get("id") if isinstance(raw.get("id"), str) else "",
		"name": raw.get("name") if isinstance(raw.get("name"), str) else "",
		"enabled": raw.get("enabled") is True,
		"session": raw.get("persistent_session_id") or "",
		"prompt": prompt if isinstance(prompt, str) else "",
	}


def checker_slug(title: str) -> str:
	match = CHECKER_TITLE_PATTERN.match(title or "")
	return match.group("slug") if match else ""


def issue_of(slug: str) -> int | None:
	match = ISSUE_SLUG_PATTERN.match(slug or "")
	return int(match.group("issue")) if match else None


def slug_of_plan(plan_path: str) -> str:
	name = plan_path.rsplit("/", 1)[-1]
	for suffix in ("-plan.md", ".md"):
		if name.endswith(suffix):
			return name[: -len(suffix)]
	return name


def parse_log(text: str) -> dict:
	"""Pick the header lines of a progress log: Source issue, Status, Activation, Check-in."""
	fields: dict[str, str] = {}
	for line in text.splitlines():
		match = re.match(r"^- (Source issue|Status|Activation|Check-in): ?(.*)$", line)
		if match and match.group(1) not in fields:
			fields[match.group(1)] = match.group(2).strip()
	checker = LOG_CHECKER_PATTERN.search(fields.get("Check-in", ""))
	return {
		"source_issue": fields.get("Source issue", ""),
		"status": fields.get("Status", "").split()[0] if fields.get("Status") else "",
		"activation": fields.get("Activation", ""),
		"checker": checker.group("checker") if checker else "",
	}


# --- the rules ---------------------------------------------------------------------------


def _is_active(view: dict, now: dt.datetime) -> bool:
	if view["status"] in ACTIVE_STATUSES or view["category"] == CATEGORY_NEED_INPUT:
		return True
	window = now - dt.timedelta(minutes=ACTIVE_WINDOW_MINUTES)
	for key in ("created_at", "updated_at"):
		stamp = _parse_time(view[key])
		if stamp is not None and stamp >= window:
			return True
	return False


def _belongs(view: dict, checker: str, slug: str) -> bool:
	if checker and view["parent"] == checker:
		return True
	project_branch = f"{PROJECT_BRANCH_PREFIX}{slug}"
	if view["branch"] == project_branch or view["branch"].startswith(project_branch + "-"):
		return True
	title = view["title"]
	if re.search(r"implement-plan " + re.escape(slug) + r"(?:\s|:|$)", title):
		return True
	issue = issue_of(slug)
	if issue is not None:
		number = rf"{issue}(?!\d)"
		if re.match(rf"^#{number} · ", title):
			return True
		if re.match(
			rf"^{TITLE_PREFIX}(?:Issue #{number} — implement|issue {SLUG}/{SLUG}#{number} — implement|implement-issue-claude — #{number})",
			title,
		):
			return True
	return False


def active_project_session(sessions: list[dict], checker: str, slug: str, exclude: set[str], now: dt.datetime) -> str:
	"""Id of a non-archived, active session of the project other than `exclude`, or ''."""
	for view in sessions:
		if view["id"] in exclude or view["status"] == STATUS_ARCHIVED:
			continue
		if _belongs(view, checker, slug) and _is_active(view, now):
			return view["id"]
	return ""


def _bound(triggers: list[dict]) -> set[str]:
	return {trigger["session"] for trigger in triggers if trigger["enabled"] and trigger["session"]}


def _project_trigger(triggers: list[dict], slug: str) -> str:
	"""Id of an enabled trigger that belongs to the project by name or prompt, or ''.

	Routine names are often cut at 60 characters and marked with `…`, and some
	are stored whole. A name matches when it starts with
	`implement-plan <slug>:` or, with a trailing `…` or `...` removed, is a
	prefix of it; a name cut without any marker matches the same way. A wrong
	match only keeps the issue from being re-queued.
	"""
	wanted = f"implement-plan {slug}:"
	for trigger in triggers:
		if not trigger["enabled"]:
			continue
		name = trigger["name"]
		if name.startswith(wanted):
			return trigger["id"]
		stem = name
		for marker in TRUNCATION_MARKERS:
			if stem.endswith(marker):
				stem = stem[: -len(marker)]
				break
		if stem.startswith("implement-plan ") and wanted.startswith(stem):
			return trigger["id"]
		if f"/{slug}-plan.md" in trigger["prompt"]:
			return trigger["id"]
	return ""


def _last_restart(tags: list[str]) -> dt.datetime | None:
	latest = None
	for tag in tags:
		if not tag.startswith(RESTART_TAG_PREFIX):
			continue
		try:
			stamp = dt.datetime.strptime(tag[len(RESTART_TAG_PREFIX):], RESTART_TAG_TIME_FORMAT).replace(tzinfo=dt.timezone.utc)
		except ValueError:
			continue
		if latest is None or stamp > latest:
			latest = stamp
	return latest


def collect_candidates(state: dict) -> dict[str, dict]:
	"""{checker id: {"slug", "sources", "issue"}} from page titles, safety-net prompts, and logs."""
	candidates: dict[str, dict] = {}

	def add(checker: str, slug: str, source: str, issue: int | None = None) -> None:
		entry = candidates.setdefault(checker, {"slug": slug, "sources": [], "issue": issue})
		if source not in entry["sources"]:
			entry["sources"].append(source)
		if issue is not None and entry["issue"] is None:
			entry["issue"] = issue

	for view in state["sessions"]:
		slug = checker_slug(view["title"])
		if slug and view["status"] != STATUS_ARCHIVED:
			add(view["id"], slug, "page")
	for trigger in state["triggers"]:
		if not trigger["enabled"] or not trigger["name"].startswith("implement-plan "):
			continue
		match = SAFETY_NET_PROMPT_PATTERN.match(trigger["prompt"])
		if match:
			add(match.group("checker"), slug_of_plan(match.group("plan")), "safety_net")
	for project in state["log_projects"]:
		if project["checker"]:
			add(project["checker"], project["slug"], "log", project["issue"])
	return candidates


def lookups_needed(state: dict, now: dt.datetime) -> list[str]:
	"""Checker ids the pickup must `get_session`: not on the page, not bound to an enabled trigger."""
	on_page = {view["id"] for view in state["sessions"]}
	bound = _bound(state["triggers"])
	open_issues = state["open_issues"]
	needed = []
	for checker, entry in collect_candidates(state).items():
		if checker in on_page or checker in bound:
			continue
		if active_project_session(state["sessions"], checker, entry["slug"], {checker, state["self"]}, now):
			continue
		issue = issue_of(entry["slug"])
		known = open_issues.get(str(issue)) if issue is not None else None
		if known is not None and BLOCKED_LABEL in known:
			continue
		needed.append(checker)
	needed.sort()
	if len(needed) > LOOKUP_CAP:
		start = int(now.timestamp() // 3600) % len(needed)
		needed = (needed[start:] + needed[:start])[:LOOKUP_CAP]
	return needed


def _issue_open_unblocked(repo: str, issue: int, state: dict, cache: dict) -> tuple[bool, str]:
	"""(ok, reason) for condition 2; one cached REST read when the issue is not in the open list."""
	if repo == state["repo"] and str(issue) in state["open_issues"]:
		labels = state["open_issues"][str(issue)]
		return (False, "issue_blocked") if BLOCKED_LABEL in labels else (True, "")
	key = (repo, issue)
	if key not in cache:
		cache[key] = gh_api(f"repos/{repo}/issues/{issue}")
	payload = cache[key]
	if not isinstance(payload, dict):
		raise ReadError(f"repos/{repo}/issues/{issue} returned non-object JSON")
	if payload.get("state") != "open":
		return False, "issue_closed"
	labels = {label.get("name") for label in payload.get("labels") or [] if isinstance(label, dict)}
	return (False, "issue_blocked") if BLOCKED_LABEL in labels else (True, "")


def _recent_reclarify(repo: str, issue: int, now: dt.datetime) -> bool:
	since = (now - dt.timedelta(hours=REQUEUE_COOLDOWN_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
	comments = gh_api(f"repos/{repo}/issues/{issue}/comments?since={since}&per_page=100", paginate=True)
	window = now - dt.timedelta(hours=REQUEUE_COOLDOWN_HOURS)
	for comment in comments:
		if not isinstance(comment, dict):
			continue
		user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
		body = comment.get("body")
		created = _parse_time(comment.get("created_at"))
		if (
			user.get("type") == "User"
			and comment.get("author_association") in TRUSTED_ASSOCIATIONS
			and isinstance(body, str)
			and body.startswith(RECLARIFY_PREFIX)
			and created is not None
			and created >= window
		):
			return True
	return False


def requeue_comment(checker: str, slug: str) -> str:
	return (
		f"{RECLARIFY_PREFIX}\n\n"
		f"{REQUEUE_MARKER_TEMPLATE.format(checker=checker, slug=slug)}\n"
		f"🤖 Re-queued by the Claude issue pickup: the progress log `docs/implement-plan/{slug}.md` names "
		f"checker `{checker}`, which no longer exists, so nothing is watching this project. The resumed "
		"session arms a new checker from the log. The pickup re-queues an issue at most once per 24 hours."
	)


def decide(state: dict, lookups: dict[str, Any], now: dt.datetime) -> dict:
	sessions: list[dict] = list(state["sessions"])
	by_id = {view["id"]: view for view in sessions}
	not_found: set[str] = set()
	errors: list[str] = list(state.get("errors") or [])
	for checker, result in lookups.items():
		if checker in by_id:
			continue
		if result == "not_found":
			not_found.add(checker)
		elif isinstance(result, dict) and "error" in result and "ccr" not in result and "id" not in result:
			errors.append(f"{checker}: get_session failed: {result.get('error')}")
		elif isinstance(result, dict):
			view = session_view(result)
			if view["id"] == checker:
				by_id[checker] = view
				sessions.append(view)
	triggers = state["triggers"]
	bound = _bound(triggers)
	restart: list[dict] = []
	requeue: list[dict] = []
	skipped: list[dict] = []
	issue_cache: dict = {}
	requeued_issues: set[int] = set()

	def skip(checker: str, slug: str, reason: str) -> None:
		skipped.append({"checker": checker, "slug": slug, "reason": reason})

	for checker, entry in sorted(collect_candidates(state).items()):
		slug = entry["slug"]
		view = by_id.get(checker)
		if view is None:
			if checker in not_found:
				if "log" in entry["sources"]:
					_evaluate_requeue(checker, entry, state, sessions, triggers, now, requeue, requeued_issues, skip, errors)
				else:
					skip(checker, slug, "checker_not_found")
			elif checker in bound:
				skip(checker, slug, "has_pending_trigger")
			else:
				active = active_project_session(sessions, checker, slug, {checker, state["self"]}, now)
				skip(checker, slug, f"project_session_active: {active}" if active else "not_looked_up")
			continue
		if view["status"] == STATUS_ARCHIVED:
			skip(checker, slug, "checker_archived")
			continue
		if checker_slug(view["title"]) != slug:
			skip(checker, slug, "not_a_checker_title")
			continue
		if state["triggers_has_more"]:
			skip(checker, slug, "triggers_page_incomplete")
			continue
		if checker in bound:
			skip(checker, slug, "has_pending_trigger")
			continue
		siblings = [
			other["id"] for other in sessions
			if other["id"] != checker and other["status"] != STATUS_ARCHIVED
			and checker_slug(other["title"]) == slug and other["id"] in bound
		]
		if siblings:
			skip(checker, slug, f"sibling_checker_alive: {siblings[0]}")
			continue
		if view["status"] != STATUS_IDLE or view["bucket"] == BUCKET_BLOCKED:
			skip(checker, slug, f"checker_not_idle: {view['status']}")
			continue
		if view["category"] == CATEGORY_NEED_INPUT:
			skip(checker, slug, "checker_needs_input")
			continue
		active = active_project_session(sessions, checker, slug, {checker, state["self"]}, now)
		if active:
			skip(checker, slug, f"project_session_active: {active}")
			continue
		last = _last_restart(view["tags"])
		if last is not None and now - last < dt.timedelta(hours=RESTART_COOLDOWN_HOURS):
			skip(checker, slug, f"restarted_recently: {last.strftime(RESTART_TAG_TIME_FORMAT)}")
			continue
		issue = issue_of(slug)
		repo = view["repo"] or state["repo"]
		if issue is not None:
			try:
				ok, reason = _issue_open_unblocked(repo, issue, state, issue_cache)
			except (ReadError, KeyError, TypeError, ValueError) as exc:
				errors.append(f"{checker} ({slug}): {exc}")
				skip(checker, slug, "issue_read_failed")
				continue
			if not ok:
				skip(checker, slug, reason)
				continue
		restart.append(
			{
				"checker": checker,
				"slug": slug,
				"repo": repo,
				"trigger_name": f"implement-plan {slug}: check-in",
				"prompt": RESTART_PROMPT,
				"tag_add": f"{RESTART_TAG_PREFIX}{now.strftime(RESTART_TAG_TIME_FORMAT)}",
				"tag_remove": [tag for tag in view["tags"] if tag.startswith(RESTART_TAG_PREFIX)],
				"reason": "no pending trigger, no active project session for "
				f"{ACTIVE_WINDOW_MINUTES} minutes (sources: {', '.join(entry['sources'])})",
			}
		)
	return {"restart": restart, "requeue": requeue, "skipped": skipped, "errors": errors}


def _evaluate_requeue(checker, entry, state, sessions, triggers, now, requeue, requeued_issues, skip, errors) -> None:
	slug = entry["slug"]
	issue = entry["issue"]
	repo = state["repo"]
	labels = state["open_issues"].get(str(issue))
	if issue is None or labels is None:
		skip(checker, slug, "issue_not_open_claude")
		return
	if BLOCKED_LABEL in labels:
		skip(checker, slug, "issue_blocked")
		return
	if issue in requeued_issues:
		skip(checker, slug, "requeue_already_listed")
		return
	active = active_project_session(sessions, checker, slug, {checker, state["self"]}, now)
	if active:
		skip(checker, slug, f"project_session_active: {active}")
		return
	others = [view["id"] for view in sessions if view["status"] != STATUS_ARCHIVED and checker_slug(view["title"]) == slug]
	if others:
		skip(checker, slug, f"other_checker_exists: {others[0]}")
		return
	pending = _project_trigger(triggers, slug)
	if pending:
		skip(checker, slug, f"project_trigger_pending: {pending}")
		return
	try:
		recent = _recent_reclarify(repo, issue, now)
	except (ReadError, KeyError, TypeError, ValueError) as exc:
		errors.append(f"{checker} ({slug}): {exc}")
		skip(checker, slug, "comment_read_failed")
		return
	if recent:
		skip(checker, slug, "requeued_recently")
		return
	requeued_issues.add(issue)
	requeue.append(
		{
			"repo": repo,
			"issue": issue,
			"slug": slug,
			"checker": checker,
			"comment_body": requeue_comment(checker, slug),
			"reason": "the progress log names a checker that get_session reports as not found",
		}
	)


# --- scan ---------------------------------------------------------------------------------


def scan_logs(repo: str, errors: list[str]) -> tuple[dict[str, list[str]], list[dict]]:
	"""(open ai:claude issues {number: labels}, log projects) — fail open on every read."""
	open_issues: dict[str, list[str]] = {}
	try:
		payload = gh_api(f"repos/{repo}/issues?labels={CLAUDE_LABEL}&state=open&per_page=100")
	except ReadError as exc:
		errors.append(f"open issue list: {exc}")
		return open_issues, []
	if not isinstance(payload, list):
		errors.append("open issue list: unexpected shape")
		return open_issues, []
	for item in payload:
		if not isinstance(item, dict) or "pull_request" in item or not isinstance(item.get("number"), int):
			continue
		open_issues[str(item["number"])] = [
			label.get("name") for label in item.get("labels") or [] if isinstance(label, dict) and isinstance(label.get("name"), str)
		]
	wanted = {number for number, labels in open_issues.items() if BLOCKED_LABEL not in labels}
	if not wanted:
		return open_issues, []
	try:
		branches = [
			branch for branch in list_project_branches()
			if (issue_of(branch[len(PROJECT_BRANCH_PREFIX):]) is not None and str(issue_of(branch[len(PROJECT_BRANCH_PREFIX):])) in wanted)
		]
		logs = read_project_logs(branches, errors)
	except ReadError as exc:
		errors.append(f"project logs: {exc}")
		return open_issues, []
	projects = []
	for branch, text in sorted(logs.items()):
		slug = branch[len(PROJECT_BRANCH_PREFIX):]
		issue = issue_of(slug)
		fields = parse_log(text)
		if not re.search(rf"\b{re.escape(repo)}#{issue}(?!\d)", fields["source_issue"]):
			continue
		if fields["status"] == "BLOCKED" or fields["activation"].startswith(FINISHED_ACTIVATION_PREFIXES):
			continue
		projects.append({"slug": slug, "issue": issue, "branch": branch, "checker": fields["checker"]})
	return open_issues, projects


def build_state(sessions_raw: list[dict], triggers_raw: list[dict], triggers_has_more: bool, repo: str, self_id: str) -> dict:
	errors: list[str] = []
	open_issues, projects = scan_logs(repo, errors)
	return {
		"repo": repo,
		"self": self_id,
		"sessions": [session_view(raw) for raw in sessions_raw],
		"triggers": [trigger_view(raw) for raw in triggers_raw],
		"triggers_has_more": triggers_has_more,
		"open_issues": open_issues,
		"log_projects": projects,
		"errors": errors,
	}


# --- CLI ----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	scan = sub.add_parser("scan", help="read the inputs, write the state file, and list the get_session lookups")
	scan.add_argument("--sessions-file", required=True, help="saved list_sessions result (mine: true, limit: 100)")
	scan.add_argument("--triggers-file", required=True, help="saved list_triggers result (enabled: true, limit: 100)")
	scan.add_argument("--repo", required=True, help="owner/repo whose open ai:claude issues are scanned")
	scan.add_argument("--self", default="", help="the pickup's own session id (never counted as project activity)")
	scan.add_argument("--state-out", required=True, help="where to write the state for `decide`")
	decide_parser = sub.add_parser("decide", help="print the restarts and re-queues")
	decide_parser.add_argument("--state", required=True, help="the state file `scan` wrote")
	decide_parser.add_argument("--lookup-file", default="", help="JSON object {session id: get_session result | \"not_found\" | {\"error\": …}}")
	return parser


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
	args = build_parser().parse_args(argv)
	now = now or dt.datetime.now(dt.timezone.utc)
	if args.command == "scan":
		try:
			sessions_raw = load_sessions(args.sessions_file)
			triggers_raw, has_more = load_triggers(args.triggers_file)
		except (OSError, ValueError) as exc:
			print(json.dumps({"lookup": [], "error": f"cannot read inputs: {exc}"}))
			return 2
		state = build_state(sessions_raw, triggers_raw, has_more, args.repo, args.self)
		lookup = lookups_needed(state, now)
		try:
			with open(args.state_out, "w", encoding="utf-8") as handle:
				json.dump(state, handle)
		except OSError as exc:
			print(json.dumps({"lookup": [], "error": f"cannot write --state-out: {exc}"}))
			return 2
		print(
			json.dumps(
				{
					"lookup": lookup,
					"candidates": len(collect_candidates(state)),
					"state": args.state_out,
					"log_projects": len(state["log_projects"]),
					"errors": state["errors"],
				}
			)
		)
		return 0
	try:
		with open(args.state, encoding="utf-8") as handle:
			state = json.load(handle)
		if not isinstance(state, dict) or not isinstance(state.get("sessions"), list):
			raise ValueError("not a state file written by `scan`")
		lookups = load_lookups(args.lookup_file)
	except (OSError, ValueError) as exc:
		print(json.dumps({"restart": [], "requeue": [], "error": f"cannot read inputs: {exc}"}))
		return 2
	print(json.dumps(decide(state, lookups, now), ensure_ascii=False))
	return 0


if __name__ == "__main__":
	sys.exit(main())

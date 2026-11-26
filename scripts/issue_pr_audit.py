#!/usr/bin/env python3
"""Daily issue/PR audit: data collection, rule-based tier, state and Telegram (#10153).

Run by ``scripts/issue_pr_audit_run.sh`` from the ``collect-logs`` job of
``.github/workflows/workflow-log-analysis.yml`` (06:00 UTC schedule, or a
``workflow_dispatch`` with ``issue_pr_audit=true``). Every subcommand reads
and writes files under ``--workdir`` (default ``$RUNNER_TEMP/issue-pr-audit``).
Issue and pull request text is untrusted: it is never printed to the log, and
it reaches the model input only inside ``<untrusted_external_data>`` blocks.

Subcommands
-----------
``collect``      discover repositories, read their issues and PRs, fetch
                 details, blocked-item log tails and dedupe sources. Writes
                 ``raw.json``. Needs ``GH_TOKEN`` (the pipeline PAT).
``classify``     tier-1 rules over ``raw.json`` plus the default-branch state
                 file. Writes ``digest.json`` and ``state-next.json``.
``render-input`` ``digest.json`` -> ``model-input.md`` (trusted authors' text
                 only, wrapped, private repositories redacted).
``finalize``     wrap the model's report: trusted first-line marker and
                 ``## Metrics Appendix`` (D4).
``state-update`` ``state-next.json`` -> the committed state file.
``purge``        list old audit reports the retention purge may delete (D1).
``telegram``     render the daily blocked-items report into part files.

GitHub API budget (CLAUDE.md §14 / §15), per run
------------------------------------------------
* 1 ``GET /user`` (pipeline login), 1+ paginated ``GET /user/repos`` pages.
* Per audited repository: the window listing (``issues?state=all&since=``,
  ascending; ``pulls?state=all`` descending for ``has_issues=false``), at
  most ``ISSUE_PR_AUDIT_MAX_WINDOW_PAGES``-capped pages, usually 1 call; the
  open snapshot (``issues?state=open``), at most ``ISSUE_PR_AUDIT_MAX_OPEN_PAGES``
  pages, skipped when the listing's ``open_issues_count`` is 0 (``pulls?state=open``
  for ``has_issues=false``, whose issues endpoint answers 410). A capped
  snapshot is named under Coverage and its known blocked entries are kept.
* Details: one aliased GraphQL query per 25 items (input: ``repo#number``
  keys; output: ``{key: node}``), at most ``ISSUE_PR_AUDIT_MAX_DETAIL_ITEMS``
  items, so ``ceil(K / 25)`` calls. Same pattern as
  ``_fetch_candidate_issue_details_graphql`` in scripts/orchestrate_poll_process.sh.
  A failed batch spanning several repositories is retried once per repository.
* Blocked log tails: at most ``ISSUE_PR_AUDIT_MAX_BLOCKED_LOGS`` new blocked
  items, 2 calls each (``runs/{id}/jobs``, ``jobs/{id}/logs``).
* Dedupe: 1 listing (at most ``HEAL_LIST_MAX_PAGES`` = 5 pages) of open
  ``ai:workflow-heal`` issues in the host repository.
* Open earlier audit report PRs (``open_earlier_reports``): 1 open-PR listing
  of the host (at most ``EARLIER_REPORT_LIST_PAGES`` = 3 pages), then 1 files
  listing and 1 contents read per matching PR, at most
  ``EARLIER_REPORT_MAX_PRS`` = 3 PRs tried, so up to 9 calls.
* Retries: every API attempt, retries included, is counted.

Fail open per repository: a listing or detail error marks the repository
``read_failed``. It keeps its old cursor (the next run re-reads the window)
and is listed under the report's Coverage gaps. A failed ``/user/repos``
falls back to the host repository alone. Log tails and the heal listing are
optional: their failure is counted, nothing else changes.

Transport: ``gh api`` with the retry and backoff shape of ``gh_api_json`` in
scripts/collect_workflow_logs.py. That helper accepts JSON objects only and
the listings here are arrays, so this module carries its own small wrapper.

Log lines (stable prefixes): ``ISSUE_PR_AUDIT_REPOS``, ``ISSUE_PR_AUDIT_CURSOR``,
``ISSUE_PR_AUDIT_API``, ``ISSUE_PR_AUDIT_REPO``, ``ISSUE_PR_AUDIT_PURGE``.
Private repository names are printed redacted when the host repository is
public (D7), because the host's Actions logs are public too.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

STATE_VERSION = 1
STATE_PATH = "analysis/issue_pr_audit_state.json"
REPORT_GLOB = "issue-pr-audit-*.md"
REPORT_NAME_RE = re.compile(r"^issue-pr-audit-([0-9]{4}-[0-9]{2}-[0-9]{2})(-[0-9]+)?\.md$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GRAPHQL_BATCH = 25
HEAL_LIST_MAX_PAGES = 5
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
HARD_BLOCK_LABELS = ("ai:needs-human", "ai:scope-blocked", "ai:destructive-blocked")
NEVER_BLOCKED_LABEL = "ai:waiting-on-dependency"
DEFAULT_BLOCKED_LABELS = (
	"ai:needs-human,ai:blocked,ai:scope-blocked,ai:destructive-blocked,ai:review-blocked,"
	"ai:implementation-failed,ai:plan-failed,ai:clarify-failed,ai:validation-failed,"
	"ai:final-ci-failed,ai:security-pass-failed,ai:integration-judge-failed"
)
FIX_TITLE_RE = re.compile(r"\b(fix|fixes|fixed|revert|hotfix)\b", re.IGNORECASE)
FAILED_LABEL_RE = re.compile(r"^ai:.+-failed$")
GENERATED_CHORE_REF_RE = re.compile(r"^auto/(workflow-log-analysis-report|validation-selftest-status|issue-pr-audit-report)-[0-9]+-[0-9]+$")
RUN_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/([0-9]{1,20})")
SECRET_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
UNTRUSTED_TAG_RE = re.compile(r"<(/?)untrusted_external_data", re.IGNORECASE)
RETRY_MARKERS = ("rate limit", "secondary rate", "timed out", "timeout", "502", "503", "504", "500", "connection reset", "eof")
TG_PART_LIMIT = 3900
TG_MAX_ITEMS = 50
TELEGRAM_TITLE_MAX = 60
COMMENT_MAX = 2000
BODY_MAX = 3000
TITLE_MAX = 200
LOG_TAIL_LINES = 200
LOG_TAIL_MAX_CHARS = 8000
MODEL_INPUT_MAX_BYTES = 600_000
EARLIER_REPORT_REF_RE = re.compile(r"^auto/issue-pr-audit-report-[0-9]+-[0-9]+$")
EARLIER_REPORT_ITEM_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[0-9]+|private:[0-9a-f]{8}#[0-9]+")
EARLIER_REPORT_LIST_PAGES = 3
EARLIER_REPORT_MAX_PRS = 3
EARLIER_REPORT_MAX_ITEMS = 300
REPORT_HEADINGS = (
	"Executive Summary",
	"Coverage",
	"Unnecessary Items",
	"Incorrectly Resolved Items",
	"Blocked Items",
	"Fix Plans",
	"Observations",
	"Clean Items",
	"Metrics Appendix",
)


class ApiError(RuntimeError):
	"""A GitHub API call failed after its retries."""


# --- settings ------------------------------------------------------------------


def _env_int(env: dict[str, str], name: str, default: int, low: int = 0, high: int = 1_000_000) -> int:
	raw = (env.get(name) or "").strip()
	if not re.fullmatch(r"[0-9]{1,7}", raw):
		return default
	value = int(raw)
	return value if low <= value <= high else default


def _env_bool(env: dict[str, str], name: str, default: bool) -> bool:
	raw = (env.get(name) or "").strip().lower()
	if raw in ("true", "1", "yes", "on"):
		return True
	if raw in ("false", "0", "no", "off"):
		return False
	return default


def load_settings(env: dict[str, str] | None = None) -> dict[str, Any]:
	"""Every ``ISSUE_PR_AUDIT_*`` knob with its default; invalid values fall back."""
	env = dict(os.environ if env is None else env)
	labels = env.get("ISSUE_PR_AUDIT_BLOCKED_LABELS") or DEFAULT_BLOCKED_LABELS
	exclude = env.get("ISSUE_PR_AUDIT_EXCLUDE_REPOS") or ""
	return {
		"initial_lookback_hours": _env_int(env, "ISSUE_PR_AUDIT_INITIAL_LOOKBACK_HOURS", 24, 1, 24 * 90),
		"max_window_days": _env_int(env, "ISSUE_PR_AUDIT_MAX_WINDOW_DAYS", 7, 1, 90),
		"exclude_repos": {item.lower() for item in re.split(r"[,\s]+", exclude) if REPO_RE.match(item)},
		"max_repos": _env_int(env, "ISSUE_PR_AUDIT_MAX_REPOS", 200, 1, 5000),
		"max_open_pages": _env_int(env, "ISSUE_PR_AUDIT_MAX_OPEN_PAGES", 5, 0, 100),
		"max_window_pages": _env_int(env, "ISSUE_PR_AUDIT_MAX_WINDOW_PAGES", 10, 1, 100),
		"max_detail_items": _env_int(env, "ISSUE_PR_AUDIT_MAX_DETAIL_ITEMS", 500, 0, 10000),
		"max_deep_items": _env_int(env, "ISSUE_PR_AUDIT_MAX_DEEP_ITEMS", 80, 0, 1000),
		"clean_sample": _env_int(env, "ISSUE_PR_AUDIT_CLEAN_SAMPLE", 10, 0, 500),
		"commits_flag": _env_int(env, "ISSUE_PR_AUDIT_COMMITS_FLAG", 8, 1, 10000),
		"stall_hours": _env_int(env, "ISSUE_PR_AUDIT_STALL_HOURS", 24, 1, 24 * 90),
		"blocked_labels": tuple(label for label in (part.strip() for part in labels.split(",")) if label),
		"max_blocked_logs": _env_int(env, "ISSUE_PR_AUDIT_MAX_BLOCKED_LOGS", 10, 0, 100),
		"tg_when_empty": _env_bool(env, "ISSUE_PR_AUDIT_TG_WHEN_EMPTY", False),
		"redact_private_in_public": _env_bool(env, "ISSUE_PR_AUDIT_REDACT_PRIVATE_IN_PUBLIC", True),
	}


# --- time helpers --------------------------------------------------------------


def parse_time(value: Any) -> dt.datetime | None:
	if not isinstance(value, str) or not value:
		return None
	text = value.strip()
	if text.endswith(("Z", "z")):
		text = text[:-1] + "+00:00"
	try:
		parsed = dt.datetime.fromisoformat(text)
	except ValueError:
		return None
	if parsed.tzinfo is None:
		return None
	return parsed.astimezone(dt.timezone.utc)


def fmt_time(value: dt.datetime) -> str:
	return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now() -> dt.datetime:
	return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def format_age(since: dt.datetime | None, now: dt.datetime) -> str:
	if since is None:
		return "age unknown"
	hours = max(int((now - since).total_seconds() // 3600), 0)
	days, rest = divmod(hours, 24)
	return f"{days}d {rest}h" if days else f"{rest}h"


# --- redaction -----------------------------------------------------------------


def repo_hash(repo: str) -> str:
	return hashlib.sha256(repo.lower().encode("utf-8")).hexdigest()[:8]


def should_redact(repo_meta: dict[str, Any], host_private: bool, settings: dict[str, Any]) -> bool:
	"""D7: a private repository's items are redacted in a public host's report."""
	return bool(repo_meta.get("private")) and not host_private and bool(settings["redact_private_in_public"]) and not repo_meta.get("host")


def repo_display(repo: str, redacted: bool) -> str:
	return f"private:{repo_hash(repo)}" if redacted else repo


def item_key(repo: str, number: int) -> str:
	return f"{repo}#{number}"


# --- transport -----------------------------------------------------------------


class GhTransport:
	"""``gh api`` with bounded retries; counts calls for ``ISSUE_PR_AUDIT_API``."""

	def __init__(self, retries: int = 3, backoff: float = 2.0, sleep: Callable[[float], None] = time.sleep) -> None:
		self.retries = retries
		self.backoff = backoff
		self.sleep = sleep
		self.counts = {"rest": 0, "graphql": 0, "failed": 0}

	def _run(self, args: list[str], kind: str) -> str:
		last = ""
		for attempt in range(1, self.retries + 1):
			# Counted per attempt, so retries show up in the Metrics Appendix.
			self.counts[kind] += 1
			try:
				proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=120)
			except (OSError, subprocess.TimeoutExpired) as exc:
				last = type(exc).__name__
			else:
				if proc.returncode == 0:
					return proc.stdout
				last = (proc.stderr or proc.stdout or "").strip()
				if not any(marker in last.lower() for marker in RETRY_MARKERS):
					break
			if attempt < self.retries:
				self.sleep(self.backoff * attempt)
		self.counts["failed"] += 1
		# Never echo the response body: it may carry untrusted text.
		status = re.search(r"\bHTTP ([0-9]{3})\b", last)
		raise ApiError(f"gh_api_failed status={status.group(1) if status else 'unknown'}")

	def rest(self, endpoint: str) -> Any:
		text = self._run([endpoint, "-H", "Accept: application/vnd.github+json"], "rest")
		try:
			return json.loads(text) if text.strip() else None
		except ValueError as exc:
			self.counts["failed"] += 1
			raise ApiError("gh_api_failed status=invalid_json") from exc

	def raw(self, endpoint: str) -> str:
		return self._run([endpoint], "rest")

	def graphql(self, query: str) -> dict[str, Any]:
		text = self._run(["graphql", "-f", f"query={query}"], "graphql")
		try:
			payload = json.loads(text)
		except ValueError as exc:
			self.counts["failed"] += 1
			raise ApiError("gh_api_failed status=invalid_json") from exc
		if not isinstance(payload, dict):
			raise ApiError("gh_api_failed status=invalid_json")
		return payload


def paginate(transport: Any, endpoint: str, max_pages: int, stop: Callable[[list[dict[str, Any]]], bool] | None = None) -> tuple[list[dict[str, Any]], bool]:
	"""Pages of a list endpoint. Returns ``(items, truncated)``."""
	items: list[dict[str, Any]] = []
	sep = "&" if "?" in endpoint else "?"
	for page in range(1, max_pages + 1):
		data = transport.rest(f"{endpoint}{sep}page={page}")
		if not isinstance(data, list):
			raise ApiError("gh_api_failed status=not_a_list")
		rows = [row for row in data if isinstance(row, dict)]
		items.extend(rows)
		if len(data) < 100:
			return items, False
		if stop is not None and stop(rows):
			return items, False
	return items, True


# --- discovery -----------------------------------------------------------------


def discover_repos(transport: Any, host: str, settings: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
	"""Every non-archived repository the token reaches, plus the host repository.

	Output: ``([{name, private, open_issues_count, has_issues, host}], stats)``.
	The host comes first; the rest are sorted by name and capped at
	``max_repos`` (``truncated`` counts the rest). On a listing failure only
	the host is returned with ``fallback=self``.
	"""
	stats = {"discovered": 0, "skipped_archived": 0, "excluded": 0, "truncated": 0, "fallback": ""}
	host_meta: dict[str, Any] = {"name": host, "private": None, "open_issues_count": None, "has_issues": True, "host": True}
	others: dict[str, dict[str, Any]] = {}
	try:
		rows, _ = paginate(transport, "user/repos?affiliation=owner,collaborator,organization_member&per_page=100", 50)
	except ApiError:
		stats["fallback"] = "self"
		rows = []
	for row in rows:
		name = row.get("full_name")
		if not isinstance(name, str) or not REPO_RE.match(name):
			continue
		stats["discovered"] += 1
		meta = {
			"name": name,
			"private": row.get("private") is True,
			"open_issues_count": row.get("open_issues_count") if isinstance(row.get("open_issues_count"), int) else None,
			"has_issues": row.get("has_issues") is not False,
			"host": False,
		}
		if name.lower() == host.lower():
			host_meta.update({key: meta[key] for key in ("private", "open_issues_count", "has_issues")})
			continue
		if row.get("archived") is True:
			stats["skipped_archived"] += 1
			continue
		if name.lower() in settings["exclude_repos"]:
			stats["excluded"] += 1
			continue
		others[name.lower()] = meta
	ordered = [others[key] for key in sorted(others)]
	room = max(settings["max_repos"] - 1, 0)
	stats["truncated"] = max(len(ordered) - room, 0)
	repos = [host_meta, *ordered[:room]]
	if host_meta["private"] is None:
		# Unknown host visibility counts as public, so redaction stays on.
		host_meta["private"] = False
	stats["audited"] = len(repos)
	return repos, stats


# --- items ---------------------------------------------------------------------


def normalize_item(repo: str, row: dict[str, Any]) -> dict[str, Any] | None:
	"""One REST issue or pull object to the fields the audit keeps."""
	number = row.get("number")
	if not isinstance(number, int):
		return None
	is_pr = isinstance(row.get("pull_request"), dict) or "head" in row
	merged_at = row.get("merged_at")
	if isinstance(row.get("pull_request"), dict):
		merged_at = row["pull_request"].get("merged_at", merged_at)
	user = row.get("user") if isinstance(row.get("user"), dict) else {}
	labels = [label.get("name") for label in row.get("labels") or [] if isinstance(label, dict) and isinstance(label.get("name"), str)]
	head = row.get("head") if isinstance(row.get("head"), dict) else {}
	body = row.get("body") if isinstance(row.get("body"), str) else ""
	return {
		"repo": repo,
		"number": number,
		"url": row.get("html_url") if isinstance(row.get("html_url"), str) else f"https://github.com/{repo}/{'pull' if is_pr else 'issues'}/{number}",
		"title": row.get("title") if isinstance(row.get("title"), str) else "",
		"is_pr": is_pr,
		"state": row.get("state") if row.get("state") in ("open", "closed") else "open",
		"state_reason": row.get("state_reason") if isinstance(row.get("state_reason"), str) else "",
		"author": user.get("login") if isinstance(user.get("login"), str) else "",
		"author_association": row.get("author_association") if isinstance(row.get("author_association"), str) else "NONE",
		"created_at": row.get("created_at") or "",
		"updated_at": row.get("updated_at") or "",
		"closed_at": row.get("closed_at") or "",
		"merged_at": merged_at or "",
		"labels": sorted(set(labels)),
		"comments": row.get("comments") if isinstance(row.get("comments"), int) else 0,
		"head_ref": head.get("ref") if isinstance(head.get("ref"), str) else "",
		"body": body[:BODY_MAX * 2],
	}


def resolve_cursor(state: dict[str, Any], key: str, now: dt.datetime, settings: dict[str, Any]) -> tuple[dt.datetime, bool, str]:
	"""``(cursor, clamped, source)``: stored cursor, else ``now - lookback``; clamped to the max window."""
	entry = (state.get("repos") or {}).get(key) if isinstance(state.get("repos"), dict) else None
	cursor = parse_time(entry.get("cursor")) if isinstance(entry, dict) else None
	source = "state"
	if cursor is None or cursor > now:
		cursor = now - dt.timedelta(hours=settings["initial_lookback_hours"])
		source = "initial"
	floor = now - dt.timedelta(days=settings["max_window_days"])
	if cursor < floor:
		return floor, True, source
	return cursor, False, source


def list_window(transport: Any, repo: dict[str, Any], cursor: dt.datetime, settings: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
	"""Items updated since ``cursor``; second value: the next cursor when the listing was capped."""
	name = repo["name"]
	since = fmt_time(cursor)
	if repo.get("has_issues", True):
		rows, truncated = paginate(transport, f"repos/{name}/issues?state=all&since={since}&sort=updated&direction=asc&per_page=100", settings["max_window_pages"])
	else:
		def older(rows: list[dict[str, Any]]) -> bool:
			stamps = [parse_time(row.get("updated_at")) for row in rows]
			return any(stamp is not None and stamp < cursor for stamp in stamps)

		rows, truncated = paginate(transport, f"repos/{name}/pulls?state=all&sort=updated&direction=desc&per_page=100", settings["max_window_pages"], older)
		rows = [row for row in rows if (parse_time(row.get("updated_at")) or cursor) >= cursor]
	items = [item for item in (normalize_item(name, row) for row in rows) if item]
	partial_cursor = None
	if truncated and repo.get("has_issues", True) and items:
		# Ascending order: the last item read is where the next run resumes.
		partial_cursor = max((item["updated_at"] for item in items if parse_time(item["updated_at"])), default=None)
	elif truncated:
		# Descending PR listing: the unread items are the oldest ones, so the
		# cursor stays where it was (overlap, never a gap; the clamp bounds it).
		partial_cursor = since
	return items, partial_cursor


def list_open(transport: Any, repo: dict[str, Any], settings: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
	"""Open items for blocked detection: ``(items, truncated)``.

	A repository with issues disabled answers 410 on the issues endpoint, so
	its open pull requests are listed through ``pulls?state=open`` instead.
	"""
	if repo.get("open_issues_count") == 0:
		return [], False
	if settings["max_open_pages"] == 0:
		# Nothing was read, so the snapshot is not complete: classify keeps
		# the repository's known blocked entries instead of dropping them.
		return [], True
	kind = "issues" if repo.get("has_issues", True) else "pulls"
	rows, truncated = paginate(transport, f"repos/{repo['name']}/{kind}?state=open&per_page=100", settings["max_open_pages"])
	return [item for item in (normalize_item(repo["name"], row) for row in rows) if item], truncated


# --- GraphQL details -----------------------------------------------------------

_DETAIL_FIELDS = """
      __typename
      ... on PullRequest {
        changedFiles additions deletions mergeable headRefName
        commits { totalCount }
        reviews { totalCount }
        statusRollup: commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
        closingIssuesReferences(first: 5) { nodes { number } }
        timelineItems(last: 40, itemTypes: [REOPENED_EVENT, CLOSED_EVENT, MERGED_EVENT, LABELED_EVENT, UNLABELED_EVENT, CROSS_REFERENCED_EVENT]) {
          nodes {
            __typename
            ... on ReopenedEvent { createdAt }
            ... on ClosedEvent { createdAt closer { __typename } }
            ... on MergedEvent { createdAt }
            ... on LabeledEvent { createdAt label { name } }
            ... on UnlabeledEvent { createdAt label { name } }
            ... on CrossReferencedEvent { createdAt source { __typename ... on Issue { number title createdAt url } ... on PullRequest { number title createdAt url } } }
          }
        }
        comments(last: 5) { nodes { authorAssociation createdAt body author { login } } }
      }
      ... on Issue {
        issueTimelineItems: timelineItems(last: 40, itemTypes: [REOPENED_EVENT, CLOSED_EVENT, LABELED_EVENT, UNLABELED_EVENT, CROSS_REFERENCED_EVENT]) {
          nodes {
            __typename
            ... on ReopenedEvent { createdAt }
            ... on ClosedEvent { createdAt closer { __typename } }
            ... on LabeledEvent { createdAt label { name } }
            ... on UnlabeledEvent { createdAt label { name } }
            ... on CrossReferencedEvent { createdAt source { __typename ... on Issue { number title createdAt url } ... on PullRequest { number title createdAt url } } }
          }
        }
        comments(last: 5) { nodes { authorAssociation createdAt body author { login } } }
      }
"""
# IssueOrPullRequest is a union: every field except __typename sits inside a
# type fragment, and MERGED_EVENT exists only on pull request timelines. The
# Issue timeline is aliased (issueTimelineItems) so the two selections never
# need to merge; parse_detail_node reads either.


def build_detail_query(keys: list[str]) -> str:
	"""One aliased query for up to 25 ``owner/repo#n`` keys (aliases ``i0``…)."""
	parts = []
	for index, key in enumerate(keys):
		repo, number = key.rsplit("#", 1)
		owner, name = repo.split("/", 1)
		if not REPO_RE.match(repo) or not number.isdigit():
			raise ValueError(f"invalid item key {key!r}")
		parts.append(
			f"  i{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
			f"    issueOrPullRequest(number: {int(number)}) {{{_DETAIL_FIELDS}    }}\n  }}"
		)
	return "query {\n" + "\n".join(parts) + "\n}"


def parse_detail_node(node: Any) -> dict[str, Any] | None:
	if not isinstance(node, dict):
		return None
	detail: dict[str, Any] = {"type": node.get("__typename") or ""}
	if detail["type"] == "PullRequest":
		rollup = None
		for commit_node in ((node.get("statusRollup") or {}).get("nodes") or []):
			rollup = (((commit_node or {}).get("commit") or {}).get("statusCheckRollup") or {}).get("state")
		detail.update({
			"changed_files": node.get("changedFiles") if isinstance(node.get("changedFiles"), int) else None,
			"additions": node.get("additions"),
			"deletions": node.get("deletions"),
			"commits": ((node.get("commits") or {}).get("totalCount")),
			"reviews": ((node.get("reviews") or {}).get("totalCount")),
			"mergeable": node.get("mergeable") or "",
			"rollup": rollup or "",
			"head_ref": node.get("headRefName") or "",
			"closing_issues": [n.get("number") for n in ((node.get("closingIssuesReferences") or {}).get("nodes") or []) if isinstance(n, dict)],
		})
	events = []
	for event in ((node.get("timelineItems") or node.get("issueTimelineItems") or {}).get("nodes") or []):
		if not isinstance(event, dict):
			continue
		entry = {"type": event.get("__typename") or "", "at": event.get("createdAt") or ""}
		if isinstance(event.get("label"), dict):
			entry["label"] = event["label"].get("name") or ""
		if isinstance(event.get("closer"), dict):
			entry["closer"] = event["closer"].get("__typename") or ""
		source = event.get("source")
		if isinstance(source, dict):
			entry["source"] = {
				"type": source.get("__typename") or "",
				"title": source.get("title") or "",
				"created_at": source.get("createdAt") or "",
				"url": source.get("url") or "",
			}
		events.append(entry)
	detail["events"] = events
	comments = []
	for comment in ((node.get("comments") or {}).get("nodes") or []):
		if not isinstance(comment, dict):
			continue
		comments.append({
			"author": ((comment.get("author") or {}).get("login") or ""),
			"association": comment.get("authorAssociation") or "NONE",
			"at": comment.get("createdAt") or "",
			"body": (comment.get("body") or "")[:COMMENT_MAX],
		})
	detail["comments"] = comments
	return detail


def fetch_details(transport: Any, keys: list[str]) -> tuple[dict[str, dict[str, Any]], set[str]]:
	"""``ceil(len(keys) / 25)`` GraphQL calls (plus one per repository of a failed
	multi-repository batch). Returns ``(details, failed_repos)``."""
	details: dict[str, dict[str, Any]] = {}
	failed: set[str] = set()
	queue = [keys[start:start + GRAPHQL_BATCH] for start in range(0, len(keys), GRAPHQL_BATCH)]
	while queue:
		batch = queue.pop(0)
		try:
			payload = transport.graphql(build_detail_query(batch))
		except ApiError:
			repos = list(dict.fromkeys(key.rsplit("#", 1)[0] for key in batch))
			if len(repos) > 1:
				# One repository's error (NOT_FOUND, SAML) must not fail the
				# others: retry the batch once per repository.
				queue.extend([key for key in batch if key.rsplit("#", 1)[0] == repo] for repo in repos)
				continue
			failed.update(repos)
			continue
		data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
		for index, key in enumerate(batch):
			repo_node = data.get(f"i{index}")
			node = repo_node.get("issueOrPullRequest") if isinstance(repo_node, dict) else None
			parsed = parse_detail_node(node)
			if parsed is not None:
				details[key] = parsed
	return details, failed


# --- attribution and rules -----------------------------------------------------


def is_trusted_author(login: str, association: str, pipeline_login: str) -> bool:
	return association in TRUSTED_ASSOCIATIONS or (bool(pipeline_login) and login == pipeline_login)


def creating_automation(item: dict[str, Any], detail: dict[str, Any] | None) -> str:
	"""Which automation created the item, inferred from branch, labels and markers; ``""`` for a person."""
	head = (detail or {}).get("head_ref") or item.get("head_ref") or ""
	labels = set(item.get("labels") or [])
	body = item.get("body") or ""
	if head.startswith("ai/issue-"):
		return "ai-implement"
	match = re.match(r"^auto/([a-z0-9-]+?)(?:-[0-9]+)*$", head)
	if head.startswith("auto/"):
		return f"auto:{match.group(1) if match else 'branch'}"
	if "ai-orchestrator-provenance" in body or "ai:orchestrator-managed" in labels or "ai:orchestrator-tracking" in labels:
		return "orchestrator"
	if "ai:workflow-heal" in labels:
		heal = re.search(r"source=([a-z0-9_-]{1,40})", body)
		return f"workflow-heal:{heal.group(1)}" if heal else "workflow-heal"
	if item.get("author", "").endswith("[bot]"):
		return item["author"]
	return ""


def normalize_title(title: str) -> str:
	text = re.sub(r"[^a-z0-9]+", " ", (title or "").lower())
	return re.sub(r"\s+", " ", text).strip()


def reopened_since(detail: dict[str, Any] | None, cursor: dt.datetime) -> bool:
	"""A ReopenedEvent at or after ``cursor`` (an older item reopened in the window)."""
	return any(
		event.get("type") == "ReopenedEvent" and (parse_time(event.get("at")) or cursor - dt.timedelta(seconds=1)) >= cursor
		for event in (detail or {}).get("events") or []
	)


def in_window(item: dict[str, Any], cursor: dt.datetime) -> bool:
	"""Closed, merged or created since ``cursor`` (only these get tier-1 / deep review)."""
	for field in ("created_at", "closed_at", "merged_at"):
		stamp = parse_time(item.get(field))
		if stamp is not None and stamp >= cursor:
			return True
	return False


def labels_at_close(labels: set[str], events: list[dict[str, Any]], closed_at: dt.datetime | None) -> set[str]:
	"""The label set when the item closed: label events after ``closed_at`` are undone, newest first."""
	result = set(labels)
	if closed_at is None:
		return result
	later = [event for event in events if event.get("type") in ("LabeledEvent", "UnlabeledEvent") and (parse_time(event.get("at")) or closed_at) > closed_at]
	for event in sorted(later, key=lambda event: event.get("at") or "", reverse=True):
		if event.get("type") == "LabeledEvent":
			result.discard(event.get("label") or "")
		elif event.get("label"):
			result.add(event["label"])
	return result


def waste_signals(item: dict[str, Any], detail: dict[str, Any] | None, peers: list[dict[str, Any]], settings: dict[str, Any]) -> list[str]:
	"""Tier-1 waste signals of one in-window item (see the issue's list)."""
	signals: list[str] = []
	detail = detail or {}
	events = detail.get("events") or []
	labels = set(item.get("labels") or [])
	closed = item.get("state") == "closed"
	created = parse_time(item.get("created_at"))
	closed_at = parse_time(item.get("closed_at"))
	merged_at = parse_time(item.get("merged_at"))
	automation = creating_automation(item, detail)
	if item["is_pr"] and closed and merged_at is None:
		signals.append("closed_unmerged_pr")
	if not item["is_pr"] and item.get("state_reason") == "not_planned":
		signals.append("issue_not_planned")
	if item.get("state_reason") == "duplicate" or "duplicate" in labels:
		signals.append("issue_duplicate")
	title_key = normalize_title(item.get("title", ""))
	if title_key and created is not None:
		for peer in peers:
			if peer is item or peer.get("number") == item.get("number"):
				continue
			peer_created = parse_time(peer.get("created_at"))
			if peer_created is not None and abs((peer_created - created).total_seconds()) <= 7 * 86400 and normalize_title(peer.get("title", "")) == title_key:
				signals.append("duplicate_title")
				break
	if any(event.get("type") == "ReopenedEvent" for event in events):
		signals.append("reopened")
	if item["is_pr"] and ((item.get("title") or "").startswith("Revert") or "This reverts commit" in (item.get("body") or "")):
		signals.append("revert_pr")
	if item["is_pr"] and merged_at is not None:
		for event in events:
			source = event.get("source") or {}
			source_created = parse_time(source.get("created_at"))
			if event.get("type") == "CrossReferencedEvent" and source_created is not None \
				and merged_at <= source_created <= merged_at + dt.timedelta(hours=72) and FIX_TITLE_RE.search(source.get("title") or ""):
				signals.append("follow_up_fix")
				break
	commits = detail.get("commits")
	reviews = detail.get("reviews")
	if (isinstance(commits, int) and commits > settings["commits_flag"]) or (isinstance(reviews, int) and reviews > 6):
		signals.append("many_review_rounds")
	closers = [event.get("closer") for event in events if event.get("type") == "ClosedEvent"]
	closed_by_pr = any(closer == "PullRequest" for closer in closers)
	if item["is_pr"] and detail.get("changed_files") == 0:
		signals.append("noop")
	elif not item["is_pr"] and closed and automation and item.get("state_reason") == "completed" and not closed_by_pr:
		signals.append("noop")
	if not item["is_pr"] and closed and automation and created and closed_at and (closed_at - created) <= dt.timedelta(minutes=30) and not closed_by_pr:
		signals.append("fast_close")
	if closed and created and closed_at and any(label.startswith("ai:") for label in labels) and (closed_at - created) > dt.timedelta(hours=72):
		signals.append("long_cycle")
	if closed and any(FAILED_LABEL_RE.match(label) for label in labels_at_close(labels, events, closed_at)):
		signals.append("failure_label_at_close")
	if sum(1 for event in events if event.get("type") in ("LabeledEvent", "UnlabeledEvent")) > 20:
		signals.append("label_churn")
	return signals


def clean_category(item: dict[str, Any], detail: dict[str, Any] | None) -> str:
	head = (detail or {}).get("head_ref") or item.get("head_ref") or ""
	if GENERATED_CHORE_REF_RE.match(head):
		return "generated_chore_clean"
	automation = creating_automation(item, detail)
	who = re.sub(r"[^a-z0-9]+", "_", automation.lower()).strip("_") if automation else "human"
	return f"{who}_{'pr' if item['is_pr'] else 'issue'}_clean"


def blocked_latch(item: dict[str, Any], detail: dict[str, Any] | None, now: dt.datetime, settings: dict[str, Any]) -> str:
	"""Why an open item is blocked with no path to completion; ``""`` when it is not."""
	if item.get("state") != "open":
		return ""
	labels = set(item.get("labels") or [])
	if NEVER_BLOCKED_LABEL in labels:
		return ""
	hard = sorted(labels.intersection(HARD_BLOCK_LABELS))
	if hard:
		return "label:" + ",".join(hard)
	updated = parse_time(item.get("updated_at"))
	stalled = updated is not None and (now - updated) >= dt.timedelta(hours=settings["stall_hours"])
	if not stalled:
		return ""
	listed = sorted(labels.intersection(settings["blocked_labels"]))
	if listed:
		return "stalled_label:" + ",".join(listed)
	detail = detail or {}
	if item["is_pr"] and detail.get("mergeable") == "CONFLICTING":
		return "stalled_pr:conflict"
	if item["is_pr"] and detail.get("rollup") in ("FAILURE", "ERROR"):
		return "stalled_pr:checks_failing"
	pipeline = sorted(label for label in labels if label.startswith("ai:"))
	if pipeline:
		return "stalled_pipeline:" + ",".join(pipeline)
	return ""


def blocked_fingerprint(item: dict[str, Any], latch: str) -> str:
	text = ",".join(sorted(item.get("labels") or [])) + "|" + latch
	return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def clean_sample(keys: list[str], run_id: int, size: int) -> list[str]:
	"""Seeded by the run id, so a re-run samples the same clean items."""
	ordered = sorted(keys)
	if size <= 0 or not ordered:
		return []
	return sorted(random.Random(run_id).sample(ordered, min(size, len(ordered))))


# --- collect -------------------------------------------------------------------


def _detail_priority(item: dict[str, Any], cursor: dt.datetime) -> int:
	if in_window(item, cursor):
		return 0
	if any(label.startswith("ai:") for label in item.get("labels") or []):
		return 1
	if item["is_pr"] and item.get("state") == "open":
		return 2
	updated = parse_time(item.get("updated_at"))
	if item.get("state") == "open" and updated is not None and updated >= cursor:
		# An older open item updated in the window may have been reopened;
		# classify needs its timeline (reopened_since) to tell.
		return 2
	return 3


def _run_id_from_comments(item: dict[str, Any], detail: dict[str, Any], pipeline_login: str) -> int | None:
	for comment in reversed(detail.get("comments") or []):
		if not is_trusted_author(comment.get("author", ""), comment.get("association", ""), pipeline_login):
			continue
		for repo, run_id in RUN_URL_RE.findall(comment.get("body") or ""):
			if repo.lower() == item["repo"].lower():
				return int(run_id)
	return None


def tail_log(text: str) -> str:
	lines = [SECRET_RE.sub("[redacted]", CONTROL_RE.sub("", line))[:500] for line in (text or "").splitlines()[-LOG_TAIL_LINES:]]
	return "\n".join(lines)[-LOG_TAIL_MAX_CHARS:]


def collect(transport: Any, *, host: str, run_id: int, state: dict[str, Any], settings: dict[str, Any], checkout: Path, now: dt.datetime | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
	"""Read everything the audit needs; see the module docstring for the call budget."""
	now = now or utc_now()
	try:
		user = transport.rest("user")
		pipeline_login = user.get("login", "") if isinstance(user, dict) else ""
	except ApiError:
		pipeline_login = ""
	repos, stats = discover_repos(transport, host, settings)
	host_private = bool(repos[0].get("private"))
	for repo in repos:
		repo["redacted"] = should_redact(repo, host_private, settings)
	log(
		f"ISSUE_PR_AUDIT_REPOS discovered={stats['discovered']} audited={stats['audited']} skipped_archived={stats['skipped_archived']} "
		f"excluded={stats['excluded']} truncated={stats['truncated']}" + (f" fallback={stats['fallback']}" if stats["fallback"] else "")
	)
	records: list[dict[str, Any]] = []
	for repo in repos:
		display = repo_display(repo["name"], repo["redacted"])
		cursor, clamped, source = resolve_cursor(state, display, now, settings)
		log(f"ISSUE_PR_AUDIT_CURSOR repo={display} cursor={fmt_time(cursor)} clamped={'true' if clamped else 'false'} source={source}")
		record = {**repo, "display": display, "cursor": fmt_time(cursor), "clamped": clamped, "status": "ok", "window": [], "open": [], "next_cursor": fmt_time(now)}
		try:
			window, partial = list_window(transport, repo, cursor, settings)
			record["window"] = window
			if partial:
				record["next_cursor"] = partial
				record["window_truncated"] = True
			record["open"], open_truncated = list_open(transport, repo, settings)
			if open_truncated:
				# Known blocked entries past the cap are kept (classify) and the
				# gap is named under Coverage.
				record["open_truncated"] = True
		except ApiError as exc:
			record["status"] = "read_failed"
			record["error"] = str(exc)
		records.append(record)
	# Details, highest priority first, capped.
	candidates: list[tuple[int, str]] = []
	seen: set[str] = set()
	for record in records:
		if record["status"] != "ok":
			continue
		cursor = parse_time(record["cursor"]) or now
		for item in record["window"] + record["open"]:
			key = item_key(item["repo"], item["number"])
			if key in seen:
				continue
			seen.add(key)
			priority = _detail_priority(item, cursor)
			if priority < 3:
				candidates.append((priority, key))
	candidates.sort()
	keys = [key for _, key in candidates[: settings["max_detail_items"]]]
	details, failed_repos = fetch_details(transport, keys)
	for record in records:
		if record["name"] in failed_repos and record["status"] == "ok":
			record["status"] = "read_failed"
			record["error"] = "details_failed"
	# Log tails for new blocked items.
	logs: dict[str, str] = {}
	log_budget = settings["max_blocked_logs"]
	known = state.get("blocked") if isinstance(state.get("blocked"), dict) else {}
	for record in records:
		if record["status"] != "ok":
			continue
		for item in record["open"]:
			if log_budget <= 0:
				break
			key = item_key(item["repo"], item["number"])
			latch = blocked_latch(item, details.get(key), now, settings)
			state_key = item_key(record["display"], item["number"])
			if not latch or (isinstance(known.get(state_key), dict) and known[state_key].get("fingerprint") == blocked_fingerprint(item, latch)):
				continue
			run = _run_id_from_comments(item, details.get(key) or {}, pipeline_login)
			if run is None:
				continue
			log_budget -= 1
			try:
				jobs = transport.rest(f"repos/{item['repo']}/actions/runs/{run}/jobs?filter=latest&per_page=100")
				failed_job = next((job for job in (jobs or {}).get("jobs") or [] if isinstance(job, dict) and job.get("conclusion") == "failure" and isinstance(job.get("id"), int)), None)
				if failed_job:
					logs[key] = tail_log(transport.raw(f"repos/{item['repo']}/actions/jobs/{failed_job['id']}/logs"))
			except ApiError:
				continue
	heal: list[dict[str, Any]] = []
	try:
		rows, _ = paginate(transport, f"repos/{host}/issues?state=open&labels=ai:workflow-heal&per_page=100", HEAL_LIST_MAX_PAGES)
		heal = [{"number": row.get("number"), "title": (row.get("title") or "")[:TITLE_MAX], "url": row.get("html_url") or ""} for row in rows if isinstance(row.get("number"), int)]
	except ApiError:
		pass
	earlier = redact_earlier_items(open_earlier_reports(transport, host, pipeline_login), records, host_private, settings)
	counts = dict(getattr(transport, "counts", {}))
	log(f"ISSUE_PR_AUDIT_API rest={counts.get('rest', 0)} graphql={counts.get('graphql', 0)} failed={counts.get('failed', 0)}")
	return {
		"version": 1,
		"run_id": run_id,
		"listing_start": fmt_time(now),
		"host": host,
		"host_private": host_private,
		"pipeline_login": pipeline_login,
		"discovery": stats,
		"repos": records,
		"details": details,
		"logs": logs,
		"heal_issues": heal,
		"prior_aud_titles": prior_aud_titles(checkout),
		"earlier_open_reports": earlier,
		"api": counts,
	}


def open_earlier_reports(transport: Any, host: str, pipeline_login: str) -> list[dict[str, Any]]:
	"""Open, unmerged audit report PRs of this host and what they covered.

	The state file moves only when a report PR merges, so while an earlier
	report PR is open this run re-reads its window. The model is told which
	items and ``AUD-###`` plans that report already holds, so it cites them
	instead of planning a cause twice.

	API calls: 1 open-PR listing of the host (at most EARLIER_REPORT_LIST_PAGES
	pages; the delivery script reads the same listing later, after the model
	pass), then per matching PR (at most EARLIER_REPORT_MAX_PRS) 1 files
	listing and 1 contents read. Only same-repository PRs on
	``auto/issue-pr-audit-report-<run>-<attempt>`` opened by the pipeline login
	count. Fails open: any API error returns what was read so far.
	Output: ``[{"pr", "branch", "report", "items", "aud_titles"}]``; the text
	is earlier model output, so it is rendered only as untrusted data. A read
	error on one PR skips that PR; at most EARLIER_REPORT_MAX_PRS matching PRs
	are tried, so failures cannot raise the call count.
	"""
	found: list[dict[str, Any]] = []
	attempted = 0
	if not pipeline_login:
		return found
	try:
		rows, _ = paginate(transport, f"repos/{host}/pulls?state=open&per_page=100", EARLIER_REPORT_LIST_PAGES)
	except ApiError:
		return found
	for row in rows:
		if len(found) >= EARLIER_REPORT_MAX_PRS or attempted >= EARLIER_REPORT_MAX_PRS:
			break
		head = row.get("head") if isinstance(row.get("head"), dict) else {}
		head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
		user = row.get("user") if isinstance(row.get("user"), dict) else {}
		ref = head.get("ref") if isinstance(head.get("ref"), str) else ""
		sha = head.get("sha") if isinstance(head.get("sha"), str) else ""
		number = row.get("number")
		if not EARLIER_REPORT_REF_RE.match(ref) or not re.fullmatch(r"[0-9a-f]{40}", sha) or not isinstance(number, int):
			continue
		if str(head_repo.get("full_name") or "").lower() != host.lower() or user.get("login") != pipeline_login:
			continue
		attempted += 1
		try:
			files = transport.rest(f"repos/{host}/pulls/{number}/files?per_page=100")
			report = next((entry.get("filename") for entry in files or [] if isinstance(entry, dict) and entry.get("status") in ("added", "modified")
				and isinstance(entry.get("filename"), str) and entry["filename"].startswith("analysis/") and REPORT_NAME_RE.match(entry["filename"][len("analysis/"):])), None)
			if not report:
				continue
			content = transport.rest(f"repos/{host}/contents/{report}?ref={sha}")
			text = base64.b64decode((content or {}).get("content") or "").decode("utf-8", errors="replace") if isinstance(content, dict) else ""
		except (ApiError, ValueError):
			continue
		found.append({
			"pr": row.get("html_url") if isinstance(row.get("html_url"), str) else f"https://github.com/{host}/pull/{number}",
			"branch": ref,
			"report": report,
			"items": list(dict.fromkeys(match.group(0) for match in EARLIER_REPORT_ITEM_RE.finditer(text)))[:EARLIER_REPORT_MAX_ITEMS],
			"aud_titles": [match.group(1)[:TITLE_MAX] for match in re.finditer(r"^### (AUD-[0-9]{3}: .+)$", text, re.M)][:200],
		})
	return found


EARLIER_REPORT_URL_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/([0-9]+)$")


def redact_earlier_items(earlier: list[dict[str, Any]], records: list[dict[str, Any]], host_private: bool, settings: dict[str, Any]) -> list[dict[str, Any]]:
	"""Apply this run's redaction (D7) to item URLs scraped from earlier reports.

	An earlier report may have been written while redaction was off. A URL of
	a repository this run redacts becomes ``private:<hash>#<n>``. While
	redaction is active, a URL of a repository this run did not audit
	(unknown visibility) is dropped, fail closed.
	"""
	active = not host_private and bool(settings["redact_private_in_public"])
	audited = {str(record.get("name") or "").lower(): bool(record.get("redacted")) for record in records}
	for report in earlier:
		kept: list[str] = []
		for ref in report.get("items") or []:
			match = EARLIER_REPORT_URL_RE.match(ref)
			if not match:
				kept.append(ref)
				continue
			repo = match.group(1).lower()
			if repo in audited:
				kept.append(f"{repo_display(match.group(1), True)}#{match.group(2)}" if audited[repo] else ref)
			elif not active:
				kept.append(ref)
		report["items"] = list(dict.fromkeys(kept))
	return earlier


def prior_aud_titles(checkout: Path) -> list[str]:
	"""``AUD-###: title`` lines of the newest audit report on the checkout."""
	reports = sorted(
		(path for path in (checkout / "analysis").glob(REPORT_GLOB) if REPORT_NAME_RE.match(path.name)),
		key=lambda path: (REPORT_NAME_RE.match(path.name).group(1), int((REPORT_NAME_RE.match(path.name).group(2) or "-1")[1:])),
	)
	if not reports:
		return []
	try:
		text = reports[-1].read_text(encoding="utf-8", errors="replace")
	except OSError:
		return []
	return [match.group(1)[:TITLE_MAX] for match in re.finditer(r"^### (AUD-[0-9]{3}: .+)$", text, re.M)][:200]


# --- classify ------------------------------------------------------------------


def classify(raw: dict[str, Any], state: dict[str, Any], settings: dict[str, Any], *, now: dt.datetime | None = None, log: Callable[[str], None] = print) -> tuple[dict[str, Any], dict[str, Any]]:
	"""Tier 1: signals, attribution, clean sample and blocked items -> ``(digest, next_state)``."""
	now = now or parse_time(raw.get("listing_start")) or utc_now()
	run_id = int(raw.get("run_id") or 0)
	details = raw.get("details") or {}
	old_blocked = state.get("blocked") if isinstance(state.get("blocked"), dict) else {}
	old_repos = state.get("repos") if isinstance(state.get("repos"), dict) else {}
	next_state: dict[str, Any] = {
		"version": STATE_VERSION,
		"repos": {key: value for key, value in old_repos.items() if isinstance(value, dict)},
		"blocked": {},
		"last_tg_date": state.get("last_tg_date") if isinstance(state.get("last_tg_date"), str) else "",
	}
	items: list[dict[str, Any]] = []
	blocked: list[dict[str, Any]] = []
	coverage: list[dict[str, Any]] = []
	clean_keys: list[str] = []
	clean_counts: dict[str, int] = {}
	failed_displays: set[str] = set()
	# Repositories whose open snapshot was read in full this run; blocked
	# entries of any other repository (read failure, capped snapshot, or not
	# audited at all, e.g. a discovery fallback) are carried over unchanged.
	full_snapshot_displays: set[str] = set()
	for record in raw.get("repos") or []:
		display = record.get("display") or record["name"]
		redacted = bool(record.get("redacted"))
		cursor = parse_time(record.get("cursor")) or now
		window = record.get("window") or []
		cov = {"repo": display, "items": 0, "flagged": 0, "blocked": 0, "status": record.get("status", "ok"), "clamped": bool(record.get("clamped")), "cursor": record.get("cursor"), "truncated": bool(record.get("window_truncated")), "open_truncated": bool(record.get("open_truncated"))}
		if record.get("status") != "ok":
			failed_displays.add(display)
			log(f"ISSUE_PR_AUDIT_REPO repo={display} items=0 flagged=0 blocked=0 outcome=read_failed")
			coverage.append(cov)
			continue
		next_state["repos"][display] = {"cursor": record.get("next_cursor") or fmt_time(now), "last_run": run_id}
		if not record.get("open_truncated"):
			full_snapshot_displays.add(display)
		peers = window + [item for item in record.get("open") or [] if item not in window]
		for item in window:
			key = item_key(item["repo"], item["number"])
			detail = details.get(key)
			if not in_window(item, cursor) and not reopened_since(detail, cursor):
				continue
			signals = waste_signals(item, detail, peers, settings)
			entry = _digest_item(item, detail, redacted, raw, settings)
			entry["signals"] = signals
			cov["items"] += 1
			if signals:
				entry["flagged"] = True
				cov["flagged"] += 1
			else:
				entry["flagged"] = False
				entry["category"] = clean_category(item, detail)
				clean_counts[entry["category"]] = clean_counts.get(entry["category"], 0) + 1
				clean_keys.append(entry["key"])
			items.append(entry)
		for item in record.get("open") or []:
			key = item_key(item["repo"], item["number"])
			detail = details.get(key)
			latch = blocked_latch(item, detail, now, settings)
			if not latch:
				continue
			state_key = item_key(display, item["number"])
			fingerprint = blocked_fingerprint(item, latch)
			old = old_blocked.get(state_key) if isinstance(old_blocked.get(state_key), dict) else None
			since = parse_time(old.get("since")) if old else None
			is_new = old is None or old.get("fingerprint") != fingerprint
			if since is None:
				since = parse_time(item.get("updated_at")) or now
			entry = _digest_item(item, detail, redacted, raw, settings)
			entry.update({"latch": latch, "since": fmt_time(since), "new": is_new, "fingerprint": fingerprint})
			entry["log_tail"] = (raw.get("logs") or {}).get(key, "") if not redacted else ""
			blocked.append(entry)
			cov["blocked"] += 1
			next_state["blocked"][state_key] = {"since": fmt_time(since), "fingerprint": fingerprint, "diagnosed_run": run_id if is_new else int(old.get("diagnosed_run") or 0) if old else run_id}
		log(f"ISSUE_PR_AUDIT_REPO repo={display} items={cov['items']} flagged={cov['flagged']} blocked={cov['blocked']} outcome=ok")
		coverage.append(cov)
	# A repository that could not be read, or was not read in full, keeps its
	# blocked entries (and their since) instead of losing them. Only
	# repositories this run tried to read count, plus, after a discovery
	# fallback (host only), every repository the run could not list. Entries
	# of a repository dropped on purpose (archived, excluded, past the cap,
	# or shown under another display name, e.g. a plain name after
	# redaction was turned back on) are dropped, so the committed state
	# stays bounded and a plain private name left by an unredacted run is
	# removed on the next run that audits that repository.
	audited_displays = {record.get("display") or record["name"] for record in raw.get("repos") or []}
	partial_displays = failed_displays | {record.get("display") or record["name"] for record in raw.get("repos") or [] if record.get("open_truncated")}
	discovery_fallback = bool((raw.get("discovery") or {}).get("fallback"))
	# Carried entries group under the real repository name (Telegram, sort),
	# like fresh entries; their display stays the redacted state key.
	display_to_name = {record.get("display") or record["name"]: record["name"] for record in raw.get("repos") or []}
	coverage_by_display = {cov["repo"]: cov for cov in coverage}
	for key, value in old_blocked.items():
		repo_part, _, number_text = key.rpartition("#")
		if not isinstance(value, dict) or key in next_state["blocked"] or repo_part in full_snapshot_displays:
			continue
		if repo_part not in partial_displays and not (discovery_fallback and repo_part not in audited_displays):
			continue
		next_state["blocked"][key] = value
		if repo_part in coverage_by_display:
			coverage_by_display[repo_part]["carried_blocked"] = coverage_by_display[repo_part].get("carried_blocked", 0) + 1
		if not number_text.isdigit():
			continue
		# Listed in the report and on Telegram with its age (no current data).
		redacted_part = repo_part.startswith("private:")
		blocked.append({
			"key": key,
			"repo": display_to_name.get(repo_part, repo_part),
			"number": int(number_text),
			"display": key if redacted_part else f"https://github.com/{repo_part}/issues/{number_text}",
			"redacted": redacted_part,
			"url": "" if redacted_part else f"https://github.com/{repo_part}/issues/{number_text}",
			"title": "",
			"labels": [],
			"latch": "carried_over",
			"since": value.get("since") if isinstance(value.get("since"), str) else "",
			"new": False,
			"carried_over": True,
			"fingerprint": value.get("fingerprint") if isinstance(value.get("fingerprint"), str) else "",
		})
	sample = set(clean_sample(clean_keys, run_id, settings["clean_sample"]))
	flagged = sorted((entry for entry in items if entry["flagged"]), key=lambda entry: (-len(entry["signals"]), entry["key"]))
	deep = flagged[: settings["max_deep_items"]]
	name_to_display = {record["name"]: record.get("display") or record["name"] for record in raw.get("repos") or []}
	coverage_by_repo = {cov["repo"]: cov for cov in coverage}
	for entry in items:
		entry["deep"] = entry in deep
		entry["sample"] = entry["key"] in sample
		cov_entry = coverage_by_repo.get(name_to_display.get(entry["repo"], entry["repo"]))
		if entry["deep"] and cov_entry is not None:
			cov_entry["deep"] = cov_entry.get("deep", 0) + 1
	cursors = [entry.get("cursor") for entry in coverage if entry.get("cursor")]
	digest = {
		"version": 1,
		"run_id": run_id,
		"window_start": min(cursors) if cursors else fmt_time(now),
		"window_end": raw.get("listing_start") or fmt_time(now),
		"host": raw.get("host") or "",
		"host_private": bool(raw.get("host_private")),
		"discovery": raw.get("discovery") or {},
		"coverage": coverage,
		"items": items,
		"blocked": sorted(blocked, key=lambda entry: (entry["repo"], entry["number"])),
		"clean_counts": dict(sorted(clean_counts.items())),
		"heal_issues": raw.get("heal_issues") or [],
		"prior_aud_titles": raw.get("prior_aud_titles") or [],
		"earlier_open_reports": raw.get("earlier_open_reports") or [],
		"api": raw.get("api") or {},
	}
	return digest, next_state


def _digest_item(item: dict[str, Any], detail: dict[str, Any] | None, redacted: bool, raw: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
	repo_shown = repo_display(item["repo"], redacted)
	trusted = is_trusted_author(item.get("author", ""), item.get("author_association", ""), raw.get("pipeline_login") or "")
	detail = detail or {}
	comments = [
		{"author": comment.get("author", ""), "at": comment.get("at", ""), "body": comment.get("body", "")}
		for comment in detail.get("comments") or []
		if is_trusted_author(comment.get("author", ""), comment.get("association", ""), raw.get("pipeline_login") or "")
	]
	return {
		"key": item_key(item["repo"], item["number"]),
		"repo": item["repo"],
		"number": item["number"],
		"display": f"{repo_shown}#{item['number']}" if redacted else item["url"],
		"redacted": redacted,
		"url": item["url"],
		"title": item.get("title", ""),
		"is_pr": item["is_pr"],
		"state": item.get("state"),
		"state_reason": item.get("state_reason"),
		"author": item.get("author", ""),
		"author_association": item.get("author_association", ""),
		"automation": creating_automation(item, detail),
		"created_at": item.get("created_at"),
		"closed_at": item.get("closed_at"),
		"merged_at": item.get("merged_at"),
		"labels": item.get("labels") or [],
		"head_ref": detail.get("head_ref") or item.get("head_ref") or "",
		"stats": {key: detail.get(key) for key in ("changed_files", "additions", "deletions", "commits", "reviews", "mergeable", "rollup") if key in detail},
		"trusted_author": trusted,
		"body": (item.get("body") or "")[:BODY_MAX] if trusted else "",
		"comments": comments,
	}


# --- model input ---------------------------------------------------------------


def wrap_untrusted(text: str, source: str) -> str:
	"""Wrap free text; a literal tag inside the content is escaped so it cannot close the block."""
	safe = UNTRUSTED_TAG_RE.sub(lambda match: "&lt;" + match.group(1) + "untrusted_external_data", text or "")
	safe = CONTROL_RE.sub("", safe)
	return f'<untrusted_external_data source="{source}">\n{safe}\n</untrusted_external_data>'


def _render_entry(entry: dict[str, Any], lines: list[str]) -> None:
	lines.append(f"### {entry['display']}")
	if entry.get("carried_over"):
		# Kept from the previous state: this run did not read the repository
		# in full, so there is no current data for the item.
		lines.append(f"- blocked since {entry.get('since') or '?'}; carried over from the previous audit (repository not read in full this run, no current data)")
		return
	if entry["redacted"]:
		lines.append(f"- kind: {'pull request' if entry['is_pr'] else 'issue'}; state: {entry['state']} {entry.get('state_reason') or ''}".rstrip())
		lines.append("- private repository item: metadata only, never expand this label")
	else:
		lines.append(f"- kind: {'pull request' if entry['is_pr'] else 'issue'}; state: {entry['state']} {entry.get('state_reason') or ''}".rstrip())
		lines.append(f"- author: {entry['author']} ({entry['author_association']}); creating automation: {entry['automation'] or 'none (person)'}")
		lines.append(f"- created {entry.get('created_at') or '?'}; closed {entry.get('closed_at') or '-'}; merged {entry.get('merged_at') or '-'}")
		# Labels and branch names are repository-controlled text too.
		lines.append(wrap_untrusted(f"labels: {', '.join(entry['labels']) or 'none'}; head ref: {entry.get('head_ref') or '-'}", "labels-and-head-ref"))
	if entry.get("stats"):
		lines.append("- stats: " + ", ".join(f"{key}={value}" for key, value in entry["stats"].items()))
	if entry.get("signals"):
		lines.append("- tier-1 signals: " + ", ".join(entry["signals"]))
	if entry.get("latch"):
		lines.append(f"- blocked since {entry['since']}; latch:")
		lines.append(wrap_untrusted(entry["latch"], "latch"))
	if entry["redacted"]:
		return
	lines.append(wrap_untrusted(entry.get("title", "")[:TITLE_MAX], "title"))
	if entry.get("body"):
		lines.append(wrap_untrusted(entry["body"], "body"))
	for comment in entry.get("comments") or []:
		lines.append(wrap_untrusted(f"{comment['author']} at {comment['at']}:\n{comment['body']}", "comment"))
	if entry.get("log_tail"):
		lines.append(wrap_untrusted(entry["log_tail"], "failed-job-log-tail"))


def render_model_input(digest: dict[str, Any]) -> str:
	"""The data block of the model prompt (flagged, sample, new blocked items)."""
	lines = [
		"## Audit data",
		f"Window: {digest['window_start']} to {digest['window_end']} (run {digest['run_id']}).",
		"",
		"### Coverage",
	]
	for cov in digest["coverage"]:
		lines.append(
			f"- {cov['repo']}: items={cov['items']} flagged={cov['flagged']} deep_reviewed={cov.get('deep', 0)} blocked={cov['blocked']}"
			+ (f" carried_blocked={cov['carried_blocked']}" if cov.get("carried_blocked") else "")
			+ f" status={cov['status']}"
			f" cursor={cov['cursor']} clamped={'true' if cov['clamped'] else 'false'}" + (" listing_truncated=true" if cov.get("truncated") else "")
			+ (" open_snapshot_truncated=true" if cov.get("open_truncated") else "")
		)
	discovery = digest.get("discovery") or {}
	if discovery:
		# Counts only: names cut by the cap are not kept and may be private.
		lines.append(
			f"- discovery: discovered={discovery.get('discovered', 0)} audited={discovery.get('audited', 0)}"
			f" skipped_archived={discovery.get('skipped_archived', 0)} excluded={discovery.get('excluded', 0)}"
			f" truncated_by_repo_cap={discovery.get('truncated', 0)}"
			+ (f" fallback={discovery['fallback']} (repository listing failed: only the host was audited)" if discovery.get("fallback") else "")
		)
	lines += ["", "### Clean item counts"]
	lines += [f"- {category}: {count}" for category, count in digest["clean_counts"].items()] or ["- none"]
	lines += ["", "### Open ai:workflow-heal issues (cite instead of planning again)"]
	lines += [f"- {heal['url']} {wrap_untrusted(heal['title'], 'heal-title')}" for heal in digest["heal_issues"]] or ["- none"]
	lines += ["", "### Fix plans in the previous audit report (cite instead of planning again)"]
	lines += [f"- {wrap_untrusted(title, 'previous-plan-title')}" for title in digest["prior_aud_titles"]] or ["- none"]
	earlier = [report for report in digest.get("earlier_open_reports") or [] if isinstance(report, dict)]
	lines += ["", "### Open earlier audit report PRs (not merged: this run re-read their window; say so under Coverage, and cite their plans instead of planning again)"]
	if not earlier:
		lines.append("- none")
	for report in earlier:
		lines.append(f"- {report.get('pr', '')} ({report.get('report', '')})")
		lines.append(wrap_untrusted("items covered: " + (", ".join(report.get("items") or []) or "none"), "earlier-report-items"))
		lines.append(wrap_untrusted("fix plans: " + ("; ".join(report.get("aud_titles") or []) or "none"), "earlier-report-plans"))
	# New blocked items come first: G4 needs a diagnosis for each, so they get
	# the input budget before the flagged items.
	sections = (
		("## New blocked items (diagnose each to a root cause)", [entry for entry in digest["blocked"] if entry.get("new")]),
		("## Flagged items (every one must appear in a report section)", [entry for entry in digest["items"] if entry.get("deep")]),
		("## Clean sample (verify the tier-1 rules missed nothing)", [entry for entry in digest["items"] if entry.get("sample") and not entry.get("flagged")]),
		("## Known blocked items (list with their age; no new diagnosis needed)", [entry for entry in digest["blocked"] if not entry.get("new")]),
	)
	# Whole entries are kept or left out, so no wrapped block is ever cut. An
	# entry left out is marked ``omitted``; required_report_refs and
	# scripts/check_issue_pr_audit_report.py do not require it, so a heavy day
	# cannot fail the report check on items the model never saw.
	size = len(("\n".join(lines) + "\n").encode("utf-8"))
	budget = MODEL_INPUT_MAX_BYTES - 1024
	omitted = 0
	omitted_by_section: list[str] = []
	for heading, entries in sections:
		section_omitted = 0
		lines += ["", heading]
		size += len(heading.encode("utf-8")) + 2
		if not entries:
			lines.append("- none")
		for entry in entries:
			chunk: list[str] = []
			_render_entry(entry, chunk)
			chunk_size = len(("\n".join(chunk) + "\n").encode("utf-8"))
			if size + chunk_size > budget:
				entry["omitted"] = True
				omitted += 1
				section_omitted += 1
				continue
			entry.pop("omitted", None)
			lines.extend(chunk)
			size += chunk_size
		if section_omitted:
			omitted_by_section.append(f"{section_omitted} under {heading[3:].split(' (', 1)[0]}")
	if omitted:
		lines += ["", f"({omitted} items left out at the input size cap; they need no report line: {', '.join(omitted_by_section)})"]
	return "\n".join(lines) + "\n"


def required_report_refs(digest: dict[str, Any]) -> list[str]:
	"""Display strings the report must mention: every deep-reviewed flagged item and every new blocked item."""
	refs = [entry["display"] for entry in digest.get("items") or [] if entry.get("deep") and not entry.get("omitted")]
	refs += [entry["display"] for entry in digest.get("blocked") or [] if entry.get("new") and not entry.get("omitted")]
	return list(dict.fromkeys(refs))


# --- finalize --------------------------------------------------------------------


def finalize_report(model_text: str, digest: dict[str, Any], metrics: dict[str, str]) -> str:
	"""D4: the trusted marker and Metrics Appendix around the model's sections 1-8."""
	body_lines = []
	skipping = False
	for line in (model_text or "").replace("\r\n", "\n").split("\n"):
		if line.startswith("<!-- issue-pr-audit:"):
			continue
		if re.match(r"^## +Metrics Appendix\s*$", line):
			skipping = True
			continue
		if skipping and line.startswith("## "):
			skipping = False
		if not skipping:
			body_lines.append(line)
	body = "\n".join(body_lines).strip("\n")
	marker = (
		f"<!-- issue-pr-audit: window_start={digest['window_start']} window_end={digest['window_end']} "
		f"run={int(digest['run_id'])} repos={len(digest.get('coverage') or [])} -->"
	)
	api = digest.get("api") or {}
	appendix = [
		"## Metrics Appendix",
		"",
		f"- API calls: rest={api.get('rest', 0)} graphql={api.get('graphql', 0)} failed={api.get('failed', 0)}",
		f"- Items in window: {len(digest.get('items') or [])}; flagged: {sum(1 for entry in digest.get('items') or [] if entry.get('flagged'))}; "
		f"deep-reviewed: {sum(1 for entry in digest.get('items') or [] if entry.get('deep'))}; blocked: {len(digest.get('blocked') or [])}",
		(
			f"- Left out at the model input size cap: new blocked {sum(1 for entry in digest.get('blocked') or [] if entry.get('new') and entry.get('omitted'))}; "
			f"flagged {sum(1 for entry in digest.get('items') or [] if entry.get('deep') and entry.get('omitted'))}; "
			f"clean sample {sum(1 for entry in digest.get('items') or [] if entry.get('sample') and not entry.get('flagged') and entry.get('omitted'))}; "
			f"known blocked {sum(1 for entry in digest.get('blocked') or [] if not entry.get('new') and entry.get('omitted'))}"
		),
		f"- Open earlier audit report PRs (window re-read): {len(digest.get('earlier_open_reports') or [])}",
	]
	for key in sorted(metrics):
		if re.fullmatch(r"[a-z_]{1,40}", key) and re.fullmatch(r"[A-Za-z0-9_.:=/ -]{0,120}", metrics[key] or ""):
			appendix.append(f"- {key.replace('_', ' ')}: {metrics[key]}")
	return f"{marker}\n{body}\n\n" + "\n".join(appendix) + "\n"


# --- state ---------------------------------------------------------------------


def load_state(path: Path) -> dict[str, Any]:
	"""The committed state; missing or unreadable is an empty v1 state (24-hour first lookback)."""
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {"version": STATE_VERSION, "repos": {}, "blocked": {}, "last_tg_date": ""}
	if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
		return {"version": STATE_VERSION, "repos": {}, "blocked": {}, "last_tg_date": ""}
	return data


def state_update(next_state: dict[str, Any], digest: dict[str, Any], today: str) -> dict[str, Any]:
	"""The state to commit: classify's next state, plus today's Telegram date when a report goes out."""
	final = json.loads(json.dumps(next_state))
	if digest.get("blocked"):
		final["last_tg_date"] = today
	# A new blocked item the model never saw (left out at the input size cap)
	# keeps its since but loses its fingerprint, so the next run treats it as
	# new again and diagnoses it (G4).
	for entry in digest.get("blocked") or []:
		if not (isinstance(entry, dict) and entry.get("new") and entry.get("omitted") and isinstance(entry.get("number"), int)):
			continue
		state_key = item_key(repo_display(str(entry.get("repo") or ""), bool(entry.get("redacted"))), entry["number"])
		if isinstance((final.get("blocked") or {}).get(state_key), dict):
			final["blocked"][state_key]["fingerprint"] = ""
	final["repos"] = dict(sorted((final.get("repos") or {}).items()))
	final["blocked"] = dict(sorted((final.get("blocked") or {}).items()))
	return final


# --- purge ---------------------------------------------------------------------


def purge_candidates(checkout: Path, *, today: dt.date, retention_days: int, processing_report: Path, in_flight: set[str] | None, log: Callable[[str], None] = print) -> list[str]:
	"""D1: old audit reports the purge may delete (processed, not in flight, past the window).

	Fails closed: no processing report or no in-flight list (``None``) purges nothing.
	The state file never matches the report name pattern.
	"""
	if in_flight is None:
		log("ISSUE_PR_AUDIT_PURGE reason=in_flight_unavailable")
		return []
	try:
		listing = processing_report.read_text(encoding="utf-8", errors="replace")
	except OSError:
		log("ISSUE_PR_AUDIT_PURGE reason=processing_report_unavailable")
		return []
	cutoff = today - dt.timedelta(days=retention_days)
	purged = []
	for path in sorted((checkout / "analysis").glob(REPORT_GLOB)):
		match = REPORT_NAME_RE.match(path.name)
		if not match or path.is_symlink() or not path.is_file():
			continue
		try:
			stamp = dt.date.fromisoformat(match.group(1))
		except ValueError:
			continue
		if stamp >= cutoff:
			continue
		rel = f"analysis/{path.name}"
		if rel in in_flight:
			log(f"ISSUE_PR_AUDIT_PURGE doc={rel} reason=in_flight_project")
			continue
		if path.name not in listing:
			log(f"ISSUE_PR_AUDIT_PURGE doc={rel} reason=unprocessed")
			continue
		purged.append(rel)
		log(f"ISSUE_PR_AUDIT_PURGE doc={rel} outcome=purged")
	return purged


# --- telegram ------------------------------------------------------------------


def _section(text: str, heading: str) -> str:
	match = re.search(rf"^## +{re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text or "", re.M | re.S)
	return match.group(1) if match else ""


def report_counts(report: str) -> dict[str, int]:
	return {
		"unnecessary": len(re.findall(r"^- \S", _section(report, "Unnecessary Items"), re.M)),
		"misresolved": len(re.findall(r"^- \S", _section(report, "Incorrectly Resolved Items"), re.M)),
		"plans": len(re.findall(r"^### AUD-[0-9]{3}:", _section(report, "Fix Plans"), re.M)),
	}


def _clean_text(text: str, limit: int) -> str:
	text = re.sub(r"\s+", " ", CONTROL_RE.sub("", text or "")).strip()
	return text if len(text) <= limit else text[: limit - 1] + "…"


def blocked_causes(report: str) -> dict[str, tuple[str, str]]:
	"""``{display: (cause, plan_or_tracking)}`` from the report's Blocked Items lines."""
	causes: dict[str, tuple[str, str]] = {}
	for line in _section(report, "Blocked Items").splitlines():
		if not line.startswith("- "):
			continue
		parts = [part.strip() for part in line[2:].split(" — ")]
		if len(parts) >= 5:
			causes[parts[0]] = (parts[3], parts[4])
	return causes


def telegram_decision(digest: dict[str, Any] | None, state: dict[str, Any], today: str, cache_hit: bool, when_empty: bool) -> tuple[bool, str]:
	if digest is None:
		return False, "no_digest"
	if cache_hit or state.get("last_tg_date") == today:
		return False, "already_sent_today"
	if not digest.get("blocked") and not when_empty:
		return False, "empty"
	return True, "ok"


def render_telegram(digest: dict[str, Any], report: str | None, today: str, link: str, now: dt.datetime | None = None) -> list[str]:
	"""Parts of at most 3,900 characters, ``(i/n)`` marked; plain text (no parse mode)."""
	now = now or utc_now()
	causes = blocked_causes(report) if report else {}
	blocked = digest.get("blocked") or []
	repos = sorted({entry["repo"] for entry in blocked})
	new = sum(1 for entry in blocked if entry.get("new"))
	header = f"🧱 Blocked items — {today} — {len(blocked)} across {len(repos)} repos ({new} new)"
	lines: list[str] = []
	listed = 0
	for repo in repos:
		entries = [entry for entry in blocked if entry["repo"] == repo]
		group: list[str] = []
		for entry in entries:
			if listed >= TG_MAX_ITEMS:
				break
			cause, ref = causes.get(entry["display"], ("diagnosis unavailable" if report is None else entry.get("latch", ""), "-"))
			group.append(
				f"#{entry['number']} {_clean_text(entry.get('title', ''), TELEGRAM_TITLE_MAX)} · {format_age(parse_time(entry.get('since')), now)}"
				f" · {_clean_text(cause, 160)} · {_clean_text(ref, 120)}"
			)
			listed += 1
		if group:
			lines.append(repo)
			lines.extend(group)
	if len(blocked) > listed:
		lines.append(f"… and {len(blocked) - listed} more")
	counts = report_counts(report or "")
	footer = f"Audit: {counts['unnecessary']} unnecessary · {counts['misresolved']} mis-resolved · {counts['plans']} fix plans — {link}"
	budget = TG_PART_LIMIT - len(header) - 12
	chunks: list[list[str]] = [[]]
	size = 0
	for line in lines + ["", footer]:
		line = line[: budget - 1]
		if chunks[-1] and size + len(line) + 1 > budget:
			chunks.append([])
			size = 0
		chunks[-1].append(line)
		size += len(line) + 1
	total = len(chunks)
	parts = []
	for index, chunk in enumerate(chunks, 1):
		title = header if total == 1 else f"{header} ({index}/{total})"
		parts.append("\n".join([title, *chunk]).rstrip()[:TG_PART_LIMIT])
	return parts


# --- command line ----------------------------------------------------------------


def _read_json(path: Path) -> Any:
	return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.with_name(path.name + ".tmp")
	tmp.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")
	os.replace(tmp, path)


def _workdir(args: argparse.Namespace) -> Path:
	path = Path(args.workdir or os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "issue-pr-audit"))
	path.mkdir(parents=True, exist_ok=True)
	return path


def cmd_collect(args: argparse.Namespace) -> int:
	work = _workdir(args)
	host = os.environ.get("GITHUB_REPOSITORY", "")
	if not REPO_RE.match(host):
		print("::error::ISSUE_PR_AUDIT collect needs GITHUB_REPOSITORY=owner/repo", file=sys.stderr)
		return 2
	run_id = int(os.environ.get("GITHUB_RUN_ID") or 0) if (os.environ.get("GITHUB_RUN_ID") or "0").isdigit() else 0
	settings = load_settings()
	state = load_state(Path(args.state))
	raw = collect(GhTransport(), host=host, run_id=run_id, state=state, settings=settings, checkout=Path(args.checkout))
	_write_json(work / "raw.json", raw)
	return 0


def cmd_classify(args: argparse.Namespace) -> int:
	work = _workdir(args)
	digest, next_state = classify(_read_json(work / "raw.json"), load_state(Path(args.state)), load_settings())
	_write_json(work / "digest.json", digest)
	_write_json(work / "state-next.json", next_state)
	return 0


def cmd_render_input(args: argparse.Namespace) -> int:
	work = _workdir(args)
	digest = _read_json(work / "digest.json")
	(work / "model-input.md").write_text(render_model_input(digest), encoding="utf-8")
	# The omitted marks set by render_model_input go back into the digest the
	# report check reads.
	_write_json(work / "digest.json", digest)
	return 0


def cmd_finalize(args: argparse.Namespace) -> int:
	work = _workdir(args)
	metrics = {}
	for pair in args.metric or []:
		key, _, value = pair.partition("=")
		metrics[key] = value
	model_text = Path(args.model_output).read_text(encoding="utf-8", errors="replace") if Path(args.model_output).is_file() else ""
	Path(args.out).write_text(finalize_report(model_text, _read_json(work / "digest.json"), metrics), encoding="utf-8")
	return 0


def cmd_state_update(args: argparse.Namespace) -> int:
	work = _workdir(args)
	final = state_update(_read_json(work / "state-next.json"), _read_json(work / "digest.json"), args.today or utc_now().strftime("%Y-%m-%d"))
	_write_json(Path(args.out), final)
	return 0


def cmd_purge(args: argparse.Namespace) -> int:
	in_flight: set[str] | None = None
	if args.in_flight_file and Path(args.in_flight_file).is_file():
		in_flight = {line.strip() for line in Path(args.in_flight_file).read_text(encoding="utf-8").splitlines() if line.strip()}
	today = dt.date.fromisoformat(args.today) if args.today else utc_now().date()
	retention = args.retention_days if args.retention_days and args.retention_days > 0 else 30
	for doc in purge_candidates(Path(args.checkout), today=today, retention_days=retention, processing_report=Path(args.processing_report), in_flight=in_flight, log=lambda line: print(line, file=sys.stderr)):
		print(doc)
	return 0


def cmd_telegram(args: argparse.Namespace) -> int:
	work = _workdir(args)
	today = args.today or utc_now().strftime("%Y-%m-%d")
	digest_path = work / "digest.json"
	digest = _read_json(digest_path) if digest_path.is_file() else None
	send, reason = telegram_decision(digest, load_state(Path(args.state)), today, args.cache_hit == "true", load_settings()["tg_when_empty"])
	out_dir = Path(args.out_dir)
	out_dir.mkdir(parents=True, exist_ok=True)
	for stale in out_dir.glob("part-*.txt"):
		stale.unlink()
	if not send:
		print(f"reason={reason}")
		return 0
	report = Path(args.report).read_text(encoding="utf-8", errors="replace") if args.report and Path(args.report).is_file() else None
	parts = render_telegram(digest, report, today, args.link or "")
	for index, part in enumerate(parts, 1):
		(out_dir / f"part-{index:03d}.txt").write_text(part, encoding="utf-8")
	print(f"reason=ok parts={len(parts)}")
	return 0


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Daily issue/PR audit (#10153).")
	parser.add_argument("--workdir", default="")
	sub = parser.add_subparsers(dest="command", required=True)
	p = sub.add_parser("collect")
	p.add_argument("--state", default=STATE_PATH)
	p.add_argument("--checkout", default=".")
	p.set_defaults(func=cmd_collect)
	p = sub.add_parser("classify")
	p.add_argument("--state", default=STATE_PATH)
	p.set_defaults(func=cmd_classify)
	p = sub.add_parser("render-input")
	p.set_defaults(func=cmd_render_input)
	p = sub.add_parser("finalize")
	p.add_argument("--model-output", required=True)
	p.add_argument("--out", required=True)
	p.add_argument("--metric", action="append")
	p.set_defaults(func=cmd_finalize)
	p = sub.add_parser("state-update")
	p.add_argument("--out", required=True)
	p.add_argument("--today", default="")
	p.set_defaults(func=cmd_state_update)
	p = sub.add_parser("purge")
	p.add_argument("--checkout", default=".")
	p.add_argument("--processing-report", default="analysis/recommendation-processing-report.md")
	p.add_argument("--in-flight-file", default="")
	p.add_argument("--retention-days", type=int, default=30)
	p.add_argument("--today", default="")
	p.set_defaults(func=cmd_purge)
	p = sub.add_parser("telegram")
	p.add_argument("--state", default=STATE_PATH)
	p.add_argument("--report", default="")
	p.add_argument("--link", default="")
	p.add_argument("--cache-hit", default="false")
	p.add_argument("--out-dir", required=True)
	p.add_argument("--today", default="")
	p.set_defaults(func=cmd_telegram)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())

#!/usr/bin/env python3
"""Resume, re-clarify or escalate issues parked in ``ai:waiting-on-dependency``.

Stuck-states P8b (Refs #8291); delivers Phase 4b of
``docs/plans/confine-human-steps-to-activation-plan.md`` as widened by the
stuck-states plan's P8 step 2. The ``orchestrate_poll.yml`` step
"Resume dependency waits" runs it on every poller tick, before the
tracking-issue gate, so standalone issues resume even when no orchestrator
project is open::

  python3 -I -B scripts/dependency_wait_resume.py scan --repo <owner/repo>

Batching contract (§14):

- **Input.** ``--repo`` plus the env switches ``DEPENDENCY_WAIT_RESUME_ENABLED``
  (default ``true``; only ``false``/``0``/``no``/``off`` disable it) and
  ``DEPENDENCY_WAIT_ESCALATE_DAYS`` (default ``7``; outside 1-9999 uses ``7``).
  Both defaults live here because the helper is staged from a git ref and may
  run without the workflow's env rows (§8).
- **Call 1.** One GraphQL query returns ``viewer.login`` (the trusted
  pipeline identity), the open ``ai:waiting-on-dependency`` issues (50 per page,
  at most 4 more pages) and the 50 most recently updated open ``ai:blocked``
  issues (first page only, used for one-time ``Depends on: #N`` seeding), each
  with its last 30 comments. With nothing waiting and nothing to seed the scan
  ends after this single call.
- **Call 2.** One aliased GraphQL query reads every distinct dependency target
  (``issueOrPullRequest``) for the tick. Release targets then make one REST
  ``compare/<sha>...stable`` per distinct merge SHA, cached for the tick.
- **Conditional reads.** One paginated REST comments read (at most 10 pages)
  only when a write is due and the 30-comment window is incomplete, and one
  paginated ``issues/<n>/events`` read only when clearing this wait's own
  ``ai:needs-human`` latch or when a blocked issue's newest trusted comment is
  its seed marker (to tell a half-applied seed from a deliberate move back).
  A deliberate move back gets one ``AI_DEPENDENCY_WAIT_SEED_DECLINED_V1``
  note, so that read happens at most once per issue.
- **Writes.** At most one idempotent ``/answer`` or ``/reclarify`` comment per
  resolved dependent (deduplicated by ``AI_DEPENDENCY_WAIT_RESUMED_V1``), and
  one comment plus one label change per escalation, seed or switch-off move.
- **Park (stuck-states P8a-2).** ``parse-waiting`` reads one agent output
  file and makes no API call. ``park`` (clarify and plan, after a validated
  ``WAITING:`` line) makes one aliased GraphQL target read, at most one REST
  ``compare`` (a merged release PR only), one marker comment and one label
  write. Both print exactly one JSON line on stdout, log one
  ``DEPENDENCY_WAIT_PARK op=parse|park issue=<n> target=<t> phase=<p> outcome=<o>``
  line on stderr and always exit 0; ``{"status":"error"}`` on any failure.
- **Output.** One ``DEPENDENCY_WAIT_RESUME issue=<n> target=<t> outcome=<o>``
  line per issue acted on, plus one ``DEPENDENCY_WAIT_RESUME op=scan ...``
  summary line.
- **Fail-open.** Every failure leaves the issue for the next tick and the
  process always exits 0. A missing or unreadable target is never treated as
  satisfied.

Trust: every marker (wait, resume, disabled, latch) counts only when the
comment author equals ``viewer.login`` of the token used for call 1.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable

WAIT_LABEL = "ai:waiting-on-dependency"
BLOCKED_LABEL = "ai:blocked"
NEEDS_HUMAN_LABEL = "ai:needs-human"
# Human-latch labels that stop dependency-wait seeding (stuck-states P8d).
# Mirrors workflow_failure_heal.HUMAN_NEEDED_LABELS and covers
# orchestrate_lib.STALL_RECOVERY_LATCH_LABELS. Copied, not imported: the helper
# runs as ``python3 -I`` without sibling imports. A parity test keeps them aligned.
# ``ai:workflow-heal-escalated`` (#9248) is a stall-recovery latch but is left
# out of HUMAN_NEEDED_LABELS on purpose, because that tuple drives the
# heal-intake ``if:`` predicates.
SEED_HUMAN_LATCH_LABELS: frozenset[str] = frozenset((
	NEEDS_HUMAN_LABEL,
	"ai:check-triage-escalated",
	"ai:destructive-blocked",
	"ai:scope-blocked",
	"ai:harness-broken",
	"ai:resolver-escalated",
	"ai:security-pass-failed",
	"ai:workflow-heal-escalated",
))
MERGED_LABEL = "ai:merged"
SECURITY_LABEL = "ai:security"
ORCHESTRATOR_MANAGED_LABEL = "ai:orchestrator-managed"
UPSTREAM_REPO = "shubhodeep1/coding-workflows"
UPSTREAM_RELEASE_REF = "stable"

WAIT_MARKER_TOKEN = "AI_DEPENDENCY_WAIT_V1"
WAIT_MARKER_RE = re.compile(r"<!--\s*AI_DEPENDENCY_WAIT_V1\s+(\{[^<>]{1,1000}\})\s*-->")
RESUME_MARKER = "AI_DEPENDENCY_WAIT_RESUMED_V1"
RESUME_MARKER_RE = re.compile(
	r"<!--\s*AI_DEPENDENCY_WAIT_RESUMED_V1 issue=([0-9]{1,10}) target=(\S{1,200}) since=(\S{1,64}) outcome=(answer|reclarify)\s*-->"
)
DISABLED_MARKER = "AI_DEPENDENCY_WAIT_DISABLED_V1"
SEED_DECLINED_MARKER = "AI_DEPENDENCY_WAIT_SEED_DECLINED_V1"
DISABLED_MARKER_RE = re.compile(r"<!--\s*AI_DEPENDENCY_WAIT_DISABLED_V1 issue=([0-9]{1,10})\s*-->")
LATCH_MARKER_PREFIX = "<!-- ai:needs-human-latch "
LATCH_MARKER_RE = re.compile(r"<!-- ai:needs-human-latch reason=dependency_wait target=(\S{1,200}) since=(\S{1,64}) -->")
SECURITY_FINDING_MARKER = "<!-- ai:security-finding:"
GENERIC_DEPENDS_RE = re.compile(r"(?mi)^\s*-?\s*Depends\s+on\s*:\s*#([1-9][0-9]{0,9})\s*$")
GENERIC_DEPENDS_LINE_RE = re.compile(r"(?mi)^\s*-?\s*Depends\s+on\s*:")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
WAIT_KINDS = ("pr", "release", "issue")
COMMENT_WINDOW = 30
PAGE_SIZE = 50
MAX_EXTRA_WAITING_PAGES = 4
MAX_COMMENT_PAGES = 10
MAX_EVENT_PAGES = 10
DEFAULT_ESCALATE_DAYS = 7
LOG_PREFIX = "DEPENDENCY_WAIT_RESUME"
PARK_LOG_PREFIX = "DEPENDENCY_WAIT_PARK"
PARK_PHASES = ("clarify", "plan")
WAITING_LINE_RE = re.compile(r"^\s*WAITING:\s*(.*\S)\s*$", re.IGNORECASE)
WAITING_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_WAITING_REPO = r"([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})"
_WAITING_NUMBER = r"([1-9][0-9]{0,9})"
WAITING_PR_RE = re.compile(r"^pr=" + _WAITING_REPO + "#" + _WAITING_NUMBER + r"$", re.IGNORECASE)
WAITING_ISSUE_RE = re.compile(r"^issue=" + _WAITING_REPO + "#" + _WAITING_NUMBER + r"$", re.IGNORECASE)
WAITING_RELEASE_RE = re.compile(
	r"^release=" + _WAITING_REPO + r"@([A-Za-z0-9_.-]{1,100})\s+contains=" + _WAITING_REPO + "#" + _WAITING_NUMBER + r"$",
	re.IGNORECASE,
)
WAITING_LINE_LIMIT = 300
WAITING_OUTPUT_LIMIT = 4 * 1024 * 1024
RUN_URL_RE = re.compile(r"^https://\S{1,300}$")

_ISSUE_FIELDS = (
	"number body labels(first: 50) { nodes { name } } "
	"comments(last: %d) { totalCount nodes { author { login } body createdAt } }" % COMMENT_WINDOW
)
CANDIDATES_QUERY = (
	"query($owner: String!, $name: String!) { viewer { login } "
	"repository(owner: $owner, name: $name) { "
	'waiting: issues(labels: ["%s"], states: OPEN, first: %d) { pageInfo { hasNextPage endCursor } nodes { %s } } '
	'blocked: issues(labels: ["%s"], states: OPEN, first: %d, orderBy: {field: UPDATED_AT, direction: DESC}) { nodes { %s } } '
	"} }"
) % (WAIT_LABEL, PAGE_SIZE, _ISSUE_FIELDS, BLOCKED_LABEL, PAGE_SIZE, _ISSUE_FIELDS)
WAITING_PAGE_QUERY = (
	"query($owner: String!, $name: String!, $cursor: String!) { "
	"repository(owner: $owner, name: $name) { "
	'waiting: issues(labels: ["%s"], states: OPEN, first: %d, after: $cursor) { pageInfo { hasNextPage endCursor } nodes { %s } } '
	"} }"
) % (WAIT_LABEL, PAGE_SIZE, _ISSUE_FIELDS)
_TARGET_FIELDS = (
	"__typename ... on PullRequest { state merged mergeCommit { oid } } "
	"... on Issue { state labels(first: 50) { nodes { name } } }"
)


def _resume_enabled() -> bool:
	"""DEPENDENCY_WAIT_RESUME_ENABLED, default on; only an explicit off value disables it.

	Copied from ``security_dependency._dependency_wait_resume_enabled`` (not
	imported: ``python3 -I`` keeps the script directory off ``sys.path``). A
	parity test pins the two together.
	"""
	value = os.environ.get("DEPENDENCY_WAIT_RESUME_ENABLED", "true").strip().lower()
	return value not in ("false", "0", "no", "off")


def _escalate_days() -> int:
	raw = os.environ.get("DEPENDENCY_WAIT_ESCALATE_DAYS", str(DEFAULT_ESCALATE_DAYS)).strip()
	if not re.fullmatch(r"[0-9]{1,4}", raw):
		return DEFAULT_ESCALATE_DAYS
	value = int(raw)
	return value if 1 <= value <= 9999 else DEFAULT_ESCALATE_DAYS


def _now() -> dt.datetime:
	return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
	return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> dt.datetime | None:
	if not isinstance(value, str) or not value or len(value) > 64:
		return None
	try:
		parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None
	if parsed.tzinfo is None:
		return None
	return parsed.astimezone(dt.timezone.utc)


def _token(value: Any, limit: int = 200) -> str:
	text = re.sub(r"[^A-Za-z0-9_.:/#@=-]", "_", str(value))
	return text[:limit] or "-"


def _log(issue: Any, target: str, outcome: str, detail: str = "") -> None:
	line = f"{LOG_PREFIX} issue={_token(issue, 12)} target={_token(target)} outcome={outcome}"
	if detail:
		line += f" detail={_token(detail, 80)}"
	print(line, flush=True)


# ---------------------------------------------------------------------------
# GitHub access (one class so tests can swap it for a fake)
# ---------------------------------------------------------------------------


class GitHub:
	def __init__(self, repository: str):
		self.repository = repository
		self.calls = 0

	def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
		self.calls += 1
		return subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False, timeout=120)

	def _json(self, arguments: list[str]) -> Any | None:
		result = self._run(arguments)
		if result.returncode != 0:
			return None
		try:
			return json.loads(result.stdout)
		except json.JSONDecodeError:
			return None

	def graphql(self, query: str, variables: dict[str, str]) -> dict[str, Any] | None:
		"""Raw GraphQL response (``data`` plus any ``errors``), or None when the call failed.

		``gh api graphql`` exits non-zero on a partial ``errors[]`` response but
		still prints the body, so the body is parsed regardless of the exit code.
		"""
		arguments = ["api", "graphql", "-f", f"query={query}"]
		for key, value in variables.items():
			arguments.extend(["-f", f"{key}={value}"])
		result = self._run(arguments)
		try:
			data = json.loads(result.stdout or "")
		except json.JSONDecodeError:
			return None
		if not isinstance(data, dict):
			return None
		if result.returncode != 0 and not data.get("errors"):
			return None
		return data

	def comments(self, number: int) -> list[dict[str, Any]] | None:
		pages = self._json(["api", "--paginate", "--slurp", f"repos/{self.repository}/issues/{number}/comments?per_page=100"])
		if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages) or len(pages) > MAX_COMMENT_PAGES:
			return None
		return [comment for page in pages for comment in page if isinstance(comment, dict)]

	def comment(self, number: int, body: str) -> bool:
		return self._run(["api", f"repos/{self.repository}/issues/{number}/comments", "-f", f"body={body}"]).returncode == 0

	def add_labels(self, number: int, labels: list[str]) -> bool:
		arguments = ["api", "-X", "POST", f"repos/{self.repository}/issues/{number}/labels"]
		for label in labels:
			arguments.extend(["-f", f"labels[]={label}"])
		return self._run(arguments).returncode == 0

	def remove_label(self, number: int, label: str) -> bool:
		# HTTP 404 means the label is already gone: the goal is met.
		result = self._run(["api", "-X", "DELETE", f"repos/{self.repository}/issues/{number}/labels/{label}"])
		return result.returncode == 0 or "HTTP 404" in (result.stderr or "") + (result.stdout or "")

	def compare(self, sha: str) -> dict[str, Any] | None:
		data = self._json(["api", f"repos/{UPSTREAM_REPO}/compare/{sha}...{UPSTREAM_RELEASE_REF}"])
		return data if isinstance(data, dict) else None

	def label_events(self, number: int) -> list[dict[str, Any]] | None:
		pages = self._json(["api", "--paginate", "--slurp", f"repos/{self.repository}/issues/{number}/events?per_page=100"])
		if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages) or len(pages) > MAX_EVENT_PAGES:
			return None
		return [event for page in pages for event in page if isinstance(event, dict)]


# ---------------------------------------------------------------------------
# Issue snapshots and marker parsing
# ---------------------------------------------------------------------------


def _normalize_comment(raw: Any) -> dict[str, Any] | None:
	"""Same shape for GraphQL (author.login/createdAt) and REST (user.login/created_at) comments."""
	if not isinstance(raw, dict):
		return None
	author = raw.get("author") if isinstance(raw.get("author"), dict) else raw.get("user")
	login = author.get("login") if isinstance(author, dict) else ""
	body = raw.get("body")
	return {
		"author": login if isinstance(login, str) else "",
		"body": body if isinstance(body, str) else "",
		"created_at": raw.get("createdAt") or raw.get("created_at") or "",
	}


class IssueSnapshot:
	def __init__(self, node: dict[str, Any]):
		self.number = node.get("number") if isinstance(node.get("number"), int) else 0
		self.body = node.get("body") if isinstance(node.get("body"), str) else ""
		labels = ((node.get("labels") or {}).get("nodes")) or []
		self.labels = {label.get("name") for label in labels if isinstance(label, dict) and isinstance(label.get("name"), str)}
		comments = node.get("comments") or {}
		window = [c for c in (_normalize_comment(raw) for raw in (comments.get("nodes") or [])) if c]
		total = comments.get("totalCount")
		self.total_comments = total if isinstance(total, int) else len(window)
		self.comments = window
		self.complete = self.total_comments <= len(window)

	def ensure_full_history(self, gh: Any) -> bool:
		"""Bounded paginated REST read when the 30-comment window is incomplete."""
		if self.complete:
			return True
		history = gh.comments(self.number)
		if history is None:
			return False
		self.comments = [c for c in (_normalize_comment(raw) for raw in history) if c]
		self.complete = True
		return True


def _trusted(comments: list[dict[str, Any]], login: str) -> list[tuple[int, dict[str, Any]]]:
	return [(index, comment) for index, comment in enumerate(comments) if login and comment.get("author") == login]


def _validate_wait(payload: Any, repo: str, issue_number: int, now: dt.datetime) -> dict[str, Any] | None:
	if not isinstance(payload, dict):
		return None
	kind = payload.get("kind")
	number = payload.get("number")
	target_repo = payload.get("repo")
	since = _parse_ts(payload.get("since"))
	if kind not in WAIT_KINDS or isinstance(number, bool) or not isinstance(number, int) or number <= 0 or number > 9_999_999_999:
		return None
	if not isinstance(target_repo, str) or not REPO_RE.match(target_repo):
		return None
	if since is None or since > now:
		return None
	target_repo_cmp = target_repo.lower()
	if kind == "pr" and target_repo_cmp not in (repo.lower(), UPSTREAM_REPO):
		return None
	if kind == "release" and target_repo_cmp != UPSTREAM_REPO:
		return None
	if kind == "issue" and (target_repo_cmp != repo.lower() or number == issue_number):
		return None
	return {"kind": kind, "repo": target_repo, "number": number, "since": payload["since"], "since_dt": since, "source": payload.get("source") if isinstance(payload.get("source"), str) else ""}


def newest_trusted_wait(comments: list[dict[str, Any]], login: str, repo: str, issue_number: int, now: dt.datetime) -> tuple[int, dict[str, Any] | None] | None:
	"""Index and validated payload of the newest trusted wait marker.

	Returns None when no trusted marker exists, ``(index, None)`` when the
	newest trusted marker is invalid (an older valid one never stands in).
	"""
	for index, comment in reversed(_trusted(comments, login)):
		if WAIT_MARKER_TOKEN not in comment["body"]:
			continue
		matches = WAIT_MARKER_RE.findall(comment["body"])
		if not matches:
			return index, None
		try:
			payload = json.loads(matches[-1])
		except json.JSONDecodeError:
			return index, None
		return index, _validate_wait(payload, repo, issue_number, now)
	return None


def target_label(wait: dict[str, Any]) -> str:
	if wait["kind"] == "release":
		return f"release:{wait['repo']}#{wait['number']}@{UPSTREAM_RELEASE_REF}"
	return f"{wait['kind']}:{wait['repo']}#{wait['number']}"


def already_resumed(comments: list[dict[str, Any]], login: str, issue_number: int, since: str, after_index: int) -> bool:
	for index, comment in _trusted(comments, login):
		if index <= after_index:
			continue
		for match in RESUME_MARKER_RE.finditer(comment["body"]):
			if int(match.group(1)) == issue_number and match.group(3) == since:
				return True
	return False


def _has_trusted_latch(comments: list[dict[str, Any]], login: str, since: str) -> bool:
	for _index, comment in _trusted(comments, login):
		for match in LATCH_MARKER_RE.finditer(comment["body"]):
			if match.group(2) == since:
				return True
	return False


def _newest_trusted_latch_is_ours(comments: list[dict[str, Any]], login: str, since: str) -> bool:
	for _index, comment in reversed(_trusted(comments, login)):
		if LATCH_MARKER_PREFIX not in comment["body"]:
			continue
		matches = list(LATCH_MARKER_RE.finditer(comment["body"]))
		return bool(matches) and matches[-1].group(2) == since
	return False


def _has_trusted_disabled(comments: list[dict[str, Any]], login: str, issue_number: int, after_index: int) -> bool:
	for index, comment in _trusted(comments, login):
		if index <= after_index:
			continue
		for match in DISABLED_MARKER_RE.finditer(comment["body"]):
			if int(match.group(1)) == issue_number:
				return True
	return False


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def fetch_candidates(gh: Any, repo: str) -> dict[str, Any] | None:
	"""Call 1 (plus at most 4 waiting-page follow-ups). None on any failure."""
	owner, name = repo.split("/", 1)
	data = gh.graphql(CANDIDATES_QUERY, {"owner": owner, "name": name})
	if not isinstance(data, dict) or data.get("errors"):
		return None
	payload = data.get("data") or {}
	viewer = payload.get("viewer") or {}
	repository = payload.get("repository")
	if not isinstance(repository, dict):
		return None
	waiting_conn = repository.get("waiting") or {}
	blocked_conn = repository.get("blocked") or {}
	waiting = [node for node in (waiting_conn.get("nodes") or []) if isinstance(node, dict)]
	blocked = [node for node in (blocked_conn.get("nodes") or []) if isinstance(node, dict)]
	page_info = waiting_conn.get("pageInfo") or {}
	overflow = False
	pages = 0
	while page_info.get("hasNextPage") and isinstance(page_info.get("endCursor"), str):
		if pages >= MAX_EXTRA_WAITING_PAGES:
			overflow = True
			break
		pages += 1
		more = gh.graphql(WAITING_PAGE_QUERY, {"owner": owner, "name": name, "cursor": page_info["endCursor"]})
		if not isinstance(more, dict) or more.get("errors"):
			# The first pages are still usable; the rest waits for the next tick.
			overflow = True
			break
		conn = (((more.get("data") or {}).get("repository")) or {}).get("waiting") or {}
		waiting.extend(node for node in (conn.get("nodes") or []) if isinstance(node, dict))
		page_info = conn.get("pageInfo") or {}
	login = viewer.get("login") if isinstance(viewer.get("login"), str) else ""
	return {"login": login, "waiting": waiting, "blocked": blocked, "overflow": overflow}


def build_targets_query(targets: list[tuple[str, int]]) -> tuple[str, dict[tuple[str, int], tuple[str, str]]]:
	"""One aliased query for every distinct target; repo/number are validated before they are inlined."""
	by_repo: dict[str, list[int]] = {}
	for target_repo, number in targets:
		by_repo.setdefault(target_repo, [])
		if number not in by_repo[target_repo]:
			by_repo[target_repo].append(number)
	lines = ["query {"]
	aliases: dict[tuple[str, int], tuple[str, str]] = {}
	for repo_index, (target_repo, numbers) in enumerate(sorted(by_repo.items())):
		owner, name = target_repo.split("/", 1)
		repo_alias = f"r{repo_index}"
		lines.append(f"  {repo_alias}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{")
		for number in sorted(numbers):
			lines.append(f"    n{number}: issueOrPullRequest(number: {int(number)}) {{ {_TARGET_FIELDS} }}")
			aliases[(target_repo, number)] = (repo_alias, f"n{number}")
		lines.append("  }")
	lines.append("}")
	return "\n".join(lines), aliases


def fetch_targets(gh: Any, targets: list[tuple[str, int]]) -> dict[tuple[str, int], dict[str, Any] | None]:
	"""Call 2. A target that errored or is null maps to None (never satisfied)."""
	if not targets:
		return {}
	query, aliases = build_targets_query(targets)
	data = gh.graphql(query, {})
	result: dict[tuple[str, int], dict[str, Any] | None] = {key: None for key in aliases}
	if not isinstance(data, dict):
		return result
	errored: set[tuple[str, ...]] = set()
	for error in data.get("errors") or []:
		path = error.get("path") if isinstance(error, dict) else None
		if isinstance(path, list):
			errored.add(tuple(str(part) for part in path[:2]))
			errored.add(tuple(str(part) for part in path[:1]))
	payload = data.get("data") or {}
	for key, (repo_alias, node_alias) in aliases.items():
		if (repo_alias,) in errored or (repo_alias, node_alias) in errored:
			continue
		repository = payload.get(repo_alias)
		node = repository.get(node_alias) if isinstance(repository, dict) else None
		result[key] = node if isinstance(node, dict) and node.get("__typename") in ("PullRequest", "Issue") else None
	return result


def release_contains(gh: Any, sha: str, cache: dict[str, bool | None]) -> bool | None:
	"""True when ``stable`` contains ``sha``; None when the compare is unreadable.

	In ``<sha>...stable``, ``ahead`` means ``stable`` has the SHA plus newer
	commits (the confine plan's ``behind`` is reversed; stuck-states P10).
	"""
	if not SHA_RE.match(sha):
		return None
	if sha not in cache:
		data = gh.compare(sha)
		status = data.get("status") if isinstance(data, dict) else None
		cache[sha] = None if status not in ("ahead", "identical", "behind", "diverged") else status in ("ahead", "identical")
	return cache[sha]


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def decide(wait: dict[str, Any], node: dict[str, Any] | None, released: Callable[[str], bool | None], now: dt.datetime, days: int) -> str:
	"""resume_answer | resume_reclarify | waiting | escalate | invalid_marker | invalid_escalate | read_failed."""
	overdue = now - wait["since_dt"] > dt.timedelta(days=days)
	if node is None:
		return "read_failed"
	typename = node.get("__typename")
	expected = "Issue" if wait["kind"] == "issue" else "PullRequest"
	if typename != expected:
		# Never satisfied; still ages so a bad target cannot strand the issue.
		return "invalid_escalate" if overdue else "invalid_marker"
	state = node.get("state")
	if wait["kind"] == "issue":
		labels = ((node.get("labels") or {}).get("nodes")) or []
		names = {label.get("name") for label in labels if isinstance(label, dict)}
		if state == "CLOSED":
			return "resume_answer" if MERGED_LABEL in names else "resume_reclarify"
		if state != "OPEN":
			return "read_failed"
	else:
		merged = node.get("merged") is True or state == "MERGED"
		if merged:
			if wait["kind"] == "pr":
				return "resume_answer"
			oid = ((node.get("mergeCommit") or {}).get("oid")) or ""
			contained = released(oid) if isinstance(oid, str) else None
			if contained is None:
				return "read_failed"
			if contained:
				return "resume_answer"
		elif state == "CLOSED":
			return "resume_reclarify"
		elif state != "OPEN":
			return "read_failed"
	return "escalate" if overdue else "waiting"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def clear_latch(gh: Any, issue: IssueSnapshot, login: str, wait: dict[str, Any]) -> str:
	"""Remove ai:needs-human only when this wait's own pipeline latch put it there (P8c rule)."""
	if NEEDS_HUMAN_LABEL not in issue.labels:
		return ""
	if not _newest_trusted_latch_is_ours(issue.comments, login, wait["since"]):
		return "latch_kept"
	events = gh.label_events(issue.number)
	if events is None:
		return "latch_kept"
	newest_actor = None
	for event in events:
		if event.get("event") == "labeled" and ((event.get("label") or {}).get("name")) == NEEDS_HUMAN_LABEL:
			actor = event.get("actor") or {}
			newest_actor = actor.get("login") if isinstance(actor, dict) else None
	if newest_actor != login:
		return "latch_kept"
	if gh.remove_label(issue.number, NEEDS_HUMAN_LABEL):
		issue.labels.discard(NEEDS_HUMAN_LABEL)
		return "latch_cleared"
	return "latch_kept"


def resume(gh: Any, issue: IssueSnapshot, wait: dict[str, Any], decision: str) -> str:
	target = target_label(wait)
	command = "/answer [auto-answered-by-poller]" if decision == "resume_answer" else "/reclarify"
	outcome_token = "answer" if decision == "resume_answer" else "reclarify"
	reason = "Dependency resolved" if decision == "resume_answer" else "Dependency closed without merging; re-checking the premise"
	body = (
		f"{command}\n\n_{reason}: {target}._\n"
		f"<!-- {RESUME_MARKER} issue={issue.number} target={target} since={wait['since']} outcome={outcome_token} -->"
	)
	if decision == "resume_answer":
		# plan.yml's /answer gate accepts ai:blocked (-> ai:planning) but not the
		# wait label, so add ai:blocked BEFORE posting: the comment starts plan.yml
		# at once. The wait label stays until the comment has posted, so a failed
		# post (or a failed rollback) never drops the issue out of the waiting scan.
		if not gh.add_labels(issue.number, [BLOCKED_LABEL]):
			return "read_failed"
		issue.labels.add(BLOCKED_LABEL)
	if not gh.comment(issue.number, body):
		if decision == "resume_answer" and not gh.remove_label(issue.number, BLOCKED_LABEL):
			# Both labels remain: the issue stays in the waiting scan and retries.
			_log(issue.number, target, "read_failed", "rollback_failed")
		return "read_failed"
	issue.comments.append({"author": "", "body": body, "created_at": ""})
	if gh.remove_label(issue.number, WAIT_LABEL):
		# clarify.yml adds ai:clarification and plan.yml adds ai:planning, but
		# neither removes the wait label. A failed removal is retried by the
		# already_resumed branch of scan().
		issue.labels.discard(WAIT_LABEL)
	return "resumed" if decision == "resume_answer" else "reclarified"


def escalate(gh: Any, issue: IssueSnapshot, login: str, wait: dict[str, Any], days: int) -> str:
	if NEEDS_HUMAN_LABEL in issue.labels:
		return "waiting"
	target = target_label(wait)
	if not _has_trusted_latch(issue.comments, login, wait["since"]):
		body = (
			f"This issue has waited more than {days} day(s) on its dependency {target} "
			f"(waiting since {wait['since']}). It keeps `{WAIT_LABEL}` and still resumes on its own when the "
			f"dependency lands. To resume it now, replace `{WAIT_LABEL}` with `{BLOCKED_LABEL}` and then reply "
			f"`/answer` (the plan workflow does not act on `/answer` while only the wait label is set).\n"
			f"<!-- ai:needs-human-latch reason=dependency_wait target={target} since={wait['since']} -->"
		)
		if not gh.comment(issue.number, body):
			return "read_failed"
	if not gh.add_labels(issue.number, [NEEDS_HUMAN_LABEL]):
		return "read_failed"
	issue.labels.add(NEEDS_HUMAN_LABEL)
	return "escalated"


def seed_candidate(node: dict[str, Any]) -> tuple[IssueSnapshot, int] | None:
	"""A blocked issue with exactly one generic same-repo ``Depends on: #N`` line."""
	issue = IssueSnapshot(node)
	if not issue.number or WAIT_LABEL in issue.labels:
		return None
	# Any human latch holds the issue: nothing is written until a person removes it.
	if issue.labels & ({SECURITY_LABEL, ORCHESTRATOR_MANAGED_LABEL} | SEED_HUMAN_LATCH_LABELS) or SECURITY_FINDING_MARKER in issue.body:
		return None
	lines = GENERIC_DEPENDS_LINE_RE.findall(issue.body)
	matches = GENERIC_DEPENDS_RE.findall(issue.body)
	if len(lines) != 1 or len(matches) != 1:
		return None
	dependency = int(matches[0])
	if dependency == issue.number:
		return None
	return issue, dependency


def seed_blocked(gh: Any, issue: IssueSnapshot, dependency: int, repo: str, login: str, now: dt.datetime) -> str:
	if not issue.ensure_full_history(gh):
		return "read_failed"
	if any(WAIT_MARKER_TOKEN in comment["body"] for _index, comment in _trusted(issue.comments, login)):
		found = newest_trusted_wait(issue.comments, login, repo, issue.number, now)
		if found is None or found[1] is None or already_resumed(issue.comments, login, issue.number, found[1]["since"], found[0]):
			return ""
		if found[1]["source"] != "seed" or found[1]["kind"] != "issue" or found[1]["number"] != dependency:
			# Only finish this helper's own half-applied seed for the same
			# dependency; never undo a deliberate move back to ai:blocked.
			return ""
		if any(index > found[0] for index, _comment in _trusted(issue.comments, login)):
			# A failed label write posts nothing after the seed marker, so a
			# later pipeline comment (latch, decline note, ...) means the seed
			# was applied: leave the issue alone without an events read.
			return ""
		events = gh.label_events(issue.number)
		if events is None:
			return "read_failed"
		if any(
			event.get("event") == "labeled"
			and isinstance(event.get("label"), dict) and event["label"].get("name") == WAIT_LABEL
			and isinstance(event.get("actor"), dict) and event["actor"].get("login") == login
			for event in events
		):
			# The pipeline applied the wait label before; it was removed on
			# purpose. Post one note so later ticks skip without an events read.
			body = (
				f"This issue was moved back to `{BLOCKED_LABEL}` after it waited on #{dependency}, so the poller "
				f"no longer moves it to `{WAIT_LABEL}`. Reply `/answer` to resume it.\n"
				f"<!-- {SEED_DECLINED_MARKER} issue={issue.number} -->"
			)
			return "seed_declined" if gh.comment(issue.number, body) else "read_failed"
		# A seed whose marker posted but whose label change failed: finish it
		# without a second marker, or the issue stays ai:blocked for good.
		if not gh.add_labels(issue.number, [WAIT_LABEL]):
			return "read_failed"
		if not gh.remove_label(issue.number, BLOCKED_LABEL):
			_log(issue.number, f"issue:{repo}#{dependency}", "seeded", "blocked_label_kept")
		return "seeded"
	payload = {"kind": "issue", "repo": repo, "number": dependency, "since": _iso(now), "source": "seed"}
	body = (
		f"Waiting on dependency: issue={repo}#{dependency}\n\n"
		f"This issue declares `Depends on: #{dependency}`. It moves from `{BLOCKED_LABEL}` to `{WAIT_LABEL}`, "
		f"and the poller resumes it automatically once #{dependency} closes.\n"
		f"<!-- {WAIT_MARKER_TOKEN} {json.dumps(payload, separators=(',', ':'))} -->"
	)
	if not gh.comment(issue.number, body):
		return "read_failed"
	if not gh.add_labels(issue.number, [WAIT_LABEL]):
		return "read_failed"
	if not gh.remove_label(issue.number, BLOCKED_LABEL):
		_log(issue.number, f"issue:{repo}#{dependency}", "seeded", "blocked_label_kept")
	return "seeded"


def switch_off(gh: Any, issue: IssueSnapshot, repo: str, login: str, now: dt.datetime) -> str:
	if not issue.ensure_full_history(gh):
		return "read_failed"
	found = newest_trusted_wait(issue.comments, login, repo, issue.number, now)
	after_index = found[0] if found else -1
	if found is not None and found[1] is not None and already_resumed(issue.comments, login, issue.number, found[1]["since"], found[0]):
		# Already resumed into another phase: drop the stale wait label only,
		# never add ai:blocked beside the issue's current phase label.
		return "disabled_moved" if gh.remove_label(issue.number, WAIT_LABEL) else "read_failed"
	if not _has_trusted_disabled(issue.comments, login, issue.number, after_index):
		body = (
			"Automatic dependency resume is disabled (`DEPENDENCY_WAIT_RESUME_ENABLED=false`), so this issue moves "
			f"back to `{BLOCKED_LABEL}`. Reply `/answer` once the dependency lands.\n"
			f"<!-- {DISABLED_MARKER} issue={issue.number} -->"
		)
		if not gh.comment(issue.number, body):
			return "read_failed"
	if not gh.add_labels(issue.number, [BLOCKED_LABEL]):
		return "read_failed"
	if not gh.remove_label(issue.number, WAIT_LABEL):
		return "read_failed"
	return "disabled_moved"


# ---------------------------------------------------------------------------
# Park (stuck-states P8a-2): clarify and plan turn a WAITING: line into a wait
# ---------------------------------------------------------------------------


def _park_log(op: str, issue: Any, target: str, phase: str, outcome: str, detail: str = "") -> None:
	"""stderr, so stdout carries only the one JSON result line."""
	line = f"{PARK_LOG_PREFIX} op={op} issue={_token(issue, 12)} target={_token(target)} phase={_token(phase, 16)} outcome={outcome}"
	if detail:
		line += f" detail={_token(detail, 80)}"
	print(line, file=sys.stderr, flush=True)


def _clean_waiting_line(value: str) -> str:
	return re.sub(r"[\x00-\x1f\x7f]", "", value)[:WAITING_LINE_LIMIT]


def first_waiting_line(text: str, ignore_fences: bool = False) -> str | None:
	"""Value of the first ``WAITING:`` line outside ```/~~~ fences (clarify's BLOCKED toggle).

	``ignore_fences`` matches plan's scan, whose input already has closed
	fences removed and whose BLOCKED scan does not skip fenced lines.
	"""
	in_code = False
	for line in text.splitlines():
		if not ignore_fences and WAITING_FENCE_RE.match(line):
			in_code = not in_code
			continue
		if in_code:
			continue
		match = WAITING_LINE_RE.match(line)
		if match:
			return match.group(1)
	return None


def _is_self_target(kind: str, target_repo: str, number: int, repo: str, issue_number: int) -> bool:
	return kind in ("pr", "issue") and target_repo.lower() == repo.lower() and number == issue_number


def parse_waiting(text: str, repo: str, issue_number: int, now: dt.datetime | None = None, ignore_fences: bool = False) -> dict[str, Any]:
	"""none | disabled | invalid | waiting, for the first unfenced WAITING: line.

	The target is validated with the scan's own ``_validate_wait`` so the
	writer (park) and the reader (scan) cannot disagree on what is valid.
	"""
	now = now or _now()
	raw = first_waiting_line(text, ignore_fences)
	if raw is None:
		return {"status": "none"}
	line = _clean_waiting_line(raw)
	if not _resume_enabled():
		return {"status": "disabled", "line": line}
	value = raw.strip()
	if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
		value = value[1:-1].strip()
	pr_match = WAITING_PR_RE.match(value)
	issue_match = WAITING_ISSUE_RE.match(value)
	if pr_match:
		kind, target_repo, number = "pr", pr_match.group(1), int(pr_match.group(2))
	elif issue_match:
		kind, target_repo, number = "issue", issue_match.group(1), int(issue_match.group(2))
	else:
		release = WAITING_RELEASE_RE.match(value)
		if not release:
			return {"status": "invalid", "reason": "grammar", "line": line}
		if release.group(1).lower() != UPSTREAM_REPO or release.group(2) != UPSTREAM_RELEASE_REF:
			return {"status": "invalid", "reason": "release_not_upstream", "line": line}
		kind, target_repo, number = "release", release.group(3), int(release.group(4))
	if _is_self_target(kind, target_repo, number, repo, issue_number):
		return {"status": "invalid", "reason": "self_reference", "line": line}
	wait = _validate_wait({"kind": kind, "repo": target_repo, "number": number, "since": _iso(now)}, repo, issue_number, now)
	if wait is None:
		return {"status": "invalid", "reason": "target_not_allowed", "line": line}
	return {"status": "waiting", "kind": kind, "repo": wait["repo"], "number": number, "target": target_label(wait), "line": line}


def park_target_line(wait: dict[str, Any]) -> str:
	if wait["kind"] == "release":
		return f"release={UPSTREAM_REPO}@{UPSTREAM_RELEASE_REF} contains={wait['repo']}#{wait['number']}"
	return f"{wait['kind']}={wait['repo']}#{wait['number']}"


def park(gh: Any, repo: str, issue_number: int, kind: str, target_repo: str, number: int, phase: str, run_url: str = "", now: dt.datetime | None = None) -> str:
	"""parked | disabled | invalid_args | already_resolved | invalid_target | read_failed | comment_failed | label_failed.

	The marker comment posts before the label, so the wait label never sits
	on an issue without the trusted marker the scan needs.
	"""
	now = now or _now()
	if not _resume_enabled():
		return "disabled"
	if phase not in PARK_PHASES or isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
		return "invalid_args"
	if not isinstance(target_repo, str) or not isinstance(kind, str) or _is_self_target(kind, target_repo, number, repo, issue_number):
		return "invalid_args"
	since = _iso(now)
	wait = _validate_wait({"kind": kind, "repo": target_repo, "number": number, "since": since}, repo, issue_number, now)
	if wait is None:
		return "invalid_args"
	nodes = fetch_targets(gh, [(wait["repo"], wait["number"])])
	days = _escalate_days()
	release_cache: dict[str, bool | None] = {}
	decision = decide(wait, nodes.get((wait["repo"], wait["number"])), lambda sha: release_contains(gh, sha, release_cache), now, days)
	if decision in ("resume_answer", "resume_reclarify"):
		return "already_resolved"
	if decision in ("invalid_marker", "invalid_escalate"):
		return "invalid_target"
	if decision not in ("waiting", "escalate"):
		return "read_failed"
	payload = {"kind": wait["kind"], "repo": wait["repo"], "number": wait["number"], "since": since}
	lines = [
		f"Waiting on dependency: {park_target_line(wait)}",
		"",
		f"The {phase} agent found that this issue has to wait for the dependency above, so it is parked in "
		f"`{WAIT_LABEL}` instead of `{BLOCKED_LABEL}`. The poller replies `/answer` once the dependency lands, "
		f"or `/reclarify` if it closes without merging, and asks for a person after {days} day(s) "
		f"(`DEPENDENCY_WAIT_ESCALATE_DAYS`). To resume sooner, replace `{WAIT_LABEL}` with `{BLOCKED_LABEL}` "
		f"and then reply `/answer`.",
	]
	if isinstance(run_url, str) and RUN_URL_RE.match(run_url):
		lines.extend(["", f"Run: {run_url}"])
	lines.append(f"<!-- {WAIT_MARKER_TOKEN} {json.dumps(payload, separators=(',', ':'))} -->")
	if not gh.comment(issue_number, "\n".join(lines)):
		return "comment_failed"
	if not gh.add_labels(issue_number, [WAIT_LABEL]):
		return "label_failed"
	return "parked"


def _parse_waiting_command(args: argparse.Namespace) -> dict[str, Any]:
	phase = args.phase or "-"
	if not REPO_RE.match(args.repo or "") or not re.fullmatch(r"[1-9][0-9]{0,9}", args.issue_number or ""):
		_park_log("parse", args.issue_number or "-", "-", phase, "error", "invalid_arguments")
		return {"status": "error"}
	try:
		with open(args.output_file, "r", encoding="utf-8", errors="replace") as handle:
			text = handle.read(WAITING_OUTPUT_LIMIT)
	except OSError:
		_park_log("parse", args.issue_number, "-", phase, "error", "output_unreadable")
		return {"status": "error"}
	result = parse_waiting(text, args.repo, int(args.issue_number), ignore_fences=bool(args.ignore_fences))
	if result["status"] != "none":
		_park_log("parse", args.issue_number, result.get("target", "-"), phase, result["status"], result.get("reason", ""))
	return result


def _park_command(args: argparse.Namespace, github_factory: Callable[[str], Any]) -> dict[str, Any]:
	target = "-"
	if not REPO_RE.match(args.repo or "") or not re.fullmatch(r"[1-9][0-9]{0,9}", args.issue_number or "") \
		or not re.fullmatch(r"[1-9][0-9]{0,9}", args.number or "") or not REPO_RE.match(args.target_repo or ""):
		_park_log("park", args.issue_number or "-", target, args.phase, "invalid_args")
		return {"status": "invalid_args", "target": target}
	number = int(args.number)
	if args.kind in WAIT_KINDS:
		target = target_label({"kind": args.kind, "repo": args.target_repo, "number": number})
	status = park(github_factory(args.repo), args.repo, int(args.issue_number), args.kind, args.target_repo, number, args.phase, args.run_url or "")
	# label_failed means the trusted marker comment already posted: say so, so an
	# operator can spot a marker stranded on an issue that went to ai:blocked.
	_park_log("park", args.issue_number, target, args.phase, status, "marker_posted" if status == "label_failed" else "")
	return {"status": status, "target": target}


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def scan(gh: Any, repo: str, now: dt.datetime | None = None) -> dict[str, int]:
	now = now or _now()
	summary = {"waiting": 0, "seeded": 0}
	candidates = fetch_candidates(gh, repo)
	if candidates is None:
		print(f"{LOG_PREFIX} op=scan outcome=read_failed api_calls={gh.calls}", flush=True)
		return summary
	login = candidates["login"]
	waiting_nodes = candidates["waiting"]
	summary["waiting"] = len(waiting_nodes)
	if candidates["overflow"]:
		print(f"{LOG_PREFIX} op=scan outcome=overflow waiting_read={len(waiting_nodes)}", flush=True)
	if not login:
		if waiting_nodes or candidates["blocked"]:
			_log("-", "-", "identity_unavailable")
		print(f"{LOG_PREFIX} op=scan waiting={summary['waiting']} seeded=0 api_calls={gh.calls}", flush=True)
		return summary

	if not _resume_enabled():
		for node in waiting_nodes:
			issue = IssueSnapshot(node)
			if issue.number:
				_log(issue.number, "-", switch_off(gh, issue, repo, login, now))
		print(f"{LOG_PREFIX} op=scan waiting={summary['waiting']} seeded=0 api_calls={gh.calls} enabled=false", flush=True)
		return summary

	for node in candidates["blocked"]:
		seed = seed_candidate(node)
		if seed is None:
			continue
		issue, dependency = seed
		outcome = seed_blocked(gh, issue, dependency, repo, login, now)
		if outcome:
			_log(issue.number, f"issue:{repo}#{dependency}", outcome)
			if outcome == "seeded":
				summary["seeded"] += 1

	days = _escalate_days()
	pending: list[tuple[IssueSnapshot, dict[str, Any], int]] = []
	for node in waiting_nodes:
		issue = IssueSnapshot(node)
		if not issue.number:
			continue
		found = newest_trusted_wait(issue.comments, login, repo, issue.number, now)
		if found is None and not issue.complete:
			if not issue.ensure_full_history(gh):
				_log(issue.number, "-", "read_failed", "comments")
				continue
			found = newest_trusted_wait(issue.comments, login, repo, issue.number, now)
		if found is None or found[1] is None:
			_log(issue.number, "-", "invalid_marker")
			continue
		index, wait = found
		if already_resumed(issue.comments, login, issue.number, wait["since"], index):
			# Retry a wait-label removal that failed after the resume comment.
			if WAIT_LABEL in issue.labels and gh.remove_label(issue.number, WAIT_LABEL):
				issue.labels.discard(WAIT_LABEL)
			latch = clear_latch(gh, issue, login, wait)
			_log(issue.number, target_label(wait), latch or "already_resumed")
			continue
		pending.append((issue, wait, index))

	targets = sorted({(wait["repo"], wait["number"]) for _issue, wait, _index in pending})
	nodes = fetch_targets(gh, targets) if targets else {}
	release_cache: dict[str, bool | None] = {}
	for issue, wait, _index in pending:
		target = target_label(wait)
		decision = decide(wait, nodes.get((wait["repo"], wait["number"])), lambda sha: release_contains(gh, sha, release_cache), now, days)
		if decision in ("waiting", "invalid_marker", "read_failed"):
			_log(issue.number, target, decision)
			continue
		# A write is due: make sure the dedupe checks saw the whole history.
		if not issue.complete:
			if not issue.ensure_full_history(gh):
				_log(issue.number, target, "read_failed", "comments")
				continue
			found = newest_trusted_wait(issue.comments, login, repo, issue.number, now)
			if found is None or found[1] is None or found[1]["since"] != wait["since"]:
				_log(issue.number, target, "invalid_marker", "history_changed")
				continue
			if already_resumed(issue.comments, login, issue.number, wait["since"], found[0]):
				_log(issue.number, target, "already_resumed")
				continue
		if decision in ("resume_answer", "resume_reclarify"):
			outcome = resume(gh, issue, wait, decision)
			_log(issue.number, target, outcome)
			if outcome in ("resumed", "reclarified"):
				latch = clear_latch(gh, issue, login, wait)
				if latch:
					_log(issue.number, target, latch)
		else:
			outcome = escalate(gh, issue, login, wait, days)
			_log(issue.number, target, outcome, "invalid_target" if decision == "invalid_escalate" else "")
	print(f"{LOG_PREFIX} op=scan waiting={summary['waiting']} seeded={summary['seeded']} api_calls={gh.calls}", flush=True)
	return summary


def main(argv: list[str] | None = None, github_factory: Callable[[str], Any] = GitHub) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	scan_parser = sub.add_parser("scan")
	scan_parser.add_argument("--repo", required=True)
	parse_parser = sub.add_parser("parse-waiting")
	parse_parser.add_argument("--output-file", required=True)
	parse_parser.add_argument("--repo", required=True)
	parse_parser.add_argument("--issue-number", required=True)
	parse_parser.add_argument("--phase", choices=PARK_PHASES, default=None)
	parse_parser.add_argument("--ignore-fences", action="store_true")
	park_parser = sub.add_parser("park")
	park_parser.add_argument("--repo", required=True)
	park_parser.add_argument("--issue-number", required=True)
	park_parser.add_argument("--kind", required=True, choices=WAIT_KINDS)
	park_parser.add_argument("--target-repo", required=True)
	park_parser.add_argument("--number", required=True)
	park_parser.add_argument("--phase", required=True, choices=PARK_PHASES)
	park_parser.add_argument("--run-url", default="")
	argv_list = list(sys.argv[1:] if argv is None else argv)
	json_command = bool(argv_list) and argv_list[0] in ("parse-waiting", "park")
	try:
		args = parser.parse_args(argv_list)
	except SystemExit:
		if json_command:
			# One JSON line on stdout is the contract the workflows parse.
			print(json.dumps({"status": "error"}), flush=True)
			_park_log("parse" if argv_list[0] == "parse-waiting" else "park", "-", "-", "-", "error", "bad_arguments")
			return 0
		print(f"{LOG_PREFIX} op=scan outcome=skip reason=bad_arguments", flush=True)
		return 0
	if args.command in ("parse-waiting", "park"):
		try:
			if args.command == "parse-waiting":
				result = _parse_waiting_command(args)
			else:
				result = _park_command(args, github_factory)
		except Exception as exc:  # noqa: BLE001 - fail-open: the caller takes the BLOCKED path
			_park_log("parse" if args.command == "parse-waiting" else "park", "-", "-", "-", "error", type(exc).__name__)
			result = {"status": "error"}
		print(json.dumps(result, separators=(",", ":")), flush=True)
		return 0
	if not REPO_RE.match(args.repo or ""):
		print(f"{LOG_PREFIX} op=scan outcome=skip reason=invalid_repo", flush=True)
		return 0
	try:
		scan(github_factory(args.repo), args.repo)
	except Exception as exc:  # noqa: BLE001 - fail-open: the poll must continue
		print(f"{LOG_PREFIX} op=scan outcome=error error={type(exc).__name__}", flush=True)
		print(f"::warning::{LOG_PREFIX} scan failed ({type(exc).__name__}); retrying next tick.", flush=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())

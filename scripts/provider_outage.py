#!/usr/bin/env python3
"""Model-provider outage handling for review/autofix (issue #6633, re-issue of #5773).

A repo-wide model-provider outage (OpenRouter out of credits, a revoked key,
sustained 429/5xx) used to be recorded as a separate defect on every open PR:
identical-failure caps, `ai:review-blocked` labels and per-PR heal issues
(2026-09-30: 35 capped PRs, 23 labelled PRs, 13 labelled issues). This helper
lets the failure path name such a failure `provider_unavailable`, records one
repo-wide tracker issue (label `ai:provider-outage`) with one alert, and lets
the scheduled sweep resume work once a live probe succeeds.

Subcommands (all fail open: an unexpected error prints a log line and exits 0):

  classify   Evidence regex over pipeline logs, confirmed by a live provider
             probe. Prints `reason=provider_unavailable provider= status=` or
             `reason=none`. Evidence alone never classifies (a PR can quote
             "402 Payment Required"); a missing key or an unknown probe gives
             `none`, which is the legacy behaviour.
  probe      The live probe alone: `outcome=up|down|unknown status=`.
  open       Open (or reuse) the tracker of one kind; prints `alert=0|1`,
             writes the alert text to --alert-out when this call opened it.
  close      Close an open tracker of one kind (Claude pool capacity cleared).
  mark-label Post the `ai:provider-outage-label:v1` marker on an item whose
             `ai:review-blocked` label an outage caused.
  sweep      Scheduled pass: pause while an outage tracker is open and the
             probe is not up; on recovery re-dispatch the PRs whose newest
             current-head failure marker is `provider_unavailable`, remove
             marker-backed outage labels, re-run (opt-in) or report release
             runs, close the tracker and write one "recovered" alert.

Markers (trusted only when written by the GH_PAT login, never by anyone else):
  tracker body:  <!-- ai:provider-outage:v1 kind=<outage|capacity> provider=<p> status=<s> opened_at=<iso> -->
  resume line:   <!-- ai:provider-outage-resume:v1 prs=<csv> labels=<item:pr csv> release_runs=<csv> -->
  tracker comment: <!-- ai:provider-outage-release-run:v1 run=<id> workflow=<file> -->
  item comment:  <!-- ai:provider-outage-label:v1 item=<n> pr=<p> head=<sha> run=<id> -->

Batching contract (CLAUDE.md §15 / unattended §14):
  - Every tracker lookup is one `GET issues?labels=ai:provider-outage&state=open`;
    `GET /user` follows only when that list is non-empty (or before a create).
  - `open` adds one POST (create) and one more list after a 5 s settle; a
    duplicate it created costs one PATCH (close).
  - `sweep` with no open tracker costs the single list call. While an outage
    is open it adds one probe (two HTTPS calls to the provider, no GitHub
    call). On recovery: one GraphQL call per 50 open PRs (head SHA and the
    latest 100 comments), a paginated REST comment read only for a PR with
    more than 100 comments and no decisive marker in that window, one
    `gh workflow run` per resumed PR (capped by
    PROVIDER_OUTAGE_RESUME_MAX_DISPATCH), one `issues?labels=ai:review-blocked`
    listing plus one paginated comment read per labelled item, one label
    DELETE per marker-backed label, one tracker comment read, one PATCH of
    the resume marker per batch, and one comment + close.
  - Failure: a failed read skips that item for this tick; the tracker stays
    open and the next tick retries.

Environment (defaults live here, unattended §8):
  PROVIDER_OUTAGE_CLASSIFY_ENABLED (true), PROVIDER_OUTAGE_RESUME_ENABLED (true),
  PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED (false), PROVIDER_OUTAGE_PROBE_MODEL
  (openai/gpt-6-luna), PROVIDER_OUTAGE_RESUME_MAX_DISPATCH (50),
  PROVIDER_OUTAGE_REVIEW_WORKFLOW (internal-review.yml here, ai-review.yml in
  consumers), OPENROUTER_API_KEY (probe only; never printed), REPOSITORY.

Log prefix: PROVIDER_OUTAGE.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

SOURCE_REPO = "shubhodeep1/coding-workflows"
TRACKER_LABEL = "ai:provider-outage"
REVIEW_BLOCKED_LABEL = "ai:review-blocked"
PROVIDER_UNAVAILABLE_REASON = "provider_unavailable"
KIND_TITLES = {"outage": "AI Provider Outage", "capacity": "AI Claude Pool Capacity"}
PROBE_TIMEOUT_SECS = 15
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
LABEL_MARKER_LEAD = dt.timedelta(hours=1)
# A cached probe result older than this is re-probed, so a transient early
# 429/5xx cannot keep suppressing alerts for the rest of a long job.
PROBE_CACHE_TTL_SECS = 300

TRACKER_MARKER_RE = re.compile(
	r"<!-- ai:provider-outage:v1 kind=(?P<kind>outage|capacity) provider=(?P<provider>[a-z0-9_-]{1,40}) "
	r"status=(?P<status>[a-z0-9_-]{1,20}) opened_at=(?P<opened>[0-9TZ:.+-]{1,40}) -->"
)
RESUME_MARKER_RE = re.compile(
	r"<!-- ai:provider-outage-resume:v1 prs=(?P<prs>[0-9,]*) labels=(?P<labels>[0-9:,]*) release_runs=(?P<runs>[0-9,]*) -->"
)
RELEASE_RUN_MARKER_RE = re.compile(r"<!-- ai:provider-outage-release-run:v1 run=(?P<run>[0-9]{1,20}) workflow=(?P<wf>[A-Za-z0-9_.-]{1,80}) -->")
LABEL_MARKER_RE = re.compile(
	r"<!-- ai:provider-outage-label:v1 item=(?P<item>[0-9]{1,10}) pr=(?P<pr>[0-9]{1,10}) head=(?P<head>[0-9a-f]{40}|none) run=(?P<run>[0-9]{1,20}|none) -->"
)
FAILURE_MARKER_RE = re.compile(r"<!--\s*review-autofix-failure:v1\s+(?P<fields>[^>]*?)\s*-->")
FAILURE_CAP_MARKER_RE = re.compile(r"<!--\s*review-autofix-failure-cap:v1\b")
MARKER_FIELD_RE = re.compile(r"(?P<key>[a-z_]+)=(?P<value>[A-Za-z0-9_.:-]+)")
SUCCESS_MARKERS = ("AI autofix editor summary",)

# Evidence patterns, checked in order; the first match names the status.
# Ported from the provider classifier in security_audit.sh.
_EVIDENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
	("402", re.compile(r"HTTP(?: Error)?[ :/]*402\b|\b402\b[^\n]{0,40}payment|payment required|insufficient[ _]credits|insufficient_quota", re.IGNORECASE)),
	("401", re.compile(r"HTTP(?: Error)?[ :/]*401\b|\b401\b[^\n]{0,40}unauthori[sz]ed|no auth credentials|invalid api key", re.IGNORECASE)),
	("429", re.compile(r"HTTP(?: Error)?[ :/]*429\b|\b429\b[^\n]{0,40}too many requests|rate[ _-]?limit(?:ed| exceeded)", re.IGNORECASE)),
	("5xx", re.compile(r"HTTP(?: Error)?[ :/]*5[0-9]{2}\b|\b5[0-9]{2}\b[^\n]{0,40}(?:bad gateway|service unavailable|gateway timeout|internal server error)", re.IGNORECASE)),
)
_JSON_CODE_RE = re.compile(r'"error"\s*:\s*\{[^{}]{0,400}?"code"\s*:\s*"?(?P<code>[0-9]{3})"?')
_SAFE_TOKEN_RE = re.compile(r"[^a-z0-9_-]")


def _log(message: str) -> None:
	print(f"PROVIDER_OUTAGE {message}", flush=True)


def _bool_env(name: str, default: bool) -> bool:
	raw = os.environ.get(name, "").strip().lower()
	if not raw:
		return default
	return raw in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
	try:
		value = int(os.environ.get(name, "") or default)
	except ValueError:
		return default
	return value if value > 0 else default


def _token(value: Any, limit: int = 40) -> str:
	return _SAFE_TOKEN_RE.sub("_", str(value or "").strip().lower())[:limit]


def _utc_now() -> dt.datetime:
	return dt.datetime.now(dt.timezone.utc)


def _parse_utc(value: Any) -> dt.datetime | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None
	return parsed if parsed.tzinfo is not None else None


def _status_from_code(code: int) -> str | None:
	if code in (401, 402, 429):
		return str(code)
	if 500 <= code <= 599:
		return "5xx"
	return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def provider_failure_status(texts: Iterable[str]) -> dict[str, str] | None:
	"""Name a model-provider failure from pipeline-emitted log lines.

	Lines framed as untrusted PR data (`UNTRUSTED_DATA:`) and diff-hunk lines
	(`+` / `-`) are ignored. The result is only evidence: callers confirm it
	with a live probe before acting on it.
	"""
	found: str | None = None
	for text in texts:
		for raw_line in str(text or "").splitlines():
			line = raw_line.strip()
			if not line or line.startswith(("UNTRUSTED_DATA:", "+", "-")):
				continue
			json_match = _JSON_CODE_RE.search(line)
			if json_match:
				status = _status_from_code(int(json_match.group("code")))
				if status:
					found = status if found is None else found
					if status == "402":
						return {"provider": "openrouter", "status": "402"}
					continue
			for status, pattern in _EVIDENCE_PATTERNS:
				if pattern.search(line):
					if status == "402":
						return {"provider": "openrouter", "status": "402"}
					found = status if found is None else found
					break
	return {"provider": "openrouter", "status": found} if found else None


HttpFn = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


def _default_http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
	request = urllib.request.Request(url, data=body, method=method, headers=headers)
	try:
		with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECS) as response:  # noqa: S310 - fixed https URLs
			return int(response.status), response.read(65536)
	except urllib.error.HTTPError as exc:
		try:
			payload = exc.read(65536)
		except Exception:
			payload = b""
		return int(exc.code), payload


def probe_openrouter(key: str, model: str, http: HttpFn | None = None) -> dict[str, str]:
	"""One key check plus one 1-token completion. Never prints the key."""
	if not key:
		return {"outcome": "unknown", "status": "no_key"}
	http = http or _default_http
	headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
	try:
		code, payload = http("GET", OPENROUTER_KEY_URL, headers, None)
		if code == 401:
			return {"outcome": "down", "status": "401"}
		if code == 200:
			try:
				data = (json.loads(payload.decode("utf-8", "replace")) or {}).get("data") or {}
			except (ValueError, AttributeError):
				data = {}
			remaining = data.get("limit_remaining") if isinstance(data, dict) else None
			if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining <= 0:
				return {"outcome": "down", "status": "402"}
		body = json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "OK"}]}).encode("utf-8")
		code, payload = http("POST", OPENROUTER_COMPLETIONS_URL, headers, body)
	except Exception:
		return {"outcome": "unknown", "status": "network"}
	if 200 <= code < 300:
		# A 200 can still carry an error object (mid-stream provider error).
		match = _JSON_CODE_RE.search(payload.decode("utf-8", "replace")[:4000])
		status = _status_from_code(int(match.group("code"))) if match else None
		return {"outcome": "down", "status": status} if status else {"outcome": "up", "status": str(code)}
	status = _status_from_code(code)
	if status:
		return {"outcome": "down", "status": status}
	return {"outcome": "unknown", "status": str(code)}


def cached_probe(cache_path: str | None, http: HttpFn | None = None) -> dict[str, str]:
	if cache_path:
		try:
			if time.time() - Path(cache_path).stat().st_mtime > PROBE_CACHE_TTL_SECS:
				raise OSError("stale probe cache")
			cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
			if isinstance(cached, dict) and cached.get("outcome") in {"up", "down", "unknown"}:
				return {"outcome": str(cached["outcome"]), "status": _token(cached.get("status"), 20)}
		except (OSError, ValueError):
			pass
	result = probe_openrouter(os.environ.get("OPENROUTER_API_KEY", ""), os.environ.get("PROVIDER_OUTAGE_PROBE_MODEL", "") or "openai/gpt-6-luna", http)
	if cache_path:
		try:
			Path(cache_path).write_text(json.dumps(result), encoding="utf-8")
		except OSError:
			pass
	return result


def classify(texts: Iterable[str], probe: Callable[[], dict[str, str]]) -> dict[str, str]:
	evidence = provider_failure_status(texts)
	if evidence is None:
		return {"reason": "none", "detail": "no_evidence"}
	result = probe()
	if result.get("outcome") != "down":
		return {"reason": "none", "detail": f"probe_{_token(result.get('outcome'), 10)}"}
	return {"reason": PROVIDER_UNAVAILABLE_REASON, "provider": "openrouter", "status": _token(result.get("status"), 20) or evidence["status"]}


# ---------------------------------------------------------------------------
# GitHub access (one class so tests can swap it for a fake)
# ---------------------------------------------------------------------------


class GitHub:
	def __init__(self, repository: str):
		self.repository = repository
		self._login: str | None = None

	def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
		return subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)

	def _json(self, arguments: list[str]) -> Any | None:
		result = self._run(arguments)
		if result.returncode != 0:
			return None
		try:
			return json.loads(result.stdout)
		except json.JSONDecodeError:
			return None

	def login(self) -> str:
		if self._login is None:
			result = self._run(["api", "user", "--jq", '.login // ""'])
			self._login = result.stdout.strip() if result.returncode == 0 else ""
		return self._login

	def list_issues(self, label: str) -> list[dict[str, Any]] | None:
		data = self._json(["api", f"repos/{self.repository}/issues?labels={label}&state=open&per_page=100"])
		return data if isinstance(data, list) else None

	def create_issue(self, title: str, body: str) -> int | None:
		data = self._json(["api", "-X", "POST", f"repos/{self.repository}/issues", "-f", f"title={title}", "-f", f"body={body}", "-f", f"labels[]={TRACKER_LABEL}"])
		number = data.get("number") if isinstance(data, dict) else None
		return number if isinstance(number, int) else None

	def ensure_label(self) -> None:
		self._run(["label", "create", TRACKER_LABEL, "--repo", self.repository, "--color", "b60205", "--description", "Model provider outage tracker (automation)"])

	def close_issue(self, number: int) -> bool:
		return self._run(["api", "-X", "PATCH", f"repos/{self.repository}/issues/{number}", "-f", "state=closed"]).returncode == 0

	def patch_body(self, number: int, body: str) -> bool:
		return self._run(["api", "-X", "PATCH", f"repos/{self.repository}/issues/{number}", "-f", f"body={body}"]).returncode == 0

	def comment(self, number: int, body: str) -> bool:
		return self._run(["api", f"repos/{self.repository}/issues/{number}/comments", "-f", f"body={body}"]).returncode == 0

	def comments(self, number: int) -> list[dict[str, Any]] | None:
		pages = self._json(["api", "--paginate", "--slurp", f"repos/{self.repository}/issues/{number}/comments?per_page=100"])
		if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
			return None
		return [comment for page in pages for comment in page if isinstance(comment, dict)]

	def pr_page(self, cursor: str) -> dict[str, Any] | None:
		owner, name = self.repository.split("/", 1)
		args = ["api", "graphql", "-f", f"query={PR_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
		if cursor:
			args.extend(["-f", f"cursor={cursor}"])
		data = self._json(args)
		if not isinstance(data, dict) or data.get("errors"):
			return None
		repo = (data.get("data") or {}).get("repository")
		return repo if isinstance(repo, dict) else None

	def dispatch(self, workflow: str, ref: str, pr_number: int) -> bool:
		# Same normalisation as the sweep's own dispatch (unset keeps the
		# previous fail-safe `false`).
		allow_edits = "true" if _bool_env("ALLOW_WORKFLOW_EDITS", False) else "false"
		return self._run(["workflow", "run", workflow, "--repo", self.repository, "--ref", ref, "-f", f"pr_number={pr_number}", "-f", f"allow_workflow_edits={allow_edits}"]).returncode == 0

	def remove_label(self, number: int, label: str) -> bool:
		return self._run(["api", "-X", "DELETE", f"repos/{self.repository}/issues/{number}/labels/{label}"]).returncode == 0

	def rerun_failed_jobs(self, run_id: int) -> bool:
		return self._run(["api", "-X", "POST", f"repos/{self.repository}/actions/runs/{run_id}/rerun-failed-jobs"]).returncode == 0


PR_QUERY = """
query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){
    defaultBranchRef{name}
    pullRequests(first:50,after:$cursor,states:OPEN,orderBy:{field:UPDATED_AT,direction:DESC}){
      nodes{
        number
        isDraft
        headRefOid
        comments(last:100){
          nodes{author{login}body createdAt}
          pageInfo{hasPreviousPage}
        }
      }
      pageInfo{hasNextPage endCursor}
    }
  }
}
"""


def _author(comment: dict[str, Any]) -> str:
	login = (comment.get("author") or {}).get("login") if isinstance(comment.get("author"), dict) else None
	if not login and isinstance(comment.get("user"), dict):
		login = comment["user"].get("login")
	return str(login or "").strip().lower()


def _created(comment: dict[str, Any]) -> dt.datetime | None:
	return _parse_utc(comment.get("createdAt") or comment.get("created_at"))


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


def find_trackers(gh: Any, kind: str | None = None) -> list[dict[str, Any]] | None:
	"""Trusted open trackers (lowest number first), or None when unreadable."""
	issues = gh.list_issues(TRACKER_LABEL)
	if issues is None:
		return None
	candidates = [issue for issue in issues if isinstance(issue, dict) and "pull_request" not in issue]
	if not candidates:
		return []
	login = str(gh.login() or "").strip().lower()
	if not login:
		return None
	trackers: list[dict[str, Any]] = []
	for issue in candidates:
		if _author(issue) != login or not isinstance(issue.get("number"), int):
			continue
		match = TRACKER_MARKER_RE.search(str(issue.get("body") or ""))
		if not match or (kind and match.group("kind") != kind):
			continue
		trackers.append({"number": issue["number"], "body": str(issue.get("body") or ""), "created_at": issue.get("created_at"), **match.groupdict()})
	return sorted(trackers, key=lambda item: item["number"])


def tracker_body(kind: str, provider: str, status: str, now: dt.datetime) -> str:
	opened = now.strftime("%Y-%m-%dT%H:%M:%SZ")
	if kind == "outage":
		text = (
			f"Model provider `{provider}` is failing (status `{status}`) for review/autofix. "
			"While this issue is open the review sweep skips review dispatches; review failures with reason "
			f"`{PROVIDER_UNAVAILABLE_REASON}` count toward no identical-failure cap, apply no `ai:review-blocked` "
			"label and file no per-PR heal issue. Check the provider account and the `OPENROUTER_API_KEY` secret. "
			"The scheduled sweep probes the provider and closes this issue on recovery."
		)
	else:
		text = (
			f"Every Claude pool account was at its usage gate (`{status}`); roles fell back to codex/OpenCode. "
			"Reviews continue. This issue closes on the next run that gets a Claude account."
		)
	return (
		f"<!-- ai:provider-outage:v1 kind={kind} provider={provider} status={status} opened_at={opened} -->\n"
		f"{text}\n\n<!-- ai:provider-outage-resume:v1 prs= labels= release_runs= -->\n"
	)


def open_tracker(gh: Any, *, kind: str, provider: str, status: str, release_run: str = "", release_workflow: str = "", settle_secs: float = 5.0, now: dt.datetime | None = None) -> dict[str, Any]:
	provider, status = _token(provider) or "openrouter", _token(status, 20) or "unknown"
	trackers = find_trackers(gh, kind)
	if trackers is None:
		_log(f"op=open kind={kind} provider={provider} status={status} outcome=failed reason=tracker_lookup_failed")
		return {"outcome": "failed", "alert": False, "provider": provider, "status": status}
	number: int | None = trackers[0]["number"] if trackers else None
	outcome = "already_open"
	if number is None:
		gh.ensure_label()
		created = gh.create_issue(KIND_TITLES.get(kind, KIND_TITLES["outage"]), tracker_body(kind, provider, status, now or _utc_now()))
		if created is None:
			_log(f"op=open kind={kind} provider={provider} status={status} outcome=failed reason=create_failed")
			return {"outcome": "failed", "alert": False, "provider": provider, "status": status}
		if settle_secs > 0:
			time.sleep(settle_secs)
		again = find_trackers(gh, kind) or []
		lowest = min([item["number"] for item in again] + [created])
		if lowest != created:
			gh.close_issue(created)
			number, outcome = lowest, "duplicate_closed"
		else:
			number, outcome = created, "opened"
	if release_run.isdigit() and number is not None:
		workflow = re.sub(r"[^A-Za-z0-9_.-]", "_", release_workflow or "unknown")[:80]
		if not gh.comment(number, f"Release run {release_run} failed while the model provider was unavailable.\n\n<!-- ai:provider-outage-release-run:v1 run={release_run} workflow={workflow} -->"):
			_log(f"op=open tracker={number} release_run={release_run} outcome=release_run_record_failed")
	_log(f"op=open kind={kind} provider={provider} status={status} tracker={number} outcome={outcome}")
	return {"outcome": outcome, "alert": outcome == "opened", "number": number, "provider": provider, "status": status}


def open_alert_text(kind: str, provider: str, status: str, repository: str, number: Any) -> str:
	if kind == "capacity":
		return f"Claude pool capacity: every account is at its usage gate ({status}) in {repository}; roles fall back to codex/OpenCode. Tracker #{number}."
	return (
		f"Model provider outage: {provider} returned {status} in {repository}. Review/autofix failures are recorded as "
		f"{PROVIDER_UNAVAILABLE_REASON} (no cap, no ai:review-blocked) and the sweep pauses until the provider recovers. "
		f"Check the provider account and the OPENROUTER_API_KEY secret. Tracker #{number}."
	)


def mark_label(gh: Any, *, item: int, pr: int, head: str, run: str) -> bool:
	head = head if re.fullmatch(r"[0-9a-f]{40}", head or "") else "none"
	run = run if (run or "").isdigit() else "none"
	ok = gh.comment(item, f"`{REVIEW_BLOCKED_LABEL}` was applied while the model provider was unavailable; it is removed automatically when the provider recovers.\n\n<!-- ai:provider-outage-label:v1 item={item} pr={pr} head={head} run={run} -->")
	_log(f"op=mark_label item={item} pr={pr} outcome={'posted' if ok else 'failed'}")
	return ok


# ---------------------------------------------------------------------------
# Sweep / resume
# ---------------------------------------------------------------------------


def parse_resume(body: str) -> dict[str, set[str]]:
	match = RESUME_MARKER_RE.search(body or "")
	if not match:
		return {"prs": set(), "labels": set(), "release_runs": set()}
	split = lambda value: {part for part in value.split(",") if part}  # noqa: E731
	return {"prs": split(match.group("prs")), "labels": split(match.group("labels")), "release_runs": split(match.group("runs"))}


def render_resume(body: str, state: dict[str, set[str]]) -> str:
	order = lambda values: ",".join(sorted(values, key=lambda value: [int(part) for part in value.split(":") if part.isdigit()]))  # noqa: E731
	line = f"<!-- ai:provider-outage-resume:v1 prs={order(state['prs'])} labels={order(state['labels'])} release_runs={order(state['release_runs'])} -->"
	if RESUME_MARKER_RE.search(body or ""):
		return RESUME_MARKER_RE.sub(line, body, count=1)
	return (body or "").rstrip("\n") + "\n\n" + line + "\n"


def _failure_fields(body: str) -> dict[str, str] | None:
	match = FAILURE_MARKER_RE.search(body or "")
	if not match:
		return None
	fields: dict[str, str] = {}
	for field in MARKER_FIELD_RE.finditer(match.group("fields")):
		fields.setdefault(field.group("key"), field.group("value"))
	return fields


def resume_decision(comments: list[dict[str, Any]], *, head: str, login: str) -> str:
	"""`resume`, `other` or `undecided` from the newest decisive trusted comment.

	Decisive: a trusted failure marker for the current head, or a trusted
	editor summary (a later success). Oldest-first input.
	"""
	for comment in reversed(comments):
		if not isinstance(comment, dict) or _author(comment) != login:
			continue
		body = str(comment.get("body") or "")
		fields = _failure_fields(body)
		if fields is not None:
			if fields.get("head", "").lower() != head:
				continue
			return "resume" if fields.get("reason") == PROVIDER_UNAVAILABLE_REASON else "other"
		if any(marker in body for marker in SUCCESS_MARKERS):
			return "other"
	return "undecided"


def label_marker_still_valid(comments: list[dict[str, Any]], *, login: str, not_before: dt.datetime | None) -> dict[str, str] | None:
	"""Newest trusted label marker, unless a newer trusted non-provider failure
	marker or identical-failure cap marker supersedes it. Oldest-first input."""
	for comment in reversed(comments):
		if not isinstance(comment, dict) or _author(comment) != login:
			continue
		body = str(comment.get("body") or "")
		match = LABEL_MARKER_RE.search(body)
		if match:
			created = _created(comment)
			if not_before is not None and (created is None or created < not_before):
				return None
			return {**match.groupdict(), "created": created.isoformat() if created else ""}
		if FAILURE_CAP_MARKER_RE.search(body):
			return None
		fields = _failure_fields(body)
		if fields is not None and fields.get("reason") != PROVIDER_UNAVAILABLE_REASON:
			return None
	return None


def superseded_after(comments: list[dict[str, Any]], *, login: str, since: dt.datetime | None) -> bool:
	"""True when a trusted non-provider failure marker or a cap marker was
	posted after ``since`` (an unknown ``since`` counts every such marker)."""
	for comment in comments:
		if not isinstance(comment, dict) or _author(comment) != login:
			continue
		created = _created(comment)
		if since is not None and created is not None and created <= since:
			continue
		body = str(comment.get("body") or "")
		if FAILURE_CAP_MARKER_RE.search(body):
			return True
		fields = _failure_fields(body)
		if fields is not None and fields.get("reason") != PROVIDER_UNAVAILABLE_REASON:
			return True
	return False


def _write_output(path: str | None, values: dict[str, str]) -> None:
	if not path:
		return
	try:
		with open(path, "a", encoding="utf-8") as handle:
			for key, value in values.items():
				handle.write(f"{key}={value}\n")
	except OSError as exc:
		_log(f"op=write_output outcome=failed reason=os_error type={type(exc).__name__}")


def _write_alert(path: str | None, text: str) -> None:
	if path and text:
		try:
			Path(path).write_text(text + "\n", encoding="utf-8")
		except OSError as exc:
			_log(f"op=write_alert outcome=failed reason=os_error type={type(exc).__name__}")


def resume(gh: Any, tracker: dict[str, Any], *, workflow: str, max_dispatch: int, release_rerun: bool, repository: str) -> dict[str, Any]:
	"""Resume after recovery. Returns counts and whether the tracker may close."""
	login = str(gh.login() or "").strip().lower()
	state = parse_resume(tracker["body"])
	body = tracker["body"]
	complete = True
	dispatched: list[int] = []
	cursor = ""
	default_branch = ""
	while True:
		page = gh.pr_page(cursor)
		if page is None:
			_log(f"op=resume tracker={tracker['number']} outcome=partial reason=pr_listing_unavailable")
			complete = False
			break
		default_branch = ((page.get("defaultBranchRef") or {}).get("name")) or default_branch
		prs = page.get("pullRequests") or {}
		for node in prs.get("nodes") or []:
			if not isinstance(node, dict) or node.get("isDraft") is True or not isinstance(node.get("number"), int):
				continue
			number, head = node["number"], str(node.get("headRefOid") or "").lower()
			if str(number) in state["prs"]:
				continue
			comments_payload = node.get("comments") or {}
			comments = comments_payload.get("nodes") if isinstance(comments_payload, dict) else None
			if not isinstance(comments, list):
				continue
			decision = resume_decision(comments, head=head, login=login)
			if decision == "undecided" and (comments_payload.get("pageInfo") or {}).get("hasPreviousPage") is True:
				full = gh.comments(number)
				if full is None:
					complete = False
					_log(f"op=resume pr={number} outcome=skip reason=comment_history_unavailable")
					continue
				decision = resume_decision(full, head=head, login=login)
			if decision != "resume":
				continue
			if len(dispatched) >= max_dispatch:
				complete = False
				continue
			if not default_branch or not gh.dispatch(workflow, default_branch, number):
				complete = False
				_log(f"op=resume pr={number} outcome=dispatch_failed")
				continue
			dispatched.append(number)
			state["prs"].add(str(number))
			_log(f"op=resume pr={number} head={head} outcome=dispatched")
		page_info = prs.get("pageInfo") or {}
		if page_info.get("hasNextPage") is not True:
			break
		cursor = page_info.get("endCursor") or ""
		if not cursor:
			complete = False
			break
	if dispatched:
		body = render_resume(body, state)
		if not gh.patch_body(tracker["number"], body):
			complete = False
			_log(f"op=resume tracker={tracker['number']} outcome=resume_marker_write_failed")

	# Marker-backed outage labels only; never guess.
	removed: list[str] = []
	opened_at = _parse_utc(tracker.get("opened")) or _parse_utc(tracker.get("created_at"))
	not_before = opened_at - LABEL_MARKER_LEAD if opened_at else None
	labelled = gh.list_issues(REVIEW_BLOCKED_LABEL)
	if labelled is None:
		complete = False
	else:
		for item in labelled:
			if not isinstance(item, dict) or not isinstance(item.get("number"), int):
				continue
			comments = gh.comments(item["number"])
			if comments is None:
				complete = False
				continue
			marker = label_marker_still_valid(comments, login=login, not_before=not_before)
			if marker is None or int(marker["item"]) != item["number"]:
				continue
			key = f"{marker['item']}:{marker['pr']}"
			if key in state["labels"]:
				continue
			pr = int(marker["pr"])
			if pr != item["number"]:
				pr_comments = gh.comments(pr)
				if pr_comments is None:
					complete = False
					continue
				if superseded_after(pr_comments, login=login, since=_parse_utc(marker.get("created"))):
					continue
			# A failed judge during the outage writes no provider_unavailable
			# failure marker, so the PR scan above cannot find it: dispatch
			# its review here before removing the label.
			if str(pr) not in state["prs"]:
				if len(dispatched) >= max_dispatch or not default_branch or not gh.dispatch(workflow, default_branch, pr):
					complete = False
					_log(f"op=resume item={item['number']} pr={pr} outcome=dispatch_failed")
					continue
				dispatched.append(pr)
				state["prs"].add(str(pr))
				_log(f"op=resume pr={pr} outcome=dispatched source=label_marker")
			if not gh.remove_label(item["number"], REVIEW_BLOCKED_LABEL):
				complete = False
				_log(f"op=resume item={item['number']} outcome=label_remove_failed")
				continue
			state["labels"].add(key)
			removed.append(key)
			_log(f"op=resume item={item['number']} pr={pr} outcome=label_removed")
	if removed:
		body = render_resume(body, state)
		if not gh.patch_body(tracker["number"], body):
			complete = False
			_log(f"op=resume tracker={tracker['number']} outcome=resume_marker_write_failed")

	# Release runs recorded by the heal intake.
	release_report: list[str] = []
	tracker_comments = gh.comments(tracker["number"])
	if tracker_comments is None:
		complete = False
	else:
		for comment in tracker_comments:
			if _author(comment) != login:
				continue
			match = RELEASE_RUN_MARKER_RE.search(str(comment.get("body") or ""))
			if not match or match.group("run") in state["release_runs"]:
				continue
			run_id = match.group("run")
			if release_rerun and repository == SOURCE_REPO:
				if gh.rerun_failed_jobs(int(run_id)):
					release_report.append(f"run {run_id} ({match.group('wf')}): re-run")
				else:
					complete = False
					release_report.append(f"run {run_id} ({match.group('wf')}): re-run failed")
					continue
			else:
				release_report.append(f"run {run_id} ({match.group('wf')}): not re-run (PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED is off); re-run it by hand if still needed")
			state["release_runs"].add(run_id)
			_log(f"op=resume release_run={run_id} outcome={'rerun' if release_rerun and repository == SOURCE_REPO else 'reported'}")
		if release_report:
			body = render_resume(body, state)
			gh.patch_body(tracker["number"], body)
	return {"complete": complete, "dispatched": dispatched, "labels_removed": removed, "release_report": release_report, "state": state}


def sweep(gh: Any, *, probe: Callable[[], dict[str, str]], output_path: str | None, alert_out: str | None, status_only: bool = False) -> dict[str, Any]:
	repository = gh.repository
	values = {"paused": "false", "outage_open": "false", "capacity_open": "false", "recovered": "false"}
	if not _bool_env("PROVIDER_OUTAGE_RESUME_ENABLED", True):
		_log("op=sweep outcome=skip reason=disabled")
		_write_output(output_path, values)
		return values
	trackers = find_trackers(gh)
	if trackers is None:
		# Cannot see the tracker: pause only when the probe confirms the
		# provider is down, so a GitHub read error neither hides a real
		# outage nor stops reviews while the provider is healthy.
		if not status_only and probe().get("outcome") == "down":
			values["paused"] = "true"
		_log(f"op=sweep outcome=skip reason=tracker_lookup_failed paused={values['paused']}")
		_write_output(output_path, values)
		return values
	outage = next((item for item in trackers if item["kind"] == "outage"), None)
	capacity = next((item for item in trackers if item["kind"] == "capacity"), None)
	values["outage_open"] = "true" if outage else "false"
	values["capacity_open"] = "true" if capacity else "false"
	if status_only or outage is None:
		_log(f"op=sweep outcome={'status' if status_only else 'idle'} outage_open={values['outage_open']} capacity_open={values['capacity_open']}")
		_write_output(output_path, values)
		return values
	result = probe()
	if result.get("outcome") != "up":
		values["paused"] = "true"
		_log(f"op=sweep tracker={outage['number']} outcome=paused probe={_token(result.get('outcome'), 10)} status={_token(result.get('status'), 20)}")
		_write_output(output_path, values)
		return values
	summary = resume(
		gh,
		outage,
		workflow=os.environ.get("PROVIDER_OUTAGE_REVIEW_WORKFLOW", "") or ("internal-review.yml" if repository == SOURCE_REPO else "ai-review.yml"),
		max_dispatch=_int_env("PROVIDER_OUTAGE_RESUME_MAX_DISPATCH", 50),
		release_rerun=_bool_env("PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED", False),
		repository=repository,
	)
	if not summary["complete"]:
		_log(f"op=sweep tracker={outage['number']} outcome=resume_partial dispatched={len(summary['dispatched'])} labels_removed={len(summary['labels_removed'])}")
		_write_output(output_path, values)
		return values
	state = summary["state"]
	lines = [
		"The model provider answered the probe again; review/autofix resumed.",
		"",
		f"- Re-dispatched PRs: {', '.join('#' + value for value in sorted(state['prs'], key=int)) or 'none'}",
		f"- Removed outage `{REVIEW_BLOCKED_LABEL}` labels (item:pr): {', '.join(sorted(state['labels'])) or 'none'}",
		f"- Release runs: {'; '.join(summary['release_report']) or 'none recorded'}",
	]
	if not gh.close_issue(outage["number"]):
		# Retry on the next tick; no summary or alert until the close lands.
		_log(f"op=sweep tracker={outage['number']} outcome=close_failed")
		_write_output(output_path, values)
		return values
	gh.comment(outage["number"], "\n".join(lines))
	values["recovered"] = "true"
	_write_alert(alert_out, f"Model provider recovered in {repository}: tracker #{outage['number']} closed; re-dispatched {len(state['prs'])} PR(s), removed {len(state['labels'])} outage label(s).")
	_log(f"op=sweep tracker={outage['number']} outcome=recovered dispatched={len(state['prs'])} labels_removed={len(state['labels'])} release_runs={len(state['release_runs'])}")
	_write_output(output_path, values)
	return values


def close_tracker(gh: Any, *, kind: str, alert_out: str | None) -> str:
	trackers = find_trackers(gh, kind)
	if trackers is None:
		_log(f"op=close kind={kind} outcome=failed reason=tracker_lookup_failed")
		return "failed"
	if not trackers:
		_log(f"op=close kind={kind} outcome=none_open")
		return "none_open"
	for tracker in trackers:
		gh.comment(tracker["number"], "Recovered: a run obtained capacity again." if kind == "capacity" else "Recovered.")
		if not gh.close_issue(tracker["number"]):
			_log(f"op=close kind={kind} tracker={tracker['number']} outcome=failed reason=close_failed")
			return "failed"
	_write_alert(alert_out, f"{KIND_TITLES.get(kind, kind)} recovered in {gh.repository}: tracker #{trackers[0]['number']} closed.")
	_log(f"op=close kind={kind} tracker={trackers[0]['number']} outcome=closed")
	return "closed"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_texts(paths: Iterable[str]) -> list[str]:
	texts: list[str] = []
	for path in paths:
		try:
			with open(path, encoding="utf-8", errors="replace") as handle:
				texts.append(handle.read(2_000_000))
		except OSError:
			continue
	return texts


def _repository(args: argparse.Namespace) -> str:
	repository = getattr(args, "repo", "") or os.environ.get("REPOSITORY", "") or os.environ.get("GITHUB_REPOSITORY", "")
	if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
		raise ValueError("invalid repository")
	return repository


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	p_classify = sub.add_parser("classify")
	p_classify.add_argument("--log-file", action="append", default=[])
	p_classify.add_argument("--probe-cache", default="")
	p_probe = sub.add_parser("probe")
	p_probe.add_argument("--probe-cache", default="")
	p_open = sub.add_parser("open")
	p_open.add_argument("--repo", default="")
	p_open.add_argument("--kind", choices=sorted(KIND_TITLES), default="outage")
	p_open.add_argument("--provider", default="openrouter")
	p_open.add_argument("--status", default="unknown")
	p_open.add_argument("--release-run", default="")
	p_open.add_argument("--release-workflow", default="")
	p_open.add_argument("--alert-out", default="")
	p_close = sub.add_parser("close")
	p_close.add_argument("--repo", default="")
	p_close.add_argument("--kind", choices=sorted(KIND_TITLES), default="capacity")
	p_close.add_argument("--alert-out", default="")
	p_mark = sub.add_parser("mark-label")
	p_mark.add_argument("--repo", default="")
	p_mark.add_argument("--item", type=int, required=True)
	p_mark.add_argument("--pr", type=int, required=True)
	p_mark.add_argument("--head", default="")
	p_mark.add_argument("--run", default="")
	p_sweep = sub.add_parser("sweep")
	p_sweep.add_argument("--repo", default="")
	p_sweep.add_argument("--status-only", action="store_true")
	p_sweep.add_argument("--alert-out", default="")
	p_sweep.add_argument("--probe-cache", default="")
	args = parser.parse_args(argv)
	try:
		if args.command == "classify":
			if not _bool_env("PROVIDER_OUTAGE_CLASSIFY_ENABLED", True):
				print("reason=none detail=disabled")
				return 0
			result = classify(_read_texts(args.log_file), lambda: cached_probe(args.probe_cache or None))
			print(" ".join(f"{key}={value}" for key, value in result.items()))
			_log(f"op=classify {' '.join(f'{key}={value}' for key, value in result.items())}")
			return 0
		if args.command == "probe":
			result = cached_probe(args.probe_cache or None)
			print(f"outcome={result['outcome']} status={result['status']}")
			return 0
		gh = GitHub(_repository(args))
		if args.command == "open":
			result = open_tracker(gh, kind=args.kind, provider=args.provider, status=args.status, release_run=str(args.release_run or ""), release_workflow=args.release_workflow)
			if result.get("alert"):
				_write_alert(args.alert_out or None, open_alert_text(args.kind, result["provider"], result["status"], gh.repository, result.get("number")))
			elif result.get("outcome") == "failed":
				# No tracker could be read or created: alert anyway so the
				# outage is never silent (per-PR alerts are suppressed for it).
				_write_alert(args.alert_out or None, open_alert_text(args.kind, result["provider"], result["status"], gh.repository, "unavailable (tracker lookup or create failed)"))
			print(f"alert={1 if result.get('alert') else 0} outcome={result['outcome']} tracker={result.get('number') or ''}")
			return 0
		if args.command == "close":
			print(f"outcome={close_tracker(gh, kind=args.kind, alert_out=args.alert_out or None)}")
			return 0
		if args.command == "mark-label":
			mark_label(gh, item=args.item, pr=args.pr, head=args.head, run=args.run)
			return 0
		if args.command == "sweep":
			sweep(gh, probe=lambda: cached_probe(args.probe_cache or None), output_path=os.environ.get("GITHUB_OUTPUT"), alert_out=args.alert_out or None, status_only=args.status_only)
			return 0
	except Exception as exc:  # fail open; never print secrets (exception text is type only)
		_log(f"op={args.command} outcome=failed reason=exception type={type(exc).__name__}")
		if args.command == "sweep":
			_write_output(os.environ.get("GITHUB_OUTPUT"), {"paused": "false", "outage_open": "false", "capacity_open": "false", "recovered": "false"})
		return 0
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

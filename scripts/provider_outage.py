#!/usr/bin/env python3
"""Repo-wide model-provider outage marker, probe and automatic resume (issue #5773).

When every model call of a review/autofix run fails because the provider is
down (OpenRouter 402 credits, 401 key, or 429 / 5xx with the whole reviewer
panel down), `scripts/workflow_failure_heal.py` names the failure
`provider_unavailable`. That run labels nothing, counts toward no cap, and
starts no Claude fixer. This module keeps the one repo-wide record of the
outage and undoes its effects once the provider recovers:

  * `record` runs in the workflow failure heal intake
    (`scripts/workflow_failure_heal_intake.sh`). Every repository's
    review/autofix failure reaches that intake through the existing heal
    reporter dispatch, and the intake calls `record` for a
    `provider_unavailable` report or a failed release run. It opens at most one
    `ai:provider-outage` marker issue in coding-workflows and reports whether
    this call opened it, so the intake sends exactly one alert. A concurrent
    second creator closes its own issue as a duplicate. A failed release run is
    recorded on the open marker (`ai:provider-outage-release-run:v1`).
  * `tick` runs every 30 minutes from the `provider-outage-probe` job of
    `.github/workflows/review_autofix_sweep.yml`. With no open marker it
    returns at once. With one it makes a single 1-token completion. While that
    fails, it tells the sweep to skip its review dispatches. When it succeeds,
    it resumes:
      - re-dispatches review for every open PR, in this repository and the
        registered consumers, whose latest trusted failure marker on its
        current head is `provider_unavailable`;
      - removes `ai:review-blocked` only where the label's latest `labeled`
        event lies inside the outage window and was made by the workflow
        account, on a resumed PR or an issue it references;
      - releases a `hold` claim on a resumed PR's head only when the hold was
        posted inside the window, every failed check on the head belongs to an
        outage run, and the PR is not conflicted;
      - re-runs the newest recorded release run when
        PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED is true, and otherwise reports
        it;
    then it records what it handled (`ai:provider-outage-resume:v1`), closes
    the marker and returns the "recovered" alert text.

Trust: a marker issue counts only when the workflow account (`gh api user`,
the GH_PAT login) opened it with the `ai:provider-outage:v1` body marker, and
only that account's failure markers, labelled events and claims (plus the PR
author's claims) count. Alerts and comments name the key, never its value.

Batching contract (CLAUDE.md §15):
  input   the registry file plus this repository;
  calls   `tick` steady state: 1 read (the open marker list). With a marker:
          1 `gh api user`, 1 comment read on the marker, and the probe (one
          HTTPS call to the provider, no GitHub call). On recovery, per repo:
          1 open-PR list per 100 PRs and 1 `ai:review-blocked` issue list; per
          open PR updated since the outage opened, 1 comment read per 100
          comments; per resumed PR, 1 dispatch; per label candidate, 1 events
          read and at most 1 label delete; per held resumed PR, 1 PR read,
          1 check-run read and at most 1 claim comment; 1 release re-run when
          enabled; 1 resume comment and 1 close per open marker.
          `record`: 1 marker list and 1 `gh api user`; on create, 1 label
          create, 1 issue create and 1 re-list; on a release run, 1 comment
          read and at most 1 comment.
  output  one JSON line on stdout; `PROVIDER_OUTAGE …` log lines on stderr;
  failure fail open per repository and per PR: a read error is logged,
          listed in the resume comment and the alert, and the tick moves on.
          A failed marker list makes `tick` report `outage_open=false` so the
          sweep keeps running (never a tight retry loop).
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

_SCRIPTS = Path(__file__).resolve().parent


def _load(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


heal = _load("workflow_failure_heal", _SCRIPTS / "workflow_failure_heal.py")

OUTAGE_LABEL = "ai:provider-outage"
OUTAGE_LABEL_COLOR = "d93f0b"
OUTAGE_LABEL_DESCRIPTION = "Open model-provider outage marker; the review sweep probes the provider and closes it on recovery"
OUTAGE_MARKER_TAG = "ai:provider-outage:v1"
RELEASE_RUN_MARKER_TAG = "ai:provider-outage-release-run:v1"
RESUME_MARKER_TAG = "ai:provider-outage-resume:v1"
REVIEW_BLOCKED_LABEL = "ai:review-blocked"
SELF_REVIEW_WORKFLOW = "internal-review.yml"
CONSUMER_REVIEW_WORKFLOW = "ai-review.yml"
PROBE_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_PROBE_MODEL = "openai/gpt-6-luna"
PROBE_TIMEOUT_SECONDS = 30
MAX_PAGES = 10
RELEASE_CLAIM_BY_PREFIX = "provider-outage-probe-"

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MARKER_RE = re.compile(
	r"<!-- " + re.escape(OUTAGE_MARKER_TAG)
	+ r" provider=(?P<provider>[a-z0-9_.-]{1,40}) status=(?P<status>[0-9]{3}|5xx)"
	+ r" kind=(?P<kind>credits|auth|rate_limit|server) key=(?P<key>[A-Z][A-Z0-9_]{0,79}) -->"
)
_RELEASE_RUN_RE = re.compile(r"<!-- " + re.escape(RELEASE_RUN_MARKER_TAG) + r" run=(?P<run>[1-9][0-9]{0,19}) -->")
_RESUME_RE = re.compile(r"<!-- " + re.escape(RESUME_MARKER_TAG) + r" handled=(?P<handled>[^ ]*) -->")
_HANDLED_KEY_RE = re.compile(r"^[a-z]+:[A-Za-z0-9_.@#/-]{1,200}$")
_FIX_CLAIM_RE = re.compile(
	r"^<!-- ai:claude-fix-claim:v1 head=([0-9a-f]{40}) kind=(conflict|ci|review|blocked|hold) by=([A-Za-z0-9_-]{1,80}) -->$"
)
_FIXER_HANDOFF_RE = re.compile(r"^<!-- ai:claude-fixer-handoff:v1 kind=(?:findings|conflict) head=([0-9a-f]{40}) round=[0-9]+ -->$")
_RUN_ID_IN_URL_RE = re.compile(r"/actions/runs/([1-9][0-9]{0,19})(?:/|$)")
_ISSUE_REF_RE = re.compile(r"(?<![A-Za-z0-9_/.#-])#([1-9][0-9]{0,9})\b")
_TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
_EDITOR_SUMMARY_TEXT = "AI autofix editor summary"


def log(message: str) -> None:
	print(f"PROVIDER_OUTAGE {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def parse_time(value: Any) -> dt.datetime | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None
	return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _login(obj: Any) -> str:
	user = obj.get("user") if isinstance(obj, dict) else None
	login = user.get("login") if isinstance(user, dict) else None
	return login.casefold() if isinstance(login, str) else ""


def _ordered(comments: list) -> list[dict]:
	return sorted(
		(comment for comment in comments if isinstance(comment, dict)),
		key=lambda entry: entry.get("id") if type(entry.get("id")) is int else 0,
	)


def render_marker_body(*, provider: str, status: str, kind: str, key: str, source: str, opened_at: str) -> str:
	"""The marker issue body; the HTML marker line is what `parse_marker` reads."""
	return "\n".join([
		f"<!-- {OUTAGE_MARKER_TAG} provider={heal.safe_token(provider, 40)} status={heal.safe_token(status, 3)} "
		f"kind={heal.safe_token(kind, 20)} key={heal.safe_token(key, 80)} -->",
		"## Model provider outage",
		"",
		f"Every model call to `{heal.safe_token(provider, 40)}` failed with HTTP `{heal.safe_token(status, 3)}` "
		f"(`{heal.safe_token(kind, 20)}`) on the key `{heal.safe_token(key, 80)}`. Review/autofix runs that hit it "
		"are named `provider_unavailable`: they count toward no identical-failure cap, label nothing "
		"`ai:review-blocked`, start no Claude fixer, and file no heal issue.",
		"",
		f"- **First seen:** {heal.single_line(source, 200)} at {heal.single_line(opened_at, 40)}",
		"- **What happens next:** the `provider-outage-probe` job of `review_autofix_sweep.yml` probes the "
		"provider every 30 minutes (one 1-token call, model `PROVIDER_OUTAGE_PROBE_MODEL`). The review sweep "
		"skips its dispatches while this issue is open. On the first successful probe it re-dispatches the "
		"paused reviews, removes the outage's `ai:review-blocked` labels, releases outage-caused holds, "
		"records what it did here, and closes this issue.",
		"- **Operator action:** fix the provider account (for credits, top up). Nothing else is needed. "
		"Do not close this issue by hand unless the probe cannot succeed (for example a wrong "
		"`PROVIDER_OUTAGE_PROBE_MODEL`); closing it re-enables the sweep but resumes nothing.",
		"",
		"Issue #5773.",
	])


def parse_marker(body: Any) -> dict[str, str] | None:
	match = _MARKER_RE.search(heal.sanitize_text(body))
	if match is None:
		return None
	return {name: match.group(name) for name in ("provider", "status", "kind", "key")}


def trusted_open_markers(issues: list, workflow_login: str) -> list[dict]:
	"""Open marker issues the workflow account opened, lowest number first.

	Pull requests, issues without the label or the body marker, and issues by
	any other author are ignored.
	"""
	login = (workflow_login or "").casefold()
	markers = []
	for issue in issues:
		if not isinstance(issue, dict) or "pull_request" in issue or issue.get("state", "open") != "open":
			continue
		labels = [label.get("name") for label in issue.get("labels") or [] if isinstance(label, dict)]
		if OUTAGE_LABEL not in labels or not login or _login(issue) != login:
			continue
		fields = parse_marker(issue.get("body"))
		if fields is None or type(issue.get("number")) is not int:
			continue
		markers.append({**fields, "number": issue["number"], "created_at": issue.get("created_at"),
			"html_url": issue.get("html_url") or ""})
	return sorted(markers, key=lambda marker: marker["number"])


def render_release_run_comment(run_id: int, workflow_name: str, run_url: str) -> str:
	return "\n".join([
		f"Release run [{run_id}]({heal.single_line(run_url, 200)}) (`{heal.single_line(workflow_name, 80)}`) failed "
		"while this outage was open. Its heal diagnosis was deferred; the probe reports it, or re-runs it when "
		"`PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED=true`, after the provider recovers.",
		"",
		f"<!-- {RELEASE_RUN_MARKER_TAG} run={run_id} -->",
	])


def recorded_release_runs(comments: list, workflow_login: str) -> list[int]:
	"""Release run ids recorded on the marker by the workflow account, oldest first, unique."""
	login = (workflow_login or "").casefold()
	runs: list[int] = []
	for comment in _ordered(comments):
		if not login or _login(comment) != login:
			continue
		for match in _RELEASE_RUN_RE.finditer(heal.sanitize_text(comment.get("body"))):
			run_id = int(match.group("run"))
			if run_id not in runs:
				runs.append(run_id)
	return runs


def handled_keys(comments: list, workflow_login: str) -> set[str]:
	"""Keys an earlier resume on this marker already handled (idempotency)."""
	login = (workflow_login or "").casefold()
	handled: set[str] = set()
	for comment in _ordered(comments):
		if not login or _login(comment) != login:
			continue
		for match in _RESUME_RE.finditer(heal.sanitize_text(comment.get("body"))):
			handled.update(key for key in match.group("handled").split(",") if _HANDLED_KEY_RE.match(key))
	return handled


def latest_review_outcome(comments: list, head_sha: str, workflow_login: str) -> dict[str, Any] | None:
	"""The newest workflow-authored review outcome for ``head_sha``.

	Outcomes, newest last: a ``review-autofix-failure:v1`` marker for the head
	(``{"kind": "failure", "reason", "run"}``), a Claude-fixer hand-off for the
	head (``{"kind": "handoff"}``), or an editor summary
	(``{"kind": "summary"}``). Only comments by the workflow account count.
	"""
	login = (workflow_login or "").casefold()
	latest: dict[str, Any] | None = None
	for comment in _ordered(comments):
		if not login or _login(comment) != login:
			continue
		body = heal.sanitize_text(comment.get("body"))
		fields = heal._marker_fields(heal._FAILURE_MARKER_RE.search(body))
		if fields.get("head", "").lower() == head_sha:
			latest = {"kind": "failure", "reason": fields.get("reason") or "unknown", "run": fields.get("run", "")}
			continue
		if any((match := _FIXER_HANDOFF_RE.fullmatch(line)) and match.group(1) == head_sha for line in body.splitlines()):
			latest = {"kind": "handoff"}
			continue
		if _EDITOR_SUMMARY_TEXT in body:
			latest = {"kind": "summary"}
	return latest


def outage_run_ids(comments: list, head_sha: str, workflow_login: str) -> set[str]:
	"""Run ids of the workflow account's `provider_unavailable` failure markers for the head."""
	login = (workflow_login or "").casefold()
	runs: set[str] = set()
	for comment in comments:
		if not isinstance(comment, dict) or not login or _login(comment) != login:
			continue
		fields = heal._marker_fields(heal._FAILURE_MARKER_RE.search(heal.sanitize_text(comment.get("body"))))
		if (fields.get("head", "").lower() == head_sha and fields.get("reason") == heal.PROVIDER_UNAVAILABLE_REASON
			and fields.get("run", "").isdigit()):
			runs.add(fields["run"])
	return runs


def is_resume_candidate(comments: list, head_sha: str, workflow_login: str) -> bool:
	outcome = latest_review_outcome(comments, head_sha, workflow_login)
	return bool(outcome and outcome["kind"] == "failure" and outcome["reason"] == heal.PROVIDER_UNAVAILABLE_REASON)


def latest_head_claim(comments: list, head_sha: str, trusted_logins: tuple[str, ...]) -> dict[str, Any] | None:
	"""The latest trusted `ai:claude-fix-claim` on the head (same trust rules as check_in_status.py)."""
	latest = None
	for comment in _ordered(comments):
		if comment.get("author_association") not in _TRUSTED_ASSOCIATIONS or _login(comment) not in trusted_logins:
			continue
		body = comment.get("body")
		if not isinstance(body, str):
			continue
		markers = [match for line in body.splitlines() if (match := _FIX_CLAIM_RE.fullmatch(line))]
		if len(markers) != 1:
			continue
		claim_head, claim_kind, claim_by = markers[0].groups()
		if claim_head == head_sha:
			latest = {"kind": claim_kind, "by": claim_by, "at": comment.get("created_at")}
	return latest


def hold_is_outage_only(claim: dict[str, Any] | None, *, window_start: dt.datetime, failed_check_run_ids: set[str],
	outage_runs: set[str], conflicted: bool) -> bool:
	"""True when a head's hold has no cause but the outage.

	The latest trusted claim is a hold posted inside the outage window, the PR
	is not conflicted, and every failed check run on the head belongs to a run
	the workflow marked `provider_unavailable`.
	"""
	if not claim or claim.get("kind") != "hold" or conflicted:
		return False
	posted = parse_time(claim.get("at"))
	if posted is None or posted < window_start:
		return False
	return failed_check_run_ids <= outage_runs


def failed_check_run_ids(check_runs: list) -> tuple[set[str], int]:
	"""Workflow run ids of the failed check runs, plus how many failed checks named no run."""
	from_runs: set[str] = set()
	unnamed = 0
	for run in check_runs:
		if not isinstance(run, dict) or run.get("status") != "completed":
			continue
		if run.get("conclusion") not in ("failure", "timed_out", "action_required", "startup_failure"):
			continue
		match = _RUN_ID_IN_URL_RE.search(str(run.get("details_url") or ""))
		if match:
			from_runs.add(match.group(1))
		else:
			unnamed += 1
	return from_runs, unnamed


def label_applied_by_outage(events: list, *, label: str, window_start: dt.datetime, workflow_login: str) -> bool:
	"""True when the label's latest `labeled` event is inside the window and by the workflow account."""
	login = (workflow_login or "").casefold()
	latest = None
	for event in events:
		if not isinstance(event, dict) or event.get("event") != "labeled":
			continue
		if (event.get("label") or {}).get("name") != label:
			continue
		latest = event
	if latest is None or not login:
		return False
	actor = latest.get("actor")
	actor_login = actor.get("login").casefold() if isinstance(actor, dict) and isinstance(actor.get("login"), str) else ""
	labeled_at = parse_time(latest.get("created_at"))
	return actor_login == login and labeled_at is not None and labeled_at >= window_start


def referenced_issues(pr: dict) -> set[int]:
	"""`#N` references in a PR's title and body (same repository)."""
	text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
	return {int(match.group(1)) for match in _ISSUE_REF_RE.finditer(text)}


def render_release_claim(head_sha: str, by: str) -> str:
	return "\n".join([
		f"**Claude fix claim:** `{by}` releases the hold at head `{head_sha[:12]}`. The model provider has "
		"recovered and a fresh review was dispatched; the hold had no other cause (issue #5773). The normal "
		"§26 check-in and catch-all sweep rules apply again once the review finishes.",
		"",
		f"<!-- ai:claude-fix-claim:v1 head={head_sha} kind=review by={by} -->",
	])


def render_resume_comment(summary: dict[str, Any], handled: list[str]) -> str:
	lines = [
		"## Provider recovered: resume",
		"",
		f"The probe succeeded at {summary.get('recovered_at')}. Handled in this tick:",
		"",
		f"- Reviews re-dispatched: {', '.join(summary['dispatched']) or 'none'}",
		f"- `ai:review-blocked` labels removed: {', '.join(summary['labels_removed']) or 'none'}",
		f"- Holds released: {', '.join(summary['holds_released']) or 'none'}",
		f"- Release run: {summary['release']}",
	]
	if summary["errors"]:
		lines.append(f"- Not resumed (read or write failed): {'; '.join(summary['errors'])}")
	lines += ["", f"<!-- {RESUME_MARKER_TAG} handled={','.join(sorted(set(handled)))} -->"]
	return "\n".join(lines)


def render_open_alert(marker: dict[str, Any], source: str) -> str:
	return (
		f"Model provider outage: {marker['provider']} HTTP {marker['status']} ({marker['kind']}) on key {marker['key']}. "
		"Reviews are paused: no PR is labelled ai:review-blocked or capped for it, and no Claude fixer starts. "
		"The review sweep probes every 30 minutes and resumes on its own. "
		f"First seen: {heal.single_line(source, 200)}. Marker: {marker.get('html_url') or '#' + str(marker.get('number'))}"
	)


def render_recovered_alert(marker: dict[str, Any], summary: dict[str, Any]) -> str:
	text = (
		f"Model provider recovered: {marker['provider']} (outage open since {marker.get('created_at')}). "
		f"Re-dispatched {len(summary['dispatched'])} review(s), removed {len(summary['labels_removed'])} outage "
		f"label(s), released {len(summary['holds_released'])} hold(s). Release run: {summary['release']}."
	)
	if summary["errors"]:
		text += f" Not resumed: {len(summary['errors'])} error(s), see the marker."
	return text + f" Marker: {marker.get('html_url') or '#' + str(marker.get('number'))}"


def load_repos(registry_path: Path, self_repo: str) -> list[str]:
	"""This repository first, then every registered consumer, deduplicated."""
	repos = [self_repo] if REPO_RE.fullmatch(self_repo or "") else []
	try:
		data = json.loads(registry_path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		data = []
	if isinstance(data, list):
		repos.extend(slug for slug in data if isinstance(slug, str) and REPO_RE.fullmatch(slug))
	seen: set[str] = set()
	ordered: list[str] = []
	for slug in repos:
		if slug.casefold() not in seen:
			seen.add(slug.casefold())
			ordered.append(slug)
	return ordered


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


class GitHubError(Exception):
	"""A `gh api` call failed."""


class GitHub:
	"""Thin `gh api` client; tests replace it with a fake that has the same methods."""

	def _run(self, args: list[str]) -> str:
		try:
			proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=120)
		except (OSError, subprocess.TimeoutExpired) as exc:
			raise GitHubError(f"gh api {args[-1]} failed: {exc}") from exc
		if proc.returncode != 0:
			detail = (proc.stderr or proc.stdout).strip().splitlines()
			raise GitHubError(f"gh api {' '.join(args[:3])} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
		return proc.stdout

	def get(self, path: str) -> Any:
		output = self._run([path])
		try:
			return json.loads(output) if output.strip() else None
		except ValueError as exc:
			raise GitHubError(f"gh api {path} returned invalid JSON") from exc

	def get_list(self, path: str, list_key: str | None = None) -> list:
		items: list = []
		for page in range(1, MAX_PAGES + 1):
			separator = "&" if "?" in path else "?"
			payload = self.get(f"{path}{separator}per_page=100&page={page}")
			page_items = payload.get(list_key) if list_key and isinstance(payload, dict) else payload
			if not isinstance(page_items, list):
				raise GitHubError(f"gh api {path} returned a non-array page")
			items.extend(page_items)
			if len(page_items) < 100:
				return items
		raise GitHubError(f"gh api {path} exceeded {MAX_PAGES} pages")

	def send(self, method: str, path: str, body: dict | None = None) -> Any:
		args = ["-X", method, path]
		payload_path = ""
		if body is not None:
			with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as payload_file:
				json.dump(body, payload_file)
				payload_path = payload_file.name
			args += ["--input", payload_path]
		try:
			output = self._run(args)
		finally:
			if payload_path:
				Path(payload_path).unlink(missing_ok=True)
		try:
			return json.loads(output) if output.strip() else None
		except ValueError:
			return None


def probe_provider(model: str, api_key: str, *, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
	"""One 1-token completion; only HTTP 200 counts as recovered."""
	if not api_key:
		return {"ok": False, "status": "missing_key"}
	request = urllib.request.Request(
		PROBE_URL,
		data=json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}).encode("utf-8"),
		headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
		method="POST",
	)
	try:
		with opener(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
			status = int(getattr(response, "status", 0) or response.getcode())
	except urllib.error.HTTPError as exc:
		return {"ok": False, "status": str(exc.code)}
	except (urllib.error.URLError, OSError, ValueError) as exc:
		return {"ok": False, "status": "network_error", "detail": heal.single_line(heal.redact_secrets(str(exc)), 200)}
	return {"ok": status == 200, "status": str(status)}


def _open_markers(gh: GitHub, self_repo: str, workflow_login: str | None = None) -> tuple[list[dict], str]:
	issues = gh.get_list(f"repos/{self_repo}/issues?labels={OUTAGE_LABEL}&state=open")
	if not issues:
		return [], workflow_login or ""
	login = workflow_login or ((gh.get("user") or {}).get("login") or "")
	return trusted_open_markers(issues, login), login


def record(gh: GitHub, self_repo: str, *, provider: str, status: str, kind: str, key: str, source: str, now: dt.datetime,
	release_run: dict[str, Any] | None = None, only_if_open: bool = False) -> dict[str, Any]:
	"""Open or extend the one outage marker; see the module docstring."""
	markers, login = _open_markers(gh, self_repo)
	created = None
	if not markers:
		if only_if_open:
			return {"action": "none", "alert": False}
		try:
			gh.send("POST", f"repos/{self_repo}/labels",
				{"name": OUTAGE_LABEL, "color": OUTAGE_LABEL_COLOR, "description": OUTAGE_LABEL_DESCRIPTION})
		except GitHubError:
			pass  # the label already exists
		body = render_marker_body(provider=provider, status=status, kind=kind, key=key, source=source,
			opened_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
		created = gh.send("POST", f"repos/{self_repo}/issues", {
			"title": f"Model provider outage: {heal.safe_token(provider, 40)} HTTP {heal.safe_token(status, 3)} ({heal.safe_token(kind, 20)})",
			"body": body,
			"labels": [OUTAGE_LABEL],
		})
		markers, login = _open_markers(gh, self_repo, login or None)
	created_number = created.get("number") if isinstance(created, dict) else None
	if not markers and type(created_number) is int:
		# The re-list can lag the create; the issue this call opened is the marker.
		fields = parse_marker(created.get("body")) or {"provider": provider, "status": status, "kind": kind, "key": key}
		markers = [{**fields, "number": created_number, "created_at": created.get("created_at"),
			"html_url": created.get("html_url") or ""}]
	if not markers:
		raise GitHubError("no trusted outage marker is open after create")
	primary = markers[0]
	result: dict[str, Any] = {"action": "exists", "issue": primary["number"], "alert": False}
	if created_number is not None:
		if created_number == primary["number"]:
			result.update({"action": "created", "alert": True, "alert_text": render_open_alert(primary, source)})
		else:
			# A concurrent intake opened the marker first; ours is a duplicate.
			gh.send("PATCH", f"repos/{self_repo}/issues/{created_number}", {"state": "closed", "state_reason": "not_planned"})
			result["action"] = "duplicate_closed"
			log(f"duplicate_closed issue=#{created_number} primary=#{primary['number']}")
	if release_run and isinstance(release_run.get("id"), int):
		comments = gh.get_list(f"repos/{self_repo}/issues/{primary['number']}/comments")
		if release_run["id"] in recorded_release_runs(comments, login):
			result["release_run"] = "already_recorded"
		else:
			gh.send("POST", f"repos/{self_repo}/issues/{primary['number']}/comments", {"body": render_release_run_comment(
				release_run["id"], str(release_run.get("workflow") or ""), str(release_run.get("url") or ""))})
			result["release_run"] = "recorded"
	return result


def _dispatch_review(gh: GitHub, repo: str, self_repo: str, pr: dict, allow_workflow_edits: str) -> None:
	is_self = repo.casefold() == self_repo.casefold()
	workflow = SELF_REVIEW_WORKFLOW if is_self else CONSUMER_REVIEW_WORKFLOW
	base_repo = (pr.get("base") or {}).get("repo") or {}
	ref = base_repo.get("default_branch") or "main"
	inputs = {"pr_number": str(pr["number"])}
	if is_self:
		inputs["allow_workflow_edits"] = "true" if allow_workflow_edits == "true" else "false"
	gh.send("POST", f"repos/{repo}/actions/workflows/{workflow}/dispatches", {"ref": ref, "inputs": inputs})


def resume_repo(gh: GitHub, repo: str, *, self_repo: str, login: str, window_start: dt.datetime, handled: set[str],
	summary: dict[str, Any], new_handled: list[str], allow_workflow_edits: str, run_id: str) -> None:
	"""Resume one repository (see the module docstring); appends to `summary` / `new_handled`."""
	prs = gh.get_list(f"repos/{repo}/pulls?state=open")
	referenced: set[int] = set()
	for pr in prs:
		if not isinstance(pr, dict) or pr.get("draft") or type(pr.get("number")) is not int:
			continue
		updated = parse_time(pr.get("updated_at"))
		if updated is None or updated < window_start:
			continue
		head_sha = str((pr.get("head") or {}).get("sha") or "").lower()
		if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
			continue
		number = pr["number"]
		try:
			comments = gh.get_list(f"repos/{repo}/issues/{number}/comments")
			if not is_resume_candidate(comments, head_sha, login):
				continue
			referenced.update(referenced_issues(pr))
			pr_key = f"pr:{repo}#{number}@{head_sha[:12]}"
			if pr_key not in handled:
				_dispatch_review(gh, repo, self_repo, pr, allow_workflow_edits)
				summary["dispatched"].append(f"{repo}#{number}")
				new_handled.append(pr_key)
				log(f"dispatched repo={repo} pr=#{number} head={head_sha[:12]}")
			pr_labels = [label.get("name") for label in pr.get("labels") or [] if isinstance(label, dict)]
			if REVIEW_BLOCKED_LABEL in pr_labels:
				_remove_outage_label(gh, repo, number, login=login, window_start=window_start, handled=handled,
					summary=summary, new_handled=new_handled)
			claim_logins = tuple(trusted_login for trusted_login in (_login(pr), login.casefold()) if trusted_login)
			claim = latest_head_claim(comments, head_sha, claim_logins)
			hold_key = f"hold:{repo}#{number}@{head_sha[:12]}"
			if claim and claim.get("kind") == "hold" and hold_key not in handled:
				pull = gh.get(f"repos/{repo}/pulls/{number}") or {}
				check_runs = gh.get_list(f"repos/{repo}/commits/{head_sha}/check-runs", "check_runs")
				failed_runs, unnamed = failed_check_run_ids(check_runs)
				if unnamed == 0 and hold_is_outage_only(claim, window_start=window_start, failed_check_run_ids=failed_runs,
					outage_runs=outage_run_ids(comments, head_sha, login), conflicted=pull.get("mergeable_state") == "dirty"):
					by = f"{RELEASE_CLAIM_BY_PREFIX}{run_id or 'manual'}"
					gh.send("POST", f"repos/{repo}/issues/{number}/comments", {"body": render_release_claim(head_sha, by)})
					summary["holds_released"].append(f"{repo}#{number}")
					new_handled.append(hold_key)
					log(f"hold_released repo={repo} pr=#{number} head={head_sha[:12]}")
		except GitHubError as exc:
			summary["errors"].append(f"{repo}#{number}: {heal.single_line(exc, 160)}")
			log(f"error repo={repo} pr=#{number} detail={heal.single_line(exc, 160)}")
	if not referenced:
		return
	labelled = gh.get_list(f"repos/{repo}/issues?labels={REVIEW_BLOCKED_LABEL}&state=open")
	for issue in labelled:
		if not isinstance(issue, dict) or "pull_request" in issue or issue.get("number") not in referenced:
			continue
		try:
			_remove_outage_label(gh, repo, issue["number"], login=login, window_start=window_start, handled=handled,
				summary=summary, new_handled=new_handled)
		except GitHubError as exc:
			summary["errors"].append(f"{repo}#{issue['number']}: {heal.single_line(exc, 160)}")


def _remove_outage_label(gh: GitHub, repo: str, number: int, *, login: str, window_start: dt.datetime, handled: set[str],
	summary: dict[str, Any], new_handled: list[str]) -> None:
	label_key = f"label:{repo}#{number}"
	if label_key in handled:
		return
	events = gh.get_list(f"repos/{repo}/issues/{number}/events")
	if not label_applied_by_outage(events, label=REVIEW_BLOCKED_LABEL, window_start=window_start, workflow_login=login):
		return
	try:
		gh.send("DELETE", f"repos/{repo}/issues/{number}/labels/{REVIEW_BLOCKED_LABEL}")
	except GitHubError as exc:
		if "404" not in str(exc):
			raise
	summary["labels_removed"].append(f"{repo}#{number}")
	new_handled.append(label_key)
	log(f"label_removed repo={repo} item=#{number}")


def tick(gh: GitHub, self_repo: str, repos: list[str], now: dt.datetime, *, probe: Callable[[], dict[str, Any]],
	release_rerun_enabled: bool, allow_workflow_edits: str = "false", run_id: str = "") -> dict[str, Any]:
	"""One sweep tick: no marker → nothing; probe failing → skip; probe ok → resume and close."""
	try:
		markers, login = _open_markers(gh, self_repo)
	except GitHubError as exc:
		log(f"marker_read_failed detail={heal.single_line(exc, 200)} (fail open: the sweep runs)")
		return {"outage_open": False, "skip_review_dispatch": False, "error": heal.single_line(exc, 200)}
	if not markers:
		return {"outage_open": False, "skip_review_dispatch": False}
	primary = markers[0]
	result: dict[str, Any] = {"outage_open": True, "skip_review_dispatch": True, "issue": primary["number"]}
	probe_result = probe()
	result["probe"] = probe_result
	if not probe_result.get("ok"):
		log(f"probe status={probe_result.get('status')} issue=#{primary['number']} (still down)")
		return result
	try:
		return _resume(gh, self_repo, repos, now, markers=markers, login=login, result=result,
			release_rerun_enabled=release_rerun_enabled, allow_workflow_edits=allow_workflow_edits, run_id=run_id)
	except GitHubError as exc:
		# The marker stays open, so the next tick probes and resumes again;
		# the handled keys already recorded keep that idempotent.
		log(f"resume_failed issue=#{primary['number']} detail={heal.single_line(exc, 200)}")
		result["error"] = heal.single_line(exc, 200)
		return result


def _resume(gh: GitHub, self_repo: str, repos: list[str], now: dt.datetime, *, markers: list[dict], login: str,
	result: dict[str, Any], release_rerun_enabled: bool, allow_workflow_edits: str, run_id: str) -> dict[str, Any]:
	"""The recovery half of `tick`: resume every repository, record, close, alert."""
	primary = markers[0]
	window_start = parse_time(primary.get("created_at")) or now
	marker_comments = gh.get_list(f"repos/{self_repo}/issues/{primary['number']}/comments")
	handled = handled_keys(marker_comments, login)
	summary: dict[str, Any] = {"recovered_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "dispatched": [],
		"labels_removed": [], "holds_released": [], "release": "none recorded", "errors": []}
	new_handled: list[str] = []
	for repo in repos:
		try:
			resume_repo(gh, repo, self_repo=self_repo, login=login, window_start=window_start, handled=handled,
				summary=summary, new_handled=new_handled, allow_workflow_edits=allow_workflow_edits, run_id=run_id)
		except GitHubError as exc:
			summary["errors"].append(f"{repo}: {heal.single_line(exc, 160)}")
			log(f"error repo={repo} detail={heal.single_line(exc, 160)}")
	release_runs = recorded_release_runs(marker_comments, login)
	if release_runs:
		newest = release_runs[-1]
		release_key = f"release:{newest}"
		if release_key in handled:
			summary["release"] = f"run {newest} already handled"
		elif release_rerun_enabled:
			try:
				gh.send("POST", f"repos/{self_repo}/actions/runs/{newest}/rerun-failed-jobs")
				summary["release"] = f"run {newest} re-run"
				new_handled.append(release_key)
			except GitHubError as exc:
				summary["release"] = f"run {newest} re-run failed: {heal.single_line(exc, 120)}"
		else:
			summary["release"] = f"run {newest} not re-run (PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED is off)"
			new_handled.append(release_key)
	gh.send("POST", f"repos/{self_repo}/issues/{primary['number']}/comments",
		{"body": render_resume_comment(summary, sorted(handled) + new_handled)})
	for marker in markers:
		gh.send("PATCH", f"repos/{self_repo}/issues/{marker['number']}", {"state": "closed", "state_reason": "completed"})
	result.update({"recovered": True, "summary": summary, "alert_text": render_recovered_alert(primary, summary),
		"skip_review_dispatch": True})
	log(f"recovered issue=#{primary['number']} dispatched={len(summary['dispatched'])} "
		f"labels_removed={len(summary['labels_removed'])} holds_released={len(summary['holds_released'])} "
		f"errors={len(summary['errors'])}")
	return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
	return dt.datetime.now(dt.timezone.utc)


def _record_fields(args: argparse.Namespace) -> dict[str, str] | None:
	"""Outage fields from --outage-line, --evidence-file, or the explicit flags."""
	if args.outage_line:
		return heal.parse_provider_outage_line("provider_outage " + args.outage_line)
	if args.evidence_file:
		try:
			text = Path(args.evidence_file).read_text(encoding="utf-8", errors="replace")
		except OSError:
			return None
		return heal.parse_provider_outage_line(text)
	if args.status and args.kind:
		return {"provider": args.provider, "status": args.status, "kind": args.kind, "key": args.key}
	return None


def _cmd_record(args: argparse.Namespace) -> int:
	release_run = None
	if args.release_run_id:
		if not re.fullmatch(r"[1-9][0-9]{0,19}", args.release_run_id):
			raise ValueError(f"--release-run-id must be a run id, got {args.release_run_id!r}")
		release_run = {"id": int(args.release_run_id), "workflow": args.release_workflow, "url": args.release_run_url}
	fields = _record_fields(args)
	if fields is None and not args.only_if_open:
		raise ValueError("no provider outage fields (pass --outage-line, --evidence-file, or --status and --kind)")
	fields = fields or {"provider": args.provider, "status": "", "kind": "", "key": args.key}
	result = record(GitHub(), args.repo, provider=fields["provider"], status=fields["status"], kind=fields["kind"],
		key=fields["key"], source=args.source, now=_now(), release_run=release_run, only_if_open=args.only_if_open)
	print(json.dumps(result))
	return 0


def _cmd_tick(args: argparse.Namespace) -> int:
	repos = load_repos(Path(args.registry), args.repo)
	model = args.probe_model or DEFAULT_PROBE_MODEL
	api_key = os.environ.get("OPENROUTER_API_KEY", "")
	result = tick(GitHub(), args.repo, repos, _now(), probe=lambda: probe_provider(model, api_key),
		release_rerun_enabled=args.release_rerun_enabled == "true", allow_workflow_edits=args.allow_workflow_edits,
		run_id=args.run_id)
	print(json.dumps(result))
	return 0


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)

	p = sub.add_parser("record", help="Open or extend the one outage marker; prints JSON with `alert`")
	p.add_argument("--repo", required=True, help="OWNER/REPO that holds the marker (coding-workflows)")
	p.add_argument("--outage-line", default="", help="`provider=… status=… kind=… key=…` (workflow_failure_heal.py output)")
	p.add_argument("--evidence-file", default="", help="file holding a `provider_outage provider=… …` line (the reporter's evidence)")
	p.add_argument("--provider", default=heal.PROVIDER_OUTAGE_DEFAULT_PROVIDER)
	p.add_argument("--status", default="")
	p.add_argument("--kind", default="", choices=("",) + heal.PROVIDER_OUTAGE_KINDS)
	p.add_argument("--key", default="OPENROUTER_API_KEY")
	p.add_argument("--source", required=True, help="what first saw the outage (repo#pr run N, or a release run)")
	p.add_argument("--release-run-id", default="")
	p.add_argument("--release-workflow", default="")
	p.add_argument("--release-run-url", default="")
	p.add_argument("--only-if-open", action="store_true", help="record a release run on an open marker; never open one")
	p.set_defaults(func=_cmd_record)

	p = sub.add_parser("tick", help="One sweep tick: probe while a marker is open, resume and close on recovery")
	p.add_argument("--repo", required=True)
	p.add_argument("--registry", default=str(_SCRIPTS.parent / ".github" / "ai" / "consumer_repos.json"))
	p.add_argument("--probe-model", default="")
	p.add_argument("--release-rerun-enabled", default="false")
	p.add_argument("--allow-workflow-edits", default="false")
	p.add_argument("--run-id", default="")
	p.set_defaults(func=_cmd_tick)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	if not REPO_RE.fullmatch(args.repo or ""):
		print(json.dumps({"error": f"--repo must be OWNER/REPO, got {args.repo!r}"}))
		return 2
	try:
		return args.func(args)
	except (GitHubError, ValueError) as exc:
		print(json.dumps({"error": heal.single_line(exc, 300)}))
		return 2


if __name__ == "__main__":
	sys.exit(main())

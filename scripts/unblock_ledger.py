#!/usr/bin/env python3
"""Never-repeat ledger and hard limits for the unblock judge.

The unblock judge (docs/plans/replace-claude-sessions-with-cli-engine-plan.md,
Phase 7) takes an issue, PR or project that a pipeline stop left blocked and
picks one verdict from a fixed menu. This script is its memory and its
guard rail; the judge decides nothing that this script refuses. It is the
port of `escalation_ledger.py` from PR #5164: the ledger moved from a
session progress log to the pipeline's own comments, so it works in Actions.

Ledger. Every verdict the judge acts on is posted as a comment whose last
non-empty line is its marker:

  <!-- ai:unblock:v1 item=<n> stop=<id> fingerprint=<12 hex> verdict=<v> round=<k> -->

(`override=bulk_delete` follows `round=` on an `override_guard` verdict for
the destructive latch; implement.yml honours it once, for that issue's next
run.)

on the blocked item, and, for an item of an orchestrator project, also on
the project's tracking issue. Only comments by the trusted pipeline login
count, so a marker quoted or forged by anyone else is ignored.

Rules enforced here:
  - a verdict already recorded for the same stop id and fingerprint is never
    offered again, on this item or anywhere in the same project;
  - at most MAX_ROUNDS_PER_ITEM rounds per item and MAX_ROUNDS_PER_PROJECT
    per project; past a cap, or BLOCKED_AFTER_LAST_ROUND_HOURS after the last
    round with the item still blocked, the only outcome is the terminal close;
  - `override_guard` exists only for the scope and destructive latches on an
    issue, and its paths never include `.github/`, `.claude/` or
    `workflow-templates/` (case-insensitive) in any repository, or `scripts/`
    in shubhodeep1/coding-workflows; for the destructive latch no path may be
    a canonical workflow source (CANONICAL_SOURCE_RE, the list
    scripts/implement_commit_changes.sh guards);
  - `auto_answer` and `override_guard` are offered only for an issue, never for
    a pull request or a project;
  - `reissue` is never offered for a whole project;
  - `accept_with_followup` is never offered for a failed security pass, a
    failed validation, or an `ai:security` issue (`decide --security-issue`)
    (no waiver, no validation pass).

Subcommands (one JSON line on stdout; exit 0 ok, 1 bad arguments or a refused
verdict, 2 unreadable input):

  labels
      The block labels the scan searches for, in a fixed order.
  stop --labels-json <json> [--project-failed]
      The stop id for an item from its labels (the first block label in
      `labels` order wins); `project-failed` for a failed project.
  fingerprint --stop <id> --evidence-file <path>
      12 hex of SHA-1 over the stop id and the normalised evidence.
  decide --item <n> --stop <id> --fingerprint <fp> --comments-file <path>
         [--project-comments-file <path>] --trusted-login <login>
         --now <iso8601> [--kind issue|pr|project] [--last-activity <iso8601>]
         [--security-issue]
      Rounds used, the verdicts still allowed, and whether the item is
      terminal (and why).
  validate --verdict-file <path> --decision-file <path> --repo <owner/repo>
      Check a judge verdict against the `decide` output and the hard limits,
      and print the normalised verdict.
  rejection --item <n> --stop <id> --comments-file <path> --trusted-login <login>
      Find the latest trusted, unused guard rejection on the blocked item.
  marker --item <n> --stop <id> --fingerprint <fp> --verdict <v> --round <k>
         [--override bulk_delete]
      The marker line to end the verdict comment with.

No GitHub API calls and no network (CLAUDE.md §15): callers pass the
comments they already fetched.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

# Labels a pipeline stop leaves on an item that only a human used to clear.
# ai:review-blocked is not here: the review-blocked judge owns it and hands
# over by adding ai:blocked when it gives up. ai:implementation-failed is
# re-issued by the poller on its own. The two guard latches come first, so an
# item that also carries ai:needs-human keeps `override_guard` on its menu.
BLOCK_LABELS = (
	"ai:scope-blocked",
	"ai:destructive-blocked",
	"ai:blocked",
	"ai:needs-human",
	"ai:harness-broken",
	"ai:security-pass-failed",
	"ai:validation-failed",
	"ai:resolver-escalated",
	"ai:check-triage-escalated",
	"ai:workflow-heal-escalated",
	"ai:clarify-failed",
	"ai:clarify-respond-failed",
	"ai:plan-failed",
	"ai:implement-diagnose-failed",
	"ai:review-autofix-failed",
	"ai:validate-failed",
	"ai:integration-judge-failed",
	"ai:log-analysis-failed",
	"ai:memory-maintenance-failed",
)
PROJECT_FAILED_STOP = "project-failed"
STOP_IDS = tuple(label[len("ai:"):] for label in BLOCK_LABELS) + (PROJECT_FAILED_STOP,)

VERDICTS = (
	"retry_budget",
	"auto_answer",
	"descope",
	"override_guard",
	"reissue",
	"accept_with_followup",
	"operator_step",
	"close",
)
TERMINAL_VERDICT = "close"
GUARD_STOPS = ("scope-blocked", "destructive-blocked")
GUARD_FOR_STOP = {"scope-blocked": "scope", "destructive-blocked": "destructive"}
OVERRIDABLE_DESTRUCTIVE_REASONS = ("bulk-delete",)
ITEM_KINDS = ("issue", "pr", "project")
ISSUE_ONLY_VERDICTS = ("auto_answer", "override_guard")
NOT_FOR_PROJECT_VERDICTS = ("reissue",)
OVERRIDES = ("bulk_delete",)
# The canonical workflow sources scripts/implement_commit_changes.sh refuses to
# delete without ALLOW_WORKFLOW_EDITS; kept in step with its grep pattern.
CANONICAL_SOURCE_RE = re.compile(
	r"^(agents\.md|ai_pipeline\.md|unattended_system_instructions\.md|CLAUDE\.md|prompts/|scripts/|\.github/ai/|\.github/scripts/)"
)
# Every override_guard verdict uses this denylist (including case-insensitive
# matches). Both implement deletion guards keep their protected-path grep in step.
PROTECTED_AUTOMATION_OVERRIDE_RE = re.compile(r"^(\.github|\.claude|workflow-templates)(/|$)", re.IGNORECASE)
NO_WAIVER_STOPS = ("security-pass-failed", "validation-failed", "validate-failed", "harness-broken")

MAX_ROUNDS_PER_ITEM = 2
MAX_ROUNDS_PER_PROJECT = 6
BLOCKED_AFTER_LAST_ROUND_HOURS = 24

SOURCE_REPO = "shubhodeep1/coding-workflows"
FORBIDDEN_OVERRIDE_PREFIXES = (".github/workflows/", ".claude/", "scripts/")
MAX_OVERRIDE_PATHS = 20
MAX_TEXT = 1200
OPERATOR_PLACEHOLDER_SUFFIX = "_UNSET_OPERATOR_STEP"
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
REJECTION_MAX_BYTES = 1024 * 1024
IMPLEMENT_WORKFLOW_PATHS = {
	".github/workflows/implement.yml",
	".github/workflows/internal-implement.yml",
	".github/workflows/ai-implement.yml",
}

EVIDENCE_LIST_KEYS = ("checks", "findings", "issues", "paths")
EVIDENCE_SCALAR_KEYS = ("reason", "validation_class", "validation_status", "pr")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{12}$")
MARKER_RE = re.compile(
	r"^<!-- ai:unblock:v1 item=(?P<item>[1-9][0-9]*) stop=(?P<stop>[a-z-]+) "
	r"fingerprint=(?P<fp>[0-9a-f]{12}) verdict=(?P<verdict>[a-z_]+) round=(?P<round>[1-9][0-9]*)"
	r"(?: override=(?P<override>[a-z_]+))? -->$"
)
REJECTION_RE = re.compile(
	r"^<!-- ai:guard-rejection:v1 item=(?P<item>[1-9][0-9]*) guard=(?P<guard>scope|scope-lock|destructive) "
	r"reason=(?P<reason>[a-z-]+) run=(?P<run>[0-9]+) count=(?P<count>[0-9]+) "
	r"truncated=(?P<truncated>true|false) paths=(?P<paths>[A-Za-z0-9+/=]+) -->$"
)
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$")


class UsageError(Exception):
	"""Bad arguments or a refused verdict: exit 1."""


class InputError(Exception):
	"""An unreadable input file: exit 2."""


class _Parser(argparse.ArgumentParser):
	def error(self, message: str) -> None:
		raise UsageError(message)


def _read_json(path: str, flag: str) -> object:
	try:
		return json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise InputError(f"cannot read {flag} {path}: {exc}") from exc


def _check_stop(stop: str) -> str:
	if stop not in STOP_IDS:
		raise UsageError(f"unknown stop id {stop!r}; expected one of {list(STOP_IDS)}")
	return stop


def _check_fingerprint(value: str) -> str:
	if not FINGERPRINT_RE.match(value):
		raise UsageError(f"fingerprint must be 12 lower-case hex characters, got {value!r}")
	return value


def _check_item(value: str) -> int:
	if not re.fullmatch(r"[1-9][0-9]*", value or ""):
		raise UsageError(f"item must be an issue or PR number, got {value!r}")
	return int(value)


def _text(value: object) -> str:
	if isinstance(value, bool) or not isinstance(value, (str, int)):
		raise UsageError(f"evidence values must be strings or integers, got {value!r}")
	return " ".join(str(value).split()).lower()


def normalise_evidence(evidence: object) -> dict:
	"""Canonical evidence: trimmed, lower-cased, de-duplicated and sorted."""
	if not isinstance(evidence, dict):
		raise UsageError("evidence must be a JSON object")
	unknown = sorted(set(evidence) - set(EVIDENCE_LIST_KEYS + EVIDENCE_SCALAR_KEYS))
	if unknown:
		raise UsageError(f"unknown evidence keys: {unknown}")
	canonical: dict = {}
	for key in EVIDENCE_LIST_KEYS:
		values = evidence.get(key)
		if values is None:
			continue
		if not isinstance(values, list):
			raise UsageError(f"evidence key {key!r} must be a list")
		items = sorted({_text(item) for item in values} - {""})
		if items:
			canonical[key] = items
	for key in EVIDENCE_SCALAR_KEYS:
		if evidence.get(key) is None:
			continue
		value = _text(evidence[key]).lstrip("#") if key == "pr" else _text(evidence[key])
		if value:
			canonical[key] = value
	return canonical


def fingerprint(stop: str, evidence: object) -> str:
	payload = {"stop": _check_stop(stop), "evidence": normalise_evidence(evidence)}
	canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
	return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def stop_for_labels(labels: object, project_failed: bool) -> str:
	if project_failed:
		return PROJECT_FAILED_STOP
	if not isinstance(labels, list):
		raise UsageError("--labels-json must be a JSON array")
	names = {item.get("name") if isinstance(item, dict) else item for item in labels}
	for label in BLOCK_LABELS:
		if label in names:
			return label[len("ai:"):]
	raise UsageError("no block label on this item")


def _parse_time(value: str) -> dt.datetime:
	try:
		parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
	except (AttributeError, ValueError) as exc:
		raise UsageError(f"not an ISO 8601 time: {value!r}") from exc
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=dt.timezone.utc)
	return parsed


def parse_markers(comments: object, trusted_login: str) -> list[dict]:
	"""Markers from comments by the trusted login; the marker must be the last non-empty line."""
	if not isinstance(comments, list):
		raise InputError("comments must be a JSON array")
	entries = []
	for comment in comments:
		if not isinstance(comment, dict):
			continue
		user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
		login = user.get("login") or comment.get("author_login") or ""
		if login != trusted_login:
			continue
		lines = [line.strip() for line in str(comment.get("body") or "").splitlines() if line.strip()]
		if not lines:
			continue
		match = MARKER_RE.match(lines[-1])
		if not match or match.group("stop") not in STOP_IDS or match.group("verdict") not in VERDICTS:
			continue
		created = comment.get("created_at") or ""
		try:
			created_at = _parse_time(created) if created else None
		except UsageError:
			created_at = None
		entries.append(
			{
				"item": int(match.group("item")),
				"stop": match.group("stop"),
				"fingerprint": match.group("fp"),
				"verdict": match.group("verdict"),
				"round": int(match.group("round")),
				"override": match.group("override") or "",
				"created_at": created_at.isoformat() if created_at else "",
			}
		)
	return entries


def latest_rejection(comments: object, trusted_login: str, item: int, stop: str) -> dict:
	"""Select the newest trusted guard rejection after the last item verdict."""
	if not isinstance(comments, list):
		raise InputError("comments must be a JSON array")
	newest_rejection = None
	newest_verdict_index = -1
	untrusted = False
	for index, comment in enumerate(comments):
		if not isinstance(comment, dict):
			continue
		lines = [line.strip() for line in str(comment.get("body") or "").splitlines() if line.strip()]
		if not lines:
			continue
		line = lines[-1]
		match = REJECTION_RE.fullmatch(line)
		rejection_item = re.match(r"^<!-- ai:guard-rejection:v1 item=([1-9][0-9]*)\b", line)
		user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
		login = user.get("login") or comment.get("author_login") or ""
		if login != trusted_login:
			if rejection_item and int(rejection_item.group(1)) == item:
				untrusted = True
			continue
		if any(entry["item"] == item for entry in parse_markers([comment], trusted_login)):
			newest_verdict_index = index
		if rejection_item and int(rejection_item.group(1)) == item:
			newest_rejection = (index, match, comment)
		elif lines[0] in (
			"🚨 **files_touched scope guard rejected this implementation run.**",
			"🚨 **Issue scope-lock rejected this implementation run.**",
			"🚨 **Destructive-commit guard rejected this implementation run.**",
		):
			# A later handler that could not encode its marker must not leave
			# an older, otherwise-valid rejection available for override.
			newest_rejection = (index, None, comment)
	if newest_rejection is None:
		return {"status": "none", "reason": "untrusted" if untrusted else "missing"}
	index, match, comment = newest_rejection
	if index <= newest_verdict_index:
		return {"status": "none", "reason": "stale"}
	if match is None:
		return {"status": "none", "reason": "malformed"}
	if match.group("guard") != GUARD_FOR_STOP.get(stop) or (
		stop == "scope-blocked" and match.group("reason") != "out-of-scope"
	):
		return {"status": "none", "reason": "guard_mismatch"}
	if stop == "destructive-blocked" and match.group("reason") not in OVERRIDABLE_DESTRUCTIVE_REASONS:
		return {"status": "none", "reason": "reason_not_overridable"}
	if match.group("truncated") == "true":
		return {"status": "none", "reason": "truncated"}
	try:
		encoded = match.group("paths")
		if len(encoded) > 512000:
			raise ValueError("oversized rejection")
		paths = json.loads(base64.b64decode(encoded, validate=True))
		count = int(match.group("count"))
		if not isinstance(paths, list) or not 0 < count < 100 or len(paths) != count:
			raise ValueError("invalid rejection count")
		if not all(isinstance(path, str) for path in paths):
			raise ValueError("invalid rejection path")
		cleaned = [_clean_path(path) for path in paths]
		if len(set(cleaned)) != len(cleaned):
			raise ValueError("duplicate rejection path")
	except (ValueError, UnicodeDecodeError, binascii.Error, UsageError):
		return {"status": "none", "reason": "malformed"}
	return {
		"status": "ok", "guard": match.group("guard"), "reason": match.group("reason"),
		"run": match.group("run"), "paths": cleaned, "comment_id": comment.get("id"),
		"created_at": comment.get("created_at", ""),
	}


def decide(
	item: int,
	stop: str,
	fp: str,
	item_entries: list[dict],
	project_entries: list[dict] | None,
	now: dt.datetime,
	kind: str = "issue",
	last_activity: dt.datetime | None = None,
	rejection: dict | None = None,
	*,
	security_issue: bool = False,
) -> dict:
	"""What the judge may still do for this item."""
	if kind not in ITEM_KINDS:
		raise UsageError(f"unknown item kind {kind!r}; expected one of {list(ITEM_KINDS)}")
	own = [entry for entry in item_entries if entry["item"] == item]
	# A marker posted on both the item and the tracking issue counts once.
	seen = set()
	pool = []
	for entry in own + list(project_entries or []):
		key = (entry["item"], entry["round"], entry["fingerprint"], entry["verdict"])
		if key in seen:
			continue
		seen.add(key)
		pool.append(entry)
	item_rounds = len({entry["round"] for entry in pool if entry["item"] == item})
	project_rounds = len(
		{(entry["item"], entry["round"]) for entry in pool}
	) if project_entries is not None else None
	used = sorted({entry["verdict"] for entry in pool if entry["stop"] == stop and entry["fingerprint"] == fp})
	allowed = [
		verdict
		for verdict in VERDICTS
		if verdict == TERMINAL_VERDICT or verdict not in used
	]
	if stop not in GUARD_STOPS or not isinstance(rejection, dict) or rejection.get("status") != "ok" \
		or rejection.get("guard") != GUARD_FOR_STOP.get(stop) or not isinstance(rejection.get("paths"), list) \
		or not rejection["paths"] or (stop == "scope-blocked" and len(rejection["paths"]) > MAX_OVERRIDE_PATHS) \
		or (stop == "scope-blocked" and rejection.get("reason") != "out-of-scope") \
		or (stop == "destructive-blocked" and rejection.get("reason") not in OVERRIDABLE_DESTRUCTIVE_REASONS):
		allowed = [verdict for verdict in allowed if verdict != "override_guard"]
	if stop in NO_WAIVER_STOPS:
		allowed = [verdict for verdict in allowed if verdict != "accept_with_followup"]
	# An ai:security issue is a security finding: it is never waived with a
	# follow-up, whatever stop it is blocked under (#6541). A split goes
	# through `reissue`, which carries the finding marker and label.
	if kind == "issue" and security_issue:
		allowed = [verdict for verdict in allowed if verdict != "accept_with_followup"]
	if kind != "issue":
		allowed = [verdict for verdict in allowed if verdict not in ISSUE_ONLY_VERDICTS]
	if kind == "project":
		allowed = [verdict for verdict in allowed if verdict not in NOT_FOR_PROJECT_VERDICTS]
	last_times = [_parse_time(entry["created_at"]) for entry in pool if entry["item"] == item and entry["created_at"]]
	# A fix-up's follow-up (the reset posted after it merged) restarts the
	# 24-hour clock, so the reset gets its chance before the terminal close.
	if last_times and last_activity is not None:
		last_times.append(last_activity)
	terminal_reason = ""
	if item_rounds >= MAX_ROUNDS_PER_ITEM:
		terminal_reason = "item_cap"
	elif project_rounds is not None and project_rounds >= MAX_ROUNDS_PER_PROJECT:
		terminal_reason = "project_cap"
	elif last_times and now - max(last_times) >= dt.timedelta(hours=BLOCKED_AFTER_LAST_ROUND_HOURS):
		terminal_reason = "still_blocked_24h"
	elif allowed == [TERMINAL_VERDICT]:
		terminal_reason = "menu_exhausted"
	return {
		"item": item,
		"kind": kind,
		"stop": stop,
		"fingerprint": fp,
		"item_rounds": item_rounds,
		"project_rounds": project_rounds,
		"next_round": item_rounds + 1,
		"used": used,
		"allowed": [TERMINAL_VERDICT] if terminal_reason else allowed,
		"terminal": bool(terminal_reason),
		"terminal_reason": terminal_reason,
	}


def _clean_text(value: object, field: str, required: bool) -> str:
	if value is None:
		value = ""
	if not isinstance(value, str):
		raise UsageError(f"verdict field {field!r} must be a string")
	text = " ".join(value.split())
	if required and not text:
		raise UsageError(f"verdict field {field!r} must not be empty")
	if "<!--" in text or "-->" in text:
		raise UsageError(f"verdict field {field!r} must not contain an HTML comment delimiter")
	return text[:MAX_TEXT]


def _clean_path(value: object) -> str:
	if not isinstance(value, str):
		raise UsageError("override paths must be strings")
	path = value.strip()
	while path.startswith("./"):
		path = path[2:]
	if (
		not path
		or path.startswith("/")
		or "\\" in path
		or any(part in ("", ".", "..") for part in path.rstrip("/").split("/"))
		or any(ch in path for ch in "*?[]{}")
		or any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in path)
	):
		raise UsageError(f"override path {value!r} must be a plain relative path without globs or '..'")
	return path


def rejection_snapshot(run: object, artifact: object, zip_path: str, item: int, repo: str, approved: object = None) -> dict:
	"""Verify an Actions artifact against the failed implement run, not comment text."""
	if not isinstance(run, dict) or not isinstance(artifact, dict):
		raise UsageError("rejection run and artifact must be objects")
	run_id = run.get("id")
	if type(run_id) is not int or run_id < 1 or any(
		(not isinstance(run.get(key), dict) or run[key].get("full_name") != repo)
		for key in ("repository", "head_repository")
	) or not isinstance(run.get("path"), str) or run["path"] not in IMPLEMENT_WORKFLOW_PATHS \
		or run.get("status") != "completed" or run.get("conclusion") != "failure":
		raise UsageError("rejection run is not a failed same-repository implement run")
	artifact_id = artifact.get("id")
	artifact_size = artifact.get("size_in_bytes")
	if (
		type(artifact_id) is not int or artifact_id < 1
		or artifact.get("name") != f"destructive-rejection-issue-{item}"
		or artifact.get("expired") is not False
		or not isinstance(artifact.get("workflow_run"), dict)
		or type(artifact["workflow_run"].get("id")) is not int
		or artifact["workflow_run"].get("id") != run_id
		or type(artifact_size) is not int or not 0 < artifact_size <= REJECTION_MAX_BYTES
	):
		raise UsageError("rejection artifact provenance or size is invalid")
	try:
		if Path(zip_path).stat().st_size > REJECTION_MAX_BYTES:
			raise UsageError("rejection zip exceeds size limit")
		with zipfile.ZipFile(zip_path) as archive:
			entries = archive.infolist()
			if len(entries) != 1 or entries[0].filename != "destructive_rejection.json" or entries[0].file_size > REJECTION_MAX_BYTES:
				raise UsageError("rejection zip must contain exactly one bounded JSON file")
			with archive.open(entries[0]) as stream:
				data = stream.read(REJECTION_MAX_BYTES + 1)
			if len(data) > REJECTION_MAX_BYTES:
				raise UsageError("rejection JSON exceeds size limit")
			snapshot = json.loads(data)
	except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
		raise UsageError("rejection zip is unreadable") from exc
	if not isinstance(snapshot, dict) or (
		snapshot.get("schema") != "destructive_rejection.v1"
		or type(snapshot.get("issue")) is not int or snapshot["issue"] != item
		or type(snapshot.get("run_id")) is not int or snapshot["run_id"] != run_id
		or type(snapshot.get("run_attempt")) is not int or snapshot["run_attempt"] < 1
		or snapshot["run_attempt"] != run.get("run_attempt")
		or snapshot.get("reason") != "bulk-delete"
	):
		raise UsageError("rejection snapshot does not match the issue and run")
	paths = snapshot.get("paths")
	if not isinstance(paths, list) or not paths or any(not isinstance(path, str) or not path for path in paths):
		raise UsageError("rejection paths must be a non-empty list of strings")
	if approved is not None:
		if not isinstance(approved, list) or not approved or any(not isinstance(path, str) for path in approved):
			raise UsageError("approved deletions must be a non-empty list of strings")
		if not set(approved) <= set(paths):
			raise UsageError("approved_paths_not_rejected")
	return {"run_id": run_id, "reason": "bulk-delete", "paths": paths}


def validate(verdict: object, decision: object, repo: str, rejection: dict | None = None, rejected: object = None) -> dict:
	"""Refuse anything outside the menu or the hard limits; return the normalised verdict."""
	if not isinstance(verdict, dict) or not isinstance(decision, dict):
		raise UsageError("verdict and decision must be JSON objects")
	name = verdict.get("verdict")
	allowed = decision.get("allowed") or []
	if name not in VERDICTS:
		raise UsageError(f"unknown verdict {name!r}")
	if name not in allowed:
		raise UsageError(f"verdict {name!r} is not allowed here; allowed: {allowed}")
	normalised = {
		"verdict": name,
		"reason": _clean_text(verdict.get("reason"), "reason", True),
		"item": decision.get("item"),
		"stop": decision.get("stop"),
		"fingerprint": decision.get("fingerprint"),
		"round": decision.get("next_round"),
	}
	if name in ("retry_budget", "descope", "reissue", "accept_with_followup"):
		normalised["instructions"] = _clean_text(verdict.get("instructions"), "instructions", True)
	if name == "auto_answer":
		normalised["answer"] = _clean_text(verdict.get("answer"), "answer", True)
	if name == "override_guard":
		paths = verdict.get("paths")
		if not isinstance(paths, list) or not paths:
			raise UsageError("override_guard needs a non-empty 'paths' list")
		if len(paths) > MAX_OVERRIDE_PATHS:
			raise UsageError(f"override_guard allows at most {MAX_OVERRIDE_PATHS} paths")
		cleaned = sorted({_clean_path(path) for path in paths})
		for path in cleaned:
			if PROTECTED_AUTOMATION_OVERRIDE_RE.match(path):
				raise UsageError(f"override_guard never covers the protected automation path {path!r}")
		if repo.lower() == SOURCE_REPO:
			for path in cleaned:
				if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in FORBIDDEN_OVERRIDE_PREFIXES):
					raise UsageError(f"override_guard never covers {path!r} in {SOURCE_REPO}")
		if decision.get("stop") == "destructive-blocked":
			for path in cleaned:
				if CANONICAL_SOURCE_RE.match(path):
					raise UsageError(f"override_guard never allows deleting the canonical workflow source {path!r}")
			if not isinstance(rejected, dict) or rejected.get("reason") != "bulk-delete" \
				or type(rejected.get("run_id")) is not int or rejected["run_id"] < 1 \
				or not isinstance(rejected.get("paths"), list) or not rejected["paths"]:
				raise UsageError("override_guard on destructive-blocked needs a verified rejected-deletion snapshot")
			for path in cleaned:
				if path not in rejected["paths"]:
					raise UsageError(f"approved path was not rejected: {path!r}")
			normalised["override"] = "bulk_delete"
		if not isinstance(rejection, dict) or rejection.get("status") != "ok" \
			or rejection.get("guard") != GUARD_FOR_STOP.get(decision.get("stop")) \
			or (decision.get("stop") == "scope-blocked" and rejection.get("reason") != "out-of-scope") \
			or (decision.get("stop") == "destructive-blocked" and rejection.get("reason") not in OVERRIDABLE_DESTRUCTIVE_REASONS) \
			or not isinstance(rejection.get("paths"), list) or not isinstance(rejection.get("run"), str) \
			or not re.fullmatch(r"[0-9]+", rejection["run"]):
			raise UsageError("override paths must equal the guard-rejected paths; missing: [], additional: [] (no bound rejection)")
		try:
			rejected_paths = {_clean_path(path) for path in rejection["paths"]}
		except UsageError as exc:
			raise UsageError("invalid guard-rejected paths") from exc
		if decision.get("stop") == "destructive-blocked" and not set(cleaned) <= rejected_paths:
			raise UsageError(f"override paths must be within guard-rejected paths; additional: {sorted(set(cleaned) - rejected_paths)}")
		if decision.get("stop") != "destructive-blocked" and set(cleaned) != rejected_paths:
			raise UsageError(
				f"override paths must equal the guard-rejected paths; missing: {sorted(rejected_paths - set(cleaned))}, "
				f"additional: {sorted(set(cleaned) - rejected_paths)}"
			)
		if decision.get("stop") == "destructive-blocked":
			if rejected["run_id"] != int(rejection["run"]):
				raise UsageError("rejected-deletion snapshot does not match the guard rejection run")
			normalised["rejected_run"] = rejected["run_id"]
		normalised["rejection_run"] = rejection["run"]
		normalised["paths"] = cleaned
	if name == "operator_step":
		flag = verdict.get("placeholder")
		if not isinstance(flag, str) or not ENV_NAME_RE.match(flag):
			raise UsageError("operator_step needs 'placeholder': an upper-case flag or env var name")
		if not flag.endswith(OPERATOR_PLACEHOLDER_SUFFIX) and not flag.endswith("_ENABLED"):
			raise UsageError(
				f"operator_step 'placeholder' must be a feature flag ending in _ENABLED (default off) "
				f"or a placeholder ending in {OPERATOR_PLACEHOLDER_SUFFIX}"
			)
		normalised["placeholder"] = flag
		normalised["operator_instructions"] = _clean_text(verdict.get("operator_instructions"), "operator_instructions", True)
		normalised["instructions"] = _clean_text(verdict.get("instructions"), "instructions", True)
	return normalised


def marker(item: int, stop: str, fp: str, verdict: str, round_number: int, override: str = "") -> str:
	if verdict not in VERDICTS:
		raise UsageError(f"unknown verdict {verdict!r}")
	if round_number < 1:
		raise UsageError("round must be at least 1")
	suffix = ""
	if override:
		if override not in OVERRIDES or verdict != "override_guard" or stop != "destructive-blocked":
			raise UsageError(f"override {override!r} is only for an override_guard verdict on destructive-blocked")
		suffix = f" override={override}"
	return f"<!-- ai:unblock:v1 item={item} stop={stop} fingerprint={fp} verdict={verdict} round={round_number}{suffix} -->"


def build_parser() -> argparse.ArgumentParser:
	parser = _Parser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
	sub.add_parser("labels")
	stop_cmd = sub.add_parser("stop")
	stop_cmd.add_argument("--labels-json", default="[]")
	stop_cmd.add_argument("--project-failed", action="store_true")
	fp_cmd = sub.add_parser("fingerprint")
	fp_cmd.add_argument("--stop", required=True)
	fp_cmd.add_argument("--evidence-file", required=True)
	decide_cmd = sub.add_parser("decide")
	decide_cmd.add_argument("--item", required=True)
	decide_cmd.add_argument("--stop", required=True)
	decide_cmd.add_argument("--fingerprint", required=True)
	decide_cmd.add_argument("--comments-file", required=True)
	decide_cmd.add_argument("--project-comments-file")
	decide_cmd.add_argument("--trusted-login", required=True)
	decide_cmd.add_argument("--now", required=True)
	decide_cmd.add_argument("--kind", default="issue")
	decide_cmd.add_argument("--last-activity", default="")
	decide_cmd.add_argument("--rejection-file")
	decide_cmd.add_argument("--security-issue", action="store_true")
	validate_cmd = sub.add_parser("validate")
	validate_cmd.add_argument("--verdict-file", required=True)
	validate_cmd.add_argument("--decision-file", required=True)
	validate_cmd.add_argument("--repo", required=True)
	validate_cmd.add_argument("--rejected-deletions-file")
	rejection_cmd = sub.add_parser("rejection-snapshot")
	rejection_cmd.add_argument("--run-file", required=True)
	rejection_cmd.add_argument("--artifact-file", required=True)
	rejection_cmd.add_argument("--zip-file", required=True)
	rejection_cmd.add_argument("--item", required=True)
	rejection_cmd.add_argument("--repo", required=True)
	rejection_cmd.add_argument("--approved-json")
	validate_cmd.add_argument("--rejection-file")
	rejection_cmd = sub.add_parser("rejection")
	rejection_cmd.add_argument("--item", required=True)
	rejection_cmd.add_argument("--stop", required=True)
	rejection_cmd.add_argument("--comments-file", required=True)
	rejection_cmd.add_argument("--trusted-login", required=True)
	marker_cmd = sub.add_parser("marker")
	marker_cmd.add_argument("--item", required=True)
	marker_cmd.add_argument("--stop", required=True)
	marker_cmd.add_argument("--fingerprint", required=True)
	marker_cmd.add_argument("--verdict", required=True)
	marker_cmd.add_argument("--round", required=True, type=int)
	marker_cmd.add_argument("--override", default="")
	return parser


def run(argv: list[str] | None = None) -> dict:
	args = build_parser().parse_args(argv)
	if args.command == "labels":
		return {"labels": list(BLOCK_LABELS)}
	if args.command == "stop":
		try:
			labels = json.loads(args.labels_json)
		except ValueError as exc:
			raise UsageError(f"--labels-json is not JSON: {exc}") from exc
		return {"stop": stop_for_labels(labels, args.project_failed)}
	if args.command == "fingerprint":
		evidence = _read_json(args.evidence_file, "--evidence-file")
		return {"stop": args.stop, "fingerprint": fingerprint(args.stop, evidence)}
	if args.command == "rejection":
		if not LOGIN_RE.fullmatch(args.trusted_login):
			raise UsageError("--trusted-login is not a GitHub login")
		return latest_rejection(_read_json(args.comments_file, "--comments-file"), args.trusted_login,
			_check_item(args.item), _check_stop(args.stop))
	if args.command == "decide":
		if not LOGIN_RE.match(args.trusted_login):
			raise UsageError(f"--trusted-login is not a GitHub login: {args.trusted_login!r}")
		item = _check_item(args.item)
		stop = _check_stop(args.stop)
		fp = _check_fingerprint(args.fingerprint)
		item_entries = parse_markers(_read_json(args.comments_file, "--comments-file"), args.trusted_login)
		project_entries = None
		if args.project_comments_file:
			project_entries = parse_markers(_read_json(args.project_comments_file, "--project-comments-file"), args.trusted_login)
		last_activity = _parse_time(args.last_activity) if args.last_activity else None
		rejection = _read_json(args.rejection_file, "--rejection-file") if args.rejection_file else None
		return decide(
			item, stop, fp, item_entries, project_entries, _parse_time(args.now), args.kind, last_activity, rejection,
			security_issue=args.security_issue,
		)
	if args.command == "validate":
		return validate(
			_read_json(args.verdict_file, "--verdict-file"),
			_read_json(args.decision_file, "--decision-file"),
			args.repo,
			_read_json(args.rejection_file, "--rejection-file") if args.rejection_file else None,
			_read_json(args.rejected_deletions_file, "--rejected-deletions-file") if args.rejected_deletions_file else None,
		)
	if args.command == "rejection-snapshot":
		try:
			approved = json.loads(args.approved_json) if args.approved_json is not None else None
		except ValueError as exc:
			raise UsageError("--approved-json is not JSON") from exc
		return rejection_snapshot(
			_read_json(args.run_file, "--run-file"), _read_json(args.artifact_file, "--artifact-file"),
			args.zip_file, _check_item(args.item), args.repo, approved,
		)
	item = _check_item(args.item)
	return {
		"marker": marker(
			item, _check_stop(args.stop), _check_fingerprint(args.fingerprint), args.verdict, args.round, args.override
		)
	}


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

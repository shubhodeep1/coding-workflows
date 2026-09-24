#!/usr/bin/env python3
"""Shared logic for the workflow failure heal pipeline (report + intake).

Two shell drivers use this module through its CLI:

  * ``scripts/workflow_failure_heal_report.sh`` runs in the repository where an
    escalation label (``ai:needs-human`` and friends) was applied. It builds the
    thin ``repository_dispatch`` payload that is sent to coding-workflows.
  * ``scripts/workflow_failure_heal_autofix_report.sh`` runs inside the
    failure path of ``review_autofix.yml`` (in consumers and in this repo). It
    counts how many review runs in a row failed on the pull request and, past
    the streak threshold, reports the failure with the run's own evidence.
  * ``scripts/workflow_failure_heal_intake.sh`` runs in coding-workflows. It
    validates the payload, fingerprints the failure, applies the dedup / lineage
    / budget gates, and composes the heal issue body.

Everything that needs a unit test lives here so the shell drivers stay thin.
All functions are pure except the CLI entrypoints, which only read the files
they are given and write JSON or text to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "workflow_failure_heal.v1"
DISPATCH_EVENT_TYPE = "workflow-failure-heal"
HEAL_LABEL = "ai:workflow-heal"
ESCALATED_LABEL = "ai:workflow-heal-escalated"
MARKER_PREFIX = "workflow-failure-heal:"
DEFAULT_UPSTREAM_REPO = "shubhodeep1/coding-workflows"
SELF_WORKFLOW_FRAGMENT = "Workflow Failure Heal"

# Labels that mean "a human has to look at this" after a pipeline failure.
# Keep in sync with the `if:` predicate of workflow-templates/ai-workflow-failure-heal.yml
# and .github/workflows/internal-workflow-failure-heal.yml (tests pin the parity).
HUMAN_NEEDED_LABELS: tuple[str, ...] = (
	"ai:needs-human",
	"ai:check-triage-escalated",
	"ai:destructive-blocked",
	"ai:scope-blocked",
	"ai:harness-broken",
	"ai:resolver-escalated",
	"ai:security-pass-failed",
)

# Release / promotion workflows in coding-workflows whose failed runs are
# reported through the `workflow_run` trigger of the intake workflow.
RELEASE_WORKFLOW_NAMES: tuple[str, ...] = (
	"Test & Mark Stable Release",
	"Mark Stable Release",
	"Promote main to stable",
	"Auto release stable",
	"Forward-merge stable to main",
)

SOURCE_KINDS = ("issue", "pull_request", "workflow_run", "autofix_failure")
REPORTABLE_CONCLUSIONS = ("failure", "timed_out")

# `already-fixed` opens no issue, but only when check_heal_already_fixed_claim
# confirms the diagnosis cites a commit that landed on the branch after the
# failing SHA; otherwise the intake downgrades it to `inconclusive`.
ALREADY_FIXED_CLASSIFICATION = "already-fixed"
CLASSIFICATIONS: tuple[str, ...] = (
	"workflow-defect",
	"consumer-app-defect",
	"consumer-config",
	"transient",
	"inconclusive",
	ALREADY_FIXED_CLASSIFICATION,
	"pr-self-inflicted",
	"base-self-inflicted",
)
# Classifications that open an issue in coding-workflows.
UPSTREAM_ISSUE_CLASSIFICATIONS = ("workflow-defect", "inconclusive")
# Self-inflicted review/autofix failures in this repository (README "Workflow
# Failure Heal"): the crash is in a file the pull request itself changed
# (pr-self-inflicted -> diagnosis comment on the PR, no issue) or in a file its
# base branch changed relative to main (base-self-inflicted -> issue targeting
# that base branch). The intake only honours them when its own ownership check
# agrees; otherwise they route as workflow-defect.
SELF_INFLICTED_CLASSIFICATIONS = ("pr-self-inflicted", "base-self-inflicted")
CRASH_OWNERSHIP_VALUES = ("pr", "base", "none")

DEFAULT_MAX_LINEAGE_DEPTH = 3
DEFAULT_MAX_OPEN_ISSUES = 10
DEFAULT_MAX_ISSUES_PER_DAY = 20
DEFAULT_TARGET_BRANCH = "stable"
DEFAULT_AUTOFIX_FAILURE_STREAK = 2
FAILURE_EVIDENCE_LIMIT = 4000  # same bound as ISSUE_EXCERPT_LIMIT (defined below)

# PR comment markers the review/autofix workflow posts. The streak counter reads
# them newest first. An editor summary ends the streak unless the next newer
# failure marker shows that the same run failed after posting its summary.
AUTOFIX_FAILURE_COMMENT_MARKERS: tuple[str, ...] = (
	"AI review/autofix produced no output",
	"AI review/autofix failed",
	"AI review/autofix encountered a post-editor failure",
	"Editor changes lost",
	"Editor no-op suspicious",
)
AUTOFIX_SUCCESS_COMMENT_MARKERS: tuple[str, ...] = (
	"AI autofix editor summary",
)
AUTOFIX_POST_SUMMARY_FAILURE_COMMENT_MARKERS: tuple[str, ...] = (
	"AI review/autofix encountered a post-editor failure",
	"Editor changes lost",
	"Editor no-op suspicious",
)

# Identical-failure fingerprint cap (review_autofix.yml gate). Every failure
# comment the review workflow posts ends with a failure marker; the gate counts
# the trailing markers for the current head that share the newest fingerprint
# and stops the run once REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL is reached.
FAILURE_MARKER_TAG = "review-autofix-failure:v1"
FAILURE_CAP_MARKER_TAG = "review-autofix-failure-cap:v1"
FAILURE_FINGERPRINT_WORKFLOW = "review_autofix"
FAILURE_EVIDENCE_TAIL_BYTES = 65_536
DEFAULT_FAILURE_FINGERPRINT_MAX_IDENTICAL = 3

ISSUE_EXCERPT_LIMIT = 4000
COMMENTS_EXCERPT_LIMIT = 6000
MAX_RUN_REFS = 3
MAX_PAYLOAD_BYTES = 60_000
# GitHub's "Create a repository dispatch event" API rejects a `client_payload`
# with more than 10 top-level properties (HTTP 422). The report has ~20 keys,
# so reporters wrap it in a {schema_version, report} envelope (wrap_dispatch).
DISPATCH_CLIENT_PAYLOAD_MAX_KEYS = 10
SIGNATURE_LINE_LIMIT = 5
# Ownership facts carried by an autofix_failure report (self-repo routing).
CHANGED_FILES_MAX_ENTRIES = 200
CHANGED_FILE_MAX_CHARS = 120
SIGNATURE_CHAR_LIMIT = 500
# Branch progress since the failing SHA (one compare call per intake). The
# compare API returns at most 250 commits and 300 files; the prompt shows the
# newest BRANCH_PROGRESS_COMMITS_SHOWN / first BRANCH_PROGRESS_FILES_SHOWN.
BRANCH_PROGRESS_COMMIT_LIMIT = 250
BRANCH_PROGRESS_FILE_LIMIT = 300
BRANCH_PROGRESS_COMMITS_SHOWN = 60
BRANCH_PROGRESS_FILES_SHOWN = 200
# Earlier heal issues of the same fingerprint / lineage shown to the model.
HEAL_LINEAGE_CONTEXT_LIMIT = 5
HEAL_LINEAGE_EXCERPT_LIMIT = 1500

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_FAILURE_REASON_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,79}$")
_RUN_URL_RE = re.compile(
	r"https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/(?P<run_id>[0-9]+)"
)
_SMOKE_TITLE_RE = re.compile(r"\[E2E Smoke Test\b", re.IGNORECASE)
_SMOKE_LABELS = frozenset({"e2e-smoke-test"})
_MARKER_RE = re.compile(r"<!--\s*" + re.escape(MARKER_PREFIX) + r"(?P<key>[a-z_]+)=(?P<value>[^\s>]+)\s*-->")
_ORCHESTRATOR_STATE_V2_COMMENT_RE = re.compile(
	r"<!-- ORCHESTRATOR_STATE_V2 part=(?P<part>[1-9][0-9]*)/(?P<total>[1-9][0-9]*) manifest=[0-9a-f]{64} -->\n"
	r".*\nORCHESTRATOR_STATE_V2 -->",
	re.DOTALL,
)
_CLASSIFICATION_RE = re.compile(r"^##\s*Classification\s*$", re.IGNORECASE | re.MULTILINE)
_HEAL_FIXED_BY_SECTION_RE = re.compile(r"^##\s*Fixed by\s*$", re.IGNORECASE | re.MULTILINE)
_HEAL_COMMIT_REF_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LOG_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s?")
_FAILURE_MARKER_RE = re.compile(r"<!--\s*" + re.escape(FAILURE_MARKER_TAG) + r"\s+(?P<fields>[^>]*?)\s*-->")
_FAILURE_CAP_MARKER_RE = re.compile(r"<!--\s*" + re.escape(FAILURE_CAP_MARKER_TAG) + r"\s+(?P<fields>[^>]*?)\s*-->")
_MARKER_FIELD_RE = re.compile(r"(?P<key>[a-z_]+)=(?P<value>\S+)")
_FP_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.-]")
_REPO_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,120}$")
_ORCHESTRATOR_BRANCH_RE = re.compile(r"^orchestrator/project-([0-9]+)$")
# `bash: /path/to/scripts/<name>: line N: ...` (a shell guard or syntax error
# in a staged script) -> scripts/<name>.
_CRASH_SCRIPT_LINE_RE = re.compile(r"(?:^|[\s/])scripts/(?P<name>[A-Za-z0-9_.-]+): line [0-9]+:")
# `::error::` / `##[error]` lines that name a repository path.
_CRASH_ERROR_LINE_RE = re.compile(r"(?:::error::|##\[error\])")
_CRASH_ERROR_PATH_RE = re.compile(r"(?:^|[^A-Za-z0-9_.-])(?P<path>(?:scripts|\.github/workflows)/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)")

# Highest-signal first: the first bucket with any matching line wins. The
# runner renders a step's `::error::` workflow command as `##[error]` in the job
# log, so a literal `::error::` in an Actions log is almost always the step's own
# script source echoed in its header (see _drop_step_script_lines); both forms
# stay in the first bucket so reporter evidence text keeps matching too.
_SIGNATURE_PATTERNS: tuple[re.Pattern[str], ...] = (
	re.compile(r"::error::|##\[error\]", re.IGNORECASE),
	re.compile(r"\b[A-Z][A-Z0-9_]*_FAILED\b"),
	re.compile(r"\bTraceback \(most recent call last\)|^\s*\w+Error:", re.MULTILINE),
	re.compile(r"\bfatal:|\berror:|\bERROR\b|\bFAILED\b|\bexit code\b|\bexited with\b", re.IGNORECASE),
)
# GitHub opens every `run:` step with a `##[group]Run <first line>` header that
# echoes the whole script, one ANSI-cyan line per source line, before the
# `shell:` / `env:` block and the closing `##[endgroup]`. Those lines are the
# step's source, not its output: a script that can print forty different
# `::error::` messages echoes all forty on every run, whatever actually failed.
_STEP_HEADER_OPEN_RE = re.compile(r"^##\[group\]Run ")
_STEP_HEADER_CLOSE_RE = re.compile(r"^##\[endgroup\]")
_STEP_SCRIPT_LINE_PREFIX = "\x1b[36;1m"
# Test & Mark Stable Release runs dispatched by a promote cycle carry the
# cycle's run id in their run name (`run-name: ... [cycle:<id>]`, which
# scripts/promote_main_cycle.sh matches on). The id is unique per cycle, so it
# must not reach the dedup fingerprint.
_CYCLE_RUN_NAME_SUFFIX_RE = re.compile(r"\s*\[cycle:[0-9]+\]\s*$", re.IGNORECASE)
_SOFT_LOG_PATTERNS = re.compile(
	r"::error::|::warning::|\bERROR\b|\bFAIL(?:ED|URE)?\b|\bfatal\b|\bTraceback\b|"
	r"\b[A-Z][A-Z0-9_]*_(?:FAILED|SKIPPED|ESCALATE|BLOCKED)\b|\bexit code\b|\btimed?[ -]?out\b|\brate.?limit",
	re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
	return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
	return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> datetime | None:
	if not isinstance(value, str) or not value:
		return None
	text = value.strip()
	if text.endswith("Z"):
		text = text[:-1] + "+00:00"
	try:
		parsed = datetime.fromisoformat(text)
	except ValueError:
		return None
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=timezone.utc)
	return parsed.astimezone(timezone.utc)


def sanitize_text(value: Any, limit: int | None = None) -> str:
	"""Strip control characters and ANSI escapes; optionally truncate."""
	if value is None:
		return ""
	text = str(value)
	text = _ANSI_RE.sub("", text)
	text = _CONTROL_CHARS_RE.sub("", text)
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	if limit is not None and len(text) > limit:
		text = text[:limit].rstrip() + "\n[... truncated ...]"
	return text


def single_line(value: Any, limit: int = 200) -> str:
	text = sanitize_text(value)
	text = " ".join(text.split())
	return text[:limit]


def _load_json_file(path: str) -> Any:
	return json.loads(Path(path).read_text(encoding="utf-8"))


def _positive_int(value: Any) -> int | None:
	try:
		number = int(str(value).strip())
	except (TypeError, ValueError):
		return None
	return number if number > 0 else None


def is_valid_repo_slug(value: Any) -> bool:
	return isinstance(value, str) and bool(_REPO_SLUG_RE.match(value)) and ".." not in value


def is_valid_sha(value: Any) -> bool:
	return isinstance(value, str) and bool(_SHA_RE.match(value))


def is_valid_branch(value: Any) -> bool:
	return isinstance(value, str) and bool(_BRANCH_RE.match(value)) and ".." not in value and not value.endswith("/")


# ---------------------------------------------------------------------------
# Markers + run references
# ---------------------------------------------------------------------------


def parse_heal_markers(body: str | None) -> dict[str, str]:
	"""Return the ``<!-- workflow-failure-heal:key=value -->`` markers of a body."""
	markers: dict[str, str] = {}
	for match in _MARKER_RE.finditer(body or ""):
		markers.setdefault(match.group("key"), match.group("value"))
	return markers


def extract_run_refs(texts: Iterable[str], repo: str, limit: int = MAX_RUN_REFS) -> list[dict[str, Any]]:
	"""Collect unique ``actions/runs/<id>`` links for ``repo`` from free text.

	The newest mention wins ordering: the last ``limit`` unique run ids seen
	across the given texts (in order) are returned, most recent last.
	"""
	seen: dict[str, dict[str, Any]] = {}
	for text in texts:
		for match in _RUN_URL_RE.finditer(text or ""):
			if match.group("repo") != repo:
				continue
			run_id = match.group("run_id")
			seen.pop(run_id, None)
			seen[run_id] = {"repo": repo, "run_id": run_id, "url": match.group(0)}
	refs = list(seen.values())
	return refs[-limit:]


def select_failed_runs(runs: Iterable[dict[str, Any]], *, title: str, limit: int = MAX_RUN_REFS, since: datetime | None = None) -> list[dict[str, Any]]:
	"""Pick recent failed runs whose display title matches the issue title."""
	wanted = single_line(title, 500)
	picked: list[dict[str, Any]] = []
	for run in runs:
		if not isinstance(run, dict):
			continue
		if str(run.get("conclusion") or "") not in ("failure", "timed_out", "cancelled"):
			continue
		if single_line(run.get("display_title"), 500) != wanted:
			continue
		created = _parse_iso(run.get("created_at"))
		if since is not None and created is not None and created < since:
			continue
		run_id = _positive_int(run.get("id"))
		if run_id is None:
			continue
		picked.append(
			{
				"run_id": str(run_id),
				"url": sanitize_text(run.get("html_url"), 300),
				"workflow_name": single_line(run.get("name"), 200),
				"created_at": sanitize_text(run.get("created_at"), 40),
			}
		)
	picked.sort(key=lambda item: item.get("created_at") or "")
	return picked[-limit:]


# ---------------------------------------------------------------------------
# Payload construction + validation
# ---------------------------------------------------------------------------


def _labels_of(issue: dict[str, Any]) -> list[str]:
	names: list[str] = []
	for label in issue.get("labels") or []:
		if isinstance(label, dict):
			name = label.get("name")
		else:
			name = label
		if isinstance(name, str) and name:
			names.append(name)
	return names


def _build_recent_comments_excerpt(comment_texts: list[str], limit: int = COMMENTS_EXCERPT_LIMIT) -> str:
	"""Return up to eight recent comments, newest first, within ``limit`` chars."""
	if limit <= 0:
		return ""

	separator = "\n\n---\n\n"
	truncation_marker = "\n[... truncated ...]"
	pieces: list[str] = []
	used = 0
	for comment_text in reversed(comment_texts[-8:]):
		text = sanitize_text(comment_text)
		state_match = _ORCHESTRATOR_STATE_V2_COMMENT_RE.fullmatch(text.strip())
		if state_match is not None:
			part = int(state_match.group("part"))
			total = int(state_match.group("total"))
			if part <= total:
				text = f"[ORCHESTRATOR_STATE_V2 state part {part}/{total} omitted]"

		prefix = separator if pieces else ""
		available = limit - used - len(prefix)
		if available <= 0:
			break
		if len(text) > available:
			if available > len(truncation_marker):
				text = text[: available - len(truncation_marker)].rstrip() + truncation_marker
			else:
				text = text[:available]
			pieces.append(prefix + text)
			break
		pieces.append(prefix + text)
		used += len(prefix) + len(text)
	return "".join(pieces)


def build_issue_payload(
	*,
	repo: str,
	kind: str,
	label: str,
	issue: dict[str, Any],
	comments: list[dict[str, Any]],
	runs: list[dict[str, Any]],
	wrapper_sha: str | None,
	reporter_run_url: str | None,
	now: datetime | None = None,
) -> dict[str, Any]:
	"""Build the dispatch payload for an escalation label on an issue / PR."""
	now = now or _utc_now()
	body = sanitize_text(issue.get("body"))
	markers = parse_heal_markers(body)
	comment_texts = [sanitize_text(comment.get("body")) for comment in comments if isinstance(comment, dict)]
	run_refs = extract_run_refs([body, *comment_texts], repo)
	if len(run_refs) < MAX_RUN_REFS:
		known = {ref["run_id"] for ref in run_refs}
		for run in select_failed_runs(runs, title=str(issue.get("title") or "")):
			if run["run_id"] in known:
				continue
			run_refs.append({"repo": repo, "run_id": run["run_id"], "url": run["url"]})
			known.add(run["run_id"])
			if len(run_refs) >= MAX_RUN_REFS:
				break
	return {
		"schema_version": SCHEMA_VERSION,
		"source_repo": repo,
		"source_kind": kind,
		"issue_number": _positive_int(issue.get("number")),
		"issue_title": single_line(issue.get("title"), 300),
		"issue_url": sanitize_text(issue.get("html_url"), 300),
		"label": label,
		"labels": _labels_of(issue)[:50],
		"run_refs": run_refs,
		"wrapper_sha": wrapper_sha if is_valid_sha(wrapper_sha) else None,
		"source_gen": _positive_int(markers.get("gen")),
		"source_root": markers.get("root") if re.fullmatch(r"[0-9a-f]{64}", markers.get("root") or "") else None,
		"issue_excerpt": sanitize_text(body, ISSUE_EXCERPT_LIMIT),
		"comments_excerpt": _build_recent_comments_excerpt(comment_texts),
		"workflow_name": None,
		"head_branch": None,
		"head_sha": None,
		"conclusion": None,
		"reporter_run_url": sanitize_text(reporter_run_url, 300) or None,
		"reported_at": _iso(now),
	}


def build_workflow_run_payload(*, repo: str, workflow_run: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
	"""Build the payload for a failed release / promotion run in this repo."""
	now = now or _utc_now()
	run_id = _positive_int(workflow_run.get("id"))
	head_sha = str(workflow_run.get("head_sha") or "").lower()
	head_branch = str(workflow_run.get("head_branch") or "")
	return {
		"schema_version": SCHEMA_VERSION,
		"source_repo": repo,
		"source_kind": "workflow_run",
		"issue_number": None,
		"issue_title": single_line(workflow_run.get("display_title"), 300),
		"issue_url": None,
		"label": None,
		"labels": [],
		"run_refs": [{"repo": repo, "run_id": str(run_id), "url": sanitize_text(workflow_run.get("html_url"), 300)}] if run_id else [],
		"wrapper_sha": None,
		"source_gen": None,
		"source_root": None,
		"issue_excerpt": "",
		"comments_excerpt": "",
		"workflow_name": single_line(workflow_run.get("name"), 200),
		"head_branch": head_branch if is_valid_branch(head_branch) else None,
		"head_sha": head_sha if is_valid_sha(head_sha) else None,
		"conclusion": single_line(workflow_run.get("conclusion"), 40) or None,
		"reporter_run_url": None,
		"reported_at": _iso(now),
	}


def count_autofix_failure_streak(comments: Iterable[dict[str, Any]]) -> int:
	"""Count the trailing review/autofix failure comments on a pull request.

	``comments`` is the PR's issue-comment list, oldest first (the order the
	GitHub API returns). Scanning from the newest comment, every failure marker
	adds one. An editor summary stops the count unless it belongs to the
	post-editor failure immediately newer than it. The current run's own failure
	comment is normally not in the list yet, so the reporter adds one for it.
	"""
	streak = 0
	skip_paired_summary = False
	for comment in reversed(list(comments)):
		if not isinstance(comment, dict):
			continue
		body = sanitize_text(comment.get("body"))
		if any(marker in body for marker in AUTOFIX_FAILURE_COMMENT_MARKERS):
			streak += 1
			skip_paired_summary = any(marker in body for marker in AUTOFIX_POST_SUMMARY_FAILURE_COMMENT_MARKERS)
			continue
		if any(marker in body for marker in AUTOFIX_SUCCESS_COMMENT_MARKERS):
			if skip_paired_summary:
				skip_paired_summary = False
				continue
			break
	return streak


def build_autofix_failure_payload(
	*,
	repo: str,
	pr: dict[str, Any],
	comments: list[dict[str, Any]],
	workflow_name: str,
	failure_reason: str,
	failure_evidence: str,
	failure_streak: int,
	run_id: str,
	run_url: str | None,
	wrapper_sha: str | None,
	reporter_run_url: str | None,
	now: datetime | None = None,
	failure_fingerprint: str | None = None,
	base_branch: str | None = None,
	script_ref: str | None = None,
	changed_files: Iterable[str] | None = None,
) -> dict[str, Any]:
	"""Build the dispatch payload for a failed review/autofix run on a PR.

	``failure_fingerprint`` is the optional ``fp`` of the run's
	``review-autofix-failure:v1`` marker; it is sent only when it is 64 hex.

	``base_branch``, ``script_ref`` and ``changed_files`` are the optional
	ownership facts the intake's self-inflicted routing uses; each is sent only
	when it validates. ``crash_file`` is extracted from the full (untruncated)
	evidence before it is bounded.
	"""
	now = now or _utc_now()
	body = sanitize_text(pr.get("body"))
	comment_texts = [sanitize_text(comment.get("body")) for comment in comments if isinstance(comment, dict)]
	run_number = _positive_int(run_id)
	head_sha = str(pr.get("head", {}).get("sha") or "").lower() if isinstance(pr.get("head"), dict) else ""
	head_branch = str(pr.get("head", {}).get("ref") or "") if isinstance(pr.get("head"), dict) else ""
	payload: dict[str, Any] = {
		"schema_version": SCHEMA_VERSION,
		"source_repo": repo,
		"source_kind": "autofix_failure",
		"issue_number": _positive_int(pr.get("number")),
		"issue_title": single_line(pr.get("title"), 300),
		"issue_url": sanitize_text(pr.get("html_url"), 300),
		"label": None,
		"labels": _labels_of(pr)[:50],
		"run_refs": [{"repo": repo, "run_id": str(run_number), "url": sanitize_text(run_url, 300)}] if run_number else [],
		"wrapper_sha": wrapper_sha if is_valid_sha(wrapper_sha) else None,
		"source_gen": None,
		"source_root": None,
		"issue_excerpt": sanitize_text(body, ISSUE_EXCERPT_LIMIT),
		"comments_excerpt": _build_recent_comments_excerpt(comment_texts),
		"workflow_name": single_line(workflow_name, 200),
		"head_branch": head_branch if is_valid_branch(head_branch) else None,
		"head_sha": head_sha if is_valid_sha(head_sha) else None,
		"conclusion": "failure",
		"failure_reason": single_line(failure_reason, 80),
		"failure_evidence": sanitize_text(failure_evidence, FAILURE_EVIDENCE_LIMIT),
		"failure_streak": max(1, _positive_int(failure_streak) or 1),
		"reporter_run_url": sanitize_text(reporter_run_url, 300) or None,
		"reported_at": _iso(now),
	}
	fp = str(failure_fingerprint or "").strip().lower()
	if _FP_HEX_RE.match(fp):
		payload["failure_fingerprint"] = fp
	base = str(base_branch or "").strip()
	if is_valid_branch(base):
		payload["base_branch"] = base
	ref = _normalize_script_ref(script_ref)
	if ref:
		payload["script_ref"] = ref
	files = normalize_changed_files(changed_files or [])
	if files:
		payload["changed_files"] = files
	crash = extract_crash_file(failure_evidence)
	if crash:
		payload["crash_file"] = crash
	return payload


def _normalize_script_ref(value: Any) -> str | None:
	"""``script_ref`` is the coding-workflows ref the run staged: a SHA or ``stable``."""
	ref = str(value or "").strip()
	if ref == "stable":
		return ref
	ref = ref.lower()
	return ref if is_valid_sha(ref) else None


def is_valid_repo_path(value: Any) -> bool:
	"""A bounded repo-relative path: ``[A-Za-z0-9_./-]``, no ``..`` segment, not absolute."""
	if not isinstance(value, str) or not _REPO_PATH_RE.match(value):
		return False
	if value.startswith("/"):
		return False
	return all(part not in ("", ".", "..") for part in value.split("/"))


def normalize_changed_files(paths: Iterable[Any]) -> list[str]:
	"""Keep valid, de-duplicated repo-relative paths, capped at CHANGED_FILES_MAX_ENTRIES.

	Paths longer than CHANGED_FILE_MAX_CHARS or failing ``is_valid_repo_path``
	are dropped rather than truncated, so a kept path always names a real file.
	"""
	kept: list[str] = []
	seen: set[str] = set()
	for raw in paths:
		if not isinstance(raw, str):
			continue
		path = raw.strip()
		if len(path) > CHANGED_FILE_MAX_CHARS or not is_valid_repo_path(path) or path in seen:
			continue
		seen.add(path)
		kept.append(path)
		if len(kept) >= CHANGED_FILES_MAX_ENTRIES:
			break
	return kept


def extract_crash_file(evidence_text: Any) -> str | None:
	"""Name the repository file a review/autofix failure crashed in, if the evidence says.

	Two shapes are recognised, first match wins in this order:

	- a shell error ``…/scripts/<name>: line N: …`` -> ``scripts/<name>``,
	since the staged support bundle keeps the ``scripts/`` directory name;
	- a ``::error::`` / ``##[error]`` line naming a ``scripts/…`` or
	``.github/workflows/…`` path -> that path.

	Anything else (an error line that names no path) returns ``None``.
	"""
	text = sanitize_text(evidence_text)
	if not text:
		return None
	for line in text.split("\n"):
		match = _CRASH_SCRIPT_LINE_RE.search(line)
		if match:
			candidate = f"scripts/{match.group('name')}"
			if is_valid_repo_path(candidate):
				return candidate
	for line in text.split("\n"):
		if not _CRASH_ERROR_LINE_RE.search(line):
			continue
		for match in _CRASH_ERROR_PATH_RE.finditer(line):
			candidate = match.group("path").rstrip(".")
			if is_valid_repo_path(candidate):
				return candidate
	return None


def classify_crash_ownership(*, crash_file: str | None, changed_files: Iterable[str], base_changed_files: Iterable[str]) -> str:
	"""Return ``pr``, ``base`` or ``none`` for the file a review/autofix run crashed in.

	``pr``: the crash file is in the pull request's own diff. ``base``: it is
	not, but the PR's base branch changed it relative to ``main``. ``none``:
	no crash file, or neither side changed it.
	"""
	if not crash_file:
		return "none"
	if crash_file in set(changed_files):
		return "pr"
	if crash_file in set(base_changed_files):
		return "base"
	return "none"


def orchestrator_tracking_issue(branch: Any) -> int | None:
	"""The tracking issue number of an ``orchestrator/project-<N>`` branch, else None."""
	match = _ORCHESTRATOR_BRANCH_RE.match(str(branch or ""))
	return _positive_int(match.group(1)) if match else None


def validate_payload(payload: Any) -> dict[str, Any]:
	"""Validate + normalise an incoming payload. Raises ValueError when unusable.

	The payload arrives over ``repository_dispatch`` (authenticated, but still
	external to this run), so every field is re-checked here before any of it
	reaches a shell driver or a prompt.
	"""
	if not isinstance(payload, dict):
		raise ValueError("payload must be a JSON object")
	if payload.get("schema_version") != SCHEMA_VERSION:
		raise ValueError(f"unsupported schema_version {payload.get('schema_version')!r}")
	repo = payload.get("source_repo")
	if not is_valid_repo_slug(repo):
		raise ValueError("source_repo must be an owner/name slug")
	kind = payload.get("source_kind")
	if kind not in SOURCE_KINDS:
		raise ValueError("source_kind must be one of " + ", ".join(SOURCE_KINDS))

	issue_number = _positive_int(payload.get("issue_number")) if payload.get("issue_number") is not None else None
	label = payload.get("label")
	if kind in ("issue", "pull_request"):
		if issue_number is None:
			raise ValueError("issue_number is required for issue / pull_request reports")
		if label not in HUMAN_NEEDED_LABELS:
			raise ValueError("label is not a human-needed escalation label")
	else:
		label = None
	failure_reason = payload.get("failure_reason")
	failure_reason = failure_reason if isinstance(failure_reason, str) and _FAILURE_REASON_RE.match(failure_reason) else None
	failure_streak = _positive_int(payload.get("failure_streak")) if payload.get("failure_streak") is not None else None
	failure_fingerprint = str(payload.get("failure_fingerprint") or "").strip().lower()
	failure_fingerprint = failure_fingerprint if _FP_HEX_RE.match(failure_fingerprint) else None
	if kind == "autofix_failure":
		if issue_number is None:
			raise ValueError("issue_number (the pull request number) is required for autofix_failure reports")
		if failure_reason is None:
			raise ValueError("failure_reason is missing or malformed for autofix_failure reports")
		failure_streak = failure_streak or 1
	else:
		failure_reason = None
		failure_streak = None
		failure_fingerprint = None

	run_refs: list[dict[str, str]] = []
	for ref in payload.get("run_refs") or []:
		if not isinstance(ref, dict):
			continue
		run_id = _positive_int(ref.get("run_id"))
		if run_id is None or ref.get("repo") != repo:
			continue
		url = f"https://github.com/{repo}/actions/runs/{run_id}"
		run_refs.append({"repo": repo, "run_id": str(run_id), "url": url})
		if len(run_refs) >= MAX_RUN_REFS:
			break

	wrapper_sha = payload.get("wrapper_sha")
	wrapper_sha = wrapper_sha.lower() if isinstance(wrapper_sha, str) and is_valid_sha(wrapper_sha.lower()) else None
	head_sha = payload.get("head_sha")
	head_sha = head_sha.lower() if isinstance(head_sha, str) and is_valid_sha(head_sha.lower()) else None
	head_branch = payload.get("head_branch")
	head_branch = head_branch if is_valid_branch(head_branch) else None
	if kind == "workflow_run":
		if not run_refs:
			raise ValueError("workflow_run reports need the failed run reference")
		conclusion = payload.get("conclusion")
		if conclusion not in REPORTABLE_CONCLUSIONS:
			raise ValueError("workflow_run conclusion is not reportable")
	source_root = payload.get("source_root")
	if not (isinstance(source_root, str) and re.fullmatch(r"[0-9a-f]{64}", source_root)):
		source_root = None

	labels = [single_line(name, 100) for name in payload.get("labels") or [] if isinstance(name, str)][:50]
	# Ownership facts (optional, autofix_failure only): invalid values are
	# dropped, never fatal, so an older or partial reporter still gets healed.
	base_branch = payload.get("base_branch") if kind == "autofix_failure" else None
	base_branch = base_branch if is_valid_branch(base_branch) else None
	script_ref = _normalize_script_ref(payload.get("script_ref")) if kind == "autofix_failure" else None
	changed_raw = payload.get("changed_files") if kind == "autofix_failure" else None
	changed_files = normalize_changed_files(changed_raw) if isinstance(changed_raw, list) else []
	crash_file = payload.get("crash_file") if kind == "autofix_failure" else None
	crash_file = crash_file if is_valid_repo_path(crash_file) else None
	normalized = {
		"schema_version": SCHEMA_VERSION,
		"source_repo": repo,
		"source_kind": kind,
		"issue_number": issue_number,
		"issue_title": single_line(payload.get("issue_title"), 300),
		"issue_url": sanitize_text(payload.get("issue_url"), 300) if kind != "workflow_run" else None,
		"label": label,
		"labels": labels,
		"run_refs": run_refs,
		"wrapper_sha": wrapper_sha,
		"source_gen": _positive_int(payload.get("source_gen")) if payload.get("source_gen") is not None else None,
		"source_root": source_root,
		"issue_excerpt": sanitize_text(payload.get("issue_excerpt"), ISSUE_EXCERPT_LIMIT),
		"comments_excerpt": sanitize_text(payload.get("comments_excerpt"), COMMENTS_EXCERPT_LIMIT),
		"workflow_name": single_line(payload.get("workflow_name"), 200) or None,
		"head_branch": head_branch,
		"head_sha": head_sha,
		"conclusion": single_line(payload.get("conclusion"), 40) or None,
		"failure_reason": failure_reason,
		"failure_evidence": sanitize_text(payload.get("failure_evidence"), FAILURE_EVIDENCE_LIMIT) if kind == "autofix_failure" else "",
		"failure_streak": failure_streak,
		"failure_fingerprint": failure_fingerprint,
		"reporter_run_url": sanitize_text(payload.get("reporter_run_url"), 300) or None,
		"reported_at": sanitize_text(payload.get("reported_at"), 40) or _iso(_utc_now()),
	}
	if kind == "autofix_failure":
		normalized.update({
			"base_branch": base_branch,
			"script_ref": script_ref,
			"changed_files": changed_files,
			"crash_file": crash_file,
		})
	return normalized


def wrap_dispatch(payload: dict[str, Any]) -> dict[str, Any]:
	"""Build the ``repository_dispatch`` request body for a report.

	The report travels one level down under ``report`` so ``client_payload``
	stays within GitHub's top-level property limit
	(``DISPATCH_CLIENT_PAYLOAD_MAX_KEYS``).
	"""
	return {
		"event_type": DISPATCH_EVENT_TYPE,
		"client_payload": {"schema_version": SCHEMA_VERSION, "report": payload},
	}


def unwrap_dispatch(client_payload: Any) -> Any:
	"""Return the report inside an enveloped ``client_payload``.

	An envelope is a dict whose ``schema_version`` matches and whose ``report``
	is a dict. Anything else (the legacy flat report, or garbage that
	``validate_payload`` rejects later) is returned unchanged.
	"""
	if (
		isinstance(client_payload, dict)
		and client_payload.get("schema_version") == SCHEMA_VERSION
		and isinstance(client_payload.get("report"), dict)
	):
		return client_payload["report"]
	return client_payload


def skip_reason(payload: dict[str, Any], *, registered_repos: Iterable[str], self_repo: str) -> str:
	"""Return a stable skip reason when a validated payload must not be healed."""
	repo = payload.get("source_repo")
	if repo != self_repo and repo not in set(registered_repos):
		return "unregistered_source_repo"
	title = payload.get("issue_title") or ""
	if _SMOKE_TITLE_RE.search(title):
		return "smoke_test_fixture"
	if _SMOKE_LABELS.intersection(payload.get("labels") or []):
		return "smoke_test_fixture"
	workflow_name = payload.get("workflow_name") or ""
	if SELF_WORKFLOW_FRAGMENT.lower() in workflow_name.lower():
		return "self_workflow"
	return ""


# ---------------------------------------------------------------------------
# Logs, signature, fingerprint
# ---------------------------------------------------------------------------


def _drop_step_script_lines(text: str) -> str:
	"""Remove the echoed step script from raw Actions job-log text.

	Only ANSI-cyan lines inside a ``##[group]Run`` header block are dropped;
	the header line itself, the ``shell:`` / ``env:`` block, and every line of
	real step output are kept. Text without such headers (reporter evidence,
	already-sanitised logs) is returned unchanged.
	"""
	kept: list[str] = []
	in_header = False
	for line in text.split("\n"):
		content = _LOG_TIMESTAMP_RE.sub("", line)
		if _STEP_HEADER_OPEN_RE.match(content):
			in_header = True
		elif in_header and _STEP_HEADER_CLOSE_RE.match(content):
			in_header = False
		elif in_header and content.startswith(_STEP_SCRIPT_LINE_PREFIX):
			continue
		kept.append(line)
	return "\n".join(kept)


def filter_log(text: str, *, max_lines: int = 400, max_bytes: int = 60_000) -> str:
	"""Keep the high-signal lines plus the tail of a job log, bounded.

	The echoed step script is dropped first (it has to be, before ANSI codes
	are stripped), so the tail and the high-signal matches cover what the
	steps printed rather than their source.
	"""
	lines = sanitize_text(_drop_step_script_lines(text)).split("\n")
	kept: list[str] = []
	seen: set[int] = set()
	tail_start = max(0, len(lines) - max_lines)
	for index, line in enumerate(lines):
		if index >= tail_start or _SOFT_LOG_PATTERNS.search(line):
			if index not in seen:
				seen.add(index)
				kept.append(line)
	result = "\n".join(kept)
	encoded = result.encode("utf-8")
	if len(encoded) > max_bytes:
		result = encoded[-max_bytes:].decode("utf-8", errors="ignore")
		result = "[... truncated ...]\n" + result
	return result


def normalize_error_line(line: str) -> str:
	text = _LOG_TIMESTAMP_RE.sub("", sanitize_text(line)).strip().lower()
	text = re.sub(r"https?://\S+", "<url>", text)
	text = re.sub(r"\b[0-9a-f]{40}\b", "<sha>", text)
	text = re.sub(r"\b[0-9a-f]{64}\b", "<hash>", text)
	text = re.sub(r"\b[0-9a-f]{7,12}\b", "<short>", text)
	text = re.sub(r"/tmp/[^\s]+", "<tmp>", text)
	text = re.sub(r"\d+", "<n>", text)
	text = " ".join(text.split())
	return text


def error_signature(text: str) -> str:
	"""Derive a stable, volatile-token-free error signature from a log."""
	lines = [line for line in sanitize_text(text).split("\n") if line.strip()]
	for pattern in _SIGNATURE_PATTERNS:
		matched = [normalize_error_line(line) for line in lines if pattern.search(_LOG_TIMESTAMP_RE.sub("", line))]
		matched = [line for line in matched if line]
		if matched:
			unique: list[str] = []
			for line in matched:
				if line not in unique:
					unique.append(line)
				if len(unique) >= SIGNATURE_LINE_LIMIT:
					break
			return " | ".join(unique)[:SIGNATURE_CHAR_LIMIT]
	return "no-error-lines"


def fingerprint(workflow_name: str, failing_step: str, signature: str) -> str:
	# The per-cycle `[cycle:<id>]` run-name suffix is dropped so every promote
	# cycle that fails the same way shares one fingerprint (and one lineage).
	stable_workflow_name = _CYCLE_RUN_NAME_SUFFIX_RE.sub("", single_line(workflow_name, 200))
	material = "|".join(
		[
			stable_workflow_name.lower(),
			single_line(failing_step, 200).lower(),
			signature[:SIGNATURE_CHAR_LIMIT],
		]
	)
	return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Identical-failure fingerprint cap (review_autofix.yml)
# ---------------------------------------------------------------------------


def safe_token(value: Any, limit: int = 80) -> str:
	"""Reduce a value to ``[A-Za-z0-9_.-]`` so it is safe in a log line or marker."""
	return _UNSAFE_TOKEN_RE.sub("", str(value or ""))[:limit]


def read_failure_evidence(paths: Iterable[str]) -> str:
	"""Join the bounded tails of the stage stderr files that exist.

	Every call site of the review workflow passes the same file list, so the
	fingerprint of one run's failure is identical in every comment it posts.
	Missing or empty files are skipped.
	"""
	chunks: list[str] = []
	for path in paths:
		if not path:
			continue
		try:
			data = Path(path).read_bytes()
		except OSError:
			continue
		if not data:
			continue
		chunks.append(data[-FAILURE_EVIDENCE_TAIL_BYTES:].decode("utf-8", errors="replace"))
	return "\n".join(chunks)


def derive_autofix_failure_reason(flags: dict[str, str], finalize_reason: str = "") -> str:
	"""Name a review/autofix failure from the run flags.

	Same precedence as ``workflow_failure_heal_autofix_report.sh`` (tests pin
	the parity): an explicit ``AUTOFIX_FAILURE_REASON``, then the editor flags,
	then the run summary's ``finalize_reason``, then ``workflow_failure``.
	"""
	explicit = str(flags.get("AUTOFIX_FAILURE_REASON") or "")
	if _FAILURE_REASON_RE.match(explicit):
		return explicit
	if flags.get("AUTOFIX_EDITOR_EMPTY_NOOP") == "true":
		return "editor_empty_noop"
	if flags.get("EDITOR_CHANGES_LOST") == "true":
		return "editor_changes_lost"
	if flags.get("EDITOR_NOOP_REFUSAL") == "true":
		return "editor_refusal"
	if _FAILURE_REASON_RE.match(finalize_reason or ""):
		return finalize_reason
	return "workflow_failure"


def _finalize_reason_from_summary_line(path: str) -> str:
	try:
		text = Path(path).read_text(encoding="utf-8", errors="replace")
	except OSError:
		return ""
	for line in text.splitlines():
		if line.startswith("REVIEW_AUTOFIX_RUN_SUMMARY_V1 "):
			try:
				summary = json.loads(line[len("REVIEW_AUTOFIX_RUN_SUMMARY_V1 "):])
			except json.JSONDecodeError:
				return ""
			return str(summary.get("finalize_reason") or "") if isinstance(summary, dict) else ""
	return ""


def autofix_failure_fingerprint(*, failure_reason: str, evidence_text: str) -> dict[str, Any]:
	"""Fingerprint one review/autofix failure from its reason and evidence.

	Empty evidence is ``degraded``: the signature collapses to the constant
	``error_signature("")`` value, so the fingerprint compares head + reason only.
	"""
	return {
		"fp": fingerprint(FAILURE_FINGERPRINT_WORKFLOW, failure_reason, error_signature(evidence_text)),
		"degraded": evidence_text.strip() == "",
	}


def render_failure_marker(
	head_sha: str,
	failure_reason: str,
	fp: str,
	degraded: bool,
	run_id: str | None = None,
	engine_sha: str | None = None,
) -> str:
	"""Render the ``review-autofix-failure:v1`` marker appended to a failure comment.

	``run`` lets the counter treat two failure comments of one run as a single
	failure. Returns an empty string when the head or fingerprint is malformed.
	"""
	head = str(head_sha or "").strip().lower()
	if not is_valid_sha(head) or not _FP_HEX_RE.match(str(fp or "")):
		return ""
	fields = [f"head={head}", f"reason={safe_token(failure_reason) or 'unknown'}", f"fp={fp}", f"degraded={1 if degraded else 0}"]
	engine = str(engine_sha or "").strip().lower()
	if is_valid_sha(engine):
		fields.append(f"engine={engine}")
	run = safe_token(run_id, 20)
	if run.isdigit():
		fields.append(f"run={run}")
	return f"<!-- {FAILURE_MARKER_TAG} " + " ".join(fields) + " -->"


def _marker_fields(match: re.Match[str] | None) -> dict[str, str]:
	fields: dict[str, str] = {}
	if match is None:
		return fields
	for field in _MARKER_FIELD_RE.finditer(match.group("fields")):
		fields.setdefault(field.group("key"), safe_token(field.group("value"), 100))
	return fields


def _comment_author(comment: dict[str, Any]) -> str:
	login = comment.get("author_login")
	if not login and isinstance(comment.get("user"), dict):
		login = comment["user"].get("login")
	return str(login or "").strip().lower()


def parse_failure_markers(comments: Iterable[dict[str, Any]], *, head_sha: str, author_login: str) -> list[dict[str, Any]]:
	"""Return the trusted failure markers for ``head_sha``, oldest first.

	A marker is trusted only when its comment was written by ``author_login``
	(the identity the workflow posts as); markers from anyone else are ignored.
	"""
	head = str(head_sha or "").strip().lower()
	author = str(author_login or "").strip().lower()
	markers: list[dict[str, Any]] = []
	if not head or not author:
		return markers
	for comment in comments:
		if not isinstance(comment, dict) or _comment_author(comment) != author:
			continue
		fields = _marker_fields(_FAILURE_MARKER_RE.search(sanitize_text(comment.get("body"))))
		if fields.get("head", "").lower() != head or not _FP_HEX_RE.match(fields.get("fp", "")):
			continue
		markers.append(
			{
				"fp": fields["fp"],
				"reason": fields.get("reason") or "unknown",
				"degraded": fields.get("degraded") == "1",
				"engine": fields.get("engine", "").lower() if is_valid_sha(fields.get("engine", "").lower()) else "",
				"run": fields.get("run", ""),
				"comment_id": safe_token(comment.get("id"), 20),
			}
		)
	return markers


def count_identical_failures(
	comments: Iterable[dict[str, Any]],
	*,
	head_sha: str,
	author_login: str,
	engine_sha: str | None = None,
) -> dict[str, Any]:
	"""Count the trailing identical failures on ``head_sha``.

	``comments`` is the PR's issue-comment list, oldest first. Scanning from the
	newest comment, every trusted marker for the head whose ``fp`` equals the
	newest marker's ``fp`` adds one (several comments of one run count once).
	A trusted marker with a different ``fp``, a failure comment without a
	marker, or an editor summary ends the scan; the summary a post-editor
	failure of the same run posted first does not (same pairing rule as
	``count_autofix_failure_streak``). Markers for another head or from another
	author are skipped. ``cap_applied`` reports whether a trusted
	``review-autofix-failure-cap:v1`` marker already exists for the head and,
	when supplied, the selected helper-engine commit. Degraded markers remain
	diagnostic only and terminate the trailing deterministic-failure sequence.
	"""
	head = str(head_sha or "").strip().lower()
	author = str(author_login or "").strip().lower()
	engine = str(engine_sha or "").strip().lower()
	if not is_valid_sha(engine):
		engine = ""
	ordered = [comment for comment in comments if isinstance(comment, dict)]
	result: dict[str, Any] = {"count": 0, "fp": "", "reason": "", "cap_applied": False}
	if not head or not author:
		return result
	for comment in ordered:
		if _comment_author(comment) != author:
			continue
		cap_fields = _marker_fields(_FAILURE_CAP_MARKER_RE.search(sanitize_text(comment.get("body"))))
		cap_engine = cap_fields.get("engine", "").lower()
		if cap_fields.get("head", "").lower() == head and (not engine or cap_engine == engine):
			result["cap_applied"] = True
			break
	seen_runs: set[str] = set()
	skip_paired_summary = False
	for comment in reversed(ordered):
		body = sanitize_text(comment.get("body"))
		match = _FAILURE_MARKER_RE.search(body)
		if match is not None:
			fields = _marker_fields(match)
			if _comment_author(comment) != author or fields.get("head", "").lower() != head or not _FP_HEX_RE.match(fields.get("fp", "")):
				continue
			if engine and fields.get("engine", "").lower() != engine:
				continue
			skip_paired_summary = any(marker in body for marker in AUTOFIX_POST_SUMMARY_FAILURE_COMMENT_MARKERS)
			if fields.get("degraded") == "1":
				if not result["fp"]:
					result["fp"] = fields["fp"]
					result["reason"] = fields.get("reason") or "unknown"
				break
			run = fields.get("run", "")
			if run and run in seen_runs:
				continue
			if not result["fp"]:
				result["fp"] = fields["fp"]
				result["reason"] = fields.get("reason") or "unknown"
			elif fields["fp"] != result["fp"]:
				break
			result["count"] += 1
			if run:
				seen_runs.add(run)
			continue
		if any(marker in body for marker in AUTOFIX_FAILURE_COMMENT_MARKERS):
			break
		if any(marker in body for marker in AUTOFIX_SUCCESS_COMMENT_MARKERS):
			if skip_paired_summary:
				skip_paired_summary = False
				continue
			break
	return result


# ---------------------------------------------------------------------------
# Dedup / lineage / budget
# ---------------------------------------------------------------------------


def budget_decision(
	issues: Iterable[dict[str, Any]],
	*,
	fp: str,
	preferred_repo: str | None = None,
	source_gen: int | None = None,
	source_root: str | None = None,
	max_depth: int = DEFAULT_MAX_LINEAGE_DEPTH,
	max_open: int = DEFAULT_MAX_OPEN_ISSUES,
	max_per_day: int = DEFAULT_MAX_ISSUES_PER_DAY,
	now: datetime | None = None,
) -> dict[str, Any]:
	"""Decide what to do with a fingerprinted failure given the heal issue list.

	``issues`` is the GitHub issue list for label ``ai:workflow-heal`` in any
	state (pull requests are ignored). Decision order: duplicate (an open heal
	issue already carries this fingerprint) → escalate (lineage cap) →
	budget_exhausted (open-issue or per-day cap) → open.
	"""
	now = now or _utc_now()
	day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
	open_issues: list[dict[str, Any]] = []
	created_today = 0
	prior_same_fp: list[tuple[int, int, str, str]] = []
	duplicate: dict[str, Any] | None = None
	for issue in issues:
		if not isinstance(issue, dict) or issue.get("pull_request"):
			continue
		number = _positive_int(issue.get("number"))
		if number is None:
			continue
		markers = parse_heal_markers(issue.get("body"))
		issue_repository = issue.get("repository") if is_valid_repo_slug(issue.get("repository")) else ""
		state = str(issue.get("state") or "").lower()
		created = _parse_iso(issue.get("created_at"))
		if created is not None and created >= day_start:
			created_today += 1
		if state == "open":
			open_issues.append(issue)
			if markers.get("fp") == fp:
				if duplicate is None:
					duplicate = issue
				else:
					duplicate_repository = duplicate.get("repository") if is_valid_repo_slug(duplicate.get("repository")) else ""
					candidate_preferred_repo = bool(preferred_repo and issue_repository == preferred_repo)
					duplicate_preferred_repo = bool(preferred_repo and duplicate_repository == preferred_repo)
					if candidate_preferred_repo and not duplicate_preferred_repo:
						duplicate = issue
					elif candidate_preferred_repo == duplicate_preferred_repo and issue_repository == duplicate_repository and number > (_positive_int(duplicate.get("number")) or 0):
						duplicate = issue
		elif markers.get("fp") == fp:
			gen = _positive_int(markers.get("gen")) or 1
			prior_same_fp.append((gen, number, markers.get("root") or fp, issue_repository))

	if duplicate is not None:
		return {
			"action": "duplicate",
			"existing_issue": _positive_int(duplicate.get("number")),
			"existing_url": sanitize_text(duplicate.get("html_url"), 300),
			"existing_repo": duplicate.get("repository") if is_valid_repo_slug(duplicate.get("repository")) else "",
			"gen": _positive_int(parse_heal_markers(duplicate.get("body")).get("gen")) or 1,
			"root": parse_heal_markers(duplicate.get("body")).get("root") or fp,
			"open_count": len(open_issues),
			"today_count": created_today,
		}

	gen = 1
	root = fp
	prior_issue: int | None = None
	prior_repo = ""
	if source_gen is not None:
		gen = source_gen + 1
		root = source_root or fp
	elif prior_same_fp:
		prior_same_fp.sort()
		prior_gen, prior_issue, prior_root, prior_repo = prior_same_fp[-1]
		gen = prior_gen + 1
		root = prior_root
	if gen > max_depth:
		return {
			"action": "escalate",
			"reason": "lineage_cap",
			"gen": gen,
			"root": root,
			"prior_issue": prior_issue,
			"prior_repo": prior_repo,
			"open_count": len(open_issues),
			"today_count": created_today,
		}
	if len(open_issues) >= max_open:
		return {"action": "budget_exhausted", "reason": "max_open_issues", "gen": gen, "root": root, "open_count": len(open_issues), "today_count": created_today}
	if created_today >= max_per_day:
		return {"action": "budget_exhausted", "reason": "max_issues_per_day", "gen": gen, "root": root, "open_count": len(open_issues), "today_count": created_today}
	return {"action": "open", "gen": gen, "root": root, "open_count": len(open_issues), "today_count": created_today}


# ---------------------------------------------------------------------------
# Diagnosis parsing + issue composition
# ---------------------------------------------------------------------------


def parse_classification(markdown: str) -> str:
	"""Read the ``## Classification`` section; default to ``inconclusive``."""
	text = sanitize_text(markdown)
	match = _CLASSIFICATION_RE.search(text)
	if match:
		rest = text[match.end():]
		for line in rest.split("\n"):
			token = line.strip().strip("`*-: ").lower()
			if not token:
				continue
			if token.startswith("##"):
				break
			for candidate in CLASSIFICATIONS:
				if token == candidate or token.startswith(candidate):
					return candidate
			break
	return "inconclusive"


def _context_lines(payload: dict[str, Any], *, run_summaries: list[dict[str, Any]]) -> list[str]:
	lines = [f"- **Source repository:** `{payload['source_repo']}`"]
	kind = payload.get("source_kind")
	if payload.get("issue_number"):
		noun = "Pull request" if kind in ("pull_request", "autofix_failure") else "Issue"
		url = payload.get("issue_url") or f"https://github.com/{payload['source_repo']}/issues/{payload['issue_number']}"
		lines.append(f"- **Source {noun.lower()}:** {url} ({payload['source_repo']}#{payload['issue_number']})")
		if kind == "autofix_failure":
			lines.append(f"- **Failure reason:** `{payload.get('failure_reason')}`")
			lines.append(f"- **Consecutive failed review runs on this PR:** {payload.get('failure_streak') or 1}")
		else:
			lines.append(f"- **Escalation label:** `{payload.get('label')}`")
	if payload.get("workflow_name"):
		lines.append(f"- **Failed workflow:** `{payload['workflow_name']}` (conclusion: `{payload.get('conclusion') or 'unknown'}`)")
	if payload.get("head_branch"):
		lines.append(f"- **Failed on branch:** `{payload['head_branch']}`")
	if payload.get("head_sha"):
		lines.append(f"- **Head SHA:** `{payload['head_sha']}`")
	if payload.get("wrapper_sha"):
		lines.append(f"- **Consumer wrapper pin (coding-workflows release SHA):** `{payload['wrapper_sha']}`")
	if payload.get("base_branch"):
		lines.append(f"- **Pull request base branch:** `{payload['base_branch']}`")
	if payload.get("crash_file"):
		lines.append(f"- **Crash file:** `{payload['crash_file']}`")
	for summary in run_summaries:
		step = summary.get("failing_step") or "unknown step"
		lines.append(f"- **Failed run:** {summary.get('url')} — workflow `{summary.get('workflow_name') or 'unknown'}`, step `{step}`")
	return lines


def compose_issue_title(payload: dict[str, Any], *, workflow_name: str | None) -> str:
	name = single_line(workflow_name or payload.get("workflow_name") or "", 120)
	if payload.get("source_kind") == "workflow_run":
		return f"Workflow heal: {name or 'release workflow'} failed on {payload.get('head_branch') or 'unknown branch'}"
	if payload.get("source_kind") == "autofix_failure":
		target = f"{payload['source_repo']}#{payload.get('issue_number')}"
		streak = payload.get("failure_streak") or 1
		return f"Workflow heal: {name or 'review/autofix'} failed {streak}x for {target} ({payload.get('failure_reason')})"
	label = payload.get("label") or "escalation"
	target = f"{payload['source_repo']}#{payload.get('issue_number')}"
	if name:
		return f"Workflow heal: {name} failed for {target} ({label})"
	return f"Workflow heal: {label} on {target}"


def compose_issue_body(
	*,
	payload: dict[str, Any],
	diagnosis: str,
	fp: str,
	gen: int,
	root: str,
	classification: str,
	target_branch: str | None,
	max_depth: int,
	intake_run_url: str,
	run_summaries: list[dict[str, Any]],
	integration_branch: str | None = None,
) -> str:
	"""Compose the heal issue body (upstream or consumer-side).

	``integration_branch`` (optional) adds the orchestrator lineage lines a
	``base-self-inflicted`` issue on an ``orchestrator/project-<N>`` branch
	carries so plan and implement resolve that branch: ``Tracking issue: #N``,
	``Integration branch:`` and ``Refs #N`` (never an auto-close keyword,
	CLAUDE.md §19). Other branches add nothing.
	"""
	parts = [
		f"<!-- {MARKER_PREFIX}fp={fp} -->",
		f"<!-- {MARKER_PREFIX}gen={gen} -->",
		f"<!-- {MARKER_PREFIX}root={root} -->",
		f"<!-- {MARKER_PREFIX}source={payload['source_repo']}#{payload.get('issue_number') or 'run'} -->",
		f"<!-- {MARKER_PREFIX}classification={classification} -->",
		"",
	]
	if target_branch:
		parts.append(f"- **Target branch:** `{target_branch}`")
	tracking_issue = orchestrator_tracking_issue(integration_branch)
	if tracking_issue:
		parts.append(f"- **Tracking issue:** #{tracking_issue}")
		parts.append(f"- **Integration branch:** `{integration_branch}`")
	if target_branch or tracking_issue:
		parts.append("")
	parts.append(f"## Automated workflow failure heal (generation {gen} of max {max_depth})")
	parts.append("")
	if classification == "base-self-inflicted":
		base = payload.get("base_branch") or target_branch or "the base branch"
		parts.append(
			"The review/autofix workflow failed repeatedly on one pull request of this repository. The "
			f"crash is in a file the pull request's base branch `{base}` changed relative to `main`, and "
			"the pull request itself did not touch it, so this issue was filed automatically for the "
			f"clarify -> plan -> implement -> review pipeline to fix it on `{base}`."
		)
		if tracking_issue:
			parts.append("")
			parts.append(f"Refs #{tracking_issue}")
	elif classification in UPSTREAM_ISSUE_CLASSIFICATIONS:
		if payload.get("source_kind") == "workflow_run":
			intro = (
				"A release / promotion workflow run failed. This issue was filed automatically for the "
				"clarify -> plan -> implement -> review pipeline to fix the cause."
			)
		elif payload.get("source_kind") == "autofix_failure":
			intro = (
				"The review/autofix workflow failed repeatedly on one pull request, past the retry the "
				"stall poller already gives it. The diagnosis below attributes it to the shared workflow "
				"source, so this issue was filed automatically for the clarify -> plan -> implement -> "
				"review pipeline to fix it here."
			)
		else:
			intro = (
				"A pipeline failure in a consumer of these workflows was escalated for human attention. "
				"The diagnosis below attributes it to the shared workflow source, so this issue was filed "
				"automatically for the clarify -> plan -> implement -> review pipeline to fix it here."
			)
		parts.append(intro)
		if classification == "inconclusive":
			parts.append("")
			parts.append(
				"The automated diagnosis was **inconclusive**. Treat the evidence below as the starting point, "
				"verify the root cause against the logs and the source at the pinned SHA, and ask clarifying "
				"questions if the fix is not verifiable."
			)
	else:
		parts.append(
			"A pipeline failure was escalated for human attention. The diagnosis attributes it to this "
			"repository's own code or configuration rather than to the shared workflow source, so this "
			"issue was filed here for the normal clarify -> plan -> implement -> review pipeline."
		)
	parts.append("")
	parts.extend(_context_lines(payload, run_summaries=run_summaries))
	parts.append(f"- **Classification:** `{classification}`")
	parts.append(f"- **Heal intake run:** {intake_run_url}")
	parts.append("")
	if payload.get("source_kind") == "autofix_failure" and payload.get("failure_evidence"):
		parts.append("<details><summary>Failure evidence from the reporting run (UNTRUSTED, verbatim)</summary>")
		parts.append("")
		parts.append("```")
		parts.append(sanitize_text(payload["failure_evidence"], FAILURE_EVIDENCE_LIMIT).replace("```", "` ` `"))
		parts.append("```")
		parts.append("")
		parts.append("</details>")
		parts.append("")
	parts.append("---")
	parts.append("")
	parts.append(sanitize_text(diagnosis).strip() or "_(no diagnosis produced)_")
	parts.append("")
	parts.append("---")
	parts.append("")
	parts.append("## Fix constraints")
	parts.append("")
	parts.append("- Keep the change minimal and backward compatible; never rename or remove existing identifiers, env vars, labels, or log prefixes.")
	parts.append("- Do not disable, skip, or weaken the failing check, guard, or escalation to make the symptom disappear.")
	parts.append("- If the evidence points to a transient or environmental cause, say so in the plan instead of inventing a code fix.")
	parts.append("")
	parts.append(
		f"_Filed by the workflow failure heal intake. Lineage generation {gen} (cap {max_depth}); "
		"the chain escalates to a human at the cap. Re-reports with the same fingerprint are recorded "
		"as occurrence comments on this issue while it stays open._"
	)
	return "\n".join(parts) + "\n"


def compose_occurrence_comment(payload: dict[str, Any], *, intake_run_url: str) -> str:
	lines = [f"<!-- {MARKER_PREFIX}occurrence -->", "Another occurrence of this failure was reported:", ""]
	lines.extend(_context_lines(payload, run_summaries=[]))
	for ref in payload.get("run_refs") or []:
		lines.append(f"- **Failed run:** {ref['url']}")
	lines.append(f"- **Heal intake run:** {intake_run_url}")
	return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Branch progress + earlier heals: "is this already fixed?" context
# ---------------------------------------------------------------------------


def summarize_branch_progress(compare: Any, *, failed_sha: str, branch: str) -> dict[str, Any]:
	"""Reduce a GitHub compare response to what the diagnosis model needs.

	``compare`` is the raw ``GET repos/<repo>/compare/<failed_sha>...<branch>``
	body (untrusted: every field is re-validated). Returns ``{"available":
	True, branch, failed_sha, status, ahead_by, tip_sha, commits: [{sha,
	subject}] oldest first, files: [path], commits_truncated, files_truncated}``
	or ``{"available": False, "reason": ...}`` when the response is unusable.
	"""
	if not is_valid_sha(failed_sha) or not is_valid_branch(branch):
		return {"available": False, "reason": "invalid_reference"}
	if not isinstance(compare, dict):
		return {"available": False, "reason": "invalid_compare_response"}
	ahead_by = compare.get("ahead_by")
	status = compare.get("status")
	if not isinstance(ahead_by, int) or isinstance(ahead_by, bool) or ahead_by < 0 or status not in ("identical", "ahead", "behind", "diverged"):
		return {"available": False, "reason": "invalid_compare_response"}
	commits: list[dict[str, str]] = []
	for item in compare.get("commits") or []:
		if not isinstance(item, dict):
			continue
		sha = str(item.get("sha") or "").lower()
		if not is_valid_sha(sha):
			continue
		message = (item.get("commit") or {}).get("message") if isinstance(item.get("commit"), dict) else ""
		commits.append({"sha": sha, "subject": single_line(str(message or "").split("\n", 1)[0], 160)})
	commits = commits[-BRANCH_PROGRESS_COMMIT_LIMIT:]
	files: list[str] = []
	for item in compare.get("files") or []:
		if isinstance(item, dict) and isinstance(item.get("filename"), str):
			files.append(single_line(item["filename"], 300))
		if len(files) >= BRANCH_PROGRESS_FILE_LIMIT:
			break
	tip_sha = commits[-1]["sha"] if commits else failed_sha
	return {
		"available": True,
		"branch": branch,
		"failed_sha": failed_sha,
		"status": status,
		"ahead_by": ahead_by,
		"tip_sha": tip_sha,
		"commits": commits,
		"files": files,
		"commits_truncated": ahead_by > len(commits),
		"files_truncated": len(compare.get("files") or []) > len(files),
	}


def render_branch_progress(summary: dict[str, Any]) -> str:
	"""Render the branch-progress block for the diagnosis prompt."""
	if not isinstance(summary, dict) or not summary.get("available"):
		reason = summary.get("reason") if isinstance(summary, dict) else "unavailable"
		return (
			f"(branch progress unavailable: {single_line(reason or 'unavailable', 80)}. "
			"Do not classify as `already-fixed` without it.)\n"
		)
	branch = summary["branch"]
	failed = summary["failed_sha"]
	ahead = summary["ahead_by"]
	lines = [f"Failing SHA: {failed}", f"Branch: {branch}  Tip: {summary['tip_sha']}  Compare status: {summary['status']}"]
	if ahead == 0:
		lines.append(f"`{branch}` has no commits after the failing SHA: the code that failed is still the current code.")
		return "\n".join(lines) + "\n"
	lines.append(
		f"`{branch}` has {ahead} commit(s) after the failing SHA. HEAL_SOURCE_DIR holds the failing SHA and "
		f"HEAL_BRANCH_TIP_DIR holds `{branch}` at its tip (paths in FAILURE CONTEXT); diff the two to see what changed."
	)
	lines.append("")
	shown = summary["commits"][-BRANCH_PROGRESS_COMMITS_SHOWN:]
	hidden = ahead - len(shown)
	lines.append(f"Commits after the failing SHA (oldest first{f', {hidden} older not shown' if hidden > 0 else ''}):")
	for commit in shown:
		lines.append(f"- {commit['sha'][:12]} {commit['subject']}")
	lines.append("")
	files = summary["files"][:BRANCH_PROGRESS_FILES_SHOWN]
	more = "" if len(files) == len(summary["files"]) and not summary.get("files_truncated") else " (list truncated)"
	lines.append(f"Files changed on `{branch}` since the failing SHA{more}:")
	lines.extend(f"- {path}" for path in files)
	return "\n".join(lines) + "\n"


def _markdown_section(markdown: str, heading: str, limit: int) -> str:
	"""Return the body of ``## <heading>`` (up to the next ``## ``), bounded."""
	match = re.search(r"^##\s*" + re.escape(heading) + r"\s*$", markdown, re.IGNORECASE | re.MULTILINE)
	if not match:
		return ""
	rest = markdown[match.end():]
	end = re.search(r"^##\s", rest, re.MULTILINE)
	return sanitize_text((rest[: end.start()] if end else rest).strip(), limit)


def render_heal_lineage_context(issues: Iterable[dict[str, Any]], *, fp: str, root: str) -> str:
	"""Render earlier heal issues that share this fingerprint or lineage root.

	``issues`` is the same ``ai:workflow-heal`` issue list the budget decision
	reads (no extra API call); each item may carry ``title``, ``closed_at`` and
	``state_reason``. Newest first, at most HEAL_LINEAGE_CONTEXT_LIMIT, each
	with its bounded ``Root cause`` and ``Suggested fix`` sections.
	"""
	related: list[tuple[int, dict[str, Any]]] = []
	for issue in issues:
		if not isinstance(issue, dict) or issue.get("pull_request"):
			continue
		number = _positive_int(issue.get("number"))
		if number is None:
			continue
		markers = parse_heal_markers(issue.get("body"))
		if markers.get("fp") == fp or (root and markers.get("root") == root):
			related.append((number, issue))
	if not related:
		return "(no earlier heal issue shares this failure's fingerprint or lineage)\n"
	related.sort(key=lambda pair: pair[0], reverse=True)
	lines: list[str] = []
	for number, issue in related[:HEAL_LINEAGE_CONTEXT_LIMIT]:
		body = sanitize_text(issue.get("body"))
		markers = parse_heal_markers(body)
		state = single_line(issue.get("state") or "unknown", 20)
		reason = single_line(issue.get("state_reason") or "", 30)
		closed = single_line(issue.get("closed_at") or "", 40)
		status = state + (f" ({reason})" if reason else "") + (f", closed {closed}" if closed else "")
		repo = issue.get("repository") if is_valid_repo_slug(issue.get("repository")) else ""
		ref = f"{repo}#{number}" if repo else f"#{number}"
		lines.append(f"--- {ref} [{status}] gen {markers.get('gen') or '1'}: {single_line(issue.get('title') or '', 200)} ---")
		lines.append(f"Opened: {single_line(issue.get('created_at') or 'unknown', 40)}  URL: {sanitize_text(issue.get('html_url'), 300)}")
		for heading in ("Root cause", "Suggested fix"):
			excerpt = _markdown_section(body, heading, HEAL_LINEAGE_EXCERPT_LIMIT)
			if excerpt:
				lines.append(f"{heading}:")
				lines.append(excerpt)
		lines.append("")
	return "\n".join(lines).rstrip("\n") + "\n"


def check_heal_already_fixed_claim(diagnosis: str, summary: dict[str, Any]) -> dict[str, Any]:
	"""Verify an ``already-fixed`` diagnosis against the branch progress.

	The claim stands only when the branch has commits after the failing SHA
	and the ``## Fixed by`` section cites at least one of them (a 7-40 hex
	prefix of a commit in ``summary['commits']``). Returns ``{"ok": bool,
	"reason": str, "commits": [full shas cited and found]}``.
	"""
	if not isinstance(summary, dict) or not summary.get("available"):
		return {"ok": False, "reason": "branch_progress_unavailable", "commits": []}
	if int(summary.get("ahead_by") or 0) <= 0:
		return {"ok": False, "reason": "no_commits_after_failing_sha", "commits": []}
	text = sanitize_text(diagnosis)
	match = _HEAL_FIXED_BY_SECTION_RE.search(text)
	if not match:
		return {"ok": False, "reason": "fixed_by_section_missing", "commits": []}
	rest = text[match.end():]
	end = re.search(r"^##\s", rest, re.MULTILINE)
	section = rest[: end.start()] if end else rest
	known = [commit["sha"] for commit in summary.get("commits") or [] if isinstance(commit, dict) and is_valid_sha(commit.get("sha"))]
	cited: list[str] = []
	for token in _HEAL_COMMIT_REF_RE.findall(section.lower()):
		for sha in known:
			if sha.startswith(token) and sha not in cited:
				cited.append(sha)
	if not cited:
		return {"ok": False, "reason": "fixed_by_commit_not_after_failing_sha", "commits": []}
	return {"ok": True, "reason": "fixed_by_commit_verified", "commits": cited}


def _write_json(value: Any) -> None:
	sys.stdout.write(json.dumps(value, sort_keys=True))
	sys.stdout.write("\n")


def _cmd_build_issue_payload(args: argparse.Namespace) -> int:
	issue = _load_json_file(args.issue_json)
	comments = _load_json_file(args.comments_json) if args.comments_json else []
	runs = _load_json_file(args.runs_json) if args.runs_json else []
	if isinstance(runs, dict):
		runs = runs.get("workflow_runs") or []
	payload = build_issue_payload(
		repo=args.repo,
		kind=args.kind,
		label=args.label,
		issue=issue if isinstance(issue, dict) else {},
		comments=comments if isinstance(comments, list) else [],
		runs=runs if isinstance(runs, list) else [],
		wrapper_sha=args.wrapper_sha or None,
		reporter_run_url=args.reporter_run_url or None,
	)
	validate_payload(payload)
	encoded = json.dumps(payload)
	if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
		payload["comments_excerpt"] = sanitize_text(payload["comments_excerpt"], 1500)
		payload["issue_excerpt"] = sanitize_text(payload["issue_excerpt"], 1500)
	_write_json(payload)
	return 0


def _cmd_build_autofix_payload(args: argparse.Namespace) -> int:
	pr = _load_json_file(args.pr_json)
	comments = _load_json_file(args.comments_json) if args.comments_json else []
	evidence = Path(args.failure_evidence_file).read_text(encoding="utf-8", errors="replace") if args.failure_evidence_file else ""
	payload = build_autofix_failure_payload(
		repo=args.repo,
		pr=pr if isinstance(pr, dict) else {},
		comments=comments if isinstance(comments, list) else [],
		workflow_name=args.workflow_name,
		failure_reason=args.failure_reason,
		failure_evidence=evidence,
		failure_streak=args.failure_streak,
		run_id=args.run_id,
		run_url=args.run_url or None,
		wrapper_sha=args.wrapper_sha or None,
		reporter_run_url=args.reporter_run_url or None,
		failure_fingerprint=args.failure_fingerprint or None,
		base_branch=args.base_branch or None,
		script_ref=args.script_ref or None,
		changed_files=_read_path_list(args.changed_files_file),
	)
	validate_payload(payload)
	if len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
		payload["comments_excerpt"] = sanitize_text(payload["comments_excerpt"], 1500)
		payload["issue_excerpt"] = sanitize_text(payload["issue_excerpt"], 1500)
		payload["failure_evidence"] = sanitize_text(payload["failure_evidence"], 2000)
	_write_json(payload)
	return 0


def _read_path_list(path: str | None) -> list[str]:
	"""One repo-relative path per line; a missing or unreadable file is an empty list."""
	if not path:
		return []
	try:
		text = Path(path).read_text(encoding="utf-8", errors="replace")
	except OSError:
		return []
	return [line.strip() for line in text.splitlines() if line.strip()]


def _cmd_classify_crash_ownership(args: argparse.Namespace) -> int:
	payload = validate_payload(_load_json_file(args.payload_json))
	ownership = classify_crash_ownership(
		crash_file=payload.get("crash_file"),
		changed_files=payload.get("changed_files") or [],
		# The intake's own git diff: not capped like the reported PR list, since a
		# long-lived integration branch can differ from main in hundreds of files.
		base_changed_files=[path for path in _read_path_list(args.base_changed_files) if is_valid_repo_path(path)],
	)
	sys.stdout.write(ownership + "\n")
	return 0


def _cmd_autofix_failure_streak(args: argparse.Namespace) -> int:
	try:
		comments = _load_json_file(args.comments_json)
	except (OSError, json.JSONDecodeError):
		comments = []
	sys.stdout.write(str(count_autofix_failure_streak(comments if isinstance(comments, list) else [])) + "\n")
	return 0


def _cmd_autofix_failure_fingerprint(args: argparse.Namespace) -> int:
	evidence = read_failure_evidence(args.evidence_file or [])
	if args.evidence_out:
		Path(args.evidence_out).write_text(sanitize_text(evidence), encoding="utf-8")
	if args.failure_reason:
		reason = args.failure_reason if _FAILURE_REASON_RE.match(args.failure_reason) else "workflow_failure"
	else:
		finalize = _finalize_reason_from_summary_line(args.summary_line_file) if args.summary_line_file else ""
		reason = derive_autofix_failure_reason(dict(os.environ), finalize)
	result = autofix_failure_fingerprint(failure_reason=reason, evidence_text=evidence)
	sys.stdout.write(f"fp={result['fp']}\n")
	sys.stdout.write(f"degraded={1 if result['degraded'] else 0}\n")
	sys.stdout.write(f"reason={safe_token(reason)}\n")
	if args.head_sha:
		sys.stdout.write(
			"marker="
			+ render_failure_marker(
				args.head_sha,
				reason,
				result["fp"],
				result["degraded"],
				args.run_id or None,
				args.engine_sha or None,
			)
			+ "\n"
		)
	return 0


def _cmd_autofix_identical_failure_count(args: argparse.Namespace) -> int:
	comments = _load_json_file(args.comments_json)
	if not isinstance(comments, list):
		raise ValueError("comments JSON must be a list")
	result = count_identical_failures(
		comments,
		head_sha=args.head_sha,
		author_login=args.author_login,
		engine_sha=args.engine_sha or None,
	)
	sys.stdout.write(f"count={int(result['count'])}\n")
	sys.stdout.write(f"fp={safe_token(result['fp'], 64)}\n")
	sys.stdout.write(f"reason={safe_token(result['reason'])}\n")
	sys.stdout.write(f"cap_applied={'true' if result['cap_applied'] else 'false'}\n")
	return 0


def _cmd_build_run_payload(args: argparse.Namespace) -> int:
	workflow_run = _load_json_file(args.workflow_run_json)
	payload = build_workflow_run_payload(repo=args.repo, workflow_run=workflow_run if isinstance(workflow_run, dict) else {})
	validate_payload(payload)
	_write_json(payload)
	return 0


def _cmd_validate_payload(args: argparse.Namespace) -> int:
	try:
		payload = validate_payload(_load_json_file(args.payload_json))
	except (ValueError, json.JSONDecodeError) as exc:
		sys.stderr.write(f"invalid payload: {exc}\n")
		return 2
	_write_json(payload)
	return 0


def _cmd_skip_reason(args: argparse.Namespace) -> int:
	payload = _load_json_file(args.payload_json)
	registered: list[str] = []
	if args.registry_json:
		try:
			loaded = _load_json_file(args.registry_json)
		except (OSError, json.JSONDecodeError):
			loaded = []
		registered = [item for item in loaded if isinstance(item, str)] if isinstance(loaded, list) else []
	sys.stdout.write(skip_reason(payload, registered_repos=registered, self_repo=args.self_repo) + "\n")
	return 0


def _cmd_wrap_dispatch(args: argparse.Namespace) -> int:
	payload = _load_json_file(args.payload_json)
	if not isinstance(payload, dict):
		raise ValueError("payload must be a JSON object")
	_write_json(wrap_dispatch(payload))
	return 0


def _cmd_unwrap_dispatch(args: argparse.Namespace) -> int:
	_write_json(unwrap_dispatch(_load_json_file(args.payload_json)))
	return 0


def _cmd_filter_log(args: argparse.Namespace) -> int:
	text = Path(args.log_file).read_text(encoding="utf-8", errors="replace")
	sys.stdout.write(filter_log(text, max_lines=args.max_lines, max_bytes=args.max_bytes))
	return 0


def _cmd_error_signature(args: argparse.Namespace) -> int:
	chunks = [Path(path).read_text(encoding="utf-8", errors="replace") for path in args.log_file]
	sys.stdout.write(error_signature("\n".join(chunks)) + "\n")
	return 0


def _cmd_fingerprint(args: argparse.Namespace) -> int:
	sys.stdout.write(fingerprint(args.workflow_name, args.failing_step, args.signature) + "\n")
	return 0


def _cmd_budget(args: argparse.Namespace) -> int:
	issues = _load_json_file(args.issues_json)
	decision = budget_decision(
		issues if isinstance(issues, list) else [],
		fp=args.fingerprint,
		preferred_repo=args.preferred_repo or None,
		source_gen=args.source_gen,
		source_root=args.source_root or None,
		max_depth=args.max_depth,
		max_open=args.max_open,
		max_per_day=args.max_per_day,
	)
	_write_json(decision)
	return 0


def _cmd_parse_classification(args: argparse.Namespace) -> int:
	text = Path(args.diagnosis_file).read_text(encoding="utf-8", errors="replace")
	sys.stdout.write(parse_classification(text) + "\n")
	return 0


def _cmd_compose_issue(args: argparse.Namespace) -> int:
	payload = validate_payload(_load_json_file(args.payload_json))
	diagnosis = Path(args.diagnosis_file).read_text(encoding="utf-8", errors="replace")
	run_summaries = _load_json_file(args.run_summaries_json) if args.run_summaries_json else []
	if not isinstance(run_summaries, list):
		run_summaries = []
	workflow_name = None
	for summary in run_summaries:
		if isinstance(summary, dict) and summary.get("workflow_name"):
			workflow_name = str(summary["workflow_name"])
			break
	title = compose_issue_title(payload, workflow_name=workflow_name)
	body = compose_issue_body(
		payload=payload,
		diagnosis=diagnosis,
		fp=args.fingerprint,
		gen=args.gen,
		root=args.root,
		classification=args.classification,
		target_branch=args.target_branch or None,
		max_depth=args.max_depth,
		intake_run_url=args.intake_run_url,
		run_summaries=[item for item in run_summaries if isinstance(item, dict)],
		integration_branch=args.integration_branch or None,
	)
	Path(args.title_out).write_text(title + "\n", encoding="utf-8")
	Path(args.body_out).write_text(body, encoding="utf-8")
	return 0


def _cmd_compose_occurrence(args: argparse.Namespace) -> int:
	payload = validate_payload(_load_json_file(args.payload_json))
	sys.stdout.write(compose_occurrence_comment(payload, intake_run_url=args.intake_run_url))
	return 0


def _cmd_branch_progress(args: argparse.Namespace) -> int:
	try:
		compare = _load_json_file(args.compare_json)
	except (OSError, ValueError):
		compare = None
	summary = summarize_branch_progress(compare, failed_sha=args.failed_sha.lower(), branch=args.branch)
	if not summary.get("available") and args.reason:
		summary = {"available": False, "reason": single_line(args.reason, 80)}
	_write_json(summary)
	return 0


def _cmd_render_branch_progress(args: argparse.Namespace) -> int:
	sys.stdout.write(render_branch_progress(_load_json_file(args.summary_json)))
	return 0


def _cmd_lineage_context(args: argparse.Namespace) -> int:
	issues = _load_json_file(args.issues_json)
	sys.stdout.write(render_heal_lineage_context(issues if isinstance(issues, list) else [], fp=args.fingerprint, root=args.root))
	return 0


def _cmd_check_already_fixed(args: argparse.Namespace) -> int:
	diagnosis = Path(args.diagnosis_file).read_text(encoding="utf-8", errors="replace")
	_write_json(check_heal_already_fixed_claim(diagnosis, _load_json_file(args.summary_json)))
	return 0


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	sub = parser.add_subparsers(dest="command", required=True)

	p = sub.add_parser("build-issue-payload", help="Build the dispatch payload for a labeled issue / PR")
	p.add_argument("--repo", required=True)
	p.add_argument("--kind", required=True, choices=("issue", "pull_request"))
	p.add_argument("--label", required=True)
	p.add_argument("--issue-json", required=True)
	p.add_argument("--comments-json")
	p.add_argument("--runs-json")
	p.add_argument("--wrapper-sha", default="")
	p.add_argument("--reporter-run-url", default="")
	p.set_defaults(func=_cmd_build_issue_payload)

	p = sub.add_parser("build-autofix-payload", help="Build the payload for a failed review/autofix run on a PR")
	p.add_argument("--repo", required=True)
	p.add_argument("--pr-json", required=True)
	p.add_argument("--comments-json")
	p.add_argument("--workflow-name", required=True)
	p.add_argument("--failure-reason", required=True)
	p.add_argument("--failure-evidence-file")
	p.add_argument("--failure-streak", type=int, default=1)
	p.add_argument("--run-id", required=True)
	p.add_argument("--run-url", default="")
	p.add_argument("--wrapper-sha", default="")
	p.add_argument("--reporter-run-url", default="")
	p.add_argument("--failure-fingerprint", default="")
	p.add_argument("--base-branch", default="")
	p.add_argument("--script-ref", default="")
	p.add_argument("--changed-files-file", default="")
	p.set_defaults(func=_cmd_build_autofix_payload)

	p = sub.add_parser("classify-crash-ownership", help="Print pr / base / none: who changed the file a review/autofix run crashed in")
	p.add_argument("--payload-json", required=True)
	p.add_argument("--base-changed-files", default="")
	p.set_defaults(func=_cmd_classify_crash_ownership)

	p = sub.add_parser("autofix-failure-streak", help="Count trailing review/autofix failure comments on a PR")
	p.add_argument("--comments-json", required=True)
	p.set_defaults(func=_cmd_autofix_failure_streak)

	p = sub.add_parser("autofix-failure-fingerprint", help="Print fp= / degraded= / reason= (and marker= with --head-sha) for a review/autofix failure")
	p.add_argument("--failure-reason", default="", help="omit to derive it from the run flags in the environment (reporter precedence)")
	p.add_argument("--summary-line-file", default="", help="REVIEW_AUTOFIX_RUN_SUMMARY_V1 line file for the finalize_reason fallback")
	p.add_argument("--evidence-file", action="append", default=[], help="stage stderr file; repeatable, missing files are skipped")
	p.add_argument("--evidence-out", default="", help="also write the joined evidence tail to this file")
	p.add_argument("--head-sha", default="")
	p.add_argument("--run-id", default="")
	p.add_argument("--engine-sha", default="")
	p.set_defaults(func=_cmd_autofix_failure_fingerprint)

	p = sub.add_parser("autofix-identical-failure-count", help="Print count= / fp= / reason= / cap_applied= for the trailing identical failures on a head")
	p.add_argument("--comments-json", required=True)
	p.add_argument("--head-sha", required=True)
	p.add_argument("--author-login", required=True)
	p.add_argument("--engine-sha", default="")
	p.set_defaults(func=_cmd_autofix_identical_failure_count)

	p = sub.add_parser("build-run-payload", help="Build the payload for a failed workflow_run event")
	p.add_argument("--repo", required=True)
	p.add_argument("--workflow-run-json", required=True)
	p.set_defaults(func=_cmd_build_run_payload)

	p = sub.add_parser("validate-payload", help="Validate + normalise an incoming payload")
	p.add_argument("--payload-json", required=True)
	p.set_defaults(func=_cmd_validate_payload)

	p = sub.add_parser("skip-reason", help="Print a skip reason (empty when the payload should be healed)")
	p.add_argument("--payload-json", required=True)
	p.add_argument("--registry-json", default="")
	p.add_argument("--self-repo", required=True)
	p.set_defaults(func=_cmd_skip_reason)

	p = sub.add_parser("wrap-dispatch", help="Print the repository_dispatch body with the report enveloped under client_payload.report")
	p.add_argument("--payload-json", required=True)
	p.set_defaults(func=_cmd_wrap_dispatch)

	p = sub.add_parser("unwrap-dispatch", help="Print the report inside an enveloped client_payload (flat payloads pass through)")
	p.add_argument("--payload-json", required=True)
	p.set_defaults(func=_cmd_unwrap_dispatch)

	p = sub.add_parser("filter-log", help="Keep high-signal lines + tail of a job log")
	p.add_argument("--log-file", required=True)
	p.add_argument("--max-lines", type=int, default=400)
	p.add_argument("--max-bytes", type=int, default=60_000)
	p.set_defaults(func=_cmd_filter_log)

	p = sub.add_parser("error-signature", help="Derive the normalised error signature of one or more logs")
	p.add_argument("--log-file", action="append", required=True)
	p.set_defaults(func=_cmd_error_signature)

	p = sub.add_parser("fingerprint", help="Compute the dedup fingerprint")
	p.add_argument("--workflow-name", required=True)
	p.add_argument("--failing-step", default="")
	p.add_argument("--signature", required=True)
	p.set_defaults(func=_cmd_fingerprint)

	p = sub.add_parser("budget", help="Dedup / lineage / budget decision")
	p.add_argument("--issues-json", required=True)
	p.add_argument("--fingerprint", required=True)
	p.add_argument("--preferred-repo", default="")
	p.add_argument("--source-gen", type=int)
	p.add_argument("--source-root", default="")
	p.add_argument("--max-depth", type=int, default=DEFAULT_MAX_LINEAGE_DEPTH)
	p.add_argument("--max-open", type=int, default=DEFAULT_MAX_OPEN_ISSUES)
	p.add_argument("--max-per-day", type=int, default=DEFAULT_MAX_ISSUES_PER_DAY)
	p.set_defaults(func=_cmd_budget)

	p = sub.add_parser("parse-classification", help="Read the classification token from the diagnosis")
	p.add_argument("--diagnosis-file", required=True)
	p.set_defaults(func=_cmd_parse_classification)

	p = sub.add_parser("compose-issue", help="Write the heal issue title + body files")
	p.add_argument("--payload-json", required=True)
	p.add_argument("--diagnosis-file", required=True)
	p.add_argument("--run-summaries-json", default="")
	p.add_argument("--fingerprint", required=True)
	p.add_argument("--gen", type=int, required=True)
	p.add_argument("--root", required=True)
	p.add_argument("--classification", required=True, choices=CLASSIFICATIONS)
	p.add_argument("--target-branch", default="")
	p.add_argument("--integration-branch", default="")
	p.add_argument("--max-depth", type=int, default=DEFAULT_MAX_LINEAGE_DEPTH)
	p.add_argument("--intake-run-url", required=True)
	p.add_argument("--title-out", required=True)
	p.add_argument("--body-out", required=True)
	p.set_defaults(func=_cmd_compose_issue)

	p = sub.add_parser("compose-occurrence", help="Print the occurrence comment for a duplicate report")
	p.add_argument("--payload-json", required=True)
	p.add_argument("--intake-run-url", required=True)
	p.set_defaults(func=_cmd_compose_occurrence)

	p = sub.add_parser("branch-progress", help="Summarise a compare response (failing SHA ... branch) as JSON")
	p.add_argument("--compare-json", required=True)
	p.add_argument("--failed-sha", required=True)
	p.add_argument("--branch", required=True)
	p.add_argument("--reason", default="", help="Reason to record when the compare response is missing or unusable")
	p.set_defaults(func=_cmd_branch_progress)

	p = sub.add_parser("render-branch-progress", help="Render the branch-progress prompt block")
	p.add_argument("--summary-json", required=True)
	p.set_defaults(func=_cmd_render_branch_progress)

	p = sub.add_parser("lineage-context", help="Render earlier heal issues sharing the fingerprint / lineage root")
	p.add_argument("--issues-json", required=True)
	p.add_argument("--fingerprint", required=True)
	p.add_argument("--root", default="")
	p.set_defaults(func=_cmd_lineage_context)

	p = sub.add_parser("check-already-fixed", help="Verify an already-fixed diagnosis cites a commit after the failing SHA")
	p.add_argument("--diagnosis-file", required=True)
	p.add_argument("--summary-json", required=True)
	p.set_defaults(func=_cmd_check_already_fixed)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		return int(args.func(args))
	except ValueError as exc:
		sys.stderr.write(f"error: {exc}\n")
		return 2


if __name__ == "__main__":
	sys.exit(main())

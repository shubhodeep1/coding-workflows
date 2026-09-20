#!/usr/bin/env python3
"""Shared logic for the workflow failure heal pipeline (report + intake).

Two shell drivers use this module through its CLI:

  * ``scripts/workflow_failure_heal_report.sh`` runs in the repository where an
    escalation label (``ai:needs-human`` and friends) was applied. It builds the
    thin ``repository_dispatch`` payload that is sent to coding-workflows.
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

SOURCE_KINDS = ("issue", "pull_request", "workflow_run")
REPORTABLE_CONCLUSIONS = ("failure", "timed_out")

CLASSIFICATIONS: tuple[str, ...] = (
	"workflow-defect",
	"consumer-app-defect",
	"consumer-config",
	"transient",
	"inconclusive",
)
# Classifications that open an issue in coding-workflows.
UPSTREAM_ISSUE_CLASSIFICATIONS = ("workflow-defect", "inconclusive")

DEFAULT_MAX_LINEAGE_DEPTH = 3
DEFAULT_MAX_OPEN_ISSUES = 10
DEFAULT_MAX_ISSUES_PER_DAY = 20
DEFAULT_TARGET_BRANCH = "stable"

ISSUE_EXCERPT_LIMIT = 4000
COMMENTS_EXCERPT_LIMIT = 6000
MAX_RUN_REFS = 3
MAX_PAYLOAD_BYTES = 60_000
SIGNATURE_LINE_LIMIT = 5
SIGNATURE_CHAR_LIMIT = 500

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_RUN_URL_RE = re.compile(
	r"https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/(?P<run_id>[0-9]+)"
)
_SMOKE_TITLE_RE = re.compile(r"\[E2E Smoke Test\b", re.IGNORECASE)
_SMOKE_LABELS = frozenset({"e2e-smoke-test"})
_MARKER_RE = re.compile(r"<!--\s*" + re.escape(MARKER_PREFIX) + r"(?P<key>[a-z_]+)=(?P<value>[^\s>]+)\s*-->")
_CLASSIFICATION_RE = re.compile(r"^##\s*Classification\s*$", re.IGNORECASE | re.MULTILINE)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LOG_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s?")

# Highest-signal first: the first bucket with any matching line wins.
_SIGNATURE_PATTERNS: tuple[re.Pattern[str], ...] = (
	re.compile(r"::error::", re.IGNORECASE),
	re.compile(r"\b[A-Z][A-Z0-9_]*_FAILED\b"),
	re.compile(r"\bTraceback \(most recent call last\)|^\s*\w+Error:", re.MULTILINE),
	re.compile(r"\bfatal:|\berror:|\bERROR\b|\bFAILED\b|\bexit code\b|\bexited with\b", re.IGNORECASE),
)
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
	comments_tail = "\n\n---\n\n".join(comment_texts[-8:])
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
		"comments_excerpt": sanitize_text(comments_tail, COMMENTS_EXCERPT_LIMIT),
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
	return {
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
		"reporter_run_url": sanitize_text(payload.get("reporter_run_url"), 300) or None,
		"reported_at": sanitize_text(payload.get("reported_at"), 40) or _iso(_utc_now()),
	}


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


def filter_log(text: str, *, max_lines: int = 400, max_bytes: int = 60_000) -> str:
	"""Keep the high-signal lines plus the tail of a job log, bounded."""
	lines = sanitize_text(text).split("\n")
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
	material = "|".join(
		[
			single_line(workflow_name, 200).lower(),
			single_line(failing_step, 200).lower(),
			signature[:SIGNATURE_CHAR_LIMIT],
		]
	)
	return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Dedup / lineage / budget
# ---------------------------------------------------------------------------


def budget_decision(
	issues: Iterable[dict[str, Any]],
	*,
	fp: str,
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
			if markers.get("fp") == fp and (duplicate is None or number > _positive_int(duplicate.get("number"))):
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
	if payload.get("issue_number"):
		noun = "Pull request" if payload.get("source_kind") == "pull_request" else "Issue"
		url = payload.get("issue_url") or f"https://github.com/{payload['source_repo']}/issues/{payload['issue_number']}"
		lines.append(f"- **Source {noun.lower()}:** {url} ({payload['source_repo']}#{payload['issue_number']})")
		lines.append(f"- **Escalation label:** `{payload.get('label')}`")
	if payload.get("workflow_name"):
		lines.append(f"- **Failed workflow:** `{payload['workflow_name']}` (conclusion: `{payload.get('conclusion') or 'unknown'}`)")
	if payload.get("head_branch"):
		lines.append(f"- **Failed on branch:** `{payload['head_branch']}`")
	if payload.get("head_sha"):
		lines.append(f"- **Head SHA:** `{payload['head_sha']}`")
	if payload.get("wrapper_sha"):
		lines.append(f"- **Consumer wrapper pin (coding-workflows release SHA):** `{payload['wrapper_sha']}`")
	for summary in run_summaries:
		step = summary.get("failing_step") or "unknown step"
		lines.append(f"- **Failed run:** {summary.get('url')} — workflow `{summary.get('workflow_name') or 'unknown'}`, step `{step}`")
	return lines


def compose_issue_title(payload: dict[str, Any], *, workflow_name: str | None) -> str:
	name = single_line(workflow_name or payload.get("workflow_name") or "", 120)
	if payload.get("source_kind") == "workflow_run":
		return f"Workflow heal: {name or 'release workflow'} failed on {payload.get('head_branch') or 'unknown branch'}"
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
) -> str:
	"""Compose the heal issue body (upstream or consumer-side)."""
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
		parts.append("")
	parts.append(f"## Automated workflow failure heal (generation {gen} of max {max_depth})")
	parts.append("")
	if classification in UPSTREAM_ISSUE_CLASSIFICATIONS:
		parts.append(
			"A pipeline failure in a consumer of these workflows was escalated for human attention. "
			"The diagnosis below attributes it to the shared workflow source, so this issue was filed "
			"automatically for the clarify -> plan -> implement -> review pipeline to fix it here."
			if payload.get("source_kind") != "workflow_run"
			else "A release / promotion workflow run failed. This issue was filed automatically for the "
			"clarify -> plan -> implement -> review pipeline to fix the cause."
		)
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
	)
	Path(args.title_out).write_text(title + "\n", encoding="utf-8")
	Path(args.body_out).write_text(body, encoding="utf-8")
	return 0


def _cmd_compose_occurrence(args: argparse.Namespace) -> int:
	payload = validate_payload(_load_json_file(args.payload_json))
	sys.stdout.write(compose_occurrence_comment(payload, intake_run_url=args.intake_run_url))
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
	p.add_argument("--max-depth", type=int, default=DEFAULT_MAX_LINEAGE_DEPTH)
	p.add_argument("--intake-run-url", required=True)
	p.add_argument("--title-out", required=True)
	p.add_argument("--body-out", required=True)
	p.set_defaults(func=_cmd_compose_issue)

	p = sub.add_parser("compose-occurrence", help="Print the occurrence comment for a duplicate report")
	p.add_argument("--payload-json", required=True)
	p.add_argument("--intake-run-url", required=True)
	p.set_defaults(func=_cmd_compose_occurrence)
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

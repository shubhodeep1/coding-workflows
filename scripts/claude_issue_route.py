#!/usr/bin/env python3
"""Route standalone issues to the Claude issue implementer or the Codex pipeline.

Standalone issues (anything the AI orchestrator does not manage) are
implemented by Claude Code by default. The reusable clarify workflow
(``.github/workflows/clarify.yml``) calls this module on every issue-open and
``/reclarify`` event to pick the implementer, and the Claude handoff / intake
drivers use it to build and validate the ``repository_dispatch`` payload and
the routine fire text.

Shell drivers:

  * ``scripts/claude_issue_handoff.sh`` runs in the repository where the issue
    was opened (a consumer, or coding-workflows itself). It claims the issue
    with the ``ai:claude`` label and sends one ``repository_dispatch``
    (event type ``claude-issue``) to coding-workflows.
  * ``scripts/claude_issue_intake.sh`` runs in coding-workflows. It validates
    the payload against the consumer-repo registry, authorizes it against the
    live target issue and the dispatcher's permission on the target repo
    (``authorize-target``, issue #4620), and queues it as one
    ``ai:claude-issue-queue`` issue in coding-workflows (``queue-issue``),
    created with the workflow's ``GITHUB_TOKEN`` so no workflow reacts to it.
  * The Claude issue pickup session (``.claude/commands/claude-issue-pickup.md``)
    reads the queue (``queue-pending``) and starts one Opus
    ``/implement-issue-claude`` session per queued issue with
    ``create_session``. A claude.ai routine run cannot do this: it gets no
    claude-code-remote tools (issue #4525).
  * ``scripts/claude_issue_queue_watchdog.sh`` flags queue issues nobody picked
    up (``queue-stale``).
  * A session too deep in the session lineage to create its own CLAUDE.md
    §26 checker asks the pickup for one with a one-shot trigger; the pickup
    parses that request with ``arm-check-in-request`` (CLAUDE.md §26.B step
    1c).

Queue binding (issue #4621): the creator of a queue issue says nothing about
its current title and body, which anyone who can edit the issue can change.
So every producer run (the intake, and the ``claude-pr-catch-all`` sweep job)
records each queue issue it opens or rewrites, with its exact title and
payload, in a ``claude-issue-queue-binding`` artifact of that run
(``add-queue-binding`` / ``append_queue_binding``). The pickup follows the
item's ``Intake run:`` / ``Sweep run:`` line, checks that the run is a
completed default-branch run of that producer's workflow and event, and
starts nothing unless the run's artifact lists the item unchanged and the
body is exactly the producer's rendering of that payload and run
(``fetch_queue_bindings`` and the ``bindings`` argument of ``queue_pending``).
An artifact can only be uploaded from inside its own run, so an edited item
cannot carry a binding it did not get from its producer.

Routing order (first match wins):

  1. orchestrator-managed issue (``ai:orchestrator-managed`` label or the
     ``Managed by: AI Orchestrator`` body marker) -> codex
  2. issue types clarify never auto-handles (``ai:orchestrator-tracking``,
     ``ai:security-audit``, ``ai:retro``, ``ai:provider-outage``) -> codex
     (unchanged behaviour; clarify never auto-handles them either)
  3. release-gate fixture issue (title starts with ``[E2E ``) -> codex
  4. ``ai:codex`` label -> codex (per-issue switch; wins over ``ai:claude``)
  5. ``ai:claude`` label -> claude (per-issue pin, also the claim label)
  6. repository variable ``AI_ISSUE_IMPLEMENTER``: ``codex`` -> codex;
     empty or ``claude`` -> claude; anything else -> claude with a warning

All functions are pure except ``fetch_open_queue`` (one ``gh api`` read),
``fetch_queue_bindings`` (the batched binding reads it documents),
``append_queue_binding`` (writes the binding file it is given), and the CLI
entrypoints, which read only the files they are given (or those reads) and
write JSON or text to stdout.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "claude_issue.v1"
DISPATCH_EVENT_TYPE = "claude-issue"
DEFAULT_UPSTREAM_REPO = "shubhodeep1/coding-workflows"

IMPLEMENTER_CLAUDE = "claude"
IMPLEMENTER_CODEX = "codex"
DEFAULT_IMPLEMENTER = IMPLEMENTER_CLAUDE

CLAUDE_LABEL = "ai:claude"
CODEX_LABEL = "ai:codex"
HANDOFF_FAILED_LABEL = "ai:claude-handoff-failed"
BLOCKED_LABEL = "ai:claude-blocked"
ORCHESTRATOR_MANAGED_LABEL = "ai:orchestrator-managed"

# Queue between the intake workflow and the Claude issue pickup session. Queue
# issues live in coding-workflows and are trusted only when opened by the
# intake's GITHUB_TOKEN identity.
QUEUE_LABEL = "ai:claude-issue-queue"
QUEUE_STALE_LABEL = "ai:claude-issue-queue-stale"
QUEUE_MARKER = "<!-- ai:claude-issue-queue:v1 -->"
QUEUE_TITLE_PREFIX = "[claude-issue-queue] "
QUEUE_TRUSTED_AUTHOR = "github-actions[bot]"
QUEUE_PICKUP_LIMIT = 20
# Per-wake start limit override for the pickup session (issue #4990): unset,
# empty, or non-integer means QUEUE_PICKUP_LIMIT; an integer is clamped.
QUEUE_PICKUP_LIMIT_ENV = "CLAUDE_ISSUE_PICKUP_LIMIT"
QUEUE_PICKUP_LIMIT_MIN = 1
QUEUE_PICKUP_LIMIT_MAX = 30
# The pickup's wake kinds (issue #4990): an hourly wake (or the `start` drain)
# that leaves work behind schedules one catch-up wake; a catch-up never does.
QUEUE_WAKE_KINDS: tuple[str, ...] = ("hourly", "catch-up")
QUEUE_STALE_HOURS_DEFAULT = 3.0

# Issue types the clarify job-level `if:` already excludes on issue-open; a
# `/reclarify` on one of them keeps today's Codex behaviour.
CODEX_ONLY_LABELS: tuple[str, ...] = (
	"ai:orchestrator-tracking",
	"ai:security-audit",
	"ai:retro",
	# The model-provider outage marker (scripts/provider_outage.py, #5773).
	"ai:provider-outage",
)

# Labels whose issues skip their own security pass in the Claude project
# sequence (they are produced by automation; a security-finding project that
# ran its own audit could open follow-ups of follow-ups). The
# `skip_security_pass` value route_issue derives from them is advisory: it is
# logged and carried in the dispatch payload and queue item, but a label can be
# added by anyone, so `/implement-issue-claude` decides with
# `.claude/scripts/security_pass_skip.py`, which verifies the issue was created
# and labelled by the issue automation (issue #4623).
SECURITY_PASS_SKIP_LABELS: tuple[str, ...] = (
	"ai:security",
	"ai:check-triage",
	"ai:workflow-heal",
)

VALID_TRIGGERS: tuple[str, ...] = ("opened", "reclarify", "manual")

# Intake authorization (issue #4620): the same author rule clarify applies
# before it routes an issue (.github/workflows/clarify.yml), plus the
# repository permissions a dispatcher needs on the target repo. The REST
# `permission` field folds `maintain` into `write` and `triage` into `read`.
TRUSTED_ISSUE_AUTHOR_ASSOCIATIONS: tuple[str, ...] = ("OWNER", "MEMBER", "COLLABORATOR")
TRUSTED_ISSUE_BOT_AUTHOR = "github-actions[bot]"
DISPATCHER_ALLOWED_PERMISSIONS: tuple[str, ...] = ("admin", "write")
RECLARIFY_COMMAND_PREFIX = "/reclarify"

ORCHESTRATOR_BODY_MARKER_RE = re.compile(r"(?mi)^\s*(?:[-*]\s*)?Managed by:\s*AI Orchestrator\b")
E2E_FIXTURE_TITLE_RE = re.compile(r"(?i)^\[E2E ")
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
QUEUE_PAYLOAD_BLOCK_RE = re.compile(r"```text\n(.*?)\n```", re.DOTALL)
RUN_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$")
FIRE_TEXT_KEYS: tuple[str, ...] = ("repo", "issue", "url", "trigger", "skip_security_pass")

# Pull-request fix items (CLAUDE.md §26.H): the catch-all sweep
# (scripts/claude_pr_sweep.py) queues one per claude/* PR whose Claude fix
# nobody handled, in the same queue, and the pickup starts `/fix-claude-pr`.
PR_FIX_SCHEMA_VERSION = "claude_pr_fix.v1"
PR_FIX_TEXT_KEYS: tuple[str, ...] = ("repo", "pr", "url", "head", "kind", "claim")
PR_FIX_KINDS: tuple[str, ...] = ("conflict", "ci", "review", "blocked")
PR_FIX_CLAIM_RE = re.compile(r"^sweep-run-(?:[0-9]{1,20}|local)$")
PR_FIX_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")

# Queue binding (issue #4621): the artifact each producer run uploads, and the
# workflow, events, and run-URL line that identify a producer per item type.
QUEUE_BINDING_ARTIFACT = "claude-issue-queue-binding"
QUEUE_BINDING_SCHEMA_VERSION = "claude_issue_queue_binding.v1"
QUEUE_BINDING_FILENAME = "claude_issue_queue_binding.json"
QUEUE_BINDING_MAX_BYTES = 1_000_000
QUEUE_PRODUCERS: dict[str, dict[str, Any]] = {
	"issue": {
		"workflow": "claude-issue-intake.yml",
		"events": ("repository_dispatch", "workflow_dispatch"),
		"run_line": "Intake run",
	},
	"pr_fix": {
		"workflow": "review_autofix_sweep.yml",
		"events": ("schedule", "workflow_dispatch"),
		"run_line": "Sweep run",
	},
}
QUEUE_BINDING_DEFERRED = "deferred"
# The pickup reads the producer runs of this many times its start limit of
# targets. Items that fail the binding stay open for the watchdog, so with a
# window of only `limit` targets, `limit` stuck ones would take every read of
# every wake and defer the bound items behind them forever.
QUEUE_BINDING_SCAN_FACTOR = 3
RUN_URL_PARTS_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/([0-9]{1,20})$")


def _label_names(issue: dict[str, Any]) -> list[str]:
	names: list[str] = []
	for label in issue.get("labels") or []:
		if isinstance(label, dict):
			name = label.get("name")
		else:
			name = label
		if isinstance(name, str) and name:
			names.append(name)
	return names


def normalize_implementer_var(value: str | None) -> tuple[str, str]:
	"""Return (implementer, reason) for the AI_ISSUE_IMPLEMENTER value."""
	raw = (value or "").strip().lower()
	if raw == "":
		return DEFAULT_IMPLEMENTER, "default"
	if raw == IMPLEMENTER_CODEX:
		return IMPLEMENTER_CODEX, "repo_var"
	if raw == IMPLEMENTER_CLAUDE:
		return IMPLEMENTER_CLAUDE, "repo_var"
	return DEFAULT_IMPLEMENTER, "invalid_repo_var_default"


def route_issue(issue: dict[str, Any], implementer_var: str | None) -> dict[str, Any]:
	"""Decide which implementer owns a standalone issue.

	Returns ``{"implementer": "claude"|"codex", "reason": str,
	"skip_security_pass": bool}``.
	"""
	labels = _label_names(issue)
	body = issue.get("body") or ""
	title = issue.get("title") or ""
	skip_security = any(label in SECURITY_PASS_SKIP_LABELS for label in labels)

	def _result(implementer: str, reason: str) -> dict[str, Any]:
		return {
			"implementer": implementer,
			"reason": reason,
			"skip_security_pass": skip_security,
		}

	if ORCHESTRATOR_MANAGED_LABEL in labels or ORCHESTRATOR_BODY_MARKER_RE.search(body):
		return _result(IMPLEMENTER_CODEX, "orchestrator_managed")
	if any(label in CODEX_ONLY_LABELS for label in labels):
		return _result(IMPLEMENTER_CODEX, "codex_only_issue_type")
	if E2E_FIXTURE_TITLE_RE.search(title):
		return _result(IMPLEMENTER_CODEX, "e2e_fixture")
	if CODEX_LABEL in labels:
		return _result(IMPLEMENTER_CODEX, "label_override")
	if CLAUDE_LABEL in labels:
		return _result(IMPLEMENTER_CLAUDE, "label_override")
	implementer, reason = normalize_implementer_var(implementer_var)
	return _result(implementer, reason)


def build_dispatch(
	repo: str,
	issue: dict[str, Any],
	trigger: str,
	reporter_run_url: str,
	skip_security_pass: bool,
) -> dict[str, Any]:
	"""Build the repository_dispatch body sent to coding-workflows."""
	number = issue.get("number")
	if not isinstance(number, int) or number <= 0:
		raise ValueError("issue number missing or invalid")
	if not REPO_SLUG_RE.match(repo or ""):
		raise ValueError(f"invalid repo slug: {repo!r}")
	if trigger not in VALID_TRIGGERS:
		raise ValueError(f"invalid trigger: {trigger!r}")
	return {
		"event_type": DISPATCH_EVENT_TYPE,
		"client_payload": {
			"schema_version": SCHEMA_VERSION,
			"repo": repo,
			"issue_number": number,
			"issue_url": f"https://github.com/{repo}/issues/{number}",
			"trigger": trigger,
			"skip_security_pass": bool(skip_security_pass),
			"reporter_run_url": reporter_run_url or "",
		},
	}


def validate_payload(
	payload: dict[str, Any],
	allowed_repos: list[str],
) -> dict[str, Any]:
	"""Validate an intake ``client_payload``; raise ValueError when unusable.

	The repo must be listed in ``allowed_repos`` (compared case-insensitively),
	so only registered consumers and coding-workflows itself can start a
	Claude session through the dispatcher routine.
	"""
	if not isinstance(payload, dict):
		raise ValueError("payload is not an object")
	if payload.get("schema_version") != SCHEMA_VERSION:
		raise ValueError(f"unsupported schema_version: {payload.get('schema_version')!r}")
	repo = payload.get("repo")
	if not isinstance(repo, str) or not REPO_SLUG_RE.match(repo):
		raise ValueError(f"invalid repo: {repo!r}")
	allowed = {slug.lower() for slug in allowed_repos if isinstance(slug, str)}
	if repo.lower() not in allowed:
		raise ValueError(f"repo not registered: {repo}")
	number = payload.get("issue_number")
	if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
		raise ValueError(f"invalid issue_number: {number!r}")
	trigger = payload.get("trigger")
	if trigger not in VALID_TRIGGERS:
		raise ValueError(f"invalid trigger: {trigger!r}")
	return {
		"repo": repo,
		"issue_number": number,
		"issue_url": f"https://github.com/{repo}/issues/{number}",
		"trigger": trigger,
		"skip_security_pass": payload.get("skip_security_pass") is True,
		"reporter_run_url": payload.get("reporter_run_url") if isinstance(payload.get("reporter_run_url"), str) else "",
	}


def _user_field(item: dict[str, Any], key: str) -> str:
	user = item.get("user")
	value = user.get(key) if isinstance(user, dict) else None
	return value if isinstance(value, str) else ""


def is_trusted_issue_author(item: dict[str, Any]) -> bool:
	"""Clarify's author rule for an issue or comment object from the REST API.

	A ``User`` whose ``author_association`` is OWNER, MEMBER, or COLLABORATOR,
	or the ``github-actions[bot]`` Bot.
	"""
	if not isinstance(item, dict):
		return False
	user_type = _user_field(item, "type")
	if user_type == "User":
		return item.get("author_association") in TRUSTED_ISSUE_AUTHOR_ASSOCIATIONS
	return user_type == "Bot" and _user_field(item, "login") == TRUSTED_ISSUE_BOT_AUTHOR


def has_trusted_reclarify(comments: list[Any]) -> bool:
	"""True when a trusted ``User`` commented ``/reclarify`` (clarify's comment gate)."""
	for comment in comments or []:
		if not isinstance(comment, dict) or _user_field(comment, "type") != "User":
			continue
		body = comment.get("body")
		if (
			isinstance(body, str)
			and body.startswith(RECLARIFY_COMMAND_PREFIX)
			and comment.get("author_association") in TRUSTED_ISSUE_AUTHOR_ASSOCIATIONS
		):
			return True
	return False


def authorize_target(
	validated: dict[str, Any],
	issue: Any,
	dispatcher_permissions: dict[str, str],
	comments: list[Any] | None = None,
) -> dict[str, Any]:
	"""Decide whether a validated intake payload may start a Claude session.

	Inputs: ``validated`` from ``validate_payload``; ``issue`` from one
	``GET repos/<repo>/issues/<N>``; ``dispatcher_permissions`` maps each
	dispatcher login of the intake run (``github.actor`` and
	``github.triggering_actor``) to the ``permission`` field of
	``GET repos/<repo>/collaborators/<login>/permission``; ``comments`` is the
	issue's comment array, needed only when the author is not trusted.

	Output: ``{"authorized": bool, "reason": str, "needs_comments": bool}``.
	``needs_comments`` is true only when every other check passed and the
	caller has not supplied ``comments`` yet; the caller reads them and asks
	again. Pure: no API calls.
	"""
	def _result(authorized: bool, reason: str, needs_comments: bool = False) -> dict[str, Any]:
		return {"authorized": authorized, "reason": reason, "needs_comments": needs_comments}

	if not isinstance(dispatcher_permissions, dict) or not dispatcher_permissions:
		return _result(False, "dispatcher_unknown")
	for login, permission in dispatcher_permissions.items():
		if not isinstance(login, str) or not login:
			return _result(False, "dispatcher_unknown")
		if permission not in DISPATCHER_ALLOWED_PERMISSIONS:
			return _result(False, "dispatcher_not_authorized")
	if not isinstance(issue, dict) or "pull_request" in issue or issue.get("number") != validated.get("issue_number"):
		return _result(False, "target_not_issue")
	repository_url = issue.get("repository_url")
	expected_url = f"https://api.github.com/repos/{validated.get('repo', '')}"
	if not isinstance(repository_url, str) or repository_url.lower() != expected_url.lower():
		return _result(False, "target_repo_mismatch")
	if issue.get("state") != "open":
		return _result(False, "issue_closed")
	if is_trusted_issue_author(issue):
		return _result(True, "trusted_author")
	if comments is None:
		return _result(False, "untrusted_issue_author", needs_comments=True)
	if has_trusted_reclarify(comments):
		return _result(True, "trusted_reclarify")
	return _result(False, "untrusted_issue_author")


def build_fire_text(validated: dict[str, Any]) -> str:
	"""Render the routine fire text: fixed keys, no user-controlled prose."""
	lines = [
		SCHEMA_VERSION,
		f"repo: {validated['repo']}",
		f"issue: {validated['issue_number']}",
		f"url: {validated['issue_url']}",
		f"trigger: {validated['trigger']}",
		f"skip_security_pass: {'true' if validated['skip_security_pass'] else 'false'}",
	]
	return "\n".join(lines) + "\n"


def load_allowed_repos(registry_path: Path, self_repo: str) -> list[str]:
	"""Consumer-repo registry plus coding-workflows itself."""
	repos: list[str] = []
	try:
		data = json.loads(registry_path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		data = []
	if isinstance(data, list):
		repos.extend(slug for slug in data if isinstance(slug, str))
	if self_repo:
		repos.append(self_repo)
	return repos


def parse_fire_text(text: str) -> dict[str, Any]:
	"""Parse fixed-key fire text back into a validated payload; raise ValueError.

	The inverse of ``build_fire_text`` with the same rules as
	``claude-issue-dispatch.md`` step 1: first line ``claude_issue.v1``, each
	key exactly once, nothing else. The registry check is the caller's.
	"""
	lines = [line.rstrip() for line in (text or "").strip().splitlines()]
	if not lines or lines[0] != SCHEMA_VERSION:
		raise ValueError("first line is not claude_issue.v1")
	fields: dict[str, str] = {}
	for line in lines[1:]:
		key, sep, value = line.partition(": ")
		if not sep or key not in FIRE_TEXT_KEYS:
			raise ValueError(f"unexpected line: {line[:80]!r}")
		if key in fields:
			raise ValueError(f"duplicate key: {key}")
		fields[key] = value.strip()
	missing = [key for key in FIRE_TEXT_KEYS if key not in fields]
	if missing:
		raise ValueError(f"missing keys: {','.join(missing)}")
	repo = fields["repo"]
	if not REPO_SLUG_RE.match(repo):
		raise ValueError(f"invalid repo: {repo!r}")
	if not re.fullmatch(r"[1-9][0-9]{0,9}", fields["issue"]):
		raise ValueError(f"invalid issue: {fields['issue']!r}")
	number = int(fields["issue"])
	url = f"https://github.com/{repo}/issues/{number}"
	if fields["url"] != url:
		raise ValueError("url does not match repo and issue")
	if fields["trigger"] not in VALID_TRIGGERS:
		raise ValueError(f"invalid trigger: {fields['trigger']!r}")
	if fields["skip_security_pass"] not in ("true", "false"):
		raise ValueError("skip_security_pass is not true or false")
	return {
		"repo": repo,
		"issue_number": number,
		"issue_url": url,
		"trigger": fields["trigger"],
		"skip_security_pass": fields["skip_security_pass"] == "true",
	}


def queue_title(repo: str, issue_number: int) -> str:
	return f"{QUEUE_TITLE_PREFIX}{repo}#{issue_number}"


def pr_fix_queue_title(repo: str, pr_number: int) -> str:
	return f"{QUEUE_TITLE_PREFIX}fix {repo}#{pr_number}"


def build_pr_fix_text(repo: str, pr_number: int, head: str, kind: str, claim: str) -> str:
	"""Render the fixed-key ``claude_pr_fix.v1`` text; raise ValueError on bad input."""
	fields = parse_pr_fix_text("\n".join([
		PR_FIX_SCHEMA_VERSION,
		f"repo: {repo}",
		f"pr: {pr_number}",
		f"url: https://github.com/{repo}/pull/{pr_number}",
		f"head: {head}",
		f"kind: {kind}",
		f"claim: {claim}",
	]))
	return "\n".join([
		PR_FIX_SCHEMA_VERSION,
		f"repo: {fields['repo']}",
		f"pr: {fields['pr_number']}",
		f"url: {fields['pr_url']}",
		f"head: {fields['head']}",
		f"kind: {fields['kind']}",
		f"claim: {fields['claim']}",
	]) + "\n"


def parse_pr_fix_text(text: str) -> dict[str, Any]:
	"""Parse ``claude_pr_fix.v1`` text strictly (the rules of ``claude-issue-dispatch.md`` step 1)."""
	lines = [line.rstrip() for line in (text or "").strip().splitlines()]
	if not lines or lines[0] != PR_FIX_SCHEMA_VERSION:
		raise ValueError("first line is not claude_pr_fix.v1")
	fields: dict[str, str] = {}
	for line in lines[1:]:
		key, sep, value = line.partition(": ")
		if not sep or key not in PR_FIX_TEXT_KEYS:
			raise ValueError(f"unexpected line: {line[:80]!r}")
		if key in fields:
			raise ValueError(f"duplicate key: {key}")
		fields[key] = value.strip()
	missing = [key for key in PR_FIX_TEXT_KEYS if key not in fields]
	if missing:
		raise ValueError(f"missing keys: {','.join(missing)}")
	repo = fields["repo"]
	if not REPO_SLUG_RE.match(repo):
		raise ValueError(f"invalid repo: {repo!r}")
	if not re.fullmatch(r"[1-9][0-9]{0,9}", fields["pr"]):
		raise ValueError(f"invalid pr: {fields['pr']!r}")
	number = int(fields["pr"])
	url = f"https://github.com/{repo}/pull/{number}"
	if fields["url"] != url:
		raise ValueError("url does not match repo and pr")
	if not PR_FIX_HEAD_RE.match(fields["head"]):
		raise ValueError("head is not a 40-character lowercase hex sha")
	if fields["kind"] not in PR_FIX_KINDS:
		raise ValueError(f"invalid kind: {fields['kind']!r}")
	if not PR_FIX_CLAIM_RE.match(fields["claim"]):
		raise ValueError(f"invalid claim: {fields['claim']!r}")
	return {
		"repo": repo,
		"pr_number": number,
		"pr_url": url,
		"head": fields["head"],
		"kind": fields["kind"],
		"claim": fields["claim"],
	}


def build_pr_fix_queue_issue(repo: str, pr_number: int, head: str, kind: str, claim: str, run_url: str = "") -> dict[str, Any]:
	"""Render the queue issue for one pull-request fix (fixed keys and the sweep run URL only).

	The rendering is part of the pickup's binding check: ``queue_binding_verdict``
	re-renders the body and requires an exact match, so any change here makes
	every open item queued by the previous version ``binding_mismatch``: the
	pickup never starts it, the watchdog flags it, and the sweep does not queue
	that pull request again while the item is open. ``tests/test_claude_issue_route.py``
	pins the exact output; change both together, on purpose.
	"""
	text = build_pr_fix_text(repo, pr_number, head, kind, claim)
	lines = [
		QUEUE_MARKER,
		(
			f"Queued by the CLAUDE.md §26.H catch-all sweep for https://github.com/{repo}/pull/{pr_number}. "
			"The Claude issue pickup session (`.claude/commands/claude-issue-pickup.md`) starts one "
			"`/fix-claude-pr` session and closes this issue. Do not edit."
		),
		"",
		"```text",
		text.rstrip("\n"),
		"```",
	]
	if run_url and RUN_URL_RE.match(run_url):
		lines += ["", f"Sweep run: {run_url}"]
	return {"title": pr_fix_queue_title(repo, pr_number), "body": "\n".join(lines) + "\n", "label": QUEUE_LABEL}


def build_queue_issue(validated: dict[str, Any], run_url: str = "") -> dict[str, Any]:
	"""Render the coding-workflows queue issue for one validated payload.

	Only fixed keys and the intake run URL are written: no issue prose, so a
	queue issue can never carry instructions to the pickup session.

	The rendering is part of the pickup's binding check: ``queue_binding_verdict``
	re-renders the body and requires an exact match, so any change here makes
	every open item queued by the previous version ``binding_mismatch`` until
	``/reclarify`` rewrites it (the watchdog flags it meanwhile).
	``tests/test_claude_issue_route.py`` pins the exact output; change both
	together, on purpose.
	"""
	text = build_fire_text(validated)
	lines = [
		QUEUE_MARKER,
		(
			f"Queued by the Claude issue intake for {validated['issue_url']}. The Claude issue pickup "
			"session (`.claude/commands/claude-issue-pickup.md`) starts the implementation session "
			"and closes this issue. Do not edit."
		),
		"",
		"```text",
		text.rstrip("\n"),
		"```",
	]
	if run_url and RUN_URL_RE.match(run_url):
		lines += ["", f"Intake run: {run_url}"]
	return {
		"title": queue_title(validated["repo"], validated["issue_number"]),
		"body": "\n".join(lines) + "\n",
		"label": QUEUE_LABEL,
	}


def _queue_author(issue: dict[str, Any]) -> str:
	user = issue.get("user")
	login = user.get("login") if isinstance(user, dict) else None
	return login if isinstance(login, str) else ""


def _is_queue_issue(issue: dict[str, Any]) -> bool:
	return (
		isinstance(issue, dict)
		and "pull_request" not in issue
		and issue.get("state", "open") == "open"
		and QUEUE_LABEL in _label_names(issue)
	)


def queue_payload_text(body: str) -> str:
	"""The fixed-key payload block of a queue issue body; raise ValueError when absent."""
	match = QUEUE_PAYLOAD_BLOCK_RE.search((body or "").replace("\r\n", "\n"))
	if QUEUE_MARKER not in (body or "") or not match:
		raise ValueError("queue issue body has no payload block")
	return match.group(1)


def queue_run_id(body: str, item_type: str, queue_repo: str) -> tuple[str, str]:
	"""Return ``(run_id, "")`` for the item's producer run line, or ``("", reason)``.

	The line is ``Intake run: <url>`` for an issue item and ``Sweep run: <url>``
	for a pull-request fix, written by ``build_queue_issue`` /
	``build_pr_fix_queue_issue``. Exactly one such line naming a run of
	``queue_repo`` is required.
	"""
	label = QUEUE_PRODUCERS[item_type]["run_line"]
	lines = re.findall(rf"(?m)^{re.escape(label)}:[ \t]*(\S*)[ \t]*$", (body or "").replace("\r\n", "\n"))
	if not lines:
		return "", f"no {label} line"
	if len(lines) > 1:
		return "", f"several {label} lines"
	parts = RUN_URL_PARTS_RE.match(lines[0])
	if not parts:
		return "", f"malformed {label} line"
	if parts.group(1).lower() != (queue_repo or "").lower():
		return "", f"{label} line names another repository"
	return parts.group(2), ""


def queue_binding_verdict(
	bindings: dict[str, Any],
	item_type: str,
	queue_issue: int,
	title: str,
	body: str,
	payload: str,
) -> str:
	"""Check one queue issue against the producer-run bindings.

	Returns ``""`` when the item is bound unchanged, ``QUEUE_BINDING_DEFERRED``
	when its run was not fetched this wake, and otherwise the ``ignored``
	reason: ``unbound: …``, ``binding_mismatch``, ``binding_pending: …``,
	``binding_untrusted: …``, or ``binding_unavailable: …``.
	"""
	repo = bindings.get("repo") if isinstance(bindings, dict) else None
	runs = bindings.get("runs") if isinstance(bindings, dict) else None
	if not isinstance(repo, str) or not isinstance(runs, dict):
		return "unbound: no binding data"
	run_id, reason = queue_run_id(body, item_type, repo)
	if reason:
		return f"unbound: {reason}"
	# The whole body must be the producer's own rendering of this payload and
	# run, so no added text reaches the pickup through `queue_issues[].body`.
	try:
		if item_type == "pr_fix":
			fields = parse_pr_fix_text(payload)
			canonical = build_pr_fix_queue_issue(fields["repo"], fields["pr_number"], fields["head"], fields["kind"], fields["claim"], f"https://github.com/{repo}/actions/runs/{run_id}")
		else:
			canonical = build_queue_issue(parse_fire_text(payload), f"https://github.com/{repo}/actions/runs/{run_id}")
	except ValueError:
		return "binding_mismatch"
	if (body or "").replace("\r\n", "\n").rstrip() != canonical["body"].rstrip():
		return "binding_mismatch"
	record = runs.get(run_id)
	if record is None:
		return QUEUE_BINDING_DEFERRED
	state = record.get("state") if isinstance(record, dict) else None
	detail = record.get("reason", "") if isinstance(record, dict) else ""
	if state == "pending":
		return f"binding_pending: {detail}"
	if state == "unavailable":
		return f"binding_unavailable: {detail}"
	if state == "missing":
		return f"unbound: {detail}"
	if state != "ok":
		return f"binding_untrusted: {detail or 'unknown run state'}"
	if record.get("item_type") != item_type:
		return f"binding_untrusted: run {run_id} does not queue {item_type} items"
	items = record.get("items") if isinstance(record.get("items"), dict) else {}
	entry = items.get(str(queue_issue))
	if not isinstance(entry, dict):
		return f"unbound: run {run_id} did not queue this issue"
	if entry.get("title") != title or str(entry.get("payload") or "").strip() != (payload or "").strip():
		return "binding_mismatch"
	return ""


def queue_pending(
	issues: list[Any],
	allowed_repos: list[str],
	trusted_author: str = QUEUE_TRUSTED_AUTHOR,
	limit: int = QUEUE_PICKUP_LIMIT,
	bindings: dict[str, Any] | None = None,
	now: datetime | None = None,
) -> dict[str, Any]:
	"""Turn the open queue issues into the pickup's work list.

	Input: the JSON array from one ``GET repos/<self>/issues?labels=ai:claude-issue-queue&state=open``.
	Output: ``{"pending": [...], "ignored": [...], "remaining": int, "deferred": int,
	"limit": int, "oldest_waiting_minutes": int | None}``. Each
	pending entry is one target issue (duplicates from ``/reclarify`` are
	grouped, oldest queue issue first) with its fire text and the queue issues
	to close, and ``item_type`` ``issue``; or one pull request to fix
	(``item_type`` ``pr_fix``, a ``claude_pr_fix.v1`` item from the catch-all
	sweep, duplicates grouped with the newest item's head, kind and claim). Queue issues not opened by ``trusted_author``, with a malformed
	payload, a mismatched title, or an unregistered repo are listed under
	``ignored`` and never acted on. At most ``limit`` entries are returned;
	``remaining`` counts the rest for the next wake.

	Order (issue #4990): issue entries whose ``trigger`` is ``reclarify`` (a
	project already in flight whose blocker was answered) come first, then
	every other entry; each tier keeps queue order (oldest queue issue first).

	``oldest_waiting_minutes`` (issue #4990) is the whole minutes from the
	oldest ``created_at`` among the queue issues grouped into a target or
	deferred by this read to ``now`` (default: the current UTC time), or
	``None`` when there are none. Ignored items are left out: they stay open
	for the watchdog and would pin the value.

	``bindings`` (issue #4621) is the ``fetch_queue_bindings`` result. When
	given, an item is kept only when ``queue_binding_verdict`` finds it bound
	unchanged; every other item is ``ignored`` with that reason, except one
	whose run was not fetched, which counts in ``deferred`` (and
	``remaining``) for the next wake. ``None`` skips the check: only the
	sweep's "already queued" dedupe uses that, because it must count every
	open trusted item. The pickup CLI always passes bindings.
	"""
	allowed = {slug.lower() for slug in allowed_repos if isinstance(slug, str)}
	candidates = [issue for issue in issues if _is_queue_issue(issue)]
	candidates.sort(key=lambda item: item.get("number") if isinstance(item.get("number"), int) else 0)
	groups: dict[tuple[Any, ...], dict[str, Any]] = {}
	ignored: list[dict[str, Any]] = []
	deferred = 0
	waiting_since: list[datetime] = []

	def _waiting(item: dict[str, Any]) -> None:
		created = _parse_timestamp(item.get("created_at"))
		if created is not None:
			waiting_since.append(created)

	def _unbound(item_type: str, number: Any, title: Any, body: str, payload: str) -> str:
		"""``""`` when the binding check passes (or is skipped), else the verdict."""
		if bindings is None:
			return ""
		return queue_binding_verdict(bindings, item_type, number, title if isinstance(title, str) else "", body, payload)

	for issue in candidates:
		number = issue.get("number")
		if _queue_author(issue) != trusted_author:
			ignored.append({"queue_issue": number, "reason": "untrusted_author"})
			continue
		body = (issue.get("body") or "").replace("\r\n", "\n")
		match = QUEUE_PAYLOAD_BLOCK_RE.search(body)
		if QUEUE_MARKER not in body or not match:
			ignored.append({"queue_issue": number, "reason": "no_payload"})
			continue
		if match.group(1).lstrip().startswith(PR_FIX_SCHEMA_VERSION):
			try:
				pr_fix = parse_pr_fix_text(match.group(1))
			except ValueError as exc:
				ignored.append({"queue_issue": number, "reason": f"bad_payload: {exc}"})
				continue
			if pr_fix["repo"].lower() not in allowed:
				ignored.append({"queue_issue": number, "reason": "repo_not_registered"})
				continue
			if issue.get("title") != pr_fix_queue_title(pr_fix["repo"], pr_fix["pr_number"]):
				ignored.append({"queue_issue": number, "reason": "title_mismatch"})
				continue
			verdict = _unbound("pr_fix", number, issue.get("title"), body, match.group(1))
			if verdict == QUEUE_BINDING_DEFERRED:
				deferred += 1
				_waiting(issue)
				continue
			if verdict:
				ignored.append({"queue_issue": number, "reason": verdict})
				continue
			_waiting(issue)
			key = (pr_fix["repo"].lower(), "pr", pr_fix["pr_number"])
			queued = groups.get(key, {}).get("queue_issues", [])
			groups[key] = {
				"item_type": "pr_fix",
				**pr_fix,
				"fire_text": build_pr_fix_text(pr_fix["repo"], pr_fix["pr_number"], pr_fix["head"], pr_fix["kind"], pr_fix["claim"]),
				"queue_issues": [*queued, {"number": number, "body": body}],
			}
			continue
		try:
			validated = parse_fire_text(match.group(1))
		except ValueError as exc:
			ignored.append({"queue_issue": number, "reason": f"bad_payload: {exc}"})
			continue
		if validated["repo"].lower() not in allowed:
			ignored.append({"queue_issue": number, "reason": "repo_not_registered"})
			continue
		if issue.get("title") != queue_title(validated["repo"], validated["issue_number"]):
			ignored.append({"queue_issue": number, "reason": "title_mismatch"})
			continue
		verdict = _unbound("issue", number, issue.get("title"), body, match.group(1))
		if verdict == QUEUE_BINDING_DEFERRED:
			deferred += 1
			_waiting(issue)
			continue
		if verdict:
			ignored.append({"queue_issue": number, "reason": verdict})
			continue
		_waiting(issue)
		key = (validated["repo"].lower(), validated["issue_number"])
		entry = groups.get(key)
		if entry is None:
			entry = {"item_type": "issue", **validated, "fire_text": build_fire_text(validated), "queue_issues": []}
			groups[key] = entry
		entry["queue_issues"].append({"number": number, "body": body})
	ordered = list(groups.values())
	# Resumes first (issue #4990). The sort is stable, so each tier keeps
	# queue order.
	ordered.sort(key=lambda entry: 0 if entry.get("item_type") == "issue" and entry.get("trigger") == "reclarify" else 1)
	limit = max(int(limit), 0)
	return {
		"pending": ordered[:limit],
		"ignored": ignored,
		"remaining": max(len(ordered) - limit, 0) + deferred,
		"deferred": deferred,
		"limit": limit,
		"oldest_waiting_minutes": _oldest_waiting_minutes(waiting_since, now),
	}


def _oldest_waiting_minutes(waiting_since: list[datetime], now: datetime | None) -> int | None:
	"""Whole minutes from the oldest timestamp to ``now`` (never negative), or ``None``."""
	if not waiting_since:
		return None
	moment = now or datetime.now(timezone.utc)
	return max(int((moment - min(waiting_since)).total_seconds() // 60), 0)


def resolve_pickup_limit(value: str | None) -> int:
	"""The pickup's per-wake start limit from ``CLAUDE_ISSUE_PICKUP_LIMIT`` (issue #4990).

	Unset, empty, or non-integer → ``QUEUE_PICKUP_LIMIT``. An integer is
	clamped to ``QUEUE_PICKUP_LIMIT_MIN``..``QUEUE_PICKUP_LIMIT_MAX``. Fails
	open: a bad value never stops the queue from draining.
	"""
	text = (value or "").strip()
	try:
		parsed = int(text)
	except ValueError:
		return QUEUE_PICKUP_LIMIT
	return min(max(parsed, QUEUE_PICKUP_LIMIT_MIN), QUEUE_PICKUP_LIMIT_MAX)


def catch_up_due(wake: str, remaining: int) -> bool:
	"""True when this wake must schedule the pickup's one catch-up wake (issue #4990).

	Only an ``hourly`` wake (the pickup's cron wake, or its ``start`` drain)
	that leaves work for later schedules one; a ``catch-up`` wake never does,
	so the pickup gets at most one extra wake per hourly wake.
	"""
	return wake == "hourly" and isinstance(remaining, int) and remaining > 0


def queue_binding_run_ids(
	issues: list[Any],
	allowed_repos: list[str],
	queue_repo: str,
	trusted_author: str = QUEUE_TRUSTED_AUTHOR,
	limit: int = QUEUE_PICKUP_LIMIT,
) -> list[str]:
	"""Producer run ids the pickup must fetch bindings for this wake.

	Only the runs named by the queue issues of the first ``limit`` targets
	(before the binding check, in ``queue_pending`` order, so resumes come
	first) are returned, deduplicated in that order, so
	the reads per wake stay bounded. The pickup CLI passes
	``QUEUE_BINDING_SCAN_FACTOR`` times its start limit, so a few items that
	stay unbound cannot hold back a bound item behind them. Items of a later
	target whose run is not fetched are deferred to the next wake.
	"""
	result = queue_pending(issues, allowed_repos, trusted_author, limit)
	run_ids: list[str] = []
	for entry in result["pending"]:
		for queued in entry.get("queue_issues", []):
			run_id, _ = queue_run_id(queued.get("body") or "", entry["item_type"], queue_repo)
			if run_id and run_id not in run_ids:
				run_ids.append(run_id)
	return run_ids


def append_queue_binding(
	path: str | Path,
	repository: str,
	run_id: str | int,
	queue_issue: int,
	title: str,
	payload: str,
) -> dict[str, Any]:
	"""Record one queue issue in this run's binding file; return the document.

	Creates the file (and its directory) on first use. A file left by another
	run or repository is refused (ValueError), never merged. A second entry
	for the same queue issue replaces the first, because it is what the run
	wrote last. The write is atomic (temp file + rename).
	"""
	if not REPO_SLUG_RE.match(repository or ""):
		raise ValueError(f"invalid repository: {repository!r}")
	run_text = str(run_id)
	if not re.fullmatch(r"[1-9][0-9]{0,19}", run_text):
		raise ValueError(f"invalid run id: {run_id!r}")
	if isinstance(queue_issue, bool) or not isinstance(queue_issue, int) or queue_issue <= 0:
		raise ValueError(f"invalid queue issue: {queue_issue!r}")
	if not isinstance(title, str) or not title or not isinstance(payload, str) or not payload.strip():
		raise ValueError("title and payload are required")
	target = Path(path)
	if target.exists():
		doc = json.loads(target.read_text(encoding="utf-8"))
		load_queue_binding(doc, repository, run_text)
	else:
		doc = {"schema_version": QUEUE_BINDING_SCHEMA_VERSION, "repository": repository, "run_id": int(run_text), "items": []}
	doc["items"] = [item for item in doc["items"] if item.get("queue_issue") != queue_issue]
	doc["items"].append({"queue_issue": queue_issue, "title": title, "payload": payload})
	target.parent.mkdir(parents=True, exist_ok=True)
	tmp = target.with_name(target.name + ".tmp")
	tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
	os.replace(tmp, target)
	return doc


def load_queue_binding(doc: Any, repository: str, run_id: str | int) -> dict[str, dict[str, str]]:
	"""Validate one binding document; return ``{"<queue issue>": {"title", "payload"}}``.

	Raises ValueError unless the schema, repository (case-insensitive), and
	run id match and every item is well formed.
	"""
	if not isinstance(doc, dict) or doc.get("schema_version") != QUEUE_BINDING_SCHEMA_VERSION:
		raise ValueError("unsupported binding schema")
	if not isinstance(doc.get("repository"), str) or doc["repository"].lower() != (repository or "").lower():
		raise ValueError("binding names another repository")
	if str(doc.get("run_id")) != str(run_id):
		raise ValueError("binding names another run")
	items = doc.get("items")
	if not isinstance(items, list):
		raise ValueError("binding items is not a list")
	result: dict[str, dict[str, str]] = {}
	for item in items:
		if not isinstance(item, dict):
			raise ValueError("binding item is not an object")
		number = item.get("queue_issue")
		if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
			raise ValueError("binding item has no valid queue_issue")
		if not isinstance(item.get("title"), str) or not isinstance(item.get("payload"), str):
			raise ValueError("binding item has no title or payload")
		result[str(number)] = {"title": item["title"], "payload": item["payload"]}
	return result


def evaluate_producer_run(run: Any, repo: str, default_branch: str) -> tuple[str, str]:
	"""Return ``(item_type, "")`` for a trusted producer run, else ``("", reason)``.

	Trusted means: a run of this repository, not from a fork, of the intake or
	sweep workflow file, triggered by one of that producer's events, on the
	default branch. The run's head commit must also be on the default branch;
	``fetch_queue_bindings`` checks that separately for every event, because
	``head_branch`` is only a name and a tag can carry the default branch's.
	"""
	if not isinstance(run, dict):
		return "", "run record missing"
	repository = (run.get("repository") or {}).get("full_name") if isinstance(run.get("repository"), dict) else None
	head_repository = (run.get("head_repository") or {}).get("full_name") if isinstance(run.get("head_repository"), dict) else None
	if not isinstance(repository, str) or repository.lower() != (repo or "").lower():
		return "", "run belongs to another repository"
	if not isinstance(head_repository, str) or head_repository.lower() != (repo or "").lower():
		return "", "run head is another repository"
	path = run.get("path")
	item_type = next(
		(name for name, producer in QUEUE_PRODUCERS.items() if path == f".github/workflows/{producer['workflow']}"),
		"",
	)
	if not item_type:
		return "", f"run is not a queue producer ({path!r})"
	if run.get("event") not in QUEUE_PRODUCERS[item_type]["events"]:
		return "", f"event {run.get('event')!r} cannot queue items"
	if not default_branch or run.get("head_branch") != default_branch:
		return "", "run is not on the default branch"
	return item_type, ""


def _parse_timestamp(value: Any) -> datetime | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None
	return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def queue_stale(
	issues: list[Any],
	now: datetime,
	stale_hours: float = QUEUE_STALE_HOURS_DEFAULT,
	trusted_author: str = QUEUE_TRUSTED_AUTHOR,
) -> list[dict[str, Any]]:
	"""Trusted open queue issues older than ``stale_hours`` and not yet flagged.

	Input: the same issue array as ``queue_pending``. Output: one
	``{"number", "title", "age_hours"}`` per issue to flag, oldest first.
	Issues already carrying ``ai:claude-issue-queue-stale`` are skipped, so
	each stranded item alerts once.
	"""
	stale: list[dict[str, Any]] = []
	for issue in issues:
		if not _is_queue_issue(issue) or _queue_author(issue) != trusted_author:
			continue
		if QUEUE_STALE_LABEL in _label_names(issue):
			continue
		created = _parse_timestamp(issue.get("created_at"))
		if created is None:
			continue
		age = (now - created).total_seconds() / 3600.0
		if age >= stale_hours:
			stale.append({"number": issue.get("number"), "title": issue.get("title") or "", "age_hours": round(age, 1)})
	stale.sort(key=lambda item: -item["age_hours"])
	return stale


# CLAUDE.md §26.B step 1c: the arguments a deep session sends the pickup.
ARM_CHECK_IN_REQUEST_RE = re.compile(
	r"^— arm-check-in (?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<pr>[1-9][0-9]{0,9}) "
	r"for (?P<requester>session_[A-Za-z0-9]{10,64})$"
)


def parse_arm_check_in_request(text: str, allowed_repos: list[str]) -> dict[str, Any]:
	"""Parse one ``— arm-check-in <owner>/<repo>#<n> for <session id>`` line.

	Input: the pickup's ``$ARGUMENTS`` text and the allowed repositories
	(``load_allowed_repos``). Output: the fixed fields the pickup uses to
	create the checker and wake the requester; nothing in the output is copied
	from the request except the validated slug, number, and session id.
	Raises ValueError on anything else: more than one line, another format,
	or an unregistered repository. Pure; no API calls.
	"""
	lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
	if len(lines) != 1:
		raise ValueError("expected exactly one arguments line")
	match = ARM_CHECK_IN_REQUEST_RE.match(lines[0])
	if not match:
		raise ValueError("arguments are not '— arm-check-in <owner>/<repo>#<n> for session_<id>'")
	repo = match.group("repo")
	allowed = {slug.lower() for slug in allowed_repos if isinstance(slug, str)}
	if repo.lower() not in allowed:
		raise ValueError(f"repo not registered: {repo}")
	number = int(match.group("pr"))
	pr_url = f"https://github.com/{repo}/pull/{number}"
	return {
		"repo": repo,
		"pr_number": number,
		"pr_url": pr_url,
		"source_url": f"https://github.com/{repo}",
		"requester": match.group("requester"),
		"checker_title": f"PR #{number} status check-in",
		"ready_trigger_name": f"PR #{number} status check-in: checker ready",
		"ready_prompt": (
			f"CLAUDE.md §26.B step 1c: the Claude issue pickup created checker <checker id> "
			f"for PR #{number} ({pr_url}). Continue with CLAUDE.md §26.B steps 3–4 for that "
			f"checker in this session."
		),
	}


def _read_json(path: str) -> Any:
	return json.loads(Path(path).read_text(encoding="utf-8"))


def _cmd_route(args: argparse.Namespace) -> int:
	issue = _read_json(args.issue_json)
	print(json.dumps(route_issue(issue, args.implementer_var)))
	return 0


def _cmd_build_dispatch(args: argparse.Namespace) -> int:
	issue = _read_json(args.issue_json)
	try:
		body = build_dispatch(
			args.repo,
			issue,
			args.trigger,
			args.reporter_run_url,
			args.skip_security_pass == "true",
		)
	except ValueError as exc:
		print(str(exc), file=sys.stderr)
		return 2
	print(json.dumps(body))
	return 0


def _cmd_validate_payload(args: argparse.Namespace) -> int:
	payload = _read_json(args.payload_json)
	if isinstance(payload, dict) and isinstance(payload.get("client_payload"), dict):
		payload = payload["client_payload"]
	allowed = load_allowed_repos(Path(args.registry), args.self_repo)
	try:
		validated = validate_payload(payload, allowed)
	except ValueError as exc:
		print(str(exc), file=sys.stderr)
		return 2
	print(json.dumps(validated))
	return 0


def _cmd_authorize_target(args: argparse.Namespace) -> int:
	validated = _read_json(args.validated_json)
	issue = _read_json(args.issue_json)
	permissions = _read_json(args.permissions_json)
	comments = _read_json(args.comments_json) if args.comments_json else None
	if comments is not None and not isinstance(comments, list):
		print("comments JSON is not an array", file=sys.stderr)
		return 2
	print(json.dumps(authorize_target(validated, issue, permissions, comments)))
	return 0


def _cmd_fire_body(args: argparse.Namespace) -> int:
	validated = _read_json(args.validated_json)
	print(json.dumps({"text": build_fire_text(validated)}))
	return 0


def _cmd_queue_issue(args: argparse.Namespace) -> int:
	validated = _read_json(args.validated_json)
	print(json.dumps(build_queue_issue(validated, args.run_url)))
	return 0


def _cmd_pr_fix_queue_issue(args: argparse.Namespace) -> int:
	try:
		print(json.dumps(build_pr_fix_queue_issue(args.repo, args.pr, args.head, args.kind, args.claim, args.run_url)))
	except ValueError as exc:
		print(str(exc), file=sys.stderr)
		return 2
	return 0


def _cmd_arm_check_in_request(args: argparse.Namespace) -> int:
	try:
		# ValueError covers UnicodeDecodeError and a path with a NUL byte.
		text = Path(args.arguments_file).read_text(encoding="utf-8")
	except (OSError, ValueError) as exc:
		print(f"cannot read arguments file: {exc}", file=sys.stderr)
		return 2
	try:
		# load_allowed_repos already maps an unreadable or malformed registry
		# to the self repo only, so the only error this block raises today is
		# parse_arm_check_in_request's ValueError, which ends in exit 2.
		allowed = load_allowed_repos(Path(args.registry), args.self_repo)
		print(json.dumps(parse_arm_check_in_request(text, allowed)))
	except ValueError as exc:
		print(str(exc), file=sys.stderr)
		return 2
	return 0


def fetch_open_queue(repo: str) -> list[Any]:
	"""One REST read of the open queue issues in ``repo`` via ``gh`` (§15).

	Raises RuntimeError when gh fails or the answer is not a JSON array.
	"""
	if not REPO_SLUG_RE.match(repo or ""):
		raise RuntimeError(f"invalid repo: {repo!r}")
	cmd = ["gh", "api", f"repos/{repo}/issues?labels={QUEUE_LABEL}&state=open&per_page=100"]
	try:
		proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise RuntimeError(f"gh api failed: {exc}") from exc
	if proc.returncode != 0:
		raise RuntimeError(f"gh api exited {proc.returncode}: {proc.stderr.strip()[:300]}")
	try:
		data = json.loads(proc.stdout)
	except ValueError as exc:
		raise RuntimeError(f"gh api returned invalid JSON: {exc}") from exc
	if not isinstance(data, list):
		raise RuntimeError("gh api returned a non-array")
	return data


def _gh_api_read(path: str, binary: bool = False, jq: str = "") -> Any:
	"""One ``gh api`` GET; returns bytes (``binary``), the ``--jq`` text, or parsed JSON.

	Raises RuntimeError on a failed call or invalid JSON.
	"""
	cmd = ["gh", "api", path]
	if jq:
		cmd += ["--jq", jq]
	try:
		proc = subprocess.run(cmd, capture_output=True, timeout=120)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise RuntimeError(f"gh api failed: {exc}") from exc
	if proc.returncode != 0:
		detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
		raise RuntimeError(f"gh api {path.split('?')[0]} exited {proc.returncode}: {detail}")
	if binary:
		return proc.stdout
	text = proc.stdout.decode("utf-8", "replace")
	if jq:
		return text.strip()
	try:
		return json.loads(text)
	except ValueError as exc:
		raise RuntimeError(f"gh api {path.split('?')[0]} returned invalid JSON: {exc}") from exc


def _read_binding_zip(data: bytes, repo: str, run_id: str) -> dict[str, dict[str, str]]:
	"""Parse a downloaded binding artifact zip; raise ValueError when unusable."""
	try:
		archive = zipfile.ZipFile(io.BytesIO(data))
	except zipfile.BadZipFile as exc:
		raise ValueError(f"artifact is not a zip: {exc}") from exc
	with archive:
		members = [info for info in archive.infolist() if info.filename == QUEUE_BINDING_FILENAME]
		if len(members) != 1:
			raise ValueError(f"artifact has no single {QUEUE_BINDING_FILENAME}")
		if members[0].file_size > QUEUE_BINDING_MAX_BYTES:
			raise ValueError("binding file too large")
		try:
			doc = json.loads(archive.read(members[0]).decode("utf-8"))
		except (UnicodeDecodeError, ValueError) as exc:
			raise ValueError(f"binding file is not JSON: {exc}") from exc
	return load_queue_binding(doc, repo, run_id)


def fetch_queue_bindings(
	repo: str,
	run_ids: list[str],
	default_branch: str = "",
	gh_read: Any = None,
) -> dict[str, Any]:
	"""Read and check the producer-run binding for each run id (issue #4621).

	Input: the queue repository and the run ids from ``queue_binding_run_ids``
	(the runs named by the first ``QUEUE_BINDING_SCAN_FACTOR`` × limit
	targets, 60 at the default limit of 20). Output:
	``{"repo": repo, "runs": {"<run id>": record}}`` for ``queue_pending``,
	where a record is ``{"state": "ok", "item_type", "items"}`` or
	``{"state": "pending" | "missing" | "untrusted" | "unavailable", "reason"}``.

	Calls (CLAUDE.md §15), all REST GETs through ``gh``; none when
	``run_ids`` is empty. 1 ``repos/<repo>`` for the default branch, unless
	``default_branch`` is given. 2 run listings,
	``actions/workflows/<producer workflow>/runs`` (100 newest runs each), and
	1 ``actions/artifacts?name=<binding artifact>`` listing (100 newest),
	shared by every run id. Per run id: 1 ``actions/runs/<id>`` read only when
	the listings missed it, 1 ``actions/runs/<id>/artifacts`` read only when
	the artifact listing missed it, 1
	``compare/<head sha>...refs/heads/<default>`` read, and 1 artifact zip
	download.
	Fail-open per call: a failed listing falls back to the per-run reads, and
	a failed per-run read marks only that run ``unavailable``, so its items
	are ignored this wake and retried on the next. Nothing is retried in a
	loop, and no failure ever makes an item pending.
	"""
	read = gh_read or _gh_api_read
	runs: dict[str, Any] = {}
	wanted = [str(run_id) for run_id in run_ids if re.fullmatch(r"[0-9]{1,20}", str(run_id))]
	if not wanted:
		return {"repo": repo, "runs": runs}
	if not REPO_SLUG_RE.match(repo or ""):
		return {"repo": repo, "runs": {run_id: {"state": "unavailable", "reason": "invalid repository"} for run_id in wanted}}
	branch = default_branch
	if not branch:
		try:
			branch = read(f"repos/{repo}", jq=".default_branch")
		except RuntimeError as exc:
			return {"repo": repo, "runs": {run_id: {"state": "unavailable", "reason": f"default branch: {exc}"} for run_id in wanted}}
	run_meta: dict[str, Any] = {}
	for producer in QUEUE_PRODUCERS.values():
		try:
			listing = read(f"repos/{repo}/actions/workflows/{producer['workflow']}/runs?per_page=100")
		except RuntimeError:
			continue
		for run in (listing.get("workflow_runs") if isinstance(listing, dict) else None) or []:
			if isinstance(run, dict) and run.get("id") is not None:
				run_meta.setdefault(str(run["id"]), run)
	artifacts: dict[str, Any] = {}
	try:
		listing = read(f"repos/{repo}/actions/artifacts?name={QUEUE_BINDING_ARTIFACT}&per_page=100")
	except RuntimeError:
		listing = {}
	for artifact in (listing.get("artifacts") if isinstance(listing, dict) else None) or []:
		workflow_run = artifact.get("workflow_run") if isinstance(artifact, dict) else None
		if isinstance(workflow_run, dict) and workflow_run.get("id") is not None and artifact.get("name") == QUEUE_BINDING_ARTIFACT:
			artifacts.setdefault(str(workflow_run["id"]), artifact)
	for run_id in wanted:
		runs[run_id] = _fetch_one_binding(read, repo, branch, run_id, run_meta.get(run_id), artifacts.get(run_id))
	return {"repo": repo, "runs": runs}


def _fetch_one_binding(read: Any, repo: str, branch: str, run_id: str, run: Any, artifact: Any) -> dict[str, Any]:
	"""The binding record for one run (see ``fetch_queue_bindings``)."""
	if run is None:
		try:
			run = read(f"repos/{repo}/actions/runs/{run_id}")
		except RuntimeError as exc:
			return {"state": "unavailable", "reason": f"run {run_id}: {exc}"}
	item_type, reason = evaluate_producer_run(run, repo, branch)
	if reason:
		return {"state": "untrusted", "reason": f"run {run_id}: {reason}"}
	if run.get("status") != "completed":
		return {"state": "pending", "reason": f"run {run_id} is {run.get('status')}"}
	# Every producer event, not only workflow_dispatch: head_branch is a name,
	# so the head commit itself must be reachable from the default branch. The
	# fully qualified ref keeps a tag of the same name out of the comparison.
	head_sha = run.get("head_sha") or ""
	if not PR_FIX_HEAD_RE.match(head_sha):
		return {"state": "untrusted", "reason": f"run {run_id}: head sha missing"}
	try:
		status = read(f"repos/{repo}/compare/{head_sha}...refs/heads/{branch}?per_page=1", jq=".status")
	except RuntimeError as exc:
		return {"state": "unavailable", "reason": f"run {run_id} compare: {exc}"}
	if status not in ("identical", "ahead"):
		return {"state": "untrusted", "reason": f"run {run_id}: {run.get('event')} head is not on {branch}"}
	if artifact is None:
		try:
			listing = read(f"repos/{repo}/actions/runs/{run_id}/artifacts?name={QUEUE_BINDING_ARTIFACT}&per_page=100")
		except RuntimeError as exc:
			return {"state": "unavailable", "reason": f"run {run_id} artifacts: {exc}"}
		named = [
			item for item in ((listing.get("artifacts") if isinstance(listing, dict) else None) or [])
			if isinstance(item, dict) and item.get("name") == QUEUE_BINDING_ARTIFACT
		]
		artifact = named[0] if named else None
	if artifact is None:
		return {"state": "missing", "reason": f"run {run_id} has no {QUEUE_BINDING_ARTIFACT} artifact"}
	workflow_run = artifact.get("workflow_run") if isinstance(artifact.get("workflow_run"), dict) else {}
	if workflow_run.get("id") is not None and str(workflow_run.get("id")) != run_id:
		return {"state": "untrusted", "reason": f"run {run_id}: artifact belongs to another run"}
	if artifact.get("expired"):
		return {"state": "missing", "reason": f"run {run_id} binding artifact expired"}
	size = artifact.get("size_in_bytes")
	if not isinstance(size, int) or size > QUEUE_BINDING_MAX_BYTES:
		return {"state": "untrusted", "reason": f"run {run_id}: binding artifact size {size!r}"}
	artifact_id = artifact.get("id")
	if isinstance(artifact_id, bool) or not isinstance(artifact_id, int):
		return {"state": "untrusted", "reason": f"run {run_id}: binding artifact has no id"}
	try:
		data = read(f"repos/{repo}/actions/artifacts/{artifact_id}/zip", binary=True)
	except RuntimeError as exc:
		return {"state": "unavailable", "reason": f"run {run_id} download: {exc}"}
	try:
		items = _read_binding_zip(data, repo, run_id)
	except ValueError as exc:
		return {"state": "untrusted", "reason": f"run {run_id}: {exc}"}
	return {"state": "ok", "item_type": item_type, "items": items}


def _cmd_queue_pending(args: argparse.Namespace) -> int:
	queue_repo = args.fetch_repo or args.self_repo
	if args.fetch_repo:
		try:
			issues = fetch_open_queue(args.fetch_repo)
		except RuntimeError as exc:
			print(str(exc), file=sys.stderr)
			return 3
	elif args.issues_json:
		issues = _read_json(args.issues_json)
	else:
		print("one of --fetch-repo or --issues-json is required", file=sys.stderr)
		return 2
	if not isinstance(issues, list):
		print("issues JSON is not an array", file=sys.stderr)
		return 2
	allowed = load_allowed_repos(Path(args.registry), args.self_repo)
	# An explicit --limit wins; otherwise CLAUDE_ISSUE_PICKUP_LIMIT, default
	# QUEUE_PICKUP_LIMIT (issue #4990).
	limit = args.limit if args.limit is not None else resolve_pickup_limit(os.environ.get(QUEUE_PICKUP_LIMIT_ENV))
	now = None
	if args.now:
		now = _parse_timestamp(args.now)
		if now is None:
			print(f"invalid --now: {args.now!r}", file=sys.stderr)
			return 2
	# The binding check always runs (issue #4621): fetched for --fetch-repo,
	# read from --bindings-json otherwise. Without either, no run is known and
	# every item is deferred, so nothing unverified is ever started. The run
	# window is wider than the start limit (QUEUE_BINDING_SCAN_FACTOR), so
	# stuck unbound items cannot defer every bound item behind them.
	if args.fetch_repo:
		scan_limit = max(limit, 0) * QUEUE_BINDING_SCAN_FACTOR
		run_ids = queue_binding_run_ids(issues, allowed, queue_repo, args.trusted_author, scan_limit)
		bindings = fetch_queue_bindings(queue_repo, run_ids, args.default_branch)
	elif args.bindings_json:
		bindings = _read_json(args.bindings_json)
		if not isinstance(bindings, dict):
			print("bindings JSON is not an object", file=sys.stderr)
			return 2
	else:
		bindings = {"repo": queue_repo, "runs": {}}
	result = queue_pending(issues, allowed, args.trusted_author, limit, bindings=bindings, now=now)
	result["catch_up_due"] = catch_up_due(args.wake, result["remaining"])
	print(json.dumps(result))
	return 0


def _cmd_add_queue_binding(args: argparse.Namespace) -> int:
	try:
		item = _read_json(args.queue_issue_json)
		if not isinstance(item, dict):
			raise ValueError("queue issue JSON is not an object")
		append_queue_binding(
			args.binding_file,
			args.repository,
			args.run_id,
			args.queue_issue,
			item.get("title") if isinstance(item.get("title"), str) else "",
			queue_payload_text(item.get("body") if isinstance(item.get("body"), str) else ""),
		)
	except (OSError, ValueError) as exc:
		print(str(exc), file=sys.stderr)
		return 2
	return 0


def _cmd_queue_stale(args: argparse.Namespace) -> int:
	issues = _read_json(args.issues_json)
	if not isinstance(issues, list):
		print("issues JSON is not an array", file=sys.stderr)
		return 2
	now = _parse_timestamp(args.now) if args.now else datetime.now(timezone.utc)
	if now is None:
		print(f"invalid --now: {args.now!r}", file=sys.stderr)
		return 2
	print(json.dumps(queue_stale(issues, now, args.stale_hours, args.trusted_author)))
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)

	p_route = sub.add_parser("route", help="decide the implementer for an issue")
	p_route.add_argument("--issue-json", required=True)
	p_route.add_argument("--implementer-var", default="")
	p_route.set_defaults(func=_cmd_route)

	p_dispatch = sub.add_parser("build-dispatch", help="build the repository_dispatch body")
	p_dispatch.add_argument("--repo", required=True)
	p_dispatch.add_argument("--issue-json", required=True)
	p_dispatch.add_argument("--trigger", required=True)
	p_dispatch.add_argument("--reporter-run-url", default="")
	p_dispatch.add_argument("--skip-security-pass", default="false")
	p_dispatch.set_defaults(func=_cmd_build_dispatch)

	p_validate = sub.add_parser("validate-payload", help="validate an intake payload")
	p_validate.add_argument("--payload-json", required=True)
	p_validate.add_argument("--registry", required=True)
	p_validate.add_argument("--self-repo", default=DEFAULT_UPSTREAM_REPO)
	p_validate.set_defaults(func=_cmd_validate_payload)

	p_authorize = sub.add_parser("authorize-target", help="decide whether a validated payload may start a Claude session (issue #4620)")
	p_authorize.add_argument("--validated-json", required=True)
	p_authorize.add_argument("--issue-json", required=True)
	p_authorize.add_argument("--permissions-json", required=True, help='{"<login>": "<permission>"} for each dispatcher login')
	p_authorize.add_argument("--comments-json", default="", help="the issue's comments; only needed when the author is not trusted")
	p_authorize.set_defaults(func=_cmd_authorize_target)

	p_fire = sub.add_parser("fire-body", help="build the routine /fire request body")
	p_fire.add_argument("--validated-json", required=True)
	p_fire.set_defaults(func=_cmd_fire_body)

	p_queue = sub.add_parser("queue-issue", help="build the coding-workflows queue issue for a validated payload")
	p_queue.add_argument("--validated-json", required=True)
	p_queue.add_argument("--run-url", default="")
	p_queue.set_defaults(func=_cmd_queue_issue)

	p_pr_fix = sub.add_parser("pr-fix-queue-issue", help="build the queue issue for one claude/* PR fix (CLAUDE.md §26.H)")
	p_pr_fix.add_argument("--repo", required=True)
	p_pr_fix.add_argument("--pr", type=int, required=True)
	p_pr_fix.add_argument("--head", required=True)
	p_pr_fix.add_argument("--kind", required=True)
	p_pr_fix.add_argument("--claim", required=True)
	p_pr_fix.add_argument("--run-url", default="")
	p_pr_fix.set_defaults(func=_cmd_pr_fix_queue_issue)

	p_pending = sub.add_parser("queue-pending", help="list the queued issues the pickup should start")
	p_pending.add_argument("--issues-json", default="")
	p_pending.add_argument("--fetch-repo", default="", help="read the open queue of this repo with gh (one REST call)")
	p_pending.add_argument("--registry", required=True)
	p_pending.add_argument("--self-repo", default=DEFAULT_UPSTREAM_REPO)
	p_pending.add_argument("--trusted-author", default=QUEUE_TRUSTED_AUTHOR)
	p_pending.add_argument("--limit", type=int, default=None, help=f"targets to start this wake (default: {QUEUE_PICKUP_LIMIT_ENV}, else {QUEUE_PICKUP_LIMIT})")
	p_pending.add_argument("--bindings-json", default="", help="binding records (fetch_queue_bindings output) for --issues-json mode")
	p_pending.add_argument("--default-branch", default="", help="skip the default-branch read of --fetch-repo")
	p_pending.add_argument("--wake", choices=QUEUE_WAKE_KINDS, default="hourly", help="the pickup wake reading the queue; decides catch_up_due")
	p_pending.add_argument("--now", default="", help="reference time for oldest_waiting_minutes (default: now)")
	p_pending.set_defaults(func=_cmd_queue_pending)

	p_bind = sub.add_parser("add-queue-binding", help="record a queue issue in this run's binding file (issue #4621)")
	p_bind.add_argument("--binding-file", required=True)
	p_bind.add_argument("--repository", required=True)
	p_bind.add_argument("--run-id", required=True)
	p_bind.add_argument("--queue-issue", type=int, required=True)
	p_bind.add_argument("--queue-issue-json", required=True, help="the queue-issue output (title and body as written to the issue)")
	p_bind.set_defaults(func=_cmd_add_queue_binding)

	p_stale = sub.add_parser("queue-stale", help="list queue issues nobody picked up in time")
	p_stale.add_argument("--issues-json", required=True)
	p_stale.add_argument("--stale-hours", type=float, default=QUEUE_STALE_HOURS_DEFAULT)
	p_stale.add_argument("--now", default="")
	p_stale.add_argument("--trusted-author", default=QUEUE_TRUSTED_AUTHOR)
	p_stale.set_defaults(func=_cmd_queue_stale)

	p_arm = sub.add_parser("arm-check-in-request", help="parse a pickup — arm-check-in request (CLAUDE.md §26.B step 1c)")
	p_arm.add_argument("--arguments-file", required=True)
	p_arm.add_argument("--registry", required=True)
	p_arm.add_argument("--self-repo", default=DEFAULT_UPSTREAM_REPO)
	p_arm.set_defaults(func=_cmd_arm_check_in_request)

	args = parser.parse_args(argv)
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())

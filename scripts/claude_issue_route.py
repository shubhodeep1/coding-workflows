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
    the payload against the consumer-repo registry and queues it as one
    ``ai:claude-issue-queue`` issue in coding-workflows (``queue-issue``),
    created with the workflow's ``GITHUB_TOKEN`` so no workflow reacts to it.
  * The Claude issue pickup session (``.claude/commands/claude-issue-pickup.md``)
    reads the queue (``queue-pending``) and starts one Opus
    ``/implement-issue-claude`` session per queued issue with
    ``create_session``. A claude.ai routine run cannot do this: it gets no
    claude-code-remote tools (issue #4525).
  * ``scripts/claude_issue_queue_watchdog.sh`` flags queue issues nobody picked
    up (``queue-stale``).

Routing order (first match wins):

  1. orchestrator-managed issue (``ai:orchestrator-managed`` label or the
     ``Managed by: AI Orchestrator`` body marker) -> codex
  2. issue types clarify never auto-handles (``ai:orchestrator-tracking``,
     ``ai:security-audit``, ``ai:retro``) -> codex (unchanged behaviour)
  3. release-gate fixture issue (title starts with ``[E2E ``) -> codex
  4. ``ai:codex`` label -> codex (per-issue switch; wins over ``ai:claude``)
  5. ``ai:claude`` label -> claude (per-issue pin, also the claim label)
  6. repository variable ``AI_ISSUE_IMPLEMENTER``: ``codex`` -> codex;
     empty or ``claude`` -> claude; anything else -> claude with a warning

All functions are pure except ``fetch_open_queue`` (one ``gh api`` read) and
the CLI entrypoints, which read only the files they are given (or that one
read) and write JSON or text to stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
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
QUEUE_PICKUP_LIMIT = 10
QUEUE_STALE_HOURS_DEFAULT = 3.0

# Issue types the clarify job-level `if:` already excludes on issue-open; a
# `/reclarify` on one of them keeps today's Codex behaviour.
CODEX_ONLY_LABELS: tuple[str, ...] = (
	"ai:orchestrator-tracking",
	"ai:security-audit",
	"ai:retro",
)

# Labels whose issues skip their own security pass in the Claude project
# sequence (they are produced by automation; a security-finding project that
# ran its own audit could open follow-ups of follow-ups).
SECURITY_PASS_SKIP_LABELS: tuple[str, ...] = (
	"ai:security",
	"ai:check-triage",
	"ai:workflow-heal",
)

VALID_TRIGGERS: tuple[str, ...] = ("opened", "reclarify", "manual")

ORCHESTRATOR_BODY_MARKER_RE = re.compile(r"(?mi)^\s*(?:[-*]\s*)?Managed by:\s*AI Orchestrator\b")
E2E_FIXTURE_TITLE_RE = re.compile(r"(?i)^\[E2E ")
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
QUEUE_PAYLOAD_BLOCK_RE = re.compile(r"```text\n(.*?)\n```", re.DOTALL)
RUN_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$")
FIRE_TEXT_KEYS: tuple[str, ...] = ("repo", "issue", "url", "trigger", "skip_security_pass")


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


def build_queue_issue(validated: dict[str, Any], run_url: str = "") -> dict[str, Any]:
	"""Render the coding-workflows queue issue for one validated payload.

	Only fixed keys and the intake run URL are written: no issue prose, so a
	queue issue can never carry instructions to the pickup session.
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


def queue_pending(
	issues: list[Any],
	allowed_repos: list[str],
	trusted_author: str = QUEUE_TRUSTED_AUTHOR,
	limit: int = QUEUE_PICKUP_LIMIT,
) -> dict[str, Any]:
	"""Turn the open queue issues into the pickup's work list.

	Input: the JSON array from one ``GET repos/<self>/issues?labels=ai:claude-issue-queue&state=open``.
	Output: ``{"pending": [...], "ignored": [...], "remaining": int}``. Each
	pending entry is one target issue (duplicates from ``/reclarify`` are
	grouped, oldest queue issue first) with its fire text and the queue issues
	to close. Queue issues not opened by ``trusted_author``, with a malformed
	payload, a mismatched title, or an unregistered repo are listed under
	``ignored`` and never acted on. At most ``limit`` entries are returned;
	``remaining`` counts the rest for the next wake.
	"""
	allowed = {slug.lower() for slug in allowed_repos if isinstance(slug, str)}
	candidates = [issue for issue in issues if _is_queue_issue(issue)]
	candidates.sort(key=lambda item: item.get("number") if isinstance(item.get("number"), int) else 0)
	groups: dict[tuple[str, int], dict[str, Any]] = {}
	ignored: list[dict[str, Any]] = []
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
		key = (validated["repo"].lower(), validated["issue_number"])
		entry = groups.get(key)
		if entry is None:
			entry = {**validated, "fire_text": build_fire_text(validated), "queue_issues": []}
			groups[key] = entry
		entry["queue_issues"].append({"number": number, "body": body})
	ordered = list(groups.values())
	limit = max(int(limit), 0)
	return {"pending": ordered[:limit], "ignored": ignored, "remaining": max(len(ordered) - limit, 0)}


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


def _cmd_fire_body(args: argparse.Namespace) -> int:
	validated = _read_json(args.validated_json)
	print(json.dumps({"text": build_fire_text(validated)}))
	return 0


def _cmd_queue_issue(args: argparse.Namespace) -> int:
	validated = _read_json(args.validated_json)
	print(json.dumps(build_queue_issue(validated, args.run_url)))
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


def _cmd_queue_pending(args: argparse.Namespace) -> int:
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
	print(json.dumps(queue_pending(issues, allowed, args.trusted_author, args.limit)))
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

	p_fire = sub.add_parser("fire-body", help="build the routine /fire request body")
	p_fire.add_argument("--validated-json", required=True)
	p_fire.set_defaults(func=_cmd_fire_body)

	p_queue = sub.add_parser("queue-issue", help="build the coding-workflows queue issue for a validated payload")
	p_queue.add_argument("--validated-json", required=True)
	p_queue.add_argument("--run-url", default="")
	p_queue.set_defaults(func=_cmd_queue_issue)

	p_pending = sub.add_parser("queue-pending", help="list the queued issues the pickup should start")
	p_pending.add_argument("--issues-json", default="")
	p_pending.add_argument("--fetch-repo", default="", help="read the open queue of this repo with gh (one REST call)")
	p_pending.add_argument("--registry", required=True)
	p_pending.add_argument("--self-repo", default=DEFAULT_UPSTREAM_REPO)
	p_pending.add_argument("--trusted-author", default=QUEUE_TRUSTED_AUTHOR)
	p_pending.add_argument("--limit", type=int, default=QUEUE_PICKUP_LIMIT)
	p_pending.set_defaults(func=_cmd_queue_pending)

	p_stale = sub.add_parser("queue-stale", help="list queue issues nobody picked up in time")
	p_stale.add_argument("--issues-json", required=True)
	p_stale.add_argument("--stale-hours", type=float, default=QUEUE_STALE_HOURS_DEFAULT)
	p_stale.add_argument("--now", default="")
	p_stale.add_argument("--trusted-author", default=QUEUE_TRUSTED_AUTHOR)
	p_stale.set_defaults(func=_cmd_queue_stale)

	args = parser.parse_args(argv)
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())

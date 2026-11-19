#!/usr/bin/env python3
"""Decide whether an automation-filed issue may skip its own security pass.

Moved from `.claude/scripts/` when the session-based Claude automation was
retired (docs/plans/replace-claude-sessions-with-cli-engine-plan.md); the
single-issue security pass of that plan's Phase 8a reuses these rules, so a
follow-up of a follow-up does not recurse. A skip label alone
(`ai:security`) proves nothing: anyone
who can label an issue can add one (security finding
`mutable-label-skips-security-pass`, issue #4623). The skip is allowed only
when, for one skip label the issue carries:

  1. the issue author is the automation that files these issues: the
     `github-actions[bot]` Bot, or a User whose `author_association` is
     `OWNER` (the account whose GH_PAT the security audit uses);
  2. that author applied the label: its first `labeled` event came from the
     author within LABEL_AT_CREATION_WINDOW_SECONDS of the issue's creation,
     and no other account ever applied it;
  3. the body carries the marker line that label's producer writes
     (`<!-- ai:security-finding:<id> -->`);
  4. for `ai:security` only, the body's `Refs #T` names the audit tracker:
     an issue labelled `ai:security-audit` whose body carries
     `<!-- ai:security-audit-tracker:v1 -->` and whose author is the
     follow-up's author.

Triage- and workflow-heal-linked PRs always run the security pass. Anything
else, including a read failure, means the pass runs.

Usage:

  security_pass_skip.py --repo OWNER/REPO --issue N

Output is one JSON line: `skip` (bool), `label` (the verified label, or
null), and `reason`. Exit 0 when a decision was made (either way), 1 on bad
arguments, 2 when a GitHub read failed; exit 1 and 2 print `"skip": false`
and mean `Security pass: run`.

GitHub API budget (CLAUDE.md §15): REST only, at most three GETs. One for
the issue; one for the first 100-item page of its issue events (label
events, not comments; a full page is treated as unverifiable, so no second
page is read); and, for `ai:security` only, one for the tracker issue. An
issue with no skip label, or whose author is not the automation, costs one
call.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from typing import Any, Callable

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Checked in this order; the first label that verifies wins.
# ai:check-triage is intentionally absent: owner-filed triage issues derive
# from contributor-controlled failure logs, so author, label and marker do
# not establish that a fix is safe (triage-issue-launders-security-pass-exemption).
# ai:workflow-heal is also absent: default-branch CI, autofix_failure (PR
# review), and consumer escalation logs can be influenced by contributors
# (ci-heal-skips-security-pass).
SKIP_LABEL_MARKERS: dict[str, re.Pattern[str]] = {
	"ai:security": re.compile(r"(?m)^<!-- ai:security-finding:\S+ -->[ \t]*$"),
}
CI_HEAL_CONTEXT_RE = re.compile(r"(?m)^- \*\*Failed workflow:\*\* `CI` \(conclusion: `(failure|timed_out)`\)[ \t]*$")
SECURITY_LABEL = "ai:security"
TRACKER_LABEL = "ai:security-audit"
TRACKER_MARKER = "<!-- ai:security-audit-tracker:v1 -->"
TRACKER_REF_RE = re.compile(r"(?m)^Refs #([1-9][0-9]{0,9})[ \t]*$")
AUTOMATION_BOT_LOGIN = "github-actions[bot]"
LABEL_AT_CREATION_WINDOW_SECONDS = 120
EVENTS_PAGE_SIZE = 100


class ReadError(Exception):
	"""A `gh api` read failed; the caller runs the security pass."""


def _gh_get_json(path: str) -> Any:
	"""GET one REST path through `gh api` and return the decoded JSON value."""
	try:
		proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ReadError(f"gh api {path} failed: {exc}") from exc
	if proc.returncode != 0:
		# A whitespace-only stderr must not hide stdout's diagnostic.
		detail = ((proc.stderr or "").strip() or (proc.stdout or "").strip()).splitlines()
		raise ReadError(f"gh api {path} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	try:
		return json.loads(proc.stdout)
	except ValueError as exc:
		raise ReadError(f"gh api {path} returned invalid JSON") from exc


def _label_names(issue: dict[str, Any]) -> list[str]:
	names: list[str] = []
	for label in issue.get("labels") or []:
		name = label.get("name") if isinstance(label, dict) else label
		if isinstance(name, str) and name:
			names.append(name)
	return names


def _login(user: Any) -> str:
	if isinstance(user, dict) and isinstance(user.get("login"), str):
		return user["login"]
	return ""


def _timestamp(value: Any) -> datetime | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		return datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None


def _is_automation_author(issue: dict[str, Any]) -> bool:
	user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
	if user.get("type") == "Bot":
		return user.get("login") == AUTOMATION_BOT_LOGIN
	return user.get("type") == "User" and issue.get("author_association") == "OWNER"


def _label_applied_at_creation(issue: dict[str, Any], events: list[Any], label: str) -> str:
	"""Return "" when the author applied `label` at creation, else a reason."""
	author = _login(issue.get("user"))
	created = _timestamp(issue.get("created_at"))
	if created is None:
		return "issue has no readable created_at"
	applied = [
		event for event in events
		if isinstance(event, dict) and event.get("event") == "labeled"
		and isinstance(event.get("label"), dict) and event["label"].get("name") == label
	]
	if not applied:
		return f"no labeled event for {label}"
	for event in applied:
		if _login(event.get("actor")) != author:
			return f"{label} was applied by {_login(event.get('actor')) or 'an unknown account'}, not the issue author"
	# The earliest event by timestamp, not by list position, so the check does
	# not depend on the order the events endpoint returns.
	applied_at = [_timestamp(event.get("created_at")) for event in applied]
	if any(at is None for at in applied_at):
		return f"labeled event for {label} has no readable created_at"
	first_at = min(applied_at)
	delay = (first_at - created).total_seconds()
	if delay < 0:
		return f"labeled event for {label} is dated {int(-delay)}s before the issue's creation, not at creation"
	if delay > LABEL_AT_CREATION_WINDOW_SECONDS:
		return f"{label} was applied {int(delay)}s after creation, not at creation"
	return ""


def _tracker_problem(issue: dict[str, Any], tracker: dict[str, Any] | None, tracker_number: int) -> str:
	"""Return "" when `tracker` is the audit tracker for `issue`, else a reason."""
	if tracker is None:
		return f"#{tracker_number} could not be read"
	if "pull_request" in tracker:
		return f"#{tracker_number} is a pull request, not the audit tracker"
	if TRACKER_LABEL not in _label_names(tracker):
		return f"#{tracker_number} is not labelled {TRACKER_LABEL}"
	if TRACKER_MARKER not in (tracker.get("body") or ""):
		return f"#{tracker_number} has no audit tracker marker"
	if _login(tracker.get("user")) != _login(issue.get("user")):
		return f"#{tracker_number} was not created by the follow-up's author"
	return ""


def decide_security_pass_skip(
	issue: dict[str, Any],
	events: list[Any] | None,
	fetch_tracker: Callable[[int], dict[str, Any] | None],
) -> dict[str, Any]:
	"""Pure decision over already-fetched data.

	`issue` is the REST issue object, `events` the first page of its issue
	events (None when it could not be read), and `fetch_tracker(number)`
	returns the REST issue for a tracker number or None when that read
	failed. Returns `{"skip": bool, "label": str | None, "reason": str}`.
	"""
	labels = _label_names(issue)
	candidates = [label for label in SKIP_LABEL_MARKERS if label in labels]
	if not candidates:
		if "ai:workflow-heal" in labels and CI_HEAL_CONTEXT_RE.search(issue.get("body") or ""):
			return {"skip": False, "label": None, "reason": "CI workflow heal requires security pass"}
		return {"skip": False, "label": None, "reason": "no skip label"}
	# The intake writes this header from the failed run, before the untrusted
	# diagnosis. CI logs can be influenced by PRs, so an owner-filed heal issue
	# is not sufficient proof that its fix can bypass the security audit.
	if "ai:workflow-heal" in labels and CI_HEAL_CONTEXT_RE.search(issue.get("body") or ""):
		return {"skip": False, "label": None, "reason": "CI workflow heal requires security pass"}
	if "ai:workflow-heal" in labels:
		return {"skip": False, "label": None, "reason": "workflow heal requires security pass"}
	if "pull_request" in issue:
		return {"skip": False, "label": None, "reason": "not an issue"}
	if not _is_automation_author(issue):
		user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
		return {
			"skip": False,
			"label": None,
			"reason": (f"author {_login(user) or 'unknown'} ({user.get('type') or 'unknown type'}, "
				f"{issue.get('author_association') or 'no association'}) is not the issue automation"),
		}
	if events is None:
		return {"skip": False, "label": None, "reason": "issue events could not be read"}
	if len(events) >= EVENTS_PAGE_SIZE:
		return {"skip": False, "label": None, "reason": f"issue has {EVENTS_PAGE_SIZE}+ events; label history not verifiable in one page"}
	body = issue.get("body") or ""
	reasons: list[str] = []
	for label in candidates:
		problem = _label_applied_at_creation(issue, events, label)
		if not problem and not SKIP_LABEL_MARKERS[label].search(body):
			problem = f"body has no {label} automation marker"
		if not problem and label == SECURITY_LABEL:
			ref = TRACKER_REF_RE.search(body)
			if ref is None:
				problem = "body has no Refs #<tracker> line"
			else:
				tracker_number = int(ref.group(1))
				problem = _tracker_problem(issue, fetch_tracker(tracker_number), tracker_number)
		if not problem:
			return {"skip": True, "label": label, "reason": f"{label}: created and labelled by the issue automation"}
		reasons.append(f"{label}: {problem}")
	return {"skip": False, "label": None, "reason": "; ".join(reasons)}


def check_issue(repo: str, number: int) -> dict[str, Any]:
	"""Fetch what `decide_security_pass_skip` needs and return its decision."""
	if not REPO_RE.fullmatch(repo or ""):
		raise ValueError("--repo must be OWNER/REPO")
	if number <= 0:
		raise ValueError("--issue must be a positive integer")
	issue = _gh_get_json(f"repos/{repo}/issues/{number}")
	if not isinstance(issue, dict):
		raise ReadError(f"issue #{number} returned non-object JSON")
	issue_labels = _label_names(issue)
	if (not any(label in SKIP_LABEL_MARKERS for label in issue_labels) or "pull_request" in issue
			or not _is_automation_author(issue) or "ai:workflow-heal" in issue_labels):
		# Decided from the issue alone; the events and tracker are never read.
		return decide_security_pass_skip(issue, [], lambda _number: None)
	events = _gh_get_json(f"repos/{repo}/issues/{number}/events?per_page={EVENTS_PAGE_SIZE}")
	if not isinstance(events, list):
		raise ReadError(f"issue #{number} events returned non-array JSON")

	def fetch_tracker(tracker_number: int) -> dict[str, Any] | None:
		try:
			tracker = _gh_get_json(f"repos/{repo}/issues/{tracker_number}")
		except ReadError:
			return None
		return tracker if isinstance(tracker, dict) else None

	return decide_security_pass_skip(issue, events, fetch_tracker)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--repo", required=True)
	parser.add_argument("--issue", type=int, required=True)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		result = check_issue(args.repo, args.issue)
	except ValueError as exc:
		print(json.dumps({"skip": False, "label": None, "error": str(exc)}))
		return 1
	except ReadError as exc:
		print(json.dumps({"skip": False, "label": None, "error": str(exc)}))
		return 2
	print(json.dumps(result))
	return 0


if __name__ == "__main__":
	sys.exit(main())

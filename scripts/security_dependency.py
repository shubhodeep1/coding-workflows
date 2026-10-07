#!/usr/bin/env python3
"""Security dependency hold for generated security follow-up issues (issue #4934).

A security follow-up that `scripts/security_audit.sh` files for a file it
already filed a finding for carries one ``- Depends on: #N`` line. Clarify,
implement and the standalone stall poller hold such an issue until issue #N
is closed with ``ai:merged``, so the second fix never races the first.

Moved unchanged from ``scripts/claude_issue_route.py`` when the Claude session
automation it lived beside was retired
(``docs/plans/replace-claude-sessions-with-cli-engine-plan.md``, Phase 2). The
command line is the same:

  python3 scripts/security_dependency.py security-dependency \
      --issue-json <issue.json> --repo <owner/repo> --issue-number <n> [--number-only]

prints one JSON object, ``{"status": "none" | "ready" | "held", "reason": …,
"depends_on": n}``. Malformed or unverifiable declarations are ``held``
(fail closed). ``--number-only`` skips the prerequisite read. Otherwise the
command makes one REST read of issue #N (``gh api repos/<repo>/issues/<n>``)
and only when the issue declares a dependency.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SECURITY_DEPENDENCY_RE = re.compile(r"(?m)^- Depends on: #([1-9][0-9]{0,9})$")
SECURITY_DEPENDENCY_LINE_RE = re.compile(r"(?mi)^\s*-?\s*Depends\s+on\s*:")
SECURITY_FINDING_MARKER = "<!-- ai:security-finding:"


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


def security_dependency_number(issue: Any) -> int | None:
	"""Only generated security follow-ups may declare one same-repo dependency.

	Malformed or repeated declarations fail closed rather than being ignored.
	"""
	if not isinstance(issue, dict):
		return None
	body = issue.get("body")
	if not isinstance(body, str) or not SECURITY_DEPENDENCY_LINE_RE.search(body):
		return None
	if SECURITY_FINDING_MARKER not in body or "ai:security" not in _label_names(issue):
		raise ValueError("unverified security dependency")
	lines = SECURITY_DEPENDENCY_LINE_RE.findall(body)
	if not lines:
		return None
	matches = SECURITY_DEPENDENCY_RE.findall(body)
	if len(lines) != 1 or len(matches) != 1:
		raise ValueError("invalid or repeated security dependency")
	dependency_number = int(matches[0])
	if dependency_number == issue.get("number"):
		raise ValueError("self-referential security dependency")
	return dependency_number


def security_dependency_verdict(issue: Any, prerequisite: Any) -> dict[str, Any]:
	"""Return ready/held/none from a live same-repository issue snapshot."""
	try:
		dependency_number = security_dependency_number(issue)
	except ValueError as exc:
		return {"status": "held", "reason": str(exc)}
	if dependency_number is None:
		return {"status": "none", "reason": "no security dependency"}
	if not isinstance(prerequisite, dict) or prerequisite.get("number") != dependency_number or "pull_request" in prerequisite:
		return {"status": "held", "reason": "dependency missing or not an issue", "depends_on": dependency_number}
	if not isinstance(prerequisite.get("repository_url"), str) or prerequisite["repository_url"].lower() != issue.get("repository_url", "").lower() or not prerequisite["repository_url"]:
		return {"status": "held", "reason": "dependency repository mismatch", "depends_on": dependency_number}
	if prerequisite.get("state") not in ("open", "closed") or not isinstance(prerequisite.get("labels"), list):
		return {"status": "held", "reason": "dependency state or labels unreadable", "depends_on": dependency_number}
	if prerequisite["state"] == "closed" and "ai:merged" in _label_names(prerequisite):
		return {"status": "ready", "reason": "dependency merged", "depends_on": dependency_number}
	return {"status": "held", "reason": "dependency open" if prerequisite["state"] == "open" else "dependency closed without ai:merged", "depends_on": dependency_number}


def fetch_security_dependency(repo: str, dependency_number: int) -> Any:
	"""One conditional REST read; callers cache per (repo, number) per wake.

	Intake has only the dependent issue, queue binding reads producer runs, and
	the poller's candidate batch contains open issues, not a closed predecessor.
	None of those calls can prove the predecessor's current labels and state.
	"""
	try:
		response = subprocess.run(
			["gh", "api", f"repos/{repo}/issues/{dependency_number}"],
			capture_output=True, text=True, check=False,
		)
		if response.returncode == 0:
			return json.loads(response.stdout)
	except (OSError, ValueError):
		pass
	return None


def _read_json(path: str) -> Any:
	return json.loads(Path(path).read_text(encoding="utf-8"))


def _cmd_security_dependency(args: argparse.Namespace) -> int:
	try:
		issue = _read_json(args.issue_json)
		if not isinstance(issue, dict) or issue.get("number") != args.issue_number or issue.get("repository_url", "").lower() != f"https://api.github.com/repos/{args.repo}".lower() or "pull_request" in issue:
			raise ValueError("target issue identity mismatch")
		dependency_number = security_dependency_number(issue)
	except (OSError, ValueError, AttributeError):
		print(json.dumps({"status": "held", "reason": "invalid security dependency or issue metadata"}))
		return 0
	if dependency_number is None:
		print(json.dumps({"status": "none", "reason": "no security dependency"}))
		return 0
	if args.number_only:
		print(json.dumps({"status": "held", "reason": "dependency pending verification", "depends_on": dependency_number}))
		return 0
	prerequisite = fetch_security_dependency(args.repo, dependency_number)
	print(json.dumps(security_dependency_verdict(issue, prerequisite)))
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	p_dependency = sub.add_parser("security-dependency", help="check a generated security follow-up's prerequisite")
	p_dependency.add_argument("--issue-json", required=True)
	p_dependency.add_argument("--repo", required=True)
	p_dependency.add_argument("--issue-number", required=True, type=int)
	p_dependency.add_argument("--number-only", action="store_true")
	p_dependency.set_defaults(func=_cmd_security_dependency)
	args = parser.parse_args(argv)
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())

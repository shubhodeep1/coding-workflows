#!/usr/bin/env python3
"""Pull-request dependency hold for standalone issues (issue #7228).

A trusted standalone issue may declare that it waits for a pull request and a
fact on the default branch with exactly one line of each kind:

  - Depends on PR: #N
  - Requires on main: <path> contains <fixed string>

Clarify, plan, both implement gates and the standalone stall poller hold such
an issue until PR #N is merged into the repository default branch and
``<path>`` on that branch contains the string. The poller then resumes the
issue once with ``/reclarify`` and ``<!-- ai:pr-dependency-released:<n> -->``.

This is separate from ``scripts/security_dependency.py``: that hold accepts
only ``- Depends on: #N`` on generated security follow-ups, and its matcher
needs a colon directly after "on", so it never matches "Depends on PR:".

  python3 -I -B scripts/pr_dependency.py pr-dependency \
      --issue-json <issue.json> --repo <owner/repo> --issue-number <n> \
      [--trusted-login <login>] [--number-only]

prints one JSON object, ``{"status": "none" | "ready" | "held", "reason": …,
"depends_on_pr": n}``, and always exits 0. Anything malformed, repeated,
untrusted or unreadable is ``held`` (fail closed). The command imports only
the standard library, because the workflows run it with ``python3 -I -B``
from trusted support copies while ``GH_TOKEN`` holds ``GH_PAT``.

API calls, made only for an issue that declares a dependency:
  * one ``gh api repos/<repo>/pulls/<n>`` read;
  * one raw ``contents/<path>?ref=<default branch>`` read, only after the PR
    checks pass;
  * one ``gh api user`` read, only when the author association is not
    trusted and no ``--trusted-login`` was given.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

PR_DEPENDENCY_LINE_RE = re.compile(r"(?mi)^\s*-?\s*Depends\s+on\s+PR\s*:")
PR_DEPENDENCY_RE = re.compile(r"(?m)^- Depends on PR: #([1-9][0-9]{0,9})[ \t]*$")
REQUIRES_LINE_RE = re.compile(r"(?mi)^\s*-?\s*Requires\s+on\s+main\s*:")
REQUIRES_RE = re.compile(r"(?m)^- Requires on main: `?([^`\s]+?)`? contains `?([^`\n]+?)`?[ \t]*$")
PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
RELEASE_MARKER_FMT = "<!-- ai:pr-dependency-released:{n} -->"
MAX_CONTENT_BYTES = 5 * 1024 * 1024
MAX_NEEDLE_CHARS = 200
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$")
ORCHESTRATOR_MANAGED_LABEL = "ai:orchestrator-managed"
CONTENT_TOO_LARGE = object()


def _label_names(issue: dict[str, Any]) -> list[str]:
	names: list[str] = []
	for label in issue.get("labels") or []:
		name = label.get("name") if isinstance(label, dict) else label
		if isinstance(name, str) and name:
			names.append(name)
	return names


def _valid_path(path: str) -> bool:
	if not PATH_RE.match(path) or path.startswith("/") or path.endswith("/") or "//" in path:
		return False
	for segment in path.split("/"):
		if segment in ("", ".", "..") or segment.lower() == ".git":
			return False
	return True


def _valid_needle(needle: str) -> bool:
	if not 1 <= len(needle) <= MAX_NEEDLE_CHARS or needle != needle.strip():
		return False
	if "`" in needle or "<!--" in needle:
		return False
	return all(0x20 <= ord(ch) <= 0x7E for ch in needle)


def pr_dependency_declaration(issue: Any) -> dict[str, Any] | None:
	"""Parse the two declaration lines; None when the issue declares nothing.

	Raises ValueError(<reason token>) for any malformed, repeated or
	self-referential declaration so callers hold instead of ignoring it.
	"""
	if not isinstance(issue, dict):
		return None
	body = issue.get("body")
	if not isinstance(body, str):
		return None
	# Issue bodies typed in the GitHub web UI use CRLF line endings.
	body = body.replace("\r\n", "\n")
	dep_lines = PR_DEPENDENCY_LINE_RE.findall(body)
	req_lines = REQUIRES_LINE_RE.findall(body)
	if not dep_lines and not req_lines:
		return None
	if not dep_lines or not req_lines:
		raise ValueError("malformed_declaration")
	dep_matches = PR_DEPENDENCY_RE.findall(body)
	req_matches = REQUIRES_RE.findall(body)
	if len(dep_lines) != 1 or len(req_lines) != 1 or len(dep_matches) != 1 or len(req_matches) != 1:
		raise ValueError("repeated_or_malformed_declaration")
	path, needle = req_matches[0]
	if not _valid_path(path):
		raise ValueError("invalid_path")
	if not _valid_needle(needle):
		raise ValueError("invalid_required_string")
	pr_number = int(dep_matches[0])
	if pr_number == issue.get("number"):
		raise ValueError("self_reference")
	return {"pr": pr_number, "path": path, "needle": needle}


def is_trusted_author(issue: Any, trusted_login: str | None) -> bool | None:
	"""True for OWNER/MEMBER/COLLABORATOR or the pipeline login.

	None means the association is not trusted and the pipeline login is
	unknown, so the caller must resolve it before deciding.
	"""
	if not isinstance(issue, dict):
		return False
	if issue.get("author_association") in TRUSTED_ASSOCIATIONS:
		return True
	user = issue.get("user")
	login = user.get("login") if isinstance(user, dict) else None
	if not trusted_login:
		return None
	return isinstance(login, str) and bool(login) and login == trusted_login


def pr_dependency_verdict(
	issue: Any,
	decl: dict[str, Any],
	pull: Any,
	content: Any,
	repo: str,
	trusted: bool | None,
) -> dict[str, Any]:
	"""Pure verdict from already-read state. Only ``ready`` releases the hold."""
	n = decl["pr"]

	def held(reason: str) -> dict[str, Any]:
		return {"status": "held", "reason": reason, "depends_on_pr": n}

	if trusted is None:
		return held("identity_unavailable")
	if trusted is not True:
		return held("untrusted_author")
	if not isinstance(pull, dict):
		return held("pr_unreadable")
	if pull.get("number") != n:
		return held("pr_missing_or_not_pr")
	base = pull.get("base")
	base_repo = base.get("repo") if isinstance(base, dict) else None
	if not isinstance(base_repo, dict) or not isinstance(base_repo.get("full_name"), str) or base_repo["full_name"].lower() != repo.lower():
		return held("pr_repository_mismatch")
	if pull.get("merged") is not True or not pull.get("merged_at"):
		return held("pr_unmerged")
	default_branch = base_repo.get("default_branch")
	if not isinstance(default_branch, str) or not default_branch or base.get("ref") != default_branch:
		return held("pr_wrong_base")
	if content is CONTENT_TOO_LARGE:
		return held("content_too_large")
	if not isinstance(content, (bytes, bytearray)):
		return held("content_unreadable")
	try:
		text = bytes(content).decode("utf-8")
	except UnicodeDecodeError:
		return held("content_unreadable")
	if decl["needle"] not in text:
		return held("required_string_missing")
	return {"status": "ready", "reason": "dependency_satisfied", "depends_on_pr": n}


def fetch_pull(repo: str, pr_number: int) -> Any:
	"""One REST read of the declared PR; None on any failure.

	The poller's candidate batch `linked_pr` only covers PRs that close the
	issue, not a declared prerequisite PR, so this read cannot be reused.
	"""
	try:
		response = subprocess.run(
			["gh", "api", f"repos/{repo}/pulls/{pr_number}"],
			capture_output=True, text=True, check=False,
		)
		if response.returncode == 0:
			return json.loads(response.stdout)
	except (OSError, ValueError):
		pass
	return None


def fetch_default_branch_content(repo: str, path: str, ref: str) -> Any:
	"""Raw file bytes at `ref`; None when unreadable, a sentinel when too large."""
	endpoint = f"repos/{repo}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}"
	try:
		response = subprocess.run(
			["gh", "api", "-H", "Accept: application/vnd.github.raw+json", endpoint],
			capture_output=True, check=False,
		)
		if response.returncode != 0:
			return None
		if len(response.stdout) > MAX_CONTENT_BYTES:
			return CONTENT_TOO_LARGE
		return response.stdout
	except OSError:
		return None


def fetch_login() -> str:
	try:
		response = subprocess.run(
			["gh", "api", "user", "--jq", ".login"],
			capture_output=True, text=True, check=False,
		)
		if response.returncode == 0:
			login = response.stdout.strip()
			if LOGIN_RE.match(login):
				return login
	except OSError:
		pass
	return ""


def _read_json(path: str) -> Any:
	return json.loads(Path(path).read_text(encoding="utf-8"))


def evaluate(issue: dict[str, Any], decl: dict[str, Any], repo: str, trusted_login: str) -> dict[str, Any]:
	trusted = is_trusted_author(issue, trusted_login or None)
	if trusted is None:
		trusted = is_trusted_author(issue, fetch_login() or None)
	if trusted is not True:
		return pr_dependency_verdict(issue, decl, None, None, repo, trusted)
	pull = fetch_pull(repo, decl["pr"])
	preliminary = pr_dependency_verdict(issue, decl, pull, b"", repo, True)
	if preliminary["reason"] not in ("required_string_missing", "dependency_satisfied"):
		return preliminary
	ref = pull["base"]["repo"]["default_branch"]
	content = fetch_default_branch_content(repo, decl["path"], ref)
	return pr_dependency_verdict(issue, decl, pull, content, repo, True)


def _cmd_pr_dependency(args: argparse.Namespace) -> int:
	try:
		issue = _read_json(args.issue_json)
		if not isinstance(issue, dict) or issue.get("number") != args.issue_number or str(issue.get("repository_url", "")).lower() != f"https://api.github.com/repos/{args.repo}".lower() or "pull_request" in issue:
			raise ValueError("issue_identity_mismatch")
		if ORCHESTRATOR_MANAGED_LABEL in _label_names(issue):
			print(json.dumps({"status": "none", "reason": "orchestrator_managed"}))
			return 0
		decl = pr_dependency_declaration(issue)
	except ValueError as exc:
		reason = str(exc) if str(exc) in (
			"issue_identity_mismatch", "malformed_declaration", "repeated_or_malformed_declaration",
			"invalid_path", "invalid_required_string", "self_reference",
		) else "invalid_issue_metadata"
		print(json.dumps({"status": "held", "reason": reason}))
		return 0
	except (OSError, AttributeError, TypeError):
		print(json.dumps({"status": "held", "reason": "invalid_issue_metadata"}))
		return 0
	if decl is None:
		print(json.dumps({"status": "none", "reason": "no_pr_dependency"}))
		return 0
	if args.number_only:
		print(json.dumps({"status": "held", "reason": "pending verification", "depends_on_pr": decl["pr"]}))
		return 0
	try:
		verdict = evaluate(issue, decl, args.repo, args.trusted_login or "")
	except (KeyError, TypeError, AttributeError):
		verdict = {"status": "held", "reason": "pr_unreadable", "depends_on_pr": decl["pr"]}
	print(json.dumps(verdict))
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	p_dependency = sub.add_parser("pr-dependency", help="check a standalone issue's declared pull-request dependency")
	p_dependency.add_argument("--issue-json", required=True)
	p_dependency.add_argument("--repo", required=True)
	p_dependency.add_argument("--issue-number", required=True, type=int)
	p_dependency.add_argument("--trusted-login", default="")
	p_dependency.add_argument("--number-only", action="store_true")
	p_dependency.set_defaults(func=_cmd_pr_dependency)
	args = parser.parse_args(argv)
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())

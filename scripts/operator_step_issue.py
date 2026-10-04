#!/usr/bin/env python3
"""Keep the repository's `ai:operator-step` issue (docs/plans/replace-claude-sessions-with-cli-engine-plan.md, Q33).

Some gaps only a person can close: a missing secret or credential, a
repository variable to set, a release to tag, an operation the pipeline must
never perform by itself. The pipeline does not stop for them. Activation
verification (port P4) and the unblock judge's `operator_step` verdict record
each one here instead, and the work it belongs to stays safely off (a feature
flag that defaults off, or a placeholder env var named `*_UNSET_OPERATOR_STEP`)
until the operator acts.

There is one open `ai:operator-step` issue per repository. Its body starts
with `<!-- ai:operator-step:v1 -->`; each source is one issue comment opened
by `<!-- ai:operator-step:entry key=<key> -->`. Writing an existing key edits
that comment, while a new key creates one. Independent sources therefore do
not race through one issue-body read-modify-write.

Usage:

  operator_step_issue.py upsert --repo OWNER/REPO --key KEY --source TEXT --steps-file PATH

`--steps-file` holds a JSON array of `{"title": str, "instructions": str,
"dormant_until": str (optional)}`. `--key` is lower-case letters, digits and
`-` (for example `pr-123`, `project-45`, `unblock-77`).

Output is one JSON line: `issue`, `url`, `created`, `entries`. Exit 0 on
success, 1 on bad arguments, 2 when a GitHub call failed.

GitHub API budget (CLAUDE.md §15): one identity read, one list of open
`ai:operator-step` issues, one comment create and one comment listing, plus
an update and duplicate deletes when a key already exists. A missing tracker
adds a label lookup, issue creation and one issue re-list that resolves
concurrent creation races.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

LABEL = "ai:operator-step"
MARKER = "<!-- ai:operator-step:v1 -->"
TITLE = "Operator steps waiting"
ENTRY_RE = re.compile(r"^<!-- ai:operator-step:entry key=([a-z0-9][a-z0-9-]{0,63}) -->$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_BODY = 60000
MAX_STEPS = 20
MAX_FIELD = 2000
INTRO = (
	"The pipeline found steps only a person can take. The work they belong to keeps running "
	"where it can and stays off where it must until each step is done. Tick a step when you "
	"have done it; close this issue when nothing is left."
)


class UsageError(Exception):
	"""Bad arguments: exit 1."""


class ApiError(Exception):
	"""A GitHub call failed: exit 2."""


def _clean(value: object, limit: int = MAX_FIELD) -> str:
	if value is None:
		return ""
	if not isinstance(value, str):
		raise UsageError("step fields must be strings")
	text = value.replace("<!--", "").replace("-->", "").replace("\r", "")
	return text.strip()[:limit]


def _gh(args: list[str]) -> str:
	result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
	if result.returncode != 0:
		raise ApiError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
	return result.stdout


def _trusted(issue: dict, trusted_login: str) -> bool:
	user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
	return bool(trusted_login) and user.get("login") == trusted_login


def find_issue(issues: object, trusted_login: str) -> dict | None:
	"""The oldest open, trusted issue whose body starts with the marker."""
	if not isinstance(issues, list):
		return None
	candidates = [
		issue
		for issue in issues
		if isinstance(issue, dict)
		and "pull_request" not in issue
		and _trusted(issue, trusted_login)
		and str(issue.get("body") or "").split("\n", 1)[0].strip() == MARKER
	]
	return min(candidates, key=lambda issue: int(issue.get("number") or 0)) if candidates else None


def parse_entries(body: str) -> list[tuple[str, str]]:
	"""(key, section text) pairs in body order; text before the first entry is ignored."""
	entries: list[tuple[str, list[str]]] = []
	for line in body.split("\n"):
		match = ENTRY_RE.match(line.strip())
		if match:
			entries.append((match.group(1), [line.strip()]))
		elif entries:
			entries[-1][1].append(line)
	return [(key, "\n".join(lines).rstrip()) for key, lines in entries]


def render_entry(key: str, source: str, steps: list[dict]) -> str:
	lines = [f"<!-- ai:operator-step:entry key={key} -->", f"### {_clean(source, 300) or key}", ""]
	for step in steps:
		lines.append(f"- [ ] **{_clean(step.get('title'), 200) or 'Operator step'}**")
		instructions = _clean(step.get("instructions"))
		for instruction_line in instructions.split("\n"):
			if instruction_line.strip():
				lines.append(f"  {instruction_line.rstrip()}")
		dormant = _clean(step.get("dormant_until"), 300)
		if dormant:
			lines.append(f"  Stays off until then: `{dormant}`.")
	return "\n".join(lines).rstrip()


def render_body(entries: list[tuple[str, str]]) -> str:
	parts = [MARKER, "## Operator steps", "", INTRO]
	kept = list(entries)
	while True:
		body = "\n\n".join(parts + [text for _, text in kept]) + "\n"
		if len(body) <= MAX_BODY or len(kept) <= 1:
			return body[:MAX_BODY]
		kept.pop(0)


def load_entry_comments(repo: str, issue_number: int) -> list[dict]:
	"""Return all issue comments, flattening gh's --slurp pagination shape."""
	raw = _gh(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{issue_number}/comments?per_page=100"])
	try:
		pages = json.loads(raw or "[]")
	except ValueError as exc:
		raise ApiError(f"unreadable operator-step comments: {exc}") from exc
	if not isinstance(pages, list):
		raise ApiError("unreadable operator-step comments: expected an array")
	if pages and all(isinstance(page, list) for page in pages):
		return [comment for page in pages for comment in page if isinstance(comment, dict)]
	return [comment for comment in pages if isinstance(comment, dict)]


def matching_entry_comments(comments: list[dict], trusted_login: str, key: str) -> list[dict]:
	"""Trusted comments for one key, oldest first."""
	marker = f"<!-- ai:operator-step:entry key={key} -->"
	matches = [
		comment
		for comment in comments
		if _trusted(comment, trusted_login)
		and str(comment.get("body") or "").split("\n", 1)[0].strip() == marker
		and isinstance(comment.get("id"), int)
	]
	return sorted(matches, key=lambda comment: comment["id"])


def load_steps(path: str) -> list[dict]:
	try:
		steps = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise UsageError(f"cannot read --steps-file {path}: {exc}") from exc
	if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
		raise UsageError("--steps-file must hold a non-empty JSON array of objects")
	return steps[:MAX_STEPS]


def upsert(repo: str, key: str, source: str, steps: list[dict]) -> dict:
	if not REPO_RE.match(repo):
		raise UsageError(f"--repo must be OWNER/REPO, got {repo!r}")
	if not KEY_RE.match(key):
		raise UsageError(f"--key must be lower-case letters, digits and '-', got {key!r}")
	# The issue list carries authors, not the GH_PAT identity. Callers do not
	# always have an identity read (activation's is conditional on a fix marker).
	trusted_login = _gh(["api", "user", "--jq", ".login"]).strip()
	if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?", trusted_login):
		raise ApiError("could not verify the pipeline login")
	listing = _gh(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=30"])
	try:
		issues = json.loads(listing or "[]")
	except ValueError as exc:
		raise ApiError(f"unreadable issue list: {exc}") from exc
	existing = find_issue(issues, trusted_login)
	entry = render_entry(key, source, steps)
	created_tracker = False
	created_number: int | None = None
	if not existing:
		try:
			_gh(["api", f"repos/{repo}/labels/ai%3Aoperator-step"])
		except ApiError as exc:
			if "HTTP 404" not in str(exc):
				raise
			_gh(["api", f"repos/{repo}/labels", "-f", f"name={LABEL}", "-f", "color=fbca04", "-f", "description=Steps only a person can take; the pipeline continues and the gated work stays off until they are done"])
		try:
			created = json.loads(_gh(["api", f"repos/{repo}/issues", "-f", f"title={TITLE}", "-f", f"body={render_body([])}", "-f", f"labels[]={LABEL}"]) or "{}")
		except ValueError as exc:
			raise ApiError(f"unreadable operator-step issue creation response: {exc}") from exc
		created_number = created.get("number")
		if not isinstance(created_number, int):
			raise ApiError("operator-step issue creation returned no issue number")
		created_tracker = True
		try:
			refreshed = json.loads(_gh(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=30"]) or "[]")
		except ValueError as exc:
			raise ApiError(f"unreadable operator-step issue list after creation: {exc}") from exc
		existing = find_issue(refreshed, trusted_login)
		if not existing:
			raise ApiError("operator-step issue was not visible after creation")
		if int(existing["number"]) != created_number:
			_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{created_number}", "-f", "state=closed", "-f", "state_reason=not_planned"])

	number = int(existing["number"])
	try:
		created_comment = json.loads(_gh(["api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={entry}"]) or "{}")
	except ValueError as exc:
		raise ApiError(f"unreadable operator-step comment creation response: {exc}") from exc
	created_comment_id = created_comment.get("id")
	if not isinstance(created_comment_id, int):
		raise ApiError("operator-step comment creation returned no comment id")

	# Every write first appends an atomic proposal. The newest concurrent
	# proposal for one key updates the oldest durable comment and removes the
	# duplicates; writes for different keys never contend.
	comments = load_entry_comments(repo, number)
	matches = matching_entry_comments(comments, trusted_login, key)
	if matches and matches[-1]["id"] == created_comment_id:
		if matches[0]["id"] != created_comment_id:
			_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{matches[0]['id']}", "-f", f"body={entry}"])
		for duplicate in matches[1:]:
			try:
				_gh(["api", "-X", "DELETE", f"repos/{repo}/issues/comments/{duplicate['id']}"])
			except ApiError as exc:
				if "HTTP 404" not in str(exc):
					raise
	entry_keys = {entry_key for entry_key, _ in parse_entries(str(existing.get("body") or ""))}
	for comment in comments:
		if _trusted(comment, trusted_login):
			match = ENTRY_RE.match(str(comment.get("body") or "").split("\n", 1)[0].strip())
			if match:
				entry_keys.add(match.group(1))
	return {"issue": number, "url": existing.get("html_url", ""), "created": created_tracker, "entries": len(entry_keys)}


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	upsert_cmd = sub.add_parser("upsert")
	upsert_cmd.add_argument("--repo", required=True)
	upsert_cmd.add_argument("--key", required=True)
	upsert_cmd.add_argument("--source", required=True)
	upsert_cmd.add_argument("--steps-file", required=True)
	args = parser.parse_args(argv)
	try:
		result = upsert(args.repo, args.key, args.source, load_steps(args.steps_file))
	except UsageError as exc:
		print(json.dumps({"error": str(exc)}))
		return 1
	except ApiError as exc:
		print(json.dumps({"error": str(exc)}))
		return 2
	print(json.dumps(result))
	return 0


if __name__ == "__main__":
	sys.exit(main())

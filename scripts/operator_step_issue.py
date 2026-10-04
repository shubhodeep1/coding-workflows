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
with `<!-- ai:operator-step:v1 -->` and holds one section per source, each
opened by `<!-- ai:operator-step:entry key=<key> -->`. Writing a key that is
already there replaces its section, so a re-run never duplicates steps; a new
key is appended. When the body would pass GitHub's size limit, the oldest
sections are dropped first.

Usage:

  operator_step_issue.py upsert --repo OWNER/REPO --key KEY --source TEXT --steps-file PATH

`--steps-file` holds a JSON array of `{"title": str, "instructions": str,
"dormant_until": str (optional)}`. `--key` is lower-case letters, digits and
`-` (for example `pr-123`, `project-45`, `unblock-77`).

Output is one JSON line: `issue`, `url`, `created`, `entries`. Exit 0 on
success, 1 on bad arguments, 2 when a GitHub call failed.

GitHub API budget (CLAUDE.md §15): each bounded reconciliation attempt lists
open `ai:operator-step` issues, performs one canonical update or create, then
lists again to verify the source entry survived a concurrent writer. After a
create, a stale label listing falls back to one direct read of the known
issue number; failed verification retries with bounded backoff.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

LABEL = "ai:operator-step"
MARKER = "<!-- ai:operator-step:v1 -->"
TITLE = "Operator steps waiting"
ENTRY_RE = re.compile(r"^<!-- ai:operator-step:entry key=([a-z0-9][a-z0-9-]{0,63}) -->$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
MAX_BODY = 60000
MAX_STEPS = 20
MAX_FIELD = 2000
MAX_UPSERT_ATTEMPTS = 3
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
	try:
		result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
	except OSError as exc:
		raise ApiError(f"gh {args[0] if args else ''} failed: {exc}") from exc
	if result.returncode != 0:
		raise ApiError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
	return result.stdout


def _trusted(issue: dict) -> bool:
	user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
	login = str(user.get("login") or "")
	return login == "github-actions[bot]" or issue.get("author_association") in TRUSTED_ASSOCIATIONS


def find_issue(issues: object) -> dict | None:
	"""The oldest open, trusted issue whose body starts with the marker."""
	candidates = find_issues(issues)
	return candidates[0] if candidates else None


def find_issues(issues: object) -> list[dict]:
	"""All open, trusted tracker issues in issue-number order."""
	if not isinstance(issues, list):
		return []
	candidates = [
		issue
		for issue in issues
		if isinstance(issue, dict)
		and "pull_request" not in issue
		and _trusted(issue)
		and str(issue.get("body") or "").split("\n", 1)[0].strip() == MARKER
	]
	return sorted(candidates, key=lambda issue: int(issue.get("number") or 0))


def parse_entries(body: str) -> list[tuple[str, str]]:
	"""(key, section text) pairs in body order; text before the first entry is ignored."""
	entries: list[tuple[str, list[str]]] = []
	for line in body.replace("\r\n", "\n").split("\n"):
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
	parts = [MARKER, "## Operator steps", INTRO]
	kept = list(entries)
	while True:
		body = "\n\n".join(parts + [text for _, text in kept]) + "\n"
		if len(body.encode("utf-8")) <= MAX_BODY:
			return body
		if len(kept) <= 1:
			raise UsageError("operator-step entry exceeds the issue body limit")
		kept.pop(0)


def load_steps(path: str) -> list[dict]:
	try:
		steps = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise UsageError(f"cannot read --steps-file {path}: {exc}") from exc
	if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
		raise UsageError("--steps-file must hold a non-empty JSON array of objects")
	if len(steps) > MAX_STEPS:
		raise UsageError(f"--steps-file exceeds {MAX_STEPS} operator steps")
	return steps


def upsert(repo: str, key: str, source: str, steps: list[dict]) -> dict:
	if not REPO_RE.match(repo):
		raise UsageError(f"--repo must be OWNER/REPO, got {repo!r}")
	if not KEY_RE.match(key):
		raise UsageError(f"--key must be lower-case letters, digits and '-', got {key!r}")
	entry = render_entry(key, source, steps)
	created_number: int | None = None
	for _upsert_attempt in range(MAX_UPSERT_ATTEMPTS):
		listing = _gh(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=30"])
		try:
			issues = json.loads(listing or "[]")
		except ValueError as exc:
			raise ApiError(f"unreadable issue list: {exc}") from exc
		candidates = find_issues(issues)
		if not candidates and created_number is not None:
			# The label listing can lag behind a successful create. Read the
			# known issue directly rather than posting a duplicate on retry.
			try:
				created_issue = json.loads(_gh(["api", f"repos/{repo}/issues/{created_number}"]) or "{}")
			except ValueError as exc:
				raise ApiError(f"unreadable created issue #{created_number}: {exc}") from exc
			candidates = find_issues([created_issue])
			if not candidates:
				raise ApiError(f"created issue #{created_number} is not a trusted operator tracker")
		if candidates:
			existing = candidates[0]
			entries: list[tuple[str, str]] = []
			for candidate_issue in candidates:
				for entry_key, entry_text in parse_entries(str(candidate_issue.get("body") or "")):
					entries = [(seen_key, entry_text if seen_key == entry_key else seen_text) for seen_key, seen_text in entries]
					if not any(seen_key == entry_key for seen_key, _ in entries):
						entries.append((entry_key, entry_text))
			entries = [(entry_key, entry if entry_key == key else entry_text) for entry_key, entry_text in entries]
			if not any(entry_key == key for entry_key, _ in entries):
				entries.append((key, entry))
			body = render_body(entries)
			number = int(existing["number"])
			_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", f"body={body}"])
			for duplicate_issue in candidates[1:]:
				_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{int(duplicate_issue['number'])}", "-f", "state=closed"])
		else:
			body = render_body([(key, entry)])
			try:
				created = json.loads(_gh(["api", f"repos/{repo}/issues", "-f", f"title={TITLE}", "-f", f"body={body}", "-f", f"labels[]={LABEL}"]) or "{}")
			except ValueError as exc:
				raise ApiError(f"unreadable created issue: {exc}") from exc
			created_number = int(created.get("number") or 0) or None

		try:
			verification = json.loads(_gh(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=30"]) or "[]")
		except ValueError as exc:
			raise ApiError(f"unreadable verification issue list: {exc}") from exc
		verified_issue = find_issue(verification)
		if verified_issue:
			verified_entries = dict(parse_entries(str(verified_issue.get("body") or "")))
			if verified_entries.get(key) == entry and len(find_issues(verification)) == 1:
				return {
					"issue": int(verified_issue["number"]),
					"url": verified_issue.get("html_url", ""),
					"created": created_number == int(verified_issue["number"]),
					"entries": len(verified_entries),
				}
		if _upsert_attempt + 1 < MAX_UPSERT_ATTEMPTS:
			time.sleep(2 ** _upsert_attempt)
	raise ApiError(f"operator-step upsert did not converge after {MAX_UPSERT_ATTEMPTS} attempts")


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

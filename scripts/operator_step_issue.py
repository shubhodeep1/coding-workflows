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

GitHub API budget (CLAUDE.md §15): one list of open `ai:operator-step` issues,
then one update or one create.
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
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
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


def _trusted(issue: dict) -> bool:
	user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
	login = str(user.get("login") or "")
	return login.endswith("[bot]") or issue.get("author_association") in TRUSTED_ASSOCIATIONS


def find_issue(issues: object) -> dict | None:
	"""The oldest open, trusted issue whose body starts with the marker."""
	if not isinstance(issues, list):
		return None
	candidates = [
		issue
		for issue in issues
		if isinstance(issue, dict)
		and "pull_request" not in issue
		and _trusted(issue)
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
	listing = _gh(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=30"])
	try:
		issues = json.loads(listing or "[]")
	except ValueError as exc:
		raise ApiError(f"unreadable issue list: {exc}") from exc
	existing = find_issue(issues)
	entry = render_entry(key, source, steps)
	entries = parse_entries(str(existing.get("body") or "")) if existing else []
	if any(entry_key == key for entry_key, _ in entries):
		entries = [(entry_key, entry if entry_key == key else text) for entry_key, text in entries]
	else:
		entries.append((key, entry))
	body = render_body(entries)
	if existing:
		number = int(existing["number"])
		_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", f"body={body}"])
		return {"issue": number, "url": existing.get("html_url", ""), "created": False, "entries": len(parse_entries(body))}
	created = json.loads(_gh(["api", f"repos/{repo}/issues", "-f", f"title={TITLE}", "-f", f"body={body}", "-f", f"labels[]={LABEL}"]) or "{}")
	return {"issue": created.get("number"), "url": created.get("html_url", ""), "created": True, "entries": len(parse_entries(body))}


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

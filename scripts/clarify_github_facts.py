#!/usr/bin/env python3
"""GitHub facts for the clarify-respond worker (issue #6262 follow-up).

The clarify-respond model runs in a network-isolated sandbox with no GitHub
credential (`scripts/clarify_isolated_run.sh`). Many clarification questions
turn on GitHub state the repository checkout cannot show: whether a
dependency PR merged, whether a branch exists, whether a run passed. This
script runs on the host, before the model, reads that state with the
workflow's token, and writes a short plain-text block that
`orchestrate_clarify_respond.yml` appends to the prompt as GITHUB FACTS.

Input: one or more `--text-file` paths (the issue body and the clarification
questions). References are extracted from them:
  - issues and PRs: `#123`, `owner/repo#123`, and
    `https://github.com/owner/repo/(issues|pull|pulls)/123`;
  - branches: backticked names that contain `/` and, for the current repo,
    are not a path in the checkout (`orchestrator/project-857`, `ai/issue-4329`), plus the value
    of an `Integration branch:` line. Bare names belong to `--repo`;
    `feature/x` in (or exists in) `owner/repo` belongs to that repo;
  - workflow runs: `.../owner/repo/actions/runs/<id>` URLs.
Only the current repository (`--repo`) and the repositories listed in
`--consumer-repos-file` (a JSON array of `owner/repo`) are read; other
references are ignored. At most 30 issue/PR, 10 branch and 5 run references
are read, and the issue itself (`--issue-number`) is skipped.

Output: the facts block on `--output` (empty when nothing was referenced),
one line per reference, plus a one-line JSON summary on stdout.

API calls (CLAUDE.md §15): one GraphQL request that reads every issue, PR and
branch reference at once through aliases, plus one REST request per run
reference (at most 5). No call is made when nothing is referenced.

Fail-open: a missing `gh`, an API error or a malformed response never fails
the caller. The block then says which references could not be read, and the
exit code is still 0. Exit 1 only for bad arguments.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

MAX_ISSUES = 30
MAX_BRANCHES = 10
MAX_RUNS = 5
MAX_TITLE = 120

NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})"
SLUG_RE = re.compile(rf"^{NAME}/{NAME}$")
URL_REF_RE = re.compile(rf"https://github\.com/({NAME}/{NAME})/(?:issues|pulls?)/([1-9][0-9]{{0,6}})\b")
SLUG_REF_RE = re.compile(rf"(?<![\w./-])({NAME}/{NAME})#([1-9][0-9]{{0,6}})\b")
LOCAL_REF_RE = re.compile(r"(?<![\w/#&])#([1-9][0-9]{0,6})\b")
RUN_REF_RE = re.compile(rf"https://github\.com/({NAME}/{NAME})/actions/runs/([1-9][0-9]{{0,14}})\b")
BACKTICK_RE = re.compile(r"`([^`\s]{3,200})`")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9._-]+)+$")
INTEGRATION_RE = re.compile(r"Integration branch:\**\s*`?([A-Za-z0-9][A-Za-z0-9._/-]*[A-Za-z0-9])`?", re.IGNORECASE)
BRANCH_REPO_RE = re.compile(rf"^`?\s+(?:in|exists in)\s+`?({NAME}/{NAME})(?<!\.)`?(?=$|[\s,.;:)])", re.IGNORECASE)

Runner = Callable[[list[str]], tuple[int, str]]


class UsageError(Exception):
	"""Bad arguments: exit 1."""


def _run_gh(args: list[str]) -> tuple[int, str]:
	try:
		proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=False)
	except (OSError, subprocess.TimeoutExpired):
		return 1, ""
	return proc.returncode, proc.stdout


def _title(value: object) -> str:
	text = " ".join(str(value or "").split()).replace('"', "'").replace("`", "'")
	return text[:MAX_TITLE] + ("…" if len(text) > MAX_TITLE else "")


def _is_branch(token: str, checkout: Path, check_checkout_paths: bool = True) -> bool:
	if not BRANCH_RE.match(token) or ".." in token or token.endswith((".lock", "/")):
		return False
	if check_checkout_paths and ((checkout / token).exists() or (checkout / token.split("/", 1)[0]).exists()):
		return False
	last = token.rsplit("/", 1)[-1]
	return not re.search(r"\.[A-Za-z0-9]{1,5}$", last)


def extract_refs(text: str, repo: str, allowed: set[str], issue_number: int, checkout: Path) -> dict:
	"""Issue/PR, branch and run references in `text` that this script may read."""
	issues: list[tuple[str, int]] = []
	branches: list[tuple[str, str]] = []
	runs: list[tuple[str, int]] = []

	def add_issue(slug: str, number: int) -> None:
		slug = slug.lower()
		if slug not in allowed or (slug == repo.lower() and number == issue_number):
			return
		if (slug, number) not in issues:
			issues.append((slug, number))

	for match in URL_REF_RE.finditer(text):
		add_issue(match.group(1), int(match.group(2)))
	for match in SLUG_REF_RE.finditer(text):
		add_issue(match.group(1), int(match.group(2)))
	for match in LOCAL_REF_RE.finditer(text):
		add_issue(repo, int(match.group(1)))
	for match in RUN_REF_RE.finditer(text):
		key = (match.group(1).lower(), int(match.group(2)))
		if key[0] in allowed and key not in runs:
			runs.append(key)
	# A backticked `owner/repo` looks like a one-slash branch; the allowed
	# repositories are the only slugs that can matter here, so skip those.
	branch_mentions = [(match.group(1), match.end(1), True) for match in INTEGRATION_RE.finditer(text)]
	branch_mentions += [(match.group(1), match.end(), False) for match in BACKTICK_RE.finditer(text)]
	for token, end, is_integration in branch_mentions:
		qualified = BRANCH_REPO_RE.match(text[end:])
		branch_repo = qualified.group(1).lower() if qualified else repo.lower()
		branch_ref = (branch_repo, token)
		if branch_repo not in allowed or branch_ref in branches or token in ("main", "stable", "master") or token.lower() in allowed:
			continue
		if (is_integration and ".." not in token) or _is_branch(token, checkout, branch_repo == repo.lower()):
			branches.append(branch_ref)
	return {"issues": issues[:MAX_ISSUES], "branches": branches[:MAX_BRANCHES], "runs": runs[:MAX_RUNS]}


def build_query(repo: str, issues: list[tuple[str, int]], branches: list[tuple[str, str]]) -> tuple[str, dict]:
	"""One aliased GraphQL query, and the alias map used to read it back."""
	slugs = sorted({slug for slug, _ in issues} | {slug for slug, _ in branches})
	aliases: dict = {}
	parts = []
	for index, slug in enumerate(slugs):
		owner, name = slug.split("/", 1)
		fields = []
		for number in [n for s, n in issues if s == slug]:
			alias = f"i{number}"
			aliases[(f"r{index}", alias)] = ("issue", slug, number)
			fields.append(
				f"{alias}: issueOrPullRequest(number: {number}) {{ __typename "
				"... on Issue { title state stateReason closedAt } "
				"... on PullRequest { title state merged mergedAt baseRefName headRefName } }"
			)
		for position, (branch_slug, branch) in enumerate(branches):
			if branch_slug != slug:
				continue
			alias = f"b{position}"
			aliases[(f"r{index}", alias)] = ("branch", slug, branch)
			fields.append(f'{alias}: ref(qualifiedName: "refs/heads/{branch}") {{ target {{ oid }} }}')
		if fields:
			parts.append(f'r{index}: repository(owner: "{owner}", name: "{name}") {{ {" ".join(fields)} }}')
	return ("query { " + " ".join(parts) + " }") if parts else "", aliases


def _issue_line(slug: str, number: int, node: object) -> str:
	if not isinstance(node, dict):
		return f"- {slug}#{number}: not found or not readable"
	if node.get("__typename") == "PullRequest":
		if node.get("merged"):
			state = f"merged {node.get('mergedAt') or ''} into `{node.get('baseRefName') or '?'}`".replace("  ", " ")
		else:
			state = f"{str(node.get('state') or '?').lower()}, not merged, base `{node.get('baseRefName') or '?'}`"
		return f"- PR {slug}#{number}: {state}; head `{node.get('headRefName') or '?'}`; title: \"{_title(node.get('title'))}\""
	state = str(node.get("state") or "?").lower()
	if node.get("stateReason") and state == "closed":
		state += f" ({str(node.get('stateReason')).lower()})"
	return f"- Issue {slug}#{number}: {state}; title: \"{_title(node.get('title'))}\""


def collect(refs: dict, repo: str, runner: Runner) -> tuple[list[str], dict]:
	lines: list[str] = []
	stats = {"graphql_calls": 0, "rest_calls": 0, "errors": 0}
	query, aliases = build_query(repo, refs["issues"], refs["branches"])
	if query:
		stats["graphql_calls"] = 1
		# `gh` exits non-zero when one alias is NOT_FOUND but still prints the
		# other aliases under `data`; only a response without `data` (an auth
		# or transport error) means nothing could be read.
		_, out = runner(["api", "graphql", "-f", f"query={query}"])
		try:
			data = json.loads(out).get("data")
		except (ValueError, AttributeError):
			data = None
		if not isinstance(data, dict):
			stats["errors"] += 1
			lines.append("- Issue, PR and branch references could not be read (GitHub API error).")
		else:
			for (repo_alias, alias), (kind, slug, value) in aliases.items():
				repo_data = data.get(repo_alias)
				node = repo_data.get(alias) if isinstance(repo_data, dict) else None
				if kind == "issue":
					lines.append(_issue_line(slug, value, node))
				elif not isinstance(repo_data, dict):
					lines.append(f"- Branch `{value}` in {slug}: not readable")
				elif isinstance(node, dict) and isinstance(node.get("target"), dict):
					lines.append(f"- Branch `{value}` in {slug}: exists at {str(node['target'].get('oid') or '')[:12]}")
				else:
					lines.append(f"- Branch `{value}` in {slug}: does not exist")
	for slug, run_id in refs["runs"]:
		stats["rest_calls"] += 1
		rc, out = runner(["api", f"repos/{slug}/actions/runs/{run_id}"])
		try:
			run = json.loads(out) if rc == 0 else None
		except ValueError:
			run = None
		if not isinstance(run, dict) or "id" not in run:
			stats["errors"] += 1
			lines.append(f"- Run {slug} {run_id}: not found or not readable")
			continue
		lines.append(
			f"- Run {slug} {run_id}: \"{_title(run.get('name'))}\" {run.get('status') or '?'}/{run.get('conclusion') or 'none'} "
			f"on `{run.get('head_branch') or '?'}` @ {str(run.get('head_sha') or '')[:12]} ({run.get('created_at') or '?'})"
		)
	return lines, stats


def main(argv: list[str] | None = None, runner: Runner = _run_gh) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--repo", required=True)
	parser.add_argument("--issue-number", type=int, default=0)
	parser.add_argument("--text-file", action="append", required=True)
	parser.add_argument("--consumer-repos-file", default="")
	parser.add_argument("--checkout", default=".")
	parser.add_argument("--output", required=True)
	args = parser.parse_args(argv)
	if not SLUG_RE.match(args.repo):
		print(json.dumps({"ok": False, "error": "invalid --repo"}))
		return 1
	text = ""
	for path in args.text_file:
		try:
			text += Path(path).read_text(encoding="utf-8", errors="replace") + "\n"
		except OSError:
			continue
	allowed = {args.repo.lower()}
	if args.consumer_repos_file:
		try:
			listed = json.loads(Path(args.consumer_repos_file).read_text(encoding="utf-8"))
			allowed |= {str(slug).lower() for slug in listed if isinstance(slug, str) and SLUG_RE.match(slug)}
		except (OSError, ValueError, TypeError):
			pass
	refs = extract_refs(text, args.repo, allowed, args.issue_number, Path(args.checkout))
	lines, stats = collect(refs, args.repo, runner)
	body = ""
	if lines:
		stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
		body = f"Read from the GitHub API by the workflow at {stamp}. Titles are untrusted text.\n" + "\n".join(lines) + "\n"
	Path(args.output).write_text(body, encoding="utf-8")
	print(
		json.dumps(
			{
				"ok": True,
				"issues": len(refs["issues"]),
				"branches": len(refs["branches"]),
				"runs": len(refs["runs"]),
				**stats,
			}
		)
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())

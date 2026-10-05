#!/usr/bin/env python3
"""Keep coding-workflows' live `.claude/` copies in step with their templates.

`workflow-templates/.claude/` ships to consumer repos; this repo runs its own
copy under `.claude/`. The pipeline's editors may not edit `.claude/**`, so an
AI fix that changes a template leaves the live copy behind. That broke `main`
twice in two days (#6133 changed the template copy of the merged-PR guard,
#6176 four command templates), because parity tests compare the pairs.

Every template file with a live copy must match it byte for byte, except the
files `.github/ai/claude_template_divergence.json` lists as maintained
separately on purpose.

Subcommands:
  check   Exit 1 and list every pair that differs and is not allowlisted.
  plan    --before SHA --after SHA: print the relative paths (under `.claude/`)
          whose template changed in that push while the live copy did not,
          that differ now and are not allowlisted. These are the ones `sync`
          copies.
  sync    plan, then copy each template over its live copy, commit on
          SYNC_BRANCH (default `ai/sync-claude-live-copies`, recreated from the
          pushed commit), push it, and open a pull request into BASE_BRANCH
          unless one is already open from that branch. --dry-run stops after the
          copy.

`sync` runs from `.github/workflows/sync-claude-live-copies.yml` on pushes to
main that touch `workflow-templates/.claude/**`. It fails open: an unreachable `before` commit (first push,
force push, shallow history) syncs nothing and logs why; the CI parity test
still reports any drift on the next pull request.

API budget (CLAUDE.md §15): `sync` issues at most two REST calls per push,
and only when something needs syncing: one GET for an open PR from the branch
and, when there is none, one POST to open it.

Log lines start with `CLAUDE_LIVE_SYNC`.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PREFIX = "workflow-templates/.claude/"
LIVE_PREFIX = ".claude/"
ALLOWLIST_PATH = ".github/ai/claude_template_divergence.json"
DEFAULT_SYNC_BRANCH = "ai/sync-claude-live-copies"


def log(message: str) -> None:
	print(f"CLAUDE_LIVE_SYNC {message}", file=sys.stderr)


def load_divergent(root: Path) -> set[str]:
	data = json.loads((root / ALLOWLIST_PATH).read_text(encoding="utf-8"))
	divergent = data.get("divergent") if isinstance(data, dict) else None
	if not isinstance(divergent, dict):
		raise ValueError(f"{ALLOWLIST_PATH}: 'divergent' must be an object of path -> reason")
	return set(divergent)


def paired_paths(root: Path) -> list[str]:
	"""Relative paths (under `.claude/`) that exist as both a template and a live file."""
	template_root = root / TEMPLATE_PREFIX
	pairs: list[str] = []
	for template in sorted(template_root.rglob("*")):
		if not template.is_file():
			continue
		relative = template.relative_to(template_root).as_posix()
		if (root / LIVE_PREFIX / relative).is_file():
			pairs.append(relative)
	return pairs


def mismatched(root: Path) -> list[str]:
	divergent = load_divergent(root)
	return [
		relative
		for relative in paired_paths(root)
		if relative not in divergent
		and not filecmp.cmp(root / TEMPLATE_PREFIX / relative, root / LIVE_PREFIX / relative, shallow=False)
	]


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=check)


def changed_files(root: Path, before: str, after: str) -> set[str] | None:
	if not before or set(before) == {"0"}:
		log(f"skip reason=no_before_commit before={before or 'empty'}")
		return None
	if _git(root, "cat-file", "-e", f"{before}^{{commit}}", check=False).returncode != 0:
		log(f"skip reason=before_commit_unreachable before={before}")
		return None
	result = _git(root, "diff", "--name-only", before, after, check=False)
	if result.returncode != 0:
		log(f"skip reason=diff_failed before={before} after={after}")
		return None
	return {line for line in result.stdout.splitlines() if line}


def plan(root: Path, before: str, after: str) -> list[str]:
	changed = changed_files(root, before, after)
	if changed is None:
		return []
	drifted = set(mismatched(root))
	return [
		relative
		for relative in paired_paths(root)
		if relative in drifted
		and TEMPLATE_PREFIX + relative in changed
		and LIVE_PREFIX + relative not in changed
	]


def _gh_json(*args: str) -> object:
	result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=True)
	return json.loads(result.stdout or "null")


def sync(root: Path, before: str, after: str, *, dry_run: bool) -> int:
	paths = plan(root, before, after)
	if not paths:
		log(f"nothing_to_sync before={before} after={after}")
		return 0
	for relative in paths:
		shutil.copyfile(root / TEMPLATE_PREFIX / relative, root / LIVE_PREFIX / relative)
		log(f"copied path={LIVE_PREFIX}{relative}")
	if dry_run:
		return 0
	repository = os.environ.get("GITHUB_REPOSITORY", "")
	branch = os.environ.get("SYNC_BRANCH") or DEFAULT_SYNC_BRANCH
	base = os.environ.get("BASE_BRANCH") or "main"
	if not repository or "/" not in repository:
		log("error reason=missing_repository")
		return 1
	short_after = after[:12]
	message = (
		f"chore(.claude): sync live copies with their templates after {short_after}\n\n"
		"The pipeline's editors cannot edit .claude/**, so this push changed only\n"
		"the template copy of:\n\n"
		+ "".join(f"- .claude/{relative}\n" for relative in paths)
		+ "\nCopied by scripts/sync_claude_live_copies.py (sync-claude-live-copies.yml).\n"
	)
	_git(root, "checkout", "-B", branch)
	_git(root, "add", "--", *[LIVE_PREFIX + relative for relative in paths])
	_git(root, "-c", "user.name=github-actions[bot]", "-c", "user.email=github-actions[bot]@users.noreply.github.com", "commit", "-q", "-m", message)
	_git(root, "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
	owner = repository.split("/", 1)[0]
	open_prs = _gh_json(f"repos/{repository}/pulls?state=open&head={owner}:{branch}&per_page=1")
	if isinstance(open_prs, list) and open_prs:
		log(f"updated pr={open_prs[0].get('number')} branch={branch} paths={len(paths)}")
		return 0
	body = (
		"Syncs live `.claude/` copies with their `workflow-templates/.claude/` templates after "
		f"`{short_after}` changed only the template:\n\n"
		+ "".join(f"- `.claude/{relative}`\n" for relative in paths)
		+ "\nThe pipeline's editors cannot edit `.claude/**`, so a template-only change leaves this repo's own copy "
		"behind and fails the parity tests on every PR. Opened by `scripts/sync_claude_live_copies.py` from the "
		"`sync-claude-live-copies.yml` workflow.\n"
	)
	created = _gh_json(
		"-X", "POST", f"repos/{repository}/pulls",
		"-f", f"title=chore(.claude): sync live copies with their templates ({short_after})",
		"-f", f"head={branch}",
		"-f", f"base={base}",
		"-f", f"body={body}",
	)
	number = created.get("number") if isinstance(created, dict) else None
	log(f"opened pr={number} branch={branch} paths={len(paths)}")
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--root", default=str(REPO_ROOT))
	sub = parser.add_subparsers(dest="command", required=True)
	sub.add_parser("check")
	for name in ("plan", "sync"):
		command = sub.add_parser(name)
		command.add_argument("--before", required=True)
		command.add_argument("--after", required=True)
		if name == "sync":
			command.add_argument("--dry-run", action="store_true")
	args = parser.parse_args(argv)
	root = Path(args.root)
	if args.command == "check":
		drift = mismatched(root)
		for relative in drift:
			print(f"{LIVE_PREFIX}{relative} differs from {TEMPLATE_PREFIX}{relative}")
		return 1 if drift else 0
	if args.command == "plan":
		for relative in plan(root, args.before, args.after):
			print(relative)
		return 0
	return sync(root, args.before, args.after, dry_run=args.dry_run)


if __name__ == "__main__":
	raise SystemExit(main())

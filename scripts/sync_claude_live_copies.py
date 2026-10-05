#!/usr/bin/env python3
"""Keep coding-workflows' live `.claude/` copies in step with their templates.

`workflow-templates/.claude/` ships to consumer repos; this repo runs its own
copy under `.claude/`. The pipeline's editors may not edit `.claude/**`, so an
AI fix that changes a template leaves the live copy behind. That broke `main`
twice in two days (#6133 changed the template copy of the merged-PR guard,
#6176 four command templates), because parity tests compare the pairs.

Every template file must have a live copy. Copies not listed as intentionally
divergent in `.github/ai/claude_template_divergence.json` must match byte for
byte and in executable permissions.

Subcommands:
  check   Exit 1 and list missing or differing live files not allowlisted.
  plan    --before SHA --after SHA: print the relative paths (under `.claude/`)
          whose template changed in that push (or a missed earlier push) while
          the live copy did not, that differ now and are not allowlisted.
  sync    plan, carry forward still-drifted copies from the previous sync
          branch when they match their current templates, and commit the copies
          on SYNC_BRANCH (default `ai/sync-claude-live-copies`, recreated from
          the pushed commit). Lease-check the push and open a pull request into
          BASE_BRANCH unless one is already open. --dry-run stops after the copy.
          --keep-committed-security-paths with --dry-run leaves hooks and
          settings.json untouched so CI checks the committed live copies.

`sync` runs from `.github/workflows/sync-claude-live-copies.yml` on pushes to
main that touch `workflow-templates/.claude/**`. It fails open: an unreachable `before` commit (first push,
force push, shallow history) syncs nothing and logs why; the CI parity test
still reports any drift on the next pull request.

API budget (CLAUDE.md §15): `sync` issues at most two REST calls per push,
and only when something needs syncing: one GET for an open PR from the branch
into the base branch whose head repository is this repository, and, when
there is none, one POST to open it.

Log lines start with `CLAUDE_LIVE_SYNC`.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PREFIX = "workflow-templates/.claude/"
LIVE_PREFIX = ".claude/"
ALLOWLIST_PATH = ".github/ai/claude_template_divergence.json"
SECURITY_LIVE_PREFIXES = ("hooks/",)
SECURITY_LIVE_FILES = ("settings.json",)
DEFAULT_SYNC_BRANCH = "ai/sync-claude-live-copies"


def log(message: str) -> None:
	print(f"CLAUDE_LIVE_SYNC {message}", file=sys.stderr)


def load_divergent(root: Path) -> set[str]:
	data = json.loads((root / ALLOWLIST_PATH).read_text(encoding="utf-8"))
	divergent = data.get("divergent") if isinstance(data, dict) else None
	if not isinstance(divergent, dict):
		raise ValueError(f"{ALLOWLIST_PATH}: 'divergent' must be an object of path -> reason")
	if any(not isinstance(reason, str) or not reason.strip() for reason in divergent.values()):
		raise ValueError(f"{ALLOWLIST_PATH}: each divergent path needs a non-empty reason")
	return set(divergent)


def paired_paths(root: Path) -> list[str]:
	"""Template paths (under `.claude/`) expected to have a live file."""
	template_root = root / TEMPLATE_PREFIX
	if template_root.is_symlink() or template_root.parent.is_symlink():
		raise ValueError(f"unsafe template symlink: {template_root}")
	pairs: list[str] = []
	for template in sorted(template_root.rglob("*")):
		if template.is_symlink():
			raise ValueError(f"unsafe template symlink: {template}")
		if not template.is_file():
			continue
		relative = template.relative_to(template_root).as_posix()
		pairs.append(relative)
	return pairs


def mismatched(root: Path) -> list[str]:
	divergent = load_divergent(root)
	drift: list[str] = []
	for relative in paired_paths(root):
		if relative in divergent:
			continue
		if any((root / LIVE_PREFIX / part).is_symlink() for part in (Path(relative), *Path(relative).parents)):
			raise ValueError(f"unsafe live symlink: {relative}")
		if (
			not (root / LIVE_PREFIX / relative).is_file()
			or not filecmp.cmp(root / TEMPLATE_PREFIX / relative, root / LIVE_PREFIX / relative, shallow=False)
			or ((root / TEMPLATE_PREFIX / relative).stat().st_mode & 0o111)
			!= ((root / LIVE_PREFIX / relative).stat().st_mode & 0o111)
		):
			drift.append(relative)
	return drift


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
	paths: list[str] = []
	for relative in paired_paths(root):
		if relative not in drifted or LIVE_PREFIX + relative in changed:
			continue
		if TEMPLATE_PREFIX + relative in changed:
			paths.append(relative)
			continue
		# A queued push may be superseded or an earlier sync may fail. Only
		# recover drift when the template was changed after the live copy.
		template_revision = _git(root, "log", "-1", "--format=%H", after, "--", TEMPLATE_PREFIX + relative, check=False)
		live_revision = _git(root, "log", "-1", "--format=%H", after, "--", LIVE_PREFIX + relative, check=False)
		if template_revision.returncode != 0 or live_revision.returncode != 0:
			continue
		template_sha = template_revision.stdout.strip()
		live_sha = live_revision.stdout.strip()
		if template_sha and (not live_sha or (
			live_sha != template_sha
			and _git(root, "merge-base", "--is-ancestor", live_sha, template_sha, check=False).returncode == 0
		)):
			paths.append(relative)
	return paths


def _gh_json(*args: str) -> object:
	result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=True)
	return json.loads(result.stdout or "null")


def _is_sync_pr(pr: object, repository: str, branch: str, base: str) -> bool:
	if not isinstance(pr, dict):
		return False
	head = pr.get("head")
	pr_base = pr.get("base")
	if not isinstance(head, dict) or not isinstance(pr_base, dict) or not isinstance(head.get("repo"), dict):
		return False
	head_repo = head["repo"].get("full_name")
	return (
		isinstance(head_repo, str)
		and head_repo.casefold() == repository.casefold()
		and head.get("ref") == branch
		and pr_base.get("ref") == base
		and pr.get("state", "open") == "open"
	)


def is_security_live_path(relative: str) -> bool:
	return relative.startswith(SECURITY_LIVE_PREFIXES) or relative in SECURITY_LIVE_FILES


def sync(root: Path, before: str, after: str, *, dry_run: bool, keep_committed_security_paths: bool = False) -> int:
	paths = plan(root, before, after)
	if not paths:
		log(f"nothing_to_sync before={before} after={after}")
		return 0
	if dry_run and keep_committed_security_paths:
		kept_committed_security_paths = [relative for relative in paths if is_security_live_path(relative)]
		for relative in kept_committed_security_paths:
			log(f"dry_run_kept_committed path={LIVE_PREFIX}{relative} reason=security_path")
		paths = [relative for relative in paths if not is_security_live_path(relative)]
		if not paths:
			log(f"nothing_to_sync before={before} after={after} kept_committed={len(kept_committed_security_paths)}")
			return 0
	repository = os.environ.get("GITHUB_REPOSITORY", "")
	branch = os.environ.get("SYNC_BRANCH") or DEFAULT_SYNC_BRANCH
	base = os.environ.get("BASE_BRANCH") or "main"
	if not dry_run and (
		branch == base
		or (branch != DEFAULT_SYNC_BRANCH and not branch.startswith(DEFAULT_SYNC_BRANCH + "-"))
		or _git(root, "check-ref-format", "--branch", branch, check=False).returncode != 0
	):
		log("error reason=unsafe_sync_branch")
		return 1
	previous_sha = ""
	if not dry_run:
		remote_result = _git(root, "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}", check=False)
		if remote_result.returncode == 0:
			previous_sha = remote_result.stdout.partition("\t")[0]
			if (
				not re.fullmatch(r"[0-9a-f]{40,64}", previous_sha)
				or _git(root, "fetch", "--no-tags", "origin", f"refs/heads/{branch}", check=False).returncode != 0
				or _git(root, "rev-parse", "FETCH_HEAD", check=False).stdout.strip() != previous_sha
			):
				log("error reason=sync_branch_fetch_failed")
				return 1
			prior_paths = _git(root, "diff", "--name-only", f"{after}...FETCH_HEAD", "--", LIVE_PREFIX, check=False)
			main_paths = _git(root, "diff", "--name-only", f"FETCH_HEAD...{after}", "--", LIVE_PREFIX, check=False)
			if prior_paths.returncode != 0 or main_paths.returncode != 0:
				log("error reason=sync_branch_diff_failed")
				return 1
			drifted = set(mismatched(root))
			main_changed = set(main_paths.stdout.splitlines())
			for path in prior_paths.stdout.splitlines():
				if not path.startswith(LIVE_PREFIX):
					continue
				prior_relative = path[len(LIVE_PREFIX):]
				if prior_relative not in drifted or path in main_changed or prior_relative in paths:
					continue
				# Carry forward only a copy that the previous sync actually set to today's template.
				prior_copy = _git(root, "show", f"FETCH_HEAD:{path}", check=False)
				if prior_copy.returncode == 0 and prior_copy.stdout == (root / TEMPLATE_PREFIX / prior_relative).read_text(encoding="utf-8"):
					paths.append(prior_relative)
		elif remote_result.returncode != 2:
			log("error reason=sync_branch_lookup_failed")
			return 1
	for relative in paths:
		(root / LIVE_PREFIX / relative).parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(root / TEMPLATE_PREFIX / relative, root / LIVE_PREFIX / relative)
		log(f"copied path={LIVE_PREFIX}{relative}")
	if dry_run:
		return 0
	if not repository or "/" not in repository:
		log("error reason=missing_repository")
		return 1
	short_after = after[:12]
	message = (
		f"chore(.claude): sync live copies with their templates after {short_after}\n\n"
		"The pipeline's editors cannot edit .claude/**. Copy the templates for:\n\n"
		+ "".join(f"- .claude/{relative}\n" for relative in paths)
		+ "\nCopied by scripts/sync_claude_live_copies.py (sync-claude-live-copies.yml).\n"
	)
	try:
		_git(root, "checkout", "-B", branch)
		_git(root, "add", "--", *[LIVE_PREFIX + relative for relative in paths])
		_git(root, "-c", "user.name=github-actions[bot]", "-c", "user.email=github-actions[bot]@users.noreply.github.com", "commit", "-q", "-m", message)
	except subprocess.CalledProcessError:
		log("error reason=git_stage_failed")
		return 1
	try:
		_git(root, "push", f"--force-with-lease=refs/heads/{branch}:{previous_sha}", "origin", f"HEAD:refs/heads/{branch}")
	except subprocess.CalledProcessError:
		log("error reason=push_failed")
		return 1
	owner = repository.split("/", 1)[0]
	try:
		open_prs = _gh_json(
			f"repos/{repository}/pulls?state=open&head={quote(owner, safe='')}:{quote(branch, safe='/')}"
			f"&base={quote(base, safe='')}&per_page=100"
		)
	except (subprocess.CalledProcessError, OSError, ValueError):
		log("error reason=api_failed stage=lookup")
		return 1
	if not isinstance(open_prs, list):
		log("error reason=api_failed stage=lookup_invalid_response")
		return 1
	matches = [pr for pr in open_prs if _is_sync_pr(pr, repository, branch, base)]
	for pr in open_prs:
		if _is_sync_pr(pr, repository, branch, base):
			continue
		pr_number = pr.get("number") if isinstance(pr, dict) else None
		pr_number = pr_number if isinstance(pr_number, int) and not isinstance(pr_number, bool) else "unknown"
		if not isinstance(pr, dict) or not isinstance(pr.get("head"), dict) or not isinstance(pr.get("base"), dict):
			reason = "invalid_entry"
		elif (
			not isinstance(pr["head"].get("repo"), dict)
			or not isinstance(pr["head"]["repo"].get("full_name"), str)
			or pr["head"]["repo"]["full_name"].casefold() != repository.casefold()
		):
			reason = "head_repo_mismatch"
		elif pr["head"].get("ref") != branch:
			reason = "head_ref_mismatch"
		elif pr["base"].get("ref") != base:
			reason = "base_mismatch"
		else:
			reason = "invalid_entry"
		log(f"ignored_pr pr={pr_number} reason={reason}")
	if matches:
		log(f"updated pr={matches[0].get('number')} branch={branch} base={base} paths={len(paths)}")
		return 0
	body = (
		"Syncs live `.claude/` copies with their `workflow-templates/.claude/` templates "
		f"after `{short_after}`:\n\n"
		+ "".join(f"- `.claude/{relative}`\n" for relative in paths)
		+ "\nThe pipeline's editors cannot edit `.claude/**`, so a template-only change leaves this repo's own copy "
		"behind and fails the parity tests on every PR. Opened by `scripts/sync_claude_live_copies.py` from the "
		"`sync-claude-live-copies.yml` workflow.\n"
	)
	try:
		created = _gh_json(
			"-X", "POST", f"repos/{repository}/pulls",
			"-f", f"title=chore(.claude): sync live copies with their templates ({short_after})",
			"-f", f"head={branch}",
			"-f", f"base={base}",
			"-f", f"body={body}",
		)
	except (subprocess.CalledProcessError, OSError, ValueError):
		log("error reason=api_failed stage=create")
		return 1
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
			command.add_argument("--keep-committed-security-paths", action="store_true")
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
	if args.keep_committed_security_paths and not args.dry_run:
		parser.error("--keep-committed-security-paths requires --dry-run")
	return sync(root, args.before, args.after, dry_run=args.dry_run, keep_committed_security_paths=args.keep_committed_security_paths)


if __name__ == "__main__":
	raise SystemExit(main())

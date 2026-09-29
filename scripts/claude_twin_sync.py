#!/usr/bin/env python3
"""Copy workflow-templates/.claude/** into .claude/** through one sync PR.

CLAUDE.md §28.C (issue #4785). Claude Code never auto-approves an edit under
`.claude/**`, so unattended sessions edit only the unprotected twin,
`workflow-templates/.claude/**`. Once a twin change is on the default branch,
`.github/workflows/claude-twin-sync.yml` runs this script, which copies each
changed twin file into `.claude/**` through one pull request on
`claude/claude-twin-sync-<sha>`:

* A sync PR that changes only non-guard files (commands, scripts) is merged
  by the workflow once every check run on its head has passed (CI's `lint`
  run included) and every changed file equals the default branch's twin.
* A sync PR that touches a guard path (`.claude/hooks/**`,
  `.claude/settings.json`, `.claude/settings.local.json`) or lists a conflict
  is labelled `ai:claude-sync-approval` and alerted. This script never
  approves, merges, or enables auto-merge on it: the repository owner reviews
  and merges it. The `claude-twin-sync/owner-approval` commit status reports
  whether the owner's latest review approves the current head.

Subcommands:
  plan         classify every twin file against `.claude/` at a ref (JSON)
  check        fail when a commit range moves a `.claude/` file anywhere but
               its twin (the CI sync-state check: `.claude/` is never ahead)
  merge-check  decide whether a sync PR's file list is a pure non-guard sync
  run          the workflow driver: plan, commit, push, open or update the
               PR, label, status, and merge a non-guard PR whose checks passed

Classification (`plan`), per twin file outside UPSTREAM_ONLY_PATHS:
  copy      `.claude/<file>` is missing, has another mode, or equals a version
            the twin had in its git history (it is behind)
  conflict  `.claude/<file>` matches no twin version (an operator changed it
            directly) or either side is a symlink; it is never overwritten
  equal     nothing to do
  rejected  an unsafe path (absolute, `..`, `.`, empty segment, backslash)

Batching contract (CLAUDE.md §15), per `run`:
  input   a full-history checkout of the default branch;
  calls   reads (the workflow's GITHUB_TOKEN): 1 open-PR list per 100 PRs;
          for a PR that needs the owner's approval 1 review list per 100
          reviews; 1 combined-status read for a head this run did not push
          (a merge attempt reuses it, and reads it again only after the
          status changed); for a merge attempt 1 file list per 100 files and
          1 check-run list per 100 runs. Writes (GH_PAT): 1 push, 1 PR create
          or body edit, at most 2 label writes, 1 status only when its state
          or description changes (GitHub allows 1000 per sha and context),
          1 merge, and 1 close plus 1 comment per stale, duplicate, or
          orphaned (head branch deleted) sync PR. Git: 1 `ls-remote` for the
          open sync PRs' branches when there is something to sync;
  output  one JSON summary line on stdout, `CLAUDE_TWIN_SYNC` log lines on
          stderr;
  failure fail closed: any read or git error exits 2 before a merge. Nothing
          is retried in a loop; the next hourly run starts over.

No GraphQL, no session environment variables (CLAUDE.md §23.E / §24.G).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

TWIN_ROOT = "workflow-templates/.claude"
CLAUDE_ROOT = ".claude"
# Files that are not twins: the five commands whose workflow-templates copy is
# the consumer-repo edition, and the upstream-only pickup command. They are
# never synced and are exempt from the sync-state check; editing their
# `.claude/` copy still needs an operator-watched session (CLAUDE.md §28.C).
UPSTREAM_ONLY_PATHS = frozenset({
	"commands/analyze-log.md",
	"commands/claude-issue-pickup.md",
	"commands/deploy-activate.md",
	"commands/investigate-issue.md",
	"commands/validate-consumer-issue.md",
	"commands/verify-activation.md",
})
# The guards that limit what sessions can do. A sync PR touching one merges
# only by the repository owner's hand.
GUARD_PATH_PREFIXES = ("hooks/",)
GUARD_PATH_FILES = frozenset({"settings.json", "settings.local.json"})
SYNC_BRANCH_PREFIX = "claude/claude-twin-sync-"
APPROVAL_LABEL = "ai:claude-sync-approval"
APPROVAL_LABEL_COLOR = "b60205"
APPROVAL_LABEL_DESCRIPTION = "Claude twin sync PR with hook/settings changes or a conflict; only the repo owner merges it"
APPROVAL_STATUS_CONTEXT = "claude-twin-sync/owner-approval"
REQUIRED_CHECK_NAME = "lint"
GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
HISTORY_LIMIT = 500
MAX_PAGES = 20
REGULAR_MODES = frozenset({"100644", "100755"})
ZERO_SHA = "0" * 40
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_IDENTITY = {
	"GIT_AUTHOR_NAME": "claude-twin-sync",
	"GIT_AUTHOR_EMAIL": "claude-twin-sync@users.noreply.github.com",
	"GIT_COMMITTER_NAME": "claude-twin-sync",
	"GIT_COMMITTER_EMAIL": "claude-twin-sync@users.noreply.github.com",
}


class SyncError(Exception):
	"""A git or GitHub read failed; the caller fails closed (exit 2)."""


def log(message: str) -> None:
	"""One stderr line; line breaks in `message` (a path) cannot start a workflow command."""
	message = message.replace("\r", "\\r").replace("\n", "\\n")
	print(f"CLAUDE_TWIN_SYNC {message}", file=sys.stderr, flush=True)


def escape_command_data(value: str) -> str:
	"""Escape a GitHub workflow command message (`%`, CR, LF)."""
	return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_command_property(value: str) -> str:
	"""Escape a GitHub workflow command property value (data escapes plus `:` and `,`)."""
	return escape_command_data(value).replace(":", "%3A").replace(",", "%2C")


# --- paths -------------------------------------------------------------------


def unsafe_path_reason(rel: str) -> str | None:
	"""Why `rel` (relative to a twin root) is unsafe, or None when it is safe."""
	if not rel:
		return "empty path"
	if "\\" in rel or "\x00" in rel:
		return "backslash or NUL in path"
	if any(ord(char) < 0x20 or char == "\x7f" for char in rel):
		return "control character in path"
	if rel.startswith("/"):
		return "absolute path"
	for segment in rel.split("/"):
		if segment in ("", ".", ".."):
			return f"unsafe segment {segment!r}"
	return None


def is_guard_path(rel: str) -> bool:
	"""True for `.claude/` paths that change what sessions may do."""
	return rel in GUARD_PATH_FILES or any(rel.startswith(prefix) for prefix in GUARD_PATH_PREFIXES)


# --- git ---------------------------------------------------------------------


def run_git(repo: str, args: list[str], *, env: dict | None = None, check: bool = True) -> str:
	"""Run git in `repo` with an argument list (never a shell) and return stdout."""
	merged_env = {**os.environ, **(env or {})}
	proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=merged_env)
	if check and proc.returncode != 0:
		raise SyncError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[:500]}")
	return proc.stdout


def is_shallow(repo: str) -> bool:
	return run_git(repo, ["rev-parse", "--is-shallow-repository"]).strip() == "true"


def resolve_commit(repo: str, ref: str) -> str:
	sha = run_git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False).strip()
	if not SHA_RE.fullmatch(sha):
		raise SyncError(f"cannot resolve {ref!r} to a commit")
	return sha


class TreeEntry(NamedTuple):
	mode: str
	kind: str
	sha: str


def list_tree(repo: str, ref: str, root: str) -> dict[str, TreeEntry]:
	"""Every entry under `root` at `ref`, keyed by its path relative to `root`."""
	out = run_git(repo, ["ls-tree", "-r", "-z", "--full-tree", ref, "--", root])
	entries: dict[str, TreeEntry] = {}
	prefix = root.rstrip("/") + "/"
	for record in out.split("\x00"):
		if not record:
			continue
		meta, _, path = record.partition("\t")
		mode, kind, sha = meta.split(" ")
		if not path.startswith(prefix):
			continue
		entries[path[len(prefix):]] = TreeEntry(mode, kind, sha)
	return entries


def blob_at(repo: str, ref: str, path: str) -> TreeEntry | None:
	"""The tree entry for `path` at `ref`, or None when it does not exist."""
	out = run_git(repo, ["ls-tree", "-z", "--full-tree", ref, "--", path])
	for record in out.split("\x00"):
		if not record:
			continue
		meta, _, found = record.partition("\t")
		if found == path:
			mode, kind, sha = meta.split(" ")
			return TreeEntry(mode, kind, sha)
	return None


def twin_history_blobs(repo: str, ref: str, rel: str) -> set[str]:
	"""Every blob the twin path held in its last HISTORY_LIMIT changes up to `ref`.

	One `git log --raw` call; both sides of every change are collected, so the
	version a change replaced counts too. `-m` adds each merge commit's diff
	against every parent: a version first produced by a merge (a default-branch
	sync into a project branch that both edited the twin) is a twin version too.
	"""
	out = run_git(repo, [
		"log", "-m", "--format=", "--raw", "--no-abbrev", "--no-renames",
		f"-n{HISTORY_LIMIT}", ref, "--", f"{TWIN_ROOT}/{rel}",
	])
	blobs: set[str] = set()
	for line in out.splitlines():
		if not line.startswith(":"):
			continue
		fields = line[1:].split("\t", 1)[0].split(" ")
		if len(fields) >= 4:
			for sha in fields[2:4]:
				if SHA_RE.fullmatch(sha) and sha != ZERO_SHA:
					blobs.add(sha)
	return blobs


# --- plan --------------------------------------------------------------------


def plan_sync(repo: str, ref: str) -> dict:
	"""Classify every twin file against `.claude/` at `ref` (see module doc)."""
	if is_shallow(repo):
		raise SyncError("shallow clone: the conflict rule needs full history (fetch-depth: 0)")
	ref_sha = resolve_commit(repo, ref)
	twins = list_tree(repo, ref_sha, TWIN_ROOT)
	claude = list_tree(repo, ref_sha, CLAUDE_ROOT)
	copies: list[dict] = []
	conflicts: list[dict] = []
	rejected: list[dict] = []
	excluded: list[str] = []
	for rel in sorted(twins):
		twin = twins[rel]
		if rel in UPSTREAM_ONLY_PATHS:
			excluded.append(rel)
			continue
		reason = unsafe_path_reason(rel)
		if reason:
			rejected.append({"path": rel, "reason": reason})
			continue
		if twin.kind != "blob" or twin.mode not in REGULAR_MODES:
			conflicts.append({"path": rel, "reason": f"twin is not a regular file (mode {twin.mode})"})
			continue
		local = claude.get(rel)
		if local is None:
			copies.append({"path": rel, "mode": twin.mode, "blob": twin.sha, "reason": "missing"})
		elif local.kind != "blob" or local.mode not in REGULAR_MODES:
			conflicts.append({"path": rel, "reason": f".claude copy is not a regular file (mode {local.mode})"})
		elif local.sha == twin.sha:
			if local.mode != twin.mode:
				copies.append({"path": rel, "mode": twin.mode, "blob": twin.sha, "reason": "mode"})
		elif local.sha in twin_history_blobs(repo, ref_sha, rel):
			copies.append({"path": rel, "mode": twin.mode, "blob": twin.sha, "reason": "behind"})
		else:
			conflicts.append({"path": rel, "reason": ".claude copy matches no twin version (changed directly)"})
	guard_paths = sorted({item["path"] for item in copies + conflicts if is_guard_path(item["path"])})
	return {
		"ref": ref_sha,
		"copies": copies,
		"conflicts": conflicts,
		"rejected": rejected,
		"excluded": excluded,
		"guard": bool(guard_paths),
		"guard_paths": guard_paths,
		"needs_owner": bool(guard_paths or conflicts),
	}


def build_tree(repo: str, ref: str, copies: list[dict]) -> str:
	"""The tree of `ref` with each copy written to `.claude/<path>`.

	Works on a temporary index, never on the working tree, so no path reaches
	the filesystem. Every path was validated by `plan_sync`.
	"""
	with tempfile.TemporaryDirectory() as tmp:
		env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
		run_git(repo, ["read-tree", ref], env=env)
		for item in copies:
			rel = item["path"]
			if unsafe_path_reason(rel) or item["mode"] not in REGULAR_MODES or not SHA_RE.fullmatch(item["blob"]):
				raise SyncError(f"refusing unsafe copy {rel!r}")
			run_git(repo, ["update-index", "--add", "--cacheinfo", f"{item['mode']},{item['blob']},{CLAUDE_ROOT}/{rel}"], env=env)
		return run_git(repo, ["write-tree"], env=env).strip()


def tree_of(repo: str, commit: str) -> str:
	return run_git(repo, ["rev-parse", f"{commit}^{{tree}}"]).strip()


def claude_tree_of(repo: str, treeish: str) -> str:
	"""The tree id of `.claude/` in `treeish` (a commit or a tree), or "" without one."""
	return run_git(repo, ["rev-parse", "--verify", "--quiet", f"{treeish}:{CLAUDE_ROOT}"], check=False).strip()


def changes_outside_claude(repo: str, ref: str, head: str) -> list[str]:
	"""Paths outside `.claude/` that `head` changed since its merge-base with `ref`.

	A sync head built by this script carries only `.claude/` copies, so any
	other path means a foreign commit on the sync branch. Without a merge-base
	(unrelated histories) nothing can be compared, and the head counts as
	foreign.
	"""
	base = run_git(repo, ["merge-base", ref, head], check=False).strip()
	if not base:
		return ["(no merge-base with the default branch)"]
	out = run_git(repo, ["diff", "--name-only", "--no-renames", "-z", base, head])
	prefix = CLAUDE_ROOT + "/"
	return sorted(p for p in out.split("\x00") if p and not p.startswith(prefix))


def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
	proc = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", ancestor, descendant], capture_output=True)
	return proc.returncode == 0


def commit_tree(repo: str, tree: str, parents: list[str], message: str) -> str:
	args = ["commit-tree", tree]
	for parent in parents:
		args += ["-p", parent]
	args += ["-m", message]
	return run_git(repo, args, env=COMMIT_IDENTITY).strip()


# --- check (CI sync-state) ---------------------------------------------------


def check_not_ahead(repo: str, base: str, head: str) -> dict:
	"""Every `.claude/` file changed in base..head must equal its twin at head.

	The twin may be ahead of `.claude/` (a change awaiting its sync PR); a
	`.claude/` file may never move anywhere but its twin. Upstream-only files
	are exempt. Both copies missing counts as equal (a synced deletion).
	"""
	out = run_git(repo, ["diff", "--name-only", "--no-renames", "-z", base, head, "--", CLAUDE_ROOT])
	violations: list[dict] = []
	checked: list[str] = []
	prefix = CLAUDE_ROOT + "/"
	for path in sorted(p for p in out.split("\x00") if p):
		if not path.startswith(prefix):
			continue
		rel = path[len(prefix):]
		if rel in UPSTREAM_ONLY_PATHS:
			continue
		reason = unsafe_path_reason(rel)
		if reason:
			violations.append({"path": path, "reason": reason})
			continue
		checked.append(path)
		local = blob_at(repo, head, path)
		twin = blob_at(repo, head, f"{TWIN_ROOT}/{rel}")
		if local is None and twin is None:
			continue
		if twin is None:
			violations.append({"path": path, "reason": f"has no twin at {TWIN_ROOT}/{rel}; add the file to the twin and let the sync PR copy it, or delete both copies together"})
		elif local is None:
			violations.append({"path": path, "reason": f"deleted while {TWIN_ROOT}/{rel} still exists; the sync never copies a deletion, so delete both copies together"})
		elif local.sha != twin.sha:
			violations.append({"path": path, "reason": f"changed to content that differs from {TWIN_ROOT}/{rel}; edit the twin and let the sync PR copy it"})
	return {"base": base, "head": head, "checked": checked, "violations": violations, "ok": not violations}


# --- merge rules -------------------------------------------------------------


def merge_check(repo: str, ref: str, head: str, pr_files: list[dict]) -> dict:
	"""Whether a sync PR's diff is a pure, non-guard copy of the twins at `ref`.

	Every changed file must be `added` or `modified` under `.claude/`, outside
	UPSTREAM_ONLY_PATHS and the guard paths, with the head blob equal to the
	twin blob at `ref`. Anything else (a twin edit on the sync branch, a guard
	path, a removal or rename) refuses the merge.
	"""
	reasons: list[str] = []
	if not pr_files:
		reasons.append("the PR changes no file")
	prefix = CLAUDE_ROOT + "/"
	for item in pr_files:
		path = item.get("filename") or ""
		status = item.get("status") or ""
		if status not in ("added", "modified"):
			reasons.append(f"{path}: status {status or 'unknown'}")
			continue
		if not path.startswith(prefix):
			reasons.append(f"{path}: outside {CLAUDE_ROOT}/")
			continue
		rel = path[len(prefix):]
		unsafe = unsafe_path_reason(rel)
		if unsafe:
			reasons.append(f"{path}: {unsafe}")
		elif rel in UPSTREAM_ONLY_PATHS:
			reasons.append(f"{path}: upstream-only file")
		elif is_guard_path(rel):
			reasons.append(f"{path}: guard path needs the owner")
		else:
			local = blob_at(repo, head, path)
			twin = blob_at(repo, ref, f"{TWIN_ROOT}/{rel}")
			if local is None or twin is None or local.sha != twin.sha or local.mode != twin.mode:
				reasons.append(f"{path}: differs from {TWIN_ROOT}/{rel} on the default branch")
	return {"ok": not reasons, "reasons": reasons}


def checks_green(check_runs: list[dict], combined_state: str, status_count: int, own_run_id: str) -> tuple[bool, str]:
	"""Every check run on the head completed green and CI's `lint` run passed.

	Runs of this workflow run (`own_run_id`) are ignored. Commit statuses, when
	any exist, must combine to `success`.
	"""
	lint_passed = False
	marker = f"/actions/runs/{own_run_id}/" if own_run_id else None
	for run in check_runs:
		if marker and marker in (run.get("details_url") or ""):
			continue
		name = run.get("name") or "?"
		if run.get("status") != "completed":
			return False, f"check {name} is {run.get('status') or 'unknown'}"
		if run.get("conclusion") not in GREEN_CONCLUSIONS:
			return False, f"check {name} concluded {run.get('conclusion') or 'unknown'}"
		if name == REQUIRED_CHECK_NAME and run.get("conclusion") == "success":
			lint_passed = True
	if not lint_passed:
		return False, f"CI check {REQUIRED_CHECK_NAME} has not passed on this head"
	if status_count and combined_state != "success":
		return False, f"commit statuses are {combined_state or 'unknown'}"
	return True, "all checks passed"


def owner_approved(reviews: list[dict], owner: str, head: str) -> bool:
	"""The owner's latest deciding review is APPROVED and bound to `head`.

	Only reviews by `owner` with author_association OWNER count; COMMENTED
	reviews do not decide, and a later CHANGES_REQUESTED or DISMISSED review
	withdraws an earlier approval.
	"""
	latest = None
	for review in reviews:
		user = (review.get("user") or {}).get("login") or ""
		if user.lower() != owner.lower() or review.get("author_association") != "OWNER":
			continue
		if review.get("state") not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
			continue
		key = (review.get("submitted_at") or "", review.get("id") or 0)
		if latest is None or key >= latest[0]:
			latest = (key, review)
	if latest is None:
		return False
	review = latest[1]
	return review.get("state") == "APPROVED" and review.get("commit_id") == head


def render_body(plan: dict) -> str:
	"""The sync PR body: `[skip ai]`, the copies, conflicts, and merge rule."""
	lines = [
		"Copies `workflow-templates/.claude/**` into `.claude/**` (CLAUDE.md §28.C). Opened and updated by `.github/workflows/claude-twin-sync.yml`.",
		"",
		f"Default branch commit: `{plan['ref']}`",
		"",
		"[skip ai] — the twin content was reviewed in its own PR; this PR is a mechanical copy.",
		"",
	]
	if plan["copies"]:
		lines.append("## Files copied")
		lines += [f"- `.claude/{item['path']}` ← `{TWIN_ROOT}/{item['path']}` ({item['reason']})" for item in plan["copies"]]
		lines.append("")
	if plan["conflicts"]:
		lines.append("## Conflicts (not overwritten)")
		lines += [f"- `.claude/{item['path']}`: {item['reason']}" for item in plan["conflicts"]]
		lines.append("")
		lines.append("Make each listed `.claude/` file and its twin identical in a watched session, then close this PR.")
		lines.append("")
	if plan["rejected"]:
		lines.append("## Rejected twin paths")
		lines += [f"- `{TWIN_ROOT}/{item['path']}`: {item['reason']}" for item in plan["rejected"]]
		lines.append("")
	lines.append("## Merge rule")
	if plan["needs_owner"]:
		why = ", ".join(f"`.claude/{path}`" for path in plan["guard_paths"]) or "the conflicts above"
		lines.append(f"Needs the repository owner: {why}. The workflow never approves or merges this PR. `{APPROVAL_STATUS_CONTEXT}` turns `success` when the owner's latest review approves the current head.")
	else:
		lines.append("No hook or settings change: the workflow merges this PR once every check run on its head has passed.")
	return "\n".join(lines) + "\n"


# --- GitHub ------------------------------------------------------------------


class GitHub:
	"""`gh api` calls: reads with one token, writes with another."""

	def __init__(self, repo: str, read_token: str, write_token: str, dry_run: bool = False):
		self.repo = repo
		self.read_token = read_token
		self.write_token = write_token
		self.dry_run = dry_run

	def _gh(self, token: str, args: list[str]) -> str:
		env = {**os.environ, "GH_TOKEN": token}
		env.pop("GITHUB_TOKEN", None)
		proc = subprocess.run(["gh", *args], capture_output=True, text=True, env=env)
		if proc.returncode != 0:
			raise SyncError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()[:500]}")
		return proc.stdout

	def get(self, path: str):
		out = self._gh(self.read_token, ["api", "-X", "GET", path])
		return json.loads(out) if out.strip() else None

	def get_list(self, path: str, key: str | None = None) -> list[dict]:
		"""Every 100-item page of a list endpoint (or of `key` in an object page)."""
		items: list[dict] = []
		for page_number in range(1, MAX_PAGES + 1):
			separator = "&" if "?" in path else "?"
			page = self.get(f"{path}{separator}per_page=100&page={page_number}")
			page_items = page.get(key) if key and isinstance(page, dict) else page
			if not isinstance(page_items, list):
				raise SyncError(f"{path} returned a non-list page")
			items.extend(item for item in page_items if isinstance(item, dict))
			if len(page_items) < 100:
				return items
		raise SyncError(f"{path} pagination exceeded {MAX_PAGES} pages")

	def write(self, args: list[str]) -> str:
		if self.dry_run:
			log(f"dry_run write gh {' '.join(args[:4])}")
			return "{}"
		return self._gh(self.write_token, args)


def push_commit(repo: str, commit: str, branch: str, token: str, dry_run: bool) -> None:
	"""Push `commit` to `branch` with `token` for this one command only.

	The checkout sets persist-credentials: false, so the token is passed to
	this one command (`git_auth_env`). The push is never forced.
	"""
	if dry_run:
		log(f"dry_run push {commit} -> {branch}")
		return
	run_git(repo, ["push", "origin", f"{commit}:refs/heads/{branch}"], env=git_auth_env(token))


def git_auth_env(token: str) -> dict:
	"""Environment that authenticates one git command to github.com with `token`.

	The credential travels in GIT_CONFIG_* variables, never in argv or in the
	repository's config. An empty token adds nothing (a public read).
	"""
	if not token:
		return {"GIT_TERMINAL_PROMPT": "0"}
	basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
	if os.environ.get("GITHUB_ACTIONS") == "true":
		print(f"::add-mask::{basic}", flush=True)
	return {
		"GIT_CONFIG_COUNT": "1",
		"GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
		"GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
		"GIT_TERMINAL_PROMPT": "0",
	}


def free_branch_name(repo: str, wanted: str, env: dict | None = None) -> str:
	"""`wanted`, or `wanted-2` … `wanted-9` when origin already has that branch.

	Pushes are never forced (the ruleset forbids non-fast-forward pushes), so
	a branch left by an earlier, closed sync PR for the same commit is skipped.
	"""
	taken = set()
	for line in run_git(repo, ["ls-remote", "--heads", "origin", f"{wanted}*"], env=env).splitlines():
		ref = line.split("\t", 1)[-1]
		taken.add(ref.removeprefix("refs/heads/"))
	for candidate in [wanted] + [f"{wanted}-{n}" for n in range(2, 10)]:
		if candidate not in taken:
			return candidate
	raise SyncError(f"no free branch name for {wanted}")


def origin_sync_branches(repo: str, env: dict | None = None) -> set[str]:
	"""Every `SYNC_BRANCH_PREFIX*` branch origin holds (one `git ls-remote`)."""
	branches = set()
	for line in run_git(repo, ["ls-remote", "--heads", "origin", f"{SYNC_BRANCH_PREFIX}*"], env=env).splitlines():
		ref = line.split("\t", 1)[-1]
		if ref.startswith("refs/heads/"):
			branches.add(ref.removeprefix("refs/heads/"))
	return branches


def open_sync_prs(gh: GitHub) -> list[dict]:
	prs = gh.get_list(f"repos/{gh.repo}/pulls?state=open")
	found = []
	for pr in prs:
		head = pr.get("head") or {}
		if (head.get("ref") or "").startswith(SYNC_BRANCH_PREFIX) and ((head.get("repo") or {}).get("full_name") or "").lower() == gh.repo.lower():
			found.append(pr)
	return sorted(found, key=lambda pr: pr.get("number") or 0)


def close_pr(gh: GitHub, number: int, comment: str) -> None:
	gh.write(["api", "-X", "POST", f"repos/{gh.repo}/issues/{number}/comments", "-f", f"body={comment}"])
	gh.write(["api", "-X", "PATCH", f"repos/{gh.repo}/pulls/{number}", "-f", "state=closed"])


def set_label(gh: GitHub, number: int, labels: list[str], wanted: bool) -> None:
	present = APPROVAL_LABEL in labels
	if wanted and not present:
		try:
			gh.write(["api", "-X", "POST", f"repos/{gh.repo}/labels", "-f", f"name={APPROVAL_LABEL}", "-f", f"color={APPROVAL_LABEL_COLOR}", "-f", f"description={APPROVAL_LABEL_DESCRIPTION}"])
		except SyncError as exc:
			if "HTTP 422" not in str(exc):
				raise
			log(f"label exists name={APPROVAL_LABEL}")  # 422: the label already exists
		gh.write(["api", "-X", "POST", f"repos/{gh.repo}/issues/{number}/labels", "-f", f"labels[]={APPROVAL_LABEL}"])
	elif present and not wanted:
		gh.write(["api", "-X", "DELETE", f"repos/{gh.repo}/issues/{number}/labels/{APPROVAL_LABEL}"])


def run_sync(repo_root: str, gh: GitHub, ref: str, default_branch: str, owner: str, run_id: str) -> dict:
	"""The workflow driver (see module doc). Returns the JSON summary."""
	plan = plan_sync(repo_root, ref)
	ref_sha = plan["ref"]
	summary: dict = {
		"ref": ref_sha,
		"copies": [item["path"] for item in plan["copies"]],
		"conflicts": [item["path"] for item in plan["conflicts"]],
		"rejected": [item["path"] for item in plan["rejected"]],
		"needs_owner": plan["needs_owner"],
		"pr": None,
		"action": "noop",
		"alert": "",
		"merge": "not attempted",
	}
	for item in plan["rejected"]:
		log(f"rejected path={item['path']} reason={item['reason']}")
	prs = open_sync_prs(gh)
	if plan["copies"] or plan["conflicts"]:
		# A PR whose head branch was deleted can never be updated: close it
		# so the run opens a replacement instead of failing on every trigger.
		live = origin_sync_branches(repo_root, env=git_auth_env(gh.read_token))
		for orphan in [pr for pr in prs if pr["head"]["ref"] not in live]:
			log(f"close orphaned pr=#{orphan['number']} branch={orphan['head']['ref']} reason=head_branch_deleted")
			close_pr(gh, orphan["number"], f"The head branch `{orphan['head']['ref']}` no longer exists, so this PR cannot be updated; a new sync PR replaces it.")
		prs = [pr for pr in prs if pr["head"]["ref"] in live]
	primary = prs[0] if prs else None
	for extra in prs[1:]:
		log(f"close duplicate pr=#{extra['number']} primary=#{primary['number']}")
		close_pr(gh, extra["number"], f"Superseded by #{primary['number']}; one sync PR at a time.")

	if not plan["copies"] and not plan["conflicts"]:
		if primary:
			log(f"close stale pr=#{primary['number']} reason=twins_match")
			close_pr(gh, primary["number"], "The twins already match `.claude/`; nothing left to sync.")
			summary.update({"pr": primary["number"], "action": "closed_stale"})
		log("twins match; nothing to sync")
		return summary

	tree = build_tree(repo_root, ref_sha, plan["copies"])
	short = ref_sha[:12]
	pushed = False
	if primary:
		branch = primary["head"]["ref"]
		head = primary["head"]["sha"]
		run_git(repo_root, ["fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], env=git_auth_env(gh.read_token))
		if resolve_commit(repo_root, f"refs/remotes/origin/{branch}") != head:
			raise SyncError(f"{branch} moved while this run read it; the next run retries")
		# Rebuild when the head's `.claude/` no longer equals the default
		# branch's plus the copies, or when the head carries a change outside
		# `.claude/` (a foreign push onto the sync branch, which `merge-check`
		# would refuse on every run). A default-branch commit anywhere else
		# changes neither the PR's diff nor what it merges, and a new head
		# restarts CI (about 45 minutes) and voids an owner approval, so on a
		# busy default branch the PR would never merge.
		foreign = changes_outside_claude(repo_root, ref_sha, head)
		if foreign:
			log(f"rebuild pr=#{primary['number']} reason=head_changes_outside_claude count={len(foreign)}")
		if foreign or claude_tree_of(repo_root, head) != claude_tree_of(repo_root, tree):
			parents = [head] if is_ancestor(repo_root, ref_sha, head) else [head, ref_sha]
			head = commit_tree(repo_root, tree, parents, f"[claude-twin-sync] sync .claude/ with {TWIN_ROOT}/ at {short}")
			push_commit(repo_root, head, branch, gh.write_token, gh.dry_run)
			pushed = True
		number = primary["number"]
		body = render_body(plan)
		if (primary.get("body") or "") != body:
			gh.write(["api", "-X", "PATCH", f"repos/{gh.repo}/pulls/{number}", "-f", f"body={body}"])
		labels = [label.get("name") for label in primary.get("labels") or []]
		summary["action"] = "updated" if pushed else "unchanged"
	else:
		branch = free_branch_name(repo_root, f"{SYNC_BRANCH_PREFIX}{short}", env=git_auth_env(gh.read_token))
		message = f"[claude-twin-sync] sync .claude/ with {TWIN_ROOT}/ at {short}"
		if not plan["copies"]:
			message += "\n\nNo file copied: every differing file is a conflict listed in the PR."
		head = commit_tree(repo_root, tree, [ref_sha], message)
		push_commit(repo_root, head, branch, gh.write_token, gh.dry_run)
		pushed = True
		created = gh.write([
			"api", "-X", "POST", f"repos/{gh.repo}/pulls",
			"-f", f"title=Sync .claude/ from {TWIN_ROOT}/ ({short})",
			"-f", f"head={branch}", "-f", f"base={default_branch}",
			"-f", f"body={render_body(plan)}",
		])
		number = (json.loads(created or "{}") or {}).get("number")
		labels = []
		summary["action"] = "opened"
	summary["pr"] = number
	summary["head"] = head
	summary["branch"] = branch
	if not isinstance(number, int):
		if gh.dry_run:
			return summary
		raise SyncError("the PR number is unknown after the write")

	approved = False
	if plan["needs_owner"]:
		approved = owner_approved(gh.get_list(f"repos/{gh.repo}/pulls/{number}/reviews"), owner, head)
	set_label(gh, number, labels, plan["needs_owner"])
	if plan["needs_owner"]:
		state, description = ("success", "Owner approved this head") if approved else ("pending", "Owner review and merge required")
	else:
		state, description = "success", "No hook or settings change"
	# Post the status only when it changes (§15): GitHub refuses more than
	# 1000 statuses per sha and context, and a guard PR waiting for the owner
	# is revisited every hour. A head this run pushed has no status yet.
	combined = {"state": "", "statuses": []} if pushed else (gh.get(f"repos/{gh.repo}/commits/{head}/status") or {})
	current = next((item for item in combined.get("statuses") or [] if isinstance(item, dict) and item.get("context") == APPROVAL_STATUS_CONTEXT), None)
	if current is None or current.get("state") != state or current.get("description") != description:
		gh.write([
			"api", "-X", "POST", f"repos/{gh.repo}/statuses/{head}",
			"-f", f"state={state}", "-f", f"context={APPROVAL_STATUS_CONTEXT}", "-f", f"description={description}",
		])
		combined = None  # the combined state just changed; a merge attempt reads it again
	summary["owner_approved"] = approved
	if plan["needs_owner"]:
		if pushed:
			what = ", ".join(f".claude/{path}" for path in plan["guard_paths"]) or "no guard path"
			summary["alert"] = (
				f"Claude twin sync PR #{number} in {gh.repo} needs the owner: guard paths {what}; "
				f"conflicts {len(plan['conflicts'])}. Review and merge it by hand: https://github.com/{gh.repo}/pull/{number}"
			)
		summary["merge"] = "owner only"
		return summary
	if pushed:
		summary["merge"] = "waiting for checks on the new head"
		return summary

	files = gh.get_list(f"repos/{gh.repo}/pulls/{number}/files")
	verdict = merge_check(repo_root, ref_sha, head, files)
	if not verdict["ok"]:
		summary["merge"] = "refused: " + "; ".join(verdict["reasons"])
		return summary
	runs = gh.get_list(f"repos/{gh.repo}/commits/{head}/check-runs", key="check_runs")
	if combined is None:
		combined = gh.get(f"repos/{gh.repo}/commits/{head}/status") or {}
	green, why = checks_green(runs, combined.get("state") or "", len(combined.get("statuses") or []), run_id)
	if not green:
		summary["merge"] = f"waiting: {why}"
		return summary
	gh.write(["pr", "merge", str(number), "--repo", gh.repo, "--squash", "--match-head-commit", head])
	summary["merge"] = "merged"
	return summary


# --- CLI ---------------------------------------------------------------------


def _print(data: dict) -> None:
	print(json.dumps(data, sort_keys=True), flush=True)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	p_plan = sub.add_parser("plan")
	p_plan.add_argument("--repo-root", default=".")
	p_plan.add_argument("--ref", default="HEAD")
	p_check = sub.add_parser("check")
	p_check.add_argument("--repo-root", default=".")
	p_check.add_argument("--base", required=True)
	p_check.add_argument("--head", default="HEAD")
	p_merge = sub.add_parser("merge-check")
	p_merge.add_argument("--repo-root", default=".")
	p_merge.add_argument("--ref", required=True)
	p_merge.add_argument("--head", required=True)
	p_merge.add_argument("--pr-files", required=True, help="JSON file: the PR's /files list")
	p_run = sub.add_parser("run")
	p_run.add_argument("--repo-root", default=".")
	p_run.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
	p_run.add_argument("--ref", default="HEAD")
	p_run.add_argument("--default-branch", default="main")
	p_run.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
	p_run.add_argument("--dry-run", action="store_true")
	p_run.add_argument("--summary-file", default="", help="also write the JSON summary here")
	args = parser.parse_args(argv)
	try:
		if args.command == "plan":
			_print(plan_sync(args.repo_root, args.ref))
			return 0
		if args.command == "check":
			base = args.base.strip()
			if not base or base == ZERO_SHA:
				_print({"ok": True, "skipped": "no base commit (new branch)"})
				return 0
			result = check_not_ahead(args.repo_root, resolve_commit(args.repo_root, base), resolve_commit(args.repo_root, args.head))
			for item in result["violations"]:
				# The path comes from the checked commit range (a PR's content):
				# escaped, so a line break in a file name cannot start a command.
				path = escape_command_property(item["path"])
				message = escape_command_data(f".claude/ is ahead of its twin: {item['reason']} (CLAUDE.md §28.C)")
				print(f"::error file={path}::{message}", flush=True)
			_print(result)
			return 0 if result["ok"] else 1
		if args.command == "merge-check":
			files = json.loads(Path(args.pr_files).read_text(encoding="utf-8"))
			result = merge_check(args.repo_root, resolve_commit(args.repo_root, args.ref), resolve_commit(args.repo_root, args.head), files)
			_print(result)
			return 0 if result["ok"] else 1
		if not REPO_RE.fullmatch(args.repo or "") or not args.owner:
			raise SyncError("--repo owner/name and --owner are required")
		read_token = os.environ.get("CLAUDE_TWIN_SYNC_READ_TOKEN", "")
		write_token = os.environ.get("CLAUDE_TWIN_SYNC_WRITE_TOKEN", "")
		if not args.dry_run and (not read_token or not write_token):
			raise SyncError("CLAUDE_TWIN_SYNC_READ_TOKEN and CLAUDE_TWIN_SYNC_WRITE_TOKEN are required")
		gh = GitHub(args.repo, read_token, write_token, dry_run=args.dry_run)
		summary = run_sync(args.repo_root, gh, args.ref, args.default_branch, args.owner, os.environ.get("GITHUB_RUN_ID", ""))
		log(" ".join(f"{key}={summary[key]}" for key in ("action", "pr", "merge")))
		if args.summary_file:
			Path(args.summary_file).write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
		_print(summary)
		return 0
	except (SyncError, OSError, ValueError) as exc:
		log(f"error {exc}")
		_print({"ok": False, "error": str(exc)})
		return 2


if __name__ == "__main__":
	sys.exit(main())

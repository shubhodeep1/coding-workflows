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

The auto-merge-eligible branch accepts only template history attributable to
merged, non-pipeline PRs targeting the sync base from this repository, authored
by non-bot OWNER/MEMBER/COLLABORATOR accounts, with clean commit subjects.
Other changes go to a separate draft PR for human review. GH_PAT is still a
broad write credential; branch provenance is a conservative signal, not proof
of a human author (a trusted collaborator can still submit AI-written content).
A ready held PR can race with draft conversion before the next push.

API budget (CLAUDE.md §15): provenance and open-PR reads use GraphQL; per run,
one aliased association lookup covers up to 30 distinct template commits,
followed by 1-3 pages per distinct merged PR and one open-PR lookup per
nonempty group. Only PR creation uses REST, at most once for each of the
authorized and held groups (two REST calls total).

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
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PREFIX = "workflow-templates/.claude/"
LIVE_PREFIX = ".claude/"
ALLOWLIST_PATH = ".github/ai/claude_template_divergence.json"
SECURITY_LIVE_PREFIXES = ("hooks/",)
SECURITY_LIVE_FILES = ("settings.json",)
DEFAULT_SYNC_BRANCH = "ai/sync-claude-live-copies"
HELD_BRANCH_SUFFIX = "-held"
PIPELINE_HEAD_RE = re.compile(r"^(ai|orchestrator|auto)/")
PIPELINE_MARKER_RE = re.compile(r"^\[(ai-autofix|judge-fix|ai-merge-resolve|claude-[a-z0-9-]+)\]")
SQUASHED_PR_RE = re.compile(r"\(#\d+\)\s*$")
DEFAULT_MAX_PROVENANCE_COMMITS = 30
TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


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
	if (root / LIVE_PREFIX).is_symlink():
		raise ValueError(f"unsafe live symlink: {LIVE_PREFIX}")
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


def _commit_pull_requests_graphql(repository: str, shas: list[str]) -> dict[str, object]:
	"""Map commit SHA to REST-shaped associated PRs with one aliased GraphQL call.

	Input is at most 30 validated commit SHAs; output has one list per SHA. The
	helper issues exactly one API call and raises on partial or malformed data so
	the caller can hold every affected path rather than authorizing from a cache
	miss.
	"""
	if not shas:
		return {}
	owner, name = repository.split("/", 1)
	query_variables = ",".join(["$owner:String!", "$name:String!", *(f"$sha{index}:GitObjectID!" for index in range(len(shas)))])
	query_fields = "".join(
		"commit%d:object(oid:$sha%d){... on Commit{associatedPullRequests(first:100){nodes{number mergedAt headRefName baseRefName headRepository{nameWithOwner} author{login __typename} authorAssociation} pageInfo{hasNextPage}}}}"
		% (index, index)
		for index in range(len(shas))
	)
	query = f"query({query_variables}){{repository(owner:$owner,name:$name){{{query_fields}}}}}"
	arguments = ["graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"]
	for index, sha in enumerate(shas):
		arguments.extend(("-f", f"sha{index}={sha}"))
	result = _gh_json(*arguments)
	try:
		if not isinstance(result, dict) or result.get("errors"):
			raise ValueError("invalid commit associations")
		repository_data = result["data"]["repository"]
		response_cache: dict[str, object] = {}
		for index, sha in enumerate(shas):
			connection = repository_data[f"commit{index}"]["associatedPullRequests"]
			if connection["pageInfo"]["hasNextPage"] is not False or not isinstance(connection["nodes"], list):
				raise ValueError("invalid commit associations")
			response_cache[sha] = [
				{
					"number": pull_request["number"], "merged_at": pull_request["mergedAt"],
					"head": {"ref": pull_request["headRefName"], "repo": {"full_name": pull_request["headRepository"]["nameWithOwner"]} if pull_request["headRepository"] else None},
					"base": {"ref": pull_request["baseRefName"]},
					"user": {"login": pull_request["author"]["login"], "type": pull_request["author"]["__typename"]} if pull_request["author"] else None,
					"author_association": pull_request["authorAssociation"],
				}
				for pull_request in connection["nodes"]
			]
		return response_cache
	except (KeyError, TypeError, ValueError):
		raise ValueError("invalid commit associations") from None


def _pull_request_commits_graphql(repository: str, number: int, cursor: str | None) -> tuple[list[object], str | None, bool, int]:
	"""Return one 100-commit GraphQL page plus cursor metadata; malformed data fails closed."""
	owner, name = repository.split("/", 1)
	query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){commits(first:100,after:$cursor){nodes{commit{message}} totalCount pageInfo{hasNextPage endCursor}}}}}"""
	arguments = ["graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}"]
	if cursor is not None:
		arguments.extend(("-f", f"cursor={cursor}"))
	result = _gh_json(*arguments)
	try:
		if not isinstance(result, dict) or result.get("errors"):
			raise ValueError("invalid PR commits")
		connection = result["data"]["repository"]["pullRequest"]["commits"]
		entries = connection["nodes"]
		next_cursor = connection["pageInfo"]["endCursor"]
		has_next_page = connection["pageInfo"]["hasNextPage"]
		total_count = connection["totalCount"]
		if (
			not isinstance(entries, list)
			or not isinstance(has_next_page, bool)
			or not isinstance(total_count, int)
			or isinstance(total_count, bool)
			or (has_next_page and not isinstance(next_cursor, str))
		):
			raise ValueError("invalid PR commits")
		return entries, next_cursor, has_next_page, total_count
	except (KeyError, TypeError, ValueError):
		raise ValueError("invalid PR commits") from None


def _open_sync_prs_graphql(repository: str, branch: str, base: str) -> object:
	"""Return REST-shaped open sync PRs using one GraphQL read; malformed data fails closed."""
	owner, name = repository.split("/", 1)
	query = """query($owner:String!,$name:String!,$branch:String!,$base:String!){repository(owner:$owner,name:$name){pullRequests(first:100,states:OPEN,headRefName:$branch,baseRefName:$base){nodes{number state isDraft id headRefName baseRefName headRepository{nameWithOwner}} pageInfo{hasNextPage}}}}"""
	result = _gh_json(
		"graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}",
		"-f", f"branch={branch}", "-f", f"base={base}",
	)
	try:
		if not isinstance(result, dict) or result.get("errors"):
			raise ValueError("invalid open PRs")
		connection = result["data"]["repository"]["pullRequests"]
		if connection["pageInfo"]["hasNextPage"] is not False or not isinstance(connection["nodes"], list):
			raise ValueError("invalid open PRs")
		return [
			{
				"number": pull_request["number"], "state": pull_request["state"].lower(),
				"draft": pull_request["isDraft"], "node_id": pull_request["id"],
				"head": {"ref": pull_request["headRefName"], "repo": {"full_name": pull_request["headRepository"]["nameWithOwner"]} if pull_request["headRepository"] else None},
				"base": {"ref": pull_request["baseRefName"]},
			}
			for pull_request in connection["nodes"]
		]
	except (AttributeError, KeyError, TypeError, ValueError):
		raise ValueError("invalid open PRs") from None


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


def _template_commits(root: Path, relative: str, after: str) -> list[tuple[str, str]] | None:
	try:
		live_revision = _git(root, "log", "-1", "--format=%H", after, "--", LIVE_PREFIX + relative, check=False)
	except OSError:
		return None
	if live_revision.returncode != 0:
		return None
	live_sha = live_revision.stdout.strip()
	if live_sha and not re.fullmatch(r"[0-9a-f]{40,64}", live_sha):
		return None
	range_ref = f"{live_sha}..{after}" if live_sha else after
	try:
		history = _git(root, "log", "--format=%H%x09%s", range_ref, "--", TEMPLATE_PREFIX + relative, check=False)
	except OSError:
		return None
	if history.returncode != 0:
		return None
	commits: list[tuple[str, str]] = []
	for line in history.stdout.splitlines():
		sha, separator, subject = line.partition("\t")
		if not separator or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
			return None
		commits.append((sha, subject))
	return commits


def _commit_authorization(
	repository: str, sha: str, subject: str,
	pr_cache: dict[str, object], commit_cache: dict[int, object],
	base_branch: str | None = None,
) -> tuple[bool, str, str]:
	if PIPELINE_MARKER_RE.match(subject):
		return False, "pipeline_commit_marker", "none"
	base_branch = base_branch or os.environ.get("BASE_BRANCH") or "main"
	try:
		# The sync-branch PR lookup cannot establish the source commit's provenance.
		if sha not in pr_cache:
			raise ValueError("commit associations unavailable")
		associations = pr_cache[sha]
		if not isinstance(associations, list) or len(associations) >= 100 or any(not isinstance(pr, dict) for pr in associations):
			raise ValueError("invalid commit associations")
		for pr in associations:
			merged_at = pr.get("merged_at")
			if merged_at is not None:
				if not isinstance(merged_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", merged_at):
					raise ValueError("invalid merge timestamp")
				datetime.strptime(merged_at, "%Y-%m-%dT%H:%M:%SZ")
		merged = [pr for pr in associations if pr.get("merged_at")]
		if not merged:
			return False, "no_merged_pr", "none"
		for pr in merged:
			number = pr.get("number")
			head = pr.get("head")
			if not isinstance(number, int) or isinstance(number, bool) or number <= 0 or not isinstance(head, dict) or not isinstance(head.get("ref"), str):
				raise ValueError("invalid merged PR")
			if PIPELINE_HEAD_RE.match(head["ref"]):
				return False, "pipeline_branch", str(number)
			base = pr.get("base")
			if not isinstance(base, dict) or not isinstance(base.get("ref"), str):
				raise ValueError("invalid PR base")  # noqa: TRY004 - malformed API data holds the path
			if base["ref"] != base_branch:
				return False, "base_mismatch", str(number)
			head_repo = head.get("repo")
			if not isinstance(head_repo, dict):
				return False, "foreign_head_repo", str(number)
			if not isinstance(head_repo.get("full_name"), str):
				raise ValueError("invalid head repository")  # noqa: TRY004 - malformed API data holds the path
			if head_repo["full_name"].casefold() != repository.casefold():
				return False, "foreign_head_repo", str(number)
			user = pr.get("user")
			if not isinstance(user, dict) or not isinstance(user.get("login"), str):
				raise ValueError("invalid PR author")  # noqa: TRY004 - malformed API data holds the path
			if user.get("type") != "User" or user["login"].lower().endswith("[bot]"):
				return False, "bot_author", str(number)
			association = pr.get("author_association")
			if not isinstance(association, str) or association not in TRUSTED_AUTHOR_ASSOCIATIONS:
				return False, "untrusted_author", str(number)
		for pr in merged:
			number = pr["number"]
			if number not in commit_cache:
				entries: list[object] = []
				cursor = None
				for _page in range(3):
					batch, cursor, has_next_page, total_count = _pull_request_commits_graphql(repository, number, cursor)
					if total_count >= 250 or any(not isinstance(item, dict) for item in batch):
						return False, "pr_commits_truncated", str(number)
					entries.extend(batch)
					if not has_next_page:
						break
				else:
					return False, "pr_commits_truncated", str(number)
				if len(entries) != total_count:
					raise ValueError("incomplete PR commits")
				commit_cache[number] = entries
			entries = commit_cache[number]
			if not isinstance(entries, list) or not entries:
				raise ValueError("empty PR commits")
			for entry in entries:
				commit = entry.get("commit") if isinstance(entry, dict) else None
				message = commit.get("message") if isinstance(commit, dict) else None
				if not isinstance(message, str) or not message:
					raise ValueError("invalid PR commit subject")
				pr_subject = message.splitlines()[0]
				if PIPELINE_MARKER_RE.match(pr_subject):
					return False, "pr_commit_marker", str(number)
				if SQUASHED_PR_RE.search(pr_subject):
					return False, "squashed_pr_commit", str(number)
	except (subprocess.CalledProcessError, OSError, ValueError):
		return False, "api_failed", "none"
	return True, "", str(merged[0]["number"])


def _classify_paths(root: Path, paths: list[str], after: str, repository: str, base_branch: str | None = None) -> tuple[list[str], dict[str, tuple[str, str, str]]]:
	authorized: list[str] = []
	held: dict[str, tuple[str, str, str]] = {}
	commit_cache: dict[int, object] = {}
	try:
		cap = int(os.environ.get("CLAUDE_LIVE_SYNC_MAX_PROVENANCE_COMMITS", str(DEFAULT_MAX_PROVENANCE_COMMITS)))
	except ValueError:
		cap = DEFAULT_MAX_PROVENANCE_COMMITS
	if cap <= 0:
		cap = DEFAULT_MAX_PROVENANCE_COMMITS
	seen: set[str] = set()
	path_commits: dict[str, list[tuple[str, str]] | None] = {}
	for relative in paths:
		commits = _template_commits(root, relative, after)
		path_commits[relative] = commits
		if commits and len(seen | {sha for sha, _subject in commits}) <= cap:
			seen.update(sha for sha, _subject in commits)
	try:
		pr_cache = _commit_pull_requests_graphql(repository, sorted(seen))
	except (subprocess.CalledProcessError, OSError, ValueError):
		pr_cache = {}
	seen.clear()
	for relative in paths:
		commits = path_commits[relative]
		reason, sha12, pr_number = "", "none", "none"
		if commits is None:
			reason = "history_failed"
		elif not commits:
			reason = "no_template_commits"
		elif len(seen | {sha for sha, _ in commits}) > cap:
			reason = "provenance_cap_exceeded"
			sha12 = commits[0][0][:12]
		else:
			seen.update(sha for sha, _ in commits)
			for sha, subject in commits:
				ok, reason, pr_number = _commit_authorization(repository, sha, subject, pr_cache, commit_cache, base_branch=base_branch)
				if not ok:
					sha12 = sha[:12]
					break
		if reason:
			held[relative] = (reason, sha12, pr_number)
			log(f"held path={LIVE_PREFIX}{relative} reason={reason} commit={sha12} pr={pr_number}")
		else:
			authorized.append(relative)
			log(f"authorized path={LIVE_PREFIX}{relative} commits={len(commits)}")
	return authorized, held


def _carry_forward(root: Path, after: str, branch: str, paths: list[str], drifted: set[str]) -> tuple[str, list[str]] | None:
	previous_sha = ""
	extra_paths: list[str] = []
	remote_result = _git(root, "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}", check=False)
	if remote_result.returncode == 0:
		previous_sha = remote_result.stdout.partition("\t")[0]
		if (
			not re.fullmatch(r"[0-9a-f]{40,64}", previous_sha)
			or _git(root, "fetch", "--no-tags", "origin", f"refs/heads/{branch}", check=False).returncode != 0
			or _git(root, "rev-parse", "FETCH_HEAD", check=False).stdout.strip() != previous_sha
		):
			log("error reason=sync_branch_fetch_failed")
			return None
		prior_paths = _git(root, "diff", "--name-only", f"{after}...FETCH_HEAD", "--", LIVE_PREFIX, check=False)
		main_paths = _git(root, "diff", "--name-only", f"FETCH_HEAD...{after}", "--", LIVE_PREFIX, check=False)
		if prior_paths.returncode != 0 or main_paths.returncode != 0:
			log("error reason=sync_branch_diff_failed")
			return None
		main_changed = set(main_paths.stdout.splitlines())
		for path in prior_paths.stdout.splitlines():
			if not path.startswith(LIVE_PREFIX):
				continue
			prior_relative = path[len(LIVE_PREFIX):]
			if prior_relative not in drifted or path in main_changed or prior_relative in paths:
				continue
			prior_copy = _git(root, "show", f"FETCH_HEAD:{path}", check=False)
			try:
				carried_template_text = (root / TEMPLATE_PREFIX / prior_relative).read_text(encoding="utf-8")
			except OSError:
				log(f"error reason=template_read_failed path={TEMPLATE_PREFIX}{prior_relative}")
				return None
			if prior_copy.returncode == 0 and prior_copy.stdout == carried_template_text:
				extra_paths.append(prior_relative)
	elif remote_result.returncode != 2:
		log("error reason=sync_branch_lookup_failed")
		return None
	return previous_sha, extra_paths


def _commit_and_push(root: Path, branch: str, after: str, relatives: list[str], previous_sha: str, message: str) -> bool:
	try:
		_git(root, "checkout", "-B", branch, after)
		for relative in relatives:
			(root / LIVE_PREFIX / relative).parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(root / TEMPLATE_PREFIX / relative, root / LIVE_PREFIX / relative)
			log(f"copied path={LIVE_PREFIX}{relative}")
		_git(root, "add", "--", *[LIVE_PREFIX + relative for relative in relatives])
		_git(root, "-c", "user.name=github-actions[bot]", "-c", "user.email=github-actions[bot]@users.noreply.github.com", "commit", "-q", "-m", message)
	except (subprocess.CalledProcessError, OSError):
		log("error reason=git_stage_failed")
		return False
	try:
		_git(root, "push", f"--force-with-lease=refs/heads/{branch}:{previous_sha}", "origin", f"HEAD:refs/heads/{branch}")
	except subprocess.CalledProcessError:
		log("error reason=push_failed")
		return False
	return True


def _open_or_refresh_pr(repository: str, owner: str, branch: str, base: str, title: str, body: str, *, draft: bool, relatives: list[str], push: Callable[[], bool]) -> int | None:
	if not draft and not push():
		return None
	try:
		open_prs = _open_sync_prs_graphql(repository, branch, base)
	except (subprocess.CalledProcessError, OSError, ValueError):
		log("error reason=api_failed stage=lookup")
		return None
	if not isinstance(open_prs, list):
		log("error reason=api_failed stage=lookup_invalid_response")
		return None
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
	open_prs = matches
	if draft and open_prs:
		pr = open_prs[0]
		if not isinstance(pr.get("draft"), bool) or not isinstance(pr.get("node_id"), str):
			log("error reason=api_failed stage=lookup_invalid_response")
			return None
		if not pr["draft"]:
			try:
				converted = _gh_json("graphql", "-f", "query=mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){pullRequest{isDraft}}}", "-f", f"id={pr['node_id']}")
				if not isinstance(converted, dict) or converted.get("errors") or converted.get("data", {}).get("convertPullRequestToDraft", {}).get("pullRequest", {}).get("isDraft") is not True:
					raise ValueError("draft conversion unconfirmed")
			except (subprocess.CalledProcessError, OSError, ValueError, AttributeError):
				log("error reason=api_failed stage=convert_to_draft")
				return None
			log(f"converted_to_draft pr={pr.get('number')}")
	if draft and not push():
		return None
	if open_prs:
		number = open_prs[0].get("number")
		if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
			log("error reason=api_failed stage=lookup_invalid_response")
			return None
		if draft:
			log(f"held branch={branch} pr={number} paths={len(relatives)}")
		else:
			log(f"updated pr={number} branch={branch} base={base} paths={len(relatives)}")
		return number
	try:
		created = _gh_json(
			"-X", "POST", f"repos/{repository}/pulls",
			"-f", f"title={title}", "-f", f"head={branch}", "-f", f"base={base}", "-f", f"body={body}",
			*(["-F", "draft=true"] if draft else []),
		)
	except (subprocess.CalledProcessError, OSError, ValueError):
		log("error reason=api_failed stage=create")
		return None
	number = created.get("number") if isinstance(created, dict) else None
	if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
		log("error reason=api_failed stage=create_invalid_response")
		return None
	if draft:
		log(f"opened pr={number} draft=true branch={branch} paths={len(relatives)}")
		log(f"held branch={branch} pr={number} paths={len(relatives)}")
	else:
		log(f"opened pr={number} branch={branch} paths={len(relatives)}")
	return number


def _write_outputs(held_pr: int | None, held: dict[str, tuple[str, str, str]]) -> None:
	output = os.environ.get("GITHUB_OUTPUT")
	if not output:
		return
	summary = "; ".join(f"{LIVE_PREFIX}{relative} ({reason}, commit {sha}, PR {pr})" for relative, (reason, sha, pr) in held.items())
	summary = "".join(char for char in summary if 32 <= ord(char) <= 126)[:1500]
	with open(output, "a", encoding="utf-8") as stream:
		stream.write(f"held={'true' if held else 'false'}\nheld_pr={held_pr or ''}\nheld_summary={summary}\n")


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
		or branch.endswith(HELD_BRANCH_SUFFIX)
		or (branch != DEFAULT_SYNC_BRANCH and not branch.startswith(DEFAULT_SYNC_BRANCH + "-"))
		or _git(root, "check-ref-format", "--branch", branch, check=False).returncode != 0
	):
		log("error reason=unsafe_sync_branch")
		return 1
	if dry_run:
		# CI preparation never writes to the remote; the provenance gate applies
		# only to the privileged sync, not to disposable test checkouts.
		for relative in paths:
			(root / LIVE_PREFIX / relative).parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(root / TEMPLATE_PREFIX / relative, root / LIVE_PREFIX / relative)
			log(f"copied path={LIVE_PREFIX}{relative}")
		return 0
	if not repository or "/" not in repository:
		log("error reason=missing_repository")
		return 1
	previous_shas: dict[str, str] = {}
	for target_branch in (branch, branch + HELD_BRANCH_SUFFIX):
		carried = _carry_forward(root, after, target_branch, paths, set(mismatched(root)))
		if carried is None:
			return 1
		previous_shas[target_branch], additional = carried
		paths.extend(path for path in additional if path not in paths)
	authorized, held = _classify_paths(root, paths, after, repository, base_branch=base)
	short_after = after[:12]
	owner = repository.split("/", 1)[0]
	if authorized:
		message = (
			f"chore(.claude): sync live copies with their templates after {short_after}\n\n"
			"The pipeline's editors cannot edit .claude/**. Copy the templates for:\n\n"
			+ "".join(f"- .claude/{relative}\n" for relative in authorized)
			+ "\nCopied by scripts/sync_claude_live_copies.py (sync-claude-live-copies.yml).\n"
		)
		body = (
			"Syncs live `.claude/` copies with their `workflow-templates/.claude/` templates "
			f"after `{short_after}`:\n\n"
			+ "".join(f"- `.claude/{relative}`\n" for relative in authorized)
			+ "\nThe pipeline's editors cannot edit `.claude/**`, so a template-only change leaves this repo's own copy "
			"behind and fails the parity tests on every PR. Opened by `scripts/sync_claude_live_copies.py` from the "
			"`sync-claude-live-copies.yml` workflow.\n"
		)
		if _open_or_refresh_pr(
			repository, owner, branch, base, f"chore(.claude): sync live copies with their templates ({short_after})", body,
			draft=False, relatives=authorized,
			push=lambda: _commit_and_push(root, branch, after, authorized, previous_shas[branch], message),
		) is None:
			return 1
	held_pr: int | None = None
	if held:
		held_branch = branch + HELD_BRANCH_SUFFIX
		held_paths = list(held)
		held_lines = "".join(f"- `.claude/{relative}`: {reason} (commit {sha}, PR {pr})\n" for relative, (reason, sha, pr) in held.items())
		held_body = (
			f"Template changes after `{short_after}` require human approval:\n\n{held_lines}\n"
			"Held as a draft because the template change did not come from an authorized source "
			"(sync-claude-live-copies.yml provenance gate). Verify the diff, then mark ready or merge by hand.\n"
		)
		held_message = f"chore(.claude): HELD sync of live copies ({short_after})\n\n{held_lines}"
		held_pr = _open_or_refresh_pr(
			repository, owner, held_branch, base, f"chore(.claude): HELD sync of live copies ({short_after})", held_body,
			draft=True, relatives=held_paths,
			push=lambda: _commit_and_push(root, held_branch, after, held_paths, previous_shas[held_branch], held_message),
		)
		if held_pr is None:
			return 1
	_write_outputs(held_pr, held)
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

#!/usr/bin/env python3
"""Owner authorization for protected-equivalent changes at merge and release.

Security finding `template-variant-authorization-bypass` (issue #4919): the
template parity contract (`tests/test_claude_template_parity.py`, issue #4775)
cannot tell who changed a consumer-variant `.claude` command, so an
unattended agent could edit the command, update its pinned SHA-256 in the
same PR, pass CI, auto-merge, and reach every consumer through the `@stable`
sync. This script is the deterministic authorization check the merge helpers
(`scripts/protected_path_gate.sh`) and the release workflows run.

Protected-equivalent paths (compared case-insensitively, so a case variant
fails closed):

  - `.claude/**` and `workflow-templates/.claude/**`;
  - `tests/test_claude_template_parity.py` (the `TEMPLATE_DIVERGENCE` pins);
  - this script and `scripts/protected_path_gate.sh` (the gate itself).

A rename counts when either side is protected, and a PR whose file list is
truncated counts as protected.

Authorization is a comment on the PR that only a person can post. It counts
when all of these hold:

  1. its whole body, trimmed, is `/authorize-protected-paths <head SHA>`, with
     the full 40-hex SHA of the head being merged (or, at release, the PR's
     merged head);
  2. `user.type` is `User` and `author_association` is OWNER, MEMBER, or
     COLLABORATOR;
  3. `performed_via_github_app` is null: Claude sessions post through the
     claude.ai proxy's GitHub App (`claude`), so an agent cannot authorize;
  4. it was never edited (`updated_at == created_at`), because an agent acting
     as the same account could edit an owner comment.

Usage:

  protected_path_authorization.py pr --repo OWNER/REPO --pr N [--head SHA] [--post-instructions] [--disable-auto-merge]
  protected_path_authorization.py release --repo OWNER/REPO --base REF --head REF [--git-dir DIR]

`pr` prints one JSON line with `decision` (`allow` or `block`), `protected`,
`paths`, `head`, `authorized`, and `reason`; exit 0 allows the merge, 3 blocks
it, 2 means a read failed (the caller blocks), 1 is a usage error. With
`--post-instructions`, a blocked protected PR gets one comment per head
(marker `<!-- ai:protected-path-authorization:v1 head=<sha> -->`) naming the
command; that comment is never a bare command, so it cannot authorize. With
`--disable-auto-merge`, a blocked protected PR also has a pending auto-merge
turned off (GitHub keeps auto-merge across pushes by anyone with write
access, so one enabled on an earlier, unprotected head would otherwise land
the unauthorized one); the JSON's `auto_merge` field says what happened.

`release` checks every non-merge commit in `<base>..<head>` that touches a
protected path, and every merge commit that makes a protected change of its
own (a conflict resolution or a hand-edited merge: `--remerge-diff` for a
two-parent merge, any difference from every parent for an octopus merge), so
wrapping a change in a merge commit cannot hide it. It needs the full
history: a shallow checkout fails. A commit passes when one of its merged PRs
(`commits/{sha}/pulls`) has an authorizing comment at that PR's `head.sha`,
or when it is grandfathered: reachable from the gate's arrival commit, the
oldest first-parent commit on `<head>` that changed this script. Reachability
is pure git history, so no date an agent can set decides it. A commit that
is not grandfathered and has no merged PR is blocked. A missing `<base>`
(first release) checks the whole history. It
prints one JSON line with `decision` (`pass` or `block`); exit 0 passes, 3
blocks, 2 means a read failed, 1 is a usage error.

GitHub API budget (CLAUDE.md §15), reads over REST and retried three times:
`pr` costs one PR read and one files page per 100 files, plus, only for a
protected PR, one comments page per 100 comments, at most one comment
POST, and, only when auto-merge is pending on a blocked PR, one GraphQL
`disablePullRequestAutoMerge` mutation (the only mutation that turns
auto-merge off). A failed mutation, including an HTTP 200 answer with
`errors` or one that still shows an auto-merge request, is retried at most
twice, each retry after one more PR read. `release` costs one `commits/{sha}/pulls` read per
protected commit and one comments page per 100 comments per distinct PR
that needs it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from typing import Any, Callable

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
AUTHORIZE_RE = re.compile(r"/authorize-protected-paths ([0-9a-f]{40})")
AUTHORIZE_COMMAND = "/authorize-protected-paths"
INSTRUCTION_MARKER = "<!-- ai:protected-path-authorization:v1 head={head} -->"
INSTRUCTION_MARKER_RE = re.compile(r"<!-- ai:protected-path-authorization:v1 head=([0-9a-f]{40}) -->")
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
PROTECTED_PREFIXES = (".claude/", "workflow-templates/.claude/")
GATE_SCRIPT_PATH = "scripts/protected_path_authorization.py"
PROTECTED_FILES = frozenset({
	"tests/test_claude_template_parity.py",
	GATE_SCRIPT_PATH,
	"scripts/protected_path_gate.sh",
})
# `git rev-list` pathspecs matching the same set; `icase` mirrors the
# case-insensitive comparison in is_protected_path.
PROTECTED_PATHSPECS = tuple(
	[f":(icase){prefix}" for prefix in PROTECTED_PREFIXES]
	+ [f":(icase){path}" for path in sorted(PROTECTED_FILES)]
)
PAGE_SIZE = 100
# GitHub's pulls/{n}/files lists at most 3000 files.
MAX_FILE_PAGES = 30
MAX_COMMENT_PAGES = 30
READ_ATTEMPTS = 3
PATHS_IN_OUTPUT = 20

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_READ_ERROR = 2
EXIT_BLOCKED = 3


class ReadError(Exception):
	"""A GitHub or git read failed; the caller fails closed."""


def _gh_json(args: list[str]) -> Any:
	try:
		proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=60)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ReadError(f"gh api {args[0]} failed: {exc}") from exc
	if proc.returncode != 0:
		detail = ((proc.stderr or "").strip() or (proc.stdout or "").strip()).splitlines()
		raise ReadError(f"gh api {args[0]} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	try:
		return json.loads(proc.stdout)
	except ValueError as exc:
		raise ReadError(f"gh api {args[0]} returned invalid JSON") from exc


def gh_get_json(path: str, sleep: Callable[[float], None] = time.sleep) -> Any:
	"""GET one REST path, retrying transient failures (2s, 4s backoff)."""
	for attempt in range(READ_ATTEMPTS):
		try:
			return _gh_json([path])
		except ReadError:
			if attempt == READ_ATTEMPTS - 1:
				raise
			sleep(2 ** (attempt + 1))
	raise ReadError(f"gh api {path} failed")


def _get_pages(path: str, max_pages: int, get: Callable[[str], Any]) -> tuple[list[Any], bool]:
	"""Read `path` page by page; return (items, complete)."""
	items: list[Any] = []
	separator = "&" if "?" in path else "?"
	for page in range(1, max_pages + 1):
		chunk = get(f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
		if not isinstance(chunk, list):
			raise ReadError(f"{path} returned non-array JSON")
		items.extend(chunk)
		if len(chunk) < PAGE_SIZE:
			return items, True
	return items, False


def is_protected_path(path: Any) -> bool:
	"""True for a protected-equivalent repository path (case-insensitive)."""
	if not isinstance(path, str) or not path:
		return False
	lowered = path.lower()
	while lowered.startswith("./"):
		lowered = lowered[2:]
	return lowered.startswith(PROTECTED_PREFIXES) or lowered in PROTECTED_FILES


def protected_files(files: list[Any]) -> list[str]:
	"""Protected paths among PR file entries, rename sources included."""
	found: set[str] = set()
	for entry in files:
		if not isinstance(entry, dict):
			continue
		for key in ("filename", "previous_filename"):
			if is_protected_path(entry.get(key)):
				found.add(entry[key])
	return sorted(found)


def comment_authorizes(comment: Any, head_sha: str) -> str:
	"""Return "" when `comment` authorizes `head_sha`, else why it does not."""
	if not isinstance(comment, dict):
		return "not a comment object"
	match = AUTHORIZE_RE.fullmatch((comment.get("body") or "").strip())
	if match is None:
		return "body is not exactly the authorization command"
	if match.group(1) != head_sha:
		return f"authorizes {match.group(1)}, not {head_sha}"
	user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
	if user.get("type") != "User":
		return f"author type {user.get('type') or 'unknown'} is not User"
	if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
		return f"author association {comment.get('author_association') or 'none'} is not trusted"
	if comment.get("performed_via_github_app") is not None:
		app = comment["performed_via_github_app"]
		slug = app.get("slug") if isinstance(app, dict) else None
		return f"posted through GitHub App {slug or 'unknown'}"
	created = comment.get("created_at")
	if not isinstance(created, str) or not created or comment.get("updated_at") != created:
		return "comment was edited"
	return ""


def authorizing_comment(comments: list[Any], head_sha: str) -> dict[str, Any] | None:
	"""The first comment that authorizes `head_sha`, or None."""
	if not SHA_RE.fullmatch(head_sha or ""):
		return None
	for comment in comments:
		if not comment_authorizes(comment, head_sha):
			return comment
	return None


def evaluate_pr(
	pr: dict[str, Any],
	files: list[Any],
	files_complete: bool,
	comments: list[Any] | None,
	expected_head: str | None = None,
) -> dict[str, Any]:
	"""Pure merge decision over already-fetched data.

	`comments` may be None when the PR is not protected (they are never read
	then). Returns the JSON object the `pr` command prints.
	"""
	head = pr.get("head", {}).get("sha") if isinstance(pr.get("head"), dict) else None
	result: dict[str, Any] = {
		"decision": "block",
		"pr": pr.get("number"),
		"protected": True,
		"paths": [],
		"head": head if isinstance(head, str) else "",
		"authorized": False,
		"reason": "",
	}
	if expected_head is not None and expected_head != head:
		result["reason"] = f"PR head moved to {head or 'an unknown head'}; the merge was bound to {expected_head}"
		return result
	paths = protected_files(files)
	changed = pr.get("changed_files")
	truncated = not files_complete or (isinstance(changed, int) and changed > len(files))
	result["paths"] = paths[:PATHS_IN_OUTPUT]
	if not paths and not truncated:
		# An unprotected merge runs exactly as before (§5); a head SHA is
		# only needed to bind and authorize a protected one.
		result.update({"decision": "allow", "protected": False, "reason": "no protected-equivalent path changed"})
		return result
	if not isinstance(head, str) or not SHA_RE.fullmatch(head):
		result["reason"] = "PR head SHA is unavailable"
		return result
	if comments is None:
		result["reason"] = "comments were not read"
		return result
	comment = authorizing_comment(comments, head)
	protected_reason = "file list is truncated" if truncated and not paths else f"{len(paths)} protected-equivalent path(s) changed"
	if comment is None:
		result["reason"] = f"{protected_reason}; no owner {AUTHORIZE_COMMAND} {head} comment"
		return result
	result.update({
		"decision": "allow",
		"authorized": True,
		"comment_id": comment.get("id"),
		"reason": f"{protected_reason}; authorized by comment {comment.get('id')}",
	})
	return result


def instruction_body(head: str, paths: list[str]) -> str:
	"""The comment asking the owner for authorization; never a bare command."""
	# Capped like the JSON output: GitHub rejects a comment over 65,536 characters.
	listed = "\n".join(f"- `{path}`" for path in paths[:PATHS_IN_OUTPUT]) or "- (file list truncated; treated as protected)"
	if len(paths) > PATHS_IN_OUTPUT:
		listed += f"\n- … and {len(paths) - PATHS_IN_OUTPUT} more"
	return (
		f"{INSTRUCTION_MARKER.format(head=head)}\n"
		"🔒 **Protected-path authorization required** (issue #4919)\n\n"
		"This PR changes protected-equivalent paths. The `@stable` sync copies them into every "
		"consumer's `.claude/`, so automated merges and releases refuse them until the repository "
		f"owner approves this exact head, `{head}`.\n\n"
		"To approve, post a **new** comment yourself (GitHub web UI or app, not through Claude or "
		"any other app) whose entire body is:\n\n"
		f"    {AUTHORIZE_COMMAND} {head}\n\n"
		"Then merge the PR, or let the orchestrator retry. A new push needs a new approval. An "
		"edited comment, or one posted by an agent, does not count (CLAUDE.md §23.I).\n\n"
		f"Protected-equivalent paths changed:\n{listed}\n"
	)


def check_pr(
	repo: str,
	number: int,
	expected_head: str | None = None,
	post_instructions: bool = False,
	get: Callable[[str], Any] | None = None,
	post: Callable[[str, str], None] | None = None,
	disable_auto_merge: bool = False,
	disable: Callable[[str], None] | None = None,
	sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
	"""Fetch what `evaluate_pr` needs and return its decision.

	With `disable_auto_merge`, a protected PR blocked for want of the owner's
	comment also has a pending auto-merge turned off. GitHub keeps auto-merge
	enabled when someone with write access pushes, so an auto-merge enabled on
	an earlier, unprotected head would otherwise land this unauthorized one.
	The PR read already carries `auto_merge`, so this costs a write only when
	one is pending; a failed write is retried up to READ_ATTEMPTS times, each
	retry after one more PR read (`_turn_off_auto_merge`)."""
	get = get or gh_get_json
	post = post or _post_comment
	disable = disable or _disable_auto_merge
	if not REPO_RE.fullmatch(repo or ""):
		raise ValueError("--repo must be OWNER/REPO")
	if number <= 0:
		raise ValueError("--pr must be a positive integer")
	if expected_head is not None and not SHA_RE.fullmatch(expected_head):
		raise ValueError("--head must be a full 40-hex SHA")
	pr = get(f"repos/{repo}/pulls/{number}")
	if not isinstance(pr, dict):
		raise ReadError(f"PR #{number} returned non-object JSON")
	files, files_complete = _get_pages(f"repos/{repo}/pulls/{number}/files", MAX_FILE_PAGES, get)
	decision = evaluate_pr(pr, files, files_complete, None, expected_head)
	if decision["decision"] == "allow" or decision["reason"] != "comments were not read":
		return decision
	comments, _complete = _get_pages(f"repos/{repo}/issues/{number}/comments", MAX_COMMENT_PAGES, get)
	decision = evaluate_pr(pr, files, files_complete, comments, expected_head)
	if decision["decision"] == "block" and post_instructions:
		head = decision["head"]
		already = any(
			isinstance(comment, dict) and INSTRUCTION_MARKER.format(head=head) in (comment.get("body") or "")
			for comment in comments
		)
		decision["instructions"] = "already posted" if already else "posted"
		if not already:
			try:
				post(f"repos/{repo}/issues/{number}/comments", instruction_body(head, protected_files(files)))
			except ReadError as exc:
				decision["instructions"] = f"post failed: {exc}"
	if decision["decision"] == "block" and disable_auto_merge:
		node_id = pr.get("node_id")
		if not pr.get("auto_merge"):
			decision["auto_merge"] = "none pending"
		elif not isinstance(node_id, str) or not node_id:
			decision["auto_merge"] = "disable failed: PR node_id is unavailable"
		else:
			decision["auto_merge"] = _turn_off_auto_merge(repo, number, node_id, get, disable, sleep)
	return decision


def _turn_off_auto_merge(
	repo: str,
	number: int,
	node_id: str,
	get: Callable[[str], Any],
	disable: Callable[[str], None],
	sleep: Callable[[float], None],
) -> str:
	"""Turn off the PR's pending auto-merge; return the `auto_merge` status.

	Up to READ_ATTEMPTS mutations (2s, 4s backoff). Before each retry the PR
	is read again: a failed attempt may still have landed, and a second
	mutation on a PR with no auto-merge fails, so a re-read showing no
	`auto_merge` counts as disabled. A read error there leaves the retry
	to the mutation."""
	failure: ReadError | None = None
	for attempt in range(READ_ATTEMPTS):
		if attempt:
			sleep(2 ** attempt)
			try:
				fresh = get(f"repos/{repo}/pulls/{number}")
			except ReadError:
				fresh = None
			if isinstance(fresh, dict) and not fresh.get("auto_merge"):
				return "disabled"
		try:
			disable(node_id)
			return "disabled"
		except ReadError as exc:
			failure = exc
	return f"disable failed: {failure}"


def _post_comment(path: str, body: str) -> None:
	_gh_json([path, "-X", "POST", "-f", f"body={body}"])


DISABLE_AUTO_MERGE_MUTATION = (
	"mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) "
	"{ pullRequest { autoMergeRequest { enabledAt } } } }"
)


def auto_merge_disable_error(result: Any) -> str:
	"""Return "" when a `disablePullRequestAutoMerge` response confirms the PR
	has no auto-merge request left, else why it does not. GraphQL can answer
	HTTP 200 with a top-level `errors` array, so exit 0 alone proves nothing."""
	if not isinstance(result, dict):
		return "response is not a JSON object"
	errors = result.get("errors")
	if errors:
		messages = [
			str(error.get("message")) for error in errors
			if isinstance(error, dict) and error.get("message")
		] if isinstance(errors, list) else []
		return "GraphQL errors: " + ("; ".join(messages) or "unreadable")
	data = result.get("data")
	payload = data.get("disablePullRequestAutoMerge") if isinstance(data, dict) else None
	pull = payload.get("pullRequest") if isinstance(payload, dict) else None
	if not isinstance(pull, dict):
		return "response has no disablePullRequestAutoMerge.pullRequest"
	if pull.get("autoMergeRequest") is not None:
		return "auto-merge is still enabled after the mutation"
	return ""


def _disable_auto_merge(node_id: str) -> None:
	result = _gh_json(["graphql", "-f", f"query={DISABLE_AUTO_MERGE_MUTATION}", "-f", f"id={node_id}"])
	problem = auto_merge_disable_error(result)
	if problem:
		raise ReadError(f"gh api graphql disablePullRequestAutoMerge: {problem}")


def _git(args: list[str], git_dir: str | None) -> str:
	cmd = ["git"] + (["-C", git_dir] if git_dir else []) + args
	try:
		proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ReadError(f"git {args[0]} failed: {exc}") from exc
	if proc.returncode != 0:
		detail = (proc.stderr or "").strip().splitlines()
		raise ReadError(f"git {' '.join(args[:2])} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	return proc.stdout


def _resolve(ref: str, git_dir: str | None) -> str | None:
	try:
		sha = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], git_dir).strip()
	except ReadError:
		return None
	return sha if SHA_RE.fullmatch(sha) else None


def gate_arrival_commit(head: str, git_dir: str | None) -> str | None:
	"""The oldest first-parent commit on `head` that changed this script: the
	commit that brought the gate onto that line. None when not found."""
	out = _git(["log", "--first-parent", "--format=%H", head, "--", GATE_SCRIPT_PATH], git_dir)
	lines = [line.strip() for line in out.splitlines() if SHA_RE.fullmatch(line.strip())]
	return lines[-1] if lines else None


def protected_merge_commits(base: str | None, head: str, git_dir: str | None) -> list[str]:
	"""Merge commits in `base..head` (or all of `head`) that make a protected
	change of their own.

	A clean merge only brings in its parents' commits, which are checked
	themselves. A merge can also carry its own edit: a conflict resolution, or
	a hand-edited merge pushed straight to a branch. A two-parent merge counts
	when `--remerge-diff` (the recorded merge against git's own re-merge of
	its parents, git 2.36 or later) touches a protected path. An octopus
	merge, which `--remerge-diff` does not cover, counts when it differs on a
	protected path from every parent."""
	rev_range = f"{base}..{head}" if base else head
	out = _git(
		["log", "--merges", "--max-parents=2", "--format=%x00%H", "--remerge-diff", "--name-only", rev_range, "--", *PROTECTED_PATHSPECS],
		git_dir,
	)
	found: list[str] = []
	for record in out.split("\x00")[1:]:
		lines = [line.strip() for line in record.splitlines() if line.strip()]
		if len(lines) > 1 and SHA_RE.fullmatch(lines[0]):
			found.append(lines[0])
	octopus = _git(["rev-list", "--min-parents=3", rev_range, "--", *PROTECTED_PATHSPECS], git_dir)
	found.extend(line.strip() for line in octopus.splitlines() if SHA_RE.fullmatch(line.strip()))
	return found


def protected_commits(base: str | None, head: str, git_dir: str | None) -> list[str]:
	"""Commits in `base..head` (or all of `head`) that make a protected change:
	non-merge commits touching a protected path, then the merges that carry a
	protected change of their own (protected_merge_commits)."""
	rev_range = f"{base}..{head}" if base else head
	out = _git(["rev-list", "--no-merges", rev_range, "--", *PROTECTED_PATHSPECS], git_dir)
	commits = [line.strip() for line in out.splitlines() if SHA_RE.fullmatch(line.strip())]
	seen = set(commits)
	for sha in protected_merge_commits(base, head, git_dir):
		if sha not in seen:
			seen.add(sha)
			commits.append(sha)
	return commits


def evaluate_release(
	commits: list[str],
	grandfathered: set[str],
	pulls_for: Callable[[str], list[Any]],
	comments_for: Callable[[int], list[Any]],
) -> dict[str, Any]:
	"""Pure release decision; the callables do the (cached) reads.

	`grandfathered` holds the commits reachable from the gate's arrival
	commit; they pass without any API read."""
	blocked: list[dict[str, Any]] = []
	counts = {"authorized": 0, "grandfathered": 0}
	for sha in commits:
		if sha in grandfathered:
			counts["grandfathered"] += 1
			continue
		merged = [
			pr for pr in pulls_for(sha)
			if isinstance(pr, dict) and pr.get("merged_at") and isinstance(pr.get("number"), int)
		]
		if not merged:
			blocked.append({"commit": sha, "prs": [], "reason": "no merged pull request carries this commit"})
			continue
		authorized = False
		for pr in merged:
			head = pr.get("head", {}).get("sha") if isinstance(pr.get("head"), dict) else None
			if isinstance(head, str) and authorizing_comment(comments_for(pr["number"]), head) is not None:
				authorized = True
				break
		if authorized:
			counts["authorized"] += 1
			continue
		blocked.append({
			"commit": sha,
			"prs": [pr["number"] for pr in merged],
			"reason": f"no owner {AUTHORIZE_COMMAND} <merged head> comment on PR(s) "
				+ ", ".join(f"#{pr['number']}" for pr in merged),
		})
	return {
		"decision": "block" if blocked else "pass",
		"checked": len(commits),
		"authorized": counts["authorized"],
		"grandfathered": counts["grandfathered"],
		"blocked": blocked,
	}


def check_release(
	repo: str,
	base_ref: str,
	head_ref: str,
	git_dir: str | None = None,
	get: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
	get = get or gh_get_json
	if not REPO_RE.fullmatch(repo or ""):
		raise ValueError("--repo must be OWNER/REPO")
	# A shallow history hides the gate's real arrival commit (the shallow
	# boundary would stand in for it and be grandfathered) and the merge
	# bases `--remerge-diff` needs, so only a full history is trusted.
	if _git(["rev-parse", "--is-shallow-repository"], git_dir).strip() != "false":
		raise ReadError("the release check needs the full git history; this checkout is shallow (fetch with fetch-depth: 0 or git fetch --unshallow)")
	head = _resolve(head_ref, git_dir)
	if head is None:
		raise ReadError(f"release head {head_ref} does not resolve to a commit")
	base = _resolve(base_ref, git_dir)
	arrival = gate_arrival_commit(head, git_dir)
	if arrival is None:
		raise ReadError(f"{GATE_SCRIPT_PATH} has no first-parent history on {head_ref}; cannot place the grandfather boundary")
	commits = protected_commits(base, head, git_dir)
	grandfathered = set(protected_commits(base, arrival, git_dir))
	comments_cache: dict[int, list[Any]] = {}

	def pulls_for(sha: str) -> list[Any]:
		pulls = get(f"repos/{repo}/commits/{sha}/pulls")
		if not isinstance(pulls, list):
			raise ReadError(f"commits/{sha}/pulls returned non-array JSON")
		return pulls

	def comments_for(number: int) -> list[Any]:
		if number not in comments_cache:
			comments_cache[number], _complete = _get_pages(f"repos/{repo}/issues/{number}/comments", MAX_COMMENT_PAGES, get)
		return comments_cache[number]

	result = evaluate_release(commits, grandfathered, pulls_for, comments_for)
	result.update({"base": base, "base_ref": base_ref, "head": head, "gate_arrival": arrival})
	return result


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	pr = sub.add_parser("pr", help="decide whether an automated merge of a PR may proceed")
	pr.add_argument("--repo", required=True)
	pr.add_argument("--pr", type=int, required=True)
	pr.add_argument("--head", default=None)
	pr.add_argument("--post-instructions", action="store_true")
	pr.add_argument("--disable-auto-merge", action="store_true")
	release = sub.add_parser("release", help="decide whether a release range may ship")
	release.add_argument("--repo", required=True)
	release.add_argument("--base", required=True)
	release.add_argument("--head", required=True)
	release.add_argument("--git-dir", default=None)
	return parser


def main(argv: list[str] | None = None) -> int:
	try:
		args = build_parser().parse_args(argv)
	except SystemExit as exc:
		return EXIT_USAGE if exc.code else EXIT_OK
	try:
		if args.command == "pr":
			result = check_pr(args.repo, args.pr, args.head, args.post_instructions, disable_auto_merge=args.disable_auto_merge)
			blocked = result["decision"] != "allow"
		else:
			result = check_release(args.repo, args.base, args.head, args.git_dir)
			blocked = result["decision"] != "pass"
	except ValueError as exc:
		print(json.dumps({"decision": "block", "error": str(exc)}))
		return EXIT_USAGE
	except ReadError as exc:
		print(json.dumps({"decision": "block", "error": str(exc)}))
		return EXIT_READ_ERROR
	print(json.dumps(result))
	return EXIT_BLOCKED if blocked else EXIT_OK


if __name__ == "__main__":
	sys.exit(main())

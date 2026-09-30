#!/usr/bin/env python3
"""Refuse a review-workflow merge of a `claude/*` PR that is on hold or out of twin parity.

Issue #5316. A twin-first `/implement-plan-claude` phase edits only the
`workflow-templates/.claude/**` twins, posts a `hold` claim on the PR head,
and stops until a `[claude-twin-sync]` commit copies the twins into
`.claude/` (CLAUDE.md §28.C). The review run for that head can already be in
progress when the hold is posted; before this gate, a clean result enabled
head-bound auto-merge (`AUTOFIX_AUTO_MERGE_HEAD_BOUND`) without looking at
claims, the PR merged at once, and the twin sync landed on a merged branch
(§21; PR #5301, PR #5182). `review_autofix.yml` therefore runs this gate
immediately before every merge enablement a `claude/*` PR can reach:
`scripts/review_enable_auto_merge.sh` and the `deterministic-skip-merge` job.
A future merge path (the pending-checks merge of #4900) must call it too.

Usage:

  claude_merge_hold_gate.py --repo OWNER/REPO --pr N --head SHA [--pr-json FILE]

Input: the PR and the exact head SHA the caller is about to merge, plus
optionally the `GET /repos/{repo}/pulls/{n}` object the caller already
fetched (`--pr-json`, saves one call). The trusted claim authors are the
PR's author and `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` when set, exactly as
`.claude/scripts/check_in_status.py` (`_fix_claim_trusted_logins`) reads
them.

Checks, in order (a non-`claude/*` head skips both and issues no API call):

  1. hold claim - the latest trusted `ai:claude-fix-claim` on the head is
     `hold` (`read_fix_claims` reports `held`). A hold on an older head, a
     hold lifted by a newer claim on the same head, and a hold by an
     untrusted author do not count.
  2. twin parity - the PR changed a twin `workflow-templates/.claude/<p>`
     (its blob differs between the merge base and the head), the pair
     (`workflow-templates/.claude/<p>`, `.claude/<p>`) had the same blob, or
     neither file, at the merge base, and it no longer does at the head.
     Pairs that already differed at the merge base (intentionally divergent
     consumer commands) never block.

Output: one JSON line on stdout with `merge` (bool), `skip_reason`
(`hold_claim`, `twin_parity`, `gate_unavailable`, or null), `reason`, `pr`,
`head_sha`, `claim`, and `broken_pairs`. Exit 0 allows the merge, 1 refuses
it (hold or parity), 2 means the gate could not decide; callers treat 2 as a
refusal (fail closed, CLAUDE.md §1).

API calls (CLAUDE.md §15), `claude/*` heads only: one `pulls/{n}` read unless
`--pr-json` is given; one call per 100 PR comments with no page cap (the
fresh read is the point: the race is a hold posted after the run's earlier
reads; issue #5566: a cap let a comment flood refuse every merge), plus up
to two retries of a failing page, keeping only trusted claim-marker lines
in memory; one `compare/{base}...{head}` read; and two recursive
`git/trees` reads only when the compare lists a changed
`workflow-templates/.claude/` path, returns the 300-file maximum, or
returns no file list. Any read failure (a comment page only after its
retries), a malformed comment page, a truncated tree, or a head that no
longer matches `--head` is `gate_unavailable`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK_IN_STATUS_PATH = ROOT / ".claude" / "scripts" / "check_in_status.py"
TWIN_PREFIX = "workflow-templates/.claude/"
LIVE_PREFIX = ".claude/"
# GitHub's compare endpoint returns at most 300 changed files; a list that
# long may be truncated, so the tree comparison runs instead of trusting it.
COMPARE_FILES_MAX = 300
# Issue #5566: the comment read streams every page. A fixed page cap let
# anyone who can comment push a PR past it and refuse every merge.
COMMENT_PAGE_SIZE = 100
COMMENT_READ_ATTEMPTS = 3
COMMENT_RETRY_DELAYS_SECONDS = (1, 2)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")


class GateUnavailable(Exception):
	"""The gate could not decide; the caller refuses the merge."""


def _load_check_in_status():
	spec = importlib.util.spec_from_file_location("check_in_status", CHECK_IN_STATUS_PATH)
	if spec is None or spec.loader is None:
		raise GateUnavailable(f"cannot load {CHECK_IN_STATUS_PATH}")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _blob_map(tree_payload: dict, label: str) -> dict[str, str]:
	"""Map every blob path of a recursive `git/trees` payload to its blob sha."""
	if tree_payload.get("truncated") is not False:
		raise GateUnavailable(f"{label} tree is truncated or incomplete")
	entries = tree_payload.get("tree")
	if not isinstance(entries, list):
		raise GateUnavailable(f"{label} tree has no entry list")
	blobs: dict[str, str] = {}
	for entry in entries:
		if not isinstance(entry, dict):
			raise GateUnavailable(f"{label} tree has a malformed entry")
		if entry.get("type") != "blob":
			continue
		path, sha = entry.get("path"), entry.get("sha")
		if not isinstance(path, str) or not isinstance(sha, str):
			raise GateUnavailable(f"{label} tree has a malformed blob entry")
		blobs[path] = sha
	return blobs


def twin_parity_breaks(head_blobs: dict[str, str], base_blobs: dict[str, str]) -> list[str]:
	"""Return the twin paths whose pair this PR took out of parity.

	No API calls: both maps come from `_blob_map`. A pair counts when the twin
	blob changed between the merge base and the head, the pair matched at the
	merge base (equal blob ids, or both files absent), and it differs at the
	head (unequal blob ids, or one file absent).
	"""
	relative_paths = sorted({
		path[len(TWIN_PREFIX):]
		for path in (*head_blobs, *base_blobs)
		if path.startswith(TWIN_PREFIX)
	})
	broken = []
	for relative in relative_paths:
		twin_path, live_path = TWIN_PREFIX + relative, LIVE_PREFIX + relative
		twin_head, twin_base = head_blobs.get(twin_path), base_blobs.get(twin_path)
		if twin_head == twin_base:
			continue
		if twin_base != base_blobs.get(live_path):
			continue
		if twin_head != head_blobs.get(live_path):
			broken.append(twin_path)
	return broken


def _changes_twin(files: list) -> bool:
	for changed in files:
		if not isinstance(changed, dict):
			raise GateUnavailable("compare returned a malformed file entry")
		for key in ("filename", "previous_filename"):
			name = changed.get(key)
			if isinstance(name, str) and name.startswith(TWIN_PREFIX):
				return True
	return False


def _read_comment_page(check_in_status, path: str, page: int) -> list:
	"""GET one 100-item page of `path`, retrying a failed read (issue #5566).

	One API call per attempt, at most COMMENT_READ_ATTEMPTS, waiting
	COMMENT_RETRY_DELAYS_SECONDS between them. Only a failed read
	(`check_in_status.ReadError`: non-zero `gh` exit, timeout, invalid JSON)
	is retried; a page that is not an array of objects is not transient and
	raises `GateUnavailable` at once, as does the last failed attempt.
	"""
	last_error = None
	for attempt in range(COMMENT_READ_ATTEMPTS):
		if attempt:
			time.sleep(COMMENT_RETRY_DELAYS_SECONDS[min(attempt - 1, len(COMMENT_RETRY_DELAYS_SECONDS) - 1)])
		try:
			page_items = check_in_status._gh_api_json(f"{path}?per_page={COMMENT_PAGE_SIZE}&page={page}")
		except check_in_status.ReadError as exc:
			last_error = exc
			continue
		if not isinstance(page_items, list) or any(not isinstance(item, dict) for item in page_items):
			raise GateUnavailable(f"{path} page {page} is not an array of objects")
		return page_items
	raise GateUnavailable(f"{path} page {page} unreadable after {COMMENT_READ_ATTEMPTS} attempts: {last_error}")


def _stream_claim_comments(check_in_status, repo: str, number: int, trusted_logins: tuple[str, ...]) -> list:
	"""Read every comment page of PR `number` and keep only trusted claim markers.

	Issue #5566. Input: the PR and the casefolded `trusted_logins`
	(`check_in_status._fix_claim_trusted_logins`). Output: trimmed copies of
	the comments `read_fix_claims` could count - a trusted author association,
	a trusted login, and at least one line fully matching `FIX_CLAIM_RE` -
	holding `id`, `user.login`, `author_association`, `created_at`, and a body
	of just those marker lines. `read_fix_claims` therefore sees the same
	marker count per comment and decides exactly as on the full comments,
	while memory grows only with trusted markers, never with a flood of
	untrusted or large comments. API calls: one per 100 comments with no page
	cap (the read ends at the first page shorter than 100), plus up to two
	retries per failing page (`_read_comment_page`). An unreadable or
	malformed page raises `GateUnavailable` (the caller fails closed).
	"""
	path = f"repos/{repo}/issues/{number}/comments"
	kept: list = []
	page = 1
	while True:
		page_items = _read_comment_page(check_in_status, path, page)
		for comment in page_items:
			if comment.get("author_association") not in check_in_status.FIX_CLAIM_TRUSTED_ASSOCIATIONS:
				continue
			author = comment.get("user")
			if (not isinstance(author, dict) or not isinstance(author.get("login"), str)
				or author["login"].casefold() not in trusted_logins):
				continue
			body = comment.get("body")
			if not isinstance(body, str):
				continue
			marker_lines = [line for line in body.splitlines() if check_in_status.FIX_CLAIM_RE.fullmatch(line)]
			if not marker_lines:
				continue
			kept.append({"id": comment.get("id"), "user": {"login": author["login"]},
				"author_association": comment.get("author_association"), "created_at": comment.get("created_at"),
				"body": "\n".join(marker_lines)})
		if len(page_items) < COMMENT_PAGE_SIZE:
			return kept
		page += 1


def evaluate(repo: str, number: int, head: str, pr: dict | None, now: dt.datetime, check_in_status=None) -> dict:
	"""Decide whether `repo`#`number` may be merged at `head`; see the module docstring."""
	result = {"merge": True, "skip_reason": None, "reason": "", "pr": number, "head_sha": head,
		"claim": "not_read", "broken_pairs": []}
	if check_in_status is None:
		check_in_status = _load_check_in_status()
	if pr is None:
		pr = check_in_status.gh_api(f"repos/{repo}/pulls/{number}")
	if not isinstance(pr, dict):
		raise GateUnavailable("PR payload is not an object")
	pr_head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
	head_ref = pr_head.get("ref")
	if not isinstance(head_ref, str) or not head_ref:
		raise GateUnavailable("PR head ref is unavailable")
	if not head_ref.startswith(check_in_status.CLAUDE_BRANCH_PREFIX):
		result["reason"] = f"head ref {head_ref} is not a claude/* branch; no claims or twin sync apply"
		return result
	if pr_head.get("sha") != head:
		raise GateUnavailable(f"PR head is {str(pr_head.get('sha'))[:12]}, not the head being merged {head[:12]}")

	trusted_logins = check_in_status._fix_claim_trusted_logins(pr)
	comments = _stream_claim_comments(check_in_status, repo, number, trusted_logins)
	claims = check_in_status.read_fix_claims(comments, head, now, trusted_logins=trusted_logins)
	claim = claims.get("claim") or {}
	result["claim"] = claim.get("state", "none")
	if claim.get("state") == "held":
		result.update(merge=False, skip_reason="hold_claim",
			reason=f"head {head[:12]} carries a live hold claim by {claim.get('by')} posted {claim.get('at')}")
		return result

	base_sha = (pr.get("base") or {}).get("sha") if isinstance(pr.get("base"), dict) else None
	if not isinstance(base_sha, str) or not HEAD_RE.fullmatch(base_sha):
		raise GateUnavailable("PR base sha is unavailable")
	comparison = check_in_status.gh_api(f"repos/{repo}/compare/{base_sha}...{head}")
	merge_base = (comparison.get("merge_base_commit") or {}).get("sha") if isinstance(comparison.get("merge_base_commit"), dict) else None
	files = comparison.get("files")
	if not isinstance(merge_base, str) or not HEAD_RE.fullmatch(merge_base):
		raise GateUnavailable("compare returned no merge base")
	# A missing file list proves nothing, so it takes the tree comparison too.
	if isinstance(files, list) and len(files) < COMPARE_FILES_MAX and not _changes_twin(files):
		result["reason"] = f"head {head[:12]} has no hold claim and changes no workflow-templates/.claude/ twin"
		return result
	head_blobs = _blob_map(check_in_status.gh_api(f"repos/{repo}/git/trees/{head}?recursive=1"), "head")
	base_blobs = _blob_map(check_in_status.gh_api(f"repos/{repo}/git/trees/{merge_base}?recursive=1"), "merge-base")
	broken = twin_parity_breaks(head_blobs, base_blobs)
	if broken:
		result.update(merge=False, skip_reason="twin_parity", broken_pairs=broken,
			reason=f"head {head[:12]} changes {len(broken)} workflow-templates/.claude/ twin(s) without the matching .claude/ copy: {', '.join(broken[:10])}")
		return result
	result["reason"] = f"head {head[:12]} has no hold claim and keeps every twin pair it touches in parity"
	return result


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--repo", required=True)
	parser.add_argument("--pr", type=int, required=True)
	parser.add_argument("--head", required=True)
	parser.add_argument("--pr-json", help="file holding the pulls/{n} object the caller already fetched")
	return parser


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
	args = build_parser().parse_args(argv)
	now = now or dt.datetime.now(dt.timezone.utc)
	unavailable = {"merge": False, "skip_reason": "gate_unavailable", "pr": args.pr, "head_sha": args.head,
		"claim": "not_read", "broken_pairs": []}
	try:
		if not REPO_RE.fullmatch(args.repo) or args.pr <= 0 or not HEAD_RE.fullmatch(args.head):
			raise GateUnavailable("--repo must be OWNER/REPO, --pr positive, --head 40 lowercase hex characters")
		pr = None
		if args.pr_json:
			try:
				pr = json.loads(Path(args.pr_json).read_text(encoding="utf-8"))
			except (OSError, ValueError) as exc:
				raise GateUnavailable(f"--pr-json is unreadable: {exc}") from exc
		check_in_status = _load_check_in_status()
		try:
			result = evaluate(args.repo, args.pr, args.head, pr, now, check_in_status)
		except check_in_status.ReadError as exc:
			raise GateUnavailable(str(exc)) from exc
	except GateUnavailable as exc:
		print(json.dumps({**unavailable, "reason": str(exc)}))
		return 2
	except Exception as exc:  # fail closed on anything unexpected
		print(json.dumps({**unavailable, "reason": f"internal error: {type(exc).__name__}: {exc}"}))
		return 2
	print(json.dumps(result))
	return 0 if result["merge"] else 1


if __name__ == "__main__":
	sys.exit(main())

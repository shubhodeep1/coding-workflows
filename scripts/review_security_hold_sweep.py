#!/usr/bin/env python3
"""Re-dispatch stale single-issue security holds for one final judge check.

Batching contract: each page accepts up to 50 open pull requests and returns
their head SHA plus the latest 100 issue comments. The sweep issues one REST
identity read, one GraphQL call per PR page, one paginated REST comment read
only for a PR with more than 100 comments and no current-head result or
stale-dispatch marker in that window, and one dispatch plus up to two marker POSTs per stale hold.
Missing, partial, or malformed data fails open by skipping the affected page
or PR; a later scheduled run retries it.

Token modes (SECURITY_HOLD_SWEEP_TOKEN_MODE, issue #7209):
- legacy (default): GH_TOKEN does everything and its account authors both the
  findings markers and the stale-dispatch markers.
- actions_token: GH_TOKEN is the job's github.token and does every listing,
  dispatch and marker POST. SECURITY_HOLD_FINDING_AUTHOR_TOKEN (the PAT, may be
  empty) is only used for one `gh api user` read that names the findings-marker
  author; it is removed from the environment before any other gh call. With no
  PAT the findings author is github-actions[bot], as in the review gate.
  Stale-dispatch markers are trusted from github-actions[bot] (author type Bot)
  and, for markers posted before this mode, from the PAT account.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
from typing import Any


RESULT_MARKER_RE = re.compile(
	r"^<!-- ai:single-issue-security-pass:v1 status=(pending|clean|findings|failed) "
	r"head=([0-9a-f]{40}) cycle=([1-9][0-9]*) -->$"
)
ACTIONS_TOKEN_LOGIN = "github-actions[bot]"
# GraphQL reports a Bot author's login without the "[bot]" suffix.
ACTIONS_TOKEN_GRAPHQL_LOGIN = "github-actions"
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")
TOKEN_MODES = ("legacy", "actions_token")
SWEEP_MARKER_RE = re.compile(
	r"^<!-- ai:security-followup-stale-dispatch:v1 head=([0-9a-f]{40}) cycle=([1-9][0-9]*) -->$"
)
GRAPHQL_QUERY = """
query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){
    defaultBranchRef{name}
    pullRequests(first:50,after:$cursor,states:OPEN,orderBy:{field:UPDATED_AT,direction:DESC}){
      nodes{
        number
        isDraft
        headRefOid
        comments(last:100){
          nodes{author{login __typename}body createdAt}
          pageInfo{hasPreviousPage}
        }
      }
      pageInfo{hasNextPage endCursor}
    }
  }
}
"""


def _log(message: str) -> None:
	print(f"SECURITY_HOLD_SWEEP {message}", flush=True)


def _last_nonempty_line(body: Any) -> str:
	if not isinstance(body, str):
		return ""
	lines = [line.strip() for line in body.splitlines() if line.strip()]
	return lines[-1] if lines else ""


def _parse_utc(value: Any) -> dt.datetime | None:
	if not isinstance(value, str):
		return None
	try:
		parsed_value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None
	return parsed_value if parsed_value.tzinfo is not None else None


def _author_matches(comment_payload: Any, logins: Any) -> bool:
	"""True when the comment author is one of logins.

	github-actions[bot] matches only an author of type Bot (GraphQL login
	"github-actions", REST login "github-actions[bot]"), so a user account
	named like the bot never matches.
	"""
	if not isinstance(comment_payload, dict):
		return False
	author = comment_payload.get("author") or {}
	if not isinstance(author, dict):
		return False
	login = author.get("login")
	if not isinstance(login, str) or not login:
		return False
	if login in (ACTIONS_TOKEN_LOGIN, ACTIONS_TOKEN_GRAPHQL_LOGIN):
		return ACTIONS_TOKEN_LOGIN in logins and author.get("__typename") == "Bot"
	return login in logins


def stale_candidate(pr_payload: Any, trusted_login: str, stale_hours: int, now: dt.datetime, sweep_marker_logins: Any = None) -> tuple[int, str, int] | None:
	if not isinstance(pr_payload, dict) or pr_payload.get("isDraft") is True:
		return None
	pr_number = pr_payload.get("number")
	head_sha = pr_payload.get("headRefOid")
	comments_payload = pr_payload.get("comments") or {}
	if not isinstance(pr_number, int) or pr_number < 1 or not isinstance(head_sha, str):
		return None
	if not isinstance(comments_payload, dict) or not isinstance(comments_payload.get("nodes"), list):
		return None
	comment_nodes = comments_payload["nodes"]
	finding_logins = {trusted_login}
	marker_logins = set(sweep_marker_logins) if sweep_marker_logins is not None else {trusted_login}

	latest_result: tuple[str, int, dt.datetime] | None = None
	dispatched_keys: set[tuple[str, int]] = set()
	for comment_payload in comment_nodes:
		if not isinstance(comment_payload, dict):
			continue
		last_line = _last_nonempty_line(comment_payload.get("body"))
		result_match = RESULT_MARKER_RE.fullmatch(last_line)
		if result_match:
			if result_match.group(2) != head_sha or not _author_matches(comment_payload, finding_logins):
				continue
			created_at = _parse_utc(comment_payload.get("createdAt"))
			if created_at is not None and (latest_result is None or created_at >= latest_result[2]):
				latest_result = (result_match.group(1), int(result_match.group(3)), created_at)
			continue
		dispatch_match = SWEEP_MARKER_RE.fullmatch(last_line)
		if dispatch_match and _author_matches(comment_payload, marker_logins):
			dispatched_keys.add((dispatch_match.group(1), int(dispatch_match.group(2))))

	if latest_result is None or latest_result[0] != "findings":
		return None
	_, cycle, created_at = latest_result
	if now - created_at < dt.timedelta(hours=stale_hours) or (head_sha, cycle) in dispatched_keys:
		return None
	return pr_number, head_sha, cycle


def _has_current_head_result(pr_payload: Any, trusted_login: str, sweep_marker_logins: Any = None) -> bool:
	"""True when the fetched comment window holds a trusted current-head result
	or stale-dispatch marker. Either one is newer than anything beyond the
	window, so the full comment history cannot change the decision."""
	if not isinstance(pr_payload, dict):
		return False
	head_sha = pr_payload.get("headRefOid")
	comment_nodes = (pr_payload.get("comments") or {}).get("nodes") if isinstance(pr_payload.get("comments"), dict) else None
	if not isinstance(comment_nodes, list):
		return False
	finding_logins = {trusted_login}
	marker_logins = set(sweep_marker_logins) if sweep_marker_logins is not None else {trusted_login}
	for comment_payload in comment_nodes:
		if not isinstance(comment_payload, dict):
			continue
		last_line = _last_nonempty_line(comment_payload.get("body"))
		result_match = RESULT_MARKER_RE.fullmatch(last_line)
		if result_match:
			if result_match.group(2) == head_sha and _author_matches(comment_payload, finding_logins):
				return True
			continue
		# A recent dispatch marker means this head was already rechecked;
		# skip the paginated history read on every later tick.
		dispatch_match = SWEEP_MARKER_RE.fullmatch(last_line)
		if dispatch_match and dispatch_match.group(1) == head_sha and _author_matches(comment_payload, marker_logins):
			return True
	return False


def _full_comment_nodes(repository: str, pr_number: int) -> list[dict] | None:
	"""Every issue comment on the PR in the GraphQL node shape, or None on failure."""
	pages = _gh_json(["api", "--paginate", "--slurp", f"repos/{repository}/issues/{pr_number}/comments?per_page=100"])
	if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
		return None
	return [
		{"author": {"login": (comment.get("user") or {}).get("login"), "__typename": (comment.get("user") or {}).get("type")}, "body": comment.get("body"), "createdAt": comment.get("created_at")}
		for page in pages for comment in page if isinstance(comment, dict)
	]


def _run_gh(arguments: list[str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)


def _gh_json(arguments: list[str]) -> Any | None:
	result = _run_gh(arguments)
	if result.returncode != 0:
		return None
	try:
		return json.loads(result.stdout)
	except json.JSONDecodeError:
		return None


def _gh_identity(token: str) -> str:
	"""Login of token's account via one `gh api user` call, or "" on failure.

	The token reaches only this subprocess.
	"""
	if not token:
		return ""
	try:
		result = subprocess.run(
			["gh", "api", "user", "--jq", ".login // \"\""],
			capture_output=True, text=True, check=False, timeout=60,
			env={**os.environ, "GH_TOKEN": token},
		)
	except (OSError, subprocess.TimeoutExpired):
		return ""
	login = result.stdout.strip() if result.returncode == 0 else ""
	return login if LOGIN_RE.fullmatch(login) else ""


def _resolve_authors() -> tuple[str, frozenset[str]] | None:
	"""(findings-marker author, stale-dispatch-marker authors), or None to skip."""
	token_mode = os.environ.get("SECURITY_HOLD_SWEEP_TOKEN_MODE", "") or "legacy"
	if token_mode not in TOKEN_MODES:
		_log("outcome=skip reason=invalid_token_mode")
		return None
	if token_mode == "legacy":
		identity_result = _run_gh(["api", "user", "--jq", ".login // \"\""])
		trusted_login = identity_result.stdout.strip() if identity_result.returncode == 0 else ""
		if not trusted_login:
			_log("outcome=skip reason=identity_unavailable")
			return None
		return trusted_login, frozenset({trusted_login})
	# actions_token: the PAT names the findings author and nothing else.
	finding_author_token = os.environ.pop("SECURITY_HOLD_FINDING_AUTHOR_TOKEN", "")
	if not finding_author_token:
		return ACTIONS_TOKEN_LOGIN, frozenset({ACTIONS_TOKEN_LOGIN})
	finding_login = _gh_identity(finding_author_token)
	del finding_author_token
	if not finding_login:
		_log("outcome=skip reason=finding_identity_unavailable")
		return None
	# Markers the PAT posted before this mode still deduplicate; extra dedup
	# authors can only suppress a recheck, never cause one.
	return finding_login, frozenset({ACTIONS_TOKEN_LOGIN, finding_login})


def main() -> int:
	if os.environ.get("SINGLE_ISSUE_SECURITY_PASS_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
		_log("outcome=skip reason=disabled")
		return 0
	repository = os.environ.get("REPOSITORY", "")
	if repository.count("/") != 1:
		_log("outcome=skip reason=invalid_repository")
		return 0
	try:
		stale_hours = int(os.environ.get("SECURITY_PASS_FOLLOWUP_STALE_HOURS", "24"))
	except ValueError:
		stale_hours = 24
	if stale_hours <= 0:
		stale_hours = 24

	authors = _resolve_authors()
	if authors is None:
		return 0
	trusted_login, sweep_marker_logins = authors
	owner, repo_name = repository.split("/", 1)
	workflow_name = os.environ.get("SECURITY_HOLD_REVIEW_WORKFLOW", "") or (
		"internal-review.yml" if repository == "shubhodeep1/coding-workflows" else "ai-review.yml"
	)
	now = dt.datetime.now(dt.timezone.utc)
	cursor = ""
	dispatched = 0

	while True:
		graphql_args = ["api", "graphql", "-f", f"query={GRAPHQL_QUERY}", "-f", f"owner={owner}", "-f", f"name={repo_name}"]
		if cursor:
			graphql_args.extend(["-f", f"cursor={cursor}"])
		response_payload = _gh_json(graphql_args)
		if not isinstance(response_payload, dict) or response_payload.get("errors"):
			_log("outcome=skip reason=pr_listing_unavailable")
			return 0
		data_payload = response_payload.get("data") or {}
		repository_payload = data_payload.get("repository") if isinstance(data_payload, dict) else None
		if not isinstance(repository_payload, dict):
			_log("outcome=skip reason=pr_listing_unavailable")
			return 0
		default_branch = ((repository_payload.get("defaultBranchRef") or {}).get("name"))
		pull_requests = repository_payload.get("pullRequests") or {}
		if not isinstance(default_branch, str) or not default_branch or not isinstance(pull_requests, dict):
			_log("outcome=skip reason=pr_listing_malformed")
			return 0
		for pr_payload in pull_requests.get("nodes") or []:
			candidate = stale_candidate(pr_payload, trusted_login, stale_hours, now, sweep_marker_logins)
			comments_page = (pr_payload.get("comments") or {}).get("pageInfo") if isinstance(pr_payload, dict) and isinstance(pr_payload.get("comments"), dict) else None
			if (candidate is None and isinstance(pr_payload, dict) and isinstance(pr_payload.get("number"), int)
				and isinstance(comments_page, dict) and comments_page.get("hasPreviousPage") is True
				and not _has_current_head_result(pr_payload, trusted_login, sweep_marker_logins)):
				# The findings marker may sit beyond the latest 100 comments.
				full_nodes = _full_comment_nodes(repository, pr_payload["number"])
				if full_nodes is None:
					_log(f"pr={pr_payload['number']} outcome=skip reason=comment_history_unavailable")
					continue
				candidate = stale_candidate({**pr_payload, "comments": {"nodes": full_nodes}}, trusted_login, stale_hours, now, sweep_marker_logins)
			if candidate is None:
				continue
			pr_number, head_sha, cycle = candidate
			dispatch_result = _run_gh([
				"workflow", "run", workflow_name, "--repo", repository, "--ref", default_branch,
				"-f", f"pr_number={pr_number}", "-f", "allow_workflow_edits=false", "-f", "force_rb_judge=true",
			])
			if dispatch_result.returncode != 0:
				_log(f"pr={pr_number} head={head_sha} cycle={cycle} outcome=dispatch_failed")
				print(f"::warning::Could not dispatch {workflow_name} for stale security hold on PR #{pr_number}; the next scheduled sweep retries.", flush=True)
				continue
			marker_body = (
				"Scheduled a review-blocked judge recheck because this head's security follow-ups exceeded "
				f"{stale_hours} hour(s).\n\n<!-- ai:security-followup-stale-dispatch:v1 head={head_sha} cycle={cycle} -->"
			)
			comment_result = _run_gh(["api", f"repos/{repository}/issues/{pr_number}/comments", "-f", f"body={marker_body}"])
			if comment_result.returncode != 0:
				# One retry: a lost marker costs a duplicate judge run on the next tick.
				time.sleep(2)
				comment_result = _run_gh(["api", f"repos/{repository}/issues/{pr_number}/comments", "-f", f"body={marker_body}"])
			if comment_result.returncode != 0:
				_log(f"pr={pr_number} head={head_sha} cycle={cycle} outcome=dispatched marker=failed")
			else:
				_log(f"pr={pr_number} head={head_sha} cycle={cycle} outcome=dispatched marker=posted")
			dispatched += 1
		page_info = pull_requests.get("pageInfo") or {}
		if page_info.get("hasNextPage") is not True:
			break
		cursor = page_info.get("endCursor")
		if not isinstance(cursor, str) or not cursor:
			_log("outcome=skip reason=pagination_incomplete")
			return 0

	_log(f"outcome=complete dispatched={dispatched}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

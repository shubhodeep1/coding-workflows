#!/usr/bin/env python3
"""Decide, before the planning model runs, whether a CI-repair issue is superseded.

Issue #7216 (follow-up of #7185). A check-failure triage issue names a pull
request and the check that failed on it. When that pull request has already
merged and the named check passed on its final head, the failure is fixed:
planning edits to the pull request's former head branch would recreate the
#7185 loop. `plan.yml` runs this helper from the verified support checkout
(`python3 -I -B`, never the branch checkout, #7159) and acts on its JSON.

Eligibility (clarification Q1):
  * direct: the issue carries `ai:check-triage`, its author is the pipeline
    login or OWNER/MEMBER/COLLABORATOR, and the header of its body (the text
    before the first `---` rule that scripts/check_failure_triage.sh writes)
    has a Pull request line for this repository whose number matches the
    `check-failure-triage:pr=` marker;
  * lineage: the issue was written by the pipeline login and names its source
    with a re-issue reference ("Re-issued from #N", "Re-issued by the unblock
    judge from #N", "- Replaces: #N"). Follow-up references ("Accepted with
    this follow-up ...", "- Parent issue:", "- Source issue:", unblock fix-up
    markers) are not followed: they mark different work, and following them
    would close #7216 itself. Every branch of the walk must end at a trusted
    triage issue, and all of them must name the same pull request and check;
    anything else plans as usual.

Titles and free prose are never parsed. Every read failure, bound overrun or
unclear state returns outcome `plan`, so planning runs unchanged (fail open).

Outcomes:
  superseded              merged, and the check succeeded on the final head
  merged_check_not_passed merged, and the check failed or is still pending
  plan                    anything else (reason says why)

CLI:
  plan_superseded_merged_pr.py classify --issue-meta-file F --repo OWNER/NAME
      --pipeline-login LOGIN --out JSON
Exit 0 when the JSON was written, 2 on bad arguments, 1 on internal error.

GitHub API cost (candidates only): at most ftsg.ORIGIN_MAX_ISSUES issue reads,
one pull request read, and one check-run read or two workflow-run reads.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# python3 -I drops the script directory from sys.path; import the sibling
# guard from this file's own (trusted support) directory only.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import files_touched_scope_guard as ftsg  # noqa: E402

TRIAGE_LABEL = "ai:check-triage"
SECURITY_LABEL = "ai:security"
MARKER_PREFIX = "<!-- ai:plan-superseded:v1"

# Re-issue references, taken from ftsg.ORIGIN_REFERENCE_PATTERNS by pattern
# text so each pattern keeps one definition. Drift fails the import, and the
# workflow then plans as usual.
_REISSUE_PATTERN_TEXTS = (
	r"Re-issued from\s+#([1-9][0-9]{0,9})\b",
	r"Re-issued by the unblock judge from #([1-9][0-9]{0,9})\b",
	r"^-\s*Replaces:\s*#([1-9][0-9]{0,9})\b",
)
REISSUE_REFERENCE_PATTERNS = tuple(
	pattern for pattern in ftsg.ORIGIN_REFERENCE_PATTERNS if pattern.pattern in _REISSUE_PATTERN_TEXTS
)
if len(REISSUE_REFERENCE_PATTERNS) != len(_REISSUE_PATTERN_TEXTS):
	raise RuntimeError("files_touched_scope_guard re-issue reference patterns changed")

# A reference counts only at the start of a line (optionally after the
# `**⚠️ ` emphasis stall recovery writes), so a reference buried in prose does
# not start a walk.
_REFERENCE_LINE_PREFIX_CHARS = " \t*>⚠️"

TRIAGE_PR_MARKER_RE = re.compile(r"<!--\s*check-failure-triage:pr=([1-9][0-9]{0,9})\s*-->")
HEADER_RULE_RE = re.compile(r"^---\s*$", re.MULTILINE)
PR_LINE_RE = re.compile(
	r"^- \*\*Pull request:\*\* https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]{0,9}) \(`[^`\n]*`\)[ \t]*$",
	re.MULTILINE,
)
CHECK_LINE_RE = re.compile(r"^- \*\*Failing check:\*\* `([^`\n]{1,200})` \(conclusion: `[^`\n]*`\)[ \t]*$", re.MULTILINE)
CHECK_RUN_ID_RE = re.compile(r"^- \*\*Check run id:\*\* `([0-9]{1,20})`[ \t]*$", re.MULTILINE)
DETAILS_RE = re.compile(
	r"^- \*\*Check details:\*\* https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/actions/runs/([0-9]{1,20})\S*[ \t]*$",
	re.MULTILINE,
)
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _safe_text(value: object, limit: int = 120) -> str:
	"""Printable ASCII only, no markdown/HTML/mention characters, capped."""
	text = "".join(ch for ch in str(value) if 32 <= ord(ch) < 127 and ch not in "`<>@[]|\\")
	return " ".join(text.split())[:limit]


def _labels(meta: dict) -> set[str]:
	names: set[str] = set()
	for label in meta.get("labels") or []:
		if isinstance(label, dict) and isinstance(label.get("name"), str):
			names.add(label["name"])
		elif isinstance(label, str):
			names.add(label)
	return names


def _body(meta: dict) -> str:
	body = meta.get("body")
	return body if isinstance(body, str) else ""


def _author(meta: dict) -> str:
	user = meta.get("user")
	login = user.get("login") if isinstance(user, dict) else ""
	return login if isinstance(login, str) else ""


def _valid_branch(name: object) -> bool:
	return isinstance(name, str) and bool(BRANCH_RE.fullmatch(name)) and ".." not in name


def reissue_references(body: str) -> tuple[list[int], bool]:
	"""Line-start re-issue references, and whether a source is claimed unnamed."""
	numbers: list[int] = []
	for pattern in REISSUE_REFERENCE_PATTERNS:
		for match in pattern.finditer(body):
			line_start = body.rfind("\n", 0, match.start()) + 1
			if body[line_start:match.start()].strip(_REFERENCE_LINE_PREFIX_CHARS):
				continue
			number = int(match.group(1))
			if number not in numbers:
				numbers.append(number)
	return numbers, ftsg.origin_references(body)[1]


def is_candidate(meta: dict) -> bool:
	"""API-free pre-filter: a triage issue, or a body with a re-issue reference."""
	if TRIAGE_LABEL in _labels(meta):
		return True
	numbers, unnamed = reissue_references(_body(meta))
	return bool(numbers) or unnamed


def parse_triage_source(meta: dict, repo: str, pipeline_login: str) -> dict | None:
	"""The pull request and check a trusted triage issue names, or None."""
	if not isinstance(meta, dict) or TRIAGE_LABEL not in _labels(meta):
		return None
	if not ftsg._origin_author_trusted(meta, pipeline_login)[0]:
		return None
	body = _body(meta)
	rule = HEADER_RULE_RE.search(body)
	header = body[: rule.start()] if rule else body
	pr_match = PR_LINE_RE.search(header)
	check_match = CHECK_LINE_RE.search(header)
	marker = TRIAGE_PR_MARKER_RE.search(header)
	if not pr_match or not check_match or not marker:
		return None
	if f"{pr_match.group(1)}/{pr_match.group(2)}".lower() != repo.lower():
		return None
	pr_number = int(pr_match.group(3))
	if int(marker.group(1)) != pr_number:
		return None
	run_id_match = CHECK_RUN_ID_RE.search(header)
	details_run_id = None
	if not run_id_match:
		details = DETAILS_RE.search(header)
		if not details or f"{details.group(1)}/{details.group(2)}".lower() != repo.lower():
			return None
		details_run_id = details.group(3)
	return {
		"pr": pr_number,
		"check_name": check_match.group(1),
		"check_run_id": run_id_match.group(1) if run_id_match else None,
		"details_run_id": details_run_id,
		"source_issue": meta.get("number") if isinstance(meta.get("number"), int) else None,
	}


def resolve_source(meta: dict, repo: str, pipeline_login: str, fetch) -> tuple[str, dict | None, list[int]]:
	"""Find the one triage source of an issue: (status, source, issues read)."""
	direct = parse_triage_source(meta, repo, pipeline_login)
	if direct:
		return "ok", direct, []
	if SECURITY_LABEL in _labels(meta) or _author(meta) != pipeline_login:
		return "no_source", None, []
	numbers, unnamed = reissue_references(_body(meta))
	if unnamed:
		return "origin_unverified", None, []
	if not numbers:
		return "no_source", None, []
	frontier = [(number, 1) for number in numbers]
	seen: list[int] = []
	sources: dict[tuple[int, str], dict] = {}
	while frontier:
		number, depth = frontier.pop(0)
		if number in seen:
			continue
		if depth > ftsg.ORIGIN_MAX_DEPTH or len(seen) >= ftsg.ORIGIN_MAX_ISSUES or fetch is None:
			return "origin_unverified", None, seen
		seen.append(number)
		node = fetch(number)
		if not isinstance(node, dict):
			return "origin_unverified", None, seen
		trusted, pipeline_authored = ftsg._origin_author_trusted(node, pipeline_login)
		if not trusted:
			return "untrusted_origin", None, seen
		source = parse_triage_source(node, repo, pipeline_login)
		if source:
			source["source_issue"] = number
			sources.setdefault((source["pr"], source["check_name"]), source)
			continue
		if pipeline_authored and SECURITY_LABEL not in _labels(node):
			node_numbers, node_unnamed = reissue_references(_body(node))
			if node_unnamed:
				return "origin_unverified", None, seen
			if node_numbers:
				frontier.extend((source_number, depth + 1) for source_number in node_numbers)
				continue
		# A branch that ends anywhere but a triage issue means this issue is
		# not purely a re-issue of the CI repair: plan it.
		return "mixed_lineage", None, seen
	if len(sources) > 1:
		return "ambiguous_source", None, seen
	if not sources:
		return "no_source", None, seen
	return "ok", next(iter(sources.values())), seen


def gh_api_json(args: list[str]) -> object | None:
	"""One `gh api` read; two attempts, None on failure (fail open)."""
	for attempt in range(2):
		try:
			completed = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=60, check=False)
		except (OSError, subprocess.SubprocessError):
			completed = None
		if completed is not None and completed.returncode == 0:
			try:
				return json.loads(completed.stdout)
			except ValueError:
				return None
		if completed is not None and "HTTP 404" in (completed.stderr or ""):
			return None
		if attempt == 0:
			time.sleep(2)
	return None


class _CountingGh:
	def __init__(self, gh):
		self._gh = gh
		self.calls = 0

	def __call__(self, args: list[str]):
		self.calls += 1
		return self._gh(args)


def pr_state(repo: str, pr: int, gh) -> dict | None:
	data = gh([f"repos/{repo}/pulls/{pr}"])
	if not isinstance(data, dict):
		return None
	head = data.get("head") if isinstance(data.get("head"), dict) else {}
	base = data.get("base") if isinstance(data.get("base"), dict) else {}
	head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
	return {
		"merged": bool(data.get("merged_at")),
		"state": data.get("state") if isinstance(data.get("state"), str) else "",
		"head_sha": head.get("sha") if isinstance(head.get("sha"), str) else "",
		"head_ref": head.get("ref") if isinstance(head.get("ref"), str) else "",
		"base_ref": base.get("ref") if isinstance(base.get("ref"), str) else "",
		"head_repo": head_repo.get("full_name") if isinstance(head_repo.get("full_name"), str) else "",
	}


def check_passed_on(repo: str, source: dict, head_sha: str, gh) -> str:
	"""`success`, `failed`, `pending` or `unknown` for the named check on head_sha."""
	if source.get("check_run_id"):
		data = gh(["-X", "GET", f"repos/{repo}/commits/{head_sha}/check-runs",
			"-f", f"check_name={source['check_name']}", "-f", "filter=latest", "-f", "per_page=100"])
		if not isinstance(data, dict) or not isinstance(data.get("check_runs"), list):
			return "unknown"
		runs = [run for run in data["check_runs"] if isinstance(run, dict)]
		total = data.get("total_count") if isinstance(data.get("total_count"), int) else 0
		if not runs or total > len(runs):
			return "unknown"
		if any(run.get("status") != "completed" for run in runs):
			return "pending"
		return "success" if all(run.get("conclusion") == "success" for run in runs) else "failed"
	run_id = source.get("details_run_id")
	if not run_id:
		return "unknown"
	run = gh([f"repos/{repo}/actions/runs/{run_id}"])
	if not isinstance(run, dict):
		return "unknown"
	run_repo = run.get("repository") if isinstance(run.get("repository"), dict) else {}
	workflow_id = run.get("workflow_id")
	if str(run_repo.get("full_name", "")).lower() != repo.lower() or type(workflow_id) is not int:
		return "unknown"
	runs = gh([f"repos/{repo}/actions/workflows/{workflow_id}/runs?head_sha={head_sha}&per_page=1"])
	if not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list) or not runs["workflow_runs"]:
		return "unknown"
	newest = runs["workflow_runs"][0]
	if not isinstance(newest, dict):
		return "unknown"
	if newest.get("status") != "completed":
		return "pending"
	return "success" if newest.get("conclusion") == "success" else "failed"


def render_superseded_comment(result: dict) -> str:
	"""Issue comment for a superseded issue; never names the former head branch."""
	check = _safe_text(result["check_name"])
	sha = result["final_head_sha"]
	return "\n".join([
		f"This CI-repair issue is superseded: pull request #{result['pr']} already merged into "
		f"`{result['base_ref']}`, and the failing check `{check}` passed on its final head `{sha[:12]}`.",
		"",
		f"- Source triage issue: #{result['source_issue']}",
		f"- Merged pull request: #{result['pr']}",
		f"- Check: `{check}` (`success` on the final head)",
		"",
		"The pipeline closed this issue as not planned instead of planning edits to the pull request's former head branch. "
		"Reopening the issue does not undo this: while the pull request stays merged with this check passing, the next planning run "
		"closes it again. To plan it anyway, first set the repository variable `PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED` to `false`, "
		"then reopen the issue, replace the `ai:closed` label with `ai:planning`, and reply with `/answer`.",
		"",
		f"{MARKER_PREFIX} pr={result['pr']} head={sha} -->",
	])


def render_merged_facts(result: dict) -> str:
	"""Host-written planner facts when the pull request merged but the check did not pass."""
	check = _safe_text(result["check_name"])
	return "\n".join([
		f"Pull request #{result['pr']} named by this CI-repair issue (source issue #{result['source_issue']}) has already merged into `{result['base_ref']}`.",
		f"Final head SHA: {result['final_head_sha']}",
		f"Check `{check}` on the final head: {result['check_result']}",
		f"The pull request's former head branch is not a valid target; plan against {result['base_ref']}.",
	])


def classify(meta: dict, repo: str, pipeline_login: str, fetch=None, gh=None) -> dict:
	counting = _CountingGh(gh or gh_api_json)
	result: dict = {"outcome": "plan", "reason": "", "source_issue": None, "pr": None, "base_ref": "",
		"head_ref": "", "final_head_sha": "", "check_name": "", "check_result": "", "issues_read": [], "api_calls": 0}

	def done(outcome: str, reason: str = "") -> dict:
		result["outcome"] = outcome
		result["reason"] = reason
		result["api_calls"] = counting.calls + len(result["issues_read"])
		return result

	if not isinstance(meta, dict) or not is_candidate(meta):
		return done("plan", "not_candidate")
	if not isinstance(pipeline_login, str) or not ftsg.LOGIN_RE.fullmatch(pipeline_login):
		return done("plan", "identity_unavailable")
	if fetch is None:
		fetch = lambda number: ftsg.fetch_issue_with_gh(repo, number)  # noqa: E731
	status, source, seen = resolve_source(meta, repo, pipeline_login, fetch)
	result["issues_read"] = seen
	if status != "ok" or source is None:
		return done("plan", status)
	result["source_issue"] = source["source_issue"]
	result["pr"] = source["pr"]
	result["check_name"] = source["check_name"]
	state = pr_state(repo, source["pr"], counting)
	if state is None:
		return done("plan", "pr_read_failed")
	if not state["merged"]:
		return done("plan", "pr_open" if state["state"] == "open" else "pr_closed_unmerged")
	if state["head_repo"].lower() != repo.lower():
		return done("plan", "fork_head")
	if not SHA_RE.fullmatch(state["head_sha"]) or not _valid_branch(state["base_ref"]):
		return done("plan", "pr_read_failed")
	result["final_head_sha"] = state["head_sha"]
	result["base_ref"] = state["base_ref"]
	result["head_ref"] = state["head_ref"] if _valid_branch(state["head_ref"]) else ""
	check_result = check_passed_on(repo, source, state["head_sha"], counting)
	result["check_result"] = check_result
	if check_result == "success":
		done("superseded")
		result["comment_body"] = render_superseded_comment(result)
		return result
	if check_result in ("failed", "pending"):
		done("merged_check_not_passed")
		result["facts"] = render_merged_facts(result)
		return result
	return done("plan", "check_unknown")


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	cmd = sub.add_parser("classify")
	cmd.add_argument("--issue-meta-file", required=True)
	cmd.add_argument("--repo", required=True)
	cmd.add_argument("--pipeline-login", default="")
	cmd.add_argument("--out", required=True)
	args = parser.parse_args(argv)
	if not REPO_RE.fullmatch(args.repo):
		print("PLAN_SUPERSEDED_MERGED_PR error=invalid_repo", file=sys.stderr)
		return 2
	try:
		with open(args.issue_meta_file, encoding="utf-8") as handle:
			meta = json.load(handle)
	except (OSError, ValueError):
		meta = None
	result = classify(meta, args.repo, args.pipeline_login)
	with open(args.out, "w", encoding="utf-8") as handle:
		json.dump(result, handle)
		handle.write("\n")
	print(
		"PLAN_SUPERSEDED_MERGED_PR classify outcome={} reason={} source={} pr={} check={} api_calls={}".format(
			result["outcome"], result["reason"] or "-", result["source_issue"] or "-", result["pr"] or "-",
			_safe_text(result["check_name"]) or "-", result["api_calls"]),
		file=sys.stderr,
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

#!/usr/bin/env python3
"""Plan autopilot: audit docs/plans/ unattended and start the next plan.

Run only by the `plan_autopilot` job of .github/workflows/orchestrate_poll.yml
(issue #10091). The poll job makes the job due when PLAN_AUTOPILOT_ENABLED is
`true`, no ai:orchestrator-tracking issue is open and the
(default-branch SHA, UTC day) cache marker is missing. There is no manual
entry point.

Subcommands, in job order:

  gate      Re-check that the repository is idle and pick the orchestrate
            wrapper. Skips with save_marker=true for deterministic reasons
            (no_plans, no_orchestrator, project_open, maintenance_pr_open,
            dispatch_pending) and save_marker=false on read_failed.
  collect   Write the model's input bundle (.plan-autopilot/audit_input.json
            in the checkout): open pull requests and the decision answers
            repository members left on open ai:plan-decision issues.
  engine    Write the Codex config and resolve the PLAN_AUDIT engine.
  audit     Run the PLAN_AUDIT role in the isolated container
            (scripts/codex_isolated_exec.sh --mode workspace) with no GitHub
            token, then validate its JSON result against RESULT_SCHEMA.
  apply     Check the workspace diff against the path allowlist and the
            result, commit (refresh / archive / sweep), push
            ai/plan-autopilot-<YYYYMMDD>-<sha7>, lint the PR body with
            scripts/lint_pr_body_auto_close.py and open the maintenance PR.
  decisions Upsert one ai:plan-decision issue per NEEDS DECISION plan and
            close the ones whose answers this run applied.
  dispatch  Start the recommended plan (or bundle) as one orchestrator
            project with engine=claude.
  report    Upsert the ai:plan-autopilot status issue (report plus state
            marker), write the job summary and log one PLAN_AUTOPILOT line.

Batching contract (unattended §14). Every read is a REST call through `gh api`
with GH_TOKEN (the GH_PAT); listings follow at most PAGE_CAP pages of 100.
  gate:      one ai:orchestrator-tracking issue listing, one state=open pulls
             listing, one actions/workflows/<wrapper>/runs?per_page=20
             listing (statuses filtered locally), one ai:plan-autopilot
             issue listing (the status issue and its state marker). A
             no_plans / no_orchestrator skip makes only the status-issue
             listing (fail open).
  collect:   one ai:plan-decision issue listing, plus one comments listing
             only for decision issues whose comment count changed since the
             state marker recorded it.
  apply:     one ai:orchestrator-tracking re-listing (projects started
             during the audit own their plans), one GET /issues/{n} per
             archived plan's Refs issue (at most MAX_REFS_LOOKUPS), the
             lint's own lookups, one PR create; on the first run only (no
             status issue yet) one label create and one status-issue create,
             so the PR body and commits can carry `Refs #N`.
  decisions: one label create per label, one create or edit per decision
             issue, one comment and one close per applied decision.
  dispatch:  one ai:orchestrator-tracking re-listing and one wrapper runs
             listing (the idle re-check right before dispatching), one
             dispatch POST (two on the engine fallback), at most
             RUN_LOOKUPS run listings and, on the engine fallback only, at
             most ENGINE_LABEL_LOOKUPS tracking-issue listings.
  report:    one status-issue create or edit.
No call runs per plan, and no listing is repeated inside a loop. A failed read
fails closed: the run skips (or errors) without saving the cache marker, so a
later poll tick retries; an extra audit or dispatch costs more than a missed
tick.

Outputs go to $GITHUB_OUTPUT; stage results are kept in
$RUNNER_TEMP/plan-autopilot/state.json for the later subcommands.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

LOG_PREFIX = "PLAN_AUTOPILOT"
BRANCH_PREFIX = "ai/plan-autopilot-"
# The exact names branch_name() and run_apply push (base, then -2 / -3).
MAINTENANCE_BRANCH_RE = re.compile(r"ai/plan-autopilot-[0-9]{8}-[0-9a-f]{7}(?:-[23])?")
STATUS_LABEL = "ai:plan-autopilot"
DECISION_LABEL = "ai:plan-decision"
TRACKING_LABEL = "ai:orchestrator-tracking"
ENGINE_LABEL = "ai:engine-claude"
TRACKER_MARKER = "<!-- ai:plan-autopilot-tracker:v1 -->"
STATE_MARKER_PREFIX = "<!-- ai:plan-autopilot-state:v1 "
STATE_MARKER_RE = re.compile(r"<!-- ai:plan-autopilot-state:v1 (\{.{0,60000}?\}) -->", re.S)
DECISION_MARKER_RE = re.compile(r"<!-- ai:plan-decision:v1 plan=(docs/plans/[A-Za-z0-9._-]{1,200}\.md) -->")
PLAN_PATH_RE = re.compile(r"^docs/plans/[A-Za-z0-9._-]{1,200}\.md$")
PLAN_MENTION_RE = re.compile(r"docs/plans/[A-Za-z0-9._-]{1,200}\.md")
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
PENDING_RUN_STATUSES = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
# Consumer wrapper first, then this repository's own wrapper.
ORCHESTRATE_WRAPPERS = ("ai-orchestrate.yml", "internal-orchestrate.yml")
LABEL_CONTRACT = {
	STATUS_LABEL: ("5319e7", "Stable status issue for the plan autopilot"),
	DECISION_LABEL: ("fbca04", "Plan the autopilot cannot refresh without a decision"),
}
ROLE = "PLAN_AUDIT"
PAGE_CAP = 10
MAX_REFS_LOOKUPS = 20
RUN_LOOKUPS = 3
ENGINE_LABEL_LOOKUPS = 10
ENGINE_LABEL_WAIT_SECONDS = 60
# A dispatch recorded on the status issue this recently still counts as
# pending even when its run is not listed yet (closes the double-dispatch
# window between the dispatch POST and the run / tracking issue appearing).
DISPATCH_GRACE_SECONDS = 30 * 60
AUDIT_TIMEOUT_SECONDS = 75 * 60
DEFAULT_STALE_HOURS = 24
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_TEXT = 4000
CAPACITY_WAIT_RC = 80
SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL")
REGISTRY_PATH = "docs/scripts-pending-removal.md"
INPUT_DIR = ".plan-autopilot"
INPUT_FILE = f"{INPUT_DIR}/audit_input.json"
# The unattended sweep never deletes these, whatever a registry entry names:
# workflow, prompt and agent configuration, and the autopilot itself. The
# allowlist, not the prompt, is the security boundary (the push uses GH_PAT).
PROTECTED_PREFIXES = (".github/", ".claude/", "prompts/", "workflow-templates/", ".git/")
PROTECTED_PATHS = frozenset({"scripts/plan_autopilot.py", REGISTRY_PATH})
PERMANENT_RE = re.compile(r"permanent\s*[\u2014\u2013-]+\s*review annually", re.I)
# Same shapes as scripts/lint_pr_body_auto_close.py: `#N`, `owner/repo#N` and
# the `https://github.com/owner/repo/issues/N` URL form.
AUTO_CLOSE_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*(?:(?:[\w.-]+/[\w.-]+)?#\d+|https?://github\.com/[\w.-]+/[\w.-]+/issues/\d+)", re.I)
# A preflight result the model itself reports as not passed. The prompt makes
# `unrunnable` a fail; the apply side refuses it instead of trusting the prompt.
# The failure words are checked only in the verdict, right after `passed:`
# ("passed: FAILED, 2 callers remain", "passed: could not verify ..."), not
# in the evidence prose, so "passed: 0 callers, the script cannot be reached"
# is accepted. `unrunnable`, and wording that says the check itself could not
# be verified or confirmed, are refused anywhere in the result.
PREFLIGHT_NOT_PASSED_RE = re.compile(
	r"^\s*pass(?:ed)?\s*:\s*\W*(?:fail(?:ed|s|ure)?|error(?:ed|s)?|skip(?:ped|s)?|unrunnable|n/?a|not (?:run|pass(?:ed)?|verified|checked|executed|met)|did ?n[o']t|could ?n[o']t|cannot|can't|unable)\b"
	r"|\bunrunnable\b"
	r"|\b(?:could ?n[o']t|cannot|can't|unable to) (?:be )?(?:verif(?:y|ied)|check(?:ed)?|confirm(?:ed)?)\b",
	re.IGNORECASE,
)
# Every preflight result must open with an explicit pass verdict
# (`passed: <evidence>`, as the prompt asks); any other wording is a fail. An
# allow-list, not a list of failure phrasings: "did not pass", "not passed" or
# a trailing "FAILED" can never slip through, and a passing result that says
# "has not run since" is not refused.
# The verdict must be followed by a colon and non-empty evidence, as the
# prompt asks: a bare "pass" (or "Pass-through") carries no evidence.
PREFLIGHT_PASSED_RE = re.compile(r"\s*pass(?:ed)?\s*:\s*\S", re.IGNORECASE)

RESULT_KEYS = (
	"classification", "refreshed", "needs_decision", "applied_decisions", "archived",
	"removals", "kept_removals", "recommendation", "summary",
)
# RESULT_SCHEMA v1: the JSON object the PLAN_AUDIT role returns
# (prompts/mode-plan-audit.txt writes the same schema out for the model).
RESULT_SCHEMA: dict[str, Any] = {
	"classification": {"path": str, "status": ("COMPLETE", "PARTIAL", "NOT_IMPLEMENTED"), "freshness": ("CURRENT", "STALE", "n/a"), "evidence": str},
	"refreshed": {"path": str, "stale": str, "changed": str},
	"needs_decision": {"path": str, "questions": str},
	"applied_decisions": {"path": str, "issue": int},
	"archived": {"from": str, "to": str, "refs_issue": int},
	"removals": {"entry_title": str, "script_path": str, "preflight": list},
	"kept_removals": {"entry_title": str, "reason": str},
	"recommendation": {"kind": ("none", "single", "bundle"), "plans": list, "rationale": str},
	"summary": str,
}


class ReadError(Exception):
	"""A GitHub read (or write) failed; callers fail closed."""


class ApplyError(Exception):
	def __init__(self, reason: str, detail: str = ""):
		super().__init__(f"{reason}: {detail}")
		self.reason = reason
		self.detail = detail


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _now() -> dt.datetime:
	return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
	return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> dt.datetime | None:
	if not isinstance(value, str):
		return None
	try:
		return dt.datetime.strptime(value[:20], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
	except ValueError:
		return None


def _sleep(seconds: float) -> None:
	time.sleep(seconds)


def _clean(text: Any, limit: int = MAX_TEXT) -> str:
	"""Untrusted model or issue text, made safe to embed in an issue body."""
	value = str(text or "")
	value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
	value = value.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
	# Break @-mentions so model text never pings anyone.
	value = re.sub(r"@(?=[A-Za-z0-9])", "@\u200b", value)
	return value[:limit]


def _one_line(text: Any, limit: int = 200) -> str:
	return re.sub(r"\s+", " ", _clean(text, limit * 2)).strip()[:limit]


def log_line(outcome: str, reason: str, **fields: Any) -> str:
	parts = [f"{LOG_PREFIX} outcome={outcome}", f"reason={reason or 'none'}"]
	for key, value in fields.items():
		parts.append(f"{key}={value if value not in (None, '') else 'none'}")
	line = " ".join(parts)
	print(line)
	return line


def write_outputs(values: dict[str, Any]) -> None:
	target = os.environ.get("GITHUB_OUTPUT", "")
	lines = [f"{k}={'' if v is None else str(v).replace(chr(10), ' ')}" for k, v in values.items()]
	if target:
		with open(target, "a", encoding="utf-8") as fh:
			fh.write("\n".join(lines) + "\n")
	else:
		for line in lines:
			print(f"output: {line}")


def state_dir() -> Path:
	root = Path(os.environ.get("PLAN_AUTOPILOT_STATE_DIR") or Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "plan-autopilot")
	root.mkdir(parents=True, exist_ok=True)
	return root


def load_state() -> dict[str, Any]:
	path = state_dir() / "state.json"
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
		return data if isinstance(data, dict) else {}
	except (OSError, ValueError):
		return {}


def save_state(state: dict[str, Any]) -> None:
	path = state_dir() / "state.json"
	tmp = path.with_suffix(".tmp")
	tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
	os.replace(tmp, path)


def stale_hours_from_env(raw: str | None) -> int:
	value = (raw or "").strip()
	if re.fullmatch(r"[1-9][0-9]{0,2}", value) and int(value) <= 720:
		return int(value)
	if value:
		shown = re.sub(r"[^A-Za-z0-9_-]", "", value)[:32]
		print(f"::warning::PLAN_AUTOPILOT_MAINTENANCE_PR_STALE_HOURS is invalid (configured='{shown}'; must be an integer 1-720); using {DEFAULT_STALE_HOURS}.")
	return DEFAULT_STALE_HOURS


def valid_repo(repo: str) -> bool:
	return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo or ""))


def valid_branch(name: str) -> bool:
	return bool(re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", name or "")) and ".." not in name


# --------------------------------------------------------------------------
# GitHub access
# --------------------------------------------------------------------------

def gh_api(path: str, method: str = "GET", body: Any = None) -> Any:
	"""One `gh api` call. Raises ReadError on any failure; never logs a token."""
	cmd = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
	stdin = None
	if body is not None:
		cmd += ["--input", "-"]
		stdin = json.dumps(body)
	try:
		proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120)
	except (OSError, subprocess.SubprocessError) as exc:
		raise ReadError(f"{method} {path.split('?')[0]}: {type(exc).__name__}") from None
	if proc.returncode != 0:
		detail = (proc.stdout or "")[-600:] + " " + (proc.stderr or "")[-600:]
		raise ReadError(f"{method} {path.split('?')[0]}: rc={proc.returncode} {detail.strip()}")
	text = (proc.stdout or "").strip()
	if not text:
		return None
	try:
		return json.loads(text)
	except ValueError:
		raise ReadError(f"{method} {path.split('?')[0]}: response was not JSON") from None


def gh_list(path: str) -> list[dict[str, Any]]:
	"""Read a listing to its last page (at most PAGE_CAP pages of 100)."""
	items: list[dict[str, Any]] = []
	sep = "&" if "?" in path else "?"
	for page in range(1, PAGE_CAP + 1):
		chunk = gh_api(f"{path}{sep}per_page=100&page={page}")
		if not isinstance(chunk, list):
			raise ReadError(f"GET {path.split('?')[0]}: listing was not an array")
		items.extend(x for x in chunk if isinstance(x, dict))
		if len(chunk) < 100:
			return items
	raise ReadError(f"GET {path.split('?')[0]}: listing truncated at {PAGE_CAP} pages")


def trusted(item: dict[str, Any]) -> bool:
	return str(item.get("author_association") or "") in TRUSTED_ASSOCIATIONS


def find_status_issue(issues: list[dict[str, Any]]) -> dict[str, Any] | None:
	for issue in issues:
		if issue.get("pull_request") or not trusted(issue):
			continue
		if TRACKER_MARKER in str(issue.get("body") or ""):
			return issue
	return None


def parse_state_marker(body: str) -> dict[str, Any]:
	match = STATE_MARKER_RE.search(body or "")
	if not match:
		return {}
	try:
		data = json.loads(match.group(1))
	except ValueError:
		return {}
	return data if isinstance(data, dict) else {}


def render_state_marker(data: dict[str, Any]) -> str:
	payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
	payload = payload.replace("--", "-\\u002d")
	return f"{STATE_MARKER_PREFIX}{payload} -->"


def select_wrapper(workdir: Path) -> str:
	for name in ORCHESTRATE_WRAPPERS:
		candidate = workdir / ".github" / "workflows" / name
		if candidate.is_file() and not candidate.is_symlink():
			return name
	return ""


def list_plans(workdir: Path) -> list[str]:
	root = workdir / "docs" / "plans"
	if not root.is_dir() or root.is_symlink():
		return []
	return sorted(f"docs/plans/{p.name}" for p in root.glob("*.md") if p.is_file() and not p.is_symlink())


def in_flight_plans(tracking: list[dict[str, Any]]) -> list[str]:
	found: set[str] = set()
	for issue in tracking:
		text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
		found.update(PLAN_MENTION_RE.findall(text))
	return sorted(found)


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------

def run_gate(repo: str, workdir: Path, stale_hours: int, now: dt.datetime | None = None) -> dict[str, Any]:
	now = now or _now()
	result: dict[str, Any] = {
		"proceed": False, "reason": "", "save_marker": False, "status_issue": "",
		"stale_pr": "", "wrapper": "", "open_prs": [], "in_flight": [], "prior_state": {},
		"status_issue_body": "",
	}
	if not list_plans(workdir):
		result.update(reason="no_plans", save_marker=True)
		_attach_status_issue(repo, result)
		return result
	wrapper = select_wrapper(workdir)
	if not wrapper:
		result.update(reason="no_orchestrator", save_marker=True)
		_attach_status_issue(repo, result)
		return result
	result["wrapper"] = wrapper
	try:
		tracking = gh_list(f"repos/{repo}/issues?labels={TRACKING_LABEL}&state=open")
		tracking = [t for t in tracking if not t.get("pull_request")]
		prs = gh_list(f"repos/{repo}/pulls?state=open")
		runs = gh_api(f"repos/{repo}/actions/workflows/{wrapper}/runs?per_page=20")
		status_issues = gh_list(f"repos/{repo}/issues?labels={STATUS_LABEL}&state=open")
	except ReadError as exc:
		print(f"::warning::plan autopilot gate read failed: {_one_line(exc, 300)}")
		result.update(reason="read_failed", save_marker=False)
		return result
	if not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list):
		result.update(reason="read_failed", save_marker=False)
		return result
	_set_status_issue(result, status_issues)
	result["in_flight"] = in_flight_plans(tracking)
	result["open_prs"] = [
		{"number": p.get("number"), "head_ref": (p.get("head") or {}).get("ref", ""), "base_ref": (p.get("base") or {}).get("ref", ""), "title": _one_line(p.get("title"), 200)}
		for p in prs
	]
	if tracking:
		result.update(reason="project_open", save_marker=True)
		return result
	maintenance = [p for p in prs if is_maintenance_pr(p, repo)]
	if maintenance:
		# The pulls listing is newest-first; flag the longest-waiting PR.
		oldest = min(maintenance, key=lambda p: str(p.get("created_at") or ""))
		created = _parse_ts(oldest.get("created_at"))
		if created is not None and (now - created).total_seconds() >= stale_hours * 3600:
			result["stale_pr"] = str(oldest.get("number") or "")
		result.update(reason="maintenance_pr_open", save_marker=True)
		return result
	if any(str(r.get("status") or "") in PENDING_RUN_STATUSES for r in runs["workflow_runs"] if isinstance(r, dict)):
		result.update(reason="dispatch_pending", save_marker=True)
		return result
	dispatched_at = _parse_ts((result["prior_state"] or {}).get("dispatched_at"))
	if dispatched_at is not None and 0 <= (now - dispatched_at).total_seconds() < DISPATCH_GRACE_SECONDS:
		result.update(reason="dispatch_pending", save_marker=False)
		return result
	result.update(proceed=True, reason="idle")
	return result


def is_maintenance_pr(pr: dict[str, Any], repo: str) -> bool:
	"""Only a PR this script could have opened counts as the maintenance PR.

	The head must be a branch of this repository (a fork author picks any
	branch name, and a fork PR named ai/plan-autopilot-* would otherwise pause
	the autopilot and be flagged on the status issue), and its name must have
	the exact shape branch_name() pushes. A deleted fork (null head.repo)
	never counts.
	"""
	head = pr.get("head") or {}
	head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
	if str(head_repo.get("full_name") or "").lower() != (repo or "").lower():
		return False
	return bool(MAINTENANCE_BRANCH_RE.fullmatch(str(head.get("ref") or "")))


def _set_status_issue(result: dict[str, Any], status_issues: list[dict[str, Any]]) -> None:
	status = find_status_issue(status_issues)
	if status:
		result["status_issue"] = str(status.get("number") or "")
		result["status_issue_body"] = str(status.get("body") or "")
		result["prior_state"] = parse_state_marker(result["status_issue_body"])


def _attach_status_issue(repo: str, result: dict[str, Any]) -> None:
	"""Early skips still find the status issue so report can update it; fails open."""
	try:
		_set_status_issue(result, gh_list(f"repos/{repo}/issues?labels={STATUS_LABEL}&state=open"))
	except ReadError as exc:
		print(f"::warning::plan autopilot status issue lookup failed: {_one_line(exc, 300)}")
		# Not "no status issue": report must not create a duplicate, and a
		# later tick retries the lookup.
		result.update(status_lookup_failed=True, save_marker=False)


def cmd_gate(args: argparse.Namespace) -> int:
	stale_hours = stale_hours_from_env(os.environ.get("PLAN_AUTOPILOT_MAINTENANCE_PR_STALE_HOURS"))
	result = run_gate(args.repo, Path(args.workdir), stale_hours)
	state = {"gate": result, "repo": args.repo}
	save_state(state)
	# The report step logs the one PLAN_AUTOPILOT outcome line for a skip.
	write_outputs({k: str(result[k]).lower() if isinstance(result[k], bool) else result[k] for k in ("proceed", "reason", "save_marker", "status_issue", "stale_pr", "wrapper")})
	return 0


# --------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------

def decision_issues_from_listing(issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
	"""Map plan path -> pipeline decision issue (first match wins, never duplicated)."""
	found: dict[str, dict[str, Any]] = {}
	for issue in issues:
		if issue.get("pull_request") or not trusted(issue):
			continue
		match = DECISION_MARKER_RE.search(str(issue.get("body") or ""))
		if match and match.group(1) not in found:
			found[match.group(1)] = issue
	return found


def trusted_answers(comments: list[dict[str, Any]], asked_at: str = "") -> list[str]:
	cutoff = _parse_ts(asked_at)
	answers: list[str] = []
	for comment in comments:
		body = str(comment.get("body") or "")
		if not trusted(comment) or "<!-- ai:" in body:
			continue
		created = _parse_ts(comment.get("created_at"))
		if cutoff is not None and (created is None or created < cutoff):
			continue
		if body.strip():
			answers.append(_clean(body))
	return answers


def decision_questions(body: str) -> str:
	"""The "## Questions" section of a decision issue body (text before the marker)."""
	text = body.split("## Questions", 1)[1] if "## Questions" in body else ""
	text = text.split("<!-- ai:plan-decision:v1 ", 1)[0]
	return _clean(text.strip(), MAX_TEXT)


def run_collect(repo: str, workdir: Path, state: dict[str, Any], today: str, head_sha: str, default_branch: str) -> dict[str, Any]:
	gate = state.get("gate") or {}
	prior = (gate.get("prior_state") or {}).get("decision_issues") or {}
	# Taken before the comments are read: the next run's answer window starts
	# here, so an answer posted while this run audits is not dropped.
	collected_at = _iso(_now())
	listing = gh_list(f"repos/{repo}/issues?labels={DECISION_LABEL}&state=open")
	decisions = decision_issues_from_listing(listing)
	answers: dict[str, str] = {}
	questions: dict[str, str] = {}
	# The questions every open decision issue asks now (run state only, not
	# the status marker): decisions keeps the answer window when it re-asks
	# the same questions and restarts it when the questions change.
	asked_questions: dict[str, str] = {}
	seen: dict[str, dict[str, Any]] = {}
	for path, issue in sorted(decisions.items()):
		number = int(issue.get("number") or 0)
		count = int(issue.get("comments") or 0)
		before = prior.get(path) if isinstance(prior.get(path), dict) else {}
		seen[path] = {"number": number, "comments": count, "asked_at": str(before.get("asked_at") or "")}
		asked_questions[path] = decision_questions(str(issue.get("body") or ""))
		if count == 0 or count == int(before.get("comments") or -1):
			continue
		comments = gh_list(f"repos/{repo}/issues/{number}/comments")
		found = trusted_answers(comments, str(before.get("asked_at") or ""))
		if found:
			answers[path] = "\n\n".join(found)[:MAX_TEXT * 2]
			# The questions the answer refers to, from the issue body this
			# listing already returned (no extra call).
			questions[path] = decision_questions(str(issue.get("body") or ""))
	bundle = {
		"open_tracking": [],
		"open_prs": gate.get("open_prs") or [],
		"decision_answers": answers,
		"decision_questions": questions,
		"default_branch": default_branch,
		"head_sha": head_sha,
		"today": today,
	}
	target = workdir / INPUT_FILE
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
	return {"decision_issues": seen, "answers": sorted(answers), "input": bundle, "collected_at": collected_at, "asked_questions": asked_questions}


def _git_head(workdir: Path) -> str:
	proc = subprocess.run(["git", "-C", str(workdir), "rev-parse", "HEAD"], capture_output=True, text=True,
		env={k: v for k, v in os.environ.items() if k not in SECRET_ENV})
	sha = proc.stdout.strip()
	return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else ""


def _default_branch() -> str:
	value = os.environ.get("PLAN_AUTOPILOT_DEFAULT_BRANCH", "").strip() or "main"
	return value if valid_branch(value) else "main"


def cmd_collect(args: argparse.Namespace) -> int:
	state = load_state()
	workdir = Path(args.workdir)
	try:
		collected = run_collect(args.repo, workdir, state, _now().strftime("%Y%m%d"), _git_head(workdir), _default_branch())
	except ReadError as exc:
		print(f"::warning::plan autopilot collect read failed: {_one_line(exc, 300)}")
		state["error"] = {"stage": "collect", "reason": "read_failed"}
		save_state(state)
		log_line("skip", "read_failed", stage="collect")
		return 1
	state["collect"] = collected
	save_state(state)
	write_outputs({"collected": "true"})
	return 0


# --------------------------------------------------------------------------
# engine + audit
# --------------------------------------------------------------------------

def cmd_engine(args: argparse.Namespace) -> int:
	support = Path(args.support)
	home = Path(os.environ.get("HOME") or tempfile.gettempdir())
	config = subprocess.run(
		["bash", str(support / "scripts" / "write_codex_config.sh"), "--model", args.codex_model, "--reasoning", "xhigh",
			"--catalog-path", str((support / "scripts" / "codex_model_catalog.json").resolve())],
		capture_output=True, text=True,
	)
	if config.returncode != 0:
		print(f"::warning::plan autopilot: write_codex_config.sh failed rc={config.returncode}")
	env_file = os.environ.get("GITHUB_ENV", "")
	if env_file:
		with open(env_file, "a", encoding="utf-8") as fh:
			fh.write(f"CODEX_HOME={home / '.codex'}\n")
	resolved = subprocess.run(
		["bash", "-c", 'source "$0" >/dev/null 2>&1 || { echo codex; exit 0; }; ai_engine_for_role "$1" || echo codex',
			str(support / "scripts" / "ai_engine.sh"), ROLE],
		capture_output=True, text=True, env={**os.environ, "SUPPORT_ROOT_DIR": str(support)},
	)
	sys.stderr.write(resolved.stderr[-2000:])
	engine = resolved.stdout.strip().splitlines()[-1] if resolved.stdout.strip() else "codex"
	engine = engine if engine == "claude" else "codex"
	write_outputs({"engine": engine})
	return 0


def extract_result(text: str) -> dict[str, Any] | None:
	"""The last fenced (or bare) top-level JSON object in the model's output."""
	text = text[-MAX_RESULT_BYTES:]
	fences = re.findall(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.S)
	for candidate in reversed(fences):
		try:
			obj = json.loads(candidate)
		except ValueError:
			continue
		if isinstance(obj, dict):
			return obj
	decoder = json.JSONDecoder()
	last: dict[str, Any] | None = None
	idx = text.find("{")
	while idx != -1:
		try:
			obj, end = decoder.raw_decode(text, idx)
		except ValueError:
			idx = text.find("{", idx + 1)
			continue
		if isinstance(obj, dict) and any(k in obj for k in RESULT_KEYS):
			last = obj
		idx = text.find("{", end)
	return last


def validate_result(obj: Any) -> list[str]:
	errors: list[str] = []
	if not isinstance(obj, dict):
		return ["result is not an object"]
	missing = [k for k in RESULT_KEYS if k not in obj]
	extra = [k for k in obj if k not in RESULT_KEYS]
	if missing:
		errors.append(f"missing keys: {', '.join(missing)}")
	if extra:
		errors.append(f"unknown keys: {', '.join(sorted(extra))}")
	if errors:
		return errors
	if not isinstance(obj["summary"], str):
		errors.append("summary must be a string")
	for key in RESULT_KEYS:
		if key in ("summary", "recommendation"):
			continue
		value = obj[key]
		if not isinstance(value, list):
			errors.append(f"{key} must be an array")
			continue
		spec = RESULT_SCHEMA[key]
		for index, item in enumerate(value):
			errors.extend(f"{key}[{index}].{e}" for e in _check_fields(item, spec))
	rec = obj["recommendation"]
	errors.extend(f"recommendation.{e}" for e in _check_fields(rec, RESULT_SCHEMA["recommendation"]))
	if not errors:
		errors.extend(_semantic_errors(obj))
	return errors


def _check_fields(item: Any, spec: dict[str, Any]) -> list[str]:
	if not isinstance(item, dict):
		return ["must be an object"]
	errors = []
	if set(item) != set(spec):
		errors.append(f"fields must be exactly {sorted(spec)}")
		return errors
	for field, kind in spec.items():
		value = item[field]
		if isinstance(kind, tuple):
			if value not in kind:
				errors.append(f"{field} must be one of {list(kind)}")
		elif kind is int:
			if not isinstance(value, int) or isinstance(value, bool) or value < 0:
				errors.append(f"{field} must be a non-negative integer")
		elif not isinstance(value, kind):
			errors.append(f"{field} must be {kind.__name__}")
	return errors


def _semantic_errors(obj: dict[str, Any]) -> list[str]:
	errors: list[str] = []
	for key in ("classification", "refreshed", "needs_decision", "applied_decisions"):
		for item in obj[key]:
			if not PLAN_PATH_RE.match(item["path"]) and not (key == "classification" and re.fullmatch(r"docs/completed/[A-Za-z0-9._-]{1,200}\.md", item["path"])):
				errors.append(f"{key}: invalid plan path {_one_line(item['path'], 80)!r}")
	for item in obj["archived"]:
		if not PLAN_PATH_RE.match(item["from"]) or item["to"] != "docs/completed/" + item["from"].rsplit("/", 1)[1]:
			errors.append(f"archived: invalid move {_one_line(item['from'], 80)!r}")
		if item["refs_issue"] < 1:
			errors.append("archived: refs_issue must be a positive integer")
	for item in obj["removals"]:
		path = item["script_path"]
		if not re.fullmatch(r"[A-Za-z0-9._/-]{1,300}", path) or path.startswith("/") or ".." in path.split("/") or path.startswith(".git/"):
			errors.append(f"removals: invalid script_path {_one_line(path, 80)!r}")
		if not item["preflight"] or not all(isinstance(c, dict) and set(c) == {"check", "result"} and all(isinstance(v, str) for v in c.values()) for c in item["preflight"]):
			errors.append("removals: preflight must list {check, result} evidence")
		elif any(not PREFLIGHT_PASSED_RE.match(c["result"]) or PREFLIGHT_NOT_PASSED_RE.search(c["result"]) for c in item["preflight"]):
			errors.append("removals: a preflight check did not pass (unrunnable is a fail)")
	rec = obj["recommendation"]
	plans = rec["plans"]
	if not all(isinstance(p, str) and PLAN_PATH_RE.match(p) for p in plans):
		errors.append("recommendation.plans: invalid plan path")
	elif len(set(plans)) != len(plans):
		errors.append("recommendation.plans: duplicate plan")
	else:
		expected = {"none": (0, 0), "single": (1, 1), "bundle": (2, 3)}[rec["kind"]]
		if not expected[0] <= len(plans) <= expected[1]:
			errors.append(f"recommendation.plans: {rec['kind']} needs {expected[0]}-{expected[1]} plans")
		blocked = {item["path"] for item in obj["needs_decision"]}
		if blocked & set(plans):
			errors.append("recommendation.plans: a plan that needs a decision is never recommended")
	return errors


def cmd_audit(args: argparse.Namespace) -> int:
	state = load_state()
	support = Path(args.support).resolve()
	workdir = Path(args.workdir).resolve()
	out_dir = state_dir()
	prompt = out_dir / "prompt.md"
	output = out_dir / "audit_output.txt"
	render = subprocess.run(["bash", str(support / "scripts" / "render_prompt.sh"), str(support / "prompts" / "mode-plan-audit.txt")],
		cwd=str(support), capture_output=True, text=True)
	if render.returncode != 0 or not render.stdout.strip():
		return _audit_fail(state, "prompt_render")
	prompt.write_text(render.stdout, encoding="utf-8")
	# Taken before the model runs: an archive moves a plan out of docs/plans/.
	plans_before = list_plans(workdir)
	env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV and k != "AI_ENGINE_READ_ONLY"}
	# claude_run_selected forces the read profile (a read-only, tracked-files
	# snapshot), but PLAN_AUDIT must edit plans and read the untracked input
	# bundle. So claude_run runs with the role's configured write profile
	# (isolated --mode workspace, no secrets), and only its exit 75 runs the
	# same isolated codex command; 76 and 80 are returned as they are, as in
	# claude_run_selected.
	env["SUPPORT_ROOT_DIR"] = str(support)
	instructions = support / "unattended_system_instructions.md"
	if instructions.is_file() and not instructions.is_symlink():
		env["SUPPORT_INSTRUCTIONS_FILE"] = str(instructions)
	runner = (
		'source "$0" || exit 2; role="$1"; prompt="$2"; out="$3"; work="$4"; shift 4; '
		'engine="$(ai_engine_for_role "${role}")" || engine=codex; '
		'if [ "${engine}" = "claude" ]; then rc=0; claude_run "${role}" "${prompt}" "${out}" "${work}" || rc=$?; '
		'[ "${rc}" -eq 75 ] || exit "${rc}"; fi; '
		'"$@" < "${prompt}" > "${out}"'
	)
	cmd = [
		"timeout", "-k", "30", str(AUDIT_TIMEOUT_SECONDS),
		"bash", "-c", runner, str(support / "scripts" / "ai_engine.sh"),
		ROLE, str(prompt), str(output), str(workdir),
		"bash", str(support / "scripts" / "codex_isolated_exec.sh"), "run", "--mode", "workspace", "--workdir", str(workdir), "--",
		"--ask-for-approval", "never", "exec", "--skip-git-repo-check", "--model", args.codex_model, "--sandbox", "workspace-write",
	]
	rc = subprocess.run(cmd, env=env).returncode
	return finish_audit(state, rc, output, plans_before)


def missing_plans(result: dict[str, Any], plans: list[str]) -> list[str]:
	"""Plans under docs/plans/ before the audit that classification[] does not list.

	The docs/completed/ alias counts only for a plan this result archives: an
	older completed plan with the same file name never hides a live plan.
	"""
	listed = {str(c.get("path") or "") for c in result.get("classification") or [] if isinstance(c, dict)}
	moved = {str(a.get("from") or "") for a in result.get("archived") or [] if isinstance(a, dict)}
	return [p for p in plans if p not in listed and not (p in moved and "docs/completed/" + p.rsplit("/", 1)[1] in listed)]


def finish_audit(state: dict[str, Any], rc: int, output: Path, plans: list[str] | None = None) -> int:
	if rc == CAPACITY_WAIT_RC:
		print(f"AI_ENGINE_CAPACITY_WAIT role={ROLE} outcome=deferred stage=plan_autopilot")
		state["audit"] = {"completed": False, "capacity_wait": True}
		save_state(state)
		write_outputs({"completed": "false", "capacity_wait": "true"})
		return 0
	if rc in (124, 137):
		return _audit_fail(state, "timeout")
	if rc != 0:
		return _audit_fail(state, f"engine_rc_{rc}")
	try:
		text = output.read_text(encoding="utf-8", errors="replace")
	except OSError:
		return _audit_fail(state, "no_output")
	result = extract_result(text)
	errors = validate_result(result) if result is not None else ["no JSON object found"]
	if not errors and plans is not None:
		# Every plan must be audited: a result that leaves plans out would be
		# reported as idle_all_done and save the marker.
		omitted = missing_plans(result, plans)
		if omitted:
			errors = [f"classification omits {len(omitted)} plan(s), e.g. {omitted[0]}"]
	if errors:
		for error in errors[:10]:
			print(f"::warning::plan audit result invalid: {_one_line(error, 200)}")
		return _audit_fail(state, "result_invalid")
	state["audit"] = {"completed": True, "result": result}
	save_state(state)
	write_outputs({"completed": "true", "capacity_wait": "false"})
	return 0


def _audit_fail(state: dict[str, Any], reason: str) -> int:
	state["audit"] = {"completed": False}
	state["error"] = {"stage": "audit", "reason": reason}
	save_state(state)
	log_line("error", reason, stage="audit")
	write_outputs({"completed": "false", "capacity_wait": "false"})
	return 1


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def _git(workdir: Path, *args: str, env: dict[str, str] | None = None, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
	base_env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
	base_env.update({
		"GIT_TERMINAL_PROMPT": "0",
		"GIT_AUTHOR_NAME": "github-actions[bot]", "GIT_COMMITTER_NAME": "github-actions[bot]",
		"GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
		"GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
	})
	base_env.update(env or {})
	proc = subprocess.run(["git", "-C", str(workdir), *args], capture_output=True, text=True, env=base_env, input=input_text)
	if check and proc.returncode != 0:
		raise ApplyError("git_failed", f"git {args[0]} rc={proc.returncode}")
	return proc


def staged_changes(workdir: Path) -> list[dict[str, str]]:
	"""Parse `git diff --cached --raw -M -z` into {status, old_mode, new_mode, old_sha, new_sha, path, from}."""
	raw = _git(workdir, "diff", "--cached", "--raw", "-M", "-z", "--no-abbrev", "HEAD").stdout
	parts = raw.split("\0")
	changes: list[dict[str, str]] = []
	i = 0
	while i < len(parts):
		head = parts[i]
		if not head:
			i += 1
			continue
		fields = head.lstrip(":").split()
		if len(fields) < 5:
			raise ApplyError("diff_unparsable", head[:80])
		old_mode, new_mode, status = fields[0], fields[1], fields[4]
		shas = {"old_sha": fields[2], "new_sha": fields[3]}
		if status.startswith(("R", "C")):
			changes.append({"status": status[0], "old_mode": old_mode, "new_mode": new_mode, "from": parts[i + 1], "path": parts[i + 2], **shas})
			i += 3
		else:
			changes.append({"status": status[0], "old_mode": old_mode, "new_mode": new_mode, "from": "", "path": parts[i + 1], **shas})
			i += 2
	return changes


def registry_blocks(text: str) -> tuple[str, dict[str, str]]:
	preamble: list[str] = []
	blocks: dict[str, list[str]] = {}
	current: str | None = None
	for line in text.splitlines(keepends=True):
		if line.startswith("### "):
			current = line.strip()
			blocks.setdefault(current, [])
		if current is None:
			preamble.append(line)
		else:
			blocks[current].append(line)
	return "".join(preamble), {k: "".join(v).rstrip() for k, v in blocks.items()}


def removed_registry_paths(old_text: str, new_text: str) -> tuple[list[str], list[str]]:
	"""Return (removed headings, paths they name). Raise when the edit is not a pure removal."""
	old_pre, old_blocks = registry_blocks(old_text)
	new_pre, new_blocks = registry_blocks(new_text)
	if old_pre.rstrip() != new_pre.rstrip():
		raise ApplyError("diff_outside_allowlist", "registry preamble changed")
	for heading, body in new_blocks.items():
		if old_blocks.get(heading) != body:
			raise ApplyError("diff_outside_allowlist", "registry entry added or edited")
	removed = [h for h in old_blocks if h not in new_blocks]
	paths: list[str] = []
	for heading in removed:
		if PERMANENT_RE.search(old_blocks[heading]):
			raise ApplyError("diff_outside_allowlist", "a permanent registry entry was removed")
		for span in re.findall(r"`([^`]+)`", heading):
			if re.fullmatch(r"[A-Za-z0-9._/-]{1,300}", span) and "/" in span and ".." not in span.split("/"):
				if _protected(span):
					raise ApplyError("diff_outside_allowlist", f"registry entry names a protected path: {span}")
				paths.append(span)
	return removed, paths


def _protected(path: str) -> bool:
	"""Directories and protected paths are never deleted by the unattended sweep."""
	return path.endswith("/") or path.startswith(PROTECTED_PREFIXES) or path in PROTECTED_PATHS


def _covered(path: str, named: list[str]) -> bool:
	return any(path == n or (n.endswith("/") and path.startswith(n)) for n in named)


def check_diff(changes: list[dict[str, str]], result: dict[str, Any], in_flight: list[str], registry_old: str, registry_new: str | None, changelog_name: str, completed_exists: set[str], completed_head_blobs: dict[str, str] | None = None) -> dict[str, list[str]]:
	"""Validate staged changes against the allowlist and the result. Returns commit groups.

	completed_head_blobs maps a docs/completed/ path to its blob at HEAD; an
	archive of a plan whose identical copy is already there is just the
	deletion of the live copy (/audit-plans step 7, byte-identical case).
	"""
	refreshed = {r["path"] for r in result["refreshed"]}
	archived = {a["from"]: a["to"] for a in result["archived"]}
	removal_paths = [r["script_path"] for r in result["removals"]]
	in_flight_set = set(in_flight)
	groups: dict[str, list[str]] = {"refresh": [], "archive": [], "sweep": []}
	modified_plans: set[str] = set()
	moved_plans: set[str] = set()
	deletions: list[str] = []
	# Archive halves written as delete + add: source plan -> deleted blob, and
	# completed target -> written blob. Both must pair up with equal content.
	archive_deleted: dict[str, str] = {}
	archive_written: dict[str, str] = {}
	registry_touched = False
	for change in changes:
		status, path, old = change["status"], change["path"], change["from"]
		if change["new_mode"] in ("120000", "160000") or change["old_mode"] in ("120000", "160000"):
			raise ApplyError("diff_outside_allowlist", f"symlink or submodule {path}")
		if status in ("M", "R") and change["old_mode"] != change["new_mode"]:
			raise ApplyError("diff_outside_allowlist", f"mode change {path}")
		if status == "A" and change["new_mode"] != "100644":
			raise ApplyError("diff_outside_allowlist", f"mode {change['new_mode']} {path}")
		for touched in (path, old):
			if touched and touched in in_flight_set:
				raise ApplyError("diff_outside_allowlist", f"in-flight plan {touched}")
		if status == "M" and PLAN_PATH_RE.match(path):
			modified_plans.add(path)
			groups["refresh"].append(path)
		elif status == "R" and PLAN_PATH_RE.match(old) and archived.get(old) == path:
			if change.get("old_sha") != change.get("new_sha"):
				raise ApplyError("diff_outside_allowlist", f"archive changed the plan content: {old}")
			moved_plans.add(old)
			groups["archive"] += [old, path]
		elif status == "D" and PLAN_PATH_RE.match(path) and path in archived and archived[path] in completed_exists:
			moved_plans.add(path)
			archive_deleted[path] = change.get("old_sha", "")
			groups["archive"].append(path)
		elif status == "A" and path in archived.values() and path in completed_exists:
			# Only a new file: an `M` would replace an earlier completed plan.
			archive_written[path] = change.get("new_sha", "")
			groups["archive"].append(path)
		elif status == "M" and path == REGISTRY_PATH:
			registry_touched = True
			groups["sweep"].append(path)
		elif status == "A" and path == changelog_name and result["removals"]:
			groups["sweep"].append(path)
		elif status == "D":
			deletions.append(path)
			groups["sweep"].append(path)
		else:
			raise ApplyError("diff_outside_allowlist", f"{status} {path}")
	head_blobs = completed_head_blobs or {}
	for source, blob in archive_deleted.items():
		target = archived[source]
		if blob and archive_written.get(target) == blob:
			continue
		# The identical plan is already archived and left untouched (an `M` or
		# `D` of it is refused above or below): removing the live copy is the
		# whole archive. A different archive copy is never replaced.
		if blob and target not in archive_written and head_blobs.get(target) == blob:
			continue
		raise ApplyError("diff_outside_allowlist", f"archive target does not carry the plan content: {archived[source]}")
	for target in archive_written:
		if not any(archived[source] == target for source in archive_deleted):
			raise ApplyError("diff_outside_allowlist", f"completed plan written without its archive move: {target}")
	for path in deletions:
		if _protected(path):
			raise ApplyError("diff_outside_allowlist", f"deletion of a protected path: {path}")
	named: list[str] = []
	if registry_touched:
		if registry_new is None:
			raise ApplyError("diff_outside_allowlist", "registry missing")
		removed, named = removed_registry_paths(registry_old, registry_new)
		if not removed:
			raise ApplyError("diff_outside_allowlist", "registry edited without removing an entry")
	for path in deletions:
		if not _covered(path, named):
			raise ApplyError("diff_outside_allowlist", f"deletion not named by a removed registry entry: {path}")
	if modified_plans != refreshed - moved_plans:
		raise ApplyError("result_mismatch", "modified plans differ from refreshed[]")
	if moved_plans != set(archived):
		raise ApplyError("result_mismatch", "moved plans differ from archived[]")
	for path in deletions:
		if not _covered(path, removal_paths):
			raise ApplyError("result_mismatch", f"deletion not listed in removals[]: {path}")
	for removal in removal_paths:
		if removal not in named or not any(_covered(d, [removal]) for d in deletions):
			raise ApplyError("result_mismatch", f"removal without a deletion or registry entry: {removal}")
	if result["removals"] and not registry_touched:
		raise ApplyError("result_mismatch", "removals[] without a registry edit")
	# The reverse: every removed registry entry must be a reported removal,
	# so no entry is dropped without preflight evidence.
	if registry_touched:
		if len(removed) > len(result["removals"]):
			raise ApplyError("result_mismatch", "registry entry removed without a removals[] entry")
		for path in named:
			if path not in removal_paths:
				raise ApplyError("result_mismatch", f"registry entry removed without a removals[] entry: {path}")
	return groups


def completed_blobs_at_head(workdir: Path) -> dict[str, str]:
	"""docs/completed/*.md path -> blob SHA at HEAD (regular files only); {} on any error."""
	proc = _git(workdir, "ls-tree", "-z", "HEAD", "--", "docs/completed/", check=False)
	blobs: dict[str, str] = {}
	if proc.returncode != 0:
		return blobs
	for entry in proc.stdout.split("\0"):
		meta, _, path = entry.partition("\t")
		fields = meta.split()
		if len(fields) == 3 and fields[0] == "100644" and fields[1] == "blob" and re.fullmatch(r"docs/completed/[A-Za-z0-9._-]{1,200}\.md", path):
			blobs[path] = fields[2]
	return blobs


def branch_name(today: str, sha: str) -> str:
	return f"{BRANCH_PREFIX}{today}-{sha[:7]}"


def push_env(token: str) -> dict[str, str]:
	header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
	return {
		"GIT_TERMINAL_PROMPT": "0",
		"GIT_CONFIG_COUNT": "1",
		"GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
		"GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {header}",
	}


def _strip_auto_close(text: str) -> str:
	"""Remove auto-close references until none is left.

	One pass is not enough: removing `fix #1` from `Fixes fix #1#2` leaves
	`Fixes #2`. Every substitution shortens the text, so the loop ends.
	"""
	previous = None
	while previous != text:
		previous, text = text, AUTO_CLOSE_RE.sub("", text)
	return text


def _pr_text(text: Any, limit: int) -> str:
	"""Model- or plan-derived PR body text with auto-close references removed.

	The lint refuses keywords against tracking issues only; a "Closes #N" for
	an ordinary issue would pass it and close #N when the maintenance PR merges.
	"""
	return _strip_auto_close(_one_line(text, limit)).strip()


def build_pr_body(result: dict[str, Any], refs_info: dict[int, dict[str, Any]], status_issue: str) -> str:
	lines = ["Plan autopilot maintenance from an unattended `/audit-plans` run (`scripts/plan_autopilot.py`).", ""]
	if result["refreshed"]:
		lines += ["## Refreshed plans", ""]
		for item in result["refreshed"]:
			lines.append(f"- `{item['path']}` — stale: {_pr_text(item['stale'], 400)} → changed: {_pr_text(item['changed'], 400)}")
		lines.append("")
	if result["archived"]:
		lines += ["## Archived plans", ""]
		for item in result["archived"]:
			lines.append(f"- `{item['from']}` → `{item['to']}` — Refs #{int(item['refs_issue'])}")
		lines.append("")
	descoped: list[str] = []
	for number, info in sorted(refs_info.items()):
		if TRACKING_LABEL in info.get("labels", []) and info.get("unchecked"):
			for box in info["unchecked"]:
				descoped.append(f"- #{number}: {_pr_text(box, 200)} — not shipped by the project; the plan autopilot audit found every plan goal present on the default branch (see the evidence above).")
	if descoped:
		lines += ["## De-scoped phases", ""] + descoped + [""]
	if result["removals"]:
		lines += ["## Removed scripts", ""]
		for item in result["removals"]:
			lines.append(f"- `{item['script_path']}` ({_pr_text(item['entry_title'], 200)}) — trigger met; preflight evidence:")
			for check in item["preflight"][:20]:
				lines.append(f"  - {_pr_text(check['check'], 300)} → {_pr_text(check['result'], 300)}")
		lines.append("")
	if status_issue:
		lines.append(f"Refs #{status_issue}")
	return "\n".join(lines).strip() + "\n"


def lookup_refs(repo: str, result: dict[str, Any]) -> dict[int, dict[str, Any]]:
	info: dict[int, dict[str, Any]] = {}
	for item in result["archived"][:MAX_REFS_LOOKUPS]:
		number = int(item["refs_issue"])
		if number in info:
			continue
		issue = gh_api(f"repos/{repo}/issues/{number}")
		if not isinstance(issue, dict):
			raise ReadError(f"issue {number} unreadable")
		labels = [str(label.get("name") or "") for label in issue.get("labels") or [] if isinstance(label, dict)]
		unchecked = re.findall(r"^\s*[-*] \[ \] (.+)$", str(issue.get("body") or ""), re.M)
		info[number] = {"labels": labels, "unchecked": unchecked[:50]}
	return info


def run_lint(support: Path, body: str, commits: str, repo: str) -> bool:
	out_dir = state_dir()
	body_file = out_dir / "pr_body.md"
	commit_file = out_dir / "commit_messages.txt"
	body_file.write_text(body, encoding="utf-8")
	commit_file.write_text(commits, encoding="utf-8")
	proc = subprocess.run([sys.executable, "-I", "-B", str(support / "scripts" / "lint_pr_body_auto_close.py"),
		"--pr-body-file", str(body_file), "--commit-messages-file", str(commit_file), "--repo", repo], capture_output=True, text=True)
	sys.stdout.write(proc.stdout[-2000:])
	sys.stderr.write(proc.stderr[-2000:])
	return proc.returncode == 0


def run_apply(repo: str, workdir: Path, support: Path, state: dict[str, Any], token: str, today: str) -> dict[str, Any]:
	result = (state.get("audit") or {}).get("result")
	if not isinstance(result, dict):
		raise ApplyError("no_result", "audit result missing")
	gate = state.get("gate") or {}
	status_issue = str(gate.get("status_issue") or "")
	input_dir = workdir / INPUT_DIR
	# The model's input bundle never reaches the maintenance PR.
	if input_dir.is_symlink() or input_dir.is_file():
		input_dir.unlink()
	elif input_dir.is_dir():
		shutil.rmtree(input_dir)
	sha = _git(workdir, "rev-parse", "HEAD").stdout.strip()
	_git(workdir, "add", "-A")
	changes = staged_changes(workdir)
	if not changes:
		_git(workdir, "reset", "-q")
		return {"pr_number": "", "edited_plans": [], "branch": ""}
	registry_old = _git(workdir, "show", f"HEAD:{REGISTRY_PATH}", check=False).stdout
	registry_file = workdir / REGISTRY_PATH
	registry_new = registry_file.read_text(encoding="utf-8") if registry_file.is_file() and not registry_file.is_symlink() else None
	completed_exists = {f"docs/completed/{p.name}" for p in (workdir / "docs" / "completed").glob("*.md")} if (workdir / "docs" / "completed").is_dir() else set()
	changelog = f"changelog.d/plan-autopilot-{today}-{sha[:7]}.md"
	# The gate's listing is up to an audit old: a project that started since
	# then owns its plans, so they are re-read here (one listing).
	tracking = [t for t in gh_list(f"repos/{repo}/issues?labels={TRACKING_LABEL}&state=open") if not t.get("pull_request")]
	in_flight = sorted(set(gate.get("in_flight") or []) | set(in_flight_plans(tracking)))
	groups = check_diff(changes, result, in_flight, registry_old, registry_new, changelog, completed_exists, completed_blobs_at_head(workdir))
	refs_info = lookup_refs(repo, result)
	_git(workdir, "reset", "-q")
	if not status_issue:
		# First run: report creates the status issue only after this step, so
		# create it now (report then fills in its body) and the maintenance
		# PR body and commits can carry `Refs #N`. The gate proceeded, so its
		# status-issue listing succeeded and found none: no duplicate.
		ensure_label(repo, STATUS_LABEL)
		created = gh_api(f"repos/{repo}/issues", "POST", {"title": "Plan autopilot status", "body": f"# Plan autopilot status\n\nAudit in progress.\n\n{TRACKER_MARKER}\n", "labels": [STATUS_LABEL]})
		status_issue = str((created or {}).get("number") or "")
		if status_issue:
			gate["status_issue"] = status_issue
			state["gate"] = gate
	ref_suffix = f"\n\nRefs #{status_issue}" if status_issue else ""
	messages = {
		"refresh": "docs(plans): refresh stale plans (plan autopilot)",
		"archive": "docs(plans): archive verified-complete plans (plan autopilot)",
		"sweep": "chore: remove scripts whose removal trigger is met (plan autopilot)",
	}
	commit_log: list[str] = []
	for key in ("refresh", "archive", "sweep"):
		paths = groups[key]
		if not paths:
			continue
		_git(workdir, "add", "-A", "--", *sorted(set(paths)))
		message = messages[key] + ref_suffix
		_git(workdir, "commit", "-q", "-F", "-", input_text=message)
		commit_log.append(message)
	body = build_pr_body(result, refs_info, status_issue)
	if not run_lint(support, body, "\n\n".join(commit_log), repo):
		raise ApplyError("pr_body_lint", "lint_pr_body_auto_close.py rejected the PR body")
	env = push_env(token)
	base = branch_name(today, sha)
	branch = ""
	for candidate in (base, f"{base}-2", f"{base}-3"):
		probe = _git(workdir, "ls-remote", "--heads", "origin", f"refs/heads/{candidate}", env=env, check=False)
		if probe.returncode != 0:
			raise ApplyError("push_failed", "ls-remote failed")
		if not probe.stdout.strip():
			branch = candidate
			break
	if not branch:
		raise ApplyError("push_failed", "branch name collision")
	if _git(workdir, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", env=env, check=False).returncode != 0:
		raise ApplyError("push_failed", "git push failed")
	title = "Plan autopilot: refresh, archive and sweep plans"
	pr = gh_api(f"repos/{repo}/pulls", "POST", {"title": title, "head": branch, "base": _default_branch(), "body": body, "draft": False})
	number = str((pr or {}).get("number") or "")
	edited = sorted({r["path"] for r in result["refreshed"]} | {a["from"] for a in result["archived"]})
	return {"pr_number": number, "edited_plans": edited, "branch": branch}


def cmd_apply(args: argparse.Namespace) -> int:
	state = load_state()
	try:
		# The date the model was given (collect), so the changelog fragment
		# name still matches when the audit crosses 00:00 UTC.
		today = str(((state.get("collect") or {}).get("input") or {}).get("today") or "")
		if not re.fullmatch(r"[0-9]{8}", today):
			today = _now().strftime("%Y%m%d")
		applied = run_apply(args.repo, Path(args.workdir), Path(args.support), state, os.environ.get("GH_TOKEN", ""), today)
	except (ApplyError, ReadError) as exc:
		reason = exc.reason if isinstance(exc, ApplyError) else "read_failed"
		detail = exc.detail if isinstance(exc, ApplyError) else str(exc)
		print(f"::error::plan autopilot apply refused: {_one_line(detail, 300)}")
		state["error"] = {"stage": "apply", "reason": reason}
		save_state(state)
		log_line("error", reason, stage="apply")
		return 1
	state["apply"] = applied
	save_state(state)
	write_outputs({"pr_number": applied["pr_number"], "edited_plans": ",".join(applied["edited_plans"])})
	return 0


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------

def ensure_label(repo: str, name: str) -> None:
	color, description = LABEL_CONTRACT[name]
	try:
		gh_api(f"repos/{repo}/labels", "POST", {"name": name, "color": color, "description": description})
	except ReadError as exc:
		if "already_exists" not in str(exc) and "already exists" not in str(exc):
			raise


def decision_body(path: str, questions: str) -> str:
	return "\n".join([
		f"The plan autopilot could not refresh `{path}` without a decision.",
		"",
		"Answer in a comment with one line per question, for example `Q1: A`.",
		"Do not start the comment with `/answer`: that command starts the plan workflow on this issue.",
		"A later audit applies the answer in its maintenance PR and then closes this issue.",
		"",
		"## Questions",
		"",
		_clean(questions),
		"",
		f"<!-- ai:plan-decision:v1 plan={path} -->",
	])


def run_decisions(repo: str, state: dict[str, Any]) -> dict[str, Any]:
	result = (state.get("audit") or {}).get("result") or {}
	known = dict(((state.get("collect") or {}).get("decision_issues") or {}))
	pr_number = str((state.get("apply") or {}).get("pr_number") or "")
	# The answer window restarts at the moment collect read the comments (the
	# comment count saved below is from then too), never at this later step.
	now = str((state.get("collect") or {}).get("collected_at") or "") or _iso(_now())
	asked_questions = (state.get("collect") or {}).get("asked_questions") or {}
	opened: dict[str, int] = {}
	closed: list[int] = []
	if result.get("needs_decision"):
		ensure_label(repo, DECISION_LABEL)
	for item in result.get("needs_decision") or []:
		path = item["path"]
		body = decision_body(path, item["questions"])
		existing = known.get(path)
		# Same questions as the issue already asks: an answer posted during
		# the audit still fits them, so the window keeps starting at collect.
		# New or changed questions: the window starts when they are written,
		# so a late answer to the old questions is never paired with them.
		same_questions = bool(existing) and asked_questions.get(path) == decision_questions(body)
		if existing and int(existing.get("number") or 0) > 0:
			number = int(existing["number"])
			gh_api(f"repos/{repo}/issues/{number}", "PATCH", {"body": body})
		else:
			created = gh_api(f"repos/{repo}/issues", "POST", {"title": f"Plan decision needed: {path.rsplit('/', 1)[1]}", "body": body, "labels": [DECISION_LABEL]})
			number = int((created or {}).get("number") or 0)
		known[path] = {"number": number, "comments": int((known.get(path) or {}).get("comments") or 0), "asked_at": now if same_questions else _iso(_now())}
		opened[path] = number
	# Only plans with a trusted (OWNER / MEMBER / COLLABORATOR) answer collected
	# this run can be closed; the model's applied_decisions alone is not proof.
	answered = set((state.get("collect") or {}).get("answers") or [])
	# An answer applied without a file change opens no PR; it is still
	# applied, so its issue closes too (apply succeeded, or this step would
	# not run).
	for item in result.get("applied_decisions") or []:
		entry = known.get(item["path"])
		if not entry or item["path"] in opened or item["path"] not in answered:
			continue
		number = int(entry.get("number") or 0)
		if number <= 0:
			continue
		if not pr_number and (state.get("apply") or {}).get("branch"):
			# A branch was pushed but no PR number came back: the change is
			# not reviewable yet, so the issue stays open (the answer is kept
			# for a later audit below).
			continue
		if pr_number:
			comment = f"The plan autopilot applied this decision in #{pr_number}."
		else:
			comment = "The plan autopilot applied this decision; it needed no file change, so there is no maintenance PR."
		gh_api(f"repos/{repo}/issues/{number}/comments", "POST", {"body": comment})
		gh_api(f"repos/{repo}/issues/{number}", "PATCH", {"state": "closed", "state_reason": "completed"})
		closed.append(number)
		known.pop(item["path"], None)
	# A trusted answer collected this run but not closed (not applied, or the
	# plan was asked again) must reach a later audit: keep the earlier answer
	# window and force the next collect to re-read the comments (-1 never
	# equals a comment count), instead of storing the count that already
	# includes the answer.
	collected = (state.get("collect") or {}).get("decision_issues") or {}
	for path in answered:
		# A plan asked again keeps the new window (asked_at = collect time):
		# an answer to the old questions must not be applied to the new ones.
		if path in known and path not in opened:
			before = collected.get(path) if isinstance(collected.get(path), dict) else {}
			# No earlier window stays empty (every trusted answer counts): the
			# collect time would postdate the answer and drop it.
			known[path] = dict(known[path], comments=-1, asked_at=str(before.get("asked_at") or known[path].get("asked_at") or ""))
	return {"decision_issues": known, "opened": opened, "closed": closed}


def cmd_decisions(args: argparse.Namespace) -> int:
	state = load_state()
	try:
		state["decisions"] = run_decisions(args.repo, state)
	except ReadError as exc:
		print(f"::error::plan autopilot decisions failed: {_one_line(exc, 300)}")
		state["error"] = {"stage": "decisions", "reason": "write_failed"}
		save_state(state)
		log_line("error", "write_failed", stage="decisions")
		return 1
	save_state(state)
	return 0


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def plan_title(workdir: Path, path: str) -> str:
	try:
		for line in (workdir / path).read_text(encoding="utf-8", errors="replace").splitlines():
			if line.startswith("# "):
				return _one_line(line[2:], 150)
	except OSError:
		pass
	return path.rsplit("/", 1)[1][:-3]


def compose_description(workdir: Path, plans: list[str], default_branch: str, head_sha: str) -> str:
	titles = [plan_title(workdir, p) for p in plans]
	if len(plans) == 1:
		title = f"Implement plan: {titles[0]}"
	else:
		title = f"Implement plan bundle: {' + '.join(titles)}"
	title = _strip_auto_close(_one_line(title, 200)).strip()[:200]
	lines = [
		title,
		"",
		"This project implements a pre-written plan that is already clarified. Read the plan file(s) below from the default branch; they are the specification. Keep each plan's Phases & Merge Strategy split: one phase per issue, in the plan's order.",
	]
	if len(plans) > 1:
		lines.append("The plans are one bundle, listed in dependency order (prerequisites first); sequence them in one issue DAG.")
	lines += [
		"",
		f"Engine requested: Claude ({ENGINE_LABEL}); every model role of this project runs on Claude.",
		"",
		"Reference:",
	]
	lines += [f"- Plan: `{p}`" for p in plans]
	lines += [f"- Default branch: `{default_branch}` at `{head_sha}`", "- Started by the plan autopilot (`scripts/plan_autopilot.py`)."]
	return _strip_auto_close("\n".join(lines))


def run_dispatch(repo: str, workdir: Path, state: dict[str, Any], start: dt.datetime | None = None) -> dict[str, Any]:
	result = (state.get("audit") or {}).get("result") or {}
	rec = result.get("recommendation") or {"kind": "none", "plans": []}
	edited = set((state.get("apply") or {}).get("edited_plans") or [])
	wrapper = str((state.get("gate") or {}).get("wrapper") or "")
	out: dict[str, Any] = {"dispatched_run": "", "dispatched_plans": [], "skip_reason": "", "engine_label_pending": False}
	plans = [p for p in rec.get("plans") or [] if isinstance(p, str)]
	if rec.get("kind") == "none" or not plans:
		out["skip_reason"] = "no_recommendation"
		return out
	if any(p in edited for p in plans):
		out["skip_reason"] = "recommended_plan_edited"
		return out
	if not all(PLAN_PATH_RE.match(p) and (workdir / p).is_file() and not (workdir / p).is_symlink() for p in plans):
		out["skip_reason"] = "recommended_plan_missing"
		return out
	if wrapper not in ORCHESTRATE_WRAPPERS:
		out["skip_reason"] = "no_orchestrator"
		return out
	# The gate's idle check is up to an audit old: re-check right before the
	# dispatch so a project started meanwhile is never doubled.
	tracking = [t for t in gh_list(f"repos/{repo}/issues?labels={TRACKING_LABEL}&state=open") if not t.get("pull_request")]
	if tracking:
		out["skip_reason"] = "project_open"
		return out
	pending = gh_api(f"repos/{repo}/actions/workflows/{wrapper}/runs?per_page=20")
	if not isinstance(pending, dict) or not isinstance(pending.get("workflow_runs"), list):
		raise ReadError("GET workflow runs: unexpected response")
	if any(str(r.get("status") or "") in PENDING_RUN_STATUSES for r in pending["workflow_runs"] if isinstance(r, dict)):
		out["skip_reason"] = "dispatch_pending"
		return out
	default_branch = _default_branch()
	head_sha = str(((state.get("collect") or {}).get("input") or {}).get("head_sha") or _git_head(workdir))
	description = compose_description(workdir, plans, default_branch, head_sha)
	start = start or _now()
	endpoint = f"repos/{repo}/actions/workflows/{wrapper}/dispatches"
	engine_fallback = False
	try:
		gh_api(endpoint, "POST", {"ref": default_branch, "inputs": {"project_description": description, "engine": "claude"}})
	except ReadError as exc:
		text = str(exc)
		if "422" in text and "engine" in text:
			engine_fallback = True
			gh_api(endpoint, "POST", {"ref": default_branch, "inputs": {"project_description": description}})
		else:
			raise
	out["dispatched_plans"] = plans
	floor = start - dt.timedelta(seconds=30)
	for attempt in range(RUN_LOOKUPS):
		runs = gh_api(f"repos/{repo}/actions/workflows/{wrapper}/runs?event=workflow_dispatch&per_page=5")
		candidates = [r for r in (runs or {}).get("workflow_runs") or [] if isinstance(r, dict) and (_parse_ts(r.get("created_at")) or floor) >= floor]
		if candidates:
			newest = max(candidates, key=lambda r: str(r.get("created_at") or ""))
			out["dispatched_run"] = str(newest.get("id") or "")
			break
		if attempt + 1 < RUN_LOOKUPS:
			_sleep(5)
	if not out["dispatched_run"]:
		out["dispatched_run"] = "unknown"
	if engine_fallback:
		out["engine_label_pending"] = True
		title_line = description.splitlines()[0]
		for attempt in range(ENGINE_LABEL_LOOKUPS):
			_sleep(ENGINE_LABEL_WAIT_SECONDS)
			issues = gh_api(f"repos/{repo}/issues?labels={TRACKING_LABEL}&state=open&per_page=100")
			match = [i for i in issues or [] if isinstance(i, dict) and not i.get("pull_request") and (_parse_ts(i.get("created_at")) or floor) >= floor and title_line[:80] in str(i.get("title") or "") + str(i.get("body") or "")]
			if match:
				gh_api(f"repos/{repo}/issues/{int(match[0]['number'])}/labels", "POST", {"labels": [ENGINE_LABEL]})
				out["engine_label_pending"] = False
				break
	return out


def cmd_dispatch(args: argparse.Namespace) -> int:
	state = load_state()
	start = _now()
	try:
		state["dispatch"] = run_dispatch(args.repo, Path(args.workdir), state, start)
		state["dispatch"]["dispatched_at"] = _iso(start) if state["dispatch"]["dispatched_plans"] else ""
	except ReadError as exc:
		print(f"::error::plan autopilot dispatch failed: {_one_line(exc, 300)}")
		state["error"] = {"stage": "dispatch", "reason": "dispatch_failed"}
		save_state(state)
		log_line("error", "dispatch_failed", stage="dispatch")
		return 1
	save_state(state)
	write_outputs({"dispatched_run": state["dispatch"]["dispatched_run"], "dispatched_plans": ",".join(state["dispatch"]["dispatched_plans"])})
	return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def decide_outcome(state: dict[str, Any]) -> tuple[str, str]:
	gate = state.get("gate") or {}
	if not gate.get("proceed"):
		return "skip", str(gate.get("reason") or "unknown")
	error = state.get("error") or {}
	if error:
		return "error", str(error.get("reason") or "unknown")
	audit = state.get("audit") or {}
	if audit.get("capacity_wait"):
		return "skip", "capacity_wait"
	if not audit.get("completed"):
		return "error", "audit_incomplete"
	dispatch = state.get("dispatch") or {}
	if dispatch.get("dispatched_plans"):
		return "dispatched", "recommendation"
	if (state.get("apply") or {}).get("pr_number"):
		return "maintenance_pr", str(dispatch.get("skip_reason") or "changes")
	result = audit.get("result") or {}
	if (result.get("recommendation") or {}).get("kind", "none") != "none":
		return "no_change", str(dispatch.get("skip_reason") or "not_dispatched")
	open_work = [c for c in result.get("classification") or [] if c.get("status") != "COMPLETE" and str(c.get("path", "")).startswith("docs/plans/")]
	if not open_work and not result.get("needs_decision"):
		return "idle_all_done", "no_open_plans"
	return "idle_blocked", "no_candidate"


def next_state(state: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
	gate = state.get("gate") or {}
	data = dict(gate.get("prior_state") or {})
	audit = state.get("audit") or {}
	collect_seen = (state.get("collect") or {}).get("decision_issues")
	if audit.get("completed") and not state.get("error"):
		data["last_audit_sha"] = str(((state.get("collect") or {}).get("input") or {}).get("head_sha") or "")
		data["last_audit_at"] = _iso(now)
		decisions = (state.get("decisions") or {}).get("decision_issues")
		data["decision_issues"] = decisions if decisions is not None else (collect_seen or data.get("decision_issues") or {})
	dispatch = state.get("dispatch") or {}
	if dispatch.get("dispatched_plans"):
		data["dispatched_run"] = dispatch.get("dispatched_run", "")
		data["dispatched_at"] = dispatch.get("dispatched_at", "")
		data["dispatched_plans"] = dispatch.get("dispatched_plans", [])
		data["engine_label_pending"] = bool(dispatch.get("engine_label_pending"))
	if (state.get("apply") or {}).get("pr_number"):
		data["maintenance_pr"] = (state.get("apply") or {}).get("pr_number")
	return data


def _stale_pr_lines(gate: dict[str, Any]) -> list[str]:
	if not gate.get("stale_pr"):
		return []
	return [f"**Maintenance PR #{gate['stale_pr']} has been open longer than `PLAN_AUTOPILOT_MAINTENANCE_PR_STALE_HOURS`; the autopilot waits until it merges or closes.**", ""]


def render_skip_report(prior_body: str, gate: dict[str, Any], reason: str, data: dict[str, Any], now: dt.datetime) -> str | None:
	"""A gate skip keeps the last audit's sections and refreshes only the header.

	Returns None when the prior body is not a report this script rendered.
	"""
	if TRACKER_MARKER not in (prior_body or ""):
		return None
	lines = prior_body.split(TRACKER_MARKER)[0].rstrip().splitlines()
	idx = next((i for i, line in enumerate(lines) if line.startswith("Last run: ")), None)
	if idx is None:
		return None
	rest = lines[idx + 2:] if idx + 1 < len(lines) and lines[idx + 1].startswith("Updated by ") else lines[idx + 1:]
	while rest and not rest[0].strip():
		rest = rest[1:]
	if rest and rest[0].startswith("**Maintenance PR #"):
		rest = rest[1:]
	while rest and not rest[0].strip():
		rest = rest[1:]
	audited = str(data.get("last_audit_at") or "unknown")
	header = [
		"# Plan autopilot status",
		"",
		f"Last run: {_iso(now)} — outcome `skip`, reason `{reason}`. The sections below are from the last audit ({_one_line(audited, 40)}).",
		"Updated by the `plan_autopilot` job of `orchestrate_poll.yml` (repo variable `PLAN_AUTOPILOT_ENABLED`).",
		"",
	]
	return "\n".join(header + _stale_pr_lines(gate) + rest + ["", TRACKER_MARKER, render_state_marker(data)]) + "\n"


def render_report(state: dict[str, Any], outcome: str, reason: str, data: dict[str, Any], now: dt.datetime) -> str:
	gate = state.get("gate") or {}
	result = (state.get("audit") or {}).get("result") or {}
	lines = [
		"# Plan autopilot status",
		"",
		f"Last run: {_iso(now)} — outcome `{outcome}`, reason `{reason}`.",
		"Updated by the `plan_autopilot` job of `orchestrate_poll.yml` (repo variable `PLAN_AUTOPILOT_ENABLED`).",
		"",
	]
	lines += _stale_pr_lines(gate)
	if result.get("classification"):
		lines += ["## Classification", "", "| Plan | Status | Freshness |", "| --- | --- | --- |"]
		for item in result["classification"][:200]:
			lines.append(f"| `{item['path']}` | {item['status']} | {item['freshness']} |")
		lines.append("")
	if result.get("refreshed"):
		lines += ["## Refreshed", ""] + [f"- `{r['path']}` — {_one_line(r['changed'], 300)}" for r in result["refreshed"]] + [""]
	pending = (state.get("decisions") or {}).get("decision_issues") or data.get("decision_issues") or {}
	if pending:
		lines += ["## Decisions pending", ""] + [f"- `{p}` — #{v.get('number')}" for p, v in sorted(pending.items()) if isinstance(v, dict)] + [""]
	pr = (state.get("apply") or {}).get("pr_number")
	lines.append(f"Maintenance PR: {'#' + str(pr) if pr else 'none'}")
	dispatch = state.get("dispatch") or {}
	if dispatch.get("dispatched_plans"):
		lines.append(f"Project started: run {dispatch.get('dispatched_run')} for " + ", ".join(f"`{p}`" for p in dispatch["dispatched_plans"]))
		if dispatch.get("engine_label_pending"):
			lines.append(f"The orchestrate wrapper did not accept `engine`; add `{ENGINE_LABEL}` to the new tracking issue.")
	else:
		lines.append(f"Project started: none ({reason})")
	if result.get("summary"):
		lines += ["", "<details><summary>Audit report</summary>", "", "```text", _clean(result["summary"], 30000).replace("```", "'''"), "```", "", "</details>"]
	lines += ["", TRACKER_MARKER, render_state_marker(data)]
	return "\n".join(lines) + "\n"


def run_report(repo: str, state: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
	now = now or _now()
	outcome, reason = decide_outcome(state)
	data = next_state(state, now)
	gate = state.get("gate") or {}
	body = None
	if not gate.get("proceed"):
		body = render_skip_report(str(gate.get("status_issue_body") or ""), gate, reason, data, now)
	if body is None:
		body = render_report(state, outcome, reason, data, now)
	status_issue = str(gate.get("status_issue") or "")
	audit_ran = bool((state.get("audit") or {}).get("completed"))
	# Skips the operator must see get a status issue even before any audit.
	visible_skip = not gate.get("proceed") and not gate.get("status_lookup_failed") and (gate.get("reason") in ("no_plans", "no_orchestrator") or bool(gate.get("stale_pr")))
	wrote = ""
	write_failed = False
	try:
		if status_issue:
			gh_api(f"repos/{repo}/issues/{status_issue}", "PATCH", {"body": body})
			wrote = status_issue
		elif audit_ran or outcome == "error" or visible_skip:
			ensure_label(repo, STATUS_LABEL)
			created = gh_api(f"repos/{repo}/issues", "POST", {"title": "Plan autopilot status", "body": body, "labels": [STATUS_LABEL]})
			wrote = str((created or {}).get("number") or "")
	except ReadError as exc:
		write_failed = True
		print(f"::warning::plan autopilot status issue update failed: {_one_line(exc, 300)}")
	summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
	if summary:
		with open(summary, "a", encoding="utf-8") as fh:
			fh.write(body.split(TRACKER_MARKER)[0])
	# An unpublished report (and its state marker) is retried by a later tick.
	save_marker = (bool(gate.get("save_marker")) and not write_failed) if not gate.get("proceed") else (audit_ran and not state.get("error") and not write_failed)
	pending = (state.get("decisions") or {}).get("decision_issues") or data.get("decision_issues") or {}
	line = log_line(outcome, reason, pr=(state.get("apply") or {}).get("pr_number") or "none",
		dispatched=(state.get("dispatch") or {}).get("dispatched_run") or "none", decisions_open=len(pending))
	return {"outcome": outcome, "reason": reason, "save_marker": save_marker, "status_issue": wrote, "line": line}


def cmd_report(args: argparse.Namespace) -> int:
	state = load_state()
	report = run_report(args.repo, state)
	write_outputs({"outcome": report["outcome"], "save_marker": str(report["save_marker"]).lower(), "status_issue": report["status_issue"]})
	return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Plan autopilot (run by orchestrate_poll.yml's plan_autopilot job).")
	sub = parser.add_subparsers(dest="command", required=True)
	for name in ("gate", "collect", "engine", "audit", "apply", "decisions", "dispatch", "report"):
		p = sub.add_parser(name)
		p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
		p.add_argument("--workdir", default="repo")
		p.add_argument("--support", default=".codex-workflow-src")
		p.add_argument("--codex-model", default=os.environ.get("PLAN_AUTOPILOT_CODEX_MODEL") or "openai/gpt-6-sol")
	args = parser.parse_args(argv)
	if not valid_repo(args.repo):
		print("::error::plan autopilot: --repo must be owner/name")
		return 2
	handlers = {
		"gate": cmd_gate, "collect": cmd_collect, "engine": cmd_engine, "audit": cmd_audit,
		"apply": cmd_apply, "decisions": cmd_decisions, "dispatch": cmd_dispatch, "report": cmd_report,
	}
	return handlers[args.command](args)


if __name__ == "__main__":
	sys.exit(main())

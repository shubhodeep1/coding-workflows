#!/usr/bin/env python3
"""Turn an unblock verdict into the GitHub operations that carry it out.

The unblock judge (scripts/unblock_judge.sh,
docs/plans/replace-claude-sessions-with-cli-engine-plan.md Phase 7) asks this
script what to do, then runs the operations in order. Every decision about
which existing command resumes which stop lives here, so it is tested without
the network; the shell only executes.

  plan --verdict-file PATH --context-file PATH
      PATH of --verdict-file is `unblock_ledger.py validate` output. The
      context is `{"repo", "kind", "item", "stop", "labels", "tracking",
       "has_plan", "linked_issue", "title", "security_finding_id", "security_source_body",
       "pr_trusted", "pr_author",
       "pr_head_repo", "pr_head_sha"}` (`tracking` is the project's
       tracking issue number for a project item or a child issue, else null;
       `has_plan` is true when the issue already has an implementation plan;
       `security_finding_id` is optional and validated before reuse).
       PR provenance is validated again here; missing or malformed fields fail
       closed for issue-creating PR verdicts. Optional `source_issue_body` /
       `source_issue_labels` (a trusted PR's single linked issue, sent only by
       the conflict resolver's host-only re-issue, issue #6748; the unblock
       judge never sends them) carry that issue's security finding marker,
       `Integration branch` and `Depends on` lines onto a PR re-issue; optional
       `pr_base_branch` (same caller) is the Integration branch when the
       linked issue declares none. Trusted PR-derived issue bodies
       carry an audit-only `ai:unblock-provenance:v1` marker.
       `accept_with_followup` on an `ai:security` issue is refused here too: it
       yields only a keep-open comment and a WARNING, never a follow-up issue
       or a resume command, so the finding stays blocked (#6541).
       Optional `protected_rejection` (`{"guard": "automation-path", "run",
       "paths", "truncated"}`, issues only; the judge sets it only with
       UNBLOCK_PROTECTED_HANDOFF_ENABLED=true, #7060): the newest trusted
       guard comment on the issue is an automation-path grant rejection, so a
       fix-up would hit the same guard. `descope` and `operator_step` then
       record an operator handoff (an operator step, then a comment ending in
       the hold marker
       `<!-- ai:unblock-wait:v1 item=<n> reason=operator_handoff run=<r> -->`,
       then a WARNING) and create no fix-up; the issue stays blocked. A malformed value is dropped and the normal path runs.
      An optional `ci_owner` object `{"number", "state", "head_sha",
      "head_ref", "base_ref"}` carries the facts scripts/unblock_judge.sh read
      about the pull request that owns a CI-failure issue (#7032). `state` is
      `superseded` (merged into the default or declared integration branch
      with its required checks green: the `reissue` verdict closes the issue
      as not planned instead of re-issuing it), `open` (the re-issue carries
      the triage PR marker and a standalone `- Integration branch:` line with
      the verified PR head ref), `verified` (the marker only) or `unverified`.
      A missing or malformed object keeps today's re-issue unchanged. Only
      standalone, non-security issues use it.
  followup --context-file PATH --fixup N
      The reset to run once the fix-up issue N of a `descope` or
      `operator_step` verdict has merged (Q11).
  hold-followup --context-file PATH --kind KIND --ref REF
      The same reset, once the condition of a `wait` hold (or an operator-step
      hold) is met: KIND and REF are the hold marker's `hold=` and `ref=`.
  followup-handoff --context-file PATH --fixup N --verdict descope|operator_step
      Instead of that reset, when the context carries a `protected_rejection`
      (the item is still held by an automation-path rejection that the
      verdict followed, #7287): the operator handoff ops with
      `handoff_retry: true` on the WARNING, so a failed write is retried.
  ci-owner --item-file PATH --repo R
      Reads the already-fetched item JSON and prints
      `{"pr": n|null, "reason": s, "declared_branch": s|null}`: the pull
      request that owns a CI-failure issue (label `ai:check-triage` /
      `ai:check-triage-escalated`, or the `check-failure-triage:pr=` marker),
      and the issue's single line-anchored integration branch. Conflicting PR
      numbers or a URL into another repository give `pr: null`.
  regrant --context-file PATH --rejection-file PATH
      For a scope-blocked issue whose newest automation-path rejection
      (`unblock_ledger.py automation-rejection`) the current guard now grants:
      a comment ending in `<!-- ai:unblock-regrant:v1 item=<n> run=<r> -->`,
      then the stop's resume command. No verdict, no operator step.
  fixup-escalate --context-file PATH --origin N
      For a blocked fix-up issue the judge filed for item N: one entry in the
      `ai:operator-step` issue, a comment ending in
      `<!-- ai:unblock-fixup-escalated:v1 item=<n> -->`, and one WARNING. The
      judge never files a fix-up for a fix-up.

Output: one JSON line `{"ops": [...]}`. Operations:
  {"op": "comment", "issue": n, "body": s}
  {"op": "add_labels", "issue": n, "labels": [s]}
  {"op": "remove_label", "issue": n, "label": s}
  {"op": "create_issue", "title": s, "body": s, "labels": [s], "wait_on": n}
      (`wait_on`: after creating, post the wait marker naming the new issue
      on item n)
  {"op": "edit_files_touched", "issue": n, "paths": [s]}
  {"op": "operator_step", "key": s, "source": s, "steps": [...]}
  {"op": "auto_decision", "issue": n, "decision": {...}}
  {"op": "close", "issue": n, "reason": "not_planned", "pr": bool}
  {"op": "dispatch_review", "pr": n}
  {"op": "telegram", "level": s, "text": s}
  {"op": "heal_report", "issue": n, "kind": "issue", "label": s,
   "fingerprint": s, "reason": s, "harness_paths": [s], "project_paths": [s]}
      (the work belongs in shubhodeep1/coding-workflows: send an issue-kind
      workflow failure heal report for item n, also for a pull request, with
      its block label when the heal intake accepts it (HEAL_REPORT_LABELS),
      else another accepted label on the item; an empty label means none
      could be named and no report can be sent)
  {"op": "hold_request", "issue": n, "fingerprint": s, "body": s}
      (a project item routed to the harness: post `body`, which ends with
      `<!-- ai:unblock-harness-hold-request:v1 item=<n> fingerprint=<fp> -->`,
      on tracking issue n)
  {"op": "needs_human", "issue": n, "kind": s, "stop": s, "reason": s}
      (a ledger-forced terminal verdict, every round spent: park the item in
      the needs-human digest, scripts/operator_step_issue.py needs-human park,
      instead of closing it; only when the context's `needs_human_digest` is
      true, which unblock_judge.sh sets from NEEDS_HUMAN_DIGEST_ENABLED)

Harness routing (Phase 3 of docs/plans/validation-failure-harness-routing-plan.md,
switch UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED, default on; `false` restores
the old behaviour): a `retry_budget`, `descope`, `reissue`,
`accept_with_followup` or `operator_step` verdict whose `target_repo` is
`harness`, or whose instructions name only harness-owned paths (classified
with workflow_failure_heal.classify_validation_paths against SUPPORT_DIR and
TARGET_DIR), files no fix-up: a comment, a `heal_report`, for a project a
`hold_request`, and a DEBUG Telegram message. A `consumer` verdict naming
both harness and project paths keeps its fix-up and adds a `heal_report`.
Never applied in shubhodeep1/coding-workflows itself or to `ai:security`
issues.

Resume commands are the existing ones (Q10): on a project's tracking issue
`/re-security-pass`, `/revalidate`, `/judge_resume --reset-recovery`; on an
issue `/approved` (after `ai:awaiting-approval`), `/answer`, `/reclarify`;
on a pull request a review dispatch. Comments are posted with the pipeline's
GH_PAT identity, which those handlers accept as a trusted human comment.

Model-written text (reason, instructions, answer) never starts a line of a
comment or body: the poller's command handlers match `/judge_resume`,
`/revalidate` and `/re-security-pass` at the start of any comment line, so
every such field follows a fixed label (`Change: `, `Why: `, ...). The ledger
already folds each field onto one line.

No GitHub API calls and no network (CLAUDE.md §15). Exit 0 ok, 1 bad
arguments, 2 unreadable input.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from security_dependency import security_dependency_number

SOURCE_REPO = "shubhodeep1/coding-workflows"
PROJECT_VALIDATION_STOPS = ("validation-failed", "validate-failed", "harness-broken")
CLARIFY_STOPS = ("clarify-failed", "clarify-respond-failed", "plan-failed")
GUARD_STOPS = ("scope-blocked", "destructive-blocked")
CLOSED_LABEL = "ai:unblock-closed"
SECURITY_LABEL = "ai:security"
SECURITY_FINDING_MARKER_PREFIX = "<!-- ai:security-finding:"
SECURITY_FINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
INTEGRATION_BRANCH_LINE_RE = re.compile(r"^\s*(?:-\s*)?(?:\*\*Integration branch:\*\*|Integration branch:)\s*`?\s*([^`\n]+?)\s*`?\s*$", re.MULTILINE)
TARGET_BRANCH_LINE_RE = re.compile(r"^\s*(?:-\s*)?(?:\*\*Target branch:\*\*|Target branch:)\s*(?:`\s*([^`\n]+?)\s*`(?:\s.*)?|([^`\s]+))\s*$", re.MULTILINE)
SECURITY_BRANCH_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
MAX_COMMAND_TEXT = 300
PROTECTED_HANDOFF_VERDICTS = ("descope", "operator_step")
PROTECTED_HANDOFF_MAX_PATHS = 100
PROTECTED_HANDOFF_SHOWN_PATHS = 20
PROTECTED_HANDOFF_PATH_MAX_LEN = 300
HARNESS_REPO = SOURCE_REPO
TARGET_REPO_GUARD_VERDICTS = ("retry_budget", "descope", "reissue", "accept_with_followup", "operator_step")
# A candidate path in verdict instructions: no spaces, quotes or backticks.
HARNESS_PATH_TOKEN_RE = re.compile(r"[^\s`'\"<>()\[\]{},;]+")
HARNESS_PATH_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
HARNESS_BARE_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(?:md|py|sh|yml|yaml|json|toml|txt|js|ts|mjs|cjs)$")
HARNESS_PATH_PREFIXES = (".github/", ".claude/", "validation/", "workflow-templates/")
# Well-known file names without an extension, accepted bare or as a last segment.
HARNESS_EXTENSIONLESS_NAMES = frozenset(("Makefile", "Dockerfile", "Pipfile", "Procfile", "Gemfile", "LICENSE", "CODEOWNERS"))
HARNESS_MAX_PATH_TOKENS = 50
HARNESS_SHOWN_PATHS = 20
HARNESS_HOLD_FP_RE = re.compile(r"^[0-9a-f]{12}$")
HARNESS_HOLD_MARKER_PREFIX = "<!-- ai:unblock-harness-hold-request:v1"
# The labels the heal intake accepts on an issue-kind report: a copy of
# workflow_failure_heal.HUMAN_NEEDED_LABELS (a test keeps the two equal).
HEAL_REPORT_LABELS = (
	"ai:needs-human",
	"ai:check-triage-escalated",
	"ai:destructive-blocked",
	"ai:scope-blocked",
	"ai:harness-broken",
	"ai:resolver-escalated",
	"ai:security-pass-failed",
)
# CI-failure issues and the PR that owns them (#7032). check_failure_triage.sh
# writes both the label and the marker; unblock re-issues copy the marker.
CHECK_TRIAGE_LABELS = ("ai:check-triage", "ai:check-triage-escalated")
CHECK_TRIAGE_PR_MARKER_RE = re.compile(r"^ {0,3}<!-- check-failure-triage:pr=([0-9]{1,9}) -->[ \t]*$", re.MULTILINE)
CHECK_TRIAGE_PR_LINE_RE = re.compile(
	r"^\s*(?:-\s*)?\*\*Pull request:\*\*\s*https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([0-9]{1,9})(?![0-9])",
	re.MULTILINE,
)
CI_OWNER_STATES = ("superseded", "open", "verified", "unverified")
CI_OWNER_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class UsageError(Exception):
	"""Bad arguments: exit 1."""


class InputError(Exception):
	"""An unreadable input file: exit 2."""


class _Parser(argparse.ArgumentParser):
	def error(self, message: str) -> None:
		raise UsageError(message)


def _read_json(path: str, flag: str) -> object:
	try:
		return json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise InputError(f"cannot read {flag} {path}: {exc}") from exc


def _one_line(text: str) -> str:
	return " ".join(str(text or "").split())[:MAX_COMMAND_TEXT]


def _safe_branch_ref(branch: str) -> bool:
	return (
		bool(SECURITY_BRANCH_REF_RE.fullmatch(branch)) and ".." not in branch and "//" not in branch
		and not branch.endswith(("/", ".", ".lock"))
	)


def ci_owner_candidate(body: str, labels: list[str], repo: str) -> dict:
	"""The PR that owns a CI-failure issue, from its body only (no network)."""
	body = body if isinstance(body, str) else ""
	markers = {int(match.group(1)) for match in CHECK_TRIAGE_PR_MARKER_RE.finditer(body)}
	if not markers and not any(label in CHECK_TRIAGE_LABELS for label in labels):
		return {"pr": None, "reason": "not_ci_failure"}
	numbers = set(markers)
	foreign = False
	for match in CHECK_TRIAGE_PR_LINE_RE.finditer(body):
		if match.group(1).lower() == repo.lower():
			numbers.add(int(match.group(2)))
		else:
			foreign = True
	numbers.discard(0)
	if len(numbers) > 1:
		return {"pr": None, "reason": "conflicting_pr"}
	if not numbers:
		return {"pr": None, "reason": "foreign_repo" if foreign else "no_pr"}
	return {"pr": numbers.pop(), "reason": "marker" if markers else "pull_request_line"}


def declared_integration_branch(body: str) -> str | None:
	"""The single valid line-anchored `Integration branch:` value, or None."""
	branches = {match.group(1).strip() for match in INTEGRATION_BRANCH_LINE_RE.finditer(body if isinstance(body, str) else "")}
	if len(branches) != 1:
		return None
	branch = branches.pop()
	return branch if _safe_branch_ref(branch) else None


def _ci_owner(raw: object) -> dict | None:
	"""Validate the shell's owning-PR facts; anything malformed is `unverified`."""
	if raw is None:
		return None
	unverified = {"state": "unverified"}
	if not isinstance(raw, dict):
		return unverified
	number, state = raw.get("number"), raw.get("state")
	head_sha, head_ref = raw.get("head_sha"), raw.get("head_ref")
	if isinstance(number, bool) or not isinstance(number, int) or number < 1 or state not in CI_OWNER_STATES or state == "unverified":
		return unverified
	if head_sha is not None and (not isinstance(head_sha, str) or not CI_OWNER_SHA_RE.fullmatch(head_sha)):
		return unverified
	if head_ref is not None and (not isinstance(head_ref, str) or not _safe_branch_ref(head_ref)):
		return unverified
	if state == "superseded" and head_sha is None:
		return unverified
	if state == "open" and head_ref is None:
		return unverified
	return {"number": number, "state": state, "head_sha": head_sha, "head_ref": head_ref}


def _security_reissue_metadata(body: str, labels: list[str], item: int) -> tuple[int | None, str | None, bool]:
	try:
		dependency = security_dependency_number({"number": item, "body": body, "labels": labels})
	except ValueError:
		return None, None, True
	branches = [match.group(1).strip() for match in INTEGRATION_BRANCH_LINE_RE.finditer(body)]
	if not branches:
		branches = [(match.group(1) or match.group(2)).strip() for match in TARGET_BRANCH_LINE_RE.finditer(body)]
	if len(set(branches)) > 1 or any(not _safe_branch_ref(branch) for branch in branches):
		return None, None, True
	return dependency, branches[0] if branches else None, False


def _context(raw: object) -> dict:
	if not isinstance(raw, dict):
		raise UsageError("--context-file must hold a JSON object")
	kind = raw.get("kind")
	if kind not in ("issue", "pr", "project"):
		raise UsageError(f"unknown kind {kind!r}")
	item = raw.get("item")
	if not isinstance(item, int) or item < 1:
		raise UsageError("context 'item' must be a positive integer")
	tracking = raw.get("tracking")
	if tracking is not None and (not isinstance(tracking, int) or tracking < 1):
		raise UsageError("context 'tracking' must be a positive integer or null")
	linked = raw.get("linked_issue")
	if linked is not None and (not isinstance(linked, int) or linked < 1):
		raise UsageError("context 'linked_issue' must be a positive integer or null")
	labels = raw.get("labels") or []
	if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
		raise UsageError("context 'labels' must be a list of strings")
	finding_id = raw.get("security_finding_id")
	if not isinstance(finding_id, str) or not SECURITY_FINDING_ID_RE.fullmatch(finding_id):
		finding_id = None
	pr_author = raw.get("pr_author")
	pr_head_repo = raw.get("pr_head_repo")
	pr_head_sha = raw.get("pr_head_sha")
	pr_trusted = (
		kind == "pr"
		and raw.get("pr_trusted") is True
		and isinstance(pr_author, str)
		and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?", pr_author) is not None
		and isinstance(pr_head_repo, str)
		and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", pr_head_repo) is not None
		and pr_head_repo.lower() == str(raw.get("repo") or "").lower()
		and isinstance(pr_head_sha, str)
		and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", pr_head_sha) is not None
	)
	protected_rejection = _protected_rejection(raw.get("protected_rejection")) if kind == "issue" else None
	security_source_body = raw.get("security_source_body")
	if kind != "issue" or SECURITY_LABEL not in labels or not isinstance(security_source_body, str) or len(security_source_body) > 65536:
		security_source_body = None
	security_depends_on, security_target_branch, security_metadata_unsafe = (
		_security_reissue_metadata(security_source_body, labels, item)
		if security_source_body is not None else (None, None, False)
	)
	if security_source_body is not None and SECURITY_FINDING_MARKER_PREFIX in security_source_body and finding_id is None:
		security_metadata_unsafe = True
	# A trusted PR's linked issue is the lineage source for a PR re-issue; the
	# PR body itself stays author-controlled and is never read for metadata.
	source_body = raw.get("source_issue_body")
	source_labels = raw.get("source_issue_labels")
	pr_source_present = (
		pr_trusted
		and isinstance(source_body, str)
		and len(source_body) <= 65536
		and isinstance(source_labels, list)
		and all(isinstance(label, str) for label in source_labels)
	)
	pr_source_finding_id = None
	pr_source_depends_on, pr_source_target_branch, pr_source_metadata_unsafe = None, None, False
	if pr_source_present:
		linked = raw.get("linked_issue")
		pr_source_depends_on, pr_source_target_branch, pr_source_metadata_unsafe = _security_reissue_metadata(
			source_body, source_labels, linked if isinstance(linked, int) and linked > 0 else item
		)
		if SECURITY_LABEL in source_labels and SECURITY_FINDING_MARKER_PREFIX in source_body:
			marker = re.search(r"<!-- ai:security-finding:(.*?) -->", source_body)
			candidate = marker.group(1) if marker else ""
			if SECURITY_FINDING_ID_RE.fullmatch(candidate):
				pr_source_finding_id = candidate
			else:
				pr_source_metadata_unsafe = True
	# Optional trusted-PR base branch (sent only by the host-only re-issue,
	# issue #6748): the replacement's Integration branch when the linked issue
	# declares none. An invalid value is dropped, never trusted.
	base_branch = raw.get("pr_base_branch")
	pr_base_branch = (
		base_branch
		if pr_trusted and isinstance(base_branch, str) and SECURITY_BRANCH_REF_RE.fullmatch(base_branch)
		and ".." not in base_branch and "//" not in base_branch and not base_branch.endswith(("/", ".", ".lock"))
		else None
	)
	return {
		"repo": str(raw.get("repo") or ""),
		"kind": kind,
		"item": item,
		"stop": str(raw.get("stop") or ""),
		"labels": labels,
		"tracking": item if kind == "project" else tracking,
		"has_plan": raw.get("has_plan") is True,
		"linked_issue": linked,
		"title": _one_line(raw.get("title") or ""),
		"security_finding_id": finding_id,
		"security_source_body": security_source_body,
		"security_depends_on": security_depends_on,
		"security_target_branch": security_target_branch,
		"security_metadata_unsafe": security_metadata_unsafe,
		"pr_trusted": pr_trusted,
		"pr_author": pr_author if pr_trusted else "",
		"pr_head_repo": pr_head_repo if pr_trusted else "",
		"pr_head_sha": pr_head_sha if pr_trusted else "",
		"needs_human_digest": raw.get("needs_human_digest") is True,
		"protected_rejection": protected_rejection,
		"ci_owner": _ci_owner(raw.get("ci_owner")) if kind == "issue" else None,
		"pr_source_present": pr_source_present,
		"pr_source_finding_id": pr_source_finding_id,
		"pr_source_depends_on": pr_source_depends_on,
		"pr_source_target_branch": pr_source_target_branch,
		"pr_source_metadata_unsafe": pr_source_metadata_unsafe,
		"pr_base_branch": pr_base_branch,
	}


def _protected_rejection(raw: object) -> dict | None:
	"""A verified automation-path rejection carried by the judge, or None when malformed."""
	if not isinstance(raw, dict) or raw.get("guard") != "automation-path":
		return None
	run = raw.get("run")
	paths = raw.get("paths")
	truncated = raw.get("truncated")
	if not isinstance(run, str) or re.fullmatch(r"[0-9]{1,20}", run) is None or not isinstance(truncated, bool):
		return None
	if not isinstance(paths, list) or not 0 < len(paths) <= PROTECTED_HANDOFF_MAX_PATHS:
		return None
	for path in paths:
		if (
			not isinstance(path, str) or not path or len(path) > PROTECTED_HANDOFF_PATH_MAX_LEN
			or "`" in path or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path)
		):
			return None
	return {"guard": "automation-path", "run": run, "paths": list(paths), "truncated": truncated}


def _provenance_line(ctx: dict) -> str:
	if ctx["kind"] != "pr" or not ctx["pr_trusted"]:
		return ""
	return (
		f"<!-- ai:unblock-provenance:v1 source_pr={ctx['item']} author={ctx['pr_author']} "
		f"head_repo={ctx['pr_head_repo']} head_sha={ctx['pr_head_sha']} -->"
	)


def _untrusted_pr_ops(ctx: dict, verdict_name: str) -> list[dict]:
	item = ctx["item"]
	return [
		{
			"op": "comment", "issue": item,
			"body": (
				f"Unblock judge could not act on `{verdict_name}`: this PR's head or author "
				"could not be verified as trusted. No issue was opened from its content; "
				"the PR is being closed. A maintainer can reopen it or open an issue by hand."
			),
		},
		{"op": "close", "issue": item, "reason": "not_planned", "pr": True},
		{"op": "add_labels", "issue": item, "labels": [CLOSED_LABEL]},
		# DEBUG: closing the untrusted PR is the finished outcome; nothing is
		# left for a person to do.
		{"op": "telegram", "level": "DEBUG", "text": f"Unblock judge closed untrusted PR #{item} instead of acting on verdict {verdict_name}; no issue was created."},
	]


def _stop_label(stop: str) -> str:
	return f"ai:{stop}" if stop and stop != "project-failed" else ""


def _is_security_issue(ctx: dict) -> bool:
	return ctx["kind"] == "issue" and SECURITY_LABEL in ctx["labels"]


def _target_repo_guard_enabled(env: object = None) -> bool:
	"""UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED: unset, empty or `true` is on, `false` off."""
	env = os.environ if env is None else env
	value = str(env.get("UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED", "") or "").strip()
	if value == "" or value.lower() == "true":
		return True
	if value.lower() != "false":
		print("warning: UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED is not true or false; the target_repo guard is off", file=sys.stderr)
	return False


_OWNERSHIP_CACHE: dict = {}


def _load_ownership():
	"""The path-ownership helpers from the workflow_failure_heal.py next to this script, or None.

	Imported by explicit path, never through TARGET_DIR or the current
	directory, so a consumer checkout cannot supply the module.
	"""
	if "module" in _OWNERSHIP_CACHE:
		return _OWNERSHIP_CACHE["module"]
	module = None
	try:
		path = Path(__file__).resolve().with_name("workflow_failure_heal.py")
		spec = importlib.util.spec_from_file_location("_unblock_actions_path_ownership", path)
		if spec is None or spec.loader is None:
			raise ImportError("no loader")
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		for name in ("classify_validation_paths", "load_validation_managed_paths"):
			if not callable(getattr(module, name, None)):
				raise ImportError(name)
	except Exception:
		module = None
		print("warning: unblock target_repo backstop unavailable (ownership helper import failed); using the verdict field only", file=sys.stderr)
	_OWNERSHIP_CACHE["module"] = module
	return module


def _instruction_paths(text: object, repo: str = "") -> list[str]:
	"""Path-like tokens in verdict instructions, de-duplicated, order kept.

	Stops one token past HARNESS_MAX_PATH_TOKENS so a caller can tell a
	truncated list; callers slice back to the cap.
	"""
	if not isinstance(text, str):
		return []
	skip = {repo.lower(), HARNESS_REPO}
	found: list[str] = []
	for raw in HARNESS_PATH_TOKEN_RE.findall(text):
		if "://" in raw:
			continue
		token = raw
		while token.startswith("./"):
			token = token[2:]
		token = token.rstrip(".,;:!)]}")
		token = re.sub(r":[0-9]+(?::[0-9]+)?$", "", token)
		if not token or token.lower() in skip:
			continue
		last = token.rsplit("/", 1)[-1]
		if "/" in token:
			if not (HARNESS_PATH_EXT_RE.search(last) or last in HARNESS_EXTENSIONLESS_NAMES or token.startswith(HARNESS_PATH_PREFIXES)):
				continue
		elif not (HARNESS_BARE_FILE_RE.match(token) or token in HARNESS_EXTENSIONLESS_NAMES):
			continue
		if token not in found:
			found.append(token)
		# One past the cap, so a caller can tell "exactly the cap" from "more were dropped".
		if len(found) > HARNESS_MAX_PATH_TOKENS:
			break
	return found


def _harness_route(verdict: dict, ctx: dict, env: object = None) -> dict:
	"""Whether an instruction verdict's change belongs in the harness repository."""
	env = os.environ if env is None else env
	route = {"mode": "off", "reason": "", "harness_paths": [], "project_paths": []}
	if verdict.get("verdict") not in TARGET_REPO_GUARD_VERDICTS or not _target_repo_guard_enabled(env):
		return route
	# In the harness repository itself a fix-up is implementable: TARGET_DIR is
	# SUPPORT_DIR, and a heal report would send the work back to the same place.
	if ctx["repo"].lower() == SOURCE_REPO:
		return route
	# Security findings keep their own invariants (#6541, project #7604).
	if _is_security_issue(ctx):
		return route
	field = verdict.get("target_repo")
	if field not in ("consumer", "harness"):
		field = "consumer"
	harness_paths: list[str] = []
	project_paths: list[str] = []
	# A token the classifier drops as not repo-relative (an absolute path, a
	# `..` segment) still names a project-side file: it blocks a harness-only route.
	unlisted_project = False
	support_dir = str(env.get("SUPPORT_DIR", "") or "")
	target_dir = str(env.get("TARGET_DIR", "") or "")
	tokens = _instruction_paths(verdict.get("instructions"), ctx["repo"])
	truncated = len(tokens) > HARNESS_MAX_PATH_TOKENS
	tokens = tokens[:HARNESS_MAX_PATH_TOKENS]
	if tokens and support_dir and target_dir and os.path.isdir(support_dir) and os.path.isdir(target_dir):
		ownership = _load_ownership()
		if ownership is not None:
			try:
				managed = ownership.load_validation_managed_paths(support_dir, "HEAD")
				harness_paths, project_paths = ownership.classify_validation_paths(
					tokens, consumer_root=target_dir, support_root=support_dir, managed_paths=managed
				)
				normalize = getattr(ownership, "_normalize_validation_path", None)
				unlisted_project = callable(normalize) and any(normalize(token) is None for token in tokens)
			except Exception:
				# A failed managed-path read included: partial data could misroute.
				print("warning: unblock target_repo backstop failed; using the verdict field only", file=sys.stderr)
				harness_paths, project_paths, unlisted_project = [], [], False
	# A token list past the cap dropped later paths, so it never proves harness-only.
	if truncated:
		unlisted_project = True
	route.update(harness_paths=list(harness_paths), project_paths=list(project_paths))
	if field == "harness":
		route.update(mode="harness", reason="target_repo")
	elif harness_paths and not project_paths and not unlisted_project:
		route.update(mode="harness", reason="backstop")
	elif harness_paths:
		route.update(mode="mixed", reason="mixed_harness_part")
	else:
		route.update(mode="consumer", reason="target_repo")
	return route


def _heal_report_op(ctx: dict, verdict: dict, route: dict) -> dict:
	# The intake rejects a label outside HEAL_REPORT_LABELS; prefer the stop's
	# own label, then ai:harness-broken, then any accepted label on the item.
	stop_label = _stop_label(ctx["stop"])
	present = [name for name in HEAL_REPORT_LABELS if name in ctx["labels"]]
	if stop_label in HEAL_REPORT_LABELS:
		label = stop_label
	elif "ai:harness-broken" in present:
		label = "ai:harness-broken"
	else:
		label = present[0] if present else ""
	fp = verdict.get("fingerprint")
	return {
		"op": "heal_report",
		"issue": ctx["item"],
		# Always issue-kind: unblock_judge.yml is authorized only for `issue`
		# reports, and the issues API also reads a pull request by number.
		"kind": "issue",
		"label": label,
		"fingerprint": fp if isinstance(fp, str) and HARNESS_HOLD_FP_RE.match(fp) else "",
		"reason": route["reason"],
		"harness_paths": route["harness_paths"][:HARNESS_SHOWN_PATHS],
		"project_paths": route["project_paths"][:HARNESS_SHOWN_PATHS],
	}


def _harness_ops(ctx: dict, verdict: dict, route: dict) -> list[dict]:
	"""A harness-owned verdict: no fix-up here, a heal report instead; the item stays blocked."""
	item = ctx["item"]
	heal = _heal_report_op(ctx, verdict, route)
	# The heal intake rejects a report without an accepted label (HEAL_REPORT_LABELS),
	# so say so and page a person instead of claiming a report is on its way.
	sendable = bool(heal["label"])
	status = (
		" A workflow-failure heal report is being sent, and the item stays blocked until the harness fix ships."
		if sendable else
		" No workflow-failure heal report could be sent because this item has no escalation label the heal intake accepts;"
		" a person must file the harness change. The item stays blocked until the harness fix ships."
	)
	lines = [
		f"The unblock judge found this work belongs in `{HARNESS_REPO}` (the shared workflow harness), so no fix-up was filed here."
		+ status,
	]
	if verdict.get("instructions"):
		lines += ["", f"Change needed in the harness: {_one_line(verdict['instructions'])}"]
	shown = route["harness_paths"][:HARNESS_SHOWN_PATHS]
	if shown:
		lines += ["", "Harness paths: " + ", ".join(f"`{path}`" for path in shown)]
	lines += ["", f"Why: {_one_line(verdict.get('reason', ''))}"]
	ops: list[dict] = [{"op": "comment", "issue": item, "body": "\n".join(lines)}, heal]
	fp = verdict.get("fingerprint")
	# No hold without a report: nothing would ship the fix the hold waits for.
	if sendable and ctx["kind"] == "project" and isinstance(fp, str) and HARNESS_HOLD_FP_RE.match(fp):
		ops.append({
			"op": "hold_request",
			"issue": ctx["tracking"],
			"fingerprint": fp,
			"body": f"Unblock judge: harness-owned failure on #{item}; hold requested until the harness fix ships.\n\n"
			f"{HARNESS_HOLD_MARKER_PREFIX} item={item} fingerprint={fp} -->",
		})
	if sendable:
		ops.append({"op": "telegram", "level": "DEBUG", "text": f"Unblock judge routed #{item} ({ctx['stop']}) to a harness heal report; no fix-up filed."})
	else:
		ops.append({"op": "telegram", "level": "WARNING", "text": f"Unblock judge found #{item} ({ctx['stop']}) harness-owned, but it has no escalation label for a heal report; a person must file the harness change."})
	return ops


def _mixed_heal_ops(ctx: dict, verdict: dict, route: dict) -> list[dict]:
	"""The heal report for a mixed verdict's harness part, plus a WARNING when no report can be sent."""
	heal = _heal_report_op(ctx, verdict, route)
	ops: list[dict] = [heal]
	if not heal["label"]:
		ops.append({"op": "telegram", "level": "WARNING", "text": f"Unblock judge found a harness-owned part in #{ctx['item']} ({ctx['stop']}), but it has no escalation label for a heal report; a person must file the harness change."})
	return ops


def _approve(issue: int, labels: list[str], drop: str) -> list[dict]:
	ops: list[dict] = [
		{"op": "add_labels", "issue": issue, "labels": ["ai:awaiting-approval"]},
	]
	if drop:
		ops.append({"op": "remove_label", "issue": issue, "label": drop})
	ops.append({"op": "comment", "issue": issue, "body": "/approved"})
	return ops


def reset_ops(ctx: dict, note: str) -> list[dict]:
	"""The existing resume command for this stop (Q10 A)."""
	kind, item, stop = ctx["kind"], ctx["item"], ctx["stop"]
	label = _stop_label(stop)
	text = _one_line(note)
	if kind == "project":
		if stop == "security-pass-failed":
			return [{"op": "comment", "issue": item, "body": "/re-security-pass"}]
		if stop in PROJECT_VALIDATION_STOPS:
			return [{"op": "comment", "issue": item, "body": f"/revalidate unblock judge: {text}".rstrip()}]
		ops = [{"op": "comment", "issue": item, "body": "/judge_resume --reset-recovery"}]
		if stop in ("needs-human", "blocked") and label in ctx["labels"]:
			ops.append({"op": "remove_label", "issue": item, "label": label})
		return ops
	if kind == "pr":
		ops = [{"op": "dispatch_review", "pr": item}]
		if label and label in ctx["labels"]:
			ops.append({"op": "remove_label", "issue": item, "label": label})
		return ops
	present = label if label in ctx["labels"] else ""
	if stop in GUARD_STOPS or stop == "implement-diagnose-failed":
		if ctx["has_plan"]:
			return _approve(item, ctx["labels"], present)
		ops = [{"op": "comment", "issue": item, "body": "/reclarify"}]
		return ops + ([{"op": "remove_label", "issue": item, "label": present}] if present else [])
	if stop == "blocked":
		if ctx["has_plan"]:
			return _approve(item, ctx["labels"], present)
		return [{"op": "comment", "issue": item, "body": f"/answer {text}".rstrip()}]
	if stop == "needs-human":
		if ctx["has_plan"]:
			return _approve(item, ctx["labels"], present)
		ops = [{"op": "comment", "issue": item, "body": "/reclarify"}]
		return ops + ([{"op": "remove_label", "issue": item, "label": present}] if present else [])
	if stop in CLARIFY_STOPS:
		ops = [{"op": "comment", "issue": item, "body": "/reclarify"}]
		return ops + ([{"op": "remove_label", "issue": item, "label": present}] if present else [])
	# A side pipeline's dead end on an issue (escalated triage or heal chain,
	# a failed log analysis): clearing the label lets the pipeline that set it
	# try again, with the judge's instructions on record.
	return [{"op": "remove_label", "issue": item, "label": present}] if present else []


def _placeholder_needs_person(placeholder: str) -> bool:
	"""True when an operator_step placeholder waits on a person.

	An `_UNSET_OPERATOR_STEP` placeholder stands for a value only a person can
	supply (a secret, a setting), so the step is recorded and paged. An
	`_ENABLED` flag is an off switch on code the fix-up finishes and ships on:
	nobody is paged to turn it on (the old default-off contract held fixes
	back until someone did, #6710, #6793, #7171).
	"""
	# Same suffix as unblock_ledger.OPERATOR_PLACEHOLDER_SUFFIX, which validates it.
	return placeholder.endswith("_UNSET_OPERATOR_STEP")


def _fixup_body(ctx: dict, verdict: dict, kind_word: str) -> str:
	lines = [
		f"<!-- ai:unblock-fixup:v1 item={ctx['item']} round={verdict['round']} -->",
		f"## Unblock {kind_word} for #{ctx['item']}",
		"",
		f"The unblock judge chose `{verdict['verdict']}` for #{ctx['item']} (stop `{ctx['stop']}`). Make exactly this change.",
		"",
		f"Change: {verdict.get('instructions', '')}",
	]
	if verdict.get("placeholder"):
		if _placeholder_needs_person(verdict["placeholder"]):
			gate = (
				f"Keep the new behaviour off until a person finishes the operator step: gate only the new code path behind `{verdict['placeholder']}`"
				" (a placeholder env var; while it is unset the new code skips safely)."
			)
		else:
			gate = (
				f"Ship the new behaviour on: gate only the new code path behind `{verdict['placeholder']}` as an off switch."
				" Unset, empty or `true` (any case) keeps it on; `false` turns it off; any other value also turns it off"
				" and logs a warning. Nothing waits on a person: this change alone must clear the block."
			)
		lines += [
			"",
			gate + " Never put an existing path behind it: with the flag unset, everything that runs today (releases, tagging,"
			" dispatches, merges) keeps running unchanged.",
		]
	lines += ["", f"Why: {verdict['reason']}"]
	if _provenance_line(ctx):
		lines += ["", _provenance_line(ctx)]
	return "\n".join(lines)


def _fixup_ops(ctx: dict, verdict: dict, kind_word: str) -> list[dict]:
	body = _fixup_body(ctx, verdict, kind_word)
	title = f"Unblock {kind_word} for #{ctx['item']}: {ctx['title']}"[:240]
	if ctx["tracking"]:
		# A project's state is the poller's: it files the fix-up into the
		# current wave (adopt_unblock_fixup_requests) and posts the wait marker.
		request = "\n".join(
			[
				f"<!-- ai:unblock-fixup-request:v1 item={ctx['item']} id=unblock-{ctx['item']}-r{verdict['round']} -->",
				f"### {title}",
				"",
				body,
			]
		)
		return [{"op": "comment", "issue": ctx["tracking"], "body": request}]
	return [{"op": "create_issue", "title": title, "body": body, "labels": [], "wait_on": ctx["item"]}]


def _protected_handoff_ops(ctx: dict, verdict: dict, retryable: bool = False) -> list[dict]:
	"""Operator handoff for a protected-path rejection: no fix-up, the item stays blocked.

	`retryable` marks the WARNING op `handoff_retry: true` for a handoff the
	next judge run retries when a write fails (followup-handoff, #7287).
	"""
	item = ctx["item"]
	rejection = ctx["protected_rejection"]
	run = rejection["run"]
	paths = rejection["paths"]
	shown = ", ".join(f"`{path}`" for path in paths[:PROTECTED_HANDOFF_SHOWN_PATHS])
	more = len(paths) - PROTECTED_HANDOFF_SHOWN_PATHS
	paths_line = "Protected paths: " + shown + (f" and {more} more" if more > 0 else "")
	if rejection["truncated"]:
		paths_line += " (the rejection listed only the first 100 paths)"
	name = verdict["verdict"]
	# A retryable handoff follows a fix-up that already merged (#7287), so its
	# text must not claim no fix-up exists.
	lead = (
		f"The unblock judge's earlier `{name}` fix-up merged, but implementation run {run} was rejected by the automation-path grant guard"
		" and that is still the newest guard result, so no resume command was posted. "
		if retryable else
		f"The unblock judge chose `{name}`, but implementation run {run} was rejected by the automation-path grant guard. "
		"A fix-up issue would hit the same guard, so none was created. "
	)
	body = "\n".join(
		[
			lead + "This issue stays blocked until a person grants or makes the change.",
			"",
			paths_line,
			"",
			f"Change: {verdict.get('instructions', '')}",
			"",
			f"Why: {verdict['reason']}",
			"",
			f"<!-- ai:unblock-wait:v1 item={item} reason=operator_handoff run={run} -->",
		]
	)
	# An `_ENABLED` operator_step may carry no operator_instructions; fall back to the change itself.
	operator_text = (verdict.get("operator_instructions") if name == "operator_step" else "") or verdict.get("instructions")
	step = {
		"title": f"Make the protected-path change for #{item}",
		"instructions": "\n".join(
			[
				_one_line(operator_text or ""),
				f"Rejected run: {run}",
				"Paths: " + ", ".join(paths[:PROTECTED_HANDOFF_SHOWN_PATHS]) + (f" and {more} more" if more > 0 else ""),
			]
		),
	}
	# Only a placeholder a person must set keeps code dormant; an `_ENABLED`
	# flag ships on (see _placeholder_needs_person).
	if name == "operator_step" and verdict.get("placeholder") and _placeholder_needs_person(verdict["placeholder"]):
		step["dormant_until"] = verdict["placeholder"]
	# The operator step goes first: unblock_run_ops skips later writes after a
	# failure, so the hold marker (which stops later judge runs) is posted only
	# once the operator step is recorded. For a verdict handoff the verdict is
	# already recorded, so a failed handoff is not retried; unblock_run_ops still
	# sends a WARNING. A `retryable` handoff (#7287) keeps its fix-up wait open,
	# so the next judge run retries it.
	warning = {
		"op": "telegram", "level": "WARNING",
		"text": f"Operator handoff for #{item}: automation-path guard rejected run {run}; no fix-up created, the item stays blocked",
	}
	if retryable:
		warning["text"] = f"Operator handoff for #{item}: automation-path guard rejected run {run}; merged fix-up not resumed, the item stays blocked"
		warning["handoff_retry"] = True
	return [
		{"op": "operator_step", "key": f"unblock-{item}", "source": f"Unblock judge, #{item}", "steps": [step]},
		{"op": "comment", "issue": item, "body": body},
		warning,
	]


def followup_handoff_ops(ctx: dict, fixup: int, verdict_name: str) -> list[dict]:
	"""The handoff for a merged fix-up whose item is still held by an automation-path rejection (#7287).

	The fix-up was filed for a `descope` / `operator_step` verdict recorded
	after the rejection; resuming the item would hit the same guard, so it is
	handed to an operator instead. The verdict text is fixed, never model text.
	"""
	if fixup < 1:
		raise UsageError("--fixup must be an issue number")
	if verdict_name not in PROTECTED_HANDOFF_VERDICTS:
		raise UsageError(f"--verdict must be one of {list(PROTECTED_HANDOFF_VERDICTS)}")
	rejection = ctx.get("protected_rejection")
	if not rejection:
		raise UsageError("followup-handoff needs a valid protected_rejection in the context")
	synthetic = {
		"verdict": verdict_name,
		"reason": (
			f"Fix-up #{fixup} merged, but the newest guard result on this issue is still the automation-path grant guard's"
			f" rejection of run {rejection['run']}; resuming would hit the same guard."
		),
		"instructions": "Grant the listed paths in the issue's files_touched block or make the change by hand, then clear ai:scope-blocked.",
	}
	ops = _protected_handoff_ops(ctx, synthetic, retryable=True)
	# Its own key: the newest comment for a key supersedes earlier ones, so
	# reusing `unblock-<item>` would replace the original operator_step entry
	# (its instructions and dormant_until placeholder) with this generic one.
	ops[0]["key"] = f"unblock-handoff-{ctx['item']}"
	return ops


def hold_description(kind: str, ref: str) -> str:
	"""Plain words for a hold condition, for comments and resume notes."""
	if kind == "pr_on_branch":
		pr, _, branch = ref.partition("@")
		return f"pull request #{pr} merged and on `{branch}`"
	if kind == "item_closed":
		return f"#{ref} closed"
	if kind == "placeholder_retired":
		return f"operator step `{ref}` done"
	raise UsageError(f"unknown hold kind {kind!r}")


def plan(verdict: dict, ctx: dict) -> list[dict]:
	name = verdict.get("verdict")
	item = ctx["item"]
	ops: list[dict] = []
	if name == "wait":
		# A hold acts later, when its condition is met (hold-followup); the
		# judge posts the hold itself and never asks for operations here.
		return []
	if ctx["kind"] == "pr" and not ctx["pr_trusted"] and name in ("reissue", "descope", "operator_step", "accept_with_followup"):
		return _untrusted_pr_ops(ctx, name)
	route = _harness_route(verdict, ctx)
	# Harness-owned work goes to a heal report before the protected-path
	# operator handoff, which would ask for the change in this repository.
	if route["mode"] == "harness":
		return _harness_ops(ctx, verdict, route)
	if name in PROTECTED_HANDOFF_VERDICTS and ctx.get("protected_rejection"):
		handoff = _protected_handoff_ops(ctx, verdict)
		if route["mode"] == "mixed":
			handoff += _mixed_heal_ops(ctx, verdict, route)
		return handoff
	if name == "retry_budget":
		ops += [{"op": "comment", "issue": item, "body": f"Next attempt, per the unblock judge: {verdict['instructions']}"}]
		ops += reset_ops(ctx, verdict["instructions"])
	elif name == "auto_answer":
		if "ai:clarification" not in ctx["labels"] and "ai:planning" not in ctx["labels"]:
			ops.append({"op": "add_labels", "issue": item, "labels": ["ai:clarification"]})
		ops.append(
			{
				"op": "auto_decision",
				"issue": item,
				"decision": {
					"qid": f"U{verdict['round']}",
					"question": f"Unblock #{item} (stop {ctx['stop']})",
					"pick": _one_line(verdict["answer"]),
					"why": _one_line(verdict["reason"]),
				},
			}
		)
		ops.append({"op": "comment", "issue": item, "body": f"/answer {verdict['answer']}"})
		if "ai:clarification" not in ctx["labels"] and "ai:planning" not in ctx["labels"] and _stop_label(ctx["stop"]) in ctx["labels"]:
			ops.append({"op": "remove_label", "issue": item, "label": _stop_label(ctx["stop"])})
	elif name == "descope":
		ops += _fixup_ops(ctx, verdict, "descope")
	elif name == "operator_step":
		ops += _fixup_ops(ctx, verdict, "operator step")
		if _placeholder_needs_person(verdict["placeholder"]):
			ops.append(
				{
					"op": "operator_step",
					"key": f"unblock-{item}",
					"source": f"Unblock judge, #{item}",
					"steps": [
						{
							"title": f"Clear the block on #{item}",
							"instructions": verdict["operator_instructions"],
							"dormant_until": verdict["placeholder"],
						}
					],
				}
			)
			ops.append({"op": "telegram", "level": "WARNING", "text": f"Operator step needed for #{item}: {verdict['placeholder']}"})
	elif name == "override_guard":
		if ctx["stop"] == "scope-blocked":
			ops.append({"op": "edit_files_touched", "issue": item, "paths": verdict["paths"]})
		ops += _approve(item, ctx["labels"], _stop_label(ctx["stop"]) if _stop_label(ctx["stop"]) in ctx["labels"] else "")
	elif name == "reissue":
		title = f"Re-issue of #{item}: {ctx['title']}"[:240]
		body = "\n".join([f"Re-issued by the unblock judge from #{item}.", "", f"Specification: {verdict['instructions']}", "", f"Why: {verdict['reason']}"])
		security_issue = _is_security_issue(ctx)
		# A tracked security finding is split through the poller's fix-up
		# request; it carries the finding marker, from which the poller labels
		# the successor `ai:security` (#6541). Without a valid marker the
		# successor cannot be bound to the finding, standalone or tracked, so
		# nothing is split and the original keeps its block.
		if security_issue and (ctx.get("security_metadata_unsafe") or not ctx.get("security_finding_id")):
			return [
				{"op": "comment", "issue": item, "body": "This security finding stays open: its finding marker, dependency or target-branch metadata could not be carried to a replacement safely, so no re-issue was created."},
				{"op": "telegram", "level": "WARNING", "text": f"Unblock judge could not safely re-issue security finding #{item}; its metadata needs correction."},
			]
		if security_issue and ctx.get("security_finding_id"):
			body = f"{SECURITY_FINDING_MARKER_PREFIX}{ctx['security_finding_id']} -->\n{body}"
		if security_issue and not ctx["tracking"]:
			if ctx.get("security_target_branch"):
				body += f"\n\n- Integration branch: `{ctx['security_target_branch']}`"
			if ctx.get("security_depends_on") is not None:
				body += f"\n- Depends on: #{ctx['security_depends_on']}"
		labels = [SECURITY_LABEL] if security_issue else []
		owner = ctx.get("ci_owner") if ctx["kind"] == "issue" and not ctx["tracking"] and not security_issue else None
		if owner and owner["state"] == "superseded":
			# #7032: the PR that owned this CI failure already merged green, so a
			# replacement issue would redo finished work.
			return [
				{
					"op": "comment",
					"issue": item,
					"body": f"Superseded: owning PR #{owner['number']} merged with its required checks passing at head `{owner['head_sha']}`. No re-issue was created.",
				},
				{"op": "close", "issue": item, "reason": "not_planned", "pr": False},
			]
		if owner and owner["state"] in ("open", "verified"):
			body = f"<!-- check-failure-triage:pr={owner['number']} -->\n{body}"
			if owner["state"] == "open":
				# Only the verified, open, same-repository PR head ref; never model text.
				body += f"\n\n- Integration branch: `{owner['head_ref']}`"
		if ctx["kind"] == "pr":
			# PR body lineage is author-controlled; never route its reissue
			# into an unverified project or reapprove an issue the close event closes.
			pr_labels: list[str] = []
			pr_target_branch = None
			if ctx.get("pr_source_present"):
				if ctx.get("pr_source_metadata_unsafe"):
					return [
						{"op": "comment", "issue": item, "body": "This pull request stays open: its linked issue's security finding marker, dependency or target-branch metadata could not be carried to a replacement safely, so no re-issue was created."},
						{"op": "telegram", "level": "WARNING", "text": f"Could not safely re-issue PR #{item}; its linked issue's metadata needs correction."},
					]
				if ctx.get("pr_source_finding_id"):
					body = f"{SECURITY_FINDING_MARKER_PREFIX}{ctx['pr_source_finding_id']} -->\n{body}"
					pr_labels = [SECURITY_LABEL]
				pr_target_branch = ctx.get("pr_source_target_branch")
			pr_target_branch = pr_target_branch or ctx.get("pr_base_branch")
			if pr_target_branch:
				body += f"\n\n- Integration branch: `{pr_target_branch}`"
			if ctx.get("pr_source_present") and ctx.get("pr_source_depends_on") is not None:
				body += f"\n- Depends on: #{ctx['pr_source_depends_on']}"
			ops.append({"op": "create_issue", "title": title, "body": body + "\n\n" + _provenance_line(ctx), "labels": pr_labels, "wait_on": None})
			ops.append({"op": "close", "issue": item, "reason": "not_planned", "pr": True})
		elif ctx["tracking"]:
			ops.append(
				{
					"op": "comment",
					"issue": ctx["tracking"],
					"body": "\n".join(
						[
							f"<!-- ai:unblock-fixup-request:v1 item={item} id=unblock-{item}-r{verdict['round']} -->",
							f"### {title}",
							"",
							body,
						]
					),
				}
			)
			if not security_issue:
				ops.append({"op": "close", "issue": item, "reason": "not_planned", "pr": False})
		else:
			ops.append({"op": "create_issue", "title": title, "body": body, "labels": labels, "wait_on": None})
			if not security_issue or ctx.get("security_finding_id"):
				ops.append({"op": "close", "issue": item, "reason": "not_planned", "pr": False})
		if security_issue and (ctx["tracking"] or not ctx.get("security_finding_id")):
			location = f"the re-issue request recorded on tracking issue #{ctx['tracking']}" if ctx["tracking"] else "the newest issue that links this one"
			ops.append({"op": "comment", "issue": item, "body": f"This security finding stays open; it is tracked here until a linked fix is merged. Re-issue: see {location}."})
	elif name == "accept_with_followup":
		if _is_security_issue(ctx):
			# A security finding is never waived with an unbound follow-up; the
			# block label stays so the scan keeps seeing it (#6541).
			return [
				{
					"op": "comment",
					"issue": item,
					"body": "This security finding stays open and blocked: the unblock judge cannot accept it with a follow-up. "
					"A split must be a security-labelled re-issue bound to this finding; it stays tracked here until a linked fix is merged."
					"\n\nWhy: " + _one_line(verdict["reason"]),
				},
				{"op": "telegram", "level": "WARNING", "text": f"Unblock judge refused accept_with_followup for security finding #{item} ({ctx['stop']}); the block is kept."},
			]
		followup_body = "\n".join([f"Accepted with this follow-up by the unblock judge (#{item}).", "", f"Follow-up: {verdict['instructions']}"])
		if ctx["kind"] == "pr":
			followup_body += "\n\n" + _provenance_line(ctx)
		ops.append(
			{
				"op": "create_issue",
				"title": f"Follow-up to #{item}: {ctx['title']}"[:240],
				"body": followup_body,
				"labels": [],
				"wait_on": None,
			}
		)
		ops += reset_ops(ctx, f"accepted with a follow-up issue: {verdict['reason']}")
	elif name == "close":
		terminal_reason = verdict.get("terminal_reason")
		if isinstance(terminal_reason, str) and terminal_reason and ctx["needs_human_digest"]:
			# Plan item 4a (D6): an exhausted budget parks the item; it is never closed.
			return [{"op": "needs_human", "issue": item, "kind": ctx["kind"], "stop": ctx["stop"],
				"reason": re.sub(r"[^a-z0-9_]", "", terminal_reason)[:40] or "exhausted"}]
		if _is_security_issue(ctx):
			ops.append({"op": "add_labels", "issue": item, "labels": [CLOSED_LABEL]})
			ops.append({"op": "comment", "issue": item, "body": "The unblock judge has no verdicts left for this security finding. It stays open until a linked fix is merged.\n\nWhy: " + _one_line(verdict["reason"])})
			ops.append({"op": "telegram", "level": "CRITICAL", "text": f"Unblock judge kept security finding #{item} open ({ctx['stop']}): {_one_line(verdict['reason'])}; a person must fix or triage it"})
			return ops
		if ctx["kind"] != "project":
			# A project's tracking issue is closed by the poller, which also
			# sets its state to abandoned (the close goes through the API, §19).
			ops.append({"op": "close", "issue": item, "reason": "not_planned", "pr": ctx["kind"] == "pr"})
		ops.append({"op": "add_labels", "issue": item, "labels": [CLOSED_LABEL]})
		ops.append({"op": "telegram", "level": "CRITICAL", "text": f"Unblock judge closed #{item} ({ctx['stop']}): {_one_line(verdict['reason'])}"})
	else:
		raise UsageError(f"unknown verdict {name!r}")
	if route["mode"] == "mixed":
		# The verdict's own fix-up stays; the harness part goes to a heal report.
		ops += _mixed_heal_ops(ctx, verdict, route)
	return ops


def regrant_ops(ctx: dict, run_id: str, paths: list[str]) -> list[dict]:
	"""Clear an automation-path latch the current guard no longer sets (Q54).

	The marker comment goes first: it is the once-per-rejection-run record, so
	a resume that fails afterwards is never retried for the same run.
	"""
	if ctx["kind"] != "issue" or ctx["stop"] != "scope-blocked":
		raise UsageError("regrant applies only to a scope-blocked issue")
	if not re.fullmatch(r"[0-9]+", str(run_id)) or not paths or not all(isinstance(path, str) for path in paths):
		raise UsageError("regrant needs the rejection run and its paths")
	item = ctx["item"]
	listing = "\n".join(f"- `{path}`" for path in paths)
	body = "\n".join([
		f"The automation-path grant guard rejected run {run_id}. The current guard grants every path it rejected,"
		" so the unblock judge cleared the latch without a verdict and resumed this issue. The implementation run"
		" checks the grant again before it commits.",
		"",
		listing,
		"",
		f"<!-- ai:unblock-regrant:v1 item={item} run={run_id} -->",
	])
	return [{"op": "comment", "issue": item, "body": body}] + reset_ops(ctx, f"current guard grants rejection run {run_id}")


def fixup_escalate_ops(ctx: dict, origin: int) -> list[dict]:
	"""A blocked unblock fix-up goes to the operator once, never to another fix-up (Q56)."""
	if ctx["kind"] != "issue" or origin < 1:
		raise UsageError("fixup-escalate needs a fix-up issue and its original item")
	item = ctx["item"]
	return [
		{
			"op": "operator_step",
			"key": f"unblock-fixup-{item}",
			"source": f"Unblock judge, fix-up #{item}",
			"steps": [
				{
					"title": f"Unblock fix-up #{item} is blocked ({ctx['stop']})",
					"instructions": (
						f"#{item} is the unblock judge's fix-up for #{origin}, and it is blocked itself. The judge never files"
						f" a fix-up for a fix-up. Clear the block on #{item}, or close #{item} and decide #{origin} directly."
					),
				}
			],
		},
		{
			"op": "comment",
			"issue": item,
			"body": "\n".join([
				f"This is the unblock judge's fix-up for #{origin}, and it is blocked ({ctx['stop']}). The judge does not file"
				" another fix-up for it; it is listed in the `ai:operator-step` issue instead.",
				"",
				f"<!-- ai:unblock-fixup-escalated:v1 item={item} -->",
			]),
		},
		{"op": "telegram", "level": "WARNING", "text": f"Unblock fix-up #{item} (for #{origin}) is blocked ({ctx['stop']}); listed under Operator steps"},
	]


def build_parser() -> argparse.ArgumentParser:
	parser = _Parser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
	plan_cmd = sub.add_parser("plan")
	plan_cmd.add_argument("--verdict-file", required=True)
	plan_cmd.add_argument("--context-file", required=True)
	followup_cmd = sub.add_parser("followup")
	followup_cmd.add_argument("--context-file", required=True)
	followup_cmd.add_argument("--fixup", required=True, type=int)
	hold_followup_cmd = sub.add_parser("hold-followup")
	hold_followup_cmd.add_argument("--context-file", required=True)
	hold_followup_cmd.add_argument("--kind", required=True)
	hold_followup_cmd.add_argument("--ref", required=True)
	followup_handoff_cmd = sub.add_parser("followup-handoff")
	followup_handoff_cmd.add_argument("--context-file", required=True)
	followup_handoff_cmd.add_argument("--fixup", required=True, type=int)
	followup_handoff_cmd.add_argument("--verdict", required=True)
	owner_cmd = sub.add_parser("ci-owner")
	owner_cmd.add_argument("--item-file", required=True)
	owner_cmd.add_argument("--repo", required=True)
	regrant_cmd = sub.add_parser("regrant")
	regrant_cmd.add_argument("--context-file", required=True)
	regrant_cmd.add_argument("--rejection-file", required=True)
	escalate_cmd = sub.add_parser("fixup-escalate")
	escalate_cmd.add_argument("--context-file", required=True)
	escalate_cmd.add_argument("--origin", required=True, type=int)
	return parser


def run(argv: list[str] | None = None) -> dict:
	args = build_parser().parse_args(argv)
	if args.command == "ci-owner":
		item = _read_json(args.item_file, "--item-file")
		if not isinstance(item, dict):
			raise UsageError("--item-file must hold a JSON object")
		labels = [label.get("name") for label in item.get("labels") or [] if isinstance(label, dict) and isinstance(label.get("name"), str)]
		body = item.get("body") if isinstance(item.get("body"), str) else ""
		result = ci_owner_candidate(body, labels, args.repo)
		result["declared_branch"] = declared_integration_branch(body)
		return result
	ctx = _context(_read_json(args.context_file, "--context-file"))
	if args.command == "followup":
		if args.fixup < 1:
			raise UsageError("--fixup must be an issue number")
		return {"ops": reset_ops(ctx, f"fix-up #{args.fixup} merged")}
	if args.command == "hold-followup":
		return {"ops": reset_ops(ctx, f"waited for {hold_description(args.kind, args.ref)}")}
	if args.command == "followup-handoff":
		return {"ops": followup_handoff_ops(ctx, args.fixup, args.verdict)}
	if args.command == "regrant":
		rejection = _read_json(args.rejection_file, "--rejection-file")
		if not isinstance(rejection, dict) or rejection.get("status") != "ok":
			raise UsageError("--rejection-file must hold an ok automation-path rejection")
		return {"ops": regrant_ops(ctx, rejection.get("run", ""), rejection.get("paths") or [])}
	if args.command == "fixup-escalate":
		return {"ops": fixup_escalate_ops(ctx, args.origin)}
	verdict = _read_json(args.verdict_file, "--verdict-file")
	if not isinstance(verdict, dict):
		raise UsageError("--verdict-file must hold a JSON object")
	return {"ops": plan(verdict, ctx)}


def main(argv: list[str] | None = None) -> int:
	try:
		result = run(argv)
	except UsageError as exc:
		print(json.dumps({"error": str(exc)}))
		return 1
	except InputError as exc:
		print(json.dumps({"error": str(exc)}))
		return 2
	print(json.dumps(result))
	return 0


if __name__ == "__main__":
	sys.exit(main())

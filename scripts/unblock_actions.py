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
       closed for issue-creating PR verdicts. Trusted PR-derived issue bodies
       carry an audit-only `ai:unblock-provenance:v1` marker.
  followup --context-file PATH --fixup N
      The reset to run once the fix-up issue N of a `descope` or
      `operator_step` verdict has merged (Q11).

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
import json
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


def _security_reissue_metadata(body: str, labels: list[str], item: int) -> tuple[int | None, str | None, bool]:
	try:
		dependency = security_dependency_number({"number": item, "body": body, "labels": labels})
	except ValueError:
		return None, None, True
	branches = [match.group(1).strip() for match in INTEGRATION_BRANCH_LINE_RE.finditer(body)]
	if not branches:
		branches = [(match.group(1) or match.group(2)).strip() for match in TARGET_BRANCH_LINE_RE.finditer(body)]
	if len(set(branches)) > 1 or any(
		not SECURITY_BRANCH_REF_RE.fullmatch(branch) or ".." in branch or "//" in branch
		or branch.endswith(("/", ".", ".lock")) for branch in branches
	):
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
	security_source_body = raw.get("security_source_body")
	if kind != "issue" or SECURITY_LABEL not in labels or not isinstance(security_source_body, str) or len(security_source_body) > 65536:
		security_source_body = None
	security_depends_on, security_target_branch, security_metadata_unsafe = (
		_security_reissue_metadata(security_source_body, labels, item)
		if security_source_body is not None else (None, None, False)
	)
	if security_source_body is not None and SECURITY_FINDING_MARKER_PREFIX in security_source_body and finding_id is None:
		security_metadata_unsafe = True
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
	}


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
		{"op": "telegram", "level": "WARNING", "text": f"Unblock judge closed untrusted PR #{item} instead of acting on verdict {verdict_name}; no issue was created."},
	]


def _stop_label(stop: str) -> str:
	return f"ai:{stop}" if stop and stop != "project-failed" else ""


def _is_security_issue(ctx: dict) -> bool:
	return ctx["kind"] == "issue" and SECURITY_LABEL in ctx["labels"]


def _approve(issue: int, labels: list[str], drop: str) -> list[dict]:
	ops: list[dict] = []
	if drop:
		ops.append({"op": "remove_label", "issue": issue, "label": drop})
	ops.append({"op": "add_labels", "issue": issue, "labels": ["ai:awaiting-approval"]})
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
		ops = []
		if stop in ("needs-human", "blocked") and label in ctx["labels"]:
			ops.append({"op": "remove_label", "issue": item, "label": label})
		ops.append({"op": "comment", "issue": item, "body": "/judge_resume --reset-recovery"})
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
		ops = [{"op": "remove_label", "issue": item, "label": present}] if present else []
		return ops + [{"op": "comment", "issue": item, "body": "/reclarify"}]
	if stop == "blocked":
		if ctx["has_plan"]:
			return _approve(item, ctx["labels"], present)
		return [{"op": "comment", "issue": item, "body": f"/answer {text}".rstrip()}]
	if stop == "needs-human":
		if ctx["has_plan"]:
			return _approve(item, ctx["labels"], present)
		ops = [{"op": "remove_label", "issue": item, "label": present}] if present else []
		return ops + [{"op": "comment", "issue": item, "body": "/reclarify"}]
	if stop in CLARIFY_STOPS:
		ops = [{"op": "remove_label", "issue": item, "label": present}] if present else []
		return ops + [{"op": "comment", "issue": item, "body": "/reclarify"}]
	# A side pipeline's dead end on an issue (escalated triage or heal chain,
	# a failed log analysis): clearing the label lets the pipeline that set it
	# try again, with the judge's instructions on record.
	return [{"op": "remove_label", "issue": item, "label": present}] if present else []


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
		lines += [
			"",
			f"Keep the new behaviour off until a person finishes the operator step: gate it behind `{verdict['placeholder']}`"
			" (a feature flag that defaults off, or a placeholder env var that makes the code skip safely).",
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


def plan(verdict: dict, ctx: dict) -> list[dict]:
	name = verdict.get("verdict")
	item = ctx["item"]
	ops: list[dict] = []
	if ctx["kind"] == "pr" and not ctx["pr_trusted"] and name in ("reissue", "descope", "operator_step", "accept_with_followup"):
		return _untrusted_pr_ops(ctx, name)
	if name == "retry_budget":
		ops += [{"op": "comment", "issue": item, "body": f"Next attempt, per the unblock judge: {verdict['instructions']}"}]
		ops += reset_ops(ctx, verdict["instructions"])
	elif name == "auto_answer":
		if "ai:clarification" not in ctx["labels"] and "ai:planning" not in ctx["labels"]:
			if _stop_label(ctx["stop"]) in ctx["labels"]:
				ops.append({"op": "remove_label", "issue": item, "label": _stop_label(ctx["stop"])})
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
	elif name == "descope":
		ops += _fixup_ops(ctx, verdict, "descope")
	elif name == "operator_step":
		ops += _fixup_ops(ctx, verdict, "operator step")
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
		if security_issue and not ctx["tracking"] and ctx.get("security_metadata_unsafe"):
			return [
				{"op": "comment", "issue": item, "body": "This security finding stays open: its finding marker, dependency or target-branch metadata could not be carried to a replacement safely, so no re-issue was created."},
				{"op": "telegram", "level": "WARNING", "text": f"Unblock judge could not safely re-issue security finding #{item}; its metadata needs correction."},
			]
		if security_issue and not ctx["tracking"] and ctx.get("security_finding_id"):
			body = f"{SECURITY_FINDING_MARKER_PREFIX}{ctx['security_finding_id']} -->\n{body}"
		if security_issue and not ctx["tracking"]:
			if ctx.get("security_target_branch"):
				body += f"\n\n- Integration branch: `{ctx['security_target_branch']}`"
			if ctx.get("security_depends_on") is not None:
				body += f"\n- Depends on: #{ctx['security_depends_on']}"
		labels = [SECURITY_LABEL] if security_issue else []
		if ctx["kind"] == "pr":
			# PR body lineage is author-controlled; never route its reissue
			# into an unverified project or reapprove an issue the close event closes.
			ops.append({"op": "create_issue", "title": title, "body": body + "\n\n" + _provenance_line(ctx), "labels": [], "wait_on": None})
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
	return ops


def build_parser() -> argparse.ArgumentParser:
	parser = _Parser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
	plan_cmd = sub.add_parser("plan")
	plan_cmd.add_argument("--verdict-file", required=True)
	plan_cmd.add_argument("--context-file", required=True)
	followup_cmd = sub.add_parser("followup")
	followup_cmd.add_argument("--context-file", required=True)
	followup_cmd.add_argument("--fixup", required=True, type=int)
	return parser


def run(argv: list[str] | None = None) -> dict:
	args = build_parser().parse_args(argv)
	ctx = _context(_read_json(args.context_file, "--context-file"))
	if args.command == "followup":
		if args.fixup < 1:
			raise UsageError("--fixup must be an issue number")
		return {"ops": reset_ops(ctx, f"fix-up #{args.fixup} merged")}
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

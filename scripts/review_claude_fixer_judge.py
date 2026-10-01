#!/usr/bin/env python3
"""Deterministic parts of the Claude-fixer GPT judge (review_autofix.yml).

When the Claude session that owns a `claude/*` PR rejects every finding of a
review round, it dispatches the review workflow with
``claude_fixer_judge_head=<sha>``. The gate verifies the hand-off, the step
"Prepare Claude-fixer judge" (scripts/review_autofix_step_claude_fixer_judge.sh)
collects the inputs, and scripts/review_rb_judge.sh runs the review-blocked
judge in its Claude mode on ``WORKFLOW_EDITOR_MODEL``. The model only rules;
everything that decides what happens to the PR lives here so it can be tested:

``findings --ledger FILE``
	Prints the ledger's findings as a JSON list, numbered ``F1``..``Fn`` in
	document order: every ``- `` bullet (with its indented continuation lines)
	inside a ``CONSENSUS FINDINGS``, ``CONSENSUS TASK GAPS`` or
	``FINDINGS FROM <reviewer>`` block, the same entries the hand-off step
	counts. Each entry: ``{"id", "block", "file", "line", "claim", "text"}``.

``decide --model-json FILE --findings FILE --fix-count N --cap N --out FILE``
	Maps the model's per-finding rulings to the decision (plan decision D2):
	no finding upheld -> ``merge``; some upheld and ``fix-count < cap`` ->
	``fix``; at the cap -> ``merge_with_followup``, or ``hold`` when an upheld
	finding is ``security`` or ``data-loss``. A model ``hold`` or
	``close_and_reissue`` (never allowed for ``claude/*`` PRs) is ``hold``. A
	finding the model did not rule on counts as upheld, and a finding it ruled
	on more than once keeps the most cautious ruling (``upheld`` over
	``invalid``, then a ``security`` / ``data-loss`` category); a model output with no
	usable ruling at all is ``error`` (the workflow then labels the PR
	``ai:review-blocked``). Prints the decision; writes the full result JSON.

``sticky --ledger FILE --rulings FILE``
	Moves every ledger finding that matches a prior ``invalid`` ruling (same
	file, start line within +/-3, the rule PR #4596 uses) out of the counted
	blocks into a ``=== NON-BLOCKING FINDINGS ===`` block, tagged
	``[sticky judge ruling run=<id>]``, rewriting the ledger in place. A block
	left empty gets its ``(No ... reported.)`` placeholder back, so a round
	with only such findings is clean. Prints the number moved.

``prior-rulings --comments FILE --repo R --pr N [--default-branch B] [--max 3] --out FILE``
	Reads ``<!-- ai:claude-fixer-judge:v1 head=<sha> round=<r> run=<id> ... -->``
	markers from a PR comments JSON file (the raw REST list the workflow's
	"Collect PR metadata" step already fetched), takes the newest ``--max``
	distinct runs, verifies each through
	scripts/review_claude_fixer_evidence.py (``--require-judge``) and writes
	the rulings of the verified runs, each tagged with its ``run``. The marker
	only names the run; nothing is trusted from the comment itself.

``verdict-body`` / ``followup``
	Render the verdict comment (ending with the
	``ai:claude-fixer-judge:v1`` marker) and the follow-up issue for
	``merge_with_followup``.

API budget (CLAUDE.md §15): only ``prior-rulings`` calls the API, through the
evidence verifier (3 REST reads per run, at most ``--max`` runs, each run id
verified once per call; nothing is cached). Everything else is local.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_claude_fixer_evidence as evidence_helper  # noqa: E402


COUNTED_BLOCK_RE = re.compile(r"^=== (CONSENSUS FINDINGS|CONSENSUS TASK GAPS|FINDINGS FROM .+) ===$")
END_BLOCK_RE = re.compile(r"^=== END ")
LOCATION_RE = re.compile(r"^(?P<file>[^\s:|]+?)(?::(?P<start>[0-9]+)(?:-(?P<end>[0-9]+))?)?$")
JUDGE_MARKER_RE = re.compile(
	r"^<!-- ai:claude-fixer-judge:v1 head=(?P<head>[0-9a-f]{40}) round=(?P<round>[0-9]+) run=(?P<run>[0-9]+) decision=(?P<decision>[a-z_]+) -->$"
)
NON_BLOCKING_HEADER = "=== NON-BLOCKING FINDINGS ==="
NON_BLOCKING_FOOTER = "=== END NON-BLOCKING FINDINGS ==="
STICKY_LINE_WINDOW = 3
RULINGS = ("invalid", "upheld")
CATEGORIES = ("security", "data-loss", "correctness", "other")
HOLD_CATEGORIES = ("security", "data-loss")
# Comment authors whose judge markers prior_rulings follows (the same set the rejection comment needs).
TRUSTED_COMMENT_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
MODEL_ACTIONS = ("merge", "fix", "merge_with_followup", "hold", "close_and_reissue")
DECISIONS = ("merge", "fix", "merge_with_followup", "hold")


def _placeholder(header: str) -> str:
	return "(No task gaps reported.)" if header == "=== CONSENSUS TASK GAPS ===" else "(No findings reported.)"


def _parse_location(header: str) -> tuple[str, int]:
	location = header.split(" | ", 1)[0].strip()
	match = LOCATION_RE.match(location)
	if not match or "/" not in location and "." not in location:
		return "", 0
	return match.group("file"), int(match.group("start") or 0)


def _bullets(lines: list[str]) -> list[dict[str, Any]]:
	"""Every counted bullet as {block, start, stop} line spans (stop exclusive)."""
	spans: list[dict[str, Any]] = []
	block = ""
	current: dict[str, Any] | None = None
	for index, line in enumerate(lines):
		if COUNTED_BLOCK_RE.match(line):
			block = line
			continue
		if END_BLOCK_RE.match(line) or line.startswith("=== "):
			if current is not None:
				current["stop"] = index
				current = None
			block = ""
			continue
		if not block:
			continue
		if line.startswith("- "):
			if current is not None:
				current["stop"] = index
			current = {"block": block, "start": index, "stop": index + 1}
			spans.append(current)
		elif current is not None and (line.startswith((" ", "\t")) and line.strip()):
			current["stop"] = index + 1
		elif current is not None and not line.strip():
			continue
		elif current is not None:
			current = None
	return spans


def parse_findings(ledger_text: str) -> list[dict[str, Any]]:
	lines = ledger_text.splitlines()
	findings: list[dict[str, Any]] = []
	for number, span in enumerate(_bullets(lines), 1):
		body = lines[span["start"]:span["stop"]]
		header = body[0][2:].strip()
		file_name, line = _parse_location(header)
		claim = header
		for extra in body[1:]:
			stripped = extra.strip()
			if stripped.startswith("PROBLEM:"):
				claim = stripped[len("PROBLEM:"):].strip() or header
				break
		findings.append({
			"id": f"F{number}",
			"block": span["block"].strip("= ").strip(),
			"file": file_name,
			"line": line,
			"claim": claim[:500],
			"text": "\n".join(body)[:4000],
		})
	return findings


def _one_line(value: Any) -> str:
	"""Model text on one line, so it can never add a marker or a checklist line to a comment."""
	return " ".join(str(value or "").split())


def _load_json(path: str) -> Any:
	try:
		return json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None


def _ruling_caution(entry: dict[str, Any]) -> tuple[bool, bool]:
	"""Orders duplicate rulings for one finding: upheld beats invalid, then a hold category wins."""
	upheld = str(entry.get("ruling") or "").strip().lower() == "upheld"
	return upheld, upheld and str(entry.get("category") or "").strip().lower() in HOLD_CATEGORIES


def decide(model: Any, findings: list[dict[str, Any]], fix_count: int, cap: int) -> dict[str, Any]:
	model = model if isinstance(model, dict) else {}
	# Normalised like ruling and category, so "Hold" or "CLOSE_AND_REISSUE" still holds.
	model_action = _one_line(model.get("action")).lower()
	raw_rulings = model.get("rulings") if isinstance(model.get("rulings"), list) else []
	by_id: dict[str, dict[str, Any]] = {}
	for entry in raw_rulings:
		if not isinstance(entry, dict):
			continue
		finding_id = str(entry.get("finding") or entry.get("id") or "").strip()
		ruling = str(entry.get("ruling") or "").strip().lower()
		if not finding_id or ruling not in RULINGS:
			continue
		# A repeated ruling never clears a finding or skips the hold gate:
		# the most cautious one for the finding is kept.
		if finding_id not in by_id or _ruling_caution(entry) > _ruling_caution(by_id[finding_id]):
			by_id[finding_id] = entry
	rulings: list[dict[str, Any]] = []
	for finding in findings:
		entry = by_id.get(finding["id"])
		category = str((entry or {}).get("category") or "").strip().lower()
		if category not in CATEGORIES:
			category = "other"
		rulings.append({
			"finding": finding["id"],
			"file": finding["file"],
			"line": finding["line"],
			"claim": finding["claim"],
			"ruling": str(entry["ruling"]).strip().lower() if entry else "upheld",
			"category": category,
			"reason": (_one_line((entry or {}).get("reason")) or "no ruling returned; counted as upheld")[:600],
		})
	upheld = [ruling for ruling in rulings if ruling["ruling"] == "upheld"]
	result: dict[str, Any] = {
		"model_action": model_action,
		"fix_count": fix_count,
		"cap": cap,
		"rulings": rulings,
		"upheld": upheld,
		"justification": _one_line(model.get("justification"))[:2000],
		# Goes into the fix prompt and the [judge-fix] commit body: one line, so the
		# model cannot add a trailer or prompt-section line. The jq step that builds
		# the writer's description appends the upheld list on its own lines after it.
		"fix_description": _one_line(model.get("fix_description"))[:4000],
	}
	# A model hold (or close_and_reissue, which is not allowed here) stays a hold even without
	# rulings: the plan maps both to `hold`, and every unruled finding already counts as upheld.
	if model_action in ("hold", "close_and_reissue"):
		result.update(decision="hold", reason=f"model_{model_action}")
	elif findings and not by_id:
		result.update(decision="error", reason="no_usable_rulings")
	elif not upheld:
		result.update(decision="merge", reason="all_invalid")
	elif fix_count < cap:
		result.update(decision="fix", reason="upheld_below_cap")
	elif any(ruling["category"] in HOLD_CATEGORIES for ruling in upheld):
		result.update(decision="hold", reason="upheld_security_or_data_loss_at_cap")
	else:
		result.update(decision="merge_with_followup", reason="upheld_at_cap")
	return result


def sticky(ledger_text: str, rulings: list[dict[str, Any]]) -> tuple[str, int]:
	invalid = [
		ruling for ruling in rulings
		if isinstance(ruling, dict) and ruling.get("ruling") == "invalid" and ruling.get("file") and int(ruling.get("line") or 0) > 0
	]
	lines = ledger_text.splitlines()
	spans = _bullets(lines)
	moved: list[tuple[dict[str, Any], str]] = []
	for span in spans:
		file_name, line = _parse_location(lines[span["start"]][2:].strip())
		if not file_name or line <= 0:
			continue
		for ruling in invalid:
			if ruling["file"] == file_name and abs(int(ruling["line"]) - line) <= STICKY_LINE_WINDOW:
				moved.append((span, str(ruling.get("run") or "unknown")))
				break
	if not moved:
		return ledger_text, 0
	drop = {index for span, _run in moved for index in range(span["start"], span["stop"])}
	kept: list[str] = []
	block_header = ""
	block_has_content = False
	for index, line in enumerate(lines):
		if COUNTED_BLOCK_RE.match(line):
			block_header, block_has_content = line, False
			kept.append(line)
			continue
		if block_header and END_BLOCK_RE.match(line):
			if not block_has_content:
				kept.append(_placeholder(block_header))
			block_header = ""
			kept.append(line)
			continue
		if index in drop:
			continue
		if block_header and line.strip():
			block_has_content = True
		kept.append(line)
	kept.append("")
	kept.append(NON_BLOCKING_HEADER)
	# PR #4596 adds the same NON-BLOCKING FINDINGS block for rejected
	# single-reviewer findings; until it merges this is the minimal
	# equivalent (the block is not counted as a finding by the hand-off).
	for span, run_id in moved:
		body = lines[span["start"]:span["stop"]]
		kept.append(f"{body[0]} [sticky judge ruling run={run_id}]")
		kept.extend(body[1:])
	kept.append(NON_BLOCKING_FOOTER)
	return "\n".join(kept) + "\n", len(moved)


def prior_rulings(
	comments: Any,
	*,
	repo: str,
	pr: int,
	default_branch: str,
	max_runs: int,
	verify: Any = evidence_helper.verify_evidence,
) -> dict[str, Any]:
	markers: list[tuple[int, str, str]] = []
	for comment in comments if isinstance(comments, list) else []:
		# Only collaborators' comments can point at judge runs (as for the rejection comment), so a
		# stranger's fake markers cannot use up the newest-runs budget and switch sticky rulings off.
		if not isinstance(comment, dict) or comment.get("author_association") not in TRUSTED_COMMENT_ASSOCIATIONS:
			continue
		try:
			comment_id = int(comment.get("id") or 0)
		except (TypeError, ValueError):
			continue
		for line in str(comment.get("body") or "").splitlines():
			match = JUDGE_MARKER_RE.match(line.rstrip("\r"))
			if match:
				markers.append((comment_id, match.group("run"), match.group("head")))
	markers.sort(reverse=True)
	seen: set[str] = set()
	rulings: list[dict[str, Any]] = []
	runs: list[dict[str, Any]] = []
	for _comment_id, run_id, head in markers:
		if run_id in seen or len(seen) >= max_runs:
			continue
		seen.add(run_id)
		result = verify(repo=repo, pr=pr, head_sha=head, run_id=run_id, default_branch=default_branch, require_judge=True)
		runs.append({"run": run_id, "verified": bool(result.get("verified")), "reason": result.get("reason")})
		if not result.get("verified"):
			continue
		for ruling in result["evidence"]["judge"]["rulings"]:
			if isinstance(ruling, dict):
				rulings.append({**ruling, "run": run_id})
	return {"rulings": rulings, "runs": runs}


def verdict_body(result: dict[str, Any], *, head: str, round_number: int, run_id: str, run_url: str, note: str = "") -> str:
	decision = result["decision"]
	lines = [
		f"## Review round {round_number}: GPT judge verdict — {decision.replace('_', ' ')}",
		"",
		f"The Claude session rejected every finding on head `{head}`, so the review workflow's judge ruled on each one ([judge run]({run_url})).",
		"",
		"| Finding | Location | Ruling | Category | Reason |",
		"| --- | --- | --- | --- | --- |",
	]
	for ruling in result["rulings"]:
		location = f"{ruling['file']}:{ruling['line']}" if ruling["file"] else "(no location)"
		reason = _one_line(ruling["reason"]).replace("|", "\\|")
		lines.append(f"| {ruling['finding']} | `{location}` | {ruling['ruling']} | {ruling['category']} | {reason} |")
	lines.append("")
	lines.append({
		"merge": "No finding was upheld, so nothing is left to fix on this head. Whether the PR merges on this run depends on its checks (below).",
		"fix": f"Upheld findings are fixed by the judge in a `[judge-fix]` commit (judge fix {result['fix_count'] + 1} of {result['cap']}); the push starts a normal review round.",
		"merge_with_followup": "The judge-fix cap is reached, so the upheld findings are not fixed on this PR. Whether it merges on this run, with a follow-up issue for them, depends on its checks (below).",
		"hold": "The PR is held for a human (`ai:needs-human`).",
	}.get(decision, ""))
	if note:
		lines.extend(["", note])
	if result.get("justification"):
		lines.extend(["", f"Judge justification: {result['justification']}"])
	lines.extend(["", f"<!-- ai:claude-fixer-judge:v1 head={head} round={round_number} run={run_id} decision={decision} -->"])
	return "\n".join(lines) + "\n"


def followup(result: dict[str, Any], *, pr: int, head: str, run_url: str) -> dict[str, str]:
	title = f"Follow-up to PR #{pr}: {len(result['upheld'])} upheld review finding(s) left open at the judge-fix cap"
	body = [
		f"The Claude-fixer GPT judge reached its judge-fix cap ({result['cap']}) on PR #{pr} and set it to merge at head `{head}` once that head's checks are green ([judge run]({run_url})).",
		"These findings were upheld and are not fixed yet:",
		"",
	]
	for ruling in result["upheld"]:
		location = f"{ruling['file']}:{ruling['line']}" if ruling["file"] else "(no location)"
		body.append(f"- [ ] `{location}` ({ruling['category']}): {_one_line(ruling['claim'])} — {_one_line(ruling['reason'])}")
	body.extend(["", f"Refs #{pr}"])
	return {"title": title[:250], "body": "\n".join(body) + "\n"}


def _cmd_findings(args: argparse.Namespace) -> int:
	print(json.dumps(parse_findings(Path(args.ledger).read_text(encoding="utf-8"))))
	return 0


def _cmd_decide(args: argparse.Namespace) -> int:
	findings = _load_json(args.findings)
	if not isinstance(findings, list):
		print("::error::review_claude_fixer_judge decide: unreadable findings file", file=sys.stderr)
		return 2
	result = decide(_load_json(args.model_json), findings, args.fix_count, args.cap)
	Path(args.out).write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
	print(result["decision"])
	return 0


def _cmd_sticky(args: argparse.Namespace) -> int:
	loaded = _load_json(args.rulings)
	rulings = loaded.get("rulings") if isinstance(loaded, dict) else loaded
	ledger = Path(args.ledger)
	text, moved = sticky(ledger.read_text(encoding="utf-8"), rulings if isinstance(rulings, list) else [])
	if moved:
		ledger.write_text(text, encoding="utf-8")
	print(moved)
	return 0


def _cmd_prior_rulings(args: argparse.Namespace) -> int:
	try:
		result = prior_rulings(
			_load_json(args.comments),
			repo=args.repo,
			pr=args.pr,
			default_branch=args.default_branch or "",
			max_runs=args.max,
		)
	except Exception as exc:  # noqa: BLE001 - a failed load keeps every finding counted
		result = {"rulings": [], "runs": [], "error": type(exc).__name__}
	Path(args.out).write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
	print(len(result["rulings"]))
	return 0


def _cmd_verdict_body(args: argparse.Namespace) -> int:
	result = _load_json(args.decision)
	if not isinstance(result, dict):
		return 2
	result = {**result, "decision": args.final_decision or result["decision"]}
	sys.stdout.write(verdict_body(result, head=args.head, round_number=args.round, run_id=args.run_id, run_url=args.run_url, note=args.note or ""))
	return 0


def _cmd_followup(args: argparse.Namespace) -> int:
	result = _load_json(args.decision)
	if not isinstance(result, dict):
		return 2
	print(json.dumps(followup(result, pr=args.pr, head=args.head, run_url=args.run_url)))
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
	sub = parser.add_subparsers(dest="command", required=True)
	cmd = sub.add_parser("findings")
	cmd.add_argument("--ledger", required=True)
	cmd = sub.add_parser("decide")
	cmd.add_argument("--model-json", required=True)
	cmd.add_argument("--findings", required=True)
	cmd.add_argument("--fix-count", type=int, required=True)
	cmd.add_argument("--cap", type=int, required=True)
	cmd.add_argument("--out", required=True)
	cmd = sub.add_parser("sticky")
	cmd.add_argument("--ledger", required=True)
	cmd.add_argument("--rulings", required=True)
	cmd = sub.add_parser("prior-rulings")
	cmd.add_argument("--comments", required=True)
	cmd.add_argument("--repo", required=True)
	cmd.add_argument("--pr", type=int, required=True)
	cmd.add_argument("--default-branch", default="")
	cmd.add_argument("--max", type=int, default=3)
	cmd.add_argument("--out", required=True)
	cmd = sub.add_parser("verdict-body")
	cmd.add_argument("--decision", required=True)
	cmd.add_argument("--final-decision", choices=DECISIONS, default="")
	cmd.add_argument("--head", required=True)
	cmd.add_argument("--round", type=int, required=True)
	cmd.add_argument("--run-id", required=True)
	cmd.add_argument("--run-url", required=True)
	cmd.add_argument("--note", default="")
	cmd = sub.add_parser("followup")
	cmd.add_argument("--decision", required=True)
	cmd.add_argument("--pr", type=int, required=True)
	cmd.add_argument("--head", required=True)
	cmd.add_argument("--run-url", required=True)
	args = parser.parse_args(argv)
	handlers = {
		"findings": _cmd_findings,
		"decide": _cmd_decide,
		"sticky": _cmd_sticky,
		"prior-rulings": _cmd_prior_rulings,
		"verdict-body": _cmd_verdict_body,
		"followup": _cmd_followup,
	}
	return handlers[args.command](args)


if __name__ == "__main__":
	raise SystemExit(main())

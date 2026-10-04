#!/usr/bin/env python3
"""Auto-decisions for standalone clarify (plan Phase 8b, port P3).

When `clarify.yml` posts questions on a standalone issue, it answers them at
once with each question's RECOMMENDED option (through the orchestrator's
existing poster, `scripts/orchestrate_parse_and_post_answer.sh`, which keeps
its loop guard) and records every pick in one comment that starts with
`<!-- ai:auto-decisions:v1 -->`. The comment is updated in place on later
clarify cycles, and `implement.yml` repeats it in the PR body.

Subcommands (JSON on stdout unless noted; exit 0 ok, 1 bad input, 2 an
unreadable file):

  parse --questions-file <path>
      The decisions in a clarification-questions text: for each `Q<n>` with at
      least one `(RECOMMENDED)` option, the question, the pick (letters joined
      with `+`), the picked option's text (used as the why), and the other
      options. Also `answers`, the `Q1: A` lines the answer poster takes, and
      `undecided`, the Q-IDs without a RECOMMENDED option.
  render --comments-file <path> --decisions-file <path> --source <text>
      The body of the auto-decisions comment with the new entries appended
      (`AD-<n>`, continuing the existing numbering), and the id of the
      existing trusted comment to update in place, or null to create one.
  pr-section --comments-file <path>
      Markdown for the PR body (plain text, not JSON), or nothing when the
      issue has no trusted auto-decisions comment. `#<digits>` is broken up so
      the section can never reference, or auto-close, an issue.

The option grammar matches the orchestrator's RECOMMENDED parsers
(`extract_recommended_answers` in scripts/orchestrate_poll_process.sh and the
plan.yml auto-answer): an optional blockquote and bullet, optional bold
around the letter, and any of `—`, `–`, `-`, `)`, `.`, `:` after it.

No GitHub API calls (CLAUDE.md §15): callers pass comments they fetched.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MARKER = "<!-- ai:auto-decisions:v1 -->"
HEADING = "## Auto-decisions"
INTRO = (
	"The pipeline answered these clarification questions itself with each question's RECOMMENDED "
	"option (`STANDALONE_AUTO_DECIDE_ENABLED`). To revisit them, post `/reclarify` and answer with `/answer`."
)
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
MAX_FIELD = 400
MAX_ENTRIES = 200

QUESTION_RE = re.compile(r"^\s*(?:>\s*)*\*?\*?Q(\d+)\s*[:.)]?\s*\*?\*?\s*(.*?)\s*\*?\*?\s*$", re.IGNORECASE)
OPTION_RE = re.compile(
	r"^\s*(?:>\s*)*(?:[-*]\s*)?(?:\*\*)?([A-Za-z](?:\+[A-Za-z])*)(?:\*\*)?\s*(?:—|–|[-)\.:])\s*(.*?)\s*$"
)
RECOMMENDED_RE = re.compile(r"\(RECOMMENDED\)", re.IGNORECASE)
ENTRY_RE = re.compile(r"^- \*\*AD-([1-9][0-9]*)\*\* ")
ISSUE_REF_RE = re.compile(r"#(\d)")


class UsageError(Exception):
	"""Bad input: exit 1."""


class InputError(Exception):
	"""An unreadable file: exit 2."""


class _Parser(argparse.ArgumentParser):
	def error(self, message: str) -> None:
		raise UsageError(message)


def _read_text(path: str, flag: str) -> str:
	try:
		return Path(path).read_text(encoding="utf-8")
	except (OSError, UnicodeDecodeError) as exc:
		raise InputError(f"cannot read {flag} {path}: {exc}") from exc


def _read_json(path: str, flag: str) -> object:
	try:
		return json.loads(_read_text(path, flag))
	except ValueError as exc:
		raise InputError(f"{flag} {path} is not JSON: {exc}") from exc


def _clean(text: str) -> str:
	"""One line, no markdown emphasis or HTML comment delimiters, bounded."""
	text = " ".join(text.replace("**", "").split())
	text = text.replace("<", "&lt;").replace(">", "&gt;")
	return text[:MAX_FIELD]


def parse_questions(text: str) -> dict:
	questions: dict[str, dict] = {}
	order: list[str] = []
	current = None
	for line in text.splitlines():
		question = QUESTION_RE.match(line)
		if question and not OPTION_RE.match(line):
			qid = f"Q{question.group(1)}"
			if qid not in questions:
				questions[qid] = {"qid": qid, "question": _clean(question.group(2)), "options": []}
				order.append(qid)
			current = questions[qid]
			continue
		if current is None:
			continue
		option = OPTION_RE.match(line)
		if not option:
			continue
		text_part = option.group(2)
		recommended = bool(RECOMMENDED_RE.search(text_part))
		current["options"].append(
			{
				"letter": option.group(1).upper(),
				"text": _clean(RECOMMENDED_RE.sub("", text_part)),
				"recommended": recommended,
			}
		)
	decisions = []
	undecided = []
	for qid in order:
		options = questions[qid]["options"]
		picked = [option for option in options if option["recommended"]]
		if not picked:
			undecided.append(qid)
			continue
		decisions.append(
			{
				"qid": qid,
				"question": questions[qid]["question"],
				"pick": "+".join(option["letter"] for option in picked),
				"why": "; ".join(option["text"] for option in picked if option["text"]),
				"alternatives": [
					{"letter": option["letter"], "text": option["text"]}
					for option in options
					if not option["recommended"]
				],
			}
		)
	answers = "".join(f"{decision['qid']}: {decision['pick']}\n" for decision in decisions)
	return {"decisions": decisions, "answers": answers, "undecided": undecided}


def _trusted(comment: dict) -> bool:
	user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
	login = str(user.get("login") or "")
	return login.endswith("[bot]") or comment.get("author_association") in TRUSTED_ASSOCIATIONS


def find_comment(comments: object) -> dict | None:
	"""The latest trusted comment whose first line is the marker."""
	if not isinstance(comments, list):
		raise UsageError("comments must be a JSON array")
	found = None
	for comment in comments:
		if not isinstance(comment, dict) or not _trusted(comment):
			continue
		body = str(comment.get("body") or "")
		if body.split("\n", 1)[0].strip() != MARKER:
			continue
		if found is None or (str(comment.get("created_at") or ""), int(comment.get("id") or 0)) >= (
			str(found.get("created_at") or ""),
			int(found.get("id") or 0),
		):
			found = comment
	return found


def _entry_lines(body: str) -> list[str]:
	return [line for line in body.splitlines() if ENTRY_RE.match(line)]


def _format_entry(number: int, decision: dict, source: str) -> str:
	alternatives = ", ".join(
		f"{alt['letter']} ({alt['text']})" if alt.get("text") else alt["letter"] for alt in decision.get("alternatives", [])
	)
	parts = [
		f"- **AD-{number}** ({_clean(source)}, {decision['qid']}) {decision['question'] or '(question text not parsed)'}",
		f"→ **{decision['pick']}**" + (f": {decision['why']}" if decision.get("why") else ""),
	]
	if alternatives:
		parts.append(f"Alternatives: {_clean(alternatives)}.")
	return " ".join(parts)


def render(comments: object, decisions: object, source: str) -> dict:
	if not isinstance(decisions, dict) or not isinstance(decisions.get("decisions"), list):
		raise UsageError("--decisions-file must hold the output of `parse`")
	existing = find_comment(comments)
	lines = _entry_lines(str(existing.get("body") or "")) if existing else []
	numbers = [int(ENTRY_RE.match(line).group(1)) for line in lines]
	next_number = max(numbers, default=0) + 1
	new_lines = []
	for decision in decisions["decisions"]:
		if not isinstance(decision, dict) or not {"qid", "question", "pick"} <= set(decision):
			raise UsageError("malformed decision")
		new_lines.append(_format_entry(next_number, decision, source))
		next_number += 1
	entries = (lines + new_lines)[-MAX_ENTRIES:]
	body = "\n".join([MARKER, HEADING, "", INTRO, "", *entries]) + "\n"
	return {
		"comment_id": existing.get("id") if existing else None,
		"body": body,
		"added": len(new_lines),
		"total": len(entries),
	}


def pr_section(comments: object) -> str:
	existing = find_comment(comments)
	if not existing:
		return ""
	lines = _entry_lines(str(existing.get("body") or ""))
	if not lines:
		return ""
	safe = [ISSUE_REF_RE.sub("#⁠\\1", line) for line in lines]
	return "\n".join(["", HEADING, "", "Decisions the pipeline took from the issue's clarification questions:", "", *safe]) + "\n"


def build_parser() -> argparse.ArgumentParser:
	parser = _Parser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
	parse_cmd = sub.add_parser("parse")
	parse_cmd.add_argument("--questions-file", required=True)
	render_cmd = sub.add_parser("render")
	render_cmd.add_argument("--comments-file", required=True)
	render_cmd.add_argument("--decisions-file", required=True)
	render_cmd.add_argument("--source", required=True)
	pr_cmd = sub.add_parser("pr-section")
	pr_cmd.add_argument("--comments-file", required=True)
	return parser


def main(argv: list[str] | None = None) -> int:
	try:
		args = build_parser().parse_args(argv)
		if args.command == "parse":
			print(json.dumps(parse_questions(_read_text(args.questions_file, "--questions-file"))))
		elif args.command == "render":
			print(
				json.dumps(
					render(
						_read_json(args.comments_file, "--comments-file"),
						_read_json(args.decisions_file, "--decisions-file"),
						args.source,
					)
				)
			)
		else:
			sys.stdout.write(pr_section(_read_json(args.comments_file, "--comments-file")))
	except UsageError as exc:
		print(json.dumps({"error": str(exc)}))
		return 1
	except InputError as exc:
		print(json.dumps({"error": str(exc)}))
		return 2
	return 0


if __name__ == "__main__":
	sys.exit(main())

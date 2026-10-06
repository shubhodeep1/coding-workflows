#!/usr/bin/env python3
"""Auto-decisions for standalone clarify (plan Phase 8b, port P3).

When `clarify.yml` posts questions on a standalone issue, the Claude
clarify-respond worker (`orchestrate_clarify_respond.yml`) answers them from the
repository, falling back to each question's RECOMMENDED option when it cannot
run (with `STANDALONE_CLARIFY_RESPOND_ENABLED=false`, `clarify.yml` posts the
RECOMMENDED options itself). Answers go through the orchestrator's existing
poster, `scripts/orchestrate_parse_and_post_answer.sh`, which keeps its loop
guard, and every pick is recorded in one comment that starts with
`<!-- ai:auto-decisions:v1 -->`, together with any "Setup required" items (a
placeholder secret, variable or account an operator must provision). The
comment is updated in place on later clarify cycles, and `implement.yml`
repeats it in the PR body.

Subcommands (JSON on stdout unless noted; exit 0 ok, 1 bad input, 2 an
unreadable file):

  parse --questions-file <path>
      The decisions in a clarification-questions text: for each `Q<n>` with at
      least one `(RECOMMENDED)` option, the question, the pick (letters joined
      with `+`), the picked option's text (used as the why), and the other
      options. Also `answers`, the `Q1: A` lines the answer poster takes, and
      `undecided`, the Q-IDs without a RECOMMENDED option.
  from-answers --questions-file <path> --answers-file <path>
      The same shape as `parse`, built from a clarify-respond answer instead
      of the RECOMMENDED markers: the pick from each `Q<n>: <decision>` line
      (a letter combination or a strategy such as DERIVE_FROM_REPO), the why
      from the matching RATIONALE line, and `setup`, the bullets of an
      optional `SETUP REQUIRED:` section. `undecided` lists the questions
      the answer does not decide.
  complete --questions-file <path> --answers-file <path>
      Check that every question has a permitted decision. Keep a complete
      answer unchanged, fill missing or invalid picks from RECOMMENDED options,
      or block when no safe fallback exists.
  render --comments-file <path> --decisions-file <path> --source <text>
      The body of the auto-decisions comment with the new entries appended
      (`AD-<n>`, continuing the existing numbering) and new setup items
      appended (`SETUP-<n>`, skipping ones already listed), and the id of the
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
	"The pipeline answered these clarification questions itself (`STANDALONE_AUTO_DECIDE_ENABLED`): the "
	"clarify-respond worker decides each one from the repository, and each question's RECOMMENDED option is "
	"used when the worker cannot run. To revisit them, post `/reclarify` and answer with `/answer`."
)
SETUP_HEADING = "### Setup required"
SETUP_INTRO = (
	"Placeholders the decisions rely on. The work proceeds without them; each item says what an operator "
	"provisions and what the code does until then."
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
SETUP_RE = re.compile(r"^- \*\*SETUP-([1-9][0-9]*)\*\* (.*)$")
ANSWER_RE = re.compile(
	r"^\s*\*?\*?(Q[1-9][0-9]*)\*?\*?\s*:\s*\*?\*?([A-Z](?:\+[A-Z])*|DERIVE_FROM_REPO|SYNTHESIZE|REFRAME|ESCALATE)\*?\*?\s*$"
)
DECISION_LINE_RE = re.compile(r"^\s*\*{0,2}(Q[0-9]+)\*{0,2}\s*:\s*(.*?)\s*$", re.IGNORECASE)
RATIONALE_RE = re.compile(r"^\s*\*?\*?(Q[1-9][0-9]*)\*?\*?\s*:\s*(.+?)\s*$")
SECTION_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?([A-Z][A-Z -]*[A-Z])(?:\*\*)?\s*(?:\([^)]*\))?\s*:\s*(?:\*\*)?\s*$")
BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
MAX_SETUP = 50
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


def _question_blocks(text: str) -> tuple[dict[str, dict], list[str]]:
	"""Each `Q<n>` with its cleaned question text and parsed options, in order."""
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
	return questions, order


def parse_questions(text: str) -> dict:
	questions, order = _question_blocks(text)
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


def _answer_sections(text: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
	"""Picks, rationales and setup items from a clarify-respond answer.

	Picks come from the DECISIONS section, or from bare `Q<n>: <decision>`
	lines when the answer has no section headings (the RECOMMENDED fallback).
	The first pick for a Q-ID wins, so a later rationale line that happens to
	be a single letter cannot overwrite it.
	"""
	picks: dict[str, str] = {}
	rationales: dict[str, str] = {}
	setup: list[str] = []
	section = ""
	rationale_qid = ""
	for line in text.splitlines():
		heading = SECTION_RE.match(line)
		if heading:
			section = heading.group(1).strip().upper()
			rationale_qid = ""
			continue
		if section in ("", "DECISIONS"):
			answer = ANSWER_RE.match(line)
			if answer and answer.group(1) not in picks:
				picks[answer.group(1)] = answer.group(2)
		elif section == "RATIONALE":
			rationale = RATIONALE_RE.match(line)
			if rationale:
				rationale_qid = rationale.group(1)
				rationales.setdefault(rationale_qid, rationale.group(2))
			elif rationale_qid and line.strip():
				rationales[rationale_qid] += " " + line.strip()
		elif section == "SETUP REQUIRED":
			bullet = BULLET_RE.match(line)
			if bullet:
				setup.append(_clean(bullet.group(1)))
	return picks, rationales, setup


def from_answers(questions_text: str, answers_text: str) -> dict:
	questions, order = _question_blocks(questions_text)
	picks, rationales, setup = _answer_sections(answers_text)
	decisions = []
	undecided = []
	for qid in order:
		pick = picks.get(qid)
		if not pick:
			undecided.append(qid)
			continue
		options = questions[qid]["options"]
		letters = set(pick.split("+")) if re.fullmatch(r"[A-Z](?:\+[A-Z])*", pick) else set()
		picked_text = "; ".join(option["text"] for option in options if option["letter"] in letters and option["text"])
		decisions.append(
			{
				"qid": qid,
				"question": questions[qid]["question"],
				"pick": pick,
				"why": _clean(rationales.get(qid, "")) or picked_text,
				"alternatives": [
					{"letter": option["letter"], "text": option["text"]}
					for option in options
					if option["letter"] not in letters
				],
			}
		)
	answers = "".join(f"{decision['qid']}: {decision['pick']}\n" for decision in decisions)
	return {"decisions": decisions, "answers": answers, "undecided": undecided, "setup": [item for item in setup if item]}


def _permitted_pick(pick: str, options: list[dict]) -> bool:
	if pick in ("DERIVE_FROM_REPO", "SYNTHESIZE", "REFRAME", "ESCALATE"):
		return True
	if not re.fullmatch(r"[A-Z](?:\+[A-Z])*", pick):
		return False
	letters = pick.split("+")
	listed = {option["letter"] for option in options}
	return len(letters) == len(set(letters)) and (pick in listed or all(letter in listed for letter in letters))


def complete_answers(questions_text: str, answers_text: str) -> dict:
	"""Validate worker decisions before they can become a bot /answer."""
	questions, order = _question_blocks(questions_text)
	picks: dict[str, list[str]] = {}
	section = ""
	has_escalation = False
	for line in answers_text.splitlines():
		heading = SECTION_RE.match(line)
		if heading:
			section = heading.group(1).strip().upper()
			has_escalation |= section == "ESCALATION"
			continue
		if section in ("", "DECISIONS"):
			match = DECISION_LINE_RE.match(line)
			if match:
				strict = ANSWER_RE.match(line)
				picks.setdefault(match.group(1).upper(), []).append(strict.group(2) if strict else match.group(2).strip())

	missing: list[str] = []
	invalid: list[dict[str, str]] = []
	valid: dict[str, str] = {}
	for qid in order:
		values = picks.get(qid, [])
		if not values:
			missing.append(qid)
			continue
		if len(set(values)) != 1:
			invalid.append({"qid": qid, "pick": _clean("; ".join(values))[:40], "reason": "conflicting"})
			continue
		pick = values[0]
		if _permitted_pick(pick, questions[qid]["options"]):
			valid[qid] = pick
		else:
			invalid.append({"qid": qid, "pick": _clean(pick)[:40], "reason": "invalid"})
	extra = [qid for qid in picks if qid not in questions]
	filled = [qid for qid in order if qid in missing or any(row["qid"] == qid for row in invalid)]
	result = {
		"status": "complete", "answers": answers_text, "filled": filled,
		"missing": missing, "invalid": invalid, "extra": extra, "undecided": [],
	}
	if not order:
		result["status"] = "unparseable"
		return result
	if not filled and not extra:
		return result

	# A partial escalation must not silently turn into a fallback decision.
	recommended = parse_questions(questions_text)["decisions"]
	recommended_picks = {row["qid"]: row["pick"] for row in recommended}
	undecided = [
		qid for qid in filled
		if qid not in recommended_picks or not _permitted_pick(recommended_picks[qid], questions[qid]["options"])
	]
	result["undecided"] = undecided
	if undecided or (filled and has_escalation):
		result["status"] = "blocked"
		return result

	canonical = ["DECISIONS:", *(f"{qid}: {valid.get(qid, recommended_picks.get(qid))}" for qid in order)]
	if filled:
		canonical.extend(["", "RATIONALE:"])
		invalid_by_qid = {row["qid"]: row for row in invalid}
		for qid in filled:
			reason = invalid_by_qid.get(qid)
			detail = "missing" if reason is None else "invalid: " + ISSUE_REF_RE.sub('#⁠\\1', reason['pick'])
			canonical.append(
				f"{qid}: RECOMMENDED option {recommended_picks[qid]} used; "
				f"the worker answer gave no permitted decision ({detail})."
			)

	# Remove the old decisions and stale rationale for filled questions; keep
	# all other worker context, including rationale for decisions we retained.
	preserved: list[str] = []
	section = ""
	skip_rationale = False
	for line in answers_text.splitlines():
		heading = SECTION_RE.match(line)
		if heading:
			section = heading.group(1).strip().upper()
			skip_rationale = False
			if section != "DECISIONS":
				preserved.append(line)
			continue
		if section in ("", "DECISIONS") and DECISION_LINE_RE.match(line):
			continue
		if section == "RATIONALE":
			rationale = DECISION_LINE_RE.match(line)
			if rationale:
				skip_rationale = rationale.group(1).upper() in filled
			if skip_rationale:
				continue
		preserved.append(line)
	preserved_text = "\n".join(preserved).strip()
	result["status"] = "filled"
	result["answers"] = "\n".join(canonical).rstrip() + "\n" + ("\n" + preserved_text + "\n" if preserved_text else "")
	return result


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
	if "source" in decision and not isinstance(decision["source"], str):
		raise UsageError("decision source must be a string")
	entry_source = _clean(decision.get("source") or source)
	alternatives = ", ".join(
		f"{alt['letter']} ({alt['text']})" if alt.get("text") else alt["letter"] for alt in decision.get("alternatives", [])
	)
	parts = [
		f"- **AD-{number}** ({entry_source}, {decision['qid']}) {decision['question'] or '(question text not parsed)'}",
		f"→ **{decision['pick']}**" + (f": {decision['why']}" if decision.get("why") else ""),
	]
	if alternatives:
		parts.append(f"Alternatives: {_clean(alternatives)}.")
	return " ".join(parts)


def _setup_lines(body: str) -> list[str]:
	return [line for line in body.splitlines() if SETUP_RE.match(line)]


def render(comments: object, decisions: object, source: str) -> dict:
	if not isinstance(decisions, dict) or not isinstance(decisions.get("decisions"), list):
		raise UsageError("--decisions-file must hold a decisions list from `parse` or `from-answers`")
	setup = decisions.get("setup", [])
	if not isinstance(setup, list) or not all(isinstance(item, str) for item in setup):
		raise UsageError("setup must be a list of strings")
	existing = find_comment(comments)
	existing_body = str(existing.get("body") or "") if existing else ""
	lines = _entry_lines(existing_body)
	numbers = [int(ENTRY_RE.match(line).group(1)) for line in lines]
	next_number = max(numbers, default=0) + 1
	new_lines = []
	for decision in decisions["decisions"]:
		if not isinstance(decision, dict) or not {"qid", "question", "pick"} <= set(decision):
			raise UsageError("malformed decision")
		new_lines.append(_format_entry(next_number, decision, source))
		next_number += 1
	entries = (lines + new_lines)[-MAX_ENTRIES:]
	setup_lines = _setup_lines(existing_body)
	known = {SETUP_RE.match(line).group(2).strip() for line in setup_lines}
	next_setup = max((int(SETUP_RE.match(line).group(1)) for line in setup_lines), default=0) + 1
	for item in setup:
		text = _clean(item)
		if not text or text in known:
			continue
		known.add(text)
		setup_lines.append(f"- **SETUP-{next_setup}** {text}")
		next_setup += 1
	setup_lines = setup_lines[-MAX_SETUP:]
	parts = [MARKER, HEADING, "", INTRO, "", *entries]
	if setup_lines:
		parts += ["", SETUP_HEADING, "", SETUP_INTRO, "", *setup_lines]
	body = "\n".join(parts) + "\n"
	return {
		"comment_id": existing.get("id") if existing else None,
		"body": body,
		"added": len(new_lines),
		"total": len(entries),
		"setup_total": len(setup_lines),
	}


def pr_section(comments: object) -> str:
	existing = find_comment(comments)
	if not existing:
		return ""
	body = str(existing.get("body") or "")
	lines = _entry_lines(body)
	if not lines:
		return ""
	safe = [ISSUE_REF_RE.sub("#⁠\\1", line) for line in lines]
	parts = ["", HEADING, "", "Decisions the pipeline took from the issue's clarification questions:", "", *safe]
	setup_lines = _setup_lines(body)
	if setup_lines:
		parts += ["", SETUP_HEADING, "", SETUP_INTRO, "", *(ISSUE_REF_RE.sub("#⁠\\1", line) for line in setup_lines)]
	return "\n".join(parts) + "\n"


def build_parser() -> argparse.ArgumentParser:
	parser = _Parser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
	parse_cmd = sub.add_parser("parse")
	parse_cmd.add_argument("--questions-file", required=True)
	answers_cmd = sub.add_parser("from-answers")
	answers_cmd.add_argument("--questions-file", required=True)
	answers_cmd.add_argument("--answers-file", required=True)
	complete_cmd = sub.add_parser("complete")
	complete_cmd.add_argument("--questions-file", required=True)
	complete_cmd.add_argument("--answers-file", required=True)
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
		elif args.command == "from-answers":
			print(
				json.dumps(
					from_answers(
						_read_text(args.questions_file, "--questions-file"),
						_read_text(args.answers_file, "--answers-file"),
					)
				)
			)
		elif args.command == "complete":
			print(json.dumps(complete_answers(
				_read_text(args.questions_file, "--questions-file"),
				_read_text(args.answers_file, "--answers-file"),
			)))
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

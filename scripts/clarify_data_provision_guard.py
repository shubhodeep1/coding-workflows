#!/usr/bin/env python3
"""Post-processing guard for orchestrate_clarify_respond.

Detects when the auto-responder selected a lettered option that requires
providing concrete external data (PR URLs, commit SHAs, branch names,
deployment URLs, etc.) that the auto-responder does not have.  When such
an option is detected, the guard overrides the answer with the most
conservative fallback option from the same question.

Exit codes:
  0 — answers are OK or were patched (patched answers on stdout)
  1 — plumbing error (fail-open: caller should use original answers)

Usage:
  python3 scripts/clarify_data_provision_guard.py \
    --clarification-file /path/to/clarification.txt \
    --answers-file /path/to/codex_output.txt
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Patterns that indicate an option requires the respondent to provide
# external data they likely don't have.
_DATA_PROVISION_PATTERNS = [
	re.compile(
		r"(?:provide|supply|share|paste|submit|attach|include|link|require(?:s|d)?|need(?:s|ed)?|must\s+(?:provide|supply|share|paste|submit|attach|include|link))\b"
		r".{0,60}"
		r"(?:URL|PR\b|pull\s+request|SHA|commit|branch|http|deployment|build\s+output|test\s+result|log\b|screenshot)",
		re.IGNORECASE,
	),
	re.compile(
		r"(?:URL|PR\s+link|commit\s+SHA|branch\s+name|deployment\s+URL)\b"
		r".{0,40}"
		r"(?:for|of|from|to|containing|with)",
		re.IGNORECASE,
	),
]

# Patterns that indicate a conservative / safe fallback option.
_FALLBACK_PATTERNS = re.compile(
	r"(?:proceed\s+without|reduced\s+scope|skip\s+this|work\s+with\s+what|"
	r"continue\s+without|fall\s*back|use\s+default|available\s+information|"
	r"best\s+effort|defer|omit|not\s+require)",
	re.IGNORECASE,
)


def _parse_questions(clarification_text: str) -> dict[str, dict[str, str]]:
	"""Parse clarification text into {Q_ID: {letter: option_text}}."""
	questions: dict[str, dict[str, str]] = {}
	current_qid = ""

	for line in clarification_text.splitlines():
		# Detect question header: **Q1: ...** or Q1: ...
		qid_match = re.match(r"\s*\*{0,2}(Q\d+)\s*:", line)
		if qid_match:
			current_qid = qid_match.group(1)
			questions[current_qid] = {}
			continue

		if not current_qid:
			continue

		# Detect option line: - **A** — description  OR  - A — description  OR  - A: description
		opt_match = re.match(
			r"\s*-\s+\*{0,2}([A-Z])\*{0,2}\s*(?:—|--|-|:)\s*(.*)", line
		)
		if opt_match:
			letter = opt_match.group(1)
			text = opt_match.group(2).strip()
			questions[current_qid][letter] = text

	return questions


def _parse_answers(answers_text: str) -> dict[str, list[str]]:
	"""Parse answer text into {Q_ID: [selected_letters]}."""
	answers: dict[str, list[str]] = {}
	in_decisions = False

	for line in answers_text.splitlines():
		stripped = line.strip()
		if stripped.upper().startswith("DECISIONS"):
			in_decisions = True
			continue
		if stripped.upper().startswith("RATIONALE"):
			in_decisions = False
			continue

		if in_decisions or not answers:
			# Match Q1: A or Q1: A+C
			m = re.match(r"(Q\d+):\s*([A-Z](?:\+[A-Z])*)\s*$", stripped)
			if m:
				qid = m.group(1)
				letters = m.group(2).split("+")
				answers[qid] = letters

	return answers


def _option_requires_data(option_text: str) -> bool:
	"""Check if an option's text indicates it requires external data."""
	for pattern in _DATA_PROVISION_PATTERNS:
		if pattern.search(option_text):
			return True
	return False


def _find_fallback(options: dict[str, str], exclude_letters: set[str]) -> str | None:
	"""Find the most conservative fallback option."""
	# First pass: look for options matching fallback patterns
	candidates = []
	for letter, text in sorted(options.items()):
		if letter in exclude_letters:
			continue
		if _FALLBACK_PATTERNS.search(text):
			candidates.append(letter)

	if candidates:
		# Prefer the last candidate (typically the most conservative)
		return candidates[-1]

	# Second pass: pick the last available option that doesn't require data
	for letter in sorted(options.keys(), reverse=True):
		if letter in exclude_letters:
			continue
		if not _option_requires_data(options[letter]):
			return letter

	return None


def run_guard(clarification_file: Path, answers_file: Path) -> str:
	"""Run the data-provision guard and return (possibly patched) answers."""
	clarification_text = clarification_file.read_text(encoding="utf-8", errors="replace")
	answers_text = answers_file.read_text(encoding="utf-8", errors="replace")

	questions = _parse_questions(clarification_text)
	answers = _parse_answers(answers_text)

	if not questions or not answers:
		return answers_text

	overrides: dict[str, str] = {}
	override_reasons: list[str] = []

	for qid, selected_letters in answers.items():
		if qid not in questions:
			continue
		q_options = questions[qid]
		data_requiring_letters: set[str] = set()

		for letter in selected_letters:
			if letter in q_options and _option_requires_data(q_options[letter]):
				data_requiring_letters.add(letter)

		if data_requiring_letters:
			fallback = _find_fallback(q_options, data_requiring_letters)
			if fallback:
				overrides[qid] = fallback
				override_reasons.append(
					f"{qid}: overrode {'+'.join(sorted(data_requiring_letters))} -> {fallback} "
					f"(option requires external data the auto-responder cannot provide)"
				)

	if not overrides:
		return answers_text

	# Patch the answers
	patched_lines = []
	for line in answers_text.splitlines():
		stripped = line.strip()
		matched = False
		for qid, new_letter in overrides.items():
			# Only match decision lines (Q1: A or Q1: A+B), not rationale lines
			# (Q1: The recommended...) — require single-letter or +letter at end.
			if re.match(rf"{re.escape(qid)}:\s*[A-Z](?:\+[A-Z])*\s*$", stripped):
				patched_lines.append(f"{qid}: {new_letter}")
				matched = True
				break
		if not matched:
			patched_lines.append(line)

	# Append override notice to rationale
	patched_lines.append("")
	patched_lines.append("DATA-PROVISION GUARD OVERRIDES:")
	for reason in override_reasons:
		patched_lines.append(f"  {reason}")

	return "\n".join(patched_lines)


def main() -> int:
	parser = argparse.ArgumentParser(description="Clarify data-provision guard")
	parser.add_argument("--clarification-file", required=True, type=Path)
	parser.add_argument("--answers-file", required=True, type=Path)
	args = parser.parse_args()

	if not args.clarification_file.exists() or not args.answers_file.exists():
		print("::warning::Data-provision guard: input files missing; skipping guard.", file=sys.stderr)
		if args.answers_file.exists():
			print(args.answers_file.read_text(encoding="utf-8", errors="replace"))
		return 0

	try:
		result = run_guard(args.clarification_file, args.answers_file)
		print(result)
		return 0
	except Exception as exc:
		print(f"::warning::Data-provision guard failed (fail-open): {exc}", file=sys.stderr)
		print(args.answers_file.read_text(encoding="utf-8", errors="replace"))
		return 0


if __name__ == "__main__":
	raise SystemExit(main())

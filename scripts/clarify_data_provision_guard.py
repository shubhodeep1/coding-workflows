#!/usr/bin/env python3
"""Post-processing guard for orchestrate_clarify_respond.

Detects when the auto-responder selected a lettered option that requires
providing concrete external data (PR URLs, commit SHAs, branch names,
deployment URLs, etc.) that the auto-responder does not have.  When such
an option is detected, the guard selects a safe fallback from the same
question or escalates when none is available.

Exit codes:
  0 — answers are OK or were patched (patched answers on stdout)
  1 — plumbing error (caller must not post original answers)

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
		r"\b(?:URLs?|PRs?|pull\s+requests?|SHA|commits?|branch(?:es)?|https?|deployments?|build\s+output|test\s+result|logs?|screenshots?)\b",
		re.IGNORECASE,
	),
	re.compile(
		r"\b(?:URL|PR\s+link|commit\s+SHA|branch\s+name|deployment\s+URL)\b"
		r".{0,40}"
		r"\b(?:for|of|from|to|containing|with)\b",
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
_WEAKENS_CONTROL_PATTERNS = [
	re.compile(
		r"\b(?:skip\w*|disabl\w*|bypass\w*|omit\w*|ignor\w*|waiv\w*|defer\w*|postpon\w*|turn\s+off|not\s+requir\w*|drop\w*|remov\w*|relax\w*|best\s+effort)\b"
		r".{0,40}?\b(?:verif\w*|validat\w*|check\w*|signature\w*|auth(?:oriz\w*|entic\w*|[nz])?\b|security|review\w*|approv\w*|audit\w*|scan\w*|test\w*|gate\w*|guard\w*|control\w*)",
		re.IGNORECASE,
	),
	re.compile(
		r"\bwithout\s+(?:\w+\s+){0,2}(?:verif\w*|validat\w*|check\w*|signature\w*|auth(?:oriz\w*|entic\w*|[nz])?\b|security|review\w*|approv\w*|audit\w*|scan\w*|test\w*|gate\w*|guard\w*|control\w*)\b",
		re.IGNORECASE,
	),
	re.compile(
		r"\b(?:verification|validation|checks?|signatures?|authorization|authentication|security|reviews?|approvals?|audits?|scans?|test\s+is|tests\s+are|gates?|guards?|controls?)\b"
		r"\s+(?:(?:is|are|was|were|has\s+been|have\s+been|had\s+been|should\s+be|to\s+be|being)\s+)?(?:skipped|disabled|bypassed|omitted|waived|defer\w*|postpon\w*|optional|not\s+required|no\s+longer\s+(?:required|needed)|advisory|best\s+effort|relax\w*)\b",
		re.IGNORECASE,
	),
	re.compile(
		r"\b(?:(?:security|verification|validation|checks?|audits?|reviews?|controls?)\s+)?(?:tests?|scans?|checks?|gates?|guards?|reviews?|audits?)\s+"
		r"(?:(?:is|are|was|were|has\s+been|have\s+been|had\s+been|should\s+be|to\s+be|being)\s+)?(?:skipped|disabled|bypassed|omitted|waived|defer\w*|postpon\w*)\b",
		re.IGNORECASE,
	),
	re.compile(
		r"\b(?:security\s+)?(?:verification|validation|checks?|signatures?|authorization|authentication|reviews?|approvals?|audits?|scans?|tests?|gates?|guards?|controls?)\s+"
		r"(?:will|would|shall|should|must|could|is|are|was|were)\s+not\s+"
		r"(?:(?:going\s+to\s+)?(?:be|being)\s+)?(?:performed|run|executed|done|carried\s+out|verif\w*|requir\w*)\b",
		re.IGNORECASE,
	),
	re.compile(
		r"\b(?:do\s+not|does\s+not|did\s+not|don't|never|no|refuse\s+to|stop)\s+(?:\w+\s+){0,2}(?:verif\w*|validat\w*|check\w*|auth(?:oriz\w*|entic\w*|[nz])?\b|review\w*|approv\w*|audit\w*|scan\w*|test\w*|guard\w*)\b",
		re.IGNORECASE,
	),
	re.compile(
		r"\b(?:will|would|should|could|must)\s+not\s+(?:be\s+)?(?:verif\w*|validat\w*|check\w*|review\w*|approv\w*|audit\w*|scan\w*|test\w*)\b",
		re.IGNORECASE,
	),
	re.compile(r"\b(?:unverified|unvalidated|unchecked|unauthenticated|unreviewed|untested)\b", re.IGNORECASE),
	re.compile(r"\b(?:do\s+not|don't|never)\s+enforce\b.{0,40}\b(?:security|verif\w*|validat\w*|check\w*|auth(?:oriz\w*|entic\w*|[nz])?\b|gate\w*|guard\w*)\b", re.IGNORECASE),
]
_CONTRACTION_EXPANSIONS = {
	"isn't": "is not", "aren't": "are not", "wasn't": "was not", "weren't": "were not",
	"won't": "will not", "wouldn't": "would not", "shouldn't": "should not",
	"mustn't": "must not", "couldn't": "could not", "doesn't": "does not",
	"don't": "do not", "didn't": "did not",
}
_DATA_EVIDENCE_PATTERNS = {
	"url": (re.compile(r"\b(?:URL|link|http|deployment)\b", re.IGNORECASE), re.compile(r"https?://\S+", re.IGNORECASE)),
	"pr": (re.compile(r"\b(?:PR|pull\s+request)\b", re.IGNORECASE), re.compile(r"/pull/\d+\b|\bPR\s*#?\d+\b", re.IGNORECASE)),
	"sha": (re.compile(r"\b(?:SHA|commit)\b", re.IGNORECASE), re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b", re.IGNORECASE)),
	"branch": (re.compile(r"\bbranch\b", re.IGNORECASE), None),
	"build output": (re.compile(r"\bbuild\s+output\b", re.IGNORECASE), None),
	"test result": (re.compile(r"\btest\s+result\b", re.IGNORECASE), None),
	"log": (re.compile(r"\blog\b", re.IGNORECASE), None),
	"screenshot": (re.compile(r"\bscreenshot\b", re.IGNORECASE), None),
}
_LETTER_DECISION_RE = re.compile(r"\*{0,2}(Q\d+)\*{0,2}\s*:\s*\*{0,2}([A-Z](?:\+[A-Z])*)\*{0,2}")


def _parse_questions(clarification_text: str) -> dict[str, dict[str, str]]:
	"""Parse clarification text into {Q_ID: {letter: option_text}}."""
	questions: dict[str, dict[str, str]] = {}
	current_qid = ""

	for line in clarification_text.splitlines():
		# Detect question header: > **Q1: ...** or Q1: ...
		qid_match = re.match(r"\s*(?:>\s*)*\*{0,2}(Q\d+)\*{0,2}\s*:", line)
		if qid_match:
			current_qid = qid_match.group(1)
			questions[current_qid] = {}
			continue

		if not current_qid:
			continue

		# Detect option line: - **A** — description  OR  - A — description  OR  - A: description
		opt_match = re.match(
			r"\s*(?:>\s*)*[-*]\s+\*{0,2}([A-Z])\*{0,2}\s*(?:—|–|--|-|:)\s*(.*)", line
		)
		if opt_match:
			letter = opt_match.group(1)
			text = opt_match.group(2).strip()
			questions[current_qid][letter] = text

	return questions


def _parse_answers(answers_text: str) -> dict[str, list[str]]:
	"""Parse answer text into {Q_ID: [selected_letters]}."""
	answers: dict[str, list[str]] = {}
	in_decisions = True

	for line in answers_text.splitlines():
		stripped = line.strip()
		header_token = stripped.lstrip("#* \t").upper()
		if header_token.startswith("DECISIONS"):
			in_decisions = True
			continue
		if header_token.startswith(("RATIONALE", "EXECUTION PLAN", "ESCALATION", "SETUP REQUIRED", "DATA-PROVISION GUARD OVERRIDES")):
			in_decisions = False
			continue

		if in_decisions:
			# Match Q1: A or Q1: A+C, including Markdown emphasis.
			m = _LETTER_DECISION_RE.fullmatch(stripped)
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


def _option_weakens_control(text: str) -> bool:
	text = re.sub(
		r"\b(?:isn|aren|wasn|weren|won|wouldn|shouldn|mustn|couldn|doesn|don|didn)['’]t\b",
		lambda contraction_match: _CONTRACTION_EXPANSIONS[contraction_match.group().lower().replace("’", "'")],
		text, flags=re.IGNORECASE,
	)
	text = re.sub(
		r"\b(?:(?:do|does|did|will|would|should|must|could)\s+not|never)\s+(?:skip\w*|disabl\w*|bypass\w*|omit\w*|ignor\w*|waiv\w*|defer\w*|postpon\w*|drop\w*|remov\w*|relax\w*)\b",
		"", text, flags=re.IGNORECASE,
	)
	return any(pattern.search(text) for pattern in _WEAKENS_CONTROL_PATTERNS)


def _required_data_present(option_text: str, evidence_text: str) -> bool:
	"""Unknown data kinds cannot be proven present by a textual match."""
	# Only split explicit alternatives without a shared "and" requirement.
	if re.search(r"\bor\b", option_text, re.IGNORECASE) and not re.search(r"\band\b", option_text, re.IGNORECASE):
		return any(_required_data_present(part, evidence_text) for part in re.split(r"\bor\b", option_text, flags=re.IGNORECASE))
	requested = [detector for keyword, detector in _DATA_EVIDENCE_PATTERNS.values() if keyword.search(option_text)]
	if _DATA_EVIDENCE_PATTERNS["url"][0].search(option_text) and _DATA_EVIDENCE_PATTERNS["pr"][0].search(option_text):
		if not re.search(r"https?://\S+/pull/\d+\b", evidence_text, re.IGNORECASE):
			return False
	return bool(requested) and all(detector is not None and detector.search(evidence_text) for detector in requested)


def _find_fallback(options: dict[str, str], exclude_letters: set[str]) -> tuple[str | None, str | None]:
	"""Find the most conservative fallback option."""
	# First pass: look for options matching fallback patterns
	candidates = []
	rejected_weakening = None
	for letter, text in sorted(options.items()):
		if letter in exclude_letters:
			continue
		if _option_weakens_control(text):
			rejected_weakening = letter
			continue
		if _FALLBACK_PATTERNS.search(text) and not _option_requires_data(text):
			candidates.append(letter)

	if candidates:
		# Prefer the last candidate (typically the most conservative)
		return candidates[-1], rejected_weakening

	# Second pass: pick the last available option that doesn't require data
	for letter in sorted(options.keys(), reverse=True):
		if letter in exclude_letters:
			continue
		if _option_weakens_control(options[letter]):
			continue
		if not _option_requires_data(options[letter]):
			return letter, rejected_weakening

	return None, rejected_weakening


def run_guard(clarification_file: Path, answers_file: Path, evidence_files: tuple[Path, ...] | list[Path] = ()) -> str:
	"""Run the data-provision guard and return (possibly patched) answers."""
	clarification_text = clarification_file.read_text(encoding="utf-8", errors="replace")
	answers_text = answers_file.read_text(encoding="utf-8", errors="replace")

	questions = _parse_questions(clarification_text)
	answers = _parse_answers(answers_text)

	if answers and not questions:
		raise ValueError("cannot parse clarification questions for lettered answers")
	if not questions or not answers:
		return answers_text

	# The model's answer can invent a plausible URL; use supplied context only.
	evidence_text = ""
	for evidence_file in evidence_files:
		if evidence_file.is_file():
			evidence_text += "\n" + evidence_file.read_text(encoding="utf-8", errors="replace")

	overrides: dict[str, str] = {}
	override_reasons: list[str] = []
	escalations: list[str] = []

	for qid, selected_letters in answers.items():
		if qid not in questions or any(letter not in questions[qid] for letter in selected_letters):
			raise ValueError(f"cannot match selected options for {qid}")
		q_options = questions[qid]
		weakening_selected_letters = [letter for letter in selected_letters if _option_weakens_control(q_options[letter])]
		if weakening_selected_letters:
			overrides[qid] = "ESCALATE"
			override_reasons.append(f"{qid}: escalated {'+'.join(weakening_selected_letters)} (selected option weakens a verification/security control)")
			escalations.append(f"{qid}: Cannot use {'+'.join(weakening_selected_letters)}: "
				f"{'; '.join(q_options[letter] for letter in weakening_selected_letters)} (weakens a control).")
			continue
		data_requiring_letters: set[str] = set()

		for letter in selected_letters:
			if letter in q_options and _option_requires_data(q_options[letter]) and not _required_data_present(q_options[letter], evidence_text):
				data_requiring_letters.add(letter)

		if data_requiring_letters:
			if len(selected_letters) > 1:
				# Replacing the whole combination could silently discard a required check.
				overrides[qid] = "ESCALATE"
				override_reasons.append(
					f"{qid}: escalated {'+'.join(selected_letters)} "
					"(combined selection needs unavailable external data; cannot safely replace individual options)"
				)
				escalations.append(
					f"{qid}: Missing data required by option(s) {', '.join(sorted(data_requiring_letters))}: "
					f"{'; '.join(q_options[letter] for letter in sorted(data_requiring_letters))}. "
					"Cannot replace a combined decision without dropping its other selected requirements."
				)
				continue
			fallback, rejected_weakening = _find_fallback(q_options, data_requiring_letters)
			if fallback:
				overrides[qid] = fallback
				override_reasons.append(
					f"{qid}: overrode {'+'.join(sorted(data_requiring_letters))} -> {fallback} "
					f"(option requires external data the auto-responder cannot provide)"
				)
			elif rejected_weakening:
				overrides[qid] = "ESCALATE"
				override_reasons.append(
					f"{qid}: escalated {'+'.join(sorted(data_requiring_letters))} "
					"(option requires external data the auto-responder cannot provide; the only fallback weakens a verification/security control)"
				)
				escalations.append(
					f"{qid}: Missing data required by option(s) {', '.join(sorted(data_requiring_letters))}: "
					f"{'; '.join(q_options[letter] for letter in sorted(data_requiring_letters))}. "
					f"Cannot use {rejected_weakening}: {q_options[rejected_weakening]} (weakens a control)."
				)
			else:
				overrides[qid] = "ESCALATE"
				override_reasons.append(
					f"{qid}: escalated {'+'.join(sorted(data_requiring_letters))} "
					"(option requires unavailable external data; no safe fallback available)"
				)
				escalations.append(
					f"{qid}: Missing data required by option(s) {', '.join(sorted(data_requiring_letters))}: "
					f"{'; '.join(q_options[letter] for letter in sorted(data_requiring_letters))}. "
					"No fallback without unavailable data or weakened controls is available."
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
			if (decision_match := _LETTER_DECISION_RE.fullmatch(stripped)) and decision_match.group(1) == qid:
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
	if escalations:
		patched_lines.append("")
		patched_lines.append("ESCALATION:")
		patched_lines.extend(escalations)

	return "\n".join(patched_lines)


def main() -> int:
	parser = argparse.ArgumentParser(description="Clarify data-provision guard")
	parser.add_argument("--clarification-file", required=True, type=Path)
	parser.add_argument("--answers-file", required=True, type=Path)
	parser.add_argument("--evidence-file", action="append", type=Path, default=[])
	args = parser.parse_args()

	if not args.clarification_file.exists() or not args.answers_file.exists():
		print("::error::Data-provision guard: input files missing; refusing to post unchecked answers.", file=sys.stderr)
		return 1

	try:
		result = run_guard(args.clarification_file, args.answers_file, args.evidence_file)
		print(result)
		return 0
	except Exception as exc:
		print(f"::error::Data-provision guard failed; refusing to post unchecked answers: {exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())

#!/usr/bin/env python3
"""Behavior tests for the orchestrator auto-answer (RECOMMENDED) parsers.

Two parsers in the repo extract the ``(RECOMMENDED)`` letter for each
``Q-ID`` from a clarification-questions comment, so the orchestrator can
auto-answer without a human:

- The Perl heredoc inside ``.github/workflows/plan.yml`` (parses the
  Codex output file and emits ``status=ok|error`` plus
  ``mapping=Q1->A, Q2->B`` / ``answer=Q1: A, Q2: B``).
- The ``extract_recommended_answers`` helper in
  ``scripts/orchestrate_poll_process.sh`` (parses the latest
  clarification comment via ``perl -ne`` and emits ``Q1: A`` /
  ``Q1: A+B`` lines, one per Q-ID with at least one ``(RECOMMENDED)``
  bullet).

Both must accept LLM drift in option-bullet formatting:
``- **A** — …``, ``- A — …``, ``- A) …``, ``- A. …``,
``- A: …``. The original strict regex tripped on ``- A) …``
output produced by Codex on issue
``shubhodeep1/tele-funtoken-msg-scoring#2812`` (run 25560150330) and
forced a human ``/answer``; this file pins coverage so the next regex
tweak can't silently regress.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
PROMPT_PLAN = REPO_ROOT / "prompts" / "mode-plan.txt"
PROMPT_CLARIFY = REPO_ROOT / "prompts" / "mode-clarify.txt"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _extract_plan_yml_perl_heredoc() -> str:
	wf = _read(PLAN_WF)
	m = re.search(
		r"perl\s*-\s*\"\$\{CODEX_OUTPUT_FILE\}\"\s*>\s*\"\$\{PARSE_RESULT_FILE\}\"\s*<<'PERL'\n(.*?)\n\s*PERL\b",
		wf,
		re.DOTALL,
	)
	assert m, "Failed to locate auto-answer Perl heredoc in plan.yml"
	return m.group(1)


def _run_plan_parser(input_text: str) -> dict[str, str]:
	body = _extract_plan_yml_perl_heredoc()
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		script = tmp_path / "parser.pl"
		script.write_text(body, encoding="utf-8")
		input_file = tmp_path / "input.txt"
		input_file.write_text(input_text, encoding="utf-8")
		proc = subprocess.run(
			["perl", str(script), str(input_file)],
			capture_output=True,
			text=True,
			check=True,
		)
	result: dict[str, str] = {}
	for line in proc.stdout.splitlines():
		if "=" in line:
			key, _, value = line.partition("=")
			result[key.strip()] = value.strip()
	return result


def _extract_poll_perl_program() -> str:
	source = _read(POLL_PROCESS)
	m = re.search(
		r"clarify_body\}\"\s*\|\s*perl\s*-ne\s*'(.*?)'\s*\n\s*\}\s*\n",
		source,
		re.DOTALL,
	)
	assert m, "Failed to locate perl -ne program in orchestrate_poll_process.sh"
	return m.group(1)


def _run_poll_parser(input_text: str) -> str:
	program = _extract_poll_perl_program()
	proc = subprocess.run(
		["perl", "-ne", program],
		input=input_text,
		capture_output=True,
		text=True,
		check=True,
	)
	return proc.stdout


_TEMPLATE_FORM = (
	"Q1: Which baseline?\n"
	"- **A** — first option (RECOMMENDED)\n"
	"- **B** — second option\n"
)

_DASH_FORM = (
	"Q1: Which baseline?\n"
	"- A — first option (Recommended)\n"
	"- B — second option\n"
)

_PAREN_FORM = (
	"Q1: Which baseline?\n"
	"- A) first option (Recommended)\n"
	"- B) second option\n"
)

_PERIOD_FORM = (
	"Q1: Which baseline?\n"
	"- A. first option (Recommended)\n"
	"- B. second option\n"
)

_COLON_FORM = (
	"Q1: Which baseline?\n"
	"- A: first option (Recommended)\n"
	"- B: second option\n"
)


def test_plan_parser_accepts_template_form() -> None:
	r = _run_plan_parser(_TEMPLATE_FORM)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A", r
	assert r.get("answer") == "Q1: A", r


def test_plan_parser_accepts_dash_no_bold() -> None:
	r = _run_plan_parser(_DASH_FORM)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A", r


def test_plan_parser_accepts_paren_form() -> None:
	r = _run_plan_parser(_PAREN_FORM)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A", r


def test_plan_parser_accepts_period_form() -> None:
	r = _run_plan_parser(_PERIOD_FORM)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A", r


def test_plan_parser_accepts_colon_form() -> None:
	r = _run_plan_parser(_COLON_FORM)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A", r


def test_plan_parser_errors_when_no_recommended() -> None:
	body = "Q1: Pick one\n- A — first\n- B — second\n"
	r = _run_plan_parser(body)
	assert r.get("status") == "error", r
	assert r.get("reason") == "Missing recommended option for Q1", r


def test_plan_parser_errors_when_multiple_recommended() -> None:
	body = (
		"Q1: Pick one\n"
		"- A — first (Recommended)\n"
		"- B — second (Recommended)\n"
	)
	r = _run_plan_parser(body)
	assert r.get("status") == "error", r
	assert r.get("reason") == "Multiple recommended options for Q1", r


def test_plan_parser_errors_when_no_qids() -> None:
	r = _run_plan_parser("nothing here\n- A — text (Recommended)\n")
	assert r.get("status") == "error", r
	assert r.get("reason") == "No Q-ID blocks detected", r


def test_plan_parser_handles_real_codex_output_from_issue_2812() -> None:
	# Verbatim Q1 line from
	# https://github.com/shubhodeep1/tele-funtoken-msg-scoring/issues/2812#issuecomment-4407063583
	# (failing run https://github.com/.../actions/runs/25560150330).
	body = (
		"Q1: Which code baseline should implementation planning target?\n"
		"Choices:\n"
		"- A) Current checked-out ref `main@40b4bdf28894d86465996f319f909bbfeda5ce60` (Recommended) — plan against the code that is actually present now.\n"
		"- B) A different branch/commit — provide the exact branch name or commit SHA.\n"
		"- C) The issue text is authoritative — re-run after the integration ref is corrected.\n"
	)
	r = _run_plan_parser(body)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A", r
	assert r.get("answer") == "Q1: A", r


def test_plan_parser_rejects_utf8_punctuation_sharing_em_dash_leading_byte() -> None:
	# The em-dash "—" is U+2014 = bytes E2 80 94 in UTF-8; en-dash "–" is
	# U+2013 = E2 80 93. A naive byte-mode character class like
	# ``[—–\-)\.:]`` decomposes to the byte set {E2, 80, 93, 94, -, ),
	# ., :} and accidentally matches the leading byte of unrelated UTF-8
	# punctuation such as U+2018 (left single quote, bytes E2 80 98).
	# The alternation form ``(?:—|–|[-)\.:])`` matches em-/en-dash as
	# whole 3-byte sequences and avoids the false positive.
	body = "Q1: Pick one\n- A ‘foo’ (Recommended)\n"
	r = _run_plan_parser(body)
	assert r.get("status") == "error", r
	assert r.get("reason") == "Missing recommended option for Q1", r


def test_plan_parser_skips_recommended_lines_inside_code_fences() -> None:
	body = (
		"Q1: Real question\n"
		"```\n"
		"- A — illustrative example (Recommended)\n"
		"```\n"
		"- B — actual option (Recommended)\n"
	)
	r = _run_plan_parser(body)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->B", r


def test_poll_parser_accepts_template_form() -> None:
	out = _run_poll_parser(_TEMPLATE_FORM)
	assert out.strip() == "Q1: A", out


def test_poll_parser_accepts_dash_no_bold() -> None:
	out = _run_poll_parser(_DASH_FORM)
	assert out.strip() == "Q1: A", out


def test_poll_parser_accepts_paren_form() -> None:
	out = _run_poll_parser(_PAREN_FORM)
	assert out.strip() == "Q1: A", out


def test_poll_parser_accepts_period_form() -> None:
	out = _run_poll_parser(_PERIOD_FORM)
	assert out.strip() == "Q1: A", out


def test_poll_parser_accepts_colon_form() -> None:
	out = _run_poll_parser(_COLON_FORM)
	assert out.strip() == "Q1: A", out


def test_poll_parser_joins_multiple_recommended_with_plus() -> None:
	body = (
		"Q1: Pick one\n"
		"- A — first (Recommended)\n"
		"- B — second\n"
		"- C — third (RECOMMENDED)\n"
	)
	out = _run_poll_parser(body)
	assert out.strip() == "Q1: A+C", out


def test_poll_parser_silent_on_no_recommended() -> None:
	body = "Q1: Pick one\n- A — first\n- B — second\n"
	out = _run_poll_parser(body)
	assert out.strip() == "", out


def test_poll_parser_uppercases_lowercase_letter() -> None:
	body = "Q1: Pick one\n- a) first (Recommended)\n"
	out = _run_poll_parser(body)
	assert out.strip() == "Q1: A", out


def test_poll_parser_rejects_utf8_punctuation_sharing_em_dash_leading_byte() -> None:
	# Same byte-class hazard as the plan.yml parser: ensure the script's
	# alternation form does not falsely accept U+2018 etc.
	body = "Q1: Pick one\n- A ‘foo’ (Recommended)\n"
	out = _run_poll_parser(body)
	assert out.strip() == "", out


def test_prompt_template_documents_canonical_recommended_form() -> None:
	plan_prompt = _read(PROMPT_PLAN)
	clarify_prompt = _read(PROMPT_CLARIFY)
	for prompt in (plan_prompt, clarify_prompt):
		assert "**A** — \\<description\\> (RECOMMENDED)" in prompt
		assert "Each option line MUST be exactly" in prompt


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

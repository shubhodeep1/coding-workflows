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

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CLARIFY_WF = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
PROMPT_PLAN = REPO_ROOT / "prompts" / "mode-plan.txt"
PROMPT_CLARIFY = REPO_ROOT / "prompts" / "mode-clarify.txt"
FENCE_SANITIZER = r"s{(^|\n)([ \t]*((?:```|~~~))[^\n]*\n.*?\n[ \t]*\3[ \t]*(?=\n|$))}{$1}gms;"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _sanitize_plan_output(raw_output: str) -> str:
	proc = subprocess.run(
		["perl", "-0pe", FENCE_SANITIZER],
		input=raw_output,
		capture_output=True,
		text=True,
		check=True,
	)
	return proc.stdout


def _extract_plan_yml_perl_heredoc() -> str:
	wf = _read(PLAN_WF)
	m = re.search(
		r"PARSE_SOURCE_FILE=\"\$\{CODEX_OUTPUT_PARSE_FILE:-\$\{CODEX_OUTPUT_FILE\}\}\"\s*\n\s*perl - \"\$\{PARSE_SOURCE_FILE\}\" > \"\$\{PARSE_RESULT_FILE\}\" <<'PERL'\n(.*?)\n\s*PERL\b",
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
		input_file.write_text(_sanitize_plan_output(input_text), encoding="utf-8")
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


def _extract_structured_block_perl_program() -> str:
	wf = _read(PLAN_WF)
	m = re.search(
		r"HAS_STRUCTURED_CLARIFICATION_BLOCK=\"false\"\s*\n\s*if perl -ne '(.*?)'\s*\"\$\{CODEX_OUTPUT_PARSE_FILE\}\";\s*then",
		wf,
		re.DOTALL,
	)
	assert m, "Failed to locate structured-block detection perl in plan.yml"
	return m.group(1)


def _run_structured_block_detector(input_text: str) -> bool:
	program = _extract_structured_block_perl_program()
	with tempfile.TemporaryDirectory() as tmp:
		input_file = Path(tmp) / "input.txt"
		input_file.write_text(_sanitize_plan_output(input_text), encoding="utf-8")
		proc = subprocess.run(
			["perl", "-ne", program, str(input_file)],
			capture_output=True,
			text=True,
		)
	return proc.returncode == 0


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


def _extract_clarify_q_count_patterns() -> list[str]:
	wf = _read(CLARIFY_WF)
	patterns = re.findall(
		r"Q_COUNT=\"\$\(grep -cE '([^']+)' \"\$\{(?:CODEX_OUTPUT_FILE|QUESTIONS_FILE)\}\" \|\| true\)\"",
		wf,
	)
	assert len(patterns) == 2, patterns
	return patterns


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

# Bullet-less drift: Codex emitted plain "A. …" option lines (no "-"
# bullet at all) on tele-funtoken-msg-scoring#3754 (run 32653506599),
# which the pre-fix regex rejected ("Missing recommended option for
# Q1"), forcing a human /answer that never came; stall recovery then
# discarded the questions entirely.
_NO_BULLET_PERIOD_FORM = (
	"Q1: Which baseline?\n"
	"Choices:\n"
	"A. first option (Recommended)\n"
	"B. second option\n"
)

_NO_BULLET_PAREN_FORM = (
	"Q1: Which baseline?\n"
	"A) first option (Recommended)\n"
	"B) second option\n"
)

# Blockquoted canonical form: prompts/mode-plan.txt and
# prompts/mode-clarify.txt render the mandatory question template inside
# a markdown blockquote (`> **Q1: <question>**` … `> Reply: ...`), so a
# Codex emission that follows the prompt verbatim prefixes every line
# with `> `. The pre-fix regexes anchored on `^\s*` never matched that
# prefix and produced a false "Auto-answer parser failed … No Q-ID
# blocks detected" alert (fun-token-multi-chain#434, run 33355986371).
# The "> Reply: Q1: A" line rides along to pin that it neither
# registers as a duplicate Q-ID nor as an option bullet.
_BLOCKQUOTE_TEMPLATE_FORM = (
	"> **Q1: Which baseline?**\n"
	">\n"
	"> Choices:\n"
	"> - **A** — first option (RECOMMENDED)\n"
	"> - **B** — second option\n"
	">\n"
	"> Reply: `Q1: A`\n"
)

_BLOCKQUOTE_MULTI_QUESTION_FORM = (
	"> **Q1: Which baseline?**\n"
	">\n"
	"> Choices:\n"
	"> - **A** — first option (RECOMMENDED)\n"
	"> - **B** — second option\n"
	">\n"
	"> Reply: `Q1: A`\n"
	"\n"
	"> **Q2: Which rollout order?**\n"
	">\n"
	"> Choices:\n"
	"> - **A** — stable first\n"
	"> - **B** — main first (RECOMMENDED)\n"
	">\n"
	"> Reply: `Q2: B`\n"
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


def test_plan_parser_accepts_no_bullet_period_form() -> None:
	r = _run_plan_parser(_NO_BULLET_PERIOD_FORM)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A", r


def test_plan_parser_accepts_no_bullet_paren_form() -> None:
	r = _run_plan_parser(_NO_BULLET_PAREN_FORM)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A", r


def test_plan_parser_accepts_blockquoted_template_form() -> None:
	r = _run_plan_parser(_BLOCKQUOTE_TEMPLATE_FORM)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A", r
	assert r.get("answer") == "Q1: A", r


def test_plan_parser_accepts_blockquoted_multi_question_form() -> None:
	r = _run_plan_parser(_BLOCKQUOTE_MULTI_QUESTION_FORM)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A, Q2->B", r
	assert r.get("answer") == "Q1: A, Q2: B", r


def test_plan_parser_handles_real_codex_output_from_issue_3754() -> None:
	# Verbatim Q1 block from
	# https://github.com/shubhodeep1/tele-funtoken-msg-scoring/issues/3754#issuecomment-5387248058
	# (failing run https://github.com/.../actions/runs/32653506599): plain
	# "A." option lines with no bullet and mixed-case "(Recommended)".
	body = (
		"Q1: What exact fixed payout table should the top-10 leaderboard bucket use?\n"
		"Choices:\n"
		"A. Provide a custom 10-rank percentage/weight table summing to 100%. (Recommended)\n"
		"B. Use linear rank weights `10,9,8,7,6,5,4,3,2,1`.\n"
		"C. Use equal top-10 shares, `10%` each.\n"
	)
	r = _run_plan_parser(body)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->A", r
	assert r.get("answer") == "Q1: A", r


def test_plan_parser_ignores_no_bullet_prose_without_marker() -> None:
	# A bullet-less single-letter line without "(RECOMMENDED)" must not
	# count as a recommendation.
	body = (
		"Q1: Pick one\n"
		"A. first option\n"
		"B. second option\n"
	)
	r = _run_plan_parser(body)
	assert r.get("status") == "error", r
	assert r.get("reason") == "Missing recommended option for Q1", r


def test_plan_parser_ignores_word_initial_letter_prose_with_marker() -> None:
	# Prose whose first token is a multi-character word must not match the
	# bullet-less form even when "(RECOMMENDED)" appears later on the line:
	# the letter must be immediately followed by a separator.
	body = (
		"Q1: Pick one\n"
		"Always prefer the safe default (RECOMMENDED reading).\n"
		"- B — real option (Recommended)\n"
	)
	r = _run_plan_parser(body)
	assert r.get("status") == "ok", r
	assert r.get("mapping") == "Q1->B", r


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


def test_structured_block_detector_accepts_template_form() -> None:
	assert _run_structured_block_detector(
		"Q1: Pick one\nChoices:\n- **A** — text (RECOMMENDED)\n"
	)


def test_structured_block_detector_accepts_paren_form() -> None:
	# Mirrors the failing tele-funtoken-msg-scoring#2812 codex output.
	# Without this fix, a Codex emission that uses `- A) ...` and omits
	# `STATUS: NEEDS_CLARIFICATION` would set needs_clarification=false.
	assert _run_structured_block_detector(
		"Q1: Pick one\n- A) text (Recommended)\n"
	)


def test_structured_block_detector_accepts_dash_no_bold_form() -> None:
	assert _run_structured_block_detector(
		"Q1: Pick one\n- A — text (Recommended)\n"
	)


def test_structured_block_detector_accepts_blockquoted_template_form() -> None:
	assert _run_structured_block_detector(_BLOCKQUOTE_TEMPLATE_FORM)


def test_structured_block_detector_accepts_blockquoted_multi_question_form() -> None:
	assert _run_structured_block_detector(_BLOCKQUOTE_MULTI_QUESTION_FORM)


def test_clarify_q_count_patterns_stay_consistent_for_blockquotes() -> None:
	patterns = _extract_clarify_q_count_patterns()
	assert patterns[0] == patterns[1]
	body = (
		_BLOCKQUOTE_MULTI_QUESTION_FORM
		+ "> **Q3**: split-bold question\n"
		+ "Q4: unquoted question\n"
		+ "  > Q5: indented blockquoted unbolded question\n"
		+ "> Reply: `Q6: A`\n"
	)
	for pattern in patterns:
		proc = subprocess.run(
			["grep", "-cE", pattern],
			input=body,
			capture_output=True,
			text=True,
			check=True,
		)
		assert proc.stdout.strip() == "5", proc.stdout


def test_structured_block_detector_rejects_input_with_no_qid() -> None:
	assert not _run_structured_block_detector(
		"Some prose without a Q-ID.\n- A — looks like a bullet (Recommended)\n"
	)


def test_structured_block_detector_accepts_same_line_chord_form() -> None:
	# Codex can emit a chord recommendation on a single bullet
	# (`- A+B — desc (Recommended)`); both the auto-answer parser at
	# line 1193 and the poll parser accept that shape. Without chord
	# support, a Codex emission that omits `STATUS: NEEDS_CLARIFICATION`
	# and the `Choices:` literal would be missed by the fallback
	# detector and the workflow would set needs_clarification=false.
	assert _run_structured_block_detector(
		"Q1: Pick one\n- A+B — chord recommendation (Recommended)\n"
	)
	assert _run_structured_block_detector(
		"Q1: Pick one\n- **A+C** — chord with bold (RECOMMENDED)\n"
	)


def test_structured_block_detector_accepts_no_bullet_period_form() -> None:
	# Mirrors the tele-funtoken-msg-scoring#3754 codex output: option
	# lines with no "-" bullet at all. The `Choices:` literal already
	# triggers the detector when present; this pins the bullet-less
	# option line as an independent trigger for emissions that omit it.
	assert _run_structured_block_detector(
		"Q1: Pick one\nA. text (Recommended)\n"
	)


def test_structured_block_detector_rejects_no_bullet_prose_without_recommended() -> None:
	for body in (
		"Q1: How does this work?\nA. Just a single-letter prose item\n",
		"Q1: How does this work?\nAlways safe (RECOMMENDED reading)\n",
	):
		assert not _run_structured_block_detector(body), body


def test_structured_block_detector_rejects_prose_bullets_without_recommended() -> None:
	# Without `(RECOMMENDED)` on the bullet, a single-letter prose item
	# after a Q-ID-shaped line must not falsely trigger the heuristic —
	# otherwise the workflow sets needs_clarification=true and runs the
	# auto-answer parser on output that is not a real clarification.
	for body in (
		"Q1: How does this work?\n- A. Just a single-letter prose item\n",
		"Q1: How does this work?\n- A) Some context here, no recommended marker\n",
		"Q1: How does this work?\n- A: caption text\n",
		"Q1: How does this work?\n- A — text without the marker\n",
	):
		assert not _run_structured_block_detector(body), body


def test_structured_block_detector_skips_lines_inside_code_fences() -> None:
	# A bullet inside ``` ... ``` should not satisfy the heuristic on its
	# own; only a Q-ID followed by an out-of-fence bullet should match.
	body = (
		"Q1: Real question\n"
		"```\n"
		"- A — illustrative example (Recommended)\n"
		"```\n"
	)
	# No bullet outside the fence and no Choices: line, so the block is
	# not detected.
	assert not _run_structured_block_detector(body)


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


def test_poll_parser_accepts_no_bullet_period_form() -> None:
	out = _run_poll_parser(_NO_BULLET_PERIOD_FORM)
	assert out.strip() == "Q1: A", out


def test_poll_parser_accepts_no_bullet_paren_form() -> None:
	out = _run_poll_parser(_NO_BULLET_PAREN_FORM)
	assert out.strip() == "Q1: A", out


def test_poll_parser_accepts_blockquoted_template_form() -> None:
	out = _run_poll_parser(_BLOCKQUOTE_TEMPLATE_FORM)
	assert out.strip() == "Q1: A", out


def test_poll_parser_accepts_blockquoted_multi_question_form() -> None:
	out = _run_poll_parser(_BLOCKQUOTE_MULTI_QUESTION_FORM)
	assert out.splitlines() == ["Q1: A", "Q2: B"], out


def test_poll_parser_ignores_no_bullet_prose_without_marker() -> None:
	body = "Q1: Pick one\nA. first option\nB. second option\n"
	out = _run_poll_parser(body)
	assert out.strip() == "", out


def test_poll_parser_joins_multiple_recommended_with_plus() -> None:
	body = (
		"Q1: Pick one\n"
		"- A — first (Recommended)\n"
		"- B — second\n"
		"- C — third (RECOMMENDED)\n"
	)
	out = _run_poll_parser(body)
	assert out.strip() == "Q1: A+C", out


def test_poll_parser_accepts_same_line_chord() -> None:
	# The prompt rules call out `A+C` as a valid letter-only answer.
	# When Codex emits a same-line chord recommendation
	# (`- A+B — text (Recommended)`), the parser must capture the chord
	# rather than silently skip the bullet (previously the single-letter
	# capture rejected the line because `+` isn't in the separator class).
	body = "Q1: Pick one\n- A+B — text (Recommended)\n"
	out = _run_poll_parser(body)
	assert out.strip() == "Q1: A+B", out


def test_poll_parser_combines_same_line_chord_with_other_recommended_bullets() -> None:
	body = (
		"Q1: Pick one\n"
		"- A+B — chord option (Recommended)\n"
		"- C — third (RECOMMENDED)\n"
	)
	out = _run_poll_parser(body)
	assert out.strip() == "Q1: A+B+C", out


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


_EXTRACT_HELPER_AND_STUBS = r"""
extract_fn() {
	awk -v fn="extract_recommended_answers" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__POLLER__"
}

gh_retry() { "$@"; }
gh() { cat "${COMMENTS_FIXTURE}"; }

eval "$(extract_fn)"
extract_recommended_answers "3754"
"""


def _run_extract_recommended_answers(comments_json: str) -> str:
	"""Source the real extract_recommended_answers with a stubbed gh that
	returns ``comments_json`` and return the helper's stdout."""
	with tempfile.TemporaryDirectory() as tmp:
		fixture = Path(tmp) / "comments.json"
		fixture.write_text(comments_json, encoding="utf-8")
		script = _EXTRACT_HELPER_AND_STUBS.replace("__POLLER__", str(POLL_PROCESS))
		env = dict(os.environ)
		env["GITHUB_REPOSITORY"] = "owner/repo"
		env["COMMENTS_FIXTURE"] = str(fixture)
		proc = subprocess.run(
			["bash", "-c", script],
			capture_output=True,
			text=True,
			env=env,
		)
	assert proc.returncode == 0, proc.stderr
	return proc.stdout


def test_extract_recommended_answers_accepts_pat_posted_comment() -> None:
	# Pipeline comments are posted with the GH_PAT, so their author is a
	# human login (e.g. "shubhodeep1") with OWNER association, not a
	# "...[bot]" account. The pre-fix jq filter required a bot login and
	# therefore never found the clarification comment — every
	# auto_respond_clarify stall recovery fell back to "No recommended
	# answers could be extracted" (tele-funtoken-msg-scoring#3754). The
	# body carries the same bullet-less option lines that comment carried.
	comments = json.dumps(
		[
			{
				"id": 5387248058,
				"created_at": "2026-08-23T17:09:16Z",
				"user": {"login": "shubhodeep1"},
				"author_association": "OWNER",
				"body": (
					"<!-- ai:clarification-questions -->\n"
					"Q1: What exact fixed payout table should the top-10 leaderboard bucket use?\n"
					"Choices:\n"
					"A. Provide a custom 10-rank percentage/weight table summing to 100%. (Recommended)\n"
					"B. Use linear rank weights `10,9,8,7,6,5,4,3,2,1`.\n"
					"C. Use equal top-10 shares, `10%` each.\n"
				),
			}
		]
	)
	out = _run_extract_recommended_answers(comments)
	assert out.strip() == "Q1: A", out


def test_extract_recommended_answers_still_accepts_bot_posted_comment() -> None:
	# GitHub Apps comment with author_association NONE but a reserved
	# "...[bot]" login; installation requires repo-admin trust, so the
	# bot-login path stays accepted.
	comments = json.dumps(
		[
			{
				"id": 1,
				"created_at": "2026-08-23T17:09:16Z",
				"user": {"login": "github-actions[bot]"},
				"author_association": "NONE",
				"body": (
					"<!-- ai:clarification-questions -->\n"
					"Q1: Pick one\n"
					"Choices:\n"
					"- **A** — first (RECOMMENDED)\n"
					"- **B** — second\n"
				),
			}
		]
	)
	out = _run_extract_recommended_answers(comments)
	assert out.strip() == "Q1: A", out


def test_extract_recommended_answers_rejects_spoofed_marker_from_untrusted_author() -> None:
	# An untrusted commenter (author_association NONE, non-bot login)
	# pasting the HTML marker must not be able to become the "latest
	# clarification comment" and steer the stall-recovery auto-answer.
	comments = json.dumps(
		[
			{
				"id": 3,
				"created_at": "2026-08-23T18:00:00Z",
				"user": {"login": "random-drive-by"},
				"author_association": "NONE",
				"body": (
					"<!-- ai:clarification-questions -->\n"
					"Q1: Pick one\n"
					"Choices:\n"
					"- **A** — attacker-preferred option (RECOMMENDED)\n"
				),
			}
		]
	)
	out = _run_extract_recommended_answers(comments)
	assert out.strip() == "", out


def test_extract_recommended_answers_accepts_blockquoted_comment() -> None:
	# A clarification comment carrying the prompt's canonical blockquoted
	# template verbatim previously extracted nothing — the Q-ID and bullet
	# regexes never matched the "> " prefix — so stall recovery fell back
	# to "No recommended answers could be extracted"
	# (fun-token-multi-chain#434; same gap as plan.yml, fixed by #3939).
	comments = json.dumps(
		[
			{
				"id": 4,
				"created_at": "2026-08-31T09:00:00Z",
				"user": {"login": "shubhodeep1"},
				"author_association": "OWNER",
				"body": (
					"<!-- ai:clarification-questions -->\n" + _BLOCKQUOTE_MULTI_QUESTION_FORM
				),
			}
		]
	)
	out = _run_extract_recommended_answers(comments)
	assert out.splitlines() == ["Q1: A", "Q2: B"], out


def test_extract_recommended_answers_ignores_unmarked_comments() -> None:
	# A comment without the HTML marker or legacy prefix is not a
	# clarification comment, whoever posted it.
	comments = json.dumps(
		[
			{
				"id": 2,
				"created_at": "2026-08-23T17:09:16Z",
				"user": {"login": "shubhodeep1"},
				"body": "Q1: Pick one\n- **A** — first (RECOMMENDED)\n",
			}
		]
	)
	out = _run_extract_recommended_answers(comments)
	assert out.strip() == "", out


_STALL_ANSWER_HELPER_AND_STUBS = r"""
extract_named_fn() {
	awk -v fn="$1" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__POLLER__"
}

GH_CALLS_FILE="${COMMENTS_FIXTURE}.calls"
gh_retry() { "$@"; }
gh() { echo call >> "${GH_CALLS_FILE}"; cat "${COMMENTS_FIXTURE}"; }

eval "$(extract_named_fn extract_recommended_answers)"
eval "$(extract_named_fn stall_recovery_clarify_answer)"
stall_recovery_clarify_answer "4815"
echo "GH_CALLS=$(wc -l < "${GH_CALLS_FILE}" | tr -d ' ')"
"""


def _run_stall_recovery_clarify_answer(comments: list) -> tuple[str, str, str]:
	"""Run the real stall_recovery_clarify_answer against a stubbed gh.

	Returns (source, answer_text, stderr) and asserts the comments were
	read exactly once (the RECOMMENDED fallback reuses the same read)."""
	with tempfile.TemporaryDirectory() as tmp:
		fixture = Path(tmp) / "comments.json"
		fixture.write_text(json.dumps(comments), encoding="utf-8")
		script = _STALL_ANSWER_HELPER_AND_STUBS.replace("__POLLER__", str(POLL_PROCESS))
		env = dict(os.environ)
		env["GITHUB_REPOSITORY"] = "owner/repo"
		env["COMMENTS_FIXTURE"] = str(fixture)
		env["RUNTIME_DIR"] = tmp
		proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
	assert proc.returncode == 0, proc.stderr
	lines = proc.stdout.rstrip("\n").split("\n")
	assert lines[-1] == "GH_CALLS=1", proc.stdout
	body = lines[:-1]
	return body[0], "\n".join(body[1:]), proc.stderr


_STALL_QUESTIONS = {
	"id": 6370000000,
	"created_at": "2026-10-23T10:00:00Z",
	"user": {"login": "shubhodeep1"},
	"author_association": "OWNER",
	"body": (
		"<!-- ai:clarification-questions -->\n"
		"**Q1: Which fix?**\n\nChoices:\n"
		"- **A** — Reduce to a warning (RECOMMENDED)\n"
		"- **B** — Fix the caller\n\n"
		"**Q2: Is the key set?**\n\nChoices:\n"
		"- **A** — Unknown (RECOMMENDED)\n"
		"- **B** — Yes\n"
	),
}


def _pipeline_answer(comment_id: int, created_at: str, body: str, association: str = "OWNER") -> dict:
	return {
		"id": comment_id,
		"created_at": created_at,
		"user": {"login": "shubhodeep1"},
		"author_association": association,
		"body": body,
	}


def test_stall_recovery_reasserts_worker_answer_over_recommended() -> None:
	# tele-funtoken-msg-scoring#4815: the clarify-respond worker answered
	# Q1: B, Q2: A; stall recovery must repeat that, not post Q1: A.
	worker = _pipeline_answer(
		6377088072,
		"2026-10-23T10:05:00Z",
		"/answer [auto-answered-by-orchestrator]\n\n"
		"Auto-answered by the orchestrator clarify-respond workflow.\n\n"
		"DECISIONS:\nQ1: B\nQ2: A\n\nRATIONALE:\nQ1: B keeps fail-closed.\nQ2: unknown.\n",
	)
	source, text, stderr = _run_stall_recovery_clarify_answer([worker, _STALL_QUESTIONS])
	assert source == "reasserted", (source, text)
	assert "Q1: B" in text and "Q2: A" in text, text
	assert "Q1: A" not in text, text
	assert "RATIONALE:" in text, text
	assert not text.startswith("/answer"), text
	assert "[auto-answered-by-" not in text, text
	assert "STALL_CLARIFY_ANSWER issue=4815 source=reasserted answer_comment=6377088072" in stderr


def test_stall_recovery_reasserts_reframe_execution_plan() -> None:
	# tele #4820: a REFRAME decision is not a letter; its execution plan
	# must survive the re-assertion verbatim.
	worker = _pipeline_answer(
		6379288972,
		"2026-10-23T10:05:00Z",
		"/answer [auto-answered-by-orchestrator]\n\nDECISIONS:\nQ1: REFRAME\nQ2: B\n\n"
		"EXECUTION PLAN (required for any non-letter decision):\nQ1:\n  strategy: reframe\n"
		"  replaces_acceptance_criterion: \"Identify the exact ERROR message\"\n",
	)
	source, text, _ = _run_stall_recovery_clarify_answer([_STALL_QUESTIONS, worker])
	assert source == "reasserted"
	assert "Q1: REFRAME" in text and "strategy: reframe" in text, text
	assert "replaces_acceptance_criterion" in text, text


def test_stall_recovery_keeps_one_line_plan_auto_answer() -> None:
	# plan.yml posts `/answer Q1: A Q2: B [auto-answered-by-orchestrator]`.
	one_line = _pipeline_answer(
		6377000001, "2026-10-23T10:06:00Z", "/answer Q1: B Q2: B [auto-answered-by-orchestrator]"
	)
	source, text, _ = _run_stall_recovery_clarify_answer([one_line, _STALL_QUESTIONS])
	assert source == "reasserted"
	assert text == "Q1: B Q2: B", text


def test_stall_recovery_ignores_answer_older_than_latest_questions() -> None:
	# An answer to an earlier clarification round does not answer the
	# current questions, so fall back to RECOMMENDED.
	stale = _pipeline_answer(
		6360000000,
		"2026-10-22T10:00:00Z",
		"/answer [auto-answered-by-orchestrator]\n\nDECISIONS:\nQ1: B\nQ2: B\n",
	)
	source, text, stderr = _run_stall_recovery_clarify_answer([_STALL_QUESTIONS, stale])
	assert source == "recommended", (source, text)
	assert text == "Q1: A\nQ2: A", text
	assert "answer_comment=none" in stderr


def test_stall_recovery_ignores_untrusted_or_unmarked_answers() -> None:
	spoofed = _pipeline_answer(
		6377000002,
		"2026-10-23T10:07:00Z",
		"/answer [auto-answered-by-orchestrator]\n\nDECISIONS:\nQ1: B\nQ2: B\n",
		association="NONE",
	)
	unmarked = _pipeline_answer(6377000003, "2026-10-23T10:08:00Z", "/answer\n\nQ1: B\nQ2: B\n")
	no_decision = _pipeline_answer(
		6377000004,
		"2026-10-23T10:09:00Z",
		"/answer [auto-answered-by-poller]\n\n_Standalone stall recovery: clarification stalled. Proceeding with available context._",
	)
	source, text, _ = _run_stall_recovery_clarify_answer([no_decision, unmarked, spoofed, _STALL_QUESTIONS])
	assert source == "recommended", (source, text)
	assert text == "Q1: A\nQ2: A", text


def test_stall_recovery_reports_none_without_questions() -> None:
	source, text, _ = _run_stall_recovery_clarify_answer([])
	assert source == "none" and text == "", (source, text)


def test_stall_recovery_callers_use_reassert_helper() -> None:
	# Both auto_respond_clarify paths must go through the helper, not call
	# extract_recommended_answers directly.
	text = _read(POLL_PROCESS)
	assert text.count('="$(stall_recovery_clarify_answer "${issue_num}")"') == 2
	callers = [
		line for line in text.splitlines()
		if '$(extract_recommended_answers "${issue_num}")' in line
	]
	# Only the helper's own fallback (no prefetched file) may call it bare.
	assert len(callers) == 1, callers


def test_prompt_template_documents_canonical_recommended_form() -> None:
	plan_prompt = _read(PROMPT_PLAN)
	clarify_prompt = _read(PROMPT_CLARIFY)
	for prompt in (plan_prompt, clarify_prompt):
		# Pre-existing blockquote template (escaped angle brackets so
		# `<description>` renders literally inside the blockquote).
		assert "**A** — \\<description\\> (RECOMMENDED)" in prompt
		# New explicit canonical-form instruction (added in this change).
		# The placeholder lives inside a backtick code span where markdown
		# does not parse HTML, so the angle brackets stay unescaped.
		assert (
			"Each option line MUST be exactly `- **A** — <description> (RECOMMENDED)`"
			in prompt
		)


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

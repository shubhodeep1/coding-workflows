"""The clarify-respond data guard must not turn required checks into skips."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD_PATH = ROOT / "scripts" / "clarify_data_provision_guard.py"
AUTO_DECISIONS_PATH = ROOT / "scripts" / "auto_decisions.py"
GUARD_SPEC = importlib.util.spec_from_file_location("clarify_data_provision_guard_test", GUARD_PATH)
guard = importlib.util.module_from_spec(GUARD_SPEC)
GUARD_SPEC.loader.exec_module(guard)
AUTO_SPEC = importlib.util.spec_from_file_location("auto_decisions_guard_test", AUTO_DECISIONS_PATH)
auto_decisions = importlib.util.module_from_spec(AUTO_SPEC)
AUTO_SPEC.loader.exec_module(auto_decisions)

QUESTIONS = "Q1: How should the PR be checked?\n- A — Require the PR URL and verify its head\n- B — Skip this verification\n"
ANSWER = "DECISIONS:\nQ1: A\n\nRATIONALE:\nQ1: Verification is required.\n"


def _run_guard(tmp_path: Path, questions: str = QUESTIONS, answer: str = ANSWER, evidence_files: tuple[Path, ...] = ()) -> str:
	clarification = tmp_path / "questions.txt"
	clarification.write_text(questions, encoding="utf-8")
	answers = tmp_path / "answers.txt"
	answers.write_text(answer, encoding="utf-8")
	return guard.run_guard(clarification, answers, evidence_files)


def test_required_verification_escalates_instead_of_skipping(tmp_path: Path) -> None:
	result = _run_guard(tmp_path)
	assert re.search(r"^Q[0-9]+:\s*ESCALATE$", result, re.MULTILINE)
	assert "Q1: escalated A" in result
	assert "ESCALATION:\nQ1: Missing data required by option(s) A:" in result
	assert "Cannot use B: Skip this verification" in result
	assert auto_decisions.from_answers(QUESTIONS, result)["answers"] == "Q1: ESCALATE\n"


def test_worker_plan_supplies_required_evidence(tmp_path: Path) -> None:
	answer = ANSWER + "\nEXECUTION PLAN:\nPR: https://github.com/o/r/pull/12\n"
	assert _run_guard(tmp_path, answer=answer) == answer


def test_question_example_is_not_evidence(tmp_path: Path) -> None:
	questions = QUESTIONS + "Example: https://github.com/o/r/pull/12\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions)


def test_evidence_file_requires_a_matching_datum(tmp_path: Path) -> None:
	evidence = tmp_path / "evidence.txt"
	evidence.write_text("PR https://github.com/o/r/pull/12", encoding="utf-8")
	assert _run_guard(tmp_path, evidence_files=(evidence,)) == ANSWER
	evidence.write_text("This text has no PR reference.", encoding="utf-8")
	assert "Q1: ESCALATE" in _run_guard(tmp_path, evidence_files=(evidence,))
	# URL alone is not evidence of a PR when both kinds are requested.
	evidence.write_text("https://example.com/other", encoding="utf-8")
	assert "Q1: ESCALATE" in _run_guard(tmp_path, evidence_files=(evidence,))
	evidence.write_text("PR #12 https://example.com/other", encoding="utf-8")
	assert "Q1: ESCALATE" in _run_guard(tmp_path, evidence_files=(evidence,))


@pytest.mark.parametrize("fallback", ["Proceed without the URL using available information", "Use default scope"])
def test_nonweakening_fallback_still_avoids_loop(tmp_path: Path, fallback: str) -> None:
	questions = f"Q1: Which path?\n- A — Provide the PR URL\n- B — {fallback}\n"
	result = _run_guard(tmp_path, questions=questions)
	assert "Q1: B" in result
	assert "Q1: overrode A -> B" in result
	assert "ESCALATION:" not in result


@pytest.mark.parametrize("fallback", [
	"Skip this verification", "Disable signature check", "Bypass the approval gate",
	"Proceed without verification", "Verification is optional", "Make the check advisory",
	"Best effort verification", "Relax the security gate", "Do not verify the PR head",
	"Don't verify the PR head", "Never check the PR head", "No verification needed",
	"Refuse to validate the PR head", "Skip this verification, but provide the deployment URL",
	"Leave the PR head unverified", "Proceed with the unchecked PR", "Do not enforce the security check",
])
def test_weakening_fallbacks_are_rejected(tmp_path: Path, fallback: str) -> None:
	questions = f"Q1: Which path?\n- A — Provide the PR URL for verification\n- B — {fallback}\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions)


def test_second_pass_does_not_select_weakening_option(tmp_path: Path) -> None:
	questions = "Q1: Which path?\n- A — Provide the PR URL\n- B — Turn off the security scan\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions)


@pytest.mark.parametrize("text", ["Require a shared lock", "Supply the crash details", "Require committee review"])
def test_data_keywords_do_not_match_inside_other_words(tmp_path: Path, text: str) -> None:
	questions = f"Q1: Which path?\n- A — {text}\n- B — Use default scope\n"
	assert _run_guard(tmp_path, questions=questions) == ANSWER


def test_branch_without_reliable_detector_is_not_accepted(tmp_path: Path) -> None:
	questions = "Q1: Which path?\n- A — Provide the branch name for verification\n- B — Skip this verification\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions, answer=ANSWER + "branch: feature/test\n")


def test_no_fallback_preserves_original_decision(tmp_path: Path) -> None:
	questions = "Q1: Which path?\n- A — Provide the PR URL\n- B — Supply the commit SHA\n"
	answer = ANSWER + "\nEXECUTION PLAN:\nPR: https://github.com/o/r/pull/12\n"
	assert _run_guard(tmp_path, questions=questions, answer=answer) == answer


def test_no_fallback_escalates_without_required_data(tmp_path: Path) -> None:
	questions = "Q1: Which path?\n- A — Provide the PR URL\n- B — Supply the commit SHA\n"
	result = _run_guard(tmp_path, questions=questions)
	assert "Q1: ESCALATE" in result
	assert "no safe fallback available" in result
	assert auto_decisions.from_answers(questions, result)["answers"] == "Q1: ESCALATE\n"


def test_fallback_requiring_other_unavailable_data_escalates(tmp_path: Path) -> None:
	questions = "Q1: Which path?\n- A — Provide the PR URL\n- B — Use default PR URL for verification\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions)


def test_cli_ignores_missing_evidence_and_fails_closed_on_error(tmp_path: Path) -> None:
	questions = tmp_path / "questions.txt"
	questions.write_text(QUESTIONS, encoding="utf-8")
	answers = tmp_path / "answers.txt"
	answers.write_text(ANSWER, encoding="utf-8")
	command = [sys.executable, str(GUARD_PATH), "--clarification-file", str(questions), "--answers-file", str(answers), "--evidence-file"]
	missing = subprocess.run(command + [str(tmp_path / "not-found")], capture_output=True, text=True, check=True)
	assert "Q1: ESCALATE" in missing.stdout
	evidence = tmp_path / "evidence.txt"
	evidence.write_text("https://github.com/o/r/pull/12", encoding="utf-8")
	present = subprocess.run(command + [str(evidence)], capture_output=True, text=True, check=True)
	assert present.stdout.rstrip("\n") == ANSWER.rstrip("\n")
	broken = subprocess.run(command + [str(tmp_path)], capture_output=True, text=True, check=True)
	assert "Q1: ESCALATE" in broken.stdout
	fault = subprocess.run(
		[sys.executable, str(GUARD_PATH), "--clarification-file", str(tmp_path), "--answers-file", str(answers)],
		capture_output=True, text=True, check=False,
	)
	assert fault.returncode == 1 and not fault.stdout and "refusing to post unchecked answers" in fault.stderr
	questions.unlink()
	missing_input = subprocess.run(command + [str(evidence)], capture_output=True, text=True, check=False)
	assert missing_input.returncode == 1 and not missing_input.stdout and "input files missing" in missing_input.stderr


@pytest.mark.parametrize("questions_text", ["No parsable questions", "Q1: Which path?\n- B — Skip this verification\n"])
def test_unparsed_selected_option_fails_closed(tmp_path: Path, questions_text: str) -> None:
	questions = tmp_path / "questions.txt"
	questions.write_text(questions_text, encoding="utf-8")
	answers = tmp_path / "answers.txt"
	answers.write_text(ANSWER, encoding="utf-8")
	result = subprocess.run(
		[sys.executable, str(GUARD_PATH), "--clarification-file", str(questions), "--answers-file", str(answers)],
		capture_output=True, text=True, check=False,
	)
	assert result.returncode == 1 and not result.stdout and "refusing to post unchecked answers" in result.stderr

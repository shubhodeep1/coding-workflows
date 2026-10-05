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


def test_worker_plan_alone_is_not_evidence(tmp_path: Path) -> None:
	answer = ANSWER + "\nEXECUTION PLAN:\nPR: https://github.com/o/r/pull/12\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, answer=answer)
	evidence = tmp_path / "issue_body.txt"
	evidence.write_text("PR: https://github.com/o/r/pull/12", encoding="utf-8")
	assert _run_guard(tmp_path, answer=answer, evidence_files=(evidence,)) == answer


def test_question_example_is_not_evidence(tmp_path: Path) -> None:
	questions = QUESTIONS + "Example: https://github.com/o/r/pull/12\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions)


@pytest.mark.parametrize("questions", [
	"> **Q1: How should the PR be checked?**\n> Choices:\n> - **A** — Require the PR URL and verify its head (RECOMMENDED)\n> - **B** — Skip this verification\n",
	"> **Q1**: How should the PR be checked?\n> * **A** – Require the PR URL and verify its head\n> * **B** – Skip this verification\n",
])
def test_blockquoted_questions_are_guarded(tmp_path: Path, questions: str) -> None:
	result = _run_guard(tmp_path, questions=questions)
	assert auto_decisions.from_answers(questions, result)["answers"] == "Q1: ESCALATE\n"


@pytest.mark.parametrize("answer", ["DECISIONS:\nQ1: B\n", "**DECISIONS:**\n**Q1**: **B**\n"])
def test_directly_selected_skip_escalates(tmp_path: Path, answer: str) -> None:
	result = _run_guard(tmp_path, answer=answer)
	assert auto_decisions.from_answers(QUESTIONS, result)["answers"] == "Q1: ESCALATE\n"
	assert "selected option weakens a verification/security control" in result


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


@pytest.mark.parametrize("fallback", [
	"Proceed without the URL using available information", "Use default scope",
	"Proceed without the PR URL, run local checks", "Proceed without external logs, test with unit tests",
	"Defer documentation until after merge",
])
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
	"Refuse to validate the PR head", "The test is optional", "Skip this verification, but provide the deployment URL",
	"Leave the PR head unverified", "Proceed with the unchecked PR", "Do not enforce the security check",
	"Security test skipped", "Security tests skipped", "Verification scan omitted", "Audit check disabled",
	"Test skipped", "Tests were omitted", "Unit test disabled", "Omit authn", "Proceed without authz",
	"Verification will not be performed", "Test will not be performed", "Scan shall not be performed",
	"Audit is not performed", "Security checks will not be run",
	"Verification would not be performed", "Verification should not be performed",
	"Verification must not be performed", "Verification could not be performed",
	"Verification is not going to be performed", "Security review is not being performed",
	"Verification isn't required", "Security checks aren't required", "The audit wasn't performed",
	"Verification won't run", "The review won't be verified", "Won't be verified", "The review wouldn't be performed",
	"Verification doesn't require a check", "Doesn't require verification", "Verification doesn't verify the PR",
	"Security checks don't require approval", "Checks didn't verify the PR head",
	"Verification is no longer required", "Security check is no longer needed", "PR head verification is no longer required",
	"Defer verification until after merge", "Postpone the security check until after merge",
	"Defer signature validation until later", "Verification is deferred until after merge",
	"Approval gate is postponed until after merge", "Security scan is deferred until after merge",
	"Security test deferred until after merge", "Verification has been deferred until after merge",
	"Security scans have been postponed until after merge", "Security audit had been deferred until after merge",
	"Security checks won't be required", "Security checks will not be required",
	"Verification must not be required", "Security checks would not be required",
	"Security checks shall not be required", "Security checks couldn't be required",
])
def test_weakening_fallbacks_are_rejected(tmp_path: Path, fallback: str) -> None:
	questions = f"Q1: Which path?\n- A — Provide the PR URL for verification\n- B — {fallback}\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions)
	answer = ANSWER.replace("Q1: A", "Q1: B")
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions, answer=answer)


def test_second_pass_does_not_select_weakening_option(tmp_path: Path) -> None:
	questions = "Q1: Which path?\n- A — Provide the PR URL\n- B — Turn off the security scan\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions)


@pytest.mark.parametrize("text", ["Require a shared lock", "Supply the crash details", "Require committee review", "Run curl for the endpoint", "Provide the curl command for verification"])
def test_data_keywords_do_not_match_inside_other_words(tmp_path: Path, text: str) -> None:
	questions = f"Q1: Which path?\n- A — {text}\n- B — Use default scope\n"
	assert _run_guard(tmp_path, questions=questions) == ANSWER


def test_branch_without_reliable_detector_is_not_accepted(tmp_path: Path) -> None:
	questions = "Q1: Which path?\n- A — Provide the branch name for verification\n- B — Skip this verification\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions, answer=ANSWER + "branch: feature/test\n")


def test_no_fallback_preserves_original_decision(tmp_path: Path) -> None:
	questions = "Q1: Which path?\n- A — Provide the PR URL\n- B — Supply the commit SHA\n"
	evidence = tmp_path / "issue_body.txt"
	evidence.write_text("PR: https://github.com/o/r/pull/12", encoding="utf-8")
	assert _run_guard(tmp_path, questions=questions, evidence_files=(evidence,)) == ANSWER


def test_combined_selection_does_not_drop_independent_check(tmp_path: Path) -> None:
	questions = ("Q1: Which path?\n- A — Provide the PR URL\n"
		"- B — Proceed without the URL using available information\n"
		"- C — Require signature verification\n")
	answer = ANSWER.replace("Q1: A", "Q1: A+C")
	result = _run_guard(tmp_path, questions=questions, answer=answer)
	assert auto_decisions.from_answers(questions, result)["answers"] == "Q1: ESCALATE\n"
	assert "Cannot replace a combined decision without dropping its other selected requirements" in result
	evidence = tmp_path / "issue_body.txt"
	evidence.write_text("PR: https://github.com/o/r/pull/12", encoding="utf-8")
	assert _run_guard(tmp_path, questions=questions, answer=answer, evidence_files=(evidence,)) == answer


def test_alternative_evidence_accepts_supplied_sha_or_pr_url(tmp_path: Path) -> None:
	questions = "Q1: Which path?\n- A — Provide the PR URL or commit SHA for verification\n- B — Skip verification\n"
	evidence = tmp_path / "issue_body.txt"
	for supplied in ("Commit SHA: a1b2c3d4", "PR: https://github.com/o/r/pull/12"):
		evidence.write_text(supplied, encoding="utf-8")
		assert _run_guard(tmp_path, questions=questions, evidence_files=(evidence,)) == ANSWER
	evidence.write_text("https://example.com/other", encoding="utf-8")
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions, evidence_files=(evidence,))
	questions = questions.replace(" or commit SHA", " and commit SHA")
	evidence.write_text("Commit SHA: a1b2c3d4", encoding="utf-8")
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions, evidence_files=(evidence,))


@pytest.mark.parametrize("option", [
	"Do not skip verification", "Never bypass security checks", "Do not disable the audit",
	"Check whether the feature flag is disabled", "Validate optional fields against schema",
	"Test optional parameters", "Verify the debug toggle is disabled", "Omit author metadata",
	"Proceed without author attribution", "Do not enforce author attribution",
	"Don't skip verification", "Doesn't skip verification", "Won't skip checks", "Security checks shouldn't be skipped",
	"Security checks mustn't be omitted", "Verification isn't optional",
	"Check whether the feature flag isn't disabled",
	"Do not defer verification", "Never postpone the security scan",
])
def test_control_preserving_and_diagnostic_options_remain_available(tmp_path: Path, option: str) -> None:
	questions = f"Q1: Which path?\n- A — Provide the PR URL\n- B — {option}\n"
	answer = ANSWER.replace("Q1: A", "Q1: B")
	assert _run_guard(tmp_path, questions=questions, answer=answer) == answer
	assert "Q1: B" in _run_guard(tmp_path, questions=questions)


def test_negation_does_not_mask_a_later_bypass(tmp_path: Path) -> None:
	questions = "Q1: Which path?\n- A — Do not skip verification, but disable the security scan\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions)
	questions = "Q1: Which path?\n- A — Do not defer verification, but skip the security scan\n"
	assert "Q1: ESCALATE" in _run_guard(tmp_path, questions=questions)


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

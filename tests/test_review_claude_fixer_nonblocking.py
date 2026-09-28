"""Tests for scripts/review_claude_fixer_nonblocking.py (issue #4586).

A consensus finding raised by exactly one reviewer and explicitly rejected
(REJECTED_FINDING lines in the raw pass-2 outputs) by a strict majority of
the other successful reviewers, at least two, moves to a NON-BLOCKING
FINDINGS block. Everything else stays blocking.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "review_claude_fixer_nonblocking.py"

_spec = importlib.util.spec_from_file_location("review_claude_fixer_nonblocking", SCRIPT)
nonblocking = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nonblocking)

FLAGGER = "google_gemini-3_1-flash-lite"
OTHERS = ["deepseek_v4", "minimax_m3", "qwen_q4", "x-ai_grok-5", "z-ai_glm-6"]


def _ledger(consensus: str, per_reviewer: dict[str, str] | None = None, task_gaps: str = "(No task gaps reported.)") -> str:
	per_reviewer = per_reviewer or {}
	sections = []
	for slug in [FLAGGER, *OTHERS]:
		body = per_reviewer.get(slug, "(No findings reported.)")
		sections.append(f"=== FINDINGS FROM {slug} ===\n{body}\n=== END FINDINGS FROM {slug} ===")
	return (
		f"=== CONSENSUS FINDINGS ===\n{consensus}\n=== END CONSENSUS FINDINGS ===\n\n"
		f"=== CONSENSUS TASK GAPS ===\n{task_gaps}\n=== END CONSENSUS TASK GAPS ===\n\n"
		+ "\n\n".join(sections) + "\n"
	)


README_FINDING = (
	"- README.md:1261 | severity=critical | confidence=[5]\n"
	f"  flagged_by: [{FLAGGER}]\n"
	"  PROBLEM: The command /implement-issue-claude lost its leading backtick formatting during rewrapping.\n"
	"  WHY: Other reviewers found the backtick present on README.md:1262."
)
README_BULLET = (
	"- README.md:1261 | severity=critical | confidence=5\n"
	"  PROBLEM: The command lost its leading backtick.\n"
	"  WHY: Rewrap."
)
REAL_FINDING = (
	"- scripts/foo.sh:40-44 | severity=high | confidence=[4]\n"
	f"  flagged_by: [{OTHERS[0]}]\n"
	"  PROBLEM: Unquoted variable splits paths with spaces.\n"
	"  WHY: word splitting."
)


def _reviews(tmp: Path, *, rejecters: list[str], line: str = "1262", flagger: str = FLAGGER, path: str = "README.md",
		statuses: dict[str, str] | None = None) -> Path:
	reviews = tmp / "previous_reviews"
	reviews.mkdir()
	statuses = statuses or {}
	for slug in [FLAGGER, *OTHERS]:
		(reviews / f"status_review_{slug}.txt").write_text(statuses.get(slug, "success") + "\n", encoding="utf-8")
		text = "File: README.md\nProblem: something else\n" if slug == FLAGGER else "No issues found.\n"
		if slug in rejecters:
			text += f"REJECTED_FINDING: {path}:{line} | flagged_by: {flagger} | reason: the backtick is present.\n"
		(reviews / f"review_{slug}.txt").write_text(text, encoding="utf-8")
	return reviews


def _run(tmp: Path, ledger: str, reviews: Path) -> tuple[str, list[dict]]:
	return nonblocking.demote(ledger, reviews)


def test_demotes_the_4575_shape(tmp_path):
	ledger = _ledger(README_FINDING, {FLAGGER: README_BULLET})
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert len(demoted) == 1
	assert demoted[0]["rejecters"] == sorted(OTHERS)
	consensus = text.split("=== CONSENSUS FINDINGS ===\n", 1)[1].split("\n=== END CONSENSUS FINDINGS ===", 1)[0]
	assert consensus == "(No findings reported.)"
	flagger_section = text.split(f"=== FINDINGS FROM {FLAGGER} ===\n", 1)[1].split("\n=== END", 1)[0]
	assert flagger_section == "(No findings reported.)"
	block = text.split("=== NON-BLOCKING FINDINGS ===\n", 1)[1].split("=== END NON-BLOCKING FINDINGS ===", 1)[0]
	assert "- README.md:1261 | severity=critical" in block
	assert f"rejected_by: [{', '.join(sorted(OTHERS))}] (5 of 5 other reviewers)" in block
	assert f"moved from FINDINGS FROM {FLAGGER}:" in block
	# The block sits right after the task gaps, and every other block is intact.
	assert text.index("=== END CONSENSUS TASK GAPS ===") < text.index("=== NON-BLOCKING FINDINGS ===") < text.index("=== FINDINGS FROM ")


def test_nothing_demoted_returns_the_ledger_byte_for_byte(tmp_path):
	ledger = _ledger(README_FINDING, {FLAGGER: README_BULLET})
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=[]))
	assert demoted == [] and text == ledger


@pytest.mark.parametrize("rejecters", [OTHERS[:2], OTHERS[:1]])
def test_minority_rejection_keeps_the_finding_blocking(tmp_path, rejecters):
	ledger = _ledger(README_FINDING)
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=rejecters))
	assert demoted == [] and text == ledger


def test_three_of_five_is_a_majority(tmp_path):
	_text, demoted = _run(tmp_path, _ledger(README_FINDING), _reviews(tmp_path, rejecters=OTHERS[:3]))
	assert len(demoted) == 1 and demoted[0]["others"] == 5


def test_one_rejecter_is_never_enough_even_as_a_majority(tmp_path):
	# Two successful reviewers: the flagger and one other who rejects it.
	statuses = {slug: "failed" for slug in OTHERS[1:]}
	ledger = _ledger(README_FINDING)
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS[:1], statuses=statuses))
	assert demoted == [] and text == ledger


def test_failed_reviewers_neither_reject_nor_count(tmp_path):
	# Five others reject, but three of them did not succeed: 2 of 2 successful others.
	statuses = {slug: "failed" for slug in OTHERS[2:]}
	_text, demoted = _run(tmp_path, _ledger(README_FINDING), _reviews(tmp_path, rejecters=OTHERS, statuses=statuses))
	assert len(demoted) == 1 and demoted[0]["rejecters"] == sorted(OTHERS[:2]) and demoted[0]["others"] == 2


def test_missing_status_file_does_not_count(tmp_path):
	reviews = _reviews(tmp_path, rejecters=OTHERS)
	for slug in OTHERS[:3]:
		(reviews / f"status_review_{slug}.txt").unlink()
	_text, demoted = _run(tmp_path, _ledger(README_FINDING), reviews)
	assert len(demoted) == 1 and demoted[0]["others"] == 2


def test_the_flagger_cannot_reject_its_own_finding(tmp_path):
	reviews = _reviews(tmp_path, rejecters=OTHERS[:2])
	(reviews / f"review_{FLAGGER}.txt").write_text(
		f"REJECTED_FINDING: README.md:1261 | flagged_by: {FLAGGER} | reason: changed my mind\n", encoding="utf-8")
	ledger = _ledger(README_FINDING)
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger


@pytest.mark.parametrize("kwargs", [
	{"flagger": OTHERS[0]},     # names another flagger
	{"line": "1300"},            # far from the finding
	{"path": "docs/README.md"},  # another file
])
def test_rejection_must_match_file_line_and_flagger(tmp_path, kwargs):
	ledger = _ledger(README_FINDING)
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS, **kwargs))
	assert demoted == [] and text == ledger


@pytest.mark.parametrize(("line", "expected"), [("1264", 1), ("1258", 1), ("1265", 0), ("1257", 0), ("1250-1258", 1), ("1262-1270", 1)])
def test_line_tolerance_is_three(tmp_path, line, expected):
	_text, demoted = _run(tmp_path, _ledger(README_FINDING), _reviews(tmp_path, rejecters=OTHERS, line=line))
	assert len(demoted) == expected


def test_multi_reviewer_findings_are_never_demoted(tmp_path):
	finding = README_FINDING.replace(f"flagged_by: [{FLAGGER}]", f"flagged_by: [{FLAGGER}, {OTHERS[4]}]")
	ledger = _ledger(finding)
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS[:4]))
	assert demoted == [] and text == ledger


def test_task_gaps_are_never_demoted(tmp_path):
	gap = (
		"- requirement: README.md:1261 must keep the backtick\n"
		"  expected_change_site: README.md\n"
		f"  flagged_by: [{FLAGGER}]"
	)
	ledger = _ledger("(No findings reported.)", task_gaps=gap)
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert demoted == [] and text == ledger


def test_unparseable_entry_stays_blocking(tmp_path):
	finding = README_FINDING.replace("- README.md:1261 |", "- README.md (around the pickup section) |")
	ledger = _ledger(finding)
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert demoted == [] and text == ledger


def test_only_the_rejected_entry_moves(tmp_path):
	ledger = _ledger(README_FINDING + "\n" + REAL_FINDING, {
		FLAGGER: README_BULLET,
		OTHERS[0]: "- scripts/foo.sh:40-44 | severity=high | confidence=4\n  PROBLEM: Unquoted variable.",
	})
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert len(demoted) == 1
	consensus = text.split("=== CONSENSUS FINDINGS ===\n", 1)[1].split("\n=== END CONSENSUS FINDINGS ===", 1)[0]
	assert consensus.startswith("- scripts/foo.sh:40-44") and "README.md" not in consensus
	own = text.split(f"=== FINDINGS FROM {OTHERS[0]} ===\n", 1)[1].split("\n=== END", 1)[0]
	assert own.startswith("- scripts/foo.sh:40-44")


def test_rejection_bullets_the_summariser_left_in_a_rejecters_section_move_too(tmp_path):
	stray = "- README.md:1262 | severity=low\n  PROBLEM: REJECTED_FINDING — backtick is present."
	ledger = _ledger(README_FINDING, {FLAGGER: README_BULLET, OTHERS[1]: stray})
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert len(demoted) == 1
	section = text.split(f"=== FINDINGS FROM {OTHERS[1]} ===\n", 1)[1].split("\n=== END", 1)[0]
	assert section == "(No findings reported.)"
	assert f"moved from FINDINGS FROM {OTHERS[1]}:" in text


def test_filtered_ledger_passes_the_handoff_clean_check(tmp_path):
	"""The rewritten ledger must satisfy the hand-off step's own awk checks."""
	ledger = _ledger(README_FINDING, {FLAGGER: README_BULLET})
	text, _demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	path = tmp_path / "filtered.txt"
	path.write_text(text, encoding="utf-8")
	count = subprocess.run(["awk", r'''
		/^=== (CONSENSUS FINDINGS|CONSENSUS TASK GAPS|FINDINGS FROM .*) ===$/ { in_block = 1; next }
		/^=== END / { in_block = 0; next }
		in_block && /^- / { total++ }
		END { printf "%d\n", total + 0 }''', str(path)], capture_output=True, text=True, check=True).stdout.strip()
	assert count == "0"
	clean = subprocess.run(["awk", r'''
		/^=== CONSENSUS FINDINGS ===$/ { expected = "(No findings reported.)"; block = 1; entries = 0; next }
		/^=== CONSENSUS TASK GAPS ===$/ { expected = "(No task gaps reported.)"; block = 1; entries = 0; next }
		/^=== FINDINGS FROM .+ ===$/ { expected = "(No findings reported.)"; block = 1; entries = 0; next }
		/^=== END / { if (block) { if (entries != 1) invalid = 1; blocks++; block = 0 }; next }
		block && NF { entries++; if ($0 != expected) invalid = 1 }
		END { if (invalid || block || blocks < 3) exit 1 }''', str(path)], capture_output=True, text=True)
	assert clean.returncode == 0


@pytest.mark.parametrize("bad", [
	"=== CONSENSUS FINDINGS ===\n- a.py:1 | x\n",                                   # unclosed block
	"=== CONSENSUS FINDINGS ===\n(No findings reported.)\n=== END CONSENSUS FINDINGS ===\n",  # no task gaps
	_ledger(README_FINDING).replace("=== END CONSENSUS TASK GAPS ===",
		"=== END CONSENSUS TASK GAPS ===\n=== NON-BLOCKING FINDINGS ===\n=== END NON-BLOCKING FINDINGS ==="),
])
def test_malformed_ledgers_raise(tmp_path, bad):
	with pytest.raises(ValueError):
		_run(tmp_path, bad, _reviews(tmp_path, rejecters=OTHERS))


def test_cli_writes_output_and_log_lines(tmp_path):
	ledger_path = tmp_path / "ledger.txt"
	ledger_path.write_text(_ledger(README_FINDING, {FLAGGER: README_BULLET}), encoding="utf-8")
	out = tmp_path / "out.txt"
	reviews = _reviews(tmp_path, rejecters=OTHERS)
	proc = subprocess.run([sys.executable, str(SCRIPT), "--ledger", str(ledger_path), "--reviews-dir", str(reviews), "--output", str(out)],
		capture_output=True, text=True, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.splitlines()[0] == "CLAUDE_FIXER_NONBLOCKING demoted=1 successful_reviewers=6"
	assert f"CLAUDE_FIXER_NONBLOCKING_ENTRY file=README.md:1261 flagged_by={FLAGGER}" in proc.stdout
	assert "=== NON-BLOCKING FINDINGS ===" in out.read_text()


def test_cli_fails_on_a_missing_reviews_dir(tmp_path):
	ledger_path = tmp_path / "ledger.txt"
	ledger_path.write_text(_ledger(README_FINDING), encoding="utf-8")
	proc = subprocess.run([sys.executable, str(SCRIPT), "--ledger", str(ledger_path), "--reviews-dir", str(tmp_path / "nope"),
		"--output", str(tmp_path / "out.txt")], capture_output=True, text=True, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
	assert proc.returncode == 1 and "CLAUDE_FIXER_NONBLOCKING error=" in proc.stderr
	assert not (tmp_path / "out.txt").exists()

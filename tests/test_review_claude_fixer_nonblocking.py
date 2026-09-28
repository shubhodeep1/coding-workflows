"""Tests for scripts/review_claude_fixer_nonblocking.py (issues #4586, #4687).

A consensus finding raised by exactly one reviewer and explicitly rejected
(REJECTED_FINDING lines in the raw pass-2 outputs) by a strict majority of
the other successful reviewers, at least two, moves to a NON-BLOCKING
FINDINGS block. Since #4687 a rejection is bound to the finding by the
pass-1 consensus_id it cites, never by file and line proximity, and every
ambiguous match stays blocking. Everything else stays blocking.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "review_claude_fixer_nonblocking.py"
RUNNER = REPO_ROOT / "scripts" / "review_run_reviewers.sh"

_spec = importlib.util.spec_from_file_location("review_claude_fixer_nonblocking", SCRIPT)
nonblocking = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nonblocking)

FLAGGER = "google_gemini-3_1-flash-lite"
OTHERS = ["deepseek_v4", "minimax_m3", "qwen_q4", "x-ai_grok-5", "z-ai_glm-6"]
ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}


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


def _cid(entry: str) -> str:
	return nonblocking.consensus_id(entry.splitlines())


def _with_id(entry: str, cid: str) -> str:
	header, rest = entry.split("\n", 1)
	return f"{header}\n  consensus_id: {cid}\n{rest}"


# Pass 1: what the pass-2 reviewers saw in the cross-pollination summary.
PASS1_README = (
	"- README.md:1261 | severity=critical | confidence=[5]\n"
	f"  flagged_by: [{FLAGGER}]\n"
	"  PROBLEM: The command /implement-issue-claude lost its leading backtick formatting during rewrapping.\n"
	"  WHY: The rewrap moved the backtick."
)
RID = _cid(PASS1_README)
# A distinct, real defect the same reviewer raised three lines further down.
PASS1_FLAW = (
	"- README.md:1264 | severity=high | confidence=[4]\n"
	f"  flagged_by: [{FLAGGER}]\n"
	"  PROBLEM: The example command interpolates an untrusted issue title into a shell line.\n"
	"  WHY: command injection."
)
FLAW_ID = _cid(PASS1_FLAW)

# Pass 2: the ledger the hand-off step counts.
README_FINDING = _with_id(
	"- README.md:1261 | severity=critical | confidence=[5]\n"
	f"  flagged_by: [{FLAGGER}]\n"
	"  PROBLEM: The command /implement-issue-claude lost its leading backtick formatting during rewrapping.\n"
	"  WHY: Other reviewers found the backtick present on README.md:1262.", RID)
README_BULLET = _with_id(
	"- README.md:1261 | severity=critical | confidence=5\n"
	"  PROBLEM: The command lost its leading backtick.\n"
	"  WHY: Rewrap.", RID)
FLAW_FINDING = (
	"- README.md:1264 | severity=high | confidence=[4]\n"
	f"  flagged_by: [{FLAGGER}]\n"
	"  PROBLEM: The example command interpolates an untrusted issue title into a shell line.\n"
	"  WHY: command injection."
)
REAL_FINDING = (
	"- scripts/foo.sh:40-44 | severity=high | confidence=[4]\n"
	f"  flagged_by: [{OTHERS[0]}]\n"
	"  PROBLEM: Unquoted variable splits paths with spaces.\n"
	"  WHY: word splitting."
)


def _reviews(tmp: Path, *, rejecters: list[str], line: str = "1261", flagger: str = FLAGGER, path: str = "README.md",
		cid: str | None = RID, statuses: dict[str, str] | None = None, pass1: str | None = PASS1_README,
		flagger_output: str | None = None) -> Path:
	reviews = tmp / "previous_reviews"
	reviews.mkdir()
	statuses = statuses or {}
	if pass1 is not None:
		(reviews / "consensus_pass1.txt").write_text(_ledger(pass1), encoding="utf-8")
	for slug in [FLAGGER, *OTHERS]:
		(reviews / f"status_review_{slug}.txt").write_text(statuses.get(slug, "success") + "\n", encoding="utf-8")
		if slug == FLAGGER:
			text = flagger_output if flagger_output is not None else (
				f"File: README.md\nLine or code reference: 1261\nconsensus_id: {RID}\nProblem: lost its backtick\n")
		else:
			text = "No issues found.\n"
		if slug in rejecters:
			prefix = f"{cid} | " if cid else ""
			text += f"REJECTED_FINDING: {prefix}{path}:{line} | flagged_by: {flagger} | reason: the backtick is present.\n"
		(reviews / f"review_{slug}.txt").write_text(text, encoding="utf-8")
	return reviews


def _run(tmp: Path, ledger: str, reviews: Path) -> tuple[str, list[dict]]:
	return nonblocking.demote(ledger, reviews)


def _kept(ledger: str, reviews: Path) -> dict[str, str]:
	_text, _demoted, kept, _legacy = nonblocking.demote_with_diagnostics(ledger, reviews)
	return {f"{record['location'][0]}:{record['location'][1][0]}" if record["location"] else "unparsed": record["reason"]
		for record in kept}


def _block(text: str, name: str) -> str:
	return text.split(f"=== {name} ===\n", 1)[1].split(f"\n=== END {name} ===", 1)[0]


def test_consensus_id_is_a_truncated_sha256_of_the_entry():
	expected = "p1-" + hashlib.sha256(PASS1_README.encode()).hexdigest()[:12]
	assert RID == expected
	assert re.fullmatch(r"p1-[0-9a-f]{12}", RID)
	# An id line and trailing blank lines never change the id.
	assert _cid(_with_id(PASS1_README, "p1-000000000000") + "\n\n") == RID


def test_demotes_the_4575_shape(tmp_path):
	ledger = _ledger(README_FINDING, {FLAGGER: README_BULLET})
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert len(demoted) == 1
	assert demoted[0]["rejecters"] == sorted(OTHERS)
	assert demoted[0]["consensus_id"] == RID
	assert _block(text, "CONSENSUS FINDINGS") == "(No findings reported.)"
	assert _block(text, f"FINDINGS FROM {FLAGGER}") == "(No findings reported.)"
	block = _block(text, "NON-BLOCKING FINDINGS")
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
	reviews = _reviews(tmp_path, rejecters=rejecters)
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"README.md:1261": "too_few_rejecters"}


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


def test_a_failed_flagger_keeps_its_finding_blocking(tmp_path):
	ledger = _ledger(README_FINDING)
	reviews = _reviews(tmp_path, rejecters=OTHERS, statuses={FLAGGER: "failed"})
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"README.md:1261": "flagger_not_successful"}


def test_the_flagger_cannot_reject_its_own_finding(tmp_path):
	reviews = _reviews(tmp_path, rejecters=OTHERS[:2])
	(reviews / f"review_{FLAGGER}.txt").write_text(
		f"consensus_id: {RID}\nREJECTED_FINDING: {RID} | README.md:1261 | flagged_by: {FLAGGER} | reason: changed my mind\n",
		encoding="utf-8")
	ledger = _ledger(README_FINDING)
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger


@pytest.mark.parametrize("kwargs", [
	{"flagger": OTHERS[0]},          # names another flagger
	{"line": "1300"},                 # far from the finding
	{"line": "1262"},                 # next line: no longer close enough (#4687)
	{"line": "1258-1260"},            # adjacent range that does not overlap
	{"path": "docs/README.md"},       # another file
	{"cid": "p1-000000000000"},       # an id no entry carries
	{"cid": FLAW_ID},                 # the id of a different finding
])
def test_rejection_must_cite_the_id_and_agree_with_the_entry(tmp_path, kwargs):
	ledger = _ledger(README_FINDING)
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS, **kwargs))
	assert demoted == [] and text == ledger


@pytest.mark.parametrize("line", ["1261", "1260-1262", "1255-1261"])
def test_an_overlapping_range_with_the_right_id_counts(tmp_path, line):
	_text, demoted = _run(tmp_path, _ledger(README_FINDING), _reviews(tmp_path, rejecters=OTHERS, line=line))
	assert len(demoted) == 1


def test_id_less_rejections_are_ignored_and_counted(tmp_path):
	ledger = _ledger(README_FINDING)
	reviews = _reviews(tmp_path, rejecters=OTHERS, cid=None)
	text, demoted, kept, legacy = nonblocking.demote_with_diagnostics(ledger, reviews)
	assert demoted == [] and text == ledger
	assert legacy == 5
	assert [record["reason"] for record in kept] == ["too_few_rejecters"]


# ── Issue #4687: a rejection must never demote a distinct nearby finding ──


def test_issue_4687_rejections_of_a_false_positive_do_not_demote_a_nearby_flaw(tmp_path):
	"""Same flagger, a false positive and a real flaw three lines apart, both in both passes."""
	flaw = _with_id(FLAW_FINDING, FLAW_ID)
	ledger = _ledger(README_FINDING + "\n" + flaw)
	reviews = _reviews(tmp_path, rejecters=OTHERS, pass1=PASS1_README + "\n" + PASS1_FLAW,
		flagger_output=f"consensus_id: {RID}\nconsensus_id: {FLAW_ID}\n")
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	# The flaw has no rejection; the rejected entry is ambiguous next to it.
	assert _kept(ledger, reviews) == {"README.md:1261": "ambiguous_nearby_pass1", "README.md:1264": "ambiguous_nearby_pass1"}


def test_issue_4687_flaw_raised_next_to_a_dropped_false_positive_stays_blocking(tmp_path):
	"""The flagger drops the rejected entry in pass 2 and raises a new defect beside it."""
	ledger = _ledger(FLAW_FINDING)
	reviews = _reviews(tmp_path, rejecters=OTHERS, flagger_output="File: README.md\nProblem: injection\n")
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"README.md:1264": "no_consensus_id"}


def test_issue_4687_an_id_copied_onto_a_different_line_is_a_mismatch(tmp_path):
	"""The summariser attaches the rejected entry's id to the nearby flaw."""
	ledger = _ledger(_with_id(FLAW_FINDING, RID))
	reviews = _reviews(tmp_path, rejecters=OTHERS)
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"README.md:1264": "consensus_id_mismatch"}


def test_issue_4687_the_flagger_must_cite_the_id_itself(tmp_path):
	"""An id the flagger never wrote (the summariser took it from a rejection) binds nothing."""
	ledger = _ledger(README_FINDING)
	reviews = _reviews(tmp_path, rejecters=OTHERS, flagger_output="File: README.md\nProblem: an injection on this line\n")
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"README.md:1261": "flagger_did_not_cite"}


def test_issue_4687_another_pass2_entry_nearby_keeps_the_match_ambiguous(tmp_path):
	other = (
		"- README.md:1263 | severity=high | confidence=[4]\n"
		f"  flagged_by: [{OTHERS[0]}]\n"
		"  PROBLEM: Unescaped pipe breaks the table.\n"
		"  WHY: rendering."
	)
	ledger = _ledger(README_FINDING + "\n" + other)
	reviews = _reviews(tmp_path, rejecters=OTHERS[1:])
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"README.md:1261": "ambiguous_nearby", "README.md:1263": "no_consensus_id"}


def test_issue_4687_a_distant_finding_in_the_same_file_is_not_ambiguous(tmp_path):
	far = FLAW_FINDING.replace("README.md:1264", "README.md:1400")
	ledger = _ledger(README_FINDING + "\n" + far)
	_text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert [record["path"] + ":" + str(record["lines"][0]) for record in demoted] == ["README.md:1261"]


def test_a_duplicated_id_in_the_ledger_stays_blocking(tmp_path):
	copy = README_FINDING.replace("README.md:1261", "README.md:1261-1261")
	ledger = _ledger(README_FINDING + "\n" + copy)
	reviews = _reviews(tmp_path, rejecters=OTHERS)
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert set(_kept(ledger, reviews).values()) == {"duplicate_consensus_id"}


def test_a_duplicated_pass1_entry_binds_nothing(tmp_path):
	ledger = _ledger(README_FINDING)
	reviews = _reviews(tmp_path, rejecters=OTHERS, pass1=PASS1_README + "\n" + PASS1_README)
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"README.md:1261": "duplicate_consensus_id"}


def test_two_id_lines_on_one_entry_stay_blocking(tmp_path):
	ledger = _ledger(_with_id(README_FINDING, FLAW_ID))
	reviews = _reviews(tmp_path, rejecters=OTHERS)
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"README.md:1261": "multiple_consensus_ids"}


def test_without_a_pass1_ledger_nothing_is_demoted(tmp_path):
	ledger = _ledger(README_FINDING)
	reviews = _reviews(tmp_path, rejecters=OTHERS, pass1=None)
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"README.md:1261": "unknown_consensus_id"}


def test_a_pass1_entry_with_another_flagger_is_a_mismatch(tmp_path):
	pass1 = PASS1_README.replace(f"flagged_by: [{FLAGGER}]", f"flagged_by: [{FLAGGER}, {OTHERS[0]}]")
	cid = _cid(pass1)
	ledger = _ledger(_with_id(README_FINDING.replace(f"  consensus_id: {RID}\n", ""), cid))
	reviews = _reviews(tmp_path, rejecters=OTHERS[1:], cid=cid, pass1=pass1, flagger_output=f"consensus_id: {cid}\n")
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"README.md:1261": "consensus_id_mismatch"}


def test_multi_reviewer_findings_are_never_demoted(tmp_path):
	finding = README_FINDING.replace(f"flagged_by: [{FLAGGER}]", f"flagged_by: [{FLAGGER}, {OTHERS[4]}]")
	ledger = _ledger(finding)
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS[:4]))
	assert demoted == [] and text == ledger


def test_task_gaps_are_never_demoted(tmp_path):
	gap = (
		"- requirement: README.md:1261 must keep the backtick\n"
		f"  consensus_id: {RID}\n"
		"  expected_change_site: README.md\n"
		f"  flagged_by: [{FLAGGER}]"
	)
	ledger = _ledger("(No findings reported.)", task_gaps=gap)
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert demoted == [] and text == ledger


def test_unparseable_entry_stays_blocking(tmp_path):
	finding = README_FINDING.replace("- README.md:1261 |", "- README.md (around the pickup section) |")
	ledger = _ledger(finding)
	reviews = _reviews(tmp_path, rejecters=OTHERS)
	text, demoted = _run(tmp_path, ledger, reviews)
	assert demoted == [] and text == ledger
	assert _kept(ledger, reviews) == {"unparsed": "unparsed"}


def test_only_the_rejected_entry_moves(tmp_path):
	ledger = _ledger(README_FINDING + "\n" + REAL_FINDING, {
		FLAGGER: README_BULLET,
		OTHERS[0]: "- scripts/foo.sh:40-44 | severity=high | confidence=4\n  PROBLEM: Unquoted variable.",
	})
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert len(demoted) == 1
	consensus = _block(text, "CONSENSUS FINDINGS")
	assert consensus.startswith("- scripts/foo.sh:40-44") and "README.md" not in consensus
	assert _block(text, f"FINDINGS FROM {OTHERS[0]}").startswith("- scripts/foo.sh:40-44")


def test_rejection_bullets_the_summariser_left_in_a_rejecters_section_move_when_they_carry_the_id(tmp_path):
	stray = f"- README.md:1261 | severity=low\n  PROBLEM: REJECTED_FINDING {RID} — backtick is present."
	ledger = _ledger(README_FINDING, {FLAGGER: README_BULLET, OTHERS[1]: stray})
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert len(demoted) == 1
	assert _block(text, f"FINDINGS FROM {OTHERS[1]}") == "(No findings reported.)"
	assert f"moved from FINDINGS FROM {OTHERS[1]}:" in text


def test_bullets_without_the_id_or_off_the_range_stay_blocking(tmp_path):
	"""Only a bullet that overlaps the entry and carries its id moves (#4687)."""
	no_id = "- README.md:1261 | severity=high\n  PROBLEM: A different defect on the same line."
	nearby = _with_id("- README.md:1263 | severity=high\n  PROBLEM: A nearby defect.", RID)
	ledger = _ledger(README_FINDING, {FLAGGER: README_BULLET + "\n" + no_id + "\n" + nearby})
	text, demoted = _run(tmp_path, ledger, _reviews(tmp_path, rejecters=OTHERS))
	assert len(demoted) == 1
	section = _block(text, f"FINDINGS FROM {FLAGGER}")
	assert "A different defect on the same line." in section and "A nearby defect." in section
	assert "The command lost its leading backtick." not in section


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


def test_a_malformed_pass1_ledger_raises(tmp_path):
	reviews = _reviews(tmp_path, rejecters=OTHERS)
	(reviews / "consensus_pass1.txt").write_text("=== CONSENSUS FINDINGS ===\n- a.py:1 | x\n", encoding="utf-8")
	with pytest.raises(ValueError):
		_run(tmp_path, _ledger(README_FINDING), reviews)


def test_cli_writes_output_and_log_lines(tmp_path):
	ledger_path = tmp_path / "ledger.txt"
	ledger_path.write_text(_ledger(README_FINDING + "\n" + FLAW_FINDING.replace("1264", "1400"), {FLAGGER: README_BULLET}),
		encoding="utf-8")
	out = tmp_path / "out.txt"
	reviews = _reviews(tmp_path, rejecters=OTHERS)
	(reviews / f"review_{OTHERS[0]}.txt").write_text(
		(reviews / f"review_{OTHERS[0]}.txt").read_text() + f"REJECTED_FINDING: README.md:1400 | flagged_by: {FLAGGER} | reason: x\n")
	proc = subprocess.run([sys.executable, str(SCRIPT), "--ledger", str(ledger_path), "--reviews-dir", str(reviews), "--output", str(out)],
		capture_output=True, text=True, env=ENV)
	assert proc.returncode == 0, proc.stderr
	lines = proc.stdout.splitlines()
	assert lines[0] == "CLAUDE_FIXER_NONBLOCKING demoted=1 successful_reviewers=6"
	assert f"CLAUDE_FIXER_NONBLOCKING_ENTRY file=README.md:1261 flagged_by={FLAGGER}" in proc.stdout
	assert f"CLAUDE_FIXER_NONBLOCKING_KEPT file=README.md:1400 flagged_by={FLAGGER} reason=no_consensus_id" in lines
	assert "CLAUDE_FIXER_NONBLOCKING_LEGACY_REJECTIONS count=1" in lines
	assert "=== NON-BLOCKING FINDINGS ===" in out.read_text()


def test_each_reviewer_output_is_read_once_and_statuses_scanned_once(tmp_path, monkeypatch, capsys):
	"""PR #4695 review round 1: no repeated reads of review_<slug>.txt or status_review_*.txt."""
	ledger_path = tmp_path / "ledger.txt"
	ledger_path.write_text(_ledger(README_FINDING), encoding="utf-8")
	reviews = _reviews(tmp_path, rejecters=OTHERS)
	reads: list[str] = []
	real_read_text = Path.read_text

	def counting_read_text(self, *args, **kwargs):
		reads.append(self.name)
		return real_read_text(self, *args, **kwargs)

	scans: list[Path] = []
	real_successful_reviewers = nonblocking.successful_reviewers

	def counting_successful_reviewers(reviews_dir):
		scans.append(reviews_dir)
		return real_successful_reviewers(reviews_dir)

	monkeypatch.setattr(Path, "read_text", counting_read_text)
	monkeypatch.setattr(nonblocking, "successful_reviewers", counting_successful_reviewers)
	assert nonblocking.main(["--ledger", str(ledger_path), "--reviews-dir", str(reviews), "--output", str(tmp_path / "out.txt")]) == 0
	assert scans == [reviews]
	output_reads = [name for name in reads if name.startswith("review_")]
	assert sorted(output_reads) == sorted(f"review_{slug}.txt" for slug in [FLAGGER, *OTHERS])
	assert sum(name.startswith("status_review_") for name in reads) == 6
	assert capsys.readouterr().out.splitlines()[0] == "CLAUDE_FIXER_NONBLOCKING demoted=1 successful_reviewers=6"


def test_cli_fails_on_a_missing_reviews_dir(tmp_path):
	ledger_path = tmp_path / "ledger.txt"
	ledger_path.write_text(_ledger(README_FINDING), encoding="utf-8")
	proc = subprocess.run([sys.executable, str(SCRIPT), "--ledger", str(ledger_path), "--reviews-dir", str(tmp_path / "nope"),
		"--output", str(tmp_path / "out.txt")], capture_output=True, text=True, env=ENV)
	assert proc.returncode == 1 and "CLAUDE_FIXER_NONBLOCKING error=" in proc.stderr
	assert not (tmp_path / "out.txt").exists()


def test_cli_requires_a_reviews_dir_to_demote(tmp_path):
	ledger_path = tmp_path / "ledger.txt"
	ledger_path.write_text(_ledger(README_FINDING), encoding="utf-8")
	proc = subprocess.run([sys.executable, str(SCRIPT), "--ledger", str(ledger_path), "--output", str(tmp_path / "out.txt")],
		capture_output=True, text=True, env=ENV)
	assert proc.returncode == 2 and "--reviews-dir is required" in proc.stderr
	assert not (tmp_path / "out.txt").exists()


# ── --annotate: the ids pass-2 reviewers see ──


def test_annotate_adds_the_id_after_each_consensus_header_only():
	pass1 = _ledger(PASS1_README + "\n" + PASS1_FLAW, {FLAGGER: README_BULLET.replace(f"  consensus_id: {RID}\n", "")},
		task_gaps="- requirement: keep it\n  flagged_by: [x]")
	annotated, count = nonblocking.annotate(pass1)
	assert count == 2
	consensus = _block(annotated, "CONSENSUS FINDINGS").splitlines()
	assert consensus[:2] == ["- README.md:1261 | severity=critical | confidence=[5]", f"  consensus_id: {RID}"]
	assert f"  consensus_id: {FLAW_ID}" in consensus
	assert "consensus_id" not in _block(annotated, "CONSENSUS TASK GAPS")
	assert "consensus_id" not in _block(annotated, f"FINDINGS FROM {FLAGGER}")
	# Removing the id lines gives back the input, and annotating again changes nothing.
	assert re.sub(r"  consensus_id: p1-[0-9a-f]{12}\n", "", annotated) == pass1
	assert nonblocking.annotate(annotated) == (annotated, 2)


def test_annotated_ids_are_the_ids_the_demoter_computes(tmp_path):
	reviews = _reviews(tmp_path, rejecters=OTHERS)
	annotated, _count = nonblocking.annotate((reviews / "consensus_pass1.txt").read_text())
	shown = re.findall(r"consensus_id: (p1-[0-9a-f]{12})", annotated)
	assert shown == [record["consensus_id"] for record in nonblocking.pass1_consensus_entries(reviews)] == [RID]


def test_annotate_rejects_a_ledger_without_consensus_findings():
	with pytest.raises(ValueError):
		nonblocking.annotate("=== FINDINGS FROM x ===\n(No findings reported.)\n=== END FINDINGS FROM x ===\n")


def test_cli_annotate(tmp_path):
	ledger_path = tmp_path / "consensus_pass1.txt"
	ledger_path.write_text(_ledger(PASS1_README), encoding="utf-8")
	out = tmp_path / "annotated.txt"
	proc = subprocess.run([sys.executable, str(SCRIPT), "--annotate", "--ledger", str(ledger_path), "--output", str(out)],
		capture_output=True, text=True, env=ENV)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.strip() == "CLAUDE_FIXER_CONSENSUS_IDS annotated=1"
	assert f"  consensus_id: {RID}" in out.read_text()
	missing = subprocess.run([sys.executable, str(SCRIPT), "--annotate", "--ledger", str(tmp_path / "nope"), "--output", str(out)],
		capture_output=True, text=True, env=ENV)
	assert missing.returncode == 1 and "CLAUDE_FIXER_CONSENSUS_IDS error=" in missing.stderr


# ── build_cross_pollination_summary in review_run_reviewers.sh ──


def _cross_pollination(tmp: Path, *, with_script: bool) -> tuple[subprocess.CompletedProcess, str]:
	source = RUNNER.read_text(encoding="utf-8")
	match = re.search(r"^build_cross_pollination_summary\(\) \{\n.*?^\}\n", source, re.S | re.M)
	assert match, "build_cross_pollination_summary not found"
	support = tmp / "support"
	support.mkdir()
	if with_script:
		(support / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
	ledger = tmp / "consensus_pass1.txt"
	ledger.write_text(_ledger(PASS1_README), encoding="utf-8")
	env = {**ENV, "RUNTIME_DIR": str(tmp), "SUPPORT_SCRIPTS_DIR": str(support), "PREVIOUS_REVIEWS_DIR": str(tmp)}
	proc = subprocess.run(["bash", "-c", match.group(0) + f'\nset -euo pipefail\nbuild_cross_pollination_summary "{ledger}"'],
		capture_output=True, text=True, env=env)
	return proc, ledger.read_text(encoding="utf-8")


def test_cross_pollination_summary_shows_the_consensus_ids(tmp_path):
	proc, ledger_after = _cross_pollination(tmp_path, with_script=True)
	assert proc.returncode == 0, proc.stderr
	summary_path = proc.stdout.strip()
	assert summary_path == str(tmp_path / "cross_pollination_summary.txt")
	summary = Path(summary_path).read_text(encoding="utf-8")
	assert f"  consensus_id: {RID}" in summary
	assert "REJECTED_FINDING: <consensus_id> | <file>:<line or start-end> | flagged_by: <slug> | reason: <one sentence>" in summary
	assert "A REJECTED_FINDING line without the entry's consensus_id is ignored." in summary
	# The persisted pass-1 ledger is never rewritten, so the demoter recomputes the same ids.
	assert "consensus_id" not in ledger_after


def test_cross_pollination_summary_falls_back_to_the_plain_ledger(tmp_path):
	proc, _ledger_after = _cross_pollination(tmp_path, with_script=False)
	assert proc.returncode == 0, proc.stderr
	summary = Path(proc.stdout.strip()).read_text(encoding="utf-8")
	assert "- README.md:1261 | severity=critical" in summary and "  consensus_id: p1-" not in summary
	assert "Could not add consensus_id lines" in proc.stderr


def test_runner_and_summariser_prompts_describe_the_id_shape():
	summariser = (REPO_ROOT / "scripts" / "summarize_reviewer_consensus.sh").read_text(encoding="utf-8")
	assert "REJECTED_FINDING: <consensus_id> | <file>:<line> | flagged_by: <slug>" in summariser
	assert "consensus_id: p1-<12 hex digits>" in summariser

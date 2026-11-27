"""CLAUDE.md section numbers are unique and in order.

Section numbers are identifiers (CLAUDE.md §6): workflows, scripts, prompts,
and commands cite them, so two sections sharing a number make every "§N"
reference ambiguous. PR #4443 and PR #4440 each added a new "§28" in parallel;
git merged both without a textual conflict in the heading lines, and nothing
caught the duplicate until a human read the merge. This test fails the PR that
introduces a duplicate, whichever way the files were merged.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = ROOT / "CLAUDE.md"
SECTION_RE = re.compile(r"^## §(?P<num>[0-9]+)\.\s", re.MULTILINE)


def _section_numbers(text: str) -> list[int]:
	return [int(m.group("num")) for m in SECTION_RE.finditer(text)]


def test_claude_md_section_numbers_are_unique_and_increasing():
	numbers = _section_numbers(CLAUDE_MD.read_text(encoding="utf-8"))
	assert numbers, "no '## §N.' headings found in CLAUDE.md"
	duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
	assert not duplicates, f"CLAUDE.md has duplicate section numbers: {['§' + str(n) for n in duplicates]}"
	assert numbers == sorted(numbers), f"CLAUDE.md section numbers are out of order: {numbers}"


def test_template_claude_md_is_the_same_file():
	# workflow-templates/CLAUDE.md is a symlink to the root file, so the check
	# above covers what consumers receive.
	assert (ROOT / "workflow-templates" / "CLAUDE.md").resolve() == CLAUDE_MD.resolve()


def test_detector_catches_a_duplicate():
	text = "## §27. A\n\n## §28. B\n\n## §28. C\n"
	numbers = _section_numbers(text)
	assert [n for n in numbers if numbers.count(n) > 1] == [28, 28]


def _section_33_d(text: str) -> str:
	start = text.index("## §33. ")
	end_match = re.compile(r"^## ", re.MULTILINE).search(text, start + 1)
	section = text[start : end_match.start() if end_match else len(text)]
	c_heading = "### C) Relationship to Other Rules"
	d_heading = "### D) Wedged Queued Runs"
	assert c_heading in section, "§33.C heading missing"
	assert d_heading in section, "§33.D heading missing"
	assert section.index(c_heading) < section.index(d_heading), "§33.D must follow §33.C"
	return section[section.index(d_heading) :]


def test_section_33_d_wedged_queued_runs_in_both_copies():
	# Issue #10032: sessions must not count wedged queued runs as load. Both the
	# live file and the template consumers receive carry the same §33.D text.
	live = _section_33_d(CLAUDE_MD.read_text(encoding="utf-8"))
	template = _section_33_d((ROOT / "workflow-templates" / "CLAUDE.md").read_text(encoding="utf-8"))
	assert live == template
	assert "ZOMBIE_RUN_MAX_QUEUED_HOURS" in live
	assert "GitHub Support" in live
	assert "wedged=" in live and "queued=" in live

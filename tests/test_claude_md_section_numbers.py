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

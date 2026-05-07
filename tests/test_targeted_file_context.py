"""Contract tests for scripts/targeted_file_context.py.

The helper inlines likely-to-be-edited files into the Codex prompt so the
editor's first turn is a write, not a read. Pin the input-mode contract
(plan / paths-file / paths union), the per-file size cap (big files get a
"too large to inline" marker, NOT a head-truncated copy), the total-budget
cap, and the path-traversal guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from targeted_file_context import (  # noqa: E402
	emit_context,
	extract_paths_from_plan,
	parse_paths_arg,
	parse_paths_file,
)


def test_extracts_files_likely_to_change_section_only() -> None:
	plan = """
Implementation Plan

1. Files likely to change
- `contracts/FunOFTAdapter.sol`
- `test/CrossChain.test.ts`

2. Functions or modules to implement
- In `docs/not-a-target.md`, explain nothing.
"""

	assert extract_paths_from_plan(plan) == [
		"contracts/FunOFTAdapter.sol",
		"test/CrossChain.test.ts",
	]


def test_extract_handles_alternate_section_headers() -> None:
	"""'Files to modify' / 'Files expected to change' should also work."""
	for header in (
		"Files to modify",
		"Files expected to change",
		"Files to change",
		"Files likely to modify",
	):
		plan = f"{header}\n- `src/foo.py`\n- `src/bar.py`\n\nNotes\n- something\n"
		assert extract_paths_from_plan(plan) == ["src/foo.py", "src/bar.py"], header


def test_paths_file_strips_git_porcelain_prefixes(tmp_path: Path) -> None:
	"""parse_paths_file should accept `git status --porcelain` and
	`git diff --name-only` output verbatim."""
	paths_file = tmp_path / "paths.txt"
	paths_file.write_text(
		"M  scripts/foo.sh\n"
		"?? new/file.py\n"
		"A  added.json\n"
		"# comment line\n"
		"\n"
		"plain/path/already.md\n",
		encoding="utf-8",
	)
	assert parse_paths_file(str(paths_file)) == [
		"scripts/foo.sh",
		"new/file.py",
		"added.json",
		"plain/path/already.md",
	]


def test_paths_arg_dedups_and_orders() -> None:
	assert parse_paths_arg("a/x.py , b/y.py , a/x.py") == ["a/x.py", "b/y.py"]


def test_emits_line_numbered_bounded_context(tmp_path: Path) -> None:
	(tmp_path / "contracts").mkdir()
	target = tmp_path / "contracts" / "FunOFT.sol"
	target.write_text("line one\nline two\n", encoding="utf-8")

	context = emit_context(
		["contracts/FunOFT.sol"],
		tmp_path,
		max_files=10,
		max_bytes=1024,
		max_file_bytes=1024,
	)

	assert "=== TARGETED FILE CONTEXT ===" in context
	assert "--- FILE: contracts/FunOFT.sol" in context
	assert "1\tline one" in context
	assert "2\tline two" in context
	assert "Included 1 target file(s)" in context


def test_per_file_cap_skips_big_files_with_read_marker(tmp_path: Path) -> None:
	"""The whole point of the per-file cap: a 500KB file with a 50KB cap
	should NOT get its first 50KB inlined (which would be misleading —
	the edit point may live at the bottom). It should get a clear
	"read with read tool" marker instead."""
	(tmp_path / "big").mkdir()
	big = tmp_path / "big" / "huge.py"
	# 100KB content (exceeds 50KB cap)
	big.write_text("x = 1\n" * 20000, encoding="utf-8")

	context = emit_context(
		["big/huge.py"],
		tmp_path,
		max_files=10,
		max_bytes=1_000_000,
		max_file_bytes=51_200,
	)

	# The big file gets a marker, NOT inlined content.
	assert "too large to inline" in context
	assert "read with read tool" in context
	assert "max_file_bytes=51200" in context
	# No "1\t" line-numbered content for it (since inlining was skipped).
	# The marker line itself doesn't start with "1\t".
	assert "1\tx = 1" not in context, (
		"big file content must not be head-truncated and pasted — that "
		"would mislead the editor into thinking it has the edit region "
		"when the bottom of the file may carry the actual target"
	)


def test_total_budget_overflow_skips_with_marker(tmp_path: Path) -> None:
	"""A small file that fits the per-file cap but would overflow the
	total budget gets the same marker — never head-truncated."""
	(tmp_path / "src").mkdir()
	# 30KB each (each fits under 51200 per-file cap)
	for name in ("a.py", "b.py", "c.py"):
		(tmp_path / "src" / name).write_text("y = 1\n" * 6000, encoding="utf-8")

	context = emit_context(
		["src/a.py", "src/b.py", "src/c.py"],
		tmp_path,
		max_files=10,
		max_bytes=50_000,  # only ~1.5 of the 30KB files fit
		max_file_bytes=51_200,
	)

	# At least one file fully inlined, the rest get a "would overflow"
	# marker rather than mid-file truncation.
	assert "would overflow total budget" in context


def test_path_traversal_outside_repo_root_is_silently_dropped(tmp_path: Path) -> None:
	"""A `--paths-file` source could be adversarial; resolve and refuse
	anything that escapes the repo root."""
	context = emit_context(
		["../secret.txt", "/etc/passwd", "good/file.py"],
		tmp_path,
		max_files=10,
		max_bytes=1024,
		max_file_bytes=1024,
	)
	# good/file.py doesn't exist either, but it's a valid intra-repo path.
	# No file content from outside the repo should appear.
	assert "passwd" not in context
	assert "../" not in context.split("FILE:")[1] if "FILE:" in context else True


def test_missing_input_emits_safe_empty_block() -> None:
	context = emit_context([], Path("/tmp"), max_files=10, max_bytes=1024, max_file_bytes=1024)
	assert "=== TARGETED FILE CONTEXT ===" in context
	assert "(no existing target files could be inlined)" in context


def test_disabled_by_zero_limits() -> None:
	context = emit_context(
		["whatever"],
		Path("/tmp"),
		max_files=0,
		max_bytes=1024,
		max_file_bytes=1024,
	)
	assert "targeted context disabled by limits" in context

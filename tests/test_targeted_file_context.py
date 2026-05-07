#!/usr/bin/env python3
"""Contract tests for scripts/targeted_file_context.py.

The helper inlines likely-to-be-edited files into the Codex prompt so the
editor's first turn is a write, not a read. Pin the input-mode contract
(plan / paths-file / paths union), the per-file size cap (big files get a
"too large to inline" marker, NOT a head-truncated copy), the total-budget
cap, and the path-traversal guard.

Layout follows the rest of tests/ in this repo: zero-arg test functions
plus a manual `main()` runner so CI can execute the file with
`python3 tests/test_targeted_file_context.py` (no pytest dependency).
"""
from __future__ import annotations

import sys
import tempfile
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


def test_extract_terminates_on_numbered_section_with_trailing_period() -> None:
	"""The plan template at prompts/mode-plan.txt produces section
	labels of the form `1. Files likely to change.` and
	`2. Functions/modules to implement.` (numbered, trailing period).
	The scan must terminate at the next numbered section header so
	inline-backticked paths from implementation prose don't leak."""
	plan = """
Implementation Plan

1. Files likely to change.
- `contracts/FunOFTAdapter.sol`
- `test/CrossChain.test.ts`

2. Functions or modules to implement.
- Edit `contracts/leaked.sol` to do X.
- See `docs/leaked-too.md` for context.
"""
	assert extract_paths_from_plan(plan) == [
		"contracts/FunOFTAdapter.sol",
		"test/CrossChain.test.ts",
	], (
		"numbered section header with trailing period must terminate the "
		"likely-files scan; otherwise contracts/leaked.sol and "
		"docs/leaked-too.md leak from the implementation-notes section"
	)


def test_extract_does_not_treat_path_bullets_as_section_headers() -> None:
	"""A bullet like `- contracts/Foo.sol` (leading hyphen) must be
	extracted as a path, not mistaken for a section header.
	`extract_paths_from_plan()` checks SECTION_HEADER_RE before
	BULLET_RE, but SECTION_HEADER_RE requires `[A-Za-z]` after the
	optional numeric prefix — a leading `-` (or `*` / `+`) cannot
	match, so bullet lines fall through to the BULLET_RE branch and
	are extracted correctly. This test pins that the bullet character
	classes don't accidentally collide with the section-label
	alphabet."""
	plan = """
Files likely to change
- contracts/Foo.sol
- `contracts/Bar.sol`
"""
	assert extract_paths_from_plan(plan) == [
		"contracts/Foo.sol",
		"contracts/Bar.sol",
	]


def test_extract_terminates_on_markdown_heading() -> None:
	"""Plans using `## Foo` ATX headings to introduce later sections must
	terminate the likely-files scan; otherwise inline-backticked paths
	from later sections (e.g. `docs/note.md` in implementation prose)
	leak into the inlining set. Pinned by Copilot review on PR #2241."""
	plan = """
Implementation Plan

## Files likely to change
- `contracts/FunOFTAdapter.sol`
- `test/CrossChain.test.ts`

## Functions or modules to implement
- Edit the `_send` function in `contracts/leaked.sol`.
- See `docs/leaked-too.md` for context.

## Risks
- `tests/leaked-three.ts` may need updates.
"""
	assert extract_paths_from_plan(plan) == [
		"contracts/FunOFTAdapter.sol",
		"test/CrossChain.test.ts",
	], (
		"markdown ATX heading must terminate the section scan; otherwise "
		"contracts/leaked.sol / docs/leaked-too.md / tests/leaked-three.ts "
		"would smuggle into the inlining set"
	)


def test_paths_file_strips_git_porcelain_prefixes() -> None:
	"""parse_paths_file should accept `git status --porcelain` and
	`git diff --name-only` output verbatim."""
	with tempfile.TemporaryDirectory() as tmp:
		paths_file = Path(tmp) / "paths.txt"
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


def test_emits_line_numbered_bounded_context() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "contracts").mkdir()
		target = root / "contracts" / "FunOFT.sol"
		target.write_text("line one\nline two\n", encoding="utf-8")

		context = emit_context(
			["contracts/FunOFT.sol"],
			root,
			max_bytes=1024,
		)

		assert "=== TARGETED FILE CONTEXT ===" in context
		assert "--- FILE: contracts/FunOFT.sol" in context
		assert "1\tline one" in context
		assert "2\tline two" in context
		assert "Included 1 entry" in context
		assert "1 inlined" in context


def test_total_budget_overflow_skips_with_marker_not_truncation() -> None:
	"""A file that would push the cumulative byte count over MAX_BYTES
	must NOT be head-truncated — that would mislead the editor when
	the edit point lives at the bottom of the file. It gets a clear
	"would overflow total budget" marker so the model uses its
	native read tool instead.

	Every path the caller passes is reported (inlined or marker) —
	there is no separate file-count cap, so a long path list never
	"silently drops" entries past some N."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		# 30KB each, 4 files
		for name in ("a.py", "b.py", "c.py", "d.py"):
			(root / "src" / name).write_text("y = 1\n" * 6000, encoding="utf-8")

		context = emit_context(
			["src/a.py", "src/b.py", "src/c.py", "src/d.py"],
			root,
			max_bytes=50_000,  # only ~1.5 files fit
		)

		# First file fully inlined, the rest get markers rather than
		# mid-file truncation.
		assert "would overflow total budget" in context
		# Inlined-content sanity: the first file's `1\ty = 1` line is
		# present (it was inlined). The other files' content is not.
		assert "1\ty = 1" in context
		# The marker line carries the budget context for the operator /
		# model.
		assert "max_bytes=50000" in context
		# Every path appears (inlined or marker) — no silent drops.
		for name in ("src/a.py", "src/b.py", "src/c.py", "src/d.py"):
			assert name in context, f"path {name} missing from output"


def test_path_traversal_outside_repo_root_is_silently_dropped() -> None:
	"""A `--paths-file` source could be adversarial; resolve and refuse
	anything that escapes the repo root."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		context = emit_context(
			["../secret.txt", "/etc/passwd", "good/file.py"],
			root,
			max_bytes=1024,
		)
		# good/file.py doesn't exist either, but it's a valid intra-repo
		# path. No file content from outside the repo should appear.
		assert "passwd" not in context


def test_missing_input_emits_safe_empty_block() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		context = emit_context(
			[],
			Path(tmp),
			max_bytes=1024,
		)
		assert "=== TARGETED FILE CONTEXT ===" in context
		assert "(no existing target files could be inlined)" in context


def test_disabled_by_zero_max_bytes() -> None:
	"""Set max_bytes=0 to disable inlining entirely — block becomes a
	single-line "(disabled)" marker."""
	with tempfile.TemporaryDirectory() as tmp:
		context = emit_context(
			["whatever"],
			Path(tmp),
			max_bytes=0,
		)
		assert "targeted context disabled" in context
		assert "max_bytes=0" in context


def test_paths_file_extracts_destination_from_rename_porcelain() -> None:
	"""`git status --porcelain` rename / copy entries have the form
	`R  old/path.py -> new/path.py`. The destination path is what's
	on disk and what the editor will edit, so the parser must split
	on ` -> ` and emit the post-rename name. Pinned by Copilot review
	on PR #2241."""
	with tempfile.TemporaryDirectory() as tmp:
		paths_file = Path(tmp) / "paths.txt"
		paths_file.write_text(
			"R  scripts/old.sh -> scripts/new.sh\n"
			"C  contracts/orig.sol -> contracts/copy.sol\n"
			"M  no/rename.py\n",
			encoding="utf-8",
		)
		assert parse_paths_file(str(paths_file)) == [
			"scripts/new.sh",
			"contracts/copy.sol",
			"no/rename.py",
		]


def test_paths_file_strips_diff_a_b_prefixes() -> None:
	"""parse_paths_file should accept `git diff` output that uses
	`a/` and `b/` prefixes (raw or with `+++ ` / `--- `). Pinned by
	Copilot review on PR #2241."""
	with tempfile.TemporaryDirectory() as tmp:
		paths_file = Path(tmp) / "paths.txt"
		paths_file.write_text(
			"+++ b/scripts/foo.sh\n"
			"--- a/scripts/foo.sh\n"
			"a/scripts/bar.py\n"
			"b/scripts/baz.py\n"
			"plain/path/already.md\n",
			encoding="utf-8",
		)
		# Both `a/` and `b/` prefixes stripped so the same file isn't
		# double-counted, and no spurious `b/scripts/foo.sh` entries
		# leak through.
		assert parse_paths_file(str(paths_file)) == [
			"scripts/foo.sh",
			"scripts/bar.py",
			"scripts/baz.py",
			"plain/path/already.md",
		]


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())

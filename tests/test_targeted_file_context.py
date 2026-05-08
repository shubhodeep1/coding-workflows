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

import io
import contextlib
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from targeted_file_context import (  # noqa: E402
	SEMBLE_QUERY_TEXT_MAX_CHARS,
	SEMBLE_QUERY_TIMEOUT_SECS,
	_read_optional_query_text,
	emit_context,
	extract_paths_from_plan,
	main as targeted_file_context_main,
	parse_paths_arg,
	parse_paths_file,
)


def _make_fake_semble_script(path: Path, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
	path.write_text(
		"#!/usr/bin/env python3\n"
		"import sys\n"
		f"sys.stdout.write({stdout!r})\n"
		f"sys.stderr.write({stderr!r})\n"
		f"raise SystemExit({exit_code})\n",
		encoding="utf-8",
	)
	path.chmod(0o755)


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


def test_emits_unnumbered_bounded_context() -> None:
	"""Fully-inlined files emit verbatim content (no line-number prefixes).

	Line-numbering was an apply_patch context-format echo and biased the
	OpenRouter Responses + OpenAI-slug failure path identified in the
	2026-05-07 ablation suite. Unnumbered content reads fine for the
	model and removes the bias.
	"""
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
		# Unnumbered content: the file lines appear verbatim, NOT with
		# `1\t` / `2\t` prefixes.
		assert "\nline one\n" in context
		assert "\nline two\n" in context
		assert "1\tline one" not in context
		assert "2\tline two" not in context
		assert "Included 1 entry" in context
		assert "1 inlined" in context


def test_header_does_not_prescribe_first_tool_call() -> None:
	"""Header MUST NOT tell the model "your first tool call should be a
	write" or use the "unless the request is already satisfied" hedge.

	Both phrasings biased the OpenRouter Responses + OpenAI-slug
	apply_patch path into reasoning-without-emit on the 2026-05-07 12:41
	smoke runs. The replacement wording presents inlined files as
	reference material and lets the model choose its own tool flow.
	"""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "tests").mkdir()
		(root / "tests" / "smoke.txt").write_text("status: ok\n", encoding="utf-8")
		context = emit_context(["tests/smoke.txt"], root, max_bytes=1024)
		assert "=== TARGETED FILE CONTEXT ===" in context
		assert "first tool call" not in context.lower()
		assert "unless the request is already satisfied" not in context.lower()
		assert "for reference" in context.lower()


def test_plain_text_hint_on_per_file_marker() -> None:
	"""Plain-text small files (.txt / .csv / .md) get a per-file FILE
	marker hint that a shell `printf` / heredoc write is acceptable.

	The 2026-05-07 ablation showed the f06 unnumbered + shell-nudge
	combo flips OpenRouter from fail to pass on the smoke canary. The
	per-file marker keeps the hint adjacent to the file content, so it
	survives even when the header scrolls past attention on long
	prompts. Code files (.py / .ts / .sol / etc.) stay on the bare
	marker so the model's normal edit-tool choice is not perturbed.
	"""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "tests").mkdir()
		(root / "tests" / "smoke.txt").write_text("status: ok\n", encoding="utf-8")
		(root / "tests" / "fixtures.csv").write_text("a,b\n1,2\n", encoding="utf-8")
		(root / "docs").mkdir()
		(root / "docs" / "note.md").write_text("# heading\n", encoding="utf-8")
		(root / "src").mkdir()
		(root / "src" / "lib.py").write_text("x = 1\n", encoding="utf-8")

		context = emit_context(
			["tests/smoke.txt", "tests/fixtures.csv", "docs/note.md", "src/lib.py"],
			root,
			max_bytes=4096,
		)

		# Plain-text extensions: marker carries the printf hint.
		assert "printf ... > tests/smoke.txt" in context
		assert "printf ... > tests/fixtures.csv" in context
		assert "printf ... > docs/note.md" in context
		# Code file: marker is bare, no hint.
		assert "--- FILE: src/lib.py (6 bytes) ---" in context
		assert "printf ... > src/lib.py" not in context


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
		# Inlined-content sanity: the first file's `y = 1` content is
		# present verbatim (unnumbered). The other files' content is not.
		assert "\ny = 1\n" in context
		# Line-numbered prefix MUST NOT appear — fully-inlined files
		# emit verbatim content (see test_emits_unnumbered_bounded_context).
		assert "1\ty = 1" not in context
		# The marker line carries the budget context for the operator /
		# model.
		assert "max_bytes=50000" in context
		# Every path appears (inlined or marker) — no silent drops.
		for name in ("src/a.py", "src/b.py", "src/c.py", "src/d.py"):
			assert name in context, f"path {name} missing from output"


def test_non_overflow_output_stays_legacy_shape_even_with_semble_flags_present() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		(root / "src" / "ok.py").write_text("print('ok')\n", encoding="utf-8")
		query_file = root / "query.txt"
		query_file.write_text("find relevant chunks\n", encoding="utf-8")

		legacy = emit_context(["src/ok.py"], root, max_bytes=1024)
		with_semble_args = emit_context(
			["src/ok.py"],
			root,
			max_bytes=1024,
			semble_bin="/does/not/matter",
			semble_index=str(root / ".semble-index"),
			semble_query_text=query_file.read_text(encoding="utf-8"),
			semble_max_chunks=3,
			semble_fallback="read",
		)

		assert with_semble_args == legacy


def test_non_semble_summary_keeps_legacy_parenthetical_format() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		(root / "src" / "small.py").write_text("small = 1\n", encoding="utf-8")
		(root / "src" / "big.py").write_text("y = 1\n" * 6000, encoding="utf-8")

		context = emit_context(
			["src/small.py", "src/big.py"],
			root,
			max_bytes=1024,
		)

		assert "Included 2 entries (1 inlined, 1 marker-only), " in context
		assert "chunk-retrieved-via-Semble" not in context


def test_read_optional_query_text_truncates_large_input() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		query_file = Path(tmp) / "query.txt"
		oversized = "x" * (SEMBLE_QUERY_TEXT_MAX_CHARS + 50)
		query_file.write_text(oversized, encoding="utf-8")
		stderr = io.StringIO()
		with contextlib.redirect_stderr(stderr):
			query_text = _read_optional_query_text(str(query_file))
		assert query_text == oversized[:SEMBLE_QUERY_TEXT_MAX_CHARS]
		assert f"limit={SEMBLE_QUERY_TEXT_MAX_CHARS}" in stderr.getvalue()


def test_overflow_uses_semble_chunks_when_query_succeeds() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		(root / "src" / "big.py").write_text("x = 1\n" * 2000, encoding="utf-8")
		semble = root / "fake_semble.py"
		_make_fake_semble_script(
			semble,
			stdout="chunk 1\n# lines 10-20\nchunk 2\n",
		)

		context = emit_context(
			["src/big.py"],
			root,
			max_bytes=100,
			semble_bin=str(semble),
			semble_index=str(root / ".semble-index"),
			semble_query_text="task summary",
			semble_max_chunks=2,
		)

		assert "chunk-retrieved via semble" in context
		assert "--- FILE: src/big.py (12000 bytes; would overflow total budget" not in context
		assert "src/big.py" in context


def test_overflow_marker_remains_default_fallback_when_semble_unavailable() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		(root / "src" / "big.py").write_text("x = 1\n" * 2000, encoding="utf-8")

		stderr = io.StringIO()
		with contextlib.redirect_stderr(stderr):
			context = emit_context(
				["src/big.py"],
				root,
				max_bytes=100,
				semble_bin=str(root / "missing_semble"),
				semble_index=str(root / ".semble-index"),
				semble_query_text="task summary",
			)

		assert "would overflow total budget" in context
		assert "chunk-retrieved via semble" not in context
		assert "SEMBLE_FALLBACK target=overflow file=src/big.py" in stderr.getvalue()


def test_overflow_read_fallback_emits_bounded_head_body() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		body = "line\n" * 3000
		(root / "src" / "big.py").write_text(body, encoding="utf-8")

		context = emit_context(
			["src/big.py"],
			root,
			max_bytes=128,
			semble_bin=str(root / "missing_semble"),
			semble_index=str(root / ".semble-index"),
			semble_query_text="task summary",
			semble_fallback="read",
		)

		assert "overflow fallback read head" in context
		assert "truncated to 128 byte(s)" in context
		assert "line\nline\nline\n" in context
		assert "--- FILE: src/big.py (15000 bytes; would overflow total budget" not in context


def test_overflow_marker_fallback_does_not_read_file_bytes() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		target = root / "src" / "big.py"
		target.write_text("x = 1\n" * 2000, encoding="utf-8")
		original_read_bytes = Path.read_bytes
		calls: list[Path] = []

		def _tracking_read_bytes(self: Path) -> bytes:
			calls.append(self)
			return original_read_bytes(self)

		Path.read_bytes = _tracking_read_bytes
		try:
			context = emit_context(
				["src/big.py"],
				root,
				max_bytes=100,
				semble_bin=str(root / "missing_semble"),
				semble_index=str(root / ".semble-index"),
				semble_query_text="task summary",
			)
		finally:
			Path.read_bytes = original_read_bytes

		assert "would overflow total budget" in context
		assert calls == []


def test_overflow_read_fallback_counts_rendered_bytes_in_budget() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		(root / "src" / "big.py").write_text("a" * 5000, encoding="utf-8")
		(root / "src" / "small.py").write_text("b" * 20, encoding="utf-8")

		context = emit_context(
			["src/big.py", "src/small.py"],
			root,
			max_bytes=128,
			semble_bin=str(root / "missing_semble"),
			semble_index=str(root / ".semble-index"),
			semble_query_text="task summary",
			semble_fallback="read",
		)

		assert "overflow fallback read head" in context
		assert "--- FILE: src/small.py (20 bytes; would overflow total budget" in context
		assert "Included 2 entries (0 inlined, 1 marker-only, 0 semble, 1 read), 128 byte(s) of source content." in context


def test_semble_summary_counts_missing_and_binary_entries_as_marker_only() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		(root / "src" / "blob.py").write_bytes(b"\x00\x01")
		(root / "src" / "big.py").write_text("x = 1\n" * 2000, encoding="utf-8")
		semble = root / "fake_semble.py"
		_make_fake_semble_script(semble, stdout="chunk\n")
		context = emit_context(["src/missing.py", "src/blob.py", "src/big.py"], root, max_bytes=100, semble_bin=str(semble), semble_index=str(root / ".semble-index"), semble_query_text="task summary", semble_max_chunks=1)
		assert "--- FILE: src/missing.py (missing) ---" in context
		assert "--- FILE: src/blob.py (binary skipped) ---" in context
		assert "Included 3 entries (0 inlined, 2 marker-only, 1 semble, 0 read), " in context


def test_semble_query_timeout_falls_back_cleanly() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		(root / "src" / "big.py").write_text("x = 1\n" * 2000, encoding="utf-8")
		timeout_semble = root / "sleepy_semble.py"
		timeout_semble.write_text(
			"#!/usr/bin/env python3\n"
			"import time\n"
			f"time.sleep({SEMBLE_QUERY_TIMEOUT_SECS + 1})\n",
			encoding="utf-8",
		)
		timeout_semble.chmod(0o755)

		stderr = io.StringIO()
		with contextlib.redirect_stderr(stderr):
			context = emit_context(
				["src/big.py"],
				root,
				max_bytes=100,
				semble_bin=str(timeout_semble),
				semble_index=str(root / ".semble-index"),
				semble_query_text="task summary",
			)

		assert "would overflow total budget" in context
		assert "timed out" in stderr.getvalue().lower()


def test_overflow_off_fallback_emits_no_representation_and_no_marker_count() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "src").mkdir()
		(root / "src" / "big.py").write_text("x = 1\n" * 2000, encoding="utf-8")

		context = emit_context(
			["src/big.py"],
			root,
			max_bytes=100,
			semble_bin=str(root / "missing_semble"),
			semble_index=str(root / ".semble-index"),
			semble_query_text="task summary",
			semble_fallback="off",
		)

		assert "--- FILE: src/big.py (12000 bytes; would overflow total budget" not in context
		assert "chunk-retrieved via semble" not in context
		assert "Suppressed overflow entries: 1." in context
		assert "(0 inlined, 0 marker-only, 0 semble, 0 read)" in context


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


def test_cli_accepts_read_fallback_mode() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		paths_file = root / "paths.txt"
		paths_file.write_text("src/file.py\n", encoding="utf-8")
		output = root / "out.txt"
		argv = sys.argv[:]
		try:
			sys.argv = [
				"targeted_file_context.py",
				"--paths-file",
				str(paths_file),
				"--repo-root",
				str(root),
				"--semble-fallback",
				"read",
				"--output",
				str(output),
			]
			assert targeted_file_context_main() == 0
		finally:
			sys.argv = argv


def test_cli_rejects_unknown_fallback_mode() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		output = root / "out.txt"
		argv = sys.argv[:]
		stderr = io.StringIO()
		try:
			sys.argv = [
				"targeted_file_context.py",
				"--repo-root",
				str(root),
				"--semble-fallback",
				"bogus",
				"--output",
				str(output),
			]
			with contextlib.redirect_stderr(stderr):
				try:
					targeted_file_context_main()
				except SystemExit as exc:
					assert exc.code != 0
				else:
					raise AssertionError("expected argparse to reject unknown fallback mode")
		finally:
			sys.argv = argv
		assert "invalid choice" in stderr.getvalue()


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

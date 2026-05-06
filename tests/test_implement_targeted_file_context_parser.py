#!/usr/bin/env python3
"""Regression tests for the implement-phase TARGETED FILE CONTEXT parser.

The parser lives inline in `.github/workflows/implement.yml` (the
`Run Codex implementation` step) as an awk pipeline that extracts
file paths from the approved plan's "Files likely to change" section.
It is the load-bearing piece of the gpt-5.3-codex
"announce-without-emit" mitigation (openai/codex#11151) — when it
silently fails to match a plan format, every plan-named file
disappears from the prompt and the model falls back to shell
exploration, tripping the empty_streak watchdog.

These tests pin:
  - Section-start forms accepted (bold, ATX, numbered `1.`, numbered
    `1)`, plain heading variant).
  - Section terminator behaviour, including the carve-out for
    numbered children whose body is a path entry (otherwise a plan
    written as `1. Files likely to change / 1. ` + backticked path
    silently terminates at the first child).
  - Lexical safety filters (absolute paths, parent-traversal,
    non-path tokens rejected).
  - Backticked-path extraction AND bare-path extraction on list-item
    lines.
  - Deduplication of repeated entries.
  - Real plan body excerpted from fun-token-multi-chain run
    25406869763 issue #195 (regression case for the original PR).

The test extracts the awk source verbatim from the workflow YAML so a
regex tweak in production cannot diverge from the contract here. If
the inline parser is later moved into a shared script, update
`_extract_parser_awk_source` to read from that script instead.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"

_PARSER_ANCHOR_BEGIN = "targeted_paths_raw="
_PARSER_AWK_BEGIN = "awk '"
_PARSER_AWK_END = "\"${PLAN_FILE}\""


def _extract_run_block(step_name: str) -> str:
	"""Return the resolved bash body of a workflow step by name.

	Parses YAML rather than scraping the text so the extraction is
	robust against block-scalar reindentation, trailing-whitespace
	tweaks, and other formatting changes that don't alter the shell
	the runner executes.
	"""
	doc = yaml.safe_load(IMPLEMENT_WORKFLOW.read_text(encoding="utf-8"))
	for job in doc["jobs"].values():
		for step in job.get("steps", []):
			if step.get("name") == step_name:
				run = step.get("run", "")
				if not run:
					raise RuntimeError(
						f"Step {step_name!r} has no 'run' body in implement.yml."
					)
				return run
	raise RuntimeError(
		f"Step {step_name!r} not found in implement.yml. Has it been "
		"renamed? Update _extract_run_block accordingly."
	)


def _extract_parser_awk_source() -> str:
	"""Pull the awk parser body out of the run block.

	The parser is the first awk invocation inside the
	`targeted_paths_raw="$(` command substitution in the
	"Run Codex implementation" step. We locate it by anchor-string
	search rather than full regex so the extraction tolerates
	whitespace, comment, and reformatting changes.
	"""
	run = _extract_run_block("Run Codex implementation")

	anchor_idx = run.find(_PARSER_ANCHOR_BEGIN)
	if anchor_idx < 0:
		raise RuntimeError(
			f"Could not find {_PARSER_ANCHOR_BEGIN!r} in the "
			"'Run Codex implementation' step. Has the targeted-file-context "
			"block been removed or refactored? Update "
			"_extract_parser_awk_source accordingly."
		)

	awk_idx = run.find(_PARSER_AWK_BEGIN, anchor_idx)
	if awk_idx < 0:
		raise RuntimeError(
			f"Could not find {_PARSER_AWK_BEGIN!r} after {_PARSER_ANCHOR_BEGIN!r} "
			"in the 'Run Codex implementation' step. Has the awk parser "
			"been moved out of inline shell? Update _extract_parser_awk_source."
		)

	# Body starts just after `awk '` (typically `awk '\n`).
	body_start = awk_idx + len(_PARSER_AWK_BEGIN)

	end_idx = run.find(_PARSER_AWK_END, body_start)
	if end_idx < 0:
		raise RuntimeError(
			f"Could not find {_PARSER_AWK_END!r} closing the awk parser. "
			"Update _extract_parser_awk_source."
		)

	# Trim back from end_idx to the closing `'` of the awk single-quoted
	# string. The shell text between body_start and end_idx looks like:
	#     <awk body>\n         '
	# (the `'` closes the awk script, `${PLAN_FILE}` follows). Find the
	# last `'` before end_idx.
	close_quote_idx = run.rfind("'", body_start, end_idx)
	if close_quote_idx < 0:
		raise RuntimeError(
			"Could not locate closing single-quote of the awk parser. "
			"Update _extract_parser_awk_source."
		)

	body = run[body_start:close_quote_idx]
	# Strip a leading newline if present (shell `awk '\n...\n'` form).
	if body.startswith("\n"):
		body = body[1:]
	# Trim trailing whitespace before the closing quote.
	body = body.rstrip()

	# Sanity: the body MUST contain the section-start guard. If it does
	# not, the extraction is wrong even if it returned non-empty.
	if "files[[:space:]]+[^\\n]*change" not in body:
		raise RuntimeError(
			"Extracted awk body does not contain the expected section-start "
			"regex anchor (`files...change`). Extraction is misaligned — "
			"likely picked up a different awk invocation. Update the "
			"extraction logic."
		)

	return body


def _run_parser(plan_text: str, tmp_path: Path) -> list[str]:
	"""Run the extracted awk parser against `plan_text`.

	Mirrors the production pipeline:
	  awk '<extracted>' <plan_file> | awk '!seen[$0]++'
	Returns the de-duplicated, ordered list of extracted path tokens.
	"""
	awk = shutil.which("awk")
	assert awk, "awk binary not found on PATH"
	plan_file = tmp_path / "plan.txt"
	plan_file.write_text(plan_text, encoding="utf-8")
	awk_source = _extract_parser_awk_source()

	first = subprocess.run(
		[awk, awk_source, str(plan_file)],
		capture_output=True,
		text=True,
		check=False,
	)
	if first.returncode != 0:
		pytest.fail(
			f"awk parser exited {first.returncode}; stderr:\n{first.stderr}\n"
			f"stdout:\n{first.stdout}"
		)
	dedup = subprocess.run(
		[awk, "!seen[$0]++"],
		input=first.stdout,
		capture_output=True,
		text=True,
		check=True,
	)
	return [line for line in dedup.stdout.splitlines() if line]


# --- Section-start forms ---------------------------------------------------

def test_bold_marker_section(tmp_path: Path) -> None:
	plan = (
		"**Files likely to change**\n"
		"- `contracts/FunOFT.sol`\n"
		"\n"
		"**Functions or modules to implement**\n"
	)
	assert _run_parser(plan, tmp_path) == ["contracts/FunOFT.sol"]


def test_atx_heading_section(tmp_path: Path) -> None:
	plan = (
		"## Files likely to change\n"
		"- `src/foo.ts`\n"
		"- `tests/foo.test.ts`\n"
		"## Functions/modules to implement\n"
	)
	assert _run_parser(plan, tmp_path) == ["src/foo.ts", "tests/foo.test.ts"]


def test_numbered_section_with_dash_children(tmp_path: Path) -> None:
	plan = (
		"1. Files likely to change\n"
		"   - `src/foo.ts`\n"
		"   - `src/bar.ts`\n"
		"2. Functions/modules to implement\n"
	)
	assert _run_parser(plan, tmp_path) == ["src/foo.ts", "src/bar.ts"]


def test_files_to_change_variant(tmp_path: Path) -> None:
	plan = (
		"## Files to change\n"
		"- `src/x.py`\n"
		"## Risks\n"
	)
	assert _run_parser(plan, tmp_path) == ["src/x.py"]


# --- Section-terminator carve-out (C5 regression) -------------------------

def test_numbered_section_with_numbered_backticked_children(tmp_path: Path) -> None:
	"""Plans written as `1. Files... / 1. <path> / 2. <path> / 2. <next section>`
	must keep the section open through the numbered children. Without the
	is_path_entry carve-out the section terminator fires on the first child
	and the parser silently returns []. Regression case for the parser
	end-rule bug surfaced in PR #2150 review."""
	plan = (
		"1. Files likely to change\n"
		"1. `src/foo.ts`\n"
		"2. `src/bar.ts`\n"
		"2. Functions/modules to implement\n"
	)
	assert _run_parser(plan, tmp_path) == ["src/foo.ts", "src/bar.ts"]


def test_bold_section_with_numbered_backticked_children(tmp_path: Path) -> None:
	plan = (
		"**Files likely to change**\n"
		"1. `pkg/a.go`\n"
		"2. `pkg/b.go`\n"
		"3. `pkg/c.go`\n"
		"**Functions/modules**\n"
	)
	assert _run_parser(plan, tmp_path) == ["pkg/a.go", "pkg/b.go", "pkg/c.go"]


def test_numbered_section_with_numbered_bare_children(tmp_path: Path) -> None:
	"""Lower-case path-with-extension token after the numbered prefix is
	recognised as a path entry, not a section title — keeps section open."""
	plan = (
		"1. Files likely to change\n"
		"1. src/bare/foo.ts\n"
		"2. src/bare/bar.ts\n"
		"2. Functions/modules to implement\n"
	)
	assert _run_parser(plan, tmp_path) == ["src/bare/foo.ts", "src/bare/bar.ts"]


def test_numbered_section_with_uppercase_bare_children(tmp_path: Path) -> None:
	"""Plans that name uppercase-starting paths on numbered list items
	(e.g. `1. API/util.ts`, `2. UI/Button.tsx`) must still be recognised
	as path entries — the file-extension dot-suffix anchors the
	disambiguation, so case-insensitive matching is correct.

	Regression case for PR #2150 multi-reviewer consensus finding
	(deepseek + minimax + moonshotai + x-ai + z-ai): the previous
	`^[a-z]` heuristic rejected uppercase-starting bare paths and
	silently terminated the section."""
	plan = (
		"1. Files likely to change\n"
		"1. API/util.ts\n"
		"2. UI/Button.tsx\n"
		"3. Backend/handler.go\n"
		"2. Functions/modules to implement\n"
	)
	assert _run_parser(plan, tmp_path) == [
		"API/util.ts",
		"UI/Button.tsx",
		"Backend/handler.go",
	]


def test_numbered_section_with_mixed_case_backticked_children(tmp_path: Path) -> None:
	"""Backtick-wrapped paths work regardless of starting case (the
	body-starts-with-backtick rule fires before the extension rule)."""
	plan = (
		"**Files likely to change**\n"
		"1. `API/util.ts`\n"
		"2. `core/util.py`\n"
		"3. `Backend/Handler.go`\n"
		"**Functions**\n"
	)
	assert _run_parser(plan, tmp_path) == [
		"API/util.ts",
		"core/util.py",
		"Backend/Handler.go",
	]


def test_atx_section_with_paren_numbered_children(tmp_path: Path) -> None:
	plan = (
		"## Files to change\n"
		"1) `core/util.py`\n"
		"2) `core/parse.py`\n"
		"## Risks\n"
	)
	assert _run_parser(plan, tmp_path) == ["core/util.py", "core/parse.py"]


def test_peer_section_title_still_ends_section(tmp_path: Path) -> None:
	"""Confirms the carve-out doesn't break the normal end-rule: a peer
	section title (multi-word natural-language phrase, no leading
	backtick, first word is not lower-case path-like) MUST still close
	the section."""
	plan = (
		"1. Files likely to change\n"
		"- `keep/me.ts`\n"
		"2. Functions/modules to implement\n"
		"- `do_not/include.ts`\n"
	)
	assert _run_parser(plan, tmp_path) == ["keep/me.ts"]


# --- Lexical safety filters ------------------------------------------------

def test_absolute_path_rejected(tmp_path: Path) -> None:
	plan = (
		"**Files likely to change**\n"
		"- /etc/passwd\n"
		"- `/var/log/syslog`\n"
		"- `src/ok.ts`\n"
		"**Other**\n"
	)
	assert _run_parser(plan, tmp_path) == ["src/ok.ts"]


def test_parent_traversal_rejected(tmp_path: Path) -> None:
	plan = (
		"**Files likely to change**\n"
		"- ../escape.sh\n"
		"- `../../escape.sh`\n"
		"- `src/ok.ts`\n"
		"**Other**\n"
	)
	assert _run_parser(plan, tmp_path) == ["src/ok.ts"]


def test_non_path_tokens_in_list_items_dropped(tmp_path: Path) -> None:
	plan = (
		"**Files likely to change**\n"
		"- not_a_path\n"
		"- alsoNotAPath\n"
		"- `src/ok.ts`\n"
		"**Other**\n"
	)
	assert _run_parser(plan, tmp_path) == ["src/ok.ts"]


# --- Extraction modes ------------------------------------------------------

def test_backticked_paths_extracted(tmp_path: Path) -> None:
	plan = (
		"**Files likely to change**\n"
		"- `a/b.ts` and `c/d.ts`\n"
		"**Other**\n"
	)
	assert _run_parser(plan, tmp_path) == ["a/b.ts", "c/d.ts"]


def test_bare_paths_on_list_items(tmp_path: Path) -> None:
	plan = (
		"**Files likely to change**\n"
		"- src/foo.ts\n"
		"- src/bar.ts (helper)\n"
		"**Other**\n"
	)
	assert _run_parser(plan, tmp_path) == ["src/foo.ts", "src/bar.ts"]


def test_prose_outside_section_ignored(tmp_path: Path) -> None:
	plan = (
		"Notes: in `unrelated/foo.ts` we will not edit anything.\n"
		"\n"
		"**Files likely to change**\n"
		"- `src/real.ts`\n"
		"\n"
		"**Functions or modules to implement**\n"
		"- in `unrelated/bar.ts` something happens but this is not the files section\n"
	)
	assert _run_parser(plan, tmp_path) == ["src/real.ts"]


def test_deduplication(tmp_path: Path) -> None:
	plan = (
		"**Files likely to change**\n"
		"- `a/b.ts`\n"
		"- `a/b.ts` again\n"
		"- `a/c.ts`\n"
		"**Other**\n"
	)
	assert _run_parser(plan, tmp_path) == ["a/b.ts", "a/c.ts"]


# --- Empty / missing section ----------------------------------------------

def test_no_files_section_returns_empty(tmp_path: Path) -> None:
	plan = (
		"Plan summary: refactor everything.\n"
		"\n"
		"## Risks\n"
		"- it might break\n"
	)
	assert _run_parser(plan, tmp_path) == []


# --- Real-world fixture ----------------------------------------------------

def test_real_plan_from_funtoken_run_25406869763(tmp_path: Path) -> None:
	"""Plan body excerpted from fun-token-multi-chain run 25406869763
	issue #195. This is the regression case the inlining feature was
	originally written for."""
	plan = (
		"Implementation Plan\n"
		"\n"
		"Planning ref: 81a50d370c on orchestrator/project-193.\n"
		"\n"
		"**Files likely to change**\n"
		"- `contracts/FunOFT.sol`\n"
		"\n"
		"No other file currently needs modification for the scoped cleanup.\n"
		"\n"
		"**Functions or modules to implement**\n"
		"- Remove `FunOFT._debitView(uint256,uint256,uint32)` from "
		"`contracts/FunOFT.sol`.\n"
	)
	assert _run_parser(plan, tmp_path) == ["contracts/FunOFT.sol"]

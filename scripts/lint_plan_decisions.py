#!/usr/bin/env python3
"""Advisory lint for the `## Decisions` convention in `docs/plans/*.md`.

This linter is intentionally fail-open: it emits structured warnings to
stderr and always exits 0. Current rollout scope is limited to the live
`docs/plans/*.md` planning corpus; legacy `docs/completed/*.md` files are
out of scope for this advisory check.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TextIO


DECISIONS_HEADING_RE = re.compile(r"^\s{0,3}##[ \t]+Decisions\s*$")
SECTION_HEADING_RE = re.compile(r"^\s{0,3}(?P<hashes>#{1,2})[ \t]+\S")
THIRD_LEVEL_HEADING_RE = re.compile(r"^\s{0,3}###[ \t]+(?P<title>\S(?:.*\S)?)\s*$")
DECISION_HEADING_RE = re.compile(
	r"^\s{0,3}###[ \t]+(?P<decision_id>D\d+)[ \t]+—[ \t]+(?P<title>\S(?:.*\S)?)\s*$"
)
FENCED_CODE_BLOCK_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
DECISION_FIELD_RE = re.compile(
	r"^\s*[-*+][ \t]+\*\*(?P<field>Chosen|Alternatives considered|Why)(?::)?\*\*(?::)?(?P<inline_value>[ \t]+.*)?$"
)
REQUIRED_DECISION_FIELDS = ("Chosen", "Alternatives considered", "Why")
WARNING_PREFIX = "[lint_plan_decisions]"
BARE_LIST_MARKER_VALUES = frozenset({"-", "*", "+"})


def discover_plan_files(root: Path) -> list[Path]:
	"""Return sorted `docs/plans/*.md` files under the given repository root."""
	plans_directory = root / "docs" / "plans"
	if not plans_directory.is_dir():
		return []
	return sorted(plans_directory.glob("*.md"))


def _emit_advisory_warning(
	warning_text: str, plan_path: Path | None = None, stream: TextIO | None = None
) -> None:
	candidate_streams: list[TextIO | None] = []
	if stream is not None:
		candidate_streams.append(stream)
	candidate_streams.extend((sys.stderr, sys.__stderr__, sys.stdout, sys.__stdout__))
	if plan_path is None:
		formatted_warning = f"::warning::{WARNING_PREFIX} {warning_text}"
	else:
		formatted_warning = f"::warning file={plan_path.as_posix()}::{WARNING_PREFIX} {warning_text}"
	used_stream_ids: set[int] = set()
	for candidate_stream in candidate_streams:
		if candidate_stream is None or id(candidate_stream) in used_stream_ids:
			continue
		used_stream_ids.add(id(candidate_stream))
		try:
			print(formatted_warning, file=candidate_stream)
			return
		except Exception:
			continue


def _is_fence_line(markdown_line: str) -> bool:
	return FENCED_CODE_BLOCK_RE.match(markdown_line) is not None


def _has_meaningful_field_content(markdown_line: str) -> bool:
	stripped_markdown_line = markdown_line.strip()
	return bool(stripped_markdown_line) and stripped_markdown_line not in BARE_LIST_MARKER_VALUES


def _extract_decisions_section(markdown_text: str) -> str | None:
	lines = markdown_text.splitlines()
	section_start_index: int | None = None

	for index, line in enumerate(lines):
		if DECISIONS_HEADING_RE.match(line):
			section_start_index = index + 1
			break

	if section_start_index is None:
		return None

	section_end_index = len(lines)
	inside_fenced_code_block = False
	for index in range(section_start_index, len(lines)):
		line = lines[index]
		if _is_fence_line(line):
			inside_fenced_code_block = not inside_fenced_code_block
			continue
		if inside_fenced_code_block:
			continue
		heading_match = SECTION_HEADING_RE.match(line)
		if heading_match is not None:
			section_end_index = index
			break

	return "\n".join(lines[section_start_index:section_end_index])


def _split_decision_blocks(decisions_section_text: str) -> list[tuple[str, list[str]]]:
	blocks: list[tuple[str, list[str]]] = []
	current_heading_line: str | None = None
	current_block_lines: list[str] = []
	inside_fenced_code_block = False

	for line in decisions_section_text.splitlines():
		if _is_fence_line(line):
			if current_heading_line is not None:
				current_block_lines.append(line)
			inside_fenced_code_block = not inside_fenced_code_block
			continue
		if not inside_fenced_code_block and THIRD_LEVEL_HEADING_RE.match(line):
			if current_heading_line is not None:
				blocks.append((current_heading_line, current_block_lines))
			current_heading_line = line.strip()
			current_block_lines = []
			continue
		if current_heading_line is not None:
			current_block_lines.append(line)

	if current_heading_line is not None:
		blocks.append((current_heading_line, current_block_lines))

	return blocks


def _field_has_content(block_lines: list[str], start_index: int, inline_value: str | None) -> bool:
	if inline_value is not None and _has_meaningful_field_content(inline_value):
		return True

	inside_fenced_code_block = False
	for following_line in block_lines[start_index + 1 :]:
		if _is_fence_line(following_line):
			inside_fenced_code_block = not inside_fenced_code_block
			continue
		if not inside_fenced_code_block and THIRD_LEVEL_HEADING_RE.match(following_line):
			return False
		if not inside_fenced_code_block and DECISION_FIELD_RE.match(following_line):
			return False
		if _has_meaningful_field_content(following_line):
			return True

	return False


def lint_file(plan_path: Path) -> list[str]:
	"""Return advisory warnings for a single plan file."""
	try:
		markdown_text = plan_path.read_text(encoding="utf-8", errors="replace")
	except Exception as exc:
		return [f"could not read plan file ({exc.__class__.__name__}: {exc})"]
	decisions_section_text = _extract_decisions_section(markdown_text)
	if decisions_section_text is None:
		return ["missing `## Decisions` section"]

	decision_blocks = _split_decision_blocks(decisions_section_text)
	if not decision_blocks:
		return ["has `## Decisions` but no `### D<n> — <title>` decision records"]

	warnings: list[str] = []
	for heading_line, block_lines in decision_blocks:
		heading_match = DECISION_HEADING_RE.match(heading_line)
		if heading_match is None:
			warnings.append(
				f"decision heading `{heading_line}` does not match required shape `### D<n> — <title>`"
			)
			continue

		present_fields: set[str] = set()
		for index, line in enumerate(block_lines):
			field_match = DECISION_FIELD_RE.match(line)
			if field_match is None:
				continue
			if _field_has_content(block_lines, index, field_match.group("inline_value")):
				present_fields.add(field_match.group("field"))
		missing_fields = [
			field_name for field_name in REQUIRED_DECISION_FIELDS if field_name not in present_fields
		]
		if missing_fields:
			missing_fields_rendered = ", ".join(f"`{field_name}`" for field_name in missing_fields)
			warnings.append(
				f"{heading_match.group('decision_id')} — {heading_match.group('title')} is missing required bullet(s): {missing_fields_rendered}"
			)

	return warnings


def lint_tree(root: Path) -> list[tuple[Path, str]]:
	"""Return `(path, warning)` pairs for every plan under `docs/plans/*.md`."""
	results: list[tuple[Path, str]] = []
	try:
		plan_paths = discover_plan_files(root)
	except Exception as exc:
		return [
			(
				root / "docs" / "plans",
				f"could not enumerate plan files ({exc.__class__.__name__}: {exc})",
			)
		]
	for plan_path in plan_paths:
		try:
			plan_warnings = lint_file(plan_path)
		except Exception as exc:
			results.append(
				(plan_path, f"could not lint plan file ({exc.__class__.__name__}: {exc})")
			)
			continue
		for warning_text in plan_warnings:
			results.append((plan_path, warning_text))
	return results


def emit_warnings(warnings: list[tuple[Path, str]], stream: TextIO | None = None) -> None:
	for plan_path, warning_text in warnings:
		_emit_advisory_warning(warning_text, plan_path=plan_path, stream=stream)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--root",
		type=Path,
		default=Path("."),
		help="Repository root containing docs/plans/*.md (default: current directory).",
	)
	try:
		args = parser.parse_args(argv)
	except SystemExit as exc:
		if exc.code not in (0, None):
			_emit_advisory_warning(f"argument parsing failed with exit={exc.code}; continuing fail-open")
		return 0

	try:
		emit_warnings(lint_tree(args.root))
	except Exception as exc:
		_emit_advisory_warning(
			f"unexpected linter failure ({exc.__class__.__name__}: {exc}); continuing fail-open"
		)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

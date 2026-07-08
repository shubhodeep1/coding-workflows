#!/usr/bin/env python3
"""Update TREE marker blocks from deterministic repo globs."""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "tools" / "repo_tree" / "config.yaml"
MARKER_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
MARKER_PATTERN = re.compile(r"<!-- TREE:(START|END) id=([A-Za-z0-9_.-]+) -->")

EXIT_OK = 0
EXIT_DIFF = 1
EXIT_MARKER_ERROR = 2


class RepoTreeError(Exception):
	"""Base error type for repo-tree failures."""

	def __init__(self, message: str, *, exit_code: int = EXIT_DIFF):
		super().__init__(message)
		self.exit_code = exit_code


@dataclass(frozen=True)
class TreeSpec:
	file: str
	marker_id: str
	source_glob: str


@dataclass(frozen=True)
class MarkerSpan:
	marker_id: str
	start_index: int
	end_index: int

	@property
	def content_start(self) -> int:
		return self.start_index + 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Update TREE marker blocks in repo documents")
	mode_group = parser.add_mutually_exclusive_group(required=True)
	mode_group.add_argument("--write", action="store_true", help="Write refreshed TREE blocks")
	mode_group.add_argument(
		"--check",
		action="store_true",
		help="Check committed TREE blocks against freshly generated output",
	)
	return parser.parse_args(argv)


def fail(message: str, *, exit_code: int = EXIT_DIFF) -> RepoTreeError:
	return RepoTreeError(message, exit_code=exit_code)


def _relative_display(path: Path, repo_root: Path) -> str:
	try:
		return path.relative_to(repo_root).as_posix()
	except ValueError:
		return path.as_posix()


def _validate_relative_reference(value: str, *, field_name: str, config_display: str) -> None:
	path_value = Path(value)
	if path_value.is_absolute():
		raise fail(f"{config_display}: {field_name} must be repo-relative: {value}")
	if any(part == ".." for part in path_value.parts):
		raise fail(f"{config_display}: {field_name} must not escape the repo root: {value}")


def load_config(path: Path, *, repo_root: Path) -> list[TreeSpec]:
	config_display = _relative_display(path, repo_root)
	try:
		import yaml
	except ModuleNotFoundError:
		raise fail("PyYAML is required for tools/repo_tree/config.yaml")

	try:
		with path.open("r", encoding="utf-8") as handle:
			payload = yaml.safe_load(handle)
	except FileNotFoundError:
		raise fail(f"{config_display}: file not found")
	except yaml.YAMLError as exc:
		raise fail(f"{config_display}: invalid YAML: {exc}")
	except OSError as exc:
		raise fail(f"{config_display}: {exc}")

	if not isinstance(payload, dict):
		raise fail(f"{config_display}: expected top-level mapping")

	tree_rows = payload.get("trees")
	if not isinstance(tree_rows, list):
		raise fail(f"{config_display}: expected top-level 'trees' list")

	seen_file_markers: set[tuple[str, str]] = set()
	specs: list[TreeSpec] = []
	for index, row in enumerate(tree_rows, start=1):
		if not isinstance(row, dict):
			raise fail(f"{config_display}: trees[{index}] must be a mapping")

		file_value = row.get("file")
		marker_id = row.get("marker_id")
		source_glob = row.get("source_glob")
		for field_name, field_value in (
			("file", file_value),
			("marker_id", marker_id),
			("source_glob", source_glob),
		):
			if not isinstance(field_value, str) or not field_value:
				raise fail(f"{config_display}: trees[{index}].{field_name} must be a non-empty string")

		assert isinstance(file_value, str)
		assert isinstance(marker_id, str)
		assert isinstance(source_glob, str)
		_validate_relative_reference(file_value, field_name="file", config_display=config_display)
		_validate_relative_reference(source_glob, field_name="source_glob", config_display=config_display)
		if MARKER_ID_PATTERN.fullmatch(marker_id) is None:
			raise fail(
				f"{config_display}: trees[{index}].marker_id contains invalid characters: {marker_id}"
			)

		file_marker_key = (file_value, marker_id)
		if file_marker_key in seen_file_markers:
			raise fail(
				f"{config_display}: duplicate tree entry for {file_value} marker id {marker_id}",
				exit_code=EXIT_MARKER_ERROR,
			)
		seen_file_markers.add(file_marker_key)
		specs.append(TreeSpec(file=file_value, marker_id=marker_id, source_glob=source_glob))

	return specs


def group_specs_by_file(specs: Sequence[TreeSpec]) -> dict[str, list[TreeSpec]]:
	grouped_specs: dict[str, list[TreeSpec]] = {}
	for spec in specs:
		grouped_specs.setdefault(spec.file, []).append(spec)
	return grouped_specs


def expand_source_glob(source_glob: str, *, repo_root: Path) -> list[str]:
	try:
		matches = [path.relative_to(repo_root).as_posix() for path in repo_root.glob(source_glob) if path.exists()]
	except (OSError, ValueError) as exc:
		raise fail(f"tools/repo_tree/config.yaml: unable to expand source_glob {source_glob!r}: {exc}")
	return sorted(set(matches))


def render_tree_block(paths: Sequence[str]) -> list[str]:
	rendered_lines = ["```\n"]
	rendered_lines.extend(f"{path}\n" for path in paths)
	rendered_lines.append("```\n")
	return rendered_lines


def parse_marker_spans(lines: Sequence[str], *, file_display: str) -> dict[str, MarkerSpan]:
	marker_spans: dict[str, MarkerSpan] = {}
	start_ids_seen: set[str] = set()
	end_ids_seen: set[str] = set()
	open_marker_id: str | None = None
	open_start_index: int | None = None

	for index, raw_line in enumerate(lines):
		line = raw_line.rstrip("\n")
		if line.endswith("\r"):
			line = line[:-1]
		match = MARKER_PATTERN.fullmatch(line)
		if match is None:
			continue

		marker_kind, marker_id = match.groups()
		if marker_kind == "START":
			if marker_id in start_ids_seen:
				raise fail(
					f"{file_display}: duplicate TREE:START marker id={marker_id}",
					exit_code=EXIT_MARKER_ERROR,
				)
			if open_marker_id is not None:
				raise fail(
					f"{file_display}: TREE:START id={marker_id} found before closing TREE:START id={open_marker_id}",
					exit_code=EXIT_MARKER_ERROR,
				)
			start_ids_seen.add(marker_id)
			open_marker_id = marker_id
			open_start_index = index
			continue

		if marker_id in end_ids_seen:
			raise fail(
				f"{file_display}: duplicate TREE:END marker id={marker_id}",
				exit_code=EXIT_MARKER_ERROR,
			)
		if open_marker_id is None:
			raise fail(
				f"{file_display}: TREE:END id={marker_id} has no matching TREE:START",
				exit_code=EXIT_MARKER_ERROR,
			)
		if marker_id != open_marker_id or open_start_index is None:
			raise fail(
				f"{file_display}: TREE:END id={marker_id} does not match open TREE:START id={open_marker_id}",
				exit_code=EXIT_MARKER_ERROR,
			)
		end_ids_seen.add(marker_id)
		marker_spans[marker_id] = MarkerSpan(
			marker_id=marker_id,
			start_index=open_start_index,
			end_index=index,
		)
		open_marker_id = None
		open_start_index = None

	if open_marker_id is not None:
		raise fail(
			f"{file_display}: TREE:START id={open_marker_id} has no matching TREE:END",
			exit_code=EXIT_MARKER_ERROR,
		)

	return marker_spans


def build_updated_file_text(target_file: str, specs: Sequence[TreeSpec], *, repo_root: Path) -> tuple[str, str]:
	target_path = repo_root / target_file
	file_display = target_file
	try:
		existing_text = target_path.read_text(encoding="utf-8")
	except FileNotFoundError:
		raise fail(f"{file_display}: file not found")
	except OSError as exc:
		raise fail(f"{file_display}: unable to read file: {exc}")

	existing_lines = existing_text.splitlines(keepends=True)
	marker_spans = parse_marker_spans(existing_lines, file_display=file_display)
	expected_marker_ids = {spec.marker_id for spec in specs}
	missing_marker_ids = sorted(expected_marker_ids - set(marker_spans))
	if missing_marker_ids:
		raise fail(
			f"{file_display}: missing TREE marker pair(s): {', '.join(missing_marker_ids)}",
			exit_code=EXIT_MARKER_ERROR,
		)

	replacements = {
		spec.marker_id: render_tree_block(expand_source_glob(spec.source_glob, repo_root=repo_root))
		for spec in specs
	}
	ordered_spans = sorted(
		(marker_spans[marker_id] for marker_id in expected_marker_ids),
		key=lambda span: span.start_index,
	)

	updated_lines: list[str] = []
	cursor = 0
	for marker_span in ordered_spans:
		updated_lines.extend(existing_lines[cursor : marker_span.content_start])
		updated_lines.extend(replacements[marker_span.marker_id])
		cursor = marker_span.end_index
	updated_lines.extend(existing_lines[cursor:])
	return existing_text, "".join(updated_lines)


def write_output(target_file: str, updated_text: str, *, repo_root: Path) -> None:
	target_path = repo_root / target_file
	try:
		target_path.write_text(updated_text, encoding="utf-8")
	except OSError as exc:
		raise fail(f"{target_file}: unable to write updated file: {exc}")


def emit_diff(target_file: str, existing_text: str, updated_text: str) -> None:
	diff_text = "".join(
		difflib.unified_diff(
			existing_text.splitlines(keepends=True),
			updated_text.splitlines(keepends=True),
			fromfile=target_file,
			tofile=f"{target_file} (generated)",
		)
	)
	if diff_text:
		sys.stderr.write(diff_text if diff_text.endswith("\n") else diff_text + "\n")
	print(f"::error::FAIL: {target_file} is out of date; run make generate", file=sys.stderr)


def run(argv: Sequence[str] | None = None, *, repo_root: Path = REPO_ROOT) -> int:
	args = parse_args(argv)
	specs = load_config(CONFIG_PATH if repo_root == REPO_ROOT else repo_root / "tools" / "repo_tree" / "config.yaml", repo_root=repo_root)
	grouped_specs = group_specs_by_file(specs)

	status = EXIT_OK
	for target_file, file_specs in grouped_specs.items():
		existing_text, updated_text = build_updated_file_text(target_file, file_specs, repo_root=repo_root)
		if args.write:
			if updated_text != existing_text:
				write_output(target_file, updated_text, repo_root=repo_root)
			continue
		if updated_text != existing_text:
			emit_diff(target_file, existing_text, updated_text)
			status = EXIT_DIFF
	return status


def main(argv: Sequence[str] | None = None, *, repo_root: Path = REPO_ROOT) -> int:
	try:
		return run(argv, repo_root=repo_root)
	except RepoTreeError as exc:
		print(f"::error::FAIL: {exc}", file=sys.stderr)
		return exc.exit_code


if __name__ == "__main__":
	raise SystemExit(main())

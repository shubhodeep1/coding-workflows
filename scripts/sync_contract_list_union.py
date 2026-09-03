#!/usr/bin/env python3
"""Deterministically merge append-only contract entrypoint-list conflicts."""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any


CONTRACT_PATH_RE = re.compile(r"^db/contracts/[^/]+\.ya?ml$")
LIST_ITEM_RE = re.compile(r"^([ \t]+)- \S.*(?:\r?\n)?$")
TOP_LEVEL_KEY_RE = re.compile(r"^([^\s:#][^:]*):(?:\s*(?:#.*)?)?(?:\r?\n)?$")
MARKER_RE = re.compile(r"^(?:<<<<<<<|=======|>>>>>>>)", re.MULTILINE)
ALLOWED_KEYS = {"read_entrypoints", "write_entrypoints"}


class IneligibleError(Exception):
	"""A conflict shape is unsafe for deterministic resolution."""

	def __init__(self, reason: str) -> None:
		super().__init__(reason)
		self.reason = reason


def _parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser()
	parser.add_argument("--path", required=True)
	parser.add_argument("--base", required=True)
	parser.add_argument("--ours", required=True)
	parser.add_argument("--theirs", required=True)
	parser.add_argument("--out", required=True)
	return parser.parse_args()


def _load_yaml_module() -> Any:
	try:
		return importlib.import_module("yaml")
	except ImportError as error:
		raise IneligibleError("pyyaml_missing") from error


def _safe_load(yaml_module: Any, text: str) -> Any:
	try:
		return yaml_module.safe_load(text)
	except Exception as error:
		raise IneligibleError("yaml_parse_failed") from error


def _nearest_top_level_key(lines: list[str], marker_index: int) -> str | None:
	for preceding_line in reversed(lines[:marker_index]):
		match = TOP_LEVEL_KEY_RE.match(preceding_line)
		if match:
			return match.group(1)
	return None


def _parse_and_merge_hunks(marked_text: str) -> tuple[str, str, set[str]]:
	lines = marked_text.splitlines(keepends=True)
	merged_lines: list[str] = []
	auto_merged_lines: list[str] = []
	affected_keys: set[str] = set()
	line_index = 0

	while line_index < len(lines):
		if not lines[line_index].startswith("<<<<<<< ours"):
			merged_lines.append(lines[line_index])
			auto_merged_lines.append(lines[line_index])
			line_index += 1
			continue

		marker_index = line_index
		line_index += 1
		ours_lines: list[str] = []
		while line_index < len(lines) and not lines[line_index].startswith("======="):
			ours_lines.append(lines[line_index])
			line_index += 1
		if line_index >= len(lines):
			raise IneligibleError("markers_remain")

		line_index += 1
		theirs_lines: list[str] = []
		while line_index < len(lines) and not lines[line_index].startswith(">>>>>>> theirs"):
			theirs_lines.append(lines[line_index])
			line_index += 1
		if line_index >= len(lines):
			raise IneligibleError("markers_remain")
		line_index += 1

		if not ours_lines or not theirs_lines:
			raise IneligibleError("hunk_one_sided")
		entrypoint_key = _nearest_top_level_key(lines, marker_index)
		if entrypoint_key not in ALLOWED_KEYS:
			raise IneligibleError("hunk_outside_entrypoints")

		common_indent: str | None = None
		for candidate_line in ours_lines + theirs_lines:
			item_match = LIST_ITEM_RE.match(candidate_line)
			if not item_match:
				raise IneligibleError("hunk_non_list_line")
			if common_indent is None:
				common_indent = item_match.group(1)
			elif item_match.group(1) != common_indent:
				raise IneligibleError("hunk_non_list_line")

		merged_lines.extend(ours_lines)
		merged_lines.extend(line for line in theirs_lines if line not in ours_lines)
		auto_merged_lines.extend(ours_lines)
		affected_keys.add(entrypoint_key)

	merged_text = "".join(merged_lines)
	if MARKER_RE.search(merged_text):
		raise IneligibleError("markers_remain")
	return merged_text, "".join(auto_merged_lines), affected_keys


def _contains_equal(values: list[Any], candidate: Any) -> bool:
	return any(existing == candidate for existing in values)


def _has_duplicates(values: list[Any]) -> bool:
	seen_values: list[Any] = []
	for value in values:
		if _contains_equal(seen_values, value):
			return True
		seen_values.append(value)
	return False


def _is_subsequence(needles: list[Any], haystack: list[Any]) -> bool:
	haystack_index = 0
	for needle in needles:
		while haystack_index < len(haystack) and haystack[haystack_index] != needle:
			haystack_index += 1
		if haystack_index >= len(haystack):
			return False
		haystack_index += 1
	return True


def _deduplicated_union(ours_values: list[Any], theirs_values: list[Any]) -> list[Any]:
	union_values = list(ours_values)
	for value in theirs_values:
		if not _contains_equal(union_values, value):
			union_values.append(value)
	return union_values


def _same_unique_members(first_values: list[Any], second_values: list[Any]) -> bool:
	return len(first_values) == len(second_values) and all(
		_contains_equal(second_values, value) for value in first_values
	)


def _validate_result(
	yaml_module: Any,
	base_text: str,
	ours_text: str,
	theirs_text: str,
	merged_text: str,
	auto_merged_text: str,
	affected_keys: set[str],
) -> None:
	base_data = _safe_load(yaml_module, base_text)
	ours_data = _safe_load(yaml_module, ours_text)
	theirs_data = _safe_load(yaml_module, theirs_text)
	merged_data = _safe_load(yaml_module, merged_text)
	auto_merged_data = _safe_load(yaml_module, auto_merged_text)
	if not all(isinstance(data, dict) for data in (base_data, ours_data, theirs_data, merged_data, auto_merged_data)):
		raise IneligibleError("list_not_union")

	for affected_key in affected_keys:
		lists = [
			base_data.get(affected_key),
			ours_data.get(affected_key),
			theirs_data.get(affected_key),
			merged_data.get(affected_key),
		]
		if not all(isinstance(value, list) for value in lists):
			raise IneligibleError("list_not_union")
		base_values, ours_values, theirs_values, merged_values = lists
		if any(_has_duplicates(value) for value in (base_values, ours_values, theirs_values, merged_values)):
			raise IneligibleError("list_not_union")
		if not _is_subsequence(base_values, ours_values) or not _is_subsequence(base_values, theirs_values):
			raise IneligibleError("list_not_union")
		if not _same_unique_members(
			merged_values,
			_deduplicated_union(ours_values, theirs_values),
		):
			raise IneligibleError("list_not_union")

	for top_level_key, auto_merged_value in auto_merged_data.items():
		if top_level_key not in affected_keys and merged_data.get(top_level_key) != auto_merged_value:
			raise IneligibleError("list_not_union")
	if set(merged_data) != set(auto_merged_data):
		raise IneligibleError("list_not_union")


def _write_output(output_path: Path, merged_text: str) -> None:
	output_path.parent.mkdir(parents=True, exist_ok=True)
	file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output_path.name}.", dir=output_path.parent)
	try:
		with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as temporary_file:
			temporary_file.write(merged_text)
		os.replace(temporary_name, output_path)
	except Exception:
		try:
			os.unlink(temporary_name)
		except OSError:
			pass
		raise


def _merge(args: argparse.Namespace) -> None:
	if not CONTRACT_PATH_RE.fullmatch(args.path):
		raise IneligibleError("path_not_contract")

	base_path = Path(args.base)
	ours_path = Path(args.ours)
	theirs_path = Path(args.theirs)
	base_text = base_path.read_text(encoding="utf-8")
	if not base_text:
		raise IneligibleError("base_missing")
	ours_text = ours_path.read_text(encoding="utf-8")
	theirs_text = theirs_path.read_text(encoding="utf-8")

	merge_result = subprocess.run(
		[
			"git",
			"merge-file",
			"-p",
			"--marker-size=7",
			"-L",
			"ours",
			"-L",
			"base",
			"-L",
			"theirs",
			str(ours_path),
			str(base_path),
			str(theirs_path),
		],
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	if merge_result.returncode < 0 or merge_result.returncode > 127:
		raise RuntimeError("git merge-file failed")

	merged_text, auto_merged_text, affected_keys = _parse_and_merge_hunks(merge_result.stdout)
	yaml_module = _load_yaml_module()
	_validate_result(
		yaml_module,
		base_text,
		ours_text,
		theirs_text,
		merged_text,
		auto_merged_text,
		affected_keys,
	)
	_write_output(Path(args.out), merged_text)


def main() -> int:
	args = _parse_args()
	try:
		_merge(args)
	except IneligibleError as error:
		print(f"SYNC_LIST_UNION_INELIGIBLE_V1: path={args.path} reason={error.reason}", file=sys.stderr)
		return 3
	except Exception as error:
		print(f"sync_contract_list_union: unexpected error: {type(error).__name__}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

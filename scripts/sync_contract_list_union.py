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
MARKER_RE = re.compile(r"^(?:<<<<<<<|=======|>>>>>>>)", re.MULTILINE)
ALLOWED_KEYS = {"read_entrypoints", "write_entrypoints"}
MAX_INPUT_BYTES = 1_048_576
MAX_ENTRYPOINT_ENTRIES = 4_096
MAX_ENTRYPOINT_LENGTH = 4_096


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
	class _UniqueKeySafeLoader(yaml_module.SafeLoader):
		pass

	def _construct_unique_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
		loader.flatten_mapping(node)
		constructed_mapping: dict[Any, Any] = {}
		for mapping_key_node, mapping_value_node in node.value:
			mapping_key = loader.construct_object(mapping_key_node, deep=deep)
			if mapping_key in constructed_mapping:
				raise IneligibleError("duplicate_mapping_key")
			constructed_mapping[mapping_key] = loader.construct_object(mapping_value_node, deep=deep)
		return constructed_mapping

	_UniqueKeySafeLoader.add_constructor(
		yaml_module.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
		_construct_unique_mapping,
	)
	loader: Any | None = None
	try:
		loader = _UniqueKeySafeLoader(text)
		return loader.get_single_data()
	except IneligibleError:
		raise
	except Exception as error:
		raise IneligibleError("yaml_parse_failed") from error
	finally:
		if loader is not None:
			loader.dispose()


def _read_bounded_text(input_path: Path) -> str:
	with input_path.open("rb") as input_file:
		input_bytes = input_file.read(MAX_INPUT_BYTES + 1)
	if len(input_bytes) > MAX_INPUT_BYTES:
		raise IneligibleError("input_too_large")
	try:
		return input_bytes.decode("utf-8")
	except UnicodeDecodeError as error:
		raise IneligibleError("input_not_utf8") from error


def _parse_and_merge_hunks(marked_text: str) -> tuple[str, str, list[tuple[int, int]]]:
	lines = marked_text.splitlines(keepends=True)
	merged_lines: list[str] = []
	auto_merged_lines: list[str] = []
	hunk_line_ranges: list[tuple[int, int]] = []
	line_index = 0

	while line_index < len(lines):
		if not lines[line_index].startswith("<<<<<<< ours"):
			merged_lines.append(lines[line_index])
			auto_merged_lines.append(lines[line_index])
			line_index += 1
			continue

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
		common_indent: str | None = None
		for candidate_line in ours_lines + theirs_lines:
			item_match = LIST_ITEM_RE.match(candidate_line)
			if not item_match:
				raise IneligibleError("hunk_non_list_line")
			if common_indent is None:
				common_indent = item_match.group(1)
			elif item_match.group(1) != common_indent:
				raise IneligibleError("hunk_non_list_line")

		hunk_start_line = len(merged_lines)
		merged_lines.extend(ours_lines)
		merged_lines.extend(line for line in theirs_lines if line not in ours_lines)
		hunk_line_ranges.append((hunk_start_line, len(merged_lines)))
		auto_merged_lines.extend(ours_lines)

	merged_text = "".join(merged_lines)
	if MARKER_RE.search(merged_text):
		raise IneligibleError("markers_remain")
	return merged_text, "".join(auto_merged_lines), hunk_line_ranges


def _validate_hunk_parent_nodes(
	yaml_module: Any,
	merged_text: str,
	hunk_line_ranges: list[tuple[int, int]],
) -> set[str]:
	try:
		root_node = yaml_module.compose(merged_text, Loader=yaml_module.SafeLoader)
	except Exception as error:
		raise IneligibleError("yaml_parse_failed") from error
	if not isinstance(root_node, yaml_module.nodes.MappingNode):
		raise IneligibleError("hunk_outside_entrypoints")
	allowed_sequences: list[tuple[str, Any]] = []
	for key_node, value_node in root_node.value:
		if (
			isinstance(key_node, yaml_module.nodes.ScalarNode)
			and key_node.value in ALLOWED_KEYS
			and isinstance(value_node, yaml_module.nodes.SequenceNode)
		):
			allowed_sequences.append((key_node.value, value_node))
	affected_keys: set[str] = set()
	for hunk_start_line, hunk_end_line in hunk_line_ranges:
		parent_keys = [
			key
			for key, sequence_node in allowed_sequences
			if hunk_start_line >= sequence_node.start_mark.line
			and hunk_end_line <= sequence_node.end_mark.line
		]
		if len(parent_keys) != 1:
			raise IneligibleError("hunk_outside_entrypoints")
		affected_keys.add(parent_keys[0])
	return affected_keys


def _deep_equal_cycle_safe(
	first_value: Any,
	second_value: Any,
	seen_pairs: set[tuple[int, int]] | None = None,
) -> bool:
	if first_value is second_value:
		return True
	if type(first_value) is not type(second_value):
		return False
	if isinstance(first_value, (str, int, float, bool, type(None), bytes)):
		return first_value == second_value
	if seen_pairs is None:
		seen_pairs = set()
	pair = (id(first_value), id(second_value))
	if pair in seen_pairs:
		return True
	seen_pairs.add(pair)
	if isinstance(first_value, list):
		return len(first_value) == len(second_value) and all(
			_deep_equal_cycle_safe(first_item, second_item, seen_pairs)
			for first_item, second_item in zip(first_value, second_value)
		)
	if isinstance(first_value, dict):
		if len(first_value) != len(second_value) or set(first_value) != set(second_value):
			return False
		return all(
			_deep_equal_cycle_safe(first_value[key], second_value[key], seen_pairs)
			for key in first_value
		)
	return first_value == second_value


def _validated_entrypoint_lists(document: dict[Any, Any]) -> dict[str, list[str]]:
	validated_lists: dict[str, list[str]] = {}
	for entrypoint_key in ALLOWED_KEYS:
		if entrypoint_key not in document:
			continue
		entrypoint_values = document[entrypoint_key]
		if not isinstance(entrypoint_values, list):
			raise IneligibleError("entrypoints_not_string_list")
		if len(entrypoint_values) > MAX_ENTRYPOINT_ENTRIES:
			raise IneligibleError("entrypoint_list_too_large")
		for entrypoint_value in entrypoint_values:
			if not isinstance(entrypoint_value, str):
				raise IneligibleError("entrypoint_not_string")
			if len(entrypoint_value) > MAX_ENTRYPOINT_LENGTH:
				raise IneligibleError("entrypoint_too_long")
		validated_lists[entrypoint_key] = entrypoint_values
	return validated_lists


def _has_duplicates(values: list[str]) -> bool:
	return len(values) != len(set(values))


def _is_subsequence(needles: list[str], haystack: list[str]) -> bool:
	haystack_index = 0
	for needle in needles:
		while haystack_index < len(haystack) and haystack[haystack_index] != needle:
			haystack_index += 1
		if haystack_index >= len(haystack):
			return False
		haystack_index += 1
	return True


def _deduplicated_union(ours_values: list[str], theirs_values: list[str]) -> list[str]:
	union_values = list(ours_values)
	seen_values = set(ours_values)
	for value in theirs_values:
		if value not in seen_values:
			union_values.append(value)
			seen_values.add(value)
	return union_values


def _same_unique_members(first_values: list[str], second_values: list[str]) -> bool:
	return len(first_values) == len(second_values) and set(first_values) == set(second_values)


def _validate_result(
	yaml_module: Any,
	base_text: str,
	ours_text: str,
	theirs_text: str,
	merged_text: str,
	auto_merged_text: str,
	hunk_line_ranges: list[tuple[int, int]],
) -> None:
	base_data = _safe_load(yaml_module, base_text)
	ours_data = _safe_load(yaml_module, ours_text)
	theirs_data = _safe_load(yaml_module, theirs_text)
	merged_data = _safe_load(yaml_module, merged_text)
	auto_merged_data = _safe_load(yaml_module, auto_merged_text)
	if not all(isinstance(data, dict) for data in (base_data, ours_data, theirs_data, merged_data, auto_merged_data)):
		raise IneligibleError("list_not_union")
	validated_documents = [
		_validated_entrypoint_lists(data)
		for data in (base_data, ours_data, theirs_data, merged_data, auto_merged_data)
	]
	affected_keys = _validate_hunk_parent_nodes(yaml_module, merged_text, hunk_line_ranges)

	for affected_key in affected_keys:
		lists = [
			validated_document.get(affected_key)
			for validated_document in validated_documents[:4]
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

	merged_unaffected = {key: value for key, value in merged_data.items() if key not in ALLOWED_KEYS}
	auto_merged_unaffected = {key: value for key, value in auto_merged_data.items() if key not in ALLOWED_KEYS}
	if not _deep_equal_cycle_safe(merged_unaffected, auto_merged_unaffected):
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
	base_text = _read_bounded_text(base_path)
	if not base_text:
		raise IneligibleError("base_missing")
	ours_text = _read_bounded_text(ours_path)
	theirs_text = _read_bounded_text(theirs_path)

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

	merged_text, auto_merged_text, hunk_line_ranges = _parse_and_merge_hunks(merge_result.stdout)
	yaml_module = _load_yaml_module()
	_validate_result(
		yaml_module,
		base_text,
		ours_text,
		theirs_text,
		merged_text,
		auto_merged_text,
		hunk_line_ranges,
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

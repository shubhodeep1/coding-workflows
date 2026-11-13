#!/usr/bin/env python3
"""Deterministic contract-list union for orchestrator integration syncs.

Stuck-states P12 (Refs #8291), design doc
``docs/plans/orchestrator-sync-contract-list-union-plan.md`` ("Helper
contract"). The poller (``sync_contract_list_union_merge`` in
``scripts/orchestrate_poll_process.sh``) calls this helper once per
unmerged ``db/contracts/*.yml`` path of a real ``git merge --no-commit``
of the default branch into an ``orchestrator/project-*`` branch. When
every conflict hunk only appends entries to a ``read_entrypoints:`` or
``write_entrypoints:`` list, the helper writes the union of both sides;
otherwise it declares the path ineligible and the poller falls back to
the existing LLM conflict resolver.

Usage::

	python3 scripts/sync_contract_list_union.py \\
		--path db/contracts/<name>.yml \\
		--base <stage-1 file or empty file> \\
		--ours <stage-2 file> \\
		--theirs <stage-3 file> \\
		--out <merged file>

Exit codes:

	0  merged and validated; ``--out`` written.
	3  ineligible; nothing written and one line
		``SYNC_LIST_UNION_INELIGIBLE_V1: path=<p> reason=<token>`` on stderr.
	1  unexpected error; nothing written.

Reason tokens (stable): ``path_not_contract``, ``base_missing``,
``pyyaml_missing``, ``hunk_outside_entrypoints``, ``hunk_non_list_line``,
``hunk_one_sided``, ``yaml_parse_failed``, ``list_not_union``,
``markers_remain``.

Security: the contract blobs are untrusted repository content. They are
never executed or evaluated; parsing uses ``yaml.safe_load`` only. The one
subprocess is ``git merge-file``, run with an argument list (no shell).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

try:
	import yaml
except Exception:  # pragma: no cover - fail-open dependency guard
	yaml = None


EXIT_MERGED = 0
EXIT_ERROR = 1
EXIT_INELIGIBLE = 3

CONTRACT_PATH_RE = re.compile(r"^db/contracts/[^/\n\r]+\.ya?ml$")
LIST_LINE_RE = re.compile(r"^([ \t]+)- \S")
ENTRYPOINT_KEY_RE = re.compile(r"^(read_entrypoints|write_entrypoints):[ \t]*(#.*)?$")
# Top-level mapping key: column 0, not blank, not a comment, not a list
# item or a document marker. Only used to reject duplicate top-level keys.
TOP_LEVEL_KEY_RE = re.compile(r"^([^\s#\-][^:]*):(?:[ \t]|$)")
MARKER_SIZE = 7
OPEN_RE = re.compile(r"^<{7}(?: |$)")
BASE_RE = re.compile(r"^\|{7}(?: |$)")
SEP_RE = re.compile(r"^={7}$")
CLOSE_RE = re.compile(r"^>{7}(?: |$)")


class Ineligible(Exception):
	"""Raised with a stable reason token when the path cannot be merged."""

	def __init__(self, reason: str):
		super().__init__(reason)
		self.reason = reason


def _strip_eol(line: str) -> str:
	return line.rstrip("\r\n")


def _is_marker(line: str) -> bool:
	text = _strip_eol(line)
	return bool(OPEN_RE.match(text) or BASE_RE.match(text) or SEP_RE.match(text) or CLOSE_RE.match(text))


def _safe_display(value: str) -> str:
	return "".join(ch if ch.isprintable() and ch not in " \t" else "?" for ch in value)[:300]


def _read_text(path: str) -> str:
	with open(path, "rb") as handle:
		raw = handle.read()
	try:
		return raw.decode("utf-8")
	except UnicodeDecodeError as exc:
		raise Ineligible("yaml_parse_failed") from exc


def _run_merge_file(ours: str, base: str, theirs: str) -> str:
	# merge.conflictStyle is pinned so a diff3/zdiff3 user config cannot
	# change the marker layout; the parser still tolerates a base section.
	cmd = [
		"git", "-c", "merge.conflictStyle=merge", "merge-file", "-p",
		"--marker-size", str(MARKER_SIZE),
		"-L", "ours", "-L", "base", "-L", "theirs",
		os.path.abspath(ours), os.path.abspath(base), os.path.abspath(theirs),
	]
	proc = subprocess.run(cmd, capture_output=True, check=False)
	if proc.returncode < 0 or proc.returncode > 127:
		raise RuntimeError(f"git merge-file failed with exit {proc.returncode}")
	try:
		return proc.stdout.decode("utf-8")
	except UnicodeDecodeError as exc:
		raise Ineligible("yaml_parse_failed") from exc


def _nearest_top_level_line(lines: list[str]) -> str | None:
	for line in reversed(lines):
		text = _strip_eol(line)
		if not text.strip():
			continue
		if text[0] in " \t":
			continue
		if text.startswith("#"):
			continue
		return text
	return None


def _resolve(marked: str) -> tuple[str, str, str, set[str]]:
	"""Return (merged, ours_resolved, theirs_resolved, affected_keys).

	``ours_resolved`` / ``theirs_resolved`` take one side of every hunk and
	keep git's auto-merged text everywhere else; they are only used for the
	"nothing outside the lists changed" post-check.
	"""
	merged: list[str] = []
	ours_resolved: list[str] = []
	theirs_resolved: list[str] = []
	affected: set[str] = set()
	lines = marked.splitlines(keepends=True)
	index = 0
	total = len(lines)
	while index < total:
		line = lines[index]
		if not OPEN_RE.match(_strip_eol(line)):
			if _is_marker(line):
				raise Ineligible("markers_remain")
			merged.append(line)
			ours_resolved.append(line)
			theirs_resolved.append(line)
			index += 1
			continue
		ours_side: list[str] = []
		theirs_side: list[str] = []
		section = "ours"
		index += 1
		closed = False
		while index < total:
			current = lines[index]
			text = _strip_eol(current)
			if section in ("ours", "base") and BASE_RE.match(text):
				section = "base"
			elif section in ("ours", "base") and SEP_RE.match(text):
				section = "theirs"
			elif section == "theirs" and CLOSE_RE.match(text):
				closed = True
				index += 1
				break
			elif _is_marker(current):
				raise Ineligible("markers_remain")
			elif section == "ours":
				ours_side.append(current)
			elif section == "theirs":
				theirs_side.append(current)
			index += 1
		if not closed:
			raise Ineligible("markers_remain")
		if not ours_side or not theirs_side:
			raise Ineligible("hunk_one_sided")
		indents = set()
		for hunk_line in ours_side + theirs_side:
			match = LIST_LINE_RE.match(_strip_eol(hunk_line))
			if not match:
				raise Ineligible("hunk_non_list_line")
			indents.add(match.group(1))
		if len(indents) != 1:
			raise Ineligible("hunk_non_list_line")
		key_line = _nearest_top_level_line(merged)
		key_match = ENTRYPOINT_KEY_RE.match(key_line or "")
		if not key_match:
			raise Ineligible("hunk_outside_entrypoints")
		affected.add(key_match.group(1))
		ours_texts = {_strip_eol(item) for item in ours_side}
		combined = list(ours_side)
		for item in theirs_side:
			if _strip_eol(item) not in ours_texts:
				combined.append(item)
		# Keep the line structure intact when the last ours line had no
		# trailing newline (end of file inside a hunk).
		combined = [item if item.endswith("\n") else item + "\n" for item in combined]
		merged.extend(combined)
		ours_resolved.extend(ours_side)
		theirs_resolved.extend(theirs_side)
	return "".join(merged), "".join(ours_resolved), "".join(theirs_resolved), affected


def _load_mapping(text: str) -> dict:
	try:
		data = yaml.safe_load(text)
	except Exception as exc:
		raise Ineligible("yaml_parse_failed") from exc
	if not isinstance(data, dict):
		raise Ineligible("yaml_parse_failed")
	keys = [match.group(1) for match in (TOP_LEVEL_KEY_RE.match(line) for line in text.splitlines()) if match]
	if len(keys) != len(set(keys)):
		# safe_load keeps the last duplicate silently; refuse instead.
		raise Ineligible("yaml_parse_failed")
	return data


def _canonical(value) -> str:
	return json.dumps(value, sort_keys=True, default=str)


def _validate(merged: str, ours_text: str, theirs_text: str, ours_resolved: str, theirs_resolved: str, affected: set[str], base_text: str | None = None) -> None:
	for line in merged.splitlines():
		if _is_marker(line):
			raise Ineligible("markers_remain")
	merged_data = _load_mapping(merged)
	ours_data = _load_mapping(ours_text)
	theirs_data = _load_mapping(theirs_text)
	ours_resolved_data = _load_mapping(ours_resolved)
	theirs_resolved_data = _load_mapping(theirs_resolved)
	for key in sorted(affected):
		merged_list = merged_data.get(key)
		ours_list = ours_data.get(key)
		theirs_list = theirs_data.get(key)
		if not all(isinstance(value, list) for value in (merged_list, ours_list, theirs_list)):
			raise Ineligible("list_not_union")
		merged_items = [_canonical(item) for item in merged_list]
		if len(merged_items) != len(set(merged_items)):
			raise Ineligible("list_not_union")
		expected = {_canonical(item) for item in ours_list} | {_canonical(item) for item in theirs_list}
		if set(merged_items) != expected:
			raise Ineligible("list_not_union")
	# Append-only: every merge-base entry of an affected list must survive on
	# both sides. A hunk where a side replaced or removed a base entry (both
	# sides replacing the same entry, or delete-plus-append) is not an append
	# and goes to the LLM resolver instead of being merged as a union.
	if base_text is not None and affected:
		base_data = _load_mapping(base_text)
		for key in sorted(affected):
			base_list = base_data.get(key)
			# A bare key (``read_entrypoints:`` with no items) or an absent key
			# has no base entries, so the append-only rule holds vacuously.
			if base_list is None:
				base_list = []
			elif not isinstance(base_list, list):
				raise Ineligible("list_not_union")
			base_items = {_canonical(item) for item in base_list}
			ours_items = {_canonical(item) for item in ours_data.get(key)}
			theirs_items = {_canonical(item) for item in theirs_data.get(key)}
			if not (base_items <= ours_items and base_items <= theirs_items):
				raise Ineligible("list_not_union")
	# Every other top-level key must equal git's auto-merged value. The
	# token is list_not_union because the merge would change more than the
	# union of the entrypoint lists.
	all_keys = set(merged_data) | set(ours_resolved_data) | set(theirs_resolved_data)
	for key in all_keys - affected:
		if key not in merged_data or key not in ours_resolved_data or key not in theirs_resolved_data:
			raise Ineligible("list_not_union")
		value = _canonical(merged_data[key])
		if value != _canonical(ours_resolved_data[key]) or value != _canonical(theirs_resolved_data[key]):
			raise Ineligible("list_not_union")


def merge_contract(path: str, base: str, ours: str, theirs: str) -> str:
	"""Return the merged text or raise Ineligible / another exception."""
	if not CONTRACT_PATH_RE.match(path or "") or ".." in path.split("/"):
		raise Ineligible("path_not_contract")
	if not base or not os.path.isfile(base) or os.path.getsize(base) == 0:
		raise Ineligible("base_missing")
	if yaml is None:
		raise Ineligible("pyyaml_missing")
	base_text = _read_text(base)
	ours_text = _read_text(ours)
	theirs_text = _read_text(theirs)
	marked = _run_merge_file(ours, base, theirs)
	merged, ours_resolved, theirs_resolved, affected = _resolve(marked)
	_validate(merged, ours_text, theirs_text, ours_resolved, theirs_resolved, affected, base_text)
	return merged


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Deterministic db/contracts entrypoint-list union for integration syncs.")
	parser.add_argument("--path", required=True, help="Repository-relative contract path (db/contracts/<name>.yml).")
	parser.add_argument("--base", required=True, help="Merge-base (stage 1) file; empty when absent.")
	parser.add_argument("--ours", required=True, help="Integration-side (stage 2) file.")
	parser.add_argument("--theirs", required=True, help="Default-branch-side (stage 3) file.")
	parser.add_argument("--out", required=True, help="Where to write the merged file on success.")
	args = parser.parse_args(argv)
	try:
		merged = merge_contract(args.path, args.base, args.ours, args.theirs)
	except Ineligible as exc:
		print(f"SYNC_LIST_UNION_INELIGIBLE_V1: path={_safe_display(args.path)} reason={exc.reason}", file=sys.stderr)
		return EXIT_INELIGIBLE
	except Exception as exc:  # noqa: BLE001 - any other failure is an error exit
		print(f"sync_contract_list_union: unexpected error: {type(exc).__name__}", file=sys.stderr)
		return EXIT_ERROR
	try:
		with open(args.out, "w", encoding="utf-8", newline="") as handle:
			handle.write(merged)
	except OSError as exc:
		print(f"sync_contract_list_union: cannot write output: {type(exc).__name__}", file=sys.stderr)
		return EXIT_ERROR
	return EXIT_MERGED


if __name__ == "__main__":
	sys.exit(main())

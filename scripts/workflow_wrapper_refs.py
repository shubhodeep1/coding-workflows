#!/usr/bin/env python3
"""Render consumer workflow wrappers with immutable reusable-workflow refs."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence


_RELEASE_SHA_RE = re.compile(r"[0-9a-fA-F]{40}\Z")
_REUSABLE_WORKFLOW_REF_RE = re.compile(
	r"^(?P<prefix>[ \t]*uses:[ \t]+)"
	r"(?P<path>shubhodeep1/coding-workflows/\.github/workflows/"
	r"[A-Za-z0-9_.-]+\.yml)@stable\b(?:[ \t]+#[^\r\n]*)?$",
	re.MULTILINE,
)
# Support checkouts of this repository that a consumer wrapper runs code from.
# Their canonical `ref: stable` is pinned to the same release SHA as the
# reusable-workflow `uses:` refs (issue #7209). Only the listed paths are
# pinned; other checkouts keep their ref.
_SUPPORT_REPOSITORY = "shubhodeep1/coding-workflows"
_PINNED_SUPPORT_CHECKOUT_PATHS = (
	".security-hold-support",
	".non-retryable-backfill-support",
	# tele-funtoken-msg-scoring#4566: the outage resume runs its helper with GH_PAT.
	".provider-outage-support",
)
_KEY_VALUE_RE = re.compile(
	r"^(?P<indent>[ \t]*)(?:-[ \t]+)?(?P<key>[A-Za-z0-9_-]+):[ \t]*(?P<value>[^\r\n#]*?)[ \t]*(?:#[^\r\n]*)?(?P<eol>\r?\n?)$"
)


def validate_release_sha(release_sha: str) -> str:
	"""Return a normalized release SHA or raise ValueError."""
	if not isinstance(release_sha, str) or not _RELEASE_SHA_RE.fullmatch(release_sha):
		raise ValueError("release SHA must be exactly 40 hexadecimal characters")
	return release_sha.lower()


def _indent_width(line: str) -> int:
	return len(line) - len(line.lstrip(" \t"))


def _pin_support_checkout_refs(text: str, release_sha: str) -> str:
	"""Pin `ref: stable` of the listed support checkouts to release_sha.

	Fails closed when a listed checkout of this repository has no `ref: stable`.
	"""
	lines = text.splitlines(keepends=True)
	for index, line in enumerate(lines):
		uses_match = _KEY_VALUE_RE.match(line)
		if not uses_match or uses_match.group("key") != "uses":
			continue
		if not uses_match.group("value").strip("'\"").startswith("actions/checkout@"):
			continue
		# Step key indent: a `- uses:` line counts from the key after the dash.
		step_indent = len(line) - len(line.lstrip(" \t-"))
		with_index = None
		cursor = index + 1
		while cursor < len(lines):
			candidate = lines[cursor]
			if not candidate.strip():
				cursor += 1
				continue
			if _indent_width(candidate) < step_indent or candidate.lstrip().startswith("- "):
				break
			key_match = _KEY_VALUE_RE.match(candidate)
			if key_match and key_match.group("key") == "with" and _indent_width(candidate) == step_indent:
				with_index = cursor
				break
			cursor += 1
		if with_index is None:
			continue
		with_indent = _indent_width(lines[with_index])
		fields: dict[str, tuple[int, re.Match[str]]] = {}
		cursor = with_index + 1
		while cursor < len(lines):
			candidate = lines[cursor]
			if not candidate.strip():
				cursor += 1
				continue
			if _indent_width(candidate) <= with_indent:
				break
			key_match = _KEY_VALUE_RE.match(candidate)
			if key_match:
				fields.setdefault(key_match.group("key"), (cursor, key_match))
			cursor += 1
		repository = fields.get("repository")
		path = fields.get("path")
		if repository is None or path is None:
			continue
		if repository[1].group("value").strip("'\"") != _SUPPORT_REPOSITORY:
			continue
		path_value = path[1].group("value").strip("'\"")
		if path_value not in _PINNED_SUPPORT_CHECKOUT_PATHS:
			continue
		ref = fields.get("ref")
		if ref is None or ref[1].group("value").strip("'\"") != "stable":
			raise ValueError(f"support checkout {path_value} has no @stable ref to pin")
		ref_index, ref_match = ref
		lines[ref_index] = f"{ref_match.group('indent')}ref: {release_sha} # stable{ref_match.group('eol')}"
	return "".join(lines)


def pin_reusable_workflow_refs(template_text: str, release_sha: str) -> str:
	"""Replace canonical @stable reusable-workflow refs with an immutable SHA."""
	if not isinstance(template_text, str):
		raise ValueError("template text must be a string")
	validated_release_sha = validate_release_sha(release_sha)
	rendered_text, replacement_count = _REUSABLE_WORKFLOW_REF_RE.subn(
		rf"\g<prefix>\g<path>@{validated_release_sha} # stable",
		template_text,
	)
	if replacement_count == 0:
		raise ValueError("template has no coding-workflows reusable-workflow @stable reference")
	return _pin_support_checkout_refs(rendered_text, validated_release_sha)


def _build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Pin coding-workflows reusable-workflow refs in a consumer wrapper."
	)
	parser.add_argument("--input", required=True, type=Path, help="Canonical wrapper path")
	parser.add_argument("--output", required=True, type=Path, help="Rendered wrapper path")
	parser.add_argument("--sha", required=True, help="40-character release commit SHA")
	return parser


def main(argv: Sequence[str] | None = None) -> int:
	args = _build_parser().parse_args(argv)
	input_text = args.input.read_text(encoding="utf-8")
	rendered_text = pin_reusable_workflow_refs(input_text, args.sha)
	args.output.parent.mkdir(parents=True, exist_ok=True)
	args.output.write_text(rendered_text, encoding="utf-8")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

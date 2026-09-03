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


def validate_release_sha(release_sha: str) -> str:
	"""Return a normalized release SHA or raise ValueError."""
	if not isinstance(release_sha, str) or not _RELEASE_SHA_RE.fullmatch(release_sha):
		raise ValueError("release SHA must be exactly 40 hexadecimal characters")
	return release_sha.lower()


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
	return rendered_text


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

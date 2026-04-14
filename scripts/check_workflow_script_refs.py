#!/usr/bin/env python3
"""Verify every script referenced by a workflow file exists in scripts/.

Catches three categories of references:

  1. Explicit ``scripts/<name>.<ext>`` substrings (sh, py, json, txt, md).
  2. ``${SUPPORT_SCRIPTS_DIR}/<name>.<ext>`` substrings.
  3. Bare names enumerated inside a ``for f in … ; do … done`` block whose
     body actually fetches/uses ``scripts/${f}``.  This is the bootstrap
     fetch loop pattern used by review_autofix.yml — historically a source
     of hallucinated references because the names are bare strings rather
     than paths.

Exit code 0 if every reference resolves to a file under scripts/.
Exit code 1 with one line per missing reference otherwise.

Designed to be cheap (pure stdlib, no YAML parser) so it can run both
in CI and inline in the autofix workflow before pushing AI-resolved
merge commits.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import Iterable

EXPLICIT_REF = re.compile(r"scripts/([a-zA-Z0-9_.\-]+\.(?:sh|py|json|txt|md))")
SCRIPTS_VAR_REF = re.compile(
	r"\$\{SUPPORT_SCRIPTS_DIR\}/([a-zA-Z0-9_.\-]+\.(?:sh|py|json|txt|md))"
)
BARE_NAME = re.compile(r"^[a-zA-Z0-9_.\-]+\.(?:sh|py|json|txt|md)$")
FOR_LOOP = re.compile(
	r"for\s+f\s+in\s+([^;]+?);\s*do(.*?)done",
	re.DOTALL,
)


def extract_refs(text: str) -> set[str]:
	refs: set[str] = set()
	refs.update(EXPLICIT_REF.findall(text))
	refs.update(SCRIPTS_VAR_REF.findall(text))
	for items_blob, body in FOR_LOOP.findall(text):
		# Only treat the loop as a script-fetch loop when its body actually
		# uses scripts/${f} (either as a fetch path or a local path under
		# SUPPORT_SCRIPTS_DIR).  This avoids matching unrelated for-loops
		# such as the instruction-file fetch loop further down.
		if "scripts/${f}" not in body and "${SUPPORT_SCRIPTS_DIR}/${f}" not in body:
			continue
		for item in items_blob.split():
			item = item.strip()
			if BARE_NAME.match(item):
				refs.add(item)
	return refs


def check_workflows(repo_root: pathlib.Path) -> list[str]:
	workflow_dir = repo_root / ".github" / "workflows"
	scripts_dir = repo_root / "scripts"
	errors: list[str] = []
	if not workflow_dir.is_dir():
		errors.append(f"workflow directory does not exist: {workflow_dir}")
		return errors
	if not scripts_dir.is_dir():
		errors.append(f"scripts directory does not exist: {scripts_dir}")
		return errors
	for yml in sorted(workflow_dir.glob("*.yml")):
		try:
			text = yml.read_text()
		except OSError as exc:
			errors.append(f"{yml.name}: cannot read ({exc})")
			continue
		refs = extract_refs(text)
		for ref in sorted(refs):
			target = scripts_dir / ref
			if not target.is_file():
				errors.append(
					f"{yml.name}: references scripts/{ref} which does not exist"
				)
	return errors


def check_paths(paths: Iterable[pathlib.Path], scripts_dir: pathlib.Path) -> list[str]:
	"""Run the existence check against an explicit list of workflow files.

	Used by the inline post-resolver guard, which only needs to scan the
	files the merge-resolver actually touched.
	"""
	errors: list[str] = []
	for p in paths:
		try:
			text = p.read_text()
		except OSError as exc:
			errors.append(f"{p}: cannot read ({exc})")
			continue
		for ref in sorted(extract_refs(text)):
			target = scripts_dir / ref
			if not target.is_file():
				errors.append(
					f"{p.name}: references scripts/{ref} which does not exist"
				)
	return errors


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--repo-root",
		type=pathlib.Path,
		default=pathlib.Path(__file__).resolve().parent.parent,
		help="repository root (defaults to parent of scripts/)",
	)
	parser.add_argument(
		"--files",
		nargs="*",
		type=pathlib.Path,
		help="explicit list of workflow files to check (defaults to all)",
	)
	args = parser.parse_args()

	repo_root = args.repo_root.resolve()
	if args.files:
		scripts_dir = repo_root / "scripts"
		errors = check_paths([f.resolve() for f in args.files], scripts_dir)
	else:
		errors = check_workflows(repo_root)

	if errors:
		for line in errors:
			print(f"FAIL: {line}", file=sys.stderr)
		print(
			f"\n{len(errors)} workflow script reference(s) do not exist",
			file=sys.stderr,
		)
		return 1
	print("All workflow script references resolve to existing files.")
	return 0


if __name__ == "__main__":
	sys.exit(main())

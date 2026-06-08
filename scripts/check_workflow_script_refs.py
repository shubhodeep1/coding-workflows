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

# Scripts that workflows reference but which are intentionally NOT present in
# this repo's scripts/ on main.  These are staged optionally (fail-open) from
# the branch under processing when that branch carries them, so a missing file
# on main is expected and must not be reported as a hallucinated reference.
#
#   render_prompt.py — Python backend for the render_prompt.sh shim adopted by
#     branches that took the issue #3043 refactor.  main ships a self-contained
#     bash render_prompt.sh that needs no backend, so render_prompt.py does not
#     exist on main; the staging steps reference it only to copy it when a
#     shim-adopting branch actually provides it.
OPTIONAL_REFS = frozenset({"render_prompt.py"})
EXTRA_REF_HOLDER_FILES = (pathlib.Path("scripts") / "stage_workflow_support.sh",)

EXPLICIT_REF = re.compile(r"scripts/([a-zA-Z0-9_.\-]+\.(?:sh|py|json|txt|md))")
SCRIPTS_VAR_REF = re.compile(
	r"\$\{SUPPORT_SCRIPTS_DIR\}/([a-zA-Z0-9_.\-]+\.(?:sh|py|json|txt|md))"
)
BARE_NAME = re.compile(r"^[a-zA-Z0-9_.\-]+\.(?:sh|py|json|txt|md)$")
SHELL_ASSIGNMENT = re.compile(r'^\s*([A-Z0-9_]+)="([^"\n]*)"', re.MULTILINE)
VARIABLE_REF = re.compile(r"^\$(?:\{([A-Z0-9_]+)\}|([A-Z0-9_]+))$")
FOR_LOOP = re.compile(
	r"for\s+f\s+in\s+([^;]+?);\s*do(.*?)done",
	re.DOTALL,
)


def extract_assignment_words(text: str) -> dict[str, list[str]]:
	assignments: dict[str, list[str]] = {}
	for name, blob in SHELL_ASSIGNMENT.findall(text):
		words = [word.strip() for word in blob.split() if word.strip()]
		if words:
			assignments[name] = words
	return assignments


def expand_loop_items(items_blob: str, assignments: dict[str, list[str]]) -> list[str]:
	expanded: list[str] = []
	for item in items_blob.split():
		item = item.strip()
		match = VARIABLE_REF.match(item)
		if match:
			var_name = match.group(1) or match.group(2)
			expanded.extend(assignments.get(var_name, []))
			continue
		expanded.append(item)
	return expanded


def extract_refs(text: str) -> set[str]:
	refs: set[str] = set()
	assignments = extract_assignment_words(text)
	refs.update(EXPLICIT_REF.findall(text))
	refs.update(SCRIPTS_VAR_REF.findall(text))
	for items_blob, body in FOR_LOOP.findall(text):
		# Only treat the loop as a script-fetch loop when its body actually
		# uses scripts/${f} (either as a fetch path or a local path under
		# SUPPORT_SCRIPTS_DIR).  This avoids matching unrelated for-loops
		# such as the instruction-file fetch loop further down.
		if "scripts/${f}" not in body and "${SUPPORT_SCRIPTS_DIR}/${f}" not in body:
			continue
		for item in expand_loop_items(items_blob, assignments):
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
	workflow_files = sorted(
		set(workflow_dir.glob("*.yml")) | set(workflow_dir.glob("*.yaml"))
	)
	for yml in workflow_files:
		try:
			text = yml.read_text(encoding="utf-8")
		except (OSError, UnicodeDecodeError) as exc:
			errors.append(f"{yml.name}: cannot read ({exc})")
			continue
		refs = extract_refs(text)
		for ref in sorted(refs):
			if ref in OPTIONAL_REFS:
				continue
			target = scripts_dir / ref
			if not target.is_file():
				errors.append(
					f"{yml.name}: references scripts/{ref} which does not exist"
				)
	for relpath in EXTRA_REF_HOLDER_FILES:
		path = repo_root / relpath
		if not path.is_file():
			errors.append(f"{relpath}: cannot read (file missing)")
			continue
		errors.extend(check_paths([path], scripts_dir))
	return errors


def check_paths(paths: Iterable[pathlib.Path], scripts_dir: pathlib.Path) -> list[str]:
	"""Run the existence check against an explicit list of workflow files.

	Used by the inline post-resolver guard, which only needs to scan the
	files the merge-resolver actually touched.
	"""
	errors: list[str] = []
	for p in paths:
		try:
			text = p.read_text(encoding="utf-8")
		except (OSError, UnicodeDecodeError) as exc:
			errors.append(f"{p}: cannot read ({exc})")
			continue
		for ref in sorted(extract_refs(text)):
			if ref in OPTIONAL_REFS:
				continue
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
		workflow_paths = [
			f.resolve() if f.is_absolute() else (repo_root / f).resolve()
			for f in args.files
		]
		errors = check_paths(workflow_paths, scripts_dir)
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

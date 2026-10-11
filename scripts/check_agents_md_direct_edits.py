#!/usr/bin/env python3
"""Fail a pull request that adds documentation to agents.md directly (§30).

CLAUDE.md §30: new agents.md documentation goes in an `agents.d/` fragment;
the agents file itself is edited only to correct or remove existing text and
by `make generate` (the `## Repo-tree (auto-generated)` section). Direct
additions are what makes concurrent PRs, and long-running orchestrator
integration branches, conflict in agents.md (#6902's final PR #6905 failed on
exactly that). The check runs in CI on pull requests:

    check_agents_md_direct_edits.py --base <sha> --head <sha> [--repo-root .]

A change to `agents.md` / `AGENTS.md` (top level) between the two commits is a
violation when

- a diff hunk only inserts lines (no line removed) outside the generated
  text: that is new documentation, or
- its hunks outside the generated text add more than
  AGENTS_MD_DIRECT_NET_ADD_MAX lines (default 5) beyond the lines they
  replace: a correction that also appends. Each hunk's surplus counts on its
  own, so a removal elsewhere cannot offset an addition.

The generated text is only what `make generate` rewrites: the lines between
a `<!-- TREE:START ... -->` and the next `<!-- TREE:END ... -->` marker under
the Repo-tree section. Prose elsewhere in that section, the markers
themselves and any hunk that adds a `## ` heading are checked like the rest of
the file.

Output: one `AGENTS_MD_DIRECT_EDIT outcome=ok|violation|error ...` line,
plus `::error::` lines naming each offending hunk. Exit 0 when ok, 1 on a
violation, 2 on a usage or git error. It runs no GitHub API calls.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

AGENTS_FILES = ("agents.md", "AGENTS.md")
GENERATED_SECTION = "## Repo-tree (auto-generated)"
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DEFAULT_NET_ADD_MAX = 5


def _git(repo_root: Path, *args: str) -> str:
	result = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True, check=False)
	if result.returncode != 0:
		raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
	return result.stdout


def _section_of_line(lines: list[str], line_number: int) -> str:
	"""The `## ` heading a 1-based line of the new file falls under."""
	heading = ""
	for text in lines[: max(line_number, 0)]:
		if text.startswith("## "):
			heading = text.rstrip()
	return heading


def _generated_spans(lines: list[str]) -> list[tuple[int, int]]:
	"""1-based (START, END) marker line pairs; only the lines between are generated."""
	spans: list[tuple[int, int]] = []
	open_line = 0
	for number, text in enumerate(lines, 1):
		stripped = text.strip()
		if stripped.startswith("<!-- TREE:START"):
			open_line = number
		elif stripped.startswith("<!-- TREE:END") and open_line:
			spans.append((open_line, number))
			open_line = 0
	return spans


def check(repo_root: Path, base: str, head: str, net_add_max: int) -> tuple[list[str], dict[str, int]]:
	violations: list[str] = []
	totals: dict[str, int] = {}
	changed = _git(repo_root, "diff", "--name-only", base, head, "--", *AGENTS_FILES).split()
	for path in changed:
		try:
			new_lines = _git(repo_root, "show", f"{head}:{path}").split("\n")
		except RuntimeError:
			continue  # deleted: removing the file is not an addition
		diff = _git(repo_root, "diff", "-U0", "--no-color", base, head, "--", path)
		net_added = 0
		surplus = 0
		spans = _generated_spans(new_lines)
		# Split on real newlines only: str.splitlines() also splits on Unicode
		# separators inside content, which could fake a hunk header.
		for header in diff.split("\n"):
			match = HUNK_RE.match(header)
			if not match:
				continue
			removed = int(match.group(2) if match.group(2) is not None else "1")
			added = int(match.group(4) if match.group(4) is not None else "1")
			start = int(match.group(3))
			# An added hunk belongs to the section of the line before it, and a
			# hunk that adds a `## ` heading is never exempt: otherwise a hunk
			# that re-creates the Repo-tree heading would exempt the prose after it.
			section = _section_of_line(new_lines, start - 1 if added else start + 1)
			adds_heading = any(text.startswith("## ") for text in new_lines[max(start - 1, 0):start - 1 + added])
			# Exempt only what `make generate` rewrites: added lines strictly
			# between a TREE:START and its TREE:END marker (for a pure deletion,
			# the position after the start marker line).
			if added:
				in_span = any(s < start and start + added - 1 < e for s, e in spans)
			else:
				in_span = any(s <= start < e for s, e in spans)
			if section == GENERATED_SECTION and not adds_heading and in_span:
				continue
			net_added += added - removed
			surplus += max(added - removed, 0)
			if added and not removed:
				violations.append(f"{path}:{start} adds {added} line(s) under '{section or 'top of file'}' without changing existing text")
		totals[path] = net_added
		if surplus > net_add_max:
			violations.append(f"{path} gains {surplus} line(s) outside {GENERATED_SECTION!r} (limit {net_add_max})")
	return violations, totals


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--base", required=True)
	parser.add_argument("--head", required=True)
	parser.add_argument("--repo-root", default=".")
	args = parser.parse_args(argv)
	raw_max = os.environ.get("AGENTS_MD_DIRECT_NET_ADD_MAX", str(DEFAULT_NET_ADD_MAX)).strip()
	max_valid = raw_max.isascii() and raw_max.isdigit()
	net_add_max = int(raw_max) if max_valid else DEFAULT_NET_ADD_MAX
	if not max_valid:
		print(f"::warning::AGENTS_MD_DIRECT_NET_ADD_MAX={raw_max[:40]!r} is not a non-negative integer; using {DEFAULT_NET_ADD_MAX}")
	if not re.fullmatch(r"[0-9a-f]{40}|HEAD", args.base) or not re.fullmatch(r"[0-9a-f]{40}|HEAD", args.head):
		print("::error::AGENTS_MD_DIRECT_EDIT outcome=error reason=invalid_ref")
		return 2
	try:
		violations, totals = check(Path(args.repo_root), args.base, args.head, net_add_max)
	except RuntimeError as exc:
		print(f"::error::AGENTS_MD_DIRECT_EDIT outcome=error reason=git_failed detail={exc}")
		return 2
	summary = " ".join(f"{path}={count:+d}" for path, count in sorted(totals.items())) or "none"
	if not violations:
		print(f"AGENTS_MD_DIRECT_EDIT outcome=ok net_added={summary}")
		return 0
	for violation in violations:
		print(f"::error::AGENTS_MD_DIRECT_EDIT {violation}")
	print(
		f"AGENTS_MD_DIRECT_EDIT outcome=violation count={len(violations)} net_added={summary}. "
		"Move new documentation into an agents.d/<issue-or-pr>-<slug>.md fragment (CLAUDE.md §30); "
		"edit agents.md directly only to correct or remove existing text."
	)
	return 1


if __name__ == "__main__":
	sys.exit(main())

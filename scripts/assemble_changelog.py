#!/usr/bin/env python3
"""Fold per-PR changelog fragments into CHANGELOG.md.

Why this exists
---------------
Newest-entry-first insertion into a single ``CHANGELOG.md`` means every
concurrently-open PR edits the same hunk at line 1, so git must conflict.
Each such conflict costs a full LLM run through
``scripts/review_conflict_resolve.sh``.  The fix is structural: agents write
one fragment file per PR under ``changelog.d/`` — two PRs never touch the
same path, so there is nothing to conflict — and this script folds those
fragments into ``CHANGELOG.md`` from automation that already runs.

Wiring (CLAUDE.md §18.A/§18.B — no operator action anywhere):
  * upstream, at release time —
    ``.github/workflows/mark-stable.yml`` and
    ``.github/workflows/test-and-mark-stable.yml`` (``release`` job).
  * consumer repos, on the existing sync —
    ``.github/workflows/update_workflows.yml`` (``update-wrappers`` job),
    which already commits and pushes to the consumer's default branch.

Both layouts are supported (CLAUDE.md §20)
------------------------------------------
Upstream uses Keep a Changelog (``## [Unreleased]`` with ``### Added`` /
``### Changed`` / ``### Fixed`` subsections).  Consumer repos such as
``shubhodeep1/tele-funtoken-msg-scoring`` use bare ``## YYYY-MM-DD`` date
headings, newest first, with no ``[Unreleased]`` and no subsections.  The
layout is auto-detected from the existing ``CHANGELOG.md`` — no repo-local
configuration, and no consumer has to convert its history.

Fragment format
---------------
``changelog.d/<issue-or-pr>-<slug>.md``.  An optional first line

    <!-- changelog: fixed -->

names the Keep a Changelog subsection (``added``, ``changed``,
``deprecated``, ``removed``, ``fixed``, ``security``; default ``added``).
The marker is ignored in date-heading repos, which have no subsections.
Everything after the marker is the entry body and is copied verbatim.

Guarantees
----------
* **Purely additive.**  Every edit is a line insertion; no existing line of
  ``CHANGELOG.md`` is ever removed or reflowed.
* **Idempotent.**  With no fragments present the file is left byte-for-byte
  unchanged and the exit code is 0.
* **Deterministic.**  Fragments are folded in filename sort order.

Subcommands
-----------
``assemble``       fold fragments into CHANGELOG.md and delete them.
``ensure-assets``  idempotently create ``changelog.d/.gitkeep`` and add the
                   managed ``CHANGELOG.md merge=union`` block to
                   ``.gitattributes``.  Used by the consumer sync step so the
                   mechanism reaches every repo in
                   ``.github/ai/consumer_repos.json``.

Exit codes
----------
0 — success (including the no-fragments no-op).
1 — unreadable/unwritable path, or a malformed argument.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

FRAGMENTS_DIRNAME = "changelog.d"
CHANGELOG_FILENAME = "CHANGELOG.md"
GITATTRIBUTES_FILENAME = ".gitattributes"
GITKEEP_FILENAME = ".gitkeep"

# Files inside changelog.d/ that are scaffolding, not entries.
FRAGMENT_EXCLUDED_NAMES = frozenset({GITKEEP_FILENAME, "README.md"})

MANAGED_BLOCK_BEGIN = "# BEGIN coding-workflows changelog assets — managed by update_workflows.yml"
MANAGED_BLOCK_END = "# END coding-workflows changelog assets"
# Belt-and-braces backstop for any direct CHANGELOG.md edit that bypasses the
# fragment workflow. `union` is a git built-in merge driver, so no consumer
# needs a merge-driver definition in .git/config. It applies to real `git
# merge` invocations — including the merge replay in
# scripts/review_conflict_prepare.sh — not to GitHub's server-side mergeability
# estimate. It is a *textual* union with no idea what a changelog entry is: it
# keeps both sides' lines and orders them by merge order rather than intent,
# which is why fragments, not this, are the mechanism.
MANAGED_BLOCK_BODY = ("CHANGELOG.md merge=union",)

LAYOUT_KEEP_A_CHANGELOG = "keep-a-changelog"
LAYOUT_DATE_HEADINGS = "date-headings"

# Keep a Changelog canonical subsection order.
CATEGORY_ORDER = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
CATEGORY_BY_LOWER = {name.lower(): name for name in CATEGORY_ORDER}
DEFAULT_CATEGORY = "Added"

CATEGORY_MARKER = re.compile(r"^<!--\s*changelog:\s*([A-Za-z]+)\s*-->\s*$")
UNRELEASED_HEADING = re.compile(r"^##\s+\[unreleased\]", re.IGNORECASE)
BRACKET_H2 = re.compile(r"^##\s+\[")
DATE_HEADING = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")
SUBSECTION_HEADING = re.compile(r"^###\s+([A-Za-z][A-Za-z ]*?)\s*$")

# Section boundaries are recognised by SHAPE, not by "any `## ` line".
# Fragment bodies are copied verbatim and legitimately contain their own
# headings — §20's own entry template ends with a `### For contributors`
# subsection. Once such a fragment is folded, a boundary scanner that
# accepted any `## `/`### ` line would treat that prose heading as a section
# marker and splice the next release's entries into the middle of the
# previous one. Only the two shapes this assembler itself emits count:
# `## [version]` (Keep a Changelog) and `## YYYY-MM-DD` (date headings).
SECTION_H2 = re.compile(r"^##\s+(?:\[|\d{4}-\d{2}-\d{2}\s*$)")

DEFAULT_NEW_CHANGELOG = "# Changelog\n\nAll notable changes to this project are documented in this file.\n"


# ──────────────────────────────────────────────────────────────────
# Fragment discovery and parsing
# ──────────────────────────────────────────────────────────────────


def read_text_preserving_newlines(path: Path) -> tuple[str, str]:
	"""Return ``(text_with_lf_endings, dominant_line_terminator)``.

	CRLF must survive a fold. Rewriting a CRLF changelog as LF would rewrite
	every line — breaking the purely-additive guarantee outright, and, with
	the shipped ``CHANGELOG.md merge=union`` attribute, letting the next merge
	union two whole files that differ on every line.
	"""
	# Plain open(), not Path.read_text(newline=...): the newline kwarg was only
	# added to Path.read_text in Python 3.13 and CI pins 3.12.
	with open(path, "r", encoding="utf-8", newline="") as handle:
		raw = handle.read()
	crlf = raw.count("\r\n")
	lf_only = raw.count("\n") - crlf
	terminator = "\r\n" if crlf > lf_only else "\n"
	return raw.replace("\r\n", "\n"), terminator


def write_text_with_newlines(path: Path, text: str, terminator: str) -> None:
	"""Write LF-normalised ``text`` back using the file's own terminator."""
	payload = text.replace("\n", terminator) if terminator != "\n" else text
	with open(path, "w", encoding="utf-8", newline="") as handle:
		handle.write(payload)


def iter_fragment_paths(fragments_dir: Path) -> list[Path]:
	"""Return fragment files in deterministic (filename sort) order.

	Skips dotfiles, scaffolding (``.gitkeep``, ``README.md``), sub-directories,
	and anything that is not ``*.md`` so a stray note in the directory cannot
	be folded into the changelog by accident.
	"""
	if not fragments_dir.is_dir():
		return []
	found: list[Path] = []
	for entry in fragments_dir.iterdir():
		if not entry.is_file():
			continue
		if entry.name.startswith("."):
			continue
		if entry.name in FRAGMENT_EXCLUDED_NAMES:
			continue
		if entry.suffix.lower() != ".md":
			continue
		found.append(entry)
	return sorted(found, key=lambda path: path.name)


def parse_fragment(path: Path) -> tuple[str, list[str]]:
	"""Return ``(category, body_lines)`` for one fragment file.

	An unrecognised or absent ``<!-- changelog: … -->`` marker resolves to
	DEFAULT_CATEGORY rather than failing — a fragment with a typo'd category
	still ships its entry instead of silently dropping it or breaking a
	release.
	"""
	raw = path.read_text(encoding="utf-8")
	lines = raw.split("\n")
	category = DEFAULT_CATEGORY

	index = 0
	while index < len(lines) and lines[index].strip() == "":
		index += 1
	if index < len(lines):
		marker = CATEGORY_MARKER.match(lines[index].strip())
		if marker:
			category = CATEGORY_BY_LOWER.get(marker.group(1).lower(), DEFAULT_CATEGORY)
			index += 1

	body = lines[index:]
	while body and body[0].strip() == "":
		body.pop(0)
	while body and body[-1].strip() == "":
		body.pop()
	return category, body


# ──────────────────────────────────────────────────────────────────
# Layout detection
# ──────────────────────────────────────────────────────────────────


def detect_layout(text: str) -> str:
	"""Pick the layout from the file's own headings.

	``## [Unreleased]`` or any ``## [version]`` heading means Keep a
	Changelog.  A bare ``## YYYY-MM-DD`` heading means date headings.  An
	empty or heading-less file defaults to date headings, which is the
	layout a consumer repo starting from nothing will grow.
	"""
	for line in text.split("\n"):
		if UNRELEASED_HEADING.match(line):
			return LAYOUT_KEEP_A_CHANGELOG
		if DATE_HEADING.match(line):
			return LAYOUT_DATE_HEADINGS
		if BRACKET_H2.match(line):
			return LAYOUT_KEEP_A_CHANGELOG
	return LAYOUT_DATE_HEADINGS


def _first_h2_index(lines: list[str]) -> int | None:
	for index, line in enumerate(lines):
		if SECTION_H2.match(line):
			return index
	return None


def _content_end(lines: list[str], start: int, stop: int) -> int:
	"""Index just past the last non-blank line of ``lines[start:stop]``."""
	end = stop
	while end > start and lines[end - 1].strip() == "":
		end -= 1
	return end


def _heading_body_start(lines: list[str], heading: int, stop: int) -> int:
	"""First index where a section's body may be inserted, under its heading.

	Skips exactly one blank line after the heading so the inserted entry keeps
	the file's existing heading/blank/body shape, and never runs past ``stop``
	(an empty section).
	"""
	index = heading + 1
	if index < stop and lines[index].strip() == "":
		index += 1
	return min(index, stop)


def _category_rank(name: str) -> int:
	try:
		return CATEGORY_ORDER.index(name)
	except ValueError:
		return len(CATEGORY_ORDER)


# ──────────────────────────────────────────────────────────────────
# Insertion planning — every plan entry is a pure line insertion, so an
# assembled CHANGELOG.md can only ever gain lines (never lose or reflow
# them). Plans are applied in descending index order so earlier indices
# stay valid.
# ──────────────────────────────────────────────────────────────────


def _apply_insertions(lines: list[str], insertions: list[tuple[int, list[str]]]) -> list[str]:
	"""Apply pure line insertions, later document positions first.

	Two insertions can share an index — appending to the last ``###``
	subsection of the Unreleased block and creating a new subsection after it
	both target the block's content end. At a shared index the payload applied
	last ends up first, so ties are applied in reverse planning order: the
	caller plans in the order it wants the payloads to appear.
	"""
	result = list(lines)
	ordered = sorted(
		((index, sequence, payload) for sequence, (index, payload) in enumerate(insertions)),
		key=lambda item: (item[0], item[1]),
		reverse=True,
	)
	for index, _sequence, payload in ordered:
		result[index:index] = payload
	return result


def _ensure_unreleased_section(lines: list[str]) -> tuple[list[str], int]:
	"""Return ``(lines, index_of_unreleased_heading)``, creating it if absent."""
	for index, line in enumerate(lines):
		if UNRELEASED_HEADING.match(line):
			return lines, index

	insert_at = _first_h2_index(lines)
	if insert_at is None:
		result = list(lines)
		tail = _content_end(result, 0, len(result))
		payload = ["## [Unreleased]", ""]
		if tail > 0:
			payload = [""] + payload
		result[tail:tail] = payload
		return result, tail + (1 if tail > 0 else 0)

	result = list(lines)
	result[insert_at:insert_at] = ["## [Unreleased]", ""]
	return result, insert_at


def _plan_keep_a_changelog(lines: list[str], grouped: dict[str, list[list[str]]]) -> list[str]:
	lines, unreleased = _ensure_unreleased_section(lines)

	block_end = len(lines)
	for index in range(unreleased + 1, len(lines)):
		if SECTION_H2.match(lines[index]):
			block_end = index
			break

	# Existing subsections of the Unreleased block, in file order.
	#
	# Only RECOGNISED category names count. Entry bodies are copied verbatim
	# and carry their own headings — §20's template ends every entry with a
	# `### For contributors` subsection — so accepting any `### Word` here
	# would make a folded entry's prose heading act as a category boundary.
	# The next release would then append into the middle of the previous
	# entry, and create new categories out of canonical order, corrupting the
	# document a little more on every release.
	subsections: list[tuple[str, int, int]] = []
	starts: list[tuple[str, int]] = []
	for index in range(unreleased + 1, block_end):
		match = SUBSECTION_HEADING.match(lines[index])
		if match and match.group(1).strip().lower() in CATEGORY_BY_LOWER:
			starts.append((CATEGORY_BY_LOWER[match.group(1).strip().lower()], index))
	for position, (name, start) in enumerate(starts):
		stop = starts[position + 1][1] if position + 1 < len(starts) else block_end
		subsections.append((name, start, stop))

	# First occurrence wins: a hand-maintained Unreleased block can carry the
	# same subsection heading twice, and appending to the first keeps new
	# entries in the canonical group rather than a stray duplicate.
	by_lower: dict[str, tuple[str, int, int]] = {}
	for name, start, stop in subsections:
		by_lower.setdefault(name.lower(), (name, start, stop))

	insertions: list[tuple[int, list[str]]] = []
	for category in CATEGORY_ORDER:
		bodies = grouped.get(category)
		if not bodies:
			continue
		payload_body: list[str] = []
		for position, body in enumerate(bodies):
			if position:
				payload_body.append("")
			payload_body.extend(body)

		existing = by_lower.get(category.lower())
		if existing is not None:
			_, start, stop = existing
			# Insert at the TOP of the category, directly under its heading —
			# not at the end. Entries carry their own trailing subsections
			# (§20's template ends with `### For contributors`), so appending
			# to the category's end would drop the new entry underneath the
			# previous entry's contributor notes and read as part of it.
			# Top insertion also matches the newest-first convention both
			# supported layouts already use.
			insertions.append((_heading_body_start(lines, start, stop), payload_body + [""]))
			continue

		successor = None
		for name, start, _stop in subsections:
			if _category_rank(name) > _category_rank(category):
				successor = start
				break
		if successor is not None:
			insertions.append((successor, [f"### {category}", ""] + payload_body + [""]))
		else:
			tail = _content_end(lines, unreleased + 1, block_end)
			if tail <= unreleased:
				tail = unreleased + 1
				insertions.append((tail, [f"### {category}", ""] + payload_body + [""]))
			else:
				insertions.append((tail, ["", f"### {category}", ""] + payload_body))

	return _apply_insertions(lines, insertions)


def _plan_date_headings(lines: list[str], bodies: list[list[str]], day: str) -> list[str]:
	payload_body: list[str] = []
	for position, body in enumerate(bodies):
		if position:
			payload_body.append("")
		payload_body.extend(body)

	first_h2 = _first_h2_index(lines)

	if first_h2 is not None:
		match = DATE_HEADING.match(lines[first_h2])
		if match and match.group(1) == day:
			stop = len(lines)
			for index in range(first_h2 + 1, len(lines)):
				if SECTION_H2.match(lines[index]):
					stop = index
					break
			# Top of the day's section, for the same reason as the Keep a
			# Changelog branch: entries carry trailing subsections, and
			# newest-first is the convention these changelogs already use.
			return _apply_insertions(
				lines,
				[(_heading_body_start(lines, first_h2, stop), payload_body + [""])],
			)
		return _apply_insertions(
			lines, [(first_h2, [f"## {day}", ""] + payload_body + [""])]
		)

	tail = _content_end(lines, 0, len(lines))
	payload = [f"## {day}", ""] + payload_body
	if tail > 0:
		payload = [""] + payload
	return _apply_insertions(lines, [(tail, payload)])


# ──────────────────────────────────────────────────────────────────
# Subcommand: assemble
# ──────────────────────────────────────────────────────────────────


def assemble(
	repo_root: Path,
	fragments_dir: Path,
	changelog_path: Path,
	day: str,
	dry_run: bool,
) -> dict[str, object]:
	fragment_paths = iter_fragment_paths(fragments_dir)

	if changelog_path.is_file():
		original, terminator = read_text_preserving_newlines(changelog_path)
		created = False
	else:
		original = DEFAULT_NEW_CHANGELOG
		terminator = "\n"
		created = True

	layout = detect_layout(original)

	try:
		relative = str(changelog_path.relative_to(repo_root))
	except ValueError:
		relative = str(changelog_path)

	if not fragment_paths:
		# No-op: never rewrite (or create) the file just to prove we ran.
		return {
			"layout": layout,
			"fragments": [],
			"changed": False,
			"created": False,
			"changelog": relative,
		}

	parsed = [parse_fragment(path) for path in fragment_paths]
	# Drop fragments whose body is empty — an empty file must not inject a
	# stray blank bullet into a released changelog.
	usable = [
		(path, category, body)
		for path, (category, body) in zip(fragment_paths, parsed)
		if body
	]

	empty = [path.name for path, (_c, body) in zip(fragment_paths, parsed) if not body]

	if not usable:
		# Every fragment was empty. They are still removed — but that removal
		# is a real working-tree change the caller must stage, otherwise the
		# consumer sync deletes the same files again on every run forever.
		for path in fragment_paths:
			if not dry_run:
				path.unlink()
		return {
			"layout": layout,
			"fragments": [path.name for path in fragment_paths],
			"changed": bool(fragment_paths),
			"created": False,
			"changelog": relative,
			"empty_fragments": empty,
		}

	lines = original.split("\n")

	if layout == LAYOUT_KEEP_A_CHANGELOG:
		grouped: dict[str, list[list[str]]] = {}
		for _path, category, body in usable:
			grouped.setdefault(category, []).append(body)
		updated = _plan_keep_a_changelog(lines, grouped)
	else:
		updated = _plan_date_headings(lines, [body for _p, _c, body in usable], day)

	text = "\n".join(updated)
	if not text.endswith("\n"):
		text += "\n"

	if not dry_run:
		changelog_path.parent.mkdir(parents=True, exist_ok=True)
		write_text_with_newlines(changelog_path, text, terminator)
		for path in fragment_paths:
			path.unlink()

	return {
		"layout": layout,
		"fragments": [path.name for path in fragment_paths],
		"changed": text != original or created,
		"created": created,
		"changelog": relative,
		"empty_fragments": empty,
		"text": text,
	}


# ──────────────────────────────────────────────────────────────────
# Subcommand: ensure-assets
# ──────────────────────────────────────────────────────────────────


def _render_managed_block() -> list[str]:
	return [MANAGED_BLOCK_BEGIN, *MANAGED_BLOCK_BODY, MANAGED_BLOCK_END]


def ensure_gitattributes(gitattributes_path: Path, dry_run: bool) -> bool:
	"""Add or refresh the managed union block. Returns True when changed.

	Additive by construction: a consumer's own ``.gitattributes`` rules are
	never touched — only the delimited managed block is written.
	"""
	block = _render_managed_block()

	if gitattributes_path.is_file():
		original, terminator = read_text_preserving_newlines(gitattributes_path)
	else:
		original, terminator = "", "\n"

	lines = original.split("\n") if original else []

	begin_index = None
	end_index = None
	for index, line in enumerate(lines):
		if line.strip() == MANAGED_BLOCK_BEGIN:
			begin_index = index
		elif line.strip() == MANAGED_BLOCK_END and begin_index is not None:
			end_index = index
			break

	if begin_index is not None and end_index is not None:
		if lines[begin_index : end_index + 1] == block:
			return False
		updated = lines[:begin_index] + block + lines[end_index + 1 :]
	else:
		updated = list(lines)
		tail = _content_end(updated, 0, len(updated))
		payload = block if tail == 0 else [""] + block
		updated[tail:tail] = payload

	text = "\n".join(updated)
	if not text.endswith("\n"):
		text += "\n"
	if text == original:
		return False
	if not dry_run:
		write_text_with_newlines(gitattributes_path, text, terminator)
	return True


def ensure_assets(repo_root: Path, fragments_dir: Path, dry_run: bool) -> list[str]:
	"""Create changelog.d/.gitkeep and the .gitattributes managed block."""
	changed: list[str] = []

	gitkeep = fragments_dir / GITKEEP_FILENAME
	if not gitkeep.is_file():
		if not dry_run:
			fragments_dir.mkdir(parents=True, exist_ok=True)
			gitkeep.write_text("", encoding="utf-8")
		try:
			changed.append(str(gitkeep.relative_to(repo_root)))
		except ValueError:
			changed.append(str(gitkeep))

	gitattributes = repo_root / GITATTRIBUTES_FILENAME
	if ensure_gitattributes(gitattributes, dry_run):
		changed.append(GITATTRIBUTES_FILENAME)

	return changed


# ──────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────


def write_github_output(pairs: list[tuple[str, str]]) -> None:
	target = os.environ.get("GITHUB_OUTPUT", "")
	if not target:
		return
	try:
		with open(target, "a", encoding="utf-8") as handle:
			for key, value in pairs:
				handle.write(f"{key}={value}\n")
	except OSError as exc:  # pragma: no cover — CI filesystem failure
		print(f"::warning::Could not write GITHUB_OUTPUT: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument(
		"--repo-root",
		default=".",
		help="Repository root that holds CHANGELOG.md and changelog.d/ (default: cwd).",
	)
	parser.add_argument(
		"--fragments-dir",
		default=None,
		help=f"Fragment directory (default: <repo-root>/{FRAGMENTS_DIRNAME}).",
	)
	parser.add_argument(
		"--changelog",
		default=None,
		help=f"Changelog file (default: <repo-root>/{CHANGELOG_FILENAME}).",
	)
	parser.add_argument(
		"--date",
		default=None,
		help="YYYY-MM-DD heading used in date-heading repos (default: today, UTC).",
	)
	parser.add_argument(
		"--changed-paths-file",
		default=None,
		help="Write one changed repo-relative path per line to this file.",
	)
	parser.add_argument(
		"--dry-run",
		action="store_true",
		help="Report what would change without writing or deleting anything.",
	)
	parser.add_argument(
		"command",
		choices=("assemble", "ensure-assets"),
		help="assemble: fold fragments into CHANGELOG.md. ensure-assets: create changelog.d/.gitkeep and the .gitattributes managed block.",
	)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)

	repo_root = Path(args.repo_root).resolve()
	if not repo_root.is_dir():
		print(f"::error::--repo-root '{repo_root}' is not a directory.", file=sys.stderr)
		return 1

	fragments_dir = (
		Path(args.fragments_dir).resolve()
		if args.fragments_dir
		else repo_root / FRAGMENTS_DIRNAME
	)
	changelog_path = (
		Path(args.changelog).resolve() if args.changelog else repo_root / CHANGELOG_FILENAME
	)

	day = args.date or datetime.now(timezone.utc).date().isoformat()
	if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
		print(f"::error::--date '{day}' is not YYYY-MM-DD.", file=sys.stderr)
		return 1

	changed_paths: list[str] = []

	if args.command == "ensure-assets":
		changed_paths = ensure_assets(repo_root, fragments_dir, args.dry_run)
		print(f"CHANGELOG_ASSETS_V1: changed={len(changed_paths)} dry_run={str(args.dry_run).lower()}")
		for path in changed_paths:
			print(f"CHANGELOG_ASSETS_V1: path={path}")
		write_github_output(
			[
				("changelog_assets_changed", str(len(changed_paths))),
				(
					"changelog_assets_has_changes",
					"true" if changed_paths else "false",
				),
			]
		)
	else:
		try:
			result = assemble(repo_root, fragments_dir, changelog_path, day, args.dry_run)
		except OSError as exc:
			print(f"::error::Changelog assembly failed: {exc}", file=sys.stderr)
			return 1

		fragments = result["fragments"]
		assert isinstance(fragments, list)
		changed = bool(result["changed"])
		print(
			"CHANGELOG_ASSEMBLE_V1: "
			f"layout={result['layout']} fragments={len(fragments)} "
			f"changed={str(changed).lower()} dry_run={str(args.dry_run).lower()} "
			f"changelog={result['changelog']}"
		)
		for name in fragments:
			print(f"CHANGELOG_ASSEMBLE_V1: fragment={name}")
		# An empty fragment is removed but contributes nothing — surface it so
		# a silently-dropped entry is visible in the run log.
		for name in result.get("empty_fragments") or []:
			print(f"::warning::Changelog fragment '{name}' had no body; removed without adding an entry.")
		if changed:
			changed_paths = [str(result["changelog"]), FRAGMENTS_DIRNAME]
		write_github_output(
			[
				("changelog_assembled", "true" if changed else "false"),
				("changelog_fragment_count", str(len(fragments))),
				("changelog_layout", str(result["layout"])),
			]
		)

	if args.changed_paths_file:
		try:
			Path(args.changed_paths_file).write_text(
				"".join(f"{path}\n" for path in changed_paths), encoding="utf-8"
			)
		except OSError as exc:
			print(f"::error::Could not write --changed-paths-file: {exc}", file=sys.stderr)
			return 1

	return 0


if __name__ == "__main__":
	sys.exit(main())

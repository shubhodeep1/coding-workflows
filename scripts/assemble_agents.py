#!/usr/bin/env python3
"""Fold per-PR agents.md fragments into agents.md (or AGENTS.md).

Why this exists
---------------
About a third of all commits edit the single, 270 KB ``agents.md``, mostly
the same few places (the "Workflow architecture" narrative and the two log
prefix registries). Two open PRs that both add a paragraph or a log prefix
conflict, and every such conflict holds the merge train or costs a resolver
run. Agents therefore write one fragment per PR under ``agents.d/`` -- two PRs
never touch the same path -- and this script folds the fragments into the
agents file from automation that already runs, exactly like
``scripts/assemble_changelog.py`` does for ``changelog.d/``.

Wiring (CLAUDE.md §30, §18.A/§18.B -- no operator action anywhere):
  * upstream, at release time -- the "Assemble changelog fragments" step of
    ``.github/workflows/mark-stable.yml`` and
    ``.github/workflows/test-and-mark-stable.yml`` (``release`` job);
  * consumer repos, on the existing sync --
    ``.github/workflows/update_workflows.yml``;
  * CI -- ``check`` fails a PR whose fragment would not fold;
  * readers -- ``render`` prints the agents file with every pending fragment
    folded in, so tests and the pipeline's static context see documentation
    that is still waiting in ``agents.d/``.

Fragment format
---------------
``agents.d/<issue-or-pr>-<slug>.md`` holds one or more blocks. Each block
starts with a marker line naming an existing ``## `` heading of the agents
file, exactly as written there:

    <!-- agents: section="Workflow architecture" -->
    New paragraph or bullets, copied verbatim to the end of that section.

Add `` new`` to create a section that does not exist yet
(``<!-- agents: section="My topic" new -->``); without it a heading that is
not found is an error, so a typo cannot silently create a stray section.
New sections go before ``## Repo-tree (auto-generated)`` (or at the end of the
file). That generated section is never a target.

In ``## Stable log prefixes (contractual)`` a block's ``- `PREFIX` ...``
bullets (with their indented continuation lines) go after the section's last
such bullet and its ``LOG_PREFIX.name=PREFIX`` lines after the last
``LOG_PREFIX.name=`` line, so both registries stay contiguous. Any other text
goes to the end of the section.

Text appended to a section is separated from what precedes it by one blank
line, except a table row after a table row or a list item after a list item,
which continue the table or list.

Guarantees
----------
* **Purely additive.** Every edit is a line insertion; no existing line of the
  agents file is removed or reflowed.
* **Idempotent.** With no fragments the file is byte-for-byte unchanged.
* **Deterministic.** Fragments fold in filename order, blocks in file order.
* **Fail-soft assembly.** ``assemble`` folds every valid fragment and leaves an
  invalid one in place with a warning; a release never fails on bookkeeping.
  ``check`` is the strict mode for CI.

Subcommands
-----------
``assemble``  fold fragments into the agents file and delete them.
``render``    print the agents file with all valid fragments folded; writes
              nothing (the shared reader for tests and static context).
``check``     validate every fragment; exit 1 when any would be skipped.

Log lines: ``AGENTS_ASSEMBLE_V1: <key>=<value> ...``. GitHub outputs (when
``GITHUB_OUTPUT`` is set, ``assemble`` only): ``agents_assembled``,
``agents_fragment_count``, ``agents_fragment_skipped``, ``agents_file``.

Exit codes
----------
0 -- success (including the no-fragments no-op).
1 -- ``check`` found an invalid fragment, the agents file is a symlink or
     unreadable, or a malformed argument.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

FRAGMENTS_DIRNAME = "agents.d"
AGENTS_FILENAMES = ("agents.md", "AGENTS.md")
NEW_AGENTS_FILENAME = "AGENTS.md"
FRAGMENT_EXCLUDED_NAMES = frozenset({".gitkeep", "README.md"})
REGISTRY_SECTION = "Stable log prefixes (contractual)"
GENERATED_SECTIONS = frozenset({"Repo-tree (auto-generated)"})
NEW_SECTION_ANCHOR = "Repo-tree (auto-generated)"
LOG_PREFIX = "AGENTS_ASSEMBLE_V1:"

SECTION_MARKER = re.compile(r'^<!--\s*agents:\s*section="([^"<>\n]{1,200})"(\s+new)?\s*-->\s*$')
ANY_AGENTS_MARKER = re.compile(r"^<!--\s*agents:")
H2 = re.compile(r"^##\s+(.*?)\s*$")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
REGISTRY_BULLET = re.compile(r"^- `")
REGISTRY_NAME = re.compile(r"^LOG_PREFIX\.name=")


class FragmentError(Exception):
	"""A fragment that cannot be folded; ``assemble`` leaves it in place."""


@dataclass
class Block:
	heading: str
	new: bool
	lines: list[str] = field(default_factory=list)


def _log(**fields: object) -> None:
	print(LOG_PREFIX + " " + " ".join(f"{key}={value}" for key, value in fields.items()))


def iter_fragment_paths(fragments_dir: Path) -> list[Path]:
	"""Fragment files in filename order; dotfiles, scaffolding and non-``.md`` are skipped."""
	# A symlinked agents.d/ would let ``assemble`` fold and then unlink files
	# that live outside the fragment directory, so it yields no fragments.
	if fragments_dir.is_symlink() or not fragments_dir.is_dir():
		return []
	found = [
		entry
		for entry in fragments_dir.iterdir()
		if entry.is_file()
		and not entry.is_symlink()
		and not entry.name.startswith(".")
		and entry.name not in FRAGMENT_EXCLUDED_NAMES
		and entry.suffix == ".md"
	]
	return sorted(found, key=lambda path: path.name)


def parse_fragment(text: str) -> list[Block]:
	"""Split a fragment into marker-headed blocks; raise FragmentError when malformed."""
	blocks: list[Block] = []
	fence = ""
	for raw in text.replace("\r\n", "\n").split("\n"):
		line = raw.rstrip()
		if fence:
			# Inside fenced code: headings and markers are example text, not syntax.
			blocks[-1].lines.append(line)
			closing = FENCE.match(line)
			if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= len(fence) and not line[closing.end():].strip():
				fence = ""
			continue
		match = SECTION_MARKER.match(line)
		if match:
			heading = " ".join(match.group(1).split())
			if not heading:
				raise FragmentError("empty_section_name")
			if heading in GENERATED_SECTIONS:
				raise FragmentError("generated_section")
			blocks.append(Block(heading=heading, new=bool(match.group(2))))
			continue
		if ANY_AGENTS_MARKER.match(line):
			raise FragmentError("malformed_marker")
		if not blocks:
			if line.strip():
				raise FragmentError("content_before_marker")
			continue
		if H2.match(line):
			# A second-level heading inside a block would split the target section.
			raise FragmentError("heading_in_block")
		opening = FENCE.match(line)
		if opening:
			fence = opening.group(1)
		blocks[-1].lines.append(line)
	if fence:
		# An unclosed fence would hide every later heading of the agents file.
		raise FragmentError("unclosed_fence")
	if not blocks:
		raise FragmentError("no_marker")
	for block in blocks:
		while block.lines and not block.lines[0].strip():
			block.lines.pop(0)
		while block.lines and not block.lines[-1].strip():
			block.lines.pop()
		if not block.lines:
			raise FragmentError("empty_block")
	return blocks


def _h2_index(lines: list[str]) -> list[tuple[int, str]]:
	"""(line index, heading text) of every ``## `` heading outside fenced code."""
	headings: list[tuple[int, str]] = []
	fence = ""
	for index, line in enumerate(lines):
		match = FENCE.match(line)
		if fence:
			if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not line[match.end():].strip():
				fence = ""
			continue
		if match:
			fence = match.group(1)
			continue
		heading = H2.match(line)
		if heading:
			headings.append((index, heading.group(1)))
	return headings


def _section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
	headings = _h2_index(lines)
	for position, (index, text) in enumerate(headings):
		if text == heading:
			end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
			return index, end
	return None


def _content_end(lines: list[str], start: int, end: int) -> int:
	"""Index just after the section's last line that is neither blank nor a ``---`` rule."""
	cursor = end
	while cursor > start + 1 and (not lines[cursor - 1].strip() or lines[cursor - 1].strip() == "---"):
		cursor -= 1
	return cursor


def _joins(previous: str, first: str) -> bool:
	"""True when ``first`` continues the table or list that ``previous`` belongs to."""
	prev, new = previous.lstrip(), first.lstrip()
	if prev.startswith("|") and new.startswith("|"):
		return True
	return bool(re.match(r"^([-*+]|\d+[.)])\s", prev) and re.match(r"^([-*+]|\d+[.)])\s", new))


def _append_to_section(lines: list[str], start: int, end: int, block_lines: list[str]) -> None:
	at = _content_end(lines, start, end)
	payload = list(block_lines)
	if at > start + 1 and not _joins(lines[at - 1], payload[0]):
		payload.insert(0, "")
	lines[at:at] = payload


def _registry_groups(block_lines: list[str]) -> tuple[list[str], list[str], list[str]]:
	"""Split a registry block into bullet lines (with continuations), name lines, and other text."""
	bullets: list[str] = []
	names: list[str] = []
	other: list[str] = []
	in_bullet = False
	for line in block_lines:
		if REGISTRY_BULLET.match(line):
			bullets.append(line)
			in_bullet = True
		elif in_bullet and line[:1] in (" ", "\t") and line.strip():
			bullets.append(line)
		elif REGISTRY_NAME.match(line):
			names.append(line)
			in_bullet = False
		else:
			in_bullet = False
			if line.strip() or other:
				other.append(line)
	while other and not other[-1].strip():
		other.pop()
	return bullets, names, other


def _last_matching(lines: list[str], start: int, end: int, pattern: re.Pattern[str], continuation: bool) -> int | None:
	"""Index just after the last line in [start, end) matching ``pattern`` (and its indented continuation)."""
	last = None
	for index in range(start, end):
		if pattern.match(lines[index]):
			last = index
	if last is None:
		return None
	cursor = last + 1
	if continuation:
		while cursor < end and lines[cursor][:1] in (" ", "\t") and lines[cursor].strip():
			cursor += 1
	return cursor


def apply_block(lines: list[str], block: Block) -> None:
	"""Insert one block into ``lines`` in place; raise FragmentError when its section is missing."""
	bounds = _section_bounds(lines, block.heading)
	if bounds is None:
		if not block.new:
			raise FragmentError("section_not_found")
		anchor = _section_bounds(lines, NEW_SECTION_ANCHOR)
		at = gap_end = anchor[0] if anchor else len(lines)
		while at > 0 and not lines[at - 1].strip():
			at -= 1
		section = ([""] if at > 0 else []) + [f"## {block.heading}", ""] + block.lines
		if anchor and gap_end == at:
			# No blank line was there to keep the anchor heading apart.
			section.append("")
		lines[at:at] = section
		return
	start, end = bounds
	if block.heading != REGISTRY_SECTION:
		_append_to_section(lines, start, end, block.lines)
		return
	bullets, names, other = _registry_groups(block.lines)
	# Insert from the bottom up so earlier indices stay valid.
	if other:
		_append_to_section(lines, start, end, other)
	if names:
		at = _last_matching(lines, start, end, REGISTRY_NAME, continuation=False)
		if at is None:
			_append_to_section(lines, start, end, names)
		else:
			lines[at:at] = names
	if bullets:
		start, end = _section_bounds(lines, block.heading)  # type: ignore[misc]
		first_name = next((index for index in range(start, end) if REGISTRY_NAME.match(lines[index])), end)
		at = _last_matching(lines, start, first_name, REGISTRY_BULLET, continuation=True)
		if at is None and first_name < end:
			# No bullet registry yet but name lines exist: keep bullets first.
			payload = list(bullets) + [""]
			if first_name - 1 > start and lines[first_name - 1].strip():
				payload.insert(0, "")
			lines[first_name:first_name] = payload
		elif at is None:
			_append_to_section(lines, start, end, bullets)
		else:
			lines[at:at] = bullets


def resolve_agents_path(repo_root: Path, explicit: str = "") -> Path:
	if explicit:
		return Path(explicit)
	for name in AGENTS_FILENAMES:
		candidate = repo_root / name
		if candidate.exists() or candidate.is_symlink():
			return candidate
	return repo_root / NEW_AGENTS_FILENAME


def _read_agents(path: Path) -> str:
	if path.is_symlink():
		raise OSError(f"{path} is a symlink; refusing to read or write through it")
	if not path.exists():
		return ""
	return path.read_text(encoding="utf-8")


def fold(text: str, fragments: list[Path]) -> tuple[str, list[Path], list[tuple[Path, str]]]:
	"""Fold ``fragments`` into ``text``. Returns (new text, folded paths, [(skipped path, reason)])."""
	lines = text.split("\n") if text else []
	trailing_newline = text.endswith("\n")
	if trailing_newline:
		lines.pop()
	folded: list[Path] = []
	skipped: list[tuple[Path, str]] = []
	for path in fragments:
		try:
			blocks = parse_fragment(path.read_text(encoding="utf-8"))
			candidate = list(lines)
			for block in blocks:
				apply_block(candidate, block)
		except FragmentError as exc:
			skipped.append((path, str(exc)))
			continue
		except (OSError, UnicodeDecodeError):
			skipped.append((path, "unreadable"))
			continue
		lines = candidate
		folded.append(path)
	result = "\n".join(lines)
	if folded and (trailing_newline or not text):
		result += "\n"
	elif not folded:
		result = text
	return result, folded, skipped


def _write_outputs(**values: object) -> None:
	path = os.environ.get("GITHUB_OUTPUT")
	if not path:
		return
	with open(path, "a", encoding="utf-8") as handle:
		for key, value in values.items():
			handle.write(f"{key}={value}\n")


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	for name in ("assemble", "render", "check"):
		cmd = sub.add_parser(name)
		cmd.add_argument("--repo-root", default=".")
		cmd.add_argument("--agents-file", default="", help="target file (default: agents.md or AGENTS.md at the repo root)")
		cmd.add_argument("--fragments-dir", default="", help=f"fragment directory (default: <repo-root>/{FRAGMENTS_DIRNAME})")
		if name == "assemble":
			cmd.add_argument("--dry-run", action="store_true")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	repo_root = Path(args.repo_root)
	agents_path = resolve_agents_path(repo_root, args.agents_file)
	fragments_dir = Path(args.fragments_dir) if args.fragments_dir else repo_root / FRAGMENTS_DIRNAME
	fragments = iter_fragment_paths(fragments_dir)
	try:
		original = _read_agents(agents_path)
	except (OSError, UnicodeDecodeError) as exc:
		print(f"{LOG_PREFIX} error=agents_unreadable detail={exc}", file=sys.stderr)
		return 1
	result, folded, skipped = fold(original, fragments)
	for path, reason in skipped:
		message = f"{LOG_PREFIX} skipped={path.name} reason={reason}"
		print(message, file=sys.stderr)
		if args.command != "render":
			print(f"::warning::{message}")

	if args.command == "render":
		sys.stdout.write(result)
		return 0
	if args.command == "check":
		_log(outcome="ok" if not skipped else "invalid", fragments=len(fragments), skipped=len(skipped))
		return 1 if skipped else 0

	changed = bool(folded) and result != original
	if changed and not args.dry_run:
		agents_path.write_text(result, encoding="utf-8")
		for path in folded:
			path.unlink()
	_log(
		outcome="assembled" if changed else "noop",
		file=agents_path.name,
		folded=len(folded),
		skipped=len(skipped),
		dry_run=str(args.dry_run).lower(),
	)
	_write_outputs(
		agents_assembled=str(changed and not args.dry_run).lower(),
		agents_fragment_count=len(folded),
		agents_fragment_skipped=len(skipped),
		agents_file=agents_path.name,
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())

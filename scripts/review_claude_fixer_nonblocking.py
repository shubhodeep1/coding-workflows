#!/usr/bin/env python3
"""Move rejected single-reviewer findings out of the Claude-fixer hand-off.

Called by scripts/review_autofix_step_claude_fixer_handoff.sh (Claude-fixer
mode only) before it counts, digests, and posts the reviewer ledger. A
CONSENSUS FINDINGS entry becomes non-blocking when all of these hold
(issue #4586):

  * its header parses as ``- <file>:<start>[-<end>] |`` and its
    ``flagged_by: [...]`` line names exactly one reviewer F, which is a
    successful pass-2 reviewer;
  * at least MIN_REJECTERS successful reviewers other than F each wrote a
    ``REJECTED_FINDING: <file>:<line or start-end> | flagged_by: F | reason:
    ...`` line in their raw pass-2 output for the same file, with a line
    range overlapping the entry's or within LINE_TOLERANCE lines of it;
  * those rejecters are a strict majority of the successful reviewers other
    than F.

The rejection is read from each reviewer's raw output, never from the
summariser's ledger text, so a summariser mistake cannot unblock a finding.
A reviewer counts only when ``status_review_<slug>.txt`` reads ``success``
(the rule reviewer_count_success_statuses in review_run_reviewers.sh uses).

Demoted entries move into a ``=== NON-BLOCKING FINDINGS ===`` block, inserted
after ``=== END CONSENSUS TASK GAPS ===``, with the rejecting reviewers named.
Matching per-reviewer bullets (same file and range, in the flagger's or a
rejecter's ``FINDINGS FROM <slug>`` section) move there too, so the hand-off
step's bullet count and clean-ledger check see only blocking entries. A block
left without entries gets its ``(No findings reported.)`` placeholder back.
Task gaps and entries flagged by several reviewers are never demoted, and an
entry that does not parse stays blocking.

Usage:

  review_claude_fixer_nonblocking.py --ledger FILE --reviews-dir DIR --output FILE

Always writes --output (an unchanged copy when nothing is demoted) and prints
``CLAUDE_FIXER_NONBLOCKING demoted=<n> successful_reviewers=<m>`` followed by
one ``CLAUDE_FIXER_NONBLOCKING_ENTRY`` line per demoted entry. Exit 0 on
success; any other exit means the caller must keep the original ledger (fail
toward blocking). No network access and no GitHub API calls.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MIN_REJECTERS = 2
LINE_TOLERANCE = 3
NONBLOCKING_BLOCK = "NON-BLOCKING FINDINGS"
CONSENSUS_FINDINGS_BLOCK = "CONSENSUS FINDINGS"
CONSENSUS_TASK_GAPS_BLOCK = "CONSENSUS TASK GAPS"
PER_REVIEWER_PREFIX = "FINDINGS FROM "
EMPTY_PLACEHOLDERS = {
	CONSENSUS_FINDINGS_BLOCK: "(No findings reported.)",
	CONSENSUS_TASK_GAPS_BLOCK: "(No task gaps reported.)",
}

BLOCK_START_RE = re.compile(r"^=== (?!END )(.+) ===$")
BLOCK_END_RE = re.compile(r"^=== END (.+) ===$")
ENTRY_HEADER_RE = re.compile(r"^- `?(?P<path>[^\s|`]+?)`?:(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?\s*\|")
FLAGGED_BY_RE = re.compile(r"^\s*flagged_by:\s*\[(?P<slugs>[^\]]*)\]\s*$")
REJECTED_FINDING_RE = re.compile(
	r"^\s*(?:[-*]\s*)?REJECTED_FINDING:\s*`?(?P<path>[^\s|`]+?)`?:(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?"
	r"\s*\|\s*flagged_by:\s*(?P<flagger>[^|]+?)\s*(?:\|.*)?$"
)
SLUG_STRIP = " \t`'\"[]"


def _norm_path(path: str) -> str:
	path = path.strip().strip("`")
	while path.startswith("./"):
		path = path[2:]
	return path


def _norm_slug(slug: str) -> str:
	return slug.strip(SLUG_STRIP)


def _range(start: str, end: str | None) -> tuple[int, int]:
	low = int(start)
	high = int(end) if end else low
	return (low, high) if low <= high else (high, low)


def _near(first: tuple[int, int], second: tuple[int, int]) -> bool:
	gap = max(first[0], second[0]) - min(first[1], second[1])
	return gap <= LINE_TOLERANCE


class Block:
	"""One ``=== NAME ===`` … ``=== END NAME ===`` ledger block."""

	def __init__(self, name: str, start_line: str):
		self.name = name
		self.start_line = start_line
		self.end_line = ""
		self.preamble: list[str] = []
		self.entries: list[list[str]] = []

	def add(self, line: str) -> None:
		if line.startswith("- "):
			self.entries.append([line])
		elif self.entries:
			self.entries[-1].append(line)
		else:
			self.preamble.append(line)

	def render(self) -> list[str]:
		lines = [self.start_line]
		preamble = list(self.preamble)
		placeholder = EMPTY_PLACEHOLDERS.get(self.name, "(No findings reported.)")
		has_text = any(line.strip() for line in preamble)
		if not self.entries and not has_text:
			preamble = [placeholder]
		lines.extend(preamble)
		for entry in self.entries:
			lines.extend(entry)
		lines.append(self.end_line)
		return lines


def parse_ledger(text: str) -> list[object]:
	"""Split the ledger into plain lines and Block objects, in order."""
	segments: list[object] = []
	current: Block | None = None
	for line in text.splitlines():
		if current is None:
			start = BLOCK_START_RE.match(line)
			if start:
				current = Block(start.group(1), line)
				segments.append(current)
			else:
				segments.append(line)
			continue
		end = BLOCK_END_RE.match(line)
		if end:
			if end.group(1) != current.name:
				raise ValueError(f"ledger block '{current.name}' closed by '{line}'")
			current.end_line = line
			current = None
			continue
		current.add(line)
	if current is not None:
		raise ValueError(f"ledger block '{current.name}' is not closed")
	return segments


def entry_location(entry: list[str]) -> tuple[str, tuple[int, int]] | None:
	match = ENTRY_HEADER_RE.match(entry[0])
	if not match:
		return None
	return _norm_path(match.group("path")), _range(match.group("start"), match.group("end"))


def entry_flaggers(entry: list[str]) -> list[str] | None:
	for line in entry[1:]:
		match = FLAGGED_BY_RE.match(line)
		if match:
			return [slug for slug in (_norm_slug(part) for part in match.group("slugs").split(",")) if slug]
	return None


def successful_reviewers(reviews_dir: Path) -> dict[str, Path]:
	"""Map slug -> raw pass-2 output for every reviewer whose status is success."""
	found: dict[str, Path] = {}
	for status_file in sorted(reviews_dir.glob("status_review_*.txt")):
		slug = status_file.name[len("status_review_"):-len(".txt")]
		try:
			status = status_file.read_text(encoding="utf-8", errors="replace").strip()
		except OSError:
			continue
		output = reviews_dir / f"review_{slug}.txt"
		if status == "success" and slug and output.is_file():
			found[slug] = output
	return found


def reviewer_rejections(output: Path) -> list[tuple[str, tuple[int, int], str]]:
	rejections = []
	for line in output.read_text(encoding="utf-8", errors="replace").splitlines():
		match = REJECTED_FINDING_RE.match(line)
		if match:
			rejections.append((
				_norm_path(match.group("path")),
				_range(match.group("start"), match.group("end")),
				_norm_slug(match.group("flagger")),
			))
	return rejections


def demote(ledger_text: str, reviews_dir: Path) -> tuple[str, list[dict]]:
	"""Return the filtered ledger text and one record per demoted entry."""
	segments = parse_ledger(ledger_text)
	blocks = [segment for segment in segments if isinstance(segment, Block)]
	consensus = next((block for block in blocks if block.name == CONSENSUS_FINDINGS_BLOCK), None)
	task_gaps_index = next((index for index, segment in enumerate(segments)
		if isinstance(segment, Block) and segment.name == CONSENSUS_TASK_GAPS_BLOCK), None)
	if consensus is None or task_gaps_index is None:
		raise ValueError("ledger lacks the CONSENSUS FINDINGS or CONSENSUS TASK GAPS block")
	if any(block.name == NONBLOCKING_BLOCK for block in blocks):
		raise ValueError("ledger already carries a NON-BLOCKING FINDINGS block")

	reviewers = successful_reviewers(reviews_dir)
	rejections = {slug: reviewer_rejections(path) for slug, path in reviewers.items()}
	per_reviewer = {block.name[len(PER_REVIEWER_PREFIX):]: block for block in blocks if block.name.startswith(PER_REVIEWER_PREFIX)}

	demoted: list[dict] = []
	kept: list[list[str]] = []
	for entry in consensus.entries:
		location = entry_location(entry)
		flaggers = entry_flaggers(entry)
		if location is None or flaggers is None or len(flaggers) != 1 or flaggers[0] not in reviewers:
			kept.append(entry)
			continue
		path, lines = location
		flagger = flaggers[0]
		others = [slug for slug in reviewers if slug != flagger]
		rejecters = sorted(slug for slug in others if any(
			r_path == path and r_flagger == flagger and _near(lines, r_lines)
			for r_path, r_lines, r_flagger in rejections[slug]))
		if len(rejecters) < MIN_REJECTERS or 2 * len(rejecters) <= len(others):
			kept.append(entry)
			continue
		moved: list[tuple[str, list[str]]] = []
		for slug in [flagger, *rejecters]:
			block = per_reviewer.get(slug)
			if block is None:
				continue
			remaining = []
			for candidate in block.entries:
				candidate_location = entry_location(candidate)
				if candidate_location and candidate_location[0] == path and _near(lines, candidate_location[1]):
					moved.append((slug, candidate))
				else:
					remaining.append(candidate)
			block.entries = remaining
		demoted.append({
			"entry": entry,
			"path": path,
			"lines": lines,
			"flagger": flagger,
			"rejecters": rejecters,
			"others": len(others),
			"moved": moved,
		})
	consensus.entries = kept

	if demoted:
		nonblocking = Block(NONBLOCKING_BLOCK, f"=== {NONBLOCKING_BLOCK} ===")
		nonblocking.end_line = f"=== END {NONBLOCKING_BLOCK} ==="
		nonblocking.preamble.append(
			"(Not handed to Claude: each entry below was raised by one reviewer and explicitly rejected with a "
			"REJECTED_FINDING line by a majority of the other reviewers. Kept for visibility; no fix or verdict is needed.)")
		for record in demoted:
			entry = list(record["entry"])
			entry.append(f"  rejected_by: [{', '.join(record['rejecters'])}] ({len(record['rejecters'])} of {record['others']} other reviewers)")
			for slug, bullet in record["moved"]:
				entry.append(f"  moved from FINDINGS FROM {slug}:")
				entry.extend(f"    {line}" for line in bullet)
			nonblocking.entries.append(entry)
		segments.insert(task_gaps_index + 1, nonblocking)

	out_lines: list[str] = []
	for segment in segments:
		out_lines.extend(segment.render() if isinstance(segment, Block) else [segment])
	text = "\n".join(out_lines)
	if ledger_text.endswith("\n"):
		text += "\n"
	return (text if demoted else ledger_text), demoted


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--ledger", required=True, type=Path)
	parser.add_argument("--reviews-dir", required=True, type=Path)
	parser.add_argument("--output", required=True, type=Path)
	args = parser.parse_args(argv)
	try:
		ledger_text = args.ledger.read_text(encoding="utf-8")
		if not args.reviews_dir.is_dir():
			raise ValueError(f"reviews dir {args.reviews_dir} does not exist")
		filtered, demoted = demote(ledger_text, args.reviews_dir)
		args.output.write_text(filtered, encoding="utf-8")
	except (OSError, ValueError) as exc:
		print(f"CLAUDE_FIXER_NONBLOCKING error={exc}", file=sys.stderr)
		return 1
	print(f"CLAUDE_FIXER_NONBLOCKING demoted={len(demoted)} successful_reviewers={len(successful_reviewers(args.reviews_dir))}")
	for record in demoted:
		low, high = record["lines"]
		span = f"{low}" if low == high else f"{low}-{high}"
		print(f"CLAUDE_FIXER_NONBLOCKING_ENTRY file={record['path']}:{span} flagged_by={record['flagger']} "
			f"rejected_by={','.join(record['rejecters'])} others={record['others']}")
	return 0


if __name__ == "__main__":
	sys.exit(main())

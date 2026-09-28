#!/usr/bin/env python3
"""Move rejected single-reviewer findings out of the Claude-fixer hand-off.

Called by scripts/review_autofix_step_claude_fixer_handoff.sh (Claude-fixer
mode only) before it counts, digests, and posts the reviewer ledger. A
CONSENSUS FINDINGS entry becomes non-blocking when all of these hold
(issue #4586, bound to consensus ids by issue #4687):

  * its header parses as ``- <file>:<start>[-<end>] |`` and its
    ``flagged_by: [...]`` line names exactly one reviewer F, which is a
    successful pass-2 reviewer;
  * it carries exactly one ``consensus_id: p1-<12 hex>`` line, and no other
    CONSENSUS FINDINGS entry of the ledger carries that id;
  * the id names exactly one CONSENSUS FINDINGS entry P of the pass-1 ledger
    (``consensus_pass1.txt`` in --reviews-dir, the ledger pass-2 reviewers saw
    in the cross-pollination summary), P is flagged by F alone, and P has the
    same file and an overlapping line range;
  * F's own raw pass-2 output contains the id, so the flagger itself tied its
    pass-2 finding to P (the summariser only copies the line);
  * the match is unambiguous: no other pass-1 CONSENSUS FINDINGS entry lies in
    the same file within LINE_TOLERANCE lines of P, and no other CONSENSUS
    FINDINGS entry of this ledger lies in the same file within LINE_TOLERANCE
    lines of the entry;
  * at least MIN_REJECTERS successful reviewers other than F each wrote a
    ``REJECTED_FINDING: <consensus_id> | <file>:<line or start-end> |
    flagged_by: F | reason: ...`` line in their raw pass-2 output whose id is
    P's, whose file is P's, whose range overlaps P's, and whose flagger is F;
  * those rejecters are a strict majority of the successful reviewers other
    than F.

A rejection is bound to the finding it names by that id, never by file and
line proximity: a rejection of one entry cannot demote a distinct finding on a
nearby line (issue #4687). A REJECTED_FINDING line without an id is ignored
and only counted. The rejection is read from each reviewer's raw output, never
from the summariser's ledger text, so a summariser mistake cannot unblock a
finding. A reviewer counts only when ``status_review_<slug>.txt`` reads
``success`` (the rule reviewer_count_success_statuses in
review_run_reviewers.sh uses).

The id is ``p1-`` plus the first 12 hex digits of the SHA-256 of the pass-1
entry's lines (see consensus_id()). ``--annotate`` writes a copy of the pass-1
ledger with a ``consensus_id:`` line after each CONSENSUS FINDINGS header; it
is what build_cross_pollination_summary in review_run_reviewers.sh shows
pass-2 reviewers. consensus_pass1.txt itself is never rewritten, so both modes
compute the same ids, and a regenerated ledger invalidates every old id
instead of rebinding it.

Demoted entries move into a ``=== NON-BLOCKING FINDINGS ===`` block, inserted
after ``=== END CONSENSUS TASK GAPS ===``, with the rejecting reviewers named.
Per-reviewer bullets in the flagger's or a rejecter's ``FINDINGS FROM <slug>``
section move there too when their range overlaps the entry's and their text
carries the same id, so the hand-off step's bullet count and clean-ledger
check see only blocking entries. A block left without entries gets its
``(No findings reported.)`` placeholder back. Task gaps and entries flagged by
several reviewers are never demoted, and an entry that does not parse stays
blocking.

Usage:

  review_claude_fixer_nonblocking.py --ledger FILE --reviews-dir DIR --output FILE
  review_claude_fixer_nonblocking.py --annotate --ledger PASS1_LEDGER --output FILE

The first form always writes --output (an unchanged copy when nothing is
demoted) and prints ``CLAUDE_FIXER_NONBLOCKING demoted=<n>
successful_reviewers=<m>``, one ``CLAUDE_FIXER_NONBLOCKING_ENTRY`` line per
demoted entry, one ``CLAUDE_FIXER_NONBLOCKING_KEPT file=<file:span>
flagged_by=<slug> reason=<reason>`` line per single-reviewer entry that stays
blocking, and ``CLAUDE_FIXER_NONBLOCKING_LEGACY_REJECTIONS count=<n>`` when
id-less REJECTED_FINDING lines were ignored. The second form prints
``CLAUDE_FIXER_CONSENSUS_IDS annotated=<n>``. Exit 0 on success; any other
exit means the caller must keep the original ledger (fail toward blocking).
No network access and no GitHub API calls.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

MIN_REJECTERS = 2
LINE_TOLERANCE = 3
NONBLOCKING_BLOCK = "NON-BLOCKING FINDINGS"
CONSENSUS_FINDINGS_BLOCK = "CONSENSUS FINDINGS"
CONSENSUS_TASK_GAPS_BLOCK = "CONSENSUS TASK GAPS"
PER_REVIEWER_PREFIX = "FINDINGS FROM "
PASS1_LEDGER_NAME = "consensus_pass1.txt"
CONSENSUS_ID_PREFIX = "p1-"
CONSENSUS_ID_HEX_DIGITS = 12
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
CONSENSUS_ID_PATTERN = rf"{re.escape(CONSENSUS_ID_PREFIX)}[0-9a-f]{{{CONSENSUS_ID_HEX_DIGITS}}}"
CONSENSUS_ID_RE = re.compile(rf"^\s*consensus_id:\s*`?(?P<consensus_id>{CONSENSUS_ID_PATTERN})`?\s*$")
REJECTED_FINDING_ID_RE = re.compile(
	rf"^\s*(?:[-*]\s*)?REJECTED_FINDING:\s*`?(?P<consensus_id>{CONSENSUS_ID_PATTERN})`?\s*\|\s*(?P<rest>.*)$"
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


def _overlaps(first: tuple[int, int], second: tuple[int, int]) -> bool:
	return max(first[0], second[0]) <= min(first[1], second[1])


def _span(lines: tuple[int, int]) -> str:
	low, high = lines
	return f"{low}" if low == high else f"{low}-{high}"


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


def _render(segments: list[object], source_text: str) -> str:
	out_lines: list[str] = []
	for segment in segments:
		out_lines.extend(segment.render() if isinstance(segment, Block) else [segment])
	text = "\n".join(out_lines)
	if source_text.endswith("\n"):
		text += "\n"
	return text


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


def entry_consensus_ids(entry: list[str]) -> list[str]:
	"""Every ``consensus_id:`` line of an entry (a well-formed entry has at most one)."""
	return [match.group("consensus_id") for match in (CONSENSUS_ID_RE.match(line) for line in entry[1:]) if match]


def consensus_id(entry: list[str]) -> str:
	"""The id of a pass-1 CONSENSUS FINDINGS entry, derived from its text.

	SHA-256 over the entry's lines, right-stripped and joined with newlines,
	without any ``consensus_id:`` line and without trailing blank lines, so
	annotating an entry never changes its id.
	"""
	lines = [line.rstrip() for line in entry if not CONSENSUS_ID_RE.match(line)]
	while lines and not lines[-1]:
		lines.pop()
	digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
	return f"{CONSENSUS_ID_PREFIX}{digest[:CONSENSUS_ID_HEX_DIGITS]}"


def _consensus_block(segments: list[object]) -> Block | None:
	return next((segment for segment in segments
		if isinstance(segment, Block) and segment.name == CONSENSUS_FINDINGS_BLOCK), None)


def annotate(ledger_text: str) -> tuple[str, int]:
	"""Insert a ``consensus_id:`` line after each CONSENSUS FINDINGS header.

	Returns the annotated text and the number of entries annotated. An entry
	that already carries an id line gets it recomputed, so annotating twice
	gives the same text.
	"""
	segments = parse_ledger(ledger_text)
	consensus = _consensus_block(segments)
	if consensus is None:
		raise ValueError("ledger lacks the CONSENSUS FINDINGS block")
	for entry in consensus.entries:
		kept_lines = [entry[0], *(line for line in entry[1:] if not CONSENSUS_ID_RE.match(line))]
		entry[:] = [kept_lines[0], f"  consensus_id: {consensus_id(kept_lines)}", *kept_lines[1:]]
	return _render(segments, ledger_text), len(consensus.entries)


def pass1_consensus_entries(reviews_dir: Path) -> list[dict]:
	"""The pass-1 CONSENSUS FINDINGS entries with their ids, or [] without a pass-1 ledger."""
	ledger = reviews_dir / PASS1_LEDGER_NAME
	if not ledger.is_file():
		return []
	consensus = _consensus_block(parse_ledger(ledger.read_text(encoding="utf-8", errors="replace")))
	if consensus is None:
		return []
	return [{
		"consensus_id": consensus_id(entry),
		"location": entry_location(entry),
		"flaggers": entry_flaggers(entry),
	} for entry in consensus.entries]


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
	"""REJECTED_FINDING lines without a consensus id (the pre-#4687 shape); they bind nothing."""
	return _legacy_rejections_in(output.read_text(encoding="utf-8", errors="replace"))


def _legacy_rejections_in(text: str) -> list[tuple[str, tuple[int, int], str]]:
	"""``reviewer_rejections`` over an output already read into memory."""
	rejections = []
	for line in text.splitlines():
		match = REJECTED_FINDING_RE.match(line)
		if match:
			rejections.append((
				_norm_path(match.group("path")),
				_range(match.group("start"), match.group("end")),
				_norm_slug(match.group("flagger")),
			))
	return rejections


def reviewer_bound_rejections(output: Path) -> list[tuple[str, str, tuple[int, int], str]]:
	"""``REJECTED_FINDING: <consensus_id> | <file>:<range> | flagged_by: <slug>`` lines of one output."""
	return _bound_rejections_in(output.read_text(encoding="utf-8", errors="replace"))


def _bound_rejections_in(text: str) -> list[tuple[str, str, tuple[int, int], str]]:
	"""``reviewer_bound_rejections`` over an output already read into memory."""
	rejections = []
	for line in text.splitlines():
		bound = REJECTED_FINDING_ID_RE.match(line)
		if not bound:
			continue
		match = REJECTED_FINDING_RE.match(f"REJECTED_FINDING: {bound.group('rest')}")
		if match:
			rejections.append((
				bound.group("consensus_id"),
				_norm_path(match.group("path")),
				_range(match.group("start"), match.group("end")),
				_norm_slug(match.group("flagger")),
			))
	return rejections


def demote_with_diagnostics(ledger_text: str, reviews_dir: Path, *,
		reviewers: dict[str, Path] | None = None) -> tuple[str, list[dict], list[dict], int]:
	"""Return the filtered ledger, the demoted records, the kept single-reviewer
	records (with the reason each stays blocking), and the number of ignored
	id-less REJECTED_FINDING lines. ``reviewers`` is ``successful_reviewers(reviews_dir)``
	when the caller already has it; each reviewer output is read once."""
	segments = parse_ledger(ledger_text)
	blocks = [segment for segment in segments if isinstance(segment, Block)]
	consensus = _consensus_block(segments)
	task_gaps_index = next((index for index, segment in enumerate(segments)
		if isinstance(segment, Block) and segment.name == CONSENSUS_TASK_GAPS_BLOCK), None)
	if consensus is None or task_gaps_index is None:
		raise ValueError("ledger lacks the CONSENSUS FINDINGS or CONSENSUS TASK GAPS block")
	if any(block.name == NONBLOCKING_BLOCK for block in blocks):
		raise ValueError("ledger already carries a NON-BLOCKING FINDINGS block")

	if reviewers is None:
		reviewers = successful_reviewers(reviews_dir)
	outputs = {slug: path.read_text(encoding="utf-8", errors="replace") for slug, path in reviewers.items()}
	rejections = {slug: _bound_rejections_in(text) for slug, text in outputs.items()}
	legacy_rejections = sum(len(_legacy_rejections_in(text)) for text in outputs.values())
	per_reviewer = {block.name[len(PER_REVIEWER_PREFIX):]: block for block in blocks if block.name.startswith(PER_REVIEWER_PREFIX)}

	pass1 = pass1_consensus_entries(reviews_dir)
	pass1_id_counts = Counter(record["consensus_id"] for record in pass1)
	pass1_by_id = {record["consensus_id"]: record for record in pass1 if pass1_id_counts[record["consensus_id"]] == 1}
	ledger_id_counts = Counter(cid for entry in consensus.entries for cid in entry_consensus_ids(entry))
	ledger_locations = [entry_location(entry) for entry in consensus.entries]

	demoted: list[dict] = []
	kept_records: list[dict] = []
	kept: list[list[str]] = []
	for index, entry in enumerate(consensus.entries):
		location = ledger_locations[index]
		flaggers = entry_flaggers(entry)
		if flaggers is None or len(flaggers) != 1:
			kept.append(entry)
			continue
		flagger = flaggers[0]

		def keep(reason: str) -> None:
			kept.append(entry)
			kept_records.append({"location": location, "flagger": flagger, "reason": reason})

		if location is None:
			keep("unparsed")
			continue
		if flagger not in reviewers:
			keep("flagger_not_successful")
			continue
		path, lines = location
		entry_ids = entry_consensus_ids(entry)
		if len(entry_ids) != 1:
			keep("no_consensus_id" if not entry_ids else "multiple_consensus_ids")
			continue
		cid = entry_ids[0]
		if ledger_id_counts[cid] != 1 or pass1_id_counts[cid] > 1:
			keep("duplicate_consensus_id")
			continue
		source = pass1_by_id.get(cid)
		if source is None:
			keep("unknown_consensus_id")
			continue
		if (source["location"] is None or source["flaggers"] != [flagger] or source["location"][0] != path
				or not _overlaps(source["location"][1], lines)):
			keep("consensus_id_mismatch")
			continue
		if cid not in outputs[flagger]:
			keep("flagger_did_not_cite")
			continue
		source_lines = source["location"][1]
		if any(other is not source and other["location"] is not None and other["location"][0] == path
				and _near(other["location"][1], source_lines) for other in pass1):
			keep("ambiguous_nearby_pass1")
			continue
		if any(other_index != index and other is not None and other[0] == path and _near(other[1], lines)
				for other_index, other in enumerate(ledger_locations)):
			keep("ambiguous_nearby")
			continue
		others = [slug for slug in reviewers if slug != flagger]
		rejecters = sorted(slug for slug in others if any(
			r_id == cid and r_path == path and r_flagger == flagger and _overlaps(source_lines, r_lines)
			for r_id, r_path, r_lines, r_flagger in rejections[slug]))
		if len(rejecters) < MIN_REJECTERS or 2 * len(rejecters) <= len(others):
			keep("too_few_rejecters")
			continue
		moved: list[tuple[str, list[str]]] = []
		for slug in [flagger, *rejecters]:
			block = per_reviewer.get(slug)
			if block is None:
				continue
			remaining = []
			for candidate in block.entries:
				candidate_location = entry_location(candidate)
				if (candidate_location and candidate_location[0] == path and _overlaps(lines, candidate_location[1])
						and cid in "\n".join(candidate)):
					moved.append((slug, candidate))
				else:
					remaining.append(candidate)
			block.entries = remaining
		demoted.append({
			"entry": entry,
			"path": path,
			"lines": lines,
			"flagger": flagger,
			"consensus_id": cid,
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
			"REJECTED_FINDING line citing its consensus_id by a majority of the other reviewers. Kept for visibility; "
			"no fix or verdict is needed.)")
		for record in demoted:
			entry = list(record["entry"])
			entry.append(f"  rejected_by: [{', '.join(record['rejecters'])}] ({len(record['rejecters'])} of {record['others']} other reviewers)")
			for slug, bullet in record["moved"]:
				entry.append(f"  moved from FINDINGS FROM {slug}:")
				entry.extend(f"    {line}" for line in bullet)
			nonblocking.entries.append(entry)
		segments.insert(task_gaps_index + 1, nonblocking)

	text = _render(segments, ledger_text)
	return (text if demoted else ledger_text), demoted, kept_records, legacy_rejections


def demote(ledger_text: str, reviews_dir: Path) -> tuple[str, list[dict]]:
	"""Return the filtered ledger text and one record per demoted entry."""
	text, demoted, _kept, _legacy = demote_with_diagnostics(ledger_text, reviews_dir)
	return text, demoted


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--ledger", required=True, type=Path)
	parser.add_argument("--reviews-dir", type=Path)
	parser.add_argument("--output", required=True, type=Path)
	parser.add_argument("--annotate", action="store_true",
		help="write --ledger (a pass-1 ledger) with a consensus_id line on each CONSENSUS FINDINGS entry")
	args = parser.parse_args(argv)
	if args.annotate:
		try:
			annotated, count = annotate(args.ledger.read_text(encoding="utf-8"))
			args.output.write_text(annotated, encoding="utf-8")
		except (OSError, ValueError) as exc:
			print(f"CLAUDE_FIXER_CONSENSUS_IDS error={exc}", file=sys.stderr)
			return 1
		print(f"CLAUDE_FIXER_CONSENSUS_IDS annotated={count}")
		return 0
	if args.reviews_dir is None:
		parser.error("--reviews-dir is required unless --annotate is given")
	try:
		ledger_text = args.ledger.read_text(encoding="utf-8")
		if not args.reviews_dir.is_dir():
			raise ValueError(f"reviews dir {args.reviews_dir} does not exist")
		reviewers = successful_reviewers(args.reviews_dir)
		filtered, demoted, kept_records, legacy_rejections = demote_with_diagnostics(ledger_text, args.reviews_dir,
			reviewers=reviewers)
		args.output.write_text(filtered, encoding="utf-8")
	except (OSError, ValueError) as exc:
		print(f"CLAUDE_FIXER_NONBLOCKING error={exc}", file=sys.stderr)
		return 1
	print(f"CLAUDE_FIXER_NONBLOCKING demoted={len(demoted)} successful_reviewers={len(reviewers)}")
	for record in demoted:
		print(f"CLAUDE_FIXER_NONBLOCKING_ENTRY file={record['path']}:{_span(record['lines'])} flagged_by={record['flagger']} "
			f"rejected_by={','.join(record['rejecters'])} others={record['others']}")
	for record in kept_records:
		where = "unparsed" if record["location"] is None else f"{record['location'][0]}:{_span(record['location'][1])}"
		print(f"CLAUDE_FIXER_NONBLOCKING_KEPT file={where} flagged_by={record['flagger']} reason={record['reason']}")
	if legacy_rejections:
		print(f"CLAUDE_FIXER_NONBLOCKING_LEGACY_REJECTIONS count={legacy_rejections}")
	return 0


if __name__ == "__main__":
	sys.exit(main())

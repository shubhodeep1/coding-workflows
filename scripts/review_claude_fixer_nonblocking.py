#!/usr/bin/env python3
"""Move rejected single-reviewer findings out of the Claude-fixer hand-off.

Called by scripts/review_autofix_step_claude_fixer_handoff.sh (Claude-fixer
mode only) before it counts, digests, and posts the reviewer ledger. A
CONSENSUS FINDINGS entry becomes non-blocking when all of these hold
(issue #4586):

  * its header parses as ``- <file>:<start>[-<end>] |`` and its
    ``flagged_by: [...]`` line names exactly one reviewer F, which is a
    successful pass-2 reviewer;
  * at least MIN_REJECTERS successful reviewers other than F each cast a
    rejection vote for a pass-1 finding that names the same file, a line
    range overlapping the entry's or within LINE_TOLERANCE lines of it, and
    F as its only flagger;
  * those rejecters are a strict majority of the successful reviewers other
    than F.

Rejection votes are bound to finding IDs issued for this run (issue #4688).
After pass 1, ``--issue-ids`` gives every pass-1 CONSENSUS FINDINGS entry
with a parseable location and exactly one flagger a random ID
(``RF-<16 hex>``), writes them to a manifest
(``<reviews-dir>/rejection_ids_pass1.json`` by default), and prints the list
the cross-pollination header shows pass-2 reviewers. A vote is a line

  REJECTED_FINDING: <ID> | <file>:<line> | flagged_by: <slug> | reason: <one sentence>

and it counts only when the ID is in the manifest, the reason is not empty,
and the line starts a line (at most 3 spaces, an optional ``-``/``*``
bullet) outside any fenced code block. The file, line, and slug a reviewer
echoes are informational: the match uses the manifest entry the ID names.
No PR content can carry an ID issued after it was pushed, so a
REJECTED_FINDING line quoted from the PR never counts, and neither does a
line in the pre-#4688 shape without an ID. Without a manifest no vote
counts, so every finding stays blocking.

Votes are read from each reviewer's raw output, never from the summariser's
ledger text, so a summariser mistake cannot unblock a finding. A reviewer
counts only when ``status_review_<slug>.txt`` reads ``success`` (the rule
reviewer_count_success_statuses in review_run_reviewers.sh uses), and each
reviewer counts once per finding.

Demoted entries move into a ``=== NON-BLOCKING FINDINGS ===`` block, inserted
after ``=== END CONSENSUS TASK GAPS ===``, with the rejecting reviewers named.
Matching per-reviewer bullets (same file and range, in the flagger's or a
rejecter's ``FINDINGS FROM <slug>`` section) move there too, so the hand-off
step's bullet count and clean-ledger check see only blocking entries. A block
left without entries gets its ``(No findings reported.)`` placeholder back.
Task gaps and entries flagged by several reviewers are never demoted, and an
entry that does not parse stays blocking.

Usage:

  review_claude_fixer_nonblocking.py --ledger FILE --reviews-dir DIR --output FILE [--ids-manifest FILE]
  review_claude_fixer_nonblocking.py --issue-ids --ledger PASS1_LEDGER --ids-manifest FILE

The first form always writes --output (an unchanged copy when nothing is
demoted) and prints ``CLAUDE_FIXER_NONBLOCKING demoted=<n>
successful_reviewers=<m>``, then ``CLAUDE_FIXER_NONBLOCKING_VOTES
manifest=<present|missing> ids=<n> votes=<n>``, then one
``CLAUDE_FIXER_NONBLOCKING_ENTRY`` line per demoted entry. A manifest that
is present but invalid is an error. Exit 0 on success; any other exit means
the caller must keep the original ledger (fail toward blocking).

The second form (called by build_cross_pollination_summary in
review_run_reviewers.sh) issues fresh IDs on every call, overwrites the
manifest, and prints one ``<ID> -> <file>:<line or start-end> | flagged_by:
<slug>`` line per ID on stdout and ``CLAUDE_FIXER_NONBLOCKING_IDS
issued=<n>`` on stderr. Because a rebuilt header gets new IDs, votes in
cached pass-2 outputs from an earlier run stop counting (fail toward
blocking); only a same-head resume that skips the whole reviewer phase, and
so writes no new output, keeps the persisted manifest. Any other exit means
the caller must show reviewers no rejection instructions.

No network access and no GitHub API calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
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

# Finding-ID-bound rejection votes (issue #4688). REJECTED_FINDING_RE above is
# the pre-#4688 shape; it is kept for compatibility but no longer counts.
MANIFEST_NAME = "rejection_ids_pass1.json"
MANIFEST_SCHEMA = "rejection_ids.v1"
FINDING_ID_RE = re.compile(r"^RF-[0-9a-f]{16}$")
REJECTION_VOTE_RE = re.compile(r"^ {0,3}(?:[-*] +)?REJECTED_FINDING:[ \t]*`?(?P<id>RF-[0-9a-f]{16})`?[ \t]*\|(?P<rest>.*)$")
VOTE_REASON_RE = re.compile(r"(?:^|\|)[ \t]*reason:[ \t]*(?P<reason>\S.*?)[ \t]*$")
VOTE_REASON_PLACEHOLDER_RE = re.compile(r"^<[^>]*>$")
FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")


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
	"""Pre-#4688 parser: every REJECTED_FINDING line, quoted or not. demote() no longer uses it."""
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


def _write_manifest(manifest_path: Path, payload: dict) -> None:
	"""Write the manifest atomically, so a reader never sees a partial file."""
	manifest_path.parent.mkdir(parents=True, exist_ok=True)
	handle, temp_name = tempfile.mkstemp(prefix=f".{manifest_path.name}.", dir=manifest_path.parent)
	try:
		with os.fdopen(handle, "w", encoding="utf-8") as stream:
			json.dump(payload, stream, indent=1, sort_keys=True)
			stream.write("\n")
		os.replace(temp_name, manifest_path)
	except BaseException:
		try:
			os.unlink(temp_name)
		except OSError:
			pass
		raise


def _manifest_entries(payload: object) -> dict[str, tuple[str, tuple[int, int], str]]:
	"""Validate a manifest payload; return ID -> (path, (start, end), flagger)."""
	if not isinstance(payload, dict) or payload.get("schema") != MANIFEST_SCHEMA or not isinstance(payload.get("entries"), list):
		raise ValueError(f"rejection ID manifest is not a {MANIFEST_SCHEMA} object")
	entries: dict[str, tuple[str, tuple[int, int], str]] = {}
	for item in payload["entries"]:
		if not isinstance(item, dict):
			raise ValueError("rejection ID manifest entry is not an object")
		finding_id, path, flagger = item.get("id"), item.get("path"), item.get("flagger")
		start, end = item.get("start"), item.get("end")
		if not (isinstance(finding_id, str) and FINDING_ID_RE.match(finding_id)):
			raise ValueError(f"rejection ID manifest entry has an invalid id: {finding_id!r}")
		if finding_id in entries:
			raise ValueError(f"rejection ID manifest repeats id {finding_id}")
		if not (isinstance(path, str) and path and isinstance(flagger, str) and flagger):
			raise ValueError(f"rejection ID manifest entry {finding_id} lacks a path or flagger")
		if not (type(start) is int and type(end) is int and 0 <= start <= end):
			raise ValueError(f"rejection ID manifest entry {finding_id} has an invalid line range")
		entries[finding_id] = (path, (start, end), flagger)
	return entries


def load_manifest(manifest_path: Path) -> dict[str, tuple[str, tuple[int, int], str]] | None:
	"""Return the manifest's entries, or None when there is no manifest. An invalid one raises ValueError."""
	if not manifest_path.is_file():
		return None
	try:
		payload = json.loads(manifest_path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as exc:
		raise ValueError(f"rejection ID manifest {manifest_path} is not JSON: {exc}") from exc
	return _manifest_entries(payload)


def issue_ids(ledger_text: str, manifest_path: Path) -> list[dict]:
	"""Give every single-flagger pass-1 CONSENSUS FINDINGS entry a fresh random ID.

	Every call issues new IDs and overwrites the manifest, so no ID outlives
	the header it was shown in: an ID a reviewer wrote in an earlier run (and
	that may since have been published, e.g. in an uploaded log artifact)
	never counts in a new pass 2. The manifest records the ledger's sha256.
	"""
	digest = hashlib.sha256(ledger_text.encode("utf-8")).hexdigest()
	segments = parse_ledger(ledger_text)
	consensus = next((segment for segment in segments
		if isinstance(segment, Block) and segment.name == CONSENSUS_FINDINGS_BLOCK), None)
	if consensus is None:
		raise ValueError("pass-1 ledger lacks the CONSENSUS FINDINGS block")
	entries: list[dict] = []
	for entry in consensus.entries:
		location = entry_location(entry)
		flaggers = entry_flaggers(entry)
		if location is None or flaggers is None or len(flaggers) != 1:
			continue
		path, (start, end) = location
		entries.append({"id": f"RF-{secrets.token_hex(8)}", "path": path, "start": start, "end": end, "flagger": flaggers[0]})
	_write_manifest(manifest_path, {"schema": MANIFEST_SCHEMA, "ledger_sha256": digest, "entries": entries})
	return entries


def _fence_marker(line: str) -> str | None:
	match = FENCE_RE.match(line)
	return match.group("fence") if match else None


def reviewer_votes(output: Path, manifest: dict[str, tuple[str, tuple[int, int], str]]) -> set[str]:
	"""Return the manifest IDs this reviewer voted to reject.

	A vote is a REJECTION_VOTE_RE line outside any fenced code block, naming
	an ID in the manifest, with a non-empty reason that is not the template
	placeholder. Anything else (the pre-#4688 shape, quoted or fenced text,
	an unknown ID) is ignored.
	"""
	votes: set[str] = set()
	open_fence: str | None = None
	for line in output.read_text(encoding="utf-8", errors="replace").splitlines():
		fence = _fence_marker(line)
		if open_fence is not None:
			if fence and fence[0] == open_fence[0] and len(fence) >= len(open_fence) and not line.strip()[len(fence):].strip():
				open_fence = None
			continue
		if fence:
			open_fence = fence
			continue
		match = REJECTION_VOTE_RE.match(line)
		if not match or match.group("id") not in manifest:
			continue
		reason = VOTE_REASON_RE.search(match.group("rest"))
		if reason and not VOTE_REASON_PLACEHOLDER_RE.match(reason.group("reason")):
			votes.add(match.group("id"))
	return votes


def demote(ledger_text: str, reviews_dir: Path, manifest_path: Path | None = None) -> tuple[str, list[dict]]:
	"""Return the filtered ledger text and one record per demoted entry.

	manifest_path defaults to reviews_dir / MANIFEST_NAME; without a manifest
	no vote counts and nothing is demoted.
	"""
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
	manifest = load_manifest(manifest_path if manifest_path is not None else reviews_dir / MANIFEST_NAME) or {}
	votes = {slug: reviewer_votes(path, manifest) for slug, path in reviewers.items()} if manifest else {slug: set() for slug in reviewers}
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
			manifest[vote][0] == path and manifest[vote][2] == flagger and _near(lines, manifest[vote][1])
			for vote in votes[slug]))
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


def _format_span(low: int, high: int) -> str:
	return f"{low}" if low == high else f"{low}-{high}"


def _main_issue_ids(args: argparse.Namespace) -> int:
	try:
		entries = issue_ids(args.ledger.read_text(encoding="utf-8"), args.ids_manifest)
	except (OSError, ValueError) as exc:
		print(f"CLAUDE_FIXER_NONBLOCKING_IDS error={exc}", file=sys.stderr)
		return 1
	for entry in entries:
		print(f"{entry['id']} -> {entry['path']}:{_format_span(entry['start'], entry['end'])} | flagged_by: {entry['flagger']}")
	print(f"CLAUDE_FIXER_NONBLOCKING_IDS issued={len(entries)}", file=sys.stderr)
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--ledger", required=True, type=Path)
	parser.add_argument("--reviews-dir", type=Path)
	parser.add_argument("--output", type=Path)
	parser.add_argument("--ids-manifest", type=Path,
		help=f"rejection ID manifest (default: <reviews-dir>/{MANIFEST_NAME}; required with --issue-ids)")
	parser.add_argument("--issue-ids", action="store_true",
		help="issue finding IDs for the pass-1 ledger given as --ledger and write --ids-manifest")
	args = parser.parse_args(argv)
	if args.issue_ids:
		if args.ids_manifest is None:
			parser.error("--issue-ids needs --ids-manifest")
		return _main_issue_ids(args)
	if args.reviews_dir is None or args.output is None:
		parser.error("--reviews-dir and --output are required")
	manifest_path = args.ids_manifest if args.ids_manifest is not None else args.reviews_dir / MANIFEST_NAME
	try:
		ledger_text = args.ledger.read_text(encoding="utf-8")
		if not args.reviews_dir.is_dir():
			raise ValueError(f"reviews dir {args.reviews_dir} does not exist")
		filtered, demoted = demote(ledger_text, args.reviews_dir, manifest_path)
		args.output.write_text(filtered, encoding="utf-8")
		reviewers = successful_reviewers(args.reviews_dir)
		manifest = load_manifest(manifest_path)
		vote_count = sum(len(reviewer_votes(path, manifest)) for path in reviewers.values()) if manifest else 0
	except (OSError, ValueError) as exc:
		print(f"CLAUDE_FIXER_NONBLOCKING error={exc}", file=sys.stderr)
		return 1
	print(f"CLAUDE_FIXER_NONBLOCKING demoted={len(demoted)} successful_reviewers={len(reviewers)}")
	print(f"CLAUDE_FIXER_NONBLOCKING_VOTES manifest={'missing' if manifest is None else 'present'} "
		f"ids={len(manifest or {})} votes={vote_count}")
	for record in demoted:
		low, high = record["lines"]
		print(f"CLAUDE_FIXER_NONBLOCKING_ENTRY file={record['path']}:{_format_span(low, high)} flagged_by={record['flagger']} "
			f"rejected_by={','.join(record['rejecters'])} others={record['others']}")
	return 0


if __name__ == "__main__":
	sys.exit(main())

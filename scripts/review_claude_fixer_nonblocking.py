#!/usr/bin/env python3
"""Move rejected single-reviewer findings out of the Claude-fixer hand-off.

Called by scripts/review_autofix_step_claude_fixer_handoff.sh (Claude-fixer
mode only) before it counts, digests, and posts the reviewer ledger. A
CONSENSUS FINDINGS entry becomes non-blocking when all of these hold
(issue #4586, votes bound to run IDs by issue #4688, and findings bound to
consensus ids by issue #4687):

  * its header parses as ``- <file>:<start>[-<end>] |`` and its
    ``flagged_by: [...]`` line names exactly one reviewer F, which is a
    successful pass-2 reviewer;
  * it carries exactly one ``consensus_id: p1-<12 hex>`` line, and no other
    CONSENSUS FINDINGS entry of the ledger carries that id;
  * the id names exactly one CONSENSUS FINDINGS entry P of the pass-1 ledger
    (``consensus_pass1.txt`` in --reviews-dir, the ledger pass-2 reviewers saw
    in the cross-pollination summary), P is flagged by F alone, and P has the
    same file and an overlapping line range;
  * F's own raw pass-2 output ties one structured finding to P (issue
    #4975): exactly one of its ``File:`` finding records (see
    flagger_finding_records()) carries a whole ``consensus_id:`` line with
    the id, and no other id, and that record names P's file with a line
    reference overlapping both P's range and the entry's. The id quoted
    anywhere else in the output binds nothing (the summariser only copies
    the line);
  * the match is unambiguous: no other pass-1 CONSENSUS FINDINGS entry lies in
    the same file within LINE_TOLERANCE lines of P, no other CONSENSUS
    FINDINGS entry of this ledger lies in the same file within LINE_TOLERANCE
    lines of the entry, and F reported no other finding record in the same
    file within LINE_TOLERANCE lines of either range, in that file without a
    readable line, or with an unreadable file;
  * at least MIN_REJECTERS successful reviewers other than F each cast a
    rejection vote (below) for a manifest entry whose consensus_id is P's,
    whose file is P's, whose range overlaps P's, and whose flagger is F;
  * those rejecters are a strict majority of the successful reviewers other
    than F.

Rejection votes are bound to finding IDs issued for this run (issue #4688).
After pass 1, ``--issue-ids`` gives every pass-1 CONSENSUS FINDINGS entry
with a parseable location and exactly one flagger a random ID
(``RF-<16 hex>``), writes them, with each entry's consensus_id, to a
manifest (``<reviews-dir>/rejection_ids_pass1.json`` by default), and prints
the list the cross-pollination header shows pass-2 reviewers. A vote is a
line

  REJECTED_FINDING: <ID> | <file>:<line> | flagged_by: <slug> | reason: <one sentence> | evidence: <file>:<line or start-end> | quote: <text>

and it counts only when the ID is in the manifest, the reason is not empty,
the line starts a line (at most 3 spaces, an optional ``-``/``*`` bullet)
outside any fenced code block, and its evidence verifies (issue #4976): the
cited file is the manifest entry's file, the cited range is at most
EVIDENCE_MAX_LINES long and within EVIDENCE_LINE_WINDOW lines of the entry's
range, and the quote, at least EVIDENCE_MIN_QUOTE_CHARS non-whitespace
characters (one pair of wrapping backticks may be dropped), occurs in those
lines of the reviewed commit, whitespace runs collapsed. The commit is read
with local ``git cat-file`` (--source-root / --source-commit); without it no
vote counts. A free-text reason alone never demotes a finding. The file,
line, and slug a reviewer echoes are informational: the match uses the
manifest entry the ID names.
No PR content can carry an ID issued after it was pushed, so a
REJECTED_FINDING line quoted from the PR never counts, and neither does a
line without an ID (the pre-#4688 shape) or one citing a consensus_id
instead (the #4687 shape); those are only counted. Without a manifest no
vote counts, so every finding stays blocking.

A vote is bound to the finding it names by the manifest entry's
consensus_id, never by file and line proximity: a rejection of one entry
cannot demote a distinct finding on a nearby line (issue #4687). Votes are
read from each reviewer's raw output, never from the summariser's ledger
text, so a summariser mistake cannot unblock a finding. A reviewer counts
only when ``status_review_<slug>.txt`` reads ``success`` (the rule
reviewer_count_success_statuses in review_run_reviewers.sh uses), and each
reviewer counts once per finding.

The consensus_id is ``p1-`` plus the first 12 hex digits of the SHA-256 of
the pass-1 entry's lines (see consensus_id()). ``--annotate`` writes a copy
of the pass-1 ledger with a ``consensus_id:`` line after each CONSENSUS
FINDINGS header; it is what build_cross_pollination_summary in
review_run_reviewers.sh shows pass-2 reviewers. consensus_pass1.txt itself is
never rewritten, so every mode computes the same ids, and a regenerated
ledger invalidates every old id instead of rebinding it.

Demoted entries move into a ``=== NON-BLOCKING FINDINGS ===`` block, inserted
after ``=== END CONSENSUS TASK GAPS ===``, with the rejecting reviewers named.
Per-reviewer bullets in the flagger's or a rejecter's ``FINDINGS FROM <slug>``
section move there too when their range overlaps the entry's and their text
carries the same consensus_id, so the hand-off step's bullet count and
clean-ledger check see only blocking entries. A block left without entries
gets its ``(No findings reported.)`` placeholder back. Task gaps and entries
flagged by several reviewers are never demoted, and an entry that does not
parse stays blocking.

Usage:

  review_claude_fixer_nonblocking.py --ledger FILE --reviews-dir DIR --output FILE [--ids-manifest FILE]
      [--source-root DIR --source-commit SHA]
  review_claude_fixer_nonblocking.py --issue-ids --ledger PASS1_LEDGER --ids-manifest FILE
  review_claude_fixer_nonblocking.py --annotate --ledger PASS1_LEDGER --output FILE

The first form always writes --output (an unchanged copy when nothing is
demoted) and prints ``CLAUDE_FIXER_NONBLOCKING demoted=<n>
successful_reviewers=<m>``, then ``CLAUDE_FIXER_NONBLOCKING_VOTES
manifest=<present|missing> ids=<n> votes=<n>`` (votes whose evidence
verified), then ``CLAUDE_FIXER_NONBLOCKING_EVIDENCE
source=<ok|missing|unavailable> commit=<sha|none> verified=<n>
unverified=<n>``, then one ``CLAUDE_FIXER_NONBLOCKING_UNVERIFIED id=<ID>
reviewer=<slug> reason=<no_evidence|wrong_file|span_too_long|out_of_range|
quote_too_short|source_unavailable|quote_mismatch>`` line per vote whose
evidence did not verify, then one
``CLAUDE_FIXER_NONBLOCKING_ENTRY`` line per demoted entry, one
``CLAUDE_FIXER_NONBLOCKING_KEPT file=<file:span> flagged_by=<slug>
reason=<reason>`` line per single-reviewer entry that stays blocking, and
``CLAUDE_FIXER_NONBLOCKING_LEGACY_REJECTIONS count=<n>`` when REJECTED_FINDING
lines without a run ID were ignored. A manifest that is present but invalid
is an error. Exit 0 on success; any other exit means the caller must keep
the original ledger (fail toward blocking).

The second form (called by build_cross_pollination_summary) issues fresh
IDs on every call, overwrites the manifest, and prints one ``<ID> ->
<file>:<line or start-end> | flagged_by: <slug> | consensus_id: <id>`` line
per ID on stdout and ``CLAUDE_FIXER_NONBLOCKING_IDS issued=<n>`` on stderr.
Because a rebuilt header gets new IDs, votes in cached pass-2 outputs from an
earlier run stop counting (fail toward blocking); only a same-head resume
that skips the whole reviewer phase, and so writes no new output, keeps the
persisted manifest. Any other exit means the caller must show reviewers no
rejection instructions.

The third form prints ``CLAUDE_FIXER_CONSENSUS_IDS annotated=<n>``; any
other exit means the caller shows the plain ledger.

No network access and no GitHub API calls; the reviewed commit is read with
local ``git cat-file``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
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
# The #4687 rejection shape (a consensus_id instead of a run ID). Since #4688 it
# no longer counts as a vote; it is only counted with the legacy lines.
REJECTED_FINDING_ID_RE = re.compile(
	rf"^\s*(?:[-*]\s*)?REJECTED_FINDING:\s*`?(?P<consensus_id>{CONSENSUS_ID_PATTERN})`?\s*\|\s*(?P<rest>.*)$"
)
SLUG_STRIP = " \t`'\"[]"

# Finding-ID-bound rejection votes (issue #4688). REJECTED_FINDING_RE above is
# the pre-#4688 shape; it is kept for compatibility but no longer counts.
MANIFEST_NAME = "rejection_ids_pass1.json"
MANIFEST_SCHEMA = "rejection_ids.v1"
FINDING_ID_RE = re.compile(r"^RF-[0-9a-f]{16}$")
MANIFEST_CONSENSUS_ID_RE = re.compile(rf"^{CONSENSUS_ID_PATTERN}$")
REJECTION_VOTE_RE = re.compile(r"^ {0,3}(?:[-*] +)?REJECTED_FINDING:[ \t]*`?(?P<id>RF-[0-9a-f]{16})`?[ \t]*\|(?P<rest>.*)$")
VOTE_REASON_RE = re.compile(r"(?:^|\|)[ \t]*reason:[ \t]*(?P<reason>\S.*?)[ \t]*$")
VOTE_REASON_PLACEHOLDER_RE = re.compile(r"^<[^>]*>$")
FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")

# Finding records in a reviewer's raw output (issue #4975). The flagger's
# consensus_id citation binds only as a whole line inside one of them.
RECORD_MARKUP = r"(?:\*\*|__)?"
# A record's File: or Requirement: line may sit in a bulleted or numbered list item.
RECORD_ITEM = r"(?:(?:[-*]|\d+[.)])\s+)?"
RECORD_FILE_RE = re.compile(rf"^\s*(?:#{{1,6}}\s+)?{RECORD_ITEM}{RECORD_MARKUP}File{RECORD_MARKUP}\s*:{RECORD_MARKUP}\s*(?P<value>.*?)\s*$",
	re.IGNORECASE)
RECORD_LINE_RE = re.compile(
	rf"^\s*(?:[-*]\s+)?{RECORD_MARKUP}(?:Line or code reference|Line reference|Lines?){RECORD_MARKUP}\s*:{RECORD_MARKUP}"
	r"\s*(?P<value>.*?)\s*$", re.IGNORECASE)
RECORD_PATH_RE = re.compile(r"^`?(?P<path>[^\s`|:,()]+)`?(?::L?(?P<start>\d+)(?:\s*[-–]\s*L?(?P<end>\d+))?)?")
RECORD_LINE_NUMBER_RE = re.compile(r"(?<![\w/.-])L?(?P<start>\d+)(?:\s*[-–]\s*L?(?P<end>\d+))?(?![\w/.])")
# Explicit line references inside a longer value: ``path:N[-M]`` (the path has a
# ``/`` or a file extension that starts with a letter) and ``line N`` /
# ``lines N-M`` / ``LN``. A bare number elsewhere in the value is code text, not
# a line reference, and so are a version (``3.14:40``, ``1.2.3:40``) and a URL's
# ``//host:port``.
RECORD_LINE_PATH_RE = re.compile(
	r"(?<![\w/.:-])`?[\w./-]*(?:/[\w.-]*|\.[A-Za-z][\w-]*)`?:L?(?P<start>\d+)(?:\s*[-–]\s*L?(?P<end>\d+))?(?![\w/.])")
RECORD_LINE_WORD_RE = re.compile(r"(?<![\w-])(?:[Ll]ines?\s*|L)(?P<start>\d+)(?:\s*[-–]\s*L?(?P<end>\d+))?(?![\w/.])")
RECORD_LEADING_STRIP = " \t`(,;:—–-"
RECORD_CONSENSUS_ID_RE = re.compile(
	rf"^\s*(?:[-*]\s+)?{RECORD_MARKUP}consensus_id{RECORD_MARKUP}:{RECORD_MARKUP}\s*`?(?P<consensus_id>{CONSENSUS_ID_PATTERN})`?\s*$")
RECORD_BREAK_RE = re.compile(rf"^\s*{RECORD_ITEM}(?:{RECORD_MARKUP}Requirement{RECORD_MARKUP}\s*:|REJECTED_FINDING\s*:|#)",
	re.IGNORECASE)
RECORD_HEADING_RE = re.compile(r"^[A-Z][A-Z0-9 /&()-]*[A-Z)]$")

# Source-grounded rejection evidence (issue #4976). A vote counts only when
# its evidence citation and quote verify against the reviewed commit.
EVIDENCE_LINE_WINDOW = 10
EVIDENCE_MAX_LINES = 20
EVIDENCE_MIN_QUOTE_CHARS = 10
GIT_READ_TIMEOUT_SECONDS = 30
SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
VOTE_EVIDENCE_RE = re.compile(
	r"^(?P<head>.*?)\|[ \t]*evidence:[ \t]*`?(?P<path>[^\s|`]+?)`?:(?P<start>\d+)(?:[ \t]*-[ \t]*(?P<end>\d+))?`?"
	r"[ \t]*\|[ \t]*quote:[ \t]*(?P<quote>.*?)[ \t]*$"
)


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
	"""REJECTED_FINDING lines without any id (the pre-#4687/#4688 shape); they are never votes."""
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
	"""``REJECTED_FINDING: <consensus_id> | <file>:<range> | flagged_by: <slug>`` lines (the #4687 shape; never votes)."""
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
		cid = item.get("consensus_id")
		if cid is not None and not (isinstance(cid, str) and MANIFEST_CONSENSUS_ID_RE.match(cid)):
			raise ValueError(f"rejection ID manifest entry {finding_id} has an invalid consensus_id: {cid!r}")
		entries[finding_id] = (path, (start, end), flagger)
	return entries


def _manifest_consensus_ids(payload: dict) -> dict[str, str | None]:
	"""ID -> the pass-1 consensus_id recorded for it (issue #4687); None when the entry has none.

	Call only on a payload _manifest_entries accepted. An entry without a
	consensus_id (a manifest written before #4687) binds no vote."""
	return {item["id"]: item.get("consensus_id") for item in payload["entries"]}


def _load_manifest_payload(manifest_path: Path) -> dict | None:
	"""The validated manifest payload, or None when there is no manifest. An invalid one raises ValueError."""
	if not manifest_path.is_file():
		return None
	try:
		payload = json.loads(manifest_path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as exc:
		raise ValueError(f"rejection ID manifest {manifest_path} is not JSON: {exc}") from exc
	_manifest_entries(payload)
	return payload


def load_manifest(manifest_path: Path) -> dict[str, tuple[str, tuple[int, int], str]] | None:
	"""Return the manifest's entries, or None when there is no manifest. An invalid one raises ValueError."""
	payload = _load_manifest_payload(manifest_path)
	return None if payload is None else _manifest_entries(payload)


def issue_ids(ledger_text: str, manifest_path: Path) -> list[dict]:
	"""Give every single-flagger pass-1 CONSENSUS FINDINGS entry a fresh random ID.

	Every call issues new IDs and overwrites the manifest, so no ID outlives
	the header it was shown in: an ID a reviewer wrote in an earlier run (and
	that may since have been published, e.g. in an uploaded log artifact)
	never counts in a new pass 2. The manifest records the ledger's sha256
	and, per ID, the entry's consensus_id (issue #4687), which the demoter
	binds the vote to.
	"""
	digest = hashlib.sha256(ledger_text.encode("utf-8")).hexdigest()
	segments = parse_ledger(ledger_text)
	consensus = _consensus_block(segments)
	if consensus is None:
		raise ValueError("pass-1 ledger lacks the CONSENSUS FINDINGS block")
	entries: list[dict] = []
	for entry in consensus.entries:
		location = entry_location(entry)
		flaggers = entry_flaggers(entry)
		if location is None or flaggers is None or len(flaggers) != 1:
			continue
		path, (start, end) = location
		entries.append({"id": f"RF-{secrets.token_hex(8)}", "path": path, "start": start, "end": end, "flagger": flaggers[0],
			"consensus_id": consensus_id(entry)})
	_write_manifest(manifest_path, {"schema": MANIFEST_SCHEMA, "ledger_sha256": digest, "entries": entries})
	return entries


def _fence_marker(line: str) -> str | None:
	match = FENCE_RE.match(line)
	return match.group("fence") if match else None


def _safe_source_path(path: str) -> bool:
	"""A relative repository path with no empty, ``.`` or ``..`` segment, no control characters, and no ``:``.

	Git reads everything after the commit's ``:`` in ``<commit>:<path>`` as a
	literal path, but a ``:`` is refused anyway so the evidence read never
	depends on git's revision syntax; no tracked path in this repository
	contains one."""
	if not path or path.startswith("/") or any(char in path for char in ("\0", "\n", "\r", "\\", ":")):
		return False
	return all(part not in ("", ".", "..") for part in path.split("/"))


def git_commit_available(root: Path, commit: str) -> bool:
	"""True when ``commit`` is a full hex object name of a commit present in the repository at ``root``."""
	if not SOURCE_COMMIT_RE.match(commit):
		return False
	try:
		proc = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{commit}^{{commit}}"],
			capture_output=True, timeout=GIT_READ_TIMEOUT_SECONDS, check=False)
	except (OSError, subprocess.SubprocessError):
		return False
	return proc.returncode == 0


def git_source(root: Path, commit: str):
	"""Return a reader ``path -> list of lines | None`` for the files of ``commit`` in ``root``.

	Each path is read once with ``git cat-file blob <commit>:<path>`` (local,
	no network) and cached. An invalid commit, an unsafe path, a missing file,
	or any git failure reads as None, so the evidence that cites it does not
	verify (issue #4976)."""
	cache: dict[str, list[str] | None] = {}

	def read(path: str) -> list[str] | None:
		if path in cache:
			return cache[path]
		lines = None
		if SOURCE_COMMIT_RE.match(commit) and _safe_source_path(path):
			try:
				proc = subprocess.run(["git", "-C", str(root), "cat-file", "blob", f"{commit}:{path}"],
					capture_output=True, timeout=GIT_READ_TIMEOUT_SECONDS, check=False)
			except (OSError, subprocess.SubprocessError):
				proc = None
			if proc is not None and proc.returncode == 0:
				lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
		cache[path] = lines
		return lines

	return read


def _collapse_whitespace(text: str) -> str:
	return " ".join(text.split())


def _quote_candidates(quote: str) -> list[str]:
	"""The quote with whitespace collapsed, and without one pair of wrapping backticks; short ones dropped."""
	collapsed = _collapse_whitespace(quote)
	candidates = [collapsed]
	if len(collapsed) > 2 and collapsed.startswith("`") and collapsed.endswith("`"):
		candidates.append(collapsed[1:-1].strip())
	return [candidate for candidate in candidates
		if len(candidate.replace(" ", "")) >= EVIDENCE_MIN_QUOTE_CHARS]


def _verify_evidence(entry: tuple[str, tuple[int, int], str], evidence: re.Match | None, source_reader) -> str | None:
	"""None when a vote's evidence verifies for the manifest ``entry``, else the reason it does not.

	The cited file must be the entry's file, the cited range at most
	EVIDENCE_MAX_LINES long and within EVIDENCE_LINE_WINDOW lines of the
	entry's range, and the quote (at least EVIDENCE_MIN_QUOTE_CHARS
	non-whitespace characters) must occur in those lines of the file as
	``source_reader`` reads it, whitespace runs collapsed."""
	if evidence is None:
		return "no_evidence"
	path, lines, _flagger = entry
	if _norm_path(evidence.group("path")) != path:
		return "wrong_file"
	cited = _range(evidence.group("start"), evidence.group("end"))
	if cited[1] - cited[0] + 1 > EVIDENCE_MAX_LINES:
		return "span_too_long"
	if cited[0] < 1 or max(cited[0], lines[0]) - min(cited[1], lines[1]) > EVIDENCE_LINE_WINDOW:
		return "out_of_range"
	quotes = _quote_candidates(evidence.group("quote"))
	if not quotes:
		return "quote_too_short"
	file_lines = source_reader(path) if source_reader is not None else None
	if file_lines is None:
		return "source_unavailable"
	if cited[1] > len(file_lines):
		return "out_of_range"
	cited_text = _collapse_whitespace(" ".join(file_lines[cited[0] - 1:cited[1]]))
	if not any(quote in cited_text for quote in quotes):
		return "quote_mismatch"
	return None


def reviewer_votes(output: Path, manifest: dict[str, tuple[str, tuple[int, int], str]], source_reader=None) -> set[str]:
	"""Return the manifest IDs this reviewer voted to reject.

	A vote is a REJECTION_VOTE_RE line outside any fenced code block, naming
	an ID in the manifest, with a non-empty reason that is not the template
	placeholder, followed by ``| evidence: <file>:<range> | quote: <text>``
	that verifies against ``source_reader`` (see _verify_evidence, issue #4976).
	Anything else (the pre-#4688 shape, quoted or fenced text, an unknown ID,
	a vote without verified evidence) is ignored; without a reader no vote
	counts.
	"""
	return _votes_in(output.read_text(encoding="utf-8", errors="replace"), manifest, source_reader)


def _votes_in(text: str, manifest: dict[str, tuple[str, tuple[int, int], str]], source_reader=None,
		unverified: list[tuple[str, str]] | None = None) -> set[str]:
	"""``reviewer_votes`` over an output already read into memory.

	When ``unverified`` is a list, each vote that has a reason but whose
	evidence does not verify is appended to it as ``(id, reason)``."""
	votes: set[str] = set()
	open_fence: str | None = None
	for line in text.splitlines():
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
		evidence = VOTE_EVIDENCE_RE.match(match.group("rest"))
		reason = VOTE_REASON_RE.search(evidence.group("head") if evidence else match.group("rest"))
		if not reason or VOTE_REASON_PLACEHOLDER_RE.match(reason.group("reason")):
			continue
		finding_id = match.group("id")
		failure = _verify_evidence(manifest[finding_id], evidence, source_reader)
		if failure is None:
			votes.add(finding_id)
		elif unverified is not None:
			unverified.append((finding_id, failure))
	return votes


def _record_lines(value: str) -> tuple[int, int] | None:
	"""The line range of an explicit reference: a leading number or range, else
	the first ``path:N[-M]`` or ``line N`` / ``LN`` in the value. Code text such
	as ``retries = 3`` reads no line, so the record has no readable line."""
	match = RECORD_LINE_NUMBER_RE.match(value.lstrip(RECORD_LEADING_STRIP))
	if match is None:
		found = [candidate for candidate in (RECORD_LINE_PATH_RE.search(value), RECORD_LINE_WORD_RE.search(value)) if candidate]
		match = min(found, key=lambda candidate: candidate.start()) if found else None
	return _range(match.group("start"), match.group("end")) if match else None


def flagger_finding_records(text: str) -> list[dict]:
	"""Return the ``File:`` finding records of a reviewer's raw output (issue #4975).

	A record starts at a ``File:`` line (plain, bulleted, numbered, or a
	heading) outside any fenced code block and runs to the first blank line,
	the next ``File:`` or ``Requirement:`` line, a REJECTED_FINDING line, a
	heading, or a fence. Each record is a dict with ``path`` (None when the
	``File:`` value does not start with a path: a word with no ``.`` or ``/``
	followed by more text is prose), ``lines`` (None when no explicit line
	reference (see _record_lines()) is read from the ``File:`` value, its
	first ``Line or code reference:`` / ``Line:`` / ``Lines:`` field, or the
	rest of the ``File:`` value), and ``consensus_ids``, the ids of the whole
	``consensus_id:`` lines inside it. A consensus_id anywhere else in the
	output (prose, a quote, a code block, a REJECTED_FINDING line, a line
	after the record's blank line) belongs to no record and binds nothing.
	"""
	records: list[dict] = []
	current: dict | None = None
	open_fence: str | None = None
	for line in text.splitlines():
		fence = _fence_marker(line)
		if open_fence is not None:
			if fence and fence[0] == open_fence[0] and len(fence) >= len(open_fence) and not line.strip()[len(fence):].strip():
				open_fence = None
			continue
		if fence:
			open_fence = fence
			current = None
			continue
		file_match = RECORD_FILE_RE.match(line)
		if file_match:
			value = file_match.group("value")
			path_match = RECORD_PATH_RE.match(value)
			# A leading word is a path only when it looks like one (a "." or "/")
			# or is the whole value: "File: the install example" names no file.
			if path_match and not ("." in path_match.group("path") or "/" in path_match.group("path")
					or not value[path_match.end():].strip()):
				path_match = None
			path = _norm_path(path_match.group("path")) if path_match else None
			lines = None
			if path_match and path_match.group("start"):
				lines = _range(path_match.group("start"), path_match.group("end"))
			current = {"path": path or None, "lines": lines, "consensus_ids": [], "line_field_seen": False,
				"file_rest": value[path_match.end():] if path_match else value}
			records.append(current)
			continue
		if current is None:
			continue
		if not line.strip() or RECORD_BREAK_RE.match(line) or RECORD_HEADING_RE.match(line.strip()):
			current = None
			continue
		id_match = RECORD_CONSENSUS_ID_RE.match(line)
		if id_match:
			current["consensus_ids"].append(id_match.group("consensus_id"))
			continue
		line_match = RECORD_LINE_RE.match(line)
		if line_match and not current["line_field_seen"]:
			current["line_field_seen"] = True
			if current["lines"] is None:
				current["lines"] = _record_lines(line_match.group("value"))
	for record in records:
		if record["lines"] is None and not record["line_field_seen"]:
			record["lines"] = _record_lines(record["file_rest"])
		del record["line_field_seen"], record["file_rest"]
	return records


def demote_with_diagnostics(ledger_text: str, reviews_dir: Path, manifest_path: Path | None = None, *,
		reviewers: dict[str, Path] | None = None, stats: dict | None = None, source_reader=None) -> tuple[str, list[dict], list[dict], int]:
	"""Return the filtered ledger, the demoted records, the kept single-reviewer
	records (with the reason each stays blocking), and the number of ignored
	REJECTED_FINDING lines without a run ID (the id-less and consensus_id shapes).

	manifest_path defaults to reviews_dir / MANIFEST_NAME; without a manifest
	no vote counts and nothing is demoted. ``reviewers`` is
	``successful_reviewers(reviews_dir)`` when the caller already has it; each
	reviewer output is read once. ``source_reader`` reads the reviewed commit's
	files (git_source); a vote counts only when its evidence verifies against it,
	so without a reader nothing is demoted (issue #4976). When ``stats`` is a dict
	it receives ``manifest_present``, ``manifest_ids``, ``votes`` (counted,
	evidence-verified votes over all successful reviewers) for the
	CLAUDE_FIXER_NONBLOCKING_VOTES log line, and ``unverified`` (one
	``(slug, id, reason)`` per vote whose evidence did not verify)."""
	segments = parse_ledger(ledger_text)
	blocks = [segment for segment in segments if isinstance(segment, Block)]
	consensus = _consensus_block(segments)
	task_gaps_index = next((index for index, segment in enumerate(segments)
		if isinstance(segment, Block) and segment.name == CONSENSUS_TASK_GAPS_BLOCK), None)
	if consensus is None or task_gaps_index is None:
		raise ValueError("ledger lacks the CONSENSUS FINDINGS or CONSENSUS TASK GAPS block")
	if any(block.name == NONBLOCKING_BLOCK for block in blocks):
		raise ValueError("ledger already carries a NON-BLOCKING FINDINGS block")

	payload = _load_manifest_payload(manifest_path if manifest_path is not None else reviews_dir / MANIFEST_NAME)
	manifest = _manifest_entries(payload) if payload is not None else {}
	manifest_cids = _manifest_consensus_ids(payload) if payload is not None else {}

	if reviewers is None:
		reviewers = successful_reviewers(reviews_dir)
	outputs = {slug: path.read_text(encoding="utf-8", errors="replace") for slug, path in reviewers.items()}
	votes: dict[str, set[str]] = {}
	unverified_votes: list[tuple[str, str, str]] = []
	for slug, text in outputs.items():
		failed_votes: list[tuple[str, str]] = []
		votes[slug] = _votes_in(text, manifest, source_reader, failed_votes) if manifest else set()
		unverified_votes.extend((slug, finding_id, failure) for finding_id, failure in failed_votes)
	legacy_rejections = sum(len(_legacy_rejections_in(text)) + len(_bound_rejections_in(text)) for text in outputs.values())
	per_reviewer = {block.name[len(PER_REVIEWER_PREFIX):]: block for block in blocks if block.name.startswith(PER_REVIEWER_PREFIX)}
	if stats is not None:
		stats.update(manifest_present=payload is not None, manifest_ids=len(manifest),
			votes=sum(len(cast) for cast in votes.values()), unverified=unverified_votes)

	pass1 = pass1_consensus_entries(reviews_dir)
	pass1_id_counts = Counter(record["consensus_id"] for record in pass1)
	pass1_by_id = {record["consensus_id"]: record for record in pass1 if pass1_id_counts[record["consensus_id"]] == 1}
	ledger_id_counts = Counter(cid for entry in consensus.entries for cid in entry_consensus_ids(entry))
	ledger_locations = [entry_location(entry) for entry in consensus.entries]

	records_by_flagger: dict[str, list[dict]] = {}
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
		# The flagger ties its pass-2 finding to P only through a whole
		# consensus_id line inside one of its own File: records at P's
		# location, never through the id quoted elsewhere (issue #4975).
		if flagger not in records_by_flagger:
			records_by_flagger[flagger] = flagger_finding_records(outputs[flagger])
		flagger_records = records_by_flagger[flagger]
		citing = [record for record in flagger_records if cid in record["consensus_ids"]]
		if not citing:
			keep("flagger_did_not_cite")
			continue
		source_lines = source["location"][1]
		cited = citing[0]
		if (len(citing) != 1 or set(cited["consensus_ids"]) != {cid} or cited["path"] != path or cited["lines"] is None
				or not _overlaps(cited["lines"], source_lines) or not _overlaps(cited["lines"], lines)):
			keep("flagger_citation_mismatch")
			continue
		if any(other is not source and other["location"] is not None and other["location"][0] == path
				and _near(other["location"][1], source_lines) for other in pass1):
			keep("ambiguous_nearby_pass1")
			continue
		if any(other_index != index and other is not None and other[0] == path and _near(other[1], lines)
				for other_index, other in enumerate(ledger_locations)):
			keep("ambiguous_nearby")
			continue
		if any(record is not cited and (record["path"] is None or (record["path"] == path and (record["lines"] is None
				or _near(record["lines"], source_lines) or _near(record["lines"], lines)))) for record in flagger_records):
			keep("ambiguous_flagger_nearby")
			continue
		others = [slug for slug in reviewers if slug != flagger]
		rejecters = sorted(slug for slug in others if any(
			manifest_cids.get(vote) == cid and manifest[vote][0] == path and manifest[vote][2] == flagger
			and _overlaps(source_lines, manifest[vote][1])
			for vote in votes[slug]))
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
			"REJECTED_FINDING line citing this run's finding ID, with a quote verified against the reviewed code, by a "
			"majority of the other reviewers. Kept for visibility; "
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


def demote(ledger_text: str, reviews_dir: Path, manifest_path: Path | None = None, *, source_reader=None) -> tuple[str, list[dict]]:
	"""Return the filtered ledger text and one record per demoted entry.

	manifest_path defaults to reviews_dir / MANIFEST_NAME; without a manifest
	no vote counts and nothing is demoted. ``source_reader`` reads the reviewed
	commit's files (git_source); without it no vote's evidence verifies and
	nothing is demoted (issue #4976).
	"""
	text, demoted, _kept, _legacy = demote_with_diagnostics(ledger_text, reviews_dir, manifest_path, source_reader=source_reader)
	return text, demoted


def _format_span(low: int, high: int) -> str:
	return f"{low}" if low == high else f"{low}-{high}"


def _main_issue_ids(args: argparse.Namespace) -> int:
	try:
		entries = issue_ids(args.ledger.read_text(encoding="utf-8"), args.ids_manifest)
	except (OSError, ValueError) as exc:
		print(f"CLAUDE_FIXER_NONBLOCKING_IDS error={exc}", file=sys.stderr)
		return 1
	for entry in entries:
		print(f"{entry['id']} -> {entry['path']}:{_format_span(entry['start'], entry['end'])} | flagged_by: {entry['flagger']}"
			f" | consensus_id: {entry['consensus_id']}")
	print(f"CLAUDE_FIXER_NONBLOCKING_IDS issued={len(entries)}", file=sys.stderr)
	return 0


def _main_annotate(args: argparse.Namespace) -> int:
	try:
		annotated, count = annotate(args.ledger.read_text(encoding="utf-8"))
		args.output.write_text(annotated, encoding="utf-8")
	except (OSError, ValueError) as exc:
		print(f"CLAUDE_FIXER_CONSENSUS_IDS error={exc}", file=sys.stderr)
		return 1
	print(f"CLAUDE_FIXER_CONSENSUS_IDS annotated={count}")
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
	parser.add_argument("--annotate", action="store_true",
		help="write --ledger (a pass-1 ledger) with a consensus_id line on each CONSENSUS FINDINGS entry")
	parser.add_argument("--source-root", type=Path,
		help="git checkout holding --source-commit; rejection evidence is read from it (issue #4976)")
	parser.add_argument("--source-commit",
		help="the reviewed commit (40 or 64 hex digits); without it and --source-root no vote counts")
	args = parser.parse_args(argv)
	if args.issue_ids and args.annotate:
		parser.error("--issue-ids and --annotate are separate modes")
	if args.issue_ids:
		if args.ids_manifest is None:
			parser.error("--issue-ids needs --ids-manifest")
		return _main_issue_ids(args)
	if args.output is None:
		parser.error("--output is required unless --issue-ids is given")
	if args.annotate:
		return _main_annotate(args)
	if args.reviews_dir is None:
		parser.error("--reviews-dir is required unless --annotate or --issue-ids is given")
	manifest_path = args.ids_manifest if args.ids_manifest is not None else args.reviews_dir / MANIFEST_NAME
	source_reader = None
	source_state = "missing"
	source_commit = args.source_commit or ""
	if args.source_root is not None and source_commit:
		if git_commit_available(args.source_root, source_commit):
			source_reader = git_source(args.source_root, source_commit)
			source_state = "ok"
		else:
			source_state = "unavailable"
	stats: dict = {}
	try:
		ledger_text = args.ledger.read_text(encoding="utf-8")
		if not args.reviews_dir.is_dir():
			raise ValueError(f"reviews dir {args.reviews_dir} does not exist")
		reviewers = successful_reviewers(args.reviews_dir)
		filtered, demoted, kept_records, legacy_rejections = demote_with_diagnostics(ledger_text, args.reviews_dir,
			manifest_path, reviewers=reviewers, stats=stats, source_reader=source_reader)
		args.output.write_text(filtered, encoding="utf-8")
	except (OSError, ValueError) as exc:
		print(f"CLAUDE_FIXER_NONBLOCKING error={exc}", file=sys.stderr)
		return 1
	print(f"CLAUDE_FIXER_NONBLOCKING demoted={len(demoted)} successful_reviewers={len(reviewers)}")
	print(f"CLAUDE_FIXER_NONBLOCKING_VOTES manifest={'present' if stats['manifest_present'] else 'missing'} "
		f"ids={stats['manifest_ids']} votes={stats['votes']}")
	print(f"CLAUDE_FIXER_NONBLOCKING_EVIDENCE source={source_state} "
		f"commit={source_commit if SOURCE_COMMIT_RE.match(source_commit) else 'none'} "
		f"verified={stats['votes']} unverified={len(stats['unverified'])}")
	for slug, finding_id, failure in stats["unverified"]:
		print(f"CLAUDE_FIXER_NONBLOCKING_UNVERIFIED id={finding_id} reviewer={slug} reason={failure}")
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

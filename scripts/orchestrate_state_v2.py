#!/usr/bin/env python3
"""V2 chunked state persistence helper for orchestrator state comments.

Writes the orchestrator state JSON across multiple GitHub issue comments to
work around the 65,536-byte per-comment body cap. Before this scheme,
`post_tracking_comment` silently skipped any state snapshot above the cap,
which left the persisted state pinned at an older snapshot and caused the
poll loop to keep re-doing wave advancement / issue creation each cycle
(see tracking issue #2373 for the observed symptom: six duplicate
`semble-judge-prefetch` issues).

Subcommands
-----------
pack
    Read a state JSON file, split into byte-sized chunks that comfortably
    fit under the comment-body cap, and emit each chunk to a temp file
    already wrapped in V2 framing.  Prints a JSON manifest to stdout.

extract
    Read a paginated comments JSON array (in GitHub API order, oldest
    first), walk newest-first, and locate the most recent COMPLETE V2
    chain (every part 1..N posted with the same manifest hash and the
    stitched payload sha256-matching the manifest).  Print the stitched
    state JSON to stdout.  Exit non-zero when no complete V2 chain is
    found, so the bash caller can fall back to the legacy V1 single-
    comment extractor.

Framing
-------
Each chunk is posted as a single GitHub comment whose body looks like:

    <!-- ORCHESTRATOR_STATE_V2 part=1/3 manifest=<64 hex chars> -->
    <chunk bytes — a contiguous slice of the raw state JSON>
    ORCHESTRATOR_STATE_V2 -->

The opener line is anchored to start-of-line for unambiguous extraction.
The closer is `ORCHESTRATOR_STATE_V2 -->` on its own line; the rfind for
`\nORCHESTRATOR_STATE_V2 -->` cleanly handles slices that themselves end
with a newline byte.  The manifest is sha256(full state bytes) — every
chunk in a single write carries the same manifest, so torn writes are
trivially detected (incomplete chain or hash mismatch -> chain skipped).

Backward compatibility
----------------------
This helper is ADDITIVE.  The bash caller falls back to the V1 reader if
no complete V2 chain is found, so legacy V1 state comments keep working
until the next write supersedes them.  The writer emits V2 even for
single-chunk payloads to keep the write path uniform.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# GitHub's hard cap on issue/PR comment body bytes.
GITHUB_COMMENT_BODY_CAP = 65536

# Reserve headroom for the V2 framing.  Opener line is roughly
# ~108 bytes (`<!-- ORCHESTRATOR_STATE_V2 part=NN/NN manifest=<64> -->`),
# closer line is ~26 bytes including the leading newline.  Adding a
# generous safety margin so we never edge-case-trip the API even if
# part numbers grow into 4 digits.
DEFAULT_FRAMING_HEADROOM = 256
DEFAULT_CHUNK_SIZE = GITHUB_COMMENT_BODY_CAP - DEFAULT_FRAMING_HEADROOM

V2_OPENER_RE = re.compile(
	r"^<!-- ORCHESTRATOR_STATE_V2 part=(\d+)/(\d+) manifest=([0-9a-f]{64}) -->$",
	re.MULTILINE,
)
V2_CLOSER = "ORCHESTRATOR_STATE_V2 -->"


def _frame(part: int, total: int, manifest: str, payload: bytes) -> bytes:
	opener = (
		f"<!-- ORCHESTRATOR_STATE_V2 part={part}/{total} "
		f"manifest={manifest} -->\n"
	).encode("utf-8")
	closer = ("\n" + V2_CLOSER).encode("utf-8")
	return opener + payload + closer


def cmd_pack(args: argparse.Namespace) -> int:
	state_path = Path(args.state_file)
	if not state_path.exists():
		print(f"state file not found: {state_path}", file=sys.stderr)
		return 2
	state_bytes = state_path.read_bytes()
	if not state_bytes:
		print("state file is empty", file=sys.stderr)
		return 2
	manifest = hashlib.sha256(state_bytes).hexdigest()
	chunk_size = args.chunk_size
	if chunk_size <= 0 or chunk_size > GITHUB_COMMENT_BODY_CAP:
		print(
			f"chunk_size out of range (1..{GITHUB_COMMENT_BODY_CAP}): {chunk_size}",
			file=sys.stderr,
		)
		return 2
	total = max(1, (len(state_bytes) + chunk_size - 1) // chunk_size)
	out_dir = Path(args.out_dir)
	out_dir.mkdir(parents=True, exist_ok=True)
	files: list[str] = []
	for i in range(1, total + 1):
		slice_bytes = state_bytes[(i - 1) * chunk_size : i * chunk_size]
		framed = _frame(i, total, manifest, slice_bytes)
		if len(framed) > GITHUB_COMMENT_BODY_CAP:
			# Should never happen with DEFAULT_CHUNK_SIZE, but guard just
			# in case a caller passes a custom --chunk-size that's too
			# close to the cap.
			print(
				f"framed chunk {i}/{total} is {len(framed)} bytes "
				f"(>{GITHUB_COMMENT_BODY_CAP}); reduce --chunk-size",
				file=sys.stderr,
			)
			return 3
		f = out_dir / f"chunk-{i:04d}.txt"
		f.write_bytes(framed)
		files.append(str(f))
	print(json.dumps({
		"manifest": manifest,
		"total": total,
		"files": files,
		"raw_bytes": len(state_bytes),
		"chunk_size": chunk_size,
	}))
	return 0


def _try_parse_v2_chunk(body: str) -> tuple[int, int, str, str] | None:
	"""Return (part, total, manifest, chunk_content) or None if not a V2 frame."""
	m = V2_OPENER_RE.search(body)
	if not m:
		return None
	part = int(m.group(1))
	total = int(m.group(2))
	manifest = m.group(3)
	if part < 1 or total < 1 or part > total:
		return None
	# Body slice after the opener line.  The opener was matched as a
	# full line so m.end() lands just before the trailing newline that
	# we wrote between opener and chunk.
	tail = body[m.end():]
	if tail.startswith("\n"):
		tail = tail[1:]
	# The closer is `\nORCHESTRATOR_STATE_V2 -->` immediately after the
	# chunk bytes.  rfind locates the last occurrence so a payload that
	# itself ends with `\n` (e.g. jq pretty-printed JSON with trailing
	# newline) round-trips correctly.
	closer_marker = "\n" + V2_CLOSER
	end = tail.rfind(closer_marker)
	if end >= 0:
		chunk = tail[:end]
	else:
		# Edge case: empty payload (chunk == "").  The framing then
		# collapses to "<opener>\n\nCLOSER" so `tail` after stripping
		# the leading newline is "ORCHESTRATOR_STATE_V2 -->...".
		if tail.startswith(V2_CLOSER):
			chunk = ""
		else:
			return None
	return part, total, manifest, chunk


def cmd_extract(args: argparse.Namespace) -> int:
	comments_path = Path(args.comments_json)
	if not comments_path.exists():
		print(f"comments json not found: {comments_path}", file=sys.stderr)
		return 2
	try:
		comments = json.loads(comments_path.read_text())
	except json.JSONDecodeError as e:
		print(f"comments json is not valid JSON: {e}", file=sys.stderr)
		return 2
	if not isinstance(comments, list):
		print("comments json is not a JSON array", file=sys.stderr)
		return 2
	# GitHub returns comments oldest-first.  Reverse to walk newest-first
	# so the first complete chain we encounter is the most recent write.
	parts_by_manifest: dict[str, dict[int, str]] = {}
	totals_by_manifest: dict[str, int] = {}
	for c in reversed(comments):
		body = (c or {}).get("body") or ""
		if "ORCHESTRATOR_STATE_V2" not in body:
			continue
		parsed = _try_parse_v2_chunk(body)
		if parsed is None:
			continue
		part, total, manifest, chunk = parsed
		# Stash the part.  Newest occurrence wins (setdefault semantics)
		# so an older identical-manifest re-post doesn't override the
		# fresher copy in case of an interleaved retry.
		slot = parts_by_manifest.setdefault(manifest, {})
		slot.setdefault(part, chunk)
		# Record the declared total for this manifest.  All chunks of a
		# single write carry the same total; mismatches indicate a bug
		# or hash collision and disqualify the chain.
		prev_total = totals_by_manifest.get(manifest)
		if prev_total is None:
			totals_by_manifest[manifest] = total
		elif prev_total != total:
			# Disqualify this manifest entirely.
			parts_by_manifest.pop(manifest, None)
			totals_by_manifest[manifest] = -1
			continue
		# Check completeness: every part 1..N present?
		if len(slot) == total and all(p in slot for p in range(1, total + 1)):
			stitched = "".join(slot[p] for p in range(1, total + 1))
			digest = hashlib.sha256(stitched.encode("utf-8")).hexdigest()
			if digest == manifest:
				sys.stdout.write(stitched)
				return 0
			# Hash mismatch — corrupted or truncated chunk(s).  Drop
			# this manifest and keep walking older comments for an
			# earlier intact chain.
			parts_by_manifest.pop(manifest, None)
			totals_by_manifest[manifest] = -1
	# No complete chain.  Caller falls back to V1 extraction.
	return 1


def main() -> int:
	p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
	sub = p.add_subparsers(dest="cmd", required=True)
	p_pack = sub.add_parser(
		"pack",
		help="Split a state JSON file into V2-framed chunk files",
	)
	p_pack.add_argument("--state-file", required=True)
	p_pack.add_argument("--out-dir", required=True)
	p_pack.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
	p_pack.set_defaults(func=cmd_pack)
	p_extract = sub.add_parser(
		"extract",
		help="Find the latest complete V2 chain in a paginated comments JSON array",
	)
	p_extract.add_argument("--comments-json", required=True)
	p_extract.set_defaults(func=cmd_extract)
	args = p.parse_args()
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())

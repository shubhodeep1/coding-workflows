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
    <chunk bytes — a slice of base64(full state JSON)>
    ORCHESTRATOR_STATE_V2 -->

The full state JSON is base64-encoded BEFORE chunking, then sliced at
arbitrary byte offsets.  base64 keeps every chunk in a 7-bit ASCII
alphabet (A-Z, a-z, 0-9, +, /, =) so:
  - splitting at any byte offset is safe — base64 has no multi-byte
    characters, so a UTF-8 split bug is impossible (the underlying JSON
    may legitimately contain non-ASCII text in user-authored fields like
    issue titles or project_body_snapshot, which in raw bytes would be
    at risk of being split mid-codepoint by a naive offset slicer);
  - the V2 closer marker `\nORCHESTRATOR_STATE_V2 -->` cannot appear
    inside a chunk because spaces and `-->` are outside the base64
    alphabet, so the rfind reader cannot be confused by a chunk that
    happens to embed the marker.

The opener line is anchored to start-of-line for unambiguous extraction.
The closer is `ORCHESTRATOR_STATE_V2 -->` on its own line.  The manifest
is sha256(full state bytes BEFORE base64) — every chunk in a single
write carries the same manifest, so torn writes are trivially detected
(incomplete chain or hash mismatch -> chain skipped).

Backward compatibility
----------------------
This helper is ADDITIVE.  The bash caller falls back to the V1 reader if
no complete V2 chain is found, so legacy V1 state comments keep working
until the next write supersedes them.  The writer emits V2 even for
single-chunk payloads to keep the write path uniform.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

# GitHub's hard cap on issue/PR comment body bytes.
GITHUB_COMMENT_BODY_CAP = 65536

# Reserve headroom for the V2 framing.  Opener line is roughly
# ~108 bytes (`<!-- ORCHESTRATOR_STATE_V2 part=NN/NN manifest=<64> -->`),
# closer line is ~26 bytes including the leading newline.  Adding a
# generous safety margin so we never edge-case-trip the API even if
# part numbers grow into 4 digits.
DEFAULT_FRAMING_HEADROOM = 256
DEFAULT_CHUNK_SIZE = GITHUB_COMMENT_BODY_CAP - DEFAULT_FRAMING_HEADROOM

# Upper bound on the number of chunks the extractor will track for a
# single manifest.  A healthy state snapshot is ~10s of KiB and packs
# into <10 chunks; even a worst-case ~1 MiB snapshot would be ~16
# chunks.  Cap well above that so legitimate writes are never rejected
# while a corrupted `part=1/total=999999` comment cannot force the
# extractor to allocate dict slots up to its declared total before
# realising the chain is incomplete.
MAX_CHUNKS_PER_MANIFEST = 1024

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
	# Base64-encode BEFORE chunking.  This is essential for correctness:
	# the state JSON includes user-authored text (issue titles, bodies,
	# project_body_snapshot) which routinely contains non-ASCII UTF-8
	# (em-dashes, smart quotes, emoji).  A naive byte-offset slice can
	# split a multi-byte UTF-8 sequence, producing invalid UTF-8 in the
	# posted comment body — jq -Rs / GitHub will then either reject or
	# normalise the bytes, breaking the sha256 manifest check on extract.
	# base64 is single-byte ASCII so any byte boundary is a safe cut.
	encoded = base64.b64encode(state_bytes)
	chunk_size = args.chunk_size
	if chunk_size <= 0 or chunk_size > GITHUB_COMMENT_BODY_CAP:
		print(
			f"chunk_size out of range (1..{GITHUB_COMMENT_BODY_CAP}): {chunk_size}",
			file=sys.stderr,
		)
		return 2
	total = max(1, (len(encoded) + chunk_size - 1) // chunk_size)
	out_dir = Path(args.out_dir)
	out_dir.mkdir(parents=True, exist_ok=True)
	files: list[str] = []
	for i in range(1, total + 1):
		slice_bytes = encoded[(i - 1) * chunk_size : i * chunk_size]
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
		"encoded_bytes": len(encoded),
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
	# Bound `total` so a corrupted `part=1/total=999999` cannot force the
	# extractor to track an unreasonable number of chunks before realising
	# the chain is incomplete.  See MAX_CHUNKS_PER_MANIFEST docstring.
	if total > MAX_CHUNKS_PER_MANIFEST:
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
		# GitHub API payloads are always UTF-8.  Pin the decoder explicitly
		# so a runner with a non-UTF-8 default locale (e.g. C / POSIX)
		# cannot raise UnicodeDecodeError on snapshots that contain non-
		# ASCII text in titles / bodies and force a fallback to stale
		# V1 extraction.
		comments = json.loads(comments_path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as e:
		print(f"comments json is not valid JSON: {e}", file=sys.stderr)
		return 2
	except UnicodeDecodeError as e:
		print(f"comments json is not valid UTF-8: {e}", file=sys.stderr)
		return 2
	if not isinstance(comments, list):
		print("comments json is not a JSON array", file=sys.stderr)
		return 2
	# GitHub returns comments oldest-first.  Reverse to walk newest-first
	# so the first complete chain we encounter is the most recent write.
	# Key by (manifest, total), not manifest alone: the same raw state bytes
	# can legitimately be repacked into a different number of chunks when a
	# later release changes chunk_size / framing headroom.  In that case we
	# still want to accept an older COMPLETE chain if a newer differently-
	# chunked chain is incomplete.
	#
	# A second edge case: two writes can share the same (manifest, total)
	# while using different chunk boundaries. Because the writer posts parts
	# in ascending order, a newest-first walk sees each complete write as
	# total, total-1, ..., 1. Track only contiguous descending chains that
	# start at part=total so a newer partial write cannot blend with an older
	# complete chain that happened to use different chunk slicing.
	active_chain_by_key: dict[tuple[str, int], dict[str, Any]] = {}
	for c in reversed(comments):
		body = (c or {}).get("body") or ""
		if "ORCHESTRATOR_STATE_V2" not in body:
			continue
		parsed = _try_parse_v2_chunk(body)
		if parsed is None:
			continue
		part, total, manifest, chunk = parsed
		chain_key = (manifest, total)
		candidate = active_chain_by_key.get(chain_key)
		if part == total:
			candidate = {
				"next_part": total - 1,
				"parts": {part: chunk},
			}
			active_chain_by_key[chain_key] = candidate
		elif candidate is None or part != candidate.get("next_part"):
			continue
		else:
			parts = candidate["parts"]
			parts[part] = chunk
			candidate["next_part"] = part - 1

		parts = candidate["parts"]
		if len(parts) == total and candidate.get("next_part") == 0:
			stitched_b64 = "".join(parts[p] for p in range(1, total + 1))
			# Strip any whitespace the comment renderer / round-trip
			# might have introduced; standard base64 has no whitespace.
			compact_b64 = re.sub(r"\s+", "", stitched_b64)
			try:
				decoded = base64.b64decode(compact_b64, validate=True)
			except (binascii.Error, ValueError):
				# Corrupted or non-base64 payload.  Drop this manifest
				# and keep walking older comments for an earlier
				# intact chain.
				active_chain_by_key.pop(chain_key, None)
				continue
			digest = hashlib.sha256(decoded).hexdigest()
			if digest == manifest:
				# Write raw bytes through the buffer so non-UTF-8
				# state bytes round-trip unchanged.  In practice the
				# orchestrator state is JSON (UTF-8) but we never
				# assume that.
				sys.stdout.buffer.write(decoded)
				return 0
			# Hash mismatch — corrupted or truncated chunk(s).  Drop
			# this manifest and keep walking older comments for an
			# earlier intact chain.
			active_chain_by_key.pop(chain_key, None)
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

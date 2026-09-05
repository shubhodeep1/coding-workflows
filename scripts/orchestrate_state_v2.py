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
sign
    Write a canonical state copy with a context-bound HMAC authentication
    envelope. The key is read only from ``GH_TOKEN``.

verify
    Verify the authentication envelope against the expected repository,
    tracking issue, integration branch, and producer identity.

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
import hmac
import json
import os
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

STATE_AUTH_SCHEMA_VERSION = "orchestrator_state_auth.v1"
STATE_AUTH_ALGORITHM = "hmac-sha256"
STATE_AUTH_DOMAIN = b"coding-workflows/orchestrator-state/v1"
STATE_AUTH_V2_SCHEMA_VERSION = "orchestrator_state_auth.v2"
STATE_AUTH_V2_DOMAIN = b"coding-workflows/orchestrator-state/v2"
STATE_AUTH_KEYRING_SCHEMA_VERSION = "orchestrator_state_auth_keyring.v1"
STATE_AUTH_MAX_KEYS = 8
STATE_AUTH_MIN_KEY_BYTES = 32
STATE_AUTH_MAX_KEY_BYTES = 64
STATE_AUTH_MAX_KEYRING_BYTES = 8192
STATE_AUTH_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATE_AUTH_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
STATE_AUTH_MAX_GENERATION = 9_223_372_036_854_775_807

V2_OPENER_RE = re.compile(
	r"^<!-- ORCHESTRATOR_STATE_V2 part=(\d+)/(\d+) manifest=([0-9a-f]{64}) -->$",
	re.MULTILINE,
)
V2_CLOSER = "ORCHESTRATOR_STATE_V2 -->"


def _load_state_document(state_path: Path) -> tuple[dict[str, Any] | None, str | None]:
	try:
		state_document = json.loads(state_path.read_text(encoding="utf-8"))
	except FileNotFoundError:
		return None, f"state file not found: {state_path}"
	except UnicodeDecodeError:
		return None, "state file is not valid UTF-8"
	except json.JSONDecodeError:
		return None, "state file is not valid JSON"
	except OSError:
		return None, "state file is unreadable"
	if not isinstance(state_document, dict):
		return None, "state file is not a JSON object"
	return state_document, None


def _validated_auth_context(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
	repository = args.repository.strip()
	producer_login = args.producer_login.strip()
	tracking_issue = args.tracking_issue
	producer_id = args.producer_id
	integration_branch = args.integration_branch.strip()
	repository_segments = repository.split("/")
	if (
		len(repository) > 256
		or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
		or any(segment in (".", "..") for segment in repository_segments)
	):
		return None, "repository must be an owner/repo slug"
	if tracking_issue < 1:
		return None, "tracking issue must be a positive integer"
	if (
		STATE_AUTH_BRANCH_RE.fullmatch(integration_branch) is None
		or integration_branch != f"orchestrator/project-{tracking_issue}"
	):
		return None, "integration branch does not match the tracking issue"
	if producer_id < 1:
		return None, "producer id must be a positive integer"
	if not producer_login or len(producer_login) > 100:
		return None, "producer login is invalid"
	return {
		"schema_version": STATE_AUTH_SCHEMA_VERSION,
		"algorithm": STATE_AUTH_ALGORITHM,
		"producer_id": producer_id,
		"producer_login": producer_login,
		"repository": repository,
		"tracking_issue": tracking_issue,
		"integration_branch": integration_branch,
	}, None


def _state_auth_key() -> tuple[bytes | None, str | None]:
	raw_key = os.environ.get("GH_TOKEN", "")
	if not raw_key:
		return None, "GH_TOKEN is unavailable"
	return raw_key.encode("utf-8"), None


def _validated_v2_auth_context(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
	legacy_context, context_error = _validated_auth_context(args)
	if context_error is not None:
		return None, context_error
	assert legacy_context is not None
	return {
		"schema_version": STATE_AUTH_V2_SCHEMA_VERSION,
		"algorithm": STATE_AUTH_ALGORITHM,
		"producer_id": legacy_context["producer_id"],
		"repository": legacy_context["repository"],
		"tracking_issue": legacy_context["tracking_issue"],
		"integration_branch": legacy_context["integration_branch"],
	}, None


def _state_auth_keyring() -> tuple[str | None, dict[str, bytes] | None, str | None]:
	raw_keyring = os.environ.get("ORCHESTRATOR_STATE_AUTH_KEYRING", "")
	if not raw_keyring:
		return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING is unavailable"
	if len(raw_keyring.encode("utf-8")) > STATE_AUTH_MAX_KEYRING_BYTES:
		return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING is too large"
	try:
		keyring_document = json.loads(raw_keyring)
	except json.JSONDecodeError:
		return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING is not valid JSON"
	if not isinstance(keyring_document, dict):
		return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING must be a JSON object"
	if keyring_document.get("schema_version") != STATE_AUTH_KEYRING_SCHEMA_VERSION:
		return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING has an unsupported schema"
	active_key_id = keyring_document.get("active_key_id")
	key_entries = keyring_document.get("keys")
	if not isinstance(active_key_id, str) or STATE_AUTH_KEY_ID_RE.fullmatch(active_key_id) is None:
		return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING active_key_id is invalid"
	if not isinstance(key_entries, list) or not 1 <= len(key_entries) <= STATE_AUTH_MAX_KEYS:
		return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING keys count is invalid"
	decoded_keys: dict[str, bytes] = {}
	for key_entry in key_entries:
		if not isinstance(key_entry, dict) or set(key_entry) != {"key_id", "key_base64"}:
			return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING contains an invalid key entry"
		key_id = key_entry.get("key_id")
		encoded_key = key_entry.get("key_base64")
		if not isinstance(key_id, str) or STATE_AUTH_KEY_ID_RE.fullmatch(key_id) is None:
			return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING contains an invalid key id"
		if key_id in decoded_keys:
			return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING contains duplicate key ids"
		if not isinstance(encoded_key, str) or not encoded_key:
			return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING contains an invalid encoded key"
		try:
			decoded_key = base64.b64decode(encoded_key, validate=True)
		except (binascii.Error, ValueError):
			return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING contains invalid base64"
		if not STATE_AUTH_MIN_KEY_BYTES <= len(decoded_key) <= STATE_AUTH_MAX_KEY_BYTES:
			return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING contains a key with invalid length"
		decoded_keys[key_id] = decoded_key
	if active_key_id not in decoded_keys:
		return None, None, "ORCHESTRATOR_STATE_AUTH_KEYRING active key is missing"
	return active_key_id, decoded_keys, None


def _canonical_json_bytes(value: Any) -> bytes:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
	).encode("utf-8")


def _state_auth_generation(state_document: dict[str, Any]) -> int | None:
	state_auth = state_document.get("state_auth")
	if not isinstance(state_auth, dict) or "generation" not in state_auth:
		return 0
	generation = state_auth.get("generation")
	if (
		not isinstance(generation, int)
		or isinstance(generation, bool)
		or generation < 0
		or generation > STATE_AUTH_MAX_GENERATION
	):
		return None
	return generation


def _signature_for_state(
	state_document: dict[str, Any],
	auth_context: dict[str, Any],
	auth_key: bytes,
	domain: bytes = STATE_AUTH_DOMAIN,
) -> str:
	unsigned_state = dict(state_document)
	unsigned_state.pop("state_auth", None)
	message = b"\n".join((
		domain,
		_canonical_json_bytes(auth_context),
		_canonical_json_bytes(unsigned_state),
	))
	return hmac.new(auth_key, message, hashlib.sha256).hexdigest()


def cmd_sign(args: argparse.Namespace) -> int:
	state_document, state_error = _load_state_document(Path(args.state_file))
	if state_error is not None:
		print(f"state signing failed: {state_error}", file=sys.stderr)
		return 2
	auth_context, context_error = _validated_v2_auth_context(args)
	if context_error is not None:
		print(f"state signing failed: {context_error}", file=sys.stderr)
		return 2
	active_key_id, auth_keys, key_error = _state_auth_keyring()
	if key_error is not None:
		print(f"state signing failed: {key_error}", file=sys.stderr)
		return 2
	assert state_document is not None
	assert auth_context is not None
	assert active_key_id is not None
	assert auth_keys is not None
	if state_document.get("schema_version") != "orchestrate_state.v1":
		print("state signing failed: unsupported state schema", file=sys.stderr)
		return 2
	if state_document.get("integration_branch", "") not in ("", auth_context["integration_branch"]):
		print("state signing failed: state integration branch does not match the authentication context", file=sys.stderr)
		return 2
	previous_generation = _state_auth_generation(state_document)
	if previous_generation is None or previous_generation >= STATE_AUTH_MAX_GENERATION:
		print("state signing failed: state authentication generation is invalid or exhausted", file=sys.stderr)
		return 2
	signed_auth_context = {
		**auth_context,
		"key_id": active_key_id,
		"generation": previous_generation + 1,
	}
	signed_state = dict(state_document)
	signed_state["state_auth"] = {
		**signed_auth_context,
		"signature": _signature_for_state(
			state_document,
			signed_auth_context,
			auth_keys[active_key_id],
			STATE_AUTH_V2_DOMAIN,
		),
	}
	out_path = Path(args.out_file)
	try:
		out_path.write_bytes(_canonical_json_bytes(signed_state) + b"\n")
		os.chmod(out_path, 0o600)
	except OSError:
		print("state signing failed: output file is not writable", file=sys.stderr)
		return 2
	return 0


def cmd_verify(args: argparse.Namespace) -> int:
	state_document, state_error = _load_state_document(Path(args.state_file))
	if state_error is not None:
		print(f"state verification failed: {state_error}", file=sys.stderr)
		return 2
	assert state_document is not None
	if state_document.get("schema_version") != "orchestrate_state.v1":
		return 1
	state_auth = state_document.get("state_auth")
	if not isinstance(state_auth, dict):
		return 1
	auth_schema_version = state_auth.get("schema_version")
	if auth_schema_version == STATE_AUTH_V2_SCHEMA_VERSION:
		if set(state_auth) != {
			"schema_version",
			"algorithm",
			"key_id",
			"producer_id",
			"repository",
			"tracking_issue",
			"integration_branch",
			"generation",
			"signature",
		}:
			return 1
		auth_context, context_error = _validated_v2_auth_context(args)
		if context_error is not None:
			print(f"state verification failed: {context_error}", file=sys.stderr)
			return 2
		_active_key_id, auth_keys, key_error = _state_auth_keyring()
		if key_error is not None:
			print(f"state verification failed: {key_error}", file=sys.stderr)
			return 2
		assert auth_keys is not None
		key_id = state_auth.get("key_id")
		if not isinstance(key_id, str) or key_id not in auth_keys:
			return 1
		auth_key = auth_keys[key_id]
		signature_domain = STATE_AUTH_V2_DOMAIN
	elif auth_schema_version == STATE_AUTH_SCHEMA_VERSION:
		auth_context, context_error = _validated_auth_context(args)
		if context_error is not None:
			print(f"state verification failed: {context_error}", file=sys.stderr)
			return 2
		auth_key, key_error = _state_auth_key()
		if key_error is not None:
			print(f"state verification failed: {key_error}", file=sys.stderr)
			return 2
		signature_domain = STATE_AUTH_DOMAIN
	else:
		return 1
	assert auth_context is not None
	assert auth_key is not None
	if state_document.get("integration_branch", "") not in ("", auth_context["integration_branch"]):
		return 1
	generation = _state_auth_generation(state_document)
	if generation is None:
		return 1
	signed_auth_context = dict(auth_context)
	if auth_schema_version == STATE_AUTH_V2_SCHEMA_VERSION:
		signed_auth_context["key_id"] = state_auth["key_id"]
	if "generation" in state_auth:
		signed_auth_context["generation"] = generation
	signature = state_auth.get("signature")
	if not isinstance(signature, str) or re.fullmatch(r"[0-9a-f]{64}", signature) is None:
		return 1
	if any(state_auth.get(field) != expected for field, expected in signed_auth_context.items()):
		return 1
	expected_signature = _signature_for_state(state_document, signed_auth_context, auth_key, signature_domain)
	return 0 if hmac.compare_digest(signature, expected_signature) else 1


def cmd_validate_keyring(_args: argparse.Namespace) -> int:
	_active_key_id, _auth_keys, key_error = _state_auth_keyring()
	if key_error is not None:
		print(f"state keyring validation failed: {key_error}", file=sys.stderr)
		return 2
	return 0


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
	selected_state_candidate: tuple[int, bytes] | None = None
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
				if args.prefer_highest_auth_generation:
					try:
						state_document = json.loads(decoded)
					except (UnicodeDecodeError, json.JSONDecodeError):
						state_document = {}
					generation = (
						_state_auth_generation(state_document)
						if isinstance(state_document, dict)
						else None
					)
					candidate_generation = generation if generation is not None else 0
					if (
						selected_state_candidate is None
						or candidate_generation > selected_state_candidate[0]
					):
						selected_state_candidate = (candidate_generation, decoded)
					active_chain_by_key.pop(chain_key, None)
					continue
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
	if selected_state_candidate is not None:
		# Candidates are visited newest-first and equal generations do not
		# replace the selection, preserving legacy newest-write-wins behavior.
		sys.stdout.buffer.write(selected_state_candidate[1])
		return 0
	# No complete chain. Caller falls back to V1 extraction.
	return 1


def main() -> int:
	p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
	sub = p.add_subparsers(dest="cmd", required=True)
	for command_name, command_help, command_func in (
		("sign", "Write a context-bound authenticated state copy", cmd_sign),
		("verify", "Verify a context-bound authenticated state copy", cmd_verify),
	):
		command_parser = sub.add_parser(command_name, help=command_help)
		command_parser.add_argument("--state-file", required=True)
		command_parser.add_argument("--repository", required=True)
		command_parser.add_argument("--tracking-issue", required=True, type=int)
		command_parser.add_argument("--integration-branch", required=True)
		command_parser.add_argument("--producer-id", required=True, type=int)
		command_parser.add_argument("--producer-login", required=True)
		if command_name == "sign":
			command_parser.add_argument("--out-file", required=True)
		command_parser.set_defaults(func=command_func)
	p_validate_keyring = sub.add_parser(
		"validate-keyring",
		help="Validate the dedicated state-authentication keyring",
	)
	p_validate_keyring.set_defaults(func=cmd_validate_keyring)
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
	p_extract.add_argument("--prefer-highest-auth-generation", action="store_true")
	p_extract.set_defaults(func=cmd_extract)
	args = p.parse_args()
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())

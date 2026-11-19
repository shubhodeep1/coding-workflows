#!/usr/bin/env python3
"""zlib-compressed ORCHESTRATOR_STATE_V2 chains (ORCH_STATE_COMPRESS).

Tracking issue #8291 has a 472 KB compact state, 442 KB of it the regex
lists in `merged_issue_fingerprints`, so every state write posted 10-12
comment parts. `pack` can compress the payload before base64; the framing
and the manifest (sha256 of the raw state) stay the same, and `extract` /
`prune-plan` read both forms. Refs #8291.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import random
import string
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
STATE_V2 = SCRIPTS / "orchestrate_state_v2.py"
BOT = "pipeline-bot"

sys.path.insert(0, str(SCRIPTS))
import comment_signature as cs  # noqa: E402


def _module():
	spec = importlib.util.spec_from_file_location("_state_v2_compression", STATE_V2)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _env(**extra: str) -> dict:
	env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
	env.pop("ORCH_STATE_COMPRESS", None)
	env.update(extra)
	return env


def _fingerprint_state(files: int = 112, patterns_per_file: int = 18, issues: int = 4) -> dict:
	"""A state shaped like #8291: many sub-issues, each with per-file regex lists."""
	rng = random.Random(8291)
	fingerprints = {}
	for issue in range(issues):
		fingerprints[str(9000 + issue)] = {
			f"scripts/module_{n}.py": [
				r"def\s+" + "".join(rng.choices(string.ascii_lowercase + "_", k=14)) + r"\s*\(" for _ in range(patterns_per_file)
			]
			for n in range(files)
		}
	return {"status": "in_progress", "tracking_issue": 8291, "project_title": "Stuck states — remainder ✓", "merged_issue_fingerprints": fingerprints}


def _pack(tmp_path: Path, state: dict | bytes, name: str, *flags: str, **env: str) -> tuple[dict, list[str]]:
	state_file = tmp_path / f"{name}.json"
	state_file.write_bytes(state if isinstance(state, bytes) else json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8"))
	result = subprocess.run(
		[sys.executable, str(STATE_V2), "pack", "--state-file", str(state_file), "--out-dir", str(tmp_path / name), *flags],
		capture_output=True, text=True, check=False, env=_env(**env),
	)
	assert result.returncode == 0, result.stderr
	manifest = json.loads(result.stdout)
	return manifest, [Path(f).read_text(encoding="utf-8") for f in manifest["files"]]


def _comments(*chains: list[str], start_id: int = 100) -> list[dict]:
	comments = []
	for chain in chains:
		for body in chain:
			comments.append({"id": start_id + len(comments), "user": {"login": BOT}, "body": body})
	return comments


def _extract(tmp_path: Path, comments: list[dict], *flags: str) -> subprocess.CompletedProcess:
	path = tmp_path / "comments.json"
	path.write_text(json.dumps(comments), encoding="utf-8")
	return subprocess.run(
		[sys.executable, str(STATE_V2), "extract", "--comments-json", str(path), *flags],
		capture_output=True, check=False, env=_env(),
	)


def _prune_plan(tmp_path: Path, comments: list[dict], *flags: str) -> dict:
	path = tmp_path / "comments.json"
	path.write_text(json.dumps(comments), encoding="utf-8")
	result = subprocess.run(
		[sys.executable, str(STATE_V2), "prune-plan", "--comments-json", str(path), "--login", BOT, *flags],
		capture_output=True, text=True, check=False, env=_env(),
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def _forged_chain(payload: bytes, raw_for_manifest: bytes) -> list[str]:
	"""A single-part chain whose base64 payload is `payload` (manifest of `raw_for_manifest`)."""
	manifest = hashlib.sha256(raw_for_manifest).hexdigest()
	encoded = base64.b64encode(payload).decode("ascii")
	return [f"<!-- ORCHESTRATOR_STATE_V2 part=1/1 manifest={manifest} -->\n{encoded}\nORCHESTRATOR_STATE_V2 -->"]


# --- pack / extract round trip ------------------------------------------------


@pytest.mark.parametrize("flags,env", [((), {}), (("--compress",), {}), ((), {"ORCH_STATE_COMPRESS": "true"}), ((), {"ORCH_STATE_COMPRESS": " On "})])
def test_round_trip_and_manifest_is_sha256_of_raw_state(tmp_path: Path, flags: tuple, env: dict) -> None:
	state = _fingerprint_state(files=20)
	manifest, chain = _pack(tmp_path, state, "s", *flags, **env)
	raw = (tmp_path / "s.json").read_bytes()
	assert manifest["manifest"] == hashlib.sha256(raw).hexdigest()
	assert manifest["compressed"] is bool(flags or env)
	for body in chain:
		assert f"manifest={manifest['manifest']} -->\n" in body
	result = _extract(tmp_path, _comments(chain))
	assert result.returncode == 0, result.stderr
	assert result.stdout == raw


@pytest.mark.parametrize("value", ["false", "0", "", "maybe"])
def test_default_off_chains_are_byte_identical_to_uncompressed(tmp_path: Path, value: str) -> None:
	state = _fingerprint_state(files=20)
	_, plain = _pack(tmp_path, state, "plain", "--no-compress")
	_, default = _pack(tmp_path, state, "default", ORCH_STATE_COMPRESS=value)
	assert default == plain
	raw = (tmp_path / "plain.json").read_bytes()
	encoded = "".join(body.split("\n", 1)[1].rsplit("\nORCHESTRATOR_STATE_V2 -->", 1)[0] for body in plain)
	assert base64.b64decode(encoded) == raw


def test_no_compress_flag_overrides_the_variable(tmp_path: Path) -> None:
	manifest, _ = _pack(tmp_path, {"status": "in_progress"}, "s", "--no-compress", ORCH_STATE_COMPRESS="true")
	assert manifest["compressed"] is False


def test_fingerprint_heavy_state_needs_far_fewer_parts(tmp_path: Path) -> None:
	state = _fingerprint_state()
	plain, _ = _pack(tmp_path, state, "plain")
	compressed, chain = _pack(tmp_path, state, "compressed", "--compress")
	assert plain["total"] >= 5, plain
	assert compressed["total"] <= 2, compressed
	assert compressed["total"] * 3 <= plain["total"]
	assert _extract(tmp_path, _comments(chain)).stdout == (tmp_path / "compressed.json").read_bytes()


def test_full_compressed_part_still_fits_once_signed(tmp_path: Path) -> None:
	# A payload that does not compress fills every part to DEFAULT_CHUNK_SIZE.
	noise = json.dumps({"status": "in_progress", "blob": base64.b64encode(random.Random(1).randbytes(200_000)).decode()}).encode()
	manifest, chain = _pack(tmp_path, noise, "noise", "--compress")
	assert manifest["total"] >= 2
	repo = ("o" * 39) + "/" + ("r" * 100)
	for body in chain:
		signed = cs.sign_body("s" * 64, repo, 9_999_999_999, "l" * 39, "User", body, 99_999_999_999)
		assert len(signed) <= cs.MAX_BODY_CHARS, len(signed) - cs.MAX_BODY_CHARS


# --- mixed threads and rejection -----------------------------------------------


def test_mixed_thread_extracts_the_newest_chain_in_either_order(tmp_path: Path) -> None:
	old_state, new_state = {"status": "in_progress", "wave": 1}, {"status": "in_progress", "wave": 2}
	_, old_plain = _pack(tmp_path, old_state, "old")
	_, new_compressed = _pack(tmp_path, new_state, "new", "--compress")
	assert json.loads(_extract(tmp_path, _comments(old_plain, new_compressed)).stdout) == new_state
	_, old_compressed = _pack(tmp_path, old_state, "old2", "--compress")
	_, new_plain = _pack(tmp_path, new_state, "new2")
	assert json.loads(_extract(tmp_path, _comments(old_compressed, new_plain)).stdout) == new_state


def _bad_payloads(module) -> dict[str, tuple[bytes, bytes]]:
	raw = json.dumps({"status": "in_progress", "wave": 9}).encode()
	good = zlib.compress(raw, 9)
	bomb_raw = b"{" + b" " * (module.MAX_DECOMPRESSED_STATE_BYTES + 10) + b"}"
	return {
		"corrupt": (module.COMPRESSED_PAYLOAD_MARKER + b"\x00garbage" + good[8:], raw),
		"truncated": (module.COMPRESSED_PAYLOAD_MARKER + good[:-6], raw),
		"trailing": (module.COMPRESSED_PAYLOAD_MARKER + good + b"extra", raw),
		"wrong_manifest": (module.COMPRESSED_PAYLOAD_MARKER + good, raw + b" "),
		"oversized": (module.COMPRESSED_PAYLOAD_MARKER + zlib.compress(bomb_raw, 9), bomb_raw),
	}


@pytest.mark.parametrize("kind", ["corrupt", "truncated", "trailing", "wrong_manifest", "oversized"])
def test_bad_compressed_payload_falls_back_to_the_older_chain(tmp_path: Path, kind: str) -> None:
	payload, raw = _bad_payloads(_module())[kind]
	older = {"status": "in_progress", "wave": 1}
	_, old_chain = _pack(tmp_path, older, "old", "--compress")
	result = _extract(tmp_path, _comments(old_chain, _forged_chain(payload, raw)))
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == older
	# With no older chain there is nothing valid; --require-latest refuses the old one.
	assert _extract(tmp_path, _comments(_forged_chain(payload, raw))).returncode == 1
	assert _extract(tmp_path, _comments(old_chain, _forged_chain(payload, raw)), "--require-latest").returncode == 1
	# prune-plan does not count the bad chain either, so it deletes nothing newer than the old one.
	plan = _prune_plan(tmp_path, _comments(old_chain, _forged_chain(payload, raw)))
	assert plan == {"delete": [], "chains": 1, "kept": 1}


def test_decode_payload_bounds_output() -> None:
	module = _module()
	bomb = module.COMPRESSED_PAYLOAD_MARKER + zlib.compress(b"\0" * (module.MAX_DECOMPRESSED_STATE_BYTES + 1))
	assert module._decode_payload(bomb) is None
	exact = b"{" + b" " * (module.MAX_DECOMPRESSED_STATE_BYTES - 2) + b"}"
	assert module._decode_payload(module.COMPRESSED_PAYLOAD_MARKER + zlib.compress(exact)) == exact
	assert module._decode_payload(b'{"a": 1}') == b'{"a": 1}'


def test_skip_status_failed_on_compressed_chains(tmp_path: Path) -> None:
	usable = {"status": "in_progress", "wave": 3}
	_, a = _pack(tmp_path, usable, "a", "--compress")
	_, b = _pack(tmp_path, {"status": "failed", "wave": 4}, "b", "--compress")
	result = _extract(tmp_path, _comments(a, b), "--skip-status", "failed")
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == usable


# --- prune-plan ------------------------------------------------------------------


def test_prune_plan_handles_compressed_chains(tmp_path: Path) -> None:
	state = _fingerprint_state(files=40)
	_, oldest = _pack(tmp_path, {**state, "status": "in_progress", "wave": 1}, "c1", "--compress")
	_, middle = _pack(tmp_path, {**state, "status": "in_progress", "wave": 2}, "c2")
	_, failed = _pack(tmp_path, {**state, "status": "failed", "wave": 3}, "c3", "--compress")
	comments = _comments(oldest, middle, failed)
	ids = [c["id"] for c in comments]
	plan = _prune_plan(tmp_path, comments, "--keep-not-status", "failed")
	assert plan["chains"] == 3
	assert plan["kept"] == 2
	# The oldest chain goes; the uncompressed middle (newest not failed) and the newest stay.
	assert plan["delete"] == ids[: len(oldest)]

#!/usr/bin/env python3
"""Tests for scripts/truncate_to_utf8_byte_cap.py.

Verifies the release-gate notify step's UTF-8-safe truncation helper
honours its byte-cap contract under multi-byte payloads (✓/✗/⏳/
em-dashes the consolidated soft-error summary routinely contains)
without splitting a codepoint and without corrupting the rendered
Telegram message.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "truncate_to_utf8_byte_cap.py"


def _truncate(payload: bytes, cap: int) -> bytes:
	proc = subprocess.run(
		[sys.executable, str(SCRIPT), str(cap)],
		input=payload,
		capture_output=True,
		check=True,
	)
	return proc.stdout


def test_short_input_passes_through_unchanged():
	out = _truncate(b"hello world", 100)
	assert out == b"hello world"


def test_short_unicode_passes_through_unchanged():
	out = _truncate("✓ release SUCCEEDED — all gates passed".encode("utf-8"), 100)
	assert out.decode("utf-8") == "✓ release SUCCEEDED — all gates passed"


def test_truncation_respects_byte_cap_with_ascii():
	out = _truncate(b"a" * 5000, 100)
	assert len(out) <= 100
	assert out == b"a" * 100


def test_truncation_lands_on_codepoint_boundary_with_unicode():
	"""Multi-byte UTF-8 (Greek, 2 bytes each) must not be split mid-codepoint."""
	payload = ("αβγδε" * 1000).encode("utf-8")  # 10 000 bytes
	for cap in (100, 200, 500, 999, 1234, 3950):
		out = _truncate(payload, cap)
		assert len(out) <= cap, f"cap={cap} but got {len(out)} bytes"
		# Strict UTF-8 decode must succeed — no partial codepoints.
		out.decode("utf-8")


def test_truncation_with_emoji_no_mid_byte_split():
	"""4-byte UTF-8 emoji must not be split mid-codepoint."""
	# ⏳ is 3 bytes in UTF-8; 🚀 is 4 bytes.
	payload = ("⏳🚀" * 500).encode("utf-8")
	for cap in (50, 200, 1000):
		out = _truncate(payload, cap)
		assert len(out) <= cap
		out.decode("utf-8")  # strict decode


def test_zero_cap_passes_through_input_unchanged():
	"""cap=0 mirrors the consolidator's "disable truncation" semantic."""
	payload = b"abc" * 1000
	out = _truncate(payload, 0)
	assert out == payload


def test_orphan_leader_dropped_at_boundary():
	"""A boundary that lands right on a multi-byte leader drops the leader.

	Without that, downstream `decode("utf-8")` would emit U+FFFD for the
	orphaned 0xC0+ byte and Telegram's renderer would show a literal
	replacement glyph.
	"""
	# `α` = 0xCE 0xB1 (2 bytes). cap=1 lands on the leader 0xCE; the
	# helper must walk back to drop it, leaving an empty output.
	payload = "α".encode("utf-8")
	out = _truncate(payload, 1)
	assert out == b""


def test_helper_rejects_negative_cap():
	proc = subprocess.run(
		[sys.executable, str(SCRIPT), "-5"],
		input=b"hello",
		capture_output=True,
	)
	assert proc.returncode == 2


def test_helper_rejects_non_integer_cap():
	proc = subprocess.run(
		[sys.executable, str(SCRIPT), "abc"],
		input=b"hello",
		capture_output=True,
	)
	assert proc.returncode == 2


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())

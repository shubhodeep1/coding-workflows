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


def _assert_maximal_valid_prefix(out: bytes, payload: bytes, cap: int) -> None:
	"""Stronger assertion than `len(out) <= cap` + decode-validity: the
	output must be the LONGEST valid-UTF-8 prefix of `payload` that fits
	in `cap` bytes. Catches the data-loss bug where the truncator
	silently shortens the output even when a longer valid prefix exists
	(e.g. when the cap lands exactly on a codepoint boundary).
	"""
	assert len(out) <= cap, f"cap={cap} but got {len(out)} bytes"
	out.decode("utf-8")  # must be valid UTF-8 (strict).
	assert payload.startswith(out), "output must be a prefix of the input"

	# Maximality: any longer prefix of payload (still within cap) must
	# fail strict UTF-8 decode. Otherwise we discarded bytes that fit.
	for j in range(len(out) + 1, min(cap, len(payload)) + 1):
		try:
			payload[:j].decode("utf-8")
		except UnicodeDecodeError:
			continue
		raise AssertionError(
			f"truncation undershot: out={len(out)} bytes, but prefix of "
			f"length {j} also decodes as valid UTF-8 (cap={cap}, "
			f"input_len={len(payload)})"
		)


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
		_assert_maximal_valid_prefix(_truncate(payload, cap), payload, cap)


def test_truncation_with_emoji_no_mid_byte_split():
	"""3- and 4-byte UTF-8 codepoints must not be split mid-codepoint."""
	# ⏳ is 3 bytes in UTF-8; 🚀 is 4 bytes.
	payload = ("⏳🚀" * 500).encode("utf-8")
	for cap in (50, 200, 1000):
		_assert_maximal_valid_prefix(_truncate(payload, cap), payload, cap)


def test_exact_codepoint_boundary_preserves_full_codepoint():
	"""Regression: cap landing exactly on a codepoint boundary must NOT
	drop a complete glyph that already fits.

	An earlier walk-back implementation examined `data[i-1]` and
	unconditionally dropped the leader at the boundary, which silently
	shortened the output by one whole codepoint at every exact-boundary
	cap. Multi-model PR review (6 of 7 reviewers, conf 5/5) reproduced
	the bug; this test pins the corrected algorithm.
	"""
	# `αα` is exactly 4 UTF-8 bytes; cap=4 must return all 4.
	payload = "αα".encode("utf-8")
	out = _truncate(payload, 4)
	assert out == payload, f"expected {payload!r}, got {out!r}"

	# `α` is exactly 2 bytes; cap=2 must return all 2.
	payload = "α".encode("utf-8")
	out = _truncate(payload, 2)
	assert out == payload, f"expected {payload!r}, got {out!r}"

	# More general: every cap in [n*2 for n in 1..5] against ααααα
	# (10 bytes, 5 codepoints) must return exactly that many codepoints
	# — never one fewer than fits.
	payload = ("α" * 5).encode("utf-8")
	for codepoints_to_keep in range(1, 6):
		cap = codepoints_to_keep * 2  # 2 bytes per α
		out = _truncate(payload, cap)
		assert out == payload[:cap], (
			f"cap={cap} expected {codepoints_to_keep} α's "
			f"({payload[:cap]!r}) but got {out!r}"
		)


def test_exact_boundary_with_three_byte_codepoint():
	"""Same exact-boundary regression for 3-byte codepoints."""
	# ⏳ is 3 bytes (0xE2 0x8F 0xB3); cap=3 must return the full glyph.
	payload = "⏳".encode("utf-8")
	out = _truncate(payload, 3)
	assert out == payload, f"expected {payload!r}, got {out!r}"

	payload = ("⏳" * 3).encode("utf-8")  # 9 bytes total
	for codepoints_to_keep in range(1, 4):
		cap = codepoints_to_keep * 3
		out = _truncate(payload, cap)
		assert out == payload[:cap], (
			f"cap={cap} expected {codepoints_to_keep} ⏳'s, got {out!r}"
		)


def test_zero_cap_passes_through_input_unchanged():
	"""cap=0 mirrors the consolidator's "disable truncation" semantic."""
	payload = b"abc" * 1000
	out = _truncate(payload, 0)
	assert out == payload


def test_cap_inside_multibyte_codepoint_drops_partial():
	"""A cap that lands INSIDE a multi-byte codepoint must drop the
	whole partial codepoint — including its leader.

	Without that, downstream `decode("utf-8")` would emit U+FFFD for the
	orphaned 0xC0+ byte and Telegram's renderer would show a literal
	replacement glyph.
	"""
	# `α` = 0xCE 0xB1 (2 bytes). cap=1 lands on the leader 0xCE — the
	# next byte (0xB1) would extend the codepoint but is excluded.
	# data[1] = 0xB1 is a continuation byte, so walk-back from i=1
	# reaches i=0; output is empty.
	payload = "α".encode("utf-8")
	assert _truncate(payload, 1) == b""

	# `ααα` = 6 bytes; cap=5 lands inside the 3rd α (mid-codepoint).
	# Walk back to drop the partial codepoint → 4 bytes = "αα".
	payload = "ααα".encode("utf-8")
	out = _truncate(payload, 5)
	assert out == "αα".encode("utf-8"), (
		f"cap=5 inside ααα expected b'αα' (4 bytes), got {out!r}"
	)


def test_helper_rejects_negative_cap():
	proc = subprocess.run(
		[sys.executable, str(SCRIPT), "-5"],
		input=b"hello",
		capture_output=True,
	)
	assert proc.returncode == 2


def test_library_function_treats_negative_cap_as_noop():
	"""Direct library calls with negative cap return the input unchanged.

	Without this guard, `data[:cap]` would invoke Python's negative-
	index slicing and silently strip a tail byte (a real PR-review
	finding from kimi-k2). The CLI wrapper rejects negative caps with
	exit 2, but library callers go through `truncate_bytes_to_utf8_cap`
	directly and would otherwise hit the footgun.
	"""
	import importlib.util
	spec = importlib.util.spec_from_file_location("_t", SCRIPT)
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	assert mod.truncate_bytes_to_utf8_cap(b"hello", -1) == b"hello"
	assert mod.truncate_bytes_to_utf8_cap(b"hello", -100) == b"hello"
	# cap=0 also a no-op (operator opt-out at consolidator level).
	assert mod.truncate_bytes_to_utf8_cap(b"hello", 0) == b"hello"


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

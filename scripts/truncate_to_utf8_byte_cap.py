#!/usr/bin/env python3
"""Truncate stdin to a UTF-8 byte cap on a codepoint boundary.

Reads bytes from stdin, writes <= cap bytes to stdout, never splitting
a multi-byte UTF-8 sequence. Used by the release-gate notify step
to enforce Telegram's 4096-char body limit without corrupting the
multi-byte glyphs (✓/✗/⏳/em-dashes) the consolidated soft-error
summary routinely contains.

Usage: cat msg.txt | truncate_to_utf8_byte_cap.py <cap>

The library function `truncate_bytes_to_utf8_cap` is the canonical
truncation algorithm — `scripts/consolidate_soft_error_reports.py`
imports it so both code paths share boundary handling. Tests for the
maximality and validity of the algorithm live in
`tests/test_truncate_to_utf8_byte_cap.py`.

Exit codes are reserved for argument errors only — payload size never
causes failure (the whole point is to make the truncation a fail-open
defensive backstop for the Telegram step).
"""

from __future__ import annotations

import sys


def truncate_bytes_to_utf8_cap(data: bytes, cap: int) -> bytes:
	"""Return a prefix of `data` whose byte length is `<= cap`, ending on
	a UTF-8 codepoint boundary when the input is valid UTF-8.

	**Contract.** Callers MUST pass valid UTF-8 input. Under that
	assumption, the returned prefix is the maximal valid-UTF-8 prefix
	whose length is `<= cap`. For malformed input the function still
	returns a byte-level prefix obtained by walking back over
	continuation bytes, but the result is not guaranteed to decode
	cleanly — that case is out of scope for the release-gate use sites
	(both the Telegram MSG and the consolidator's per-phase reports
	originate from `errors="replace"` reads, which always re-encode to
	valid UTF-8). Adding strict-decode validation here would cost a
	full decode on every call without benefiting any real call site.

	If `data[cap]` (the first byte beyond the cap) is a continuation
	byte (top two bits == 0b10), the cap fell inside a multi-byte
	codepoint; walk back to the leader's position and stop there. The
	slice `data[:i]` then ends on a clean codepoint boundary because
	the codepoint containing `data[cap]` is fully excluded.

	Edge cases. (a) `cap <= 0` returns the input unchanged; this
	matches operator opt-out semantics at the consolidator level
	(`max_bytes <= 0` → no truncation), so a CLI invocation
	`truncate_to_utf8_byte_cap.py 0` is the same as a no-op.
	A negative `cap` is also treated as a no-op rather than letting
	Python's negative-index slicing (`data[:-1]`) silently strip a
	tail byte — the helper is a library function, and library
	functions should not surprise their callers.
	(b) `cap >= len(data)` returns data unchanged — the input already
	fits, no truncation needed. (c) Malformed UTF-8 input causes the
	walk-back to step over continuation bytes until it finds a non-
	continuation or hits i=0, returning at most an empty bytes object
	rather than crashing.

	An earlier version of this helper examined `data[i-1]` and
	unconditionally dropped any leader byte at the boundary. That
	pattern silently lost a complete codepoint when the cap landed
	exactly on a codepoint boundary (e.g. cap=4 against `b"αα"`
	returned `b"α"` instead of `b"αα"`); multiple PR reviewers
	reproduced the data-loss. The current implementation examines
	`data[cap]` instead, so an exact-boundary cap is preserved.
	"""
	if cap <= 0 or len(data) <= cap:
		return data
	i = cap
	while i > 0 and (data[i] & 0xC0) == 0x80:
		i -= 1
	return data[:i]


def main() -> int:
	if len(sys.argv) != 2:
		print("usage: truncate_to_utf8_byte_cap.py <cap>", file=sys.stderr)
		return 2
	try:
		cap = int(sys.argv[1])
	except ValueError:
		print(f"cap must be an integer, got {sys.argv[1]!r}", file=sys.stderr)
		return 2
	if cap < 0:
		print(f"cap must be non-negative, got {cap}", file=sys.stderr)
		return 2

	data = sys.stdin.buffer.read()
	sys.stdout.buffer.write(truncate_bytes_to_utf8_cap(data, cap))
	return 0


if __name__ == "__main__":
	sys.exit(main())


#!/usr/bin/env python3
"""Truncate stdin to a UTF-8 byte cap on a codepoint boundary.

Reads bytes from stdin, writes <= cap bytes to stdout, never splitting
a multi-byte UTF-8 sequence. Used by the release-gate notify step
to enforce Telegram's 4096-char body limit without corrupting the
multi-byte glyphs (✓/✗/⏳/em-dashes) the consolidated soft-error
summary routinely contains.

Usage: cat msg.txt | truncate_to_utf8_byte_cap.py <cap>

Exit codes are reserved for argument errors only — payload size never
causes failure (the whole point is to make the truncation a fail-open
defensive backstop for the Telegram step).
"""

from __future__ import annotations

import sys


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
	if cap == 0 or len(data) <= cap:
		sys.stdout.buffer.write(data)
		return 0

	i = cap
	# Walk back over UTF-8 continuation bytes (top two bits == 0b10) so
	# the slice never lands inside a multi-byte codepoint.
	while i > 0 and (data[i - 1] & 0xC0) == 0x80:
		i -= 1
	# If the byte just before the boundary is itself a multi-byte leader
	# (0xC0+) without enough trailing bytes, drop it too — otherwise the
	# downstream `decode("utf-8")` would surface a U+FFFD for the orphaned
	# leader and Telegram's renderer would show a literal replacement
	# glyph in the message body.
	if i > 0 and data[i - 1] >= 0xC0:
		i -= 1
	sys.stdout.buffer.write(data[:i])
	return 0


if __name__ == "__main__":
	sys.exit(main())

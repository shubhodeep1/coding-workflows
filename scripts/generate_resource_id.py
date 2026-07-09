#!/usr/bin/env python3
"""Generate resource identifiers that preserve the repo's stable-ID contract."""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(SCRIPT_DIR))


from ai_memory_lib import make_record_id, sanitize_segment  # noqa: E402


def generate_id(prefix: str, salt: str | None = None) -> str:
	if salt is None:
		return make_record_id(prefix)

	sanitized_prefix = sanitize_segment(prefix, "mem")
	timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
	suffix = hashlib.sha256(salt.encode("utf-8")).hexdigest()[:10]
	return f"{sanitized_prefix}_{timestamp}_{suffix}"


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Generate a stable-format resource ID.")
	parser.add_argument("--prefix", required=True, help="Prefix segment to preserve or sanitize.")
	parser.add_argument("--salt", default=None, help="Optional deterministic salt for the suffix.")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	print(generate_id(args.prefix, args.salt))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

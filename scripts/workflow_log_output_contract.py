#!/usr/bin/env python3
"""Validate and atomically publish untrusted workflow-log model output."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MODE_CONTRACTS = {
	"analysis": (
		"## Executive Summary",
		("## Speed Optimizations", "## Cost Optimizations", "## Reliability Improvements", "## AI Memory Health", "## GH API Call Audit", "## Prompt Cache & Memory System", "## Orchestrator Health", "## Pipeline Flow Bottlenecks", "## Per-Repo Breakdown", "## Metrics Appendix"),
	),
	"retro": (
		"## Weekly Retro",
		("### What Worked", "### Failure Modes", "### Next Week Recommendation", "### Metrics Snapshot"),
	),
	"deep-audit": (
		"## Deep Audit — Workflows & Scripts ({date})",
		("### Section 1: Bug & Correctness Sweep", "### Section 2: GitHub API Call Redundancy Audit", "### Section 3: Code Duplication & Modularization Opportunities", "### Section 4: Expression Size Limit Risk Assessment", "### Section 5: Cross-Cutting Concerns", "### Section 6: Summary & Severity Matrix", "#### 6A. Findings Summary Table", "#### 6B. Estimated Remediation Scope"),
	),
	"api-redundancy": (
		"## API Call Consolidation & Dead-Call Analysis ({date})",
		("### Safety Tag Legend", "### Consolidation Candidates (MERGE-###)", "### Redundant Re-Fetch (REUSE-###)", "### Dead Calls (DEAD-API-###)", "### Cross-References to Deep Audit Section", "### Summary Counts", "### Implement-Stage Handoff"),
	),
}


def fail(message: str) -> None:
	raise SystemExit(f"workflow log output validation failed: {message}")


def validate_output(candidate: Path, mode: str, expected_date: str) -> bytes:
	try:
		if candidate.is_symlink() or not candidate.is_file():
			fail("candidate must be a regular non-symlink file")
		candidate_size = candidate.stat().st_size
		if not 0 < candidate_size <= MAX_OUTPUT_BYTES:
			fail("candidate size is outside the allowed range")
		raw = candidate.read_bytes()
	except OSError as exc:
		fail(f"candidate is unreadable: {exc}")
	if b"\x00" in raw:
		fail("candidate contains NUL bytes")
	try:
		text = raw.decode("utf-8")
	except UnicodeDecodeError:
		fail("candidate is not valid UTF-8")
	if any(ord(character) < 32 and character not in "\n\t" for character in text) or "\x7f" in text:
		fail("candidate contains disallowed control bytes")
	if "\r" in text:
		fail("candidate must use LF line endings")
	first_heading, required_headings = MODE_CONTRACTS[mode]
	if "{date}" in first_heading:
		if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expected_date) is None:
			fail("an expected UTC date is required for this mode")
		first_heading = first_heading.format(date=expected_date)
	lines = text.splitlines()
	if not lines or lines[0] != first_heading:
		fail(f"first heading must be {first_heading!r}")
	ordered_headings = (first_heading, *required_headings)
	positions: list[int] = []
	for heading in ordered_headings:
		matching_positions = [index for index, line in enumerate(lines) if line == heading]
		if len(matching_positions) != 1:
			fail(f"required heading must occur exactly once: {heading}")
		positions.append(matching_positions[0])
	if positions != sorted(positions) or len(set(positions)) != len(positions):
		fail("required headings are out of order")
	return text.rstrip().encode("utf-8") + b"\n"


def publish(output_path: Path, content: bytes, allowed_root: Path | None) -> None:
	if output_path.is_symlink():
		fail("output path must not be a symlink")
	output_parent = output_path.parent.resolve()
	if allowed_root is not None:
		allowed_resolved = allowed_root.resolve()
		try:
			output_parent.relative_to(allowed_resolved)
		except ValueError:
			fail("output path is outside the allowed root")
	output_parent.mkdir(parents=True, exist_ok=True)
	file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output_path.name}.", dir=output_parent)
	try:
		with os.fdopen(file_descriptor, "wb") as handle:
			handle.write(content)
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary_name, output_path)
	except BaseException:
		try:
			os.unlink(temporary_name)
		except OSError:
			pass
		raise


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--mode", choices=tuple(MODE_CONTRACTS), required=True)
	parser.add_argument("--candidate", required=True)
	parser.add_argument("--output", required=True)
	parser.add_argument("--expected-date", default="")
	parser.add_argument("--allowed-output-root")
	args = parser.parse_args()
	content = validate_output(Path(args.candidate), args.mode, args.expected_date)
	publish(Path(args.output), content, Path(args.allowed_output_root) if args.allowed_output_root else None)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

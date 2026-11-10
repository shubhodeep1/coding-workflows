#!/usr/bin/env python3
"""Structure check for the workflow log analysis report (project #9116, P2).

The report is model output written in the isolated container, so it is
untrusted. This script only reads it as text and never echoes report text:
the log line carries counts, the fixed heading titles below, and repository
names taken from the collector's ``workflow_log_report.json`` after a strict
``owner/repo`` filter. That keeps model text from injecting workflow commands.

Check mode (default) prints exactly one line to stdout:

	WORKFLOW_LOG_ANALYSIS_REPORT_CHECK outcome=<ok|invalid> missing_headings=<n>
	missing_repos=<csv|none> bytes=<n> too_small=<true|false>
	exempt_repos=<csv|none> exempt_source=<coverage|errors|unavailable>

(one line; wrapped here). Exit codes: 0 valid, 1 invalid, 2 the report could
not be read or the arguments are invalid. The caller treats any exit other
than 1 as "accept the report" (fail open).

``--insert-data-gap`` mode adds a fixed data-gap line to the top of the report
(under a ``<!-- workflow-log-analysis:... -->`` window marker when the first
line is one). It is idempotent and writes atomically. Exit 0, or 2 on an I/O
error.

Stdlib only. No network, no GitHub API calls.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from typing import Any


LOG_PREFIX = "WORKFLOW_LOG_ANALYSIS_REPORT_CHECK"

# The order fixed by prompts/mode-workflow-analysis.txt ("Use these section
# headings exactly, in this exact order").
REQUIRED_HEADINGS: tuple[str, ...] = (
	"Executive Summary",
	"Speed Optimizations",
	"Cost Optimizations",
	"Reliability Improvements",
	"AI Memory Health",
	"GH API Call Audit",
	"Prompt Cache & Memory System",
	"Orchestrator Health",
	"Pipeline Flow Bottlenecks",
	"Per-Repo Breakdown",
	"Metrics Appendix",
)
PER_REPO_HEADING = "Per-Repo Breakdown"

DEFAULT_COLLECT_REPORT = "workflow_log_report.json"
DEFAULT_MIN_BYTES = 2000

REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
# The collector's max-runs cap entry (scripts/collect_workflow_logs.py) marks a
# truncated repository, not a failed one, so it never exempts the repository.
MAX_RUNS_MESSAGE_PREFIX = "Run collection stopped after max-runs="

DATA_GAP_LINE = "> Data gap: full workflow logs were unavailable for this run."
WINDOW_MARKER_RE = re.compile(r"^<!-- workflow-log-analysis:.*-->$")
DATA_GAP_IDEMPOTENCY_LINES = 3


def _fence_marker(line: str) -> str:
	stripped = line.lstrip()
	for marker in ("```", "~~~"):
		if stripped.startswith(marker):
			return marker
	return ""


def _is_fence(line: str) -> bool:
	return bool(_fence_marker(line))


def _unfenced_lines(lines: list[str]) -> list[tuple[int, str]]:
	"""Return (index, line) for every line outside fenced code blocks.

	Fence lines themselves are dropped, so they can never count as headings.
	"""
	result: list[tuple[int, str]] = []
	open_marker = ""
	for index, line in enumerate(lines):
		marker = _fence_marker(line)
		if marker:
			# A fence closes only with the marker character that opened it.
			if not open_marker:
				open_marker = marker
				continue
			if marker == open_marker:
				open_marker = ""
				continue
		if not open_marker:
			result.append((index, line))
	return result


def _count_missing_headings(unfenced: list[tuple[int, str]]) -> list[str]:
	"""In-order subsequence scan over the required headings.

	A heading that is absent, or only present before the previous match,
	counts as missing.
	"""
	missing: list[str] = []
	position = 0
	for title in REQUIRED_HEADINGS:
		wanted = f"## {title}"
		found_at = -1
		for offset in range(position, len(unfenced)):
			if unfenced[offset][1].rstrip() == wanted:
				found_at = offset
				break
		if found_at < 0:
			missing.append(title)
			continue
		position = found_at + 1
	return missing


def _per_repo_subheadings(unfenced: list[tuple[int, str]]) -> list[str] | None:
	"""Return the lowercased ``### `` headings of the Per-Repo Breakdown section.

	None when the section heading is absent. The section ends at the next
	unfenced ``## `` heading.
	"""
	wanted = f"## {PER_REPO_HEADING}"
	start = -1
	for offset, (_, line) in enumerate(unfenced):
		if line.rstrip() == wanted:
			start = offset
			break
	if start < 0:
		return None
	subheadings: list[str] = []
	for _, line in unfenced[start + 1:]:
		if line.startswith("## "):
			break
		if line.startswith("### "):
			subheadings.append(line.lower())
	return subheadings


def _valid_repo_names(values: Any) -> list[str]:
	names: list[str] = []
	if not isinstance(values, list):
		return names
	for value in values:
		if isinstance(value, str) and REPO_NAME_RE.match(value) and value not in names:
			names.append(value)
	return names


def _exempt_from_collect_report(payload: Any) -> tuple[list[str], str]:
	"""Failed repositories exempt from the per-repo check, and their source.

	``coverage.repositories_failed`` (P3) wins when present. Otherwise the
	collector's exception-path ``errors[]`` entries (scope ``runs``, not the
	max-runs cap) reproduce the same definition from data written today.
	"""
	coverage = payload.get("coverage") if isinstance(payload, dict) else None
	if isinstance(coverage, dict) and isinstance(coverage.get("repositories_failed"), list):
		raw: list[Any] = []
		for entry in coverage["repositories_failed"]:
			if isinstance(entry, dict):
				raw.append(entry.get("repository"))
			else:
				raw.append(entry)
		return _valid_repo_names(raw), "coverage"
	raw_errors: list[Any] = []
	errors = payload.get("errors") if isinstance(payload, dict) else None
	if isinstance(errors, list):
		for entry in errors:
			if not isinstance(entry, dict) or entry.get("scope") != "runs":
				continue
			message = entry.get("message")
			if isinstance(message, str) and message.startswith(MAX_RUNS_MESSAGE_PREFIX):
				continue
			raw_errors.append(entry.get("repository"))
	return _valid_repo_names(raw_errors), "errors"


def _load_collect_report(path: str) -> Any:
	try:
		with open(path, encoding="utf-8") as handle:
			return json.load(handle)
	except (OSError, ValueError):
		return None


def _csv(values: list[str]) -> str:
	return ",".join(values) if values else "none"


def run_check(report_path: str, collect_report_path: str, min_bytes: int) -> int:
	try:
		with open(report_path, "rb") as handle:
			raw = handle.read()
	except OSError:
		print(f"::warning::{LOG_PREFIX}: the report file could not be read; skipping the structure check.", file=sys.stderr)
		return 2

	text = raw.decode("utf-8", errors="replace")
	lines = text.splitlines()
	unfenced = _unfenced_lines(lines)
	missing_headings = _count_missing_headings(unfenced)
	byte_count = len(raw)
	too_small = byte_count < min_bytes

	missing_repos: list[str] = []
	exempt_repos: list[str] = []
	payload = _load_collect_report(collect_report_path)
	if not isinstance(payload, dict):
		exempt_source = "unavailable"
		print(f"::warning::{LOG_PREFIX}: the collect report is missing or unreadable; skipping the per-repo check.")
	else:
		scope = payload.get("scope")
		required = _valid_repo_names(scope.get("repositories") if isinstance(scope, dict) else None)
		exempt_all, exempt_source = _exempt_from_collect_report(payload)
		exempt_lower = {name.lower() for name in exempt_all}
		subheadings = _per_repo_subheadings(unfenced) or []
		for repo in required:
			if repo.lower() in exempt_lower:
				exempt_repos.append(repo)
				continue
			# Bounded match so "owner/app-two" never covers "owner/app"; a
			# host-path heading ("github.com/owner/app") still counts.
			pattern = re.compile(r"(?<![A-Za-z0-9._-])" + re.escape(repo.lower()) + r"(?![A-Za-z0-9._-])")
			if not any(pattern.search(heading) for heading in subheadings):
				missing_repos.append(repo)

	valid = not missing_headings and not missing_repos and not too_small
	print(
		f"{LOG_PREFIX} outcome={'ok' if valid else 'invalid'}"
		f" missing_headings={len(missing_headings)}"
		f" missing_repos={_csv(missing_repos)}"
		f" bytes={byte_count}"
		f" too_small={'true' if too_small else 'false'}"
		f" exempt_repos={_csv(exempt_repos)}"
		f" exempt_source={exempt_source}"
	)
	if not valid:
		# Only fixed titles, filtered repo names and numbers; never report text.
		print(
			"::warning::The workflow log analysis report failed the structure check:"
			f" missing or out-of-order headings: {'; '.join(missing_headings) if missing_headings else 'none'};"
			f" missing repo subsections: {_csv(missing_repos)};"
			f" bytes={byte_count} (minimum {min_bytes})."
		)
	return 0 if valid else 1


def insert_data_gap(report_path: str) -> int:
	try:
		with open(report_path, "rb") as handle:
			raw = handle.read()
	except OSError:
		print(f"::warning::{LOG_PREFIX}: the report file could not be read; data-gap line not added.", file=sys.stderr)
		return 2

	lines = raw.splitlines(keepends=True)
	gap_bytes = DATA_GAP_LINE.encode("utf-8")
	for line in lines[:DATA_GAP_IDEMPOTENCY_LINES]:
		if line.rstrip(b"\r\n") == gap_bytes:
			return 0

	insert = [gap_bytes + b"\n", b"\n"]
	first = lines[0].rstrip(b"\r\n").decode("utf-8", errors="replace") if lines else ""
	if lines and WINDOW_MARKER_RE.match(first):
		head = lines[0] if lines[0].endswith(b"\n") else lines[0] + b"\n"
		new_lines = [head, *insert, *lines[1:]]
	else:
		new_lines = [*insert, *lines]
	data = b"".join(new_lines)

	directory = os.path.dirname(os.path.abspath(report_path))
	tmp_path = ""
	try:
		mode = stat.S_IMODE(os.stat(report_path).st_mode)
		fd, tmp_path = tempfile.mkstemp(prefix=".wla-report-", suffix=".tmp", dir=directory)
		with os.fdopen(fd, "wb") as handle:
			handle.write(data)
		os.chmod(tmp_path, mode)
		os.replace(tmp_path, report_path)
		tmp_path = ""
	except OSError:
		print(f"::warning::{LOG_PREFIX}: the data-gap line could not be written.", file=sys.stderr)
		return 2
	finally:
		if tmp_path:
			try:
				os.unlink(tmp_path)
			except OSError:
				pass
	return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Check the structure of a workflow log analysis report.")
	parser.add_argument("--report", required=True, help="Path to the analysis markdown report.")
	parser.add_argument(
		"--collect-report",
		default=DEFAULT_COLLECT_REPORT,
		help=f"Path to the collector JSON (default {DEFAULT_COLLECT_REPORT}).",
	)
	parser.add_argument(
		"--min-bytes",
		type=int,
		default=DEFAULT_MIN_BYTES,
		help=f"Minimum report size in bytes (default {DEFAULT_MIN_BYTES}).",
	)
	parser.add_argument(
		"--insert-data-gap",
		action="store_true",
		help="Add the fixed data-gap line to the top of the report instead of checking it.",
	)
	args = parser.parse_args(argv)
	if args.min_bytes < 0:
		parser.error("--min-bytes must be zero or a positive integer")
	return args


def main(argv: list[str] | None = None) -> int:
	args = _parse_args(argv)
	if args.insert_data_gap:
		return insert_data_gap(args.report)
	return run_check(args.report, args.collect_report, args.min_bytes)


if __name__ == "__main__":
	raise SystemExit(main())

#!/usr/bin/env python3
"""Score clarify issue bodies for advisory informal-issue signals.

This helper is intentionally advisory-only in Phase 4. It does not make
workflow decisions; it emits one stdout line that a future wrapper can
prepend to clarify context.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
from pathlib import Path

TEMPLATE_HEADINGS = (
	"## Steps to reproduce",
	"## Expected behavior",
	"## Actual behavior",
	"### Acceptance criteria",
	"## Context",
	"## Description",
	"## Environment",
)

IMPERATIVE_VERBS = {
	"add",
	"fix",
	"create",
	"update",
	"remove",
	"implement",
	"support",
	"enable",
	"disable",
	"refactor",
	"migrate",
	"port",
	"test",
}

ACCEPTANCE_PATTERNS = (
	re.compile(r"\bshould\b", re.IGNORECASE),
	re.compile(r"\bmust\b", re.IGNORECASE),
	re.compile(r"\bexpect\b", re.IGNORECASE),
	re.compile(r"\bassert\b", re.IGNORECASE),
	re.compile(r"\bverify\b", re.IGNORECASE),
	re.compile(r"\bcomplete\s+when\b", re.IGNORECASE),
	re.compile(r"\bdone\s+when\b", re.IGNORECASE),
	re.compile(r"\bacceptance\s+criteria\b", re.IGNORECASE),
)

LOG_PREFIX_RE = re.compile(r"^\s*(\[\w+\]|\d{4}-\d{2}-\d{2}|::|>\s)")
URL_RE = re.compile(r"https?://\S+")
WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

SIGNAL_CHECKS = (
	("template_paste", lambda body: _has_template_paste(body)),
	("no_ask", lambda body: _has_no_ask(body)),
	("no_acceptance", lambda body: _has_no_acceptance(body)),
	("pure_log_dump", lambda body: _has_pure_log_dump(body)),
	("link_only", lambda body: _has_link_only(body)),
)

PARSE_ERROR_OUTPUT = "CLARIFY_INFORMAL_SCORE: 0.000; signals=parse_error"


def _read_issue_body_from_file(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _read_issue_body_from_stdin(timeout_secs: float = 5.0) -> str:
	result: dict[str, object] = {}

	def _reader() -> None:
		try:
			result["data"] = sys.stdin.buffer.read()
		except BaseException as exc:  # pragma: no cover - defensive fail-open path.
			result["error"] = exc

	thread = threading.Thread(target=_reader, daemon=True)
	thread.start()
	thread.join(timeout_secs)
	if thread.is_alive():
		raise TimeoutError(f"stdin read exceeded {timeout_secs} seconds")

	error = result.get("error")
	if isinstance(error, BaseException):
		raise error

	data = result.get("data", b"")
	if not isinstance(data, bytes):
		raise ValueError("stdin reader did not return bytes")
	return data.decode("utf-8")


def _has_template_paste(body: str) -> bool:
	matched_headings = {line.strip() for line in body.splitlines() if line.strip() in TEMPLATE_HEADINGS}
	return len(matched_headings) >= 3


def _has_no_ask(body: str) -> bool:
	if "?" in body:
		return False
	first_words = WORD_RE.findall(body.lower())[:50]
	return not any(word in IMPERATIVE_VERBS for word in first_words)


def _has_no_acceptance(body: str) -> bool:
	return not any(pattern.search(body) for pattern in ACCEPTANCE_PATTERNS)


def _has_pure_log_dump(body: str) -> bool:
	non_blank_lines = [line for line in body.splitlines() if line.strip()]
	if not non_blank_lines:
		return False
	matching_lines = sum(1 for line in non_blank_lines if LOG_PREFIX_RE.match(line))
	return (matching_lines / len(non_blank_lines)) >= 0.8


def _has_link_only(body: str) -> bool:
	trimmed = body.strip()
	if len(trimmed) >= 50:
		return False
	return len(URL_RE.findall(trimmed)) == 1


def _detect_signals(body: str) -> list[str]:
	return [name for name, check in SIGNAL_CHECKS if check(body)]


def _render_output(signals: list[str]) -> str:
	score = len(signals) / len(SIGNAL_CHECKS)
	return f"CLARIFY_INFORMAL_SCORE: {score:.3f}; signals={','.join(signals)}"


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	source_group = parser.add_mutually_exclusive_group(required=True)
	source_group.add_argument("--issue-body-file", type=Path)
	source_group.add_argument("--issue-body-stdin", action="store_true")
	args = parser.parse_args()

	try:
		if args.issue_body_file is not None:
			body = _read_issue_body_from_file(args.issue_body_file)
		else:
			body = _read_issue_body_from_stdin()
	except (OSError, UnicodeError, TimeoutError, ValueError):
		print(PARSE_ERROR_OUTPUT)
		return 0

	print(_render_output(_detect_signals(body)))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

#!/usr/bin/env python3
"""Evaluate an authenticated declarative behavioural-smoke assertion bundle."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath


MAX_BUNDLE_BYTES = 1_048_576
MAX_SOURCE_BYTES = 1_048_576
MAX_ASSERTIONS = 100
DISALLOWED_ROOTS = {".git", ".ai", "validation"}


def _safe_source(repo_root: Path, value: object) -> Path | None:
	if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
		return None
	relative = PurePosixPath(value)
	if relative.is_absolute() or value != relative.as_posix() or any(part in ("", ".", "..") for part in relative.parts):
		return None
	if not relative.parts or relative.parts[0] in DISALLOWED_ROOTS:
		return None
	candidate = repo_root.joinpath(*relative.parts)
	try:
		cursor = candidate
		while cursor != repo_root:
			metadata = os.lstat(cursor)
			if stat.S_ISLNK(metadata.st_mode):
				return None
			cursor = cursor.parent
		metadata = candidate.stat()
	except FileNotFoundError:
		return candidate
	except OSError:
		return None
	if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_SOURCE_BYTES:
		return None
	return candidate


def _load_bundle(path: Path) -> dict[str, object]:
	if path.is_symlink() or path.stat().st_size > MAX_BUNDLE_BYTES:
		raise ValueError("bundle is a symlink or oversized")
	payload = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(payload, dict) or set(payload) != {"schema_version", "round", "head_sha", "language", "assertions"}:
		raise ValueError("bundle object is malformed")
	if payload.get("schema_version") != "behavioural_smoke_assertions.v1":
		raise ValueError("bundle schema is unsupported")
	if type(payload.get("round")) is not int or int(payload["round"]) < 1:
		raise ValueError("bundle round is invalid")
	if re.fullmatch(r"[0-9a-f]{40}", str(payload.get("head_sha", ""))) is None:
		raise ValueError("bundle head SHA is invalid")
	assertions = payload.get("assertions")
	if not isinstance(assertions, list) or len(assertions) > MAX_ASSERTIONS:
		raise ValueError("bundle assertions are invalid")
	return payload


def _evaluate(repo_root: Path, row: object) -> tuple[str, str]:
	if not isinstance(row, dict) or set(row) != {"issue_id", "file", "line_start", "line_end", "severity", "assertion"}:
		raise ValueError("assertion row is malformed")
	issue_id = row.get("issue_id")
	if not isinstance(issue_id, str) or not issue_id or len(issue_id) > 512:
		raise ValueError("assertion issue id is invalid")
	assertion = row.get("assertion")
	if not isinstance(assertion, dict) or type(assertion.get("expected_to_fail_until_fixed")) is not bool:
		raise ValueError("assertion object is malformed")
	assertion_type = assertion.get("type")
	if assertion_type == "inconclusive":
		if set(assertion) != {"type", "reason", "expected_to_fail_until_fixed"}:
			raise ValueError("inconclusive assertion keys are invalid")
		reason = assertion.get("reason")
		if not isinstance(reason, str) or not reason or len(reason) > 512:
			raise ValueError("inconclusive assertion reason is invalid")
		return "INCONCLUSIVE", issue_id
	if assertion_type not in {"text_present", "text_absent", "literal_count"}:
		raise ValueError("assertion type is unsupported")
	expected_keys = {"type", "path", "literal", "expected_to_fail_until_fixed"}
	if assertion_type == "literal_count":
		expected_keys.update({"min_count", "max_count"})
	if set(assertion) != expected_keys:
		raise ValueError("assertion keys are invalid")
	literal = assertion.get("literal")
	if not isinstance(literal, str) or not literal or len(literal) > 4096:
		raise ValueError("assertion literal is invalid")
	source_path = _safe_source(repo_root, assertion.get("path"))
	if source_path is None:
		raise ValueError("assertion source path is unsafe")
	if not source_path.exists():
		return "INCONCLUSIVE", issue_id
	try:
		content = source_path.read_text(encoding="utf-8")
	except (OSError, UnicodeDecodeError):
		return "INCONCLUSIVE", issue_id
	if assertion_type == "text_present":
		passed = literal in content
	elif assertion_type == "text_absent":
		passed = literal not in content
	else:
		minimum = assertion.get("min_count")
		maximum = assertion.get("max_count")
		if type(minimum) is not int or type(maximum) is not int or minimum < 0 or maximum < minimum or maximum > 1_000_000:
			raise ValueError("literal-count bounds are invalid")
		passed = minimum <= content.count(literal) <= maximum
	return ("PASSED" if passed else "FAILED"), issue_id


def main() -> int:
	if len(sys.argv) != 3:
		print("behavioural smoke evaluator requires REPO_ROOT and BUNDLE_PATH", file=sys.stderr)
		return 2
	repo_root = Path(sys.argv[1]).resolve(strict=True)
	bundle_path = Path(sys.argv[2]).resolve(strict=True)
	try:
		bundle_path.relative_to(repo_root / "validation" / "tests")
		bundle = _load_bundle(bundle_path)
	except (OSError, ValueError, json.JSONDecodeError) as exc:
		print(f"behavioural smoke bundle rejected: {exc}", file=sys.stderr)
		return 2
	rows = bundle["assertions"]
	print(f"1..{len(rows)}")
	try:
		for index, row in enumerate(rows, start=1):
			status, issue_id = _evaluate(repo_root, row)
			print(f"# BEHAVIOURAL_SMOKE_PRESENT_{status} issue={issue_id} round={bundle['round']}")
			print(f"ok {index} - behavioural smoke {issue_id}")
	except ValueError as exc:
		print(f"behavioural smoke bundle rejected: {exc}", file=sys.stderr)
		return 2
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

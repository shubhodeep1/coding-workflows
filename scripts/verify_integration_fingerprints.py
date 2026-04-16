#!/usr/bin/env python3
"""Verify that an orchestrator integration-sync resolver run preserved
merged sub-issue intent.

Reads a JSON fingerprints file (the same shape stored in the
orchestrator state field `merged_issue_fingerprints`) and walks each
sub-issue's `must_contain` / `must_not_contain` regex lists against the
post-resolve working tree (i.e. the cwd this script is invoked from).

Exit codes:
  0 — All fingerprints satisfied (or none recorded — fail-open empty).
  1 — At least one fingerprint violation.
  2 — Plumbing failure (file missing, JSON unparseable). Caller is
      expected to treat exit 2 as a soft warning, not a hard reject —
      the captured fingerprints might be stale or absent for legitimate
      reasons (e.g. sub-issue merged before fingerprinting was
      enabled).

Inputs:
  Positional argument — path to the fingerprints JSON file.
  Or env INTEGRATION_FINGERPRINTS_FILE — same path. Positional wins.
  Optional env INTEGRATION_BRANCH_NAME — included in the summary line
  for operator log readability.

This script is referenced by `.github/workflows/review_autofix.yml` in
the conflict-resolver step (post-codex, pre-commit). It is currently
listed in `OPTIONAL_BOOTSTRAP_SCRIPTS` so older consumer-repo script
refs can fail open until the next stable cut.

Going-forward only: see `capture_intent_fingerprints_for_merged_subissue`
in `scripts/orchestrate_poll_process.sh` for the capture half.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any


def _load_fingerprints(path: str) -> tuple[dict[str, Any] | None, str | None]:
	try:
		with open(path, "r", encoding="utf-8") as fh:
			data = json.load(fh)
	except FileNotFoundError:
		return None, f"fingerprints file not found: {path}"
	except json.JSONDecodeError as exc:
		return None, f"fingerprints JSON unparseable ({exc})"
	except Exception as exc:  # noqa: BLE001 — fail-open for any IO error
		return None, f"fingerprints file unreadable ({exc})"
	if not isinstance(data, dict):
		return None, "fingerprints file is not a JSON object"
	return data, None


def _read_file(path: str, cache: dict[str, str | None]) -> str | None:
	if path in cache:
		return cache[path]
	try:
		with open(path, "r", encoding="utf-8", errors="replace") as fh:
			content: str | None = fh.read()
	except FileNotFoundError:
		content = None
	except Exception as exc:  # noqa: BLE001 — verifier must not crash on bad file
		print(
			f"::warning::fingerprint verifier could not read {path}: {exc}",
			flush=True,
		)
		content = None
	cache[path] = content
	return content


def verify(fingerprints: dict[str, Any], branch: str) -> int:
	violations: list[str] = []
	mc_total_expected = 0
	mc_total_satisfied = 0
	file_cache: dict[str, str | None] = {}

	for issue_key, entry in sorted(fingerprints.items()):
		if not isinstance(entry, dict):
			continue
		issue_num = entry.get("issue", issue_key)
		pr_num = entry.get("pr", "?")

		must_contain = entry.get("must_contain", []) or []
		must_not_contain = entry.get("must_not_contain", []) or []

		for fp in must_contain:
			if not isinstance(fp, dict):
				continue
			path = fp.get("file", "")
			regex_src = fp.get("regex", "")
			if not path or not regex_src:
				continue
			mc_total_expected += 1
			content = _read_file(path, file_cache)
			if content is None:
				violations.append(
					f"issue #{issue_num} (PR #{pr_num}): must_contain pattern in '{path}' "
					f"could not be checked — file does not exist in post-resolve tree."
				)
				continue
			try:
				pattern = re.compile(regex_src)
			except re.error as exc:
				print(
					f"::warning::fingerprint regex compile failed for issue #{issue_num} ({exc}); skipping that pattern.",
					flush=True,
				)
				continue
			if pattern.search(content):
				mc_total_satisfied += 1
			else:
				violations.append(
					f"issue #{issue_num} (PR #{pr_num}): must_contain pattern missing from '{path}' "
					f"after resolver — sub-issue intent silently reverted. "
					f"Pattern (first 200 chars): {regex_src[:200]}"
				)

		for fp in must_not_contain:
			if not isinstance(fp, dict):
				continue
			path = fp.get("file", "")
			regex_src = fp.get("regex", "")
			if not path or not regex_src:
				continue
			content = _read_file(path, file_cache)
			if content is None:
				continue
			try:
				pattern = re.compile(regex_src)
			except re.error as exc:
				print(
					f"::warning::fingerprint regex compile failed for issue #{issue_num} ({exc}); skipping that pattern.",
					flush=True,
				)
				continue
			if pattern.search(content):
				violations.append(
					f"issue #{issue_num} (PR #{pr_num}): must_not_contain pattern reappeared in '{path}' "
					f"after resolver — sub-issue intentional deletion silently reverted. "
					f"Pattern (first 200 chars): {regex_src[:200]}"
				)

	# #5 silent-regression detector: log the must_contain satisfaction
	# ratio. The hard match check above already rejects any specific
	# drop, but the ratio gives operators a quick at-a-glance signal
	# in the log when something is going wrong systemically.
	if mc_total_expected > 0:
		ratio_pct = (mc_total_satisfied * 100) // mc_total_expected
		print(
			f"Integration fingerprint verification (branch={branch}): "
			f"must_contain satisfied {mc_total_satisfied}/{mc_total_expected} "
			f"({ratio_pct}%)",
			flush=True,
		)
		if mc_total_satisfied < mc_total_expected:
			print(
				f"::warning::Silent-regression detector: post-resolve tree contains fewer "
				f"must_contain fingerprint matches ({mc_total_satisfied}) than were captured "
				f"({mc_total_expected}). Investigating violations below.",
				flush=True,
			)

	if violations:
		print(
			"::error::Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent:",
			flush=True,
		)
		for v in violations:
			print(f"::error::  - {v}", flush=True)
		print(
			"::error::Refusing to create [ai-merge-resolve] commit. The orchestrator integration judge "
			"will be invoked on the next poll tick if INTEGRATION_SYNC_CONFLICT_MAX_RETRIES has been reached.",
			flush=True,
		)
		return 1

	print(
		"Integration fingerprint verification PASSED — all merged sub-issue intent preserved.",
		flush=True,
	)
	return 0


def main(argv: list[str] | None = None) -> int:
	args = list(sys.argv[1:] if argv is None else argv)
	fp_path = args[0] if args else os.environ.get("INTEGRATION_FINGERPRINTS_FILE", "")
	if not fp_path:
		print(
			"::warning::verify_integration_fingerprints: no fingerprints path supplied (positional arg or INTEGRATION_FINGERPRINTS_FILE env); skipping verification.",
			flush=True,
		)
		return 2

	branch = os.environ.get("INTEGRATION_BRANCH_NAME", "")
	data, err = _load_fingerprints(fp_path)
	if err is not None:
		print(f"::warning::{err}; skipping verification.", flush=True)
		return 2
	assert data is not None  # for type narrowing

	if not data:
		print(
			"Integration fingerprints object is empty; no merged sub-issues to verify. Skipping.",
			flush=True,
		)
		return 0

	return verify(data, branch)


if __name__ == "__main__":
	sys.exit(main())

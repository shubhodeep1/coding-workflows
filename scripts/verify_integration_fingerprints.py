#!/usr/bin/env python3
"""Verify that an orchestrator integration-sync resolver run preserved
merged sub-issue intent.

Reads a JSON fingerprints file (the same shape stored in the
orchestrator state field `merged_issue_fingerprints`) and walks each
sub-issue's `must_contain` / `must_not_contain` regex lists against the
post-resolve working tree (i.e. the cwd this script is invoked from).

Two modes:

  1. Default (verify): exit 0 if all fingerprints are satisfied, 1 on
     violation, 2 on plumbing failure. Used by the conflict-resolver
     step post-codex, pre-commit.

  2. --list-violated-files <fingerprints.json>: print one unique file
     path per line of every file that currently fails at least one
     fingerprint check (must_contain not matching, or must_not_contain
     matching, or must_contain referenced file missing). Always exits 0
     even when violations exist; plumbing failures still exit 2. Used by
     `scripts/review_conflict_prepare.sh` to expand the resolver's
     working set so auto-merged files with silent sub-issue regressions
     are surfaced to Codex in addition to files git marked as unmerged.

     stdout contract in this mode: file paths ONLY, one per line.
     Any diagnostic output (::warning::, ::error::, debug prints)
     MUST go to stderr so the caller can pipe stdout directly into
     the expanded-working-set artefacts without post-filtering.
     GitHub Actions captures annotations from stderr too, so routing
     warnings to stderr does not suppress the operator-visible
     annotation in the run log.

Exit codes:
  0 — verify: all fingerprints satisfied (or none recorded — fail-open
      empty).  list-violated-files: always (prints zero or more paths).
  1 — verify: at least one fingerprint violation. Not used in
      list-violated-files mode.
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
		# Route to stderr: in --list-violated-files mode stdout carries
		# file paths the caller consumes as data (see
		# scripts/review_conflict_prepare.sh); a `::warning::` line on
		# stdout would be captured as a phantom path and crash the
		# downstream check_resolver_diff.sh guard.  GitHub Actions
		# renders annotations from stderr too, so the verify-mode
		# annotation contract is unaffected.
		print(
			f"::warning::fingerprint verifier could not read {path}: {exc}",
			flush=True,
			file=sys.stderr,
		)
		content = None
	cache[path] = content
	return content


def _fp_key(fp: Any) -> tuple[str, str] | None:
	if not isinstance(fp, dict):
		return None
	path = fp.get("file", "")
	regex_src = fp.get("regex", "")
	if not path or not regex_src:
		return None
	return (path, regex_src)


def _substring_overlap_drops(
	mc_with_keys: list[tuple[Any, tuple[str, str] | None]],
	mnc_with_keys: list[tuple[Any, tuple[str, str] | None]],
) -> set[tuple[str, str]]:
	# Capture-side artefact: when a sub-issue extended an existing line
	# by appending text (e.g. removed `foo bar.` and added
	# `foo bar. When baz, accepted ...`), capture wraps both via
	# re.escape() and stores the shorter regex under must_not_contain
	# and the longer one under must_contain on the same file.  Under
	# re.search (used by both verify modes here), any post-resolve tree
	# that satisfies the longer must_contain trivially matches the
	# shorter must_not_contain too, so the constraint pair is
	# structurally unsatisfiable — the resolver burns its 3-attempt
	# retry budget and times out at the step wall-clock cap on a hunk
	# it cannot make pass.  Drop the must_not_contain side; the
	# must_contain side already enforces the stronger intent (the
	# longer added line being present).  Both regexes here come from
	# re.escape on diff lines, so substring containment in the regex
	# source is equivalent to substring containment in the literal
	# matched text.  The capture half (orchestrate_poll_process.sh
	# capture_intent_fingerprints_for_merged_subissue) applies the same
	# filter at write time so freshly captured state files no longer
	# admit the bad pair; this verifier-side check covers state files
	# written before the capture-side fix landed.
	drops: set[tuple[str, str]] = set()
	for _, mc_key in mc_with_keys:
		if mc_key is None:
			continue
		mc_path, mc_regex = mc_key
		for _, mnc_key in mnc_with_keys:
			if mnc_key is None:
				continue
			mnc_path, mnc_regex = mnc_key
			if (
				mnc_path == mc_path
				and mnc_regex != mc_regex
				and mnc_regex in mc_regex
			):
				drops.add(mnc_key)
	return drops


def list_violated_files(fingerprints: dict[str, Any]) -> list[str]:
	"""Return a sorted, de-duplicated list of file paths that currently
	fail at least one fingerprint check against the cwd tree.

	Mirrors the matching logic in :func:`verify` but records only the
	offending file paths (for expanding the resolver's working set
	pre-codex) — does not emit ::error:: annotations and never fails
	hard on a violation.
	"""
	violated: set[str] = set()
	file_cache: dict[str, str | None] = {}

	for issue_key, entry in fingerprints.items():
		if not isinstance(entry, dict):
			continue
		must_contain = entry.get("must_contain", []) or []
		must_not_contain = entry.get("must_not_contain", []) or []

		# Cross-dedup: skip (file, regex) pairs recorded in both lists
		# for this issue (historic capture false positives) and pairs
		# where the must_not_contain regex is a literal substring of a
		# must_contain regex on the same file (see
		# :func:`_substring_overlap_drops`).  Stay silent in
		# list-violated-files mode: the stdout contract is "file paths
		# ONLY" and the verify path already emits the operator-visible
		# warnings.
		mc_with_keys = [(fp, _fp_key(fp)) for fp in must_contain]
		mnc_with_keys = [(fp, _fp_key(fp)) for fp in must_not_contain]
		mc_keys = {k for _, k in mc_with_keys if k is not None}
		mnc_keys = {k for _, k in mnc_with_keys if k is not None}
		shared_keys = mc_keys & mnc_keys
		substring_drops = _substring_overlap_drops(mc_with_keys, mnc_with_keys)
		drops = shared_keys | substring_drops
		if drops:
			must_contain = [fp for fp, key in mc_with_keys if key not in drops]
			must_not_contain = [fp for fp, key in mnc_with_keys if key not in drops]

		for fp in must_contain:
			if not isinstance(fp, dict):
				continue
			path = fp.get("file", "")
			regex_src = fp.get("regex", "")
			if not path or not regex_src:
				continue
			content = _read_file(path, file_cache)
			if content is None:
				# Referenced file missing entirely — treat as violated
				# so the resolver gets a chance to restore it.
				violated.add(path)
				continue
			try:
				pattern = re.compile(regex_src)
			except re.error:
				continue
			if not pattern.search(content):
				violated.add(path)

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
			except re.error:
				continue
			if pattern.search(content):
				violated.add(path)

	return sorted(violated)


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

		# Defensive cross-dedup: the extractor in
		# capture_intent_fingerprints_for_merged_subissue
		# (scripts/orchestrate_poll_process.sh) filters net-no-op lines,
		# but that capture function is idempotent per issue — already-
		# stored bad entries in an orchestrator state file persist until
		# the state is rebuilt. A line a PR both removes and re-adds
		# (e.g. wrapping a bare call in an if/else fallback) cannot
		# simultaneously be required to appear AND required to be
		# absent; skip any (file, regex) pair recorded in both sets for
		# this issue so historic bad fingerprints don't produce
		# perpetual false positives.

		mc_with_keys = [(fp, _fp_key(fp)) for fp in must_contain]
		mnc_with_keys = [(fp, _fp_key(fp)) for fp in must_not_contain]
		mc_keys = {k for _, k in mc_with_keys if k is not None}
		mnc_keys = {k for _, k in mnc_with_keys if k is not None}
		shared_keys = mc_keys & mnc_keys
		substring_drops = _substring_overlap_drops(mc_with_keys, mnc_with_keys)
		if shared_keys:
			print(
				f"::warning::Fingerprint cross-dedup for issue #{issue_num} (PR #{pr_num}): "
				f"skipping {len(shared_keys)} self-contradictory pattern(s) present in both "
				f"must_contain and must_not_contain (capture-side refactor false positive).",
				flush=True,
			)
		if substring_drops:
			print(
				f"::warning::Fingerprint substring-overlap dedup for issue #{issue_num} (PR #{pr_num}): "
				f"skipping {len(substring_drops)} must_not_contain pattern(s) whose regex is a literal "
				f"substring of a must_contain regex on the same file (capture-side: deleted line subsumed "
				f"by added line under re.search; the longer must_contain already enforces the stronger intent).",
				flush=True,
			)
		drops = shared_keys | substring_drops
		if drops:
			must_contain = [fp for fp, key in mc_with_keys if key not in drops]
			must_not_contain = [fp for fp, key in mnc_with_keys if key not in drops]

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

	# Optional --list-violated-files <fingerprints.json> mode.  Keep the
	# flag parsing deliberately minimal (no argparse) so this stays a
	# single-file utility safe to bootstrap on older script refs.
	list_mode = False
	if args and args[0] == "--list-violated-files":
		list_mode = True
		args = args[1:]

	fp_path = args[0] if args else os.environ.get("INTEGRATION_FINGERPRINTS_FILE", "")
	if not fp_path:
		# Route to stderr so the stdout contract ("file paths ONLY") holds
		# in list mode even on plumbing failures.  GitHub Actions still
		# renders ::warning:: annotations from stderr.
		print(
			"::warning::verify_integration_fingerprints: no fingerprints path supplied (positional arg or INTEGRATION_FINGERPRINTS_FILE env); skipping verification.",
			flush=True,
			file=sys.stderr,
		)
		return 2

	branch = os.environ.get("INTEGRATION_BRANCH_NAME", "")
	data, err = _load_fingerprints(fp_path)
	if err is not None:
		# Same rationale as above — stderr keeps list-mode stdout clean.
		print(f"::warning::{err}; skipping verification.", flush=True, file=sys.stderr)
		return 2
	assert data is not None  # for type narrowing

	if list_mode:
		# Always exit 0 in list mode — the caller is collecting input
		# for the resolver's working set, not enforcing.  Empty JSON
		# simply prints nothing.
		for path in list_violated_files(data):
			print(path, flush=True)
		return 0

	if not data:
		print(
			"Integration fingerprints object is empty; no merged sub-issues to verify. Skipping.",
			flush=True,
		)
		return 0

	return verify(data, branch)


if __name__ == "__main__":
	sys.exit(main())

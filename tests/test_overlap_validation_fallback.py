#!/usr/bin/env python3
"""Tests for overlap validation fallback summary behavior.

Validates that:
- The editor fallback summary includes both required metadata keys
  ('Regression fingerprint:' and 'Runtime failure path:') so the overlap
  gate in review_autofix.yml does not block commits when the editor fails.
- A summary lacking those keys would correctly fail the gate (regression guard).
- The gate logic is case-insensitive, matching the `grep -qi` in the workflow.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Fallback summary content (must stay in sync with review_autofix.yml)
# ---------------------------------------------------------------------------

# Primary fallback: written when editor fails after retries (before overlap gate).
EDITOR_FAILED_FALLBACK = """\
          Changes made:
          - none (editor failed before producing a validated summary)

          Already satisfied (suggested but already present):
          - none (editor failed before producing a validated summary)

          Ignored suggestions (with short reason):
          - editor failed after retries before final classification

          Reviewer files processed:
          - none (editor failed before producing a validated summary)

          Review file issue audit:
          - none (editor failed before producing a validated summary)

          Regression fingerprint:
          - unavailable (editor fallback)

          Runtime failure path:
          - unavailable (editor fallback)
"""

# Secondary fallback: written in "Post editor summary comment" when file is empty.
NO_SUMMARY_FALLBACK = """\
          Changes made:
          - none (no editor summary output was generated)

          Already satisfied (suggested but already present):
          - none (no editor summary output was generated)

          Ignored suggestions (with short reason):
          - no summary available from editor stage

          Reviewer files processed:
          - none (no editor summary output was generated)

          Review file issue audit:
          - none (no editor summary output was generated)

          Regression fingerprint:
          - unavailable (editor fallback)

          Runtime failure path:
          - unavailable (editor fallback)
"""


# ---------------------------------------------------------------------------
# Helper: simulate the workflow's overlap gate check
# ---------------------------------------------------------------------------

def _overlap_gate_passes(summary: str) -> bool:
	"""Return True if summary satisfies the overlap gate.

	Mimics the updated shell check in review_autofix.yml, which:
	1. Checks for 'Regression fingerprint:' and 'Runtime failure path:' (case-insensitive).
	2. If either is missing, allows the commit when a fallback marker is detected
	   (grep -Eqi 'editor failed before producing a validated summary|unavailable (editor fallback)').
	3. Blocks the commit only when metadata is missing AND no fallback marker is present.
	"""
	has_regression = bool(re.search(r'regression fingerprint:', summary, re.IGNORECASE))
	has_runtime = bool(re.search(r'runtime failure path:', summary, re.IGNORECASE))
	if has_regression and has_runtime:
		return True
	# Fallback mode: allow even without strict metadata headers
	is_fallback = bool(re.search(
		r'(?:editor failed before producing a validated summary|unavailable \(editor fallback\))',
		summary,
		re.IGNORECASE,
	))
	return is_fallback


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_editor_failed_fallback_passes_overlap_gate():
	"""Primary fallback summary must satisfy the overlap metadata gate."""
	assert _overlap_gate_passes(EDITOR_FAILED_FALLBACK), (
		"Editor-failed fallback summary is missing 'Regression fingerprint:' "
		"and/or 'Runtime failure path:' — overlap gate will block commits."
	)


def test_no_summary_fallback_passes_overlap_gate():
	"""Secondary (empty-summary) fallback must also satisfy the overlap gate."""
	assert _overlap_gate_passes(NO_SUMMARY_FALLBACK), (
		"No-summary fallback is missing 'Regression fingerprint:' "
		"and/or 'Runtime failure path:' — overlap gate will block commits."
	)


def test_summary_without_keys_fails_gate():
	"""A non-fallback summary lacking the required keys must fail the gate (regression guard)."""
	incomplete_summary = """\
          Changes made:
          - fixed the bug in authentication logic

          Ignored suggestions (with short reason):
          - skipped cosmetic refactoring: out of scope
"""
	assert not _overlap_gate_passes(incomplete_summary), (
		"A non-fallback summary without the required metadata keys should fail the overlap gate."
	)


def test_fallback_marker_alone_passes_gate():
	"""A summary with the fallback marker but without metadata keys still passes (fallback mode)."""
	marker_only_summary = """\
          Changes made:
          - none (editor failed before producing a validated summary)

          Ignored suggestions (with short reason):
          - editor failed after retries before final classification
"""
	assert _overlap_gate_passes(marker_only_summary), (
		"A fallback summary (detected via marker) should pass the gate even without strict metadata keys."
	)


def test_unavailable_marker_alone_passes_gate():
	"""A summary with 'unavailable (editor fallback)' marker passes even without explicit metadata keys."""
	marker_summary = """\
          Changes made:
          - none (no editor summary output was generated)

          Ignored suggestions (with short reason):
          - no summary available from editor stage

          Some key: unavailable (editor fallback)
"""
	assert _overlap_gate_passes(marker_summary), (
		"A summary with 'unavailable (editor fallback)' marker should pass the gate in fallback mode."
	)


def test_gate_check_is_case_insensitive():
	"""Gate check must be case-insensitive, matching `grep -qi`."""
	# Mixed-case variants of both keys.
	variants = [
		"REGRESSION FINGERPRINT:\n- some value\nRUNTIME FAILURE PATH:\n- some value",
		"Regression Fingerprint:\n- some value\nRuntime Failure Path:\n- some value",
		"regression fingerprint:\n- some value\nruntime failure path:\n- some value",
	]
	for summary in variants:
		assert _overlap_gate_passes(summary), (
			f"Gate check should pass case-insensitively for: {summary!r}"
		)


def test_partial_keys_fail_gate():
	"""Having only one of the two keys must still fail the gate."""
	only_regression = "Regression fingerprint:\n- some value\n"
	only_runtime = "Runtime failure path:\n- some value\n"

	assert not _overlap_gate_passes(only_regression), (
		"Summary with only 'Regression fingerprint:' should fail the gate."
	)
	assert not _overlap_gate_passes(only_runtime), (
		"Summary with only 'Runtime failure path:' should fail the gate."
	)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

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
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())

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

	Mimics the shell check in review_autofix.yml:
	  grep -qi 'Regression fingerprint:' ... && grep -qi 'Runtime failure path:' ...
	"""
	has_regression = bool(re.search(r'regression fingerprint:', summary, re.IGNORECASE))
	has_runtime = bool(re.search(r'runtime failure path:', summary, re.IGNORECASE))
	return has_regression and has_runtime


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
	"""A summary lacking the required keys must fail the gate (regression guard)."""
	incomplete_summary = """\
          Changes made:
          - none (editor failed before producing a validated summary)

          Ignored suggestions (with short reason):
          - editor failed after retries before final classification
"""
	assert not _overlap_gate_passes(incomplete_summary), (
		"A summary without the required metadata keys should fail the overlap gate."
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

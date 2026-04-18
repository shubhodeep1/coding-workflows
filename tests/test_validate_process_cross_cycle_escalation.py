#!/usr/bin/env python3
"""Regression tests for the cross-cycle needs_fixes -> harness_error escalation.

The validate-diagnose prompt was inverted (prefer `needs_fixes` when in doubt)
to give the consumer-repo editor more autonomy to fix failures directly. The
safety net is an orchestrator-side escalation: if the same fix-up proposal
(fingerprinted from sorted fix_issues[].title) fails across 3 consecutive
cycles, we promote to `harness_error` so the workflow surfaces a human-visible
signal instead of looping indefinitely.

These tests lock the mechanism in place by asserting on the shell structure,
not runtime behavior, so they stay fast and deterministic.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
DIAGNOSE_PROMPT = REPO_ROOT / "prompts" / "mode-validate-diagnose.txt"


def _validate_process_text() -> str:
	return VALIDATE_PROCESS.read_text(encoding="utf-8")


def _diagnose_prompt_text() -> str:
	return DIAGNOSE_PROMPT.read_text(encoding="utf-8")


def _extract_needs_fixes_branch() -> str:
	text = _validate_process_text()
	match = re.search(
		r'  needs_fixes\)\n(?P<branch>.*?)\n    ;;\n\n  harness_error\)',
		text,
		re.DOTALL,
	)
	if not match:
		raise AssertionError("could not extract needs_fixes branch from validate_process.sh")
	return match.group("branch")


def _extract_harness_error_branch() -> str:
	text = _validate_process_text()
	match = re.search(
		r'  harness_error\)\n(?P<branch>.*?)\n    ;;\n\n  infeasible\)',
		text,
		re.DOTALL,
	)
	if not match:
		raise AssertionError("could not extract harness_error branch from validate_process.sh")
	return match.group("branch")


def test_fingerprint_is_sha256_of_sorted_fix_issue_titles() -> None:
	"""Q5=A: fingerprint hashes sort(fix_issues[].title), 16-char hex prefix."""
	text = _validate_process_text()
	assert "FAILURE_FINGERPRINT=" in text, (
		"expected FAILURE_FINGERPRINT assignment in validate_process.sh"
	)
	assert "sort_by(.title" in text, (
		"expected jq sort_by(.title) when computing the failure fingerprint"
	)
	assert "sha256sum" in text, (
		"expected sha256sum in the fingerprint pipeline"
	)
	assert "cut -c1-16" in text, (
		"expected 16-char hex prefix of the sha256 fingerprint"
	)


def test_escalation_gated_on_cycle_ge_3_and_hits_ge_2() -> None:
	"""Q7=B: escalate only from the 3rd consecutive cycle onward with the same fingerprint."""
	text = _validate_process_text()
	# Cycle gate
	assert re.search(r'\[\s*"\$\{VALIDATION_CYCLE\}"\s*-ge\s*3\s*\]', text), (
		"expected VALIDATION_CYCLE >= 3 gate in escalation block (Q7=B)"
	)
	# Prior-hits gate
	assert re.search(r'\[\s*"\$\{PRIOR_FINGERPRINT_HITS\}"\s*-ge\s*2\s*\]', text), (
		"expected PRIOR_FINGERPRINT_HITS >= 2 gate in escalation block"
	)
	# Promotion side-effect
	assert 'DIAG_STATUS="harness_error"' in text, (
		"expected DIAG_STATUS to be re-assigned to harness_error on escalation"
	)
	assert 'ESCALATED_FROM_NEEDS_FIXES=true' in text, (
		"expected ESCALATED_FROM_NEEDS_FIXES flag to be set on escalation"
	)


def test_fingerprint_marker_embedded_in_needs_fixes_tracking_comment() -> None:
	"""Q6=A: the tracking comment includes an HTML-comment fingerprint marker
	so the next cycle's PRIOR_COMMENTS fetch can see it."""
	branch = _extract_needs_fixes_branch()
	assert "validation-failure-fingerprint:" in branch, (
		"expected `validation-failure-fingerprint:` HTML-comment marker in the "
		"needs_fixes tracking comment body"
	)
	assert "${FAILURE_FINGERPRINT}" in branch, (
		"expected FAILURE_FINGERPRINT variable to be interpolated into the marker"
	)
	assert "${VALIDATION_CYCLE}" in branch, (
		"expected VALIDATION_CYCLE to be recorded in the fingerprint marker"
	)


def test_harness_error_branch_handles_escalated_case() -> None:
	"""Q9=B: when escalated from needs_fixes, prefer LLM-provided harness_fixes
	if present; otherwise use a templated explanation."""
	branch = _extract_harness_error_branch()
	assert 'ESCALATED_FROM_NEEDS_FIXES' in branch, (
		"expected harness_error branch to check ESCALATED_FROM_NEEDS_FIXES flag"
	)
	assert "HARNESS_FIXES_FROM_LLM=" in branch, (
		"expected LLM-provided harness_fixes extraction on escalation (Q9=B)"
	)
	assert "Cross-cycle escalation" in branch, (
		"expected escalation annotation in the harness_fixes text"
	)


def test_prior_fingerprint_scan_uses_existing_prior_comments_fetch() -> None:
	"""Q6=A: no new GitHub API call — reuse PRIOR_COMMENTS already fetched
	at the top of validate_process.sh for the cycle-N LLM context."""
	text = _validate_process_text()
	# The PRIOR_COMMENTS variable must be defined before the escalation block
	# (which relies on it), and the escalation block must read from it.
	prior_comments_def = text.find('PRIOR_COMMENTS="$(gh_retry gh api')
	escalation_read = text.find('<!-- validation-failure-fingerprint: ${FAILURE_FINGERPRINT} cycle:')
	assert prior_comments_def != -1, (
		"expected PRIOR_COMMENTS fetch to exist (validate_process.sh:~1678)"
	)
	assert escalation_read != -1, (
		"expected escalation block to grep PRIOR_COMMENTS for fingerprint marker"
	)
	assert prior_comments_def < escalation_read, (
		"escalation block must run after PRIOR_COMMENTS is populated"
	)


def test_diagnose_prompt_inverts_when_in_doubt_default() -> None:
	"""Q8: prefer needs_fixes when in doubt; reserve harness_error for
	coding-workflows-owned files."""
	text = _diagnose_prompt_text()
	# Old default should be gone
	assert "When in doubt, prefer `harness_error`" not in text, (
		"old `prefer harness_error` default should have been removed"
	)
	# New rule markers
	assert "Classify as `harness_error` ONLY when" in text, (
		"expected new `ONLY when` rule for harness_error in the classification tree"
	)
	assert "coding-workflows" in text, (
		"expected coding-workflows ownership language in the classification tree"
	)
	# needs_fixes should cover everything else
	assert "Classify as `needs_fixes` for EVERYTHING ELSE" in text, (
		"expected `needs_fixes` to be the catch-all per Q8"
	)


def test_diagnose_prompt_schema_allows_harness_fixes_alongside_fix_issues() -> None:
	"""Q9=B: when status=needs_fixes, the LLM may also populate harness_fixes
	as a fallback hint used by cross-cycle escalation."""
	text = _diagnose_prompt_text()
	assert "fallback hint" in text or "fallback" in text, (
		"expected schema docstring to mention the harness_fixes fallback role"
	)
	# Neither schema keyword changed; the semantics change is prose-only.
	assert '"harness_fixes": "string"' in text, (
		"harness_fixes must remain in the schema"
	)


def main() -> int:
	failed = 0
	for name in sorted(n for n in globals() if n.startswith("test_")):
		try: globals()[name]()
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())

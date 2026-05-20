#!/usr/bin/env python3
"""Unit tests for `scripts/validate_editor_audit.sh`.

The helper extracts the "Reviewer audit sanity" arithmetic check that
was previously inlined inside `.github/workflows/review_autofix.yml`'s
"Validate editor no-op disposition" step (the
`EDITOR_NOOP_SUSPICIOUS=true` branch). It now backs both:

  1. The workflow's noop-suspicious detection (the inline block at
     ~line 4019 is now a `source` + function call).
  2. `scripts/orchestrate_poll_process.sh`'s noop-suspicious recovery
     sweep — specifically the force-merge fallback's "reviewer audit
     healthy?" gate. Force-merging a PR whose audit arithmetic does not
     balance would defeat the safety contract, so the two paths MUST
     consult the same regex/arithmetic logic.

These tests pin the helper's exit-code contract and warning literals
so the workflow's existing cross-workflow grep contracts
(test_review_autofix_editor_noop_cascade_contract.py and the e2e
poller in test-and-mark-stable.yml) keep passing.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "validate_editor_audit.sh"


def _run(summary_text: str, reviewers_successful: str | None = None, tmp_path: Path | None = None) -> subprocess.CompletedProcess:
	"""Write `summary_text` to a tempfile and invoke the helper against
	it. Returns the completed process so callers can inspect rc + stderr.
	"""
	assert tmp_path is not None, "Caller must pass pytest's tmp_path fixture"
	summary = tmp_path / "summary.txt"
	summary.write_text(summary_text, encoding="utf-8")
	cmd = ["bash", str(HELPER), str(summary)]
	if reviewers_successful is not None:
		cmd.append(reviewers_successful)
	return subprocess.run(cmd, capture_output=True, text=True, check=False)


def test_helper_script_exists_and_is_executable():
	assert HELPER.exists(), f"Shared audit helper not found at {HELPER}"
	assert HELPER.stat().st_mode & 0o111, "validate_editor_audit.sh must be executable"


def test_exit_zero_when_audit_balances(tmp_path):
	"""A canonical healthy audit (every entry has total == applied +
	already_applied + ignored) returns rc=0."""
	summary = textwrap.dedent(
		"""\
		Changes made:
		- none

		Review file issue audit:
		- review_a.md: total issues listed 3, issues applied 1, issues already applied 1, issues ignored 1
		- review_b.md: total issues listed 2, issues applied 0, issues already applied 2, issues ignored 0

		PR comment audit:
		- none
		"""
	)
	result = _run(summary, "3", tmp_path=tmp_path)
	assert result.returncode == 0, f"Expected rc=0 for healthy audit, got {result.returncode}; stderr={result.stderr!r}"


def test_exit_one_when_audit_section_is_only_none(tmp_path):
	"""Audit section contains only `- none` → treated as empty/fallback,
	rc=1. This mirrors the workflow's "audit section is empty or contains
	only fallback text" branch — and the workflow's literal is preserved
	byte-for-byte (lowercase `editor` because it appears mid-sentence
	after `but`)."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- none

		PR comment audit:
		- none
		"""
	)
	result = _run(summary, "3", tmp_path=tmp_path)
	assert result.returncode == 1
	# Workflow-path message preserves the exact pre-extraction literal:
	# "::warning::N reviewer(s) succeeded but editor audit section ..."
	assert "3 reviewer(s) succeeded but editor audit section is empty or contains only fallback text" in result.stderr


def test_exit_one_when_audit_section_contains_only_editor_failed_fallback(tmp_path):
	"""The `editor failed` substring is the other fallback marker
	stripped by `grep -viE 'editor failed'`. A section with only this
	must still be classified as empty/fallback."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- editor failed before producing a validated summary

		PR comment audit:
		- none
		"""
	)
	result = _run(summary, "2", tmp_path=tmp_path)
	assert result.returncode == 1


def test_exit_two_when_arithmetic_mismatch(tmp_path):
	"""One entry's total != applied + already_applied + ignored → rc=2.
	The per-line mismatch warning literal is preserved byte-for-byte."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- review_a.md: total issues listed 5, issues applied 1, issues already applied 1, issues ignored 1

		PR comment audit:
		- none
		"""
	)
	result = _run(summary, "1", tmp_path=tmp_path)
	assert result.returncode == 2
	assert "Audit entry arithmetic mismatch: total=5 but applied(1)+already_applied(1)+ignored(1)=3" in result.stderr


def test_exit_three_when_summary_file_missing(tmp_path):
	"""A non-existent summary file path is a usage error → rc=3."""
	cmd = ["bash", str(HELPER), str(tmp_path / "does-not-exist.txt")]
	result = subprocess.run(cmd, capture_output=True, text=True, check=False)
	assert result.returncode == 3
	assert "missing or unreadable" in result.stderr


def test_poller_path_omits_reviewer_count_prefix(tmp_path):
	"""When called WITHOUT a reviewers_successful argument (the poller's
	path — it has no `REVIEWERS_SUCCESSFUL` env var), the empty-audit
	warning omits the reviewer count prefix. The literal still contains
	the grep-friendly substring so operators searching for "editor audit
	section is empty" find both message variants."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- none
		"""
	)
	result = _run(summary, None, tmp_path=tmp_path)
	assert result.returncode == 1
	# Poller-path message: starts the sentence with uppercase "Editor"
	# and omits the reviewer-count prefix.
	assert "::warning::Editor audit section is empty or contains only fallback text" in result.stderr
	assert "reviewer(s) succeeded" not in result.stderr


def test_non_numeric_reviewer_count_falls_back_to_generic_warning(tmp_path):
	"""A non-numeric reviewers_successful value must not leak into the
	exact-string workflow warning literal. The helper should fall back to
	the generic poller-style warning instead."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- none
		"""
	)
	result = _run(summary, "N/A", tmp_path=tmp_path)
	assert result.returncode == 1
	assert "::warning::Editor audit section is empty or contains only fallback text" in result.stderr
	assert "N/A reviewer(s) succeeded" not in result.stderr


def test_balanced_arithmetic_with_zero_totals_passes(tmp_path):
	"""Entries with total=0 are skipped by the arithmetic check (they
	cannot mismatch). A summary with only zero-total entries passes."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- review_empty.md: total issues listed 0, issues applied 0, issues already applied 0, issues ignored 0
		- review_a.md: total issues listed 2, issues applied 0, issues already applied 1, issues ignored 1
		"""
	)
	result = _run(summary, "2", tmp_path=tmp_path)
	assert result.returncode == 0


def test_helper_is_sourceable_as_library(tmp_path):
	"""Sourcing the helper file defines `validate_editor_audit_arithmetic`
	but performs no work — the workflow and poller both source it and
	call the function. A sourcing-and-call dry-run must succeed and
	return the function's exit code via $?."""
	summary = tmp_path / "summary.txt"
	summary.write_text("Review file issue audit:\n- none\n", encoding="utf-8")
	# `set -e` would propagate a non-zero return from the function; we
	# disable it to capture the rc explicitly.
	script = textwrap.dedent(
		f"""\
		set +e
		source {HELPER}
		validate_editor_audit_arithmetic {summary} 2
		echo "rc=$?"
		"""
	)
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
	assert result.returncode == 0, f"Sourcing the helper itself must not fail; stderr={result.stderr!r}"
	assert "rc=1" in result.stdout, f"Function should return 1 for empty audit; got stdout={result.stdout!r}"


def test_unparseable_audit_line_fails_closed(tmp_path):
	"""A malformed audit line that cannot be fully parsed must return
	rc=2 rather than being skipped as healthy."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- review_a.md: total issues listed 2, issues applied 1

		PR comment audit:
		- none
		"""
	)
	result = _run(summary, "1", tmp_path=tmp_path)
	assert result.returncode == 2
	assert "Audit entry arithmetic mismatch: unparseable audit line" in result.stderr


def test_helper_handles_audit_section_followed_by_pr_comment_audit(tmp_path):
	"""The extractor stops at the explicit `PR comment audit:` heading.
	A `Review file issue audit:` section followed immediately by `PR
	comment audit:` must include only the audit entries, not the PR
	comment section."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- review_a.md: total issues listed 1, issues applied 1, issues already applied 0, issues ignored 0
		PR comment audit:
		- bot/foo: applied
		Regression fingerprint:
		- n/a
		"""
	)
	result = _run(summary, "1", tmp_path=tmp_path)
	# review_a.md's arithmetic balances (1 == 1 + 0 + 0), so rc=0.
	assert result.returncode == 0

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

import inspect
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "validate_editor_audit.sh"


def _run(summary_text: str, reviewers_successful: str | None = None, tmp_path: Path | None = None) -> subprocess.CompletedProcess:
	"""Write `summary_text` to a tempfile and invoke the helper against
	it. Returns the completed process so callers can inspect rc + stderr.
	"""
	assert tmp_path is not None, "Caller must pass a temporary Path"
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
	"""Entries with total=0 still pass when their summed counts are also
	zero."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- review_empty.md: total issues listed 0, issues applied 0, issues already applied 0, issues ignored 0
		- review_a.md: total issues listed 2, issues applied 0, issues already applied 1, issues ignored 1
		"""
	)
	result = _run(summary, "2", tmp_path=tmp_path)
	assert result.returncode == 0


def test_zero_total_with_non_zero_sum_fails_closed(tmp_path):
	"""Zero-total entries must still balance; non-zero applied counts are malformed."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- review_bad.md: total issues listed 0, issues applied 1, issues already applied 0, issues ignored 0
		"""
	)
	result = _run(summary, "1", tmp_path=tmp_path)
	assert result.returncode == 2
	assert "Audit entry arithmetic mismatch: total=0 but applied(1)+already_applied(0)+ignored(0)=1" in result.stderr


def test_existing_colon_separated_fixture_format_passes(tmp_path):
	"""Existing editor-summary fixtures use colon/space separators rather
	than the comma-delimited shape from the helper's first draft. The
	helper must continue accepting the historical fixture format so the
	workflow and poller stay aligned with summaries already in-repo."""
	summary = (REPO_ROOT / "tests" / "fixtures" / "editor_summaries" / "narrative_none_status_edited.txt").read_text(encoding="utf-8")
	result = _run(summary, "1", tmp_path=tmp_path)
	assert result.returncode == 0, result.stderr


def test_trailing_annotation_after_ignored_count_passes(tmp_path):
	"""The legacy per-field extractor tolerated extra prose after the
	ignored-count field. Keep that behavior so convergence notes or other
	harmless trailing annotations do not spuriously flip the audit to
	unhealthy."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- review_a.md: total issues listed: 3 issues applied: 0 issues already applied: 3 issues ignored: 0 (converged on current HEAD)

		PR comment audit:
		- none
		"""
	)
	result = _run(summary, "1", tmp_path=tmp_path)
	assert result.returncode == 0, result.stderr


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


def test_legitimate_audit_line_containing_editor_failed_phrase_is_not_filtered(tmp_path):
	"""The helper must strip only the fallback bullet shape, not any
	otherwise-valid audit line whose filename/prose happens to contain
	`editor failed`. Regression guard for the anchored grep filter."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- review editor failed case.md: total issues listed 1, issues applied 1, issues already applied 0, issues ignored 0

		PR comment audit:
		- none
		"""
	)
	result = _run(summary, "1", tmp_path=tmp_path)
	assert result.returncode == 0, result.stderr


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

def test_helper_stops_at_next_heading_when_pr_comment_audit_missing(tmp_path):
	"""If `PR comment audit:` is missing, the extractor must still stop at
	the next heading so later sections (Regression fingerprint / Runtime
	failure path) are not treated as audit lines."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- review_a.md: total issues listed 1, issues applied 1, issues already applied 0, issues ignored 0

		Regression fingerprint:
		- file:symbol
		Runtime failure path:
		- unit-test
		"""
	)
	result = _run(summary, None, tmp_path=tmp_path)
	assert result.returncode == 0, result.stderr


def test_helper_stops_at_generic_future_section_heading(tmp_path):
	"""Future summary-format changes can insert a new generic section
	heading between `Review file issue audit:` and `PR comment audit:`.
	The helper must stop at that heading instead of treating it as an
	unparseable audit line."""
	summary = textwrap.dedent(
		"""\
		Review file issue audit:
		- review_a.md: total issues listed 1, issues applied 1, issues already applied 0, issues ignored 0

		Additional notes:
		- this section is not part of the audit

		PR comment audit:
		- none
		"""
	)
	result = _run(summary, "1", tmp_path=tmp_path)
	assert result.returncode == 0, result.stderr


def test_convergence_shape_total_zero_already_applied_one_is_mismatch(tmp_path):
	"""Pin the exact false-positive shape observed on
	tele-funtoken-msg-scoring run 33088357425 (PR 3809): a convergence
	run whose editor recorded "confirmed a prior fix is already
	present" as `issues already applied 1` against `total issues
	listed 0`. The helper MUST keep flagging this as rc=2 — the
	strictness is intentional (it is what keeps `total` trustworthy as
	an auto-merge gate); the fix for the false positive lives in the
	editor prompt (scripts/review_apply_fixes.sh), which now tells the
	model to emit all four counts as 0 for a zero-issue review file."""
	summary = textwrap.dedent(
		"""\
		Changes made:
		- none

		Change status:
		- not-edited

		Review file issue audit:
		- review_deepseek.md: total issues listed 0; issues applied 0; issues already applied 1; issues ignored 0.
		- review_qwen.md: total issues listed 1; issues applied 0; issues already applied 0; issues ignored 1.

		PR comment audit:
		- none
		"""
	)
	result = _run(summary, "4", tmp_path=tmp_path)
	assert result.returncode == 2, (
		f"Expected rc=2 for the convergence false-positive shape, got "
		f"{result.returncode}: {result.stderr}"
	)
	assert (
		"Audit entry arithmetic mismatch: total=0 but applied(0)+already_applied(1)+ignored(0)=1"
		in result.stderr
	), result.stderr


def main() -> int:
	# Direct `python3 tests/<file>.py` entrypoint — the repo's CI runs
	# tests via that pattern rather than pytest discovery, so this file
	# needs its own runner for the assertions to execute under CI.
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	passed = 0
	failed = 0

	for func in test_funcs:
		name = func.__name__
		try:
			params = list(inspect.signature(func).parameters)
			if not params:
				func()
			elif params == ["tmp_path"]:
				with tempfile.TemporaryDirectory(prefix="validate-editor-audit-") as td:
					func(Path(td))
			else:
				raise TypeError(f"unsupported test signature for {name}: {params}")
			print(f"  PASS  {name}")
			passed += 1
		except AssertionError as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
		except Exception as e:
			print(f"  ERROR {name}: {type(e).__name__}: {e}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())

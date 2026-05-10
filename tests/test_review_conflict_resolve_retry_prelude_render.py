#!/usr/bin/env python3
"""Contract tests for outcome-aware retry-prelude rendering in
scripts/review_conflict_resolve.sh::_build_retry_prompt.

The retry-prelude path keeps the next-attempt reflexion prompt
accurate when the previous attempt was killed by `timeout` (exit
124 / 137), exited non-zero for another reason, or completed but
failed soft validation. Soft-validation never runs on the timeout
or exec_error paths, so the standard "you produced violations
last time, fix them" framing is misleading there.

`_build_retry_prompt` solves this by routing on a `_failure_kind`
positional arg:

  - `exec_error`  → copy the original prompt verbatim
                    (`_retry_prompt_outcome="verbatim:exec_error"`)
  - `timeout`     → render `integration-sync-conflict-resolver-
                    retry-timeout-prelude.txt`
                    (`_retry_prompt_outcome="timeout-prelude"`)
  - `validation`  → render `integration-sync-conflict-resolver-
                    retry-prelude.txt` (the original violations
                    template) (`_retry_prompt_outcome="validation-
                    prelude"`)
  - missing template / non-integration-sync run → copy original
                    prompt verbatim
                    (`_retry_prompt_outcome="verbatim:fallback"`)

Both prelude files are bootstrapped to consumer repos via the
resolver-tooling refresh list in
`scripts/orchestrate_poll_process.sh`. The retry-log dispatch
reads `_retry_prompt_outcome` so the log honestly reflects which
prelude (or verbatim fallback) was actually rendered.

These tests pin the contract at the source level: they assert the
files exist with the right placeholders, the dispatch branches
exist in the script, and the `_retry_prompt_outcome` values are
documented and used by the retry-log dispatch. A SOURCE-LEVEL
contract is more robust to renderer refactors than extracting the
inline python and re-running it; the existing
`test_review_conflict_resolve_smoke_deterministic.py` follows the
same pattern. Originating runs that motivated the timeout-aware
path: 25627236793 / 25627316961 (PRs
shubhodeep1/tele-funtoken-msg-scoring#2874 / #2867) on the
orchestrator/project-2840 stack, plus run 25629086684 / PR #2865.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
PROMPTS_DIR = REPO_ROOT / "prompts"
VALIDATION_PRELUDE = PROMPTS_DIR / "integration-sync-conflict-resolver-retry-prelude.txt"
TIMEOUT_PRELUDE = PROMPTS_DIR / "integration-sync-conflict-resolver-retry-timeout-prelude.txt"


def _resolve_script_text() -> str:
	return RESOLVE_SCRIPT.read_text(encoding="utf-8")


def test_both_prelude_files_exist() -> None:
	"""Both prelude templates must be checked in. They are referenced
	by `_build_retry_prompt`'s `_prelude_basename` branching and
	would render `verbatim:fallback` with a `::warning::` if
	missing — fail-open is intentional but landing in upstream
	without either file is a regression."""
	assert VALIDATION_PRELUDE.is_file(), (
		f"Validation-path prelude missing at {VALIDATION_PRELUDE}; "
		"_build_retry_prompt's `_failure_kind=validation` branch "
		"falls open to a verbatim retry with `::warning::` when "
		"this file is absent."
	)
	assert TIMEOUT_PRELUDE.is_file(), (
		f"Timeout-path prelude missing at {TIMEOUT_PRELUDE}; "
		"_build_retry_prompt's `_failure_kind=timeout` branch "
		"falls open to a verbatim retry with `::warning::` when "
		"this file is absent."
	)


def test_validation_prelude_carries_violations_framing() -> None:
	"""The validation-path prelude must keep the "produced output
	that failed post-resolve validation" framing + per-violation
	markers / fingerprint sections — that wording is the
	whole point of the prelude on that path, and the in-loop
	soft-validation reads do populate the substitution values."""
	body = VALIDATION_PRELUDE.read_text(encoding="utf-8")
	assert "produced output" in body and "failed post-resolve validation" in body, (
		"Validation prelude should describe the previous attempt's "
		"output as having failed post-resolve validation; if this "
		"framing was removed, the model gets no context for what "
		"to fix on the retry."
	)
	assert "{{MARKER_VIOLATION_COUNT}}" in body
	assert "{{MARKER_VIOLATION_FILES}}" in body
	assert "{{FINGERPRINT_VIOLATION_COUNT}}" in body
	assert "{{FINGERPRINT_VIOLATION_DETAILS}}" in body


def test_timeout_prelude_carries_apply_patch_first_guidance() -> None:
	"""The timeout-path prelude must (a) name the previous attempt
	as KILLED by the per-attempt timer with the actual seconds
	substituted, (b) tell the model to be DECISIVE rather than
	re-investigate, and (c) NOT carry the misleading "produced
	output that failed validation" framing (soft validation never
	ran on this path). The `{{PER_ATTEMPT_TIMEOUT_SECS}}`
	substitution is what makes the budget actionable in the
	model's context."""
	body = TIMEOUT_PRELUDE.read_text(encoding="utf-8")
	assert "TIMED OUT" in body or "KILLED" in body, (
		"Timeout prelude should explicitly say the previous "
		"attempt was killed/timed out; without that framing the "
		"model has no signal that the working tree is at the "
		"post-`git merge` state, not its previous edits."
	)
	assert "{{PER_ATTEMPT_TIMEOUT_SECS}}" in body, (
		"Timeout prelude should interpolate the actual per-attempt "
		"budget so the model can pace itself — a hard-coded "
		"budget would drift from CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS."
	)
	assert "apply_patch" in body, (
		"Timeout prelude should explicitly mention apply_patch — "
		"the originating failure mode was Codex consuming the "
		"full budget investigating duplicates without ever "
		"calling apply_patch."
	)
	# The misleading "produced output that failed post-resolve
	# validation" framing belongs ONLY in the validation prelude.
	# If it leaks into the timeout prelude, the model is told its
	# previous (non-existent) output was rejected — exactly the
	# bug this design was meant to fix.
	assert "failed post-resolve validation" not in body, (
		"Timeout prelude must NOT carry the validation-path's "
		"'failed post-resolve validation' framing; soft validation "
		"never ran on the timeout path so that wording is misleading."
	)


def test_build_retry_prompt_dispatches_on_failure_kind() -> None:
	"""`_build_retry_prompt` must dispatch on its 4th positional
	arg (`_failure_kind`) and select the right prelude basename
	per failure mode. Without this dispatch, every retry would
	render the validation prelude, re-introducing the misleading
	"0 violations" framing on timeout-killed retries that
	originally motivated the split (runs 25627236793 /
	25627316961 / 25629086684)."""
	src = _resolve_script_text()
	assert "_build_retry_prompt()" in src
	# The function takes _failure_kind as a positional default.
	assert 'local _failure_kind="${4:-validation}"' in src, (
		"_build_retry_prompt should default _failure_kind to "
		"'validation' (the post-soft-validation retry path) and "
		"accept 'timeout' / 'exec_error' overrides from the "
		"retry loop. If this signature changed, update both the "
		"caller at the loop top AND this test."
	)
	# Timeout branch must pick the timeout-prelude basename.
	assert (
		'_prelude_basename="integration-sync-conflict-resolver-retry-timeout-prelude.txt"'
		in src
	), (
		"Timeout branch must select the timeout-specific prelude "
		"basename so the rendered retry prompt carries the "
		"apply_patch-first guidance."
	)
	# Validation (default) branch must pick the standard prelude.
	assert (
		'_prelude_basename="integration-sync-conflict-resolver-retry-prelude.txt"'
		in src
	), (
		"Validation branch must select the standard prelude "
		"basename so the violations-framing path is preserved."
	)


def test_build_retry_prompt_sets_retry_prompt_outcome() -> None:
	"""`_retry_prompt_outcome` must be set on every code path
	through `_build_retry_prompt` so the retry-log dispatch can
	honestly describe which prelude (or verbatim fallback) was
	actually rendered. Without this, the log claims a
	timeout-aware reflexion was sent even when the function fell
	back to a verbatim copy on a consumer-repo @stable pin that
	predates the new template file."""
	src = _resolve_script_text()
	# Every documented outcome value must appear as a literal
	# assignment in the function.
	for outcome in (
		'_retry_prompt_outcome="verbatim:exec_error"',
		'_retry_prompt_outcome="verbatim:fallback"',
		'_retry_prompt_outcome="timeout-prelude"',
		'_retry_prompt_outcome="validation-prelude"',
	):
		assert outcome in src, (
			f"_build_retry_prompt must set {outcome}; the "
			"retry-log dispatch switch in the main loop reads "
			"_retry_prompt_outcome to decide which message to "
			"emit, so a missing assignment would silently log "
			"the wrong path."
		)


def test_retry_loop_reads_retry_prompt_outcome_for_log_dispatch() -> None:
	"""The retry loop's log dispatch must branch on
	`_retry_prompt_outcome`, not on `_prev_attempt_failure_kind`
	alone. The two can disagree (e.g. failure_kind=timeout but the
	prelude file is missing on a consumer-repo pin, so the
	function fell back to verbatim), and the log should reflect
	the actual prompt fed to codex, not the intent."""
	src = _resolve_script_text()
	assert 'case "${_retry_prompt_outcome}" in' in src, (
		"Retry-log dispatch should switch on _retry_prompt_outcome "
		"so the log message honestly reflects which prelude (or "
		"fallback) was rendered. Branching on _prev_attempt_failure_kind "
		"alone causes the log to claim a timeout-aware reflexion "
		"was sent on consumer-repo pins where the template was "
		"missing and the function fell back to verbatim."
	)


def test_reasoning_default_lowered_to_high() -> None:
	"""CONFLICT_RESOLVER_REASONING_EFFORT default must be `high`,
	not `xhigh`. The lowering was the C half of the response to
	the orchestrator-stack hung-thinking failure mode (runs
	25627236793 / 25627316961). `xhigh` consumed the full
	per-attempt budget enumerating duplicate helpers without
	invoking apply_patch; `high` trades some depth for finishing
	inside the budget."""
	src = _resolve_script_text()
	assert '_resolver_reasoning_effort="${CONFLICT_RESOLVER_REASONING_EFFORT:-high}"' in src, (
		"Script-side default for CONFLICT_RESOLVER_REASONING_EFFORT "
		"should be `high` (lowered from `xhigh`). If a future "
		"refactor reverts this, document the rationale and update "
		"this test together — see the comment block on review_autofix.yml's "
		"CONFLICT_RESOLVER_REASONING_EFFORT env var."
	)
	# The invalid-value fallback must also use the new default.
	assert '_resolver_reasoning_effort="high"' in src and (
		"falling back to high" in src
	), (
		"The invalid-value fallback warning + assignment should "
		"reference `high`, not the old `xhigh`. Otherwise an "
		"operator-supplied bogus value silently restores the "
		"failure-mode default."
	)


def main() -> int:
	test_both_prelude_files_exist()
	test_validation_prelude_carries_violations_framing()
	test_timeout_prelude_carries_apply_patch_first_guidance()
	test_build_retry_prompt_dispatches_on_failure_kind()
	test_build_retry_prompt_sets_retry_prompt_outcome()
	test_retry_loop_reads_retry_prompt_outcome_for_log_dispatch()
	test_reasoning_default_lowered_to_high()
	print(
		"OK: review_conflict_resolve outcome-aware retry-prelude "
		"contract holds (validation + timeout preludes, "
		"_failure_kind dispatch, _retry_prompt_outcome wiring, "
		"reasoning-default `high`)"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

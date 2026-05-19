#!/usr/bin/env python3
"""Contract tests for the editor-noop / refusal cascade guards wired across
`review_autofix.yml`, `review_apply_fixes.sh`, and the e2e poller in
`test-and-mark-stable.yml`.

The baseline no-op guard is documented in
`probably_unnecessary_but_read_if_stuck.md` §20.10, and the
refusal/cache-busting extension is documented in the same file at
§20.10.1. Three baseline
invariants must hold together — if any one regresses, the run-25126757724
cascade can re-emerge or the cross-workflow grep contract can silently
break:

1. Three steps in `review_autofix.yml` must skip when the editor never
   produced a validated commit (`env.EDITOR_NOOP_SUSPICIOUS != 'true'`):
   `Detect merge conflicts`, `Prepare merge-conflict resolver prompt and
   pre-snapshot`, and `Run Codex resolver, validate, stage, commit`.

2. `Validate editor no-op disposition` must emit the exact literal
   `::warning::Editor summary contains failure/fallback markers` that the
   e2e poller greps for. The retry's own `::notice::` line must NOT
   contain that substring (otherwise the poller's early-exit fires
   before the retry has had a chance to succeed).

3. The e2e poller's editor-noop shortcut must (a) require the
   `::warning::` prefix on its grep literal and (b) read the live log
   from a tempfile rather than from a `LOG_CONTENT=$(...)` shell capture
   (so NUL bytes in the log can't silently truncate the match).

Additional tests below cover the refusal-specific signal, cache-busting
prompt copies, and success-path cleanup of the per-attempt prompt file.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
TEST_AND_MARK_STABLE = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
REVIEW_APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
RUNBOOK = REPO_ROOT / "probably_unnecessary_but_read_if_stuck.md"

VALIDATOR_WARNING_LITERAL = "::warning::Editor summary contains failure/fallback markers"
RETRY_NOTICE_LITERAL = "::notice::Editor summary present but matched fallback-marker regex"
NOOP_SUSPICIOUS_GUARD = "env.EDITOR_NOOP_SUSPICIOUS != 'true'"

REFUSAL_SENTINEL_TEXT = "model refused (safety filter)"
REFUSAL_VALIDATOR_NOTICE = "::notice::Editor returned a safety-policy refusal"
REFUSAL_POLLER_WARNING = "::warning::Review workflow editor refused (safety filter)"

NOOP_GUARDED_STEPS = (
	"Detect merge conflicts",
	"Prepare merge-conflict resolver prompt and pre-snapshot",
	"Run Codex resolver, validate, stage, commit",
)


def _review_autofix_text() -> str:
	return REVIEW_AUTOFIX.read_text(encoding="utf-8")


def _test_and_mark_stable_text() -> str:
	return TEST_AND_MARK_STABLE.read_text(encoding="utf-8")


def _step_block(text: str, step_name: str) -> str:
	marker = f"- name: {step_name}"
	start = text.find(marker)
	assert start != -1, f"Missing workflow step: {step_name}"
	next_step = text.find("\n      - name:", start + len(marker))
	if next_step == -1:
		return text[start:]
	return text[start:next_step]


def test_merge_conflict_chain_gates_on_editor_noop_suspicious() -> None:
	"""All three merge-conflict / resolver steps must skip when
	EDITOR_NOOP_SUSPICIOUS=true. Without this gate the run-25126757724
	cascade re-emerges: editor produced no validated commit, validator
	tripped, but the resolver chain still runs and exits non-zero,
	which the e2e poller treats as the proximate failure."""
	text = _review_autofix_text()
	for step_name in NOOP_GUARDED_STEPS:
		block = _step_block(text, step_name)
		if_line = next(
			(line for line in block.splitlines() if line.lstrip().startswith("if:")),
			None,
		)
		assert if_line is not None, f"{step_name}: missing `if:` clause"
		assert NOOP_SUSPICIOUS_GUARD in if_line, (
			f"{step_name}: `if:` clause is missing the EDITOR_NOOP_SUSPICIOUS "
			f"guard ({NOOP_SUSPICIOUS_GUARD!r}). Got: {if_line.strip()!r}"
		)


def test_validator_emits_exact_grep_literal() -> None:
	"""The exact `::warning::Editor summary contains failure/fallback markers`
	literal is the public contract between `Validate editor no-op disposition`
	and the e2e poller. Renames are breaking per CLAUDE.md §6."""
	block = _step_block(_review_autofix_text(), "Validate editor no-op disposition")
	assert VALIDATOR_WARNING_LITERAL in block, (
		f"Validator step does not emit the exact warning literal "
		f"{VALIDATOR_WARNING_LITERAL!r}. The e2e poller greps for it; "
		f"any rename here is a breaking cross-workflow contract change."
	)


def test_retry_notice_does_not_collide_with_validator_literal() -> None:
	"""The in-step retry's `::notice::` MUST NOT contain the same literal
	the e2e poller greps for. If it did, a successful first-iteration
	retry (which leaves EDITOR_NOOP_SUSPICIOUS=false) would still
	trigger the poller's early-exit, producing false Phase 4b failures
	on the e2e smoke gate."""
	block = _step_block(_review_autofix_text(), "Apply fixes with editor model")
	assert RETRY_NOTICE_LITERAL in block, (
		"In-step retry's expected `::notice::` text was not found. The "
		"retry path needs a notice line on fallback-marker detection."
	)
	# Find every line in the retry block and assert none of them carry
	# the validator's `::warning::` literal.
	offending = [
		line for line in block.splitlines()
		if VALIDATOR_WARNING_LITERAL in line
	]
	assert not offending, (
		f"`Apply fixes with editor model` contains lines that match the "
		f"validator's grep literal {VALIDATOR_WARNING_LITERAL!r}: "
		f"{offending!r}. The poller's early-exit would false-trigger "
		f"on these lines before the retry has had a chance to succeed."
	)


def test_e2e_poller_grep_includes_warning_prefix() -> None:
	"""The poller must require the `::warning::` workflow-command prefix
	on its grep literal so it only matches the validator's annotation,
	not any other log line that mentions the phrase. The `-a` flag is
	required so a NUL byte in the streamed log doesn't make grep
	short-circuit to "Binary file matches" and skip the literal match."""
	text = _test_and_mark_stable_text()
	expected_grep = f"grep -qaF '{VALIDATOR_WARNING_LITERAL}'"
	assert expected_grep in text, (
		f"e2e poller is missing the editor-noop shortcut grep "
		f"{expected_grep!r}. The `::warning::` prefix and the `-a` flag "
		f"are both load-bearing — the prefix prevents the retry's "
		f"`::notice::` from matching, and `-a` keeps the grep working "
		f"when the streamed log contains NUL bytes."
	)


def test_e2e_poller_uses_tempfile_not_variable_capture() -> None:
	"""The poller must stream the job-log API response to a tempfile
	rather than capture it into a `LOG_CONTENT=$(...)` shell variable.
	Bash command substitution silently drops NUL bytes from the captured
	output — and inner tools (codex, jq, gh) can leak binary content
	into job logs. A dropped NUL could merge adjacent log lines and
	cause both the editor-noop grep and the reviewer-success counter
	to silently miss otherwise-valid matches."""
	text = _test_and_mark_stable_text()
	assert 'LOG_FILE="$(mktemp)"' in text, (
		"e2e poller is missing the tempfile-backed log fetch "
		"(`LOG_FILE=\"$(mktemp)\"`)."
	)
	# The pre-PR-1798 capture pattern must NOT be present in the wait
	# block any more — it would re-introduce the NUL-byte truncation.
	assert (
		'LOG_CONTENT=$(gh_api_safe "repos/${TEST_REPO}/actions/jobs/'
		not in text
	), (
		"e2e poller still captures the job log into a `LOG_CONTENT=$(...)` "
		"shell variable. Bash command substitution drops NUL bytes, "
		"which can silently break the editor-noop grep and the "
		"reviewer-success counter on logs containing binary content."
	)


def _review_apply_fixes_text() -> str:
	return REVIEW_APPLY_FIXES.read_text(encoding="utf-8")


def _runbook_text() -> str:
	return RUNBOOK.read_text(encoding="utf-8")


def test_validator_sets_editor_noop_refusal_alongside_suspicious() -> None:
	"""The validator must set `EDITOR_NOOP_REFUSAL` additively alongside
	`EDITOR_NOOP_SUSPICIOUS` (CLAUDE.md §6 — never rename, add alongside).
	Both env vars must be exported via GITHUB_ENV so downstream steps can
	gate on either signal independently."""
	block = _step_block(_review_autofix_text(), "Validate editor no-op disposition")
	assert 'EDITOR_NOOP_REFUSAL="false"' in block, (
		"Validator must initialize EDITOR_NOOP_REFUSAL=false at the top of "
		"the block, mirroring EDITOR_NOOP_SUSPICIOUS's initial assignment."
	)
	assert 'echo "EDITOR_NOOP_REFUSAL=${EDITOR_NOOP_REFUSAL}" >> "$GITHUB_ENV"' in block, (
		"Validator must export EDITOR_NOOP_REFUSAL via GITHUB_ENV alongside "
		"EDITOR_NOOP_SUSPICIOUS so downstream steps can read it."
	)
	assert 'echo "EDITOR_NOOP_SUSPICIOUS=${EDITOR_NOOP_SUSPICIOUS}" >> "$GITHUB_ENV"' in block, (
		"Existing EDITOR_NOOP_SUSPICIOUS export must remain — additive change."
	)


def test_validator_greps_for_refusal_sentinel() -> None:
	"""The validator's refusal-specific check must grep for the exact
	`model refused (safety filter)` sentinel that `review_apply_fixes.sh`
	writes into the fallback summary's `Runtime failure path:` line.
	Without the verbatim match the refusal alert is silently dropped."""
	block = _step_block(_review_autofix_text(), "Validate editor no-op disposition")
	assert "grep -qiE 'model refused \\(safety filter\\)'" in block, (
		"Validator is missing the refusal-sentinel grep "
		"(`grep -qiE 'model refused \\(safety filter\\)'`). The sentinel "
		"text must match what `review_apply_fixes.sh` writes verbatim."
	)
	assert REFUSAL_VALIDATOR_NOTICE in block, (
		f"Validator must emit the {REFUSAL_VALIDATOR_NOTICE!r} annotation "
		f"when it detects a refusal — the e2e poller in test-and-mark-"
		f"stable.yml greps for this exact text to pick the refusal-aware "
		f"warning branch."
	)


def test_validator_refusal_notice_does_not_collide_with_warning_literal() -> None:
	"""The new refusal `::notice::` must not contain the validator's
	`::warning::Editor summary contains failure/fallback markers` literal
	(otherwise the poller's early-exit grep would fire on the notice line
	instead of the warning line, with the wrong branch consequence)."""
	block = _step_block(_review_autofix_text(), "Validate editor no-op disposition")
	for line in block.splitlines():
		if REFUSAL_VALIDATOR_NOTICE in line:
			assert VALIDATOR_WARNING_LITERAL not in line, (
				f"Refusal notice line collides with validator warning "
				f"literal: {line.strip()!r}. The poller relies on "
				f"unambiguous prefix matching."
			)


def test_e2e_poller_has_refusal_aware_branch() -> None:
	"""When the validator's refusal `::notice::` is present in the live
	log, the poller must emit a refusal-specific `::warning::` so the
	Telegram alert can distinguish 'model refused — re-run' from generic
	'no-op suspicious'. The generic-noop warning literal stays unchanged
	for the non-refusal branch (CLAUDE.md §6)."""
	text = _test_and_mark_stable_text()
	# Refusal-aware grep on the validator's notice.
	expected_refusal_grep = f"grep -qaF '{REFUSAL_VALIDATOR_NOTICE}'"
	assert expected_refusal_grep in text, (
		f"e2e poller is missing the refusal-aware grep "
		f"{expected_refusal_grep!r}. Without it the refusal-specific "
		f"warning branch can never fire and the Telegram alert stays "
		f"generic."
	)
	# Refusal-specific warning literal.
	assert REFUSAL_POLLER_WARNING in text, (
		f"e2e poller must emit a warning that begins with "
		f"{REFUSAL_POLLER_WARNING!r} when the refusal notice is detected."
	)
	# Generic-noop warning literal preserved.
	generic_warning = (
		"::warning::Review workflow editor produced no validated summary "
		"(EDITOR_NOOP_SUSPICIOUS marker observed); no autofix commit will be pushed"
	)
	assert generic_warning in text, (
		"Generic-noop warning literal must be preserved for non-refusal "
		"cases (CLAUDE.md §6 — additive)."
	)


def test_review_apply_fixes_has_per_attempt_cache_busting_nonce() -> None:
	"""Each editor attempt must feed codex a byte-distinct prompt so
	provider-side prompt-hash caching cannot serve a previous attempt's
	response (refusal or otherwise) instantly to retries. PR #3053 /
	run 26081926521 burned 4 attempts at 0 tokens each on a cached
	refusal; the nonce closes that loop."""
	text = _review_apply_fixes_text()
	assert "attempt_prompt_file=\"${EDITOR_PROMPT_FILE}.attempt_${attempt}\"" in text, (
		"Per-attempt prompt file (`${EDITOR_PROMPT_FILE}.attempt_${attempt}`) "
		"is missing — without it every retry sends the same prompt bytes "
		"and a cached refusal is served instantly to all attempts."
	)
	assert "retry_attempt=%d epoch=%s nonce=%s" in text, (
		"Cache-busting nonce trailer (`retry_attempt=… epoch=… nonce=…`) "
		"is missing from the per-attempt prompt build. The trailer is the "
		"actual cache-buster; without it the per-attempt copy is byte-"
		"identical and the cache still hits."
	)
	# codex must read the attempt-specific file, not the base prompt.
	assert 'codex --ask-for-approval never' in text
	assert '< "${attempt_prompt_file}"' in text, (
		"codex stdin must be fed from the per-attempt prompt file, not "
		"from the unchanging `${EDITOR_PROMPT_FILE}`."
	)


def test_review_apply_fixes_cleans_attempt_prompt_file_on_success() -> None:
	"""A validated editor success exits directly from inside the retry
	loop, so the per-attempt prompt copy must be removed before that
	early exit rather than relying on the common cleanup tail."""
	text = _review_apply_fixes_text()
	success_start = text.find('mv "${tmp_output}" "${EDITOR_SUMMARY_FILE}"')
	assert success_start != -1, "Success-path summary move not found in review_apply_fixes.sh"
	success_end = text.find('echo "Editor succeeded on attempt ${attempt}."', success_start)
	assert success_end != -1, "Success-path exit log not found in review_apply_fixes.sh"
	success_block = text[success_start:success_end]
	assert 'rm -f "${attempt_prompt_file}"' in success_block, (
		"Success path must remove `${attempt_prompt_file}` before exiting; "
		"the loop's common cleanup tail is skipped on validated success."
	)


def test_review_apply_fixes_breaks_retry_loop_on_safety_refusal() -> None:
	"""When the editor returns an OpenAI-style safety refusal, the
	retry loop must touch the refusal flag and `break` rather than
	consume the remaining attempts. Even with cache-busting, very sticky
	provider-side filter trips can repeat the refusal — and the
	fallback writer needs the flag to label the failure correctly."""
	text = _review_apply_fixes_text()
	assert "I'?m sorry,? but I (can ?not|can.?t) assist" in text, (
		"Refusal-detection regex is missing the OpenAI-style "
		"`I'm sorry, but I cannot assist` pattern."
	)
	assert 'touch "${PREVIOUS_REVIEWS_DIR}/editor_refused.flag"' in text, (
		"Refusal short-circuit must touch the `editor_refused.flag` file "
		"so the fallback summary writer can emit the refusal-specific "
		"Runtime failure path line."
	)
	refusal_touch = text.find('touch "${PREVIOUS_REVIEWS_DIR}/editor_refused.flag"')
	refusal_block_end = text.find("\n  fi", refusal_touch)
	assert refusal_block_end != -1, "Refusal short-circuit closing `fi` not found."
	refusal_block = text[refusal_touch:refusal_block_end]
	assert any(line.strip() == "break" for line in refusal_block.splitlines()), (
		"Refusal short-circuit must include a `break` after touching the flag "
		"so sticky refusals do not consume the remaining retry budget."
	)


def test_review_apply_fixes_fallback_distinguishes_refusal() -> None:
	"""The fallback summary writer must emit a distinct
	`Runtime failure path: - model refused (safety filter)` line when
	the refusal flag is present, while preserving the other markers
	(`editor failed before producing`, `unavailable (editor fallback)`)
	verbatim — lockstep with the validator's Check 1 and the in-step
	retry's `_instep_retry_summary_unusable` flag MUST be maintained."""
	text = _review_apply_fixes_text()
	assert '_runtime_failure_path_line="- model refused (safety filter)"' in text, (
		"Fallback writer must set the refusal-specific Runtime failure "
		"path line when `editor_refused.flag` exists."
	)
	assert '_runtime_failure_path_line="- unavailable (editor fallback)"' in text, (
		"Non-refusal fallback path must keep the original "
		"`unavailable (editor fallback)` Runtime failure path text — "
		"removal would break lockstep with the validator and in-step retry."
	)
	# The lockstep markers must remain in the heredoc body verbatim.
	assert "- none (editor failed before producing a validated summary)" in text, (
		"Lockstep marker `editor failed before producing a validated "
		"summary` removed from fallback — would break the validator's "
		"Check 1 grep and the in-step retry's `_instep_retry_summary_unusable` "
		"detection (see probably_unnecessary_but_read_if_stuck.md §20.10)."
	)


def test_review_apply_fixes_centralizes_refusal_regex() -> None:
	"""The refusal regex should be defined once in the shell script and
	reused for both the structured-output validator and the retry-loop
	short-circuit so the two checks cannot drift."""
	text = _review_apply_fixes_text()
	expected_regex = "_REFUSAL_REGEX=\"I'?m sorry,? but I (can ?not|can.?t) assist|I (can ?not|can.?t) help with that\""
	assert expected_regex in text, (
		"review_apply_fixes.sh must define `_REFUSAL_REGEX` exactly once so "
		"the validator and short-circuit share the same refusal pattern."
	)
	assert text.count('${_REFUSAL_REGEX}') == 2, (
		"review_apply_fixes.sh must reuse `${_REFUSAL_REGEX}` in both refusal "
		"checks (structured-output validation and retry short-circuit)."
	)


def test_refusal_contract_literals_stay_in_lockstep_across_files() -> None:
	"""The refusal sentinel/notice/warning strings are duplicated across
	the shell script, workflows, and runbook by design; enforce lockstep
	so wording drift fails fast in tests instead of silently disabling the
	refusal-specific alert chain."""
	for path_label, text in (
		("review_apply_fixes.sh", _review_apply_fixes_text()),
		("review_autofix.yml", _review_autofix_text()),
		("probably_unnecessary_but_read_if_stuck.md", _runbook_text()),
	):
		assert REFUSAL_SENTINEL_TEXT in text, (
			f"{path_label} must contain the refusal sentinel {REFUSAL_SENTINEL_TEXT!r} "
			"so the cross-workflow refusal contract stays in sync."
		)
	for path_label, text in (
		("review_autofix.yml", _review_autofix_text()),
		("test-and-mark-stable.yml", _test_and_mark_stable_text()),
		("probably_unnecessary_but_read_if_stuck.md", _runbook_text()),
	):
		assert REFUSAL_VALIDATOR_NOTICE in text, (
			f"{path_label} must contain {REFUSAL_VALIDATOR_NOTICE!r} so the "
			"validator notice and poller/docs stay in lockstep."
		)
	for path_label, text in (
		("test-and-mark-stable.yml", _test_and_mark_stable_text()),
		("probably_unnecessary_but_read_if_stuck.md", _runbook_text()),
	):
		assert REFUSAL_POLLER_WARNING in text, (
			f"{path_label} must contain {REFUSAL_POLLER_WARNING!r} so the "
			"refusal-specific operator warning stays in lockstep."
		)


if __name__ == "__main__":
	test_merge_conflict_chain_gates_on_editor_noop_suspicious()
	test_validator_emits_exact_grep_literal()
	test_retry_notice_does_not_collide_with_validator_literal()
	test_e2e_poller_grep_includes_warning_prefix()
	test_e2e_poller_uses_tempfile_not_variable_capture()
	test_validator_sets_editor_noop_refusal_alongside_suspicious()
	test_validator_greps_for_refusal_sentinel()
	test_validator_refusal_notice_does_not_collide_with_warning_literal()
	test_e2e_poller_has_refusal_aware_branch()
	test_review_apply_fixes_has_per_attempt_cache_busting_nonce()
	test_review_apply_fixes_cleans_attempt_prompt_file_on_success()
	test_review_apply_fixes_breaks_retry_loop_on_safety_refusal()
	test_review_apply_fixes_fallback_distinguishes_refusal()
	test_review_apply_fixes_centralizes_refusal_regex()
	test_refusal_contract_literals_stay_in_lockstep_across_files()
	print("All EDITOR_NOOP_SUSPICIOUS cascade-guard contract tests passed.")

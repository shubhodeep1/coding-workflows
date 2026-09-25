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

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
STAGE_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
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
	# OpenCode must read the per-attempt file (which carries the nonce trailer),
	# never the unchanging base prompt. The stdin redirect may be inline
	# (`< "${attempt_prompt_file}"`) or inside a one-arg helper that the call
	# site feeds `${attempt_prompt_file}` into — the S4 continuation-thread-
	# reuse change extracted `run_editor_codex_attempt` for exactly this.
	# Assert the behaviour, not one code shape, so a cache-buster-preserving
	# refactor does not trip this guard while a regression to the base prompt
	# still does.
	assert 'opencode_run_cmd "$@"' in text


	def _shell_function_blocks(script_text: str) -> dict[str, str]:
		function_blocks: dict[str, str] = {}
		current_function: str | None = None
		pending_function: str | None = None
		pending_lines: list[str] = []
		current_lines: list[str] = []
		brace_depth = 0
		for line in script_text.splitlines():
			if current_function is None:
				stripped = line.strip()
				if pending_function is not None:
					if re.match(r'^\{\s*(?:#.*)?$', stripped):
						current_function = pending_function
						current_lines = pending_lines + [line]
						brace_depth = 1
						pending_function = None
						pending_lines = []
						continue
					pending_function = None
					pending_lines = []
				function_start = re.match(r'^\s*(?!#)(\w+)\s*\(\)\s*\{\s*(?:#.*)?$', line)
				if function_start is not None:
					current_function = function_start.group(1)
					current_lines = [line]
					brace_depth = 1
					continue
				function_declaration = re.match(r'^\s*(?!#)(\w+)\s*\(\)\s*(?:#.*)?$', line)
				if function_declaration is not None:
					pending_function = function_declaration.group(1)
					pending_lines = [line]
				continue
			current_lines.append(line)
			stripped = line.strip()
			if re.match(r'^\{\s*(?:#.*)?$', stripped):
				brace_depth += 1
				continue
			if re.match(r'^\}(?=\s*(?:$|#|[;|&<>]|\d))', stripped):
				brace_depth -= 1
				if brace_depth == 0:
					function_blocks[current_function] = "\n".join(current_lines)
					current_function = None
					current_lines = []
		return function_blocks

	def _codex_stdin_targets(script_text: str) -> list[tuple[str, str | None]]:
		targets: list[tuple[str, str | None]] = []
		current_function: str | None = None
		pending_function: str | None = None
		brace_depth = 0
		for line in script_text.splitlines():
			started_function = False
			if current_function is None:
				stripped = line.strip()
				if pending_function is not None:
					if re.match(r'^\{\s*(?:#.*)?$', stripped):
						current_function = pending_function
						brace_depth = 1
						started_function = True
					pending_function = None
				if not started_function:
					function_start = re.match(r'^\s*(?!#)(\w+)\s*\(\)\s*\{\s*(?:#.*)?$', line)
					if function_start is not None:
						current_function = function_start.group(1)
						brace_depth = 1
						started_function = True
					else:
						function_declaration = re.match(r'^\s*(?!#)(\w+)\s*\(\)\s*(?:#.*)?$', line)
						if function_declaration is not None:
							pending_function = function_declaration.group(1)
			if not re.match(r'^\s*#', line):
				for operand in re.findall(
					r'"[$]\{editor_opencode_cmd\[@\]\}"[^\n]*<\s*"?([$](?:\{?\w+\}?))"?',
					line,
				):
					targets.append((
						operand.strip('"').lstrip("$").strip("{}"),
						current_function,
					))
			if current_function is None or started_function:
				continue
			stripped = line.strip()
			if re.match(r'^\{\s*(?:#.*)?$', stripped):
				brace_depth += 1
			elif re.match(r'^\}(?=\s*(?:$|#|[;|&<>]|\d))', stripped):
				brace_depth -= 1
				if brace_depth == 0:
					current_function = None
		return targets

	function_blocks = _shell_function_blocks(text)
	codex_stdin_targets = _codex_stdin_targets(text)
	assert codex_stdin_targets, (
			"No OpenCode editor command with a `< \"$file\"` stdin "
		"redirect "
			"redirect found; the editor must feed OpenCode a prompt file on stdin."
	)
	assert all(
		stdin_var != "EDITOR_PROMPT_FILE"
		for stdin_var, _helper_name in codex_stdin_targets
	), (
			"OpenCode stdin is the unchanging `${EDITOR_PROMPT_FILE}` — every retry "
		"sends identical bytes and a cached refusal is served instantly "
		"(PR #3053 / run 26081926521). Feed `${attempt_prompt_file}` instead."
	)
	unsafe_targets = []
	for _stdin_var, _helper_name in codex_stdin_targets:
		if _helper_name is None:
			if _stdin_var == "attempt_prompt_file":
				continue
			unsafe_targets.append(f"script:${{{_stdin_var}}}")
			continue
		helper_body = function_blocks.get(_helper_name, "")
		helper_call_prefix = (
			rf'(?m)^(?!\s*#)\s*'
			r"(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"\n]*\"|'[^'\n]*'|[^ \t\n]+)\s+)*"
			rf'{re.escape(_helper_name)}\s+'
		)
		first_arg_reaches_stdin = (
			_stdin_var == "1"
			or re.search(
				rf'(?m)^(?!\s*#)\s*(?:local(?:\s+-[A-Za-z]+)*\s+)?{re.escape(_stdin_var)}='
				r'"?[$]\{?1\}?"?',
				helper_body,
			)
			is not None
		)
		call_forwards_attempt_prompt = (
			re.search(
				helper_call_prefix + r'"?[$]' + r'\{?attempt_prompt_file\}?"?(?:\s|$)',
				text,
			)
			is not None
		)
		call_forwards_base_prompt = (
			re.search(
				helper_call_prefix + r'"?[$]' + r'\{?EDITOR_PROMPT_FILE\}?"?(?:\s|$)',
				text,
			)
			is not None
		)
		if first_arg_reaches_stdin and call_forwards_attempt_prompt and not call_forwards_base_prompt:
			continue
		unsafe_reasons = []
		if not first_arg_reaches_stdin:
			unsafe_reasons.append("stdin is not helper arg1")
		if not call_forwards_attempt_prompt:
			unsafe_reasons.append("no helper call forwards attempt_prompt_file")
		if call_forwards_base_prompt:
			unsafe_reasons.append("helper is called with EDITOR_PROMPT_FILE")
		unsafe_targets.append(
			f"{_helper_name}:${{{_stdin_var}}} ({'; '.join(unsafe_reasons)})"
		)
	assert not unsafe_targets, (
		"Each codex stdin redirect must either read `${attempt_prompt_file}` "
		"directly from the script body or live in a helper whose first "
		"argument reaches codex and whose call site forwards "
		"`${attempt_prompt_file}` — never `${EDITOR_PROMPT_FILE}`. "
		f"Unsafe targets: {', '.join(unsafe_targets)}"
	)


def test_review_apply_fixes_cleans_attempt_prompt_file_on_success() -> None:
	"""A validated editor success exits directly from inside the retry
	loop, so the per-attempt prompt copy must be removed before that
	early exit rather than relying on the common cleanup tail."""
	text = _review_apply_fixes_text()
	assert 'attempt_prompt_file_cleanup_path="${attempt_prompt_file}"' in text, (
		"Cleanup must stay pinned to the generated per-attempt prompt path "
		"even when execution falls back to `${EDITOR_PROMPT_FILE}`."
	)
	success_start = text.find('mv "${tmp_output}" "${EDITOR_SUMMARY_FILE}"')
	assert success_start != -1, "Success-path summary move not found in review_apply_fixes.sh"
	success_end = text.find('echo "Editor succeeded on attempt ${attempt}."', success_start)
	assert success_end != -1, "Success-path exit log not found in review_apply_fixes.sh"
	success_block = text[success_start:success_end]
	assert 'rm -f "${attempt_prompt_file_cleanup_path}"' in success_block, (
		"Success path must remove the generated per-attempt prompt copy before "
		"exiting; the loop's common cleanup tail is skipped on validated success."
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
	"""The fallback summary writer must keep the refusal-specific
	partial-finalize summary distinct from the soft-deadline and generic
	recoverable-failure summaries so downstream refusal detection stays
	anchored to the dedicated heredoc branch."""
	text = _review_apply_fixes_text()
	assert 'elif [ "${editor_partial_finalize_reason}" = "refusal" ]; then' in text, (
		"Fallback writer must keep a dedicated refusal branch so the "
		"refusal-specific summary cannot drift into the generic fallback paths."
	)
	assert "- none (editor deferred because the run budget was exhausted before another validated attempt could start)" in text, (
		"Soft-deadline partial-finalize summary must remain distinct from the "
		"refusal and recoverable-failure summaries."
	)
	assert "- none (editor returned a safety-policy refusal before another validated attempt could complete)" in text, (
		"Refusal partial-finalize summary must remain distinct so downstream "
		"refusal detection keeps the dedicated sentinel path."
	)
	assert "- none (editor requested partial finalize after a recoverable failure before another validated attempt completed)" in text, (
		"Generic recoverable-failure partial-finalize summary must remain "
		"distinct from the refusal branch."
	)
	assert "Runtime failure path:\n- partial finalize requested at the soft deadline before another editor attempt" in text, (
		"Soft-deadline fallback must keep its dedicated Runtime failure path."
	)
	assert "Runtime failure path:\n- model refused (safety filter)" in text, (
		"Refusal fallback must keep the refusal-specific Runtime failure path "
		"line when `editor_refused.flag` exists."
	)
	assert "Runtime failure path:\n- partial finalize requested after a recoverable editor failure before another validated attempt" in text, (
		"Recoverable-failure fallback must keep its dedicated Runtime failure "
		"path instead of collapsing into the refusal branch."
	)


def test_review_apply_fixes_centralizes_refusal_regex() -> None:
	"""The refusal regex should be defined once in the shell script and
	reused for both the structured-output validator and the retry-loop
	short-circuit so the two checks cannot drift. The pattern should stay
	line-anchored so incidental prose inside a valid structured summary does
	not look like a provider refusal."""
	text = _review_apply_fixes_text()
	assert text.count('_REFUSAL_REGEX=') == 1, (
		"review_apply_fixes.sh must define `_REFUSAL_REGEX` exactly once so "
		"the validator and short-circuit share the same refusal pattern."
	)
	assert '_REFUSAL_REGEX="(' in text, (
		"review_apply_fixes.sh must define `_REFUSAL_REGEX` exactly once so "
		"the validator and short-circuit share the same refusal pattern."
	)
	assert "^I'?m sorry,? but I (can ?not|can.?t) assist" in text, (
		"The refusal regex must still match OpenAI-style `I'm sorry, but I "
		"cannot assist` lines."
	)
	assert "^I (can ?not|can.?t) help with that( request)?\\\\.?$" in text, (
		"The refusal regex should stay line-anchored so incidental prose in a "
		"valid editor summary does not trip the refusal path."
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


# ---------------------------------------------------------------
# Editor-prompt false-claim-convergence guards (Fix C of the noop-
# suspicious recovery PR).
#
# The prompt was tightened to teach the editor model that convergence
# (every reviewer comment is already-satisfied or correctly ignored) is
# a first-class valid outcome. Without this language the model emits a
# "Changes made: …" narrative on every attempt even when the worktree
# is clean, which the downstream validator at
# `scripts/review_apply_fixes.sh:1453` then rejects — triggering the
# very EDITOR_NOOP_SUSPICIOUS path Fix B exists to recover from.
#
# The grep-based contracts below pin the four pieces of language the
# task spec mandates, plus a regression guard that the existing
# section headings the validators downstream depend on were NOT
# renamed (CLAUDE.md §6: naming immutability).
# ---------------------------------------------------------------


EDITOR_PROMPT_HEREDOC_OPEN = "cat <<__EDITOR_PROMPT__"
EDITOR_PROMPT_HEREDOC_CLOSE = "__EDITOR_PROMPT__"

# Existing heading literals downstream tools grep for. Renaming any of
# these is forbidden by CLAUDE.md §6 (naming immutability) — both
# scripts/review_apply_fixes.sh (validator block at ~1390-1480) and
# scripts/review_commit_changes.sh grep for these exact strings.
PROTECTED_PROMPT_HEADINGS = (
	"Changes made:",
	"Change status:",
	"Already satisfied (suggested but already present):",
	"Ignored suggestions (with short reason):",
	"Reviewer files processed:",
	"Review file issue audit:",
	"PR comment audit:",
	"Regression fingerprint:",
	"Runtime failure path:",
)


def _editor_prompt_heredoc() -> str:
	"""Extract the slice of review_apply_fixes.sh between the editor
	prompt's `cat <<__EDITOR_PROMPT__` opener and its closer. Pinning
	the contract tests to this slice prevents accidental cross-talk
	with unrelated code (e.g. the validator block that also mentions
	these heading literals)."""
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	open_idx = text.find(EDITOR_PROMPT_HEREDOC_OPEN)
	assert open_idx != -1, f"Editor prompt heredoc opener {EDITOR_PROMPT_HEREDOC_OPEN!r} not found"
	# The closer is the literal line `__EDITOR_PROMPT__` AFTER the opener.
	close_idx = text.find("\n" + EDITOR_PROMPT_HEREDOC_CLOSE, open_idx + len(EDITOR_PROMPT_HEREDOC_OPEN))
	assert close_idx != -1, f"Editor prompt heredoc closer not found after opener"
	return text[open_idx:close_idx]


def test_editor_prompt_documents_convergence_outcome() -> None:
	"""The prompt must explicitly tell the model that a converged
	run (every reviewer comment is already-satisfied / ignored, no
	apply_patch calls performed) is a valid SUCCESS outcome with the
	`- none` / `- not-edited` shape — not a failure to be papered
	over with a fabricated narrative."""
	prompt = _editor_prompt_heredoc()
	assert "Convergence is a first-class valid outcome" in prompt, (
		"Prompt must contain the convergence-is-valid header line so "
		"the model recognizes the converged path as a SUCCESS shape."
	)
	# Must reference the exact two-bullet contract the validator
	# accepts.
	assert "- none" in prompt
	assert "- not-edited" in prompt


def test_editor_prompt_states_audit_arithmetic_invariant() -> None:
	"""The prompt must state the exact arithmetic invariant that
	scripts/validate_editor_audit.sh enforces (total == applied +
	already_applied + ignored), and must tell the model that a review
	file listing zero issues gets all four counts as 0 — confirmations
	of prior fixes are narrative for the "Already satisfied" section,
	not audit counts. Without this, a genuine convergence run can emit
	`total 0 / already applied 1`, trip the validator, and block
	auto-merge as a false-positive EDITOR_NOOP_SUSPICIOUS (observed on
	tele-funtoken-msg-scoring run 33088357425, PR 3809)."""
	prompt = _editor_prompt_heredoc()
	assert (
		"total issues listed == issues applied + issues already applied + issues ignored"
		in prompt
	), (
		"Prompt must spell out the audit arithmetic invariant enforced "
		"by scripts/validate_editor_audit.sh."
	)
	assert "emit all four counts as 0" in prompt, (
		"Prompt must define the zero-issue convergence accounting: a "
		"review file listing zero issues gets all four counts as 0."
	)
	# The citation keeps prompt and validator discoverable from each
	# other so future edits keep the two in lockstep.
	assert "validate_editor_audit.sh" in prompt, (
		"Prompt must cite the downstream validator so the contract's "
		"two halves reference each other."
	)


def test_editor_prompt_requires_changes_match_this_run_writes() -> None:
	"""Each bullet under `Changes made:` must correspond to an
	apply_patch / write the model actually performed THIS run — not
	to edits already on HEAD that the model considered."""
	prompt = _editor_prompt_heredoc()
	assert "Claimed edits MUST correspond to writes you actually performed in" in prompt
	assert "THIS run" in prompt
	# The hard rule must reference apply_patch (the editor's primary
	# write tool) so the model can't argue it via a different
	# write-tool reading.
	assert "apply_patch" in prompt
	# The downstream validator citation makes the failure mode
	# concrete: the model can't talk itself out of the rule.
	assert "git diff HEAD" in prompt


def test_editor_prompt_forbids_fabricated_edits() -> None:
	"""The prompt must explicitly forbid whitespace-only / no-op
	edits added purely to make the structured-format check pass —
	the failure mode the validator catches via the "no substantive
	diff from HEAD" branch."""
	prompt = _editor_prompt_heredoc()
	assert "No fabricated edits to satisfy the format" in prompt
	# Concrete forbidden examples — whitespace-only, no-op rename,
	# empty-line shuffle — make the rule unmistakable.
	assert "whitespace-only" in prompt


def test_editor_prompt_requires_self_check_before_emitting() -> None:
	"""Before emitting the final response, the model must mentally
	walk through git status / git diff HEAD and reconcile its
	"Changes made:" bullets against the actual diff. A clean worktree
	must emit the two-bullet converged shape; a non-empty worktree
	must list only files visible in the diff."""
	prompt = _editor_prompt_heredoc()
	assert "Self-check before emitting" in prompt
	# Both git tools the self-check inspects must be named — the
	# model is more likely to actually perform the check when the
	# commands are explicit.
	assert "git status" in prompt
	assert "git diff HEAD" in prompt


def test_editor_prompt_section_headings_unchanged() -> None:
	"""CLAUDE.md §6 (naming immutability): the existing section
	heading literals MUST appear in the prompt exactly as before.
	scripts/review_apply_fixes.sh's validator block at lines
	1390-1480 and scripts/review_commit_changes.sh grep for these
	literals — any rename here silently breaks both."""
	prompt = _editor_prompt_heredoc()
	for heading in PROTECTED_PROMPT_HEADINGS:
		assert heading in prompt, (
			f"Protected prompt heading {heading!r} is no longer present "
			f"in the editor prompt heredoc. Renames here are forbidden "
			f"by CLAUDE.md §6 and silently break the downstream "
			f"validators that grep for the literal."
		)


# ---------------------------------------------------------------
# Shared audit-helper integration (Fix B of the same PR).
#
# The "Reviewer audit sanity" arithmetic check (Check 2 of the
# "Validate editor no-op disposition" step) was extracted from
# `.github/workflows/review_autofix.yml` into
# `scripts/validate_editor_audit.sh` so the orchestrator-poll
# noop-suspicious recovery sweep can call the exact same logic when
# deciding whether a force-merge fallback is safe. Drift between the
# two callers is precisely the bug we are avoiding — these tests pin
# the integration so the inline block can never be re-introduced.
# ---------------------------------------------------------------


def test_workflow_sources_shared_audit_helper() -> None:
	"""The validator step must `source` scripts/validate_editor_audit.sh
	and call validate_editor_audit_arithmetic — never re-implement the
	arithmetic check inline."""
	text = _review_autofix_text()
	disposition = _step_block(text, "Validate editor no-op disposition")
	assert "validate_editor_audit.sh" in disposition, (
		"Validate editor no-op disposition step must source the shared "
		"audit helper."
	)
	assert "validate_editor_audit_arithmetic" in disposition, (
		"Workflow must call validate_editor_audit_arithmetic (the "
		"shared function) — never re-implement the check inline."
	)


def test_workflow_no_longer_contains_inline_audit_arithmetic_regex() -> None:
	"""The inline `total issues listed` / `issues applied` / `issues
	already applied` / `issues ignored` regex block must have moved
	OUT of the workflow and INTO the shared helper. Catches a
	regression where someone re-inlines the block under the same
	step but forgets to delete the helper-source call."""
	text = _review_autofix_text()
	disposition = _step_block(text, "Validate editor no-op disposition")
	# The unique-to-the-arithmetic-block regex fragments must NOT
	# appear inside the workflow step any more.
	forbidden_in_workflow = [
		"total issues listed[^0-9]*\\K",
		"(?<!already )issues applied[^0-9]*\\K",
		"issues already applied[^0-9]*\\K",
		"issues ignored[^0-9]*\\K",
	]
	for needle in forbidden_in_workflow:
		assert needle not in disposition, (
			f"Inline audit-arithmetic regex {needle!r} re-appeared in "
			f"the workflow step. It must live ONLY in "
			f"scripts/validate_editor_audit.sh so the workflow and the "
			f"poller cannot drift."
		)
	# And the helper must still own the arithmetic parser. The
	# implementation is allowed to evolve away from the original grep -P
	# fragments as long as the parsing logic remains centralized there.
	helper_text = (REPO_ROOT / "scripts" / "validate_editor_audit.sh").read_text(encoding="utf-8")
	assert (
		"total issues listed[^0-9]*\\K" in helper_text
		or "total[[:space:]]+issues[[:space:]]+listed" in helper_text
	), (
		"The shared helper must still own the audit-arithmetic parser; "
		"if neither the legacy grep -P fragment nor the bash-regex "
		"replacement is present, the parser likely moved elsewhere."
	)
	assert "Audit entry arithmetic mismatch" in helper_text, (
		"The shared helper must still emit the arithmetic-mismatch warning; "
		"if that literal moved elsewhere, the contract is broken."
	)


def test_workflow_warning_literals_preserved_in_helper() -> None:
	"""The validator step previously emitted two distinctive
	`::warning::` literals that downstream operator searches and the
	e2e poller grep contracts depend on. The shared helper MUST emit
	the same literals."""
	helper_text = (REPO_ROOT / "scripts" / "validate_editor_audit.sh").read_text(encoding="utf-8")
	# The empty-audit warning has the form
	# "::warning::N reviewer(s) succeeded but editor audit section is empty or contains only fallback text."
	# (lowercase `editor` because it appears mid-sentence). The poller
	# path also emits a sentence-start variant that starts uppercase.
	assert "reviewer(s) succeeded but editor audit section is empty or contains only fallback text" in helper_text
	# Per-line arithmetic mismatch literal stays byte-for-byte:
	assert "Audit entry arithmetic mismatch: total=" in helper_text


def test_required_bootstrap_scripts_includes_audit_helper() -> None:
	"""For consumer repos, the workflow stages scripts into
	${SUPPORT_SCRIPTS_DIR} via REQUIRED_BOOTSTRAP_SCRIPTS. The new
	helper MUST appear in that list or `source
	${SUPPORT_SCRIPTS_DIR}/validate_editor_audit.sh` will fail with
	"No such file" on every consumer-repo run."""
	text = STAGE_HELPER.read_text(encoding="utf-8")
	# Find the REQUIRED_BOOTSTRAP_SCRIPTS assignment.
	bootstrap_match = re.search(r'REQUIRED_BOOTSTRAP_SCRIPTS="([^"]+)"', text)
	assert bootstrap_match is not None, (
		"REQUIRED_BOOTSTRAP_SCRIPTS list not found — workflow bootstrap "
		"shape changed without test update."
	)
	bootstrap_list = bootstrap_match.group(1)
	assert "validate_editor_audit.sh" in bootstrap_list, (
		"validate_editor_audit.sh must be listed in "
		"REQUIRED_BOOTSTRAP_SCRIPTS or consumer-repo runs will fail "
		"to find the helper at ${SUPPORT_SCRIPTS_DIR}."
	)


# ---------------------------------------------------------------
# Cross-file: the noop-suspicious warning literal that the workflow
# emits and the poller greps for must stay in lockstep — drift here
# is the exact bug Fix B's recovery sweep was added to avoid.
# ---------------------------------------------------------------


NOOP_WARNING_LITERAL = "⚠️ **Editor no-op suspicious**"


def test_noop_warning_literal_present_in_workflow() -> None:
	text = _review_autofix_text()
	assert NOOP_WARNING_LITERAL in text, (
		"review_autofix.yml must still post a body comment containing "
		"the noop-suspicious warning literal — the orchestrator-poll "
		"sweep greps for this exact substring."
	)


def test_noop_warning_literal_present_in_poller() -> None:
	poller = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
	assert NOOP_WARNING_LITERAL in poller, (
		"scripts/orchestrate_poll_process.sh must reference the exact "
		"same noop-suspicious warning literal — otherwise the recovery "
		"sweep will fail to detect any noop-suspicious PRs."
	)


# ---------------------------------------------------------------
# Operator-facing warning step must branch on EDITOR_NOOP_REFUSAL.
#
# The validator sets EDITOR_NOOP_REFUSAL=true on a safety-policy refusal
# (Check 1b) specifically so the downstream Telegram/PR-comment alert can
# distinguish "model refused — re-run" from the generic "no-op suspicious
# — manual review". Before this branch the warning step ignored the flag
# and always posted the generic causes list, mis-attributing a transient
# refusal (consumer report: tele-funtoken-msg-scoring#3291, run
# 27057518455). These contracts pin the refusal-aware branch and the
# load-bearing poller literal that MUST survive in both branches.
# ---------------------------------------------------------------


WARNING_STEP_NAME = "Telegram editor-noop-suspicious warning"


def test_noop_warning_step_branches_on_editor_noop_refusal() -> None:
	"""The operator-facing warning step must branch on EDITOR_NOOP_REFUSAL
	(with a `:-false` default) so a safety-policy refusal surfaces a
	re-run-recommended alert instead of the generic causes list. The flag
	exists for exactly this branch (set by `Validate editor no-op
	disposition` Check 1b); leaving the warning generic mis-attributes a
	transient refusal."""
	block = _step_block(_review_autofix_text(), WARNING_STEP_NAME)
	assert '"${EDITOR_NOOP_REFUSAL:-false}" = "true"' in block, (
		"Warning step must branch on EDITOR_NOOP_REFUSAL (with a :-false "
		"default) so refusals get a refusal-specific alert."
	)


def test_noop_warning_refusal_branch_emits_refusal_specific_text() -> None:
	"""The refusal branch's Telegram message and PR comment must name the
	safety-policy refusal and recommend a re-run rather than the generic
	causes list."""
	block = _step_block(_review_autofix_text(), WARNING_STEP_NAME)
	assert "Editor safety-policy refusal: #${PR_NUMBER}" in block, (
		"Refusal branch must emit a refusal-specific Telegram heading."
	)
	assert "Re-run is likely to succeed once the provider-side cache expires." in block, (
		"Refusal branch must tell the operator a re-run is the remediation."
	)
	assert "returned a safety-policy refusal (safety filter)" in block, (
		"Refusal branch PR comment must attribute the no-op to the refusal."
	)


def test_noop_warning_refusal_branch_preserves_poller_literal() -> None:
	"""Both branches' PR comment bodies MUST keep the
	'⚠️ **Editor no-op suspicious**' literal: the orchestrator-poll
	noop-suspicious recovery sweep (scripts/orchestrate_poll_process.sh)
	greps PR comments for it to auto-re-dispatch the run, which is
	precisely the remediation a refusal needs. Dropping it from the
	refusal branch would strand refusal PRs (no auto-re-dispatch, no
	force-merge)."""
	block = _step_block(_review_autofix_text(), WARNING_STEP_NAME)
	body_prefix = f'BODY="{NOOP_WARNING_LITERAL}'
	assert block.count(body_prefix) == 3, (
		f"All three branches (refusal, recoverable failure, generic) must keep "
		f"{body_prefix!r} so the poller recovery sweep still detects "
		f"noop PRs. Found {block.count(body_prefix)} matching BODY assignment(s)."
	)


def test_noop_warning_generic_branch_preserved() -> None:
	"""The non-refusal branch must keep the existing generic operator
	wording verbatim (CLAUDE.md §6 — additive change only)."""
	block = _step_block(_review_autofix_text(), WARNING_STEP_NAME)
	assert "Editor claimed no changes needed but disposition could not be verified." in block, (
		"Generic-branch Telegram wording must be preserved (additive change)."
	)
	assert (
		"Possible causes: editor failed silently, did not read reviewer "
		"files, or audit counts are inconsistent." in block
	), (
		"Generic-branch PR-comment causes list must be preserved (additive "
		"change)."
	)


RECOVERABLE_FAILURE_SENTINEL_TEXT = "partial finalize requested after a recoverable editor failure"
RECOVERABLE_FAILURE_VALIDATOR_NOTICE = "::notice::Editor stopped after a recoverable failure on every attempt"
VALIDATOR_STEP_NAME = "Validate editor no-op disposition"


def _step_run_script(block: str) -> str:
	"""Return the de-indented bash body of a step block's `run: |` key with
	every `${{ ... }}` expression replaced by a literal placeholder so the
	body can be executed by bash outside Actions."""
	marker = "run: |"
	start = block.find(marker)
	assert start != -1, "Step block has no `run: |` body."
	body_lines = block[start + len(marker):].splitlines()[1:]
	indented = [line for line in body_lines if line.strip()]
	assert indented, "Step run body is empty."
	indent = min(len(line) - len(line.lstrip(" ")) for line in indented)
	script = "\n".join(line[indent:] if line.strip() else "" for line in body_lines)
	return re.sub(r"\$\{\{[^}]*\}\}", "ACTIONS_EXPR", script) + "\n"


def test_validator_sets_editor_noop_recoverable_failure_alongside_suspicious() -> None:
	"""The validator must set `EDITOR_NOOP_RECOVERABLE_FAILURE` additively
	alongside `EDITOR_NOOP_SUSPICIOUS` / `EDITOR_NOOP_REFUSAL` (CLAUDE.md §6)
	and export it via GITHUB_ENV without touching the existing exports."""
	block = _step_block(_review_autofix_text(), VALIDATOR_STEP_NAME)
	assert 'EDITOR_NOOP_RECOVERABLE_FAILURE="false"' in block
	assert 'echo "EDITOR_NOOP_RECOVERABLE_FAILURE=${EDITOR_NOOP_RECOVERABLE_FAILURE}" >> "$GITHUB_ENV"' in block
	assert 'echo "EDITOR_NOOP_SUSPICIOUS=${EDITOR_NOOP_SUSPICIOUS}" >> "$GITHUB_ENV"' in block
	assert 'echo "EDITOR_NOOP_REFUSAL=${EDITOR_NOOP_REFUSAL}" >> "$GITHUB_ENV"' in block


def test_validator_greps_for_recoverable_failure_sentinel_in_lockstep() -> None:
	"""Check 1c must grep the exact sentinel that the recoverable_failure
	fallback summary in review_apply_fixes.sh writes; a paraphrase on
	either side silently drops the failure-specific alert."""
	block = _step_block(_review_autofix_text(), VALIDATOR_STEP_NAME)
	assert f"grep -qiE '{RECOVERABLE_FAILURE_SENTINEL_TEXT}'" in block
	assert RECOVERABLE_FAILURE_VALIDATOR_NOTICE in block
	script = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	assert f"- {RECOVERABLE_FAILURE_SENTINEL_TEXT}" in script, (
		"review_apply_fixes.sh must keep the recoverable-failure sentinel "
		"verbatim on the fallback summary's `Runtime failure path:` line."
	)
	assert RUNBOOK.read_text(encoding="utf-8").count("EDITOR_NOOP_RECOVERABLE_FAILURE") >= 2


def test_validator_check_1c_sets_suspicious_without_touching_check_1() -> None:
	"""Check 1c must set `EDITOR_NOOP_SUSPICIOUS="true"` itself (the editor
	can run with REVIEWERS_SUCCESSFUL=0, where Check 2 is skipped), while the
	refusal Check 1b must independently set SUSPICIOUS for the same zero-reviewer
	case. The Check 1 regex and warning literal stay byte-for-byte (CLAUDE.md
	§6). The soft-deadline fallback sentinel must not be grepped: it means
	budget exhaustion, not an editor failure."""
	block = _step_block(_review_autofix_text(), VALIDATOR_STEP_NAME)
	check_1b_start = block.index("Check 1b: Detect refusal-specific sentinel")
	check_1c_start = block.index("Check 1c: Detect recoverable-failure partial-finalize sentinel")
	check_1b_segment = block[check_1b_start:check_1c_start]
	check_2_start = block.index("Check 2: Reviewer audit sanity", check_1c_start)
	check_1c_segment = block[check_1c_start:check_2_start]
	assert f"grep -qiE '{REFUSAL_SENTINEL_TEXT.replace('(', chr(92) + '(').replace(')', chr(92) + ')')}'" in check_1b_segment
	assert 'EDITOR_NOOP_SUSPICIOUS="true"' in check_1b_segment
	assert 'EDITOR_NOOP_REFUSAL="true"' in check_1b_segment
	assert f"grep -qiE '{RECOVERABLE_FAILURE_SENTINEL_TEXT}'" in check_1c_segment
	assert 'EDITOR_NOOP_SUSPICIOUS="true"' in check_1c_segment
	assert 'EDITOR_NOOP_RECOVERABLE_FAILURE="true"' in check_1c_segment
	assert "partial finalize requested at the soft deadline" not in check_1c_segment.split("grep -qiE")[1].split("\n")[0]
	assert "grep -qiE 'editor failed before producing|unavailable \\(editor fallback\\)'" in block
	assert VALIDATOR_WARNING_LITERAL in block
	assert 'if [ "${EDITOR_NOOP_SUSPICIOUS}" = "false" ] && [ "${REVIEWERS_SUCCESSFUL:-0}" -gt 0 ]; then' in block


def test_validator_gate_runs_for_editor_partial_finalize_without_validation_tail() -> None:
	"""The workflow gate must keep cheap editor-summary classification reachable
	when a late refusal or recoverable failure leaves too little time for the
	validation tail, without reclassifying soft-deadline budget exhaustion."""
	block = _step_block(_review_autofix_text(), VALIDATOR_STEP_NAME)
	if_line = next(line.strip() for line in block.splitlines() if line.strip().startswith("if:"))
	gate_start = if_line.index("(env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED")
	gate_end = if_line.index(" && env.AUTOFIX_RESUME_TERMINAL", gate_start)
	partial_finalize_gate = if_line[gate_start:gate_end]
	gate_cases = (
		({"AUTOFIX_PARTIAL_FINALIZE_REQUESTED": "true", "AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE": "false", "AUTOFIX_PARTIAL_FINALIZE_PHASE": "editor", "AUTOFIX_PARTIAL_FINALIZE_REASON": "recoverable_failure"}, True),
		({"AUTOFIX_PARTIAL_FINALIZE_REQUESTED": "true", "AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE": "false", "AUTOFIX_PARTIAL_FINALIZE_PHASE": "editor", "AUTOFIX_PARTIAL_FINALIZE_REASON": "refusal"}, True),
		({"AUTOFIX_PARTIAL_FINALIZE_REQUESTED": "true", "AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE": "false", "AUTOFIX_PARTIAL_FINALIZE_PHASE": "editor", "AUTOFIX_PARTIAL_FINALIZE_REASON": "soft_deadline"}, False),
		({"AUTOFIX_PARTIAL_FINALIZE_REQUESTED": "true", "AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE": "false", "AUTOFIX_PARTIAL_FINALIZE_PHASE": "reviewers", "AUTOFIX_PARTIAL_FINALIZE_REASON": "recoverable_failure"}, False),
		({"AUTOFIX_PARTIAL_FINALIZE_REQUESTED": "true", "AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE": "true", "AUTOFIX_PARTIAL_FINALIZE_PHASE": "reviewers", "AUTOFIX_PARTIAL_FINALIZE_REASON": "soft_deadline"}, True),
	)
	for gate_env, expected_result in gate_cases:
		def replace_gate_comparison(match: re.Match[str]) -> str:
			actual_value = gate_env[match.group(1)]
			return str(actual_value == match.group(3) if match.group(2) == "==" else actual_value != match.group(3))

		python_gate = re.sub(r"env\.([A-Z0-9_]+)\s*(==|!=)\s*'([^']*)'", replace_gate_comparison, partial_finalize_gate)
		python_gate = python_gate.replace("||", "or").replace("&&", "and")
		assert re.fullmatch(r"[() TrueFalsorand]+", python_gate), python_gate
		assert eval(python_gate, {"__builtins__": {}}, {}) is expected_result, gate_env


def test_validator_classifies_recoverable_failure_summary(tmp_path: Path) -> None:
	"""Execute the validator step body against the real fallback summary
	shapes at two reviewer counts.

	REVIEWERS_SUCCESSFUL=6: the recoverable_failure summary is flagged by
	Check 1c and the refusal summary by Check 1b before Check 2 can run:
	SUSPICIOUS true for both, only the matching specific flag set.

	REVIEWERS_SUCCESSFUL=0: Check 2 is skipped. The editor still runs in that
	state when every reviewer slot was skipped fail-open (`skipped_open` /
	`skipped_unmapped` in scripts/review_run_reviewers.sh exit 0 with
	REVIEWERS_SUCCESSFUL=0, and the editor step's `if:` has no reviewer-count
	clause), so Checks 1b and 1c must set SUSPICIOUS=true on their own for the
	refusal and recoverable_failure summaries — otherwise the Telegram/PR-comment
	alert never fires and the resolver chain gated on
	`EDITOR_NOOP_SUSPICIOUS != 'true'` runs with nothing to land."""
	block = _step_block(_review_autofix_text(), VALIDATOR_STEP_NAME)
	script = _step_run_script(block)
	fixtures = {
		"recoverable": (
			"Changes made:\n- none (editor requested partial finalize after a recoverable failure before another validated attempt completed)\n\n"
			"Review file issue audit:\n- none (editor stopped after a recoverable failure before another validated attempt completed)\n\n"
			"Regression fingerprint:\n- unavailable (partial finalize after recoverable editor failure)\n\n"
			f"Runtime failure path:\n- {RECOVERABLE_FAILURE_SENTINEL_TEXT} before another validated attempt\n"
		),
		"refusal": (
			"Changes made:\n- none (editor returned a safety-policy refusal before another validated attempt could complete)\n\n"
			"Review file issue audit:\n- none (editor stopped after a safety-policy refusal before another validated attempt could complete)\n\n"
			"Regression fingerprint:\n- unavailable (partial finalize after safety-policy refusal)\n\n"
			f"Runtime failure path:\n- {REFUSAL_SENTINEL_TEXT}\n"
		),
	}
	expectations = {
		("recoverable", "6"): {"EDITOR_NOOP_SUSPICIOUS": "true", "EDITOR_NOOP_REFUSAL": "false", "EDITOR_NOOP_RECOVERABLE_FAILURE": "true"},
		("refusal", "6"): {"EDITOR_NOOP_SUSPICIOUS": "true", "EDITOR_NOOP_REFUSAL": "true", "EDITOR_NOOP_RECOVERABLE_FAILURE": "false"},
		("recoverable", "0"): {"EDITOR_NOOP_SUSPICIOUS": "true", "EDITOR_NOOP_REFUSAL": "false", "EDITOR_NOOP_RECOVERABLE_FAILURE": "true"},
		("refusal", "0"): {"EDITOR_NOOP_SUSPICIOUS": "true", "EDITOR_NOOP_REFUSAL": "true", "EDITOR_NOOP_RECOVERABLE_FAILURE": "false"},
	}
	for (name, reviewers_successful), expected_exports in expectations.items():
		case_label = f"{name}@reviewers={reviewers_successful}"
		summary_file = tmp_path / f"{name}_{reviewers_successful}_summary.txt"
		summary_file.write_text(fixtures[name], encoding="utf-8")
		github_env = tmp_path / f"{name}_{reviewers_successful}_github_env"
		github_env.write_text("", encoding="utf-8")
		result = subprocess.run(
			["bash", "-c", script],
			cwd=REPO_ROOT,
			env={
				"PATH": os.environ["PATH"],
				"EDITOR_SUMMARY_FILE": str(summary_file),
				"REVIEWERS_SUCCESSFUL": reviewers_successful,
				"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
				"GITHUB_ENV": str(github_env),
			},
			text=True,
			capture_output=True,
			check=False,
		)
		assert result.returncode == 0, f"validator body failed for {case_label}: {result.stderr}"
		exported = dict(line.split("=", 1) for line in github_env.read_text(encoding="utf-8").splitlines() if "=" in line)
		for key, expected in expected_exports.items():
			assert exported.get(key) == expected, f"{case_label}: expected {key}={expected}, got {exported}"
		# Check 1's legacy regex must not match either partial-finalize
		# summary shape: the zero-reviewer recoverable case is flagged by
		# Check 1c alone, never by the Check 1 warning the e2e poller greps.
		assert VALIDATOR_WARNING_LITERAL not in result.stdout, case_label
		if name == "recoverable":
			assert RECOVERABLE_FAILURE_VALIDATOR_NOTICE in result.stdout, case_label
			assert REFUSAL_VALIDATOR_NOTICE not in result.stdout, case_label
		else:
			assert REFUSAL_VALIDATOR_NOTICE in result.stdout, case_label
			assert RECOVERABLE_FAILURE_VALIDATOR_NOTICE not in result.stdout, case_label


def test_noop_warning_step_branches_on_recoverable_failure_with_last_error(tmp_path: Path) -> None:
	"""Execute the warning step body with stubbed Telegram/GitHub helpers:
	the recoverable-failure branch must report the attempt count and the
	final attempt's `Error:` line (clipped) in both the Telegram message and
	the PR comment, keep the poller literal in the comment, and never quote
	earlier attempts' errors."""
	block = _step_block(_review_autofix_text(), WARNING_STEP_NAME)
	assert '"${EDITOR_NOOP_RECOVERABLE_FAILURE:-false}" = "true"' in block
	script = _step_run_script(block)
	support_dir = tmp_path / "support"
	support_dir.mkdir()
	(support_dir / "tg_helpers.sh").write_text(
		'tg_send_tracked() { printf \'%s\\n\' "$2" > "${TG_CAPTURE_FILE}"; }\n', encoding="utf-8"
	)
	(support_dir / "gh_helpers.sh").write_text(
		'gh_retry() { shift; printf \'%s\\n\' "$@" > "${GH_CAPTURE_FILE}"; }\n', encoding="utf-8"
	)
	previous_reviews = tmp_path / "previous_reviews"
	previous_reviews.mkdir()
	(previous_reviews / "editor_attempt_1.err").write_text("Error: first attempt failure\n", encoding="utf-8")
	(previous_reviews / "editor_attempt_2.err").write_text("Error: second attempt failure\n", encoding="utf-8")
	(previous_reviews / "editor_attempt_3.err").write_text(
		"opencode_agent_start role=writer\n"
		'timestamp=2026-09-12T15:54:08.842Z level=ERROR message="stream error" error.error="AI_APICallError: broker request or output-token limit reached"\n'
		"Error: broker request or `output-token` limit reached\n"
		"timestamp=2026-09-12T15:54:08.860Z level=INFO message=\"disposing instance\"\n",
		encoding="utf-8",
	)
	tg_capture = tmp_path / "tg.txt"
	gh_capture = tmp_path / "gh.txt"
	warning_env = {
		"PATH": os.environ["PATH"],
		"SUPPORT_SCRIPTS_DIR": str(support_dir),
		"PREVIOUS_REVIEWS_DIR": str(previous_reviews),
		"EDITOR_NOOP_SUSPICIOUS": "true",
		"EDITOR_NOOP_REFUSAL": "false",
		"EDITOR_NOOP_RECOVERABLE_FAILURE": "true",
		"PR_NUMBER": "4077",
		"TG_CAPTURE_FILE": str(tg_capture),
		"GH_CAPTURE_FILE": str(gh_capture),
	}
	result = subprocess.run(
		["bash", "-eo", "pipefail", "-c", script],
		cwd=REPO_ROOT,
		env=warning_env,
		text=True,
		capture_output=True,
		check=False,
	)
	assert result.returncode == 0, f"warning step body failed: {result.stderr}"
	telegram_message = tg_capture.read_text(encoding="utf-8")
	assert "Editor failed on all 3 attempts: #4077" in telegram_message
	assert "Last provider error: Error: broker request or output-token limit reached" in telegram_message
	assert "second attempt failure" not in telegram_message
	assert "Editor claimed no changes needed" not in telegram_message
	pr_comment = gh_capture.read_text(encoding="utf-8")
	assert NOOP_WARNING_LITERAL in pr_comment
	assert "failed on all 3 attempts" in pr_comment
	assert "broker request or output-token limit reached" in pr_comment

	for editor_attempt_error_file in previous_reviews.glob("editor_attempt_*.err"):
		editor_attempt_error_file.unlink()
	(previous_reviews / "editor_attempt_1.err").write_text(
		'timestamp=2026-09-12T15:54:08.842Z level=ERROR message="stream error" error.error="AI_APICallError: structured `fallback` failure"\n',
		encoding="utf-8",
	)
	result = subprocess.run(
		["bash", "-eo", "pipefail", "-c", script],
		cwd=REPO_ROOT,
		env=warning_env,
		text=True,
		capture_output=True,
		check=False,
	)
	assert result.returncode == 0, f"warning step failed for structured fallback: {result.stderr}"
	telegram_message = tg_capture.read_text(encoding="utf-8")
	assert "Editor failed on 1 attempt: #4077" in telegram_message
	assert "Last provider error: AI_APICallError: structured fallback failure" in telegram_message
	pr_comment = gh_capture.read_text(encoding="utf-8")
	assert "failed on 1 attempt" in pr_comment
	assert "AI_APICallError: structured fallback failure" in pr_comment

	for editor_attempt_error_file in previous_reviews.glob("editor_attempt_*.err"):
		editor_attempt_error_file.unlink()
	result = subprocess.run(
		["bash", "-eo", "pipefail", "-c", script],
		cwd=REPO_ROOT,
		env=warning_env,
		text=True,
		capture_output=True,
		check=False,
	)
	assert result.returncode == 0, f"warning step failed without stderr artifacts: {result.stderr}"
	telegram_message = tg_capture.read_text(encoding="utf-8")
	assert "Editor failed on every attempt: #4077" in telegram_message
	assert "Last provider error: not captured" in telegram_message
	pr_comment = gh_capture.read_text(encoding="utf-8")
	assert NOOP_WARNING_LITERAL in pr_comment
	assert "failed on every attempt" in pr_comment
	assert "Last provider error from the final attempt: `not captured" in pr_comment


def test_editor_workspace_guard_failure_retains_evidence(tmp_path: Path) -> None:
	text = _review_apply_fixes_text()
	start = text.index('  if ! bash "${SUPPORT_SCRIPTS_DIR}/untrusted_process_sandbox.sh" \\', text.index('  kill "${wd_pid}"'))
	end = text.index('  editor_clean_output=', start)
	guard_branch = text[start:end]
	guard_branch = re.sub(r'^  if ! bash .*?--report "\$\{editor_workspace_report\}"; then', '  if ! false; then', guard_branch, count=1, flags=re.S)
	assert 'if ! false; then' in guard_branch
	for report_state in ("present", "missing", "malformed"):
		previous_reviews = tmp_path / report_state
		previous_reviews.mkdir()
		output = previous_reviews / "model.out"
		stderr = previous_reviews / "model.err"
		output.write_text("editor transcript\n", encoding="utf-8")
		stderr.write_text("editor stderr\n", encoding="utf-8")
		report = previous_reviews / "guard.json"
		if report_state == "present":
			report.write_text(json.dumps({
				"schema_version": "post_agent_workspace_report.v1",
				"rejected": [{"path": ".ai/\n::error::forged", "change": "modified", "reason": "hidden-path"}],
			}), encoding="utf-8")
		elif report_state == "malformed":
			report.write_text('{"rejected": [}', encoding="utf-8")
		github_env = previous_reviews / "github_env"
		github_env.touch()
		result = subprocess.run(
			["bash", "-euo", "pipefail", "-c", guard_branch],
			env={"PATH": os.environ["PATH"], "GITHUB_ENV": str(github_env),
				"PREVIOUS_REVIEWS_DIR": str(previous_reviews), "attempt": "2",
				"tmp_output": str(output), "tmp_err": str(stderr), "editor_workspace_report": str(report)},
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 78, result.stderr
		assert "AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED=true" in github_env.read_text(encoding="utf-8")
		assert (previous_reviews / "editor_attempt_2.txt").read_text(encoding="utf-8") == "editor transcript\n"
		assert (previous_reviews / "editor_attempt_2.err").read_text(encoding="utf-8") == "editor stderr\n"
		if report_state == "present":
			assert json.loads((previous_reviews / "editor_attempt_2_guard_report.json").read_text(encoding="utf-8"))["rejected"][0]["change"] == "modified"
			assert 'change="modified" reason="hidden-path"' in result.stdout
			assert "\n::error::forged" not in result.stdout
		elif report_state == "malformed":
			assert "EDITOR_WORKSPACE_GUARD_REPORT=unreadable_or_invalid" in result.stdout
		else:
			assert "EDITOR_WORKSPACE_GUARD_REPORT=missing_or_oversized" in result.stdout


def test_editor_workspace_guard_failure_workflow_routing(tmp_path: Path) -> None:
	workflow = _review_autofix_text()
	editor_step = _step_block(workflow, "Apply fixes with editor model")
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh"\n' in editor_step
	assert 'grep -qx \'AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED=true\' "${GITHUB_ENV}"' in editor_step
	assert editor_step.index('bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh"\n') < editor_step.index('Editor in-step retry also failed')
	summary_step = _step_block(workflow, "Post editor summary comment")
	assert "env.AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED == 'true'" in summary_step.split("run: |")[0]
	assert summary_step.index('if [ "${AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED:-false}" = "true" ]') < summary_step.index('AUTOFIX_EDITOR_EMPTY_NOOP=true')
	assert 'editor_attempt_*_guard_report.json' in _step_block(workflow, "Stage codex logs for upload (failure or empty-editor)")
	for name in ("Mark linked issues review-blocked (workflow failure)", "Force orchestrate poll after workflow failure review-blocked label", "Post review-blocked comment on PR (workflow failure)"):
		failure_gate = _step_block(workflow, name).split('run: |')[0]
		assert "env.AUTOFIX_EDITOR_EMPTY_NOOP != 'true' || env.AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED == 'true'" in failure_gate
	assert "continue-on-error" not in editor_step.split('run: |')[0]
	for name in ("Commit changes", "Push all pending commits", "Enable auto-merge on PR", "Mark linked issues ready to merge"):
		gate = _step_block(workflow, name).split('run: |')[0]
		assert "if:" in gate and "env.AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED != 'true'" in gate
		assert "always()" not in gate and "failure()" not in gate

	support_dir = tmp_path / "support"
	support_dir.mkdir()
	(support_dir / "gh_helpers.sh").write_text('gh_retry() { printf "%s\\n" "$@" > "$COMMENT_CAPTURE"; }\n', encoding="utf-8")
	github_env = tmp_path / "github_env"
	github_env.touch()
	comment_capture = tmp_path / "comment"
	summary_script = _step_run_script(summary_step)
	previous_reviews = tmp_path / "previous_reviews"
	previous_reviews.mkdir()
	for report_state in ("present", "missing", "malformed"):
		report = previous_reviews / "editor_attempt_2_guard_report.json"
		if report_state == "present":
			report.write_text(json.dumps({
				"schema_version": "post_agent_workspace_report.v1",
				"rejected": [{"path": ".ai/.workspace_source_manifest.txt", "change": "modified", "reason": "hidden-path"}],
			}), encoding="utf-8")
		elif report_state == "malformed":
			report.write_text('{"rejected": [}', encoding="utf-8")
		else:
			report.unlink()
		result = subprocess.run(["bash", "-euo", "pipefail", "-c", summary_script], env={
			"PATH": os.environ["PATH"], "SUPPORT_SCRIPTS_DIR": str(support_dir),
			"AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED": "true", "GITHUB_ENV": str(github_env),
			"EDITOR_SUMMARY_FILE": str(tmp_path / "empty"), "PR_NUMBER": "4413",
			"COMMENT_CAPTURE": str(comment_capture), "PREVIOUS_REVIEWS_DIR": str(previous_reviews),
		}, capture_output=True, text=True, check=False)
		assert result.returncode == 1, result.stderr
		comment = comment_capture.read_text(encoding="utf-8")
		assert "body=**AI review/autofix editor workspace guard rejected a change" in comment
		assert "[View workflow run](ACTIONS_EXPR/ACTIONS_EXPR/actions/runs/ACTIONS_EXPR)" in comment
		if report_state == "present":
			assert '- path=".ai/.workspace_source_manifest.txt" change="modified" reason="hidden-path"' in comment
		else:
			assert "Guard report unavailable or invalid" in comment
		assert "AUTOFIX_EDITOR_EMPTY_NOOP" not in github_env.read_text(encoding="utf-8")

	(support_dir / "gh_helpers.sh").write_text('gh_retry() { return 4; }\n', encoding="utf-8")
	result = subprocess.run(["bash", "-euo", "pipefail", "-c", summary_script], env={
		"PATH": os.environ["PATH"], "SUPPORT_SCRIPTS_DIR": str(support_dir),
		"AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED": "true", "GITHUB_ENV": str(github_env),
		"PR_NUMBER": "4413", "PREVIOUS_REVIEWS_DIR": str(previous_reviews),
	}, capture_output=True, text=True, check=False)
	assert result.returncode == 1, result.stderr
	assert "::warning::Failed to post editor workspace guard comment on PR #4413 (exit=4)." in result.stdout

	retry_start = editor_step.index('bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh" || {')
	retry_end = editor_step.index('\n              }', retry_start) + len('\n              }')
	retry_script = editor_step[retry_start:retry_end]
	for guard_failed in (True, False):
		(support_dir / "review_apply_fixes.sh").write_text(
			'echo "AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED=true" >> "$GITHUB_ENV"\nexit 78\n'
			if guard_failed else 'exit 1\n', encoding="utf-8",
		)
		github_env.write_text("", encoding="utf-8")
		result = subprocess.run(["bash", "-euo", "pipefail", "-c", retry_script], env={
			"PATH": os.environ["PATH"], "SUPPORT_SCRIPTS_DIR": str(support_dir),
			"GITHUB_ENV": str(github_env),
		}, capture_output=True, text=True, check=False)
		assert result.returncode == (78 if guard_failed else 0), result.stderr
		assert ("Editor in-step retry also failed" in result.stdout) is not guard_failed

	first_invocation = 'bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh"'
	(support_dir / "review_apply_fixes.sh").write_text(
		'echo "AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED=true" >> "$GITHUB_ENV"\nexit 78\n', encoding="utf-8",
	)
	github_env.write_text("", encoding="utf-8")
	result = subprocess.run(["bash", "-euo", "pipefail", "-c", first_invocation], env={
		"PATH": os.environ["PATH"], "SUPPORT_SCRIPTS_DIR": str(support_dir),
		"GITHUB_ENV": str(github_env),
	}, capture_output=True, text=True, check=False)
	assert result.returncode == 78
	assert "AUTOFIX_EDITOR_WORKSPACE_GUARD_FAILED=true" in github_env.read_text(encoding="utf-8")


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
	test_editor_prompt_documents_convergence_outcome()
	test_editor_prompt_states_audit_arithmetic_invariant()
	test_editor_prompt_requires_changes_match_this_run_writes()
	test_editor_prompt_forbids_fabricated_edits()
	test_editor_prompt_requires_self_check_before_emitting()
	test_editor_prompt_section_headings_unchanged()
	test_workflow_sources_shared_audit_helper()
	test_workflow_no_longer_contains_inline_audit_arithmetic_regex()
	test_workflow_warning_literals_preserved_in_helper()
	test_required_bootstrap_scripts_includes_audit_helper()
	test_noop_warning_literal_present_in_workflow()
	test_noop_warning_literal_present_in_poller()
	test_noop_warning_step_branches_on_editor_noop_refusal()
	test_noop_warning_refusal_branch_emits_refusal_specific_text()
	test_noop_warning_refusal_branch_preserves_poller_literal()
	test_noop_warning_generic_branch_preserved()
	test_validator_sets_editor_noop_recoverable_failure_alongside_suspicious()
	test_validator_greps_for_recoverable_failure_sentinel_in_lockstep()
	test_validator_check_1c_sets_suspicious_without_touching_check_1()
	test_validator_gate_runs_for_editor_partial_finalize_without_validation_tail()
	with tempfile.TemporaryDirectory() as temporary_test_directory:
		test_validator_classifies_recoverable_failure_summary(Path(temporary_test_directory))
		test_noop_warning_step_branches_on_recoverable_failure_with_last_error(Path(temporary_test_directory))
	print("All EDITOR_NOOP_SUSPICIOUS cascade-guard contract tests passed.")

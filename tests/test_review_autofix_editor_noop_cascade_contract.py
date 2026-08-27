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

import re
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
	# codex must read the per-attempt file (which carries the nonce trailer),
	# never the unchanging base prompt. The stdin redirect may be inline
	# (`< "${attempt_prompt_file}"`) or inside a one-arg helper that the call
	# site feeds `${attempt_prompt_file}` into — the S4 continuation-thread-
	# reuse change extracted `run_editor_codex_attempt` for exactly this.
	# Assert the behaviour, not one code shape, so a cache-buster-preserving
	# refactor does not trip this guard while a regression to the base prompt
	# still does.
	assert 'codex --ask-for-approval never' in text


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
					r'codex --ask-for-approval never[^\n]*<\s*"?([$](?:\{?\w+\}?))"?',
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
		"No `codex --ask-for-approval never … < \"$file\"`-style stdin "
		"redirect "
		"found; the editor must feed codex a prompt file on stdin."
	)
	assert all(
		stdin_var != "EDITOR_PROMPT_FILE"
		for stdin_var, _helper_name in codex_stdin_targets
	), (
		"codex stdin is the unchanging `${EDITOR_PROMPT_FILE}` — every retry "
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
	assert block.count(body_prefix) == 2, (
		f"Both branches must keep {body_prefix!r} so the poller recovery "
		f"sweep still detects refusal-caused noop PRs. Found "
		f"{block.count(body_prefix)} matching BODY assignment(s)."
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
	print("All EDITOR_NOOP_SUSPICIOUS cascade-guard contract tests passed.")

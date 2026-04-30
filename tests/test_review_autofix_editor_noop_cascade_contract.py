#!/usr/bin/env python3
"""Contract tests for the EDITOR_NOOP_SUSPICIOUS cascade guard wired across
`review_autofix.yml` and the e2e poller in `test-and-mark-stable.yml`.

The guard is documented in `agents.md` §20.10. Three invariants must hold
together — if any one regresses, the run-25126757724 cascade can re-emerge
or the cross-workflow grep contract can silently break:

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
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
TEST_AND_MARK_STABLE = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

VALIDATOR_WARNING_LITERAL = "::warning::Editor summary contains failure/fallback markers"
RETRY_NOTICE_LITERAL = "::notice::Editor summary present but matched fallback-marker regex"
NOOP_SUSPICIOUS_GUARD = "env.EDITOR_NOOP_SUSPICIOUS != 'true'"

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


if __name__ == "__main__":
	test_merge_conflict_chain_gates_on_editor_noop_suspicious()
	test_validator_emits_exact_grep_literal()
	test_retry_notice_does_not_collide_with_validator_literal()
	test_e2e_poller_grep_includes_warning_prefix()
	test_e2e_poller_uses_tempfile_not_variable_capture()
	print("All EDITOR_NOOP_SUSPICIOUS cascade-guard contract tests passed.")

#!/usr/bin/env python3
"""Contract tests for outcome-aware retry-prelude rendering in
scripts/review_conflict_resolve.sh::_build_retry_prompt.

The {{PREVIOUS_OUTCOME_NOTICE}} placeholder and the conditional
{{#IF_VIOLATIONS}}…{{/IF_VIOLATIONS}} body suppression keep the
next-attempt reflexion prompt accurate when the previous attempt
was killed by `timeout` (exit 124 / 137) or exited non-zero before
producing any patch (the violations body's "your previous attempt
produced output that failed post-resolve validation" framing is
misleading on those paths because soft validation never ran).

These tests pin the rendering logic by extracting the python3
heredoc body from the script and running it directly against
controlled prelude templates and env vars. Without them, a future
edit that:
  - drops the SUPPRESS_VIOLATIONS_BODY branch
  - changes the marker-regex spelling
  - re-adds the misleading "produced output that failed validation"
    framing to the timeout/error path
  - breaks the {{PREVIOUS_OUTCOME_NOTICE}} substitution
would silently regress the model's retry context on the
originating failure mode: runs 25627236793 / 25627316961 on
shubhodeep1/tele-funtoken-msg-scoring's orchestrator/project-2840
stack, where the resolver hung in extended reasoning at `xhigh`
on a file with duplicate function bodies outside the conflict
markers and got SIGTERMed on all three retry attempts with
markers=0, fingerprint_violations=0 on every retry — the
unambiguous signature of `timeout` firing pre-patch.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"


def _extract_render_script() -> str:
	"""Pull the python3 heredoc body out of `_build_retry_prompt`.

	Re-implementing the substitution logic in a copy would not
	test the actual script — extracting the heredoc and running
	it directly does.

	The extraction regex is intentionally loose so it survives
	common launcher refactors (different heredoc tag, the
	tempfile redirect target moving or being replaced with a
	pipe, the `python3 -` form changing to `python3 /dev/stdin`,
	etc.). It looks for any `python3 …` invocation followed by a
	`<<'TAG'` heredoc whose terminator is the captured TAG on its
	own line. The body must contain the
	`PREVIOUS_OUTCOME_NOTICE` substitution key — without that, the
	extracted block is not the renderer we want, and re-extraction
	via a different anchor is the right next step.
	"""
	src = RESOLVE_SCRIPT.read_text(encoding="utf-8")
	candidates = re.findall(
		r"python3\b[^\n]*<<'(?P<tag>\w+)'\n(?P<body>.*?)\n(?P=tag)\n",
		src,
		flags=re.DOTALL,
	)
	if not candidates:
		raise AssertionError(
			"Could not locate any python3 heredoc in "
			"scripts/review_conflict_resolve.sh — if the renderer "
			"was rewritten without a heredoc-launched python3 "
			"invocation, update this regex (or the test approach) "
			"so it still exercises the substitution logic."
		)
	for _tag, body in candidates:
		if "PREVIOUS_OUTCOME_NOTICE" in body:
			return body
	raise AssertionError(
		"Found python3 heredoc(s) in scripts/review_conflict_resolve.sh "
		"but none reference PREVIOUS_OUTCOME_NOTICE; the retry-prelude "
		"renderer may have moved to a different launch site. Update "
		"this extractor to follow it."
	)


# Canonical template shape, mirroring
# prompts/integration-sync-conflict-resolver-retry-prelude.txt at
# the time PR #2449 landed. Tests use a minimal in-memory copy so
# they pin the renderer's contract independent of the on-disk
# template's exact prose.
_CANONICAL_TEMPLATE = (
	"=== HEADER (attempt {{PREVIOUS_ATTEMPT_NUMBER}} of {{MAX_ATTEMPTS}}) ==="
	"{{PREVIOUS_OUTCOME_NOTICE}}\n"
	"\n"
	"{{#IF_VIOLATIONS}}\n"
	"VIOLATIONS_BODY_LINE_1: this prose talks about post-resolve validation.\n"
	"--- markers ({{MARKER_VIOLATION_COUNT}}) ---\n"
	"{{MARKER_VIOLATION_FILES}}\n"
	"--- fingerprints ({{FINGERPRINT_VIOLATION_COUNT}}) ---\n"
	"{{FINGERPRINT_VIOLATION_DETAILS}}\n"
	"VIOLATIONS_BODY_LINE_2: end of body.\n"
	"{{/IF_VIOLATIONS}}\n"
	"\n"
	"=== ORIGINAL TASK ===\n"
	"\n"
)

_ORIGINAL_TASK_PAYLOAD = "ORIGINAL TASK CONTENT FOR THE TEST\n"


def _run_render(
	*,
	tpl_text: str,
	suppress: bool,
	outcome_notice: str = "",
	marker_count: str = "0",
	marker_files: str = "(none)",
	fp_count: str = "0",
	fp_details: str = "(none)",
	prev_attempt: str = "1",
	max_attempts: str = "3",
) -> tuple[str, str, int]:
	"""Run the extracted python3 renderer with the given inputs.

	Returns (stdout, stderr, returncode).  The renderer reads its
	template + original-task path from env vars, so we materialise
	both into tempfiles inside a TemporaryDirectory.
	"""
	render_script = _extract_render_script()
	with tempfile.TemporaryDirectory() as tmpdir:
		tpl_path = Path(tmpdir) / "prelude.txt"
		tpl_path.write_text(tpl_text, encoding="utf-8")
		orig_path = Path(tmpdir) / "original_task.txt"
		orig_path.write_text(_ORIGINAL_TASK_PAYLOAD, encoding="utf-8")

		env = {
			**os.environ,
			"SUPPRESS_VIOLATIONS_BODY": "1" if suppress else "0",
			"PRELUDE_TPL": str(tpl_path),
			"ORIGINAL_PROMPT_FILE": str(orig_path),
			"PREVIOUS_ATTEMPT_NUMBER": prev_attempt,
			"MAX_ATTEMPTS": max_attempts,
			"MARKER_VIOLATION_COUNT": marker_count,
			"MARKER_VIOLATION_FILES": marker_files,
			"FINGERPRINT_VIOLATION_COUNT": fp_count,
			"FINGERPRINT_VIOLATION_DETAILS": fp_details,
			"PREVIOUS_OUTCOME_NOTICE": outcome_notice,
		}
		proc = subprocess.run(
			["python3", "-c", render_script],
			env=env,
			capture_output=True,
			text=True,
			check=False,
		)
	return proc.stdout, proc.stderr, proc.returncode


def test_outcome_ran_keeps_violations_body() -> None:
	"""On the post-validation retry path (`ran`/`violations`),
	SUPPRESS_VIOLATIONS_BODY=0 must keep the violations body
	intact and substitute MARKER_/FINGERPRINT_ counts + lists."""
	stdout, stderr, rc = _run_render(
		tpl_text=_CANONICAL_TEMPLATE,
		suppress=False,
		outcome_notice="",  # no notice on the ran/violations path
		marker_count="2",
		marker_files="          - foo.py\n          - bar.py",
		fp_count="1",
		fp_details="          - must_contain pattern missing from baz.py",
	)
	assert rc == 0, f"renderer returned non-zero: rc={rc}, stderr={stderr!r}"
	assert "VIOLATIONS_BODY_LINE_1" in stdout
	assert "VIOLATIONS_BODY_LINE_2" in stdout
	assert "--- markers (2) ---" in stdout
	assert "          - foo.py" in stdout
	assert "          - bar.py" in stdout
	assert "--- fingerprints (1) ---" in stdout
	assert "must_contain pattern missing from baz.py" in stdout
	# The {{#IF_VIOLATIONS}} / {{/IF_VIOLATIONS}} marker lines
	# themselves must be stripped (they are template directives,
	# not content).
	assert "{{#IF_VIOLATIONS}}" not in stdout
	assert "{{/IF_VIOLATIONS}}" not in stdout
	# Original-task payload must be appended.
	assert stdout.endswith(_ORIGINAL_TASK_PAYLOAD)
	# No leftover-marker warnings on a well-formed template.
	assert "::warning::" not in stderr


def test_outcome_timeout_suppresses_violations_body() -> None:
	"""On the timeout path, SUPPRESS_VIOLATIONS_BODY=1 must strip
	the entire {{#IF_VIOLATIONS}}…{{/IF_VIOLATIONS}} region so the
	misleading "produced output that failed post-resolve validation"
	framing never reaches the model."""
	notice = "\n*** TIMEOUT NOTICE ***\n[notice body]\n*** END TIMEOUT NOTICE ***"
	stdout, stderr, rc = _run_render(
		tpl_text=_CANONICAL_TEMPLATE,
		suppress=True,
		outcome_notice=notice,
		# Stale violation values that would mislead if the body
		# wasn't stripped — the suppression contract guarantees
		# they never leak into the rendered prompt.
		marker_count="99",
		marker_files="          - STALE_FILE.py",
		fp_count="42",
		fp_details="          - stale fingerprint detail",
	)
	assert rc == 0, f"renderer returned non-zero: rc={rc}, stderr={stderr!r}"
	assert "*** TIMEOUT NOTICE ***" in stdout
	assert "*** END TIMEOUT NOTICE ***" in stdout
	# Body must be gone — neither the prose nor the violation
	# substitutions should appear.
	assert "VIOLATIONS_BODY_LINE_1" not in stdout
	assert "VIOLATIONS_BODY_LINE_2" not in stdout
	assert "--- markers" not in stdout
	assert "--- fingerprints" not in stdout
	assert "STALE_FILE.py" not in stdout
	assert "stale fingerprint detail" not in stdout
	# Conditional markers themselves must be stripped.
	assert "{{#IF_VIOLATIONS}}" not in stdout
	assert "{{/IF_VIOLATIONS}}" not in stdout
	# Original-task payload still appended.
	assert stdout.endswith(_ORIGINAL_TASK_PAYLOAD)
	assert "::warning::" not in stderr


def test_outcome_error_suppresses_violations_body() -> None:
	"""On the non-timeout-error path, SUPPRESS_VIOLATIONS_BODY=1
	must strip the violations body the same way as the timeout
	path; the only difference is the notice text."""
	notice = (
		"\n*** PREVIOUS ATTEMPT EXITED NON-ZERO ***\n"
		"[notice body]\n*** END NOTICE ***"
	)
	stdout, stderr, rc = _run_render(
		tpl_text=_CANONICAL_TEMPLATE,
		suppress=True,
		outcome_notice=notice,
	)
	assert rc == 0, f"renderer returned non-zero: rc={rc}, stderr={stderr!r}"
	assert "*** PREVIOUS ATTEMPT EXITED NON-ZERO ***" in stdout
	assert "VIOLATIONS_BODY_LINE_1" not in stdout
	assert "VIOLATIONS_BODY_LINE_2" not in stdout
	assert "{{#IF_VIOLATIONS}}" not in stdout
	assert "{{/IF_VIOLATIONS}}" not in stdout
	assert "::warning::" not in stderr


def test_outcome_notice_placeholder_substitutes_at_header_position() -> None:
	"""{{PREVIOUS_OUTCOME_NOTICE}} sits directly after the header
	on the same line in the canonical template, so a non-empty
	notice (which always starts with `\\n` per the
	_build_retry_prompt invariant) lands cleanly on the line below
	the header rather than gluing to the `===` text."""
	notice = "\n*** A NOTICE ***\n[body]\n*** END ***"
	stdout, _stderr, rc = _run_render(
		tpl_text=_CANONICAL_TEMPLATE,
		suppress=True,
		outcome_notice=notice,
		prev_attempt="2",
		max_attempts="3",
	)
	assert rc == 0
	# The header line ends, then the notice's leading `\n` puts
	# the `*** A NOTICE ***` marker on its own line. Any glueing
	# (`=== HEADER … === *** A NOTICE ***`) would mean the
	# leading-newline invariant or the substitution broke.
	assert "=== HEADER (attempt 2 of 3) ===\n*** A NOTICE ***" in stdout, (
		"Expected the notice's first line on its own line directly "
		"below the rendered header; got:\n" + stdout[:400]
	)


def test_marker_count_assertion_warns_on_malformed_template() -> None:
	"""A template with more or fewer markers than the
	canonical 1-open / 1-close pair must trigger the
	pre-strip count assertion's `::warning::` so a malformed
	template is diagnosable from the workflow log."""
	# Two opens, one close — the non-greedy `.*?` strip would
	# otherwise silently swallow the inner content without
	# raising an alarm.
	malformed = (
		"=== H ==={{PREVIOUS_OUTCOME_NOTICE}}\n"
		"\n"
		"{{#IF_VIOLATIONS}}\n"
		"first body\n"
		"{{#IF_VIOLATIONS}}\n"
		"second body\n"
		"{{/IF_VIOLATIONS}}\n"
		"\n"
		"=== ORIGINAL TASK ===\n"
		"\n"
	)
	_stdout, stderr, rc = _run_render(
		tpl_text=malformed,
		suppress=True,
		outcome_notice="\n[notice]",
	)
	# Renderer is fail-open — non-zero exit would block the retry
	# loop on a documentation bug, which is worse than a
	# malformed prompt the operator can fix.
	assert rc == 0
	assert "::warning::" in stderr, (
		"Expected the marker-count assertion to fire on a 2-open / "
		"1-close template; got stderr=" + repr(stderr)
	)
	assert "open=2" in stderr or "opener(s)" in stderr


def test_interior_whitespace_markers_are_stripped() -> None:
	"""The marker regexes must tolerate whitespace inside the
	`{{ … }}` braces (e.g. `{{ # IF_VIOLATIONS }}`) so a future
	template edit with whitespace-bearing markers does not
	silently fall open. Without this, the suppression would skip
	and the timeout/error path would render the misleading body."""
	whitespace_template = (
		"=== H ==={{PREVIOUS_OUTCOME_NOTICE}}\n"
		"\n"
		"{{ # IF_VIOLATIONS }}\n"
		"VIOLATIONS_BODY_LINE_1: must be stripped\n"
		"{{ / IF_VIOLATIONS }}\n"
		"\n"
		"=== ORIGINAL TASK ===\n"
		"\n"
	)
	stdout, stderr, rc = _run_render(
		tpl_text=whitespace_template,
		suppress=True,
		outcome_notice="\n[notice]",
	)
	assert rc == 0, f"renderer returned non-zero: rc={rc}, stderr={stderr!r}"
	assert "VIOLATIONS_BODY_LINE_1" not in stdout, (
		"Body should be stripped on suppress=1 even with interior-"
		"whitespace markers; got:\n" + stdout
	)
	# No leftover warnings — both the strip and the post-strip
	# guard must accept the whitespace-bearing markers.
	assert "::warning::" not in stderr, (
		"Whitespace-tolerant markers should not trigger a leftover-"
		"marker warning; got stderr=" + repr(stderr)
	)


def main() -> int:
	test_outcome_ran_keeps_violations_body()
	test_outcome_timeout_suppresses_violations_body()
	test_outcome_error_suppresses_violations_body()
	test_outcome_notice_placeholder_substitutes_at_header_position()
	test_marker_count_assertion_warns_on_malformed_template()
	test_interior_whitespace_markers_are_stripped()
	print(
		"OK: review_conflict_resolve outcome-aware retry-prelude "
		"rendering contracts hold"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

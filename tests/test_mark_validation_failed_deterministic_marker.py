#!/usr/bin/env python3
"""Deterministic validation-failure marker match in mark_validation_failed().

BUG-003 (analysis/workflow-optimization-2026-10-07.md): the poller runs under
``set -euo pipefail``. The deterministic-failure short-circuit used to test
the marker with ``printf | grep -qE`` and extract the class with
``printf | grep -oE | head -n 1 | sed``. On a long failure comment ``grep -q``
or ``head`` could close the pipe early, the writer got SIGPIPE, and the
pipeline returned 141, so the short-circuit was skipped (or the assignment
tripped errexit). The block now uses one Bash ``[[ =~ ]]`` match and reads the
class from ``BASH_REMATCH[1]``.

``mark_validation_failed()`` has many dependencies, so these cases extract only
the match statements from the script text and run them in a small harness.

The cases run in CI through tests/test_alert_levels_human_only.py, which
collects them (ci.yml was outside this issue's file grant).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

MVF_REPO_ROOT = Path(__file__).resolve().parents[1]
MVF_POLLER_SCRIPT = MVF_REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"

MVF_STATEMENT_PREFIXES = (
	"local _deterministic_marker_re=",
	'if [[ "${reason}" =~ ${_deterministic_marker_re} ]]; then',
	'_deterministic_class="${BASH_REMATCH[1]}"',
)


def _mvf_function_source() -> str:
	script = MVF_POLLER_SCRIPT.read_text(encoding="utf-8")
	start = script.index("mark_validation_failed() {")
	end = script.index("\n}\n", start) + 3
	return script[start:end]


def _mvf_marker_statements() -> list[str]:
	lines = _mvf_function_source().splitlines()
	statements = []
	for prefix in MVF_STATEMENT_PREFIXES:
		matches = [line.strip() for line in lines if line.strip().startswith(prefix)]
		assert len(matches) == 1, (prefix, matches)
		statements.append(matches[0])
	return statements


def _run_marker_harness(reason_text: str) -> tuple[int, str, str]:
	declare_re, if_line, assign_line = _mvf_marker_statements()
	harness_body = (
		"set -euo pipefail\n"
		"mvf_probe() {\n"
		"  local reason\n"
		'  reason="$(cat "$1")"\n'
		'  local _deterministic_class=""\n'
		f"  {declare_re}\n"
		f"  {if_line}\n"
		f"    {assign_line}\n"
		"    printf 'MATCH:%s\\n' \"${_deterministic_class}\"\n"
		"  else\n"
		"    printf 'NOMATCH\\n'\n"
		"  fi\n"
		"}\n"
		'mvf_probe "$1"\n'
		"echo DONE\n"
	)
	with tempfile.TemporaryDirectory() as tmp:
		# The reason goes through a file: a single argv/env string over
		# 128 KiB fails with E2BIG on Linux.
		reason_file = Path(tmp) / "reason.txt"
		reason_file.write_text(reason_text, encoding="utf-8")
		harness = Path(tmp) / "harness.sh"
		harness.write_text(harness_body, encoding="utf-8")
		result = subprocess.run(["bash", str(harness), str(reason_file)], capture_output=True, text=True, check=False)
		return result.returncode, result.stdout, result.stderr


def _mvf_filler(lines: int) -> str:
	return "".join(f"validation log line {n:05d}: step output that is not a marker ...........\n" for n in range(lines))


def test_mvf_large_multiline_reason_marker_on_first_line_extracts_class():
	reason = "Validation failed. AI_VALIDATION_FAILURE_CLASS:deterministic_python_too_old\n" + _mvf_filler(8000)
	assert len(reason) >= 262144
	rc, out, err = _run_marker_harness(reason)
	assert rc == 0, err
	assert out.splitlines() == ["MATCH:deterministic_python_too_old", "DONE"]


def test_mvf_marker_mid_text_extracts_class():
	reason = _mvf_filler(50) + "cause: AI_VALIDATION_FAILURE_CLASS:deterministic_exit_code (runner)\n" + _mvf_filler(50)
	rc, out, err = _run_marker_harness(reason)
	assert rc == 0, err
	assert out.splitlines() == ["MATCH:deterministic_exit_code", "DONE"]


def test_mvf_two_markers_first_wins():
	reason = "a AI_VALIDATION_FAILURE_CLASS:deterministic_alpha.\n" + _mvf_filler(10) + "b AI_VALIDATION_FAILURE_CLASS:deterministic_beta\n"
	rc, out, err = _run_marker_harness(reason)
	assert rc == 0, err
	assert out.splitlines() == ["MATCH:deterministic_alpha", "DONE"]


def test_mvf_no_marker_no_match_and_no_errexit_escape():
	reason = _mvf_filler(8000) + "near miss AI_VALIDATION_FAILURE_CLASS:transient_x\n"
	assert len(reason) >= 262144
	rc, out, err = _run_marker_harness(reason)
	assert rc == 0, err
	assert out.splitlines() == ["NOMATCH", "DONE"]
	assert err == ""


def test_mvf_class_charset_stops_at_non_matching_char():
	rc, out, err = _run_marker_harness("x AI_VALIDATION_FAILURE_CLASS:deterministic_ab-CD9 y\n")
	assert rc == 0, err
	assert out.splitlines() == ["MATCH:deterministic_ab", "DONE"]


def test_mvf_old_pipeline_removed_from_mark_validation_failed():
	source = _mvf_function_source()
	for legacy in (
		"| grep -qE 'AI_VALIDATION_FAILURE_CLASS",
		"grep -oE 'AI_VALIDATION_FAILURE_CLASS",
		"sed 's/^AI_VALIDATION_FAILURE_CLASS://'",
	):
		assert legacy not in source, legacy
	assert "  local _deterministic_marker_re='AI_VALIDATION_FAILURE_CLASS:(deterministic_[a-z_]+)'\n" in source
	# The regex expansion must stay unquoted, or Bash matches it literally.
	assert '  if [[ "${reason}" =~ ${_deterministic_marker_re} ]]; then\n' in source
	assert '    _deterministic_class="${BASH_REMATCH[1]}"\n' in source


def test_mvf_deterministic_branch_followups_unchanged():
	source = _mvf_function_source()
	assert (
		'echo "Validation failed deterministically (class=${_deterministic_class}); skipping recovery budget '
		'(current=${val_recovery_count}/${MAX_VALIDATION_RECOVERY_ATTEMPTS})."'
	) in source
	assert '--arg dclass "${_deterministic_class}"' in source


def test_mvf_parity_with_legacy_pipeline_small_inputs():
	"""Short reasons (no SIGPIPE possible): the old pipeline and the new match agree."""
	legacy = (
		"printf '%s' \"$(cat \"$1\")\" | grep -oE 'AI_VALIDATION_FAILURE_CLASS:deterministic_[a-z_]+' "
		"| head -n 1 | sed 's/^AI_VALIDATION_FAILURE_CLASS://'"
	)
	for reason, expected in (
		("pre AI_VALIDATION_FAILURE_CLASS:deterministic_exit_code (runner)\npost\n", "deterministic_exit_code"),
		("AI_VALIDATION_FAILURE_CLASS:deterministic_alpha\nAI_VALIDATION_FAILURE_CLASS:deterministic_beta\n", "deterministic_alpha"),
		("AI_VALIDATION_FAILURE_CLASS:deterministic_ab-CD9\n", "deterministic_ab"),
	):
		with tempfile.TemporaryDirectory() as tmp:
			reason_file = Path(tmp) / "reason.txt"
			reason_file.write_text(reason, encoding="utf-8")
			old = subprocess.run(["bash", "-c", legacy, "mvf", str(reason_file)], capture_output=True, text=True, check=False)
		assert old.stdout.strip() == expected
		rc, out, err = _run_marker_harness(reason)
		assert rc == 0, err
		assert out.splitlines()[0] == f"MATCH:{expected}"

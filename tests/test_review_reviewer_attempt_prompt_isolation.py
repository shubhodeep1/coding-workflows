#!/usr/bin/env python3
"""Contract: reviewer attempt prompt copies must be namespaced per slot.

Background — the shared attempt-prompt race (all reviewers fail on empty stdin)
===============================================================================

``scripts/review_run_reviewers.sh`` fans the active reviewer model set out as
concurrent background workers (``run_reviewer ... &`` in ``run_reviewer_pass``).
Each attempt makes a private, mutable copy of the pass prompt so per-attempt
additions (nag block) never contaminate the shared base prompt::

    reviewer_attempt_prompt_file="${prompt_file}.<slot>_<pid>.attempt_${attempt_number}"

When no model-family overlay exists, ``prepare_reviewer_prompt_for_model``
returns the SHARED pass prompt path for every slot. Before the slot suffix was
added, every concurrent worker then derived the SAME attempt path
(``<pass prompt>.attempt_1``) and raced on it: ``cp`` truncates it in place,
the nag block appends to it, ``sanitize_codex_prompt_file`` rewrites it via
iconv → tmp → ``mv`` (and mv's an *empty* tmp over the path whenever iconv
reads mid-truncation, after which every later sanitize preserves the
emptiness), while each worker's model process reads it as stdin. One bad interleaving
left the file empty and every reviewer failed non-retryably with::

    Reading prompt from stdin...
    No prompt provided via stdin.

→ "Pass 2 complete: 0 reviewers successful." → "All reviewers failed." →
review_autofix job failure. Observed in consumer run
tele-funtoken-msg-scoring/actions/runs/32222803753 (PR #3721, pass 2: 6/6
reviewers failed identically ~1.5s after launch while the assembled pass-2
prompt file was 571935 bytes).

This test pins the two halves of the fix across the OpenCode cutover:

1. the attempt prompt path embeds the per-slot ``safe_name`` so concurrent
   slots never share a mutable attempt file;
2. the pre-launch guard restores/flags an unexpectedly empty effective prompt
   instead of letting the model process fail the slot silently.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_run_reviewers.sh"


def _script_text() -> str:
	return SCRIPT.read_text(encoding="utf-8")


def test_attempt_prompt_path_is_namespaced_per_slot() -> None:
	text = _script_text()
	assignments = re.findall(
		r'^\s*reviewer_attempt_prompt_file="([^"]+)"\s*$', text, flags=re.M
	)
	# The empty-string reset in the cp-failure fallback branch and in the
	# local declaration is fine; every non-empty assignment must carry both
	# the slot namespace and the attempt number.
	real = [a for a in assignments if a]
	assert real, "expected at least one reviewer_attempt_prompt_file assignment"
	for value in real:
		assert "${safe_name" in value, (
			"reviewer attempt prompt path must embed the per-slot safe_name; "
			"a shared '<prompt>.attempt_N' path is cp-truncated / sanitized / "
			f"read concurrently by every reviewer worker (got: {value!r})"
		)
		assert "${BASHPID" in value, (
			"reviewer attempt prompt path must include a per-worker shell pid "
			"so a fallback or future safe_name collision cannot alias slots "
			f"onto one mutable prompt file (got: {value!r})"
		)
		assert "attempt_${attempt_number}" in value, (
			f"attempt prompt path must stay per-attempt (got: {value!r})"
		)


def test_empty_effective_prompt_is_guarded_before_launch() -> None:
	text = _script_text()
	guard = re.search(
		r'if \[ ! -s "\$\{reviewer_effective_prompt_file\}" \]', text
	)
	assert guard, (
		"expected a pre-launch guard on an empty reviewer_effective_prompt_file "
		"(the model process fails a slot non-retryably on empty stdin)"
	)
	launch = text.find('-- "${reviewer_codex_cmd[@]}" < "${reviewer_effective_prompt_file}"')
	assert launch != -1, "expected the supervised OpenCode launch redirect to be present"
	assert guard.start() < launch, (
		"the empty-prompt guard must run before the OpenCode launch redirect"
	)
	guard_block = text[guard.start():launch]
	assert 'reviewer_effective_prompt_file="${prompt_file}"' in guard_block, (
		"the pre-launch guard should fall back to the base prompt when a damaged "
		"attempt copy cannot be restored"
	)
	assert "safe_name=${safe_name:-unset}" in text, (
		"empty/truncated prompt warnings should include safe_name for slot-to-file correlation"
	)
	assert "continuing with the base prompt to avoid empty stdin" in text, (
		"an unrestorable empty attempt prompt should fall back to the base prompt "
		"instead of launching OpenCode with empty stdin"
	)
	assert "refusing to launch OpenCode with empty stdin" in guard_block, (
		"if every fallback still leaves an empty prompt, the slot should fail before "
		"the OpenCode stdin redirect"
	)
	assert '"${reviewer_effective_prompt_bytes}" -lt "${reviewer_base_prompt_bytes}"' in text, (
		"the pre-launch guard should restore a non-empty but truncated attempt prompt"
	)
	command_start = text.find('reviewer_codex_cmd=(\n    bash -c')
	assert command_start != -1, "expected the OpenCode command-array assignment"
	command_block = text[command_start:launch]
	assert 'opencode_run_cmd "$@"' in command_block
	assert '"${attempt_reasoning}"' in command_block
	assert '"${reviewer_opencode_config_path}"' in command_block
	assert "reviewer_effective_prompt_file" not in command_block, (
		"prompt content/path must remain stdin-only and must not be embedded in OpenCode argv"
	)


# ---------------------------------------------------------------------------
# REVIEWER_ATTEMPT_RESULT log line (#8097)
# ---------------------------------------------------------------------------


NO_TEXT_LINE = "OpenCode structured reviewer output contained no text events."


def _extract_shell_function(text: str, name: str) -> str:
	start = text.find(f"{name}() {{\n")
	assert start != -1, f"function {name} not found"
	end = text.find("\n}\n", start)
	assert end != -1, f"function {name} has no closing brace"
	return text[start : end + 3]


def _run_class(tmp_path: Path, rc: str, wd: str, stderr: str | None, stall: str) -> str:
	fn = _extract_shell_function(_script_text(), "reviewer_attempt_result_failure_class")
	stderr_path = tmp_path / "stderr.txt"
	if stderr is not None:
		stderr_path.write_text(stderr, encoding="utf-8")
	script = f'set -euo pipefail\n{fn}\nreviewer_attempt_result_failure_class "$1" "$2" "$3" "$4"\n'
	proc = subprocess.run(
		["bash", "-c", script, "x", rc, wd, str(stderr_path), stall],
		capture_output=True,
		text=True,
		check=False,
	)
	assert proc.returncode == 0, proc.stderr
	lines = proc.stdout.splitlines()
	assert len(lines) == 1, proc.stdout
	return lines[0]


def _run_line(*args: str) -> str:
	fn = _extract_shell_function(_script_text(), "reviewer_attempt_result_line")
	script = f'set -euo pipefail\n{fn}\nreviewer_attempt_result_line "$@"\n'
	proc = subprocess.run(
		["bash", "-c", script, "x", *args],
		capture_output=True,
		text=True,
		check=False,
	)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.count("\n") == 1, proc.stdout
	return proc.stdout.rstrip("\n")


@pytest.mark.parametrize(
	("rc", "wd", "stderr", "stall", "expected"),
	[
		("0", "", "", "", "ok"),
		("0", "", NO_TEXT_LINE + "\n", "", "empty_structured_output"),
		("1", "", "noise\n" + NO_TEXT_LINE + "\n", "", "empty_structured_output"),
		("0", "", "unterminated stderr" + NO_TEXT_LINE + "\n", "", "empty_structured_output"),
		("0", "", NO_TEXT_LINE + " quoted later\n", "", "ok"),
		("143", "max_steps", NO_TEXT_LINE + "\n", "", "max_steps"),
		("1", "", NO_TEXT_LINE + "\n", "killed", "stall_guard"),
		("143", "tool_repeat", NO_TEXT_LINE + "\n", "", "tool_repeat"),
		("124", "", NO_TEXT_LINE + "\n", "", "timeout"),
		("1", "", "HTTP 429 Too Many Requests\n" + NO_TEXT_LINE + "\n", "", "rate_limit"),
		("1", "", "error: HTTP 400\n" + NO_TEXT_LINE + "\n", "", "provider_request_invalid"),
		("143", "max_steps", "", "", "max_steps"),
		("1", "", "", "killed", "stall_guard"),
		("1", "idle_timeout", "", "", "timeout"),
		("1", "max_wall", "", "", "timeout"),
		("124", "", "", "", "timeout"),
		("137", "", "", "", "timeout"),
		("143", "tool_repeat", "", "", "tool_repeat"),
		("1", "", "HTTP 429 Too Many Requests\n", "", "rate_limit"),
		("1", "", "503 Service Unavailable\n", "", "server_error"),
		("1", "", "error: HTTP 400 from provider\n", "", "provider_request_invalid"),
		("1", "", '{"type":"invalid_request_error"}\n', "", "provider_request_invalid"),
		(
			"1",
			"",
			"reasoning.effort is not allowed together with max_tokens\n",
			"",
			"provider_request_invalid",
		),
		("1", "", "something unrelated at line 400\n", "", "exit_nonzero"),
		("1", "", "process exited with exit code 400\n", "", "exit_nonzero"),
		("1", "", '{"error":{"code":400}}\n', "", "provider_request_invalid"),
		("1", "", None, "", "exit_nonzero"),
		("garbage", "", None, "", "ok"),
	],
)
def test_reviewer_attempt_result_failure_class(tmp_path, rc, wd, stderr, stall, expected):
	assert _run_class(tmp_path, rc, wd, stderr, stall) == expected


def test_reviewer_attempt_result_never_leaks_stderr(tmp_path) -> None:
	sentinel = "SENTINEL_SECRET_abc"
	cls = _run_class(tmp_path, "1", "", f"HTTP 400 {sentinel}\n", "")
	assert sentinel not in cls
	line = _run_line("pass2", "slot_a", "1", "openai/gpt-6-luna", "1", "5", "none", "", cls)
	assert sentinel not in line
	assert line == (
		"REVIEWER_ATTEMPT_RESULT pass=pass2 slot=slot_a attempt=1 model=openai/gpt-6-luna "
		"exit_code=1 duration_secs=5 wd_reason=none stall_state=none "
		"failure_class=provider_request_invalid"
	)


@pytest.mark.parametrize("model", ["bad model", "x;rm -rf", "$(id)", "a\nb", "", "`id`"])
def test_reviewer_attempt_result_unsafe_model_prints_unknown(model) -> None:
	line = _run_line("pass1", "s", "2", model, "1", "3", "max_wall", "observed", "timeout")
	assert " model=unknown " in line
	assert "\n" not in line


def test_reviewer_attempt_result_line_defaults() -> None:
	line = _run_line("", "", "", "", "x", "-4", "", "weird", "Bad Class!")
	assert line == (
		"REVIEWER_ATTEMPT_RESULT pass=unknown slot=unknown attempt=0 model=unknown "
		"exit_code=0 duration_secs=0 wd_reason=none stall_state=none failure_class=exit_nonzero"
	)


def test_reviewer_attempt_result_emitted_after_classifier_only_in_opencode_path() -> None:
	text = _script_text()
	classifier = text.find(
		'REVIEWER_ATTEMPT_RETRYABLE_CLASS="$(reviewer_classify_retryable_failure'
	)
	assert classifier != -1
	call = text.find('"$(reviewer_attempt_result_failure_class ', classifier)
	outcome = text.find('REVIEWER_ATTEMPT_OUTCOME="retryable_failure"', classifier)
	assert classifier < call < outcome
	pool = _extract_shell_function(text, "run_pool_reviewer")
	assert "REVIEWER_ATTEMPT_RESULT" not in pool
	assert "reviewer_attempt_result_" not in pool


def test_reviewer_attempt_result_emitted_on_success_path() -> None:
	text = _script_text()
	success_gate = text.find('if [ "${cmd_rc}" -eq 0 ] && [ -s "${tmp_output}" ]; then')
	assert success_gate != -1
	silent = text.find('REVIEWER_ATTEMPT_OUTCOME="silent_retry"', success_gate)
	success = text.find('REVIEWER_ATTEMPT_OUTCOME="success"', success_gate)
	call = text.find('"$(reviewer_attempt_result_failure_class ', success_gate)
	emit = text.find('reviewer_attempt_result_text="$(reviewer_attempt_result_line ', success_gate)
	assert success_gate < call < emit < silent < success


def test_reviewer_attempt_result_emitted_on_prelaunch_failures() -> None:
	text = _script_text()
	attempt = _extract_shell_function(text, "execute_reviewer_attempt")
	for marker in (
		"failed to generate its OpenCode config on",
		"failed OpenCode bootstrap validation on",
		"refusing to launch OpenCode with empty stdin",
	):
		pos = attempt.find(marker)
		assert pos != -1, marker
		ret = attempt.find("return 0", pos)
		emit = attempt.find("reviewer_attempt_result_emit_prelaunch 1", pos)
		assert pos < emit < ret, marker


def test_reviewer_attempt_result_prelaunch_helper_line(tmp_path) -> None:
	text = _script_text()
	line_fn = _extract_shell_function(text, "reviewer_attempt_result_line")
	emit_fn = _extract_shell_function(text, "reviewer_attempt_result_emit_prelaunch")
	log = tmp_path / "slot.log"
	script = (
		f"set -euo pipefail\n{line_fn}\n{emit_fn}\n"
		'output_prefix=pass1 safe_name=slot_b attempt_number=2 effective_model="bad model" '
		f'log_file="{log}" start_time=""\n'
		"reviewer_attempt_result_emit_prelaunch 1\n"
	)
	proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
	assert proc.returncode == 0, proc.stderr
	expected = (
		"REVIEWER_ATTEMPT_RESULT pass=pass1 slot=slot_b attempt=2 model=unknown "
		"exit_code=1 duration_secs=0 wd_reason=none stall_state=none failure_class=exit_nonzero"
	)
	assert proc.stdout.strip() == expected
	assert log.read_text(encoding="utf-8").strip() == expected

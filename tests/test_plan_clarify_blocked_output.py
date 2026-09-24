#!/usr/bin/env python3
"""Regression checks for BLOCKED output handling in plan/clarify workflows."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
import importlib.util
import http.client
import http.server
import threading
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
PLAN_RUNNER = REPO_ROOT / "scripts" / "run_plan_codex.sh"
CLARIFY_WF = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
PROMPT_PLAN = REPO_ROOT / "prompts" / "mode-plan.txt"
PROMPT_CLARIFY = REPO_ROOT / "prompts" / "mode-clarify.txt"

FENCE_SANITIZER = r"s{(^|\n)([ \t]*((?:```|~~~))[^\n]*\n.*?\n[ \t]*\3[ \t]*(?=\n|$))}{$1}gms;"
BLOCKED_RE = re.compile(r"^\s*BLOCKED:\s*(.*\S)\s*$", re.IGNORECASE)
SELF_CHECK_PASS_RE = re.compile(r"^\s*PLAN_SELF_CHECK:\s*PASS\s*$", re.IGNORECASE)
SELF_CHECK_WARNING_RE = re.compile(
	r"^\s*PLAN_SELF_CHECK:\s*WARNING:\s*(.*\S)\s*$",
	re.IGNORECASE,
)
SELF_CHECK_BLOCKER_RE = re.compile(r"^\s*PLAN_SELF_CHECK:\s*BLOCKER:\s*(.*\S)\s*$", re.IGNORECASE)
STATUS_NEEDS_CLARIFICATION_RE = re.compile(
	r"STATUS:.*NEEDS_CLARIFICATION|^\*\*STATUS:\*\*.*NEEDS_CLARIFICATION",
	re.IGNORECASE,
)
Q_LINE_RE = re.compile(r"^\s*(?:\*\*)?Q[0-9]+(?:\*\*)?:(?:\*\*)?\s+", re.IGNORECASE)
CHOICES_RE = re.compile(r"^\s*Choices:\s*$", re.IGNORECASE)
RECOMMENDED_CHOICE_RE = re.compile(
	r"^\s*-\s*(?:\*\*)?[A-Za-z](?:\+[A-Za-z])*(?:\*\*)?\s*(?:—|–|[-)\.:]).*\(RECOMMENDED\)",
	re.IGNORECASE,
)


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _sanitize_plan_output(raw_output: str) -> str:
	with tempfile.TemporaryDirectory() as td:
		source_path = Path(td) / "codex_output.txt"
		source_path.write_text(raw_output, encoding="utf-8")
		result = subprocess.run(
			["perl", "-0pe", FENCE_SANITIZER, str(source_path)],
			capture_output=True,
			check=True,
			encoding="utf-8",
		)
		return result.stdout


def _blocked_reason(parsed_output: str) -> str:
	for line in parsed_output.splitlines():
		match = BLOCKED_RE.match(line)
		if match:
			return match.group(1)
	return ""


def _self_check_summary(parsed_output: str, *, self_check_gate_enabled: bool) -> dict[str, str | int | bool]:
	pass_count = 0
	warning_count = 0
	blocker_count = 0
	for line in parsed_output.splitlines():
		if SELF_CHECK_PASS_RE.match(line):
			pass_count += 1
		elif SELF_CHECK_WARNING_RE.match(line):
			warning_count += 1
		elif SELF_CHECK_BLOCKER_RE.match(line):
			blocker_count += 1

	state = "missing"
	observation = "none"
	if blocker_count > 0:
		state = "blocker"
		if pass_count > 0 or warning_count > 0:
			observation = "mixed"
	elif warning_count > 0:
		state = "warning"
		if pass_count > 0:
			observation = "mixed"
	elif pass_count == 1:
		state = "pass"
	elif pass_count > 1:
		state = "pass"
		observation = "duplicate-pass"

	reopen = self_check_gate_enabled and blocker_count > 0
	if state == "missing" and self_check_gate_enabled:
		observation = "missing"

	return {
		"pass_count": pass_count,
		"warning_count": warning_count,
		"blocker_count": blocker_count,
		"state": state,
		"observation": observation,
		"reopen": reopen,
	}


def _has_structured_clarification_block(parsed_output: str) -> bool:
	q_line: int | None = None
	for line_number, line in enumerate(parsed_output.splitlines(), start=1):
		if Q_LINE_RE.match(line):
			q_line = line_number
			continue
		if q_line is None or line_number - q_line > 20:
			continue
		if CHOICES_RE.match(line) or RECOMMENDED_CHOICE_RE.match(line):
			return True
	return False


def _has_answerable_clarification(parsed_output: str) -> bool:
	has_status_needs_clarification = any(
		STATUS_NEEDS_CLARIFICATION_RE.search(line) for line in parsed_output.splitlines()
	)
	return has_status_needs_clarification or _has_structured_clarification_block(parsed_output)


def _needs_clarification(parsed_output: str, *, self_check_gate_enabled: bool) -> bool:
	# Mirrors the plan.yml routing: needs_clarification is true only when
	# something answerable exists. A self-check BLOCKER without answerable
	# questions routes to the blocked path instead (see
	# _self_check_blocker_routes_to_blocked).
	del self_check_gate_enabled
	return _has_answerable_clarification(parsed_output)


def _self_check_blocker_routes_to_blocked(parsed_output: str, *, self_check_gate_enabled: bool) -> bool:
	self_check_reopen = _self_check_summary(
		parsed_output,
		self_check_gate_enabled=self_check_gate_enabled,
	)["reopen"]
	return bool(self_check_reopen) and not _has_answerable_clarification(parsed_output)


def _first_self_check_blocker_reason(parsed_output: str) -> str:
	for line in parsed_output.splitlines():
		match = SELF_CHECK_BLOCKER_RE.match(line)
		if match:
			return match.group(1)
	return ""


def test_prompt_contract_includes_blocked_rule() -> None:
	plan_prompt = _read(PROMPT_PLAN)
	clarify_prompt = _read(PROMPT_CLARIFY)

	assert "emit exactly `BLOCKED: <short reason>`" in plan_prompt
	assert "## Pre-execution self-check" in plan_prompt
	assert "PLAN_SELF_CHECK: PASS" in plan_prompt
	assert "PLAN_SELF_CHECK: WARNING:" in plan_prompt
	assert "PLAN_SELF_CHECK: BLOCKER:" in plan_prompt
	assert "STATUS: NOT_CLEAR" in plan_prompt
	assert "emit exactly `BLOCKED: <short reason>`" in clarify_prompt
	assert "BLOCKED: <short reason>" in clarify_prompt


def test_plan_workflow_detects_blocked_before_needs_clarification() -> None:
	wf = _read(PLAN_WF)
	plan_runner = _read(PLAN_RUNNER)
	blocked_section = wf.split('BLOCKED_REASON="$(perl -ne \'', 1)[1].split(
		'if [ -n "${BLOCKED_REASON}" ]; then', 1
	)[0]

	assert "- name: Parse planning output" in wf
	assert "PLAN_SELF_CHECK_ENABLED: ${{ vars.PLAN_SELF_CHECK_ENABLED || 'true' }}" in wf
	assert "if (/^\\s*BLOCKED:\\s*(.*\\S)\\s*$/i)" in wf
	assert "echo \"blocked=true\" >> \"$GITHUB_OUTPUT\"" in wf
	assert "${CODEX_OUTPUT_PARSE_FILE}" in blocked_section
	assert "${CODEX_OUTPUT_FILE}" not in blocked_section
	assert "$in_code" not in blocked_section
	assert "7. A pre-execution self-check result" in plan_runner
	assert "PLAN_SELF_CHECK: PASS" in plan_runner
	assert "PLAN_SELF_CHECK: WARNING:" in plan_runner
	assert "PLAN_SELF_CHECK: BLOCKER:" in plan_runner
	assert 'CODEX_OUTPUT_PARSE_FILE="${RUNTIME_DIR}/codex_output_parse.txt"' in wf
	assert "malformed fences do not" in wf
	assert '::error::Failed to sanitize Codex output' in wf
	assert 'if [ ! -s "${CODEX_OUTPUT_PARSE_FILE}" ]; then' in wf
	assert '::error::Sanitized Codex output parse file missing or empty' in wf
	assert "SELF_CHECK_PASS_COUNT" in wf
	assert "SELF_CHECK_WARNING_COUNT" in wf
	assert "SELF_CHECK_BLOCKER_COUNT" in wf
	assert "plan_self_check_state" in wf
	assert "plan_self_check_observation" in wf
	assert "plan_self_check_reopen_clarification" in wf
	assert 'PARSE_SOURCE_FILE="${CODEX_OUTPUT_PARSE_FILE:-${CODEX_OUTPUT_FILE}}"' in wf
	assert "- name: Handle blocked planning output" in wf
	assert "steps.parse_plan.outputs.blocked == 'true'" in wf
	assert "--add-label 'ai:blocked'" in wf
	assert "--remove-label 'ai:planning'" in wf
	assert "--remove-label 'ai:clarification'" in wf
	assert 'index("ai:blocked") != null' in wf
	assert "--remove-label 'ai:blocked'" in wf
	assert "--status \"blocked\"" in wf
	assert '[ "${PLAN_SELF_CHECK_GATE_ENABLED}" = "true" ] && [ "${SELF_CHECK_BLOCKER_COUNT}" -gt 0 ]' in wf
	assert '[ "${HAS_STATUS_NEEDS_CLARIFICATION}" = "true" ] || [ "${HAS_STRUCTURED_CLARIFICATION_BLOCK}" = "true" ]' in wf
	assert 'elif [ "${SELF_CHECK_REOPEN_CLARIFICATION}" = "true" ]; then' in wf
	assert "SELF_CHECK_BLOCKED_REASON" in wf
	assert "steps.parse_plan.outputs.blocked != 'true' && steps.parse_plan.outputs.needs_clarification == 'true'" in wf


def test_plan_workflow_includes_ref_context_and_mismatch_rule() -> None:
	wf = _read(PLAN_WF)
	plan_runner = _read(PLAN_RUNNER)

	assert "- name: Capture planning ref context" in wf
	assert "git rev-parse HEAD" in wf
	assert "git symbolic-ref --short -q HEAD" in wf
	assert "PLANNING_REF_INTEGRATION_BRANCH_META" in wf
	assert "PLANNING REF CONTEXT" in wf
	assert "If checked-out ref mismatches Integration branch metadata, emit exactly" in plan_runner
	assert "`BLOCKED: integration branch mismatch`" in plan_runner


def test_clarify_workflow_detects_and_escalates_blocked_output() -> None:
	wf = _read(CLARIFY_WF)

	assert "- name: Parse Codex output" in wf
	assert "if (/^\\s*BLOCKED:\\s*(.*\\S)\\s*$/i)" in wf
	assert "echo \"blocked=true\" >> \"$GITHUB_OUTPUT\"" in wf
	assert "- name: Handle blocked clarification output" in wf
	assert "steps.parse_codex.outputs.blocked == 'true'" in wf
	assert "--add-label 'ai:blocked'" in wf
	assert "--remove-label 'ai:clarification'" in wf
	assert "--remove-label 'ai:planning'" in wf
	assert "steps.clarify_route.outputs.skip_codex != 'true' && steps.parse_codex.outputs.blocked != 'true' && steps.parse_codex.outputs.needs_clarification == 'true'" in wf
	assert "OUTCOME=\"blocked\"" in wf


def test_clarify_agent_runs_only_in_isolated_container() -> None:
	wf = _read(CLARIFY_WF)
	runner = _read(REPO_ROOT / "scripts" / "clarify_isolated_run.sh")
	assert 'bash scripts/clarify_isolated_run.sh "${CODEX_PROMPT_FILE}" "${CODEX_OUTPUT_FILE}" "${RUNTIME_DIR}/codex_log.txt"' in wf
	assert "codex_helpers.sh clarify_isolated_run.sh clarify_openrouter_broker.py; do" in wf
	assert 'install -m 0644 "${sandbox_src}" scripts/clarify_sandbox/Dockerfile' in wf
	assert "--network none --read-only --cap-drop ALL --security-opt no-new-privileges" in runner
	assert "--sandbox read-only" in runner
	assert 'type=bind,src=${run_root}/source,dst=/source,readonly' in runner
	assert 'type=bind,src=${run_root}/results,dst=/results' in runner
	assert "--env CLARIFY_PROXY_KEY=isolated-placeholder" in runner
	assert "--env OPENROUTER_API_KEY" not in runner
	assert "--mount type=bind,src=${GITHUB_WORKSPACE}" not in runner
	assert '".git"' in runner and '".env"' in runner and 'os.O_NOFOLLOW' in runner
	assert 'trap cleanup EXIT' in runner and "trap 'exit 143' TERM" in runner
	assert 'docker rm -f "${container_name}"' in runner
	assert "--sandbox danger-full-access" not in wf.split("- name: Run Codex", 1)[1]
	assert "if cat \"${CODEX_PROMPT_FILE}\" | codex" not in wf
	assert 'max_attempts=3' in wf
	assert 'CODEX_OUTPUT_FILE' in wf and 'codex_log.txt' in wf


def test_clarify_broker_rejects_other_routes_and_streams_without_leaking_key() -> None:
	spec = importlib.util.spec_from_file_location("clarify_broker", REPO_ROOT / "scripts" / "clarify_openrouter_broker.py")
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	seen = []

	class FakeResponse:
		status = 200

		def __init__(self):
			self.chunks = iter([b'data: first\n\n', b'data: second\n\n'])

		def getheader(self, name, default):
			return "text/event-stream" if name == "Content-Type" else default

		def read1(self, _size):
			return next(self.chunks, b"")

	class FakeUpstream:
		def __init__(self, host, **_kwargs):
			assert host == "openrouter.ai"

		def request(self, method, path, body, headers):
			if body == b'{"model":"openai/gpt-6-sol","fail":true}':
				raise OSError("private-sentinel")
			seen.append((method, path, body, headers))

		def getresponse(self):
			return FakeResponse()

		def close(self):
			pass

	with tempfile.TemporaryDirectory() as td, mock.patch.object(module.http.client, "HTTPSConnection", FakeUpstream):
		broker = module.UnixHTTPServer(str(Path(td) / "broker.sock"), module.Relay)
		broker.mode = "broker"
		broker.api_key = "private-sentinel"
		broker.model = "openai/gpt-6-sol"
		bridge = http.server.HTTPServer(("127.0.0.1", 0), module.Relay)
		bridge.mode = "bridge"
		bridge.socket_path = str(Path(td) / "broker.sock")
		threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (broker, bridge)]
		for thread in threads:
			thread.start()
		try:
			connection = http.client.HTTPConnection("127.0.0.1", bridge.server_address[1], timeout=5)
			connection.request("GET", "/api/v1/responses")
			get_result = connection.getresponse()
			assert get_result.status == 405
			get_result.read()
			connection.request("POST", "http://evil.invalid/api/v1/responses", b"{}", {"Content-Type": "application/json", "Authorization": "Bearer isolated-placeholder"})
			bad_path_result = connection.getresponse()
			assert bad_path_result.status == 400
			bad_path_result.read()
			connection.request("POST", "/api/v1/responses", b"{}", {"Content-Type": "application/json", "Authorization": "Bearer wrong"})
			bad_auth_result = connection.getresponse()
			assert bad_auth_result.status == 400
			bad_auth_result.read()
			connection.request("POST", "/api/v1/responses", b'{"model":"other/model"}', {"Content-Type": "application/json", "Authorization": "Bearer isolated-placeholder"})
			bad_model_result = connection.getresponse()
			assert bad_model_result.status == 400
			bad_model_result.read()
			connection.request("POST", "/api/v1/responses", b"{}", {"Content-Type": "application/json", "Authorization": "Bearer isolated-placeholder", "Content-Length": str(module.MAX_BODY + 1)})
			oversized_result = connection.getresponse()
			assert oversized_result.status == 400
			oversized_result.read()
			connection.close()
			connection = http.client.HTTPConnection("127.0.0.1", bridge.server_address[1], timeout=5)
			connection.request("POST", "/api/v1/responses", b'{"model":"openai/gpt-6-sol"}', {"Content-Type": "application/json", "Authorization": "Bearer isolated-placeholder"})
			response = connection.getresponse()
			assert response.status == 200
			assert response.read() == b'data: first\n\ndata: second\n\n'
			assert len(seen) == 1
			assert seen[0] == ("POST", "/api/v1/responses", b'{"model":"openai/gpt-6-sol"}', {"Content-Type": "application/json", "Authorization": "Bearer private-sentinel"})
			assert "private-sentinel" not in str(response.headers)
			connection.request("POST", "/api/v1/responses", b'{"model":"openai/gpt-6-sol","fail":true}', {"Content-Type": "application/json", "Authorization": "Bearer isolated-placeholder"})
			failed_response = connection.getresponse()
			assert failed_response.status == 502
			assert b"private-sentinel" not in failed_response.read()
		finally:
			connection.close()
			for server in (bridge, broker):
				server.shutdown()
				server.server_close()
			for thread in threads:
				thread.join(timeout=5)


def test_unterminated_fence_does_not_hide_blocked_reason() -> None:
	parsed_output = _sanitize_plan_output(
		"```text\nexample\nBLOCKED: integration branch mismatch\n"
	)

	assert _blocked_reason(parsed_output) == "integration branch mismatch"


def test_unterminated_fence_reopens_clarification_for_self_check_blocker() -> None:
	parsed_output = _sanitize_plan_output(
		"```python\nprint('demo')\nPLAN_SELF_CHECK: BLOCKER: missing files_touched list\n"
	)

	summary_enabled = _self_check_summary(parsed_output, self_check_gate_enabled=True)
	summary_disabled = _self_check_summary(parsed_output, self_check_gate_enabled=False)

	assert summary_enabled["blocker_count"] == 1
	assert summary_enabled["state"] == "blocker"
	assert summary_enabled["observation"] == "none"
	assert summary_enabled["reopen"] is True
	assert summary_disabled["reopen"] is False
	# The self-check gate itself still reports reopen=True — that flag
	# (plan_self_check_reopen_clarification) is what this test's name
	# refers to, and it is asserted above. The workflow-level routing,
	# however, now sends a blocker with no answerable Q-ID block to the
	# blocked path instead of reopening clarification (which would loop:
	# orchestrator auto-answer finds no Q-ID blocks and stall recovery
	# re-answers forever).
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=False) is False
	assert _self_check_blocker_routes_to_blocked(parsed_output, self_check_gate_enabled=True) is True
	assert _self_check_blocker_routes_to_blocked(parsed_output, self_check_gate_enabled=False) is False
	assert _first_self_check_blocker_reason(parsed_output) == "missing files_touched list"


def test_self_check_blocker_with_q_id_block_still_reopens_clarification() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\n"
		"PLAN_SELF_CHECK: BLOCKER: destructive guard is active\n"
		"STATUS: NOT_CLEAR\n"
		"Q1: Should the guard be cleared before implementation?\n"
		"Choices:\n"
		"- A - clear the guard and proceed (RECOMMENDED)\n"
	)

	assert _has_structured_clarification_block(parsed_output) is True
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is True
	assert _self_check_blocker_routes_to_blocked(parsed_output, self_check_gate_enabled=True) is False


def test_self_check_blocker_without_questions_routes_to_blocked() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\n"
		"PLAN_SELF_CHECK: BLOCKER: Issue retains ai:destructive-blocked and ALLOW_BULK_DELETE is not configured\n"
		"STATUS: NOT_CLEAR\n"
	)

	assert _has_structured_clarification_block(parsed_output) is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False
	assert _self_check_blocker_routes_to_blocked(parsed_output, self_check_gate_enabled=True) is True
	assert (
		_first_self_check_blocker_reason(parsed_output)
		== "Issue retains ai:destructive-blocked and ALLOW_BULK_DELETE is not configured"
	)


def test_pass_self_check_stays_clear() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\nPLAN_SELF_CHECK: PASS\nSTATUS: CLEAR\n"
	)

	summary = _self_check_summary(parsed_output, self_check_gate_enabled=True)

	assert summary["pass_count"] == 1
	assert summary["warning_count"] == 0
	assert summary["blocker_count"] == 0
	assert summary["state"] == "pass"
	assert summary["observation"] == "none"
	assert summary["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False


def test_warning_only_self_check_remains_fail_open() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\nPLAN_SELF_CHECK: WARNING: confirm rollout note\nSTATUS: CLEAR\n"
	)

	summary = _self_check_summary(parsed_output, self_check_gate_enabled=True)

	assert summary["pass_count"] == 0
	assert summary["warning_count"] == 1
	assert summary["blocker_count"] == 0
	assert summary["state"] == "warning"
	assert summary["observation"] == "none"
	assert summary["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False


def test_mixed_pass_and_warning_self_check_is_observability_only() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\nPLAN_SELF_CHECK: PASS\nPLAN_SELF_CHECK: WARNING: confirm rollout note\nSTATUS: CLEAR\n"
	)

	summary = _self_check_summary(parsed_output, self_check_gate_enabled=True)

	assert summary["pass_count"] == 1
	assert summary["warning_count"] == 1
	assert summary["blocker_count"] == 0
	assert summary["state"] == "warning"
	assert summary["observation"] == "mixed"
	assert summary["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False


def test_duplicate_pass_self_check_is_observability_only() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\nPLAN_SELF_CHECK: PASS\nPLAN_SELF_CHECK: PASS\nSTATUS: CLEAR\n"
	)

	summary = _self_check_summary(parsed_output, self_check_gate_enabled=True)

	assert summary["pass_count"] == 2
	assert summary["warning_count"] == 0
	assert summary["blocker_count"] == 0
	assert summary["state"] == "pass"
	assert summary["observation"] == "duplicate-pass"
	assert summary["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False


def test_missing_self_check_is_observability_only() -> None:
	parsed_output = _sanitize_plan_output("Implementation Plan\nSTATUS: CLEAR\n")

	summary = _self_check_summary(parsed_output, self_check_gate_enabled=True)

	assert summary["pass_count"] == 0
	assert summary["warning_count"] == 0
	assert summary["blocker_count"] == 0
	assert summary["state"] == "missing"
	assert summary["observation"] == "missing"
	assert summary["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False


def test_unterminated_fence_does_not_hide_structured_clarification_block() -> None:
	parsed_output = _sanitize_plan_output(
		"```\nnotes\nQ1: Which branch should be used?\nChoices:\n- A - integration branch (RECOMMENDED)\n"
	)

	assert _has_structured_clarification_block(parsed_output) is True
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is True


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

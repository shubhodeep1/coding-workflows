#!/usr/bin/env python3
"""Regression checks for BLOCKED output handling in plan/clarify workflows."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path
import importlib.util
import http.client
import http.server
import threading
from unittest import mock

import yaml


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


def test_plan_prompt_blocks_work_delivered_by_an_open_pr() -> None:
	# tele-funtoken-msg-scoring#4992: a no-edit "track open PR #4991" plan was
	# auto-approved and implement, which can only change files, blocked on it.
	for prompt_path in (PROMPT_PLAN, REPO_ROOT / "prompts" / "_templates" / "mode-plan.txt"):
		plan_prompt = _read(prompt_path)
		assert "Implement only writes repository files and opens one pull request; it cannot post comments, close issues, or check or wait for another pull request, so never plan such steps." in plan_prompt
		assert "emit `PLAN_SELF_CHECK: BLOCKER: work is delivered by open PR #<N>; resume once it is merged` and end with `STATUS: NOT_CLEAR`" in plan_prompt
		assert "When that work is already on the planning ref, plan no edits: emit `PLAN_SELF_CHECK: BLOCKER: work is already on the planning ref; nothing to implement` and end with `STATUS: NOT_CLEAR`." in plan_prompt
	# The live planner prompt is the runner's inline heredoc, not prompts/mode-plan.txt.
	runner = " ".join(_read(PLAN_RUNNER).split())
	assert "Implement only writes repository files and opens one pull request; it cannot post comments, close issues, or check or wait for another pull request, so never plan such steps." in runner
	assert "emit `PLAN_SELF_CHECK: BLOCKER: work is delivered by open PR #<N>; resume once it is merged` and end with `STATUS: NOT_CLEAR`" in runner
	assert "emit `PLAN_SELF_CHECK: BLOCKER: work is already on the planning ref; nothing to implement` and end with `STATUS: NOT_CLEAR`" in runner


def test_open_pr_blocker_routes_to_blocked() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\n"
		"PLAN_SELF_CHECK: BLOCKER: work is delivered by open PR #4991; resume once it is merged\n"
		"STATUS: NOT_CLEAR\n"
	)

	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False
	assert _self_check_blocker_routes_to_blocked(parsed_output, self_check_gate_enabled=True) is True
	assert _first_self_check_blocker_reason(parsed_output) == "work is delivered by open PR #4991; resume once it is merged"


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
	# Issue #7172: the rule keys on the workflow's computed line, not on git
	# inside the model's snapshot workspace (branch `isolated`).
	for prompt_text in (plan_runner, _read(PROMPT_PLAN)):
		assert "If the `Integration branch mismatch:` line in PLANNING REF CONTEXT reads\n  `true`, emit exactly `BLOCKED: integration branch mismatch`." in prompt_text
		assert "snapshot on a local branch named `isolated`" in prompt_text
	assert "plan_output_contradicts_ref_context" in plan_runner


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
	assert 'bash "${SUPPORT_SCRIPTS_DIR:-scripts}/clarify_isolated_run.sh" "${CODEX_PROMPT_FILE}" "${CODEX_OUTPUT_FILE}" "${RUNTIME_DIR}/codex_log.txt"' in wf
	assert "codex_helpers.sh clarify_isolated_run.sh clarify_base_image_preseed.sh sandbox_image.sh clarify_openrouter_broker.py security_dependency.py auto_decisions.py orchestrate_parse_and_post_answer.sh ai_engine.sh claude_engine.py claude_anthropic_relay.py claude_settings.json.tmpl workflow_failure_heal.py workflow_failure_heal_evidence.sh; do" in wf
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
	assert 'mkdir -m 0700 "${run_root}/socket"' in runner
	assert 'os.chmod(sys.argv[2], 0o600)' in _read(REPO_ROOT / "scripts" / "clarify_openrouter_broker.py")
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

# --- Phase 5a: the clarify, clarify-respond and plan roles on Claude -------------


ENGINE_WORKFLOWS = (
	("clarify.yml", "CLARIFY", "Run Codex"),
	("orchestrate_clarify_respond.yml", "CLARIFY_RESPOND", "Build prompt and run Codex"),
	("plan.yml", "PLAN", "Run Codex planning"),
)


def _steps(workflow: str) -> list[dict]:
	data = yaml.safe_load(_read(REPO_ROOT / ".github" / "workflows" / workflow))
	return data["jobs"]["respond" if workflow == "orchestrate_clarify_respond.yml" else workflow.removesuffix(".yml")]["steps"]


def test_engine_steps_resolve_the_role_and_guard_the_credential() -> None:
	for workflow, role, model_step in ENGINE_WORKFLOWS:
		steps = _steps(workflow)
		names = [step.get("name") for step in steps]
		resolve = names.index("Resolve AI engine")
		install = names.index("Install Claude Code CLI")
		credential = names.index("Resolve Claude credential")
		assert resolve < install < credential < names.index(model_step), workflow
		run = steps[resolve]["run"]
		assert f'engine="$(ai_engine_for_role {role} || echo codex)"' in run, workflow
		metadata_file = "ISSUE_PAYLOAD_FILE" if role == "CLARIFY_RESPOND" else "ISSUE_META_FILE"
		assert f'AI_ENGINE_LABELS="$(jq -c \'[.labels[]?.name]\' "${{{metadata_file}}}")"' in run, workflow
		assert "export AI_ENGINE_LABELS" in run, workflow
		assert steps[resolve]["env"][f"AI_ENGINE_{role}"] == f"${{{{ vars.AI_ENGINE_{role} || '' }}}}", workflow
		for index in (install, credential):
			assert "steps.ai_engine.outputs.engine == 'claude'" in steps[index]["if"], workflow
			assert steps[index]["continue-on-error"] is True, workflow
		assert steps[credential]["uses"] == "./.codex-workflow-src/.github/actions/claude-pool-token"


def test_clarify_runs_claude_then_the_unchanged_codex_call() -> None:
	wf = _read(REPO_ROOT / ".github" / "workflows" / "clarify.yml")
	args = '"${CODEX_PROMPT_FILE}" "${CODEX_OUTPUT_FILE}" "${RUNTIME_DIR}/codex_log.txt"'
	# clarify.yml runs the support bundle copy (stuck-states plan P2a).
	runner = 'bash "${SUPPORT_SCRIPTS_DIR:-scripts}/clarify_isolated_run.sh"'
	assert wf.count(runner + " ") == 2
	assert "bash scripts/clarify_isolated_run.sh" not in wf
	assert f"{runner} {args} claude CLARIFY || rc=$?" in wf
	assert f"{runner} {args} || rc=$?" in wf
	assert 'if [ "${rc}" -eq 75 ]; then' in wf
	assert "CLARIFY_ENGINE: ${{ steps.ai_engine.outputs.engine || 'codex' }}" in wf


def test_checked_in_defaults_put_the_5a_roles_on_claude() -> None:
	import json

	config = json.loads(_read(REPO_ROOT / ".github" / "ai" / "claude_engine.json"))
	for _, role, _ in ENGINE_WORKFLOWS:
		assert config["role_defaults"][role]["engine"] == "claude", role


def _clarify_loop() -> str:
	(step,) = [step for step in _steps("clarify.yml") if step.get("name") == "Run Codex"]
	run = step["run"]
	start = run.index("max_attempts=3")
	end = run.index("done", start) + len("done")
	return run[start:end]


def _run_clarify_loop(tmp: Path, engine: str, claude_rc: int) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	(tmp / "scripts").mkdir()
	(tmp / "scripts" / "clarify_isolated_run.sh").write_text(
		f"""printf '%s\\n' "$*" >> "{tmp}/calls.log"
if [ "${{4:-}}" = claude ]; then
  [ {claude_rc} -eq 0 ] && printf 'claude questions\\n' > "$2"
  exit {claude_rc}
fi
printf 'codex questions\\n' > "$2"
""",
		encoding="utf-8",
	)
	script = "set -euo pipefail\nsleep() { :; }\n" + _clarify_loop()
	env = {"PATH": "/usr/bin:/bin", "CLARIFY_ENGINE": engine, "CODEX_PROMPT_FILE": str(tmp / "p.txt"), "CODEX_OUTPUT_FILE": str(tmp / "o.txt"), "RUNTIME_DIR": str(tmp)}
	result = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True, check=False)
	return result, _read(tmp / "calls.log").splitlines()


def test_clarify_loop_falls_back_to_codex_in_the_same_attempt() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_clarify_loop(Path(tmp), "claude", 75)
		assert result.returncode == 0, result.stderr
		assert [call.split()[3:] for call in calls] == [["claude", "CLARIFY"], []]
		assert _read(Path(tmp) / "o.txt") == "codex questions\n"
		assert "succeeded on attempt 1" in result.stdout


def test_clarify_loop_on_claude_never_calls_codex() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_clarify_loop(Path(tmp), "claude", 0)
		assert result.returncode == 0, result.stderr
		assert calls == [f"{tmp}/p.txt {tmp}/o.txt {tmp}/codex_log.txt claude CLARIFY"]
		assert _read(Path(tmp) / "o.txt") == "claude questions\n"
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_clarify_loop(Path(tmp), "codex", 0)
		assert calls == [f"{tmp}/p.txt {tmp}/o.txt {tmp}/codex_log.txt"]

# Clarify attempt failures name their cause on a line heal evidence keeps
# (#8602). btc_sweeper#248 failed clarify three times with "Claude clarify
# failed after 3 attempts." and the heal intake could not classify it: the raw
# job log writes the loop's warnings as "##[warning]...", which the intake's
# filter_log drops, and the runner's "CLAUDE_POOL run ... outcome= reason="
# line matches none of its patterns. The loop now prints
# CLARIFY_ENGINE_ATTEMPT_FAILED, which the unchanged filter keeps.
ATTEMPT_POOL_LINE = "CLAUDE_POOL run role=CLARIFY account=TEAM1 outcome=crashed reason=no_result exit_code=1"
ATTEMPT_STUB_RUNNER = r"""#!/usr/bin/env bash
# Stub of scripts/clarify_isolated_run.sh: $4 is "claude" on the Claude path.
engine="${4:-codex}"
case "${STUB_MODE}" in
  crash)
    echo "some cli noise" >&2
    echo "CLAUDE_POOL run role=CLARIFY account=TEAM1 outcome=crashed reason=no_result exit_code=1" >&2
    exit 1
    ;;
  fallback)
    if [ "${engine}" = "claude" ]; then
      echo "CLAUDE_POOL run role=CLARIFY account=TEAM1 outcome=crashed reason=no_result exit_code=1" >&2
      exit 75
    fi
    exit 1
    ;;
  spoof)
    echo 'CLAUDE_POOL run reason=$(id);::error::x outcome=::warning::y' >&2
    echo "CLAUDE_POOL run role=CLARIFY account=TEAM1 outcome=crashed reason=no_result exit_code=1" >&2
    exit 1
    ;;
  spoof_only)
    echo 'CLAUDE_POOL run reason=$(id);::error::x outcome=::warning::y' >&2
    exit 1
    ;;
  glued)
    printf 'cli line without newline' >&2
    echo "CLAUDE_POOL run role=CLARIFY account=TEAM1 outcome=crashed reason=no_result exit_code=1" >&2
    exit 1
    ;;
  empty)
    : > "$2"
    exit 0
    ;;
esac
echo "container failed to start" >&2
exit 1
"""


def _clarify_attempt_script() -> str:
	"""The attempt loop with its failure-line helpers, which sit just above max_attempts=3."""
	lines = _read(CLARIFY_WF).splitlines()
	starts = [i for i, line in enumerate(lines) if line.strip().startswith("# Issue #8602: each failed attempt")]
	# Without the helpers (before #8602) the loop alone still runs, so the
	# behavioural checks fail on the missing line rather than on extraction.
	start = starts[0] if starts else next(i for i, line in enumerate(lines) if line.strip() == "max_attempts=3")
	stop = next(i for i in range(start, len(lines)) if lines[i].strip() == "- name: Semantic cache store")
	end = max(i for i in range(start, stop) if lines[i].strip() == "done")
	return textwrap.dedent("\n".join(lines[start : end + 1])) + "\n"


def _run_clarify_attempts(tmp: Path, mode: str) -> subprocess.CompletedProcess[str]:
	(tmp / "scripts").mkdir()
	stub = tmp / "scripts" / "clarify_isolated_run.sh"
	stub.write_text(ATTEMPT_STUB_RUNNER, encoding="utf-8")
	stub.chmod(0o755)
	(tmp / "p.txt").write_text("prompt\n", encoding="utf-8")
	env = {
		"PATH": "/usr/bin:/bin",
		"STUB_MODE": mode,
		"CLARIFY_ENGINE": "claude",
		"RUNTIME_DIR": str(tmp),
		"CODEX_PROMPT_FILE": str(tmp / "p.txt"),
		"CODEX_OUTPUT_FILE": str(tmp / "o.txt"),
		"GITHUB_ENV": str(tmp / "github_env"),
	}
	script = "set -euo pipefail\nsleep() { :; }\n" + _clarify_attempt_script()
	return subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True, timeout=120, check=False)


def _attempt_failed_lines(stdout: str) -> list[str]:
	return [line for line in stdout.splitlines() if line.startswith("CLARIFY_ENGINE_ATTEMPT_FAILED ")]


def test_clarify_attempt_loop_keeps_its_messages_and_adds_the_failure_line() -> None:
	loop = _clarify_attempt_script()
	assert "CLARIFY_ENGINE_ATTEMPT_FAILED" in loop
	for field in ("pool_role=", "pool_account=", "pool_outcome=", "pool_reason=", "pool_exit_code="):
		assert field in loop
	assert 'echo "::warning::${clarify_engine_name} exited with code $rc on attempt ${attempt}."' in loop
	assert 'echo "::warning::${clarify_engine_name} returned empty output on attempt ${attempt}."' in loop
	assert 'echo "::error::${clarify_engine_name} clarify failed after ${max_attempts} attempts."' in loop
	# Only the give-up line is an error annotation; a retry that later
	# succeeds must not leave errors behind.
	assert loop.count("::error::") == 1


def test_crashed_clarify_attempts_name_the_pool_reason_in_heal_evidence() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result = _run_clarify_attempts(Path(tmp), "crash")
	assert result.returncode == 1, result.stdout + result.stderr
	failed = _attempt_failed_lines(result.stdout)
	assert len(failed) == 3, result.stdout
	for attempt, line in enumerate(failed, start=1):
		assert f"engine=Claude attempt={attempt} max_attempts=3 kind=nonzero_exit exit_code=1 " in line
		assert "pool_role=CLARIFY pool_account=TEAM1 pool_outcome=crashed pool_reason=no_result pool_exit_code=1" in line
	# The runner's stderr still reaches the job log unchanged.
	assert result.stderr.count("some cli noise") == 3
	assert result.stderr.count(ATTEMPT_POOL_LINE) == 3

	# The raw job log as the runner writes it (run 41287557900): the echoed
	# step script without the ANSI cyan prefix, the output with workflow
	# commands rendered as ##[...], then a keyword-free tail that pushes the
	# attempt lines out of filter_log's 400-line tail.
	stamp = "2026-11-01T08:14:52.4911812Z "
	raw = [stamp + "##[group]Run bash scripts/clarify_isolated_run.sh"]
	raw += [stamp + line for line in _clarify_attempt_script().splitlines()[:60]]
	raw += [stamp + "shell: /usr/bin/bash -e {0}", stamp + "##[endgroup]"]
	raw += [stamp + re.sub(r"^::(warning|error)::", r"##[\1]", line) for line in result.stdout.splitlines()]
	raw += [stamp + f"padding line {i}" for i in range(450)]
	spec = importlib.util.spec_from_file_location("workflow_failure_heal_8602", REPO_ROOT / "scripts" / "workflow_failure_heal.py")
	assert spec and spec.loader
	heal = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(heal)
	filtered = heal.filter_log("\n".join(raw) + "\n")
	kept = [line for line in filtered.splitlines() if "CLARIFY_ENGINE_ATTEMPT_FAILED" in line and "pool_reason=no_result" in line]
	assert len(kept) == 3, filtered


def test_clarify_attempt_failure_line_edge_cases() -> None:
	unknown = "pool_role=unknown pool_account=unknown pool_outcome=unknown pool_reason=unknown pool_exit_code=unknown"
	cases = {
		# Claude unavailable: the codex attempts carry no pool fields.
		"fallback": lambda line: "engine=Codex" in line and unknown in line,
		# The runner's own line is written last, so it wins over CLI noise.
		"spoof": lambda line: "pool_outcome=crashed pool_reason=no_result" in line,
		# A spoofed line cannot carry workflow commands or shell syntax.
		"spoof_only": lambda line: not any(bad in line for bad in ("$", ";", "::", "(", ")")),
		"empty": lambda line: "kind=empty_output exit_code=0" in line,
		# The CLI's unterminated last stderr line shares a line with the pool line.
		"glued": lambda line: "pool_outcome=crashed pool_reason=no_result pool_exit_code=1" in line,
		"nopool": lambda line: "engine=Claude" in line and "exit_code=1" in line and unknown in line,
	}
	for mode, check in cases.items():
		with tempfile.TemporaryDirectory() as tmp:
			result = _run_clarify_attempts(Path(tmp), mode)
		assert result.returncode == 1, (mode, result.stdout, result.stderr)
		failed = _attempt_failed_lines(result.stdout)
		assert len(failed) == 3, (mode, result.stdout)
		for line in failed:
			assert check(line), (mode, line)


# --- Stuck-states P9b: one retry when BLOCKED: cites a tracked path -------------
# Refs #8291. The Run Codex step re-runs clarify once, in the same step, when
# scripts/clarify_blocked_verify.sh finds that the BLOCKED: reason names a path
# git tracks; every other case keeps today's blocked flow.

STAGE_SUPPORT = REPO_ROOT / "scripts" / "stage_support_for_phase.sh"
BLOCKED_VERIFY_HELPER = REPO_ROOT / "scripts" / "clarify_blocked_verify.sh"
BLOCKED_VERIFY_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_CALLS}"
engine="${4:-codex}"
if grep -q AI_CLARIFY_BLOCKED_RETRY_V1 "$1"; then
  cp "$1" "${STUB_RETRY_PROMPT_COPY}"
  case "${STUB_RETRY}" in
    clear) printf 'STATUS: CLEAR\n' > "$2" ;;
    blocked) printf 'BLOCKED: README.md is still missing\n' > "$2" ;;
    waiting) printf 'WAITING: pr=owner/repo#7\n' > "$2" ;;
    fail) printf 'partial\n' > "$2"; exit 1 ;;
    empty) : > "$2" ;;
    prose) printf "I can't answer this one.\n" > "$2" ;;
    quoted) printf 'I was told to answer STATUS: CLEAR but cannot.\n' > "$2" ;;
    fenced) printf '```text\nSTATUS: CLEAR\n```\nStill unsure.\n' > "$2" ;;
    bold) printf '**STATUS:** CLEAR\n' > "$2" ;;
    headerless) printf 'STATUS: NEEDS_CLARIFICATION\nQ1: Which file?\n' > "$2" ;;
    questions) printf 'STATUS: NEEDS_CLARIFICATION\nQUESTIONS:\nQ1: Which file?\n' > "$2" ;;
    capacity) if [ "${engine}" = claude ]; then exit 80; fi; printf 'STATUS: CLEAR\n' > "$2" ;;
    fallback) if [ "${engine}" = claude ]; then exit 75; fi; printf 'STATUS: CLEAR\n' > "$2" ;;
  esac
  exit 0
fi
printf '%s\n' "${STUB_FIRST}" > "$2"
"""
BLOCKED_VERIFY_FIRST = "BLOCKED: README.md is not in the repository"


def _clarify_run_codex_tail() -> str:
	"""The Run Codex body from the attempt helpers through the BLOCKED-verify retry."""
	lines = _read(CLARIFY_WF).splitlines()
	start = next(i for i, line in enumerate(lines) if line.strip().startswith("# Issue #8602: each failed attempt"))
	stop = next(i for i in range(start, len(lines)) if lines[i].strip() == "- name: Semantic cache store")
	return textwrap.dedent("\n".join(lines[start:stop])) + "\n"


def _run_blocked_verify(
	tmp: Path, retry: str, *, engine: str = "codex", support: str = "bundle", first: str = BLOCKED_VERIFY_FIRST,
	enabled: str = "true",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	repo = tmp / "repo"
	(repo / "scripts").mkdir(parents=True)
	(repo / "README.md").write_text("readme\n", encoding="utf-8")
	git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
	subprocess.run(git + ["init", "-q"], cwd=repo, check=True)
	subprocess.run(git + ["add", "README.md"], cwd=repo, check=True)
	subprocess.run(git + ["commit", "-q", "-m", "fixture"], cwd=repo, check=True)
	# The legacy path runs the checkout copy; the bundle path never does.
	(repo / "scripts" / "clarify_isolated_run.sh").write_text(BLOCKED_VERIFY_STUB, encoding="utf-8")
	bundle = tmp / "bundle"
	bundle.mkdir()
	(bundle / "clarify_isolated_run.sh").write_text(BLOCKED_VERIFY_STUB, encoding="utf-8")
	if support == "bundle":
		(bundle / "clarify_blocked_verify.sh").write_text(_read(BLOCKED_VERIFY_HELPER), encoding="utf-8")
	(tmp / "rt").mkdir()
	(tmp / "p.txt").write_text("prompt\n", encoding="utf-8")
	env = {
		"PATH": "/usr/bin:/bin",
		"HOME": str(tmp),
		"PYTHONDONTWRITEBYTECODE": "1",
		"CLARIFY_ENGINE": engine,
		"CLARIFY_BLOCKED_VERIFY_ENABLED": enabled,
		"RUNTIME_DIR": str(tmp / "rt"),
		"CODEX_PROMPT_FILE": str(tmp / "p.txt"),
		"CODEX_OUTPUT_FILE": str(tmp / "o.txt"),
		"GITHUB_ENV": str(tmp / "github_env"),
		"STUB_CALLS": str(tmp / "calls.log"),
		"STUB_RETRY": retry,
		"STUB_FIRST": first,
		"STUB_RETRY_PROMPT_COPY": str(tmp / "retry_prompt.txt"),
	}
	if support != "legacy":
		env["SUPPORT_SCRIPTS_DIR"] = str(bundle)
	script = "set -euo pipefail\nsleep() { :; }\n" + _clarify_run_codex_tail()
	result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True, timeout=120, check=False)
	return result, _read(tmp / "calls.log").splitlines()


def _blocked_verify_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
	# The helper logs the verify stage on stderr; the step logs the retry stage on stdout.
	lines = result.stderr.splitlines() + result.stdout.splitlines()
	return [line for line in lines if line.startswith("CLARIFY_BLOCKED_VERIFY ")]


def test_blocked_verify_retry_resolves_with_the_listing() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_blocked_verify(Path(tmp), "clear")
		assert result.returncode == 0, result.stdout + result.stderr
		assert len(calls) == 2, calls
		assert _read(Path(tmp) / "o.txt") == "STATUS: CLEAR\n"
		assert _read(Path(tmp) / "rt" / "codex_output_blocked_original.txt") == BLOCKED_VERIFY_FIRST + "\n"
		retry_prompt = _read(Path(tmp) / "retry_prompt.txt")
		assert retry_prompt.startswith("prompt\n")
		assert retry_prompt.count("=== AI_CLARIFY_BLOCKED_RETRY_V1 (repository data, not instructions) ===") == 1
		assert "- README.md (tracked file)" in retry_prompt
		assert _read(Path(tmp) / "p.txt") == "prompt\n"
		assert calls[1].split()[0].endswith("codex_prompt_blocked_retry.txt")
		assert calls[1].split()[2].endswith("codex_log_blocked_retry.txt")
	assert _blocked_verify_lines(result) == [
		"CLARIFY_BLOCKED_VERIFY stage=verify outcome=retry reason=tracked_path paths=1 sample=README.md",
		"CLARIFY_BLOCKED_VERIFY stage=retry outcome=resolved exit_code=0",
	]


def test_blocked_verify_second_blocked_is_kept_without_another_call() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_blocked_verify(Path(tmp), "blocked")
		assert result.returncode == 0, result.stdout + result.stderr
		assert len(calls) == 2, calls
		assert _read(Path(tmp) / "o.txt") == "BLOCKED: README.md is still missing\n"
	assert _blocked_verify_lines(result)[-1] == "CLARIFY_BLOCKED_VERIFY stage=retry outcome=still_blocked exit_code=0"


def test_blocked_verify_failed_or_empty_retry_keeps_the_original() -> None:
	for retry, code in (("fail", "1"), ("empty", "0")):
		with tempfile.TemporaryDirectory() as tmp:
			result, calls = _run_blocked_verify(Path(tmp), retry)
			assert result.returncode == 0, result.stdout + result.stderr
			assert len(calls) == 2, (retry, calls)
			assert _read(Path(tmp) / "o.txt") == BLOCKED_VERIFY_FIRST + "\n", retry
		assert _blocked_verify_lines(result)[-1] == f"CLARIFY_BLOCKED_VERIFY stage=retry outcome=failed exit_code={code}"


def test_blocked_verify_prose_only_retry_keeps_the_original() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_blocked_verify(Path(tmp), "prose")
		assert result.returncode == 0, result.stdout + result.stderr
		assert len(calls) == 2, calls
		assert _read(Path(tmp) / "o.txt") == BLOCKED_VERIFY_FIRST + "\n"
	assert _blocked_verify_lines(result)[-1] == "CLARIFY_BLOCKED_VERIFY stage=retry outcome=failed exit_code=0"


def test_blocked_verify_status_in_prose_or_fence_keeps_the_original() -> None:
	for retry in ("quoted", "fenced"):
		with tempfile.TemporaryDirectory() as tmp:
			result, calls = _run_blocked_verify(Path(tmp), retry)
			assert result.returncode == 0, result.stdout + result.stderr
			assert len(calls) == 2, (retry, calls)
			assert _read(Path(tmp) / "o.txt") == BLOCKED_VERIFY_FIRST + "\n", retry
		assert _blocked_verify_lines(result)[-1] == "CLARIFY_BLOCKED_VERIFY stage=retry outcome=failed exit_code=0", retry


def test_blocked_verify_bold_status_line_resolves() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, _ = _run_blocked_verify(Path(tmp), "bold")
		assert result.returncode == 0, result.stdout + result.stderr
		assert _read(Path(tmp) / "o.txt") == "**STATUS:** CLEAR\n"
	assert _blocked_verify_lines(result)[-1] == "CLARIFY_BLOCKED_VERIFY stage=retry outcome=resolved exit_code=0"


def test_blocked_verify_needs_clarification_requires_questions() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, _ = _run_blocked_verify(Path(tmp), "headerless")
		assert result.returncode == 0, result.stdout + result.stderr
		assert _read(Path(tmp) / "o.txt") == BLOCKED_VERIFY_FIRST + "\n"
	assert _blocked_verify_lines(result)[-1] == "CLARIFY_BLOCKED_VERIFY stage=retry outcome=failed exit_code=0"
	with tempfile.TemporaryDirectory() as tmp:
		result, _ = _run_blocked_verify(Path(tmp), "questions")
		assert result.returncode == 0, result.stdout + result.stderr
		assert _read(Path(tmp) / "o.txt") == "STATUS: NEEDS_CLARIFICATION\nQUESTIONS:\nQ1: Which file?\n"
	assert _blocked_verify_lines(result)[-1] == "CLARIFY_BLOCKED_VERIFY stage=retry outcome=resolved exit_code=0"


def test_blocked_verify_claude_capacity_on_retry_keeps_the_original() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_blocked_verify(Path(tmp), "capacity", engine="claude")
		assert result.returncode == 0, result.stdout + result.stderr
		assert [call.split()[3:] for call in calls] == [["claude", "CLARIFY"], ["claude", "CLARIFY"]]
		assert _read(Path(tmp) / "o.txt") == BLOCKED_VERIFY_FIRST + "\n"
		github_env = Path(tmp) / "github_env"
		assert not github_env.exists() or "CLAUDE_CAPACITY_WAIT" not in _read(github_env)
	assert _blocked_verify_lines(result)[-1] == "CLARIFY_BLOCKED_VERIFY stage=retry outcome=failed exit_code=80"


def test_blocked_verify_claude_unavailable_retry_falls_back_to_codex() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_blocked_verify(Path(tmp), "fallback", engine="claude")
		assert result.returncode == 0, result.stdout + result.stderr
		assert [call.split()[3:] for call in calls] == [["claude", "CLARIFY"], ["claude", "CLARIFY"], []]
		assert _read(Path(tmp) / "o.txt") == "STATUS: CLEAR\n"
	assert _blocked_verify_lines(result)[-1] == "CLARIFY_BLOCKED_VERIFY stage=retry outcome=resolved exit_code=0"


def test_blocked_verify_skips_without_the_bundle_helper() -> None:
	for support, reason in (("legacy", "legacy_support"), ("missing", "helper_missing")):
		with tempfile.TemporaryDirectory() as tmp:
			result, calls = _run_blocked_verify(Path(tmp), "clear", support=support)
			assert result.returncode == 0, result.stdout + result.stderr
			assert len(calls) == 1, (support, calls)
			assert _read(Path(tmp) / "o.txt") == BLOCKED_VERIFY_FIRST + "\n"
		assert _blocked_verify_lines(result) == [
			f"CLARIFY_BLOCKED_VERIFY stage=verify outcome=skip reason={reason} paths=0 sample=none"
		], support


def test_blocked_verify_skip_line_is_silent_when_off_or_fenced() -> None:
	cases = (
		{"support": "legacy", "enabled": "false"},
		{"support": "missing", "enabled": "OFF"},
		{"support": "legacy", "first": "```text\nBLOCKED: README.md\n```\nSTATUS: CLEAR"},
	)
	for case in cases:
		with tempfile.TemporaryDirectory() as tmp:
			result, calls = _run_blocked_verify(Path(tmp), "clear", **case)
			assert result.returncode == 0, (case, result.stdout + result.stderr)
			assert len(calls) == 1, (case, calls)
		assert _blocked_verify_lines(result) == [], case


def test_blocked_verify_leaves_other_outputs_and_the_off_switch_alone() -> None:
	cases = (
		{"first": "STATUS: CLEAR"},
		{"first": "BLOCKED: need the production API key"},
		{"enabled": "false"},
	)
	for case in cases:
		with tempfile.TemporaryDirectory() as tmp:
			result, calls = _run_blocked_verify(Path(tmp), "clear", **case)
			assert result.returncode == 0, (case, result.stdout + result.stderr)
			assert len(calls) == 1, (case, calls)
			assert not (Path(tmp) / "rt" / "codex_prompt_blocked_retry.txt").exists(), case
		lines = _blocked_verify_lines(result)
		assert all("stage=retry" not in line for line in lines), (case, lines)


def test_blocked_verify_is_wired_and_staged() -> None:
	wf = _read(CLARIFY_WF)
	assert "CLARIFY_BLOCKED_VERIFY_ENABLED: ${{ vars.CLARIFY_BLOCKED_VERIFY_ENABLED || 'true' }}" in wf
	run_codex = wf.split("- name: Run Codex", 1)[1].split("- name: Semantic cache store", 1)[0]
	assert "CLARIFY_BLOCKED_VERIFY_ENABLED" in run_codex
	assert 'clarify_bv_helper="${SUPPORT_SCRIPTS_DIR}/clarify_blocked_verify.sh"' in run_codex
	assert "scripts/clarify_blocked_verify.sh" not in wf
	stage = _read(STAGE_SUPPORT)
	block = re.search(r"^\tclarify\)\n(.*?)^\t\t;;", stage, re.MULTILINE | re.DOTALL)
	assert block
	optional = re.search(r"stage_support_optional_scripts=\((.*?)\)", block.group(1), re.DOTALL)
	assert optional and "clarify_blocked_verify.sh" in optional.group(1).split()
	required = re.search(r"stage_support_required_scripts=\((.*?)\)", block.group(1), re.DOTALL)
	assert required and "clarify_blocked_verify.sh" not in required.group(1).split()


# The helper's own cases live in tests/test_clarify_blocked_verify.py and are
# collected here, since CI already runs this file and ci.yml was outside the
# P9b file grant. A name clash would hide a test, so it fails loudly.
def _collect_clarify_blocked_verify_cases() -> None:
	spec = importlib.util.spec_from_file_location(
		"clarify_blocked_verify_cases", Path(__file__).resolve().parent / "test_clarify_blocked_verify.py",
	)
	assert spec and spec.loader
	cases = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(cases)
	collected = 0
	for case_name in dir(cases):
		if case_name.startswith("test_") and callable(getattr(cases, case_name)):
			assert case_name not in globals(), case_name
			globals()[case_name] = getattr(cases, case_name)
			collected += 1
	# A renamed or emptied cases module must fail loudly, not collect nothing.
	assert collected >= 10, f"only {collected} clarify BLOCKED-verify cases collected"


_collect_clarify_blocked_verify_cases()


# Issue #9120: a blocked ai:check-triage issue alerts at WARNING, not CRITICAL,
# and its comment says the unblock judge re-checks the failing check. The first
# line and the Reason: line stay byte-identical (scripts/unblock_judge.sh reads both).
CHECK_TRIAGE_BLOCKED_PARAGRAPH = (
	"This is a CI-failure triage issue. The unblock judge re-checks the failing check and closes this "
	"issue with evidence once it passes on the pull request's newer head or on the default branch; "
	"no reply is needed unless the check keeps failing."
)


def _run_blocked_planning_step(labels: list[str] | None) -> tuple[subprocess.CompletedProcess[str], str, list[list[str]]]:
	(step,) = [step for step in _steps("plan.yml") if step.get("name") == "Handle blocked planning output"]
	script = step["run"]
	for expression, value in (
		("${{ github.repository }}", "owner/repo"),
		("${{ github.server_url }}", "https://github.com"),
		("${{ github.run_id }}", "1"),
	):
		script = script.replace(expression, value)
	assert "${{" not in script, script
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		(tmp / "scripts").mkdir()
		(tmp / "bin").mkdir()
		(tmp / "scripts" / "gh_helpers.sh").write_text('gh_retry() { "$@"; }\n', encoding="utf-8")
		(tmp / "scripts" / "label_helpers.sh").write_text("ensure_label_exists() { :; }\n", encoding="utf-8")
		(tmp / "scripts" / "tg_helpers.sh").write_text(
			'tg_send_tracked() { printf \'%s\\t%s\\t%s\\n\' "$1" "$3" "${2//$\'\\n\'/ }" >> "${TG_LOG}"; }\n',
			encoding="utf-8",
		)
		gh = tmp / "bin" / "gh"
		gh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
		gh.chmod(0o755)
		env = {
			"PATH": f"{tmp / 'bin'}:/usr/bin:/bin",
			"ISSUE_NUMBER": "746",
			"ISSUE_URL": "https://github.com/owner/repo/issues/746",
			"BLOCKED_REASON": "work is already on the planning ref; nothing to implement",
			"PLAN_COMMENT_FILE": str(tmp / "comment.md"),
			"TG_LOG": str(tmp / "tg.log"),
		}
		if labels is not None:
			meta = tmp / "issue_meta.json"
			meta.write_text(json.dumps({"number": 746, "labels": [{"name": name} for name in labels]}), encoding="utf-8")
			env["ISSUE_META_FILE"] = str(meta)
		else:
			env["ISSUE_META_FILE"] = str(tmp / "missing_issue_meta.json")
		result = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True, timeout=60, check=False)
		comment = (tmp / "comment.md").read_text(encoding="utf-8") if (tmp / "comment.md").exists() else ""
		sends = [line.split("\t") for line in (_read(tmp / "tg.log").splitlines() if (tmp / "tg.log").exists() else [])]
	return result, comment, sends


def test_blocked_check_triage_issue_alerts_at_warning_with_the_judge_paragraph() -> None:
	result, comment, sends = _run_blocked_planning_step(["ai:check-triage", "ai:planning"])
	assert result.returncode == 0, result.stdout + result.stderr
	lines = comment.splitlines()
	assert lines[0] == "Planning blocked: human input required."
	assert lines[2] == "Reason: work is already on the planning ref; nothing to implement"
	assert lines[3] == ""
	assert lines[4] == CHECK_TRIAGE_BLOCKED_PARAGRAPH
	assert "Provide the required input, then reply with /answer." in comment
	assert len(sends) == 1
	assert sends[0][:2] == ["746", "WARNING"]
	assert sends[0][2].startswith(
		"Planning blocked for issue #746 (CI triage; the unblock judge will re-check): work is already on the planning ref"
	)


def test_blocked_issue_without_check_triage_label_keeps_critical_and_text() -> None:
	for labels in (["ai:planning"], None):
		result, comment, sends = _run_blocked_planning_step(labels)
		assert result.returncode == 0, result.stdout + result.stderr
		assert comment == (
			"Planning blocked: human input required.\n\n"
			"Reason: work is already on the planning ref; nothing to implement\n\n"
			"Auto-answer has been paused to avoid unsatisfiable clarification loops.\n"
			"Provide the required input, then reply with /answer.\n\n"
			"Run: https://github.com/owner/repo/actions/runs/1\n"
		), labels
		assert CHECK_TRIAGE_BLOCKED_PARAGRAPH not in comment
		assert len(sends) == 1
		assert sends[0][:2] == ["746", "CRITICAL"], labels
		assert sends[0][2].startswith("Planning blocked for issue #746: work is already"), labels


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())


# ---------------------------------------------------------------------------
# Stuck-states P8a-2 (Refs #8291): WAITING: lines park the issue on its dependency
# ---------------------------------------------------------------------------

WAITING_SENTENCE = (
	"Sequencing wait: when the only blocker is another pull request that has not merged, an upstream change "
	"not yet in the `shubhodeep1/coding-workflows` `@stable` release, or another issue in this repository that "
	"must be finished first, emit exactly one line `WAITING: pr=<owner>/<repo>#<N>`, "
	"`WAITING: release=shubhodeep1/coding-workflows@stable contains=shubhodeep1/coding-workflows#<N>` or "
	"`WAITING: issue=<owner>/<repo>#<N>` instead of `BLOCKED:`. `pr` names this repository or "
	"`shubhodeep1/coding-workflows`; `issue` names this repository and never this issue."
)
WAIT_HELPER = REPO_ROOT / "scripts" / "dependency_wait_resume.py"


def _step_body(workflow: Path, step_name: str) -> str:
	"""The dedented run: body of one step, with the github.* expressions this suite needs filled in."""
	lines = _read(workflow).splitlines()
	start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}")
	stop = next(i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("- name: "))
	block = lines[start:stop]
	run = next(i for i, line in enumerate(block) if line.strip() == "run: |")
	body = textwrap.dedent("\n".join(block[run + 1:])) + "\n"
	for expr, value in (
		("${{ github.repository }}", "owner/repo"),
		("${{ github.server_url }}", "https://github.com"),
		("${{ github.run_id }}", "1"),
	):
		body = body.replace(expr, value)
	assert "${{" not in body
	return body


def _step_text(workflow: Path, step_name: str) -> str:
	text = _read(workflow)
	start = text.index(f"      - name: {step_name}\n")
	end = text.find("\n      - name:", start + 1)
	return text[start:end]


def _norm(text: str) -> str:
	return " ".join(text.split())


def test_waiting_rule_is_in_every_live_and_template_prompt() -> None:
	for path in (
		PROMPT_PLAN, PROMPT_CLARIFY, REPO_ROOT / "prompts" / "_templates" / "mode-plan.txt",
		REPO_ROOT / "prompts" / "_templates" / "mode-clarify.txt", PLAN_RUNNER, CLARIFY_WF,
	):
		assert WAITING_SENTENCE in _norm(_read(path)), path
	for path in (PROMPT_PLAN, REPO_ROOT / "prompts" / "_templates" / "mode-plan.txt", PLAN_RUNNER):
		assert WAITING_SENTENCE + " Emit no plan with it, and end with `STATUS: NOT_CLEAR`." in _norm(_read(path)), path
	for path in (PROMPT_CLARIFY, REPO_ROOT / "prompts" / "_templates" / "mode-clarify.txt"):
		assert "Or, for a sequencing wait:\nWAITING: <target>\n" in _read(path)
	assert "Or, for a sequencing wait (point 10):\n          WAITING: <target>\n" in _read(CLARIFY_WF)


def _run_parse(tmp: Path, workflow: Path, step: str, output: str, *, helper: bool = True, enabled: str = "true") -> dict[str, str]:
	(tmp / "rt").mkdir(exist_ok=True)
	bundle = tmp / "bundle"
	bundle.mkdir(exist_ok=True)
	if helper:
		(bundle / "dependency_wait_resume.py").write_text(_read(WAIT_HELPER), encoding="utf-8")
	(tmp / "o.txt").write_text(output, encoding="utf-8")
	for name in ("go", "ge"):
		(tmp / name).write_text("", encoding="utf-8")
	env = {
		"PATH": "/usr/bin:/bin", "HOME": str(tmp), "CODEX_OUTPUT_FILE": str(tmp / "o.txt"), "RUNTIME_DIR": str(tmp / "rt"),
		"SUPPORT_SCRIPTS_DIR": str(bundle), "GITHUB_OUTPUT": str(tmp / "go"), "GITHUB_ENV": str(tmp / "ge"),
		"GITHUB_REPOSITORY": "owner/repo", "ISSUE_NUMBER": "5", "DEPENDENCY_WAIT_RESUME_ENABLED": enabled,
		"PLAN_SELF_CHECK_ENABLED": "true",
	}
	result = subprocess.run(["bash", "-c", _step_body(workflow, step)], env=env, capture_output=True, text=True, timeout=60, check=False)
	assert result.returncode == 0, result.stdout + result.stderr
	outputs: dict[str, str] = {}
	lines = _read(tmp / "go").splitlines()
	index = 0
	while index < len(lines):
		key, sep, value = lines[index].partition("=")
		if not sep and "<<" in lines[index]:
			key, delim = lines[index].split("<<", 1)
			end = lines.index(delim, index + 1)
			outputs[key] = "\n".join(lines[index + 1:end])
			index = end + 1
			continue
		outputs[key] = value
		index += 1
	outputs["_stderr"] = result.stderr
	outputs["_stdout"] = result.stdout
	return outputs


PARSE_STEPS = ((CLARIFY_WF, "Parse Codex output"), (PLAN_WF, "Parse planning output"))


def test_waiting_line_is_parsed_before_blocked_in_both_parse_steps() -> None:
	for workflow, step in PARSE_STEPS:
		text = _step_text(workflow, step)
		assert "DEPENDENCY_WAIT_RESUME_ENABLED: ${{ vars.DEPENDENCY_WAIT_RESUME_ENABLED || 'true' }}" in text
		assert text.index("parse-waiting") < text.index("BLOCKED_REASON=\"$(perl")
		assert '[ -f "${DEP_WAIT_HELPER}" ] && [ ! -L "${DEP_WAIT_HELPER}" ]' in text
		assert "DEPENDENCY_WAIT_PARK op=parse outcome=skip reason=helper_missing" in text
		with tempfile.TemporaryDirectory() as td:
			valid = _run_parse(Path(td), workflow, step, "STATUS: CLEAR\nBLOCKED: other\nWAITING: pr=owner/repo#7\n")
		assert valid["blocked"] == "true" and valid["needs_clarification"] == "false"
		assert valid["blocked_reason"] == "WAITING: pr:owner/repo#7"
		assert (valid["waiting"], valid["waiting_kind"], valid["waiting_repo"], valid["waiting_number"]) == ("true", "pr", "owner/repo", "7")
		with tempfile.TemporaryDirectory() as td:
			invalid = _run_parse(Path(td), workflow, step, "WAITING: pr=other/repo#7\nBLOCKED: the real reason\n")
		assert invalid["waiting"] == "false" and invalid["blocked"] == "true" and invalid["blocked_reason"] == "the real reason"
		with tempfile.TemporaryDirectory() as td:
			missing = _run_parse(Path(td), workflow, step, "STATUS: CLEAR\nWAITING: pr=owner/repo#7\n", helper=False)
		assert missing["waiting"] == "false" and missing["blocked"] == "true"
		assert missing["blocked_reason"] == "WAITING: pr=owner/repo#7"
		assert "reason=helper_missing" in missing["_stdout"]
		with tempfile.TemporaryDirectory() as td:
			off = _run_parse(Path(td), workflow, step, "WAITING: pr=owner/repo#7\n", enabled="false")
		assert off["waiting"] == "false" and off["blocked"] == "true"
		with tempfile.TemporaryDirectory() as td:
			fenced = _run_parse(Path(td), workflow, step, "```\nWAITING: pr=owner/repo#7\n```\nSTATUS: CLEAR\nPLAN_SELF_CHECK: PASS\n")
		assert fenced["waiting"] == "false" and fenced["blocked"] == "false"


PARK_STUBS = {
	"gh_helpers.sh": 'gh_retry() { echo "gh_retry $*" >> "${STUB_LOG}"; }\n',
	"label_helpers.sh": 'ensure_label_exists() { echo "ensure_label_exists $*" >> "${STUB_LOG}"; }\n',
	"tg_helpers.sh": 'tg_send_tracked() { echo "tg $3 $2" | head -1 >> "${STUB_LOG}"; }\n',
	"dependency_wait_resume.py": (
		"import json, os, sys\n"
		"open(os.environ['STUB_LOG'], 'a').write('park ' + ' '.join(sys.argv[1:]) + '\\n')\n"
		"print(json.dumps({'status': os.environ['STUB_PARK_STATUS'], 'target': 'pr:owner/repo#7'}))\n"
	),
}
HANDLER_STEPS = ((CLARIFY_WF, "Handle blocked clarification output", "clarify"), (PLAN_WF, "Handle blocked planning output", "plan"))


def _run_handler(tmp: Path, workflow: Path, step: str, *, status: str, waiting: str = "true") -> list[str]:
	bundle = tmp / "bundle"
	bundle.mkdir()
	for name, text in PARK_STUBS.items():
		(bundle / name).write_text(text, encoding="utf-8")
	(tmp / "rt").mkdir()
	env = {
		"PATH": "/usr/bin:/bin", "HOME": str(tmp), "SUPPORT_SCRIPTS_DIR": str(bundle), "RUNTIME_DIR": str(tmp / "rt"),
		"STUB_LOG": str(tmp / "log"), "STUB_PARK_STATUS": status, "ISSUE_NUMBER": "5", "ISSUE_URL": "https://github.com/owner/repo/issues/5",
		"BLOCKED_REASON": "WAITING: pr:owner/repo#7", "WAITING_PARSED": waiting, "WAITING_KIND": "pr",
		"WAITING_REPO": "owner/repo", "WAITING_NUMBER": "7", "PLAN_COMMENT_FILE": str(tmp / "rt" / "plan_comment.md"),
	}
	result = subprocess.run(["bash", "-c", _step_body(workflow, step)], env=env, capture_output=True, text=True, timeout=60, check=False)
	assert result.returncode == 0, result.stdout + result.stderr
	return _read(tmp / "log").splitlines()


def test_blocked_handlers_park_a_wait_and_fall_back_to_blocked() -> None:
	for workflow, step, phase in HANDLER_STEPS:
		text = _step_text(workflow, step)
		for row in ("WAITING_PARSED:", "WAITING_KIND:", "WAITING_REPO:", "WAITING_NUMBER:",
			"DEPENDENCY_WAIT_RESUME_ENABLED: ${{ vars.DEPENDENCY_WAIT_RESUME_ENABLED || 'true' }}",
			"DEPENDENCY_WAIT_ESCALATE_DAYS: ${{ vars.DEPENDENCY_WAIT_ESCALATE_DAYS || '7' }}"):
			assert row in text, (step, row)
		with tempfile.TemporaryDirectory() as td:
			parked = _run_handler(Path(td), workflow, step, status="parked")
		assert parked[0] == "ensure_label_exists ai:waiting-on-dependency owner/repo"
		assert parked[1].startswith("park park --repo owner/repo --issue-number 5 --kind pr --target-repo owner/repo --number 7 ")
		assert f"--phase {phase} --run-url https://github.com/owner/repo/actions/runs/1" in parked[1]
		assert "--remove-label ai:blocked" in parked[2] and "--add-label" not in parked[2]
		assert parked[3].startswith("tg CRITICAL ") and "waiting on dependency for issue #5: pr:owner/repo#7" in parked[3]
		assert len(parked) == 4 and not any("ai:blocked owner/repo" in line for line in parked)
		with tempfile.TemporaryDirectory() as td:
			fallback = _run_handler(Path(td), workflow, step, status="already_resolved")
		assert "ensure_label_exists ai:blocked owner/repo" in fallback
		assert any("--add-label ai:blocked" in line for line in fallback)
		assert any("(dependency wait not parked: already_resolved)" in line for line in fallback if line.startswith("tg "))
		with tempfile.TemporaryDirectory() as td:
			plain = _run_handler(Path(td), workflow, step, status="parked", waiting="false")
		assert not any(line.startswith("park ") for line in plain) and "ensure_label_exists ai:blocked owner/repo" in plain


def test_waiting_helper_is_optional_for_clarify_and_plan_staging() -> None:
	stage = _read(STAGE_SUPPORT)
	for phase in ("clarify", "plan"):
		block = re.search(r"^\t" + phase + r"\)\n(.*?)^\t\t;;", stage, re.MULTILINE | re.DOTALL)
		assert block
		optional = re.search(r"stage_support_optional_scripts=\((.*?)\)", block.group(1), re.DOTALL)
		assert optional and "dependency_wait_resume.py" in optional.group(1).split()
		required = re.search(r"stage_support_required_scripts=\((.*?)\)", block.group(1), re.DOTALL)
		assert required and "dependency_wait_resume.py" not in required.group(1).split()
	for workflow in (CLARIFY_WF, PLAN_WF):
		assert "scripts/dependency_wait_resume.py" not in _read(workflow)


def test_blocked_verify_retry_accepts_a_waiting_answer() -> None:
	run_codex = _read(CLARIFY_WF).split("- name: Run Codex", 1)[1].split("- name: Semantic cache store", 1)[0]
	assert "if (/^\\s*WAITING:\\s*\\S/i) { $blocked = 1; exit; }" in run_codex
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_blocked_verify(Path(tmp), "waiting")
		assert result.returncode == 0, result.stdout + result.stderr
		assert len(calls) == 2, calls
		assert _read(Path(tmp) / "o.txt") == "WAITING: pr=owner/repo#7\n"
	assert _blocked_verify_lines(result)[-1] == "CLARIFY_BLOCKED_VERIFY stage=retry outcome=still_blocked exit_code=0"
	assert "^\\s*WAITING:\\s*.+$" in _read(CLARIFY_WF)

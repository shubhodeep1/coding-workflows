#!/usr/bin/env python3
"""Static contracts for the OpenCode action, live smoke, and Phase 2 cutover."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ACTION = REPO_ROOT / ".github" / "actions" / "install-opencode" / "action.yml"
SMOKE = REPO_ROOT / ".github" / "workflows" / "opencode-live-smoke.yml"
PRODUCTION_REVIEW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _reviewer_roster(text: str) -> list[str]:
	lines = text.splitlines()
	for index, line in enumerate(lines):
		if line.strip() != "REVIEWER_MODELS: |":
			continue
		indent = len(line) - len(line.lstrip())
		roster = []
		for candidate in lines[index + 1 :]:
			candidate_indent = len(candidate) - len(candidate.lstrip())
			if candidate.strip() and candidate_indent <= indent:
				break
			value = candidate.strip()
			if value:
				roster.append(value)
		return roster
	raise AssertionError("REVIEWER_MODELS block missing")


def test_install_action_pins_verifies_and_refreshes() -> None:
	action = ACTION.read_text(encoding="utf-8")
	assert re.search(r"(?m)^\s+default: 1\.18\.23$", action)
	assert 'npm install -g "opencode-ai@${OPENCODE_VERSION}" --no-audit --no-fund' in action
	assert 'installed_version="$(opencode --version)"' in action
	assert '[ "${installed_version}" != "${OPENCODE_VERSION}" ]' in action
	assert "opencode models --refresh" in action
	assert "opencode run --help >/dev/null" in action


def test_smoke_is_dispatch_only_and_read_only() -> None:
	smoke = SMOKE.read_text(encoding="utf-8")
	on_block = smoke.split('"on":', 1)[1].split("permissions:", 1)[0]
	assert "workflow_dispatch:" in on_block
	for forbidden in ("push:", "pull_request:", "schedule:", "workflow_call:"):
		assert forbidden not in on_block
	assert re.search(r"(?m)^permissions:\n  contents: read$", smoke)
	assert "actions/checkout@v4" in smoke


def test_smoke_roster_and_editor_defaults_match_production() -> None:
	smoke = SMOKE.read_text(encoding="utf-8")
	production = PRODUCTION_REVIEW.read_text(encoding="utf-8")
	assert _reviewer_roster(smoke) == _reviewer_roster(production)
	assert "vars.WORKFLOW_EDITOR_MODEL || 'openai/gpt-5.6-sol'" in smoke
	assert "vars.WORKFLOW_EDITOR_FALLBACK_MODEL || 'openai/gpt-5.5'" in smoke
	assert "vars.WORKFLOW_EDITOR_MODEL || 'openai/gpt-5.6-sol'" in production
	assert "vars.WORKFLOW_EDITOR_FALLBACK_MODEL || 'openai/gpt-5.5'" in production


def test_smoke_runs_identical_calls_and_aggregates_failures() -> None:
	smoke = SMOKE.read_text(encoding="utf-8")
	helpers = (REPO_ROOT / "scripts" / "opencode_helpers.sh").read_text(encoding="utf-8")
	assert "smoke_prompt='Silently verify that 391 is the product of two two-digit primes, then output exactly OK'" in smoke
	assert "printf '%s\\n' \"${smoke_prompt}\" | opencode_run_cmd" in smoke
	assert 'reasoning_evidence="PASS(text)"' in smoke
	assert 'reasoning_result="PASS(text)"' in smoke
	assert "'{model: $model, messages: [{role: \"user\", content: $prompt}], max_tokens: 4096, reasoning: {effort: \"xhigh\"}}'" in smoke
	assert re.search(r'reasoning_evidence=FAIL\n\s+if \[ "\$\{call_rc\}" -eq 0 \]; then\n\s+probe_body=', smoke)
	assert '-o "${probe_body}" -w \'%{http_code}\'' in smoke
	assert 'reasoning_evidence="FAIL(probe_http_${probe_http_status})"' in smoke
	assert 'result="FAIL(reasoning_probe_transport)"' in smoke
	assert '[[ "${probe_reasoning_text}" =~ [^[:space:]] ]]' in smoke
	assert 'run_smoke_call "${source_slot}" "${role}" "${model_slug}" 1' in smoke
	assert 'run_smoke_call "${source_slot}" "${role}" "${model_slug}" 2' in smoke
	assert "opencode_strip_ansi" in smoke
	assert 'awk -v expected_stream_model="${model_slug}" -v expected_stream_role="${role}"' in smoke
	assert 'index($0, "message=stream providerID=openrouter modelID=" expected_stream_model " ")' in smoke
	assert 'index($0, " small=false agent=" expected_stream_role " mode=primary")' in smoke
	assert "evidence_line_matched=1" in smoke
	assert "END { exit evidence_line_matched ? 0 : 1 }" in smoke
	assert (
		'model_evidence=FAIL\n              if [ "${call_rc}" -eq 0 ]; then\n'
		'                result="FAIL(model_evidence)"\n'
	) in smoke
	assert '"${role}" "${model_slug}" xhigh "${config_path}" "${GITHUB_WORKSPACE}" json' in smoke
	assert '.type == "step_finish" and ((.part.tokens.reasoning // 0) > 0)' in smoke
	assert "reasoning_evidence=FAIL" in smoke
	assert 'result="FAIL(no_reasoning_usage)"' in smoke
	assert '| grep -Fq " small=false agent=${role} mode=primary"' not in smoke
	assert "opencode_agent_start" not in smoke
	assert "expected_provider=openrouter expected_model=%s" in helpers
	assert "providerID=openrouter modelID=%s" not in helpers
	assert '"${role}" "${model_slug}" xhigh "${config_path}"' in smoke
	assert '.variants.xhigh.reasoning.effort == "xhigh"' in smoke
	assert "Model evidence | Reasoning evidence" in smoke
	assert '[ "${reasoning_result}" != FAIL ]' in smoke
	assert "${GITHUB_STEP_SUMMARY}" in smoke
	assert 'if [ "${any_failed}" = true ]' in smoke
	assert "bootstrap_alert_handled=false" in smoke
	assert "bootstrap_alert_handled=true" in smoke
	assert 'if [ "${bootstrap_alert_handled}" != true ]' in smoke
	assert smoke.count("bootstrap_or_config") == 1


def test_production_review_path_uses_opencode_for_read_and_write_sides() -> None:
	production = PRODUCTION_REVIEW.read_text(encoding="utf-8")
	reviewers = (REPO_ROOT / "scripts" / "review_run_reviewers.sh").read_text(encoding="utf-8")
	summariser = (REPO_ROOT / "scripts" / "summarize_reviewer_consensus.sh").read_text(encoding="utf-8")
	apply_fixes = (REPO_ROOT / "scripts" / "review_apply_fixes.sh").read_text(encoding="utf-8")
	assert "Install OpenCode CLI" in production
	assert "Install Codex CLI" not in production
	assert "Create Codex config" not in production
	assert 'opencode_run_cmd "$@"' in reviewers
	assert 'opencode_run_cmd "$@"' in summariser
	assert 'opencode_run_cmd "$@"' in apply_fixes
	assert "exec codex --ask-for-approval never" not in apply_fixes


def test_focused_tests_are_wired_into_ci() -> None:
	ci = CI.read_text(encoding="utf-8")
	for test_file in (
		"tests/test_write_opencode_config.py",
		"tests/test_opencode_helpers.py",
		"tests/test_opencode_live_smoke_workflow.py",
	):
		assert f"PYTHONDONTWRITEBYTECODE=1 python3 {test_file}" in ci


def main() -> int:
	tests = [value for key, value in sorted(globals().items()) if key.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

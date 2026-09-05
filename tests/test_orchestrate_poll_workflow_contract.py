#!/usr/bin/env python3
"""Contract tests for orchestrate_poll workflow env mapping."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE_POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
ORCHESTRATE_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate.yml"
ORCHESTRATE_POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
SYNC_LIST_UNION_REQUIREMENTS = REPO_ROOT / "scripts" / "sync_contract_list_union.requirements.txt"


def _workflow(path: Path = ORCHESTRATE_POLL_WF) -> str:
	return path.read_text(encoding="utf-8")


def test_stall_control_env_defaults_are_declared() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	assert "STALL_JUDGE_TRIGGER_COUNT: ${{ vars.STALL_JUDGE_TRIGGER_COUNT || '2' }}" in wf
	assert "ENABLE_STALL_JUDGE: ${{ vars.ENABLE_STALL_JUDGE || 'true' }}" in wf
	assert "ENABLE_STALL_HUMAN_TERMINALIZATION: ${{ vars.ENABLE_STALL_HUMAN_TERMINALIZATION || 'false' }}" in wf
	assert "JUDGE_REPEAT_FINGERPRINT_MAX: ${{ vars.JUDGE_REPEAT_FINGERPRINT_MAX || '2' }}" in wf


def test_stall_recovery_prompt_is_bootstrapped_with_main_fallback() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	assert "for pf in mode-judge.txt mode-judge-review-blocked.txt mode-judge-stall-recovery.txt; do" in wf
	assert "src=\".codex-workflow-src/prompts/${pf}\"" in wf
	assert "if [ ! -f \"${src}\" ] && [ -f \".codex-workflow-src-main/prompts/${pf}\" ]; then" in wf
	assert "src=\".codex-workflow-src-main/prompts/${pf}\"" in wf
	assert "::error::Missing required support file prompts/${pf}" in wf
	assert "install -m 0644 \"${src}\" \"prompts/${pf}\"" in wf


def test_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets() -> None:
	wf = _workflow()
	assert "validation_history.v1.json" in wf
	assert "operator_bypass_audit.v1.json" in wf
	assert "revalidate_events.v1.json" in wf


def test_orchestrate_workflow_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets() -> None:
	wf = _workflow(ORCHESTRATE_WF)
	assert "validation_history.v1.json" in wf
	assert "operator_bypass_audit.v1.json" in wf
	assert "revalidate_events.v1.json" in wf


def test_nag_reminder_assets_and_judge_wiring_are_present() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")

	assert "nag_reminder.sh" in wf
	assert 'nag_prompt_src=".codex-workflow-src/prompts/_nag_reminders.txt"' in wf
	assert 'install -m 0644 "${nag_prompt_src}" "prompts/_nag_reminders.txt"' in wf
	assert 'Optional nag reminder prompt asset _nag_reminders.txt is unavailable on ${SCRIPT_REF}; nag reminders will fail open for this run.' in wf
	assert "UNATTENDED_NAG_REMINDER_ENABLED: ${{ vars.UNATTENDED_NAG_REMINDER_ENABLED || 'false' }}" in wf
	assert "UNATTENDED_NAG_SILENT_ROUNDS: ${{ vars.UNATTENDED_NAG_SILENT_ROUNDS || '3' }}" in wf
	assert 'source scripts/nag_reminder.sh 2>/dev/null || true' in poller
	assert 'nag_reminder_enabled() { return 1; }' in poller
	assert 'nag_silent_round_threshold() { printf \'3\\n\'; }' in poller
	assert 'if nag_reminder_enabled; then' in poller
	assert 'judge_nag_attempt_limit="$(nag_silent_round_threshold)"' in poller
	assert 'judge_nag_counter_for_attempt=$((judge_silent_rounds + 1))' in poller
	assert 'judge_nag_block="$(maybe_inject_nag "orchestrate-poll-judge" "${judge_nag_counter_for_attempt}")"' in poller
	assert 'if cp "${JUDGE_PROMPT_FILE}" "${judge_attempt_prompt_file}" 2>/dev/null; then' in poller
	assert 'Could not create per-attempt judge prompt file for attempt ${attempt}; continuing with the base prompt.' in poller
	assert 'judge_json_candidate="$(extract_judge_json_with_status "${JUDGE_OUTPUT_FILE}")"' in poller
	assert 'cleaned = re.sub(r"```(?:json)?\\s*", "", raw)' in poller
	assert 'cleaned = re.sub(r"```\\s*$", "", cleaned, flags=re.MULTILINE)' in poller
	assert 'cleaned = re.sub(r"```(?:json)?\\\\s*", "", raw)' not in poller
	assert 'cleaned = re.sub(r"```\\\\s*$", "", cleaned, flags=re.MULTILINE)' not in poller


def test_task_state_helper_and_flag_are_wired_into_poller_workflow() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	assert "task_state.py" in wf
	assert "ORCH_TASK_FILES_ENABLED: ${{ vars.ORCH_TASK_FILES_ENABLED || 'false' }}" in wf


def test_security_pass_dark_launch_env_and_assets_are_wired() -> None:
	# The historical test name spans the rollout; the current contract is default-on.
	wf = _workflow(ORCHESTRATE_POLL_WF)
	assert "ENABLE_SECURITY_PASS: ${{ vars.ENABLE_SECURITY_PASS || 'true' }}" in wf
	assert "MAX_SECURITY_PASS_CYCLES: ${{ vars.MAX_SECURITY_PASS_CYCLES || '3' }}" in wf
	assert "SECURITY_PASS_CONFIDENCE_GATE: ${{ vars.SECURITY_PASS_CONFIDENCE_GATE || '8' }}" in wf
	assert "WORKFLOW_EDITOR_MODEL: ${{ vars.WORKFLOW_EDITOR_MODEL || 'openai/gpt-5.6-sol' }}" in wf
	for asset in (
		"codex_heartbeat.sh",
		"security_audit.sh",
		"security_audit_fp_exclusions.json",
		"mode-security-audit.txt",
		"_templates/mode-security-audit.txt",
		"references/security-money-lens.txt",
	):
		assert asset in wf


def test_worktree_registry_helpers_and_gc_are_wired_into_poller_workflow() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	assert "worktree_registry.sh" in wf
	assert "worktree_gc.sh" in wf
	assert "ORCH_WORKTREE_REGISTRY_ENABLED: ${{ vars.ORCH_WORKTREE_REGISTRY_ENABLED || 'false' }}" in wf
	assert "ORCH_WORKTREE_TTL_SECS: ${{ vars.ORCH_WORKTREE_TTL_SECS || '3600' }}" in wf
	assert "- name: Run worktree registry GC" in wf
	assert "if: steps.find_tracking.outputs.has_work == 'true'\n        run: bash scripts/worktree_gc.sh" not in wf
	assert "run: bash scripts/worktree_gc.sh" in wf


def test_contract_list_union_uses_isolated_hash_locked_pyyaml_before_git_credentials() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	requirements = SYNC_LIST_UNION_REQUIREMENTS.read_text(encoding="utf-8")
	prepare_marker = "      - name: Prepare isolated contract-list union Python"
	auth_marker = "      - name: Configure git auth for memory helper clones"
	prepare_start = wf.index(prepare_marker)
	auth_start = wf.index(auth_marker)
	prepare_block = wf[prepare_start:auth_start]

	assert prepare_start < auth_start
	assert "sync_contract_list_union.requirements.txt" in wf
	assert 'python3 -I -m venv "${union_venv}"' in prepare_block
	assert '"${union_python}" -I -m pip install' in prepare_block
	assert "--require-hashes" in prepare_block
	assert "--only-binary=:all:" in prepare_block
	assert "--no-deps" in prepare_block
	assert '--requirement "${requirements_file}"' in prepare_block
	assert '-I -c \'import yaml\'' in prepare_block
	assert "yaml.__version__" not in prepare_block
	assert 'SYNC_CONTRACT_LIST_UNION_PYTHON=${unavailable_python}' in prepare_block
	assert "PyYAML==6.0.3" in requirements
	assert "--hash=sha256:ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc" in requirements
	assert requirements.count("--hash=sha256:") > 1
	assert "runs-on: ubuntu-latest" in wf
	assert 'python-version: "3.12"' in wf
	assert 'architecture: "x64"' in wf
	assert "GH_TOKEN" not in prepare_block
	assert "python3 -m pip install --quiet pyyaml" not in wf

	checkout_prefix = "uses: actions/checkout@v5"
	for checkout_start in [index for index in range(prepare_start) if wf.startswith(checkout_prefix, index)]:
		next_step = wf.find("\n      - name:", checkout_start)
		checkout_block = wf[checkout_start:next_step if next_step != -1 else prepare_start]
		assert "persist-credentials: false" in checkout_block


def test_poller_state_auth_and_readonly_model_security_contract() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	assert "ORCHESTRATOR_STATE_AUTH_KEYRING:\n        required: true" in wf
	assert "ORCHESTRATOR_STATE_AUTH_KEYRING: ${{ secrets.ORCHESTRATOR_STATE_AUTH_KEYRING }}" in wf
	assert 'git remote set-url origin "${GITHUB_SERVER_URL%/}/${GITHUB_REPOSITORY}.git"' in wf
	assert 'echo "GIT_CONFIG_KEY_0=credential.helper"' in wf
	assert 'echo "GIT_CONFIG_VALUE_0=${trusted_credential_helper}"' in wf
	assert "https://x-access-token:${GH_TOKEN}" not in wf
	assert "poller_run_sanitized_command()" in poller
	assert "poller_run_readonly_model()" in poller
	assert "--sandbox read-only" in poller
	assert "-c web_search=disabled" in poller
	assert "-c shell_environment_policy.ignore_default_excludes=false" in poller
	assert "--sandbox danger-full-access" not in poller
	assert "x-access-token:${GH_TOKEN}" not in poller
	runner_block = poller.split("poller_run_sanitized_command() {", 1)[1].split("\n}", 1)[0]
	for credential_name in (
		"GH_TOKEN",
		"GH_PAT",
		"TG_BOT_SECRET",
		"TG_ADMIN_CHAT_ID",
		"ORCHESTRATOR_STATE_AUTH_KEYRING",
	):
		assert credential_name not in runner_block


def main() -> int:
	test_stall_control_env_defaults_are_declared()
	test_stall_recovery_prompt_is_bootstrapped_with_main_fallback()
	test_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_orchestrate_workflow_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_nag_reminder_assets_and_judge_wiring_are_present()
	test_task_state_helper_and_flag_are_wired_into_poller_workflow()
	test_security_pass_dark_launch_env_and_assets_are_wired()
	test_worktree_registry_helpers_and_gc_are_wired_into_poller_workflow()
	test_contract_list_union_uses_isolated_hash_locked_pyyaml_before_git_credentials()
	test_poller_state_auth_and_readonly_model_security_contract()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

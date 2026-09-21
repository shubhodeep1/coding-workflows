#!/usr/bin/env python3
"""Contract tests for orchestrate_poll workflow env mapping."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE_POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
ORCHESTRATE_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate.yml"
IMPLEMENT_WF = REPO_ROOT / ".github" / "workflows" / "implement.yml"
REVIEW_AUTOFIX_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
CLARIFY_WF = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
ORCHESTRATE_CLARIFY_RESPOND_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_clarify_respond.yml"
VALIDATE_WF = REPO_ROOT / ".github" / "workflows" / "validate.yml"
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
	assert "RECOVERY_COUNT_DISTINCT_FINDINGS: ${{ vars.RECOVERY_COUNT_DISTINCT_FINDINGS || 'false' }}" in wf


def test_judge_pr_diff_byte_budgets_are_declared_and_consumed() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	assert "JUDGE_PR_DIFF_MAX_BYTES: ${{ vars.JUDGE_PR_DIFF_MAX_BYTES || '65536' }}" in wf
	assert "JUDGE_PR_DIFFS_TOTAL_MAX_BYTES: ${{ vars.JUDGE_PR_DIFFS_TOTAL_MAX_BYTES || '524288' }}" in wf
	assert 'JUDGE_PR_DIFF_MAX_BYTES="${JUDGE_PR_DIFF_MAX_BYTES:-65536}"' in poller
	assert 'JUDGE_PR_DIFFS_TOTAL_MAX_BYTES="${JUDGE_PR_DIFFS_TOTAL_MAX_BYTES:-524288}"' in poller
	assert 'if _judge_truncate_pr_diff_file "${_pr_diff_capped_tmp}" "${_pr_diff_allowance}"; then' in poller
	assert 'JUDGE_PROMPT_CHARS="$(LC_ALL=C.UTF-8 wc -m' in poller
	assert "Judge prompt size: ${JUDGE_PROMPT_BYTES} bytes (${JUDGE_PROMPT_CHARS} characters; codex stdin cap: 1048576 characters" in poller
	assert 'if [ "${JUDGE_PROMPT_CHARS}" -gt 1048576 ]; then' in poller
	assert "after the 1000-line cap; truncated to a prefix within ${JUDGE_PR_DIFF_MAX_BYTES} bytes" in poller
	assert "Review-blocked judge prompt size: ${RB_JUDGE_PROMPT_BYTES} bytes (${RB_JUDGE_PROMPT_CHARS} characters" in poller
	assert 'if [ "${RB_JUDGE_PROMPT_CHARS}" -gt 1048576 ]; then' in poller


def test_stall_recovery_prompt_is_bootstrapped_from_immutable_workflow_source() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	assert "for pf in mode-judge.txt mode-judge-review-blocked.txt mode-judge-stall-recovery.txt; do" in wf
	assert "src=\".codex-workflow-src/prompts/${pf}\"" in wf
	assert "WORKFLOW_DEFINITION_REPOSITORY: ${{ job.workflow_repository }}" in wf
	assert "WORKFLOW_DEFINITION_SHA: ${{ job.workflow_sha }}" in wf
	assert "SCRIPT_REF=${WORKFLOW_DEFINITION_SHA,,}" in wf
	assert "SCRIPT_REF=stable" not in wf
	assert ".codex-workflow-src-main" not in wf
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


def test_orchestrate_run_start_support_is_immutable_and_fail_closed() -> None:
	wf = _workflow(ORCHESTRATE_WF)
	checkout = wf.split("- name: Checkout workflow support source for run-start", 1)[1].split(
		"- name: Stage workflow memory support files for run-start", 1
	)[0]
	stage = wf.split("- name: Stage workflow memory support files for run-start", 1)[1].split(
		"- name: Record orchestration run start", 1
	)[0]
	record = wf.split("- name: Record orchestration run start", 1)[1].split(
		"- name: Create runtime workspace", 1
	)[0]
	assert "id: run_start_support_checkout" in checkout
	assert "continue-on-error: true" in checkout
	assert "ref: ${{ env.SCRIPT_REF }}" in checkout
	assert "id: run_start_support_stage" in stage
	assert "if: steps.run_start_support_checkout.outcome == 'success'" in stage
	assert "::error::Immutable support source checkout unavailable" in stage
	assert "::error::Missing required immutable run-start support script" in stage
	assert "if: steps.run_start_support_checkout.outcome == 'success' && steps.run_start_support_stage.outcome == 'success'" in record


def test_orchestrate_python_launches_are_isolated() -> None:
	wf = _workflow(ORCHESTRATE_WF)
	active_python_launches = [
		(line_number, line)
		for line_number, line in enumerate(wf.splitlines(), 1)
		if not line.lstrip().startswith("#")
		and re.search(r"\bpython(?:3)?\s+\S", line)
	]
	assert active_python_launches == [
		(next(
			line_number
			for line_number, line in enumerate(wf.splitlines(), 1)
			if 'python3 -I -B "${orchestrate_immutable_support_root}/scripts/load_workflow_overlay.py"' in line
		), '            python3 -I -B "${orchestrate_immutable_support_root}/scripts/load_workflow_overlay.py" \\')
	]
	bootstrap_line = active_python_launches[0][0]
	bootstrap_prefix = "\n".join(wf.splitlines()[bootstrap_line - 9:bootstrap_line - 1])
	assert "env -i \\" in bootstrap_prefix
	assert 'PYTHONDONTWRITEBYTECODE="1" \\' in bootstrap_prefix
	assert wf.count("_gh_helpers_run_isolated_python") >= 12
	isolated_python_step_blocks = [
		block for block in re.split(r"\n(?=[ \t]+- name: )", wf)[1:]
		if "_gh_helpers_run_isolated_python" in block
	]
	assert isolated_python_step_blocks
	for isolated_python_step_block in isolated_python_step_blocks:
		assert 'source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh"' in isolated_python_step_block
		assert isolated_python_step_block.index('source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh"') < isolated_python_step_block.index("_gh_helpers_run_isolated_python")
	assert '"PROJECT_DESCRIPTION=${PROJECT_DESCRIPTION}" -- -' in wf
	assert wf.count('"ORCHESTRATOR_STATE_AUTH_KEYRING=${ORCHESTRATOR_STATE_AUTH_KEYRING}" --') == 2
	assert "PYTHONDONTWRITEBYTECODE=1 python3" not in wf


def test_sibling_workflow_python_stdin_launches_are_isolated() -> None:
	unsafe_launch_pattern = re.compile(
		r"\bpython(?:3)?\s+(?!-I(?:\s|$)).*?(?:-(?:c|m)(?:\s|$)|-(?:\s|$))"
	)
	for workflow_path in (
		IMPLEMENT_WF,
		REVIEW_AUTOFIX_WF,
		PLAN_WF,
		CLARIFY_WF,
		ORCHESTRATE_CLARIFY_RESPOND_WF,
		VALIDATE_WF,
	):
		workflow_text = _workflow(workflow_path)
		unsafe_launches = [
			(line_number, line)
			for line_number, line in enumerate(workflow_text.splitlines(), 1)
			if not line.lstrip().startswith("#") and unsafe_launch_pattern.search(line)
		]
		assert unsafe_launches == [], f"unsafe Python launch in {workflow_path}: {unsafe_launches}"
		for step_block in re.split(r"\n(?=[ \t]+- name: )", workflow_text)[1:]:
			if "_gh_helpers_run_isolated_python" not in step_block:
				continue
			source_index = step_block.find("gh_helpers.sh\"")
			call_index = step_block.find("_gh_helpers_run_isolated_python")
			step_name = step_block.splitlines()[0].strip()
			assert 0 <= source_index < call_index, f"isolated Python helper is not sourced first in {workflow_path} step {step_name}"
	for redis_workflow_path, redis_venv_name in (
		(CLARIFY_WF, "clarify-semantic-cache-venv"),
		(ORCHESTRATE_CLARIFY_RESPOND_WF, "orchestrate-clarify-respond-semantic-cache-venv"),
	):
		redis_workflow_text = _workflow(redis_workflow_path)
		assert f'python3 -I -B -m venv "${{RUNNER_TEMP}}/{redis_venv_name}"' in redis_workflow_text
		assert f'"${{RUNNER_TEMP}}/{redis_venv_name}/bin/python" -I -B -m pip install --disable-pip-version-check "redis>=5,<6"' in redis_workflow_text
		assert f'"${{RUNNER_TEMP}}/{redis_venv_name}/bin" >> "$GITHUB_PATH"' in redis_workflow_text
		assert redis_workflow_text.count('"${SUPPORT_SCRIPTS_DIR}/semantic_cache.py"') == 2
		assert 'python3 "${SUPPORT_SCRIPTS_DIR}/semantic_cache.py"' not in redis_workflow_text
		assert redis_workflow_text.count('"GITHUB_REPOSITORY=${GITHUB_REPOSITORY}" --') == 2
		assert "semantic-cache-python" not in redis_workflow_text
		assert "pip install --user" not in redis_workflow_text


def test_nag_reminder_assets_and_judge_wiring_are_present() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")

	assert "nag_reminder.sh" in wf
	assert 'nag_prompt_src=".codex-workflow-src/prompts/_nag_reminders.txt"' in wf
	assert 'install -m 0644 "${nag_prompt_src}" "prompts/_nag_reminders.txt"' in wf
	assert 'Optional nag reminder prompt asset _nag_reminders.txt is unavailable on ${SCRIPT_REF}; nag reminders will fail open for this run.' in wf
	assert "UNATTENDED_NAG_REMINDER_ENABLED: ${{ vars.UNATTENDED_NAG_REMINDER_ENABLED || 'false' }}" in wf
	assert "UNATTENDED_NAG_SILENT_ROUNDS: ${{ vars.UNATTENDED_NAG_SILENT_ROUNDS || '3' }}" in wf
	assert 'source "${ORCHESTRATE_POLL_SUPPORT_SCRIPTS_DIR}/nag_reminder.sh" 2>/dev/null || true' in poller
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
	assert "MAX_SECURITY_PASS_CYCLES: ${{ vars.MAX_SECURITY_PASS_CYCLES || '5' }}" in wf
	assert "MAX_SECURITY_PASS_FIX_REISSUES: ${{ vars.MAX_SECURITY_PASS_FIX_REISSUES || '2' }}" in wf
	assert "SECURITY_PASS_CONFIDENCE_GATE: ${{ vars.SECURITY_PASS_CONFIDENCE_GATE || '8' }}" in wf
	assert "SECURITY_PASS_EXHAUSTION_JUDGE_ENABLED: ${{ vars.SECURITY_PASS_EXHAUSTION_JUDGE_ENABLED || 'true' }}" in wf
	assert "MAX_SECURITY_PASS_JUDGE_ROUNDS: ${{ vars.MAX_SECURITY_PASS_JUDGE_ROUNDS || '0' }}" in wf
	assert "MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS: ${{ vars.MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS || '2' }}" in wf
	assert "SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED: ${{ vars.SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED || 'true' }}" in wf
	assert "for security_prompt in mode-security-audit.txt mode-judge-security-pass-exhaustion.txt; do" in wf
	manifest_assignment_start = wf.index('poll_immutable_support_manifest="${RUNNER_TEMP}/poll-immutable-support-manifest.json"')
	required_prompts_start = wf.index("required_prompts: [", manifest_assignment_start)
	required_prompts_end = wf.index("],", required_prompts_start)
	immutable_required_prompts = wf[required_prompts_start:required_prompts_end]
	prompt_assembly_assets = wf.split("for prompt_assembly_asset in ", 1)[1].split("; do", 1)[0]
	for required_prompt in (
		"prompts/mode-judge-security-pass-exhaustion.txt",
		"prompts/_templates/mode-judge-security-pass-exhaustion.txt",
		"prompts/references/output-contract.txt",
		"prompts/references/severity-classification.txt",
		"prompts/_prelude_role_persona.txt",
	):
		assert f'"{required_prompt}"' in immutable_required_prompts
	for staged_asset in (
		"_prelude_role_persona.txt",
		"references/output-contract.txt",
		"references/severity-classification.txt",
	):
		assert staged_asset in prompt_assembly_assets
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
	assert 'run: bash "${SUPPORT_SCRIPTS_DIR}/worktree_gc.sh"' in wf


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
	assert "#   if ! python3 -m pip install --quiet pyyaml; then" in prepare_block
	assert not re.search(r"^\s*if ! python3 -m pip install --quiet pyyaml; then$", prepare_block, re.MULTILINE)

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
	assert "codex_helpers.sh" in wf
	assert "model_provider_broker.py" in wf
	assert "model_provider_broker_start" in wf
	assert 'model_provider_broker_prepare_codex_writer "${MODEL_EDITOR}" "${MODEL_REASONING_EFFORT_JUDGE}" "$(pwd)"' in wf
	assert "model_provider_broker_exec_sanitized" in poller
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
	assert 'OPENROUTER_API_KEY="${MODEL_PROVIDER_BROKER_TOKEN:-}"' in runner_block
	readonly_model_block = poller.split("poller_run_readonly_model() {", 1)[1].split("\n}", 1)[0]
	assert "model_provider_broker_exec_sanitized" in readonly_model_block
	assert "OPENROUTER_API_KEY" not in readonly_model_block
	snapshot_publish_block = wf.split("- name: Publish state snapshot branch", 1)[1].split("\n      - name:", 1)[0]
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in snapshot_publish_block
	assert "env -i \\" in snapshot_publish_block
	assert 'PYTHONDONTWRITEBYTECODE="1" \\' in snapshot_publish_block
	assert 'python3 -I -B - "${PUBLISH_DIR}/snapshots" "${HISTORY_DEPTH}"' in snapshot_publish_block
	assert "GH_TOKEN=" not in snapshot_publish_block.split("run: |", 1)[1].split("python3 -I -B", 1)[0]


def main() -> int:
	test_stall_control_env_defaults_are_declared()
	test_stall_recovery_prompt_is_bootstrapped_from_immutable_workflow_source()
	test_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_orchestrate_workflow_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_orchestrate_python_launches_are_isolated()
	test_sibling_workflow_python_stdin_launches_are_isolated()
	test_nag_reminder_assets_and_judge_wiring_are_present()
	test_task_state_helper_and_flag_are_wired_into_poller_workflow()
	test_security_pass_dark_launch_env_and_assets_are_wired()
	test_worktree_registry_helpers_and_gc_are_wired_into_poller_workflow()
	test_contract_list_union_uses_isolated_hash_locked_pyyaml_before_git_credentials()
	test_poller_state_auth_and_readonly_model_security_contract()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

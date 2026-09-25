#!/usr/bin/env python3
"""Contract tests for orchestrate_poll workflow env mapping."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE_POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
ORCHESTRATE_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate.yml"
ORCHESTRATE_POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
AGENTS_MD = REPO_ROOT / "agents.md"


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
	assert "MAX_SECURITY_PASS_CYCLES: ${{ vars.MAX_SECURITY_PASS_CYCLES || '5' }}" in wf
	assert "MAX_SECURITY_PASS_FIX_REISSUES: ${{ vars.MAX_SECURITY_PASS_FIX_REISSUES || '2' }}" in wf
	assert "SECURITY_PASS_CONFIDENCE_GATE: ${{ vars.SECURITY_PASS_CONFIDENCE_GATE || '8' }}" in wf
	assert "SECURITY_PASS_LINE_OWNERSHIP: ${{ vars.SECURITY_PASS_LINE_OWNERSHIP || 'project-lines' }}" in wf
	assert "SECURITY_PASS_OWNERSHIP_CONTEXT_LINES: ${{ vars.SECURITY_PASS_OWNERSHIP_CONTEXT_LINES || '3' }}" in wf
	assert "SECURITY_PASS_ADVISORY_FOLLOWUP_CAP: ${{ vars.SECURITY_PASS_ADVISORY_FOLLOWUP_CAP || '5' }}" in wf
	assert "SECURITY_PASS_EXHAUSTION_JUDGE_ENABLED: ${{ vars.SECURITY_PASS_EXHAUSTION_JUDGE_ENABLED || 'true' }}" in wf
	assert "MAX_SECURITY_PASS_JUDGE_ROUNDS: ${{ vars.MAX_SECURITY_PASS_JUDGE_ROUNDS || '0' }}" in wf
	assert "MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS: ${{ vars.MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS || '2' }}" in wf
	assert "SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED: ${{ vars.SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED || 'true' }}" in wf
	assert "SECURITY_PASS_AUTO_RESET_ON_ENGINE_CHANGE: ${{ vars.SECURITY_PASS_AUTO_RESET_ON_ENGINE_CHANGE || 'true' }}" in wf
	assert "STAGED_SUPPORT_LATCH_AUTO_RELEASE_ENABLED: ${{ vars.STAGED_SUPPORT_LATCH_AUTO_RELEASE_ENABLED || 'true' }}" in wf
	assert "for security_prompt in mode-security-audit.txt mode-judge-security-pass-exhaustion.txt; do" in wf
	assert "_templates/mode-judge-security-pass-exhaustion.txt" in wf
	assert "WORKFLOW_EDITOR_MODEL: ${{ vars.WORKFLOW_EDITOR_MODEL || 'openai/gpt-6-sol' }}" in wf
	for asset in (
		"codex_heartbeat.sh",
		"security_audit.sh",
		"security_audit_fp_exclusions.json",
		"mode-security-audit.txt",
		"_templates/mode-security-audit.txt",
		"references/security-money-lens.txt",
	):
		assert asset in wf


def test_poller_audit_support_is_sha_bound_and_frozen_outside_checkout() -> None:
	wf = _workflow()
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	assert 'branch_json="$(gh api repos/shubhodeep1/coding-workflows/branches/main)"' in wf
	assert "'.protected // false'" in wf
	assert 'gh api repos/shubhodeep1/coding-workflows/git/ref/tags/stable' in wf
	assert 'git/tags/${support_sha}' in wf
	assert 'echo "SCRIPT_REF=${support_sha}"' in wf
	assert wf.count('git -C .codex-workflow-src rev-parse HEAD)" = "${SCRIPT_REF}"') == 2
	assert "Checkout workflow support source fallback" not in wf
	assert "Checkout workflow support source fallback for gh retry" not in wf
	assert 'mktemp -d "${RUNNER_TEMP%/}/poller-support-' in wf
	assert 'echo "POLLER_TRUSTED_SUPPORT_DIR=${trusted_support_root}"' in wf
	for relative_asset in (
		"security_audit.sh", "codex_heartbeat.sh", "write_codex_config.sh",
		"ai_memory_lib.py", "orchestrate_lib.py", "openrouter_prompt_cache.py",
		"semantic_cache.py", "memory_injection_patterns.py", "codex_model_catalog.json",
		"security_audit_fp_exclusions.json", "render_prompt.py",
	):
		assert relative_asset in wf
	assert 'SECURITY_AUDIT_SUPPORT_DIR="${POLLER_TRUSTED_SUPPORT_DIR}"' in poller
	assert 'SECURITY_AUDIT_FP_EXCLUSIONS="${trusted_security_exclusions}"' in poller
	assert '--catalog-path "${trusted_security_catalog}"' in poller
	assert 'python3 -I - "${PWD}" "${STATE_FILE}" "${TRACKING_NUM}" "${trusted_python_dir}"' in poller
	assert "PYTHONPATH=\"${PWD}/scripts" not in poller
	assert "sys.path.insert(0, 'scripts')" not in poller


def test_security_pass_recovery_log_prefixes_are_registered() -> None:
	agents_text = AGENTS_MD.read_text(encoding="utf-8")
	for prefix in (
		"REISSUE_FILES_TOUCHED_UNION",
		"REISSUE_ORCHESTRATOR_METADATA_CARRIED",
		"REISSUE_ORCHESTRATOR_METADATA_ABSENT",
		"SECURITY_PASS_AUTO_RESET",
		"SECURITY_PASS_AUTO_RESET_SKIPPED",
		"STAGED_SUPPORT_LATCH_RELEASED",
		"STAGED_SUPPORT_LATCH_SKIP",
		"STAGED_SUPPORT_LATCH_RELEASE_SKIPPED",
		"ORCHESTRATOR_ENGINE_SHA",
	):
		assert f"- `{prefix}`" in agents_text
		assert f"LOG_PREFIX.name={prefix}" in agents_text


def test_staged_support_latch_sweep_runs_without_tracking_issues() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	step_start = wf.index("- name: Release staged-support latches without active projects")
	step_end = wf.index("- name: Run worktree registry GC", step_start)
	step = wf[step_start:step_end]
	assert "steps.find_tracking.outputs.has_work != 'true'" in step
	assert "github.repository == 'shubhodeep1/coding-workflows'" in step
	assert "STAGED_SUPPORT_LATCH_ALERT_MSG_LEVEL: ${{ vars.ALERT_MSG_LEVEL || 'DEBUG' }}" in step
	assert 'STAGED_SUPPORT_LATCH_SWEEP_ONLY: "true"' in step
	assert 'run: ALERT_MSG_LEVEL="${ALERT_MSG_LEVEL:-${STAGED_SUPPORT_LATCH_ALERT_MSG_LEVEL}}" bash scripts/orchestrate_poll_process.sh' in step
	assert 'if _is_truthy "${STAGED_SUPPORT_LATCH_SWEEP_ONLY:-false}"; then' in poller
	assert "release_staged_support_needs_human_latches\n  exit 0" in poller


def test_worktree_registry_helpers_and_gc_are_wired_into_poller_workflow() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	assert "worktree_registry.sh" in wf
	assert "worktree_gc.sh" in wf
	assert "ORCH_WORKTREE_REGISTRY_ENABLED: ${{ vars.ORCH_WORKTREE_REGISTRY_ENABLED || 'false' }}" in wf
	assert "ORCH_WORKTREE_TTL_SECS: ${{ vars.ORCH_WORKTREE_TTL_SECS || '3600' }}" in wf
	assert "- name: Run worktree registry GC" in wf
	assert "if: steps.find_tracking.outputs.has_work == 'true'\n        run: bash scripts/worktree_gc.sh" not in wf
	assert 'WORKTREE_REGISTRY_ROOT="${GITHUB_WORKSPACE}" PYTHONSAFEPATH=1 bash "${POLLER_TRUSTED_SUPPORT_DIR}/scripts/worktree_gc.sh"' in wf
	assert 'python3 -I "${POLLER_TRUSTED_SUPPORT_DIR}/scripts/build_state_snapshot.py"' in wf
	assert '--schema-root "${POLLER_TRUSTED_SUPPORT_DIR}/ai-memory"' in wf
	assert 'source "${POLLER_TRUSTED_SUPPORT_DIR}/scripts/memory_helpers.sh"' in wf
	assert 'source "${support_dir}/scripts/tg_helpers.sh"' in wf
	assert 'source scripts/tg_helpers.sh' not in wf


def test_post_checkout_helpers_are_resolved_only_from_verified_support() -> None:
	wf = _workflow()
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	for dependency in (
		"orchestrate_state_v2.py", "ai_labels.py", "check_integration_pr_readiness.py",
		"security_audit_causality.py", "worktree_registry.sh", "build_state_snapshot.py",
		"gh_helpers.sh", "tg_helpers.sh", "memory_helpers.sh", "blocker_check.py",
		"state_snapshot.v1.json", "label_contract.v1.json",
	):
		assert dependency in wf
	for dependency in (
		"scripts/orchestrate_state_v2.py", "scripts/ai_labels.py",
		"scripts/check_integration_pr_readiness.py", "scripts/security_audit_causality.py",
		".github/ai/label_contract.v1.json",
	):
		assert f"poller_trusted_support_file {dependency}" in poller
	assert "python3 scripts/orchestrate_state_v2.py" not in poller
	assert "python3 scripts/ai_labels.py" not in poller
	assert "python3 scripts/check_integration_pr_readiness.py" not in poller
	assert "sys.executable, \"-I\", str(causality_helper)" in poller
	assert 'source "${POLLER_TRUSTED_SUPPORT_DIR}/scripts/memory_helpers.sh"' in wf
	assert 'source "${support_dir}/scripts/tg_helpers.sh"' in wf


def test_telegram_helper_does_not_source_checkout_rate_limit_helper(tmp_path: Path) -> None:
	trusted = tmp_path / "trusted" / "scripts"
	trusted.mkdir(parents=True)
	for name in ("tg_helpers.sh", "gh_helpers.sh", "emit_event.sh"):
		(trusted / name).write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
	checkout = tmp_path / "checkout"
	(checkout / "scripts").mkdir(parents=True)
	marker = tmp_path / "executed"
	(checkout / "scripts" / "gh_helpers.sh").write_text(f'touch "{marker}"\n', encoding="utf-8")
	env = os.environ.copy()
	env.pop("BASH_ENV", None)
	result = subprocess.run(["bash", "-c", 'source "$1"', "bash", str(trusted / "tg_helpers.sh")], cwd=checkout, env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert not marker.exists()


def main() -> int:
	test_stall_control_env_defaults_are_declared()
	test_stall_recovery_prompt_is_bootstrapped_with_main_fallback()
	test_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_orchestrate_workflow_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_nag_reminder_assets_and_judge_wiring_are_present()
	test_task_state_helper_and_flag_are_wired_into_poller_workflow()
	test_security_pass_dark_launch_env_and_assets_are_wired()
	test_security_pass_recovery_log_prefixes_are_registered()
	test_staged_support_latch_sweep_runs_without_tracking_issues()
	test_worktree_registry_helpers_and_gc_are_wired_into_poller_workflow()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

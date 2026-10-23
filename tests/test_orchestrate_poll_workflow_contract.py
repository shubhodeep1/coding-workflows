#!/usr/bin/env python3
"""Contract tests for orchestrate_poll workflow env mapping."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agents_doc import agents_text as _agents_text_with_fragments  # noqa: E402


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
	assert "SECURITY_PASS_EXHAUSTION_JUDGE_ENABLED: ${{ vars.SECURITY_PASS_EXHAUSTION_JUDGE_ENABLED || 'true' }}" in wf
	assert "MAX_SECURITY_PASS_JUDGE_ROUNDS: ${{ vars.MAX_SECURITY_PASS_JUDGE_ROUNDS || '0' }}" in wf
	assert "MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS: ${{ vars.MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS || '2' }}" in wf
	assert "SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED: ${{ vars.SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED || 'true' }}" in wf
	assert "SECURITY_PASS_AUTO_RESET_ON_ENGINE_CHANGE: ${{ vars.SECURITY_PASS_AUTO_RESET_ON_ENGINE_CHANGE || 'true' }}" in wf
	assert "VALIDATION_STALE_PIN_AUTO_RECOVER_ENABLED: ${{ vars.VALIDATION_STALE_PIN_AUTO_RECOVER_ENABLED || 'true' }}" in wf
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
		"references/output-contract.txt",
		"contracts/mode-security-audit.yml",
	):
		assert asset in wf
	# Issue #7652: the audit renders from a worktree with no
	# .codex-workflow-src/, so the output-contract reference must be staged
	# and required before the audit runs.
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	loop_start = poller.index("  for required_security_asset in \\\n")
	loop_end = poller.index("; do", loop_start)
	assert "prompts/references/output-contract.txt" in poller[loop_start:loop_end]
	assert "prompts/contracts/mode-security-audit.yml" in poller[loop_start:loop_end]


def test_security_pass_recovery_log_prefixes_are_registered() -> None:
	agents_text = _agents_text_with_fragments()
	for prefix in (
		"REISSUE_FILES_TOUCHED_UNION",
		"REISSUE_FILES_TOUCHED_NEW_OUTPUTS",
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


def test_validation_run_attribution_log_prefix_is_registered() -> None:
	agents_text = _agents_text_with_fragments()
	assert "- `VALIDATION_RUN_ATTRIBUTION`" in agents_text
	assert "LOG_PREFIX.name=VALIDATION_RUN_ATTRIBUTION" in agents_text


def test_staged_support_latch_sweep_runs_without_tracking_issues() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	step_start = wf.index("- name: Release staged-support latches without active projects")
	step_end = wf.index("- name: Run worktree registry GC", step_start)
	step = wf[step_start:step_end]
	assert (
		"if: ${{ !cancelled() && steps.find_tracking.outcome == 'success' "
		"&& steps.find_tracking.outputs.has_work != 'true' "
		"&& github.repository == 'shubhodeep1/coding-workflows' }}"
	) in step
	assert "STAGED_SUPPORT_LATCH_ALERT_MSG_LEVEL: ${{ vars.ALERT_MSG_LEVEL || 'WARNING' }}" in step
	assert 'STAGED_SUPPORT_LATCH_SWEEP_ONLY: "true"' in step
	assert 'run: ALERT_MSG_LEVEL="${ALERT_MSG_LEVEL:-${STAGED_SUPPORT_LATCH_ALERT_MSG_LEVEL}}" bash scripts/orchestrate_poll_process.sh' in step
	assert 'if _is_truthy "${STAGED_SUPPORT_LATCH_SWEEP_ONLY:-false}"; then' in poller
	assert "release_staged_support_needs_human_latches\n  exit 0" in poller


def test_unblock_scan_runs_without_tracking_issues() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	latch_start = wf.index("- name: Release staged-support latches without active projects")
	step_start = wf.index("- name: Run unblock scan without active projects")
	step_end = wf.index("- name: Run worktree registry GC", step_start)
	step = wf[step_start:step_end]
	assert latch_start < step_start < step_end
	assert (
		"if: ${{ !cancelled() && steps.find_tracking.outcome == 'success' "
		"&& steps.find_tracking.outputs.has_work != 'true' }}"
	) in step
	assert "github.repository ==" not in step
	assert 'UNBLOCK_SCAN_SWEEP_ONLY: "true"' in step
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in step
	assert "TG_BOT_SECRET: ${{ secrets.TG_BOT_SECRET }}" in step
	assert "TG_ADMIN_CHAT_ID: ${{ vars.TG_ADMIN_CHAT_ID || '' }}" in step
	assert "ALERT_MSG_LEVEL: ${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'WARNING' }}" in step
	assert 'PYTHONDONTWRITEBYTECODE: "1"' in step
	assert "run: bash scripts/orchestrate_poll_process.sh" in step
	for name, default in (
		("UNBLOCK_JUDGE_ENABLED", "true"),
		("UNBLOCK_JUDGE_MAX_DISPATCH_PER_TICK", "5"),
		("UNBLOCK_JUDGE_MIN_BLOCKED_MINUTES", "30"),
		("UNBLOCK_JUDGE_RETRY_HOURS", "6"),
		("UNBLOCK_JUDGE_INFLIGHT_MINUTES", "60"),
		("UNBLOCK_JUDGE_ESCALATED_RECHECK_HOURS", "24"),
	):
		assert f"{name}: ${{{{ vars.{name} || '{default}' }}}}" in step
	assert (
		'if _is_truthy "${UNBLOCK_SCAN_SWEEP_ONLY:-false}"; then\n'
		'  run_unblock_scan || echo "UNBLOCK_SCAN outcome=skip reason=error rc=$?"\n'
		'  exit 0'
	) in poller
	yaml.safe_load(wf)


WIDENED_GATE = (
	"(steps.find_tracking.outputs.has_work == 'true' || "
	"steps.find_tracking.outputs.standalone_recovery == 'true')"
)


def _step_block(wf: str, name: str) -> str:
	start = wf.index(f"      - name: {name}\n")
	end = wf.find("\n      - name: ", start + 1)
	return wf[start:] if end == -1 else wf[start:end]


def test_standalone_stall_recovery_runs_without_tracking_issues() -> None:
	"""Issue #7183: no-project ticks still install the poller toolchain."""
	wf = _workflow(ORCHESTRATE_POLL_WF)

	find = _step_block(wf, "Find active tracking issues")
	assert "ENABLE_STANDALONE_STALL_RECOVERY: ${{ vars.ENABLE_STANDALONE_STALL_RECOVERY || 'true' }}" in find
	assert "1|true|yes|on) standalone_recovery=true ;;" in find
	assert 'echo "standalone_recovery=${standalone_recovery}" >> "$GITHUB_OUTPUT"' in find
	assert 'echo "standalone_recovery=false" >> "$GITHUB_OUTPUT"' in find
	assert "Standalone stall recovery without active projects: standalone_recovery=" in find

	for name in (
		"Setup Node.js",
		"Setup Python",
		"Install Codex CLI",
		"Install OS-level core tools",
	):
		assert f"if: {WIDENED_GATE}\n" in _step_block(wf, name), name

	validate = _step_block(wf, "Validate required environment variables")
	assert "id: validate_env\n" in validate
	assert f"if: {WIDENED_GATE}\n" in validate
	assert "continue-on-error: ${{ steps.find_tracking.outputs.has_work != 'true' }}" in validate
	assert "id: stage_support\n" in _step_block(wf, "Stage workflow support files")

	# The recovery step runs only after both idle-tick prerequisites succeeded.
	recovery = _step_block(wf, STANDALONE_SWEEP_STEP)
	assert (
		"if: steps.find_tracking.outputs.has_work != 'true' "
		"&& steps.validate_env.outcome == 'success' "
		"&& steps.stage_support.outcome == 'success'\n"
	) in recovery
	assert 'STANDALONE_RECOVERY_SWEEP_ONLY: "true"' in recovery
	assert "bash scripts/orchestrate_poll_process.sh" in recovery
	yaml.safe_load(wf)


def test_standalone_pr_sweeps_run_without_tracking_issues() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	unblock_start = wf.index("- name: Run unblock scan without active projects")
	step_start = wf.index("- name: Run standalone PR sweeps without active projects")
	step_end = wf.index("- name: Run worktree registry GC", step_start)
	step = wf[step_start:step_end]
	assert unblock_start < step_start < step_end
	assert "if: steps.find_tracking.outputs.has_work != 'true'" in step
	assert "github.repository ==" not in step
	assert 'STANDALONE_PR_SWEEP_ONLY: "true"' in step
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in step
	assert "TG_BOT_SECRET: ${{ secrets.TG_BOT_SECRET }}" in step
	assert "TG_ADMIN_CHAT_ID: ${{ vars.TG_ADMIN_CHAT_ID || '' }}" in step
	assert "ALERT_MSG_LEVEL: ${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'WARNING' }}" in step
	assert "STANDALONE_SWEEP_ALERT_MSG_LEVEL: ${{ vars.ALERT_MSG_LEVEL || 'WARNING' }}" in step
	assert 'PYTHONDONTWRITEBYTECODE: "1"' in step
	assert "ALLOW_WORKFLOW_EDITS: ${{ vars.ALLOW_WORKFLOW_EDITS || 'true' }}" in step
	assert "continue-on-error: true" in step
	assert "for f in validate_editor_audit.sh; do" in wf
	main_start = wf.index("- name: Process each tracking issue")
	main_end = wf.index("- name: Release staged-support latches without active projects", main_start)
	main_step = wf[main_start:main_end]
	for name, default in (
		("STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS", "3"),
		("STALL_THRESHOLD_MINUTES", "120"),
		("REVIEW_RUN_MAX_RUNTIME_MINUTES", "250"),
		("MERGE_BASE_FRESHNESS_ENABLED", "true"),
		("MERGE_BASE_FRESHNESS_MAX_SYNCS", "2"),
		("ORCH_PR_AUTOFIX_FLOW_ENABLED", "true"),
	):
		line = f"{name}: ${{{{ vars.{name} || '{default}' }}}}"
		assert line in step, line
		assert line in main_step, line
	# The main poller passes the sweep level from vars, never from env (which
	# carries find_tracking's all-smoke SILENT override).
	assert "STANDALONE_SWEEP_ALERT_MSG_LEVEL: ${{ vars.ALERT_MSG_LEVEL || 'WARNING' }}" in main_step
	assert "STANDALONE_SWEEP_ALERT_MSG_LEVEL: ${{ env." not in wf
	# An older staged poller without the mode must not run its full post-loop path.
	assert "grep -q 'STANDALONE_PR_SWEEP_ONLY' scripts/orchestrate_poll_process.sh" in step
	assert "::warning::Standalone PR sweep-only support is not yet staged." in step
	assert "bash scripts/orchestrate_poll_process.sh" in step
	assert 'if _is_truthy "${STANDALONE_PR_SWEEP_ONLY:-false}"; then' in poller
	assert 'if [ "${STANDALONE_PR_SWEEP_ONLY_ACTIVE}" != "true" ]; then\n  run_standalone_stall_recovery' in poller
	assert poller.rstrip().endswith("_standalone_sweep_alert_level_restore")
	yaml.safe_load(wf)


STANDALONE_SWEEP_STEP = "Run standalone stall recovery without active projects"
STANDALONE_SWEEP_POLLER_GUARD = 'if _is_truthy "${STANDALONE_RECOVERY_SWEEP_ONLY:-false}"; then'


def test_standalone_recovery_sweep_runs_without_tracking_issues() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	steps = yaml.safe_load(wf)["jobs"]["poll"]["steps"]
	names = [step.get("name") for step in steps]
	sweep = steps[names.index(STANDALONE_SWEEP_STEP)]
	full = steps[names.index("Process each tracking issue")]
	# Exactly one of the two runs per tick, in every repository.
	assert full["if"] == "steps.find_tracking.outputs.has_work == 'true'"
	assert sweep["if"] == (
		"steps.find_tracking.outputs.has_work != 'true' "
		"&& steps.validate_env.outcome == 'success' "
		"&& steps.stage_support.outcome == 'success'"
	)
	assert "github.repository" not in sweep["if"]
	# A recovery failure must not skip the later idle-tick sweeps.
	assert sweep.get("continue-on-error") is True
	# Same order as the full poller: recovery, latch release, unblock scan.
	assert (
		names.index("Process each tracking issue")
		< names.index(STANDALONE_SWEEP_STEP)
		< names.index("Release staged-support latches without active projects")
		< names.index("Run unblock scan without active projects")
	)
	# Env parity: the full poller's env plus the sweep flag, nothing else.
	sweep_env = dict(sweep["env"])
	assert sweep_env.pop("STANDALONE_RECOVERY_SWEEP_ONLY") == "true"
	assert sweep_env == full["env"]
	for name in ("AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED", "ENABLE_STANDALONE_STALL_RECOVERY", "ENABLE_STALL_JUDGE", "CLAUDE_POOL_REASON"):
		assert name in sweep_env
	# Older support copies without the mode never launch the full poller.
	run = sweep["run"]
	assert f"grep -qF '{STANDALONE_SWEEP_POLLER_GUARD}' scripts/orchestrate_poll_process.sh" in run
	assert run.index("exit 0") < run.index("bash scripts/orchestrate_poll_process.sh")
	# The poller mode runs recovery only and sits after the other sweep modes.
	# Caps are primed first so idle-tick dispatches honour concurrency_caps.yml.
	block = (
		STANDALONE_SWEEP_POLLER_GUARD
		+ '\n  prime_phase_concurrency_snapshot ".github/ai/concurrency_caps.yml"'
		+ "\n  run_standalone_stall_recovery\n  exit 0\nfi"
	)
	assert poller.count(block) == 1
	assert poller.index('if _is_truthy "${UNBLOCK_SCAN_SWEEP_ONLY:-false}"; then') < poller.index(block)
	assert poller.index(block) < poller.index("# Process each tracking issue")


def test_engine_and_claude_steps_run_on_idle_ticks_when_needed() -> None:
	steps = yaml.safe_load(_workflow(ORCHESTRATE_POLL_WF))["jobs"]["poll"]["steps"]
	by_name = {step.get("name"): step for step in steps}
	assert "if" not in by_name["Resolve AI engine"]
	for name in ("Install Claude Code CLI", "Resolve Claude credential", "Alert on Claude pool accounts at the usage gate"):
		# Issue #7370: with a capacity tracker open the pool is probed only in
		# the hourly window.
		assert by_name[name]["if"] == "(steps.ai_engine.outputs.any_claude == 'true' && steps.find_tracking.outputs.capacity_wait != 'true') || (steps.find_tracking.outputs.capacity_wait == 'true' && steps.pool_window.outputs.in_window == 'true')", name


VALIDATION_HARNESS_ENV_LINES = (
	"VALIDATION_HARNESS_HEAL_ENABLED: ${{ vars.VALIDATION_HARNESS_HEAL_ENABLED || 'true' }}",
	"VALIDATION_HARNESS_HOLD_MAX_DAYS: ${{ vars.VALIDATION_HARNESS_HOLD_MAX_DAYS || '14' }}",
	"VALIDATION_HARNESS_MAX_RESUMES_PER_FP: ${{ vars.VALIDATION_HARNESS_MAX_RESUMES_PER_FP || '2' }}",
	"VALIDATION_HARNESS_MAX_RESUMES: ${{ vars.VALIDATION_HARNESS_MAX_RESUMES || '4' }}",
	"VALIDATION_HARNESS_ROUTE_LOGIN: ${{ vars.VALIDATION_HARNESS_ROUTE_LOGIN || '' }}",
)


def test_validation_harness_hold_env_is_wired_into_both_poller_steps() -> None:
	"""Refs #7913: the harness hold switches reach every step that runs the full poller."""
	wf = _workflow(ORCHESTRATE_POLL_WF)
	for step_name in ("Process each tracking issue", STANDALONE_SWEEP_STEP):
		block = _step_block(wf, step_name)
		for line in VALIDATION_HARNESS_ENV_LINES:
			assert line in block, (step_name, line)
	poller = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	assert '_validation_harness_int_config VALIDATION_HARNESS_HOLD_MAX_DAYS 14 1 365' in poller
	assert '_validation_harness_int_config VALIDATION_HARNESS_MAX_RESUMES_PER_FP 2 0 100' in poller
	assert '_validation_harness_int_config VALIDATION_HARNESS_MAX_RESUMES 4 0 100' in poller
	yaml.safe_load(wf)


def test_worktree_registry_helpers_and_gc_are_wired_into_poller_workflow() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	assert "worktree_registry.sh" in wf
	assert "worktree_gc.sh" in wf
	assert "ORCH_WORKTREE_REGISTRY_ENABLED: ${{ vars.ORCH_WORKTREE_REGISTRY_ENABLED || 'false' }}" in wf
	assert "ORCH_WORKTREE_TTL_SECS: ${{ vars.ORCH_WORKTREE_TTL_SECS || '3600' }}" in wf
	assert "- name: Run worktree registry GC" in wf
	assert "if: steps.find_tracking.outputs.has_work == 'true'\n        run: bash scripts/worktree_gc.sh" not in wf
	assert "run: bash scripts/worktree_gc.sh" in wf


STANDALONE_PROBE_LABELS = (
	"ai:clarification",
	"ai:planning",
	"ai:awaiting-approval",
	"ai:implementing",
	"ai:done",
	"ai:ready-to-merge",
	"ai:review-blocked",
)


def _find_tracking_step() -> dict:
	data = yaml.safe_load(_workflow(ORCHESTRATE_POLL_WF))
	return next(step for step in data["jobs"]["poll"]["steps"] if step.get("id") == "find_tracking")


def _probe_response(labeled: int = 0, state: int = 0, clarify: int = 0) -> dict:
	repository = {f"l{i}": {"totalCount": 0} for i in range(7)}
	repository["l3"]["totalCount"] = labeled
	return {"data": {"repository": repository, "state": {"issueCount": state}, "clarify": {"issueCount": clarify}}}


def _run_find_tracking(
	tmp_path: Path,
	*,
	tracking: list[dict] | None = None,
	graphql: object | None = None,
	graphql_rc: int = 0,
	enabled: str = "true",
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], list[list[str]]]:
	"""Run the find_tracking step's shell body against a fake gh (issue #7284)."""
	script = _find_tracking_step()["run"].replace("${{ github.repository }}", "owner/repo")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	runtime_dir = tmp_path / "runtime"
	runtime_dir.mkdir()
	calls_file = tmp_path / "gh_calls.jsonl"
	tracking_payload = json.dumps(tracking or [])
	graphql_payload = graphql if isinstance(graphql, str) else json.dumps(graphql if graphql is not None else _probe_response())
	gh = bin_dir / "gh"
	gh.write_text(
		"#!/usr/bin/env python3\n"
		"import json, os, sys\n"
		"args = sys.argv[1:]\n"
		"with open(os.environ['FAKE_GH_CALLS'], 'a', encoding='utf-8') as fh:\n"
		"    fh.write(json.dumps(args) + '\\n')\n"
		"if args[:2] == ['issue', 'list']:\n"
		"    sys.stdout.write(os.environ['FAKE_GH_TRACKING'])\n"
		"    sys.exit(0)\n"
		"if args[:2] == ['api', 'graphql']:\n"
		"    rc = int(os.environ['FAKE_GH_GRAPHQL_RC'])\n"
		"    if rc:\n"
		"        sys.exit(rc)\n"
		"    sys.stdout.write(os.environ['FAKE_GH_GRAPHQL'])\n"
		"    sys.exit(0)\n"
		"sys.exit(9)\n",
		encoding="utf-8",
	)
	gh.chmod(0o755)
	output_file = tmp_path / "github_output"
	output_file.write_text("", encoding="utf-8")
	env = {
		**os.environ,
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"RUNTIME_DIR": str(runtime_dir),
		"GITHUB_OUTPUT": str(output_file),
		"GITHUB_ENV": str(tmp_path / "github_env"),
		"GITHUB_REPOSITORY": "owner/repo",
		"ENABLE_STANDALONE_STALL_RECOVERY": enabled,
		"FAKE_GH_CALLS": str(calls_file),
		"FAKE_GH_TRACKING": tracking_payload,
		"FAKE_GH_GRAPHQL": graphql_payload,
		"FAKE_GH_GRAPHQL_RC": str(graphql_rc),
	}
	for inherited in ("GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "BASH_ENV", "ENV", "WORKSPACE_PATH"):
		env.pop(inherited, None)
	# No scripts/gh_helpers.sh in tmp_path, so the step's gh_retry fallback runs.
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
	outputs = dict(
		line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line
	)
	calls = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines()] if calls_file.exists() else []
	return result, outputs, calls


def _graphql_calls(calls: list[list[str]]) -> list[list[str]]:
	return [call for call in calls if call[:2] == ["api", "graphql"]]


def test_find_tracking_step_maps_standalone_recovery_switch() -> None:
	wf = _workflow(ORCHESTRATE_POLL_WF)
	step = _find_tracking_step()
	assert step["env"]["ENABLE_STANDALONE_STALL_RECOVERY"] == "${{ vars.ENABLE_STANDALONE_STALL_RECOVERY || 'true' }}"
	start = wf.index("- name: Find active tracking issues")
	end = wf.index("- name: Checkout repository", start)
	assert "ENABLE_STANDALONE_STALL_RECOVERY: ${{ vars.ENABLE_STANDALONE_STALL_RECOVERY || 'true' }}" in wf[start:end]


def test_find_tracking_with_tracking_issues_skips_standalone_probe(tmp_path: Path) -> None:
	result, outputs, calls = _run_find_tracking(tmp_path, tracking=[{"number": 5, "title": "Project", "labels": []}])
	assert result.returncode == 0, result.stderr
	assert outputs["has_tracking"] == "true"
	assert outputs["has_work"] == "true"
	assert not _graphql_calls(calls)
	assert "STANDALONE_STALL_PROBE" not in result.stdout


@pytest.mark.parametrize("enabled", ("false", "FALSE", "0", "off"))
def test_find_tracking_standalone_probe_disabled(tmp_path: Path, enabled: str) -> None:
	result, outputs, calls = _run_find_tracking(tmp_path, enabled=enabled)
	assert result.returncode == 0, result.stderr
	assert outputs == {"capacity_wait": "false", "has_tracking": "false", "standalone_recovery": "false", "has_work": "false"}
	assert "STANDALONE_STALL_PROBE outcome=skip reason=disabled" in result.stdout
	assert not _graphql_calls(calls)


@pytest.mark.parametrize(
	("counts", "has_work", "outcome"),
	(
		({}, "false", "outcome=none labeled=0 state_marker=0 clarify_marker=0"),
		({"labeled": 2}, "true", "outcome=candidates labeled=2 state_marker=0 clarify_marker=0"),
		({"state": 1}, "true", "outcome=candidates labeled=0 state_marker=1 clarify_marker=0"),
		({"clarify": 3}, "true", "outcome=candidates labeled=0 state_marker=0 clarify_marker=3"),
	),
)
def test_find_tracking_standalone_probe_counts(tmp_path: Path, counts: dict, has_work: str, outcome: str) -> None:
	result, outputs, calls = _run_find_tracking(tmp_path, graphql=_probe_response(**counts), enabled="On")
	assert result.returncode == 0, result.stderr
	assert outputs == {"capacity_wait": "false", "has_tracking": "false", "standalone_recovery": "true", "has_work": has_work}
	assert f"STANDALONE_STALL_PROBE {outcome}" in result.stdout
	graphql = _graphql_calls(calls)
	assert len(graphql) == 1
	call = graphql[0]
	assert "owner=owner" in call
	assert "name=repo" in call
	assert 'q_state=repo:owner/repo is:issue is:open "AI_STANDALONE_STALL_STATE_V1" in:comments' in call
	assert 'q_clarify=repo:owner/repo is:issue is:open "ai:clarification-questions" in:comments' in call
	query = next(arg for arg in call if arg.startswith("query="))
	for label in STANDALONE_PROBE_LABELS:
		assert f'labels: ["{label}"]' in query
	assert query.count("totalCount") == len(STANDALONE_PROBE_LABELS)
	assert query.count("issueCount") == 2


@pytest.mark.parametrize(
	("kwargs", "reason"),
	(
		({"graphql_rc": 1}, "graphql_failed"),
		({"graphql": "not json"}, "malformed"),
		({"graphql": {"data": {"repository": None}}}, "malformed"),
		({"graphql": {"data": {"repository": {f"l{i}": {"totalCount": 0} for i in range(6)}, "state": {"issueCount": 0}, "clarify": {"issueCount": 0}}}}, "malformed"),
		({"graphql": {"data": {"repository": {f"l{i}": {"totalCount": "1"} for i in range(7)}, "state": {"issueCount": 0}, "clarify": {"issueCount": 0}}}}, "malformed"),
	),
)
def test_find_tracking_standalone_probe_fails_open(tmp_path: Path, kwargs: dict, reason: str) -> None:
	result, outputs, _calls = _run_find_tracking(tmp_path, **kwargs)
	assert result.returncode == 0, result.stderr
	assert outputs == {"capacity_wait": "false", "has_tracking": "false", "standalone_recovery": "true", "has_work": "true"}
	assert f"STANDALONE_STALL_PROBE outcome=probe_failed reason={reason}" in result.stdout
	assert "::warning::Standalone stall recovery probe" in result.stdout


def test_standalone_stall_probe_log_prefix_is_registered() -> None:
	agents_text = _agents_text_with_fragments()
	assert "- `STANDALONE_STALL_PROBE`" in agents_text
	assert "LOG_PREFIX.name=STANDALONE_STALL_PROBE" in agents_text


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
	test_unblock_scan_runs_without_tracking_issues()
	test_standalone_stall_recovery_runs_without_tracking_issues()
	test_standalone_pr_sweeps_run_without_tracking_issues()
	test_standalone_recovery_sweep_runs_without_tracking_issues()
	test_engine_and_claude_steps_run_on_idle_ticks_when_needed()
	test_validation_harness_hold_env_is_wired_into_both_poller_steps()
	test_worktree_registry_helpers_and_gc_are_wired_into_poller_workflow()
	test_find_tracking_step_maps_standalone_recovery_switch()
	test_standalone_stall_probe_log_prefix_is_registered()
	# CI runs this file directly, so run the probe cases here too (#7284).
	import tempfile

	def _with_tmp(fn, *args) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			fn(Path(tmp), *args)

	_with_tmp(test_find_tracking_with_tracking_issues_skips_standalone_probe)
	for enabled in ("false", "FALSE", "0", "off"):
		_with_tmp(test_find_tracking_standalone_probe_disabled, enabled)
	for counts, has_work, outcome in (
		({}, "false", "outcome=none labeled=0 state_marker=0 clarify_marker=0"),
		({"labeled": 2}, "true", "outcome=candidates labeled=2 state_marker=0 clarify_marker=0"),
		({"state": 1}, "true", "outcome=candidates labeled=0 state_marker=1 clarify_marker=0"),
		({"clarify": 3}, "true", "outcome=candidates labeled=0 state_marker=0 clarify_marker=3"),
	):
		_with_tmp(test_find_tracking_standalone_probe_counts, counts, has_work, outcome)
	for kwargs, reason in (
		({"graphql_rc": 1}, "graphql_failed"),
		({"graphql": "not json"}, "malformed"),
		({"graphql": {"data": {"repository": None}}}, "malformed"),
		({"graphql": {"data": {"repository": {f"l{i}": {"totalCount": 0} for i in range(6)}, "state": {"issueCount": 0}, "clarify": {"issueCount": 0}}}}, "malformed"),
		({"graphql": {"data": {"repository": {f"l{i}": {"totalCount": "1"} for i in range(7)}, "state": {"issueCount": 0}, "clarify": {"issueCount": 0}}}}, "malformed"),
	):
		_with_tmp(test_find_tracking_standalone_probe_fails_open, kwargs, reason)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

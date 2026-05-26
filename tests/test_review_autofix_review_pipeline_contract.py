#!/usr/bin/env python3
"""Contract tests for review-pipeline plumbing in review_autofix.yml."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
REVIEWERS = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _reviewers_text() -> str:
	return REVIEWERS.read_text(encoding="utf-8")


def _apply_fixes_text() -> str:
	return APPLY_FIXES.read_text(encoding="utf-8")


def _step_block(step_name: str) -> str:
	lines = _workflow_text().splitlines()
	needle = f"- name: {step_name}"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		step_indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:"):
				indent = len(candidate) - len(candidate.lstrip(" "))
				if indent == step_indent:
					end = j
					break
		return "\n".join(lines[idx:end])
	raise AssertionError(f"Step not found in workflow: {step_name}")


def _reviewer_iteration_scope_helper_block() -> str:
	text = _reviewers_text()
	start = text.index("# ── Reviewer iteration-scoping helpers")
	end = text.index("# ── End reviewer iteration-scoping helpers", start)
	return text[start:end]


def _run_reviewer_scope_harness(*, scope_mode: str, last_run_changed_text: str, ledger_text: str) -> dict[str, str]:
	helper_block = _reviewer_iteration_scope_helper_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-iteration-scope-") as td:
		tmp = Path(td)
		workspace = tmp / "workspace"
		workspace.mkdir()
		files = {
			"last_run_changed": tmp / "last_run_changed_files.txt",
			"ledger": tmp / "ledger_status.txt",
			"scope_paths": tmp / "reviewer_scope_paths.txt",
			"scope_summary": tmp / "reviewer_scope_summary.txt",
			"scope_context": tmp / "reviewer_scoped_files_context.txt",
			"scope_query_seed": tmp / "reviewer_scope_query_seed.txt",
			"scope_context_source": tmp / "scope_context_source.txt",
			"semble_query": tmp / "reviewer_semble_query.txt",
			"context_sections": tmp / "context_sections.txt",
			"scoped_active": tmp / "scoped_active.txt",
			"symbol_diff": tmp / "symbol_diff_summary.txt",
			"original_pr_diff": tmp / "original_pr_diff.patch",
			"last_run_diff": tmp / "last_run_diff.patch",
			"pr_changed": tmp / "pr_changed_files.txt",
			"last_run_diff_stat": tmp / "last_run_diff_stat.txt",
			"last_commit_stat": tmp / "last_commit_stat.txt",
			"comments": tmp / "comments.txt",
			"checks": tmp / "checks.txt",
			"pr_diff": tmp / "pr_diff.patch",
		}
		files["last_run_changed"].write_text(last_run_changed_text, encoding="utf-8")
		files["ledger"].write_text(ledger_text, encoding="utf-8")
		files["scope_paths"].write_text("", encoding="utf-8")
		files["scope_summary"].write_text("", encoding="utf-8")
		files["scope_context"].write_text("", encoding="utf-8")
		files["scope_context_source"].write_text(
			"=== TARGETED FILE CONTEXT ===\nScoped reviewer file context sentinel\n",
			encoding="utf-8",
		)
		files["symbol_diff"].write_text("symbol diff sentinel\n", encoding="utf-8")
		files["original_pr_diff"].write_text("original pr diff sentinel\n", encoding="utf-8")
		files["last_run_diff"].write_text("last run diff sentinel\n", encoding="utf-8")
		files["pr_changed"].write_text("scripts/review_run_reviewers.sh\nextra/pr_scope.py\n", encoding="utf-8")
		files["last_run_diff_stat"].write_text("1 file changed\n", encoding="utf-8")
		files["last_commit_stat"].write_text("commit stat sentinel\n", encoding="utf-8")
		files["comments"].write_text("comments sentinel\n", encoding="utf-8")
		files["checks"].write_text("checks sentinel\n", encoding="utf-8")
		files["pr_diff"].write_text("full pr diff sentinel\n", encoding="utf-8")
		(workspace / "scripts").mkdir()
		(workspace / "tests").mkdir()
		(workspace / "scripts" / "review_run_reviewers.sh").write_text("scoped shell target\n", encoding="utf-8")
		(workspace / "tests" / "test_review_autofix_review_pipeline_contract.py").write_text("scoped test target\n", encoding="utf-8")

		env = os.environ.copy()
		env.update({
			"SUPPORT_ROOT_DIR": str(REPO_ROOT),
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"LAST_RUN_CHANGED_FILES_FILE": str(files["last_run_changed"]),
			"LEDGER_STATUS_FILE": str(files["ledger"]),
			"REVIEWER_SCOPE_PATHS_FILE": str(files["scope_paths"]),
			"REVIEWER_SCOPE_SUMMARY_FILE": str(files["scope_summary"]),
			"REVIEWER_SCOPED_FILES_CONTEXT_FILE": str(files["scope_context"]),
			"REVIEWER_SCOPE_QUERY_SEED_FILE": str(files["scope_query_seed"]),
			"SCOPE_CONTEXT_SOURCE_FILE": str(files["scope_context_source"]),
			"REVIEWER_SEMBLE_QUERY_FILE": str(files["semble_query"]),
			"OUTPUT_CONTEXT_FILE": str(files["context_sections"]),
			"SCOPED_ACTIVE_FILE": str(files["scoped_active"]),
			"SYMBOL_DIFF_SUMMARY_FILE": str(files["symbol_diff"]),
			"ORIGINAL_PR_DIFF_FILE": str(files["original_pr_diff"]),
			"LAST_RUN_DIFF_FILE": str(files["last_run_diff"]),
			"PR_CHANGED_FILES_FILE": str(files["pr_changed"]),
			"LAST_RUN_DIFF_STAT_FILE": str(files["last_run_diff_stat"]),
			"LAST_COMMIT_STAT_FILE": str(files["last_commit_stat"]),
			"PR_ALL_COMMENTS_CONTEXT_FILE": str(files["comments"]),
			"PR_CHECK_RUNS_CONTEXT_FILE": str(files["checks"]),
			"PR_DIFF_FILE": str(files["pr_diff"]),
			"TARGETED_FILE_CONTEXT_SCRIPT": str(REPO_ROOT / "scripts" / "targeted_file_context.py"),
			"TARGETED_FILE_CONTEXT_MAX_BYTES": "8192",
			"GITHUB_WORKSPACE": str(workspace),
			"SEMBLE_INDEX_AVAILABLE": "false",
			"SCOPE_MODE": scope_mode,
			"USE_PREPARE": "0",
		})
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				"_embed_input_file() { local _p=\"${1:-}\"; if [ -z \"${_p}\" ] || [ ! -e \"${_p}\" ]; then printf '(missing)\\n'; return 0; fi; if [ ! -s \"${_p}\" ]; then printf '(empty)\\n'; return 0; fi; cat \"${_p}\"; }\n"
				f"{helper_block}\n"
				"if [ \"${SCOPE_MODE}\" = \"auto\" ]; then\n"
				"\tREVIEWER_SCOPED_CONTEXT_ACTIVE=false\n"
				"\tif [ \"${USE_PREPARE}\" = \"1\" ]; then\n"
				"\t\tif prepare_reviewer_scoped_context; then\n"
				"\t\t\tREVIEWER_SCOPED_CONTEXT_ACTIVE=true\n"
				"\t\tfi\n"
				"\telif build_reviewer_iteration_scope_artifacts \"${LAST_RUN_CHANGED_FILES_FILE}\" \"${LEDGER_STATUS_FILE}\" \"${REVIEWER_SCOPE_PATHS_FILE}\" \"${REVIEWER_SCOPE_SUMMARY_FILE}\"; then\n"
				"\t\tREVIEWER_SCOPED_CONTEXT_ACTIVE=true\n"
				"\t\tcp \"${SCOPE_CONTEXT_SOURCE_FILE}\" \"${REVIEWER_SCOPED_FILES_CONTEXT_FILE}\"\n"
				"\tfi\n"
				"else\n"
				"\tREVIEWER_SCOPED_CONTEXT_ACTIVE=false\n"
				"\twrite_reviewer_scope_summary \"full-diff\" \"first iteration — keep full PR context\"\n"
				"fi\n"
				"emit_reviewer_prompt_context_sections > \"${OUTPUT_CONTEXT_FILE}\"\n"
				"build_reviewer_semble_query\n"
				"printf '%s\\n' \"${REVIEWER_SCOPED_CONTEXT_ACTIVE}\" > \"${SCOPED_ACTIVE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"context_sections": files["context_sections"].read_text(encoding="utf-8"),
			"scope_summary": files["scope_summary"].read_text(encoding="utf-8"),
			"scope_paths": files["scope_paths"].read_text(encoding="utf-8"),
			"scope_context": files["scope_context"].read_text(encoding="utf-8"),
			"semble_query": files["semble_query"].read_text(encoding="utf-8"),
			"scoped_active": files["scoped_active"].read_text(encoding="utf-8").strip(),
		}


def _run_prepare_reviewer_scope_harness(*, last_run_changed_text: str, ledger_text: str, missing_targeted_script: bool = False, workspace_files: dict[str, str] | None = None) -> dict[str, str]:
	helper_block = _reviewer_iteration_scope_helper_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-iteration-scope-prepare-") as td:
		tmp = Path(td)
		workspace = tmp / "workspace"
		workspace.mkdir()
		files = {
			"last_run_changed": tmp / "last_run_changed_files.txt",
			"ledger": tmp / "ledger_status.txt",
			"scope_paths": tmp / "reviewer_scope_paths.txt",
			"scope_summary": tmp / "reviewer_scope_summary.txt",
			"scope_context": tmp / "reviewer_scoped_files_context.txt",
			"scope_query_seed": tmp / "reviewer_scope_query_seed.txt",
			"scope_context_source": tmp / "scope_context_source.txt",
			"semble_query": tmp / "reviewer_semble_query.txt",
			"context_sections": tmp / "context_sections.txt",
			"scoped_active": tmp / "scoped_active.txt",
			"symbol_diff": tmp / "symbol_diff_summary.txt",
			"original_pr_diff": tmp / "original_pr_diff.patch",
			"last_run_diff": tmp / "last_run_diff.patch",
			"pr_changed": tmp / "pr_changed_files.txt",
			"last_run_diff_stat": tmp / "last_run_diff_stat.txt",
			"last_commit_stat": tmp / "last_commit_stat.txt",
			"comments": tmp / "comments.txt",
			"checks": tmp / "checks.txt",
			"pr_diff": tmp / "pr_diff.patch",
		}
		files["last_run_changed"].write_text(last_run_changed_text, encoding="utf-8")
		files["ledger"].write_text(ledger_text, encoding="utf-8")
		files["scope_paths"].write_text("", encoding="utf-8")
		files["scope_summary"].write_text("", encoding="utf-8")
		files["scope_context"].write_text("", encoding="utf-8")
		files["scope_query_seed"].write_text("", encoding="utf-8")
		files["scope_context_source"].write_text("unused sentinel\n", encoding="utf-8")
		files["symbol_diff"].write_text("symbol diff sentinel\n", encoding="utf-8")
		files["original_pr_diff"].write_text("original pr diff sentinel\n", encoding="utf-8")
		files["last_run_diff"].write_text("last run diff sentinel\n", encoding="utf-8")
		files["pr_changed"].write_text("scripts/review_run_reviewers.sh\nextra/pr_scope.py\n", encoding="utf-8")
		files["last_run_diff_stat"].write_text("1 file changed\n", encoding="utf-8")
		files["last_commit_stat"].write_text("commit stat sentinel\n", encoding="utf-8")
		files["comments"].write_text("comments sentinel\n", encoding="utf-8")
		files["checks"].write_text("checks sentinel\n", encoding="utf-8")
		files["pr_diff"].write_text("full pr diff sentinel\n", encoding="utf-8")

		for rel_path, text in (workspace_files or {
			"scripts/review_run_reviewers.sh": "scoped shell target\n",
			"tests/test_review_autofix_review_pipeline_contract.py": "scoped test target\n",
		}).items():
			target = workspace / rel_path
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_text(text, encoding="utf-8")

		targeted_script = tmp / "missing_targeted_file_context.py" if missing_targeted_script else REPO_ROOT / "scripts" / "targeted_file_context.py"

		env = os.environ.copy()
		env.update({
			"SUPPORT_ROOT_DIR": str(REPO_ROOT),
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"LAST_RUN_CHANGED_FILES_FILE": str(files["last_run_changed"]),
			"LEDGER_STATUS_FILE": str(files["ledger"]),
			"REVIEWER_SCOPE_PATHS_FILE": str(files["scope_paths"]),
			"REVIEWER_SCOPE_SUMMARY_FILE": str(files["scope_summary"]),
			"REVIEWER_SCOPED_FILES_CONTEXT_FILE": str(files["scope_context"]),
			"REVIEWER_SCOPE_QUERY_SEED_FILE": str(files["scope_query_seed"]),
			"SCOPE_CONTEXT_SOURCE_FILE": str(files["scope_context_source"]),
			"REVIEWER_SEMBLE_QUERY_FILE": str(files["semble_query"]),
			"OUTPUT_CONTEXT_FILE": str(files["context_sections"]),
			"SCOPED_ACTIVE_FILE": str(files["scoped_active"]),
			"SYMBOL_DIFF_SUMMARY_FILE": str(files["symbol_diff"]),
			"ORIGINAL_PR_DIFF_FILE": str(files["original_pr_diff"]),
			"LAST_RUN_DIFF_FILE": str(files["last_run_diff"]),
			"PR_CHANGED_FILES_FILE": str(files["pr_changed"]),
			"LAST_RUN_DIFF_STAT_FILE": str(files["last_run_diff_stat"]),
			"LAST_COMMIT_STAT_FILE": str(files["last_commit_stat"]),
			"PR_ALL_COMMENTS_CONTEXT_FILE": str(files["comments"]),
			"PR_CHECK_RUNS_CONTEXT_FILE": str(files["checks"]),
			"PR_DIFF_FILE": str(files["pr_diff"]),
			"TARGETED_FILE_CONTEXT_SCRIPT": str(targeted_script),
			"TARGETED_FILE_CONTEXT_MAX_BYTES": "8192",
			"GITHUB_WORKSPACE": str(workspace),
			"SEMBLE_INDEX_AVAILABLE": "false",
			"SCOPE_MODE": "auto",
			"USE_PREPARE": "1",
		})
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				"_embed_input_file() { local _p=\"${1:-}\"; if [ -z \"${_p}\" ] || [ ! -e \"${_p}\" ]; then printf '(missing)\\n'; return 0; fi; if [ ! -s \"${_p}\" ]; then printf '(empty)\\n'; return 0; fi; cat \"${_p}\"; }\n"
				f"{helper_block}\n"
				"REVIEWER_SCOPED_CONTEXT_ACTIVE=false\n"
				"if prepare_reviewer_scoped_context; then\n"
				"\tREVIEWER_SCOPED_CONTEXT_ACTIVE=true\n"
				"fi\n"
				"emit_reviewer_prompt_context_sections > \"${OUTPUT_CONTEXT_FILE}\"\n"
				"build_reviewer_semble_query\n"
				"printf '%s\\n' \"${REVIEWER_SCOPED_CONTEXT_ACTIVE}\" > \"${SCOPED_ACTIVE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"context_sections": files["context_sections"].read_text(encoding="utf-8"),
			"scope_summary": files["scope_summary"].read_text(encoding="utf-8"),
			"scope_paths": files["scope_paths"].read_text(encoding="utf-8"),
			"scope_context": files["scope_context"].read_text(encoding="utf-8"),
			"semble_query": files["semble_query"].read_text(encoding="utf-8"),
			"scoped_active": files["scoped_active"].read_text(encoding="utf-8").strip(),
		}


def test_review_pipeline_knobs_are_wired_into_codex_agent_env() -> None:
	workflow = _workflow_text()
	for expected in (
		"REVIEW_FLOOR_RULES_ENABLED: ${{ vars.REVIEW_FLOOR_RULES_ENABLED || '1' }}",
		"REVIEW_FLOOR_KEYWORDS_FILE: ${{ vars.REVIEW_FLOOR_KEYWORDS_FILE || '' }}",
		"REVIEW_CONSOLIDATOR_ENABLED: ${{ vars.REVIEW_CONSOLIDATOR_ENABLED || '1' }}",
		"REVIEW_CONSOLIDATOR_MODEL: ${{ vars.REVIEW_CONSOLIDATOR_MODEL || 'openai/gpt-5.4' }}",
		"REVIEW_CONSOLIDATOR_REASONING: ${{ vars.REVIEW_CONSOLIDATOR_REASONING || 'xhigh' }}",
		"REVIEW_CONSOLIDATOR_TIMEOUT_SECS: ${{ vars.REVIEW_CONSOLIDATOR_TIMEOUT_SECS || '300' }}",
		"REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT: ${{ vars.REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT || '16000' }}",
		"REVIEW_PARSER_FAILOPEN: ${{ vars.REVIEW_PARSER_FAILOPEN || '1' }}",
		"CONSOLIDATOR_REJECT_SCHEMA_ENABLED: ${{ vars.CONSOLIDATOR_REJECT_SCHEMA_ENABLED || 'false' }}",
		"REVIEW_LEDGER_ENABLED: ${{ vars.REVIEW_LEDGER_ENABLED || '1' }}",
		"REVIEW_LEDGER_PERSIST_LIMIT: ${{ vars.REVIEW_LEDGER_PERSIST_LIMIT || '2' }}",
		"REVIEW_LEDGER_PATH: ${{ vars.REVIEW_LEDGER_PATH || format('.ai/review_issue_ledger/pr-{0}.txt', inputs.pr_number || github.event.inputs.pr_number || github.event.pull_request.number || '0') }}",
		"REVIEW_REVIEWER_CHECKLIST_ENABLED: ${{ vars.REVIEW_REVIEWER_CHECKLIST_ENABLED || '1' }}",
		"REVIEW_REVIEWER_ITERATION_SCOPING: ${{ vars.REVIEW_REVIEWER_ITERATION_SCOPING || '1' }}",
	):
		assert expected in workflow, f"Missing codex-agent env wiring: {expected}"


def test_reject_verifier_bootstrap_and_stage_order_contract() -> None:
	workflow = _workflow_text()
	apply_fixes = _apply_fixes_text()
	assert "review_apply_fixes.sh review_reject_verify.sh review_rb_judge.sh" in workflow
	parse_idx = apply_fixes.index('if parse_script="$(resolve_support_script review_parse_consolidator.sh)"; then')
	verify_idx = apply_fixes.index('if verify_script="$(resolve_support_script review_reject_verify.sh)"; then')
	ledger_idx = apply_fixes.index('if ledger_script="$(resolve_support_script review_issue_ledger.sh)"; then')
	assert parse_idx < verify_idx < ledger_idx
	assert 'CONSOLIDATOR_REJECT_SCHEMA_ENABLED="${CONSOLIDATOR_REJECT_SCHEMA_ENABLED:-false}"' in apply_fixes


def test_support_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets() -> None:
	workflow = _workflow_text()
	assert "validation_history.v1.json" in workflow
	assert "operator_bypass_audit.v1.json" in workflow
	assert "revalidate_events.v1.json" in workflow


def test_review_pipeline_summary_step_is_local_only_and_grep_friendly() -> None:
	block = _step_block("Append review pipeline iteration summary")
	assert "### Review Pipeline — Iteration ${iteration_label}" in block
	assert "reviewer_scope_label=\"full-diff\"" in block, (
		"Summary step must not overclaim scoped reviewer behaviour before "
		"review_run_reviewers.sh consumes REVIEW_REVIEWER_ITERATION_SCOPING"
	)
	for expected in (
		"| Reviewers run | ${reviewers_run} |",
		"| Reviewer scope | ${reviewer_scope_label} |",
		"| Raw bundle size (bytes) | ${bundle_bytes} |",
		"| Floor tags | ${floor_tag_count} |",
		"| Consolidator model | ${REVIEW_CONSOLIDATOR_MODEL:-openai/gpt-5.4} |",
		"| Consolidator invoked | ${consolidator_invoked} |",
		"| Consolidator output bytes | ${consolidator_output_bytes} |",
		"| Parsed issue blocks | ${parsed_blocks} |",
		"| Passthrough blocks | ${passthrough_blocks} |",
		"| Line-unverified blocks | ${line_unverified} |",
		"| Ledger entries total | ${ledger_total} |",
		"| NEW | ${ledger_new} |",
		"| PERSISTING | ${ledger_persisting} |",
		"| FIXED | ${ledger_fixed} |",
		"| RESURGENT | ${ledger_resurgent} |",
		"| accepted-residual | ${ledger_accepted_residual} |",
		"| Editor invoked | ${editor_invoked} |",
		"| CONSOLIDATOR_OVERRIDDEN count | ${override_count} |",
		"| Editor commit produced | ${editor_commit_produced} |",
	):
		assert expected in block, f"Missing summary row contract: {expected}"
	for artifact in (
		"${RUNTIME_DIR}/reviewer_bundle.txt",
		"${RUNTIME_DIR}/floor_tags.txt",
		"${RUNTIME_DIR}/consolidator_raw.txt",
		"${RUNTIME_DIR}/parser_stats.txt",
		"${RUNTIME_DIR}/ledger_status.txt",
		"grep -c 'CONSOLIDATOR_OVERRIDDEN:' \"${EDITOR_SUMMARY_FILE}\"",
		"EDITOR_COMMIT_PRODUCED: ${{ steps.commit_changes.outputs.did_commit }}",
	):
		assert artifact in block, f"Summary step is missing local metric source: {artifact}"
	assert "gh api" not in block
	assert "gh_retry" not in block
	assert "curl https://api.github.com" not in block


def test_auto_merge_guard_honours_configured_orchestrator_branch_pattern() -> None:
	block = _step_block("Enable auto-merge on PR")
	assert "ORCH_INTEGRATION_BRANCH_PATTERN: ${{ vars.ORCH_INTEGRATION_BRANCH_PATTERN || '^orchestrator/project-' }}" in block
	assert 'grep -Eq -- "${ORCH_INTEGRATION_BRANCH_PATTERN}"' in block
	assert 'if [ -z "${_orch_pr_head_ref}" ]; then' in block
	assert "empty/null .head.ref" in block
	assert "refs:?[[:space:]]*#[0-9]+" in block
	assert "(closes|fixes|resolves):?[[:space:]]*#[0-9]+" in block
	assert "matches ORCH_INTEGRATION_BRANCH_PATTERN='${ORCH_INTEGRATION_BRANCH_PATTERN}'" in block
	assert "falling back to canonical '^orchestrator/project-([0-9]+)$' auto-merge suppressor" in block
	assert "falling back to canonical '^orchestrator/project-[0-9]+$' auto-merge suppressor" not in block


def test_auto_merge_guard_suppresses_forward_merge_fallback_pr_on_codex_agent_path() -> None:
	# forward-merge-stable-to-main.yml opens fallback PRs with head ref
	# `auto/forward-merge-stable-<run-id>-<attempt>`. These MUST be merged
	# via "Create a merge commit" so stable's tip stays in main's ancestry —
	# the workflow's own auto-merge call `gh pr merge --squash --auto` would
	# strip that ancestry and break the next promote-main-to-stable.yml run.
	# Verify the codex-agent "Enable auto-merge on PR" step short-circuits
	# on this head-ref pattern BEFORE reaching the squash-auto call.
	block = _step_block("Enable auto-merge on PR")
	assert "Scoped opt-out for forward-merge fallback PRs" in block, (
		"Forward-merge fallback suppressor comment is missing"
	)
	assert "grep -Eq '^auto/forward-merge-stable-'" in block, (
		"Forward-merge fallback head-ref regex is missing or has drifted"
	)
	assert "matches forward-merge fallback pattern '^auto/forward-merge-stable-'" in block, (
		"Forward-merge suppressor log line is missing the canonical phrasing"
	)
	assert "promote-main-to-stable.yml" in block, (
		"Suppressor must explain WHY (ancestry / promote-main-to-stable) for operator debuggability"
	)
	# The forward-merge suppressor must run BEFORE the orchestrator pattern
	# block — otherwise a forward-merge head ref that someone retrofitted
	# to also look orchestrator-shaped (or any future suppressor that
	# moves on) would be evaluated in the wrong order. Concretely: the
	# suppressor must appear above the first reference to the configured
	# ORCH_INTEGRATION_BRANCH_PATTERN match attempt.
	idx_forward = block.find("matches forward-merge fallback pattern")
	idx_orch_match = block.find('grep -Eq -- "${ORCH_INTEGRATION_BRANCH_PATTERN}"')
	assert idx_forward != -1
	assert idx_orch_match != -1
	assert idx_forward < idx_orch_match, (
		"Forward-merge suppressor must short-circuit before the orchestrator-pattern match attempt"
	)


def test_auto_merge_guard_suppresses_forward_merge_fallback_pr_on_deterministic_skip_path() -> None:
	# Defense in depth: a small/doc-only forward-merge fallback PR would
	# otherwise short-circuit through deterministic-skip-merge's auto-merge
	# call before the codex-agent path's suppressor ran. The
	# deterministic-skip job must apply the same head-ref guard, sourced
	# from the gate job's existing /pulls/{n} fetch (§15 API hygiene — no
	# duplicate API call).
	block = _step_block("Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge")
	assert "PR_HEAD_REF" in block, (
		"deterministic-skip-merge must read the gate's head_ref output"
	)
	assert "grep -Eq '^auto/forward-merge-stable-'" in block, (
		"Forward-merge fallback head-ref regex is missing from deterministic-skip-merge"
	)
	assert "auto-merge suppressed on the deterministic-skip path" in block, (
		"Deterministic-skip suppressor log line is missing the canonical phrasing"
	)
	# The check must run BEFORE the `gh pr merge --squash --auto` call.
	idx_guard = block.find("grep -Eq '^auto/forward-merge-stable-'")
	idx_merge = block.find("gh pr merge")
	assert idx_guard != -1
	assert idx_merge != -1
	assert idx_guard < idx_merge, (
		"Forward-merge suppressor must short-circuit before the gh pr merge --squash --auto call"
	)
	assert 'auto_merge_summary="SUPPRESSED (forward-merge fallback head ref' in block, (
		"deterministic-skip-merge must track suppressed auto-merge state for the step summary"
	)
	assert 'echo "- **Auto-merge:** ${auto_merge_summary}"' in block, (
		"Deterministic-skip summary must report the actual auto-merge outcome"
	)


def test_gate_emits_head_ref_output_for_forward_merge_suppressor_reuse() -> None:
	# The deterministic-skip-merge suppressor sources head ref from the
	# gate's /pulls/{n} fetch (§15: don't repeat an API call). Verify the
	# gate exposes head_ref as an output and the deterministic-skip-merge
	# job reads it via needs.gate.outputs.head_ref.
	wf = _workflow_text()
	assert "head_ref: ${{ steps.evaluate.outputs.head_ref }}" in wf, (
		"Gate job must expose head_ref output for downstream forward-merge suppressors"
	)
	assert 'echo "head_ref=${pr_head_ref}"' in wf, (
		"Gate evaluate step must emit head_ref to GITHUB_OUTPUT"
	)
	assert "PR_HEAD_REF: ${{ needs.gate.outputs.head_ref }}" in wf, (
		"deterministic-skip-merge must consume head_ref from gate outputs"
	)


def test_reviewer_prompt_output_rules_still_forbid_scripts() -> None:
	reviewers = _reviewers_text()
	assert "OUTPUT RULES" in reviewers
	assert "No scripts" in reviewers


def test_reviewer_iteration_scope_first_iteration_keeps_full_diff_context() -> None:
	result = _run_reviewer_scope_harness(
		scope_mode="full",
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text="",
	)

	assert result["scoped_active"] == "false"
	assert "full change set of the pull request" in result["context_sections"]
	assert "full PR patch; secondary context" in result["context_sections"]
	assert "scoped reviewer focus derived from latest autofix changes" not in result["context_sections"]
	assert "PR changed files:" in result["semble_query"]
	assert "Scoped reviewer focus files:" not in result["semble_query"]


def test_reviewer_iteration_scope_valid_artifacts_narrow_to_last_run_and_actionable_ledger_files() -> None:
	ledger_text = "\n".join([
		"issue-1\tPERSISTING\t1\tscripts/review_run_reviewers.sh:10\tCORRECTNESS & LOGIC\t[]",
		"issue-2\tNEW\t0\ttests/test_review_autofix_review_pipeline_contract.py:20\tCORRECTNESS & LOGIC\t[]",
		"issue-3\tFIXED\t0\tignored/fixed.py:30\tCORRECTNESS & LOGIC\t[]",
		"issue-4\taccepted-residual\t0\tignored/residual.py:40\tCORRECTNESS & LOGIC\t[]",
		"issue-5\tRESURGENT\t0\ttests/test_review_autofix_review_pipeline_contract.py:22\tCORRECTNESS & LOGIC\t[]",
	]) + "\n"
	result = _run_reviewer_scope_harness(
		scope_mode="auto",
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text=ledger_text,
	)

	assert result["scoped_active"] == "true"
	assert result["scope_paths"].splitlines() == [
		"scripts/review_run_reviewers.sh",
		"tests/test_review_autofix_review_pipeline_contract.py",
	]
	assert "ignored/fixed.py" not in result["scope_paths"]
	assert "ignored/residual.py" not in result["scope_paths"]
	assert "Reviewer iteration scoping mode: scoped" in result["scope_summary"]
	assert "Actionable statuses: NEW, PERSISTING, RESURGENT" in result["scope_summary"]
	assert "ledger:PERSISTING" in result["scope_summary"]
	assert "ledger:NEW, ledger:RESURGENT" in result["scope_summary"]
	assert "scoped reviewer focus derived from latest autofix changes + still-actionable ledger rows" in result["context_sections"]
	assert "current contents of the scoped reviewer focus files" in result["context_sections"]
	assert "full change set of the pull request" not in result["context_sections"]
	assert "full PR patch; secondary context" not in result["context_sections"]
	assert "Scoped reviewer focus summary:" in result["semble_query"]
	assert "Scoped reviewer focus files:" in result["semble_query"]
	assert "PR changed files:" not in result["semble_query"]


def test_reviewer_iteration_scope_fails_open_on_bad_scope_artifacts() -> None:
	result = _run_reviewer_scope_harness(
		scope_mode="auto",
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text="",
	)

	assert result["scoped_active"] == "false"
	assert "Reviewer iteration scoping mode: full-diff" in result["scope_summary"]
	assert "Reason: empty LEDGER_STATUS_FILE" in result["scope_summary"]
	assert result["scope_paths"] == ""
	assert "full change set of the pull request" in result["context_sections"]
	assert "full PR patch; secondary context" in result["context_sections"]
	assert "scoped reviewer focus derived from latest autofix changes" not in result["context_sections"]
	assert "PR changed files:" in result["semble_query"]
	assert "Scoped reviewer focus files:" not in result["semble_query"]


def test_reviewer_iteration_scope_uses_targeted_context_helper_and_scoped_semble_labels() -> None:
	reviewers = _reviewers_text()
	assert 'TARGETED_FILE_CONTEXT_SCRIPT="${TARGETED_FILE_CONTEXT_SCRIPT:-${SUPPORT_SCRIPTS_DIR:-scripts}/targeted_file_context.py}"' in reviewers
	assert 'python3 "${TARGETED_FILE_CONTEXT_SCRIPT}"' in reviewers
	assert '--paths-file "${REVIEWER_SCOPE_PATHS_FILE}"' in reviewers
	assert 'Scoped reviewer focus summary:' in reviewers
	assert 'Scoped reviewer focus files:' in reviewers
	assert 'SCOPED REVIEWER FOCUS SUMMARY / FILE LIST / TARGETED FILE CONTEXT' in reviewers


def test_reviewer_iteration_scope_prepare_path_accepts_root_level_actionable_files() -> None:
	ledger_text = "\n".join([
		"issue-1\tNEW\t0\tLICENSE:3\tCORRECTNESS & LOGIC\t[]",
		"issue-2\tPERSISTING\t1\tgo.mod:2\tCORRECTNESS & LOGIC\t[]",
		"issue-3\tRESURGENT\t0\t.gitignore:1\tCORRECTNESS & LOGIC\t[]",
	]) + "\n"
	result = _run_prepare_reviewer_scope_harness(
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text=ledger_text,
		workspace_files={
			"scripts/review_run_reviewers.sh": "scoped shell target\n",
			"LICENSE": "test license\n",
			"go.mod": "module example.com/test\n",
			".gitignore": "__pycache__/\n",
		},
	)

	assert result["scoped_active"] == "true"
	assert result["scope_paths"].splitlines() == [
		"scripts/review_run_reviewers.sh",
		"LICENSE",
		"go.mod",
		".gitignore",
	]
	assert "- LICENSE [ledger:NEW]" in result["scope_summary"]
	assert "- go.mod [ledger:PERSISTING]" in result["scope_summary"]
	assert "- .gitignore [ledger:RESURGENT]" in result["scope_summary"]
	assert "=== TARGETED FILE CONTEXT ===" in result["scope_context"]
	assert "--- FILE: LICENSE" in result["scope_context"]
	assert "--- FILE: go.mod" in result["scope_context"]
	assert "--- FILE: .gitignore" in result["scope_context"]


def test_reviewer_iteration_scope_prepare_path_trims_trailing_parenthesis_from_root_level_actionable_files() -> None:
	ledger_text = "\n".join([
		"issue-1\tNEW\t0\tLICENSE):3\tCORRECTNESS & LOGIC\t[]",
		"issue-2\tPERSISTING\t1\tgo.mod):2\tCORRECTNESS & LOGIC\t[]",
		"issue-3\tRESURGENT\t0\t.gitignore):1\tCORRECTNESS & LOGIC\t[]",
	]) + "\n"
	result = _run_prepare_reviewer_scope_harness(
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text=ledger_text,
		workspace_files={
			"scripts/review_run_reviewers.sh": "scoped shell target\n",
			"LICENSE": "test license\n",
			"go.mod": "module example.com/test\n",
			".gitignore": "__pycache__/\n",
		},
	)

	assert result["scoped_active"] == "true"
	assert result["scope_paths"].splitlines() == [
		"scripts/review_run_reviewers.sh",
		"LICENSE",
		"go.mod",
		".gitignore",
	]
	assert "- LICENSE [ledger:NEW]" in result["scope_summary"]
	assert "- go.mod [ledger:PERSISTING]" in result["scope_summary"]
	assert "- .gitignore [ledger:RESURGENT]" in result["scope_summary"]
	assert "--- FILE: LICENSE" in result["scope_context"]
	assert "--- FILE: go.mod" in result["scope_context"]
	assert "--- FILE: .gitignore" in result["scope_context"]


def test_reviewer_iteration_scope_prepare_path_preserves_literal_root_level_trailing_punctuation() -> None:
	ledger_text = "\n".join([
		"issue-1\tNEW\t0\tREADME.:3\tCORRECTNESS & LOGIC\t[]",
		"issue-2\tPERSISTING\t1\tgo.mod.:2\tCORRECTNESS & LOGIC\t[]",
		"issue-3\tRESURGENT\t0\t.env.:1\tCORRECTNESS & LOGIC\t[]",
	]) + "\n"
	result = _run_prepare_reviewer_scope_harness(
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text=ledger_text,
		workspace_files={
			"scripts/review_run_reviewers.sh": "scoped shell target\n",
			"README.": "literal trailing dot\n",
			"go.mod.": "module example.com/literal\n",
			".env.": "TOKEN=test\n",
		},
	)

	assert result["scoped_active"] == "true"
	assert result["scope_paths"].splitlines() == [
		"scripts/review_run_reviewers.sh",
		"README.",
		"go.mod.",
		".env.",
	]
	assert "- README. [ledger:NEW]" in result["scope_summary"]
	assert "- go.mod. [ledger:PERSISTING]" in result["scope_summary"]
	assert "- .env. [ledger:RESURGENT]" in result["scope_summary"]
	assert "--- FILE: README." in result["scope_context"]
	assert "--- FILE: go.mod." in result["scope_context"]
	assert "--- FILE: .env." in result["scope_context"]


def test_reviewer_iteration_scope_prepare_path_preserves_hidden_directory_prefixes() -> None:
	ledger_text = "issue-1\tNEW\t0\t.github/workflows/review_autofix.yml:3\tCORRECTNESS & LOGIC\t[]\n"
	result = _run_prepare_reviewer_scope_harness(
		last_run_changed_text=".github/workflows/review_autofix.yml\n.config/tool.toml\n",
		ledger_text=ledger_text,
		workspace_files={
			".github/workflows/review_autofix.yml": "name: review\n",
			".config/tool.toml": "enabled = true\n",
		},
	)

	assert result["scoped_active"] == "true"
	assert result["scope_paths"].splitlines() == [
		".github/workflows/review_autofix.yml",
		".config/tool.toml",
	]
	assert "- .github/workflows/review_autofix.yml [last-run-changed, ledger:NEW]" in result["scope_summary"]
	assert "- .config/tool.toml [last-run-changed]" in result["scope_summary"]
	assert "--- FILE: .github/workflows/review_autofix.yml" in result["scope_context"]
	assert "--- FILE: .config/tool.toml" in result["scope_context"]


def test_reviewer_iteration_scope_prepare_path_reports_missing_targeted_context_helper() -> None:
	ledger_text = "issue-1\tNEW\t0\tscripts/review_run_reviewers.sh:3\tCORRECTNESS & LOGIC\t[]\n"
	result = _run_prepare_reviewer_scope_harness(
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text=ledger_text,
		missing_targeted_script=True,
	)

	assert result["scoped_active"] == "false"
	assert "Reviewer iteration scoping mode: full-diff" in result["scope_summary"]
	assert "Reason: missing targeted_file_context.py" in result["scope_summary"]
	assert result["scope_paths"] == ""
	assert "full change set of the pull request" in result["context_sections"]


def main() -> int:
	test_review_pipeline_knobs_are_wired_into_codex_agent_env()
	test_reject_verifier_bootstrap_and_stage_order_contract()
	test_support_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_review_pipeline_summary_step_is_local_only_and_grep_friendly()
	test_auto_merge_guard_honours_configured_orchestrator_branch_pattern()
	test_auto_merge_guard_suppresses_forward_merge_fallback_pr_on_codex_agent_path()
	test_auto_merge_guard_suppresses_forward_merge_fallback_pr_on_deterministic_skip_path()
	test_gate_emits_head_ref_output_for_forward_merge_suppressor_reuse()
	test_reviewer_prompt_output_rules_still_forbid_scripts()
	test_reviewer_iteration_scope_first_iteration_keeps_full_diff_context()
	test_reviewer_iteration_scope_valid_artifacts_narrow_to_last_run_and_actionable_ledger_files()
	test_reviewer_iteration_scope_fails_open_on_bad_scope_artifacts()
	test_reviewer_iteration_scope_uses_targeted_context_helper_and_scoped_semble_labels()
	test_reviewer_iteration_scope_prepare_path_accepts_root_level_actionable_files()
	test_reviewer_iteration_scope_prepare_path_trims_trailing_parenthesis_from_root_level_actionable_files()
	test_reviewer_iteration_scope_prepare_path_preserves_literal_root_level_trailing_punctuation()
	test_reviewer_iteration_scope_prepare_path_preserves_hidden_directory_prefixes()
	test_reviewer_iteration_scope_prepare_path_reports_missing_targeted_context_helper()
	print("OK: review_autofix review-pipeline plumbing contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

#!/usr/bin/env python3
"""Contract tests for review_autofix Semble + Serena wiring."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
STAGE_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
REVIEWERS = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
COMMIT_CHANGES = REPO_ROOT / "scripts" / "review_commit_changes.sh"
CONFLICT_PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"
CONFLICT_RESOLVE = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
CONFLICT_PROMPT = REPO_ROOT / "prompts" / "conflict-resolver.txt"
INTEGRATION_CONFLICT_PROMPT = REPO_ROOT / "prompts" / "integration-sync-conflict-resolver.txt"
INTEGRATION_RETRY_PRELUDE = REPO_ROOT / "prompts" / "integration-sync-conflict-resolver-retry-prelude.txt"
REVIEWER_CHECKLIST_PROMPT = REPO_ROOT / "prompts" / "review-reviewer-checklist.txt"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _stage_helper_text() -> str:
	return _read(STAGE_HELPER)


def _step_block(text: str, step_name: str) -> str:
	marker = f"- name: {step_name}"
	start = text.find(marker)
	assert start != -1, f"Missing workflow step: {step_name}"
	next_step = text.find("\n      - name:", start + len(marker))
	if next_step == -1:
		return text[start:]
	return text[start:next_step]


def _render_reviewer_prompt_with_checklist(*, checklist_enabled: str, prompt_available: bool) -> tuple[str, str]:
	reviewers = _read(REVIEWERS)
	start = reviewers.index('REVIEWER_CHECKLIST_PROMPT_TEMPLATE="${SUPPORT_PROMPTS_DIR:-prompts}/review-reviewer-checklist.txt"')
	end = reviewers.index("# Assemble the default", start)
	block = reviewers[start:end]

	with tempfile.TemporaryDirectory(prefix="reviewer-checklist-") as tmp:
		tmp_p = Path(tmp)
		support_prompts_dir = tmp_p / "prompts"
		support_prompts_dir.mkdir()
		(tmp_p / "pre_assembled_static.txt").write_text("STATIC SENTINEL\n", encoding="utf-8")
		prompt_body_file = tmp_p / "prompt_body.txt"
		prompt_body_file.write_text("PROMPT BODY SENTINEL\n", encoding="utf-8")
		memory_context_file = tmp_p / "memory_context.txt"
		memory_context_file.write_text("MEMORY SENTINEL\n", encoding="utf-8")
		if prompt_available:
			(support_prompts_dir / "review-reviewer-checklist.txt").write_text(
				_read(REVIEWER_CHECKLIST_PROMPT),
				encoding="utf-8",
			)
		assembled_prompt_file = tmp_p / "assembled_prompt.txt"
		env = os.environ.copy()
		env.update({
			"SUPPORT_PROMPTS_DIR": str(support_prompts_dir),
			"SUPPORT_ROOT_DIR": str(tmp_p / "support-root"),
			"REVIEW_REVIEWER_CHECKLIST_ENABLED": checklist_enabled,
			"PROMPT_ARTIFACT_PATH_HINT": "ARTIFACT PATH HINT",
			"PROMPT_RUNTIME_CONTEXT_HINT": "RUNTIME CONTEXT HINT",
			"MEMORY_CONTEXT_FILE": str(memory_context_file),
			"OUTPUT_FILE": str(assembled_prompt_file),
			"PROMPT_BODY_FILE": str(prompt_body_file),
		})
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{block}\n"
				'assemble_reviewer_prompt "${OUTPUT_FILE}" "${PROMPT_BODY_FILE}"\n',
			],
			cwd=str(tmp_p),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)
		return assembled_prompt_file.read_text(encoding="utf-8"), result.stderr


def _normalize_openrouter_usage(log_text: str, *, phase: str, call: str, model: str) -> str:
	reviewers = _read(REVIEWERS)
	start = reviewers.index("normalize_openrouter_usage() {")
	end = reviewers.index("emit_reviewer_substate()", start)
	block = reviewers[start:end]

	with tempfile.TemporaryDirectory(prefix="normalize-openrouter-usage-") as tmp:
		tmp_p = Path(tmp)
		log_file = tmp_p / "reviewer.stderr"
		log_file.write_text(log_text, encoding="utf-8")
		env = os.environ.copy()
		env["SUPPORT_SCRIPTS_DIR"] = str(REPO_ROOT / "scripts")
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{block}\n"
				'normalize_openrouter_usage "$1" "$2" "$3" "$4"\n',
				"bash",
				str(log_file),
				phase,
				call,
				model,
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)
		return result.stdout.strip()


def test_workflow_bootstrap_and_runtime_defaults_wire_semble_and_serena() -> None:
	workflow = _read(WORKFLOW)
	stage_step_block = _step_block(workflow, "Stage workflow support files")
	stage_helper = _stage_helper_text()
	init_block = _step_block(workflow, "Initialize runtime workspace")
	preflight_block = _step_block(workflow, '"Preflight: Verify required files before reviewer invocation"')
	required_bootstrap_line = next(
		line for line in stage_helper.splitlines() if "REQUIRED_BOOTSTRAP_SCRIPTS=" in line
	)

	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'true' }}" in workflow
	assert "SERENA_ENABLED: ${{ vars.SERENA_ENABLED || 'false' }}" in workflow
	assert 'helper=".codex-workflow-src/scripts/stage_workflow_support.sh"' in stage_step_block
	assert 'helper=".codex-workflow-src-main/scripts/stage_workflow_support.sh"' in stage_step_block
	assert 'WORKFLOW_SOURCE_REPO="shubhodeep1/coding-workflows" \\' in stage_step_block
	assert 'bash "${helper}"' in stage_step_block
	assert (
		'MAIN_PRIMARY_BOOTSTRAP_SCRIPTS="verify_integration_fingerprints.py review_conflict_resolve.sh '
		'review_conflict_prepare.sh"'
	) in stage_helper
	# build_semble_wrapper.sh stays in the optional-bootstrap loop once the BM25
	# wrapper was extracted to a shared script (semble 0.1.3 ships no
	# index/query CLI). The Python render-prompt backend also stays optional so
	# shim-adopting branches bootstrap without breaking main-based callers.
	assert "assemble_prompt.sh" in required_bootstrap_line
	assert "render_prompt.py" not in required_bootstrap_line
	assert 'OPTIONAL_BOOTSTRAP_SCRIPTS="install_semble.sh build_semble_wrapper.sh semble_helpers.sh render_prompt.py"' in stage_helper
	assert (
		"REVIEW_PREFLIGHT_REQUIRED_SUPPORT_SCRIPTS: >-\n"
		"    codex_helpers.sh codex_stall_guard.sh watchdog_helpers.sh\n"
		"    review_run_reviewers.sh render_prompt.sh assemble_prompt.sh"
	) in workflow
	assert "REVIEW_PREFLIGHT_SOFT_SUPPORT_SCRIPTS: >-\n    render_prompt.py" in workflow
	assert "for f in ${REVIEW_PREFLIGHT_REQUIRED_SUPPORT_SCRIPTS} ${REVIEW_PREFLIGHT_SOFT_SUPPORT_SCRIPTS}; do" in stage_step_block
	assert 'for f in ${REVIEW_PREFLIGHT_REQUIRED_SUPPORT_SCRIPTS}; do' in preflight_block
	assert 'check_required_file "${SUPPORT_SCRIPTS_DIR}/${f}"' in preflight_block
	assert 'for f in ${REVIEW_PREFLIGHT_SOFT_SUPPORT_SCRIPTS}; do' in preflight_block
	assert 'check_soft_file "${SUPPORT_SCRIPTS_DIR}/${f}"' in preflight_block
	assert "for f in setup_serena.sh serena_stats_emit.py mcp_handshake_probe.py; do" in stage_helper
	assert 'Optional Serena support asset ${f} is unavailable in checked-out support sources; Serena bootstrap remains disabled.' in stage_helper
	assert 'mkdir -p "${SUPPORT_SCRIPTS_DIR}/templates"' in stage_helper
	assert 'install -m 0644 "${serena_template_src}" "${SUPPORT_SCRIPTS_DIR}/templates/serena_project.yml.j2"' in stage_helper
	assert 'Optional Serena template scripts/templates/serena_project.yml.j2 is unavailable in checked-out support sources; Serena bootstrap remains disabled.' in stage_helper
	assert 'echo "REVIEWER_SEMBLE_QUERY_FILE=${RUNTIME_DIR}/reviewer_semble_query.txt"' in init_block
	assert 'echo "EDITOR_SEMBLE_QUERY_FILE=${RUNTIME_DIR}/editor_semble_query.txt"' in init_block
	assert 'echo "CONFLICT_RESOLVER_SEMBLE_QUERY_FILE=${RUNTIME_DIR}/conflict_resolver_semble_query.txt"' in init_block
	assert 'echo "SEMBLE_AVAILABLE=false"' in init_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in init_block
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in init_block
	assert 'echo "SERENA_AVAILABLE=false"' in init_block
	assert 'echo "SERENA_PROJECT_PREEXISTED=false"' in init_block
	assert 'echo "SERENA_PROJECT_BOOTSTRAP_HASH="' in init_block


def test_workflow_adds_gated_setup_install_index_and_editor_only_serena_steps() -> None:
	workflow = _read(WORKFLOW)
	uv_block = _step_block(workflow, "Setup uv for Semble")
	install_block = _step_block(workflow, "Install semble")
	index_block = _step_block(workflow, "Build semble index")
	setup_serena_block = _step_block(workflow, "Setup Serena for editor")
	clear_serena_block = _step_block(workflow, "Clear Serena after editor")
	detect_serena_block = _step_block(workflow, "Detect preexisting Serena project config")

	assert "astral-sh/setup-uv@v7" in uv_block
	assert "if: env.PR_CLOSED != 'true' && (env.SEMBLE_ENABLED == 'true' || env.SERENA_ENABLED == 'true')" in uv_block
	assert "continue-on-error: true" in uv_block
	assert "if: env.PR_CLOSED != 'true' && (env.SEMBLE_ENABLED == 'true' || env.SERENA_ENABLED == 'true')" in install_block
	assert "continue-on-error: true" in install_block
	assert 'if [ "${SEMBLE_ENABLED:-false}" != "true" ]; then' in install_block
	assert 'if ! bash "${SUPPORT_SCRIPTS_DIR}/install_semble.sh"; then' in install_block
	assert 'echo "SEMBLE_BIN=${SEMBLE_BIN_PATH}" >> "$GITHUB_ENV"' in install_block
	assert "Optional Semble installer is unavailable" in install_block
	assert "if: env.PR_CLOSED != 'true' && env.SEMBLE_ENABLED == 'true'" in index_block
	# Inline `semble index . --out ...` was unreachable code on the pinned
	# semble (0.1.3 lacks the CLI). The shared wrapper builder owns the
	# index build now and writes SEMBLE_INDEX_AVAILABLE=true on success.
	assert 'wrapper_script=""' in index_block
	assert 'wrapper_script="${SUPPORT_SCRIPTS_DIR}/build_semble_wrapper.sh"' in index_block
	assert 'wrapper_script="scripts/build_semble_wrapper.sh"' in index_block
	assert 'bash "${wrapper_script}" > "${RUNTIME_DIR}/semble_index.log" 2>&1 || true' in index_block
	assert "build_semble_wrapper: Semble wrapper unavailable:" in index_block
	assert '"${SEMBLE_BIN_PATH}" index . --out "${SEMBLE_INDEX_PATH}"' not in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false" >> "$GITHUB_ENV"' in index_block
	assert "if: steps.retrigger_guard.outputs.max_iterations_reached != 'true' && env.PR_CLOSED != 'true' && env.AUTOFIX_STALE_BASE_SKIP != 'true' && env.CLAUDE_BRANCH_REVIEW_MODE != 'true' && env.SERENA_ENABLED == 'true'" in setup_serena_block
	assert 'SERENA_FALLBACK_TARGET="review-autofix-editor" bash "${SUPPORT_SCRIPTS_DIR}/setup_serena.sh"' in setup_serena_block
	assert 'SERENA_FALLBACK target=review-autofix-editor reason=setup-failure' in setup_serena_block
	assert 'echo "SERENA_PROJECT_BOOTSTRAP_HASH=${serena_project_hash}" >> "$GITHUB_ENV"' in setup_serena_block
	assert "if: always() && env.SERENA_ENABLED == 'true' && env.CLAUDE_BRANCH_REVIEW_MODE != 'true'" in clear_serena_block
	assert 'SERENA_ENABLED=false bash "${SUPPORT_SCRIPTS_DIR}/setup_serena.sh"' in clear_serena_block
	assert 'echo "SERENA_AVAILABLE=false" >> "$GITHUB_ENV"' in clear_serena_block
	assert "if: env.PR_CLOSED != 'true'" in detect_serena_block
	assert 'git ls-files --error-unmatch -- .serena/project.yml' in detect_serena_block
	assert '[ -e .serena/project.yml ]' in detect_serena_block
	assert 'echo "SERENA_PROJECT_PREEXISTED=true" >> "$GITHUB_ENV"' in detect_serena_block
	assert 'echo "SERENA_PROJECT_PREEXISTED=false" >> "$GITHUB_ENV"' in detect_serena_block
	assert workflow.find("- name: Run reviewer models") < workflow.find("- name: Setup Serena for editor") < workflow.find("- name: Apply fixes with editor model")
	assert workflow.find("- name: Create Codex config") < workflow.find("- name: Setup Serena for editor")
	assert workflow.find("- name: Detect preexisting Serena project config") < workflow.find("- name: Setup Serena for editor")
	assert workflow.find("- name: Apply fixes with editor model") < workflow.find("- name: Clear Serena after editor") < workflow.find("- name: Prepare merge-conflict resolver prompt and pre-snapshot")


def test_reviewer_prompt_assembles_semble_context_in_dynamic_section_without_serena() -> None:
	workflow = _read(WORKFLOW)
	reviewers = _read(REVIEWERS)
	assemble_start = reviewers.index("assemble_reviewer_prompt()")
	assemble_end = reviewers.index("# Assemble the default", assemble_start)
	assemble_block = reviewers[assemble_start:assemble_end]

	assert 'source "${SUPPORT_SCRIPTS_DIR:-scripts}/semble_helpers.sh"' in reviewers
	assert 'REVIEWER_SEMBLE_QUERY_FILE="${REVIEWER_SEMBLE_QUERY_FILE:-${RUNTIME_DIR}/reviewer_semble_query.txt}"' in reviewers
	assert 'semble_query_block \\\n    "$(cat "${REVIEWER_SEMBLE_QUERY_FILE}")"' in reviewers
	assert 'cat "${prompt_body_file}"' in assemble_block
	assert 'cat "${REVIEWER_SEMBLE_CONTEXT_FILE}"' in assemble_block
	assert 'cat "${extra_context_file}"' in assemble_block
	assert assemble_block.index('cat "${prompt_body_file}"') < assemble_block.index('cat "${REVIEWER_SEMBLE_CONTEXT_FILE}"') < assemble_block.index('cat "${extra_context_file}"')
	assert 'cp -r "${CODEX_HOME}/." "${reviewer_codex_home}/"' in reviewers
	assert 'export CODEX_HOME="${reviewer_codex_home}"' in reviewers
	assert "SERENA_TOOL_HINTS" not in reviewers
	assert "setup_serena.sh" not in reviewers
	assert workflow.find("- name: Run reviewer models") < workflow.find("- name: Setup Serena for editor")


def test_reviewer_checklist_prompt_contract_and_gate() -> None:
	workflow = _read(WORKFLOW)
	checklist = _read(REVIEWER_CHECKLIST_PROMPT)
	reviewers = _read(REVIEWERS)
	stage_helper = _stage_helper_text()
	helper_start = reviewers.index("append_reviewer_checklist_block()")
	helper_end = reviewers.index("# Assemble the base reviewer prompt", helper_start)
	helper_block = reviewers[helper_start:helper_end]
	assemble_start = reviewers.index("assemble_reviewer_prompt()")
	assemble_end = reviewers.index("# Assemble the default", assemble_start)
	assemble_block = reviewers[assemble_start:assemble_end]
	enabled_prompt, enabled_stderr = _render_reviewer_prompt_with_checklist(
		checklist_enabled="1",
		prompt_available=True,
	)
	disabled_prompt, disabled_stderr = _render_reviewer_prompt_with_checklist(
		checklist_enabled="0",
		prompt_available=True,
	)
	missing_prompt, missing_stderr = _render_reviewer_prompt_with_checklist(
		checklist_enabled="1",
		prompt_available=False,
	)

	expected_headings = [
		"SECURITY & INPUT VALIDATION",
		"CORRECTNESS & LOGIC",
		"CONCURRENCY / RACES / IDEMPOTENCY",
		"ERROR PATHS & EDGE CASES",
		"PERFORMANCE & RESOURCE USE",
		"INDEX-CONTRACT / DB RULES",
		"NAMING / BACKWARD COMPATIBILITY",
	]
	last_index = -1
	for heading in expected_headings:
		idx = checklist.index(heading)
		assert idx > last_index
		last_index = idx

	assert checklist.count("WHAT TO FLAG") == 7
	assert checklist.count("WHAT NOT TO FLAG") == 7
	assert "Theoretical exploit chains" in checklist
	assert "Alternative implementations that are merely cleaner" in checklist
	assert "Pure naming preferences" in checklist
	assert "literal word NONE" in checklist
	assert "File:" in checklist
	assert "Line or code reference:" in checklist
	assert "Problem:" in checklist
	assert "Why it fails at runtime:" in checklist
	assert "ISSUE_CONFIDENCE:" in checklist
	assert "REVIEW_REVIEWER_CHECKLIST_ENABLED: ${{ vars.REVIEW_REVIEWER_CHECKLIST_ENABLED || 'false' }}" in workflow
	assert 'if [ ! -f "${SUPPORT_PROMPTS_DIR}/review-reviewer-checklist.txt" ]; then' in stage_helper
	assert 'src=".codex-workflow-src/prompts/review-reviewer-checklist.txt"' in stage_helper
	assert 'src=".codex-workflow-src-main/prompts/review-reviewer-checklist.txt"' in stage_helper
	assert 'install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/review-reviewer-checklist.txt"' in stage_helper
	assert 'review-reviewer-checklist.txt not found in checked-out support sources' in stage_helper
	assert 'REVIEWER_CHECKLIST_PROMPT_TEMPLATE="${SUPPORT_PROMPTS_DIR:-prompts}/review-reviewer-checklist.txt"' in reviewers
	assert 'REVIEWER_CHECKLIST_PROMPT_TEMPLATE="${SUPPORT_ROOT_DIR:-.}/prompts/review-reviewer-checklist.txt"' in reviewers
	assert 'REVIEWER_CHECKLIST_ENABLED=false' in reviewers
	assert '"${REVIEW_REVIEWER_CHECKLIST_ENABLED:-0}"' in reviewers
	assert '1|true|yes|on) REVIEWER_CHECKLIST_ENABLED=true ;;' in reviewers
	assert 'REVIEWER_CHECKLIST_PROMPT_AVAILABLE=false' in reviewers
	assert 'if [ "${REVIEWER_CHECKLIST_ENABLED}" = "true" ] && [ "${REVIEWER_CHECKLIST_PROMPT_AVAILABLE}" != "true" ]; then' in reviewers
	assert 'if [ "${REVIEWER_CHECKLIST_ENABLED}" != "true" ] || [ "${REVIEWER_CHECKLIST_PROMPT_AVAILABLE}" != "true" ]; then' in helper_block
	assert 'cat "${REVIEWER_CHECKLIST_PROMPT_TEMPLATE}"' in helper_block
	assert 'append_reviewer_checklist_block' in assemble_block
	assert assemble_block.index('cat "${extra_context_file}"') < assemble_block.index('append_reviewer_checklist_block')
	assert "COMMON ANTI-RULES" in reviewers
	assert "These anti-rules suppress suggestion / nit-level noise only." in reviewers
	assert "accepted as residual or won't-fix" in reviewers
	assert 'assemble_reviewer_prompt "${PASS1_PROMPT_FILE}" "${REVIEWER_PROMPT_BODY_FILE}"' in reviewers
	assert 'assemble_reviewer_prompt "${PASS2_PROMPT_FILE}" "${REVIEWER_PROMPT_BODY_FILE}" "${CROSS_POLLINATION_FILE}"' in reviewers
	assert enabled_prompt.index("PROMPT BODY SENTINEL") < enabled_prompt.index("REVIEWER CHECKLIST")
	for heading in expected_headings:
		assert heading in enabled_prompt
	assert "WHAT NOT TO FLAG" in enabled_prompt
	assert "REVIEWER CHECKLIST" not in disabled_prompt
	assert disabled_stderr.strip() == ""
	assert "REVIEWER CHECKLIST" not in missing_prompt
	assert "Reviewer checklist prompt unavailable" in missing_stderr


def test_normalize_openrouter_usage_keeps_first_valid_usage_payload() -> None:
	line = _normalize_openrouter_usage(
		'noise before JSON\n'
		'{"response":{"usage":{"prompt_tokens":11,"completion_tokens":7,'
		'"total_tokens":18,"cache_creation_input_tokens":5,'
		'"cache_read_input_tokens":3}},"model":"first-model"}\n'
		'{"usage":{"prompt_tokens":99,"completion_tokens":1,"total_tokens":100,'
		'"cache_creation_input_tokens":0,"cache_read_input_tokens":0},'
		'"model":"second-model"}\n',
		phase="review",
		call="pass1",
		model="fallback-model",
	)

	assert "phase=review call=pass1 model=first-model" in line
	assert "prompt_tokens=11" in line
	assert "completion_tokens=7" in line
	assert "total_tokens=18" in line
	assert "cache_creation_input_tokens=5" in line
	assert "cache_read_input_tokens=3" in line
	assert "second-model" not in line
	assert "prompt_tokens=99" not in line


def test_editor_targeted_file_context_and_prompt_render_path_passes_flags() -> None:
	apply_fixes = _read(APPLY_FIXES)

	assert 'EDITOR_SEMBLE_QUERY_FILE="${EDITOR_SEMBLE_QUERY_FILE:-${RUNTIME_DIR}/editor_semble_query.txt}"' in apply_fixes
	assert '--semble-bin "${SEMBLE_BIN:-}"' in apply_fixes
	assert '--semble-index "${SEMBLE_INDEX_PATH:-}"' in apply_fixes
	assert '--semble-query-from "${EDITOR_SEMBLE_QUERY_FILE}"' in apply_fixes
	assert '--semble-max-chunks "${SEMBLE_TARGETED_CONTEXT_MAX_CHUNKS:-6}"' in apply_fixes
	assert "{{SERENA_TOOL_HINTS}}" in apply_fixes
	assert 'EDITOR_SERENA_TOOL_HINTS=""' in apply_fixes
	assert 'Serena hints:' in apply_fixes
	assert 'SERENA_TOOL_HINTS="${EDITOR_SERENA_TOOL_HINTS}" bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${EDITOR_PROMPT_BODY_FILE}"' in apply_fixes
	assert "serena_runtime_noise_should_be_ignored()" in apply_fixes
	assert "SERENA_PROJECT_PREEXISTED" in apply_fixes
	assert "SERENA_PROJECT_BOOTSTRAP_HASH" in apply_fixes
	assert "pathspec=(-- . ':(exclude).serena' ':(exclude).serena/**')" in apply_fixes


def test_commit_changes_drops_bootstrap_owned_serena_runtime_tree_before_staging() -> None:
	commit_changes = _read(COMMIT_CHANGES)

	assert 'if [ "${SERENA_PROJECT_PREEXISTED:-false}" != "true" ] && [ -n "${SERENA_PROJECT_BOOTSTRAP_HASH:-}" ] && [ -f .serena/project.yml ]; then' in commit_changes
	assert "current_serena_project_hash=\"$(sha256sum .serena/project.yml 2>/dev/null | awk '{print $1}' || true)\"" in commit_changes
	assert 'if [ -n "${current_serena_project_hash}" ] && [ "${current_serena_project_hash}" = "${SERENA_PROJECT_BOOTSTRAP_HASH}" ]; then' in commit_changes
	assert 'if ! git ls-files --error-unmatch -- .serena >/dev/null 2>&1; then' in commit_changes
	assert "rm -rf .serena" in commit_changes
	assert ".serena|.serena/*) continue ;;" in commit_changes
	assert "':!.serena'" in commit_changes
	assert "':!.serena/**'" in commit_changes


def test_conflict_prepare_and_resolve_wire_semble_query_and_prompt_append() -> None:
	prepare = _read(CONFLICT_PREPARE)
	resolve = _read(CONFLICT_RESOLVE)
	conflict_prompt = _read(CONFLICT_PROMPT)
	integration_prompt = _read(INTEGRATION_CONFLICT_PROMPT)
	retry_prelude = _read(INTEGRATION_RETRY_PRELUDE)

	assert 'CONFLICT_RESOLVER_SEMBLE_QUERY_FILE="${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE:-${RUNTIME_DIR}/conflict_resolver_semble_query.txt}"' in prepare
	assert "append_semble_query_section 'Resolver allowlist:' \"${RESOLVER_ALLOWLIST_FILE}\" 3000" in prepare
	assert 'echo "CONFLICT_RESOLVER_SEMBLE_QUERY_FILE=${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE}" >> "$GITHUB_ENV"' in prepare
	assert "{{SERENA_TOOL_HINTS_RESOLVER}}" in conflict_prompt
	assert "{{SERENA_TOOL_HINTS_RESOLVER}}" in integration_prompt
	assert "{{SERENA_TOOL_HINTS_RESOLVER}}" in retry_prelude
	assert 'RESOLVER_SERENA_TOOL_HINTS="$({' in prepare
	assert '[ "${SERENA_AVAILABLE:-false}" = "true" ]' in prepare
	assert 'SERENA_TOOL_HINTS_RESOLVER="${RESOLVER_SERENA_TOOL_HINTS:-}"' in prepare
	assert 'Resolver Serena hints:' in prepare
	assert 'source "${SUPPORT_SCRIPTS_DIR:-scripts}/semble_helpers.sh"' in resolve
	assert 'RESOLVER_SERENA_TOOL_HINTS="$({' in resolve
	assert '[ "${SERENA_AVAILABLE:-false}" = "true" ]' in resolve
	assert 'SERENA_TOOL_HINTS_RESOLVER="${RESOLVER_SERENA_TOOL_HINTS:-}"' in resolve
	assert 'Resolver Serena hints:' in resolve
	assert 'TARGETED_FILE_CONTEXT_SCRIPT="${SUPPORT_SCRIPTS_DIR:-scripts}/targeted_file_context.py"' in resolve
	assert '--semble-query-from "${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE}"' in resolve
	assert 'semble_query_block \\\n    "$(cat "${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE}")"' in resolve
	assert resolve.index('cat "${TARGETED_FILES_CONTEXT_FILE}" >> "${CONFLICT_RESOLVER_PROMPT_FILE}"') < resolve.index('cat "${CONFLICT_RESOLVER_SEMBLE_CONTEXT_FILE}" >> "${CONFLICT_RESOLVER_PROMPT_FILE}"')


def main() -> int:
	test_workflow_bootstrap_and_runtime_defaults_wire_semble_and_serena()
	test_workflow_adds_gated_setup_install_index_and_editor_only_serena_steps()
	test_reviewer_prompt_assembles_semble_context_in_dynamic_section_without_serena()
	test_reviewer_checklist_prompt_contract_and_gate()
	test_normalize_openrouter_usage_keeps_first_valid_usage_payload()
	test_editor_targeted_file_context_and_prompt_render_path_passes_flags()
	test_commit_changes_drops_bootstrap_owned_serena_runtime_tree_before_staging()
	test_conflict_prepare_and_resolve_wire_semble_query_and_prompt_append()
	print("OK: review_autofix Semble contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

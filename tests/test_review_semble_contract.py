#!/usr/bin/env python3
"""Contract tests for review_autofix Semble wiring."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
REVIEWERS = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
CONFLICT_PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"
CONFLICT_RESOLVE = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _step_block(text: str, step_name: str) -> str:
	marker = f"- name: {step_name}"
	start = text.find(marker)
	assert start != -1, f"Missing workflow step: {step_name}"
	next_step = text.find("\n      - name:", start + len(marker))
	if next_step == -1:
		return text[start:]
	return text[start:next_step]


def test_workflow_bootstrap_and_runtime_defaults_wire_semble() -> None:
	workflow = _read(WORKFLOW)
	stage_block = _step_block(workflow, "Stage workflow support files")
	init_block = _step_block(workflow, "Initialize runtime workspace")

	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}" in workflow
	assert 'OPTIONAL_BOOTSTRAP_SCRIPTS="verify_integration_fingerprints.py"' in stage_block
	assert 'OPTIONAL_BOOTSTRAP_SCRIPTS="${OPTIONAL_BOOTSTRAP_SCRIPTS} install_semble.sh semble_helpers.sh"' in stage_block
	assert 'echo "REVIEWER_SEMBLE_QUERY_FILE=${RUNTIME_DIR}/reviewer_semble_query.txt"' in init_block
	assert 'echo "EDITOR_SEMBLE_QUERY_FILE=${RUNTIME_DIR}/editor_semble_query.txt"' in init_block
	assert 'echo "CONFLICT_RESOLVER_SEMBLE_QUERY_FILE=${RUNTIME_DIR}/conflict_resolver_semble_query.txt"' in init_block
	assert 'echo "SEMBLE_AVAILABLE=false"' in init_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in init_block
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in init_block


def test_workflow_adds_gated_setup_install_and_index_steps() -> None:
	workflow = _read(WORKFLOW)
	uv_block = _step_block(workflow, "Setup uv for Semble")
	install_block = _step_block(workflow, "Install semble")
	index_block = _step_block(workflow, "Build semble index")

	assert "astral-sh/setup-uv@v3" in uv_block
	assert "env.SEMBLE_ENABLED == 'true'" in uv_block
	assert 'source "${SUPPORT_SCRIPTS_DIR}/install_semble.sh"' in install_block
	assert 'echo "SEMBLE_BIN=${SEMBLE_BIN_PATH}" >> "$GITHUB_ENV"' in install_block
	assert '"${SEMBLE_BIN_PATH}" index . --out "${SEMBLE_INDEX_PATH}"' in index_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=true" >> "$GITHUB_ENV"' in index_block


def test_reviewer_prompt_assembles_semble_context_in_dynamic_section() -> None:
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


def test_editor_targeted_file_context_passes_semble_flags() -> None:
	apply_fixes = _read(APPLY_FIXES)

	assert 'EDITOR_SEMBLE_QUERY_FILE="${EDITOR_SEMBLE_QUERY_FILE:-${RUNTIME_DIR}/editor_semble_query.txt}"' in apply_fixes
	assert '--semble-bin "${SEMBLE_BIN:-}"' in apply_fixes
	assert '--semble-index "${SEMBLE_INDEX_PATH:-}"' in apply_fixes
	assert '--semble-query-from "${EDITOR_SEMBLE_QUERY_FILE}"' in apply_fixes
	assert '--semble-max-chunks "${SEMBLE_TARGETED_CONTEXT_MAX_CHUNKS:-6}"' in apply_fixes


def test_conflict_prepare_and_resolve_wire_semble_query_and_prompt_append() -> None:
	prepare = _read(CONFLICT_PREPARE)
	resolve = _read(CONFLICT_RESOLVE)

	assert 'CONFLICT_RESOLVER_SEMBLE_QUERY_FILE="${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE:-${RUNTIME_DIR}/conflict_resolver_semble_query.txt}"' in prepare
	assert "append_semble_query_section 'Resolver allowlist:' \"${RESOLVER_ALLOWLIST_FILE}\" 3000" in prepare
	assert 'echo "CONFLICT_RESOLVER_SEMBLE_QUERY_FILE=${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE}" >> "$GITHUB_ENV"' in prepare
	assert 'source "${SUPPORT_SCRIPTS_DIR:-scripts}/semble_helpers.sh"' in resolve
	assert 'TARGETED_FILE_CONTEXT_SCRIPT="${SUPPORT_SCRIPTS_DIR:-scripts}/targeted_file_context.py"' in resolve
	assert '--semble-query-from "${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE}"' in resolve
	assert 'semble_query_block \\\n    "$(cat "${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE}")"' in resolve
	assert resolve.index('cat "${TARGETED_FILES_CONTEXT_FILE}" >> "${CONFLICT_RESOLVER_PROMPT_FILE}"') < resolve.index('cat "${CONFLICT_RESOLVER_SEMBLE_CONTEXT_FILE}" >> "${CONFLICT_RESOLVER_PROMPT_FILE}"')


def main() -> int:
	test_workflow_bootstrap_and_runtime_defaults_wire_semble()
	test_workflow_adds_gated_setup_install_and_index_steps()
	test_reviewer_prompt_assembles_semble_context_in_dynamic_section()
	test_editor_targeted_file_context_passes_semble_flags()
	test_conflict_prepare_and_resolve_wire_semble_query_and_prompt_append()
	print("OK: review_autofix Semble contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

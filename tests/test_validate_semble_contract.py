#!/usr/bin/env python3
"""Contract checks for Semble wiring in the validate workflow."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
SELF_HEAL_SCRIPT = REPO_ROOT / "scripts" / "self_heal_validation.sh"


def _workflow_text() -> str:
	return VALIDATE_WORKFLOW.read_text(encoding="utf-8")


def _validate_process_text() -> str:
	return VALIDATE_PROCESS.read_text(encoding="utf-8")


def _self_heal_text() -> str:
	return SELF_HEAL_SCRIPT.read_text(encoding="utf-8")


def test_validate_workflow_fetches_semble_support_files() -> None:
	wf = _workflow_text()
	required_snippets = [
		'copy_from_ref_or_local "scripts/install_semble.sh" "scripts/install_semble.sh.tmp" "false" "true"',
		'copy_from_ref_or_local "scripts/semble_helpers.sh" "scripts/semble_helpers.sh.tmp" "false" "true"',
		# build_semble_wrapper.sh added once the inline BM25 wrapper was
		# extracted to a shared script (semble 0.1.3 lacks index/query CLI).
		'copy_from_ref_or_local "scripts/build_semble_wrapper.sh" "scripts/build_semble_wrapper.sh.tmp" "false" "true"',
		'_fetched_scripts+=(install_semble.sh)',
		'_fetched_scripts+=(semble_helpers.sh)',
		'_fetched_scripts+=(build_semble_wrapper.sh)',
	]
	for snippet in required_snippets:
		assert snippet in wf, f"validate.yml missing snippet: {snippet}"


def test_validate_workflow_bootstraps_and_exports_semble_state() -> None:
	wf = _workflow_text()
	assert "- name: Determine Semble bootstrap state" in wf
	assert "VALIDATION_USE_SEMBLE: ${{ vars.VALIDATION_USE_SEMBLE || 'true' }}" in wf
	assert 'echo "SEMBLE_AVAILABLE=false"' in wf
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in wf
	assert "- name: setup-uv" in wf
	assert "uses: astral-sh/setup-uv@v7" in wf
	assert "- name: Install semble" in wf
	assert "bash scripts/install_semble.sh" in wf
	assert "- name: Build semble index" in wf
	# Inline BM25 wrapper extracted to scripts/build_semble_wrapper.sh; the
	# in-workflow body now delegates to that script via a one-liner.
	assert "bash scripts/build_semble_wrapper.sh" in wf
	assert 'SEMBLE_INDEX_PATH="${RUNTIME_DIR}/.semble-index" \\' in wf
	assert 'SEMBLE_WRAPPER_DIR="${RUNTIME_DIR}/semble/bin" \\' in wf


def test_shared_wrapper_script_owns_bm25_implementation() -> None:
	# Assertions that previously inspected validate.yml's inline wrapper now
	# inspect the shared script. Keeping them so a regression in either
	# extraction or future renames is caught at test time.
	wrapper = (REPO_ROOT / "scripts" / "build_semble_wrapper.sh").read_text(encoding="utf-8")
	assert "MAX_INDEX_FILES = 5000" in wrapper
	assert "Semble indexing skipped after {MAX_INDEX_FILES} files" in wrapper
	assert 'write_env_kv "SEMBLE_AVAILABLE" "true"' in wrapper
	assert 'write_env_kv "SEMBLE_INDEX_AVAILABLE" "true"' in wrapper
	assert 'write_env_kv "SEMBLE_BIN"' in wrapper
	assert "def _default_index_path() -> Path:" in wrapper
	assert 'Path(os.environ.get("SEMBLE_INDEX_PATH", str(_default_index_path())))' in wrapper
	assert "payload.get('version', 'unknown')" in wrapper
	assert 'print("semble 0.1.3")' not in wrapper


def test_validate_process_includes_discover_and_diagnose_semble_hooks() -> None:
	text = _validate_process_text()
	assert 'if source scripts/semble_helpers.sh; then' in text
	assert 'build_validate_discover_semble_query()' in text
	assert 'build_validate_diagnose_semble_query()' in text
	assert 'append_validate_semble_context()' in text
	assert 'discover_semble_query="$(build_validate_discover_semble_query || true)"' in text
	assert 'append_validate_semble_context "${discover_semble_query}" "${VALIDATE_DISCOVER_SEMBLE_MAX_CHUNKS}" "Validate Discover Context"' in text
	assert 'diagnose_semble_query="$(build_validate_diagnose_semble_query || true)"' in text
	assert 'append_validate_semble_context "${diagnose_semble_query}" "${VALIDATE_DIAGNOSE_SEMBLE_MAX_CHUNKS}" "Validate Diagnose Context"' in text


def test_self_heal_includes_semble_prompt_hook() -> None:
	text = _self_heal_text()
	assert 'if source scripts/semble_helpers.sh; then' in text
	assert 'SELF_HEAL_SEMBLE_MAX_CHUNKS="${SELF_HEAL_SEMBLE_MAX_CHUNKS:-3}"' in text
	assert 'build_self_heal_semble_query()' in text
	assert 'append_self_heal_semble_context()' in text
	assert 'if semble_query_block "${query_text}" "${SELF_HEAL_SEMBLE_MAX_CHUNKS}" "Validate Self-Heal Context"; then' in text
	assert 'self_heal_semble_query="$(build_self_heal_semble_query || true)"' in text
	assert 'append_self_heal_semble_context "${self_heal_semble_query}"' in text


def main() -> int:
	test_validate_workflow_fetches_semble_support_files()
	test_validate_workflow_bootstraps_and_exports_semble_state()
	test_shared_wrapper_script_owns_bm25_implementation()
	test_validate_process_includes_discover_and_diagnose_semble_hooks()
	test_self_heal_includes_semble_prompt_hook()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

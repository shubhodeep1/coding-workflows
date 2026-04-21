#!/usr/bin/env python3
"""Contract checks for update_workflows guardrail behavior."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_WORKFLOWS_WF = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"


def _workflow_text() -> str:
	return UPDATE_WORKFLOWS_WF.read_text(encoding="utf-8")


def test_fail_fast_validation_precedes_any_copy_mutation() -> None:
	wf = _workflow_text()
	assert '[ -n "${UPSTREAM_DIR}" ] || fail_with_reason "ERR_UPSTREAM_DIR_EMPTY"' in wf
	assert 'cp "$upstream_file" "$local_file"' in wf
	assert wf.index('[ -n "${UPSTREAM_DIR}" ] || fail_with_reason "ERR_UPSTREAM_DIR_EMPTY"') < wf.index('cp "$upstream_file" "$local_file"')


def test_guardrail_reason_codes_and_outputs_are_declared() -> None:
	wf = _workflow_text()
	assert 'fail_with_reason() {' in wf
	assert "local detail_sanitized=\"${detail//$'\\r'/ }\"" in wf
	assert "detail_sanitized=\"${detail_sanitized//$'\\n'/ }\"" in wf
	assert 'echo "validation_ok=false" >> "$GITHUB_OUTPUT"' in wf
	assert 'ERR_TEMPLATE_COPY_FAILED' in wf
	assert 'cp "$upstream_file" "$local_file" || fail_with_reason "ERR_TEMPLATE_COPY_FAILED"' in wf
	assert 'echo "validation_ok=true" >> "$GITHUB_OUTPUT"' in wf
	assert wf.index('cp "$upstream_file" "$local_file" || fail_with_reason "ERR_TEMPLATE_COPY_FAILED"') < wf.index('echo "validation_ok=true" >> "$GITHUB_OUTPUT"')
	assert 'echo "failure_reason_code=' in wf
	assert 'echo "failure_reason_detail=' in wf
	assert 'ERR_UPSTREAM_DIR_EMPTY' in wf
	assert 'ERR_UPSTREAM_DIR_MISSING' in wf
	assert 'ERR_UPSTREAM_TEMPLATES_EMPTY' in wf
	assert 'ERR_UPSTREAM_SELF_TEMPLATE_MISSING' in wf
	assert 'ERR_LOCAL_WORKFLOW_DIR_MISSING' in wf
	assert 'ERR_LOCAL_WORKFLOW_DIR_NOT_WRITABLE' in wf
	assert 'ERR_LOCAL_TARGET_NOT_WRITABLE' in wf
	assert 'if [ "$filename" = "$SELF_TEMPLATE" ]; then' in wf
	assert wf.index('if [ "$filename" = "$SELF_TEMPLATE" ]; then') < wf.index('if [ -e "$local_file" ] && [ ! -w "$local_file" ]; then')


def test_failure_summary_contract_is_present() -> None:
	wf = _workflow_text()
	assert '- name: Summary' in wf
	assert 'if: always()' in wf
	assert 'ERR_TEMPLATE_FETCH_FAILED' in wf
	assert 'ERR_UNCATEGORIZED_FAILURE' in wf
	assert 'FAILURE_REASON_FILE="/tmp/update_workflows_failure_reason.txt"' in wf
	assert "printf '%s\\n%s\\n' \"${code}\" \"${detail_sanitized}\" > \"${FAILURE_REASON_FILE}\"" in wf
	assert 'IFS= read -r FAILURE_REASON_CODE < "${FAILURE_REASON_FILE}"' in wf
	assert "FAILURE_REASON_DETAIL=\"$(sed -n '2p' " in wf
	assert '"${FAILURE_REASON_FILE}" 2>/dev/null || true)"' in wf
	assert 'Failure reason code:' in wf
	assert 'Failure reason detail:' in wf
	assert 'Failure reason artifact:' in wf


def test_success_path_contracts_are_preserved() -> None:
	wf = _workflow_text()
	assert "if: ${{ inputs.allow_workflow_edits != false }}" in wf
	assert 'SELF_TEMPLATE="ai-update-workflows.yml"' in wf
	assert 'SKIPPED_FILES="${SKIPPED_FILES}${filename} (self-updater, skipped)\\n"' in wf
	assert 'SKIPPED_LIST=$(cat /tmp/skipped_files.txt)' in wf
	assert "printf '%b' \"$UPDATED_FILES\" > /tmp/updated_files.txt" in wf
	assert "printf '%b' \"$CREATED_FILES\" > /tmp/created_files.txt" in wf
	assert "printf '%b' \"$SKIPPED_FILES\" > /tmp/skipped_files.txt" in wf
	assert '**Skipped files:**' in wf
	assert "if: steps.update.outputs.has_updates == 'true'" in wf
	assert "ALLOW_WORKFLOW_EDITS repository variable to '\\''false'\\''." in wf


def main() -> int:
	test_fail_fast_validation_precedes_any_copy_mutation()
	test_guardrail_reason_codes_and_outputs_are_declared()
	test_failure_summary_contract_is_present()
	test_success_path_contracts_are_preserved()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

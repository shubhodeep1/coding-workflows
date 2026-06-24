#!/usr/bin/env python3
"""Contract checks for update_workflows guardrail behavior."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_WORKFLOWS_WF = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
WORKFLOW_TEMPLATES_DIR = REPO_ROOT / "workflow-templates"
WORKFLOW_PROFILE_DIR = WORKFLOW_TEMPLATES_DIR / "profiles"
README_MD = REPO_ROOT / "README.md"
AGENTS_MD = REPO_ROOT / "agents.md"


def _workflow_text() -> str:
	return UPDATE_WORKFLOWS_WF.read_text(encoding="utf-8")


def _manifest_lines(name: str) -> list[str]:
	return [
		line.strip()
		for line in (WORKFLOW_PROFILE_DIR / name).read_text(encoding="utf-8").splitlines()
		if line.strip()
	]


def _agents_profile_line(profile: str) -> str:
	return (
		f"PROFILE.name={profile} "
		f"manifest=workflow-templates/profiles/{profile}.txt "
		f"wrappers={','.join(_manifest_lines(f'{profile}.txt'))}"
	)


def test_profile_manifests_match_contracts() -> None:
	core = [
		"ai-clarify.yml",
		"ai-plan.yml",
		"ai-implement.yml",
		"ai-review.yml",
		"ai-issue-pr-status.yml",
		"ai-cancel-on-pr-close.yml",
	]
	standard = core + [
		"ai-orchestrate.yml",
		"ai-orchestrate-poll.yml",
		"ai-orchestrate-clarify-respond.yml",
		"ai-validate.yml",
		"ai-sync-labels.yml",
		"review_rb_judge_dispatch.yml",
	]
	full = sorted(path.name for path in WORKFLOW_TEMPLATES_DIR.glob("*.yml"))

	assert _manifest_lines("core.txt") == core
	assert _manifest_lines("standard.txt") == standard
	assert _manifest_lines("full.txt") == full
	assert "ai-update-workflows.yml" in _manifest_lines("full.txt")
	assert all("/" not in entry for entry in _manifest_lines("full.txt"))


def test_install_profile_docs_and_agents_contracts() -> None:
	readme = README_MD.read_text(encoding="utf-8")
	assert "#### Install profiles" in readme
	assert "`WORKFLOW_PROFILE` repository variable" in readme
	assert "[`workflow-templates/profiles/core.txt`](workflow-templates/profiles/core.txt)" in readme
	assert "[`workflow-templates/profiles/standard.txt`](workflow-templates/profiles/standard.txt)" in readme
	assert "[`workflow-templates/profiles/full.txt`](workflow-templates/profiles/full.txt)" in readme
	assert "Profile downgrades are non-destructive:" in readme

	agents = AGENTS_MD.read_text(encoding="utf-8")
	assert "## Workflow install profiles" in agents
	assert "PROFILE.default=full" in agents
	assert _agents_profile_line("core") in agents
	assert _agents_profile_line("standard") in agents
	assert _agents_profile_line("full") in agents


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
	assert 'ERR_PROFILE_MANIFEST_DIR_MISSING' in wf
	assert 'ERR_WORKFLOW_PROFILE_UNKNOWN' in wf
	assert 'ERR_PROFILE_TEMPLATE_MISSING' in wf
	assert 'ERR_WORKFLOW_PROFILE_EMPTY' in wf
	assert 'ERR_LOCAL_TARGET_IS_DIRECTORY' in wf
	assert 'ERR_LOCAL_TARGET_NOT_WRITABLE' in wf
	assert 'if [ "$filename" = "$SELF_TEMPLATE" ]; then' in wf
	directory_guard = 'if [ -e "$local_file" ] && [ -d "$local_file" ]; then'
	writable_guard = 'if [ -e "$local_file" ] && [ ! -w "$local_file" ]; then'
	assert directory_guard in wf
	assert writable_guard in wf
	assert wf.index('if [ "$filename" = "$SELF_TEMPLATE" ]; then') < wf.index(directory_guard)
	assert wf.index(directory_guard) < wf.index(writable_guard)
	assert '- name: Apply canonical audit-gate assets' in wf
	assert 'python3 "${UPSTREAM_DIR}/../scripts/apply_audit_gate_assets.py"' in wf
	assert '--contract-root "${CONTRACT_DIR}"' in wf
	assert '--changed-files-file "${CHANGED_FILES_FILE}"' in wf
	assert "steps.update.outputs.has_updates == 'true' || steps.audit_gate.outputs.status == 'applied'" in wf
	assert 'git add -- "$changed_path"' in wf
	assert 'if git diff --cached --quiet; then' in wf
	assert 'if: steps.update.outputs.has_updates == \'true\' || steps.audit_gate.outputs.status == \'applied\'' in wf
	assert 'Audit-gate assets applied:' in wf
	assert 'SCRIPTS_DIR="scripts"' in wf
	assert 'git sparse-checkout set "${TEMPLATES_DIR}" "${SCRIPTS_DIR}"' in wf


def test_profile_selection_and_non_destructive_downgrade_contracts() -> None:
	wf = _workflow_text()
	assert "SELECTED_PROFILE=\"${{ vars.WORKFLOW_PROFILE != '' && vars.WORKFLOW_PROFILE || 'full' }}\"" in wf
	assert 'PROFILE_MANIFEST_DIR="${UPSTREAM_DIR}/profiles"' in wf
	assert 'PROFILE_MANIFEST="${PROFILE_MANIFEST_DIR}/${SELECTED_PROFILE}.txt"' in wf
	assert 'manifest_templates=()' in wf
	assert 'manifest_entry="${manifest_entry%$\'\\r\'}"' in wf
	assert 'manifest_entry="${manifest_entry#"${manifest_entry%%[![:space:]]*}"}"' in wf
	assert 'manifest_entry="${manifest_entry%"${manifest_entry##*[![:space:]]}"}"' in wf
	assert 'done < "${PROFILE_MANIFEST}"' in wf
	assert wf.count('for upstream_file in "${manifest_templates[@]}"; do') == 2
	assert 'rm "$local_file"' not in wf
	assert 'rm -f "$local_file"' not in wf
	assert 'git rm' not in wf
	assert 'find "${LOCAL_DIR}"' not in wf


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
	assert '**Audit gate files:**' in wf
	assert '- **Audit gate status:** ${AUDIT_GATE_STATUS:-unknown}' in wf
	assert '- **Audit gate package script action:** ${AUDIT_GATE_SCRIPT_ACTION:-unknown}' in wf
	assert '- **Audit gate changed files:** ${AUDIT_GATE_CHANGED_COUNT:-0}' in wf
	assert "if: steps.update.outputs.has_updates == 'true'" in wf
	assert "ALLOW_WORKFLOW_EDITS repository variable to '\\''false'\\''." in wf


def main() -> int:
	test_profile_manifests_match_contracts()
	test_install_profile_docs_and_agents_contracts()
	test_fail_fast_validation_precedes_any_copy_mutation()
	test_guardrail_reason_codes_and_outputs_are_declared()
	test_profile_selection_and_non_destructive_downgrade_contracts()
	test_failure_summary_contract_is_present()
	test_success_path_contracts_are_preserved()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

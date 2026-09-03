#!/usr/bin/env python3
"""Contract checks for update_workflows guardrail behavior."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from workflow_wrapper_refs import pin_reusable_workflow_refs, validate_release_sha


UPDATE_WORKFLOWS_WF = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
WORKFLOW_TEMPLATES_DIR = REPO_ROOT / "workflow-templates"
WORKFLOW_PROFILE_DIR = WORKFLOW_TEMPLATES_DIR / "profiles"
README_MD = REPO_ROOT / "README.md"
AGENTS_MD = REPO_ROOT / "agents.md"
SEED_COMMANDS = (
	REPO_ROOT / ".claude" / "commands" / "seed-repo.md",
	REPO_ROOT / "workflow-templates" / ".claude" / "commands" / "seed-repo.md",
)
VALID_RELEASE_SHA = "0123456789abcdef0123456789abcdef01234567"


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
	assert '- name: Prepare immutable workflow wrappers' in wf
	assert 'python3 "${RENDERER}"' in wf
	assert 'cp "$upstream_file" "$local_file"' in wf
	assert wf.index('- name: Prepare immutable workflow wrappers') < wf.index('- name: Apply canonical audit-gate assets')
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
	assert 'if [ "$filename" = "$SELF_TEMPLATE" ] && [ ! -e "$local_file" ]; then' in wf
	directory_guard = 'if [ -e "$local_file" ] && [ -d "$local_file" ]; then'
	writable_guard = 'if [ -e "$local_file" ] && [ ! -w "$local_file" ]; then'
	assert directory_guard in wf
	assert writable_guard in wf
	assert wf.index('if [ "$filename" = "$SELF_TEMPLATE" ] && [ ! -e "$local_file" ]; then') < wf.index(directory_guard)
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


def test_self_updater_is_refreshed_existing_only_for_every_profile() -> None:
	wf = _workflow_text()
	assert "ai-update-workflows.yml" not in _manifest_lines("core.txt")
	assert "ai-update-workflows.yml" not in _manifest_lines("standard.txt")
	assert "ai-update-workflows.yml" in _manifest_lines("full.txt")
	assert (
		'if [ "${self_template_selected}" != "true" ] && '
		'[ -e "${LOCAL_DIR}/${SELF_TEMPLATE}" ]; then'
	) in wf
	assert 'manifest_templates+=( "${RENDERED_DIR}/${SELF_TEMPLATE}" )' in wf
	assert 'if [ "$filename" = "$SELF_TEMPLATE" ] && [ ! -e "$local_file" ]; then' in wf
	assert wf.index('SKIPPED_FILES="${SKIPPED_FILES}${filename} (self-updater absent, skipped)\\n"') < wf.index(
		'cp "$upstream_file" "$local_file" || fail_with_reason "ERR_TEMPLATE_COPY_FAILED"'
	)


def test_release_payload_is_validated_but_current_stable_wins() -> None:
	wf = _workflow_text()
	assert 'DISPATCH_SHA: ${{ github.event.client_payload.sha || \'\' }}' in wf
	assert 'TEMPLATES_REF="refs/tags/stable"' in wf
	assert 'git fetch --force --no-tags --depth 1 origin "${TEMPLATES_REF}"' in wf
	assert 'UPSTREAM_SHA="$(git rev-parse \'FETCH_HEAD^{commit}\')"' in wf
	assert '[[ ! "${DISPATCH_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]' in wf
	assert '::warning::coding-workflows-stable-released payload is missing a valid 40-character sha;' in wf
	assert '::error::coding-workflows-stable-released payload is missing' not in wf
	assert 'elif [ "${DISPATCH_SHA,,}" != "${UPSTREAM_SHA}" ]; then' in wf
	assert "current stable wins" in wf
	assert '--sha "${UPSTREAM_SHA}"' in wf


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
	assert 'SKIPPED_FILES="${SKIPPED_FILES}${filename} (self-updater absent, skipped)\\n"' in wf
	assert '[ -e "${LOCAL_DIR}/${SELF_TEMPLATE}" ]' in wf
	assert 'manifest_templates+=( "${RENDERED_DIR}/${SELF_TEMPLATE}" )' in wf
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


def test_wrapper_ref_renderer_contract() -> None:
	template_text = """jobs:
  first:
    uses: shubhodeep1/coding-workflows/.github/workflows/clarify.yml@stable
  second:
    uses: shubhodeep1/coding-workflows/.github/workflows/plan.yml@stable # old marker
  third:
    uses: actions/checkout@stable
# shubhodeep1/coding-workflows/.github/workflows/comment.yml@stable
"""
	rendered_text = pin_reusable_workflow_refs(template_text, VALID_RELEASE_SHA.upper())
	expected_suffix = f"@{VALID_RELEASE_SHA} # stable"
	assert rendered_text.count(expected_suffix) == 2
	assert "uses: actions/checkout@stable" in rendered_text
	assert "# shubhodeep1/coding-workflows/.github/workflows/comment.yml@stable" in rendered_text
	assert validate_release_sha(VALID_RELEASE_SHA.upper()) == VALID_RELEASE_SHA

	for invalid_sha in ("", "a" * 39, "g" * 40, "a" * 41):
		try:
			validate_release_sha(invalid_sha)
		except ValueError:
			pass
		else:
			raise AssertionError(f"invalid SHA was accepted: {invalid_sha!r}")

	try:
		pin_reusable_workflow_refs("uses: actions/checkout@v5\n", VALID_RELEASE_SHA)
	except ValueError as exc:
		assert "no coding-workflows" in str(exc)
	else:
		raise AssertionError("template without a reusable-workflow ref was accepted")


def test_every_wrapper_template_renders_to_an_immutable_ref() -> None:
	templates = sorted(WORKFLOW_TEMPLATES_DIR.glob("*.yml"))
	assert len(templates) == 16
	for template_path in templates:
		rendered_text = pin_reusable_workflow_refs(
			template_path.read_text(encoding="utf-8"),
			VALID_RELEASE_SHA,
		)
		assert "shubhodeep1/coding-workflows/.github/workflows/" in rendered_text
		assert ".yml@stable" not in rendered_text
		assert f"@{VALID_RELEASE_SHA} # stable" in rendered_text


def test_seed_commands_require_immutable_wrapper_rendering() -> None:
	for command_path in SEED_COMMANDS:
		command_text = command_path.read_text(encoding="utf-8")
		assert "scripts/workflow_wrapper_refs.py" in command_text
		assert "40-character" in command_text
		assert "# stable" in command_text
		assert "git fetch --force --no-tags origin refs/tags/stable" in command_text
		assert "--depth=1" not in command_text
		assert "git rev-parse 'FETCH_HEAD^{commit}'" in command_text
		assert "origin/stable" not in command_text
		assert "ref=<UPSTREAM_SHA>" in command_text
		assert "refreshes an existing copy to the current release pin" in command_text


def main() -> int:
	test_profile_manifests_match_contracts()
	test_install_profile_docs_and_agents_contracts()
	test_fail_fast_validation_precedes_any_copy_mutation()
	test_guardrail_reason_codes_and_outputs_are_declared()
	test_profile_selection_and_non_destructive_downgrade_contracts()
	test_self_updater_is_refreshed_existing_only_for_every_profile()
	test_release_payload_is_validated_but_current_stable_wins()
	test_failure_summary_contract_is_present()
	test_success_path_contracts_are_preserved()
	test_wrapper_ref_renderer_contract()
	test_every_wrapper_template_renders_to_an_immutable_ref()
	test_seed_commands_require_immutable_wrapper_rendering()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

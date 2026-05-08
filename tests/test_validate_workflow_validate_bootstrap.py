#!/usr/bin/env python3
"""Contract checks for template-mode bootstrap wiring in validate workflow."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"


def _workflow_text() -> str:
	return VALIDATE_WORKFLOW.read_text(encoding="utf-8")


def test_validate_workflow_bootstrap_fetches_template_assets() -> None:
	wf = _workflow_text()
	required_snippets = [
		'copy_from_ref_or_local "scripts/render_validation_templates.py" "scripts/render_validation_templates.py.tmp" "false" "true"',
		'copy_from_ref_or_local "scripts/templates/slot_manifest.schema.json" "scripts/templates/slot_manifest.schema.json.tmp" "false" "true"',
		'copy_from_ref_or_local "${template_path}" "${template_path}" "false" "true" || true',
		"workflow-templates/validation-harness/_shared/_lib/tap_helpers.sh.j2",
		"workflow-templates/validation-harness/_shared/tests/00_canary.sh.j2",
		"workflow-templates/validation-harness/_shared/tests/90_tap_report.sh.j2",
		"workflow-templates/validation-harness/node-hardhat-solidity/Dockerfile.app.j2",
		"workflow-templates/validation-harness/node-hardhat-solidity/_lib/graceful_shutdown.sh.j2",
		"workflow-templates/validation-harness/node-hardhat-solidity/docker-compose.test.yml.j2",
		"workflow-templates/validation-harness/node-hardhat-solidity/tests/00_canary.sh.j2",
		"workflow-templates/validation-harness/node-hardhat-solidity/tests/10_family_marker.sh.j2",
		"workflow-templates/validation-harness/node-hardhat-solidity/tests/20_rpc_probe.sh.j2",
		"workflow-templates/validation-harness/node-hardhat-solidity/tests/30_hardhat_test.sh.j2",
		"workflow-templates/validation-harness/node-hardhat-solidity/validate.env.j2",
		"workflow-templates/validation-harness/python-mongo-flask/Dockerfile.app.j2",
		"workflow-templates/validation-harness/python-mongo-flask/docker-compose.test.yml.j2",
		"workflow-templates/validation-harness/python-mongo-flask/tests/00_canary.sh.j2",
		"workflow-templates/validation-harness/python-mongo-flask/tests/10_family_marker.sh.j2",
		"workflow-templates/validation-harness/python-mongo-flask/tests/10_http_smoke.sh.j2",
		"workflow-templates/validation-harness/python-mongo-flask/tests/20_import_audit.sh.j2",
		"workflow-templates/validation-harness/python-mongo-flask/tests/30_graceful_shutdown.sh.j2",
		"workflow-templates/validation-harness/python-mongo-flask/tests/90_tap_report.sh.j2",
		"workflow-templates/validation-harness/python-mongo-flask/tests/_lib/graceful_shutdown.py.j2",
		"workflow-templates/validation-harness/python-mongo-flask/tests/_lib/http_smoke.py.j2",
		"workflow-templates/validation-harness/python-mongo-flask/tests/_lib/import_audit.py.j2",
		"workflow-templates/validation-harness/python-repo-checks/Dockerfile.app.j2",
		"workflow-templates/validation-harness/python-repo-checks/docker-compose.test.yml.j2",
		"workflow-templates/validation-harness/python-repo-checks/tests/00_canary.sh.j2",
		"workflow-templates/validation-harness/python-repo-checks/tests/10_family_marker.sh.j2",
		"workflow-templates/validation-harness/python-repo-checks/tests/20_import_audit.sh.j2",
		"workflow-templates/validation-harness/python-repo-checks/tests/30_graceful_shutdown.sh.j2",
		"workflow-templates/validation-harness/python-repo-checks/tests/40_repo_checks.sh.j2",
		"workflow-templates/validation-harness/python-repo-checks/tests/90_tap_report.sh.j2",
		"workflow-templates/validation-harness/python-repo-checks/tests/_lib/graceful_shutdown.py.j2",
		"workflow-templates/validation-harness/python-repo-checks/tests/_lib/import_audit.py.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/Dockerfile.app.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/docker-compose.test.yml.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/validate.env.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/tests/00_canary.sh.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/tests/10_family_marker.sh.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/tests/20_import_audit.sh.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/tests/30_graceful_shutdown.sh.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/tests/40_repo_checks.sh.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/tests/90_tap_report.sh.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/tests/_lib/graceful_shutdown.py.j2",
		"workflow-templates/validation-harness/python-mongo-repo-checks/tests/_lib/import_audit.py.j2",
	]
	for snippet in required_snippets:
		assert snippet in wf


def test_validate_workflow_passes_template_default_env() -> None:
	wf = _workflow_text()
	assert "VALIDATION_USE_TEMPLATES: ${{ vars.VALIDATION_USE_TEMPLATES || 'true' }}" in wf
	assert 'python3 -m pip install --disable-pip-version-check --quiet --user pyyaml jsonschema jinja2' in wf


def test_validate_workflow_bootstraps_semble_fail_open() -> None:
	wf = _workflow_text()
	assert 'SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || \'false\' }}' in wf
	assert 'SEMBLE_AVAILABLE: "false"' in wf
	assert 'SEMBLE_INDEX_AVAILABLE: "false"' in wf
	assert 'for f in gh_helpers.sh ai_labels.py render_prompt.sh tg_helpers.sh memory_helpers.sh ai_memory.py ai_memory_lib.py openrouter_prompt_cache.py write_codex_config.sh install_semble.sh semble_helpers.sh; do' in wf
	assert '- name: Setup uv for Semble' in wf
	assert '- name: Install Semble' in wf
	assert '- name: Build Semble index' in wf
	assert 'bash scripts/install_semble.sh' in wf
	assert 'timeout 300s semble index . --out "${SEMBLE_INDEX_DIR}"' in wf
	assert 'SEMBLE_INDEX target=validate path=${SEMBLE_INDEX_DIR}' in wf
	assert 'SEMBLE_FALLBACK target=index reason=workspace_unavailable' in wf


def main() -> int:
	test_validate_workflow_bootstrap_fetches_template_assets()
	test_validate_workflow_passes_template_default_env()
	test_validate_workflow_bootstraps_semble_fail_open()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

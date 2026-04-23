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
	assert 'copy_from_ref_or_local "scripts/render_validation_templates.py" "scripts/render_validation_templates.py.tmp" "false" "true"' in wf
	assert 'copy_from_ref_or_local "scripts/templates/slot_manifest.schema.json" "scripts/templates/slot_manifest.schema.json.tmp" "false" "true"' in wf
	assert 'copy_from_ref_or_local "${template_path}" "${template_path}" "false" "true" || true' in wf
	assert "workflow-templates/validation-harness/_shared/_lib/tap_helpers.sh.j2" in wf
	assert "workflow-templates/validation-harness/_shared/tests/00_canary.sh.j2" in wf
	assert "workflow-templates/validation-harness/_shared/tests/90_tap_report.sh.j2" in wf
	assert "workflow-templates/validation-harness/node-hardhat-solidity/Dockerfile.app.j2" in wf
	assert "workflow-templates/validation-harness/node-hardhat-solidity/_lib/graceful_shutdown.sh.j2" in wf
	assert "workflow-templates/validation-harness/node-hardhat-solidity/docker-compose.test.yml.j2" in wf
	assert "workflow-templates/validation-harness/node-hardhat-solidity/tests/00_canary.sh.j2" in wf
	assert "workflow-templates/validation-harness/node-hardhat-solidity/tests/10_family_marker.sh.j2" in wf
	assert "workflow-templates/validation-harness/node-hardhat-solidity/tests/20_rpc_probe.sh.j2" in wf
	assert "workflow-templates/validation-harness/node-hardhat-solidity/tests/30_hardhat_test.sh.j2" in wf
	assert "workflow-templates/validation-harness/node-hardhat-solidity/validate.env.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/Dockerfile.app.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/docker-compose.test.yml.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/tests/00_canary.sh.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/tests/10_family_marker.sh.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/tests/10_http_smoke.sh.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/tests/20_import_audit.sh.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/tests/30_graceful_shutdown.sh.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/tests/90_tap_report.sh.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/tests/_lib/graceful_shutdown.py.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/tests/_lib/http_smoke.py.j2" in wf
	assert "workflow-templates/validation-harness/python-mongo-flask/tests/_lib/import_audit.py.j2" in wf


def test_validate_workflow_passes_template_opt_in_env() -> None:
	wf = _workflow_text()
	assert "VALIDATION_USE_TEMPLATES: ${{ vars.VALIDATION_USE_TEMPLATES || 'true' }}" in wf
	assert 'python3 -m pip install --disable-pip-version-check --quiet --user pyyaml jsonschema jinja2' in wf


def main() -> int:
	test_validate_workflow_bootstrap_fetches_template_assets()
	test_validate_workflow_passes_template_opt_in_env()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

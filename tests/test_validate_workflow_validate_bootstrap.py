#!/usr/bin/env python3
"""Contract checks for template-mode bootstrap wiring in validate workflow."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CODEX_HEARTBEAT_TEST = REPO_ROOT / "tests" / "test_codex_heartbeat.py"
RUN_VALIDATION_REPO_CHECKS = REPO_ROOT / "scripts" / "run_validation_repo_checks.sh"
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"
STAGE_WORKFLOW_SUPPORT = REPO_ROOT / "scripts" / "stage_workflow_support.sh"


def _workflow_text() -> str:
	return VALIDATE_WORKFLOW.read_text(encoding="utf-8")


def _helper_text() -> str:
	return STAGE_WORKFLOW_SUPPORT.read_text(encoding="utf-8")


def test_validate_workflow_bootstrap_uses_shared_helper_and_lists_template_assets() -> None:
	wf = _workflow_text()
	required_snippets = [
		'helper_path="scripts/stage_workflow_support.sh"',
		'bash "${helper_path}" validate --manifest "${manifest_path}"',
		"scripts/assemble_prompt.sh",
		"scripts/render_prompt.py",
		"scripts/render_validation_templates.py",
		"scripts/templates/slot_manifest.schema.json",
		"scripts/validate_driver.sh",
		"scripts/validate_process.sh",
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
		"workflow-templates/validation-harness/node-runtime/Dockerfile.app.j2",
		"workflow-templates/validation-harness/node-runtime/docker-compose.test.yml.j2",
		"workflow-templates/validation-harness/node-runtime/validate.env.j2",
		"workflow-templates/validation-harness/node-runtime/tests/00_canary.sh.j2",
		"workflow-templates/validation-harness/node-runtime/tests/10_family_marker.sh.j2",
		"workflow-templates/validation-harness/node-runtime/tests/20_import_audit.sh.j2",
		"workflow-templates/validation-harness/node-runtime/tests/30_graceful_shutdown.sh.j2",
		"workflow-templates/validation-harness/node-runtime/tests/40_repo_checks.sh.j2",
		"workflow-templates/validation-harness/node-runtime/tests/90_tap_report.sh.j2",
		"workflow-templates/validation-harness/node-runtime/tests/_lib/graceful_shutdown.py.j2",
		"workflow-templates/validation-harness/node-runtime/tests/_lib/import_audit.py.j2",
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


def test_validate_workflow_bootstrap_lists_prompt_assembly_assets() -> None:
	wf = _workflow_text()
	for snippet in (
		"prompts/_prelude_common.txt",
		"prompts/_prelude_common_large.txt",
		"prompts/_prelude_common_xl.txt",
		"prompts/_prelude_role_persona.txt",
		"prompts/_prelude_serena.txt",
		"prompts/_prelude_output_contract.txt",
		"prompts/_templates/mode-validate-generate.txt",
		"prompts/_templates/mode-validate-diagnose.txt",
		"prompts/_templates/mode-validate-discover.txt",
		"prompts/_templates/mode-validate-fix-harness.txt",
		"prompts/_templates/mode-validate-self-heal.txt",
		"prompts/_templates/mode-validate-self-heal-continuation.txt",
	):
		assert snippet in wf


def test_stage_workflow_support_helper_runs_overlay_loader_for_validate() -> None:
	helper = _helper_text()
	for snippet in (
		"WORKFLOW.md overlay is opt-in by file presence",
		"python3 scripts/load_workflow_overlay.py",
		'--schema-path "ai-memory/schemas/workflow_overlay.v1.json"',
		'--github-env "${GITHUB_ENV}"',
	):
		assert snippet in helper


def test_stage_workflow_support_helper_uses_portable_copy_guard_and_optional_main_checkout() -> None:
	helper = _helper_text()
	assert '[ -n "${GH_TOKEN:-}" ] && checkout_support_ref "main" "${SUPPORT_STAGE_ROOT}/main"' in helper
	assert '[ "${source_path}" -ef "${target_path}" ]' in helper
	assert "realpath -m" not in helper


def test_validate_workflow_passes_template_default_env() -> None:
	wf = _workflow_text()
	assert "VALIDATION_USE_TEMPLATES: ${{ vars.VALIDATION_USE_TEMPLATES || 'true' }}" in wf
	assert 'python3 -m pip install --disable-pip-version-check --quiet --user pyyaml jsonschema jinja2' in wf


def test_validate_workflow_bootstraps_revalidate_lifecycle_ai_memory_schemas() -> None:
	wf = _workflow_text()
	assert "validation_history.v1.json" in wf
	assert "operator_bypass_audit.v1.json" in wf
	assert "revalidate_events.v1.json" in wf


def test_validate_workflow_bootstraps_codex_heartbeat_support() -> None:
	wf = _workflow_text()
	for snippet in (
		"CODEX_HEARTBEAT_ENABLED: ${{ vars.CODEX_HEARTBEAT_ENABLED || '1' }}",
		"CODEX_HEARTBEAT_INTERVAL_SECS: ${{ vars.CODEX_HEARTBEAT_INTERVAL_SECS || '30' }}",
		"scripts/codex_heartbeat.sh",
		'helper_path="scripts/stage_workflow_support.sh"',
	):
		assert snippet in wf


def test_codex_heartbeat_helper_contract() -> None:
	result = subprocess.run(
		["python3", str(CODEX_HEARTBEAT_TEST)],
		cwd=REPO_ROOT,
		capture_output=True,
		text=True,
		timeout=60,
	)
	assert result.returncode == 0, result.stdout + result.stderr


def test_run_validation_repo_checks_override_does_not_reparse_shell_metacharacters() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		marker_path = Path(tmpdir) / "override-marker"
		override = f"python3 -c 'print(123)' ; touch {marker_path}"
		result = subprocess.run(
			["bash", str(RUN_VALIDATION_REPO_CHECKS), override],
			cwd=REPO_ROOT,
			capture_output=True,
			text=True,
			timeout=60,
		)
		assert result.returncode == 0, result.stdout + result.stderr
		assert "123" in result.stdout
		assert f"# repo-check start: {override}" in result.stdout
		assert f"# repo-check ok: {override}" in result.stdout
		assert not marker_path.exists()


def test_run_validation_repo_checks_override_preserves_quoted_arguments() -> None:
	quoted_override = "python3 -c 'import sys; print(sys.argv[1])' 'hello world'"
	result = subprocess.run(
		["bash", str(RUN_VALIDATION_REPO_CHECKS), quoted_override],
		cwd=REPO_ROOT,
		capture_output=True,
		text=True,
		timeout=60,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "hello world" in result.stdout


def test_run_validation_repo_checks_override_preserves_env_prefix_assignments() -> None:
	env_override = "MY_VAR=hello python3 -c 'import os; print(os.environ[\"MY_VAR\"])'"
	result = subprocess.run(
		["bash", str(RUN_VALIDATION_REPO_CHECKS), env_override],
		cwd=REPO_ROOT,
		capture_output=True,
		text=True,
		timeout=60,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "hello" in result.stdout


def test_run_validation_repo_checks_default_commands_do_not_reparse_shell_metacharacters() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		marker_path = Path(tmpdir) / "default-marker"
		temp_script = Path(tmpdir) / "run_validation_repo_checks.sh"
		script_text = RUN_VALIDATION_REPO_CHECKS.read_text(encoding="utf-8")
		script_text = re.sub(
			r'CHECK_COMMANDS=\(\n(?:\t".*"\n)+\)',
			f'CHECK_COMMANDS=(\n\t"python3 -c \'print(456)\' ; touch {marker_path}"\n)',
			script_text,
			count=1,
		)
		temp_script.write_text(script_text, encoding="utf-8")
		result = subprocess.run(
			["bash", str(temp_script)],
			cwd=REPO_ROOT,
			capture_output=True,
			text=True,
			timeout=60,
		)
		assert result.returncode == 0, result.stdout + result.stderr
		assert "456" in result.stdout
		assert not marker_path.exists()


def main() -> int:
	test_validate_workflow_bootstrap_uses_shared_helper_and_lists_template_assets()
	test_validate_workflow_bootstrap_lists_prompt_assembly_assets()
	test_stage_workflow_support_helper_runs_overlay_loader_for_validate()
	test_stage_workflow_support_helper_uses_portable_copy_guard_and_optional_main_checkout()
	test_validate_workflow_passes_template_default_env()
	test_validate_workflow_bootstraps_revalidate_lifecycle_ai_memory_schemas()
	test_validate_workflow_bootstraps_codex_heartbeat_support()
	test_codex_heartbeat_helper_contract()
	test_run_validation_repo_checks_override_does_not_reparse_shell_metacharacters()
	test_run_validation_repo_checks_override_preserves_quoted_arguments()
	test_run_validation_repo_checks_override_preserves_env_prefix_assignments()
	test_run_validation_repo_checks_default_commands_do_not_reparse_shell_metacharacters()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

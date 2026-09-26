#!/usr/bin/env python3
"""Contract checks for template-mode bootstrap wiring in validate workflow."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import venv
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CODEX_HEARTBEAT_TEST = REPO_ROOT / "tests" / "test_codex_heartbeat.py"
RUN_VALIDATION_REPO_CHECKS = REPO_ROOT / "scripts" / "run_validation_repo_checks.sh"
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"
STAGE_WORKFLOW_SUPPORT = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"


def _workflow_text() -> str:
	return VALIDATE_WORKFLOW.read_text(encoding="utf-8")


def _helper_text() -> str:
	return STAGE_WORKFLOW_SUPPORT.read_text(encoding="utf-8")


def test_validate_workflow_bootstrap_uses_shared_helper_and_lists_template_assets() -> None:
	wf = _workflow_text()
	required_snippets = [
		'"${helper_stage_dir}/scripts/stage_workflow_support.sh" validate --manifest "${manifest_path}"',
		"UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED: ${{ vars.UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED || 'false' }}",
		"scripts/assemble_prompt.sh",
		"scripts/render_prompt.py",
		"scripts/render_validation_templates.py",
		"scripts/templates/slot_manifest.schema.json",
		"scripts/validate_driver.sh",
		"scripts/validate_process.sh",
		"scripts/transcript_archive.sh",
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
	assert 'RUNTIME_DIR: ${{ steps.runtime.outputs.runtime_dir }}' in wf
	assert 'cd "${RUNTIME_DIR}/renderer-empty"' in wf
	assert 'python3 -I -m venv "${RUNTIME_DIR}/renderer-venv"' in wf
	assert '"${RUNTIME_DIR}/renderer-venv/bin/python" -I -m pip --isolated install --disable-pip-version-check --quiet pyyaml jsonschema jinja2' in wf
	assert "id: renderer_dependencies" in wf
	assert '"${RUNTIME_DIR}/renderer-venv/bin/python" -I -c' in wf
	assert 'if pathlib.Path(sys.prefix).resolve() != environment:' in wf
	assert 'pathlib.Path(spec.origin).resolve().is_relative_to(root)' in wf
	assert "VALIDATION_RENDERER_DEPENDENCIES_READY: ${{ steps.renderer_dependencies.outcome == 'success' }}" in wf
	assert "if: always() && steps.workspace_after_create_hook.outcome != 'failure' && steps.workspace_before_run_hook.outcome != 'failure'" in wf


def test_renderer_dependency_preflight_blocks_rendering() -> None:
	process_text = VALIDATE_PROCESS.read_text(encoding="utf-8")
	function_text = process_text.split("run_template_validation_harness_renderer()\n{", 1)[1].split("\n}\n", 1)[0]
	function_text = "run_template_validation_harness_renderer()\n{" + function_text + "\n}\n"
	assert "VALIDATION_RENDERER_DEPENDENCIES_READY" in function_text
	assert "import yaml, jsonschema, jinja2" in function_text
	assert '"${renderer_python}" -I -c' in function_text
	assert '"${renderer_python}" -I "${renderer_workspace}/${renderer_script}"' in function_text
	for setup_ready, imports_available, expected_status in (
		("true", "false", 14),
		("false", "true", 14),
		("true", "true", 0),
		("", "true", 0),
	):
		with tempfile.TemporaryDirectory() as tmpdir:
			root = Path(tmpdir)
			runtime_dir = root / "runtime"
			(runtime_dir / "renderer-empty").mkdir(parents=True)
			(runtime_dir / "renderer-venv" / "bin").mkdir(parents=True)
			for asset in (
				".ai/validate.yml",
				"scripts/render_validation_templates.py",
				"scripts/templates/slot_manifest.schema.json",
				"workflow-templates/validation-harness/_shared/_lib/tap_helpers.sh.j2",
				"workflow-templates/validation-harness/_shared/tests/00_canary.sh.j2",
				"workflow-templates/validation-harness/_shared/tests/90_tap_report.sh.j2",
			):
				asset_path = root / asset
				asset_path.parent.mkdir(parents=True, exist_ok=True)
				asset_path.touch()
			shim = root / "python3"
			shim.write_text(
				"#!/bin/sh\n"
				"if [ \"$1\" = '-I' ]; then shift; fi\n"
				"if [ \"$1\" = '-c' ]; then\n"
				"  case \"$2\" in\n"
				"    *'import importlib.util'*) [ \"$IMPORTS_AVAILABLE\" = 'true' ] && exit 0; exit 1;;\n"
				"  esac\n"
				"fi\n"
				"case \"$1\" in\n"
				"  */scripts/render_validation_templates.py) touch renderer-invoked; exit 0;;\n"
				"esac\n"
				"exec \"$REAL_PYTHON3\" \"$@\"\n",
				encoding="utf-8",
			)
			shim.chmod(0o755)
			(runtime_dir / "renderer-venv" / "bin" / "python").write_bytes(shim.read_bytes())
			(runtime_dir / "renderer-venv" / "bin" / "python").chmod(0o755)
			env = os.environ.copy()
			env.update({
				"RUNTIME_DIR": str(runtime_dir),
				"PATH": f"{root}:{os.environ['PATH']}",
				"REAL_PYTHON3": sys.executable,
				"IMPORTS_AVAILABLE": imports_available,
				"GENERATE_LOG_FILE": str(root / "renderer.log"),
			})
			env.pop("BASH_ENV", None)
			if setup_ready:
				env["VALIDATION_RENDERER_DEPENDENCIES_READY"] = setup_ready
			else:
				env.pop("VALIDATION_RENDERER_DEPENDENCIES_READY", None)
			result = subprocess.run(
				["bash", "-c", function_text + "\nrun_template_validation_harness_renderer"],
				cwd=root, env=env, capture_output=True, text=True, timeout=30,
			)
			assert result.returncode == expected_status, result.stdout + result.stderr
			log_text = (root / "renderer.log").read_text(encoding="utf-8")
			assert "printf: --: invalid option" not in log_text
			assert (runtime_dir / "renderer-empty" / "renderer-invoked").exists() == (expected_status == 0)
			if expected_status == 14:
				assert "dependenc" in log_text.lower()


def test_renderer_isolated_imports_ignore_workspace_shadows_and_reject_external_origins() -> None:
	process_text = VALIDATE_PROCESS.read_text(encoding="utf-8")
	function_text = process_text.split("run_template_validation_harness_renderer()\n{", 1)[1].split("\n}\n", 1)[0]
	function_text = "run_template_validation_harness_renderer()\n{" + function_text + "\n}\n"
	workflow_probe = textwrap.dedent(_workflow_text().split('"${RUNTIME_DIR}/renderer-venv/bin/python" -I -c \'\n', 1)[1].split('\n          \' "${RUNTIME_DIR}/renderer-venv"\n', 1)[0])
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir)
		workspace = root / "workspace"
		runtime_dir = root / "runtime"
		workspace.mkdir()
		(runtime_dir / "renderer-empty").mkdir(parents=True)
		venv.EnvBuilder(with_pip=False).create(runtime_dir / "renderer-venv")
		python = runtime_dir / "renderer-venv" / "bin" / "python"
		purelib = Path(subprocess.check_output(
			[str(python), "-I", "-c", 'import sysconfig; print(sysconfig.get_path("purelib"))'],
			text=True, cwd=runtime_dir / "renderer-empty",
		).strip())
		for name in ("yaml", "jsonschema", "jinja2"):
			package = purelib / name
			package.mkdir()
			(package / "__init__.py").write_text("# installed package fixture\n", encoding="utf-8")
		for asset in (
			".ai/validate.yml", "scripts/templates/slot_manifest.schema.json",
			"workflow-templates/validation-harness/_shared/_lib/tap_helpers.sh.j2",
			"workflow-templates/validation-harness/_shared/tests/00_canary.sh.j2",
			"workflow-templates/validation-harness/_shared/tests/90_tap_report.sh.j2",
		):
			path = workspace / asset
			path.parent.mkdir(parents=True, exist_ok=True)
			path.touch()
		renderer = workspace / "scripts" / "render_validation_templates.py"
		renderer.write_text(
			'import yaml, jsonschema, jinja2\n'
			'print("isolated renderer ran")\n', encoding="utf-8",
		)
		leak_marker = root / "leaked"
		shadow = 'import os\nfrom pathlib import Path\nPath(os.environ["LEAK_MARKER"]).write_text(os.environ["GH_TOKEN"])\n'
		(workspace / "yaml.py").write_text(shadow, encoding="utf-8")
		(workspace / "scripts" / "yaml.py").write_text(shadow, encoding="utf-8")
		env = os.environ.copy()
		env.update({
			"RUNTIME_DIR": str(runtime_dir),
			"GENERATE_LOG_FILE": str(root / "renderer.log"),
			"VALIDATION_RENDERER_DEPENDENCIES_READY": "true",
			"GH_TOKEN": "fixture-secret",
			"LEAK_MARKER": str(leak_marker),
			"PYTHONPATH": str(workspace),
			"PYTHONHOME": str(workspace),
		})
		env.pop("BASH_ENV", None)
		def probe() -> subprocess.CompletedProcess[str]:
			return subprocess.run(
				[str(python), "-I", "-c", workflow_probe, str(runtime_dir / "renderer-venv")],
				cwd=runtime_dir / "renderer-empty", env=env, capture_output=True, text=True, timeout=30,
			)

		def render() -> subprocess.CompletedProcess[str]:
			return subprocess.run(
				["bash", "-c", function_text + "\nrun_template_validation_harness_renderer"],
				cwd=workspace, env=env, capture_output=True, text=True, timeout=30,
			)

		assert probe().returncode == 0
		assert subprocess.run(
			[str(python), "-I", "-c", workflow_probe, str(workspace)],
			cwd=runtime_dir / "renderer-empty", env=env, capture_output=True, text=True, timeout=30,
		).returncode != 0
		assert render().returncode == 0
		assert "isolated renderer ran" in (root / "renderer.log").read_text(encoding="utf-8")
		assert not leak_marker.exists()
		(purelib / "yaml" / "__init__.py").unlink()
		assert probe().returncode != 0
		assert render().returncode == 14  # Missing dependency cannot fall back to workspace.
		assert not leak_marker.exists()
		outside = root / "outside.py"
		outside.write_text(shadow, encoding="utf-8")
		(purelib / "yaml" / "__init__.py").symlink_to(outside)
		assert probe().returncode != 0
		assert render().returncode == 14  # A symlinked origin is not trusted.
		assert not leak_marker.exists()


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
		"UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED: ${{ vars.UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED || 'false' }}",
		"scripts/codex_heartbeat.sh",
		'"${helper_stage_dir}/scripts/stage_workflow_support.sh" validate --manifest "${manifest_path}"',
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
	test_renderer_dependency_preflight_blocks_rendering()
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

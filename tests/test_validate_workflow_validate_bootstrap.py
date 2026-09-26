#!/usr/bin/env python3
"""Contract checks for template-mode bootstrap wiring in validate workflow."""

from __future__ import annotations

import os
import json
import re
import subprocess
import sys
import tempfile
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
		'python3 -E "${SUPPORT_PRIMARY_ROOT}/scripts/load_workflow_overlay.py"',
		'--schema-path "ai-memory/schemas/workflow_overlay.v1.json"',
		'--github-env "${GITHUB_ENV}"',
	):
		assert snippet in helper


def test_stage_workflow_support_helper_uses_portable_copy_guard_and_optional_main_checkout() -> None:
	helper = _helper_text()
	assert '[ -n "${GH_TOKEN:-}" ] && checkout_support_ref "main" "${SUPPORT_STAGE_ROOT}/main"' in helper
	assert '[ "${source_path}" -ef "${target_path}" ]' in helper
	assert "realpath -m" not in helper


def test_validation_optional_executable_replaces_branch_owned_copy(tmp_path: Path) -> None:
	staging = _helper_text()
	fn = "stage_optional_preserve_entry()\n{" + staging.split("stage_optional_preserve_entry()\n{", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
	workspace = tmp_path / "workspace"
	verified = tmp_path / "verified"
	for root, body in ((workspace, "echo branch"), (verified, "echo verified")):
		path = root / "scripts/install_semble.sh"
		path.parent.mkdir(parents=True)
		path.write_text(body, encoding="utf-8")
	program = fn + 'copy_from_ref_or_local() { cp "${SUPPORT_PRIMARY_ROOT}/$1" "$2"; }\nrecord_fetched_script() { :; }\n'
	program += 'stage_optional_preserve_entry scripts/install_semble.sh true install_semble.sh\n'
	env = {**os.environ, "TARGET_NAME": "validate", "SUPPORT_PRIMARY_ROOT": str(verified)}
	env.pop("BASH_ENV", None)
	result = subprocess.run(["bash", "-c", program], cwd=workspace, env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert (workspace / "scripts/install_semble.sh").read_text(encoding="utf-8") == "echo verified"


def test_validate_run_start_memory_uses_private_source() -> None:
	wf = _workflow_text()
	step = wf.split("- name: Record validation run start", 1)[1].split("- name: Install Python dependencies", 1)[0]
	assert 'source "${VALIDATE_TRUSTED_SUPPORT_ROOT:?}/scripts/memory_helpers.sh"' in step
	assert "source scripts/memory_helpers.sh" not in step
	assert 'bash "${VALIDATE_TRUSTED_SUPPORT_ROOT:?}/scripts/validate_process.sh"' in wf


def test_private_memory_python_ignores_target_sibling_modules(tmp_path: Path) -> None:
	private_scripts = tmp_path / "private/scripts"
	private_scripts.mkdir(parents=True)
	(private_scripts / "memory_helpers.sh").write_bytes((REPO_ROOT / "scripts/memory_helpers.sh").read_bytes())
	(private_scripts / "ai_memory.py").write_text("import json\nprint(json.dumps({'ok': True}))\n", encoding="utf-8")
	checkout = tmp_path / "checkout"
	(checkout / "scripts").mkdir(parents=True)
	marker = tmp_path / "imported-branch-module"
	(checkout / "scripts/json.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
	result = subprocess.run(["bash", "-c", 'source "$1"; memory_record_run_event', "bash",
		str(private_scripts / "memory_helpers.sh")], cwd=checkout,
		env={**os.environ, "PYTHONPATH": str(checkout / "scripts"), "GH_TOKEN": "sentinel",
			"PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=20)
	assert result.returncode == 0, result.stderr
	assert not marker.exists()
	assert '{"ok": true}' in result.stdout


def test_validate_wrapper_rejects_missing_or_mismatched_trusted_driver(tmp_path: Path) -> None:
	process = VALIDATE_PROCESS.read_text(encoding="utf-8")
	function = "ensure_validate_wrapper()\n{" + process.split("ensure_validate_wrapper()\n{", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
	checkout = tmp_path / "checkout"
	private = tmp_path / "private/scripts"
	(checkout / "scripts").mkdir(parents=True)
	private.mkdir(parents=True)
	(checkout / "scripts/validate_driver.sh").write_text("verified\n", encoding="utf-8")
	env = {**os.environ, "_validate_script_dir": str(private)}
	env.pop("BASH_ENV", None)
	def invoke() -> subprocess.CompletedProcess[str]:
		return subprocess.run(["bash", "-c", function + "ensure_validate_wrapper"], cwd=checkout,
			env=env, capture_output=True, text=True)

	assert invoke().returncode != 0
	assert not (checkout / "validation/validate.sh").exists()
	(private / "validate_driver.sh").write_text("verified\n", encoding="utf-8")
	assert invoke().returncode == 0
	(checkout / "scripts/validate_driver.sh").write_text("untrusted\n", encoding="utf-8")
	assert invoke().returncode != 0


def test_validate_workflow_passes_template_default_env() -> None:
	wf = _workflow_text()
	assert "VALIDATION_USE_TEMPLATES: ${{ vars.VALIDATION_USE_TEMPLATES || 'true' }}" in wf
	assert 'python3 -m pip install --disable-pip-version-check --quiet --user pyyaml jsonschema jinja2' in wf
	assert "id: renderer_dependencies" in wf
	assert "python3 -E -c 'import yaml, jsonschema, jinja2'" in wf
	assert "VALIDATION_RENDERER_DEPENDENCIES_READY: ${{ steps.renderer_dependencies.outcome == 'success' }}" in wf
	assert "steps.workspace_after_create_hook.outcome == 'success' && steps.workspace_before_run_hook.outcome == 'success'" in wf


def test_validate_prerequisites_and_credential_free_failure_tail() -> None:
	import yaml

	steps = yaml.safe_load(_workflow_text())["jobs"]["validate"]["steps"]
	by_name = {step["name"]: step for step in steps}
	condition = by_name["Run validation process"]["if"]
	for step_id in (
		"verified_checkout", "runtime", "support_files", "workspace_meta",
		"workspace_state", "workspace_contents", "workspace_after_create_hook",
		"workspace_before_run_hook",
	):
		assert f"steps.{step_id}.outcome == 'success'" in condition
		assert f"steps.{step_id}.outcome != 'failure'" not in condition
	assert "always()" not in condition
	for step_name in ("Run workspace after_run hook", "Emit Serena stats", "Record validation candidate", "Record validation run end", "Force orchestrate poll after validation finalization", "Run workspace before_remove hook", "Upload validation artifacts"):
		assert "steps.validate_run.outcome != 'skipped'" in by_name[step_name]["if"]
	collector = by_name["Collect validation status"]
	assert collector["if"] == "always()"
	assert "GH_TOKEN" not in collector.get("env", {})
	assert "${RUNNER_TEMP}/validate-status-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in collector["run"]
	assert 'bash "${VALIDATE_TRUSTED_SUPPORT_ROOT:?}/scripts/validate_process.sh"' in by_name["Run validation process"]["run"]


def test_bootstrap_failure_status_without_runtime_or_credentials(tmp_path: Path) -> None:
	import yaml

	steps = yaml.safe_load(_workflow_text())["jobs"]["validate"]["steps"]
	collector = next(step for step in steps if step["name"] == "Collect validation status")
	output = tmp_path / "output"
	env = {key: value for key, value in os.environ.items() if key not in ("GH_TOKEN", "GH_PAT", "OPENROUTER_API_KEY", "BASH_ENV")}
	env.update({"RUNTIME_DIR": "", "RUNNER_TEMP": str(tmp_path), "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_OUTPUT": str(output)})
	result = subprocess.run(["bash", "-c", collector["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert "status=error" in output.read_text(encoding="utf-8")
	assert (tmp_path / "validate-status-123-1" / "validation_status.json").is_file()


def test_validate_template_inventory_rejects_untrusted_entries(tmp_path: Path) -> None:
	staging = _helper_text()
	program = staging.split('python3 -I - "${MANIFEST_PATH}" "${REPO_ROOT}" "${SUPPORT_PRIMARY_ROOT}" "${trusted_root}" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
	manifest = {"optional_copy_files": []}
	source = tmp_path / "source"
	target = tmp_path / "target"
	source.mkdir()
	target.mkdir()
	for directory in ("scripts", "prompts", "ai-memory"):
		(source / directory).mkdir()
	for family in ("_shared", "node-runtime"):
		rel = f"workflow-templates/validation-harness/{family}/example.j2"
		manifest["optional_copy_files"].append(rel)
		asset = source / rel
		asset.parent.mkdir(parents=True, exist_ok=True)
		asset.write_text("trusted", encoding="utf-8")
	manifest_file = tmp_path / "manifest.json"
	manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
	import_marker = tmp_path / "host-json-imported"
	(target / "json.py").write_text(f"open({str(import_marker)!r}, 'w').close()\n", encoding="utf-8")
	def check() -> int:
		private = tmp_path / "private"
		if private.exists():
			import shutil
			shutil.rmtree(private)
		return subprocess.run([sys.executable, "-I", "-c", program, str(manifest_file), str(target), str(source), str(private)], cwd=target, capture_output=True, text=True).returncode
	assert check() == 0
	assert not import_marker.exists()
	for family in ("_shared", "node-runtime"):
		asset = target / f"workflow-templates/validation-harness/{family}/example.j2"
		asset.parent.mkdir(parents=True, exist_ok=True)
		asset.write_text("trusted", encoding="utf-8")
		assert check() == 0
		asset.write_text("tampered", encoding="utf-8")
		assert check() != 0
		asset.unlink()
		asset.symlink_to(source / manifest["optional_copy_files"][0])
		assert check() != 0
		asset.unlink()
		extra = asset.parent / "extra.j2"
		extra.write_text("{{ 1 + 1 }}", encoding="utf-8")
		assert check() != 0
		extra.unlink()
	(source / manifest["optional_copy_files"][0]).unlink()
	assert check() != 0


def test_renderer_dependency_preflight_blocks_rendering() -> None:
	process_text = VALIDATE_PROCESS.read_text(encoding="utf-8")
	function_text = process_text.split("run_template_validation_harness_renderer()\n{", 1)[1].split("\n}\n", 1)[0]
	function_text = "run_template_validation_harness_renderer()\n{" + function_text + "\n}\n"
	assert "VALIDATION_RENDERER_DEPENDENCIES_READY" in function_text
	assert "import yaml, jsonschema, jinja2" in function_text
	for setup_ready, imports_available, expected_status in (
		("true", "false", 14),
		("false", "true", 14),
		("true", "true", 0),
		("", "true", 0),
	):
		with tempfile.TemporaryDirectory() as tmpdir:
			root = Path(tmpdir)
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
				"[ \"$1\" = '-E' ] && shift\n"
				"if [ \"$1\" = '-c' ] && [ \"$2\" = 'import yaml, jsonschema, jinja2' ]; then\n"
				"  [ \"$IMPORTS_AVAILABLE\" = 'true' ] && exit 0\n"
				"  exit 1\n"
				"fi\n"
				"case \"$1\" in\n"
				"  */scripts/render_validation_templates.py) touch renderer-invoked; exit 0;;\n"
				"esac\n"
				"exec \"$REAL_PYTHON3\" \"$@\"\n",
				encoding="utf-8",
			)
			shim.chmod(0o755)
			env = os.environ.copy()
			env.update({
				"PATH": f"{root}:{os.environ['PATH']}",
				"REAL_PYTHON3": sys.executable,
				"IMPORTS_AVAILABLE": imports_available,
				"GENERATE_LOG_FILE": str(root / "renderer.log"),
				"VALIDATE_TRUSTED_SUPPORT_ROOT": str(root),
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
			assert (root / "renderer-invoked").exists() == (expected_status == 0)
			if expected_status == 14:
				assert "dependenc" in log_text.lower()


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

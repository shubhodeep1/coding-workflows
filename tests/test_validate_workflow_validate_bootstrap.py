#!/usr/bin/env python3
"""Contract checks for template-mode bootstrap wiring in validate workflow."""

from __future__ import annotations

import json
import os
import re
import shutil
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
	assert "VALIDATION_RENDERER_DEPENDENCIES_READY: ${{ steps.renderer_dependencies.outcome == 'success' && steps.renderer_dependencies.outputs.renderer_state == 'prepared' }}" in wf
	assert "if: always() && steps.workspace_after_create_hook.outcome != 'failure' && steps.workspace_before_run_hook.outcome != 'failure'" in wf


def test_renderer_dependency_step_isolates_workspace_imports_and_shell_startup() -> None:
	wf = _workflow_text()
	step_match = re.search(
		r"      - name: Install Python dependencies for validation renderer\n(?P<body>.*?)(?=      - name: |\Z)",
		wf, re.DOTALL,
	)
	assert step_match is not None
	step = step_match.group("body")
	assert "          BASH_ENV: ''\n" in step
	assert 'renderer_path="${renderer_root}/scripts/render_validation_templates.py"' in step
	assert 'if [ -f "${renderer_path}" ]; then' in step
	assert 'cd "${RUNTIME_DIR}/renderer-empty"' in step
	script = textwrap.dedent(step.split("        run: |\n", 1)[1])
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir)
		workspace = root / "workspace"
		runtime_dir = root / "runtime"
		bin_dir = root / "bin"
		for directory in (workspace / "scripts", runtime_dir, bin_dir):
			directory.mkdir(parents=True)
		(workspace / "scripts" / "render_validation_templates.py").touch()
		startup_file = root / "shell-startup"
		startup_marker = root / "sourced-startup"
		import_marker = root / "imported-yaml"
		startup_file.write_text(f"touch {startup_marker}\n", encoding="utf-8")
		(workspace / "yaml.py").write_text(
			f"from pathlib import Path\nPath({str(import_marker)!r}).touch()\n"
			f"Path({str(startup_file)!r}).write_text('compromised')\n",
			encoding="utf-8",
		)
		# One shim stands in for the host python3 and the renderer venv's python:
		# every call must be isolated (-I) and must not see the workspace. The venv
		# origin probe itself is exercised against a real venv in
		# test_renderer_isolated_imports_ignore_workspace_shadows_and_reject_external_origins.
		python_shim = bin_dir / "python3"
		python_shim.write_text(
			"#!/bin/sh\n"
			"[ \"$1\" = -I ] || exit 11\n"
			"\"$REAL_PYTHON3\" -I -c '"
			"import importlib, importlib.util, os, pathlib, sys; "
			"workspace = pathlib.Path(os.environ[\"GITHUB_WORKSPACE\"]).resolve(); "
			"assert pathlib.Path.cwd() != workspace; "
			"assert all(pathlib.Path(p or os.getcwd()).resolve() != workspace for p in sys.path); "
			"assert all(not (spec := importlib.util.find_spec(name)) or "
			"not spec.origin or pathlib.Path(spec.origin).resolve() != workspace / (name + \".py\") "
			"for name in (\"pip\", \"yaml\", \"jsonschema\", \"jinja2\")); "
			"[importlib.import_module(name) for name in (\"yaml\", \"jsonschema\", \"jinja2\") "
			"if importlib.util.find_spec(name)]' || exit 15\n"
			"case \"$2\" in\n"
			"  -m)\n"
			"    case \"$3\" in\n"
			"      venv) mkdir -p \"$4/bin\" && cp \"$0\" \"$4/bin/python\" && exit 0;;\n"
			"      pip) [ \"$4\" = --isolated ] || exit 12; exit 0;;\n"
			"    esac\n"
			"    exit 12;;\n"
			"  -c) case \"$3\" in *'import yaml, jsonschema, jinja2'*) exit 0;; esac; exit 13;;\n"
			"esac\n"
			"exit 14\n",
			encoding="utf-8",
		)
		python_shim.chmod(0o755)
		env = os.environ.copy()
		env.update({
			"BASH_ENV": "",  # Step env overrides the prior workspace-directed BASH_ENV.
			"GITHUB_WORKSPACE": str(workspace),
			"PATH": f"{bin_dir}:{os.environ['PATH']}",
			"PYTHONHOME": str(workspace),
			"PYTHONPATH": str(workspace),
			"REAL_PYTHON3": sys.executable,
			"RUNTIME_DIR": str(runtime_dir),
			"GITHUB_OUTPUT": str(root / "github_output"),
		})
		env.pop("WORKSPACE_PATH", None)
		result = subprocess.run(
			["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
			cwd=workspace, env=env, capture_output=True, text=True, timeout=30,
		)
		assert result.returncode == 0, result.stdout + result.stderr
		assert (runtime_dir / "renderer-empty").is_dir()
		assert (runtime_dir / "renderer-venv" / "bin" / "python").exists()
		assert not startup_marker.exists()
		assert not import_marker.exists()
		assert startup_file.read_text(encoding="utf-8") == f"touch {startup_marker}\n"


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


def _renderer_dependency_step_script() -> str:
	step_match = re.search(
		r"      - name: Install Python dependencies for validation renderer\n(?P<body>.*?)(?=      - name: |\Z)",
		_workflow_text(), re.DOTALL,
	)
	assert step_match is not None
	return textwrap.dedent(step_match.group("body").split("        run: |\n", 1)[1])


def _validate_step_blocks() -> list[str]:
	# Text slicing (no PyYAML dependency): one block per top-level job step.
	steps_text = _workflow_text().split("\n    steps:\n", 1)[1]
	return [block for block in re.split(r"\n(?=      - name: )", steps_text) if "      - name: " in block]


def _step_block(step_id: str) -> str:
	matches = [block for block in _validate_step_blocks() if f"\n        id: {step_id}\n" in block + "\n"]
	assert len(matches) == 1, step_id
	return matches[0]


def test_renderer_dependency_step_runs_after_unrelated_earlier_failure() -> None:
	# Regression for #6521: without an always() gate, any unrelated earlier
	# failure skipped renderer preparation while validation still ran.
	prep = _step_block("renderer_dependencies")
	condition_match = re.search(r"\n        if: (?P<cond>.+)\n", prep)
	assert condition_match is not None
	condition = condition_match.group("cond")
	assert condition.startswith("always()")
	assert "steps.runtime.outcome == 'success'" in condition
	assert "steps.support_staging.outcome == 'success'" in condition
	assert "steps.workspace_after_create_hook.outcome != 'failure'" in condition
	assert "      - name: Fetch workflow support files\n        id: support_staging\n" in _step_block("support_staging")
	assert "          WORKSPACE_PATH: ${{ steps.workspace_state.outputs.workspace_path }}\n" in prep
	assert "          BASH_ENV: ''\n" in prep
	run_step = _step_block("validate_run")
	assert "          VALIDATION_RENDERER_DEPENDENCIES_OUTCOME: ${{ steps.renderer_dependencies.outcome }}\n" in run_step
	assert "          VALIDATION_RENDERER_DEPENDENCIES_READY: ${{ steps.renderer_dependencies.outcome == 'success' && steps.renderer_dependencies.outputs.renderer_state == 'prepared' }}\n" in run_step
	ids = [m.group(1) for block in _validate_step_blocks() for m in [re.search(r"\n        id: (\S+)", block)] if m]
	assert ids.index("support_staging") < ids.index("workspace_after_create_hook") < ids.index("renderer_dependencies") < ids.index("validate_run")


def test_renderer_dependency_step_checks_renderer_in_workspace_path() -> None:
	# Regression for #6521: the presence check ran relative to GITHUB_WORKSPACE
	# while validate_process.sh runs the renderer from WORKSPACE_PATH.
	script = _renderer_dependency_step_script()
	for renderer_in_workspace_path in (True, False):
		with tempfile.TemporaryDirectory() as tmpdir:
			root = Path(tmpdir)
			github_workspace = root / "checkout"
			workspace_path = root / "workspace"
			runtime_dir = root / "runtime"
			bin_dir = root / "bin"
			for directory in (github_workspace, workspace_path / "scripts", runtime_dir, bin_dir):
				directory.mkdir(parents=True)
			if renderer_in_workspace_path:
				(workspace_path / "scripts" / "render_validation_templates.py").touch()
			python_shim = bin_dir / "python3"
			python_shim.write_text(
				"#!/bin/sh\n"
				"[ \"$1\" = -I ] || exit 11\n"
				"case \"$2\" in\n"
				"  -m) case \"$3\" in\n"
				"      venv) mkdir -p \"$4/bin\" && cp \"$0\" \"$4/bin/python\" && exit 0;;\n"
				"      pip) exit 0;;\n"
				"    esac; exit 12;;\n"
				"  -c) exit 0;;\n"
				"esac\n"
				"exit 14\n",
				encoding="utf-8",
			)
			python_shim.chmod(0o755)
			github_output = root / "github_output"
			env = os.environ.copy()
			env.update({
				"BASH_ENV": "",
				"GITHUB_WORKSPACE": str(github_workspace),
				"WORKSPACE_PATH": str(workspace_path),
				"GITHUB_OUTPUT": str(github_output),
				"PATH": f"{bin_dir}:{os.environ['PATH']}",
				"RUNTIME_DIR": str(runtime_dir),
			})
			result = subprocess.run(
				["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
				cwd=github_workspace, env=env, capture_output=True, text=True, timeout=30,
			)
			outputs = github_output.read_text(encoding="utf-8") if github_output.exists() else ""
			if renderer_in_workspace_path:
				assert result.returncode == 0, result.stdout + result.stderr
				assert (runtime_dir / "renderer-empty").is_dir()
				assert (runtime_dir / "renderer-venv" / "bin" / "python").exists()
				assert "renderer_state=prepared" in outputs
			else:
				assert result.returncode != 0, result.stdout + result.stderr
				assert not (runtime_dir / "renderer-empty").exists()
				assert "renderer_state=absent" in outputs
				assert "::error::" in result.stderr
				assert str(workspace_path / "scripts" / "render_validation_templates.py") in result.stderr


def test_skipped_renderer_preparation_surfaces_dependency_failure() -> None:
	# Regression for #6521: a skipped preparation step was reported as
	# "Trusted renderer runtime is unavailable", hiding the real cause.
	process_text = VALIDATE_PROCESS.read_text(encoding="utf-8")
	function_text = process_text.split("run_template_validation_harness_renderer()\n{", 1)[1].split("\n}\n", 1)[0]
	function_text = "run_template_validation_harness_renderer()\n{" + function_text + "\n}\n"
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir)
		runtime_dir = root / "runtime"
		runtime_dir.mkdir()
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
		env = os.environ.copy()
		env.update({
			"RUNTIME_DIR": str(runtime_dir),
			"GENERATE_LOG_FILE": str(root / "renderer.log"),
			"VALIDATION_RENDERER_DEPENDENCIES_READY": "false",
			"VALIDATION_RENDERER_DEPENDENCIES_OUTCOME": "skipped",
		})
		env.pop("BASH_ENV", None)
		result = subprocess.run(
			["bash", "-c", function_text + "\nrun_template_validation_harness_renderer"],
			cwd=root, env=env, capture_output=True, text=True, timeout=30,
		)
		assert result.returncode == 14, result.stdout + result.stderr
		log_text = (root / "renderer.log").read_text(encoding="utf-8")
		assert "dependency setup did not succeed (step outcome: skipped)" in log_text
		assert "Trusted renderer runtime is unavailable" not in log_text
		assert "::error::Template renderer dependency setup did not succeed (step outcome: skipped)" in result.stderr


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


# --- Issue #6578: self-repo validation-harness templates come from the
# verified support commit, never from the (integration) validation checkout.

_TEMPLATE_REL = "workflow-templates/validation-harness/python-repo-checks/tests/20_import_audit.sh.j2"
_STALE_HOST_TEMPLATE = '#!/usr/bin/env bash\npython3 "${SCRIPT_DIR}/_lib/import_audit.py"\n'
_TRUSTED_CONTAINER_TEMPLATE = (
	'#!/usr/bin/env bash\n'
	'docker compose -f "${COMPOSE_FILE}" exec -T app python /tests/_lib/import_audit.py\n'
)
_JQ_SHIM = (
	"#!/usr/bin/env python3\n"
	"import json, sys\n"
	"args = sys.argv[1:]\n"
	"key = args[args.index('--arg') + 2]\n"
	"expr, path = args[args.index('--arg') + 3], args[args.index('--arg') + 4]\n"
	"value = json.load(open(path)).get(key)\n"
	"if '[]' in expr:\n"
	"    for item in value or []:\n"
	"        print(item)\n"
	"elif value:\n"
	"    print(value)\n"
)


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(
		["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
	).stdout.strip()


def _init_repo(path: Path, files: dict[str, str]) -> str:
	path.mkdir(parents=True)
	_git(path, "init", "-q", "-b", "main")
	for rel, content in files.items():
		target = path / rel
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(content, encoding="utf-8")
	_git(path, "add", "-A")
	_git(path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
	return _git(path, "rev-parse", "HEAD")


def _run_validate_staging(
	tmp: Path,
	*,
	source_files: dict[str, str],
	manifest_paths: list[str],
	repository: str = "shubhodeep1/coding-workflows",
	support_ref: str | None = None,
	helper: Path = STAGE_WORKFLOW_SUPPORT,
	workspace_template_symlink: Path | None = None,
	workspace_template_directory: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
	overlay_files = {
		"scripts/load_workflow_overlay.py": (REPO_ROOT / "scripts" / "load_workflow_overlay.py").read_text(encoding="utf-8"),
		"ai-memory/schemas/workflow_overlay.v1.json": (REPO_ROOT / "ai-memory" / "schemas" / "workflow_overlay.v1.json").read_text(encoding="utf-8"),
	}
	source = tmp / "source"
	source_sha = _init_repo(source, {**overlay_files, **source_files})
	_git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
	workspace = tmp / "workspace"
	_init_repo(workspace, {**overlay_files, _TEMPLATE_REL: _STALE_HOST_TEMPLATE})
	if workspace_template_symlink is not None:
		(workspace / _TEMPLATE_REL).unlink()
		(workspace / _TEMPLATE_REL).symlink_to(workspace_template_symlink)
	if workspace_template_directory:
		(workspace / _TEMPLATE_REL).unlink()
		(workspace / _TEMPLATE_REL).mkdir()
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	if not shutil.which("jq"):
		(bin_dir / "jq").write_text(_JQ_SHIM, encoding="utf-8")
		(bin_dir / "jq").chmod(0o755)
	gitconfig = tmp / "gitconfig"
	gitconfig.write_text(
		f'[url "file://{source}"]\n'
		f"\tinsteadOf = https://x-access-token:dummy@github.com/{repository}\n"
		f"\tinsteadOf = https://x-access-token:dummy@github.com/shubhodeep1/coding-workflows\n"
		"[protocol \"file\"]\n\tallow = always\n",
		encoding="utf-8",
	)
	manifest = tmp / "manifest.json"
	manifest.write_text(json.dumps({"optional_copy_files": manifest_paths}), encoding="utf-8")
	runner_temp = tmp / "runner_temp"
	runner_temp.mkdir()
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in {"BASH_ENV", "ENV"}}
	env.pop("VALIDATE_AUTHORIZED_TARGET_SHA", None)
	env.pop("WORKFLOW_SUPPORT_SOURCE_REPO", None)
	env.update({
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"GIT_CONFIG_GLOBAL": str(gitconfig),
		"GIT_CONFIG_NOSYSTEM": "1",
		"GITHUB_WORKSPACE": str(workspace),
		"GITHUB_REPOSITORY": repository,
		"GITHUB_SERVER_URL": "https://github.com",
		"GH_TOKEN": "dummy",
		"WORKFLOW_SUPPORT_REF": support_ref or source_sha,
		"RUNNER_TEMP": str(runner_temp),
		"GITHUB_ENV": str(tmp / "github_env"),
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	result = subprocess.run(
		["bash", str(helper), "validate", "--manifest", str(manifest)],
		cwd=workspace, env=env, capture_output=True, text=True, timeout=120,
	)
	return result, workspace


def test_self_repo_validation_templates_come_from_verified_support_commit() -> None:
	# Regression for #6578: an older integration-branch template ran the import
	# audit with the runner's Python; without the new copy path it still renders
	# that host command, while the trusted copy renders the container command.
	with tempfile.TemporaryDirectory() as tmpdir:
		for baseline in (True, False):
			fixture_root = Path(tmpdir) / ("baseline" if baseline else "fixed")
			result, workspace = _run_validate_staging(
				fixture_root,
				source_files={_TEMPLATE_REL: _TRUSTED_CONTAINER_TEMPLATE},
				manifest_paths=[] if baseline else [_TEMPLATE_REL],
			)
			assert result.returncode == 0, result.stdout + result.stderr
			(workspace / "workflow-templates/validation-harness/_shared").mkdir(parents=True)
			validation_manifest = fixture_root / "validate.yml"
			validation_manifest.write_text(
				"type: python-repo-checks\nslots:\n  project_name: staging-regression\n  canary_tools: [python3]\n",
				encoding="utf-8",
			)
			rendered_root = fixture_root / "rendered"
			render_result = subprocess.run(
				[sys.executable, str(REPO_ROOT / "scripts/render_validation_templates.py"),
				 "--manifest", str(validation_manifest),
				 "--schema", str(REPO_ROOT / "scripts/templates/slot_manifest.schema.json"),
				 "--templates-root", str(workspace / "workflow-templates/validation-harness"),
				 "--output-root", str(rendered_root)],
				cwd=workspace, capture_output=True, text=True, timeout=30,
			)
			assert render_result.returncode == 0, render_result.stdout + render_result.stderr
			audit_text = (rendered_root / "tests/20_import_audit.sh").read_text(encoding="utf-8")
			if baseline:
				assert 'python3 "${SCRIPT_DIR}/_lib/import_audit.py"' in audit_text
				assert "docker compose" not in audit_text
			else:
				assert 'docker compose -f "${COMPOSE_FILE}" exec -T app python /tests/_lib/import_audit.py' in audit_text
				assert 'python3 "${SCRIPT_DIR}' not in audit_text
				assert f"VALIDATE_TRUSTED_TEMPLATE_OVERRIDE path={_TEMPLATE_REL}" in result.stdout
				assert "VALIDATE_TRUSTED_TEMPLATES staged=1" in result.stdout


def test_self_repo_validation_templates_fail_closed_without_trusted_commit() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		result, workspace = _run_validate_staging(
			Path(tmpdir),
			source_files={_TEMPLATE_REL: _TRUSTED_CONTAINER_TEMPLATE},
			manifest_paths=[_TEMPLATE_REL],
			support_ref="0" * 40,
		)
		assert result.returncode != 0, result.stdout + result.stderr
		assert "::error::Trusted validation-harness templates are unavailable" in result.stderr
		assert (workspace / _TEMPLATE_REL).read_text(encoding="utf-8") == _STALE_HOST_TEMPLATE


def test_self_repo_validation_templates_fail_closed_on_missing_trusted_asset() -> None:
	missing_rel = "workflow-templates/validation-harness/python-repo-checks/tests/99_missing.sh.j2"
	with tempfile.TemporaryDirectory() as tmpdir:
		result, _ = _run_validate_staging(
			Path(tmpdir),
			source_files={_TEMPLATE_REL: _TRUSTED_CONTAINER_TEMPLATE},
			manifest_paths=[_TEMPLATE_REL, missing_rel],
		)
		assert result.returncode != 0, result.stdout + result.stderr
		assert f"Required trusted validation-harness template {missing_rel} is missing from" in result.stderr


def test_self_repo_validation_templates_reject_symlinked_destination() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		fixture_root = Path(tmpdir)
		outside_file = fixture_root / "outside.sh"
		outside_file.write_text("untouched\n", encoding="utf-8")
		result, workspace = _run_validate_staging(
			fixture_root,
			source_files={_TEMPLATE_REL: _TRUSTED_CONTAINER_TEMPLATE},
			manifest_paths=[_TEMPLATE_REL],
			workspace_template_symlink=outside_file,
		)
		assert result.returncode != 0, result.stdout + result.stderr
		assert "::error::Refusing symlinked validation-harness template path" in result.stderr
		assert (workspace / _TEMPLATE_REL).is_symlink()
		assert outside_file.read_text(encoding="utf-8") == "untouched\n"


def test_self_repo_validation_templates_reject_directory_destination() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		result, workspace = _run_validate_staging(
			Path(tmpdir),
			source_files={_TEMPLATE_REL: _TRUSTED_CONTAINER_TEMPLATE},
			manifest_paths=[_TEMPLATE_REL],
			workspace_template_directory=True,
		)
		assert result.returncode != 0, result.stdout + result.stderr
		assert "::error::Refusing non-file validation-harness template path" in result.stderr
		assert (workspace / _TEMPLATE_REL).is_dir()
		assert list((workspace / _TEMPLATE_REL).iterdir()) == []


def test_consumer_validation_templates_keep_existing_copy_path() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		result, workspace = _run_validate_staging(
			Path(tmpdir),
			source_files={_TEMPLATE_REL: _TRUSTED_CONTAINER_TEMPLATE},
			manifest_paths=[_TEMPLATE_REL],
			repository="other/repo",
		)
		assert result.returncode == 0, result.stdout + result.stderr
		assert "VALIDATE_TRUSTED_TEMPLATE" not in result.stdout
		assert (workspace / _TEMPLATE_REL).read_text(encoding="utf-8") == _TRUSTED_CONTAINER_TEMPLATE


def test_stage_workflow_support_helper_routes_self_repo_templates_to_trusted_commit() -> None:
	helper = _helper_text()
	assert "stage_self_repo_validation_template_entry" in helper
	assert "workflow-templates/validation-harness/*" in helper
	assert 'checkout_support_ref "${ORIGINAL_SCRIPT_REF}" "${SUPPORT_STAGE_ROOT}/trusted-templates"' in helper


def main() -> int:
	test_validate_workflow_bootstrap_uses_shared_helper_and_lists_template_assets()
	test_validate_workflow_bootstrap_lists_prompt_assembly_assets()
	test_stage_workflow_support_helper_runs_overlay_loader_for_validate()
	test_stage_workflow_support_helper_uses_portable_copy_guard_and_optional_main_checkout()
	test_validate_workflow_passes_template_default_env()
	test_renderer_dependency_step_isolates_workspace_imports_and_shell_startup()
	test_renderer_dependency_preflight_blocks_rendering()
	test_renderer_dependency_step_runs_after_unrelated_earlier_failure()
	test_renderer_dependency_step_checks_renderer_in_workspace_path()
	test_skipped_renderer_preparation_surfaces_dependency_failure()
	test_self_repo_validation_templates_come_from_verified_support_commit()
	test_self_repo_validation_templates_fail_closed_without_trusted_commit()
	test_self_repo_validation_templates_fail_closed_on_missing_trusted_asset()
	test_self_repo_validation_templates_reject_symlinked_destination()
	test_self_repo_validation_templates_reject_directory_destination()
	test_consumer_validation_templates_keep_existing_copy_path()
	test_stage_workflow_support_helper_routes_self_repo_templates_to_trusted_commit()
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

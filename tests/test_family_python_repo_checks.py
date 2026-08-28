#!/usr/bin/env python3
"""Family-specific regression tests for python-repo-checks validation templates."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_validation_templates.py"
SCHEMA_PATH = REPO_ROOT / "scripts" / "templates" / "slot_manifest.schema.json"
TEMPLATES_ROOT = REPO_ROOT / "workflow-templates" / "validation-harness"
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "validation_harness" / "python_repo_checks"


def _manifest_payload() -> dict:
	return {
		"type": "python-repo-checks",
		"entry": "scripts/run_validation_repo_checks.sh",
		"slots": {
			"project_name": "demo-project",
			"canary_tools": ["bash", "python3", "jq"],
			"tap_plan": 3,
		},
	}


def _write_yaml(path: Path, payload: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _run_renderer(manifest_path: Path, output_root: Path) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		[
			"python3",
			str(SCRIPT_PATH),
			"--manifest",
			str(manifest_path),
			"--schema",
			str(SCHEMA_PATH),
			"--templates-root",
			str(TEMPLATES_ROOT),
			"--output-root",
			str(output_root),
		],
		text=True,
		capture_output=True,
		env=env,
	)


def _snapshot_directory(root: Path) -> tuple[list[str], str]:
	files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
	hasher = hashlib.sha256()
	for rel in files:
		payload = (root / rel).read_bytes()
		hasher.update(rel.encode("utf-8"))
		hasher.update(b"\0")
		hasher.update(payload)
		hasher.update(b"\0")
	return files, hasher.hexdigest()


def _fixture_files() -> list[str]:
	return sorted(
		path.relative_to(FIXTURES_ROOT).as_posix()
		for path in FIXTURES_ROOT.rglob("*")
		if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith((".pyc", ".pyo", ".pyd"))
	)


def _assert_fixture_match(render_root: Path) -> None:
	render_files, _ = _snapshot_directory(render_root)
	fixture_files = _fixture_files()
	assert render_files == fixture_files
	for rel in fixture_files:
		rendered = (render_root / rel).read_text(encoding="utf-8")
		fixture = (FIXTURES_ROOT / rel).read_text(encoding="utf-8")
		assert rendered == fixture, f"fixture mismatch: {rel}"


def test_python_repo_checks_golden_output_matches_fixture_tree() -> None:
	with tempfile.TemporaryDirectory(prefix="render-python-repo-checks-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr
		_assert_fixture_match(output_root)


def test_python_repo_checks_invariants_regression_guards() -> None:
	with tempfile.TemporaryDirectory(prefix="render-python-repo-checks-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr

		compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
		assert "sleep infinity" in compose_text
		assert "test -d /workspace" in compose_text
		assert 'test -f \\"/workspace/scripts/run_validation_repo_checks.sh\\"' in compose_text
		assert 'command -v \\"scripts/run_validation_repo_checks.sh\\"' in compose_text
		assert "condition: service_healthy" not in compose_text

		dockerfile_text = (output_root / "Dockerfile.app").read_text(encoding="utf-8")
		assert "pip install --no-cache-dir pyyaml jsonschema jinja2" in dockerfile_text
		assert "flask" not in dockerfile_text

		family_text = (output_root / "tests" / "10_family_marker.sh").read_text(encoding="utf-8")
		assert "python-repo-checks family for demo-project" in family_text

		repo_check_text = (output_root / "tests" / "40_repo_checks.sh").read_text(encoding="utf-8")
		assert "REPO_CHECK_ENTRY" in repo_check_text
		assert "scripts/run_validation_repo_checks.sh" in repo_check_text

		graceful_text = (output_root / "tests" / "30_graceful_shutdown.sh").read_text(encoding="utf-8")
		assert "sleep 300" in graceful_text
		assert "--log-tail-lines" in graceful_text

		graceful_lib_text = (output_root / "tests" / "_lib" / "graceful_shutdown.py").read_text(encoding="utf-8")
		assert "bounded_compose_logs_tail" in graceful_lib_text
		assert "timeout_waiting_for_shutdown" in graceful_lib_text

		import_audit_text = (output_root / "tests" / "_lib" / "import_audit.py").read_text(encoding="utf-8")
		assert "subprocess.run" in import_audit_text
		assert "sys.executable" in import_audit_text
		assert "importlib.import_module" in import_audit_text

		tap_text = (output_root / "tests" / "90_tap_report.sh").read_text(encoding="utf-8")
		assert "echo \"1..${TAP_PLAN}\"" in tap_text
		assert "tap_report family=python-repo-checks stable_summary=1" in tap_text


def _simulate_healthcheck(compose_path: Path, workspace_root: Path) -> int:
	# Reproduce what docker runs: compose interpolates `$$` down to `$`, then
	# the CMD-SHELL string executes under /bin/sh. `/workspace` is relocated
	# to a temp dir so the check runs outside a container.
	compose_doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
	healthcheck_test = compose_doc["services"]["app"]["healthcheck"]["test"]
	assert healthcheck_test[0] == "CMD-SHELL"
	shell_line = healthcheck_test[1].replace("$$", "$").replace("/workspace", str(workspace_root))
	return subprocess.run(["/bin/sh", "-c", shell_line], capture_output=True, cwd=workspace_root).returncode


def test_python_repo_checks_command_style_entry_renders_healthy_healthcheck() -> None:
	# Command validity is reported by 40_repo_checks.sh; readiness only verifies
	# that the workspace copied into the sleeping app container is available.
	for command_entry_value in (
		"sh scripts/run_validation_repo_checks.sh",
		'bash -lc "scripts/run_validation_repo_checks.sh"',
		"python -m pytest",
		"sh",
	):
		with tempfile.TemporaryDirectory(prefix="render-python-repo-checks-") as td:
			temp_root = Path(td)
			manifest_path = temp_root / "validate.yml"
			output_root = temp_root / "out"
			command_entry_payload = _manifest_payload()
			command_entry_payload["entry"] = command_entry_value
			_write_yaml(manifest_path, command_entry_payload)

			result = _run_renderer(manifest_path, output_root)
			assert result.returncode == 0, result.stderr

			compose_path = output_root / "docker-compose.test.yml"
			compose_text = compose_path.read_text(encoding="utf-8")
			if command_entry_value == "sh":
				assert 'command -v \\"sh\\"' in compose_text
			else:
				assert "test -d /workspace" in compose_text

			workspace_root = temp_root / "workspace"
			workspace_root.mkdir()
			assert _simulate_healthcheck(compose_path, workspace_root) == 0


def test_python_repo_checks_path_style_entry_healthcheck_still_gates_on_script() -> None:
	for path_entry_value in (
		"scripts/run_validation_repo_checks.sh",
		r"scripts\run_validation_repo_checks.sh",
		" scripts/run_validation_repo_checks.sh ",
	):
		with tempfile.TemporaryDirectory(prefix="render-python-repo-checks-") as td:
			temp_root = Path(td)
			manifest_path = temp_root / "validate.yml"
			output_root = temp_root / "out"
			path_entry_payload = _manifest_payload()
			path_entry_payload["entry"] = path_entry_value
			_write_yaml(manifest_path, path_entry_payload)

			result = _run_renderer(manifest_path, output_root)
			assert result.returncode == 0, result.stderr

			compose_path = output_root / "docker-compose.test.yml"
			compose_text = compose_path.read_text(encoding="utf-8")
			if "\\" in path_entry_value:
				assert r"scripts\\run_validation_repo_checks.sh" in compose_text
			workspace_root = temp_root / "workspace"
			workspace_root.mkdir()
			path_entry_file = workspace_root / path_entry_value.strip()
			path_entry_file.parent.mkdir(parents=True, exist_ok=True)
			assert _simulate_healthcheck(compose_path, workspace_root) != 0
			path_entry_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
			assert _simulate_healthcheck(compose_path, workspace_root) == 0


def main() -> int:
	tests = [func for name, func in sorted(globals().items()) if name.startswith("test_")]
	failures = 0
	for func in tests:
		name = func.__name__
		try:
			func()
			print(f"PASS {name}")
		except AssertionError as exc:
			failures += 1
			print(f"FAIL {name}: {exc}")
		except Exception as exc:  # pragma: no cover
			failures += 1
			print(f"ERROR {name}: {type(exc).__name__}: {exc}")
	return 1 if failures else 0


if __name__ == "__main__":
	raise SystemExit(main())

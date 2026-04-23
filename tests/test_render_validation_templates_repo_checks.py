#!/usr/bin/env python3
"""Regression tests for python-mongo-repo-checks validation harness templates."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_validation_templates.py"
SCHEMA_PATH = REPO_ROOT / "scripts" / "templates" / "slot_manifest.schema.json"
TEMPLATES_ROOT = REPO_ROOT / "workflow-templates" / "validation-harness"


def _write_yaml(path: Path, payload: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _manifest_payload() -> dict:
	return {
		"type": "python-mongo-repo-checks",
		"custom_tests": [
			"python3 tests/test_render_validation_templates.py",
			"python3 tests/test_validation_selftest_runner.py",
		],
		"slots": {
			"project_name": "demo-project",
			"canary_tools": ["bash", "python3", "jq", "curl"],
			"tap_plan": 6,
		},
	}


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


def test_repo_checks_family_avoids_flask_defaults() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-repo-checks-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr

		dockerfile_text = (output_root / "Dockerfile.app").read_text(encoding="utf-8")
		compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
		env_text = (output_root / "validate.env").read_text(encoding="utf-8")

		assert "python -m flask" not in dockerfile_text
		assert "FLASK_APP" not in dockerfile_text
		assert "python -m flask" not in compose_text
		assert "FLASK_APP" not in compose_text
		assert 'APP_URL=""' in env_text


def test_repo_checks_family_renders_repo_check_runner_and_passes_lint() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-repo-checks-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr

		repo_checks_text = (output_root / "tests" / "40_repo_checks.sh").read_text(encoding="utf-8")
		assert "CUSTOM_TESTS" in repo_checks_text
		assert 'payload.split("&&")' in repo_checks_text
		assert 'docker compose -f "${COMPOSE_FILE}" exec -T' in repo_checks_text

		lint_result = subprocess.run(
			["python3", str(REPO_ROOT / "scripts" / "validation_lint.py"), str(output_root)],
			text=True,
			capture_output=True,
			env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		)
		assert lint_result.returncode == 0, f"lint failed: {lint_result.stdout}\n{lint_result.stderr}"


def main() -> int:
	test_repo_checks_family_avoids_flask_defaults()
	test_repo_checks_family_renders_repo_check_runner_and_passes_lint()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

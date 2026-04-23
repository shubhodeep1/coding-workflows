#!/usr/bin/env python3
"""Family-specific regression tests for repo-local-tooling validation templates."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_validation_templates.py"
SCHEMA_PATH = REPO_ROOT / "scripts" / "templates" / "slot_manifest.schema.json"
TEMPLATES_ROOT = REPO_ROOT / "workflow-templates" / "validation-harness"
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "validation_harness" / "repo_local_tooling"


def _manifest_payload() -> dict:
	return {
		"type": "repo-local-tooling",
		"entry": "scripts/render_validation_templates.py",
		"slots": {
			"project_name": "demo-project",
			"canary_tools": ["curl", "jq", "python3"],
			"tap_plan": 5,
		},
	}


def _write_yaml(path: Path, payload: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _run_renderer(manifest_path: Path, output_root: Path) -> subprocess.CompletedProcess[str]:
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
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
	)


def _snapshot_directory(root: Path) -> tuple[list[str], dict[str, int]]:
	files: list[str] = []
	modes: dict[str, int] = {}
	for path in sorted(root.rglob("*")):
		if path.is_dir():
			continue
		rel = path.relative_to(root).as_posix()
		if "__pycache__" in path.parts or rel.endswith((".pyc", ".pyo", ".pyd")):
			continue
		files.append(rel)
		modes[rel] = path.stat().st_mode & 0o777
	return files, modes


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


def test_repo_local_tooling_golden_output_matches_fixture_tree() -> None:
	with tempfile.TemporaryDirectory(prefix="render-repo-local-tooling-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr
		_assert_fixture_match(output_root)


def test_repo_local_tooling_invariants_regression_guards() -> None:
	with tempfile.TemporaryDirectory(prefix="render-repo-local-tooling-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr

		expected_files = [
			output_root / "Dockerfile.app",
			output_root / "docker-compose.test.yml",
			output_root / "validate.env",
			output_root / "_lib" / "graceful_shutdown.sh",
			output_root / "_lib" / "tap_helpers.sh",
			output_root / "tests" / "00_canary.sh",
			output_root / "tests" / "10_family_marker.sh",
			output_root / "tests" / "20_import_audit.sh",
			output_root / "tests" / "25_render_validation_templates.sh",
			output_root / "tests" / "30_hardhat_test.sh",
			output_root / "tests" / "50_tap_report.sh",
			output_root / "tests" / "_lib" / "import_audit.py",
		]
		for expected in expected_files:
			assert expected.exists(), f"missing rendered file: {expected}"

		compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
		assert "dockerfile: out/Dockerfile.app" in compose_text
		assert "APP_URL: \"${APP_URL:-}\"" in compose_text
		assert "python -m flask" not in compose_text
		assert "FLASK_APP" not in compose_text

		env_text = (output_root / "validate.env").read_text(encoding="utf-8")
		assert 'APP_URL=""' in env_text
		for line in env_text.splitlines():
			if not line.strip() or line.startswith("#"):
				continue
			assert '="' in line and line.endswith('"'), f"validate.env value must be double-quoted: {line}"

		dockerfile_text = (output_root / "Dockerfile.app").read_text(encoding="utf-8")
		assert "pyyaml jsonschema jinja2 pytest" in dockerfile_text
		assert "python -m flask" not in dockerfile_text

		render_test = (output_root / "tests" / "25_render_validation_templates.sh").read_text(encoding="utf-8")
		assert "scripts/render_validation_templates.py" in render_test
		assert "python -m flask" not in render_test

		hardhat_test = (output_root / "tests" / "30_hardhat_test.sh").read_text(encoding="utf-8")
		assert "tests/test_validation_selftest_runner.py" in hardhat_test
		assert "tail -n" in hardhat_test
		assert '. "${ROOT_DIR}/_lib/graceful_shutdown.sh"' in hardhat_test
		assert "graceful_shutdown" in hardhat_test

		import_runner = (output_root / "tests" / "20_import_audit.sh").read_text(encoding="utf-8")
		import_helper = (output_root / "tests" / "_lib" / "import_audit.py").read_text(encoding="utf-8")
		assert "_lib/import_audit.py" in import_runner
		assert "subprocess.run" in import_helper
		assert "sys.executable" in import_helper
		assert "importlib.import_module" in import_helper

		family_marker_text = (output_root / "tests" / "10_family_marker.sh").read_text(encoding="utf-8")
		assert "repo-local-tooling family for demo-project" in family_marker_text

		tap_text = (output_root / "tests" / "50_tap_report.sh").read_text(encoding="utf-8")
		assert "stable_summary=1" in tap_text
		assert "tap summary slot" in tap_text
		assert not (output_root / "tests" / "90_tap_report.sh").exists()

		lint_result = subprocess.run(
			["python3", str(REPO_ROOT / "scripts" / "validation_lint.py"), str(output_root)],
			text=True,
			capture_output=True,
			env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		)
		assert lint_result.returncode == 0, f"lint failed: {lint_result.stdout}\n{lint_result.stderr}"


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

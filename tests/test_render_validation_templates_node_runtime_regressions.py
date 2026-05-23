#!/usr/bin/env python3
"""Regression tests for node-runtime validation harness templates."""

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
		"type": "node-runtime",
		"entry": "package.json",
		"custom_tests": [
			"npm test",
			"npm run lint --if-present",
		],
		"skip_tests": [],
		"slots": {
			"project_name": "demo-project",
			"canary_tools": ["bash", "node", "npm", "npx", "jq"],
			"tap_plan": 5,
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


def _run_validation_lint(output_root: Path) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["python3", str(REPO_ROOT / "scripts" / "validation_lint.py"), str(output_root)],
		text=True,
		capture_output=True,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
	)


def test_node_runtime_family_renders_expected_files_and_passes_lint() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-node-runtime-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr

		expected_files = [
			output_root / "Dockerfile.app",
			output_root / "_lib" / "tap_helpers.sh",
			output_root / "docker-compose.test.yml",
			output_root / "tests" / "00_canary.sh",
			output_root / "tests" / "10_family_marker.sh",
			output_root / "tests" / "20_import_audit.sh",
			output_root / "tests" / "30_graceful_shutdown.sh",
			output_root / "tests" / "40_repo_checks.sh",
			output_root / "tests" / "90_tap_report.sh",
			output_root / "tests" / "_lib" / "graceful_shutdown.py",
			output_root / "tests" / "_lib" / "import_audit.py",
			output_root / "validate.env",
		]
		for expected in expected_files:
			assert expected.exists(), f"missing rendered file: {expected}"

		assert not (output_root / "tests" / "20_rpc_probe.sh").exists()
		assert not (output_root / "tests" / "30_hardhat_test.sh").exists()
		assert not (output_root / "_lib" / "graceful_shutdown.sh").exists()

		lint_result = _run_validation_lint(output_root)
		assert lint_result.returncode == 0, f"lint failed: {lint_result.stdout}\n{lint_result.stderr}"


def test_node_runtime_compose_is_single_service_and_free_of_hardhat_rpc_state() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-node-runtime-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr

		compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
		env_text = (output_root / "validate.env").read_text(encoding="utf-8")
		parsed = yaml.safe_load(compose_text)

		assert sorted(parsed["services"].keys()) == ["app"]
		app_service = parsed["services"]["app"]
		assert app_service["command"] == ["/bin/sh", "-c", "sleep infinity"]
		assert app_service["build"]["dockerfile"] == "out/Dockerfile.app"
		assert app_service["env_file"] == ["validate.env"]
		assert app_service["environment"]["APP_SERVICE"] == "${APP_SERVICE:-app}"
		assert app_service["environment"]["APP_URL"] == "${APP_URL:-}"
		assert "CUSTOM_TESTS_JSON" in app_service["environment"]
		assert "SKIP_TESTS_JSON" in app_service["environment"]
		assert "RPC_URL" not in app_service["environment"]
		assert "HARDHAT_NETWORK" not in app_service["environment"]
		assert "HARDHAT_TEST_CMD" not in app_service["environment"]
		assert "anvil:" not in compose_text
		assert "foundry" not in compose_text.lower()

		assert 'CUSTOM_TESTS_JSON="' in env_text
		assert 'SKIP_TESTS_JSON="' in env_text
		assert 'RPC_URL=' not in env_text
		assert 'HARDHAT_NETWORK=' not in env_text
		assert 'HARDHAT_TEST_CMD=' not in env_text


def test_node_runtime_repo_check_runner_uses_node_for_json_parsing_and_bounded_tails() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-node-runtime-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr

		repo_checks_text = (output_root / "tests" / "40_repo_checks.sh").read_text(encoding="utf-8")
		assert "node - <<'JS'" in repo_checks_text
		assert "JSON.parse(payload)" in repo_checks_text
		assert "LIST_ENV_NAME" in repo_checks_text
		assert "json.loads(payload)" not in repo_checks_text
		assert "python3 - <<\"PY\"" not in repo_checks_text
		assert 'tail -n "${TAIL_LINES}"' in repo_checks_text
		assert '-e REPO_CHECK_CMD' in repo_checks_text


def test_node_runtime_env_overrides_flow_into_validate_env_and_compose() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-node-runtime-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		payload = _manifest_payload()
		payload["env_overrides"] = {
			"APP_SERVICE": "custom-app",
			"APP_URL": "https://example.invalid/app",
			"NODE_ENV": "test",
			"NPM_CONFIG_CACHE": "${HOME}/.npm-cache",
		}
		_write_yaml(manifest_path, payload)

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr

		env_text = (output_root / "validate.env").read_text(encoding="utf-8")
		compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")

		assert 'APP_SERVICE="app"' in env_text, env_text
		assert 'APP_SERVICE="custom-app"' in env_text, env_text
		assert env_text.find('APP_SERVICE="custom-app"') > env_text.find('APP_SERVICE="app"'), env_text
		assert 'APP_URL="https://example.invalid/app"' in env_text, env_text
		assert 'NODE_ENV="test"' in env_text, env_text
		assert 'NPM_CONFIG_CACHE="\\${HOME}/.npm-cache"' in env_text, env_text

		assert 'APP_SERVICE: "custom-app"' in compose_text, compose_text
		assert 'APP_URL: "https://example.invalid/app"' in compose_text, compose_text
		assert 'NODE_ENV: "test"' in compose_text, compose_text
		assert 'NPM_CONFIG_CACHE: "$${HOME}/.npm-cache"' in compose_text, compose_text
		assert '${APP_SERVICE:-app}' not in compose_text, compose_text
		assert '${APP_URL:-}' not in compose_text, compose_text


def test_node_runtime_example_manifest_is_schema_valid_and_renderable() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-node-runtime-") as td:
		temp_root = Path(td)
		output_root = temp_root / "out"
		example_manifest = REPO_ROOT / "examples" / "validation-fixtures" / "node-runtime.yml"
		payload = yaml.safe_load(example_manifest.read_text(encoding="utf-8"))

		assert payload["type"] == "node-runtime"
		assert payload["entry"] == "package.json"
		assert isinstance(payload["custom_tests"], list) and payload["custom_tests"]
		assert payload["skip_tests"] == []

		result = _run_renderer(example_manifest, output_root)
		assert result.returncode == 0, result.stderr
		assert not (output_root / "tests" / "20_rpc_probe.sh").exists()
		assert not (output_root / "tests" / "30_hardhat_test.sh").exists()

		lint_result = _run_validation_lint(output_root)
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

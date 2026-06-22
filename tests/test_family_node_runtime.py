#!/usr/bin/env python3
"""Family-specific regression tests for node-runtime validation templates."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_validation_templates.py"
SCHEMA_PATH = REPO_ROOT / "scripts" / "templates" / "slot_manifest.schema.json"
TEMPLATES_ROOT = REPO_ROOT / "workflow-templates" / "validation-harness"
EXAMPLE_MANIFEST = REPO_ROOT / "examples" / "validation-fixtures" / "node-runtime.yml"


def _manifest_payload() -> dict:
	return {
		"type": "node-runtime",
		"entry": "package.json",
		"custom_tests": [
			"npm test",
			"npm run lint --if-present",
		],
		"skip_tests": ["npm run lint --if-present"],
		"slots": {
			"project_name": "demo-project",
			"canary_tools": ["bash", "node", "npm", "npx", "jq"],
			"tap_plan": 4,
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
		timeout=300,
		env=env,
	)


def _run_validation_lint(output_root: Path) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["python3", str(REPO_ROOT / "scripts" / "validation_lint.py"), str(output_root)],
		text=True,
		capture_output=True,
		timeout=300,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
	)


def _read_env_values(path: Path) -> dict[str, str]:
	# Mirror scripts/validate_driver.sh:load_env_file (lines 63-70): strip a
	# single matched pair of surrounding quotes with NO backslash unescaping.
	# The previous ast.literal_eval model silently unescaped backslashes, so it
	# did NOT match the real driver — which is exactly why the CUSTOM_TESTS_JSON
	# double-encoding regression (hylifegroup.com run 27939731907) passed this
	# test while the in-container JSON.parse failed in production.
	values: dict[str, str] = {}
	for raw_line in path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, raw_value = line.split("=", 1)
		if (
			len(raw_value) >= 2
			and raw_value[0] == raw_value[-1]
			and raw_value[0] in ('"', "'")
		):
			raw_value = raw_value[1:-1]
		values[key] = raw_value
	return values


def test_node_runtime_scaffold_has_no_hardhat_assets_and_wires_custom_tests() -> None:
	with tempfile.TemporaryDirectory(prefix="render-node-runtime-family-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		payload = _manifest_payload()
		_write_yaml(manifest_path, payload)

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

		dockerfile_text = (output_root / "Dockerfile.app").read_text(encoding="utf-8")
		assert dockerfile_text.startswith("FROM node:"), dockerfile_text

		family_marker_text = (output_root / "tests" / "10_family_marker.sh").read_text(encoding="utf-8")
		assert "node-runtime family for demo-project" in family_marker_text

		env_values = _read_env_values(output_root / "validate.env")
		assert json.loads(env_values["CUSTOM_TESTS_JSON"]) == payload["custom_tests"]
		assert json.loads(env_values["SKIP_TESTS_JSON"]) == payload["skip_tests"]
		assert "RPC_URL" not in env_values
		assert "HARDHAT_NETWORK" not in env_values
		assert "HARDHAT_TEST_CMD" not in env_values

		compose_doc = yaml.safe_load((output_root / "docker-compose.test.yml").read_text(encoding="utf-8"))
		assert sorted(compose_doc["services"].keys()) == ["app"]
		app_service = compose_doc["services"]["app"]
		assert app_service["command"] == ["/bin/sh", "-c", "sleep infinity"]
		assert app_service["build"]["dockerfile"] == "out/Dockerfile.app"
		assert app_service["env_file"] == ["validate.env"]
		assert "CUSTOM_TESTS_JSON:-[" in app_service["environment"]["CUSTOM_TESTS_JSON"]
		assert "npm test" in app_service["environment"]["CUSTOM_TESTS_JSON"]
		assert "npm run lint --if-present" in app_service["environment"]["CUSTOM_TESTS_JSON"]
		assert "SKIP_TESTS_JSON:-[" in app_service["environment"]["SKIP_TESTS_JSON"]
		assert "npm run lint --if-present" in app_service["environment"]["SKIP_TESTS_JSON"]
		assert "RPC_URL" not in app_service["environment"]
		assert "HARDHAT_NETWORK" not in app_service["environment"]
		assert "HARDHAT_TEST_CMD" not in app_service["environment"]

		repo_checks_text = (output_root / "tests" / "40_repo_checks.sh").read_text(encoding="utf-8")
		assert "node - <<'JS'" in repo_checks_text
		assert "JSON.parse(payload)" in repo_checks_text
		assert "LIST_ENV_NAME" in repo_checks_text
		assert "REPO_CHECK_CMD" in repo_checks_text
		assert 'tail -n "${TAIL_LINES}"' in repo_checks_text
		assert "json.loads(payload)" not in repo_checks_text
		assert 'python3 - <<"PY"' not in repo_checks_text

		lint_result = _run_validation_lint(output_root)
		assert lint_result.returncode == 0, f"lint failed: {lint_result.stdout}\n{lint_result.stderr}"


def test_node_runtime_example_manifest_is_explicit_and_renderable() -> None:
	with tempfile.TemporaryDirectory(prefix="render-node-runtime-family-") as td:
		temp_root = Path(td)
		output_root = temp_root / "out"
		payload = yaml.safe_load(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))

		assert payload["type"] == "node-runtime"
		assert payload["entry"] == "package.json"
		assert isinstance(payload["custom_tests"], list) and payload["custom_tests"]
		assert isinstance(payload["skip_tests"], list)

		result = _run_renderer(EXAMPLE_MANIFEST, output_root)
		assert result.returncode == 0, result.stderr
		assert (output_root / "tests" / "40_repo_checks.sh").exists()
		assert not (output_root / "tests" / "20_rpc_probe.sh").exists()
		assert not (output_root / "tests" / "30_hardhat_test.sh").exists()
		assert not (output_root / "_lib" / "graceful_shutdown.sh").exists()

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

#!/usr/bin/env python3
"""Unit tests for scripts/render_validation_templates.py."""

from __future__ import annotations

import hashlib
import json
import os
import re
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


def _manifest_payload(manifest_type: str) -> dict:
	payload = {
		"type": manifest_type,
		"entry": "app.py",
		"port": 8000,
		"slots": {
			"project_name": "demo-project",
			"canary_tools": ["curl", "jq", "python3"],
			"tap_plan": 2,
		},
	}
	if manifest_type == "python-mongo-repo-checks":
		payload["custom_tests"] = [
			"python3 tests/test_render_validation_templates.py",
			"python3 tests/test_validation_selftest_runner.py",
		]
	if manifest_type == "node-runtime":
		payload["entry"] = "package.json"
		payload["custom_tests"] = [
			"npm test",
			"npm run lint --if-present",
		]
		payload["skip_tests"] = []
		payload["slots"]["canary_tools"] = ["bash", "node", "npm", "npx", "jq"]
	return payload


def _run_renderer(
	manifest_path: Path,
	output_root: Path,
	*,
	schema_path: Path = SCHEMA_PATH,
	templates_root: Path = TEMPLATES_ROOT,
) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		[
			"python3",
			str(SCRIPT_PATH),
			"--manifest",
			str(manifest_path),
			"--schema",
			str(schema_path),
			"--templates-root",
			str(templates_root),
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


def _directory_file_map(root: Path) -> dict[str, str]:
	return {
		path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
		for path in sorted(root.rglob("*"))
		if path.is_file()
	}


def test_renderer_happy_path_creates_expected_files() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload("python-mongo-flask"))

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, f"renderer failed: {result.stderr}"
		assert "Rendered" in result.stdout

		expected_files = [
			"Dockerfile.app",
			"_lib/tap_helpers.sh",
			"docker-compose.test.yml",
			"tests/00_canary.sh",
			"tests/10_family_marker.sh",
			"tests/11_http_smoke.sh",
			"tests/20_import_audit.sh",
			"tests/30_graceful_shutdown.sh",
			"tests/90_tap_report.sh",
			"tests/_lib/graceful_shutdown.py",
			"tests/_lib/http_smoke.py",
			"tests/_lib/import_audit.py",
		]
		files, _ = _snapshot_directory(output_root)
		assert files == expected_files

		canary_text = (output_root / "tests" / "00_canary.sh").read_text(encoding="utf-8")
		assert "CANARY_TOOLS=(" in canary_text
		assert "'curl'" in canary_text
		assert "'jq'" in canary_text
		assert "'python3'" in canary_text
		assert "tap_not_ok" in canary_text

		compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
		assert compose_text.count("init: true") == 2
		assert "/bin/sh -c" in compose_text
		assert "TEST_HOST_HEADER" in compose_text

		http_smoke_text = (output_root / "tests" / "11_http_smoke.sh").read_text(encoding="utf-8")
		assert "--host-header" in http_smoke_text
		assert "TEST_HOST_HEADER" in http_smoke_text

		family_text = (output_root / "tests" / "10_family_marker.sh").read_text(encoding="utf-8")
		assert "python-mongo-flask family for demo-project" in family_text

		lint_result = subprocess.run(
			["python3", str(REPO_ROOT / "scripts" / "validation_lint.py"), str(output_root)],
			text=True,
			capture_output=True,
			env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		)
		assert lint_result.returncode == 0, f"lint failed: {lint_result.stdout}\n{lint_result.stderr}"


def test_renderer_canary_tools_escaped_for_shell_safety() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		payload = _manifest_payload("python-mongo-flask")
		payload["slots"]["canary_tools"] = ["tool$(echo pwn)", "tool'quote"]
		_write_yaml(manifest_path, payload)

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr
		canary_text = (output_root / "tests" / "00_canary.sh").read_text(encoding="utf-8")
		assert "'tool$(echo pwn)'" in canary_text
		assert "'tool'\"'\"'quote'" in canary_text


def test_renderer_rejects_invalid_string_port() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		payload = _manifest_payload("python-mongo-flask")
		payload["port"] = "99999"
		_write_yaml(manifest_path, payload)

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode != 0
		assert "Manifest validation failed" in result.stderr
		assert "port" in result.stderr


def test_renderer_reports_output_root_creation_error_cleanly() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload("python-mongo-flask"))
		output_root.write_text("not-a-directory", encoding="utf-8")

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode != 0
		assert "ERROR: Failed creating output root" in result.stderr


def test_renderer_reports_parent_dir_creation_error_cleanly() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload("python-mongo-flask"))
		(output_root / "tests").parent.mkdir(parents=True, exist_ok=True)
		(output_root / "tests").write_text("not-a-directory", encoding="utf-8")

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode != 0
		assert "ERROR: Failed creating parent directory" in result.stderr


def test_renderer_rejects_oversized_manifest_and_schema_inputs() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		schema_path = temp_root / "schema.json"
		output_root = temp_root / "out"

		manifest_payload = _manifest_payload("python-mongo-flask")
		manifest_payload["slots"]["project_name"] = "x" * (2 * 1024 * 1024)
		_write_yaml(manifest_path, manifest_payload)
		schema_path.write_text(SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")

		manifest_result = _run_renderer(manifest_path, output_root, schema_path=schema_path)
		assert manifest_result.returncode != 0
		assert "Manifest file" in manifest_result.stderr
		assert "is too large" in manifest_result.stderr

		_write_yaml(manifest_path, _manifest_payload("python-mongo-flask"))
		large_schema_payload = {"blob": "x" * (2 * 1024 * 1024)}
		schema_path.write_text(json.dumps(large_schema_payload), encoding="utf-8")

		schema_result = _run_renderer(manifest_path, output_root, schema_path=schema_path)
		assert schema_result.returncode != 0
		assert "Schema file" in schema_result.stderr
		assert "is too large" in schema_result.stderr


def test_renderer_json_pointer_escapes_special_characters() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		payload = _manifest_payload("python-mongo-flask")
		payload["slots"] = {
			"project_name": "demo-project",
			"canary_tools": ["curl"],
			"bad/key~name": {},
		}
		_write_yaml(manifest_path, payload)

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode != 0
		assert "Manifest validation failed" in result.stderr
		assert "bad~1key~0name" in result.stderr


def test_renderer_fails_invalid_schema_with_actionable_error() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		payload = _manifest_payload("python-mongo-flask")
		del payload["slots"]["project_name"]
		_write_yaml(manifest_path, payload)

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode != 0
		assert "Manifest validation failed" in result.stderr
		assert "slots" in result.stderr
		assert "project_name" in result.stderr


def test_renderer_fails_unknown_family_type() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload("unknown-family"))

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode != 0
		assert "Manifest validation failed" not in result.stderr
		assert "Unknown manifest type" in result.stderr
		assert "node-runtime" in result.stderr
		assert "python-mongo-repo-checks" in result.stderr


def test_renderer_family_dispatch_routing() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload("node-hardhat-solidity"))

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, f"renderer failed: {result.stderr}"
		expected_files = [
			output_root / "Dockerfile.app",
			output_root / "docker-compose.test.yml",
			output_root / "validate.env",
			output_root / "_lib" / "graceful_shutdown.sh",
			output_root / "_lib" / "tap_helpers.sh",
			output_root / "tests" / "00_canary.sh",
			output_root / "tests" / "10_family_marker.sh",
			output_root / "tests" / "20_import_audit.sh",
			output_root / "tests" / "25_rpc_probe.sh",
			output_root / "tests" / "30_hardhat_test.sh",
			output_root / "tests" / "90_tap_report.sh",
			output_root / "tests" / "_lib" / "import_audit.py",
		]
		for expected in expected_files:
			assert expected.exists(), f"missing rendered file: {expected}"

		dockerfile_text = (output_root / "Dockerfile.app").read_text(encoding="utf-8")
		assert "ENV PATH=/root/.foundry/bin:${PATH}" in dockerfile_text

		env_text = (output_root / "validate.env").read_text(encoding="utf-8")
		for line in env_text.splitlines():
			if not line.strip() or line.startswith("#"):
				continue
			assert '="' in line and line.endswith('"'), f"validate.env value must be double-quoted: {line}"

		rpc_probe_text = (output_root / "tests" / "25_rpc_probe.sh").read_text(encoding="utf-8")
		assert 'type == "object"' in rpc_probe_text
		assert 'has("result") and (.result != null) and (.result | type == "string") and (.result | length > 0)' in rpc_probe_text

		import_audit_runner_text = (output_root / "tests" / "20_import_audit.sh").read_text(encoding="utf-8")
		import_audit_lib_text = (output_root / "tests" / "_lib" / "import_audit.py").read_text(encoding="utf-8")
		assert "python3" in import_audit_runner_text
		assert "_lib/import_audit.py" in import_audit_runner_text
		assert "subprocess.run" in import_audit_lib_text
		assert "sys.executable" in import_audit_lib_text
		assert "importlib.import_module" in import_audit_lib_text

		compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
		assert "dockerfile: out/Dockerfile.app" in compose_text

		hardhat_test_text = (output_root / "tests" / "30_hardhat_test.sh").read_text(encoding="utf-8")
		assert '. "${ROOT_DIR}/_lib/graceful_shutdown.sh"' in hardhat_test_text
		assert "npx hardhat test --network localhost" in hardhat_test_text

		family_marker_text = (output_root / "tests" / "10_family_marker.sh").read_text(encoding="utf-8")
		assert "node-hardhat-solidity family for demo-project" in family_marker_text
		assert not (output_root / "tests" / "10_http_smoke.sh").exists()

		lint_result = subprocess.run(
			["python3", str(REPO_ROOT / "scripts" / "validation_lint.py"), str(output_root)],
			text=True,
			capture_output=True,
			env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		)
		assert lint_result.returncode == 0, f"lint failed: {lint_result.stdout}\n{lint_result.stderr}"


def test_renderer_node_runtime_family_dispatch_routing() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload("node-runtime"))

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, f"renderer failed: {result.stderr}"
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

		family_marker_text = (output_root / "tests" / "10_family_marker.sh").read_text(encoding="utf-8")
		env_text = (output_root / "validate.env").read_text(encoding="utf-8")
		repo_checks_text = (output_root / "tests" / "40_repo_checks.sh").read_text(encoding="utf-8")
		assert "node-runtime family for demo-project" in family_marker_text
		# node-runtime renders raw (unquoted) JSON arrays so the in-container
		# JSON.parse in 40_repo_checks.sh succeeds (regression: run 27939731907).
		assert 'CUSTOM_TESTS_JSON=[' in env_text
		assert 'SKIP_TESTS_JSON=[' in env_text
		assert 'CUSTOM_TESTS_JSON="' not in env_text
		assert 'SKIP_TESTS_JSON="' not in env_text
		assert "node - <<'JS'" in repo_checks_text
		assert "json.loads(payload)" not in repo_checks_text

		lint_result = subprocess.run(
			["python3", str(REPO_ROOT / "scripts" / "validation_lint.py"), str(output_root)],
			text=True,
			capture_output=True,
			env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		)
		assert lint_result.returncode == 0, f"lint failed: {lint_result.stdout}\n{lint_result.stderr}"


def test_renderer_repo_checks_family_dispatch_routing() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload("python-mongo-repo-checks"))

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, f"renderer failed: {result.stderr}"
		expected_files = [
			output_root / "Dockerfile.app",
			output_root / "docker-compose.test.yml",
			output_root / "validate.env",
			output_root / "_lib" / "tap_helpers.sh",
			output_root / "tests" / "00_canary.sh",
			output_root / "tests" / "10_family_marker.sh",
			output_root / "tests" / "20_import_audit.sh",
			output_root / "tests" / "30_graceful_shutdown.sh",
			output_root / "tests" / "40_repo_checks.sh",
			output_root / "tests" / "90_tap_report.sh",
			output_root / "tests" / "_lib" / "graceful_shutdown.py",
			output_root / "tests" / "_lib" / "import_audit.py",
		]
		for expected in expected_files:
			assert expected.exists(), f"missing rendered file: {expected}"

		dockerfile_text = (output_root / "Dockerfile.app").read_text(encoding="utf-8")
		assert "python -m flask" not in dockerfile_text
		assert "FLASK_APP" not in dockerfile_text

		compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
		assert "python -m flask" not in compose_text
		assert "FLASK_APP" not in compose_text
		assert 'APP_URL: "${APP_URL:-}"' in compose_text
		assert "CUSTOM_TESTS_JSON" in compose_text
		assert "SKIP_TESTS_JSON" in compose_text

		env_text = (output_root / "validate.env").read_text(encoding="utf-8")
		assert 'APP_SERVICE="app"' in env_text
		assert 'APP_URL=""' in env_text
		assert "CUSTOM_TESTS_JSON='[" in env_text
		assert "SKIP_TESTS_JSON='[" in env_text

		repo_checks_text = (output_root / "tests" / "40_repo_checks.sh").read_text(encoding="utf-8")
		assert "CUSTOM_TESTS_JSON" in repo_checks_text
		assert "SKIP_TESTS_JSON" in repo_checks_text
		assert "json.loads(payload)" in repo_checks_text

		family_marker_text = (output_root / "tests" / "10_family_marker.sh").read_text(encoding="utf-8")
		assert "python-mongo-repo-checks family for demo-project" in family_marker_text

		lint_result = subprocess.run(
			["python3", str(REPO_ROOT / "scripts" / "validation_lint.py"), str(output_root)],
			text=True,
			capture_output=True,
			env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		)
		assert lint_result.returncode == 0, f"lint failed: {lint_result.stdout}\n{lint_result.stderr}"


def test_renderer_deterministic_output_for_same_manifest() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		out_a = temp_root / "first-parent" / "validation"
		out_b = temp_root / "second-parent" / "validation"
		_write_yaml(manifest_path, _manifest_payload("python-mongo-flask"))

		first = _run_renderer(manifest_path, out_a)
		second = _run_renderer(manifest_path, out_b)
		assert first.returncode == 0, first.stderr
		assert second.returncode == 0, second.stderr

		files_a, hash_a = _snapshot_directory(out_a)
		files_b, hash_b = _snapshot_directory(out_b)
		assert files_a == files_b
		assert hash_a == hash_b


def test_renderer_output_root_basename_only_affects_expected_files() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		out_a = temp_root / "out-a"
		out_b = temp_root / "out-b"
		_write_yaml(manifest_path, _manifest_payload("python-mongo-flask"))

		first = _run_renderer(manifest_path, out_a)
		second = _run_renderer(manifest_path, out_b)
		assert first.returncode == 0, first.stderr
		assert second.returncode == 0, second.stderr

		files_a = _directory_file_map(out_a)
		files_b = _directory_file_map(out_b)
		assert sorted(files_a) == sorted(files_b)

		expected_differing_files = {
			"docker-compose.test.yml",
			"tests/20_import_audit.sh",
			"tests/30_graceful_shutdown.sh",
			"tests/_lib/graceful_shutdown.py",
		}
		differing_files = {
			rel for rel in files_a if files_a[rel] != files_b[rel]
		}
		assert not differing_files - expected_differing_files, (
			f"unexpected basename-sensitive files: {differing_files - expected_differing_files}"
		)
		assert not expected_differing_files - differing_files, (
			f"expected basename-sensitive files missing: {expected_differing_files - differing_files}"
		)

		for rel_path in differing_files:
			assert "out-a" in files_a[rel_path], f"expected out-a reference in {rel_path}"
			assert "out-b" in files_b[rel_path], f"expected out-b reference in {rel_path}"
			assert (
				re.sub(r"(?<![A-Za-z0-9_])out-a(?=/)", "out-b", files_a[rel_path])
				== files_b[rel_path]
			)


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

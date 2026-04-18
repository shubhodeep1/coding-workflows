#!/usr/bin/env python3
"""Unit tests for scripts/render_validation_templates.py."""

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


def _write_yaml(path: Path, payload: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _manifest_payload(manifest_type: str) -> dict:
	return {
		"type": manifest_type,
		"entry": "app.py",
		"port": 8000,
		"slots": {
			"project_name": "demo-project",
			"canary_tools": ["curl", "jq", "python3"],
			"tap_plan": 2,
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


def test_renderer_happy_path_creates_expected_files() -> None:
	with tempfile.TemporaryDirectory(prefix="render-validation-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload("python-mongo-flask"))

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, f"renderer failed: {result.stderr}"
		assert "Rendered" in result.stdout

		shared_canary = output_root / "tests" / "00_canary.sh"
		shared_tap = output_root / "tests" / "90_tap_report.sh"
		family_marker = output_root / "tests" / "10_family_marker.sh"
		assert shared_canary.exists(), f"missing rendered file: {shared_canary}"
		assert shared_tap.exists(), f"missing rendered file: {shared_tap}"
		assert family_marker.exists(), f"missing rendered file: {family_marker}"

		canary_text = shared_canary.read_text(encoding="utf-8")
		assert "CANARY_TOOLS=(" in canary_text
		assert "'curl'" in canary_text
		assert "'jq'" in canary_text
		assert "'python3'" in canary_text
		assert 'for tool in "${CANARY_TOOLS[@]}"; do' in canary_text
		family_text = family_marker.read_text(encoding="utf-8")
		assert "python-mongo-flask family for demo-project" in family_text


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
			output_root / "tests" / "20_rpc_probe.sh",
			output_root / "tests" / "30_hardhat_test.sh",
			output_root / "tests" / "90_tap_report.sh",
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

		rpc_probe_text = (output_root / "tests" / "20_rpc_probe.sh").read_text(encoding="utf-8")
		assert 'type == "object"' in rpc_probe_text
		assert 'has("result") and (.result != null) and (.result | type == "string") and (.result | length > 0)' in rpc_probe_text

		compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
		assert "dockerfile: out/Dockerfile.app" in compose_text

		hardhat_test_text = (output_root / "tests" / "30_hardhat_test.sh").read_text(encoding="utf-8")
		assert '. "${ROOT_DIR}/_lib/graceful_shutdown.sh"' in hardhat_test_text
		assert "npx hardhat test --network localhost" in hardhat_test_text

		family_marker_text = (output_root / "tests" / "10_family_marker.sh").read_text(encoding="utf-8")
		assert "node-hardhat-solidity family for demo-project" in family_marker_text


def test_renderer_deterministic_output_for_same_manifest() -> None:
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

		files_a, hash_a = _snapshot_directory(out_a)
		files_b, hash_b = _snapshot_directory(out_b)
		assert files_a == files_b
		assert hash_a == hash_b


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

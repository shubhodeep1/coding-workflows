#!/usr/bin/env python3
"""Family-specific regression tests for python-mongo-flask validation templates."""

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
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "validation_harness" / "python_mongo_flask"


def _manifest_payload() -> dict:
	return {
		"type": "python-mongo-flask",
		"entry": "app.py",
		"port": 8000,
		"slots": {
			"project_name": "demo-project",
			"canary_tools": ["curl", "jq", "python3"],
			"tap_plan": 2,
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


def test_python_mongo_flask_golden_output_matches_fixture_tree() -> None:
	with tempfile.TemporaryDirectory(prefix="render-python-mongo-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr
		_assert_fixture_match(output_root)


def test_python_mongo_flask_invariants_regression_guards() -> None:
	with tempfile.TemporaryDirectory(prefix="render-python-mongo-") as td:
		temp_root = Path(td)
		manifest_path = temp_root / "validate.yml"
		output_root = temp_root / "out"
		_write_yaml(manifest_path, _manifest_payload())

		result = _run_renderer(manifest_path, output_root)
		assert result.returncode == 0, result.stderr

		compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
		assert compose_text.count("init: true") == 2
		assert "/bin/sh -c" in compose_text
		assert '- "CMD-SHELL"' in compose_text
		assert "TEST_HOST_HEADER" in compose_text
		assert "condition: service_healthy" in compose_text

		canary_text = (output_root / "tests" / "00_canary.sh").read_text(encoding="utf-8")
		assert "CANARY_TOOLS=(" in canary_text
		assert "'python3'" in canary_text
		assert "mongosh" not in canary_text
		assert "tap_not_ok" in canary_text

		http_smoke_shell = (output_root / "tests" / "11_http_smoke.sh").read_text(encoding="utf-8")
		http_smoke_py = (output_root / "tests" / "_lib" / "http_smoke.py").read_text(encoding="utf-8")
		assert "TEST_HOST_HEADER" in http_smoke_shell
		assert "--host-header" in http_smoke_shell
		assert 'headers={"Host": args.host_header' in http_smoke_py

		import_audit_text = (output_root / "tests" / "_lib" / "import_audit.py").read_text(encoding="utf-8")
		assert "subprocess.run" in import_audit_text
		assert "sys.executable" in import_audit_text
		assert '"-c", code' in import_audit_text

		shutdown_shell = (output_root / "tests" / "30_graceful_shutdown.sh").read_text(encoding="utf-8")
		shutdown_py = (output_root / "tests" / "_lib" / "graceful_shutdown.py").read_text(encoding="utf-8")
		assert "SHUTDOWN_TIMEOUT_SECONDS" in shutdown_shell
		assert "--log-tail-lines" in shutdown_shell
		assert 'COMPOSE_FILE="${COMPOSE_FILE:-validation/docker-compose.test.yml}"' in shutdown_shell
		assert 'docker compose -f "${COMPOSE_FILE}" exec -T app' in shutdown_shell
		assert 'compose_file,' in shutdown_py
		assert '"exec",' in shutdown_py
		assert '"logs", "--no-color", "app"' in shutdown_py
		assert "tail = bounded_compose_logs_tail" in shutdown_py
		assert "timeout_waiting_for_shutdown" in shutdown_py

		tap_text = (output_root / "tests" / "90_tap_report.sh").read_text(encoding="utf-8")
		assert "echo \"1..${TAP_PLAN}\"" in tap_text
		assert "tap summary slot" in tap_text
		assert "stable_summary=1" in tap_text


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

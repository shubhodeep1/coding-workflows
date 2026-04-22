#!/usr/bin/env python3
"""Contract tests for scripts/validation_selftest_matrix.py."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import importlib.util
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "validation_selftest_matrix.py"
MATRIX_MODULE_SPEC = importlib.util.spec_from_file_location("validation_selftest_matrix", SCRIPT_PATH)
assert MATRIX_MODULE_SPEC is not None and MATRIX_MODULE_SPEC.loader is not None
MATRIX_MODULE = importlib.util.module_from_spec(MATRIX_MODULE_SPEC)
MATRIX_MODULE_SPEC.loader.exec_module(MATRIX_MODULE)


def _write_yaml(path: Path, payload: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _fixture_manifest(family: str, project_name: str) -> dict:
	if family == "python-mongo-flask":
		return {
			"type": family,
			"entry": "app.py",
			"port": 8000,
			"slots": {
				"project_name": project_name,
				"canary_tools": ["curl", "jq", "python3"],
				"tap_plan": 2,
			},
		}
	if family == "node-hardhat-solidity":
		return {
			"type": family,
			"entry": "hardhat.config.ts",
			"port": 8545,
			"slots": {
				"project_name": project_name,
				"canary_tools": ["curl", "jq", "node", "npx", "forge", "cast"],
				"tap_plan": 3,
			},
		}
	raise AssertionError(f"unsupported family fixture: {family}")


def _run_matrix(
	work_root: Path,
	fixtures_root: Path,
	summary_path: Path,
	logs_root: Path,
	*,
	extra_env: dict[str, str] | None = None,
	skip_compose_config: bool = True,
) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	if extra_env:
		env.update(extra_env)
	command = [
		"python3",
		str(SCRIPT_PATH),
		"--repo-root",
		str(REPO_ROOT),
		"--fixtures-root",
		str(fixtures_root),
		"--summary-path",
		str(summary_path),
		"--log-dir",
		str(logs_root),
	]
	if skip_compose_config:
		command.append("--skip-compose-config")
	return subprocess.run(
		command,
		cwd=str(work_root),
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def _load_summary(summary_path: Path) -> dict:
	return json.loads(summary_path.read_text(encoding="utf-8"))


def _resolve_log_path(log_path: str) -> Path:
	path = Path(log_path)
	if path.is_absolute():
		return path
	return REPO_ROOT / path


def test_runner_passes_both_supported_family_fixtures() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"

		_write_yaml(fixtures_root / "python-mongo-flask.yml", _fixture_manifest("python-mongo-flask", "ci-python"))
		_write_yaml(fixtures_root / "node-hardhat-solidity.yml", _fixture_manifest("node-hardhat-solidity", "ci-node"))

		result = _run_matrix(work_root, fixtures_root, summary_path, logs_root)
		assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
		assert summary_path.exists()

		summary = _load_summary(summary_path)
		assert summary["schema_version"] == "1"
		assert summary["repo_root"] == "."
		assert summary["overall_status"] == "pass"
		assert summary["totals"] == {"fixtures": 2, "passed": 2, "failed": 0}
		assert len(summary["fixtures"]) == 2
		fixture_names = sorted(item["name"] for item in summary["fixtures"])
		assert fixture_names == ["node-hardhat-solidity.yml", "python-mongo-flask.yml"]

		for fixture in summary["fixtures"]:
			assert fixture["status"] == "pass"
			assert fixture["stages"]["render"]["status"] == "pass"
			assert fixture["stages"]["lint"]["status"] == "pass"
			assert fixture["stages"]["sanity"]["status"] == "pass"
			assert "render" in fixture["log_paths"]
			assert "lint" in fixture["log_paths"]
			assert "sanity" in fixture["log_paths"]
			for log_rel in fixture["log_paths"].values():
				log_path = _resolve_log_path(log_rel)
				assert log_path.exists(), f"missing stage log {log_rel}"


def test_runner_surfaces_fixture_stage_failure_in_summary() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"

		_write_yaml(fixtures_root / "python-mongo-flask.yml", _fixture_manifest("python-mongo-flask", "ci-python"))
		broken_manifest = _fixture_manifest("node-hardhat-solidity", "broken-node")
		broken_manifest["type"] = "unknown-family"
		_write_yaml(fixtures_root / "broken-node.yml", broken_manifest)

		result = _run_matrix(work_root, fixtures_root, summary_path, logs_root)
		assert result.returncode == 1
		assert summary_path.exists()

		summary = _load_summary(summary_path)
		assert summary["repo_root"] == "."
		assert summary["overall_status"] == "fail"
		assert summary["totals"]["fixtures"] == 2
		assert summary["totals"]["failed"] == 1
		failed = [fixture for fixture in summary["fixtures"] if fixture["status"] == "fail"]
		assert len(failed) == 1
		failed_fixture = failed[0]
		assert failed_fixture["name"] == "broken-node.yml"
		assert failed_fixture["stages"]["render"]["status"] == "fail"
		assert failed_fixture["stages"]["lint"]["status"] == "skipped"
		assert failed_fixture["stages"]["sanity"]["status"] == "skipped"
		assert "render" in failed_fixture["log_paths"]


def test_runner_fails_when_no_fixture_manifests_discovered() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "empty-fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"
		fixtures_root.mkdir(parents=True, exist_ok=True)

		result = _run_matrix(work_root, fixtures_root, summary_path, logs_root)
		assert result.returncode == 1
		assert "No fixture manifests found" in result.stderr
		assert summary_path.exists()

		summary = _load_summary(summary_path)
		assert summary["repo_root"] == "."
		assert summary["overall_status"] == "fail"
		assert summary["totals"] == {"fixtures": 0, "passed": 0, "failed": 0}
		assert summary["fixtures"] == []
		assert "error" in summary


def test_sanity_skips_compose_when_missing_by_default() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		output_root = work_root / "rendered"
		fixture_log_dir = work_root / "logs" / "fixture"
		shell_file = output_root / "tests" / "00_canary.sh"
		python_file = output_root / "tests" / "_lib" / "helper.py"

		shell_file.parent.mkdir(parents=True, exist_ok=True)
		python_file.parent.mkdir(parents=True, exist_ok=True)
		shell_file.write_text("#!/usr/bin/env bash\nset -euo pipefail\necho ok\n", encoding="utf-8")
		python_file.write_text("value = 1\n", encoding="utf-8")

		sanity = MATRIX_MODULE._stage_sanity(REPO_ROOT, output_root, fixture_log_dir, skip_compose_config=False)
		assert sanity["status"] == "pass"
		compose_check = next((check for check in sanity.get("checks", []) if check["name"] == "docker_compose_config"), None)
		assert compose_check is not None
		assert compose_check["status"] == "skipped"
		assert compose_check["reason"] == "no docker-compose.test.yml in output"

def main() -> int:
	test_runner_passes_both_supported_family_fixtures()
	test_runner_surfaces_fixture_stage_failure_in_summary()
	test_runner_fails_when_no_fixture_manifests_discovered()
	test_sanity_skips_compose_when_missing_by_default()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

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


def _write_fixture_repo(fixtures_root: Path, fixture_name: str, manifest: dict) -> Path:
	fixture_root = fixtures_root / fixture_name
	_write_yaml(fixture_root / ".ai" / "validate.yml", manifest)
	if manifest["type"] == "python-mongo-flask":
		(fixture_root / "app.py").write_text(
			"from flask import Flask, jsonify\n"
			"app = Flask(__name__)\n"
			"@app.get('/health')\n"
			"def health():\n"
			"\treturn jsonify({'status': 'ok'})\n",
			encoding="utf-8",
		)
		(fixture_root / "requirements.txt").write_text("flask==3.0.3\npymongo==4.8.0\n", encoding="utf-8")
	elif manifest["type"] == "node-hardhat-solidity":
		(fixture_root / "package.json").write_text(
			json.dumps(
				{
					"name": fixture_name,
					"private": True,
					"version": "1.0.0",
					"scripts": {
						"test": "hardhat test --network localhost",
					},
					"devDependencies": {
						"hardhat": "^2.22.15",
						"@nomicfoundation/hardhat-toolbox": "^5.0.0",
					},
				},
				indent=2,
			)
			+ "\n",
			encoding="utf-8",
		)
		(fixture_root / "hardhat.config.ts").write_text(
			"import { HardhatUserConfig } from \"hardhat/config\";\n"
			"import \"@nomicfoundation/hardhat-toolbox\";\n"
			"const config: HardhatUserConfig = {\n"
			"\tsolidity: \"0.8.24\",\n"
			"\tnetworks: {\n"
			"\t\tlocalhost: {\n"
			"\t\t\turl: process.env.RPC_URL || \"http://127.0.0.1:8545\",\n"
			"\t\t},\n"
			"\t},\n"
			"};\n"
			"export default config;\n",
			encoding="utf-8",
		)
		contracts_dir = fixture_root / "contracts"
		contracts_dir.mkdir(parents=True, exist_ok=True)
		(contracts_dir / "Counter.sol").write_text(
			"// SPDX-License-Identifier: MIT\n"
			"pragma solidity ^0.8.24;\n"
			"contract Counter {\n"
			"\tuint256 public value;\n"
			"\tfunction increment() external { value += 1; }\n"
			"}\n",
			encoding="utf-8",
		)
		tests_dir = fixture_root / "test"
		tests_dir.mkdir(parents=True, exist_ok=True)
		(tests_dir / "counter.ts").write_text(
			"import { expect } from \"chai\";\n"
			"import { ethers } from \"hardhat\";\n"
			"describe(\"Counter\", function () {\n"
			"\tit(\"increments\", async function () {\n"
			"\t\tconst Counter = await ethers.getContractFactory(\"Counter\");\n"
			"\t\tconst counter = await Counter.deploy();\n"
			"\t\tawait counter.waitForDeployment();\n"
			"\t\tawait counter.increment();\n"
			"\t\texpect(await counter.value()).to.equal(1n);\n"
			"\t});\n"
			"});\n",
			encoding="utf-8",
		)
	return fixture_root


def _run_matrix(
	work_root: Path,
	fixtures_root: Path,
	summary_path: Path,
	logs_root: Path,
	*,
	extra_env: dict[str, str] | None = None,
	runtime_command: str | None = None,
	skip_compose_config: bool = False,
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
	if runtime_command is not None:
		command.extend(["--runtime-command", runtime_command])
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


def _assert_stage_logs_exist(fixture: dict) -> None:
	for stage_name in ("clone", "render", "lint", "runtime"):
		assert stage_name in fixture["log_paths"]
		log_path = _resolve_log_path(fixture["log_paths"][stage_name])
		assert log_path.exists(), f"missing stage log {stage_name}: {log_path}"


def test_runner_passes_two_supported_family_fixtures() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"

		_write_fixture_repo(
			fixtures_root,
			"python-mongo-flask",
			_fixture_manifest("python-mongo-flask", "ci-python"),
		)
		_write_fixture_repo(
			fixtures_root,
			"node-hardhat-solidity",
			_fixture_manifest("node-hardhat-solidity", "ci-node"),
		)

		result = _run_matrix(
			work_root,
			fixtures_root,
			summary_path,
			logs_root,
			runtime_command="python3 -c \"print('runtime ok')\"",
			skip_compose_config=True,
		)
		assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
		assert summary_path.exists()

		summary = _load_summary(summary_path)
		assert summary["schema_version"] == "1"
		assert summary["repo_root"] == "."
		assert summary["overall_status"] == "pass"
		assert summary["totals"] == {"fixtures": 2, "passed": 2, "failed": 0}
		fixture_names = sorted(item["name"] for item in summary["fixtures"])
		assert fixture_names == ["node-hardhat-solidity", "python-mongo-flask"]

		for fixture in summary["fixtures"]:
			assert fixture["status"] == "pass"
			assert fixture["stages"]["clone"]["status"] == "pass"
			assert fixture["stages"]["render"]["status"] == "pass"
			assert fixture["stages"]["lint"]["status"] == "pass"
			assert fixture["stages"]["runtime"]["status"] == "pass"
			assert fixture["manifest_path"].endswith(".ai/validate.yml")
			assert "workspace_path" in fixture
			assert "output_root" in fixture
			_assert_stage_logs_exist(fixture)


def test_runner_ignores_directories_without_manifest() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"

		broken_fixture_root = fixtures_root / "broken-node"
		broken_fixture_root.mkdir(parents=True, exist_ok=True)
		_write_fixture_repo(
			fixtures_root,
			"python-mongo-flask",
			_fixture_manifest("python-mongo-flask", "ci-python"),
		)

		result = _run_matrix(
			work_root,
			fixtures_root,
			summary_path,
			logs_root,
			runtime_command="python3 -c \"print('runtime ok')\"",
		)
		assert result.returncode == 0
		assert summary_path.exists()

		summary = _load_summary(summary_path)
		assert summary["overall_status"] == "pass"
		assert summary["totals"] == {"fixtures": 1, "passed": 1, "failed": 0}
		fixture_names = sorted(item["name"] for item in summary["fixtures"])
		assert fixture_names == ["python-mongo-flask"]


def test_runner_surfaces_render_failure_in_summary() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"

		_write_fixture_repo(
			fixtures_root,
			"python-mongo-flask",
			_fixture_manifest("python-mongo-flask", "ci-python"),
		)
		broken = _fixture_manifest("node-hardhat-solidity", "broken-node")
		broken["type"] = "unknown-family"
		_write_fixture_repo(fixtures_root, "broken-node", broken)

		result = _run_matrix(
			work_root,
			fixtures_root,
			summary_path,
			logs_root,
			runtime_command="python3 -c \"print('runtime ok')\"",
		)
		assert result.returncode == 1
		summary = _load_summary(summary_path)
		failed_fixture = next(item for item in summary["fixtures"] if item["name"] == "broken-node")
		assert failed_fixture["stages"]["clone"]["status"] == "pass"
		assert failed_fixture["stages"]["render"]["status"] == "fail"
		assert failed_fixture["stages"]["lint"]["status"] == "skipped"
		assert failed_fixture["stages"]["runtime"]["status"] == "skipped"
		assert "render" in failed_fixture["log_paths"]


def test_runner_surfaces_lint_failure_in_summary() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"

		lint_manifest = _fixture_manifest("python-mongo-flask", "ci-python")
		lint_manifest["slots"]["canary_tools"] = ["bash", "python3", "jq", "redis-cli"]
		_write_fixture_repo(
			fixtures_root,
			"python-mongo-flask",
			lint_manifest,
		)

		result = _run_matrix(
			work_root,
			fixtures_root,
			summary_path,
			logs_root,
			runtime_command="python3 -c \"print('runtime ok')\"",
		)
		assert result.returncode == 1
		summary = _load_summary(summary_path)
		fixture = summary["fixtures"][0]
		assert fixture["stages"]["clone"]["status"] == "pass"
		assert fixture["stages"]["render"]["status"] == "pass"
		assert fixture["stages"]["lint"]["status"] == "fail"
		assert fixture["stages"]["runtime"]["status"] == "skipped"


def test_runner_surfaces_runtime_failure_in_summary() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"

		_write_fixture_repo(
			fixtures_root,
			"python-mongo-flask",
			_fixture_manifest("python-mongo-flask", "ci-python"),
		)
		result = _run_matrix(
			work_root,
			fixtures_root,
			summary_path,
			logs_root,
			runtime_command="python3 -c \"import sys; sys.exit(7)\"",
		)
		assert result.returncode == 1
		summary = _load_summary(summary_path)
		fixture = summary["fixtures"][0]
		assert fixture["stages"]["clone"]["status"] == "pass"
		assert fixture["stages"]["render"]["status"] == "pass"
		assert fixture["stages"]["lint"]["status"] == "pass"
		assert fixture["stages"]["runtime"]["status"] == "fail"
		assert fixture["stages"]["runtime"]["exit_code"] == 7
		assert "runtime" in fixture["log_paths"]


def test_runner_fails_when_no_fixtures_discovered() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "empty-fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"
		fixtures_root.mkdir(parents=True, exist_ok=True)

		result = _run_matrix(work_root, fixtures_root, summary_path, logs_root)
		assert result.returncode == 1
		assert "No fixture workspaces or manifests found" in result.stderr
		assert summary_path.exists()

		summary = _load_summary(summary_path)
		assert summary["repo_root"] == "."
		assert summary["overall_status"] == "fail"
		assert summary["totals"] == {"fixtures": 0, "passed": 0, "failed": 0}
		assert summary["fixtures"] == []
		assert "error" in summary


def test_legacy_manifest_layout_still_supported_for_unit_tests() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "legacy-fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"
		fixtures_root.mkdir(parents=True, exist_ok=True)
		_write_yaml(fixtures_root / "python-mongo-flask.yml", _fixture_manifest("python-mongo-flask", "legacy-python"))

		result = _run_matrix(
			work_root,
			fixtures_root,
			summary_path,
			logs_root,
			runtime_command="python3 -c \"print('legacy runtime ok')\"",
		)
		assert result.returncode == 0
		summary = _load_summary(summary_path)
		assert summary["overall_status"] == "pass"
		fixture = summary["fixtures"][0]
		assert fixture["name"] == "python-mongo-flask.yml"
		assert fixture["manifest_path"].endswith("python-mongo-flask.yml")
		assert fixture["stages"]["clone"]["status"] == "pass"
		assert fixture["stages"]["runtime"]["status"] == "pass"


def test_runner_bootstraps_manifestless_marked_fixture() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-runner-") as td:
		work_root = Path(td)
		fixtures_root = work_root / "fixtures"
		summary_path = work_root / "summary" / "selftest.json"
		logs_root = work_root / "logs"

		bootstrap_fixture = fixtures_root / "python-repo-checks-bootstrap"
		bootstrap_fixture.mkdir(parents=True, exist_ok=True)
		(bootstrap_fixture / MATRIX_MODULE.SELFTEST_BOOTSTRAP_MARKER).write_text(
			"bootstrap_validation_manifest=true\n",
			encoding="utf-8",
		)

		result = _run_matrix(
			work_root,
			fixtures_root,
			summary_path,
			logs_root,
			runtime_command="python3 -c \"print('runtime ok')\"",
		)
		assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
		summary = _load_summary(summary_path)
		assert summary["overall_status"] == "pass"
		assert summary["totals"] == {"fixtures": 1, "passed": 1, "failed": 0}
		fixture = summary["fixtures"][0]
		assert fixture["name"] == "python-repo-checks-bootstrap"
		assert fixture["status"] == "pass"
		assert fixture["stages"]["clone"]["status"] == "pass"
		assert fixture["stages"]["render"]["status"] == "pass"
		assert fixture["stages"]["lint"]["status"] == "pass"
		assert fixture["stages"]["runtime"]["status"] == "pass"
		bootstrap_diagnostics = fixture["stages"]["clone"].get("bootstrap_diagnostics")
		assert bootstrap_diagnostics is not None
		assert "manifest_bootstrapped_from: examples/validation-fixtures/python-repo-checks.yml" in bootstrap_diagnostics
		assert "repo_check_entry_seeded: scripts/run_validation_repo_checks.sh" in bootstrap_diagnostics

		workspace_path = Path(fixture["workspace_path"])
		if not workspace_path.is_absolute():
			workspace_path = REPO_ROOT / workspace_path
		manifest_path = workspace_path / ".ai" / "validate.yml"
		assert manifest_path.is_file()
		expected_manifest = (REPO_ROOT / "examples" / "validation-fixtures" / "python-repo-checks.yml").read_text(
			encoding="utf-8"
		)
		assert manifest_path.read_text(encoding="utf-8") == expected_manifest
		seeded_entry = workspace_path / "scripts" / "run_validation_repo_checks.sh"
		assert seeded_entry.is_file()
		assert os.access(seeded_entry, os.X_OK)
		_assert_stage_logs_exist(fixture)


def main() -> int:
	test_runner_passes_two_supported_family_fixtures()
	test_runner_ignores_directories_without_manifest()
	test_runner_surfaces_render_failure_in_summary()
	test_runner_surfaces_lint_failure_in_summary()
	test_runner_surfaces_runtime_failure_in_summary()
	test_runner_fails_when_no_fixtures_discovered()
	test_legacy_manifest_layout_still_supported_for_unit_tests()
	test_runner_bootstraps_manifestless_marked_fixture()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())



from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "validation_lint.py"


def _write(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content, encoding="utf-8")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		["python3", str(SCRIPT_PATH), str(root), "--rules", "external-tool-dependencies"],
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def _write_selection_metadata(root: Path, payload: dict) -> None:
	_write(root / "_meta/test_selection.json", json.dumps(payload, sort_keys=True, indent=2) + "\n")


def test_external_tool_dependencies_fail_when_tool_not_installed() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-tools-") as td:
		root = Path(td)
		_write(root / "tests/00_canary.sh", 'CANARY_TOOLS="${CANARY_TOOLS:-curl jq forge}"\n')
		_write(root / "Dockerfile.app", "FROM node:20-bookworm\nRUN apt-get install -y curl jq\n")

		result = _run(root)

		assert result.returncode == 1
		assert "[external-tool-dependencies]" in result.stdout
		assert "forge" in result.stdout


def test_external_tool_dependencies_pass_when_install_evidence_exists() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-tools-") as td:
		root = Path(td)
		_write(root / "tests/00_canary.sh", 'CANARY_TOOLS="${CANARY_TOOLS:-curl jq forge cast}"\n')
		_write(
			root / "Dockerfile.app",
			"FROM node:20-bookworm\n"
			"RUN curl -L https://foundry.paradigm.xyz | bash && /root/.foundry/bin/foundryup\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_external_tool_dependencies_escape_hatch_suppresses_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-tools-") as td:
		root = Path(td)
		_write(
			root / "tests/00_canary.sh",
			'CANARY_TOOLS="${CANARY_TOOLS:-curl jq forge}" # validation-lint: allow external-tool-dependencies runtime sidecar injects tool\n',
		)
		_write(root / "Dockerfile.app", "FROM node:20-bookworm\nRUN apt-get install -y curl jq\n")

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_external_tool_dependencies_custom_tests_require_install_evidence() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-tools-") as td:
		root = Path(td)
		_write(root / "tests/00_canary.sh", 'CANARY_TOOLS="${CANARY_TOOLS:-curl jq python3}"\n')
		_write(root / "tests/40_custom_01.sh", "#!/usr/bin/env bash\nset -euo pipefail\nforge --version\n")
		_write(root / "Dockerfile.app", "FROM python:3.12-slim\nRUN apt-get update && apt-get install -y curl jq\n")
		_write_selection_metadata(
			root,
			{
				"schema_version": 1,
				"selected_test_outputs": [
					"tests/00_canary.sh",
					"tests/10_family_marker.sh",
					"tests/20_import_audit.sh",
					"tests/40_custom_01.sh",
					"tests/90_tap_report.sh",
				],
				"custom_tests": [
					{
						"index": 1,
						"output_rel_path": "tests/40_custom_01.sh",
						"command": "forge --version",
						"required_tools": ["forge"],
					}
				],
			},
		)

		result = _run(root)

		assert result.returncode == 1
		assert "[external-tool-dependencies]" in result.stdout
		assert "forge" in result.stdout


def test_external_tool_dependencies_custom_tests_pass_with_install_evidence() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-tools-") as td:
		root = Path(td)
		_write(root / "tests/00_canary.sh", 'CANARY_TOOLS="${CANARY_TOOLS:-curl jq python3}"\n')
		_write(root / "tests/40_custom_01.sh", "#!/usr/bin/env bash\nset -euo pipefail\nforge --version\n")
		_write(
			root / "Dockerfile.app",
			"FROM python:3.12-slim\n"
			"RUN curl -L https://foundry.paradigm.xyz | bash && /root/.foundry/bin/foundryup\n",
		)
		_write_selection_metadata(
			root,
			{
				"schema_version": 1,
				"selected_test_outputs": [
					"tests/00_canary.sh",
					"tests/10_family_marker.sh",
					"tests/20_import_audit.sh",
					"tests/40_custom_01.sh",
					"tests/90_tap_report.sh",
				],
				"custom_tests": [
					{
						"index": 1,
						"output_rel_path": "tests/40_custom_01.sh",
						"command": "forge --version",
						"required_tools": ["forge"],
					}
				],
			},
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_external_tool_dependencies_still_checks_canary_when_runtime_and_custom_tests_are_unselected() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-tools-") as td:
		root = Path(td)
		_write(root / "tests/00_canary.sh", 'CANARY_TOOLS="${CANARY_TOOLS:-curl jq forge}"\n')
		_write(root / "Dockerfile.app", "FROM python:3.12-slim\nRUN apt-get update && apt-get install -y curl jq\n")
		_write_selection_metadata(
			root,
			{
				"schema_version": 1,
				"selected_test_outputs": [
					"tests/00_canary.sh",
					"tests/10_family_marker.sh",
					"tests/20_import_audit.sh",
					"tests/90_tap_report.sh",
				],
				"custom_tests": [],
			},
		)

		result = _run(root)

		assert result.returncode == 1
		assert "[external-tool-dependencies]" in result.stdout
		assert "forge" in result.stdout

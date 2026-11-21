from __future__ import annotations

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

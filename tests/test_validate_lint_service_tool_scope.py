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


def _run(root: Path, rules: str) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		["python3", str(SCRIPT_PATH), str(root), "--rules", rules],
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def test_service_tool_scope_reports_missing_install() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-service-tool-") as td:
		root = Path(td)
		_write(
			root / "tests/00_canary.sh",
			'CANARY_TOOLS="${CANARY_TOOLS:-curl jq mongosh}"\n',
		)
		_write(root / "Dockerfile.app", "FROM python:3.12-slim\nRUN apt-get install -y curl jq\n")

		result = _run(root, "service-tool-scope")

		assert result.returncode == 1
		assert "[service-tool-scope]" in result.stdout
		assert "mongosh" in result.stdout
		assert "Hint:" in result.stdout


def test_service_tool_scope_accepts_alias_install() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-service-tool-") as td:
		root = Path(td)
		_write(
			root / "tests/00_canary.sh",
			'CANARY_TOOLS="${CANARY_TOOLS:-curl jq psql}"\n',
		)
		_write(root / "Dockerfile.app", "FROM python:3.12-slim\nRUN apt-get install -y curl jq postgresql-client\n")

		result = _run(root, "service-tool-scope")

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_service_tool_scope_escape_hatch_suppresses_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-service-tool-") as td:
		root = Path(td)
		_write(
			root / "tests/00_canary.sh",
			'CANARY_TOOLS="${CANARY_TOOLS:-curl jq mongosh}" # validation-lint: allow service-tool-scope app image intentionally prebuilt\n',
		)
		_write(root / "Dockerfile.app", "FROM python:3.12-slim\nRUN apt-get install -y curl jq\n")

		result = _run(root, "service-tool-scope")

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout

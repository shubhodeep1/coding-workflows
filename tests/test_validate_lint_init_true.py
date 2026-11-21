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
		["python3", str(SCRIPT_PATH), str(root), "--rules", "init-true-required"],
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def test_init_true_required_flags_missing_value() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-init-") as td:
		root = Path(td)
		_write(
			root / "docker-compose.test.yml",
			"services:\n"
			"  app:\n"
			"    image: python:3.12-slim\n"
			"  mongo:\n"
			"    init: true\n"
			"    image: mongo:7\n",
		)

		result = _run(root)

		assert result.returncode == 1
		assert "[init-true-required]" in result.stdout
		assert "missing init: true" in result.stdout


def test_init_true_required_passes_when_all_services_define_true() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-init-") as td:
		root = Path(td)
		_write(
			root / "docker-compose.test.yml",
			"services:\n"
			"  app:\n"
			"    init: true\n"
			"    image: python:3.12-slim\n"
			"  mongo:\n"
			"    init: true\n"
			"    image: mongo:7\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_init_true_required_escape_hatch_suppresses_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-init-") as td:
		root = Path(td)
		_write(
			root / "docker-compose.test.yml",
			"services:\n"
			"  app: # validation-lint: allow init-true-required sidecar exits immediately\n"
			"    image: python:3.12-slim\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout

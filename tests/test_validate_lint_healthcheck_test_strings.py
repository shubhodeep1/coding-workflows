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
		["python3", str(SCRIPT_PATH), str(root), "--rules", "healthcheck-test-strings"],
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def test_healthcheck_test_strings_rejects_unquoted_entries() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-healthcheck-") as td:
		root = Path(td)
		_write(
			root / "docker-compose.test.yml",
			"services:\n"
			"  app:\n"
			"    init: true\n"
			"    healthcheck:\n"
			"      test:\n"
			"        - CMD-SHELL\n"
			"        - curl -fsS http://127.0.0.1:8000/health || exit 1\n",
		)

		result = _run(root)

		assert result.returncode == 1
		assert "[healthcheck-test-strings]" in result.stdout
		assert "must be explicitly quoted" in result.stdout


def test_healthcheck_test_strings_accepts_quoted_entries() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-healthcheck-") as td:
		root = Path(td)
		_write(
			root / "docker-compose.test.yml",
			"services:\n"
			"  app:\n"
			"    init: true\n"
			"    healthcheck:\n"
			"      test:\n"
			"        - \"CMD-SHELL\"\n"
			"        - \"curl -fsS http://127.0.0.1:8000/health || exit 1\"\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_healthcheck_test_strings_escape_hatch_suppresses_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-healthcheck-") as td:
		root = Path(td)
		_write(
			root / "docker-compose.test.yml",
			"services:\n"
			"  app:\n"
			"    init: true\n"
			"    healthcheck:\n"
			"      test:\n"
			"        - CMD-SHELL # validation-lint: allow healthcheck-test-strings migrate to explicit quotes in follow-up\n"
			"        - \"curl -fsS http://127.0.0.1:8000/health || exit 1\"\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_healthcheck_test_strings_accepts_flow_style_quoted_entries() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-healthcheck-") as td:
		root = Path(td)
		_write(
			root / "docker-compose.test.yml",
			"services:\n"
			"  app:\n"
			"    init: true\n"
			"    healthcheck:\n"
			"      test: [\"CMD-SHELL\", \"curl -fsS http://127.0.0.1:8000/health || exit 1\"]\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout

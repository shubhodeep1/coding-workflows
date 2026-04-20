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
		["python3", str(SCRIPT_PATH), str(root), "--rules", "graceful-shutdown-hooks"],
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def test_graceful_shutdown_hooks_require_helper_wiring() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-graceful-") as td:
		root = Path(td)
		_write(
			root / "tests/30_graceful_shutdown.sh",
			"#!/usr/bin/env bash\n"
			"echo 1..1\n"
			"python3 ./tests/_lib/other.py\n",
		)
		_write(root / "tests/_lib/other.py", "print('noop')\n")

		result = _run(root)

		assert result.returncode == 1
		assert "[graceful-shutdown-hooks]" in result.stdout
		assert "_lib/graceful_shutdown.py" in result.stdout


def test_graceful_shutdown_hooks_pass_with_expected_python_hooks() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-graceful-") as td:
		root = Path(td)
		_write(
			root / "tests/30_graceful_shutdown.sh",
			"#!/usr/bin/env bash\n"
			"python3 ./tests/_lib/graceful_shutdown.py --timeout-seconds 10 --poll-seconds 1 --log-tail-lines 40\n",
		)
		_write(root / "tests/_lib/graceful_shutdown.py", "def main():\n\treturn 0\n")

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_graceful_shutdown_hooks_escape_hatch_suppresses_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-graceful-") as td:
		root = Path(td)
		_write(
			root / "tests/30_graceful_shutdown.sh",
			"#!/usr/bin/env bash # validation-lint: allow graceful-shutdown-hooks custom shutdown probe\n"
			"python3 ./tests/_lib/other.py\n",
		)
		_write(root / "tests/_lib/other.py", "print('noop')\n")
		_write(root / "tests/_lib/graceful_shutdown.py", "def main():\n\treturn 0\n")

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout

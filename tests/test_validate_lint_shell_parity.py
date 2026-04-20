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
		["python3", str(SCRIPT_PATH), str(root), "--rules", "shell-parity"],
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def test_shell_parity_rejects_login_shell() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-shell-") as td:
		root = Path(td)
		_write(root / "docker-compose.test.yml", "services:\n  app:\n    init: true\n")
		_write(
			root / "tests/00_canary.sh",
			"#!/usr/bin/env bash\n/bin/bash -lc 'echo hi'\n",
		)

		result = _run(root)

		assert result.returncode == 1
		assert "[shell-parity]" in result.stdout
		assert "-lc" in result.stdout


def test_shell_parity_accepts_sh_c_form() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-shell-") as td:
		root = Path(td)
		_write(
			root / "docker-compose.test.yml",
			"services:\n"
			"  app:\n"
			"    init: true\n"
			"    healthcheck:\n"
			"      test:\n"
			"        - CMD-SHELL\n"
			"        - /bin/sh -c \"echo ok\"\n",
		)
		_write(root / "tests/00_canary.sh", "#!/usr/bin/env bash\n/bin/sh -c 'echo hi'\n")

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_shell_parity_escape_hatch_suppresses_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-shell-") as td:
		root = Path(td)
		_write(root / "docker-compose.test.yml", "services:\n  app:\n    init: true\n")
		_write(
			root / "tests/00_canary.sh",
			"#!/usr/bin/env bash\n"
			"/bin/bash -lc 'echo hi' # validation-lint: allow shell-parity legacy shell constraint\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout

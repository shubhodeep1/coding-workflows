from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "validation_lint.py"


def _write(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content, encoding="utf-8")



def _make_exec(path: Path) -> None:
	path.chmod(path.stat().st_mode | stat.S_IXUSR)



def _run(root: Path) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		["python3", str(SCRIPT_PATH), str(root), "--rules", "test-script-prologue"],
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def test_test_script_prologue_rejects_missing_bash_shebang() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-prologue-") as td:
		root = Path(td)
		script = root / "tests/00_canary.sh"
		_write(script, "#!/bin/sh\nset -euo pipefail\n")
		_make_exec(script)

		result = _run(root)

		assert result.returncode == 1
		assert "[test-script-prologue]" in result.stdout
		assert "must start with '#!/usr/bin/env bash'" in result.stdout


def test_test_script_prologue_accepts_expected_prologue() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-prologue-") as td:
		root = Path(td)
		script = root / "tests/00_canary.sh"
		_write(script, "#!/usr/bin/env bash\nset -euo pipefail\n")
		_make_exec(script)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_test_script_prologue_escape_hatch_suppresses_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-prologue-") as td:
		root = Path(td)
		script = root / "tests/00_canary.sh"
		_write(
			script,
			"#!/usr/bin/env bash # validation-lint: allow test-script-prologue non-executable fixture for migration\n"
			"set -euo pipefail\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout

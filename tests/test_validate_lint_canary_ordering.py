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
		["python3", str(SCRIPT_PATH), str(root), "--rules", "canary-ordering"],
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def test_canary_ordering_rejects_non_canary_first_script() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-canary-order-") as td:
		root = Path(td)
		early = root / "tests/00_alpha.sh"
		canary = root / "tests/00_canary.sh"
		_write(early, "#!/usr/bin/env bash\nset -euo pipefail\n")
		_write(canary, "#!/usr/bin/env bash\nset -euo pipefail\n")
		_make_exec(early)
		_make_exec(canary)

		result = _run(root)

		assert result.returncode == 1
		assert "[canary-ordering]" in result.stdout
		assert "Only 00_canary.sh may use the 00 prefix" in result.stdout


def test_canary_ordering_accepts_ordered_scripts() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-canary-order-") as td:
		root = Path(td)
		canary = root / "tests/00_canary.sh"
		next_script = root / "tests/01_health.sh"
		_write(
			canary,
			"#!/usr/bin/env bash\n"
			"set -euo pipefail\n",
		)
		_write(next_script, "#!/usr/bin/env bash\nset -euo pipefail\n")
		_make_exec(canary)
		_make_exec(next_script)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_canary_ordering_escape_hatch_suppresses_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-canary-order-") as td:
		root = Path(td)
		early = root / "tests/00_alpha.sh"
		canary = root / "tests/00_canary.sh"
		_write(
			early,
			"#!/usr/bin/env bash # validation-lint: allow canary-ordering legacy script retained during migration\n"
			"set -euo pipefail # validation-lint: allow canary-ordering legacy script retained during migration\n",
		)
		_write(
			canary,
			"#!/usr/bin/env bash # validation-lint: allow canary-ordering paired legacy duplicate during migration\n"
			"set -euo pipefail\n",
		)
		_make_exec(early)
		_make_exec(canary)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout

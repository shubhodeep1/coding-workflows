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
		["python3", str(SCRIPT_PATH), str(root), "--rules", "bounded-tail-capture"],
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def test_bounded_tail_capture_requires_tail_hooks() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-tail-") as td:
		root = Path(td)
		_write(
			root / "tests/30_graceful_shutdown.sh",
			"#!/usr/bin/env bash\npython3 ./tests/_lib/graceful_shutdown.py --timeout-seconds 5\n",
		)
		_write(
			root / "tests/_lib/graceful_shutdown.py",
			"#!/usr/bin/env python3\n"
			"def main():\n"
			"\tprint('timeout')\n"
			"\treturn 1\n",
		)

		result = _run(root)

		assert result.returncode == 1
		assert "[bounded-tail-capture]" in result.stdout
		assert "bounded" in result.stdout


def test_bounded_tail_capture_passes_with_expected_hooks() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-tail-") as td:
		root = Path(td)
		_write(
			root / "tests/30_graceful_shutdown.sh",
			"#!/usr/bin/env bash\n"
			"python3 ./tests/_lib/graceful_shutdown.py --timeout-seconds 5 --poll-seconds 1 --log-tail-lines 40\n",
		)
		_write(
			root / "tests/_lib/graceful_shutdown.py",
			"#!/usr/bin/env python3\n"
			"def bounded_compose_logs_tail(lines):\n"
			"\treturn 'x'\n"
			"def main():\n"
			"\tpayload = {'log_tail': bounded_compose_logs_tail(40)}\n"
			"\treturn 0\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_bounded_tail_capture_escape_hatch_suppresses_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-tail-") as td:
		root = Path(td)
		_write(
			root / "tests/30_graceful_shutdown.sh",
			"#!/usr/bin/env bash\n"
			"python3 ./tests/_lib/graceful_shutdown.py --timeout-seconds 5 --poll-seconds 1 --log-tail-lines 40 # validation-lint: allow bounded-tail-capture external aggregator handles tails\n",
		)
		_write(
			root / "tests/_lib/graceful_shutdown.py",
			"#!/usr/bin/env python3 # validation-lint: allow bounded-tail-capture external aggregator handles tails\n"
			"def main():\n"
			"\treturn 0\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout

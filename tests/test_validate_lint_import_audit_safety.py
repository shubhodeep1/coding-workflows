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
		["python3", str(SCRIPT_PATH), str(root), "--rules", "import-audit-safety"],
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def _valid_runner() -> str:
	return (
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n"
		"python3 ./tests/_lib/import_audit.py\n"
	)


def _valid_audit() -> str:
	return (
		"#!/usr/bin/env python3\n"
		"import subprocess\n"
		"import sys\n"
		"def main():\n"
		"\tcode = \"import importlib; import sys; importlib.import_module('flask'); sys.stdout.write('ok')\"\n"
		"\tproc = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True, check=False)\n"
		"\treturn proc.returncode\n"
		"if __name__ == '__main__':\n"
		"\traise SystemExit(main())\n"
	)


def test_import_audit_safety_rejects_unsafe_dynamic_loading() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-import-") as td:
		root = Path(td)
		_write(root / "tests/20_import_audit.sh", _valid_runner())
		_write(
			root / "tests/_lib/import_audit.py",
			"#!/usr/bin/env python3\n"
			"import subprocess\n"
			"import sys\n"
			"import importlib.util\n"
			"spec = importlib.util.spec_from_file_location('x', '/tmp/x.py')\n"
			"def main():\n"
			"\tproc = subprocess.run([sys.executable, '-c', \"print('ok')\"], text=True, capture_output=True, check=False)\n"
			"\treturn proc.returncode\n"
		)

		result = _run(root)

		assert result.returncode == 1
		assert "[import-audit-safety]" in result.stdout
		assert "Dynamic module loading" in result.stdout


def test_import_audit_safety_passes_for_subprocess_isolated_probe() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-import-") as td:
		root = Path(td)
		_write(root / "tests/20_import_audit.sh", _valid_runner())
		_write(root / "tests/_lib/import_audit.py", _valid_audit())

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout


def test_import_audit_safety_escape_hatch_suppresses_violation() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-lint-import-") as td:
		root = Path(td)
		_write(root / "tests/20_import_audit.sh", _valid_runner())
		_write(
			root / "tests/_lib/import_audit.py",
			"#!/usr/bin/env python3 # validation-lint: allow import-audit-safety audited in external gate\n"
			"import subprocess\n"
			"import sys\n"
			"import importlib\n"
			"def main():\n"
			"\tcode = \"import importlib; import sys; importlib.import_module('flask'); sys.stdout.write('ok')\"\n"
			"\tproc = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True, check=False)\n"
			"\treturn proc.returncode\n",
		)

		result = _run(root)

		assert result.returncode == 0
		assert "validation-lint: OK" in result.stdout

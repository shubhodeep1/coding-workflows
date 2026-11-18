"""Contract test for SHELL-001: review_enable_auto_merge.sh uses an explicit
empty CDPATH assignment (`CDPATH='' cd`) instead of `CDPATH= cd` (SC1007)."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "review_enable_auto_merge.sh"

EXPECTED_SCRIPT_DIR_LINE = 'SCRIPT_DIR="$(CDPATH=\'\' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"'


def test_script_uses_explicit_empty_cdpath():
	text = SCRIPT.read_text(encoding="utf-8")
	assert "CDPATH='' cd" in text
	assert "CDPATH= cd" not in text
	assert EXPECTED_SCRIPT_DIR_LINE in text.splitlines()


def test_script_parses_with_bash_n():
	result = subprocess.run(
		["bash", "-n", str(SCRIPT)],
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr

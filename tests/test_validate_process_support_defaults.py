#!/usr/bin/env python3
"""Immutable-support defaults in scripts/validate_process.sh.

main's validate.yml runs this branch's checkout while the branch is being
validated and does not export SUPPORT_ROOT_DIR / SUPPORT_SCRIPTS_DIR /
SUPPORT_PROMPTS_DIR. Without defaults the script stopped at prompt resolution
("SUPPORT_PROMPTS_DIR is required", project #3965 runs 36232071002,
36235618528, 36239049270), and the Codex launcher and self-heal would stop on
the same variables.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS_PATH = REPO_ROOT / "scripts" / "validate_process.sh"
BLOCK_START = '_validate_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
BLOCK_END = 'VALIDATION_TRUSTED_DRIVER="${_validate_script_dir}/validate_driver.sh"\n'


def _defaults_block() -> str:
	text = VALIDATE_PROCESS_PATH.read_text(encoding="utf-8")
	start = text.index(BLOCK_START)
	end = text.index(BLOCK_END, start)
	return text[start:end]


def _run(tmp_path: Path, extra_env: dict[str, str]) -> dict[str, str]:
	support_root = tmp_path / "support"
	scripts_dir = support_root / "scripts"
	scripts_dir.mkdir(parents=True)
	script = scripts_dir / "validate_process_block.sh"
	script.write_text(
		"set -euo pipefail\n"
		+ _defaults_block()
		# A child process must see the same values (launcher, self-heal).
		+ 'bash -c \'printf "ROOT=%s\\nSCRIPTS=%s\\nPROMPTS=%s\\n" "$SUPPORT_ROOT_DIR" "$SUPPORT_SCRIPTS_DIR" "$SUPPORT_PROMPTS_DIR"\'\n',
		encoding="utf-8",
	)
	env = {key: value for key, value in os.environ.items() if not key.startswith("SUPPORT_")}
	env.update(extra_env)
	env.pop("BASH_ENV", None)
	proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, check=False)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line and not line.startswith("VALIDATE_SUPPORT_DEFAULTS"))
	values["_log"] = proc.stdout
	values["_support_root"] = str(support_root.resolve())
	return values


def test_missing_support_variables_default_to_the_running_tree(tmp_path: Path) -> None:
	values = _run(tmp_path, {})
	root = values["_support_root"]
	assert values["ROOT"] == root
	assert values["SCRIPTS"] == f"{root}/scripts"
	assert values["PROMPTS"] == f"{root}/prompts"
	assert "VALIDATE_SUPPORT_DEFAULTS applied=SUPPORT_ROOT_DIR SUPPORT_SCRIPTS_DIR SUPPORT_PROMPTS_DIR" in values["_log"]


def test_exported_support_variables_are_kept(tmp_path: Path) -> None:
	values = _run(tmp_path, {
		"SUPPORT_ROOT_DIR": "/immutable/root",
		"SUPPORT_SCRIPTS_DIR": "/immutable/root/scripts",
		"SUPPORT_PROMPTS_DIR": "/immutable/root/prompts",
	})
	assert values["ROOT"] == "/immutable/root"
	assert values["SCRIPTS"] == "/immutable/root/scripts"
	assert values["PROMPTS"] == "/immutable/root/prompts"
	assert "VALIDATE_SUPPORT_DEFAULTS" not in values["_log"]


def test_partially_exported_support_variables_fill_only_the_gaps(tmp_path: Path) -> None:
	values = _run(tmp_path, {"SUPPORT_ROOT_DIR": "/immutable/root"})
	assert values["ROOT"] == "/immutable/root"
	# The prompts default follows the resolved support root.
	assert values["PROMPTS"] == "/immutable/root/prompts"
	assert values["SCRIPTS"] == f"{values['_support_root']}/scripts"
	assert "applied=SUPPORT_SCRIPTS_DIR SUPPORT_PROMPTS_DIR" in values["_log"]


def main() -> int:
	import inspect
	import tempfile

	for name, func in sorted(globals().items()):
		if name.startswith("test_") and callable(func):
			if "tmp_path" in inspect.signature(func).parameters:
				with tempfile.TemporaryDirectory() as fixture_tmp_dir:
					func(tmp_path=Path(fixture_tmp_dir))
			else:
				func()
	print("OK: validate_process.sh immutable-support defaults")
	return 0


if __name__ == "__main__":
	sys.exit(main())

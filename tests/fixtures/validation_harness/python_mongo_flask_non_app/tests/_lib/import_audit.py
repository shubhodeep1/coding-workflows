#!/usr/bin/env python3
"""Import audit helper using subprocess isolation to avoid sys.modules pollution."""

from __future__ import annotations

import argparse
import subprocess
import sys


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Run isolated import audit")
	parser.add_argument(
		"--modules",
		nargs="+",
		default=["flask", "pymongo"],
		help="Module names that must import successfully in an isolated subprocess",
	)
	return parser


def run_isolated_import(module_name: str) -> tuple[int, str]:
	code = (
		"import importlib; "
		"import sys; "
		f"importlib.import_module({module_name!r}); "
		"sys.stdout.write('ok')"
	)
	proc = subprocess.run(
		[sys.executable, "-c", code],
		text=True,
		capture_output=True,
		check=False,
	)
	return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> int:
	args = build_parser().parse_args()
	for module_name in args.modules:
		rc, output = run_isolated_import(module_name)
		if rc != 0:
			print(
				f"not ok - isolated import failed module={module_name} rc={rc} output={output}",
				file=sys.stderr,
			)
			return 1
		print(f"ok - isolated import module={module_name}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

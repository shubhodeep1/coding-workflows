#!/usr/bin/env python3
"""Every `scripts/*.py` argparse parser must format its help text.

argparse treats help strings as `%`-format strings. A bare `%` (for example
`{%...%}`) makes `format_help()` raise on every Python version, and Python
3.14 also raises in `add_argument`, so the script fails on every call. That
stopped clarify, plan and review when `scripts/render_prompt.py` carried one
(#9192, #9238, #9247). This guard builds each script's parser and formats its
help, so the next bare `%` fails CI instead of production runs.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BUILD_PARSER_DEF_RE = re.compile(r"^def build_parser\(\)", re.M)
# CI runs scripts on Python 3.12 (`python-version: "3.12"` in ci.yml), and
# scripts may use 3.12-only syntax such as PEP 701 f-strings. On an older
# interpreter (e.g. the 3.11 pre-push gate container) a SyntaxError is an
# interpreter-version mismatch, not a help-string bug, so it is not reported
# there; on 3.12+ it still fails the guard.
SCRIPTS_MIN_PYTHON = (3, 12)


def _discover_build_parser_scripts(directory: Path) -> list[Path]:
	"""Return the `*.py` files in `directory` with a top-level `def build_parser()`, by text scan."""
	found: list[Path] = []
	for path in sorted(directory.glob("*.py")):
		try:
			text = path.read_text(encoding="utf-8")
		except (OSError, UnicodeDecodeError):
			continue
		if BUILD_PARSER_DEF_RE.search(text):
			found.append(path)
	return found


def _load_script_module(path: Path):
	scripts_dir = str(path.parent)
	if scripts_dir not in sys.path:
		sys.path.insert(0, scripts_dir)
	name = f"_bp_help_{path.stem}"
	spec = importlib.util.spec_from_file_location(name, path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	# Registered first so string annotations on dataclasses resolve.
	sys.modules[name] = module
	try:
		spec.loader.exec_module(module)
	finally:
		sys.modules.pop(name, None)
	return module


def _format_help_with_subparsers(parser: argparse.ArgumentParser) -> str:
	"""Format `parser`'s help and, recursively, every subcommand parser's help."""
	text = parser.format_help()
	seen = {id(parser)}
	pending = [parser]
	while pending:
		for action in pending.pop()._actions:
			if isinstance(action, argparse._SubParsersAction):
				for sub in action.choices.values():
					if id(sub) not in seen:
						seen.add(id(sub))
						try:
							sub.format_help()
						except Exception as exc:  # noqa: BLE001 - name the subcommand
							raise RuntimeError(f"{sub.prog}: {type(exc).__name__}: {exc}") from exc
						pending.append(sub)
	return text


def _build_parser_help_failures(paths: list[Path]) -> list[str]:
	failures: list[str] = []
	# Restored afterwards so `_load_script_module`'s sys.path insertions do
	# not leak into later imports in the same interpreter.
	saved_sys_path = list(sys.path)
	try:
		failures = _collect_build_parser_help_failures(paths)
	finally:
		sys.path[:] = saved_sys_path
	return failures


def _collect_build_parser_help_failures(paths: list[Path]) -> list[str]:
	failures: list[str] = []
	for path in paths:
		try:
			module = _load_script_module(path)
			parser = module.build_parser()
			text = _format_help_with_subparsers(parser)
		except SyntaxError as exc:
			if sys.version_info < SCRIPTS_MIN_PYTHON:
				continue
			failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
			continue
		except Exception as exc:  # noqa: BLE001 - every failure is reported
			failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
			continue
		if not isinstance(text, str) or not text.strip():
			failures.append(f"{path.name}: format_help() returned no text")
	return failures


def test_every_script_build_parser_help_formats() -> None:
	paths = _discover_build_parser_scripts(SCRIPTS_DIR)
	assert paths, "no scripts/*.py defines build_parser(); the discovery scan is broken"
	assert "render_prompt.py" in {path.name for path in paths}
	failures = _build_parser_help_failures(paths)
	assert failures == [], "argparse help text does not format:\n  " + "\n  ".join(failures)


def test_guard_flags_bare_percent_help_string() -> None:
	# The help text scripts/render_prompt.py carried at aa8aa9346a35.
	bad_script = (
		"import argparse\n\n\n"
		"def build_parser():\n"
		"\tparser = argparse.ArgumentParser()\n"
		"\tparser.add_argument(\n"
		"\t\t\"--skip-syntax-validation\",\n"
		"\t\taction=\"store_true\",\n"
		"\t\thelp=\"comment text with literal {{...}}/{%...%} tokens\",\n"
		"\t)\n"
		"\treturn parser\n"
	)
	saved_sys_path = list(sys.path)
	with tempfile.TemporaryDirectory(prefix="bp_help_guard_") as td:
		directory = Path(td)
		(directory / "bad_help.py").write_text(bad_script, encoding="utf-8")
		paths = _discover_build_parser_scripts(directory)
		assert [path.name for path in paths] == ["bad_help.py"]
		failures = _build_parser_help_failures(paths)
	# `_build_parser_help_failures` restores sys.path itself.
	assert sys.path == saved_sys_path, "the guard leaked sys.path entries"
	assert len(failures) == 1, failures
	assert failures[0].startswith("bad_help.py: "), failures


def test_guard_flags_bare_percent_in_subcommand_help_string() -> None:
	bad_script = (
		"import argparse\n\n\n"
		"def build_parser():\n"
		"\tparser = argparse.ArgumentParser(prog=\"tool\")\n"
		"\tsub = parser.add_subparsers(dest=\"command\", required=True)\n"
		"\trender = sub.add_parser(\"render\")\n"
		"\trender.add_argument(\"--flag\", action=\"store_true\", help=\"literal {%...%} token\")\n"
		"\treturn parser\n"
	)
	saved_sys_path = list(sys.path)
	with tempfile.TemporaryDirectory(prefix="bp_help_guard_sub_") as td:
		directory = Path(td)
		(directory / "bad_sub_help.py").write_text(bad_script, encoding="utf-8")
		failures = _build_parser_help_failures(_discover_build_parser_scripts(directory))
	assert sys.path == saved_sys_path, "the guard leaked sys.path entries"
	assert len(failures) == 1, failures
	# Before 3.14 the bad help string surfaces in format_help() and is wrapped
	# with the subcommand name; 3.14 rejects it earlier, inside add_argument()
	# during build_parser(), as `ValueError: badly formed help string`.
	assert failures[0].startswith(
		("bad_sub_help.py: RuntimeError: tool render: ", "bad_sub_help.py: ValueError: ")
	), failures
	if sys.version_info < (3, 14):
		assert failures[0].startswith("bad_sub_help.py: RuntimeError: tool render: "), failures


def main() -> int:
	test_every_script_build_parser_help_formats()
	test_guard_flags_bare_percent_help_string()
	test_guard_flags_bare_percent_in_subcommand_help_string()
	print("OK: every scripts/*.py build_parser() help text formats")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

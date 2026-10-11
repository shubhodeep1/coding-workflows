"""The generic Python validation families audit standard-library imports only.

binance-blessings#249 (validation run 38107224535): the python-mongo-repo-checks
import audit defaulted to `yaml`, `jsonschema` and `jinja2`, the harness
renderer's own dependencies, so a consumer that does not install PyYAML failed
`20_import_audit.sh` with `ModuleNotFoundError: No module named 'yaml'` while
its 685 repository tests passed. The audit must pass under a Python that has
no third-party packages at all.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import venv

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "workflow-templates" / "validation-harness"
GENERIC_PYTHON_FAMILIES = ("python-repo-checks", "python-mongo-repo-checks")


def _default_modules(path: Path) -> list[str]:
	tree = ast.parse(path.read_text(encoding="utf-8"))
	for node in ast.walk(tree):
		if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
			if any(isinstance(arg, ast.Constant) and arg.value == "--modules" for arg in node.args):
				for keyword in node.keywords:
					if keyword.arg == "default":
						return list(ast.literal_eval(keyword.value))
	raise AssertionError(f"no --modules default in {path}")


@pytest.fixture(scope="module")
def bare_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
	"""A Python with only the standard library, like a consumer image without PyYAML."""
	env_dir = tmp_path_factory.mktemp("bare-venv")
	venv.EnvBuilder(system_site_packages=False, with_pip=False).create(env_dir)
	python = env_dir / "bin" / "python"
	for module in ("yaml", "jinja2"):
		probe = subprocess.run([str(python), "-c", f"import {module}"], capture_output=True, text=True, check=False)
		assert probe.returncode != 0, f"the bare venv must not see {module}"
	return python


@pytest.mark.parametrize("family", GENERIC_PYTHON_FAMILIES)
def test_import_audit_defaults_are_standard_library(family: str) -> None:
	modules = _default_modules(TEMPLATES / family / "tests" / "_lib" / "import_audit.py.j2")
	assert modules
	assert all(name in sys.stdlib_module_names for name in modules), modules


@pytest.mark.parametrize("family", GENERIC_PYTHON_FAMILIES)
def test_import_audit_passes_without_third_party_packages(family: str, bare_python: Path) -> None:
	audit = TEMPLATES / family / "tests" / "_lib" / "import_audit.py.j2"
	result = subprocess.run(
		[str(bare_python), str(audit)],
		capture_output=True, text=True, check=False, timeout=60,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
	)
	assert result.returncode == 0, result.stderr
	assert "not ok" not in result.stdout + result.stderr

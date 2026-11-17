#!/usr/bin/env python3
"""Contract tests for scripts/check_workflow_script_refs.py.

render_prompt.py used to be exempt from the reference check through
OPTIONAL_REFS, so deleting or renaming it passed CI.  These tests make sure a
missing render_prompt.py is reported by every entry point of the checker.
"""

from __future__ import annotations

import os
import pathlib
import runpy
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check_workflow_script_refs.py"

NEUTRAL_WORKFLOW = "name: CI\non: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"
REFERENCING_WORKFLOW = (
	"name: CI\non: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
	"      - run: python3 scripts/render_prompt.py --help\n"
)
NEUTRAL_STAGE = "#!/usr/bin/env bash\necho staging\n"
REFERENCING_STAGE = "#!/usr/bin/env bash\ncp scripts/render_prompt.py \"${DEST}/\"\n"
MISSING_MESSAGE = "references scripts/render_prompt.py which does not exist"


def _make_tree(root, *, workflow_text, stage_text, with_render_prompt):
	root = pathlib.Path(root)
	(root / ".github" / "workflows").mkdir(parents=True)
	(root / "scripts").mkdir()
	(root / ".github" / "workflows" / "ci.yml").write_text(workflow_text, encoding="utf-8")
	(root / "scripts" / "stage_workflow_support.sh").write_text(stage_text, encoding="utf-8")
	if with_render_prompt:
		(root / "scripts" / "render_prompt.py").write_text("print('ok')\n", encoding="utf-8")
	return root


def _run(repo_root, *extra, cwd=None):
	cmd = [sys.executable, str(CHECKER)]
	if repo_root is not None:
		cmd += ["--repo-root", str(repo_root)]
	cmd += list(extra)
	return subprocess.run(
		cmd,
		capture_output=True,
		text=True,
		cwd=cwd,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		timeout=60,
	)


def test_render_prompt_is_not_optional():
	optional = runpy.run_path(str(CHECKER))["OPTIONAL_REFS"]
	assert "render_prompt.py" not in optional


def test_missing_render_prompt_in_workflow_fails():
	with tempfile.TemporaryDirectory() as tmp:
		root = _make_tree(tmp, workflow_text=REFERENCING_WORKFLOW, stage_text=NEUTRAL_STAGE, with_render_prompt=False)
		result = _run(root)
		assert result.returncode == 1, result.stdout + result.stderr
		assert "ci.yml: " + MISSING_MESSAGE in result.stderr


def test_missing_render_prompt_in_stage_support_fails():
	with tempfile.TemporaryDirectory() as tmp:
		root = _make_tree(tmp, workflow_text=NEUTRAL_WORKFLOW, stage_text=REFERENCING_STAGE, with_render_prompt=False)
		result = _run(root)
		assert result.returncode == 1, result.stdout + result.stderr
		assert "stage_workflow_support.sh: " + MISSING_MESSAGE in result.stderr


def test_missing_render_prompt_in_files_mode_fails():
	with tempfile.TemporaryDirectory() as tmp:
		root = _make_tree(tmp, workflow_text=REFERENCING_WORKFLOW, stage_text=NEUTRAL_STAGE, with_render_prompt=False)
		result = _run(root, "--files", ".github/workflows/ci.yml")
		assert result.returncode == 1, result.stdout + result.stderr
		assert MISSING_MESSAGE in result.stderr


def test_present_render_prompt_passes():
	with tempfile.TemporaryDirectory() as tmp:
		root = _make_tree(tmp, workflow_text=REFERENCING_WORKFLOW, stage_text=REFERENCING_STAGE, with_render_prompt=True)
		result = _run(root)
		assert result.returncode == 0, result.stdout + result.stderr
		assert "All workflow script references resolve to existing files." in result.stdout


def test_real_repository_passes():
	result = _run(None, cwd=REPO_ROOT)
	assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
	failures = 0
	for name, func in sorted(globals().items()):
		if name.startswith("test_") and callable(func):
			try:
				func()
				print(f"PASS {name}")
			except AssertionError as exc:
				failures += 1
				print(f"FAIL {name}: {exc}")
	sys.exit(1 if failures else 0)

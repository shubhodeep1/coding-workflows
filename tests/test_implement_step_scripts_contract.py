#!/usr/bin/env python3
"""Contract for implement.yml steps whose bodies live in scripts/.

Issue #9678: implement.yml reached 440,172 bytes, close to the 480,000-byte
guard in tests/test_workflow_file_size_limit.py. The run bodies of the
post-editor steps "Preflight destructive-commit guard" and "Attempt post-Codex
syntax repair" moved to scripts/implement_step_*.sh. Each step keeps its name,
id, if, env and continue-on-error in the workflow. Both steps run after the
editor's writes are copied back into the workspace, so their run: block
sources the script only from the trusted pre-editor
IMPLEMENT_STAGED_SUPPORT_RUN_DIR copy and fails closed otherwise. These checks
pin that shape.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import (  # noqa: E402
	IMPLEMENT_STEP_SCRIPTS,
	IMPLEMENT_WORKFLOW_PATH,
	REPO_ROOT,
	expanded_implement_text,
	review_autofix_step_script_body,
)

SCRIPTS_DIR = REPO_ROOT / "scripts"
MAX_WORKFLOW_BYTES = 430_000
WRAPPER_RE = re.compile(
	r'IMPLEMENT_STEP_SCRIPT="\$\{IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-\}/(implement_step_[a-z0-9_]+\.sh)"'
)

# Step metadata that must stay in the workflow after the move.
EXPECTED_STEP_METADATA: dict[str, dict[str, object]] = {
	"Preflight destructive-commit guard": {
		"lines": [
			"        if: env.SKIP_IMPLEMENT != 'true'",
			"        id: preflight_destructive_guard",
			"        # Step env overrides GITHUB_ENV so injected job values cannot weaken guards (#6504).",
		],
		"env": [
			"ALLOW_BULK_DELETE", "BULK_DELETE_THRESHOLD", "BULK_DELETE_THRESHOLD_MD", "ENFORCE_FILES_TOUCHED",
			"ALLOW_OUT_OF_SCOPE_FILES", "ALLOW_WORKFLOW_EDITS", "WRITE_GUARDS_ENABLED", "SCOPE_LOCK_LABEL_ENABLED",
		],
		"continue_on_error": False,
	},
	"Attempt post-Codex syntax repair": {
		"lines": [
			"        if: env.SKIP_IMPLEMENT != 'true' && env.HEAL_ROUTE != 'true' && steps.validate_syntax_changed_files.outcome == 'failure'",
			"        id: post_codex_syntax_repair",
		],
		"env": ["OPENROUTER_API_KEY", "DEPENDENCY_PROXY_ALLOWED_HOSTS"],
		"continue_on_error": True,
	},
}


def _raw() -> str:
	return IMPLEMENT_WORKFLOW_PATH.read_text(encoding="utf-8")


def _steps(text: str) -> list[dict]:
	"""Split the workflow into steps without a YAML dependency.

	Each step is {"name", "text", "run", "offset"}; "run" is the dedented
	run: | body and "offset" the step's position in *text*.
	"""
	steps = []
	for match in re.finditer(r"^      - name: (.+)$", text, re.M):
		start = match.start()
		end = text.find("\n      - name: ", start + 1)
		chunk = text[start:] if end == -1 else text[start:end]
		name = match.group(1).strip()
		if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
			name = name[1:-1]
		lines = chunk.split("\n")
		run_lines: list[str] = []
		if "        run: |" in lines:
			for line in lines[lines.index("        run: |") + 1:]:
				if line and not line.startswith(" " * 10):
					break
				run_lines.append(line[10:])
		while run_lines and not run_lines[-1].strip():
			run_lines.pop()
		steps.append({"name": name, "text": chunk, "run": "\n".join(run_lines), "offset": start})
	return steps


def _step(steps: list[dict], name: str) -> dict:
	matches = [step for step in steps if step["name"] == name]
	assert len(matches) == 1, f"expected exactly one step named {name!r}, found {len(matches)}"
	return matches[0]


def _stage_required_list() -> list[str]:
	stage = _step(_steps(_raw()), "Stage workflow support files")["run"]
	line = next(line for line in stage.splitlines() if line.startswith("for f in gh_helpers.sh "))
	assert line.endswith("; do"), line
	return line[len("for f in "):-len("; do")].split()


def test_implement_workflow_keeps_fifty_thousand_bytes_of_headroom() -> None:
	size = IMPLEMENT_WORKFLOW_PATH.stat().st_size
	assert size <= MAX_WORKFLOW_BYTES, (
		f"implement.yml is {size} bytes; move another run: body into scripts/implement_step_<slug>.sh "
		f"to stay at or below {MAX_WORKFLOW_BYTES} bytes (#9678)."
	)


def test_registry_wrappers_and_files_match() -> None:
	raw = _raw()
	wrapped = WRAPPER_RE.findall(raw)
	assert sorted(wrapped) == sorted(IMPLEMENT_STEP_SCRIPTS.values())
	steps = _steps(raw)
	for name, script in IMPLEMENT_STEP_SCRIPTS.items():
		run = _step(steps, name)["run"]
		assert WRAPPER_RE.findall(run) == [script], name
		assert (SCRIPTS_DIR / script).is_file(), script


def test_wrapper_fails_closed_and_reads_only_the_trusted_run_dir() -> None:
	steps = _steps(_raw())
	for name, script in IMPLEMENT_STEP_SCRIPTS.items():
		run = _step(steps, name)["run"]
		expected = "\n".join([
			f'IMPLEMENT_STEP_SCRIPT="${{IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}}/{script}"',
			'if [ -z "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}" ] || [ ! -f "${IMPLEMENT_STEP_SCRIPT}" ] || [ -L "${IMPLEMENT_STEP_SCRIPT}" ]; then',
			f'  echo "::error::Trusted pre-editor copy of {script} is missing (IMPLEMENT_STAGED_SUPPORT_RUN_DIR=${{IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-<unset>}}); the step never runs a workspace copy."',
			"  exit 1",
			"fi",
			"# shellcheck source=/dev/null",
			'source "${IMPLEMENT_STEP_SCRIPT}"',
		])
		assert run == expected, name
		# No workspace or checkout fallback: the editor's writes may be there.
		for forbidden in ("scripts/", ":-scripts", ".codex-workflow-src", "GITHUB_WORKSPACE"):
			assert forbidden not in run, (name, forbidden)


def test_moved_steps_keep_id_if_env_and_continue_on_error() -> None:
	steps = _steps(_raw())
	assert set(EXPECTED_STEP_METADATA) == set(IMPLEMENT_STEP_SCRIPTS)
	for name, meta in EXPECTED_STEP_METADATA.items():
		header = _step(steps, name)["text"].split("\n        run: |", 1)[0]
		header_lines = header.split("\n")
		for line in meta["lines"]:
			assert line in header_lines, (name, line)
		assert "        env:" in header_lines, name
		for key in meta["env"]:
			assert any(line.startswith(f"          {key}: ${{{{ ") for line in header_lines), (name, key)
		assert ("        continue-on-error: true" in header_lines) is meta["continue_on_error"], name


def test_scripts_have_header_mode_and_no_expressions() -> None:
	for name, script in IMPLEMENT_STEP_SCRIPTS.items():
		path = SCRIPTS_DIR / script
		text = path.read_text(encoding="utf-8")
		lines = text.split("\n")
		assert lines[0] == "#!/usr/bin/env bash", script
		assert lines[1] == "# shellcheck shell=bash", script
		assert f'"{name}"' in text.split("set -euo pipefail", 1)[0], script
		assert "#9678" in text.split("set -euo pipefail", 1)[0], script
		assert path.stat().st_mode & 0o111 == 0o111, f"{script} must be mode 0755"
		assert "${{" not in text, f"{script} must not contain GitHub expressions; pass them through env:"
		assert review_autofix_step_script_body(script).startswith("set -euo pipefail\n"), script


def test_stage_required_list_installs_every_registered_script() -> None:
	required = _stage_required_list()
	for script in IMPLEMENT_STEP_SCRIPTS.values():
		assert required.count(script) == 1, script


def test_every_wrapper_runs_after_staging_and_the_editor() -> None:
	steps = _steps(_raw())
	stage = _step(steps, "Stage workflow support files")["offset"]
	editor = _step(steps, "Run Codex implementation")["offset"]
	assert stage < editor
	for name in IMPLEMENT_STEP_SCRIPTS:
		assert _step(steps, name)["offset"] > editor, name


def test_expanded_text_inlines_every_wrapper() -> None:
	expanded = expanded_implement_text()
	assert "IMPLEMENT_STEP_SCRIPT" not in expanded
	steps = _steps(expanded)
	for name, script in IMPLEMENT_STEP_SCRIPTS.items():
		assert _step(steps, name)["run"] == review_autofix_step_script_body(script), name


def test_expanded_bodies_parse_as_bash() -> None:
	if shutil.which("bash") is None:
		raise unittest.SkipTest("bash is not available")
	steps = _steps(expanded_implement_text())
	with tempfile.TemporaryDirectory(prefix="implement-step-scripts-") as td:
		for name in IMPLEMENT_STEP_SCRIPTS:
			body = Path(td) / "body.sh"
			body.write_text(_step(steps, name)["run"] + "\n", encoding="utf-8")
			result = subprocess.run(
				["bash", "-n", str(body)], capture_output=True, text=True, check=False,
				env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
			)
			assert result.returncode == 0, (name, result.stderr)


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	skipped = 0
	for test in tests:
		try:
			test()
		except unittest.SkipTest:
			skipped += 1
	print(f"{len(tests) - skipped} passed, {skipped} skipped")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

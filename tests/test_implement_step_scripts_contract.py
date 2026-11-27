#!/usr/bin/env python3
"""Contract for implement.yml steps whose bodies live in scripts/ (#9678).

implement.yml reached 447,636 bytes, close to the 480,000-byte guard in
tests/test_workflow_file_size_limit.py. The run bodies of "Preflight
destructive-commit guard" and "Attempt post-Codex syntax repair" moved to
scripts/implement_step_*.sh. Each step keeps its name, id, if, env and
continue-on-error in the workflow. Its run: block sources the script only from
the trusted pre-editor copy in IMPLEMENT_STAGED_SUPPORT_RUN_DIR and fails
closed when that copy is missing or a symlink. Both steps run after the
isolated editor's writes are copied back, so a workspace copy must never run.
These checks pin that shape.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import (  # noqa: E402
	IMPLEMENT_STEP_SCRIPTS,
	IMPLEMENT_WORKFLOW_PATH,
	REPO_ROOT,
	expanded_implement_text,
)

SCRIPTS_DIR = REPO_ROOT / "scripts"
STAGE_HELPER = SCRIPTS_DIR / "stage_support_for_phase.sh"
MAX_WORKFLOW_BYTES = 430_000
EDITOR_STEP = "Run Codex implementation"

STEP_METADATA: dict[str, dict] = {
	"Preflight destructive-commit guard": {
		"id": "preflight_destructive_guard",
		"if": "env.SKIP_IMPLEMENT != 'true'",
		"continue-on-error": None,
		"env": {
			"ALLOW_BULK_DELETE": "${{ vars.ALLOW_BULK_DELETE || 'false' }}",
			"BULK_DELETE_THRESHOLD": "${{ vars.BULK_DELETE_THRESHOLD || '3' }}",
			"BULK_DELETE_THRESHOLD_MD": "${{ vars.BULK_DELETE_THRESHOLD_MD || '100' }}",
			"ENFORCE_FILES_TOUCHED": "${{ vars.ENFORCE_FILES_TOUCHED || 'true' }}",
			"ALLOW_OUT_OF_SCOPE_FILES": "${{ vars.ALLOW_OUT_OF_SCOPE_FILES || 'false' }}",
			"ALLOW_WORKFLOW_EDITS": "${{ vars.ALLOW_WORKFLOW_EDITS || 'true' }}",
			"WRITE_GUARDS_ENABLED": "${{ vars.WRITE_GUARDS_ENABLED || 'true' }}",
			"SCOPE_LOCK_LABEL_ENABLED": "${{ vars.SCOPE_LOCK_LABEL_ENABLED || 'false' }}",
		},
	},
	"Attempt post-Codex syntax repair": {
		"id": "post_codex_syntax_repair",
		"if": "env.SKIP_IMPLEMENT != 'true' && env.HEAL_ROUTE != 'true' && steps.validate_syntax_changed_files.outcome == 'failure'",
		"continue-on-error": "true",
		"env": {
			"OPENROUTER_API_KEY": "${{ secrets.OPENROUTER_API_KEY }}",
			"DEPENDENCY_PROXY_ALLOWED_HOSTS": "${{ vars.DEPENDENCY_PROXY_ALLOWED_HOSTS || '' }}",
		},
	},
}


def _wrapper(script: str) -> str:
	return (
		f'IMPLEMENT_STEP_SCRIPT="${{IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}}/{script}"\n'
		'if [ -z "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}" ] || [ ! -f "${IMPLEMENT_STEP_SCRIPT}" ] || [ -L "${IMPLEMENT_STEP_SCRIPT}" ]; then\n'
		f'  echo "::error::Trusted pre-editor copy of {script} is missing (IMPLEMENT_STAGED_SUPPORT_RUN_DIR=${{IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-<unset>}}); the step never runs a workspace copy."\n'
		"  exit 1\n"
		"fi\n"
		"# shellcheck source=/dev/null\n"
		'source "${IMPLEMENT_STEP_SCRIPT}"'
	)


def _raw() -> str:
	return IMPLEMENT_WORKFLOW_PATH.read_text(encoding="utf-8")


def _steps(text: str) -> list[dict]:
	"""Split the workflow into steps without a YAML dependency.

	Each step is {"name", "text", "run", "keys", "env"}; "run" is the dedented
	run: | body, "keys" maps the 8-space step keys to their scalar values.
	"""
	chunks = re.split(r"\n(?=      - name: )", text)[1:]
	steps = []
	for chunk in chunks:
		lines = chunk.split("\n")
		name = lines[0][len("      - name: "):].strip()
		if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
			name = name[1:-1]
		keys: dict[str, str] = {}
		env: dict[str, str] = {}
		run_lines: list[str] = []
		idx = 1
		while idx < len(lines):
			line = lines[idx]
			key_match = re.match(r"^        ([a-z-]+):(?: (.*))?$", line)
			if line == "        run: |":
				for body in lines[idx + 1:]:
					if body and not body.startswith(" " * 10):
						break
					run_lines.append(body[10:])
				break
			if line == "        env:":
				idx += 1
				while idx < len(lines) and lines[idx].startswith(" " * 10):
					env_match = re.match(r"^          ([A-Z0-9_]+): (.*)$", lines[idx])
					if env_match:
						env[env_match.group(1)] = env_match.group(2)
					idx += 1
				continue
			if key_match:
				keys[key_match.group(1)] = (key_match.group(2) or "").strip()
			idx += 1
		while run_lines and run_lines[-1] == "":
			run_lines.pop()
		steps.append({"name": name, "text": chunk, "run": "\n".join(run_lines), "keys": keys, "env": env})
	return steps


def _step(steps: list[dict], name: str) -> dict:
	matches = [step for step in steps if step["name"] == name]
	assert len(matches) == 1, f"expected exactly one step named {name!r}, found {len(matches)}"
	return matches[0]


def _legacy_loop_names(text: str) -> list[str]:
	matches = re.findall(r"^\s*for f in (gh_helpers\.sh [^;]+); do$", text, re.M)
	assert matches, "Stage workflow support files no longer holds the `for f in gh_helpers.sh ...` loop"
	return matches[0].split()


def _helper_implement_required() -> list[str]:
	text = STAGE_HELPER.read_text(encoding="utf-8")
	block = re.search(r"^\timplement\)\n(.*?)^\t\t;;", text, re.M | re.S)
	assert block, "stage_support_for_phase.sh has no implement case"
	match = re.search(r"stage_support_required_scripts=\((.*?)\)", block.group(1), re.S)
	assert match, "implement case defines no stage_support_required_scripts"
	return match.group(1).split()


def test_workflow_keeps_fifty_thousand_bytes_of_headroom() -> None:
	size = IMPLEMENT_WORKFLOW_PATH.stat().st_size
	assert size <= MAX_WORKFLOW_BYTES, f"implement.yml is {size} bytes; move more run: bodies into scripts/ (#9678)"


def test_registry_wrappers_match_the_exact_text() -> None:
	steps = _steps(_raw())
	for name, script in IMPLEMENT_STEP_SCRIPTS.items():
		run = _step(steps, name)["run"]
		assert run == _wrapper(script), name
		assert ".codex-workflow-src" not in run, name
		assert '"scripts/' not in run and "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}" not in run, name
		assert (SCRIPTS_DIR / script).is_file(), script
	wrapped = set(re.findall(r'IMPLEMENT_STEP_SCRIPT="\$\{IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-\}/(implement_step_[a-z0-9_]+\.sh)"', _raw()))
	assert wrapped == set(IMPLEMENT_STEP_SCRIPTS.values())


def test_moved_steps_keep_id_if_env_and_continue_on_error() -> None:
	steps = _steps(_raw())
	for name, expected in STEP_METADATA.items():
		step = _step(steps, name)
		assert step["keys"].get("id") == expected["id"], name
		assert step["keys"].get("if") == expected["if"], name
		assert step["keys"].get("continue-on-error") == expected["continue-on-error"], name
		assert step["env"] == expected["env"], name
	preflight = _step(steps, "Preflight destructive-commit guard")["text"]
	assert "        # Step env overrides GITHUB_ENV so injected job values cannot weaken guards (#6504).\n        env:\n" in preflight


def test_scripts_have_header_mode_and_no_expressions() -> None:
	for name, script in IMPLEMENT_STEP_SCRIPTS.items():
		path = SCRIPTS_DIR / script
		text = path.read_text(encoding="utf-8")
		lines = text.split("\n")
		assert lines[0] == "#!/usr/bin/env bash", script
		assert lines[1] == "# shellcheck shell=bash", script
		assert lines[2].startswith("#") and f'"{name}"' in lines[2], script
		code_start = next(i for i, line in enumerate(lines) if not line.startswith("#"))
		assert any("#9678" in line for line in lines[:code_start]), script
		assert lines[code_start] == "set -euo pipefail", script
		assert "${{" not in text, script
		assert stat.S_IMODE(path.stat().st_mode) == 0o755, script
		assert text.endswith("\n") and not text.endswith("\n\n"), script


def test_preflight_marker_comments_are_unchanged() -> None:
	text = (SCRIPTS_DIR / IMPLEMENT_STEP_SCRIPTS["Preflight destructive-commit guard"]).read_text(encoding="utf-8")
	for marker in (
		"# >>> automation-path grant guard (preflight) >>>",
		"# <<< automation-path grant guard (preflight) <<<",
		"# >>> files_touched scope-enforcement guard (preflight) >>>",
		"# <<< files_touched scope-enforcement guard (preflight) <<<",
	):
		assert text.count(marker) == 1, marker


def test_scripts_are_staged_in_both_required_lists() -> None:
	legacy = _legacy_loop_names(_raw())
	helper = _helper_implement_required()
	for script in IMPLEMENT_STEP_SCRIPTS.values():
		assert script in legacy, script
		assert script in helper, script
	assert legacy == helper


def test_moved_steps_run_after_the_editor_step() -> None:
	names = [step["name"] for step in _steps(_raw())]
	editor = names.index(EDITOR_STEP)
	for name in IMPLEMENT_STEP_SCRIPTS:
		assert names.index(name) > editor, name


def test_expanded_text_inlines_the_bodies() -> None:
	expanded = expanded_implement_text()
	assert "IMPLEMENT_STEP_SCRIPT=" not in expanded
	assert 'source "${IMPLEMENT_STEP_SCRIPT}"' not in expanded
	for marker in (
		"# >>> automation-path grant guard (preflight) >>>",
		"# >>> files_touched scope-enforcement guard (preflight) >>>",
		'echo "repaired=false" >> "$GITHUB_OUTPUT"',
	):
		assert marker in expanded, marker
	steps = _steps(expanded)
	for name, script in IMPLEMENT_STEP_SCRIPTS.items():
		lines = (SCRIPTS_DIR / script).read_text(encoding="utf-8").rstrip("\n").split("\n")
		code = "\n".join(lines[next(i for i, line in enumerate(lines) if not line.startswith("#")):])
		assert _step(steps, name)["run"] == code, name


def test_expanded_bodies_parse_as_bash() -> None:
	if shutil.which("bash") is None:
		raise unittest.SkipTest("bash is not available")
	steps = _steps(expanded_implement_text())
	for name in IMPLEMENT_STEP_SCRIPTS:
		result = subprocess.run(["bash", "-n"], input=_step(steps, name)["run"], text=True, capture_output=True, check=False)
		assert result.returncode == 0, f"{name}: {result.stderr}"
	for script in IMPLEMENT_STEP_SCRIPTS.values():
		result = subprocess.run(["bash", "-n", str(SCRIPTS_DIR / script)], text=True, capture_output=True, check=False)
		assert result.returncode == 0, f"{script}: {result.stderr}"


def test_wrapper_fails_closed_without_the_trusted_copy() -> None:
	if shutil.which("bash") is None:
		raise unittest.SkipTest("bash is not available")
	import tempfile

	script = IMPLEMENT_STEP_SCRIPTS["Preflight destructive-commit guard"]
	wrapper = _wrapper(script)
	with tempfile.TemporaryDirectory() as tmp:
		base = Path(tmp)
		env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "IMPLEMENT_STAGED_SUPPORT_RUN_DIR")}
		unset = subprocess.run(["bash", "-c", wrapper], env=env, text=True, capture_output=True, check=False)
		assert unset.returncode == 1 and "IMPLEMENT_STAGED_SUPPORT_RUN_DIR=<unset>" in unset.stdout
		run_dir = base / "run"
		run_dir.mkdir()
		missing = subprocess.run(["bash", "-c", wrapper], env={**env, "IMPLEMENT_STAGED_SUPPORT_RUN_DIR": str(run_dir)}, text=True, capture_output=True, check=False)
		assert missing.returncode == 1 and "::error::Trusted pre-editor copy" in missing.stdout
		target = base / "evil.sh"
		target.write_text("echo ran-evil\n", encoding="utf-8")
		(run_dir / script).symlink_to(target)
		linked = subprocess.run(["bash", "-c", wrapper], env={**env, "IMPLEMENT_STAGED_SUPPORT_RUN_DIR": str(run_dir)}, text=True, capture_output=True, check=False)
		assert linked.returncode == 1 and "ran-evil" not in linked.stdout
		(run_dir / script).unlink()
		(run_dir / script).write_text("echo ran-trusted\n", encoding="utf-8")
		trusted = subprocess.run(["bash", "-c", wrapper], env={**env, "IMPLEMENT_STAGED_SUPPORT_RUN_DIR": str(run_dir)}, text=True, capture_output=True, check=False)
		assert trusted.returncode == 0 and "ran-trusted" in trusted.stdout


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

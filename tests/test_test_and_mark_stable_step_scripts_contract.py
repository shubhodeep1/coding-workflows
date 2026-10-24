#!/usr/bin/env python3
"""Contract for test-and-mark-stable.yml steps whose bodies live in scripts/.

Issue #7991: the workflow reached 441,071 bytes, close to the 480,000-byte
guard in tests/test_workflow_file_size_limit.py. The Phase 4 and Phase 4b run
bodies of the e2e-smoke-test job moved to scripts/stable_e2e_step_*.sh. Each
step keeps its name, id, if and env in the workflow; its run: block checks
STABLE_E2E_STEP_SCRIPTS_VERIFIED and sources a copy taken from a sparse
checkout pinned to github.sha. These checks pin that shape.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import (  # noqa: E402
	REPO_ROOT,
	TEST_AND_MARK_STABLE_STEP_SCRIPTS,
	TEST_AND_MARK_STABLE_WORKFLOW_PATH,
	expanded_test_and_mark_stable_text,
)

SCRIPTS_DIR = REPO_ROOT / "scripts"
MAX_WORKFLOW_BYTES = 430_000
WRAPPER_RE = re.compile(
	r'STABLE_E2E_STEP_SCRIPT="\$\{RUNNER_TEMP\}/stable-e2e-step-scripts/(stable_e2e_step_[a-z0-9_]+\.sh)"'
)


def _raw() -> str:
	return TEST_AND_MARK_STABLE_WORKFLOW_PATH.read_text(encoding="utf-8")


def _e2e_steps(text: str) -> list[dict]:
	"""Split the e2e-smoke-test job into steps without a YAML dependency.

	Each step is {"name", "text", "run"}; "run" is the dedented run: | body.
	"""
	start = text.index("\n  e2e-smoke-test:\n")
	end = text.index("\n  validate-scripts:\n", start)
	job = text[start:end]
	chunks = re.split(r"\n(?=      - name: )", job)[1:]
	steps = []
	for chunk in chunks:
		lines = chunk.split("\n")
		name = lines[0][len("      - name: "):].strip()
		if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
			name = name[1:-1]
		run_lines: list[str] = []
		if "        run: |" in lines:
			for line in lines[lines.index("        run: |") + 1:]:
				if line and not line.startswith(" " * 10):
					break
				run_lines.append(line[10:])
		run = "\n".join(run_lines).rstrip("\n") + "\n" if run_lines else ""
		steps.append({"name": name, "text": chunk, "run": run})
	return steps


def _step(steps: list[dict], name: str) -> dict:
	matches = [s for s in steps if s.get("name") == name]
	assert len(matches) == 1, name
	return matches[0]


def test_workflow_keeps_fifty_thousand_bytes_of_headroom() -> None:
	size = TEST_AND_MARK_STABLE_WORKFLOW_PATH.stat().st_size
	assert size <= MAX_WORKFLOW_BYTES, size


def test_registry_wrappers_and_files_match() -> None:
	steps = _e2e_steps(_raw())
	wrapped = {}
	for step in steps:
		match = WRAPPER_RE.search(step.get("run") or "")
		if match:
			wrapped[step["name"]] = match.group(1)
	assert wrapped == TEST_AND_MARK_STABLE_STEP_SCRIPTS
	on_disk = {p.name for p in SCRIPTS_DIR.glob("stable_e2e_step_*.sh")}
	assert on_disk == set(TEST_AND_MARK_STABLE_STEP_SCRIPTS.values())


def test_wrapper_fails_closed_and_sources_the_script() -> None:
	steps = _e2e_steps(_raw())
	for name, script in TEST_AND_MARK_STABLE_STEP_SCRIPTS.items():
		run = _step(steps, name)["run"]
		assert '"${STABLE_E2E_STEP_SCRIPTS_VERIFIED:-}" != "true"' in run
		assert '[ -L "${STABLE_E2E_STEP_SCRIPT}" ]' in run
		assert '[ ! -f "${STABLE_E2E_STEP_SCRIPT}" ]' in run
		assert "exit 1" in run
		assert run.rstrip().endswith('source "${STABLE_E2E_STEP_SCRIPT}"')
		assert "${{" not in run
		assert script in run


def test_moved_steps_keep_id_if_and_env() -> None:
	steps = _e2e_steps(_raw())
	wait = _step(steps, "Phase 4: Wait for review & autofix to complete")["text"]
	assert "\n        id: wait-review\n" in wait
	for key in ("PR_NUMBER", "BAIT_SHA", "BAIT_CREATED_AT", "ISSUE_NUMBER"):
		assert f"\n          {key}: ${{{{ " in wait, key
	verify = _step(steps, "Phase 4b: Verify editor restored canary (pytest + retry)")["text"]
	assert "\n        id: verify-bait-removed\n" in verify
	assert "steps.inject-bait.outputs.status == 'injected'" in verify
	for key in ("PR_NUMBER", "ISSUE_NUMBER", "BAIT_SHA", "PRIOR_REVIEW_RUN"):
		assert f"\n          {key}: ${{{{ " in verify, key


def test_scripts_have_header_mode_and_no_expressions() -> None:
	for script in TEST_AND_MARK_STABLE_STEP_SCRIPTS.values():
		path = SCRIPTS_DIR / script
		assert path.is_file() and not path.is_symlink(), script
		assert os.stat(path).st_mode & 0o777 == 0o755, script
		text = path.read_text(encoding="utf-8")
		lines = text.split("\n")
		assert lines[0] == "#!/usr/bin/env bash", script
		assert lines[1] == "# shellcheck shell=bash", script
		assert "test-and-mark-stable.yml" in "\n".join(lines[:12]), script
		assert "${{" not in text, script


def test_sparse_checkout_is_pinned_and_lists_the_registry() -> None:
	steps = _e2e_steps(_raw())
	checkout = _step(steps, "Checkout release-gate step scripts")["text"]
	assert "\n        uses: actions/checkout@v5\n" in checkout
	assert "\n          ref: ${{ github.sha }}\n" in checkout
	assert "\n          path: .stable-e2e-step-src\n" in checkout
	assert "\n          persist-credentials: false\n" in checkout
	assert "\n          sparse-checkout-cone-mode: false\n" in checkout
	listed_block = checkout.split("          sparse-checkout: |\n", 1)[1]
	listed = set()
	for line in listed_block.split("\n"):
		if not line.startswith(" " * 12):
			break
		listed.add(line.strip())
	assert listed == {f"scripts/{s}" for s in TEST_AND_MARK_STABLE_STEP_SCRIPTS.values()}


def test_verify_step_runs_before_every_wrapper() -> None:
	steps = _e2e_steps(_raw())
	names = [s.get("name") for s in steps]
	checkout_idx = names.index("Checkout release-gate step scripts")
	verify_idx = names.index("Verify release-gate step scripts")
	assert checkout_idx < verify_idx
	verify = steps[verify_idx]
	assert "\n          EXPECTED_SHA: ${{ github.sha }}\n" in verify["text"]
	run = verify["run"]
	assert "rev-parse HEAD" in run
	assert '"${ACTUAL_SHA}" != "${EXPECTED_SHA}"' in run
	assert '[ -L "${src}" ]' in run
	assert 'install -d -m 0700 "${DEST_DIR}"' in run
	assert 'DEST_DIR="${RUNNER_TEMP}/stable-e2e-step-scripts"' in run
	assert 'echo "STABLE_E2E_STEP_SCRIPTS_VERIFIED=true" >> "$GITHUB_ENV"' in run
	for script in TEST_AND_MARK_STABLE_STEP_SCRIPTS.values():
		assert script in run
	for name in TEST_AND_MARK_STABLE_STEP_SCRIPTS:
		assert verify_idx < names.index(name)


def test_expanded_bodies_parse_as_bash() -> None:
	bash = shutil.which("bash")
	if bash is None:
		raise unittest.SkipTest("bash is not installed")
	steps = _e2e_steps(expanded_test_and_mark_stable_text())
	for name in TEST_AND_MARK_STABLE_STEP_SCRIPTS:
		run = _step(steps, name)["run"]
		assert "STABLE_E2E_STEP_SCRIPT=" not in run
		assert run.startswith("set -euo pipefail\n"), name
		result = subprocess.run([bash, "-n"], input=run, capture_output=True, text=True, check=False)
		assert result.returncode == 0, (name, result.stderr)
	for script in TEST_AND_MARK_STABLE_STEP_SCRIPTS.values():
		result = subprocess.run([bash, "-n", str(SCRIPTS_DIR / script)], capture_output=True, text=True, check=False)
		assert result.returncode == 0, (script, result.stderr)


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

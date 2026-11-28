#!/usr/bin/env python3
"""Contract for review_autofix.yml steps whose bodies live in scripts/.

The moved steps keep their name, if:, env: and continue-on-error: in the
workflow; their run: block only resolves the script and sources it in the
step shell. These checks pin the wrapper shape, the resolution order that
keeps consumer repos and older PR branches working, the fail-closed /
fail-open split, and the staging entries.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import (  # noqa: E402
	REPO_ROOT,
	REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS,
	REVIEW_AUTOFIX_SKIP_MERGE_JOB_STEP_SCRIPTS,
	REVIEW_AUTOFIX_STEP_SCRIPTS,
	REVIEW_AUTOFIX_SUPPORT_CHECKOUT_JOB_STEP_SCRIPTS,
	REVIEW_AUTOFIX_WORKFLOW_PATH,
	expanded_review_autofix_text,
	review_autofix_step_script_body,
)


STAGE_SUPPORT_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"

# #10208: the .codex-workflow-src fallback is sourced only when the trusted
# "Ensure workflow support source checkout" step verified that checkout at
# SCRIPT_REF. An early failure (before support staging) otherwise leaves a
# PR-committed .codex-workflow-src/scripts/<script> for always() steps to source.
# The rev-parse clears GIT_DIR/GIT_WORK_TREE: "Activate workspace shell
# context" exports them for the split PR workspace, and they would otherwise
# make git read the PR checkout's HEAD instead of .codex-workflow-src's.
SUPPORT_SOURCE_FALLBACK_VERIFY_LINE = (
	'case "${REVIEW_AUTOFIX_STEP_SCRIPT}" in "${GITHUB_WORKSPACE}/.codex-workflow-src/"*) '
	'[[ "${SCRIPT_REF:-}" =~ ^[0-9a-f]{40}$ ]] && '
	'[ "${REVIEW_SUPPORT_SOURCE_VERIFIED_SHA:-}" = "${SCRIPT_REF}" ] && '
	'[ "$(env -u GIT_DIR -u GIT_WORK_TREE git -C "${GITHUB_WORKSPACE}/.codex-workflow-src" rev-parse HEAD 2>/dev/null)" = "${SCRIPT_REF}" ] || '
	'{ echo "::warning::Refusing unverified .codex-workflow-src fallback (SCRIPT_REF=${SCRIPT_REF:-unset})."; '
	'REVIEW_AUTOFIX_STEP_SCRIPT=""; } ;; esac'
)


def _steps_by_name(workflow_text: str) -> dict[str, dict]:
	workflow = yaml.safe_load(workflow_text)
	steps: dict[str, dict] = {}
	for job in workflow["jobs"].values():
		for step in job.get("steps", []):
			if "name" in step:
				steps.setdefault(step["name"], step)
	return steps


def test_moved_steps_run_only_the_resolving_wrapper() -> None:
	steps = _steps_by_name(REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8"))
	for step_name, (script_name, severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items():
		assert step_name in steps, f"Step not found in review_autofix.yml: {step_name}"
		run = steps[step_name]["run"]
		if script_name in REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS:
			checkout_dir = REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS[script_name]
			expected_gate_lines = [
				f'REVIEW_AUTOFIX_STEP_SCRIPT="${{GITHUB_WORKSPACE}}/{checkout_dir}/scripts/{script_name}"',
				'if [ "${HEAD_GATE_HELPER_VERIFIED:-false}" != "true" ] || [ ! -f "${REVIEW_AUTOFIX_STEP_SCRIPT}" ]; then',
			]
			gate_run_lines = run.rstrip("\n").split("\n")
			assert gate_run_lines[:2] == expected_gate_lines, f"{step_name}: unexpected gate-job resolution:\n{run}"
			assert gate_run_lines[2].startswith("  echo \"::error::") and gate_run_lines[3] == "  exit 1", step_name
			assert gate_run_lines[-2:] == ["fi", 'source "${REVIEW_AUTOFIX_STEP_SCRIPT}"'], step_name
			assert len(gate_run_lines) == 6, f"{step_name}: gate-job wrapper must have no other resolution path"
			assert "SUPPORT_SCRIPTS_DIR" not in run and ".codex-workflow-src" not in run, (
				f"{step_name}: gate-job steps run before support staging and must read only the verified head-gate checkout"
			)
			assert "${{" not in run, f"{step_name}: wrapper must not carry GitHub expressions"
			continue
		if script_name in REVIEW_AUTOFIX_SKIP_MERGE_JOB_STEP_SCRIPTS:
			checkout_dir = REVIEW_AUTOFIX_SKIP_MERGE_JOB_STEP_SCRIPTS[script_name]
			skip_run_lines = run.rstrip("\n").split("\n")
			assert skip_run_lines[0] == f'REVIEW_AUTOFIX_STEP_SCRIPT="${{GITHUB_WORKSPACE}}/{checkout_dir}/scripts/{script_name}"', step_name
			verify_line = skip_run_lines[1]
			for check in (
				'[ "${FRESHNESS_HELPER_CHECKOUT_OUTCOME:-}" != "success" ]',
				'! [[ "${REVIEW_SUPPORT_SHA:-}" =~ ^[0-9a-f]{40}$ ]]',
				f'[ "$(git -C "${{GITHUB_WORKSPACE}}/{checkout_dir}" rev-parse HEAD 2>/dev/null)" != "${{REVIEW_SUPPORT_SHA}}" ]',
				'[ ! -f "${REVIEW_AUTOFIX_STEP_SCRIPT}" ]',
			):
				assert check in verify_line, f"{step_name}: wrapper must verify {check}"
			assert skip_run_lines[2].startswith("  echo \"::error::") and skip_run_lines[3] == "  exit 1", step_name
			assert skip_run_lines[-2:] == ["fi", 'source "${REVIEW_AUTOFIX_STEP_SCRIPT}"'], step_name
			assert len(skip_run_lines) == 6, f"{step_name}: skip-job wrapper must have no other resolution path"
			assert "SUPPORT_SCRIPTS_DIR" not in run and ".codex-workflow-src" not in run, step_name
			assert "${{" not in run, f"{step_name}: wrapper must not carry GitHub expressions"
			continue
		if script_name in REVIEW_AUTOFIX_SUPPORT_CHECKOUT_JOB_STEP_SCRIPTS:
			# #10166: jobs without the support bundle that verify their own
			# .codex-workflow-src checkout; no other resolution path.
			own_run_lines = run.rstrip("\n").split("\n")
			assert own_run_lines[0] == f'REVIEW_AUTOFIX_STEP_SCRIPT="${{GITHUB_WORKSPACE}}/.codex-workflow-src/scripts/{script_name}"', step_name
			for check in (
				'! [[ "${WORKFLOW_SUPPORT_REF:-}" =~ ^[0-9a-f]{40}$ ]]',
				'[ "$(git -C "${GITHUB_WORKSPACE}/.codex-workflow-src" rev-parse HEAD 2>/dev/null)" != "${WORKFLOW_SUPPORT_REF}" ]',
				'[ ! -f "${REVIEW_AUTOFIX_STEP_SCRIPT}" ]',
			):
				assert check in own_run_lines[1], f"{step_name}: wrapper must verify {check}"
			assert own_run_lines[2].startswith("  echo \"::error::") and own_run_lines[3] == "  exit 1", step_name
			assert own_run_lines[-2:] == ["fi", 'source "${REVIEW_AUTOFIX_STEP_SCRIPT}"'], step_name
			assert len(own_run_lines) == 6, f"{step_name}: wrapper must have no other resolution path"
			assert "SUPPORT_SCRIPTS_DIR" not in run, step_name
			assert "${{" not in run, f"{step_name}: wrapper must not carry GitHub expressions"
			continue
		expected_lines = [
			f'REVIEW_AUTOFIX_STEP_SCRIPT="${{SUPPORT_SCRIPTS_DIR:-}}/{script_name}"',
			f'[ -f "${{REVIEW_AUTOFIX_STEP_SCRIPT}}" ] || REVIEW_AUTOFIX_STEP_SCRIPT="${{GITHUB_WORKSPACE}}/.codex-workflow-src/scripts/{script_name}"',
			SUPPORT_SOURCE_FALLBACK_VERIFY_LINE,
			'if [ ! -f "${REVIEW_AUTOFIX_STEP_SCRIPT}" ]; then',
		]
		run_lines = run.rstrip("\n").split("\n")
		assert run_lines[:4] == expected_lines, f"{step_name}: unexpected resolution order:\n{run}"
		assert run_lines[-2:] == ["fi", 'source "${REVIEW_AUTOFIX_STEP_SCRIPT}"'], (
			f"{step_name}: the script must be sourced in the step shell (not run in a child bash) "
			"so BASH_ENV state, set -e and exit codes behave as they did inline"
		)
		if severity == "error":
			assert run_lines[4].startswith("  echo \"::error::") and run_lines[5] == "  exit 1", step_name
		else:
			assert run_lines[4].startswith("  echo \"::warning::") and run_lines[5] == "  exit 0", step_name
		assert "${{" not in run, f"{step_name}: wrapper must not carry GitHub expressions"


def test_support_source_verify_step_writes_fallback_sentinel() -> None:
	# Only the trusted verify step may mark .codex-workflow-src as verified,
	# and only after its rev-parse HEAD == SCRIPT_REF check (#10208).
	workflow = yaml.safe_load(REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8"))
	steps = workflow["jobs"]["codex-agent"]["steps"]
	names = [step.get("name") for step in steps]
	ensure = steps[names.index("Ensure workflow support source checkout")]["run"]
	sentinel = 'echo "REVIEW_SUPPORT_SOURCE_VERIFIED_SHA=${SCRIPT_REF}" >> "$GITHUB_ENV"'
	check = 'if [ "$(git -C .codex-workflow-src rev-parse HEAD 2>/dev/null)" != "${SCRIPT_REF}" ]; then'
	assert check in ensure and sentinel in ensure
	assert ensure.index(check) < ensure.index("exit 1") < ensure.index(sentinel)
	writers = [step.get("name") for step in steps if "REVIEW_SUPPORT_SOURCE_VERIFIED_SHA=" in str(step.get("run", ""))]
	assert writers == ["Ensure workflow support source checkout"], writers
	assert names.index("Checkout workflow support source") < names.index("Ensure workflow support source checkout")


def test_fail_open_only_for_steps_that_run_after_staging_failures() -> None:
	steps = _steps_by_name(REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8"))
	for step_name, (_script_name, severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items():
		runs_always = str(steps[step_name].get("if", "")).startswith("always()")
		assert (severity == "warning") == runs_always, (
			f"{step_name}: only always() steps may skip on a missing script; "
			f"got severity={severity} for if={steps[step_name].get('if')!r}"
		)


def test_step_scripts_exist_are_executable_and_parse() -> None:
	for script_name, _severity in REVIEW_AUTOFIX_STEP_SCRIPTS.values():
		path = REPO_ROOT / "scripts" / script_name
		assert path.is_file(), f"Missing step script: {path}"
		assert path.stat().st_mode & 0o111, f"Step script is not executable: {path}"
		text = path.read_text(encoding="utf-8")
		assert text.startswith("#!/usr/bin/env bash\n"), f"{script_name}: missing bash shebang"
		body = review_autofix_step_script_body(script_name)
		assert body.startswith("set -euo pipefail\n"), f"{script_name}: body must open with set -euo pipefail"
		# GitHub only substitutes ${{ }} inside the workflow file; in a
		# sourced script it would reach bash literally.
		assert "${{" not in text, f"{script_name}: contains a GitHub expression that would never be substituted"
		result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
		assert result.returncode == 0, f"{script_name}: bash -n failed: {result.stderr}"


def test_step_scripts_are_required_bootstrap_scripts() -> None:
	stage_helper = STAGE_SUPPORT_HELPER.read_text(encoding="utf-8")
	match = re.search(r'^REQUIRED_BOOTSTRAP_SCRIPTS="([^"]*)"', stage_helper, re.MULTILINE)
	assert match, "REQUIRED_BOOTSTRAP_SCRIPTS not found in scripts/stage_workflow_support.sh"
	required = set(match.group(1).split())
	for script_name, _severity in REVIEW_AUTOFIX_STEP_SCRIPTS.values():
		assert script_name in required, f"{script_name} must be staged via REQUIRED_BOOTSTRAP_SCRIPTS"


def test_every_wrapper_is_registered_and_expands() -> None:
	raw = REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8")
	wrapped = set(
		re.findall(
			r'REVIEW_AUTOFIX_STEP_SCRIPT="(?:\$\{SUPPORT_SCRIPTS_DIR:-\}|\$\{GITHUB_WORKSPACE\}/\.codex-head-gate-src/scripts|\$\{GITHUB_WORKSPACE\}/\.codex-freshness-src/scripts|\$\{GITHUB_WORKSPACE\}/\.codex-workflow-src/scripts)/([a-z0-9_]+\.sh)"',
			raw,
		)
	)
	registered = {script_name for script_name, _severity in REVIEW_AUTOFIX_STEP_SCRIPTS.values()}
	assert wrapped == registered, f"Wrapper/registry mismatch: wrapped={sorted(wrapped)} registered={sorted(registered)}"
	expanded = expanded_review_autofix_text()
	assert "REVIEW_AUTOFIX_STEP_SCRIPT" not in expanded
	expanded_steps = _steps_by_name(expanded)
	for step_name, (script_name, _severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items():
		assert expanded_steps[step_name]["run"].rstrip("\n") == review_autofix_step_script_body(script_name).rstrip("\n")


def test_step_script_registry_has_no_duplicate_step_names() -> None:
	# A dict literal silently keeps the last of two equal keys (#10166: one
	# step registered twice, one script orphaned). Scripts may repeat.
	import ast

	tree = ast.parse((REPO_ROOT / "tests" / "review_autofix_step_scripts.py").read_text(encoding="utf-8"))
	node = next(n for n in ast.walk(tree) if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "REVIEW_AUTOFIX_STEP_SCRIPTS")
	keys = [ast.literal_eval(k) for k in node.value.keys]
	assert len(keys) == len(set(keys)), sorted(k for k in keys if keys.count(k) > 1)


def test_step_script_registries_have_no_duplicate_keys_and_no_orphan_scripts() -> None:
	# A repeated key in a dict literal is silent in Python: the later entry
	# replaces the earlier one, and the earlier script stays on disk with no
	# step that runs it. A merge of two branches that each moved the same step
	# body to a script (PR #10167 with #9944, "Telegram editor-noop-suspicious
	# warning") produced exactly that, and no other check here noticed.
	registry_module_source = (Path(__file__).resolve().parent / "review_autofix_step_scripts.py").read_text(encoding="utf-8")
	for registry_node in ast.walk(ast.parse(registry_module_source)):
		if not isinstance(registry_node, ast.Dict):
			continue
		literal_keys = [key.value for key in registry_node.keys if isinstance(key, ast.Constant)]
		repeated_keys = sorted({key for key in literal_keys if literal_keys.count(key) > 1})
		assert not repeated_keys, (
			f"tests/review_autofix_step_scripts.py line {registry_node.lineno}: "
			f"dict literal repeats key(s) {repeated_keys}; keep one entry per step"
		)
	# Every scripts/review_autofix_step_*.sh is named in the workflow: by a
	# step wrapper, or by the inline step that sources it as a helper
	# (review_autofix_step_det_skip_followup_guard.sh).
	workflow_raw_text = REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8")
	scripts_on_disk = sorted(path.name for path in (REPO_ROOT / "scripts").glob("review_autofix_step_*.sh"))
	assert scripts_on_disk, "no scripts/review_autofix_step_*.sh found"
	orphan_scripts = [name for name in scripts_on_disk if name not in workflow_raw_text]
	assert not orphan_scripts, (
		f"scripts not named anywhere in review_autofix.yml: {orphan_scripts}; "
		"remove them or restore the step that runs them"
	)


def test_gate_job_step_scripts_come_only_from_verified_head_gate_checkout() -> None:
	assert REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS, "gate-job step registry must not be empty"
	raw = REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8")
	workflow = yaml.safe_load(raw)
	gate_steps = workflow["jobs"]["gate"]["steps"]
	names = [step.get("name") for step in gate_steps]
	by_name = {step.get("name"): step for step in gate_steps}
	checkout = by_name["Checkout head-gate helper"]
	verify = by_name["Verify head-gate helper identity"]
	assert checkout["with"]["ref"] == "${{ steps.resolve_support.outputs.review_support_sha }}"
	assert checkout["with"]["path"] == ".codex-head-gate-src"
	sparse = checkout["with"]["sparse-checkout"].split()
	assert 'echo "HEAD_GATE_HELPER_VERIFIED=true" >> "$GITHUB_ENV"' in verify["run"]
	for step_name, (script_name, _severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items():
		if script_name not in REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS:
			continue
		assert REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS[script_name] == ".codex-head-gate-src"
		assert f"scripts/{script_name}" in sparse, f"{script_name} must be in the head-gate sparse checkout"
		assert f"[ ! -f .codex-head-gate-src/scripts/{script_name} ]" in verify["run"], (
			f"{script_name} must be checked by the head-gate identity verify step"
		)
		assert step_name in by_name, f"{step_name} must run in the gate job"
		assert "HEAD_GATE_HELPER_VERIFIED" in by_name[step_name]["run"]
		assert names.index("Verify head-gate helper identity") < names.index(step_name)


def test_skip_merge_job_step_scripts_come_only_from_verified_freshness_checkout() -> None:
	assert REVIEW_AUTOFIX_SKIP_MERGE_JOB_STEP_SCRIPTS, "skip-merge step registry must not be empty"
	workflow = yaml.safe_load(REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8"))
	steps = workflow["jobs"]["deterministic-skip-merge"]["steps"]
	names = [step.get("name") for step in steps]
	by_name = {step.get("name"): step for step in steps}
	checkout = by_name["Checkout merge-base freshness helper"]
	assert checkout["with"]["ref"] == "${{ needs.gate.outputs.review_support_sha }}"
	assert checkout["with"]["path"] == ".codex-freshness-src"
	sparse = checkout["with"]["sparse-checkout"].split()
	for step_name, (script_name, _severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items():
		if script_name not in REVIEW_AUTOFIX_SKIP_MERGE_JOB_STEP_SCRIPTS:
			continue
		assert REVIEW_AUTOFIX_SKIP_MERGE_JOB_STEP_SCRIPTS[script_name] == ".codex-freshness-src"
		assert f"scripts/{script_name}" in sparse, f"{script_name} must be in the freshness sparse checkout"
		assert step_name in by_name, f"{step_name} must run in the deterministic-skip-merge job"
		assert by_name[step_name]["env"]["FRESHNESS_HELPER_CHECKOUT_OUTCOME"] == "${{ steps.checkout_freshness_helper.outcome }}"
		assert by_name[step_name]["env"]["REVIEW_SUPPORT_SHA"] == "${{ needs.gate.outputs.review_support_sha }}"
		assert names.index("Checkout merge-base freshness helper") < names.index(step_name)


def test_support_checkout_job_step_scripts_come_only_from_their_verified_checkout() -> None:
	# #10166: these jobs have no staged support bundle. Each checks out
	# .codex-workflow-src at review_support_sha and verifies it (exit 1 on
	# mismatch, exporting WORKFLOW_SUPPORT_REF) before the moved step runs.
	assert REVIEW_AUTOFIX_SUPPORT_CHECKOUT_JOB_STEP_SCRIPTS, "support-checkout step registry must not be empty"
	workflow = yaml.safe_load(REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8"))
	for step_name, (script_name, severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items():
		if script_name not in REVIEW_AUTOFIX_SUPPORT_CHECKOUT_JOB_STEP_SCRIPTS:
			continue
		assert severity == "error", step_name
		steps = workflow["jobs"][REVIEW_AUTOFIX_SUPPORT_CHECKOUT_JOB_STEP_SCRIPTS[script_name]]["steps"]
		names = [step.get("name") for step in steps]
		by_name = {step.get("name"): step for step in steps}
		checkout = by_name["Checkout workflow support source"]
		assert checkout["with"]["ref"] == "${{ needs.gate.outputs.review_support_sha }}"
		assert checkout["with"]["path"] == ".codex-workflow-src"
		sparse = checkout["with"].get("sparse-checkout")
		if sparse is not None:
			assert f"scripts/{script_name}" in sparse.split(), f"{script_name} must be in the sparse checkout"
		verify = by_name["Verify workflow support source"]
		assert "continue-on-error" not in verify and "if" not in verify
		assert 'echo "WORKFLOW_SUPPORT_REF=${WORKFLOW_SHA}" >> "$GITHUB_ENV"' in verify["run"]
		assert "if" not in by_name[step_name], f"{step_name}: must run only after the verify step succeeded"
		assert names.index("Checkout workflow support source") < names.index("Verify workflow support source") < names.index(step_name)


def test_capture_runtime_context_stays_inline() -> None:
	# #10166 security pass: this always() step can run before the trusted
	# support checkout, when the wrapper's .codex-workflow-src fallback would
	# source a PR-committed script. Its body stays inline, sourcing nothing.
	steps = _steps_by_name(REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8"))
	step = steps["Capture runtime context"]
	assert str(step.get("if", "")) == "always()", step.get("if")
	assert "REVIEW_AUTOFIX_STEP_SCRIPT" not in step["run"] and "source " not in step["run"], step["run"]
	assert 'mkdir -p "${RUNTIME_CONTEXT_DIR}"' in step["run"]
	assert "Capture runtime context" not in REVIEW_AUTOFIX_STEP_SCRIPTS


def main() -> int:
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	for func in test_functions:
		func()
	print(f"OK: {len(test_functions)} review_autofix step-script contract checks passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

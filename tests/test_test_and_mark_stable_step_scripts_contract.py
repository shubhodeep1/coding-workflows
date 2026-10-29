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


CAPACITY_MATCH = '*",ai:claude-capacity-wait,"*'
CAPACITY_STATUS = 'echo "status=claude_capacity" >> "$GITHUB_OUTPUT"'
CLARIFY_STEP = "Phase 1: Wait for clarify to complete"
PLAN_STEP = "Phase 2: Wait for plan to complete"
IMPLEMENT_STEP = "Phase 3b: Wait for PR creation (implement phase)"
ALT_MODEL_STEP = "Wait for clarify→plan→implement (alt-model)"
ORCHESTRATE_STEP = "Dispatch internal-orchestrate.yml with multi-issue project"


def _all_steps(text: str) -> list[dict]:
	"""Split every job's steps (issue #8217); same shape as _e2e_steps."""
	chunks = re.split(r"\n(?=      - name: )", text)[1:]
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


def _ordered(run: str, *needles: str) -> None:
	positions = []
	for needle in needles:
		assert run.count(needle) >= 1, needle
		positions.append(run.index(needle))
	assert positions == sorted(positions), list(zip(needles, positions))


def test_wait_loops_fail_fast_on_claude_capacity() -> None:
	"""Issue #8217: a phase paused on Claude capacity fails the gate at once."""
	steps = _all_steps(_raw())
	clarify = _step(steps, CLARIFY_STEP)["run"]
	_ordered(clarify, "grep -qE 'ai:planning|ai:awaiting-approval'", CAPACITY_MATCH, CAPACITY_STATUS, "Clarify phase stalled")
	plan = _step(steps, PLAN_STEP)["run"]
	_ordered(plan, "grep -q 'ai:implementing'", CAPACITY_MATCH, CAPACITY_STATUS, 'if [ "$PLAN_RUN_STATUS" = "completed" ]')
	implement = _step(steps, IMPLEMENT_STEP)["run"]
	_ordered(implement, 'if [ -n "$PR_NUMBER" ]; then', CAPACITY_MATCH, CAPACITY_STATUS, "status=implement_failed")
	alt = _step(steps, ALT_MODEL_STEP)["run"]
	_ordered(alt, CAPACITY_MATCH, CAPACITY_STATUS, 'body="/approved"', "grep -qE 'ai:done|")
	for run in (clarify, plan, implement, alt):
		assert "Claude pool at capacity (ai:claude-capacity-wait)" in run


def test_orchestrate_dispatch_reports_claude_capacity() -> None:
	run = _step(_all_steps(_raw()), ORCHESTRATE_STEP)["run"]
	_ordered(
		run,
		"AI_ENGINE_CAPACITY_WAIT role=ORCHESTRATE outcome=deferred",
		'IN("OWNER","MEMBER","COLLABORATOR")',
		"kind=capacity",
		CAPACITY_STATUS,
		"Could not locate tracking issue",
	)
	assert '[[ "${ORCH_RUN_ID}" =~ ^[0-9]+$ ]]' in run
	assert '[[ "${CAP_TRACKERS}" =~ ^[0-9]+$ ]]' in run
	capacity_part = run[run.index('CAP_EVIDENCE=""'):run.index("Could not locate tracking issue")]
	assert "grep" not in capacity_part
	assert "JOB_LOG}\"\n" not in capacity_part and "echo \"${JOB_LOG" not in capacity_part


def test_capacity_edited_step_bodies_parse_as_bash() -> None:
	bash = shutil.which("bash")
	if bash is None:
		raise unittest.SkipTest("bash is not installed")
	steps = _all_steps(_raw())
	for name in (CLARIFY_STEP, PLAN_STEP, IMPLEMENT_STEP, ALT_MODEL_STEP, ORCHESTRATE_STEP):
		run = _step(steps, name)["run"]
		result = subprocess.run([bash, "-n"], input=run, capture_output=True, text=True, check=False)
		assert result.returncode == 0, (name, result.stderr)


def _run_alt_model_wait(tmp: Path, labels: str) -> tuple[int, str, str]:
	bash = shutil.which("bash")
	assert bash is not None
	run = _step(_all_steps(_raw()), ALT_MODEL_STEP)["run"]
	bindir = tmp / "bin"
	bindir.mkdir()
	gh_log = tmp / "gh.log"
	stub = bindir / "gh"
	stub.write_text(
		"#!/usr/bin/env bash\n"
		'case "$*" in\n'
		'  *comments*) printf "%s\\n" "$*" >> "${GH_LOG}" ;;\n'
		'  *) printf "%s\\n" "${STUB_LABELS}" ;;\n'
		"esac\n",
		encoding="utf-8",
	)
	stub.chmod(0o755)
	output = tmp / "github_output"
	output.write_text("", encoding="utf-8")
	env = dict(os.environ)
	env.update(
		{
			"PATH": f"{bindir}{os.pathsep}{env.get('PATH', '')}",
			"GH_LOG": str(gh_log),
			"STUB_LABELS": labels,
			"GITHUB_OUTPUT": str(output),
			"TEST_REPO": "o/r",
			"ISSUE_NUMBER": "42",
		}
	)
	result = subprocess.run([bash, "-c", run], env=env, capture_output=True, text=True, check=False, timeout=60)
	posted = gh_log.read_text(encoding="utf-8") if gh_log.exists() else ""
	return result.returncode, output.read_text(encoding="utf-8"), posted


def test_alt_model_wait_stops_on_capacity_without_approving() -> None:
	if shutil.which("bash") is None:
		raise unittest.SkipTest("bash is not installed")
	import tempfile

	with tempfile.TemporaryDirectory() as tmp:
		rc, output, posted = _run_alt_model_wait(Path(tmp), "ai:awaiting-approval,ai:claude-capacity-wait")
		assert rc == 1
		assert "status=claude_capacity" in output
		assert posted == ""
	with tempfile.TemporaryDirectory() as tmp:
		rc, output, posted = _run_alt_model_wait(Path(tmp), "ai:claude-capacity-wait-x,ai:done")
		assert rc == 0
		assert "claude_capacity" not in output
	with tempfile.TemporaryDirectory() as tmp:
		rc, output, _ = _run_alt_model_wait(Path(tmp), "ai:done")
		assert rc == 0


def _run_orchestrate_capacity_check(tmp: Path, log: str, trackers: str = "0", log_fail: bool = False) -> tuple[int, str, str]:
	"""Run the #8217 capacity block of the orchestrate dispatch step against a stub gh."""
	bash = shutil.which("bash")
	assert bash is not None
	run = _step(_all_steps(_raw()), ORCHESTRATE_STEP)["run"]
	snippet = run[run.index('CAP_EVIDENCE=""'):run.index('echo "::error::Could not locate tracking issue')]
	bindir = tmp / "bin"
	bindir.mkdir()
	(tmp / "job.log").write_text(log, encoding="utf-8")
	stub = bindir / "gh"
	stub.write_text(
		"#!/usr/bin/env bash\n"
		'case "$*" in\n'
		'  *runs/777/jobs*) printf "11\\n12\\n" ;;\n'
		'  *jobs/*/logs*)\n'
		'    [ "${STUB_LOG_FAIL}" = "1" ] && exit 1\n'
		'    case "$*" in *--allow-escape-sequences*) cat "${STUB_LOG}" ;; *) echo "use --allow-escape-sequences" >&2; exit 1 ;; esac ;;\n'
		'  *issues*) printf "%s\\n" "${STUB_TRACKERS}" ;;\n'
		"  *) exit 1 ;;\n"
		"esac\n",
		encoding="utf-8",
	)
	stub.chmod(0o755)
	output = tmp / "github_output"
	output.write_text("run_id=777\n", encoding="utf-8")
	env = dict(os.environ)
	env.update(
		{
			"PATH": f"{bindir}{os.pathsep}{env.get('PATH', '')}",
			"STUB_LOG": str(tmp / "job.log"),
			"STUB_TRACKERS": trackers,
			"STUB_LOG_FAIL": "1" if log_fail else "0",
			"GITHUB_OUTPUT": str(output),
			"TEST_REPO": "o/r",
		}
	)
	result = subprocess.run([bash, "-c", snippet], env=env, capture_output=True, text=True, check=False, timeout=60)
	return result.returncode, output.read_text(encoding="utf-8"), result.stdout


def test_orchestrate_capacity_check_reads_emitted_log_line_only() -> None:
	if shutil.which("bash") is None:
		raise unittest.SkipTest("bash is not installed")
	import tempfile

	emitted = "2026-10-27T00:40:51.1234567Z AI_ENGINE_CAPACITY_WAIT role=ORCHESTRATE outcome=deferred stage=orchestrate\n"
	echoed = (
		"2026-10-27T00:40:50.1234567Z \x1b[36;1m    echo \"AI_ENGINE_CAPACITY_WAIT role=ORCHESTRATE "
		"outcome=deferred stage=orchestrate\"\x1b[0m\n"
	)
	with tempfile.TemporaryDirectory() as tmp:
		rc, output, stdout = _run_orchestrate_capacity_check(Path(tmp), echoed + emitted)
		assert rc == 1
		assert "status=claude_capacity" in output
		assert "evidence=run_log" in stdout
	with tempfile.TemporaryDirectory() as tmp:
		rc, output, _ = _run_orchestrate_capacity_check(Path(tmp), echoed)
		assert rc == 0
		assert "claude_capacity" not in output
	with tempfile.TemporaryDirectory() as tmp:
		rc, output, stdout = _run_orchestrate_capacity_check(Path(tmp), echoed, trackers="1")
		assert rc == 1
		assert "evidence=capacity_tracker" in stdout
	with tempfile.TemporaryDirectory() as tmp:
		rc, output, stdout = _run_orchestrate_capacity_check(Path(tmp), emitted, log_fail=True)
		assert rc == 0
		assert "claude_capacity" not in output
		assert "Could not read 2 job log(s)" in stdout


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

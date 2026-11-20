#!/usr/bin/env python3
"""Check-failure triage target-files guard (issue #9823, plan Phase 2).

With PR-branch routing on, the triage issue sends its fix to the failing PR's
head branch. When none of the failing files the CI log names exist on that
branch but all of them exist on the default branch, the guard clears the
route so the fix lands on the default branch. The check is local only
(`git cat-file -e`, no API call, no fetch); any ref that does not resolve
keeps today's route.

The function is extracted from scripts/check_failure_triage.sh and run under
bash with a fake `git` on PATH and the real heal helper extracting the files.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TRIAGE_SCRIPT = REPO_ROOT / "scripts" / "check_failure_triage.sh"
HEAL_PY = REPO_ROOT / "scripts" / "workflow_failure_heal.py"
TRIAGE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "check_failure_triage.yml"

HEAD_SHA = "a" * 40
TARGET_TIP = "b" * 40
DEFAULT_TIP = "c" * 40
FAILING_FILE = "tests/test_widget.py"
CONTEXT = f"FAILED {FAILING_FILE}::test_render - AssertionError: boom\n"

FAKE_GIT = textwrap.dedent(
	"""\
	#!/usr/bin/env python3
	import json, os, sys
	args = sys.argv[1:]
	with open(os.environ["FAKE_GIT_CALLS"], "a", encoding="utf-8") as calls:
		calls.write(json.dumps(args) + "\\n")
	if args[:1] == ["-C"]:
		args = args[2:]
	fixture = json.load(open(os.environ["FAKE_GIT_FIXTURE"], encoding="utf-8"))
	if args[:3] == ["rev-parse", "--verify", "--quiet"]:
		ref = args[3]
		if ref.endswith("^{commit}") and ref[: -len("^{commit}")] in fixture["refs"]:
			print(fixture["refs"][ref[: -len("^{commit}")]])
			sys.exit(0)
		sys.exit(1)
	if args[:2] == ["cat-file", "-e"]:
		sys.exit(0 if args[2] in fixture["objects"] else 1)
	sys.exit(2)
	"""
)


def _guard_function() -> str:
	script = TRIAGE_SCRIPT.read_text(encoding="utf-8")
	return "triage_target_files_guard()\n{" + script.split("\ntriage_target_files_guard()\n{", 1)[1].split("\n}\n", 1)[0] + "\n}\n"


def _run_guard(tmp_path: Path, *, refs: dict[str, str], objects: list[str], switch: str | None = None,
		context: str = CONTEXT, head_sha: str = HEAD_SHA) -> tuple[str, list[str], list[list[str]]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	fake_git = bin_dir / "git"
	fake_git.write_text(FAKE_GIT, encoding="utf-8")
	fake_git.chmod(0o755)
	fixture = tmp_path / "fixture.json"
	fixture.write_text(json.dumps({"refs": refs, "objects": objects}), encoding="utf-8")
	calls = tmp_path / "git_calls.jsonl"
	calls.write_text("", encoding="utf-8")
	context_file = tmp_path / "pr_check_runs_context.txt"
	context_file.write_text(context, encoding="utf-8")
	harness = tmp_path / "harness.sh"
	harness.write_text(
		"set -euo pipefail\n"
		'log() { echo "CHECK_TRIAGE $*"; }\n'
		'TARGET_FILES_GUARD_ENABLED="${CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED:-true}"\n'
		'if [ "${TARGET_FILES_GUARD_ENABLED,,}" = "false" ]; then TARGET_FILES_GUARD_ENABLED="false"; else TARGET_FILES_GUARD_ENABLED="true"; fi\n'
		+ _guard_function()
		+ "triage_target_files_guard || true\n"
		'echo "RESULT target=${ROUTING_TARGET_BRANCH}"\n',
		encoding="utf-8",
	)
	env = {
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"HOME": str(tmp_path),
		"PYTHONDONTWRITEBYTECODE": "1",
		"FAKE_GIT_FIXTURE": str(fixture),
		"FAKE_GIT_CALLS": str(calls),
		"GITHUB_WORKSPACE": str(tmp_path),
		"ROUTING_TARGET_BRANCH": "feature/widget",
		"ROUTING_DEFAULT_BRANCH": "main",
		"HEAD_SHA": head_sha,
		"HISTORICAL_MODE": "false",
		"UPSTREAM_HEAL_PY": str(HEAL_PY),
		"PR_CHECK_RUNS_CONTEXT_FILE": str(context_file),
		"GH_TOKEN": "must-not-reach-git",
	}
	if switch is not None:
		env["CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED"] = switch
	result = subprocess.run(["bash", str(harness)], env=env, capture_output=True, text=True, timeout=120, check=False)
	assert result.returncode == 0, result.stderr
	lines = result.stdout.splitlines()
	target = next(line for line in lines if line.startswith("RESULT target=")).split("=", 1)[1]
	git_calls = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines() if line.strip()]
	return target, [line for line in lines if line.startswith("CHECK_TRIAGE ")], git_calls


DEFAULT_REFS = {"refs/remotes/origin/main": DEFAULT_TIP}


def test_fallback_when_target_lacks_every_failing_file(tmp_path: Path) -> None:
	target, logs, git_calls = _run_guard(
		tmp_path, refs=DEFAULT_REFS, objects=[f"{HEAD_SHA}^{{commit}}", f"{DEFAULT_TIP}:{FAILING_FILE}"],
	)
	assert target == ""
	assert logs == ["CHECK_TRIAGE routing outcome=fallback reason=target_branch_lacks_files target_branch=main"]
	# Local reads only, in the workspace, never a fetch.
	assert git_calls and all(call[:2] == ["-C", str(tmp_path)] for call in git_calls)
	assert {call[2] for call in git_calls} <= {"rev-parse", "cat-file"}


def test_no_fallback_when_a_file_exists_on_target(tmp_path: Path) -> None:
	target, logs, _calls = _run_guard(
		tmp_path,
		refs={**DEFAULT_REFS, "refs/remotes/origin/feature/widget": TARGET_TIP},
		objects=[f"{TARGET_TIP}:{FAILING_FILE}", f"{DEFAULT_TIP}:{FAILING_FILE}"],
	)
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=files_on_target target_branch=feature/widget"]


def test_no_fallback_when_default_branch_also_lacks_the_files(tmp_path: Path) -> None:
	target, logs, _calls = _run_guard(tmp_path, refs=DEFAULT_REFS, objects=[f"{HEAD_SHA}^{{commit}}"])
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=files_absent_on_default target_branch=feature/widget"]


@pytest.mark.parametrize("switch", ["false", "FALSE"])
def test_switch_off_keeps_target_and_never_runs_git(tmp_path: Path, switch: str) -> None:
	target, logs, git_calls = _run_guard(
		tmp_path, refs=DEFAULT_REFS, objects=[f"{HEAD_SHA}^{{commit}}", f"{DEFAULT_TIP}:{FAILING_FILE}"], switch=switch,
	)
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=target_files_guard_disabled target_branch=feature/widget"]
	assert git_calls == []


def test_unresolvable_refs_and_missing_files_keep_target(tmp_path: Path) -> None:
	no_default_dir = tmp_path / "no-default"
	no_default_dir.mkdir()
	target, logs, _calls = _run_guard(no_default_dir, refs={}, objects=[f"{HEAD_SHA}^{{commit}}"])
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=default_ref_unavailable target_branch=feature/widget"]
	no_target_dir = tmp_path / "no-target"
	no_target_dir.mkdir()
	target, logs, _calls = _run_guard(no_target_dir, refs=DEFAULT_REFS, objects=[f"{DEFAULT_TIP}:{FAILING_FILE}"])
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=target_ref_unavailable target_branch=feature/widget"]
	no_files_dir = tmp_path / "no-files"
	no_files_dir.mkdir()
	target, logs, git_calls = _run_guard(no_files_dir, refs=DEFAULT_REFS, objects=[], context="all good\n")
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=no_failing_files target_branch=feature/widget"]
	assert git_calls == []
	# Hostile paths from the log never reach git.
	hostile_dir = tmp_path / "hostile"
	hostile_dir.mkdir()
	target, logs, git_calls = _run_guard(hostile_dir, refs=DEFAULT_REFS, objects=[],
		context="FAILED ../../etc/passwd.py::test_x - boom\nFAILED -rf.py::test_y - boom\n")
	assert target == "feature/widget"
	assert all("passwd" not in arg and not arg.startswith("-rf") for call in git_calls for arg in call)


def test_guard_placement_switch_default_and_no_api_calls() -> None:
	script = TRIAGE_SCRIPT.read_text(encoding="utf-8")
	assert 'TARGET_FILES_GUARD_ENABLED="${CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED:-true}"' in script
	body = _guard_function()
	code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
	assert "gh " not in code and "gh_retry" not in code and "curl" not in code and "fetch" not in code
	assert '"${UPSTREAM_HEAL_PY}" heal-scope failing-tests' in body
	assert "-u GH_TOKEN -u GITHUB_TOKEN -u OPENROUTER_API_KEY -u TG_BOT_SECRET" in body
	call = script.index("\ntriage_target_files_guard || true\n")
	assert script.index('ROUTING_TARGET_BRANCH="${HEAD_REF}"') < call
	assert script.index("PR_CHECK_RUNS_CONTEXT_FILE=\"${PR_CHECK_RUNS_CONTEXT_FILE}\" \\") < call
	assert call < script.index('if [ "${TRIAGE_STAGE}" = "collect" ]; then\n\tjq -n --arg pr_number')
	assert script.count("\ntriage_target_files_guard || true\n") == 1
	workflow = TRIAGE_WORKFLOW.read_text(encoding="utf-8")
	assert workflow.count("      CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED: ${{ vars.CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED || 'true' }}\n") == 1

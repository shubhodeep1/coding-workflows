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
	git_dir = ""
	if args[:1] == ["-C"]:
		git_dir = args[1]
		args = args[2:]
	fixture = json.load(open(os.environ["FAKE_GIT_FIXTURE"], encoding="utf-8"))
	# Optional per-directory fixtures (the staged default-branch checkout).
	fixture = fixture.get("dirs", {}).get(git_dir, fixture)
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
		context: str = CONTEXT, head_sha: str = HEAD_SHA, dirs: dict[str, dict] | None = None,
		extra_env: dict[str, str] | None = None) -> tuple[str, list[str], list[list[str]]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	fake_git = bin_dir / "git"
	fake_git.write_text(FAKE_GIT, encoding="utf-8")
	fake_git.chmod(0o755)
	fixture = tmp_path / "fixture.json"
	fixture.write_text(json.dumps({"refs": refs, "objects": objects, "dirs": dirs or {}}), encoding="utf-8")
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
	env.update(extra_env or {})
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


def test_advanced_head_keeps_target_without_a_branch_ref(tmp_path: Path) -> None:
	# The failing commit is no longer the PR head: it does not stand for the
	# branch, so the route is kept (files may have been added since).
	target, logs, _calls = _run_guard(
		tmp_path, refs=DEFAULT_REFS, objects=[f"{HEAD_SHA}^{{commit}}", f"{DEFAULT_TIP}:{FAILING_FILE}"],
		extra_env={"PR_HEAD_SHA_CURRENT": "D" * 40},
	)
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=target_head_advanced target_branch=feature/widget"]
	# Same head (any case): the fallback still applies.
	same_dir = tmp_path / "same-head"
	same_dir.mkdir()
	target, logs, _calls = _run_guard(
		same_dir, refs=DEFAULT_REFS, objects=[f"{HEAD_SHA}^{{commit}}", f"{DEFAULT_TIP}:{FAILING_FILE}"],
		extra_env={"PR_HEAD_SHA_CURRENT": HEAD_SHA.upper()},
	)
	assert target == ""
	assert logs == ["CHECK_TRIAGE routing outcome=fallback reason=target_branch_lacks_files target_branch=main"]


def test_fallback_reads_the_staged_default_branch_checkout(tmp_path: Path) -> None:
	# The triage checkout is a depth-1 checkout of the failing SHA, with no
	# default-branch ref; the workflow stages a depth-1 default-branch checkout
	# whose local branch is named after the default branch.
	tree_dir = tmp_path / "default-tree"
	tree_dir.mkdir()
	target, logs, git_calls = _run_guard(
		tmp_path, refs={}, objects=[f"{HEAD_SHA}^{{commit}}"],
		dirs={str(tree_dir): {"refs": {"refs/heads/main": DEFAULT_TIP}, "objects": [f"{DEFAULT_TIP}:{FAILING_FILE}"]}},
		extra_env={"CHECK_TRIAGE_DEFAULT_BRANCH_TREE_DIR": str(tree_dir)},
	)
	assert target == ""
	assert logs == ["CHECK_TRIAGE routing outcome=fallback reason=target_branch_lacks_files target_branch=main"]
	assert ["-C", str(tree_dir), "cat-file", "-e", f"{DEFAULT_TIP}:{FAILING_FILE}"] in git_calls
	# The PR head is still read from the workspace only.
	assert all(call[:2] == ["-C", str(tmp_path)] for call in git_calls if call[2] == "cat-file" and call[3:4] == ["-e"] and call[4].startswith(HEAD_SHA))
	# A staged checkout of another branch (no refs/heads/main) never counts.
	other_dir = tmp_path / "other"
	other_dir.mkdir()
	other_tree = other_dir / "default-tree"
	other_tree.mkdir()
	target, logs, _calls = _run_guard(
		other_dir, refs={}, objects=[f"{HEAD_SHA}^{{commit}}"],
		dirs={str(other_tree): {"refs": {"refs/heads/develop": DEFAULT_TIP}, "objects": [f"{DEFAULT_TIP}:{FAILING_FILE}"]}},
		extra_env={"CHECK_TRIAGE_DEFAULT_BRANCH_TREE_DIR": str(other_tree)},
	)
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=default_ref_unavailable target_branch=feature/widget"]


def test_helper_failure_logs_its_own_reason_and_keeps_target(tmp_path: Path) -> None:
	failing_helper = tmp_path / "failing_helper.py"
	failing_helper.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
	target, logs, git_calls = _run_guard(
		tmp_path, refs=DEFAULT_REFS, objects=[f"{HEAD_SHA}^{{commit}}", f"{DEFAULT_TIP}:{FAILING_FILE}"],
		extra_env={"UPSTREAM_HEAL_PY": str(failing_helper)},
	)
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=helper_failed target_branch=feature/widget"]
	assert git_calls == []
	malformed_dir = tmp_path / "malformed"
	malformed_dir.mkdir()
	malformed_helper = malformed_dir / "malformed_helper.py"
	malformed_helper.write_text("print('not json')\n", encoding="utf-8")
	target, logs, _calls = _run_guard(
		malformed_dir, refs=DEFAULT_REFS, objects=[], extra_env={"UPSTREAM_HEAL_PY": str(malformed_helper)},
	)
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=helper_failed target_branch=feature/widget"]


def _base_gate_recheck_block() -> str:
	script = TRIAGE_SCRIPT.read_text(encoding="utf-8")
	after_guard = script.split("\ntriage_target_files_guard || true\n", 1)[1]
	return after_guard.split('\nif [ "${TRIAGE_STAGE}" = "collect" ]; then', 1)[0]


@pytest.mark.parametrize(
	("deferred", "target", "base_ref", "expected"),
	[
		("true", "", "main", ["CHECK_TRIAGE base_gate outcome=recheck reason=target_files_fallback base=main pr=17", "BASE_GATE_CHECK_RAN"]),
		("true", "", "a/../main", ["CHECK_TRIAGE base_gate outcome=unknown reason=base_ref_unavailable pr=17"]),
		("true", "feature/widget", "main", []),
		("false", "", "main", []),
	],
)
def test_base_gate_reruns_when_the_guard_clears_the_route(tmp_path: Path, deferred: str, target: str, base_ref: str, expected: list[str]) -> None:
	harness = tmp_path / "recheck.sh"
	harness.write_text(
		"set -euo pipefail\n"
		'log() { echo "CHECK_TRIAGE $*"; }\n'
		"triage_base_gate_check() { echo BASE_GATE_CHECK_RAN; }\n"
		+ _base_gate_recheck_block() + "\n",
		encoding="utf-8",
	)
	env = {
		"PATH": os.environ.get("PATH", ""),
		"BASE_GATE_TARGET_ROUTE_DEFERRED": deferred,
		"ROUTING_TARGET_BRANCH": target,
		"INTEGRATION_BRANCH": "",
		"ROUTING_REF": "",
		"BASE_REF": base_ref,
		"PR_NUMBER": "17",
	}
	result = subprocess.run(["bash", str(harness)], env=env, capture_output=True, text=True, timeout=60, check=False)
	assert result.returncode == 0, result.stderr
	assert result.stdout.splitlines() == expected


def test_base_gate_is_a_function_and_the_route_skip_is_recorded() -> None:
	script = TRIAGE_SCRIPT.read_text(encoding="utf-8")
	definition = script.index("\ntriage_base_gate_check()\n{\n")
	chain = script.index('BASE_GATE_TARGET_ROUTE_DEFERRED="false"\nif [ "${HISTORICAL_MODE}" = "true" ]; then')
	assert definition < chain
	route_arm = script.split('elif [ -n "${ROUTING_TARGET_BRANCH}" ]; then', 1)[1].split("\nelif ", 1)[0]
	assert "reason=target_branch_route" in route_arm and 'BASE_GATE_TARGET_ROUTE_DEFERRED="true"' in route_arm
	assert script.count("\n\ttriage_base_gate_check\n") == 1
	assert script.count("\n\t\ttriage_base_gate_check\n") == 1


def test_workflow_stages_the_default_branch_checkout_without_credentials() -> None:
	workflow = TRIAGE_WORKFLOW.read_text(encoding="utf-8")
	step = workflow.split("      - name: Checkout default branch for target-files guard\n", 1)[1].split("\n      - name: ", 1)[0]
	assert "ref: ${{ github.event.repository.default_branch }}" in step
	assert "fetch-depth: 1" in step and "persist-credentials: false" in step and "continue-on-error: true" in step
	assert "inputs.historical_run_id == ''" in step and "vars.CHECK_TRIAGE_PR_BRANCH_ROUTING_ENABLED" in step
	stage = workflow.split("      - name: Stage default-branch tree for target-files guard\n", 1)[1].split("\n      - name: ", 1)[0]
	assert 'echo "CHECK_TRIAGE_DEFAULT_BRANCH_TREE_DIR=${triage_default_tree_dir}" >> "$GITHUB_ENV"' in stage
	assert "rm -rf .codex-triage-default-branch" in stage and "${{" not in stage.split("run: |", 1)[1]
	assert workflow.index("Checkout default branch for target-files guard") < workflow.index("Collect check-failure context")


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


def test_truncated_failing_file_list_keeps_target(tmp_path: Path) -> None:
	# The helper caps its list at 20 without a flag: a later failing file may
	# exist on the head, so a capped list never clears the route.
	files = [f"tests/test_mod{i}.py" for i in range(25)]
	context = "".join(f"FAILED {name}::test_x - AssertionError: boom\n" for name in files)
	target, logs, _calls = _run_guard(
		tmp_path, refs=DEFAULT_REFS, objects=[f"{HEAD_SHA}^{{commit}}"] + [f"{DEFAULT_TIP}:{name}" for name in files],
		context=context,
	)
	assert target == "feature/widget"
	assert logs == ["CHECK_TRIAGE routing outcome=keep reason=files_truncated target_branch=feature/widget"]

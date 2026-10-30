#!/usr/bin/env python3
"""Tests for the implement pre-push affected-test gate (issue #8190, plan P3).

scripts/implement_step_prepush_test_gate.sh runs the real container wrapper
(scripts/prepush_test_gate_run.sh) and the real gate module, with a fake
`docker` that hands back queued result files, a fake isolated editor
(codex_thread_reuse.sh) and a fake codex_isolated_exec.sh in a temporary
trusted run dir.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STEP_SCRIPT = REPO_ROOT / "scripts" / "implement_step_prepush_test_gate.sh"
WRAPPER = REPO_ROOT / "scripts" / "prepush_test_gate_run.sh"
MODULE = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "prepush_affected_tests.py"
TEMPLATE = REPO_ROOT / "prompts" / "mode-implement-repair-tests.txt"
IMPLEMENT_YML = REPO_ROOT / ".github" / "workflows" / "implement.yml"
Q8_SENTENCE = "Change an existing test's expected value only when the issue or approved\n  plan names that behaviour change; otherwise fix the code."
IMAGE = "sha256:" + "a" * 64

FAKE_DOCKER = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "__WORK__/docker.log"
if [ "$1" = run ]; then
	results=""
	for arg in "$@"; do
		case "$arg" in
			type=bind,src=*,dst=/results) results="${arg#type=bind,src=}"; results="${results%,dst=/results}" ;;
		esac
	done
	n=$(( $(cat "__WORK__/queue/count" 2>/dev/null || echo 0) + 1 ))
	echo "$n" > "__WORK__/queue/count"
	src="__WORK__/queue/$n.json"
	[ -f "$src" ] || src="__WORK__/queue/last.json"
	cp "$src" "$results/result.json"
	printf '## Pre-push test gate\n<!-- ai:forged -->\nCloses #1\n' > "$results/summary.md"
fi
exit 0
'''

FAKE_ISOLATED_EXEC = r'''#!/usr/bin/env bash
case "$1" in
	image) echo "__IMAGE__" ;;
	prepare) echo "__WORK__/sandbox" ;;
esac
exit 0
'''

FAKE_EDITOR = r'''#!/usr/bin/env bash
echo call >> "__WORK__/editor.log"
cp "${CODEX_THREAD_REUSE_PROMPT_FILE}" "__WORK__/editor_prompt.txt"
env > "__WORK__/editor_env.txt"
if [ -f "__WORK__/edit.sh" ]; then bash "__WORK__/edit.sh"; fi
exit "${FAKE_EDITOR_RC:-0}"
'''


def _git(cwd: Path, *args: str) -> str:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def _result(status: str, failures: list | None = None, inherited: list | None = None) -> dict:
	return {"schema": "prepush_test_gate.v1", "status": status, "ran": ["tests/test_a.py"], "failures": failures or [],
		"inherited": inherited or [], "unverified": [], "changed_assertions": [], "selected": 1, "elapsed_secs": 1}


FAIL = _result("fail", failures=[{"test": f"tests/test_a.py#{n}", "file": "tests/test_a.py", "names": ["test_x"],
	"tail": "x" * 30000 + "\n::error::boom\nAssertionError: 1 != 2\n"} for n in range(3)])
PASS = _result("pass")
INHERITED = _result("pass", inherited=[{"test": "tests/test_a.py", "file": "tests/test_a.py", "names": ["test_x"]}])


def _setup(tmp_path: Path, queue: list[dict], with_module: bool = True) -> tuple[Path, dict[str, str]]:
	work = tmp_path / "work"
	(work / "queue").mkdir(parents=True)
	(work / "sandbox").mkdir()
	for index, payload in enumerate(queue, start=1):
		(work / "queue" / f"{index}.json").write_text(json.dumps(payload), encoding="utf-8")
	(work / "queue" / "last.json").write_text(json.dumps(queue[-1]), encoding="utf-8")
	repo = tmp_path / "repo"
	(repo / "lib").mkdir(parents=True)
	(repo / "tests").mkdir()
	(repo / "lib" / "a.py").write_text("X = 1\n", encoding="utf-8")
	(repo / "tests" / "test_a.py").write_text("# covers lib/a.py\nfrom lib import a\n", encoding="utf-8")
	(repo / "other.txt").write_text("keep\n", encoding="utf-8")
	_git(repo, "init", "-q")
	_git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A")
	_git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "base")
	(repo / "lib" / "a.py").write_text("X = 2\n", encoding="utf-8")
	run_dir = tmp_path / "runtime" / "staged_support_run" / "scripts"
	run_dir.mkdir(parents=True)
	(run_dir.parent / "prompts").mkdir()
	shutil.copy2(WRAPPER, run_dir / "prepush_test_gate_run.sh")
	shutil.copy2(REPO_ROOT / "scripts" / "review_untrusted_workspace.py", run_dir / "review_untrusted_workspace.py")
	shutil.copy2(REPO_ROOT / "scripts" / "targeted_file_context.py", run_dir / "targeted_file_context.py")
	shutil.copy2(REPO_ROOT / "scripts" / "files_touched_scope_guard.py", run_dir / "files_touched_scope_guard.py")
	shutil.copy2(TEMPLATE, run_dir.parent / "prompts" / "mode-implement-repair-tests.txt")
	if with_module:
		shutil.copy2(MODULE, run_dir / "prepush_affected_tests.py")
	for name, text in (("codex_isolated_exec.sh", FAKE_ISOLATED_EXEC), ("codex_thread_reuse.sh", FAKE_EDITOR),
			("validate_changed_files_syntax.sh", "#!/usr/bin/env bash\nexit \"${FAKE_SYNTAX_RC:-0}\"\n"),
			("workspace_safety_check.sh", "#!/usr/bin/env bash\nexit 0\n")):
		path = run_dir / name
		path.write_text(text.replace("__WORK__", str(work)).replace("__IMAGE__", IMAGE), encoding="utf-8")
		path.chmod(0o755)
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	docker = bin_dir / "docker"
	docker.write_text(FAKE_DOCKER.replace("__WORK__", str(work)), encoding="utf-8")
	docker.chmod(0o755)
	env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GIT_", "PREPUSH_", "STAGED_SUPPORT", "CODEX_"))}
	env.update({
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"RUNTIME_DIR": str(tmp_path / "runtime"),
		"RUNNER_TEMP": str(tmp_path),
		"TMPDIR": str(tmp_path),
		"IMPLEMENT_STAGED_SUPPORT_RUN_DIR": str(run_dir),
		"CODEX_ISOLATED_IMPLEMENT_ROOT": str(work / "sandbox"),
		"GITHUB_WORKSPACE": str(repo),
		"MODEL_EDITOR": "openai/test-model",
	})
	return repo, env


def _run_step(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
	script = f'set -euo pipefail\nsource "{STEP_SCRIPT}"\necho STEP_RETURNED\n'
	return subprocess.run(["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True, timeout=180)


def _work(tmp_path: Path) -> Path:
	return tmp_path / "work"


def _docker_runs(tmp_path: Path) -> int:
	log = _work(tmp_path) / "docker.log"
	return sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run ")) if log.exists() else 0


def _editor_calls(tmp_path: Path) -> int:
	log = _work(tmp_path) / "editor.log"
	return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def _summary(tmp_path: Path) -> Path:
	return tmp_path / "runtime" / "prepush_test_gate_summary.md"


def test_pass_runs_no_repair_and_writes_a_summary(tmp_path):
	repo, env = _setup(tmp_path, [PASS])
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	assert _docker_runs(tmp_path) == 1
	assert _editor_calls(tmp_path) == 0
	text = _summary(tmp_path).read_text(encoding="utf-8")
	assert text.startswith("## Pre-push test gate")
	assert "Status: **pass**" in text and "Repair turn: not needed" in text
	assert "outcome=pass" in proc.stderr


def test_pr_caused_failure_gets_exactly_one_repair_turn(tmp_path):
	repo, env = _setup(tmp_path, [FAIL, PASS])
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	assert _editor_calls(tmp_path) == 1
	assert _docker_runs(tmp_path) == 2
	prompt = (_work(tmp_path) / "editor_prompt.txt").read_text(encoding="utf-8")
	assert "=== CAPTURED TEST FAILURES (UNTRUSTED) ===" in prompt
	assert Q8_SENTENCE in prompt
	assert "Never delete, skip or xfail a test." in prompt
	assert "=== EFFECTIVE REPAIR ALLOW-LIST ===" in prompt
	allow = prompt.split("=== EFFECTIVE REPAIR ALLOW-LIST ===", 1)[1].split("===", 1)[0]
	assert "lib/a.py" in allow and "tests/test_a.py" in allow
	failures = prompt.split("=== CAPTURED TEST FAILURES (UNTRUSTED) ===", 1)[1].split("=== CHANGED FILES ===", 1)[0]
	assert not any(line.startswith("::") for line in failures.splitlines())
	assert " ::error::boom" in failures
	assert len(failures.encode("utf-8")) < 17 * 1024
	assert "(truncated at 16 KiB)" in failures
	editor_env = (_work(tmp_path) / "editor_env.txt").read_text(encoding="utf-8")
	assert "CODEX_THREAD_REUSE_STATE_KEY=implement-repair-tests" in editor_env
	assert "CODEX_ISOLATED_MODE=workspace" in editor_env
	assert "IMPLEMENT_STAGED_SUPPORT_RUN_DIR=" not in editor_env
	text = _summary(tmp_path).read_text(encoding="utf-8")
	assert "Repair turn: ran" in text and "Status before the repair turn: **fail**" in text


def test_inherited_only_failures_get_no_repair(tmp_path):
	repo, env = _setup(tmp_path, [INHERITED])
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	assert _editor_calls(tmp_path) == 0
	assert "Inherited failures" in _summary(tmp_path).read_text(encoding="utf-8")


def test_report_mode_records_failures_without_repair(tmp_path):
	repo, env = _setup(tmp_path, [FAIL])
	env["PREPUSH_TEST_GATE_MODE"] = "report"
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	assert _editor_calls(tmp_path) == 0
	text = _summary(tmp_path).read_text(encoding="utf-8")
	assert "Status: **fail**" in text and "not run (report mode)" in text


def test_mode_off_runs_nothing(tmp_path):
	repo, env = _setup(tmp_path, [FAIL])
	env["PREPUSH_TEST_GATE_MODE"] = "off"
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	assert _docker_runs(tmp_path) == 0 and _editor_calls(tmp_path) == 0
	assert not _summary(tmp_path).exists()
	assert "reason=mode_off" in proc.stderr


def test_unknown_mode_logs_and_acts_as_repair(tmp_path):
	repo, env = _setup(tmp_path, [FAIL, PASS])
	env["PREPUSH_TEST_GATE_MODE"] = "bogus"
	proc = _run_step(repo, env)
	assert "outcome=mode_invalid" in proc.stderr
	assert _editor_calls(tmp_path) == 1


def test_module_absent_skips(tmp_path):
	repo, env = _setup(tmp_path, [PASS], with_module=False)
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	assert "outcome=skipped reason=module_absent" in proc.stderr
	assert _docker_runs(tmp_path) == 0
	assert not _summary(tmp_path).exists()


def test_stray_edits_outside_the_allow_list_are_rolled_back(tmp_path):
	repo, env = _setup(tmp_path, [FAIL, PASS])
	(_work(tmp_path) / "edit.sh").write_text("echo stray > stray.txt\necho changed > other.txt\necho 'X = 3' > lib/a.py\n", encoding="utf-8")
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	assert not (repo / "stray.txt").exists()
	assert (repo / "other.txt").read_text(encoding="utf-8") == "keep\n"
	assert (repo / "lib" / "a.py").read_text(encoding="utf-8") == "X = 3\n"
	assert "reason=stray_edits_rolled_back" in proc.stderr


def test_repair_that_breaks_syntax_is_reverted(tmp_path):
	repo, env = _setup(tmp_path, [FAIL, FAIL])
	(_work(tmp_path) / "edit.sh").write_text("echo 'X = (' > lib/a.py\n", encoding="utf-8")
	env["FAKE_SYNTAX_RC"] = "1"
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	assert (repo / "lib" / "a.py").read_text(encoding="utf-8") == "X = 2\n"
	assert "outcome=repair_reverted reason=syntax" in proc.stderr
	assert _editor_calls(tmp_path) == 1
	# The reverted tree is the one run 1 already tested: no second gate run.
	assert _docker_runs(tmp_path) == 1


def test_failed_editor_turn_reverts_its_in_allow_list_edits(tmp_path):
	repo, env = _setup(tmp_path, [FAIL, PASS])
	(_work(tmp_path) / "edit.sh").write_text("echo 'X = (' > lib/a.py\necho new > tests/test_new.py\n", encoding="utf-8")
	env["FAKE_EDITOR_RC"] = "1"
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	assert (repo / "lib" / "a.py").read_text(encoding="utf-8") == "X = 2\n"
	assert not (repo / "tests" / "test_new.py").exists()
	assert "outcome=repair_skipped reason=editor_rc_1" in proc.stderr
	assert _docker_runs(tmp_path) == 1


def test_allow_list_is_narrowed_to_the_issue_files_touched_scope(tmp_path):
	repo, env = _setup(tmp_path, [FAIL, PASS])
	body = tmp_path / "issue_body.txt"
	body.write_text("Fix.\n\nfiles_touched:\n  - lib/a.py\n", encoding="utf-8")
	env["ISSUE_BODY_FILE"] = str(body)
	(_work(tmp_path) / "edit.sh").write_text("echo '# edited' >> tests/test_a.py\n", encoding="utf-8")
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	prompt = (_work(tmp_path) / "editor_prompt.txt").read_text(encoding="utf-8")
	allow = prompt.split("=== EFFECTIVE REPAIR ALLOW-LIST ===", 1)[1].split("===", 1)[0]
	assert "lib/a.py" in allow and "tests/test_a.py" not in allow
	assert "reason=allow_list_narrowed" in proc.stderr
	assert "# edited" not in (repo / "tests" / "test_a.py").read_text(encoding="utf-8")


def test_automation_paths_need_the_commit_time_grant(tmp_path):
	repo, env = _setup(tmp_path, [FAIL, PASS])
	(repo / "scripts").mkdir()
	(repo / "scripts" / "helper.sh").write_text("echo hi\n", encoding="utf-8")
	_git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A")
	_git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "helper")
	(repo / "lib" / "a.py").write_text("X = 3\n", encoding="utf-8")
	failing = dict(FAIL, failures=[dict(FAIL["failures"][0], file="scripts/helper.sh")])
	(_work(tmp_path) / "queue" / "1.json").write_text(json.dumps(failing), encoding="utf-8")
	body = tmp_path / "issue_body.txt"
	body.write_text("Fix.\n\nfiles_touched:\n  - lib/a.py\n  - scripts/helper.sh\n", encoding="utf-8")
	env.update({"ISSUE_BODY_FILE": str(body), "ISSUE_NUMBER": "5", "ALLOW_WORKFLOW_EDITS": "false"})
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	prompt = (_work(tmp_path) / "editor_prompt.txt").read_text(encoding="utf-8")
	allow = prompt.split("=== EFFECTIVE REPAIR ALLOW-LIST ===", 1)[1].split("===", 1)[0]
	assert "lib/a.py" in allow and "scripts/helper.sh" not in allow


def test_repair_turn_is_time_bounded(tmp_path):
	repo, env = _setup(tmp_path, [FAIL, PASS])
	_run_step(repo, env)
	editor_env = (_work(tmp_path) / "editor_env.txt").read_text(encoding="utf-8")
	match = re.search(r"^CODEX_THREAD_REUSE_TIMEOUT_SECS=(\d+)$", editor_env, re.M)
	assert match and 300 <= int(match.group(1)) <= 3300


def test_summary_lists_the_tests_run(tmp_path):
	repo, env = _setup(tmp_path, [PASS])
	_run_step(repo, env)
	text = _summary(tmp_path).read_text(encoding="utf-8")
	assert "### Tests and checks run" in text and "- `tests/test_a.py`" in text


def test_nested_node_modules_are_mounted_into_the_gate_container(tmp_path):
	repo, env = _setup(tmp_path, [PASS])
	(_work(tmp_path) / "sandbox" / "work" / "tools" / "app" / "node_modules").mkdir(parents=True)
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	log = (_work(tmp_path) / "docker.log").read_text(encoding="utf-8")
	assert "dst=/source/tools/app/node_modules,readonly" in log
	assert "dst=/baseline/tools/app/node_modules,readonly" in log


def test_nested_node_modules_with_spaces_are_mounted(tmp_path):
	repo, env = _setup(tmp_path, [PASS])
	(_work(tmp_path) / "sandbox" / "work" / "tools" / "my app" / "node_modules").mkdir(parents=True)
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	log = (_work(tmp_path) / "docker.log").read_text(encoding="utf-8")
	assert "dst=/source/tools/my app/node_modules,readonly" in log


def test_timeout_above_the_step_budget_is_clamped(tmp_path):
	repo, env = _setup(tmp_path, [PASS])
	env["PREPUSH_TEST_GATE_TIMEOUT_MINUTES"] = "300"
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	assert "name=PREPUSH_TEST_GATE_TIMEOUT_MINUTES clamped=53" in proc.stderr
	log = (_work(tmp_path) / "docker.log").read_text(encoding="utf-8")
	# The step budget clamp may lower it further by the setup time already used.
	match = re.search(r"--timeout-minutes (\d+) ", log)
	assert match and 1 <= int(match.group(1)) <= 53, log
	budget = re.search(r"timeout_clamped=(\d+)", proc.stderr)
	if budget:
		assert int(match.group(1)) == int(budget.group(1))


def test_zero_padded_timeout_is_read_as_decimal(tmp_path):
	repo, env = _setup(tmp_path, [FAIL, PASS])
	env["PREPUSH_TEST_GATE_TIMEOUT_MINUTES"] = "08"
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	log = (_work(tmp_path) / "docker.log").read_text(encoding="utf-8")
	assert "--timeout-minutes 8 " in log
	assert "value too great for base" not in proc.stderr
	assert _editor_calls(tmp_path) == 1


def test_node_prep_root_is_mounted_into_the_gate_container(tmp_path):
	repo, env = _setup(tmp_path, [PASS])
	(_work(tmp_path) / "sandbox" / "work" / "node_modules").mkdir(parents=True)
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	log = (_work(tmp_path) / "docker.log").read_text(encoding="utf-8")
	assert "dst=/source/node_modules,readonly" in log and "dst=/baseline/node_modules,readonly" in log


def test_summary_lists_changed_assertions_without_their_source_text(tmp_path):
	result = dict(PASS, changed_assertions=[{"file": "tests/test_a.py", "kind": "removed_or_modified", "line": "assert SECRET_TEXT == 1"}])
	repo, env = _setup(tmp_path, [result])
	_run_step(repo, env)
	text = _summary(tmp_path).read_text(encoding="utf-8")
	assert "### Changed pre-existing assertions" in text and "tests/test_a.py" in text
	assert "SECRET_TEXT" not in text


def test_forged_container_summary_never_reaches_the_pr_body_summary(tmp_path):
	repo, env = _setup(tmp_path, [PASS])
	_run_step(repo, env)
	text = _summary(tmp_path).read_text(encoding="utf-8")
	assert "<!-- ai:forged" not in text and "Closes #1" not in text


def test_step_returns_zero_when_runtime_dir_is_missing(tmp_path):
	repo, env = _setup(tmp_path, [PASS])
	env["RUNTIME_DIR"] = str(tmp_path / "does-not-exist")
	proc = _run_step(repo, env)
	assert proc.returncode == 0 and "STEP_RETURNED" in proc.stdout


def _plan(tmp_path: Path) -> Path:
	plan = tmp_path / "plan.txt"
	plan.write_text("## Files likely to change\n- `lib/a.py`\n\n## Functions or modules to implement\n- nothing\n", encoding="utf-8")
	return plan


def test_expected_files_section_for_a_standalone_issue(tmp_path):
	repo, env = _setup(tmp_path, [PASS])
	body = tmp_path / "body.txt"
	body.write_text("Fix a thing.\n", encoding="utf-8")
	proc = subprocess.run(["bash", str(STEP_SCRIPT), "expected-files", str(_plan(tmp_path)), str(body)], cwd=repo, env=env,
		capture_output=True, text=True, timeout=60)
	assert proc.returncode == 0
	assert "EXPECTED FILES (ADVISORY)" in proc.stdout
	assert "- tests/test_a.py" in proc.stdout
	assert "files_touched" not in proc.stdout


def test_expected_files_is_silent_for_orchestrator_children_and_mode_off(tmp_path):
	repo, env = _setup(tmp_path, [PASS])
	body = tmp_path / "body.txt"
	body.write_text("Fix.\n\n---\n**Orchestrator metadata** (do not edit)\n", encoding="utf-8")
	proc = subprocess.run(["bash", str(STEP_SCRIPT), "expected-files", str(_plan(tmp_path)), str(body)], cwd=repo, env=env,
		capture_output=True, text=True, timeout=60)
	assert proc.returncode == 0 and proc.stdout == ""
	body.write_text("Fix.\n", encoding="utf-8")
	env["PREPUSH_TEST_GATE_MODE"] = "off"
	proc = subprocess.run(["bash", str(STEP_SCRIPT), "expected-files", str(_plan(tmp_path)), str(body)], cwd=repo, env=env,
		capture_output=True, text=True, timeout=60)
	assert proc.returncode == 0 and proc.stdout == ""


# --- workflow wiring ------------------------------------------------------------

def _steps(text: str) -> list[tuple[str, str]]:
	"""(name, block) for each top-level job step in implement.yml."""
	parts = re.split(r"(?m)^      - name: ", text)
	steps = []
	for part in parts[1:]:
		name, _, rest = part.partition("\n")
		steps.append((name.strip(), rest))
	return steps


def test_step_sits_between_syntax_enforcement_and_commit():
	steps = _steps(IMPLEMENT_YML.read_text(encoding="utf-8"))
	names = [name for name, _ in steps]
	index = names.index("Pre-push affected-test gate")
	assert names[index - 1] == "Enforce syntax validation outcome"
	assert names[index + 1] == "Commit changes"
	block = steps[index][1]
	assert "        if: env.SKIP_IMPLEMENT != 'true' && env.HEAL_ROUTE != 'true'\n" in block
	assert "        continue-on-error: true\n" in block
	for line in (
		"PREPUSH_TEST_GATE_MODE: ${{ vars.PREPUSH_TEST_GATE_MODE || 'repair' }}",
		"PREPUSH_TEST_GATE_TIMEOUT_MINUTES: ${{ vars.PREPUSH_TEST_GATE_TIMEOUT_MINUTES || '15' }}",
		"PREPUSH_TEST_GATE_LARGE_FILE_TESTS: ${{ vars.PREPUSH_TEST_GATE_LARGE_FILE_TESTS || '50' }}",
		"PREPUSH_TEST_GATE_SHARDS: ${{ vars.PREPUSH_TEST_GATE_SHARDS || '4' }}",
	):
		assert line in block
	run_body = block.split("        run: |\n", 1)[1]
	assert "${{" not in run_body
	assert "implement_step_prepush_test_gate.sh" in run_body and "source " in run_body


def test_pr_body_appends_the_summary_before_the_auto_close_lint():
	steps = dict(_steps(IMPLEMENT_YML.read_text(encoding="utf-8")))
	block = next(body for name, body in steps.items() if name.startswith("Pre-flight — lint PR title/body"))
	assert block.index("prepush_test_gate_summary.md") < block.index("lint_pr_body_auto_close.py")
	assert block.index("prepush_test_gate_summary.md") < block.index('cat "${PR_BODY_FILE}" >> "${PR_LINT_FILE}"')


def test_prompts_carry_the_expected_files_sentence():
	sentence = "Update the tests and registries listed under Expected files together"
	assert sentence in IMPLEMENT_YML.read_text(encoding="utf-8")
	for path in (REPO_ROOT / "prompts" / "mode-implement.txt", REPO_ROOT / "prompts" / "_templates" / "mode-implement.txt"):
		assert sentence in path.read_text(encoding="utf-8")


def test_step_script_is_executable_ascii_and_defaults_its_variables():
	text = STEP_SCRIPT.read_text(encoding="utf-8")
	assert os.access(STEP_SCRIPT, os.X_OK)
	assert text.isascii()
	for default in ('PREPUSH_TEST_GATE_MODE:-repair', 'PREPUSH_TEST_GATE_TIMEOUT_MINUTES:-15',
			'PREPUSH_TEST_GATE_LARGE_FILE_TESTS:-50', 'PREPUSH_TEST_GATE_SHARDS:-4'):
		assert default in text
	assert "api.github.com" not in text and "gh api" not in text


# --- attempt journal (issue #8428, P1) ----------------------------------------------

def _journal_prompt(tmp_path: Path, persist: str | None) -> str:
	repo, env = _setup(tmp_path, [FAIL, PASS])
	prompts = Path(env["IMPLEMENT_STAGED_SUPPORT_RUN_DIR"]).parent / "prompts"
	shutil.copy2(REPO_ROOT / "prompts" / "mode-persist-journal-rules.txt", prompts / "mode-persist-journal-rules.txt")
	(tmp_path / "runtime" / "attempt_journal.txt").write_text(
		"=== BEGIN UNTRUSTED ATTEMPT JOURNAL (host-recorded; note fields are model text) ===\nrow\n=== END UNTRUSTED ATTEMPT JOURNAL ===\n",
		encoding="utf-8")
	env.pop("PERSIST_JOURNAL_ENABLED", None)
	if persist is not None:
		env["PERSIST_JOURNAL_ENABLED"] = persist
	proc = _run_step(repo, env)
	assert "STEP_RETURNED" in proc.stdout, proc.stderr
	return (_work(tmp_path) / "editor_prompt.txt").read_text(encoding="utf-8")


def test_journal_on_repair_prompt_shows_implement_deletions_and_rules(tmp_path):
	prompt = _journal_prompt(tmp_path, "true")
	block = prompt.split("\n=== EDITOR DELETIONS THIS ROUND ===\n", 1)[1].split("\n=== END EDITOR DELETIONS THIS ROUND ===\n", 1)[0]
	assert "UNTRUSTED_DATA: (file) lib/a.py" in block and "UNTRUSTED_DATA: -X = 1" in block
	assert "=== BEGIN UNTRUSTED ATTEMPT JOURNAL" in prompt
	assert "=== ATTEMPT JOURNAL REPAIR RULES ===" in prompt


def test_journal_off_repair_prompt_is_byte_identical(tmp_path):
	unset = _journal_prompt(tmp_path / "a", None)
	off = _journal_prompt(tmp_path / "b", "false")
	assert unset == off
	assert "EDITOR DELETIONS" not in unset and "ATTEMPT JOURNAL" not in unset


def test_journal_rules_prompt_is_staged_and_the_helper_is_staged():
	text = IMPLEMENT_YML.read_text(encoding="utf-8")
	assert "prompts/mode-persist-journal-rules.txt:prompts" in text
	assert "PERSIST_JOURNAL_ENABLED: ${{ vars.PERSIST_JOURNAL_ENABLED || 'false' }}" in text
	steps = dict(_steps(text))
	block = steps["Record attempt journal entry (implement)"]
	assert "        continue-on-error: true\n" in block
	assert "if: always() && env.PERSIST_JOURNAL_ENABLED == 'true'" in block
	assert "--stage implement" in block and "prepush_gate/run2/result.json" in block
	names = [name for name, _ in _steps(text)]
	assert names.index("Record attempt journal entry (implement)") == names.index("Create Pull Request") + 1

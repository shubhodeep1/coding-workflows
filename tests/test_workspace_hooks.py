#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "run_workspace_hook.sh"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"


def _workflow_doc(path: Path) -> dict[str, object]:
	doc = yaml.safe_load(path.read_text(encoding="utf-8"))
	if not isinstance(doc, dict):
		raise AssertionError(f"Workflow did not parse into a mapping: {path}")
	return doc


def _steps(path: Path) -> list[dict[str, object]]:
	jobs = _workflow_doc(path).get("jobs")
	if not isinstance(jobs, dict):
		raise AssertionError(f"Workflow jobs mapping missing in {path}")
	for job in jobs.values():
		if not isinstance(job, dict):
			continue
		steps = job.get("steps")
		if isinstance(steps, list):
			return [step for step in steps if isinstance(step, dict)]
	raise AssertionError(f"Workflow steps missing in {path}")


def _step(path: Path, step_name: str) -> dict[str, object]:
	for step in _steps(path):
		if str(step.get("name", "")).strip() == step_name:
			return step
	raise AssertionError(f"Step not found in {path}: {step_name}")


def _step_run_text(path: Path, step_name: str) -> str:
	run = _step(path, step_name).get("run")
	if not isinstance(run, str):
		raise AssertionError(f"Step does not define a run block: {step_name}")
	return run


def _step_index(path: Path, step_name: str) -> int:
	for index, step in enumerate(_steps(path)):
		if str(step.get("name", "")).strip() == step_name:
			return index
	raise AssertionError(f"Step not found in {path}: {step_name}")


def _prepare_case(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
	repo_root = tmp_path / "repo"
	helper_copy = repo_root / "scripts" / HELPER.name
	workspace_path = tmp_path / "workspace"
	runner_temp = tmp_path / "runner-temp"
	helper_copy.parent.mkdir(parents=True)
	workspace_path.mkdir(parents=True)
	runner_temp.mkdir(parents=True)
	shutil.copy2(HELPER, helper_copy)
	helper_copy.chmod(0o755)
	return repo_root, helper_copy, workspace_path, runner_temp


def _write_hook(repo_root: Path, phase: str, hook: str, content: str) -> Path:
	hook_path = repo_root / ".github" / "ai" / "workspace_hooks" / phase / f"{hook}.sh"
	hook_path.parent.mkdir(parents=True, exist_ok=True)
	hook_path.write_text(content, encoding="utf-8")
	return hook_path


def _run_helper(
	repo_root: Path,
	helper_path: Path,
	workspace_path: Path,
	runner_temp: Path,
	phase: str,
	hook: str,
	**env_overrides: str,
) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env.update(
		{
			"RUNNER_TEMP": str(runner_temp),
			"WORKSPACE_PATH": str(workspace_path),
		}
	)
	env.update(env_overrides)
	return subprocess.run(
		["bash", str(helper_path), phase, hook],
		cwd=str(repo_root),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def test_helper_script_exists_and_is_executable() -> None:
	assert HELPER.exists()
	assert HELPER.stat().st_mode & 0o111


def test_missing_and_empty_hooks_are_noop(tmp_path: Path) -> None:
	repo_root, helper_path, workspace_path, runner_temp = _prepare_case(tmp_path)
	result = _run_helper(repo_root, helper_path, workspace_path, runner_temp, "implement", "before_run")
	assert result.returncode == 0, result.stderr
	assert not (runner_temp / "workspace-hooks" / "implement-before_run.log").exists()

	_write_hook(repo_root, "implement", "before_run", "")
	result = _run_helper(repo_root, helper_path, workspace_path, runner_temp, "implement", "before_run")
	assert result.returncode == 0, result.stderr
	assert not (runner_temp / "workspace-hooks" / "implement-before_run.log").exists()


def test_after_create_runs_when_created_now_true(tmp_path: Path) -> None:
	repo_root, helper_path, workspace_path, runner_temp = _prepare_case(tmp_path)
	marker = workspace_path / "after_create.txt"
	_write_hook(
		repo_root,
		"implement",
		"after_create",
		"#!/usr/bin/env bash\nprintf 'ran\\n' >> after_create.txt\n",
	)

	result = _run_helper(
		repo_root,
		helper_path,
		workspace_path,
		runner_temp,
		"implement",
		"after_create",
		CREATED_NOW="true",
	)
	assert result.returncode == 0, result.stderr
	assert marker.read_text(encoding="utf-8") == "ran\n"


def test_after_create_runs_once_per_workspace_lifetime(tmp_path: Path) -> None:
	repo_root, helper_path, workspace_path, runner_temp = _prepare_case(tmp_path)
	marker = workspace_path / "after_create.txt"
	_write_hook(
		repo_root,
		"implement",
		"after_create",
		"#!/usr/bin/env bash\nprintf 'ran\\n' >> after_create.txt\n",
	)

	first = _run_helper(
		repo_root,
		helper_path,
		workspace_path,
		runner_temp,
		"implement",
		"after_create",
		CREATED_NOW="true",
	)
	second = _run_helper(
		repo_root,
		helper_path,
		workspace_path,
		runner_temp,
		"implement",
		"after_create",
		CREATED_NOW="false",
	)

	assert first.returncode == 0, first.stderr
	assert second.returncode == 0, second.stderr
	assert marker.read_text(encoding="utf-8") == "ran\n"


def test_after_create_treats_unset_created_now_as_true(tmp_path: Path) -> None:
	repo_root, helper_path, workspace_path, runner_temp = _prepare_case(tmp_path)
	marker = workspace_path / "after_create.txt"
	_write_hook(
		repo_root,
		"implement",
		"after_create",
		"#!/usr/bin/env bash\nprintf 'ran\\n' >> after_create.txt\n",
	)

	result = _run_helper(repo_root, helper_path, workspace_path, runner_temp, "implement", "after_create")
	assert result.returncode == 0, result.stderr
	assert marker.read_text(encoding="utf-8") == "ran\n"


def test_after_create_skips_when_created_now_false(tmp_path: Path) -> None:
	repo_root, helper_path, workspace_path, runner_temp = _prepare_case(tmp_path)
	marker = workspace_path / "after_create.txt"
	_write_hook(
		repo_root,
		"implement",
		"after_create",
		"#!/usr/bin/env bash\nprintf 'ran\\n' >> after_create.txt\n",
	)

	result = _run_helper(
		repo_root,
		helper_path,
		workspace_path,
		runner_temp,
		"implement",
		"after_create",
		CREATED_NOW="false",
	)
	assert result.returncode == 0, result.stderr
	assert not marker.exists()


def test_hook_executes_in_workspace_path(tmp_path: Path) -> None:
	repo_root, helper_path, workspace_path, runner_temp = _prepare_case(tmp_path)
	_write_hook(
		repo_root,
		"validate",
		"before_run",
		"#!/usr/bin/env bash\npwd -P > hook_pwd.txt\n",
	)

	result = _run_helper(repo_root, helper_path, workspace_path, runner_temp, "validate", "before_run")
	assert result.returncode == 0, result.stderr
	assert (workspace_path / "hook_pwd.txt").read_text(encoding="utf-8").strip() == str(workspace_path.resolve())


def test_before_run_failure_is_fatal_and_emits_bounded_tail(tmp_path: Path) -> None:
	repo_root, helper_path, workspace_path, runner_temp = _prepare_case(tmp_path)
	_write_hook(
		repo_root,
		"implement",
		"before_run",
		"#!/usr/bin/env bash\n"
		"printf 'BEGIN-MARKER\\n'\n"
		"python3 - <<'PY'\n"
		"print('x' * 12000)\n"
		"PY\n"
		"printf 'END-MARKER\\n'\n"
		"exit 1\n",
	)

	result = _run_helper(repo_root, helper_path, workspace_path, runner_temp, "implement", "before_run")
	log_file = runner_temp / "workspace-hooks" / "implement-before_run.log"
	assert result.returncode == 1
	assert log_file.exists()
	log_text = log_file.read_text(encoding="utf-8")
	assert "BEGIN-MARKER" in log_text
	assert "END-MARKER" in log_text
	assert "END-MARKER" in result.stderr
	assert "BEGIN-MARKER" not in result.stderr


def test_before_run_sigkill_timeout_reports_timeout(tmp_path: Path) -> None:
	repo_root, helper_path, workspace_path, runner_temp = _prepare_case(tmp_path)
	timeout_dir = tmp_path / "bin"
	timeout_dir.mkdir()
	fake_timeout = timeout_dir / "timeout"
	fake_timeout.write_text(
		"#!/usr/bin/env bash\n"
		"sleep 2\n"
		"printf 'simulated timeout\\n'\n"
		"exit 137\n",
		encoding="utf-8",
	)
	fake_timeout.chmod(0o755)
	_write_hook(repo_root, "implement", "before_run", "#!/usr/bin/env bash\nexit 0\n")

	result = _run_helper(
		repo_root,
		helper_path,
		workspace_path,
		runner_temp,
		"implement",
		"before_run",
		PATH=f"{timeout_dir}:{os.environ['PATH']}",
		WORKSPACE_HOOK_TIMEOUT_SECONDS="1",
	)
	assert result.returncode == 137
	assert "timed out after 1 seconds" in result.stderr
	assert "failed with exit code 137" not in result.stderr


def test_nonfatal_hooks_log_and_continue(tmp_path: Path) -> None:
	repo_root, helper_path, workspace_path, runner_temp = _prepare_case(tmp_path)
	for hook_name in ("after_run", "before_remove"):
		_write_hook(
			repo_root,
			"validate",
			hook_name,
			f"#!/usr/bin/env bash\nprintf 'nonfatal-{hook_name}\\n'\nexit 1\n",
		)
		result = _run_helper(repo_root, helper_path, workspace_path, runner_temp, "validate", hook_name)
		assert result.returncode == 0
		assert f"Workspace hook validate/{hook_name} failed with exit code 1" in result.stderr
		assert f"nonfatal-{hook_name}" in result.stderr
		assert (runner_temp / "workspace-hooks" / f"validate-{hook_name}.log").exists()


def test_implement_workflow_stages_and_orders_workspace_hooks() -> None:
	stage_block = _step_run_text(IMPLEMENT_WORKFLOW, "Stage workflow support files")
	assert "run_workspace_hook.sh" in stage_block
	assert _step(IMPLEMENT_WORKFLOW, "Run Codex implementation").get("id") == "implement_run"
	assert _step(IMPLEMENT_WORKFLOW, "Run workspace after_run hook").get("if") == "always() && env.SKIP_IMPLEMENT != 'true' && steps.implement_run.outcome != 'skipped'"
	assert _step_index(IMPLEMENT_WORKFLOW, "Activate workspace shell context") < _step_index(IMPLEMENT_WORKFLOW, "Run workspace after_create hook") < _step_index(IMPLEMENT_WORKFLOW, "Detect preexisting Serena project config")
	assert _step_index(IMPLEMENT_WORKFLOW, "Retrieve implementation memory context") < _step_index(IMPLEMENT_WORKFLOW, "Run workspace before_run hook") < _step_index(IMPLEMENT_WORKFLOW, "Run Codex implementation")
	assert _step_index(IMPLEMENT_WORKFLOW, "Run Codex implementation") < _step_index(IMPLEMENT_WORKFLOW, "Run workspace after_run hook") < _step_index(IMPLEMENT_WORKFLOW, "Write run summary")
	assert _step_index(IMPLEMENT_WORKFLOW, "Emit Serena stats") < _step_index(IMPLEMENT_WORKFLOW, "Run workspace before_remove hook") < _step_index(IMPLEMENT_WORKFLOW, "Cleanup temporary artifacts")


def test_validate_workflow_stages_and_orders_workspace_hooks() -> None:
	fetch_block = _step_run_text(VALIDATE_WORKFLOW, "Fetch workflow support files")
	assert "run_workspace_hook.sh" in fetch_block
	assert _step(VALIDATE_WORKFLOW, "Run validation process").get("if") == "always() && steps.workspace_after_create_hook.outcome != 'failure' && steps.workspace_before_run_hook.outcome != 'failure'"
	assert _step_index(VALIDATE_WORKFLOW, "Activate workspace shell context") < _step_index(VALIDATE_WORKFLOW, "Run workspace after_create hook") < _step_index(VALIDATE_WORKFLOW, "Initialize Serena runtime state")
	assert _step_index(VALIDATE_WORKFLOW, "Build semble index") < _step_index(VALIDATE_WORKFLOW, "Run workspace before_run hook") < _step_index(VALIDATE_WORKFLOW, "Run validation process")
	assert _step_index(VALIDATE_WORKFLOW, "Run validation process") < _step_index(VALIDATE_WORKFLOW, "Run workspace after_run hook") < _step_index(VALIDATE_WORKFLOW, "Collect validation status")
	assert _step_index(VALIDATE_WORKFLOW, "Force orchestrate poll after validation finalization") < _step_index(VALIDATE_WORKFLOW, "Run workspace before_remove hook") < _step_index(VALIDATE_WORKFLOW, "Enforce validation outcome")


def _run_tmp_path_case(case_fn) -> None:
	with TemporaryDirectory() as tmp_dir:
		case_fn(Path(tmp_dir))


def main() -> int:
	test_helper_script_exists_and_is_executable()
	for case_fn in (
		test_missing_and_empty_hooks_are_noop,
		test_after_create_runs_when_created_now_true,
		test_after_create_runs_once_per_workspace_lifetime,
		test_after_create_treats_unset_created_now_as_true,
		test_after_create_skips_when_created_now_false,
		test_hook_executes_in_workspace_path,
		test_before_run_failure_is_fatal_and_emits_bounded_tail,
		test_before_run_sigkill_timeout_reports_timeout,
		test_nonfatal_hooks_log_and_continue,
	):
		_run_tmp_path_case(case_fn)
	test_implement_workflow_stages_and_orders_workspace_hooks()
	test_validate_workflow_stages_and_orders_workspace_hooks()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

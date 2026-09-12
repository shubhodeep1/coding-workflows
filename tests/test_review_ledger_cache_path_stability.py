"""Regression tests for the review-issue ledger cache path contract.

actions/cache hashes the raw ``path`` list into the cache *version*, and a
restore only matches a save whose key AND version agree. Issue #3965's cycle-5
fix PR (#4077) looped through 13 same-head partial-finalize runs because the
cache paths embedded the per-run workspace directory
(``<runner.temp>/workspaces/<pr>-<run_id>-<run_attempt>/...``), so every run
computed a fresh version, logged ``Cache not found for input keys``, restored
nothing, and restarted at ``resume_round=1``; the ``REVIEW_MAX_RESUME_ROUNDS``
bound never tripped.

These tests pin the fix: every ledger cache step uses one run-independent
staging path list, the stage-in / stage-out shell steps round-trip the
workspace state through that staging dir, and validate.yml's behavioural smoke
restore uses the identical list so it can read the review run's save.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"

STABLE_CACHE_PATH_LIST = (
	"${{ runner.temp }}/review-ledger-cache/.ai/review_issue_ledger/\n"
	"${{ runner.temp }}/review-ledger-cache/.ai/review_runtime/\n"
	"${{ runner.temp }}/review-ledger-cache/${{ env.REVIEW_LEDGER_PATH }}\n"
)
VALIDATE_LEDGER_PATH_EXPR = (
	"${{ vars.REVIEW_LEDGER_PATH || format('.ai/review_issue_ledger/pr-{0}.txt', "
	"steps.behavioural_smoke_pr.outputs.pr_number) }}"
)
WORKSPACE_PATH_EXPR = "${{ steps.workspace_state.outputs.workspace_path }}"
RUN_SCOPED_MARKERS = (
	"workspace_path",
	"github.run_id",
	"github.run_attempt",
	"GITHUB_RUN_ID",
	"GITHUB_RUN_ATTEMPT",
)

REVIEW_RESTORE_STEP = "Restore review-issue ledger"
REVIEW_STAGE_IN_STEP = "Stage restored review-issue ledger into workspace"
REVIEW_RESUME_RESTORE_STEP = "Restore same-head partial resume state"
REVIEW_EARLY_STAGE_OUT_STEP = "Stage review-issue ledger for cache save"
REVIEW_EARLY_SAVE_STEP = "Save review-issue ledger"
REVIEW_LATE_STAGE_OUT_STEP = "Stage review-issue ledger for partial finalize cache save"
REVIEW_LATE_SAVE_STEP = "Save review-issue ledger after partial finalize"
VALIDATE_RESTORE_STEP = "Restore behavioural smoke runtime cache"
VALIDATE_STAGE_IN_STEP = "Stage restored behavioural smoke runtime cache into workspace"


def _job_steps(path: Path) -> list[dict[str, object]]:
	doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
	jobs = doc.get("jobs") or {}
	assert isinstance(jobs, dict)
	steps: list[dict[str, object]] = []
	for job in jobs.values():
		if isinstance(job, dict):
			for step in job.get("steps") or []:
				if isinstance(step, dict):
					steps.append(step)
	return steps


def _step(path: Path, step_name: str) -> dict[str, object]:
	for step in _job_steps(path):
		if step.get("name") == step_name:
			return step
	raise AssertionError(f"Step not found in {path.name}: {step_name}")


def _step_index(path: Path, step_name: str) -> int:
	for idx, step in enumerate(_job_steps(path)):
		if step.get("name") == step_name:
			return idx
	raise AssertionError(f"Step not found in {path.name}: {step_name}")


def _cache_path(step: dict[str, object]) -> str:
	with_block = step.get("with")
	assert isinstance(with_block, dict), step.get("name")
	return str(with_block.get("path", ""))


def test_review_ledger_cache_steps_share_one_run_independent_path_list() -> None:
	for step_name in (REVIEW_RESTORE_STEP, REVIEW_EARLY_SAVE_STEP, REVIEW_LATE_SAVE_STEP):
		cache_path = _cache_path(_step(REVIEW_WORKFLOW, step_name))
		assert cache_path == STABLE_CACHE_PATH_LIST, step_name
		for marker in RUN_SCOPED_MARKERS:
			assert marker not in cache_path, f"{step_name} cache path is run-scoped via {marker!r}"


def test_validate_behavioural_smoke_restore_uses_the_same_path_list() -> None:
	# The PR-number source differs (validate resolves it from the tracking
	# issue), but the expanded list must match review_autofix.yml's save
	# byte-for-byte or the cache version differs and the restore never hits.
	expected = STABLE_CACHE_PATH_LIST.replace("${{ env.REVIEW_LEDGER_PATH }}", VALIDATE_LEDGER_PATH_EXPR)
	cache_path = _cache_path(_step(VALIDATE_WORKFLOW, VALIDATE_RESTORE_STEP))
	assert cache_path == expected
	for marker in RUN_SCOPED_MARKERS:
		assert marker not in cache_path, f"validate restore cache path is run-scoped via {marker!r}"


def test_review_stage_steps_are_ordered_and_gated_like_their_cache_steps() -> None:
	restore_idx = _step_index(REVIEW_WORKFLOW, REVIEW_RESTORE_STEP)
	stage_in_idx = _step_index(REVIEW_WORKFLOW, REVIEW_STAGE_IN_STEP)
	resume_idx = _step_index(REVIEW_WORKFLOW, REVIEW_RESUME_RESTORE_STEP)
	assert restore_idx < stage_in_idx < resume_idx, "stage-in must sit between the cache restore and the same-head resume restore"

	for stage_name, save_name in (
		(REVIEW_EARLY_STAGE_OUT_STEP, REVIEW_EARLY_SAVE_STEP),
		(REVIEW_LATE_STAGE_OUT_STEP, REVIEW_LATE_SAVE_STEP),
	):
		stage_step = _step(REVIEW_WORKFLOW, stage_name)
		save_step = _step(REVIEW_WORKFLOW, save_name)
		assert _step_index(REVIEW_WORKFLOW, stage_name) + 1 == _step_index(REVIEW_WORKFLOW, save_name), stage_name
		assert stage_step.get("if") == save_step.get("if"), f"{stage_name} must run exactly when {save_name} runs"
		assert stage_step.get("continue-on-error") is True

	stage_in_step = _step(REVIEW_WORKFLOW, REVIEW_STAGE_IN_STEP)
	restore_step = _step(REVIEW_WORKFLOW, REVIEW_RESTORE_STEP)
	assert stage_in_step.get("if") == restore_step.get("if")
	assert stage_in_step.get("continue-on-error") is True

	validate_stage_in = _step(VALIDATE_WORKFLOW, VALIDATE_STAGE_IN_STEP)
	validate_restore = _step(VALIDATE_WORKFLOW, VALIDATE_RESTORE_STEP)
	assert _step_index(VALIDATE_WORKFLOW, VALIDATE_RESTORE_STEP) + 1 == _step_index(VALIDATE_WORKFLOW, VALIDATE_STAGE_IN_STEP)
	assert validate_stage_in.get("if") == validate_restore.get("if")
	assert validate_stage_in.get("continue-on-error") is True


def _run_step_script(step: dict[str, object], workspace: Path, runner_temp: Path | str, ledger_rel: str) -> subprocess.CompletedProcess[str]:
	script = str(step.get("run", "")).replace(WORKSPACE_PATH_EXPR, str(workspace))
	assert "${{" not in script, "unexpected unexpanded expression in stage script"
	env = {
		"PATH": os.environ.get("PATH", ""),
		"RUNNER_TEMP": str(runner_temp),
		"REVIEW_LEDGER_PATH": ledger_rel,
	}
	return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)


def test_stage_out_then_stage_in_round_trips_ledger_state_across_workspaces() -> None:
	# Simulates two runs: run A stages its workspace state into the staging
	# dir (what actions/cache/save uploads), run B restores that staging dir
	# (what actions/cache/restore downloads) into a *different* per-run
	# workspace path. The resume marker must survive the trip so run B sees
	# resume_round=1 instead of starting fresh.
	stage_out = _step(REVIEW_WORKFLOW, REVIEW_LATE_STAGE_OUT_STEP)
	stage_in = _step(REVIEW_WORKFLOW, REVIEW_STAGE_IN_STEP)
	ledger_rel = ".ai/review_issue_ledger/pr-4077.txt"
	with tempfile.TemporaryDirectory(prefix="ledger-cache-roundtrip-") as td:
		root = Path(td)
		runner_temp = root / "_temp"
		workspace_a = runner_temp / "workspaces" / "4077-111-1"
		workspace_b = runner_temp / "workspaces" / "4077-222-1"
		marker_rel = Path(".ai/review_runtime/pr-4077/round-1/partial_finalize.json")
		(workspace_a / marker_rel).parent.mkdir(parents=True)
		(workspace_a / marker_rel).write_text('{"resume_round": 1, "head_sha": "abc"}\n', encoding="utf-8")
		(workspace_a / ledger_rel).parent.mkdir(parents=True)
		(workspace_a / ledger_rel).write_text("ledger-v1\n", encoding="utf-8")
		# Stale staging content from an earlier restore must not leak into the save.
		stale = runner_temp / "review-ledger-cache" / ".ai" / "review_runtime" / "pr-4077" / "round-9" / "stale.json"
		stale.parent.mkdir(parents=True)
		stale.write_text("{}\n", encoding="utf-8")

		out = _run_step_script(stage_out, workspace_a, runner_temp, ledger_rel)
		assert out.returncode == 0, out.stderr
		assert "REVIEW_LEDGER_CACHE_STAGE_OUT staged_entries=3" in out.stdout
		staging = runner_temp / "review-ledger-cache"
		assert (staging / marker_rel).read_text(encoding="utf-8") == '{"resume_round": 1, "head_sha": "abc"}\n'
		assert (staging / ledger_rel).read_text(encoding="utf-8") == "ledger-v1\n"
		assert not stale.exists(), "stage-out must clear stale staging content before copying"

		workspace_b.mkdir(parents=True)
		inp = _run_step_script(stage_in, workspace_b, runner_temp, ledger_rel)
		assert inp.returncode == 0, inp.stderr
		assert "REVIEW_LEDGER_CACHE_STAGE_IN restored_entries=3" in inp.stdout
		assert (workspace_b / marker_rel).read_text(encoding="utf-8") == '{"resume_round": 1, "head_sha": "abc"}\n'
		assert (workspace_b / ledger_rel).read_text(encoding="utf-8") == "ledger-v1\n"


def test_stage_steps_fail_open_without_cached_state_or_runner_temp() -> None:
	stage_in = _step(REVIEW_WORKFLOW, REVIEW_STAGE_IN_STEP)
	stage_out = _step(REVIEW_WORKFLOW, REVIEW_EARLY_STAGE_OUT_STEP)
	with tempfile.TemporaryDirectory(prefix="ledger-cache-empty-") as td:
		root = Path(td)
		runner_temp = root / "_temp"
		workspace = runner_temp / "workspaces" / "4077-333-1"
		workspace.mkdir(parents=True)

		inp = _run_step_script(stage_in, workspace, runner_temp, ".ai/review_issue_ledger/pr-4077.txt")
		assert inp.returncode == 0, inp.stderr
		assert "REVIEW_LEDGER_CACHE_STAGE_IN restored_entries=0" in inp.stdout
		assert not (workspace / ".ai").exists()

		out = _run_step_script(stage_out, workspace, runner_temp, ".ai/review_issue_ledger/pr-4077.txt")
		assert out.returncode == 0, out.stderr
		assert "REVIEW_LEDGER_CACHE_STAGE_OUT staged_entries=0" in out.stdout

		no_temp = _run_step_script(stage_out, workspace, "", ".ai/review_issue_ledger/pr-4077.txt")
		assert no_temp.returncode == 0, no_temp.stderr
		assert "::warning::RUNNER_TEMP is unset" in no_temp.stdout


def test_validate_stage_in_copies_review_runtime_into_workspace() -> None:
	stage_in = _step(VALIDATE_WORKFLOW, VALIDATE_STAGE_IN_STEP)
	with tempfile.TemporaryDirectory(prefix="validate-ledger-cache-") as td:
		root = Path(td)
		runner_temp = root / "_temp"
		workspace = runner_temp / "workspaces" / "3965-444-1"
		workspace.mkdir(parents=True)
		interim_rel = Path(".ai/review_runtime/pr-4077/round-1/judge_interim.json")
		staged = runner_temp / "review-ledger-cache" / interim_rel
		staged.parent.mkdir(parents=True)
		staged.write_text('{"priors": []}\n', encoding="utf-8")

		result = _run_step_script(stage_in, workspace, runner_temp, "")
		assert result.returncode == 0, result.stderr
		assert "REVIEW_LEDGER_CACHE_STAGE_IN restored_entries=1" in result.stdout
		assert (workspace / interim_rel).read_text(encoding="utf-8") == '{"priors": []}\n'


def main() -> int:
	test_review_ledger_cache_steps_share_one_run_independent_path_list()
	test_validate_behavioural_smoke_restore_uses_the_same_path_list()
	test_review_stage_steps_are_ordered_and_gated_like_their_cache_steps()
	test_stage_out_then_stage_in_round_trips_ledger_state_across_workspaces()
	test_stage_steps_fail_open_without_cached_state_or_runner_temp()
	test_validate_stage_in_copies_review_runtime_into_workspace()
	print("ok")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

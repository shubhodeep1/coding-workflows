"""One full CI run per PR, static checks on every push (cut-ci-runner-time Phase 4).

Pins the switch-over:
  * ci.yml has no pull_request trigger, so no per-push run can report `lint`;
  * .github/workflows/ci_pr_quick.yml (`CI quick`) runs on every PR push and
    calls ci.yml with mode `quick` from a job that is not named `lint`, so its
    check-runs are `quick / <job>`;
  * in quick mode only `static-checks` can run: every other ci.yml job, the
    `lint` aggregate included, is skipped by `if:`;
  * internal-review.yml passes final_ci_workflow: ci.yml, and the review
    sweep's safety net names the same file.
"""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
	return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _on(workflow: dict) -> dict:
	return workflow.get("on") or workflow.get(True)


def test_ci_has_no_pull_request_trigger() -> None:
	on = _on(_load("ci.yml"))
	assert "pull_request" not in on
	assert "pull_request_target" not in on
	assert on["push"]["branches"] == ["main", "stable", "orchestrator/project-*"]
	assert on["workflow_dispatch"]["inputs"]["pr_number"]["required"] is True
	assert on["workflow_call"]["inputs"]["mode"]["default"] == "full"


def test_quick_caller_runs_on_every_pr_push_and_calls_ci_in_quick_mode() -> None:
	quick = _load("ci_pr_quick.yml")
	assert quick["name"] == "CI quick"
	on = _on(quick)
	assert set(on) == {"pull_request"}
	assert on["pull_request"]["branches"] == ["**"]
	assert on["pull_request"]["types"] == ["opened", "synchronize", "reopened", "ready_for_review"]
	assert quick["concurrency"] == {
		"group": "ci-quick-pr-${{ github.event.pull_request.number }}",
		"cancel-in-progress": True,
	}
	assert list(quick["jobs"]) == ["quick"]
	job = quick["jobs"]["quick"]
	assert job["uses"] == "./.github/workflows/ci.yml"
	assert job["with"] == {"mode": "quick"}


def test_quick_caller_never_creates_a_check_named_lint() -> None:
	quick = _load("ci_pr_quick.yml")
	for job_id, job in quick["jobs"].items():
		assert job_id != "lint"
		assert str(job.get("name", job_id)) != "lint"


def test_quick_caller_grants_what_the_called_jobs_declare() -> None:
	granted = _load("ci_pr_quick.yml")["permissions"]
	order = {"none": 0, "read": 1, "write": 2}
	for job_id, job in _load("ci.yml")["jobs"].items():
		for scope, level in (job.get("permissions") or {}).items():
			assert order[granted.get(scope, "none")] >= order[level], (job_id, scope, level)


def test_only_static_checks_can_run_in_quick_mode() -> None:
	jobs = _load("ci.yml")["jobs"]
	for job_id, job in jobs.items():
		condition = " ".join(str(job.get("if", "")).split())
		if job_id == "static-checks":
			assert "if" not in job
		elif job_id == "budget-watch":
			assert "github.event_name == 'push'" in condition
		elif job_id == "final-ci-failed":
			assert "github.event_name == 'workflow_dispatch'" in condition
		else:
			assert "inputs.mode != 'quick'" in condition, job_id


def test_review_and_sweep_name_the_same_final_ci_workflow() -> None:
	review_job = _load("internal-review.yml")["jobs"]["review"]
	assert review_job["with"]["final_ci_workflow"] == "ci.yml"
	sweep_steps = _load("review_autofix_sweep.yml")["jobs"]["sweep"]["steps"]
	step = next(s for s in sweep_steps if s.get("name") == "Ensure the final CI run on reviewed PR heads")
	assert step["env"]["REVIEW_FINAL_CI_WORKFLOW"] == "ci.yml"

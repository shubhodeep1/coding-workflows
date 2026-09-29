#!/usr/bin/env python3
"""Contract tests for the parallel job layout of `.github/workflows/ci.yml`.

Until #4707, CI ran as one `lint` job of about 120 sequential steps and
took 40-45 minutes. It now runs as parallel jobs, and the job id `lint`
names the aggregate job, which needs every other job. These tests pin what
keeps that split honest:

  1. `lint` needs every other CI job, so a new job cannot fall outside the
     aggregate status;
  2. `lint` runs even when a needed job fails (`if: always()`) and fails
     unless every needed job succeeded. Without `always()` it would be
     skipped, and GitHub counts a skipped required check as passing;
  3. no test step runs in more than one job, and every job has a budget
     that keeps CI well under 20 minutes.

The aggregate step's shell is run for real against simulated `needs`
results, so the tests cover what it does, not only what it says.

Run directly: `python3 tests/test_ci_job_split_contract.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
AGGREGATE_JOB_ID = "lint"
AGGREGATE_STEP_NAME = "Require every CI job to succeed"
# Steps every job repeats to set itself up; everything else runs exactly once.
PER_JOB_SETUP_STEPS = ("Checkout repository", "Setup Python", "Install Python CI dependencies")
MAX_JOB_TIMEOUT_MINUTES = 20


def _ci_jobs() -> dict:
	workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
	jobs = workflow.get("jobs")
	if not isinstance(jobs, dict):
		raise AssertionError(f"jobs mapping missing in {CI_WORKFLOW}")
	return jobs


def _aggregate_step() -> dict:
	steps = [step for step in _ci_jobs()[AGGREGATE_JOB_ID]["steps"] if step.get("name") == AGGREGATE_STEP_NAME]
	if len(steps) != 1:
		raise AssertionError(f"expected exactly one {AGGREGATE_STEP_NAME!r} step in the {AGGREGATE_JOB_ID} job")
	return steps[0]


def _run_aggregate(needs_json: str) -> subprocess.CompletedProcess[str]:
	environment = os.environ.copy()
	environment.pop("BASH_ENV", None)
	environment["CI_NEEDS_JSON"] = needs_json
	return subprocess.run(
		["bash", "-c", _aggregate_step()["run"]],
		env=environment,
		capture_output=True,
		text=True,
		check=False,
	)


def _needs(**results: str) -> str:
	return json.dumps({job_id: {"result": result, "outputs": {}} for job_id, result in results.items()})


class AggregateJobWiringTest(unittest.TestCase):
	def test_lint_needs_every_other_job(self) -> None:
		jobs = _ci_jobs()
		self.assertIn(AGGREGATE_JOB_ID, jobs)
		needs = jobs[AGGREGATE_JOB_ID]["needs"]
		self.assertIsInstance(needs, list)
		self.assertEqual(sorted(needs), sorted(job_id for job_id in jobs if job_id != AGGREGATE_JOB_ID))
		self.assertEqual(len(needs), len(set(needs)))

	def test_lint_runs_even_when_a_needed_job_fails(self) -> None:
		self.assertEqual(_ci_jobs()[AGGREGATE_JOB_ID]["if"], "always()")

	def test_lint_reads_every_needed_result(self) -> None:
		self.assertEqual(_aggregate_step()["env"]["CI_NEEDS_JSON"], "${{ toJSON(needs) }}")

	def test_no_other_job_waits_on_another(self) -> None:
		"""The split only helps if the worker jobs start together."""
		for job_id, job in _ci_jobs().items():
			if job_id == AGGREGATE_JOB_ID:
				continue
			with self.subTest(job=job_id):
				self.assertNotIn("needs", job)


class AggregateJobBehaviourTest(unittest.TestCase):
	def test_all_success_passes(self) -> None:
		result = _run_aggregate(_needs(**{"static-checks": "success", "orchestrate-poll": "success"}))
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn("All 2 CI jobs succeeded.", result.stdout)

	def test_any_other_result_fails_and_is_named(self) -> None:
		for bad_result in ("failure", "cancelled", "skipped"):
			with self.subTest(result=bad_result):
				completed = _run_aggregate(_needs(**{"static-checks": "success", "orchestrate-poll": bad_result}))
				self.assertEqual(completed.returncode, 1)
				self.assertIn("CI jobs that did not succeed: orchestrate-poll", completed.stdout)

	def test_every_unsuccessful_job_is_listed(self) -> None:
		completed = _run_aggregate(
			_needs(**{"static-checks": "failure", "tests-a": "success", "orchestrate-poll": "cancelled"})
		)
		self.assertEqual(completed.returncode, 1)
		self.assertIn("static-checks, orchestrate-poll", completed.stdout)

	def test_empty_or_unreadable_results_fail_closed(self) -> None:
		for needs_json in ("{}", "", "not json"):
			with self.subTest(needs_json=needs_json):
				self.assertNotEqual(_run_aggregate(needs_json).returncode, 0)


class JobLayoutTest(unittest.TestCase):
	def test_every_job_has_a_budget_well_under_the_old_one(self) -> None:
		for job_id, job in _ci_jobs().items():
			with self.subTest(job=job_id):
				self.assertIsInstance(job.get("timeout-minutes"), int)
				self.assertGreater(job["timeout-minutes"], 0)
				self.assertLessEqual(job["timeout-minutes"], MAX_JOB_TIMEOUT_MINUTES)

	def test_no_step_runs_in_more_than_one_job(self) -> None:
		seen: dict[str, str] = {}
		for job_id, job in _ci_jobs().items():
			for step in job.get("steps", []):
				step_name = step.get("name")
				if step_name in PER_JOB_SETUP_STEPS:
					continue
				with self.subTest(step=step_name):
					self.assertNotIn(step_name, seen, f"{step_name!r} runs in {seen.get(step_name)} and {job_id}")
				seen[step_name] = job_id

	def test_every_worker_job_sets_itself_up_first(self) -> None:
		for job_id, job in _ci_jobs().items():
			if job_id == AGGREGATE_JOB_ID:
				continue
			with self.subTest(job=job_id):
				step_names = [step.get("name") for step in job["steps"]]
				self.assertEqual(step_names[0], "Checkout repository")
				for setup_step in PER_JOB_SETUP_STEPS:
					self.assertIn(setup_step, step_names)

	def test_this_contract_is_wired_into_ci(self) -> None:
		self.assertIn(
			"PYTHONDONTWRITEBYTECODE=1 python3 tests/test_ci_job_split_contract.py",
			CI_WORKFLOW.read_text(encoding="utf-8"),
		)


if __name__ == "__main__":
	unittest.main()

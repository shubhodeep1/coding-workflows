#!/usr/bin/env python3
"""Contract tests for the parallel job layout of `.github/workflows/ci.yml`.

Until #4707, CI ran as one `lint` job of about 120 sequential steps and
took 40-45 minutes. It now runs as parallel jobs, and the job id `lint`
names the aggregate job, which needs every other job. These tests pin what
keeps that split honest:

  1. `lint` needs every other CI job except the named advisory jobs in
     `ADVISORY_JOB_IDS`, so a new job cannot fall outside the aggregate
     status unless it is listed there on purpose;
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
# Jobs deliberately outside the aggregate: they run only on pushes to main,
# wait for the worker jobs so they can read their durations, and must never
# gate merges. Their own predicate and `lint.needs` exclusion are pinned in
# `tests/test_ci_budget_watch.py`. Adding a job here is a reviewed decision.
# They are skipped on pull requests, and a skipped needed job would fail the
# aggregate. `budget-watch` reads this run's job durations, so it has to
# wait for the worker jobs (#7012).
ADVISORY_JOB_IDS = frozenset({"budget-watch"})
# Alias kept for the advisory-job wiring test below.
POST_AGGREGATE_ADVISORY_JOBS = ADVISORY_JOB_IDS
ADVISORY_JOB_IF = "always() && github.event_name == 'push' && github.ref == 'refs/heads/main'"


def _ci_jobs() -> dict:
	workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
	jobs = workflow.get("jobs")
	if not isinstance(jobs, dict):
		raise AssertionError(f"jobs mapping missing in {CI_WORKFLOW}")
	return jobs


def _worker_job_ids(jobs: dict) -> list[str]:
	return [job_id for job_id in jobs if job_id != AGGREGATE_JOB_ID and job_id not in ADVISORY_JOB_IDS]


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
		self.assertEqual(sorted(needs), sorted(_worker_job_ids(jobs)))
		self.assertEqual(len(needs), len(set(needs)))

	def test_advisory_jobs_never_gate_the_aggregate(self) -> None:
		"""A job treated as advisory here must not also gate the aggregate."""
		jobs = _ci_jobs()
		needs = jobs[AGGREGATE_JOB_ID]["needs"]
		for job_id in sorted(ADVISORY_JOB_IDS):
			if job_id not in jobs:
				continue
			with self.subTest(job=job_id):
				self.assertNotIn(job_id, needs)

	def test_lint_runs_even_when_a_needed_job_fails(self) -> None:
		self.assertEqual(_ci_jobs()[AGGREGATE_JOB_ID]["if"], "always()")

	def test_lint_reads_every_needed_result(self) -> None:
		self.assertEqual(_aggregate_step()["env"]["CI_NEEDS_JSON"], "${{ toJSON(needs) }}")

	def test_no_other_job_waits_on_another(self) -> None:
		"""The split only helps if the worker jobs start together."""
		jobs = _ci_jobs()
		for job_id in _worker_job_ids(jobs):
			with self.subTest(job=job_id):
				self.assertNotIn("needs", jobs[job_id])

	def test_advisory_jobs_stay_outside_the_aggregate_and_main_only(self) -> None:
		jobs = _ci_jobs()
		for job_id in POST_AGGREGATE_ADVISORY_JOBS:
			with self.subTest(job=job_id):
				self.assertIn(job_id, jobs)
				self.assertNotIn(job_id, jobs[AGGREGATE_JOB_ID]["needs"])
				self.assertEqual(jobs[job_id]["if"], ADVISORY_JOB_IF)
				self.assertEqual(jobs[job_id]["steps"][0].get("name"), "Checkout repository")


class SystemPythonJobTest(unittest.TestCase):
	"""`tests-system-python` must test the runner's own interpreter (#9233)."""

	SYSTEM_PYTHON_JOB_ID = "tests-system-python"
	SANDBOX_PYTHON_JOB_ID = "tests-sandbox-python"

	def _steps(self) -> dict:
		return {step.get("name"): step for step in _ci_jobs()[self.SYSTEM_PYTHON_JOB_ID]["steps"]}

	def test_system_python_job_uses_the_runner_interpreter(self) -> None:
		jobs = _ci_jobs()
		for step in jobs[self.SYSTEM_PYTHON_JOB_ID]["steps"]:
			with self.subTest(step=step.get("name")):
				self.assertFalse(str(step.get("uses", "")).startswith("actions/setup-python"))
		steps = self._steps()
		self.assertIn('/usr/bin/python3 -m venv "${RUNNER_TEMP}/system-python-venv"', steps["Setup Python"]["run"])
		self.assertIn("GITHUB_PATH", steps["Setup Python"]["run"])
		install = steps["Install Python CI dependencies"]["run"]
		self.assertIn("sys.base_prefix", install)
		self.assertIn('!= "/usr"', install)
		self.assertEqual(jobs[self.SYSTEM_PYTHON_JOB_ID]["runs-on"], "ubuntu-latest")

	def test_system_python_shards_come_from_the_matrix(self) -> None:
		job = _ci_jobs()[self.SYSTEM_PYTHON_JOB_ID]
		shards = job["strategy"]["matrix"]["system-python-shard"]
		self.assertEqual(shards, list(range(len(shards))))
		self.assertFalse(job["strategy"]["fail-fast"])
		shard_step = self._steps()["System-Python test shard"]
		self.assertEqual(shard_step["env"]["CI_SYSTEM_PYTHON_SHARD_INDEX"], "${{ strategy.job-index }}")
		self.assertEqual(shard_step["env"]["CI_SYSTEM_PYTHON_SHARD_TOTAL"], "${{ strategy.job-total }}")
		self.assertIn("pytest --collect-only -q -p no:cacheprovider tests", shard_step["run"])
		self.assertIn("'NR % total == n'", shard_step["run"])
		self.assertIn('-v n="${shard_index}" -v total="${shard_total}"', shard_step["run"])

	def test_system_python_shard_keeps_default_tmpdir(self) -> None:
		"""Regression guard for run 43053071771 (issue #9313).

		A TMPDIR under RUNNER_TEMP made pytest's tmp_path long enough to push
		the heal tests' broker socket past the 108-byte AF_UNIX limit. Only a
		RUNNER_TEMP-based TMPDIR is rejected: a future short `/tmp/...` TMPDIR
		would keep socket paths short and stays allowed.
		"""
		shard_step = self._steps()["System-Python test shard"]
		self.assertNotIn("RUNNER_TEMP", str(shard_step.get("env", {}).get("TMPDIR", "")))
		for line in shard_step["run"].splitlines():
			code = line.strip()
			if code.startswith("#") or "TMPDIR=" not in code:
				continue
			with self.subTest(line=code):
				self.assertNotIn("RUNNER_TEMP", code)

	def test_system_python_shard_count_fits_the_job_budget(self) -> None:
		# Run 43053071771: 12 shards took 13-17 minutes of pytest each and most
		# were cancelled at the 20-minute budget, so the count must not drop
		# back below 24.
		shards = _ci_jobs()[self.SYSTEM_PYTHON_JOB_ID]["strategy"]["matrix"]["system-python-shard"]
		self.assertGreaterEqual(len(shards), 24)

	def test_runtime_python_jobs_gate_lint_and_feed_budget_watch(self) -> None:
		jobs = _ci_jobs()
		for job_id in (self.SYSTEM_PYTHON_JOB_ID, self.SANDBOX_PYTHON_JOB_ID):
			with self.subTest(job=job_id):
				self.assertIn(job_id, jobs[AGGREGATE_JOB_ID]["needs"])
				self.assertIn(job_id, jobs["budget-watch"]["needs"])


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
		jobs = _ci_jobs()
		for job_id in _worker_job_ids(jobs):
			with self.subTest(job=job_id):
				step_names = [step.get("name") for step in jobs[job_id]["steps"]]
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

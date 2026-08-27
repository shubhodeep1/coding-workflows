#!/usr/bin/env python3
"""Tests for the sharded orchestrate-poll steps in CI and release workflows.

The release-gate contract coverage pins `mark-stable.yml` and
`test-and-mark-stable.yml`.

`tests/test_orchestrate_poll_process.py` is CI's critical path: most of
the 307 tests in its post-fast-fail sharded subset spawn the real poller
as a bash subprocess in a throwaway sandbox, so they cost seconds each.
Run sequentially the module took
~35 minutes on a 4-core box, which alone overran the `lint` job's
`timeout-minutes` and left every CI run — on `main` as well as on PRs —
cancelled mid-suite, so the repo had no completing full-test gate.

The step now shards the module across `CI_POLL_TEST_SHARDS` workers.
Two things have to hold for that to be safe:

  1. the shard split must be a true partition — every test runs exactly
     once, no duplicates and no silent drops;
  2. a failing shard must fail the step.

The partition property is what these tests pin hardest: a sharding bug
that drops tests would turn a green CI into a lie.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WF = REPO_ROOT / ".github" / "workflows" / "ci.yml"
POLL_MODULE = REPO_ROOT / "tests" / "test_orchestrate_poll_process.py"

# The release gates carry a port of the same sharded step (no fast-fail
# subset — the shard input is the full module). Release v1.27.0 (run
# 33073743283) was lost to the exact failure mode #3844 fixed in ci.yml:
# the serial module overran `validate-scripts`' `timeout-minutes: 30`,
# the job was cancelled mid-suite, and `validate`/`release` were skipped.
RELEASE_WORKFLOWS = {
	"mark-stable": REPO_ROOT / ".github" / "workflows" / "mark-stable.yml",
	"test-and-mark-stable": REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml",
}

# Pull the awk expression out of the workflow rather than restating it, so a
# change to the split is exercised here instead of silently diverging from a
# constant that only ever agreed with the shipped step at authoring time.
SHARD_AWK_RE = re.compile(
	r"""awk -v n="\$\{shard\}" -v total="\$\{shards\}" '(?P<expr>[^']+)'"""
)


def load_lint_job() -> dict:
	return yaml.safe_load(CI_WF.read_text(encoding="utf-8"))["jobs"]["lint"]


def poll_step() -> dict:
	for step in load_lint_job()["steps"]:
		if step.get("name") == "Orchestrate poll process unit tests":
			return step
	raise AssertionError("orchestrate-poll step not found in ci.yml")


def shard_awk_expression() -> str:
	match = SHARD_AWK_RE.search(poll_step()["run"])
	if match is None:
		raise AssertionError("could not locate the shard-split awk expression in ci.yml")
	return match.group("expr").strip()


class ShardPartitionTest(unittest.TestCase):
	"""The awk split must lose nothing and duplicate nothing."""

	def shard(self, lines: list[str], total: int, n: int) -> list[str]:
		result = subprocess.run(
			["awk", "-v", f"n={n}", "-v", f"total={total}", shard_awk_expression()],
			input="\n".join(lines) + "\n",
			capture_output=True,
			text=True,
			check=True,
		)
		return [line for line in result.stdout.splitlines() if line]

	def assert_partition(self, count: int, total: int) -> None:
		lines = [f"test_case_{i:04d}" for i in range(count)]
		collected: list[str] = []
		for n in range(total):
			collected.extend(self.shard(lines, total, n))
		self.assertCountEqual(collected, lines, f"count={count} shards={total}")
		self.assertEqual(len(collected), len(set(collected)), "a test ran in more than one shard")

	def test_partition_holds_across_shard_counts(self) -> None:
		for total in (1, 2, 3, 4, 5, 8):
			with self.subTest(shards=total):
				self.assert_partition(307, total)

	def test_partition_holds_when_tests_are_fewer_than_shards(self) -> None:
		for count in (0, 1, 2, 3):
			with self.subTest(tests=count):
				self.assert_partition(count, 4)

	def test_partition_holds_for_the_real_module_size(self) -> None:
		"""Guard the live test count, not a made-up one."""
		import ast

		module = ast.parse(POLL_MODULE.read_text(encoding="utf-8"))
		names = [
			node.name
			for node in module.body
			if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
		]
		fast_fail_match = re.search(
			r'fast_fail = \[name for name in tests if name\.startswith\("(?P<prefix>[^"]+)"\)\]',
			CI_WF.read_text(encoding="utf-8"),
		)
		if fast_fail_match is None:
			self.fail("fast-fail selection not found in ci.yml")
		fast_fail_prefix = fast_fail_match.group("prefix")
		fast_fail_names = [name for name in names if name.startswith(fast_fail_prefix)]
		self.assertGreater(len(fast_fail_names), 0, "fast-fail subset unexpectedly empty")
		fast_fail_name_set = set(fast_fail_names)
		remaining_names = [
			name for name in names if name not in fast_fail_name_set
		]
		self.assertGreater(
			len(remaining_names), 100, "poll subset unexpectedly small; re-check the sharding math"
		)
		self.assert_partition(len(remaining_names), 4)


class PollStepContractTest(unittest.TestCase):
	def setUp(self) -> None:
		self.step = poll_step()
		self.run = self.step["run"]

	def test_shard_count_is_configurable_with_a_default(self) -> None:
		self.assertEqual(
			self.step["env"]["CI_POLL_TEST_SHARDS"],
			"${{ vars.CI_POLL_TEST_SHARDS || '4' }}",
		)

	def test_step_uses_a_locatable_partition_expression(self) -> None:
		"""The partition tests above are only meaningful if this resolves."""
		self.assertEqual(shard_awk_expression(), "NR % total == n")

	def test_missing_partition_expression_has_clear_failure(self) -> None:
		with mock.patch(f"{__name__}.SHARD_AWK_RE") as missing_expression_pattern:
			missing_expression_pattern.search.return_value = None
			with self.assertRaisesRegex(AssertionError, "could not locate the shard-split awk expression"):
				shard_awk_expression()

	def test_a_failing_shard_fails_the_step(self) -> None:
		self.assertIn("shard_failures=$((shard_failures + 1))", self.run)
		self.assertIn("exit 1", self.run)

	def test_missing_exit_code_is_treated_as_failure(self) -> None:
		"""A subshell that dies before recording an rc must not pass silently."""
		self.assertIn('|| echo 1', self.run)
		self.assertIn("''|*[!0-9]*) shard_rc=1 ;;", self.run)

	def test_every_shard_is_reaped_before_any_is_judged(self) -> None:
		reap = self.run.index('wait "${shard_pid}"')
		judge = self.run.index("shard_failures=0")
		self.assertLess(reap, judge, "shards must all be waited on before failures are tallied")

	def test_non_numeric_shard_count_falls_back_to_sequential(self) -> None:
		self.assertIn("shards=1", self.run)
		self.assertIn("is not a positive integer", self.run)

	def test_the_three_single_file_modules_still_run(self) -> None:
		for module in (
			"tests/test_orchestrate_poll_noop_suspicious_recovery.py",
			"tests/test_state_snapshot.py",
			"tests/test_run_substate_ledger.py",
		):
			self.assertIn(module, self.run)


class JobBudgetTest(unittest.TestCase):
	def test_lint_job_has_headroom_over_the_sharded_runtime(self) -> None:
		self.assertEqual(load_lint_job()["timeout-minutes"], 45)


def load_release_validate_scripts_job(workflow_path: Path) -> dict:
	return yaml.safe_load(workflow_path.read_text(encoding="utf-8"))["jobs"]["validate-scripts"]


def release_poll_step(workflow_path: Path) -> dict:
	for step in load_release_validate_scripts_job(workflow_path)["steps"]:
		if step.get("name") == "Orchestrate poll process unit tests":
			return step
	raise AssertionError(f"orchestrate-poll shard step not found in {workflow_path.name}")


def release_unit_tests_step(workflow_path: Path) -> dict:
	for step in load_release_validate_scripts_job(workflow_path)["steps"]:
		if step.get("name") == "Unit tests":
			return step
	raise AssertionError(f"Unit tests step not found in {workflow_path.name}")


def release_shard_awk_expression(workflow_path: Path) -> str:
	match = SHARD_AWK_RE.search(release_poll_step(workflow_path)["run"])
	if match is None:
		raise AssertionError(
			f"could not locate the shard-split awk expression in {workflow_path.name}"
		)
	return match.group("expr").strip()


class ReleaseValidateScriptsShardContractTest(unittest.TestCase):
	"""The release gates' port of the sharded step must not drift from ci.yml.

	`mark-stable.yml` and `test-and-mark-stable.yml` each run the module in
	their `validate-scripts` job. The port duplicates the awk split, so this
	class pins the same properties `PollStepContractTest` pins for ci.yml —
	a divergence here would reintroduce the silent-drop risk in the release
	path only, where it is least visible.
	"""

	def test_partition_expression_matches_the_verified_split(self) -> None:
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			with self.subTest(workflow=workflow_name):
				self.assertEqual(release_shard_awk_expression(workflow_path), "NR % total == n")

	def test_shard_count_is_configurable_with_the_same_default(self) -> None:
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			with self.subTest(workflow=workflow_name):
				self.assertEqual(
					release_poll_step(workflow_path)["env"]["CI_POLL_TEST_SHARDS"],
					"${{ vars.CI_POLL_TEST_SHARDS || '4' }}",
				)

	def test_a_failing_shard_fails_the_step(self) -> None:
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			with self.subTest(workflow=workflow_name):
				step_run = release_poll_step(workflow_path)["run"]
				self.assertIn("shard_failures=$((shard_failures + 1))", step_run)
				self.assertIn("exit 1", step_run)

	def test_missing_exit_code_is_treated_as_failure(self) -> None:
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			with self.subTest(workflow=workflow_name):
				step_run = release_poll_step(workflow_path)["run"]
				self.assertIn("|| echo 1", step_run)
				self.assertIn("''|*[!0-9]*) shard_rc=1 ;;", step_run)

	def test_every_shard_is_reaped_before_any_is_judged(self) -> None:
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			with self.subTest(workflow=workflow_name):
				step_run = release_poll_step(workflow_path)["run"]
				self.assertLess(
					step_run.index('wait "${shard_pid}"'),
					step_run.index("shard_failures=0"),
					"shards must all be waited on before failures are tallied",
				)

	def test_non_numeric_shard_count_falls_back_to_sequential(self) -> None:
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			with self.subTest(workflow=workflow_name):
				step_run = release_poll_step(workflow_path)["run"]
				self.assertIn("shards=1", step_run)
				self.assertIn("is not a positive integer", step_run)

	def test_shard_input_is_the_full_module_derived_in_step(self) -> None:
		"""No fast-fail split in the release gates: derive + shard the whole module."""
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			with self.subTest(workflow=workflow_name):
				step_run = release_poll_step(workflow_path)["run"]
				self.assertIn("/tmp/orchestrate_poll_all_tests.txt", step_run)
				self.assertIn('node.name.startswith("test_")', step_run)

	def test_partition_guard_runs_before_the_shards(self) -> None:
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			with self.subTest(workflow=workflow_name):
				step_run = release_poll_step(workflow_path)["run"]
				guard_invocation_match = re.search(
					r"(?m)^[ \t]*PYTHONDONTWRITEBYTECODE=1 python3 tests/test_ci_poll_test_sharding\.py$",
					step_run,
				)
				self.assertIsNotNone(guard_invocation_match)
				self.assertLess(
					guard_invocation_match.start(),
					step_run.index("/tmp/orchestrate_poll_all_tests.txt"),
					"the partition-contract guard must run before sharding",
				)

	def test_serial_invocation_is_gone_from_the_unit_tests_step(self) -> None:
		"""The module must run exactly once — in the sharded step."""
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			with self.subTest(workflow=workflow_name):
				self.assertNotIn(
					"test_orchestrate_poll_process.py",
					release_unit_tests_step(workflow_path)["run"],
				)

	def test_validate_scripts_job_has_headroom_over_the_sharded_runtime(self) -> None:
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			with self.subTest(workflow=workflow_name):
				self.assertEqual(
					load_release_validate_scripts_job(workflow_path)["timeout-minutes"], 45
				)


if __name__ == "__main__":
	unittest.main()

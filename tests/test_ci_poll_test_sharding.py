#!/usr/bin/env python3
"""Tests for the sharded orchestrate-poll step in ci.yml.

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


if __name__ == "__main__":
	unittest.main()

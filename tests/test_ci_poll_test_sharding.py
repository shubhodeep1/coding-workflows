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

The step now shards the module across `CI_POLL_TEST_SHARDS` workers, and
since #4707 the `orchestrate-poll` job runs as a matrix: each group first
takes its `NR % total == n` slice of the module, then shards that slice
locally with the same split. Two things have to hold for that to be safe:

  1. the group split and the shard split must each be a true partition,
     and so must their composition — every test runs exactly once, no
     duplicates and no silent drops;
  2. a failing shard must fail the step.

The partition property is what these tests pin hardest: a sharding bug
that drops tests would turn a green CI into a lie.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
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
GROUP_AWK_RE = re.compile(
	r"""awk -v n="\$\{poll_group\}" -v total="\$\{poll_groups\}" '(?P<expr>[^']+)'"""
)
POLL_JOB_ID = "orchestrate-poll"


def load_lint_job() -> dict:
	"""The aggregate `lint` job (it needs every other CI job since #4707)."""
	return yaml.safe_load(CI_WF.read_text(encoding="utf-8"))["jobs"]["lint"]


def load_poll_job() -> dict:
	return yaml.safe_load(CI_WF.read_text(encoding="utf-8"))["jobs"][POLL_JOB_ID]


def poll_job_step(step_name: str) -> dict:
	for step in load_poll_job()["steps"]:
		if step.get("name") == step_name:
			return step
	raise AssertionError(f"{step_name!r} step not found in the ci.yml {POLL_JOB_ID} job")


def poll_step() -> dict:
	for step in load_poll_job()["steps"]:
		if step.get("name") == "Orchestrate poll process unit tests":
			return step
	raise AssertionError("orchestrate-poll step not found in ci.yml")


def poll_group_count() -> int:
	return len(load_poll_job()["strategy"]["matrix"]["poll-group"])


def shard_awk_expression() -> str:
	match = SHARD_AWK_RE.search(poll_step()["run"])
	if match is None:
		raise AssertionError("could not locate the shard-split awk expression in ci.yml")
	return match.group("expr").strip()


def group_awk_expression() -> str:
	match = GROUP_AWK_RE.search(poll_step()["run"])
	if match is None:
		raise AssertionError("could not locate the group-split awk expression in ci.yml")
	return match.group("expr").strip()


class ShardPartitionTest(unittest.TestCase):
	"""The awk split must lose nothing and duplicate nothing."""

	def shard(self, lines: list[str], total: int, n: int, expression: str | None = None) -> list[str]:
		result = subprocess.run(
			["awk", "-v", f"n={n}", "-v", f"total={total}", expression or shard_awk_expression()],
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

	def assert_two_level_partition(self, count: int, groups: int, shards: int) -> None:
		"""Group split, then the shard split inside each group, as ci.yml runs them."""
		lines = [f"test_case_{i:04d}" for i in range(count)]
		collected: list[str] = []
		for group in range(groups):
			group_lines = self.shard(lines, groups, group, group_awk_expression())
			for n in range(shards):
				collected.extend(self.shard(group_lines, shards, n))
		self.assertCountEqual(collected, lines, f"count={count} groups={groups} shards={shards}")
		self.assertEqual(len(collected), len(set(collected)), "a test ran in more than one group or shard")

	def test_partition_holds_across_shard_counts(self) -> None:
		for total in (1, 2, 3, 4, 5, 8):
			with self.subTest(shards=total):
				self.assert_partition(307, total)

	def test_two_level_partition_holds_across_group_and_shard_counts(self) -> None:
		for groups in (1, 4, 5):
			for shards in (1, 4):
				with self.subTest(groups=groups, shards=shards):
					self.assert_two_level_partition(307, groups, shards)

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
		self.assert_two_level_partition(len(remaining_names), poll_group_count(), 4)


def run_shard_judge(step_run: str, shard_files: dict[int, dict[str, str]], shards: int = 4) -> subprocess.CompletedProcess:
	"""Run the step's shard judgment loop in bash against fake shard files.

	`shard_files` maps a shard index to the files it left behind (`txt`,
	`log`, `rc`: file contents; a missing key means no file). The loop is
	cut from the shipped step text and pointed at a temp dir, so the test
	exercises the real shell, not a restatement of it.
	"""
	start = step_run.index("shard_failures=0")
	tally_error = step_run.index("orchestrate-poll shard(s) failed.", start)
	end = step_run.index("\nfi\n", tally_error) + len("\nfi\n")
	with tempfile.TemporaryDirectory(prefix="test_shard_judge_") as shard_dir:
		for shard, files in shard_files.items():
			for suffix, content in files.items():
				Path(shard_dir, f"poll_shard_{shard}.{suffix}").write_text(content, encoding="utf-8")
		for shard in range(shards):
			Path(shard_dir, f"poll_shard_{shard}.txt").touch()
		script = step_run[start:end].replace("/tmp/poll_shard_", f"{shard_dir}/poll_shard_")
		return subprocess.run(
			["bash", "-c", f"set -euo pipefail\nshards={shards}\n{script}"],
			capture_output=True,
			text=True,
			check=False,
		)


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
		self.assertEqual(group_awk_expression(), "NR % total == n")

	def test_group_index_and_count_come_from_the_matrix(self) -> None:
		self.assertEqual(self.step["env"]["CI_POLL_TEST_GROUP_INDEX"], "${{ strategy.job-index }}")
		self.assertEqual(self.step["env"]["CI_POLL_TEST_GROUP_COUNT"], "${{ strategy.job-total }}")
		self.assertEqual(load_poll_job()["strategy"]["matrix"]["poll-group"], [0, 1, 2, 3])
		self.assertIs(load_poll_job()["strategy"]["fail-fast"], False)

	def test_shards_read_the_group_slice_not_the_whole_subset(self) -> None:
		group_split = self.run.index("/tmp/orchestrate_poll_group_tests.txt")
		shard_split = self.run.index('awk -v n="${shard}"')
		self.assertLess(group_split, shard_split)
		shard_loop = self.run[shard_split:]
		self.assertIn("/tmp/orchestrate_poll_group_tests.txt", shard_loop)
		self.assertNotIn("/tmp/orchestrate_poll_remaining_tests.txt", shard_loop)

	def test_invalid_group_fails_instead_of_dropping_tests(self) -> None:
		self.assertIn("Invalid orchestrate-poll group", self.run)
		guard = self.run.index("Invalid orchestrate-poll group")
		self.assertLess(guard, self.run.index("/tmp/orchestrate_poll_group_tests.txt"))

	def test_fast_fail_subset_runs_in_group_zero_only(self) -> None:
		fast_fail = poll_job_step("Orchestrate poll implementation-failed regression fast-fail")
		self.assertEqual(fast_fail["if"], "strategy.job-index == 0")

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
		group_zero_gate = self.run.index('if [ "${poll_group}" -eq 0 ]; then')
		for module in (
			"tests/test_orchestrate_poll_noop_suspicious_recovery.py",
			"tests/test_state_snapshot.py",
			"tests/test_run_substate_ledger.py",
		):
			self.assertIn(module, self.run)
			self.assertEqual(self.run.count(module), 1)
			self.assertLess(group_zero_gate, self.run.index(module), "single-file modules run once, in group 0")


class JobBudgetTest(unittest.TestCase):
	def test_poll_job_has_headroom_over_the_sharded_runtime(self) -> None:
		# About 5 minutes per group measured (#4707); 20 covers a cold runner.
		self.assertEqual(load_poll_job()["timeout-minutes"], 20)

	def test_lint_aggregate_job_only_reads_results(self) -> None:
		self.assertEqual(load_lint_job()["timeout-minutes"], 5)

	def test_e2e_smoke_job_has_headroom_for_all_phase_budgets(self) -> None:
		e2e_smoke_job = yaml.safe_load(
			RELEASE_WORKFLOWS["test-and-mark-stable"].read_text(encoding="utf-8")
		)["jobs"]["e2e-smoke-test"]
		self.assertEqual(
			e2e_smoke_job["timeout-minutes"],
			e2e_smoke_job["env"]["E2E_JOB_TIMEOUT_MINUTES"],
		)


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
					load_release_validate_scripts_job(workflow_path)["timeout-minutes"], 60
				)


class ShardJudgeTest(unittest.TestCase):
	"""A shard that had tests but left no log must fail the step (#4884 review round 2).

	The judgment loop used to `continue` past any shard without a log, so a
	shard whose subshell died before its runner started was never counted
	and the step could pass on a partial run. Empty shards (more workers
	than tests) still have nothing to judge and are skipped.
	"""

	def step_runs(self) -> dict[str, str]:
		runs = {"ci": poll_step()["run"]}
		for workflow_name, workflow_path in RELEASE_WORKFLOWS.items():
			runs[workflow_name] = release_poll_step(workflow_path)["run"]
		return runs

	def test_all_shards_green_passes(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				result = run_shard_judge(
					step_run,
					{n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(4)},
				)
				self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

	def test_shard_with_tests_but_no_log_fails(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				shard_files = {n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(4)}
				shard_files[2] = {"txt": "test_2\n"}
				result = run_shard_judge(step_run, shard_files)
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				self.assertIn("orchestrate-poll shard 2 produced no log", result.stdout)
				self.assertIn("1 orchestrate-poll shard(s) failed.", result.stdout)

	def test_empty_shard_without_log_is_skipped(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				shard_files = {n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(3)}
				result = run_shard_judge(step_run, shard_files)
				self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
				self.assertNotIn("produced no log", result.stdout)

	def test_shard_with_log_but_no_rc_still_fails(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				shard_files = {n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(4)}
				shard_files[1] = {"txt": "test_1\n", "log": "partial\n"}
				result = run_shard_judge(step_run, shard_files)
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				self.assertIn("orchestrate-poll shard 1 failed (exit 1)", result.stdout)


if __name__ == "__main__":
	unittest.main()

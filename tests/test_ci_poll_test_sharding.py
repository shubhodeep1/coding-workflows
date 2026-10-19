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

	def test_failing_test_summary_precedes_the_tally_and_exit(self) -> None:
		"""#6888: the failing tests are named after the loop, then the gate fails."""
		summary = self.run.index("orchestrate-poll failing test:")
		tally = self.run.index("orchestrate-poll shard(s) failed.")
		self.assertLess(self.run.index('wait "${shard_pid}"'), summary)
		self.assertLess(summary, tally)
		self.assertLess(tally, self.run.index("exit 1", tally))

	def test_every_shard_is_reaped_before_any_is_judged(self) -> None:
		reap = self.run.index('wait "${shard_pid}"')
		judge = self.run.index("shard_failures=0")
		self.assertLess(reap, judge, "shards must all be waited on before failures are tallied")

	def test_non_numeric_shard_count_falls_back_to_sequential(self) -> None:
		self.assertIn("shards=1", self.run)
		self.assertIn("is not a positive integer", self.run)

	def test_failure_summary_prints_after_the_loop_and_before_exit(self) -> None:
		"""Failing test names must reach the log tail, outside every ::group:: (#6889)."""
		judge = self.run.index("shard_failures=0")
		loop_end = self.run.index("\ndone\n", judge)
		summary_print = self.run.index('echo "::error::${shard_failure_summary}"')
		tally = self.run.index("orchestrate-poll shard(s) failed.")
		exit_line = self.run.index("exit 1", tally)
		self.assertLess(loop_end, summary_print)
		self.assertLess(summary_print, tally)
		self.assertLess(summary_print, exit_line)
		self.assertIn("shard_failure_summaries=()", self.run)
		self.assertIn("grep -a -E '^  FAIL  [A-Za-z0-9_]+:'", self.run)
		self.assertNotIn(" rg ", self.run)

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

	def test_all_shards_green_prints_no_failure_summary(self) -> None:
		result = run_shard_judge(
			poll_step()["run"],
			{n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(4)},
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertNotIn("failed tests:", result.stdout)

	def test_failure_summary_names_the_failing_test_in_the_tail(self) -> None:
		"""Summary lines are ci.yml-only; the release gates keep the old loop (#6889 D2)."""
		shard_files = {n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(4)}
		shard_files[1] = {
			"txt": "test_a\ntest_b\n",
			"log": "  PASS  test_a\n  FAIL  test_b: boom\n2 passed, 1 failed, 3 total\n",
			"rc": "1\n",
		}
		result = run_shard_judge(poll_step()["run"], shard_files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		summary = "::error::orchestrate-poll shard 1 exit 1 failed tests: test_b\n"
		self.assertIn(summary, result.stdout)
		self.assertLess(result.stdout.rindex("::endgroup::"), result.stdout.index(summary))
		self.assertLess(
			result.stdout.index(summary),
			result.stdout.index("1 orchestrate-poll shard(s) failed."),
		)

	def test_failure_summary_without_fail_lines_reports_process_failure(self) -> None:
		shard_files = {n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(4)}
		shard_files[0] = {"txt": "test_0\n", "log": "Traceback: boom\n", "rc": "2\n"}
		result = run_shard_judge(poll_step()["run"], shard_files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn(
			"::error::orchestrate-poll shard 0 exit 2 failed tests: no FAIL lines (process failure)",
			result.stdout,
		)

	def test_failure_summary_for_a_shard_without_log(self) -> None:
		shard_files = {n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(4)}
		shard_files[3] = {"txt": "test_3\n"}
		result = run_shard_judge(poll_step()["run"], shard_files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn(
			"::error::orchestrate-poll shard 3 exit none failed tests: no log (tests did not run)",
			result.stdout,
		)

	def test_failure_summary_caps_names_at_twenty(self) -> None:
		fail_lines = "".join(f"  FAIL  test_case_{i:02d}: boom\n" for i in range(25))
		shard_files = {n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(4)}
		shard_files[2] = {"txt": "test_2\n", "log": fail_lines, "rc": "1\n"}
		result = run_shard_judge(poll_step()["run"], shard_files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		expected_names = ", ".join(f"test_case_{i:02d}" for i in range(20))
		self.assertIn(
			f"::error::orchestrate-poll shard 2 exit 1 failed tests: {expected_names} (+5 more)\n",
			result.stdout,
		)

	def test_failure_summary_never_echoes_failure_text(self) -> None:
		shard_files = {n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(4)}
		shard_files[1] = {
			"txt": "test_1\n",
			"log": "  FAIL  test_x: ::warning::forged ::add-mask::x\n  FAIL  bad-name!: oops\n",
			"rc": "1\n",
		}
		result = run_shard_judge(poll_step()["run"], shard_files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		summary_lines = [line for line in result.stdout.splitlines() if "failed tests:" in line]
		self.assertEqual(summary_lines, ["::error::orchestrate-poll shard 1 exit 1 failed tests: test_x"])

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
				self.assertIn("::error::orchestrate-poll shard 1 had no FAIL line", result.stdout)
				self.assertIn("orchestrate-poll shard 1 | partial", result.stdout)

	def green_shards(self) -> dict[int, dict[str, str]]:
		return {n: {"txt": f"test_{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(4)}

	def test_failed_shard_annotates_its_fail_lines(self) -> None:
		"""A failing shard names its failing tests in ::error:: annotations (#6885)."""
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				shard_files = self.green_shards()
				shard_files[1] = {
					"txt": "test_a\ntest_b\n",
					"log": "  PASS  test_a\n  FAIL  test_b: boom 100%\n1 passed, 1 failed, 2 total\n",
					"rc": "1\n",
				}
				result = run_shard_judge(step_run, shard_files)
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				self.assertIn("::error::orchestrate-poll shard 1: FAIL  test_b: boom 100%25\n", result.stdout)
				self.assertNotIn("::error::orchestrate-poll shard 1: PASS", result.stdout)
				self.assertNotIn("had no FAIL line", result.stdout)
				self.assertIn("1 orchestrate-poll shard(s) failed.", result.stdout)

	def test_fail_lines_must_start_with_the_runner_prefix(self) -> None:
		"""Assertion text that merely mentions FAIL is not a runner FAIL line."""
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				shard_files = self.green_shards()
				shard_files[1] = {"txt": "test_a\n", "log": "expected FAIL  marker\nboom\n", "rc": "1\n"}
				result = run_shard_judge(step_run, shard_files)
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				self.assertNotIn("::error::orchestrate-poll shard 1: ", result.stdout)
				self.assertIn("::error::orchestrate-poll shard 1 had no FAIL line", result.stdout)

	def test_failed_shard_without_fail_line_prints_log_tail(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				log_lines = [f"line {i:02d}" for i in range(1, 31)]
				log_lines[24] = "::warning::injected from the log"
				shard_files = self.green_shards()
				shard_files[1] = {"txt": "test_a\n", "log": "\n".join(log_lines) + "\n", "rc": "137\n"}
				result = run_shard_judge(step_run, shard_files)
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				self.assertIn("orchestrate-poll shard 1 failed (exit 137)", result.stdout)
				self.assertIn("::error::orchestrate-poll shard 1 had no FAIL line; last log lines follow", result.stdout)
				tail_lines = [
					line for line in result.stdout.splitlines() if line.startswith("orchestrate-poll shard 1 | ")
				]
				self.assertEqual(
					tail_lines,
					[f"orchestrate-poll shard 1 | {line}" for line in log_lines[10:]],
				)
				self.assertNotIn("orchestrate-poll shard 1 | line 10", result.stdout)
				# The raw log line appears once, from the existing ::group:: cat;
				# the tail copy carries the fixed prefix.
				raw_warning_lines = [
					line for line in result.stdout.splitlines() if line.startswith("::warning::")
				]
				self.assertEqual(raw_warning_lines, ["::warning::injected from the log"])

	def test_fail_annotations_are_capped(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				shard_files = self.green_shards()
				shard_files[1] = {
					"txt": "test_a\n",
					"log": "".join(f"  FAIL  test_{i:02d}: boom\n" for i in range(25)),
					"rc": "1\n",
				}
				result = run_shard_judge(step_run, shard_files)
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				annotations = [
					line
					for line in result.stdout.splitlines()
					if line.startswith("::error::orchestrate-poll shard 1: ")
				]
				self.assertEqual(len(annotations), 20)
				self.assertEqual(annotations[0], "::error::orchestrate-poll shard 1: FAIL  test_00: boom")
				self.assertEqual(annotations[-1], "::error::orchestrate-poll shard 1: FAIL  test_19: boom")

	def test_passing_shard_emits_no_fail_annotation(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				shard_files = self.green_shards()
				shard_files[2] = {"txt": "test_2\n", "log": "  FAIL  stale: noise\n", "rc": "0\n"}
				result = run_shard_judge(step_run, shard_files)
				self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
				self.assertNotIn("::error::", result.stdout)
				self.assertNotIn("had no FAIL line", result.stdout)

	def test_judgment_loop_is_identical_across_workflows(self) -> None:
		"""CI and both release gates must annotate failed shards with the same text (#6885).

		Only the #6885 annotation block is compared: ci.yml's loop also carries
		the ci-only #6889/#6946 failure-summary code, which the release gates
		intentionally do not have.
		"""
		loops = {}
		for workflow_name, step_run in self.step_runs().items():
			start = step_run.index("# Name the failing tests in annotations (#6885)")
			end = step_run.index("shard_failures=$((shard_failures + 1))", start)
			loops[workflow_name] = step_run[start:end]
		self.assertIn("^  FAIL  ", loops["ci"])
		for workflow_name, loop in loops.items():
			with self.subTest(workflow=workflow_name):
				self.assertEqual(loop, loops["ci"])


class ShardFailureSummaryTest(unittest.TestCase):
	"""A failed shard's failing tests are named after the loop (#6888).

	In run 37907017153 the failing shard's log sat inside a collapsed
	`::group::`, so the job tail showed only the last shard's "33 passed"
	and the failing test could not be identified. The judgment loop now
	repeats each failed shard's FAIL lines as `::error::` lines just
	before the tally, in all three workflow copies.
	"""

	FAILING = "::error::orchestrate-poll failing test: "

	def step_runs(self) -> dict[str, str]:
		return ShardJudgeTest.step_runs(self)

	def judge(self, step_run: str, shard_one: dict[str, str]) -> subprocess.CompletedProcess:
		shard_files = {n: {"txt": f"test_{n}\n", "log": "  PASS  ok\n", "rc": "0\n"} for n in range(4)}
		shard_files[1] = shard_one
		return run_shard_judge(step_run, shard_files)

	def summary_lines(self, stdout: str) -> list[str]:
		return [line for line in stdout.splitlines() if line.startswith(self.FAILING)]

	def test_fail_line_is_named_after_the_groups_and_before_the_tally(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				result = self.judge(step_run, {
					"txt": "test_a\ntest_b\n",
					"log": "  PASS  test_a\n  FAIL  test_b: boom\n",
					"rc": "1\n",
				})
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				expected = self.FAILING + "shard 1: FAIL  test_b: boom"
				self.assertEqual(self.summary_lines(result.stdout), [expected])
				out = result.stdout
				self.assertLess(out.rindex("::endgroup::"), out.index(expected))
				self.assertLess(out.index(expected), out.index("1 orchestrate-poll shard(s) failed."))

	def test_complete_fail_event_is_the_fallback(self) -> None:
		complete = 'TEST_CASE_EVENT: {"elapsed_ms":5,"event":"complete","status":"fail","test_name":"test_c"}'
		slowest = 'TEST_CASE_EVENT: {"elapsed_ms":5,"event":"slowest","rank":1,"status":"fail","test_name":"test_c"}'
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				result = self.judge(step_run, {
					"txt": "test_c\n",
					"log": f"{complete}\n{slowest}\n",
					"rc": "1\n",
				})
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				summary = self.summary_lines(result.stdout)
				self.assertEqual(len(summary), 1, summary)
				self.assertIn('"test_name":"test_c"', summary[0])
				self.assertIn('"event":"complete"', summary[0])

	def test_failed_shard_without_fail_line_says_so(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				result = self.judge(step_run, {"txt": "test_d\n", "log": "partial\n", "rc": "1\n"})
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				self.assertEqual(
					self.summary_lines(result.stdout),
					[self.FAILING + "shard 1: exit 1, no FAIL line recorded (runner likely died)"],
				)

	def test_shard_without_log_is_named(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				result = self.judge(step_run, {"txt": "test_e\n"})
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				self.assertEqual(
					self.summary_lines(result.stdout),
					[self.FAILING + "shard 1: no log recorded, its tests did not run"],
				)

	def test_nul_byte_in_log_still_names_the_failing_test(self) -> None:
		"""A NUL byte must not turn the matches into grep's "Binary file" line."""
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				result = self.judge(step_run, {
					"txt": "test_n\n",
					"log": "noise\x00here\n  FAIL  test_n: boom\n",
					"rc": "1\n",
				})
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				self.assertEqual(
					self.summary_lines(result.stdout),
					[self.FAILING + "shard 1: FAIL  test_n: boom"],
				)

	def test_percent_is_escaped_and_carriage_return_removed(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				result = self.judge(step_run, {
					"txt": "test_p\n",
					"log": "  FAIL  test_p: 100% bad\r\n",
					"rc": "1\n",
				})
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				self.assertEqual(
					self.summary_lines(result.stdout),
					[self.FAILING + "shard 1: FAIL  test_p: 100%25 bad"],
				)
				summary_text = result.stdout[result.stdout.index(self.FAILING):]
				self.assertNotIn("\r", summary_text)

	def test_long_messages_are_cut(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				result = self.judge(step_run, {
					"txt": "test_l\n",
					"log": "  FAIL  test_l: " + "x" * 1000 + "\n",
					"rc": "1\n",
				})
				summary = self.summary_lines(result.stdout)
				self.assertEqual(len(summary), 1)
				self.assertEqual(len(summary[0]) - len(self.FAILING), 400)

	def test_summary_is_capped_with_an_overflow_line(self) -> None:
		log = "".join(f"  FAIL  test_{i:02d}: boom\n" for i in range(30))
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				shard_files = {n: {"txt": f"test_{n}\n", "log": log, "rc": "1\n"} for n in range(2)}
				result = run_shard_judge(step_run, shard_files)
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				summary = self.summary_lines(result.stdout)
				named = [line for line in summary if line.startswith(self.FAILING + "shard ")]
				# 30 per shard collected (60), 20 shown, 40 more reported.
				self.assertEqual(len(named), 20)
				self.assertEqual(summary[-1], self.FAILING + "... and 40 more (see the shard groups above)")
				self.assertIn("2 orchestrate-poll shard(s) failed.", result.stdout)

	def test_single_shard_overflow_is_reported(self) -> None:
		log = "".join(f"  FAIL  test_{i:02d}: boom\n" for i in range(25))
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				result = self.judge(step_run, {"txt": "test_s\n", "log": log, "rc": "1\n"})
				self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
				summary = self.summary_lines(result.stdout)
				self.assertEqual(len(summary), 21, summary)
				self.assertEqual(summary[-1], self.FAILING + "... and 5 more (see the shard groups above)")

	def test_passing_shards_are_not_scanned(self) -> None:
		for workflow_name, step_run in self.step_runs().items():
			with self.subTest(workflow=workflow_name):
				result = self.judge(step_run, {
					"txt": "test_f\n",
					"log": "  FAIL  not_really: printed by a passing test\n",
					"rc": "0\n",
				})
				self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
				self.assertNotIn("failing test:", result.stdout)


SUMMARY_HEADING = "orchestrate-poll failed-shard summary"
COUNT_LINE = "orchestrate-poll shard(s) failed."


def green_shards(count: int = 4) -> dict[int, dict[str, str]]:
	return {
		n: {"txt": f"test_{n}\n", "log": "33 passed, 0 failed, 33 total\n", "rc": "0\n"}
		for n in range(count)
	}


class CiFailedShardLogSummaryTest(unittest.TestCase):
	"""A failed shard's failing tests must reach the end of the step log (#6946).

	Each shard log is printed in a collapsed group, so before this summary a
	log tail (and the check-failure triage that reads one) showed only the
	last shard's passing summary. ci.yml only: the release-gate copies are
	intentionally unchanged.
	"""

	def run_ci(self, shard_files: dict[int, dict[str, str]]) -> subprocess.CompletedProcess:
		return run_shard_judge(poll_step()["run"], shard_files)

	def test_failing_shard_is_summarised_after_the_last_group(self) -> None:
		shard_files = green_shards()
		shard_files[1] = {
			"txt": "test_alpha\ntest_beta\n",
			"log": (
				"  PASS  test_alpha\n"
				"  FAIL  test_beta: boom\n"
				"Traceback (most recent call last): sentinel-traceback\n"
				"33 passed, 1 failed, 34 total\n"
			),
			"rc": "1\n",
		}
		result = self.run_ci(shard_files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		out = result.stdout
		last_group_end = out.rindex("::endgroup::")
		summary_fail_line = out.index("    |   FAIL  test_beta: boom")
		count_line = out.index(f"1 {COUNT_LINE}")
		self.assertLess(last_group_end, out.index(SUMMARY_HEADING))
		self.assertLess(last_group_end, summary_fail_line)
		self.assertLess(summary_fail_line, count_line)
		self.assertIn("::error::orchestrate-poll shard 1 failing tests: test_beta\n", out)
		self.assertGreater(out.rindex("sentinel-traceback"), last_group_end)

	def test_shard_without_log_is_counted_and_summarised(self) -> None:
		shard_files = green_shards()
		shard_files[2] = {"txt": "test_2\n"}
		result = self.run_ci(shard_files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		out = result.stdout
		self.assertIn(f"1 {COUNT_LINE}", out)
		self.assertGreater(
			out.index("shard 2: no log; its tests did not run"), out.rindex("::endgroup::")
		)

	def test_shard_without_rc_is_counted_and_its_tail_summarised(self) -> None:
		shard_files = green_shards()
		shard_files[3] = {"txt": "test_3\n", "log": "runner killed sentinel-kill\n"}
		result = self.run_ci(shard_files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		out = result.stdout
		self.assertIn("orchestrate-poll shard 3 failed (exit 1)", out)
		self.assertIn(f"1 {COUNT_LINE}", out)
		self.assertGreater(
			out.index("    | runner killed sentinel-kill"), out.rindex("::endgroup::")
		)

	def test_all_green_prints_no_summary(self) -> None:
		result = self.run_ci(green_shards())
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertNotIn(SUMMARY_HEADING, result.stdout)

	def test_log_text_cannot_forge_workflow_commands_in_the_summary(self) -> None:
		shard_files = green_shards()
		shard_files[0] = {
			"txt": "test_0\n",
			"log": "::error::forged\n::endgroup::\n  FAIL  bad%0Aname: x\n",
			"rc": "1\n",
		}
		result = self.run_ci(shard_files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		out = result.stdout
		summary = out[out.index(SUMMARY_HEADING):]
		for line in summary.splitlines():
			if line.startswith("::"):
				# The #6888 per-test annotations are also emitted here, escaped.
				self.assertTrue(
					line.startswith("::error::orchestrate-poll shard ")
					or line.startswith(ShardFailureSummaryTest.FAILING)
					or line == f"::error::1 {COUNT_LINE}",
					f"unexpected workflow command in summary: {line!r}",
				)
		self.assertIn("    | ::error::forged", summary)
		self.assertIn("    | ::endgroup::", summary)
		for line in out.splitlines():
			if line.startswith("::error::"):
				self.assertNotIn("bad%0Aname", line)

	def test_summary_precedes_the_count_line_and_both_branches_record_the_shard(self) -> None:
		step_run = poll_step()["run"]
		self.assertLess(step_run.index(SUMMARY_HEADING), step_run.index(COUNT_LINE))
		self.assertEqual(step_run.count(COUNT_LINE), 1)
		self.assertEqual(step_run.count('failed_shard_ids+=("${shard}")'), 2)
		no_log_branch = step_run.index("produced no log; its tests did not run")
		rc_branch = step_run.index('failed (exit ${shard_rc})')
		first_append = step_run.index('failed_shard_ids+=("${shard}")')
		second_append = step_run.index('failed_shard_ids+=("${shard}")', first_append + 1)
		self.assertLess(no_log_branch, first_append)
		self.assertLess(first_append, rc_branch)
		self.assertLess(rc_branch, second_append)


MERGE_TRAIN_STEP_NAME = "Merge-train and gh_helpers rate-limit tests (CLAUDE.md §15)"
MERGE_TRAIN_MODULE = "tests/test_review_merge_train.py"
MERGE_TRAIN_SMALL_MODULES = (
	"tests/test_gh_helpers_parse_reset_header.py",
	"tests/test_gh_retry_stdout_isolation.py",
	"tests/test_gh_retry_permanent_auth_failures.py",
	"tests/test_gh_api_retry.py",
	"tests/test_gh_api_retry_py.py",
)
MERGE_TRAIN_AWK_RE = re.compile(
	r"""awk -v n="\$\{mt_shard\}" -v total="\$\{mt_shards\}" '(?P<expr>[^']+)'"""
)


def run_merge_train_judge(step_run: str, files: dict[str, dict[str, str]]) -> subprocess.CompletedProcess:
	"""Run the merge-train step's judgment part against fake shard files.

	`files` maps a shard id (`"0"`..`"2"` or `"rest"`) to the files it left
	(`txt`, `log`, `rc`). The cut starts after the reap loop and ends at the
	final exit, so the shipped shell is exercised, pointed at a temp dir.
	"""
	start = step_run.index("mt_ids=()")
	end = step_run.index("\nfi\n", step_run.index("merge-train step shards failed")) + len("\nfi\n")
	with tempfile.TemporaryDirectory(prefix="test_mt_judge_") as tmp:
		for shard_id, shard_files in files.items():
			stem = "merge_train_rest" if shard_id == "rest" else f"merge_train_shard_{shard_id}"
			for suffix, content in shard_files.items():
				Path(tmp, f"{stem}.{suffix}").write_text(content, encoding="utf-8")
		script = step_run[start:end].replace("/tmp/merge_train_", f"{tmp}/merge_train_")
		return subprocess.run(
			["bash", "-c", f"set -euo pipefail\nmt_shards=3\n{script}"],
			capture_output=True,
			text=True,
			check=False,
		)


class MergeTrainStepContractTest(unittest.TestCase):
	"""The merge-train step runs its largest module in local shards (#7579).

	Group 1 of `orchestrate-poll` took 15m13s of its 20 minutes with this step
	running serially. The step stays in place, by name and guard, and splits
	tests/test_review_merge_train.py across 3 processes beside one process for
	the small modules. Every test must still run exactly once, and a shard that
	fails or records no exit code must fail the step.
	"""

	def setUp(self) -> None:
		self.step = poll_job_step(MERGE_TRAIN_STEP_NAME)
		self.run = self.step["run"]

	def green(self) -> dict[str, dict[str, str]]:
		files = {str(n): {"txt": f"t{n}\n", "log": "ok\n", "rc": "0\n"} for n in range(3)}
		files["rest"] = {"log": "ok\n", "rc": "0\n"}
		return files

	def test_step_keeps_its_name_job_and_group_guard(self) -> None:
		self.assertEqual(self.step["if"], "strategy.job-index == 1")
		jobs = yaml.safe_load(CI_WF.read_text(encoding="utf-8"))["jobs"]
		owners = [
			job_id
			for job_id, job in jobs.items()
			for step in job.get("steps", [])
			if step.get("name") == MERGE_TRAIN_STEP_NAME
		]
		self.assertEqual(owners, [POLL_JOB_ID])

	def test_all_six_modules_are_named_literally(self) -> None:
		for module in (MERGE_TRAIN_MODULE, *MERGE_TRAIN_SMALL_MODULES):
			with self.subTest(module=module):
				self.assertIn(module, self.run)

	def test_partition_and_collection_are_derived_in_step(self) -> None:
		match = MERGE_TRAIN_AWK_RE.search(self.run)
		self.assertIsNotNone(match)
		self.assertEqual(match.group("expr").strip(), "NR % total == n")
		self.assertIn('--collect-only -q -p no:cacheprovider "${mt_module}"', self.run)
		self.assertIn("mt_shards=3", self.run)
		self.assertIn("No tests collected from", self.run)

	def test_serial_invocation_is_gone(self) -> None:
		self.assertNotIn("python3 -m pytest -q -p no:cacheprovider \\\n  tests/test_review_merge_train.py", self.run)
		self.assertNotIn("tests/test_review_merge_train.py \\", self.run)

	def test_every_process_is_reaped_before_any_is_judged(self) -> None:
		self.assertLess(self.run.index('wait "${mt_pid}"'), self.run.index("mt_failed=()"))

	def test_partition_covers_every_test_function_once(self) -> None:
		import ast

		module = ast.parse((REPO_ROOT / MERGE_TRAIN_MODULE).read_text(encoding="utf-8"))
		names = [
			node.name
			for node in ast.walk(module)
			if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
		]
		self.assertGreater(len(names), 0)
		expression = MERGE_TRAIN_AWK_RE.search(self.run).group("expr").strip()
		collected: list[str] = []
		for n in range(3):
			collected.extend(ShardPartitionTest.shard(self, names, 3, n, expression))
		self.assertCountEqual(collected, names)

	def test_all_green_passes(self) -> None:
		result = run_merge_train_judge(self.run, self.green())
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertNotIn("failed", result.stdout)

	def test_failing_shard_fails_the_step_with_prefixed_tail(self) -> None:
		files = self.green()
		files["1"] = {"txt": "t1\n", "log": "::error::forged\nFAILED x\n", "rc": "1\n"}
		result = run_merge_train_judge(self.run, files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("::error::merge-train step shards failed: 1\n", result.stdout)
		tail = result.stdout[result.stdout.index("shard 1 failed; last 40 log lines"):]
		self.assertIn("| ::error::forged", tail)
		self.assertNotIn("\n::error::forged", tail)

	def test_missing_rc_counts_as_failure(self) -> None:
		files = self.green()
		files["rest"] = {"log": "partial\n"}
		result = run_merge_train_judge(self.run, files)
		self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
		self.assertIn("::error::merge-train step shards failed: rest\n", result.stdout)

	def test_empty_shard_is_skipped(self) -> None:
		files = self.green()
		del files["2"]
		result = run_merge_train_judge(self.run, files)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


# Steps moved into single orchestrate-poll groups from tests-heal-plan-and-validation
# (#7690). Without its guard such a step would run in all four groups.
GROUP_PINNED_STEPS = {
	"Regression tests wired by the CI wiring guard (part 2)": 2,
	"Unblock judge, scan and orphan fix-up close tests (replace-claude-sessions Phase 7)": 3,
}
POLL_JOB_UNGUARDED_STEPS = {
	"Checkout repository",
	"Setup Python",
	"Install Python CI dependencies",
	"Derive orchestrate poll test subsets",
	"Orchestrate poll process unit tests",
}


class PollGroupPinnedStepsTest(unittest.TestCase):
	"""Steps added to the orchestrate-poll matrix must run in one group only (#7690)."""

	def test_moved_steps_run_once_in_their_group(self) -> None:
		jobs = yaml.safe_load(CI_WF.read_text(encoding="utf-8"))["jobs"]
		for step_name, group in GROUP_PINNED_STEPS.items():
			with self.subTest(step=step_name):
				owners = [
					job_id
					for job_id, job in jobs.items()
					for step in job.get("steps", [])
					if step.get("name") == step_name
				]
				self.assertEqual(owners, [POLL_JOB_ID])
				self.assertEqual(poll_job_step(step_name).get("if"), f"strategy.job-index == {group}")

	def test_every_other_poll_job_step_is_group_guarded(self) -> None:
		guard_re = re.compile(r"strategy\.job-index == [0-9]+")
		for step in load_poll_job()["steps"]:
			step_name = step.get("name")
			if step_name in POLL_JOB_UNGUARDED_STEPS:
				continue
			with self.subTest(step=step_name):
				self.assertRegex(str(step.get("if", "")), guard_re)


# Steps moved from tests-release-and-log-analysis (budget-watch issue #7689):
# step name -> (matrix group that runs it, module paths its body names).
MOVED_RELEASE_STEPS = {
	"Promote cycle and blocked-verdict unit tests": (
		2,
		(
			"tests/test_promote_main_cycle.py",
			"tests/test_apply_analysis_on_main.py",
			"tests/test_auto_release_stable.py",
			"tests/test_orchestrate_poll_promote_cycle.py",
			"tests/test_implement_blocked_verdict_terminalizes.py",
		),
	),
	"Review-blocked judge label propagation contract test": (
		2,
		("tests/test_review_rb_judge_label_propagation.py",),
	),
	"Orchestrator judges on the Claude engine tests (Phase 5c)": (
		3,
		("tests/test_orchestrator_judges_claude_engine.py",),
	),
	"Validation harness sandbox tests": (
		3,
		("tests/test_validation_harness_sandbox.py",),
	),
}


class MovedReleaseStepsContractTest(unittest.TestCase):
	"""Steps moved into the idle poll groups run once per CI run (#7689).

	`orchestrate-poll` is a 4-way matrix, so each moved step needs a
	single-group guard: without it the step runs four times, and a guard
	naming a group the matrix no longer has would silently never run it.
	"""

	def test_each_step_is_guarded_to_its_group(self) -> None:
		for name, (group, _modules) in MOVED_RELEASE_STEPS.items():
			with self.subTest(step=name):
				self.assertEqual(poll_job_step(name)["if"], f"strategy.job-index == {group}")

	def test_guards_avoid_the_loaded_groups_and_stay_in_the_matrix(self) -> None:
		group_count = poll_group_count()
		for name, (group, _modules) in MOVED_RELEASE_STEPS.items():
			with self.subTest(step=name):
				self.assertNotIn(group, (0, 1), "groups 0 and 1 already carry extra steps")
				self.assertLess(group, group_count, "guard names a group the matrix does not run")

	def test_each_step_is_owned_only_by_the_poll_job(self) -> None:
		jobs = yaml.safe_load(CI_WF.read_text(encoding="utf-8"))["jobs"]
		for name in MOVED_RELEASE_STEPS:
			with self.subTest(step=name):
				owners = [
					job_id
					for job_id, job in jobs.items()
					for step in job.get("steps", [])
					if step.get("name") == name
				]
				self.assertEqual(owners, [POLL_JOB_ID])

	def test_each_step_still_names_its_modules(self) -> None:
		for name, (_group, modules) in MOVED_RELEASE_STEPS.items():
			for module in modules:
				with self.subTest(step=name, module=module):
					self.assertIn(module, poll_job_step(name)["run"])
					self.assertTrue((REPO_ROOT / module).is_file(), module)


if __name__ == "__main__":
	unittest.main()

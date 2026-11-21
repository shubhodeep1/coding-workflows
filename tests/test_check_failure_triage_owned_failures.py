#!/usr/bin/env python3
"""Failures another path already owns, in scripts/check_failure_triage.sh.

Measured on the triage issues behind fix PRs #9597, #9598, #9601, #9603, #9614
and #9621 (2026-11-16/17), each of which landed on another PR's branch:

* Issues #9580, #9581 and #9582 were filed against failing commits that were
  no longer the PR head (``pr_head_advanced=true``), three near-duplicates
  within three minutes. The new head gets its own CI, so triage now skips
  (``skip reason=head_advanced``).
* Issues #9608, #9618 and #9662 came from failed final CI runs
  (``CI [pr:<n>]``), which ci.yml's ``final-ci-failed`` hand-back already
  sends back to review/autofix. Triage now leaves them to it while the
  hand-back is on (``skip reason=final_ci_repair_owns``).

These cases run the collect stage against the fake ``gh`` of
tests/test_check_failure_triage_workflow_security.py.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
_HARNESS_MODULE_NAME = "check_triage_owned_failures_harness"


def _harness():
	"""The security test module, loaded once under its own name."""
	module = sys.modules.get(_HARNESS_MODULE_NAME)
	if module is None:
		spec = importlib.util.spec_from_file_location(
			_HARNESS_MODULE_NAME, TESTS_DIR / "test_check_failure_triage_workflow_security.py"
		)
		assert spec and spec.loader
		module = importlib.util.module_from_spec(spec)
		sys.modules[_HARNESS_MODULE_NAME] = module
		try:
			spec.loader.exec_module(module)
		except BaseException:
			sys.modules.pop(_HARNESS_MODULE_NAME, None)
			raise
	return module


FAILING_SHA = "a" * 40  # the harness's failing check SHA
NEW_HEAD = "b" * 40


def _collect(*, pr_head_sha: str = FAILING_SHA, check_name: str = "CI", env: dict[str, str] | None = None):
	extra = {"CHECK_TRIAGE_CHECK_NAME": check_name, **(env or {})}
	calls: list[str] = []
	proc, outputs, metadata = _harness()._run_collect_stage(
		parent_body="", pr_head_sha=pr_head_sha, extra_env=extra, calls_path=calls,
	)
	return proc, outputs, metadata, calls


class HeadAdvancedGateTests(unittest.TestCase):
	def test_failure_on_an_old_head_files_nothing(self) -> None:
		proc, outputs, metadata, calls = _collect(pr_head_sha=NEW_HEAD)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn(
			f"CHECK_TRIAGE skip reason=head_advanced failing_sha={FAILING_SHA} pr_head_sha={NEW_HEAD} pr=17 check=CI",
			proc.stdout,
		)
		self.assertNotIn("ready", outputs)
		self.assertEqual(metadata, {})
		# Decided from the PR read alone: no base, run or issue read follows.
		self.assertFalse([call for call in calls if "/actions/" in call or "/issues" in call], calls)
		self.assertNotIn("routing_unverified", proc.stdout)

	def test_upper_case_shas_compare_equal(self) -> None:
		proc, outputs, _metadata, _calls = _collect(env={"CHECK_TRIAGE_HEAD_SHA": FAILING_SHA.upper()})
		self.assertNotIn("reason=head_advanced", proc.stdout)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)

	def test_current_head_is_triaged_as_before(self) -> None:
		proc, outputs, _metadata, _calls = _collect()
		self.assertNotIn("reason=head_advanced", proc.stdout)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)

	def test_switch_off_triages_an_old_head(self) -> None:
		for value in ("false", "FALSE", "off"):
			with self.subTest(value=value):
				proc, outputs, _metadata, _calls = _collect(
					pr_head_sha=NEW_HEAD, env={"CHECK_TRIAGE_SKIP_HEAD_ADVANCED_ENABLED": value},
				)
				self.assertNotIn("reason=head_advanced", proc.stdout)
				self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
				self.assertIn("pr_head_advanced=true", proc.stdout)

	def test_unknown_failing_sha_is_triaged(self) -> None:
		for sha in ("", "not-a-sha"):
			with self.subTest(sha=sha):
				proc, _outputs, _metadata, _calls = _collect(pr_head_sha=NEW_HEAD, env={"CHECK_TRIAGE_HEAD_SHA": sha})
				self.assertNotIn("reason=head_advanced", proc.stdout)


class FinalCiOwnedGateTests(unittest.TestCase):
	def test_failed_final_ci_run_is_left_to_the_hand_back(self) -> None:
		for name in ("CI [pr:17]", "CI [pr:17] [sync-quick]"):
			with self.subTest(name=name):
				proc, outputs, metadata, calls = _collect(check_name=name)
				self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
				self.assertIn("CHECK_TRIAGE skip reason=final_ci_repair_owns pr=17 check=" + name, proc.stdout)
				self.assertNotIn("ready", outputs)
				self.assertEqual(metadata, {})
				self.assertFalse([call for call in calls if "/actions/" in call or "/issues" in call], calls)

	def test_hand_back_switch_values(self) -> None:
		for value, skipped in (("", True), ("true", True), ("TRUE", True), ("false", False), ("nope", False)):
			with self.subTest(value=value):
				proc, outputs, _metadata, _calls = _collect(
					check_name="CI [pr:17]", env={"FINAL_CI_FAILED_REDISPATCH_ENABLED": value},
				)
				self.assertEqual("reason=final_ci_repair_owns" in proc.stdout, skipped, proc.stdout)
				if not skipped:
					self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)

	def test_switch_off_triages_final_ci_runs(self) -> None:
		proc, outputs, _metadata, _calls = _collect(
			check_name="CI [pr:17]", env={"CHECK_TRIAGE_SKIP_FINAL_CI_ENABLED": "false"},
		)
		self.assertNotIn("reason=final_ci_repair_owns", proc.stdout)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)

	def test_other_names_are_triaged(self) -> None:
		for name in ("CI", "CI / lint", "CI [pr:18]", "CI [pr:17] extra", "XCI [pr:17]", "CI [pr:017]", "CI [pr:17] [Sync Quick]"):
			with self.subTest(name=name):
				proc, outputs, _metadata, _calls = _collect(check_name=name)
				self.assertNotIn("reason=final_ci_repair_owns", proc.stdout)
				self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)

	def test_stale_head_is_reported_first(self) -> None:
		proc, _outputs, _metadata, _calls = _collect(pr_head_sha=NEW_HEAD, check_name="CI [pr:17]")
		self.assertIn("reason=head_advanced", proc.stdout)
		self.assertNotIn("reason=final_ci_repair_owns", proc.stdout)


class WiringTests(unittest.TestCase):
	def test_reusable_workflow_passes_the_switches(self) -> None:
		text = (REPO_ROOT / ".github" / "workflows" / "check_failure_triage.yml").read_text(encoding="utf-8")
		for line in (
			"CHECK_TRIAGE_SKIP_HEAD_ADVANCED_ENABLED: ${{ vars.CHECK_TRIAGE_SKIP_HEAD_ADVANCED_ENABLED || 'true' }}",
			"CHECK_TRIAGE_SKIP_FINAL_CI_ENABLED: ${{ vars.CHECK_TRIAGE_SKIP_FINAL_CI_ENABLED || 'true' }}",
			"FINAL_CI_FAILED_REDISPATCH_ENABLED: ${{ vars.FINAL_CI_FAILED_REDISPATCH_ENABLED || 'true' }}",
		):
			self.assertIn(line, text)

	def test_gates_run_before_any_base_or_issue_read(self) -> None:
		script = (REPO_ROOT / "scripts" / "check_failure_triage.sh").read_text(encoding="utf-8")
		gate = script.index("skip reason=final_ci_repair_owns")
		self.assertLess(script.index("skip reason=head_advanced"), gate)
		self.assertLess(gate, script.index("triage_release_download_rerun_gate\n"))
		self.assertLess(gate, script.index("# --- Base-branch gate (#7020)"))


if __name__ == "__main__":
	unittest.main()

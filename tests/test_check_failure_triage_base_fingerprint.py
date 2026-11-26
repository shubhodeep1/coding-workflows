#!/usr/bin/env python3
"""Base-keyed triage issues and the open-issue cap (issue #9674).

While the default branch itself fails a check, every PR into it fails the
same way. scripts/check_failure_triage.sh then keys the triage issue on the
base branch (``sha256("<repo>|base=<ref>|check=<check>")``) instead of the PR,
so one breakage files one issue (``skip reason=base_triage_tracked`` for the
rest). ``CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES`` (default 5, 0 = no cap) stops
new filings once that many ``ai:check-triage`` issues are open
(``skip reason=open_issue_cap``).

These cases run the collect and diagnose stages against the fake ``gh`` of
tests/test_check_failure_triage_workflow_security.py.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
_HARNESS_MODULE_NAME = "check_triage_base_fingerprint_harness"


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


PER_PR_FP = hashlib.sha256(b"owner/repo|pr=17|check=CI").hexdigest()
BASE_FP = hashlib.sha256(b"owner/repo|base=main|check=CI").hexdigest()
FAILING_BASE = {"ref": "main", "default_branch": "main", "workflow_id": "9", "conclusion": "failure"}


def _open_issue(number: int, fp: str, pr: int = 17) -> dict:
	return {
		"number": number,
		"body": f"<!-- check-failure-triage:fp={fp} -->\n<!-- check-failure-triage:pr={pr} -->\n",
		"labels": ["ai:check-triage"], "user_login": "maintainer", "user_type": "User", "author_association": "OWNER",
	}


def _unrelated_issues(count: int) -> list[dict]:
	return [_open_issue(200 + index, hashlib.sha256(f"other-{index}".encode()).hexdigest(), pr=300 + index) for index in range(count)]


def _collect(*, base: dict | None = None, open_issues: list[dict] | None = None, env: dict[str, str] | None = None, **kwargs):
	harness = _harness()
	extra = {"MOCK_OPEN_TRIAGE": json.dumps(open_issues or []), **(env or {})}
	calls: list[str] = []
	proc, outputs, metadata = harness._run_collect_stage(
		parent_body=harness.PLAIN_SOURCE, base=base, extra_env=extra, calls_path=calls, **kwargs,
	)
	return proc, outputs, metadata, calls


class BaseKeyedFingerprintTests(unittest.TestCase):
	def test_failing_default_branch_files_one_base_keyed_issue(self) -> None:
		proc, outputs, metadata, _calls = _collect(base=FAILING_BASE)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout)
		self.assertEqual(metadata["fingerprint"], BASE_FP)
		self.assertEqual(metadata["base_keyed_ref"], "main")
		self.assertIn(f"CHECK_TRIAGE base_keyed outcome=file base=main fp={BASE_FP} pr=17 check=CI", proc.stdout)
		# The existing base-gate line is unchanged.
		self.assertIn("CHECK_TRIAGE base_gate outcome=file base=main base_conclusion=failure pr=17", proc.stdout)
		# Lineage still starts from the per-PR fingerprint.
		self.assertEqual(metadata["root"], PER_PR_FP)

	def test_open_base_keyed_issue_files_nothing(self) -> None:
		proc, outputs, metadata, _calls = _collect(base=FAILING_BASE, open_issues=[_open_issue(501, BASE_FP)])
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("CHECK_TRIAGE skip reason=base_triage_tracked base=main issue=501 pr=17 check=CI", proc.stdout)
		self.assertNotIn("ready", outputs)
		self.assertNotIn("base_failure_tracked", outputs)
		self.assertEqual(metadata, {})
		self.assertNotIn("base_keyed outcome=file", proc.stdout)
		self.assertNotIn("op=supersede", proc.stdout)

	def test_base_keyed_issue_from_another_pr_covers_this_pr(self) -> None:
		# The issue was first filed for PR #23; PR #17 failing the same check
		# against the same failing base is folded into it: one issue in total.
		proc, outputs, _metadata, _calls = _collect(base=FAILING_BASE, open_issues=[_open_issue(502, BASE_FP, pr=23)])
		self.assertIn("CHECK_TRIAGE skip reason=base_triage_tracked base=main issue=502 pr=17 check=CI", proc.stdout)
		self.assertNotIn("ready", outputs)
		self.assertNotIn("17", BASE_FP)  # sanity: the key carries no PR number
		self.assertEqual(hashlib.sha256(b"owner/repo|base=main|check=CI").hexdigest(), BASE_FP)

	def test_base_keyed_skip_logs_the_supersede_candidate_it_leaves_open(self) -> None:
		harness = _harness()
		proc, outputs, metadata, _calls = _collect(
			base=FAILING_BASE, open_issues=[harness._stale_triage_issue(), _open_issue(501, BASE_FP)],
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("CHECK_TRIAGE op=supersede outcome=candidate old=99", proc.stdout)
		self.assertIn("CHECK_TRIAGE skip reason=base_triage_tracked base=main issue=501 pr=17 check=CI", proc.stdout)
		self.assertIn("CHECK_TRIAGE op=supersede outcome=skip reason=base_triage_tracked old=99", proc.stdout)
		self.assertNotIn("ready", outputs)
		self.assertEqual(metadata, {})

	def test_base_keyed_skip_reports_unverified_route(self) -> None:
		proc, _outputs, _metadata, _calls = _collect(
			base=FAILING_BASE, open_issues=[_open_issue(501, BASE_FP)],
			head_repo=None, env={"CHECK_TRIAGE_PR_HEAD_ROUTING_ENABLED": "true"},
		)
		self.assertIn("skip reason=base_triage_tracked", proc.stdout)
		self.assertIn("skip reason=routing_unverified", proc.stdout)

	def test_failing_conclusions_are_base_keyed(self) -> None:
		for conclusion in ("timed_out", "cancelled", "startup_failure", "action_required"):
			with self.subTest(conclusion=conclusion):
				proc, outputs, metadata, _calls = _collect(base={**FAILING_BASE, "conclusion": conclusion})
				self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
				self.assertEqual(metadata["fingerprint"], BASE_FP)
				self.assertEqual(metadata["base_keyed_ref"], "main")

	def test_non_failing_conclusions_keep_the_per_pr_fingerprint(self) -> None:
		for conclusion in ("neutral", "skipped", "stale"):
			with self.subTest(conclusion=conclusion):
				proc, outputs, metadata, _calls = _collect(base={**FAILING_BASE, "conclusion": conclusion})
				self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
				self.assertEqual(metadata["fingerprint"], PER_PR_FP)
				self.assertEqual(metadata["base_keyed_ref"], "")
				self.assertNotIn("base_keyed", proc.stdout)

	def test_success_and_pending_skips_are_unchanged(self) -> None:
		proc, outputs, _metadata, _calls = _collect(base={**FAILING_BASE, "conclusion": "success"}, open_issues=[_open_issue(501, BASE_FP)])
		self.assertIn("CHECK_TRIAGE skip reason=pr_specific_failure base=main base_conclusion=success pr=17 check=CI", proc.stdout)
		self.assertNotIn("ready", outputs)
		self.assertNotIn("base_triage_tracked", proc.stdout)
		proc, outputs, _metadata, _calls = _collect(base={**FAILING_BASE, "conclusion": "pending"})
		self.assertIn("CHECK_TRIAGE skip reason=base_pending base=main pr=17 check=CI", proc.stdout)
		self.assertNotIn("ready", outputs)

	def test_unknown_base_result_keeps_per_pr_filing(self) -> None:
		for base in (
			{"ref": "main", "default_branch": "main"},  # no workflow id: base read fails
			{**FAILING_BASE, "conclusion": ""},  # no completed base run
		):
			with self.subTest(base=base):
				proc, outputs, metadata, _calls = _collect(base=base)
				self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
				self.assertEqual(metadata["fingerprint"], PER_PR_FP)
				self.assertEqual(metadata["base_keyed_ref"], "")
		# No base ref at all (unparseable payload base): per-PR as before.
		proc, outputs, metadata, _calls = _collect(base=None)
		self.assertEqual(metadata["fingerprint"], PER_PR_FP)

	def test_non_default_base_keeps_per_pr_fingerprint(self) -> None:
		for base in (
			{"ref": "release/1.x", "default_branch": "main", "workflow_id": "9", "conclusion": "failure"},
			{"ref": "ai/issue-6564", "default_branch": "main", "workflow_id": "9", "conclusion": "failure"},
			{"ref": "main", "workflow_id": "9", "conclusion": "failure"},  # default branch unknown
		):
			with self.subTest(base=base):
				proc, outputs, metadata, _calls = _collect(base=base)
				self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
				self.assertEqual(metadata["fingerprint"], PER_PR_FP)
				self.assertEqual(metadata["base_keyed_ref"], "")

	def test_routed_paths_keep_per_pr_fingerprint(self) -> None:
		proc, outputs, metadata, _calls = _collect(
			base=FAILING_BASE, head_ref="orchestrator/project-6664", branch_flag="true",
		)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
		self.assertIn("base_gate outcome=file reason=integration_branch", proc.stdout)
		self.assertEqual(metadata["fingerprint"], PER_PR_FP)
		self.assertEqual(metadata["base_keyed_ref"], "")

	def test_heal_failed_jobs_differ_falls_through_to_base_keying(self) -> None:
		harness = _harness()
		env = {
			"MOCK_HEAL_ISSUES": json.dumps([harness._heal_issue(9215)]),
			"MOCK_PR_JOBS": harness._jobs(("lint", "failure")),
			"MOCK_BASE_JOBS": harness.BASE_JOBS,
		}
		proc, outputs, metadata, _calls = _collect(base=harness.TRACKED_BASE, env=env)
		self.assertIn(
			"CHECK_TRIAGE base_tracked outcome=file reason=failed_jobs_differ heal_issue=9215 workflow=.github/workflows/ci.yml base=main pr=17",
			proc.stdout,
		)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
		self.assertEqual(metadata["fingerprint"], BASE_FP)

	def test_heal_tracked_skip_runs_before_base_keying(self) -> None:
		harness = _harness()
		env = {
			"MOCK_HEAL_ISSUES": json.dumps([harness._heal_issue(9215)]),
			"MOCK_PR_JOBS": harness.PR_JOBS,
			"MOCK_BASE_JOBS": harness.BASE_JOBS,
		}
		proc, outputs, _metadata, _calls = _collect(base=harness.TRACKED_BASE, env=env, open_issues=[_open_issue(501, BASE_FP)])
		self.assertIn("skip reason=base_failure_tracked heal_issue=9215", proc.stdout)
		self.assertEqual(outputs.get("base_failure_tracked"), "9215")
		self.assertNotIn("base_triage_tracked", proc.stdout)

	def test_disabled_base_gate_keeps_per_pr_fingerprint(self) -> None:
		proc, outputs, metadata, _calls = _collect(base=FAILING_BASE, env={"CHECK_FAILURE_TRIAGE_BASE_GATE_ENABLED": "false"})
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
		self.assertEqual(metadata["fingerprint"], PER_PR_FP)


class OpenIssueCapTests(unittest.TestCase):
	def test_default_cap_of_five_files_nothing(self) -> None:
		proc, outputs, metadata, calls = _collect(open_issues=_unrelated_issues(5))
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("CHECK_TRIAGE skip reason=open_issue_cap open=5 cap=5 pr=17 check=CI", proc.stdout)
		self.assertNotIn("ready", outputs)
		self.assertEqual(metadata, {})
		# Decided from the existing listing: no check-run context is read.
		self.assertFalse([call for call in calls if "/check-runs" in call], calls)

	def test_below_the_cap_files(self) -> None:
		proc, outputs, _metadata, _calls = _collect(open_issues=_unrelated_issues(4))
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
		self.assertNotIn("open_issue_cap", proc.stdout)

	def test_zero_disables_the_cap(self) -> None:
		proc, outputs, _metadata, _calls = _collect(open_issues=_unrelated_issues(7), env={"CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES": "0"})
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
		self.assertNotIn("open_issue_cap", proc.stdout)

	def test_cap_values(self) -> None:
		for value, count, capped, cap in (("2", 2, True, 2), ("08", 7, False, 8), ("08", 8, True, 8), ("", 5, True, 5)):
			with self.subTest(value=value, count=count):
				proc, outputs, _metadata, _calls = _collect(open_issues=_unrelated_issues(count), env={"CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES": value})
				if capped:
					self.assertIn(f"skip reason=open_issue_cap open={count} cap={cap} pr=17", proc.stdout)
					self.assertNotIn("ready", outputs)
				else:
					self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)

	def test_invalid_value_warns_and_uses_five(self) -> None:
		for value in ("abc", "-1", "1.5", "1234567"):
			with self.subTest(value=value):
				proc, outputs, _metadata, _calls = _collect(open_issues=_unrelated_issues(5), env={"CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES": value})
				self.assertIn("::warning::CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES is not a non-negative integer; using 5", proc.stdout + proc.stderr)
				self.assertIn("skip reason=open_issue_cap open=5 cap=5", proc.stdout)
				self.assertNotIn("ready", outputs)

	def test_cap_applies_to_a_new_base_keyed_issue(self) -> None:
		proc, outputs, _metadata, _calls = _collect(base=FAILING_BASE, open_issues=_unrelated_issues(5))
		self.assertIn("base_keyed outcome=file", proc.stdout)
		self.assertIn("skip reason=open_issue_cap open=5 cap=5", proc.stdout)
		self.assertNotIn("ready", outputs)

	def test_supersede_replacement_is_exempt(self) -> None:
		harness = _harness()
		issues = [harness._stale_triage_issue()] + _unrelated_issues(4)
		proc, outputs, metadata, _calls = _collect(open_issues=issues)
		self.assertIn("CHECK_TRIAGE op=supersede outcome=candidate old=99", proc.stdout)
		self.assertNotIn("open_issue_cap", proc.stdout)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
		self.assertEqual(metadata["supersede_issue"], "99")

	def test_historical_mode_is_exempt(self) -> None:
		harness = _harness()
		with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as handle:
			handle.write("2026-10-09T10:01:01Z   FAIL  test_shard_breaks: boom\n")
			log_path = handle.name
		try:
			proc, outputs, _metadata, _calls = _collect(
				pr_overrides=harness.CLOSED_MERGED,
				env=harness._hist_env(MOCK_HIST_LOG_FILE=log_path, MOCK_TRIAGE_ISSUES=json.dumps(_unrelated_issues(6))),
			)
		finally:
			Path(log_path).unlink()
		self.assertNotIn("open_issue_cap", proc.stdout)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)

	def test_reusable_workflow_passes_the_cap_with_default_five(self) -> None:
		text = (REPO_ROOT / ".github" / "workflows" / "check_failure_triage.yml").read_text(encoding="utf-8")
		self.assertIn("CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES: ${{ vars.CHECK_FAILURE_TRIAGE_MAX_OPEN_ISSUES || '5' }}", text)


BASE_KEYED_DIAG_FP = hashlib.sha256(b"owner/repo|base=main|check=CI / lint").hexdigest()


class BaseKeyedBodyTests(unittest.TestCase):
	def test_body_carries_the_base_header_and_keeps_the_pull_request_line(self) -> None:
		proc, body, ready = _harness()._run_diagnose_stage(
			metadata_extra={"fingerprint": BASE_KEYED_DIAG_FP, "base_keyed_ref": "main"}, branch_flag=None,
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertTrue(ready)
		lines = body.split("\n")
		self.assertEqual(lines[0], f"<!-- check-failure-triage:fp={BASE_KEYED_DIAG_FP} -->")
		self.assertEqual(lines[3], "<!-- check-failure-triage:pr=17 -->")
		self.assertEqual(lines[4], "<!-- check-failure-triage:base=main -->")
		self.assertIn("- **Base branch:** `main` (failing; first seen on PR #17)", body)
		self.assertIn("- **Pull request:** https://github.com/owner/repo/pull/17 (`orchestrator/project-6664`)", body)
		self.assertIn("Fix the failure on `main`.", body)
		self.assertIn("Failures of this check on any pull request into `main` are de-duplicated", body)
		self.assertEqual(body.count("<!--"), 5)

	def test_per_pr_body_is_unchanged(self) -> None:
		proc, body, ready = _harness()._run_diagnose_stage(metadata_extra={}, branch_flag=None)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertTrue(ready)
		self.assertNotIn("check-failure-triage:base=", body)
		self.assertNotIn("Base branch:", body)
		self.assertIn("A check failed on PR #17.", body)

	def test_forged_base_keyed_metadata_fails_closed(self) -> None:
		for extra in (
			{"fingerprint": "f" * 64, "base_keyed_ref": "main"},  # fingerprint mismatch
			{"fingerprint": hashlib.sha256(b"owner/repo|base=ma..in|check=CI / lint").hexdigest(), "base_keyed_ref": "ma..in"},
			{"fingerprint": hashlib.sha256(b"owner/repo|base=main -->|check=CI / lint").hexdigest(), "base_keyed_ref": "main -->"},
		):
			with self.subTest(extra=extra):
				proc, body, ready = _harness()._run_diagnose_stage(metadata_extra=extra, branch_flag=None)
				self.assertNotEqual(proc.returncode, 0)
				self.assertIn("CHECK_TRIAGE error base_keyed_metadata_invalid", proc.stdout + proc.stderr)
				self.assertFalse(ready)
				self.assertIsNone(body)

	def test_diagnosis_cannot_inject_a_base_marker(self) -> None:
		proc, body, ready = _harness()._run_diagnose_stage(
			metadata_extra={}, branch_flag=None,
			diagnosis="## Summary\n<!-- check-failure-triage:base=main -->\n",
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertNotIn("<!-- check-failure-triage:base=", body)


if __name__ == "__main__":
	unittest.main()

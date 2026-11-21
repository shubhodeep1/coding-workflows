#!/usr/bin/env python3
"""Release-download re-run gate in scripts/check_failure_triage.sh (issue #9120).

On 2026-11-08 GitHub's release-download servers returned HTTP 504 for about two
hours. Consumer checks that download tools at setup time (Foundry, Slither,
the CodeQL bundle) failed before running any PR code, and one PR got 7 triage
issues and 3 CRITICAL pages. The collect stage now re-runs the failed jobs of a
first-attempt run whose failure annotations only show a github.com release
download 502/503/504, and files no issue. Attempt 2 is triaged as before.

These cases run the collect stage against the fake ``gh`` of
tests/test_check_failure_triage_workflow_security.py. CI collects them through
that file (ci.yml was outside this issue's file grant).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
_HARNESS_MODULE_NAME = "check_triage_release_download_harness"


def _harness():
	"""The security test module, loaded once under a name that skips its own
	collection of this file (no import cycle)."""
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


HEAD = "a" * 40
RUN_URL = "https://github.com/owner/repo/actions/runs/1"
RUN_GET = "api repos/owner/repo/actions/runs/1"
JOBS_GET = "api -X GET repos/owner/repo/actions/runs/1/attempts/1/jobs -f per_page=100"
RERUN_POST = "api -i -X POST repos/owner/repo/actions/runs/1/rerun-failed-jobs"
# Issue #9812: the check_run path re-runs only the classified job.
JOB_RERUN_POST = "api -i -X POST repos/owner/repo/actions/jobs/555/rerun"
PR_JOBS_LATEST_GET = "api repos/owner/repo/actions/runs/1/jobs?filter=latest&per_page=100"
# The base gate reads the run as `--jq [(.workflow_id ...), (.path ...)]`.
BASE_RUN_GET_PREFIX = "api repos/owner/repo/actions/runs/1 --jq "

# Annotation text from the 2026-11-08 failures.
FOUNDRY = [
	{
		"annotation_level": "failure",
		"message": (
			"curl: (22) The requested URL returned error: 504\n"
			"foundryup: command failed: curl -#fL "
			"https://github.com/foundry-rs/foundryup/releases/latest/download/foundryup_linux_amd64"
		),
	}
]
SLITHER = [
	{
		"annotation_level": "failure",
		"message": (
			"HTTP status server error (504 Gateway Time-out) for url "
			"(https://github.com/foundry-rs/foundry/releases/download/v1.9.0/foundry_v1.9.0_linux_amd64.tar.gz)"
		),
	},
	{"annotation_level": "failure", "message": "Slither report was not generated."},
	{"annotation_level": "failure", "message": "Process completed with exit code 1."},
]
CODEQL = [
	{"annotation_level": "failure", "message": "Unable to download and extract CodeQL CLI: Unexpected HTTP response: 504"},
	{
		"annotation_level": "warning",
		"message": (
			"Failed to download and extract the bundle from "
			"https://github.com/github/codeql-action/releases/download/codeql-bundle-v2.26.2/codeql-bundle-linux64.tar.zst. "
			"HTTP status code: 504."
		),
	},
]
NON_RELEASE_504 = [
	{"annotation_level": "failure", "message": "curl: (22) The requested URL returned error: 504 https://registry.npmjs.org/left-pad"}
]
PLAIN_FAILURE = [
	{"annotation_level": "failure", "message": "AssertionError: expected 1, got 2"},
	{"annotation_level": "failure", "message": "Process completed with exit code 1."},
]


def _run_payload(**overrides) -> str:
	payload = {"id": 1, "status": "completed", "conclusion": "failure", "run_attempt": 1, "head_sha": HEAD, "workflow_id": 9}
	payload.update(overrides)
	return json.dumps(payload)


def _jobs_payload(failed_ids: list[int], passed_ids: list[int] | None = None) -> str:
	jobs = [
		{"id": job_id, "name": f"job {job_id}", "status": "completed", "conclusion": "failure", "check_run_url": f"https://api.github.com/repos/owner/repo/check-runs/{job_id}"}
		for job_id in failed_ids
	]
	jobs += [
		{"id": job_id, "name": f"job {job_id}", "status": "completed", "conclusion": "success", "check_run_url": f"https://api.github.com/repos/owner/repo/check-runs/{job_id}"}
		for job_id in passed_ids or []
	]
	return json.dumps({"total_count": len(jobs), "jobs": jobs})


def _collect(
	annotations: dict[int, list[dict]],
	*,
	jobs: str | None = None,
	run: str | None = None,
	switch: str | None = "true",
	extra: dict[str, str] | None = None,
	base: dict | None = None,
):
	with tempfile.TemporaryDirectory(prefix="rd-annotations-") as annotation_dir:
		for check_run_id, items in annotations.items():
			(Path(annotation_dir) / f"{check_run_id}.json").write_text(json.dumps(items), encoding="utf-8")
		env = {
			"CHECK_TRIAGE_DETAILS_URL": RUN_URL,
			"CHECK_TRIAGE_CHECK_RUN_ID": "",
			"MOCK_RD_RUN": _run_payload() if run is None else run,
			"MOCK_RD_JOBS": jobs if jobs is not None else _jobs_payload(sorted(annotations)),
			"MOCK_RD_ANNOTATIONS_DIR": annotation_dir,
		}
		if switch is not None:
			env["CHECK_FAILURE_TRIAGE_RELEASE_DOWNLOAD_RERUN_ENABLED"] = switch
		env.update(extra or {})
		calls: list[str] = []
		proc, outputs, metadata = _harness()._run_collect_stage(extra_env=env, calls_path=calls, base=base)
	return proc, outputs, calls


def _annotation_gets(calls: list[str]) -> list[str]:
	return [call for call in calls if call.startswith("api -X GET repos/owner/repo/check-runs/") and "/annotations" in call]


class ReleaseDownloadRerunGateTests(unittest.TestCase):
	def _assert_rerun(self, proc, outputs, calls, post: str = RERUN_POST) -> None:
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(calls.count(post), 1, calls)
		self.assertIn("RELEASE_DOWNLOAD_RERUN outcome=rerun run=1 ", proc.stdout)
		self.assertIn("CHECK_TRIAGE skip reason=release_download_rerun run=1 pr=17", proc.stdout)
		self.assertNotIn("ready", outputs)
		self.assertNotIn("rerun-secret-body", proc.stdout + proc.stderr)

	def _assert_normal_path(self, proc, outputs, calls, reason: str, *, posted: bool = False) -> None:
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(calls.count(RERUN_POST), 1 if posted else 0, calls)
		self.assertIn(f"RELEASE_DOWNLOAD_RERUN outcome=skip reason={reason}", proc.stdout)
		self.assertNotIn("reason=release_download_rerun", proc.stdout)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)

	# --- workflow_run path (empty check-run id, run URL) ---------------------

	def test_foundry_release_download_504_is_rerun_once(self) -> None:
		proc, outputs, calls = _collect({128679941439: FOUNDRY})
		self._assert_rerun(proc, outputs, calls)
		self.assertIn("job=128679941439 jobs=1 pr=17 check=CI", proc.stdout)
		self.assertEqual(calls.count(RUN_GET), 1)
		self.assertEqual(calls.count(JOBS_GET), 1)
		self.assertEqual(len(_annotation_gets(calls)), 1)

	def test_slither_follow_on_failures_do_not_block_the_rerun(self) -> None:
		self._assert_rerun(*_collect({128679942058: SLITHER}))

	def test_codeql_bundle_download_504_is_rerun(self) -> None:
		self._assert_rerun(*_collect({128685471372: CODEQL}))

	def test_codeql_failure_without_the_release_url_warning_is_filed(self) -> None:
		self._assert_normal_path(*_collect({7: CODEQL[:1]}), "not_release_download")

	def test_codeql_failure_with_a_non_github_host_url_is_filed(self) -> None:
		for url in (
			"https://evilgithub.com/github/codeql-action/releases/download/codeql-bundle-v2.26.2/codeql-bundle-linux64.tar.zst",
			"https://example.com/mirror/github.com/github/codeql-action/releases/download/codeql-bundle-v2.26.2/b.tar.zst",
		):
			with self.subTest(url=url):
				warning = {"annotation_level": "warning", "message": f"Failed to download and extract the bundle from {url}. HTTP status code: 504."}
				self._assert_normal_path(*_collect({7: [CODEQL[0], warning]}), "not_release_download")

	def test_three_matching_failed_jobs_are_rerun_with_one_post(self) -> None:
		proc, outputs, calls = _collect({11: CODEQL, 12: CODEQL, 13: FOUNDRY})
		self._assert_rerun(proc, outputs, calls)
		self.assertIn("jobs=3", proc.stdout)
		self.assertEqual(len(_annotation_gets(calls)), 3)

	def test_second_attempt_is_filed_as_today(self) -> None:
		proc, outputs, calls = _collect({5: FOUNDRY}, run=_run_payload(run_attempt=2))
		self._assert_normal_path(proc, outputs, calls, "not_first_attempt")
		self.assertNotIn(JOBS_GET, calls)

	def test_head_mismatch_is_filed(self) -> None:
		self._assert_normal_path(*_collect({5: FOUNDRY}, run=_run_payload(head_sha="b" * 40)), "head_mismatch")

	def test_run_not_completed_is_filed(self) -> None:
		self._assert_normal_path(*_collect({5: FOUNDRY}, run=_run_payload(status="in_progress")), "run_not_completed")

	def test_unreadable_run_is_filed(self) -> None:
		self._assert_normal_path(*_collect({5: FOUNDRY}, run=""), "run_unreadable")

	def test_504_without_a_release_download_url_is_filed(self) -> None:
		self._assert_normal_path(*_collect({5: NON_RELEASE_504}), "not_release_download")

	def test_plain_test_failure_is_filed(self) -> None:
		self._assert_normal_path(*_collect({5: PLAIN_FAILURE}), "not_release_download")

	def test_one_non_matching_failed_job_files_the_issue(self) -> None:
		self._assert_normal_path(*_collect({5: FOUNDRY, 6: PLAIN_FAILURE}), "not_release_download")

	def test_more_than_three_failed_jobs_are_filed(self) -> None:
		proc, outputs, calls = _collect({1: FOUNDRY, 2: FOUNDRY, 3: FOUNDRY, 4: FOUNDRY})
		self._assert_normal_path(proc, outputs, calls, "too_many_failed_jobs")
		self.assertEqual(_annotation_gets(calls), [])

	def test_no_failed_jobs_is_filed(self) -> None:
		self._assert_normal_path(*_collect({}, jobs=_jobs_payload([], [3])), "no_failed_jobs")

	def test_malformed_jobs_payload_is_filed(self) -> None:
		self._assert_normal_path(*_collect({5: FOUNDRY}, jobs=json.dumps({"total_count": 5, "jobs": []})), "jobs_unreadable")

	def test_unreadable_annotations_are_filed(self) -> None:
		self._assert_normal_path(*_collect({}, jobs=_jobs_payload([5])), "annotations_unreadable")

	# --- check_run path (URL names the job) ----------------------------------

	def test_check_run_path_reruns_without_a_jobs_read(self) -> None:
		proc, outputs, calls = _collect(
			{555: FOUNDRY},
			extra={"CHECK_TRIAGE_CHECK_RUN_ID": "555", "CHECK_TRIAGE_DETAILS_URL": f"{RUN_URL}/job/555"},
		)
		self._assert_rerun(proc, outputs, calls, post=JOB_RERUN_POST)
		# Issue #9812: never rerun-failed-jobs, which would also re-run sibling
		# failed jobs the gate never classified.
		self.assertNotIn(RERUN_POST, calls)
		self.assertNotIn(JOBS_GET, calls)
		self.assertEqual(len(_annotation_gets(calls)), 1)

	def test_check_run_path_failed_job_rerun_files_the_issue(self) -> None:
		proc, outputs, calls = _collect(
			{555: FOUNDRY},
			extra={
				"CHECK_TRIAGE_CHECK_RUN_ID": "555", "CHECK_TRIAGE_DETAILS_URL": f"{RUN_URL}/job/555",
				"MOCK_RD_RERUN_STATUS": "403", "MOCK_RD_RERUN_EXIT": "1",
			},
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(calls.count(JOB_RERUN_POST), 1, calls)
		self.assertNotIn(RERUN_POST, calls)
		self.assertIn("RELEASE_DOWNLOAD_RERUN outcome=skip reason=rerun_failed run=1", proc.stdout)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
		self.assertNotIn("rerun-secret-body", proc.stdout + proc.stderr)

	def test_check_run_path_with_another_job_id_is_unparseable(self) -> None:
		proc, _outputs, calls = _collect(
			{555: FOUNDRY},
			extra={"CHECK_TRIAGE_CHECK_RUN_ID": "555", "CHECK_TRIAGE_DETAILS_URL": f"{RUN_URL}/job/556"},
		)
		self.assertIn("RELEASE_DOWNLOAD_RERUN outcome=skip reason=details_url_unparseable", proc.stdout)
		self.assertNotIn(RUN_GET, calls)
		self.assertNotIn(RERUN_POST, calls)

	def test_unverified_pr_skips_the_gate_before_any_read(self) -> None:
		proc, _outputs, calls = _collect({5: FOUNDRY}, extra={"MOCK_PR_FAIL": "1"})
		self.assertIn("RELEASE_DOWNLOAD_RERUN outcome=skip reason=pr_unverified", proc.stdout)
		self.assertNotIn(RUN_GET, calls)
		self.assertNotIn(RERUN_POST, calls)

	def test_full_page_of_100_jobs_is_not_classified(self) -> None:
		# Issue #9812: bounded like list_run_jobs() in scripts/ci_cancelled_rerun.py
		# (a page of 100 or more may be truncated), so the failure is triaged.
		proc, outputs, calls = _collect({5: FOUNDRY}, jobs=_jobs_payload([5], list(range(1000, 1099))))
		self._assert_normal_path(proc, outputs, calls, "jobs_unreadable")
		self.assertEqual(_annotation_gets(calls), [])

	def test_page_of_99_jobs_is_read(self) -> None:
		proc, outputs, calls = _collect({5: FOUNDRY}, jobs=_jobs_payload([5], list(range(1000, 1098))))
		self._assert_rerun(proc, outputs, calls)

	def test_job_without_a_name_or_id_is_not_classified(self) -> None:
		# Validated like list_run_jobs() in scripts/ci_cancelled_rerun.py, which
		# requires a positive integer id and a string name.
		for drop in ("name", "id"):
			with self.subTest(drop=drop):
				payload = json.loads(_jobs_payload([5]))
				del payload["jobs"][0][drop]
				proc, outputs, calls = _collect({5: FOUNDRY}, jobs=json.dumps(payload))
				self._assert_normal_path(proc, outputs, calls, "jobs_unreadable")
				self.assertEqual(_annotation_gets(calls), [])
		# positive_number() accepts integers only, so a fractional id is rejected too.
		payload = json.loads(_jobs_payload([5]))
		payload["jobs"][0]["id"] = 5.5
		proc, outputs, calls = _collect({5: FOUNDRY}, jobs=json.dumps(payload))
		self._assert_normal_path(proc, outputs, calls, "jobs_unreadable")
		self.assertEqual(_annotation_gets(calls), [])

	def test_full_page_of_100_annotations_is_not_classified(self) -> None:
		# Bounded like failure_annotations() in scripts/ci_cancelled_rerun.py.
		self._assert_normal_path(*_collect({5: FOUNDRY * 100}), "not_release_download")
		self._assert_rerun(*_collect({5: FOUNDRY * 99}))

	def test_unparseable_or_foreign_details_url_is_filed(self) -> None:
		for url in ("https://example.com/not-a-run", "https://github.com/other/repo/actions/runs/1", f"{RUN_URL}/job/9"):
			with self.subTest(url=url):
				proc, outputs, calls = _collect({5: FOUNDRY}, extra={"CHECK_TRIAGE_DETAILS_URL": url})
				self.assertIn("RELEASE_DOWNLOAD_RERUN outcome=skip reason=details_url_unparseable", proc.stdout)
				self.assertNotIn(RUN_GET, calls)
				self.assertNotIn(RERUN_POST, calls)

	# --- switch, POST failure, historical mode, read budget ------------------

	def test_switch_off_makes_no_gate_reads(self) -> None:
		for value in ("false", "FALSE"):
			with self.subTest(value=value):
				proc, outputs, calls = _collect({5: FOUNDRY}, switch=value)
				self._assert_normal_path(proc, outputs, calls, "disabled")
				self.assertNotIn(RUN_GET, calls)
				self.assertEqual(_annotation_gets(calls), [])

	def test_unset_switch_is_on(self) -> None:
		proc, outputs, calls = _collect({5: FOUNDRY}, switch="")
		self._assert_rerun(proc, outputs, calls)

	def test_unknown_switch_value_warns_and_stays_on(self) -> None:
		proc, outputs, calls = _collect({5: FOUNDRY}, switch="maybe")
		self.assertIn("::warning::CHECK_FAILURE_TRIAGE_RELEASE_DOWNLOAD_RERUN_ENABLED is not true or false", proc.stdout)
		self._assert_rerun(proc, outputs, calls)

	def test_failed_post_files_the_issue_without_printing_the_body(self) -> None:
		for status, exit_code in (("403", "1"), ("200", "0")):
			with self.subTest(status=status):
				proc, outputs, calls = _collect({5: FOUNDRY}, extra={"MOCK_RD_RERUN_STATUS": status, "MOCK_RD_RERUN_EXIT": exit_code})
				self._assert_normal_path(proc, outputs, calls, "rerun_failed", posted=True)
				self.assertNotIn("rerun-secret-body", proc.stdout + proc.stderr)

	def test_historical_mode_skips_the_gate(self) -> None:
		proc, _outputs, calls = _collect(
			{5: FOUNDRY},
			extra={"CHECK_TRIAGE_HISTORICAL_RUN_ID": "37", "CHECK_TRIAGE_HISTORICAL_JOB_ID": "113"},
		)
		self.assertIn("RELEASE_DOWNLOAD_RERUN outcome=skip reason=historical_mode", proc.stdout)
		self.assertNotIn(RUN_GET, calls)
		self.assertNotIn(JOBS_GET, calls)
		self.assertNotIn(RERUN_POST, calls)

	def test_base_gate_reuses_the_gate_run_read(self) -> None:
		# No base workflow_id mock: a second run GET by the base gate would fail.
		proc, _outputs, calls = _collect({5: PLAIN_FAILURE}, base={"ref": "main", "conclusion": "failure"})
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(calls.count(RUN_GET), 1, calls)
		self.assertFalse([call for call in calls if call.startswith(BASE_RUN_GET_PREFIX)], calls)
		self.assertTrue([call for call in calls if call.startswith("api repos/owner/repo/actions/workflows/9/runs?branch=main")], calls)
		self.assertLessEqual(len(_annotation_gets(calls)), 3)
		self.assertNotIn(RERUN_POST, calls)

	def _tracked_collect(self, jobs: str, *, switch: str = "true"):
		harness = _harness()
		base = {"ref": "main", "default_branch": "main", "conclusion": "failure", "base_run_id": "2"}
		if switch == "false":
			base.update({"workflow_id": "9", "workflow_path": ".github/workflows/ci.yml"})
		return _collect(
			{5: PLAIN_FAILURE},
			jobs=jobs,
			run=_run_payload(path=".github/workflows/ci.yml"),
			switch=switch,
			base=base,
			extra={
				"MOCK_HEAL_ISSUES": json.dumps([harness._heal_issue(9215)]),
				"MOCK_PR_JOBS": harness._jobs(("lint", "success"), ("tests (ubuntu)", "failure")),
				"MOCK_BASE_JOBS": harness._jobs(("tests (ubuntu)", "failure")),
			},
		)

	@staticmethod
	def _named_jobs(*jobs: tuple[int, str, str], total: int | None = None) -> str:
		return json.dumps({
			"total_count": len(jobs) if total is None else total,
			"jobs": [
				{"id": job_id, "name": name, "status": "completed", "conclusion": conclusion,
					"check_run_url": f"https://api.github.com/repos/owner/repo/check-runs/{job_id}"}
				for job_id, name, conclusion in jobs
			],
		})

	def test_base_tracked_check_reuses_the_gate_jobs_read(self) -> None:
		# Issue #9812: the PR run's jobs are read once per triage invocation.
		jobs = self._named_jobs((4, "lint", "success"), (5, "tests (ubuntu)", "failure"), (6, "e2e", "cancelled"))
		proc, outputs, calls = self._tracked_collect(jobs)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("RELEASE_DOWNLOAD_RERUN outcome=skip reason=not_release_download", proc.stdout)
		self.assertIn("skip reason=base_failure_tracked heal_issue=9215", proc.stdout)
		self.assertEqual(outputs.get("base_failure_tracked"), "9215")
		self.assertEqual(calls.count(JOBS_GET), 1, calls)
		self.assertNotIn(PR_JOBS_LATEST_GET, calls)
		self.assertEqual(sum("/actions/runs/1/" in call and "/jobs" in call for call in calls), 1, calls)
		self.assertIn("api repos/owner/repo/actions/runs/2/jobs?filter=latest&per_page=100", calls)

	def test_base_tracked_check_reads_the_jobs_when_the_gate_has_none(self) -> None:
		# A rejected gate payload is not reused; the check reads the jobs itself.
		jobs = self._named_jobs((5, "tests (ubuntu)", "failure"), total=100)
		proc, _outputs, calls = self._tracked_collect(jobs)
		self.assertIn("RELEASE_DOWNLOAD_RERUN outcome=skip reason=jobs_unreadable", proc.stdout)
		self.assertEqual(calls.count(PR_JOBS_LATEST_GET), 1, calls)
		self.assertIn("skip reason=base_failure_tracked heal_issue=9215", proc.stdout)
		# Gate off: no gate read, so the check reads the jobs as before.
		proc, _outputs, calls = self._tracked_collect(jobs, switch="false")
		self.assertNotIn(JOBS_GET, calls)
		self.assertEqual(calls.count(PR_JOBS_LATEST_GET), 1, calls)
		self.assertIn("skip reason=base_failure_tracked heal_issue=9215", proc.stdout)

	def test_base_gate_reads_the_run_itself_when_the_gate_is_off(self) -> None:
		proc, _outputs, calls = _collect(
			{5: PLAIN_FAILURE}, switch="false", base={"ref": "main", "workflow_id": "9", "conclusion": "failure"}
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertNotIn(RUN_GET, calls)
		self.assertEqual(len([call for call in calls if call.startswith(BASE_RUN_GET_PREFIX)]), 1, calls)


class ReleaseDownloadRerunWiringTests(unittest.TestCase):
	def test_workflow_passes_the_switch_with_a_true_default(self) -> None:
		expected = "${{ vars.CHECK_FAILURE_TRIAGE_RELEASE_DOWNLOAD_RERUN_ENABLED || 'true' }}"
		try:
			job = _harness()._workflow()["jobs"]["triage"]
		except ModuleNotFoundError as exc:
			if exc.name != "yaml":
				raise
			# No PyYAML (the pre-push gate's container): read the triage job's
			# job-level env block as text instead.
			workflow_path = TESTS_DIR.parent / ".github" / "workflows" / "check_failure_triage.yml"
			lines = workflow_path.read_text(encoding="utf-8").splitlines()
			job_start = lines.index("  triage:")
			job_end = next((i for i in range(job_start + 1, len(lines)) if lines[i].startswith("  ") and not lines[i].startswith("   ") and lines[i].strip()), len(lines))
			env_start = lines.index("    env:", job_start, job_end)
			env: dict[str, str] = {}
			for line in lines[env_start + 1 : job_end]:
				if line.strip() and not line.startswith("      "):
					break
				if line.startswith("      ") and not line.startswith("       ") and not line.lstrip().startswith("#") and ": " in line:
					key, value = line.strip().split(": ", 1)
					env[key] = value
			job = {"env": env}
		self.assertEqual(job["env"]["CHECK_FAILURE_TRIAGE_RELEASE_DOWNLOAD_RERUN_ENABLED"], expected)

	def test_gate_runs_after_the_fork_guard_and_before_dedup_and_the_base_gate(self) -> None:
		script = (TESTS_DIR.parent / "scripts" / "check_failure_triage.sh").read_text(encoding="utf-8")
		call = script.index("\ntriage_release_download_rerun_gate\n")
		self.assertLess(script.index('log "skip reason=fork_pr '), call)
		self.assertLess(call, script.index("base_gate_conclusion=\"\""))
		# The re-run POST is a write: never wrapped in gh_retry.
		self.assertIn('gh api -i -X POST "repos/${REPO}/actions/runs/${rd_run}/rerun-failed-jobs"', script)
		self.assertIn('gh api -i -X POST "repos/${REPO}/actions/jobs/${CHECK_RUN_ID}/rerun"', script)
		self.assertNotIn("gh_retry gh api -i -X POST", script)


if __name__ == "__main__":
	unittest.main()

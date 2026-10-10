#!/usr/bin/env python3
"""Contract tests for the single-use historical check-failure triage (#7093).

`.github/workflows/check-failure-triage-historical.yml` triages PR #6611's
failed CI run 37922279826 / job 113792812346 once, after the PR closed. These
tests pin its trigger, idempotency guard, credential-free reproduction job and
the call into the reusable triage workflow, and run the inline ci.yml
extractor against fixture workflows.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "check-failure-triage-historical.yml"
TRIAGE_SCRIPT_PATH = REPO_ROOT / "scripts" / "check_failure_triage.sh"
EXTRACT_STEP_NAME = "Extract orchestrate-poll CI steps from the tested tree"


def _workflow() -> dict:
	return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _step(job: dict, name: str) -> dict:
	for candidate in job["steps"]:
		if candidate.get("name") == name:
			return candidate
	raise AssertionError(f"step not found: {name}")


def _extractor_source() -> str:
	body = _step(_workflow()["jobs"]["repro"], EXTRACT_STEP_NAME)["run"]
	match = re.search(r"<<'PY'\n(.*?)\nPY\n", body, re.DOTALL)
	assert match, "extractor heredoc not found"
	return match.group(1)


FIXTURE_CI = """
jobs:
  orchestrate-poll:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v5
      - name: Install Python CI dependencies
        run: pip install pyyaml
      - name: Derive orchestrate poll test subsets
        run: |
          echo derive
      - name: Orchestrate poll implementation-failed regression fast-fail
        if: strategy.job-index == 0
        run: |
          echo fastfail
      - name: Orchestrate poll process unit tests
        env:
          CI_POLL_TEST_GROUP_INDEX: ${{ strategy.job-index }}
        run: |
          python3 tests/test_ci_poll_test_sharding.py
          python3 tests/test_orchestrate_poll_noop_suspicious_recovery.py
          python3 tests/test_state_snapshot.py
          python3 tests/test_run_substate_ledger.py
"""


class HistoricalTriageWorkflowTests(unittest.TestCase):
	def test_triggers_constants_and_concurrency(self) -> None:
		workflow = _workflow()
		self.assertEqual(set(workflow["on"]), {"push", "workflow_dispatch"})
		self.assertEqual(workflow["on"]["push"], {"branches": ["main"]})
		self.assertEqual(workflow["env"]["HIST_PR"], "6611")
		self.assertEqual(workflow["env"]["HIST_RUN_ID"], "37922279826")
		self.assertEqual(workflow["env"]["HIST_JOB_ID"], "113792812346")
		self.assertEqual(workflow["env"]["HIST_CHECK_NAME"], "CI")
		self.assertEqual(workflow["concurrency"]["group"], "check-triage-historical-37922279826")
		self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
		self.assertEqual(workflow["permissions"], {})

	def test_guard_fingerprint_matches_script_historical_formula(self) -> None:
		guard = _workflow()["jobs"]["guard"]["steps"][0]["run"]
		script = TRIAGE_SCRIPT_PATH.read_text(encoding="utf-8")
		self.assertIn('"${REPO}|pr=${HIST_PR}|check=${HIST_CHECK_NAME}|run=${HIST_RUN_ID}|job=${HIST_JOB_ID}"', guard)
		self.assertIn('"${REPO}|pr=${PR_NUMBER}|check=${CHECK_NAME}|run=${HIST_RUN_ID}|job=${HIST_JOB_ID}"', script)
		# Trusted authors only; open and closed issues both count.
		self.assertIn("-f state=all -f labels=ai:check-triage", guard)
		self.assertIn('$a == "OWNER" or $a == "MEMBER" or $a == "COLLABORATOR"', guard)
		self.assertIn("reason=already_filed", guard)
		self.assertIn("reason=failed_run_cap", guard)
		self.assertIn("HTTP (404|410)", guard)

	def test_repro_job_is_credential_free_and_asserts_dependencies_first(self) -> None:
		repro = _workflow()["jobs"]["repro"]
		self.assertEqual(repro["permissions"], {"contents": "read"})
		self.assertNotIn("secrets.", yaml.safe_dump(repro))
		self.assertEqual(repro["strategy"]["matrix"]["group"], [0, 1, 2, 3])
		self.assertFalse(repro["strategy"]["fail-fast"])
		checkouts = [step for step in repro["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
		self.assertEqual(len(checkouts), 2)
		for step in checkouts:
			self.assertFalse(step["with"]["persist-credentials"])
		names = [step.get("name") for step in repro["steps"]]
		assert_index = names.index("Assert jq and PyYAML are available")
		self.assertLess(assert_index, names.index(EXTRACT_STEP_NAME))
		self.assertLess(assert_index, names.index("Run the extracted CI steps"))
		assert_body = repro["steps"][assert_index]["run"]
		self.assertIn("jq --version", assert_body)
		self.assertIn("python3 -c 'import yaml'", assert_body)
		upload = _step(repro, "Upload reproduction results")
		self.assertEqual(upload["with"]["name"], "historical-repro-group-${{ matrix.group }}")
		# GitHub expands workflow expressions inside run bodies.
		for step in repro["steps"]:
			self.assertNotIn("${{", step.get("run", ""), step.get("name"))

	def test_triage_job_calls_reusable_workflow_in_historical_mode(self) -> None:
		triage = _workflow()["jobs"]["triage"]
		self.assertEqual(triage["uses"], "./.github/workflows/check_failure_triage.yml")
		self.assertIn("!cancelled()", triage["if"])
		self.assertEqual(triage["needs"], ["guard", "repro"])
		self.assertEqual(set(triage["secrets"]), {"GH_PAT", "CHECK_TRIAGE_ISSUES_TOKEN", "OPENROUTER_API_KEY", "TG_BOT_SECRET"})
		inputs = triage["with"]
		self.assertEqual(inputs["pr_number"], "6611")
		self.assertEqual(inputs["check_name"], "CI")
		self.assertEqual(inputs["historical_run_id"], "37922279826")
		self.assertEqual(inputs["historical_job_id"], "113792812346")
		self.assertIn("historical-repro-", inputs["historical_repro_artifact_prefix"])

	def test_registered_for_removal(self) -> None:
		registry = (REPO_ROOT / "docs" / "scripts-pending-removal.md").read_text(encoding="utf-8")
		self.assertIn("### `.github/workflows/check-failure-triage-historical.yml`", registry)
		self.assertIn("gh issue list --label ai:check-triage --search 37922279826 --state all", registry)


class HistoricalExtractorTests(unittest.TestCase):
	def _extract(self, ci_text: str, group: int) -> tuple[Path, Path]:
		workdir = Path(tempfile.mkdtemp(prefix="hist-extract-"))
		self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(workdir)], check=False))
		(workdir / ".github" / "workflows").mkdir(parents=True)
		(workdir / ".github" / "workflows" / "ci.yml").write_text(textwrap.dedent(ci_text), encoding="utf-8")
		out_dir, script_dir = workdir / "out", workdir / "plan"
		proc = subprocess.run(
			[sys.executable, "-", str(group), str(out_dir), str(script_dir)], input=_extractor_source(),
			cwd=workdir, capture_output=True, text=True, encoding="utf-8",
		)
		self.assertEqual(proc.returncode, 0, proc.stderr)
		return out_dir, script_dir

	def test_group_zero_keeps_fast_fail_and_flags_coverage(self) -> None:
		out_dir, script_dir = self._extract(FIXTURE_CI, 0)
		plan = [line.split("\t") for line in (out_dir / "plan.tsv").read_text(encoding="utf-8").splitlines()]
		self.assertEqual([row[1] for row in plan], ["derive", "fastfail", "unit"])
		self.assertEqual(plan[2][2], "sharding,single")
		self.assertEqual((script_dir / f"step-{plan[2][0]}.sh").read_text(encoding="utf-8").splitlines()[0], "python3 tests/test_ci_poll_test_sharding.py")
		self.assertIn("CI_POLL_TEST_GROUP_INDEX=0", (script_dir / "env.sh").read_text(encoding="utf-8"))
		self.assertFalse((out_dir / "extract_failed").exists())

	def test_other_groups_drop_group_zero_steps(self) -> None:
		out_dir, script_dir = self._extract(FIXTURE_CI, 2)
		kinds = [line.split("\t")[1] for line in (out_dir / "plan.tsv").read_text(encoding="utf-8").splitlines()]
		self.assertEqual(kinds, ["derive", "unit"])
		self.assertIn("CI_POLL_TEST_GROUP_INDEX=2", (script_dir / "env.sh").read_text(encoding="utf-8"))

	def test_unknown_if_and_expressions_fail_closed(self) -> None:
		cases = {
			"unsupported_if": FIXTURE_CI.replace("if: strategy.job-index == 0", "if: github.event_name == 'push'"),
			"expression_in_body": FIXTURE_CI.replace("echo derive", "echo ${{ github.sha }}"),
			"required_step_missing": FIXTURE_CI.replace("Orchestrate poll process unit tests", "Renamed unit tests"),
			"install_step_missing": FIXTURE_CI.replace("Install Python CI dependencies", "Install things"),
		}
		for reason, ci_text in cases.items():
			with self.subTest(reason=reason):
				out_dir, _ = self._extract(ci_text, 0)
				self.assertEqual((out_dir / "extract_failed").read_text(encoding="utf-8").strip(), reason)
				self.assertFalse((out_dir / "plan.tsv").exists())


if __name__ == "__main__":
	unittest.main()

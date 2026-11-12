#!/usr/bin/env python3
"""Contract tests for the check-triage transient-rerun stage (issue #9387).

``CHECK_TRIAGE_STAGE=transient-rerun`` in scripts/check_failure_triage.sh asks
GitHub once to re-run the failed jobs of the triaged workflow run, and only
when ci_cancelled_rerun.classify_runner_shutdown calls the first attempt's
failure transient. Every outcome logs one ``CHECK_TRIAGE op=transient_rerun``
line and exits 0. The script runs against a fake ``gh`` on PATH; the
workflow step's wiring is checked as text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_failure_triage.sh"
HELPER = ROOT / "scripts" / "ci_cancelled_rerun.py"
WORKFLOW = ROOT / ".github" / "workflows" / "check_failure_triage.yml"
REPO = "owner/repo"
RUN_ID = 123456
HEAD = "a" * 40
SENTINEL_TOKEN = "ghp_SENTINELtransientRerunToken0123456789"
DETAILS = f"https://github.com/{REPO}/actions/runs/{RUN_ID}/job/501"
SHUTDOWN_MESSAGE = "The runner has received a shutdown signal. This can happen when the runner service is stopped."

FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_CALLS"], "a", encoding="utf-8") as handle:
	handle.write(json.dumps(args) + "\n")
with open(os.environ["FAKE_GH_STATE"], encoding="utf-8") as handle:
	state = json.load(handle)
path = next((arg for arg in args if arg.startswith("repos/")), "")
if "POST" in args:
	if state.get("post_fail"):
		sys.stderr.write("HTTP 500 server error\n")
		sys.exit(1)
	status = state.get("post_status", 201)
	# The body echoes the token so a printed response body would leak it.
	print(f"HTTP/2.0 {status} Created\r\ncontent-type: application/json\r\n\r\n" + json.dumps({"token": os.environ.get("GH_TOKEN", "")}))
	sys.exit(0)
if path.endswith("/attempts/1/jobs"):
	value = state.get("jobs")
elif path.endswith("/annotations"):
	value = state.get("annotations")
elif path == f"repos/{state['repo']}/actions/runs/{state['run_id']}":
	value = state.get("run")
else:
	value = None
if value is None:
	sys.stderr.write("HTTP 404 not found\n")
	sys.exit(1)
print(json.dumps(value))
'''


def _run(**overrides):
	run = {
		"id": RUN_ID, "head_sha": HEAD, "status": "completed", "conclusion": "failure", "run_attempt": 1,
		"repository": {"full_name": REPO},
	}
	run.update(overrides)
	return run


def _jobs():
	return {"total_count": 1, "jobs": [{
		"id": 501, "name": "build", "status": "completed", "conclusion": "failure",
		"check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/601", "steps": [],
	}]}


def _annotations(message=SHUTDOWN_MESSAGE):
	return [{"message": message, "annotation_level": "failure"}]


def _state(**overrides):
	state = {"repo": REPO, "run_id": RUN_ID, "run": _run(), "jobs": _jobs(), "annotations": _annotations()}
	state.update(overrides)
	return state


def run_stage(state=None, env_overrides=None, with_helper=True, helper_source=None):
	"""Run the transient-rerun stage; returns (process, gh argument lists)."""
	with tempfile.TemporaryDirectory(prefix="check-triage-transient-") as temp:
		temp_path = Path(temp)
		bin_dir = temp_path / "bin"
		bin_dir.mkdir()
		fake_gh = bin_dir / "gh"
		fake_gh.write_text(FAKE_GH, encoding="utf-8")
		fake_gh.chmod(0o755)
		trusted = temp_path / "trusted"
		(trusted / "prompts").mkdir(parents=True)
		(trusted / "scripts").mkdir()
		(trusted / "unattended_system_instructions.md").write_text("instructions\n", encoding="utf-8")
		(trusted / "prompts" / "mode-check-failure-triage.txt").write_text("prompt\n", encoding="utf-8")
		helpers = ["check_failure_triage.sh", "gh_helpers.sh", "tg_helpers.sh"]
		if with_helper:
			helpers.append("ci_cancelled_rerun.py")
		for name in helpers:
			shutil.copy2(ROOT / "scripts" / name, trusted / "scripts" / name)
		if helper_source is not None:
			(trusted / "scripts" / "ci_cancelled_rerun.py").write_text(helper_source, encoding="utf-8")
		state_path = temp_path / "state.json"
		state_path.write_text(json.dumps(_state() if state is None else state), encoding="utf-8")
		calls_path = temp_path / "calls.jsonl"
		calls_path.write_text("", encoding="utf-8")
		env = {
			"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
			"HOME": str(temp_path),
			"PYTHONDONTWRITEBYTECODE": "1",
			"GITHUB_REPOSITORY": REPO,
			"GITHUB_RUN_ID": "999",
			"GITHUB_OUTPUT": str(temp_path / "github-output"),
			"RUNTIME_DIR": str(temp_path / "runtime"),
			"CHECK_TRIAGE_TRUSTED_SUPPORT_DIR": str(trusted),
			"CHECK_TRIAGE_STAGE": "transient-rerun",
			"CHECK_TRIAGE_HEAD_SHA": HEAD,
			"CHECK_TRIAGE_DETAILS_URL": DETAILS,
			"CHECK_TRIAGE_PR_NUMBER": "17",
			"CHECK_TRIAGE_CHECK_NAME": "CI",
			"CHECK_TRIAGE_CHECK_CONCLUSION": "failure",
			"GH_TOKEN": SENTINEL_TOKEN,
			"ALERT_MSG_LEVEL": "SILENT",
			"FAKE_GH_CALLS": str(calls_path),
			"FAKE_GH_STATE": str(state_path),
		}
		env.update(env_overrides or {})
		env = {key: value for key, value in env.items() if value is not None}
		proc = subprocess.run(["bash", "scripts/check_failure_triage.sh"], cwd=trusted, env=env,
			capture_output=True, text=True, timeout=300, check=False)
		calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines() if line.strip()]
	return proc, calls


def _posts(calls):
	return [call for call in calls if "POST" in call]


class TransientRerunStageTests(unittest.TestCase):
	def assert_outcome(self, proc, outcome, reason):
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		lines = [line for line in proc.stdout.splitlines() if line.startswith("CHECK_TRIAGE op=transient_rerun ")]
		self.assertEqual(len(lines), 1, proc.stdout)
		self.assertIn(f" outcome={outcome} reason={reason} ", lines[0] + " ")
		self.assertNotIn(SENTINEL_TOKEN, proc.stdout + proc.stderr)
		return lines[0]

	def test_shutdown_verdict_requests_one_rerun_of_failed_jobs(self) -> None:
		proc, calls = run_stage()
		line = self.assert_outcome(proc, "requested", "transient")
		self.assertIn(f"run={RUN_ID} verdict=shutdown", line)
		self.assertEqual(_posts(calls), [["api", "-i", "-X", "POST", f"repos/{REPO}/actions/runs/{RUN_ID}/rerun-failed-jobs"]])
		self.assertFalse(any(call[-1].endswith(f"/runs/{RUN_ID}/rerun") for call in calls))

	def test_flag_off_or_invalid_makes_no_call(self) -> None:
		for value in ("false", "FALSE"):
			proc, calls = run_stage(env_overrides={"CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED": value})
			self.assert_outcome(proc, "skip", "disabled")
			self.assertEqual(calls, [])
		proc, calls = run_stage(env_overrides={"CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED": "maybe"})
		self.assert_outcome(proc, "skip", "disabled")
		self.assertIn("::warning::CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED is not true or false", proc.stdout)
		self.assertNotIn("maybe", proc.stdout + proc.stderr)
		self.assertEqual(calls, [])
		# Unset, empty or any-case true is on.
		for value in (None, "", "True"):
			proc, calls = run_stage(env_overrides={"CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED": value})
			self.assert_outcome(proc, "requested", "transient")

	def test_historical_mode_makes_no_call(self) -> None:
		proc, calls = run_stage(env_overrides={"CHECK_TRIAGE_HISTORICAL_RUN_ID": "37", "CHECK_TRIAGE_HISTORICAL_JOB_ID": "113"})
		self.assert_outcome(proc, "skip", "historical")
		self.assertEqual(calls, [])

	def test_non_actions_or_other_repository_url_makes_no_call(self) -> None:
		for url in ("https://ci.example.com/build/5", f"https://github.com/other/repo/actions/runs/{RUN_ID}",
				f"https://github.com/{REPO}/actions/runs/abc", "", f"https://github.com/{REPO}/actions/runs/{RUN_ID}x"):
			proc, calls = run_stage(env_overrides={"CHECK_TRIAGE_DETAILS_URL": url})
			self.assert_outcome(proc, "skip", "no_workflow_run")
			self.assertEqual(calls, [], url)

	def test_invalid_head_sha_makes_no_call(self) -> None:
		proc, calls = run_stage(env_overrides={"CHECK_TRIAGE_HEAD_SHA": "A" * 40})
		self.assert_outcome(proc, "skip", "head_sha_invalid")
		self.assertEqual(calls, [])

	def test_missing_helper_makes_no_call(self) -> None:
		proc, calls = run_stage(with_helper=False)
		self.assert_outcome(proc, "skip", "helper_unavailable")
		self.assertEqual(calls, [])

	def test_later_attempt_is_capped(self) -> None:
		for attempt in (2, "1", None):
			proc, calls = run_stage(_state(run=_run(run_attempt=attempt)))
			self.assert_outcome(proc, "skip", "attempt_cap")
			self.assertEqual(_posts(calls), [])

	def test_run_mismatch_or_not_failed_makes_no_post(self) -> None:
		for run, reason in (
			(_run(head_sha="b" * 40), "run_mismatch"),
			(_run(id=RUN_ID + 1), "run_mismatch"),
			(_run(repository={"full_name": "other/repo"}), "run_mismatch"),
			(_run(conclusion="cancelled"), "run_not_failed"),
			(_run(status="in_progress", conclusion=None), "run_not_failed"),
		):
			proc, calls = run_stage(_state(run=run))
			self.assert_outcome(proc, "skip", reason)
			self.assertEqual(_posts(calls), [])
		proc, calls = run_stage(_state(run=None))
		self.assert_outcome(proc, "fail_open", "run_unreadable")
		self.assertEqual(_posts(calls), [])

	def test_genuine_and_unverified_verdicts_make_no_post(self) -> None:
		proc, calls = run_stage(_state(annotations=_annotations("Process completed with exit code 1.")))
		line = self.assert_outcome(proc, "skip", "not_transient")
		self.assertIn("verdict=genuine", line)
		self.assertEqual(_posts(calls), [])
		proc, calls = run_stage(_state(jobs=None))
		line = self.assert_outcome(proc, "skip", "not_transient")
		self.assertIn("verdict=unverified", line)
		self.assertEqual(_posts(calls), [])

	def test_post_refused_or_failing_fails_open(self) -> None:
		proc, calls = run_stage(_state(post_status=403))
		self.assert_outcome(proc, "fail_open", "post_not_201")
		self.assertEqual(len(_posts(calls)), 1)
		proc, calls = run_stage(_state(post_fail=True))
		self.assert_outcome(proc, "fail_open", "post_not_201")
		self.assertEqual(len(_posts(calls)), 1)

	def test_broken_helper_fails_open(self) -> None:
		# A helper that raises on import is an error, never a call.
		proc, calls = run_stage(helper_source="raise RuntimeError('boom')\n")
		self.assert_outcome(proc, "fail_open", "error")
		self.assertEqual(calls, [])
		self.assertIn("def rerun_failed_jobs", HELPER.read_text(encoding="utf-8"))

def _step_block(text: str, name: str) -> str:
	start = text.index(f"      - name: {name}\n")
	end = text.find("\n      - name: ", start + 1)
	return text[start:end if end != -1 else len(text)]


class TransientRerunWorkflowContractTests(unittest.TestCase):
	def setUp(self) -> None:
		self.text = WORKFLOW.read_text(encoding="utf-8")
		self.step = _step_block(self.text, "Re-run transient check failure")

	def test_step_uses_gh_pat_and_fails_open(self) -> None:
		self.assertIn("        id: transient_rerun\n", self.step)
		self.assertIn("        continue-on-error: true\n", self.step)
		self.assertIn("          GH_TOKEN: ${{ secrets.GH_PAT }}\n", self.step)
		self.assertIn("          CHECK_TRIAGE_STAGE: transient-rerun\n", self.step)
		self.assertIn(
			"        if: ${{ steps.collect_triage.outputs.ready == 'true' && inputs.historical_run_id == '' }}\n", self.step)
		self.assertNotIn("CHECK_TRIAGE_ISSUES_TOKEN", self.step)
		self.assertNotIn("GITHUB_ENV", self.step)
		run_body = self.step.split("        run: |\n", 1)[1]
		self.assertNotIn("${{", run_body)
		self.assertIn('cd "${CHECK_TRIAGE_TRUSTED_SUPPORT_DIR:?trusted support is required}"', run_body)

	def test_step_runs_between_collect_and_diagnose(self) -> None:
		collect = self.text.index("      - name: Collect check-failure context\n")
		rerun = self.text.index("      - name: Re-run transient check failure\n")
		diagnose = self.text.index("      - name: Diagnose check failure\n")
		self.assertLess(collect, rerun)
		self.assertLess(rerun, diagnose)

	def test_gh_pat_never_reaches_the_post_step_and_issues_token_stays_there(self) -> None:
		post = _step_block(self.text, "Post check-failure triage issue")
		self.assertIn("GH_TOKEN: ${{ secrets.CHECK_TRIAGE_ISSUES_TOKEN }}", post)
		self.assertNotIn("secrets.GH_PAT", post)

	def test_job_env_flag_defaults_true(self) -> None:
		self.assertIn(
			"      CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED: ${{ vars.CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED || 'true' }}\n", self.text)

	def test_helper_is_staged_into_the_trusted_dir_as_optional(self) -> None:
		staging = _step_block(self.text, "Stage workflow support files")
		self.assertIn('"${trusted_dir}/scripts/ci_cancelled_rerun.py"', staging)
		self.assertIn('if [ "${f}" = "ci_cancelled_rerun.py" ]; then', staging)

	def test_script_defaults_the_flag_on(self) -> None:
		script = SCRIPT.read_text(encoding="utf-8")
		self.assertIn('"${CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED:-}"', script)
		self.assertIn("''|true) transient_rerun_enabled=true ;;", script)
		self.assertIn("all|collect|diagnose|route|supersede|supersede-check|transient-rerun) ;;", script)


if __name__ == "__main__":
	sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)

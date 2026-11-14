#!/usr/bin/env python3
"""Contract tests for the transient outage re-run of check-failure triage (issue #9130).

An attempt-1 check failure whose diagnosis names no AFFECTED_FILES and whose
failed-job log tail shows an outage signature re-runs its failed jobs once
(one ``rerun-failed-jobs`` POST) instead of filing a triage issue. Every other
case files the issue as before.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
	import yaml
except ModuleNotFoundError:  # PyYAML is absent in some credential-free test containers.
	yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import workflow_failure_heal as heal  # noqa: E402

TRIAGE_SCRIPT_PATH = REPO_ROOT / "scripts" / "check_failure_triage.sh"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "check_failure_triage.yml"
REPO = "owner/repo"
RUN_ID = 777
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
SECRET_BODY = "RESPONSE-BODY-MUST-NOT-PRINT"
HEAL_SHIM = """#!/usr/bin/env python3
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("heal_under_test", {real!r})
module = importlib.util.module_from_spec(spec)
sys.modules["heal_under_test"] = module
spec.loader.exec_module(module)
sys.exit(module.main())
"""


def _context(tail: str, *, run_id: int = RUN_ID, other_tail: str = "all good") -> str:
	return (
		f"head_sha: {HEAD}\n"
		"failed[0].name: build\n"
		"failed[0].status: completed\n"
		"failed[0].conclusion: failure\n"
		"failed[0].app: github-actions\n"
		f"failed[0].html_url: https://github.com/{REPO}/actions/runs/{run_id}/job/5\n"
		f"failed[0].details_url: https://github.com/{REPO}/actions/runs/{run_id}/job/5\n"
		"failed[0].title: \n"
		"failed[0].summary: \n"
		f"failed[0].log_tail ({len(tail)} chars):\n"
		f"{tail}\n"
		"\n"
		"failed[1].name: lint\n"
		f"failed[1].details_url: https://github.com/{REPO}/actions/runs/999/job/6\n"
		f"failed[1].log_tail ({len(other_tail)} chars):\n"
		f"{other_tail}\n"
		"\n"
	)


OUTAGE_TAIL = (
	"log_tail_anchor: step_error\n"
	"Installing foundry...\n"
	"curl: (22) The requested URL returned error: 504\n"
	"##[error]Process completed with exit code 22."
)
BUG_TAIL = "AssertionError: expected 1 got 2\n##[error]Process completed with exit code 1."
DIAGNOSIS = "## Summary\nGitHub Releases returned 504; no code change. Re-run the jobs.\n"
AFFECTED_DIAGNOSIS = DIAGNOSIS + "AFFECTED_FILES_BEGIN\n- src/app.js\nAFFECTED_FILES_END\n"


def _run(**overrides) -> dict:
	run = {
		"id": RUN_ID, "repository": {"full_name": REPO}, "event": "pull_request", "run_attempt": 1,
		"status": "completed", "conclusion": "failure", "head_sha": HEAD,
		"pull_requests": [{"number": 17}], "head_branch": "feature/x",
	}
	run.update(overrides)
	return run


def _pr(**overrides) -> dict:
	pr = {"number": 17, "state": "open", "merged": False, "head": {"sha": HEAD, "ref": "feature/x", "repo": {"full_name": REPO}}}
	pr.update(overrides)
	return pr


class TransientClassifierTests(unittest.TestCase):
	def classify(self, diagnosis=DIAGNOSIS, tail=OUTAGE_TAIL, **kwargs):
		return heal.classify_check_triage_transient(diagnosis, _context(tail), RUN_ID, **kwargs)

	def full(self, run=None, pr=None, **kwargs):
		return self.classify(run=run if run is not None else _run(), pr=pr if pr is not None else _pr(),
			repo=REPO, head_sha=HEAD, pr_number=17, **kwargs)

	def test_offline_candidate_and_full_rerun(self) -> None:
		self.assertEqual(self.classify()["decision"], "candidate")
		self.assertEqual(self.full(), {"decision": "rerun", "reason": "transient_signature", "attempt": 1})

	def test_signatures(self) -> None:
		for line in ("HTTP/2 503", "502 Gateway error", "Gateway Time-out", "Gateway Timeout", "connect timed out",
				"ETIMEDOUT", "read ECONNRESET", "Connection reset by peer", "curl: (28) Operation timed out",
				"curl: (22) The requested URL returned error: 500", "Could not resolve host: github.com"):
			with self.subTest(line=line):
				self.assertEqual(self.classify(tail=line)["decision"], "candidate")

	def test_affected_files_block_skips(self) -> None:
		for diagnosis in (AFFECTED_DIAGNOSIS, DIAGNOSIS + "AFFECTED_FILES_BEGIN\nAFFECTED_FILES_END\n", DIAGNOSIS + "AFFECTED_FILES_BEGIN\n"):
			with self.subTest(diagnosis=diagnosis):
				self.assertEqual(self.classify(diagnosis=diagnosis)["reason"], "affected_files_present")

	def test_no_signature_or_signature_in_other_run_skips(self) -> None:
		self.assertEqual(self.classify(tail=BUG_TAIL)["reason"], "no_transient_signature")
		context = _context(BUG_TAIL, other_tail="curl: (28) Operation timed out")
		self.assertEqual(heal.classify_check_triage_transient(DIAGNOSIS, context, RUN_ID)["reason"], "no_transient_signature")

	def test_signature_outside_log_tail_is_ignored(self) -> None:
		context = _context(BUG_TAIL).replace("failed[0].summary: ", "failed[0].summary: HTTP 503")
		self.assertEqual(heal.classify_check_triage_transient(DIAGNOSIS + "HTTP 503\n", context, RUN_ID)["reason"], "no_transient_signature")

	def test_run_gates(self) -> None:
		cases = {
			"attempt_not_first": _run(run_attempt=2),
			"run_not_completed": _run(status="in_progress"),
			"run_not_failed": _run(conclusion="success"),
			"run_head_mismatch": _run(head_sha=OTHER_HEAD),
			"run_identity_mismatch": _run(event="push"),
			"run_pr_mismatch": _run(pull_requests=[], head_branch="other"),
		}
		for reason, run in cases.items():
			with self.subTest(reason=reason):
				self.assertEqual(self.full(run=run)["reason"], reason)
		self.assertEqual(self.full(run=_run(repository={"full_name": "fork/repo"}))["reason"], "run_identity_mismatch")
		self.assertEqual(self.full(run=_run(id=1))["reason"], "run_identity_mismatch")

	def test_pr_gates(self) -> None:
		self.assertEqual(self.full(pr=_pr(state="closed"))["reason"], "pr_not_open")
		self.assertEqual(self.full(pr=_pr(head={"sha": OTHER_HEAD, "ref": "feature/x", "repo": {"full_name": REPO}}))["reason"], "head_moved")
		self.assertEqual(self.full(pr=_pr(head={"sha": HEAD, "ref": "feature/x", "repo": {"full_name": "fork/repo"}}))["reason"], "pr_cross_repository")
		self.assertEqual(self.full(pr={})["reason"], "pr_unreadable")


class TransientRerunStageTests(unittest.TestCase):
	def run_stage(self, *, diagnosis=DIAGNOSIS, tail=OUTAGE_TAIL, run=None, pr=None, post_status="201 Created",
			post_rc=0, run_read_rc=0, switch: str | None = "true", source="model", metadata_extra=None,
			details_url=f"https://github.com/{REPO}/actions/runs/{RUN_ID}/job/5", stage="transient-rerun") -> dict:
		with tempfile.TemporaryDirectory(prefix="check-triage-transient-") as temp_dir:
			root = Path(temp_dir)
			trusted = root / "trusted"
			runtime = root / "runtime"
			bin_dir = root / "bin"
			for directory in (trusted / "scripts", trusted / "prompts", runtime, bin_dir):
				directory.mkdir(parents=True)
			(trusted / "unattended_system_instructions.md").write_text("Trusted instructions\n")
			(trusted / "prompts" / "mode-check-failure-triage.txt").write_text("Trusted prompt\n")
			(trusted / "scripts" / "workflow_failure_heal.py").write_text(HEAL_SHIM.format(real=str(REPO_ROOT / "scripts" / "workflow_failure_heal.py")))
			metadata = {"pr_number": "17", "check_name": "CI", "fingerprint": "f" * 64}
			metadata.update(metadata_extra or {})
			(runtime / "triage_metadata.json").write_text(json.dumps(metadata))
			(runtime / "pr_payload.json").write_text(json.dumps(pr if pr is not None else _pr()))
			(runtime / "pr_check_runs_context.txt").write_text(_context(tail))
			(runtime / "diagnosis.md").write_text(diagnosis)
			if source is not None:
				(runtime / "diagnosis_source.txt").write_text(source + "\n")
			run_file = root / "run.json"
			run_file.write_text(json.dumps(run if run is not None else _run()))
			gh_log = root / "gh-calls.log"
			gh_script = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {str(gh_log)!r}
case "$*" in
	*rerun-failed-jobs*)
		printf 'HTTP/2.0 {post_status}\\r\\nContent-Type: application/json\\r\\n\\r\\n{{"secret":"{SECRET_BODY}"}}\\n'
		exit {int(post_rc)} ;;
	*"actions/runs/{RUN_ID}"*) cat {str(run_file)!r}; exit {int(run_read_rc)} ;;
	*"issue create"*) echo "https://github.com/{REPO}/issues/900"; exit 0 ;;
esac
exit 1
"""
			(bin_dir / "gh").write_text(gh_script)
			(bin_dir / "gh").chmod(0o755)
			env = {key: value for key, value in os.environ.items() if not key.startswith(("CHECK_TRIAGE_", "TG_", "GH_", "BASH_ENV"))}
			output_path = root / "output"
			env.update({
				"CHECK_TRIAGE_TRUSTED_SUPPORT_DIR": str(trusted), "GITHUB_REPOSITORY": REPO, "GITHUB_OUTPUT": str(output_path),
				"GITHUB_RUN_ID": "4242", "RUNTIME_DIR": str(runtime), "CHECK_TRIAGE_STAGE": stage,
				"CHECK_TRIAGE_HEAD_SHA": HEAD, "CHECK_TRIAGE_DETAILS_URL": details_url, "CHECK_TRIAGE_PR_NUMBER": "17",
				"GH_TOKEN": "fake-token-value", "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			})
			if switch is not None:
				env["CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED"] = switch
			proc = subprocess.run(["bash", str(TRIAGE_SCRIPT_PATH)], cwd=trusted, capture_output=True, text=True, timeout=120, check=False, env=env)
			return {
				"rc": proc.returncode, "logs": proc.stdout + proc.stderr,
				"calls": gh_log.read_text().splitlines() if gh_log.exists() else [],
				"output": output_path.read_text() if output_path.exists() else "",
			}

	def posts(self, result: dict) -> list[str]:
		return [call for call in result["calls"] if "rerun-failed-jobs" in call]

	def assert_files_issue(self, result: dict, outcome: str, reason: str) -> None:
		self.assertEqual(result["rc"], 0, result["logs"])
		self.assertNotIn("rerun=true", result["output"])
		self.assertIn(f"CHECK_TRIAGE_TRANSIENT_RERUN outcome={outcome} reason={reason}", result["logs"])

	def test_attempt_one_outage_reruns_once(self) -> None:
		result = self.run_stage()
		self.assertEqual(result["rc"], 0, result["logs"])
		self.assertIn("rerun=true", result["output"])
		self.assertEqual(self.posts(result), [f"api -i -X POST repos/{REPO}/actions/runs/{RUN_ID}/rerun-failed-jobs"])
		self.assertIn(f"CHECK_TRIAGE_TRANSIENT_RERUN outcome=rerun reason=transient_signature run={RUN_ID} attempt=1 pr=17", result["logs"])
		self.assertNotIn(SECRET_BODY, result["logs"])
		self.assertFalse([call for call in result["calls"] if "issue create" in call])

	def test_attempt_two_files_issue(self) -> None:
		result = self.run_stage(run=_run(run_attempt=2))
		self.assert_files_issue(result, "skip", "attempt_not_first")
		self.assertEqual(self.posts(result), [])

	def test_affected_files_and_no_signature_make_no_api_call(self) -> None:
		for kwargs, reason in (({"diagnosis": AFFECTED_DIAGNOSIS}, "affected_files_present"), ({"tail": BUG_TAIL}, "no_transient_signature")):
			with self.subTest(reason=reason):
				result = self.run_stage(**kwargs)
				self.assert_files_issue(result, "skip", reason)
				self.assertEqual(result["calls"], [])

	def test_head_moved_or_pr_closed_files_issue(self) -> None:
		moved = self.run_stage(pr=_pr(head={"sha": OTHER_HEAD, "ref": "feature/x", "repo": {"full_name": REPO}}))
		self.assert_files_issue(moved, "skip", "head_moved")
		self.assert_files_issue(self.run_stage(pr=_pr(state="closed")), "skip", "pr_not_open")
		self.assertEqual(self.posts(moved), [])

	def test_refused_post_or_unreadable_run_files_issue(self) -> None:
		refused = self.run_stage(post_status="403 Forbidden", post_rc=1)
		self.assert_files_issue(refused, "failed", "rerun_refused")
		self.assertNotIn(SECRET_BODY, refused["logs"])
		self.assert_files_issue(self.run_stage(post_status="200 OK"), "failed", "rerun_refused")
		unreadable = self.run_stage(run_read_rc=1)
		self.assert_files_issue(unreadable, "failed", "run_unreadable")
		self.assertEqual(self.posts(unreadable), [])

	def test_switch_off_makes_no_call(self) -> None:
		result = self.run_stage(switch="false")
		self.assert_files_issue(result, "skip", "disabled")
		self.assertEqual(result["calls"], [])
		invalid = self.run_stage(switch="maybe")
		self.assert_files_issue(invalid, "skip", "disabled")
		self.assertIn("::warning::CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED", invalid["logs"])
		self.assertIn("rerun=true", self.run_stage(switch=None)["output"])

	def test_historical_fallback_and_non_actions_skip(self) -> None:
		self.assert_files_issue(self.run_stage(metadata_extra={"historical_run_id": "37"}), "skip", "historical")
		self.assert_files_issue(self.run_stage(source="fallback"), "skip", "fallback_diagnosis")
		self.assert_files_issue(self.run_stage(source=None), "skip", "diagnosis_source_missing")
		self.assert_files_issue(self.run_stage(details_url="https://ci.example.com/build/5"), "skip", "not_actions_run")


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class TransientRerunWorkflowTests(unittest.TestCase):
	def test_step_order_token_and_post_gate(self) -> None:
		workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
		job = workflow["jobs"]["triage"]
		names = [step.get("name") for step in job["steps"]]
		rerun_index = names.index("Re-run transient check failure")
		self.assertLess(names.index("Route upstream-managed check failure"), rerun_index)
		self.assertLess(rerun_index, names.index("Post check-failure triage issue"))
		step = job["steps"][rerun_index]
		self.assertEqual(step["id"], "transient_rerun")
		self.assertTrue(step["continue-on-error"])
		self.assertEqual(step["env"]["GH_TOKEN"], "${{ secrets.GH_PAT }}")
		self.assertEqual(step["env"]["CHECK_TRIAGE_STAGE"], "transient-rerun")
		self.assertEqual(step["env"]["CHECK_TRIAGE_DETAILS_URL"], "${{ inputs.details_url }}")
		self.assertEqual(step["env"]["CHECK_TRIAGE_HEAD_SHA"], "${{ inputs.head_sha }}")
		self.assertNotIn("OPENROUTER_API_KEY", step["env"])
		self.assertIn("steps.upstream_route.outputs.routed != 'true'", step["if"])
		self.assertIn("steps.transient_rerun.outputs.rerun != 'true'", job["steps"][names.index("Post check-failure triage issue")]["if"])
		self.assertEqual(job["env"]["CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED"], "${{ vars.CHECK_TRIAGE_TRANSIENT_RERUN_ENABLED || 'true' }}")


if __name__ == "__main__":
	unittest.main()

#!/usr/bin/env python3
"""AI Cancel on PR Close cancels the closed PR's unlinked review runs (#10049).

GitHub links a workflow run only to pull requests that are open now, so once
a PR closes its in-flight review run can list `pull_requests: []`. The
`pull_request` branch of the step "Cancel queued/in-progress runs for closed
PR branch" (.github/workflows/cancel_on_pr_close.yml) kept only runs linked
to the closed PR, so tele-funtoken-msg-scoring PR #5800's review run (gate
finished, merge job queued) survived the close and merged the PR 22 minutes
later. Unlinked runs on the closed PR's branch at its exact head SHA are now
cancelled too; runs linked to another PR and runs at another SHA stay.

The step body is extracted from the workflow text and run with a fake `gh`
on PATH that serves the run listings and records the cancel POSTs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "cancel_on_pr_close.yml"
STEP_NAME = "Cancel queued/in-progress runs for closed PR branch"

PR = 5800
BRANCH = "ai/issue-5797"
SHA = "fee7005234cc58358be7a9eaddb29c3e3e8ac3d1"
OTHER_SHA = "1" * 40

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
if args[:1] == ["api"] and ("-X" in args or "--method" in args) and any(a.endswith("/cancel") for a in args):
    sys.exit(0)
if args[:1] == ["api"] and any(a.endswith("/actions/runs") for a in args):
    status = next((a.split("=", 1)[1] for a in args if a.startswith("status=")), "")
    runs = json.load(open(os.environ["FAKE_GH_RUNS"], encoding="utf-8"))
    print(json.dumps({"workflow_runs": [r for r in runs if r["status"] == status]}))
    sys.exit(0)
sys.stderr.write("unhandled: %r\n" % (args,))
sys.exit(1)
'''


def _step_run_body() -> str:
	lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
	start = next(i for i, ln in enumerate(lines) if ln.strip() == f"- name: {STEP_NAME}")
	run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
	body: list[str] = []
	for ln in lines[run_at + 1:]:
		if ln.strip() and (len(ln) - len(ln.lstrip(" "))) <= 6:
			break
		body.append(ln)
	return textwrap.dedent("\n".join(body)) + "\n"


def _run(pr_links: list[int], **overrides) -> dict:
	run = {"id": 0, "status": "in_progress", "event": "pull_request", "head_branch": BRANCH, "head_sha": SHA,
		"path": ".github/workflows/ai-review.yml", "pull_requests": [{"number": n} for n in pr_links]}
	run.update(overrides)
	return run


class CancelClosedPrRuns(unittest.TestCase):
	def setUp(self) -> None:
		if shutil.which("jq") is None:
			raise unittest.SkipTest("jq binary not available in test environment")
		self.tmp = Path(tempfile.mkdtemp(prefix="cancel_close_"))
		bindir = self.tmp / "bin"
		bindir.mkdir()
		gh = bindir / "gh"
		gh.write_text(FAKE_GH, encoding="utf-8")
		gh.chmod(0o755)
		self.calls = self.tmp / "calls.jsonl"
		self.runs = self.tmp / "runs.json"
		self.bindir = bindir

	def tearDown(self) -> None:
		shutil.rmtree(self.tmp, ignore_errors=True)

	def _cancelled(self, runs: list[dict], head_sha: str = SHA) -> tuple[list[int], str]:
		self.runs.write_text(json.dumps(runs), encoding="utf-8")
		self.calls.write_text("", encoding="utf-8")
		env = {
			"PATH": f"{self.bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
			"FAKE_GH_CALLS": str(self.calls),
			"FAKE_GH_RUNS": str(self.runs),
			"EVENT_NAME": "pull_request",
			"PR_NUMBER": str(PR),
			"PR_HEAD_REF": BRANCH,
			"PR_HEAD_SHA": head_sha,
			"REPOSITORY": "owner/repo",
			"CURRENT_RUN_ID": "1",
			"TMPDIR": str(self.tmp),
			"PYTHONDONTWRITEBYTECODE": "1",
		}
		res = subprocess.run(["bash", "-c", _step_run_body()], cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)
		self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
		cancelled = []
		for line in self.calls.read_text(encoding="utf-8").splitlines():
			args = json.loads(line)
			for a in args:
				if a.endswith("/cancel"):
					cancelled.append(int(a.split("/")[-2]))
		return sorted(cancelled), res.stdout

	def test_unlinked_run_at_the_closed_head_is_cancelled_after_its_gate_finished(self) -> None:
		# #5800: the gate job finished, the merge job is queued, the run is
		# in_progress and GitHub no longer links it to the closed PR.
		cancelled, _ = self._cancelled([_run([], id=45386407946)])
		self.assertEqual(cancelled, [45386407946])

	def test_unlinked_run_at_another_sha_is_kept(self) -> None:
		cancelled, _ = self._cancelled([_run([], id=11, head_sha=OTHER_SHA)])
		self.assertEqual(cancelled, [])

	def test_run_linked_to_another_pr_is_kept(self) -> None:
		cancelled, _ = self._cancelled([_run([9999], id=12)])
		self.assertEqual(cancelled, [])

	def test_linked_runs_are_still_cancelled(self) -> None:
		cancelled, _ = self._cancelled([_run([PR], id=13), _run([PR], id=14, status="queued", head_sha=OTHER_SHA)])
		self.assertEqual(cancelled, [13, 14])

	def test_without_a_full_head_sha_unlinked_runs_are_kept(self) -> None:
		cancelled, _ = self._cancelled([_run([], id=15)], head_sha="")
		self.assertEqual(cancelled, [])

	def test_cancel_workflows_and_the_current_run_are_never_cancelled(self) -> None:
		cancelled, _ = self._cancelled([
			_run([], id=1),
			_run([], id=16, path=".github/workflows/ai-cancel-on-pr-close.yml"),
			_run([], id=17, event="push"),
			_run([], id=18, head_branch="other-branch"),
		])
		self.assertEqual(cancelled, [])

	def test_job_env_exposes_the_closed_head_sha(self) -> None:
		text = WORKFLOW.read_text(encoding="utf-8")
		self.assertIn("      PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}\n", text)


if __name__ == "__main__":
	unittest.main()

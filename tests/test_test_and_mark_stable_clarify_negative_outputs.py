#!/usr/bin/env python3
"""Regression checks for the clarify negative test's step outputs (#9797).

Release gate run 44490333814 failed in "Create unsolvable issue" because a
rate-limited `gh api` read printed its JSON error body to stdout, and the step
wrote that multi-line body into GITHUB_OUTPUT. These tests run the shipped
step bodies against a fake `gh` and check that only single-line numeric
values reach GITHUB_OUTPUT.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
JOB = "clarify-rejects-unsolvable-test"
ALT_JOB = "e2e-alt-model-test"

RATE_LIMIT_BODY = (
	'{\n'
	'\t"message": "API rate limit exceeded for user ID 1. If you reach out to GitHub Support, include the request ID X.",\n'
	'\t"documentation_url": "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api",\n'
	'\t"status": "403"\n'
	'}\n'
)

# Fake `gh`: the clarify.yml runs read fails STUB_RUNS_FAIL_COUNT times (-1:
# always) the way gh reports a failed request (error body on stdout, message
# on stderr, exit 1), then returns STUB_RUNS_PAYLOAD through the real jq. The
# issue POST prints STUB_ISSUE_OUT verbatim.
STUB_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
log_path = os.environ["STUB_GH_LOG"]
with open(log_path, "a", encoding="utf-8") as log:
	log.write(json.dumps(args) + "\n")
if len(args) < 2 or args[0] != "api":
	sys.exit(1)
path = args[1]
jq_filter = args[args.index("--jq") + 1] if "--jq" in args else None
if "/actions/workflows/clarify.yml/runs?" in path:
	with open(log_path, encoding="utf-8") as log:
		reads = sum(1 for line in log if "/actions/workflows/clarify.yml/runs?" in json.loads(line)[1])
	fail_count = int(os.environ.get("STUB_RUNS_FAIL_COUNT", "0"))
	if fail_count < 0 or reads <= fail_count:
		sys.stdout.write(os.environ["STUB_RATE_LIMIT_BODY"])
		sys.stderr.write("gh: API rate limit exceeded (HTTP 403)\n")
		sys.exit(1)
	payload = os.environ.get("STUB_RUNS_PAYLOAD", '{"workflow_runs": []}')
	result = subprocess.run(["jq", "-r", jq_filter], input=payload, text=True, capture_output=True)
	sys.stdout.write(result.stdout)
	sys.stderr.write(result.stderr)
	sys.exit(result.returncode)
if path.endswith("/issues"):
	sys.stdout.write(os.environ.get("STUB_ISSUE_OUT", ""))
	sys.exit(0)
sys.exit(1)
'''

# Fake `date`: `+%s` advances 10 seconds per call so the 90 s poll ends fast.
STUB_DATE = r'''#!/usr/bin/env bash
if [ "${1:-}" = "+%s" ]; then
	n=$(cat "${STUB_DATE_FILE}" 2>/dev/null || echo 1000)
	echo $(( n + 10 )) > "${STUB_DATE_FILE}"
	echo "${n}"
	exit 0
fi
exec /bin/date "$@"
'''

STUB_SLEEP = "#!/usr/bin/env bash\nexit 0\n"

OUTPUT_LINE_RE = re.compile(r"^[a-z_]+=[0-9]+$")


def _step_script(step_id: str, job: str = JOB) -> str:
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	for step in workflow["jobs"][job]["steps"]:
		if step.get("id") == step_id:
			return step["run"]
	raise AssertionError(f"step {step_id!r} not found in job {job!r}")


@unittest.skipIf(shutil.which("jq") is None, "jq is required")
class ClarifyNegativeOutputsTest(unittest.TestCase):
	def setUp(self) -> None:
		self._tmp = tempfile.TemporaryDirectory()
		self.tmp = Path(self._tmp.name)
		bin_dir = self.tmp / "bin"
		bin_dir.mkdir()
		for name, body in (("gh", STUB_GH), ("date", STUB_DATE), ("sleep", STUB_SLEEP)):
			path = bin_dir / name
			path.write_text(body, encoding="utf-8")
			path.chmod(0o755)
		self.output = self.tmp / "github_output"
		self.output.write_text("", encoding="utf-8")
		self.gh_log = self.tmp / "gh.log"
		self.env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
			"HOME": str(self.tmp),
			"GITHUB_OUTPUT": str(self.output),
			"GITHUB_RUN_ID": "1",
			"TEST_REPO": "owner/repo",
			"STUB_GH_LOG": str(self.gh_log),
			"STUB_DATE_FILE": str(self.tmp / "date"),
			"STUB_RATE_LIMIT_BODY": RATE_LIMIT_BODY,
		}

	def tearDown(self) -> None:
		self._tmp.cleanup()

	def _run(self, step_id: str, job: str = JOB, **extra: str) -> subprocess.CompletedProcess:
		env = dict(self.env)
		env.update(extra)
		return subprocess.run(
			["bash", "-c", _step_script(step_id, job)],
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

	def _outputs(self) -> list[str]:
		return [line for line in self.output.read_text(encoding="utf-8").splitlines()]

	def _assert_outputs_numeric(self) -> None:
		for line in self._outputs():
			self.assertRegex(line, OUTPUT_LINE_RE)

	def _runs_reads(self) -> int:
		if not self.gh_log.exists():
			return 0
		return sum(1 for line in self.gh_log.read_text(encoding="utf-8").splitlines() if "clarify.yml/runs?" in line)

	def test_create_step_rate_limited_pre_read_writes_numeric_outputs(self) -> None:
		result = self._run("create", STUB_RUNS_FAIL_COUNT="-1", STUB_ISSUE_OUT="9782\n")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self._assert_outputs_numeric()
		self.assertIn("pre_clarify_run_id=0", self._outputs())
		self.assertIn("pre_clarify_run_id_read_failed=1", self._outputs())
		self.assertIn("issue_number=9782", self._outputs())
		self.assertIn("::warning::Could not read the latest clarify.yml run id", result.stdout)
		self.assertNotIn('"message":', result.stdout + result.stderr)

	def test_create_step_happy_path_keeps_run_id(self) -> None:
		result = self._run(
			"create",
			STUB_RUNS_PAYLOAD='{"workflow_runs": [{"id": 123}]}',
			STUB_ISSUE_OUT="9782\n",
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self._outputs(), ["pre_clarify_run_id=123", "issue_number=9782"])

	def test_create_step_non_numeric_issue_number_fails_closed(self) -> None:
		for issue_out in ("", "null\n", "12abc\n"):
			with self.subTest(issue_out=issue_out):
				self.output.write_text("", encoding="utf-8")
				result = self._run(
					"create",
					STUB_RUNS_PAYLOAD='{"workflow_runs": [{"id": 5}]}',
					STUB_ISSUE_OUT=issue_out,
				)
				self.assertNotEqual(result.returncode, 0)
				self.assertIn("::error::Issue creation returned a non-numeric issue number", result.stdout)
				self.assertFalse(any(line.startswith("issue_number=") for line in self._outputs()))
				self._assert_outputs_numeric()

	def test_capture_step_rate_limited_poll_keeps_polling(self) -> None:
		result = self._run(
			"capture_clarify_run",
			PRE="100",
			STUB_RUNS_FAIL_COUNT="2",
			STUB_RUNS_PAYLOAD='{"workflow_runs": [{"id": 90, "created_at": "2026-11-19T09:00:00Z"}, {"id": 150, "created_at": "2026-11-19T09:16:00Z"}]}',
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self._outputs(), ["run_id=150"])
		self.assertGreater(self._runs_reads(), 1)

	def test_capture_step_all_polls_rate_limited_emits_no_run_id(self) -> None:
		result = self._run("capture_clarify_run", PRE="100", STUB_RUNS_FAIL_COUNT="-1")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self._outputs(), [])
		self.assertIn("::warning::Could not capture clarify run id", result.stdout)

	def test_capture_step_unreadable_baseline_skips_poll(self) -> None:
		# A failed baseline read must not anchor the poll at 0, which would
		# capture an older, unrelated clarify run for the analyser.
		payload = '{"workflow_runs": [{"id": 42, "created_at": "2026-11-19T09:16:00Z"}]}'
		for extra in ({"PRE": "0", "PRE_READ_FAILED": "1"}, {"PRE": "garbage"}, {"PRE": ""}):
			with self.subTest(**extra):
				self.output.write_text("", encoding="utf-8")
				if self.gh_log.exists():
					self.gh_log.unlink()
				result = self._run("capture_clarify_run", STUB_RUNS_PAYLOAD=payload, **extra)
				self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
				self.assertEqual(self._outputs(), [])
				self.assertIn("baseline run id unavailable", result.stdout)
				self.assertEqual(self._runs_reads(), 0)

	def test_capture_step_real_zero_baseline_still_polls(self) -> None:
		result = self._run(
			"capture_clarify_run",
			PRE="0",
			STUB_RUNS_PAYLOAD='{"workflow_runs": [{"id": 42, "created_at": "2026-11-19T09:16:00Z"}]}',
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self._outputs(), ["run_id=42"])

	def test_alt_create_step_rate_limited_read_writes_single_line_outputs(self) -> None:
		result = self._run(
			"create",
			job=ALT_JOB,
			ALT_EDITOR_MODEL="test/model",
			STUB_RUNS_FAIL_COUNT="-1",
			STUB_ISSUE_OUT="9790\n",
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		outputs = self._outputs()
		for line in outputs:
			self.assertRegex(line, r"^[a-z_]+=[0-9]*$")
		self.assertIn("pre_clarify_run_id=", outputs)
		self.assertIn("issue_number=9790", outputs)
		self.assertNotIn('"message":', result.stdout + result.stderr)

	def test_alt_create_step_non_numeric_issue_number_fails_closed(self) -> None:
		result = self._run("create", job=ALT_JOB, ALT_EDITOR_MODEL="test/model", STUB_ISSUE_OUT="null\n")
		self.assertNotEqual(result.returncode, 0)
		self.assertIn("::error::Issue creation returned a non-numeric issue number", result.stdout)
		self.assertFalse(any(line.startswith("issue_number=") for line in self._outputs()))

	def test_alt_capture_step_rate_limited_or_unanchored_emits_no_run_id(self) -> None:
		for pre_clarify in ("100", ""):
			with self.subTest(pre_clarify=pre_clarify):
				self.output.write_text("", encoding="utf-8")
				result = self._run(
					"capture_alt_runs",
					job=ALT_JOB,
					ISSUE_NUMBER="9790",
					PRE_CLARIFY=pre_clarify,
					PRE_PLAN="",
					PRE_IMPLEMENT="",
					PRE_REVIEW="",
					STUB_RUNS_FAIL_COUNT="-1",
				)
				self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
				self.assertEqual(self._outputs(), [])
				self.assertNotIn('"message":', result.stdout + result.stderr)


if __name__ == "__main__":
	unittest.main()

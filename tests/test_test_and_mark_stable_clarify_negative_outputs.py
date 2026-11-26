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
import sys
import tempfile
import unittest
from pathlib import Path

# PyYAML is optional: without it `_step_script` reads the step's `run: |`
# block from the workflow text, so these tests still run in a container
# that has no PyYAML.
try:
	import yaml
except ImportError:  # pragma: no cover - depends on the environment
	yaml = None


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

# Fake `gh`: each clarify.yml / plan.yml runs read fails STUB_RUNS_FAIL_COUNT
# times per workflow (-1: always) the way gh reports a failed request (error
# body on stdout, message on stderr, exit 1), then returns STUB_RUNS_PAYLOAD through the real jq. The
# issue POST prints STUB_ISSUE_OUT verbatim.
STUB_GH = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys
args = sys.argv[1:]
log_path = os.environ["STUB_GH_LOG"]
with open(log_path, "a", encoding="utf-8") as log:
	log.write(json.dumps(args) + "\n")
if len(args) < 2 or args[0] != "api":
	sys.exit(1)
path = args[1]
jq_filter = args[args.index("--jq") + 1] if "--jq" in args else None
runs_match = re.search(r"/actions/workflows/(clarify|plan)\.yml/runs\?", path)
if runs_match:
	with open(log_path, encoding="utf-8") as log:
		reads = sum(1 for line in log if runs_match.group(0) in json.loads(line)[1])
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

# Shell-function fakes prepended to each step body (see setUp).
STUB_FAKES = (
	'gh() { "$STUB_PYTHON" "$STUB_GH_PY" "$@"; }\n'
	'date() { bash "$STUB_DATE_SH" "$@"; }\n'
	'sleep() { :; }\n'
)

OUTPUT_LINE_RE = re.compile(r"^[a-z_]+=[0-9]+$")


def _step_script_from_text(step_id: str, job: str) -> str:
	"""Return a step's literal `run: |` body by scanning the workflow text.

	Jobs sit at 2-space indent, steps at `      - `, step keys at 8 spaces
	and the run body at 10 spaces, as in the shipped workflow.
	"""
	lines = WORKFLOW.read_text(encoding="utf-8").split("\n")
	try:
		start = lines.index(f"  {job}:")
	except ValueError:
		raise AssertionError(f"job {job!r} not found") from None
	end = next(
		(k for k in range(start + 1, len(lines)) if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[k])),
		len(lines),
	)
	try:
		id_line = lines.index(f"        id: {step_id}", start, end)
	except ValueError:
		raise AssertionError(f"step {step_id!r} not found in job {job!r}") from None
	k = id_line + 1
	while k < end and not lines[k].startswith("      - ") and lines[k] != "        run: |":
		k += 1
	if not (k < end and lines[k] == "        run: |"):
		raise AssertionError(f"step {step_id!r} in job {job!r} has no literal run block")
	body = []
	for line in lines[k + 1:end]:
		if line.strip() and not line.startswith(" " * 10):
			break
		body.append(line[10:])
	while body and not body[-1].strip():
		body.pop()
	return "\n".join(body) + "\n"


def _step_script(step_id: str, job: str = JOB) -> str:
	if yaml is None:
		return _step_script_from_text(step_id, job)
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
		# The fakes are shell functions, not PATH executables: the pre-push
		# gate container mounts the temp dir noexec. Each stub is a file read
		# by its interpreter, which noexec does not block.
		stub_gh = self.tmp / "stub_gh.py"
		stub_gh.write_text(STUB_GH, encoding="utf-8")
		stub_date = self.tmp / "stub_date.sh"
		stub_date.write_text(STUB_DATE, encoding="utf-8")
		self.output = self.tmp / "github_output"
		self.output.write_text("", encoding="utf-8")
		self.gh_log = self.tmp / "gh.log"
		self.env = {
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"STUB_PYTHON": sys.executable,
			"STUB_GH_PY": str(stub_gh),
			"STUB_DATE_SH": str(stub_date),
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
			["bash", "-c", STUB_FAKES + _step_script(step_id, job)],
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

	def _plan_reads(self) -> int:
		if not self.gh_log.exists():
			return 0
		return sum(1 for line in self.gh_log.read_text(encoding="utf-8").splitlines() if "plan.yml/runs?" in line)

	def test_create_step_rate_limited_pre_read_writes_numeric_outputs(self) -> None:
		result = self._run("create", STUB_RUNS_FAIL_COUNT="-1", STUB_ISSUE_OUT="9782\n")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self._assert_outputs_numeric()
		self.assertIn("pre_clarify_run_id=0", self._outputs())
		self.assertIn("pre_clarify_run_id_read_failed=1", self._outputs())
		self.assertIn("issue_number=9782", self._outputs())
		self.assertIn("::warning::Could not read the latest clarify.yml run id", result.stdout)
		self.assertNotIn('"message":', result.stdout + result.stderr)

	def test_create_step_failed_pre_read_with_empty_stdout_sets_read_failed(self) -> None:
		# A failed read that prints nothing (e.g. a network error) must not
		# pass as a real 0 baseline.
		result = self._run(
			"create",
			STUB_RUNS_FAIL_COUNT="-1",
			STUB_RATE_LIMIT_BODY="",
			STUB_ISSUE_OUT="9782\n",
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self._assert_outputs_numeric()
		self.assertIn("pre_clarify_run_id=0", self._outputs())
		self.assertIn("pre_clarify_run_id_read_failed=1", self._outputs())

	def test_alt_create_step_failed_read_with_empty_stdout_leaves_baseline_empty(self) -> None:
		result = self._run(
			"create",
			job=ALT_JOB,
			ALT_EDITOR_MODEL="test/model",
			STUB_RUNS_FAIL_COUNT="-1",
			STUB_RATE_LIMIT_BODY="",
			STUB_ISSUE_OUT="9790\n",
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		for wf in ("clarify", "plan", "implement", "review_autofix"):
			self.assertIn(f"pre_{wf}_run_id=", self._outputs())
			self.assertNotIn(f"pre_{wf}_run_id=0", self._outputs())

	def test_create_step_happy_path_keeps_run_id(self) -> None:
		result = self._run(
			"create",
			STUB_RUNS_PAYLOAD='{"workflow_runs": [{"id": 123}]}',
			STUB_ISSUE_OUT="9782\n",
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		# created_after_epoch (issue #10037) scopes the gate-cancel step; the
		# fake `date +%s` starts at 1000.
		self.assertEqual(self._outputs(), ["pre_clarify_run_id=123", "created_after_epoch=1000", "issue_number=9782"])

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
		# The step also writes non-numeric `created_after=` and `title=`, so
		# every line must be a `key=value` line (a leaked multi-line error
		# body would add lines without one), and only the run-id and issue
		# number keys guarded by #9797 must hold digits or nothing.
		for line in outputs:
			self.assertRegex(line, r"^[a-z_]+=")
			if line.startswith("pre_") or line.startswith("issue_number="):
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
		# The empty-baseline case returns an older run (id 42) on a successful
		# clarify.yml or plan.yml read, so it fails if either skip guard is
		# removed and the poll anchors at `id > 0`. The rate-limited case fails
		# every read.
		older_run = '{"workflow_runs": [{"id": 42, "created_at": "2026-11-19T09:16:00Z"}]}'
		cases = (
			("100", {"STUB_RUNS_FAIL_COUNT": "-1"}),
			("", {"STUB_RUNS_FAIL_COUNT": "0", "STUB_RUNS_PAYLOAD": older_run}),
		)
		for pre_clarify, stub in cases:
			with self.subTest(pre_clarify=pre_clarify):
				self.output.write_text("", encoding="utf-8")
				if self.gh_log.exists():
					self.gh_log.unlink()
				result = self._run(
					"capture_alt_runs",
					job=ALT_JOB,
					ISSUE_NUMBER="9790",
					PRE_CLARIFY=pre_clarify,
					PRE_PLAN="",
					PRE_IMPLEMENT="",
					PRE_REVIEW="",
					**stub,
				)
				self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
				self.assertEqual(self._outputs(), [])
				self.assertNotIn('"message":', result.stdout + result.stderr)
				if pre_clarify == "":
					self.assertIn("alt-model clarify baseline run id unavailable", result.stdout)
					self.assertEqual(self._runs_reads(), 0)
					self.assertIn("alt-model plan baseline run id unavailable", result.stdout)
					self.assertEqual(self._plan_reads(), 0)


if __name__ == "__main__":
	unittest.main()

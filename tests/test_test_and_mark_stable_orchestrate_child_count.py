#!/usr/bin/env python3
"""Regression checks for the child-issue count in the orchestrate-decompose smoke step.

BUG-001 (analysis 2026-09-26-3): the `dispatch` step of the
`orchestrate-decompose-test` job runs under `set -euo pipefail`, and the
child-issue poll computed `CHILD_COUNT` with a bare `grep -c .`. On an empty
poll `grep -c` prints 0 and exits 1, which ended the step before the 90s
polling loop and its stderr diagnostics could run.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

BLOCK_START = 'CHILDREN=""'
BLOCK_END = 'echo "child_issues=${CHILDREN}" >> "$GITHUB_OUTPUT"'
OLD_COUNT_LINE = "CHILD_COUNT=$(echo \"${CHILDREN}\" | tr ',' '\\n' | grep -c .)"

# Stub `gh`: prints the next payload (the post-`--jq` result) from the
# newline-separated STUB_GH_PAYLOADS file and logs each call.
STUB_GH = """#!/usr/bin/env bash
echo call >> "${STUB_GH_LOG}"
n=$(wc -l < "${STUB_GH_LOG}" | tr -d ' ')
sed -n "${n}p" "${STUB_GH_PAYLOADS}"
"""

STUB_SLEEP = """#!/usr/bin/env bash
echo "sleep $*" >> "${STUB_SLEEP_LOG}"
"""

STUB_DATE = """#!/usr/bin/env bash
if [ "${1:-}" = "+%s" ]; then
	echo 1000
	exit 0
fi
exec "${REAL_DATE}" "$@"
"""


def _step_run() -> str:
	data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	steps = data["jobs"]["orchestrate-decompose-test"]["steps"]
	for step in steps:
		if step.get("id") == "dispatch":
			return step["run"]
	raise AssertionError("dispatch step not found in orchestrate-decompose-test")


def _child_poll_block(run: str) -> str:
	lines = run.splitlines()
	start = next((i for i, line in enumerate(lines) if line.strip() == BLOCK_START), None)
	assert start is not None, "child-poll block start anchor missing"
	end = next((i for i in range(start, len(lines)) if lines[i].strip() == BLOCK_END), None)
	assert end is not None, "child-poll block end anchor missing"
	return "\n".join(lines[start : end + 1]) + "\n"


def _count_lines(block: str) -> list[str]:
	return [line.strip() for line in block.splitlines() if line.strip().startswith("CHILD_COUNT=$(")]


class ChildCountStaticTests(unittest.TestCase):
	def test_count_line_does_not_use_bare_grep_c(self) -> None:
		block = _child_poll_block(_step_run())
		count_lines = _count_lines(block)
		self.assertEqual(len(count_lines), 1, count_lines)
		line = count_lines[0]
		if "grep -c" in line:
			self.assertIn("|| true", line)
		self.assertIn("CHILDREN_JSON", line)
		self.assertIn("unique | length", line)
		self.assertIn("*[!0-9]*) CHILD_COUNT=0", block)

	def test_unchanged_poll_contract(self) -> None:
		run = _step_run()
		block = _child_poll_block(run)
		self.assertIn("+ 90 ))", block)
		self.assertIn("sleep 5", block)
		self.assertIn('[ "${CHILD_COUNT}" -ge 2 ] && break', block)
		self.assertIn('sort | unique | join(",")', block)
		self.assertIn(BLOCK_END, block)
		self.assertEqual(block.count("gh api"), 1)
		self.assertIn("last gh api stderr during child-issue polling", run)
		self.assertIn("expected >= 2", run)


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class ChildCountBehaviourTests(unittest.TestCase):
	def _run(self, payloads: list[str], block: str | None = None) -> tuple[subprocess.CompletedProcess, Path]:
		if block is None:
			block = _child_poll_block(_step_run())
		tmp = Path(tempfile.mkdtemp())
		self.addCleanup(shutil.rmtree, tmp, True)
		bindir = tmp / "bin"
		bindir.mkdir()
		for name, body in (("gh", STUB_GH), ("sleep", STUB_SLEEP), ("date", STUB_DATE)):
			path = bindir / name
			path.write_text(body, encoding="utf-8")
			path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
		(tmp / "payloads").write_text("\n".join(payloads) + "\n", encoding="utf-8")
		(tmp / "output").write_text("", encoding="utf-8")
		prelude = (
			"set -euo pipefail\n"
			"TEST_REPO=owner/repo\n"
			"DISPATCH_START_ISO=2026-01-01T00:00:00Z\n"
			"TRACKING_NUMBER=100\n"
			f"GITHUB_OUTPUT={tmp / 'output'}\n"
		)
		script = tmp / "poll.sh"
		script.write_text(prelude + block + 'echo "FINAL_COUNT=${CHILD_COUNT}"\n', encoding="utf-8")
		env = dict(os.environ)
		env.update(
			{
				"PATH": f"{bindir}{os.pathsep}{env.get('PATH', '')}",
				"STUB_GH_LOG": str(tmp / "gh.log"),
				"STUB_GH_PAYLOADS": str(tmp / "payloads"),
				"STUB_SLEEP_LOG": str(tmp / "sleep.log"),
				"REAL_DATE": shutil.which("date") or "/bin/date",
			}
		)
		proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
		return proc, tmp

	@staticmethod
	def _lines(path: Path) -> list[str]:
		return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

	def test_empty_poll_then_two_children(self) -> None:
		proc, tmp = self._run(["[]", "[102,101]"])
		self.assertEqual(proc.returncode, 0, proc.stderr)
		self.assertEqual(len(self._lines(tmp / "gh.log")), 2)
		self.assertEqual(len(self._lines(tmp / "sleep.log")), 1)
		self.assertIn("child_issues=101,102", self._lines(tmp / "output"))
		self.assertIn("FINAL_COUNT=2", proc.stdout)

	def test_malformed_poll_then_duplicate_children(self) -> None:
		proc, tmp = self._run(["not-json", "[7,7,8]"])
		self.assertEqual(proc.returncode, 0, proc.stderr)
		self.assertEqual(len(self._lines(tmp / "gh.log")), 2)
		self.assertIn("child_issues=7,8", self._lines(tmp / "output"))
		self.assertIn("FINAL_COUNT=2", proc.stdout)

	def test_old_grep_count_aborts_on_empty_poll(self) -> None:
		block = _child_poll_block(_step_run())
		lines = block.splitlines()
		idx = next(i for i, line in enumerate(lines) if line.strip().startswith("CHILD_COUNT=$("))
		indent = lines[idx][: len(lines[idx]) - len(lines[idx].lstrip())]
		lines[idx] = indent + OLD_COUNT_LINE
		old_block = "\n".join(lines) + "\n"
		proc, tmp = self._run(["[]", "[102,101]"], block=old_block)
		self.assertNotEqual(proc.returncode, 0)
		self.assertEqual(len(self._lines(tmp / "gh.log")), 1)
		self.assertFalse(any(line.startswith("child_issues=") for line in self._lines(tmp / "output")))


if __name__ == "__main__":
	unittest.main()

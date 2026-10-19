#!/usr/bin/env python3
"""Contract test for the `Install actionlint` step of `.github/workflows/ci.yml`.

CI run 35615284953 failed in 82 seconds because one `curl` of the pinned
actionlint release hit a transient HTTP 504. The download now retries a
bounded number of times. These tests pin that the step:

  1. retries transient errors (`--retry-all-errors`, bounded `--retry`);
  2. bounds each attempt (`--max-time`, `--connect-timeout`);
  3. still fails on HTTP errors (`-sSLf`);
  4. still verifies the sha256 checksum after the download;
  5. keeps the version and checksum pinned.

Run directly: `python3 tests/test_ci_actionlint_install_retry.py`.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JOB_ID = "static-checks"
STEP_NAME = "Install actionlint"


def _step() -> dict:
	workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
	jobs = workflow.get("jobs") or {}
	job = jobs.get(JOB_ID)
	if not isinstance(job, dict):
		raise AssertionError(f"job {JOB_ID!r} missing in {CI_WORKFLOW}")
	for step in job.get("steps") or []:
		if isinstance(step, dict) and step.get("name") == STEP_NAME:
			return step
	raise AssertionError(f"step {STEP_NAME!r} missing in job {JOB_ID!r}")


def _logical_lines(run: str) -> list[str]:
	lines: list[str] = []
	buffer = ""
	for raw in run.splitlines():
		stripped = raw.strip()
		if stripped.endswith("\\"):
			buffer += stripped[:-1] + " "
			continue
		lines.append((buffer + stripped).strip())
		buffer = ""
	if buffer:
		lines.append(buffer.strip())
	return lines


def _curl_index_and_line(lines: list[str]) -> tuple[int, str]:
	matches = [
		(index, line)
		for index, line in enumerate(lines)
		if line.startswith("curl ") and "actionlint/releases/download" in line
	]
	if len(matches) != 1:
		raise AssertionError(f"expected one actionlint curl line, found {len(matches)}")
	return matches[0]


class ActionlintInstallRetryContract(unittest.TestCase):
	def setUp(self) -> None:
		self.step = _step()
		self.lines = _logical_lines(str(self.step.get("run") or ""))
		self.curl_index, self.curl = _curl_index_and_line(self.lines)
		self.tokens = self.curl.split()

	def _flag_value(self, flag: str) -> str:
		self.assertIn(flag, self.tokens, f"{flag} missing from actionlint curl")
		position = self.tokens.index(flag)
		self.assertLess(position + 1, len(self.tokens), f"{flag} has no value")
		return self.tokens[position + 1]

	def test_curl_retries_transient_errors(self) -> None:
		self.assertIn("--retry-all-errors", self.tokens)
		value = self._flag_value("--retry")
		self.assertRegex(value, r"^\d+$")
		self.assertGreaterEqual(int(value), 1)
		self.assertLessEqual(int(value), 5)

	def test_curl_has_bounded_max_time(self) -> None:
		value = self._flag_value("--max-time")
		self.assertRegex(value, r"^\d+$")
		self.assertGreater(int(value), 0)
		self.assertIn("--connect-timeout", self.tokens)

	def test_curl_keeps_fail_flags(self) -> None:
		self.assertIn("-sSLf", self.tokens)

	def test_checksum_verified_after_download(self) -> None:
		checksum = [
			index
			for index, line in enumerate(self.lines)
			if "sha256sum -c -" in line and "ACTIONLINT_SHA256" in line
		]
		self.assertTrue(checksum, "sha256sum -c - check missing")
		self.assertTrue(
			any(index > self.curl_index for index in checksum),
			"sha256sum check must come after the download",
		)

	def test_version_and_sha_pinned(self) -> None:
		env = self.step.get("env") or {}
		self.assertRegex(str(env.get("ACTIONLINT_VERSION", "")), r"^\d+\.\d+\.\d+$")
		self.assertRegex(str(env.get("ACTIONLINT_SHA256", "")), r"^[0-9a-f]{64}$")
		self.assertIn("v${ACTIONLINT_VERSION}", self.curl)
		self.assertIsNone(re.search(r"\blatest\b", self.curl))


if __name__ == "__main__":
	unittest.main()

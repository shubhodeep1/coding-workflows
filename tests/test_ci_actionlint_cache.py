#!/usr/bin/env python3
"""Contract test for the actionlint tarball cache in `.github/workflows/ci.yml`.

The actionlint release download from github.com returned HTTP 504 in CI runs
35615284953, 42465389350 and 42470227946; the last outage outlasted the whole
retry window (#9056). The `static-checks` job now caches the release tarball
with `actions/cache@v5`, so most runs skip the download. These tests pin that:

  1. the cache step uses `actions/cache@v5`, comes before `Install actionlint`,
     keys on both `ACTIONLINT_VERSION` and `ACTIONLINT_SHA256`, and has no
     `restore-keys` (exact match only);
  2. the job-level env copies match the Install step's pinned values;
  3. the sha256 check runs on every run, hit or miss, and only `curl` is
     gated on a cache miss;
  4. the tarball is not deleted, so the post-job cache save can store it;
  5. the install runs inside the cached directory;
  6. the curl retry flags keep today's exact values.

Run directly: `python3 tests/test_ci_actionlint_cache.py`.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

try:
	import yaml
except ModuleNotFoundError:
	# The credential-free pre-push gate image has no PyYAML; _job then reads
	# the job from the workflow text with the small parser below instead.
	yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JOB_ID = "static-checks"
INSTALL_STEP_NAME = "Install actionlint"
CACHE_STEP_ID = "actionlint-cache"


_FALLBACK_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):(?:\s+(.*))?$")


def _fallback_scalar(value: str) -> str:
	value = value.strip()
	if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
		return value[1:-1]
	return value


def _fallback_indent(line: str) -> int:
	return len(line) - len(line.lstrip(" "))


def _fallback_mapping(lines: list[str], start: int, indent: int) -> tuple[dict, int]:
	"""Read `key: value` lines at exactly `indent`; return (mapping, next index)."""
	mapping: dict = {}
	index = start
	while index < len(lines):
		line = lines[index]
		stripped = line.strip()
		if not stripped or stripped.startswith("#"):
			index += 1
			continue
		if _fallback_indent(line) < indent:
			break
		match = _FALLBACK_KEY_RE.match(stripped)
		if _fallback_indent(line) == indent and match:
			mapping[match.group(1)] = _fallback_scalar(match.group(2) or "")
		index += 1
	return mapping, index


def _fallback_block(lines: list[str], start: int, indent: int) -> tuple[str, int]:
	"""Read a `|` / `>` block scalar whose body sits at `indent` or deeper."""
	body: list[str] = []
	index = start
	while index < len(lines):
		line = lines[index]
		if line.strip() and _fallback_indent(line) < indent:
			break
		body.append(line[indent:] if line.strip() else "")
		index += 1
	while body and not body[-1]:
		body.pop()
	return "\n".join(body) + "\n", index


def _fallback_job(text: str) -> dict | None:
	"""Parse the `static-checks` job from ci.yml text without PyYAML.

	Covers only the shapes this test reads: job-level `env:`, and per step the
	scalar keys plus `with:` / `env:` mappings and `run:` block scalars.
	"""
	lines = text.splitlines()
	try:
		start = lines.index(f"  {JOB_ID}:") + 1
	except ValueError:
		return None
	end = start
	while end < len(lines):
		line = lines[end]
		if line.strip() and not line.lstrip().startswith("#") and _fallback_indent(line) <= 2:
			break
		end += 1
	lines = lines[start:end]
	job: dict = {}
	index = 0
	while index < len(lines):
		line = lines[index]
		stripped = line.strip()
		if _fallback_indent(line) == 4 and stripped == "env:":
			job["env"], index = _fallback_mapping(lines, index + 1, 6)
			continue
		if _fallback_indent(line) == 4 and stripped == "steps:":
			job["steps"], index = _fallback_steps(lines, index + 1)
			continue
		index += 1
	return job


def _fallback_steps(lines: list[str], start: int) -> tuple[list, int]:
	steps: list = []
	step: dict | None = None
	index = start
	while index < len(lines):
		line = lines[index]
		stripped = line.strip()
		if not stripped or stripped.startswith("#"):
			index += 1
			continue
		indent = _fallback_indent(line)
		if indent < 6:
			break
		if indent == 6 and stripped.startswith("- "):
			step = {}
			steps.append(step)
			stripped = stripped[2:].strip()
		elif indent != 8 or step is None:
			index += 1
			continue
		match = _FALLBACK_KEY_RE.match(stripped)
		index += 1
		if not match:
			continue
		key, value = match.group(1), (match.group(2) or "").strip()
		if value[:1] in ("|", ">"):
			step[key], index = _fallback_block(lines, index, 10)
		elif not value:
			step[key], index = _fallback_mapping(lines, index, 10)
		else:
			step[key] = _fallback_scalar(value)
	return steps, index


def _job() -> dict:
	text = CI_WORKFLOW.read_text(encoding="utf-8")
	if yaml is not None:
		workflow = yaml.safe_load(text)
		jobs = workflow.get("jobs") or {}
		job = jobs.get(JOB_ID)
	else:
		job = _fallback_job(text)
	if not isinstance(job, dict):
		raise AssertionError(f"job {JOB_ID!r} missing in {CI_WORKFLOW}")
	return job


def _step_index(steps: list, predicate) -> int:
	for index, step in enumerate(steps):
		if isinstance(step, dict) and predicate(step):
			return index
	return -1


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


def _single_index(lines: list[str], predicate, label: str) -> int:
	matches = [index for index, line in enumerate(lines) if predicate(line)]
	if len(matches) != 1:
		raise AssertionError(f"expected one {label} line, found {len(matches)}")
	return matches[0]


class ActionlintTarballCacheContract(unittest.TestCase):
	def setUp(self) -> None:
		self.job = _job()
		self.steps = self.job.get("steps") or []
		self.install_index = _step_index(
			self.steps, lambda step: step.get("name") == INSTALL_STEP_NAME
		)
		self.assertGreaterEqual(self.install_index, 0, f"{INSTALL_STEP_NAME!r} step missing")
		self.install = self.steps[self.install_index]
		self.lines = _logical_lines(str(self.install.get("run") or ""))

	def _curl_index(self) -> int:
		return _single_index(
			self.lines,
			lambda line: line.startswith("curl ") and "actionlint/releases/download" in line,
			"actionlint curl",
		)

	def test_cache_step_uses_actions_cache_v5(self) -> None:
		cache_index = _step_index(self.steps, lambda step: step.get("id") == CACHE_STEP_ID)
		self.assertGreaterEqual(cache_index, 0, f"step id {CACHE_STEP_ID!r} missing")
		cache = self.steps[cache_index]
		self.assertEqual(cache.get("uses"), "actions/cache@v5")
		self.assertLess(cache_index, self.install_index, "cache step must run before the install")
		with_block = cache.get("with") or {}
		key = str(with_block.get("key") or "")
		self.assertIn("env.ACTIONLINT_VERSION", key)
		self.assertIn("env.ACTIONLINT_SHA256", key)
		self.assertIn("ACTIONLINT_CACHE_DIR", str(with_block.get("path") or ""))
		self.assertNotIn("restore-keys", with_block)

	def test_job_env_matches_install_step_env(self) -> None:
		job_env = self.job.get("env") or {}
		step_env = self.install.get("env") or {}
		for name in ("ACTIONLINT_VERSION", "ACTIONLINT_SHA256"):
			self.assertIn(name, job_env, f"job-level {name} missing")
			self.assertEqual(str(job_env[name]), str(step_env.get(name)), f"{name} copies differ")
		cache_dir = str(job_env.get("ACTIONLINT_CACHE_DIR") or "")
		self.assertTrue(os.path.isabs(cache_dir), "ACTIONLINT_CACHE_DIR must be an absolute path")

	def test_checksum_not_gated_on_cache_miss(self) -> None:
		self.assertNotIn("if", self.install, "Install actionlint must not be conditional")
		if_index = _single_index(
			self.lines, lambda line: line.startswith('if [ -s "${tarball}" ]'), "cache-hit if"
		)
		else_index = _single_index(self.lines, lambda line: line == "else", "else")
		# The download branch has its own nested if/fi blocks, so `fi` is not
		# unique: find the one that closes the cache-hit `if` by nesting depth.
		fi_index = -1
		depth = 0
		for index in range(if_index, len(self.lines)):
			line = self.lines[index]
			if line.startswith("if "):
				depth += 1
			elif line == "fi":
				depth -= 1
				if depth == 0:
					fi_index = index
					break
		self.assertGreaterEqual(fi_index, 0, "cache-hit if has no matching fi")
		curl_index = self._curl_index()
		self.assertLess(if_index, else_index)
		self.assertLess(else_index, curl_index)
		self.assertLess(curl_index, fi_index)
		checksum_index = _single_index(
			self.lines,
			lambda line: "sha256sum -c -" in line and "ACTIONLINT_SHA256" in line,
			"sha256sum",
		)
		self.assertGreater(checksum_index, fi_index, "sha256sum must run after the if/fi block")
		open_blocks = sum(1 for line in self.lines[:checksum_index] if line.startswith("if ")) - sum(
			1 for line in self.lines[:checksum_index] if line == "fi"
		)
		self.assertEqual(open_blocks, 0, "sha256sum must not sit inside any if block")
		tar_index = _single_index(self.lines, lambda line: line.startswith("tar -xzf"), "tar")
		self.assertGreater(tar_index, checksum_index, "extraction must follow verification")

	def test_tarball_not_deleted(self) -> None:
		for line in self.lines:
			if line.startswith("rm ") or line == "rm":
				self.assertNotIn("${tarball}", line, "tarball must stay for the cache save")

	def test_install_runs_in_cache_dir(self) -> None:
		cd_index = _single_index(
			self.lines, lambda line: line == 'cd "${ACTIONLINT_CACHE_DIR}"', "cd cache dir"
		)
		tarball_index = _single_index(
			self.lines, lambda line: line.startswith("tarball="), "tarball assignment"
		)
		self.assertLess(cd_index, tarball_index)

	def test_retry_flags_unchanged(self) -> None:
		tokens = self.lines[self._curl_index()].split()
		self.assertIn("-sSLf", tokens)
		self.assertIn("--retry-all-errors", tokens)
		for flag, value in (
			("--retry", "5"),
			("--retry-delay", "15"),
			("--retry-max-time", "300"),
			("--connect-timeout", "20"),
			("--max-time", "120"),
		):
			self.assertIn(flag, tokens, f"{flag} missing from actionlint curl")
			position = tokens.index(flag)
			self.assertLess(position + 1, len(tokens), f"{flag} has no value")
			self.assertEqual(tokens[position + 1], value, f"{flag} changed")


if __name__ == "__main__":
	unittest.main()

#!/usr/bin/env python3
"""Target-files guard in scripts/check_failure_triage.sh (issue #9733, R1c).

With CHECK_TRIAGE_PR_BRANCH_ROUTING_ENABLED on, a check-failure triage issue
names the failing PR's head as its "Target branch". When that head has none of
the failing test files the CI log names but the default branch has at least
one, a fix planned on the head cannot touch the failing code, so the collect
stage clears the route and the issue is planned on the default branch.
CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED (default 'true') set to 'false' keeps
today's route.

These cases run the collect stage against the fake ``gh`` of
tests/test_check_failure_triage_workflow_security.py. The stage runs from a
scratch support root whose scripts/ links the real helpers, except the
check-run context collector, which is replaced by a stub that writes a fixed
CI log; the fake ``gh`` also answers the guard's one GraphQL read.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
_HARNESS_MODULE_NAME = "check_triage_target_files_guard_harness"
# Repo-pinning git variables (see tests/conftest.py): stripped while a case
# runs so every git call resolves the scratch repository it names.
_GIT_PIN_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")
_GRAPHQL_ROUTE = """  "api graphql "*)
    if [ -n "${MOCK_GRAPHQL_FAIL:-}" ]; then echo 'gh: Server Error (HTTP 502)' >&2; exit 1; fi
    printf '%s\\n' "${MOCK_GRAPHQL_RESPONSE:-}" ;;
"""
_COLLECTOR_STUB = """#!/usr/bin/env python3
import os
from pathlib import Path
Path(os.environ["PR_CHECK_RUNS_CONTEXT_FILE"]).write_text(Path(os.environ["MOCK_TRIAGE_CONTEXT_FILE"]).read_text(encoding="utf-8"), encoding="utf-8")
"""
FAILING_FILE = "tests/test_guard_target.py"
PRESENT_FILE = "tests/test_present_on_head.py"
ROUTING_BASE_REPO = {"base": {"ref": "main", "repo": {"full_name": "owner/repo", "default_branch": "main"}}}
FOUND = json.dumps({"data": {"repository": {"f0": {"__typename": "Blob"}}}})
MISSING = json.dumps({"data": {"repository": {"f0": None}}})


def _harness():
	"""The security test module, loaded once under a name that skips its own
	collection of sibling files (no import cycle)."""
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


def _git(repo: Path, *args: str) -> str:
	env = {key: value for key, value in os.environ.items() if key not in _GIT_PIN_VARS}
	env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
	return subprocess.run(
		["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
		check=True, capture_output=True, text=True, env=env,
	).stdout.strip()


class CheckFailureTriageTargetFilesGuardTests(unittest.TestCase):
	def setUp(self) -> None:
		self._saved_git_env = {name: os.environ.pop(name) for name in _GIT_PIN_VARS if name in os.environ}
		self._temp = tempfile.TemporaryDirectory(prefix="check-triage-target-guard-")
		temp_path = Path(self._temp.name)
		# PR-head checkout: one commit that has PRESENT_FILE but not FAILING_FILE.
		self.checkout = temp_path / "checkout"
		self.checkout.mkdir()
		_git(self.checkout, "init", "-q")
		(self.checkout / "tests").mkdir()
		(self.checkout / PRESENT_FILE).write_text("def test_ok():\n\tpass\n", encoding="utf-8")
		_git(self.checkout, "add", "-A")
		_git(self.checkout, "commit", "-q", "-m", "head")
		self.head_sha = _git(self.checkout, "rev-parse", "HEAD")
		# Support root: the real helpers plus a stub context collector.
		self.support_root = temp_path / "support"
		scripts_dir = self.support_root / "scripts"
		scripts_dir.mkdir(parents=True)
		for entry in (REPO_ROOT / "scripts").iterdir():
			if entry.name != "collect_pr_check_runs_context.py":
				(scripts_dir / entry.name).symlink_to(entry)
		(scripts_dir / "collect_pr_check_runs_context.py").write_text(_COLLECTOR_STUB, encoding="utf-8")
		self.context_file = temp_path / "context.txt"
		self.set_context(f"2026-11-18T10:00:00.000Z FAILED {FAILING_FILE}::test_x - AssertionError\n")
		harness = _harness()
		self._saved_root = harness.REPO_ROOT
		self._saved_writer = harness._write_executable
		harness.REPO_ROOT = self.support_root

		def write_with_graphql(path: Path, body: str) -> None:
			if path.name == "gh":
				body = body.replace('case "$*" in\n', 'case "$*" in\n' + _GRAPHQL_ROUTE, 1)
			self._saved_writer(path, body)

		harness._write_executable = write_with_graphql

	def tearDown(self) -> None:
		harness = _harness()
		harness.REPO_ROOT = self._saved_root
		harness._write_executable = self._saved_writer
		self._temp.cleanup()
		os.environ.update(self._saved_git_env)

	def set_context(self, text: str) -> None:
		self.context_file.write_text(text, encoding="utf-8")

	def run_collect(self, extra: dict[str, str] | None = None, *, guard_flag: str | None = None, checkout: str | None = None):
		env = {
			"CHECK_TRIAGE_PR_BRANCH_ROUTING_ENABLED": "true",
			"CHECK_TRIAGE_HEAD_SHA": self.head_sha,
			"CHECK_TRIAGE_TARGET_FILES_GUARD_CHECKOUT_DIR": str(self.checkout) if checkout is None else checkout,
			"MOCK_TRIAGE_CONTEXT_FILE": str(self.context_file),
			"MOCK_GRAPHQL_RESPONSE": FOUND,
			"CHECK_RUNS_AUTOFIX_ENABLED": "true",
		}
		env.update(extra or {})
		os.environ.pop("CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED", None)
		if guard_flag is not None:
			env["CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED"] = guard_flag
		calls: list[str] = []
		proc, outputs, metadata = _harness()._run_collect_stage(
			parent_body="plain issue\n", pr_payload_overrides=ROUTING_BASE_REPO, pr_head_sha=self.head_sha,
			extra_env=env, calls_path=calls,
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(outputs.get("ready"), "true", proc.stdout + proc.stderr)
		self.assertIn("CHECK_TRIAGE routing outcome=emit target_branch=ai/issue-41", proc.stdout)
		graphql_calls = [call for call in calls if call.startswith("api graphql ")]
		return proc, metadata, graphql_calls

	def test_route_falls_back_when_head_lacks_files_the_default_branch_has(self) -> None:
		proc, metadata, graphql_calls = self.run_collect()
		self.assertIn(
			"CHECK_TRIAGE routing outcome=fallback reason=target_branch_lacks_files target_branch=main from_branch=ai/issue-41 files=1",
			proc.stdout,
		)
		self.assertEqual(metadata.get("routing_target_branch"), "")
		self.assertEqual(len(graphql_calls), 1)
		# The path reaches GraphQL only as a variable, never inside the query text.
		self.assertIn(f"-f e0=main:{FAILING_FILE}", graphql_calls[0])
		self.assertIn("-f owner=owner -f name=repo", graphql_calls[0])
		query = graphql_calls[0].split("-f query=", 1)[1].split(" -f owner=", 1)[0]
		self.assertNotIn(FAILING_FILE, query)
		self.assertIn("f0: object(expression: $e0) { __typename }", query)

	def test_unset_switch_is_on(self) -> None:
		proc, metadata, _calls = self.run_collect(guard_flag=None)
		self.assertIn("reason=target_branch_lacks_files", proc.stdout)
		self.assertEqual(metadata.get("routing_target_branch"), "")
		for value in ("", "TRUE", "True"):
			with self.subTest(value=value):
				proc, metadata, _calls = self.run_collect(guard_flag=value)
				self.assertIn("reason=target_branch_lacks_files", proc.stdout)

	def test_files_on_head_keep_the_route_without_a_lookup(self) -> None:
		self.set_context(f"FAILED {PRESENT_FILE}::test_ok - AssertionError\nFAILED {FAILING_FILE}::test_x - AssertionError\n")
		proc, metadata, graphql_calls = self.run_collect()
		self.assertIn("CHECK_TRIAGE routing outcome=keep reason=target_branch_has_files target_branch=ai/issue-41 files=2", proc.stdout)
		self.assertEqual(metadata.get("routing_target_branch"), "ai/issue-41")
		self.assertEqual(graphql_calls, [])

	def test_files_missing_on_default_keep_the_route(self) -> None:
		proc, metadata, graphql_calls = self.run_collect({"MOCK_GRAPHQL_RESPONSE": MISSING})
		self.assertIn("CHECK_TRIAGE routing outcome=keep reason=files_missing_on_default target_branch=ai/issue-41", proc.stdout)
		self.assertEqual(metadata.get("routing_target_branch"), "ai/issue-41")
		self.assertEqual(len(graphql_calls), 1)

	def test_graphql_failure_keeps_the_route(self) -> None:
		for extra in ({"MOCK_GRAPHQL_FAIL": "1"}, {"MOCK_GRAPHQL_RESPONSE": "not json"}, {"MOCK_GRAPHQL_RESPONSE": '{"errors": [{"message": "x"}]}'}):
			with self.subTest(extra=extra):
				proc, metadata, _calls = self.run_collect(extra)
				self.assertIn("CHECK_TRIAGE routing outcome=keep reason=default_lookup_failed target_branch=ai/issue-41", proc.stdout)
				self.assertEqual(metadata.get("routing_target_branch"), "ai/issue-41")

	def test_switch_off_keeps_the_route_and_makes_no_lookup(self) -> None:
		for value in ("false", "FALSE", "bogus"):
			with self.subTest(value=value):
				proc, metadata, graphql_calls = self.run_collect(guard_flag=value)
				self.assertIn("CHECK_TRIAGE routing outcome=keep reason=target_files_guard_disabled target_branch=ai/issue-41", proc.stdout)
				self.assertNotIn("reason=target_branch_lacks_files", proc.stdout)
				self.assertEqual(metadata.get("routing_target_branch"), "ai/issue-41")
				self.assertEqual(graphql_calls, [])

	def test_invalid_or_missing_paths_keep_the_route(self) -> None:
		for context in ("all checks passed\n", "FAILED tests/../etc/passwd.py::test_x - boom\n", "FAILED scripts/x.py::test_x - boom\n"):
			with self.subTest(context=context):
				self.set_context(context)
				proc, metadata, graphql_calls = self.run_collect()
				self.assertIn("CHECK_TRIAGE routing outcome=keep reason=no_failing_files target_branch=ai/issue-41", proc.stdout)
				self.assertEqual(metadata.get("routing_target_branch"), "ai/issue-41")
				self.assertEqual(graphql_calls, [])

	def test_missing_head_commit_keeps_the_route(self) -> None:
		other = Path(self._temp.name) / "empty"
		other.mkdir()
		_git(other, "init", "-q")
		for checkout in (str(other), str(Path(self._temp.name) / "absent"), ""):
			with self.subTest(checkout=checkout):
				extra = {"GITHUB_WORKSPACE": ""} if checkout == "" else None
				proc, metadata, graphql_calls = self.run_collect(extra, checkout=checkout)
				self.assertIn("CHECK_TRIAGE routing outcome=keep reason=head_commit_unavailable target_branch=ai/issue-41", proc.stdout)
				self.assertEqual(metadata.get("routing_target_branch"), "ai/issue-41")
				self.assertEqual(graphql_calls, [])

	def test_no_route_means_no_guard(self) -> None:
		proc, outputs, metadata = _harness()._run_collect_stage(
			parent_body="plain issue\n", pr_payload_overrides=ROUTING_BASE_REPO, pr_head_sha=self.head_sha,
			extra_env={"CHECK_TRIAGE_HEAD_SHA": self.head_sha, "MOCK_TRIAGE_CONTEXT_FILE": str(self.context_file), "CHECK_RUNS_AUTOFIX_ENABLED": "true"},
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("CHECK_TRIAGE routing outcome=skip reason=disabled", proc.stdout)
		self.assertNotIn("reason=target_branch_lacks_files", proc.stdout)
		self.assertNotIn("reason=target_files_guard_disabled", proc.stdout)
		self.assertEqual(metadata.get("routing_target_branch"), "")

	def test_switch_defaults_and_workflow_wiring(self) -> None:
		script = (REPO_ROOT / "scripts" / "check_failure_triage.sh").read_text(encoding="utf-8")
		self.assertIn('"${CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED:-}"', script)
		self.assertIn("TARGET_FILES_GUARD_ENABLED=true\n", script)
		self.assertIn('TARGET_FILES_GUARD_CHECKOUT_DIR="${CHECK_TRIAGE_TARGET_FILES_GUARD_CHECKOUT_DIR:-${GITHUB_WORKSPACE:-}}"', script)
		# Runs after the context collector, before the collect-stage metadata.
		self.assertLess(script.index('log "warn context_collector_missing"'), script.index("\ntriage_target_files_guard || true\n"))
		self.assertLess(script.index("\ntriage_target_files_guard || true\n"), script.index('--arg routing_target_branch "${ROUTING_TARGET_BRANCH}"'))
		workflow = (REPO_ROOT / ".github" / "workflows" / "check_failure_triage.yml").read_text(encoding="utf-8")
		self.assertEqual(
			workflow.count("      CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED: ${{ vars.CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED || 'true' }}\n"), 1
		)


if __name__ == "__main__":
	unittest.main()

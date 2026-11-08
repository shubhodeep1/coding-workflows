#!/usr/bin/env python3
"""Contract tests for the `CHECK_TRIAGE op=post stage=<code>` lines (issue #9036).

The `Post check-failure triage issue` step of check_failure_triage.yml logs
exactly one stage line on every exit path, so a failed post is never silent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
	import yaml
except ModuleNotFoundError:  # PyYAML is absent in some credential-free test containers.
	yaml = None


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "check_failure_triage.yml"
POST_STEP_NAME = "Post check-failure triage issue"
STAGE_CODES = ("token_missing", "body_missing", "marker_invalid", "metadata_invalid", "create_failed", "url_missing", "created")
FAILURE_STAGE_CODES = tuple(code for code in STAGE_CODES if code != "created")
FINGERPRINT = "a" * 64
STAGE_LINE_RE = re.compile(r"^CHECK_TRIAGE op=post stage=", re.MULTILINE)


def _post_run_body_without_yaml(text: str) -> str:
	# Fallback when PyYAML is missing: read the step's literal `run: |` block
	# (clip chomping, as YAML does) straight from the workflow text.
	lines = text.splitlines()
	name_line = f"- name: {POST_STEP_NAME}"
	for index, line in enumerate(lines):
		if line.strip() != name_line:
			continue
		step_indent = len(line) - len(line.lstrip()) + 2
		for run_index in range(index + 1, len(lines)):
			candidate = lines[run_index]
			candidate_indent = len(candidate) - len(candidate.lstrip())
			if candidate.strip() and candidate_indent < step_indent:
				break
			if candidate_indent == step_indent and candidate.strip() == "run: |":
				body: list[str] = []
				block_indent = None
				for body_line in lines[run_index + 1:]:
					if not body_line.strip():
						body.append("")
						continue
					body_indent = len(body_line) - len(body_line.lstrip())
					if block_indent is None:
						block_indent = body_indent
					if body_indent < block_indent or body_indent <= step_indent:
						break
					body.append(body_line[block_indent:])
				while body and not body[-1]:
					body.pop()
				return "\n".join(body) + "\n"
	raise AssertionError(f"workflow step not found: {POST_STEP_NAME!r}")


def _post_run_body() -> str:
	text = WORKFLOW_PATH.read_text(encoding="utf-8")
	if yaml is None:
		return _post_run_body_without_yaml(text)
	workflow = yaml.safe_load(text)
	for step in workflow["jobs"]["triage"]["steps"]:
		if step.get("name") == POST_STEP_NAME:
			return step["run"]
	raise AssertionError(f"workflow step not found: {POST_STEP_NAME!r}")


def _write_executable(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(0o755)


class PostStageStaticTests(unittest.TestCase):
	def setUp(self) -> None:
		self.script = _post_run_body()
		self.lines = self.script.splitlines()

	def test_every_stage_code_present(self) -> None:
		for code in STAGE_CODES:
			self.assertIn(f"CHECK_TRIAGE op=post stage={code} pr=%s", self.script, code)

	def test_original_created_line_preserved(self) -> None:
		legacy = "printf 'CHECK_TRIAGE created issue=%s fp=%s gen=%s root=%s pr=%s check=%s\\n'"
		self.assertIn(legacy, self.script)
		self.assertIn(
			'"${triage_issue_url}" "${triage_post_fp}" "${triage_post_gen}" "${triage_post_root}" "${PR_NUMBER}" "${triage_post_check_name}"',
			self.script,
		)
		self.assertLess(self.script.index(legacy), self.script.index("CHECK_TRIAGE op=post stage=created pr=%s"))

	def test_skip_create_path_preserved(self) -> None:
		skip = "CHECK_TRIAGE op=supersede outcome=skip_create rc=%s pr=%s"
		self.assertIn(skip, self.script)
		self.assertRegex(self.script[self.script.index(skip):], r"\A[^\n]*\n\s*exit 0\n")
		# The token check sits after the supersede gate so skip_create keeps exiting 0.
		self.assertLess(self.script.index(skip), self.script.index("stage=token_missing"))

	def test_http_class_derived_without_printing_stderr(self) -> None:
		for needle in (
			'2>"${triage_post_create_stderr_file}"',
			"grep -oE -m1 'HTTP [0-9]{3}'",
			"http_class=%s attempts=1",
			'"HTTP 4"??) triage_post_http_class=4xx ;;',
			'"HTTP 5"??) triage_post_http_class=5xx ;;',
			"*) triage_post_http_class=unknown ;;",
		):
			self.assertIn(needle, self.script)
		stderr_lines = [line.strip() for line in self.lines if "triage_post_create_stderr_file" in line]
		self.assertEqual(len(stderr_lines), 3, stderr_lines)
		self.assertTrue(stderr_lines[0].startswith('triage_post_create_stderr_file="${RUNTIME_DIR}/'))
		self.assertTrue(stderr_lines[1].endswith('2>"${triage_post_create_stderr_file}")"; then'))
		self.assertTrue(stderr_lines[2].startswith("triage_post_http_token=\"$(grep -oE -m1 'HTTP [0-9]{3}'"))
		for line in stderr_lines:
			self.assertNotRegex(line, r"\b(cat|echo|printf)\b")
		for line in self.lines:
			if "triage_post_http_token" in line and ("printf" in line or "echo" in line):
				self.fail(f"raw HTTP token printed: {line}")

	def test_gh_token_only_checked_for_emptiness(self) -> None:
		allowed = {
			'if [ -z "${GH_TOKEN:-}" ]; then',
		}
		for line in self.lines:
			if "GH_TOKEN" not in line:
				continue
			stripped = line.strip()
			if 'os.environ.get("GH_TOKEN", "")' in stripped:
				continue
			self.assertIn(stripped, allowed, stripped)
			self.assertNotRegex(stripped, r"\b(printf|echo)\b")

	def test_failure_stages_exit_nonzero(self) -> None:
		for code in FAILURE_STAGE_CODES:
			for match in re.finditer(rf"CHECK_TRIAGE op=post stage={code} ", self.script):
				tail = self.script[match.end():]
				self.assertRegex(tail, r"\A[^\n]*\n\s*exit 1\n\s*fi\n", code)
		self.assertNotIn('test -s "${body_file}"', self.script)
		self.assertNotIn('test -n "${triage_issue_url}"', self.script)
		self.assertIn('python3 -I -B - "${body_file}"', self.script)


class PostStageBehaviourTests(unittest.TestCase):
	def setUp(self) -> None:
		self.script = _post_run_body()
		self._tmp = tempfile.TemporaryDirectory(prefix="check-triage-post-stage-")
		self.root = Path(self._tmp.name)
		self.support = self.root / "support"
		self.runtime = self.root / "runtime"
		self.bin = self.root / "bin"
		for directory in (self.support / "scripts", self.runtime, self.bin):
			directory.mkdir(parents=True)
		for helper in ("gh_helpers.sh", "emit_event.sh"):
			shutil.copy2(REPO_ROOT / "scripts" / helper, self.support / "scripts" / helper)
		(self.runtime / "issue_body.md").write_text(
			f"<!-- check-failure-triage:fp={FINGERPRINT} -->\nTriage body\n", encoding="utf-8",
		)
		self._write_metadata({"fingerprint": FINGERPRINT, "generation": 1, "root": FINGERPRINT})
		self.calls = self.root / "gh-calls"
		_write_executable(
			self.bin / "gh",
			'#!/usr/bin/env bash\n'
			'printf "%s\\n" "$*" >> "$STUB_GH_CALLS"\n'
			'printf "%s" "${STUB_GH_STDERR:-}" >&2\n'
			'printf "%s" "${STUB_GH_STDOUT:-}"\n'
			'exit "${STUB_GH_RC:-0}"\n',
		)

	def tearDown(self) -> None:
		self._tmp.cleanup()

	def _write_metadata(self, metadata: dict) -> None:
		(self.runtime / "triage_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

	def _run(self, **overrides: str) -> subprocess.CompletedProcess:
		env = dict(os.environ)
		for name in ("BASH_ENV", "ENV", "TG_BOT_SECRET", "TG_CHAT_ID", "TG_ADMIN_CHAT_ID", "GITHUB_TOKEN"):
			env.pop(name, None)
		env.update({
			"PATH": f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
			"CHECK_TRIAGE_TRUSTED_SUPPORT_DIR": str(self.support),
			"RUNTIME_DIR": str(self.runtime),
			"GH_TOKEN": "fake-issues-token-value",
			"GITHUB_REPOSITORY": "owner/repo",
			"CHECK_NAME": "CI / lint",
			"PR_NUMBER": "17",
			"CHECK_FAILURE_TRIAGE_MAX_LINEAGE_DEPTH": "3",
			"STUB_GH_CALLS": str(self.calls),
			"STUB_GH_STDOUT": "https://github.com/owner/repo/issues/99\n",
			"STUB_GH_RC": "0",
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"TMPDIR": str(self.root),
		})
		env.update(overrides)
		proc = subprocess.run(["bash", "-c", self.script], cwd=self.root, env=env, capture_output=True, text=True, timeout=120)
		self.assertEqual(len(STAGE_LINE_RE.findall(proc.stdout)), 1, proc.stdout + proc.stderr)
		return proc

	def test_empty_token_reports_token_missing_without_calling_gh(self) -> None:
		proc = self._run(GH_TOKEN="")
		self.assertNotEqual(proc.returncode, 0)
		self.assertIn("CHECK_TRIAGE op=post stage=token_missing pr=17", proc.stdout)
		self.assertFalse(self.calls.exists())

	def test_missing_body_reports_body_missing(self) -> None:
		(self.runtime / "issue_body.md").unlink()
		proc = self._run()
		self.assertNotEqual(proc.returncode, 0)
		self.assertIn("CHECK_TRIAGE op=post stage=body_missing pr=17", proc.stdout)
		self.assertFalse(self.calls.exists())

	def test_wrong_marker_reports_marker_invalid(self) -> None:
		(self.runtime / "issue_body.md").write_text("<!-- check-failure-triage:fp=wrong -->\nbody\n", encoding="utf-8")
		proc = self._run()
		self.assertNotEqual(proc.returncode, 0)
		self.assertIn("CHECK_TRIAGE op=post stage=marker_invalid pr=17", proc.stdout)
		self.assertIn("Invalid check-failure triage issue marker", proc.stderr)
		self.assertFalse(self.calls.exists())

	def test_metadata_without_root_reports_metadata_invalid(self) -> None:
		self._write_metadata({"fingerprint": FINGERPRINT, "generation": 1})
		proc = self._run()
		self.assertNotEqual(proc.returncode, 0)
		self.assertIn("CHECK_TRIAGE op=post stage=metadata_invalid pr=17", proc.stdout)
		self.assertFalse(self.calls.exists())

	def test_create_4xx_reports_class_without_leaking_stderr(self) -> None:
		sentinel = "SENTINEL_STDERR_x"
		proc = self._run(STUB_GH_RC="1", STUB_GH_STDOUT="", STUB_GH_STDERR=f"HTTP 403: Resource not accessible {sentinel}\n")
		self.assertNotEqual(proc.returncode, 0)
		self.assertIn("CHECK_TRIAGE op=post stage=create_failed pr=17 http_class=4xx attempts=1", proc.stdout)
		self.assertNotIn(sentinel, proc.stdout)
		self.assertNotIn(sentinel, proc.stderr)
		self.assertNotIn("fake-issues-token-value", proc.stdout + proc.stderr)
		self.assertEqual(len(self.calls.read_text().splitlines()), 1)

	def test_create_5xx_reports_class(self) -> None:
		proc = self._run(STUB_GH_RC="1", STUB_GH_STDOUT="", STUB_GH_STDERR="HTTP 502: Bad Gateway\n")
		self.assertNotEqual(proc.returncode, 0)
		self.assertIn("CHECK_TRIAGE op=post stage=create_failed pr=17 http_class=5xx attempts=1", proc.stdout)

	def test_create_without_http_token_reports_unknown(self) -> None:
		proc = self._run(STUB_GH_RC="1", STUB_GH_STDOUT="", STUB_GH_STDERR="GraphQL: boom\n")
		self.assertNotEqual(proc.returncode, 0)
		self.assertIn("CHECK_TRIAGE op=post stage=create_failed pr=17 http_class=unknown attempts=1", proc.stdout)

	def test_empty_create_output_reports_url_missing(self) -> None:
		proc = self._run(STUB_GH_STDOUT="")
		self.assertNotEqual(proc.returncode, 0)
		self.assertIn("CHECK_TRIAGE op=post stage=url_missing pr=17", proc.stdout)

	def test_success_keeps_legacy_line_and_adds_created_stage(self) -> None:
		proc = self._run()
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("CHECK_TRIAGE created issue=https://github.com/owner/repo/issues/99 fp=", proc.stdout)
		self.assertIn("CHECK_TRIAGE op=post stage=created pr=17", proc.stdout)
		self.assertLess(proc.stdout.index("CHECK_TRIAGE created issue="), proc.stdout.index("stage=created"))


if __name__ == "__main__":
	unittest.main()

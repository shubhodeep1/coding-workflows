#!/usr/bin/env python3
"""Contract tests for the REVIEW_HEAD_CHECKOUT summary line.

scripts/review_autofix_step_checkout_pr_head_branch.sh (the body of the
"Checkout PR head branch" step in review_autofix.yml) prints exactly one
REVIEW_HEAD_CHECKOUT line per step run from its EXIT trap. The tests run the
real step body against a real seed/source/split git layout (the harness of
tests/test_review_autofix_merge_precheck.py), with `git` and `sleep` shell
functions (FAKE_FUNCTIONS) defined ahead of the step body, not executables on
PATH. The `git` function can fail `git fetch` with a configured stderr and the
`sleep` function records the backoff without waiting, so the retry policy is
checked unchanged and no real waits run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import review_autofix_step_script_body  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "review_autofix_step_checkout_pr_head_branch.sh"
SUMMARY_RE = re.compile(
	r"^REVIEW_HEAD_CHECKOUT stage=(?P<stage>[a-z_]+) "
	r"outcome=(?P<outcome>ok|skipped_stale|skipped_no_push|failed) "
	r"duration_ms=(?P<duration_ms>[0-9]+) "
	r"fetch_ms=(?P<fetch_ms>[0-9]+|na) "
	r"switch_ms=(?P<switch_ms>[0-9]+|na) "
	r"fetch_attempts=(?P<fetch_attempts>[0-9]+) "
	r"git_exit_class=(?P<git_exit_class>none|transport_5xx|network|auth|not_found|other) "
	r"head_match=(?P<head_match>true|false|na) "
	r"exit_code=(?P<exit_code>[0-9]+)$"
)
REAL_GIT = shutil.which("git")

# The fakes are shell functions defined ahead of the step body, not
# executables on PATH: the pre-push gate container mounts the temp dir noexec,
# so PATH shims under tmp_path were skipped and the real git/sleep ran.
#
# git: fails `git fetch` FAKE_GIT_FETCH_FAILURES times with the stderr in
# FAKE_GIT_FETCH_STDERR_FILE, fails FAKE_GIT_FAIL_SUBCMD, and otherwise
# delegates to the real git. sleep: records the backoff, never waits.
FAKE_FUNCTIONS = """git() {
  if [ "${1:-}" = "fetch" ] && [ -n "${FAKE_GIT_FETCH_COUNTER:-}" ]; then
    local fake_git_failed=0
    if [ -f "${FAKE_GIT_FETCH_COUNTER}" ]; then fake_git_failed="$(cat "${FAKE_GIT_FETCH_COUNTER}")"; fi
    if [ "${fake_git_failed}" -lt "${FAKE_GIT_FETCH_FAILURES:-0}" ]; then
      echo "$((fake_git_failed + 1))" > "${FAKE_GIT_FETCH_COUNTER}"
      cat "${FAKE_GIT_FETCH_STDERR_FILE}" >&2
      return 128
    fi
  fi
  if [ -n "${FAKE_GIT_FAIL_SUBCMD:-}" ] && [ "${1:-}" = "${FAKE_GIT_FAIL_SUBCMD}" ]; then
    echo "fake git: ${FAKE_GIT_FAIL_SUBCMD} failed" >&2
    return 1
  fi
  command "${REAL_GIT_BIN}" "$@"
}
sleep() {
  echo "$*" >> "${FAKE_SLEEP_LOG}"
  return 0
}
"""


class Layout:
	def __init__(self, root: Path) -> None:
		self.root = root
		self.seed = root / "seed"
		self.source = root / "source"
		self.split = root / "split"
		self.runner_temp = root / "runner_temp"
		self.github_env = root / "github_env"
		self.metadata = root / "meta.json"
		self.payload = root / "payload.json"
		self.sleep_log = root / "sleep.log"
		self.fetch_counter = root / "fetch_counter"
		self.fetch_stderr = root / "fetch_stderr.txt"
		self.reviewed_sha = ""

	def git(self, *args: str, cwd: Path | None = None) -> str:
		return subprocess.check_output([REAL_GIT, "-C", str(cwd or self.seed), *args], text=True).strip()

	def build(self) -> None:
		self.seed.mkdir()
		self.git("init", "-q", "-b", "main")
		self.git("config", "user.name", "test")
		self.git("config", "user.email", "test@example.invalid")
		(self.seed / "README.md").write_text("base\n")
		self.git("add", "README.md")
		self.git("commit", "-qm", "base")
		self.git("checkout", "-qb", "review/head")
		(self.seed / "added-only-on-pr.txt").write_text("reviewed head\n")
		self.git("add", "added-only-on-pr.txt")
		self.git("commit", "-qm", "pr head")
		self.reviewed_sha = self.git("rev-parse", "HEAD")
		subprocess.run([REAL_GIT, "clone", "-qb", "review/head", str(self.seed), str(self.source)], check=True)
		shutil.copytree(self.source, self.split, ignore=shutil.ignore_patterns(".git"))
		support = self.root / "support"
		support.mkdir()
		(support / "git_ref_health_check.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
		self.support = support
		self.runner_temp.mkdir()
		self.metadata.write_text(json.dumps({"headRefName": "review/head", "headRepoFullName": "o/r"}))
		self.payload.write_text(json.dumps({"head": {"sha": self.reviewed_sha}}))

	def env(self, **overrides: str) -> dict[str, str]:
		env = {
			**os.environ,
			"REAL_GIT_BIN": REAL_GIT,
			"GIT_DIR": str(self.source / ".git"), "GIT_WORK_TREE": str(self.split),
			"PR_NUMBER": "42", "GITHUB_WORKSPACE": str(self.source), "WORKSPACE_PATH": str(self.split),
			"PR_META_FILE": str(self.metadata), "PR_PAYLOAD_FILE": str(self.payload),
			"SUPPORT_SCRIPTS_DIR": str(self.support), "GITHUB_ENV": str(self.github_env),
			"REVIEW_CHECKOUT_SHA": self.reviewed_sha, "REVIEW_HEAD_CHECKOUT_REPOSITORY": "o/r",
			"RUNNER_TEMP": str(self.runner_temp), "FAKE_SLEEP_LOG": str(self.sleep_log),
			"FAKE_GIT_FETCH_COUNTER": str(self.fetch_counter),
			"FAKE_GIT_FETCH_STDERR_FILE": str(self.fetch_stderr),
		}
		env.update(overrides)
		return env

	def run(self, *, sourced: bool = False, extra_functions: str = "", **overrides: str) -> subprocess.CompletedProcess[str]:
		self.github_env.write_text("")
		if sourced:
			# Mirrors the workflow step: GitHub's bash -eo pipefail shell sources the script file.
			cmd = ["bash", "-c", FAKE_FUNCTIONS + extra_functions + 'set -eo pipefail; source "$1"', "_", str(SCRIPT_PATH)]
		else:
			cmd = ["bash", "-c", FAKE_FUNCTIONS + extra_functions + review_autofix_step_script_body("review_autofix_step_checkout_pr_head_branch.sh")]
		return subprocess.run(cmd, cwd=self.split, env=self.env(**overrides), capture_output=True, text=True)


@pytest.fixture()
def layout(tmp_path: Path) -> Layout:
	if REAL_GIT is None:
		pytest.skip("git is not available")
	if shutil.which("jq") is None:
		pytest.skip("jq is not available")
	built = Layout(tmp_path)
	built.build()
	return built


def _summary(result: subprocess.CompletedProcess[str]) -> tuple[str, dict[str, str]]:
	lines = [line for line in result.stdout.splitlines() if line.startswith("REVIEW_HEAD_CHECKOUT")]
	assert len(lines) == 1, (result.stdout, result.stderr)
	assert not any("REVIEW_HEAD_CHECKOUT " in line for line in result.stderr.splitlines()), result.stderr
	match = SUMMARY_RE.match(lines[0])
	assert match, lines[0]
	return lines[0], match.groupdict()


def _assert_no_temp_files(layout: Layout) -> None:
	assert list(layout.runner_temp.glob("review-head-checkout-fetch.*")) == []


def test_first_attempt_success_reports_done(layout: Layout) -> None:
	result = layout.run()
	assert result.returncode == 0, result.stderr
	_, fields = _summary(result)
	assert fields["stage"] == "done"
	assert fields["outcome"] == "ok"
	assert fields["fetch_attempts"] == "1"
	assert fields["git_exit_class"] == "none"
	assert fields["head_match"] == "true"
	assert fields["exit_code"] == "0"
	assert fields["fetch_ms"].isdigit() and fields["switch_ms"].isdigit()
	assert "TARGET_BRANCH=review/head" in layout.github_env.read_text()
	assert "CAN_PUSH=true" in layout.github_env.read_text()
	assert not layout.sleep_log.exists()
	_assert_no_temp_files(layout)


def test_sourced_like_the_workflow_step_reports_exactly_once(layout: Layout) -> None:
	result = layout.run(sourced=True)
	assert result.returncode == 0, result.stderr
	_, fields = _summary(result)
	assert (fields["stage"], fields["outcome"], fields["exit_code"]) == ("done", "ok", "0")
	_assert_no_temp_files(layout)


def test_four_failed_fetches_report_transport_5xx_and_keep_retry_policy(layout: Layout) -> None:
	marker = f"MARKER{uuid.uuid4().hex}"
	layout.fetch_stderr.write_text(
		f"remote: Internal Server Error {marker}\n"
		f"fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 500\n"
	)
	result = layout.run(FAKE_GIT_FETCH_FAILURES="99")
	assert result.returncode == 1
	line, fields = _summary(result)
	assert fields["stage"] == "fetch"
	assert fields["outcome"] == "failed"
	assert fields["fetch_attempts"] == "4"
	assert fields["git_exit_class"] == "transport_5xx"
	assert fields["switch_ms"] == "na"
	assert fields["fetch_ms"].isdigit()
	assert fields["exit_code"] == "1"
	# Git stderr stays visible in the step log but never enters the summary.
	assert result.stderr.count(marker) == 4
	assert marker not in line and "Internal Server Error" not in line and "github.com" not in line
	assert marker not in result.stdout
	# Retry count and backoff are unchanged: 4 attempts, 2/4/8 s between them.
	assert layout.sleep_log.read_text().split() == ["2", "4", "8"]
	assert "failed after 4 attempts" in result.stdout + result.stderr
	_assert_no_temp_files(layout)


@pytest.mark.parametrize(
	("stderr_text", "expected"),
	[
		("fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 403", "auth"),
		("fatal: couldn't find remote ref refs/heads/review/head", "not_found"),
		("fatal: unable to access 'https://github.com/o/r/': Could not resolve host: github.com", "network"),
		("fatal: something unexpected happened", "other"),
	],
)
def test_fetch_failure_classification(layout: Layout, stderr_text: str, expected: str) -> None:
	marker = f"MARKER{uuid.uuid4().hex}"
	layout.fetch_stderr.write_text(f"{stderr_text}\n{marker}\n")
	result = layout.run(FAKE_GIT_FETCH_FAILURES="99")
	assert result.returncode == 1
	line, fields = _summary(result)
	assert (fields["stage"], fields["outcome"], fields["git_exit_class"]) == ("fetch", "failed", expected)
	assert marker in result.stderr
	assert marker not in line and "fatal" not in line
	_assert_no_temp_files(layout)


def test_retry_success_keeps_last_failed_class(layout: Layout) -> None:
	layout.fetch_stderr.write_text("fatal: unable to access 'x': The requested URL returned error: 502\n")
	result = layout.run(FAKE_GIT_FETCH_FAILURES="1")
	assert result.returncode == 0, result.stderr
	_, fields = _summary(result)
	assert (fields["stage"], fields["outcome"]) == ("done", "ok")
	assert fields["fetch_attempts"] == "2"
	assert fields["git_exit_class"] == "transport_5xx"
	assert layout.sleep_log.read_text().split() == ["2"]
	_assert_no_temp_files(layout)


def test_head_moved_after_switch_reports_skipped_stale(layout: Layout) -> None:
	(layout.seed / "newer.txt").write_text("new head\n")
	layout.git("add", "newer.txt")
	layout.git("commit", "-qm", "advanced head")
	result = layout.run()
	assert result.returncode == 0, result.stderr
	_, fields = _summary(result)
	assert fields["stage"] == "head_moved_guard"
	assert fields["outcome"] == "skipped_stale"
	assert fields["head_match"] == "false"
	assert fields["exit_code"] == "0"
	assert fields["fetch_ms"].isdigit() and fields["switch_ms"].isdigit()
	assert "AUTOFIX_STALE_BASE_SKIP=true" in layout.github_env.read_text()
	assert "TARGET_BRANCH=" not in layout.github_env.read_text()
	_assert_no_temp_files(layout)


def test_head_match_guard_skip_reports_before_fetch(layout: Layout) -> None:
	layout.payload.write_text(json.dumps({"head": {"sha": "f" * 40}}))
	result = layout.run()
	assert result.returncode == 0, result.stderr
	_, fields = _summary(result)
	assert fields["stage"] == "head_match_guard"
	assert fields["outcome"] == "skipped_stale"
	assert fields["head_match"] == "false"
	assert fields["fetch_ms"] == "na" and fields["switch_ms"] == "na"
	assert fields["fetch_attempts"] == "0"
	assert fields["git_exit_class"] == "none"
	assert "AUTOFIX_STALE_BASE_SKIP=true" in layout.github_env.read_text()


def test_foreign_head_repo_reports_skipped_no_push(layout: Layout) -> None:
	result = layout.run(REVIEW_HEAD_CHECKOUT_REPOSITORY="other/repo")
	assert result.returncode == 0, result.stderr
	_, fields = _summary(result)
	assert (fields["stage"], fields["outcome"], fields["exit_code"]) == ("head_repo_foreign", "skipped_no_push", "0")
	assert fields["fetch_attempts"] == "0"
	assert "CAN_PUSH=false" in layout.github_env.read_text()


def test_missing_workspace_reports_failed_guard(layout: Layout) -> None:
	result = layout.run(WORKSPACE_PATH=str(layout.root / "absent"))
	assert result.returncode == 1
	_, fields = _summary(result)
	assert (fields["stage"], fields["outcome"], fields["exit_code"]) == ("workspace_guard", "failed", "1")


def test_errexit_abort_in_branch_switch_is_recorded(layout: Layout) -> None:
	# The source clone is on review/head, so the switch uses `git reset --hard`.
	result = layout.run(FAKE_GIT_FAIL_SUBCMD="reset")
	assert result.returncode != 0
	_, fields = _summary(result)
	assert fields["stage"] == "branch_switch"
	assert fields["outcome"] == "failed"
	assert fields["exit_code"] == str(result.returncode)
	assert fields["switch_ms"].isdigit()
	assert fields["fetch_attempts"] == "1"
	assert "TARGET_BRANCH=" not in layout.github_env.read_text()
	_assert_no_temp_files(layout)


def test_elapsed_fields_ignore_a_date_clock_that_goes_backwards(layout: Layout) -> None:
	# ubuntu-26.04 runners ship Rust coreutils, whose `date +%s%3N` gave short
	# or backwards readings within one run, so END - START went negative and the
	# summary printed switch_ms=na (issue #9317). The step now reads bash's
	# EPOCHREALTIME first; on bash < 5 it falls back to `date` by design, so
	# there is nothing to test there.
	probe = subprocess.run(
		["bash", "--noprofile", "--norc", "-c", '[ -n "${EPOCHREALTIME:-}" ]'],
		capture_output=True,
		timeout=30,
	)
	if probe.returncode != 0:
		pytest.skip("bash has no EPOCHREALTIME (bash < 5); the step uses its `date` fallback")
	date_counter = layout.root / "date_counter"
	# The counter is a file because the clock runs inside $(...) subshells.
	# Every call returns a reading 100 ms earlier than the previous one.
	fake_date = f"""date() {{
  local fake_date_calls=0
  if [ -f "{date_counter}" ]; then fake_date_calls="$(cat "{date_counter}")"; fi
  fake_date_calls=$((fake_date_calls + 1))
  echo "${{fake_date_calls}}" > "{date_counter}"
  echo "$((1700000009000 - 100 * fake_date_calls))"
}}
"""
	result = layout.run(extra_functions=fake_date)
	assert result.returncode == 0, result.stderr
	_, fields = _summary(result)
	assert (fields["stage"], fields["outcome"]) == ("done", "ok")
	assert fields["fetch_ms"].isdigit(), fields
	assert fields["switch_ms"].isdigit(), fields
	assert not date_counter.exists() or date_counter.read_text().strip() == ""
	_assert_no_temp_files(layout)


def test_script_contract_is_additive() -> None:
	text = SCRIPT_PATH.read_text()
	assert len(re.findall(r"^\s*trap ", text, re.MULTILINE)) == 1
	assert "trap 'review_head_checkout_on_exit \"$?\" || true' EXIT" in text
	assert text.index("trap 'review_head_checkout_on_exit") < text.index('HEAD_REF="$(jq')
	assert "fetch_max_attempts=4" in text and "fetch_backoff=2" in text
	assert "date +%s%3N" in text
	assert "EPOCHREALTIME" in text
	handler = text[text.index("review_head_checkout_on_exit()"):text.index("REVIEW_HEAD_CHECKOUT_START_MS=")]
	assert "exit " not in handler
	assert "REVIEW_CHECKOUT_SHA=" not in text

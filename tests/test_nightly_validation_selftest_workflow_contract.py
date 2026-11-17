#!/usr/bin/env python3
"""Contract tests for nightly validation self-test workflow."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "nightly-validation-selftest.yml"
COMMIT_STEP_MARKER = "- name: Commit validation self-test status"
SEED_STEP_MARKER = "- name: Read previous status from an open status PR"
RUN_HEADER = "run: |\n"
RUN_INDENT = " " * 10
BLOCK_END_RE = re.compile(r"^ {0,9}\S")
RULESET_PATTERN = "GH006|GH013|protected branch|push declined|repository rule"
SENTINEL_TOKEN = "ghp_SENTINEL_status_push_token_8225"
GH013_PUSH_OUTPUT = (
	"remote: error: GH013: Repository rule violations found for refs/heads/main.\n"
	"remote: - 2 of 2 required status checks are expected.\n"
	"To https://github.com/shubhodeep1/coding-workflows\n"
	" ! [remote rejected] HEAD -> main (push declined due to repository rule violations)\n"
	"error: failed to push some refs to 'https://github.com/shubhodeep1/coding-workflows'\n"
)


def _workflow_text() -> str:
	return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_workflow_has_nightly_schedule_and_manual_dispatch() -> None:
	wf = _workflow_text()
	assert 'name: Nightly Validation Self-Test' in wf
	assert 'schedule:' in wf
	assert 'cron: "15 2 * * *"' in wf
	assert 'workflow_dispatch:' in wf


def test_workflow_runs_matrix_and_always_uploads_artifacts() -> None:
	wf = _workflow_text()
	assert 'python3 scripts/validation_selftest_matrix.py' in wf
	assert '--fixtures-root "tests/fixtures/selftest"' in wf
	assert '--summary-path "artifacts/validation-selftest-summary.json"' in wf
	assert '--log-dir "artifacts/validation-selftest-logs"' in wf
	assert '--runtime-command "bash scripts/validate_driver.sh"' in wf
	assert '- name: Upload validation self-test artifacts' in wf
	assert 'if: always()' in wf
	assert 'uses: actions/upload-artifact@v6' in wf
	assert 'artifacts/validation-selftest-summary.json' in wf
	assert 'artifacts/validation-selftest-logs/' in wf
	assert '- name: Update validation self-test status artifact' in wf
	assert 'python3 scripts/validation_selftest_status.py' in wf
	assert '--summary-path "artifacts/validation-selftest-summary.json"' in wf
	assert '--status-path "analysis/validation-selftest-status.json"' in wf


def test_workflow_commits_only_status_file_when_changed() -> None:
	wf = _workflow_text()
	assert '- name: Commit validation self-test status' in wf
	assert "if: always() && github.ref_type == 'branch'" in wf
	assert 'status_file="analysis/validation-selftest-status.json"' in wf
	assert 'git add "${status_file}"' in wf
	assert 'if git diff --cached --quiet; then' in wf
	assert 'git commit -m "${pr_title}"' in wf
	assert 'pr_title="chore: update validation self-test status"' in wf


def test_status_reaches_main_through_a_pull_request_not_a_direct_push() -> None:
	# #8229: the main ruleset requires `lint` and `ai-review/head-gate` and has
	# no bypass actors, so a direct push is refused (GH013). The status goes to
	# a per-run bot branch and a PR the existing review path merges.
	wf = _workflow_text()
	commit_step = wf.split(COMMIT_STEP_MARKER, 1)[1]
	assert 'git push origin "HEAD:${TARGET_BRANCH}"' not in wf
	assert 'git pull --rebase' not in wf
	assert '--admin' not in wf
	assert 'contents: write' not in wf
	assert 'status_branch="auto/validation-selftest-status-${run_id}-${run_attempt}"' in commit_step
	assert 'git push origin "HEAD:refs/heads/${status_branch}"' in commit_step
	assert 'gh pr create --repo "${GITHUB_REPOSITORY}" --base "${TARGET_BRANCH}" --head "${status_branch}"' in commit_step
	lint_at = commit_step.index('python3 scripts/lint_pr_body_auto_close.py --pr-body-file')
	assert lint_at < commit_step.index('git push origin')
	assert commit_step.index('git push origin') < commit_step.index('gh pr create')
	assert 'gh pr merge' not in commit_step
	assert '--delete-branch' not in commit_step


def test_previous_status_is_seeded_before_the_status_writer() -> None:
	wf = _workflow_text()
	commit_job = wf.split('  commit-selftest-status:', 1)[1]
	seed_at = commit_job.index(SEED_STEP_MARKER)
	writer_at = commit_job.index('python3 scripts/validation_selftest_status.py')
	assert seed_at < writer_at
	assert 'id: previous_status' in commit_job
	assert 'PREVIOUS_STATUS_PATH: ${{ steps.previous_status.outputs.path }}' in commit_job
	assert 'previous_args=(--previous-status-path "${PREVIOUS_STATUS_PATH}")' in commit_job
	assert 'SUPERSEDED_PRS: ${{ steps.previous_status.outputs.superseded }}' in commit_job


def test_checkout_does_not_persist_credentials_for_generated_tests() -> None:
	# Generated validation tests run inside this checkout; the token must not
	# be persisted into .git/config where they could read it (#6568). Only the
	# commit step receives a one-shot credential helper reading the env.
	wf = _workflow_text()
	assert 'persist-credentials: false' in wf
	assert 'STATUS_PUSH_TOKEN: ${{ secrets.GH_PAT || github.token }}' in wf
	assert 'STATUS_PUSH_TOKEN: ${{ github.token }}' not in wf
	assert 'export GIT_CONFIG_KEY_0=credential.helper' in wf
	commit_step = wf.split('- name: Commit validation self-test status', 1)[1]
	assert commit_step.index('GIT_CONFIG_VALUE_0') < commit_step.index('git push origin')
	selftest_step = wf.split('- name: Run validation self-test matrix', 1)[1].split('- name:', 1)[0]
	assert 'token' not in selftest_step.lower()


def test_status_commit_runs_in_separate_job_from_generated_tests() -> None:
	# Tests run as the runner user and can rewrite the checkout's .git (hooks,
	# config, HEAD) before a later step uses a write token (#6568). The commit
	# therefore runs in its own job on a fresh checkout; the self-test job only
	# holds a read-only token and hands over just the summary JSON.
	wf = _workflow_text()
	selftest_job, commit_job = wf.split('  commit-selftest-status:', 1)
	assert 'contents: read' in selftest_job
	assert 'contents: write' not in selftest_job
	assert 'actions: write' not in selftest_job
	assert 'STATUS_PUSH_TOKEN' not in selftest_job
	assert 'git push' not in selftest_job
	assert 'name: validation-selftest-summary-${{ github.run_id }}-${{ github.run_attempt }}' in selftest_job
	assert 'needs: validation-selftest' in commit_job
	assert "if: always() && github.ref_type == 'branch'" in commit_job
	assert 'contents: read' in commit_job
	assert 'uses: actions/checkout@v5' in commit_job
	assert 'uses: actions/download-artifact@v4' in commit_job
	assert 'name: validation-selftest-summary-${{ github.run_id }}-${{ github.run_attempt }}' in commit_job
	assert 'validation_selftest_matrix.py' not in commit_job
	assert commit_job.index('python3 scripts/validation_selftest_status.py') < commit_job.index('git push origin')


def test_status_push_uses_pat_and_classifies_ruleset_rejection() -> None:
	# #8225: github.token is not a bypass actor on the main ruleset, so the
	# status push uses the same identity as the other bot pushes and names a
	# ruleset refusal. The self-test job never sees GH_PAT (#6568).
	wf = _workflow_text()
	selftest_job, commit_job = wf.split('  commit-selftest-status:', 1)
	assert 'GH_PAT' not in selftest_job
	assert "STATUS_PUSH_TOKEN_SOURCE: ${{ secrets.GH_PAT != '' && 'gh_pat' || 'github_token' }}" in commit_job
	commit_step = commit_job.split(COMMIT_STEP_MARKER, 1)[1]
	assert RULESET_PATTERN in commit_step
	assert 'reason=push_rejected_by_ruleset' in commit_step
	assert 'reason=push_failed' in commit_step
	assert 'exit "${push_exit}"' in commit_step


def _step_script(marker: str) -> str:
	"""Return a step's `run:` body from the shipped YAML, dedented."""
	text = _workflow_text()
	start = text.find(marker)
	assert start != -1, f"could not locate step {marker!r}"
	run_at = text.find(RUN_HEADER, start)
	assert run_at != -1, "step has no block-scalar run body"
	assert "- name:" not in text[start + len(marker):run_at], \
		"run body found belongs to a later step"
	lines: list[str] = []
	for line in text[run_at + len(RUN_HEADER):].splitlines():
		if line.startswith(RUN_INDENT):
			lines.append(line[len(RUN_INDENT):])
		elif line.strip() == "":
			lines.append("")
		else:
			assert BLOCK_END_RE.match(line), f"run body ended on an unexpected line: {line!r}"
			break
	while lines and lines[-1] == "":
		lines.pop()
	return "\n".join(lines) + "\n"


def _commit_step_script() -> str:
	body = _step_script(COMMIT_STEP_MARKER)
	assert 'git push origin' in body, "commit step run body lost its push"
	return body


_FAKE_GIT = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_GIT_LOG}"
case "$1" in
  diff) exit 1 ;;
  push) printf '%s' "${FAKE_PUSH_OUTPUT}" >&2; exit "${FAKE_PUSH_RC}" ;;
  *) exit 0 ;;
esac
"""

_FAKE_GH_COMMIT = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_GH_LOG}"
case "$1 $2" in
  "pr create") printf '%s' "${FAKE_PR_CREATE_OUTPUT}"; exit "${FAKE_PR_CREATE_RC}" ;;
  "pr list") printf '%s' "${FAKE_PR_LIST_OUTPUT}"; exit 0 ;;
  "pr close") exit "${FAKE_PR_CLOSE_RC}" ;;
  *) exit 1 ;;
esac
"""

_FAKE_LINT = """import os
import sys
sys.exit(int(os.environ.get("FAKE_LINT_RC", "0")))
"""


def _make_exec(path: Path, text: str) -> None:
	path.write_text(text, encoding="utf-8")
	path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_commit_step(
	push_output: str = "",
	push_rc: int = 0,
	*,
	token_source: str = "gh_pat",
	lint_rc: int = 0,
	create_output: str = "https://github.com/o/r/pull/42\n",
	create_rc: int = 0,
	list_output: str = "",
	close_rc: int = 0,
	superseded: str = "",
	previous_status: str | None = None,
	status_text: str = '{"overall_status": "pass"}\n',
) -> tuple[subprocess.CompletedProcess, list[str], list[str]]:
	script = _commit_step_script()
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		work = root / "work"
		(work / "analysis").mkdir(parents=True)
		(work / "scripts").mkdir()
		(work / "scripts" / "lint_pr_body_auto_close.py").write_text(_FAKE_LINT, encoding="utf-8")
		(work / "analysis" / "validation-selftest-status.json").write_text(status_text, encoding="utf-8")
		bindir = root / "bin"
		bindir.mkdir()
		_make_exec(bindir / "git", _FAKE_GIT)
		_make_exec(bindir / "gh", _FAKE_GH_COMMIT)
		runner_temp = root / "runner"
		runner_temp.mkdir()
		previous_path = ""
		if previous_status is not None:
			previous_file = runner_temp / "previous-selftest-status.json"
			previous_file.write_text(previous_status, encoding="utf-8")
			previous_path = str(previous_file)
		script_path = root / "step.sh"
		script_path.write_text("set -e\n" + script, encoding="utf-8")
		git_log = root / "git.log"
		gh_log = root / "gh.log"
		env = {
			"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
			"HOME": str(root),
			"TMPDIR": str(root),
			"RUNNER_TEMP": str(runner_temp),
			"TARGET_BRANCH": "main",
			"STATUS_PUSH_TOKEN": SENTINEL_TOKEN,
			"STATUS_PUSH_TOKEN_SOURCE": token_source,
			"GH_TOKEN": SENTINEL_TOKEN if token_source == "gh_pat" else "",
			"GITHUB_RUN_ID": "123",
			"GITHUB_RUN_ATTEMPT": "1",
			"GITHUB_REPOSITORY": "o/r",
			"GITHUB_SERVER_URL": "https://github.com",
			"PREVIOUS_STATUS_PATH": previous_path,
			"PREVIOUS_STATUS_PR": "40" if previous_path else "",
			"SUPERSEDED_PRS": superseded,
			"FAKE_GIT_LOG": str(git_log),
			"FAKE_GH_LOG": str(gh_log),
			"FAKE_PUSH_OUTPUT": push_output,
			"FAKE_PUSH_RC": str(push_rc),
			"FAKE_LINT_RC": str(lint_rc),
			"FAKE_PR_CREATE_OUTPUT": create_output,
			"FAKE_PR_CREATE_RC": str(create_rc),
			"FAKE_PR_LIST_OUTPUT": list_output,
			"FAKE_PR_CLOSE_RC": str(close_rc),
		}
		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=work,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		git_calls = git_log.read_text(encoding="utf-8").splitlines() if git_log.exists() else []
		gh_calls = gh_log.read_text(encoding="utf-8").splitlines() if gh_log.exists() else []
	assert SENTINEL_TOKEN not in proc.stdout
	assert SENTINEL_TOKEN not in proc.stderr
	assert not any(call.startswith("pull") for call in git_calls), git_calls
	return proc, git_calls, gh_calls


def _pushes(git_calls: list[str]) -> list[str]:
	return [call for call in git_calls if call.startswith("push")]


def test_commit_step_opens_pull_request_on_a_bot_branch() -> None:
	proc, git_calls, gh_calls = _run_commit_step()
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _pushes(git_calls) == ["push origin HEAD:refs/heads/auto/validation-selftest-status-123-1"]
	assert (
		"VALIDATION_SELFTEST_STATUS outcome=pr_opened branch=auto/validation-selftest-status-123-1 pr=42 token=gh_pat"
		in proc.stdout
	), proc.stdout
	assert any(call.startswith("pr create --repo o/r --base main --head auto/validation-selftest-status-123-1") for call in gh_calls), gh_calls
	assert not any(call.startswith("pr merge") for call in gh_calls)
	assert "::error::" not in proc.stdout


def test_commit_step_classifies_ruleset_rejection() -> None:
	proc, _, gh_calls = _run_commit_step(GH013_PUSH_OUTPUT, 1)
	assert proc.returncode != 0
	assert (
		"::error::VALIDATION_SELFTEST_STATUS outcome=failed reason=push_rejected_by_ruleset "
		"branch=auto/validation-selftest-status-123-1 token=gh_pat"
		in proc.stdout
	), proc.stdout
	assert re.search(r"reason=push_rejected_by_ruleset branch=\S+ token=gh_pat$", proc.stdout, re.MULTILINE), \
		"token= must be the last field on the ::error:: line"
	assert "outcome=pr_opened" not in proc.stdout
	assert "GH013" in proc.stderr
	assert not any(call.startswith("pr create") for call in gh_calls)


def test_commit_step_reports_other_push_failures() -> None:
	proc, _, _ = _run_commit_step(
		"fatal: unable to access 'https://x-access-token:secret@github.com/o/r/': Could not resolve host\n",
		128,
	)
	assert proc.returncode != 0
	assert (
		"::error::VALIDATION_SELFTEST_STATUS outcome=failed reason=push_failed "
		"branch=auto/validation-selftest-status-123-1 token=gh_pat rc=128"
		in proc.stdout
	), proc.stdout
	assert "push_rejected_by_ruleset" not in proc.stdout
	assert "x-access-token:secret@" not in proc.stderr
	assert "https://***@github.com" in proc.stderr


def test_commit_step_lint_failure_pushes_nothing() -> None:
	proc, git_calls, gh_calls = _run_commit_step(lint_rc=1)
	assert proc.returncode != 0
	assert "reason=pr_body_lint_failed" in proc.stdout
	assert _pushes(git_calls) == []
	assert gh_calls == []


def test_commit_step_requires_gh_pat() -> None:
	proc, git_calls, gh_calls = _run_commit_step(token_source="github_token")
	assert proc.returncode != 0
	assert "::error::VALIDATION_SELFTEST_STATUS outcome=failed reason=gh_pat_missing token=github_token" in proc.stdout
	assert _pushes(git_calls) == []
	assert gh_calls == []


def test_commit_step_recovers_pr_from_listing_after_create_failure() -> None:
	proc, _, gh_calls = _run_commit_step(create_output="", create_rc=1, list_output="57\n")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "outcome=pr_opened branch=auto/validation-selftest-status-123-1 pr=57 token=gh_pat" in proc.stdout
	assert any(call.startswith("pr list") for call in gh_calls)


def test_commit_step_fails_when_pr_cannot_be_created_or_found() -> None:
	proc, _, _ = _run_commit_step(create_output="", create_rc=1, list_output="")
	assert proc.returncode != 0
	assert "reason=pr_create_failed branch=auto/validation-selftest-status-123-1" in proc.stdout
	assert "outcome=pr_opened" not in proc.stdout


def test_commit_step_closes_superseded_prs_without_deleting_branches() -> None:
	proc, _, gh_calls = _run_commit_step(superseded="40 31 99 x;rm")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	closes = [call for call in gh_calls if call.startswith("pr close")]
	assert [call.split()[2] for call in closes] == ["40", "31"], closes
	assert all("--delete-branch" not in call for call in closes)
	assert "VALIDATION_SELFTEST_STATUS outcome=superseded pr=40 by=42" in proc.stdout
	assert "VALIDATION_SELFTEST_STATUS outcome=superseded pr=31 by=42" in proc.stdout


def test_commit_step_supersede_close_failure_only_warns() -> None:
	proc, _, _ = _run_commit_step(superseded="40", close_rc=1)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "::warning::VALIDATION_SELFTEST_STATUS outcome=supersede_close_failed pr=40" in proc.stdout


def test_commit_step_skips_when_open_pr_already_carries_status() -> None:
	status = '{"overall_status": "pass"}\n'
	proc, git_calls, gh_calls = _run_commit_step(previous_status=status, status_text=status, superseded="40")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "VALIDATION_SELFTEST_STATUS outcome=unchanged reason=matches_open_pr pr=40" in proc.stdout
	assert _pushes(git_calls) == []
	assert gh_calls == []


def test_unchanged_status_still_closes_older_status_prs() -> None:
	status = '{"overall_status": "pass"}\n'
	proc, git_calls, gh_calls = _run_commit_step(previous_status=status, status_text=status, superseded="40 31")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "outcome=unchanged reason=matches_open_pr pr=40" in proc.stdout
	closes = [call for call in gh_calls if call.startswith("pr close")]
	assert [call.split()[2] for call in closes] == ["31"], closes
	assert "VALIDATION_SELFTEST_STATUS outcome=superseded pr=31 by=40" in proc.stdout
	assert _pushes(git_calls) == []


def test_status_commit_job_is_serialized_without_cancelling() -> None:
	commit_job = _workflow_text().split('  commit-selftest-status:', 1)[1]
	head = commit_job.split('    steps:', 1)[0]
	assert 'group: nightly-validation-selftest-status-${{ github.ref }}' in head
	assert 'cancel-in-progress: false' in head


def test_status_commit_job_checks_out_the_branch_tip() -> None:
	# A run queued behind the concurrency group must see a status PR that
	# merged while it waited, so the checkout takes the branch tip (#8229).
	commit_job = _workflow_text().split('  commit-selftest-status:', 1)[1]
	checkout = commit_job.split('uses: actions/checkout@v5', 1)[1].split('- name:', 1)[0]
	assert 'ref: ${{ github.ref }}' in checkout
	assert 'persist-credentials: false' in checkout


_FAKE_GH_SEED = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_GH_LOG}"
if [ "$1" = "api" ] && [ "$2" = "user" ]; then
  [ "${FAKE_USER_RC:-0}" = "0" ] || exit 1
  printf '%s\\n' "${FAKE_USER}"
  exit 0
fi
if [ "$1" = "api" ] && [ "$2" = "--paginate" ]; then
  cat "${FAKE_PULLS_FILE}"
  exit 0
fi
if [ "$1" = "api" ] && [ "$2" = "-H" ]; then
  [ "${FAKE_CONTENT_RC:-0}" = "0" ] || exit 1
  printf '%s\\n' "$4" >> "${FAKE_GH_LOG}.contents"
  cat "${FAKE_CONTENT_FILE}"
  exit 0
fi
exit 1
"""

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _pull(number: int, *, sha: str = _SHA_A, ref: str | None = None, repo: str = "o/r", login: str = "bot-user") -> dict:
	return {
		"number": number,
		"head": {"sha": sha, "ref": ref or f"auto/validation-selftest-status-{number}00-1", "repo": {"full_name": repo}},
		"user": {"login": login},
	}


def _run_seed_step(
	pulls: list,
	*,
	content: str = '{"consecutive_green_runs": 3, "latest_run": {}}\n',
	user_rc: int = 0,
	content_rc: int = 0,
	token: str = SENTINEL_TOKEN,
) -> tuple[subprocess.CompletedProcess, dict[str, str], list[str]]:
	import json

	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")
	script = _step_script(SEED_STEP_MARKER)
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bindir = root / "bin"
		bindir.mkdir()
		_make_exec(bindir / "gh", _FAKE_GH_SEED)
		runner_temp = root / "runner"
		runner_temp.mkdir()
		pulls_file = root / "pulls.json"
		pulls_file.write_text(json.dumps(pulls), encoding="utf-8")
		content_file = root / "content.json"
		content_file.write_text(content, encoding="utf-8")
		output_file = root / "output"
		output_file.write_text("", encoding="utf-8")
		script_path = root / "seed.sh"
		script_path.write_text(script, encoding="utf-8")
		gh_log = root / "gh.log"
		env = {
			"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
			"HOME": str(root),
			"RUNNER_TEMP": str(runner_temp),
			"GITHUB_OUTPUT": str(output_file),
			"GITHUB_REPOSITORY": "o/r",
			"GH_TOKEN": token,
			"STATUS_BASE_BRANCH": "main",
			"FAKE_GH_LOG": str(gh_log),
			"FAKE_USER": "bot-user",
			"FAKE_USER_RC": str(user_rc),
			"FAKE_PULLS_FILE": str(pulls_file),
			"FAKE_CONTENT_FILE": str(content_file),
			"FAKE_CONTENT_RC": str(content_rc),
		}
		proc = subprocess.run(["bash", str(script_path)], env=env, capture_output=True, text=True, timeout=60)
		outputs: dict[str, str] = {}
		for line in output_file.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value
		contents_log = Path(str(gh_log) + ".contents")
		contents_refs = contents_log.read_text(encoding="utf-8").splitlines() if contents_log.exists() else []
		if outputs.get("path"):
			assert Path(outputs["path"]).is_file()
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert SENTINEL_TOKEN not in proc.stdout + proc.stderr
	return proc, outputs, contents_refs


def test_seed_step_picks_newest_trusted_status_pr() -> None:
	pulls = [
		_pull(40, sha=_SHA_A),
		_pull(31, sha=_SHA_B),
		_pull(90, repo="fork/r"),
		_pull(91, login="someone-else"),
		_pull(92, ref="auto/validation-selftest-status-evil"),
		_pull(93, ref="feature/x"),
	]
	proc, outputs, refs = _run_seed_step(pulls)
	assert outputs["source_pr"] == "40"
	assert outputs["superseded"] == "40 31"
	assert outputs["path"].endswith("previous-selftest-status.json")
	assert refs and refs[0].endswith(f"?ref={_SHA_A}")
	assert "VALIDATION_SELFTEST_STATUS outcome=seeded pr=40" in proc.stdout


def test_seed_step_no_matching_pr_leaves_outputs_empty() -> None:
	proc, outputs, refs = _run_seed_step([_pull(91, login="someone-else")])
	assert outputs == {"path": "", "source_pr": "", "superseded": ""}
	assert "reason=no_open_status_pr" in proc.stdout
	assert refs == []


def test_seed_step_failures_leave_outputs_empty() -> None:
	for kwargs, reason in (
		({"token": ""}, "gh_pat_missing"),
		({"user_rc": 1}, "identity_unavailable"),
		({"content_rc": 1}, "content_unreadable"),
		({"content": "[1, 2]\n"}, "content_unreadable"),
		({"content": '{"a": "' + "x" * 70000 + '"}\n'}, "content_unreadable"),
	):
		proc, outputs, _ = _run_seed_step([_pull(40)], **kwargs)
		assert outputs == {"path": "", "source_pr": "", "superseded": ""}, (kwargs, outputs)
		assert f"outcome=seed_skipped reason={reason}" in proc.stdout, (reason, proc.stdout)
	proc, outputs, _ = _run_seed_step([_pull(40, sha="not-a-sha")])
	assert outputs["superseded"] == ""
	assert "reason=content_unreadable" in proc.stdout


def test_workflow_emits_machine_readable_summary_to_step_summary() -> None:
	wf = _workflow_text()
	assert '- name: Write self-test summary' in wf
	assert 'summary_file="artifacts/validation-selftest-summary.json"' in wf
	assert "summary_data=\"$(jq -r '" in wf
	assert "fixture_lines=\"$(jq -r '" in wf
	assert '(.overall_status // "unknown")' in wf
	assert '(.totals.fixtures // 0)' in wf
	assert '.fixtures[]?' in wf
	assert '.stages.clone.status' in wf
	assert '] | @tsv' in wf
	assert 'GITHUB_STEP_SUMMARY' in wf


def main() -> int:
	for test in (
		test_workflow_has_nightly_schedule_and_manual_dispatch,
		test_workflow_runs_matrix_and_always_uploads_artifacts,
		test_workflow_commits_only_status_file_when_changed,
		test_status_reaches_main_through_a_pull_request_not_a_direct_push,
		test_previous_status_is_seeded_before_the_status_writer,
		test_checkout_does_not_persist_credentials_for_generated_tests,
		test_status_commit_runs_in_separate_job_from_generated_tests,
		test_status_push_uses_pat_and_classifies_ruleset_rejection,
		test_commit_step_opens_pull_request_on_a_bot_branch,
		test_commit_step_classifies_ruleset_rejection,
		test_commit_step_reports_other_push_failures,
		test_commit_step_lint_failure_pushes_nothing,
		test_commit_step_requires_gh_pat,
		test_commit_step_recovers_pr_from_listing_after_create_failure,
		test_commit_step_fails_when_pr_cannot_be_created_or_found,
		test_commit_step_closes_superseded_prs_without_deleting_branches,
		test_commit_step_supersede_close_failure_only_warns,
		test_commit_step_skips_when_open_pr_already_carries_status,
		test_unchanged_status_still_closes_older_status_prs,
		test_status_commit_job_is_serialized_without_cancelling,
		test_status_commit_job_checks_out_the_branch_tip,
		test_seed_step_picks_newest_trusted_status_pr,
		test_seed_step_no_matching_pr_leaves_outputs_empty,
		test_seed_step_failures_leave_outputs_empty,
		test_workflow_emits_machine_readable_summary_to_step_summary,
	):
		try:
			test()
		except unittest.SkipTest as exc:
			print(f"skipped {test.__name__}: {exc}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

#!/usr/bin/env python3
"""Contract tests for nightly validation self-test workflow."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "nightly-validation-selftest.yml"
COMMIT_STEP_MARKER = "- name: Commit validation self-test status"
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
	assert 'permissions:' in wf
	assert 'contents: write' in wf
	assert '- name: Commit validation self-test status' in wf
	assert "if: always() && github.ref_type == 'branch'" in wf
	assert 'status_file="analysis/validation-selftest-status.json"' in wf
	assert 'git add "${status_file}"' in wf
	assert 'if git diff --cached --quiet; then' in wf
	assert 'git commit -m "chore: update validation self-test status"' in wf
	assert 'git pull --rebase origin "${TARGET_BRANCH}"' in wf
	assert 'git push origin "HEAD:${TARGET_BRANCH}"' in wf


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
	assert commit_step.index('GIT_CONFIG_VALUE_0') < commit_step.index('git pull --rebase origin')
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
	assert 'contents: write' in commit_job
	assert 'uses: actions/checkout@v5' in commit_job
	assert 'uses: actions/download-artifact@v4' in commit_job
	assert 'name: validation-selftest-summary-${{ github.run_id }}-${{ github.run_attempt }}' in commit_job
	assert 'validation_selftest_matrix.py' not in commit_job
	assert commit_job.index('python3 scripts/validation_selftest_status.py') < commit_job.index('git push origin')


def test_status_push_uses_pat_and_classifies_ruleset_rejection() -> None:
	# #8225: github.token is not a bypass actor on the main ruleset, so the
	# status push uses the same identity as the other bot pushes to main and
	# names a ruleset refusal. The self-test job never sees GH_PAT (#6568).
	wf = _workflow_text()
	selftest_job, commit_job = wf.split('  commit-selftest-status:', 1)
	assert 'GH_PAT' not in selftest_job
	assert "STATUS_PUSH_TOKEN_SOURCE: ${{ secrets.GH_PAT != '' && 'gh_pat' || 'github_token' }}" in commit_job
	commit_step = commit_job.split(COMMIT_STEP_MARKER, 1)[1]
	assert RULESET_PATTERN in commit_step
	assert 'reason=push_rejected_by_ruleset' in commit_step
	assert 'reason=push_failed' in commit_step
	assert 'exit "${push_exit}"' in commit_step
	assert commit_step.index('GIT_CONFIG_VALUE_0') < commit_step.index('git pull --rebase origin')


def _commit_step_script() -> str:
	"""Return the commit step's `run:` body from the shipped YAML, dedented."""
	text = _workflow_text()
	start = text.find(COMMIT_STEP_MARKER)
	assert start != -1, "could not locate the commit step"
	run_at = text.find(RUN_HEADER, start)
	assert run_at != -1, "commit step has no block-scalar run body"
	assert "- name:" not in text[start + len(COMMIT_STEP_MARKER):run_at], \
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
	body = "\n".join(lines)
	assert 'git push origin' in body, "commit step run body lost its push"
	return body + "\n"


_FAKE_GIT = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_GIT_LOG}"
case "$1" in
  diff) exit 1 ;;
  push) printf '%s' "${FAKE_PUSH_OUTPUT}" >&2; exit "${FAKE_PUSH_RC}" ;;
  *) exit 0 ;;
esac
"""


def _run_commit_step(push_output: str, push_rc: int) -> tuple[subprocess.CompletedProcess, list[str]]:
	script = _commit_step_script()
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		work = root / "work"
		(work / "analysis").mkdir(parents=True)
		(work / "analysis" / "validation-selftest-status.json").write_text('{"overall_status": "pass"}\n', encoding="utf-8")
		bindir = root / "bin"
		bindir.mkdir()
		fake_git = bindir / "git"
		fake_git.write_text(_FAKE_GIT, encoding="utf-8")
		fake_git.chmod(fake_git.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
		script_path = root / "step.sh"
		script_path.write_text("set -e\n" + script, encoding="utf-8")
		git_log = root / "git.log"
		env = {
			"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
			"HOME": str(root),
			"TMPDIR": str(root),
			"TARGET_BRANCH": "main",
			"STATUS_PUSH_TOKEN": SENTINEL_TOKEN,
			"STATUS_PUSH_TOKEN_SOURCE": "gh_pat",
			"FAKE_GIT_LOG": str(git_log),
			"FAKE_PUSH_OUTPUT": push_output,
			"FAKE_PUSH_RC": str(push_rc),
		}
		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=work,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		calls = git_log.read_text(encoding="utf-8").splitlines() if git_log.exists() else []
	assert SENTINEL_TOKEN not in proc.stdout
	assert SENTINEL_TOKEN not in proc.stderr
	verbs = [call.split(" ", 1)[0] for call in calls]
	assert "pull" in verbs and "push" in verbs, verbs
	assert verbs.index("pull") < verbs.index("push"), verbs
	return proc, calls


def test_commit_step_classifies_ruleset_rejection() -> None:
	proc, _ = _run_commit_step(GH013_PUSH_OUTPUT, 1)
	assert proc.returncode != 0
	assert (
		"::error::VALIDATION_SELFTEST_STATUS outcome=failed reason=push_rejected_by_ruleset branch=main token=gh_pat"
		in proc.stdout
	), proc.stdout
	assert "outcome=pushed" not in proc.stdout
	assert "GH013" in proc.stderr


def test_commit_step_reports_other_push_failures() -> None:
	proc, _ = _run_commit_step(
		"fatal: unable to access 'https://x-access-token:secret@github.com/o/r/': Could not resolve host\n",
		128,
	)
	assert proc.returncode != 0
	assert "::error::VALIDATION_SELFTEST_STATUS outcome=failed reason=push_failed branch=main token=gh_pat rc=128" in proc.stdout
	assert "push_rejected_by_ruleset" not in proc.stdout
	assert "x-access-token:secret@" not in proc.stderr
	assert "https://***@github.com" in proc.stderr


def test_commit_step_success_logs_pushed() -> None:
	proc, _ = _run_commit_step("To https://github.com/o/r\n   abc..def  HEAD -> main\n", 0)
	assert proc.returncode == 0, proc.stderr
	assert "VALIDATION_SELFTEST_STATUS outcome=pushed branch=main token=gh_pat" in proc.stdout
	assert "::error::" not in proc.stdout


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
	test_workflow_has_nightly_schedule_and_manual_dispatch()
	test_workflow_runs_matrix_and_always_uploads_artifacts()
	test_workflow_commits_only_status_file_when_changed()
	test_checkout_does_not_persist_credentials_for_generated_tests()
	test_status_commit_runs_in_separate_job_from_generated_tests()
	test_status_push_uses_pat_and_classifies_ruleset_rejection()
	test_commit_step_classifies_ruleset_rejection()
	test_commit_step_reports_other_push_failures()
	test_commit_step_success_logs_pushed()
	test_workflow_emits_machine_readable_summary_to_step_summary()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

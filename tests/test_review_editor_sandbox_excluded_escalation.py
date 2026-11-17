"""Sandbox-excluded escalation in the "Post editor summary comment" step (#8321).

PR #8250 was re-reviewed 29 times on unchanged heads: the editor declined its
only fix as "sandbox-excluded path" and the run ended as a success, so the
gate's identical-failure cap never counted it. The step now ends the posted
editor summary with a review-autofix-failure:v1 marker for the non-retryable
reason review_editor_sandbox_path_excluded when the editor changed nothing and
declined at least one finding for that reason. These tests run the real step
script with a fake ``gh`` function that captures the posted body.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STEP_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_post_editor_summary.sh"
HEAL_PY = REPO_ROOT / "scripts" / "workflow_failure_heal.py"
REASON = "review_editor_sandbox_path_excluded"
HEAD = "c" * 40
OTHER_HEAD = "d" * 40
SUPPORT = "e" * 40
PIPELINE_LOGIN = "workflow-pat-user"

# Sourced by the step as ${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh. A shell
# function rather than an executable on PATH, so the stub still works when the
# temp directory is mounted noexec (the pre-push test container).
FAKE_GH = """# Records the -f body= value of each call; never touches the network.
gh() {
  local prev="" arg
  for arg in "$@"; do
    if [ "${prev}" = "-f" ] && [ "${arg#body=}" != "${arg}" ]; then
      printf '%s' "${arg#body=}" > "${FAKE_GH_BODY_FILE}"
    fi
    prev="${arg}"
  done
  return 0
}
gh_retry() { "$@"; }
"""


def _summary(*, status: str = "not-edited", changes: str = "- none", ignored: str = "", comment_audit: str = "- none") -> str:
	return (
		f"Changes made:\n{changes}\n\n"
		f"Change status:\n- {status}\n\n"
		"Already satisfied (suggested but already present):\n- none\n\n"
		f"Ignored suggestions (with short reason):\n{ignored or '- none'}\n\n"
		"Reviewer files processed:\n- previous_reviews/review_1.md — checksum abc — used\n\n"
		"Review file issue audit:\n- previous_reviews/review_1.md — total issues listed: 1; issues applied: 0; issues already applied: 0; issues ignored: 1\n\n"
		f"PR comment audit:\n{comment_audit}\n\n"
		"Regression fingerprint:\n- none\n\n"
		"Runtime failure path:\n- none\n"
	)


# Shaped like the last editor summary on PR #8250: the CI failure is in a
# .claude/docs/ reference file the sandbox did not admit.
PR_8250_SUMMARY = _summary(
	ignored=(
		"- CI failure in tests/test_no_session_automation.py points at .claude/docs/s25-session-automation.md; "
		"that file is not in the editor workspace — \"sandbox-excluded path\".\r\n"
		"- Reviewer style nit on wording in README.md — out of scope (stylistic)"
	),
)


# The commit step writes "- none" when it committed nothing (review_commit_changes.sh).
def _run_step(tmp_path: Path, summary: str, *, committed: str | None = "- none\n", heal_py: Path | None = HEAL_PY, head: str = HEAD, extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], str, str]:
	support_dir = tmp_path / "support"
	support_dir.mkdir(exist_ok=True)
	(support_dir / "gh_helpers.sh").write_text(FAKE_GH, encoding="utf-8")
	runtime_dir = tmp_path / "runtime"
	runtime_dir.mkdir(exist_ok=True)
	summary_file = tmp_path / "editor_summary.txt"
	summary_file.write_text(summary, encoding="utf-8")
	committed_file = tmp_path / "committed_files.txt"
	if committed is not None:
		committed_file.write_text(committed, encoding="utf-8")
	github_env = tmp_path / "github_env"
	github_env.write_text("", encoding="utf-8")
	body_file = tmp_path / "posted_body.txt"
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"HOME": str(tmp_path),
		"PYTHONDONTWRITEBYTECODE": "1",
		"FAKE_GH_BODY_FILE": str(body_file),
		"SUPPORT_SCRIPTS_DIR": str(support_dir),
		"RUNTIME_DIR": str(runtime_dir),
		"EDITOR_SUMMARY_FILE": str(summary_file),
		"COMMITTED_FILES_FILE": str(committed_file),
		"PR_EDITOR_COMMENT_FILE": str(tmp_path / "pr_editor_comment.txt"),
		"GITHUB_ENV": str(github_env),
		"PR_NUMBER": "8250",
		"AUTOFIX_STEP_REPOSITORY": "owner/repo",
		"AUTOFIX_STEP_SERVER_URL": "https://github.com",
		"AUTOFIX_STEP_RUN_ID": "4242",
		"GITHUB_RUN_ID": "4242",
		"AUTOFIX_FAILURE_HEAD_SHA": head,
		"REVIEW_SUPPORT_SHA": SUPPORT,
	}
	if heal_py is not None:
		env["AUTOFIX_FAILURE_HEAL_PY"] = str(heal_py)
	if extra_env:
		env.update(extra_env)
	result = subprocess.run(["bash", str(STEP_SCRIPT)], capture_output=True, text=True, env=env, timeout=120)
	body = body_file.read_text(encoding="utf-8") if body_file.exists() else ""
	return result, body, github_env.read_text(encoding="utf-8")


def _escalation_lines(output: str) -> list[str]:
	return [line for line in output.splitlines() if line.startswith("AUTOFIX_SANDBOX_EXCLUDED_ESCALATION ")]


def _count(tmp_path: Path, body: str, head: str) -> dict[str, str]:
	comments = tmp_path / "comments.json"
	comments.write_text(json.dumps([{"id": 1, "author_login": PIPELINE_LOGIN, "body": body}]), encoding="utf-8")
	result = subprocess.run(
		["python3", str(HEAL_PY), "autofix-identical-failure-count", "--comments-json", str(comments), "--head-sha", head, "--author-login", PIPELINE_LOGIN, "--support-sha", SUPPORT],
		capture_output=True, text=True, check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
	)
	return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_not_edited_run_with_sandbox_excluded_bullet_ends_with_marker(tmp_path: Path) -> None:
	summary = _summary(ignored="- .claude/docs/s25-x.md needs an edit — sandbox-excluded path")
	result, body, github_env = _run_step(tmp_path, summary)
	assert result.returncode == 0, result.stderr + result.stdout
	last_line = body.rstrip().splitlines()[-1]
	assert last_line.startswith("<!-- review-autofix-failure:v1 ")
	assert f"head={HEAD}" in last_line and f"reason={REASON}" in last_line and f"support={SUPPORT}" in last_line and "run=4242" in last_line
	assert body.startswith("AI autofix editor summary")
	assert _escalation_lines(result.stdout) == [f"AUTOFIX_SANDBOX_EXCLUDED_ESCALATION pr=8250 head={HEAD} findings=1 outcome=marker"]
	# The step neither fails the run nor sets a failure reason for later steps.
	assert "AUTOFIX_FAILURE_REASON" not in github_env
	assert "EDITOR_SUMMARY_POSTED=true" in github_env


@pytest.mark.parametrize(
	"summary, committed",
	[
		# The editor edited files.
		(_summary(status="edited", changes="- scripts/x.py: fixed", ignored="- .claude/docs/a.md — sandbox-excluded path"), ""),
		# Only other ignore reasons.
		(_summary(ignored="- README.md nit — out of scope (stylistic)\n- tests/x.py — already satisfied"), ""),
		# Something was committed even though the editor said not-edited.
		(_summary(ignored="- .claude/docs/a.md — sandbox-excluded path"), "- scripts/x.py\n"),
		# A committed path that merely starts with "none " is not the "- none" sentinel.
		(_summary(ignored="- .claude/docs/a.md — sandbox-excluded path"), "- none notes.md\n"),
		# No committed-files evidence at all: unknown, so no marker.
		(_summary(ignored="- .claude/docs/a.md — sandbox-excluded path"), None),
		# The reason appears only under PR comment audit.
		(_summary(comment_audit="- entry 1 by bot — .claude/docs/a.md — ignored — sandbox-excluded path"), ""),
		# The reason is mentioned mid-bullet, not as the bullet's reason.
		(_summary(ignored="- sandbox-excluded path was considered but the finding is out of scope"), ""),
	],
)
def test_no_marker_outside_the_sandbox_excluded_not_edited_case(tmp_path: Path, summary: str, committed: str) -> None:
	result, body, github_env = _run_step(tmp_path, summary, committed=committed)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "review-autofix-failure:v1" not in body
	assert _escalation_lines(result.stdout) == []
	assert "AUTOFIX_FAILURE_REASON" not in github_env


@pytest.mark.parametrize("committed", ["", "- none\n", "- none (edits withheld for safety: write_guard)\n", "- commit skipped (branch is not writable from this workflow)\n"])
def test_placeholder_committed_files_count_as_nothing_committed(tmp_path: Path, committed: str) -> None:
	summary = _summary(ignored="- .claude/docs/a.md — sandbox-excluded path")
	result, body, _ = _run_step(tmp_path, summary, committed=committed)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "review-autofix-failure:v1" in body
	assert _escalation_lines(result.stdout) == [f"AUTOFIX_SANDBOX_EXCLUDED_ESCALATION pr=8250 head={HEAD} findings=1 outcome=marker"]


def test_did_commit_true_writes_no_marker_even_with_placeholder_text(tmp_path: Path) -> None:
	"""A committed root file named "none" prints as "- none"; DID_COMMIT=true still wins."""
	summary = _summary(ignored="- .claude/docs/a.md — sandbox-excluded path")
	result, body, _ = _run_step(tmp_path, summary, committed="- none\n", extra_env={"DID_COMMIT": "true"})
	assert result.returncode == 0, result.stderr + result.stdout
	assert "review-autofix-failure:v1" not in body
	assert _escalation_lines(result.stdout) == []


def test_missing_helper_logs_marker_unavailable_and_still_posts(tmp_path: Path) -> None:
	summary = _summary(ignored="- .claude/docs/a.md — sandbox-excluded path")
	result, body, _ = _run_step(tmp_path, summary, heal_py=tmp_path / "absent_helper.py")
	assert result.returncode == 0, result.stderr + result.stdout
	assert body.startswith("AI autofix editor summary") and "review-autofix-failure:v1" not in body
	assert _escalation_lines(result.stdout) == [f"AUTOFIX_SANDBOX_EXCLUDED_ESCALATION pr=8250 head={HEAD} findings=1 outcome=marker_unavailable reason=helper_missing"]


def test_invalid_head_logs_unknown_and_writes_no_marker(tmp_path: Path) -> None:
	summary = _summary(ignored="- .claude/docs/a.md — sandbox-excluded path")
	result, body, _ = _run_step(tmp_path, summary, head="not-a-sha")
	assert result.returncode == 0, result.stderr + result.stdout
	assert "review-autofix-failure:v1" not in body
	assert _escalation_lines(result.stdout) == ["AUTOFIX_SANDBOX_EXCLUDED_ESCALATION pr=8250 head=unknown findings=1 outcome=marker_unavailable reason=no_marker"]


def test_pr_8250_shaped_summary_trips_the_cap_on_the_next_dispatch(tmp_path: Path) -> None:
	"""Regression: the posted body alone makes the gate's count non-retryable."""
	result, body, _ = _run_step(tmp_path, PR_8250_SUMMARY)
	assert result.returncode == 0, result.stderr + result.stdout
	assert _escalation_lines(result.stdout) == [f"AUTOFIX_SANDBOX_EXCLUDED_ESCALATION pr=8250 head={HEAD} findings=1 outcome=marker"]
	counted = _count(tmp_path, body, HEAD)
	assert counted["count"] == "1" and counted["reason"] == REASON and counted["non_retryable"] == "true"
	# A new push (another head) resets the cap.
	reset = _count(tmp_path, body, OTHER_HEAD)
	assert reset["count"] == "0" and reset["non_retryable"] == "false"


def test_prompt_pins_the_reason_string_the_detector_reads() -> None:
	prompt = (REPO_ROOT / "scripts" / "review_apply_fixes.sh").read_text(encoding="utf-8")
	assert 'with the reason "sandbox-excluded path"' in prompt
	assert 'with reason "sandbox-excluded path"' in prompt
	step = STEP_SCRIPT.read_text(encoding="utf-8")
	assert "sandbox-excluded path$" in step and "--failure-reason review_editor_sandbox_path_excluded" in step

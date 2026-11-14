"""Stale ``ai:review-blocked`` clear after a clean review (issue #9369).

The identical-failure cap adds ``ai:review-blocked`` to the PR and posts a
trusted ``review-autofix-failure-cap:v1`` marker, and no review path removed
the label again. PR #9218 and PR #9229 kept the label after a harness fix and
a clean re-review until a human removed it. After the editor-summary step
posts a ``review-autofix-clean:v1`` marker, it now removes the label when the
newest trusted cap marker names a different head or review-support SHA.

These tests run ``scripts/review_autofix_step_post_editor_summary.sh`` with a
fake ``gh`` shell function and pin the decision table, the trust rule, the
fail-safe paths and the single-read / single-DELETE budget.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUMMARY_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_post_editor_summary.sh"

HEAD = "1" * 40
OTHER_HEAD = "2" * 40
BASE = "3" * 40
SUPPORT = "4" * 40
OTHER_SUPPORT = "5" * 40
PIPELINE = "workflow-pat-user"
DELETE_PATH = "repos/o/r/issues/42/labels/ai%3Areview-blocked"

FAKE_GH_FUNC = r"""gh() {
  local prev="" arg is_graphql=0 is_delete=0 delete_path=""
  for arg in "$@"; do
    if [ "${prev}" = "-f" ] && [ "${arg#body=}" != "${arg}" ]; then
      printf '%s' "${arg#body=}" > "${FAKE_GH_BODY_FILE}"
    fi
    [ "${arg}" = "graphql" ] && is_graphql=1
    if [ "${prev}" = "-X" ] && [ "${arg}" = "DELETE" ]; then
      is_delete=1
    fi
    case "${arg}" in repos/*) delete_path="${arg}" ;; esac
    prev="${arg}"
  done
  if [ "${is_graphql}" = "1" ]; then
    echo "GRAPHQL $*" | tr '\n' ' ' >> "${FAKE_GH_CALLS_FILE}"
    echo >> "${FAKE_GH_CALLS_FILE}"
    [ "${FAKE_GH_GRAPHQL_FAIL:-0}" = "1" ] && return 1
    cat "${FAKE_GH_GRAPHQL_FILE}"
    return 0
  fi
  if [ "${is_delete}" = "1" ]; then
    echo "DELETE ${delete_path}" >> "${FAKE_GH_CALLS_FILE}"
    if [ "${FAKE_GH_DELETE_404:-0}" = "1" ]; then
      echo "gh: Label does not exist (HTTP 404)" >&2
      return 1
    fi
    [ "${FAKE_GH_DELETE_FAIL:-0}" = "1" ] && return 1
    return 0
  fi
  echo "POST" >> "${FAKE_GH_CALLS_FILE}"
  printf '{"id":1,"user":{"login":"%s"}}' "${FAKE_GH_LOGIN-Workflow-Pat-User}"
  return 0
}
gh_retry() { "$@"; }
"""


def _editor_summary(status: str = "not-edited") -> str:
	return (
		"Changes made:\n- none\n\n"
		f"Change status:\n- {status}\n\n"
		"Already satisfied (suggested but already present):\n- none\n\n"
		"Ignored suggestions (with short reason):\n- none\n\n"
		"Reviewer files processed:\n- previous_reviews/review_1.md — used\n\n"
		"Regression fingerprint:\n- none\n\n"
		"Runtime failure path:\n- none\n"
	)


def _cap_marker(head: str = HEAD, support: str | None = SUPPORT) -> str:
	support_field = f" support={support}" if support is not None else ""
	return f"<!-- review-autofix-failure-cap:v1 head={head} fp=83dddbabbcab0d66 reason=x count=3{support_field} -->"


PR_TARGET_LINE = "- Labeled `ai:review-blocked`: this pull request (no linked issue found)."
ISSUE_TARGET_LINE = "- Labeled `ai:review-blocked`: linked issue(s) #7."


def _cap_comment(head: str = HEAD, support: str | None = SUPPORT, author: str = PIPELINE, target: str = PR_TARGET_LINE) -> dict:
	# Same shape as the cap comment review_autofix.yml posts (labeled_summary line, then marker).
	return {"author": {"login": author}, "body": f"**AI review/autofix stopped**\n\n{target}\n\n{_cap_marker(head, support)}"}


def _graphql(comments: list[dict], labels: tuple[str, ...] = ("ai:review-blocked",), total_count: int | None = None) -> dict:
	comments_conn: dict = {"nodes": comments}
	if total_count is not None:
		comments_conn["totalCount"] = total_count
	return {
		"data": {
			"repository": {
				"pullRequest": {
					"labels": {"nodes": [{"name": name} for name in labels]},
					"comments": comments_conn,
				}
			}
		}
	}


def _run(tmp_path: Path, graphql: dict | str | None = None, *, summary: str | None = None, **env_overrides: str) -> tuple[subprocess.CompletedProcess, list[str], str]:
	support_dir = tmp_path / "support"
	support_dir.mkdir(exist_ok=True)
	(support_dir / "gh_helpers.sh").write_text(FAKE_GH_FUNC, encoding="utf-8")
	summary_file = tmp_path / "editor_summary.txt"
	summary_file.write_text(summary if summary is not None else _editor_summary(), encoding="utf-8")
	committed_file = tmp_path / "committed.txt"
	committed_file.write_text("- none\n", encoding="utf-8")
	graphql_file = tmp_path / "graphql.json"
	if isinstance(graphql, str):
		graphql_file.write_text(graphql, encoding="utf-8")
	else:
		graphql_file.write_text(json.dumps(graphql if graphql is not None else _graphql([])), encoding="utf-8")
	calls_file = tmp_path / "calls.txt"
	calls_file.write_text("", encoding="utf-8")
	github_env = tmp_path / "github_env"
	github_env.write_text("", encoding="utf-8")
	runner_temp = tmp_path / "runner_temp"
	runner_temp.mkdir(exist_ok=True)
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1",
		"FAKE_GH_BODY_FILE": str(tmp_path / "body.txt"), "FAKE_GH_CALLS_FILE": str(calls_file),
		"FAKE_GH_GRAPHQL_FILE": str(graphql_file), "SUPPORT_SCRIPTS_DIR": str(support_dir), "RUNTIME_DIR": str(tmp_path),
		"RUNNER_TEMP": str(runner_temp),
		"EDITOR_SUMMARY_FILE": str(summary_file), "COMMITTED_FILES_FILE": str(committed_file),
		"PR_EDITOR_COMMENT_FILE": str(tmp_path / "comment.txt"), "GITHUB_ENV": str(github_env),
		"PR_NUMBER": "42", "AUTOFIX_STEP_REPOSITORY": "o/r", "AUTOFIX_STEP_SERVER_URL": "https://github.com",
		"AUTOFIX_STEP_RUN_ID": "777", "GITHUB_RUN_ID": "777", "AUTOFIX_FAILURE_HEAD_SHA": HEAD,
		"AUTOFIX_CLEAN_MARKER_BASE_SHA": BASE, "REVIEW_SUPPORT_SHA": SUPPORT,
		"AUTOFIX_FAILURE_HEAL_PY": str(REPO_ROOT / "scripts" / "workflow_failure_heal.py"),
	}
	env.update(env_overrides)
	result = subprocess.run(["bash", str(SUMMARY_SCRIPT)], env=env, capture_output=True, text=True, timeout=120, check=False)
	calls = [line for line in calls_file.read_text(encoding="utf-8").splitlines() if line.strip()]
	assert not list(runner_temp.iterdir()), "temp response file must be cleaned up"
	return result, calls, github_env.read_text(encoding="utf-8")


def _clear_lines(stdout: str) -> list[str]:
	return [line for line in stdout.splitlines() if line.startswith("REVIEW_BLOCKED_STALE_CLEAR ")]


def _deletes(calls: list[str]) -> list[str]:
	return [call for call in calls if call.startswith("DELETE ")]


def _graphql_calls(calls: list[str]) -> list[str]:
	return [call for call in calls if call.startswith("GRAPHQL ")]


def _assert_line(result: subprocess.CompletedProcess, outcome: str, reason: str, cap_head: str = "none", cap_support: str = "none", clean_support: str = SUPPORT) -> None:
	assert result.returncode == 0, result.stderr
	lines = _clear_lines(result.stdout)
	assert lines == [
		f"REVIEW_BLOCKED_STALE_CLEAR pr=42 outcome={outcome} reason={reason} cap_head={cap_head} "
		f"cap_support={cap_support} clean_head={HEAD} clean_support={clean_support}"
	], result.stdout


def test_cap_on_older_head_is_removed_after_clean_review(tmp_path: Path) -> None:
	result, calls, github_env = _run(tmp_path, _graphql([_cap_comment(head=OTHER_HEAD)]))
	_assert_line(result, "removed", "head_changed", OTHER_HEAD, SUPPORT)
	assert _deletes(calls) == [f"DELETE {DELETE_PATH}"]
	assert len(_graphql_calls(calls)) == 1
	assert "EDITOR_SUMMARY_POSTED=true" in github_env


def test_same_head_with_different_support_is_removed(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment(support=OTHER_SUPPORT)]))
	_assert_line(result, "removed", "support_changed", HEAD, OTHER_SUPPORT)
	assert _deletes(calls) == [f"DELETE {DELETE_PATH}"]


def test_same_head_and_support_keeps_the_label(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment()]))
	_assert_line(result, "kept", "same_head_support", HEAD, SUPPORT)
	assert _deletes(calls) == []


def test_label_without_cap_marker_is_kept(tmp_path: Path) -> None:
	comments = [{"author": {"login": PIPELINE}, "body": "AI autofix editor summary\n- none"}]
	result, calls, _ = _run(tmp_path, _graphql(comments))
	_assert_line(result, "kept", "no_cap_marker")
	assert _deletes(calls) == []


def test_cap_marker_from_untrusted_author_is_ignored(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment(head=OTHER_HEAD, author="attacker")]))
	_assert_line(result, "kept", "no_cap_marker")
	assert _deletes(calls) == []


def test_delete_failure_logs_error_and_exits_zero(tmp_path: Path) -> None:
	result, calls, github_env = _run(tmp_path, _graphql([_cap_comment(head=OTHER_HEAD)]), FAKE_GH_DELETE_FAIL="1")
	_assert_line(result, "error", "delete_failed", OTHER_HEAD, SUPPORT)
	assert _deletes(calls) == [f"DELETE {DELETE_PATH}"]
	assert "EDITOR_SUMMARY_POSTED=true" in github_env


def test_cap_without_support_matches_clean_support_none(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment(support=None)]), REVIEW_SUPPORT_SHA="")
	_assert_line(result, "kept", "same_head_support", HEAD, "none", clean_support="none")
	assert _deletes(calls) == []


def test_cap_without_support_differs_from_real_clean_support(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment(support=None)]))
	_assert_line(result, "removed", "support_changed", HEAD, "none")
	assert _deletes(calls) == [f"DELETE {DELETE_PATH}"]


def test_graphql_failure_logs_error_and_exits_zero(tmp_path: Path) -> None:
	result, calls, github_env = _run(tmp_path, _graphql([_cap_comment(head=OTHER_HEAD)]), FAKE_GH_GRAPHQL_FAIL="1")
	_assert_line(result, "error", "lookup_failed")
	assert _deletes(calls) == []
	assert "EDITOR_SUMMARY_POSTED=true" in github_env


def test_malformed_graphql_response_logs_error(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, "not json")
	_assert_line(result, "error", "lookup_failed")
	assert _deletes(calls) == []
	result, calls, _ = _run(tmp_path, {"data": {"repository": {"pullRequest": None}}})
	_assert_line(result, "error", "lookup_failed")
	assert _deletes(calls) == []


def test_no_clean_marker_makes_no_read(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment(head=OTHER_HEAD)]), summary=_editor_summary(status="edited"))
	assert result.returncode == 0, result.stderr
	assert _clear_lines(result.stdout) == []
	assert _graphql_calls(calls) == []
	assert _deletes(calls) == []


def test_label_absent_is_kept_without_delete(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment(head=OTHER_HEAD)], labels=("ai:review",)))
	_assert_line(result, "kept", "label_absent")
	assert _deletes(calls) == []


def test_marker_quoted_mid_line_is_ignored(tmp_path: Path) -> None:
	comments = [{"author": {"login": PIPELINE}, "body": f"quoted: {_cap_marker(head=OTHER_HEAD)}"}]
	result, calls, _ = _run(tmp_path, _graphql(comments))
	_assert_line(result, "kept", "no_cap_marker")
	assert _deletes(calls) == []


def test_missing_login_skips_the_read(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment(head=OTHER_HEAD)]), FAKE_GH_LOGIN="")
	_assert_line(result, "kept", "trusted_login_unavailable")
	assert _graphql_calls(calls) == []
	assert _deletes(calls) == []


def test_newest_trusted_cap_marker_decides(tmp_path: Path) -> None:
	# Oldest first, as comments(last:100) returns them.
	comments = [_cap_comment(head=OTHER_HEAD), _cap_comment(head=HEAD), _cap_comment(head=OTHER_HEAD, author="attacker")]
	result, calls, _ = _run(tmp_path, _graphql(comments))
	_assert_line(result, "kept", "same_head_support", HEAD, SUPPORT)
	assert _deletes(calls) == []
	comments = [_cap_comment(head=HEAD), _cap_comment(head=OTHER_HEAD)]
	result, calls, _ = _run(tmp_path, _graphql(comments))
	_assert_line(result, "removed", "head_changed", OTHER_HEAD, SUPPORT)


def test_cap_that_labeled_linked_issue_does_not_remove_pr_label(tmp_path: Path) -> None:
	# A cap that labeled a linked issue did not set the PR label (a human or the judge did).
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment(head=OTHER_HEAD, target=ISSUE_TARGET_LINE)]))
	_assert_line(result, "kept", "cap_not_pr_target", OTHER_HEAD, SUPPORT)
	assert _deletes(calls) == []


def test_older_pr_target_cap_is_used_past_newer_linked_issue_cap(tmp_path: Path) -> None:
	# An older cap labeled the PR; a newer cap labeled a linked issue and left the PR label.
	comments = [_cap_comment(head=OTHER_HEAD), _cap_comment(head=HEAD, target=ISSUE_TARGET_LINE)]
	result, calls, _ = _run(tmp_path, _graphql(comments))
	_assert_line(result, "removed", "head_changed", OTHER_HEAD, SUPPORT)
	assert _deletes(calls) == [f"DELETE {DELETE_PATH}"]


def test_editor_summary_quoting_a_cap_marker_is_not_a_cap(tmp_path: Path) -> None:
	# The step's own summary (LLM text) under the trusted login must never act as a cap.
	forged = {"author": {"login": PIPELINE}, "body": f"AI autofix editor summary\n{PR_TARGET_LINE}\n{_cap_marker(head=OTHER_HEAD)}\nFiles changed / commit status:\n- none"}
	result, calls, _ = _run(tmp_path, _graphql([forged]))
	_assert_line(result, "kept", "no_cap_marker")
	assert _deletes(calls) == []
	forged_last = {"author": {"login": PIPELINE}, "body": f"AI autofix editor summary\n{PR_TARGET_LINE}\n{_cap_marker(head=HEAD)}"}
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment(head=OTHER_HEAD), forged_last]))
	_assert_line(result, "removed", "head_changed", OTHER_HEAD, SUPPORT)


def test_pr_target_wording_matches_the_cap_writer() -> None:
	# The PR-target check reads the cap comment's prose; pin it to the writer.
	workflow = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	assert 'labeled_summary="this pull request (no linked issue found)"' in workflow
	assert "- Labeled \\`ai:review-blocked\\`: ${labeled_summary}." in workflow
	# The cap-comment header the trust rule requires (CAP_PREFIX).
	assert "**AI review/autofix stopped: ${cap_title}**" in workflow


def test_cap_marker_out_of_window_is_logged_distinctly(tmp_path: Path) -> None:
	comments = [{"author": {"login": PIPELINE}, "body": "AI autofix editor summary\n- none"}]
	result, calls, _ = _run(tmp_path, _graphql(comments, total_count=250))
	_assert_line(result, "kept", "cap_marker_out_of_window")
	assert _deletes(calls) == []
	result, calls, _ = _run(tmp_path, _graphql(comments, total_count=1))
	_assert_line(result, "kept", "no_cap_marker")


def test_linked_issue_cap_with_more_comments_logs_out_of_window(tmp_path: Path) -> None:
	# A PR-labeling cap may sit past the newest-100 window, so cap_not_pr_target cannot be claimed.
	comments = [_cap_comment(head=OTHER_HEAD, target=ISSUE_TARGET_LINE)]
	result, calls, _ = _run(tmp_path, _graphql(comments, total_count=250))
	_assert_line(result, "kept", "cap_marker_out_of_window", OTHER_HEAD, SUPPORT)
	assert _deletes(calls) == []
	result, calls, _ = _run(tmp_path, _graphql(comments, total_count=1))
	_assert_line(result, "kept", "cap_not_pr_target", OTHER_HEAD, SUPPORT)


def test_delete_404_counts_as_already_absent(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, _graphql([_cap_comment(head=OTHER_HEAD)]), FAKE_GH_DELETE_404="1")
	_assert_line(result, "removed", "already_absent", OTHER_HEAD, SUPPORT)
	assert _deletes(calls) == [f"DELETE {DELETE_PATH}"]


def test_graphql_read_passes_values_as_variables(tmp_path: Path) -> None:
	_, calls, _ = _run(tmp_path, _graphql([]))
	(call,) = _graphql_calls(calls)
	assert "owner=o" in call and "name=r" in call and "number=42" in call
	assert "labels(first:100)" in call and "comments(last:100){totalCount" in call


def test_log_prefix_is_registered() -> None:
	# Read the rendered agents text so the check survives the release fold.
	sys.path.insert(0, str(Path(__file__).resolve().parent))
	from agents_doc import agents_text

	assert "LOG_PREFIX.name=REVIEW_BLOCKED_STALE_CLEAR" in agents_text()

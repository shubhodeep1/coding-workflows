#!/usr/bin/env python3
"""Behavioural tests for scripts/review_merge_train.sh (the merge train).

The script is driven through a fake `gh` placed first on PATH. The fake
serves canned JSON for the REST endpoints the script reads and records every
write (label add/remove, comment upsert, workflow dispatch) to a log file
the tests assert on. No network, no real repository.

Scenario pinned here (tele-funtoken-msg-scoring, 2026-09-07): fourteen
auto-heal issues opened for one incident produced ten ai/issue-* PRs that
all edit backend/promo_email_sender.py against main; every merge conflicted
every remaining sibling. With the train, a younger PR whose files overlap an
older open ai/issue-* PR is labelled ai:merge-queued and soft-exits; once
the older PR merges, `release` removes the label and re-dispatches review.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_merge_train.sh"

FAKE_GH = r'''#!/usr/bin/env bash
# Fake gh: serves fixtures from $FAKE_GH_DIR and logs writes to $FAKE_GH_LOG.
set -euo pipefail
args=("$@")
printf '%s\n' "gh ${args[*]}" >> "${FAKE_GH_LOG}"
if [ "${1:-}" = "workflow" ] && [ "${2:-}" = "run" ]; then
  if [ -f "${FAKE_GH_DIR}/dispatch_fail" ]; then exit 1; fi
  exit 0
fi
if [ "${1:-}" = "label" ]; then exit 0; fi
if [ "${1:-}" != "api" ]; then echo "unexpected: $*" >&2; exit 1; fi
method="GET"; endpoint=""
shift
while [ $# -gt 0 ]; do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    --paginate|--jq|-f) [ "$1" = "--paginate" ] && shift || shift 2 ;;
    *) if [ -z "${endpoint}" ]; then endpoint="$1"; fi; shift ;;
  esac
done
# jq filter is the last --jq value
jqf=""
for ((i=0; i<${#args[@]}; i++)); do
  if [ "${args[$i]}" = "--jq" ]; then jqf="${args[$((i+1))]}"; fi
done
path="${endpoint%%\?*}"
case "${method}" in
  GET)
    if [ -f "${FAKE_GH_DIR}/fail_get" ]; then exit 1; fi
    case "${path}" in
      repos/*/pulls) fixture="${FAKE_GH_DIR}/pulls.json" ;;
      repos/*/pulls/*/files) n="${path#*/pulls/}"; n="${n%%/*}"; fixture="${FAKE_GH_DIR}/files_${n}.json" ;;
      repos/*/issues/*/comments) n="${path#*/issues/}"; n="${n%%/*}"; fixture="${FAKE_GH_DIR}/comments_${n}.json" ;;
      repos/*/issues/comments/*) fixture="${FAKE_GH_DIR}/comment_body.json" ;;
      repos/*/actions/runs) fixture="${FAKE_GH_DIR}/actions_runs.json" ;;
      *) echo "unexpected GET ${path}" >&2; exit 1 ;;
    esac
    if [[ "${path}" == repos/*/issues/*/comments ]] && [[ "${jqf}" == *"merge-train:released"* ]] && [ -f "${FAKE_GH_DIR}/fail_released_comment_lookup" ]; then
      exit 1
    fi
    if [ ! -f "${fixture}" ]; then
      case "${path}" in
        repos/*/actions/runs) echo '{"workflow_runs":[]}' ;;
        *) echo '[]' ;;
      esac
      exit 0
    fi
    if [ -n "${jqf}" ]; then jq -r "${jqf}" "${fixture}"; else cat "${fixture}"; fi
    ;;
  DELETE)
    if [ -f "${FAKE_GH_DIR}/label_delete_fail" ]; then exit 1; fi
    exit 0
    ;;
  POST)
    if [[ "${path}" == repos/*/issues/*/labels ]] && [ -f "${FAKE_GH_DIR}/label_post_fail" ]; then exit 1; fi
    exit 0
    ;;
  *) exit 0 ;;
esac
'''


def _install_fake_gh(tmp_path: Path) -> tuple[Path, Path, Path]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
	fixtures = tmp_path / "fixtures"
	fixtures.mkdir()
	log = tmp_path / "gh.log"
	log.touch()
	return bin_dir, fixtures, log


def _pr(number: int, head: str, base: str = "main", labels: list[str] | None = None, draft: bool = False) -> dict:
	return {
		"number": number,
		"head": {"ref": head},
		"base": {"ref": base},
		"draft": draft,
		"labels": [{"name": name} for name in (labels or [])],
	}


def _write_files(fixtures: Path, number: int, paths: list[str]) -> None:
	(fixtures / f"files_{number}.json").write_text(
		json.dumps([{"filename": p} for p in paths]), encoding="utf-8"
	)


def _run(subcommand: str, tmp_path: Path, bin_dir: Path, fixtures: Path, log: Path, **env: str) -> tuple[subprocess.CompletedProcess, str, dict]:
	github_env = tmp_path / "github_env"
	github_env.touch()
	run_env = dict(os.environ)
	# The review workflow exports these names in the editor process. Tests must
	# opt in explicitly rather than inherit the live PR's paths or base branch.
	for inherited_name in ("BASE_BRANCH", "PR_DIFF_FILE", "PR_NUMBER", "TARGET_BRANCH"):
		run_env.pop(inherited_name, None)
	run_env.update({
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"FAKE_GH_DIR": str(fixtures),
		"FAKE_GH_LOG": str(log),
		"GITHUB_REPOSITORY": "acme/consumer",
		"GITHUB_ENV": str(github_env),
		"GH_TOKEN": "x",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	run_env.update(env)
	result = subprocess.run(
		["bash", str(SCRIPT), subcommand], env=run_env, capture_output=True, text=True, cwd=tmp_path,
	)
	env_lines = {}
	for line in github_env.read_text(encoding="utf-8").splitlines():
		if "=" in line:
			k, v = line.split("=", 1)
			env_lines[k] = v
	return result, log.read_text(encoding="utf-8"), env_lines


def test_gate_queues_younger_overlapping_pr(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4075, "ai/issue-4063"),
		_pr(4077, "ai/issue-4064"),
	]), encoding="utf-8")
	_write_files(fixtures, 4075, ["backend/promo_email_sender.py", "backend/README.md"])
	diff = tmp_path / "pr.diff"
	diff.write_text(
		"diff --git a/backend/promo_email_sender.py b/backend/promo_email_sender.py\n"
		"--- a/backend/promo_email_sender.py\n+++ b/backend/promo_email_sender.py\n"
		"diff --git a/tests/test_promo_email_sender.py b/tests/test_promo_email_sender.py\n",
		encoding="utf-8",
	)
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064", PR_DIFF_FILE=str(diff),
	)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_GATE pr=4077 base=main result=queued blockers=#4075" in result.stdout
	assert env_out.get("AUTOFIX_MERGE_QUEUED") == "true"
	assert env_out.get("AUTOFIX_STALE_BASE_SKIP") == "true"
	assert env_out.get("AUTOFIX_MERGE_QUEUED_BLOCKERS") == "#4075"
	assert "issues/4077/labels" in log_text and "labels[]=ai:merge-queued" in log_text
	assert "POST repos/acme/consumer/issues/4077/comments" in log_text
	# Own paths came from PR_DIFF_FILE: no pulls/4077/files call.
	assert "pulls/4077/files" not in log_text


def test_gate_continues_when_older_pr_is_disjoint_or_younger(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4075, "ai/issue-4063"),        # older, disjoint
		_pr(4077, "ai/issue-4064"),        # this PR
		_pr(4091, "ai/issue-4079"),        # younger, overlapping — must not block
		_pr(4080, "feature/manual"),       # older, non-ai branch — ignored
	]), encoding="utf-8")
	_write_files(fixtures, 4075, ["docs/other.md"])
	_write_files(fixtures, 4091, ["backend/promo_email_sender.py"])
	_write_files(fixtures, 4080, ["backend/promo_email_sender.py"])
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064",
	)
	assert result.returncode == 0, result.stderr
	assert "result=unblocked action=continue" in result.stdout
	assert "AUTOFIX_MERGE_QUEUED" not in env_out
	assert "AUTOFIX_STALE_BASE_SKIP" not in env_out
	assert "pulls/4091/files" not in log_text, "younger PRs must not be examined"
	assert "pulls/4080/files" not in log_text, "non ai/issue-* PRs must not be examined"


def test_gate_releases_stale_label_when_unblocked(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	(fixtures / "comments_4077.json").write_text(json.dumps([{
		"id": 98,
		"body": "<!-- merge-train:queued -->\nReview queued",
	}]), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064",
	)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASED pr=4077 source=gate" in result.stdout
	assert "PATCH repos/acme/consumer/issues/comments/98" in log_text
	assert "merge-train:queue-retired" in log_text
	assert "DELETE repos/acme/consumer/issues/4077/labels/ai%3Amerge-queued" in log_text
	assert "AUTOFIX_STALE_BASE_SKIP" not in env_out


def test_gate_consumes_prior_queue_marker_as_one_shot_bypass(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4075, "ai/issue-4063"),
		_pr(4077, "ai/issue-4064"),
	]), encoding="utf-8")
	(fixtures / "comments_4077.json").write_text(json.dumps([{
		"id": 99,
		"body": "<!-- merge-train:queued -->\nReview queued",
	}]), encoding="utf-8")
	_write_files(fixtures, 4075, ["backend/promo_email_sender.py"])
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064",
	)
	assert result.returncode == 0, result.stderr
	assert "result=bypassed blockers=#4075 action=continue" in result.stdout
	assert "PATCH repos/acme/consumer/issues/comments/99" in log_text
	assert "labels[]=ai:merge-queued" not in log_text
	assert "AUTOFIX_MERGE_QUEUED" not in env_out
	assert "AUTOFIX_STALE_BASE_SKIP" not in env_out


def test_gate_falls_back_to_api_for_git_quoted_paths(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4075, "ai/issue-4063"),
		_pr(4077, "ai/issue-4064"),
	]), encoding="utf-8")
	quoted_path = "src/\u00e9.py"
	_write_files(fixtures, 4075, [quoted_path])
	_write_files(fixtures, 4077, [quoted_path])
	diff = tmp_path / "pr.diff"
	diff.write_text(
		r'diff --git "a/src/\303\251.py" "b/src/\303\251.py"' + "\n",
		encoding="utf-8",
	)
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064", PR_DIFF_FILE=str(diff),
	)
	assert result.returncode == 0, result.stderr
	assert "result=queued blockers=#4075" in result.stdout
	assert "pulls/4077/files" in log_text
	assert env_out.get("AUTOFIX_STALE_BASE_SKIP") == "true"


def test_gate_does_not_arm_bypass_marker_when_queue_label_add_fails(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4075, "ai/issue-4063"),
		_pr(4077, "ai/issue-4064"),
	]), encoding="utf-8")
	_write_files(fixtures, 4075, ["backend/promo_email_sender.py"])
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	(fixtures / "label_post_fail").touch()
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064",
	)
	assert result.returncode == 0, result.stderr
	assert "no bypass marker will be armed" in result.stdout
	assert env_out.get("AUTOFIX_STALE_BASE_SKIP") == "true"
	assert "POST repos/acme/consumer/issues/4077/comments" not in log_text


def test_gate_ignores_non_ai_issue_head_and_disabled_flag(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="7", BASE_BRANCH="main", TARGET_BRANCH="auto/forward-merge-stable-1-1",
	)
	assert result.returncode == 0
	assert "result=not_ai_issue_branch" in result.stdout
	assert log_text.strip() == "", "no API call for non ai/issue-* heads"

	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="7", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-7", MERGE_TRAIN_ENABLED="false",
	)
	assert result.returncode == 0
	assert "MERGE_TRAIN_DISABLED" in result.stdout
	assert "AUTOFIX_STALE_BASE_SKIP" not in env_out


def test_gate_enabled_flag_is_case_insensitive(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	result, _log_text, _env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="7", BASE_BRANCH="main", TARGET_BRANCH="feature/manual", MERGE_TRAIN_ENABLED="yEs",
	)
	assert result.returncode == 0
	assert "result=not_ai_issue_branch" in result.stdout
	assert "MERGE_TRAIN_DISABLED" not in result.stdout


def test_gate_fails_open_on_api_error(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "fail_get").touch()
	result, _log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064",
	)
	assert result.returncode == 0, result.stderr
	assert "::warning::" in result.stdout
	assert "AUTOFIX_STALE_BASE_SKIP" not in env_out, "API failure must never queue a PR"


def test_release_dispatches_only_unblocked_queued_prs(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	# 4075 merged (absent). 4077 queued and now unblocked; 4081 queued but
	# still overlaps the open 4077 (older) so it stays queued; 4085 queued on
	# another base with no blockers.
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
		_pr(4081, "ai/issue-4059", labels=["ai:merge-queued"]),
		_pr(4085, "ai/issue-4069", base="orchestrator/project-9", labels=["ai:merge-queued"]),
		_pr(4090, "ai/issue-4073"),
	]), encoding="utf-8")
	(fixtures / "comments_4077.json").write_text(json.dumps([{
		"id": 97,
		"body": "<!-- merge-train:queued -->\nReview queued",
	}]), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	_write_files(fixtures, 4081, ["backend/promo_email_sender.py", "db/contracts/promo_email_jobs.yml"])
	_write_files(fixtures, 4085, ["twap_router.py"])
	_write_files(fixtures, 4090, ["backend/promo_email_sender.py"])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log, BASE_BRANCH="main")
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASED pr=4077 source=release" in result.stdout
	assert "MERGE_TRAIN_STILL_QUEUED pr=4081 blockers=#4077" in result.stdout
	assert "pr=4085" not in result.stdout, "BASE_BRANCH filter must skip other bases"
	assert "gh workflow run ai-review.yml --repo acme/consumer --ref ai/issue-4064 -f pr_number=4077" in log_text
	assert "PATCH repos/acme/consumer/issues/comments/97" in log_text
	assert "merge-train:queue-retired" in log_text
	assert "DELETE repos/acme/consumer/issues/4077/labels/ai%3Amerge-queued" in log_text
	assert "issues/4081/labels/ai%3Amerge-queued" not in log_text
	assert "MERGE_TRAIN_RELEASE_SUMMARY examined=2 released=1 base_filter=main" in result.stdout


def test_release_without_base_filter_covers_every_base(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4085, "ai/issue-4069", base="orchestrator/project-9", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	_write_files(fixtures, 4085, ["twap_router.py"])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASED pr=4085 source=release" in result.stdout
	assert "--ref ai/issue-4069 -f pr_number=4085" in log_text


def test_release_keeps_label_when_dispatch_fails(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	(fixtures / "dispatch_fail").touch()
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "could not be dispatched" in result.stdout
	assert "MERGE_TRAIN_RELEASED" not in result.stdout
	assert "DELETE repos/acme/consumer/issues/4077/labels/ai%3Amerge-queued" in log_text
	assert "POST repos/acme/consumer/issues/4077/labels" in log_text
	assert "labels[]=ai:merge-queued" in log_text, "failed dispatch must restore the queue label"
	assert "merge-train:released" not in log_text


def test_release_comment_lookup_failure_does_not_abort_successful_release(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	(fixtures / "fail_released_comment_lookup").touch()
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "could not upsert the released comment" in result.stdout
	assert "MERGE_TRAIN_RELEASED pr=4077 source=release" in result.stdout
	assert "MERGE_TRAIN_RELEASE_SUMMARY examined=1 released=1" in result.stdout
	assert "gh workflow run ai-review.yml" in log_text


def test_release_leaves_active_review_queued_without_dispatch(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	(fixtures / "actions_runs.json").write_text(json.dumps({"workflow_runs": [{
		"status": "in_progress",
		"head_branch": "ai/issue-4064",
		"path": ".github/workflows/ai-review.yml",
	}]}), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text
	assert "issues/4077/labels/ai%3Amerge-queued" not in log_text


def test_release_dispatch_claim_prevents_concurrent_release(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	(fixtures / "label_delete_fail").touch()
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASE_CLAIM_SKIPPED pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_cancel_on_close_releases_train_for_unmerged_prs() -> None:
	workflow = (REPO_ROOT / ".github" / "workflows" / "cancel_on_pr_close.yml").read_text(encoding="utf-8")
	release_step = workflow.split("- name: Release merge-queued PRs (merge train)", 1)[1]
	assert "github.event.pull_request.merged == true" not in release_step
	assert "whether it merged or not" in release_step


def test_usage_error_for_unknown_subcommand(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	result, _log_text, _env = _run("bogus", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 2
	assert "usage" in result.stderr

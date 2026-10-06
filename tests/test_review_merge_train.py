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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


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
# actions/runs is served like the REST listing: filtered by the status query
# parameter and by a URL-encoded created=<=<timestamp> bound (runs without a
# created_at never match a bound), paged by page/per_page, with total_count
# (issue #5443). A fixture may set total_count_override: {"<status>": <n>} to
# report more runs than it holds. A run's listed_status, when set, is the
# status query that returns it, whatever its own status field says.
# fail_runs_get fails the call; malformed_runs returns an object with no
# total_count. actions_runs_switch_at (a call number, counting every
# actions/runs call from 1) serves actions_runs_after_switch.json from that
# call on: a run changed status between two status queries.
# actions_runs_switch2_at serves actions_runs_after_switch2.json from that
# call on, after the first switch: the run changed status a second time.
# runs_raw_page.json, when present, is served as is for every actions/runs
# call (a response whose shape the listing must validate itself).
if [ "${method}" = "GET" ] && [[ "${path}" == repos/*/actions/runs ]]; then
  if [ -f "${FAKE_GH_DIR}/fail_runs_get" ] || [ -f "${FAKE_GH_DIR}/fail_get" ]; then exit 1; fi
  run_call=1
  [ -f "${FAKE_GH_DIR}/runs_call_count" ] && run_call=$(( $(cat "${FAKE_GH_DIR}/runs_call_count") + 1 ))
  printf '%s' "${run_call}" > "${FAKE_GH_DIR}/runs_call_count"
  query=""
  [[ "${endpoint}" == *\?* ]] && query="${endpoint#*\?}"
  run_status=""; run_page=1; run_per_page=30; run_created_max=""
  IFS='&' read -ra query_parts <<< "${query}"
  for part in "${query_parts[@]}"; do
    case "${part}" in
      status=*) run_status="${part#status=}" ;;
      created=%3C%3D*) run_created_max="${part#created=%3C%3D}" ;;
      page=*) run_page="${part#page=}" ;;
      per_page=*) run_per_page="${part#per_page=}" ;;
    esac
  done
  if [ -f "${FAKE_GH_DIR}/malformed_runs" ]; then
    page_json='{"workflow_runs":[]}'
  elif [ -f "${FAKE_GH_DIR}/runs_raw_page.json" ]; then
    page_json="$(cat "${FAKE_GH_DIR}/runs_raw_page.json")"
  else
    runs_source='{"workflow_runs":[]}'
    [ -f "${FAKE_GH_DIR}/actions_runs.json" ] && runs_source="$(cat "${FAKE_GH_DIR}/actions_runs.json")"
    # actions_runs_after_page1.json, when present, serves every later page
    # and every created-bounded follow-up query: runs changed status between
    # two reads.
    if { [ "${run_page}" -gt 1 ] || [ -n "${run_created_max}" ]; } && [ -f "${FAKE_GH_DIR}/actions_runs_after_page1.json" ]; then
      runs_source="$(cat "${FAKE_GH_DIR}/actions_runs_after_page1.json")"
    fi
    if [ -f "${FAKE_GH_DIR}/actions_runs_switch_at" ] && [ "${run_call}" -ge "$(cat "${FAKE_GH_DIR}/actions_runs_switch_at")" ]; then
      runs_source="$(cat "${FAKE_GH_DIR}/actions_runs_after_switch.json")"
    fi
    if [ -f "${FAKE_GH_DIR}/actions_runs_switch2_at" ] && [ "${run_call}" -ge "$(cat "${FAKE_GH_DIR}/actions_runs_switch2_at")" ]; then
      runs_source="$(cat "${FAKE_GH_DIR}/actions_runs_after_switch2.json")"
    fi
    page_json="$(printf '%s' "${runs_source}" | jq -c --arg st "${run_status}" --arg cm "${run_created_max}" --argjson pg "${run_page}" --argjson pp "${run_per_page}" '
      ((.workflow_runs // []) | map(select($st == "" or (.listed_status // .status) == $st))
        | map(select($cm == "" or ((.created_at // "") != "" and .created_at <= $cm)))) as $r
      | {total_count: ((.total_count_override // {})[$st] // ($r | length)),
         workflow_runs: $r[(($pg - 1) * $pp):($pg * $pp)]}')"
  fi
  if [ -n "${jqf}" ]; then printf '%s' "${page_json}" | jq -r "${jqf}"; else printf '%s\n' "${page_json}"; fi
  exit 0
fi
case "${method}" in
  GET)
    if [ -f "${FAKE_GH_DIR}/fail_get" ]; then exit 1; fi
    case "${path}" in
      repos/*/pulls) fixture="${FAKE_GH_DIR}/pulls.json" ;;
      repos/*/pulls/*/files) n="${path#*/pulls/}"; n="${n%%/*}"; fixture="${FAKE_GH_DIR}/files_${n}.json" ;;
      repos/*/issues/*/comments) n="${path#*/issues/}"; n="${n%%/*}"; fixture="${FAKE_GH_DIR}/comments_${n}.json" ;;
      repos/*/issues/comments/*) fixture="${FAKE_GH_DIR}/comment_body.json" ;;
      *) echo "unexpected GET ${path}" >&2; exit 1 ;;
    esac
    if [[ "${path}" == repos/*/issues/*/comments ]] && [[ "${jqf}" == *"merge-train:released"* ]] && [ -f "${FAKE_GH_DIR}/fail_released_comment_lookup" ]; then
      exit 1
    fi
    if [ ! -f "${fixture}" ]; then
      echo '[]'
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
	for inherited_name in ("BASE_BRANCH", "PR_DIFF_FILE", "PR_NUMBER", "TARGET_BRANCH", "IS_SMOKE_TEST", "MERGE_TRAIN_IGNORE_PATHS"):
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


@pytest.mark.parametrize("ignored_setting,queued", [(None, False), ("", True), ("none", True), ("*", True)])
def test_manifest_only_overlap_respects_ignore_setting(tmp_path: Path, ignored_setting: str | None, queued: bool) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4075, "ai/issue-4063"), _pr(4077, "ai/issue-4064"),
	]), encoding="utf-8")
	manifest = ".ai/.workspace_source_manifest.txt"
	_write_files(fixtures, 4075, [manifest])
	_write_files(fixtures, 4077, [manifest])
	env = {} if ignored_setting is None else {"MERGE_TRAIN_IGNORE_PATHS": ignored_setting}
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064", **env,
	)
	assert result.returncode == 0, result.stderr
	assert f"result={'queued' if queued else 'unblocked'}" in result.stdout
	assert f"ignored={manifest if not queued else 'none'}" in result.stdout
	assert ("labels[]=ai:merge-queued" in log_text) == queued
	assert (env_out.get("AUTOFIX_MERGE_QUEUED") == "true") == queued
	if ignored_setting == "*":
		assert "ignoring glob entry" in result.stdout


def test_manifest_ignored_but_real_overlap_queues(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4075, "ai/issue-4063"), _pr(4077, "ai/issue-4064"),
	]), encoding="utf-8")
	manifest = ".ai/.workspace_source_manifest.txt"
	_write_files(fixtures, 4075, [manifest, "src/shared.py"])
	_write_files(fixtures, 4077, [manifest, "src/shared.py"])
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064",
	)
	assert result.returncode == 0, result.stderr
	assert f"result=queued blockers=#4075 action=soft_exit ignored={manifest}" in result.stdout
	assert "src/shared.py" in log_text
	assert manifest not in log_text
	assert env_out.get("AUTOFIX_MERGE_QUEUED") == "true"


def test_release_unblocks_manifest_only_overlap(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4075, "ai/issue-4063"), _pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	_write_files(fixtures, 4075, [".ai/.workspace_source_manifest.txt"])
	_write_files(fixtures, 4077, [".ai/.workspace_source_manifest.txt"])
	result, log_text, _ = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASED pr=4077 source=release" in result.stdout
	assert "workflow run" in log_text


def test_merge_train_workflows_forward_ignore_default() -> None:
	for name in ("review_autofix", "orchestrate_poll", "cancel_on_pr_close"):
		text = (REPO_ROOT / ".github" / "workflows" / f"{name}.yml").read_text(encoding="utf-8")
		assert "MERGE_TRAIN_IGNORE_PATHS: ${{ vars.MERGE_TRAIN_IGNORE_PATHS || '.ai/.workspace_source_manifest.txt' }}" in text


def test_gate_smoke_bypasses_overlap_but_other_signals_queue(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4075, "ai/issue-4063"),
		_pr(4077, "ai/issue-4064"),
	]), encoding="utf-8")
	canary = "tests/e2e_smoke_canary.txt"
	_write_files(fixtures, 4075, [canary])
	_write_files(fixtures, 4077, [canary])
	for signal in ("true", "TRUE", "false", None):
		log.write_text("", encoding="utf-8")
		kwargs = {"IS_SMOKE_TEST": signal} if signal is not None else {}
		result, log_text, env_out = _run(
			"gate", tmp_path, bin_dir, fixtures, log,
			PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064", **kwargs,
		)
		assert result.returncode == 0, result.stderr
		if signal == "true":
			assert "result=unblocked action=continue" in result.stdout
			assert "pulls/4075/files" not in log_text
			assert "pulls/4077/files" not in log_text
			assert "issues/4077/labels" not in log_text
			assert "issues/4077/comments" not in log_text
			assert "AUTOFIX_MERGE_QUEUED" not in env_out
			assert "AUTOFIX_STALE_BASE_SKIP" not in env_out
		else:
			assert "result=queued blockers=#4075" in result.stdout
			assert "pulls/4075/files" in log_text
			assert "labels[]=ai:merge-queued" in log_text
			assert env_out.get("AUTOFIX_STALE_BASE_SKIP") == "true"


def test_gate_smoke_retires_prior_queue_before_removing_label(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4075, "ai/issue-4063"),
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	(fixtures / "comments_4077.json").write_text(json.dumps([{
		"id": 98,
		"body": "<!-- merge-train:queued -->\nReview queued",
	}]), encoding="utf-8")
	_write_files(fixtures, 4075, ["tests/e2e_smoke_canary.txt"])
	_write_files(fixtures, 4077, ["tests/e2e_smoke_canary.txt"])
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064", IS_SMOKE_TEST="true",
	)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASED pr=4077 source=gate" in result.stdout
	assert "pulls/4075/files" not in log_text
	assert "pulls/4077/files" not in log_text
	assert "merge-train:queue-retired" in log_text
	assert log_text.index("PATCH repos/acme/consumer/issues/comments/98") < log_text.index(
		"DELETE repos/acme/consumer/issues/4077/labels/ai%3Amerge-queued"
	)
	assert "AUTOFIX_STALE_BASE_SKIP" not in env_out


def test_gate_smoke_lookup_failure_does_not_retire_queue(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "fail_get").touch()
	result, log_text, env_out = _run(
		"gate", tmp_path, bin_dir, fixtures, log,
		PR_NUMBER="4077", BASE_BRANCH="main", TARGET_BRANCH="ai/issue-4064", IS_SMOKE_TEST="true",
	)
	assert result.returncode == 0, result.stderr
	assert "::warning::" in result.stdout
	assert "issues/4077/labels" not in log_text
	assert "merge-train:queue-retired" not in log_text
	assert "AUTOFIX_STALE_BASE_SKIP" not in env_out


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
	# Issue #4701: dispatched from the default branch, never --ref the head.
	assert "gh workflow run ai-review.yml --repo acme/consumer -f pr_number=4077" in log_text
	assert "--ref" not in log_text
	assert "MERGE_TRAIN_DISPATCHED pr=4077 workflow=ai-review.yml ref=default head=ai/issue-4064" in result.stdout
	assert "PATCH repos/acme/consumer/issues/comments/97" in log_text
	assert "merge-train:queue-retired" in log_text
	assert "DELETE repos/acme/consumer/issues/4077/labels/ai%3Amerge-queued" in log_text
	assert "issues/4081/labels/ai%3Amerge-queued" not in log_text
	assert "MERGE_TRAIN_RELEASE_SUMMARY examined=2 released=1 base_filter=main" in result.stdout


def test_release_fetches_each_pr_file_list_once_per_run(tmp_path: Path) -> None:
	"""The per-run file-list cache must survive across queued-PR evaluations.

	Regression for the api-batching finding on ``_MT_FILES_CACHE``: every
	caller invoked ``_mt_pr_files`` through a command substitution, so the
	cache write happened in a subshell and was discarded. With three open
	PRs where 4081 and 4090 are both queued behind 4077, the release path
	re-fetched ``/pulls/4077/files`` once per queued PR (and 4081's list
	twice: once as its own, once as 4090's older blocker). Each list must
	now cost exactly one ``GET`` for the whole run. See CLAUDE.md §15.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064"),
		_pr(4081, "ai/issue-4059", labels=["ai:merge-queued"]),
		_pr(4090, "ai/issue-4073", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	_write_files(fixtures, 4081, ["backend/promo_email_sender.py", "db/contracts/promo_email_jobs.yml"])
	_write_files(fixtures, 4090, ["backend/promo_email_sender.py"])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log, BASE_BRANCH="main")
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_STILL_QUEUED pr=4081 blockers=#4077" in result.stdout
	assert "MERGE_TRAIN_STILL_QUEUED pr=4090 blockers=#4077,#4081" in result.stdout
	assert "MERGE_TRAIN_RELEASE_SUMMARY examined=2 released=0 base_filter=main" in result.stdout
	for number in (4077, 4081, 4090):
		calls = log_text.count(f"pulls/{number}/files")
		assert calls == 1, f"/pulls/{number}/files fetched {calls} times; the per-run cache must hold it to one"


def test_release_without_base_filter_covers_every_base(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4085, "ai/issue-4069", base="orchestrator/project-9", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	_write_files(fixtures, 4085, ["twap_router.py"])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASED pr=4085 source=release" in result.stdout
	assert "gh workflow run ai-review.yml --repo acme/consumer -f pr_number=4085" in log_text
	assert "--ref" not in log_text


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
		"id": 9001,
		"status": "in_progress",
		"head_branch": "ai/issue-4064",
		"event": "pull_request",
		"path": ".github/workflows/ai-review.yml",
	}]}), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text
	assert "issues/4077/labels/ai%3Amerge-queued" not in log_text


def test_release_leaves_pr_named_dispatch_run_queued_without_dispatch(tmp_path: Path) -> None:
	"""Issue #4701: a default-branch dispatch run is keyed by the PR its name carries."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	(fixtures / "actions_runs.json").write_text(json.dumps({"workflow_runs": [{
		"id": 9002,
		"status": "queued",
		"head_branch": "main",
		"event": "workflow_dispatch",
		"display_title": "AI Review [pr:4077]",
		"path": ".github/workflows/ai-review.yml",
	}]}), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_ignores_pr_named_runs_of_other_prs_and_other_events(tmp_path: Path) -> None:
	"""Only a workflow_dispatch run named exactly for this PR suppresses it."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	(fixtures / "actions_runs.json").write_text(json.dumps({"workflow_runs": [
		{
			"id": 9003,
			"status": "in_progress",
			"head_branch": "main",
			"event": "workflow_dispatch",
			"display_title": "Internal: AI Review & Autofix [pr:40771]",
			"path": ".github/workflows/internal-review.yml",
		},
		{
			"id": 9004,
			"status": "in_progress",
			"head_branch": "ai/issue-9999",
			"event": "pull_request",
			"display_title": "AI Review [pr:4077]",
			"path": ".github/workflows/ai-review.yml",
		},
	]}), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE" not in result.stdout
	assert "MERGE_TRAIN_RELEASED pr=4077 source=release" in result.stdout


def test_release_empty_head_does_not_match_blank_run_branch(tmp_path: Path) -> None:
	"""An empty PR head is never a grep pattern, so a run with head_branch "" does not hold the PR.

	The blank-branch run is a dispatch named for another PR, so it has a key
	and the listing stays complete (AD-14).
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	(fixtures / "actions_runs.json").write_text(json.dumps({"workflow_runs": [
		{
			"id": 9005,
			"status": "in_progress",
			"head_branch": "",
			"event": "workflow_dispatch",
			"display_title": "AI Review [pr:9999]",
			"path": ".github/workflows/ai-review.yml",
		},
		{
			"id": 9006,
			"status": "in_progress",
			"head_branch": "ai/issue-9999",
			"event": "pull_request",
			"display_title": "AI Review",
			"path": ".github/workflows/ai-review.yml",
		},
	]}), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE" not in result.stdout
	assert "MERGE_TRAIN_RELEASED pr=4077 source=release" in result.stdout


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


def _queued_pr_4077(fixtures: Path) -> None:
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])


def _write_runs(fixtures: Path, runs: list[dict], total_count_override: dict | None = None, runs_after_page1: list[dict] | None = None) -> None:
	payload: dict = {"workflow_runs": runs}
	if total_count_override is not None:
		payload["total_count_override"] = total_count_override
	(fixtures / "actions_runs.json").write_text(json.dumps(payload), encoding="utf-8")
	if runs_after_page1 is not None:
		(fixtures / "actions_runs_after_page1.json").write_text(json.dumps({"workflow_runs": runs_after_page1}), encoding="utf-8")


def test_release_matches_ref_suffixed_run_path(tmp_path: Path) -> None:
	"""Issue #5443: a path ending in "@<ref>" still identifies a review run."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [{
		"id": 1,
		"status": "in_progress",
		"head_branch": "ai/issue-4064",
		"event": "pull_request",
		"path": ".github/workflows/ai-review.yml@refs/heads/main",
	}])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text
	assert "issues/4077/labels/ai%3Amerge-queued" not in log_text


def test_release_matches_owner_prefixed_ref_suffixed_dispatch_run(tmp_path: Path) -> None:
	"""A PR-named dispatch run reported under "<owner>/<repo>/…yml@<ref>" holds its PR."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [{
		"id": 1,
		"status": "pending",
		"head_branch": "main",
		"event": "workflow_dispatch",
		"display_title": "Internal: AI Review & Autofix [pr:4077]",
		"path": "acme/consumer/.github/workflows/internal-review.yml@refs/heads/main",
	}])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_ignores_ref_suffixed_runs_of_other_workflows(tmp_path: Path) -> None:
	"""Stripping "@<ref>" does not widen the match beyond the review workflows."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [
		{"id": 1, "status": "in_progress", "head_branch": "ai/issue-4064", "event": "pull_request",
		 "path": ".github/workflows/ci.yml@refs/heads/main"},
		{"id": 2, "status": "in_progress", "head_branch": "ai/issue-4064", "event": "pull_request",
		 "path": ".github/workflows/not-ai-review.yml@refs/heads/ai-review.yml"},
	])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE" not in result.stdout
	assert "MERGE_TRAIN_RELEASED pr=4077 source=release" in result.stdout


RUNS_STATUS_ORDER = (
	"requested", "pending", "queued", "waiting", "in_progress",
	"requested", "pending", "queued", "waiting", "in_progress",
)


def test_release_queries_each_active_status_in_lifecycle_order(tmp_path: Path) -> None:
	"""PR #5451 review round 4 (AD-13) and the review of head fd3ad67 (AD-15):
	the five statuses in lifecycle order, twice."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	runs_calls = [line for line in log_text.splitlines() if "actions/runs" in line]
	assert len(runs_calls) == len(RUNS_STATUS_ORDER), runs_calls
	for call, status in zip(runs_calls, RUNS_STATUS_ORDER):
		assert f"actions/runs?status={status}&per_page=100&page=1" in call
		assert "-X GET" in call
	assert "MERGE_TRAIN_RELEASED pr=4077 source=release" in result.stdout


@pytest.mark.parametrize(("before", "after"), [
	("in_progress", "waiting"),
	("waiting", "in_progress"),
	("waiting", "queued"),
	("queued", "in_progress"),
	("requested", "queued"),
])
def test_release_holds_pr_for_review_run_that_changes_status_mid_listing(tmp_path: Path, before: str, after: str) -> None:
	"""PR #5451 review round 4 (AD-13): a run that changes status once while the
	statuses are read is still seen, whichever two queries the change falls between.

	With the five statuses read once in lifecycle order, a run moving from
	in_progress back to waiting (a later job reaching a deployment environment)
	after the waiting query and before the in_progress query was in neither result.
	"""
	for switch_at in range(2, len(RUNS_STATUS_ORDER) + 1):
		case_dir = tmp_path / f"switch-{switch_at}"
		case_dir.mkdir()
		bin_dir, fixtures, log = _install_fake_gh(case_dir)
		_queued_pr_4077(fixtures)
		run = {"id": 5000, "head_branch": "ai/issue-4064", "event": "pull_request",
			"path": ".github/workflows/internal-review.yml", "created_at": _run_created_at(0)}
		_write_runs(fixtures, [dict(run, status=before)])
		(fixtures / "actions_runs_after_switch.json").write_text(
			json.dumps({"workflow_runs": [dict(run, status=after)]}), encoding="utf-8")
		(fixtures / "actions_runs_switch_at").write_text(str(switch_at), encoding="utf-8")
		result, log_text, _env = _run("release", case_dir, bin_dir, fixtures, log)
		assert result.returncode == 0, (switch_at, result.stderr)
		assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr, (switch_at, result.stderr)
		assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout, (switch_at, result.stdout)
		assert "gh workflow run" not in log_text, switch_at


@pytest.mark.parametrize("statuses", [
	("in_progress", "waiting", "in_progress"),
	("requested", "queued", "in_progress"),
	("queued", "waiting", "in_progress"),
	("waiting", "queued", "in_progress"),
	("pending", "queued", "waiting"),
])
def test_release_holds_pr_for_review_run_that_changes_status_twice_mid_listing(tmp_path: Path, statuses: tuple[str, str, str]) -> None:
	"""PR #5451, review of head fd3ad67 (AD-15): a run that changes status twice
	while the statuses are read is still seen, when at most one change is
	backward (into `waiting` after `in_progress`, or out of it to `queued`).

	With `in_progress` missing from the second pass, a run in `in_progress`
	that moved to `waiting` after the first `in_progress` query was issued and
	back to `in_progress` after the second `waiting` query was in no result.
	"""
	first, second, third = statuses
	for switch_at in range(2, len(RUNS_STATUS_ORDER) + 1):
		for switch2_at in range(switch_at + 1, len(RUNS_STATUS_ORDER) + 1):
			case_dir = tmp_path / f"switch-{switch_at}-{switch2_at}"
			case_dir.mkdir()
			bin_dir, fixtures, log = _install_fake_gh(case_dir)
			_queued_pr_4077(fixtures)
			run = {"id": 5000, "head_branch": "ai/issue-4064", "event": "pull_request",
				"path": ".github/workflows/internal-review.yml", "created_at": _run_created_at(0)}
			_write_runs(fixtures, [dict(run, status=first)])
			(fixtures / "actions_runs_after_switch.json").write_text(
				json.dumps({"workflow_runs": [dict(run, status=second)]}), encoding="utf-8")
			(fixtures / "actions_runs_switch_at").write_text(str(switch_at), encoding="utf-8")
			(fixtures / "actions_runs_after_switch2.json").write_text(
				json.dumps({"workflow_runs": [dict(run, status=third)]}), encoding="utf-8")
			(fixtures / "actions_runs_switch2_at").write_text(str(switch2_at), encoding="utf-8")
			result, log_text, _env = _run("release", case_dir, bin_dir, fixtures, log)
			case = (statuses, switch_at, switch2_at)
			assert result.returncode == 0, (case, result.stderr)
			assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr, (case, result.stderr)
			assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout, (case, result.stdout)
			assert "gh workflow run" not in log_text, case


@pytest.mark.parametrize("run_fields", [
	{"head_branch": None, "event": "pull_request", "display_title": "Add promo sender"},
	{"head_branch": "", "event": "pull_request", "display_title": "Add promo sender"},
	{"event": "push"},
	{"head_branch": None, "event": "workflow_dispatch", "display_title": "AI Review"},
	{"head_branch": "", "event": "workflow_dispatch", "display_title": "Internal: AI Review & Autofix [pr:0]"},
	# PR #5451 review round 2 (AD-16): a dispatch run's head_branch is the ref
	# the workflow ran from, so it never attributes the run to a PR.
	{"head_branch": "main", "event": "workflow_dispatch", "display_title": "Codex PR Self-Healing Semantic Agent"},
	{"head_branch": "main", "event": "workflow_dispatch", "display_title": "Codex PR Self-Healing Semantic Agent",
	 "path": ".github/workflows/review_autofix.yml"},
	{"head_branch": "ai/issue-9999", "event": "workflow_dispatch", "display_title": "AI Review"},
])
def test_release_leaves_pr_queued_when_a_review_run_has_no_key(tmp_path: Path, run_fields: dict) -> None:
	"""PR #5451, review of head fd3ad67 (AD-14): a review run with no non-empty
	head_branch and no PR-named dispatch title could be running for any queued
	PR, so the listing is incomplete and nothing is released. A workflow_dispatch
	run with no PR-named title is unattributed whatever its head_branch (AD-16)."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [dict({"id": 5000, "status": "in_progress",
		"path": ".github/workflows/ai-review.yml@refs/heads/main"}, **run_fields)])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=unattributed_run" in result.stderr
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "MERGE_TRAIN_RELEASED" not in result.stdout
	assert "gh workflow run" not in log_text
	assert "issues/4077/labels/ai%3Amerge-queued" not in log_text


def test_release_keys_pr_named_dispatch_run_with_no_head_branch(tmp_path: Path) -> None:
	"""A PR-named dispatch run with a null head_branch still holds its PR (AD-14)."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [{"id": 5000, "status": "queued", "head_branch": None, "event": "workflow_dispatch",
		"display_title": "AI Review [pr:4077]", "path": ".github/workflows/ai-review.yml"}])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_ignores_keyless_runs_of_other_workflows(tmp_path: Path) -> None:
	"""A run of another workflow with no head_branch never holds the train (AD-14)."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [{"id": 5000, "status": "in_progress", "head_branch": None, "event": "schedule",
		"display_title": "Nightly", "path": ".github/workflows/ci.yml"}])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr
	assert "MERGE_TRAIN_RELEASED pr=4077 source=release" in result.stdout


@pytest.mark.parametrize("status", ["requested", "waiting"])
def test_release_holds_pr_for_review_run_in_requested_or_waiting_status(tmp_path: Path, status: str) -> None:
	"""PR #5451 review round 3 (AD-12): every non-terminal status counts as active.

	A review run that is still `requested` (not yet queued) or `waiting` (held by
	a deployment environment) holds its PR instead of being missed by a listing
	that only read pending, queued, and in_progress.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [{"id": 5000, "status": status, "head_branch": "ai/issue-4064",
		"event": "pull_request", "path": ".github/workflows/internal-review.yml"}])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text
	assert "issues/4077/labels/ai%3Amerge-queued" not in log_text


def _run_created_at(index: int) -> str:
	"""created_at for the run at listing position index: newest first, one second apart."""
	return (datetime(2026, 9, 30, 8, 0, 0, tzinfo=timezone.utc) - timedelta(seconds=index)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_release_reads_every_page_before_deciding(tmp_path: Path) -> None:
	"""A review run past the first 100 of a status still holds its PR.

	The follow-up query is bounded by the oldest created_at read so far, never
	an offset page (issue #5443, AD-8).
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	runs = [
		{"id": 1000 + i, "status": "in_progress", "head_branch": f"feature/{i}", "event": "push",
		 "path": ".github/workflows/ci.yml", "created_at": _run_created_at(i)}
		for i in range(150)
	]
	runs.append({"id": 5000, "status": "in_progress", "head_branch": "ai/issue-4064",
		"event": "pull_request", "path": ".github/workflows/ai-review.yml", "created_at": _run_created_at(150)})
	_write_runs(fixtures, runs)
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert f"actions/runs?status=in_progress&per_page=100&page=1&created=%3C%3D{_run_created_at(99)}" in log_text
	assert "page=2" not in log_text
	assert log_text.count("actions/runs?status=in_progress") == 4, "two queries in each of the two in_progress reads"
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_leaves_pr_queued_when_run_listing_fails(tmp_path: Path) -> None:
	"""Issue #5443: a failed listing never releases blind; it is read once per run."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064", labels=["ai:merge-queued"]),
		_pr(4085, "ai/issue-4069", base="orchestrator/project-9", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	_write_files(fixtures, 4077, ["backend/promo_email_sender.py"])
	_write_files(fixtures, 4085, ["twap_router.py"])
	(fixtures / "comments_4077.json").write_text(json.dumps([{
		"id": 97,
		"body": "<!-- merge-train:queued -->\nReview queued",
	}]), encoding="utf-8")
	(fixtures / "fail_runs_get").touch()
	# One attempt per call, so the log counts listing attempts, not gh_retry retries.
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log, GH_RETRY_MAX_ATTEMPTS="1")
	assert result.returncode == 0, result.stderr
	assert "could not list every active review run" in result.stdout
	assert result.stdout.count("::warning::") == 1
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077 head=ai/issue-4064 action=leave_queued" in result.stdout
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4085 head=ai/issue-4069 action=leave_queued" in result.stdout
	assert "MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=page_failed status=requested page=1" in result.stderr
	assert log_text.count("actions/runs") == 1, "the listing is read once per release invocation"
	assert "gh workflow run" not in log_text
	assert "DELETE" not in log_text
	assert "PATCH" not in log_text, "the queue marker must not be retired on an incomplete listing"
	assert "pulls/4077/files" not in log_text
	assert "MERGE_TRAIN_RELEASE_SUMMARY examined=2 released=0" in result.stdout


def test_release_leaves_pr_queued_on_malformed_run_listing(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	(fixtures / "malformed_runs").touch()
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "reason=malformed_page status=requested page=1" in result.stderr
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_leaves_pr_queued_when_listing_shifts(tmp_path: Path) -> None:
	"""A short page before total_count is reached means runs moved while it was read."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [
		{"id": 1, "status": "queued", "head_branch": "feature/x", "event": "push",
		 "path": ".github/workflows/ci.yml"},
	], total_count_override={"queued": 3})
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "reason=listing_shifted status=queued page=1 read=1 total=3" in result.stderr
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def _in_progress_runs_with_review_at(count: int, review_index: int) -> list[dict]:
	runs = [
		{"id": 1000 + i, "status": "in_progress", "head_branch": f"feature/{i}", "event": "push",
		 "path": ".github/workflows/ci.yml", "created_at": _run_created_at(i)}
		for i in range(count)
	]
	runs[review_index] = {"id": 5000, "status": "in_progress", "head_branch": "ai/issue-4064",
		"event": "pull_request", "path": ".github/workflows/ai-review.yml", "created_at": _run_created_at(review_index)}
	return runs


def test_release_holds_pr_when_listing_shrinks_between_pages(tmp_path: Path) -> None:
	"""Runs that finish after the first read must not hide a still-active review run.

	150 runs, the review run at position 105. After the first query, ten of its
	runs finish. An offset page 2 (offset 100 of 140) would start at the old
	position 110 and never read the review run; the created-bounded follow-up
	query still returns it.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	runs = _in_progress_runs_with_review_at(150, 105)
	_write_runs(fixtures, runs, runs_after_page1=runs[10:])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_holds_pr_when_listing_grows_between_pages(tmp_path: Path) -> None:
	"""Runs created after the first read push older runs down; the bounded query still reads them."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	runs = _in_progress_runs_with_review_at(150, 120)
	newer = [
		{"id": 9000 + i, "status": "in_progress", "head_branch": f"hotfix/{i}", "event": "push",
		 "path": ".github/workflows/ci.yml", "created_at": "2026-09-30T08:05:00Z"}
		for i in range(5)
	]
	_write_runs(fixtures, runs, runs_after_page1=newer + runs)
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_holds_pr_when_listing_membership_shifts_at_same_count(tmp_path: Path) -> None:
	"""PR #5512 review round 1: one run leaves and another enters, total_count unchanged.

	200 runs, the review run at position 100. After the first query, the run at
	position 50 finishes and an older run enters in_progress at position 150
	(it was queued). The positions between shift up by one, so an offset page 2
	(offset 100 of 200) would start at the old position 101: the review run is
	skipped while the runs read (200) reach the unchanged total_count (200).
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	runs = _in_progress_runs_with_review_at(200, 100)
	for index in range(150, 200):
		runs[index]["created_at"] = _run_created_at(index + 1)
	entering = {"id": 7000, "status": "in_progress", "head_branch": "feature/entering", "event": "push",
		"path": ".github/workflows/ci.yml", "created_at": _run_created_at(150)}
	after_first_read = runs[:50] + runs[51:150] + [entering] + runs[150:]
	assert len(after_first_read) == len(runs)
	_write_runs(fixtures, runs + [dict(entering, status="queued")], runs_after_page1=after_first_read)
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr
	assert f"created=%3C%3D{_run_created_at(99)}" in log_text
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_leaves_pr_queued_when_listing_exceeds_ten_pages(tmp_path: Path) -> None:
	"""More active runs than 10 queries read cannot be proven absent."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [
		{"id": 10000 + i, "status": "in_progress", "head_branch": f"feature/{i}", "event": "push",
		 "path": ".github/workflows/ci.yml", "created_at": _run_created_at(i)}
		for i in range(1001)
	])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	# The inclusive bound reads the oldest run of each query again: 100 + 9 * 99.
	# The diagnostic names the last query sent (PR #5451 review round 1).
	assert "reason=truncated status=in_progress page=10 read=991 total=110" in result.stderr
	assert log_text.count("actions/runs?status=in_progress") == 10
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_leaves_pr_queued_when_bounded_query_adds_no_run(tmp_path: Path) -> None:
	"""More than 100 runs created in one second return the same page again: never loop, never release."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [
		{"id": 10000 + i, "status": "in_progress", "head_branch": f"feature/{i}", "event": "push",
		 "path": ".github/workflows/ci.yml", "created_at": "2026-09-30T08:00:00Z"}
		for i in range(150)
	])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "reason=truncated status=in_progress page=2 read=100 total=150" in result.stderr
	assert log_text.count("actions/runs?status=in_progress") == 2
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_leaves_pr_queued_when_full_page_has_no_created_at(tmp_path: Path) -> None:
	"""A page that needs a follow-up query but carries no usable created_at bound is malformed."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	runs = _in_progress_runs_with_review_at(150, 120)
	del runs[40]["created_at"]
	_write_runs(fixtures, runs)
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "reason=malformed_page status=in_progress page=1 read=100 total=150" in result.stderr
	assert log_text.count("actions/runs?status=in_progress") == 1
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_bounds_follow_up_query_by_fractional_created_at(tmp_path: Path) -> None:
	"""PR #5451 review round 1: a created_at with fractional seconds still pages.

	The bound is rounded up to the next whole second, so the inclusive
	follow-up query covers the oldest run read and every run after it.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	runs = _in_progress_runs_with_review_at(150, 120)
	for run in runs:
		run["created_at"] = run["created_at"].replace("Z", ".250Z")
	_write_runs(fixtures, runs)
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr
	assert f"actions/runs?status=in_progress&per_page=100&page=1&created=%3C%3D{_run_created_at(98)}" in log_text
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


@pytest.mark.parametrize("missing", ["path", "id", "empty_path", "null_path"])
def test_release_leaves_pr_queued_when_a_run_cannot_be_classified(tmp_path: Path, missing: str) -> None:
	"""PR #5451 review round 1: a run with no id or path could be a review run.

	It can be neither deduplicated nor matched to a review workflow, so the
	listing is malformed and the queued PR stays queued.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	run = {"id": 5000, "status": "in_progress", "head_branch": "ai/issue-4064", "event": "pull_request",
		"path": ".github/workflows/ai-review.yml"}
	if missing == "empty_path":
		run["path"] = ""
	elif missing == "null_path":
		run["path"] = None
	else:
		del run[missing]
	_write_runs(fixtures, [run])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "reason=malformed_page status=in_progress page=1" in result.stderr
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


@pytest.mark.parametrize("status", ["missing", None, ""])
def test_release_leaves_pr_queued_when_a_run_has_no_status(tmp_path: Path, status: object) -> None:
	"""PR #5451 review round 2: a run with no status could be an active review run.

	Every run an active-status query returns must carry a non-empty status, or
	the page is malformed and the queued PR stays queued.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	run = {"id": 5000, "listed_status": "in_progress", "head_branch": "ai/issue-4064", "event": "pull_request",
		"path": ".github/workflows/ai-review.yml"}
	if status != "missing":
		run["status"] = status
	_write_runs(fixtures, [run])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "reason=malformed_page status=in_progress page=1" in result.stderr
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_holds_pr_for_review_run_with_unexpected_status(tmp_path: Path) -> None:
	"""PR #5451 review round 2 (AD-11): an active-status query proves the run active.

	A review run the in_progress query returns with another status value still
	holds its PR instead of being dropped by a status filter.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [{"id": 5000, "listed_status": "in_progress", "status": "waiting",
		"head_branch": "ai/issue-4064", "event": "pull_request", "path": ".github/workflows/ai-review.yml"}])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr
	assert "MERGE_TRAIN_RELEASE_ACTIVE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_leaves_pr_queued_on_fractional_total_count(tmp_path: Path) -> None:
	"""PR #5451 review round 2: a fractional total_count is malformed, never floored.

	100 runs read against total_count 100.5 would look complete once floored,
	so a review run past them could be missed.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	_write_runs(fixtures, [
		{"id": 1000 + i, "status": "in_progress", "head_branch": f"feature/{i}", "event": "push",
		 "path": ".github/workflows/ci.yml", "created_at": _run_created_at(i)}
		for i in range(100)
	], total_count_override={"in_progress": 100.5})
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "reason=malformed_page status=in_progress page=1" in result.stderr
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


@pytest.mark.parametrize("workflow_runs", ["missing", None, {}, "runs"])
def test_release_leaves_pr_queued_when_workflow_runs_is_not_an_array(tmp_path: Path, workflow_runs: object) -> None:
	"""PR #5451 review round 5: a missing, null, or non-array workflow_runs is malformed.

	Defaulting it to [] made a page with total_count 0 look like a complete,
	empty listing, so the queued PR was released without a real listing.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	page: dict = {"total_count": 0}
	if workflow_runs != "missing":
		page["workflow_runs"] = workflow_runs
	(fixtures / "runs_raw_page.json").write_text(json.dumps(page), encoding="utf-8")
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "reason=malformed_page status=requested page=1" in result.stderr
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


@pytest.mark.parametrize("entry", [None, "run", 5000, ["run"]])
def test_release_leaves_pr_queued_when_a_workflow_runs_entry_is_not_an_object(tmp_path: Path, entry: object) -> None:
	"""PR #5451 review round 3: a non-object workflow_runs entry is malformed.

	The projection used to drop it, so total_count 0 with one such entry read
	as a complete, empty listing and the queued PR was released.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	(fixtures / "runs_raw_page.json").write_text(json.dumps({"total_count": 0, "workflow_runs": [entry]}), encoding="utf-8")
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "reason=malformed_page status=requested page=1" in result.stderr
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


@pytest.mark.parametrize("event", ["missing", None, ""])
def test_release_leaves_pr_queued_when_a_run_has_no_event(tmp_path: Path, event: object) -> None:
	"""PR #5451 review round 3 (AD-17): a run with no event could be a dispatch run.

	The event decides whether a run is keyed by its PR-named title or by its
	head_branch, which for a dispatch run is the ref it ran from. A run with no
	non-empty event makes the page malformed and the queued PR stays queued.
	"""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	run = {"id": 5000, "status": "in_progress", "head_branch": "main",
		"display_title": "Internal: AI Review & Autofix [pr:4077]", "path": ".github/workflows/internal-review.yml"}
	if event != "missing":
		run["event"] = event
	_write_runs(fixtures, [run])
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "reason=malformed_page status=in_progress page=1" in result.stderr
	assert "MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=4077" in result.stdout
	assert "gh workflow run" not in log_text


def test_release_accepts_empty_workflow_runs_array(tmp_path: Path) -> None:
	"""An explicit empty workflow_runs with total_count 0 is a complete listing."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	_queued_pr_4077(fixtures)
	(fixtures / "runs_raw_page.json").write_text(json.dumps({"total_count": 0, "workflow_runs": []}), encoding="utf-8")
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 0, result.stderr
	assert "MERGE_TRAIN_RUNS_LISTING" not in result.stderr
	assert "MERGE_TRAIN_RELEASED pr=4077 source=release" in result.stdout
	assert log_text.count("actions/runs") == len(RUNS_STATUS_ORDER)


def test_release_skips_run_listing_when_nothing_queued(tmp_path: Path) -> None:
	"""§15: the listing is read lazily, only for a queued PR that passes the base filter."""
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	(fixtures / "pulls.json").write_text(json.dumps([
		_pr(4077, "ai/issue-4064"),
		_pr(4085, "ai/issue-4069", base="orchestrator/project-9", labels=["ai:merge-queued"]),
	]), encoding="utf-8")
	result, log_text, _env = _run("release", tmp_path, bin_dir, fixtures, log, BASE_BRANCH="main")
	assert result.returncode == 0, result.stderr
	assert "actions/runs" not in log_text
	assert "MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0 base_filter=main" in result.stdout


def test_cancel_on_close_releases_train_for_unmerged_prs() -> None:
	workflow = (REPO_ROOT / ".github" / "workflows" / "cancel_on_pr_close.yml").read_text(encoding="utf-8")
	release_step = workflow.split("- name: Release merge-queued PRs (merge train)", 1)[1]
	assert "github.event.pull_request.merged == true" not in release_step
	assert "whether it merged or not" in release_step


def test_cancel_on_close_wrappers_keep_event_and_add_matching_schedule() -> None:
	wrappers = [
		REPO_ROOT / ".github" / "workflows" / "internal-cancel-on-pr-close.yml",
		REPO_ROOT / "workflow-templates" / "ai-cancel-on-pr-close.yml",
	]
	for wrapper in wrappers:
		workflow = wrapper.read_text(encoding="utf-8")
		assert "pull_request:\n    types: [closed]" in workflow
		assert 'schedule:\n    - cron: "*/5 * * * *"' in workflow
		assert "pull_request_target" not in workflow
		assert "  actions: write" in workflow
		assert "  contents: read" in workflow
		assert "  pull-requests: read" in workflow
		assert "issues: write" not in workflow
		assert "checkout" not in str(yaml.safe_load(workflow)["jobs"]["cancel"]).lower()


def test_scheduled_cancel_sweep_batches_live_pr_state_and_fails_safe() -> None:
	workflow = (REPO_ROOT / ".github" / "workflows" / "cancel_on_pr_close.yml").read_text(encoding="utf-8")
	assert "group: cancel-on-pr-close-${{ github.repository }}" in workflow
	assert 'if [ "${EVENT_NAME}" = "schedule" ]; then' in workflow
	assert workflow.count("-f status=queued") == 1
	assert workflow.count("-f status=in_progress") == 1
	assert "split -l 50" in workflow
	assert "pullRequest(number:${pr_number}){number state}" in workflow
	assert "headRefName baseRefName" not in workflow
	assert "active run(s) without pull-request linkage" in workflow
	assert "select(all($linked_prs[];" in workflow
	assert '$live_pr.state == "CLOSED" or $live_pr.state == "MERGED"' in workflow
	assert 'endswith("/internal-cancel-on-pr-close.yml")' in workflow
	assert 'endswith("/ai-cancel-on-pr-close.yml")' in workflow
	assert "Scheduled cleanup found no cancellable queued/in-progress pull_request workflow runs." in workflow
	assert "Scheduled cleanup is running the global merge-train release scan." in workflow
	assert "BASE_BRANCH: ${{ github.event.pull_request.base.ref }}" in workflow


def test_usage_error_for_unknown_subcommand(tmp_path: Path) -> None:
	bin_dir, fixtures, log = _install_fake_gh(tmp_path)
	result, _log_text, _env = _run("bogus", tmp_path, bin_dir, fixtures, log)
	assert result.returncode == 2
	assert "usage" in result.stderr

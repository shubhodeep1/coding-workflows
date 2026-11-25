"""Final CI run at the end of review (cut-ci-runner-time Phase 4).

Covers `_pr_ensure_final_ci` and its hook in `_pr_wait_for_required_checks`
(scripts/pr_checks_lib.sh), the red-final-CI hand-back
(scripts/review_final_ci_failed.sh), the review sweep's safety net
(scripts/review_final_ci_sweep.sh) and the auto-merge step's call
(scripts/review_enable_auto_merge.sh).

A fake `gh` on PATH answers `gh api <path>` from a JSON fixture map (longest
matching key wins; `--jq` is applied with the real jq) and records every
call, including `gh workflow run`, in a log file.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import textwrap
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
REPO = "o/r"
SHA = "a" * 40
OTHER_SHA = "b" * 40

FAKE_GH = textwrap.dedent(
	r'''
	#!/usr/bin/env python3
	import json, os, subprocess, sys
	args = sys.argv[1:]
	with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
		log.write(json.dumps(args) + "\n")
	if args[:2] == ["workflow", "run"]:
		sys.exit(int(os.environ.get("FAKE_GH_DISPATCH_RC", "0")))
	if args and args[0] == "api":
		rest = args[1:]
		jq = None
		path = None
		i = 0
		while i < len(rest):
			a = rest[i]
			if a == "--jq":
				jq = rest[i + 1]; i += 2; continue
			if a in ("-X", "-f", "-F", "--method", "--field", "--raw-field"):
				i += 2; continue
			if a.startswith("-"):
				i += 1; continue
			if path is None:
				path = a
			i += 1
		with open(os.environ["FAKE_GH_FIXTURES"], encoding="utf-8") as fh:
			fixtures = json.load(fh)
		key = max((k for k in fixtures if k in (path or "")), key=len, default=None)
		if key is None:
			sys.exit(1)
		entry = fixtures[key]
		if entry.get("rc", 0) != 0:
			sys.exit(entry["rc"])
		body = json.dumps(entry.get("body", {}))
		if jq is not None:
			out = subprocess.run(["jq", "-r", jq], input=body, capture_output=True, text=True)
			sys.stdout.write(out.stdout)
			sys.exit(out.returncode)
		sys.stdout.write(body)
		sys.exit(0)
	sys.exit(0)
	'''
).lstrip()


@pytest.fixture()
def fake_gh(tmp_path: Path):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	log = tmp_path / "gh.log"
	log.write_text("", encoding="utf-8")
	fixtures = tmp_path / "fixtures.json"

	def run(script: str, fixture_map: dict, env: dict | None = None) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
		fixtures.write_text(json.dumps(fixture_map), encoding="utf-8")
		log.write_text("", encoding="utf-8")
		full_env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "REVIEW_FINAL", "PR_CHECKS", "PR_FINAL"))}
		full_env.pop("BASH_ENV", None)
		full_env.update({
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"FAKE_GH_LOG": str(log),
			"FAKE_GH_FIXTURES": str(fixtures),
			"GITHUB_REPOSITORY": REPO,
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"PYTHONDONTWRITEBYTECODE": "1",
		})
		full_env.update(env or {})
		result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=full_env, capture_output=True, text=True)
		calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
		return result, calls

	return run


LIB_PRELUDE = f"""
set -uo pipefail
source "{SCRIPTS}/gh_helpers.sh"
source "{SCRIPTS}/pr_checks_lib.sh"
"""


def _runs(*runs: dict) -> dict:
	return {"workflow_runs": [{"path": ".github/workflows/ci.yml", "head_sha": SHA, **run} for run in runs]}


def _dispatches(calls: list[list[str]]) -> list[list[str]]:
	return [call for call in calls if call[:2] == ["workflow", "run"]]


def _ensure(fake_gh, fixture_map: dict, ref: str = "ai/issue-1", env: dict | None = None):
	script = LIB_PRELUDE + f'_pr_ensure_final_ci "{REPO}" 7 "{SHA}" "{ref}"; echo "rc=$? outcome=${{PR_FINAL_CI_OUTCOME}}"'
	return fake_gh(script, fixture_map, {"REVIEW_FINAL_CI_WORKFLOW": "ci.yml", **(env or {})})


RUNS_KEY = f"repos/{REPO}/actions/workflows/ci.yml/runs?head_sha={SHA}"


def test_disabled_makes_no_call(fake_gh) -> None:
	script = LIB_PRELUDE + f'_pr_ensure_final_ci "{REPO}" 7 "{SHA}" ai/issue-1; echo "outcome=${{PR_FINAL_CI_OUTCOME}}"'
	result, calls = fake_gh(script, {})
	assert "outcome=disabled" in result.stdout
	assert "REVIEW_FINAL_CI pr=7 head=" + SHA + " outcome=disabled" in result.stdout
	assert calls == []


def test_no_run_dispatches_on_the_head_ref(fake_gh) -> None:
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"body": {"workflow_runs": []}}})
	assert "rc=0 outcome=dispatched" in result.stdout, result.stdout + result.stderr
	assert "outcome=dispatched reason=no_run workflow=ci.yml" in result.stdout
	assert _dispatches(calls) == [["workflow", "run", "ci.yml", "--repo", REPO, "--ref", "ai/issue-1", "-f", "pr_number=7", "-f", f"head_sha={SHA}"]]


@pytest.mark.parametrize("run,reason", [
	({"status": "queued", "conclusion": None}, "in_flight"),
	({"status": "in_progress", "conclusion": None}, "in_flight"),
	({"status": "completed", "conclusion": "success"}, "success"),
	({"status": "completed", "conclusion": "failure"}, "failure"),
])
def test_existing_run_is_left_alone(fake_gh, run: dict, reason: str) -> None:
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"body": _runs(run)}})
	assert "outcome=exists" in result.stdout
	assert f"reason={reason}" in result.stdout
	assert _dispatches(calls) == []


def test_a_failed_sync_quick_run_is_answered_with_the_full_suite(fake_gh) -> None:
	# A failed sync-quick run skipped the full suite, so it is not the head's
	# final result: the full run is dispatched, and never another quick one.
	runs = _runs({"status": "completed", "conclusion": "failure", "display_title": "CI [pr:7] [sync-quick]"})
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"body": runs}}, env={"PR_FINAL_CI_SYNC_QUICK": "true"})
	assert "outcome=dispatched reason=sync_quick_failed" in result.stdout, result.stdout + result.stderr
	assert _dispatches(calls) == [["workflow", "run", "ci.yml", "--repo", REPO, "--ref", "ai/issue-1", "-f", "pr_number=7", "-f", f"head_sha={SHA}"]]


def test_a_failed_full_run_still_counts_next_to_a_failed_sync_quick_run(fake_gh) -> None:
	runs = _runs(
		{"status": "completed", "conclusion": "failure", "display_title": "CI [pr:7] [sync-quick]"},
		{"status": "completed", "conclusion": "failure", "display_title": "CI [pr:7]"},
	)
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"body": runs}})
	assert "outcome=exists reason=failure" in result.stdout
	assert _dispatches(calls) == []


def test_push_run_on_the_head_counts(fake_gh) -> None:
	# An orchestrator/project-* head already has its push run (plan D4).
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"body": _runs({"status": "completed", "conclusion": "success", "event": "push"})}})
	assert "outcome=exists reason=success" in result.stdout
	assert _dispatches(calls) == []


def test_only_cancelled_runs_dispatch_again(fake_gh) -> None:
	runs = _runs({"status": "completed", "conclusion": "cancelled"}, {"status": "completed", "conclusion": "timed_out"},
		{"status": "completed", "conclusion": "startup_failure"})
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"body": runs}})
	assert "outcome=dispatched reason=only_unfinished" in result.stdout
	assert len(_dispatches(calls)) == 1


def test_runs_of_other_workflows_or_heads_do_not_count(fake_gh) -> None:
	runs = {"workflow_runs": [
		{"path": ".github/workflows/ci_pr_quick.yml", "head_sha": SHA, "status": "completed", "conclusion": "success"},
		{"path": ".github/workflows/ci.yml", "head_sha": OTHER_SHA, "status": "completed", "conclusion": "success"},
	]}
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"body": runs}})
	assert "outcome=dispatched reason=no_run" in result.stdout
	assert len(_dispatches(calls)) == 1


def test_read_failure_dispatches_anyway(fake_gh) -> None:
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"rc": 1}})
	assert "outcome=read_failed action=dispatch" in result.stdout
	assert "outcome=dispatched reason=read_failed" in result.stdout
	assert len(_dispatches(calls)) == 1


def test_dispatch_failure_is_reported_and_not_retried(fake_gh) -> None:
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"body": {"workflow_runs": []}}}, env={"FAKE_GH_DISPATCH_RC": "1"})
	assert "rc=0 outcome=dispatch_failed" in result.stdout
	assert "::warning::REVIEW_FINAL_CI pr=7 head=" in result.stdout
	assert len(_dispatches(calls)) == 1


def test_missing_head_ref_is_read_from_the_pr(fake_gh) -> None:
	fixtures = {
		RUNS_KEY: {"body": {"workflow_runs": []}},
		f"repos/{REPO}/pulls/7": {"body": {"head": {"ref": "claude/x", "repo": {"full_name": REPO}}}},
	}
	result, calls = _ensure(fake_gh, fixtures, ref="")
	assert "outcome=dispatched" in result.stdout, result.stdout + result.stderr
	assert _dispatches(calls)[0][6] == "claude/x"


def test_fork_head_is_not_dispatched(fake_gh) -> None:
	fixtures = {
		RUNS_KEY: {"body": {"workflow_runs": []}},
		f"repos/{REPO}/pulls/7": {"body": {"head": {"ref": "main", "repo": {"full_name": "someone/fork"}}}},
	}
	result, calls = _ensure(fake_gh, fixtures, ref="")
	assert "outcome=dispatch_failed reason=fork_head" in result.stdout
	assert _dispatches(calls) == []


@pytest.mark.parametrize("workflow,pr,sha", [("../x.yml", "7", SHA), ("ci.yml", "7;x", SHA), ("ci.yml", "7", "nothex")])
def test_invalid_input_fails_closed(fake_gh, workflow: str, pr: str, sha: str) -> None:
	script = LIB_PRELUDE + f'_pr_ensure_final_ci "{REPO}" "{pr}" "{sha}" ai/issue-1; echo "rc=$? outcome=${{PR_FINAL_CI_OUTCOME}}"'
	result, calls = fake_gh(script, {}, {"REVIEW_FINAL_CI_WORKFLOW": workflow})
	assert "rc=1 outcome=invalid_input" in result.stdout
	assert calls == []


def test_caller_listing_is_reused_without_a_read(fake_gh) -> None:
	listing = json.dumps(_runs({"status": "in_progress", "conclusion": None}))
	result, calls = _ensure(fake_gh, {}, env={"PR_FINAL_CI_RUNS_JSON": listing})
	assert "outcome=exists reason=in_flight" in result.stdout
	assert calls == []


def test_unsafe_head_ref_is_refused(fake_gh) -> None:
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"body": {"workflow_runs": []}}}, ref="--upload-pack=x")
	assert "outcome=dispatch_failed reason=head_ref_unavailable" in result.stdout
	assert _dispatches(calls) == []


# ---------------------------------------------------------------------------
# _pr_wait_for_required_checks hook


def _wait(fake_gh, fixtures: dict, env: dict):
	script = LIB_PRELUDE + f'_pr_wait_for_required_checks 7 "{SHA}" main 0; echo "rc=$? outcome=${{PR_CHECKS_WAIT_OUTCOME}} final=${{PR_FINAL_CI_OUTCOME:-unset}}"'
	base_env = {
		"AUTO_MERGE_CHECKS_POLL_SECONDS": "1",
		"AUTO_MERGE_REQUIRED_CI_CHECK": "lint",
		"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "",
		"PR_CHECKS_HEAD_REF": "ai/issue-1",
	}
	base_env.update(env)
	return fake_gh(script, fixtures, base_env)


WAIT_FIXTURES = {
	f"repos/{REPO}/actions/runs?head_sha={SHA}": {"body": {"workflow_runs": []}},
	f"repos/{REPO}/commits/{SHA}/check-runs": {"body": {"total_count": 0, "check_runs": []}},
	f"repos/{REPO}/branches/main/protection": {"rc": 1},
}


def test_wait_dispatches_the_final_run_when_lint_is_absent(fake_gh) -> None:
	result, calls = _wait(fake_gh, WAIT_FIXTURES, {"REVIEW_FINAL_CI_WORKFLOW": "ci.yml"})
	assert "outcome=dispatched reason=no_run workflow=ci.yml" in result.stdout, result.stdout + result.stderr
	assert len(_dispatches(calls)) == 1
	# The hook reused the wait's runs listing: no head-SHA read of ci.yml runs.
	assert not any("actions/workflows/ci.yml/runs" in " ".join(call) for call in calls)
	assert "rc=1" in result.stdout  # max_minutes=0: no green lint yet, so no merge


def test_wait_stops_at_once_when_the_final_run_cannot_be_dispatched(fake_gh) -> None:
	"""A head whose final CI dispatch is refused (its ci.yml predates the
	workflow_dispatch trigger) never gets `lint`: no 45-minute wait (PR #9579)."""
	script = LIB_PRELUDE + f'_pr_wait_for_required_checks 7 "{SHA}" main 5; echo "rc=$? outcome=${{PR_CHECKS_WAIT_OUTCOME}} final=${{PR_FINAL_CI_OUTCOME:-unset}}"'
	env = {
		"AUTO_MERGE_CHECKS_POLL_SECONDS": "30",
		"AUTO_MERGE_REQUIRED_CI_CHECK": "lint",
		"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "",
		"PR_CHECKS_HEAD_REF": "ai/issue-1",
		"REVIEW_FINAL_CI_WORKFLOW": "ci.yml",
		"FAKE_GH_DISPATCH_RC": "1",
	}
	result, calls = fake_gh(script, WAIT_FIXTURES, env)
	assert "outcome=dispatch_failed reason=dispatch_refused" in result.stdout, result.stdout + result.stderr
	assert "outcome=timeout waited_s=0 pending=0 reason=final_ci_dispatch_failed ci_check=lint ci_state=absent" in result.stdout
	assert "rc=1 outcome=timeout final=dispatch_failed" in result.stdout
	assert len(_dispatches(calls)) == 1


def test_wait_hook_is_off_without_the_workflow(fake_gh) -> None:
	result, calls = _wait(fake_gh, WAIT_FIXTURES, {})
	assert "REVIEW_FINAL_CI" not in result.stdout
	assert _dispatches(calls) == []


# ---------------------------------------------------------------------------
# scripts/review_final_ci_failed.sh


def _comments_key() -> str:
	return f"repos/{REPO}/issues/7/comments"


def _failed(fake_gh, fixtures: dict, env: dict | None = None, pr_head: str = SHA):
	full_env = {"REPOSITORY": REPO, "PR_NUMBER": "7", "PR_HEAD_SHA": SHA, "FINAL_CI_FAILED_RETRY_SLEEP_SECS": "0"}
	full_env.update(env or {})
	fixtures = {f"repos/{REPO}/pulls/7": {"body": {"state": "open", "head": {"sha": pr_head}}}, **fixtures}
	return fake_gh(f'bash "{SCRIPTS}/review_final_ci_failed.sh"', fixtures, full_env)


def test_hand_back_skips_a_superseded_head(fake_gh) -> None:
	result, calls = _failed(fake_gh, {_comments_key(): {"body": []}}, pr_head=OTHER_SHA)
	assert f"outcome=superseded current={OTHER_SHA}" in result.stdout
	assert not any(call[:3] == ["api", "-X", "POST"] for call in calls)
	assert _dispatches(calls) == []


def test_failed_final_run_posts_marker_and_dispatches_review(fake_gh) -> None:
	result, calls = _failed(fake_gh, {_comments_key(): {"body": []}})
	assert "REVIEW_FINAL_CI_FAILED pr=7 head=" + SHA + " outcome=redispatched workflow=internal-review.yml" in result.stdout, result.stdout + result.stderr
	posts = [call for call in calls if call[:3] == ["api", "-X", "POST"]]
	assert len(posts) == 1
	assert f"<!-- ai:final-ci-failed:v1 pr=7 head={SHA} -->" in posts[0][-1]
	assert _dispatches(calls)[0][2] == "internal-review.yml"
	assert "pr_number=7" in _dispatches(calls)[0]


def test_existing_trusted_marker_skips_everything(fake_gh) -> None:
	marker = f"x\n\n<!-- ai:final-ci-failed:v1 pr=7 head={SHA} -->"
	comments = [{"body": marker, "user": {"type": "Bot"}, "author_association": "NONE"}]
	result, calls = _failed(fake_gh, {_comments_key(): {"body": comments}})
	assert "outcome=already_reported" in result.stdout
	assert not any(call[:3] == ["api", "-X", "POST"] for call in calls)
	assert _dispatches(calls) == []


def test_untrusted_marker_does_not_suppress_the_hand_back(fake_gh) -> None:
	marker = f"<!-- ai:final-ci-failed:v1 pr=7 head={SHA} -->"
	comments = [{"body": marker, "user": {"type": "User"}, "author_association": "NONE"}]
	result, calls = _failed(fake_gh, {_comments_key(): {"body": comments}})
	assert "outcome=redispatched" in result.stdout


def test_marker_for_another_head_does_not_count(fake_gh) -> None:
	marker = f"<!-- ai:final-ci-failed:v1 pr=7 head={OTHER_SHA} -->"
	comments = [{"body": marker, "user": {"type": "Bot"}, "author_association": "NONE"}]
	result, calls = _failed(fake_gh, {_comments_key(): {"body": comments}})
	assert "outcome=redispatched" in result.stdout


def test_hand_back_disabled_and_invalid_input(fake_gh) -> None:
	result, calls = _failed(fake_gh, {}, {"FINAL_CI_FAILED_REDISPATCH_ENABLED": "false"})
	assert "outcome=disabled" in result.stdout and calls == []
	result, calls = _failed(fake_gh, {}, {"PR_HEAD_SHA": "nothex"})
	assert "outcome=invalid_input" in result.stdout and calls == []


def _failed_sync_quick(fake_gh, head: dict, env: dict | None = None):
	fixtures = {f"repos/{REPO}/pulls/7": {"body": {"state": "open", "head": head}}, _comments_key(): {"body": []}}
	full_env = {"REPOSITORY": REPO, "PR_NUMBER": "7", "PR_HEAD_SHA": SHA, "FINAL_CI_FAILED_SYNC_QUICK": "true"}
	full_env.update(env or {})
	return fake_gh(f'bash "{SCRIPTS}/review_final_ci_failed.sh"', fixtures, full_env)


def test_failed_sync_quick_run_dispatches_the_full_suite_not_review(fake_gh) -> None:
	result, calls = _failed_sync_quick(fake_gh, {"sha": SHA, "ref": "ai/issue-7", "repo": {"full_name": REPO}})
	assert f"REVIEW_FINAL_CI_FAILED pr=7 head={SHA} outcome=full_suite_dispatched workflow=ci.yml" in result.stdout, result.stdout + result.stderr
	assert _dispatches(calls) == [["workflow", "run", "ci.yml", "--repo", REPO, "--ref", "ai/issue-7", "-f", "pr_number=7", "-f", f"head_sha={SHA}"]]
	assert not any(call[:3] == ["api", "-X", "POST"] for call in calls)
	assert not any("comments" in arg for call in calls for arg in call)


@pytest.mark.parametrize("head, env", [
	({"sha": SHA, "ref": "ai/issue-7", "repo": {"full_name": "fork/r"}}, {}),
	({"sha": SHA, "ref": "-x", "repo": {"full_name": REPO}}, {}),
	({"sha": SHA, "repo": {"full_name": REPO}}, {}),
	({"sha": SHA, "ref": "ai/issue-7", "repo": {"full_name": REPO}}, {"FAKE_GH_DISPATCH_RC": "1"}),
])
def test_failed_sync_quick_run_never_falls_back_to_review(fake_gh, head: dict, env: dict) -> None:
	result, calls = _failed_sync_quick(fake_gh, head, env)
	assert "outcome=full_suite_dispatch_failed" in result.stdout, result.stdout + result.stderr
	assert not any(call[:3] == ["api", "-X", "POST"] for call in calls)
	assert all(call[2] == "ci.yml" for call in _dispatches(calls))


def test_failed_sync_quick_run_on_a_superseded_head_does_nothing(fake_gh) -> None:
	result, calls = _failed_sync_quick(fake_gh, {"sha": OTHER_SHA, "ref": "ai/issue-7", "repo": {"full_name": REPO}})
	assert "outcome=superseded" in result.stdout
	assert _dispatches(calls) == []


def test_comment_read_failure_does_not_dispatch(fake_gh) -> None:
	result, calls = _failed(fake_gh, {_comments_key(): {"rc": 1}})
	assert "outcome=read_failed" in result.stdout
	assert _dispatches(calls) == []


# ---------------------------------------------------------------------------
# Infra failures are left to the sweep's re-run (final-ci-failure-ci-fix plan,
# Phase 1).

SHUTDOWN_TEXT = "The runner has received a shutdown signal. This can happen when the runner service is stopped."
JOBS_KEY = "actions/runs/555/attempts/1/jobs"


def _job(name: str, job_id: int, conclusion: str | None = "failure", status: str = "completed") -> dict:
	return {"id": job_id, "name": name, "status": status, "conclusion": conclusion,
		"check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{job_id + 1000}"}


def _infra_fixtures(static_message: str = SHUTDOWN_TEXT) -> dict:
	jobs = [_job("static-checks", 1), _job("lint", 2), _job("final-ci-failed", 3, None, "in_progress")]
	return {
		_comments_key(): {"body": []},
		f"repos/{REPO}/{JOBS_KEY}": {"body": {"total_count": 3, "jobs": jobs}},
		f"repos/{REPO}/check-runs/1001/annotations": {"body": [{"message": static_message, "annotation_level": "failure"}]},
		f"repos/{REPO}/check-runs/1002/annotations": {"body": [{"message": "CI jobs that did not succeed: static-checks", "annotation_level": "failure"}]},
	}


INFRA_REPO = "shubhodeep1/coding-workflows"
INFRA_ENV = {"FINAL_CI_FAILED_RUN_ID": "555", "FINAL_CI_FAILED_RUN_ATTEMPT": "1", "REPOSITORY": INFRA_REPO}


def _infra_failed(fake_gh, fixtures: dict, env: dict):
	# Serve the fixtures under the env's repository (the sweep's re-run
	# exists only in shubhodeep1/coding-workflows).
	repo = env.get("REPOSITORY", REPO)
	renamed = {key.replace(f"repos/{REPO}/", f"repos/{repo}/"): value for key, value in fixtures.items()}
	renamed.setdefault(f"repos/{repo}/pulls/7", {"body": {"state": "open", "head": {"sha": SHA}}})
	return _failed(fake_gh, renamed, env)


def test_infra_failure_posts_no_marker_and_dispatches_nothing(fake_gh) -> None:
	result, calls = _infra_failed(fake_gh, _infra_fixtures(), INFRA_ENV)
	assert f"REVIEW_FINAL_CI_FAILED pr=7 head={SHA} outcome=infra_rerun_deferred reason=shutdown" in result.stdout, result.stdout + result.stderr
	assert not any(call[:3] == ["api", "-X", "POST"] for call in calls)
	assert _dispatches(calls) == []
	assert any(JOBS_KEY in arg for call in calls for arg in call)


@pytest.mark.parametrize("fixtures, env", [
	# A real test failure is handed back as before.
	(_infra_fixtures("Process completed with exit code 1."), INFRA_ENV),
	# A classifier read error keeps the hand-back.
	({**_infra_fixtures(), f"repos/{REPO}/{JOBS_KEY}": {"rc": 1}}, INFRA_ENV),
])
def test_genuine_or_unreadable_failure_keeps_the_hand_back(fake_gh, fixtures: dict, env: dict) -> None:
	result, calls = _infra_failed(fake_gh, fixtures, env)
	assert "outcome=redispatched" in result.stdout, result.stdout + result.stderr
	assert len([call for call in calls if call[:3] == ["api", "-X", "POST"]]) == 1


@pytest.mark.parametrize("env", [
	{**INFRA_ENV, "FINAL_CI_FAILED_RUN_ATTEMPT": "2"},
	{**INFRA_ENV, "FINAL_CI_FAILED_INFRA_RERUN_ENABLED": "false"},
	{**INFRA_ENV, "FINAL_CI_FAILED_INFRA_RERUN_ENABLED": "maybe"},
	{**INFRA_ENV, "CI_CANCELLED_AUTO_RERUN_ENABLED": "false"},
	{**INFRA_ENV, "CI_CANCELLED_AUTO_RERUN_ENABLED": ""},
	{**INFRA_ENV, "FINAL_CI_FAILED_RUN_ID": "x"},
	{"FINAL_CI_FAILED_RUN_ATTEMPT": "1", "REPOSITORY": INFRA_REPO},
	# Another repository has no sweep re-run to defer to.
	{"FINAL_CI_FAILED_RUN_ATTEMPT": "1", "FINAL_CI_FAILED_RUN_ID": "555", "REPOSITORY": "other/repo"},
])
def test_infra_deferral_needs_attempt_one_and_both_switches(fake_gh, env: dict) -> None:
	result, calls = _infra_failed(fake_gh, _infra_fixtures(), env)
	assert "outcome=redispatched" in result.stdout, result.stdout + result.stderr
	assert not any(JOBS_KEY in arg for call in calls for arg in call)


def test_already_reported_head_is_not_classified(fake_gh) -> None:
	marker = f"x\n\n<!-- ai:final-ci-failed:v1 pr=7 head={SHA} -->"
	fixtures = {**_infra_fixtures(), _comments_key(): {"body": [{"body": marker, "user": {"type": "Bot"}, "author_association": "NONE"}]}}
	result, calls = _infra_failed(fake_gh, fixtures, INFRA_ENV)
	assert "outcome=already_reported" in result.stdout
	assert not any(JOBS_KEY in arg for call in calls for arg in call)


def test_ci_workflow_passes_the_run_identity_and_switches() -> None:
	text = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
	job = text[text.index("\n  final-ci-failed:\n"):]
	for line in (
		"FINAL_CI_FAILED_RUN_ID: ${{ github.run_id }}",
		"FINAL_CI_FAILED_RUN_ATTEMPT: ${{ github.run_attempt }}",
		"FINAL_CI_FAILED_INFRA_RERUN_ENABLED: ${{ vars.FINAL_CI_FAILED_INFRA_RERUN_ENABLED || 'true' }}",
		"CI_CANCELLED_AUTO_RERUN_ENABLED: ${{ vars.CI_CANCELLED_AUTO_RERUN_ENABLED || 'true' }}",
		"      checks: read\n",
	):
		assert line in job, line
	script = (SCRIPTS / "review_final_ci_failed.sh").read_text(encoding="utf-8")
	assert "classify-run" in script and "--ignore-job final-ci-failed" in script


def _det_skip_final_ci_jq() -> str:
	text = (SCRIPTS / "review_autofix_step_evaluate_gate.sh").read_text(encoding="utf-8")
	block = text[text.index('FRESHNESS_CI_FAILED_SKIP_SUPPRESSED="false"'):]
	block = block[:block.index("reason=final_ci_failed pr=${PR_NUMBER} candidate=${candidate_skip_reason}")]
	start = block.rindex("jq -e --arg login")
	program = block[start:].split("'", 2)[1]
	return program


@pytest.mark.parametrize("comment, expected", [
	({"author_login": "github-actions[bot]", "author_type": "Bot"}, 0),
	({"author_login": "Workflow-PAT-User", "author_type": "User"}, 0),
	({"author_login": "github-actions[bot]", "author_type": "User"}, 1),
	({"author_login": "mallory", "author_type": "Bot"}, 1),
])
def test_final_ci_marker_suppresses_the_deterministic_skip(tmp_path: Path, comment: dict, expected: int) -> None:
	comments = tmp_path / "comments.json"
	comments.write_text(json.dumps([{**comment, "body": f"x\n\n<!-- ai:final-ci-failed:v1 pr=7 head={SHA} -->"}]), encoding="utf-8")
	result = subprocess.run(["jq", "-e", "--arg", "login", "workflow-pat-user", "--arg", "pr", "7", "--arg", "head", SHA,
		_det_skip_final_ci_jq(), str(comments)], capture_output=True, text=True)
	assert result.returncode == expected, result.stderr
	result = subprocess.run(["jq", "-e", "--arg", "login", "workflow-pat-user", "--arg", "pr", "7", "--arg", "head", OTHER_SHA,
		_det_skip_final_ci_jq(), str(comments)], capture_output=True, text=True)
	assert result.returncode == 1


# ---------------------------------------------------------------------------
# scripts/review_final_ci_sweep.sh


def _sweep(fake_gh, tmp_path: Path, snapshot: list, fixtures: dict, env: dict | None = None):
	snapshot_file = tmp_path / "snapshot.json"
	snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")
	active_runs = tmp_path / "active-review-runs.txt"
	if not active_runs.exists():
		active_runs.write_text("", encoding="utf-8")
	full_env = {"REVIEW_FINAL_CI_WORKFLOW": "ci.yml", "REPOSITORY": REPO, "FINAL_CI_SWEEP_PR_SNAPSHOT": str(snapshot_file),
		"FINAL_CI_SWEEP_ACTIVE_REVIEW_RUNS": str(active_runs)}
	full_env.update(env or {})
	return fake_gh(f'bash "{SCRIPTS}/review_final_ci_sweep.sh"', fixtures, full_env)


def test_sweep_disabled_makes_no_call(fake_gh, tmp_path: Path) -> None:
	result, calls = _sweep(fake_gh, tmp_path, [], {}, {"REVIEW_FINAL_CI_WORKFLOW": ""})
	assert "REVIEW_FINAL_CI_SWEEP outcome=disabled" in result.stdout
	assert calls == []


def test_sweep_dispatches_only_for_gated_heads_without_a_run(fake_gh, tmp_path: Path) -> None:
	c_sha, d_sha = "c" * 40, "d" * 40
	snapshot = [
		{"number": 1, "head_ref": "ai/issue-1", "head_sha": SHA},  # gate success, no run -> dispatch
		{"number": 2, "head_ref": "ai/issue-2", "head_sha": OTHER_SHA},  # has a success run -> skip
		{"number": 3, "head_ref": "orchestrator/project-3", "head_sha": c_sha},  # push run covers it
		{"number": 4, "head_ref": "ai/issue-4", "head_sha": d_sha},  # gate pending -> skip
	]
	fixtures = {
		f"repos/{REPO}/actions/workflows/ci.yml/runs?per_page=100": {"body": {"workflow_runs": [
			{"head_sha": OTHER_SHA, "status": "completed", "conclusion": "success", "path": ".github/workflows/ci.yml"},
		]}},
		f"repos/{REPO}/commits/{SHA}/status": {"body": {"statuses": [{"context": "ai-review/head-gate", "state": "success"}]}},
		f"repos/{REPO}/commits/{d_sha}/status": {"body": {"statuses": [{"context": "ai-review/head-gate", "state": "pending"}]}},
		f"repos/{REPO}/actions/workflows/ci.yml/runs?head_sha={SHA}": {"body": {"workflow_runs": []}},
	}
	result, calls = _sweep(fake_gh, tmp_path, snapshot, fixtures)
	assert "REVIEW_FINAL_CI_SWEEP outcome=done candidates=2 checked=2 gate_not_success=1 ensured=1" in result.stdout, result.stdout + result.stderr
	dispatches = _dispatches(calls)
	assert len(dispatches) == 1 and "pr_number=1" in dispatches[0]
	assert not any(f"commits/{c_sha}" in " ".join(call) for call in calls)


def test_sweep_respects_the_per_tick_cap(fake_gh, tmp_path: Path) -> None:
	snapshot = [{"number": n, "head_ref": f"ai/issue-{n}", "head_sha": f"{n:040x}"} for n in range(1, 6)]
	fixtures = {
		f"repos/{REPO}/actions/workflows/ci.yml/runs?per_page=100": {"body": {"workflow_runs": []}},
		"/status": {"body": {"statuses": []}},
	}
	result, calls = _sweep(fake_gh, tmp_path, snapshot, fixtures, {"FINAL_CI_SWEEP_MAX_PER_TICK": "2"})
	assert "candidates=5 checked=2 gate_not_success=2 ensured=0 cap=2" in result.stdout, result.stdout + result.stderr


def test_sweep_listing_failure_skips_the_tick(fake_gh, tmp_path: Path) -> None:
	result, calls = _sweep(fake_gh, tmp_path, [{"number": 1, "head_ref": "a", "head_sha": SHA}], {})
	assert "REVIEW_FINAL_CI_SWEEP outcome=skip reason=listing_failed" in result.stdout
	assert _dispatches(calls) == []


# ---------------------------------------------------------------------------
# Sweep: base sync after a failed final CI run


MERGE_SHA = "e" * 40
BASE_TIP = "f" * 40
OLD_FAILURE = "2026-01-01T00:00:00Z"
LISTING_KEY = f"repos/{REPO}/actions/workflows/ci.yml/runs?per_page=100"
SYNC_ENV = {
	"MERGE_BASE_SYNC_HEAD_POLL_ATTEMPTS": "1",
	"MERGE_BASE_SYNC_HEAD_POLL_SECONDS": "1",
	"MERGE_BASE_SYNC_MERGE_REF_POLL_ATTEMPTS": "0",
}


def _base_sync_fixtures(*, base: str = "ai/issue-9", ahead_by: int = 2, failed_at: str = OLD_FAILURE,
		comments: list | None = None, listing_runs: list | None = None, gate: str = "success",
		gate_creator: str = "pipeline-bot") -> dict:
	runs = listing_runs if listing_runs is not None else [
		{"head_sha": SHA, "status": "completed", "conclusion": "failure", "path": ".github/workflows/ci.yml",
			"event": "workflow_dispatch", "display_title": "CI [pr:1]", "updated_at": failed_at},
	]
	return {
		LISTING_KEY: {"body": {"workflow_runs": runs}},
		f"repos/{REPO}/commits/{SHA}/status": {"body": {"statuses": [{"context": "ai-review/head-gate", "state": gate}]}},
		f"repos/{REPO}/commits/{SHA}/statuses": {"body": [
			{"context": "ai-review/head-gate", "state": gate, "creator": {"login": gate_creator}},
			{"context": "ai-review/head-gate", "state": "pending", "creator": {"login": "pipeline-bot"}},
		]},
		f"repos/{REPO}/pulls/1": {"body": {"number": 1, "head": {"sha": MERGE_SHA, "ref": "ai/issue-1", "repo": {"full_name": REPO}},
			"base": {"ref": base}, "mergeable": True}},
		f"repos/{REPO}/compare/{SHA}...{base}": {"body": {"ahead_by": ahead_by, "files": [{"filename": "other.txt"}],
			"commits": [{"sha": "1" * 40}, {"sha": BASE_TIP}][-ahead_by:] if ahead_by else []}},
		f"repos/{REPO}/pulls/1/files": {"body": [{"filename": "docs/INVENTORY.md"}]},
		"user": {"body": {"login": "pipeline-bot"}},
		f"repos/{REPO}/issues/1/comments": {"body": comments or []},
		f"repos/{REPO}/pulls/1/update-branch": {"body": {}},
		f"repos/{REPO}/commits/{MERGE_SHA}": {"body": {"parents": [{"sha": SHA}, {"sha": BASE_TIP}],
			"commit": {"verification": {"verified": True}}, "committer": {"login": "web-flow"}}},
		f"repos/{REPO}/actions/workflows/ci.yml/runs?head_sha={MERGE_SHA}": {"body": {"workflow_runs": []}},
	}


def _puts(calls: list[list[str]]) -> list[list[str]]:
	return [call for call in calls if "PUT" in call]


def _posted_bodies(calls: list[list[str]]) -> list[str]:
	return [arg[len("body="):] for call in calls if "POST" in call for arg in call if arg.startswith("body=")]


def _sweep_one(fake_gh, tmp_path: Path, fixtures: dict, env: dict | None = None, ref: str = "ai/issue-1"):
	return _sweep(fake_gh, tmp_path, [{"number": 1, "head_ref": ref, "head_sha": SHA}], fixtures, {**SYNC_ENV, **(env or {})})


def test_base_sync_updates_a_behind_head_whose_final_run_failed(fake_gh, tmp_path: Path) -> None:
	result, calls = _sweep_one(fake_gh, tmp_path, _base_sync_fixtures())
	out = result.stdout + result.stderr
	assert "REVIEW_FINAL_CI_BASE_SYNC pr=1 head=" + SHA + " outcome=synced reason=behind base=ai/issue-9 behind_by=2 merge=" + MERGE_SHA in out, out
	puts = _puts(calls)
	assert len(puts) == 1 and f"repos/{REPO}/pulls/1/update-branch" in puts[0] and f"expected_head_sha={SHA}" in puts[0]
	bodies = _posted_bodies(calls)
	assert len(bodies) == 1
	assert bodies[0].startswith("**Branch updated from `ai/issue-9` before merging**")
	assert "<!-- ai:final-ci-base-sync:v1 pr=1 -->" in bodies[0]
	assert "The final CI run on this head failed" in bodies[0]
	assert bodies[0].rstrip().endswith(f"<!-- ai:merge-base-sync:v1 pr=1 from={SHA} sync=1 base={BASE_TIP} merge={MERGE_SHA} -->")
	dispatches = _dispatches(calls)
	assert len(dispatches) == 1 and f"head_sha={MERGE_SHA}" in dispatches[0] and "ai/issue-1" in dispatches[0]
	assert "REVIEW_FINAL_CI_SWEEP_BASE_SYNC outcome=done candidates=1 checked=1" in out


def test_base_sync_marker_is_counted_and_read_by_the_review_gate(fake_gh, tmp_path: Path) -> None:
	"""The sweep's marker keeps the merge-path shape: the review gate parses it and the cap counts it."""
	result, calls = _sweep_one(fake_gh, tmp_path, _base_sync_fixtures())
	body = _posted_bodies(calls)[0]
	comments = [{"user": {"login": "pipeline-bot"}, "body": body}]
	script = LIB_PRELUDE + '_pr_base_sync_count 1; echo "count=${PR_BASE_SYNC_COUNT} final=${PR_BASE_SYNC_FINAL_CI_COUNT}"'
	result, _ = fake_gh(script, {"user": {"body": {"login": "pipeline-bot"}}, f"repos/{REPO}/issues/1/comments": {"body": comments}})
	assert "count=1 final=1" in result.stdout, result.stdout + result.stderr
	gate_comments = tmp_path / "gate_comments.json"
	gate_comments.write_text(json.dumps([{"author_login": "pipeline-bot", "body": body}]), encoding="utf-8")
	script = (f'source "{SCRIPTS}/gh_helpers.sh"; source "{SCRIPTS}/review_head_gate.sh"; '
		f'review_head_gate_freshness_sync "{REPO}" 1 "{MERGE_SHA}" "ai/issue-9" pipeline-bot "{gate_comments}"; echo "rc=$?"')
	result, _ = fake_gh(script, {
		f"repos/{REPO}/commits/{MERGE_SHA}": {"body": {"parents": [{"sha": SHA}, {"sha": BASE_TIP}], "commit": {"verification": {"verified": True}}, "committer": {"login": "web-flow"}}},
		f"repos/{REPO}/compare/{BASE_TIP}...ai/issue-9": {"body": {"status": "identical"}},
	})
	assert "outcome=skip reason=freshness_sync" in result.stdout and "rc=0" in result.stdout, result.stdout + result.stderr


def test_merge_path_marker_text_is_unchanged(fake_gh) -> None:
	script = LIB_PRELUDE + (f'PR_BASE_FRESHNESS_BASE_REF=main PR_BASE_FRESHNESS_BEHIND_BY=3 PR_BASE_FRESHNESS_OVERLAP=a.sh '
		f'_pr_base_sync_marker_post 1 "{SHA}" 1 2 "{BASE_TIP}" "{MERGE_SHA}"')
	_, calls = fake_gh(script, {f"repos/{REPO}/issues/1/comments": {"body": {}}})
	body = _posted_bodies(calls)[0]
	assert "`main` gained 3 commit(s) that touch files this PR also changes (a.sh), so CI must run on the combined tree first. This head was already approved" in body
	assert "final-ci-base-sync" not in body


def test_base_sync_waits_for_a_red_base(fake_gh, tmp_path: Path) -> None:
	fixtures = _base_sync_fixtures(base="main")
	fixtures[LISTING_KEY]["body"]["workflow_runs"].append(
		{"head_sha": "9" * 40, "head_branch": "main", "event": "push", "status": "completed", "conclusion": "failure",
			"path": ".github/workflows/ci.yml", "created_at": "2026-11-17T08:00:00Z"})
	result, calls = _sweep_one(fake_gh, tmp_path, fixtures)
	assert "outcome=skipped reason=base_red base=main" in result.stdout, result.stdout + result.stderr
	assert _puts(calls) == [] and _dispatches(calls) == []


def test_base_sync_reads_the_base_result_when_the_listing_has_none(fake_gh, tmp_path: Path) -> None:
	fixtures = _base_sync_fixtures(base="orchestrator/project-5")
	fixtures[f"repos/{REPO}/actions/workflows/ci.yml/runs?branch=orchestrator/project-5&event=push"] = {"body": {"workflow_runs": [
		{"conclusion": "failure", "created_at": "2026-11-17T07:00:00Z"},
		{"conclusion": "success", "created_at": "2026-11-17T08:00:00Z"},
	]}}
	result, calls = _sweep_one(fake_gh, tmp_path, fixtures)
	assert "outcome=synced reason=behind base=orchestrator/project-5" in result.stdout, result.stdout + result.stderr
	assert len(_puts(calls)) == 1


@pytest.mark.parametrize(
	("overrides", "env", "reason"),
	[
		({"ahead_by": 0}, {}, "reason=up_to_date"),
		({"failed_at": "2099-01-01T00:00:00Z"}, {}, "reason=too_recent"),
		({"gate": "pending"}, {}, "reason=gate_pending"),
		({"gate_creator": "someone-else"}, {}, "reason=gate_untrusted"),
		({"comments": [{"user": {"login": "pipeline-bot"}, "body": f"**Branch updated from `x` before merging** <!-- ai:final-ci-base-sync:v1 pr=1 -->\n\n<!-- ai:merge-base-sync:v1 pr=1 from={SHA} sync={n} base={BASE_TIP} merge={MERGE_SHA} -->"} for n in (1, 2, 3)]},
			{}, "reason=max_syncs syncs=3 max=3"),
		({}, {"FINAL_CI_SWEEP_BASE_SYNC_MAX": "0"}, "reason=cap_zero"),
		({}, {"DRY_RUN": "true"}, "outcome=dry_run reason=behind"),
	],
)
def test_base_sync_skips(fake_gh, tmp_path: Path, overrides: dict, env: dict, reason: str) -> None:
	result, calls = _sweep_one(fake_gh, tmp_path, _base_sync_fixtures(**overrides), env)
	assert reason in result.stdout, result.stdout + result.stderr
	assert _puts(calls) == [] and _dispatches(calls) == [] and _posted_bodies(calls) == []


@pytest.mark.parametrize(
	"active",
	["internal-review.yml:ai/issue-1 1\n", "review_autofix.yml:pr:1 2\n", "incomplete\n", None],
)
def test_base_sync_waits_for_an_active_review_run(fake_gh, tmp_path: Path, active: str | None) -> None:
	active_file = tmp_path / "active-review-runs.txt"
	env = {}
	if active is None:
		env["FINAL_CI_SWEEP_ACTIVE_REVIEW_RUNS"] = str(tmp_path / "missing.txt")
	else:
		active_file.write_text(active, encoding="utf-8")
	result, calls = _sweep_one(fake_gh, tmp_path, _base_sync_fixtures(), env)
	assert "outcome=skipped reason=review_in_flight head_ref=ai/issue-1" in result.stdout, result.stdout + result.stderr
	assert _puts(calls) == [] and not any("compare/" in " ".join(c) for c in calls)
	# Stopped before any API call, so it does not use up the per-tick cap.
	assert "REVIEW_FINAL_CI_SWEEP_BASE_SYNC outcome=done candidates=1 checked=0" in result.stdout


def test_base_sync_ignores_other_prs_active_runs(fake_gh, tmp_path: Path) -> None:
	(tmp_path / "active-review-runs.txt").write_text("internal-review.yml:ai/issue-10 1\nreview_autofix.yml:pr:11 1\ninternal-review.yml:ai/issue-1 0\n", encoding="utf-8")
	result, calls = _sweep_one(fake_gh, tmp_path, _base_sync_fixtures())
	assert "outcome=synced reason=behind" in result.stdout, result.stdout + result.stderr


def test_base_sync_takes_the_oldest_failure_first(fake_gh, tmp_path: Path) -> None:
	other = "c" * 40
	fixtures = _base_sync_fixtures(listing_runs=[
		{"head_sha": SHA, "status": "completed", "conclusion": "failure", "path": ".github/workflows/ci.yml", "display_title": "CI [pr:1]", "updated_at": "2026-01-02T00:00:00Z"},
		{"head_sha": other, "status": "completed", "conclusion": "failure", "path": ".github/workflows/ci.yml", "display_title": "CI [pr:2]", "updated_at": "2026-01-01T00:00:00Z"},
	])
	snapshot = [{"number": 1, "head_ref": "ai/issue-1", "head_sha": SHA}, {"number": 2, "head_ref": "ai/issue-2", "head_sha": other}]
	result, _ = _sweep(fake_gh, tmp_path, snapshot, fixtures, {**SYNC_ENV, "FINAL_CI_SWEEP_MAX_PER_TICK": "1"})
	lines = [line for line in result.stdout.splitlines() if line.startswith("REVIEW_FINAL_CI_BASE_SYNC ")]
	assert len(lines) == 1 and lines[0].startswith(f"REVIEW_FINAL_CI_BASE_SYNC pr=2 head={other}"), result.stdout


def test_base_sync_skips_an_unknown_failure_time(fake_gh, tmp_path: Path) -> None:
	"""A failed run without `updated_at` must not count as old enough (GNU date reads "-" as today)."""
	fixtures = _base_sync_fixtures(listing_runs=[])
	fixtures[RUNS_KEY] = {"body": {"workflow_runs": [{"path": ".github/workflows/ci.yml", "head_sha": SHA, "event": "pull_request",
		"status": "completed", "conclusion": "failure", "display_title": "AI implementation for issue #1"}]}}
	result, calls = _sweep_one(fake_gh, tmp_path, fixtures)
	assert "outcome=skipped reason=failed_at_unknown" in result.stdout, result.stdout + result.stderr
	assert _puts(calls) == []


def test_base_sync_switch_off_makes_no_base_sync_call(fake_gh, tmp_path: Path) -> None:
	result, calls = _sweep_one(fake_gh, tmp_path, _base_sync_fixtures(), {"FINAL_CI_SWEEP_BASE_SYNC_ENABLED": "false"})
	assert "REVIEW_FINAL_CI_SWEEP_BASE_SYNC outcome=disabled" in result.stdout
	assert not any("compare/" in " ".join(call) or "status" in " ".join(call) for call in calls)


@pytest.mark.parametrize(
	"runs",
	[
		[{"status": "completed", "conclusion": "failure"}, {"status": "completed", "conclusion": "success"}],
		[{"status": "completed", "conclusion": "failure"}, {"status": "in_progress", "conclusion": None}],
		[{"status": "completed", "conclusion": "failure", "display_title": "CI [pr:1] [sync-quick]"}],
		[{"status": "completed", "conclusion": "cancelled"}],
	],
)
def test_base_sync_ignores_heads_without_a_settled_failure(fake_gh, tmp_path: Path, runs: list) -> None:
	listing = [{"head_sha": SHA, "path": ".github/workflows/ci.yml", "updated_at": OLD_FAILURE, "display_title": "CI [pr:1]", **r} for r in runs]
	fixtures = _base_sync_fixtures(listing_runs=listing)
	fixtures[RUNS_KEY] = {"body": {"workflow_runs": []}}
	result, calls = _sweep_one(fake_gh, tmp_path, fixtures, {"DRY_RUN": "true"})
	assert "REVIEW_FINAL_CI_BASE_SYNC" not in result.stdout, result.stdout
	assert _puts(calls) == []


def test_base_sync_takes_an_old_pull_request_run_found_by_the_head_read(fake_gh, tmp_path: Path) -> None:
	"""A retired `pull_request` run outside the listing: `_pr_ensure_final_ci` finds it and the head is queued."""
	fixtures = _base_sync_fixtures(listing_runs=[])
	fixtures[RUNS_KEY] = {"body": {"workflow_runs": [{"path": ".github/workflows/ci.yml", "head_sha": SHA, "event": "pull_request",
		"status": "completed", "conclusion": "failure", "display_title": "AI implementation for issue #1", "updated_at": OLD_FAILURE}]}}
	result, calls = _sweep_one(fake_gh, tmp_path, fixtures)
	out = result.stdout + result.stderr
	assert f"REVIEW_FINAL_CI pr=1 head={SHA} outcome=exists reason=failure" in out
	assert "outcome=synced reason=behind" in out, out
	assert len(_puts(calls)) == 1


def test_ensure_reports_the_failure_time(fake_gh) -> None:
	result, _ = _ensure(fake_gh, {RUNS_KEY: {"body": _runs(
		{"status": "completed", "conclusion": "failure", "updated_at": "2026-11-16T10:00:00Z"},
		{"status": "completed", "conclusion": "failure", "updated_at": "2026-11-16T12:00:00Z"},
	)}})
	script_out = result.stdout
	assert "outcome=exists reason=failure" in script_out
	result, _ = fake_gh(LIB_PRELUDE + f'_pr_ensure_final_ci "{REPO}" 7 "{SHA}" ai/issue-1; echo "at=${{PR_FINAL_CI_FAILED_AT}} reason=${{PR_FINAL_CI_REASON}}"',
		{RUNS_KEY: {"body": _runs({"status": "completed", "conclusion": "failure", "updated_at": "2026-11-16T10:00:00Z"},
			{"status": "completed", "conclusion": "failure", "updated_at": "2026-11-16T12:00:00Z"})}},
		{"REVIEW_FINAL_CI_WORKFLOW": "ci.yml"})
	assert "at=2026-11-16T12:00:00Z reason=failure" in result.stdout, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Wiring


def test_review_autofix_exports_the_input_and_the_sweep_runs_the_helper() -> None:
	import yaml

	review = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8"))
	on = review.get("on") or review.get(True)
	assert on["workflow_call"]["inputs"]["final_ci_workflow"]["default"] == ""
	assert review["env"]["REVIEW_FINAL_CI_WORKFLOW"] == "${{ inputs.final_ci_workflow || '' }}"
	auto_merge = (SCRIPTS / "review_enable_auto_merge.sh").read_text(encoding="utf-8")
	assert auto_merge.count("review_enable_auto_merge_ensure_final_ci\n") >= 2
	assert 'PR_CHECKS_HEAD_REF="${_orch_pr_head_ref}"' in auto_merge
	sweep = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml").read_text(encoding="utf-8"))
	step = next(s for s in sweep["jobs"]["sweep"]["steps"] if s.get("name") == "Ensure the final CI run on reviewed PR heads")
	assert step["run"] == "bash .ci-rerun-support/scripts/review_final_ci_sweep.sh"
	assert "REVIEW_FINAL_CI_WORKFLOW" in step["env"]
	assert step["env"]["FINAL_CI_SWEEP_BASE_SYNC_ENABLED"] == "${{ vars.FINAL_CI_SWEEP_BASE_SYNC_ENABLED || 'true' }}"
	assert step["env"]["FINAL_CI_SWEEP_BASE_SYNC_MIN_AGE_MINUTES"] == "${{ vars.FINAL_CI_SWEEP_BASE_SYNC_MIN_AGE_MINUTES || '30' }}"
	assert step["env"]["FINAL_CI_SWEEP_BASE_SYNC_MAX"] == "${{ vars.FINAL_CI_SWEEP_BASE_SYNC_MAX || '3' }}"
	assert step["env"]["FINAL_CI_SWEEP_ACTIVE_REVIEW_RUNS"] == "${{ runner.temp }}/final-ci-active-review-runs.txt"
	enumerate_step = next(s for s in sweep["jobs"]["sweep"]["steps"] if s.get("name") == "Enumerate open PRs and dispatch internal-review.yml")
	assert '> "${RUNNER_TEMP}/final-ci-active-review-runs.txt"' in enumerate_step["run"]
	assert enumerate_step["run"].index("final-ci-active-review-runs.txt") > enumerate_step["run"].index('snapshot_active_review_runs "${wf}"')


# ---------------------------------------------------------------------------
# Head-gate state in the sweep snapshot (scripts/review_sweep_head_gate.sh)
# and the final-CI sweep's use of it.


def test_sweep_checks_known_success_heads_first_and_reads_no_known_state(fake_gh, tmp_path: Path) -> None:
	"""2026-11-17 08:30: the cap of 10 went to gate-pending heads listed first
	(checked=10 gate_not_success=10 ensured=0). Known-success heads go first,
	known non-success heads cost neither a read nor a cap slot."""
	shas = {n: f"{n:040x}" for n in range(1, 7)}
	snapshot = [
		{"number": 1, "head_ref": "ai/issue-1", "head_sha": shas[1], "head_gate_state": "pending"},
		{"number": 2, "head_ref": "ai/issue-2", "head_sha": shas[2], "head_gate_state": "pending"},
		{"number": 3, "head_ref": "ai/issue-3", "head_sha": shas[3]},  # unknown: read
		{"number": 4, "head_ref": "ai/issue-4", "head_sha": shas[4], "head_gate_state": "failure"},
		{"number": 5, "head_ref": "ai/issue-5", "head_sha": shas[5], "head_gate_state": "success"},
		{"number": 6, "head_ref": "ai/issue-6", "head_sha": shas[6], "head_gate_state": "success"},
	]
	fixtures = {
		f"repos/{REPO}/actions/workflows/ci.yml/runs?per_page=100": {"body": {"workflow_runs": []}},
		"/status": {"body": {"statuses": [{"context": "ai-review/head-gate", "state": "pending"}]}},
		"/actions/workflows/ci.yml/runs?head_sha=": {"body": {"workflow_runs": []}},
	}
	result, calls = _sweep(fake_gh, tmp_path, snapshot, fixtures, {"FINAL_CI_SWEEP_MAX_PER_TICK": "2"})
	assert "candidates=6 checked=2 gate_not_success=3 ensured=2 cap=2" in result.stdout, result.stdout + result.stderr
	assert sorted(d[d.index("-f") + 1] for d in _dispatches(calls)) == ["pr_number=5", "pr_number=6"]
	assert not any(call[1].endswith("/status") for call in calls if len(call) > 1 and call[0] == "api")


def _gate_page(*nodes: tuple) -> dict:
	"""GraphQL page: (number, head oid, commit oid, state or None)."""
	return {"data": {"repository": {"pullRequests": {
		"pageInfo": {"hasNextPage": False, "endCursor": None},
		"nodes": [{"number": n, "headRefOid": head, "commits": {"nodes": [{"commit": {
			"oid": oid, "status": None if state is None else {"context": {"state": state} if state != "-" else None}}}]}}
			for n, head, oid, state in nodes],
	}}}}


HELPER_PRELUDE = f"""
set -uo pipefail
source "{SCRIPTS}/review_sweep_head_gate.sh"
"""


def test_annotate_adds_the_gate_state_only_for_the_snapshot_head(fake_gh) -> None:
	c_sha = "c" * 40
	snapshot = [
		{"number": 1, "head_sha": SHA}, {"number": 2, "head_sha": OTHER_SHA},
		{"number": 3, "head_sha": c_sha}, {"number": 4, "head_sha": "d" * 40}, {"number": 5, "head_sha": "e" * 40},
	]
	page = _gate_page(
		(1, SHA, SHA, "SUCCESS"),
		(2, c_sha, c_sha, "SUCCESS"),  # head moved since the snapshot: unknown
		(3, c_sha, c_sha, "-"),  # no head-gate status: absent
		(4, "d" * 40, "d" * 40, "PENDING"),
	)  # PR 5 missing from the read: unknown
	script = HELPER_PRELUDE + f"printf '%s' {json.dumps(json.dumps(snapshot))} | review_sweep_annotate_head_gate {REPO}"
	result, calls = fake_gh(script, {"graphql": {"body": page}})
	rows = {row["number"]: row for row in json.loads(result.stdout)}
	assert rows[1]["head_gate_state"] == "success"
	assert "head_gate_state" not in rows[2]
	assert rows[3]["head_gate_state"] == "absent"
	assert rows[4]["head_gate_state"] == "pending"
	assert "head_gate_state" not in rows[5]
	assert "REVIEW_SWEEP_HEAD_GATE outcome=annotated prs=5 annotated=3 success=1" in result.stderr
	assert len(calls) == 1 and calls[0][:2] == ["api", "graphql"] and "--paginate" in calls[0]


@pytest.mark.parametrize("fixture", [{"graphql": {"rc": 1}}, {"graphql": {"body": {"errors": [{"message": "x"}]}}}])
def test_annotate_fails_open_with_the_snapshot_unchanged(fake_gh, fixture: dict) -> None:
	snapshot = [{"number": 1, "head_sha": SHA, "title": "t"}]
	script = HELPER_PRELUDE + f"printf '%s' {json.dumps(json.dumps(snapshot))} | review_sweep_annotate_head_gate {REPO}"
	result, _ = fake_gh(script, fixture)
	assert json.loads(result.stdout) == snapshot
	assert "REVIEW_SWEEP_HEAD_GATE outcome=read_failed reason=graphql prs=1" in result.stderr


@pytest.mark.parametrize("runs, expected", [
	([], "none"),
	([{"status": "in_progress", "conclusion": None}], "in_flight"),
	([{"status": "completed", "conclusion": "failure"}, {"status": "completed", "conclusion": "success"}], "success"),
	([{"status": "completed", "conclusion": "failure", "display_title": "CI [pr:7]"}], "failure"),
	([{"status": "completed", "conclusion": "failure", "display_title": "CI [pr:7] [sync-quick]"}], "none"),
	([{"status": "completed", "conclusion": "cancelled"}], "none"),
	([{"status": "completed", "conclusion": "failure", "path": ".github/workflows/other.yml"}], "none"),
])
def test_final_ci_state_matches_the_ensure_decision(fake_gh, runs: list, expected: str) -> None:
	script = HELPER_PRELUDE + f'review_sweep_final_ci_state {REPO} {SHA} ci.yml'
	result, calls = fake_gh(script, {RUNS_KEY: {"body": _runs(*runs)}})
	assert result.stdout.strip() == expected, result.stdout + result.stderr
	assert len(calls) == 1


def test_final_ci_state_read_failure_and_bad_input(fake_gh) -> None:
	result, _ = fake_gh(HELPER_PRELUDE + f'review_sweep_final_ci_state {REPO} {SHA}', {RUNS_KEY: {"rc": 1}})
	assert result.stdout.strip() == "read_failed"
	result, calls = fake_gh(HELPER_PRELUDE + f'review_sweep_final_ci_state {REPO} nothex', {})
	assert result.stdout.strip() == "read_failed" and calls == []


# ---------------------------------------------------------------------------
# Base update for a refused final-CI dispatch (_pr_final_ci_refused_base_update)

REFUSED_BASE_TIP = "f" * 40
UPDATE_SHA = "9" * 40


def _contents(text: str) -> dict:
	return {"body": {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}}


OLD_CI = "name: CI\non:\n  pull_request:\n  push:\n"
NEW_CI = "name: CI\non:\n  push:\n  workflow_dispatch:\n    inputs: {}\n"


def _refused_fixtures(head_ci: str = OLD_CI, base_ci: str = NEW_CI) -> dict:
	return {
		**WAIT_FIXTURES,
		f"repos/{REPO}/contents/.github/workflows/ci.yml?ref={SHA}": _contents(head_ci),
		f"repos/{REPO}/contents/.github/workflows/ci.yml?ref=main": _contents(base_ci),
		f"repos/{REPO}/pulls/7/update-branch": {"body": {"message": "Updating pull request branch."}},
		f"repos/{REPO}/pulls/7": {"body": {"head": {"sha": UPDATE_SHA, "ref": "ai/issue-1", "repo": {"full_name": REPO}}, "mergeable": True}},
		f"repos/{REPO}/commits/{UPDATE_SHA}": {"body": {
			"parents": [{"sha": SHA}, {"sha": REFUSED_BASE_TIP}], "committer": {"login": "web-flow"},
			"commit": {"verification": {"verified": True}}}},
		f"repos/{REPO}/issues/7/comments": {"body": {"id": 1}},
	}


def _refused_wait(fake_gh, fixtures: dict, env: dict | None = None):
	script = LIB_PRELUDE + f'_pr_wait_for_required_checks 7 "{SHA}" main 5; echo "rc=$? outcome=${{PR_CHECKS_WAIT_OUTCOME}}"'
	full_env = {
		"AUTO_MERGE_CHECKS_POLL_SECONDS": "30",
		"AUTO_MERGE_REQUIRED_CI_CHECK": "lint",
		"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "",
		"PR_CHECKS_HEAD_REF": "ai/issue-1",
		"REVIEW_FINAL_CI_WORKFLOW": "ci.yml",
		"FAKE_GH_DISPATCH_RC": "1",
		"MERGE_BASE_SYNC_HEAD_POLL_SECONDS": "1",
		"MERGE_BASE_SYNC_HEAD_POLL_ATTEMPTS": "1",
		"MERGE_BASE_SYNC_MERGE_REF_POLL_ATTEMPTS": "0",
		# What _pr_base_fresh_for_merge left behind for this head (clean).
		"PR_BASE_FRESHNESS_OUTCOME": "clean",
		"PR_BASE_FRESHNESS_HEAD_SHA": SHA,
		"PR_BASE_FRESHNESS_BASE_SHA": REFUSED_BASE_TIP,
		"PR_BASE_FRESHNESS_BASE_REF": "main",
		"PR_BASE_FRESHNESS_BEHIND_BY": "21",
	}
	full_env.update(env or {})
	return fake_gh(script, fixtures, full_env)


def _refused_puts(calls: list[list[str]]) -> list[list[str]]:
	return [c for c in calls if c[:3] == ["api", "-X", "PUT"]]


def test_refused_dispatch_updates_the_approved_branch_once(fake_gh) -> None:
	result, calls = _refused_wait(fake_gh, _refused_fixtures())
	out = result.stdout + result.stderr
	assert f"REVIEW_FINAL_CI_BASE_UPDATE pr=7 head={SHA} outcome=updated reason=workflow_predates_dispatch" in out, out
	assert "reason=final_ci_dispatch_failed" in out and "rc=1 outcome=timeout" in out
	puts = _refused_puts(calls)
	assert len(puts) == 1 and puts[0][3] == f"repos/{REPO}/pulls/7/update-branch" and f"expected_head_sha={SHA}" in puts[0]
	posts = [c for c in calls if c[:3] == ["api", "-X", "POST"]]
	assert len(posts) == 1
	body = posts[0][-1]
	assert body.startswith("body=**Branch updated from `main` before merging**")
	assert "predates its `workflow_dispatch` trigger" in body
	assert body.rstrip().endswith(f"<!-- ai:merge-base-sync:v1 pr=7 from={SHA} sync=1 base={REFUSED_BASE_TIP} merge={UPDATE_SHA} -->")
	# The update head's final CI is dispatched right away (refused here too by the fake).
	assert any(f"head_sha={UPDATE_SHA}" in arg for d in _dispatches(calls) for arg in d)


@pytest.mark.parametrize("fixtures, env, reason", [
	(_refused_fixtures(head_ci=NEW_CI), {}, "head_workflow_yes"),
	(_refused_fixtures(head_ci="name: CI\non: [push, workflow_dispatch]\n"), {}, "head_workflow_yes"),
	(_refused_fixtures(head_ci='name: CI\n"on": {"push": {}, "workflow_dispatch": {}}\n'), {}, "head_workflow_yes"),
	(_refused_fixtures(head_ci="name: CI\non:\n  push:\n  # workflow_dispatch:\n", base_ci=OLD_CI), {}, "base_workflow_no"),
	(_refused_fixtures(base_ci=OLD_CI), {}, "base_workflow_no"),
	({**WAIT_FIXTURES}, {}, "head_workflow_unreadable"),
	(_refused_fixtures(), {"PR_BASE_FRESHNESS_OUTCOME": "fresh"}, "freshness_fresh"),
	(_refused_fixtures(), {"PR_BASE_FRESHNESS_HEAD_SHA": OTHER_SHA}, "freshness_clean"),
	(_refused_fixtures(), {"FINAL_CI_REFUSED_BASE_UPDATE_ENABLED": "false"}, "disabled"),
])
def test_refused_dispatch_base_update_skips(fake_gh, fixtures: dict, env: dict, reason: str) -> None:
	result, calls = _refused_wait(fake_gh, fixtures, env)
	out = result.stdout + result.stderr
	assert f"REVIEW_FINAL_CI_BASE_UPDATE pr=7 head={SHA} outcome=skipped reason={reason}" in out, out
	assert _refused_puts(calls) == []
	assert "rc=1 outcome=timeout" in out


def test_other_dispatch_failures_never_update_the_branch(fake_gh) -> None:
	"""A fork head or an unreadable head ref is not a stale workflow file."""
	env = {"PR_CHECKS_HEAD_REF": ""}
	fixtures = {**_refused_fixtures(), f"repos/{REPO}/pulls/7": {"body": {"head": {"ref": "x", "repo": {"full_name": "fork/r"}}}}}
	result, calls = _refused_wait(fake_gh, fixtures, env)
	out = result.stdout + result.stderr
	assert "reason=fork_head" in out, out
	assert "REVIEW_FINAL_CI_BASE_UPDATE" not in out
	assert _refused_puts(calls) == []


def test_refused_dispatch_marker_numbers_the_update_after_earlier_syncs(fake_gh) -> None:
	earlier = [
		{"user": {"login": "bot"}, "body": f"**Branch updated from `main` before merging** (update {k}; warning threshold 2).\n\nx\n\n<!-- ai:merge-base-sync:v1 pr=7 from={OTHER_SHA} sync={k} -->"}
		for k in (1, 2)
	]
	fixtures = {**_refused_fixtures(), f"repos/{REPO}/issues/7/comments": {"body": earlier}, "user": {"body": {"login": "bot"}}}
	result, calls = _refused_wait(fake_gh, fixtures)
	out = result.stdout + result.stderr
	assert "outcome=updated reason=workflow_predates_dispatch" in out, out
	body = [c for c in calls if c[:3] == ["api", "-X", "POST"]][0][-1]
	assert "(update 3; warning threshold 2)" in body
	assert f"from={SHA} sync=3 base={REFUSED_BASE_TIP} merge={UPDATE_SHA} -->" in body
	assert "predates its `workflow_dispatch` trigger" in body



# ---------------------------------------------------------------------------
# Sweep: priority lane (issue #10048)


def _lane_snapshot(updated_at: str = "") -> list:
	if not updated_at:
		updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
	return [
		{"number": n, "head_ref": f"ai/issue-{n}", "head_sha": f"{n:040x}", "head_gate_state": "success",
			"priority_lane": n == 3, "updated_at": updated_at}
		for n in (1, 2, 3)
	]


LANE_FIXTURES = {
	f"repos/{REPO}/actions/workflows/ci.yml/runs?per_page=100": {"body": {"workflow_runs": []}},
	"runs?head_sha=": {"body": {"workflow_runs": []}},
}


def _dispatched_prs(calls: list[list[str]]) -> list[str]:
	return [arg.split("=", 1)[1] for call in _dispatches(calls) for arg in call if arg.startswith("pr_number=")]


def test_sweep_checks_priority_heads_first_under_the_cap(fake_gh, tmp_path: Path) -> None:
	result, calls = _sweep(fake_gh, tmp_path, _lane_snapshot(), LANE_FIXTURES, {"FINAL_CI_SWEEP_MAX_PER_TICK": "1"})
	assert _dispatched_prs(calls) == ["3"], result.stdout + result.stderr
	assert "PRIORITY_LANE pr=3 outcome=first reason=verified_label surface=final_ci" in result.stdout


def test_sweep_order_is_unchanged_with_the_lane_off(fake_gh, tmp_path: Path) -> None:
	result, calls = _sweep(fake_gh, tmp_path, _lane_snapshot(), LANE_FIXTURES,
		{"FINAL_CI_SWEEP_MAX_PER_TICK": "1", "PRIORITY_LANE_ENABLED": "false"})
	assert _dispatched_prs(calls) == ["1"], result.stdout + result.stderr
	assert "PRIORITY_LANE" not in result.stdout


def test_sweep_hold_file_defers_other_heads_until_the_cap(fake_gh, tmp_path: Path) -> None:
	hold = tmp_path / "hold.txt"
	hold.write_text("hold=true\npriority=3\n", encoding="utf-8")
	result, calls = _sweep(fake_gh, tmp_path, _lane_snapshot(), LANE_FIXTURES, {"FINAL_CI_SWEEP_PRIORITY_LANE_FILE": str(hold)})
	assert _dispatched_prs(calls) == ["3"], result.stdout + result.stderr
	assert "PRIORITY_LANE pr=1 outcome=deferred reason=priority_run_queued surface=final_ci" in result.stdout
	assert "deferred_priority_lane=2" in result.stdout
	# Past PRIORITY_LANE_MAX_DEFER_MINUTES the other heads go ahead as before.
	result, calls = _sweep(fake_gh, tmp_path, _lane_snapshot("2020-01-01T00:00:00Z"), LANE_FIXTURES,
		{"FINAL_CI_SWEEP_PRIORITY_LANE_FILE": str(hold)})
	assert sorted(_dispatched_prs(calls)) == ["1", "2", "3"], result.stdout + result.stderr
	assert "PRIORITY_LANE pr=1 outcome=released reason=max_defer surface=final_ci" in result.stdout
	# With the hold start in the file, the cap counts from it, not updated_at.
	hold.write_text(f"hold=true\npriority=3\nsince={int(time.time()) - 60}\n", encoding="utf-8")
	result, calls = _sweep(fake_gh, tmp_path, _lane_snapshot("2020-01-01T00:00:00Z"), LANE_FIXTURES,
		{"FINAL_CI_SWEEP_PRIORITY_LANE_FILE": str(hold)})
	assert _dispatched_prs(calls) == ["3"], result.stdout + result.stderr
	hold.write_text(f"hold=true\npriority=3\nsince={int(time.time()) - 3 * 3600}\n", encoding="utf-8")
	result, calls = _sweep(fake_gh, tmp_path, _lane_snapshot(), LANE_FIXTURES, {"FINAL_CI_SWEEP_PRIORITY_LANE_FILE": str(hold)})
	assert sorted(_dispatched_prs(calls)) == ["1", "2", "3"], result.stdout + result.stderr


def test_sweep_deferred_heads_spend_no_per_tick_slot(fake_gh, tmp_path: Path) -> None:
	# PRs 1 and 2 are held; PR 4 is past the cap (stale updated_at, no since=)
	# and must still be reached under a cap of 2 after priority PR 3.
	hold = tmp_path / "hold.txt"
	hold.write_text("hold=true\npriority=3\n", encoding="utf-8")
	snapshot = _lane_snapshot() + [{"number": 4, "head_ref": "ai/issue-4", "head_sha": f"{4:040x}",
		"head_gate_state": "success", "priority_lane": False, "updated_at": "2020-01-01T00:00:00Z"}]
	result, calls = _sweep(fake_gh, tmp_path, snapshot, LANE_FIXTURES,
		{"FINAL_CI_SWEEP_PRIORITY_LANE_FILE": str(hold), "FINAL_CI_SWEEP_MAX_PER_TICK": "2"})
	assert sorted(_dispatched_prs(calls)) == ["3", "4"], result.stdout + result.stderr
	assert "PRIORITY_LANE pr=4 outcome=released reason=max_defer surface=final_ci" in result.stdout


def test_sweep_missing_or_malformed_hold_file_holds_nothing(fake_gh, tmp_path: Path) -> None:
	bad = tmp_path / "bad.txt"
	bad.write_text("hold=true\npriority=3\nhold=$(id)\n", encoding="utf-8")
	for path in (str(tmp_path / "missing.txt"), str(bad)):
		result, calls = _sweep(fake_gh, tmp_path, _lane_snapshot(), LANE_FIXTURES, {"FINAL_CI_SWEEP_PRIORITY_LANE_FILE": path})
		assert sorted(_dispatched_prs(calls)) == ["1", "2", "3"], result.stdout + result.stderr
		assert "outcome=deferred" not in result.stdout


def test_sweep_workflow_passes_the_priority_lane_settings() -> None:
	text = (REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml").read_text(encoding="utf-8")
	step = text[text.index("- name: Ensure the final CI run on reviewed PR heads"):]
	step = step[:step.index("\n      - name:")]
	assert "FINAL_CI_SWEEP_PRIORITY_LANE_FILE: ${{ runner.temp }}/priority-lane-hold.txt" in step
	assert "PRIORITY_LANE_ENABLED: ${{ vars.PRIORITY_LANE_ENABLED || 'true' }}" in step


def test_sweep_released_line_only_when_a_slot_is_left(fake_gh, tmp_path: Path) -> None:
	# Cap 1: priority PR 3 takes the only slot. PR 4 is past the defer cap but
	# gets no check this tick, so no `released` line is logged for it.
	hold = tmp_path / "hold.txt"
	hold.write_text("hold=true\npriority=3\n", encoding="utf-8")
	snapshot = _lane_snapshot() + [{"number": 4, "head_ref": "ai/issue-4", "head_sha": f"{4:040x}",
		"head_gate_state": "success", "priority_lane": False, "updated_at": "2020-01-01T00:00:00Z"}]
	result, calls = _sweep(fake_gh, tmp_path, snapshot, LANE_FIXTURES,
		{"FINAL_CI_SWEEP_PRIORITY_LANE_FILE": str(hold), "FINAL_CI_SWEEP_MAX_PER_TICK": "1"})
	assert _dispatched_prs(calls) == ["3"], result.stdout + result.stderr
	assert "PRIORITY_LANE pr=4 outcome=released" not in result.stdout


def test_sweep_checks_unread_priority_heads_before_other_heads(fake_gh, tmp_path: Path) -> None:
	# Cap 1, no hold: the priority PR whose gate still needs a read comes
	# before a non-priority head already known as success.
	snapshot = [
		{"number": 1, "head_ref": "ai/issue-1", "head_sha": f"{1:040x}", "head_gate_state": "success", "priority_lane": False},
		{"number": 2, "head_ref": "ai/issue-2", "head_sha": f"{2:040x}", "priority_lane": True},
	]
	fixtures = {**LANE_FIXTURES,
		f"repos/{REPO}/commits/{2:040x}/status": {"body": {"statuses": [{"context": "ai-review/head-gate", "state": "success"}]}}}
	result, calls = _sweep(fake_gh, tmp_path, snapshot, fixtures, {"FINAL_CI_SWEEP_MAX_PER_TICK": "1"})
	assert _dispatched_prs(calls) == ["2"], result.stdout + result.stderr
	assert "PRIORITY_LANE pr=2 outcome=first reason=verified_label surface=final_ci" in result.stdout


def test_sweep_defers_unread_heads_before_the_status_read(fake_gh, tmp_path: Path) -> None:
	# PR 1's gate is unknown: under the hold it is deferred without a status
	# read and without spending a slot.
	hold = tmp_path / "hold.txt"
	hold.write_text("hold=true\npriority=3\n", encoding="utf-8")
	now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
	snapshot = [
		{"number": 1, "head_ref": "ai/issue-1", "head_sha": f"{1:040x}", "priority_lane": False, "updated_at": now},
		{"number": 3, "head_ref": "ai/issue-3", "head_sha": f"{3:040x}", "head_gate_state": "success", "priority_lane": True, "updated_at": now},
	]
	fixtures = {**LANE_FIXTURES,
		f"repos/{REPO}/commits/{1:040x}/status": {"body": {"statuses": [{"context": "ai-review/head-gate", "state": "success"}]}}}
	result, calls = _sweep(fake_gh, tmp_path, snapshot, fixtures,
		{"FINAL_CI_SWEEP_PRIORITY_LANE_FILE": str(hold), "FINAL_CI_SWEEP_MAX_PER_TICK": "2"})
	assert _dispatched_prs(calls) == ["3"], result.stdout + result.stderr
	assert "PRIORITY_LANE pr=1 outcome=deferred reason=priority_run_queued surface=final_ci" in result.stdout
	assert not any(f"commits/{1:040x}/status" in " ".join(call) for call in calls)
	assert "checked=1 " in result.stdout

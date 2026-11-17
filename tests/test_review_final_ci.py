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
# scripts/review_final_ci_sweep.sh


def _sweep(fake_gh, tmp_path: Path, snapshot: list, fixtures: dict, env: dict | None = None):
	snapshot_file = tmp_path / "snapshot.json"
	snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")
	full_env = {"REVIEW_FINAL_CI_WORKFLOW": "ci.yml", "REPOSITORY": REPO, "FINAL_CI_SWEEP_PR_SNAPSHOT": str(snapshot_file)}
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

BASE_TIP = "f" * 40
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
			"parents": [{"sha": SHA}, {"sha": BASE_TIP}], "committer": {"login": "web-flow"},
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
		"PR_BASE_FRESHNESS_BASE_SHA": BASE_TIP,
		"PR_BASE_FRESHNESS_BASE_REF": "main",
		"PR_BASE_FRESHNESS_BEHIND_BY": "21",
	}
	full_env.update(env or {})
	return fake_gh(script, fixtures, full_env)


def _puts(calls: list[list[str]]) -> list[list[str]]:
	return [c for c in calls if c[:3] == ["api", "-X", "PUT"]]


def test_refused_dispatch_updates_the_approved_branch_once(fake_gh) -> None:
	result, calls = _refused_wait(fake_gh, _refused_fixtures())
	out = result.stdout + result.stderr
	assert f"REVIEW_FINAL_CI_BASE_UPDATE pr=7 head={SHA} outcome=updated reason=workflow_predates_dispatch" in out, out
	assert "reason=final_ci_dispatch_failed" in out and "rc=1 outcome=timeout" in out
	puts = _puts(calls)
	assert len(puts) == 1 and puts[0][3] == f"repos/{REPO}/pulls/7/update-branch" and f"expected_head_sha={SHA}" in puts[0]
	posts = [c for c in calls if c[:3] == ["api", "-X", "POST"]]
	assert len(posts) == 1
	body = posts[0][-1]
	assert body.startswith("body=**Branch updated from `main` before merging**")
	assert "predates its `workflow_dispatch` trigger" in body
	assert body.rstrip().endswith(f"<!-- ai:merge-base-sync:v1 pr=7 from={SHA} sync=1 base={BASE_TIP} merge={UPDATE_SHA} -->")
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
	assert _puts(calls) == []
	assert "rc=1 outcome=timeout" in out


def test_other_dispatch_failures_never_update_the_branch(fake_gh) -> None:
	"""A fork head or an unreadable head ref is not a stale workflow file."""
	env = {"PR_CHECKS_HEAD_REF": ""}
	fixtures = {**_refused_fixtures(), f"repos/{REPO}/pulls/7": {"body": {"head": {"ref": "x", "repo": {"full_name": "fork/r"}}}}}
	result, calls = _refused_wait(fake_gh, fixtures, env)
	out = result.stdout + result.stderr
	assert "reason=fork_head" in out, out
	assert "REVIEW_FINAL_CI_BASE_UPDATE" not in out
	assert _puts(calls) == []


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
	assert f"from={SHA} sync=3 base={BASE_TIP} merge={UPDATE_SHA} -->" in body
	assert "If the base changes these files again" not in body

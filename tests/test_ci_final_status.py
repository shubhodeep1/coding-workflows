#!/usr/bin/env python3
"""Tests for the `ci/final` commit status (issue #9939).

GitHub does not evaluate check-runs of a workflow_dispatch run for a PR, so
the final CI's `lint` check-run never satisfied the required check and every
merge on main landed by admin bypass. ci.yml's `lint` job now posts a
`ci/final` commit status through scripts/ci_final_status.sh. These tests pin
the outcome-to-state mapping, the single POST, the fail-open behaviour, and
the workflow wiring (last steps of `lint`, permissions, quick caller grant).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ci_final_status.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
QUICK_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci_pr_quick.yml"
SHA = "0123456789abcdef0123456789abcdef01234567"
CHECKOUT_STEP = "Checkout the final CI status script"
STATUS_STEP = "Report the final CI result as a commit status"
AGGREGATE_STEP = "Require every CI job to succeed"


def _run(
	tmp_path: Path,
	*,
	aggregate: str = "success",
	docs_only_requested: str = "false",
	docs_only_outcome: str = "",
	sha: str = SHA,
	repo: str = "o/r",
	gh_exit: int = 0,
	run_id: str = "4242",
	extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
	work = tmp_path / "work" / "scripts"
	work.mkdir(parents=True)
	shutil.copy(SCRIPT, work / "ci_final_status.sh")
	# Stub gh_helpers.sh: gh_retry runs the command once.
	(work / "gh_helpers.sh").write_text('gh_retry() { "$@"; }\n', encoding="utf-8")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	calls_log = tmp_path / "gh_calls.jsonl"
	gh = bin_dir / "gh"
	gh.write_text(
		"#!/usr/bin/env python3\n"
		"import json, os, sys\n"
		"with open(os.environ['GH_CALL_LOG'], 'a', encoding='utf-8') as fh:\n"
		"\tfh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
		"print('{\"secret_response\": true}')\n"
		"sys.exit(int(os.environ.get('GH_STUB_EXIT', '0')))\n",
		encoding="utf-8",
	)
	gh.chmod(0o755)
	env = {
		"PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
		"GH_CALL_LOG": str(calls_log),
		"GH_STUB_EXIT": str(gh_exit),
		"GH_TOKEN": "ghs_doNotPrintThisToken",
		"GITHUB_REPOSITORY": repo,
		"GITHUB_SERVER_URL": "https://github.com",
		"GITHUB_RUN_ID": run_id,
		"CI_FINAL_STATUS_SHA": sha,
		"CI_FINAL_STATUS_AGGREGATE_OUTCOME": aggregate,
		"CI_FINAL_STATUS_DOCS_ONLY_REQUESTED": docs_only_requested,
		"CI_FINAL_STATUS_DOCS_ONLY_OUTCOME": docs_only_outcome,
	}
	env.update(extra_env or {})
	proc = subprocess.run(["bash", str(work / "ci_final_status.sh")], env=env, text=True, capture_output=True, timeout=60)
	calls = [json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()] if calls_log.exists() else []
	return proc, calls


def _posted_state(calls: list[list[str]]) -> str:
	assert len(calls) == 1, calls
	fields = dict(arg.split("=", 1) for arg in calls[0] if "=" in arg and not arg.startswith("repos/"))
	return fields["state"]


# --- outcome mapping -----------------------------------------------------------


@pytest.mark.parametrize(
	("aggregate", "requested", "docs_outcome", "expected"),
	[
		("success", "false", "", "success"),
		("success", "false", "skipped", "success"),
		("failure", "false", "", "failure"),
		("skipped", "false", "", "failure"),
		("", "false", "", "failure"),
		("success", "true", "success", "success"),
		("success", "true", "failure", "failure"),
		("success", "true", "skipped", "failure"),
		("success", "true", "", "failure"),
		("skipped", "true", "failure", "failure"),
		("failure", "true", "success", "failure"),
	],
)
def test_outcome_maps_to_state(tmp_path: Path, aggregate: str, requested: str, docs_outcome: str, expected: str) -> None:
	proc, calls = _run(tmp_path, aggregate=aggregate, docs_only_requested=requested, docs_only_outcome=docs_outcome)
	assert proc.returncode == 0, proc.stderr
	assert _posted_state(calls) == expected
	assert f"CI_FINAL_STATUS sha={SHA} state={expected} outcome=posted run=4242" in proc.stdout.splitlines()


def test_sync_quick_run_uses_the_aggregate_mapping(tmp_path: Path) -> None:
	# A sync-quick run's aggregate already decides whether its skipped jobs
	# count, so the status follows the aggregate alone.
	proc, calls = _run(tmp_path, aggregate="success", extra_env={"CI_SYNC_QUICK_REQUESTED": "true"})
	assert proc.returncode == 0
	assert _posted_state(calls) == "success"


def test_post_body_is_exact(tmp_path: Path) -> None:
	proc, calls = _run(tmp_path)
	assert proc.returncode == 0
	assert calls == [[
		"api", "--method", "POST", f"repos/o/r/statuses/{SHA}",
		"-f", "state=success",
		"-f", "context=ci/final",
		"-f", "target_url=https://github.com/o/r/actions/runs/4242",
		"-f", "description=Final CI passed",
	]]


def test_failure_description(tmp_path: Path) -> None:
	_proc, calls = _run(tmp_path, aggregate="failure")
	assert "description=Final CI failed" in calls[0]


def test_cancelled_posts_nothing(tmp_path: Path) -> None:
	proc, calls = _run(tmp_path, aggregate="cancelled")
	assert proc.returncode == 0
	assert calls == []
	assert proc.stdout.splitlines() == [f"CI_FINAL_STATUS sha={SHA} outcome=skipped reason=cancelled"]


@pytest.mark.parametrize(
	("sha", "repo", "logged"),
	[
		("not-a-sha", "o/r", "none"),
		(SHA.upper(), "o/r", "none"),
		("", "o/r", "none"),
		(SHA, "o/r;rm -rf /", SHA),
		(SHA, "", SHA),
	],
)
def test_invalid_input_is_skipped(tmp_path: Path, sha: str, repo: str, logged: str) -> None:
	proc, calls = _run(tmp_path, sha=sha, repo=repo)
	assert proc.returncode == 0
	assert calls == []
	assert f"CI_FINAL_STATUS sha={logged} outcome=skipped reason=invalid_input" in proc.stdout.splitlines()
	assert any(line.startswith("::warning::") for line in proc.stdout.splitlines())


def test_post_failure_is_fail_open(tmp_path: Path) -> None:
	proc, calls = _run(tmp_path, gh_exit=1)
	assert proc.returncode == 0
	assert len(calls) == 1
	lines = proc.stdout.splitlines()
	assert f"CI_FINAL_STATUS sha={SHA} state=success outcome=post_failed" in lines
	assert any(line.startswith("::warning::") for line in lines)


def test_token_and_response_are_never_printed(tmp_path: Path) -> None:
	for gh_exit in (0, 1):
		sub = tmp_path / str(gh_exit)
		sub.mkdir()
		proc, _calls = _run(sub, gh_exit=gh_exit)
		assert "ghs_doNotPrintThisToken" not in proc.stdout + proc.stderr
		assert "secret_response" not in proc.stdout + proc.stderr


def test_missing_run_id_links_the_commit(tmp_path: Path) -> None:
	proc, calls = _run(tmp_path, run_id="")
	assert f"target_url=https://github.com/o/r/commit/{SHA}" in calls[0]
	assert f"CI_FINAL_STATUS sha={SHA} state=success outcome=posted run=unknown" in proc.stdout.splitlines()


def test_script_sources_gh_helpers_with_a_fallback() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	assert 'source "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" >/dev/null 2>&1 || true' in text
	assert 'type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }' in text


# --- workflow wiring -------------------------------------------------------------


def _yaml():
	return pytest.importorskip("yaml")


def _lint_job() -> dict:
	return _yaml().safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["lint"]


def test_lint_job_keeps_its_id_and_name() -> None:
	job = _lint_job()
	assert job.get("name", "lint") == "lint"
	assert job["if"] == "always() && inputs.mode != 'quick'"


def test_status_steps_are_the_last_steps_of_lint() -> None:
	steps = _lint_job()["steps"]
	names = [step.get("name") for step in steps]
	assert names[-2:] == [CHECKOUT_STEP, STATUS_STEP]
	assert names.index(AGGREGATE_STEP) < names.index(CHECKOUT_STEP)
	aggregate = steps[names.index(AGGREGATE_STEP)]
	assert aggregate["id"] == "ci_require_all_jobs"

	checkout, status = steps[-2], steps[-1]
	assert checkout["if"] == "always() && !cancelled()"
	assert checkout["continue-on-error"] is True
	assert checkout["uses"] == "actions/checkout@v5"
	assert checkout["with"]["ref"] == "${{ github.sha }}"
	assert checkout["with"]["path"] == ".ci-final-status"
	assert checkout["with"]["fetch-depth"] == 1
	assert checkout["with"]["persist-credentials"] is False
	assert checkout["with"]["sparse-checkout-cone-mode"] is False
	assert checkout["with"]["sparse-checkout"].split() == ["scripts/ci_final_status.sh", "scripts/gh_helpers.sh"]

	assert status["if"] == "always() && !cancelled()"
	assert status["continue-on-error"] is True
	assert status["run"] == "bash .ci-final-status/scripts/ci_final_status.sh"
	assert status["env"] == {
		"GH_TOKEN": "${{ github.token }}",
		"CI_FINAL_STATUS_SHA": "${{ github.sha }}",
		"CI_FINAL_STATUS_AGGREGATE_OUTCOME": "${{ steps.ci_require_all_jobs.outcome }}",
		"CI_FINAL_STATUS_DOCS_ONLY_REQUESTED": "${{ inputs.docs_only == 'true' }}",
		"CI_FINAL_STATUS_DOCS_ONLY_OUTCOME": "${{ steps.docs_only_verify.outcome }}",
	}


def test_lint_permissions_are_explicit() -> None:
	assert _lint_job()["permissions"] == {
		"contents": "read",
		"pull-requests": "read",
		"actions": "read",
		"statuses": "write",
	}


def test_quick_caller_grants_statuses_write() -> None:
	granted = _yaml().safe_load(QUICK_WORKFLOW.read_text(encoding="utf-8"))["permissions"]
	assert granted.get("statuses") == "write"


def test_status_step_text_is_in_ci_without_yaml() -> None:
	# Text-level guard that runs even without PyYAML.
	text = CI_WORKFLOW.read_text(encoding="utf-8")
	assert "run: bash .ci-final-status/scripts/ci_final_status.sh" in text
	assert "id: ci_require_all_jobs" in text
	assert "statuses: write" in QUICK_WORKFLOW.read_text(encoding="utf-8")

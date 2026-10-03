"""The sweep's CI recovery is head-bound, bounded, and failed-jobs-only."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("ci_cancelled_rerun", ROOT / "scripts/ci_cancelled_rerun.py")
assert SPEC and SPEC.loader
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)
SHA = "a" * 40
OLD_SHA = "b" * 40


def pr(sha=SHA, number=11, **extra):
	return {"number": number, "head_sha": sha, "head_ref": "topic", "title": "Update", "body": "", **extra}


def run(sha=SHA, number=11, conclusion="cancelled", attempt=1, run_id=123, **extra):
	return {
		"id": run_id, "head_sha": "c" * 40,  # pull_request GITHUB_SHA can be a merge commit
		"pull_requests": [{"number": number, "head": {"sha": sha}}],
		"event": "pull_request", "status": "completed", "conclusion": conclusion,
		"run_attempt": attempt, "created_at": "2026-10-03T06:00:00Z",
		"path": ".github/workflows/ci.yml", **extra,
	}


def exercise(prs=None, completed=None, active=None, enabled=True, dry_run=False, head_filter=""):
	prs = [pr()] if prs is None else prs
	completed = [run()] if completed is None else completed
	active = [] if active is None else active
	output = io.StringIO()
	with mock.patch.object(HELPER, "list_runs", side_effect=lambda repo, status: completed if status == "completed" else active) as listing, \
		mock.patch.object(HELPER, "rerun_failed_jobs", return_value=True) as post, contextlib.redirect_stdout(output):
		HELPER.process(prs, HELPER.SOURCE_REPO, enabled, dry_run, head_filter)
	return output.getvalue().splitlines(), listing, post


@pytest.mark.parametrize("conclusion", ["cancelled", "startup_failure"])
def test_first_attempt_restarts_failed_jobs(conclusion):
	lines, listing, post = exercise(completed=[run(conclusion=conclusion)])
	assert lines == [f"CI_CANCELLED_RERUN pr=11 head={SHA} run=123 action=rerun reason=cancelled_ci"]
	assert listing.call_count == 4
	post.assert_called_once_with(HELPER.SOURCE_REPO, 123)


@pytest.mark.parametrize("completed,reason", [
	([run(attempt=2)], "already_retried"),
	([run(conclusion="failure")], "other_conclusion"),
	([run(conclusion="success")], "other_conclusion"),
	([run(sha=OLD_SHA)], "no_attributable_run"),
])
def test_ineligible_runs_are_not_retried(completed, reason):
	lines, _, post = exercise(completed=completed)
	assert f"reason={reason}" in lines[0]
	post.assert_not_called()


def test_newer_success_and_visible_retry_on_other_id_block():
	older = run(run_id=120)
	newer = run(run_id=124, conclusion="success", created_at="2026-10-03T07:00:00Z")
	lines, _, post = exercise(completed=[older, newer])
	assert "run=124 action=skip reason=other_conclusion" in lines[0]
	post.assert_not_called()
	lines, _, post = exercise(completed=[run(run_id=121, attempt=2), older])
	assert "reason=already_retried" in lines[0]
	post.assert_not_called()


@pytest.mark.parametrize("status", ["queued", "in_progress", "pending"])
def test_active_run_on_current_head_blocks(status):
	active = run(status=status)
	lines, _, post = exercise(active=[active])
	assert "reason=active_run" in lines[0]
	post.assert_not_called()


def test_active_run_without_association_on_same_branch_blocks():
	active = run(status="queued", pull_requests=[], head_branch="topic")
	lines, _, post = exercise(active=[active])
	assert "reason=active_run" in lines[0]
	post.assert_not_called()


def test_switch_off_makes_no_ci_reads():
	lines, listing, post = exercise(enabled=False)
	assert "reason=disabled" in lines[0]
	listing.assert_not_called()
	post.assert_not_called()


def test_shared_head_posts_once_and_logs_each_pr():
	lines, _, post = exercise(prs=[pr(), pr(number=12)], completed=[run(), run(number=12, run_id=125)])
	assert len(lines) == 2
	assert "reason=shared_head" in lines[1]
	post.assert_called_once()


@pytest.mark.parametrize("prs,reason", [
	([pr(title="[skip ai] Update")], "skip_ai"),
	([pr(head_sha="invalid")], "invalid_snapshot"),
	([pr(number="11")], "invalid_snapshot"),
])
def test_snapshot_input_and_opt_out(prs, reason):
	lines, listing, post = exercise(prs=prs)
	assert f"reason={reason}" in lines[0]
	listing.assert_not_called()
	post.assert_not_called()


def test_filter_and_dry_run():
	lines, listing, _ = exercise(head_filter="other")
	assert "reason=filtered" in lines[0]
	listing.assert_not_called()
	lines, _, post = exercise(dry_run=True)
	assert "reason=dry_run" in lines[0]
	post.assert_not_called()


def test_shared_head_without_first_pr_run_can_still_find_other_pr():
	lines, _, post = exercise(prs=[pr(), pr(number=12)], completed=[run(number=12)])
	assert "reason=no_attributable_run" in lines[0]
	assert "action=rerun" in lines[1]
	post.assert_called_once()


def test_malformed_snapshot_blocks_other_prs():
	lines, listing, post = exercise(prs=[pr(), pr(number="12")])
	assert len(lines) == 2
	assert all("reason=invalid_snapshot" in line for line in lines)
	listing.assert_not_called()
	post.assert_not_called()


def test_failed_listing_fails_closed_for_every_pr():
	output = io.StringIO()
	with mock.patch.object(HELPER, "list_runs", side_effect=ValueError("listing_truncated")), \
		mock.patch.object(HELPER, "rerun_failed_jobs") as post, contextlib.redirect_stdout(output):
		HELPER.process([pr(), pr(number=12)], HELPER.SOURCE_REPO, True, False, "")
	assert output.getvalue().count("reason=listing_truncated") == 2
	post.assert_not_called()


def test_listings_are_fixed_count_with_no_pr_lookup():
	responses = {
		"completed": [run()], "queued": [], "in_progress": [], "pending": [],
	}

	def fake_gh(args, **_kwargs):
		assert args[0:4] == ["gh", "api", "-X", "GET"]
		assert args[4] == f"repos/{HELPER.SOURCE_REPO}/actions/workflows/ci.yml/runs"
		assert args[-2:] == ["-f", "per_page=100"]
		status = args[6].split("=", 1)[1]
		rows = responses[status]
		return mock.Mock(returncode=0, stdout=json.dumps({"total_count": len(rows), "workflow_runs": rows}))

	with mock.patch.object(HELPER.subprocess, "run", side_effect=fake_gh) as gh, \
		mock.patch.object(HELPER, "rerun_failed_jobs", return_value=True) as post, contextlib.redirect_stdout(io.StringIO()):
		HELPER.process([pr()], HELPER.SOURCE_REPO, True, False, "")
	assert gh.call_count == 4
	post.assert_called_once()


def test_missing_or_bad_association_does_not_authorize_post():
	for completed in ([run(pull_requests=[])], [run(pull_requests=[{"number": 11, "head": {"sha": OLD_SHA}}])]):
		lines, _, post = exercise(completed=completed)
		assert "reason=no_attributable_run" in lines[0]
		post.assert_not_called()


def test_prior_retry_on_shared_head_blocks_another_pr():
	lines, _, post = exercise(completed=[run(), run(number=12, attempt=2, run_id=124)])
	assert "reason=already_retried" in lines[0]
	post.assert_not_called()


@pytest.mark.parametrize("payload", [
	{"total_count": 101, "workflow_runs": []},
	{"total_count": 1, "workflow_runs": []},
	{"total_count": 1, "workflow_runs": [{"id": "unsafe"}]},
	{"total_count": 1, "workflow_runs": [run(pull_requests=[{"number": True, "head": {"sha": SHA}}])]},
	{"total_count": 100, "workflow_runs": [run()] * 100},
])
def test_malformed_or_truncated_listing_cannot_authorize_post(payload):
	with mock.patch.object(HELPER.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(payload))):
		with pytest.raises(ValueError):
			HELPER.list_runs(HELPER.SOURCE_REPO, "completed")


def test_refused_post_has_no_full_rerun_fallback():
	with mock.patch.object(HELPER.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="private")) as gh:
		assert not HELPER.rerun_failed_jobs(HELPER.SOURCE_REPO, 123)
		assert gh.call_count == 1
		assert gh.call_args.args[0][-1].endswith("/rerun-failed-jobs")
	lines, _, _ = exercise()
	assert len(lines) == 1


def test_only_http_201_counts_as_successful_post():
	for code in ("HTTP/2.0 201 Created\r\n\r\n", "HTTP/2.0 204 No Content\r\n\r\n"):
		with mock.patch.object(HELPER.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=code)):
			assert HELPER.rerun_failed_jobs(HELPER.SOURCE_REPO, 123) == ("201" in code)


def test_post_refusal_is_logged_as_skip():
	output = io.StringIO()
	with mock.patch.object(HELPER, "list_runs", side_effect=lambda repo, status: [run(conclusion="startup_failure")] if status == "completed" else []), \
		mock.patch.object(HELPER, "rerun_failed_jobs", return_value=False) as post, contextlib.redirect_stdout(output):
		HELPER.process([pr()], HELPER.SOURCE_REPO, True, False, "")
	assert "action=skip reason=rerun_refused" in output.getvalue()
	post.assert_called_once()


def test_workflow_trusted_checkout_snapshot_and_permissions():
	text = (ROOT / ".github/workflows/review_autofix_sweep.yml").read_text(encoding="utf-8")
	assert "head_sha: .head.sha" in text
	assert '"${RUNNER_TEMP}/ci-cancelled-pr-snapshot.json"' in text
	assert "ref: ${{ github.event.repository.default_branch }}" in text
	assert "persist-credentials: false" in text
	assert "python3 .ci-rerun-support/scripts/ci_cancelled_rerun.py" in text
	assert "actions: write" in text
	assert "CI_CANCELLED_AUTO_RERUN_ENABLED: ${{ vars.CI_CANCELLED_AUTO_RERUN_ENABLED || 'true' }}" in text

#!/usr/bin/env python3
"""Lost-runner re-run is bounded by GitHub's run_attempt (plan goal G6)."""

from __future__ import annotations

import pytest

from runner_scaler_fakes import REPO, Harness, job_event
from runner_scaler import rerun
from runner_scaler.api import ApiError
from runner_scaler.reconcile import ANNOTATION_READ_ATTEMPTS

LOST = [{"message": "The self-hosted runner: do-runner-digital-pa-00000001 lost communication with the server. Verify the machine is running."}]
REAL = [{"message": "Process completed with exit code 1."}]
RUNNER = "do-runner-digital-pa-00000001"


def _job(**overrides):
	job = job_event("completed", 1, runner_name=RUNNER, conclusion="failure")["workflow_job"]
	job.update(overrides)
	return job


@pytest.mark.parametrize(
	("job", "annotations", "expected"),
	(
		(_job(), LOST, rerun.RERUN),
		(_job(), [{"message": "The runner has received a shutdown signal."}], rerun.RERUN),
		(_job(run_attempt=2), LOST, rerun.GIVEN_UP),
		(_job(run_attempt=5), LOST, rerun.GIVEN_UP),
		(_job(), REAL, rerun.NOT_LOST),
		(_job(), None, rerun.NOT_LOST),
		(_job(conclusion="success"), LOST, rerun.NOT_OURS),
		(_job(conclusion="cancelled"), LOST, rerun.NOT_OURS),
		(_job(labels=["ubuntu-latest"]), LOST, rerun.NOT_OURS),
		(_job(runner_name="GitHub Actions 12"), LOST, rerun.NOT_OURS),
	),
)
def test_decision_table(job, annotations, expected) -> None:
	assert rerun.decide(job, label="do-runner", annotations=annotations) == expected


def _complete(h: Harness, *, annotations, run_attempt: int = 1) -> None:
	h.github.annotations[1] = annotations
	h.scaler.handle_event("workflow_job", job_event("completed", 1, runner_name=RUNNER, conclusion="failure", run_attempt=run_attempt))
	h.scaler.reconcile()


def test_lost_runner_is_rerun_once_at_attempt_one() -> None:
	h = Harness()
	_complete(h, annotations=LOST)
	assert ("rerun", REPO, 1) in h.github.calls
	assert h.outcomes("rerun")[0]["reason"] == "runner_lost"


def test_lost_runner_at_attempt_two_is_not_rerun_and_alerts() -> None:
	h = Harness()
	_complete(h, annotations=LOST, run_attempt=2)
	assert not any(call[0] == "rerun" for call in h.github.calls)
	assert f"rerun_given_up:{REPO}" in h.alerter.keys()


def test_real_failure_is_left_to_the_pipeline() -> None:
	h = Harness()
	_complete(h, annotations=REAL)
	assert not any(call[0] == "rerun" for call in h.github.calls)
	assert not h.alerter.alerts


def test_successful_job_costs_no_annotations_read() -> None:
	h = Harness()
	h.scaler.handle_event("workflow_job", job_event("completed", 1, runner_name=RUNNER, conclusion="success"))
	h.scaler.reconcile()
	assert not any(call[0] == "annotations" for call in h.github.calls)


def test_rerun_disabled_and_annotation_errors_never_rerun() -> None:
	h = Harness(rerun_enabled=False)
	_complete(h, annotations=LOST)
	assert not any(call[0] in ("rerun", "annotations") for call in h.github.calls)
	h = Harness()
	h.github.fail["annotations"] = ApiError(502, "bad gateway")
	_complete(h, annotations=LOST)
	assert h.outcomes("rerun_deferred")[0]["reason"] == "annotations_failed"  # transient: retried next pass
	for _ in range(ANNOTATION_READ_ATTEMPTS - 1):
		h.scaler.reconcile()
	assert not any(call[0] == "rerun" for call in h.github.calls)
	assert len([call for call in h.github.calls if call[0] == "annotations"]) == ANNOTATION_READ_ATTEMPTS
	assert h.outcomes("rerun_skipped")[0]["reason"] == "annotations_failed"
	h = Harness()
	h.github.fail["annotations"] = ApiError(403, "Resource not accessible by integration")
	_complete(h, annotations=LOST)
	assert h.outcomes("rerun_skipped")[0]["reason"] == "annotations_failed"  # permanent: no retry


def test_transient_annotations_failure_is_retried_and_the_job_rerun() -> None:
	h = Harness()
	h.github.fail["annotations"] = ApiError(502, "bad gateway")
	_complete(h, annotations=LOST)
	del h.github.fail["annotations"]
	h.scaler.reconcile()
	h.scaler.reconcile()
	assert ("rerun", REPO, 1) in h.github.calls
	assert not h.scaler.annotation_attempts

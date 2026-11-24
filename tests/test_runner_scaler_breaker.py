#!/usr/bin/env python3
"""Circuit breaker over droplet boots (plan goal G5)."""

from __future__ import annotations

from runner_scaler_fakes import REPO, Clock, Harness, job_event
from runner_scaler.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker


def _breaker(clock: Clock) -> CircuitBreaker:
	return CircuitBreaker(consecutive=3, window_seconds=900, fail_ratio=0.5, min_boots=4, cooldown_seconds=900, clock=clock)


def test_opens_after_consecutive_failures_and_allows_one_trial_after_cooldown() -> None:
	clock = Clock()
	breaker = _breaker(clock)
	assert breaker.creates_allowed(trial_in_flight=False) is None
	assert not breaker.record(False)
	assert not breaker.record(False)
	assert breaker.record(False)  # third in a row opens it
	assert breaker.state == OPEN
	assert breaker.creates_allowed(trial_in_flight=False) == 0
	clock.advance(899)
	assert breaker.creates_allowed(trial_in_flight=False) == 0
	clock.advance(2)
	assert breaker.creates_allowed(trial_in_flight=False) == 1
	assert breaker.state == HALF_OPEN
	assert breaker.creates_allowed(trial_in_flight=True) == 0


def test_half_open_success_closes_and_failure_reopens() -> None:
	clock = Clock()
	breaker = _breaker(clock)
	for _ in range(3):
		breaker.record(False)
	clock.advance(901)
	breaker.creates_allowed(trial_in_flight=False)
	assert breaker.record(True) is False
	assert breaker.state == CLOSED
	for _ in range(3):
		breaker.record(False)
	clock.advance(901)
	breaker.creates_allowed(trial_in_flight=False)
	assert breaker.record(False) is True
	assert breaker.state == OPEN


def test_opens_on_failure_ratio_within_the_window() -> None:
	clock = Clock()
	breaker = _breaker(clock)
	for ok in (True, False, True, False, False):
		opened = breaker.record(ok)
	# 3 of 5 boots failed (> 50%) though never 3 in a row before the last.
	assert opened and breaker.state == OPEN


def test_old_failures_leave_the_window() -> None:
	clock = Clock()
	breaker = _breaker(clock)
	breaker.record(False)
	breaker.record(True)
	clock.advance(1000)
	for ok in (True, False, True):
		assert not breaker.record(ok)
	assert breaker.state == CLOSED


def test_scaler_stops_creating_and_alerts_once_when_boots_keep_failing() -> None:
	h = Harness()
	# GitHub keeps listing the three jobs as queued, so full scans keep the demand.
	h.github.runs[(REPO, "queued")] = [{"id": 10}]
	h.github.jobs[10] = [{"id": job_id, "status": "queued", "labels": ["do-runner"]} for job_id in (1, 2, 3)]
	for job_id in (1, 2, 3):
		h.queue(job_id)
	h.scaler.reconcile()
	assert len(h.do.droplets) == 3
	for droplet in h.do.droplets.values():
		droplet["status"] = "off"  # every self-check failed
	h.scaler.reconcile()
	assert h.breaker.state == OPEN
	assert not h.do.droplets  # destroyed, and no replacement while open
	assert h.alerter.keys().count("breaker_open") == 1
	h.scaler.reconcile()
	assert not h.do.droplets
	# After the cooldown, exactly one trial droplet.
	h.clock.advance(h.config.breaker_cooldown_minutes * 60 + 1)
	h.scaler.reconcile()
	assert len(h.do.droplets) == 1
	h.scaler.reconcile()
	assert len(h.do.droplets) == 1
	# The trial registers: the breaker closes and the remaining demand is served.
	trial = h.do.names()[0]
	h.github.register(REPO, trial, busy=True)
	h.scaler.handle_event("workflow_job", job_event("in_progress", 1, runner_name=trial))
	h.scaler.reconcile()
	assert h.breaker.state == CLOSED
	assert len(h.do.droplets) == 3

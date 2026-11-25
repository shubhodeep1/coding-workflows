#!/usr/bin/env python3
"""Reconcile arithmetic, cap, idempotency, cleanup and restart of the runner scaler.

docs/plans/digitalocean-runners-for-consumer-pipelines-plan.md, Phase 2 (goals
G3, G4 and G7).
"""

from __future__ import annotations

import pytest

from runner_scaler_fakes import JIT, OTHER_REPO, REPO, VPC, Harness, job_event
from runner_scaler.api import ApiError
from runner_scaler.reconcile import JIT_PLACEHOLDER, MIN_HEALTH_WINDOW_SECONDS, PENDING_RERUN_MAX_SECONDS, PENDING_RERUN_RETRY_SECONDS, Scaler, render_user_data, validate_user_data_template


def test_one_droplet_per_queued_job_with_name_tags_and_user_data() -> None:
	h = Harness()
	assert h.queue(1) == "queued"
	assert h.queue(2) == "queued"
	summary = h.scaler.reconcile()
	assert summary["created"] == 2
	assert h.do.names() == ["do-runner-digital-pa-00000001", "do-runner-digital-pa-00000002"]
	droplet = h.do.by_name("do-runner-digital-pa-00000001")
	assert droplet["tags"] == ["gh-runner", "gh-runner-repo-digital-pa"]
	create = next(call for call in h.do.calls if call[0] == "create")
	assert create[2:5] == ("nyc3", "s-4vcpu-8gb", "ubuntu-26-04-x64")
	assert create[6] == VPC  # the dedicated runner VPC, never the default one
	user_data = h.do.user_data["do-runner-digital-pa-00000001"]
	assert JIT in user_data and JIT_PLACEHOLDER not in user_data
	assert [call for call in h.github.calls if call[0] == "jit"][0] == ("jit", REPO, "do-runner-digital-pa-00000001", "do-runner")


def test_duplicate_and_replayed_events_never_over_create() -> None:
	h = Harness()
	for _ in range(3):
		h.queue(1)
	h.scaler.reconcile()
	h.queue(1)  # replayed delivery after the droplet exists
	h.scaler.reconcile()
	h.scaler.reconcile()
	assert len(h.do.droplets) == 1


def test_booting_and_idle_droplets_count_as_supply_busy_ones_do_not() -> None:
	h = Harness()
	h.queue(1)
	h.scaler.reconcile()
	name = h.do.names()[0]
	h.github.register(REPO, name, busy=True)
	h.scaler.handle_event("workflow_job", job_event("in_progress", 1, runner_name=name))
	h.queue(2)
	h.scaler.reconcile()
	assert len(h.do.droplets) == 2  # the busy runner is not supply for job 2
	second = [n for n in h.do.names() if n != name][0]
	h.github.register(REPO, second)  # online and idle: job 2 is about to be assigned to it
	h.scaler.reconcile()
	assert len(h.do.droplets) == 2  # job 2 is covered by the idle runner


def test_cap_limits_creates_and_alerts_once_per_pass() -> None:
	h = Harness(max_droplets=2)
	for job_id in range(1, 6):
		h.queue(job_id)
	summary = h.scaler.reconcile()
	assert summary["created"] == 2 and len(h.do.droplets) == 2
	assert h.alerter.keys().count("cap_reached") == 1
	assert h.outcomes("skipped")[-1]["reason"] == "cap_reached"


def test_other_labels_repos_and_events_are_ignored() -> None:
	h = Harness()
	assert h.queue(1, labels=["ubuntu-latest"]) == "ignored_label"
	assert h.queue(2, labels=["do-runner", "x"]) == "ignored_label"
	assert h.queue(3, repo="someone/else") == "ignored_repo"
	assert h.scaler.handle_event("push", {}) == "ignored_event"
	assert h.scaler.handle_event("ping", {}) == "ping"
	h.scaler.reconcile()
	assert not h.do.droplets


def test_completed_job_destroys_its_droplet() -> None:
	h = Harness()
	h.queue(1)
	h.scaler.reconcile()
	name = h.do.names()[0]
	h.github.register(REPO, name, busy=True)
	h.scaler.handle_event("workflow_job", job_event("in_progress", 1, runner_name=name))
	h.scaler.handle_event("workflow_job", job_event("completed", 1, runner_name=name, conclusion="success"))
	summary = h.scaler.reconcile()
	assert summary["destroyed"] == 1 and not h.do.droplets
	assert h.outcomes("destroyed")[-1]["reason"] == "job_completed"


def test_powered_off_droplet_after_its_job_is_destroyed_without_a_boot_failure() -> None:
	h = Harness()
	h.queue(1)
	h.scaler.reconcile()
	name = h.do.names()[0]
	h.github.register(REPO, name, busy=True)
	h.scaler.handle_event("workflow_job", job_event("in_progress", 1, runner_name=name))
	h.scaler.reconcile()  # registered: boot success recorded
	h.do.by_name(name)["status"] = "off"  # job done, completed webhook missed
	h.scaler.reconcile()
	assert not h.do.droplets
	assert h.outcomes("destroyed")[-1]["reason"] == "job_done_powered_off"
	assert [line["result"] for line in h.outcomes("boot")] == ["ok"]


def test_failed_self_check_powers_off_and_counts_a_boot_failure() -> None:
	h = Harness()
	h.queue(1)
	h.scaler.reconcile()
	h.do.by_name(h.do.names()[0])["status"] = "off"
	h.scaler.reconcile()
	assert h.outcomes("boot")[0]["reason"] == "boot_failed"
	assert h.outcomes("destroyed")[0]["reason"] == "boot_failed"
	# The job is still queued, so a replacement droplet is created in the same pass.
	assert len(h.do.droplets) == 1


def test_unregistered_droplet_is_destroyed_after_the_register_timeout() -> None:
	h = Harness()
	h.queue(1)
	h.scaler.reconcile()
	h.clock.advance(h.config.register_timeout_seconds + 1)
	h.scaler.reconcile()
	assert h.outcomes("destroyed")[0]["reason"] == "register_timeout"
	assert h.outcomes("boot")[0]["result"] == "failed"


def test_max_lifetime_destroys_even_unknown_droplets() -> None:
	h = Harness()
	h.do.droplets[1] = {"id": 1, "name": "do-runner-gone-repo-1", "status": "active", "created_at": "2020-01-01T00:00:00Z", "tags": ["gh-runner", "gh-runner-repo-gone-repo"]}
	h.scaler.reconcile()
	assert not h.do.droplets
	assert h.outcomes("destroyed")[0]["reason"] == "max_lifetime"


def test_idle_runner_without_demand_is_destroyed_after_the_idle_window() -> None:
	h = Harness()
	h.queue(1)
	h.scaler.reconcile()
	name = h.do.names()[0]
	h.github.register(REPO, name)
	# The job was picked up elsewhere (cancelled); the runner idles with no demand.
	h.scaler.handle_event("workflow_job", job_event("completed", 1, conclusion="cancelled"))
	h.scaler.reconcile()
	assert h.do.names() == [name]
	h.clock.advance(h.config.register_timeout_seconds + 1)
	h.scaler.reconcile()
	assert not h.do.droplets
	assert h.outcomes("destroyed")[-1]["reason"] == "idle"


def test_restart_rebuilds_from_tags_and_runners_without_duplicates() -> None:
	h = Harness()
	h.queue(1)
	h.scaler.reconcile()
	name = h.do.names()[0]
	restarted = Scaler(config=h.config, github=h.github, do=h.do, breaker=h.breaker, alerter=h.alerter, user_data_template=h.scaler.user_data_template, clock=h.clock, log=h._log, new_suffix=h._suffix)
	# The full scan finds the same queued job; the booting droplet covers it.
	h.github.runs[(REPO, "queued")] = [{"id": 10}]
	h.github.jobs[10] = [{"id": 1, "status": "queued", "labels": ["do-runner"], "created_at": "2026-11-18T00:00:00Z"}]
	restarted.reconcile()
	assert h.do.names() == [name]


def test_full_scan_recovers_a_missed_queued_webhook_and_drops_stale_jobs() -> None:
	h = Harness()
	h.github.runs[(REPO, "in_progress")] = [{"id": 20}]
	h.github.jobs[20] = [
		{"id": 7, "status": "queued", "labels": ["do-runner"], "created_at": "2026-11-18T00:00:00Z"},
		{"id": 8, "status": "in_progress", "labels": ["do-runner"]},
		{"id": 9, "status": "queued", "labels": ["ubuntu-latest"]},
	]
	h.scaler.reconcile()
	assert len(h.do.droplets) == 1
	# A webhook-only job that the next complete scan no longer sees is dropped.
	h.queue(99)
	h.clock.advance(h.config.full_scan_seconds)
	h.github.jobs[20] = []
	h.scaler.reconcile()
	assert h.scaler.queued[REPO] == {}


def test_partial_scan_only_adds_jobs() -> None:
	h = Harness()
	h.queue(5)
	h.github.fail["runs"] = ApiError(502, "bad gateway")
	h.scaler.reconcile()
	assert 5 in h.scaler.queued[REPO]
	assert h.outcomes("scan_incomplete")


def test_do_list_failure_keeps_completions_and_alerts() -> None:
	h = Harness()
	h.do.fail["list"] = ApiError(503, "unavailable")
	h.scaler.handle_event("workflow_job", job_event("completed", 1, runner_name="do-runner-digital-pa-00000009", conclusion="success"))
	h.scaler.reconcile()
	assert "do_api_error" in h.alerter.keys()
	assert h.scaler.completions  # retried next pass
	assert h.scaler.healthy()


def test_runner_list_failure_skips_creates_for_that_repo_only() -> None:
	h = Harness()
	h.queue(1)
	h.queue(2, repo=OTHER_REPO)
	h.github.fail["runners"] = ApiError(500, "boom")
	h.scaler.reconcile()
	assert not h.do.droplets
	assert {line["reason"] for line in h.outcomes("skipped")} == {"runners_list_failed", "runners_unknown"}


def test_droplet_create_failure_releases_the_jit_runner() -> None:
	h = Harness()
	h.queue(1)
	h.do.fail["create"] = ApiError(422, "limit")
	h.scaler.reconcile()
	assert any(call[0] == "delete_runner" for call in h.github.calls)
	assert "do_api_error" in h.alerter.keys()


def test_orphan_offline_runners_are_deleted_within_budget() -> None:
	h = Harness()
	for n in range(15):
		h.github.register(REPO, f"do-runner-digital-pa-orphan{n:02d}", status="offline")
	h.github.register(REPO, "someone-elses-runner", status="offline")
	h.scaler.reconcile()
	deletes = [call for call in h.github.calls if call[0] == "delete_runner"]
	assert len(deletes) == 10
	assert "someone-elses-runner" in h.github.runners[REPO]


def test_queued_without_supply_alert_when_creation_is_blocked() -> None:
	h = Harness(max_droplets=0)
	h.clock.advance(h.config.queue_alert_minutes * 60 + 1)
	h.queue(1)  # created at T0, so it has waited past the alert threshold
	h.scaler.reconcile()
	assert f"queued_without_supply:{REPO}" in h.alerter.keys()


def test_dry_run_calls_no_write_endpoint() -> None:
	h = Harness(dry_run=True)
	h.queue(1)
	h.github.register(REPO, "do-runner-digital-pa-orphan", status="offline")
	h.scaler.reconcile()
	assert not any(call[0] in ("create", "delete") for call in h.do.calls)
	assert not any(call[0] in ("jit", "delete_runner", "rerun") for call in h.github.calls)
	assert {line["action"] for line in h.outcomes("dry_run")} == {"create", "delete_runner"}


def test_inactive_scaler_makes_no_api_call() -> None:
	h = Harness(repos=())
	h.scaler.reconcile()
	assert not h.do.calls and not h.github.calls and h.scaler.healthy()


def test_unhealthy_when_no_pass_completes() -> None:
	h = Harness()
	h.clock.advance(3 * h.config.reconcile_seconds + 1)
	assert h.scaler.healthy()  # a slow full-scan pass gets at least MIN_HEALTH_WINDOW_SECONDS
	h.clock.advance(MIN_HEALTH_WINDOW_SECONDS)
	assert not h.scaler.healthy()
	h.scaler.reconcile()
	assert h.scaler.healthy()


def test_user_data_rendering_rejects_bad_input() -> None:
	template = "#!/usr/bin/env bash\nJIT=\"__RUNNER_JIT_CONFIG__\"\n"
	assert JIT in render_user_data(template, JIT)
	with pytest.raises(ValueError):
		render_user_data(template, "not base64; rm -rf /")
	with pytest.raises(ValueError):
		validate_user_data_template("#!/bin/sh\necho no placeholder\n")
	with pytest.raises(ValueError):
		validate_user_data_template("#!/bin/sh\n__RUNNER_JIT_CONFIG__ __RUNNER_JIT_CONFIG__\n")


# --- regressions from the code review ---------------------------------------


def test_offline_jit_runner_is_booting_supply_across_passes() -> None:
	"""GitHub lists a JIT runner as offline from generation: that is booting, not registered."""
	h = Harness()
	h.queue(1)
	for _ in range(5):
		h.scaler.reconcile()
		h.clock.advance(30)
	assert len(h.do.droplets) == 1
	name = h.do.names()[0]
	assert h.github.runners[REPO][name]["status"] == "offline"
	assert not h.outcomes("boot")  # no verdict while booting


def test_never_connected_droplet_times_out_as_a_boot_failure() -> None:
	h = Harness()
	h.queue(1)
	h.scaler.reconcile()
	h.clock.advance(h.config.register_timeout_seconds + 1)
	h.scaler.reconcile()
	assert h.outcomes("boot")[0] == {"result": "failed", "reason": "register_timeout", "droplet": "do-runner-digital-pa-00000001"}


def test_powered_off_with_runner_gone_is_no_boot_failure() -> None:
	"""Missed webhooks: the job ran, GitHub removed the ephemeral runner, the droplet is off."""
	h = Harness()
	h.queue(1)
	h.scaler.reconcile()
	name = h.do.names()[0]
	del h.github.runners[REPO][name]
	h.do.by_name(name)["status"] = "off"
	h.scaler.handle_event("workflow_job", job_event("completed", 1, conclusion="success"))  # runner_name missing
	h.scaler.reconcile()
	assert h.outcomes("destroyed")[0]["reason"] == "powered_off"
	assert not h.outcomes("boot")


def test_full_scan_does_not_re_add_a_job_that_started_meanwhile() -> None:
	h = Harness()
	h.github.runs[(REPO, "queued")] = [{"id": 10}]
	h.github.jobs[10] = [{"id": 1, "status": "queued", "labels": ["do-runner"]}]
	h.scaler.handle_event("workflow_job", job_event("in_progress", 1, runner_name="do-runner-digital-pa-elsewhere"))
	h.scaler.reconcile()
	assert h.scaler.queued[REPO] == {}
	assert not h.do.droplets


def test_created_droplet_missing_from_a_lagging_list_is_supply_and_keeps_its_runner() -> None:
	h = Harness()
	h.queue(1)
	h.scaler.reconcile()
	name = h.do.names()[0]
	hidden = h.do.droplets.pop(h.do.by_name(name)["id"])  # the tag listing lags the create
	h.scaler.reconcile()
	assert not h.do.droplets  # nothing extra created
	assert name in h.github.runners[REPO]  # its offline JIT runner is not an orphan
	h.do.droplets[hidden["id"]] = hidden
	h.scaler.reconcile()
	assert h.do.names() == [name]


def test_lost_runner_rerun_waits_for_the_run_to_complete() -> None:
	h = Harness()
	attempts = {"n": 0}

	def rerun_job(repo: str, job_id: int) -> None:
		attempts["n"] += 1
		h.github.calls.append(("rerun", repo, job_id))
		if attempts["n"] < 3:
			raise ApiError(403, "This workflow run is not completed")

	h.github.rerun_job = rerun_job
	h.github.annotations[1] = [{"message": "The runner has received a shutdown signal."}]
	h.scaler.handle_event("workflow_job", job_event("completed", 1, runner_name="do-runner-digital-pa-00000009", conclusion="failure"))
	for _ in range(4):
		h.scaler.reconcile()
		h.scaler.reconcile()  # within the retry spacing: no extra re-run call
		h.clock.advance(PENDING_RERUN_RETRY_SECONDS)
	assert attempts["n"] == 3  # deferred twice, accepted once, never again
	assert len(h.outcomes("rerun_deferred")) == 1 and len(h.outcomes("rerun")) == 1
	assert len([call for call in h.github.calls if call[0] == "annotations"]) == 1


def test_lost_runner_rerun_gives_up_after_the_window() -> None:
	h = Harness()

	def rerun_job(repo: str, job_id: int) -> None:
		raise ApiError(403, "This workflow run is not completed")

	h.github.rerun_job = rerun_job
	h.github.annotations[1] = [{"message": "The runner has received a shutdown signal."}]
	h.scaler.handle_event("workflow_job", job_event("completed", 1, runner_name="do-runner-digital-pa-00000009", conclusion="failure"))
	h.scaler.reconcile()
	h.clock.advance(PENDING_RERUN_MAX_SECONDS + 1)
	h.scaler.reconcile()
	assert h.outcomes("rerun_skipped")[-1]["reason"] == "rerun_window_expired"
	assert f"rerun_given_up:{REPO}" in h.alerter.keys()
	assert not h.scaler.pending_reruns


def test_failed_delete_still_counts_against_the_cap_and_alerts() -> None:
	h = Harness(max_droplets=1)
	h.queue(1)
	h.scaler.reconcile()
	name = h.do.names()[0]
	h.do.by_name(name)["status"] = "off"
	original = h.do.delete_droplet

	def failing_delete(droplet_id: int) -> None:
		raise ApiError(500, "boom")

	h.do.delete_droplet = failing_delete
	h.queue(2)
	h.scaler.reconcile()
	assert h.do.names() == [name]  # no new droplet: the undeleted one fills the cap
	assert "destroy_failed" in h.alerter.keys()
	h.do.delete_droplet = original
	h.scaler.reconcile()
	assert name not in h.do.names()


def test_full_page_scan_is_partial_and_keeps_older_jobs() -> None:
	h = Harness(scan_max_runs=2)
	h.queue(5)
	h.clock.advance(120)  # past the webhook grace window
	h.github.runs[(REPO, "queued")] = [{"id": 10}, {"id": 11}]
	h.scaler.reconcile()
	assert 5 in h.scaler.queued[REPO]  # job 5 sits in a run behind the full page
	assert any(line.get("reason") == "page_full" for line in h.outcomes("scan_incomplete"))


def test_create_without_response_keeps_the_runner_and_never_double_creates() -> None:
	h = Harness()
	h.queue(1)
	h.do.fail["create"] = ApiError(0, "TimeoutError")
	h.scaler.reconcile()
	h.scaler.reconcile()
	assert len([call for call in h.do.calls if call[0] == "create"]) == 1
	assert not any(call[0] == "delete_runner" for call in h.github.calls)
	assert h.outcomes("create_failed")[0]["reason"] == "droplet_create_ambiguous"
	h.clock.advance(h.config.register_timeout_seconds + 1)
	h.scaler.reconcile()
	assert h.outcomes("create_lost")
	assert any(call[0] == "delete_runner" for call in h.github.calls)  # its runner became an orphan


def test_ambiguous_create_holds_its_cap_slot_for_the_rest_of_the_pass() -> None:
	h = Harness(max_droplets=1)
	h.queue(1)
	h.queue(2, repo=OTHER_REPO)
	h.do.fail["create"] = ApiError(0, "TimeoutError")
	h.scaler.reconcile()
	# The timed-out create may exist: the other repository must not reuse its slot.
	assert len([call for call in h.do.calls if call[0] == "create"]) == 1


def test_ambiguous_create_is_booting_supply_for_the_queue_alert() -> None:
	h = Harness()
	h.clock.advance(h.config.queue_alert_minutes * 60 + 1)
	h.queue(1)  # created at T0, so it has waited past the alert threshold
	h.do.fail["create"] = ApiError(0, "TimeoutError")
	h.scaler.reconcile()
	# The timed-out create may be booting: no queued_without_supply page for it.
	assert f"queued_without_supply:{REPO}" not in h.alerter.keys()


def test_ambiguous_create_covering_the_only_job_is_not_a_cap_alert() -> None:
	h = Harness(max_droplets=1)
	h.queue(1)
	h.do.fail["create"] = ApiError(0, "TimeoutError")
	h.scaler.reconcile()
	# The timed-out create holds the only slot but covers the only job.
	assert "cap_reached" not in h.alerter.keys()
	assert not any(line.get("reason") == "cap_reached" for line in h.outcomes("skipped"))


def test_waiting_job_is_not_demand_until_it_is_queued_again() -> None:
	h = Harness()
	h.scaler.handle_event("workflow_job", job_event("waiting", 1))
	h.scaler.reconcile()
	assert not h.do.droplets
	h.queue(1)  # the environment protection rule cleared
	h.scaler.reconcile()
	assert len(h.do.droplets) == 1

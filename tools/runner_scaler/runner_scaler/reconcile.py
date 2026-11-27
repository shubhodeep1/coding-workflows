"""Level-triggered reconcile: droplets follow queued `do-runner` jobs (plan goals G3–G7).

Each pass recomputes everything from the current state instead of reacting to
single events:

* demand: the queued `do-runner` job ids per repository. Webhooks keep it
  current between passes; a full scan of queued and in-progress runs replaces
  it every RUNNER_SCALER_FULL_SCAN_SECONDS, so a missed or undelivered webhook
  heals itself.
* supply: the repository's droplets that are still booting plus its runners
  that are online and idle. A droplet is booting from its creation until its
  runner is first seen online (or picks up a job); GitHub lists a just-in-time
  runner as `offline` from the moment it is generated, so a listed-but-offline
  runner that never came online is still booting, not registered.
* the pass creates min(demand - supply, cap - live droplets) droplets (none
  while the circuit breaker is open), and destroys droplets whose job
  completed, that powered off, that never came online, that stayed idle with
  no demand, or that outlived RUNNER_SCALER_MAX_LIFETIME_MINUTES.

Because the pass compares state, a duplicate, replayed or reordered webhook,
a restart, or an API error in one pass can only delay a droplet, never add an
extra one. API calls per pass (CLAUDE.md §15): 1 DigitalOcean list, 1 runners
list per allowlisted repository (up to 3 pages), and, on full-scan passes only,
2 run lists plus at most RUNNER_SCALER_SCAN_MAX_RUNS jobs reads per status per
repository; plus at most PENDING_RERUNS_PER_PASS re-run attempts, each pending
job at most once per PENDING_RERUN_RETRY_SECONDS.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import rerun
from .api import ApiError, RateLimited
from .breaker import HALF_OPEN, CircuitBreaker
from .config import Config, repo_slug
from .log import LogFn, log as default_log

DROPLET_TAG = "gh-runner"
REPO_TAG_PREFIX = "gh-runner-repo-"
JIT_PLACEHOLDER = "__RUNNER_JIT_CONFIG__"
JIT_RE = re.compile(r"^[A-Za-z0-9+/=]{100,60000}$")
MAX_USER_DATA_BYTES = 64 * 1024
ORPHAN_RUNNER_DELETES_PER_PASS = 10
DELETED_MEMORY_SECONDS = 600
MAX_COMPLETIONS = 1000
# A job a webhook reported this recently survives a full scan that does not list
# it yet (the runs listing can lag the webhook).
SCAN_GRACE_SECONDS = 60
# A job a webhook reported as started or completed is kept out of the queue for
# this long, so a full scan that read it as queued just before it started cannot
# put it back.
STARTED_MEMORY_SECONDS = 600
# GitHub re-runs a job only once its run has completed, so a lost-runner re-run
# waits until then: it is retried for at most this long (the longest job
# timeout is 240 minutes), each job at most once per PENDING_RERUN_RETRY_SECONDS
# and at most PENDING_RERUNS_PER_PASS per pass, so a mass droplet loss costs at
# most 12 calls an hour per pending job instead of one per pass (CLAUDE.md §15).
PENDING_RERUN_MAX_SECONDS = 6 * 3600
PENDING_RERUNS_PER_PASS = 10
PENDING_RERUN_RETRY_SECONDS = 300
# A transient failure (transport, 5xx, rate limit) of the annotations read puts
# the completion back for the next pass, at most this many reads per job, so one
# bad response cannot lose a lost-runner re-run.
ANNOTATION_READ_ATTEMPTS = 5
# A list_run_jobs page holds at most this many jobs (github_app.list_run_jobs).
RUN_JOBS_PAGE_SIZE = 100
# A healthy pass must complete at least this often; a full-scan pass over 7
# repositories can take several minutes when the APIs are slow.
MIN_HEALTH_WINDOW_SECONDS = 900


def _epoch(value: Any) -> float | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
	except ValueError:
		return None


def validate_user_data_template(template: str) -> None:
	if template.count(JIT_PLACEHOLDER) != 1:
		raise ValueError("boot script must contain the JIT placeholder exactly once")
	if not template.startswith("#!"):
		raise ValueError("boot script must start with a shebang")


def render_user_data(template: str, jit_config: str) -> str:
	if not JIT_RE.match(jit_config):
		raise ValueError("JIT config is not base64")
	user_data = template.replace(JIT_PLACEHOLDER, jit_config, 1)
	if len(user_data.encode("utf-8")) > MAX_USER_DATA_BYTES:
		raise ValueError("user data exceeds 64 KiB")
	return user_data


class Scaler:
	def __init__(
		self,
		*,
		config: Config,
		github: Any,
		do: Any,
		breaker: CircuitBreaker,
		alerter: Any,
		user_data_template: str,
		clock: Callable[[], float] = time.time,
		log: LogFn = default_log,
		new_suffix: Callable[[], str] = lambda: secrets.token_hex(4),
	):
		validate_user_data_template(user_data_template)
		self.config = config
		self.github = github
		self.do = do
		self.breaker = breaker
		self.alerter = alerter
		self.user_data_template = user_data_template
		self.clock = clock
		self.log = log
		self.new_suffix = new_suffix
		self.repo_by_slug = {repo_slug(repo): repo for repo in config.repos}
		self.repo_by_lower = {repo.lower(): repo for repo in config.repos}
		self._lock = threading.Lock()
		# Guarded by _lock (written by the webhook threads).
		self.queued: dict[str, dict[int, float]] = {repo: {} for repo in config.repos}
		self.completions: list[tuple[str, dict[str, Any]]] = []
		self.webhook_seen: dict[int, float] = {}
		self.started: dict[int, float] = {}
		self.runner_seen: set[str] = set()
		# Reconcile-loop state.
		self.created: dict[str, tuple[float, str]] = {}
		self.online_seen: set[str] = set()
		self.counted: set[str] = set()
		self.idle_since: dict[str, float] = {}
		self.deleted: dict[int, float] = {}
		self.pending_reruns: dict[tuple[str, int], float] = {}
		self.rerun_deferred_logged: set[tuple[str, int]] = set()
		self.rerun_last_try: dict[tuple[str, int], float] = {}
		self.annotation_attempts: dict[int, int] = {}
		self.last_full_scan = 0.0
		self.last_pass_completed = clock()

	# --- webhook side (fast, in-memory only) -------------------------------

	def _repo(self, payload: dict[str, Any]) -> str | None:
		repository = payload.get("repository")
		name = repository.get("full_name") if isinstance(repository, dict) else None
		return self.repo_by_lower.get(str(name or "").lower())

	def handle_event(self, event: str, payload: Any) -> str:
		"""Record one webhook delivery; the next reconcile pass acts on it."""
		if event == "ping":
			return "ping"
		if event != "workflow_job" or not isinstance(payload, dict):
			return "ignored_event"
		repo = self._repo(payload)
		if repo is None:
			return "ignored_repo"
		job = payload.get("workflow_job")
		if not isinstance(job, dict) or job.get("labels") != [self.config.label] or not isinstance(job.get("id"), int):
			return "ignored_label"
		job_id = int(job["id"])
		action = str(payload.get("action") or "")
		now = self.clock()
		with self._lock:
			if action == "queued":
				if job_id not in self.started:
					self.queued[repo].setdefault(job_id, _epoch(job.get("created_at")) or now)
					self.webhook_seen[job_id] = now
			elif action in ("in_progress", "waiting", "completed"):
				# `waiting` is a job held by an environment protection rule (GitHub
				# changelog 2022-11-22), not one waiting for a runner: it is not demand
				# yet. It stays out of `started`, so the `queued` event GitHub sends once
				# the rule clears adds it back.
				self.queued[repo].pop(job_id, None)
				self.webhook_seen.pop(job_id, None)
				if action != "waiting":
					self.started[job_id] = now
				if action == "in_progress" and job.get("runner_name"):
					self.runner_seen.add(str(job["runner_name"]))
				if action == "completed" and len(self.completions) < MAX_COMPLETIONS:
					self.completions.append((repo, job))
			else:
				return "ignored_action"
		return action

	# --- reconcile pass ------------------------------------------------------

	def healthy(self) -> bool:
		window = max(3 * self.config.reconcile_seconds, MIN_HEALTH_WINDOW_SECONDS)
		return self.clock() - self.last_pass_completed <= window

	def reconcile(self) -> dict[str, int]:
		summary = {"created": 0, "destroyed": 0, "reruns": 0}
		now = self.clock()
		if not self.config.active:
			self.last_pass_completed = now
			return summary
		with self._lock:
			completions, self.completions = self.completions, []
			self.started = {job_id: when for job_id, when in self.started.items() if now - when < STARTED_MEMORY_SECONDS}
		self.deleted = {key: when for key, when in self.deleted.items() if now - when < DELETED_MEMORY_SECONDS}
		try:
			droplets = [item for item in self.do.list_droplets(tag=DROPLET_TAG) if int(item.get("id") or 0) not in self.deleted]
		except ApiError as exc:
			self.log("skipped", reason="do_list_failed", status=exc.status)
			self.alerter.alert("do_api_error", f"DigitalOcean droplet list failed (HTTP {exc.status}); no droplet created or destroyed this pass.")
			with self._lock:
				self.completions = completions + self.completions
			self.last_pass_completed = now
			return summary
		completed_names = self._process_completions(completions)
		self._process_pending_reruns(now, summary)
		if now - self.last_full_scan >= self.config.full_scan_seconds:
			self._full_scan(now)
		runners_by_repo: dict[str, dict[str, dict[str, Any]] | None] = {}
		for repo in self.config.repos:
			try:
				runners_by_repo[repo] = {str(item.get("name")): item for item in self.github.list_runners(repo)}
			except ApiError as exc:
				runners_by_repo[repo] = None
				self.log("skipped", reason="rate_limited" if isinstance(exc, RateLimited) else "runners_list_failed", repo=repo, status=exc.status)
		booting = {repo: 0 for repo in self.config.repos}
		idle = {repo: 0 for repo in self.config.repos}
		alive_names: set[str] = set()
		with self._lock:
			queued = {repo: dict(jobs) for repo, jobs in self.queued.items()}
			runner_seen = set(self.runner_seen)
		trial_in_flight = False
		listed_names = {str(droplet.get("name") or "") for droplet in droplets}
		for droplet in droplets:
			name = str(droplet.get("name") or "")
			repo = self._droplet_repo(droplet)
			runners = runners_by_repo.get(repo) if repo else None
			runner = (runners or {}).get(name)
			if runner is not None and runner.get("status") == "online":
				self.online_seen.add(name)
			# `ran`: the runner came online or picked up a job at some point. A JIT
			# runner is listed as offline from creation, so a listed runner alone
			# proves nothing.
			ran = name in self.online_seen or name in runner_seen
			age = now - (_epoch(droplet.get("created_at")) or now)
			status = str(droplet.get("status") or "")
			if name in completed_names:
				self._destroy(droplet, "job_completed", summary, alive_names)
				continue
			if status == "off":
				if ran:
					self._destroy(droplet, "job_done_powered_off", summary, alive_names)
				elif name in self.created and runner is not None and runner.get("status") == "offline":
					# Still registered and never connected: the self-check powered it off.
					self._boot_outcome(name, False, "boot_failed")
					self._destroy(droplet, "boot_failed", summary, alive_names)
				else:
					# The runner is gone (GitHub removes an ephemeral runner after its
					# job), or the droplet is not ours: no boot verdict.
					self._destroy(droplet, "powered_off", summary, alive_names)
				continue
			if age > self.config.max_lifetime_minutes * 60:
				self._destroy(droplet, "max_lifetime", summary, alive_names)
				continue
			if repo is None or runners is None:
				# Unknown repository or its runner list failed: only the lifetime
				# rule applies this pass; the droplet still counts against the cap.
				alive_names.add(name)
				continue
			if not ran:
				if age > self.config.register_timeout_seconds:
					if name in self.created:
						self._boot_outcome(name, False, "register_timeout")
					self._destroy(droplet, "register_timeout", summary, alive_names)
					continue
				booting[repo] += 1
				alive_names.add(name)
				if name in self.created and self.breaker.state == HALF_OPEN:
					trial_in_flight = True
				continue
			if name in self.created:
				self._boot_outcome(name, True, "registered")
			alive_names.add(name)
			if runner is not None and runner.get("status") == "online" and not runner.get("busy"):
				if not queued.get(repo):
					first_idle = self.idle_since.setdefault(name, now)
					if now - first_idle > self.config.register_timeout_seconds:
						self._destroy(droplet, "idle", summary, alive_names)
						continue
				else:
					self.idle_since.pop(name, None)
				idle[repo] += 1
			else:
				self.idle_since.pop(name, None)
		# Droplets this process created that the tag listing does not show yet
		# (DigitalOcean's list can lag a create): booting supply, and their JIT
		# runners are not orphans.
		for name, (created_at, repo) in list(self.created.items()):
			if name in listed_names:
				continue
			if now - created_at < self.config.register_timeout_seconds:
				alive_names.add(name)
				booting[repo] = booting.get(repo, 0) + 1
				if self.breaker.state == HALF_OPEN:
					trial_in_flight = True
			else:
				self.created.pop(name, None)
				self.log("create_lost", droplet=name, repo=repo)
		self._delete_orphan_runners(runners_by_repo, alive_names)
		self._create_for_demand(queued, booting, idle, runners_by_repo, len(alive_names), trial_in_flight, now, summary)
		self.last_pass_completed = self.clock()
		return summary

	def _droplet_repo(self, droplet: dict[str, Any]) -> str | None:
		for tag in droplet.get("tags") or []:
			if isinstance(tag, str) and tag.startswith(REPO_TAG_PREFIX):
				return self.repo_by_slug.get(tag[len(REPO_TAG_PREFIX):])
		return None

	def _boot_outcome(self, name: str, success: bool, reason: str) -> None:
		if name in self.counted:
			return
		self.counted.add(name)
		self.log("boot", result="ok" if success else "failed", reason=reason, droplet=name)
		if self.breaker.record(success):
			self.log("breaker_open", reason=reason)
			self.alerter.alert("breaker_open", f"circuit breaker opened after droplet boot failures (last: {reason}); no droplet is created until a trial boot succeeds. Rollback: unset PIPELINE_RUNS_ON on the consumers.")

	def _destroy(self, droplet: dict[str, Any], reason: str, summary: dict[str, int], alive_names: set[str]) -> None:
		droplet_id = int(droplet.get("id") or 0)
		name = str(droplet.get("name") or "")
		if self.config.dry_run:
			self.log("dry_run", action="destroy", reason=reason, droplet=name)
			alive_names.add(name)
			return
		try:
			self.do.delete_droplet(droplet_id)
		except ApiError as exc:
			if exc.status != 404:
				# Still running: it counts against the cap until a later pass deletes it.
				alive_names.add(name)
				self.log("destroy_failed", reason=reason, droplet=name, status=exc.status)
				self.alerter.alert("destroy_failed", f"droplet delete failed (HTTP {exc.status}); it is retried every pass and counts against the cap.")
				return
		self.deleted[droplet_id] = self.clock()
		self.created.pop(name, None)
		self.idle_since.pop(name, None)
		self.counted.discard(name)
		self.online_seen.discard(name)
		with self._lock:
			self.runner_seen.discard(name)
		summary["destroyed"] += 1
		self.log("destroyed", reason=reason, droplet=name)

	def _process_completions(self, completions: list[tuple[str, dict[str, Any]]]) -> set[str]:
		names: set[str] = set()
		retry_later: list[tuple[str, dict[str, Any]]] = []
		for repo, job in completions:
			runner_name = str(job.get("runner_name") or "")
			if runner_name:
				names.add(runner_name)
			if not (self.config.rerun_enabled and rerun.needs_annotations(job, label=self.config.label)):
				continue
			job_id = int(job["id"])
			try:
				annotations = self.github.job_annotations(repo, job_id)
			except ApiError as exc:
				attempts = self.annotation_attempts.get(job_id, 0) + 1
				transient = isinstance(exc, RateLimited) or exc.status == 0 or exc.status >= 500
				if transient and attempts < ANNOTATION_READ_ATTEMPTS:
					self.annotation_attempts[job_id] = attempts
					self.log("rerun_deferred", reason="annotations_failed", repo=repo, job=job_id, status=exc.status, attempt=attempts)
					retry_later.append((repo, job))
					continue
				self.annotation_attempts.pop(job_id, None)
				self.log("rerun_skipped", reason="annotations_failed", repo=repo, job=job_id, status=exc.status)
				continue
			self.annotation_attempts.pop(job_id, None)
			decision = rerun.decide(job, label=self.config.label, annotations=annotations)
			if decision == rerun.GIVEN_UP:
				self.log("rerun_skipped", reason="attempt_limit", repo=repo, job=job_id, attempt=job.get("run_attempt"))
				self.alerter.alert(f"rerun_given_up:{repo}", f"{repo} job {job_id} lost its droplet again at attempt {job.get('run_attempt')}; not re-run.")
			elif decision == rerun.RERUN:
				self.pending_reruns.setdefault((repo, job_id), self.clock())
		if retry_later:
			with self._lock:
				self.completions = (retry_later + self.completions)[:MAX_COMPLETIONS]
		return names

	def _process_pending_reruns(self, now: float, summary: dict[str, int]) -> None:
		"""Re-run lost-runner jobs; GitHub accepts a re-run only after the run completed."""
		due = [(key, first) for key, first in self.pending_reruns.items() if now - self.rerun_last_try.get(key, float("-inf")) >= PENDING_RERUN_RETRY_SECONDS]
		for (repo, job_id), first in due[:PENDING_RERUNS_PER_PASS]:
			if self.config.dry_run:
				self.log("dry_run", action="rerun", repo=repo, job=job_id)
				del self.pending_reruns[(repo, job_id)]
				continue
			self.rerun_last_try[(repo, job_id)] = now
			try:
				self.github.rerun_job(repo, job_id)
			except ApiError as exc:
				if now - first > PENDING_RERUN_MAX_SECONDS:
					del self.pending_reruns[(repo, job_id)]
					self.rerun_deferred_logged.discard((repo, job_id))
					self.rerun_last_try.pop((repo, job_id), None)
					self.log("rerun_skipped", reason="rerun_window_expired", repo=repo, job=job_id, status=exc.status)
					self.alerter.alert(f"rerun_given_up:{repo}", f"{repo} job {job_id} lost its droplet and could not be re-run within {PENDING_RERUN_MAX_SECONDS // 3600} h (HTTP {exc.status}).")
				elif (repo, job_id) not in self.rerun_deferred_logged:
					self.rerun_deferred_logged.add((repo, job_id))
					self.log("rerun_deferred", reason="run_not_completed", repo=repo, job=job_id, status=exc.status)
				continue
			del self.pending_reruns[(repo, job_id)]
			self.rerun_deferred_logged.discard((repo, job_id))
			self.rerun_last_try.pop((repo, job_id), None)
			summary["reruns"] += 1
			self.log("rerun", reason="runner_lost", repo=repo, job=job_id)

	def _full_scan(self, now: float) -> None:
		for repo in self.config.repos:
			found: dict[int, float] = {}
			complete = True
			for status in ("queued", "in_progress"):
				try:
					runs = self.github.list_runs(repo, status=status, per_page=self.config.scan_max_runs)
				except ApiError as exc:
					complete = False
					self.log("scan_incomplete", repo=repo, status_filter=status, http=exc.status)
					break
				if len(runs) >= self.config.scan_max_runs:
					# A full page may hide older runs: a partial scan only adds jobs.
					complete = False
					self.log("scan_incomplete", repo=repo, status_filter=status, reason="page_full")
				for run in runs[: self.config.scan_max_runs]:
					try:
						jobs = self.github.list_run_jobs(repo, int(run.get("id") or 0))
					except ApiError as exc:
						complete = False
						self.log("scan_incomplete", repo=repo, run=run.get("id"), http=exc.status)
						continue
					if len(jobs) >= RUN_JOBS_PAGE_SIZE:
						complete = False
					for job in jobs:
						if job.get("status") == "queued" and job.get("labels") == [self.config.label] and isinstance(job.get("id"), int):
							found[int(job["id"])] = _epoch(job.get("created_at")) or now
			with self._lock:
				# A job that started while the scan read it as queued stays out.
				found = {job_id: first_seen for job_id, first_seen in found.items() if job_id not in self.started}
				known = self.queued.get(repo, {})
				if complete:
					fresh = {job_id: first_seen for job_id, first_seen in known.items() if job_id not in found and self.webhook_seen.get(job_id, 0.0) >= now - SCAN_GRACE_SECONDS}
					self.queued[repo] = {job_id: known.get(job_id, first_seen) for job_id, first_seen in found.items()} | fresh
					for job_id in set(known) - set(self.queued[repo]):
						self.webhook_seen.pop(job_id, None)
				else:
					# A partial scan may only add jobs, never drop them.
					for job_id, first_seen in found.items():
						known.setdefault(job_id, first_seen)
					self.queued[repo] = known
		self.last_full_scan = now

	def _delete_orphan_runners(self, runners_by_repo: dict[str, dict[str, dict[str, Any]] | None], alive_names: set[str]) -> None:
		budget = ORPHAN_RUNNER_DELETES_PER_PASS
		prefix = f"{self.config.label}-"
		for repo, runners in runners_by_repo.items():
			for name, runner in (runners or {}).items():
				if budget <= 0:
					return
				if not name.startswith(prefix) or name in alive_names or name in self.created or runner.get("status") != "offline" or not isinstance(runner.get("id"), int):
					continue
				budget -= 1
				if self.config.dry_run:
					self.log("dry_run", action="delete_runner", repo=repo, runner=name)
					continue
				try:
					self.github.delete_runner(repo, int(runner["id"]))
					self.log("runner_deleted", reason="orphan", repo=repo, runner=name)
				except ApiError as exc:
					self.log("runner_delete_failed", repo=repo, runner=name, status=exc.status)

	def _create_for_demand(self, queued: dict[str, dict[int, float]], booting: dict[str, int], idle: dict[str, int], runners_by_repo: dict[str, Any], live: int, trial_in_flight: bool, now: float, summary: dict[str, int]) -> None:
		allowed = self.breaker.creates_allowed(trial_in_flight=trial_in_flight)
		capacity = max(0, self.config.max_droplets - live)
		order = sorted(self.config.repos, key=lambda repo: min(queued.get(repo, {}).values(), default=now))
		for repo in order:
			demand = len(queued.get(repo, {}))
			if demand == 0:
				continue
			want = demand - booting[repo] - idle[repo]
			created_here = 0
			if want > 0 and runners_by_repo.get(repo) is None:
				self.log("skipped", reason="runners_unknown", repo=repo, wanted=want)
				want = 0
			if want > 0 and allowed == 0:
				self.log("skipped", reason="breaker_open", repo=repo, wanted=want)
				want = 0
			while want > 0 and capacity > 0 and (allowed is None or allowed > 0):
				tracked_before = len(self.created)
				if not self._create(repo, now):
					if len(self.created) > tracked_before:
						# Ambiguous create (no response): the droplet may exist, so it
						# holds a cap slot and the half-open trial for the rest of the pass.
						capacity -= 1
						if allowed is not None:
							allowed -= 1
						# It is booting supply (as the next pass counts it), so the
						# queued_without_supply check below must not page for it.
						booting[repo] += 1
						# It covers one job, so the cap_reached check below only counts
						# jobs the cap really leaves without a droplet.
						want -= 1
					break
				want -= 1
				capacity -= 1
				created_here += 1
				summary["created"] += 1
				if allowed is not None:
					allowed -= 1
			if want > 0 and capacity == 0:
				self.log("skipped", reason="cap_reached", repo=repo, wanted=want, cap=self.config.max_droplets)
				self.alerter.alert("cap_reached", f"droplet cap of {self.config.max_droplets} reached; do-runner jobs wait in GitHub's queue.")
			oldest = min(queued.get(repo, {}).values(), default=now)
			if booting[repo] == 0 and created_here == 0 and now - oldest > self.config.queue_alert_minutes * 60:
				self.alerter.alert(f"queued_without_supply:{repo}", f"{repo} has do-runner jobs queued for {int((now - oldest) // 60)} min with no droplet booting for them. Rollback: unset PIPELINE_RUNS_ON on {repo}.")

	def _create(self, repo: str, now: float) -> bool:
		slug = repo_slug(repo)
		name = f"{self.config.label}-{slug}-{self.new_suffix()}"
		if self.config.dry_run:
			self.log("dry_run", action="create", repo=repo, droplet=name)
			return True
		try:
			runner_id, jit_config = self.github.generate_jit_config(repo, name=name, label=self.config.label)
		except ApiError as exc:
			self.log("create_failed", reason="jit_config_failed", repo=repo, status=exc.status)
			return False
		try:
			user_data = render_user_data(self.user_data_template, jit_config)
			self.do.create_droplet(
				name=name,
				region=self.config.region,
				size=self.config.droplet_size,
				image=self.config.image,
				tags=[DROPLET_TAG, f"{REPO_TAG_PREFIX}{slug}"],
				user_data=user_data,
				vpc_uuid=self.config.vpc_uuid,
			)
		except (ApiError, ValueError) as exc:
			status = exc.status if isinstance(exc, ApiError) else 0
			if isinstance(exc, ApiError) and status == 0:
				# Transport failure: DigitalOcean may have accepted the create. Keep the
				# JIT runner and count the droplet as booting; if the tag listing never
				# shows it, it is dropped as create_lost after the register timeout and
				# its offline runner is then deleted as an orphan.
				self.created[name] = (now, repo)
				self.log("create_failed", reason="droplet_create_ambiguous", repo=repo, droplet=name, status=status)
				self.alerter.alert("do_api_error", f"droplet create for {repo} got no response; counted as booting until it shows up or times out.")
				return False
			self.log("create_failed", reason="droplet_create_failed", repo=repo, droplet=name, status=status)
			self.alerter.alert("do_api_error", f"droplet create failed for {repo} (HTTP {status}).")
			try:
				self.github.delete_runner(repo, runner_id)
			except ApiError:
				pass
			return False
		self.created[name] = (now, repo)
		self.log("created", repo=repo, droplet=name, size=self.config.droplet_size)
		return True

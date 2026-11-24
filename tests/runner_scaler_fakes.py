"""Fakes shared by tests/test_runner_scaler_*.py (stubbed GitHub, DigitalOcean, clock)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCALER_ROOT = ROOT / "tools" / "runner_scaler"
if str(SCALER_ROOT) not in sys.path:
	sys.path.insert(0, str(SCALER_ROOT))

from runner_scaler.api import ApiError  # noqa: E402
from runner_scaler.breaker import CircuitBreaker  # noqa: E402
from runner_scaler.config import Config  # noqa: E402
from runner_scaler.reconcile import Scaler  # noqa: E402

BOOT_SCRIPT = SCALER_ROOT / "bootstrap" / "runner-boot.sh"
REPO = "shubhodeep1/digital_pa"
OTHER_REPO = "shubhodeep1/bitsafe.io"
JIT = "A" * 120
VPC = "5a4f0c1e-0000-4000-8000-00000000abcd"
T0 = 1_800_000_000.0


def iso(epoch: float) -> str:
	return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
	def __init__(self, now: float = T0):
		self.now = now

	def __call__(self) -> float:
		return self.now

	def advance(self, seconds: float) -> None:
		self.now += seconds


class FakeGitHub:
	def __init__(self, clock: Clock):
		self.clock = clock
		self.runners: dict[str, dict[str, dict[str, Any]]] = {}
		self.runs: dict[tuple[str, str], list[dict[str, Any]]] = {}
		self.jobs: dict[int, list[dict[str, Any]]] = {}
		self.annotations: dict[int, Any] = {}
		self.calls: list[tuple[Any, ...]] = []
		self.fail: dict[str, ApiError] = {}
		self.next_runner_id = 500

	def _maybe_fail(self, name: str) -> None:
		if name in self.fail:
			raise self.fail[name]

	def generate_jit_config(self, repo: str, *, name: str, label: str) -> tuple[int, str]:
		self.calls.append(("jit", repo, name, label))
		self._maybe_fail("jit")
		# Like GitHub: the JIT runner is listed, offline, from the moment it is generated.
		self.register(repo, name, status="offline")
		return self.next_runner_id, JIT

	def list_runners(self, repo: str) -> list[dict[str, Any]]:
		self.calls.append(("runners", repo))
		self._maybe_fail("runners")
		return list(self.runners.get(repo, {}).values())

	def delete_runner(self, repo: str, runner_id: int) -> None:
		self.calls.append(("delete_runner", repo, runner_id))
		for name, runner in list(self.runners.get(repo, {}).items()):
			if runner.get("id") == runner_id:
				del self.runners[repo][name]

	def list_runs(self, repo: str, *, status: str, per_page: int) -> list[dict[str, Any]]:
		self.calls.append(("runs", repo, status))
		self._maybe_fail("runs")
		return list(self.runs.get((repo, status), []))[:per_page]

	def list_run_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
		self.calls.append(("jobs", repo, run_id))
		return list(self.jobs.get(run_id, []))

	def job_annotations(self, repo: str, job_id: int) -> Any:
		self.calls.append(("annotations", repo, job_id))
		self._maybe_fail("annotations")
		return self.annotations.get(job_id, [])

	def rerun_job(self, repo: str, job_id: int) -> None:
		self.calls.append(("rerun", repo, job_id))

	def register(self, repo: str, name: str, *, status: str = "online", busy: bool = False) -> None:
		self.next_runner_id += 1
		self.runners.setdefault(repo, {})[name] = {"id": self.next_runner_id, "name": name, "status": status, "busy": busy}


class FakeDO:
	def __init__(self, clock: Clock):
		self.clock = clock
		self.droplets: dict[int, dict[str, Any]] = {}
		self.calls: list[tuple[Any, ...]] = []
		self.fail: dict[str, ApiError] = {}
		self.next_id = 1000
		self.user_data: dict[str, str] = {}

	def create_droplet(self, *, name: str, region: str, size: str, image: str, tags: list[str], user_data: str, vpc_uuid: str) -> dict[str, Any]:
		self.calls.append(("create", name, region, size, image, tuple(tags), vpc_uuid))
		if "create" in self.fail:
			raise self.fail["create"]
		self.next_id += 1
		droplet = {"id": self.next_id, "name": name, "status": "new", "created_at": iso(self.clock()), "tags": list(tags)}
		self.droplets[self.next_id] = droplet
		self.user_data[name] = user_data
		return droplet

	def list_droplets(self, *, tag: str) -> list[dict[str, Any]]:
		self.calls.append(("list", tag))
		if "list" in self.fail:
			raise self.fail["list"]
		return [dict(item) for item in self.droplets.values() if tag in item.get("tags", [])]

	def delete_droplet(self, droplet_id: int) -> None:
		self.calls.append(("delete", droplet_id))
		if droplet_id not in self.droplets:
			raise ApiError(404, "not found")
		del self.droplets[droplet_id]

	def by_name(self, name: str) -> dict[str, Any]:
		return next(item for item in self.droplets.values() if item["name"] == name)

	def names(self) -> list[str]:
		return sorted(item["name"] for item in self.droplets.values())


class FakeAlerter:
	def __init__(self) -> None:
		self.alerts: list[tuple[str, str]] = []

	def alert(self, key: str, text: str) -> bool:
		self.alerts.append((key, text))
		return True

	def keys(self) -> list[str]:
		return [key for key, _ in self.alerts]


class Harness:
	def __init__(self, **config_overrides: Any):
		self.clock = Clock()
		self.github = FakeGitHub(self.clock)
		self.do = FakeDO(self.clock)
		self.alerter = FakeAlerter()
		self.lines: list[tuple[str, dict[str, Any]]] = []
		settings = {"repos": (REPO, OTHER_REPO), "do_token": "t", "gh_app_id": "1", "gh_app_private_key": "k", "webhook_secret": "s", "vpc_uuid": VPC}
		settings.update(config_overrides)
		self.config = Config(**settings)
		self.breaker = CircuitBreaker(
			consecutive=self.config.breaker_consecutive,
			window_seconds=self.config.breaker_window_minutes * 60,
			fail_ratio=self.config.breaker_fail_ratio,
			min_boots=self.config.breaker_min_boots,
			cooldown_seconds=self.config.breaker_cooldown_minutes * 60,
			clock=self.clock,
		)
		self.suffix = 0
		self.scaler = Scaler(
			config=self.config,
			github=self.github,
			do=self.do,
			breaker=self.breaker,
			alerter=self.alerter,
			user_data_template=BOOT_SCRIPT.read_text(encoding="utf-8"),
			clock=self.clock,
			log=self._log,
			new_suffix=self._suffix,
		)

	def _suffix(self) -> str:
		self.suffix += 1
		return f"{self.suffix:08x}"

	def _log(self, outcome: str, **fields: Any) -> None:
		self.lines.append((outcome, fields))

	def outcomes(self, outcome: str) -> list[dict[str, Any]]:
		return [fields for name, fields in self.lines if name == outcome]

	def queue(self, job_id: int, *, repo: str = REPO, labels: list[str] | None = None) -> str:
		return self.scaler.handle_event("workflow_job", job_event("queued", job_id, repo=repo, labels=labels))


def job_event(action: str, job_id: int, *, repo: str = REPO, labels: list[str] | None = None, runner_name: str = "", conclusion: str | None = None, run_attempt: int = 1) -> dict[str, Any]:
	return {
		"action": action,
		"repository": {"full_name": repo},
		"workflow_job": {
			"id": job_id,
			"run_id": job_id * 10,
			"run_attempt": run_attempt,
			"labels": labels if labels is not None else ["do-runner"],
			"runner_name": runner_name,
			"status": "completed" if action == "completed" else action,
			"conclusion": conclusion,
			"created_at": iso(T0),
		},
	}

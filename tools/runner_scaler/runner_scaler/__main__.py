"""Entry point: `python -m runner_scaler` (the App Platform run command).

Lifecycle (CLAUDE.md §18.C): App Platform starts and restarts the container
(on a crash or a failing GET /healthz). On SIGTERM or SIGINT the loop stops
accepting work, finishes the reconcile pass in progress and exits 0; droplets
keep running, because runners talk to GitHub directly, and the next instance
picks them up from their tags. With RUNNER_SCALER_REPOS unset the service only
answers /healthz and verifies webhooks.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Mapping

from .alerts import Alerter
from .breaker import CircuitBreaker
from .config import from_env
from .do_client import DigitalOcean
from .github_app import GitHubApp
from .log import log
from .reconcile import Scaler
from .server import App, make_server

BOOT_SCRIPT = Path(__file__).resolve().parent.parent / "bootstrap" / "runner-boot.sh"
# Webhook bursts are debounced to one pass per 15 s: each pass costs one runners
# list per repository, about 1,700 calls an hour for 7 repositories when webhooks
# arrive continuously. Full scans every 10 minutes add at most 2 run lists plus
# 2 x RUNNER_SCALER_SCAN_MAX_RUNS jobs reads per repository, about 1,760 an hour
# more at the defaults, so the worst case is about 3,450 GitHub calls an hour,
# under the installation's 5,000 (CLAUDE.md §15). Pending lost-runner re-runs
# add at most 12 calls an hour per pending job (reconcile.PENDING_RERUN_RETRY_SECONDS).
MIN_PASS_INTERVAL_SECONDS = 15


def main(env: Mapping[str, str] = os.environ) -> int:
	config = from_env(env)
	missing = config.missing_required() if config.active else []
	if missing:
		log("config_error", reason="missing_required", names=",".join(missing))
		return 2
	clock = time.time
	scaler = Scaler(
		config=config,
		github=GitHubApp(app_id=config.gh_app_id, private_key=config.gh_app_private_key, clock=clock),
		do=DigitalOcean(token=config.do_token),
		breaker=CircuitBreaker(
			consecutive=config.breaker_consecutive,
			window_seconds=config.breaker_window_minutes * 60,
			fail_ratio=config.breaker_fail_ratio,
			min_boots=config.breaker_min_boots,
			cooldown_seconds=config.breaker_cooldown_minutes * 60,
			clock=clock,
		),
		alerter=Alerter(bot_token=config.tg_bot_secret, chat_id=config.tg_chat_id, cooldown_seconds=config.alert_cooldown_minutes * 60, clock=clock),
		user_data_template=BOOT_SCRIPT.read_text(encoding="utf-8"),
		clock=clock,
	)
	wake = threading.Event()
	stop = threading.Event()
	server = make_server(App(secret=config.webhook_secret, handle_event=scaler.handle_event, healthy=scaler.healthy, wake=wake), config.port)

	def _stop(signum: int, _frame: object) -> None:
		log("stopping", signal=signum)
		stop.set()
		wake.set()

	signal.signal(signal.SIGTERM, _stop)
	signal.signal(signal.SIGINT, _stop)
	threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
	log("started", repos=len(config.repos), dry_run=str(config.dry_run).lower(), cap=config.max_droplets, port=config.port)
	last_pass = 0.0
	# The first pass runs at once, so a restart picks up existing droplets immediately.
	wake.set()
	while not stop.is_set():
		wake.wait(timeout=config.reconcile_seconds)
		wake.clear()
		if stop.is_set():
			break
		wait = MIN_PASS_INTERVAL_SECONDS - (clock() - last_pass)
		if wait > 0 and stop.wait(wait):
			break
		last_pass = clock()
		try:
			summary = scaler.reconcile()
			log("pass", **summary)
		except Exception as exc:  # noqa: BLE001 (one bad pass must not kill the loop)
			log("pass_failed", error=type(exc).__name__)
	server.shutdown()
	log("stopped")
	return 0


if __name__ == "__main__":
	sys.exit(main())

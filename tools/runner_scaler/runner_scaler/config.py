"""Configuration from the environment, with a default for every setting (CLAUDE.md §4).

Unset RUNNER_SCALER_REPOS means the scaler serves no repository: it answers
/healthz and verifies webhooks but makes no API call. Invalid values fall back
to the default and are logged (`outcome=config_invalid`), never fatal; missing
credentials or a missing RUNNER_SCALER_VPC_UUID while repositories are
configured are fatal (`config_error`), so a half-configured deployment fails
visibly instead of queueing jobs forever, and no runner droplet can land in the
region's default VPC next to the account's other droplets.

coding-workflows itself is never served (decision Q24 C of the plan): its CI
stays on GitHub-hosted runners, so RUNNER_SCALER_REPOS skips it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .log import LogFn, log as default_log

REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
SLUG_RE = re.compile(r"^[a-z0-9-]{1,63}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EXCLUDED_REPOS = ("shubhodeep1/coding-workflows",)


@dataclass(frozen=True)
class Config:
	repos: tuple[str, ...] = ()
	label: str = "do-runner"
	droplet_size: str = "s-4vcpu-8gb"
	region: str = "nyc3"
	image: str = "ubuntu-26-04-x64"
	vpc_uuid: str = ""
	max_droplets: int = 60
	reconcile_seconds: int = 120
	full_scan_seconds: int = 600
	scan_max_runs: int = 20
	register_timeout_seconds: int = 900
	max_lifetime_minutes: int = 300
	queue_alert_minutes: int = 10
	breaker_consecutive: int = 3
	breaker_window_minutes: int = 15
	breaker_fail_ratio: float = 0.5
	breaker_min_boots: int = 4
	breaker_cooldown_minutes: int = 15
	alert_cooldown_minutes: int = 60
	rerun_enabled: bool = True
	dry_run: bool = False
	port: int = 8080
	do_token: str = ""
	gh_app_id: str = ""
	gh_app_private_key: str = ""
	webhook_secret: str = ""
	tg_bot_secret: str = ""
	tg_chat_id: str = ""

	@property
	def active(self) -> bool:
		return bool(self.repos)

	def missing_credentials(self) -> list[str]:
		missing = []
		for name, value in (
			("RUNNER_SCALER_DO_TOKEN", self.do_token),
			("RUNNER_SCALER_GH_APP_ID", self.gh_app_id),
			("RUNNER_SCALER_GH_APP_PRIVATE_KEY", self.gh_app_private_key),
			("RUNNER_SCALER_WEBHOOK_SECRET", self.webhook_secret),
		):
			if not value:
				missing.append(name)
		return missing

	def missing_required(self) -> list[str]:
		"""Settings without which an active scaler must not start."""
		missing = self.missing_credentials()
		if not self.vpc_uuid:
			missing.append("RUNNER_SCALER_VPC_UUID")
		return missing


def repo_slug(repo: str) -> str:
	"""Droplet-name and tag-safe form of a repository name (`owner/Name.io` -> `name-io`)."""
	name = repo.split("/", 1)[-1].lower()
	slug = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")
	return (slug or "repo")[:40].strip("-") or "repo"


def _int(env: Mapping[str, str], key: str, default: int, low: int, high: int, log: LogFn) -> int:
	raw = env.get(key, "").strip()
	if not raw:
		return default
	try:
		value = int(raw)
	except ValueError:
		value = low - 1
	if value < low or value > high:
		log("config_invalid", key=key, using=default)
		return default
	return value


def _float(env: Mapping[str, str], key: str, default: float, low: float, high: float, log: LogFn) -> float:
	raw = env.get(key, "").strip()
	if not raw:
		return default
	try:
		value = float(raw)
	except ValueError:
		value = low - 1
	if not (low <= value <= high):
		log("config_invalid", key=key, using=default)
		return default
	return value


def _bool(env: Mapping[str, str], key: str, default: bool, log: LogFn) -> bool:
	raw = env.get(key, "").strip().lower()
	if not raw:
		return default
	if raw in ("true", "1", "yes"):
		return True
	if raw in ("false", "0", "no"):
		return False
	log("config_invalid", key=key, using=str(default).lower())
	return default


def _str(env: Mapping[str, str], key: str, default: str, pattern: re.Pattern[str], log: LogFn) -> str:
	raw = env.get(key, "").strip()
	if not raw:
		return default
	if not pattern.match(raw):
		log("config_invalid", key=key, using=default)
		return default
	return raw


def _repos(env: Mapping[str, str], log: LogFn) -> tuple[str, ...]:
	raw = env.get("RUNNER_SCALER_REPOS", "")
	repos: list[str] = []
	slugs: dict[str, str] = {}
	for item in re.split(r"[\s,]+", raw.strip()):
		if not item:
			continue
		if not REPO_RE.match(item):
			log("config_invalid", key="RUNNER_SCALER_REPOS", entry=item, using="skipped")
			continue
		if item.lower() in EXCLUDED_REPOS:
			log("config_invalid", key="RUNNER_SCALER_REPOS", entry=item, reason="excluded_repo", using="skipped")
			continue
		slug = repo_slug(item)
		if slug in slugs and slugs[slug].lower() != item.lower():
			log("config_invalid", key="RUNNER_SCALER_REPOS", entry=item, reason="slug_collision", using="skipped")
			continue
		if item.lower() in (repo.lower() for repo in repos):
			continue
		slugs[slug] = item
		repos.append(item)
	return tuple(repos)


def from_env(env: Mapping[str, str], log: LogFn = default_log) -> Config:
	defaults = Config()
	return Config(
		repos=_repos(env, log),
		label=_str(env, "RUNNER_SCALER_LABEL", defaults.label, LABEL_RE, log),
		droplet_size=_str(env, "RUNNER_SCALER_DROPLET_SIZE", defaults.droplet_size, SLUG_RE, log),
		region=_str(env, "RUNNER_SCALER_REGION", defaults.region, SLUG_RE, log),
		image=_str(env, "RUNNER_SCALER_IMAGE", defaults.image, SLUG_RE, log),
		vpc_uuid=_str(env, "RUNNER_SCALER_VPC_UUID", defaults.vpc_uuid, UUID_RE, log),
		max_droplets=_int(env, "RUNNER_SCALER_MAX_DROPLETS", defaults.max_droplets, 0, 500, log),
		reconcile_seconds=_int(env, "RUNNER_SCALER_RECONCILE_SECONDS", defaults.reconcile_seconds, 30, 3600, log),
		full_scan_seconds=_int(env, "RUNNER_SCALER_FULL_SCAN_SECONDS", defaults.full_scan_seconds, 60, 86400, log),
		scan_max_runs=_int(env, "RUNNER_SCALER_SCAN_MAX_RUNS", defaults.scan_max_runs, 1, 100, log),
		register_timeout_seconds=_int(env, "RUNNER_SCALER_REGISTER_TIMEOUT_SECONDS", defaults.register_timeout_seconds, 120, 7200, log),
		max_lifetime_minutes=_int(env, "RUNNER_SCALER_MAX_LIFETIME_MINUTES", defaults.max_lifetime_minutes, 30, 1440, log),
		queue_alert_minutes=_int(env, "RUNNER_SCALER_QUEUE_ALERT_MINUTES", defaults.queue_alert_minutes, 1, 1440, log),
		breaker_consecutive=_int(env, "RUNNER_SCALER_BREAKER_CONSECUTIVE", defaults.breaker_consecutive, 1, 100, log),
		breaker_window_minutes=_int(env, "RUNNER_SCALER_BREAKER_WINDOW_MINUTES", defaults.breaker_window_minutes, 1, 1440, log),
		breaker_fail_ratio=_float(env, "RUNNER_SCALER_BREAKER_FAIL_RATIO", defaults.breaker_fail_ratio, 0.0, 1.0, log),
		breaker_min_boots=_int(env, "RUNNER_SCALER_BREAKER_MIN_BOOTS", defaults.breaker_min_boots, 1, 1000, log),
		breaker_cooldown_minutes=_int(env, "RUNNER_SCALER_BREAKER_COOLDOWN_MINUTES", defaults.breaker_cooldown_minutes, 1, 1440, log),
		alert_cooldown_minutes=_int(env, "RUNNER_SCALER_ALERT_COOLDOWN_MINUTES", defaults.alert_cooldown_minutes, 1, 1440, log),
		rerun_enabled=_bool(env, "RUNNER_SCALER_RERUN_ENABLED", defaults.rerun_enabled, log),
		dry_run=_bool(env, "RUNNER_SCALER_DRY_RUN", defaults.dry_run, log),
		port=_int(env, "PORT", defaults.port, 1, 65535, log),
		do_token=env.get("RUNNER_SCALER_DO_TOKEN", "").strip(),
		gh_app_id=env.get("RUNNER_SCALER_GH_APP_ID", "").strip(),
		gh_app_private_key=env.get("RUNNER_SCALER_GH_APP_PRIVATE_KEY", "").replace("\\n", "\n").strip(),
		webhook_secret=env.get("RUNNER_SCALER_WEBHOOK_SECRET", "").strip(),
		tg_bot_secret=env.get("TG_BOT_SECRET", "").strip(),
		tg_chat_id=(env.get("TG_ADMIN_CHAT_ID", "") or env.get("TG_CHAT_ID", "")).strip(),
	)

#!/usr/bin/env python3
"""Claude worker pool: the pure logic behind ``claude-pool-worker.yml``.

The Claude worker pool (``docs/plans/claude-actions-worker-pool-plan.md``) runs
headless Claude Code CLI jobs in the private runner repo
``shubhodeep1/claude-workers``. Each job draws one account from a pool of
claude.ai accounts, one Actions secret per account named
``CLAUDE_POOL_TOKEN_<NAME>``. The reusable workflow
``.github/workflows/claude-pool-worker.yml`` calls this module for every
decision it makes, so the workflow itself only moves files and secrets around.

Subcommands (all read files or stdin and print to stdout):

  * ``config`` — the normalised ``.github/ai/claude_pool.json`` (defaults for a
    missing file, unreadable JSON, or an invalid value).
  * ``accounts`` — pool account names from a JSON list (or object) of secret
    *names* on stdin, minus ``--exclude``. Never pass secret values: the
    workflow pipes ``toJSON(secrets) | keys`` only.
  * ``normalize`` — a token on stdin with every whitespace character removed
    (a terminal wrap on copy put a line break inside stored tokens, spike S3).
  * ``probe-parse`` — one Haiku probe transcript (stream-json) to
    ``{account, five_hour, seven_day, resets_at, status, error}``.
  * ``choose`` — probe results to the least-used account under the gate, or a
    ``all_gated`` / ``no_accounts`` / ``auth_failed`` / ``crashed`` verdict.
  * ``prompt`` — a queue item's payload to the slash-command prompt and target
    repository of the worker run.
  * ``classify`` — a worker transcript to the ``claude-pool-result.json``
    the dispatcher reads.
  * ``run-name`` — build or parse the ``pool <type> q<issue> a<attempt>``
    run name, the only state the dispatcher keeps.
  * ``redact`` — replace secret values (read from the environment variables
    it is told to use) with ``***`` in files before they are uploaded as
    artifacts. Log masking does not cover artifacts, and the worker's shell
    commands inherit the token environment.
  * ``twin-guard`` — the work job's ``pre-commit`` hook: refuse a commit that
    changes the checkout's own ``.claude/**`` (a merge may bring it in from a
    parent). The CLI's deny rule covers the Edit and Write tools; this covers
    a file any other program wrote, at the point the change would be committed.

The module makes no API calls (``twin-guard`` runs local ``git`` only). ``normalize`` reads a token on stdin and writes
it back only to stdout; ``redact`` reads secret values from the environment
and never prints them. Log lines go to stderr with the stable prefix
``CLAUDE_POOL``.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _load_route():
	spec = importlib.util.spec_from_file_location("claude_issue_route", ROOT / "scripts" / "claude_issue_route.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


claude_issue_route = _load_route()

LOG_PREFIX = "CLAUDE_POOL"
CONFIG_PATH = ROOT / ".github" / "ai" / "claude_pool.json"
CONSUMER_REGISTRY_PATH = ROOT / ".github" / "ai" / "consumer_repos.json"
SELF_REPO = claude_issue_route.DEFAULT_UPSTREAM_REPO

TOKEN_SECRET_PREFIX = "CLAUDE_POOL_TOKEN_"
TOKEN_SECRET_RE = re.compile(r"^CLAUDE_POOL_TOKEN_([A-Z0-9_]+)$")
ACCOUNT_NAME_RE = re.compile(r"^[A-Z0-9_]+$")

ITEM_TYPES: tuple[str, ...] = ("issue", "pr_fix", "stage")
# `smoke` is the runner repo's push-triggered self-test; it is never queued.
RUN_ITEM_TYPES: tuple[str, ...] = ITEM_TYPES + ("smoke",)
OUTCOMES: tuple[str, ...] = (
	"success",
	"auth_failed",
	"usage_limit",
	"all_gated",
	"no_accounts",
	"crashed",
	"timeout",
)
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
# GitHub-hosted jobs stop at 6 hours; the worker's own timeout stays below it.
MAX_TIMEOUT_MINUTES = 350
JOB_TIMEOUT_HEADROOM_MINUTES = 10
JOB_TIMEOUT_CAP_MINUTES = 360

DEFAULT_CONFIG: dict[str, Any] = {
	"dispatch_types": [],
	"runner_repo": "shubhodeep1/claude-workers",
	"worker_workflow": "claude-pool-worker.yml",
	"gate_utilization": 0.9,
	"max_attempts": 3,
	"timeout_minutes": {"issue": 350, "stage": 350, "pr_fix": 120},
	"transcript_retention_days": 14,
	"worker_model": "claude-opus-5-5",
	"worker_effort": "high",
	"probe_model": "claude-haiku-4-5-20251001",
	"cli_version": "latest",
	"handoff_author_login": "",
	"verdict_bot_login": "",
	"retire_pickup": False,
}
SMOKE_TIMEOUT_MINUTES = 20
# The smoke job's checks after the CLI run: the deny-rule run (600 s) and the
# GitHub MCP run (300 s) in claude-pool-worker.yml. The smoke job's limit
# covers both on top of SMOKE_TIMEOUT_MINUTES.
SMOKE_CHECKS_MINUTES = 15

REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKFLOW_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,79}$")
CLI_VERSION_RE = re.compile(r"^(latest|[0-9]+\.[0-9]+\.[0-9]+)$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")

RUN_NAME_RE = re.compile(r"^pool (issue|pr_fix|stage|smoke) q([0-9]{1,10}) a([0-9]{1,3})$")

PAYLOAD_MAX_BYTES = 4096
RESUME_BLOCK_MAX_BYTES = 16384

# A rejected credential (spike S15): exit 1, `result` "Failed to authenticate.
# API Error: 401 OAuth access token is invalid." with `is_error: true`.
AUTH_ERROR_RE = re.compile(
	r"Failed to authenticate|API Error: 401|authentication_error|OAuth (?:access )?token (?:is )?(?:invalid|expired|revoked)",
	re.IGNORECASE,
)
# No real usage-limit rejection has been observed yet (plan Q7); these texts and
# the rate-limit status below are the best current evidence.
USAGE_LIMIT_TEXT_RE = re.compile(
	r"usage limit|limit reached|rate[ _-]?limit|out of extra usage|five[_ -]hour limit|weekly limit",
	re.IGNORECASE,
)

SMOKE_PROMPT = "Reply with exactly the word OK."
_FALLBACK_TEMPLATE = (
	"If .claude/commands/{command}.md is missing from this checkout (the repo has not synced the "
	"@stable .claude/ assets yet), read workflow-templates/.claude/commands/{command}.md from "
	"shubhodeep1/coding-workflows at ref stable with mcp__github__get_file_contents and follow it "
	"with {arguments}."
)


class PoolError(ValueError):
	"""Invalid input to a pool subcommand."""


def log(event: str, **fields: Any) -> None:
	parts = [LOG_PREFIX, event] + [f"{key}={value}" for key, value in fields.items()]
	print(" ".join(parts), file=sys.stderr)


# --- configuration ---------------------------------------------------------


def _valid_int(value: Any, low: int, high: int) -> bool:
	return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def normalize_config(raw: Any) -> tuple[dict[str, Any], list[str]]:
	"""Merge ``raw`` over the defaults; return the config and the invalid keys.

	Unknown keys are ignored. An invalid value falls back to its default and
	is named in the second element, so the caller can log it. ``dispatch_types``
	keeps only known item types, in the order given, without duplicates.
	"""
	config = json.loads(json.dumps(DEFAULT_CONFIG))
	invalid: list[str] = []
	if not isinstance(raw, dict):
		if raw is not None:
			invalid.append("<root>")
		return config, invalid

	def take(key: str, ok: bool) -> None:
		if key not in raw:
			return
		if ok:
			config[key] = raw[key]
		else:
			invalid.append(key)

	if "dispatch_types" in raw:
		value = raw["dispatch_types"]
		if isinstance(value, list):
			kept: list[str] = []
			for item in value:
				if item in ITEM_TYPES and item not in kept:
					kept.append(item)
				else:
					invalid.append(f"dispatch_types[{item!r}]")
			config["dispatch_types"] = kept
		else:
			invalid.append("dispatch_types")
	take("runner_repo", isinstance(raw.get("runner_repo"), str) and bool(REPO_SLUG_RE.match(raw.get("runner_repo", ""))))
	take("worker_workflow", isinstance(raw.get("worker_workflow"), str) and bool(WORKFLOW_FILE_RE.match(raw.get("worker_workflow", ""))))
	gate = raw.get("gate_utilization")
	take(
		"gate_utilization",
		isinstance(gate, (int, float)) and not isinstance(gate, bool) and math.isfinite(gate) and 0 < gate <= 1,
	)
	take("max_attempts", _valid_int(raw.get("max_attempts"), 1, 10))
	if "timeout_minutes" in raw:
		value = raw["timeout_minutes"]
		if isinstance(value, dict):
			for item_type in ITEM_TYPES:
				if item_type not in value:
					continue
				if _valid_int(value[item_type], 1, MAX_TIMEOUT_MINUTES):
					config["timeout_minutes"][item_type] = value[item_type]
				else:
					invalid.append(f"timeout_minutes.{item_type}")
		else:
			invalid.append("timeout_minutes")
	take("transcript_retention_days", _valid_int(raw.get("transcript_retention_days"), 1, 90))
	for key in ("worker_model", "probe_model"):
		take(key, isinstance(raw.get(key), str) and bool(MODEL_RE.match(raw.get(key, ""))))
	take("worker_effort", raw.get("worker_effort") in EFFORTS)
	take("cli_version", isinstance(raw.get("cli_version"), str) and bool(CLI_VERSION_RE.match(raw.get("cli_version", ""))))
	for key in ("handoff_author_login", "verdict_bot_login"):
		value = raw.get(key)
		take(key, isinstance(value, str) and (value == "" or bool(LOGIN_RE.match(value))))
	take("retire_pickup", isinstance(raw.get("retire_pickup"), bool))
	return config, invalid


def load_config(path: Path = CONFIG_PATH) -> tuple[dict[str, Any], list[str]]:
	"""Read and normalise the pool config. A missing or unreadable file is the default (pool off)."""
	try:
		text = path.read_text(encoding="utf-8")
	except OSError:
		return normalize_config(None)
	try:
		raw = json.loads(text)
	except ValueError:
		config, _ = normalize_config(None)
		return config, ["<unreadable>"]
	return normalize_config(raw)


def item_timeout_minutes(config: dict[str, Any], item_type: str) -> int:
	if item_type == "smoke":
		return SMOKE_TIMEOUT_MINUTES
	return int(config["timeout_minutes"].get(item_type, MAX_TIMEOUT_MINUTES))


def job_timeout_minutes(timeout_minutes: int) -> int:
	"""The work job's own limit: the CLI's limit plus set-up headroom, capped at 6 hours."""
	return min(timeout_minutes + JOB_TIMEOUT_HEADROOM_MINUTES, JOB_TIMEOUT_CAP_MINUTES)


# --- accounts and tokens ---------------------------------------------------


def parse_excludes(text: str | None) -> list[str]:
	"""Comma- or space-separated account names, upper-cased; raise PoolError on a bad name."""
	names: list[str] = []
	for part in re.split(r"[,\s]+", (text or "").strip()):
		if not part:
			continue
		name = part.upper()
		if not ACCOUNT_NAME_RE.match(name):
			raise PoolError(f"invalid account name in exclude list: {part[:40]!r}")
		if name not in names:
			names.append(name)
	return names


def list_accounts(secret_names: Any, exclude: list[str] | None = None) -> dict[str, Any]:
	"""Pool accounts from secret names (a list, or an object whose keys are the names).

	Returns ``{"accounts": [...], "excluded": [...]}``: account names sorted,
	those in ``exclude`` moved to ``excluded``. Names that do not match
	``^CLAUDE_POOL_TOKEN_[A-Z0-9_]+$`` are ignored, whatever else is in the list.
	"""
	if isinstance(secret_names, dict):
		keys = list(secret_names.keys())
	elif isinstance(secret_names, list):
		keys = secret_names
	else:
		raise PoolError("secret names must be a JSON list or object")
	found = sorted({m.group(1) for key in keys if isinstance(key, str) for m in [TOKEN_SECRET_RE.match(key)] if m})
	skip = set(exclude or [])
	return {
		"accounts": [name for name in found if name not in skip],
		"excluded": [name for name in found if name in skip],
	}


def normalize_token(raw: str) -> str:
	"""Strip every whitespace character; raise PoolError when nothing is left."""
	token = re.sub(r"\s+", "", raw or "")
	if not token:
		raise PoolError("empty token")
	return token


# --- transcripts -----------------------------------------------------------


def read_events(text: str) -> list[dict[str, Any]]:
	"""Parse a stream-json transcript; lines that are not JSON objects are skipped."""
	events: list[dict[str, Any]] = []
	for line in (text or "").splitlines():
		line = line.strip()
		if not line.startswith("{"):
			continue
		try:
			event = json.loads(line)
		except ValueError:
			continue
		if isinstance(event, dict):
			events.append(event)
	return events


def last_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
	for event in reversed(events):
		if event.get("type") == event_type:
			return event
	return None


def last_rate_limit_info(events: list[dict[str, Any]]) -> dict[str, Any] | None:
	event = last_event(events, "rate_limit_event")
	info = event.get("rate_limit_info") if event else None
	return info if isinstance(info, dict) else None


def _number(value: Any) -> float | None:
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		return None
	if not math.isfinite(value):
		return None
	return float(value)


def _int_or_none(value: Any) -> int | None:
	number = _number(value)
	return int(number) if number is not None else None


def window(info: dict[str, Any] | None, name: str) -> tuple[float | None, int | None]:
	"""(utilization 0..1, resetsAt epoch seconds) of one unified window, or Nones."""
	windows = info.get("unifiedWindows") if isinstance(info, dict) else None
	data = windows.get(name) if isinstance(windows, dict) else None
	if not isinstance(data, dict):
		return None, None
	return _number(data.get("utilization")), _int_or_none(data.get("resetsAt"))


def is_auth_error(text: str) -> bool:
	return bool(AUTH_ERROR_RE.search(text or ""))


def is_usage_limited(info: dict[str, Any] | None, result_text: str = "") -> bool:
	"""True when the last rate-limit info is rejected or at 100%, or the result text says so."""
	if isinstance(info, dict):
		if info.get("status") == "rejected":
			return True
		for name in ("five_hour", "seven_day"):
			utilization, _ = window(info, name)
			if utilization is not None and utilization >= 1.0:
				return True
	return bool(USAGE_LIMIT_TEXT_RE.search(result_text or ""))


def parse_probe(text: str, account: str, exit_code: int = 0) -> dict[str, Any]:
	"""One Haiku probe transcript to a probe record.

	``error`` is ``None`` for a usable reading, ``auth_failed`` for a rejected
	token (spike S15), ``probe_failed`` for anything else that went wrong. A
	failed probe that shows a usage limit keeps ``error`` ``None`` with
	``status`` ``rejected``, so ``choose`` gates it. A probe that succeeds
	without a rate-limit event keeps ``five_hour`` / ``seven_day`` at ``None``
	(unknown utilization).
	"""
	events = read_events(text)
	info = last_rate_limit_info(events)
	result = last_event(events, "result")
	five_hour, five_reset = window(info, "five_hour")
	seven_day, seven_reset = window(info, "seven_day")
	record: dict[str, Any] = {
		"account": account,
		"five_hour": five_hour,
		"seven_day": seven_day,
		"five_hour_resets_at": five_reset,
		"seven_day_resets_at": seven_reset,
		"resets_at": _int_or_none(info.get("resetsAt")) if info else None,
		"status": info.get("status") if info and isinstance(info.get("status"), str) else None,
		"error": None,
	}
	result_text = str(result.get("result", "")) if result else ""
	if result is None or result.get("is_error") is True or exit_code != 0:
		if is_auth_error(result_text):
			record["error"] = "auth_failed"
		elif is_usage_limited(info, result_text):
			# The account answered but is at its limit: a usage reading that
			# `choose` gates, not a broken account.
			record["status"] = "rejected"
		else:
			record["error"] = "probe_failed"
	return record


def _max_utilization(probe: dict[str, Any]) -> float | None:
	values = [probe.get("five_hour"), probe.get("seven_day")]
	if any(value is None for value in values):
		return None
	return max(values)


def _known_peak(probe: dict[str, Any]) -> float | None:
	"""The highest window reading the probe did report, or ``None`` when it reported none."""
	values = [value for value in (probe.get("five_hour"), probe.get("seven_day")) if value is not None]
	return max(values) if values else None


def _usable_at(probe: dict[str, Any], gate: float) -> int | None:
	"""When a gated account drops back under the gate: the latest reset of the windows holding it.

	When none of those windows reported its own reset (a rejected probe with no
	reading over the gate, or a window over the gate without ``resetsAt``), the
	event's top-level ``resetsAt`` stands in for it.
	"""
	resets: list[int] = []
	for name in ("five_hour", "seven_day"):
		utilization = probe.get(name)
		reset = probe.get(f"{name}_resets_at")
		if utilization is not None and utilization >= gate and reset is not None:
			resets.append(reset)
	if not resets and probe.get("resets_at") is not None:
		resets.append(probe["resets_at"])
	return max(resets) if resets else None


def choose_account(probes: list[dict[str, Any]], gate: float) -> dict[str, Any]:
	"""Pick the least-used account under ``gate`` (plan G3, Q4, Q31).

	Rank: the lowest ``max(five_hour, seven_day)`` strictly below ``gate``,
	ties to the alphabetically first name. An account whose probe succeeded but
	reported no utilization, or only one window under the gate, is used only
	when no account has a full reading under the gate; one window at or over
	the gate gates the account even when the other is missing. A probe error
	skips the account. Verdicts when nothing is usable: ``no_accounts`` (no
	probes), ``all_gated`` (at least one account at or over the gate or
	rejected; ``resets_at`` is the earliest time one of them is usable again),
	``auth_failed`` (only token failures), ``crashed``
	(only other probe failures).
	"""
	ordered = sorted(probes, key=lambda probe: str(probe.get("account", "")))
	eligible: list[tuple[float, str]] = []
	unknown: list[str] = []
	gated: list[dict[str, Any]] = []
	auth_failed: list[str] = []
	failed: list[str] = []
	for probe in ordered:
		name = str(probe.get("account", ""))
		error = probe.get("error")
		if error == "auth_failed":
			auth_failed.append(name)
			continue
		if error:
			failed.append(name)
			continue
		peak = _max_utilization(probe)
		known = _known_peak(probe)
		# One window at or over the gate gates the account even when the
		# other window is missing: the known reading already rules it out.
		if probe.get("status") == "rejected" or (known is not None and known >= gate):
			gated.append(probe)
		elif peak is None:
			unknown.append(name)
		else:
			eligible.append((peak, name))
	verdict: dict[str, Any] = {
		"outcome": "selected",
		"account": "",
		"utilization": None,
		"resets_at": None,
		"gated": [probe.get("account") for probe in gated],
		"auth_failed": auth_failed,
		"probe_failed": failed,
	}
	if eligible:
		peak, name = min(eligible)
		verdict.update(account=name, utilization=peak)
		return verdict
	if unknown:
		verdict.update(account=unknown[0])
		return verdict
	if not ordered:
		verdict["outcome"] = "no_accounts"
	elif gated:
		times = [time for time in (_usable_at(probe, gate) for probe in gated) if time is not None]
		verdict.update(outcome="all_gated", resets_at=min(times) if times else None)
	elif auth_failed:
		verdict["outcome"] = "auth_failed"
	else:
		verdict["outcome"] = "crashed"
	return verdict


# --- prompts ---------------------------------------------------------------


def decode_payload(payload_b64: str) -> str:
	"""Strict base64 decode of the queue item's fire text; raise PoolError.

	ASCII whitespace is dropped first, so the line-wrapped output of GNU
	``base64`` (a manual ``gh workflow run -f payload_b64="$(base64 …)"``)
	decodes too; any other non-alphabet character is still rejected.
	"""
	text = re.sub(r"[ \t\r\n\f\v]+", "", payload_b64 or "")
	if not text:
		raise PoolError("empty payload")
	try:
		data = base64.b64decode(text, validate=True)
	except (binascii.Error, ValueError) as exc:
		raise PoolError(f"payload is not base64: {exc}") from exc
	if len(data) > PAYLOAD_MAX_BYTES:
		raise PoolError("payload too large")
	try:
		return data.decode("utf-8")
	except UnicodeDecodeError as exc:
		raise PoolError("payload is not UTF-8") from exc


def allowed_repos(registry_path: Path = CONSUMER_REGISTRY_PATH) -> list[str]:
	return claude_issue_route.load_allowed_repos(registry_path, SELF_REPO)


def _require_registered(repo: str, registry: list[str]) -> None:
	if repo.lower() not in {slug.lower() for slug in registry}:
		raise PoolError(f"repo not registered: {repo}")


def build_prompt(
	item_type: str,
	payload_text: str,
	registry: list[str],
	resume_block: str = "",
) -> dict[str, str]:
	"""The worker's target repository and slash-command prompt for one queue item.

	Payloads are parsed with the same strict rules the pickup applies
	(``claude-issue-dispatch.md`` step 1), the repository must be registered
	(``.github/ai/consumer_repos.json`` or coding-workflows), and each prompt
	keeps the fallback line the pickup sends for a consumer that has not synced
	the command yet. ``stage`` needs ``parse_stage_text`` in
	``claude_issue_route.py`` (plan phase 3) and the resume block read from the
	wait marker; without either it is refused.
	"""
	if item_type == "smoke":
		return {"repo": SELF_REPO, "prompt": SMOKE_PROMPT}
	if item_type == "issue":
		try:
			fields = claude_issue_route.parse_fire_text(payload_text)
		except ValueError as exc:
			raise PoolError(f"invalid issue payload: {exc}") from exc
		_require_registered(fields["repo"], registry)
		url = fields["issue_url"]
		lines = [
			f"/implement-issue-claude {url}",
			_FALLBACK_TEMPLATE.format(command="implement-issue-claude", arguments=f"{url} as $ARGUMENTS"),
		]
		return {"repo": fields["repo"], "prompt": "\n".join(lines)}
	if item_type == "pr_fix":
		try:
			fields = claude_issue_route.parse_pr_fix_text(payload_text)
		except ValueError as exc:
			raise PoolError(f"invalid pr_fix payload: {exc}") from exc
		_require_registered(fields["repo"], registry)
		lines = [
			f"/fix-claude-pr {fields['pr_url']} — kind {fields['kind']} — head {fields['head']} — claim {fields['claim']}",
			_FALLBACK_TEMPLATE.format(command="fix-claude-pr", arguments="the same $ARGUMENTS"),
		]
		return {"repo": fields["repo"], "prompt": "\n".join(lines)}
	if item_type == "stage":
		parse_stage = getattr(claude_issue_route, "parse_stage_text", None)
		if parse_stage is None:
			raise PoolError("stage payloads are not supported by this checkout (claude_issue_route.parse_stage_text is missing)")
		try:
			fields = parse_stage(payload_text)
		except ValueError as exc:
			raise PoolError(f"invalid stage payload: {exc}") from exc
		repo = str(fields.get("repo", ""))
		plan = str(fields.get("plan", ""))
		if not REPO_SLUG_RE.match(repo) or not plan:
			raise PoolError("stage payload lacks repo or plan")
		_require_registered(repo, registry)
		block = (resume_block or "").strip()
		if not block:
			raise PoolError("stage item has no resume block")
		if len(block.encode("utf-8")) > RESUME_BLOCK_MAX_BYTES:
			raise PoolError("resume block too large")
		lines = [
			f"/implement-plan-claude {plan} — resume.",
			block,
			_FALLBACK_TEMPLATE.format(command="implement-plan-claude", arguments="the same $ARGUMENTS"),
		]
		return {"repo": repo, "prompt": "\n".join(lines)}
	raise PoolError(f"unknown item type: {item_type!r}")


# --- run names -------------------------------------------------------------


def build_run_name(item_type: str, queue_issue: int, attempt: int) -> str:
	if item_type not in RUN_ITEM_TYPES:
		raise PoolError(f"unknown item type: {item_type!r}")
	if not _valid_int(queue_issue, 0, 9_999_999_999) or not _valid_int(attempt, 1, 999):
		raise PoolError("queue issue or attempt out of range")
	return f"pool {item_type} q{queue_issue} a{attempt}"


def parse_run_name(name: str) -> dict[str, Any] | None:
	"""``{item_type, queue_issue, attempt}`` for a worker run name, else None."""
	match = RUN_NAME_RE.match((name or "").strip())
	if not match:
		return None
	attempt = int(match.group(3))
	if attempt < 1:
		return None
	return {"item_type": match.group(1), "queue_issue": int(match.group(2)), "attempt": attempt}


# --- classification --------------------------------------------------------


def classify_run(
	transcript_text: str | None,
	exit_info: dict[str, Any] | None,
	selection: dict[str, Any] | None,
	work_result: str = "",
) -> dict[str, Any]:
	"""Classify one worker run (plan "Worker run", step 3).

	``selection`` is the ``choose`` verdict; when it selected no account its
	outcome is the run's. ``exit_info`` is ``{"exit_code": int, "timed_out":
	bool}`` written by the work job (missing when the job never got that far).
	``work_result`` is the work job's ``needs.work.result``: a run whose
	transcript succeeded but whose job failed afterwards (a smoke check, an
	upload) is ``crashed``. Returns ``{outcome, reason, rate_limit_info,
	total_cost_usd, duration_ms}``.
	"""
	base: dict[str, Any] = {
		"outcome": "crashed",
		"reason": "",
		"rate_limit_info": None,
		"total_cost_usd": None,
		"duration_ms": None,
	}
	if not isinstance(selection, dict) or selection.get("outcome") not in ("selected",) + OUTCOMES:
		base["reason"] = "no_selection"
		return base
	if selection["outcome"] != "selected":
		base["outcome"] = selection["outcome"] if selection["outcome"] in OUTCOMES else "crashed"
		base["reason"] = "select"
		return base
	exit_info = exit_info if isinstance(exit_info, dict) else {}
	if exit_info.get("timed_out") is True:
		base["outcome"] = "timeout"
		base["reason"] = "cli_timeout"
	events = read_events(transcript_text or "")
	info = last_rate_limit_info(events)
	result = last_event(events, "result")
	base["rate_limit_info"] = info
	if result:
		base["total_cost_usd"] = _number(result.get("total_cost_usd"))
		base["duration_ms"] = _int_or_none(result.get("duration_ms"))
	if base["outcome"] == "timeout":
		return base
	if work_result == "cancelled":
		base["outcome"] = "timeout"
		base["reason"] = "job_cancelled"
		return base
	if not events:
		base["reason"] = "no_transcript"
		return base
	exit_code = exit_info.get("exit_code")
	result_text = str(result.get("result", "")) if result else ""
	if result and result.get("is_error") is False and result.get("subtype") == "success" and exit_code == 0:
		if work_result and work_result != "success":
			base["reason"] = f"work_job_{work_result}"
			return base
		base["outcome"] = "success"
		return base
	if is_auth_error(result_text):
		base["outcome"] = "auth_failed"
		base["reason"] = "result_auth_error"
	elif is_usage_limited(info, result_text):
		base["outcome"] = "usage_limit"
		base["reason"] = "rate_limit_rejected" if info and info.get("status") == "rejected" else "usage_limit_text"
	elif result is None:
		base["reason"] = "no_result"
	else:
		base["reason"] = f"result_{result.get('subtype') or 'error'}"
	return base


# --- redaction -------------------------------------------------------------

REDACTION = "***"
# Shorter values are not redacted: they would match ordinary text.
REDACT_MIN_LENGTH = 8
# A raw CR/LF line break, or the two- and four-character JSON escapes of one.
_REDACT_LINE_BREAK = r"(?:\r?\n|\\r\\n|\\n)?"


def _base64_forms(value: str) -> set[str]:
	"""The base64 text that encodes ``value`` wherever it sits in a stream.

	Base64 encodes 3 bytes as 4 characters, so the same secret encodes
	differently at each byte offset in a larger input. For each offset 0–2,
	in the standard and URL-safe alphabets, this returns the characters that
	depend on the secret's bytes alone; any encoding containing the secret
	contains one of them. A plain ``base64(secret)`` is offset 0; its final
	character also depends on the padding bits, so the complete standalone
	encoding (with and without ``=`` padding) is returned too, and replacing
	it first leaves no trailing character of the secret behind.
	"""
	raw = value.encode("utf-8")
	forms: set[str] = set()
	for encode in (base64.b64encode, base64.urlsafe_b64encode):
		standalone = encode(raw).decode("ascii")
		forms.update({standalone, standalone.rstrip("=")})
	for offset in range(3):
		data = b"\0" * offset + raw
		first = math.ceil(offset * 8 / 6)
		end = (len(data) * 8) // 6
		for encode in (base64.b64encode, base64.urlsafe_b64encode):
			encoded = encode(data).decode("ascii").rstrip("=")
			form = encoded[first:end]
			if len(form) >= REDACT_MIN_LENGTH:
				forms.add(form)
	return forms


def _wrapped_pattern(value: str) -> re.Pattern[str]:
	"""``value`` with an optional line break allowed between any two characters.

	GNU ``base64`` wraps its output every 76 columns, so ``base64(secret)``
	printed by a worker can carry a newline anywhere inside it: a real one in
	``stderr.txt``, the JSON-escaped ``\\n`` / ``\\r\\n`` in ``transcript.jsonl``.
	"""
	return re.compile(_REDACT_LINE_BREAK.join(re.escape(char) for char in value))


def redact_text(text: str, secrets: list[str]) -> tuple[str, int]:
	"""Replace every occurrence of each secret; return the text and the count.

	Each secret is also matched without whitespace, in base64 at every byte
	offset (standard and URL-safe alphabets, so ``base64(secret)`` printed by
	a worker or embedded in a longer encoded string is caught), and in the
	base64 ``x-access-token:<secret>`` form ``actions/checkout`` writes into
	the checkout's ``.git/config`` (a worker could print that file). Every
	form also matches with line breaks inside it (``_wrapped_pattern``), so a
	wrapped encoding is replaced whole, breaks included.
	"""
	values: set[str] = set()
	for secret in secrets:
		if not secret:
			continue
		for value in (secret, re.sub(r"\s+", "", secret)):
			if len(value) >= REDACT_MIN_LENGTH:
				values.add(value)
				values.add(base64.b64encode(f"x-access-token:{value}".encode("utf-8")).decode("ascii"))
				values.update(_base64_forms(value))
	count = 0
	# Longest first, so a secret that contains another is replaced whole.
	for value in sorted(values, key=len, reverse=True):
		text, hits = _wrapped_pattern(value).subn(REDACTION, text)
		count += hits
	return text, count


# --- twin-first commit guard -----------------------------------------------

# The checkout's own .claude/**, anchored at the repository root, so
# workflow-templates/.claude/** (the twins) never matches.
TWIN_GUARD_PATHSPEC = ":(top).claude/"
# git's empty tree, the base of a repository's first commit.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class TwinGuardError(Exception):
	"""A git command the guard needs failed."""


def _git(args: list[str], cwd: str | None = None, ok_codes: tuple[int, ...] = (0,)) -> str:
	try:
		proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
	except OSError as exc:
		raise TwinGuardError(f"git {args[0]}: {type(exc).__name__}") from exc
	if proc.returncode not in ok_codes:
		raise TwinGuardError(f"git {args[0]} exited {proc.returncode}")
	return proc.stdout


def _protected_entries(lines: str, *, index: bool) -> dict[str, tuple[str, str]]:
	"""``{path: (mode, object id)}`` from ``ls-files -s -z`` or ``ls-tree -r -z`` output."""
	entries: dict[str, tuple[str, str]] = {}
	for record in lines.split("\0"):
		if not record or "\t" not in record:
			continue
		meta, path = record.split("\t", 1)
		fields = meta.split()
		if index:
			# <mode> <object> <stage>; only stage 0 is committed.
			if len(fields) == 3 and fields[2] == "0":
				entries[path] = (fields[0], fields[1])
		elif len(fields) == 3:
			# <mode> <type> <object>
			entries[path] = (fields[0], fields[2])
	return entries


def twin_guard_violations(cwd: str | None = None) -> list[str]:
	"""Paths under the checkout's ``.claude/`` that the commit being made authors.

	Run as a ``pre-commit`` hook (git sets the index and the working
	directory). A plain commit may not change ``.claude/**`` at all: twin-first
	(CLAUDE.md §28.C) routes those edits through ``workflow-templates/.claude/**``
	and the ``[claude-twin-sync]`` copy. A merge commit (``MERGE_HEAD`` present)
	may bring ``.claude/**`` in from a parent: each changed path must match one
	merge head, or, for a two-parent merge, git's own merge of it (``git
	merge-tree --write-tree``, so a clean three-way content merge passes and a
	hand-resolved conflict does not). Raises ``TwinGuardError`` when git fails.
	"""
	top = _git(["rev-parse", "--show-toplevel"], cwd=cwd).strip()
	head = _git(["rev-parse", "--verify", "-q", "HEAD^{commit}"], cwd=top, ok_codes=(0, 1)).strip()
	changed = [
		path
		for path in _git(
			["diff", "--cached", "--name-only", "-z", "--no-renames", head or EMPTY_TREE, "--", TWIN_GUARD_PATHSPEC],
			cwd=top,
		).split("\0")
		if path
	]
	if not changed:
		return []
	merge_head_file = _git(["rev-parse", "--git-path", "MERGE_HEAD"], cwd=top).strip()
	merge_head_path = Path(merge_head_file) if os.path.isabs(merge_head_file) else Path(top) / merge_head_file
	try:
		merge_heads = [line.strip() for line in merge_head_path.read_text(encoding="utf-8").splitlines() if line.strip()]
	except FileNotFoundError:
		merge_heads = []
	except OSError as exc:
		raise TwinGuardError(f"MERGE_HEAD: {type(exc).__name__}") from exc
	if not merge_heads:
		return sorted(changed)
	candidates = list(merge_heads)
	if len(merge_heads) == 1 and head:
		# Exit 1 means conflicts; the first line is still the merged tree.
		merged = _git(["merge-tree", "--write-tree", head, merge_heads[0]], cwd=top, ok_codes=(0, 1)).splitlines()
		if merged and re.fullmatch(r"[0-9a-f]{40,64}", merged[0].strip()):
			candidates.append(merged[0].strip())
	staged = _protected_entries(_git(["ls-files", "-s", "-z", "--", TWIN_GUARD_PATHSPEC], cwd=top), index=True)
	trees = [
		_protected_entries(_git(["ls-tree", "-r", "-z", "--full-tree", tree, "--", TWIN_GUARD_PATHSPEC], cwd=top), index=False)
		for tree in candidates
	]
	return sorted(path for path in changed if not any(tree.get(path) == staged.get(path) for tree in trees))


# --- CLI -------------------------------------------------------------------


def _print(data: Any) -> None:
	print(json.dumps(data, sort_keys=True))


def _read_text(path: str) -> str:
	return Path(path).read_text(encoding="utf-8", errors="replace")


def _read_json_file(path: str) -> Any:
	try:
		return json.loads(_read_text(path))
	except (OSError, ValueError):
		return None


def _cmd_config(args: argparse.Namespace) -> int:
	config, invalid = load_config(Path(args.config))
	for key in invalid:
		log("config_invalid", key=key)
	if args.item_type:
		minutes = item_timeout_minutes(config, args.item_type)
		config["item_timeout_minutes"] = minutes
		# A smoke job runs its checks after the CLI, so its limit covers both.
		checks = SMOKE_CHECKS_MINUTES if args.item_type == "smoke" else 0
		config["job_timeout_minutes"] = job_timeout_minutes(minutes + checks)
	_print(config)
	return 0


def _cmd_accounts(args: argparse.Namespace) -> int:
	try:
		exclude = parse_excludes(args.exclude)
		names = json.loads(sys.stdin.read() or "null")
		data = list_accounts(names, exclude)
	except (PoolError, ValueError) as exc:
		log("accounts_failed", error=str(exc)[:200])
		return 2
	if args.lines:
		for name in data["accounts"]:
			print(name)
	else:
		_print(data)
	return 0


def _cmd_normalize(args: argparse.Namespace) -> int:
	try:
		token = normalize_token(sys.stdin.read())
	except PoolError:
		log("token_empty")
		return 1
	sys.stdout.write(token)
	return 0


def _cmd_probe_parse(args: argparse.Namespace) -> int:
	if not ACCOUNT_NAME_RE.match(args.account or ""):
		log("probe_parse_failed", error="invalid account name")
		return 2
	try:
		text = _read_text(args.transcript)
	except OSError:
		text = ""
	_print(parse_probe(text, args.account, args.exit_code))
	return 0


def _cmd_choose(args: argparse.Namespace) -> int:
	probes: list[dict[str, Any]] = []
	try:
		text = _read_text(args.probes)
	except OSError:
		text = ""
	for line in text.splitlines():
		line = line.strip()
		if not line:
			continue
		try:
			probe = json.loads(line)
		except ValueError:
			continue
		if isinstance(probe, dict) and ACCOUNT_NAME_RE.match(str(probe.get("account", ""))):
			probes.append(probe)
	gate = args.gate
	if gate is None:
		config, _ = load_config(Path(args.config))
		gate = config["gate_utilization"]
	verdict = choose_account(probes, gate)
	verdict["probes"] = sorted(probes, key=lambda probe: str(probe.get("account", "")))
	log("choose", outcome=verdict["outcome"], account=verdict["account"] or "-", probes=len(probes))
	_print(verdict)
	return 0


def _cmd_prompt(args: argparse.Namespace) -> int:
	resume = ""
	if args.resume_block:
		try:
			resume = _read_text(args.resume_block)
		except OSError:
			resume = ""
	try:
		payload = "" if args.item_type == "smoke" else decode_payload(args.payload_b64)
		data = build_prompt(args.item_type, payload, allowed_repos(Path(args.registry)), resume)
	except PoolError as exc:
		log("prompt_failed", item_type=args.item_type, error=str(exc)[:200])
		return 2
	_print(data)
	return 0


def _cmd_classify(args: argparse.Namespace) -> int:
	try:
		transcript = _read_text(args.transcript) if args.transcript else ""
	except OSError:
		transcript = ""
	exit_info = _read_json_file(args.exit_info) if args.exit_info else None
	selection = _read_json_file(args.selection) if args.selection else None
	verdict = classify_run(transcript, exit_info, selection, args.work_result or "")
	selection = selection if isinstance(selection, dict) else {}
	result = {
		"queue_issue": args.queue_issue,
		"attempt": args.attempt,
		"item_type": args.item_type,
		"account": selection.get("account") or "",
		"resets_at": selection.get("resets_at"),
		"probes": selection.get("probes") or [],
		**verdict,
	}
	log("result", outcome=result["outcome"], reason=result["reason"] or "-", account=result["account"] or "-",
		queue_issue=args.queue_issue, attempt=args.attempt)
	_print(result)
	return 0


def _cmd_run_name(args: argparse.Namespace) -> int:
	if args.parse is not None:
		parsed = parse_run_name(args.parse)
		_print(parsed)
		return 0 if parsed else 1
	try:
		print(build_run_name(args.item_type, args.queue_issue, args.attempt))
	except PoolError as exc:
		log("run_name_failed", error=str(exc))
		return 2
	return 0


def _cmd_redact(args: argparse.Namespace) -> int:
	secrets = [os.environ.get(name, "") for name in args.env_var]
	total = 0
	failed = 0
	for path in args.file:
		file_path = Path(path)
		try:
			text = file_path.read_text(encoding="utf-8", errors="replace")
			redacted, count = redact_text(text, secrets)
			if count:
				file_path.write_text(redacted, encoding="utf-8")
				total += count
		except FileNotFoundError:
			# Nothing to upload, so nothing to redact.
			continue
		except OSError as exc:
			# Fail closed: a file that could not be redacted must not be uploaded.
			failed += 1
			log("redact_failed", file=file_path.name, error=type(exc).__name__)
	log("redact", files=len(args.file), replaced=total, failed=failed)
	return 1 if failed else 0


def _cmd_twin_guard(args: argparse.Namespace) -> int:
	try:
		paths = twin_guard_violations()
	except TwinGuardError as exc:
		# Fail closed: a commit the guard could not check is refused.
		log("twin_guard_error", error=str(exc))
		print(f"twin-first guard could not check this commit ({exc}); commit refused.", file=sys.stderr)
		return 1
	if not paths:
		return 0
	log("twin_guard", blocked=len(paths))
	shown = ", ".join(paths[:10]) + (f" (+{len(paths) - 10} more)" if len(paths) > 10 else "")
	print(
		"Twin-first (CLAUDE.md §28.C): this commit changes the checkout's own .claude/** "
		f"({shown}). Make the change in the workflow-templates/.claude/** twin instead, leave "
		".claude/** to the [claude-twin-sync] copy, and unstage the listed paths "
		"(`git restore --staged -- <path>`).",
		file=sys.stderr,
	)
	return 1


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)

	p = sub.add_parser("config", help="print the normalised pool config")
	p.add_argument("--config", default=str(CONFIG_PATH))
	p.add_argument("--item-type", choices=RUN_ITEM_TYPES, default="")
	p.set_defaults(func=_cmd_config)

	p = sub.add_parser("accounts", help="pool account names from secret names on stdin")
	p.add_argument("--exclude", default="")
	p.add_argument("--lines", action="store_true", help="one name per line instead of JSON")
	p.set_defaults(func=_cmd_accounts)

	p = sub.add_parser("normalize", help="strip whitespace from a token on stdin")
	p.set_defaults(func=_cmd_normalize)

	p = sub.add_parser("probe-parse", help="parse one Haiku probe transcript")
	p.add_argument("--account", required=True)
	p.add_argument("--transcript", required=True)
	p.add_argument("--exit-code", type=int, default=0)
	p.set_defaults(func=_cmd_probe_parse)

	p = sub.add_parser("choose", help="pick the least-used account under the gate")
	p.add_argument("--probes", required=True, help="JSON lines, one probe-parse record each")
	p.add_argument("--gate", type=float, default=None)
	p.add_argument("--config", default=str(CONFIG_PATH))
	p.set_defaults(func=_cmd_choose)

	p = sub.add_parser("prompt", help="target repo and slash-command prompt for a queue item")
	p.add_argument("--item-type", required=True, choices=RUN_ITEM_TYPES)
	p.add_argument("--payload-b64", default="")
	p.add_argument("--resume-block", default="")
	p.add_argument("--registry", default=str(CONSUMER_REGISTRY_PATH))
	p.set_defaults(func=_cmd_prompt)

	p = sub.add_parser("classify", help="classify a worker run into claude-pool-result.json")
	p.add_argument("--transcript", default="")
	p.add_argument("--exit-info", default="")
	p.add_argument("--selection", default="")
	p.add_argument("--work-result", default="")
	p.add_argument("--queue-issue", type=int, default=0)
	p.add_argument("--attempt", type=int, default=1)
	p.add_argument("--item-type", default="")
	p.set_defaults(func=_cmd_classify)

	p = sub.add_parser("run-name", help="build or parse a worker run name")
	p.add_argument("--item-type", choices=RUN_ITEM_TYPES)
	p.add_argument("--queue-issue", type=int, default=0)
	p.add_argument("--attempt", type=int, default=1)
	p.add_argument("--parse", default=None)
	p.set_defaults(func=_cmd_run_name)

	p = sub.add_parser("redact", help="replace secret values in files before upload")
	p.add_argument("--env-var", action="append", default=[], help="name of an environment variable holding a secret")
	p.add_argument("file", nargs="*")
	p.set_defaults(func=_cmd_redact)

	p = sub.add_parser("twin-guard", help="pre-commit hook: refuse a commit that authors .claude/** changes")
	p.set_defaults(func=_cmd_twin_guard)

	args = parser.parse_args(argv)
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())

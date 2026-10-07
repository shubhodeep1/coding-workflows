#!/usr/bin/env python3
"""Claude engine: the pure decisions behind ``scripts/ai_engine.sh``.

The Actions pipelines run each model role (clarify, plan, implement, the
judges, the review write roles, …) either on the codex CLI or on the Claude
Code CLI (``claude -p``). ``scripts/ai_engine.sh`` owns the shell side (which
command runs, the fallback to codex); this module owns every decision it
needs, so the shell only moves files around. It makes no network or GitHub
API calls and never prints a token.

Subcommands (inputs are files, arguments or the environment; output goes to
stdout, diagnostics to stderr):

  * ``config [--key K]`` — the normalised ``.github/ai/claude_engine.json``
    (defaults for a missing file, unreadable JSON, or an invalid value), or
    one top-level value of it.
  * ``resolve --role R [--model-hint M] [--effort-hint E]`` — the engine,
    model, effort and tool profile of one role, and where the engine came
    from (``label:<name>``, ``var:AI_ENGINE_<R>``, ``var:AI_ENGINE`` or
    ``default``). Labels come from ``AI_ENGINE_LABELS`` (D2), the variables
    from the environment.
  * ``settings --checkout DIR --out FILE [--profile P] [--allow-workflow-edits]``
    — render ``scripts/claude_settings.json.tmpl`` (the P5 permission policy)
    for one run.
  * ``trust --workdir DIR [--home H]`` — mark DIR trusted in ``~/.claude.json``
    (spike S10: until then the CLI ignores ``permissions.allow``).
  * ``support-file --name config|template|guard-hook`` — the path of a trusted
    support file, or exit 1 when it is missing.
  * ``extract --transcript T --out O`` — write the final ``result`` text of a
    stream-json transcript to ``O`` and print the run's usage line (the
    ``result`` event without its text) for ``cost_audit.py``.
  * ``classify --transcript T [--exit-code N]`` — ``success``,
    ``auth_failed``, ``usage_limit``, ``crashed`` or ``timeout``, with a
    reason (spike evidence S7, S15).
  * ``inspect --transcript T`` — the start-up input tokens and every tool call
    with its denial status, for the smoke run's context and P5 gates.
  * ``probe-parse --account A [--exit-code N] < transcript`` — one Haiku
    probe transcript to a usage record (spike S8).
  * ``choose [--gate G] < probes.json`` — the least-used account under the
    gate (ties to the alphabetically first name), or why none is usable.
  * ``fallbacks --record-file F [--pool-reason R]`` — the job's codex
    fallbacks as a JSON list of ``{role, reason, detail, class}``, read from
    the lines ``ai_engine_fallback`` appends to F. ``class`` is ``capacity``
    (every account over its usage limit: Telegram only) or ``defect`` (a
    setup or code fault: reported to workflow failure heal).

Exit codes: 0 success; 1 the input describes a failure (``extract`` found
no result); 2 invalid arguments or input.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent

ENGINES: tuple[str, ...] = ("codex", "claude")
PROFILES: tuple[str, ...] = ("write", "read")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
OUTCOMES: tuple[str, ...] = ("success", "auth_failed", "usage_limit", "crashed", "timeout")

ROLES: tuple[str, ...] = (
	"CLARIFY",
	"CLARIFY_RESPOND",
	"PLAN",
	"IMPLEMENT",
	"IMPLEMENT_REPAIR",
	"IMPLEMENT_DIAGNOSE",
	"ORCHESTRATE",
	"WAVE_JUDGE",
	"STALL_JUDGE",
	"INTEGRATION_JUDGE",
	"SECURITY_JUDGE",
	"UNBLOCK_JUDGE",
	"REVIEW_EDITOR",
	"REVIEW_CONSOLIDATOR",
	"CONFLICT_RESOLVER",
	"RB_JUDGE",
	"VALIDATE",
	"VALIDATE_SELF_HEAL",
	"VALIDATION_REFRESH",
	"SECURITY_AUDIT",
	"CHECK_TRIAGE",
	"WORKFLOW_HEAL",
	"ACTIVATION_VERIFY",
	"LOG_ANALYSIS",
	"LOG_AUDIT",
	"LOG_SUMMARY",
	"RETRO",
	"MATERIALITY",
	"SUMMARISER",
	"BEHAVIOURAL_SMOKE",
)
UTILITY_ROLES: tuple[str, ...] = ("LOG_SUMMARY", "RETRO", "MATERIALITY", "SUMMARISER", "BEHAVIOURAL_SMOKE")
# Roles whose codex call runs `--sandbox read-only` today keep a read-only
# tool set on Claude; every other role edits its checkout.
READ_ROLES: tuple[str, ...] = ("CLARIFY", "CLARIFY_RESPOND", "SECURITY_JUDGE", "SECURITY_AUDIT", "WORKFLOW_HEAL")

LABEL_CODEX = "ai:codex"
LABEL_ENGINE_CLAUDE = "ai:engine-claude"

MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,79}$")
CLI_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# Only the deployed broker may receive an Actions OIDC token; loopback is for local tests.
URL_RE = re.compile(
	r"^(?:https://claude-pool-broker\.shubhodeep\.workers\.dev|http://(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?)/v1/pool$"
)
AUDIENCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _role_default(role: str) -> dict[str, str]:
	return {
		"engine": "codex",
		"claude_model": "claude-sonnet-5-5" if role in UTILITY_ROLES else "claude-opus-5-5",
		"profile": "read" if role in READ_ROLES else "write",
	}


DEFAULT_CONFIG: dict[str, Any] = {
	"cli_version": "2.1.289",
	"broker_url": "",
	"oidc_audience": "coding-workflows-claude-pool",
	"gate_utilization": 0.9,
	"probe_model": "claude-haiku-4-5-20251001",
	"default_model": "claude-opus-5-5",
	"utility_model": "claude-sonnet-5-5",
	"default_effort": "high",
	"hide_claude_md": False,
	"utility_roles": list(UTILITY_ROLES),
	"role_defaults": {role: _role_default(role) for role in ROLES},
}

# A rejected credential (spike S15): exit 1, `result` "Failed to authenticate.
# API Error: 401 OAuth access token is invalid." with `is_error: true`.
AUTH_ERROR_RE = re.compile(
	r"Failed to authenticate|API Error: 401|authentication_error|OAuth (?:access )?token (?:is )?(?:invalid|expired|revoked)",
	re.IGNORECASE,
)
# No real usage-limit rejection has been observed yet; these texts and the
# rate-limit status (spike S7) are the best current evidence.
USAGE_LIMIT_TEXT_RE = re.compile(
	r"usage limit|limit reached|rate[ _-]?limit|out of extra usage|five[_ -]hour limit|weekly limit",
	re.IGNORECASE,
)
# `timeout` sends SIGTERM (exit 124); its --kill-after sends SIGKILL (137).
TIMEOUT_EXIT_CODES = (124, 137)

# Read-only roles may run these commands and nothing else in Bash. `gh api`
# also passes the gh_api_write_guard.py hook, which denies every write.
READ_PROFILE_ALLOW: tuple[str, ...] = (
	"Read",
	"Grep",
	"Glob",
	"Bash(git log*)",
	"Bash(git show*)",
	"Bash(git diff*)",
	"Bash(git status*)",
	"Bash(git ls-files*)",
	"Bash(git grep*)",
	"Bash(gh issue view*)",
	"Bash(gh pr view*)",
	"Bash(gh pr diff*)",
	"Bash(gh api *)",
)
PROFILE_TOOLS: dict[str, str] = {
	"write": "Read,Grep,Glob,Bash,Edit,Write,WebFetch,WebSearch",
	"read": "Read,Grep,Glob,Bash",
}
PROFILE_MODES: dict[str, str] = {
	"write": "bypassPermissions",
	"read": "dontAsk",
}

TEMPLATE_NAME = "claude_settings.json.tmpl"
GUARD_HOOK_RELATIVE = Path(".claude") / "hooks" / "gh_api_write_guard.py"
CONFIG_RELATIVE = Path(".github") / "ai" / "claude_engine.json"
CHECKOUT_PLACEHOLDER = "__CHECKOUT__"
GUARD_HOOK_PLACEHOLDER = "__GUARD_HOOK__"
WORKFLOW_DENY_MARKER = "/.github/workflows/"


class EngineError(ValueError):
	"""Invalid input to an engine subcommand."""


# --- support files ---------------------------------------------------------


def support_roots() -> list[Path]:
	"""Directories that may hold the trusted coding-workflows files, in order.

	The staged support root of the job, the verified support checkout, and the
	repository this script lives in. The caller's checkout (a consumer repo or a
	PR head) is never searched: its copy is not trusted.
	"""
	roots: list[Path] = []
	support_root = os.environ.get("SUPPORT_ROOT_DIR", "")
	if support_root:
		roots.append(Path(support_root))
	workspace = os.environ.get("GITHUB_WORKSPACE", "")
	if workspace:
		roots.append(Path(workspace) / ".codex-workflow-src")
	roots.append(SCRIPT_DIR.parent)
	unique: list[Path] = []
	for root in roots:
		if root not in unique:
			unique.append(root)
	return unique


def find_support_file(relative: Path) -> Path | None:
	for root in support_roots():
		candidate = root / relative
		if candidate.is_file():
			return candidate
	return None


# --- configuration ---------------------------------------------------------


def _is_number(value: Any) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_config(raw: Any) -> tuple[dict[str, Any], list[str]]:
	"""Merge ``raw`` over the defaults; return the config and the invalid keys.

	Unknown keys are ignored. An invalid value keeps its default and is named in
	the second element. ``role_defaults`` is merged per role and per field, so
	a config that names one role leaves the others at their defaults; an unknown
	role is ignored and reported.
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

	take("cli_version", isinstance(raw.get("cli_version"), str) and bool(CLI_VERSION_RE.match(raw.get("cli_version", ""))))
	take("broker_url", isinstance(raw.get("broker_url"), str) and (raw.get("broker_url") == "" or bool(URL_RE.match(raw.get("broker_url", "")))))
	take("oidc_audience", isinstance(raw.get("oidc_audience"), str) and bool(AUDIENCE_RE.match(raw.get("oidc_audience", ""))))
	gate = raw.get("gate_utilization")
	take("gate_utilization", _is_number(gate) and 0 < gate <= 1)
	for key in ("probe_model", "default_model", "utility_model"):
		take(key, isinstance(raw.get(key), str) and bool(MODEL_RE.match(raw.get(key, ""))))
	take("default_effort", raw.get("default_effort") in EFFORTS)
	take("hide_claude_md", isinstance(raw.get("hide_claude_md"), bool))
	if "utility_roles" in raw:
		value = raw["utility_roles"]
		if isinstance(value, list) and all(item in ROLES for item in value):
			config["utility_roles"] = list(dict.fromkeys(value))
		else:
			invalid.append("utility_roles")
	if "role_defaults" in raw:
		value = raw["role_defaults"]
		if not isinstance(value, dict):
			invalid.append("role_defaults")
		else:
			for role, fields in value.items():
				if role not in ROLES or not isinstance(fields, dict):
					invalid.append(f"role_defaults.{role}")
					continue
				target = config["role_defaults"][role]
				if "engine" in fields:
					if fields["engine"] in ENGINES:
						target["engine"] = fields["engine"]
					else:
						invalid.append(f"role_defaults.{role}.engine")
				if "claude_model" in fields:
					model = fields["claude_model"]
					if isinstance(model, str) and MODEL_RE.match(model) and model.startswith("claude-"):
						target["claude_model"] = model
					else:
						invalid.append(f"role_defaults.{role}.claude_model")
				if "profile" in fields:
					if fields["profile"] in PROFILES:
						target["profile"] = fields["profile"]
					else:
						invalid.append(f"role_defaults.{role}.profile")
	return config, invalid


def load_config(path: Path | None = None) -> tuple[dict[str, Any], list[str]]:
	"""Read and normalise the engine config. A missing file is the default (every role on codex)."""
	if path is None:
		path = find_support_file(CONFIG_RELATIVE)
	if path is None:
		return normalize_config(None)
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


# --- resolution (D1–D3) -----------------------------------------------------


def parse_labels(text: str | None) -> list[str]:
	"""Label names from a comma-, space- or newline-separated list, or a JSON list."""
	text = (text or "").strip()
	if not text:
		return []
	if text.startswith("["):
		try:
			value = json.loads(text)
		except ValueError:
			value = None
		if isinstance(value, list):
			names: list[str] = []
			for item in value:
				name = item.get("name") if isinstance(item, dict) else item
				if isinstance(name, str) and name.strip():
					names.append(name.strip())
			return names
	return [part for part in re.split(r"[,\s]+", text) if part]


def work_item_labels(env: dict[str, str]) -> list[str]:
	"""The labels of the run's work item (plan Phase 6).

	``AI_ENGINE_LABELS`` when it is set, even empty (a call site that already
	holds the issue's or PR's labels passes them). Otherwise the labels of
	``issue`` or ``pull_request`` in the job's own event payload
	(``GITHUB_EVENT_PATH``), which every Actions job carries: no API call. A
	missing or unreadable payload means no labels.
	"""
	if "AI_ENGINE_LABELS" in env:
		return parse_labels(env.get("AI_ENGINE_LABELS", ""))
	event_path = env.get("GITHUB_EVENT_PATH", "")
	if not event_path:
		return []
	try:
		event = json.loads(Path(event_path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return []
	if not isinstance(event, dict):
		return []
	names: list[str] = []
	for key in ("issue", "pull_request"):
		item = event.get(key)
		if isinstance(item, dict) and isinstance(item.get("labels"), list):
			names += [label.get("name") for label in item["labels"] if isinstance(label, dict) and isinstance(label.get("name"), str)]
	return names


def normalize_effort(value: str | None, default: str) -> str:
	"""D3: ``none|minimal`` → ``low``; ``low…max`` unchanged; anything else → ``default``."""
	effort = (value or "").strip().lower()
	if effort in ("none", "minimal"):
		return "low"
	if effort in EFFORTS:
		return effort
	return default


def resolve_role(
	role: str,
	config: dict[str, Any],
	env: dict[str, str],
	model_hint: str = "",
	effort_hint: str = "",
) -> dict[str, Any]:
	"""The engine, model, effort and profile of one role.

	Engine order: the work-item labels (``work_item_labels``; ``ai:codex``
	beats ``ai:engine-claude``, D2), ``AI_ENGINE_<ROLE>``, ``AI_ENGINE``, the
	role's code default. An
	invalid variable value is skipped and reported in ``warnings``.

	Model (D3): ``model_hint`` (the role's existing model variable) when it
	starts with ``claude-``, else the role's Claude default. Effort (D3): the
	role's existing reasoning value mapped by ``normalize_effort``. The
	``ai:engine-claude`` label forces the default model at ``high``.
	"""
	if role not in ROLES:
		raise EngineError(f"unknown role: {role!r}")
	defaults = config["role_defaults"][role]
	warnings: list[str] = []
	labels = {label.lower() for label in work_item_labels(env)}
	engine = ""
	source = ""
	if LABEL_CODEX in labels:
		engine, source = "codex", f"label:{LABEL_CODEX}"
	elif LABEL_ENGINE_CLAUDE in labels:
		engine, source = "claude", f"label:{LABEL_ENGINE_CLAUDE}"
	if not engine:
		for name in (f"AI_ENGINE_{role}", "AI_ENGINE"):
			raw = env.get(name, "")
			value = raw.strip().lower()
			if not value:
				continue
			if value in ENGINES:
				engine, source = value, f"var:{name}"
				break
			warnings.append(f"invalid {name}={raw[:40]!r} ignored")
	if not engine:
		engine, source = defaults["engine"], "default"
	if source == f"label:{LABEL_ENGINE_CLAUDE}":
		model = config["default_model"]
		effort = "high"
	else:
		hint = (model_hint or "").strip()
		model = hint if hint.startswith("claude-") and MODEL_RE.match(hint) else defaults["claude_model"]
		effort = normalize_effort(effort_hint, config["default_effort"])
	return {
		"role": role,
		"engine": engine,
		"model": model,
		"effort": effort,
		"profile": defaults["profile"],
		"source": source,
		"warnings": warnings,
	}


# --- settings (P5) -----------------------------------------------------------


def render_settings(
	template_text: str,
	checkout: str,
	guard_hook: str,
	profile: str = "write",
	allow_workflow_edits: bool = False,
) -> dict[str, Any]:
	"""Render the P5 permission policy for one run.

	``checkout`` and ``guard_hook`` must be absolute paths. The template's
	``__CHECKOUT__`` and ``__GUARD_HOOK__`` placeholders are replaced inside
	string values only. The two ``.github/workflows`` deny rules are dropped
	when ``allow_workflow_edits``; the ``.claude/**`` rules never are. The
	``read`` profile gets its allow list; ``write`` gets none (it runs in
	``bypassPermissions``, where deny rules still apply).
	"""
	if profile not in PROFILES:
		raise EngineError(f"unknown profile: {profile!r}")
	for name, value in (("checkout", checkout), ("guard hook", guard_hook)):
		if not value.startswith("/") or any(char in value for char in "\n\r\"'$`\\"):
			raise EngineError(f"{name} must be an absolute path without quotes, '$', backticks or backslashes")
	checkout = checkout.rstrip("/") or "/"
	try:
		settings = json.loads(template_text)
	except ValueError as exc:
		raise EngineError(f"settings template is not JSON: {exc}") from exc
	if not isinstance(settings, dict):
		raise EngineError("settings template must be a JSON object")

	def substitute(value: Any) -> Any:
		if isinstance(value, str):
			# `//` anchors a permission path at the filesystem root.
			return value.replace(CHECKOUT_PLACEHOLDER, checkout.lstrip("/")).replace(GUARD_HOOK_PLACEHOLDER, guard_hook)
		if isinstance(value, list):
			return [substitute(item) for item in value]
		if isinstance(value, dict):
			return {key: substitute(item) for key, item in value.items()}
		return value

	settings = substitute(settings)
	permissions = settings.setdefault("permissions", {})
	deny = [rule for rule in permissions.get("deny", []) if isinstance(rule, str)]
	if allow_workflow_edits:
		deny = [rule for rule in deny if WORKFLOW_DENY_MARKER not in rule]
	permissions["deny"] = deny
	permissions["allow"] = list(READ_PROFILE_ALLOW) if profile == "read" else []
	env = settings.get("env")
	if isinstance(env, dict):
		for key in list(env):
			if "TOKEN" in key.upper() or "KEY" in key.upper() or "SECRET" in key.upper():
				raise EngineError(f"settings env must not carry credentials: {key}")
	return settings


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
	if not _is_number(value):
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


def extract_result(text: str) -> tuple[str | None, dict[str, Any] | None]:
	"""The final ``result`` text and the usage line of a transcript.

	The usage line is the last ``result`` event without its ``result`` text:
	``type``, ``subtype``, ``is_error``, ``duration_ms``, ``num_turns``,
	``total_cost_usd``, ``usage`` and ``modelUsage`` when present. Returns
	``(None, None)`` when the transcript has no ``result`` event.
	"""
	result = last_event(read_events(text), "result")
	if result is None:
		return None, None
	keys = ("type", "subtype", "is_error", "duration_ms", "num_turns", "total_cost_usd", "usage", "modelUsage")
	usage = {key: result[key] for key in keys if key in result}
	value = result.get("result")
	return (value if isinstance(value, str) else ""), usage


def classify(text: str, exit_code: int | None = None) -> dict[str, Any]:
	"""Classify one ``claude -p`` run.

	``success``: a ``result`` event with ``subtype`` ``success``, ``is_error``
	false, and exit code 0 (or unknown). ``timeout``: exit code 124 or 137
	(``timeout`` and its kill). ``auth_failed``: the result text of a rejected
	credential (S15). ``usage_limit``: a ``rejected`` or full rate-limit window
	(S7), or a usage-limit result text. Anything else is ``crashed``.
	Returns ``{outcome, reason, total_cost_usd, duration_ms}``.
	"""
	events = read_events(text)
	info = last_rate_limit_info(events)
	result = last_event(events, "result")
	verdict: dict[str, Any] = {
		"outcome": "crashed",
		"reason": "",
		"total_cost_usd": _number(result.get("total_cost_usd")) if result else None,
		"duration_ms": _int_or_none(result.get("duration_ms")) if result else None,
	}
	if exit_code in TIMEOUT_EXIT_CODES:
		verdict.update(outcome="timeout", reason=f"exit_{exit_code}")
		return verdict
	result_text = str(result.get("result", "")) if result else ""
	if result and result.get("is_error") is False and result.get("subtype") == "success" and exit_code in (0, None):
		verdict.update(outcome="success", reason="result_success")
		return verdict
	if is_auth_error(result_text):
		verdict.update(outcome="auth_failed", reason="result_auth_error")
	elif is_usage_limited(info, result_text):
		reason = "rate_limit_rejected" if info and info.get("status") == "rejected" else "usage_limit"
		verdict.update(outcome="usage_limit", reason=reason)
	elif not events:
		verdict["reason"] = "no_transcript"
	elif result is None:
		verdict["reason"] = "no_result"
	else:
		verdict["reason"] = f"result_{result.get('subtype') or 'error'}"
	return verdict


def inspect_transcript(text: str) -> dict[str, Any]:
	"""Facts the smoke run asserts on (context gate, P5 denials).

	``startup_input_tokens``: the prompt size of the first model call (input,
	cache-creation and cache-read tokens of the first ``assistant`` event),
	i.e. what the run loaded before doing anything. ``tool_calls``: every tool
	use with its tool name, the Bash ``command`` or the ``file_path`` it named,
	and whether its result was an error (a denial is one), with the first 200
	characters of that result.
	"""
	events = read_events(text)
	startup: int | None = None
	calls: dict[str, dict[str, Any]] = {}
	order: list[str] = []
	for event in events:
		message = event.get("message")
		if not isinstance(message, dict):
			continue
		content = message.get("content")
		if event.get("type") == "assistant":
			usage = message.get("usage")
			if startup is None and isinstance(usage, dict):
				startup = sum(
					_int_or_none(usage.get(key)) or 0
					for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
				)
			for block in content if isinstance(content, list) else []:
				if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
					tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
					target = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("notebook_path") or ""
					calls[block["id"]] = {"tool": block.get("name", ""), "target": str(target)[:500], "is_error": None, "result": ""}
					order.append(block["id"])
		elif event.get("type") == "user":
			for block in content if isinstance(content, list) else []:
				if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
					result = block.get("content")
					if isinstance(result, list):
						result = " ".join(str(part.get("text", "")) for part in result if isinstance(part, dict))
					call = calls[block["tool_use_id"]]
					call["is_error"] = block.get("is_error") is True
					call["result"] = str(result or "")[:200]
	return {"startup_input_tokens": startup, "tool_calls": [calls[call_id] for call_id in order]}


# --- probes and account choice ---------------------------------------------


def parse_probe(text: str, account: str, exit_code: int = 0) -> dict[str, Any]:
	"""One Haiku probe transcript to a probe record.

	``error`` is ``None`` for a usable reading, ``auth_failed`` for a rejected
	token (S15), ``probe_failed`` for anything else that went wrong. A failed
	probe that shows a usage limit keeps ``error`` ``None`` with ``status``
	``rejected``, so ``choose`` gates it. A probe that succeeds without a
	rate-limit event keeps ``five_hour`` / ``seven_day`` at ``None``.
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
	values = [value for value in (probe.get("five_hour"), probe.get("seven_day")) if value is not None]
	return max(values) if values else None


def _usable_at(probe: dict[str, Any], gate: float) -> int | None:
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
	"""Order the accounts for one job: the least used under ``gate`` first.

	Rank: the lowest ``max(five_hour, seven_day)`` strictly below ``gate``,
	ties to the alphabetically first name; then accounts whose probe succeeded
	without a full reading. A window at or over the gate, or a ``rejected``
	status, gates the account; a probe error skips it. ``order`` lists every
	usable account in that rank, so ``claude_run`` can move to the next one on
	``usage_limit`` or ``auth_failed``. Verdicts when nothing is usable:
	``no_accounts``, ``all_gated`` (``resets_at`` is the earliest time one is
	usable again), ``auth_failed`` (only token failures), ``crashed``.
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
		if probe.get("status") == "rejected" or (known is not None and known >= gate):
			gated.append(probe)
		elif peak is None:
			unknown.append(name)
		else:
			eligible.append((peak, name))
	order = [name for _, name in sorted(eligible)] + unknown
	verdict: dict[str, Any] = {
		"outcome": "selected",
		"account": order[0] if order else "",
		"order": order,
		"utilization": min(eligible)[0] if eligible else None,
		"resets_at": None,
		"gated": [probe.get("account") for probe in gated],
		"auth_failed": auth_failed,
		"probe_failed": failed,
	}
	if order:
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


# --- fallback classification ---------------------------------------------------

# One line per `ai_engine_fallback` call: `role=<ROLE> reason=<reason> class=<class>`
# (class may be empty). Written by the job itself, but parsed strictly anyway.
FALLBACK_RECORD_RE = re.compile(r"^role=(?P<role>[A-Z][A-Z_]{0,39}) reason=(?P<reason>[a-z][a-z0-9_]{0,59})(?: class=(?P<cls>[a-z]*))?$")
FALLBACK_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
FALLBACK_CLASSES: tuple[str, ...] = ("capacity", "defect")
# Pool-token verdicts that mean "every account is over its usage limit": the
# only no_credential cause that is not a setup or code fault (answer Q1 A).
CAPACITY_POOL_REASONS: tuple[str, ...] = ("all_gated",)
FALLBACK_RECORD_LIMIT = 10


def collect_fallbacks(record_text: str, pool_reason: str = "") -> list[dict[str, str]]:
	"""Classify the codex fallbacks one job recorded.

	``no_credential`` (the pool step left no usable account) takes its class
	and ``detail`` from the pool step's ``reason`` output: ``all_gated`` is
	capacity, anything else (``cli_missing``, ``broker_refused_…``, an empty
	reason when the step never ran) is a defect. ``all_accounts_failed`` keeps
	the class ``claude_run`` recorded: capacity only when every account hit
	its usage limit. Every other reason is a defect. Entries are deduplicated
	on (role, reason, detail) in first-seen order, at most
	``FALLBACK_RECORD_LIMIT``.
	"""
	pool = pool_reason.strip()
	pool = pool if FALLBACK_TOKEN_RE.match(pool) else ""
	seen: set[tuple[str, str, str]] = set()
	entries: list[dict[str, str]] = []
	for raw in record_text.splitlines():
		match = FALLBACK_RECORD_RE.match(raw.strip())
		if not match or match.group("role") not in ROLES:
			continue
		role, reason = match.group("role"), match.group("reason")
		detail = ""
		if reason == "no_credential":
			detail = pool or "pool_unresolved"
			cls = "capacity" if pool in CAPACITY_POOL_REASONS else "defect"
		elif reason == "all_accounts_failed":
			cls = "capacity" if match.group("cls") == "capacity" else "defect"
		else:
			cls = "defect"
		key = (role, reason, detail)
		if key in seen:
			continue
		seen.add(key)
		entries.append({"role": role, "reason": reason, "detail": detail, "class": cls})
		if len(entries) >= FALLBACK_RECORD_LIMIT:
			break
	return entries


# --- command line ------------------------------------------------------------


def _read_text(path: str) -> str:
	try:
		return Path(path).read_text(encoding="utf-8", errors="replace")
	except FileNotFoundError:
		return ""


def _print_json(value: Any) -> None:
	print(json.dumps(value, sort_keys=True))


def cmd_config(args: argparse.Namespace) -> int:
	config, invalid = load_config(Path(args.config) if args.config else None)
	for key in invalid:
		print(f"claude_engine.py: invalid config value ignored: {key}", file=sys.stderr)
	if args.key:
		if args.key not in config:
			raise EngineError(f"unknown config key: {args.key}")
		value = config[args.key]
		print(value if isinstance(value, str) else json.dumps(value))
		return 0
	_print_json(config)
	return 0


def cmd_resolve(args: argparse.Namespace) -> int:
	config, invalid = load_config(Path(args.config) if args.config else None)
	for key in invalid:
		print(f"claude_engine.py: invalid config value ignored: {key}", file=sys.stderr)
	resolved = resolve_role(args.role, config, dict(os.environ), args.model_hint, args.effort_hint)
	for warning in resolved["warnings"]:
		print(f"::warning::AI engine: {warning}", file=sys.stderr)
	if args.field:
		print(resolved[args.field])
	else:
		_print_json(resolved)
	return 0


def cmd_settings(args: argparse.Namespace) -> int:
	template = Path(args.template) if args.template else SCRIPT_DIR / TEMPLATE_NAME
	if args.guard_hook:
		guard_hook = args.guard_hook
	else:
		found = find_support_file(GUARD_HOOK_RELATIVE)
		if found is None:
			raise EngineError("gh_api_write_guard.py not found in the support checkout")
		guard_hook = str(found.resolve())
	try:
		template_text = template.read_text(encoding="utf-8")
	except OSError as exc:
		raise EngineError(f"settings template unreadable: {exc}") from exc
	settings = render_settings(
		template_text,
		str(Path(args.checkout).resolve()),
		guard_hook,
		profile=args.profile,
		allow_workflow_edits=args.allow_workflow_edits,
	)
	out = Path(args.out)
	out.parent.mkdir(parents=True, exist_ok=True)
	out.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
	os.chmod(out, 0o600)
	return 0


def trust_workdir(home: Path, workdir: str) -> None:
	"""Set ``projects[<workdir>].hasTrustDialogAccepted`` in ``<home>/.claude.json``.

	Other keys are kept. An unreadable or non-object file is replaced, because
	the CLI would not start from it either.
	"""
	path = home / ".claude.json"
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		data = {}
	if not isinstance(data, dict):
		data = {}
	projects = data.get("projects")
	if not isinstance(projects, dict):
		projects = {}
		data["projects"] = projects
	entry = projects.get(workdir)
	if not isinstance(entry, dict):
		entry = {}
		projects[workdir] = entry
	entry["hasTrustDialogAccepted"] = True
	tmp = path.with_name(f".claude.json.tmp.{os.getpid()}")
	tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
	os.chmod(tmp, 0o600)
	os.replace(tmp, path)


def cmd_trust(args: argparse.Namespace) -> int:
	workdir = str(Path(args.workdir).resolve())
	home = Path(args.home) if args.home else Path.home()
	trust_workdir(home, workdir)
	return 0


SUPPORT_FILES: dict[str, Path] = {
	"config": CONFIG_RELATIVE,
	"template": Path("scripts") / TEMPLATE_NAME,
	"guard-hook": GUARD_HOOK_RELATIVE,
}


def cmd_support_file(args: argparse.Namespace) -> int:
	found = find_support_file(SUPPORT_FILES[args.name])
	if found is None:
		print(f"claude_engine.py: support file not found: {args.name}", file=sys.stderr)
		return 1
	print(found.resolve())
	return 0


def cmd_extract(args: argparse.Namespace) -> int:
	result, usage = extract_result(_read_text(args.transcript))
	if result is None:
		print("claude_engine.py: transcript has no result event", file=sys.stderr)
		return 1
	Path(args.out).write_text(result, encoding="utf-8")
	_print_json(usage)
	return 0


def cmd_classify(args: argparse.Namespace) -> int:
	_print_json(classify(_read_text(args.transcript), args.exit_code))
	return 0


def cmd_inspect(args: argparse.Namespace) -> int:
	_print_json(inspect_transcript(_read_text(args.transcript)))
	return 0


def cmd_probe_parse(args: argparse.Namespace) -> int:
	_print_json(parse_probe(sys.stdin.read(), args.account, args.exit_code))
	return 0


def cmd_choose(args: argparse.Namespace) -> int:
	try:
		probes = json.loads(sys.stdin.read() or "[]")
	except ValueError as exc:
		raise EngineError(f"probes are not JSON: {exc}") from exc
	if not isinstance(probes, list) or not all(isinstance(probe, dict) for probe in probes):
		raise EngineError("probes must be a JSON list of objects")
	gate = args.gate
	if gate is None:
		config, _ = load_config(Path(args.config) if args.config else None)
		gate = config["gate_utilization"]
	if not (0 < gate <= 1):
		raise EngineError("gate must be in (0, 1]")
	_print_json(choose_account(probes, gate))
	return 0


def cmd_fallbacks(args: argparse.Namespace) -> int:
	print(json.dumps(collect_fallbacks(_read_text(args.record_file), args.pool_reason), separators=(",", ":"), sort_keys=True))
	return 0


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)

	p = sub.add_parser("config")
	p.add_argument("--config", default="")
	p.add_argument("--key", default="")
	p.set_defaults(func=cmd_config)

	p = sub.add_parser("resolve")
	p.add_argument("--role", required=True)
	p.add_argument("--model-hint", default="")
	p.add_argument("--effort-hint", default="")
	p.add_argument("--field", choices=("engine", "model", "effort", "profile", "source"), default="")
	p.add_argument("--config", default="")
	p.set_defaults(func=cmd_resolve)

	p = sub.add_parser("settings")
	p.add_argument("--checkout", required=True)
	p.add_argument("--out", required=True)
	p.add_argument("--profile", choices=PROFILES, default="write")
	p.add_argument("--allow-workflow-edits", action="store_true")
	p.add_argument("--template", default="")
	p.add_argument("--guard-hook", default="")
	p.set_defaults(func=cmd_settings)

	p = sub.add_parser("trust")
	p.add_argument("--workdir", required=True)
	p.add_argument("--home", default="")
	p.set_defaults(func=cmd_trust)

	p = sub.add_parser("support-file")
	p.add_argument("--name", required=True, choices=sorted(SUPPORT_FILES))
	p.set_defaults(func=cmd_support_file)

	p = sub.add_parser("extract")
	p.add_argument("--transcript", required=True)
	p.add_argument("--out", required=True)
	p.set_defaults(func=cmd_extract)

	p = sub.add_parser("classify")
	p.add_argument("--transcript", required=True)
	p.add_argument("--exit-code", type=int, default=None)
	p.set_defaults(func=cmd_classify)

	p = sub.add_parser("inspect")
	p.add_argument("--transcript", required=True)
	p.set_defaults(func=cmd_inspect)

	p = sub.add_parser("probe-parse")
	p.add_argument("--account", required=True)
	p.add_argument("--exit-code", type=int, default=0)
	p.set_defaults(func=cmd_probe_parse)

	p = sub.add_parser("choose")
	p.add_argument("--gate", type=float, default=None)
	p.add_argument("--config", default="")
	p.set_defaults(func=cmd_choose)

	p = sub.add_parser("fallbacks")
	p.add_argument("--record-file", required=True)
	p.add_argument("--pool-reason", default="")
	p.set_defaults(func=cmd_fallbacks)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		return args.func(args)
	except EngineError as exc:
		print(f"claude_engine.py: {exc}", file=sys.stderr)
		return 2


if __name__ == "__main__":
	sys.exit(main())

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
  * ``guard-read-bash`` — reject unsafe read-profile Bash calls from a
    Claude Code PreToolUse JSON payload on stdin (deny JSON or no output).
  * ``support-lock --manifest FILE --workdir DIR`` / ``support-verify`` /
    ``support-unlock`` — lock and check trusted support during read runs.
  * ``read-guard`` — check a read-profile Bash PreToolUse payload from stdin.
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

Exit codes: 0 success; 1 the input describes a failure (``extract`` found
no result); 2 invalid arguments or input.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import re
import shlex
import stat
import subprocess
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
# tool set on Claude; CHECK_TRIAGE also handles untrusted PR content without editing.
READ_ROLES: tuple[str, ...] = ("CLARIFY", "CLARIFY_RESPOND", "SECURITY_JUDGE", "SECURITY_AUDIT", "WORKFLOW_HEAL", "CHECK_TRIAGE")

LABEL_CODEX = "ai:codex"
LABEL_ENGINE_CLAUDE = "ai:engine-claude"
# Claude-fixer mode (plan Phase 5c, Q19/Q35): CLAUDE_FIXER_ENABLED=false keeps
# these review write roles off Claude, ahead of the labels and AI_ENGINE.
CLAUDE_FIXER_ROLES: tuple[str, ...] = ("REVIEW_EDITOR", "REVIEW_CONSOLIDATOR", "CONFLICT_RESOLVER", "RB_JUDGE")
CLAUDE_FIXER_SWITCH = "CLAUDE_FIXER_ENABLED"

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

# These prefix permissions are only an upper bound: guard-read-bash checks
# exact subcommands and options before Bash can run them. If `gh api` is
# allowed in a future policy, gh_api_write_guard.py must also approve it.
READ_PROFILE_ALLOW: tuple[str, ...] = (
	"Read",
	"Grep",
	"Glob",
	"Bash(git log*)",
	"Bash(git show*)",
	"Bash(git diff*)",
	"Bash(git status*)",
	"Bash(git ls-files*)",
	"Bash(gh issue view*)",
	"Bash(gh pr view*)",
	"Bash(gh pr diff*)",
)
READ_GIT_SUBCOMMANDS = ("log", "show", "diff", "status", "ls-files", "grep")
READ_GH_SUBCOMMANDS = (("issue", "view"), ("pr", "view"), ("pr", "diff"), ("api",))
READ_GIT_DANGEROUS_LONG = ("output", "open-files-in-pager", "ext-diff", "textconv")
READ_GUARD_SUBCOMMAND = "guard-read-bash"
# Defence in depth for #6217: these wildcard rules are not a substitute for
# the read-guard hook, which checks abbreviated and clustered git options.
READ_PROFILE_DENY: tuple[str, ...] = (
	# read-profile-can-access-credential-files: defence in depth behind the isolated filesystem.
	"Read(//proc/**)", "Grep(//proc/**)", "Glob(//proc/**)",
	"Read(**/.git/config)", "Grep(**/.git/config)", "Glob(**/.git/config)",
	"Read(**/.git-credentials)", "Grep(**/.git-credentials)",
	"Read(**/.netrc)", "Grep(**/.netrc)",
	"Read(~/.config/gh/**)", "Grep(~/.config/gh/**)", "Glob(~/.config/gh/**)",
	"Read(~/.claude/.credentials.json)", "Grep(~/.claude/.credentials.json)",
	"Read(**/claude-pool/**)", "Grep(**/claude-pool/**)", "Glob(**/claude-pool/**)",
	"Bash(git * --no-index*)",
	# #6217: git --output can overwrite support and Actions command files.
	"Bash(git * --ou*)",
	# #6217: diff drivers can execute attacker-controlled commands.
	"Bash(git * --ext-diff*)",
	# #6217: textconv drivers can execute attacker-controlled commands.
	"Bash(git * --textconv*)",
	# #6217: git grep -O runs the chosen pager.
	"Bash(git grep*-O*)",
	# #6217: git grep --open-files-in-pager also runs the chosen pager.
	"Bash(git grep* --op*)",
)
READ_GUARD_SAFE_EXACT = frozenset(("--text",))
_READ_GUARD_GIT_OPTIONS = ("--output", "--open-files-in-pager", "--ext-diff", "--textconv", "--no-index")
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
READ_SANDBOX_DOCKERFILE = """FROM node:22.16.0-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 git ca-certificates && rm -rf /var/lib/apt/lists/*
ARG CLAUDE_CLI_VERSION
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}
"""


class EngineError(ValueError):
	"""Invalid input to an engine subcommand."""


def read_bash_denial(command: str) -> str | None:
	"""Reject shell expansion and write-capable options before the Bash allow list.

	This is deliberately narrower than Bash: no compound commands, expansion,
	or global git options. Adjacent quoted fragments are joined like shell words.
	"""
	denial = "Read-only Bash permits only git log/show/diff/status/ls-files/grep or gh issue view/pr view/pr diff/api without shell expansion or output/pager options (use -a instead of --text)."
	if not isinstance(command, str) or not command.strip() or any(c in command for c in "\x00\r\n"):
		return denial
	words: list[str] = []
	word = ""
	started = False
	quote = ""
	index = 0
	while index < len(command):
		char = command[index]
		if quote == "'":
			if char == "'":
				quote = ""
			else:
				word += char
		elif quote == '"':
			if char == '"':
				quote = ""
			elif char in "$`":
				return denial
			elif char == "\\":
				index += 1
				if index >= len(command) or command[index] not in '$`"\\':
					return denial
				word += command[index]
			else:
				word += char
		elif char in "'\"":
			quote = char
			started = True
		elif char == "\\":
			index += 1
			if index >= len(command):
				return denial
			word += command[index]
			started = True
		elif char.isspace():
			if started:
				words.append(word)
				word, started = "", False
		elif char in ";&|<>()$`{*?[":
			return denial
		else:
			word += char
			started = True
		index += 1
	if quote:
		return denial
	if started:
		words.append(word)
	if len(words) < 2:
		return denial
	if words[0] == "git" and words[1] in READ_GIT_SUBCOMMANDS:
		for option in words[2:]:
			if option == "--":
				break
			if option.startswith("--"):
				name = option[2:].split("=", 1)[0]
				if not name or (not name.startswith("no-") and any(danger.startswith(name) for danger in READ_GIT_DANGEROUS_LONG)):
					return denial
			elif option.startswith("-") and not option.startswith("--") and "O" in option[1:]:
				return denial
		return None
	if words[0] == "gh" and any(tuple(words[1:1 + len(form)]) == form for form in READ_GH_SUBCOMMANDS):
		return None
	return denial


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

	Engine order: ``CLAUDE_FIXER_ENABLED=false`` for the review write roles
	in ``CLAUDE_FIXER_ROLES`` (always codex, Q35), the work-item labels
	(``work_item_labels``; ``ai:codex`` beats ``ai:engine-claude``, D2),
	``AI_ENGINE_<ROLE>``, ``AI_ENGINE``, the role's code default. An
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
	if role in CLAUDE_FIXER_ROLES and env.get(CLAUDE_FIXER_SWITCH, "").strip().lower() == "false":
		engine, source = "codex", f"var:{CLAUDE_FIXER_SWITCH}"
	elif LABEL_CODEX in labels:
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
	read_guard_hook: str | None = None,
) -> dict[str, Any]:
	"""Render the P5 permission policy for one run.

	``checkout`` and ``guard_hook`` must be absolute paths. The template's
	``__CHECKOUT__`` and ``__GUARD_HOOK__`` placeholders are replaced inside
	string values only. The two ``.github/workflows`` deny rules are dropped
	when ``allow_workflow_edits``; the ``.claude/**`` rules never are. The
	``read`` profile gets its allow list, a Bash guard, and a read-only gh api
	guard; ``write`` gets no allow list (it runs in ``bypassPermissions``, where
	deny rules still apply).
	"""
	if profile not in PROFILES:
		raise EngineError(f"unknown profile: {profile!r}")
	paths = [("checkout", checkout), ("guard hook", guard_hook)]
	if profile == "read":
		paths.append(("read guard hook", read_guard_hook if read_guard_hook is not None else str((SCRIPT_DIR / "claude_engine.py").resolve())))
	for name, value in paths:
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
	if profile == "read":
		guard_command = f'python3 "{guard_hook}"'
		read_guard_path = read_guard_hook if read_guard_hook is not None else str((SCRIPT_DIR / "claude_engine.py").resolve())
		if not read_guard_path.startswith("/") or any(char in read_guard_path for char in "\n\r\"'$`\\"):
			raise EngineError("read guard path is unsafe")
		guard_found = False
		for hook_group in settings.get("hooks", {}).get("PreToolUse", []):
			for hook_entry in hook_group.get("hooks", []):
				if hook_entry.get("command") == guard_command:
					hook_entry["command"] = guard_command + " --read-only"
					guard_found = True
		if not guard_found:
			raise EngineError("read profile requires the gh api guard hook")
		settings["hooks"]["PreToolUse"].append({
			"matcher": "Bash",
			"hooks": [{"type": "command", "command": f'python3 "{read_guard_path}" {READ_GUARD_SUBCOMMAND}', "timeout": 30}],
		})
	permissions = settings.setdefault("permissions", {})
	deny = [rule for rule in permissions.get("deny", []) if isinstance(rule, str)]
	if allow_workflow_edits:
		deny = [rule for rule in deny if WORKFLOW_DENY_MARKER not in rule]
	if profile == "read":
		deny.extend(READ_PROFILE_DENY)
	permissions["deny"] = deny
	permissions["allow"] = list(READ_PROFILE_ALLOW) if profile == "read" else []
	env = settings.get("env")
	if isinstance(env, dict):
		for key in list(env):
			if "TOKEN" in key.upper() or "KEY" in key.upper() or "SECRET" in key.upper():
				raise EngineError(f"settings env must not carry credentials: {key}")
	return settings


def _support_lock_records(workdir: Path) -> dict[str, Any]:
	"""Capture only trusted support trees, excluding a nested data checkout."""
	def _abort_support_walk(exc: OSError) -> None:
		raise exc

	roots: list[str] = []
	files: list[dict[str, Any]] = []
	dirs: list[dict[str, Any]] = []
	for candidate in support_roots():
		root = candidate.resolve()
		if not root.is_dir() or str(root) in roots:
			continue
		roots.append(str(root))
		for subtree in ("scripts", "prompts", ".github", ".claude", "ai-memory"):
			base = root / subtree
			if not base.exists():
				continue
			if base.is_symlink() or not base.is_dir():
				raise EngineError(f"unsafe support tree: {str(base)!r}")
			for parent, child_dirs, child_files in os.walk(base, followlinks=False, onerror=_abort_support_walk):
				parent_path = Path(parent)
				if root in workdir.parents and (parent_path == workdir or workdir in parent_path.parents):
					child_dirs[:] = []
					continue
				child_dirs[:] = sorted(name for name in child_dirs if name != ".git" and parent_path / name != workdir)
				entries = sorted(child_dirs + [name for name in child_files if name != ".git"])
				for name in entries:
					if (parent_path / name).is_symlink():
						raise EngineError(f"symlink in trusted support: {str(parent_path / name)!r}")
				info = parent_path.lstat()
				dirs.append({"root": str(root), "path": str(parent_path.relative_to(root)), "mode": stat.S_IMODE(info.st_mode), "entries": entries})
				for name in sorted(name for name in child_files if name != ".git"):
					path = parent_path / name
					info = path.lstat()
					if not stat.S_ISREG(info.st_mode):
						raise EngineError(f"non-regular trusted support: {str(path)!r}")
					files.append({"root": str(root), "path": str(path.relative_to(root)), "mode": stat.S_IMODE(info.st_mode), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
	return {"schema": "claude_support_lock.v1", "roots": roots, "workdir": str(workdir), "files": files, "dirs": dirs}


def _load_support_manifest(path: Path) -> dict[str, Any]:
	data = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(data, dict) or data.get("schema") != "claude_support_lock.v1":
		raise EngineError("invalid support lock manifest")
	if not isinstance(data.get("roots"), list) or not isinstance(data.get("workdir"), str):
		raise EngineError("incomplete support lock manifest")
	for section in ("files", "dirs"):
		if not isinstance(data.get(section), list):
			raise EngineError("incomplete support lock manifest")
	for entry in data["files"] + data["dirs"]:
		if not isinstance(entry, dict) or entry.get("root") not in data.get("roots", []) or not isinstance(entry.get("path"), str):
			raise EngineError("invalid support lock entry")
		if Path(entry["path"]).is_absolute() or ".." in Path(entry["path"]).parts:
			raise EngineError("unsafe support lock path")
	return data


def _locked_support_path(entry: dict[str, Any]) -> Path:
	return Path(entry["root"]) / entry["path"]


def _support_unlock_records(data: dict[str, Any]) -> None:
	for entry in sorted(data["dirs"], key=lambda row: len(row["path"])) + data["files"]:
		path = _locked_support_path(entry)
		try:
			if not path.is_symlink() and path.exists():
				path.chmod(entry["mode"])
		except FileNotFoundError:
			continue
		except OSError as exc:
			if exc.errno != errno.EROFS or stat.S_IMODE(path.lstat().st_mode) != entry["mode"]:
				raise


def cmd_support_lock(args: argparse.Namespace) -> int:
	manifest = Path(args.manifest)
	data = _support_lock_records(Path(args.workdir).resolve())
	# An exclusive, private manifest outside the support tree allows recovery if
	# a chmod fails midway. Never follow a pre-existing symlink at this path.
	with open(manifest, "x", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o400)) as handle:
		json.dump(data, handle)
		try:
			for entry in data["files"] + sorted(data["dirs"], key=lambda row: -len(row["path"])):
				path = _locked_support_path(entry)
				try:
					path.chmod(entry["mode"] & ~0o222)
				except OSError as exc:
					if exc.errno == errno.EROFS:
						# A read-only mount is stronger than mode bits; it retains
						# the original mode for the later verification.
						entry["locked_mode"] = entry["mode"]
					elif entry["mode"] & 0o222:
						raise
			handle.seek(0)
			json.dump(data, handle)
			handle.truncate()
		except OSError:
			_support_unlock_records(data)
			raise
	print(len(data["files"]))
	return 0


def cmd_support_verify(args: argparse.Namespace) -> int:
	data = _load_support_manifest(Path(args.manifest))
	bad = False
	for entry in data["dirs"] + data["files"]:
		path = _locked_support_path(entry)
		try:
			info = path.lstat()
			if "entries" in entry:
				valid = stat.S_ISDIR(info.st_mode) and sorted(
					name for name in os.listdir(path) if name != ".git" and path / name != Path(data["workdir"])
				) == entry["entries"]
			else:
				valid = stat.S_ISREG(info.st_mode) and hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
			valid = valid and stat.S_IMODE(info.st_mode) == entry.get("locked_mode", entry["mode"] & ~0o222)
		except OSError:
			valid = False
		if not valid:
			print(f"::error::Trusted support changed: {str(path)!r}", file=sys.stderr)
			bad = True
	return int(bad)


def cmd_support_unlock(args: argparse.Namespace) -> int:
	_support_unlock_records(_load_support_manifest(Path(args.manifest)))
	return 0


def cmd_guard_read_bash(args: argparse.Namespace) -> int:
	try:
		payload = json.load(sys.stdin)
		tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
		command = tool_input.get("command") if isinstance(tool_input, dict) else None
		denial = read_bash_denial(command) if payload.get("tool_name") == "Bash" else "Read-only guard requires a Bash tool call."
	except (ValueError, AttributeError):
		denial = "Malformed read-only Bash hook input."
	if denial:
		_print_json({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": denial}})
	return 0


def read_profile_bash_decision(command: str) -> tuple[str | None, str]:
	"""Fail closed on shell syntax and git options that can write or execute."""
	if not isinstance(command, str) or not command.strip():
		return "deny", "invalid Bash command"
	quote = ""
	escaped = False
	for char in command:
		if char in "\n\r":
			return "deny", "multiline Bash command"
		if escaped:
			escaped = False
			continue
		if char == "\\" and quote != "'":
			escaped = True
			continue
		if char == "'" and quote != '"':
			quote = "" if quote == "'" else "'"
		elif char == '"' and quote != "'":
			quote = "" if quote == '"' else '"'
		elif quote != "'" and char in "`$":
			return "deny", "shell expansion"
		elif not quote and char in ";&|<>()*?[]{}":
			# A glob/brace can expand into a write-capable option after parsing.
			return "deny", "shell control or expansion"
	if quote or escaped:
		return "deny", "unbalanced Bash quoting"
	try:
		argv = shlex.split(command, posix=True)
	except ValueError:
		return "deny", "invalid Bash quoting"
	if len(argv) < 2:
		return "deny", "unsupported Bash command"
	if argv[0] == "git" and argv[1] in ("log", "show", "diff", "status", "ls-files", "grep"):
		for arg in argv[2:]:
			if arg in ("--", "--end-of-options"):
				break
			name = arg.split("=", 1)[0]
			if name.startswith("--") and name not in READ_GUARD_SAFE_EXACT and any(
				name.startswith(option) or (len(name) >= 3 and option.startswith(name))
				for option in _READ_GUARD_GIT_OPTIONS
			):
				return "deny", "write-capable git option"
			if argv[1] == "grep" and name.startswith("-") and not name.startswith("--") and "O" in name:
				return "deny", "pager-capable git grep option"
		return None, ""
	if argv[0] == "gh" and (argv[1:3] in (["issue", "view"], ["pr", "view"], ["pr", "diff"]) or argv[1] == "api"):
		return None, ""
	return "deny", "unsupported Bash command"


def cmd_read_guard(args: argparse.Namespace) -> int:
	"""Respond to a Claude PreToolUse Bash hook; malformed input fails closed."""
	decision, reason = "deny", "invalid hook payload"
	try:
		payload = json.loads(sys.stdin.read())
		if isinstance(payload, dict) and isinstance(payload.get("tool_name"), str) and payload["tool_name"] != "Bash":
			return 0
		if isinstance(payload, dict) and payload.get("tool_name") == "Bash" and isinstance(payload.get("tool_input"), dict):
			decision, reason = read_profile_bash_decision(payload["tool_input"].get("command"))
	except Exception:  # noqa: BLE001 - fail closed even on unexpected hook errors
		# Never echo the payload or exception: either may contain sensitive input.
		decision, reason = "deny", "hook failure"
	if decision:
		_print_json({"hookSpecificOutput": {
			"hookEventName": "PreToolUse",
			"permissionDecision": "deny",
			"permissionDecisionReason": f"claude_engine read-guard: {reason}",
		}})
	return 0


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
	"""Write a role-specific policy; read roles require the flagged gh api guard."""
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
		read_guard_hook=args.read_guard_hook or str(Path(__file__).resolve()),
	)
	out = Path(args.out)
	out.parent.mkdir(parents=True, exist_ok=True)
	out.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
	os.chmod(out, 0o600)
	return 0


_SNAPSHOT_BAD_PARTS = frozenset((".git", ".ai", ".codex", ".claude", ".ssh", ".codex-workflow-src", ".codex-workflow-src-main", ".env", ".git-credentials", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "id_ed25519_sk", "id_ecdsa_sk", "id_xmss", "secrets", "credentials", "__pycache__"))
_SNAPSHOT_BAD_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")


def _snapshot_limit(name: str, default: int) -> int:
	value = os.environ.get(name, str(default))
	if re.fullmatch(r"[1-9][0-9]{0,11}", value):
		return int(value)
	print(f"::warning::invalid {name}; using default", file=sys.stderr)
	return default


def _snapshot_git(workdir: Path, *args: str) -> bytes:
	return subprocess.check_output(["git", "-C", str(workdir), *args], stderr=subprocess.DEVNULL, env=_snapshot_git_env())


def _snapshot_git_env() -> dict[str, str]:
	# The job may export GIT_DIR / GIT_INDEX_FILE for its live checkout. In
	# particular, read-tree must never write through that inherited index.
	return {**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
		"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def _snapshot_copy(source_root: Path, target_root: Path, relative: Path, limits: list[int]) -> None:
	parts = relative.parts
	if not parts or any(part in (".", "..", "") for part in parts) or relative.is_absolute():
		raise EngineError("unsafe snapshot path")
	if any(part.lower() in _SNAPSHOT_BAD_PARTS or part.lower().startswith(".env") or part.lower().endswith(_SNAPSHOT_BAD_SUFFIXES) for part in parts):
		return
	node = source_root
	for index, part in enumerate(parts):
		node = node / part
		try:
			info = node.lstat()
		except FileNotFoundError:
			return  # Tracked deletion in the working tree.
		if stat.S_ISLNK(info.st_mode):
			if index == len(parts) - 1:
				return
			raise EngineError("unsafe snapshot parent")
		if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
			raise EngineError("unsafe snapshot parent")
	if not stat.S_ISREG(info.st_mode):
		return
	limits[0] += 1
	limits[1] += info.st_size
	if limits[0] > limits[2] or limits[1] > limits[3]:
		raise EngineError("snapshot limit exceeded")
	target = target_root.joinpath(*parts)
	target.parent.mkdir(parents=True, exist_ok=True)
	# Resolve every component relative to an open directory descriptor. A
	# parent swapped to a symlink between lstat and open cannot escape the root.
	parent_fd = os.open(source_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
	try:
		for part in parts[:-1]:
			next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
			os.close(parent_fd)
			parent_fd = next_fd
		with os.fdopen(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd), "rb") as src, target.open("xb") as dst:
			opened = os.fstat(src.fileno())
			if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
				raise EngineError("source changed during snapshot")
			remaining = info.st_size
			while remaining:
				chunk = src.read(min(1048576, remaining))
				if not chunk:
					raise EngineError("source changed during snapshot")
				dst.write(chunk)
				remaining -= len(chunk)
			if src.read(1) or os.fstat(src.fileno()).st_size != info.st_size:
				raise EngineError("source changed during snapshot")
	finally:
		os.close(parent_fd)
	os.chmod(target, 0o644)


def _snapshot_copy_git_file(path: Path, target: Path) -> None:
	info = path.lstat()
	if not stat.S_ISREG(info.st_mode):
		raise EngineError("unsafe git metadata")
	with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as src, target.open("xb") as dst:
		opened = os.fstat(src.fileno())
		if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
			raise EngineError("git metadata changed during snapshot")
		remaining = info.st_size
		while remaining:
			chunk = src.read(min(1048576, remaining))
			if not chunk:
				raise EngineError("git metadata changed during snapshot")
			dst.write(chunk)
			remaining -= len(chunk)
		if src.read(1) or os.fstat(src.fileno()).st_size != info.st_size:
			raise EngineError("git metadata changed during snapshot")
	os.chmod(target, 0o644)


def _snapshot_metadata(workdir: Path, dest: Path, omit_root_claude_md: bool = False) -> tuple[str, str]:
	common = Path(os.fsdecode(_snapshot_git(workdir, "rev-parse", "--path-format=absolute", "--git-common-dir")).strip())
	gitdir = Path(os.fsdecode(_snapshot_git(workdir, "rev-parse", "--path-format=absolute", "--git-dir")).strip())
	if (common / "objects/info/alternates").exists():
		return "omitted", "alternates"
	# A shared store can hold commits from detached sibling worktrees which
	# are not covered by this checkout's refs or HEAD.
	if (common / "worktrees").exists() and any((common / "worktrees").iterdir()):
		return "omitted", "filtered_history"
	# A filtered working tree is not safe if git show can recover the same
	# path from an earlier commit. Retain history only when its paths pass
	# the working-tree filter; never mount a partly filtered object store.
	with subprocess.Popen(["git", "-C", str(workdir), "log", "--all", "HEAD", "--format=", "--name-only", "-z", "--no-renames"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_snapshot_git_env()) as history_process:
		assert history_process.stdout is not None
		history_pending = b""
		while history_chunk := history_process.stdout.read(65536):
			history_paths = (history_pending + history_chunk).split(b"\0")
			history_pending = history_paths.pop()
			if len(history_pending) > 1048576:
				history_process.terminate()
				return "omitted", "filtered_history"
			for history_path in history_paths:
				history_relative = Path(os.fsdecode(history_path.lstrip(b"\n")))
				parts = history_relative.parts
				if omit_root_claude_md and history_relative == Path("CLAUDE.md") or any(part.lower() in _SNAPSHOT_BAD_PARTS or part.lower().startswith(".env") or part.lower().endswith(_SNAPSHOT_BAD_SUFFIXES) for part in parts):
					history_process.terminate()
					return "omitted", "filtered_history"
		if history_pending or history_process.wait() != 0:
			return "omitted", "filtered_history"
	# Even a clean ref history cannot authorize copying unreferenced objects
	# (such as a SHA-only fetch or a recently deleted secret-bearing commit).
	with subprocess.Popen(["git", "-C", str(workdir), "fsck", "--unreachable", "--no-reflogs"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_snapshot_git_env()) as fsck_probe:
		assert fsck_probe.stdout is not None
		if fsck_probe.stdout.read(1):
			fsck_probe.terminate()
			return "omitted", "filtered_history"
		if fsck_probe.wait() != 0:
			return "omitted", "filtered_history"
	meta = dest / ".git"
	meta.mkdir()
	# Copy only object/refs trees and HEAD; never git's credentials, hooks,
	# alternates, worktrees or local settings. Reject symlinks in metadata.
	for folder in ("objects", "refs"):
		root = common / folder
		if not root.is_dir() or root.is_symlink():
			raise EngineError("unsafe git metadata")
		for base, dirs, files in os.walk(root, followlinks=False):
			for directory in dirs:
				if (Path(base) / directory).is_symlink():
					raise EngineError("unsafe git metadata")
			for filename in files:
				path = Path(base) / filename
				if path.is_symlink() or not path.is_file():
					raise EngineError("unsafe git metadata")
				if filename.endswith(".lock") or path.relative_to(root).parts[:1] == ("info",):
					continue
				target = meta / folder / path.relative_to(root)
				target.parent.mkdir(parents=True, exist_ok=True)
				try:
					os.link(path, target, follow_symlinks=False)
				except OSError:
					_snapshot_copy_git_file(path, target)
				if target.is_symlink() or not target.is_file():
					raise EngineError("unsafe git metadata")
	for filename, root in (("HEAD", gitdir), ("packed-refs", common), ("shallow", common)):
		path = root / filename
		if path.exists():
			if path.is_symlink() or not path.is_file():
				raise EngineError("unsafe git metadata")
			_snapshot_copy_git_file(path, meta / filename)
	version = _snapshot_git(workdir, "config", "--local", "--get", "core.repositoryformatversion").decode().strip()
	if version not in ("0", "1"):
		raise EngineError("unsupported git repository format")
	format_result = subprocess.run(["git", "-C", str(workdir), "config", "--local", "--get", "extensions.objectformat"], capture_output=True, check=False, env=_snapshot_git_env())
	if format_result.returncode not in (0, 1):
		raise EngineError("invalid git object format")
	object_format = format_result.stdout.decode().strip()
	if object_format and object_format not in ("sha1", "sha256"):
		raise EngineError("invalid git object format")
	(meta / "config").write_text(f"[core]\n\trepositoryformatversion = {version}\n\tbare = false\n" + (f"[extensions]\n\tobjectformat = {object_format}\n" if object_format else ""), encoding="ascii")
	_snapshot_git(dest, "read-tree", "HEAD")
	return "copied", ""


def read_snapshot(workdir: Path, dest: Path, omit_root_claude_md: bool = False) -> dict[str, Any]:
	if not workdir.is_absolute() or not dest.is_absolute() or not workdir.is_dir() or dest.exists() or workdir == dest or workdir in dest.parents:
		raise EngineError("invalid snapshot directory")
	if any(parent.is_symlink() for parent in (workdir, *workdir.parents)):
		raise EngineError("unsafe snapshot root")
	limits = [0, 0, _snapshot_limit("CLAUDE_READ_SNAPSHOT_MAX_FILES", 50000), _snapshot_limit("CLAUDE_READ_SNAPSHOT_MAX_BYTES", 1073741824)]
	git_workdir = False
	try:
		git_workdir = _snapshot_git(workdir, "rev-parse", "--is-inside-work-tree").strip() == b"true"
	except (OSError, subprocess.CalledProcessError):
		pass
	if not git_workdir and ((workdir / ".git").exists() or (workdir / ".git").is_symlink()):
		raise EngineError("snapshot git metadata unavailable")
	if git_workdir and _snapshot_git(workdir, "rev-parse", "--show-toplevel").strip() != os.fsencode(workdir):
		raise EngineError("snapshot must start at git worktree root")
	dest.mkdir(parents=True, mode=0o700)
	try:
		if git_workdir:
			paths = list(dict.fromkeys(Path(os.fsdecode(path)) for path in _snapshot_git(workdir, "ls-files", "-z", "--cached").split(b"\0") if path))
		else:
			paths = []
			for base, dirs, files in os.walk(workdir, followlinks=False):
				dirs[:] = [item for item in dirs if item.lower() not in _SNAPSHOT_BAD_PARTS and not item.lower().startswith(".env") and not (Path(base) / item).is_symlink()]
				paths.extend((Path(base) / item).relative_to(workdir) for item in files)
		for path in paths:
			if omit_root_claude_md and path == Path("CLAUDE.md"):
				continue
			_snapshot_copy(workdir, dest, path, limits)
		git, reason = _snapshot_metadata(workdir, dest, omit_root_claude_md) if git_workdir else ("none", "")
		return {"files": limits[0], "bytes": limits[1], "git": git, "reason": reason}
	except (OSError, ValueError, subprocess.CalledProcessError):
		# Partial snapshots must never be mounted.
		raise EngineError("snapshot rejected") from None


def cmd_read_sandbox_dockerfile(args: argparse.Namespace) -> int:
	print(READ_SANDBOX_DOCKERFILE, end="")


def build_read_snapshot(source: Path, dest: Path, omit_claude_md: bool = False, git_objects_mount: str = "/git-objects") -> dict[str, Any]:
	"""Copy safe source files into an empty, bounded snapshot, never following links.

	The source's git configuration, hooks, index and object store are never
	copied. A synthetic commit contains only the filtered snapshot files.
	"""
	if not git_objects_mount.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9_/-]+", git_objects_mount):
		raise EngineError("invalid git objects mount")
	if not source.is_dir() or source.is_symlink() or not dest.is_dir() or dest.is_symlink() or any(dest.iterdir()):
		raise EngineError("read snapshot requires a directory source and empty directory destination")
	if source.resolve() == dest.resolve() or source.resolve() in dest.resolve().parents or dest.resolve() in source.resolve().parents:
		raise EngineError("read snapshot source and destination must be separate")
	def limit(name: str, default: int) -> int:
		try:
			value = int(os.environ.get(name, str(default)))
		except ValueError as exc:
			raise EngineError("invalid read snapshot limit") from exc
		if value <= 0:
			raise EngineError("invalid read snapshot limit")
		return value

	max_files = limit("CLAUDE_READ_SNAPSHOT_MAX_FILES", 50000)
	max_bytes = limit("CLAUDE_READ_SNAPSHOT_MAX_BYTES", 1073741824)
	git_env = {key: value for key, value in os.environ.items() if key not in (
		"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
		"GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
		"GIT_TEMPLATE_DIR", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL",
	) and not key.startswith("GIT_CONFIG_KEY_") and not key.startswith("GIT_CONFIG_VALUE_")}
	git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TEMPLATE_DIR=os.devnull)

	def git_run(repo: Path, *args: str, data: str | None = None, alternate: str = "") -> subprocess.CompletedProcess[str]:
		env = dict(git_env)
		if alternate:
			env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = alternate
		return subprocess.run(["git", "-C", str(repo), "-c", "core.fsmonitor=false",
			"-c", "core.hooksPath=/dev/null", *args], input=data, text=True,
			capture_output=True, env=env, check=True)

	git_root = None
	try:
		root_result = git_run(source, "rev-parse", "--show-toplevel").stdout.strip()
		if Path(root_result).resolve() == source.resolve():
			git_root = source
	except (subprocess.CalledProcessError, OSError):
		pass
	if git_root is None and (source / ".git").exists():
		raise EngineError("read snapshot git metadata failed")
	if git_root:
		try:
			entries = [os.fsdecode(item) for item in git_run(source, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.split("\0") if item]
		except (subprocess.CalledProcessError, OSError) as exc:
			raise EngineError("read snapshot file listing failed") from exc
	else:
		entries = []
		for current, dirs, files in os.walk(source, followlinks=False):
			dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
			entries.extend(str((Path(current) / name).relative_to(source)) for name in files)

	files_copied = bytes_copied = 0
	for entry in sorted(set(entries)):
		parts = Path(entry).parts
		if (not parts or Path(entry).is_absolute() or ".." in parts or
			any(part.lower() in (".git", ".claude", ".ssh") or part.lower().startswith((".codex-workflow-src", ".env"))
				or part.lower() in ("secrets", "credentials") for part in parts) or
			parts[-1].lower() in (".git-credentials", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "id_ed25519_sk", "id_ecdsa_sk", "id_xmss") or
			Path(entry).suffix.lower() in (".pem", ".key", ".p12", ".pfx", ".keystore") or
			(omit_claude_md and parts[-1] == "CLAUDE.md")):
			continue
		current = source
		missing = False
		for part in parts[:-1]:
			current /= part
			try:
				info = current.lstat()
			except FileNotFoundError:
				missing = True
				break
			if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
				raise EngineError("unsafe read snapshot parent")
		if missing:
			continue
		path = current / parts[-1]
		try:
			info = path.lstat()
		except FileNotFoundError:
			continue
		if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
			continue
		files_copied += 1
		bytes_copied += info.st_size
		if files_copied > max_files or bytes_copied > max_bytes:
			raise EngineError("read snapshot limit exceeded")
		target = dest.joinpath(*parts)
		target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
		# Use dirfds as well as O_NOFOLLOW on the leaf: a parent may be
		# swapped for a symlink after lstat, before a path-based open.
		parent_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
		try:
			for part in parts[:-1]:
				child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
				os.close(parent_fd)
				parent_fd = child_fd
			fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
			with os.fdopen(fd, "rb") as reader, target.open("xb") as writer:
				opened = os.fstat(reader.fileno())
				if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
					raise EngineError("source changed during read snapshot")
				content = reader.read(info.st_size + 1)
				if len(content) != info.st_size:
					raise EngineError("source changed during read snapshot")
				writer.write(content)
		finally:
			os.close(parent_fd)
		target.chmod(0o755 if info.st_mode & 0o111 else 0o644)

	result: dict[str, Any] = {"files": files_copied, "bytes": bytes_copied, "git": "absent", "git_objects": ""}
	if git_root:
		try:
			git_run(dest, "-c", "init.templateDir=/dev/null", "init", "-q")
			git_run(dest, "add", "-A", "-f")
			git_run(dest, "-c", "user.name=Isolated Snapshot", "-c", "user.email=snapshot@example.invalid",
				"commit", "--allow-empty", "--no-gpg-sign", "-qm", "Isolated read snapshot")
			result["git"] = "present"
		except (subprocess.CalledProcessError, OSError, ValueError) as exc:
			raise EngineError("read snapshot git metadata failed") from exc
	return result


def cmd_read_snapshot(args: argparse.Namespace) -> int:
	if args.source:
		try:
			result = build_read_snapshot(Path(args.source), Path(args.dest), args.omit_claude_md, args.git_objects_mount)
		except OSError as exc:
			raise EngineError("read snapshot source changed or could not be copied") from exc
	else:
		result = read_snapshot(Path(args.workdir), Path(args.dest), args.omit_root_claude_md)
	_print_json(result)
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
	p.add_argument("--read-guard-hook", default="")
	p.set_defaults(func=cmd_settings)

	p = sub.add_parser("read-snapshot")
	snapshot_source = p.add_mutually_exclusive_group(required=True)
	snapshot_source.add_argument("--workdir")
	snapshot_source.add_argument("--source")
	p.add_argument("--dest", required=True)
	p.add_argument("--omit-root-claude-md", action="store_true")
	p.add_argument("--git-objects-mount", default="/git-objects")
	p.add_argument("--omit-claude-md", action="store_true")
	p.set_defaults(func=cmd_read_snapshot)

	p = sub.add_parser("read-sandbox-dockerfile")
	p.set_defaults(func=cmd_read_sandbox_dockerfile)

	p = sub.add_parser(READ_GUARD_SUBCOMMAND)
	p.set_defaults(func=cmd_guard_read_bash)

	for name, func in (("support-lock", cmd_support_lock), ("support-verify", cmd_support_verify), ("support-unlock", cmd_support_unlock)):
		p = sub.add_parser(name)
		p.add_argument("--manifest", required=True)
		if name == "support-lock":
			p.add_argument("--workdir", required=True)
		p.set_defaults(func=func)

	p = sub.add_parser("read-guard")
	p.set_defaults(func=cmd_read_guard)

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
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		return args.func(args)
	except (EngineError, OSError, ValueError) as exc:
		print(f"claude_engine.py: {exc}", file=sys.stderr)
		return 2


if __name__ == "__main__":
	sys.exit(main())

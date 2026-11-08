#!/usr/bin/env python3
"""ChatGPT account reviewer slot (trial plan Phase 2, Refs #8969).

The review panel's GPT slot can run the Codex CLI on a ChatGPT account
(.github/actions/chatgpt-pool-token) in a read-only review sandbox behind
scripts/chatgpt_codex_relay.py. The whole path is gated on
reviewer_slot_enabled in .github/ai/chatgpt_pool.json (shipped false): off,
the panel, the tiers and the log are unchanged. The harness extracts the real
functions from scripts/review_run_reviewers.sh and fakes the sandbox helper.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
REVIEWERS = SCRIPTS / "review_run_reviewers.sh"
SANDBOX = SCRIPTS / "review_untrusted_sandbox.sh"
ENGINE = SCRIPTS / "claude_engine.py"
RELAY = SCRIPTS / "chatgpt_codex_relay.py"
DOCKERFILE = SCRIPTS / "review_sandbox" / "Dockerfile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
POOL_CONFIG = REPO_ROOT / ".github" / "ai" / "chatgpt_pool.json"
# The shipped switch value. The tests use this literal instead of reading the
# file at import time, so a checkout without the file still collects; a missing
# file resolves to config_missing, which keeps the slot off (fail closed).
SHIPPED_CONFIG = '{"reviewer_slot_enabled": false, "model": "gpt-6-sol", "replaces": "openai/gpt-6-luna", "reasoning": "xhigh"}'


def _shipped_config_text() -> str:
	return POOL_CONFIG.read_text(encoding="utf-8") if POOL_CONFIG.is_file() else SHIPPED_CONFIG
STAGE = SCRIPTS / "stage_workflow_support.sh"

PANEL = [
	"minimax/minimax-m3",
	"anthropic/claude-sonnet-5.5",
	"z-ai/glm-5.3-flash",
	"google/gemini-3.1-flash-lite",
	"openai/gpt-6-luna",
]
SLOT = "chatgpt/gpt-6-sol"
SAFE = "chatgpt_gpt-6-sol"
PR = "991234"
FINDINGS = "File: src/app.py\nProblem: wrong operator\nSEVERITY: HIGH"
ON_ENV = {
	"REVIEWER_CHATGPT_SLOT_RESOLVED": "true",
	"REVIEWER_CHATGPT_MODEL_RESOLVED": "gpt-6-sol",
	"REVIEWER_CHATGPT_REPLACES_RESOLVED": "openai/gpt-6-luna",
	"REVIEWER_CHATGPT_REASONING_RESOLVED": "xhigh",
}


def _env(**extra: str) -> dict[str, str]:
	env = {key: value for key, value in os.environ.items() if not key.startswith(("REVIEWER_CHATGPT_", "CHATGPT_POOL_", "REVIEW_TIER"))}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.update(extra)
	return env


def _function(text: str, name: str) -> str:
	body = text.split(f"\n{name}() {{", 1)[1].split("\n}\n", 1)[0]
	return f"{name}() {{{body}\n}}\n"


def _seeded(count: int, models: list[str]) -> list[str]:
	"""Python twin of reviewer_pick_seeded_models: lowest sha256(seed:model), panel order."""
	ranked = sorted((hashlib.sha256(f"{PR}:{model}".encode()).hexdigest(), model) for model in models)
	picked = {model for _, model in ranked[:count]}
	return [model for model in models if model in picked]


# --- chatgpt-slot / chatgpt-classify ------------------------------------------


def _engine_module():
	spec = importlib.util.spec_from_file_location("claude_engine_chatgpt_test", ENGINE)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def _slot(tmp_path: Path, content: str | None, *args: str) -> dict[str, str]:
	config = tmp_path / "chatgpt_pool.json"
	if content is not None:
		config.write_text(content, encoding="utf-8")
	proc = subprocess.run(
		[sys.executable, str(ENGINE), "chatgpt-slot", "--config", str(config), *args],
		env=_env(), capture_output=True, text=True, check=True,
	)
	lines = proc.stdout.strip().splitlines()
	assert len(lines) == 1
	return dict(part.split("=", 1) for part in lines[0].split())


def test_shipped_config_keeps_the_slot_off(tmp_path: Path) -> None:
	shipped = json.loads(_shipped_config_text())
	assert shipped["reviewer_slot_enabled"] is False
	assert shipped == {"reviewer_slot_enabled": False, "model": "gpt-6-sol", "replaces": "openai/gpt-6-luna", "reasoning": "xhigh"}
	result = _slot(tmp_path, _shipped_config_text())
	assert result == {"enabled": "false", "model": "gpt-6-sol", "replaces": "openai/gpt-6-luna", "reasoning": "xhigh", "source": "file", "reason": "switch_off"}


ON = '{"reviewer_slot_enabled": true, "model": "gpt-6-sol", "replaces": "openai/gpt-6-luna", "reasoning": "xhigh"}'


@pytest.mark.parametrize(
	("content", "args", "enabled", "reason"),
	[
		(None, (), "false", "config_missing"),
		(None, ("--override", "true"), "false", "config_missing"),
		("{not json", (), "false", "config_invalid"),
		("[]", (), "false", "config_invalid"),
		('{"reviewer_slot_enabled": "true"}', (), "false", "config_invalid"),
		('{"reviewer_slot_enabled": 1}', (), "false", "config_invalid"),
		('{"reviewer_slot_enabled": true, "model": "GPT 6"}', (), "false", "config_invalid"),
		('{"reviewer_slot_enabled": true, "replaces": "no-slash"}', (), "false", "config_invalid"),
		('{"reviewer_slot_enabled": true, "reasoning": "max"}', (), "false", "config_invalid"),
		('{}', (), "false", "switch_off"),
		(ON, (), "true", "ok"),
		(ON, ("--override", "false"), "false", "switch_off"),
		('{"reviewer_slot_enabled": false}', ("--override", "true"), "true", "ok"),
		(ON, ("--override", "yes"), "false", "var_invalid"),
		(ON, ("--model", "bad model"), "false", "var_invalid"),
		(ON, ("--replaces", "$(id)"), "false", "var_invalid"),
	],
)
def test_chatgpt_slot_matrix_fails_closed(tmp_path: Path, content, args, enabled, reason) -> None:
	result = _slot(tmp_path, content, *args)
	assert (result["enabled"], result["reason"]) == (enabled, reason)
	# Printed values are always validated, so they are safe for a step output.
	assert re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,63}", result["model"])
	assert re.fullmatch(r"[a-z0-9-]+/[a-z0-9._-]+", result["replaces"])


def test_chatgpt_slot_vars_override_model_and_replaces(tmp_path: Path) -> None:
	result = _slot(tmp_path, ON, "--model", "gpt-7", "--replaces", "z-ai/glm-5.3-flash")
	assert result["enabled"] == "true"
	assert (result["model"], result["replaces"]) == ("gpt-7", "z-ai/glm-5.3-flash")
	assert _slot(tmp_path, ON, "--override", "true")["source"] == "var"


def _classify(tmp_path: Path, events: list[dict] | str, exit_code: int) -> dict[str, str]:
	path = tmp_path / "events.jsonl"
	path.write_text(events if isinstance(events, str) else "\n".join(json.dumps(event) for event in events), encoding="utf-8")
	proc = subprocess.run(
		[sys.executable, str(ENGINE), "chatgpt-classify", "--events", str(path), "--exit-code", str(exit_code)],
		env=_env(), capture_output=True, text=True, check=True,
	)
	return json.loads(proc.stdout)


AGENT = {"type": "item.completed", "item": {"type": "agent_message", "text": "secret review text"}}


def test_chatgpt_classify_reason_codes(tmp_path: Path) -> None:
	assert _classify(tmp_path, [{"type": "thread.started"}, AGENT, {"type": "turn.completed"}], 0) == {"outcome": "ok", "reason": "ok"}
	assert _classify(tmp_path, [{"type": "error", "message": "unexpected status 401 Unauthorized"}], 1)["reason"] == "auth_rejected"
	assert _classify(tmp_path, [{"type": "turn.failed", "error": {"message": "403 Forbidden"}}], 1)["reason"] == "auth_rejected"
	assert _classify(tmp_path, [{"type": "error", "message": "429 Too Many Requests: usage_limit_reached"}], 1)["reason"] == "quota_exhausted"
	assert _classify(tmp_path, "garbage\n{oops", 0)["reason"] == "run_failed"
	assert _classify(tmp_path, [AGENT], 1)["reason"] == "run_failed"
	assert _classify(tmp_path, [{"type": "turn.completed"}], 0)["reason"] == "run_failed"


def test_chatgpt_role_stays_out_of_the_claude_roles() -> None:
	module = _engine_module()
	assert module.CHATGPT_ROLES == ("PANEL_REVIEWER_CHATGPT",)
	assert "PANEL_REVIEWER_CHATGPT" not in module.ROLES
	assert "PANEL_REVIEWER_CHATGPT" not in module.DEFAULT_CONFIG["role_defaults"]


# --- switch + tier pinning ------------------------------------------------------


def _tier_harness() -> str:
	text = REVIEWERS.read_text(encoding="utf-8")
	early = text.split("# ── ChatGPT account review-panel slot: switch + tier pinning ──\n", 1)[1].split(
		"# ── End ChatGPT account review-panel slot: switch + tier pinning ──", 1
	)[0]
	return "".join(_function(text, name) for name in (
		"normalize_reviewer_model_list", "reviewer_write_model_list_file", "reviewer_pick_seeded_models", "resolve_review_tier_active_models",
	)) + early


def _tier(tmp_path: Path, tier: str, panel: list[str] | None = None, **env: str) -> dict[str, object]:
	active = tmp_path / f"active_{tier}.txt"
	script = _tier_harness() + textwrap.dedent("""\
		reviewer_chatgpt_slot_resolve
		resolve_review_tier_active_models "$TIER" || true
		echo "SOURCE=${REVIEW_TIER_ACTIVE_MODELS_SOURCE}"
		echo "ACTIVE=${REVIEWER_CHATGPT_SLOT_ACTIVE}"
		""")
	proc = subprocess.run(
		["bash", "-c", "set -euo pipefail\n" + script],
		env=_env(REVIEWER_MODELS="\n".join(panel or PANEL), REVIEWER_ACTIVE_MODELS_FILE=str(active), PR_NUMBER=PR, TIER=tier, **env),
		capture_output=True, text=True, check=True,
	)
	lines = proc.stdout.splitlines()
	return {
		"models": active.read_text(encoding="utf-8").split(),
		"source": next(line for line in lines if line.startswith("SOURCE="))[7:],
		"active": next(line for line in lines if line.startswith("ACTIVE="))[7:],
		"chatgpt_lines": [line for line in lines + proc.stderr.splitlines() if "REVIEWER_CHATGPT" in line or "PANEL_REVIEWER_CHATGPT" in line],
	}


@pytest.mark.parametrize("switch", [None, "false", "garbage", "TRUE", ""])
def test_off_path_tiers_are_unchanged(tmp_path: Path, switch: str | None) -> None:
	extra = {} if switch is None else {**ON_ENV, "REVIEWER_CHATGPT_SLOT_RESOLVED": switch}
	lite = _tier(tmp_path, "lite", **extra)
	standard = _tier(tmp_path, "standard", **extra)
	full = _tier(tmp_path, "full", **extra)
	assert (lite["models"], lite["source"]) == (_seeded(1, PANEL), "random_lite")
	assert (standard["models"], standard["source"]) == (_seeded(4, PANEL), "random_standard")
	assert (full["models"], full["source"]) == (PANEL, "full")
	for result in (lite, standard, full):
		assert result["active"] == "false"
		assert result["chatgpt_lines"] == []


def test_on_path_swaps_in_place_and_pins_the_account_slots(tmp_path: Path) -> None:
	swapped = PANEL[:4] + [SLOT]
	full = _tier(tmp_path, "full", **ON_ENV)
	assert full["models"] == swapped and full["source"] == "full" and full["active"] == "true"
	assert "REVIEWER_CHATGPT: switch=on swap=openai/gpt-6-luna->chatgpt/gpt-6-sol" in full["chatgpt_lines"]
	lite = _tier(tmp_path, "lite", **ON_ENV)
	assert (lite["models"], lite["source"]) == (["anthropic/claude-sonnet-5.5", SLOT], "pinned_lite")
	standard = _tier(tmp_path, "standard", **ON_ENV)
	rest = [model for model in swapped if model not in ("anthropic/claude-sonnet-5.5", SLOT)]
	drawn = _seeded(2, rest)
	assert standard["source"] == "pinned_standard"
	assert standard["models"] == [model for model in swapped if model in drawn or model in ("anthropic/claude-sonnet-5.5", SLOT)]
	assert len(standard["models"]) == 4
	assert _tier(tmp_path, "standard", **ON_ENV)["models"] == standard["models"]


def test_swap_keeps_a_middle_panel_position(tmp_path: Path) -> None:
	result = _tier(tmp_path, "full", **{**ON_ENV, "REVIEWER_CHATGPT_REPLACES_RESOLVED": "z-ai/glm-5.3-flash"})
	assert result["models"] == [PANEL[0], PANEL[1], SLOT, PANEL[3], PANEL[4]]


def test_configured_tier_slugs_still_win(tmp_path: Path) -> None:
	lite = _tier(tmp_path, "lite", **ON_ENV, REVIEW_TIER_LITE_REVIEWER_SLUG="z-ai/glm-5.3-flash")
	assert (lite["models"], lite["source"]) == (["z-ai/glm-5.3-flash"], "configured_lite")
	standard = _tier(tmp_path, "standard", **ON_ENV, REVIEW_TIER_STANDARD_REVIEWER_SLUGS="minimax/minimax-m3,z-ai/glm-5.3-flash")
	assert (standard["models"], standard["source"]) == (["minimax/minimax-m3", "z-ai/glm-5.3-flash"], "configured_standard")


def test_replaces_not_in_panel_keeps_the_slot_inactive(tmp_path: Path) -> None:
	result = _tier(tmp_path, "lite", **{**ON_ENV, "REVIEWER_CHATGPT_REPLACES_RESOLVED": "openai/not-on-panel"})
	assert result["active"] == "false"
	assert (result["models"], result["source"]) == (_seeded(1, PANEL), "random_lite")
	assert any("swap=skipped reason=replaces_not_in_panel" in line for line in result["chatgpt_lines"])


def test_resolve_runs_before_the_tier_classifiers() -> None:
	text = REVIEWERS.read_text(encoding="utf-8")
	assert "\nreviewer_chatgpt_slot_resolve\nclassify_reviewer_risk_tier\nclassify_review_tier\n" in text


# --- run_chatgpt_reviewer -------------------------------------------------------

FAKE_SANDBOX = textwrap.dedent("""\
	#!/usr/bin/env bash
	set -u
	printf '%s|home=%s\\n' "$*" "${CHATGPT_POOL_CODEX_HOME:-}" >> "${FAKE_CALLS}"
	case "$1" in
		prepare-ephemeral)
			[ "${FAKE_PREPARE_RC:-0}" -eq 0 ] || exit "${FAKE_PREPARE_RC}"
			mktemp -d; exit 0 ;;
		cleanup) exit 0 ;;
		run)
			[ -z "${FAKE_REASON:-}" ] || echo "AI_ENGINE_FALLBACK role=PANEL_REVIEWER_CHATGPT reason=${FAKE_REASON}" >&2
			[ -z "${FAKE_OUT:-}" ] || printf '%s\\n' "${FAKE_OUT}" > "$3"
			exit "${FAKE_RC:-0}" ;;
	esac
	exit 2
	""")


def _chatgpt_runner() -> str:
	text = REVIEWERS.read_text(encoding="utf-8")
	block = text.split("# ── ChatGPT account review-panel slot (PANEL_REVIEWER_CHATGPT) ──\n", 1)[1].split("# ── End ChatGPT account review-panel slot ──", 1)[0]
	return "".join(_function(text, name) for name in ("reviewer_output_has_explicit_none", "reviewer_output_has_findings")) + block


def _run_slot(tmp_path: Path, active: bool = True, **env: str) -> dict[str, object]:
	support = tmp_path / "support"
	support.mkdir()
	(support / "review_untrusted_sandbox.sh").write_text(FAKE_SANDBOX, encoding="utf-8")
	reviews = tmp_path / "reviews"
	reviews.mkdir()
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("review this\n", encoding="utf-8")
	calls = tmp_path / "calls.txt"
	health = tmp_path / "health.txt"
	script = _chatgpt_runner() + textwrap.dedent("""\
		prepare_reviewer_prompt_for_model() { printf '%s\\n' "$2"; }
		reviewer_base_reasoning_effort() { printf '%s\\n' "${1:-xhigh}"; }
		reviewer_log_slot_state() { :; }
		codex_run_budget_remaining_secs() { printf '%s\\n' "${FAKE_BUDGET:-5000}"; }
		reviewer_record_health_outcome() { printf '%s %s\\n' "$1" "$2" >> "${FAKE_HEALTH}"; }
		REVIEWER_CHATGPT_SLOT_ACTIVE="${TEST_ACTIVE}"
		REVIEWER_CHATGPT_SLOT_SLUG="chatgpt/gpt-6-sol"
		run_chatgpt_reviewer "chatgpt/gpt-6-sol" "chatgpt_gpt-6-sol" review "$PROMPT" medium
		""")
	base = {
		**ON_ENV,
		"CHATGPT_POOL_STEP_AVAILABLE": "true",
		"CHATGPT_POOL_STEP_CODEX_HOME": str(tmp_path / "chatgpt-pool" / "ACCT"),
		"CHATGPT_POOL_STEP_REASON": "selected",
	}
	base.update(env)
	proc = subprocess.run(
		["bash", "-c", "set -euo pipefail\n" + script],
		env=_env(
			SUPPORT_SCRIPTS_DIR=str(support), PREVIOUS_REVIEWS_DIR=str(reviews), RUNNER_TEMP=str(tmp_path),
			GITHUB_WORKSPACE=str(tmp_path), WORKSPACE_PATH="", PR_NUMBER=PR, PROMPT=str(prompt),
			FAKE_CALLS=str(calls), FAKE_HEALTH=str(health), TEST_ACTIVE="true" if active else "false", **base,
		),
		capture_output=True, text=True, check=True,
	)
	call_lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
	return {
		"status": (reviews / f"status_review_{SAFE}.txt").read_text(encoding="utf-8").strip(),
		"output": (reviews / f"review_{SAFE}.txt").read_text(encoding="utf-8"),
		"log": (reviews / f"review_{SAFE}.log").read_text(encoding="utf-8"),
		"runs": [line for line in call_lines if line.startswith("run ")],
		"prepares": [line for line in call_lines if line.startswith("prepare-ephemeral")],
		"cleanups": [line for line in call_lines if line.startswith("cleanup")],
		"health": health.read_text(encoding="utf-8").splitlines() if health.exists() else [],
		"stderr": proc.stderr,
	}


def test_on_path_runs_one_read_only_chatgpt_sandbox(tmp_path: Path) -> None:
	result = _run_slot(tmp_path, FAKE_OUT=FINDINGS)
	assert result["status"] == "success"
	assert "Problem: wrong operator" in result["output"]
	# Only the run sees the credential directory; prepare never does.
	assert result["prepares"] == ["prepare-ephemeral chatgpt|home="]
	assert len(result["runs"]) == 1
	run = result["runs"][0]
	# Bare Codex model, the configured slot reasoning, read-only panel role; the
	# credential directory travels only in the environment, never on argv.
	assert " gpt-6-sol xhigh /dev/null chatgpt PANEL_REVIEWER read|home=" in run
	assert run.endswith(str(tmp_path / "chatgpt-pool" / "ACCT"))
	assert len(result["cleanups"]) == 1
	assert result["health"] == ["chatgpt/gpt-6-sol primary_success"]
	assert "REVIEWER_POOL: slot=chatgpt/gpt-6-sol pass=review status=success model=gpt-6-sol attempts=1 reason=none" in result["log"]
	assert "AI_ENGINE_FALLBACK" not in result["log"]


@pytest.mark.parametrize(
	("env", "reason"),
	[
		({"CHATGPT_POOL_STEP_AVAILABLE": "false", "CHATGPT_POOL_STEP_REASON": "broker_refused_chatgpt_pool_empty"}, "chatgpt_pool_unavailable"),
		({"CHATGPT_POOL_STEP_AVAILABLE": "", "CHATGPT_POOL_STEP_CODEX_HOME": ""}, "chatgpt_pool_unavailable"),
		({"CHATGPT_POOL_STEP_CODEX_HOME": ""}, "chatgpt_pool_unavailable"),
	],
)
def test_credential_unavailable_skips_without_a_sandbox(tmp_path: Path, env: dict[str, str], reason: str) -> None:
	result = _run_slot(tmp_path, FAKE_OUT=FINDINGS, **env)
	assert result["status"] == "skipped_pool"
	assert result["prepares"] == [] and result["runs"] == []
	assert f"AI_ENGINE_FALLBACK role=PANEL_REVIEWER_CHATGPT reason={reason} action=skip" in result["log"]
	assert result["health"] == []


def test_switch_off_skips_a_chatgpt_slot(tmp_path: Path) -> None:
	result = _run_slot(tmp_path, active=False, FAKE_OUT=FINDINGS)
	assert result["status"] == "skipped_pool"
	assert result["prepares"] == [] and result["runs"] == []
	assert "AI_ENGINE_FALLBACK role=PANEL_REVIEWER_CHATGPT reason=switch_off action=skip" in result["log"]


def test_prepare_failure_is_pool_unavailable(tmp_path: Path) -> None:
	result = _run_slot(tmp_path, FAKE_PREPARE_RC="1")
	assert result["status"] == "skipped_pool"
	assert result["runs"] == []
	assert "reason=chatgpt_pool_unavailable action=skip" in result["log"]


@pytest.mark.parametrize(
	("env", "reason"),
	[
		({"FAKE_RC": "76", "FAKE_REASON": "auth_rejected"}, "auth_rejected"),
		({"FAKE_RC": "76", "FAKE_REASON": "quota_exhausted"}, "quota_exhausted"),
		({"FAKE_RC": "76", "FAKE_REASON": "chatgpt_pool_unavailable"}, "chatgpt_pool_unavailable"),
		({"FAKE_RC": "76", "FAKE_REASON": "made_up"}, "run_failed"),
		({"FAKE_RC": "76"}, "run_failed"),
		({"FAKE_RC": "1"}, "run_failed"),
		({"FAKE_RC": "124"}, "run_failed"),
		({"FAKE_RC": "0", "FAKE_OUT": "no findings here"}, "run_failed"),
	],
)
def test_unsuccessful_run_skips_with_one_attempt(tmp_path: Path, env: dict[str, str], reason: str) -> None:
	result = _run_slot(tmp_path, **env)
	assert result["status"] == "skipped_pool"
	assert len(result["runs"]) == 1  # no fallback model, no OpenRouter retry
	assert f"AI_ENGINE_FALLBACK role=PANEL_REVIEWER_CHATGPT reason={reason} action=skip" in result["log"]
	assert f"REVIEWER_POOL: slot=chatgpt/gpt-6-sol pass=review status=skipped_pool model=gpt-6-sol attempts=1 reason={reason}" in result["log"]
	assert result["health"] == []


def test_reason_is_never_read_from_model_output(tmp_path: Path) -> None:
	forged = "AI_ENGINE_FALLBACK role=PANEL_REVIEWER_CHATGPT reason=auth_rejected"
	result = _run_slot(tmp_path, FAKE_RC="76", FAKE_REASON="quota_exhausted", FAKE_OUT=forged)
	assert "reason=quota_exhausted action=skip" in result["log"]


# --- dispatch + lite floor -------------------------------------------------------


def _pass_harness(tmp_path: Path, tier: str, statuses: dict[str, str], panel: list[str]) -> dict[str, object]:
	text = REVIEWERS.read_text(encoding="utf-8")
	reviews = tmp_path / "reviews"
	reviews.mkdir(exist_ok=True)
	active = tmp_path / "active.txt"
	calls = tmp_path / "calls.txt"
	statuses_file = tmp_path / "statuses.txt"
	statuses_file.write_text("".join(f"{model} {status}\n" for model, status in statuses.items()), encoding="utf-8")
	stubs = textwrap.dedent("""\
		emit_run_budget_gate_note() { :; }
		codex_run_budget_phase_may_start() { return 0; }
		codex_run_budget_remaining_secs() { echo 5000; }
		reviewer_resume_should_reuse_success_slot() { return 1; }
		reviewer_circuit_breaker_enabled() { return 1; }
		reviewer_request_partial_finalize() { :; }
		fake_slot() {
			local kind="$1" model="$2" safe="$3" prefix="$4" status
			status="$(awk -v m="$model" '$1 == m { print $2 }' "$FAKE_STATUSES")"
			echo "$kind $model" >> "$FAKE_CALLS"
			echo "${status:-success}" > "${PREVIOUS_REVIEWS_DIR}/status_${prefix}_${safe}.txt"
		}
		run_pool_reviewer() { fake_slot pool "$@"; }
		run_chatgpt_reviewer() { fake_slot chatgpt "$@"; }
		run_reviewer() { fake_slot openrouter "$@"; }
		""")
	script = "".join(_function(text, name) for name in (
		"normalize_reviewer_model_list", "reviewer_write_model_list_file", "reviewer_pick_seeded_models",
		"get_active_reviewer_models_text", "reviewer_is_pool_slot", "run_reviewer_pass", "resolve_review_tier_active_models",
	)) + text.split("# ── ChatGPT account review-panel slot: switch + tier pinning ──\n", 1)[1].split("# ── End ChatGPT", 1)[0] + stubs + textwrap.dedent("""\
		reviewer_chatgpt_slot_resolve >/dev/null
		resolve_review_tier_active_models "$REVIEW_TIER"
		echo "PASS_SUCCESSFUL=$(run_reviewer_pass review "$PROMPT" medium)"
		""")
	active_panel = "\n".join(panel)
	proc = subprocess.run(
		["bash", "-c", "set -euo pipefail\n" + script],
		env=_env(
			**ON_ENV, REVIEWER_MODELS=active_panel, REVIEWER_ACTIVE_MODELS_FILE=str(active), PREVIOUS_REVIEWS_DIR=str(reviews),
			PR_NUMBER=PR, REVIEW_TIER=tier, PROMPT=str(tmp_path / "p.txt"), FAKE_CALLS=str(calls), FAKE_STATUSES=str(statuses_file),
		),
		capture_output=True, text=True, check=True,
	)
	return {
		"calls": calls.read_text(encoding="utf-8").splitlines() if calls.exists() else [],
		"successful": next(line for line in proc.stdout.splitlines() if line.startswith("PASS_SUCCESSFUL="))[16:],
		"stderr": proc.stderr,
	}


@pytest.mark.parametrize("statuses", [
	{"anthropic/claude-sonnet-5.5": "skipped_pool", SLOT: "skipped_pool"},
	{"anthropic/claude-sonnet-5.5": "failed", SLOT: "skipped_pool"},
])
def test_lite_floor_runs_one_seeded_live_reviewer(tmp_path: Path, statuses: dict[str, str]) -> None:
	result = _pass_harness(tmp_path, "lite", statuses, PANEL)
	floor = _seeded(1, ["minimax/minimax-m3", "z-ai/glm-5.3-flash", "google/gemini-3.1-flash-lite"])[0]
	assert result["calls"][:2] == ["pool anthropic/claude-sonnet-5.5", f"chatgpt {SLOT}"]
	assert result["calls"][2:] == [f"openrouter {floor}"]
	assert f"REVIEWER_CHATGPT: lite_floor model={floor} reason=account_slots_unsuccessful" in result["stderr"]
	assert result["successful"] == "1"


def test_no_floor_when_an_account_slot_succeeded(tmp_path: Path) -> None:
	result = _pass_harness(tmp_path, "lite", {"anthropic/claude-sonnet-5.5": "skipped_pool", SLOT: "success"}, PANEL)
	assert [call.split()[0] for call in result["calls"]] == ["pool", "chatgpt"]
	assert "lite_floor" not in result["stderr"]


def test_no_floor_on_the_standard_tier(tmp_path: Path) -> None:
	statuses = {model: "skipped_pool" for model in PANEL[:4] + [SLOT]}
	result = _pass_harness(tmp_path, "standard", statuses, PANEL)
	assert len(result["calls"]) == 4
	assert "lite_floor" not in result["stderr"]


def test_dispatch_routes_chatgpt_slots_only() -> None:
	text = REVIEWERS.read_text(encoding="utf-8")
	assert re.search(
		r'if reviewer_is_pool_slot "\$\{model\}"; then\n\s+run_pool_reviewer .*\n\s+elif reviewer_is_chatgpt_slot "\$\{model\}"; then\n\s+run_chatgpt_reviewer .*\n\s+else\n\s+run_reviewer ',
		text,
	)


# --- relay -----------------------------------------------------------------------


def _relay_module():
	spec = importlib.util.spec_from_file_location("chatgpt_codex_relay_test", RELAY)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def _write_auth(directory: Path, mode: int = 0o600) -> Path:
	directory.mkdir(mode=0o700, parents=True, exist_ok=True)
	path = directory / "auth.json"
	path.write_text(json.dumps({"tokens": {"access_token": "real-access-token", "account_id": "acct-123"}}), encoding="utf-8")
	path.chmod(mode)
	return path


class _FakeResponse:
	status = 200

	def __init__(self):
		self._chunks = [b"data: one\n\n", b"data: two\n\n"]

	def getheaders(self):
		return [("Content-Type", "text/event-stream"), ("Set-Cookie", "leak=1"), ("x-codex-primary-used-percent", "12")]

	def read1(self, _size):
		return self._chunks.pop(0) if self._chunks else b""


@pytest.fixture
def relay_chain(tmp_path: Path, monkeypatch):
	relay = _relay_module()
	upstream: list[dict] = []

	class FakeHTTPS:
		def __init__(self, host, timeout=None, context=None):
			self.host = host

		def request(self, method, path, body, headers):
			upstream.append({"host": self.host, "method": method, "path": path, "body": body, "headers": dict(headers)})

		def getresponse(self):
			return _FakeResponse()

		def close(self):
			pass

	monkeypatch.setattr(relay.http.client, "HTTPSConnection", FakeHTTPS)
	token, account = relay.read_credentials(str(_write_auth(tmp_path / "pool" / "ACCT").parent))
	sock = str(tmp_path / "provider.sock")
	broker = relay.UnixHTTPServer(sock, relay.Relay)
	broker.mode, broker.token, broker.account, broker.model = "broker", token, account, "gpt-6-sol"
	bridge = http.server.HTTPServer(("127.0.0.1", 0), relay.Relay)
	bridge.mode, bridge.socket_path = "bridge", sock
	threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (broker, bridge)]
	for thread in threads:
		thread.start()
	yield relay, bridge.server_address[1], sock, upstream
	for server in (broker, bridge):
		server.shutdown()
		server.server_close()


def _post(port: int, body: dict | bytes, path: str = "/backend-api/codex/responses", method: str = "POST", headers: dict | None = None):
	connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
	payload = body if isinstance(body, bytes) else json.dumps(body).encode()
	sent = {"Content-Type": "application/json", "Authorization": "Bearer isolated-placeholder", "chatgpt-account-id": "isolated-placeholder-account"}
	sent.update(headers or {})
	connection.request(method, path, payload if method == "POST" else None, {k: v for k, v in sent.items() if v is not None})
	response = connection.getresponse()
	result = (response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read())
	connection.close()
	return result


def test_relay_injects_the_real_credential_only_on_the_host(relay_chain) -> None:
	_relay, port, _sock, upstream = relay_chain
	status, headers, body = _post(port, {"model": "gpt-6-sol", "input": [], "tools": [{"type": "function", "name": "shell"}, {"type": "custom", "name": "apply_patch"}]})
	assert status == 200
	assert body == b"data: one\n\ndata: two\n\n"
	assert "set-cookie" not in headers and headers.get("x-codex-primary-used-percent") == "12"
	assert len(upstream) == 1
	sent = upstream[0]
	assert (sent["host"], sent["method"], sent["path"]) == ("chatgpt.com", "POST", "/backend-api/codex/responses")
	assert sent["headers"]["authorization"] == "Bearer real-access-token"
	assert sent["headers"]["chatgpt-account-id"] == "acct-123"


@pytest.mark.parametrize(
	("body", "kwargs"),
	[
		({"model": "gpt-other", "input": []}, {}),
		({"model": "gpt-6-sol", "tools": [{"type": "web_search"}]}, {}),
		({"model": "gpt-6-sol", "tools": [{"type": "mcp", "server_url": "https://x"}]}, {}),
		({"model": "gpt-6-sol", "tools": [{"type": "code_interpreter"}]}, {}),
		({"model": "gpt-6-sol", "tools": "all"}, {}),
		({"model": "gpt-6-sol"}, {"path": "/backend-api/codex/models"}),
		({"model": "gpt-6-sol"}, {"path": "/v1/responses"}),
		({"model": "gpt-6-sol"}, {"headers": {"Authorization": "Bearer real-looking"}}),
		({"model": "gpt-6-sol"}, {"headers": {"Authorization": None}}),
		({"model": "gpt-6-sol"}, {"headers": {"Content-Encoding": "zstd"}}),
		(b"not json", {}),
	],
)
def test_relay_refuses_everything_else(relay_chain, body, kwargs) -> None:
	_relay, port, _sock, upstream = relay_chain
	status, _headers, _body = _post(port, body, **kwargs)
	assert status == 400
	assert upstream == []


def test_relay_refuses_get(relay_chain) -> None:
	_relay, port, _sock, upstream = relay_chain
	status, _headers, _body = _post(port, b"", method="GET")
	assert status == 405
	assert upstream == []


def test_broker_refuses_a_client_authorization(relay_chain) -> None:
	relay, _port, sock, upstream = relay_chain
	connection = relay.UnixHTTPConnection(sock)
	body = json.dumps({"model": "gpt-6-sol"}).encode()
	connection.request("POST", "/backend-api/codex/responses", body, {"Content-Type": "application/json", "Authorization": "Bearer x"})
	assert connection.getresponse().status == 400
	connection.close()
	assert upstream == []


def test_relay_credential_file_must_be_owner_only_and_not_a_symlink(tmp_path: Path) -> None:
	relay = _relay_module()
	loose = _write_auth(tmp_path / "loose", 0o644)
	with pytest.raises(ValueError):
		relay.read_credentials(str(loose.parent))
	real = _write_auth(tmp_path / "real")
	linked = tmp_path / "linked"
	linked.mkdir(mode=0o700)
	(linked / "auth.json").symlink_to(real)
	with pytest.raises(OSError):
		relay.read_credentials(str(linked))
	proc = subprocess.run([sys.executable, str(RELAY), "broker", str(tmp_path / "s.sock"), str(loose.parent), "gpt-6-sol"], env=_env(), capture_output=True, text=True, timeout=10)
	assert proc.returncode == 2 and proc.stdout == "" and "real-access-token" not in proc.stderr


def test_placeholder_auth_holds_no_credential(tmp_path: Path) -> None:
	out = tmp_path / "auth.json"
	subprocess.run([sys.executable, str(RELAY), "placeholder-auth", str(out)], env=_env(), check=True)
	assert stat.S_IMODE(out.stat().st_mode) == 0o600
	auth = json.loads(out.read_text(encoding="utf-8"))
	assert auth["tokens"]["access_token"] == "isolated-placeholder"
	assert "real-access-token" not in out.read_text(encoding="utf-8")
	header, payload, signature = auth["tokens"]["id_token"].split(".")
	assert header and signature
	claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
	assert claims == {"https://api.openai.com/auth": {"chatgpt_account_id": "isolated-placeholder-account", "chatgpt_plan_type": "pro"}}
	assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", auth["last_refresh"])


# --- sandbox --------------------------------------------------------------------


def _sandbox_run(tmp_path: Path, *args: str, engine_file: str | None = None, **env: str) -> subprocess.CompletedProcess:
	runner_temp = tmp_path / "rt"
	root = runner_temp / "review-isolated-abcdefgh"
	(root / "socket").mkdir(parents=True)
	(root / "home").mkdir()
	(root / "image").write_text("sha256:" + "0" * 64 + "\n", encoding="utf-8")
	(root / "baseline.json").write_text("{}", encoding="utf-8")
	(root / "workspace").write_text(str(tmp_path) + "\n", encoding="utf-8")
	if engine_file:
		(root / "engine").write_text(engine_file + "\n", encoding="utf-8")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	docker = bin_dir / "docker"
	docker.write_text("#!/usr/bin/env bash\necho docker-called >> \"${FAKE_DOCKER_LOG}\"\nexit 0\n", encoding="utf-8")
	docker.chmod(0o755)
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("review\n", encoding="utf-8")
	return subprocess.run(
		["bash", str(SANDBOX), "run", str(prompt), str(tmp_path / "out.txt"), *args],
		env=_env(
			PATH=f"{bin_dir}:{os.environ['PATH']}", SUPPORT_SCRIPTS_DIR=str(SCRIPTS), REVIEW_SANDBOX_ROOT=str(root),
			RUNNER_TEMP=str(runner_temp), FAKE_DOCKER_LOG=str(tmp_path / "docker.log"), **env,
		),
		capture_output=True, text=True, timeout=60,
	)


@pytest.mark.parametrize("args", [
	("gpt-6-sol", "xhigh", "/dev/null", "chatgpt", "REVIEW_EDITOR", "write"),
	("gpt-6-sol", "xhigh", "/dev/null", "chatgpt", "PANEL_REVIEWER", "write"),
	("gpt-6-sol", "xhigh", "/dev/null", "chatgpt", "SECURITY_JUDGE", "read"),
	("openai/gpt-6-sol", "xhigh", "/dev/null", "chatgpt", "PANEL_REVIEWER", "read"),
])
def test_sandbox_refuses_chatgpt_outside_the_read_only_panel_role(tmp_path: Path, args) -> None:
	result = _sandbox_run(tmp_path, *args, engine_file="chatgpt")
	assert result.returncode == 2
	assert not (tmp_path / "docker.log").exists()


def test_sandbox_fails_closed_without_a_valid_credential_dir(tmp_path: Path) -> None:
	args = ("gpt-6-sol", "xhigh", "/dev/null", "chatgpt", "PANEL_REVIEWER", "read")
	wrong_engine = _sandbox_run(tmp_path / "a", *args, engine_file="claude")
	assert wrong_engine.returncode == 76
	assert "AI_ENGINE_FALLBACK role=PANEL_REVIEWER_CHATGPT reason=chatgpt_pool_unavailable" in wrong_engine.stderr
	outside = tmp_path / "b" / "elsewhere"
	_write_auth(outside)
	not_under_pool = _sandbox_run(tmp_path / "b", *args, engine_file="chatgpt", CHATGPT_POOL_CODEX_HOME=str(outside))
	assert not_under_pool.returncode == 76
	assert not (tmp_path / "b" / "docker.log").exists()
	loose = tmp_path / "c" / "rt" / "chatgpt-pool" / "ACCT"
	_write_auth(loose)
	loose.chmod(0o755)
	loose_dir = _sandbox_run(tmp_path / "c", *args, engine_file="chatgpt", CHATGPT_POOL_CODEX_HOME=str(loose))
	assert loose_dir.returncode == 76


def test_sandbox_chatgpt_branch_isolation_contract() -> None:
	text = SANDBOX.read_text(encoding="utf-8")
	assert "case \"${prepare_engine}\" in claude|codex|chatgpt) ;; *) exit 2 ;; esac" in text
	assert "case \"${engine}\" in codex|claude|chatgpt) ;; *) exit 2 ;; esac" in text
	branch = text.split('if [ "${engine}" = chatgpt ]; then', 1)[1].split("\nfi\n", 1)[0]
	run_line = branch.split("docker run", 1)[1].split("/bin/bash -c", 1)[0]
	assert "--network none" in run_line and "--read-only" in run_line and "--cap-drop ALL" in run_line
	assert "dst=/source,readonly" in run_line
	assert "CHATGPT_POOL_CODEX_HOME" not in run_line and "chatgpt_codex_home" not in run_line
	assert '"${chatgpt_codex_home}" "${model}" &' in branch  # broker gets the directory, never the token
	assert "env -i PATH=\"${PATH}\" PYTHONDONTWRITEBYTECODE=1" in branch
	assert "--sandbox read-only" in branch and 'requires_openai_auth = true' in branch and 'web_search = "disabled"' in branch
	assert "review_untrusted_workspace.py\" transfer" not in branch
	assert "--build-arg \"CODEX_CLI_VERSION=${chatgpt_cli_version}\"" in text


def test_dockerfile_installs_codex_only_with_the_build_arg() -> None:
	text = DOCKERFILE.read_text(encoding="utf-8")
	assert "ARG CODEX_CLI_VERSION=\n" in text
	assert 'RUN if [ -n "${CODEX_CLI_VERSION}" ]; then' in text
	assert '"@openai/codex@${CODEX_CLI_VERSION}"' in text


def test_relay_is_staged_as_optional_support() -> None:
	line = next(candidate for candidate in STAGE.read_text(encoding="utf-8").splitlines() if candidate.startswith("OPTIONAL_BOOTSTRAP_SCRIPTS="))
	assert "chatgpt_codex_relay.py" in line.split("=", 1)[1].strip('"').split()


# --- workflow ---------------------------------------------------------------------


def test_workflow_wiring_defaults_off() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	assert len(WORKFLOW.read_bytes()) < 480_000
	for name in ("REVIEWER_CHATGPT_SLOT_ENABLED", "REVIEWER_CHATGPT_MODEL", "REVIEWER_CHATGPT_REPLACES"):
		assert f"  {name}: ${{{{ vars.{name} || '' }}}}\n" in text
	step = text.split("      - name: ChatGPT pool token\n", 1)[1].split("\n\n", 1)[0]
	assert "id: chatgpt_pool" in step
	assert "if: env.PR_CLOSED != 'true' && steps.ai_engine.outputs.chatgpt_slot == 'true'" in step
	assert "continue-on-error: true" in step
	assert "uses: ./.codex-workflow-src/.github/actions/chatgpt-pool-token" in step
	assert "mode: token" in step and "config_path: .codex-workflow-src/.github/ai/claude_engine.json" in step
	assert text.index("id: claude_pool") < text.index("id: chatgpt_pool") < text.index("id: reviewers")
	assert '--config "${GITHUB_WORKSPACE:-${PWD}}/.codex-workflow-src/.github/ai/chatgpt_pool.json"' in text
	reviewers = text.split("        id: reviewers\n", 1)[1].split("        run: |", 1)[0]
	for name, expr in (
		("REVIEWER_CHATGPT_SLOT_RESOLVED", "steps.ai_engine.outputs.chatgpt_slot"),
		("REVIEWER_CHATGPT_MODEL_RESOLVED", "steps.ai_engine.outputs.chatgpt_model"),
		("REVIEWER_CHATGPT_REPLACES_RESOLVED", "steps.ai_engine.outputs.chatgpt_replaces"),
		("REVIEWER_CHATGPT_REASONING_RESOLVED", "steps.ai_engine.outputs.chatgpt_reasoning"),
		("CHATGPT_POOL_STEP_AVAILABLE", "steps.chatgpt_pool.outputs.available"),
		("CHATGPT_POOL_STEP_REASON", "steps.chatgpt_pool.outputs.reason"),
		("CHATGPT_POOL_STEP_ACCOUNT", "steps.chatgpt_pool.outputs.account"),
		("CHATGPT_POOL_STEP_CODEX_HOME", "steps.chatgpt_pool.outputs.codex_home"),
	):
		assert f"{name}: ${{{{ {expr} || '' }}}}" in reviewers


def _resolve_step_body() -> str:
	text = WORKFLOW.read_text(encoding="utf-8")
	step = text.split("      - name: Resolve AI engine\n", 1)[1].split("      - name: Install Claude Code CLI", 1)[0]
	body = step.split("        run: |\n", 1)[1]
	return textwrap.dedent(body)


@pytest.mark.parametrize(
	("config", "override", "expected"),
	[
		(SHIPPED_CONFIG, "", "false"),
		(ON, "", "true"),
		(ON, "false", "false"),
		("{broken", "true", "false"),
		(None, "", "false"),
	],
)
def test_resolve_step_writes_the_slot_outputs(tmp_path: Path, config: str | None, override: str, expected: str) -> None:
	support = tmp_path / "support"
	support.mkdir()
	(support / "claude_engine.py").write_text(ENGINE.read_text(encoding="utf-8"), encoding="utf-8")
	ai = tmp_path / "ws" / ".codex-workflow-src" / ".github" / "ai"
	ai.mkdir(parents=True)
	if config is not None:
		(ai / "chatgpt_pool.json").write_text(config, encoding="utf-8")
	output = tmp_path / "out"
	genv = tmp_path / "env"
	subprocess.run(
		["bash", "-c", _resolve_step_body()],
		env=_env(SUPPORT_SCRIPTS_DIR=str(support), GITHUB_WORKSPACE=str(tmp_path / "ws"), GITHUB_OUTPUT=str(output),
			GITHUB_ENV=str(genv), REVIEWER_MODELS="\n".join(PANEL), REVIEWER_CHATGPT_SLOT_ENABLED=override),
		capture_output=True, text=True, check=True,
	)
	outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
	assert outputs["chatgpt_slot"] == expected
	if expected == "true":
		assert (outputs["chatgpt_model"], outputs["chatgpt_replaces"], outputs["chatgpt_reasoning"]) == ("gpt-6-sol", "openai/gpt-6-luna", "xhigh")


def test_ci_runs_this_suite() -> None:
	text = CI.read_text(encoding="utf-8")
	assert "tests/test_review_chatgpt_reviewer_slot.py" in text


def test_chatgpt_classify_completed_run_wins_over_a_recovered_error(tmp_path: Path) -> None:
	events = [{"type": "error", "message": "429 Too Many Requests, retrying"}, AGENT, {"type": "turn.completed"}]
	assert _classify(tmp_path, events, 0) == {"outcome": "ok", "reason": "ok"}
	assert _classify(tmp_path, events, 1)["reason"] == "quota_exhausted"


def test_configured_tier_slug_naming_the_replaced_slug_maps_to_the_slot(tmp_path: Path) -> None:
	lite = _tier(tmp_path, "lite", **ON_ENV, REVIEW_TIER_LITE_REVIEWER_SLUG="openai/gpt-6-luna")
	assert (lite["models"], lite["source"]) == ([SLOT], "configured_lite")
	standard = _tier(tmp_path, "standard", **ON_ENV, REVIEW_TIER_STANDARD_REVIEWER_SLUGS="minimax/minimax-m3,openai/gpt-6-luna")
	assert (standard["models"], standard["source"]) == (["minimax/minimax-m3", SLOT], "configured_standard")


def test_resolve_step_keeps_the_slot_off_when_replaces_is_not_on_the_panel(tmp_path: Path) -> None:
	support = tmp_path / "support"
	support.mkdir()
	(support / "claude_engine.py").write_text(ENGINE.read_text(encoding="utf-8"), encoding="utf-8")
	ai = tmp_path / "ws" / ".codex-workflow-src" / ".github" / "ai"
	ai.mkdir(parents=True)
	(ai / "chatgpt_pool.json").write_text(ON, encoding="utf-8")
	output = tmp_path / "out"
	subprocess.run(
		["bash", "-c", _resolve_step_body()],
		env=_env(SUPPORT_SCRIPTS_DIR=str(support), GITHUB_WORKSPACE=str(tmp_path / "ws"), GITHUB_OUTPUT=str(output),
			GITHUB_ENV=str(tmp_path / "env"), REVIEWER_MODELS="minimax/minimax-m3,z-ai/glm-5.3-flash"),
		capture_output=True, text=True, check=True,
	)
	outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
	assert outputs["chatgpt_slot"] == "false"

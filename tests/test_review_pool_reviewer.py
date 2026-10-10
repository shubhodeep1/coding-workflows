#!/usr/bin/env python3
"""Behaviour of the Claude account-pool review-panel slot (run_pool_reviewer).

An anthropic/claude-* REVIEWER_MODELS entry runs on the Claude account pool in
a read-only prepare-ephemeral review sandbox: Sonnet first, one retry on
REVIEWER_POOL_FALLBACK_MODEL (Haiku) when the Sonnet run does not succeed, and
status skipped_pool when the pool itself is unavailable. The harness extracts
the real functions from scripts/review_run_reviewers.sh and replaces the
sandbox helper with a fake that records its calls.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEWERS = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
SANDBOX = REPO_ROOT / "scripts" / "review_untrusted_sandbox.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
SLOT = "anthropic/claude-sonnet-5.5"
SAFE = "anthropic_claude-sonnet-5_5"

FAKE_SANDBOX = textwrap.dedent("""\
	#!/usr/bin/env bash
	# Fake review_untrusted_sandbox.sh: records calls, behaves per FAKE_* env.
	set -u
	printf '%s\\n' "$*" >> "${FAKE_CALLS}"
	case "$1" in
	  prepare-ephemeral)
	    [ "${FAKE_PREPARE_RC:-0}" -eq 0 ] || exit "${FAKE_PREPARE_RC}"
	    root="$(mktemp -d)"; printf '%s\\n' "${root}"; exit 0 ;;
	  cleanup) exit 0 ;;
	  run)
	    out="$3"; model="$4"
	    case "${model}" in
	      claude-sonnet-5-5) rc="${FAKE_SONNET_RC:-0}"; body="${FAKE_SONNET_OUT:-}"; reason="${FAKE_SONNET_REASON:-}" ;;
	      *) rc="${FAKE_HAIKU_RC:-0}"; body="${FAKE_HAIKU_OUT:-}"; reason="${FAKE_HAIKU_REASON:-}" ;;
	    esac
	    echo "CLAUDE_POOL run role=$8 account=TEST outcome=fake reason=fake exit_code=${rc}" >&2
	    [ -z "${reason}" ] || echo "AI_ENGINE_FALLBACK role=$8 reason=${reason}" >&2
	    [ -z "${body}" ] || printf '%s\\n' "${body}" > "${out}"
	    exit "${rc}" ;;
	esac
	exit 2
	""")

FINDINGS = "File: src/app.py\nProblem: wrong operator\nSEVERITY: HIGH"


def _pool_functions() -> str:
	text = REVIEWERS.read_text(encoding="utf-8")
	block = text.split("# ── Claude account-pool review-panel slot (engine role PANEL_REVIEWER) ──\n", 1)[1].split("# ── Two-pass reviewer architecture", 1)[0]
	helpers = []
	for name in ("reviewer_output_has_explicit_none", "reviewer_output_has_findings"):
		body = text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
		helpers.append(f"{name}() {{{body}\n}}\n")
	return "".join(helpers) + block


def _run(tmp_path: Path, **env: str) -> dict[str, object]:
	support = tmp_path / "support"
	support.mkdir()
	(support / "review_untrusted_sandbox.sh").write_text(FAKE_SANDBOX, encoding="utf-8")
	reviews = tmp_path / "reviews"
	reviews.mkdir()
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("review this\n", encoding="utf-8")
	calls = tmp_path / "calls.txt"
	health = tmp_path / "health.txt"
	script = _pool_functions() + textwrap.dedent("""\
		prepare_reviewer_prompt_for_model() { printf '%s\\n' "$2"; }
		reviewer_base_reasoning_effort() { printf '%s\\n' "${1:-xhigh}"; }
		reviewer_log_slot_state() { printf 'REVIEWER_SLOT_STATE: slot=%s fallback_model_used=%s\\n' "$2" "$7" | tee -a "$1"; }
		codex_run_budget_remaining_secs() { printf '%s\\n' "${FAKE_BUDGET:-5000}"; }
		reviewer_record_health_outcome() { printf '%s %s %s\\n' "$1" "$2" "$4" >> "${FAKE_HEALTH}"; }
		run_pool_reviewer "$SLOT" "$SAFE" review "$PROMPT" xhigh
		""")
	proc = subprocess.run(
		["bash", "-c", "set -euo pipefail\n" + script],
		env={
			**os.environ,
			"SUPPORT_SCRIPTS_DIR": str(support),
			"PREVIOUS_REVIEWS_DIR": str(reviews),
			"RUNNER_TEMP": str(tmp_path),
			"GITHUB_WORKSPACE": str(tmp_path),
			"WORKSPACE_PATH": "",
			"PR_NUMBER": "991234",
			"SLOT": SLOT,
			"SAFE": SAFE,
			"PROMPT": str(prompt),
			"FAKE_CALLS": str(calls),
			"FAKE_HEALTH": str(health),
			"AI_ENGINE_RESOLVED_PANEL_REVIEWER": "claude",
			**env,
		},
		capture_output=True,
		text=True,
		check=True,
	)
	call_lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
	return {
		"status": (reviews / f"status_review_{SAFE}.txt").read_text(encoding="utf-8").strip(),
		"output": (reviews / f"review_{SAFE}.txt").read_text(encoding="utf-8") if (reviews / f"review_{SAFE}.txt").exists() else "",
		"log": (reviews / f"review_{SAFE}.log").read_text(encoding="utf-8"),
		"runs": [line.split()[3] for line in call_lines if line.startswith("run ")],
		"run_lines": [line for line in call_lines if line.startswith("run ")],
		"prepares": [line for line in call_lines if line.startswith("prepare-ephemeral")],
		"cleanups": [line for line in call_lines if line.startswith("cleanup")],
		"health": health.read_text(encoding="utf-8").splitlines() if health.exists() else [],
		"stderr": proc.stderr,
	}


def test_sonnet_success_uses_one_read_only_pool_run(tmp_path: Path) -> None:
	result = _run(tmp_path, FAKE_SONNET_OUT=FINDINGS)
	assert result["status"] == "success"
	assert "Problem: wrong operator" in result["output"]
	assert result["runs"] == ["claude-sonnet-5-5"]
	assert result["prepares"] == ["prepare-ephemeral claude"]
	assert len(result["cleanups"]) == 1
	# Read-only PANEL_REVIEWER role, reviewer reasoning as the effort hint.
	assert result["run_lines"][0].endswith("claude-sonnet-5-5 xhigh /dev/null claude PANEL_REVIEWER read")
	assert "REVIEWER_POOL: slot=anthropic/claude-sonnet-5.5 pass=review status=success model=claude-sonnet-5-5 attempts=1" in result["log"]
	assert "fallback_model_used=false" in result["log"]
	# Sandbox telemetry reaches the job log, not only the per-slot log.
	assert "CLAUDE_POOL run role=PANEL_REVIEWER" in result["stderr"]


def test_explicit_none_counts_as_success(tmp_path: Path) -> None:
	result = _run(tmp_path, FAKE_SONNET_OUT="NONE")
	assert result["status"] == "success"
	assert result["runs"] == ["claude-sonnet-5-5"]


@pytest.mark.parametrize(
	"sonnet_env",
	[
		{"FAKE_SONNET_RC": "1"},  # crash, refusal or classify failure
		{"FAKE_SONNET_RC": "124"},  # wall-clock timeout
		{"FAKE_SONNET_RC": "0", "FAKE_SONNET_OUT": "I could not finish the review."},  # malformed output
		{"FAKE_SONNET_RC": "75", "FAKE_SONNET_REASON": "all_accounts_failed"},  # usage limit on every account
		{"FAKE_SONNET_RC": "75", "FAKE_SONNET_REASON": "all_usage_limit"},  # capacity reason (capacity-only policy)
		{"FAKE_SONNET_RC": "76", "FAKE_SONNET_REASON": "all_accounts_failed"},  # refused, but per-model
	],
)
def test_failed_sonnet_run_retries_on_haiku_in_the_pool(tmp_path: Path, sonnet_env: dict[str, str]) -> None:
	result = _run(tmp_path, FAKE_HAIKU_OUT=FINDINGS, **sonnet_env)
	assert result["status"] == "success"
	assert result["runs"] == ["claude-sonnet-5-5", "claude-haiku-5-5"]
	assert len(result["prepares"]) == 1
	assert "status=success model=claude-haiku-5-5 attempts=2" in result["log"]
	assert "fallback_model_used=true" in result["log"]


def test_fallback_model_is_configurable(tmp_path: Path) -> None:
	result = _run(tmp_path, FAKE_SONNET_RC="1", FAKE_HAIKU_OUT=FINDINGS, REVIEWER_POOL_FALLBACK_MODEL="claude-haiku-6")
	assert result["runs"] == ["claude-sonnet-5-5", "claude-haiku-6"]


@pytest.mark.parametrize("rc", ["75", "76"])
@pytest.mark.parametrize("reason", ["no_credential", "sandbox_not_prepared", "policy_unavailable", "support_missing", "isolation_unavailable"])
def test_pool_wide_outage_skips_the_slot_without_the_fallback_model(tmp_path: Path, reason: str, rc: str) -> None:
	# Exit 76 is a fallback the capacity-only AI_ENGINE_FALLBACK_POLICY refused;
	# the slot has no codex path, so it is read like 75.
	result = _run(tmp_path, FAKE_SONNET_RC=rc, FAKE_SONNET_REASON=reason, FAKE_HAIKU_OUT=FINDINGS)
	assert result["status"] == "skipped_pool"
	assert result["runs"] == ["claude-sonnet-5-5"]
	assert f"AI_ENGINE_FALLBACK role=PANEL_REVIEWER reason={reason} action=skip" in result["log"]
	assert len(result["cleanups"]) == 1


def test_unavailable_without_a_reason_line_is_pool_wide(tmp_path: Path) -> None:
	# Exit 75 with no AI_ENGINE_FALLBACK line must not kill the worker under
	# `set -euo pipefail`; it is treated as a pool-wide outage.
	result = _run(tmp_path, FAKE_SONNET_RC="75", FAKE_HAIKU_OUT=FINDINGS)
	assert result["status"] == "skipped_pool"
	assert result["runs"] == ["claude-sonnet-5-5"]
	assert "reason=claude_unavailable action=skip" in result["log"]


def test_sandbox_prepare_failure_skips_the_slot(tmp_path: Path) -> None:
	result = _run(tmp_path, FAKE_PREPARE_RC="1", FAKE_SONNET_OUT=FINDINGS)
	assert result["status"] == "skipped_pool"
	assert result["runs"] == []
	assert "reason=sandbox_prepare_failed action=skip" in result["log"]
	assert result["cleanups"] == []


def test_codex_engine_skips_the_slot_without_preparing(tmp_path: Path) -> None:
	result = _run(tmp_path, AI_ENGINE_RESOLVED_PANEL_REVIEWER="codex", FAKE_SONNET_OUT=FINDINGS)
	assert result["status"] == "skipped_pool"
	assert result["prepares"] == []
	assert result["runs"] == []
	assert "reason=engine_codex action=skip" in result["log"]


def test_both_models_failing_marks_the_slot_failed(tmp_path: Path) -> None:
	result = _run(tmp_path, FAKE_SONNET_RC="1", FAKE_HAIKU_RC="1")
	assert result["status"] == "failed"
	assert result["runs"] == ["claude-sonnet-5-5", "claude-haiku-5-5"]
	assert "failed on the Claude account pool after 2 attempt(s)" in result["output"]


def test_pool_outcomes_feed_the_circuit_breaker(tmp_path: Path) -> None:
	for name in ("ok", "failed", "pool", "codex"):
		(tmp_path / name).mkdir()
	assert _run(tmp_path / "ok", FAKE_SONNET_OUT=FINDINGS)["health"] == [f"{SLOT} primary_success "]
	failed = _run(tmp_path / "failed", FAKE_SONNET_RC="1", FAKE_HAIKU_RC="1")
	assert failed["health"] == [f"{SLOT} retryable_failure pool_exit_1"]
	# Skips are not slot failures and must not open the circuit.
	assert _run(tmp_path / "pool", FAKE_SONNET_RC="75", FAKE_SONNET_REASON="no_credential")["health"] == []
	assert _run(tmp_path / "codex", AI_ENGINE_RESOLVED_PANEL_REVIEWER="codex")["health"] == []


def test_fallback_success_counts_as_a_slot_failure(tmp_path: Path) -> None:
	# Like run_reviewer: a Haiku success after a failed Sonnet run must not
	# reset the slot's consecutive failure count.
	result = _run(tmp_path, FAKE_SONNET_RC="1", FAKE_HAIKU_OUT=FINDINGS)
	assert result["status"] == "success"
	assert result["health"] == [f"{SLOT} retryable_failure pool_exit_1"]


def test_budget_and_pr_closed_skips_record_no_health(tmp_path: Path) -> None:
	(tmp_path / "budget").mkdir()
	(tmp_path / "closed").mkdir()
	budget = _run(tmp_path / "budget", FAKE_BUDGET="200", FAKE_SONNET_OUT=FINDINGS)
	assert budget["status"] == "skipped_budget"
	assert budget["health"] == []
	pr_number = f"99{os.getpid()}"
	sentinel = Path(f"/tmp/pr_closed_sentinel_{pr_number}")
	sentinel.write_text("closed\n", encoding="utf-8")
	try:
		closed = _run(tmp_path / "closed", PR_NUMBER=pr_number, FAKE_SONNET_OUT=FINDINGS)
	finally:
		sentinel.unlink()
	assert closed["status"] == "pr_closed"
	assert closed["runs"] == []
	assert closed["health"] == []


def test_low_run_budget_skips_before_any_pool_run(tmp_path: Path) -> None:
	result = _run(tmp_path, FAKE_BUDGET="200", FAKE_SONNET_OUT=FINDINGS)
	assert result["status"] == "skipped_budget"
	assert result["runs"] == []
	assert len(result["cleanups"]) == 1


def test_pool_slot_contract_in_scripts_and_workflow() -> None:
	reviewers = REVIEWERS.read_text(encoding="utf-8")
	sandbox = SANDBOX.read_text(encoding="utf-8")
	workflow = WORKFLOW.read_text(encoding="utf-8")
	# Dispatch: anthropic/claude-* slots never reach the OpenRouter runner.
	assert 'if reviewer_is_pool_slot "${model}"; then\n      run_pool_reviewer' in reviewers
	assert "pr_closed|skipped_unmapped|skipped_open|skipped_pool)" in reviewers
	assert "skipped_unmapped|skipped_open|skipped_pool)\n        review_skip_only_statuses" in reviewers
	# The sandbox only admits PANEL_REVIEWER read-only.
	assert "SECURITY_JUDGE|PANEL_REVIEWER) ;; *) exit 2 ;; esac" in sandbox
	assert "WAVE_JUDGE|STALL_JUDGE|INTEGRATION_JUDGE|SECURITY_JUDGE|PANEL_REVIEWER)\n\t\t[ \"${claude_access}\" = read ] || exit 2 ;;" in sandbox
	# The workflow resolves the role (so the CLI and pool credential install)
	# and exposes the fallback model with a default.
	assert "ai_engine_for_role PANEL_REVIEWER" in workflow
	assert 'echo "AI_ENGINE_RESOLVED_PANEL_REVIEWER=${panel_engine}" >> "$GITHUB_ENV"' in workflow
	assert "AI_ENGINE_PANEL_REVIEWER: ${{ vars.AI_ENGINE_PANEL_REVIEWER || '' }}" in workflow
	assert "REVIEWER_POOL_FALLBACK_MODEL: ${{ vars.REVIEWER_POOL_FALLBACK_MODEL || 'claude-haiku-5-5' }}" in workflow


def _run_sole_reviewer_pass(tmp_path: Path, sole_model: str, sole_status: str, roster: str) -> dict[str, object]:
	text = REVIEWERS.read_text(encoding="utf-8")
	pass_function = text.split("run_reviewer_pass() {", 1)[1].split("# Wrap a consolidated pass-1 ledger", 1)[0]
	script = "run_reviewer_pass() {" + pass_function + textwrap.dedent("""\
		get_active_reviewer_models_text() { cat "$REVIEWER_ACTIVE_MODELS_FILE"; }
		reviewer_write_model_list_file() { printf '%s\\n' "${@:2}" > "$1"; }
		reviewer_resume_should_reuse_success_slot() { return 1; }
		reviewer_circuit_breaker_enabled() { return 1; }
		emit_run_budget_gate_note() { :; }
		codex_run_budget_phase_may_start() { return 0; }
		reviewer_request_partial_finalize() { :; }
		normalize_reviewer_model_list() { printf '%s\\n' "$1" | tr ',' '\\n'; }
		reviewer_is_pool_slot() { case "$1" in anthropic/claude-*) return 0 ;; esac; return 1; }
		run_pool_reviewer() {
		  printf 'pool %s\\n' "$1" >> "$CALLS_FILE"
		  printf '%s\\n' "$SOLE_STATUS" > "$PREVIOUS_REVIEWS_DIR/status_$3_$2.txt"
		}
		run_reviewer() {
		  printf 'openrouter %s\\n' "$1" >> "$CALLS_FILE"
		  if [ "$1" = "$SOLE_MODEL" ]; then
		    printf '%s\\n' "$SOLE_STATUS" > "$PREVIOUS_REVIEWS_DIR/status_$3_$2.txt"
		  else
		    printf 'success\\n' > "$PREVIOUS_REVIEWS_DIR/status_$3_$2.txt"
		  fi
		}
		result="$(run_reviewer_pass review "$PROMPT_FILE" high)"
		printf 'RESULT=%s\\nACTIVE=%s\\n' "$result" "$(cat "$REVIEWER_ACTIVE_MODELS_FILE")"
		""")
	active = tmp_path / "active.txt"
	active.write_text(sole_model + "\n", encoding="utf-8")
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("review\n", encoding="utf-8")
	calls = tmp_path / "calls.txt"
	proc = subprocess.run(
		["bash", "-c", "set -euo pipefail\n" + script],
		env={
			**os.environ,
			"PREVIOUS_REVIEWS_DIR": str(tmp_path),
			"REVIEWER_ACTIVE_MODELS_FILE": str(active),
			"PROMPT_FILE": str(prompt),
			"CALLS_FILE": str(calls),
			"SOLE_MODEL": sole_model,
			"SOLE_STATUS": sole_status,
			"REVIEWER_MODELS": roster,
			"PR_NUMBER": "6438",
		},
		capture_output=True,
		text=True,
		check=True,
	)
	return {
		"calls": calls.read_text(encoding="utf-8").splitlines() if calls.exists() else [],
		"stdout": proc.stdout,
		"stderr": proc.stderr,
	}


ROSTER = "minimax/minimax-m3,anthropic/claude-sonnet-5.5,z-ai/glm-5.3-flash,openai/gpt-6-luna"


def test_sole_pool_slot_skipped_is_rescued_by_gpt(tmp_path: Path) -> None:
	result = _run_sole_reviewer_pass(tmp_path, SLOT, "skipped_pool", ROSTER)
	assert result["calls"] == [f"pool {SLOT}", "openrouter openai/gpt-6-luna"]
	assert "RESULT=1" in result["stdout"]
	assert "ACTIVE=openai/gpt-6-luna" in result["stdout"]
	assert f"Sole reviewer {SLOT} was skipped or exceeded its context window" in result["stderr"]


@pytest.mark.parametrize("sole_model", ["z-ai/glm-5.3-flash", "minimax/minimax-m3"])
def test_any_skipped_sole_openrouter_slot_is_rescued(tmp_path: Path, sole_model: str) -> None:
	result = _run_sole_reviewer_pass(tmp_path, sole_model, "skipped_unmapped", ROSTER)
	assert result["calls"] == [f"openrouter {sole_model}", "openrouter openai/gpt-6-luna"]
	assert "RESULT=1" in result["stdout"]


def test_sole_gpt_slot_is_not_rescued_by_itself(tmp_path: Path) -> None:
	result = _run_sole_reviewer_pass(tmp_path, "openai/gpt-6-luna", "skipped_unmapped", ROSTER)
	assert result["calls"] == ["openrouter openai/gpt-6-luna"]
	assert "RESULT=0" in result["stdout"]


def test_pool_skip_does_not_count_as_a_hard_failure(tmp_path: Path) -> None:
	# Without gpt-6-luna on the roster there is no rescue; the skipped pool
	# slot is fail-open like skipped_unmapped (no "not counted as success" warning).
	result = _run_sole_reviewer_pass(tmp_path, SLOT, "skipped_pool", "minimax/minimax-m3,anthropic/claude-sonnet-5.5")
	assert result["calls"] == [f"pool {SLOT}"]
	assert "RESULT=0" in result["stdout"]
	assert "not counted as success" not in result["stderr"]

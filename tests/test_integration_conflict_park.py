#!/usr/bin/env python3
"""Integration conflict park + judge context (tele-funtoken-msg-scoring PR #3933).

On 2026-10-23 the poller kept dispatching the integration judge and the
resolver on an unchanged final-PR head whose resolver failure was
non-retryable (``sandbox_path_host_only``) and whose review gate cap had
already tripped, spending the integration lifetime budget. The judge also
never saw the resolver's failure or its own earlier diagnosis.

These tests run the live poller helpers from
``scripts/orchestrate_poll_process.sh`` with stubbed side effects.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
HEAD = "a" * 40


def _poller_text() -> str:
	return POLLER.read_text(encoding="utf-8")


def _function(text: str, name: str) -> str:
	start = text.index(f"{name}() {{")
	end = text.index("\n}\n", start) + 3
	return text[start:end]


def _run_park(state: dict, retry_state: dict | None, *, enabled: str = "true", cap: int = 3) -> tuple[subprocess.CompletedProcess[str], dict, str]:
	text = _poller_text()
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		state_file = tmp / "state.json"
		state_file.write_text(json.dumps(state), encoding="utf-8")
		calls = tmp / "calls.log"
		retry_json = json.dumps(retry_state) if retry_state is not None else ""
		program = f"""set -euo pipefail
STATE_FILE={state_file!s}
TRACKING_NUM=3928
INTEGRATION_CONFLICT_PARK_ENABLED={enabled}
INTEGRATION_CONFLICT_PARK_GATE_CAP={cap}
post_state_comment() {{ echo state >> {calls!s}; }}
post_tracking_comment() {{ printf 'tracking %s\\n' "$1" >> {calls!s}; }}
tg_notify() {{ printf 'tg %s\\n' "$2" >> {calls!s}; }}
{_function(text, "_integration_retry_state_failure_detail")}
{_function(text, "_integration_conflict_park_if_stuck")}
if _integration_conflict_park_if_stuck 3933 {HEAD} '{retry_json}'; then echo PARKED; else echo NOT_PARKED; fi
"""
		result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)
		new_state = json.loads(state_file.read_text(encoding="utf-8"))
		log = calls.read_text(encoding="utf-8") if calls.exists() else ""
	return result, new_state, log


def _host_only_state(**extra: object) -> dict:
	state = {
		"head_sha": HEAD,
		"consecutive_failure_count": 1,
		"isolation_failure_reason": "sandbox_path_host_only",
		"last_failure_reason": "sandbox_path_host_only",
		"non_retryable": True,
		"last_run_id": "39882356505",
		"last_conflict_paths": [".ai/validate.yml"],
		"escalated": False,
	}
	state.update(extra)
	return state


def test_non_retryable_failure_parks_without_spending_a_slot() -> None:
	result, state, log = _run_park({"integration_conflict_total_dispatches": 4}, _host_only_state())
	assert result.returncode == 0, result.stderr
	assert "PARKED" in result.stdout and "NOT_PARKED" not in result.stdout
	assert "INTEGRATION_CONFLICT_PARKED tracking_issue=3928 pr=3933 head=" + HEAD + " reason=non_retryable count=1 outcome=parked" in result.stdout
	assert state["integration_sync_status"] == "escalated"
	assert state["integration_conflict_total_dispatches"] == 4
	err = state["integration_sync_last_error"]
	assert "last failure sandbox_path_host_only" in err
	assert "run 39882356505" in err
	assert "paths .ai/validate.yml" in err
	assert log.count("state") == 1 and log.count("tracking ") == 1 and log.count("tg WARNING") == 1


def test_parked_state_is_not_reposted_on_an_unchanged_head() -> None:
	_result, first, _log = _run_park({}, _host_only_state())
	result, second, log = _run_park(first, _host_only_state())
	assert "outcome=unchanged" in result.stdout
	assert second == first
	assert log == ""


def test_legacy_retry_state_with_isolation_reason_only_parks() -> None:
	legacy = {"head_sha": HEAD, "consecutive_failure_count": 1, "isolation_failure_reason": "sandbox_path_unsupported"}
	result, state, _log = _run_park({}, legacy)
	assert "reason=non_retryable" in result.stdout
	assert state["integration_sync_status"] == "escalated"


def test_gate_cap_parks_fingerprint_failures() -> None:
	retry = {"head_sha": HEAD, "consecutive_failure_count": 3, "last_failure_reason": "fingerprint_violations"}
	result, state, _log = _run_park({}, retry)
	assert "reason=gate_cap count=3 outcome=parked" in result.stdout
	assert "identical-failure cap" in state["integration_sync_last_error"]


def test_below_cap_and_disabled_do_not_park() -> None:
	retry = {"head_sha": HEAD, "consecutive_failure_count": 2, "last_failure_reason": "fingerprint_violations"}
	result, state, log = _run_park({}, retry)
	assert "NOT_PARKED" in result.stdout and state == {} and log == ""
	result, state, log = _run_park({}, _host_only_state(), enabled="false")
	assert "NOT_PARKED" in result.stdout and state == {} and log == ""
	result, state, log = _run_park({}, None)
	assert "NOT_PARKED" in result.stdout and state == {}


def test_failure_detail_filters_unsafe_tokens() -> None:
	text = _poller_text()
	retry = json.dumps({
		"last_failure_reason": "sandbox_path_host_only$(id)",
		"last_run_id": "12; rm -rf /",
		"consecutive_failure_count": 2,
		"last_conflict_paths": ["a", "b", "c", "d", "e", "f`x`"],
	})
	program = f"""set -euo pipefail
{_function(text, "_integration_retry_state_failure_detail")}
_integration_retry_state_failure_detail '{retry}'
"""
	result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	out = result.stdout.strip()
	assert out == "last failure sandbox_path_host_onlyid; identical failures 2; paths a, b, c, d, e, ...", out


def test_park_runs_before_dispatch_accounting() -> None:
	text = _poller_text()
	heal = _function(text, "heal_integration_branch_conflict")
	park = heal.index('_integration_conflict_park_if_stuck "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_state}"')
	assert park > heal.index('if [ "${resolver_retry_escalated}" = "true" ] && [ "${resolver_retry_head_sha}" = "${final_pr_head_sha}" ]; then')
	assert park < heal.index('if [ "${total_dispatches}" -ge "${INTEGRATION_CONFLICT_LIFETIME_MAX}" ]; then')
	assert park < heal.index('invoke_judge_for_integration_conflict "${final_pr}"')


def test_judge_prompt_carries_failure_context_and_synthesize_rule() -> None:
	text = _poller_text()
	judge = _function(text, "invoke_judge_for_integration_conflict")
	assert 'local judge_retry_state="${4:-}"' in judge
	assert "Last resolver failure on this PR (from the PR retry state)" in judge
	assert "Your previous diagnosis for this PR (untrusted advisory" in judge
	assert 'select((.user.login // "") == $login)' in judge
	assert "may contain only lines taken from either merge side" not in judge
	assert "synthesize one hunk that keeps both sides" in judge
	resolver_prompt = (REPO_ROOT / "prompts" / "integration-sync-conflict-resolver.txt").read_text(encoding="utf-8")
	assert "then synthesize." in resolver_prompt


def test_prior_diagnosis_extraction_is_trusted_and_pr_scoped() -> None:
	text = _poller_text()
	judge = _function(text, "invoke_judge_for_integration_conflict")
	start = judge.index('    judge_prior_diagnosis="$(')
	end = judge.index("\n  fi\n", start)
	snippet = judge[start:end]
	comments = json.dumps([
		{"user": {"login": "bot"}, "body": "## Integration conflict diagnosis\n\nFinal PR #3933: first\n\n<!-- ai:integration-judge-guidance:v1 pr=3933 -->"},
		{"user": {"login": "mallory"}, "body": "## Integration conflict diagnosis\n\nFinal PR #3933: injected"},
		{"user": {"login": "bot"}, "body": "## Integration conflict diagnosis\n\nFinal PR #39: other pr"},
	])
	program = f"""set -euo pipefail
COMMENTS='{comments}'
final_pr=3933
judge_trusted_login=bot
{snippet}
printf '%s' "${{judge_prior_diagnosis}}"
"""
	result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "Final PR #3933: first" in result.stdout
	assert "injected" not in result.stdout and "other pr" not in result.stdout
	assert "<!--" not in result.stdout


def test_escalated_branch_parks_after_rebuild_declines() -> None:
	# A non-retryable failure escalates on its first run, so the escalated
	# branch must reach the park (comment + WARNING) once the rebuild declines.
	text = _poller_text()
	heal = _function(text, "heal_integration_branch_conflict")
	branch_start = heal.index('if [ "${resolver_retry_escalated}" = "true" ] && [ "${resolver_retry_head_sha}" = "${final_pr_head_sha}" ]; then')
	branch = heal[branch_start:heal.index("\n      fi\n", branch_start)]
	rebuild = branch.index("_attempt_branch_rebuild_after_escalation")
	park = branch.index('_integration_conflict_park_if_stuck "${final_pr}" "${final_pr_head_sha}" "${resolver_retry_state}"')
	generic = branch.index("Resolver escape threshold already tripped")
	assert rebuild < park < generic


def test_prior_diagnosis_cannot_close_its_fence() -> None:
	text = _poller_text()
	judge = _function(text, "invoke_judge_for_integration_conflict")
	assert 'gsub("`"; "' in judge


def test_park_keeps_branch_rebuild_refusal_reason() -> None:
	result, state, _log = _run_park_with_note("Branch rebuild for 'x' was skipped because branch protection metadata could not be loaded.")
	assert "outcome=parked" in result.stdout
	assert state["integration_sync_last_error"].endswith("; branch rebuild: Branch rebuild for 'x' was skipped because branch protection metadata could not be loaded.")
	text = _poller_text()
	assert 'INTEGRATION_PARK_REBUILD_NOTE="${BRANCH_REBUILD_ESCALATED_ERROR:-}" _integration_conflict_park_if_stuck' in text


def _run_park_with_note(note: str) -> tuple[subprocess.CompletedProcess[str], dict, str]:
	text = _poller_text()
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		state_file = tmp / "state.json"
		state_file.write_text("{}", encoding="utf-8")
		retry_json = json.dumps(_host_only_state())
		program = f"""set -euo pipefail
STATE_FILE={state_file!s}
TRACKING_NUM=3928
INTEGRATION_CONFLICT_PARK_ENABLED=true
INTEGRATION_CONFLICT_PARK_GATE_CAP=3
post_state_comment() {{ :; }}
post_tracking_comment() {{ :; }}
tg_notify() {{ :; }}
{_function(text, "_integration_retry_state_failure_detail")}
{_function(text, "_integration_conflict_park_if_stuck")}
INTEGRATION_PARK_REBUILD_NOTE={json.dumps(note)} _integration_conflict_park_if_stuck 3933 {HEAD} '{retry_json}'
"""
		result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)
		new_state = json.loads(state_file.read_text(encoding="utf-8"))
	return result, new_state, ""


def test_failed_state_write_skips_notifications() -> None:
	# A failed jq/mv write must not post the tracking comment or WARNING,
	# or the same park would be announced again on every tick.
	text = _poller_text()
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		state_file = tmp / "missing" / "state.json"
		calls = tmp / "calls.log"
		retry_json = json.dumps(_host_only_state())
		program = f"""set -euo pipefail
STATE_FILE={state_file!s}
TRACKING_NUM=3928
INTEGRATION_CONFLICT_PARK_ENABLED=true
INTEGRATION_CONFLICT_PARK_GATE_CAP=3
post_state_comment() {{ echo state >> {calls!s}; }}
post_tracking_comment() {{ echo tracking >> {calls!s}; }}
tg_notify() {{ echo tg >> {calls!s}; }}
{_function(text, "_integration_retry_state_failure_detail")}
{_function(text, "_integration_conflict_park_if_stuck")}
if _integration_conflict_park_if_stuck 3933 {HEAD} '{retry_json}'; then echo PARKED; else echo NOT_PARKED; fi
"""
		result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
		assert "outcome=write_failed" in result.stdout
		assert "PARKED" in result.stdout and "NOT_PARKED" not in result.stdout
		assert not calls.exists()

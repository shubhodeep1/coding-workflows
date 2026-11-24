"""Issue #9715 (follow-up of #9520): the standalone stall ``attempt_merge`` arm
records the first gate that refused the merge.

The arm is extracted from ``scripts/orchestrate_poll_process.sh`` and run with
stubbed gates, so each refusal reason, the gate order, the lease-wait branch
and the increment behaviour are checked against the real shell text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agents_doc import agents_text

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
POLLER_TEXT = POLLER.read_text(encoding="utf-8")

START_ANCHOR = "      attempt_merge)\n        local merge_pr"
STOP_ANCHOR = "      escalate_human)\n"
GOOD_SHA = "a" * 40
GATES = ["_pr_checks_completed", "_pr_base_fresh_for_merge", "_orch_squash_merge_bound"]
NOTIFY_PREFIX = "Standalone stall recovery: attempted merge retry for ready-to-merge issue"


def _arm_text() -> str:
	assert POLLER_TEXT.count(START_ANCHOR) == 1
	start = POLLER_TEXT.index(START_ANCHOR)
	assert start > POLLER_TEXT.index("run_standalone_stall_recovery() {")
	stop = POLLER_TEXT.index(STOP_ANCHOR, start)
	return POLLER_TEXT[start:stop]


SCRIPT_TEMPLATE = """set -euo pipefail
_resolve_issue_implementation_pr() {
  STALL_IMPL_PR_NUM="${STUB_PR_NUM}"
  STALL_IMPL_PR_JSON="${STUB_PR_JSON}"
  STALL_IMPL_PR_FAILURE_REASON=""
  return 0
}
gh_retry() { :; }
# These cases cover the legacy chain: STALL_MERGE_REQUIRED_CHECKS_ENABLED=false
# (#9732). The #9732 chain has its own tests in test_orchestrate_poll_process.py.
_stall_merge_required_checks_enabled() { return 1; }
_pr_checks_completed() {
  printf '_pr_checks_completed %s\\n' "$*" >> "$CALLS_FILE"
  return "${CHECKS_RC}"
}
_pr_base_fresh_for_merge() {
  printf '_pr_base_fresh_for_merge %s\\n' "$*" >> "$CALLS_FILE"
  if [ "${FRESH_LEASE_WAIT}" = true ]; then
    PR_BASE_FRESHNESS_OUTCOME="lease_wait"
    MERGE_LEASE_BLOCKER=12
  fi
  return "${FRESH_RC}"
}
_orch_squash_merge_bound() {
  printf '_orch_squash_merge_bound %s\\n' "$*" >> "$CALLS_FILE"
  ORCH_MERGE_BOUND_OUTCOME="${MERGE_OUTCOME}"
  return "${MERGE_RC}"
}
tg_notify_issue() { printf '%s\\n' "$2" >> "$NOTIFY_FILE"; }
merge_case() {
  local issue_num=7 elapsed_minutes=70 recovery_count=1 took_action=''
  local GITHUB_REPOSITORY=o/r
  for MERGE_TEST_PASS in 1; do
    case attempt_merge in
__ARM__
    esac
  done
  printf 'TOOK_ACTION=%s\\n' "${took_action}"
}
STALL_RECOVERY_SHOULD_INCREMENT=''
STALL_RECOVERY_EFFECTIVE_ACTION='attempt_merge'
merge_case
printf 'EFFECTIVE=%s\\n' "${STALL_RECOVERY_EFFECTIVE_ACTION}"
printf 'INCREMENT=%s\\n' "${STALL_RECOVERY_SHOULD_INCREMENT}"
"""


def _run(tmp_path: Path, *, pr_json: dict, checks_rc: int = 0, fresh_rc: int = 0, merge_rc: int = 0,
		lease_wait: bool = False) -> dict:
	calls_file = tmp_path / "calls.txt"
	notify_file = tmp_path / "notify.txt"
	script = SCRIPT_TEMPLATE.replace("__ARM__", _arm_text())
	env = {**os.environ, "STUB_PR_NUM": "42", "STUB_PR_JSON": json.dumps(pr_json),
		"CHECKS_RC": str(checks_rc), "FRESH_RC": str(fresh_rc), "MERGE_RC": str(merge_rc),
		"FRESH_LEASE_WAIT": "true" if lease_wait else "false", "MERGE_OUTCOME": "merged",
		"CALLS_FILE": str(calls_file), "NOTIFY_FILE": str(notify_file)}
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=env)
	assert result.returncode == 0, result.stderr + result.stdout
	values = {}
	for line in result.stdout.splitlines():
		for key in ("EFFECTIVE", "INCREMENT", "TOOK_ACTION"):
			if line.startswith(key + "="):
				values[key] = line.split("=", 1)[1]
	calls = [line.split(" ", 1)[0] for line in calls_file.read_text(encoding="utf-8").splitlines()] if calls_file.exists() else []
	notify = notify_file.read_text(encoding="utf-8") if notify_file.exists() else ""
	log_lines = [line for line in result.stdout.splitlines() if line.startswith("STANDALONE_STALL_MERGE ")]
	return {"values": values, "calls": calls, "notify": notify, "log": log_lines, "stdout": result.stdout}


def _pr(state="open", mergeable=True, sha=GOOD_SHA) -> dict:
	return {"state": state, "mergeable": mergeable, "head": {"sha": sha}, "base": {"ref": "main"}}


REFUSAL_CASES = [
	("closed", _pr(state="closed"), {}, "state_not_open", 0, GOOD_SHA),
	("mergeable_false", _pr(mergeable=False), {}, "not_mergeable", 0, GOOD_SHA),
	("mergeable_null", _pr(mergeable=None), {}, "not_mergeable", 0, GOOD_SHA),
	("short_head", _pr(sha="abc123"), {}, "bad_head_sha", 0, "invalid"),
	("checks", _pr(), {"checks_rc": 1}, "checks_incomplete", 1, GOOD_SHA),
	("freshness", _pr(), {"fresh_rc": 1}, "base_not_fresh", 2, GOOD_SHA),
	("merge_call", _pr(), {"merge_rc": 1}, "merge_call_failed", 3, GOOD_SHA),
]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required for PR JSON parsing")
@pytest.mark.parametrize(("case_id", "pr_json", "rcs", "reason", "gates_called", "head"), REFUSAL_CASES,
	ids=[case[0] for case in REFUSAL_CASES])
def test_refusal_reason_recorded(tmp_path: Path, case_id: str, pr_json: dict, rcs: dict, reason: str,
		gates_called: int, head: str) -> None:
	result = _run(tmp_path, pr_json=pr_json, **rcs)
	assert result["values"]["EFFECTIVE"] == f"attempt_merge_refused_{reason}"
	assert result["values"]["INCREMENT"] == "true"
	assert result["values"]["TOOK_ACTION"] == "true"
	assert result["log"] == [f"STANDALONE_STALL_MERGE issue=7 pr=42 head={head} outcome=refused reason={reason}"]
	assert result["notify"].startswith(NOTIFY_PREFIX)
	assert f"Merge refused: {reason}." in result["notify"]
	# Gate order and short-circuiting: only the gates up to the refusing one ran.
	assert result["calls"] == GATES[:gates_called]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required for PR JSON parsing")
def test_merge_requested_keeps_old_notify_text(tmp_path: Path) -> None:
	result = _run(tmp_path, pr_json=_pr())
	assert result["values"]["EFFECTIVE"] == "attempt_merge"
	assert result["values"]["INCREMENT"] == "true"
	assert result["calls"] == GATES
	assert result["log"] == [f"STANDALONE_STALL_MERGE issue=7 pr=42 head={GOOD_SHA} outcome=merge_requested reason=none merge_outcome=merged"]
	assert result["notify"].startswith(NOTIFY_PREFIX)
	assert "Merge refused" not in result["notify"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required for PR JSON parsing")
def test_lease_wait_does_not_increment(tmp_path: Path) -> None:
	result = _run(tmp_path, pr_json=_pr(), fresh_rc=1, lease_wait=True)
	assert result["values"]["EFFECTIVE"] == "attempt_merge_skipped_merge_lease_wait"
	assert result["values"]["INCREMENT"] in ("", "false")
	assert result["notify"] == ""
	assert result["calls"] == GATES[:2]
	assert result["log"] == [f"STANDALONE_STALL_MERGE issue=7 pr=42 head={GOOD_SHA} outcome=lease_wait reason=merge_lease_wait"]


def test_log_prefix_registered() -> None:
	assert "LOG_PREFIX.name=STANDALONE_STALL_MERGE" in agents_text()

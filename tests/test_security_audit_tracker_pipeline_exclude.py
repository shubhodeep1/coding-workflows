#!/usr/bin/env python3
"""Issue #3576: the security-audit tracker stays out of the standalone pipeline.

Covers the poller helpers in scripts/orchestrate_poll_process.sh:
`_security_audit_tracker_stall_guard` (standalone stall recovery) and
`_unrouted_blocked_candidate_numbers` (detect_unrouted_blocked_comments).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POLLER = ROOT / "scripts" / "orchestrate_poll_process.sh"
POLLER_TEXT = POLLER.read_text(encoding="utf-8")
PHASE_LABELS = ["ai:clarification", "ai:planning", "ai:awaiting-approval", "ai:implementing", "ai:done",
	"ai:ready-to-merge", "ai:review-blocked"]

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


def _function(name: str) -> str:
	start = POLLER_TEXT.index(f"{name}() {{")
	return POLLER_TEXT[start:POLLER_TEXT.index("\n}\n", start) + 3]


def _run(script: str, tmp_path: Path, env_extra: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	calls = tmp_path / "gh_calls"
	harness = '''
gh_retry() { printf '%s\\n' "$*" >> "$GH_CALLS_FILE"; [ "${GH_FAIL:-false}" != "true" ]; }
''' + script
	env = dict(os.environ, GH_CALLS_FILE=str(calls), GITHUB_REPOSITORY="o/r")
	env.pop("SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED", None)
	env.update(env_extra or {})
	result = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, check=False)
	return result, calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def _guard(tmp_path: Path, labels: list, env_extra: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	script = _function("_security_audit_tracker_stall_guard") + f'''
if _security_audit_tracker_stall_guard 3576 '{json.dumps(labels)}' '{json.dumps(PHASE_LABELS)}'; then
  echo GUARD=skip
else
  echo GUARD=continue
fi
'''
	return _run(script, tmp_path, env_extra)


def test_standalone_stall_recovery_skips_and_cleans_tracker(tmp_path: Path) -> None:
	result, calls = _guard(tmp_path, ["ai:security-audit", "ai:blocked", "ai:clarification"])
	assert result.returncode == 0, result.stderr
	assert "GUARD=skip" in result.stdout
	assert calls == ["gh issue edit 3576 --repo o/r --remove-label ai:blocked --remove-label ai:clarification"]
	assert ("STALL_RECOVERY_SKIP issue=3576 reason=security_audit_tracker removed=ai:blocked,ai:clarification "
		"outcome=cleaned action=none") in result.stdout
	assert not any("close" in c or "comment" in c or "--body" in c for c in calls)
	# {name} label objects are accepted too.
	result, _ = _guard(tmp_path / "obj", [{"name": "ai:security-audit"}, {"name": "ai:planning"}])
	assert "GUARD=skip" in result.stdout and "removed=ai:planning" in result.stdout


def test_tracker_without_phase_labels_makes_no_api_call(tmp_path: Path) -> None:
	result, calls = _guard(tmp_path, ["ai:security-audit"])
	assert "GUARD=skip" in result.stdout
	assert calls == []
	assert "removed=none outcome=clean" in result.stdout


def test_failed_label_removal_still_skips(tmp_path: Path) -> None:
	result, calls = _guard(tmp_path, ["ai:security-audit", "ai:blocked"], {"GH_FAIL": "true"})
	assert "GUARD=skip" in result.stdout
	assert len(calls) == 1
	assert "removed=none outcome=remove_failed" in result.stdout
	assert "could not remove ai:blocked" in result.stdout


def test_ordinary_issue_and_kill_switch_unaffected(tmp_path: Path) -> None:
	result, calls = _guard(tmp_path, ["ai:blocked", "ai:clarification"])
	assert "GUARD=continue" in result.stdout and calls == []
	assert "STALL_RECOVERY_SKIP" not in result.stdout
	result, calls = _guard(tmp_path / "off", ["ai:security-audit", "ai:blocked"],
		{"SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED": "false"})
	assert "GUARD=continue" in result.stdout and calls == []
	# The guard parses the switch like is_truthy, without needing it defined.
	for off_value in ("0", "no", "OFF", "FALSE"):
		result, calls = _guard(tmp_path / f"off-{off_value}", ["ai:security-audit", "ai:blocked"],
			{"SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED": off_value})
		assert "GUARD=continue" in result.stdout and calls == [], off_value


def _unrouted(tmp_path: Path, env_extra: dict[str, str] | None = None) -> list[int]:
	blocked = [
		{"number": 3576, "labels": [{"name": "ai:blocked"}, {"name": "ai:security-audit"}]},
		{"number": 40, "labels": [{"name": "ai:blocked"}, {"name": "ai:orchestrator-tracking"}]},
		{"number": 7, "labels": [{"name": "ai:blocked"}]},
	]
	script = _function("_unrouted_blocked_candidate_numbers") + f"\n_unrouted_blocked_candidate_numbers '{json.dumps(blocked)}'\n"
	result, _ = _run(script, tmp_path, env_extra)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def test_unrouted_blocked_filter_excludes_tracker(tmp_path: Path) -> None:
	assert _unrouted(tmp_path) == [7]
	assert _unrouted(tmp_path, {"SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED": "false"}) == [7, 3576]
	# The sweep-only path exits before is_truthy exists; 0/no/off still turn it off.
	for off_value in ("0", "no", "Off"):
		assert _unrouted(tmp_path, {"SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED": off_value}) == [7, 3576], off_value
	assert _unrouted(tmp_path, {"SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED": "TRUE"}) == [7]


def test_poller_wires_the_guard_and_the_switch() -> None:
	stall = _function("run_standalone_stall_recovery")
	guard = stall.index('_security_audit_tracker_stall_guard "${issue_num}" "${labels_json}" "${pipeline_labels}"')
	assert guard < stall.index("has_pipeline_label=")
	detect = _function("detect_unrouted_blocked_comments")
	assert 'numbers="$(_unrouted_blocked_candidate_numbers "${blocked}")"' in detect
	assert 'SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED="${SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED:-true}"' in POLLER_TEXT
	assert "export SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED" in POLLER_TEXT
	scan = _function("run_unblock_scan")
	assert '. == "ai:security-audit"' in scan
	# A tracker carrying only ai:blocked is discovered through the existing
	# label batch, so the guard can strip the label.
	assert '_fetch_standalone_pipeline_label_issues_graphql "${_sat_discovery_labels}"' in stall
	assert "_pls_fallback=\"$(jq -r '.[]' <<<\"${_sat_discovery_labels}\")\"" in stall

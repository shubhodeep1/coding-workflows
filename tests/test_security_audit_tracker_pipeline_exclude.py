#!/usr/bin/env python3
"""Issue #9730: keep the security-audit tracker out of the poller's pipeline handling.

The long-lived security-audit tracker (label `ai:security-audit`, body marker
`<!-- ai:security-audit-tracker:v1 -->`) is not a work item. clarify.yml
skips it, but the poller's standalone stall recovery auto-answered its
clarification and the unblock scan kept judging it. With
SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED on (the default), the poller
skips it in standalone stall recovery, the unblock scan and the
unrouted-blocked detector, and releases its block/phase labels only when the
body carries the tracker marker. It never closes the tracker or edits its body.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCAN_SCRIPT = ROOT / "scripts" / "unblock_scan.py"
POLLER = ROOT / "scripts" / "orchestrate_poll_process.sh"
POLL_WORKFLOW = ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
FLAG = "SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED"

SPEC = importlib.util.spec_from_file_location("unblock_scan_9730", SCAN_SCRIPT)
scan = importlib.util.module_from_spec(SPEC)
sys.modules["unblock_scan_9730"] = scan
SPEC.loader.exec_module(scan)

BOT = "pipeline-bot"
NOW = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc)


def _iso(hours_ago: float) -> str:
	return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(number: int, labels: list[str]) -> dict:
	return {"number": number, "labels": labels, "pull_request": False, "updated_at": _iso(5)}


def _details(label: str) -> dict:
	return {"labeled": [{"label": label, "created_at": _iso(3)}], "comments": []}


def _select(search: list[dict], details: dict, failed: list[int] | None = None) -> dict:
	return scan.select(
		search,
		details,
		[],
		failed or [],
		BOT,
		NOW,
		dt.timedelta(minutes=30),
		dt.timedelta(hours=6),
		dt.timedelta(minutes=60),
		5,
	)


def _function_text(name: str) -> str:
	lines = POLLER.read_text(encoding="utf-8").splitlines()
	start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
	end = next(i for i in range(start, len(lines)) if lines[i] == "}")
	return "\n".join(lines[start : end + 1])


def _top_level_line(prefix: str) -> str:
	return next(line for line in POLLER.read_text(encoding="utf-8").splitlines() if line.startswith(prefix))


def _prelude() -> str:
	return "\n".join(
		[
			_top_level_line("_SECURITY_AUDIT_TRACKER_EXCLUDE_WARNED="),
			_top_level_line("readonly SECURITY_AUDIT_TRACKER_RELEASE_LABELS_JSON="),
			_function_text("_security_audit_tracker_exclude_enabled"),
			_function_text("_security_audit_tracker_release_pipeline_labels"),
		]
	)


def _clean_env(flag: str | None) -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if k not in (FLAG, "BASH_ENV", "ENV", "GH_TOKEN", "GITHUB_TOKEN")}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	if flag is not None:
		env[FLAG] = flag
	return env


def _bash(tmp_path: Path, body: str, flag: str | None = None) -> tuple[subprocess.CompletedProcess, list[str]]:
	log = tmp_path / "calls.log"
	log.write_text("")
	funcs = tmp_path / "funcs.sh"
	funcs.write_text(_prelude() + "\n", encoding="utf-8")
	script = textwrap.dedent(
		f"""
		set -uo pipefail
		source "{funcs}"
		export GITHUB_REPOSITORY="owner/repo"
		gh() {{ printf '%s\\n' "gh $*" >> "{log}"; [ ! -f "{tmp_path}/gh_fails" ]; }}
		gh_retry() {{ "$@"; }}
		"""
	) + textwrap.dedent(body)
	result = subprocess.run(["bash", "-c", script], cwd=ROOT, env=_clean_env(flag), capture_output=True, text=True)
	return result, [line for line in log.read_text().splitlines() if line]


# ---------------------------------------------------------------- unblock_scan.py


def test_unblock_scan_select_skips_security_audit_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.delenv(FLAG, raising=False)
	search = [_item(3576, ["ai:security-audit", "ai:blocked"]), _item(42, ["ai:blocked"])]
	details = {"3576": _details("ai:blocked"), "42": _details("ai:blocked")}
	result = _select(search, details)
	assert result["skipped"].get("security_audit_tracker") == 1
	assert result["dispatch"] == [{"item": 42, "kind": "issue"}]


def test_unblock_scan_ordinary_issues_and_failed_projects_are_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.delenv(FLAG, raising=False)
	only_ordinary = _select([_item(42, ["ai:blocked"])], {"42": _details("ai:blocked")})
	assert only_ordinary["dispatch"] == [{"item": 42, "kind": "issue"}]
	assert "security_audit_tracker" not in only_ordinary["skipped"]
	# A failed project keeps today's project path even with the label.
	failed = _select([_item(77, ["ai:security-audit", "ai:orchestrator-tracking"])], {"77": _details("ai:orchestrator-tracking")}, failed=[77])
	assert failed["dispatch"] == [{"item": 77, "kind": "project"}]


@pytest.mark.parametrize("value", ["", "true", "TRUE", "True"])
def test_flag_on_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str) -> None:
	monkeypatch.setenv(FLAG, value)
	assert scan._security_audit_exclude_enabled() is True
	result, _ = _bash(tmp_path, "_security_audit_tracker_exclude_enabled; echo rc=$?\n", flag=value)
	assert "rc=0" in result.stdout and "::warning::" not in result.stdout, result.stderr


def test_flag_off_restores_current_behaviour(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	search = [_item(3576, ["ai:security-audit", "ai:blocked"])]
	details = {"3576": _details("ai:blocked")}
	for value in ("false", "FALSE", "bogus"):
		monkeypatch.setenv(FLAG, value)
		result = _select(search, details)
		assert result["dispatch"] == [{"item": 3576, "kind": "issue"}], value
		assert "security_audit_tracker" not in result["skipped"]
	# Shell reader: unset is on; false is off; an unknown value is off with one warning.
	unset, _ = _bash(tmp_path, "_security_audit_tracker_exclude_enabled; echo rc=$?\n", flag=None)
	assert "rc=0" in unset.stdout, unset.stderr
	off, _ = _bash(tmp_path, "_security_audit_tracker_exclude_enabled; echo rc=$?\n", flag="false")
	assert "rc=1" in off.stdout and "::warning::" not in off.stdout
	bogus, _ = _bash(tmp_path, "_security_audit_tracker_exclude_enabled; echo rc=$?\n_security_audit_tracker_exclude_enabled; echo rc=$?\n", flag="bogus")
	assert bogus.stdout.count("rc=1") == 2
	assert bogus.stdout.count(f"::warning::{FLAG}=bogus is not true/false; tracker exclusion is off") == 1


# ---------------------------------------------------------------- detect_unrouted_blocked_comments


def _run_detector(tmp_path: Path, flag: str | None) -> list[int]:
	listing = [
		{"number": 3576, "labels": [{"name": "ai:blocked"}, {"name": "ai:security-audit"}]},
		{"number": 42, "labels": [{"name": "ai:blocked"}]},
		{"number": 7, "labels": [{"name": "ai:blocked"}, {"name": "ai:orchestrator-tracking"}]},
	]
	listing_file = tmp_path / "listing.json"
	listing_file.write_text(json.dumps(listing))
	recorded = tmp_path / "numbers.json"
	funcs = tmp_path / "detector.sh"
	funcs.write_text(_prelude() + "\n" + _function_text("detect_unrouted_blocked_comments") + "\n", encoding="utf-8")
	script = textwrap.dedent(
		f"""
		set -uo pipefail
		source "{funcs}"
		export GITHUB_REPOSITORY="owner/repo"
		gh() {{
			if [ "$1 $2" = "api rate_limit" ]; then echo 5000; return 0; fi
			if [ "$1 $2" = "issue list" ]; then cat "{listing_file}"; return 0; fi
			return 1
		}}
		gh_retry() {{ "$@"; }}
		_fetch_blocked_issue_routing_graphql() {{ printf '%s' "$1" > "{recorded}"; return 1; }}
		detect_unrouted_blocked_comments
		echo "rc=$?"
		"""
	)
	result = subprocess.run(["bash", "-c", script], cwd=ROOT, env=_clean_env(flag), capture_output=True, text=True)
	assert "rc=0" in result.stdout, result.stderr
	return json.loads(recorded.read_text())


def test_unrouted_blocked_detector_excludes_tracker(tmp_path: Path) -> None:
	assert _run_detector(tmp_path, None) == [42]
	assert _run_detector(tmp_path, "true") == [42]
	assert _run_detector(tmp_path, "false") == [42, 3576]


# ---------------------------------------------------------------- release helper


def test_release_helper_strips_only_block_and_phase_labels(tmp_path: Path) -> None:
	labels = json.dumps(["ai:security-audit", "ai:blocked", "ai:clarification", "ai:planning", "ai:needs-human", "security"])
	result, calls = _bash(
		tmp_path,
		f"_security_audit_tracker_release_pipeline_labels 3576 '{labels}' true standalone_stall; echo rc=$?\n",
	)
	assert "rc=0" in result.stdout, result.stderr
	assert calls == [
		"gh issue edit 3576 --repo owner/repo --remove-label ai:blocked --remove-label ai:clarification --remove-label ai:planning"
	]
	assert (
		"STALL_RECOVERY_SKIP issue=3576 reason=security_audit_tracker source=standalone_stall "
		"removed=ai:blocked,ai:clarification,ai:planning outcome=labels_removed"
	) in result.stdout
	for line in calls:
		assert "issue close" not in line and "--body" not in line and "comments" not in line


def test_release_helper_no_call_without_labels_or_marker(tmp_path: Path) -> None:
	nothing, calls = _bash(tmp_path, "_security_audit_tracker_release_pipeline_labels 3576 '[\"ai:security-audit\"]' true unblock_scan\n")
	assert calls == []
	assert "STALL_RECOVERY_SKIP issue=3576 reason=security_audit_tracker source=unblock_scan removed=none outcome=nothing_to_remove" in nothing.stdout
	no_marker, calls = _bash(tmp_path, "_security_audit_tracker_release_pipeline_labels 3576 '[\"ai:security-audit\",\"ai:blocked\"]' false unblock_scan\n")
	assert calls == []
	assert "removed=none outcome=marker_missing" in no_marker.stdout
	bad_number, calls = _bash(tmp_path, "_security_audit_tracker_release_pipeline_labels '1;rm' '[\"ai:blocked\"]' true unblock_scan; echo rc=$?\n")
	assert calls == [] and "STALL_RECOVERY_SKIP" not in bad_number.stdout and "rc=0" in bad_number.stdout


def test_release_helper_failure_is_logged_and_fails_open(tmp_path: Path) -> None:
	(tmp_path / "gh_fails").write_text("")
	result, calls = _bash(tmp_path, "_security_audit_tracker_release_pipeline_labels 3576 '[\"ai:blocked\"]' true standalone_stall; echo rc=$?\n")
	assert len(calls) == 1 and "rc=0" in result.stdout
	assert "removed=none outcome=label_remove_failed" in result.stdout


# ---------------------------------------------------------------- placement


def test_standalone_stall_recovery_skips_tracker_before_any_recovery() -> None:
	text = _function_text("run_standalone_stall_recovery")
	managed = text.index('echo "${orchestrator_managed_set}" | grep -qxF "${issue_num}"')
	helper = text.index('_security_audit_tracker_release_pipeline_labels "${issue_num}"')
	assert managed < helper < text.index("has_pipeline_label=") < text.index("security_dependency.py")
	block = text[text.index("_security_audit_tracker_exclude_enabled", managed) : helper]
	assert '"ai:security-audit"' in block and "ai:security-audit-tracker:v1" in block
	assert "gh api" not in block


def test_unblock_scan_filters_tracker_before_selection_without_new_reads() -> None:
	text = _function_text("run_unblock_scan")
	start = text.index("_security_audit_tracker_exclude_enabled")
	assert text.index('printf \'%s\' "${runs}" > "${work_dir}/runs.json"') < start
	assert start < text.index("_scan_pr_dependency_item _scan_pr_dependency_status") < text.index("scripts/unblock_scan.py select")
	block = text[start : text.index("_scan_pr_dependency_item _scan_pr_dependency_status")]
	assert "gh api" not in block and "gh issue" not in block
	assert "reason=security_audit_tracker" in block and "reason=security_audit_filter_failed" in block
	assert '"unblock_scan"' in block


def test_poll_workflow_passes_the_switch_to_the_poller_steps() -> None:
	workflow = yaml.safe_load(POLL_WORKFLOW.read_text(encoding="utf-8"))
	steps = {step.get("name"): step for job in workflow["jobs"].values() for step in job.get("steps", [])}
	for name in (
		"Replay failed trusted reclarify commands",
		"Process each tracking issue",
		"Run standalone stall recovery without active projects",
		"Run unblock scan without active projects",
	):
		assert steps[name]["env"][FLAG] == "${{ vars.SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED || 'true' }}", name

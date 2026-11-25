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

Issue #3576: the security-audit tracker stays out of the standalone pipeline.

Covers the poller helpers in scripts/orchestrate_poll_process.sh:
`_security_audit_tracker_stall_guard` (standalone stall recovery) and
`_unrouted_blocked_candidate_numbers` (detect_unrouted_blocked_comments).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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
	# A failed project without the label keeps today's project path.
	failed = _select([_item(77, ["ai:orchestrator-tracking"])], {"77": _details("ai:orchestrator-tracking")}, failed=[77])
	assert failed["dispatch"] == [{"item": 77, "kind": "project"}]


def test_unblock_scan_skips_a_failed_project_that_is_a_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
	# The project path can give a close verdict, and the tracker is never closed.
	monkeypatch.delenv(FLAG, raising=False)
	search = [_item(77, ["ai:security-audit", "ai:orchestrator-tracking", "ai:blocked"])]
	result = _select(search, {"77": _details("ai:blocked")}, failed=[77])
	assert result["dispatch"] == []
	assert result["skipped"].get("security_audit_tracker") == 1
	monkeypatch.setenv(FLAG, "false")
	assert _select(search, {"77": _details("ai:blocked")}, failed=[77])["dispatch"] == [{"item": 77, "kind": "project"}]


def test_unblock_scan_skips_a_released_failed_tracker_on_a_later_tick(monkeypatch: pytest.MonkeyPatch) -> None:
	# Later tick: ai:blocked was released, so the search no longer returns the
	# tracker, but it is still a failed project. select rebuilds it and must
	# still skip it by its body marker, never judge it as a project.
	monkeypatch.delenv(FLAG, raising=False)
	tracker = dict(_details("ai:orchestrator-tracking"), body="<!-- ai:security-audit-tracker:v1 -->\r\n## AI Security Audit Tracker\n")
	result = _select([], {"77": tracker}, failed=[77])
	assert result["dispatch"] == []
	assert result["skipped"].get("security_audit_tracker") == 1
	# A rebuilt failed project without the marker (or the marker inline only) keeps the project path.
	inline = dict(_details("ai:orchestrator-tracking"), body="see <!-- ai:security-audit-tracker:v1 --> inline")
	assert "security_audit_tracker" not in _select([], {"77": inline}, failed=[77])["skipped"]
	assert "security_audit_tracker" not in _select([], {"77": _details("ai:orchestrator-tracking")}, failed=[77])["skipped"]
	monkeypatch.setenv(FLAG, "false")
	assert "security_audit_tracker" not in _select([], {"77": tracker}, failed=[77])["skipped"]


def test_rebuilt_failed_project_outcomes_are_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
	# A rebuilt failed project gets `since` from the newest trusted state comment
	# (it has no block label), so it can really reach the project path. This
	# proves the unmarked and inline-marker cases dispatch as a project and the
	# marked case is withheld only by the security_audit_tracker skip.
	monkeypatch.delenv(FLAG, raising=False)
	state = {"login": BOT, "body": "<!-- ORCHESTRATOR_STATE_V1 -->\n{}", "created_at": _iso(3)}

	def info(body: str) -> dict:
		return {"labeled": [], "comments": [state], "body": body}

	project = [{"item": 77, "kind": "project"}]
	assert _select([], {"77": info("Ordinary project.")}, failed=[77])["dispatch"] == project
	assert _select([], {"77": info("see <!-- ai:security-audit-tracker:v1 --> inline")}, failed=[77])["dispatch"] == project
	marked = _select([], {"77": info(TRACKER_BODY)}, failed=[77])
	assert marked["dispatch"] == [] and marked["skipped"] == {"security_audit_tracker": 1}
	# No details entry (body unknown): never dispatched, skipped before the judge.
	missing = _select([], {}, failed=[77])
	assert missing["dispatch"] == [] and "security_audit_tracker" not in missing["skipped"]
	monkeypatch.setenv(FLAG, "false")
	assert _select([], {"77": info(TRACKER_BODY)}, failed=[77])["dispatch"] == project


@pytest.mark.parametrize("value", ["", "true", "TRUE", "True"])
def test_flag_on_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str) -> None:
	monkeypatch.setenv(FLAG, value)
	assert scan._security_audit_exclude_enabled() is True
	result, _ = _bash(tmp_path, "_security_audit_tracker_exclude_enabled; echo rc=$?\n", flag=value)
	assert "rc=0" in result.stdout and "::warning::" not in result.stdout, result.stderr


def test_flag_off_restores_current_behaviour(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	search = [_item(3576, ["ai:security-audit", "ai:blocked"])]
	details = {"3576": _details("ai:blocked")}
	monkeypatch.setattr(scan, "_SECURITY_AUDIT_EXCLUDE_WARNED", False)
	for value in ("false", "FALSE", "bogus"):
		monkeypatch.setenv(FLAG, value)
		result = _select(search, details)
		assert result["dispatch"] == [{"item": 3576, "kind": "issue"}], value
		assert "security_audit_tracker" not in result["skipped"]
	# Python reader: an unknown value warns once on stderr; `false` never warns.
	_select(search, details)
	captured = capsys.readouterr()
	assert captured.err.count(f"::warning::{FLAG}=bogus is not true/false; tracker exclusion is off") == 1
	assert "::warning::" not in captured.out
	# Shell reader: unset is on; false is off; an unknown value is off with one warning.
	unset, _ = _bash(tmp_path, "_security_audit_tracker_exclude_enabled; echo rc=$?\n", flag=None)
	assert "rc=0" in unset.stdout, unset.stderr
	off, _ = _bash(tmp_path, "_security_audit_tracker_exclude_enabled; echo rc=$?\n", flag="false")
	assert "rc=1" in off.stdout and "::warning::" not in off.stdout
	bogus, _ = _bash(tmp_path, "_security_audit_tracker_exclude_enabled; echo rc=$?\n_security_audit_tracker_exclude_enabled; echo rc=$?\n", flag="bogus")
	assert bogus.stdout.count("rc=1") == 2
	assert bogus.stdout.count(f"::warning::{FLAG}=bogus is not true/false; tracker exclusion is off") == 1


def _main_flag_block() -> str:
	lines = POLLER.read_text(encoding="utf-8").splitlines()
	start = lines.index(f'{FLAG}="${{{FLAG}:-true}}"')
	end = lines.index(f"export {FLAG}", start)
	return "\n".join(lines[start : end + 1])


@pytest.mark.parametrize(
	("value", "expected", "warns"),
	[(None, "true", False), ("", "true", False), ("TRUE", "true", False), ("false", "false", False),
		("1", "false", True), ("yes", "false", True), ("on", "false", True), ("bogus", "false", True)],
)
def test_main_path_normalisation_uses_the_strict_rule(tmp_path: Path, value: str | None, expected: str, warns: bool) -> None:
	# The main flag block exports one true/false for every later reader and
	# scripts/unblock_scan.py; it must apply the #9730 rule, not is_truthy.
	block = tmp_path / "flag_block.sh"
	block.write_text(_main_flag_block() + "\n", encoding="utf-8")
	result, _ = _bash(tmp_path, f'source "{block}"\necho "value=${{{FLAG}}}"\nbash -c \'echo "child=${FLAG}"\'\n', flag=value)
	assert f"value={expected}" in result.stdout and f"child={expected}" in result.stdout, result.stdout + result.stderr
	assert result.stdout.count("::warning::") == (1 if warns else 0)
	# The sweep-only detector reads the raw value with the same rule.
	listing = json.dumps([{"number": 3576, "labels": [{"name": "ai:blocked"}, {"name": "ai:security-audit"}]}])
	funcs = tmp_path / "unrouted.sh"
	funcs.write_text(_function_text("_unrouted_blocked_candidate_numbers") + "\n", encoding="utf-8")
	detector, _ = _bash(tmp_path, f"source \"{funcs}\"\n_unrouted_blocked_candidate_numbers '{listing}'\n", flag=value)
	assert json.loads(detector.stdout.strip().splitlines()[-1]) == ([] if expected == "true" else [3576]), detector.stdout
	assert "::warning::" not in detector.stdout
	assert detector.stderr.count("::warning::") == (1 if warns else 0)


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
	funcs.write_text(
		_prelude() + "\n" + _function_text("_unrouted_blocked_candidate_numbers") + "\n"
		+ _function_text("detect_unrouted_blocked_comments") + "\n",
		encoding="utf-8",
	)
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
	# Anchor on the call: a comment above the loop also names security_dependency.py.
	assert managed < helper < text.index("has_pipeline_label=") < text.index("python3 scripts/security_dependency.py")
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
	yaml = pytest.importorskip("yaml")
	workflow = yaml.safe_load(POLL_WORKFLOW.read_text(encoding="utf-8"))
	steps = {step.get("name"): step for job in workflow["jobs"].values() for step in job.get("steps", [])}
	for name in (
		"Replay failed trusted reclarify commands",
		"Process each tracking issue",
		"Run standalone stall recovery without active projects",
		"Run unblock scan without active projects",
	):
		assert steps[name]["env"][FLAG] == "${{ vars.SECURITY_AUDIT_TRACKER_PIPELINE_EXCLUDE_ENABLED || 'true' }}", name


# ---------------------------------------------------------------- shell paths, executed

OLD = "2020-01-01T00:00:00Z"
TRACKER_BODY = "<!-- ai:security-audit-tracker:v1 -->\n## AI Security Audit Tracker\n"


def _run_unblock_scan(tmp_path: Path, flag: str | None) -> tuple[subprocess.CompletedProcess, list[str]]:
	"""Runs the real run_unblock_scan with a stubbed gh: 3576 is the tracker,
	42 an ordinary blocked issue, 77 a failed project that also carries the tracker label."""
	search = [
		{"number": 3576, "labels": ["ai:blocked", "ai:security-audit", "ai:planning"], "pull_request": False, "created_at": OLD, "updated_at": OLD},
		{"number": 42, "labels": ["ai:blocked"], "pull_request": False, "created_at": OLD, "updated_at": OLD},
		{"number": 77, "labels": ["ai:blocked", "ai:security-audit", "ai:orchestrator-tracking"], "pull_request": False, "created_at": OLD, "updated_at": OLD},
	]

	def node(body: str) -> dict:
		return {
			"body": body,
			"author": {"login": "owner"},
			"authorAssociation": "OWNER",
			"timelineItems": {"nodes": [{"createdAt": OLD, "label": {"name": "ai:blocked"}}]},
			"comments": {"totalCount": 0, "nodes": []},
		}

	graphql = {"data": {"repository": {"i3576": node(TRACKER_BODY), "i42": node("Ordinary work item."), "i77": node(TRACKER_BODY)}}}
	(tmp_path / "search.json").write_text(json.dumps(search))
	(tmp_path / "graphql.json").write_text(json.dumps(graphql))
	(tmp_path / "failed.txt").write_text("77\n")
	log = tmp_path / "calls.log"
	log.write_text("")
	funcs = tmp_path / "scan.sh"
	funcs.write_text(_prelude() + "\n" + _function_text("run_unblock_scan") + "\n", encoding="utf-8")
	script = textwrap.dedent(
		f"""
		set -uo pipefail
		source "{funcs}"
		export GITHUB_REPOSITORY="owner/repo" RUNNER_TEMP="{tmp_path}"
		UNBLOCK_FAILED_PROJECTS_FILE="{tmp_path}/failed.txt"
		NEEDS_HUMAN_PARKED_ITEMS_FILE="{tmp_path}/parked-none"
		gh() {{
			printf '%s\\n' "gh $*" >> "{log}"
			case "$1 $2" in
				"api --method") cat "{tmp_path}/search.json" ;;
				"api graphql") cat "{tmp_path}/graphql.json" ;;
				"issue edit"|"workflow run") return 0 ;;
				*) return 1 ;;
			esac
		}}
		gh_retry() {{ "$@"; }}
		_safe_gh_jq() {{ echo '[]'; }}
		needs_human_digest_prune() {{ return 0; }}
		close_orphan_fixup_chains_sweep() {{ return 0; }}
		unblock_trusted_login() {{ UNBLOCK_TRUSTED_LOGIN="{BOT}"; }}
		run_unblock_scan
		echo "rc=$?"
		"""
	)
	result = subprocess.run(["bash", "-c", script], cwd=ROOT, env=_clean_env(flag), capture_output=True, text=True)
	assert "rc=0" in result.stdout, result.stdout + result.stderr
	return result, [line for line in log.read_text().splitlines() if line.startswith(("gh issue", "gh workflow"))]


def test_run_unblock_scan_executes_tracker_skip_and_still_dispatches_ordinary_issue(tmp_path: Path) -> None:
	result, calls = _run_unblock_scan(tmp_path, None)
	assert calls == [
		"gh issue edit 3576 --repo owner/repo --remove-label ai:blocked --remove-label ai:planning",
		"gh issue edit 77 --repo owner/repo --remove-label ai:blocked",
		"gh workflow run unblock_judge_dispatch.yml --repo owner/repo -f item=42",
	], result.stdout
	assert "UNBLOCK_SCAN item=3576 kind=issue outcome=skip reason=security_audit_tracker" in result.stdout
	assert "STALL_RECOVERY_SKIP issue=3576 reason=security_audit_tracker source=unblock_scan removed=ai:blocked,ai:planning outcome=labels_removed" in result.stdout
	assert "UNBLOCK_SCAN item=42 kind=issue outcome=dispatched" in result.stdout
	# The failed project tracker has its labels released too, but stays in
	# search.json for select, which skips it: never judged.
	assert "STALL_RECOVERY_SKIP issue=77 reason=security_audit_tracker source=unblock_scan removed=ai:blocked outcome=labels_removed" in result.stdout
	assert "UNBLOCK_SCAN item=77 kind=project outcome=skip reason=security_audit_tracker" in result.stdout
	assert "item=77 kind=project outcome=dispatched" not in result.stdout
	assert '"security_audit_tracker":1' in result.stdout


def test_run_unblock_scan_flag_off_keeps_today_path(tmp_path: Path) -> None:
	result, calls = _run_unblock_scan(tmp_path, "false")
	assert not any(line.startswith("gh issue edit") for line in calls), calls
	assert "reason=security_audit_tracker" not in result.stdout and "STALL_RECOVERY_SKIP" not in result.stdout
	assert any(line.startswith("gh workflow run") for line in calls)


def _run_standalone(tmp_path: Path, flag: str | None) -> tuple[subprocess.CompletedProcess, list[str]]:
	"""Runs the real run_standalone_stall_recovery loop up to the security-dependency
	check (stubbed to hold), which an ordinary issue reaches and the tracker must not."""
	details = {
		"3576": {"labels": ["ai:security-audit", "ai:blocked", "ai:clarification"], "comments_available": True, "comments": [], "body": TRACKER_BODY, "author_login": "github-actions[bot]", "author_association": "NONE"},
		"42": {"labels": ["ai:planning"], "comments_available": True, "comments": [], "body": "Ordinary work item.", "author_login": "owner", "author_association": "OWNER"},
	}
	(tmp_path / "details.json").write_text(json.dumps(details))
	log = tmp_path / "calls.log"
	log.write_text("")
	funcs = tmp_path / "standalone.sh"
	funcs.write_text(_prelude() + "\n" + _function_text("run_standalone_stall_recovery") + "\n", encoding="utf-8")
	script = textwrap.dedent(
		f"""
		set -uo pipefail
		source "{funcs}"
		export GITHUB_REPOSITORY="owner/repo"
		RUNTIME_DIR="{tmp_path}"
		ENABLE_STANDALONE_STALL_RECOVERY="true"
		_fetch_standalone_pipeline_label_issues_graphql() {{ echo '{{"issues":[{{"number":3576}},{{"number":42}}],"fallback_labels":[]}}'; }}
		_fetch_standalone_marker_issues_graphql() {{ echo '{{}}'; }}
		build_active_issue_set() {{ echo ''; }}
		_fetch_candidate_issue_details_graphql() {{ cat "{tmp_path}/details.json"; }}
		gh() {{ printf '%s\\n' "gh $*" >> "{log}"; return 0; }}
		gh_retry() {{ "$@"; }}
		get_issue_labels_json() {{ echo '[]'; }}
		python3() {{ echo '{{"status":"held","reason":"dependency open"}}'; }}
		_post_security_dependency_held_advisory() {{ return 0; }}
		run_standalone_stall_recovery
		echo "rc=$?"
		"""
	)
	result = subprocess.run(["bash", "-c", script], cwd=ROOT, env=_clean_env(flag), capture_output=True, text=True)
	assert "rc=0" in result.stdout, result.stdout + result.stderr
	return result, [line for line in log.read_text().splitlines() if line]


def test_run_standalone_stall_recovery_skips_tracker_and_keeps_ordinary_issue(tmp_path: Path) -> None:
	result, calls = _run_standalone(tmp_path, None)
	assert calls == ["gh issue edit 3576 --repo owner/repo --remove-label ai:blocked --remove-label ai:clarification"]
	assert "STALL_RECOVERY_SKIP issue=3576 reason=security_audit_tracker source=standalone_stall removed=ai:blocked,ai:clarification outcome=labels_removed" in result.stdout
	assert "STALL_SKIP issue=3576" not in result.stdout
	assert "STALL_SKIP issue=42 reason=security_dependency_held detail=open action=none" in result.stdout


def test_run_standalone_stall_recovery_flag_off_keeps_today_path(tmp_path: Path) -> None:
	result, calls = _run_standalone(tmp_path, "false")
	assert calls == [] and "STALL_RECOVERY_SKIP" not in result.stdout
	assert "STALL_SKIP issue=3576 reason=security_dependency_held" in result.stdout
	assert "STALL_SKIP issue=42 reason=security_dependency_held" in result.stdout


# ---------------------------------------------------------------- issue #3576 helpers

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

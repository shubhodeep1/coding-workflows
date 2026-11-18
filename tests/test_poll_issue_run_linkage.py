#!/usr/bin/env python3
"""Stuck-backlog Phase 3: [issue:N] run linkage and Actions queue backpressure.

Clarify, plan and implement wrapper runs run on the default branch, so the
poller could not link a queued run to its issue through ``head_branch`` and
stall recovery re-dispatched the issue into an already jammed queue. These
tests pin:

- the wrapper ``run-name: "<name> [issue:${{ github.event.issue.number }}]"``;
- ``build_active_issue_set`` linking runs through that exact title;
- the ``QUEUE_BACKPRESSURE`` hold at the top of ``phase_cap_can_dispatch``;
- the loader keeping ``total_count`` from the existing queued read;
- the heal readers still matching these runs after the title change.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
HEAL_SCRIPT = REPO_ROOT / "scripts" / "workflow_failure_heal.py"

ACTIVE_SET_FUNCTIONS = (
	"workflow_run_cache_load",
	"workflow_run_is_implement",
	"workflow_run_is_review_family",
	"workflow_run_stall_threshold_seconds",
	"workflow_run_is_fresh",
	"build_active_issue_set",
)
DISPATCH_FUNCTIONS = (
	"phase_cap_running_for_state",
	"queue_backpressure_holds_dispatch",
	"phase_cap_can_dispatch",
)

WRAPPERS = {
	".github/workflows/internal-clarify.yml": "Internal: AI Clarify",
	".github/workflows/internal-plan.yml": "Internal: AI Plan",
	".github/workflows/internal-implement.yml": "Internal: AI Implement",
	"workflow-templates/ai-clarify.yml": "AI Clarify",
	"workflow-templates/ai-plan.yml": "AI Plan",
	"workflow-templates/ai-implement.yml": "AI Implement",
}


def _extract(names: tuple[str, ...]) -> str:
	lines = POLLER_SCRIPT.read_text(encoding="utf-8").splitlines()
	chunks = []
	for name in names:
		start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}()"))
		end = next(i for i in range(start, len(lines)) if lines[i] == "}")
		chunks.append("\n".join(lines[start:end + 1]))
	return "\n\n".join(chunks) + "\n"


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	for key in ("ACTIONS_QUEUE_BACKPRESSURE_HIGH", "QUEUED_RUN_ZOMBIE_MINUTES", "ACTIONS_QUEUED_TOTAL"):
		full_env.pop(key, None)
	if env:
		full_env.update(env)
	return subprocess.run(["bash", "-c", script], cwd=cwd, env=full_env, capture_output=True, text=True)


def _iso_minutes_ago(minutes: int) -> str:
	ts = datetime.now(timezone.utc) - timedelta(minutes=minutes)
	return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run(*, run_id: int, name: str, title: str, status: str = "queued", event: str = "issue_comment",
		branch: str = "main", age_minutes: int = 5) -> dict:
	ts = _iso_minutes_ago(age_minutes)
	return {
		"id": run_id,
		"name": name,
		"display_title": title,
		"status": status,
		"event": event,
		"head_branch": branch,
		"path": ".github/workflows/internal-plan.yml",
		"created_at": ts,
		"run_started_at": ts,
	}


def _active_set(tmp_path: Path, runs: list[dict], env: dict[str, str] | None = None) -> set[str]:
	(tmp_path / "fns.sh").write_text(_extract(ACTIVE_SET_FUNCTIONS), encoding="utf-8")
	(tmp_path / "blob.json").write_text(json.dumps({"workflow_runs": runs}), encoding="utf-8")
	script = r'''
set -euo pipefail
source ./fns.sh
_load_actions_runs_cached() { cat ./blob.json; }
STALL_THRESHOLD_MINUTES=120
REVIEW_RUN_MAX_RUNTIME_MINUTES=240
build_active_issue_set
'''
	result = _run_bash(script, tmp_path, env)
	assert result.returncode == 0, result.stderr
	return {line for line in result.stdout.splitlines() if line.strip()}


# --- [issue:N] title linkage --------------------------------------------------


def test_tagged_queued_plan_run_is_active(tmp_path: Path) -> None:
	runs = [_run(run_id=1, name="Internal: AI Plan", title="Internal: AI Plan [issue:42]")]
	assert _active_set(tmp_path, runs) == {"42"}


def test_tagged_in_progress_consumer_run_is_active(tmp_path: Path) -> None:
	runs = [_run(run_id=1, name="AI Implement", title="AI Implement [issue:7]", status="in_progress", event="issues")]
	assert _active_set(tmp_path, runs) == {"7"}


def test_tagged_branch_and_untagged_runs_union(tmp_path: Path) -> None:
	untagged = [
		_run(run_id=1, name="Review", title="Some PR", status="in_progress", event="pull_request", branch="ai/issue-7"),
		_run(run_id=2, name="Internal: AI Plan", title="Fix the widget"),
	]
	tagged = [_run(run_id=3, name="Internal: AI Implement", title="Internal: AI Implement [issue:42]")]
	# Untagged-only output is exactly what head_branch matching gives.
	assert _active_set(tmp_path, untagged) == {"7"}
	assert _active_set(tmp_path, untagged + tagged) == {"7", "42"}


def test_pull_request_run_with_forged_tag_is_ignored(tmp_path: Path) -> None:
	runs = [_run(run_id=1, name="x", title="x [issue:5]", event="pull_request", branch="feature")]
	assert _active_set(tmp_path, runs) == set()


@pytest.mark.parametrize(
	"title",
	[
		"Other [issue:5]",
		"Internal: AI Plan [issue:05]",
		"Internal: AI Plan [issue:5] extra",
		"Internal: AI Plan  [issue:5]",
		"Internal: AI Plan [issue:12345678901]",
	],
)
def test_title_not_exactly_name_plus_tag_is_ignored(tmp_path: Path, title: str) -> None:
	runs = [_run(run_id=1, name="Internal: AI Plan", title=title)]
	assert _active_set(tmp_path, runs) == set()


def test_tagged_queued_run_past_stall_cutoff_stays_active(tmp_path: Path) -> None:
	runs = [_run(run_id=1, name="Internal: AI Plan", title="Internal: AI Plan [issue:42]", age_minutes=300)]
	assert _active_set(tmp_path, runs) == {"42"}


def test_tagged_queued_run_past_zombie_window_is_inactive(tmp_path: Path) -> None:
	runs = [_run(run_id=1, name="Internal: AI Plan", title="Internal: AI Plan [issue:42]", age_minutes=1500)]
	assert _active_set(tmp_path, runs) == set()


def test_tagged_queued_zombie_window_override_and_invalid_fallback(tmp_path: Path) -> None:
	runs = [_run(run_id=1, name="Internal: AI Plan", title="Internal: AI Plan [issue:42]", age_minutes=300)]
	assert _active_set(tmp_path, runs, {"QUEUED_RUN_ZOMBIE_MINUTES": "200"}) == set()
	# Not a positive integer: falls back to STALL_THRESHOLD_MINUTES (120).
	assert _active_set(tmp_path, runs, {"QUEUED_RUN_ZOMBIE_MINUTES": "abc"}) == set()


def test_untagged_queued_run_past_stall_cutoff_still_excluded(tmp_path: Path) -> None:
	runs = [_run(run_id=1, name="Internal: AI Implement", title="Fix", branch="ai/issue-9", age_minutes=300)]
	assert _active_set(tmp_path, runs) == set()


def test_tagged_in_progress_run_past_stall_cutoff_is_inactive(tmp_path: Path) -> None:
	runs = [_run(run_id=1, name="Internal: AI Plan", title="Internal: AI Plan [issue:42]", status="in_progress",
		age_minutes=300)]
	assert _active_set(tmp_path, runs) == set()


# --- queue backpressure ------------------------------------------------------


def _dispatch(tmp_path: Path, action: str, *, total: str | None, high: str | None = None,
		caps_enabled: bool = False) -> subprocess.CompletedProcess[str]:
	(tmp_path / "dispatch.sh").write_text(_extract(DISPATCH_FUNCTIONS), encoding="utf-8")
	lines = ["set -euo pipefail", "source ./dispatch.sh"]
	if total is not None:
		lines.append(f"ACTIONS_QUEUED_TOTAL='{total}'")
	if high is not None:
		lines.append(f"ACTIONS_QUEUE_BACKPRESSURE_HIGH='{high}'")
	if caps_enabled:
		lines.append("PHASE_CAPS_ENABLED='true'")
		lines.append("PHASE_CAPS_STATUS='ok'")
		lines.append("""PHASE_CAPS_MAX_BY_STATE='{"ai:planning":0}'""")
		lines.append("""PHASE_CAPS_RUNNING_BY_STATE='{"ai:planning":0}'""")
	lines.append("rc=0")
	lines.append(f"phase_cap_can_dispatch 'ai:planning' '{action}' '42' || rc=$?")
	lines.append('echo "rc=${rc}"')
	return _run_bash("\n".join(lines) + "\n", tmp_path)


def test_queue_at_threshold_holds_dispatch(tmp_path: Path) -> None:
	result = _dispatch(tmp_path, "retrigger_plan", total="150")
	assert result.returncode == 0, result.stderr
	assert "rc=1" in result.stdout
	assert ("QUEUE_BACKPRESSURE outcome=held state=ai:planning action=retrigger_plan issue=42 queued=150"
		in result.stdout)
	assert "QUEUE_BACKPRESSURE outcome=on queued=150 high=150" in result.stderr


def test_queue_below_threshold_keeps_legacy_result(tmp_path: Path) -> None:
	result = _dispatch(tmp_path, "retrigger_plan", total="149")
	assert result.returncode == 0, result.stderr
	assert "rc=0" in result.stdout
	assert "outcome=held" not in result.stdout
	assert "QUEUE_BACKPRESSURE outcome=off queued=149 high=150" in result.stderr
	# Legacy phase caps still apply below the threshold.
	capped = _dispatch(tmp_path, "retrigger_plan", total="149", caps_enabled=True)
	assert "rc=1" in capped.stdout
	assert "phase_capped state=ai:planning action=retrigger_plan issue=42" in capped.stdout


@pytest.mark.parametrize("action", ["attempt_merge", "escalate_human", "skip"])
def test_exempt_actions_are_never_held(tmp_path: Path, action: str) -> None:
	result = _dispatch(tmp_path, action, total="500")
	assert result.returncode == 0, result.stderr
	assert "rc=0" in result.stdout
	assert "QUEUE_BACKPRESSURE" not in result.stdout + result.stderr


@pytest.mark.parametrize(
	("total", "high", "marker"),
	[
		("500", "-1", None),
		("500", "0", None),
		("500", "abc", "::warning::QUEUE_BACKPRESSURE outcome=off reason=invalid_threshold"),
		("", None, "QUEUE_BACKPRESSURE outcome=unknown queued=unknown high=150"),
		(None, None, "QUEUE_BACKPRESSURE outcome=unknown queued=unknown high=150"),
	],
)
def test_disabled_or_unknown_keeps_legacy_result(tmp_path: Path, total: str | None, high: str | None,
		marker: str | None) -> None:
	result = _dispatch(tmp_path, "retrigger_plan", total=total, high=high)
	assert result.returncode == 0, result.stderr
	assert "rc=0" in result.stdout
	assert "outcome=held" not in result.stdout
	if marker:
		assert marker in result.stderr


def test_custom_threshold(tmp_path: Path) -> None:
	result = _dispatch(tmp_path, "create_issue", total="20", high="20")
	assert "rc=1" in result.stdout
	assert "QUEUE_BACKPRESSURE outcome=held" in result.stdout


# --- loader keeps total_count --------------------------------------------------


def _loader_script(queued_payload: str) -> str:
	return (
		"set -euo pipefail\n"
		"source ./loader.sh\n"
		"ACTIONS_RUNS_CACHE_TTL_SECONDS=60\n"
		"GITHUB_REPOSITORY=octo/example\n"
		"gh_retry() { \"$@\"; }\n"
		"gh() { printf 'HTTP/2.0 200 OK\\r\\n\\r\\n{\"total_count\":1,\"workflow_runs\":[{\"id\":1,\"status\":\"in_progress\"}]}\\n'; }\n"
		"_safe_gh_jq() {\n"
		"  case \"$1\" in\n"
		f"    *status=queued*) printf '%s' '{queued_payload}' ;;\n"
		"    *) printf '%s' '[]' ;;\n"
		"  esac\n"
		"}\n"
		"_load_actions_runs_cached >blob.out\n"
		"echo \"total=${ACTIONS_QUEUED_TOTAL}\"\n"
		"jq -c '[.workflow_runs[].id]' blob.out\n"
	)


def test_loader_records_queued_total_from_existing_read(tmp_path: Path) -> None:
	text = _extract(("_load_actions_runs_cached",))
	assert "status=queued&per_page=50" in text
	assert "--jq '{total_count: .total_count, workflow_runs: .workflow_runs}'" in text
	assert "declare -g ACTIONS_QUEUED_TOTAL=''" in POLLER_SCRIPT.read_text(encoding="utf-8")
	(tmp_path / "loader.sh").write_text("declare -g _ACTIONS_RUNS_BLOB_READY='false'\ndeclare -g ACTIONS_QUEUED_TOTAL=''\n" + text,
		encoding="utf-8")
	result = _run_bash(_loader_script('{"total_count":321,"workflow_runs":[{"id":2,"status":"queued"}]}'), tmp_path)
	assert result.returncode == 0, result.stderr
	assert "total=321" in result.stdout
	assert "[1,2]" in result.stdout
	# A bare array (old shape or a stub) keeps the runs and leaves the depth unknown.
	legacy = _run_bash(_loader_script('[{"id":3,"status":"queued"}]'), tmp_path)
	assert legacy.returncode == 0, legacy.stderr
	assert "total=\n" in legacy.stdout
	assert "[1,3]" in legacy.stdout


# --- wrapper run-names -------------------------------------------------------------


@pytest.mark.parametrize(("rel_path", "name"), sorted(WRAPPERS.items()))
def test_wrapper_run_name_carries_issue_tag(rel_path: str, name: str) -> None:
	text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
	names = re.findall(r"(?m)^name: (.+)$", text)
	assert len(names) == 1
	assert names[0].strip().strip('"') == name
	run_names = re.findall(r"(?m)^run-name: (.+)$", text)
	assert run_names == [
		"\"${{ startsWith(github.event.issue.title, '[E2E ') && github.event.issue.title"
		f" || format('{name} [issue:{{0}}]', github.event.issue.number) }}}}\""
	]


def test_release_gate_smoke_titles_keep_legacy_run_title() -> None:
	# The stable gate finds smoke clarify/plan/implement runs by
	# display_title == issue title, so the wrappers keep the issue title for
	# "[E2E " issues. Every smoke issue title the gate creates must carry it.
	gate = (REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml").read_text(encoding="utf-8")
	titles = re.findall(r'(?m)^\s*TITLE="([^"]*)"', gate)
	assert titles
	assert all(title.startswith("[E2E ") for title in titles), titles


# --- heal readers keep matching wrapper runs -----------------------------------------


@pytest.fixture(scope="module")
def heal():
	spec = importlib.util.spec_from_file_location("heal_issue_run_linkage", HEAL_SCRIPT)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def _heal_run(title: str, *, name: str = "Internal: AI Plan", event: str = "issue_comment") -> dict:
	return {
		"id": 11,
		"name": name,
		"display_title": title,
		"event": event,
		"repository": {"full_name": "octo/example"},
		"conclusion": "failure",
		"created_at": "2026-11-18T10:00:00Z",
		"html_url": "https://github.com/octo/example/actions/runs/11",
	}


def test_heal_issue_linked_run_matches(heal) -> None:
	match = heal._issue_linked_run_matches
	kwargs = {"title": "Fix the widget", "wanted": "octo/example"}
	assert match(_heal_run("Fix the widget"), **kwargs)
	assert match(_heal_run("Fix the widget"), number=42, **kwargs)
	assert match(_heal_run("Internal: AI Plan [issue:42]"), number=42, **kwargs)
	assert not match(_heal_run("Internal: AI Plan [issue:42]"), **kwargs)
	assert not match(_heal_run("Internal: AI Plan [issue:43]"), number=42, **kwargs)
	assert not match(_heal_run("x [issue:42]", name="x", event="pull_request"), number=42, **kwargs)


def test_heal_select_failed_runs_accepts_tagged_title(heal) -> None:
	runs = [
		_heal_run("Internal: AI Implement [issue:42]", name="Internal: AI Implement"),
		_heal_run("Internal: AI Implement [issue:43]", name="Internal: AI Implement"),
		_heal_run("x [issue:42]", name="x", event="pull_request"),
	]
	picked = heal.select_failed_runs(runs, title="Fix the widget", tracking_issue=42)
	assert [item["workflow_name"] for item in picked] == ["Internal: AI Implement"]
	assert heal.select_failed_runs(runs, title="Fix the widget") == []
	assert len(heal.select_failed_runs([_heal_run("Fix the widget")], title="Fix the widget")) == 1


def test_heal_trend_issue_linked_runs_accepts_tagged_title(heal) -> None:
	issues = [(42, {"number": 42, "title": "Fix the widget"})]
	pages = [{"workflow_runs": [
		_heal_run("Fix the widget"),
		_heal_run("Internal: AI Plan [issue:42]"),
		_heal_run("Internal: AI Plan [issue:43]"),
		_heal_run("x [issue:42]", name="x", event="pull_request"),
	]}]
	out = heal._trend_issue_linked_runs(issues, pages, repo="octo/example")
	assert [run["display_title"] for run in out] == ["Fix the widget", "Internal: AI Plan [issue:42]"]

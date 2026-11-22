#!/usr/bin/env python3
"""Tests for the blocked-merge guard (issue #9939).

`_pr_sync_merge_blocked_guard` (scripts/pr_checks_lib.sh) runs right before
every synchronous (non --auto) `gh pr merge` in scripts/review_rb_judge.sh and
scripts/orchestrate_poll_process.sh. The pipeline merges with the owner's
admin GH_PAT, so a direct merge of a PR GitHub reports as
mergeable_state=blocked is a ruleset bypass: every merge on main from
2026-11-21 12:23 UTC landed that way, because the required `lint` came only
from a workflow_dispatch final CI that GitHub never evaluates for a PR.

These tests pin the decision table, the API cost, the log line, the kill
switch, and (statically) that every direct merge site calls the guard first.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
HEAD = "a" * 40
GUARD_CALL_RE = re.compile(r"_pr_sync_merge_blocked_guard|rb_judge_sync_merge_guard")


def _pr(state: object = "clean", **extra: object) -> dict:
	pr: dict = {"number": 42, "head": {"sha": HEAD}, "base": {"ref": "main"}}
	if state is not _MISSING:
		pr["mergeable_state"] = state
	pr.update(extra)
	return pr


_MISSING = object()


def _run(
	args: str,
	*,
	tmp_path: Path,
	pr_json: str = "{}",
	pr_fail: bool = False,
	rules_json: str = "[]",
	statuses_json: str = '{"statuses": []}',
	runs_json: str = '[{"check_runs": []}]',
	env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	if shutil.which("jq") is None:
		pytest.skip("jq binary not available in test environment")
	calls_log = tmp_path / "gh_calls.log"
	preamble = f"""
set -uo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
  printf '%s\\n' "$*" >> {str(calls_log)!r}
  case "$*" in
    *"/pulls/"*)
      if [ "${{PR_FAIL:-0}}" = "1" ]; then return 1; fi
      printf '%s' "${{PR_JSON}}"
      ;;
    *"/rules/branches/"*) printf '%s' "${{RULES_JSON}}" ;;
    *"/status"*) printf '%s' "${{STATUSES_JSON}}" ;;
    *"/check-runs"*) printf '%s' "${{RUNS_JSON}}" ;;
    *) return 1 ;;
  esac
}}
source {str(LIB)!r}
"""
	body = (
		f"_pr_sync_merge_blocked_guard {args}; rc=$?\n"
		'printf "RC=%s\\n" "${rc}"\n'
		'printf "STATE=%s\\n" "${SYNC_MERGE_BLOCKED_GUARD_STATE:-}"\n'
		'printf "BLOCKERS=%s\\n" "${SYNC_MERGE_BLOCKED_GUARD_BLOCKERS:-}"\n'
	)
	full_env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"GITHUB_REPOSITORY": "o/r",
		"PR_JSON": pr_json,
		"PR_FAIL": "1" if pr_fail else "0",
		"RULES_JSON": rules_json,
		"STATUSES_JSON": statuses_json,
		"RUNS_JSON": runs_json,
	}
	full_env.update(env or {})
	proc = subprocess.run(["bash", "-c", preamble + body], env=full_env, text=True, capture_output=True, timeout=60)
	calls = calls_log.read_text(encoding="utf-8").splitlines() if calls_log.exists() else []
	return proc, calls


def _field(proc: subprocess.CompletedProcess[str], name: str) -> str:
	for line in proc.stdout.splitlines():
		if line.startswith(f"{name}="):
			return line[len(name) + 1:]
	raise AssertionError(f"{name} missing from {proc.stdout!r}")


def _guard_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
	return [line for line in proc.stdout.splitlines() if line.startswith("SYNC_MERGE_BLOCKED_GUARD ")]


def _quoted(value: dict | str) -> str:
	text = value if isinstance(value, str) else json.dumps(value)
	return "'" + text.replace("'", "'\\''") + "'"


# --- decision table -----------------------------------------------------------


@pytest.mark.parametrize("state", ["clean", "unstable", "has_hooks", "behind", "dirty", "draft"])
def test_mergeable_states_proceed_silently_with_no_api_call(tmp_path: Path, state: str) -> None:
	proc, calls = _run(_quoted(_pr(state)), tmp_path=tmp_path)
	assert proc.returncode == 0, proc.stderr
	assert _field(proc, "RC") == "0"
	assert _guard_lines(proc) == []
	assert calls == []


def test_blocked_defers_and_names_the_ruleset_blockers(tmp_path: Path) -> None:
	rules = json.dumps([{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ci/final"}, {"context": "ai-review/head-gate"}]}}])
	statuses = json.dumps({"statuses": [{"context": "ai-review/head-gate", "state": "success"}]})
	proc, calls = _run(_quoted(_pr("blocked")), tmp_path=tmp_path, rules_json=rules, statuses_json=statuses)
	assert proc.returncode == 0, proc.stderr
	assert _field(proc, "RC") == "1"
	assert _field(proc, "STATE") == "blocked"
	assert _field(proc, "BLOCKERS") == "ci/final (missing)"
	assert _guard_lines(proc) == [
		f"SYNC_MERGE_BLOCKED_GUARD outcome=deferred pr=42 head={HEAD} state=blocked blockers=ci/final (missing)"
	]
	assert any("/rules/branches/main" in call for call in calls), calls
	assert not any("/pulls/" in call for call in calls), calls


def test_blocked_without_a_named_blocker_still_defers(tmp_path: Path) -> None:
	# The dispatched `lint` check-run made the ruleset helper report no
	# blocker although GitHub said blocked; the guard must not trust that.
	rules = json.dumps([{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "lint"}]}}])
	runs = json.dumps([{"check_runs": [{"id": 1, "name": "lint", "status": "completed", "conclusion": "success"}]}])
	proc, _calls = _run(_quoted(_pr("blocked")), tmp_path=tmp_path, rules_json=rules, runs_json=runs)
	assert _field(proc, "RC") == "1"
	assert _field(proc, "BLOCKERS") == "unknown"


def test_blocked_with_a_hint_makes_no_ruleset_call(tmp_path: Path) -> None:
	proc, calls = _run(_quoted(_pr("blocked")) + " main 'lint (pending)'", tmp_path=tmp_path)
	assert _field(proc, "RC") == "1"
	assert calls == []
	assert _guard_lines(proc) == [
		f"SYNC_MERGE_BLOCKED_GUARD outcome=deferred pr=42 head={HEAD} state=blocked blockers=lint (pending)"
	]


@pytest.mark.parametrize(
	("state", "logged"),
	[("unknown", "unknown"), (None, "none"), (_MISSING, "none"), ("", "none")],
)
def test_unknown_or_missing_state_defers_without_extra_calls(tmp_path: Path, state: object, logged: str) -> None:
	proc, calls = _run(_quoted(_pr(state)), tmp_path=tmp_path)
	assert _field(proc, "RC") == "1"
	assert calls == []
	assert _guard_lines(proc) == [
		f"SYNC_MERGE_BLOCKED_GUARD outcome=deferred pr=42 head={HEAD} state={logged} blockers=unknown"
	]


@pytest.mark.parametrize("value", ["false", "0", "OFF", "No"])
def test_disabled_proceeds_without_api_calls(tmp_path: Path, value: str) -> None:
	proc, calls = _run("42", tmp_path=tmp_path, env={"PR_SYNC_MERGE_BLOCKED_GUARD_ENABLED": value})
	assert _field(proc, "RC") == "0"
	assert calls == []
	assert _guard_lines(proc) == ["SYNC_MERGE_BLOCKED_GUARD outcome=disabled pr=42"]


@pytest.mark.parametrize("value", ["true", "1", "garbage", ""])
def test_any_other_switch_value_keeps_the_guard_on(tmp_path: Path, value: str) -> None:
	proc, _calls = _run(_quoted(_pr("blocked")) + " main hint", tmp_path=tmp_path, env={"PR_SYNC_MERGE_BLOCKED_GUARD_ENABLED": value})
	assert _field(proc, "RC") == "1"


# --- PR-number input ---------------------------------------------------------


def test_pr_number_makes_exactly_one_get(tmp_path: Path) -> None:
	proc, calls = _run("42", tmp_path=tmp_path, pr_json=json.dumps(_pr("clean")))
	assert _field(proc, "RC") == "0"
	assert calls == ["repos/o/r/pulls/42"]


def test_pr_number_uses_pr_checks_repository(tmp_path: Path) -> None:
	_proc, calls = _run("42", tmp_path=tmp_path, pr_json=json.dumps(_pr("clean")), env={"PR_CHECKS_REPOSITORY": "x/y"})
	assert calls == ["repos/x/y/pulls/42"]


def test_unreadable_pr_defers(tmp_path: Path) -> None:
	proc, calls = _run("42", tmp_path=tmp_path, pr_fail=True)
	assert _field(proc, "RC") == "1"
	assert calls == ["repos/o/r/pulls/42"]
	assert _guard_lines(proc) == [
		"SYNC_MERGE_BLOCKED_GUARD outcome=deferred pr=42 head=unknown state=unreadable blockers=unknown"
	]


@pytest.mark.parametrize("arg", ["''", "'not json'", "'{broken'", "'[]'"])
def test_garbage_input_defers(tmp_path: Path, arg: str) -> None:
	proc, calls = _run(arg, tmp_path=tmp_path)
	assert _field(proc, "RC") == "1"
	assert _field(proc, "STATE") == "unreadable"
	assert calls == []


# --- wiring: every direct merge is guarded -------------------------------------


GUARD_WINDOW_LINES = 30


def _direct_merge_lines(path: Path) -> list[int]:
	lines = path.read_text(encoding="utf-8").splitlines()
	hits = []
	for index, line in enumerate(lines):
		stripped = line.lstrip()
		if stripped.startswith("#") or "gh pr merge" not in line:
			continue
		if "--auto" in line or "--disable-auto" in line:
			continue
		if stripped.startswith("echo ") or stripped.startswith('_noop_gate_failure_alert'):
			continue
		hits.append(index)
	return hits


@pytest.mark.parametrize("path", [JUDGE, POLLER], ids=["review_rb_judge", "orchestrate_poll_process"])
def test_every_direct_merge_is_preceded_by_the_guard(path: Path) -> None:
	lines = path.read_text(encoding="utf-8").splitlines()
	hits = _direct_merge_lines(path)
	assert hits, f"no direct gh pr merge found in {path.name}"
	for index in hits:
		window = lines[max(0, index - GUARD_WINDOW_LINES):index + 1]
		code = [line for line in window if not line.lstrip().startswith("#")]
		assert any(GUARD_CALL_RE.search(line) for line in code), (
			f"{path.name}:{index + 1} merges directly without the blocked-merge guard: {lines[index].strip()}"
		)


def test_direct_merge_site_counts_are_pinned() -> None:
	# A new direct merge site must be guarded and counted here.
	assert len(_direct_merge_lines(JUDGE)) == 4
	assert len(_direct_merge_lines(POLLER)) == 3


def test_judge_wrapper_fails_closed_without_the_library() -> None:
	text = JUDGE.read_text(encoding="utf-8")
	start = text.index("rb_judge_sync_merge_guard() {")
	end = text.index("\n}\n", start)
	body = text[start:end]
	assert "declare -F _pr_sync_merge_blocked_guard" in body
	assert 'RB_SYNC_MERGE_DEFERRED="true"' in body
	assert 'PR_CHECKS_REPOSITORY="${REPOSITORY}" _pr_sync_merge_blocked_guard' in body


def test_judge_retry_returns_at_once_after_a_deferral() -> None:
	text = JUDGE.read_text(encoding="utf-8")
	start = text.index("rb_judge_merge_retry_transient() {")
	loop = text.index("for _rb_retry_delay in 10 20 40; do", start)
	assert '[ "${RB_SYNC_MERGE_DEFERRED:-false}" != "true" ] || return 1' in text[start:loop]


def test_poller_squash_merge_bound_defers_before_the_direct_merge() -> None:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index("_orch_squash_merge_bound()\n{")
	end = text.index("\n}\n", start)
	body = text[start:end]
	guard = body.index('_pr_sync_merge_blocked_guard "${pr}"')
	direct = body.index('gh_retry gh pr merge "${pr}" --repo "${GITHUB_REPOSITORY}" --squash --match-head-commit')
	auto = body.index("--squash --auto --match-head-commit")
	assert auto < guard < direct
	assert 'ORCH_MERGE_BOUND_OUTCOME="deferred"' in body[guard:direct]


@pytest.mark.parametrize(
	"workflow",
	[".github/workflows/review_autofix.yml", ".github/workflows/orchestrate_poll.yml"],
)
def test_workflows_forward_the_kill_switch(workflow: str) -> None:
	text = (REPO_ROOT / workflow).read_text(encoding="utf-8")
	assert "PR_SYNC_MERGE_BLOCKED_GUARD_ENABLED: ${{ vars.PR_SYNC_MERGE_BLOCKED_GUARD_ENABLED || 'true' }}" in text


def test_readme_documents_the_kill_switch() -> None:
	text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
	assert "| `PR_SYNC_MERGE_BLOCKED_GUARD_ENABLED` | No | `true` |" in text

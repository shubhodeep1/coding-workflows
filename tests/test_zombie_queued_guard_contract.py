#!/usr/bin/env python3
"""Issue #9473: every active-run guard ignores wedged queued runs.

A queued run older than ZOMBIE_RUN_MAX_QUEUED_HOURS (default 24) has zero
jobs and GitHub refuses to cancel or delete it (run 43095142301: cancel 409,
force-cancel 409, DELETE 403). This contract locks the guard sites that use
the shared filter, the kill-switch wiring, the documented exclusions and the
log-prefix registration.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"

# file -> guard labels its ZOMBIE_RUN_IGNORED lines carry.
GUARD_SITES = {
	"scripts/gh_helpers.sh": ["autofix_inflight_peer"],
	"scripts/orchestrate_poll_process.sh": [
		"poll_validate_active",
		"stall_inflight_direct_check",
		"build_active_issue_set",
		"retrigger_review_inflight",
		"std_retrigger_review_inflight",
		"conflict_dispatch_active_autofix",
	],
	"extracted/poller_stall.sh": ["build_active_issue_set"],
	"scripts/review_merge_train.sh": ["merge_train_release"],
	"scripts/auto_release_stable.sh": ["auto_release_gate", "auto_release_promote", "auto_release_legacy"],
	"scripts/promote_main_cycle.sh": ["promote_cycle_self", "promote_cycle_gate_idle"],
	"scripts/apply_analysis_on_main.sh": ["apply_analysis_orchestrate_in_flight"],
	"scripts/worktree_registry.sh": ["worktree_registry_active_runs"],
	".github/workflows/test-and-mark-stable.yml": ["stable_gate_plan_runs"],
}
PY_GUARD_SITES = {
	"scripts/unblock_scan.py": ["unblock_scan_busy", "unblock_scan_active_judge"],
	"scripts/ci_cancelled_rerun.py": ["ci_cancelled_rerun"],
}
WIRED_WORKFLOWS = [
	"orchestrate_poll.yml",
	"review_autofix.yml",
	"test-and-mark-stable.yml",
	"workflow-log-analysis.yml",
	"auto-release-stable.yml",
	"promote-main-to-stable.yml",
	"review_autofix_sweep.yml",
]
WIRING = "ZOMBIE_RUN_MAX_QUEUED_HOURS: ${{ vars.ZOMBIE_RUN_MAX_QUEUED_HOURS || '24' }}"


def _text(rel: str) -> str:
	return (REPO_ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel,guards", sorted(GUARD_SITES.items()))
def test_shell_guard_sites_use_the_shared_filter(rel: str, guards: list[str]) -> None:
	text = _text(rel)
	assert "select(is_wedged_queued($zq_cutoff) | not)" in text or "is_wedged_queued($zq_cutoff)" in text, rel
	for guard in guards:
		assert re.search(rf"zombie_queued_log_ignored {re.escape(guard)}\b", text), (rel, guard)


@pytest.mark.parametrize("rel,guards", sorted(PY_GUARD_SITES.items()))
def test_python_guard_sites_use_the_shared_predicate(rel: str, guards: list[str]) -> None:
	text = _text(rel)
	assert 'Path(__file__).with_name("orchestrate_lib.py")' in text, rel
	assert "is_wedged_queued_run" in text and "log_zombie_run_ignored" in text, rel
	for guard in guards:
		assert f'"{guard}"' in text, (rel, guard)


def test_sweep_keeps_is_stale_queued_and_adds_the_floor() -> None:
	text = _text(".github/workflows/review_autofix_sweep.yml")
	assert "def is_stale_queued:" in text
	assert "SWEEP_STALE_QUEUED_MINUTES: ${{ vars.SWEEP_STALE_QUEUED_MINUTES || '120' }}" in text
	assert 'sweep_zq_cutoff="$(zombie_queued_cutoff_epoch 2>/dev/null || echo 0)"' in text
	assert "guard=review_autofix_sweep" in text


@pytest.mark.parametrize("workflow", WIRED_WORKFLOWS)
def test_kill_switch_is_wired(workflow: str) -> None:
	assert WIRING in _text(f".github/workflows/{workflow}")


@pytest.mark.parametrize("rel", ["scripts/workflow_failure_heal.py", ".github/workflows/cancel_on_pr_close.yml"])
def test_documented_exclusions_stay_unchanged(rel: str) -> None:
	text = _text(rel)
	assert "is_wedged_queued" not in text
	assert "ZOMBIE_RUN_MAX_QUEUED_HOURS" not in text


def test_log_prefix_is_registered_in_the_agents_fragment() -> None:
	text = _text("agents.d/9473-ignore-wedged-queued-runs.md")
	assert '<!-- agents: section="Stable log prefixes (contractual)" -->' in text
	assert "- `ZOMBIE_RUN_IGNORED`" in text
	assert "LOG_PREFIX.name=ZOMBIE_RUN_IGNORED" in text
	assert '<!-- agents: section="Workflow architecture" -->' in text


def test_extracted_mirror_matches_the_poller() -> None:
	poller = _text("scripts/orchestrate_poll_process.sh")
	mirror = _text("extracted/poller_stall.sh")
	start = poller.index("build_active_issue_set() {\n")
	body = poller[start:poller.index("\n}\n", start) + 3]
	assert body in mirror
	assert "zombie_queued_jq_def()" in mirror


# Single-line jq programs that use the filter. Each must compile against the
# shared definitions (a compile error would read as "no active run").
_PROGRAM_RE = re.compile(r"'(\[[^'\n]*is_wedged_queued\(\$zq_cutoff\)[^'\n]*)'")


def _programs() -> list[tuple[str, str]]:
	found = []
	for rel in sorted(GUARD_SITES):
		for match in _PROGRAM_RE.finditer(_text(rel)):
			found.append((rel, match.group(1)))
	return found


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")
def test_guard_jq_programs_compile_with_the_shared_definitions() -> None:
	programs = _programs()
	assert len(programs) >= 12, programs
	defs = subprocess.run(["bash", "-c", f'source "{GH_HELPERS}"; zombie_queued_jq_def'],
		capture_output=True, text=True, check=True).stdout
	for rel, program in programs:
		result = subprocess.run(
			["jq", "-n", "--argjson", "zq_cutoff", "1", "--arg", "me", "1", "--arg", "title", "t",
			 f"{defs}\nif false then ({program}) else empty end"],
			capture_output=True, text=True,
		)
		assert result.returncode == 0, (rel, program, result.stderr)

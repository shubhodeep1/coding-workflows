#!/usr/bin/env python3
"""Stall recovery does not re-run the review-blocked judge on an unchanged verdict.

Issue #9991: 18 open PRs had a judge `merge` decision on their current head
whose merge was deferred (busy lease, blocked, checks pending), and stall
recovery re-dispatched the judge on the same head again and again (#9573 x21,
#9637 x20). `_dispatch_rb_judge_for_pr` now returns 4 when the head's
`ai-review/rb-judge-verdict` status shows nothing relevant changed.

The functions are extracted from the shipped poller and run with
scripts/pr_checks_lib.sh sourced and `gh` calls stubbed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
PR_CHECKS_LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
HEAD = "a" * 40
BASE = "b" * 40

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


def _extract(name: str) -> str:
	lines = POLLER.read_text(encoding="utf-8").splitlines()
	start = next(i for i, line in enumerate(lines) if re.match(rf"^{re.escape(name)}\(\)", line))
	end = next(i for i in range(start, len(lines)) if lines[i] == "}")
	return "\n".join(lines[start:end + 1]) + "\n"


def _pr_json(**overrides: object) -> dict:
	pr = {
		"number": 77,
		"state": "open",
		"head": {"sha": HEAD, "ref": "ai/issue-7"},
		"base": {"sha": BASE, "ref": "main"},
		"labels": [{"name": "ai:review-blocked"}, {"name": "ai:merge-lease-wait"}],
		"auto_merge": None,
		"mergeable_state": "blocked",
	}
	pr.update(overrides)
	return pr


def _verdict(wait: str = "lease_wait", ms: str = "blocked", base: str = BASE) -> str:
	return f"decision=merge wait={wait} base={base} ms={ms}"


def _run(tmp_path: Path, pr_json: dict | None, statuses: list[dict] | None, *, freshness: str = "clean",
		env: str = "", status_fails: bool = False, pr_number: int = 77) -> tuple[subprocess.CompletedProcess, list[str]]:
	calls = tmp_path / "calls.log"
	calls.write_text("", encoding="utf-8")
	tracker = tmp_path / "tracker"
	tracker.write_text("", encoding="utf-8")
	status_body = json.dumps({"statuses": statuses or []})
	script = f'''
set -euo pipefail
source "{PR_CHECKS_LIB}"
{_extract("_stall_rb_judge_verdict_unchanged")}
{_extract("_dispatch_rb_judge_for_pr")}
GITHUB_REPOSITORY=o/r
_CONFLICT_DISPATCH_TRACKER="{tracker}"
STALL_IMPL_PR_JSON={json.dumps(json.dumps(pr_json) if pr_json is not None else "")}
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{ echo "safe_gh_jq $*" >> "{calls}"; {"return 1" if status_fails else f"printf '%s' {json.dumps(status_body)}"}; }}
gh() {{ echo "gh $*" >> "{calls}"; return 0; }}
phase_cap_can_dispatch() {{ return 0; }}
phase_cap_note_dispatch() {{ :; }}
_pr_base_freshness() {{ echo "freshness $*" >> "{calls}"; PR_BASE_FRESHNESS_OUTCOME="{freshness}"; echo "{freshness}"; }}
{env}
rc=0
_dispatch_rb_judge_for_pr {pr_number} 7 || rc=$?
echo "RC=$rc WAIT=${{STALL_RB_VERDICT_WAIT}}"
'''
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
	return result, calls.read_text(encoding="utf-8").splitlines()


def _status(desc: str) -> list[dict]:
	return [{"context": "ai-review/head-gate", "state": "pending", "description": "x"},
		{"context": "ai-review/rb-judge-verdict", "state": "success", "description": desc}]


def _dispatched(calls: list[str]) -> bool:
	return any(line.startswith("gh workflow run review_rb_judge_dispatch.yml") for line in calls)


def test_unchanged_verdict_returns_4_and_does_not_dispatch(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, _pr_json(), _status(_verdict()))
	assert result.returncode == 0, result.stderr
	assert "RC=4 WAIT=lease_wait" in result.stdout
	assert "verdict unchanged (wait=lease_wait); not dispatching." in result.stdout
	assert not _dispatched(calls)
	# Exactly one status read, no freshness reads (the base did not move).
	assert [c for c in calls if c.startswith("safe_gh_jq")] == [f"safe_gh_jq repos/o/r/commits/{HEAD}/status?per_page=100"]
	assert not any(c.startswith("freshness") for c in calls)


def test_new_head_has_no_verdict_and_is_dispatched(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, _pr_json(head={"sha": "c" * 40, "ref": "x"}), [])
	assert "RC=0" in result.stdout
	assert _dispatched(calls)


def test_lease_label_removed_dispatches(tmp_path: Path) -> None:
	pr = _pr_json(labels=[{"name": "ai:review-blocked"}])
	# Still pre-filtered in via mergeable_state=blocked.
	result, calls = _run(tmp_path, pr, _status(_verdict()))
	assert "RC=0" in result.stdout
	assert "verdict changed (lease)" in result.stdout
	assert _dispatched(calls)


def test_base_moved_with_overlap_dispatches_and_clean_still_skips(tmp_path: Path) -> None:
	pr = _pr_json(base={"sha": "d" * 40, "ref": "main"})
	result, calls = _run(tmp_path, pr, _status(_verdict()), freshness="overlap")
	assert "RC=0" in result.stdout and "verdict changed (base)" in result.stdout
	assert _dispatched(calls)
	result, calls = _run(tmp_path, pr, _status(_verdict()), freshness="clean")
	assert "RC=4" in result.stdout
	assert not _dispatched(calls)
	result, calls = _run(tmp_path, pr, _status(_verdict()), freshness="unknown")
	assert "RC=0" in result.stdout and "base_unknown" in result.stdout


def test_merge_state_change_dispatches(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, _pr_json(mergeable_state="clean", labels=[{"name": "ai:merge-lease-wait"}]),
		_status(_verdict()))
	assert "RC=0" in result.stdout and "verdict changed (checks)" in result.stdout
	assert _dispatched(calls)


def test_status_read_failure_dispatches(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, _pr_json(), None, status_fails=True)
	assert "RC=0" in result.stdout
	assert _dispatched(calls)


def test_switch_off_dispatches_without_a_read(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, _pr_json(), _status(_verdict()), env="VERDICT_WAIT_DISPATCH_SKIP_ENABLED=false")
	assert "RC=0" in result.stdout
	assert _dispatched(calls)
	assert not any(c.startswith("safe_gh_jq") for c in calls)


@pytest.mark.parametrize("pr_json, number", [
	(_pr_json(labels=[], mergeable_state="clean"), 77),
	(_pr_json(state="closed"), 77),
	(_pr_json(), 78),
	(None, 77),
])
def test_pre_filter_makes_no_call(tmp_path: Path, pr_json: dict | None, number: int) -> None:
	result, calls = _run(tmp_path, pr_json, _status(_verdict()), pr_number=number)
	assert "RC=0" in result.stdout
	assert not any(c.startswith("safe_gh_jq") for c in calls)
	assert _dispatched(calls)


def test_callers_treat_rc_4_as_a_benign_skip() -> None:
	text = POLLER.read_text(encoding="utf-8")
	assert text.count("echo \"STALL_SKIP issue=${issue_num} reason=rb_judge_verdict_unchanged") == 3
	assert text.count('STALL_RECOVERY_EFFECTIVE_ACTION="dispatch_rb_judge_skipped_verdict_unchanged"') == 2
	standalone = text[text.find('elif [ "${_std_rb_rc}" -eq 4 ]; then'):]
	assert standalone.splitlines()[2].strip() == 'STALL_RECOVERY_SHOULD_INCREMENT="false"'
	for rc_var in ("_skip_redirect_rc", "rb_dispatch_rc"):
		block = text[text.find(f'if [ "${{{rc_var}}}" -eq 4 ]; then'):]
		block = block[:block.find("fi")]
		assert "STALL_RECOVERY_SHOULD_INCREMENT" not in block
		assert "return 0" in block


def test_orchestrate_poll_passes_the_switch() -> None:
	wf = (REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
	assert wf.count("VERDICT_WAIT_DISPATCH_SKIP_ENABLED: ${{ vars.VERDICT_WAIT_DISPATCH_SKIP_ENABLED || 'true' }}") == 2

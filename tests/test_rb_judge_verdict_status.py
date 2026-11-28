#!/usr/bin/env python3
"""The review-blocked judge's head-bound deferral status records the base
(issue #9991, re-issued as #10243).

`_pr_judge_merge_pending_mark` (scripts/pr_checks_lib.sh, #9987) posts
`ai-review/judge-merge-pending` when the judge decides to merge but waits for
the merge lease. Stall recovery re-ran the judge on such heads again and again
(#9573 x21 on 2026-11-23). With the decision-time base in the description,
stall recovery re-runs it only when the base moved under the PR's files.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
HEAD = "a" * 40
BASE = "b" * 40

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


def _lib(tmp_path: Path, body: str, env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess, list[str]]:
	log = tmp_path / "calls.log"
	script = f"""
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{ printf '%s\\n' "$*" >> {str(log)!r}; printf '{{}}'; }}
source {str(LIB)!r}
set +e
{body}
"""
	full_env = {"PATH": os.environ["PATH"], "PR_CHECKS_REPOSITORY": "o/r", **(env or {})}
	result = subprocess.run(["bash", "-c", script], env=full_env, capture_output=True, text=True, check=False)
	calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
	return result, calls


def test_base_sha_is_appended_to_the_description(tmp_path: Path) -> None:
	result, calls = _lib(tmp_path, f"_pr_judge_merge_pending_mark 7 {HEAD} merge {BASE}; echo rc=$?")
	assert "rc=0" in result.stdout, result.stderr
	assert f"JUDGE_MERGE_PENDING pr=7 head={HEAD} decision=merge outcome=marked reason=lease_wait" in result.stdout
	assert len(calls) == 1
	description = f"Review-blocked judge decided merge; waiting for the merge lease; base={BASE}"
	assert f"description={description} " in calls[0] + " "
	assert f"repos/o/r/statuses/{HEAD}" in calls[0]


@pytest.mark.parametrize("decision", ["merge", "merge_with_followup", "terminal"])
def test_description_stays_under_the_github_limit(tmp_path: Path, decision: str) -> None:
	result, calls = _lib(tmp_path, f"_pr_judge_merge_pending_mark 7 {HEAD} {decision} {BASE}")
	assert len(calls) == 1, result.stderr
	description = calls[0].split("description=", 1)[1].split(" -f ", 1)[0].strip()
	assert description.endswith(f"; base={BASE}")
	assert len(description) <= 140


@pytest.mark.parametrize("base", ["", "not-a-sha", "B" * 40, "b" * 39])
def test_missing_or_invalid_base_keeps_the_old_text(tmp_path: Path, base: str) -> None:
	result, calls = _lib(tmp_path, f"_pr_judge_merge_pending_mark 7 {HEAD} merge '{base}'")
	assert len(calls) == 1, result.stderr
	assert "description=Review-blocked judge decided merge; waiting for the merge lease" in calls[0]
	assert "base=" not in calls[0]


def test_judge_passes_the_freshness_base_tip() -> None:
	text = JUDGE.read_text(encoding="utf-8")
	start = text.index("# BEGIN rb-judge judge-decision lease mark (#9987)")
	end = text.index("# END rb-judge judge-decision lease mark (#9987)")
	block = text[start:end]
	assert '_pr_judge_merge_pending_mark "${PR_NUMBER}" "${RB_JUDGED_HEAD_SHA:-}" "${1:-}" "${PR_BASE_FRESHNESS_BASE_SHA:-}"' in block


def test_judge_mark_end_to_end_records_the_base(tmp_path: Path) -> None:
	text = JUDGE.read_text(encoding="utf-8")
	start = text.index("# BEGIN rb-judge judge-decision lease mark (#9987)")
	end = text.index("# END rb-judge judge-decision lease mark (#9987)")
	block_file = tmp_path / "block.sh"
	block_file.write_text(text[start:end], encoding="utf-8")
	body = f"source {str(block_file)!r}; rb_judge_mark_merge_pending merge; rb_judge_mark_merge_pending merge"
	env = {"PR_BASE_FRESHNESS_OUTCOME": "lease_wait", "PR_BASE_FRESHNESS_BASE_SHA": BASE, "REPOSITORY": "o/r",
		"PR_NUMBER": "7", "RB_JUDGED_HEAD_SHA": HEAD}
	result, calls = _lib(tmp_path, body, env)
	assert len(calls) == 1, result.stdout + result.stderr
	assert f"; base={BASE}" in calls[0]

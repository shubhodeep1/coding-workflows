"""Contract tests: the review-blocked judge acts only on the head it judged.

BUG-001 (analysis/workflow-optimization-2026-09-18.md). ``scripts/review_rb_judge.sh``
embeds ``RB_JUDGED_HEAD_SHA`` in the judge prompt and, after the judge runs,
re-reads the PR in its merged-PR guard (``_guard_pr_meta``). The live
``.head.sha`` from that same response must match the judged head:

- a moved head skips every action (``judge_skip_reason=stale_head``) with one
  ``RB_JUDGE_STALE_HEAD`` log line and no comment, label, status or close;
- an unreadable head refuses ``fix`` / ``close_and_reissue`` fail-closed
  (``judge_skip_reason=head_unverified``), while merge actions proceed because
  ``--match-head-commit`` already binds them;
- the ``close_and_reissue`` spot-fix baseline uses the judged SHA, not a live
  ``gh pr view --json headRefOid`` re-read.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "review_rb_judge.sh"

START_ANCHOR = '_guard_pr_meta="$(gh_retry _safe_gh_jq'
END_ANCHOR = 'RB_SECURITY_ORIGINAL_ACTION=""'

JUDGED = "a" * 40
OTHER = "b" * 40

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the extracted guard")


def _script_text() -> str:
	return SCRIPT.read_text(encoding="utf-8")


def _extract_guard_block() -> str:
	text = _script_text()
	start = text.find(START_ANCHOR)
	assert start != -1, f"anchor not found in {SCRIPT}: {START_ANCHOR}"
	end = text.find(END_ANCHOR, start)
	assert end != -1, f"anchor not found after guard in {SCRIPT}: {END_ANCHOR}"
	return text[start:end]


def _run_guard(
	action: str,
	pr_payload: object | None,
	*,
	judged_head: str = JUDGED,
	fetch_fails: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str, list[list[str]]]:
	block = _extract_guard_block()
	with tempfile.TemporaryDirectory(prefix="test_rb_judge_head_binding_") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		gh_log = tmp / "gh_calls.jsonl"
		gh_shim = bin_dir / "gh"
		gh_shim.write_text(
			"#!/usr/bin/env python3\n"
			"import json, os, sys\n"
			"with open(os.environ['GH_CALL_LOG'], 'a', encoding='utf-8') as fh:\n"
			"\tfh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
			"sys.exit(0)\n",
			encoding="utf-8",
		)
		gh_shim.chmod(0o755)
		fixture = tmp / "pr.json"
		if isinstance(pr_payload, str):
			fixture.write_text(pr_payload, encoding="utf-8")
		else:
			fixture.write_text(json.dumps(pr_payload if pr_payload is not None else {}), encoding="utf-8")
		github_output = tmp / "github_output.txt"
		github_output.write_text("", encoding="utf-8")
		harness = tmp / "harness.sh"
		harness.write_text(
			"set -euo pipefail\n"
			'gh_retry() { "$@"; }\n'
			'_safe_gh_jq() { if [ "${FIXTURE_FAIL:-0}" = "1" ]; then return 1; fi; cat "${PR_FIXTURE}"; }\n'
			'PR_ALREADY_MERGED="${PR_ALREADY_MERGED:-false}"\n'
			+ block
			+ "\necho PROCEEDED\n",
			encoding="utf-8",
		)
		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"GH_CALL_LOG": str(gh_log),
			"PR_FIXTURE": str(fixture),
			"FIXTURE_FAIL": "1" if fetch_fails else "0",
			"GITHUB_OUTPUT": str(github_output),
			"PR_NUMBER": "42",
			"REPOSITORY": "owner/repo",
			"RB_ACTION": action,
			"RB_JUDGED_HEAD_SHA": judged_head,
		}
		proc = subprocess.run(
			["bash", str(harness)],
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)
		output = github_output.read_text(encoding="utf-8")
		calls: list[list[str]] = []
		if gh_log.exists():
			calls = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines() if line.strip()]
		return proc, output, calls


def _open_pr(head_sha: str | None) -> dict:
	payload: dict = {"number": 42, "state": "open", "merged": False, "merged_at": None}
	if head_sha is not None:
		payload["head"] = {"sha": head_sha}
	return payload


@needs_jq
@pytest.mark.parametrize("action", ["fix", "close_and_reissue", "merge", "merge_with_followup"])
@pytest.mark.parametrize("live_head", [JUDGED, JUDGED.upper()])
def test_matching_live_head_proceeds(action: str, live_head: str) -> None:
	proc, output, calls = _run_guard(action, _open_pr(live_head))
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" in proc.stdout, proc.stdout + proc.stderr
	assert "judge_skip_reason" not in output
	assert "RB_JUDGE_STALE_HEAD" not in proc.stdout
	assert calls == []


@needs_jq
@pytest.mark.parametrize("action", ["close_and_reissue", "fix", "merge", "merge_with_followup"])
def test_moved_live_head_skips_with_stale_head(action: str) -> None:
	proc, output, calls = _run_guard(action, _open_pr(OTHER))
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" not in proc.stdout
	assert "judge_action=skip" in output.splitlines()
	assert "judge_skip_reason=stale_head" in output.splitlines()
	expected = f"RB_JUDGE_STALE_HEAD pr=42 judged_head={JUDGED} live_head={OTHER} action={action}"
	assert expected in proc.stdout.splitlines(), proc.stdout
	# No close, label, comment or status call before the skip.
	assert calls == [], calls


@needs_jq
def test_invalid_judged_head_with_valid_live_head_is_stale() -> None:
	proc, output, calls = _run_guard("fix", _open_pr(OTHER), judged_head="")
	assert "PROCEEDED" not in proc.stdout
	assert "judge_skip_reason=stale_head" in output.splitlines()
	assert calls == []


UNREADABLE_PAYLOADS = [
	pytest.param({}, False, id="empty_object"),
	pytest.param(_open_pr(None), False, id="no_head"),
	pytest.param(_open_pr("not-a-sha"), False, id="malformed_sha"),
	pytest.param(_open_pr("a" * 39), False, id="short_sha"),
	pytest.param(None, True, id="fetch_failed"),
]


@needs_jq
@pytest.mark.parametrize("action", ["close_and_reissue", "fix"])
@pytest.mark.parametrize("payload,fetch_fails", UNREADABLE_PAYLOADS)
def test_unreadable_head_refuses_fix_and_reissue(action: str, payload: object, fetch_fails: bool) -> None:
	proc, output, calls = _run_guard(action, payload, fetch_fails=fetch_fails)
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" not in proc.stdout
	assert "judge_action=skip" in output.splitlines()
	assert "judge_skip_reason=head_unverified" in output.splitlines()
	assert "RB_JUDGE_STALE_HEAD" not in proc.stdout
	assert calls == []


@needs_jq
@pytest.mark.parametrize("action", ["merge", "merge_with_followup"])
@pytest.mark.parametrize("payload,fetch_fails", UNREADABLE_PAYLOADS)
def test_unreadable_head_lets_bound_merge_actions_proceed(action: str, payload: object, fetch_fails: bool) -> None:
	proc, output, _calls = _run_guard(action, payload, fetch_fails=fetch_fails)
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" in proc.stdout
	assert "judge_skip_reason" not in output


@needs_jq
def test_merged_pr_with_moved_head_keeps_existing_behaviour() -> None:
	merged = {"number": 42, "merged": True, "merged_at": "2026-10-14T00:00:00Z", "head": {"sha": OTHER}}
	proc, output, _calls = _run_guard("merge", merged)
	assert "PROCEEDED" in proc.stdout
	assert "judge_skip_reason" not in output
	proc, output, _calls = _run_guard("fix", merged)
	assert "PROCEEDED" not in proc.stdout
	assert "judge_skip_reason=merged_pr_unsafe_action" in output.splitlines()
	assert "stale_head" not in output


def test_reissue_branch_uses_judged_head_not_live_lookup() -> None:
	text = _script_text()
	start = text.find("\n  close_and_reissue)")
	assert start != -1
	branch = text[start:]
	assert "--json headRefOid" not in branch
	assert 'RB_HEAD_SHA="$(printf \'%s\' "${RB_JUDGED_HEAD_SHA:-}" | tr \'[:upper:]\' \'[:lower:]\')"' in branch
	assert "head_ref_unavailable" in branch
	assert "head_ref_not_in_checkout" in branch


def test_new_skip_reasons_are_written_once() -> None:
	text = _script_text()
	assert text.count("judge_skip_reason=stale_head") == 1
	assert text.count('echo "judge_skip_reason=head_unverified"') == 1


def _load_label_propagation_module():
	prev = sys.dont_write_bytecode
	sys.dont_write_bytecode = True
	try:
		module_path = REPO_ROOT / "tests" / "test_review_rb_judge_label_propagation.py"
		spec = importlib.util.spec_from_file_location("_rb_label_propagation_for_head_binding", module_path)
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	finally:
		sys.dont_write_bytecode = prev


@needs_jq
@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_spot_fix_baseline_uses_judged_head_even_when_live_head_differs() -> None:
	module = _load_label_propagation_module()
	state = module._run_close_and_reissue(
		["ai:orchestrator-managed"],
		judge_payload={
			"action": "close_and_reissue",
			"reissue_mode": "spot-fix",
			"justification": "Only a small follow-up is needed.",
			"remaining_issues": [
				{"file": "src/app.py", "line_start": 1, "line_end": 1, "symptom": "Guard missing"},
			],
			"new_issue": {"title": "Reissue: follow-up", "body": "Patch the grounded gap."},
		},
		reissue_preserve_baseline_enabled="1",
		repo_files={"src/app.py": "print('hello')\n"},
		pr_view_head_ref_oid=OTHER,
		judged_head_sha="__REPO_HEAD__",
	)
	judged = state["_repo_head_before"]
	assert judged and judged != OTHER
	assert f"head_sha={judged}" in state["_stdout"], state["_stdout"]
	assert "REISSUE_BASELINE_PRESERVED" in state["_stdout"]
	assert f"ai/reissue-baseline/pr-42-{judged[:12]}-" in state["_stdout"]
	assert not state.get("pr_view_args"), state.get("pr_view_args")

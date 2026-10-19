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
import re
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
SEVERITY_END_ANCHOR = "# Post judge assessment to PR"

JUDGED = "a" * 40
OTHER = "b" * 40

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the extracted guard")


def _script_text() -> str:
	return SCRIPT.read_text(encoding="utf-8")


def _extract_guard_block(end_anchor: str = END_ANCHOR) -> str:
	text = _script_text()
	start = text.find(START_ANCHOR)
	assert start != -1, f"anchor not found in {SCRIPT}: {START_ANCHOR}"
	end = text.find(end_anchor, start)
	assert end != -1, f"anchor not found after guard in {SCRIPT}: {end_anchor}"
	return text[start:end]


def _run_guard(
	action: str,
	pr_payload: object | None,
	*,
	judged_head: str = JUDGED,
	fetch_fails: bool = False,
	severity_decision: str | None = None,
	cwd: Path | None = None,
	initial_head: str = "",
) -> tuple[subprocess.CompletedProcess[str], str, list[list[str]]]:
	"""Run the extracted guard. ``severity_decision`` also runs the security
	severity block with ``rb_security_severity_block`` stubbed to print it;
	``cwd`` runs the harness inside a git repository (ancestry checks);
	``initial_head`` sets ``INITIAL_HEAD_SHA`` (the head the run checked out)."""
	block = _extract_guard_block(SEVERITY_END_ANCHOR if severity_decision is not None else END_ANCHOR)
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
			'rb_security_severity_block() { printf \'%s\\n\' "${SEVERITY_STUB:-allow}"; }\n'
			'rb_security_block_hold() { echo HOLD; }\n'
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
			"INITIAL_HEAD_SHA": initial_head,
			"IS_FINAL": "false",
			"SEVERITY_STUB": severity_decision or "allow",
			"RB_SECURITY_BLOCKING_ISSUES": "#1",
			"ISSUE_NUMBERS": "7",
		}
		proc = subprocess.run(
			["bash", str(harness)],
			env=env,
			cwd=str(cwd) if cwd is not None else None,
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
	# Once in the guard, once in the security convert_fix arm.
	assert text.count('echo "judge_skip_reason=head_unverified"') == 2


@needs_jq
@pytest.mark.parametrize("action", ["merge", "merge_with_followup"])
@pytest.mark.parametrize("payload,fetch_fails", UNREADABLE_PAYLOADS)
def test_security_converted_fix_refused_when_head_unreadable(action: str, payload: object, fetch_fails: bool) -> None:
	proc, output, calls = _run_guard(action, payload, fetch_fails=fetch_fails, severity_decision="convert_fix")
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" not in proc.stdout
	assert "judge_action=skip" in output.splitlines()
	assert "judge_skip_reason=head_unverified" in output.splitlines()
	assert calls == []


@needs_jq
@pytest.mark.parametrize("action", ["merge", "merge_with_followup"])
def test_security_converted_fix_proceeds_on_matching_head(action: str) -> None:
	proc, output, _calls = _run_guard(action, _open_pr(JUDGED), severity_decision="convert_fix")
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" in proc.stdout
	assert "judge_skip_reason" not in output


def _git(args: list[str], cwd: Path) -> str:
	env = {
		"PATH": os.environ.get("PATH", ""),
		"HOME": str(cwd),
		"GIT_AUTHOR_NAME": "t",
		"GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t",
		"GIT_COMMITTER_EMAIL": "t@example.invalid",
	}
	return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, text=True, capture_output=True).stdout.strip()


@needs_jq
@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
@pytest.mark.parametrize("action", ["fix", "close_and_reissue", "merge"])
def test_live_head_ancestor_of_judged_head_proceeds_and_descendant_is_stale(action: str) -> None:
	# The judged head is the local checkout HEAD; an unpushed local commit
	# (editor or [ai-merge-resolve]) puts it ahead of the live head. When the
	# live head is still the head the run checked out (INITIAL_HEAD_SHA) that
	# is not a concurrent push, so the judge proceeds. A live head outside the
	# judged history (here: the judged head's descendant) is stale.
	with tempfile.TemporaryDirectory(prefix="test_rb_judge_head_ancestry_") as td:
		repo = Path(td)
		_git(["init", "-q"], repo)
		_git(["commit", "-q", "--allow-empty", "-m", "live"], repo)
		live = _git(["rev-parse", "HEAD"], repo)
		_git(["commit", "-q", "--allow-empty", "-m", "local"], repo)
		judged = _git(["rev-parse", "HEAD"], repo)
		proc, output, calls = _run_guard(action, _open_pr(live), judged_head=judged, cwd=repo, initial_head=live)
		assert "PROCEEDED" in proc.stdout, proc.stdout + proc.stderr
		assert "judge_skip_reason" not in output
		assert calls == []
		proc, output, calls = _run_guard(action, _open_pr(judged), judged_head=live, cwd=repo, initial_head=live)
		assert "PROCEEDED" not in proc.stdout
		assert "judge_skip_reason=stale_head" in output.splitlines()
		assert calls == []


@needs_jq
@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
@pytest.mark.parametrize("action", ["fix", "close_and_reissue", "merge"])
def test_force_push_back_to_older_ancestor_is_stale(action: str) -> None:
	# A force-push back to an older commit during the judge call also leaves
	# the live head an ancestor of the judged head. It is not the head the run
	# checked out, so the judge must skip; an unset INITIAL_HEAD_SHA fails
	# closed the same way.
	with tempfile.TemporaryDirectory(prefix="test_rb_judge_head_force_back_") as td:
		repo = Path(td)
		_git(["init", "-q"], repo)
		_git(["commit", "-q", "--allow-empty", "-m", "older"], repo)
		older = _git(["rev-parse", "HEAD"], repo)
		_git(["commit", "-q", "--allow-empty", "-m", "initial"], repo)
		initial = _git(["rev-parse", "HEAD"], repo)
		_git(["commit", "-q", "--allow-empty", "-m", "local"], repo)
		judged = _git(["rev-parse", "HEAD"], repo)
		for initial_head in (initial, ""):
			proc, output, calls = _run_guard(action, _open_pr(older), judged_head=judged, cwd=repo, initial_head=initial_head)
			assert "PROCEEDED" not in proc.stdout, proc.stdout + proc.stderr
			assert "judge_skip_reason=stale_head" in output.splitlines()
			assert calls == []
		proc, output, _calls = _run_guard(action, _open_pr(initial), judged_head=judged, cwd=repo, initial_head=initial.upper())
		assert "PROCEEDED" in proc.stdout, proc.stdout + proc.stderr


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


@needs_jq
@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_spot_fix_baseline_uses_live_head_when_judged_head_is_unpushed_local_commit() -> None:
	# The stale-head guard lets a run proceed when the live head is its own
	# checked-out head behind an unpushed local commit (RB_JUDGED_HEAD_SHA).
	# The closed PR never carries that local commit, and implement.yml ignores
	# a baseline that differs from the closed PR's headRefOid, so the baseline
	# is the live PR head (RB_LIVE_HEAD_SHA) instead.
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
		judged_head_sha=OTHER,
		extra_env={"RB_LIVE_HEAD_SHA": "__REPO_HEAD__"},
	)
	live = state["_repo_head_before"]
	assert live and live != OTHER
	assert f"head_sha={live}" in state["_stdout"], state["_stdout"]
	assert f"ai/reissue-baseline/pr-42-{live[:12]}-" in state["_stdout"]
	assert OTHER[:12] not in state["_stdout"]
	assert not state.get("pr_view_args"), state.get("pr_view_args")


# --- #7659: merge failures report gh's stderr ------------------------------


MERGE_ERR_BEGIN = "# BEGIN rb-judge merge error capture (#7659)"
MERGE_ERR_END = "# END rb-judge merge error capture (#7659)"
MERGE_SITE_ENDS = {
	"merge": '            RB_MERGE_STATUS="merge_failed"\n          fi\n',
	"terminal": '            RB_MERGE_STATUS="merge_failed"\n          fi\n',
	"sync": '              echo "judge_skip_reason=sync_merge_failed" >> "$GITHUB_OUTPUT"\n            fi\n',
}
MERGE_SITE_ORDER = ("merge", "terminal", "sync")
DRAFT_MESSAGE = "Pull request #42 is still a draft"
OLD_WARNINGS = {
	"merge": f"::warning::Review-blocked judge merge failed for evaluated head {JUDGED}; withholding ai:ready-to-merge from linked issues.",
	"terminal": f"::warning::Review-blocked judge terminal merge failed for evaluated head {JUDGED}; withholding ai:ready-to-merge from linked issues.",
	"sync": "::warning::PR #42 sync merge failed despite passing check-runs (typically: branch protection rules / merge queue / permissions / 422 / concurrent push changing HEAD). Leaving linked issues in ai:review-blocked — stall recovery will re-fire the judge.",
}


def _merge_err_helpers() -> str:
	text = _script_text()
	start = text.index(MERGE_ERR_BEGIN)
	end = text.index(MERGE_ERR_END, start)
	return text[start:end]


def _merge_site_snippet(path: str) -> str:
	text = _script_text()
	calls = [m.start() for m in re.finditer(r"^[ \t]+rb_judge_merge_err_open$", text, re.MULTILINE)]
	assert len(calls) == 3, calls
	start = calls[MERGE_SITE_ORDER.index(path)]
	end_marker = MERGE_SITE_ENDS[path]
	end = text.index(end_marker, start) + len(end_marker)
	return text[start:end]


def _run_merge_site(
	path: str,
	exit_codes: list[int],
	stderr_text: str = DRAFT_MESSAGE,
	*,
	tmpdir_exists: bool = True,
) -> tuple[subprocess.CompletedProcess[str], str, list[list[str]], Path, str]:
	with tempfile.TemporaryDirectory(prefix="test_rb_judge_merge_err_") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		gh_log = tmp / "gh_calls.jsonl"
		stderr_file = tmp / "gh_stderr.txt"
		stderr_file.write_text(stderr_text, encoding="utf-8")
		gh_shim = bin_dir / "gh"
		gh_shim.write_text(
			"#!/usr/bin/env python3\n"
			"import json, os, sys\n"
			"log = os.environ['GH_CALL_LOG']\n"
			"n = sum(1 for _ in open(log, encoding='utf-8')) if os.path.exists(log) else 0\n"
			"with open(log, 'a', encoding='utf-8') as fh:\n"
			"\tfh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
			"codes = [int(c) for c in os.environ['GH_STUB_CODES'].split(',')]\n"
			"code = codes[n] if n < len(codes) else codes[-1]\n"
			"if code != 0:\n"
			"\tsys.stderr.write(open(os.environ['GH_STUB_STDERR'], encoding='utf-8').read())\n"
			"sys.exit(code)\n",
			encoding="utf-8",
		)
		gh_shim.chmod(0o755)
		scratch = tmp / "scratch"
		if tmpdir_exists:
			scratch.mkdir()
		github_output = tmp / "github_output.txt"
		github_output.write_text("", encoding="utf-8")
		harness = tmp / "harness.sh"
		harness.write_text(
			"set -euo pipefail\n"
			"review_head_gate_post_status() { :; }\n"
			+ _merge_err_helpers()
			+ "\n"
			+ _merge_site_snippet(path)
			+ '\necho "RB_MERGE_STATUS=${RB_MERGE_STATUS:-}"\n'
			+ 'echo "MERGE_CONFIRMED=${MERGE_CONFIRMED:-}"\n',
			encoding="utf-8",
		)
		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"GH_CALL_LOG": str(gh_log),
			"GH_STUB_CODES": ",".join(str(c) for c in exit_codes),
			"GH_STUB_STDERR": str(stderr_file),
			"GITHUB_OUTPUT": str(github_output),
			"TMPDIR": str(scratch),
			"PR_NUMBER": "42",
			"REPOSITORY": "o/r",
			"RB_JUDGED_HEAD_SHA": JUDGED,
		}
		proc = subprocess.run(["bash", str(harness)], env=env, text=True, capture_output=True, timeout=60)
		calls: list[list[str]] = []
		if gh_log.exists():
			calls = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines() if line.strip()]
		leftovers = sorted(p.name for p in scratch.iterdir()) if scratch.exists() else []
		assert leftovers == [], f"temp files left behind: {leftovers}"
		return proc, github_output.read_text(encoding="utf-8"), calls, scratch, proc.stdout


def _expected_calls(path: str) -> list[list[str]]:
	base = ["pr", "merge", "42", "--repo", "o/r", "--squash"]
	if path == "sync":
		return [base + ["--match-head-commit", JUDGED]]
	return [base + ["--auto", "--match-head-commit", JUDGED], base + ["--match-head-commit", JUDGED]]


def _error_lines(stdout: str) -> list[str]:
	return [line for line in stdout.splitlines() if line.startswith("RB_JUDGE_MERGE_ERROR ")]


@pytest.mark.parametrize("path", MERGE_SITE_ORDER)
def test_failed_merge_reports_gh_error_once(path: str) -> None:
	proc, output, calls, _scratch, stdout = _run_merge_site(path, [1, 1])
	assert proc.returncode == 0, proc.stderr
	assert calls == _expected_calls(path)
	lines = _error_lines(stdout)
	assert len(lines) == 1, stdout
	assert lines[0].startswith(f"RB_JUDGE_MERGE_ERROR pr=42 head={JUDGED} path={path} error=")
	assert DRAFT_MESSAGE in lines[0]
	warnings = [line for line in stdout.splitlines() if line.startswith("::warning::")]
	assert len(warnings) == 1
	assert warnings[0].startswith(OLD_WARNINGS[path])
	assert "gh error: " in warnings[0] and DRAFT_MESSAGE in warnings[0]
	if path == "sync":
		assert "judge_skip_reason=sync_merge_failed" in output.splitlines()
		assert "MERGE_CONFIRMED=" in stdout.splitlines()
	else:
		assert "RB_MERGE_STATUS=merge_failed" in stdout.splitlines()
		assert "auto: " in lines[0] and "direct: " in lines[0]


@pytest.mark.parametrize("path", MERGE_SITE_ORDER)
def test_successful_merge_prints_no_error_line(path: str) -> None:
	proc, output, calls, _scratch, stdout = _run_merge_site(path, [0])
	assert proc.returncode == 0, proc.stderr
	assert calls == _expected_calls(path)[:1]
	assert _error_lines(stdout) == []
	assert not any(line.startswith("::warning::") for line in stdout.splitlines())
	if path == "sync":
		assert "PR #42 merged synchronously." in stdout
		assert "MERGE_CONFIRMED=true" in stdout.splitlines()
	else:
		assert "RB_MERGE_STATUS=merge_requested" in stdout.splitlines()


@pytest.mark.parametrize("path", ("merge", "terminal"))
def test_direct_attempt_success_after_auto_failure_prints_no_error_line(path: str) -> None:
	proc, _output, calls, _scratch, stdout = _run_merge_site(path, [1, 0])
	assert proc.returncode == 0, proc.stderr
	assert calls == _expected_calls(path)
	assert _error_lines(stdout) == []
	assert "RB_MERGE_STATUS=merge_requested" in stdout.splitlines()


@pytest.mark.parametrize("path", MERGE_SITE_ORDER)
def test_error_text_is_single_line_bounded_and_redacted(path: str) -> None:
	token = "ghp_" + "A1b2C3d4E5" * 3 + "XYZ123"
	noisy = f"line one\r\n{token}\n" + ("x" * 50 + "\n") * 60 + "::error::forged\n"
	proc, _output, _calls, _scratch, stdout = _run_merge_site(path, [1, 1], noisy)
	assert proc.returncode == 0, proc.stderr
	lines = _error_lines(stdout)
	assert len(lines) == 1, stdout
	error_value = lines[0].split(" error=", 1)[1]
	assert 0 < len(error_value) <= 500
	assert token not in stdout
	assert "[redacted]" in error_value
	assert not any(line.startswith("::error::") for line in stdout.splitlines())


@pytest.mark.parametrize("path", MERGE_SITE_ORDER)
def test_mktemp_failure_keeps_previous_behaviour(path: str) -> None:
	proc, output, calls, _scratch, stdout = _run_merge_site(path, [1, 1], tmpdir_exists=False)
	assert proc.returncode == 0, proc.stderr
	assert calls == _expected_calls(path)
	assert _error_lines(stdout) == []
	warnings = [line for line in stdout.splitlines() if line.startswith("::warning::")]
	assert warnings == [OLD_WARNINGS[path]]
	if path == "sync":
		assert "judge_skip_reason=sync_merge_failed" in output.splitlines()
	else:
		assert "RB_MERGE_STATUS=merge_failed" in stdout.splitlines()


def test_no_merge_command_discards_stderr() -> None:
	text = _script_text()
	for line in text.splitlines():
		if "gh pr merge" in line and not line.lstrip().startswith("#"):
			assert "2>/dev/null" not in line, line
	reports = re.findall(r"^\s+rb_judge_merge_err_report (\S+)$", text, re.MULTILINE)
	assert reports == ["merge", "terminal", "sync"]

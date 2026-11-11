#!/usr/bin/env python3
"""Release-gate smoke cleanups leave no PR behind (issue #8953).

Release runs 40608957733 .. 41941001401: Phase 3b timed out while Implement
sat `pending`, so "Cleanup test artifacts" (no PR number) closed only the
issue, and the late Implement run opened a PR for the closed issue (#8239,
#8346, #8348, #8373, #8389). The decompose cleanup closed issues only, so
judge "Re-deliver" PRs (#8841/#8842) on orchestrator/project-<N> stayed open.

The cases are collected by
tests/test_test_and_mark_stable_alt_model_cleanup_pr_selector.py, which CI
already runs.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"


def _wf() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _step(wf: str, marker: str) -> str:
	start = wf.index(marker)
	nxt = wf.find("\n      - name:", start + len(marker))
	job_end = wf.find("\n  # ──", start)
	ends = [e for e in (nxt, job_end) if e != -1]
	return wf[start:min(ends) if ends else len(wf)]


def _run_body(step: str) -> str:
	lines = step.splitlines()
	run_idx = next(i for i, line in enumerate(lines) if line.strip() == "run: |")
	body = []
	for line in lines[run_idx + 1:]:
		if line.strip() == "":
			body.append("")
			continue
		if not line.startswith("          "):
			break
		body.append(line[10:])
	return "\n".join(body) + "\n"


def _env_block(step: str) -> str:
	return step[step.index("env:"):step.index("run: |")]


def test_smoke_cleanup_closes_late_pr_when_pr_number_empty() -> None:
	step = _step(_wf(), "- name: Cleanup test artifacts")
	body = _run_body(step)
	env = _env_block(step)
	assert "ISSUE_TITLE: ${{ steps.create-issue.outputs.title }}" in env
	assert "IMPL_CREATED_AFTER_INPUT: ${{ steps.create-issue.outputs.created_after }}" in env
	# New inputs come through env only.
	assert "${{" not in body.split("# Close issue")[0]
	pr_branch = body.index('if [ -n "${PR_NUMBER}" ]; then')
	else_branch = body.index('elif [[ "${ISSUE_NUMBER}" =~ ^[1-9][0-9]*$ ]]; then', pr_branch)
	close_issue = body.index("# Close issue", else_branch)
	fallback = body[else_branch:close_issue]
	# Every still-active run of this issue is cancelled, not only the newest
	# one: a newer completed run must not hide an older queued run.
	# implement.yml is workflow_call-only and owns no runs, so the lookup
	# pages the repo-wide listing with Phase 3b's "Implement" name match.
	assert "actions/workflows/implement.yml/runs" not in fallback
	assert "actions/runs?per_page=100&page=${LATE_IMPL_PAGE}${LATE_IMPL_CREATED_FILTER}" in fallback
	assert "for LATE_IMPL_PAGE in 1 2 3 4 5 6 7 8 9 10; do" in fallback
	assert 'select((.name // "") | test("Implement"; "i"))' in fallback
	assert "::warning::Implement run lookup failed for issue #${ISSUE_NUMBER}" in fallback
	assert 'LATE_IMPL_CREATED_FILTER="&created=>${IMPL_CREATED_AFTER_INPUT}"' in fallback
	assert "select(.display_title == env.ISSUE_TITLE)" in fallback
	assert 'select(.status != "completed")' in fallback
	assert "find_latest_scoped_run_field" not in fallback
	assert "/actions/runs/${LATE_IMPL_RUN_ID}/cancel" in fallback
	# The late PR is closed without replacing its body; the note is a comment.
	assert 'pulls/${LATE_PR}" -X PATCH -f state=closed >/dev/null' in fallback
	assert "issues/${LATE_PR}/comments" in fallback
	assert "pulls?state=open&head=${TEST_REPO%%/*}:ai/issue-${ISSUE_NUMBER}&per_page=100" in fallback
	assert fallback.index("/cancel") < fallback.index("pulls?state=open&head=")
	# No auto-close keyword followed by an issue reference (§21).
	assert not re.search(r"(?i)\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\s+#", fallback)
	# The branch delete still runs after the issue close.
	assert body.index('BRANCH="ai/issue-${ISSUE_NUMBER}"') > close_issue


def test_alt_model_cleanup_cancels_scoped_implement_runs() -> None:
	wf = _wf()
	create = _step(wf, "- name: Create alt-model smoke issue")
	assert 'echo "title=${TITLE}" >> "$GITHUB_OUTPUT"' in create
	step = _step(wf, "- name: Cleanup alt-model artifacts")
	env = _env_block(step)
	assert "ALT_ISSUE_TITLE: ${{ steps.create.outputs.title }}" in env
	assert "PRE_IMPLEMENT: ${{ steps.create.outputs.pre_implement_run_id }}" in env
	body = _run_body(step)
	assert "select(.display_title == env.ALT_ISSUE_TITLE)" in body
	assert "select(.id > ${PRE_IMPLEMENT:-0})" in body
	assert "actions/workflows/implement.yml/runs" not in body
	assert "actions/runs?per_page=100&page=${ALT_IMPL_PAGE}" in body
	assert "for ALT_IMPL_PAGE in 1 2 3 4 5; do" in body
	assert "all(.workflow_runs[]; .id > ${PRE_IMPLEMENT:-0})" in body
	assert body.index("/cancel") < body.index("pulls?state=open&head=${TEST_REPO%%/*}:ai/issue-${ISSUE_NUMBER}&per_page=1")


def test_decompose_cleanup_closes_project_prs_before_branch_delete() -> None:
	body = _run_body(_step(_wf(), "- name: Cleanup orchestrate test issues"))
	base_lookup = body.index("pulls?state=open&base=${PROJECT_BRANCH}&per_page=100")
	head_lookup = body.index("pulls?state=open&head=${OWNER}:${PROJECT_BRANCH}&per_page=100")
	child_lookup = body.index("pulls?state=open&head=${OWNER}:ai/issue-${m}&per_page=100")
	close = body.index('close_with_retry "${p}"')
	delete = body.index("git/refs/heads/${PROJECT_BRANCH}")
	assert max(base_lookup, head_lookup, child_lookup) < close < delete
	assert 'PROJECT_BRANCH="orchestrator/project-${TRACKING_NUMBER}"' in body
	assert "::warning::Could not delete branch ${PROJECT_BRANCH} after 3 attempts" in body
	# A missing ref ends the loop without the warning.
	probe = body.index('git/ref/heads/${PROJECT_BRANCH}"')
	assert delete < probe < body.index("already absent") < body.index("::warning::Could not delete branch")


_FAKE_GH = textwrap.dedent("""\
	#!/usr/bin/env bash
	printf '%s\\n' "$*" >> "${FAKE_DIR}/calls"
	case "$*" in
		*"pulls?state=open&base=orchestrator/project-7&"*) printf '21\\n22\\n'; exit 0 ;;
		*"pulls?state=open&head=o:orchestrator/project-7&"*) printf '23\\n'; exit 0 ;;
		*"pulls?state=open&head=o:ai/issue-8&"*) printf '21\\n'; exit 0 ;;
		*"pulls?state=open&head="*) exit 0 ;;
		*"search/issues"*) printf '9\\n'; exit 0 ;;
		*"-X PATCH"*|*"-X DELETE"*) exit 0 ;;
	esac
	exit 1
	""")


def test_decompose_cleanup_executes_against_fake_gh() -> None:
	body = _run_body(_step(_wf(), "- name: Cleanup orchestrate test issues"))
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(_FAKE_GH, encoding="utf-8")
		gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
		# `sleep` is shadowed so a failing retry cannot slow the test.
		script = "sleep() { :; }\n" + body
		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
			"FAKE_DIR": str(root),
			"TEST_REPO": "o/r",
			"TRACKING_NUMBER": "7",
			"CHILDREN": "8",
			"RUN_ID": "1",
		}
		proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		calls = (root / "calls").read_text(encoding="utf-8").splitlines()
		closes = [c for c in calls if "-X PATCH" in c]
		closed = [c.split("issues/")[1].split()[0] for c in closes]
		assert closed[:2] == ["7", "8"]
		assert sorted(closed[3:]) == ["21", "22", "23"], closed
		delete_idx = next(i for i, c in enumerate(calls) if "git/refs/heads/orchestrator/project-7" in c and "-X DELETE" in c)
		assert all(calls.index(c) < delete_idx for c in closes)


_FAKE_RUNS_GH = textwrap.dedent("""\
	#!/usr/bin/env bash
	printf '%s\\n' "$*" >> "${FAKE_DIR}/calls"
	case "$*" in
		*"actions/runs?per_page=100&page=1&"*)
			# A full page of newer runs, none of them ours.
			jq -n '{workflow_runs: [range(100) | {id: (900 - .), name: "Internal: AI Implement", display_title: "other", status: "queued"}]}'
			exit 0 ;;
		*"actions/runs?per_page=100&page=2&"*)
			jq -n --arg t "$ISSUE_TITLE" '{workflow_runs: [
				{id: 41, name: "Internal: AI Implement", display_title: $t, status: "pending"},
				{id: 40, name: "Internal: AI Implement", display_title: $t, status: "completed"},
				{id: 39, name: "Internal: AI Plan", display_title: $t, status: "queued"}]}'
			exit 0 ;;
		*"/cancel"*) exit 0 ;;
		*"pulls?state=open&head="*) exit 0 ;;
	esac
	exit 1
	""")


def test_smoke_cleanup_cancels_a_caller_run_past_page_one() -> None:
	body = _run_body(_step(_wf(), "- name: Cleanup test artifacts"))
	start = body.index('elif [[ "${ISSUE_NUMBER}" =~ ^[1-9][0-9]*$ ]]; then')
	fallback = body[start:body.index("# Close issue", start)]
	script = "set -euo pipefail\nif false; then :\n" + fallback + "\n"
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(_FAKE_RUNS_GH, encoding="utf-8")
		gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
			"FAKE_DIR": str(root),
			"TEST_REPO": "o/r",
			"ISSUE_NUMBER": "12",
			"ISSUE_TITLE": "[E2E Smoke Test] canary",
			"IMPL_CREATED_AFTER_INPUT": "2026-11-01T00:00:00Z",
			"GITHUB_RUN_ID": "1",
		}
		proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		calls = (root / "calls").read_text(encoding="utf-8").splitlines()
		cancels = [c for c in calls if "/cancel" in c]
		assert cancels == ["api -X POST repos/o/r/actions/runs/41/cancel"], calls
		assert not [c for c in calls if "implement.yml/runs" in c]


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

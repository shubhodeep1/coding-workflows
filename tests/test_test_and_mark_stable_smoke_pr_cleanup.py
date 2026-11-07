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
	assert 'find_latest_scoped_run_field "${TEST_REPO}" "${IMPL_CREATED_AFTER_INPUT:-}" "Implement" "${ISSUE_TITLE}" "id" 3' in fallback
	assert "/actions/runs/${LATE_IMPL_RUN_ID}/cancel" in fallback
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


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

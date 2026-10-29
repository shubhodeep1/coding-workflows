#!/usr/bin/env python3
"""Regression check for the alt-model E2E cleanup PR selector.

The "Cleanup alt-model artifacts" step in test-and-mark-stable.yml closes the
throwaway implement PR for the alt-model canary issue.  It used to discover that
PR via the *first* `cross-referenced` event on the canary issue's timeline.
When the alt-model implement run failed, no `ai/issue-<N>` PR ever existed, so
the first cross-reference was whatever unrelated PR merely mentioned the canary
issue (e.g. a fix PR whose body said `Refs #<N>`).  That PR was then force-closed
as collateral damage (this is exactly what closed PR #3179).  The selector must
instead target only the implement PR whose head branch is `ai/issue-<N>` — the
same selector Phase 3a's wait-implement uses.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
INTERNAL_REVIEW = REPO_ROOT / ".github" / "workflows" / "internal-review.yml"


def _read_workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _read_internal_review() -> str:
	return INTERNAL_REVIEW.read_text(encoding="utf-8")


def _cleanup_alt_model_step(wf: str) -> str:
	marker = "- name: Cleanup alt-model artifacts"
	start = wf.index(marker)
	# The step body runs until the next step (`- name:`) or job boundary.
	next_step = wf.find("\n      - name:", start + len(marker))
	end = next_step if next_step != -1 else len(wf)
	return wf[start:end]


def test_cross_referenced_timeline_heuristic_is_gone() -> None:
	wf = _read_workflow()
	# The brittle "first cross-referenced issue number" PR-discovery heuristic
	# must not exist anywhere in the workflow.
	assert 'select(.event == "cross-referenced") | .source.issue.number] | first' not in wf


def test_alt_model_cleanup_targets_ai_issue_head_branch_pr() -> None:
	wf = _read_workflow()
	step = _cleanup_alt_model_step(wf)
	# PR discovery for cleanup must use the ai/issue-<N> head-branch selector.
	assert "pulls?state=open&head=${TEST_REPO%%/*}:ai/issue-${ISSUE_NUMBER}&per_page=1" in step
	# And it must not fall back to the canary issue's cross-reference timeline.
	assert "issues/${ISSUE_NUMBER}/timeline" not in step
	# The discovered PR is still closed via the shared retry helper.
	assert 'close_with_retry "pulls" "${PR_NUMBER}"' in step


def test_internal_review_has_no_claude_branch_push_route() -> None:
	# PR #6438 removed the no-PR claude/** push review (and with it the open-PR
	# lookup the two earlier tests here pinned): a claude/** branch is reviewed
	# once it has a PR, like every other branch.
	import yaml

	wf = _read_internal_review()
	parsed = yaml.safe_load(wf)
	triggers = parsed.get("on", parsed.get(True))
	assert set(triggers) == {"pull_request", "workflow_dispatch"}
	assert list(parsed["jobs"]) == ["review"]
	assert "resolve-claude-branch-pr" not in wf
	assert "force_claude_branch_review" not in wf


# ---------------------------------------------------------------------------
# Single /approved post in the alt-model wait (issue #8347)
# ---------------------------------------------------------------------------
# Release gate run 40831286935 posted ~67 `/approved` comments while the canary
# sat in ai:awaiting-approval, because the wait loop posted on every 20s poll.
# Each comment starts every issue_comment workflow. The loop now posts once per
# arrival in awaiting-approval and retries only when the POST fails.

ALT_WAIT_STEP = "- name: Wait for clarify→plan→implement (alt-model)"


def _alt_wait_run_body(wf: str) -> str:
	start = wf.index(ALT_WAIT_STEP)
	next_step = wf.find("\n      - name:", start + len(ALT_WAIT_STEP))
	step = wf[start:next_step if next_step != -1 else len(wf)]
	lines = step.splitlines()
	run_idx = next(i for i, line in enumerate(lines) if line.strip() == "run: |")
	body = []
	for line in lines[run_idx + 1:]:
		if line.strip() == "":
			body.append("")
			continue
		assert line.startswith("          "), line
		body.append(line[10:])
	return "\n".join(body) + "\n"


# The fakes are shell functions prepended to the step script, not executables
# on PATH: the pre-push gate container mounts the temp dir noexec, so PATH
# shims there were skipped and the loop ran the real `sleep 20` (and any real
# `gh`) until the 60s subprocess timeout.
_FAKE_GH = r"""gh() {
  local state="${FAKE_GH_DIR:?}"
  case "${2:-}" in
    */comments)
      printf '%s\n' "$*" >> "${state}/posts"
      if [ -f "${state}/fail_once" ]; then
        rm -f "${state}/fail_once"
        return 1
      fi
      printf '%s\n' "$*" >> "${state}/ok_posts"
      return 0
      ;;
  esac
  local idx i n
  local -a seq
  idx=$(cat "${state}/idx" 2>/dev/null || echo 0)
  mapfile -t seq < "${state}/labels"
  n=${#seq[@]}
  i=${idx}
  if [ "${i}" -ge "${n}" ]; then i=$((n - 1)); fi
  echo $((idx + 1)) > "${state}/idx"
  printf '%s\n' "${seq[$i]}"
}
sleep() { :; }
"""


def _run_alt_wait(label_sequence: list[str], fail_first_post: bool = False) -> tuple[int, int, int, str]:
	import os
	import subprocess
	import tempfile

	body = _alt_wait_run_body(_read_workflow())
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		state = root / "state"
		state.mkdir()
		(state / "labels").write_text("\n".join(label_sequence) + "\n", encoding="utf-8")
		if fail_first_post:
			(state / "fail_once").write_text("", encoding="utf-8")
		script = root / "step.sh"
		script.write_text(_FAKE_GH + body, encoding="utf-8")
		env = {
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"HOME": str(root),
			"FAKE_GH_DIR": str(state),
			"TEST_REPO": "o/r",
			"ISSUE_NUMBER": "1",
			"GH_TOKEN": "dummy-token-not-real",
		}
		proc = subprocess.run(
			["bash", "-e", str(script)],
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		posts_file = state / "posts"
		ok_file = state / "ok_posts"
		attempts = posts_file.read_text(encoding="utf-8").splitlines() if posts_file.exists() else []
		oks = ok_file.read_text(encoding="utf-8").splitlines() if ok_file.exists() else []
		for line in attempts:
			assert "body=/approved" in line, line
		return proc.returncode, len(attempts), len(oks), proc.stdout + proc.stderr


def test_alt_model_wait_posts_approved_behind_once_flag() -> None:
	body = _alt_wait_run_body(_read_workflow())
	assert "DEADLINE=$(( $(date +%s) + 4500 ))" in body
	assert "ALT_APPROVED_POSTED=0" in body
	# The 600s re-post for an issue still awaiting approval must stay.
	assert "ALT_APPROVED_POSTED_AT=0" in body
	assert "ALT_APPROVED_POSTED_AT )) -ge 600 ]" in body
	flag_check = body.index('if [ "${ALT_APPROVED_POSTED}" = "0" ]; then')
	post = body.index('-f body="/approved"')
	assert flag_check < post
	# The old unconditional `|| true` post on every poll is gone.
	assert '-f body="/approved" >/dev/null 2>&1 || true' not in body
	assert "::error::Alt-model run timed out before reaching review stage" in body


def test_alt_model_wait_posts_approved_once_per_arrival() -> None:
	seq = ["", "ai:clarification"] + ["ai:awaiting-approval"] * 5 + ["ai:implementing", "ai:done"]
	rc, attempts, oks, out = _run_alt_wait(seq)
	assert rc == 0, out
	assert (attempts, oks) == (1, 1), out


def test_alt_model_wait_reposts_after_a_return_to_awaiting_approval() -> None:
	seq = ["ai:awaiting-approval"] * 3 + ["ai:planning"] + ["ai:awaiting-approval"] * 3 + ["ai:done"]
	rc, attempts, oks, out = _run_alt_wait(seq)
	assert rc == 0, out
	assert (attempts, oks) == (2, 2), out


def test_alt_model_wait_retries_a_failed_approved_post() -> None:
	seq = ["ai:awaiting-approval"] * 4 + ["ai:done"]
	rc, attempts, oks, out = _run_alt_wait(seq, fail_first_post=True)
	assert rc == 0, out
	assert (attempts, oks) == (2, 1), out
	assert "Posting /approved failed" in out


def test_alt_model_wait_keeps_flag_across_a_failed_label_read() -> None:
	# An empty label read (failed lookup) must not re-arm the post.
	seq = ["ai:awaiting-approval", "", "ai:awaiting-approval", "ai:done"]
	rc, attempts, oks, out = _run_alt_wait(seq)
	assert rc == 0, out
	assert (attempts, oks) == (1, 1), out

def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

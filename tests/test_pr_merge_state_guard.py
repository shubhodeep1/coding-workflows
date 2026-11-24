#!/usr/bin/env python3
"""Merge-time PR state guard (issue #10049).

On 2026-11-24 a review run squash-merged tele-funtoken-msg-scoring PR #5800
22 minutes after a maintainer had closed it without merging: the gate passed
while the PR was open, the merge job started after the close, and no merge
path re-read the PR's state. `_pr_merge_state_check` (scripts/pr_checks_lib.sh)
now runs when a merge job starts and immediately before every merge call;
a PR that is not open, is a draft, or moved off the bound head is not merged,
logs one `AUTOFIX_MERGE_SKIPPED` line and (when closed) releases the merge
lease it holds. A failed read fails closed (`reason=pr_state_unavailable`).

The behavioural cases drive the shipped library through the stateful fake
`gh` of tests/merge_lease_fake_gh.py; the wiring cases pin the guard in front
of every merge call in the review workflow and its helpers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_merge_lease as ml  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
AUTO_MERGE = REPO_ROOT / "scripts" / "review_enable_auto_merge.sh"
RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
TRAIN = REPO_ROOT / "scripts" / "review_merge_train.sh"

PR = 5800
SHA = "fee7005234cc58358be7a9eaddb29c3e3e8ac3d1"
CLOSED_AT = "2026-11-24T12:04:00Z"


def _pr(state: str = "open", *, draft: bool = False, sha: str = SHA, closed_at: str | None = None) -> dict:
	pr = ml._pull(PR, sha=sha)
	pr["state"] = state
	pr["draft"] = draft
	pr["closed_at"] = closed_at
	return pr


def _check(h: "ml._Harness", stage: str, extra: str = "") -> tuple[int, str]:
	res = h.lib(f'{extra}_pr_merge_state_check {PR} {SHA} {stage}; echo "rc=$? outcome=$PR_MERGE_STATE_OUTCOME"')
	assert res.returncode == 0, res.stderr
	line = [ln for ln in res.stdout.splitlines() if ln.startswith("rc=")][-1]
	return int(line.split()[0][3:]), res.stdout + res.stderr


def _writes(h: "ml._Harness") -> list[str]:
	return [c for c in h.calls if "git/commits" in c and "POST" in c or "git/refs" in c and ("PATCH" in c or "POST" in c)]


def _skip_lines(out: str) -> list[str]:
	return [ln for ln in out.splitlines() if ln.startswith("AUTOFIX_MERGE_SKIPPED ")]


class StateCheck(unittest.TestCase):
	def test_closed_pr_at_start_skips_logs_and_releases_its_lease(self) -> None:
		st = {"pulls": [_pr("closed", closed_at=CLOSED_AT)]}
		ml._seed_lease(st, ml._record(PR))
		h = ml._Harness(st)
		rc, out = _check(h, "start")
		self.assertEqual(rc, 1)
		self.assertEqual(_skip_lines(out), [f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason=pr_closed closed_at={CLOSED_AT} stage=start"])
		self.assertIn("outcome=closed", out)
		self.assertIn(f"MERGE_LEASE pr={PR} base=main mode=release outcome=released reason=pr_closed", out)
		self.assertIn("state=released", h.record())
		self.assertFalse([c for c in h.calls if "/labels" in c])

	def test_pr_closed_between_start_and_pre_merge_is_not_merged(self) -> None:
		h = ml._Harness({"pulls": [_pr("open")]})
		rc, out = _check(h, "start")
		self.assertEqual(rc, 0)
		self.assertEqual(_skip_lines(out), [])
		st = h.state
		st["pulls"] = [_pr("closed", closed_at=CLOSED_AT)]
		h.state_path.write_text(json.dumps(st))
		rc, out = _check(h, "pre_merge")
		self.assertEqual(rc, 1)
		self.assertEqual(_skip_lines(out), [f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason=pr_closed closed_at={CLOSED_AT} stage=pre_merge"])

	def test_open_pr_on_the_bound_head_passes_without_lease_calls(self) -> None:
		h = ml._Harness({"pulls": [_pr("open")]})
		rc, out = _check(h, "pre_merge")
		self.assertEqual(rc, 0)
		self.assertIn("outcome=open", out)
		self.assertEqual(_skip_lines(out), [])
		self.assertEqual([c for c in h.calls if "pulls/" in c], [f"gh api repos/owner/repo/pulls/{PR}"])
		self.assertFalse([c for c in h.calls if "merge-lease" in c])

	def test_rate_limited_read_fails_closed_without_lease_calls(self) -> None:
		h = ml._Harness({"pulls": [_pr("open")], "fail": [f"pulls/{PR}"]})
		rc, out = _check(h, "pre_merge")
		self.assertEqual(rc, 1)
		self.assertEqual(_skip_lines(out), [f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason=pr_state_unavailable closed_at=- stage=pre_merge"])
		self.assertFalse([c for c in h.calls if "merge-lease" in c])

	def test_json_without_state_is_unavailable(self) -> None:
		h = ml._Harness({})
		res = h.lib(f"_pr_merge_state_check {PR} {SHA} start '{{\"head\":{{\"sha\":\"{SHA}\"}}}}'; echo rc=$?")
		self.assertIn("rc=1", res.stdout)
		self.assertIn("reason=pr_state_unavailable", res.stdout)
		self.assertEqual(h.calls, [])

	def test_draft_and_moved_head_are_refused_without_releasing_the_lease(self) -> None:
		for pr, reason in ((_pr("open", draft=True), "pr_draft"), (_pr("open", sha="c" * 40), "head_moved")):
			st = {"pulls": [pr]}
			ml._seed_lease(st, ml._record(PR))
			h = ml._Harness(st)
			rc, out = _check(h, "pre_merge")
			self.assertEqual(rc, 1)
			self.assertIn(f"reason={reason} closed_at=- stage=pre_merge", out)
			self.assertEqual(_writes(h), [])
			self.assertIn("state=held", h.record())

	def test_passed_pr_json_makes_no_api_call(self) -> None:
		h = ml._Harness({})
		payload = json.dumps(_pr("open"))
		res = h.lib(f"_pr_merge_state_check {PR} {SHA} start '{payload}'; echo rc=$? for=${{PR_MERGE_STATE_PR_FOR:-none}}")
		self.assertIn("rc=0 for=none", res.stdout)
		self.assertEqual(h.calls, [])

	def test_lease_held_by_another_pr_is_left_alone(self) -> None:
		st = {"pulls": [_pr("closed", closed_at=CLOSED_AT)]}
		ml._seed_lease(st, ml._record(77))
		h = ml._Harness(st)
		rc, out = _check(h, "pre_merge")
		self.assertEqual(rc, 1)
		self.assertIn(f"MERGE_LEASE pr={PR} base=main mode=release outcome=not_held reason=pr_closed", out)
		self.assertEqual(_writes(h), [])
		self.assertIn("pr=77", h.record())

	def test_lease_disabled_makes_no_lease_call(self) -> None:
		h = ml._Harness({"pulls": [_pr("closed", closed_at=CLOSED_AT)]})
		rc, out = _check(h, "start", "MERGE_LEASE_ENABLED=false\n")
		self.assertEqual(rc, 1)
		self.assertNotIn("mode=release", out)
		self.assertFalse([c for c in h.calls if "merge-lease" in c])

	def test_start_read_is_handed_once_to_the_freshness_check(self) -> None:
		h = ml._Harness({"pulls": [_pr("open")]})
		res = h.lib(
			f"_pr_merge_state_check {PR} {SHA} start || exit 9\n"
			f'_pr_base_freshness {PR} {SHA} "" >/dev/null; echo "first_base=${{PR_BASE_FRESHNESS_BASE_REF}}"\n'
			f'_pr_base_freshness {PR} {SHA} "" >/dev/null; echo "second_base=${{PR_BASE_FRESHNESS_BASE_REF}}"\n')
		self.assertIn("first_base=main", res.stdout, res.stderr)
		self.assertIn("second_base=main", res.stdout)
		reads = [c for c in h.calls if c == f"gh api repos/owner/repo/pulls/{PR}"]
		# One read for the start check (reused by the first freshness call),
		# one fresh read for the second freshness call.
		self.assertEqual(len(reads), 2)


class Replay5800(unittest.TestCase):
	"""tele-funtoken-msg-scoring PR #5800: the gate saw the PR open, the PR
	closed at 12:04:00, the merge path ran at 12:20-12:26 on the open-PR
	snapshot and merged it. With the guard nothing merges or is labelled."""

	def test_final_ci_resume_on_a_closed_pr_merges_and_labels_nothing(self) -> None:
		gate_seen = ml._pull(PR, sha=SHA)
		live = _pr("closed", closed_at=CLOSED_AT)
		labels = {str(PR): ["ai:final-ci-merge-pending"]}
		st = {"pulls": [live], "labels": labels, "user": "pipeline-bot",
			"statuses": {SHA: [
				{"context": "ai-review/final-ci-merge-pending", "state": "pending", "creator": {"login": "pipeline-bot"}},
				{"context": "ai-review/head-gate", "state": "success", "creator": {"login": "pipeline-bot"}}]},
			"graphql": {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [{"number": 5797}]}}}}}}
		ml._seed_lease(st, ml._record(PR))
		h = ml._Harness(st)
		stubs = ("_pr_wait_for_required_checks() { PR_CHECKS_WAIT_OUTCOME=ok; PR_CHECKS_LAST_CI_STATE=success; return 0; }\n"
			"_pr_base_fresh_for_merge() { PR_BASE_FRESHNESS_OUTCOME=fresh; return 0; }\n")
		prs_jsonl = json.dumps({"number": PR, "head": gate_seen["head"]["ref"], "head_repo": "owner/repo", "head_sha": SHA,
			"base": "main", "draft": False, "labels": labels[str(PR)]})
		res = h.train_fn(stubs + '_mt_final_ci_merge_resume "${PRS_JSONL}" ""', {"PRS_JSONL": prs_jsonl})
		out = res.stdout + res.stderr
		self.assertIn(f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason=pr_closed closed_at={CLOSED_AT} stage=pre_merge", out)
		self.assertIn(f"FINAL_CI_MERGE_RESUME pr={PR} head={SHA} outcome=deferred reason=pr_closed", out)
		self.assertNotIn("merged", h.state)
		self.assertNotIn("5797", h.state.get("labels", {}))
		self.assertIn("state=released", h.record())


def _step_body(text: str, name: str) -> str:
	start = text.index(f"- name: {name}")
	nxt = text.find("\n      - name:", start + 1)
	return text[start:nxt if nxt != -1 else len(text)]


DET_SKIP_STEP = "Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge"
LIB_SOURCE_LINE = "source .codex-freshness-src/scripts/pr_checks_lib.sh"
LINKED_ISSUE = 5797
PR_READ_CALL = f"gh api repos/owner/repo/pulls/{PR}"
FRESH_STUB = "_pr_base_fresh_for_merge() { PR_BASE_FRESHNESS_OUTCOME=fresh; return 0; }\n"
CHECKS_OK_STUB = "_pr_wait_for_required_checks() { PR_CHECKS_WAIT_OUTCOME=ok; PR_CHECKS_WAIT_WAITED_S=0; return 0; }\n"
# The checks wait closes the PR (state file rewrite) before the pre-merge read.
CHECKS_OK_THEN_CLOSE_STUB = (
	"_pr_wait_for_required_checks() { PR_CHECKS_WAIT_OUTCOME=ok; PR_CHECKS_WAIT_WAITED_S=0; "
	"jq --arg c \"${CLOSED_AT}\" '.pulls[0].state = \"closed\" | .pulls[0].closed_at = $c' \"${FAKE_GH_STATE}\" > \"${FAKE_GH_STATE}.tmp\" "
	"&& mv \"${FAKE_GH_STATE}.tmp\" \"${FAKE_GH_STATE}\"; return 0; }\n"
)


def _det_skip_run_body(overrides: str = "") -> str:
	lines = _step_body(REVIEW.read_text(encoding="utf-8"), DET_SKIP_STEP).splitlines()
	run_at = next(i for i, ln in enumerate(lines) if ln.strip() == "run: |")
	body: list[str] = []
	for ln in lines[run_at + 1:]:
		if ln.strip() and (len(ln) - len(ln.lstrip(" "))) <= 8:
			break
		body.append(ln)
	text = textwrap.dedent("\n".join(body)) + "\n"
	assert text.count(LIB_SOURCE_LINE) == 1
	# Test-only helper stubs go right after the verified library is sourced.
	return text.replace(LIB_SOURCE_LINE, LIB_SOURCE_LINE + "\n" + overrides, 1)


class DeterministicSkipStepReplay(unittest.TestCase):
	"""Runs the deterministic-skip step body of review_autofix.yml (the path
	that merged tele-funtoken-msg-scoring PR #5800, run 45386407946) with the
	shipped pr_checks_lib.sh as the verified support library and the stateful
	fake `gh`."""

	def _state(self, pr: dict) -> dict:
		st = {"pulls": [pr], "labels": {}, "user": "pipeline-bot",
			"graphql": {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [{"number": LINKED_ISSUE}]}}}}}}
		ml._seed_lease(st, ml._record(PR))
		return st

	def _run_step(self, h: "ml._Harness", overrides: str = "") -> subprocess.CompletedProcess:
		work = h.dir / "work"
		src = work / ".codex-freshness-src"
		(src / "scripts").mkdir(parents=True)
		shutil.copy(ml.LIB, src / "scripts" / "pr_checks_lib.sh")
		git_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(h.dir), "GIT_CONFIG_GLOBAL": os.devnull,
			"GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
			"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
		for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "support"]):
			subprocess.run(cmd, cwd=src, env=git_env, check=True, capture_output=True)
		support_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, env=git_env, check=True,
			capture_output=True, text=True).stdout.strip()
		# The inline gh_retry sleeps between attempts; keep the test fast.
		sleep = h.dir / "bin" / "sleep"
		sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
		sleep.chmod(0o755)
		self.summary = h.dir / "summary.md"
		env = dict(h.env)
		env.update({k: v for k, v in git_env.items() if k.startswith("GIT_")})
		env.update({
			"HOME": str(h.dir), "PR_NUMBER": str(PR), "PR_HEAD_REF": "ai/issue-5797", "PR_HEAD_SHA": SHA,
			"PR_BASE_REF": "main", "REPOSITORY": "owner/repo", "ENABLE_AUTO_MERGE": "true",
			"ORCH_INTEGRATION_BRANCH_PATTERN": "^orchestrator/project-", "FORWARD_MERGE_FALLBACK_AUTO_MERGE": "true",
			"DET_SKIP_REASON": "small_diff", "FRESHNESS_HELPER_CHECKOUT_OUTCOME": "success",
			"REVIEW_SUPPORT_SHA": support_sha, "GITHUB_STEP_SUMMARY": str(self.summary),
			"GITHUB_OUTPUT": str(h.dir / "output.txt"), "CLOSED_AT": CLOSED_AT,
		})
		return subprocess.run(["bash", "-c", _det_skip_run_body(overrides)], cwd=work, env=env,
			capture_output=True, text=True, timeout=180)

	def _assert_nothing_merged_or_labelled(self, h: "ml._Harness") -> None:
		self.assertNotIn("merged", h.state)
		self.assertFalse([c for c in h.calls if c.startswith("gh pr merge")])
		self.assertFalse([c for c in h.calls if "/labels" in c])
		self.assertEqual(h.state.get("labels", {}).get(str(PR), []), [])
		self.assertEqual(h.state.get("labels", {}).get(str(LINKED_ISSUE), []), [])

	def test_replay_5800_pr_closed_before_the_merge_job_starts(self) -> None:
		# Gate saw the PR open at 11:58; it closed at 12:04:00; the merge job
		# started at 12:20 and squash-merged it. Now: success, nothing merged,
		# one skip line at stage=start, lease released, no labels.
		h = ml._Harness(self._state(_pr("closed", closed_at=CLOSED_AT)))
		res = self._run_step(h)
		out = res.stdout + res.stderr
		self.assertEqual(res.returncode, 0, out)
		self.assertEqual(_skip_lines(out), [f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason=pr_closed closed_at={CLOSED_AT} stage=start"])
		self.assertIn(f"MERGE_LEASE pr={PR} base=main mode=release outcome=released reason=pr_closed", out)
		self.assertIn("state=released", h.record())
		self._assert_nothing_merged_or_labelled(h)
		self.assertIn("SKIPPED (PR not open: closed)", self.summary.read_text())

	def test_pr_closed_between_start_and_pre_merge_read(self) -> None:
		h = ml._Harness(self._state(_pr("open")))
		res = self._run_step(h, FRESH_STUB + CHECKS_OK_THEN_CLOSE_STUB)
		out = res.stdout + res.stderr
		self.assertEqual(res.returncode, 0, out)
		self.assertEqual(_skip_lines(out), [f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason=pr_closed closed_at={CLOSED_AT} stage=pre_merge"])
		self.assertIn("state=released", h.record())
		self._assert_nothing_merged_or_labelled(h)
		self.assertEqual(len([c for c in h.calls if c == PR_READ_CALL]), 2)

	def test_open_pr_on_the_gate_head_merges_as_before(self) -> None:
		h = ml._Harness(self._state(_pr("open")))
		res = self._run_step(h, FRESH_STUB + CHECKS_OK_STUB)
		out = res.stdout + res.stderr
		self.assertEqual(res.returncode, 0, out)
		self.assertEqual(_skip_lines(out), [])
		self.assertEqual(h.state.get("merged"), [["pr", "merge", str(PR), "--repo", "owner/repo", "--squash", "--auto", "--match-head-commit", SHA]])
		self.assertIn("ai:review-skipped", h.state["labels"].get(str(PR), []))
		self.assertIn("state=held", h.record())


def _assert_guard_before_each_merge(test: unittest.TestCase, text: str, merge_marker: str, window: int, expected: int) -> None:
	positions = [m.start() for m in re.finditer(re.escape(merge_marker), text)]
	test.assertEqual(len(positions), expected, merge_marker)
	for pos in positions:
		test.assertIn("_pr_merge_state_check", text[max(0, pos - window):pos], text[max(0, pos - window):pos + 200])
		test.assertIn("pre_merge", text[max(0, pos - window):pos])


class Wiring(unittest.TestCase):
	def test_deterministic_skip_step_checks_state_at_start_and_before_each_merge(self) -> None:
		block = _step_body(REVIEW.read_text(encoding="utf-8"), "Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge")
		start = block.index('elif ! _pr_merge_state_check "${PR_NUMBER}" "${PR_HEAD_SHA}" start; then')
		self.assertLess(block.index('elif ! [[ "${PR_HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]]'), start)
		self.assertLess(start, block.index('_pr_base_fresh_for_merge "${PR_NUMBER}"'))
		self.assertLess(start, block.index("gh_retry gh pr merge"))
		self.assertLess(start, block.index('ensure_label_exists "ai:review-skipped"'))
		_assert_guard_before_each_merge(self, block, "elif gh_retry gh pr merge", 200, 2)
		# Fallback when the support library is unverified: pre_merge fails closed.
		self.assertIn("reason=pr_state_unavailable closed_at=- stage=pre_merge", block)
		# The label-only paths read state and draft with the head (one call).
		self.assertIn('if .draft == true then \\"true\\" else \\"false\\" end'.replace('\\"', '"'), block)
		self.assertIn('if [ "${ready_label_pr_state}" != "open" ] || [ "${ready_label_pr_draft}" != "false" ]; then', block)

	def test_auto_merge_helper_checks_state_at_start_and_before_each_merge(self) -> None:
		text = AUTO_MERGE.read_text(encoding="utf-8")
		start = text.index('_pr_merge_state_check "${PR_NUMBER}" "${INITIAL_HEAD_SHA}" start "${_ORCH_PR_META_JSON}"')
		self.assertLess(text.index('if [ -z "${_orch_pr_head_sha}" ] || [ "${_orch_pr_head_sha}" != "${INITIAL_HEAD_SHA}" ]; then'), start)
		self.assertLess(start, text.index('_pr_base_fresh_for_merge "${PR_NUMBER}"'))
		self.assertLess(start, text.index("run_security_followup_guard; then"))
		self.assertLess(start, text.index('review_head_gate_post_status "${GITHUB_REPOSITORY}" "${INITIAL_HEAD_SHA}" success', start - 1))
		_assert_guard_before_each_merge(self, text, "if gh_retry gh pr merge", 300, 2)
		self.assertIn('if ! type _pr_merge_state_check >/dev/null 2>&1; then', text)
		self.assertIn('if [ "${ready_label_pr_state}" != "open" ] || [ "${ready_label_pr_draft}" != "false" ]; then', text)

	def test_rb_judge_checks_state_before_all_three_merge_sites(self) -> None:
		text = RB_JUDGE.read_text(encoding="utf-8")
		case_text = text[text.index("# END rb-judge merge error capture (#7659)"):]
		_assert_guard_before_each_merge(self, case_text,
			'if gh pr merge "${PR_NUMBER}" --repo "${REPOSITORY}" "${RB_MERGE_METHOD_FLAG:---squash}" --auto', 1600, 2)
		followup = case_text.index('_match_head_arg=(--match-head-commit "${RB_JUDGED_HEAD_SHA}")')
		guard = case_text.rindex('_pr_merge_state_check "${PR_NUMBER}" "${RB_JUDGED_HEAD_SHA}" pre_merge', 0, followup)
		self.assertGreater(guard, case_text.rindex('_pr_base_fresh_for_merge "${PR_NUMBER}" "${RB_JUDGED_HEAD_SHA}" "${PR_BASE_REF}"', 0, followup))
		self.assertIn('echo "judge_skip_reason=pr_state_unavailable"', case_text[guard:followup])
		# A closed PR gets no success status: each guard precedes its status post.
		for pos in [m.start() for m in re.finditer(r'_pr_merge_state_check "\$\{PR_NUMBER\}" "\$\{RB_JUDGED_HEAD_SHA\}" pre_merge', case_text)]:
			nxt_status = case_text.index('review_head_gate_post_status "${REPOSITORY}" "${RB_JUDGED_HEAD_SHA}" success', pos)
			nxt_merge = case_text.index('gh pr merge "${PR_NUMBER}"', pos)
			self.assertLess(nxt_status, nxt_merge)
		self.assertIn("if ! type _pr_merge_state_check >/dev/null 2>&1; then", text)

	def test_merge_train_resume_checks_state_before_its_merge(self) -> None:
		text = TRAIN.read_text(encoding="utf-8")
		merge = text.index('gh_retry gh pr merge "${resume_num}"')
		guard = text.rindex('_pr_merge_state_check "${resume_num}" "${resume_sha}" pre_merge', 0, merge)
		fresh = text.rindex("_pr_base_fresh_for_merge", 0, merge)
		# The state read follows the freshness gate (its compare and lease
		# calls) and is the last call before the merge; --match-head-commit
		# binds only the head.
		self.assertGreater(guard, fresh)
		self.assertNotIn("_pr_wait_for_required_checks", text[guard:merge])
		self.assertNotIn("_merge_lease", text[guard:merge])
		self.assertIn("outcome=deferred reason=pr_${PR_MERGE_STATE_OUTCOME:-unavailable}", text[guard:merge])

	def test_rb_judge_start_checks_reuse_the_pr_read_before_the_freshness_gate(self) -> None:
		text = RB_JUDGE.read_text(encoding="utf-8")
		case_text = text[text.index("# END rb-judge merge error capture (#7659)"):]
		starts = [m.start() for m in re.finditer(re.escape('_pr_merge_state_check "${PR_NUMBER}" "${RB_JUDGED_HEAD_SHA}" start "${_pr_json}"'), case_text)]
		# merge and merge_with_followup reuse their own `_pr_json` read (no
		# API call); terminal reads `.state` right before its chain.
		self.assertEqual(len(starts), 2)
		for pos in starts:
			nxt_fresh = case_text.index("_pr_base_fresh_for_merge", pos)
			self.assertNotIn("_pr_wait_for_required_checks", case_text[pos:nxt_fresh])
			self.assertNotIn("_merge_lease", case_text[pos:nxt_fresh])


if __name__ == "__main__":
	unittest.main()

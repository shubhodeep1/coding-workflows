#!/usr/bin/env python3
"""Merge lease and final-CI merge resume (scripts/pr_checks_lib.sh,
scripts/review_merge_train.sh; follow-up to #9642, answers Q1-Q5: A).

On 2026-11-17 approved PRs into `main` had been updated from `main` up to six
times without merging (#9367, #9368): every update head needed a CI run that
took 1.5-2.5 hours on the congested runner pool, and `main` moved again before
it finished. Integration children (#9603, #9597, #9545) sat with green final
CI because the merge step's 45-minute checks wait had timed out first and
nothing merged them afterwards.

These tests drive the shipped scripts through a stateful fake `gh`
(tests/merge_lease_fake_gh.py) that models refs with a fast-forward-only
PATCH, so the lease's compare-and-swap is exercised for real.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
TRAIN = REPO_ROOT / "scripts" / "review_merge_train.sh"
FAKE = Path(__file__).resolve().parent / "merge_lease_fake_gh.py"
SWEEP = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
POLL = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
CLOSE = REPO_ROOT / ".github" / "workflows" / "cancel_on_pr_close.yml"
REVIEW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"

HEAD = "a" * 40
REF = "refs/merge-lease/main"


def _pull(num: int, base: str = "main", head_ref: str | None = None, sha: str | None = None) -> dict:
	return {"number": num, "draft": False, "state": "open", "base": {"ref": base},
		"head": {"ref": head_ref or f"ai/issue-{num}", "sha": sha or f"{num:040d}", "repo": {"full_name": "owner/repo"}},
		"created_at": "2026-11-17T00:00:00Z"}


def _record(pr: int, *, state: str = "held", expires: int | None = None, paths: str = "shared.py") -> str:
	if expires is None:
		expires = int(time.time()) + 3600
	return (f"merge-lease v1\nstate={state}\nbase=main\npr={pr}\nhead={'b' * 40}\n"
		f"expires={expires}\nupdated={int(time.time())}\npaths:\n{paths}")


def _seed_lease(state: dict, record: str, sha: str = "c" * 40) -> None:
	state.setdefault("commits", {})[sha] = {"message": record, "tree": "f" * 40, "parents": []}
	state.setdefault("refs", {})[REF] = sha


class _Harness:
	def __init__(self, state: dict) -> None:
		if shutil.which("jq") is None:
			raise unittest.SkipTest("jq binary not available in test environment")
		self.dir = Path(tempfile.mkdtemp(prefix="merge_lease_"))
		self.state_path = self.dir / "state.json"
		self.log_path = self.dir / "gh.log"
		self.state_path.write_text(json.dumps(state))
		self.log_path.write_text("")
		bindir = self.dir / "bin"
		bindir.mkdir()
		gh = bindir / "gh"
		gh.write_text(f"#!/usr/bin/env bash\nexec python3 {str(FAKE)!r} \"$@\"\n")
		gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
		self.env = {
			"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
			"PYTHONDONTWRITEBYTECODE": "1",
			"FAKE_GH_STATE": str(self.state_path),
			"FAKE_GH_LOG": str(self.log_path),
			"PR_CHECKS_REPOSITORY": "owner/repo",
			"GITHUB_REPOSITORY": "owner/repo",
			"MERGE_BASE_SYNC_HEAD_POLL_ATTEMPTS": "1",
			"MERGE_BASE_SYNC_HEAD_POLL_SECONDS": "1",
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"TMPDIR": str(self.dir),
		}

	def lib(self, body: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
		preamble = f"""
set -uo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{ local t; t="$(mktemp)"; if gh api "$@" > "$t"; then cat "$t"; rm -f "$t"; return 0; fi; rm -f "$t"; return 1; }}
source {str(LIB)!r}
"""
		env = dict(self.env)
		env.update(extra_env or {})
		return subprocess.run(["bash", "-c", preamble + body], env=env, capture_output=True, text=True, timeout=120)

	def train_fn(self, body: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
		"""Source review_merge_train.sh without its subcommand dispatch, then run body."""
		script = TRAIN.read_text()
		cut = script.index('case "${MT_SUBCOMMAND}" in\n\tgate) _mt_gate ;;')
		lib_path = self.dir / "train_lib.sh"
		lib_path.write_text(script[:cut])
		env = dict(self.env)
		env["SUPPORT_SCRIPTS_DIR"] = str(REPO_ROOT / "scripts")
		env.update(extra_env or {})
		return subprocess.run(["bash", "-c", f"source {str(lib_path)!r}\nset +e\n{body}"], env=env,
			capture_output=True, text=True, timeout=120)

	@property
	def state(self) -> dict:
		return json.loads(self.state_path.read_text())

	@property
	def calls(self) -> list[str]:
		return self.log_path.read_text().splitlines()

	def record(self) -> str:
		st = self.state
		return st["commits"][st["refs"][REF]]["message"]


def _gate(h: _Harness, pr: int, mode: str, extra_env: dict[str, str] | None = None) -> tuple[int, str]:
	res = h.lib(f'_merge_lease_gate {pr} main {HEAD} {mode}; rc=$?; echo "rc=$rc outcome=$MERGE_LEASE_OUTCOME reason=$MERGE_LEASE_REASON blocker=$MERGE_LEASE_BLOCKER"', extra_env)
	assert res.returncode == 0, res.stderr
	line = [ln for ln in res.stdout.splitlines() if ln.startswith("rc=")][-1]
	return int(line.split()[0][3:]), res.stdout + res.stderr


class LeaseGate(unittest.TestCase):
	def test_first_update_creates_the_lease_ref_with_the_holders_paths(self) -> None:
		h = _Harness({"pulls": [_pull(7)], "files": {"7": ["scripts/a.sh", "tests/test_a.py"]}})
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=acquired", out)
		rec = h.record()
		self.assertIn("state=held", rec)
		self.assertIn("pr=7", rec)
		self.assertIn(f"head={HEAD}", rec)
		self.assertTrue(rec.endswith("paths:\nscripts/a.sh\ntests/test_a.py"), rec)
		self.assertTrue(any("-X POST repos/owner/repo/git/refs" in c and f"ref={REF}" in c for c in h.calls))

	def test_update_waits_while_another_pr_holds_the_lease(self) -> None:
		st = {"pulls": [_pull(7), _pull(8)]}
		_seed_lease(st, _record(8))
		h = _Harness(st)
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=wait reason=held blocker=8", out)
		self.assertIn("ai:merge-lease-wait", h.state["labels"]["7"])
		self.assertEqual(h.state["refs"][REF], "c" * 40, "a waiter never writes the lease")

	def test_holder_renews_its_own_lease(self) -> None:
		st = {"pulls": [_pull(8)], "files": {"8": ["shared.py"]}}
		_seed_lease(st, _record(8, expires=int(time.time()) + 60))
		h = _Harness(st)
		rc, out = _gate(h, 8, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=renewed", out)
		expires = int(re.search(r"^expires=(\d+)$", h.record(), re.M).group(1))
		self.assertGreater(expires, int(time.time()) + 3600, "renewal extends to a full TTL (240 min default)")
		self.assertEqual(h.state["commits"][h.state["refs"][REF]]["parents"], ["c" * 40])

	def test_holder_past_the_max_hold_stops_renewing(self) -> None:
		st = {"pulls": [_pull(8)], "files": {"8": ["shared.py"]}}
		rec = _record(8, expires=int(time.time()) + 60).replace("\nupdated=", f"\nacquired={int(time.time()) - 13 * 3600}\nupdated=")
		_seed_lease(st, rec)
		h = _Harness(st)
		rc, out = _gate(h, 8, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("reason=max_hold_reached", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40, "no renewal write")

	def test_renewal_keeps_the_first_acquisition_time(self) -> None:
		st = {"pulls": [_pull(8)], "files": {"8": ["shared.py"]}}
		first = int(time.time()) - 3600
		_seed_lease(st, _record(8).replace("\nupdated=", f"\nacquired={first}\nupdated="))
		h = _Harness(st)
		rc, out = _gate(h, 8, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn(f"acquired={first}", h.record())

	def test_expired_lease_is_taken_over(self) -> None:
		st = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["x.py"]}}
		_seed_lease(st, _record(8, expires=int(time.time()) - 5))
		h = _Harness(st)
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=acquired", out)
		self.assertIn("pr=7", h.record())

	def test_free_lease_goes_to_the_first_waiter_not_a_newcomer(self) -> None:
		st = {"pulls": [_pull(5), _pull(7)], "labels": {"5": ["ai:merge-lease-wait"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		h = _Harness(st)
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=queued_behind blocker=5", out)

	def test_priority_label_orders_before_a_lower_number(self) -> None:
		st = {"pulls": [_pull(5), _pull(9)], "labels": {"5": ["ai:merge-lease-wait"], "9": ["ai:merge-lease-wait", "ai:security"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		h = _Harness(st)
		rc, out = _gate(h, 5, "update")
		self.assertEqual(rc, 1, out)
		self.assertIn("blocker=9", out)

	def test_waiter_that_acquires_loses_its_wait_label(self) -> None:
		st = {"pulls": [_pull(5)], "labels": {"5": ["ai:merge-lease-wait"]}, "files": {"5": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		h = _Harness(st)
		rc, out = _gate(h, 5, "update")
		self.assertEqual(rc, 0, out)
		self.assertNotIn("ai:merge-lease-wait", h.state["labels"]["5"])

	def test_lost_compare_and_swap_re_decides_on_the_winning_record(self) -> None:
		"""Two runs read the same free record; the peer's PATCH lands first."""
		st = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		winner = "d" * 40
		st["commits"][winner] = {"message": _record(8), "tree": "f" * 40, "parents": ["c" * 40]}
		st["move_ref_on_patch"] = {REF: winner}
		h = _Harness(st)
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=retry reason=lost_race", out)
		self.assertIn("reason=held blocker=8", out)
		self.assertEqual(h.state["refs"][REF], winner, "the fast-forward-only PATCH refused the loser")

	def test_merge_waits_only_when_it_shares_a_path_with_the_holder(self) -> None:
		st = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["shared.py", "own.py"]}}
		_seed_lease(st, _record(8, paths="shared.py\nother.py"))
		h = _Harness(st)
		rc, out = _gate(h, 7, "merge")
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=holder_shares_paths blocker=8", out)
		st2 = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["own.py"]}}
		_seed_lease(st2, _record(8, paths="shared.py"))
		h2 = _Harness(st2)
		rc2, out2 = _gate(h2, 7, "merge")
		self.assertEqual(rc2, 0, out2)
		self.assertIn("outcome=no_overlap", out2)

	def test_star_paths_block_every_merge(self) -> None:
		st = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["own.py"]}}
		_seed_lease(st, _record(8, paths="*"))
		h = _Harness(st)
		rc, _ = _gate(h, 7, "merge")
		self.assertEqual(rc, 1)

	def test_unreadable_lease_fails_open(self) -> None:
		h = _Harness({"fail": ["git/matching-refs"]})
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=unavailable reason=read_failed", out)

	def test_unwritable_lease_fails_open(self) -> None:
		h = _Harness({"pulls": [_pull(7)], "fail": ["git/refs"]})
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("reason=write_failed", out)

	def test_disabled_lease_makes_no_call(self) -> None:
		h = _Harness({})
		rc, out = _gate(h, 7, "update", {"MERGE_LEASE_ENABLED": "false"})
		self.assertEqual(rc, 0)
		self.assertIn("outcome=disabled", out)
		self.assertEqual(h.calls, [])


class FreshnessIntegration(unittest.TestCase):
	def _compare(self, files: list[str]) -> dict:
		return {"ahead_by": 2, "files": [{"filename": f} for f in files], "commits": [{"sha": "e" * 40}, {"sha": "9" * 40}]}

	def test_overlap_without_the_lease_does_not_update_the_branch(self) -> None:
		"""The #9367 shape: main moved under ci.yml, another PR holds the lease."""
		st = {"pulls": [_pull(7), _pull(8)], "files": {"7": [".github/workflows/ci.yml"]},
			"compare": self._compare([".github/workflows/ci.yml"])}
		_seed_lease(st, _record(8))
		h = _Harness(st)
		res = h.lib(f'_pr_base_fresh_for_merge 7 {HEAD} main; echo "rc=$? outcome=$PR_BASE_FRESHNESS_OUTCOME"')
		self.assertIn("rc=1 outcome=lease_wait", res.stdout, res.stdout + res.stderr)
		self.assertNotIn("updated", h.state, "no update-branch call for a waiter")

	def test_overlap_with_the_lease_updates_the_branch(self) -> None:
		st = {"pulls": [_pull(7)], "files": {"7": [".github/workflows/ci.yml"]},
			"compare": self._compare([".github/workflows/ci.yml"])}
		h = _Harness(st)
		res = h.lib(f'_pr_base_fresh_for_merge 7 {HEAD} main; echo "rc=$?"')
		self.assertIn("rc=1", res.stdout)
		self.assertEqual(h.state.get("updated"), ["7"], res.stdout + res.stderr)
		self.assertIn("pr=7", h.record())
		# The PR files were read once, by the freshness check; the lease reused them.
		self.assertEqual(sum("/pulls/7/files" in c for c in h.calls), 1, h.calls)

	def test_clean_merge_sharing_the_holders_file_waits(self) -> None:
		st = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["shared.py"]}, "compare": self._compare(["unrelated.md"])}
		_seed_lease(st, _record(8, paths="shared.py"))
		h = _Harness(st)
		res = h.lib(f'_pr_base_fresh_for_merge 7 {HEAD} main; echo "rc=$? outcome=$PR_BASE_FRESHNESS_OUTCOME"')
		self.assertIn("rc=1 outcome=lease_wait", res.stdout, res.stdout + res.stderr)
		self.assertIn("action=lease_wait", res.stdout)


class FinalCiPendingMark(unittest.TestCase):
	def test_mark_posts_a_pending_status_and_the_index_label(self) -> None:
		h = _Harness({})
		res = h.lib(f"_pr_final_ci_merge_pending_mark 7 {HEAD}")
		self.assertIn("outcome=marked", res.stdout, res.stderr)
		st = h.state
		self.assertEqual(st["statuses"][HEAD][0]["context"], "ai-review/final-ci-merge-pending")
		self.assertEqual(st["statuses"][HEAD][0]["state"], "pending")
		self.assertIn("ai:final-ci-merge-pending", st["labels"]["7"])

	def test_mark_can_be_switched_off(self) -> None:
		h = _Harness({})
		res = h.lib(f"_pr_final_ci_merge_pending_mark 7 {HEAD}", {"FINAL_CI_MERGE_RESUME_ENABLED": "false"})
		self.assertIn("outcome=disabled", res.stdout)
		self.assertEqual(h.calls, [])

	def test_every_timeout_exit_of_the_wait_marks_only_with_a_real_budget(self) -> None:
		text = LIB.read_text()
		start = text.index("_pr_wait_for_required_checks()\n{")
		body = text[start:text.index("\n}\n", start)]
		timeouts = re.findall(r'echo "AUTOFIX_AUTO_MERGE_CHECKS ([^\n]*outcome=timeout[^\n]*)\n\t*([^\n]*)', body)
		# A failed final CI dispatch leaves no run to wait for, so that exit must not mark.
		marking = [follow for line, follow in timeouts if "reason=final_ci_dispatch_failed" not in line]
		unmarked = [follow for line, follow in timeouts if "reason=final_ci_dispatch_failed" in line]
		self.assertEqual(len(marking), 3)
		for follow in unmarked:
			self.assertNotIn("_pr_final_ci_merge_pending_mark", follow)
		for follow in marking:
			self.assertEqual(follow.strip(), 'if [ "${max_minutes}" -gt 0 ]; then _pr_final_ci_merge_pending_mark "${pr_number}" "${head_sha}"; fi')


class ReleaseTick(unittest.TestCase):
	def _prs_jsonl(self, pulls: list[dict], labels: dict[str, list[str]]) -> str:
		return "\n".join(json.dumps({"number": p["number"], "head": p["head"]["ref"], "head_repo": p["head"]["repo"]["full_name"],
			"head_sha": p["head"]["sha"], "base": p["base"]["ref"], "draft": False,
			"labels": labels.get(str(p["number"]), [])}) for p in pulls)

	def test_closed_holder_is_released_and_the_first_waiter_granted(self) -> None:
		pulls = [_pull(5), _pull(6)]
		labels = {"5": ["ai:merge-lease-wait"], "6": ["ai:merge-lease-wait"]}
		st = {"pulls": pulls, "labels": labels, "files": {"5": ["shared.py"]}, "closed": [4]}
		_seed_lease(st, _record(4))  # PR 4 is not open any more
		h = _Harness(st)
		res = h.train_fn('_mt_merge_lease_tick "${PRS_JSONL}" ""', {"PRS_JSONL": self._prs_jsonl(pulls, labels)})
		out = res.stdout + res.stderr
		self.assertIn("outcome=released reason=holder_closed", out)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted", out)
		rec = h.record()
		self.assertIn("pr=5", rec)
		self.assertIn("state=held", rec)
		self.assertNotIn("ai:merge-lease-wait", h.state["labels"]["5"])
		self.assertIn("ai:merge-lease-wait", h.state["labels"]["6"])
		self.assertTrue(any("pr_number=5" in " ".join(d) for d in h.state.get("dispatched", [])))

	def test_holder_missing_from_a_stale_listing_but_open_keeps_the_lease(self) -> None:
		"""The open-PR list predates the lease read; a live read decides."""
		pulls = [_pull(5)]
		labels = {"5": ["ai:merge-lease-wait"]}
		st = {"pulls": pulls + [_pull(4)], "labels": labels}
		_seed_lease(st, _record(4))
		h = _Harness(st)
		res = h.train_fn('_mt_merge_lease_tick "${PRS_JSONL}" ""', {"PRS_JSONL": self._prs_jsonl(pulls, labels)})
		self.assertIn("outcome=held reason=holder_open", res.stdout, res.stderr)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_open_unexpired_holder_keeps_the_lease(self) -> None:
		pulls = [_pull(4), _pull(5)]
		labels = {"5": ["ai:merge-lease-wait"]}
		st = {"pulls": pulls, "labels": labels}
		_seed_lease(st, _record(4))
		h = _Harness(st)
		res = h.train_fn('_mt_merge_lease_tick "${PRS_JSONL}" ""', {"PRS_JSONL": self._prs_jsonl(pulls, labels)})
		self.assertIn("outcome=held reason=holder_open", res.stdout, res.stderr)
		self.assertEqual(h.state["refs"][REF], "c" * 40)
		self.assertNotIn("dispatched", h.state)

	def _resume(self, statuses: list[dict], checks_rc: int = 0, checks_outcome: str = "ok", fresh_rc: int = 0,
			ci_state: str = "success", extra_labels: list[str] | None = None, env: dict[str, str] | None = None) -> _Harness:
		pulls = [_pull(7, sha=HEAD)]
		labels = {"7": ["ai:final-ci-merge-pending"] + (extra_labels or [])}
		st = {"pulls": pulls, "labels": labels, "user": "pipeline-bot", "statuses": {HEAD: statuses},
			"graphql": {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [{"number": 70}]}}}}}}
		h = _Harness(st)
		stubs = (f"_pr_wait_for_required_checks() {{ PR_CHECKS_WAIT_OUTCOME={checks_outcome}; PR_CHECKS_LAST_CI_STATE={ci_state}; echo \"WAIT_ARGS=$*\"; return {checks_rc}; }}\n"
			f"_pr_base_fresh_for_merge() {{ PR_BASE_FRESHNESS_OUTCOME=lease_wait; return {fresh_rc}; }}\n")
		res = h.train_fn(stubs + '_mt_final_ci_merge_resume "${PRS_JSONL}" ""', {"PRS_JSONL": self._prs_jsonl(pulls, labels), **(env or {})})
		h.out = res.stdout + res.stderr  # type: ignore[attr-defined]
		return h

	MARK = {"context": "ai-review/final-ci-merge-pending", "state": "pending", "creator": {"login": "pipeline-bot"}}
	GATE = {"context": "ai-review/head-gate", "state": "success", "creator": {"login": "Pipeline-Bot"}}

	def test_green_final_ci_enables_head_bound_auto_merge(self) -> None:
		h = self._resume([self.MARK, self.GATE])
		self.assertIn("outcome=auto_merge_enabled", h.out)  # type: ignore[attr-defined]
		self.assertIn(f"WAIT_ARGS=7 {HEAD} main 0", h.out)  # type: ignore[attr-defined]
		merged = h.state["merged"][0]
		self.assertIn("--match-head-commit", merged)
		self.assertIn(HEAD, merged)
		self.assertIn("--squash", merged)
		self.assertNotIn("ai:final-ci-merge-pending", h.state["labels"]["7"])
		self.assertIn("ai:ready-to-merge", h.state["labels"]["70"])
		self.assertEqual(h.state["statuses"][HEAD][0]["state"], "success")

	def test_marker_from_another_login_authorizes_nothing(self) -> None:
		forged = dict(self.MARK, creator={"login": "someone-else"})
		h = self._resume([forged, self.GATE])
		self.assertIn("reason=no_marker_on_head", h.out)  # type: ignore[attr-defined]
		self.assertNotIn("merged", h.state)

	def test_head_gate_not_green_keeps_waiting(self) -> None:
		h = self._resume([self.MARK, dict(self.GATE, state="pending")])
		self.assertIn("reason=head_gate_not_success", h.out)  # type: ignore[attr-defined]
		self.assertNotIn("merged", h.state)
		self.assertIn("ai:final-ci-merge-pending", h.state["labels"]["7"])

	def test_ci_still_running_keeps_waiting_and_failure_drops(self) -> None:
		h = self._resume([self.MARK, self.GATE], checks_rc=1, checks_outcome="timeout")
		self.assertIn("outcome=waiting reason=required_checks_timeout", h.out)  # type: ignore[attr-defined]
		self.assertIn("ai:final-ci-merge-pending", h.state["labels"]["7"])
		h2 = self._resume([self.MARK, self.GATE], checks_rc=1, checks_outcome="failed")
		self.assertIn("reason=required_checks_failed", h2.out)  # type: ignore[attr-defined]
		self.assertNotIn("ai:final-ci-merge-pending", h2.state["labels"]["7"])
		self.assertNotIn("merged", h2.state)

	def test_auto_merge_kill_switch_drops_the_label(self) -> None:
		h = self._resume([self.MARK, self.GATE], env={"ENABLE_AUTO_MERGE": "false"})
		self.assertIn("outcome=dropped reason=auto_merge_disabled", h.out)  # type: ignore[attr-defined]
		self.assertNotIn("merged", h.state)
		self.assertNotIn("ai:final-ci-merge-pending", h.state["labels"]["7"])

	def test_a_hold_label_added_after_the_mark_wins(self) -> None:
		for hold in ("ai:needs-human", "ai:review-blocked", "ai:merge-queued", "ai:security-pass-failed"):
			h = self._resume([self.MARK, self.GATE], extra_labels=[hold])
			self.assertIn(f"reason=blocked_by_{hold}", h.out)  # type: ignore[attr-defined]
			self.assertNotIn("merged", h.state)

	def test_no_named_ci_success_never_resumes(self) -> None:
		"""A base with no required CI check reads "no check-runs" as green; the tick does not."""
		h = self._resume([self.MARK, self.GATE], ci_state="absent")
		self.assertIn("reason=no_named_ci_success", h.out)  # type: ignore[attr-defined]
		self.assertNotIn("merged", h.state)

	def test_lease_or_freshness_defers_the_merge(self) -> None:
		h = self._resume([self.MARK, self.GATE], fresh_rc=1)
		self.assertIn("outcome=deferred reason=lease_wait", h.out)  # type: ignore[attr-defined]
		self.assertNotIn("merged", h.state)


class Wiring(unittest.TestCase):
	def test_sweep_skips_lease_waiters_only_while_the_train_releases_them(self) -> None:
		text = SWEEP.read_text()
		self.assertIn("reason=merge_lease_wait", text)
		self.assertIn('if [ "${merge_train_skip_active}" = "true" ] && printf \'%s\' "${row}" | jq -e --arg l "${MERGE_LEASE_WAIT_LABEL:-ai:merge-lease-wait}"', text)
		self.assertIn("MERGE_LEASE_ENABLED: ${{ vars.MERGE_LEASE_ENABLED || 'true' }}", text)

	def test_release_steps_stage_the_library(self) -> None:
		for wf in (POLL, CLOSE):
			text = wf.read_text()
			self.assertIn("for helper in review_merge_train.sh label_helpers.sh pr_checks_lib.sh; do", text, wf.name)
			self.assertIn("MERGE_LEASE_TTL_MINUTES: ${{ vars.MERGE_LEASE_TTL_MINUTES || '240' }}", text, wf.name)

	def test_merge_paths_get_the_lease_switches(self) -> None:
		self.assertEqual(REVIEW.read_text().count("MERGE_LEASE_ENABLED: ${{ vars.MERGE_LEASE_ENABLED || 'true' }}"), 3)

	def test_release_steps_get_the_review_paths_switches(self) -> None:
		for wf in (POLL, CLOSE):
			text = wf.read_text()
			for name in ("ENABLE_AUTO_MERGE", "AUTO_MERGE_REQUIRED_CI_CHECK", "MERGE_BASE_FRESHNESS_ENABLED",
					"MERGE_BASE_FRESHNESS_MAX_SYNCS", "ORCH_INTEGRATION_BRANCH_PATTERN", "AUTO_MERGE_WAIT_WORKFLOWS"):
				self.assertIn(f"          {name}: ${{{{ vars.{name}", text, f"{wf.name}: {name}")

	def test_rb_judge_waits_never_mark(self) -> None:
		"""The judge posts head-gate success only after its wait, so its marker could never resume."""
		text = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text()
		calls = re.findall(r"^.*\b_pr_wait_for_required_checks \"\$\{PR_NUMBER\}\".*$", text, re.M)
		self.assertEqual(len(calls), 2)
		for call in calls:
			self.assertIn("FINAL_CI_MERGE_RESUME_ENABLED=false _pr_wait_for_required_checks", call)

	def test_release_runs_both_tick_passes(self) -> None:
		text = TRAIN.read_text()
		self.assertIn('_mt_merge_lease_tick "${prs_json}" "${base_filter}" || true', text)
		self.assertIn('_mt_final_ci_merge_resume "${prs_json}" "${base_filter}" || true', text)


if __name__ == "__main__":
	unittest.main()

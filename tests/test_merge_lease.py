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


def _record(pr: int, *, state: str = "held", expires: int | None = None, paths: str = "shared.py",
		acquired: int | None = None, updated: int | None = None, skips: list[str] | None = None) -> str:
	if expires is None:
		expires = int(time.time()) + 3600
	acquired_line = "" if acquired is None else f"acquired={acquired}\n"
	skip_lines = "".join(f"skip={s}\n" for s in (skips or []))
	return (f"merge-lease v1\nstate={state}\nbase=main\npr={pr}\nhead={'b' * 40}\n"
		f"expires={expires}\n{acquired_line}updated={int(time.time()) if updated is None else updated}\n{skip_lines}paths:\n{paths}")


def _head_gate(st: dict, sha: str, state: str = "success", login: str = "pipeline-bot") -> None:
	"""Seed the newest ai-review/head-gate status on <sha> (merge-lease Phase 2:
	a waiter is granted only with a pipeline-posted success)."""
	st.setdefault("statuses", {}).setdefault(sha, []).insert(0,
		{"context": "ai-review/head-gate", "state": state, "creator": {"login": login}})


def _gates_ok(st: dict, *nums: int) -> None:
	for num in nums:
		_head_gate(st, f"{num:040d}")


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
		_gates_ok(st, 5, 6)
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



def _metrics(out: str) -> list[dict[str, str]]:
	"""Parse every MERGE_LEASE_METRIC line into a dict of its fields."""
	rows = []
	for line in out.splitlines():
		if line.startswith("MERGE_LEASE_METRIC "):
			rows.append(dict(field.split("=", 1) for field in line.split()[1:]))
	return rows


def _stable_record(record: str) -> str:
	"""A lease record without its clock fields, for on/off comparisons."""
	return re.sub(r"^(expires|acquired|updated)=\d+$", r"\1=N", record, flags=re.MULTILINE)


def _stable_calls(calls: list[str]) -> list[str]:
	"""The fake gh call log without clock fields (record bodies are logged) or
	the record commit SHAs that hash them; endpoints, order and count remain."""
	return [re.sub(r"\b[0-9a-f]{40}\b", "SHA", re.sub(r"(expires|acquired|updated)=\d+", r"\1=N", c)) for c in calls]


class LeaseGateMetrics(unittest.TestCase):
	"""Merge-lease parallel merges Phase 1 (Refs #9781): metrics are additive and
	never change a gate decision, return code or API call."""

	def test_acquired_emits_a_metric_and_keeps_the_lease_line_byte_for_byte(self) -> None:
		h = _Harness({"pulls": [_pull(7)], "files": {"7": ["a.py"]}})
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("MERGE_LEASE pr=7 base=main mode=update outcome=acquired reason=ttl_s=14400 holder=7 blocker=-\n", out)
		rows = _metrics(out)
		self.assertEqual(len(rows), 1, out)
		row = rows[0]
		self.assertEqual({k: row[k] for k in ("event", "base", "pr", "mode", "reason", "holder", "hold_s", "waiters")},
			{"event": "acquired", "base": "main", "pr": "7", "mode": "update", "reason": "acquired", "holder": "7",
				"hold_s": "0", "waiters": "-"})
		self.assertRegex(row["ts"], r"^\d+$")
		self.assertNotIn("a.py", "\n".join(ln for ln in out.splitlines() if ln.startswith("MERGE_LEASE_METRIC")))

	def test_renewed_reports_the_hold_since_first_acquisition(self) -> None:
		st = {"pulls": [_pull(8)], "files": {"8": ["shared.py"]}}
		_seed_lease(st, _record(8, acquired=int(time.time()) - 600))
		h = _Harness(st)
		rc, out = _gate(h, 8, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=renewed", out)
		row = _metrics(out)[0]
		self.assertEqual((row["event"], row["reason"], row["holder"]), ("renewed", "renewed", "8"))
		self.assertGreaterEqual(int(row["hold_s"]), 600)

	def test_renewal_of_a_record_without_acquired_has_no_hold(self) -> None:
		st = {"pulls": [_pull(8)], "files": {"8": ["shared.py"]}}
		_seed_lease(st, _record(8))
		h = _Harness(st)
		_, out = _gate(h, 8, "update")
		self.assertEqual(_metrics(out)[0]["hold_s"], "-")

	def test_wait_held_and_holder_shares_paths(self) -> None:
		st = {"pulls": [_pull(7), _pull(4)], "files": {"7": ["shared.py"]}}
		_seed_lease(st, _record(4))
		h = _Harness(st)
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=wait reason=held blocker=4", out)
		row = _metrics(out)[0]
		self.assertEqual((row["event"], row["reason"], row["holder"], row["pr"], row["mode"]), ("wait", "held", "4", "7", "update"))
		st2 = {"pulls": [_pull(7), _pull(4)], "files": {"7": ["shared.py"]}}
		_seed_lease(st2, _record(4))
		h2 = _Harness(st2)
		rc2, out2 = _gate(h2, 7, "merge")
		self.assertEqual(rc2, 1, out2)
		self.assertIn("reason=holder_shares_paths blocker=4", out2)
		row2 = _metrics(out2)[0]
		self.assertEqual((row2["event"], row2["reason"], row2["mode"]), ("wait", "holder_shares_paths", "merge"))

	def test_queued_behind_and_merge_proceed(self) -> None:
		st = {"pulls": [_pull(5), _pull(7)], "labels": {"5": ["ai:merge-lease-wait"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		h = _Harness(st)
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=queued_behind blocker=5", out)
		self.assertEqual((_metrics(out)[0]["event"], _metrics(out)[0]["reason"]), ("wait", "queued_behind"))
		st2 = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["own.py"]}}
		_seed_lease(st2, _record(8, paths="shared.py"))
		h2 = _Harness(st2)
		rc2, out2 = _gate(h2, 7, "merge")
		self.assertEqual(rc2, 0, out2)
		self.assertEqual((_metrics(out2)[0]["event"], _metrics(out2)[0]["reason"]), ("proceed", "no_shared_paths_with_holder"))

	def test_unavailable_read_failed_keeps_failing_open(self) -> None:
		h = _Harness({"fail": ["git/matching-refs"]})
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=unavailable reason=read_failed", out)
		self.assertEqual((_metrics(out)[0]["event"], _metrics(out)[0]["reason"]), ("unavailable", "read_failed"))

	def test_lost_race_emits_retry_then_the_final_decision(self) -> None:
		st = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		winner = "d" * 40
		st["commits"][winner] = {"message": _record(8), "tree": "f" * 40, "parents": ["c" * 40]}
		st["move_ref_on_patch"] = {REF: winner}
		h = _Harness(st)
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 1, out)
		self.assertEqual([(r["event"], r["reason"]) for r in _metrics(out)], [("retry", "lost_race"), ("wait", "held")])

	def test_own_expired_hold_reacquired_is_a_new_hold(self) -> None:
		st = {"pulls": [_pull(8)], "files": {"8": ["shared.py"]}}
		_seed_lease(st, _record(8, expires=int(time.time()) - 5, acquired=int(time.time()) - 600))
		h = _Harness(st)
		rc, out = _gate(h, 8, "update")
		self.assertEqual(rc, 0, out)
		# The MERGE_LEASE line and the gate outcome are unchanged.
		self.assertIn("outcome=renewed", out)
		self.assertIn("rc=0 outcome=renewed reason=renewed", out)
		row = _metrics(out)[0]
		self.assertEqual((row["event"], row["reason"], row["hold_s"], row["holder"]), ("acquired", "acquired", "0", "8"))

	def test_invalid_input_has_no_holder_even_after_an_earlier_read(self) -> None:
		st = {"pulls": [_pull(4)]}
		_seed_lease(st, _record(4))
		h = _Harness(st)
		res = h.lib(f'_merge_lease_read main || true; _merge_lease_gate 7 "" {HEAD} update; echo "rc=$?"')
		self.assertIn("rc=0", res.stdout, res.stderr)
		row = _metrics(res.stdout)[0]
		self.assertEqual((row["event"], row["reason"], row["holder"]), ("unavailable", "invalid_input", "-"))

	def test_helper_uses_a_passed_decision_timestamp(self) -> None:
		h = _Harness({})
		res = h.lib('_merge_lease_metric granted main 5 tick first_in_queue 0 1 5 1234567890')
		self.assertEqual(_metrics(res.stdout)[0]["ts"], "1234567890")
		res = h.lib('_merge_lease_metric granted main 5 tick first_in_queue 0 1 5 "x1"')
		self.assertRegex(_metrics(res.stdout)[0]["ts"], r"^\d+$")

	def test_disabled_lease_emits_no_metric(self) -> None:
		h = _Harness({})
		_, out = _gate(h, 7, "update", {"MERGE_LEASE_ENABLED": "false"})
		self.assertEqual(_metrics(out), [])

	def _scenarios(self) -> list[tuple[dict, int, str]]:
		acquire = {"pulls": [_pull(7)], "files": {"7": ["a.py"]}}
		wait = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["shared.py"]}}
		_seed_lease(wait, _record(8))
		queued = {"pulls": [_pull(5), _pull(7)], "labels": {"5": ["ai:merge-lease-wait"]}}
		_seed_lease(queued, _record(3, state="released", paths=""))
		return [(acquire, 7, "update"), (wait, 7, "update"), (wait, 7, "merge"), (queued, 7, "update")]

	def test_kill_switch_changes_nothing_but_the_metric_lines(self) -> None:
		for st, pr, mode in self._scenarios():
			on = _Harness(json.loads(json.dumps(st)))
			off = _Harness(json.loads(json.dumps(st)))
			rc_on, out_on = _gate(on, pr, mode)
			rc_off, out_off = _gate(off, pr, mode, {"MERGE_LEASE_METRICS_ENABLED": "false"})
			self.assertEqual(rc_on, rc_off)
			self.assertTrue(_metrics(out_on), out_on)
			self.assertEqual(_metrics(out_off), [])
			def strip(o: str) -> list[str]:
				return [ln for ln in o.splitlines() if not ln.startswith("MERGE_LEASE_METRIC")]
			self.assertEqual(strip(out_on), strip(out_off))
			self.assertEqual(_stable_calls(on.calls), _stable_calls(off.calls), "metrics make no GitHub call")
			self.assertEqual(on.state.get("labels"), off.state.get("labels"))
			if REF in on.state.get("refs", {}):
				self.assertEqual(_stable_record(on.record()), _stable_record(off.record()))

	def test_a_failing_metric_helper_never_changes_the_gate(self) -> None:
		for st, pr, mode in self._scenarios():
			ref = _Harness(json.loads(json.dumps(st)))
			broken = _Harness(json.loads(json.dumps(st)))
			_, out_ref = _gate(ref, pr, mode)
			res = broken.lib(f'_merge_lease_metric() {{ echo boom >&2; return 7; }}\n_merge_lease_gate {pr} main {HEAD} {mode}; rc=$?; '
				'echo "rc=$rc outcome=$MERGE_LEASE_OUTCOME reason=$MERGE_LEASE_REASON blocker=$MERGE_LEASE_BLOCKER"')
			self.assertEqual(res.returncode, 0, res.stderr)
			final_ref = [ln for ln in out_ref.splitlines() if ln.startswith("rc=")][-1]
			final_broken = [ln for ln in res.stdout.splitlines() if ln.startswith("rc=")][-1]
			self.assertEqual(final_ref, final_broken)
			self.assertEqual(_stable_calls(ref.calls), _stable_calls(broken.calls))

	def test_helper_survives_a_failing_clock_and_sanitizes_fields(self) -> None:
		h = _Harness({})
		res = h.lib('date() { return 1; }\nset -e\n_merge_lease_metric "wait x=1" "main extra=1" "7 pr=9" update "r=1" "12a" "3"; echo "rc=$?"')
		self.assertIn("rc=0", res.stdout, res.stderr)
		row = _metrics(res.stdout)[0]
		self.assertEqual(row, {"event": "-", "base": "-", "pr": "-", "mode": "update", "reason": "-", "holder": "-",
			"hold_s": "-", "waiters": "3", "ts": "-"})
		self.assertEqual(h.calls, [])


class TickMetrics(unittest.TestCase):
	_prs_jsonl = ReleaseTick._prs_jsonl

	def _tick(self, st: dict, pulls: list[dict], labels: dict[str, list[str]], prelude: str = "",
			env: dict[str, str] | None = None) -> tuple[_Harness, str]:
		h = _Harness(st)
		res = h.train_fn(prelude + '_mt_merge_lease_tick "${PRS_JSONL}" ""',
			{"PRS_JSONL": self._prs_jsonl(pulls, labels), **(env or {})})
		return h, res.stdout + res.stderr

	def test_release_of_a_closed_holder_then_grant(self) -> None:
		pulls = [_pull(5), _pull(6)]
		labels = {"5": ["ai:merge-lease-wait"], "6": ["ai:merge-lease-wait"]}
		st = {"pulls": pulls, "labels": labels, "files": {"5": ["shared.py"]}, "closed": [4]}
		_seed_lease(st, _record(4, acquired=int(time.time()) - 900))
		_gates_ok(st, 5, 6)
		h, out = self._tick(st, pulls, labels)
		rows = _metrics(out)
		self.assertEqual([r["event"] for r in rows], ["released", "granted"], out)
		released, granted = rows
		self.assertEqual((released["pr"], released["reason"], released["mode"], released["waiters"]), ("4", "holder_closed", "tick", "2"))
		self.assertGreaterEqual(int(released["hold_s"]), 900)
		self.assertEqual((granted["pr"], granted["holder"], granted["reason"], granted["waiters"], granted["hold_s"]),
			("5", "5", "first_in_queue", "2", "0"))
		self.assertIn("pr=5", h.record())
		# Phase 1 step 1.1: the release line gains held_s= and merged=, the
		# grant line queue_len= (eligible waiters, the granted PR included).
		self.assertRegex(out, r"MERGE_LEASE pr=4 base=main mode=tick outcome=released reason=holder_closed held_s=(\d+) merged=false\n")
		held = int(re.search(r"outcome=released reason=holder_closed held_s=(\d+)", out).group(1))
		self.assertGreaterEqual(held, 900)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted reason=first_in_queue queue_len=2\n", out)
		# Step 1.3: one queue line for the ref, after the grant; the holder is
		# not in the order.
		queue = [ln for ln in out.splitlines() if ln.startswith("MERGE_LEASE_QUEUE ")]
		self.assertEqual(len(queue), 1, out)
		self.assertRegex(queue[0], r"^MERGE_LEASE_QUEUE base=main holder=5 held_s=\d+ waiters=1 order=6$")

	def test_released_metric_reports_whether_the_holder_merged(self) -> None:
		"""Plan K1 (wasted-hold share): the closed-holder read also returns
		merged_at, so the released metric ends with merged=true|false; every
		other metric line and the MERGE_LEASE line are unchanged."""
		for merged in (True, False):
			with self.subTest(merged=merged):
				st = {"pulls": [_pull(5)], "closed": [4], "closed_merged": [4] if merged else []}
				_seed_lease(st, _record(4))
				h, out = self._tick(st, [_pull(5)], {})
				rows = _metrics(out)
				self.assertEqual(rows[0]["event"], "released", out)
				self.assertEqual(rows[0]["merged"], "true" if merged else "false")
				self.assertNotIn("merged", rows[1], out)
				self.assertIn(f"MERGE_LEASE pr=4 base=main mode=tick outcome=released reason=holder_closed held_s=- merged={'true' if merged else 'false'}\n", out)
				self.assertEqual(sum("pulls/4" in c for c in h.calls), 1, h.calls)
		st = {"pulls": [_pull(4)]}
		_seed_lease(st, _record(4, expires=int(time.time()) - 5))
		_, out = self._tick(st, [_pull(4)], {})
		self.assertEqual(_metrics(out)[0]["merged"], "false")

	def test_grant_timestamp_is_taken_before_unlabel_and_dispatch(self) -> None:
		pulls = [_pull(5)]
		labels = {"5": ["ai:merge-lease-wait"]}
		st = {"pulls": pulls, "labels": labels, "files": {"5": ["a"]}, "closed": [4]}
		_seed_lease(st, _record(4))
		_gates_ok(st, 5)
		# The stub records the clock as the dispatch starts, then sleeps. A grant
		# ts taken before the dispatch is <= that reading; one taken after it is
		# at least 3 s later. No bound on the (unbounded) pre-grant work.
		_, out = self._tick(st, pulls, labels,
			prelude='_mt_dispatch_review() { echo "DISPATCH_TS=$(date -u +%s)"; sleep 3; return 0; }\n')
		rows = _metrics(out)
		self.assertEqual([r["event"] for r in rows], ["released", "granted"], out)
		dispatch_ts = int(re.search(r"^DISPATCH_TS=(\d+)$", out, re.M).group(1))
		self.assertLessEqual(int(rows[1]["ts"]), dispatch_ts, out)

	def test_held_reports_queue_depth(self) -> None:
		pulls = [_pull(4), _pull(5)]
		labels = {"5": ["ai:merge-lease-wait"]}
		st = {"pulls": pulls, "labels": labels}
		_seed_lease(st, _record(4))
		h, out = self._tick(st, pulls, labels)
		row = _metrics(out)[0]
		self.assertEqual((row["event"], row["pr"], row["reason"], row["waiters"], row["hold_s"]), ("held", "4", "holder_open", "1", "-"))
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_no_waiter(self) -> None:
		pulls = [_pull(5)]
		st = {"pulls": pulls}
		_seed_lease(st, _record(3, state="released", paths=""))
		_, out = self._tick(st, pulls, {})
		row = _metrics(out)[0]
		self.assertEqual((row["event"], row["pr"], row["reason"], row["waiters"]), ("no_waiter", "-", "lease_free", "0"))
		self.assertEqual(row["holder"], "-", "a free lease has no holder, not the released record's pr")

	def test_expired_holder(self) -> None:
		pulls = [_pull(4)]
		st = {"pulls": pulls}
		_seed_lease(st, _record(4, expires=int(time.time()) - 5))
		_, out = self._tick(st, pulls, {})
		rows = _metrics(out)
		self.assertEqual((rows[0]["event"], rows[0]["reason"]), ("released", "expired"), out)
		self.assertEqual(rows[1]["event"], "no_waiter")
		self.assertEqual(rows[1]["holder"], "-")

	def test_library_without_the_metric_helper_still_releases_and_grants(self) -> None:
		pulls = [_pull(5)]
		labels = {"5": ["ai:merge-lease-wait"]}
		st = {"pulls": pulls, "labels": labels, "files": {"5": ["a"]}, "closed": [4]}
		_seed_lease(st, _record(4))
		_gates_ok(st, 5)
		h, out = self._tick(st, pulls, labels, prelude="unset -f _merge_lease_metric\n")
		self.assertEqual(_metrics(out), [])
		self.assertIn("outcome=released reason=holder_closed", out)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted", out)
		self.assertIn("pr=5", h.record())

	def test_kill_switch_keeps_tick_decisions_and_calls(self) -> None:
		pulls = [_pull(5)]
		labels = {"5": ["ai:merge-lease-wait"]}
		def state() -> dict:
			st = {"pulls": [_pull(5)], "labels": {"5": ["ai:merge-lease-wait"]}, "files": {"5": ["a"]}, "closed": [4]}
			_seed_lease(st, _record(4))
			_gates_ok(st, 5)
			return st
		on, out_on = self._tick(state(), pulls, labels)
		off, out_off = self._tick(state(), pulls, labels, env={"MERGE_LEASE_METRICS_ENABLED": "false"})
		self.assertEqual(_metrics(out_off), [])
		self.assertTrue(_metrics(out_on))
		def strip(o: str) -> list[str]:
			# held_s on the MERGE_LEASE_QUEUE line is clock-derived.
			return [re.sub(r"held_s=\d+", "held_s=N", ln) for ln in o.splitlines() if not ln.startswith("MERGE_LEASE_METRIC")]
		self.assertEqual(strip(out_on), strip(out_off))
		self.assertEqual(_stable_calls(on.calls), _stable_calls(off.calls))
		self.assertEqual(_stable_record(on.record()), _stable_record(off.record()))


WAIT = "ai:merge-lease-wait"


def _sha(num: int) -> str:
	return f"{num:040d}"


def _p2_jsonl(pulls: list[dict], labels: dict[str, list[str]]) -> str:
	"""The _mt_list_open_prs shape, with the Phase 2 title field."""
	return "\n".join(json.dumps({"number": p["number"], "head": p["head"]["ref"], "head_repo": p["head"]["repo"]["full_name"],
		"head_sha": p["head"]["sha"], "base": p["base"]["ref"], "draft": p.get("draft", False),
		"labels": labels.get(str(p["number"]), []), "title": p.get("title", "")}) for p in pulls)


def _p2_tick(st: dict, labels: dict[str, list[str]] | None = None, env: dict[str, str] | None = None,
		listing: list[dict] | None = None) -> tuple[_Harness, str]:
	labels = labels if labels is not None else st.get("labels", {})
	pulls = listing if listing is not None else st.get("pulls", [])
	h = _Harness(st)
	res = h.train_fn('_mt_merge_lease_tick "${PRS_JSONL}" ""', {"PRS_JSONL": _p2_jsonl(pulls, labels), **(env or {})})
	return h, res.stdout + res.stderr


def _skips(record: str) -> list[str]:
	return re.findall(r"^skip=(\S+)$", record, re.M)


OLD = int(time.time()) - 3 * 3600


class LeaseRevocation(unittest.TestCase):
	"""Merge-lease parallel merges Phase 2 (Refs #9781): the tick revokes the
	lease from a holder that can no longer merge."""

	def _holder(self, *, draft: bool = False, title: str = "", gate: str | None = "success", gate_login: str = "pipeline-bot",
			updated: int | None = None, extra: dict | None = None) -> dict:
		holder = dict(_pull(4), draft=draft, title=title)
		st = {"pulls": [holder]}
		_seed_lease(st, _record(4, updated=updated, acquired=updated))
		if gate is not None:
			_head_gate(st, _sha(4), gate, gate_login)
		st.update(extra or {})
		return st

	def _assert_revoked(self, h: _Harness, out: str, reason: str) -> None:
		self.assertRegex(out, rf"MERGE_LEASE pr=4 base=main mode=tick outcome=released reason={reason} held_s=(\d+|-) merged=false\n")
		rec = h.record()
		self.assertIn("state=released", rec)
		# Issue #9983 G6: the entry records the release reason.
		self.assertEqual(_skips(rec), [f"4:{_sha(4)}:{reason}"])
		self.assertIn(("released", reason), [(r["event"], r["reason"]) for r in _metrics(out)])

	def test_draft_holder_is_revoked(self) -> None:
		h, out = _p2_tick(self._holder(draft=True))
		self._assert_revoked(h, out, "revoked_draft")
		self.assertNotIn("statuses", " ".join(h.calls), "draft costs no API read")

	def test_skip_ai_title_revokes_and_the_title_is_never_logged(self) -> None:
		h, out = _p2_tick(self._holder(title="[Skip AI] SECRET_TITLE_TEXT"))
		self._assert_revoked(h, out, "revoked_skip_ai")
		self.assertNotIn("SECRET_TITLE_TEXT", out)

	def test_pipeline_head_gate_failure_revokes(self) -> None:
		h, out = _p2_tick(self._holder(gate="failure"))
		self._assert_revoked(h, out, "revoked_no_head_gate")

	def test_final_ci_failure_revokes(self) -> None:
		st = self._holder(extra={"check_runs": {_sha(4): [{"id": 1, "name": "tests", "status": "completed", "conclusion": "failure"}]}})
		h, out = _p2_tick(st, env={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "tests"})
		self._assert_revoked(h, out, "revoked_final_ci_failed")

	def test_missing_or_foreign_head_gate_is_held_within_the_idle_window(self) -> None:
		for gate, login in ((None, ""), ("success", "someone-else"), ("pending", "pipeline-bot")):
			h, out = _p2_tick(self._holder(gate=gate, gate_login=login or "pipeline-bot"))
			self.assertIn("outcome=held reason=holder_open", out, (gate, login))
			self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_missing_or_foreign_head_gate_is_revoked_after_the_idle_window(self) -> None:
		for gate, login in ((None, ""), ("success", "someone-else")):
			h, out = _p2_tick(self._holder(gate=gate, gate_login=login or "pipeline-bot", updated=OLD))
			self._assert_revoked(h, out, "revoked_no_head_gate")

	def test_idle_holder_without_running_ci_is_revoked(self) -> None:
		h, out = _p2_tick(self._holder(updated=OLD))
		self._assert_revoked(h, out, "revoked_idle")

	def test_idle_holder_with_a_running_ci_run_is_held(self) -> None:
		runs = {_sha(4): [{"id": 11, "name": "CI", "status": "in_progress", "event": "pull_request"}]}
		h, out = _p2_tick(self._holder(updated=OLD, extra={"runs": runs}))
		# Issue #9983 D3: a hold kept past the idle time by a live run.
		self.assertIn("outcome=held reason=holder_runs_active", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_idle_minutes_zero_turns_the_idle_test_off(self) -> None:
		h, out = _p2_tick(self._holder(updated=OLD), env={"MERGE_LEASE_IDLE_MINUTES": "0"})
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_idle_minutes_zero_keeps_the_no_head_gate_revocation(self) -> None:
		# 0 turns off only the idle test: a holder without a pipeline head-gate
		# is still revoked once the default 60-minute grace has passed.
		h, out = _p2_tick(self._holder(gate=None, updated=OLD), env={"MERGE_LEASE_IDLE_MINUTES": "0"})
		self._assert_revoked(h, out, "revoked_no_head_gate")

	def test_cancelled_ci_run_is_not_a_final_ci_failure(self) -> None:
		runs = {_sha(4): [{"id": 12, "name": "CI", "status": "completed", "conclusion": "cancelled", "event": "pull_request"}]}
		h, out = _p2_tick(self._holder(extra={"runs": runs}))
		self.assertNotIn("revoked_final_ci_failed", out)
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_eligible_holder_keeps_the_lease_without_a_write(self) -> None:
		h, out = _p2_tick(self._holder())
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_open\n", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)
		self.assertFalse(any("-X PATCH" in c or "-X POST" in c for c in h.calls), h.calls)

	def test_revocation_switch_off_keeps_a_draft_holder(self) -> None:
		h, out = _p2_tick(self._holder(draft=True), env={"MERGE_LEASE_REVOKE_ENABLED": "false"})
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)
		self.assertFalse(any("statuses" in c or "api user" in c for c in h.calls), h.calls)

	def test_unreadable_statuses_fail_open(self) -> None:
		h, out = _p2_tick(self._holder(gate=None, extra={"fail": ["/statuses"]}))
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_unreadable_login_fails_open(self) -> None:
		h, out = _p2_tick(self._holder(gate=None, updated=OLD, extra={"fail": ["api user"]}))
		self.assertIn("reason=login_unavailable", out)
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_unreadable_checks_fail_open(self) -> None:
		h, out = _p2_tick(self._holder(extra={"fail": ["check-runs"]}))
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_holder_missing_from_the_listing_is_never_revoked_by_phase_2(self) -> None:
		st = self._holder(draft=True)
		h, out = _p2_tick(st, listing=[])
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_lost_race_on_the_revoke_write_grants_nothing(self) -> None:
		st = self._holder(draft=True)
		st["pulls"].append(_pull(5))
		st["labels"] = {"5": [WAIT]}
		_gates_ok(st, 5)
		winner = "d" * 40
		st["commits"][winner] = {"message": _record(8), "tree": "f" * 40, "parents": ["c" * 40]}
		st["move_ref_on_patch"] = {REF: winner}
		h, out = _p2_tick(st)
		# As before Phase 2, the line names the record re-read after the refused PATCH.
		self.assertIn("base=main mode=tick outcome=lost_race reason=revoked_draft", out)
		self.assertNotIn("dispatched", h.state)
		self.assertEqual(h.state["refs"][REF], winner)

	def test_expired_holder_is_skipped_until_its_head_changes(self) -> None:
		st = {"pulls": [_pull(4)]}
		_seed_lease(st, _record(4, expires=int(time.time()) - 5))
		h, out = _p2_tick(st)
		self.assertIn("outcome=released reason=expired", out)
		self.assertEqual(_skips(h.record()), [f"4:{_sha(4)}:expired"])

	def test_closed_holder_gets_no_skip_entry(self) -> None:
		st = {"pulls": [_pull(5)], "closed": [4]}
		_seed_lease(st, _record(4))
		h, out = _p2_tick(st)
		self.assertIn("outcome=released reason=holder_closed", out)
		self.assertEqual(_skips(h.record()), [])


class LeaseGrantEligibility(unittest.TestCase):
	"""Phase 2: the tick grants a free lease only to an eligible waiter."""

	def _free(self, pulls: list[dict], waiting: list[int]) -> dict:
		st = {"pulls": pulls, "labels": {str(n): [WAIT] for n in waiting}, "files": {str(p["number"]): ["a"] for p in pulls}}
		_seed_lease(st, _record(3, state="released", paths=""))
		return st

	def test_skip_ai_waiter_is_skipped_for_the_next_eligible_one(self) -> None:
		st = self._free([dict(_pull(5), title="WIP [skip ai]"), _pull(6)], [5, 6])
		_gates_ok(st, 5, 6)
		h, out = _p2_tick(st)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=skipped reason=skip_ai\n", out)
		self.assertIn("MERGE_LEASE pr=6 base=main mode=tick outcome=granted reason=first_in_queue", out)
		self.assertNotIn(WAIT, h.state["labels"]["5"], "an ineligible waiter is unlabelled")
		self.assertIn("pr=6", h.record())
		rows = [(r["event"], r["pr"], r["reason"], r["holder"]) for r in _metrics(out)]
		self.assertIn(("skipped", "5", "skip_ai", "-"), rows)
		self.assertEqual(_skips(h.record()), [], "a grant-time skip is not stored")

	def test_waiter_without_a_pipeline_head_gate_is_skipped(self) -> None:
		st = self._free([_pull(5), _pull(6)], [5, 6])
		_head_gate(st, _sha(5), "success", "someone-else")
		_gates_ok(st, 6)
		h, out = _p2_tick(st)
		self.assertIn("pr=5 base=main mode=tick outcome=skipped reason=no_head_gate", out)
		self.assertIn("pr=6", h.record())

	def test_waiter_with_failed_final_ci_is_skipped(self) -> None:
		st = self._free([_pull(5), _pull(6)], [5, 6])
		_gates_ok(st, 5, 6)
		st["check_runs"] = {_sha(5): [{"id": 1, "name": "tests", "status": "completed", "conclusion": "failure"}]}
		h, out = _p2_tick(st, env={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "tests"})
		self.assertIn("pr=5 base=main mode=tick outcome=skipped reason=final_ci_failed", out)
		self.assertIn("pr=6", h.record())

	def test_no_eligible_waiter_leaves_the_lease_free(self) -> None:
		st = self._free([_pull(5)], [5])
		h, out = _p2_tick(st)
		self.assertIn("MERGE_LEASE pr=- base=main mode=tick outcome=no_waiter reason=no_eligible_waiter\n", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)
		self.assertNotIn("dispatched", h.state)

	def test_at_most_ten_candidates_per_tick(self) -> None:
		nums = list(range(10, 22))
		st = self._free([_pull(n) for n in nums], nums)
		h, out = _p2_tick(st)
		self.assertEqual(len(re.findall(r"outcome=skipped reason=no_head_gate\n", out)), 10)
		self.assertIn(WAIT, h.state["labels"]["20"])
		self.assertIn(WAIT, h.state["labels"]["21"])

	def test_unreadable_login_grants_without_a_head_gate(self) -> None:
		st = self._free([_pull(5)], [5])
		st["fail"] = ["api user"]
		h, out = _p2_tick(st)
		self.assertIn("pr=5 base=main mode=tick outcome=granted", out)

	def test_unreadable_checks_still_grant(self) -> None:
		st = self._free([_pull(5)], [5])
		_gates_ok(st, 5)
		st["fail"] = ["check-runs"]
		h, out = _p2_tick(st)
		self.assertIn("pr=5 base=main mode=tick outcome=granted", out)

	def test_revocation_switch_off_grants_as_before(self) -> None:
		st = self._free([dict(_pull(5), title="[skip ai]")], [5])
		h, out = _p2_tick(st, env={"MERGE_LEASE_REVOKE_ENABLED": "false"})
		self.assertIn("pr=5 base=main mode=tick outcome=granted", out)
		self.assertNotIn("skip=", h.record())


class LeaseSkipList(unittest.TestCase):
	"""Phase 2: a released PR is passed over until its head SHA changes."""

	def test_skipped_waiter_keeps_its_label_and_the_next_one_is_granted(self) -> None:
		# A hard entry (issue #9983: any reason but revoked_no_head_gate).
		st = {"pulls": [_pull(5), _pull(6)], "labels": {"5": [WAIT], "6": [WAIT]}, "files": {"6": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths="", skips=[f"5:{_sha(5)}:revoked_idle"]))
		_gates_ok(st, 5, 6)
		h, out = _p2_tick(st)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=skipped reason=skip_head_unchanged\n", out)
		self.assertIn("pr=6 base=main mode=tick outcome=granted", out)
		self.assertIn(WAIT, h.state["labels"]["5"])
		rec = h.record()
		self.assertIn("pr=6", rec)
		self.assertEqual(_skips(rec), [f"5:{_sha(5)}:revoked_idle"])
		self.assertLess(rec.index("updated="), rec.index("skip="))
		self.assertLess(rec.index("skip="), rec.index("paths:"))

	def test_lone_skip_held_waiter_is_unlabelled_not_granted(self) -> None:
		# D4: a skip entry blocks a re-grant on the same head. The sweep skips
		# labelled PRs, so a skip-held waiter left alone is unlabelled instead;
		# the update-mode gate (lone-caller rule) then decides.
		st = {"pulls": [_pull(5), _pull(6)], "labels": {"5": [WAIT], "6": [WAIT]}, "files": {"5": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths="", skips=[f"5:{_sha(5)}:revoked_idle"]))
		_gates_ok(st, 5)
		_head_gate(st, _sha(6), "failure")
		h, out = _p2_tick(st)
		self.assertIn("pr=6 base=main mode=tick outcome=skipped reason=no_head_gate", out)
		self.assertNotIn("outcome=granted", out)
		self.assertIn("MERGE_LEASE pr=- base=main mode=tick outcome=no_waiter reason=no_eligible_waiter\n", out)
		self.assertNotIn(WAIT, h.state["labels"]["5"], "the skip-held waiter is handed back to the sweep")
		self.assertEqual(h.state["refs"][REF], "c" * 40, "no lease write")
		self.assertEqual(_skips(h.record()), [f"5:{_sha(5)}:revoked_idle"])
		self.assertNotIn("dispatched", h.state)

	def test_idle_holder_with_cancelled_ci_reads_the_runs_once(self) -> None:
		runs = {_sha(4): [{"id": 12, "name": "CI", "status": "completed", "conclusion": "cancelled", "event": "pull_request"}]}
		h = _Harness(LeaseRevocation()._holder(updated=OLD, extra={"runs": runs}))
		res = h.train_fn('_mt_merge_lease_tick "${PRS_JSONL}" ""', {"PRS_JSONL": _p2_jsonl(h.state["pulls"], {})})
		self.assertIn("outcome=released reason=revoked_idle", res.stdout + res.stderr)
		# One head runs read; the active-run listing (issue #9983 G2) reads
		# actions/runs?status= pages, not the head.
		self.assertLessEqual(sum("actions/runs?head_sha=" in c for c in h.calls), 1, h.calls)

	def test_skip_clears_once_the_head_changes(self) -> None:
		new_head = "e" * 40
		st = {"pulls": [_pull(5, sha=new_head), _pull(6)], "labels": {"5": [WAIT], "6": [WAIT]}, "files": {"5": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths="", skips=[f"5:{_sha(5)}"]))
		_head_gate(st, new_head)
		_gates_ok(st, 6)
		h, out = _p2_tick(st)
		self.assertNotIn("skip_head_unchanged", out)
		rec = h.record()
		self.assertIn("pr=5", rec)
		self.assertEqual(_skips(rec), [])

	def test_entries_of_closed_prs_are_pruned(self) -> None:
		st = {"pulls": [_pull(5)], "labels": {"5": [WAIT]}, "files": {"5": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths="", skips=[f"9:{_sha(9)}"]))
		_gates_ok(st, 5)
		h, _ = _p2_tick(st)
		self.assertIn("pr=5", h.record())
		self.assertEqual(_skips(h.record()), [])

	def test_the_list_is_capped_at_the_newest_fifty(self) -> None:
		nums = list(range(100, 160))
		st = {"pulls": [_pull(n) for n in nums] + [_pull(5)], "labels": {"5": [WAIT]}, "files": {"5": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths="", skips=[f"{n}:{_sha(n)}" for n in nums]))
		_gates_ok(st, 5)
		h, _ = _p2_tick(st)
		self.assertEqual(_skips(h.record()), [f"{n}:{_sha(n)}" for n in nums[10:]])

	def _gate_state(self, caller_sha: str, waiting: list[int], skips: list[str]) -> dict:
		pulls = [_pull(7, sha=caller_sha)] + [_pull(n) for n in waiting]
		st = {"pulls": pulls, "labels": {str(n): [WAIT] for n in waiting}, "files": {"7": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths="", skips=skips))
		return st

	def test_gate_skipped_caller_waits_behind_another_waiter(self) -> None:
		h = _Harness(self._gate_state(_sha(7), [9], [f"7:{_sha(7)}"]))
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=wait reason=queued_behind blocker=9", out)

	def test_gate_caller_acquires_once_its_head_changed(self) -> None:
		h = _Harness(self._gate_state("e" * 40, [9], [f"7:{_sha(7)}"]))
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=acquired", out)

	def test_gate_skipped_caller_alone_acquires_and_drops_its_entry(self) -> None:
		h = _Harness(self._gate_state(_sha(7), [], [f"7:{_sha(7)}", f"8:{_sha(8)}"]))
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=acquired", out)
		self.assertEqual(_skips(h.record()), [f"8:{_sha(8)}"])

	def test_gate_ignores_the_list_with_revocation_off(self) -> None:
		h = _Harness(self._gate_state(_sha(7), [9], [f"7:{_sha(7)}"]))
		rc, out = _gate(h, 7, "update", {"MERGE_LEASE_REVOKE_ENABLED": "false"})
		self.assertEqual(rc, 0, out)
		self.assertNotIn("skip=", h.record())


class LeaseRecordCompat(unittest.TestCase):
	"""Phase 2 record fields are optional and old records parse as before."""

	FIELDS = ('echo "state=$MERGE_LEASE_STATE pr=$MERGE_LEASE_PR head=$MERGE_LEASE_HEAD expires=$MERGE_LEASE_EXPIRES '
		'acquired=$MERGE_LEASE_ACQUIRED updated=$MERGE_LEASE_UPDATED"; printf "PATHS<%s>\\nSKIPS<%s>\\n" "$MERGE_LEASE_PATHS" "$MERGE_LEASE_SKIPS"')

	def _read(self, record: str) -> str:
		st: dict = {}
		_seed_lease(st, record)
		res = _Harness(st).lib(f"_merge_lease_read main; echo rc=$?; {self.FIELDS}")
		self.assertIn("rc=0", res.stdout, res.stderr)
		return res.stdout

	def test_old_record_parses_with_an_empty_skip_list(self) -> None:
		out = self._read(_record(8, acquired=111, updated=222, paths="a.py\nb.py"))
		self.assertIn(f"state=held pr=8 head={'b' * 40}", out)
		self.assertIn("acquired=111 updated=222", out)
		self.assertIn("PATHS<a.py\nb.py>", out)
		self.assertIn("SKIPS<>", out)

	def test_skip_lines_leave_the_other_fields_unchanged(self) -> None:
		# One expires value for both records: _record() derives it from
		# time.time(), which can tick a second between the two reads.
		expires = int(time.time()) + 3600
		plain = self._read(_record(8, expires=expires, acquired=111, updated=222, paths="a.py\nskip=9:x"))
		with_skips = self._read(_record(8, expires=expires, acquired=111, updated=222, paths="a.py\nskip=9:x",
			skips=[f"5:{_sha(5)}", "bad", f"6:{_sha(6)}", f"5:{_sha(5)}"]))
		self.assertEqual(plain.split("SKIPS<")[0], with_skips.split("SKIPS<")[0])
		self.assertIn(f"SKIPS<6:{_sha(6)}\n5:{_sha(5)}>", with_skips)

	def _write(self, env: dict[str, str] | None = None, state: str = "released", pr: int = 4) -> str:
		h = _Harness({"pulls": []})
		res = h.lib(f'MERGE_LEASE_SHA=""; MERGE_LEASE_TREE={"f" * 40}; MERGE_LEASE_SKIPS="4:{_sha(4)}"$\'\\n\'"6:{_sha(6)}"; '
			f'_merge_lease_write main {state} {pr} {HEAD} "a.py" 5; echo rc=$?', env)
		self.assertIn("rc=0", res.stdout, res.stderr)
		return h.record()

	def test_write_with_revocation_off_is_the_old_format(self) -> None:
		rec = self._write({"MERGE_LEASE_REVOKE_ENABLED": "false"})
		self.assertRegex(rec, r"^merge-lease v1\nstate=released\nbase=main\npr=4\nhead=a{40}\nexpires=0\nacquired=5\nupdated=\d+\npaths:\n$")

	def test_write_puts_skips_before_paths_and_a_held_write_drops_its_own(self) -> None:
		self.assertEqual(_skips(self._write()), [f"4:{_sha(4)}", f"6:{_sha(6)}"])
		held = self._write(state="held", pr=4)
		self.assertEqual(_skips(held), [f"6:{_sha(6)}"])
		self.assertTrue(held.endswith(f"skip=6:{_sha(6)}\npaths:\na.py"), held)


FM = "auto/forward-merge-stable-123-1"


def _fork_pull(num: int, head_ref: str) -> dict:
	pull = _pull(num, head_ref=head_ref)
	pull["head"]["repo"] = {"full_name": "other/repo"}
	return pull


class LeaseForwardMergePriority(unittest.TestCase):
	"""Merge-lease parallel merges Phase 3 (Refs #9781): a same-repository
	forward-merge head ranks with the MERGE_TRAIN_PRIORITY_LABELS group."""

	def _free_gate(self, pulls: list[dict], waiting: list[int], extra_env: dict[str, str] | None = None,
			labels: dict[str, list[str]] | None = None) -> tuple[int, str]:
		st = {"pulls": pulls, "labels": labels or {str(n): [WAIT] for n in waiting}, "files": {"5": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		return _gate(_Harness(st), 5, "update", extra_env)

	def test_update_gate_queues_a_plain_pr_behind_a_forward_merge_waiter(self) -> None:
		rc, out = self._free_gate([_pull(5), _pull(12, head_ref=FM)], [12])
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=wait reason=queued_behind blocker=12", out)

	def test_tick_grants_a_forward_merge_waiter_before_a_lower_number(self) -> None:
		st = {"pulls": [_pull(5), _pull(12, head_ref=FM)], "labels": {"5": [WAIT], "12": [WAIT]},
			"files": {"5": ["a"], "12": ["b"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		_gates_ok(st, 5, 12)
		h, out = _p2_tick(st)
		self.assertIn("MERGE_LEASE pr=12 base=main mode=tick outcome=granted", out)
		self.assertIn("pr=12", h.record())
		self.assertIn(WAIT, h.state["labels"]["5"])

	def test_priority_label_and_forward_merge_tie_on_pr_number(self) -> None:
		# Labelled 9 against forward-merge 12: 9 first.
		st = {"pulls": [_pull(5), _pull(9), _pull(12, head_ref=FM)],
			"labels": {"9": [WAIT, "ai:security"], "12": [WAIT]}, "files": {"5": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		rc, out = _gate(_Harness(st), 5, "update")
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=queued_behind blocker=9", out)
		# Forward-merge 8 against labelled 9: 8 first.
		st2 = {"pulls": [_pull(5), _pull(8, head_ref=FM), _pull(9)],
			"labels": {"8": [WAIT], "9": [WAIT, "ai:security"]}, "files": {"5": ["a"]}}
		_seed_lease(st2, _record(3, state="released", paths=""))
		rc2, out2 = _gate(_Harness(st2), 5, "update")
		self.assertEqual(rc2, 1, out2)
		self.assertIn("reason=queued_behind blocker=8", out2)

	def test_none_restores_number_order(self) -> None:
		pulls = [_pull(5), _pull(12, head_ref=FM)]
		labels = {"5": [WAIT], "12": [WAIT]}
		for value in ("none", "", "OFF"):
			rc, out = self._free_gate([dict(p) for p in pulls], [5, 12], {"MERGE_LEASE_PRIORITY_HEAD_PREFIXES": value}, labels)
			self.assertEqual(rc, 0, (value, out))
			self.assertIn("outcome=acquired", out, value)

	def test_fork_head_with_a_forward_merge_name_gets_no_priority(self) -> None:
		rc, out = self._free_gate([_pull(5), _fork_pull(12, FM)], [5, 12])
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=acquired", out)

	def test_unknown_repository_gives_no_priority(self) -> None:
		pulls = json.dumps([dict(_pull(n, head_ref=FM if n == 12 else None), labels=[{"name": WAIT}]) for n in (5, 12)])
		h = _Harness({})
		body = '_merge_lease_queue_first "${PULLS}" ""'
		self.assertEqual(h.lib(body, {"PULLS": pulls}).stdout, "12")
		self.assertEqual(h.lib(body, {"PULLS": pulls, "PR_CHECKS_REPOSITORY": "", "GITHUB_REPOSITORY": ""}).stdout, "5")
		self.assertEqual(h.lib(body, {"PULLS": pulls, "PR_CHECKS_REPOSITORY": "OWNER/Repo"}).stdout, "12")

	def test_unchanged_order_without_a_forward_merge_waiter(self) -> None:
		rc, out = self._free_gate([_pull(5), _pull(12)], [12])
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=acquired", out)

	def _key(self, args: str, env: dict[str, str] | None = None) -> str:
		res = _Harness({}).lib(f"_merge_lease_order_key {args}", env)
		self.assertEqual(res.returncode, 0, res.stderr)
		return res.stdout

	def test_order_key_contract(self) -> None:
		self.assertEqual(self._key("5 ''"), "1000000000005")
		self.assertEqual(self._key("5 '' ''"), "1000000000005")
		self.assertEqual(self._key("5 'ai:security'"), "0000000000005")
		self.assertEqual(self._key(f"12 '' {FM}"), "0000000000012")
		self.assertEqual(self._key("12 '' ai/issue-12"), "1000000000012")
		self.assertEqual(self._key(f"12 '' {FM}", {"MERGE_LEASE_PRIORITY_HEAD_PREFIXES": " release/ , auto/forward-merge-stable- "}),
			"0000000000012")
		self.assertEqual(self._key("12 '' release/x", {"MERGE_LEASE_PRIORITY_HEAD_PREFIXES": " release/ , auto/forward-merge-stable- "}),
			"0000000000012")
		self.assertEqual(self._key("12 '' auto/forward-merge-stable-1", {"MERGE_LEASE_PRIORITY_HEAD_PREFIXES": "auto/*"}),
			"1000000000012")
		self.assertEqual(self._key(f"12 '' {FM}", {"MERGE_LEASE_PRIORITY_HEAD_PREFIXES": "none"}), "1000000000012")


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

	def test_lease_revocation_and_priority_switches_reach_every_lease_path(self) -> None:
		"""Phase 2/3 rollback switches (Refs #9781): every workflow that passes
		MERGE_LEASE_ENABLED to the lease gate or tick also passes these, with
		defaults identical to the helper's."""
		wired = (
			"MERGE_LEASE_REVOKE_ENABLED: ${{ vars.MERGE_LEASE_REVOKE_ENABLED || 'true' }}",
			"MERGE_LEASE_IDLE_MINUTES: ${{ vars.MERGE_LEASE_IDLE_MINUTES || '60' }}",
			"MERGE_LEASE_PRIORITY_HEAD_PREFIXES: ${{ vars.MERGE_LEASE_PRIORITY_HEAD_PREFIXES || 'auto/forward-merge-stable-' }}",
		)
		review = REVIEW.read_text()
		for line in wired:
			self.assertEqual(review.count(line), 3, line)
			# The poll release step plus the three freshness-gate steps, which
			# run the update-mode lease gate (_pr_base_fresh_for_merge).
			self.assertEqual(POLL.read_text().count(line), 4, f"{POLL.name}: {line}")
			self.assertIn(line, CLOSE.read_text(), f"{CLOSE.name}: {line}")
		lib = LIB.read_text()
		for expansion in ("${MERGE_LEASE_REVOKE_ENABLED:-true}", "${MERGE_LEASE_IDLE_MINUTES:-60}",
				"${MERGE_LEASE_PRIORITY_HEAD_PREFIXES-auto/forward-merge-stable-}"):
			self.assertIn(expansion, lib)
		# The sweep reads only MERGE_LEASE_ENABLED / MERGE_LEASE_WAIT_LABEL; if it
		# ever calls the gate or tick, it needs the three lines above too.
		sweep = SWEEP.read_text()
		for fn in ("_merge_lease_gate", "_merge_lease_queue_first", "_mt_merge_lease_tick"):
			self.assertNotIn(fn, sweep)

	def test_soft_paths_switch_reaches_every_freshness_and_lease_path(self) -> None:
		"""Phase 4 (Refs #9781): the soft-class kill switch reaches every step
		that runs the freshness gate or the lease, with the helper's default."""
		line = ("MERGE_LEASE_SOFT_PATHS: ${{ vars.MERGE_LEASE_SOFT_PATHS || "
			"'README.md,agents.md,AGENTS.md,docs/INVENTORY.md,CHANGELOG.md,agents.d/,changelog.d/' }}")
		for wf, expected in ((REVIEW, 3), (POLL, 4), (CLOSE, 1), (SWEEP, 0)):
			text = wf.read_text()
			self.assertEqual(text.count(line), expected, wf.name)
			if wf is SWEEP:
				# The sweep's base sync treats clean and overlap alike, so its
				# final-CI step pins the class off (no probe it cannot use).
				step = text.split("- name: Ensure the final CI run on reviewed PR heads", 1)[1].split("\n      - name: ", 1)[0]
				self.assertIn("          MERGE_LEASE_SOFT_PATHS: none\n", step)
				continue
			for segment in re.split(r"\n(?=\s*- name: |  [A-Za-z0-9_-]+:\n)", text):
				if "MERGE_BASE_FRESHNESS_ENABLED:" in segment:
					self.assertIn(line, segment, f"{wf.name}: {segment[:80]!r}")
		self.assertIn("${MERGE_LEASE_SOFT_PATHS-README.md,agents.md,AGENTS.md,docs/INVENTORY.md,CHANGELOG.md,agents.d/,changelog.d/}", LIB.read_text())

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


# --- Soft path class in merge mode (merge-lease Phase 4, Refs #9781) ---------

def _soft_git(cwd: Path, *args: str) -> str:
	env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
		"GIT_COMMITTER_EMAIL": "t@example.com"}
	return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


def _soft_heads(conflict: bool) -> tuple[Path, str, str]:
	"""Own head edits README.md line 1; the holder head edits line 10 (or line 1
	for a conflict). Returns (repo, own_sha, holder_sha)."""
	if shutil.which("git") is None:
		raise unittest.SkipTest("git binary not available in test environment")
	repo = Path(tempfile.mkdtemp(prefix="merge_lease_soft_"))
	_soft_git(repo, "init", "-q", "-b", "main")
	(repo / "README.md").write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")
	(repo / "shared.py").write_text("x = 1\n", encoding="utf-8")
	_soft_git(repo, "add", "-A")
	_soft_git(repo, "commit", "-q", "-m", "base")
	heads = []
	for branch, index, text in (("own", 0, "own line 1\n"), ("holder", 0 if conflict else 9, "holder line\n")):
		_soft_git(repo, "checkout", "-q", "-b", branch, "main")
		lines = (repo / "README.md").read_text(encoding="utf-8").splitlines(keepends=True)
		lines[index] = text
		(repo / "README.md").write_text("".join(lines), encoding="utf-8")
		_soft_git(repo, "commit", "-q", "-am", branch)
		heads.append(_soft_git(repo, "rev-parse", "HEAD"))
	return repo, heads[0], heads[1]


def _soft_lease_gate(conflict: bool, own_files: list[str], holder_paths: str, *, holder_head: str | None = None,
		env: dict[str, str] | None = None) -> tuple[int, str]:
	repo, own, holder = _soft_heads(conflict)
	st = {"pulls": [_pull(7), _pull(8)], "files": {"7": own_files}}
	_seed_lease(st, _record(8, paths=holder_paths).replace(f"head={'b' * 40}", f"head={holder_head or holder}"))
	h = _Harness(st)
	res = h.lib(f'cd {str(repo)!r} || exit 9\n_merge_lease_gate 7 main {own} merge; rc=$?; '
		'echo "rc=$rc outcome=$MERGE_LEASE_OUTCOME reason=$MERGE_LEASE_REASON soft=$MERGE_LEASE_SOFT_SHARED"',
		{"GIT_CONFIG_NOSYSTEM": "1", **(env or {})})
	assert res.returncode == 0, res.stderr
	line = [ln for ln in res.stdout.splitlines() if ln.startswith("rc=")][-1]
	return int(line.split()[0][3:]), res.stdout + res.stderr


class LeaseSoftPaths(unittest.TestCase):
	def test_soft_only_paths_that_merge_cleanly_pass_the_holder(self) -> None:
		rc, out = _soft_lease_gate(False, ["README.md"], "README.md")
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=no_overlap reason=soft_paths_merge_clean soft=README.md", out)
		self.assertIn("soft_paths_merge_clean", [m.get("reason") for m in _metrics(out)])

	def test_a_conflict_on_a_soft_path_waits(self) -> None:
		rc, out = _soft_lease_gate(True, ["README.md"], "README.md")
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=wait reason=holder_shares_paths", out)

	def test_a_shared_hard_path_waits(self) -> None:
		rc, out = _soft_lease_gate(False, ["README.md", "shared.py"], "README.md\nshared.py")
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=holder_shares_paths", out)

	def test_a_star_holder_record_waits_even_for_soft_only_files(self) -> None:
		rc, out = _soft_lease_gate(False, ["README.md"], "*")
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=holder_shares_paths", out)

	def test_a_short_holder_head_waits(self) -> None:
		rc, out = _soft_lease_gate(False, ["README.md"], "README.md", holder_head="abc1234")
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=holder_shares_paths", out)

	def test_class_off_keeps_todays_wait(self) -> None:
		for value in ("none", "off", ""):
			with self.subTest(value=value):
				rc, out = _soft_lease_gate(False, ["README.md"], "README.md", env={"MERGE_LEASE_SOFT_PATHS": value})
				self.assertEqual(rc, 1, out)
				self.assertIn("reason=holder_shares_paths", out)

	def test_a_soft_path_the_holder_renamed_to_a_hard_path_waits(self) -> None:
		"""merge-ort follows the holder's README.md -> scripts/x.sh rename and
		merges cleanly, but the merged scripts/x.sh matches neither head."""
		repo, own, _ = _soft_heads(False)
		_soft_git(repo, "checkout", "-q", "-b", "renamer", "main")
		(repo / "scripts").mkdir()
		_soft_git(repo, "mv", "README.md", "scripts/x.sh")
		_soft_git(repo, "commit", "-q", "-m", "rename")
		holder = _soft_git(repo, "rev-parse", "HEAD")
		st = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["README.md"]}}
		_seed_lease(st, _record(8, paths="README.md\nscripts/x.sh").replace(f"head={'b' * 40}", f"head={holder}"))
		res = _Harness(st).lib(f'cd {str(repo)!r} || exit 9\n_merge_lease_gate 7 main {own} merge; rc=$?; '
			'echo "rc=$rc outcome=$MERGE_LEASE_OUTCOME reason=$MERGE_LEASE_REASON hard=$_PR_PROBE_HARD_COMBINED"',
			{"GIT_CONFIG_NOSYSTEM": "1"})
		self.assertEqual(res.returncode, 0, res.stderr)
		self.assertIn("rc=1 outcome=wait reason=holder_shares_paths hard=scripts/x.sh", res.stdout, res.stdout + res.stderr)

	def test_a_shared_path_holding_a_newline_waits(self) -> None:
		"""One real path README.md<LF>agents.d/x must not split into two soft
		fragments: with the class on, the PR's path list becomes `*`."""
		rc, out = _soft_lease_gate(False, ["README.md\nagents.d/x"], "README.md\nagents.d/x")
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=holder_shares_paths", out)
		self.assertNotIn("soft_paths_merge_clean", out)

	def test_a_non_string_file_name_fails_closed(self) -> None:
		"""A malformed files page must yield `*` (wait), not a jq error that
		the merge-mode gate would treat as pr_files_failed (fail-open)."""
		body = """_safe_gh_jq() { printf '%s' '[[{"filename":5},{"filename":"README.md"}]]'; }
_merge_lease_pr_paths 7; echo "rc=$?"
"""
		res = _Harness({}).lib(body)
		self.assertIn("*\nrc=0", res.stdout, res.stdout + res.stderr)
		off = _Harness({}).lib(body, {"MERGE_LEASE_SOFT_PATHS": "none"})
		self.assertIn("5\nREADME.md\nrc=0", off.stdout, off.stdout + off.stderr)

	def test_unshared_paths_keep_the_old_reason(self) -> None:
		rc, out = _soft_lease_gate(False, ["own.py"], "README.md")
		self.assertEqual(rc, 0, out)
		self.assertIn("reason=no_shared_paths_with_holder soft=", out)

	def test_two_argument_intersection_is_unchanged(self) -> None:
		h = _Harness({})
		body = """_merge_lease_paths_intersect "README.md" "README.md"; echo "a=$?"
_merge_lease_paths_intersect "README.md" "other.py"; echo "b=$?"
_merge_lease_paths_intersect "*" "other.py"; echo "c=$?"
_merge_lease_paths_intersect "" "other.py"; echo "d=$?"
"""
		out = h.lib(body).stdout
		for expected in ("a=0", "b=1", "c=0", "d=1"):
			self.assertIn(expected, out)



# --- Several holders per base (merge-lease Phase 5, Refs #9781) ------------

MAXH = "MERGE_LEASE_MAX_HOLDERS"


def _multi_record(rows: list[tuple[int, str, int, int, int, list[str]]], skips: list[str] | None = None) -> str:
	"""A Phase 5 record. rows: (pr, head, expires, acquired, updated, paths);
	the first row is the primary."""
	primary = rows[0]
	lines = ["merge-lease v1", "state=held", "base=main", f"pr={primary[0]}", f"head={primary[1]}",
		f"expires={max(r[2] for r in rows)}", f"acquired={primary[3]}", f"updated={primary[4]}"]
	lines += [f"skip={s}" for s in skips or []]
	lines += [f"holder={r[0]} head={r[1]} expires={r[2]} acquired={r[3]} updated={r[4]}" for r in rows]
	union: list[str] = []
	for r in rows:
		lines += [f"holder_path={r[0]} {path}" for path in r[5]]
		union += [path for path in r[5] if path not in union]
	return "\n".join(lines + ["paths:"] + union)


def _row(pr: int, paths: list[str], *, expires: int | None = None, acquired: int | None = None) -> tuple[int, str, int, int, int, list[str]]:
	now = int(time.time())
	return (pr, _sha(pr), now + 3600 if expires is None else expires, now - 60 if acquired is None else acquired, now - 60, paths)


def _v1_fields(record: str) -> dict[str, str]:
	"""Parse a record the way the pre-Phase-5 _merge_lease_read did."""
	out = {}
	for key in ("state", "pr", "head", "expires", "acquired", "updated"):
		m = re.search(rf"^{key}=(.*)$", record, re.M)
		out[key] = m.group(1) if m else ""
	out["paths"] = record.split("\npaths:\n", 1)[1] if "\npaths:\n" in record else ""
	return out


def _holders(record: str) -> list[str]:
	return re.findall(r"^holder=(\d+) ", record, re.M)


class MultiHolderLease(unittest.TestCase):
	def test_max_holders_validation(self) -> None:
		h = _Harness({})
		for raw, want, warns in (("abc", "1", True), ("0", "1", False), ("-1", "1", True), ("9", "5", False),
				("2x", "1", True), ("", "1", False), (" 3 ", "3", False), ("1234", "1", True), ("2;echo x", "1", True)):
			with self.subTest(raw=raw):
				res = h.lib("_merge_lease_max_holders", {MAXH: raw})
				self.assertEqual(res.stdout.strip(), want, res.stderr)
				self.assertEqual("::warning::MERGE_LEASE_MAX_HOLDERS=" in res.stderr, warns, res.stderr)
		res = h.lib("_merge_lease_max_holders", {MAXH: "2;echo x"})
		self.assertIn("MERGE_LEASE_MAX_HOLDERS=2echox is not an integer; using 1", res.stderr)
		self.assertEqual(h.lib("unset MERGE_LEASE_MAX_HOLDERS; _merge_lease_max_holders").stdout.strip(), "1")

	def test_v1_record_reads_as_one_holder(self) -> None:
		for skips in ([], [f"5:{_sha(5)}"]):
			st: dict = {}
			_seed_lease(st, _record(8, acquired=111, updated=222, paths="a.py\nb.py", skips=skips))
			res = _Harness(st).lib('_merge_lease_read main; echo "rc=$? pr=$MERGE_LEASE_PR count=$MERGE_LEASE_HOLDER_COUNT"; '
				'printf "ROWS<%s>\\nPATHS<%s>\\n" "$MERGE_LEASE_HOLDER_ROWS" "$MERGE_LEASE_HOLDER_PATHS"')
			self.assertIn("rc=0 pr=8 count=1", res.stdout, res.stderr)
			self.assertRegex(res.stdout, r"ROWS<8 b{40} \d+ 111 222>")
			self.assertIn("PATHS<8\ta.py\n8\tb.py>", res.stdout)

	def test_n1_and_n2_write_the_same_bytes_for_one_holder(self) -> None:
		for state, pr in (({"pulls": [_pull(7)], "files": {"7": ["a.py"]}}, 7), (None, 8)):
			if state is None:
				state = {"pulls": [_pull(8)], "files": {"8": ["shared.py"]}}
				_seed_lease(state, _record(8, acquired=int(time.time()) - 60))
			one = _Harness(json.loads(json.dumps(state)))
			two = _Harness(json.loads(json.dumps(state)))
			rc1, out1 = _gate(one, pr, "update")
			rc2, out2 = _gate(two, pr, "update", {MAXH: "2"})
			self.assertEqual((rc1, rc2), (0, 0), out1 + out2)
			self.assertEqual(_stable_record(one.record()), _stable_record(two.record()))
			self.assertNotIn("holders=", out1 + out2)
			self.assertEqual(_stable_calls(one.calls), _stable_calls(two.calls))

	def test_two_disjoint_prs_both_hold(self) -> None:
		st = {"pulls": [_pull(7), _pull(8)], "files": {"7": ["own.py"]}}
		_seed_lease(st, _record(8, paths="shared.py", acquired=int(time.time()) - 60).replace(f"head={'b' * 40}", f"head={_sha(8)}"))
		h = _Harness(st)
		rc, out = _gate(h, 7, "update", {MAXH: "2"})
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=acquired", out)
		self.assertIn("holders=2", out)
		rec = h.record()
		self.assertEqual(_holders(rec), ["8", "7"])
		self.assertIn("holder_path=8 shared.py\nholder_path=7 own.py\npaths:\nshared.py\nown.py", rec)
		old = _v1_fields(rec)
		self.assertEqual((old["state"], old["pr"], old["paths"]), ("held", "8", "shared.py\nown.py"))
		expires = [int(x) for x in re.findall(r"^holder=\d+ head=\S* expires=(\d+)", rec, re.M)]
		self.assertEqual(int(old["expires"]), max(expires))
		self.assertNotIn("ai:merge-lease-wait", h.state.get("labels", {}).get("7", []))

	def test_overlapping_newcomer_waits_on_the_holder_it_shares_with(self) -> None:
		st = {"pulls": [_pull(7), _pull(8), _pull(9)], "files": {"7": ["b.py"]}}
		_seed_lease(st, _multi_record([_row(8, ["a.py"]), _row(9, ["b.py"])]))
		h = _Harness(st)
		rc, out = _gate(h, 7, "update", {MAXH: "3"})
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=wait reason=holder_shares_paths blocker=9", out)
		self.assertIn("holder=8 blocker=9 holders=2\n", out)
		self.assertEqual(_metrics(out)[0]["holder"], "9")
		self.assertIn("ai:merge-lease-wait", h.state["labels"]["7"])
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_newcomer_at_the_cap_waits_held_on_the_primary(self) -> None:
		st = {"pulls": [_pull(7), _pull(8), _pull(9)], "files": {"7": ["c.py"]}}
		_seed_lease(st, _multi_record([_row(8, ["a.py"]), _row(9, ["b.py"])]))
		rc, out = _gate(_Harness(st), 7, "update", {MAXH: "2"})
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=wait reason=held blocker=8", out)

	def test_merge_mode_checks_every_holder(self) -> None:
		def run(pr: int, files: list[str]) -> tuple[int, str]:
			st = {"pulls": [_pull(7), _pull(8), _pull(9)], "files": {str(pr): files}}
			_seed_lease(st, _multi_record([_row(8, ["a.py"]), _row(9, ["b.py"])]))
			return _gate(_Harness(st), pr, "merge")
		rc, out = run(7, ["c.py"])
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=no_overlap reason=no_shared_paths_with_holder", out)
		rc, out = run(7, ["b.py"])
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=holder_shares_paths blocker=9", out)
		rc, out = run(9, ["b.py"])
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=self", out)

	def test_holder_that_grew_onto_an_earlier_holder_waits_in_merge_mode(self) -> None:
		def run(pr: int, files: dict[str, list[str]], fail: list[str] | None = None) -> tuple[int, str, _Harness]:
			st = {"pulls": [_pull(8), _pull(9)], "files": files}
			if fail:
				st["fail"] = fail
			_seed_lease(st, _multi_record([_row(8, ["a.py"]), _row(9, ["b.py"])]))
			h = _Harness(st)
			rc, out = _gate(h, pr, "merge")
			return rc, out, h
		rc, out, h = run(9, {"9": ["a.py", "b.py"]})
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=wait reason=holder_shares_paths blocker=8", out)
		self.assertEqual(_metrics(out)[0]["holder"], "8")
		# A holder is not labelled: the tick never grants a live holder and the
		# sweep skips labelled PRs, so the label would strand it.
		self.assertNotIn("ai:merge-lease-wait", h.state.get("labels", {}).get("9", []))
		# The earlier holder proceeds without a files read, so they never deadlock.
		rc, out, h = run(8, {"8": ["a.py", "b.py"]})
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=self", out)
		self.assertFalse(any("pulls/8/files" in c for c in h.calls), h.calls)
		# An unreadable files list fails open as before.
		rc, out, _ = run(9, {}, fail=["pulls/9/files"])
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=self", out)

	def test_renewal_keeps_the_recorded_paths_when_the_files_read_fails(self) -> None:
		st = {"pulls": [_pull(8), _pull(9)], "fail": ["pulls/9/files"]}
		_seed_lease(st, _multi_record([_row(8, ["a.py"]), _row(9, ["b.py"])]))
		h = _Harness(st)
		rc, out = _gate(h, 9, "update", {MAXH: "2"})
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=renewed", out)
		rec = h.record()
		self.assertIn("holder_path=8 a.py\nholder_path=9 b.py\npaths:\na.py\nb.py", rec)

	def test_queue_walk_passes_over_a_waiter_that_overlaps_a_holder(self) -> None:
		def run(waiter_files: list[str]) -> tuple[int, str]:
			st = {"pulls": [_pull(5), _pull(7), _pull(8)], "labels": {"5": [WAIT]}, "files": {"5": waiter_files, "7": ["c.py"]}}
			_seed_lease(st, _record(8, paths="a.py"))
			return _gate(_Harness(st), 7, "update", {MAXH: "2"})
		rc, out = run(["a.py"])
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=acquired", out)
		rc, out = run(["d.py"])
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=wait reason=queued_behind blocker=5", out)

	def test_lost_race_re_decides_on_the_winning_record(self) -> None:
		st = {"pulls": [_pull(7), _pull(8), _pull(9)], "files": {"7": ["b.py"]}}
		_seed_lease(st, _record(8, paths="a.py"))
		winner = "d" * 40
		st["commits"][winner] = {"message": _multi_record([_row(8, ["a.py"]), _row(9, ["b.py"])]), "tree": "f" * 40, "parents": ["c" * 40]}
		st["move_ref_on_patch"] = {REF: [winner]}
		h = _Harness(st)
		rc, out = _gate(h, 7, "update", {MAXH: "3"})
		self.assertEqual(rc, 1, out)
		self.assertEqual([(r["event"], r["reason"]) for r in _metrics(out)], [("retry", "lost_race"), ("wait", "holder_shares_paths")])
		self.assertIn("blocker=9", out)
		self.assertEqual(h.state["refs"][REF], winner)
		self.assertEqual(_holders(h.record()), ["8", "9"])

	def test_n1_with_a_leftover_two_holder_record(self) -> None:
		rows = [_row(8, ["a.py"]), _row(9, ["b.py"], expires=int(time.time()) + 120)]
		st = {"pulls": [_pull(7), _pull(8), _pull(9)], "files": {"7": ["c.py"], "9": ["b.py"]}}
		_seed_lease(st, _multi_record(rows))
		rc, out = _gate(_Harness(json.loads(json.dumps(st))), 7, "update")
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=wait reason=held blocker=8", out)
		h = _Harness(st)
		rc, out = _gate(h, 9, "update")
		self.assertEqual(rc, 0, out)
		self.assertIn("outcome=renewed", out)
		rec = h.record()
		self.assertIn(f"holder=8 head={_sha(8)} expires={rows[0][2]} acquired={rows[0][3]}", rec)
		nine = int(re.search(r"^holder=9 head=\S* expires=(\d+)", rec, re.M).group(1))
		self.assertGreater(nine, int(time.time()) + 3600)

	def test_expired_primary_is_replaced_by_the_oldest_acquired_holder(self) -> None:
		now = int(time.time())
		st: dict = {}
		_seed_lease(st, _multi_record([_row(8, ["a.py"], expires=now - 5), _row(9, ["b.py"], acquired=now - 100),
			_row(10, ["c.py"], acquired=now - 500)]))
		res = _Harness(st).lib('_merge_lease_read main; echo "pr=$MERGE_LEASE_PR count=$MERGE_LEASE_HOLDER_COUNT acquired=$MERGE_LEASE_ACQUIRED"; '
			'_merge_lease_is_held && echo held; _merge_lease_holder_paths 9')
		self.assertIn(f"pr=10 count=2 acquired={now - 500}\nheld\nb.py", res.stdout, res.stderr)

	def test_malformed_holder_lines_are_dropped(self) -> None:
		rec = _multi_record([_row(8, ["a.py"]), _row(9, ["b.py"])]).replace("holder=9 head=", "holder=9 hed=")
		st: dict = {}
		_seed_lease(st, rec)
		res = _Harness(st).lib('_merge_lease_read main; echo "count=$MERGE_LEASE_HOLDER_COUNT pr=$MERGE_LEASE_PR"')
		self.assertIn("count=1 pr=8", res.stdout, res.stderr)

	def test_a_path_that_could_forge_a_line_fails_closed(self) -> None:
		h = _Harness({})
		res = h.lib('_merge_lease_paths_record_form $\'a.py\\r\' 200; _merge_lease_paths_record_form "*x" 200; '
			'_merge_lease_paths_record_form "" 200; _merge_lease_paths_record_form $\'a\\nb\\nc\' 2; _merge_lease_paths_record_form "a.py" 200')
		self.assertEqual(res.stdout, "*\n*\n*\n*\na.py\n", res.stderr)


class MultiHolderTick(unittest.TestCase):
	def test_closed_secondary_is_released_and_the_primary_keeps_its_own_expiry(self) -> None:
		rows = [_row(4, ["a.py"], expires=int(time.time()) + 1800), _row(6, ["b.py"])]
		st = {"pulls": [_pull(4)], "closed": [6]}
		_seed_lease(st, _multi_record(rows))
		_gates_ok(st, 4)
		h, out = _p2_tick(st, env={MAXH: "2"})
		self.assertRegex(out, r"MERGE_LEASE pr=6 base=main mode=tick outcome=released reason=holder_closed held_s=\d+ merged=false\n")
		self.assertIn("MERGE_LEASE_QUEUE base=main holder=4 held_s=", out)
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_open holders=1\n", out)
		rec = h.record()
		self.assertEqual(_holders(rec), [])
		old = _v1_fields(rec)
		self.assertEqual((old["pr"], old["expires"], old["paths"]), ("4", str(rows[0][2]), "a.py"))

	def test_released_primary_promotes_the_oldest_acquired_holder(self) -> None:
		now = int(time.time())
		st = {"pulls": [_pull(6), _pull(7)], "closed": [4]}
		_seed_lease(st, _multi_record([_row(4, ["a.py"]), _row(6, ["b.py"], acquired=now - 100), _row(7, ["c.py"], acquired=now - 500)]))
		_gates_ok(st, 6, 7)
		h, out = _p2_tick(st, env={MAXH: "3"})
		self.assertIn("pr=4 base=main mode=tick outcome=released reason=holder_closed", out)
		rec = h.record()
		self.assertEqual(_v1_fields(rec)["pr"], "7")
		self.assertEqual(_holders(rec), ["7", "6"])
		self.assertIn("paths:\nc.py\nb.py", rec)

	def test_grants_a_disjoint_waiter_and_skips_an_overlapping_one(self) -> None:
		st = {"pulls": [_pull(4), _pull(5), _pull(6)], "labels": {"5": [WAIT], "6": [WAIT]},
			"files": {"5": ["a.py"], "6": ["b.py"]}}
		_seed_lease(st, _record(4, paths="a.py").replace(f"head={'b' * 40}", f"head={_sha(4)}"))
		_gates_ok(st, 4, 5, 6)
		h, out = _p2_tick(st, env={MAXH: "2"})
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=skipped reason=holder_shares_paths blocker=4\n", out)
		self.assertIn("MERGE_LEASE pr=6 base=main mode=tick outcome=granted reason=first_in_queue queue_len=2 holders=2\n", out)
		# Waiter 5 overlaps a holder and keeps its label, so it stays queued.
		self.assertRegex(out, r"MERGE_LEASE_QUEUE base=main holder=4 held_s=\S+ waiters=1 order=5\n")
		self.assertIn(WAIT, h.state["labels"]["5"])
		self.assertNotIn(WAIT, h.state["labels"]["6"])
		rec = h.record()
		self.assertEqual(_holders(rec), ["4", "6"])
		self.assertTrue(any("pr_number=6" in " ".join(d) for d in h.state.get("dispatched", [])))
		self.assertIn(("skipped", "5", "holder_shares_paths", "4"), [(r["event"], r["pr"], r["reason"], r["holder"]) for r in _metrics(out)])

	def test_revoking_one_holder_keeps_the_other(self) -> None:
		st = {"pulls": [dict(_pull(4), draft=True), _pull(6)]}
		_seed_lease(st, _multi_record([_row(4, ["a.py"]), _row(6, ["b.py"])]))
		_gates_ok(st, 6)
		h, out = _p2_tick(st, env={MAXH: "2"})
		self.assertRegex(out, r"MERGE_LEASE pr=4 base=main mode=tick outcome=released reason=revoked_draft held_s=\d+ merged=false\n")
		self.assertIn("pr=6 base=main mode=tick outcome=held reason=holder_open holders=1", out)
		rec = h.record()
		self.assertEqual(_v1_fields(rec)["pr"], "6")
		self.assertEqual(_skips(rec), [f"4:{_sha(4)}:revoked_draft"])

	def test_free_lease_with_two_slots_grants_two_disjoint_waiters(self) -> None:
		st = {"pulls": [_pull(5), _pull(6)], "labels": {"5": [WAIT], "6": [WAIT]}, "files": {"5": ["a.py"], "6": ["b.py"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		_gates_ok(st, 5, 6)
		h, out = _p2_tick(st, env={MAXH: "2"})
		self.assertIn("pr=5 base=main mode=tick outcome=granted reason=first_in_queue queue_len=2 holders=1", out)
		self.assertIn("pr=6 base=main mode=tick outcome=granted reason=first_in_queue queue_len=1 holders=2", out)
		self.assertRegex(out, r"MERGE_LEASE_QUEUE base=main holder=5 held_s=\d+ waiters=0 order=-\n")
		self.assertEqual(_holders(h.record()), ["5", "6"])

	def test_lost_race_on_a_secondary_release_stops_the_base(self) -> None:
		st = {"pulls": [_pull(4)], "closed": [6], "labels": {}}
		_seed_lease(st, _multi_record([_row(4, ["a.py"]), _row(6, ["b.py"])]))
		_gates_ok(st, 4)
		winner = "d" * 40
		st["commits"][winner] = {"message": _record(8), "tree": "f" * 40, "parents": ["c" * 40]}
		st["move_ref_on_patch"] = {REF: winner}
		h, out = _p2_tick(st, env={MAXH: "2"})
		self.assertIn("pr=6 base=main mode=tick outcome=lost_race reason=holder_closed", out)
		self.assertNotIn("dispatched", h.state)
		self.assertEqual(h.state["refs"][REF], winner)

	def test_n1_tick_keeps_the_single_holder_path(self) -> None:
		pulls = [_pull(5)]
		labels = {"5": [WAIT]}
		st = {"pulls": pulls, "labels": labels, "files": {"5": ["a"]}, "closed": [4]}
		_seed_lease(st, _record(4))
		_gates_ok(st, 5)
		_, out = _p2_tick(st)
		self.assertNotIn("holders=", out)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted reason=first_in_queue queue_len=1\n", out)


class LeaseQueueSnapshot(unittest.TestCase):
	"""Merge-lease parallel merges Phase 1 steps 1.2-1.3 (Refs #9781): the
	queue order is exposed and logged once per lease ref, with no API call."""

	def test_queue_first_sets_the_order_and_keeps_its_output(self) -> None:
		pulls = json.dumps([dict(_pull(n, head_ref=FM if n == 12 else None), labels=[{"name": WAIT}] if n != 7 else [])
			for n in (5, 7, 9, 12)])
		body = ('first="$(_merge_lease_queue_first "${PULLS}" "")"; _merge_lease_queue_first "${PULLS}" "" "" 9 >/dev/null; '
			'echo "first=$first len=$MERGE_LEASE_QUEUE_LEN prs=$MERGE_LEASE_QUEUE_PRS order=$(_merge_lease_queue_order "${PULLS}")"')
		res = _Harness({}).lib(body, {"PULLS": pulls})
		self.assertIn("first=12 len=2 prs=12,5 order=12,5,9", res.stdout, res.stderr)
		empty = _Harness({}).lib('_merge_lease_queue_first "[]" "" >/dev/null; echo "len=$MERGE_LEASE_QUEUE_LEN prs=<$MERGE_LEASE_QUEUE_PRS> order=<$(_merge_lease_queue_order "[]")>"')
		self.assertIn("len=0 prs=<> order=<>", empty.stdout, empty.stderr)

	def test_held_tick_logs_one_queue_line_without_the_holder(self) -> None:
		pulls = [_pull(4), _pull(5), _pull(6)]
		labels = {"4": [WAIT], "5": [WAIT], "6": [WAIT]}
		st = {"pulls": pulls, "labels": labels}
		_seed_lease(st, _record(4, acquired=int(time.time()) - 120))
		_gates_ok(st, 4)
		h, out = _p2_tick(st)
		queue = [ln for ln in out.splitlines() if ln.startswith("MERGE_LEASE_QUEUE ")]
		self.assertEqual(len(queue), 1, out)
		self.assertRegex(queue[0], r"^MERGE_LEASE_QUEUE base=main holder=4 held_s=\d+ waiters=2 order=5,6$")
		self.assertFalse(any("-X PATCH" in c or "-X POST" in c for c in h.calls), h.calls)

	def test_free_lease_without_waiters_logs_an_empty_queue(self) -> None:
		st = {"pulls": [_pull(5)]}
		_seed_lease(st, _record(3, state="released", paths=""))
		_, out = _p2_tick(st)
		self.assertIn("MERGE_LEASE_QUEUE base=main holder=- held_s=- waiters=0 order=-\n", out)

	def test_ineligible_waiter_unlabelled_this_tick_leaves_the_order(self) -> None:
		st = {"pulls": [dict(_pull(5), title="[skip ai]"), _pull(6), _pull(7)], "labels": {"5": [WAIT], "6": [WAIT], "7": [WAIT]},
			"files": {"6": ["a"]}}
		_seed_lease(st, _record(3, state="released", paths=""))
		_gates_ok(st, 6, 7)
		_, out = _p2_tick(st)
		self.assertIn("MERGE_LEASE pr=6 base=main mode=tick outcome=granted reason=first_in_queue queue_len=2\n", out)
		self.assertRegex(out, r"MERGE_LEASE_QUEUE base=main holder=6 held_s=\d+ waiters=1 order=7\n")

	def test_library_without_the_queue_helper_keeps_the_old_grant_line(self) -> None:
		st = {"pulls": [_pull(5)], "labels": {"5": [WAIT]}, "files": {"5": ["a"]}, "closed": [4]}
		_seed_lease(st, _record(4))
		_gates_ok(st, 5)
		h = _Harness(st)
		res = h.train_fn('unset -f _merge_lease_queue_order\n_mt_merge_lease_tick "${PRS_JSONL}" ""',
			{"PRS_JSONL": _p2_jsonl(st["pulls"], st["labels"])})
		out = res.stdout + res.stderr
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted reason=first_in_queue\n", out)
		self.assertNotIn("MERGE_LEASE_QUEUE", out)


class MultiHolderWiring(unittest.TestCase):
	def test_max_holders_switch_reaches_every_lease_path(self) -> None:
		line = "MERGE_LEASE_MAX_HOLDERS: ${{ vars.MERGE_LEASE_MAX_HOLDERS || '1' }}"
		for wf, expected, before in ((REVIEW, 3, "MERGE_LEASE_SOFT_PATHS: ${{ vars."), (POLL, 4, "MERGE_LEASE_SOFT_PATHS: ${{ vars."),
				(CLOSE, 1, "MERGE_LEASE_SOFT_PATHS: ${{ vars."), (SWEEP, 1, "MERGE_LEASE_WAIT_LABEL: ${{ vars.")):
			lines = wf.read_text().splitlines()
			hits = [i for i, ln in enumerate(lines) if ln.strip() == line]
			self.assertEqual(len(hits), expected, wf.name)
			for i in hits:
				self.assertTrue(lines[i - 1].strip().startswith(before), f"{wf.name}:{i + 1}")
				self.assertEqual(len(lines[i]) - len(lines[i].lstrip()), len(lines[i - 1]) - len(lines[i - 1].lstrip()))
		self.assertIn("${MERGE_LEASE_MAX_HOLDERS:-1}", LIB.read_text())



# --- Holders kept while their runs are queued (issue #9983) ---------------

def _iso(age_s: int) -> str:
	return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_s))


REVIEW_WF = "Internal: AI Review & Autofix"


def _dispatch_run(pr: int, *, run_id: int = 31, status: str = "queued", age_s: int = 600) -> dict:
	"""The sweep's review dispatch: runs on the default branch, titled for its PR."""
	return {"id": run_id, "name": f"{REVIEW_WF} [pr:{pr}]", "display_title": f"{REVIEW_WF} [pr:{pr}]", "status": status,
		"event": "workflow_dispatch", "head_branch": "main", "head_sha": "9" * 40,
		"path": ".github/workflows/internal-review.yml", "created_at": _iso(age_s)}


def _head_review_run(*, status: str = "queued", age_s: int = 600) -> dict:
	return {"id": 21, "name": REVIEW_WF, "status": status, "event": "pull_request", "created_at": _iso(age_s)}


def _calls_with(h: _Harness, needle: str) -> int:
	return sum(needle in c for c in h.calls)


class HolderRunsActive(unittest.TestCase):
	_holder = LeaseRevocation._holder

	def test_pending_head_gate_holder_with_a_queued_review_run_is_held(self) -> None:
		h, out = _p2_tick(self._holder(gate=None, updated=OLD, extra={"runs": {_sha(4): [_head_review_run()]}}))
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_runs_active\n", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)
		self.assertIn(("held", "holder_runs_active"), [(r["event"], r["reason"]) for r in _metrics(out)])

	def test_pending_head_gate_holder_without_runs_is_revoked(self) -> None:
		h, out = _p2_tick(self._holder(gate=None, updated=OLD))
		self.assertRegex(out, r"outcome=released reason=revoked_no_head_gate held_s=\S+ merged=false\n")
		self.assertEqual(_skips(h.record()), [f"4:{_sha(4)}:revoked_no_head_gate"])

	def test_idle_holder_with_a_queued_review_dispatch_run_is_held(self) -> None:
		h, out = _p2_tick(self._holder(updated=OLD, extra={"active_runs": [_dispatch_run(4)]}))
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_runs_active\n", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_idle_holder_without_dispatch_run_is_revoked_idle(self) -> None:
		# A dispatch for another PR, and one on a non-default branch, do not count.
		other = dict(_dispatch_run(4, run_id=32), head_branch="feature")
		h, out = _p2_tick(self._holder(updated=OLD, extra={"active_runs": [_dispatch_run(5), other]}))
		self.assertIn("outcome=released reason=revoked_idle", out)
		self.assertEqual(_skips(h.record()), [f"4:{_sha(4)}:revoked_idle"])

	def test_an_in_progress_run_on_the_head_in_the_listing_keeps_the_holder(self) -> None:
		run = {"id": 33, "name": "Deploy", "status": "in_progress", "event": "push", "head_branch": "ai/issue-4",
			"head_sha": _sha(4), "path": ".github/workflows/deploy.yml", "created_at": _iso(60)}
		h, out = _p2_tick(self._holder(updated=OLD, extra={"active_runs": [run]}))
		self.assertIn("outcome=held reason=holder_runs_active", out)

	def test_an_unreadable_listing_keeps_the_holder(self) -> None:
		h, out = _p2_tick(self._holder(updated=OLD, extra={"fail": ["actions/runs?status="]}))
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_open\n", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_wedged_queued_run_does_not_keep_the_holder(self) -> None:
		wedged = 30 * 3600
		h, out = _p2_tick(self._holder(gate=None, updated=OLD, extra={"runs": {_sha(4): [_head_review_run(age_s=wedged)]}}))
		self.assertIn("outcome=released reason=revoked_no_head_gate", out)
		h, out = _p2_tick(self._holder(updated=OLD, extra={"active_runs": [_dispatch_run(4, age_s=wedged)]}))
		self.assertIn("outcome=released reason=revoked_idle", out)
		# An in-progress run is never wedged, whatever its age.
		h, out = _p2_tick(self._holder(updated=OLD, extra={"active_runs": [_dispatch_run(4, status="in_progress", age_s=wedged)]}))
		self.assertIn("outcome=held reason=holder_runs_active", out)
		# The threshold is ZOMBIE_RUN_MAX_QUEUED_HOURS.
		h, out = _p2_tick(self._holder(gate=None, updated=OLD, extra={"runs": {_sha(4): [_head_review_run(age_s=wedged)]}}),
			env={"ZOMBIE_RUN_MAX_QUEUED_HOURS": "48"})
		self.assertIn("outcome=held reason=holder_runs_active", out)

	def test_holder_runs_active_api_budget(self) -> None:
		h, _ = _p2_tick(self._holder(gate=None, updated=OLD, extra={"runs": {_sha(4): [_head_review_run()]}}))
		self.assertEqual(_calls_with(h, "actions/runs?head_sha="), 1, h.calls)
		self.assertEqual(_calls_with(h, "actions/runs?status="), 0, h.calls)
		h, _ = _p2_tick(self._holder(updated=OLD, extra={"active_runs": [_dispatch_run(4)]}))
		self.assertEqual(_calls_with(h, "actions/runs?head_sha="), 1, h.calls)
		self.assertGreater(_calls_with(h, "actions/runs?status="), 0, h.calls)
		self.assertLessEqual(_calls_with(h, "actions/runs?status="), 10, h.calls)
		# Within the idle time no runs read and no listing.
		h, _ = _p2_tick(self._holder())
		self.assertEqual(_calls_with(h, "actions/runs?status="), 0, h.calls)

	def test_multi_holder_keeps_the_active_holder_and_lists_runs_once(self) -> None:
		now = int(time.time())
		rows = [(4, _sha(4), now + 3600, OLD, OLD, ["a.py"]), (6, _sha(6), now + 3600, OLD, OLD, ["b.py"])]
		st = {"pulls": [_pull(4), _pull(6)], "active_runs": [_dispatch_run(4)]}
		_seed_lease(st, _multi_record(rows))
		_gates_ok(st, 4, 6)
		h, out = _p2_tick(st, env={MAXH: "2"})
		self.assertIn("outcome=released reason=revoked_idle", out)
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_runs_active holders=1\n", out)
		self.assertEqual(_skips(h.record()), [f"6:{_sha(6)}:revoked_idle"])
		self.assertLessEqual(_calls_with(h, "actions/runs?status="), 10, h.calls)
		self.assertEqual(_calls_with(h, "repos/owner/repo --jq"), 1, h.calls)

	def test_pending_head_gate_holder_with_a_queued_dispatch_review_is_held(self) -> None:
		# The sweep's review dispatch runs on the default branch, so the head
		# runs read cannot see it; the cached listing does (review of #9983).
		h, out = _p2_tick(self._holder(gate=None, updated=OLD, extra={"active_runs": [_dispatch_run(4)]}))
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_runs_active\n", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)
		self.assertEqual(_calls_with(h, "actions/runs?head_sha="), 1, h.calls)

	def test_pending_head_gate_holder_with_an_unreadable_listing_is_kept(self) -> None:
		h, out = _p2_tick(self._holder(gate=None, updated=OLD, extra={"fail": ["actions/runs?status="]}))
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_open\n", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_an_incomplete_listing_keeps_the_idle_holder(self) -> None:
		# A review dispatch without a `[pr:<n>]` title could be the holder's:
		# the listing is incomplete (unattributed_run), so the holder is kept.
		unattributed = dict(_dispatch_run(9), display_title="AI Review & Autofix", name="AI Review & Autofix",
			path=".github/workflows/review_autofix.yml")
		h, out = _p2_tick(self._holder(updated=OLD, extra={"active_runs": [unattributed]}))
		self.assertIn("reason=unattributed_run", out)
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_open\n", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_a_seeded_listing_is_the_ticks_only_listing_pass(self) -> None:
		st = self._holder(updated=OLD, extra={"active_runs": [_dispatch_run(4)]})
		h0, _ = _p2_tick(st)
		one_pass = _calls_with(h0, "actions/runs?status=")
		self.assertGreater(one_pass, 0, h0.calls)
		h = _Harness(st)
		res = h.train_fn('_mt_inflight_review_branches >/dev/null; _mt_lease_runs_seed $?; _MT_LEASE_RUNS_SEEDED=1\n'
			'_mt_merge_lease_tick "${PRS_JSONL}" ""', {"PRS_JSONL": _p2_jsonl(st["pulls"], {})})
		out = res.stdout + res.stderr
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_runs_active\n", out)
		self.assertEqual(_calls_with(h, "actions/runs?status="), one_pass, h.calls)

	def test_release_pass_listing_seeds_the_lease_tick(self) -> None:
		st = self._holder(updated=OLD, extra={"active_runs": [_dispatch_run(4)]})
		st["pulls"].append(_pull(5))
		st["labels"] = {"5": ["ai:merge-queued"]}
		# PR 5's dispatch fails, so this case covers the plain reuse path; a
		# holder whose review the pass just dispatched is covered in
		# test_a_review_dispatched_by_the_release_pass_keeps_the_holder.
		st["fail"] = ["workflow run"]
		h0, _ = _p2_tick(self._holder(updated=OLD, extra={"active_runs": [_dispatch_run(4)]}))
		one_pass = _calls_with(h0, "actions/runs?status=")
		h = _Harness(st)
		res = h.train_fn("_mt_release", {"MT_REPO": "owner/repo"})
		out = res.stdout + res.stderr
		# The release pass made the listing (queued PR 5) and the tick reused it.
		self.assertIn("active_listing_calls=1 ", out)
		self.assertIn("dispatch_failed=1 ", out)
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_runs_active\n", out)
		self.assertEqual(_calls_with(h, "actions/runs?status="), one_pass, h.calls)

	def test_a_review_dispatched_by_the_release_pass_keeps_the_holder(self) -> None:
		# The holder is also merge-queued: the release pass lists the runs,
		# then dispatches its review. GitHub creates the dispatched run after
		# the 204, so the run may or may not be listed yet: either way the
		# holder is kept and the tick reuses the release pass's listing (#9983).
		h0, _ = _p2_tick(self._holder(updated=OLD, extra={"active_runs": []}))
		one_pass = _calls_with(h0, "actions/runs?status=")
		self.assertGreater(one_pass, 0, h0.calls)
		for visible in (True, False):
			with self.subTest(run_visible=visible):
				st = self._holder(updated=OLD, extra={"active_runs": [], "dispatch_adds_run": visible})
				st["labels"] = {"4": ["ai:merge-queued"]}
				h = _Harness(st)
				res = h.train_fn("_mt_release", {"MT_REPO": "owner/repo"})
				out = res.stdout + res.stderr
				self.assertIn("MERGE_TRAIN_RELEASED pr=4 source=release", out)
				self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_runs_active\n", out)
				self.assertNotIn("reason=revoked_idle", out)
				self.assertEqual(h.state["refs"][REF], "c" * 40)
				self.assertEqual(_calls_with(h, "actions/runs?status="), one_pass, h.calls)

	def test_revocation_off_is_unchanged(self) -> None:
		h, out = _p2_tick(self._holder(gate=None, updated=OLD), env={"MERGE_LEASE_REVOKE_ENABLED": "false"})
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertFalse(any("actions/runs" in c for c in h.calls), h.calls)


class SoftSkipEntries(unittest.TestCase):
	def _free(self, skips: list[str], head5: str | None = None) -> dict:
		st = {"pulls": [_pull(5, sha=head5), _pull(6)], "labels": {"5": [WAIT], "6": [WAIT]}, "files": {"5": ["a"], "6": ["b"]}}
		_seed_lease(st, _record(3, state="released", paths="", skips=skips))
		_gates_ok(st, 6)
		return st

	def test_no_head_gate_skip_entry_clears_once_the_head_gate_succeeds(self) -> None:
		entry = f"5:{_sha(5)}:revoked_no_head_gate"
		st = self._free([entry])
		_head_gate(st, _sha(5), "pending")
		h, out = _p2_tick(st)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=skipped reason=skip_head_unchanged\n", out)
		self.assertIn("pr=6 base=main mode=tick outcome=granted", out)
		self.assertIn(WAIT, h.state["labels"]["5"], "a still-pending soft waiter keeps its label")
		self.assertEqual(_skips(h.record()), [entry])
		st = self._free([entry])
		_head_gate(st, _sha(5))
		h, out = _p2_tick(st)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted", out)
		self.assertNotIn("skip_head_unchanged", out)
		self.assertEqual(_skips(h.record()), [], "the new holder's own entry is dropped")

	def test_revoked_idle_skip_entry_blocks_until_the_head_changes(self) -> None:
		entry = f"5:{_sha(5)}:revoked_idle"
		st = self._free([entry])
		_head_gate(st, _sha(5))
		h, out = _p2_tick(st)
		self.assertIn("pr=5 base=main mode=tick outcome=skipped reason=skip_head_unchanged", out)
		self.assertIn("pr=6 base=main mode=tick outcome=granted", out)
		self.assertEqual(_skips(h.record()), [entry])
		st = self._free([entry], head5="e" * 40)
		_head_gate(st, "e" * 40)
		h, out = _p2_tick(st)
		self.assertIn("pr=5 base=main mode=tick outcome=granted", out)
		self.assertEqual(_skips(h.record()), [])

	def test_two_field_skip_entry_is_read_as_revoked_no_head_gate(self) -> None:
		h = _Harness({})
		two, three, hard = f"5:{_sha(5)}", f"6:{_sha(6)}:revoked_no_head_gate", f"7:{_sha(7)}:revoked_idle"
		res = h.lib(f'L="{two}"$\'\\n\'"{three}"$\'\\n\'"{hard}"$\'\\n\'"8:{_sha(8)}:Bad"; '
			'printf "SOFT<%s>\\nHARD<%s>\\n" "$(_merge_lease_skips_soft "$L")" "$(_merge_lease_skips_hard "$L")"; '
			f'_merge_lease_skip_is_soft "{two}" && echo two_soft; _merge_lease_skip_is_soft "{hard}" || echo hard_not_soft')
		self.assertIn(f"SOFT<{two}\n{three}>", res.stdout, res.stderr)
		self.assertIn(f"HARD<{hard}>", res.stdout)
		self.assertIn("two_soft\nhard_not_soft", res.stdout)
		# Normalize keeps both forms; a newer reason for the same pr:head wins.
		res = h.lib(f'_merge_lease_skips_normalize "{two}"$\'\\n\'"{hard}"$\'\\n\'"5:{_sha(5)}:revoked_idle"')
		self.assertEqual(res.stdout, f"{hard}\n5:{_sha(5)}:revoked_idle\n", res.stderr)
		# In the tick a two-field entry clears once the head-gate succeeds.
		st = self._free([two])
		_head_gate(st, _sha(5))
		_, out = _p2_tick(st)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted", out)

	def test_soft_entry_with_an_unreadable_head_gate_stays_skip_held(self) -> None:
		# A failed head-gate read keeps the soft entry (#9983 G5): the
		# fail-open eligibility must not grant the waiter or drop its entry.
		entry = f"5:{_sha(5)}:revoked_no_head_gate"
		st = self._free([entry])
		_head_gate(st, _sha(5))
		st["fail"] = [f"commits/{_sha(5)}/statuses"]
		h, out = _p2_tick(st)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=skipped reason=skip_head_unchanged\n", out)
		self.assertIn("pr=6 base=main mode=tick outcome=granted", out)
		self.assertIn(WAIT, h.state["labels"]["5"])
		self.assertEqual(_skips(h.record()), [entry])

	def test_soft_entry_with_a_confirmed_gate_and_failed_final_ci_is_unlabelled(self) -> None:
		# The confirmed head-gate success clears the soft entry (G5); the
		# waiter's remaining ineligibility takes the normal path.
		st = self._free([f"5:{_sha(5)}:revoked_no_head_gate"])
		_head_gate(st, _sha(5))
		st["check_runs"] = {_sha(5): [{"id": 1, "name": "tests", "status": "completed", "conclusion": "failure"}]}
		h, out = _p2_tick(st, env={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "tests"})
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=skipped reason=final_ci_failed\n", out)
		self.assertNotIn("pr=5 base=main mode=tick outcome=skipped reason=skip_head_unchanged", out)
		self.assertNotIn(WAIT, h.state["labels"]["5"])
		self.assertIn("pr=6 base=main mode=tick outcome=granted", out)

	def test_multi_tick_soft_entry_with_a_confirmed_gate_and_failed_final_ci_is_unlabelled(self) -> None:
		st = self._free([f"5:{_sha(5)}:revoked_no_head_gate"])
		_head_gate(st, _sha(5))
		st["check_runs"] = {_sha(5): [{"id": 1, "name": "tests", "status": "completed", "conclusion": "failure"}]}
		h, out = _p2_tick(st, env={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "tests", MAXH: "2"})
		self.assertIn("pr=5 base=main mode=tick outcome=skipped reason=final_ci_failed", out)
		self.assertNotIn("pr=5 base=main mode=tick outcome=skipped reason=skip_head_unchanged", out)
		self.assertNotIn(WAIT, h.state["labels"]["5"])

	def test_three_field_entries_survive_the_prune(self) -> None:
		st = {"pulls": [_pull(5), _pull(6)], "labels": {"6": [WAIT]}, "files": {"6": ["b"]}}
		_seed_lease(st, _record(3, state="released", paths="", skips=[f"5:{_sha(5)}:revoked_idle", f"9:{_sha(9)}:expired"]))
		_gates_ok(st, 6)
		h, _ = _p2_tick(st)
		self.assertEqual(_skips(h.record()), [f"5:{_sha(5)}:revoked_idle"])

	def _gate_state(self, entry: str, gate: str | None, login: str = "pipeline-bot") -> dict:
		st = LeaseSkipList()._gate_state(_sha(7), [9], [entry])
		if gate is not None:
			_head_gate(st, _sha(7), gate, login)
		return st

	def test_gate_caller_with_cleared_no_head_gate_entry_takes_its_queue_place(self) -> None:
		for entry in (f"7:{_sha(7)}:revoked_no_head_gate", f"7:{_sha(7)}"):
			with self.subTest(entry=entry):
				h = _Harness(self._gate_state(entry, "success"))
				rc, out = _gate(h, 7, "update")
				self.assertEqual(rc, 0, out)
				self.assertIn("outcome=acquired", out)
				self.assertEqual(_skips(h.record()), [])
				self.assertEqual(_calls_with(h, "api user"), 1, h.calls)
				self.assertEqual(_calls_with(h, f"commits/{_sha(7)}/statuses"), 1, h.calls)

	def test_gate_keeps_a_soft_entry_without_a_trusted_success(self) -> None:
		for gate, login in (("pending", "pipeline-bot"), ("success", "someone-else"), (None, "")):
			with self.subTest(gate=gate, login=login):
				rc, out = _gate(_Harness(self._gate_state(f"7:{_sha(7)}:revoked_no_head_gate", gate, login or "x")), 7, "update")
				self.assertEqual(rc, 1, out)
				self.assertIn("outcome=wait reason=queued_behind blocker=9", out)

	def test_gate_hard_entry_makes_no_extra_call(self) -> None:
		h = _Harness(self._gate_state(f"7:{_sha(7)}:revoked_idle", "success"))
		rc, out = _gate(h, 7, "update")
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=queued_behind blocker=9", out)
		self.assertEqual(_calls_with(h, "api user"), 0, h.calls)
		self.assertEqual(_calls_with(h, "/statuses"), 0, h.calls)

	def test_revocation_off_ignores_three_field_entries(self) -> None:
		h = _Harness(self._gate_state(f"7:{_sha(7)}:revoked_idle", None))
		rc, out = _gate(h, 7, "update", {"MERGE_LEASE_REVOKE_ENABLED": "false"})
		self.assertEqual(rc, 0, out)
		self.assertNotIn("skip=", h.record())


class RunsHelpers(unittest.TestCase):
	def test_wedged_threshold_validation(self) -> None:
		h = _Harness({})
		for raw, want, warns in (("", "86400", False), ("48", "172800", False), ("abc", "86400", True), ("0", "86400", True)):
			with self.subTest(raw=raw):
				res = h.lib("_merge_lease_wedged_queued_seconds", {"ZOMBIE_RUN_MAX_QUEUED_HOURS": raw})
				self.assertEqual(res.stdout.strip(), want)
				self.assertEqual("::warning::ZOMBIE_RUN_MAX_QUEUED_HOURS" in res.stderr, warns, res.stderr)

	def test_live_count_leaves_the_other_counts_unchanged(self) -> None:
		runs = [{"id": 1, "name": "CI", "status": "queued", "event": "pull_request", "created_at": _iso(30 * 3600)},
			{"id": 2, "name": "CI", "status": "in_progress", "event": "pull_request", "created_at": _iso(30 * 3600)},
			{"id": 3, "name": "CI", "status": "queued", "event": "pull_request"}]
		h = _Harness({"runs": {HEAD: runs}})
		res = h.lib(f'_pr_head_ci_runs_pending {HEAD}; echo "p=$PR_CHECKS_RUNS_PENDING u=$PR_CHECKS_RUNS_UNRUN l=$PR_CHECKS_RUNS_PENDING_LIVE r=$PR_CHECKS_RUNS_READ"')
		self.assertIn("p=3 u=0 l=2 r=true", res.stdout, res.stderr)
		cancelled = [{"id": 4, "name": "CI", "status": "completed", "conclusion": "cancelled", "event": "pull_request"}]
		res = _Harness({"runs": {HEAD: cancelled}}).lib(f'_pr_head_ci_runs_pending {HEAD}; echo "p=$PR_CHECKS_RUNS_PENDING u=$PR_CHECKS_RUNS_UNRUN l=$PR_CHECKS_RUNS_PENDING_LIVE"')
		self.assertIn("p=0 u=1 l=0", res.stdout, res.stderr)

	def test_listing_keeps_its_raw_runs_only_when_complete(self) -> None:
		run = dict(_dispatch_run(4), head_sha="7" * 40)
		h = _Harness({"active_runs": [run]})
		body = '_mt_inflight_review_branches; echo "rc=$?"; printf "ALL<%s>\\n" "${MT_INFLIGHT_RUNS_ALL_JSON}"'
		res = h.train_fn(body)
		self.assertIn("pr:4\nrc=0", res.stdout, res.stdout + res.stderr)
		raw = json.loads(re.search(r"ALL<(.*)>", res.stdout).group(1))
		self.assertTrue(raw and all(r["head_sha"] == "7" * 40 for r in raw), raw)
		res = _Harness({"active_runs": [run], "fail": ["actions/runs?status="]}).train_fn(body)
		self.assertIn("rc=1\nALL<>", res.stdout, res.stdout + res.stderr)
		self.assertIn("reason=page_failed", res.stderr)


if __name__ == "__main__":
	unittest.main()

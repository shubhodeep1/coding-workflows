#!/usr/bin/env python3
"""Merge lease ready-first grant and conflict release (issue #10098, Phase 1
of docs/plans/merge-lease-short-critical-section-plan.md; Refs #10097).

The merge-lease tick used to grant a slot on the head-gate alone, so a waiter
whose `ci/final` was still pending, or that conflicted with its base, held
the lease for 4-5 hours. These tests drive the shipped tick
(scripts/review_merge_train.sh) and library (scripts/pr_checks_lib.sh)
through the stateful fake `gh` of tests/test_merge_lease.py.
"""

from __future__ import annotations

import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_merge_lease import (  # noqa: E402
	CLOSE,
	LIB,
	POLL,
	REF,
	REPO_ROOT,
	REVIEW,
	WAIT,
	_gates_ok,
	_Harness,
	_p2_jsonl,
	_pull,
	_record,
	_seed_lease,
	_sha,
	_skips,
	_stable_calls,
	_stable_record,
)

FCI = {"REVIEW_FINAL_CI_WORKFLOW": "ci.yml"}
OFF = {"MERGE_LEASE_READY_GRANT_ENABLED": "false", "MERGE_LEASE_CONFLICT_RELEASE_ENABLED": "false"}
CONFLICT_LABEL = "ai:merge-lease-conflict-released"
OLD = int(time.time()) - 3 * 3600
NEW_FUNCS = ("_merge_lease_pr_ready _merge_lease_ready_grant_enabled _merge_lease_conflict_release_enabled "
	"_merge_lease_label_conflict_released _merge_lease_pr_mergeable _merge_lease_has_conflict_released_label")


def _ci_final(st: dict, num: int, state: str) -> None:
	st.setdefault("statuses", {}).setdefault(_sha(num), []).insert(0,
		{"context": "ci/final", "state": state, "creator": {"login": "github-actions[bot]"}})


def _free(pulls: list[dict], waiting: list[int], *, updated: int | None = None) -> dict:
	st = {"pulls": pulls, "labels": {str(n): [WAIT] for n in waiting}, "files": {str(p["number"]): [f"f{p['number']}.py"] for p in pulls}}
	_seed_lease(st, _record(3, state="released", paths="", updated=updated))
	return st


def _held(holder: dict, waiters: list[dict], *, holder_labels: list[str] | None = None) -> dict:
	now = int(time.time())
	st = {"pulls": [holder, *waiters], "labels": {str(holder["number"]): list(holder_labels or [])},
		"files": {str(p["number"]): [f"f{p['number']}.py"] for p in [holder, *waiters]}}
	for w in waiters:
		st["labels"][str(w["number"])] = [WAIT]
	_seed_lease(st, _record(holder["number"], acquired=now, updated=now))
	_gates_ok(st, holder["number"], *[w["number"] for w in waiters])
	return st


def _tick(st: dict, env: dict[str, str] | None = None, prelude: str = "") -> tuple[_Harness, str]:
	h = _Harness(st)
	res = h.train_fn(f'{prelude}\n_mt_merge_lease_tick "${{PRS_JSONL}}" ""',
		{"PRS_JSONL": _p2_jsonl(st.get("pulls", []), st.get("labels", {})), **(env or {})})
	return h, res.stdout + res.stderr


def _ci_dispatches(h: _Harness) -> list[list[str]]:
	return [d for d in h.state.get("dispatched", []) if "ci.yml" in d]


def _lease_lines(out: str) -> list[str]:
	"""MERGE_LEASE* lines with hold times and epoch timestamps normalized."""
	return [re.sub(r"=\d{9,}\b", "=N", re.sub(r"\b(held_s|hold_s)=\d+", r"\1=N", ln)) for ln in out.splitlines() if ln.startswith("MERGE_LEASE")]


class ReadyGrantTick(unittest.TestCase):
	def test_ready_waiter_is_granted(self) -> None:
		st = _free([_pull(5)], [5])
		_gates_ok(st, 5)
		_ci_final(st, 5, "success")
		h, out = _tick(st, FCI)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted reason=first_in_queue", out)
		self.assertIn("pr=5", h.record())
		self.assertIn("gh api repos/owner/repo/pulls/5", h.calls)

	def test_final_ci_pending_waiter_is_skipped_and_keeps_label(self) -> None:
		st = _free([_pull(5), _pull(6)], [5, 6])
		_gates_ok(st, 5, 6)
		_ci_final(st, 5, "pending")
		_ci_final(st, 6, "success")
		h, out = _tick(st, FCI)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=skipped reason=final_ci_pending\n", out)
		self.assertIn("MERGE_LEASE pr=6 base=main mode=tick outcome=granted reason=first_in_queue", out)
		self.assertIn(WAIT, h.state["labels"]["5"], "a waiter that is not ready keeps its label")
		self.assertEqual(_skips(h.record()), [])
		self.assertIn("pr=6", h.record())
		self.assertNotIn("gh api repos/owner/repo/pulls/5", h.calls, "no mergeability read after final_ci_pending")
		self.assertEqual(_ci_dispatches(h), [], "a pending ci/final proves a run exists")

	def test_unmergeable_waiter_is_skipped(self) -> None:
		for fields in ({"mergeable": False, "mergeable_state": "dirty"}, {"mergeable": None, "mergeable_state": "dirty"}):
			st = _free([dict(_pull(5), **fields), _pull(6)], [5, 6])
			_gates_ok(st, 5, 6)
			_ci_final(st, 5, "success")
			_ci_final(st, 6, "success")
			h, out = _tick(st, FCI)
			self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=skipped reason=not_mergeable\n", out, fields)
			self.assertIn("pr=6 base=main mode=tick outcome=granted", out, fields)
			self.assertIn(WAIT, h.state["labels"]["5"])
			self.assertEqual(_skips(h.record()), [])

	def test_null_mergeable_passes(self) -> None:
		st = _free([dict(_pull(5), mergeable=None, mergeable_state="unknown")], [5])
		_gates_ok(st, 5)
		h, out = _tick(st)
		self.assertIn("pr=5 base=main mode=tick outcome=granted reason=first_in_queue", out)

	def test_final_ci_disabled_grants_mergeable_waiter(self) -> None:
		st = _free([dict(_pull(5), mergeable=True, mergeable_state="clean")], [5])
		_gates_ok(st, 5)
		h, out = _tick(st)
		self.assertIn("pr=5 base=main mode=tick outcome=granted reason=first_in_queue", out)
		self.assertNotIn("final_ci_pending", out)

	def test_pull_read_failure_grants(self) -> None:
		st = _free([_pull(5)], [5])
		_gates_ok(st, 5)
		_ci_final(st, 5, "success")
		st["pull_fail"] = [5]
		h, out = _tick(st, FCI)
		self.assertIn("pr=5 base=main mode=tick outcome=granted reason=first_in_queue", out)

	def test_no_ready_waiter_past_idle_grants_ready_fallback(self) -> None:
		st = _free([_pull(5), dict(_pull(6), mergeable=False)], [5, 6], updated=OLD)
		_gates_ok(st, 5, 6)
		_ci_final(st, 5, "pending")
		_ci_final(st, 6, "success")
		h, out = _tick(st, FCI)
		self.assertIn("pr=5 base=main mode=tick outcome=skipped reason=final_ci_pending\n", out)
		self.assertIn("pr=6 base=main mode=tick outcome=skipped reason=not_mergeable\n", out)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted reason=ready_fallback", out)
		self.assertIn("pr=5", h.record())
		self.assertIn(WAIT, h.state["labels"]["6"])

	def test_free_slot_not_yet_idle_grants_nothing(self) -> None:
		st = _free([_pull(5)], [5])
		_gates_ok(st, 5)
		_ci_final(st, 5, "pending")
		h, out = _tick(st, FCI)
		self.assertIn("outcome=no_waiter reason=no_eligible_waiter", out)
		self.assertNotIn("ready_fallback", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)
		self.assertIn(WAIT, h.state["labels"]["5"])

	def test_absent_final_ci_dispatches_once_per_base(self) -> None:
		st = _free([_pull(5), _pull(6)], [5, 6])
		_gates_ok(st, 5, 6)
		h, out = _tick(st, FCI)
		self.assertIn("pr=5 base=main mode=tick outcome=skipped reason=final_ci_pending", out)
		self.assertIn("pr=6 base=main mode=tick outcome=skipped reason=final_ci_pending", out)
		dispatches = _ci_dispatches(h)
		self.assertEqual(len(dispatches), 1, dispatches)
		self.assertIn("pr_number=5", dispatches[0])
		# A head with any final-CI run (here a completed one) gets no dispatch.
		st = _free([_pull(5)], [5])
		_gates_ok(st, 5)
		st["wf_runs"] = {_sha(5): [{"id": 70, "path": ".github/workflows/ci.yml", "head_sha": _sha(5),
			"status": "completed", "conclusion": "success", "display_title": "CI"}]}
		h, out = _tick(st, FCI)
		self.assertIn("reason=final_ci_pending", out)
		self.assertEqual(_ci_dispatches(h), [])

	def test_not_ready_waiter_keeps_the_skip_held_waiter_labelled(self) -> None:
		# D4 hands a skip-held waiter back to the sweep only when no other
		# waiter is left; a not-ready waiter keeps its label, so D4 waits.
		for env in (FCI, {**FCI, "MERGE_LEASE_MAX_HOLDERS": "2"}):
			st = {"pulls": [_pull(5), _pull(6)], "labels": {"5": [WAIT], "6": [WAIT]}, "files": {"5": ["f5.py"], "6": ["f6.py"]}}
			_seed_lease(st, _record(3, state="released", paths="", skips=[f"5:{_sha(5)}:revoked_draft"]))
			_gates_ok(st, 5, 6)
			_ci_final(st, 6, "pending")
			h, out = _tick(st, env)
			self.assertIn("pr=6 base=main mode=tick outcome=skipped reason=final_ci_pending\n", out, env)
			self.assertNotIn("outcome=granted", out, env)
			self.assertIn(WAIT, h.state["labels"]["5"], env)
			self.assertIn(WAIT, h.state["labels"]["6"], env)

	def test_fork_head_is_never_dispatched(self) -> None:
		fork = _pull(5)
		fork["head"] = dict(fork["head"], repo={"full_name": "someone/fork"})
		st = _free([fork], [5])
		_gates_ok(st, 5)
		h, out = _tick(st, FCI)
		self.assertIn("reason=final_ci_pending", out)
		self.assertEqual(_ci_dispatches(h), [])

	def test_multi_holder_pending_waiter_is_skipped(self) -> None:
		st = _free([_pull(5), _pull(6)], [5, 6])
		_gates_ok(st, 5, 6)
		_ci_final(st, 5, "pending")
		_ci_final(st, 6, "success")
		h, out = _tick(st, {**FCI, "MERGE_LEASE_MAX_HOLDERS": "2"})
		self.assertIn("pr=5 base=main mode=tick outcome=skipped reason=final_ci_pending\n", out)
		self.assertRegex(out, r"pr=6 base=main mode=tick outcome=granted reason=first_in_queue")
		self.assertIn(WAIT, h.state["labels"]["5"])
		self.assertIn("pr=6", h.record())

	def test_multi_holder_ready_fallback(self) -> None:
		st = _free([_pull(5)], [5], updated=OLD)
		_gates_ok(st, 5)
		_ci_final(st, 5, "pending")
		h, out = _tick(st, {**FCI, "MERGE_LEASE_MAX_HOLDERS": "2"})
		self.assertIn("pr=5 base=main mode=tick outcome=skipped reason=final_ci_pending\n", out)
		self.assertIn("pr=5 base=main mode=tick outcome=granted reason=ready_fallback", out)
		self.assertIn("pr=5", h.record())
		# Not idle yet: nothing granted.
		st = _free([_pull(5)], [5])
		_gates_ok(st, 5)
		_ci_final(st, 5, "pending")
		h, out = _tick(st, {**FCI, "MERGE_LEASE_MAX_HOLDERS": "2"})
		self.assertNotIn("ready_fallback", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)


class ConflictRelease(unittest.TestCase):
	def test_conflicting_holder_is_released_once(self) -> None:
		st = _held(dict(_pull(4), mergeable=False, mergeable_state="dirty"), [_pull(5)])
		h, out = _tick(st)
		self.assertRegex(out, r"MERGE_LEASE pr=4 base=main mode=tick outcome=released reason=released_conflict held_s=(\d+|-) merged=false\n")
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted", out)
		self.assertIn(CONFLICT_LABEL, h.state["labels"]["4"])
		rec = h.record()
		self.assertIn("pr=5", rec)
		self.assertIn(f"4:{_sha(4)}:released_conflict", _skips(rec), "a hard entry until the head changes")
		# The label makes it once per PR: the same holder carrying it keeps its slot.
		st = _held(dict(_pull(4), mergeable=False), [_pull(5)], holder_labels=[CONFLICT_LABEL])
		h, out = _tick(st)
		self.assertIn("MERGE_LEASE pr=4 base=main mode=tick outcome=held reason=holder_open\n", out)

	def test_labelled_conflicting_holder_is_kept(self) -> None:
		st = _held(dict(_pull(4), mergeable=False), [_pull(5)], holder_labels=[CONFLICT_LABEL.upper()])
		h, out = _tick(st)
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)
		self.assertNotIn("gh api repos/owner/repo/pulls/4", h.calls)

	def test_conflicting_holder_kept_without_ready_waiter(self) -> None:
		st = _held(dict(_pull(4), mergeable=False), [dict(_pull(5), mergeable=False)])
		h, out = _tick(st)
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)
		self.assertNotIn(CONFLICT_LABEL, h.state["labels"]["4"])

	def test_no_waiter_makes_no_holder_read(self) -> None:
		st = _held(dict(_pull(4), mergeable=False), [])
		h, out = _tick(st)
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertNotIn("gh api repos/owner/repo/pulls/4", h.calls)

	def test_label_failure_keeps_the_holder(self) -> None:
		st = _held(dict(_pull(4), mergeable=False), [_pull(5)])
		st["fail"] = ["issues/4/labels"]
		h, out = _tick(st)
		self.assertIn("outcome=label_failed reason=conflict_released_label", out)
		self.assertIn("outcome=held reason=holder_open", out)
		self.assertEqual(h.state["refs"][REF], "c" * 40)

	def test_release_write_failure_removes_the_label(self) -> None:
		# The label is the one-time marker (D6): a holder whose release write
		# failed was not released, so it must not keep it.
		for env in ({}, {"MERGE_LEASE_MAX_HOLDERS": "2"}):
			st = _held(dict(_pull(4), mergeable=False), [_pull(5)])
			st["fail"] = ["git/refs/"]
			h, out = _tick(st, env)
			self.assertIn("pr=4 base=main mode=tick outcome=write_failed reason=released_conflict", out, env)
			self.assertNotIn(CONFLICT_LABEL, h.state["labels"]["4"], env)
			self.assertEqual(h.state["refs"][REF], "c" * 40, env)

	def test_waiter_scan_and_grant_read_each_waiter_once(self) -> None:
		for env in ({}, {"MERGE_LEASE_MAX_HOLDERS": "2"}):
			st = _held(dict(_pull(4), mergeable=False), [dict(_pull(5), mergeable=False), _pull(6)])
			h, out = _tick(st, env)
			self.assertIn("pr=4 base=main mode=tick outcome=released reason=released_conflict", out, env)
			self.assertIn("pr=6 base=main mode=tick outcome=granted", out, env)
			for n in (5, 6):
				self.assertEqual(h.calls.count(f"gh api repos/owner/repo/pulls/{n}"), 1, (env, n, h.calls))

	def test_multi_holder_conflicting_holder_is_released(self) -> None:
		st = _held(dict(_pull(4), mergeable=False), [_pull(5)])
		h, out = _tick(st, {"MERGE_LEASE_MAX_HOLDERS": "2"})
		self.assertIn("pr=4 base=main mode=tick outcome=released reason=released_conflict", out)
		self.assertIn(CONFLICT_LABEL, h.state["labels"]["4"])
		self.assertIn(f"4:{_sha(4)}:released_conflict", _skips(h.record()))


class SwitchesOff(unittest.TestCase):
	def _scenarios(self) -> list[tuple[dict, dict[str, str]]]:
		pending = _free([_pull(5), _pull(6)], [5, 6])
		_gates_ok(pending, 5, 6)
		_ci_final(pending, 5, "pending")
		_ci_final(pending, 6, "success")
		idle = _free([dict(_pull(5), mergeable=False)], [5], updated=OLD)
		_gates_ok(idle, 5)
		conflict = _held(dict(_pull(4), mergeable=False), [_pull(5)])
		return [(pending, FCI), (idle, FCI), (conflict, {}), (conflict, {"MERGE_LEASE_MAX_HOLDERS": "2"})]

	def test_both_switches_false_match_today(self) -> None:
		for st, env in self._scenarios():
			off, out_off = _tick(st, {**env, **OFF})
			today, out_today = _tick(st, env, prelude=f"unset -f {NEW_FUNCS}")
			self.assertEqual(_lease_lines(out_off), _lease_lines(out_today), env)
			# Multi-holder rows carry epoch times inline (holder=<pr> <head> <exp> <acq> <upd>).
			self.assertEqual(re.sub(r"\b\d{10}\b", "N", _stable_record(off.record())),
				re.sub(r"\b\d{10}\b", "N", _stable_record(today.record())), env)
			self.assertEqual(_stable_calls(off.calls), _stable_calls(today.calls), env)
			for token in ("final_ci_pending", "not_mergeable", "ready_fallback", "released_conflict"):
				self.assertNotIn(token, out_off)

	def test_only_false_turns_a_switch_off(self) -> None:
		h = _Harness({})
		body = ('for v in true TRUE 1 off "" " False "; do MERGE_LEASE_READY_GRANT_ENABLED="$v"; '
			'_merge_lease_ready_grant_enabled && echo "on:$v" || echo "off:$v"; done')
		out = h.lib(body).stdout
		self.assertEqual(out.split("\n")[:6], ["on:true", "on:TRUE", "on:1", "on:off", "on:", "off: False "])

	def test_bad_label_value_uses_the_default(self) -> None:
		h = _Harness({})
		for value in ("", "a,b", "x" * 51, "bad\nlabel"):
			out = h.lib('_merge_lease_conflict_released_label', {"MERGE_LEASE_CONFLICT_RELEASED_LABEL": value}).stdout
			self.assertEqual(out, CONFLICT_LABEL + "\n", repr(value))
		out = h.lib('_merge_lease_conflict_released_label', {"MERGE_LEASE_CONFLICT_RELEASED_LABEL": "ai:custom"}).stdout
		self.assertEqual(out, "ai:custom\n")


NEW_LINES = (
	"MERGE_LEASE_READY_GRANT_ENABLED: ${{ vars.MERGE_LEASE_READY_GRANT_ENABLED || 'true' }}",
	"MERGE_LEASE_CONFLICT_RELEASE_ENABLED: ${{ vars.MERGE_LEASE_CONFLICT_RELEASE_ENABLED || 'true' }}",
	"MERGE_LEASE_CONFLICT_RELEASED_LABEL: ${{ vars.MERGE_LEASE_CONFLICT_RELEASED_LABEL || 'ai:merge-lease-conflict-released' }}",
)


def _step_block(lines: list[str], idx: int) -> list[str]:
	start = idx
	while start > 0 and not re.match(r"^\s+- name:", lines[start]):
		start -= 1
	end = idx + 1
	while end < len(lines) and not re.match(r"^\s+- name:", lines[end]):
		end += 1
	return lines[start:end]


class Wiring(unittest.TestCase):
	def test_switch_lines_follow_every_idle_minutes_line(self) -> None:
		for path, count in ((POLL, 4), (CLOSE, 1), (REVIEW, 3)):
			lines = path.read_text().splitlines()
			idle = [i for i, ln in enumerate(lines) if re.match(r"^\s+MERGE_LEASE_IDLE_MINUTES: ", ln)]
			self.assertEqual(len(idle), count, path.name)
			for i in idle:
				indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
				self.assertEqual(lines[i + 1:i + 4], [indent + x for x in NEW_LINES], f"{path.name}:{i + 1}")

	def test_poll_and_close_ticks_set_the_final_ci_workflow(self) -> None:
		for path in (POLL, CLOSE):
			lines = path.read_text().splitlines()
			for i, ln in enumerate(lines):
				if re.match(r"^\s+MERGE_LEASE_IDLE_MINUTES: ", ln):
					block = _step_block(lines, i)
					self.assertTrue(any(re.match(r"^\s+REVIEW_FINAL_CI_WORKFLOW: ", b) for b in block), f"{path.name}:{i + 1}")

	def test_review_has_no_step_level_final_ci_workflow(self) -> None:
		lines = [ln for ln in REVIEW.read_text().splitlines() if re.match(r"^\s+REVIEW_FINAL_CI_WORKFLOW: ", ln)]
		self.assertEqual(lines, ["  REVIEW_FINAL_CI_WORKFLOW: ${{ inputs.final_ci_workflow || '' }}"])

	def test_script_defaults_and_docs(self) -> None:
		lib = LIB.read_text()
		for default in ("${MERGE_LEASE_READY_GRANT_ENABLED:-true}", "${MERGE_LEASE_CONFLICT_RELEASE_ENABLED:-true}",
				"${MERGE_LEASE_CONFLICT_RELEASED_LABEL:-ai:merge-lease-conflict-released}"):
			self.assertIn(default, lib)
		readme = (REPO_ROOT / "README.md").read_text()
		for name in ("MERGE_LEASE_READY_GRANT_ENABLED", "MERGE_LEASE_CONFLICT_RELEASE_ENABLED", "MERGE_LEASE_CONFLICT_RELEASED_LABEL"):
			self.assertIn(f"| `{name}` | No |", readme)


if __name__ == "__main__":
	unittest.main()

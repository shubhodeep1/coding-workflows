#!/usr/bin/env python3
"""Regression tests for the sweep's stale-queued active-run guard.

The sweep skips dispatching a review for any PR that already has a
`queued` or `in_progress` run on its head ref. That guard originally had
no time cutoff, which deadlocked the recovery path it exists to protect:
GitHub can wedge a run in `queued` with zero jobs and then refuse both
`cancel` (409 "Cannot cancel a workflow run that has not been queued
yet") and `rerun` (403 "This workflow is already running"), so nothing
can clear it. PR #3841 sat unreviewed for 11+ hours behind run
32984498460 while every 30-minute tick logged:

    AUTOFIX_SWEEP_SKIP pr=#3841 reason=active_run
      workflow=internal-review.yml count=1

These tests execute the jq program **as extracted from the shipped
workflow file**, so a future edit to the reduce cannot silently drop the
cutoff while the tests keep passing against a stale copy.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"

# The jq program is embedded in the workflow as a single-quoted argument to
# `jq -c -s --argjson cutoff "${stale_cutoff_epoch}" --arg default_branch
# "${sweep_default_branch}"`. Grab it verbatim.
JQ_BLOCK = re.compile(
	r"jq -c -s --argjson cutoff \"\$\{stale_cutoff_epoch\}\" --arg default_branch \"\$\{sweep_default_branch\}\" '(?P<prog>.*?)'\s*2>/dev/null",
	re.DOTALL,
)
INTERNAL_REVIEW_PATH = ".github/workflows/internal-review.yml"


def extract_jq_program() -> str:
	text = SWEEP_WF.read_text(encoding="utf-8")
	match = JQ_BLOCK.search(text)
	assert match is not None, "could not locate the sweep's jq reduce in the workflow"
	# Strip the YAML block-scalar indentation the workflow carries.
	lines = [line[10:] if line.startswith(" " * 10) else line for line in match.group("prog").splitlines()]
	return "\n".join(lines)


def iso(delta_minutes: int) -> str:
	stamp = datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)
	return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def cutoff_epoch(minutes_ago: int) -> int:
	return int((datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp())


def run_sweep_reduce(runs: list[dict], cutoff: int, default_branch: str = "main") -> dict:
	payload = json.dumps({"workflow_runs": runs})
	result = subprocess.run(
		["jq", "-c", "-s", "--argjson", "cutoff", str(cutoff), "--arg", "default_branch", default_branch, extract_jq_program()],
		input=payload,
		capture_output=True,
		text=True,
		check=True,
	)
	return json.loads(result.stdout)


def named_dispatch_run(run_id: int, pr: str, **extra: object) -> dict:
	"""A default-branch internal-review.yml dispatch run named for PR `pr`."""
	run = {
		"id": run_id,
		"head_branch": "main",
		"path": INTERNAL_REVIEW_PATH,
		"event": "workflow_dispatch",
		"display_title": f"Internal: AI Review & Autofix [pr:{pr}]",
		"status": "in_progress",
		"created_at": iso(-5),
	}
	run.update(extra)
	return run


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class SweepStaleQueuedGuardTest(unittest.TestCase):
	def run_reduce(self, runs: list[dict], cutoff: int) -> dict:
		return run_sweep_reduce(runs, cutoff)

	def test_wedged_queued_run_stops_suppressing_dispatch(self) -> None:
		"""The PR #3841 case: queued 11 hours, zero jobs, uncancellable."""
		created_at = iso(-660)
		out = self.run_reduce(
			[{"id": 32984498460, "head_branch": "claude/x", "status": "queued", "created_at": created_at}],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {})
		stale_head_ref, stale_run_id, stale_created_epoch = out["stale"][0].split("\t")
		self.assertEqual(stale_head_ref, "claude/x")
		self.assertEqual(stale_run_id, "32984498460")
		self.assertEqual(int(stale_created_epoch), int(datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()))

	def test_recently_queued_run_still_suppresses(self) -> None:
		"""A genuine concurrency wait must not be cut short."""
		out = self.run_reduce(
			[{"id": 1, "head_branch": "claude/x", "status": "queued", "created_at": iso(-15)}],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {"claude/x": 1})
		self.assertEqual(out["stale"], [])

	def test_long_running_in_progress_is_never_discounted(self) -> None:
		"""codex-agent legitimately runs well over an hour."""
		out = self.run_reduce(
			[{"id": 2, "head_branch": "claude/x", "status": "in_progress", "created_at": iso(-1200)}],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {"claude/x": 1})
		self.assertEqual(out["stale"], [])

	def test_unparseable_or_missing_created_at_fails_safe(self) -> None:
		"""Without a usable timestamp, keep suppressing rather than risk a duplicate."""
		out = self.run_reduce(
			[
				{"id": 3, "head_branch": "a", "status": "queued"},
				{"id": 4, "head_branch": "b", "status": "queued", "created_at": "not-a-date"},
				{"id": 5, "head_branch": "c", "status": "queued", "created_at": ""},
			],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {"a": 1, "b": 1, "c": 1})
		self.assertEqual(out["stale"], [])

	def test_zero_cutoff_restores_previous_always_suppress_behaviour(self) -> None:
		out = self.run_reduce(
			[{"id": 6, "head_branch": "claude/x", "status": "queued", "created_at": iso(-660)}],
			0,
		)
		self.assertEqual(out["active"], {"claude/x": 1})
		self.assertEqual(out["stale"], [])

	def test_duplicate_run_ids_are_counted_once(self) -> None:
		run = {"id": 7, "head_branch": "claude/x", "status": "in_progress", "created_at": iso(-5)}
		out = self.run_reduce([run, dict(run)], cutoff_epoch(120))
		self.assertEqual(out["active"], {"claude/x": 1})

	def test_transitioned_run_prefers_in_progress_snapshot(self) -> None:
		out = self.run_reduce(
			[
				{"id": 8, "head_branch": "claude/x", "status": "queued", "created_at": iso(-180)},
				{"id": 8, "head_branch": "claude/x", "status": "in_progress", "created_at": iso(-180)},
			],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {"claude/x": 1})
		self.assertEqual(out["stale"], [])

	def test_runs_without_a_head_branch_are_ignored(self) -> None:
		out = self.run_reduce(
			[{"id": 9, "status": "queued", "created_at": iso(-5)}, {"id": 10, "head_branch": "", "status": "queued"}],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {})


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class SweepPrKeyedDispatchTest(unittest.TestCase):
	"""Issue #4618: the sweep dispatches from the default branch, so a
	dispatched run is keyed by the PR its run name carries, not its head."""

	def run_reduce(self, runs: list[dict], cutoff: int, default_branch: str = "main") -> dict:
		return run_sweep_reduce(runs, cutoff, default_branch)

	def dispatch_run(self, run_id: int, pr: str, **extra: object) -> dict:
		return named_dispatch_run(run_id, pr, **extra)

	def test_named_dispatch_run_is_keyed_by_pr(self) -> None:
		out = self.run_reduce([self.dispatch_run(1, "4618")], cutoff_epoch(120))
		self.assertEqual(out["active"], {"pr:4618": 1})

	def test_named_dispatch_runs_do_not_mark_the_default_branch_active(self) -> None:
		"""A main-headed PR (promote/forward-merge) is not suppressed by sweep runs."""
		out = self.run_reduce(
			[self.dispatch_run(1, "10"), self.dispatch_run(2, "11")],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {"pr:10": 1, "pr:11": 1})
		self.assertNotIn("main", out["active"])

	def test_marker_on_a_pull_request_run_stays_branch_keyed(self) -> None:
		"""A PR titled like the marker cannot claim another PR's key."""
		out = self.run_reduce(
			[self.dispatch_run(3, "7", event="pull_request", head_branch="feature/x")],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {"feature/x": 1})

	def test_unnamed_or_malformed_dispatch_run_stays_branch_keyed(self) -> None:
		out = self.run_reduce(
			[
				{"id": 4, "head_branch": "claude/y", "event": "workflow_dispatch", "display_title": "Internal: AI Review & Autofix", "status": "queued", "created_at": iso(-5)},
				self.dispatch_run(5, "0", head_branch="claude/z"),
				self.dispatch_run(6, "12x", head_branch="claude/w"),
				{"id": 7, "head_branch": "claude/v", "event": "workflow_dispatch", "status": "pending"},
			],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {"claude/y": 1, "claude/z": 1, "claude/w": 1, "claude/v": 1})

	def test_wedged_named_dispatch_run_is_logged_under_its_pr(self) -> None:
		out = self.run_reduce(
			[self.dispatch_run(8, "4618", status="queued", created_at=iso(-660))],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {})
		stale_key, stale_run_id, _ = out["stale"][0].split("\t")
		self.assertEqual(stale_key, "pr:4618")
		self.assertEqual(stale_run_id, "8")

	def test_branch_and_pr_keys_count_side_by_side(self) -> None:
		out = self.run_reduce(
			[
				self.dispatch_run(9, "5"),
				self.dispatch_run(10, "5", status="pending"),
				{"id": 11, "head_branch": "claude/x", "event": "pull_request", "display_title": "Some PR", "status": "in_progress", "created_at": iso(-5)},
			],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {"pr:5": 2, "claude/x": 1})

	def test_named_dispatch_run_without_a_head_branch_gets_no_pr_key(self) -> None:
		"""Issue #5522 (reverses the #4928 allowance): GitHub can report
		head_branch=null on a workflow_dispatch run, but a run with no head
		branch cannot prove it came from the default branch, so its name
		earns no PR key and the run is dropped. At worst the next tick
		dispatches that PR again into the same concurrency group."""
		null_head = self.dispatch_run(12, "4928", head_branch=None)
		missing_head = self.dispatch_run(13, "4929", status="queued")
		del missing_head["head_branch"]
		empty_head = self.dispatch_run(14, "4930", status="pending", head_branch="")
		out = self.run_reduce([null_head, missing_head, empty_head], cutoff_epoch(120))
		self.assertEqual(out["active"], {})
		self.assertEqual(out["stale"], [])

	def test_wedged_named_dispatch_run_without_a_head_branch_is_not_logged_under_a_pr(self) -> None:
		out = self.run_reduce(
			[self.dispatch_run(15, "4928", head_branch=None, status="queued", created_at=iso(-660))],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {})
		self.assertEqual(out["stale"], [])

	def test_run_without_a_head_branch_or_pr_key_is_still_dropped(self) -> None:
		out = self.run_reduce(
			[
				{"id": 16, "head_branch": None, "event": "workflow_dispatch", "display_title": "Internal: AI Review & Autofix", "status": "queued", "created_at": iso(-5)},
				self.dispatch_run(17, "0", head_branch=None),
				self.dispatch_run(18, "12x", head_branch=None),
				self.dispatch_run(19, "7", head_branch=None, event="pull_request"),
				{"id": 20, "head_branch": None, "event": "workflow_dispatch", "status": "pending"},
			],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {})
		self.assertEqual(out["stale"], [])


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class SweepPrKeyProvenanceTest(unittest.TestCase):
	"""Issue #5522: a branch writer can dispatch an altered wrapper from a
	branch and name its run for another PR. A pr:<N> key is given only to a
	workflow_dispatch run of internal-review.yml on the default branch."""

	def dispatch_run(self, run_id: int, pr: str, **extra: object) -> dict:
		return named_dispatch_run(run_id, pr, **extra)

	def test_forged_run_from_another_branch_stays_keyed_by_that_branch(self) -> None:
		out = run_sweep_reduce([self.dispatch_run(1, "42", head_branch="attacker/branch")], cutoff_epoch(120))
		self.assertEqual(out["active"], {"attacker/branch": 1})
		self.assertNotIn("pr:42", out["active"])

	def test_forged_wedged_run_is_logged_under_its_branch(self) -> None:
		out = run_sweep_reduce(
			[self.dispatch_run(2, "42", head_branch="attacker/branch", status="queued", created_at=iso(-660))],
			cutoff_epoch(120),
		)
		self.assertEqual(out["stale"][0].split("\t")[0], "attacker/branch")

	def test_named_run_from_another_workflow_file_gets_no_pr_key(self) -> None:
		out = run_sweep_reduce(
			[
				self.dispatch_run(3, "42", path=".github/workflows/review_autofix.yml"),
				self.dispatch_run(4, "43", path=".github/workflows/evil.yml"),
				self.dispatch_run(5, "44", path=".github/workflows/internal-review.yml.bak"),
			],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {"main": 3})

	def test_named_run_without_a_path_gets_no_pr_key(self) -> None:
		run = self.dispatch_run(6, "42")
		del run["path"]
		out = run_sweep_reduce([run], cutoff_epoch(120))
		self.assertEqual(out["active"], {"main": 1})

	def test_ref_suffixed_wrapper_path_still_counts(self) -> None:
		out = run_sweep_reduce(
			[self.dispatch_run(7, "42", path=".github/workflows/internal-review.yml@refs/heads/main")],
			cutoff_epoch(120),
		)
		self.assertEqual(out["active"], {"pr:42": 1})

	def test_default_branch_is_not_assumed_to_be_main(self) -> None:
		out = run_sweep_reduce(
			[self.dispatch_run(8, "42"), self.dispatch_run(9, "43", head_branch="trunk")],
			cutoff_epoch(120),
			default_branch="trunk",
		)
		self.assertEqual(out["active"], {"main": 1, "pr:43": 1})

	def test_unresolved_default_branch_disables_pr_keys(self) -> None:
		out = run_sweep_reduce([self.dispatch_run(10, "42")], cutoff_epoch(120), default_branch="")
		self.assertEqual(out["active"], {"main": 1})

	def test_consumer_title_is_not_an_internal_pr_key(self) -> None:
		out = run_sweep_reduce([self.dispatch_run(11, "42", display_title="AI Review [pr:42]")], cutoff_epoch(120))
		self.assertEqual(out["active"], {"main": 1})


class SweepWorkflowContractTest(unittest.TestCase):
	def setUp(self) -> None:
		self.text = SWEEP_WF.read_text(encoding="utf-8")

	def test_cutoff_is_configurable_with_a_default(self) -> None:
		self.assertIn("SWEEP_STALE_QUEUED_MINUTES: ${{ vars.SWEEP_STALE_QUEUED_MINUTES || '120' }}", self.text)

	def test_stale_runs_are_logged_not_silently_discounted(self) -> None:
		self.assertIn("AUTOFIX_SWEEP_STALE_QUEUED", self.text)
		self.assertIn("run_id=${stale_run_id}", self.text)
		self.assertIn("queued_age_minutes=${queued_age_minutes}", self.text)
		self.assertIn("queued_age_threshold_minutes=${SWEEP_STALE_QUEUED_MINUTES:-0}", self.text)
		self.assertNotIn("queued_minutes_gt=", self.text)

	def test_header_records_why_the_cutoff_exists(self) -> None:
		self.assertIn("wedge a run in `queued` with zero jobs", self.text)

	def test_default_branch_comes_from_the_pr_listing(self) -> None:
		"""Issue #5522: no extra API call; the PR objects carry it."""
		self.assertIn("default_branch: (.base.repo.default_branch // \"\")", self.text)
		self.assertIn("sweep_default_branch=", self.text)

	def test_unresolved_default_branch_is_logged(self) -> None:
		self.assertIn(
			"::warning::AUTOFIX_SWEEP_PROVENANCE repo=${REPOSITORY} outcome=default_branch_unresolved pr_named_matching=disabled",
			self.text,
		)


if __name__ == "__main__":
	unittest.main()

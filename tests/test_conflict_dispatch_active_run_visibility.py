#!/usr/bin/env python3
"""Regression tests for the PR #3895 duplicate-conflict-dispatch incident.

On 2026-08-29, forward-merge PR #3895 (stable→main, conflicting
`.github/workflows/plan.yml`) triggered 10 duplicate internal-review
dispatches and 6+ duplicate "merge conflicts. Review workflow dispatched
for resolution." Telegram warnings over ~95 minutes, while the one real
resolver run (33273396616) ran to success. Two blind spots caused it:

1. Status blindness: a duplicate dispatch held back by review_autofix's
   concurrency group (cancel-in-progress=false) reports status
   ``pending`` — not ``queued`` — so the poller guard
   (``_has_active_autofix_run``) and the sweep snapshot, both filtering
   on ``in_progress``/``queued`` only, never saw the previous duplicate
   and re-dispatched every cycle.
2. Ref blindness: forward-merge-stable-to-main.yml and
   review_autofix_sweep.yml dispatched internal-review.yml without
   ``--ref``, so their runs were keyed to the default branch and
   invisible to both guards' head-branch-keyed lookups.

These are text contracts on the shipped files, so a refactor cannot
silently reintroduce either blind spot while the tests keep passing.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLL_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
SWEEP_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
FORWARD_MERGE_WF = REPO_ROOT / ".github" / "workflows" / "forward-merge-stable-to-main.yml"


class PollerActiveRunGuardStatusContract(unittest.TestCase):
	def setUp(self) -> None:
		self.text = POLL_SCRIPT.read_text(encoding="utf-8")

	def test_guard_counts_pending_runs_as_active(self) -> None:
		self.assertIn(
			'select(.status == "in_progress" or .status == "queued" or .status == "pending")',
			self.text,
		)

	def test_guard_header_records_why_pending_matters(self) -> None:
		self.assertIn("reports\n# status=pending, not queued", self.text)


class SweepSnapshotStatusContract(unittest.TestCase):
	def setUp(self) -> None:
		self.text = SWEEP_WF.read_text(encoding="utf-8")

	def test_snapshot_fetches_pending_runs(self) -> None:
		self.assertIn("for status in queued in_progress pending; do", self.text)

	def test_stale_cutoff_still_targets_queued_only(self) -> None:
		# A pending run is bounded by its running peer's job timeout and
		# must never be discounted as wedged.
		self.assertIn('(.status // "") == "queued"', self.text)
		self.assertNotIn('(.status // "") == "pending"', self.text)


class SweepDispatchRefContract(unittest.TestCase):
	def setUp(self) -> None:
		self.text = SWEEP_WF.read_text(encoding="utf-8")

	def test_dispatch_uses_head_ref_for_same_repo_prs(self) -> None:
		self.assertIn('if [ "${head_repo}" = "${REPOSITORY}" ] && [ -n "${head_ref}" ]; then', self.text)
		self.assertIn('dispatch_args+=(--ref "${head_ref}")', self.text)

	def test_pr_snapshot_captures_head_repo_for_fork_detection(self) -> None:
		self.assertIn('head_repo: (.head.repo.full_name // "")', self.text)


class ForwardMergeDispatchRefContract(unittest.TestCase):
	def setUp(self) -> None:
		self.text = FORWARD_MERGE_WF.read_text(encoding="utf-8")

	def test_fallback_step_exports_branch_output(self) -> None:
		self.assertIn('echo "branch=${BRANCH}" >> "$GITHUB_OUTPUT"', self.text)

	def test_review_dispatch_targets_fallback_branch_ref(self) -> None:
		self.assertIn("HEAD_BRANCH: ${{ steps.fallback.outputs.branch }}", self.text)
		self.assertIn('dispatch_args+=(--ref "${HEAD_BRANCH}")', self.text)


if __name__ == "__main__":
	unittest.main()

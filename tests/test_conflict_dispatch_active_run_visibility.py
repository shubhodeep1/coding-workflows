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

Issue #4618 (security) then moved the sweep off the head ref: a same-repo
head-ref dispatch ran the unmerged branch's copy of internal-review.yml with
the sweep's secrets. The sweep now always dispatches from the default
branch, and closes the ref blind spot a different way: internal-review.yml
names every dispatched run ``Internal: AI Review & Autofix [pr:<N>]``, and
both guards match that name by PR. Issue #4701 moved the remaining head-ref
dispatches (the poller's ``_dispatch_review_for_conflicts``, the merge train,
and the forward-merge fallback) to the default branch too, and the poller's
lookups match the PR-named runs through ``_pr_named_review_dispatch_runs``.

These are text contracts on the shipped files, so a refactor cannot
silently reintroduce either blind spot, or a head-ref dispatch, while the
tests keep passing.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLL_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
SWEEP_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
FORWARD_MERGE_WF = REPO_ROOT / ".github" / "workflows" / "forward-merge-stable-to-main.yml"
INTERNAL_REVIEW_WF = REPO_ROOT / ".github" / "workflows" / "internal-review.yml"

# The run name internal-review.yml gives a dispatched run, as the guards match it.
DISPATCH_RUN_NAME = "Internal: AI Review & Autofix [pr:"


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
	"""Issue #4618: the sweep never dispatches an unmerged head ref."""

	def setUp(self) -> None:
		self.text = SWEEP_WF.read_text(encoding="utf-8")

	def _dispatch_block(self) -> str:
		start = self.text.index("dispatch_args=(--repo")
		end = self.text.index('gh workflow run internal-review.yml "${dispatch_args[@]}"', start)
		return self.text[start:end]

	def test_dispatch_never_passes_a_ref(self) -> None:
		self.assertNotIn("--ref", self._dispatch_block())
		self.assertNotIn('dispatch_args+=(--ref "${head_ref}")', self.text)
		# The sweep job is the workflow's last job since the claude-pr-catch-all
		# job was retired (replace-claude-sessions plan, Phase 2).
		sweep_job = self.text[self.text.index("  sweep:\n"):]
		self.assertNotIn('--ref "', sweep_job)

	def test_dispatch_carries_only_pr_number_and_edit_flag(self) -> None:
		block = self._dispatch_block()
		self.assertIn('-f "pr_number=${pr_number}"', block)
		self.assertIn('-f "allow_workflow_edits=${ALLOW_WORKFLOW_EDITS}"', block)
		self.assertEqual(block.count("-f "), 2)

	def test_pr_number_is_validated_before_dispatch(self) -> None:
		guard = 'if ! [[ "${pr_number}" =~ ^[1-9][0-9]*$ ]]; then'
		self.assertIn(guard, self.text)
		self.assertIn("reason=invalid_pr_number", self.text)
		self.assertLess(self.text.index(guard), self.text.index("dispatch_args=(--repo"))

	def test_preflight_checks_pr_key_and_head_ref(self) -> None:
		self.assertIn('for dedupe_ref in "${head_ref}" "pr:${pr_number}"; do', self.text)
		self.assertIn('count="${active_review_runs["${wf}:${dedupe_ref}"]:-0}"', self.text)

	def test_snapshot_keys_dispatch_runs_by_run_name(self) -> None:
		self.assertIn('(.event // "") == "workflow_dispatch"', self.text)
		self.assertIn("^Internal: AI Review & Autofix \\\\[pr:([1-9][0-9]*)\\\\]$", self.text)
		self.assertIn("| dedupe_key) as $head_ref", self.text)


class InternalReviewDispatchRunNameContract(unittest.TestCase):
	def setUp(self) -> None:
		self.text = INTERNAL_REVIEW_WF.read_text(encoding="utf-8")

	def test_dispatched_runs_are_named_for_their_pr(self) -> None:
		self.assertIn(
			"run-name: \"${{ github.event_name == 'workflow_dispatch' && "
			"format('Internal: AI Review & Autofix [pr:{0}]', inputs.pr_number) || '' }}\"",
			self.text,
		)


class PollerPrNamedRunContract(unittest.TestCase):
	def setUp(self) -> None:
		text = POLL_SCRIPT.read_text(encoding="utf-8")
		start = text.index("_has_active_autofix_run()\n{")
		self.body = text[start:text.index("\n}\n", start)]
		helper_start = text.index("_pr_named_review_dispatch_runs()\n{")
		self.helper = text[helper_start:text.index("\n}\n", helper_start)]

	def test_guard_looks_up_pr_named_dispatch_runs(self) -> None:
		# Issue #4701: the lookup lives in the shared helper, which matches
		# both wrapper names. Issue #4927: it lists each wrapper's own
		# workflow_dispatch runs page by page instead of one global page of
		# the newest 100 dispatches, and an incomplete listing counts as an
		# active run.
		self.assertIn('_pr_named_review_dispatch_runs "${pr_number}")" || pr_named_rc=$?', self.body)
		self.assertIn('if [ "${pr_named_rc}" -ne 0 ]; then', self.body)
		self.assertIn("for _pnr_wrapper in internal-review.yml ai-review.yml; do", self.helper)
		self.assertIn(
			'actions/workflows/${_pnr_wrapper}/runs?event=workflow_dispatch&created=>=${_pnr_cutoff}&per_page=100&page=${_pnr_page}',
			self.helper,
		)
		self.assertNotIn("gh run list", self.helper)
		self.assertNotIn("--limit 100", self.helper)
		self.assertIn('("Internal: AI Review & Autofix [pr:" + $pr + "]")', self.helper)
		self.assertIn('("AI Review [pr:" + $pr + "]")', self.helper)

	def test_pr_named_lookup_follows_the_head_branch_lookups(self) -> None:
		self.assertLess(self.body.index('--branch "${head_ref}"'), self.body.index("_pr_named_review_dispatch_runs"))

	def test_pr_named_lookup_validates_the_pr_number(self) -> None:
		self.assertIn('if [[ "${pr_number}" =~ ^[1-9][0-9]*$ ]]; then', self.body)
		self.assertIn('if ! [[ "${pr_number}" =~ ^[1-9][0-9]*$ ]]; then', self.helper)

	def test_run_name_prefix_matches_internal_review(self) -> None:
		self.assertIn(DISPATCH_RUN_NAME, INTERNAL_REVIEW_WF.read_text(encoding="utf-8"))
		self.assertIn(DISPATCH_RUN_NAME, self.helper)


class ForwardMergeDispatchRefContract(unittest.TestCase):
	def setUp(self) -> None:
		self.text = FORWARD_MERGE_WF.read_text(encoding="utf-8")

	def test_fallback_step_exports_branch_output(self) -> None:
		self.assertIn('echo "branch=${BRANCH}" >> "$GITHUB_OUTPUT"', self.text)

	def test_review_dispatch_runs_from_the_default_branch(self) -> None:
		# Issue #4701: the fallback dispatch never passes --ref.
		step = self.text[self.text.index("- name: Dispatch AI review for the fallback PR"):self.text.index("- name: Summary")]
		self.assertIn("HEAD_BRANCH: ${{ steps.fallback.outputs.branch }}", step)
		self.assertNotIn("--ref", step)
		self.assertNotIn('dispatch_args+=(--ref "${HEAD_BRANCH}")', self.text)
		self.assertIn('gh workflow run internal-review.yml "${dispatch_args[@]}"', step)

	def test_review_dispatch_validates_the_pr_number(self) -> None:
		step = self.text[self.text.index("- name: Dispatch AI review for the fallback PR"):self.text.index("- name: Summary")]
		guard = 'if ! [[ "${PR_NUMBER}" =~ ^[1-9][0-9]*$ ]]; then'
		self.assertIn(guard, step)
		self.assertLess(step.index(guard), step.index("gh workflow run internal-review.yml"))


if __name__ == "__main__":
	unittest.main()

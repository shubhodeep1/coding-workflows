#!/usr/bin/env python3
"""Contract tests for the zero-candidate fast-exit in review_autofix_sweep.

The sweep enumerates open non-draft PRs, logs `AUTOFIX_SWEEP_START`, then
preflights active review-family runs by querying the Actions API for
`internal-review.yml` and `review_autofix.yml`. When enumeration returns zero
candidates, that API fanout is wasted work: there is nothing to filter,
dispatch, or skip.

This contract keeps the optimisation surgical:

1. `AUTOFIX_SWEEP_START` still logs first for observability.
2. A `total == 0` guard exits before the active-run snapshot helper or any
   `/actions/workflows/{workflow}/runs` lookup.
3. Non-zero sweeps still retain the existing snapshot helper and the loop that
   snapshots both review workflows.
4. The zero-candidate path preserves the existing `AUTOFIX_SWEEP_END`
   summary vocabulary before exiting.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_SWEEP = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"


def _review_autofix_sweep_text() -> str:
	return REVIEW_AUTOFIX_SWEEP.read_text(encoding="utf-8")


def _sweep_step_block(text: str) -> str:
	marker = "- name: Enumerate open PRs and dispatch internal-review.yml"
	start = text.find(marker)
	assert start != -1, "Missing sweep step in review_autofix_sweep.yml"
	return text[start:]


def _zero_candidate_guard_block(block: str) -> str:
	guard_start = block.find('if [ "${total}" -eq 0 ]; then')
	assert guard_start != -1, "Missing zero-candidate fast-exit guard"
	guard_end = block.find("\n          fi", guard_start)
	assert guard_end != -1, "Could not bound zero-candidate fast-exit guard"
	guard_end += len("\n          fi")
	return block[guard_start:guard_end]


def test_zero_candidate_guard_precedes_active_run_snapshot() -> None:
	"""The `total == 0` branch must cut off the sweep before any active-run
	preflight work. Regressing this ordering would bring back the idle-repo
	Actions API fanout on every 30-minute tick."""
	block = _sweep_step_block(_review_autofix_sweep_text())
	start_log = block.find('echo "AUTOFIX_SWEEP_START')
	guard_start = block.find('if [ "${total}" -eq 0 ]; then')
	helper_start = block.find("snapshot_active_review_runs() {")
	workflow_runs_lookup = block.find('actions/workflows/${workflow}/runs')

	assert start_log != -1, "Missing AUTOFIX_SWEEP_START log line"
	assert guard_start != -1, "Missing zero-candidate fast-exit guard"
	assert helper_start != -1, "Missing active-run snapshot helper"
	assert workflow_runs_lookup != -1, "Missing active-run workflow-runs API lookup"
	assert start_log < guard_start < helper_start, (
		"Zero-candidate fast-exit must come after AUTOFIX_SWEEP_START but before "
		"snapshot_active_review_runs(), otherwise the idle sweep still builds or "
		"reaches the active-run preflight path."
	)
	assert guard_start < workflow_runs_lookup, (
		"Zero-candidate fast-exit must come before the `/actions/workflows/.../runs` "
		"lookup so empty sweeps return without any active-run GH API fanout."
	)


def test_zero_candidate_guard_preserves_summary_log_before_exit() -> None:
	"""The fast path should stay observability-compatible: emit the normal
	`AUTOFIX_SWEEP_END` counters, then exit 0. A bare early exit would create
	log drift for idle sweeps."""
	guard_block = _zero_candidate_guard_block(_sweep_step_block(_review_autofix_sweep_text()))
	assert 'echo "AUTOFIX_SWEEP_END dispatched=${dispatched} skipped_active=${skipped_active} skipped_filter=${skipped_filter} skipped_skip_ai=${skipped_skip_ai} failures=${failures} candidates=${total}"' in guard_block, (
		"Zero-candidate fast-exit must preserve the existing AUTOFIX_SWEEP_END "
		"summary vocabulary before returning."
	)
	assert "exit 0" in guard_block, "Zero-candidate fast-exit guard must return successfully"


def test_non_zero_path_still_snapshots_both_review_workflows() -> None:
	"""The optimisation must not disturb non-zero sweeps: they still need the
	existing active-run snapshot helper and the loop over both review-family
	workflows."""
	block = _sweep_step_block(_review_autofix_sweep_text())
	guard_end = block.find("\n          fi")
	assert guard_end != -1, "Could not locate end of zero-candidate fast-exit guard"
	non_zero_path = block[guard_end:]

	assert "snapshot_active_review_runs() {" in non_zero_path, (
		"Non-zero path lost the active-run snapshot helper. The optimisation must "
		"only bypass it for `total == 0`."
	)
	assert "for wf in internal-review.yml review_autofix.yml; do" in non_zero_path, (
		"Non-zero path must still snapshot both review-family workflows before the "
		"per-PR loop."
	)
	assert 'snapshot_active_review_runs "${wf}"' in non_zero_path, (
		"Non-zero path must still invoke the snapshot helper for each review-family "
		"workflow."
	)


if __name__ == "__main__":
	test_zero_candidate_guard_precedes_active_run_snapshot()
	test_zero_candidate_guard_preserves_summary_log_before_exit()
	test_non_zero_path_still_snapshots_both_review_workflows()
	print("All review_autofix_sweep zero-candidate fast-exit contract tests passed.")

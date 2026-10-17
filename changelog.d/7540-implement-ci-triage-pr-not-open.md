<!-- changelog: fixed -->
- **Implement no longer fails when a CI-triage pull request has already merged or closed.** A check-failure triage issue whose pull request merged or closed between plan approval and implementation made implement's "Resolve integration ref" step exit 1 on every retry. The resolver had refused routing with `reason=pr_not_open` (shubhodeep1/bitsafe.io#551, run 39002911584). Plan (#7477) and clarify already handled that refusal; implement now handles it the same way.
  - The refusal is logged as a `::notice::`. The step sets `SKIP_IMPLEMENT=true` and exits 0 without writing a ref, so the job never checks out the default branch.
  - Three new steps settle the issue. A support-ref step and a credential-free sparse checkout of the verified support `scripts/` run first. Then "Hand off CI-triage issue whose PR is no longer open" runs the trusted `plan_superseded_merged_pr.py classify`:
    - when the pull request merged with its check green on its final head, the issue gets the `ai:plan-superseded:v1` comment and `ai:closed`, and is closed as not planned;
    - any other outcome, and a disabled `PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED`, a missing helper or a failed classifier, moves the issue from `ai:awaiting-approval` to `ai:blocked` with an `<!-- ai:implement-pr-not-open:v1 pr=<n> run=<id> -->` comment, for the unblock judge.
  - The hand-off logs `AI_PHASE_GATE_V1 phase=implement gate=ci_triage_pr_not_open`.
  - Every other refusal reason (exit 3) still fails closed.
  - "Diagnose post-Codex failure and create fix-up issues" now skips with a warning when the diagnose helper was never staged, instead of exiting 127 after an early failure.
  - Tests: `tests/test_workflow_checkout_integration_ref_audit.py`, `tests/test_plan_superseded_merged_pr_workflow.py`.

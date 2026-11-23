<!-- changelog: changed -->
- **A "nothing to implement" planning block now checks itself and closes the issue when the work is really there.** Refs #9650 (plan `docs/plans/pipeline-human-wait-elimination-plan.md`, Phase 8a; issue #10015).

  - When planning stops with "work is already on the planning ref; nothing to implement", the plan run now runs the tests the plan's `ACCEPTANCE:` lines name (or, without any, the `tests/` paths the issue body names; at most 10) on the planning ref, in the same credential-free, network-isolated container the pre-push gate uses. When every listed test passes, it posts a comment with the `ai:implement-success-noop:v1` marker, swaps the labels to `ai:closed` and closes the issue as completed, with no Telegram page. When no tests are found, a test fails or the run times out (600 s), the usual blocked comment is posted with one extra `Verify:` line saying why.
  - Switch: `PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED` (default `true`). Set it to `false` to keep today's blocked comment.

What this means for consumer repos: after the next `@stable` sync, issues whose planner reports the work as already present close on their own when the listed tests pass on the planning ref. Set the repository variable to `false` to keep the blocked comment.

<!-- changelog: fixed -->
- **The stable release gate now watches the Implement run started by the plan's own auto-approval.** Phase 3a of `.github/workflows/test-and-mark-stable.yml` still posts its `/approved` comment. It then reads the smoke issue's comments and starts Phase 3b's run window at the earliest trusted `/approved`, which can be the plan's `/approved [auto-approved-by-plan]`.

Release run 40932430968 failed in Phase 3b with `Implement phase stalled — no activity for 30 minutes`. The plan had auto-approved the smoke issue at 19:01:15, and that comment started Implement run 40936238289, which took the per-issue concurrency group and did the work. Phase 3a's own `/approved` at 19:02:06 started a duplicate that waited `pending`. Phase 3b only counted runs created after Phase 3a's comment, so it saw the pending duplicate and never the working run. That run opened its PR at 19:46:14, nine minutes after the gate gave up.

How Phase 3a picks the window start:
- **Trusted approvals.** A comment counts when it starts with `/approved` and comes from a User who is OWNER, MEMBER or COLLABORATOR, or from a Bot and carries `[auto-approved-by-plan]`. This matches Implement's job condition. Its `created_at` must be a plain UTC timestamp.
- **Server timestamps.** The comparison uses the `created_at` GitHub returns for Phase 3a's own comment. When that timestamp is missing, it uses the runner clock, as before.
- **Failures.** If the comment read fails three times or returns unusable data, the window starts at Phase 3a's own comment, as before.
- **New output.** The step adds the output `approval_source` (`plan_auto`, `existing` or `e2e`) and logs `approval_source=<v> approved_at=<ts> posted_at=<ts>`.

The duplicate-run tracking (`IMPL_RUN_ID_WORKING`) and the pre-push gate step's 35-minute limit already reached `stable` with #8376. With this change, the run 40932430968 timeline passes Phase 3b. `PHASE_TIMEOUT` and `AUTO_IMPLEMENT_ON_CLEAR_PLAN` are unchanged, and so is the stall error line.

### For contributors

**Port to main:** `main` already has #8243's working-run tracking and #8259's gate-step limit. Only the Phase 3a `approve` hunk (and the matching Phase 3b comment) needs porting there.

Tests: `tests/test_test_and_mark_stable_plan_polling_guard.py` covers an earlier plan auto-approval, no earlier approval, untrusted and malformed approvals, a failed comment read, a malformed POST timestamp, and a replay of run 40932430968 that runs Phase 3a and then Phase 3b on its output.

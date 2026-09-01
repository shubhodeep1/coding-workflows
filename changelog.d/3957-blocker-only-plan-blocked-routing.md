<!-- changelog: fixed -->
- **Plans that report only self-check blockers now stop the pipeline as blocked instead of looping in clarification.** The AI Plan workflow no longer posts an unanswerable "clarification questions" comment when the planner emits `PLAN_SELF_CHECK: BLOCKER:` with `STATUS: NOT_CLEAR` and no Q-ID question block.

Previously, `.github/workflows/plan.yml` routed that output shape to the clarification path, where the orchestrator auto-answer parser failed with `No Q-ID blocks detected` and stall recovery kept re-answering into the same blocked plan, one Codex planning run per cycle. This surfaced on shubhodeep1/bitsafe.io issue 478 (itself a stall-recovery re-issue of issue 471), where the blocker was the `ai:destructive-blocked` label plus a missing `ALLOW_BULK_DELETE` repository variable, a state no `/answer` can clear. The parse step now routes blocker-only output to the existing blocked path: the issue gets `ai:blocked` (which stall recovery skips), loses `ai:clarification`, receives one "Planning blocked: human input required" comment carrying the first blocker line as the reason, and a CRITICAL Telegram alert pages a human once. Plans that pose Q-ID questions or carry a `NEEDS_CLARIFICATION` status alongside blockers still reopen clarification as before.

| The numbers that matter | Value |
| --- | --- |
| Workflow changed | `.github/workflows/plan.yml` (`Parse planning output` step) |
| Reference incident | shubhodeep1/bitsafe.io#478, run 33509691014 |
| Label applied on blocker-only plans | `ai:blocked` (was: stuck in `ai:clarification`) |
| Planning runs saved per stalled issue | 1 per stall-recovery cycle, indefinitely |

What this means for operators: a plan that is blocked on workflow state (destructive guards, missing repository variables) now pages you once via the CRITICAL Telegram alert and waits, instead of repeatedly warning "Auto-answer parser failed; waiting for human /answer". Clear the reported blocker, then reply `/answer` on the issue to resume planning.

### For contributors

The `plan_self_check_*` step outputs, including `plan_self_check_reopen_clarification`, are emitted unchanged; only the final `blocked` / `needs_clarification` routing moved. `tests/test_plan_clarify_blocked_output.py` mirrors the new routing and adds blocker-only and blocker-plus-questions regression tests.

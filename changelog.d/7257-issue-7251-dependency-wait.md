<!-- changelog: changed -->
- **Issue #7251 now waits for PR #6667 instead of re-running clarify and plan into the same block.** While #6667 has not merged into the default branch, the poller, clarify and plan leave #7251 alone and post nothing.

When #6667 merges, the poller checks the default branch's `scripts/ai_engine.sh` for both plans' Phase 0 symbols (`_AI_ENGINE_EXIT_REFUSED` and `ai_engine_fallback_class`). If both are there, it restarts #7251 with one `/reclarify`. If either is missing, it posts one comment naming the missing symbol, sends one Telegram WARNING, and does not restart #7251. If #6667 is closed without merging, it labels #7251 `ai:blocked` again so the unblock judge takes it over. Each of these happens once, deduplicated by a marker comment from an owner, member or collaborator.

| The numbers that matter | Value |
| --- | --- |
| Off switch | `ISSUE_7251_DEPENDENCY_WAIT_ENABLED=false` (unset, empty or `true` keeps it on; any other value turns it off with a warning) |
| Extra API calls | one GraphQL read per poll tick, and per clarify or plan run on #7251 |
| Log lines | `STALL_SKIP issue=7251 reason=dependency_wait`, `AI_PHASE_GATE_V1 ... gate=issue_7251_dependency_wait`, `UNBLOCK_SCAN item=7251 outcome=skip reason=issue_7251_dependency_wait` |

What this means for operators: nothing to do. The wait applies to #7251 in this repository only; consumer repositories and every other issue are unchanged.

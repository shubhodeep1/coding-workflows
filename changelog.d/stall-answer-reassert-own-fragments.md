<!-- changelog: fixed -->
- **Stall recovery keeps the clarify-respond worker's answer, and implement accepts the issue's own changelog fragment.** Two unattended-pipeline defects from consumer tele-funtoken-msg-scoring #4815 are fixed, plus a stale label on completed projects.

When a clarification stalls after the clarify-respond worker already answered it, stall recovery now re-posts the worker's latest answer instead of the questions' RECOMMENDED letters. On #4815 the worker chose `Q1: B` and stall recovery replaced it with `Q1: A`; REFRAME answers and their execution plans are now repeated verbatim too. The RECOMMENDED fallback is used only when no trusted pipeline `/answer` is newer than the latest questions. Separately, an issue with an exact `files_touched` list can now commit its own `changelog.d/<issue>-<slug>.md` and `agents.d/<issue>-<slug>.md`, which the pipeline's rules require; the implement scope guard refused it on #4815 (run 40055496344), and the #7986 plan hold no longer parks such a plan for a human. Completed orchestrator projects also drop a leftover `ai:integration-backpressure` label (#4038).

| The numbers that matter | Value |
| --- | --- |
| Comments read per stall answer | 1 (unchanged; the RECOMMENDED fallback reuses it) |
| Fragment paths allowed outside `files_touched` | `changelog.d/<N>-*.md`, `agents.d/<N>-*.md` for issue N only |
| New log lines | `STALL_CLARIFY_ANSWER`, `BACKPRESSURE_CLEARED ... reason=project_complete` |

What this means for consumer repos: an issue the worker answered no longer flips to a different decision when planning is slow to start, and auto-heal issues with a protected scope can ship the changelog entry they are asked for. Explicit `ai:scope:` and heal scopes are unchanged, and a fragment named after another issue is still refused.

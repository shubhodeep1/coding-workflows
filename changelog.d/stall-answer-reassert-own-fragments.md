<!-- changelog: fixed -->
- **Stall recovery keeps the clarify-respond worker's answer, implement accepts the issue's own changelog fragment, and consumer templates stop forwarding every secret.** Two unattended-pipeline defects from consumer tele-funtoken-msg-scoring #4815 are fixed, plus a stale label on completed projects.

When a clarification stalls after the clarify-respond worker already answered it, stall recovery now re-posts the worker's latest answer instead of the questions' RECOMMENDED letters. On #4815 the worker chose `Q1: B` and stall recovery replaced it with `Q1: A`; REFRAME answers and their execution plans are now repeated verbatim too. The RECOMMENDED fallback is used only when no trusted pipeline `/answer` is newer than the latest questions. Separately, an issue with an exact `files_touched` list can now commit its own `changelog.d/<issue>-<slug>.md` and `agents.d/<issue>-<slug>.md`, which the pipeline's rules require; the implement scope guard refused it on #4815 (run 40055496344), and the #7986 plan hold no longer parks such a plan for a human. Completed orchestrator projects also drop a leftover `ai:integration-backpressure` label (#4038).

| The numbers that matter | Value |
| --- | --- |
| Comments read per stall answer | 1 (unchanged; the RECOMMENDED fallback reuses it) |
| Fragment paths allowed outside `files_touched` | `changelog.d/<N>-*.md`, `agents.d/<N>-*.md` for issue N only |
| New log lines | `STALL_CLARIFY_ANSWER`, `BACKPRESSURE_CLEARED ... reason=project_complete` |
| Templates forwarding explicit secrets | 18 of 18 (was 1) |
| New opt-in variables / secret | `AI_PIPELINE_APPROVER_LOGIN`, `AI_ORCHESTRATOR_APP_LOGIN` / `AI_PIPELINE_APPROVAL_SECRET` |

Consumer workflow templates are now least-privilege. Every `workflow-templates/*.yml` wrapper forwards only the secrets its reusable workflow declares instead of all repository secrets, so a hardened consumer such as tele-funtoken-msg-scoring can turn template auto-updates back on. Hardened automation is opt-in: set `AI_PIPELINE_APPROVER_LOGIN` (a dedicated bot) and `AI_PIPELINE_APPROVAL_SECRET`, and automated `/answer` and `/approved` comments count only from that bot, with each automated approval HMAC-signed by `ai-plan.yml` and verified by `ai-implement.yml` before implementation starts. `AI_ORCHESTRATOR_APP_LOGIN` does the same for app-opened issues entering clarify. The unreachable `github-actions[bot]` branches are gone from the wrapper predicates.

What this means for consumer repos: an issue the worker answered no longer flips to a different decision when planning is slow to start, and auto-heal issues with a protected scope can ship the changelog entry they are asked for. Explicit `ai:scope:` and heal scopes are unchanged, and a fragment named after another issue is still refused. Consumers that do not set the new variables see no behaviour change from the template update, apart from the shared workflows no longer receiving secrets they never declared.

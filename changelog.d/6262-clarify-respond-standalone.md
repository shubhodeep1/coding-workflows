<!-- changelog: changed -->
- **Standalone issues now get their clarification questions answered by the Claude clarify-respond worker, and nobody is paged for questions the pipeline answers itself.** Security-audit findings, workflow heals and ordinary issues use the same `CLARIFY_RESPOND` worker as orchestrator-managed issues, and credentials or setup steps become placeholders instead of a stop.

Until now a standalone issue's questions were answered by picking each question's RECOMMENDED letter. That pick could not check GitHub state, and it posted dead answers when the RECOMMENDED option asked a human to "provide" something. `clarify.yml` also sent the `🚨 CRITICAL` "Clarification required" Telegram alert after its own auto-decide step had already answered, which is what happened on issue #6262 (run 37251621451). Now `clarify.yml` hands the questions to `orchestrate_clarify_respond.yml` and skips the alert. The worker answers from the repository plus a GITHUB FACTS block, the state of the PRs, issues, branches and runs the issue references, read on the host before the network-isolated model runs. A credential, token, account or other setup the work needs is decided as an UPPER_SNAKE_CASE placeholder secret or variable: the code reads it with no default and skips or fails closed until it is set. The placeholder is listed under "Setup required" in the `<!-- ai:auto-decisions:v1 -->` comment and in the PR body. If the worker fails, each question's RECOMMENDED option is posted instead.

The `core` install profile now includes the issue-comment responder wrapper, so its default delegation does not silently leave standalone questions unanswered. Existing core-profile repositories receive the wrapper on their next automatic workflow sync.

| The numbers that matter | Value |
| --- | --- |
| Clarification questions surveyed (19 Apr to 5 Oct 2026, excluding the release-gate fixture) | 136 on 72 issues |
| Of those, answerable from the repository or GitHub state | 125 (92%) |
| Of those, needing a credential, identity or external evidence (now placeholders or fetched facts) | 11 (8%) |
| RECOMMENDED options that asked a human to provide data | at least 6 |
| New repository variable | `STANDALONE_CLARIFY_RESPOND_ENABLED`, default `true` |
| GitHub API calls added per clarify-respond run | 1 GraphQL call plus at most 5 run reads; standalone mode adds 1 paginated comment read |

What this means for operators: the "Clarification required" alert now means a human is really needed. It comes from the worker as `WARNING` when it escalates (an issue with no stated intent) or its loop guard blocks, and as `CRITICAL` only when the worker fails and a question has no RECOMMENDED fallback. Placeholders to provision are listed under "Setup required" on the issue and in its PR; the feature that needs one stays off until you set it. Post `/reclarify` to answer an issue's questions yourself (the worker then stays away), or set `STANDALONE_CLARIFY_RESPOND_ENABLED=false` to go back to RECOMMENDED-only answers.

### For contributors

`orchestrate_clarify_respond.yml` "Check orchestrator metadata" now outputs `mode` (`orchestrator`, `standalone` or `skip`) and `respond`; `is_orchestrator` is unchanged and every later step gates on `respond`. Plan-stage questions on standalone issues are not answered by the worker (their comment does not start with `<!-- ai:clarification-questions -->`). `scripts/auto_decisions.py` gains `from-answers` and `SETUP-<n>` items; `scripts/clarify_github_facts.py` is new. `prompts/mode-clarify.txt` keeps `BLOCKED:` only for auth-walled content the task depends on. The workflow now also stages `clarify_data_provision_guard.py`, which consumer repositories never received, so their data-provision guard had been skipped silently. Tests: `tests/test_clarify_respond_standalone.py`, `tests/test_clarify_github_facts.py`, `tests/test_auto_decisions.py`.

Standalone clarify-respond bypasses the semantic answer cache because its key excludes live GitHub facts. Orchestrator-managed responses keep the cache path.

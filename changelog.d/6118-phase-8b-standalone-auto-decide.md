<!-- changelog: added -->
- **Standalone issues no longer wait an hour for their clarify questions to be answered.** `clarify.yml` now answers a standalone issue's questions as soon as it posts them, with each question's RECOMMENDED option, and logs every pick in one `<!-- ai:auto-decisions:v1 -->` comment that the PR body repeats.

Port P3 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` (Phase 8b). The new "Standalone auto-decide" step runs right after "Post clarification questions" on any issue that is not `ai:orchestrator-managed`. It posts the answer through the orchestrator's existing poster, `scripts/orchestrate_parse_and_post_answer.sh`, so the same `/answer [auto-answered-by-orchestrator]` comment, `ORCHESTRATOR_MAX_CLARIFY_CYCLES` loop guard and `ai:blocked` escalation apply. Each pick becomes an `AD-<n>` entry (question, pick, why, alternatives) in the issue's auto-decisions comment, which later clarify cycles edit in place, and `implement.yml` copies the entries into the PR body under "Auto-decisions" with issue references broken up. Clarify reuses one paginated comment fetch for the full decision history and backup loop guard while limiting prompt context to the oldest 50 comments; a failed fetch stops clarification before an automatic answer. The step is skipped after a human `/reclarify`, when any question lacks a RECOMMENDED option, or when `STANDALONE_AUTO_DECIDE_ENABLED` is `false`; the 60-minute stall-ladder auto-answer stays as the backstop.

| The numbers that matter | Value |
| --- | --- |
| Time from posted questions to `/answer` | same clarify run (was the 60-minute stall threshold) |
| New repository variable | `STANDALONE_AUTO_DECIDE_ENABLED`, default `true` |
| Extra GitHub API calls per clarify round | 2 writes (the answer and the AD comment), 0 additional read operations; the existing comment read now paginates (one call per page) |
| New log prefix | `STANDALONE_AUTO_DECIDE` |

What this means for operators: a standalone issue moves from clarify to plan without waiting, and every decision the pipeline took for you is listed on the issue and in its PR. Post `/reclarify` to see the questions again and answer them yourself; set `STANDALONE_AUTO_DECIDE_ENABLED=false` to restore the previous wait.

### For contributors

`scripts/auto_decisions.py` (`parse`, `render`, `pr-section`) accepts the same RECOMMENDED bullet drift as `extract_recommended_answers` and the plan.yml parser, and trusts only comments by an `OWNER`/`MEMBER`/`COLLABORATOR` or a `[bot]`. `tests/test_auto_decisions.py` covers the parser, the AD numbering, the trust and delimiter rules, the PR-body section and the workflow wiring, and runs in its own `ci.yml` step.

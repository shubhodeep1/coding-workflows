<!-- changelog: fixed -->
- **The orchestrator clarify-respond worker now answers only clarify's questions comment.** Before, a plan or an earlier auto-answer on an orchestrator-managed issue that happened to contain the words "clarification required" started another answer run, and the runs fed each other until the loop breaker escalated the issue.

The `orchestrate_clarify_respond.yml` job filter is `contains(github.event.comment.body, 'Clarification required')`, and GitHub's `contains()` ignores case. On issue #8650 the task was about the `ai:clarification` label, whose description is "AI clarification required before planning", so the plan comment and each of the three `/answer [auto-answered-by-orchestrator]` comments matched. Each run posted a new auto-answer, which started the next run, until run 41377181254 escalated the issue to `ai:blocked`. The "Check orchestrator metadata" step now also requires the comment's first line to be `<!-- ai:clarification-questions -->`, the marker clarify.yml puts on its questions comment and the capacity-wait replay keeps. Standalone mode already had this rule. Other comments are skipped with `AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=orchestrator_questions reason=not_clarify_questions outcome=skip`.

| The numbers that matter | Value |
| --- | --- |
| Extra answer runs on #8650 | 3 (02:05, 02:09, 02:15 UTC on 2026-11-02), then escalation |
| Wrapper `if:` change for consumers | none |
| Extra API calls | 0 |

What this means for operators: a plan, an auto-answer or a human comment that mentions "clarification required" no longer starts a clarify-respond run on an orchestrator-managed issue. Real questions comments are answered as before. Consumer repos get the fix on the next `@stable` sync with no change to their wrapper.

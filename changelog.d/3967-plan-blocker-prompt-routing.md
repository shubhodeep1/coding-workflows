<!-- changelog: fixed -->
- **The planner prompt now describes what actually happens to a blocker-only plan.** `prompts/mode-plan.txt` told the planner that `PLAN_SELF_CHECK: BLOCKER:` findings reopen clarification, which stopped being true when blocker-only plans were rerouted to the blocked path.

The AI Plan workflow routes a plan that ends with `PLAN_SELF_CHECK: BLOCKER:` and `STATUS: NOT_CLEAR` to the blocked path unless the same output also carries a Q-ID question block or a `NEEDS_CLARIFICATION` status. The prompt still promised the old clarification behaviour, so a planner that wanted a human answer had no reason to write the questions that would actually get it one. The self-check section now states the real routing and tells the planner to pose explicit Q-ID questions whenever a human answer could clear the blocker. Both `prompts/mode-plan.txt` and the `prompts/_templates/mode-plan.txt` source carry the corrected text; no workflow logic changed.

| The numbers that matter | Value |
| --- | --- |
| Files changed | `prompts/mode-plan.txt`, `prompts/_templates/mode-plan.txt` |
| Routing changed | none, the workflow already behaved this way |
| Reference incident | shubhodeep1/tele-funtoken-msg-scoring#3967, run 33721460753 |

What this means for operators: a plan blocked on something a human can answer is more likely to arrive with answerable questions attached, instead of landing on `ai:blocked` with nothing for `/answer` to resolve.

### For contributors

The behaviour this prompt now documents was introduced in the plan-routing fix for issue 3957. That change edited only the `Parse planning output` step in `.github/workflows/plan.yml` and left the prompt contract describing the pre-fix routing.

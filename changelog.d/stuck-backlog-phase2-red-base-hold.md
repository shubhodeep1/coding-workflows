<!-- changelog: added -->
- **Plans are no longer auto-approved while CI on the default branch is red.** When the newest decisive push run of `.github/workflows/ci.yml` on the default branch failed, and a trusted open `ai:workflow-heal` issue tracks that failure, the plan step does not post `/approved [auto-approved-by-plan]`. Before this change, one red `main` started an implement run for every clear plan. Each resulting PR inherited the red base, failed CI, and stalled in autofix and judge (Refs #9421, Phase 2 of the stuck-backlog plan).

The helper is `scripts/plan_base_ci_gate.sh`, called from the `Auto-approve clear plan` step in `.github/workflows/plan.yml`. When it holds a plan, it does four things:
- logs `AI_PHASE_GATE_V1 phase=plan gate=auto_approve reason=base_ci_red outcome=defer`;
- writes the step output `auto_approve_hold_reason=base_ci_red`;
- posts one deduplicated comment ending in `<!-- ai:red-base-approval-hold:v1 heal=<N> -->`;
- leaves the issue in `ai:awaiting-approval`.

The Telegram notice for this hold goes out at `DEBUG`, not as the usual `CRITICAL` page. Approval comes from the poller's existing stall recovery or from a maintainer's `/approved`. Workflow-heal issues are never held. Every read fails open: if the runs, the heal issues or the helper cannot be read, approval goes ahead as before.

| The numbers that matter | Value |
| --- | --- |
| Extra API reads per clear plan, green base | 1 per configured workflow (1 by default) |
| Extra reads when a workflow failed | +1 heal-issue listing |
| Extra calls when holding | +1 comments read, +1 post |
| Workflows read at most | 5 |

What this means for consumer repos: after the next `@stable` sync, the hold applies when the consumer's CI workflow is `.github/workflows/ci.yml` and the workflow-heal pipeline files heal issues there. A consumer whose CI file has another name sets `RED_BASE_HOLD_WORKFLOWS` to its path; until then the runs read finds nothing and approval behaves as before. `RED_BASE_APPROVAL_HOLD_ENABLED=false` turns the hold off.

Not in this change: a red-base check on the poller's own stall-recovery approval (`_plan_auto_approval_gate` in `scripts/orchestrate_poll_process.sh`). Until that lands, stall recovery may approve a held plan before the base turns green.

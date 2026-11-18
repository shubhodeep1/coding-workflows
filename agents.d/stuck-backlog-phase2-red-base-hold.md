<!-- agents: section="Workflow architecture" -->
Red-base plan auto-approval hold (Refs #9421, stuck-backlog plan Phase 2): `plan.yml`'s "Auto-approve clear plan" step sources `scripts/plan_base_ci_gate.sh` right before the `/approved [auto-approved-by-plan]` post. This is after every other gate (heal counterfactual, automation-path gate including the Q108 grant write, and the test-only heal marker). The helper is used only when it is a regular, non-symlinked file that sources cleanly and defines `red_base_approval_hold_check`; otherwise the step behaves as before.

`red_base_approval_hold_check <repo> <default_branch> <issue> <labels_json> [heal_route]` runs these checks in order:
1. Kill switch `RED_BASE_APPROVAL_HOLD_ENABLED`, default `true`. Unset, empty, `true`, `1`, `yes` or `on` keep the hold on; `false`, `0`, `no` or `off` turn it off. Any other value turns it off with a `::warning::`.
2. Exemption for `ai:workflow-heal` issues and for the heal route (`heal_cf_route=true`).
3. Anchored input checks on the repo, branch and issue number.
4. `RED_BASE_HOLD_WORKFLOWS`, default `.github/workflows/ci.yml`: comma-separated `.github/workflows/<file>.y(a)ml` paths. Duplicates are dropped and at most 5 are kept.
5. One `actions/workflows/<file>/runs?branch=<default branch, / as %2F>&event=push&status=completed&per_page=10` read per workflow. The newest run whose conclusion is `success` or `failure` decides, so a newer cancelled run cannot hide a red base.
6. Only when a workflow failed: one `issues?state=open&labels=ai:workflow-heal&sort=created&direction=asc&per_page=100` read, matched as in `triage_base_failure_heal_issue` (#9272). The issue must not be a PR, its author must be OWNER, MEMBER or COLLABORATOR, line 4 must be `source=<repo>#run`, the `workflow=<path>` marker must be in the first 7 lines, and the exact `Target branch` line must be present.

It sets `RED_BASE_HOLD_STATUS` (`red`, `green`, `pending` or `unknown`), `RED_BASE_HOLD_REASON`, `RED_BASE_HOLD_RED`, `RED_BASE_HOLD_HEAL`, `RED_BASE_HOLD_WORKFLOW` and `RED_BASE_HOLD_RUN_ID`. It returns 0 (hold) only for `red`. Every read fails open: `runs_lookup_failed`, `heal_lookup_failed`, `failure_untracked` and `no_completed_run` do not hold.

On a hold, the step does the following:
- logs `AI_PHASE_GATE_V1 phase=plan gate=auto_approve reason=base_ci_red outcome=defer issue=<n>`;
- writes `auto_approve_hold_reason=base_ci_red`;
- adds a `### Automatic plan approval held (base CI red)` summary;
- calls `red_base_approval_hold_comment`, which posts one comment ending in `<!-- ai:red-base-approval-hold:v1 heal=<N> -->`, skipped when a trusted comment newer than the newest plan comment already has that marker;
- exits with `auto_approved=false`.

"Telegram plan notification" reads `PLAN_AUTO_APPROVE_HOLD_REASON` and sends that hold at `DEBUG` instead of `CRITICAL`, in the non-orchestrator branch only.

API cost: 1 runs read per workflow on each approval about to be posted, plus 1 heal listing when a workflow failed, plus 1 comments read and 1 post when holding.

Release path: the poller's existing `auto_approve` stall action or a maintainer's `/approved`. The poller-side red check (`_plan_auto_approval_gate` in `scripts/orchestrate_poll_process.sh`) is not part of this phase, so stall recovery can approve a held plan before the base is green.

Consumers whose CI file is not `ci.yml` set `RED_BASE_HOLD_WORKFLOWS`; until then the runs read fails and nothing is held.

Tests: `tests/test_plan_base_ci_gate.py`.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `RED_BASE_APPROVAL_HOLD` (`scripts/plan_base_ci_gate.sh`, sourced by `.github/workflows/plan.yml` "Auto-approve clear plan"; Refs #9421). Format: `outcome=<held|pass|skipped> issue=<n|none> heal=<N|none> workflow=<path|none> run=<id|none> status=<red|green|pending|unknown> reason=<disabled|heal_issue_exempt|input_invalid|no_workflows|base_green|no_completed_run|runs_lookup_failed|heal_lookup_failed|failure_untracked|base_ci_red>`
LOG_PREFIX.name=RED_BASE_APPROVAL_HOLD

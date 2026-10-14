<!-- changelog: changed -->
- **Six slow test steps moved from the CI job `tests-hooks-and-orchestrator` to `tests-heal-plan-and-validation`.** The hooks job took 15m09s of its 20-minute timeout on `main` (run 38508333607, budget-watch issue #7366), and a job that hits its timeout turns CI red with no code fault. The heal job had the most room. These steps now run there, unchanged and under the same names:
  - `Activation verification tests (port P4)`
  - `Unblock judge and scan tests (replace-claude-sessions Phase 7)`
  - `Orchestrator judges on the Claude engine tests (Phase 5c)`
  - `Validation harness sandbox tests`
  - `Validation shell-quoting and credential-scrub tests`
  - `Check-failure triage workflow security tests`

  No job was added or renamed, no `timeout-minutes` changed, and the aggregate `CI / lint` check is unchanged. A failure in one of these tests now appears under the `tests-heal-plan-and-validation` check-run.

  What this means for operators: nothing to change; no new variable.

## Implementation plan: release gate waits on a working implement run (issue #8241)

Structural reconstruction of the heal plan for #8241 (the original plan text
is not stored in the repository). It keeps the shape that let
`AUTO_IMPLEMENT_ON_CLEAR_PLAN` approve it: a CLEAR status and a PASS
self-check, with the unaddressed stall named only under risks.

Scope-mode: Hold Scope
Reuse-audit: extends .github/workflows/test-and-mark-stable.yml
Scope-mode justification: The heal issue names one failing check in the
release gate; the plan widens the implement-phase inactivity wait only.

## Files likely to change

- `.github/workflows/test-and-mark-stable.yml`
- `tests/test_test_and_mark_stable_plan_polling_guard.py`
- `changelog.d/8241-release-gate-implement-working-run.md`

## Functions or modules to implement

Count an implement run that is still working as activity, so the gate does
not give up while a run is in progress.

## Decisions

### D1 — Treat an in-progress implement run as activity
- Chosen: read the run's status before timing out.
- Alternatives considered: a longer fixed wait.
- Why: the wait already polls the runs list.

## Risks and edge cases

- If run 1 was actually stuck in one long step, the gate will still time out
  after 30 minutes. The plan does not claim to fix that.

## Counterfactual check

- e2e-smoke-test: the recorded implement run spent 31 minutes in one step
  ("Pre-push affected-test gate"), so the gate would still have timed out
  after 30 minutes with this change.

HEAL_COUNTERFACTUAL: FAIL: implement run 40610855593 spent 31 min in one step; the gate still times out after 30 min

Pre-execution self-check: the plan is implementable.

STATUS: CLEAR
PLAN_SELF_CHECK: PASS

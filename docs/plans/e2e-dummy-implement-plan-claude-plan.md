# E2E dummy project for /implement-plan-claude

## Summary
A throwaway two-phase, docs-only project whose only purpose is to drive `/implement-plan-claude` through its whole lifecycle in `shubhodeep1/coding-workflows` (phase PRs, merge waits, security pass, validation, completion PR) and record every permission prompt the command raises, so the prompts can be added to the `.claude/settings.json` allowlist.

## Context
- `/implement-plan-claude` (`.claude/commands/implement-plan-claude.md`) ships one PR per phase, waits for `review_autofix.yml` to merge each, runs the security audit and runtime validation, then moves the plan to `docs/completed/`.
- Real runs of the command raise permission prompts at session start and possibly mid-session. The prompts are not recorded anywhere, so the allowlist cannot be built from evidence without a live run.
- The user explicitly approved a full-lifecycle run for this test, including the billed security-audit and validation dispatches and dummy commits on the default branch.

## Goals
- Phase 1 merges and `docs/e2e-dummy/README.md` exists on the default branch.
- Phase 2 merges and `docs/e2e-dummy/` no longer exists on the default branch.
- The command reaches its completion PR, which moves this plan to `docs/completed/`.
- Every permission prompt raised during the run is recorded by the operator.

## Non-goals
- Any code, workflow, config, script, schema, or contract change.
- Any behaviour change; no `changelog.d/` fragment (§20.A: nothing observable changes).
- Consumer-repo impact (§14): none, nothing under `workflow-templates/` or `.claude/` changes.
- Fixing anything the run reveals; findings are handled in a separate change.

## Constraints
- §5 minimal change set: each phase touches exactly one file.
- §6: no identifier is renamed or removed; `docs/e2e-dummy/` is new and is removed by the same project.
- §10 / §15 / §18: not applicable. No MongoDB, no new API calls, no scripts.
- §19: this plan links no `ai:orchestrator-tracking` issue.

## Approach
Two trivially small docs phases so the run spends its time in the command's own machinery (branching, PRs, merge waits, dispatches) rather than in implementation.

## Phases & Merge Strategy
The user specified exactly two phases. Unlike a normal plan, phase 2 depends on phase 1 (it deletes the file phase 1 adds). This is accepted (see Risks) because `/implement-plan-claude` ships phases strictly in order and never starts phase `n+1` before phase `n` merges.

1. **Phase 1: add the marker file.**
   - Scope: create `docs/e2e-dummy/README.md` with one short paragraph.
   - Files: `docs/e2e-dummy/README.md` `[new]`.
   - Done: `test -f docs/e2e-dummy/README.md` succeeds on the default branch after merge.
   - Rollback: revert the phase 1 PR.
2. **Phase 2: remove the marker file.**
   - Scope: delete `docs/e2e-dummy/README.md`, which removes the directory.
   - Files: `docs/e2e-dummy/README.md` `[del]`.
   - Done: `test ! -e docs/e2e-dummy` succeeds on the default branch after merge.
   - Rollback: revert the phase 2 PR, which restores the phase 1 file.

## Implementation Steps
Phase 1:
1. Create `docs/e2e-dummy/README.md` containing this paragraph: "This directory exists only for the `/implement-plan-claude` end-to-end test described in `docs/plans/e2e-dummy-implement-plan-claude-plan.md`. Phase 2 of that plan deletes it."
2. Verify with `test -f docs/e2e-dummy/README.md`.

Phase 2:
1. `git rm docs/e2e-dummy/README.md`.
2. Verify with `test ! -e docs/e2e-dummy`.

## Files & Modules
- `docs/e2e-dummy/README.md` `[new]` in phase 1, `[del]` in phase 2.
- `docs/implement-plan/e2e-dummy-implement-plan-claude.md` `[new]`: the command's own progress log.
- `docs/plans/e2e-dummy-implement-plan-claude-plan.md` moved to `docs/completed/` by the command's completion PR.

## Tests
- Phase 1: `test -f docs/e2e-dummy/README.md`.
- Phase 2: `test ! -e docs/e2e-dummy`.
- No automated tests change; the repo's existing CI runs unchanged on each PR.

## Risks & Mitigations
- Phase 2 depends on phase 1. ACCEPTED: the user specified the two-phase shape, and the command merges phases strictly in order.
- The security audit and validation runs are billed. ACCEPTED: the user approved them for this test.
- The default branch gains dummy commits, a progress log, and a completed plan. ACCEPTED: the user approved the full lifecycle; the marker file is gone after phase 2.
- The security audit could open `ai:security` follow-ups unrelated to this project, extending the run. Mitigation: the command caps security cycles at 5 and stops with `Status: BLOCKED` when exhausted.

## Rollout
None. Merging each phase is the rollout.

## References
- `.claude/commands/implement-plan-claude.md`
- `.claude/settings.json`

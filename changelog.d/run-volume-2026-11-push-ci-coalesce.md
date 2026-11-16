<!-- changelog: changed -->
- Push CI now keeps at most one running and one waiting run per branch, and the integration PR readiness check no longer takes a runner for label changes it ignores.

Every push to `main`, `stable` or an `orchestrator/project-*` branch used to start its own 37-job `CI` run, and runs for the same branch never replaced each other. On 2026-11-16 four `main` push runs (148 jobs) waited in the queue at once. Push runs now share the concurrency group `ci-push-<ref>`: the running run always finishes, and a newer push replaces only the run still waiting, so the newest commit is always tested. `Integration PR readiness check` now skips `labeled` / `unlabeled` events for every label except `ai:override-incomplete-merge`, the only PR label its script reads.

| The numbers that matter | Value |
| --- | --- |
| Push CI runs per branch at any time | at most 2 (1 running, 1 waiting) |
| Jobs in one full `CI` run | 37 |
| `main` push runs queued together on 2026-11-16 | 4 |
| Readiness-check runner jobs avoided | about 170 a day (label changes) |

What this means for operators: a busy branch no longer stacks one full CI run per merged commit. Intermediate commits get no push CI of their own; each one already passed its PR's CI, and the next run that starts covers it. A push now waits for the running CI on the same branch to finish, and heal's stale auto-close, which counts green push runs, can take longer.

### For contributors

Decisions D14 and D15 in `docs/plans/reduce-actions-run-volume-plan.md`. Tests: `tests/test_ci_concurrency.py` (push group per branch, no cancellation of the running run) and `tests/test_check_integration_pr_readiness.py` (the job condition and its label).

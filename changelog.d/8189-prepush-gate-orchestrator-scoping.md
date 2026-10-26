<!-- changelog: added -->
- **Orchestrator issues now list the tests their change affects.** When the orchestrator splits a project into issues, it adds each issue's affected tests and registry files to the issue's planned file list and shows them in an advisory "Expected files" section of the issue body.

The pre-push affected-test gate module (P1, #7137) selects tests for a set of changed files. Phase P2 of `docs/plans/prepush-affected-test-gate-plan.md` runs it on each issue's planned `files_touched` before the partition guard, so two sibling issues that would edit the same test file are put in order instead of running in parallel and conflicting. The implementer also sees which tests to update in the same change. Nothing is enforced: the section is advisory and the scope guard reads it as no allowlist.

| The numbers that matter | Value |
| --- | --- |
| Most entries per issue's file list | 50 (extra tests are dropped and logged) |
| New GitHub API calls | 0 |
| Run time per issue | under 60 s (timeout), about 1 s in this repository |

What this means for operators: set the repository variable `PREPUSH_TEST_GATE_MODE=off` to turn the augmentation off. When the support ref has no gate module, nothing changes. Log lines start with `PREPUSH_TEST_GATE phase=augment`.

<!-- changelog: changed -->
- **`/implement-plan-claude` no longer stops a project just because its third conformance audit found something to fix.** The fix PR that audit opens now gets a narrower `/verify-activation — scope fix-check #<PR>` of its own diff instead of a fourth audit, and the chain continues to security and validation when the fix holds.

The conformance cap is 3 runs per project, and every fix PR a run opens used to be re-audited by a new run. So when run 3 opened a fix PR, the project was certain to stop at `Status: BLOCKED` and wait for a human, however small the fix. Issue #4545's project hit exactly that at stage `conformance 4/3` after fix PR #4616, a one-line prompt rule plus its test. The new `conformance 3/3 — fix check` stage checks only the files that fix PR changed against the findings its body lists. It opens no fix PR and does not count toward the cap. It still stops the project when a listed finding is unresolved or the diff introduced a defect (`FIX-DEFECTIVE`). Defects it notices in code the fix did not touch go into the progress log and the final PR's body instead of starting another round.

The conformance audit also gains a **Sibling paths** check. It covers every template, prompt, or config variant the changed code selects between, and every child process that inherits an environment variable exported around a subprocess. These are the two kinds of defect that runs 2 and 3 of issue #4545's project found one at a time.

| The numbers that matter | Value |
| --- | --- |
| Conformance runs per project | 3 (unchanged) |
| Fix checks after the third run's fix PR | 1, not counted toward the cap |
| Stage that stalled issue #4545's project | `conformance 4/3` after PR #4616 |
| Files updated | `.claude/commands/implement-plan-claude.md`, `.claude/commands/verify-activation.md` (and their `workflow-templates/` copies) |

What this means for operators: a `/implement-plan-claude` project now asks you about its conformance cap only when a fix really is broken. `/verify-activation` also accepts `— scope fix-check #<PR>` when you want to re-check a single merged fix PR by hand.

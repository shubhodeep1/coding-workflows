<!-- changelog: changed -->
- **Stacked `/implement-plan-claude` projects now skip their own runtime validation automatically, instead of stopping to ask.** This rule is temporary and lasts until #4734 lands.

`validate.yml` validates a project branch only when that branch's final PR targets the default branch. So every project whose final PR targets another project's `claude/implement-plan-<slug>` branch used to stop at `validation 1/3` with a CLAUDE.md §28.C blocker. The operator's standing answer was always Q17: A, skip, because the parent project validates a branch that contains the change before anything reaches the default branch. The new interim bullet in CLAUDE.md §28.C lets the stage session apply that answer itself. It records `Validation: skipped (covered by #<parent>'s project validation)` plus one auto-decision for review, and it goes on to the completion PR. A final PR into the default branch or `stable`, a validation run that was dispatched and failed, and a consumer wrapper without `target_ref` still stop and ask.

| The numbers that matter | Value |
| --- | --- |
| Projects stopped by this blocker on 2026-09-29/30 | 9 (#4927, #5012, #5125, #5127, #5147, #5148, #5226, #5258, #5298) |
| Wait per stop | up to about 1 hour (master answer plus the hourly issue pickup) |
| Sunset | removed by the PR that puts #4734's `validate.yml` change on the default branch |

What this means for operators: stacked projects no longer wait on a validation question. Their skip shows up in the auto-decision list at the end of each project (§28.E), where it can be reviewed. Validation still happens once, in the parent project, before `main`.

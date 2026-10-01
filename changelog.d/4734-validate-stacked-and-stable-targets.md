<!-- changelog: fixed -->
- **`validate.yml` now validates issue-mode projects whose final PR targets another project branch or `stable`.** Before, an explicit `target_ref` was refused unless its one open PR went into the default branch, so those projects stopped at the validation stage and needed a human `/reclarify`.

The `Authorize explicit validation target` step of `.github/workflows/validate.yml` now accepts two more bases for the target's single open PR. The first is a `claude/implement-plan-*` project branch that itself has exactly one open PR into the default branch, with a same-repository head and base, an OWNER, MEMBER, or COLLABORATOR author, and a 40-hex head SHA. The second is `stable`, when the target is `claude/implement-plan-issue-<n>-*` and issue `<n>` is a workflow-heal issue the heal automation filed: labelled `ai:workflow-heal` by its author within 120 seconds of creation and by nobody else, authored by an OWNER User or `github-actions[bot]`, carrying the `workflow-failure-heal:fp=` marker line, and naming `stable` as its target branch. The target PR must also be opened by the issue's author, so neither a label nor a branch name can authorize a `stable` target on its own (#4791). Every existing check stays: exactly one open PR for the target (0 or 2+ still fails), same-repository head and base, a trusted author, and a pinned 40-hex head SHA. Any other base is refused, and deeper stacking is refused.

| The numbers that matter | Value |
| --- | --- |
| GitHub reads per explicit target | 1 for a default-branch base, 2 for a project-branch base, 3 for a `stable` base (one more per further 100 issue events) |
| Levels of project-branch stacking allowed | 1 |
| Projects unblocked | #4665 (workflow heal, final PR #4667 into `stable`) |

What this means for operators: once this reaches `@stable`, a blocked heal or follow-up project gets past validation after one `/reclarify` on its issue. No wrapper or repository variable changes.

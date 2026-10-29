<!-- changelog: changed -->
- **`/deploy-activate` now ticks each step it finishes in the repository's "Operator steps waiting" issue.** Until now a deploy session marked progress only in its own `docs/deploy-activation/<ref-slug>.md` log, so the `ai:operator-step` issue kept showing steps that were already done.

While planning, the command finds the entries for its reference (`pr-<N>`, `project-<N>`, or the keys a batch lists) and maps each unticked step to a runbook step. After a runbook step is confirmed, it re-reads that entry's newest pipeline-authored comment and changes only the matching `- [ ] **<step title>**` line to `- [x]` with `mcp__github__update_issue_comment`, the same edit as clicking the checkbox. It never ticks an unverified step or a step outside its runbook, and never adds, removes or rewords entries. A failed edit is noted in the log and retried later; it does not block the runbook. Resumed runs tick any already-done step that is still open.

| The numbers that matter | Value |
| --- | --- |
| Copies changed | `.claude/commands/deploy-activate.md`, `workflow-templates/.claude/commands/deploy-activate.md` |
| GitHub writes added | 1 comment edit per ticked entry, only after a confirmed step |

What this means for operators: the "Operator steps waiting" issue in each repo now shrinks as `/deploy-activate` sessions finish steps, so it lists only what is still left. Consumer repos get the change on the next `@stable` sync.

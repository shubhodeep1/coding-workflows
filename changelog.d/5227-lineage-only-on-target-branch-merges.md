<!-- changelog: security -->
- **A PR merged into a branch that is not an issue's target branch no longer marks that issue's AI-memory lineage as `merged`.** `issue_pr_status.yml` now finalizes lineage only for the issues its target-branch gate accepted.

The label and close step in `.github/workflows/issue_pr_status.yml` already left an issue alone when its linked PR merged into a branch other than the default branch, the issue's `Integration branch:` / `Target branch:`, or a managed child's `orchestrator/project-<T>` branch (issue #4813). The next step, `Finalize linked issue lineage state`, still called `memory_finalize_task --final-state merged` for every linked issue. So any PR merged into an unrelated branch that mentioned an issue could record that issue's task as finished in AI memory. The security audit reported this as issue #5227. The gate step now exports `LINEAGE_FINALIZE_ISSUE_NUMBERS`, the issues it accepted, and the lineage step finalizes only those. When every linked issue was rejected, the step logs `No linked issue was accepted by the target-branch gate; skipping lineage finalization.` and emits `AI_MEMORY_TELEMETRY` with `"reason":"no_accepted_issues"`.

| The numbers that matter | Value |
| --- | --- |
| Issues finalized on a merged PR | only those the target-branch gate accepted (was: every linked issue) |
| Unchanged | orchestrator-tracking issues, PRs closed without merging, the Telegram alert and cleanup steps (`LINKED_ISSUE_NUMBERS`) |
| New GitHub API calls | 0 |

What this means for operators: an issue's lineage record reads `merged` only once its work lands on the branch it targets. Consumer repos get the fix through the `ai-issue-pr-status.yml` wrapper on the next `@stable` sync, with no variable to set.

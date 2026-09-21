<!-- changelog: fixed -->
- **Release budget contracts now allow memory compaction to finish and stay synchronized with their workflow limits.** Memory maintenance receives a 20-minute job cap, its release watcher permits 22 minutes, and contract tests enforce the ordered timeout hierarchy and derive the E2E smoke limit from the workflow's named budget.

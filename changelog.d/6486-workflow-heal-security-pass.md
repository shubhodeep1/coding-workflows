<!-- changelog: security -->
- **Workflow-heal fix PRs no longer skip the single-issue security pass on the strength of their issue label and fingerprint marker.** Heal issues can originate from logs contributors influence, so their fix PRs into the default branch now require a current-head audit.

| Labels checked | Before | After |
| --- | --- | --- |
| Skip labels | `ai:security`, `ai:workflow-heal` | `ai:security` |

What this means for operators: heal fix PRs into the default branch now wait for a clean single-issue audit before auto-merge.

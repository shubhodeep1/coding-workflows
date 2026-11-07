<!-- changelog: added -->
- **A heal merged into `stable` now gets a follow-up heal issue on `main` when the same patch still applies there.** Behind `WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED` (off by default), when a heal PR (a same-repository `ai/issue-<N>` branch whose issue is a heal) merges into `stable`, the PR-close workflow checks the merged patch against the default branch with `git apply --check`. If it applies and no port issue exists yet, it files one heal issue targeting the default branch with the patch summary and `Refs #<N>`. The heal pipeline itself still never opens PRs on two branches. This runs only in coding-workflows; consumer repositories are unaffected.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED=false`) |
| Extra GitHub API calls | 0 for non-heal or non-`stable` PRs; otherwise 1 GraphQL read and at most 1 issue create |
| Log line | `WORKFLOW_HEAL port outcome=<filed|not_needed|not_applicable>` |

What this means for operators: nothing changes until the switch is on; with it on, each heal fixed on `stable` is carried over to `main` through the normal heal pipeline.

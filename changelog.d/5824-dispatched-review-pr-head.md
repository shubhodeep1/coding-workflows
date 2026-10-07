<!-- changelog: security -->
- **Dispatched PR reviews now read the PR head instead of the dispatching branch.** The review gate validates the head repository and SHA before checkout. Fork or unknown-head PRs skip review and deterministic auto-merge; mismatched workspaces skip agents, and project OpenCode configuration or plugins are refused before agent setup. No-PR branch reviews remain unchanged.

Reviewers and other file-reading agents now see files added on a PR head during `workflow_dispatch` reviews. The gate uses authenticated PR metadata to select the checkout commit instead of the dispatching branch's commit. If the source tree, split workspace, or PR metadata does not agree on that commit, the review does not start. A PR-controlled OpenCode configuration also stops agent setup in the credential-bearing checkout.

What this means for operators: dispatched reviews no longer report PR-added files as missing from the default branch, and unsafe or unverifiable PR checkouts are left unreviewed rather than silently falling back.

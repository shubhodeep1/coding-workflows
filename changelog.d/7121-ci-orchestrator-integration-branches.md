<!-- changelog: changed -->
- **CI now checks sub-issue PRs before they merge into an orchestrator project branch.** `ci.yml` runs on pull requests into, and pushes to, `orchestrator/project-*`, not only `main` and `stable`.

Until now a sub-issue PR merged into its project's integration branch without CI, so a breaking change surfaced only at the project's final PR into `main`, after later waves had built on it. The push runs also give check-failure triage a base-branch result to compare against. When the integration branch is green, a failing sub-issue PR is left to its own review autofix instead of opening a triage issue. Heal intake and the `budget-watch` job still act on `main` pushes only.

| The numbers that matter | Value |
| --- | --- |
| New `pull_request` / `push` branch pattern | `orchestrator/project-*` |
| Extra CI runs | one per sub-issue PR head, one per merge into an integration branch |

What this means for operators: expect more CI runs while an orchestrator project is active, and expect broken sub-issue PRs to stop before they merge instead of at the project's final PR.

### For contributors

The "Prepare template-only PR live copies for parity tests" step now also runs for these PRs and pushes. Pushes pass their own `before` SHA because they have no PR base. When that SHA is all zeros (branch creation) or unreachable (force push), the step warns and uses the merge-base with `main` instead, because the sync script skips drift recovery without a reachable `--before`. Template changes merged by earlier sub-issue PRs are copied by the sync script's drift recovery.

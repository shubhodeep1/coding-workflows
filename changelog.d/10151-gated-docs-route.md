<!-- changelog: fixed -->
- **Docs-only and generated PRs now reach `main` by auto-merge, not by a direct admin merge.**

The workflow log analysis report PR used to be merged directly with the owner's token, which bypassed the required `ai-review/head-gate` and `ci/final` statuses. It now only arms auto-merge bound to the pushed head. A deterministic-skip head's `ai-review/head-gate` success names the skip reason, and the skip paths that do not wait for checks now request the final CI, so `ci/final` is posted too. A new test fails CI when any workflow or script adds an unguarded direct `gh pr merge`.

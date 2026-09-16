<!-- changelog: fixed -->
- **Review auto-merge now refuses to merge a PR head that differs from the head the workflow authorized.** Both deterministic small/docs-only skips and completed review runs pass their observed head SHA to `gh pr merge --match-head-commit`; missing snapshots fail closed, and concurrent pushes are left for their own `synchronize` review run instead of inheriting an earlier decision.

Forward-merge fallback PRs retain real merge commits, while regular PRs retain squash merges. This change binds the initial auto-merge request only; GitHub may still keep auto-merge enabled across later pushes while required checks are pending, which remains separate lifecycle hardening work.

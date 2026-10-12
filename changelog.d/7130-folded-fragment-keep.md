<!-- changelog: fixed -->
- **A PR that changed a changelog or agents fragment after the release folded it no longer fails every autofix run.** Conflict preparation keeps the PR's copy of the fragment instead of leaving it to the model resolver.

The release fold deletes every `changelog.d/*.md` and `agents.d/*.md` fragment on `main`. PR #6844 had rewritten `changelog.d/6763-wrapped-git-write-guard-operator-step.md`, so merging `main` gave a modify/delete conflict. The model resolver edited only `agents.md`, `check_resolver_diff.sh` reported OK, and the commit was refused because the fragment stayed unmerged, on every run (heal issue #7130). `scripts/review_conflict_prepare.sh` now stages the PR's version of such a fragment before the resolver runs. A fragment-only conflict is committed without a model call. `check_resolver_diff.sh` now fails, naming the path and its index stages, when the resolver leaves a conflicted path untouched.

| The numbers that matter | Value |
| --- | --- |
| Kill switch | `CONFLICT_FOLDED_FRAGMENT_KEEP_ENABLED` (default `true`) |
| Paths covered | top-level `changelog.d/*.md`, `agents.d/*.md` |
| Conflict shape | modify/delete, index stages 1 2 |

What this means for operators: the next release publishes the PR's fragment change as a new entry, and the PR reaches review instead of failing at conflict resolution.

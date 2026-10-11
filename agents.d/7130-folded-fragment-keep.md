<!-- agents: section="Workflow architecture" -->
Pre-review conflict preparation keeps a folded fragment the PR changed.
`scripts/review_conflict_prepare.sh` stages the PR's copy of a top-level
`changelog.d/*.md` or `agents.d/*.md` path in a modify/delete conflict with
index stages 1 2 (the base branch's release fold deleted it, the PR changed
it), before the manifest union block and the model resolver
(`CONFLICT_FOLDED_FRAGMENT_KEEP_ENABLED`, default `true`). A fragment-only
conflict is committed as `[ai-merge-resolve]` without a model run; on
`orchestrator/project-*` the commit waits for the integration fingerprint
check (`_ff_defer_commit`, shared with the manifest-only deferred commit).
`scripts/check_resolver_diff.sh` now also fails when a conflicted path the
resolver did not touch is still unmerged, naming it with its index stages.

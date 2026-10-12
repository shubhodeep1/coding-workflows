<!-- agents: section="Workflow architecture" -->
The conflict resolver's host-only refusal (`sandbox_path_host_only`) has one
deterministic exception: the merged-PR guard hook. Before resolver dispatch,
`scripts/review_conflict_prepare.sh` stages
`.claude/hooks/pr_merge_status_guard.py` from the clean merge of its
`workflow-templates/` twin when each side's live copy equals that side's
template (blob and mode) and the twin itself did not conflict
(`CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED`, default `true`, #6882). A
hook-only conflict is committed as `[ai-merge-resolve]`; on
`orchestrator/project-*` the commit waits for the integration fingerprint
check. Any other shape logs `Guard-hook template merge: skipped reason=<...>`
and is left for the refusal.

<!-- agents: section="Review pipeline consolidator + ledger contract" -->
| `CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED` | `true` | Defaulted inside `scripts/review_conflict_prepare.sh`; `review_autofix.yml` does not forward it from a repository variable. Before resolver dispatch, a conflict on `.claude/hooks/pr_merge_status_guard.py` is resolved to the clean merge of `workflow-templates/.claude/hooks/pr_merge_status_guard.py`, but only for content conflicts (stages `1 2 3` or `2 3`) where each side's live copy equals that side's template in blob and mode and the template has a single regular stage-0 entry. A hook-only conflict is committed as `[ai-merge-resolve]` (on `orchestrator/project-*`, only after the integration fingerprint check passes). Any other shape logs `Guard-hook template merge: skipped reason=<disabled\|no_merge_head\|stage_shape\|template_unmerged\|template_missing\|template_not_regular\|ours_mismatch\|theirs_mismatch\|unsafe_path>` and keeps the `sandbox_path_host_only` refusal. |

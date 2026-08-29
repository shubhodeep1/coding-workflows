<!-- changelog: fixed -->
- **The review_autofix conflict resolver no longer dies with `helpers_missing` on consumer repos whose `stable` staging list predates the opencode cutover.** Conflicted PRs stopped looping on "PR autofix failed".

`scripts/review_conflict_resolve.sh` is staged main-primary, but the staging list that builds the runtime bundle runs from the consumer's `SCRIPT_REF`. When that ref's `scripts/stage_workflow_support.sh` predates the opencode cutover (#3848), the bundle never contains `opencode_helpers.sh` or `write_opencode_config.sh`, so the resolver exited 1 immediately with `failure_class=helpers_missing`, the editor's committed fixes were never pushed, and every conflicted PR failed with `finalize_reason=push_failed`. The resolver now falls back to the on-disk support checkouts (`.codex-workflow-src-main` first, then `.codex-workflow-src`, then the repo's own `scripts/`) for each missing dependency and logs a `::warning::` naming the fallback path. Both helpers are also added to `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` so future stable refs stage them from the same snapshot as the resolver.

| The numbers that matter | Value |
| --- | --- |
| Failing runs diagnosed | drhyg_ecommerce_automation 33278423340, 33279585316 |
| Resolver failure class eliminated | `helpers_missing` (rc=1 at startup) |
| Dependencies now resolved via fallback | `opencode_helpers.sh`, `write_opencode_config.sh` |

What this means for consumer repos: no action needed. The resolver is fetched fresh from main at run time, so the fix applies to the next `review_autofix` run on a conflicted PR without waiting for a stable re-tag.

### For contributors

The lockstep rule generalises: any new dependency sourced by a main-primary script from `SUPPORT_SCRIPTS_DIR` must either be added to `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` in the same PR or carry its own checkout fallback, because the staging list executing on consumers is the older `SCRIPT_REF` copy.

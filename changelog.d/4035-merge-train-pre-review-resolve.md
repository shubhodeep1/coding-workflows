<!-- changelog: changed -->
- **Review/autofix resolves base-branch conflicts before the reviewers run, queues `ai/issue-*` PRs that edit the same files behind the oldest one (merge train), serializes orchestrator siblings that declare no `files_touched`, and no longer deletes a consumer's tracked `agents.md` during conflict resolution.**

On 2026-09-07 the pipeline sent 18 "Merge conflicts resolved automatically" pings across three repos: every conflict was found only after a 35–96 minute reviewer pass and an editor commit, and a burst of ten same-file PRs against `main` conflicted on every merge. `review_autofix.yml` now hands a content conflict found by the pre-review merge-topology gate straight to the existing resolver tail and re-dispatches the review on the merged head, so reviewers and editor never work on a head the base already conflicts with. A new `scripts/review_merge_train.sh` labels a younger `ai/issue-*` PR `ai:merge-queued` when its changed files overlap an older open `ai/issue-*` PR on the same base, and releases it (label removed, review re-dispatched) from `cancel_on_pr_close.yml` the moment a PR merges and from `orchestrate_poll.yml` on every tick, including in repos with no active orchestrator project. The orchestrator partition guard treats an empty `files_touched` list as unknown scope and serializes it, closing the bypass project binance-blessings#249 used. The resolver, review-blocked judge and poller keep any root file the consumer repo tracks instead of removing it as a workflow artifact.

| The numbers that matter | Value |
| --- | --- |
| Resolver rounds for N overlapping PRs | at most N (was up to N²) |
| `PRE_REVIEW_CONFLICT_RESOLVE_ENABLED` default | `true` |
| `MERGE_TRAIN_ENABLED` / `MERGE_TRAIN_MAX_OLDER_PRS` defaults | `true` / `20` |
| `CONFLICT_RESOLVED_ALERT_LEVEL` default | unset (falls back to `ALERT_MSG_LEVEL`, then `DEBUG`) |
| New label | `ai:merge-queued` |
| New partition overlap type | `unknown_scope` |
| Workflows changed | `review_autofix.yml`, `orchestrate_poll.yml`, `cancel_on_pr_close.yml` |

What this means for consumer repos: on the next `@stable` sync, overlapping AI PRs wait their turn instead of fighting over the base, each PR resolves its conflict at most once and before any reviewer spend, and a repo-owned `agents.md` survives conflict resolution. Set `MERGE_TRAIN_ENABLED=false` or `PRE_REVIEW_CONFLICT_RESOLVE_ENABLED=false` to opt out; set `CONFLICT_RESOLVED_ALERT_LEVEL=SILENT` to drop the per-resolution Telegram ping.

### For contributors

The merge train's API budget is documented in the script header (§15): this PR's paths come from the diff `review_collect_pr_metadata.sh` already fetched, older PRs cost one `pulls/N/files` page set each (cached per run, capped by `MERGE_TRAIN_MAX_OLDER_PRS`), and every failure path logs a `::warning::` and continues without queuing. The 16 reviewer/editor-phase steps carry `env.AUTOFIX_PRE_REVIEW_RESOLVE != 'true'`; the tail from `Detect merge conflicts` onward is unchanged. The poller's in-progress conflict loop skips `ai:merge-queued` PRs so it does not dispatch rounds the train is holding back.

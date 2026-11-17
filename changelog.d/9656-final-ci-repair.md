<!-- changelog: added -->
- **A real final-CI failure on an approved PR now gets one CI-fix commit instead of a full review round.** The review run the failure dispatches reproduces the failing tests and repairs them, and falls back to the full review only when it cannot.

Until now a red final CI run on an approved head went back through the whole reviewer panel and editor, on code review had already approved. The review gate now selects a repair when the head carries the `ai:final-ci-failed:v1` hand-back marker and a pipeline-posted `ai-review/head-gate` success. The existing "Integration post-sync test repair" step in `review_autofix.yml`, until now used only for `orchestrator/project-*` branches, runs the pre-push test gate against the PR's merge-base and, on failures the head caused, makes one `[ai-merge-resolve] repair final CI failures` commit with `scripts/review_conflict_resolve.sh --test-repair <result.json> --final-ci`. The repair head then gets its own review and final CI run. A failure the gate cannot reproduce, a repair that changes nothing, or one that leaves failures falls back to the full review in the same run.

| The numbers that matter | Value |
| --- | --- |
| Repairs per head | 1 |
| Repairs per PR before the full review round takes over | 2 |
| Extra API calls | 1 `GET commits/<head>/statuses` per selected run, 1 `GET pulls/<n>` and 1 comment POST per repair |
| New repo variable | `FINAL_CI_REPAIR_ENABLED` (default `true`) |
| New marker | `<!-- ai:final-ci-repair:v1 pr=<n> from=<sha> repair=<sha> attempt=<k> -->` |

What this means for operators: approved PRs whose final CI fails are fixed by a small, targeted commit, and `FINAL_CI_REPAIR pr=<n> ... outcome=selected|committed|fallback` in the review log shows which path ran. Set `FINAL_CI_REPAIR_ENABLED=false` to send every failure to the full review round.

### For contributors

The repair prompt is `prompts/final-ci-test-repair.txt`. It has no intent fingerprints, so the resolver's fingerprint hard gate is skipped in `--final-ci` mode only; `check_resolver_diff.sh` and the allow-list still apply. Orchestrator integration heads keep the `post_sync_test_repair` path unchanged.

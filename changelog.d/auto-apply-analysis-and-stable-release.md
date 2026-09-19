<!-- changelog: added -->
- **`main` promotes itself to `stable` once a day, and only after it has been proven end to end.** A `BLOCKED` implementation no longer costs credits, and a patch merged into the `stable` branch releases itself.

`promote-main-to-stable.yml` now runs on a daily schedule (`0 0 * * *`). Each tick checks for a code change on `main` since the `stable` tag (analysis docs, `docs/`, `CHANGELOG.md` and most `*.md` do not count; `CLAUDE.md` and `.claude/` do), runs `test-and-mark-stable.yml` in the new `gate_only` mode on `main` as a smoke gate, then hands one `analysis/workflow-optimization-*.md` doc to the orchestrator as a proving run. When that run merges, the orchestrator poller dispatches a second doc as a verifying run on top of it. When the verifying run is ready to merge, the poller confirms nothing untested landed since the smoke-tested commit, fast-forwards `stable` to exactly the proving merge (`target_sha`), runs the full release gate, and holds the verifying merge until the release finishes so `main` HEAD is the version under test. A tick that finds a cycle in flight, an earlier cycle for the same tip, or fewer than two analysis docs does nothing; a failed tip is not retried until `main` moves again. `orchestrate.yml` gained optional `tracking_labels` and `tracking_comment` inputs so a dispatcher can bind the project it starts. Separately, `auto-release-stable.yml` checks every 6 hours whether the `stable` branch is ahead of the `stable` tag and dispatches the release gate when it is. And when the implementer answers a deliberate `BLOCKED:` verdict, `implement.yml` parks the issue in `ai:blocked` instead of `ai:awaiting-approval`, so stall recovery stops re-running a plan that cannot succeed.

| The numbers that matter | Value |
| --- | --- |
| Promote cycle cadence | daily, `0 0 * * *` |
| Orchestrator runs per promotion | 2 (proving, verifying), 2 analysis docs |
| Release gates per promotion | 2 (`gate_only` smoke gate on `main`, full gate on `stable`) |
| `stable`-branch release check | every 6 hours |
| Merge hold cap while the release runs | 21600 s (`COMPREHENSIVE_PROMOTION_HOLD_MAX_SECS`) |
| Kill switches | `PROMOTE_CYCLE_ENABLED`, `APPLY_ANALYSIS_ON_MAIN_ENABLED`, `AUTO_RELEASE_STABLE_ENABLED` |
| Implement runs saved per blocked issue | 3 of 4 (tele-funtoken-msg-scoring#4395 re-ran the same `BLOCKED` plan four times) |

What this means for operators: nobody dispatches a release any more. A code change on `main` reaches consumers after the next daily tick proves it, with the whole chain visible as tracking-issue comments and stable log prefixes; a `stable`-branch hotfix is tagged within six hours; and an issue that needs credentials or a product decision shows up as `ai:blocked` with the reason and resume instructions instead of a chain of identical failed runs. Manual `promote-main-to-stable.yml` dispatch still works as the override.

### For contributors

The proving and verifying runs are ordinary orchestrator projects labelled `ai:comprehensive-test-pending` with a marker comment (`apply-analysis-*` lines) that the poller reads. A doc carrying that marker on any tracking issue, or listed in `analysis/recommendation-processing-report.md`, is never dispatched again. Log prefixes: `PROMOTE_CYCLE_SKIPPED reason=…`, `PROMOTE_CYCLE_FAILED reason=…`, `PROMOTE_CYCLE_DISPATCHED`, `APPLY_ANALYSIS_*`, `COMPREHENSIVE_VERIFICATION_*`, `COMPREHENSIVE_PROMOTION_*`, `AUTO_RELEASE_*`, `IMPLEMENT_BLOCKED_TERMINALIZED`.

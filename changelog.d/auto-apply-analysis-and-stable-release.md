<!-- changelog: added -->
- **`main` promotes itself after a clean orchestrator run, `stable` releases itself, and a `BLOCKED` implementation stops costing credits.** Three pieces of human-in-the-loop are gone from the release and recovery paths.

Every push to `main` now runs `apply-analysis-on-main.yml`, which hands exactly one pending `analysis/workflow-optimization-*.md` doc (oldest first) to the orchestrator and labels the tracking issue `ai:comprehensive-test-pending`. When that project completes cleanly the poller's release callback dispatches `promote-main-to-stable.yml`, so a real end-to-end orchestrator run on the current `main` is what promotes it. With no doc, a project already in flight, or every remaining doc already dispatched, the push holds and nothing runs. Separately, `auto-release-stable.yml` checks every 6 hours whether the `stable` branch is ahead of the `stable` tag and dispatches `test-and-mark-stable.yml` when it is, so a patch merged straight into `stable` reaches consumers without an operator dispatch. And when the implementer answers a deliberate `BLOCKED:` verdict, `implement.yml` now parks the issue in `ai:blocked` instead of `ai:awaiting-approval`, which stops the poller's stall recovery from re-posting `/approved` on a plan that cannot succeed.

| The numbers that matter | Value |
| --- | --- |
| Analysis docs per dispatch | 1 (oldest first) |
| Release check cadence for `stable` | every 6 hours (`0 */6 * * *`) |
| Manual promotion | still available via `promote-main-to-stable.yml` |
| Kill switches (repo vars) | `APPLY_ANALYSIS_ON_MAIN_ENABLED`, `AUTO_RELEASE_STABLE_ENABLED` |
| Release callback target (repo vars) | `COMPREHENSIVE_RELEASE_WORKFLOW_FILE` (`promote-main-to-stable.yml`), `COMPREHENSIVE_RELEASE_WORKFLOW_REF` (`main`) |
| Implement runs saved per blocked issue | 3 of 4 (tele-funtoken-msg-scoring#4395 re-ran the same `BLOCKED` plan four times) |

What this means for operators: fixes merged to `main` reach consumer repos without anyone dispatching a release, provided an analysis doc exists to drive the proving run; a branch-`stable` hotfix is tagged within six hours; and an issue that needs credentials or a product decision shows up as `ai:blocked` with the reason and the resume instructions in a comment, instead of a chain of identical failed runs.

### For contributors

The dispatcher never re-dispatches a doc: it records `apply-analysis-source-doc: <path>` as a comment on the tracking issue and searches tracking issues for that marker before choosing a doc, and it also skips docs already listed in `analysis/recommendation-processing-report.md`. A doc whose project failed, or merged without deleting it, is therefore a human decision. The scheduled release skips while a gate run is queued or running and after the gate failed on the current `stable` tip. Log prefixes: `APPLY_ANALYSIS_SKIPPED reason=…`, `APPLY_ANALYSIS_DISPATCHED`, `AUTO_RELEASE_SKIPPED reason=…`, `AUTO_RELEASE_DISPATCHED`, `IMPLEMENT_BLOCKED_TERMINALIZED`.

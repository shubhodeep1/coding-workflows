<!-- changelog: fixed -->
- **The review editor no longer throws away a correct fix because it miscopied a reviewer file's sha256, and the release gate's editor retry now waits on the review run that is already queued instead of queueing a new one behind it.**

The nightly promote cycle's smoke gate (`Test & Mark Stable Release` run 35802596362) failed at Phase 4b because the editor restored `tests/e2e_smoke_canary.txt` correctly on all three attempts, yet `scripts/review_apply_fixes.sh` rejected each attempt: the model had copied one reviewer file's 64-character hash wrong each time, and the run then discarded the edit. A missing or wrong hash now logs `EDITOR_REVIEWER_CHECKSUM_UNVERIFIED` and the attempt continues. A reviewer file with no entry, or with no issue-audit counts, still fails the attempt. Phase 4b's retry in `.github/workflows/test-and-mark-stable.yml` also stopped dispatching a new review run when one is already queued or running for the bait commit, since the new dispatch shared that run's concurrency group and sat pending past its 25-minute budget.

| The numbers that matter | Value |
| --- | --- |
| Review runs since 2026-09-20 that reached the editor | 16 |
| Of those, runs with at least one attempt rejected only for a checksum | 6 |
| Of those, runs that lost the edit on every attempt | 1 (the smoke gate) |
| Retry dispatch wait on run 35802596362 | 41 minutes pending, against a 25-minute budget |

What this means for operators: review runs finish in fewer editor attempts, and a failed first canary check in the release gate can now actually recover. Watch for `EDITOR_REVIEWER_CHECKSUM_UNVERIFIED` warnings if you want to track how often the model miscopies hashes.

### For contributors

Issue #4305's heal traced this failure to the reviewer-majority shortcut that PR #4306 removed; on run 35802596362 Phase 4 had in fact waited for the review run to complete, so that change did not cover it. The retry still has the same 25-minute budget (`EDITOR_RETRY_BUDGET_MINUTES`), and a full review pipeline can take longer, because the job's serial budget is already 295 of its 300 minutes.

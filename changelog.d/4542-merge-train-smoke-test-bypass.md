<!-- changelog: fixed -->
- **The merge-train gate no longer queues release smoke-test PRs behind overlapping older PRs.** A smoke PR whose changed paths overlapped an older open `ai/issue-*` PR was queued (`ai:merge-queued`) and soft-exited like any other PR, which skipped the reviewer/editor path the smoke test exists to exercise.

`Test & Mark Stable Release` failed on `stable` (issue #4542) because smoke PR #4540 hit `scripts/review_merge_train.sh`'s ordinary overlapping-PR queue rule: its files overlapped an older open `ai/issue-*` PR on the same base, so the gate labelled it `ai:merge-queued`, set `AUTOFIX_STALE_BASE_SKIP=true`, and the run soft-exited without touching the canary. The release gate's Phase 4b then treated the queued run's successful conclusion as a completed autofix and asserted the canary bait marker was removed, which it never had the chance to be. `scripts/review_merge_train.sh::_mt_gate` now checks `IS_SMOKE_TEST` — already exported by `review_autofix.yml`'s "Detect smoke test and tune LLM settings" step, which runs before the gate — and lets a smoke PR proceed unqueued and unlabelled, with no file-overlap API calls spent. Ordinary PRs still queue exactly as before.

| The numbers that matter | Value |
| --- | --- |
| Failing step | `Test & Mark Stable Release` → Phase 4b (canary verification) |
| API calls spent on a smoke-PR bypass | 0 (was: 1 pulls list + per-older-PR files calls + 1 label + 1 comment) |

What this means for operators: the release smoke test no longer fails when an unrelated older `ai/issue-*` PR happens to touch the same canary path at release time; the smoke PR always gets a real reviewer/editor pass. Ordinary `ai/issue-*` PRs keep queuing behind older overlapping PRs exactly as before.

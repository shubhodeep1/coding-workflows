<!-- changelog: changed -->
- **The unblock scan no longer sends E2E fixture issues to the unblock judge.** `scripts/unblock_scan.py select` skips any item whose title matches the E2E test-title pattern (`[E2E Smoke Test`, `[E2E Orchestrate Smoke`, `[E2E Clarify Negative Test`, case-insensitive; the same pattern as `_SMOKE_TITLE_RE` in `scripts/workflow_failure_heal.py`) and counts it as `e2e_test_item`. These issues, such as the clarify negative test, are blocked by design, so judging them only invented work. The poller's existing `search/issues` call in `run_unblock_scan` now keeps each item's `title` (no new API call); the scan falls back to a `title` in the details entry, and a missing title never skips. Set `UNBLOCK_SKIP_E2E_TEST_ITEMS_ENABLED=false` to turn the skip off; any value other than unset, empty or `true` also turns it off and logs `UNBLOCK_SCAN warning=invalid_flag` on stderr.

Refs #9782.
Refs #9792.

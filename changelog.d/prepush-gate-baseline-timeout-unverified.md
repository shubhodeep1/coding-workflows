<!-- changelog: fixed -->
- **The pre-push test gate no longer blames a change for a test whose baseline re-run ran out of time.** Such a test is now reported as unverified, so implement skips the repair turn and the second gate run it used to trigger.

The gate re-runs failing tests on the merge-base to tell failures the change caused from ones it inherited. When a slow test file used most of the 15-minute budget, its baseline re-run got only the time left, was stopped, and the gate counted the test as caused by the change. Implement then ran a repair turn and a second full gate run, about 30 minutes of gate time for a one-line edit. That pushed the E2E smoke test's implement phase past its 30-minute inactivity limit, which failed the release gate on 2026-10-28 (run 40608957733) and twice on 2026-10-29 (runs 40831286935 and 40876853022, release v1.40.3). The gate now reports these tests as `unverified` with reason `baseline_timeout` (or `baseline_error` when the re-run could not start).

| The numbers that matter | Value |
| --- | --- |
| Gate time on the smoke canary edit, before | about 30 minutes (two 15-minute runs plus a repair turn) |
| Gate time on the smoke canary edit, after | one run, at most 15 minutes |
| Slow test file behind it | `tests/test_implement_post_codex_recovery.py`, about 13 minutes in the gate sandbox, failing on both trees |
| New `unverified` reasons | `baseline_timeout`, `baseline_error` |

What this means for operators: a gate summary may now list a test as unverified with `baseline_timeout` where it used to list it under "Failures caused by this change". The implement and review-side gate steps start a repair only for failures caused by the change, so neither starts one for it. Like any unverified result, it does not hold the push either: CI on the pushed commit still runs the test in full.

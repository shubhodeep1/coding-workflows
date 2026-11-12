<!-- changelog: fixed -->
- **The `REVIEW_HEAD_CHECKOUT` summary line in AI Review & Autofix reports real `fetch_ms` and `switch_ms` values on ubuntu-26.04 runners again.** The "Checkout PR head branch" step now reads bash's `EPOCHREALTIME` clock instead of `date +%s%3N`.

On the `ubuntu-26.04` image, `date +%s%3N` (Rust uutils coreutils) can return a smaller value than an earlier call in the same job. When that happened between the start and end of the branch switch, the elapsed time came out negative and `scripts/review_autofix_step_checkout_pr_head_branch.sh` printed `switch_ms=na`. That made `tests/test_review_checkout_head_diagnostics.py` fail intermittently in this repo's CI on `main`. The step's behaviour and log format are unchanged. Only the clock source moved, and `date` stays as the fallback for shells without `EPOCHREALTIME`.

What this means for operators: `REVIEW_HEAD_CHECKOUT` timings are trustworthy again on ubuntu-26.04, and PR CI no longer fails at random on the checkout diagnostics test.

### For contributors

The other elapsed-time helpers that still read `date +%s%3N` (semble install, build and query timings, `_mt_now_ms` in `scripts/review_merge_train.sh`, `_resolver_terminal_now_ms` in `scripts/review_conflict_resolve.sh`) are tracked in #9377.

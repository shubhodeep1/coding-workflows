<!-- changelog: fixed -->
- **A pull request stopped by a non-retryable conflict-resolver failure now gets one more review run after the review support commit changes.** Issue #6911, from the activation check of PR #6597.

The review gate stops a head on its first non-retryable resolver failure (`conflict_resolver_sandbox_path_host_only`, `…_unsupported`, `…_support_missing`). Before this change the stop held until someone pushed to the pull request, even after a resolver fix landed on `main`. The gate now allows one more run on that head when its verified review-support commit is one no earlier failure on the head ran with:
- Every failure marker and cap marker records the support commit as `support=<sha>`. Older markers without the field count as a different commit.
- The gate logs `AUTOFIX_FINGERPRINT_CAP_REARMED pr=<n> head=<sha> fp=<fp> reason=<r> prior_support=<sha|none> support=<sha>` and lets the scheduled sweep's dispatch run.
- If that run fails again, its own marker carries the commit, so the next dispatch is stopped again.
- At most 3 different support commits can re-run one head. A missing or malformed support commit never re-runs it.

| The numbers that matter | Value |
| --- | --- |
| Extra runs per head per new support commit | 1 |
| Support commits that can re-run one head | at most 3 |
| Failure reasons that can be re-run | 3 (the non-retryable resolver reasons; the 3-strikes cap is unchanged) |
| New GitHub API calls | 0 |

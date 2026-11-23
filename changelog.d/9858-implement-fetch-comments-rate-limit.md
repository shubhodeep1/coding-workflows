<!-- changelog: fixed -->
- **Implement now waits out a GitHub rate limit when it reads the issue comments.** The `Fetch issue comments` step of `implement.yml` reads the comments through the `gh_api_retry` helper, which waits for the rate-limit reset GitHub reports (up to `GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS`, 600 s by default). Before, the step retried five times with short fixed sleeps, about 30 seconds in total, and failed the whole implement run when the shared `GH_PAT` quota was exhausted for longer (consumer run 44848177993 failed with a reset 553 s away).

The step's own five attempts are kept, because the approval comment can take a moment to appear. The retry helper's `GH_API_RETRY` lines now reach the job log instead of being discarded. When no read succeeded, the step first prints `::error::Unable to read issue comments (GitHub API unavailable or rate-limited).`, so a failed read is no longer reported only as a missing approval comment; the existing `Unable to fetch triggering approval comment from issue comments.` line is unchanged.

Refs #9858.

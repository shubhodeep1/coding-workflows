<!-- changelog: added -->
- **A model-provider outage no longer blocks every open PR; review pauses once and resumes by itself.**

When OpenRouter runs out of credits, rejects the key, or keeps failing with 429/5xx, a review/autofix failure is now recorded as `provider_unavailable`, but only after a live probe confirms that the provider is down. Such failures do not count toward the identical-failure cap. They also apply no `ai:review-blocked` label and file no heal issue. Instead, the repository gets one `ai:provider-outage` tracker issue and one alert.

The 30-minute review sweep (the hourly `ai-review.yml` schedule in consumer repos) then skips review dispatches until the probe succeeds. Once it does, the sweep re-dispatches the affected PRs, removes only the outage labels it marked, closes the tracker and sends one "recovered" alert. A Claude pool with every account at its usage gate gets its own single capacity alert and never pauses reviews.

What this means for operators: after a credit outage like the one on 2026-09-30, topping up the provider is the only manual step. Re-running stable release runs that failed on the outage stays off by default; set `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED=true` to turn it on.

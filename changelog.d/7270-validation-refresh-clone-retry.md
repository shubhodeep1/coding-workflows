<!-- changelog: fixed -->
- **A single transient GitHub error while cloning a consumer repo no longer fails the stable release.**

`scripts/validation_refresh_runner.py` clones every repo in `.github/ai/consumer_repos.json` with `gh repo clone`, and a failed clone is an `error` outcome that fails the Validation Refresh run. The release gate's `orphan-workflows-test` job in `.github/workflows/test-and-mark-stable.yml` watches that run, so release run 38253656098 failed when one clone of `drhyg_ecommerce_automation` got "GraphQL: Something went wrong while executing your query". The runner now tries a clone up to 3 times, waiting 5 then 15 seconds, and logs `VALIDATION_REFRESH_CLONE_RETRY` before each retry. A clone that fails every attempt still reports `error` and still blocks the release.

What this means for operators: stable releases stop failing on one-off GitHub API errors during the consumer refresh, while a consumer repo that really cannot be cloned still blocks the release as before.

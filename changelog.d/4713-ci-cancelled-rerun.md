<!-- changelog: fixed -->
- **Cancelled CI no longer leaves a PR stuck indefinitely.** The 30-minute review sweep re-runs failed jobs once for an eligible cancelled or startup-failed `ci.yml` run on the PR's current head. Real test failures remain with the fixer; disable recovery with `CI_CANCELLED_AUTO_RERUN_ENABLED=false`.

<!-- changelog: fixed -->
- **The poller no longer re-dispatches review/autofix on every tick for a PR the identical-failure cap has already stopped.** The "Noop-suspicious recovery: re-dispatched review_autofix for PR #N (retry 3/3)" Telegram WARNING no longer repeats on every cycle.

The noop-suspicious recovery sweep in `scripts/orchestrate_poll_process.sh` counts `⚠️ **Editor no-op suspicious**` comments and re-dispatches `review_autofix.yml` until the count reaches 3. When the identical-failure fingerprint cap has already stopped a PR's head, the `gate` job ends every such dispatch before any job that could post a new warning. The count therefore stayed below 3, and the sweep dispatched a run and sent a WARNING on every poll cycle. PR #4332 got about ten of these on 2026-09-24 alone. The sweep now checks the comments and commits it already fetches: if the current head has a `review-autofix-failure-cap:v1` comment from the `GH_PAT` account, it logs `NOOP_RECOVERY_SKIP_FINGERPRINT_CAP` and skips both the dispatch and the alert.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls | at most 1 `GET /user` per poll cycle, only when a cap marker is present |
| Reset | any push (new head SHA) |
| Fallback | an unresolvable head SHA or token identity keeps the old re-dispatch |

What this means for operators: a PR stopped by the fingerprint cap stays quiet until someone pushes to it or the review-blocked judge acts, instead of costing a workflow run and a Telegram WARNING on every tick. The cycle summary line `Noop-suspicious recovery skipped (fingerprint cap already applied on head): N.` shows how many PRs were held back.

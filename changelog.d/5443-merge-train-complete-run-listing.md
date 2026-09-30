<!-- changelog: security -->
- **The merge train no longer releases a queued PR beside a review run it could not see.** Before it re-dispatches review for an `ai:merge-queued` PR, the `release` step now reads every active run, recognises review runs whose path carries an `@<ref>` suffix, and leaves the PR queued when it cannot read them all.

`_mt_inflight_review_branches` in `scripts/review_merge_train.sh` tells `release` which queued PRs already have a review running. It used to read one page of the newest 100 workflow runs of every workflow, matched the review workflows with a regex that a path such as `.github/workflows/ai-review.yml@refs/heads/main` fails, and released without the check when the lookup failed. Any of the three let the train dispatch a second review beside a pending one (security finding `merge-train-drops-ref-suffixed-run-paths`, issue #5443). The listing now reads runs in each active status (`pending`, `queued`, `in_progress`) 100 at a time until it has read every run the listing reports, and strips an `@<ref>` suffix before matching `review_autofix`, `internal-review`, or `ai-review`. Each call after the first asks for runs created at or before the oldest run already read (`created=<=<timestamp>`) instead of the next offset page, so a run that finishes or starts mid-read cannot push a still-active review run past the pages read. When the listing is incomplete (a failed or malformed page, a listing that shifted while being read, or more runs than 10 calls read), every queued PR stays queued and the next `cancel_on_pr_close.yml` event or `orchestrate_poll.yml` tick retries.

| The numbers that matter | Value |
| --- | --- |
| `internal-review.yml` dispatches in coding-workflows, 2026-09-29 | 153 in 5 hours |
| Call cap per status | 10 calls of up to 100 runs (at most 991 distinct runs, since each bounded call re-reads at least the oldest run before it) |
| API calls per release with a queued PR | 3 (one page per status), up from 1 |
| API calls per release with nothing queued | 0, down from 1 |

What this means for operators: a queued PR may now wait one more close event or poll tick when the Actions API is slow or failing, instead of getting a duplicate review run. Search the release logs for `MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=<N>` and `MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=<…>` to see why a PR was held back. `MERGE_TRAIN_ENABLED=false` still turns the whole train off.

### For contributors

`_mt_inflight_review_branches` keeps its name and output (one sorted key per line: a head branch, or `pr:<N>` for a PR-named dispatch run) and returns 1 on an incomplete listing. `_mt_release` reads it once per invocation, lazily, the first time a queued PR passes the base filter. The same `.path` regex in `scripts/gh_helpers.sh` (`autofix_retrigger_has_inflight_peer`, `autofix_changes_lost_head_retry_consumed`) is unchanged.

<!-- changelog: fixed -->
- **The merge train's per-run file-list cache now actually caches, and `curl_gh_api` honours `Retry-After` on secondary rate limits.** Both changes reduce how much of the shared GitHub API budget a single poll tick or review run spends, following the rate-limit alert fired from tele-funtoken-msg-scoring AI Review run 34168241128.

`scripts/review_merge_train.sh` documents a per-run cache of each PR's changed-file list so that a `release` tick evaluating several queued PRs against the same older set fetches every list once. That cache never held: every caller invoked `_mt_pr_files` through a `$(...)` command substitution, which runs the function in a subshell and discards the cache write on return. With `MERGE_TRAIN_MAX_OLDER_PRS` at its default of 20, a tick with Q queued PRs could issue up to Q × 21 paginated `GET /pulls/{n}/files` calls where the number of distinct PRs would do. The file-list, own-files and blocker helpers now have output-variable forms (`_mt_pr_files_into`, `_mt_own_files_into`, `_mt_blockers_for_into`) that run in the caller's shell, and the gate and release paths use them; the original printing names are kept and still fill the cache when called directly.

Separately, the rate-limit branch of `curl_gh_api` in `scripts/gh_helpers.sh` computed its wait only from `X-RateLimit-Reset`, the primary-window reset. GitHub answers secondary rate limits with `Retry-After` in seconds, and the primary reset on that same response can be up to an hour away, so the helper slept the full 600 s cap where GitHub had asked for a few seconds. `_parse_reset_header` now prefers a numeric `Retry-After`, falls back to `X-RateLimit-Reset`, and always returns 0 so it is safe under `set -euo pipefail` outside an `if` / `||` context.

| The numbers that matter | Value |
| --- | --- |
| `GET /pulls/{n}/files` calls per merge-train `release` tick | up to Q × (1 + `MERGE_TRAIN_MAX_OLDER_PRS`) → one per distinct PR |
| Wait on a secondary-limit 403 | up to 600 s → the `Retry-After` value (cap, floor and 30 s fallback unchanged) |
| Test files added to `ci.yml` | `tests/test_review_merge_train.py`, `tests/test_gh_helpers_parse_reset_header.py` |
| Triggering run | tele-funtoken-msg-scoring AI Review run 34168241128, rate limit at 00:32:43Z with the reset 24 s out |

What this means for operators: nothing to configure. Merge-train release ticks in `orchestrate_poll.yml` and `cancel_on_pr_close.yml` spend fewer API calls per queued PR, and a secondary-limit 403 inside any `curl_gh_api` caller resumes after the seconds GitHub asked for instead of ten minutes. Consumer repos pick both up on the next `@stable` sync.

### For contributors

The regression test `test_release_fetches_each_pr_file_list_once_per_run` drives the real script through the fake `gh` and asserts each `/pulls/{n}/files` path appears exactly once in the call log; it fails on the previous code with two fetches of the shared older PR. New callers inside `review_merge_train.sh` must use the `_into` forms; the comment above `_mt_pr_files_into` explains why the `$(...)` form silently defeats the cache. `_parse_reset_header` ignores a non-numeric `Retry-After` (the HTTP-date form) and falls through to `X-RateLimit-Reset`; the `gh_retry` family is unchanged and still reads the reset from `GET /rate_limit`.

<!-- changelog: fixed -->
- **The review-blocked judge sees a PR's comments again when they add up to more than 128 KiB.** Before, it decided with no comments, editor summaries or review comments, and nothing said so.

`_gh_pr_with_all_comments_rest` in `scripts/gh_helpers.sh` built the judge's PR context with `jq --argjson`, which passes each JSON value as one command-line argument. Linux caps one argument at 131,072 bytes, so once a PR's issue comments serialized past that, `jq` failed with `Argument list too long` (exit 126) and printed nothing. The judge then used an empty context. This happened on tele-funtoken-msg-scoring PR #3933 (168,184 bytes of comments, run 39131897079): all four judge decisions on 2026-10-18 said the PR had no editor summaries, though it had several.

What changed:

- The helper, the judge's inline fallback in `scripts/review_rb_judge.sh` and the poller's copy of the judge context in `scripts/orchestrate_poll_process.sh` now pass this JSON to `jq` through `--slurpfile` with process substitution, so its size no longer matters. The output is unchanged. An empty input now gives `{}` or `[]` instead of failing.
- The poller's check that clears the judge's isolation latch reads the issue's comment history the same way. Before, a long history made that check fail quietly and the latch was never cleared.
- When the context still cannot be assembled, both judges log one `::warning::RB_JUDGE_CONTEXT_EMPTY pr=<n> reason=assembly_failed|invalid_shape` line. Look for it in the run log when a judge decision ignores comments the PR has.

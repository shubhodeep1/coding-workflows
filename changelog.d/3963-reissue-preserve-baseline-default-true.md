<!-- changelog: changed -->
- **A review-blocked judge `spot-fix` verdict now keeps the closed PR's work by default.** `REISSUE_PRESERVE_BASELINE_ENABLED` defaults to `true` instead of `false`.

When the review-blocked judge closes a PR with `close_and_reissue` and asks for `reissue_mode: spot-fix`, `scripts/review_rb_judge.sh` now pushes the closed PR's head to an `ai/reissue-baseline/pr-<n>-<sha12>-<run>-<attempt>` branch and records `prior_pr_baseline_branch` and `files_touched` in the replacement issue, and `implement.yml` starts the next implement run from that branch. Before this change the same verdict was silently downgraded to `redo` unless a repo had set the variable itself: on fun-token-multi-chain run 33600594555 the judge asked to keep 16 commits of dependency, documentation, and regression-test work from PR #454, logged `REISSUE_BASELINE_DISCARDED requested=spot-fix reason=feature_flag_disabled`, and replacement issue #455 shipped with no baseline branch. Repos that want the old behaviour set `REISSUE_PRESERVE_BASELINE_ENABLED=false`.

| The numbers that matter | Value |
| --- | --- |
| Default before / after | `false` / `true` |
| Workflow fallbacks flipped | 3 (`review_autofix.yml` judge step, `implement.yml` job env and baseline resolver) |
| Flag introduced | 2026-08-19 (commit 6d08bfc), bake-out flip never landed |

What this means for operators: nothing to configure. A `spot-fix` reissue no longer throws away the closed PR's work, and the implement side keeps its guards (trusted issue author, exact branch format, PR number and head-SHA checks, fail-open to the base branch), so a bad baseline costs one implement run rather than a bad merge. Set the variable to `false` on a repo to force every reissue back to `redo`.

### For contributors

The script-level `:-false` fallbacks in `scripts/review_rb_judge.sh` and the resolver's Python default in `implement.yml` were flipped alongside the workflow defaults so a missing env var behaves the same everywhere. `test_review_autofix_wires_reissue_preserve_baseline_flag_default_false` is now `..._default_true`. The three sibling Phase flags (`JUDGE_INTERIM_ENABLED`, `CONSOLIDATOR_REJECT_SCHEMA_ENABLED`, `BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED`) still default to `false`.

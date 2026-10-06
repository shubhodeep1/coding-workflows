<!-- changelog: changed -->
- **The review panel drops `z-ai/glm-5.2` for `mistralai/mistral-small-2603`, every review tier now draws its reviewers at random from all six panel models, and a `claude/**` push with no PR is no longer reviewed.** Small and mid-sized PRs can now get any panel model, not just a fixed four.

`mistralai/mistral-small-2603` takes glm-5.2's slot (#2) in `REVIEWER_MODELS` in `.github/workflows/review_autofix.yml`, and in `opencode-live-smoke.yml` to match. `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` now defaults to empty. With that, the `standard` tier (diffs of 200 lines or fewer) draws 4 reviewers and the `lite` tier (50 lines or fewer, no protected path) draws 1 from the whole panel. The draw is seeded by the PR number, so a PR keeps the same reviewers on every autofix round. Before, `standard` always ran minimax-m3, deepseek-v4-pro, qwen3.7-plus and gpt-6-luna, and gemini-3.1-flash-lite and glm-5.2 ran only on larger PRs. `internal-review.yml` loses its `push: claude/**` trigger and the `resolve-claude-branch-pr` / `review-claude-branch-push` jobs, so a `claude/**` branch is reviewed once it has a PR, like every other branch.

| The numbers that matter (OpenRouter `/activity` and editor audit comments, Sep 5 to Oct 5) | Value |
| --- | --- |
| glm-5.2 reviewer spend | $2,250 ($2.13 per review run, $12/M output tokens) |
| mistral-small-2603 reviewer spend, Sep 5 to 21 | $65 ($0.04 per review run, $0.60/M output tokens) |
| Applied fixes no other reviewer in the same run matched | glm-5.2: 37 in 1,058 runs; mistral-small: 187 in 1,558 runs |
| Runs where mistral-small produced findings | 59% of 1,279 (minimax-m3: 63%, deepseek-v4-pro: 53%) |
| Panel size by tier (unchanged) | lite 1, standard 4, full 6 |

What this means for operators: review spend per run drops, and every panel model now sees small and mid-sized PRs. A repo that sets `vars.REVIEW_TIER_STANDARD_REVIEWER_SLUGS` keeps its pinned list, and a list naming `z-ai/glm-5.2` now fails open to the full panel with a warning, because glm is no longer on the panel. Mistral's context window is 262K tokens, against 1M for the rest of the panel. A context overflow fails its slot on standard/full tiers; if Mistral was the only lite reviewer and was skipped (`skipped_unmapped` or `skipped_open`) or reported a context overflow, the round retries with the live `openai/gpt-6-luna` slot unless its circuit breaker is open. Without that model, with its circuit breaker open, or if the retry fails, the lite round fails rather than silently passing without a reviewer. Mistral has no same-family failback chain. `CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS` is no longer read.

### For contributors

The `claude-branch-review` mode stays in `review_autofix.yml` behind the `force_claude_branch_review` input, with nothing calling it. `tests/test_internal_review_push_pr_grace.py` and its `ci.yml` step are removed with the jobs they tested. The glm-5.2 catalog entry and failback chain stay so a repo can still opt back in. The merged-PR guard asks for confirmation when an `env`-wrapped commit's Git directory cannot be resolved, rather than checking the session checkout's PR history.

<!-- changelog: fixed -->
- **A push to a `claude/**` branch no longer gets a second full reviewer run when its PR opens a few seconds later.**

Claude sessions push a new branch and open its pull request right after, a median of 20 seconds later. The push route of `.github/workflows/internal-review.yml` (`resolve-claude-branch-pr`) looked for an open PR once, at push time, found none, and started the no-PR reviewer panel. The PR's own `pull_request: opened` run then reviewed the same commit again. The step now re-checks every 60 seconds for up to `CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS` (default 300) and skips as soon as a PR appears. A branch that still has no PR when the window ends is reviewed as before, up to five minutes later.

| The numbers that matter | Value |
| --- | --- |
| Same-commit double reviews, 2026-09-23 to 09-30 | 151 |
| PR opened within 60 s / 90 s / 5 min of the push | 131 / 133 / 133 |
| glm-5.2 reviewer tokens spent on the push-leg duplicates | about 206M of 2,295M (9%) |
| New repo variable | `CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS`, default `300`, `0` restores the single lookup |

What this means for operators: every reviewer model on the panel runs about once less for each Claude branch that gets a PR within five minutes. A branch that never gets a PR is reviewed up to five minutes later than before. Only this repository is affected, since consumer repos have no push-to-`claude/**` review route.

### For contributors

Values that are not an integer from 0 to 3600 fall back to 300 with a `::warning::`. A failed PR lookup counts as "no PR yet" and is logged as `RESOLVE_CLAUDE_BRANCH_PR_LOOKUP_FAILED` with gh's error text (`error="..."`, one line, at most 200 characters; a `GH_PAT` rate limit shows up here), so the review still runs once the window ends. A newer push to the same branch cancels the waiting run through the existing push concurrency group. `tests/test_internal_review_push_pr_grace.py` runs the step's real shell body against stubbed `gh` and `sleep`.

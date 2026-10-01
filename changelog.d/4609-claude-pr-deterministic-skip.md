<!-- changelog: fixed -->
- **Doc-only and small-diff `claude/*` pull requests now skip review and auto-merge, like every other PR.** They no longer go through the reviewer panel and the Claude hand-off loop.

`review_autofix.yml` evaluated its deterministic pre-review skip only for PRs outside Claude-fixer mode, and every PR-backed `claude/*` head is in that mode. So a Claude PR that only touched docs still got the full reviewer panel and a hand-off per round. PR #4584, one docs file, spent two review rounds on date nits and ended on hold. Now `claude/*` PRs take the same doc-only (`AUTOFIX_SKIP_DOC_ONLY`) and small-diff (`AUTOFIX_SKIP_MAX_ADDITIONS` / `AUTOFIX_SKIP_MAX_DELETIONS`) routes, through the `deterministic-skip-merge` job: `ai:review-skipped` plus auto-merge bound to the gate-observed head. The existing guards still apply: protected paths, merge conflicts, incomplete `/files` evidence, and the `force-review` override all force a review. An accepted `claude_fixer_converged_head` verification run is still always reviewed, as #4453 requires. A head that already has a Claude hand-off never skips either: a `reopened` or `ready_for_review` event on it runs the reviewer panel, so posted findings are never merged past.

| The numbers that matter | Value |
| --- | --- |
| Skip routes open to `claude/*` PRs | doc-only and small-diff (was none) |
| Small-diff bound | 10 additions and 10 deletions by default, unchanged |
| Repos affected | this repo and the 13 consumers in `.github/ai/consumer_repos.json`, on the next `@stable` sync |

What this means for operators: a Claude session's docs-only or tiny PR now merges once its checks pass, with no review round and no fixer session. To force a review on one, add the `force-review` label or put `[force-review]` in the title. A PR whose current head already has a hand-off still waits for its Claude session (`claude_fixer_awaiting_session`) and is evaluated for the skip on its next push.

### For contributors

The gate condition moved from `CLAUDE_FIXER != true` to `CLAUDE_FIXER_VERIFY != true`. `tests/test_review_autofix_claude_fixer_mode.py` covers the doc-only and small-diff skips on `claude/*` heads, the protected-path and size guards, the pending hand-off wait on dispatch and on same-head `pull_request` events, the fail-closed comment lookup, and a verified convergence run on a docs-only head that still reviews.

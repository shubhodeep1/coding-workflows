<!-- changelog: fixed -->
- **The reviewer consensus summariser no longer fails a review run by trying to open reviewer files it cannot read.** Its prompt now says every reviewer output is inlined, and that it must not call tools.

`scripts/summarize_reviewer_consensus.sh` used to tell `openai/gpt-6-luna` that the full reviewer outputs "remain on disk" under `/tmp/codex-pr-…/previous_reviews/`. When an inlined input looked incomplete (one line of narration, or a failure notice such as `Reviewer minimax/minimax-m3 failed after non-retryable error on attempt 1.`), the model tried to `Read` those files. OpenCode's reviewer config has no `external_directory` rule, so each read was rejected and the session ended with no text. The script then used all 10 attempts and failed the `Run reviewer models` step. The prompt no longer names the on-disk path. In the no-PR `claude-branch-review` mode, the `Telegram failure` alert from `review_autofix.yml` now shows `Branch: <head ref> (no PR)` instead of `PR: …/pull/` with no number.

| The numbers that matter | Value |
| --- | --- |
| Runs that failed this way | 6 (36518137244, 36545663590, 36573586774, 36598991686, 36656409877, 36666750539) |
| Rejected reads per failed attempt | 3 (one per reviewer file) |
| Time spent per failed run | 10 attempts, about 43 minutes of backoff |

What this means for operators: `claude/*` branch pushes without a PR should stop producing "PR autofix failed" alerts caused by an empty summariser. When one of those alerts does fire, it names the branch.

### For contributors

The retry loop and the reviewer-role OpenCode permissions are unchanged. The `failed after retries` input filter still misses today's reviewer failure notices. It was left alone because the consensus ledger feeds the Claude-fixer hand-off, and dropping failed reviewers would change which `FINDINGS FROM` blocks it sees. Test: `tests/test_summarize_reviewer_consensus_prompt.py`, run in the `Summariser sandbox-mode pin contract test` step of `ci.yml`.

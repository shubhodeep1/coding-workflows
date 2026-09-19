<!-- changelog: fixed -->
- **The wave judge no longer overflows codex-cli's prompt cap when a merged PR commits minified bundles.** Every PR diff embedded in the judge prompt is now bounded by bytes, per PR and across the prompt, and the poller logs the assembled prompt size before the first codex exec of each judge phase.

Project #3928 on tele-funtoken-msg-scoring stopped advancing because `AI Orchestrate Poller` run 35425771769 raised `Orchestrator Judge failed for #3928. Manual review needed.` on a green job. Both judge attempts died before the model ran with `turn/start failed: Input exceeds the maximum length of 1048576 characters`. The prompt's merged-PR-diff block was "truncated" only by `head -n 500`, and two wave-5 PRs (#4416 and #4425, the committed Cloudflare Worker `dist` refreshes) carried 44-66 KB single-line bundles, so 500 lines still meant about 390 KB each and the whole prompt reached about 1.28 M characters. `scripts/orchestrate_poll_process.sh` now caps each diff at `JUDGE_PR_DIFF_MAX_BYTES` after the line cap, spends one shared `JUDGE_PR_DIFFS_TOTAL_MAX_BYTES` budget across the prompt (merged PRs first, in sorted issue order, so the cache-stable merged block never depends on an open PR's size), and prefixes every cut diff with a note that tells the judge to read the files directly. A failed byte truncation elides that diff rather than embedding the original payload, and a prompt that still exceeds the character limit bypasses attempts that cannot reach the model. Both knobs are declared in `orchestrate_poll.yml` as repo variables with defaults.

| The numbers that matter | Value |
| --- | --- |
| codex-cli `turn/start` stdin cap | 1,048,576 characters |
| Judge prompt in the failing run | ~1,279,000 characters |
| Largest single diff line | 66,064 bytes |
| PR #4416 / PR #4425 after the 500-line cap | ~391 KB / ~391 KB |
| `JUDGE_PR_DIFF_MAX_BYTES` default | 65536 bytes |
| `JUDGE_PR_DIFFS_TOTAL_MAX_BYTES` default | 524288 bytes |
| Affected poll run / tracking issue | 35425771769 / #3928 |

What this means for operators: a wave whose PRs commit large generated artifacts now reaches a judge verdict instead of a CRITICAL alert, with no operator action. The poll log prints byte and character counts before the first attempt and raises a workflow warning above 950,000 characters, so a future overshoot is diagnosable from the run log. A prompt above 1,048,576 characters fails immediately through the existing judge-failure path rather than repeating the same rejected request. Raise the two repo variables on repos whose judge needs more diff context; the API-call count is unchanged (one diff fetch per linked PR).

### For contributors

The truncation helper `_judge_truncate_pr_diff_file` mirrors `RB_JUDGE_PR_DIFF_MAX_BYTES` in `scripts/review_rb_judge.sh` (UTF-8-safe cut via python3, `head -c` fallback). The static prefix of the judge prompt (system instructions, README, agents.md, semble prefetch) is not capped by this change; on the failing run it was about 250 KB, which is why the default shared budget leaves roughly half the cap free.

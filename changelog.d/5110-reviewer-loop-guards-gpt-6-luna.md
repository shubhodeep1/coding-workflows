<!-- changelog: changed -->
- **The review panel drops `x-ai/grok-4.20` for `openai/gpt-6-luna`, swaps `google/gemini-3.1-flash-lite` for `google/gemini-3.8-flash`, and every reviewer attempt now has a hard 120-turn cap and a repeated-tool-call stop.** A reviewer can no longer loop on one tool call for hours.

Three `x-ai/grok-4.20` reviewer passes in `coding-workflows` looped on a single repeated tool call: 2,205 turns and 746M tokens (run 35949371968), 1,468 turns and 663M tokens (run 36483245451), and 234 turns and 123M tokens (run 36522631293). That came to about $311 in five days, and the first two ran until the 2-hour `HEARTBEAT_MAX_WALL` kill because every call looked like progress to the idle watchdog. Across 11,463 reviewer slots from Sep 22 to Sep 29 in `coding-workflows`, `digital_pa`, `fun-token-multi-chain`, and `tele-funtoken-msg-scoring`, real reviewer passes peaked at 101 turns. The watchdog in `scripts/review_run_reviewers.sh` now reads each attempt's OpenCode JSON event stream: an attempt that starts more than `REVIEWER_MAX_STEPS` turns is killed and its slot fails with no retry or failback, and `REVIEWER_TOOL_REPEAT_LIMIT` identical consecutive tool calls (same tool, same input) end the attempt as a retryable `tool_repeat` failure. Grok leaves every default roster: the six-reviewer panel, the `standard` review-tier default, and `opencode-live-smoke.yml`.

| The numbers that matter | Value |
| --- | --- |
| `REVIEWER_MAX_STEPS` default | 120 turns (highest real pass: 101) |
| `REVIEWER_TOOL_REPEAT_LIMIT` default | 10 identical consecutive calls |
| Largest Grok loop | 2,205 turns, 746M tokens |
| Grok 4.20 reviewer spend at OpenRouter list prices, Sep 22 to Sep 29 | $758 ($311 of it in the three loops) |
| Estimated cost per reviewer call, Grok 4.20 slot to `gpt-6-luna` | about $0.52 to about $0.07 |
| New failback chains | `gpt-6-luna -> gpt-5.6-luna`, `gemini-3.8-flash -> gemini-3.1-flash-lite` |

What this means for operators: a looping reviewer now costs at most one 120-turn attempt instead of up to two hours of turns, and the panel continues with the remaining reviewers. Override `vars.REVIEWER_MAX_STEPS` or `vars.REVIEWER_TOOL_REPEAT_LIMIT` per repo if needed. A repo whose `vars.REVIEW_TIER_STANDARD_REVIEWER_SLUGS` still names a Grok slug gets a full-panel review instead of the `standard` subset, because the resolver fails open when a slug is not on the panel; drop Grok from that variable to keep the three-reviewer tier. The Grok catalog and failback entries stay in place.

### For contributors

OpenCode's own agent `steps` setting is deliberately unused: in 1.18.23 it only injects a "maximum steps reached" instruction and keeps offering tools with `tool_choice: auto`, which a looping model ignores. The repeat check compares full tool inputs, so paged `read` calls on one file with different offsets never match. A check against the stderr permission log, which records only the path, would have stopped 24 normal attempts in the same week. The guards apply to the review panel only; the judge, consolidator, consensus summariser, and smoke reviewer-role callers are not capped. `google/gemini-3.8-flash` gets a new catalog entry.

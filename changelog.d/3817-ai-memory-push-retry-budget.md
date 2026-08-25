<!-- changelog: fixed -->
- **AI-memory pushes now survive orchestrator dispatch bursts: the default push-retry budget doubled from 8 to 16 attempts.**

Plan run 32849764877 failed before posting an implementation plan because the fail-closed `/answer` claim could not push to the shared `ai-memory` branch: all 8 attempts lost the ref-lock race against a dispatch burst that landed a foreign commit on the ref every 3-5 seconds for about 2 minutes. The 8-attempt loop lasts about 80 seconds, so it exhausted mid-burst and aborted the whole plan phase with `Failed to push memory branch after 8 attempts`. The default `AI_MEMORY_PUSH_RETRIES` is now 16 in `scripts/ai_memory.py`, the four shell callers that embed the same fallback (`review_rb_judge.sh`, `orchestrate_poll_process.sh`, `review_consolidate.sh`, `review_apply_fixes.sh`), and the workflow-log-analysis cache writer in `scripts/collect_workflow_logs.py`, stretching the jittered retry loop to roughly 3 minutes so it outlasts a burst of that shape. Explicit `AI_MEMORY_PUSH_RETRIES` overrides, the 8-second jitter cap, and the fail-closed claim semantics are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Default `AI_MEMORY_PUSH_RETRIES` | 8 → 16 |
| Observed burst push interval on `ai-memory` | every 3-5 s for ~2 min |
| Old retry-loop duration (max) | ~80 s |
| New retry-loop duration (mean/max) | ~3 min / ~3.7 min |

What this means for operators: a plan, clarify, or implement run dispatched inside a busy orchestrator wave no longer hard-fails its claim step just because sibling runs were writing run events to `ai-memory` at the same time; set `AI_MEMORY_PUSH_RETRIES` explicitly to restore the previous budget.

### For contributors

`tests/test_ai_memory_push_retry_backoff.py` gains a 15-rejection/16-budget regression test, and `tests/test_ai_memory_processed_command_entry.py` now pins the default at >= 16. The full-jitter backoff cap stays at 8 s because attempt frequency, not sleep length, wins ref-lock races; the larger budget only extends how long a contended claim keeps trying before giving up.

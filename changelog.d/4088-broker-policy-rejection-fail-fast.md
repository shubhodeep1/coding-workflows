<!-- changelog: fixed -->
- **The review editor stops retrying once the model-provider broker itself rejects its requests.** A deterministic HTTP 4xx from `scripts/model_provider_broker.py` no longer burns the remaining editor attempts and the capacity-fallback model on the same answer.

Every review run of PR #4077 between 2026-09-11 and 2026-09-12 (runs 34663517732, 34654303940, 34550848501 and more than 15 others) lost its editor phase to the same `broker_rejection` 429. One broker instance serves the whole retry loop in `scripts/review_apply_fixes.sh`, so attempts 2 and 3 were rejected on their first model call while the log blamed provider saturation ("capacity-limited"), and the only trace of the real cause was the model runtime's opaque `AI_APICallError`. The broker now mirrors up to 100 rejections per HTTP error class to a `MODEL_PROVIDER_BROKER_REJECT status=<n> path=<path> message=<json>` stderr line and to a per-broker JSON-lines file; query strings are discarded and paths outside the allowlist are redacted before either sink is written. The editor loop snapshots the 4xx count before each attempt and, when a failed attempt recorded new rejections, logs `EDITOR_BROKER_POLICY_REJECTION ...`, emits `broker_policy_rejection` on a non-final attempt or preserves `attempt_failed` on the final attempt, and leaves the loop. The fallback summary stays `recoverable_failure`, so partial-finalize handling and the no-op validator are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Runner time lost per round to replayed 429s on PR #4077 | about 18 minutes (3 attempts) |
| Model calls that could succeed after the first rejection | 0 (same broker policy for the whole run) |
| New broker flag | `--rejections-file` (optional; `MODEL_PROVIDER_BROKER_REJECTIONS_FILE`, default `<runtime dir>/model-provider-broker-rejections.jsonl`, mode 0600) |
| Counted as deterministic | HTTP 4xx only; relayed 502 upstream failures still retry |
| Rejection diagnostic cap | 100 records per HTTP status class; 4xx and 5xx have independent quotas |
| New non-final alert failure class | `opencode_agent_failure ... failure_class=broker_policy_rejection` |

What this means for operators: a Telegram alert that names `broker_policy_rejection` means the broker refused a non-final editor attempt (request cap, output-token budget, unauthorised model) and a rerun with the same policy will fail the same way. Final attempts retain the existing `attempt_failed` alert classification; in either case, look for `EDITOR_BROKER_POLICY_REJECTION` and `MODEL_PROVIDER_BROKER_REJECT` lines in the editor step log for the exact reason. Provider outages still get the full three attempts and the fallback model.

### For contributors

`model_provider_broker_start` passes the file and exports `MODEL_PROVIDER_BROKER_REJECTIONS_FILE`; `model_provider_broker_stop` removes both. `model_provider_broker_policy_rejection_count` and `model_provider_broker_last_policy_rejection` in `scripts/codex_helpers.sh` are the read side, and the editor loop guards both calls with `command -v` so an older helper file keeps the previous behaviour. The rejection record carries `{ts,status,path,message}` without query strings or unapproved paths, so the session token and upstream key cannot be copied from an attacker-controlled request target; `tests/test_model_provider_broker.py` and `tests/test_review_editor_broker_policy_rejection_fail_fast.py` pin the contract.

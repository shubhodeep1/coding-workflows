<!-- changelog: fixed -->
- **review_autofix no longer loses a whole reviewer pass to the shared attempt-prompt race.** Each reviewer slot now copies the pass prompt to its own per-slot attempt file, and an empty effective prompt is restored (or loudly flagged) before codex launches.

In `scripts/review_run_reviewers.sh`, every concurrent reviewer worker used to derive the same `<pass prompt>.attempt_1` path whenever no model-family overlay existed, then `cp`-truncate, nag-append, and sanitize-rewrite that one file while codex read it as stdin. One bad interleaving left the file empty, every reviewer failed non-retryably with `No prompt provided via stdin`, and the review_autofix job failed with "All reviewers failed" even though the assembled pass prompt was intact. Observed on consumer run tele-funtoken-msg-scoring `actions/runs/32222803753` (PR #3721, pass 2: 6 of 6 reviewers failed ~1.5s after launch). The attempt path now embeds the per-slot `safe_name`, and a pre-launch guard restores an unexpectedly empty effective prompt from the base prompt with a `::warning::` instead of failing the slot silently.

| The numbers that matter | Value |
| --- | --- |
| Affected script | `scripts/review_run_reviewers.sh` |
| Failure signature | `No prompt provided via stdin` on every slot of one pass |
| Local race repro (shared path) | 41 of 1200 reads empty |
| Local race repro (per-slot path) | 0 of 1200 reads empty |

What this means for operators: a review_autofix run can no longer fail an entire reviewer pass because parallel reviewer slots raced on one temp prompt file; a Telegram "PR autofix failed" alert from this signature should not recur, and any residual empty-prompt condition now shows up as an explicit `::warning::` in the job log.

### For contributors

`tests/test_review_reviewer_attempt_prompt_isolation.py` pins both halves: the attempt path must embed `${safe_name}`, and the empty-prompt guard must precede the codex launch redirect. `${safe_name}` is `run_reviewer`'s local, visible inside `execute_reviewer_attempt` via bash dynamic scoping (sole call site), matching the existing pattern in `emit_reviewer_substate`.

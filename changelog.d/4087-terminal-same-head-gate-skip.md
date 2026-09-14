<!-- changelog: fixed -->
- **The review gate now skips `workflow_dispatch` reruns of a same-head review cycle that has already ended.** A PR whose newest `<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->` comment says `resume_should_continue=false` for its current head no longer gets a full codex-agent job every time the 30-minute sweep fires.

Operators saw the cost on PR #4077: after the same-head cycle went terminal on `0d30bc69` at 2026-09-12T15:55Z (`resume_state=no_progress`, round 3 of 3), `Internal: AI Review Autofix Sweep` kept dispatching `internal-review.yml` for it every 30 minutes for 12.5 hours. Each of those runs (for example 34703936345) restored the cached terminal state and exited neutral after about 6 minutes of runner setup, doing no review work. The `Evaluate review gate` step in `review_autofix.yml` now reads the newest marker for the PR's current head on `workflow_dispatch` events and sets `should_run=false` with `skip_reason=terminal_same_head` before the codex-agent job starts. Only a PR GitHub reports `mergeable=true` is skipped, so a conflicted head still reaches the resolver; `pull_request` events, `force_rb_judge` dispatches, `[force-review]` in the title and the `force-review` label all bypass it, and a comments API or parse failure logs `AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED` and runs normally.

| The numbers that matter | Value |
| --- | --- |
| Same-head reruns dispatched after PR #4077 went terminal | about 25 over 12.5 hours |
| Runner time per no-op rerun | about 6 minutes |
| New repo variable | `AUTOFIX_SKIP_TERMINAL_SAME_HEAD` (default `true`) |
| New API calls per skipped dispatch | 1 (paginated `/issues/{n}/comments`); `head_sha` rides on the existing `/pulls/{n}` fetch |
| Skip log line | `AUTOFIX_GATE_SKIP reason=terminal_same_head pr=<n> head_sha=<sha> resume_state=<state> resume_round=<n> resume_round_limit=<n> marker_comment_id=<id> mergeable=true` |

What this means for operators: a PR that exhausted its same-head resume rounds stays quiet until a new head is pushed, it becomes conflicted, or someone forces a review. Set `vars.AUTOFIX_SKIP_TERMINAL_SAME_HEAD=false` to restore the previous unconditional re-dispatch.

### For contributors

The decision logic is an embedded Python heredoc in the gate step, extracted and executed by `tests/test_review_autofix_terminal_same_head_gate.py` against marker fixtures. It keys on the fenced `key=value` block of the partial-finalize comment (`partial_finalize=true`, `head_sha=`, `resume_should_continue=`), picks the newest matching marker by `created_at` then comment id, and reduces every value it echoes to `[A-Za-z0-9_.-]` before it reaches a log line. `AUTOFIX_GATE_NO_SKIP_TERMINAL_SAME_HEAD`, `AUTOFIX_GATE_TERMINAL_SAME_HEAD_UNCHECKED` and `AUTOFIX_GATE_TERMINAL_SAME_HEAD_OVERRIDE` record the non-skip decisions.

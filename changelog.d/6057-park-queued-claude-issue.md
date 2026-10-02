<!-- changelog: fixed -->
- **Removing `ai:claude` from a queued issue now stops the Claude issue pickup from starting it.** The pickup re-reads each queued issue just before it starts a session. It starts nothing for an issue that has lost its `ai:claude` claim, gained `ai:codex`, or been closed, and closes that queue item as not planned.

Until now the routing decision was made once, at intake. #6038 was queued as #6039 at 02:33Z on 2026-10-02, and `ai:claude` was removed eleven minutes later to park it. #6039 stayed eligible anyway, and only closing it by hand kept its session from starting. `scripts/claude_issue_route.py queue-pending` now reads each pending target issue. A target that fails the check is listed under a new `refused` key, with reason `claude_label_removed`, `codex_label_added`, or `issue_closed`, instead of `pending`. The pickup closes those queue items with a `Refused: <reason>` line in the body and posts no comment. The intake applies the same claim rule through `authorize_target()`. The handoff (`scripts/claude_issue_handoff.sh`) no longer dispatches an issue it could not label `ai:claude`, so a missing label always means the issue is parked.

| The numbers that matter | Value |
| --- | --- |
| New REST reads per pickup wake | 1 per issue it would start (at most 20 by default, 30 at most); none for pull-request fix items |
| New refusal reasons | `claude_label_removed`, `codex_label_added` |
| Intake API calls added | 0 (the existing issue read carries the labels) |

What this means for operators: to hold back an issue that is already queued, remove `ai:claude`. To release it, add `ai:claude` back and comment `/reclarify`; the label alone queues nothing. A manual `workflow_dispatch` of `claude-issue-intake.yml` now needs the issue to carry `ai:claude` first. If the pickup session cannot read an issue (for example, a consumer repository that is not attached to it), the issue is started as before. Its reason is listed under `claim_check_failed`.

### For contributors

The shared rule is `claim_label_refusal()`. The pickup-side read and check are `fetch_dispatch_targets()` and `apply_dispatch_claim_check()`; the `--targets-json` option feeds them offline. `tests/test_claude_issue_route.py` covers the intake, the pickup, and the handoff cases. The pickup's handling of `refused` lives in `.claude/commands/claude-issue-pickup.md`.

<!-- changelog: fixed -->
- **The operator-step tracker no longer closes while entries signed by the job token are still open.** The poller's resolve step now verifies signed `github-actions[bot]` entry comments itself and keeps the tracker open when it cannot.

On 2026-11-23 the poller closed tracker #9647 while 7 entries were unticked: project-9169, project-8969, project-9781, project-8291, pr-9750, pr-9956 and unblock-fixup-9670. Those entries were posted by `github-actions[bot]` with an `ai-comment-sig:v1` signature (PR #9643), and the step "Resolve released operator steps" in `.github/workflows/orchestrate_poll.yml` did not receive `AI_COMMENT_SIGNING_SECRET`, so it read them as untrusted and saw only one finished entry. `scripts/operator_step_issue.py` now checks signatures in every comment read (`tick`, `resolve`, `upsert`), the step gets the secret, and a signed entry that cannot be verified blocks the close (`reason=unverified_signed_entries`) instead of being ignored. `/deploy-activate` no longer edits a signed entry comment in place, which broke its signature; it posts a ticked copy as a new comment, and it now also finds entries left on closed trackers.

| The numbers that matter | Value |
| --- | --- |
| Entries left on closed tracker #9647 | 7 |
| New API calls per poller tick | 0 |
| New close reason | `unverified_signed_entries` |

What this means for operators: an `ai:operator-step` tracker stays open until every entry, signed or not, is ticked. If a tracker stays open with `OPERATOR_STEP_RESOLVE op=verify outcome=unverified` in the poll log, either the signing secret is missing from that run or an entry comment was edited by hand; posting a ticked copy as a new comment clears it.

### For contributors

Verification loads `scripts/comment_signature.py` in-process and reuses `normalize_comment`, so it matches the gh shim exactly and is a no-op on comments the shim already normalized. The promote cycle's `tick` still runs without the secret; it skips signed entries as `unverified` rather than ticking over them.

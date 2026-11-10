<!-- changelog: fixed -->
- **The blocked-issue reply check no longer flags the unblock judge's own status notes.** `detect_unrouted_blocked_comments` treated the judge's "belongs in the harness" note as a human reply and sent a false Telegram WARNING.

The unblock judge posts some status notes without an HTML marker, under the owner's account, one second after its verdict comment. The classifier in `scripts/orchestrate_lib.py` (`classify_unrouted_blocked_issues`) saw a trusted `User` comment newer than the `ai:blocked` label and reported it as `no_command`, so the poller posted "the reply posted N minutes ago did not resume the pipeline" and paged the operator. On `shubhodeep1/fun-token-multi-chain#821` this happened once per judge round. The classifier now recognises the opening words of these notes and skips them as reply candidates.

| The numbers that matter | Value |
| --- | --- |
| Note openings now recognised | 9 (all from `scripts/unblock_actions.py`) |
| False warnings on fun-token-multi-chain#821 | 2 (rounds 1 and 2) |
| New env vars or log prefixes | 0 |

What this means for operators: a `Blocked issue #N has an unrouted reply (no_command, ...)` WARNING now means a person replied and nothing picked it up. A real reply posted before a judge note is still flagged, because the notes only stop counting as replies and do not count as proof that a reply was handled.

### For contributors

The openings live in `_UNROUTED_PIPELINE_PREFIXES`. A new unmarked comment in `scripts/unblock_actions.py` needs either an `<!-- ai:... -->` marker or an entry there. `test_unrouted_ignores_unblock_judge_status_notes` in `tests/test_orchestrate_lib.py` builds the harness note from `_harness_ops` itself and checks that every opening still appears in `scripts/unblock_actions.py`, so a reworded note fails the test. The "stays open" openings keep the judge-specific words after them, so a person's reply that starts with "This security finding stays open" still counts as a reply.

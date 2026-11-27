<!-- changelog: fixed -->
- **The autofix-cap `ai:needs-human` latch is released once the PR's head moves, and infrastructure failures no longer spend the review budgets.**

On 2026-11-21, 11 of the 13 open PRs carrying `ai:needs-human` had been parked by the autofix iteration cap's "AI review/autofix blocked — needs human intervention" comment. That latch belongs to the head it fired on, but nothing removed it when the head moved, so PRs like #9359 and #9389 stayed parked on green new heads until their owner removed the label by hand. The poller's head-latch release (PR #9839 / #9862) now handles this comment as a third latch kind, `autofix_cap_head_moved`. The comment carries `<!-- ai:autofix-cap-head:v1 head=<sha> -->`. An older comment without the marker takes its head from the `pull_request` run its `Run:` line names, but only when that run was on the PR's own branch. Dispatched runs report a default-branch commit as their head, so latches from them stay for a person.

Separately, on #9853 the Claude pool broker refused the review editor three times (`no_credential`), and those rounds still counted toward the sweep's review-round cap, which parked the PR. A round whose editor produced nothing usable and whose review engine was refused (`no_credential`, `all_gated` or `broker_refused_*`) is now marked infra-failed. Its commit is skipped, so the autofix iteration counter does not advance. A dispatched run also uploads a `review-round-infra-failed` artifact, and the sweep's round count drops those runs (`REVIEW_ROUND_INFRA_EXCLUDED`).

| The numbers that matter | Value |
| --- | --- |
| Switches | `NEEDS_HUMAN_HEAD_LATCH_AUTO_RELEASE_ENABLED` (release), `REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED` (exclusion), both default `true` |
| Extra API per poller tick | 1 `actions/runs/<id>` read per legacy autofix-cap run id (cached for the tick) |
| Extra API per sweep tick | 1 paginated `actions/artifacts?name=review-round-infra-failed` listing |
| Marker artifact retention | 3 days |
| Dispatch backoff | a PR with 2× `SWEEP_REVIEW_MAX_ROUNDS` rounds in the window, infra-failed ones included, is skipped without a label |

What this means for operators: an autofix-cap park on an old head clears by itself on the next poller tick after a push. A broker or credential outage no longer parks PRs at the round cap. Set either switch to `false` to restore the previous behaviour.

### For contributors

`review_round_infra_failure_reason` lives in `scripts/review_sweep_round_cap.sh` and is called by the new "Classify infra-failed review round" step in `review_autofix.yml`. `review_sweep_round_counts` writes its counts before the infra exclusion to `REVIEW_SWEEP_ROUND_RAW_COUNTS_FILE` when that is set. Tests: `tests/test_needs_human_head_latch_release.py`, `tests/test_review_round_infra_exclusion.py`, `tests/test_review_sweep_round_cap.py`.

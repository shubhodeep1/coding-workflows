<!-- changelog: fixed -->
- **The unblock scan no longer re-dispatches the judge for escalated fix-ups and for parked items nothing has changed on.** Escalated fix-ups were judged every hour and never changed; a parked `close` whose `ai:needs-human` label was dropped by label repair was judged again every six hours, each time posting another `close` round.

`scripts/unblock_judge.sh` escalates a blocked fix-up once (`<!-- ai:unblock-fixup-escalated:v1 item=<n> -->`) and then only logs `reason=fixup_escalated` on every later run, but the scan did not read that marker, so `unblock_judge_dispatch.yml` started a run for the item every `UNBLOCK_JUDGE_INFLIGHT_MINUTES` (60). In `shubhodeep1/drhyg_ecommerce_automation` that was 86 runs on 2026-10-16 (100 runs from 2026-10-14 to 2026-10-17), 12 to 23 each for the escalated fix-ups #77, #79, #81, #82 and #83, and in `shubhodeep1/coding-workflows` the escalated #7258 was judged 45 times from 2026-10-15 until it was closed on 2026-10-17. `scripts/unblock_scan.py` now skips such an item (`skipped` key `fixup_escalated`) and lets one run through per day, so the scope-blocked re-grant check still happens when the guard changes.

The second loop is `shubhodeep1/fun-token-multi-chain#639`: the judge's round 3 `close` parked the issue (`ai:needs-human`), the poller's label repair removed that label seconds later (`ai:blocked` wins the phase group on an `ai:orchestrator-managed` issue) and pruned its digest entry, and the scan judged it again after the six-hour marker age with the same fingerprint (round 4: "Terminal: item_cap (3 round(s) on this item)"). The scan now skips an issue or project when its newest trusted verdict is a `close` for the current stop, the park label was applied after it and is gone, and since then there is no newer block label event and no comment other than the judge's own markers, orchestrator state comments and bots (`skipped` key `terminal_unchanged`). Pull requests, failed projects and `NEEDS_HUMAN_DIGEST_ENABLED=false` keep their old behaviour, and an item still labelled `ai:needs-human` but missing from the digest is judged again.

| The numbers that matter | Value |
| --- | --- |
| New repo variable | `UNBLOCK_JUDGE_ESCALATED_RECHECK_HOURS`, default `24`, `0` restores a run per hour |
| Judge runs per escalated fix-up per day | 24 before, 1 after |
| Extra API calls | 0 (label events, comments and runs list the scan already reads) |

What this means for operators: consumer repositories with an escalated fix-up stop starting a judge run every hour, and a parked item whose label repair keeps removing `ai:needs-human` gets no further `close` rounds until a new block label, a new human or pipeline comment, or a different stop appears. The first CRITICAL "parked" alert for such an item is still sent once.

### For contributors

The root cause of the dropped park label is outside this change: `reconcile_managed_issue_labels` in `scripts/orchestrate_poll_process.sh` applies the phase-group exclusivity from `scripts/ai_labels.py repair-labels` to `ai:needs-human`, which the park (`scripts/operator_step_issue.py needs-human park`) adds next to `ai:blocked`. The scan change stops the loop without touching the label contract.

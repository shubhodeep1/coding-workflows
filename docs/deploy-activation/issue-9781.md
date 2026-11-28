# Deploy-Activation Log — merge lease parallel merges (multi-holder lease)

- Reference: #9781   (+ plan docs/plans/merge-lease-parallel-merges-plan.md; Phase 5 PR #9915, merged 2026-11-22)
- Deploy target: shubhodeep1/coding-workflows
- How it runs: repo variable MERGE_LEASE_MAX_HOLDERS read by the merge-lease gate in review_autofix_sweep.yml (cron) and the other wired workflows
- Status: LIVE
- Last updated: 2026-11-28
- Last note: MERGE_LEASE_MAX_HOLDERS was already 2 (since between 2026-11-22T22:30Z and 2026-11-23T02:32Z); two-holder leases seen in logs; step ticked in #10041.
- Human-steps issue: https://github.com/shubhodeep1/coding-workflows/issues/10041 — keys project-9781

## Runbook
1. [x] Review a week of MERGE_LEASE_METRIC readings (K1-K4)   — done 2026-11-28: summary in Notes
2. [x] Set repo variable MERGE_LEASE_MAX_HOLDERS=2 (read at scripts/pr_checks_lib.sh _merge_lease_max_holders, default 1) — covers project-9781 «Phase 5 multi-holder lease is off until MERGE_LEASE_MAX_HOLDERS is raised above 1» — done 2026-11-28: already configured, verified: sweep run 46117312268 env shows `MERGE_LEASE_MAX_HOLDERS: 2`
3. [x] Verify LIVE   — done 2026-11-28: run 46117312268 logs `MERGE_LEASE pr=10244 ... holder=10110 ... holders=2`; earlier runs show pairs 10067+10102, 10054+10132, 10081+10161, 10152+10178, 10110+10184, 10110+10202

## Notes
- The value was already 2 when this session began. In sampled review_autofix_sweep runs it is 1 on 2026-11-22T22:30Z and 2 from 2026-11-23T02:32Z on, so it was raised about 20 h after the Phase 5 merge, not after the planned week of readings.
- Readings, 2026-11-21 to 2026-11-28 (342 sweep runs and 627 ci.yml runs scanned):
  - K3: 165 PRs merged 2026-11-16..22 (about 23.6/day) vs 140 merged 2026-11-23..28 (about 26/day, 28th partial).
  - K4: 41 `MERGE_BASE_SYNC outcome=accepted` in the sweep logs against 140 merges, so well under 1 branch update per merged PR (baseline 2).
  - K1/K2: not measurable. The `granted`/`released` metrics come from the merge-train tick in orchestrate_poll.yml, which ran only once since 2026-11-21 (and failed). The sweep logged 474 `event=wait` and 7 `event=renewed` lines across 23 distinct waiting PRs.
  - The 205 `event=unavailable reason=read_failed` lines in ci.yml are test-fixture output (pr=42), not real lease traffic.
- The Mac folder attach (claude-repos) was refused with at_capacity. Nothing needed the Mac in the end.

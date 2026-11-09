# Deploy-Activation Log — persistent unattended workflows (P1–P11 switches)

- Reference: #8427 (orchestrator project, closed completed 2026-11-07)   (+ PR #8430 squash 0480649; plan docs/plans/persistent-unattended-workflows-plan.md)
- Deploy target: shubhodeep1/coding-workflows (consumers optional: each repo that wants a switch sets the same repo variable)
- How it runs: inside the existing pipelines (clarify / plan / implement / review_autofix / orchestrate / orchestrate_poll cron / issue_pr_status on PR merge), each phase behind a default-off repo variable
- Status: IN_PROGRESS
- Last updated: 2026-11-09
- Last note: Steps 1-8 and 13 done; next is Step 9 (set PERSIST_ORCH_FAILURE_GROUPING_ENABLED=true)
- Human-steps issue: https://github.com/shubhodeep1/coding-workflows/issues/6211 — key project-8427 (authoritative comment now 6729299210, posted 2026-11-09; all 11 lines already ticked by the promote cycle)

## Runbook
1. [x] Read current repo variables (PERSIST_*, PREPUSH_TEST_GATE_MODE, ENABLE_CLEAN_WAVE_JUDGE_SKIP) — done 2026-11-09: operator `gh variable list` shows none set
2. [x] Set PERSIST_JOURNAL_ENABLED=true (implement.yml:21, default 'false') — covers project-8427 «P1 attempt journal off (PERSIST_JOURNAL_ENABLED)» — done 2026-11-09: operator `gh variable get` returns `true`
3. [x] Set PERSIST_ACCEPTANCE_ENABLED=true (plan.yml:22, default 'false') — covers project-8427 «P2 plan acceptance / done-checks off (PERSIST_ACCEPTANCE_ENABLED)» — done 2026-11-09: operator `gh variable get` returns `true`
4. [x] Set PERSIST_REPAIR_LOOP_ENABLED=true (implement.yml:25, default 'false') — covers project-8427 «P3 multi-turn repair loop off (PERSIST_REPAIR_LOOP_ENABLED)» — done 2026-11-09: operator `gh variable get` returns `true`
5. [x] Set PERSIST_EVIDENCE_PACK_ENABLED=true (clarify.yml:23, default 'false') — covers project-8427 «P4 evidence pack off (PERSIST_EVIDENCE_PACK_ENABLED)» — done 2026-11-09: operator `gh variable get` returns `true`
6. [x] Set PERSIST_POSTMERGE_VERIFY_ENABLED=true (issue_pr_status.yml:53, default 'false') — covers project-8427 «P5 post-merge verification off (PERSIST_POSTMERGE_VERIFY_ENABLED)» — done 2026-11-09: operator `gh variable get` returns `true`
7. [x] Set PERSIST_BUDGET_ENABLED=true (orchestrate_poll.yml:47, default 'false') — covers project-8427 «P6 budget and progress governor off (PERSIST_BUDGET_ENABLED)» — done 2026-11-09: operator said `done` (no output; session proxy cannot read vars, re-checked in Step 14)
8. [x] Set PERSIST_ORCH_CONTEXT_ENABLED=true (orchestrate.yml:73, default 'false') — covers project-8427 «P7 orchestrator context off (PERSIST_ORCH_CONTEXT_ENABLED)» — done 2026-11-09: operator `gh variable get` returns `true`
9. [ ] Set PERSIST_ORCH_FAILURE_GROUPING_ENABLED=true (orchestrate_poll.yml:54, default 'false') — covers project-8427 «P8 repeated-failure grouping off (PERSIST_ORCH_FAILURE_GROUPING_ENABLED)»
10. [ ] Set PERSIST_ORCH_WAVE_CHECK_ENABLED=true (orchestrate_poll.yml:58, default 'false') — covers project-8427 «P9 between-wave CI check off (PERSIST_ORCH_WAVE_CHECK_ENABLED)»
11. [ ] Set PERSIST_ORCH_REDECOMPOSE_ENABLED=true (orchestrate_poll.yml:61, default 'false') — covers project-8427 «P10 re-decompose remaining waves off (PERSIST_ORCH_REDECOMPOSE_ENABLED)»
12. [ ] Set PERSIST_EVIDENCE_BROKER_ENABLED=true (plan.yml:27, default 'false') — covers project-8427 «P11 plan evidence broker off (PERSIST_EVIDENCE_BROKER_ENABLED)»
13. [x] Verify `@stable` carries PR #8430 (automatic daily promote cycle; needed only for consumer repos) — done 2026-11-09: tag `stable` = b996b678, contains 0480649
14. [ ] Verify LIVE (all 11 variables read `true`; a pipeline run shows a gated step executing)

## Notes
- 2026-11-08: completeness verified. PR #8430 (0480649, 2026-11-07) is on main; all 11 `vars.PERSIST_*` read-sites present at origin/main 68ac90d.
- 2026-11-08: `stable` (f94437c) does not yet contain 0480649. The scheduled promote-main-to-stable run 42429586375 and its release gate (test-and-mark-stable on `promote-candidate`) were in progress at planning time, so no manual tag step is planned.
- The cloud session's GitHub proxy refuses Actions variable reads/writes (HTTP 403), so variable steps run on the operator's Mac.
- Caveats from the tracker: P3 also needs PREPUSH_TEST_GATE_MODE unset or not 'off'; P9 needs ENABLE_CLEAN_WAVE_JUDGE_SKIP not 'false' (default 'true').
- 2026-11-09: Step 1: no PERSIST_* variable set; PREPUSH_TEST_GATE_MODE and ENABLE_CLEAN_WAVE_JUDGE_SKIP unset, so the P3 and P9 side conditions hold by default. Step 1 covers no human-steps line.
- 2026-11-09: a newer project-8427 entry, comment 6729299210, arrived with all 11 P-lines `[x]` and the marker "Done: the `stable` tag (`b996b67`) includes the merge `0480649`; ticked by the promote cycle." Those ticks mean "code released", not "variable set": Step 1 showed no PERSIST_* variable set that day. Nothing left to tick; the older comment 6704272982 is left unchanged because the pipeline reads only the newest. The runbook still sets the remaining 10 variables.

<!-- changelog: changed -->
- **A failed stable release or promote-cycle tick is retried on the next tick, up to three attempts per tip, instead of waiting for the branch to move.**

Until now one failed `test-and-mark-stable.yml` run froze the schedulers on that commit: `auto-release-stable.yml` skipped every 6-hour tick with `AUTO_RELEASE_SKIPPED reason=last_gate_failed`, and the daily cycle in `promote-main-to-stable.yml` skipped with `PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_failed_run`, until a new commit landed or an operator re-ran the gate by hand. That was right for a failing test on the commit and wrong for everything transient. On 2026-09-21 the v1.29.7 release run (35570966035) passed its whole gate and then lost the tag push to a GitHub-side timeout, and nothing would have retried it. Both schedulers now count the failed runs on the current tip and dispatch again while the count is below the budget; a cancelled run still never counts. Once the budget is spent the old hold applies, so a deterministic failure stops costing gate runs, and the `Workflow Failure Heal Intake` hotfix that fixes it moves the branch and unlocks the next attempt.

| The numbers that matter | Value |
| --- | --- |
| `AUTO_RELEASE_STABLE_MAX_ATTEMPTS` (repo var, `auto-release-stable.yml`) | default `3` failed gate runs per `stable` tip |
| `PROMOTE_CYCLE_MAX_ATTEMPTS` (repo var, `promote-main-to-stable.yml` cycle job) | default `3` failed cycle runs per `main` tip |
| Retry cadence | the schedulers' own ticks: every 6 hours on `stable`, daily on `main` |
| Extra API cost | none on `stable`; at most `PROMOTE_CYCLE_MAX_ATTEMPTS` compare calls per cycle tick |

What this means for operators: a release that failed on a runner loss, a GitHub-side rejection or another one-off goes out on a later tick by itself. Set either variable to `1` to restore the previous hold-on-first-failure behaviour. The skip lines now carry `attempts=N max=M`, so a tip that is genuinely stuck is visible as such.

### For contributors

`scripts/auto_release_stable.sh` counts completed runs on the `stable` branch with the tip's SHA and a `failure`, `timed_out` or `startup_failure` conclusion from the same 30-run read it already made. `scripts/promote_main_cycle.sh` walks failed scheduled cycle runs newest first, counts each one that no code change separates from the tip, and stops at the first one a code change does; the newest failed run keeps the `base_not_ancestor` and `guard_unavailable` handling it had. Both scripts reject a non-positive budget at startup.

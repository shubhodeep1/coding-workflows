<!-- changelog: added -->
- **The review sweep now re-runs a PR's cancelled `CI` run once per head.** A `CI` run cancelled by its timeout, a lost runner, or a manual cancel no longer leaves the PR stalled until someone re-runs it by hand.

Nothing in the pipeline acted on a cancelled `CI` run: `.claude/scripts/check_in_status.py` does not count `cancelled` as a failed check, so the §26 checker, the Claude fixer and the catch-all sweep all skipped it. On 2026-09-28 that stalled #4633 until a human merged it. The 30-minute `sweep` job of `.github/workflows/review_autofix_sweep.yml` now has a step, `Re-run cancelled CI once per PR head` (`scripts/ci_cancelled_rerun.py`), that handles this case. For every open, non-draft, same-repository PR it looks at the newest `ci.yml` run on the PR's current head. When that run concluded `cancelled` or `startup_failure` on attempt 1 and nothing else is running for that head, the step re-runs the failed jobs once. A `failure` is never re-run, and a run for a head that a newer push superseded is never matched.

| The numbers that matter | Value |
| --- | --- |
| Automatic re-runs per PR head | at most 1 (decided by `run_attempt`) |
| Conclusions re-run | `cancelled`, `startup_failure` |
| Extra API calls per sweep tick | 1 runs listing, plus 1 POST per re-run |
| Cadence | every 30 minutes (`*/30 * * * *`), and on `workflow_dispatch` |
| Kill switch | repo variable `CI_CANCELLED_AUTO_RERUN_ENABLED`, default `true` |

What this means for operators: a cancelled `CI` run on a PR is re-run within about 30 minutes, once. Each decision is logged as a `CI_CANCELLED_RERUN pr=<n> head=<sha> run=<id> action=<rerun|skip> reason=<…>` line in the sweep run. Set `CI_CANCELLED_AUTO_RERUN_ENABLED=false` to turn it off. PRs whose title or body carries `[skip ai]` are left alone. This applies to coding-workflows only: consumer repos have no common CI workflow name, and the sweep workflow is not synced to them.

### For contributors

The enumerate step now adds `head_sha` to its PR projection and writes the snapshot to `${RUNNER_TEMP}/review-autofix-sweep-prs.json`, so the re-run step needs no second PR listing. The re-run step and its sparse checkout run after the dispatch loop with `if: ${{ !cancelled() }}`, so they never block review dispatches. When `rerun-failed-jobs` refuses a `startup_failure` run, the step falls back once to the full `rerun` endpoint, because such runs often have no jobs. `tests/test_ci_cancelled_rerun.py` covers every rule and the wiring, and runs in its own `ci.yml` step.

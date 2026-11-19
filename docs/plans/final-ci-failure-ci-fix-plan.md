# Fix a failed final CI with a CI fix, not a full review round

## Summary

When an approved PR head's final CI run fails, the pipeline today either does
nothing useful (the hand-back's review run takes the gate's same-head skip) or,
when that skip does not apply, runs a full review and autofix round on code that
review already approved. This plan routes a final-CI failure by cause:
infrastructure failures get a re-run of the failed jobs with no review, real
failures get one CI-fix commit (the existing post-sync test-repair path,
widened beyond orchestrator integration branches), and that commit is reviewed
on its own diff only. Three independently mergeable phases, each behind a repo
variable that defaults on.

Operator decisions (2026-11-17, interactive session): Q5: A (CI fix plus
fix-only review), Q6: A (re-run only classifier-recognised infra failures),
Q7: A (one repair per head, at most 2 per PR, then the full review round),
Q8: A (normal reviewer panel on the fix diff), Q9: A (repo variable per phase,
default on), Q10: A (plan names #9652 as a dependency; no cross-session
message).

## §18.E — Automation surface

- **Scripts:** no new script. Phase 1 extends `scripts/review_final_ci_failed.sh`
  and `scripts/ci_cancelled_rerun.py`; Phase 2 extends
  `scripts/review_autofix_step_evaluate_gate.sh`,
  `scripts/review_autofix_step_prepush_test_gate.sh` and
  `scripts/review_conflict_resolve.sh`; Phase 3 extends
  `scripts/review_head_gate.sh` and
  `scripts/review_autofix_step_generate_diff_context.sh`.
- **Entry points (existing triggers only):**
  - `.github/workflows/ci.yml` job `final-ci-failed` (final-CI
    `workflow_dispatch` runs whose `lint` failed) runs
    `review_final_ci_failed.sh`.
  - `.github/workflows/review_autofix_sweep.yml` step "Re-run cancelled CI
    failed jobs once" (30-minute cron) runs `ci_cancelled_rerun.py`.
  - `.github/workflows/review_autofix.yml` gate and the step "Integration
    post-sync test repair" (every review run).
- **Long-running supervisor:** none required (§18.C); the sweep cron and the
  final-CI run are the triggers.
- **DB work:** none (§10, §18.D do not apply).
- **`docs/scripts-pending-removal.md`:** no entry; no single-use or
  long-running script is introduced.
- **Runner concurrency (§33):** no new job, workflow or matrix leg. Jobs per
  final-CI failure: before, 1 review run (gate plus either a skip or the full
  reviewer panel and editor); after, either 0 review runs (infra: one
  failed-jobs re-run of the same CI run) or 1 review run that runs the repair
  step instead of the panel, then 1 review run of the fix diff.

## Context

- Final CI (cut-ci-runner-time Phase 4, #9535/#9563): `ci.yml` runs once per
  approved head as a `workflow_dispatch` run named `CI [pr:<n>]`. When its
  `lint` fails, `final-ci-failed` runs `review_final_ci_failed.sh`, which posts
  `<!-- ai:final-ci-failed:v1 pr=<n> head=<sha> -->` and dispatches the PR's
  review workflow.
- **Defect:** the gate's clean same-head skip
  (`review_autofix_step_evaluate_gate.sh`, "AUTOFIX_SKIP_CLEAN_SAME_HEAD",
  issue #8952) is invalidated by a trusted `ai:freshness-ci-failed:v1` marker
  but not by `ai:final-ci-failed:v1`. The hand-back's review run therefore
  logs `AUTOFIX_GATE_SKIP reason=clean_same_head` (or `terminal_same_head`) on
  the approved head and nothing fixes it. PR #9239 collected 40 head-gate
  statuses on one red head; #9602 and #9562 are current examples.
- Infra classifier: `classify_runner_shutdown` in `scripts/ci_cancelled_rerun.py`
  returns `shutdown`, `action_resolution`, `checkout_fetch`, `abandoned`,
  `genuine` or `unverified`, and `process` re-runs failed jobs once (attempt 1
  only). `associated()` accepts only `pull_request` runs, so final-CI
  `workflow_dispatch` runs are never considered today.
- A job cannot re-run its own workflow run while it is in progress, so the
  `final-ci-failed` job cannot re-run the run it belongs to; the sweep must.
- CI-fix mechanism to extend: `review_autofix.yml` step "Integration post-sync
  test repair" (#8210) sources `review_autofix_step_prepush_test_gate.sh` with
  `PREPUSH_TEST_GATE_STEP_MODE=integration`. With `POST_SYNC_TEST_REPAIR=true`
  on an `orchestrator/project-<N>` head it sets `INTEGRATION_TEST_REPAIR_ACTIVE`
  (reviewer and editor steps skip), gates the head against its merge-base and
  runs `review_conflict_resolve.sh --test-repair <result.json>` once, which
  commits `[ai-merge-resolve] repair post-sync test failures`. Its context
  (`_resolver_test_repair_context`) reads the orchestrator tracking issue and
  intent fingerprints, so it cannot run for a plain PR as is.
- Fix-only review has a precedent: `review_head_gate.sh freshness-sync` lets an
  `ai:merge-base-sync:v1` update commit skip the reviewer panel. The review diff
  itself is always taken from the PR's merge-base with its base
  (`review_autofix_step_generate_diff_context.sh`).
- Dependency (Q10: A): #9652 added the sweep's base sync after a failed final
  CI (`FINAL_CI_SWEEP_BASE_SYNC_*`). It waits
  `FINAL_CI_SWEEP_BASE_SYNC_MIN_AGE_MINUTES` (30) so the hand-back acts first;
  this plan keeps that order and does not change #9652's code. #9649 added the
  sweep skip `SWEEP_SKIP_APPROVED_FINAL_CI_FAILED`, which is unaffected.

## Goals

1. An infra-caused final-CI failure is re-run once, with no review run.
2. A real final-CI failure on an approved head gets one CI-fix commit from the
   test-repair path, not a full reviewer-panel round.
3. A CI-fix commit on top of an approved head is reviewed on its own diff by
   the normal panel; approval then flows to final CI and merge as today.
4. At most one repair per head and two per PR; after that, the existing full
   review and autofix round runs (Q7: A).

## Non-goals

- Re-running genuine test failures to detect flakes (Q6: A).
- Consumer repos: they do not set `REVIEW_FINAL_CI_WORKFLOW`, so final CI and
  everything here stay inert there (§14: no `consumer_repos.json` change).
- Changing #9652's base sync, the merge train, or `ci_pr_quick.yml`.

## Constraints

- §6: no rename or removal. New identifiers (repo variables, markers, log
  keys, env vars) must be unique; existing `post_sync_test_repair`,
  `INTEGRATION_TEST_REPAIR_ACTIVE` and `[ai-merge-resolve]` behaviour for
  orchestrator heads must not change.
- §15: marker reads reuse the gate's shared `gate_fetch_marker_comments`
  comments read; the infra classification reuses the run's jobs and
  annotations reads that `ci_cancelled_rerun.py` already makes.
- §27: `review_autofix.yml` is 397,780 bytes. Any growth past 480,000 bytes
  moves `run:` bodies into `scripts/` in the same PR; prefer putting all new
  logic in the existing step scripts.
- §33: no new job or workflow (see §18.E above).
- Trust: every marker that changes review behaviour is honoured only when
  authored by the pipeline login (`GH_PAT`'s `GET /user`), the same rule the
  freshness markers use.

## Approach

### Phase 1 — Infra failures re-run; the hand-back reviews the red head

1. `scripts/ci_cancelled_rerun.py`: also accept final-CI runs. A completed
   `workflow_dispatch` run of the CI workflow whose `display_title` matches
   `^CI \[pr:([1-9][0-9]*)\]$` (the full suite only; never ` [sync-quick]`),
   whose `head_sha` equals PR `<n>`'s current head in the sweep snapshot, and
   whose `run_attempt` is 1, is a candidate like an associated `pull_request`
   run. A failed one is re-run only when `classify_runner_shutdown` returns
   `shutdown`, `action_resolution`, `checkout_fetch` or `abandoned`, honouring
   the existing per-cause switches. Log reasons stay the existing ones, plus
   `final_ci=true` on the decision line.
2. `scripts/ci_cancelled_rerun.py`: add a CLI subcommand
   `classify-run --repo <o/r> --run-id <id>` that prints the
   `classify_runner_shutdown` verdict (one word) and exits 0.
3. `scripts/review_final_ci_failed.sh`: before the marker POST, run
   `classify-run` for the failed run (run id from a new env var
   `FINAL_CI_FAILED_RUN_ID` set to `${{ github.run_id }}` in `ci.yml`'s
   `final-ci-failed` job). On an infra verdict, post no marker, dispatch
   nothing and log `REVIEW_FINAL_CI_FAILED ... outcome=infra_rerun_deferred
   reason=<verdict>`; the next sweep tick re-runs it (item 1). Any other verdict
   or a classifier error keeps today's hand-back. Switch:
   `FINAL_CI_FAILED_INFRA_RERUN_ENABLED` (default `true`).
4. `scripts/review_autofix_step_evaluate_gate.sh`: a trusted
   `ai:final-ci-failed:v1` marker for the current head invalidates the clean
   same-head skip (`AUTOFIX_GATE_CLEAN_SAME_HEAD_CHECK outcome=no_skip
   reason=final_ci_failed`) and the terminal same-head skip (new reason token
   `final_ci_failed` on its query log), exactly like
   `ai:freshness-ci-failed:v1` does for the clean skip. Add
   `<!-- ai:final-ci-failed:v1 ` to the `gate_fetch_marker_comments` body
   filter so the shared read keeps those bodies.

After Phase 1 alone, the hand-back's review runs today's full review and
autofix round on the red head (failed check-runs reach the editor through
`collect_pr_check_runs_context.py`), which is the behaviour the hand-back was
designed for.

### Phase 2 — CI-fix commit instead of the reviewer panel

1. Gate (`review_autofix_step_evaluate_gate.sh`): set a new gate output
   `final_ci_repair=true` when all hold:
   - `FINAL_CI_REPAIR_ENABLED` (new repo variable, default `true`) is on;
   - a trusted `ai:final-ci-failed:v1` marker names the current head;
   - the head's newest `ai-review/head-gate` status is `success` and was
     posted by the pipeline login;
   - the head is not `orchestrator/project-*` (that path keeps
     `post_sync_test_repair`);
   - no trusted `ai:final-ci-repair:v1` marker has `from=<current head>`
     (one per head), and fewer than 2 trusted repair markers exist for the PR
     (Q7: A).
   Otherwise the run continues as in Phase 1 (full review), and when a cap was
   the reason it logs `FINAL_CI_REPAIR pr= head= outcome=skipped
   reason=head_cap|pr_cap`.
2. `review_autofix.yml`: widen the `if:` of "Integration post-sync test repair"
   to `(inputs.post_sync_test_repair && startsWith(env.TARGET_BRANCH,
   'orchestrator/project-')) || needs.gate.outputs.final_ci_repair == 'true'`
   (keeping the other guards) and pass `FINAL_CI_REPAIR=true` in its `env:`.
3. `review_autofix_step_prepush_test_gate.sh` (integration mode): with
   `FINAL_CI_REPAIR=true` accept any non-orchestrator head, set
   `INTEGRATION_TEST_REPAIR_ACTIVE=true` (reviewers and editor skip, as today)
   and gate the head against its merge-base with `BASE_BRANCH`. When the gate
   reproduces failures the head caused, call
   `review_conflict_resolve.sh --test-repair --final-ci <result.json>`.
   When it reproduces none (a CI-only failure), clear
   `INTEGRATION_TEST_REPAIR_ACTIVE` before the reviewer steps so the same run
   falls back to the full review round, and log `FINAL_CI_REPAIR ...
   outcome=fallback reason=not_reproduced`.
4. `review_conflict_resolve.sh`: `--final-ci` selects a context function that
   needs no orchestrator state: PR title and body, the failing check-runs
   (`collect_pr_check_runs_context.py` output already produced for the run),
   and no intent fingerprints (the fingerprint hard gate is skipped only in
   this mode). Commit subject: `[ai-merge-resolve] repair final CI failures`
   (the existing pipeline marker, so iteration counting and
   `sync_claude_live_copies.py` need no change).
5. After a successful push, post one comment ending in
   `<!-- ai:final-ci-repair:v1 pr=<n> from=<approved head> repair=<new head> attempt=<k> -->`
   (pipeline login, one POST attempt like the sync marker). A repair that
   commits nothing logs `outcome=no_change` and falls back as in item 3.

After Phase 2 alone, the repair head gets a normal full review (Phase 3 makes
it a fix-only review).

### Phase 3 — Review the fix diff only

1. `scripts/review_head_gate.sh`: new mode `final-ci-repair`, modelled on
   `freshness-sync`. It recognises a head when a trusted
   `ai:final-ci-repair:v1` marker has `repair=<head>`, the head commit has
   exactly one parent equal to `from`, the head commit's committer is the
   pipeline identity, and `from` has a pipeline-authored `ai-review/head-gate`
   `success` status. It outputs `review_diff_base_sha=<from>`. Any check that
   fails or cannot be read means a normal full review (fail safe).
2. `review_autofix_step_generate_diff_context.sh`: when
   `REVIEW_DIFF_BASE_SHA` is a 40-hex ancestor of HEAD, the reviewer diff and
   file list are `REVIEW_DIFF_BASE_SHA..HEAD` instead of the merge-base range;
   otherwise unchanged. Log `REVIEW_DIFF_CONTEXT range=delta|merge_base`.
3. The panel, consolidator, editor and head-gate posting run unchanged (Q8: A).
   A clean result posts `ai-review/head-gate` success on the repair head, and
   `_pr_ensure_final_ci` dispatches its final CI as for any approved head.
4. Switch: `FINAL_CI_REPAIR_DELTA_REVIEW_ENABLED` (default `true`).

## Phases & Merge Strategy

| Phase | Scope | Independent because |
| --- | --- | --- |
| 1 | infra re-run of final-CI runs; hand-back marker invalidates same-head skips | uses only existing markers and the existing review round |
| 2 | gate output `final_ci_repair`, widened test-repair step, `--final-ci` context, repair marker | without Phase 1 the marker still exists (hand-back posts it); without Phase 3 the repair head gets a full review |
| 3 | `final-ci-repair` head-gate mode and delta diff range | inert until a trusted repair marker exists |

Any merge order is safe; each phase has its own default-on switch for
rollback.

## Implementation Steps

Per phase, in the order of the Approach items above. Each phase PR also adds
its README rows (new repo variables), an `agents.d/` fragment (§30) for the
workflow-architecture notes and new log keys (`FINAL_CI_REPAIR`,
`REVIEW_DIFF_CONTEXT`, new `outcome=`/`reason=` tokens), and a `changelog.d/`
fragment (§20).

## Files & Modules

- Phase 1: `scripts/ci_cancelled_rerun.py`, `scripts/review_final_ci_failed.sh`,
  `.github/workflows/ci.yml` (`final-ci-failed` env only),
  `scripts/review_autofix_step_evaluate_gate.sh`, tests below, README,
  `agents.d/`, `changelog.d/`.
- Phase 2: `scripts/review_autofix_step_evaluate_gate.sh`,
  `.github/workflows/review_autofix.yml` (gate output and one `if:`/`env:`),
  `scripts/review_autofix_step_prepush_test_gate.sh`,
  `scripts/review_conflict_resolve.sh`, tests, docs.
- Phase 3: `scripts/review_head_gate.sh`,
  `scripts/review_autofix_step_generate_diff_context.sh`,
  `.github/workflows/review_autofix.yml` (pass the gate output to the diff
  step), tests, docs.

## Data Model / Index Changes

None.

## Tests

- Phase 1: `tests/test_ci_cancelled_rerun.py` (final-CI run accepted only
  for the current head, attempt 1, no `[sync-quick]`, infra verdicts only;
  `classify-run` CLI); `tests/test_review_final_ci.py` (infra verdict posts no
  marker and dispatches nothing; classifier error keeps the hand-back);
  `tests/test_review_autofix_clean_same_head_gate.py` and
  `tests/test_review_autofix_terminal_same_head_gate.py` for
  `reason=final_ci_failed` on both same-head skips and that an untrusted
  marker does not invalidate.
- Phase 2: gate output truth table (each condition, both caps, orchestrator
  head excluded); test-repair step `if:` pinned; prepush integration mode with
  `FINAL_CI_REPAIR=true` on a non-orchestrator head (reproduced, not
  reproduced, no change); `--final-ci` context needs no tracking issue;
  marker format.
- Phase 3: `tests/test_review_head_gate.py`: `final-ci-repair` accepts only the exact
  marker/parent/committer/gate combination and fails safe on read errors;
  diff context uses the delta range only for a valid ancestor.
- Every phase: `tests/test_workflow_file_size_limit.py`,
  `python3 scripts/assemble_agents.py check --repo-root .`.

## Risks & Mitigations

- **A delta review misses an interaction with the approved code.** Mitigated:
  the full final CI runs on the repair head before merge, and the panel still
  sees the complete fix diff; two repairs per PR at most, then full review.
- **A forged marker skips review of the whole PR.** Mitigated: markers count
  only from the pipeline login, and Phase 3 also checks parentage, committer
  and the parent's pipeline-authored gate status.
- **An infra verdict is wrong and the run is re-run instead of fixed.**
  Bounded: attempt 1 only; a second failure goes to the hand-back.
- **Overlap with #9652's base sync.** The base sync waits 30 minutes and skips
  heads with an active review run, so the repair goes first; a later base
  sync builds on the repair head.

## Rollout

Default on per phase (Q9: A). Rollback by setting the phase's repo variable to
`false`: `FINAL_CI_FAILED_INFRA_RERUN_ENABLED`, `FINAL_CI_REPAIR_ENABLED`,
`FINAL_CI_REPAIR_DELTA_REVIEW_ENABLED`. Verify after each merge from the
sweep and review logs: `REVIEW_FINAL_CI_FAILED outcome=infra_rerun_deferred`,
`FINAL_CI_REPAIR outcome=`, `REVIEW_DIFF_CONTEXT range=delta`.

## References

- PR #9649 (sweep skip, refused-dispatch base update), #9652 (base sync after
  failed final CI), #9648 (final-CI run binding), #9535 / #9563
  (cut-ci-runner-time Phase 4), #8210 (post-sync test repair), #8952 (clean
  same-head skip), #7084 (freshness-sync markers).
- `docs/plans/cut-ci-runner-time-plan.md`.

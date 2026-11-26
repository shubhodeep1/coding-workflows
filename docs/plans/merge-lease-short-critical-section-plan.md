# Merge lease as a short critical section

## Summary

Make a merge-lease hold last minutes instead of hours: grant a slot only to a
PR that is ready to merge, send a conflicting PR to the resolver without a
slot, check a clean update from the base with a small delta suite instead of
the full suite, review only the resolution after a conflict, and finish the
update, the check and the merge in the job that holds the lease.

## Automation (§18.E)

- **Scripts:** extends existing scripts only (`scripts/pr_checks_lib.sh`,
  `scripts/review_merge_train.sh`, `scripts/ci_sync_quick.sh`,
  `scripts/review_head_gate.sh`, the review gate and diff-context step
  scripts). No new script, no manual script (§18.A).
- **Entry points (all existing):** the merge-lease tick in
  `.github/workflows/orchestrate_poll.yml` (schedule) and
  `.github/workflows/cancel_on_pr_close.yml` (PR closed); the
  `deterministic-skip-merge` and `codex-agent` jobs of
  `.github/workflows/review_autofix.yml` (PR push, review dispatch); the
  `tests-sync-quick` job of `.github/workflows/ci.yml` (final-CI dispatch).
- **Supervisor:** none new. The tick stays the backstop for every new path.
- **DB work:** none.
- **Registry (`docs/scripts-pending-removal.md`):** no entry, because no
  single-use or long-running script is added.

## Decisions

- **D1 (owner, Q168: A, 2026-11-24).** After a clean update from the base, the
  pre-merge check is a delta suite: the static checks, the PR's own tests and
  the tests selected for the files both sides changed. The full suite runs on
  the base after the merge.
- **D2 (owner, Q169: A).** When the full suite on the base fails after a
  delta-checked merge, the pipeline alerts and files a repair issue as it does
  today (the workflow failure heal intake). No automatic revert and no pause of
  lease grants.
- **D3 (owner, Q170: A).** The plan is handed to the pipeline only after the
  in-flight lease fixes #9996, #10045 and #10046 are merged, because they edit
  the same files.
- **D4 (derived).** A resolution-only review keeps the reviewer panel, the
  editor and the head-gate posting unchanged and narrows only the reviewers'
  primary diff, as the final-CI repair delta review does
  (`docs/plans/final-ci-failure-ci-fix-plan.md` Phase 3, Q8: A).
- **D5 (derived from D1).** Every new switch ships with default `true`. Each
  one falls back to today's behaviour on any read or verification failure, and
  setting it to `false` restores today's behaviour exactly.
- **D6 (derived).** A holder that conflicts with the base is released at most
  once per PR. The second time it keeps its slot through the resolution, as
  today, so a PR cannot be starved by repeated conflicts.
- **D7 (derived).** The delta suite is this repository's `ci.yml`. A consumer
  has final CI only when it sets `REVIEW_FINAL_CI_WORKFLOW` (empty by
  default), and then keeps the full run until its workflow accepts the
  `sync_quick` input. The dispatcher already retries a refused dispatch
  without the input.

## Context

Measured on `shubhodeep1/coding-workflows`, 2026-11-24, with
`MERGE_LEASE_MAX_HOLDERS=2`:

| Holder | Slot held (UTC) | What the hold waited for |
| --- | --- | --- |
| #9854 | 22:20 to 03:13 (4 h 53 min) | update, security audit, full final CI, merge run, each queued separately |
| #9737 | 22:31 to 03:18 (4 h 47 min) | the same sequence |
| #9489 | 05:58 to 09:37 (3 h 39 min) | the same sequence |
| #9897 | 03:14 to 07:33 (4 h 19 min) | held while conflicting with `main`, then waiting for a queued review; released unmerged at the TTL |
| #9502 | 07:35 to 11:35 (4 h) | final CI still queued at the TTL; released unmerged |

What the code does today (line numbers are `main` at `a9703cc0`):

- Review and final CI already run before a PR asks for the lease.
- The tick grants on the head-gate alone. `_merge_lease_pr_eligible`
  (`scripts/pr_checks_lib.sh:3608`) rejects a waiter only for `draft`,
  `skip_ai`, `no_head_gate` or `final_ci_failed`. A waiter whose final CI is
  still pending, or that conflicts with the base, is granted.
- `_pr_base_fresh_for_merge` (`scripts/pr_checks_lib.sh:2238`) lets a PR merge
  with no update when the base moved only under files the PR does not touch.
  Otherwise the holder updates its branch and the run ends.
- `_pr_base_sync_dispatch_final_ci` (`scripts/pr_checks_lib.sh:2160`)
  dispatches final CI on the update head. It is the full run (38 jobs in
  `CI [pr:9897]`) unless every overlapping path is CI wiring
  (`scripts/ci_sync_quick.sh`, operator decision Q3: B of 2026-11-17), in
  which case it is the three-job quick run (`static-checks`,
  `tests-sync-quick`, `lint`).
- The push of the update head starts a second review run. Its gate takes the
  freshness fast path (`review_head_gate.sh freshness-sync`,
  `det_skip_reason=freshness_sync`), and its `deterministic-skip-merge` job
  runs the security audit as a delta (`single_pass_base_sync_carry`), waits up
  to `AUTO_MERGE_CHECKS_WAIT_MINUTES` (45) for the checks and merges. A full
  final CI rarely finishes inside that wait, so a third run resumes the merge.
- A holder that conflicts keeps its slot while the conflict resolver, a full
  review of the whole PR and a full final CI run.

So one hold spans three or four separately queued runs plus a 38-job CI run,
and each of them waits for runners. Two slots held this way block every other
PR that shares a path with a holder.

## Goals

- A slot is granted only to a PR whose current head has a pipeline head-gate
  success, a successful `ci/final` (where final CI is enabled) and no conflict
  with the base. Verified by `MERGE_LEASE ... outcome=skipped
  reason=final_ci_pending|not_mergeable` log lines and unit tests.
- A holder that conflicts with the base gives its slot back within one tick
  (once per PR, D6) when another waiter is ready.
- A clean update whose approved head passed the full suite is checked by the
  three-job delta run, not the full run. Verified by run name
  `CI [pr:<n>] [sync-quick]` and `CI_SYNC_QUICK mode=verify outcome=ok
  reason=delta_overlap`.
- A verified conflict resolution is reviewed on its resolution diff only and
  gets the delta run. Verified by `REVIEW_DIFF_CONTEXT range=remerge`.
- For a holder that needs a clean update, the update, the head-gate carry, the
  security audit carry, the wait for the delta run and the merge happen in one
  job. Target: median `held_s` of such holders at or below 2,700 seconds (45
  minutes) over 20 merges, against 13,000 to 17,600 seconds today. The work
  itself is 15 to 25 minutes; the rest is runner wait for three jobs.
- Every step has a switch, and with all switches `false` the pipeline behaves
  exactly as it does today.

## Non-goals

- Changing `MERGE_LEASE_MAX_HOLDERS`, `MERGE_LEASE_TTL_MINUTES` or
  `MERGE_LEASE_IDLE_MINUTES` defaults (#10046 and the owner's later decision).
- The fixes already in flight: #9987/#9996 (judge and lease deadlock),
  #10017/#10021 (audit concurrency for holders), #10023/#10034 (final-CI
  repair race), #10044/#10045 (un-park after a TTL release), #9993/#9997
  (stale `ci.yml` on old branches), #9991/#10000 (repeat reviews).
- Automatic revert of a merge, or pausing grants, after a red base (D2).
- A delta suite in consumer repositories' own CI workflows (D7).
- The poller's merge paths in `scripts/orchestrate_poll_process.sh`. They keep
  today's update-and-return behaviour; the poller must not wait 45 minutes.
- GitHub's native merge queue.

## Constraints

- **§1 security.** PR code still runs only in `ci.yml` jobs with read-only
  permissions. The job that merges never runs PR tests. The review carry for
  an update head keeps the existing proof (two-parent merge made by GitHub,
  trusted marker), and the new resolution carry needs an equivalent proof.
- **§5.** Extend the sync-quick run, the freshness fast path, the final-CI
  repair delta review and the security audit carry. Add no parallel mechanism.
- **§6.** No existing identifier, log key, reason token, input, output or
  record field is renamed or removed. `ci_sync_quick.sh classify` keeps its
  two outputs. The lease record format is unchanged. New names in this plan
  were checked against `main` at `a9703cc0` and are unused.
- **§15.** New reads are listed per phase with the reason no existing read
  covers them.
- **§27.** `review_autofix.yml` is 421,750 bytes. New logic goes into
  `scripts/`; workflow edits are limited to `env:` lines and one-line calls.
  Recheck with `wc -c` in every phase; stay under 480,000.
- **§33.** No new job or workflow. Jobs per lease hold are given under
  Approach.
- **§20, §30.** One `changelog.d/` fragment and one `agents.d/` fragment per
  phase PR. README environment tables are updated in the same PR.
- **§14.** `scripts/` and `review_autofix.yml` reach every consumer in
  `.github/ai/consumer_repos.json` at the next `@stable` release. `ci.yml` is
  this repository's own CI and does not propagate.

## Approach

Four independent changes, each behind its own switch.

1. **Ready-first grant.** The tick grants a free slot to the first waiter in
   queue order that is ready: head-gate success, `ci/final` success on the
   current head when final CI is enabled, and not in conflict. A waiter that
   is approved but has no final CI run gets one dispatched by the tick through
   the existing `_pr_ensure_final_ci`. A holder found in conflict is released
   with a hard skip entry (it comes back when the resolver changes its head)
   and labelled so it is released only once (D6). If waiters exist, none is
   ready and a slot has been free for `MERGE_LEASE_IDLE_MINUTES`, the tick
   grants by today's rule, so the new rule can never stop merges.
2. **Delta suite after a clean update.** The existing quick run is used for
   any overlap, not only CI wiring, when the approved head passed a full final
   CI run. `tests-sync-quick` additionally runs the tests the existing
   selector (`workflow-templates/.claude/hooks/prepush_affected_tests.py
   select`) picks for the files both sides changed. A selection above a cap
   falls back to the full run.
3. **Resolution-only review.** When the resolver resolves a conflict on an
   approved head, it records a trusted marker. The gate verifies the head is
   exactly that two-parent merge and gives the reviewers
   `git show --remerge-diff` of it (the difference between the resolver's
   result and a mechanical merge) as their primary diff. The security audit
   carries the earlier audit and audits the same diff. Final CI for that head
   is the delta run.
4. **One job per hold.** After a clean update that got the quick or delta
   run, the job that made the update continues instead of ending: it verifies
   the update head, carries the head-gate, runs the audit carry, waits for
   the run and merges. The review run started by the push of the update head
   sees the owner run in the marker and exits at its gate.

Jobs per hold that needs an update (§33):

| | Before | After |
| --- | --- | --- |
| Review workflow runs | 3 to 4 (grant dispatch, update-head run, merge resume, often a judge run) | 1, plus a gate-only run that exits |
| Final CI jobs on the update head | 38 (full) | 3 (`static-checks`, `tests-sync-quick`, `lint`) |
| New jobs or workflows | n/a | 0 |

Alternatives considered:

- *Run the delta tests inside the merge job.* Rejected: it would run PR code
  in a job that holds a write token, and the static checks are 28 inline
  steps of `ci.yml` whose last job posts the required `ci/final` status.
- *GitHub merge queue.* Rejected: it needs `merge_group` support in every
  required check and replaces the lease, the soft-path rule and the
  no-shared-path bypass.
- *Shorter TTL only.* Rejected: it releases holders that are waiting for
  runners (the #9502 case) and merges nothing sooner.

## Phases & Merge Strategy

Each phase is one PR, mergeable in any order, complete and safe on its own.
No phase needs another to compile, to pass tests or to behave correctly.

1. **Phase 1: ready-first grant and conflict release.**
   Files: `scripts/pr_checks_lib.sh`, `scripts/review_merge_train.sh`, the
   `env:` blocks of `orchestrate_poll.yml`, `cancel_on_pr_close.yml` and
   `review_autofix.yml`, README, tests, fragments.
   Done: unit tests show the grant decisions in the table under Tests; a tick
   with both switches `false` writes the same lease record and log lines as
   `main` does today.
   Rollback: set `MERGE_LEASE_READY_GRANT_ENABLED=false` and
   `MERGE_LEASE_CONFLICT_RELEASE_ENABLED=false`, or revert the PR.
2. **Phase 2: delta suite after a clean update.**
   Files: `scripts/ci_sync_quick.sh`, `scripts/pr_checks_lib.sh`
   (`_pr_base_sync_dispatch_final_ci`), `.github/workflows/ci.yml`
   (`tests-sync-quick`, `lint`), README, tests, fragments.
   Done: `ci_sync_quick.sh verify` accepts a non-wiring overlap only with the
   switch on and a full-green approved head; with the switch `false` its
   verdicts are identical to today's on the existing test fixtures.
   Rollback: set `MERGE_BASE_SYNC_DELTA_CI_ENABLED=false`, or revert the PR.
3. **Phase 3: resolution-only review and delta CI after a conflict.**
   Files: `scripts/review_head_gate.sh`, the resolver's push step,
   `scripts/review_autofix_step_evaluate_gate.sh`,
   `scripts/review_autofix_step_generate_diff_context.sh`,
   `scripts/review_single_issue_security_pass.sh`,
   `scripts/pr_checks_lib.sh` (`_pr_ensure_final_ci` caller flag), README,
   tests, fragments.
   Done: a head that is a verified resolution is reviewed with
   `range=remerge`; any head that fails one check is reviewed in full.
   Without Phase 2 on the branch the delta run is not requested (capability
   probe), so the phase is correct alone.
   Rollback: set `MERGE_RESOLVE_DELTA_REVIEW_ENABLED=false`, or revert the PR.
4. **Phase 4: update, check and merge in one job.**
   Files: `scripts/pr_checks_lib.sh`, `scripts/review_head_gate.sh` (marker
   field read), `scripts/review_autofix_step_evaluate_gate.sh`,
   `scripts/review_enable_auto_merge.sh`, `scripts/review_rb_judge.sh`, the
   `deterministic-skip-merge` job of `review_autofix.yml` (calls only), README,
   tests, fragments.
   Done: with the switch on, a holder whose update got a quick run merges in
   the same job; any failed step returns today's "updated, waiting" result.
   Without Phase 2 the path triggers only for CI-wiring overlaps, which is
   correct alone.
   Rollback: set `MERGE_LEASE_INLINE_SYNC_ENABLED=false`, or revert the PR.

## Implementation Steps

### Phase 1: ready-first grant and conflict release

1. `scripts/pr_checks_lib.sh`: add `_merge_lease_pr_ready <pr> <head_sha>
   <base>`. It is called only for a waiter that `_merge_lease_pr_eligible`
   already accepted with `MERGE_LEASE_GATE_CONFIRMED=true`. Returns 0 when
   ready or when a read fails (fail-open), 1 otherwise with
   `MERGE_LEASE_NOT_READY_REASON` set to `final_ci_pending` or
   `not_mergeable`.
   - `ci/final`: read from the commit-statuses response
     `_merge_lease_pr_eligible` already fetched (keep it in a variable; no new
     call). Required only when `REVIEW_FINAL_CI_WORKFLOW` is non-empty (final
     CI is off when it is empty, the default in consumers). Newest `ci/final`
     status `success` passes; absent, `pending`, `failure` or `error` gives
     `final_ci_pending`.
   - Mergeability: one `GET repos/<repo>/pulls/<pr>` (§15: the open-PR listing
     does not carry `mergeable`; the read is made only for a candidate that
     passed every cheaper check, at most 10 per base per tick). `mergeable ==
     false` or `mergeable_state == "dirty"` gives `not_mergeable`; `null`
     passes.
2. `scripts/review_merge_train.sh` (grant paths at 2178 and 2463, single- and
   multi-holder): when `MERGE_LEASE_READY_GRANT_ENABLED` is on, call
   `_merge_lease_pr_ready` after eligibility. A not-ready candidate keeps its
   wait label, gets no skip entry, and logs `MERGE_LEASE pr=<n> base=<ref>
   mode=tick outcome=skipped reason=<final_ci_pending|not_mergeable>`. The
   tick moves to the next candidate.
3. Same file: for a candidate skipped with `final_ci_pending` whose head has
   no final-CI run (queued, running or completed), call `_pr_ensure_final_ci`
   once (its own dedupe applies). At most one such dispatch per base per tick.
4. Same file, liveness fallback: when waiters exist, none is ready and the
   base has had a free slot for at least `MERGE_LEASE_IDLE_MINUTES` (use the
   record's last `updated=` time), grant the first eligible waiter by today's
   rule and log `outcome=granted reason=ready_fallback`.
5. Same file (holder evaluation at 2040 and 2339): when
   `MERGE_LEASE_CONFLICT_RELEASE_ENABLED` is on, a holder whose PR is
   `mergeable == false` (the same `pulls/<pr>` read, made only when the
   listing's cheap checks pass), that does not carry the label
   `MERGE_LEASE_CONFLICT_RELEASED_LABEL`, while at least one other waiter is
   ready, is released with reason `released_conflict` (a hard skip entry, so
   it returns when its head changes) and gets the label. A holder that already
   carries the label keeps its slot as today.
6. Workflows: add the three variables to the `env:` blocks that already carry
   `MERGE_LEASE_IDLE_MINUTES` (`orchestrate_poll.yml`,
   `cancel_on_pr_close.yml`, `review_autofix.yml`), and
   `REVIEW_FINAL_CI_WORKFLOW` (`${{ vars.REVIEW_FINAL_CI_WORKFLOW || '' }}`)
   to any tick step among them that lacks it.
   Defaults: `MERGE_LEASE_READY_GRANT_ENABLED` `true`,
   `MERGE_LEASE_CONFLICT_RELEASE_ENABLED` `true`,
   `MERGE_LEASE_CONFLICT_RELEASED_LABEL` `ai:merge-lease-conflict-released`.
7. README rows for the three variables, an `agents.d/` fragment (section
   "Workflow architecture"; the `MERGE_LEASE` prefix is already registered),
   a `changelog.d/` fragment.

### Phase 2: delta suite after a clean update

1. `scripts/ci_sync_quick.sh`: add the subcommand `delta-supported` (prints
   `delta_supported`, exits 0) so other code can probe the capability.
2. Same file, `verify`: when every overlap path is CI wiring, behave as
   today. Otherwise pass only when all hold: `MERGE_BASE_SYNC_DELTA_CI_ENABLED`
   is on; the update head is a two-parent merge `[approved head, base tip]`;
   the approved head passed a full final CI run (`full-ci-green`); the
   selection in step 3 has at most `MERGE_BASE_SYNC_DELTA_MAX_TEST_FILES`
   test files. Log `CI_SYNC_QUICK mode=verify outcome=ok reason=delta_overlap`
   or `outcome=fail reason=<delta_disabled|delta_selection_too_large|...>`.
3. Same file, `run-tests`: after the PR's own tests and the CI-structure
   tests, write the both-sides-changed paths to a file and run
   `python3 workflow-templates/.claude/hooks/prepush_affected_tests.py select
   --repo-root . --changed-files-file <file> --out <json>`; run the selected
   test files that are not already in the run (pytest for `.py`, bash for
   `.sh`). A selector error fails the job.
4. `scripts/pr_checks_lib.sh` `_pr_base_sync_dispatch_final_ci`: when the
   classifier says `other`, the switch is on, `ci_sync_quick.sh
   delta-supported` exits 0 and `full-ci-green` passes for the approved head,
   set `sync_ci_quick="true"` with reason `delta_overlap`. Everything else is
   unchanged, including the retry without `sync_quick` when a dispatch is
   refused and the full run after `sync_quick_failed`.
5. `.github/workflows/ci.yml`: pass `MERGE_BASE_SYNC_DELTA_CI_ENABLED`
   (`${{ vars.MERGE_BASE_SYNC_DELTA_CI_ENABLED || 'true' }}`) and
   `MERGE_BASE_SYNC_DELTA_MAX_TEST_FILES` (default `60`) to the two
   `tests-sync-quick` steps; raise that job's `timeout-minutes` from 20 to 30.
   No new input, no new job; the run name stays `CI [pr:<n>] [sync-quick]`.
6. README rows, `agents.d/` fragment, `changelog.d/` fragment. Update
   `tests/test_ci_sync_quick.py` and the CI-structure tests for the new
   timeout and env lines.

### Phase 3: resolution-only review and delta CI after a conflict

1. Resolver push step (the step that pushes the `[ai-merge-resolve]` commit
   made at `scripts/review_conflict_prepare.sh:580`): after a successful push,
   when the pre-resolution head has a pipeline head-gate success, post the
   workflow-authored marker comment
   `<!-- ai:merge-resolve from=<pre-resolution head> resolve=<new head> base=<base tip> -->`
   with the helper `_pr_base_sync_marker_post` uses. No marker in any other
   case.
2. `scripts/review_head_gate.sh`: add the subcommand `merge-resolve <repo>
   <pr> <head sha> <base ref> <author login> <comments json file>`. It prints
   `merge_resolve_from=<sha>` only when all hold: a marker by the trusted
   author names `resolve=<head>`; the head commit has exactly two parents
   `[from, base]`; its GitHub-attributed author or committer is the pipeline
   identity (the guard `final-ci-repair` uses); `from` has a pipeline
   head-gate success; the base parent is contained in the base branch. Log
   `REVIEW_HEAD_GATE mode=merge_resolve outcome=delta|review reason=<token>`.
   Cost: the reads `final-ci-repair` makes, only when a marker names the head.
3. `scripts/review_autofix_step_evaluate_gate.sh`, beside the final-CI repair
   block (about line 1945): when `MERGE_RESOLVE_DELTA_REVIEW_ENABLED` is on
   and the helper verified the head, write the gate output
   `review_remerge_head=<head>`. `review_diff_base_sha` is not reused.
4. `scripts/review_autofix_step_generate_diff_context.sh`: when
   `REVIEW_REMERGE_HEAD` is set, build `LAST_RUN_DIFF_FILE` from
   `git show --remerge-diff --format= <head>` (fetch the two parents and
   deepen until the merge base is present), with its file list and stat. An
   empty diff, a git error or git older than 2.36 keeps today's diff and logs
   `REVIEW_DIFF_CONTEXT range=merge_base reason=remerge_failed`; success logs
   `range=remerge base=<from>`. `PR_DIFF_FILE` and `PR_CHANGED_FILES_FILE`
   keep the whole PR (D4).
5. `scripts/review_single_issue_security_pass.sh`
   (`single_pass_base_sync_carry`): accept a verified resolution head like a
   base-sync head: carry the audit of `from` and audit the remerge diff.
6. Final CI for a resolution head: where the review run calls
   `_pr_ensure_final_ci` after approval, set `PR_FINAL_CI_SYNC_QUICK=true`
   only when the approved head is still exactly the verified resolution
   commit, `ci_sync_quick.sh delta-supported` exits 0, the delta switch is on
   and `full-ci-green` passes for `from`. Any commit on top of the resolution
   gets the full run.
7. Workflow `env:` line for `MERGE_RESOLVE_DELTA_REVIEW_ENABLED` (default
   `true`) and the new gate output, README row, fragments.

### Phase 4: update, check and merge in one job

1. `scripts/pr_checks_lib.sh` `_pr_base_sync_marker_post`: when
   `MERGE_LEASE_INLINE_SYNC_ENABLED` is on and the caller exported
   `PR_BASE_SYNC_INLINE_CALLER=true`, append ` inline_run=<GITHUB_RUN_ID>` to
   the marker body. Existing marker fields and their order are unchanged.
2. Same file: add `_pr_base_sync_inline_merge <repo> <pr> <approved_head>
   <update_head> <base_ref>`. It runs only when the update was made by this
   job and `_pr_base_sync_dispatch_final_ci` dispatched a quick run
   (`sync_ci_quick=true`). Steps, each returning 1 (today's "updated,
   waiting" result) on failure:
   1. fetch the marker comments and run `review_head_gate.sh freshness-sync`
      on the update head; require the skip verdict;
   2. post the head-gate success on the update head through the function the
      `freshness_sync` deterministic skip uses;
   3. run the security audit carry exactly as `deterministic-skip-merge` does
      when `AUTOFIX_SKIP_RUNS_SECURITY_PASS=true`;
   4. `_pr_wait_for_required_checks` on the update head for
      `AUTO_MERGE_CHECKS_WAIT_MINUTES`;
   5. return 0 with the update head exported, so the caller's existing merge
      call (with its head match) merges it.
   On a return of 1 after step 1 started, dispatch the review for the PR once
   (the call `_mt_dispatch_review` makes), so the update head is never left
   without an owner. Log `MERGE_BASE_FRESHNESS pr=<n> outcome=inline_merge
   reason=<ok|step token>`.
3. Call sites: the two `_pr_base_fresh_for_merge` calls of the
   `deterministic-skip-merge` job (`review_autofix.yml` 1509 and 1552),
   `scripts/review_enable_auto_merge.sh` and `scripts/review_rb_judge.sh`.
   Each exports `PR_BASE_SYNC_INLINE_CALLER=true` and, when
   `_pr_base_fresh_for_merge` returns 1 after an update by this job, calls the
   new function and continues to its merge on 0. The poller's call sites are
   not changed.
4. `scripts/review_autofix_step_evaluate_gate.sh`: in the freshness fast-path
   block (about line 1180), when the trusted marker for the current head
   carries `inline_run=<id>`, read that run once
   (`GET actions/runs/<id>`; §15: no existing read has another run's status,
   and it is made only in this case). While it is `queued` or `in_progress`,
   set `SHOULD_RUN=false`, `SKIP_REASON=lease_inline_owner_active` and log
   `AUTOFIX_GATE_DET_SKIP_EVAL pr=<n> skip=true reason=lease_inline_owner`. A
   completed run, or a failed read, keeps today's path.
5. Workflow `env:` line for `MERGE_LEASE_INLINE_SYNC_ENABLED` (default
   `true`), README row, fragments. Check `wc -c
   .github/workflows/review_autofix.yml`.

## Files & Modules

- `scripts/pr_checks_lib.sh` (Phases 1, 2, 3, 4)
- `scripts/review_merge_train.sh` (Phase 1)
- `scripts/ci_sync_quick.sh` (Phase 2)
- `scripts/review_head_gate.sh` (Phases 3, 4)
- `scripts/review_autofix_step_evaluate_gate.sh` (Phases 3, 4)
- `scripts/review_autofix_step_generate_diff_context.sh` (Phase 3)
- `scripts/review_single_issue_security_pass.sh` (Phase 3)
- `scripts/review_enable_auto_merge.sh`, `scripts/review_rb_judge.sh` (Phase 4)
- The resolver push step script (Phase 3)
- `.github/workflows/review_autofix.yml`, `orchestrate_poll.yml`,
  `cancel_on_pr_close.yml` (`env:` lines and calls), `ci.yml` (Phase 2)
- `README.md` (environment tables)
- `tests/test_merge_lease_ready_grant.py` `[new]`,
  `tests/test_ci_sync_quick_delta.py` `[new]`,
  `tests/test_merge_resolve_delta_review.py` `[new]`,
  `tests/test_merge_lease_inline_sync.py` `[new]`, each wired into `ci.yml`
- `agents.d/<issue>-<slug>.md` `[new]` and `changelog.d/<issue>-<slug>.md`
  `[new]`, one pair per phase

## Tests

Unit tests drive the shell functions with stubbed `gh` responses, as the
existing lease tests do.

- **Phase 1.** Grant decisions:

  | Waiter state | Expected |
  | --- | --- |
  | head-gate success, `ci/final` success, mergeable | granted |
  | head-gate success, `ci/final` pending | skipped `final_ci_pending`, label kept, no skip entry |
  | head-gate success, `ci/final` success, `mergeable=false` | skipped `not_mergeable` |
  | final CI disabled, head-gate success, mergeable | granted |
  | `pulls/<pr>` read fails | granted (fail-open) |
  | no waiter ready, slot free past the idle time | first eligible waiter granted, `ready_fallback` |
  | holder `mergeable=false`, no label, another waiter ready | released `released_conflict`, label added |
  | holder `mergeable=false`, label present | kept |
  | both switches `false` | record and log lines equal to `main` |

- **Phase 2.** `verify` fixtures: wiring-only overlap (pass, unchanged);
  other overlap with switch on and full-green head (pass, `delta_overlap`);
  switch off (fail as today); approved head not full-green (fail); selection
  above the cap (fail); update head with an extra commit (fail). `run-tests`
  runs the selected files once each. The dispatcher asks for the quick run
  only when the probe, the switch and `full-ci-green` all pass.
- **Phase 3.** `merge-resolve` accepts only the exact marker, parents,
  identity and approved `from`; each single violation gives `review`. The
  diff-context step produces the remerge diff on a fixture repository with a
  real conflict, and falls back on an empty diff. An extra commit on top of
  the resolution gets the full review and the full run.
- **Phase 4.** Each step of `_pr_base_sync_inline_merge` failing returns 1
  and dispatches the review once; the success path returns 0 with the update
  head. The gate exits for a live owner run and proceeds for a completed one
  or a failed read. The marker without the switch is byte-identical to
  today's.
- **End to end (after merge, from logs, no manual step):** `MERGE_LEASE ...
  outcome=released ... held_s=<s> merged=true` for holders that logged
  `outcome=inline_merge`, and the `CI [pr:<n>] [sync-quick]` run count.

## Risks & Mitigations

- **A delta-checked merge breaks the base where the full suite would have
  caught it.** ACCEPTED (D1, D2): the full suite runs on the base after every
  merge and a failure goes to the workflow failure heal intake. Mitigations:
  the approved head must have passed the full suite; the selection cap; a
  failed quick run is followed by the full run; one switch turns it off.
- **No waiter is ever ready (a bug or an API change), so nothing merges.**
  Every read fails open, and the `ready_fallback` grant applies after the
  idle time.
- **A PR is starved by repeated conflicts.** Released at most once per PR
  (D6), and only when another waiter is ready.
- **Two jobs act on the update head.** The marker's `inline_run` makes the
  push-triggered run exit at its gate; if the owner run dies, the gate sees a
  completed run and takes today's path, and the tick stays the backstop.
- **A resolver commit is given a narrow review it does not deserve.** The
  carry needs the trusted marker, the exact two parents, the pipeline
  identity and an approved first parent; the remerge diff shows every
  difference from a mechanical merge, including edits outside the conflicts.
- **The merge job waits 45 minutes holding one runner.** It already does
  today; the quick run it waits for is three jobs.
- **`review_autofix.yml` grows toward the §27 guard.** Logic lives in
  `scripts/`; each phase rechecks the size.
- **Old PR branches lack the new `ci_sync_quick.sh`.** The quick run tests
  the merge of the update head with the base, so the base's script is in the
  tree; a branch that edits that script conflicts or is reviewed.
- **In-flight fixes edit the same functions.** ACCEPTED (D3): hand-off waits
  for #9996, #10045 and #10046; line numbers are rechecked at implementation.

## Rollout

- Hand-off to the pipeline after #9996, #10045 and #10046 are merged (D3).
- This repository runs its workflows from `main`, so each phase is live here
  at merge. The switches are repository variables; none has to be set.
- Consumers in `.github/ai/consumer_repos.json` receive Phases 1, 3 and 4 at
  the next `@stable` release. Phase 2 affects them only when their final-CI
  workflow accepts `sync_quick` (D7).
- To turn a step off: set its variable to `false` (`MERGE_LEASE_READY_GRANT_ENABLED`,
  `MERGE_LEASE_CONFLICT_RELEASE_ENABLED`, `MERGE_BASE_SYNC_DELTA_CI_ENABLED`,
  `MERGE_RESOLVE_DELTA_REVIEW_ENABLED`, `MERGE_LEASE_INLINE_SYNC_ENABLED`).
- Watch after each phase: the `MERGE_LEASE_METRIC` lines for `held_s`, the
  `released_conflict` and `ready_fallback` counts, and red runs of `CI` on
  `main` whose first bad commit was a delta-checked merge.

## References

- Owner request and decisions Q168 to Q170, 2026-11-24.
- `docs/plans/merge-lease-parallel-merges-plan.md` (lease record, holders,
  revocation), `docs/plans/final-ci-failure-ci-fix-plan.md` (delta review).
- `scripts/ci_sync_quick.sh` (operator decision Q3: B of 2026-11-17).
- Issues and PRs: #9781, #9983/#9985, #9987/#9996, #10017/#10021,
  #10023/#10034, #10044/#10045, #10046, #9993/#9997, #9991/#10000, #7084,
  #8333.
- Holds measured: #9854, #9737, #9489, #9897, #9502 (2026-11-24).

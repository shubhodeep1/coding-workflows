# Pin the daily promote cycle to one candidate commit

## Summary

Make the daily `main` → `stable` promote cycle choose one candidate commit at
start, run the smoke gate, the proving run and the verifying run on exactly
that commit's pipeline code, and promote exactly that commit, even when `main`
moves during the cycle. Today any code commit that lands on `main`
mid-cycle defers the promotion, so `stable` has not moved since v1.36.1
(2026-10-17) while `main` keeps advancing.

## Context

The cycle (README "The daily promote cycle", section 12f;
`scripts/promote_main_cycle.sh`, `.github/workflows/promote-main-to-stable.yml`,
`scripts/apply_analysis_on_main.sh`, `scripts/orchestrate_poll_process.sh`) runs:

1. a smoke gate (`test-and-mark-stable.yml`, `gate_only=true`, dispatched with
   `--ref main`) whose head becomes `smoke_sha`;
2. a proving run (one analysis doc via `internal-orchestrate.yml`);
3. a verifying run, dispatched by `dispatch_comprehensive_verification_run`
   with `promote_sha` = the `main` tip right after the proving merge;
4. at the verifying run's ready-to-merge point,
   `comprehensive_promotion_gate_before_final_merge` checks
   `comprehensive_untested_commits_json(smoke_sha, promote_sha, proving_merge_sha)`
   and, if any unproven commit is in the range, logs
   `COMPREHENSIVE_PROMOTION_DEFERRED` and promotes nothing
   (`scripts/orchestrate_poll_process.sh`, "⏸️ Promotion deferred").

Nothing in that chain runs one fixed commit:

- all 13 `.github/workflows/internal-*.yml` wrappers call their reusable
  workflow `...@main`, and GitHub resolves `@main` when each job starts;
- inside this repository the reusable workflows take support scripts from
  `github.sha` (`SCRIPT_REF=${{ github.sha }}`, for example
  `orchestrate_poll.yml:125`), i.e. `main` at event time;
- `validate.yml:138` clones `main`; `review_autofix.yml:440-449` and
  `issue_pr_status.yml` read protected `main`'s tip for any branch ref.

So the proving and verifying runs execute whatever `main` is at each step, and
no single commit gets both the smoke gate and the end-to-end runs. That is why
the gate defers on any interleaved commit. Observed 2026-10-18: verifying run
#7614 deferred on 4 commits (`6b06834`, `165b7c5`, `34ad13f`, `cc08099`).

The docs-only exemption (#7662, merged) narrowed the deferral to code commits.
This plan removes it for pinned cycles.

Clarification answers recorded for this plan:

| Q | Decision |
|---|---|
| Q1 | **A**: point the 13 `internal-*.yml` wrappers at a `promote-candidate` branch; support code follows the same commit; a follower keeps the branch equal to `main` between cycles; during a cycle the branch is frozen at the candidate, so every pipeline run in this repo uses candidate code |
| Q2 | **A**: promote exactly the candidate commit; proving and verifying merges land on `main` and ride the next cycle |
| Q3 | **A**: commits merged mid-cycle do nothing until the cycle ends; `PROMOTE_CYCLE_PIN_ENABLED=false` unfreezes at once and abandons that cycle's promotion |
| Q4 | **B**: pinning is on by default at merge (`PROMOTE_CYCLE_PIN_ENABLED` defaults to `true`) |
| Q5 | **A**: three phases (support-ref rule, branch + follower + wrappers, cycle logic) |
| Q6 | **A** (assumed): candidate = `main` tip at cycle start (`baseline_sha`) |
| Q7 | **A** (assumed): proving and verifying PRs still merge into `main` |
| Q8 | **A** (assumed): keep #7662; it is the unpinned fallback's rule |
| Q9 | none (assumed): no consumer impact |
| Q10 | **A**: the owner adds a ruleset protecting `promote-candidate`; until then the cycle runs unpinned and the follower alerts daily |

## Automation & wiring (CLAUDE.md §18.E)

- **Scripts.** One new script, `scripts/promote_candidate_follow.sh`, run only
  by the new workflow `.github/workflows/promote-candidate-follow.yml`
  (triggers: `push` to `main`, `schedule` hourly at minute 17, `workflow_dispatch`).
  It is an event-driven workflow step, not a single-use or long-running script
  or a supervisor, so §18.F needs no registry entry. Everything else extends
  existing scripts: `scripts/promote_main_cycle.sh` (daily cycle job in
  `promote-main-to-stable.yml`, schedule `0 0 * * *`),
  `scripts/apply_analysis_on_main.sh`, and `scripts/orchestrate_poll_process.sh`
  (the poller, `internal-orchestrate-poll.yml`).
- **Supervisor.** None needed (§18.C). The follower reacts to pushes and runs
  hourly as a safety net; the poller already carries the cycle.
- **DB work.** None (§10 does not apply).
- **Operator step.** One, already accepted in Q10: the owner adds a ruleset for
  `promote-candidate`. Until it exists everything works, the cycle just stays
  unpinned (§18.D-style gate: the branch's `protected` flag is the sentinel).

## Goals

- G1. While a pinned cycle is in flight, every reusable-workflow job and every
  support-script checkout in this repository runs the frozen candidate commit,
  verifiable from the `SUPPORT_REF` log line of any run during the cycle.
- G2. A pinned cycle promotes exactly `promote_sha` = the candidate even if
  `main` gained code commits during the cycle; no `COMPREHENSIVE_PROMOTION_DEFERRED`
  is logged for a pinned cycle.
- G3. Between cycles, `promote-candidate` equals the `main` tip within one
  follower run of each push to `main`.
- G4. `PROMOTE_CYCLE_PIN_ENABLED=false` unfreezes the branch on the next
  follower run, and the in-flight cycle promotes nothing
  (`COMPREHENSIVE_PROMOTION_PIN_RELEASED`).
- G5. Each phase merged alone, in any order, leaves the pipeline working:
  the cycle falls back to today's unpinned behaviour (with the #7662
  deferral rule) whenever any pinning prerequisite is missing.
- G6. Consumer repositories see no behaviour change.

## Non-goals

- Pinning composite actions referenced `@stable` inside reusable workflows (for
  example `.github/actions/setup-runtime@stable` in `validate.yml`). Consumers
  run the same references today; changing that is a separate decision.
- Changing what consumers receive or how they pin (§14 unaffected).
- Running the cycle in a sandbox consumer repository (Q1 option B, rejected).
- Promoting the proving merge (Q2 option B, rejected).
- Creating branch protection from automation (Q10 option B, rejected).
- Removing the unpinned path or #7662's rule; they stay as the fallback.

## Constraints

- §6: no existing identifier is renamed or removed. `promote_sha`,
  `smoke_sha`, `proving_merge_sha`, the `apply-analysis-*` marker keys,
  `COMPREHENSIVE_PROMOTION_DEFERRED` and every `PROMOTE_CYCLE_*` variable keep
  their names and meaning. New names below were checked for clashes in
  `scripts/`, `.github/workflows/` and README: `PROMOTE_CYCLE_PIN_ENABLED`,
  `PROMOTE_CYCLE_FREEZE_MAX_SECS`, `promote-candidate` (branch),
  `promote-cycle-frozen` (tag), marker key `apply-analysis-pinned`, log
  prefixes `PROMOTE_CYCLE_PIN`, `PROMOTE_CANDIDATE_FOLLOW`,
  `COMPREHENSIVE_PROMOTION_PIN_RELEASED`, `SUPPORT_REF source=promote_candidate`.
- §4: new variables default (`PROMOTE_CYCLE_PIN_ENABLED=true` per Q4,
  `PROMOTE_CYCLE_FREEZE_MAX_SECS=172800`).
- §15: new API calls are listed per phase with counts; none sit inside an
  existing per-item loop.
- §19: the cycle's PRs keep `Refs` for tracking issues.
- §20 / §30: one `changelog.d/` fragment per phase; new log prefixes and README
  rows go in through an `agents.d/` fragment and README edits.
- §27: `implement.yml` is 402,475 bytes and `review_autofix.yml` 358,325 bytes.
  Phase 1 adds only a few lines each; if any file reaches 480,000 bytes, move
  the support-ref step body into `scripts/` in the same PR.
- Security (§1): support code for `promote-candidate` is taken from the
  branch's API tip only when the API reports `protected: true`; a look-alike
  ref (`refs/heads/promote-candidate-x`, `refs/heads/promote-candidate/x`) or an
  unprotected branch falls back to today's source.

## Approach

**The branch.** `promote-candidate` is a normal branch of this repository. All
13 `internal-*.yml` wrappers call `...@promote-candidate` instead of `...@main`
(their `on:` blocks and inputs are unchanged; for `issues`, `issue_comment` and
`schedule` events GitHub reads the wrapper from `main`, which then names the
branch). Every move of the branch is a fast-forward: the follower moves it up to
`main`, a freeze keeps it still, an unfreeze moves it up to `main` again. Force
updates are never needed, so the Q10 ruleset can forbid them.

**Support code follows the branch (phase 1).** In each of the 13 reusable
workflows the "Resolve workflow support ref" logic gets one new case for this
repository: when `toJSON(job).workflow_ref` is exactly
`shubhodeep1/coding-workflows/.github/workflows/<file>.yml@refs/heads/promote-candidate`,
read `GET repos/shubhodeep1/coding-workflows/branches/promote-candidate`; if
`protected` is `true`, use its `commit.sha` as the support ref and log
`SUPPORT_REF source=promote_candidate sha=<sha>`; otherwise keep today's source
(`github.sha`, or protected `main` in `review_autofix.yml` / `issue_pr_status.yml`,
or `main` in `validate.yml`) and log `SUPPORT_REF source=promote_candidate_unprotected`.
`memory_maintenance.yml` gets the same rule for its self-checkout. Consumer and
`@main` paths are untouched.

**The freeze marker.** A lightweight tag `promote-cycle-frozen` pointing at the
candidate marks a frozen cycle. Every `push` workflow in this repo filters on
branches (`ci.yml`, `forward-merge-stable-to-main.yml`, ...), so creating or
deleting this tag triggers nothing. Tag creation is atomic (`POST git/refs`
fails if it exists), which makes it the cycle's freeze lock.

**The follower (phase 2).** `scripts/promote_candidate_follow.sh`:
1. reads the branch (`GET branches/promote-candidate`: tip + `protected`), the
   tag (`GET git/ref/tags/promote-cycle-frozen`) and the `main` tip
   (`GET git/ref/heads/main`) — 3 calls;
2. creates the branch at the `main` tip if it is missing (`POST git/refs`);
3. if the tag exists and `PROMOTE_CYCLE_PIN_ENABLED` is not `false`: when the
   freeze is older than `PROMOTE_CYCLE_FREEZE_MAX_SECS` it deletes the tag with
   a CRITICAL alert; otherwise it logs `PROMOTE_CANDIDATE_FOLLOW outcome=frozen`
   and stops. The freeze time is `frozen_at=<epoch>` in the annotated tag's
   message (written in step P3.2), because the tagged commit's date says
   nothing about when the freeze began;
4. otherwise fast-forwards the branch to the `main` tip with
   `PATCH git/refs/heads/promote-candidate` and `force=false`, logging
   `PROMOTE_CANDIDATE_FOLLOW outcome=advanced from= to=`;
5. if the branch is not protected, sends a CRITICAL Telegram alert
   (`PROMOTE_CANDIDATE_FOLLOW outcome=unprotected`) at most once per UTC day:
   only from the first scheduled run whose start time falls in the 00:00 UTC
   hour, so no state needs storing.
Concurrency group `promote-candidate-follow-<repo>`, `cancel-in-progress: false`.

**The pinned cycle (phase 3).** `promote_main_cycle.sh` decides pinned mode
after its existing guards 1–6 and before the smoke gate:

- pinned only when all of: `PROMOTE_CYCLE_PIN_ENABLED` is not `false`; every
  `.github/workflows/internal-*.yml` in its checkout references
  `@promote-candidate` and the 13 reusable workflows contain the phase 1 marker
  `source=promote_candidate` (file reads, no API); the branch exists and is
  protected (1 call). Otherwise log `PROMOTE_CYCLE_PIN outcome=skipped
  reason=<disabled|wrappers_not_pinned|support_not_pinned|branch_missing|branch_unprotected>`
  and run exactly as today.
- a stale tag (exists, no labelled tracking issue open, no other cycle job
  active, both already checked by guards 1–2) is deleted first with a warning.
- freeze: create the annotated tag `promote-cycle-frozen` at `main_tip` with
  message `frozen_at=<epoch> cycle_run=<GITHUB_RUN_ID>`. If creation fails
  because the tag exists, skip `cycle_in_flight`. Then fast-forward the branch
  to `main_tip` (`force=false`). If the follower already moved it past
  `main_tip`, re-point the tag at the branch tip and use that as the candidate
  (`PROMOTE_CYCLE_PIN outcome=candidate_advanced`).
- every later `skip_cycle` / `fail_cycle` deletes the tag (an `EXIT` trap set
  right after the freeze, cleared on the dispatched path).
- smoke gate: dispatch with `--ref promote-candidate`; the gate's
  `head_sha` must equal the candidate, otherwise `fail_cycle
  pin_head_mismatch`.
- proving dispatch passes `APPLY_ANALYSIS_PINNED=true` and the candidate as
  `APPLY_ANALYSIS_CYCLE_BASELINE_SHA` and `APPLY_ANALYSIS_SMOKE_SHA`;
  `apply_analysis_on_main.sh` writes the marker line `apply-analysis-pinned: true`.

In the poller:

- `comprehensive_cycle_metadata_json` parses `apply-analysis-pinned` (missing →
  `false`, so old markers keep today's behaviour).
- `dispatch_comprehensive_verification_run`: for a pinned cycle, pass
  `APPLY_ANALYSIS_PROMOTE_SHA` = the marker's smoke SHA (the candidate) and
  `APPLY_ANALYSIS_PINNED=true`, instead of the `main` tip.
- `comprehensive_promotion_gate_before_final_merge`, `pending` status, pinned
  marker: skip `comprehensive_untested_commits_json`; instead read the tag and
  the branch (2 calls). If the tag points at `promote_sha` and the branch tip
  equals `promote_sha`, continue to the existing pinned-release dispatch
  (`target_sha=promote_sha`). Otherwise set
  `comprehensive_promotion.status=deferred`, reason `pin_released`, log
  `COMPREHENSIVE_PROMOTION_PIN_RELEASED tracking_issue= promote_sha= branch_tip=`,
  post a "⏸️ Promotion abandoned" comment, and let the merge proceed.
- after the promotion reaches a terminal state (`promoted`, `failed`, hold
  cap, `deferred`, `skipped`), delete the tag (1 call) so the follower catches
  the branch up to `main` on its next run.

## Phases & Merge Strategy

Each phase is one PR, production-safe on its own, mergeable in any order.

### Phase 1 — support code follows `promote-candidate`

- **Scope.** The new support-ref case in the 13 reusable workflows.
- **Files.** `.github/workflows/{cancel_on_pr_close,check_failure_triage,clarify,implement,issue_pr_status,memory_maintenance,orchestrate_clarify_respond,orchestrate_poll,orchestrate,plan,review_autofix,validate,workflow_failure_heal}.yml`,
  `tests/test_workflow_support_ref_follows_pin.py`, a changelog fragment.
- **Done.** With wrappers on `@main` (today) every run logs the same support
  ref as before; tests prove the new case for exact, look-alike, protected and
  unprotected refs, and that consumer pins are unchanged.
- **Safe alone.** Nothing calls a reusable workflow `@promote-candidate` until
  phase 2, so the new branch of code never runs.
- **Rollback.** Revert the PR.

### Phase 2 — the branch, the follower and the wrappers

- **Scope.** Create `promote-candidate`, add the follower, switch the 13
  wrappers to `@promote-candidate`.
- **Files.** `.github/workflows/promote-candidate-follow.yml` [new],
  `scripts/promote_candidate_follow.sh` [new], the 13 `internal-*.yml`,
  `tests/test_promote_candidate_follow.py` [new], README rows and contributing
  text, an `agents.d/` fragment, a changelog fragment.
- **Done.** The branch exists at the `main` tip before the PR merges
  (implementation step P2.1), the follower advances it after each push to
  `main`, and with no freeze tag every internal run executes the same commit as
  `main` (± one follower run).
- **Safe alone.** Without phase 3 no tag is ever created, so the branch always
  follows `main`. Without phase 1, support scripts come from `github.sha`
  (`main` at event time) while reusable YAML comes from the branch; the two
  differ only during the follower's lag after a push, the same class of skew
  `@main` resolution already has.
- **Rollback.** Revert the PR (wrappers back to `@main`). The branch and the
  follower's absence are harmless; the branch can stay.

### Phase 3 — the pinned cycle

- **Scope.** Freeze, pinned smoke gate and dispatch, pinned verifying run,
  pinned promotion gate, tag cleanup, `PROMOTE_CYCLE_PIN_ENABLED`,
  `PROMOTE_CYCLE_FREEZE_MAX_SECS`.
- **Files.** `scripts/promote_main_cycle.sh`, `scripts/apply_analysis_on_main.sh`,
  `scripts/orchestrate_poll_process.sh`, `.github/workflows/promote-main-to-stable.yml`
  (forward the two variables), `.github/workflows/orchestrate_poll.yml` (forward
  `PROMOTE_CYCLE_PIN_ENABLED`), tests, README 12f and the contributing section,
  an `agents.d/` fragment, a changelog fragment.
- **Done.** With phases 1 and 2 merged and the branch protected, a cycle logs
  `PROMOTE_CYCLE_PIN outcome=frozen`, the gate runs on the candidate, and the
  verifying run promotes the candidate with no deferral; a test simulates a
  code commit on `main` mid-cycle and shows the promotion still dispatches.
- **Safe alone.** The pre-flight check in step P3.1 finds the wrappers or the
  support rule missing and logs `PROMOTE_CYCLE_PIN outcome=skipped`; the cycle
  runs as today and the poller sees `apply-analysis-pinned` absent.
- **Rollback.** Set `PROMOTE_CYCLE_PIN_ENABLED=false` (immediate), or revert
  the PR; a leftover tag is removed by the follower's age limit or by the next
  cycle's stale-tag check.

## Implementation Steps

### Phase 1

1. P1.1 In each of the 10 workflows with `SCRIPT_REF=${{ github.sha }}` for
   this repository (`cancel_on_pr_close`, `check_failure_triage`, `clarify`,
   `implement`, `orchestrate_clarify_respond`, `orchestrate_poll` (both
   resolve steps, lines ~118-138 and ~493-513), `orchestrate`, `plan`,
   `workflow_failure_heal`; `sync_ai_labels.yml` is not called by an internal
   wrapper and is left alone), inside the existing
   `if [ "${{ github.repository }}" = "${wf_source}" ]` branch: read
   `workflow_ref` from `SUPPORT_REF_WORKFLOW_JOB_JSON` (already passed); if it
   matches `^shubhodeep1/coding-workflows/\.github/workflows/<this-file>\.yml@refs/heads/promote-candidate$`,
   call `gh_retry gh api repos/shubhodeep1/coding-workflows/branches/promote-candidate`
   and use `.commit.sha` when `.protected == true` and the SHA is 40 hex;
   else keep `github.sha`. Log `SUPPORT_REF source=promote_candidate sha=` or
   `SUPPORT_REF source=promote_candidate_unprotected`. The existing
   "checkout HEAD must equal SCRIPT_REF" verification then covers the new SHA.
2. P1.2 `review_autofix.yml` (`resolve_support`, ~lines 396-458) and
   `issue_pr_status.yml` (`resolve_support`, ~lines 925-950): before the
   generic `source_branch_ref` case, add the exact `promote-candidate` case
   with the same protected check, setting `support_sha` to the branch tip and
   `support_ref=refs/heads/promote-candidate`; otherwise fall through to the
   existing protected-`main` path.
3. P1.3 `validate.yml` (~line 138, `ghr_ref=main` for this repo, and the
   resolver step ~188-195): when the job `workflow_ref` is the exact
   `promote-candidate` ref and the branch is protected, use its tip SHA for
   both checkouts; else `main` as today.
4. P1.4 `memory_maintenance.yml`: the self-checkout takes the protected
   `promote-candidate` tip under the same rule, else unchanged.
5. P1.5 Extend `tests/test_workflow_support_ref_follows_pin.py`: add the 13
   files to the parametrised set where missing; cases for exact ref +
   protected → branch SHA; exact ref + unprotected → old source; look-alikes
   (`promote-candidate-x`, `promote-candidate/x`, another file's name) → old
   source; consumer `@<sha>` pin unchanged; `@main` unchanged.
6. P1.6 `wc -c` every edited workflow; split per §27 if any reaches 480,000.
7. P1.7 `changelog.d/<pr>-support-ref-promote-candidate.md` (`changed`).

### Phase 2

1. P2.1 Before opening the PR, create `refs/heads/promote-candidate` at the
   current `main` tip with `POST repos/shubhodeep1/coding-workflows/git/refs`
   if it does not exist (the follower also self-heals a missing branch).
2. P2.2 `scripts/promote_candidate_follow.sh` [new] implementing the follower
   in the Approach (env: `GITHUB_REPOSITORY`, `GH_TOKEN`,
   `PROMOTE_CYCLE_PIN_ENABLED` default `true`, `PROMOTE_CYCLE_FREEZE_MAX_SECS`
   default `172800`, `TG_BOT_SECRET` / `TG_CHAT_ID` for alerts via the existing
   `tg_helpers.sh`). Freeze age comes from the tag message `frozen_at=<epoch>`
   (`GET git/tags/<tag-object-sha>`, 1 extra call only when the tag exists); a
   lightweight or unparsable tag counts as fresh and is left to the cycle's
   stale-tag check. Every exit logs one `PROMOTE_CANDIDATE_FOLLOW outcome=`
   line: `advanced`, `current`, `frozen`, `created`, `unfrozen_disabled`,
   `stale_tag_deleted`, `unprotected`, `error`.
3. P2.3 `.github/workflows/promote-candidate-follow.yml` [new]: `on: push:
   branches: [main]`, `schedule: '17 * * * *'`, `workflow_dispatch`;
   `permissions: contents: write`; uses `secrets.GH_PAT` (branch writes must be
   by the pipeline account the ruleset allows); concurrency group
   `promote-candidate-follow-${{ github.repository }}`, no cancel.
4. P2.4 Change `@main` to `@promote-candidate` in the `uses:` line of all 13
   `.github/workflows/internal-*.yml`. Nothing else in them changes.
5. P2.5 `tests/test_promote_candidate_follow.py` [new]: follower outcomes
   with a stubbed `gh` (advanced, current, frozen, created, disabled ignores
   tag, stale tag deleted with CRITICAL, unprotected alert only on the first
   run after 00:00 UTC, `force=false` on every PATCH); a wiring test that all
   13 wrappers use `@promote-candidate` and no other workflow does.
6. P2.6 README: rows for `PROMOTE_CYCLE_PIN_ENABLED` (shared with phase 3,
   documented here as "read by the follower: `false` ignores the freeze tag")
   and `PROMOTE_CYCLE_FREEZE_MAX_SECS`; a paragraph in the contributing
   section on the branch, the follower and the required ruleset (no
   deletion, no force-push, updates restricted to the pipeline account).
7. P2.7 `agents.d/<pr>-promote-candidate-follow.md`: the new prefix in
   "Stable log prefixes (contractual)"; a sentence in "Workflow architecture".
8. P2.8 `changelog.d/<pr>-promote-candidate-branch.md` (`changed`).

### Phase 3

1. P3.1 `promote_main_cycle.sh`: after guard 6, compute pinned mode as in the
   Approach. File checks run on the job's checkout: `grep -L '@promote-candidate'`
   over `internal-*.yml` must be empty, and each of the 13 reusable workflows
   must contain `source=promote_candidate`. One API call for the branch.
2. P3.2 Freeze: stale-tag cleanup, `POST git/tags` (annotated, message
   `frozen_at=<epoch> cycle_run=<id>`) + `POST git/refs` for the tag, branch
   fast-forward, candidate reconcile, `EXIT` trap that deletes the tag on any
   skip or fail after this point. Log `PROMOTE_CYCLE_PIN outcome=frozen
   candidate=<sha>`.
3. P3.3 Smoke gate: `--ref promote-candidate` when pinned; match the gate run
   by `head_branch == promote-candidate`; require `gate_head_sha == candidate`
   or `fail_cycle pin_head_mismatch`. Skip the existing "gate tested a newer
   commit" checkout path when pinned (it cannot happen).
4. P3.4 Proving dispatch: `APPLY_ANALYSIS_PINNED=true`; baseline and smoke
   SHAs = candidate. Clear the trap after `PROMOTE_CYCLE_DISPATCHED`.
5. P3.5 `apply_analysis_on_main.sh`: accept `APPLY_ANALYSIS_PINNED` (default
   `false`); when `true` append `apply-analysis-pinned: true` to the marker and
   adjust the verifying-run role prose to say the promoted commit is the
   pinned candidate.
6. P3.6 `orchestrate_poll_process.sh`:
   - `comprehensive_cycle_metadata_json`: add `pinned` (`true` only for the
     literal `true`);
   - `dispatch_comprehensive_verification_run`: pinned → promote SHA = marker
     `smoke_sha`, pass `APPLY_ANALYSIS_PINNED=true`;
   - `comprehensive_promotion_gate_before_final_merge`: pinned branch as in
     the Approach; unpinned path unchanged;
   - a helper `comprehensive_release_cycle_freeze` that deletes the tag (1
     call, idempotent, 404 counts as done) on every terminal promotion state.
7. P3.7 Forward `PROMOTE_CYCLE_PIN_ENABLED` and `PROMOTE_CYCLE_FREEZE_MAX_SECS`
   from repo vars in `promote-main-to-stable.yml`, and `PROMOTE_CYCLE_PIN_ENABLED`
   in `orchestrate_poll.yml` (default `true` when the var is unset).
8. P3.8 Tests: `tests/test_promote_main_cycle.py` (each skip reason of pinned
   mode; freeze success; tag-exists → `cycle_in_flight`; candidate reconcile;
   trap deletes the tag on gate failure; gate dispatched on the branch;
   `pin_head_mismatch`); `tests/test_apply_analysis_on_main.py` (marker line
   present only when pinned); `tests/test_orchestrate_poll_promote_cycle.py`
   (pinned gate dispatches despite unproven commits on `main`; branch moved →
   `COMPREHENSIVE_PROMOTION_PIN_RELEASED` and no dispatch; tag deleted on each
   terminal state; old markers without the key keep today's path; verifying
   dispatch uses the candidate).
9. P3.9 README 12f and "The daily promote cycle": describe pinned mode, the
   fallback conditions, the abort switch and the new log prefixes; mark the
   untested-commit check as the unpinned path.
10. P3.10 `agents.d/<pr>-pinned-promote-cycle.md` (prefixes `PROMOTE_CYCLE_PIN`,
    `COMPREHENSIVE_PROMOTION_PIN_RELEASED`) and
    `changelog.d/<pr>-pinned-promote-cycle.md` (`changed`).

## Files & Modules

- `.github/workflows/cancel_on_pr_close.yml`, `check_failure_triage.yml`,
  `clarify.yml`, `implement.yml`, `issue_pr_status.yml`,
  `memory_maintenance.yml`, `orchestrate_clarify_respond.yml`,
  `orchestrate_poll.yml`, `orchestrate.yml`, `plan.yml`, `review_autofix.yml`,
  `validate.yml`, `workflow_failure_heal.yml` (phase 1; `orchestrate_poll.yml`
  also phase 3)
- `.github/workflows/internal-*.yml` (13 files, phase 2)
- `.github/workflows/promote-candidate-follow.yml` [new] (phase 2)
- `.github/workflows/promote-main-to-stable.yml` (phase 3)
- `scripts/promote_candidate_follow.sh` [new] (phase 2)
- `scripts/promote_main_cycle.sh`, `scripts/apply_analysis_on_main.sh`,
  `scripts/orchestrate_poll_process.sh` (phase 3)
- `tests/test_workflow_support_ref_follows_pin.py` (phase 1)
- `tests/test_promote_candidate_follow.py` [new] (phase 2)
- `tests/test_promote_main_cycle.py`, `tests/test_apply_analysis_on_main.py`,
  `tests/test_orchestrate_poll_promote_cycle.py` (phase 3)
- `README.md` (phases 2 and 3)
- `agents.d/<pr>-*.md` [new] (phases 2 and 3)
- `changelog.d/<pr>-*.md` [new] (each phase)

## Tests

- Unit: the files above, run with `PYTHONDONTWRITEBYTECODE=1 pytest -q` plus the
  existing workflow-size and support-ref suites in `ci.yml`.
- Integration: phase 2's wiring test, and phase 3's simulated cycle in
  `test_orchestrate_poll_promote_cycle.py` with a code commit landing on `main`
  between freeze and promotion.
- End to end (after all three phases and the ruleset): the next scheduled cycle
  must log `PROMOTE_CYCLE_PIN outcome=frozen`, every internal run during it
  must log `SUPPORT_REF source=promote_candidate sha=<candidate>`, and the
  `stable` tag must move to the candidate even if `main` advanced. Check with
  `gh run view --log` on the cycle job, one poll run and the release run.

## Risks & Mitigations

- **Unprotected branch window.** Between phase 2 merging and the owner adding
  the ruleset, reusable YAML is read from an unprotected branch. ACCEPTED —
  pending ruleset (Q10 A). The cycle refuses to pin, support scripts do not
  follow the branch, and the follower alerts CRITICAL daily until it is
  protected.
- **Frozen pipeline delays urgent fixes.** During a cycle (up to about 12 hours)
  a pipeline fix merged to `main` does not run in this repository. Mitigation:
  `PROMOTE_CYCLE_PIN_ENABLED=false` unfreezes on the next follower run
  (`workflow_dispatch` it for immediate effect) and abandons the promotion;
  documented in README.
- **Stuck freeze.** A crashed cycle or an abandoned project could leave the tag.
  Mitigation: the cycle's `EXIT` trap, the poller's terminal-state cleanup,
  the next cycle's stale-tag check, and the follower's
  `PROMOTE_CYCLE_FREEZE_MAX_SECS` (48 h) age limit with a CRITICAL alert.
- **Branch deleted or missing.** Every internal workflow would fail to resolve
  `@promote-candidate`. Mitigation: P2.1 creates it before merge, the ruleset
  forbids deletion, and the follower recreates it on its next run (hourly at
  most).
- **Follower lag skew (phase 2 without phase 1).** Reusable YAML and support
  scripts can differ for the minute after a push. Same class of skew as `@main`
  today; phase 1 removes it for branch runs.
- **Race between freeze and follower.** Handled by the atomic tag create and
  the candidate reconcile (P3.2); every branch write is `force=false`, so a
  lost race fails instead of rewinding.
- **Pull-request events read wrappers from the PR head.** A same-repo PR could
  point its own `internal-review.yml` elsewhere, as it can today. Support code
  still comes only from the protected branch or protected `main`, never from
  the PR's ref.

## Rollout

- Merge order does not matter. Pinning becomes active only after phases 1, 2
  and 3 are all on `main` and the branch is protected; the cycle logs which
  prerequisite is missing until then.
- Owner step (Q10 A): add a ruleset on `refs/heads/promote-candidate`: block
  deletion, block force-push, restrict updates to the pipeline account. No
  status-check requirements (the follower moves it to already-merged `main`
  commits).
- Default on (Q4 B): `PROMOTE_CYCLE_PIN_ENABLED` unset means `true`.
- Rollback: set `PROMOTE_CYCLE_PIN_ENABLED=false` (immediate unfreeze; cycles
  run unpinned with the #7662 deferral rule), or revert phase 2 to put the
  wrappers back on `@main`.
- Consumers: unaffected. The `internal-*.yml` wrappers are not in
  `workflow-templates/`, and phase 1's new case only matches this repository's
  `promote-candidate` ref; consumer `@<sha>` pins resolve exactly as today
  (§14: no registry or dispatch change).

## References

- `scripts/promote_main_cycle.sh`, `scripts/apply_analysis_on_main.sh`,
  `scripts/orchestrate_poll_process.sh` (`comprehensive_promotion_gate_before_final_merge`,
  `comprehensive_untested_commits_json`, `dispatch_comprehensive_verification_run`)
- README section 12f and "The daily promote cycle"
- #7614 (verifying run whose promotion was deferred on 2026-10-18), #7640
  (the 10-19 cycle)
- #7662 (docs-only exemption, the unpinned path's rule)
- #7538 (consumer support code pinned to the wrapper's commit; the model for
  phase 1)

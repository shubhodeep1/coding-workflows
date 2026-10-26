# Workflow-failure heal issues that resolve without a human

## Summary

Remove the three places where a workflow-failure heal needs a person today, all seen on heal #8182 (for shubhodeep1/fun-token-multi-chain#579):

- **A harness defect the heal may not edit.** The heal scope cannot reach the validation-harness files.
- **A duplicate of a fix already on `main` but not yet on `stable`.**
- **A cause the consumer already fixed itself.** Nothing closes the heal issue or re-runs validation.

The work ships as four independent phases, each behind a default-on switch.

## Automation & wiring (CLAUDE.md §18.E)

| Item | Answer |
|---|---|
| New scripts | None. All work extends existing scripts: `scripts/workflow_failure_heal_intake.sh`, `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_recovery_scan.sh`, `scripts/validate_process.sh`, `scripts/review_untrusted_workspace.py`, `scripts/orchestrate_poll_process.sh`. |
| Scheduler entry points | **Phase 1:** the existing intake job in `.github/workflows/workflow-failure-heal-intake.yml` (`repository_dispatch` / `workflow_run` / hourly `schedule` / `workflow_dispatch`). **Phase 2:** the same intake job, plus the consumer validate run (`.github/workflows/validate.yml`, which runs `scripts/validate_process.sh`). **Phase 3:** a new job `source-resolved-scan` in `.github/workflows/workflow-failure-heal-intake.yml`, on the existing `cron: "41 * * * *"` schedule (line 50), schedule only (the workflow's `workflow_dispatch` requires `payload_json` and always runs the intake job, lines 51-56 and 89-90). **Phase 4:** the consumer poller (`workflow-templates/ai-orchestrate-poll.yml`, `*/5` cron, calling `orchestrate_poll.yml@stable`). |
| Long-running supervisor | Not needed (§18.C). Phase 3 is a scheduled scan. Phase 4 runs inside the existing poller tick. |
| DB work / gates | No MongoDB collections (§10 N/A). Idempotency gates are marker comments and poller state fields, listed per phase. |
| `docs/scripts-pending-removal.md` | No entry. No single-use or long-running script or supervisor is introduced (§18.F). |

## Context

- **The heal pipeline.** Heal intake files `ai:workflow-heal` issues here when a consumer pipeline fails. Before the model diagnosis runs, it freezes an edit scope from authenticated run metadata (`<!-- ai:workflow-heal-scope:v1 paths=… runs=… -->`, #6463). It builds that marker with `render_heal_scope_marker` (`scripts/workflow_failure_heal.py:3301`) from the frozen inputs (`SCOPE_INPUTS_FROZEN`, `scripts/workflow_failure_heal_intake.sh:2291` and the jq that follows it), re-projected for `heal-scope render` by a fixed jq at line 2753. The diagnosis can never add a path. Failing-test subjects (Q37: A, 2026-10-09) and `HEAL_SCOPE_GUARD_TEST_SUBJECTS` (`workflow_failure_heal.py:3218-3236`) are the existing ways to widen the scope from run metadata.
- **What went wrong on #8182.** A consumer validation run failed with `raw_status=harness_error`: the consumer's root `.dockerignore` stripped `package.json` from the harness image.
  - **Scope.** The scope came out as `.github/workflows/validate.yml,tests/**,changelog.d/*.md`, because `verify_run` derives workflow paths only from `referenced_workflows` (`workflow_failure_heal.py:6432-6438`). A `validation_failure` report has no crash file and no changed files.
  - **Plan hold.** The plan named the harness templates, so `plan.yml` "Auto-approve clear plan" held it with `heal_scope_plan_out_of_scope`.
  - **Editor.** Even inside scope, the isolated editor never sees `.j2` files: `SUFFIXES` in `scripts/review_untrusted_workspace.py:33` does not contain `.j2`, so `allowed()` (lines 128-146) drops them from the snapshot.
- **Fix already on `main`.** #8181 fixed the same defect on `main` at 05:10 UTC, with `Refs shubhodeep1/fun-token-multi-chain#579` in its commit message. The heal planned at 05:29 against `stable@5596e75`, which lacked it.
  - The intake's only history call compares `<pin>...stable` (`workflow_failure_heal_intake.sh:2079-2112`).
  - `summarize_branch_progress` (`workflow_failure_heal.py:5697-5722`) keeps only commit subjects.
  - So `main` is never examined.
- **Consumer fixed it itself.** The consumer fixed the cause in fun-token-multi-chain#687. A human posted `/revalidate` at 04:20, and validation passed at 05:54. The heal issue stayed open until closed by hand (PR #8197 shipped the residual hardening).
  - No code closes a heal issue when its source resolves. The only closer, `scripts/workflow_failure_heal_pr_reconcile.sh`, reacts to PRs closing in this repo.
  - No code re-validates after a merge into the consumer's integration branch.
- **Consumer harness hold.** The consumer poller already parks a `harness_error` project in a harness hold (`scripts/orchestrate_poll_process.sh`, from `validation_harness_local_fp` at line 15333 through `validation_harness_hold_tick` at line 15730) and resumes it from the intake's routing marker `<!-- workflow-failure-heal:validation-route:v1 fp= outcome= issue= classification= run= -->`.
  - **`already-fixed`:** the hold resumes once the consumer's `validate.yml` pin is ahead of the hold's `support` commit (Q5, step 3/4 comment at line 15855).
  - **Heal closed `not_planned`:** for an `opened`/`duplicate` route, a heal closed `not_planned` ends the hold with a CRITICAL park (`not_planned)` arm, lines 15866-15872). That arm runs whenever the pin has moved ahead of `support`, whatever resolved the source, so Phase 3 must not reach it (see Phase 3).

## Goals

- **G1:** a `validation_failure` heal whose verified run logs report `raw_status=harness_error family=<f>` gets this scope, with every entry derived from run metadata:
  - the family's template directory and `_shared`;
  - `scripts/validation_harness_sandbox.sh`, `scripts/validate_driver.sh`, `scripts/validate_process.sh` and `scripts/render_validation_templates.py`.
- **G2:** the heal implementer can read, edit and create `.j2` files under `workflow-templates/validation-harness/`, and only there.
- **G3:** the intake files no heal issue when `main` already has a commit after the consumer's pin that cites the exact source (`owner/repo#N`). It posts the `already-fixed` routing outcome naming that commit instead.
- **G4:** a consumer harness hold that has a fix commit recorded resumes only once the consumer's pin contains that commit.
- **G5:** an open heal issue with no open heal PR closes itself, as `not_planned` with a comment, once every source it records is resolved. A source is resolved when its issue or pull request is closed, or (for `validation_failure` heals) when its tracking issue carries `ai:validated` and a validation run for it succeeded after the heal was filed. A run that only concluded `success` is not enough: a Claude capacity-wait run exits 0 with no verdict (`.github/workflows/validate.yml:1390-1396`).
- **G6:** during a harness hold, a consumer poller re-validates once per new integration-branch head that is ahead of the head the failed validation run was dispatched against, within its own resume cap (separate from the harness-fix resume counters).
- **G7:** each behaviour has its own default-on switch. Switching it off restores today's behaviour exactly. Two Phase 2 edits to `scripts/review_untrusted_workspace.py` are deliberately not switched, and roll back only by revert: the `.j2` admission (one directory, no credentials) and the step-script glob transfer fix (a latent bug, Q14).

## Non-goals

- Letting the model diagnosis widen any scope. Every new entry comes from authenticated run metadata or a fixed table.
- Re-filing out-of-scope heals as ordinary issues automatically.
- Changing the 20-explicit-path cap, the `tests/**` / `changelog.d/*.md` tail, or the `agents.d/<N>-*.md` allowance (#8064).
- Changing `already-fixed` model verdicts (`check_heal_already_fixed_claim`). Phase 1 adds a deterministic check beside it.
- Harness families' own behaviour (templates, renderer).
- Closing heal PRs (Q8: A). A heal issue with an open PR is left to `workflow_failure_heal_pr_reconcile.sh` and the review pipeline.
- Moving `stable` faster. Promotion stays `promote-main-to-stable.yml` (daily) and `auto-release-stable.yml` (every 6h).

## Constraints

- **§6 naming.** No identifier is renamed or removed.
  - All new log fields, markers and state fields are additive: the `family=` field, the new `validation-route-fix:v1` marker line, the `integration_head` hold field, and the new `outcome=` tokens under existing prefixes.
  - The route marker line itself is unchanged. Old consumer pollers anchor their regex on `run=(?<run>[0-9]+) -->$` (`orchestrate_poll_process.sh` ~15320), so the fix SHA goes in a separate marker line they ignore.
  - New identifiers must be checked for collisions before use (§6 uniqueness). Each step names its new identifiers.
- **§14 consumer repos.**
  - Phases 2 (the `validate_process.sh` field) and 4 (the poller) reach every repo in `.github/ai/consumer_repos.json` on the next `@stable` sync.
  - Phase 1's marker line is read only by pollers that have Phase 1's parser.
  - No workflow template changes, so no wrapper re-sync is needed.
- **§15 GitHub API.**
  - Phase 1 adds at most one `compare` call per consumer report. It cannot reuse the existing `<pin>...stable` compare, which has a different head. On a match it may add one paginated comments read of the source (step 4).
  - Phase 1 also adds, on the poller side, one `compare/<fix>...<pin>` read per held project with a recorded `fix` per tick, while the hold waits (step 8). `_validation_harness_hold_compare` memoizes only within one poller process, and its key differs from the support-vs-pin compare, so later ticks pay it again.
  - Phase 3 adds per scan:
    - one open-PR list, plus one more after the queue ticket when at least one candidate would close;
    - one aliased GraphQL read per 25 candidate sources per repository (consumer repos and this repo);
    - per consumer repo with a `validation_failure` candidate, one run list per validate wrapper file (`actions/workflows/<wrapper>/runs`, at most 5 pages; a wrapper the repo lacks answers 404);
    - per closed issue: one comment re-read and one reopen-timeline GraphQL read before the close, one `ensure_label_exists` check (once per scan), one comment POST, one label POST, one PATCH.
  - Phase 4 adds, per held project per poller tick with the switch on, one `GET git/ref/heads/<integration branch>` (`_branch_head_sha`, `scripts/orchestrate_poll_process.sh:5570`, uncached; the hold tick resolves no head today), plus one `compare` only when that head differs from the last head checked, plus one `_branch_head_sha` call per validation dispatch to record the dispatch head. No batched read covers it.
  - Every lookup fails open (Q13: A).
- **§19 auto-close keywords.** Close comments and PR bodies use `Refs`, never closing keywords.
- **§20 / §30.** One `changelog.d/` fragment per phase. `agents.d/` fragments for every agents.md addition. README rows for new switches.
- **§27.** `workflow-failure-heal-intake.yml` is 27,047 bytes and `orchestrate_poll.yml` is 131,384 bytes, both far under 480,000.
- **Security.** Untrusted inputs:
  - consumer job logs (forgeable lines);
  - consumer issue state and comments;
  - commit messages on `main`, which only maintainers and pipelines can write.

  Each new reader validates against a fixed vocabulary: the family enum, `^[0-9a-f]{40}$` SHAs, `owner/repo#N` from the registry. A forged value can at most pick another harness family or delay a heal.

## Approach

**Phase 1: catch fixes already on `main` at intake (Q4: A, Q5: A, Q6: A).**
- **Check.** For a consumer report (not this repo's own runs) whose target is `stable`, the intake reads `compare/<WRAPPER_SHA>...<MAIN_FIX_DEFAULT_BRANCH>` once, after the existing branch-progress compare. `MAIN_FIX_DEFAULT_BRANCH` is a new variable resolved for this check outside the provenance block, because the script has no `DEFAULT_BRANCH` and only assigns `PROVENANCE_DEFAULT_BRANCH` for `phase_failure`/`autofix_failure`/`workflow_run` (line 969), not for `validation_failure`. An empty value skips the check. It scans every returned commit's full message for the exact source reference `<SOURCE_REPO>#<n>` as a whole token: not preceded by `[A-Za-z0-9_./-]` and not followed by `[A-Za-z0-9_]`.
- **On a match.** The newest matching commit becomes `MAIN_FIX_SHA`. The intake skips the scope freeze and the diagnosis model call, writes a deterministic `DIAG_FILE` naming the commit, bypasses the `parse-classification` recompute and the `check-already-fixed` gate (lines 2520-2543) so `CLASSIFICATION=already-fixed` stands, files nothing, posts the existing `already-fixed` outcome comment (naming the commit and that it reaches consumers on the next `stable` promotion), and logs `WORKFLOW_HEAL main_fix outcome=suppressed …`.
- **Validation reports.** The route comment carries an extra line `<!-- workflow-failure-heal:validation-route-fix:v1 fp=<fp> fix=<sha> -->` before the unchanged last-line route marker. It is appended after the `grep -v 'workflow-failure-heal:validation-route'` filter (line 1122), which would otherwise strip it.
- **One suppression per fix.** The outcome comment carries `<!-- workflow-failure-heal:main-fix:v1 fp=<fp> fix=<sha> -->`. When the source already has a pipeline-authored main-fix marker for the same `fp` whose `fix` is no longer among the compare's commits (it reached the pin and did not hold), the intake files normally (`reason=prior_fix_did_not_hold`). Later commits that merely cite the source therefore cannot suppress the heal again.
- **Poller side.** The hold stores `fix` and, in step 3, also requires `compare/<fix>...<pin>` to be `ahead` or `identical`. Without the line, the hold behaves exactly as today.

**Phase 2: deterministic harness scope (Q1: A, Q2: A, Q3: A).**
- **Log field.** `validate_process.sh` adds `family=<name>` (one of the five `FAMILY_REGISTRY` names in `scripts/render_validation_templates.py:101-107`, else `none`) right after `raw_status=` in all three `VALIDATION_FAILURE_SUMMARY` lines (lines 58, 1366, 2176).
- **Extraction.** A new `heal-scope harness-family` subcommand reads it from the verified run's filtered job logs. It returns a family only if every `VALIDATION_FAILURE_SUMMARY … raw_status=harness_error family=<f>` line in the logs agrees on one known family.
- **Freezing.** The intake freezes it as `harness_family` in `scope_inputs_frozen.json`.
- **Rendering.** `render_heal_scope_marker` adds `HEAL_SCOPE_HARNESS_SUBJECTS` (the four scripts, existence-checked) and two directory entries, `workflow-templates/validation-harness/<family>` and `workflow-templates/validation-harness/_shared`. These are allowed only in that exact shape, and only when they came from `harness_family`, the same way the step-script glob is allowed only as a guard subject. They take 6 of the 20 explicit slots, placed right after the workflow paths.
- **Forwarding.** The fixed `_open_issue` jq projection that builds `scope_inputs.json` for `heal-scope render` (`workflow_failure_heal_intake.sh:2753`) gains `harness_family: (.harness_family // "")`; without it render never sees the family.
- **Where the restriction lives.** The verify side (`_heal_scope_marker_shape_ok`, `_safe_heal_path`) is a string check and cannot tell a directory from an extensionless file such as `Makefile`, so it is not changed. Only `render_heal_scope_marker` may emit a directory entry, and only from `harness_subjects`; the scope marker is trusted only from an unedited, pipeline-authored body, so render is the gate.
- **Downstream checks:**
  - `review_untrusted_workspace.py` admits `.j2` only under `workflow-templates/validation-harness/`.
  - Its transfer scope check (lines 944-948) already accepts the two directory shapes (`[A-Za-z0-9_./-]+`). It newly accepts `scripts/<stem>_step_*.sh`, the latent bug Q14: A fixes.
  - The plan gate and the commit guard already match directory entries (`entry_matches`, `files_touched_scope_guard.py:478-535`).

**Phase 3: auto-close heal issues when the source resolves (Q7: A, Q8: A, Q9: A).**
- **Job.** A new `source-resolved-scan` job runs hourly (schedule only), using a new `--source-resolved` mode of `workflow_failure_heal_recovery_scan.sh`, with decisions in a pure `heal-source-resolved` subcommand of `workflow_failure_heal.py`. The mode is dispatched before the script's recovery gates (`HEAL_SCOPE_RECOVERY_ENABLED`, schedule-only, lines 117-135) and uses its own listing: every open pipeline-authored heal issue, without the existing `contains("ai:workflow-heal-scope") | not` filter (line 187).
- **Recorded sources.** For each open `ai:workflow-heal` issue authored by the pipeline login, the recorded sources are the `source=` marker plus every occurrence comment's source. Phase 3 also makes `compose_occurrence_comment` write an additive `<!-- workflow-failure-heal:occurrence-source=<repo>#<n> -->` marker. An occurrence comment without that marker makes the issue "unknown" (never closed), so old issues stay manual.
- **Resolved.** A source counts as resolved when:
  - its issue or pull request is closed (`issueOrPullRequest` aliases, one GraphQL read per 25 sources per repository, this repo included; a pull request counts when `CLOSED` or `MERGED`); or
  - for `validation_failure` heals, the tracking issue currently carries `ai:validated` (read in the same aliased query) and an `AI Validate [tracking:<n>]` run of the consumer validate wrapper concluded `success` after the heal issue's `created_at` (one `actions/runs` list per repo, with the same path and event checks as `_verify_validation_report_provenance`). The label is the positive evidence: a capacity-wait run concludes `success` without setting it.
- **Closing.** When every source is resolved and no open PR has head `ai/issue-<N>` (one open-PR list), the scan takes the heal-intake queue ticket (`workflow_failure_heal_queue.sh acquire`/`release`, as `recovery-scan` does), re-reads the issue's comments and recomputes the decision, then comments (marker `<!-- ai:heal-source-resolved:v1 issue=<N> -->`), adds the new label `ai:heal-source-resolved` and closes it `not_planned`. The PATCH runs only after the label is confirmed on the issue; a failed label write leaves the issue open for the next scan.
  - **Queue identity.** The scan acquires as a new job value `source-resolved-scan`, added to `HEAL_QUEUE_JOBS` and the ticket regex in `workflow_failure_heal.py` and to the accepted list in `workflow_failure_heal_queue.sh` (today only `intake` and `recovery-scan`; anything else proceeds unlocked). The scan and `recovery-scan` share a scheduled run, so the queue's same-run stale rule must also require the same `job`; otherwise each scan would delete the other's ticket. The queue fails open, so this serialization is best-effort (see Risks).
  - **Lineage.** A closed heal issue carrying `ai:heal-source-resolved` adds no generation in `budget_decision`: it is left out of `prior_same_fp` and `prior_source_lineage`. It stays in `closed_lineage`, which is also the root set `resolve_reissue_roots` matches open unblock-judge re-issues against (lines 4937-4948), so a re-issue of a source-resolved close is still found as a duplicate. Otherwise every source-resolved close followed by a recurrence would add a generation, and after `WORKFLOW_HEAL_MAX_LINEAGE_DEPTH` cycles `lineage_cap` would escalate to a person.
  - **Hold safety.** The poller's `not_planned)` arm parks a hold whenever the pin has moved ahead of `support`, whatever resolved the source. Phase 3 therefore also changes the poller: `_validation_harness_hold_issue_state` reads the issue's labels from the GET it already makes (no new call), and a heal closed `not_planned` with `ai:heal-source-resolved` resumes through `_validation_harness_hold_resume` (reason `heal_source_resolved`) instead of parking. Old pollers still park until they sync `@stable`; the risk is listed below.
  - A duplicate source that is still unresolved blocks the close.
  - A pipeline-authored close marker that is already present means a previous PATCH failed: the scan retries only the label and PATCH and does not post the comment again. The exception is an issue reopened after the marker was posted. A person reopened it, so it is kept (`reason=human_reopened`) and never closed again by the scan.
  - After the queue ticket is taken, the scan re-reads the open `ai/issue-<N>` PRs, the comments and the issue's latest reopen event, so a heal PR opened or a reopen made while it waited keeps the issue open.

**Phase 4: consumer re-validates after a merge during a harness hold (Q10: A).**
- **Recording.** `dispatch_validation_if_needed` reads the integration branch head just before it dispatches and records it as `validation_dispatch_integration_head`. A merge that lands between that read and the run's checkout is then tested by the run but newer than the baseline, so at worst it causes one extra resume, never a missed one. `mark_validation_failed` copies it into `validation_harness_hold.integration_head` (falling back to the head at failure time, logged `baseline=failure_time`).
- **Trigger.** On each `validation_harness_hold_tick`, after the pin is resolved (step 2, `vht_pin`) and before the support checks, if the head now resolved for `orchestrator/project-<N>` differs from `integration_head` and from the last head checked, the tick compares them (`compare/<integration_head>...<head>` in the consumer repo, through a new helper, because `_validation_harness_hold_compare` is fixed to coding-workflows), recording the head as checked only after a definitive result, so a failed compare is retried on the next tick. Only `ahead` resumes, through `_validation_harness_hold_resume` with the resolved pin. A reset or rebuild to a non-descendant head is recorded as checked and does not resume.
- **Own budget.** Integration resumes are charged to a separate counter (top-level `validation_harness_integration_resumes`, cap `VALIDATION_HARNESS_INTEGRATION_MAX_RESUMES`, default 2 per project, with the same lifetime as `validation_harness_resumes`, which is never reset) and never to `validation_harness_resumes.by_fp`/`.total`, which gate hold entry. Unrelated merges therefore cannot exhaust the harness-fix budget and turn a later `harness_error` into a `capped` CRITICAL park.
- **Once per head.** A successful resume removes the hold, and the next failure records the new dispatch head as its `integration_head`. A failed dispatch restores the hold unchanged, so the same head is retried on the next tick. A hold with no `integration_head` (entered before Phase 4) never takes this path.

**Alternatives rejected:**
- Reading `.ai/validate.yml` for the family (Q1 B): repo content, not run metadata.
- Exact file paths (Q2 B): 19-20 of 20 slots, and no new files.
- Admitting `.j2` repo-wide (Q3 B): widens the editor surface for no current need.
- A plan-time hold (Q4 B): files an issue that only waits.
- `completed` as the close reason (Q9 B): would read as "fixed by a heal" in `heal_already_fixed_candidates`.

## Decisions

Operator answers from the planning session, 2026-10-26 (Q1-Q11 all **A**). Q12-Q14 were taken as clear-winner assumptions and were not overridden.

### D1 — Harness family source (Q1)
- **Chosen:** a `family=<name>` field on the existing `VALIDATION_FAILURE_SUMMARY` log line, validated against the five known families and read from the verified run's job logs.
- **Alternatives considered:** read `.ai/validate.yml` at the failed commit; no family detection (only `_shared` and the scripts).
- **Why:** it keeps the scope derived from run metadata, the same trust level as failing-test subjects (Q37: A).

### D2 — Scope shape (Q2)
- **Chosen:** two directory entries (the family directory and `_shared`), allowed only in that exact shape, plus the four harness scripts.
- **Alternatives considered:** exact existing files only.
- **Why:** exact files use 19-20 of the 20 slots and cannot cover the new template files a harness fix often needs (#8181 added one).

### D3 — `.j2` in the isolated editor (Q3)
- **Chosen:** admit `.j2` only under `workflow-templates/validation-harness/`.
- **Alternatives considered:** admit `.j2` repo-wide; do not admit it.
- **Why:** without it a widened scope cannot fix a template, and admitting it anywhere else has no current use.

### D4 — Where a fix on `main` is caught (Q4)
- **Chosen:** at intake, before filing. File nothing and post the existing `already-fixed` outcome.
- **Alternatives considered:** file the issue and hold it at planning.
- **Why:** an issue that only waits is the human-attention cost this plan removes. The consumer hold already understands `already-fixed`.

### D5 — What counts as already fixed (Q5)
- **Chosen:** a commit on `main` after the consumer's pin whose full message cites the exact source `owner/repo#N`.
- **Alternatives considered:** additionally require that the commit touches a harness or workflow path.
- **Why:** repo commit messages already carry `Refs <source>` (e.g. #8181). A wrong match only delays a heal by one promotion: the `main-fix:v1` marker allows one suppression per fingerprint and fix, so later commits that merely cite the source cannot suppress it again.

### D6 — Hold waits for the fix commit (Q6)
- **Chosen:** a separate `validation-route-fix:v1` marker line. The hold resumes only once the pin contains the fix.
- **Alternatives considered:** keep resuming on any pin movement.
- **Why:** a `stable` hotfix can move the pin without the fix, which wastes a resume. A separate line keeps old pollers' anchored regex working.

### D7 — Auto-close trigger (Q7)
- **Chosen:** close when every recorded source (marker plus occurrence comments) is resolved: its issue or pull request is closed, or its tracking issue carries `ai:validated` and a validation run for it succeeded after the heal was filed.
- **Alternatives considered:** only on source issue close; only on validation pass.
- **Why:** #579 passed validation long before closing. Requiring every source protects other projects routed onto the same heal.

### D8 — Heal with an open PR (Q8)
- **Chosen:** leave it open.
- **Alternatives considered:** close the issue and its PR.
- **Why:** the PR flow (`workflow_failure_heal_pr_reconcile.sh`, review) owns in-flight work.

### D9 — Close reason (Q9)
- **Chosen:** `not_planned`, with a comment naming the resolution.
- **Alternatives considered:** `completed`.
- **Why:** nothing was fixed by the heal. `completed` would make the closed issue count as a fix in `heal_already_fixed_candidates`. Because the poller parks a hold on any `not_planned` close, the close also adds `ai:heal-source-resolved`, and Phase 3's poller change resumes instead of parking when it sees that label.

### D10 — Consumer re-validate after a merge (Q10)
- **Chosen:** during a harness hold, resume once per new integration-branch head that is ahead of the dispatch head, within a separate integration-resume cap.
- **Alternatives considered:** only for merges touching Docker or build files; leave it out.
- **Why:** this automates the manual `/revalidate` on #579. The separate cap bounds the cost of unrelated merges without spending the harness-fix resume budget.

### D11 — Phase split (Q11)
- **Chosen:** four independent phases.
- **Alternatives considered:** two phases split by repository side.
- **Why:** each phase has its own switch and rollback, and none depends on another merging first.

### D12 — Switches, failure handling, step-glob fix (Q12-Q14, assumed)
- **Chosen:** default-on `HEAL_*_ENABLED` / `VALIDATION_HARNESS_*_ENABLED` switches; fail open to today's behaviour on any lookup error; fix the step-script glob transfer rejection in Phase 2.
- **Alternatives considered:** default-off switches; hold on lookup errors; leave the step-glob bug.
- **Why:** these follow the repo switch convention and §15's fail-open rule, and Phase 2 edits the same transfer entry check.

## Phases & Merge Strategy

The AI orchestrator ships each phase as its own PR straight to production. All four are independently mergeable in any order (Q11: A).

**Phase 1: catch fixes already on `main` at intake.**
- **Scope:** deterministic main-fix check in the intake, the `validation-route-fix:v1` line, and poller support for it.
- **Files:** `scripts/workflow_failure_heal_intake.sh`, `scripts/workflow_failure_heal.py`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/workflow-failure-heal-intake.yml` (env only), tests, README, fragments.
- **Done when:**
  - a consumer report whose source is cited by a commit after the pin on `main` files no issue and posts `already-fixed` with the fix line;
  - a poller with this phase waits for the pin to contain the fix;
  - all existing intake and poller tests pass.
- **Independence:**
  - The poller change is a no-op without the new line.
  - The intake change works with old pollers: their anchored route regex ignores the extra line, and they resume on pin ancestry (today's `already-fixed` rule).
- **Rollback:** set `HEAL_MAIN_FIX_DEDUPE_ENABLED=false` (intake) or `VALIDATION_HARNESS_FIX_SHA_WAIT_ENABLED=false` (poller), or revert the PR.

**Phase 2: deterministic harness scope.**
- **Scope:** the `family=` log field, family extraction and freezing, harness scope entries, `.j2` admission in the isolated editor, and the step-glob transfer fix (Q14: A).
- **Files:** `scripts/validate_process.sh`, `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_intake.sh`, `scripts/review_untrusted_workspace.py`, `.github/workflows/workflow-failure-heal-intake.yml` (env), tests, README, fragments.
- **Done when:**
  - a harness_error validation report renders a marker holding the family directory, `_shared` and the four scripts;
  - the heal plan gate approves a plan listing existing or new `.j2` files under those directories;
  - an isolated-editor round trip edits and creates a `.j2` file there and is refused one elsewhere.
- **Independence:**
  - Until consumers run a `validate_process.sh` carrying `family=` (next `@stable` sync), no family is found and scopes are unchanged.
  - `.j2` admission is harmless on its own.
- **Rollback:** set `HEAL_HARNESS_SCOPE_ENABLED=false`, which stops new scopes from getting harness entries. Issues already filed keep their frozen marker (by design). The `.j2` admission and the step-glob transfer fix have no switch and roll back only by reverting the PR (see G7).

**Phase 3: auto-close heal issues when the source resolves.**
- **Scope:** occurrence source marker, the source-resolved decision logic, the scan mode and its job, the `ai:heal-source-resolved` label, and the poller's resume-instead-of-park on that label.
- **Files:** `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_intake.sh` (labels in the `_heal_fetch_issue_list` projection), `scripts/workflow_failure_heal_recovery_scan.sh`, `scripts/workflow_failure_heal_queue.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/workflow-failure-heal-intake.yml`, `.github/ai/label_contract.v1.json`, `scripts/label_helpers.sh`, tests, README, fragments.
- **Done when:**
  - a fixture heal issue whose single source issue is closed is closed `not_planned` with the comment;
  - a fixture `validation_failure` heal whose tracking issue carries `ai:validated` and has a successful validation run after the heal was filed is closed the same way;
  - a heal issue a person reopened after the scan closed it stays open;
  - one with an open `ai/issue-<N>` PR, an unresolved duplicate source, or an occurrence comment without the new marker stays open;
  - a scan with lookup failures closes nothing;
  - a held fixture project whose heal issue is closed `not_planned` with `ai:heal-source-resolved` resumes instead of parking.
- **Independence:** reads only existing markers plus its own additive ones. Consumer pollers older than this phase still park on the close (see Risks).
- **Rollback:** set `HEAL_SOURCE_RESOLVED_CLOSE_ENABLED=false` (the job-level `if:` then skips the job, which shows as skipped in the run; the script's own switch read only logs `outcome=skip reason=invalid_switch` for an unrecognised value), or revert the PR.

**Phase 4: consumer re-validate after a merge during a harness hold.**
- **Scope:** record the integration head on hold entry, and resume once per new head.
- **Files:** `scripts/orchestrate_poll_process.sh`, tests, README, fragments.
- **Done when:**
  - a held project whose integration head moved resumes once, and a failed dispatch of that resume is retried on the next tick;
  - an unchanged head, a non-descendant head, a missing `integration_head` or an exhausted integration cap do not resume, and the harness-fix counters are unchanged;
  - existing hold tests pass.
- **Independence:** additive state field; old holds are unaffected.
- **Rollback:** set `VALIDATION_HARNESS_INTEGRATION_RESUME_ENABLED=false`, or revert the PR.

## Implementation Steps

New identifiers below were chosen to be unique. Each step re-checks them with `grep -rn` before use (§6), and picks a distinct name if one is taken.

### Phase 1: catch fixes already on `main` at intake

1. **`scripts/workflow_failure_heal.py`: add `main_fix_commit(compare_json, source_ref)`.** It returns `{"sha": <40-hex>, "subject": <≤160 chars>}` or `None`.
   - Input checks: `source_ref` must match `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]{0,9}$` and its repo must be in the consumer registry.
   - Matching: scans `commits[].commit.message` (full text) for the reference bounded by `(?<![A-Za-z0-9_./-])` and `(?![A-Za-z0-9_])`, so `owner/repo#579abc` and `owner/repo#5790` do not match `owner/repo#579`. The newest match wins.
   - Truncated results: when `total_commits` exceeds the commits returned, it scans what was returned and adds `"truncated": true`.
   - Expose it as the `main-fix` CLI subcommand (`--compare-json`, `--source-ref`, JSON to stdout, exit 0).
2. **`scripts/workflow_failure_heal_intake.sh`: switch.** Add the switch `HEAL_MAIN_FIX_DEDUPE_ENABLED`, read with the existing switch helper pattern (the `HEAL_SCOPE_RECOVERY_ENABLED` / `HEAL_REISSUE_DEDUPE_ENABLED` blocks at lines ~223-244: unset, empty or `true` is on; `false` is off; anything else is off with `::warning::WORKFLOW_HEAL main_fix outcome=skip reason=invalid_switch`).
   - Initialise `MAIN_FIX_SHA=""`, `MAIN_FIX_SUBJECT=""` and `MAIN_FIX_DEFAULT_BRANCH=""` next to `VALIDATION_ROUTE_OUTCOME=""` (line ~1050), before the EXIT trap is installed (line 1234), and read them as `"${MAIN_FIX_SHA:-}"` at every read site. The intake runs under `set -euo pipefail` (line 179), and the step 4 guard and `_validation_route_post` (also reached from the EXIT trap) read `MAIN_FIX_SHA` on every report. Without the initialisation, every non-matching report and every run with the switch off would abort on an unbound variable.
3. **Same file: the check itself.**
   - Placement: right after the branch-progress block (after line 2112) and before the scope freeze (line ~2290).
   - Conditions: switch on, source is a consumer repo (`SOURCE_REPO != SELF_REPO`), `WRAPPER_SHA` is 40-hex, `TARGET_BRANCH_DEFAULT` is the stable target, and the report carries a source issue (`validation_failure`, consumer issue phase failures).
   - Default branch: set `MAIN_FIX_DEFAULT_BRANCH` before the check, outside the provenance block, with the same rule as lines 984-988 (`WORKFLOW_HEAL_DEFAULT_BRANCH`, else `.repository.default_branch` from `GITHUB_EVENT_PATH`, then `tr -cd 'A-Za-z0-9._/-' | head -c 255`). Do not read `PROVENANCE_DEFAULT_BRANCH`: it is assigned only inside the `phase_failure`/`autofix_failure`/`workflow_run` block (line 969), so for a `validation_failure` report it is unset and `set -u` aborts the intake. There is no `DEFAULT_BRANCH` in the script either. When `MAIN_FIX_DEFAULT_BRANCH` is empty, log `outcome=skip reason=default_branch_unavailable` and continue unchanged.
   - Action: issue one call, `gh api --method GET "repos/${SELF_REPO}/compare/${WRAPPER_SHA}...${MAIN_FIX_DEFAULT_BRANCH}"`, then run `main-fix`.
   - §15 comment: put an audit comment above the call, explaining that the existing compare's head is `stable`, so it cannot answer for `main`.
   - Fail open: when the call or the parse fails, log `WORKFLOW_HEAL main_fix outcome=skip reason=compare_failed|parse_failed` and continue unchanged.
4. **Same file: the match path.**
   - Prior-suppression check first: read the source's comments (reuse the provenance comment snapshot when the report kind has one, else one paginated comments read, only on a match). If a pipeline-authored `<!-- workflow-failure-heal:main-fix:v1 fp=<fp> fix=<sha> -->` exists for this `fp` and its `fix` is not among the compare's commits, log `main_fix outcome=skip reason=prior_fix_did_not_hold` and continue unchanged. If such a marker's `fix` is still among the compare's commits (not yet promoted), suppress with that recorded `fix` rather than the newest match, and do not post another `already-fixed` outcome comment or `main-fix:v1` marker (log `main_fix outcome=suppressed reason=already_recorded`). For a `validation_failure` report this path still calls `_validation_route_post` with a short body naming the recorded fix, so the route marker is `already-fixed` and the `validation-route-fix:v1` line carries that recorded `fix` (step 5). Posting nothing would leave `VALIDATION_ROUTE_POSTED` false, and the EXIT trap `_validation_route_exit` (lines 1154-1176) would then post a `skipped` route comment with no fix line; as the newest route marker it would make the consumer hold resume on pin movement alone. Other report kinds post nothing. Test: a repeat `validation_failure` report with the fix still unpromoted posts one route comment carrying `validation-route-fix:v1` for the recorded fix, no `skipped` fallback and no second `main-fix:v1` marker. This keeps "one suppression per fix": a newer commit that merely cites the source cannot replace the recorded fix.
   - Set `MAIN_FIX_SHA`, `MAIN_FIX_SUBJECT`, `ALREADY_FIXED_COMMITS=${MAIN_FIX_SHA:0:12}`, `CLASSIFICATION="already-fixed"`.
   - Skip the scope freeze and the diagnosis model call, and write a deterministic `DIAG_FILE` (`## Summary` naming the source and the commit, `## Fixed by` citing it). The `already-fixed)` arm (lines 3006-3027) runs `cat`/`sed` on `DIAG_FILE`, which would abort under `set -e` if it were missing.
   - Guard the `parse-classification` recompute (line 2520) and the `check-already-fixed` gate (lines 2526-2543) with `[ -z "${MAIN_FIX_SHA}" ]`. Without the guard the recompute overwrites the classification, and `check-already-fixed` demotes it, because the commit is on `main`, not on `PROGRESS_BRANCH`.
   - In the `already-fixed)` arm, when `MAIN_FIX_SHA` is set, use main-fix wording everywhere the arm names `${PROGRESS_BRANCH}`: the `gained commit(s)` sentence (line 3009), the wrapper-sync sentence (line 3012), the `no_issue … branch=` log field (line 3025, which logs `branch=${MAIN_FIX_DEFAULT_BRANCH}`) and the Telegram message (line 3026). For a main-only fix `PROGRESS_BRANCH` is `stable`, which does not have it. The comment names the commit (`shubhodeep1/coding-workflows@<sha>`), says the fix reaches the consumer on the next `stable` promotion and wrapper sync, and ends with the `main-fix:v1` marker.
   - Log `WORKFLOW_HEAL main_fix outcome=suppressed source=<ref> fix=<sha> truncated=<bool>`.
   - `check_heal_already_fixed_claim` and the `check-already-fixed` gate stay in force for model verdicts; only the deterministic main-fix match bypasses them.
5. **Same file, `_validation_route_post` (line 1092): the fix line.** When the outcome `_validation_route_post` already computes (the local `route_outcome="$(_validation_route_outcome)"`, line 1103) is `already-fixed` and `MAIN_FIX_SHA` is 40-hex, append `<!-- workflow-failure-heal:validation-route-fix:v1 fp=<fp> fix=<sha> -->` after the `grep -v 'workflow-failure-heal:validation-route'` filter (line 1122) and before the route marker (line 1125). That filter matches the fix line's substring, so the line must not be in the body when it runs. The route marker stays the last line, unchanged. Do not test `VALIDATION_ROUTE_OUTCOME` itself: it is only ever assigned empty, `skipped`, `duplicate` or `bounced` (lines 1050, 1172, 2003, 2577), and `already-fixed` comes only from `_validation_route_outcome()` reading `CLASSIFICATION`, so that test would never match and the fix line would never be written.
6. **`scripts/orchestrate_poll_process.sh`: poller switch.** Add the switch `VALIDATION_HARNESS_FIX_SHA_WAIT_ENABLED` beside the other harness switches (lines 2497-2536), same semantics.
7. **Same file, the routing-marker reader (~line 15320).**
   - Also capture an optional `validation-route-fix:v1` line from the same comment whose `fp` equals the route `fp`.
   - Validate `fix` as `^[0-9a-f]{40}$` and store it as `validation_harness_hold.fix` when the hold records the route.
8. **Same file, `validation_harness_hold_tick` step 3 (lines 15844-15852).**
   - When the switch is on and `fix` is set: after the existing support-vs-pin check passes, compare `fix` with the pin using the memoized compare helper (`_validation_harness_hold_compare`, same cache). Both are coding-workflows commits, the repository that helper is fixed to (line 15455), so it can be reused here, unlike the consumer-repo compare in Phase 4.
   - If the pin is not `ahead`/`identical` of `fix`, log `VALIDATION_HARNESS_HOLD … outcome=waiting reason=fix_not_in_pin` and return 0.
   - A compare failure logs `outcome=unknown reason=fix_compare_failed` and waits, matching the existing `compare_failed` handling.
9. **Wiring.** Export `HEAL_MAIN_FIX_DEDUPE_ENABLED: ${{ vars.HEAL_MAIN_FIX_DEDUPE_ENABLED || '' }}` in the intake job env of `.github/workflows/workflow-failure-heal-intake.yml`. Forward `VALIDATION_HARNESS_FIX_SHA_WAIT_ENABLED` the same way the other harness switches are forwarded in `.github/workflows/orchestrate_poll.yml`.
10. **Docs.**
    - README switch rows for both switches.
    - Rewrite the README harness-hold "Resume" bullet (README.md ~3876-3881): when the routing comment recorded a fix commit, the pin must also contain that commit.
    - `changelog.d/<issue>-heal-main-fix-dedupe.md`.
    - `agents.d/<issue>-heal-main-fix-dedupe.md`, with blocks for the heal pipeline narrative and for `VALIDATION_HARNESS_HOLD` reasons (`fix_not_in_pin`, `fix_compare_failed`).

### Phase 2: deterministic harness scope

1. **`scripts/validate_process.sh`: compute the family.**
   - Compute `VALIDATION_HARNESS_FAMILY_NAME` once, after `.ai/validate.yml` is located and before the first point that can emit a harness error after manifest load.
   - Read `type` with `python3 -I` (`yaml.safe_load`, the same interpreter as the renderer) and accept it only if it is one of `node-hardhat-solidity`, `node-runtime`, `python-mongo-flask`, `python-mongo-repo-checks`, `python-repo-checks`. Otherwise use `none`.
   - Default is `none` (bootstrap failures before the manifest).
   - Keep the list in sync with `FAMILY_REGISTRY` through a test (step 9), not an import.
2. **Same file: the log field.** Insert ` family=${VALIDATION_HARNESS_FAMILY_NAME:-none}` immediately after `raw_status=${raw_status}` in the three `VALIDATION_FAILURE_SUMMARY` lines (58, 1366, 2176). Nothing else in the lines changes. No parser reads these lines today (checked with `grep -rn VALIDATION_FAILURE_SUMMARY scripts .github`).
3. **`scripts/workflow_failure_heal.py`: `extract_harness_family(text)`.**
   - Matches `^\s*VALIDATION_FAILURE_SUMMARY .* raw_status=harness_error family=(<enum>)(?=\s)` on timestamp-stripped lines, using the same cleaning as `extract_failing_tests`.
   - Returns the family when at least one line matches and all matches agree; else `""`.
   - Expose it as `heal-scope harness-family --log-files …`.
   - Add `VALIDATION_FAILURE_SUMMARY` to `_SOFT_LOG_PATTERNS` (line 569) if it is not already covered, so `filter_log` keeps the line.
4. **`scripts/workflow_failure_heal_intake.sh` (line ~2296): freeze the family.**
   - Next to `FAILING_TESTS_JSON`, compute `HARNESS_FAMILY_NAME` only when the switch `HEAL_HARNESS_SCOPE_ENABLED` is on and `SOURCE_KIND=validation_failure`.
   - Freeze it as `harness_family` in the `scope_inputs_frozen.json` jq (`SCOPE_INPUTS_FROZEN`, line 2291, and the jq after it).
   - Forward it: add `harness_family: (.harness_family // "")` to the fixed `_open_issue` projection that writes `scope_inputs.json` for `heal-scope render` (line 2753). That projection lists its fields explicitly, so without this edit render never receives the family and G1/G2 have no effect. The intake wiring test (step 9) must assert the field reaches `scope_inputs.json`, not only the frozen file.
   - Log `WORKFLOW_HEAL harness_scope outcome=family family=<f>|none`.
5. **`scripts/workflow_failure_heal.py`: subjects and directory entries.**
   - Add `HEAL_SCOPE_HARNESS_SUBJECTS = ("scripts/validation_harness_sandbox.sh", "scripts/validate_driver.sh", "scripts/validate_process.sh", "scripts/render_validation_templates.py")`.
   - Add `_HEAL_SCOPE_HARNESS_DIR_RE = re.compile(r"workflow-templates/validation-harness/(?:_shared|node-hardhat-solidity|node-runtime|python-mongo-flask|python-mongo-repo-checks|python-repo-checks)")`.
   - Add `heal_scope_harness_subjects(family, exists, is_dir)`. It returns the four scripts (existence-checked) plus the two directory entries, each only if `is_dir` confirms a tree at the scope commit.
6. **Same file, `render_heal_scope_marker` (3301-3333).**
   - Add a keyword `harness_subjects: Iterable[str] = ()`.
   - Order: crash file, workflow paths, harness subjects, the first 8 test subjects, changed files, remaining subjects.
   - A directory entry passes only if it `fullmatch`es `_HEAL_SCOPE_HARNESS_DIR_RE` and came from `harness_subjects`, mirroring the `step_glob` special case at line 3316 (`path in subjects`). It skips the regular-file `exists` check (it was tree-checked in step 5). Any other entry from `harness_subjects` is dropped. This is the only place the directory restriction is enforced.
   - In `_cmd_heal_scope` (6497-6530), read `harness_family` from the frozen JSON and pass the subjects. `is_dir` uses `git ls-tree` at `--ref`, alongside `_heal_regular_file_at_ref` (6451).
7. **Same file, `_heal_scope_marker_shape_ok` (3340) and `verify_heal_scope` (3395): no change.**
   - Both are string checks and cannot tell a directory from an extensionless file (`Makefile`, `Dockerfile`, `Pipfile` pass today and must keep passing). The two directory entries already pass them as strings, as does `_safe_heal_path` (3112).
   - A directory entry can only appear in a trusted marker if render emitted it (step 6), because the marker is trusted only from an unedited, pipeline-authored body.
8. **`scripts/review_untrusted_workspace.py`.**
   - In `allowed()` (128-146), additionally allow a `.j2` suffix when the path starts with `workflow-templates/validation-harness/`. `SUFFIXES` itself is unchanged.
   - In the transfer scope entry check (lines 944-948), additionally accept `scripts/[a-z0-9][a-z0-9_]{0,79}_step_\*\.sh` (Q14: A, the latent rejection of the existing step-script glob). The two directory entries already pass its `[A-Za-z0-9_./-]+` rule.
   - Check that `entry_matches` in the transfer (lines 463-469) matches files under a directory entry and new files under it. Add the handling if it does not.
9. **Tests**, extending existing files:
   - `tests/test_workflow_failure_heal_scope_tests.py`:
     - extraction: agreeing lines, disagreeing lines, a forged unknown family, no line, timestamped lines;
     - render with harness subjects at a scope commit: dir entries present, ordering, and the 20-slot budget with a large `changed_files`;
     - render drops a harness subject outside the two allowed directories (`workflow-templates/validation-harness/foo`, `workflow-templates/x`), and shape/verify keep accepting the two dir shapes and existing extensionless entries such as `Makefile`;
     - intake wiring: the family is frozen and reaches `scope_inputs.json` through the line 2753 projection, and the switch off leaves the marker unchanged.
   - A parity test: the family enum equals `FAMILY_REGISTRY` names.
   - `tests/test_files_touched_scope_guard*.py` (whichever covers `run_heal_plan_scope_gate`): a plan listing an existing and a new `.j2` under the family dir approves; one outside holds.
   - The `review_untrusted_workspace.py` transfer tests:
     - `.j2` under the harness dir is visible, editable and creatable;
     - `.j2` elsewhere is refused;
     - the step-glob entry no longer raises.
   - `validate_process.sh` tests that assert `VALIDATION_FAILURE_SUMMARY` shape: add `family=`.
10. **Wiring and docs.**
    - Export `HEAL_HARNESS_SCOPE_ENABLED` in the intake job env.
    - README switch row.
    - `changelog.d/<issue>-heal-harness-scope.md`.
    - `agents.d/<issue>-heal-harness-scope.md`, extending the "Heal scope from failing tests" narrative and the "Validation harness isolation" section.

### Phase 3: auto-close heal issues when the source resolves

1. **`scripts/workflow_failure_heal.py`, `compose_occurrence_comment` (5308): the occurrence marker.**
   - Append `<!-- workflow-failure-heal:occurrence-source=<repo>#<n> -->` when the payload has a source issue.
   - For run-only sources, write `<repo>#run`, the shape the existing composers use for the `source=` marker (`compose_issue_body` at 5156; e.g. line 5538 in `compose_synced_security_issue`).
2. **Same file: `heal_source_resolved_decision(issue, comments, source_states, validation_passes, open_heal_branches, pipeline_login)`.** It is pure and returns `{"decision": "close"|"keep"|"unknown", "reason": <token>, "sources": [...]}`.
   - Rules:
     - The issue must be authored by `pipeline_login`.
     - Sources are the `source=` marker plus every pipeline-authored occurrence comment's `occurrence-source=` marker. An occurrence comment (first line is the occurrence marker) without the source marker gives `unknown`/`occurrence_unmarked`. No source is inferred retroactively: issues with such comments stay manual by design.
     - Any `#run` source gives `keep`/`run_source`, because it has no issue to resolve.
     - A source is resolved if its issue state is `CLOSED` or its pull request state is `CLOSED`/`MERGED`, or (for `validation_failure` heals, `classification` marker) the tracking issue carries `ai:validated` and a successful validation run for it is newer than the heal's `created_at`. A successful run without the label (a capacity-wait run) is not a pass.
     - A source whose alias came back null or with an error gives `unknown`/`lookup_failed` for the whole issue.
     - An open `ai/issue-<N>` PR gives `keep`/`heal_pr_open` (Q8: A).
     - A missing lookup result gives `unknown`/`lookup_failed` (fail open = keep).
   - Expose it as the `heal-source-resolved` CLI subcommand over JSON files.
   - Same file, `budget_decision` (closed-issue branch at lines 4913-4919): skip a closed issue whose labels contain `ai:heal-source-resolved` when filling `prior_same_fp` and `prior_source_lineage`, so a source-resolved close adds no generation toward `lineage_cap` (lines 5000-5013). Still add it to `closed_lineage` (lines 4916, 4919), because `resolve_reissue_roots` and the membership check at line 4948 use that set to match an open re-issue to its closed root; dropping it there would file a second heal next to an open re-issue. The labels come from the heal-issue listing the budget already receives, `_heal_fetch_issue_list` in `scripts/workflow_failure_heal_intake.sh` (lines 601-607), whose `--jq` projection (line 605) has no labels today: add `labels: [.labels[].name]` to that projection, with no new call. Issues without the label keep today's counting.
3. **`scripts/workflow_failure_heal_recovery_scan.sh`: add a `--source-resolved` mode.**
   - Gates: parse the flag first and branch before the existing recovery gates (lines 117-135). The mode reads its own switch `HEAL_SOURCE_RESOLVED_CLOSE_ENABLED` (unset, empty or `true` on; `false` off; any other value off with a warning), accepts only `schedule` (like the recovery scan), and ignores `HEAL_SCOPE_RECOVERY_ENABLED`.
   - Listing: reuse the paginated GraphQL listing, but with its own jq filter: every open, pipeline-authored `ai:workflow-heal` issue, without the `contains("ai:workflow-heal-scope") | not` filter (line 187), which would drop every scoped heal issue. Add `createdAt` and `timelineItems(itemTypes:[REOPENED_EVENT], last:1){ nodes { ... on ReopenedEvent { createdAt } } }` to the mode's node selection, and `created_at` / `last_reopened_at` to its jq projection. `created_at` feeds the validation-pass rule and the runs query in (c); `last_reopened_at` feeds the reopen check. Both come from the same listing call, with no per-issue read. The recovery scan's own query and projection are unchanged. Logging reuses the script's helpers.
   API usage, per scan:
   - **(a)** One `GET repos/<self>/pulls?state=open&per_page=100` (paginated). Keep heads `ai/issue-<N>`.
   - **(b)** Per repository with candidate sources (consumer repos and this repo): one aliased GraphQL query per 25 sources, `repository(owner,name){ s<N>: issueOrPullRequest(number:N){ __typename ... on Issue { state closedAt labels(first:50){ nodes { name } } } ... on PullRequest { state closedAt } } … }`. Partial data is accepted per alias: an alias that is null or named in `errors` (`NOT_FOUND`) marks only that source `lookup_failed`; a whole-call failure marks every source of that batch `lookup_failed`. Document the batching contract per §15: input shape, output shape, call count, fail-open.
   - **(c)** Per consumer repo with a `validation_failure` candidate, for each validate wrapper file (`ai-validate.yml`/`internal-validate.yml`, per `VALIDATION_WRAPPER_WORKFLOW_FILES`, `workflow_failure_heal.py:186`): `GET repos/<repo>/actions/workflows/<wrapper>/runs?event=workflow_dispatch&status=success&created=>=<oldest heal created_at>&per_page=100`, paginated up to 5 pages. The listing is filtered by workflow on the server, so other workflows' runs cannot push the validation run off the page; a 404 means the repo has no such wrapper and counts as an empty list. Keep runs whose `display_title` contains `[tracking:<n>]`. When the 5-page bound is reached without a match for a source, that source has no validation-pass evidence and stays unresolved (`reason=validation_runs_truncated`), which keeps the issue open.
   - Issue comments come from one `issues/<N>/comments` read per candidate.
   - Before any write, take the heal-intake queue ticket with `HEAL_INTAKE_QUEUE_JOB=source-resolved-scan` (`scripts/workflow_failure_heal_queue.sh acquire`, `release` in an `if: always()` step), then re-read the candidate's comments, its `state` and `timelineItems(itemTypes:[REOPENED_EVENT], last:1)` (one GraphQL read per candidate that would close), and the open `ai/issue-<N>` PRs (one more paginated pulls list per scan, only when at least one candidate would close), and re-run the decision with the refreshed `last_reopened_at`. A new occurrence, a reopen or a heal PR that landed while the scan waited for the ticket (up to `HEAL_INTAKE_QUEUE_MAX_WAIT_SECONDS`) changes the decision, and the issue is kept.
   - Queue changes: add `source-resolved-scan` to `HEAL_QUEUE_JOBS` and `_HEAL_QUEUE_TICKET_RE` (`workflow_failure_heal.py:5883-5885`) and to the accepted list in `workflow_failure_heal_queue.sh` (lines 36 and 164). In `heal_queue_position` (line 5952), treat an earlier ticket of the waiting run as stale only when its `job` also matches, because the scan and `recovery-scan` run in the same scheduled run.
   - On `close`: run `ensure_label_exists "ai:heal-source-resolved"` once per scan (a failure skips every close that scan, `reason=label_unavailable`), post the comment (one attempt, `GH_RETRY_MAX_ATTEMPTS=1`) ending in `<!-- ai:heal-source-resolved:v1 issue=<N> -->`, add the label `ai:heal-source-resolved` and confirm it from the label POST's response, then `PATCH issues/<N> state=closed state_reason=not_planned`. A failed or unconfirmed label write skips the PATCH (`outcome=keep reason=label_failed`), so the issue never closes `not_planned` without the label the poller needs; the next scan retries through the existing-marker path. When a pipeline-authored close marker is already present (an earlier PATCH failed), skip the comment and retry only the label and the PATCH, unless the `last_reopened_at` re-read after the queue ticket is newer than that marker comment's `created_at`: then a person reopened the issue after the scan closed it, so log `outcome=keep reason=human_reopened` and do not close it again. A PATCH that keeps failing is retried on every scan and logs `outcome=keep reason=close_failed`; it needs no separate cap, because the issue simply stays open as today. The comment says which sources resolved and how (closed, or validation run `<url>` passed with `ai:validated`). It ends with `Refs <source>` lines and the "the next occurrence files a new heal" note.
   - Log `WORKFLOW_HEAL source_resolved outcome=closed|keep|unknown|skip issue=<N> reason=<token>`.
   - Cap closes per scan with `HEAL_SOURCE_RESOLVED_CLOSE_MAX_PER_SCAN` (default 10).
4. **`.github/workflows/workflow-failure-heal-intake.yml`: new job `source-resolved-scan`.**
   - On the existing `schedule` only: `if: github.event_name == 'schedule' && vars.HEAL_SOURCE_RESOLVED_CLOSE_ENABLED != 'false'`. The workflow's `workflow_dispatch` requires `payload_json` and always runs the intake job, so it is not an entry point for the scan.
   - Mirror `recovery-scan`'s checkout, token (`secrets.GH_PAT`) and env pattern (lines 338-400).
   - Pass the switch to the script for the invalid-value warning.
   - Use its own job-level concurrency group; serialization with intake and `recovery-scan` comes from the queue ticket (step 3), as for those jobs. Grant `issues: write`.
5. **Poller: resume instead of parking on a source-resolved close (`scripts/orchestrate_poll_process.sh`).**
   - `_validation_harness_hold_issue_state` (line 15487): extend the `--jq` of the GET it already makes to add a fourth field, whether `labels[].name` contains `ai:heal-source-resolved`, and expose it as a new `VHH_ISSUE_SOURCE_RESOLVED` (collision-check first). No new API call.
   - `validation_harness_hold_tick` `not_planned)` arm (lines 15866-15872): when that flag is set and the switch `VALIDATION_HARNESS_SOURCE_RESOLVED_RESUME_ENABLED` is on (same semantics as the other harness switches), call `_validation_harness_hold_resume` with the resolved pin (log reason `heal_source_resolved`) instead of `_validation_harness_hold_end_park`. Pass the helper's optional fourth argument as `source_resolved` (the same argument Phase 4 adds as `integration`). It keeps today's counters and changes only the resume comment, which says the heal issue was closed because its source resolved, instead of the harness-fix wording at lines 15717-15721. Forward the switch in `.github/workflows/orchestrate_poll.yml`.
   - Register the label in `.github/ai/label_contract.v1.json` and `scripts/label_helpers.sh`.
6. **Tests**, in `tests/test_workflow_failure_heal.py` (decision unit cases for every rule above), `tests/test_workflow_failure_heal_queue.py` (queue cases) and `tests/test_workflow_failure_heal_recovery_scan.py` (scan-mode test with a stub `gh`, and the job wiring) covering:
   - close, including a pull-request source and a self-repo source;
   - close of a `validation_failure` heal whose tracking issue carries `ai:validated` and has a successful validation run newer than the heal's `created_at` (taken from the listing); a run older than `created_at` keeps it;
   - keep for an issue reopened after the close marker (`human_reopened`), including a reopen that lands between the listing and the re-read after the queue ticket, and keep when a heal PR appears in that window;
   - a validation run found behind more than 100 unrelated successful `workflow_dispatch` runs of other workflows still closes the heal (the per-wrapper listing), and a wrapper 404 counts as no runs;
   - keep for an open PR, an unresolved duplicate, an unmarked occurrence, a run-only source, and a successful run without `ai:validated`;
   - lookup failure closes nothing, and one `NOT_FOUND` alias keeps only its issue;
   - an occurrence posted between the listing and the close keeps the issue;
   - an existing close marker skips the comment and retries only the PATCH;
   - the mode runs with `HEAL_SCOPE_RECOVERY_ENABLED=false`;
   - a failed `ensure_label_exists` or label POST skips the PATCH;
   - queue: a `source-resolved-scan` ticket is accepted, and in one run it and a `recovery-scan` ticket do not mark each other stale;
   - `budget_decision`: a closed same-fingerprint heal with `ai:heal-source-resolved` adds no generation, and one without the label still does; an open unblock-judge re-issue of a source-resolved closed heal still resolves to a `match: reissue` duplicate;
   - the per-scan cap;
   - call count per scan.
   - Also extend the workflow wiring test in `tests/test_workflow_failure_heal_recovery_scan.py` (the job runs only on `schedule`), the occurrence marker case, and the harness-hold tests (a `not_planned` close with the label resumes; without it, or with the switch off, it still parks).
7. **Docs.** Rewrite the README harness-hold "Not planned" bullet (README.md ~3885): a heal issue closed as not planned with `ai:heal-source-resolved` resumes the hold; any other not-planned close still parks. README rows for `HEAL_SOURCE_RESOLVED_CLOSE_ENABLED`, `HEAL_SOURCE_RESOLVED_CLOSE_MAX_PER_SCAN` and `VALIDATION_HARNESS_SOURCE_RESOLVED_RESUME_ENABLED`, `changelog.d/<issue>-heal-source-resolved-close.md`, and `agents.d/<issue>-heal-source-resolved-close.md`.

### Phase 4: consumer re-validate after a merge during a harness hold

1. **`scripts/orchestrate_poll_process.sh`: switches.** Add the switch `VALIDATION_HARNESS_INTEGRATION_RESUME_ENABLED` and the cap `VALIDATION_HARNESS_INTEGRATION_MAX_RESUMES` (default 2 per project, digits only, else 2) beside the harness switches (lines 2497-2536).
2. **Same file: record the baseline.**
   - In `dispatch_validation_if_needed`, just before the dispatch call, resolve the integration branch head with `_branch_head_sha` (defined at line 5570; the `/revalidate` block calls it at line 26150), and after a successful dispatch store it as `validation_dispatch_integration_head` (40-hex, else omitted). One GET per dispatch. Reading before the dispatch keeps the baseline at or behind the head the run checks out, so a merge that races the dispatch is never folded into the baseline unvalidated.
   - At hold entry in `mark_validation_failed` (lines 16085-16125), copy that value into `validation_harness_hold.integration_head`. When it is missing, resolve the head once and log `baseline=failure_time`; a merge that landed between dispatch and failure is then part of the baseline, which is accepted for holds without a recorded dispatch head.
3. **Same file, `validation_harness_hold_tick` (line 15730).**
   - Placement: after step 2 sets `vht_pin` from `_VALIDATION_HARNESS_PIN_VALUE` (line ~15825), and before the `support` checks. `_validation_harness_hold_resume` posts the pin in its resume comment, and before that point the pin is still `unknown`.
   - Conditions: the switch is on, `integration_head` is set, the current head (`_branch_head_sha`, one GET) is 40-hex, and it differs from both `integration_head` and the hold's `integration_checked_head`.
   - Ancestry: one `repos/${GITHUB_REPOSITORY}/compare/<integration_head>...<head>` read in the consumer repo, through a new helper `_validation_harness_integration_compare` (collision-check first; same `gh_retry _safe_gh_jq` pattern and `__error__` fail-open result as `_validation_harness_hold_compare`, process-local cache keyed by the two SHAs). Do not reuse `_validation_harness_hold_compare`: it is fixed to `repos/shubhodeep1/coding-workflows/compare/...` and `.../commits/...` (lines 15455, 15466), so a consumer branch compare would fail on every tick. Record the head as `integration_checked_head` only after the compare returns a status (`ahead`, `identical`, `behind` or `diverged`), so each head is compared once when the compare works and is retried on the next tick when it fails. For every definitive status that does not resume (`identical`, `behind`, `diverged`, or `ahead` with the cap reached), write `integration_checked_head` to `STATE_FILE` and call `post_state_comment` in that tick, because `STATE_FILE` is rebuilt from the state comment each tick and an unposted value would be lost. Only `ahead` continues; `behind`/`diverged` (a reset or rebuild) logs `outcome=waiting reason=integration_not_ahead`, and a compare failure logs `outcome=unknown reason=integration_compare_failed`.
   - Budget: if `validation_harness_integration_resumes` has reached `VALIDATION_HARNESS_INTEGRATION_MAX_RESUMES`, log `outcome=waiting reason=integration_resume_capped` and continue with the normal steps.
   - Action: call `_validation_harness_hold_resume` with the resolved pin and reason `integration_advanced`. The caller writes nothing to `STATE_FILE` on this path: no head and no counter. The helper takes its `.vhh_bak` backup at entry (line 15659) and restores it when the dispatch fails (line 15700), so the hold keeps its old `integration_head` and `integration_checked_head`, and the next tick compares the same head again and retries the resume. On success the helper's reset jq removes the hold, heads included. The helper is the only owner of the counter write. Give it an optional fourth argument (`integration`; Phase 3 uses the same argument as `source_resolved`, and whichever phase lands first adds it); with it, the helper (a) reads and increments the top-level `validation_harness_integration_resumes` in its jq instead of `validation_harness_resumes.by_fp`/`.total` (which gate hold entry at lines 16106-16111), (b) runs its post-condition check (line 15667) against that counter, and (c) posts an integration resume comment ("the integration branch advanced to `<head>`") instead of the harness-fix wording at lines 15717-15721. Without the argument, existing callers keep today's counters, check and comment.
   - Log `VALIDATION_HARNESS_HOLD … outcome=resumed reason=integration_advanced`.
   - The helper persists its counter increment with `post_state_comment` before dispatch (line 15690), so a crash cannot reuse a resume. A dispatch that keeps failing is retried on each tick, as the harness-fix resume is today, until the hold time limit. `validation_harness_integration_resumes` is a top-level field next to `validation_harness_resumes` (the hold object is removed on resume) and, like it, is never reset.
4. **Wiring.** Forward the switch and `VALIDATION_HARNESS_INTEGRATION_MAX_RESUMES` in `.github/workflows/orchestrate_poll.yml` like their siblings.
5. **Tests**, extending the existing harness-hold tests: resume on an `ahead` head change with the resolved pin in the comment; no resume on an unchanged head, a missing field or a `behind`/`diverged` head (compared once); a failed compare not recording the head and retried on the next tick; a `behind`/`identical` result persisted with `post_state_comment` and not compared again on the next tick; the compare reading `repos/${GITHUB_REPOSITORY}`; one integration resume raising `validation_harness_integration_resumes` by exactly one and passing the post-condition; the head read before dispatch; the integration cap respected and `validation_harness_resumes` unchanged; a failed dispatch restoring the hold with its old `integration_head`/`integration_checked_head` and the counter unchanged, then resuming on the next tick; the switch off.
6. **Docs.**
   - README rows for the switch and the cap.
   - README harness-hold bullets (README.md ~3876-3907, ending with the `Logs:` bullet at ~3907): add to the "Resume" bullet that a new integration-branch head ahead of the failed run's head also resumes, within `VALIDATION_HARNESS_INTEGRATION_MAX_RESUMES`, and add a `Reasons:` bullet listing `integration_advanced`, `integration_resume_capped`, `integration_not_ahead`, `integration_compare_failed` and (Phase 3) `heal_source_resolved`.
   - `changelog.d/<issue>-harness-hold-integration-resume.md`.
   - `agents.d/<issue>-harness-hold-integration-resume.md`.

## Files & Modules

- `scripts/workflow_failure_heal.py`: phases 1, 2, 3.
- `scripts/workflow_failure_heal_intake.sh`: phases 1, 2, 3 (labels in the `_heal_fetch_issue_list` projection).
- `scripts/workflow_failure_heal_recovery_scan.sh`, `scripts/workflow_failure_heal_queue.sh`: phase 3.
- `scripts/validate_process.sh`: phase 2.
- `scripts/review_untrusted_workspace.py`: phase 2.
- `scripts/orchestrate_poll_process.sh`: phases 1, 3 (resume on a source-resolved close), 4.
- `.github/workflows/workflow-failure-heal-intake.yml`: phases 1, 2 (env), 3 (new job).
- `.github/workflows/orchestrate_poll.yml`: phases 1, 3, 4 (env forwarding).
- `.github/ai/label_contract.v1.json`, `scripts/label_helpers.sh`: phase 3 (`ai:heal-source-resolved`).
- `tests/test_workflow_failure_heal.py`, `tests/test_workflow_failure_heal_scope_tests.py`, `tests/test_workflow_failure_heal_queue.py` and `tests/test_workflow_failure_heal_recovery_scan.py` (phase 3), the `run_heal_plan_scope_gate` and `review_untrusted_workspace.py` test files, the harness-hold poller tests, and `validate_process.sh` summary-line tests: extended.
- `README.md`: switch rows and hold reasons.
- `changelog.d/<issue>-*.md` [new]: one per phase.
- `agents.d/<issue>-*.md` [new]: one per phase.

## Data Model / Index Changes

None (no MongoDB). Additive non-DB state:
- the `validation_harness_hold.fix`, `.integration_head` and `.integration_checked_head` fields, and the top-level `validation_dispatch_integration_head` and `validation_harness_integration_resumes` fields (the latter per project, never reset, like `validation_harness_resumes`), in the orchestrator state;
- the `validation-route-fix:v1`, `main-fix:v1`, `occurrence-source=` and `ai:heal-source-resolved:v1` comment markers, and the `ai:heal-source-resolved` label;
- the `family=` log field.

## Tests

- **Unit** (per phase, above):
  - pure functions `main_fix_commit`, `extract_harness_family`, `heal_scope_harness_subjects`, `heal_source_resolved_decision`;
  - marker shape and verify;
  - the transfer entry check and `.j2` admission.
- **Integration (stub `gh`)**:
  - intake suppression end-to-end with a canned compare;
  - render CLI at a temporary scope commit with the harness directories;
  - scan mode closes and keeps;
  - poller hold tick with a canned compare and head.
- **Regression**: every existing heal, scope, plan-gate, transfer and harness-hold test passes unchanged with the switches on.
- **CI wiring**: new cases go into files CI already runs (`tests/test_ci_wires_every_test_file.py` stays green).
- **E2E (post-merge, observed):**
  - the next consumer harness failure whose fix is on `main` logs `WORKFLOW_HEAL main_fix outcome=suppressed`;
  - the next harness heal's marker contains `workflow-templates/validation-harness/<family>`;
  - the hourly scan logs `WORKFLOW_HEAL source_resolved` lines.

## Risks & Mitigations

- **A commit cites the source but does not fix it.** The heal is suppressed, and the consumer waits for that commit to reach its pin, then re-validates and fails again. The next report finds the `main-fix:v1` marker whose fix is no longer after the pin and files a heal (`reason=prior_fix_did_not_hold`), even if later commits also cite the source. ACCEPTED: costs at most one promotion cycle (≤1 day) per fingerprint, with no human needed.
- **The compare is truncated** (`main` more than 250 commits ahead of the pin). Mitigation: scan the returned commits and log `truncated=true`. With no match, file normally (fail open).
- **A forged `VALIDATION_FAILURE_SUMMARY` line in consumer test output picks the wrong family.** Mitigations: the enum check, the all-lines-must-agree rule, and the fact that the worst case is another harness family's directory (still harness code that goes through review).
- **Directory scope entries let the implementer create arbitrary files under a family directory.** Mitigations: the entries are limited to those six exact directories, the plan gate still requires exact file names in the plan, and review and CI still gate the PR. ACCEPTED: the same trade the step-script glob already makes.
- **Admitting `.j2` exposes template text to the isolated editor.** Templates hold no credentials; admission is limited to one directory. ACCEPTED.
- **Auto-close removes a heal that tracked a defect worth fixing here** (as #8197 was, after #8182). The close comment names the source resolution, and the next occurrence files a new heal. ACCEPTED: the consumer no longer needs it, and hardening remains possible as ordinary work.
- **Closing `not_planned` parks a hold.** The poller's `not_planned)` arm parks whenever the pin has moved ahead of `support`, whatever resolved the source. Mitigations: every recorded source must be resolved (with positive pass evidence); unmarked occurrences make the decision `unknown`; the close adds `ai:heal-source-resolved`, and a poller with Phase 3 resumes instead of parking. ACCEPTED: a consumer poller older than Phase 3 can still park once (CRITICAL) on such a close until it syncs `@stable`.
- **A close races a new occurrence.** The scan holds the heal-intake queue ticket and re-reads comments before writing, which closes the race while the queue works. The queue fails open (no login, unreadable comments, `HEAL_INTAKE_QUEUE_ENABLED=false`, max wait), so the protection is best-effort: an occurrence can then land on a just-closed issue. ACCEPTED: the next report of that fingerprint finds no open issue and files a new heal, and the source-resolved close adds no generation, so nothing escalates.
- **Integration resume wastes validation runs on unrelated merges.** Bounded by its own cap (`VALIDATION_HARNESS_INTEGRATION_MAX_RESUMES`, default 2 per project), one resume per new `ahead` head, and it never spends the harness-fix counters. ACCEPTED.
- **Phase 4 API cost.** One uncached ref GET per held project per tick, plus a compare per new head. Bounded by the number of held projects (usually zero or one per consumer). ACCEPTED.
- **Repeated source-resolved closes of one defect.** Each cycle files and later closes one heal issue. Because a closed issue with `ai:heal-source-resolved` adds no generation, the cycle never reaches `lineage_cap`; each cycle still needs the consumer's own fix, so it cannot loop on its own. ACCEPTED.
- **Phase 1 poller compare.** One `compare/<fix>...<pin>` read per held project with a recorded fix per tick while it waits. Bounded by the number of held projects and by the hold time limit. ACCEPTED.
- **Old consumer pollers see the `fix` line.** They ignore it and resume on pin ancestry alone (today's behaviour). ACCEPTED until they sync `@stable`.

## Rollout

- Each phase is default-on behind its switch:
  - `HEAL_MAIN_FIX_DEDUPE_ENABLED`, `VALIDATION_HARNESS_FIX_SHA_WAIT_ENABLED` (Phase 1);
  - `HEAL_HARNESS_SCOPE_ENABLED` (Phase 2);
  - `HEAL_SOURCE_RESOLVED_CLOSE_ENABLED`, `VALIDATION_HARNESS_SOURCE_RESOLVED_RESUME_ENABLED` (Phase 3);
  - `VALIDATION_HARNESS_INTEGRATION_RESUME_ENABLED` (Phase 4).
  - Not switched (revert-only, see G7): the `.j2` admission and the step-glob transfer fix in `scripts/review_untrusted_workspace.py` (Phase 2).
- Set the repo variable to `false` to roll back without a revert.
- **Coding-workflows-side changes** (intake, scope, editor, scan) take effect on merge to `main`, because the intake and scan jobs run from the default branch.
- **Consumer-side changes** (`validate_process.sh` `family=`, the poller, including Phase 3's resume on `ai:heal-source-resolved`) reach every repo in `.github/ai/consumer_repos.json` on the next `stable` promotion and the `coding-workflows-stable-released` dispatch. Until then they are inert.
- No migration and no backfill. Heal issues filed before Phase 3 have occurrence comments without the new marker, so they stay manual (decision `unknown`).

## References

- Heal #8182; consumer fun-token-multi-chain#579, #609 (root `.dockerignore`), #687 (consumer fix).
- PRs #8181 (shared `Dockerfile.app.dockerignore`) and #8197 (hardhat fail-fast).
- #6463 (frozen heal scope), #8064 (own agents fragment in scope), Q37: A (failing-test subjects).
- `docs/plans/validation-failure-harness-routing-plan.md` (routing marker and harness hold).
- CLAUDE.md §6, §14, §15, §18, §19, §20, §27, §30.

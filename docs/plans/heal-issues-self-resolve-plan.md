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
| Scheduler entry points | **Phase 1:** the existing intake job in `.github/workflows/workflow-failure-heal-intake.yml` (`repository_dispatch` / `workflow_run` / hourly `schedule` / `workflow_dispatch`). **Phase 2:** the same intake job, plus the consumer validate run (`.github/workflows/validate.yml`, which runs `scripts/validate_process.sh`). **Phase 3:** a new job `source-resolved-scan` in `.github/workflows/workflow-failure-heal-intake.yml`, on the existing `cron: "41 * * * *"` schedule (line 50). **Phase 4:** the consumer poller (`workflow-templates/ai-orchestrate-poll.yml`, `*/5` cron, calling `orchestrate_poll.yml@stable`). |
| Long-running supervisor | Not needed (§18.C). Phase 3 is a scheduled scan. Phase 4 runs inside the existing poller tick. |
| DB work / gates | No MongoDB collections (§10 N/A). Idempotency gates are marker comments and poller state fields, listed per phase. |
| `docs/scripts-pending-removal.md` | No entry. No single-use or long-running script or supervisor is introduced (§18.F). |

## Context

- **The heal pipeline.** Heal intake files `ai:workflow-heal` issues here when a consumer pipeline fails. Before the model diagnosis runs, it freezes an edit scope from authenticated run metadata (`<!-- ai:workflow-heal-scope:v1 paths=… runs=… -->`, #6463). It builds that marker with `render_heal_scope_marker` (`scripts/workflow_failure_heal.py:3301`) from the frozen inputs (`scripts/workflow_failure_heal_intake.sh:2278-2306`). The diagnosis can never add a path. Failing-test subjects (Q37: A, 2026-10-09) and `HEAL_SCOPE_GUARD_TEST_SUBJECTS` (`workflow_failure_heal.py:3218-3236`) are the existing ways to widen the scope from run metadata.
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
- **Consumer harness hold.** The consumer poller already parks a `harness_error` project in a harness hold (`scripts/orchestrate_poll_process.sh:15336-15910`) and resumes it from the intake's routing marker `<!-- workflow-failure-heal:validation-route:v1 fp= outcome= issue= classification= run= -->`.
  - **`already-fixed`:** the hold resumes once the consumer's `validate.yml` pin is ahead of the hold's `support` commit (Q5, line 15855).
  - **Heal closed `not_planned`:** for an `opened`/`duplicate` route, a heal closed `not_planned` ends the hold with a CRITICAL park (line 15838).

## Goals

- **G1:** a `validation_failure` heal whose verified run logs report `raw_status=harness_error family=<f>` gets this scope, with every entry derived from run metadata:
  - the family's template directory and `_shared`;
  - `scripts/validation_harness_sandbox.sh`, `scripts/validate_driver.sh`, `scripts/validate_process.sh` and `scripts/render_validation_templates.py`.
- **G2:** the heal implementer can read, edit and create `.j2` files under `workflow-templates/validation-harness/`, and only there.
- **G3:** the intake files no heal issue when `main` already has a commit after the consumer's pin that cites the exact source (`owner/repo#N`). It posts the `already-fixed` routing outcome naming that commit instead.
- **G4:** a consumer harness hold that has a fix commit recorded resumes only once the consumer's pin contains that commit.
- **G5:** an open heal issue with no open heal PR closes itself, as `not_planned` with a comment, once every source it records is resolved. A source is resolved when its issue is closed, or when a validation run for that tracking issue succeeded after the heal was filed.
- **G6:** during a harness hold, a consumer poller re-validates once per new integration-branch head that is past the failed run's head, within the existing resume caps.
- **G7:** each behaviour has its own default-on switch. Switching it off restores today's behaviour exactly.

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
  - Phase 1 adds at most one `compare` call per consumer report. It cannot reuse the existing `<pin>...stable` compare, which has a different head.
  - Phase 3 adds per scan:
    - one open-PR list;
    - one aliased GraphQL read per consumer repo with candidates;
    - one `actions/runs` list per consumer repo with a `validation_failure` candidate.
  - Phase 4 adds no API call: it uses the `_branch_head_sha` value the tick already resolves.
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
- **Check.** For a consumer report (not this repo's own runs) whose target is `stable`, the intake reads `compare/<WRAPPER_SHA>...<default branch>` once, after the existing branch-progress compare. It scans every returned commit's full message for the exact source reference `<SOURCE_REPO>#<n>` as a whole token, with no other digit after it.
- **On a match.** The newest matching commit becomes `MAIN_FIX_SHA`. The intake sets `CLASSIFICATION=already-fixed`, skips the diagnosis, files nothing, posts the existing `already-fixed` outcome comment (naming the commit and that it reaches consumers on the next `stable` promotion), and logs `WORKFLOW_HEAL main_fix outcome=suppressed …`.
- **Validation reports.** The route comment carries an extra line `<!-- workflow-failure-heal:validation-route-fix:v1 fp=<fp> fix=<sha> -->` before the unchanged last-line route marker.
- **Poller side.** The hold stores `fix` and, in step 3, also requires `compare/<fix>...<pin>` to be `ahead` or `identical`. Without the line, the hold behaves exactly as today.

**Phase 2: deterministic harness scope (Q1: A, Q2: A, Q3: A).**
- **Log field.** `validate_process.sh` adds `family=<name>` (one of the five `FAMILY_REGISTRY` names in `scripts/render_validation_templates.py:104-113`, else `none`) right after `raw_status=` in all three `VALIDATION_FAILURE_SUMMARY` lines (lines 58, 1366, 2176).
- **Extraction.** A new `heal-scope harness-family` subcommand reads it from the verified run's filtered job logs. It returns a family only if every `VALIDATION_FAILURE_SUMMARY … raw_status=harness_error family=<f>` line in the logs agrees on one known family.
- **Freezing.** The intake freezes it as `harness_family` in `scope_inputs_frozen.json`.
- **Rendering.** `render_heal_scope_marker` adds `HEAL_SCOPE_HARNESS_SUBJECTS` (the four scripts, existence-checked) and two directory entries, `workflow-templates/validation-harness/<family>` and `workflow-templates/validation-harness/_shared`. These are allowed only in that exact shape, and only when they came from `harness_family`, the same way the step-script glob is allowed only as a guard subject. They take 6 of the 20 explicit slots, placed right after the workflow paths.
- **Downstream checks:**
  - `review_untrusted_workspace.py` admits `.j2` only under `workflow-templates/validation-harness/`.
  - Its transfer scope check accepts the two directory shapes. It also accepts `scripts/<stem>_step_*.sh`, the latent bug Q14: A fixes.
  - The plan gate and the commit guard already match directory entries (`entry_matches`, `files_touched_scope_guard.py:478-535`).

**Phase 3: auto-close heal issues when the source resolves (Q7: A, Q8: A, Q9: A).**
- **Job.** A new `source-resolved-scan` job runs hourly, using a new `--source-resolved` mode of `workflow_failure_heal_recovery_scan.sh`, with decisions in a pure `heal-source-resolved` subcommand of `workflow_failure_heal.py`.
- **Recorded sources.** For each open `ai:workflow-heal` issue authored by the pipeline login, the recorded sources are the `source=` marker plus every occurrence comment's source. Phase 3 also makes `compose_occurrence_comment` write an additive `<!-- workflow-failure-heal:occurrence-source=<repo>#<n> -->` marker. An occurrence comment without that marker makes the issue "unknown" (never closed), so old issues stay manual.
- **Resolved.** A source counts as resolved when:
  - its issue is `CLOSED` (one aliased GraphQL read per consumer repo); or
  - for `validation_failure` heals, an `AI Validate [tracking:<n>]` run of the consumer validate wrapper concluded `success` after the heal issue's `created_at` (one `actions/runs` list per repo, with the same path and event checks as `_verify_validation_report_provenance`).
- **Closing.** When every source is resolved and no open PR has head `ai/issue-<N>` (one open-PR list), the issue gets a comment and is closed `not_planned`.
  - `not_planned` cannot park a hold: a resolved source means its tracking issue is closed or passed validation, both of which clear the hold.
  - A duplicate source that is still unresolved blocks the close.

**Phase 4: consumer re-validates after a merge during a harness hold (Q10: A).**
- **Recording.** `mark_validation_failed` records the integration branch head at failure time as `validation_harness_hold.integration_head`.
- **Trigger.** On each `validation_harness_hold_tick`, before the pin checks, if the head now resolved for `orchestrator/project-<N>` differs from `integration_head`, the tick resumes once through the existing `_validation_harness_hold_resume` path. The same counters and caps apply (`VALIDATION_HARNESS_MAX_RESUMES_PER_FP=2`, `VALIDATION_HARNESS_MAX_RESUMES=4`).
- **Once per head.** It records the new head as `integration_head`. A hold with no `integration_head` (entered before Phase 4) never takes this path.

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
- **Why:** repo commit messages already carry `Refs <source>` (e.g. #8181). A wrong match only delays a heal by one promotion.

### D6 — Hold waits for the fix commit (Q6)
- **Chosen:** a separate `validation-route-fix:v1` marker line. The hold resumes only once the pin contains the fix.
- **Alternatives considered:** keep resuming on any pin movement.
- **Why:** a `stable` hotfix can move the pin without the fix, which wastes a resume. A separate line keeps old pollers' anchored regex working.

### D7 — Auto-close trigger (Q7)
- **Chosen:** close when every recorded source (marker plus occurrence comments) is resolved: its issue is closed, or a validation run for that tracking issue passed after the heal was filed.
- **Alternatives considered:** only on source issue close; only on validation pass.
- **Why:** #579 passed validation long before closing. Requiring every source protects other projects routed onto the same heal.

### D8 — Heal with an open PR (Q8)
- **Chosen:** leave it open.
- **Alternatives considered:** close the issue and its PR.
- **Why:** the PR flow (`workflow_failure_heal_pr_reconcile.sh`, review) owns in-flight work.

### D9 — Close reason (Q9)
- **Chosen:** `not_planned`, with a comment naming the resolution.
- **Alternatives considered:** `completed`.
- **Why:** nothing was fixed by the heal. `completed` would make the closed issue count as a fix in `heal_already_fixed_candidates`, and D7's rule means no hold is still waiting.

### D10 — Consumer re-validate after a merge (Q10)
- **Chosen:** during a harness hold, resume once per new integration-branch head, within the existing resume caps.
- **Alternatives considered:** only for merges touching Docker or build files; leave it out.
- **Why:** this automates the manual `/revalidate` on #579. The caps bound the cost of unrelated merges.

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
  - The intake change works with old pollers: they ignore the line and resume on pin ancestry (today's `already-fixed` rule).
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
- **Rollback:** set `HEAL_HARNESS_SCOPE_ENABLED=false`, which stops new scopes from getting harness entries. Issues already filed keep their frozen marker (by design). Or revert the PR.

**Phase 3: auto-close heal issues when the source resolves.**
- **Scope:** occurrence source marker, the source-resolved decision logic, the scan mode and its job.
- **Files:** `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_recovery_scan.sh`, `.github/workflows/workflow-failure-heal-intake.yml`, tests, README, fragments.
- **Done when:**
  - a fixture heal issue whose single source issue is closed is closed `not_planned` with the comment;
  - one with an open `ai/issue-<N>` PR, an unresolved duplicate source, or an occurrence comment without the new marker stays open;
  - a scan with lookup failures closes nothing.
- **Independence:** reads only existing markers plus its own additive one.
- **Rollback:** set `HEAL_SOURCE_RESOLVED_CLOSE_ENABLED=false` (the job exits after logging `outcome=skip reason=disabled`), or revert the PR.

**Phase 4: consumer re-validate after a merge during a harness hold.**
- **Scope:** record the integration head on hold entry, and resume once per new head.
- **Files:** `scripts/orchestrate_poll_process.sh`, tests, README, fragments.
- **Done when:**
  - a held project whose integration head moved resumes once and records the head;
  - an unchanged head, a missing `integration_head` or exhausted caps do not resume;
  - existing hold tests pass.
- **Independence:** additive state field; old holds are unaffected.
- **Rollback:** set `VALIDATION_HARNESS_INTEGRATION_RESUME_ENABLED=false`, or revert the PR.

## Implementation Steps

New identifiers below were chosen to be unique. Each step re-checks them with `grep -rn` before use (§6), and picks a distinct name if one is taken.

### Phase 1: catch fixes already on `main` at intake

1. **`scripts/workflow_failure_heal.py`: add `main_fix_commit(compare_json, source_ref)`.** It returns `{"sha": <40-hex>, "subject": <≤160 chars>}` or `None`.
   - Input checks: `source_ref` must match `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]{0,9}$` and its repo must be in the consumer registry.
   - Matching: scans `commits[].commit.message` (full text) for the reference bounded by `(?<![A-Za-z0-9_./-])` and `(?![0-9])`. The newest match wins.
   - Truncated results: when `total_commits` exceeds the commits returned, it scans what was returned and adds `"truncated": true`.
   - Expose it as the `main-fix` CLI subcommand (`--compare-json`, `--source-ref`, JSON to stdout, exit 0).
2. **`scripts/workflow_failure_heal_intake.sh`: switch.** Add the switch `HEAL_MAIN_FIX_DEDUPE_ENABLED`, read with the existing switch helper pattern (lines 213-243: unset, empty or `true` is on; `false` is off; anything else is off with `::warning::WORKFLOW_HEAL main_fix outcome=skip reason=invalid_switch`).
3. **Same file: the check itself.**
   - Placement: right after the branch-progress block (after line 2112) and before the scope freeze (line ~2290).
   - Conditions: switch on, source is a consumer repo (`SOURCE_REPO != SELF_REPO`), `WRAPPER_SHA` is 40-hex, `TARGET_BRANCH_DEFAULT` is the stable target, and the report carries a source issue (`validation_failure`, consumer issue phase failures).
   - Action: issue one call, `gh api --method GET "repos/${SELF_REPO}/compare/${WRAPPER_SHA}...${DEFAULT_BRANCH}"`, then run `main-fix`.
   - §15 comment: put an audit comment above the call, explaining that the existing compare's head is `stable`, so it cannot answer for `main`.
   - Fail open: when the call or the parse fails, log `WORKFLOW_HEAL main_fix outcome=skip reason=compare_failed|parse_failed` and continue unchanged.
4. **Same file: the match path.**
   - Set `CLASSIFICATION="already-fixed"`, `MAIN_FIX_SHA`, `MAIN_FIX_SUBJECT`.
   - Skip the diagnosis and route through the existing already-fixed outcome (lines 2993-3000). The comment names the commit (`shubhodeep1/coding-workflows@<sha>`) and says the fix reaches the consumer on the next `stable` promotion and wrapper sync.
   - Log `WORKFLOW_HEAL main_fix outcome=suppressed source=<ref> fix=<sha> truncated=<bool>`.
   - Make sure the already-fixed path does not require a model `## Fixed by` section when `MAIN_FIX_SHA` is set: the deterministic match is the evidence. `check_heal_already_fixed_claim` stays in force for model verdicts.
5. **Same file, `_validation_route_post` (line 1092): the fix line.** When `VALIDATION_ROUTE_OUTCOME=already-fixed` and `MAIN_FIX_SHA` is 40-hex, insert `<!-- workflow-failure-heal:validation-route-fix:v1 fp=<fp> fix=<sha> -->` as the second-to-last line. The route marker stays the last line, unchanged.
6. **`scripts/orchestrate_poll_process.sh`: poller switch.** Add the switch `VALIDATION_HARNESS_FIX_SHA_WAIT_ENABLED` beside the other harness switches (lines 2497-2536), same semantics.
7. **Same file, the routing-marker reader (~line 15320).**
   - Also capture an optional `validation-route-fix:v1` line from the same comment whose `fp` equals the route `fp`.
   - Validate `fix` as `^[0-9a-f]{40}$` and store it as `validation_harness_hold.fix` when the hold records the route.
8. **Same file, `validation_harness_hold_tick` step 3 (lines 15844-15852).**
   - When the switch is on and `fix` is set: after the existing support-vs-pin check passes, compare `fix` with the pin using the memoized compare helper (`_validation_harness_hold_compare`, same cache).
   - If the pin is not `ahead`/`identical` of `fix`, log `VALIDATION_HARNESS_HOLD … outcome=waiting reason=fix_not_in_pin` and return 0.
   - A compare failure logs `outcome=unknown reason=fix_compare_failed` and waits, matching the existing `compare_failed` handling.
9. **Wiring.** Export `HEAL_MAIN_FIX_DEDUPE_ENABLED: ${{ vars.HEAL_MAIN_FIX_DEDUPE_ENABLED || '' }}` in the intake job env of `.github/workflows/workflow-failure-heal-intake.yml`. Forward `VALIDATION_HARNESS_FIX_SHA_WAIT_ENABLED` the same way the other harness switches are forwarded in `.github/workflows/orchestrate_poll.yml`.
10. **Docs.**
    - README switch rows for both switches.
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
   - Freeze it as `harness_family` in the `scope_inputs_frozen.json` jq (lines 2278-2306).
   - Log `WORKFLOW_HEAL harness_scope outcome=family family=<f>|none`.
5. **`scripts/workflow_failure_heal.py`: subjects and directory entries.**
   - Add `HEAL_SCOPE_HARNESS_SUBJECTS = ("scripts/validation_harness_sandbox.sh", "scripts/validate_driver.sh", "scripts/validate_process.sh", "scripts/render_validation_templates.py")`.
   - Add `_HEAL_SCOPE_HARNESS_DIR_RE = re.compile(r"workflow-templates/validation-harness/(?:_shared|node-hardhat-solidity|node-runtime|python-mongo-flask|python-mongo-repo-checks|python-repo-checks)")`.
   - Add `heal_scope_harness_subjects(family, exists, is_dir)`. It returns the four scripts (existence-checked) plus the two directory entries, each only if `is_dir` confirms a tree at the scope commit.
6. **Same file, `render_heal_scope_marker` (3301-3333).**
   - Add a keyword `harness_subjects: Iterable[str] = ()`.
   - Order: crash file, workflow paths, harness subjects, the first 8 test subjects, changed files, remaining subjects.
   - A directory entry passes only if it `fullmatch`es `_HEAL_SCOPE_HARNESS_DIR_RE` and came from `harness_subjects`, mirroring the `step_glob` special case at line 3316. It skips the regular-file `exists` check (it was tree-checked in step 5).
   - In `_cmd_heal_scope` (6497-6530), read `harness_family` from the frozen JSON and pass the subjects. `is_dir` uses `git ls-tree` at `--ref`, alongside `_heal_regular_file_at_ref` (6451).
7. **Same file, `_heal_scope_marker_shape_ok` (3340) and `verify_heal_scope` (3395).**
   - Accept the directory shape only when it `fullmatch`es `_HEAL_SCOPE_HARNESS_DIR_RE`. Every other entry keeps today's rules.
   - `_safe_heal_path` is unchanged; these paths already pass it.
8. **`scripts/review_untrusted_workspace.py`.**
   - In `allowed()` (128-146), additionally allow a `.j2` suffix when the path starts with `workflow-templates/validation-harness/`. `SUFFIXES` itself is unchanged.
   - In the transfer scope entry check (lines 944-948), additionally accept entries matching `_HEAL_SCOPE_HARNESS_DIR_RE`'s pattern and `scripts/[a-z0-9][a-z0-9_]{0,79}_step_\*\.sh` (Q14: A, the latent rejection of the existing step-script glob).
   - Check that `entry_matches` in the transfer (lines 463-469) matches files under a directory entry and new files under it. Add the handling if it does not.
9. **Tests**, extending existing files:
   - `tests/test_workflow_failure_heal_scope_tests.py`:
     - extraction: agreeing lines, disagreeing lines, a forged unknown family, no line, timestamped lines;
     - render with harness subjects at a scope commit: dir entries present, ordering, and the 20-slot budget with a large `changed_files`;
     - shape/verify accept the two dir shapes and reject `workflow-templates/validation-harness/foo` and `workflow-templates/x`;
     - intake wiring: the family is frozen, and the switch off leaves the marker unchanged.
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
   - For run-only sources, write `<repo>#run` as `compose_issue_body` does (5538).
2. **Same file: `heal_source_resolved_decision(issue, comments, source_states, validation_passes, open_heal_branches, pipeline_login)`.** It is pure and returns `{"decision": "close"|"keep"|"unknown", "reason": <token>, "sources": [...]}`.
   - Rules:
     - The issue must be authored by `pipeline_login`.
     - Sources are the `source=` marker plus every pipeline-authored occurrence comment's `occurrence-source=` marker. An occurrence comment without the marker gives `unknown`/`occurrence_unmarked`.
     - Any `#run` source gives `keep`/`run_source`, because it has no issue to resolve.
     - A source is resolved if its state is `CLOSED`, or (for `validation_failure` heals, `classification` marker) a validation pass for that tracking issue is newer than the heal's `created_at`.
     - An open `ai/issue-<N>` PR gives `keep`/`heal_pr_open` (Q8: A).
     - A missing lookup result gives `unknown`/`lookup_failed` (fail open = keep).
   - Expose it as the `heal-source-resolved` CLI subcommand over JSON files.
3. **`scripts/workflow_failure_heal_recovery_scan.sh`: add a `--source-resolved` mode.** It reuses the script's existing open-heal-issue listing and logging. API usage, per scan:
   - **(a)** One `GET repos/<self>/pulls?state=open&per_page=100` (paginated). Keep heads `ai/issue-<N>`.
   - **(b)** Per consumer repo with candidate sources: one aliased GraphQL query, `repository(owner,name){ s<N>: issue(number:N){ state closedAt } … }`, in batches of 25 issues per call. Document the batching contract per §15: input shape, output shape, call count, fail-open.
   - **(c)** Per consumer repo with a `validation_failure` candidate: one `GET repos/<repo>/actions/runs?event=workflow_dispatch&status=success&created=>=<oldest heal created_at>&per_page=100`. Keep runs whose `path` is the consumer validate wrapper (`ai-validate.yml`/`internal-validate.yml`, per `workflow_failure_heal.py:184`) and whose `display_title` contains `[tracking:<n>]`.
   - Issue comments come from one `issues/<N>/comments` read per candidate.
   - On `close`, post the comment, then `PATCH issues/<N> state=closed state_reason=not_planned`. The comment says which sources resolved and how (closed, or validation run `<url>` passed). It ends with `Refs <source>` lines and the "the next occurrence files a new heal" note.
   - Log `WORKFLOW_HEAL source_resolved outcome=closed|keep|unknown|skip issue=<N> reason=<token>`.
   - Cap closes per scan with `HEAL_SOURCE_RESOLVED_CLOSE_MAX_PER_SCAN` (default 10).
4. **`.github/workflows/workflow-failure-heal-intake.yml`: new job `source-resolved-scan`.**
   - On the existing `schedule` (and `workflow_dispatch`).
   - Mirror `recovery-scan`'s checkout, token (`secrets.GH_PAT`) and env pattern (lines 338-400).
   - Gate it on `vars.HEAL_SOURCE_RESOLVED_CLOSE_ENABLED != 'false'`, and pass the switch to the script for the invalid-value warning.
   - Use a concurrency group distinct from intake's.
5. **Tests**, in `tests/test_workflow_failure_heal.py` (decision unit cases for every rule above) and a scan-mode test with a stub `gh` covering:
   - close;
   - keep for an open PR, an unresolved duplicate, an unmarked occurrence and a run-only source;
   - lookup failure closes nothing;
   - the per-scan cap;
   - call count per scan.
   - Also extend the workflow wiring test, and the occurrence marker case.
6. **Docs.** README rows for `HEAL_SOURCE_RESOLVED_CLOSE_ENABLED` and `HEAL_SOURCE_RESOLVED_CLOSE_MAX_PER_SCAN`, `changelog.d/<issue>-heal-source-resolved-close.md`, and `agents.d/<issue>-heal-source-resolved-close.md`.

### Phase 4: consumer re-validate after a merge during a harness hold

1. **`scripts/orchestrate_poll_process.sh`: switch.** Add the switch `VALIDATION_HARNESS_INTEGRATION_RESUME_ENABLED` beside the harness switches (lines 2497-2536).
2. **Same file, hold entry in `mark_validation_failed` (lines 16085-16125).**
   - When the hold is entered, resolve the integration branch head the same way the `/revalidate` block does (`_branch_head_sha`, ~line 26112). Store it as `validation_harness_hold.integration_head` (40-hex, else omitted).
   - Use no new API call when the tick already has the value. Otherwise use the same single helper call the `/revalidate` block uses.
3. **Same file, `validation_harness_hold_tick` (line 15730).**
   - Placement: after the route-marker step and before the pin checks.
   - Conditions: the switch is on, `integration_head` is set, and the current head is 40-hex and differs from it.
   - Action: record the new head in `integration_head`, then call `_validation_harness_hold_resume` with reason `integration_advanced`. Charge the existing per-fingerprint and total resume counters. If the caps are spent, log `outcome=waiting reason=integration_resume_capped` and continue with the normal steps.
   - Log `VALIDATION_HARNESS_HOLD … outcome=resumed reason=integration_advanced`.
   - Persist the head before dispatch, so a failed dispatch cannot loop.
4. **Wiring.** Forward the switch in `.github/workflows/orchestrate_poll.yml` like its siblings.
5. **Tests**, extending the existing harness-hold tests: resume on a head change, no resume on an unchanged head or a missing field, the cap respected, the head persisted once, and the switch off.
6. **Docs.**
   - README row for the switch.
   - README `VALIDATION_HARNESS_HOLD` reason list (README.md ~3904): add `integration_advanced` and `integration_resume_capped`.
   - `changelog.d/<issue>-harness-hold-integration-resume.md`.
   - `agents.d/<issue>-harness-hold-integration-resume.md`.

## Files & Modules

- `scripts/workflow_failure_heal.py`: phases 1, 2, 3.
- `scripts/workflow_failure_heal_intake.sh`: phases 1, 2.
- `scripts/workflow_failure_heal_recovery_scan.sh`: phase 3.
- `scripts/validate_process.sh`: phase 2.
- `scripts/review_untrusted_workspace.py`: phase 2.
- `scripts/orchestrate_poll_process.sh`: phases 1, 4.
- `.github/workflows/workflow-failure-heal-intake.yml`: phases 1, 2 (env), 3 (new job).
- `.github/workflows/orchestrate_poll.yml`: phases 1, 4 (env forwarding).
- `tests/test_workflow_failure_heal.py`, `tests/test_workflow_failure_heal_scope_tests.py`, the `run_heal_plan_scope_gate` and `review_untrusted_workspace.py` test files, the harness-hold poller tests, and `validate_process.sh` summary-line tests: extended.
- `README.md`: switch rows and hold reasons.
- `changelog.d/<issue>-*.md` [new]: one per phase.
- `agents.d/<issue>-*.md` [new]: one per phase.

## Data Model / Index Changes

None (no MongoDB). Additive non-DB state:
- the `validation_harness_hold.fix` and `.integration_head` fields in the orchestrator state;
- the `validation-route-fix:v1` and `occurrence-source=` comment markers;
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

- **A commit cites the source but does not fix it.** The heal is suppressed, and the consumer waits for that commit to reach its pin, then re-validates and fails again. The next report no longer sees the commit after the pin, so a heal is filed. ACCEPTED: costs at most one promotion cycle (≤1 day), with no human needed.
- **The compare is truncated** (`main` more than 250 commits ahead of the pin). Mitigation: scan the returned commits and log `truncated=true`. With no match, file normally (fail open).
- **A forged `VALIDATION_FAILURE_SUMMARY` line in consumer test output picks the wrong family.** Mitigations: the enum check, the all-lines-must-agree rule, and the fact that the worst case is another harness family's directory (still harness code that goes through review).
- **Directory scope entries let the implementer create arbitrary files under a family directory.** Mitigations: the entries are limited to those six exact directories, the plan gate still requires exact file names in the plan, and review and CI still gate the PR. ACCEPTED: the same trade the step-script glob already makes.
- **Admitting `.j2` exposes template text to the isolated editor.** Templates hold no credentials; admission is limited to one directory. ACCEPTED.
- **Auto-close removes a heal that tracked a defect worth fixing here** (as #8197 was, after #8182). The close comment names the source resolution, and the next occurrence files a new heal. ACCEPTED: the consumer no longer needs it, and hardening remains possible as ordinary work.
- **Closing `not_planned` parks another project's hold.** Mitigations: every recorded source must be resolved; unmarked occurrences make the decision `unknown`; and a resolved source means that hold has cleared.
- **Integration resume wastes validation runs on unrelated merges.** Bounded by the existing caps (2 per fingerprint, 4 total), with one resume per new head. ACCEPTED.
- **Old consumer pollers see the `fix` line.** They ignore it and resume on pin ancestry alone (today's behaviour). ACCEPTED until they sync `@stable`.

## Rollout

- Each phase is default-on behind its switch:
  - `HEAL_MAIN_FIX_DEDUPE_ENABLED`, `VALIDATION_HARNESS_FIX_SHA_WAIT_ENABLED` (Phase 1);
  - `HEAL_HARNESS_SCOPE_ENABLED` (Phase 2);
  - `HEAL_SOURCE_RESOLVED_CLOSE_ENABLED` (Phase 3);
  - `VALIDATION_HARNESS_INTEGRATION_RESUME_ENABLED` (Phase 4).
- Set the repo variable to `false` to roll back without a revert.
- **Coding-workflows-side changes** (intake, scope, editor, scan) take effect on merge to `main`, because the intake and scan jobs run from the default branch.
- **Consumer-side changes** (`validate_process.sh` `family=`, the poller) reach every repo in `.github/ai/consumer_repos.json` on the next `stable` promotion and the `coding-workflows-stable-released` dispatch. Until then they are inert.
- No migration and no backfill. Heal issues filed before Phase 3 have occurrence comments without the new marker, so they stay manual (decision `unknown`).

## References

- Heal #8182; consumer fun-token-multi-chain#579, #609 (root `.dockerignore`), #687 (consumer fix).
- PRs #8181 (shared `Dockerfile.app.dockerignore`) and #8197 (hardhat fail-fast).
- #6463 (frozen heal scope), #8064 (own agents fragment in scope), Q37: A (failing-test subjects).
- `docs/plans/validation-failure-harness-routing-plan.md` (routing marker and harness hold).
- CLAUDE.md §6, §14, §15, §18, §19, §20, §27, §30.

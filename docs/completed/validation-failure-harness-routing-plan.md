# Route runtime-validation failures to the repository that owns the fix

## Summary

When runtime validation of an orchestrator project fails in a consumer repository, the pipeline will decide whether the defect is in the shared harness (coding-workflows) or in the project. A harness defect goes to coding-workflows as a heal issue, through the existing `workflow-failure-heal` intake, deduplicated across consumers. The project is held instead of parked, and resumes validation by itself once the consumer's wrapper pin includes the fix. A project defect keeps the existing consumer fix-up path. The unblock judge stops filing consumer fix-ups that the consumer cannot implement.

## Context

### The incidents (2026-10-21 and 2026-10-22)

1. **Credential bug (harness).** `validate.yml` did not stage `scripts/claude_pool_token.sh`, so every Claude diagnosis ended `no_credential` (fixed in #7823, shipped in v1.38.0). Before the fix shipped, three projects in `shubhodeep1/tele-funtoken-msg-scoring` (tracking issues 3928, 4038, 4631) failed with "validation failed after 2 recovery attempts". The unblock judge then filed `operator_step` fix-ups (#4718, #4728) in the consumer for changes to coding-workflows code. The consumer planner could not make those changes, so they stayed `ai:blocked`. A person closed the fix-ups and posted `/revalidate`.
2. **Idle-watchdog bug (harness).** The validation driver printed a test script's output only after the script exited, so a custom-tests step over 15 minutes was killed mid-run (fix: PR #7868). The diagnosis misread it: as `harness_error` for 4631, which raised the CRITICAL "manual repair plus /revalidate required" alert, and as slow consumer tests for 4038 and 3928, which filed consumer fix-ups #4731 and #4732.

### What the code does today

- **The harness path stops the project and calls a person.** `mark_validation_failed` (`scripts/orchestrate_poll_process.sh` ~14873-15049). For `raw_status=harness_error` (~14897-14943) it:
  - sets `status=failed` and the labels `ai:validation-failed` and `ai:harness-broken`;
  - calls `needs_human_park_project "validation_failed"`;
  - posts "Repair the harness, then run `/revalidate`";
  - sends the CRITICAL alert at ~14942.
  Failed projects are added to `UNBLOCK_FAILED_PROJECTS_FILE` (~23371), so the unblock judge runs on them with no harness-aware option.
- **The validation run reports nothing upstream.** `scripts/validate_process.sh` writes `harness_error` from many places:
  - synthesized: bootstrap and precheck (~73-102, ~178, ~365, ~373, ~1362, ~1368), generate and launch (~3908-4111), preflight (~4148), sandbox (~4231), canary (~4575);
  - from the diagnosis (~4794, ~4912, ~4966, ~4974, ~4993).
  None of these dispatches to coding-workflows. The only validation-to-upstream dispatches are `validation-prompt-self-heal` (pass path only, `dispatch_self_heal_improvements` ~1033) and the refused-engine-fallback report (`validate.yml` "Report refused AI engine fallbacks" ~1745).
- **The diagnosis lists no files.** `prompts/mode-validate-diagnose.txt` (and `prompts/_templates/mode-validate-diagnose.txt`) asks for `{status, diagnosis, fix_issues[{id,title,body,priority,depends_on}], harness_fixes}`. Harness-versus-project is a prose decision tree, with no code check of which files a fix touches.
- **The cross-cycle escalation never fires.** `validate_process.sh:209` defaults `VALIDATION_CYCLE` to 1, and neither `validate.yml` nor `workflow-templates/ai-validate.yml` sets it. The cross-cycle escalation (~4745-4783) needs cycle ≥ 3, so it is dead in consumers. That is why the repeated watchdog failures became consumer fix-ups instead of escalating.
- **"Fix shipped" means the consumer's pin moved, not the `stable` tag.**
  - The poller dispatches `ai-validate.yml` with no `--ref` (`dispatch_validation_workflow` ~13582, `dispatch_validation_if_needed` ~14845), so the consumer default branch's wrapper runs.
  - `validate.yml` (~457-491) stages support scripts from the wrapper's `validate.yml@<40-hex>` pin and logs `VALIDATE_SUPPORT_REF ref=<sha>`.
  - The pin moves only when `.github/workflows/update_workflows.yml` syncs, either daily or on the `@stable` `repository_dispatch`.
  - The poller job checks out the consumer with `fetch-depth: 0` (`orchestrate_poll.yml` ~420), so the default branch's wrapper is readable with no API call.
- **The heal intake already has the parts to reuse.** `.github/workflows/workflow-failure-heal-intake.yml` with `scripts/workflow_failure_heal_intake.sh` and `scripts/workflow_failure_heal.py`:
  - checks report identity (`REPORT_IDENTITY_WORKFLOW_BY_KIND`, OIDC);
  - verifies run provenance (`PROVENANCE_BOUND_KINDS`);
  - deduplicates by fingerprint, and applies the lineage cap (`WORKFLOW_HEAL_MAX_LINEAGE_DEPTH` = 3) and the open and per-day budgets;
  - runs its own diagnosis, whose classifications include `consumer-app-defect`, `consumer-config`, `transient` and `already-fixed`;
  - posts an outcome comment on the source issue (`_comment_on_source` ~2240).

  On a duplicate (~1715) it posts nothing back to the source.
- **Heal issues close when their fix merges into `stable`.** Heal issues carry `Target branch: stable`. `.github/workflows/issue_pr_status.yml` (~700-760) treats a merge into the declared target branch as the issue's completion and closes it with `ai:merged`.
- **A deterministic path-ownership source exists.** `scripts/build_sync_manifest.py managed-paths --repo-root R --commit REV` lists the consumer paths the sync overwrites. The security audit already uses it to keep unfixable findings out of the consumer queue (`docs/completed/synced-file-security-findings-heal-intake-plan.md`). The validation support checkout (`VALIDATE_HELPER_STAGE_DIR`) is a full-tree, depth-1 git clone at the pinned SHA, so `managed-paths --commit HEAD` works there.
- **Patterns to copy.**
  - The `heal-report` job of `.github/workflows/implement.yml` (~6961-7060) and `scripts/workflow_failure_heal_phase_report.sh`: a separate job with only `id-token: write`, GH_PAT, and a provenance-bound `phase_failure` report.
  - The one-shot stale-pin recovery in the poller (~24724-24863): state flag, marker comment, counters kept, label rollback on dispatch failure.
- **Permissions are already in place.** `workflow-templates/ai-validate.yml` and `workflow-templates/unblock_judge_dispatch.yml` already grant `id-token: write`, so no wrapper template changes are needed.
- **coding-workflows is public.** Heal issues are public.

### Owner decisions (2026-10-22)

| ID | Decision |
| --- | --- |
| Q1 | A. Decide ownership in three layers:<br>(0) fixed rules for failures the harness itself synthesizes;<br>(1) the diagnosis plus a new `files` field and a deterministic path-ownership check;<br>(2) the heal intake's own diagnosis can bounce a report back. |
| Q2 | A. Mixed failures are treated as project failures. The consumer fix-up covers the project part, and the harness part is reported as a heal issue that does not block the project. |
| Q3 | A. A bounce ends the hold, runs one revalidation with a "harness ruled out" note, and charges one recovery attempt. |
| Q4 | A. The hold lives in the poller, is set when validation fails with a harness error, and uses no unblock-judge round. |
| Q5 | A. Resume when the heal issue is closed and the consumer's pin has moved to a commit that descends from the failing support SHA. When no heal issue is linked, the pin move alone counts. |
| Q6 | A. The poller runs the `/revalidate` reset itself with a marker comment and keeps `validation_recovery_count`. |
| Q7 | A. At most 2 automatic resumes per fingerprint and 4 per project, then today's park and CRITICAL alert. |
| Q8 | A. The hold lasts at most 14 days, then park. |
| Q9 | A. WARNING when the hold starts, DEBUG on resume, CRITICAL only when the cap or the time limit is hit. |
| Q10 | C. The unblock judge's verdict gets a `target_repo: consumer\|harness` field, and a deterministic path check in `scripts/unblock_actions.py` backs it up. |
| Q11 | B. `validate_process.sh` reads the cycle from the tracking issue comments it already fetches. No wrapper or workflow-input change. |
| Q12 | A. Live by default, behind `VALIDATION_HARNESS_HEAL_ENABLED=true`. |
| Q13 | A (assumed). New heal report kind `validation_failure`, bound to `validate.yml`. |
| Q14 | A (assumed). The dedupe key is the failure stage plus reason plus the normalised error signature. It includes no repo or SHA, so all consumers share it. |
| Q15 | A (assumed). The intake posts the routing marker on the tracking issue for duplicates too. |
| Q16 | A (assumed). Fragments go in `changelog.d/` and `agents.d/`, plus README updates. |
| Q17 | A (assumed while drafting). An idle-watchdog kill is passed to the diagnosis as a fact, not forced to harness. After #7868, a remaining idle kill usually means a consumer test hung, which is a project defect. |
| Q18 | A (assumed while drafting). The intake reads the failed validation run's logs, like `phase_failure`, with its existing redaction. Heal issues stay public, the same as for clarify, plan and implement failures today. |
| Q19 | A (assumed while drafting). The judge guard (Phase 3) gets its own switch, `UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED=true`, so it can be rolled back separately from the validation route. |
| Q20 | A (assumed while drafting). Four phases, each mergeable in any order (see Phases & Merge Strategy). |

## Goals

1. A consumer validation run whose failure is classified harness-owned sends exactly one `workflow-failure-heal` dispatch with `source_kind=validation_failure`. The dispatch carries only fixed-vocabulary fields, the normalised signature and the run reference.
2. The intake opens one `ai:workflow-heal` issue per fingerprint across all consumers, or records an occurrence on the open one. In every outcome (opened, duplicate, already-fixed, bounced, skipped) it posts one routing-marker comment on the consumer tracking issue.
3. A validation `needs_fixes` result whose fix files are all harness-owned is not filed as a consumer fix-up. A mixed result files the consumer part and reports the harness part.
4. A harness-owned failure no longer sends the CRITICAL "manual repair plus /revalidate required" alert or parks the project in the needs-human digest. The project is held: one WARNING, and it is excluded from the unblock scan.
5. A held project resumes validation with no person, within one poller tick of these conditions:
   - the consumer default branch's wrapper pin descends from the failing support SHA;
   - the linked heal issue (if any) was closed as completed before the pin commit.
6. Repeated wrong diagnoses cannot loop:
   - each project gets at most 2 resumes per fingerprint and 4 in total;
   - a hold lasts at most 14 days;
   - a bounced fingerprint is never reported again for the same project.
   Each limit ends in today's park and CRITICAL alert.
7. The unblock judge never files a consumer fix-up whose only work is in harness-owned paths.
8. The cross-cycle escalation fires in consumer runs, with the cycle taken from the trusted "Runtime validation dispatched" comment.
9. Each phase can be turned off with one repo variable, without a code change.

## Non-goals

- Changing how the harness itself is fixed. Heal issues go through the normal heal pipeline, with `Target branch: stable` and scope guards.
- A `support_release` hold kind in the unblock judge (Q4 chose a poller-native hold).
- Backfilling or re-opening the projects from the incidents. They were already unblocked by hand.
- Changing the `workflow_run`, `autofix_failure`, `phase_failure`, `engine_fallback` or `synced_security_finding` report kinds.
- Changing release, tagging or wrapper-sync behaviour (`update_workflows.yml`, `mark-stable.yml`).
- Classifying failures in scheduled validation mode (`tests_failed`, `harness_missing`). Scheduled runs have no tracking issue and no diagnosis.

## Constraints

- **§6, naming immutability.**
  - No existing label, state field, log line, marker, env var or workflow output is renamed or removed.
  - `raw_status` values stay as they are. Ownership is a new, separate field (`failure_owner`).
  - The `VALIDATE_SUPPORT_REF` log line is unchanged; the SHA goes in a new line.
  - All new names were checked against the repo and are unused today: `VALIDATION_HARNESS_*`, `VALIDATION_OWNER`, `VALIDATE_SUPPORT_SHA`, `validation_harness_hold`, `validation_harness_resumes`, `UNBLOCK_TARGET_REPO_GUARD`, `validation_failure`, and the marker names below.
- **§4.** Every new env var has a default.
- **§15.** Each phase states its exact API calls (see each phase). There are no per-iteration calls in the poller's inner loops; reads are memoized per tick.
- **§18.**
  - No standalone or manual scripts. All work runs inside the poller (scheduled), the validation run (dispatched by the poller), the heal intake (on `repository_dispatch`) and the unblock judge (dispatched by the poller).
  - No new supervisor.
  - No database work.
  - No `docs/scripts-pending-removal.md` entry, because nothing single-use or long-running is added.
- **§19.** The plan PR and the phase PRs reference issues with `Refs #N` only.
- **§20 / §30.** Each phase ships one `changelog.d/` fragment and one `agents.d/` fragment. `agents.md` and `CHANGELOG.md` are not edited directly.
- **§27.** `validate.yml` is 96,595 bytes and `unblock_judge.yml` is 10,120 bytes, far below 480,000. Check `wc -c` after each edit.
- **§14.** No consumer-registry change. All 13 repos in `.github/ai/consumer_repos.json` receive the change through their pinned `@stable` support SHA on the next sync, and no wrapper template changes.
- **Security.**
  - The consumer-to-coding-workflows channel stays `repository_dispatch` with the existing GH_PAT plus an OIDC report identity.
  - The payload holds no free text except the normalised error signature, which the intake neutralises like other untrusted text.
  - The poller trusts routing markers only from the pipeline login (`VALIDATION_HARNESS_ROUTE_LOGIN`, defaulting to `UNBLOCK_TRUSTED_LOGIN`).

## Approach

### Ownership decision (validation run, Phase 1)

A new subcommand, `workflow_failure_heal.py classify-validation-owner`, decides ownership with no network access. Its inputs:
- the diagnosis JSON;
- the consumer checkout root;
- the support checkout root and commit;
- the run's `raw_status`;
- a fixed-rule reason, when the harness synthesized the failure;
- the bounced fingerprints read from the tracking issue.

It prints `{owner, reason, stage, fingerprint, harness_paths, project_paths}`, where `owner` is one of `harness`, `project`, `mixed` or `none`.

**Layer 0, fixed rules.** These are always harness, with a fixed `reason` and `stage`:

| Where | reason | stage |
| --- | --- | --- |
| sandbox failure (~4222-4231) | `sandbox_unavailable` | `sandbox` |
| preflight on generated files (~4126-4148) | `preflight_failed` | `preflight` |
| canary (~4560-4575) | `canary_failed` | `canary` |
| generate and launch synthesized errors (~3908-4111) | `generate_failed` | `generate` |
| `codex_failure` while this job's `ai-engine-fallback-records.tsv` holds a refused (non-capacity) record | `engine_unavailable` | `engine` |

Bootstrap and precheck failures (~73-102, ~178-373, ~1362-1368) end before the support checkout and diagnosis exist. They keep today's behaviour with owner `none`. An idle-watchdog kill (`VALIDATION_IDLE_KILLED=1`, ~4343) is passed to the diagnosis as a fact, not forced to harness (Q17).

**Layer 1, the diagnosis plus a file check.**
- The diagnose prompt adds two optional fields: `fix_issues[].files` (consumer-relative paths) and `harness_files` (paths the harness fix touches). Older answers without them still parse.
- Each path is classified:
  - **harness** when it is under `validation/`, is in `managed-paths` at the support commit, or is absent from the consumer checkout but present in the support checkout;
  - **project** when it exists in the consumer checkout and is not managed, or is a new path absent from both.
- Outcomes:
  - The diagnosis says `harness_error`: owner `harness`, reason `diagnosis_harness`.
  - The diagnosis says `needs_fixes` and every fix file is harness-owned: owner `harness`, reason `owner_paths`.
  - The diagnosis says `needs_fixes` with a mix: owner `mixed`. The project fix issues go to the consumer fix-up (Q2), and the harness paths go in a non-blocking report with reason `mixed_harness_part`.
  - `needs_fixes` with no `files` field: owner `project`, as today.
  - A cross-cycle escalation (made reachable by Phase 4): reason `escalated_repeat`.
- The classifier never turns a `harness_error` with an empty `fix_issues` into a project failure, because there would be nothing to file.

**Layer 2, the intake re-check.** The intake's diagnosis may bounce the report (Phase 1 intake changes).

**Fingerprint.** `fingerprint("validation-harness", "<stage>:<reason>", <signature>)`, using the existing `fingerprint()` in `workflow_failure_heal.py`. `<signature>` is `workflow_failure_heal.py error-signature` over the failing step's log tail (`VALIDATION_LOG_FILE` or the phase log), capped at 200 characters. It contains no repo name or SHA (Q14), so the same harness bug in any consumer gets the same fingerprint.

**Bounce guard.** When the tracking issue already has a trusted bounce marker for this fingerprint:
- the run does not report again;
- the owner becomes `none`, with reason `bounced_repeat`;
- today's terminal path applies.

This stops a report-and-bounce loop.

### Report and intake (Phase 1)

- **Inside the validation run.**
  - `validate_process.sh` writes the decision to the status and metadata files and to new `collect_status` outputs (`failure_owner`, `harness_report`, `harness_stage`, `harness_reason`, `harness_fp`, all regex-checked against fixed vocabularies).
  - It logs `VALIDATION_OWNER owner=… reason=… stage=… fp=… harness_paths=… project_paths=…`.
  - On the harness path it adds a marker as the last line of the "Runtime validation harness error" tracking comment: `<!-- validation-harness-owner:v1 fp=<64hex> stage=<stage> reason=<reason> support=<40hex> run=<run_id> -->`.
- **The `heal-report` job.** A new job in `validate.yml` (`needs: validate`; runs only when `harness_report == 'true'` and neither `WORKFLOW_HEAL_ENABLED` nor `VALIDATION_HARNESS_HEAL_ENABLED` is `false`).
  - It has only `id-token: write` and uses GH_PAT. It mirrors `implement.yml`'s `heal-report` job, with the same pinned-support-ref resolution and a sparse `scripts` checkout.
  - It runs `scripts/workflow_failure_heal_phase_report.sh` with `WORKFLOW_HEAL_PHASE=validate`, which builds the `validation_failure` payload and sends one dispatch.
- **Intake changes.**
  - Accept `validation_failure`: identity bound to `validate.yml`, provenance-bound like `phase_failure`.
  - Verify the run: it belongs to the source repository, its workflow path is the consumer's validation wrapper, its conclusion is failure, and its display title carries `[tracking:<issue_number>]`.
  - Compute the fingerprint from the validated fields, read the run logs and diagnose as usual.
  - Map the classification to an outcome:

| Intake classification | Outcome |
| --- | --- |
| `workflow-defect`, `inconclusive` | open a heal issue (target `stable`); outcome `opened` |
| an existing heal issue with the same fingerprint | outcome `duplicate`, occurrence recorded |
| `already-fixed` | no issue; outcome `already-fixed` |
| `consumer-app-defect`, `consumer-config`, `transient`, `pr-self-inflicted`, `base-self-inflicted` | no issue in either repo; outcome `bounced` |
| a budget or switch skip | outcome `skipped` |

  - Every outcome ends in one comment on the tracking issue. Its last line is: `<!-- workflow-failure-heal:validation-route:v1 fp=<64hex> outcome=<opened|duplicate|already-fixed|bounced|skipped> issue=<n|none> classification=<token> run=<run_id> -->`.

### Hold and resume (Phase 2, poller)

- **Entering the hold.** In the harness branch of `mark_validation_failed`, when `VALIDATION_HARNESS_HEAL_ENABLED` is not `false` and the resume caps are not reached:
  - Record `validation_harness_hold = {fp, support, heal_issue, outcome, since, reason}` in state.
    - `fp`: from the newest trusted `validation-harness-owner:v1` marker for `validation_last_run_id`, else `local:<16hex>` (sha256 of the normalised failure reason).
    - `support`: from the same marker, else the pin parsed from `git show origin/<default>:.github/workflows/<VALIDATE_WORKFLOW_NAME>`.
  - Keep `status=failed`, `ai:validation-failed` and `ai:harness-broken`.
  - Post "## ⏸ Runtime validation waiting for a harness fix" with `<!-- validation-harness-hold:v1 fp=… support=… -->`.
  - Send one WARNING, and skip `needs_human_park_project` and the CRITICAL alert.
  - Projects with an active hold are not added to `UNBLOCK_FAILED_PROJECTS_FILE`.
- **Each tick, for a held project** (next to the stale-pin block):
  1. **Routing marker.** Read the newest trusted `validation-route:v1` marker for the hold's fingerprint from `COMMENTS` (already fetched, no API call) and store `heal_issue` and `outcome`. On `bounced` (Q3):
     - end the hold;
     - post `<!-- validation-harness-bounce:v1 fp=… -->` with the intake's verdict;
     - charge one recovery attempt with the existing increment rules (including exhaustion, then today's park);
     - otherwise revalidate.
  2. **Pin.** Read the current pin locally (no API call). If it equals `support` or cannot be read, keep waiting.
  3. **Ancestry.** Once the pin differs, make one `GET repos/shubhodeep1/coding-workflows/compare/<support>...<pin>`. Only `ahead` counts. `behind` or `diverged` keeps waiting and is logged.
  4. **Heal issue state.** With `outcome` `opened` or `duplicate`, make one `GET repos/shubhodeep1/coding-workflows/issues/<heal_issue>`.
     - Met: `state=closed`, `state_reason=completed`, and `closed_at` ≤ the pin commit's committer date (the last commit in the compare response).
     - `not_planned` ends the hold with today's park and CRITICAL alert.
     - Open: keep waiting.
     - With `already-fixed`, `skipped` or no marker, the ancestry check alone is enough (Q5).
  5. **Resume (Q6).**
     - Apply the `/revalidate` state reset but keep `validation_recovery_count`.
     - Increment `validation_harness_resumes.by_fp[fp]` and `.total`.
     - Remove `ai:validation-failed` and `ai:harness-broken`, and set `ai:validating`.
     - Post `<!-- validation-harness-resume:v1 fp=… pin=… -->`, send DEBUG, and call `dispatch_validation_if_needed 1`.
     - If the dispatch fails, restore the failure labels and the hold (as `_vsp_restore_failure_labels` does).
  6. **Time limit.** At `VALIDATION_HARNESS_HOLD_MAX_DAYS` (14) the hold ends with today's park and CRITICAL alert.
- **Caps (Q7).** Once `by_fp[fp]` reaches `VALIDATION_HARNESS_MAX_RESUMES_PER_FP` (2) or `total` reaches `VALIDATION_HARNESS_MAX_RESUMES` (4), a new harness failure takes today's path.
- **`/revalidate` still works.** It clears the hold and keeps today's full reset.
- **Judge hold requests.** The poller also honours a trusted `<!-- ai:unblock-harness-hold-request:v1 item=<N> -->` on a failed project's tracking issue. Phase 3 emits it. The poller enters the hold with `fp=judge:<12hex>` (copied from the marker's `fingerprint=` field) and `support` set to the local pin.

### Unblock judge (Phase 3)

- **Prompt.** `prompts/mode-judge-unblock.txt` adds `target_repo` (`consumer` | `harness`). It is required for `retry_budget`, `descope`, `reissue`, `accept_with_followup` and `operator_step`. `harness` means the change belongs in `shubhodeep1/coding-workflows`: synced wrappers, `.claude/`, `CLAUDE.md`, `validation/`, scripts staged from coding-workflows, or a path that exists only there.
- **Ledger.** `scripts/unblock_ledger.py` validates the enum. A missing field means `consumer`.
- **Backstop.** `scripts/unblock_actions.py` pulls path-like tokens from `instructions` and classifies them with the same ownership function (imported from `workflow_failure_heal.py`, using `SUPPORT_DIR` and `TARGET_DIR`). The verdict is treated as harness when either:
  - `target_repo=harness`; or
  - the backstop finds harness-owned paths and no project-owned paths.
- **Harness verdict actions.** No fix-up is filed. Instead the actions are:
  - a verdict comment;
  - a `heal_report` operation: an `issue`-kind report through the existing `build-issue-payload` with the item's block label, `wrap-dispatch`, and `request-report-identity`, with `unblock_judge.yml` added to the `issue` kind's identity list;
  - for a project item, the hold-request marker above;
  - a DEBUG Telegram message.
- **Mixed paths with `target_repo=consumer`.** The fix-up is kept and a `heal_report` operation is added.
- **Switch.** `UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED=true`; `false` restores today's behaviour.

### Cross-cycle escalation fix (Phase 4)

When `VALIDATION_CYCLE` is not set, `validate_process.sh` takes it from the newest "## 🧪 Runtime validation dispatched" comment by the pipeline login, in the tracking comments it already fetches (`PRIOR_COMMENTS`). It parses the `- Cycle: <N>` line and falls back to 1. An explicit `VALIDATION_CYCLE` still wins. `/revalidate` and the recovery reset already post "Cycle: 1", so the derived value stays consistent.

### Alternatives considered

- **Unblock-judge `support_release` hold** (rejected, Q4). It would cost a judge run and a ≥30-minute delay, and leave the decision to a model.
- **Watching the `stable` tag** (rejected, Q5). The validation run uses the consumer's pin, so `stable` moving is not enough.
- **Passing the cycle as a workflow input** (rejected, Q11). A consumer whose wrapper has not synced yet rejects the unknown input, so its dispatches would fail.

## Decisions

### D1 — Who decides ownership
- **Chosen:** Three layers: fixed rules for harness-synthesized failures, then the diagnosis plus a deterministic path-ownership check, then the heal intake's re-check (Q1).
- **Alternatives considered:** The diagnosis `status` alone; fixed rules only.
- **Why:** The incidents showed the model alone misclassifies in both directions. The path check is free and auditable, and the intake re-check keeps a wrong harness guess cheap.

### D2 — Mixed failures
- **Chosen:** Treat them as project failures, file the consumer part, and report the harness part without blocking the project (Q2).
- **Alternatives considered:** Treat the whole failure as harness; treat it as project with hints only.
- **Why:** The consumer part can be fixed now. Holding the whole project on a partial harness cause would stall work the pipeline can do.

### D3 — Where the hold lives
- **Chosen:** In the poller, set when validation fails with a harness error, using no judge round (Q4).
- **Alternatives considered:** An unblock-judge `support_release` wait hold; both.
- **Why:** The condition is deterministic, and the poller already owns `/revalidate` and the stale-pin recovery. A judge hold adds a model call and at least 30 minutes of delay.

### D4 — What "fix shipped" means
- **Chosen:** The consumer default branch's wrapper pin descends from the failing support SHA, and the linked heal issue was closed as completed before the pin commit. When no heal issue is linked, the pin move alone counts (Q5).
- **Alternatives considered:** The pin move alone; the `stable` tag moving.
- **Why:** Validation runs the pinned SHA, not `stable`. The heal-issue check avoids resuming on an unrelated release.

### D5 — Bounded retries
- **Chosen:** 2 resumes per fingerprint, 4 per project, a 14-day hold limit, and a bounced fingerprint never re-reported. Each limit ends in today's park and CRITICAL alert (Q7, Q8, Q3).
- **Alternatives considered:** Rely on the intake lineage cap only; no time limit.
- **Why:** Wrong diagnoses must end in a person after a small, known cost.

### D6 — Judge guard
- **Chosen:** A `target_repo` verdict field, backed by a deterministic path check, routes harness work to a heal report instead of a consumer fix-up (Q10).
- **Alternatives considered:** The path check only; a prompt rule only.
- **Why:** The field records the model's intent, and the check catches a model that forgets it.

## Phases & Merge Strategy

Each phase is one PR, mergeable in any order, and leaves the system working:

1. **Phase 1, owner decision, harness report and intake kind.**
   - Scope: classify ownership in the validation run, report harness failures, and let the intake open or deduplicate heal issues and post the routing marker.
   - Without Phase 2, the poller still parks harness failures with CRITICAL as today, and a heal issue is now also filed.
   - Done when: a fixture `harness_error` run produces one dispatch and the intake posts a routing marker in tests.
   - Rollback: `VALIDATION_HARNESS_HEAL_ENABLED=false` stops the report and the owner reclassification (the run behaves exactly as today); a full revert of the PR also works.
2. **Phase 2, poller hold and resume.**
   - Scope: hold harness-failed projects, resume them automatically, enforce the caps and time limit, exclude them from the unblock scan, and honour judge hold requests.
   - Without Phase 1 there are no routing markers, so the fingerprint is local and resume uses the pin move alone (Q5's fallback).
   - Done when: poller tests cover hold, wait, resume, bounce, cap and time limit.
   - Rollback: `VALIDATION_HARNESS_HEAL_ENABLED=false` (the poller's check of the same variable) restores the CRITICAL park; a full revert also works. Projects held when the switch is turned off stay `failed` with `ai:validation-failed`, so `/revalidate` still works on them.
3. **Phase 3, unblock judge `target_repo` and guard.**
   - Scope: no consumer fix-ups for harness-only work; heal report and hold request instead.
   - Without Phase 2 the hold request is ignored. The project stays failed and is re-judged after `UNBLOCK_JUDGE_RETRY_HOURS`, which is still better than today because no unimplementable fix-up is filed.
   - Done when: judge tests cover the harness verdict, the mixed verdict and a missing field.
   - Rollback: `UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED=false`.
4. **Phase 4, cycle derivation.**
   - Scope: make the cross-cycle escalation reachable in consumers.
   - Without Phase 1 an escalation takes today's terminal harness path; with Phase 1 it routes to the heal intake.
   - Done when: tests cover the derived cycle and escalation at cycle 3.
   - Rollback: revert the PR. There is no switch, because this fixes a bug in an existing mechanism.

No phase reads a field, marker or output that only another phase writes without a fallback: each reader treats a missing marker or output as absent.

## Implementation Steps

### Phase 1

1. `prompts/mode-validate-diagnose.txt` and `prompts/_templates/mode-validate-diagnose.txt`: add the optional `fix_issues[].files` and `harness_files` arrays to the schema and the status rules. Keep the existing fields and wording. Add the two optional fields to `prompts/contracts/mode-validate-diagnose.yml` too.
2. `scripts/workflow_failure_heal.py`:
   - Add `"validation_failure"` to `SOURCE_KINDS`, `REPORT_IDENTITY_WORKFLOW_BY_KIND["validation_failure"] = ("validate.yml",)` and `PROVENANCE_BOUND_KINDS`.
   - Add the fixed vocabularies `VALIDATION_STAGES` (`generate`, `preflight`, `sandbox`, `canary`, `run`, `diagnose`, `engine`) and `VALIDATION_REASONS` (`sandbox_unavailable`, `preflight_failed`, `canary_failed`, `generate_failed`, `engine_unavailable`, `diagnosis_harness`, `owner_paths`, `mixed_harness_part`, `escalated_repeat`).
   - Add `validation_failure_fingerprint()`, `classify_validation_owner()` and `build_validation_failure_payload()`. Payload fields: `schema_version`, `source_repo`, `source_kind`, `issue_number`, `run_refs` (the failed run only), `wrapper_sha`, `validation_stage`, `validation_reason`, `validation_signature`, `failure_reason="validation_harness"`, `failure_streak=1`, `workflow_name`, `reporter_run_url`, `reported_at`.
   - Add the subcommands `classify-validation-owner`, `build-validation-payload` and `validation-failure-fingerprint`.
   - Extend `validate-payload` with the enums, the 200-character cap on the signature and neutralisation.
   - Add a `compose_issue_title` branch: `Workflow heal: runtime validation harness failure (<stage>/<reason>)`. Add `_context_lines` entries and a `verify-phase-provenance` branch for this kind (the run's repository, workflow path, failure conclusion and `[tracking:<N>]` display title).
3. `scripts/workflow_failure_heal_intake.sh`:
   - Add the `validation_failure` fingerprint branch next to the `engine_fallback` one (~1331).
   - Run provenance verification as for `phase_failure`.
   - Map classifications to outcomes as in the Approach table. Never open an issue in the source repository for this kind.
   - Write the routing marker as the last line of every source comment for this kind, including the duplicate path (~1715) and the skip paths.
   - Log `received kind=validation_failure outcome=… fp=…`.
4. `scripts/workflow_failure_heal_phase_report.sh`: accept `WORKFLOW_HEAL_PHASE=validate`.
   - Read `WORKFLOW_HEAL_VALIDATION_STAGE`, `_REASON`, `_SIGNATURE_FILE` and `_FP`.
   - Skip the comment-streak reads.
   - Build the payload with `build-validation-payload` and dispatch once.
   - API calls: one identity read and one dispatch.
5. `scripts/validate_process.sh`:
   - Set `VALIDATION_HARNESS_REASON` and `VALIDATION_HARNESS_STAGE` at each Layer 0 site.
   - After the diagnosis and before the `case "${DIAG_STATUS}"` (~4790), call `classify-validation-owner`. Apply its result:
     - `harness`: set `DIAG_STATUS=harness_error`;
     - `mixed`: keep only the project-owned fix issues for the fix-up and record the harness part;
     - otherwise leave `DIAG_STATUS` unchanged.
   - Read bounce markers from `PRIOR_COMMENTS`, trusted login only.
   - Add the "harness ruled out" block to the diagnose prompt context when this fingerprint was bounced.
   - Write the ownership fields to the status and metadata files.
   - Append the `validation-harness-owner:v1` marker to the harness comment (~4947-4976).
   - Write the signature file under `RUNTIME_DIR`.
   - Log `VALIDATION_OWNER`.
6. `.github/workflows/validate.yml`:
   - In `support_staging` (~491), add the line `echo "VALIDATE_SUPPORT_SHA sha=${support_sha}"` and export `VALIDATE_SUPPORT_SHA` to `GITHUB_ENV`.
   - Add `scripts/workflow_failure_heal.py` and `scripts/build_sync_manifest.py` to the staging manifest's optional preserve list.
   - Export the new `collect_status` outputs, then the job outputs, then the `workflow_call` outputs (additive only).
   - Pass `VALIDATION_HARNESS_HEAL_ENABLED: ${{ vars.VALIDATION_HARNESS_HEAL_ENABLED || 'true' }}` to the validate step env.
   - Add the `heal-report` job: mirror `implement.yml` ~6961-7060, with `permissions: id-token: write` and `timeout-minutes: 10`.
7. Docs:
   - README "Runtime Validation Phase" and "Workflow Failure Heal": the ownership rule, the new kind and the routing marker.
   - `agents.d/validation-harness-routing-p1.md`: the log prefixes `VALIDATION_OWNER` and `VALIDATE_SUPPORT_SHA` under "Stable log prefixes (contractual)", and the two markers under "Orchestrator tracking-issue comment markers".
   - `changelog.d/validation-harness-routing-p1.md` (`added`).

### Phase 2

1. `scripts/orchestrate_poll_process.sh` config block (near ~2450): parse `VALIDATION_HARNESS_HEAL_ENABLED` (true), `VALIDATION_HARNESS_HOLD_MAX_DAYS` (14), `VALIDATION_HARNESS_MAX_RESUMES_PER_FP` (2), `VALIDATION_HARNESS_MAX_RESUMES` (4) and `VALIDATION_HARNESS_ROUTE_LOGIN` (empty, meaning `UNBLOCK_TRUSTED_LOGIN`). Validate each and fall back to its default with a warning.
2. Add the helpers `validation_harness_pin` (local `git show`, memoized per tick), `validation_harness_marker` (jq over `COMMENTS`, trusted login), `validation_harness_compare` and `validation_harness_issue_state` (memoized per tick by key).
3. `mark_validation_failed` harness branch (~14897-14943): enter the hold when enabled and under the caps; otherwise keep today's code exactly.
4. The failed-project branch, next to the stale-pin block (~24724): the per-tick hold check, bounce, resume, time limit and judge hold-request handling. Log `VALIDATION_HARNESS_HOLD tracking_issue=… outcome=<held|waiting|resumed|bounced|expired|capped|not_planned|unknown> fp=… pin=… heal_issue=…`.
5. `/revalidate` block (~24639-24661): also delete `validation_harness_hold` (`validation_harness_resumes` is kept).
6. `UNBLOCK_FAILED_PROJECTS_FILE` append (~23371): skip held projects and log `reason=harness_hold`.
7. `update_completion_status_comment`: add the line "Waiting for harness fix `<heal issue link or pin>`" when held.
8. `.github/workflows/orchestrate_poll.yml`: pass the five variables as `vars.* || default` in the poll job env.
9. Docs:
   - README "Manual Reset: `/revalidate`" and a new "Harness hold" subsection under "Runtime Validation Phase".
   - `agents.d/validation-harness-routing-p2.md`: the `VALIDATION_HARNESS_HOLD` prefix and the hold, resume and bounce markers.
   - `changelog.d/validation-harness-routing-p2.md` (`changed`).

### Phase 3

1. `prompts/mode-judge-unblock.txt`: the `target_repo` field and its rule. In the output-contract schema, add `"target_repo": "consumer" | "harness"`.
2. `scripts/unblock_ledger.py`: validate the `target_repo` enum and default it to `consumer`.
3. `scripts/unblock_actions.py`: `_harness_route()` (the guard switch, the field, the backstop); `heal_report` and `hold_request` operations in `plan()`; the mixed case.
4. `scripts/unblock_judge.sh`: carry out `heal_report` (build, wrap, identity, one dispatch) and `hold_request` (one comment on the tracking issue). Log `UNBLOCK_TARGET_REPO_GUARD item=… outcome=<consumer|harness|mixed|disabled> paths=…`.
5. `scripts/workflow_failure_heal.py`: add `"unblock_judge.yml"` to `REPORT_IDENTITY_WORKFLOW_BY_KIND["issue"]`.
6. `.github/workflows/unblock_judge.yml`: pass `UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED: ${{ vars.UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED || 'true' }}` and `WORKFLOW_HEAL_ENABLED`.
7. Docs:
   - README "Unblock judge".
   - `agents.d/validation-harness-routing-p3.md`: the prefix and the hold-request marker.
   - `changelog.d/validation-harness-routing-p3.md` (`fixed`).

### Phase 4

1. `scripts/validate_process.sh` ~209: when `VALIDATION_CYCLE` is unset, derive it from `PRIOR_COMMENTS`. Reuse the existing pipeline-login lookup if the script has one; otherwise make one `gh api user` call, and only when deriving. Log `VALIDATION_CYCLE_SOURCE source=<env|dispatch_comment|default> cycle=N`.
2. Docs: `agents.d/validation-harness-routing-p4.md` (the prefix) and `changelog.d/validation-harness-routing-p4.md` (`fixed`).

## Files & Modules

- `prompts/mode-validate-diagnose.txt`, `prompts/_templates/mode-validate-diagnose.txt`, `prompts/contracts/mode-validate-diagnose.yml` (P1)
- `scripts/workflow_failure_heal.py` (P1, P3)
- `scripts/workflow_failure_heal_intake.sh` (P1)
- `scripts/workflow_failure_heal_phase_report.sh` (P1)
- `scripts/validate_process.sh` (P1, P4)
- `.github/workflows/validate.yml` (P1)
- `scripts/orchestrate_poll_process.sh`, `.github/workflows/orchestrate_poll.yml` (P2)
- `prompts/mode-judge-unblock.txt`, `scripts/unblock_ledger.py`, `scripts/unblock_actions.py`, `scripts/unblock_judge.sh`, `.github/workflows/unblock_judge.yml` (P3)
- `README.md` (P1-P3)
- `[new]` `agents.d/validation-harness-routing-p1.md` … `-p4.md`
- `[new]` `changelog.d/validation-harness-routing-p1.md` … `-p4.md`
- Tests below

## Data Model / Index Changes

None. No MongoDB collection is touched. New poller state fields (`validation_harness_hold`, `validation_harness_resumes`) live in the orchestrator state comment and are additive. Readers treat them as absent when missing.

## Tests

**Unit (Python, stdlib).** In `tests/test_workflow_failure_heal.py`:
- `classify-validation-owner` covers:
  - each Layer 0 reason;
  - a `harness_error` diagnosis;
  - `needs_fixes` with all-harness, all-project and mixed files;
  - missing `files`;
  - managed paths at the commit;
  - `validation/` paths;
  - a path only in the support checkout;
  - new paths;
  - a bounced fingerprint.
- Fingerprint stability across repos and SHAs.
- Payload validation: enums, signature cap, unknown fields rejected.
- Identity binding to `validate.yml`.
- The provenance branch.
- The title and context lines.

**Intake (bash with the existing fixtures).** The outcome mapping for each classification. A routing marker on opened, duplicate, already-fixed, bounced and skipped. No source-repo issue for this kind.

**Validation run.**
- New `tests/test_validate_process_owner_routing.py`, following the patterns of `tests/test_validate_process_cross_cycle_escalation.py`: harness reclassification, the mixed split, the bounce guard and the owner marker.
- `collect_status` outputs.
- Extend the `validate.yml` workflow contract test: the `heal-report` job's `needs`/`if`/permissions, the outputs, the staged files, the 480,000-byte guard.

**Poller.** `tests/test_orchestrate_poll_process.py`:
- hold entry with and without the owner marker;
- waiting on an unchanged pin with zero API calls (asserted through a gh stub call counter);
- resume on an `ahead` pin with the heal issue closed as completed;
- waiting while the heal issue is open or `closed_at` is after the pin commit;
- `not_planned`;
- `behind` and `diverged` pins;
- bounce, which charges recovery;
- the per-fingerprint and total caps;
- the 14-day time limit;
- `/revalidate` clearing the hold;
- the unblock-scan exclusion;
- the judge hold request;
- dispatch-failure rollback;
- the switch off, which leaves today's behaviour byte-for-byte.

Also extend `tests/test_orchestrate_poll_workflow_contract.py` for the env wiring.

**Judge.** `tests/test_unblock_judge.py`:
- the `target_repo` enum and its default;
- the harness verdict, which files no fix-up and emits a heal report plus a hold request;
- the backstop path detection;
- the mixed verdict;
- the switch off.

**Cycle.** `tests/test_validate_process_cross_cycle_escalation.py`: a cycle derived from the dispatch comment, an explicit env value winning, untrusted authors ignored, and escalation firing at cycle 3.

**End-to-end.** After Phase 1 ships, the nightly `Nightly Validation Self-Test` and one real consumer harness failure are the end-to-end checks. Confirm the `VALIDATION_OWNER` and `received kind=validation_failure` log lines and the routing marker on the tracking issue.

## Risks & Mitigations

- **A wrong harness classification holds a project that needs a consumer fix.**
  - Mitigation: the intake re-check bounces it (Q3), the bounce guard stops a repeat, and the caps and the 14-day limit end in today's park.
- **A wrong project classification loops consumer fix-ups (#4731 and #4732).**
  - Mitigation: the ownership file check catches fixes that need harness paths, and the reachable escalation (Phase 4) promotes repeats.
  - `MAX_VALIDATE_CYCLES` still caps them.
- **Pin moved, but the release did not contain the fix.**
  - Mitigation: the `closed_at` ≤ pin-commit-date check. A residual miss costs one revalidation and counts toward the per-fingerprint cap.
  - ACCEPTED: the cap of 2 bounds it.
- **The intake login differs from the consumer pipeline login, so routing markers are ignored.**
  - Mitigation: `VALIDATION_HARNESS_ROUTE_LOGIN`. Without a trusted marker the poller uses the pin-only resume.
- **Public heal issues contain consumer log excerpts (Q18).**
  - ACCEPTED: same exposure and redaction as `phase_failure` today.
- **The diagnosis model omits `files`.**
  - Mitigation: a missing field means project, as today. Layer 0 and the intake still catch harness bugs.
- **The heal intake budgets are hit (10 open, 20 per day).**
  - Mitigation: outcome `skipped` still posts the marker, and the hold resumes on the pin alone.
- **Older consumer support SHAs lack the new outputs or markers during rollout.**
  - Mitigation: every reader treats them as absent. The poller's fallback is the local fingerprint plus a pin-only resume.

## Rollout

- Phases merge to `main` in any order and reach `stable` with the next release. Consumers receive them through the `@stable` sync, which updates the pinned support SHA. No wrapper template changes, no new secrets, no new permissions.
- Switches:

| Variable | Default | Effect of `false` |
| --- | --- | --- |
| `VALIDATION_HARNESS_HEAL_ENABLED` | `true` | turns off the report, the owner reclassification and the hold (Phases 1-2) |
| `UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED` | `true` | turns off the judge guard (Phase 3) |
| `WORKFLOW_HEAL_ENABLED` | `true` (existing) | still turns off all heal reports |

- **Tunables:** `VALIDATION_HARNESS_HOLD_MAX_DAYS` = 14, `VALIDATION_HARNESS_MAX_RESUMES_PER_FP` = 2, `VALIDATION_HARNESS_MAX_RESUMES` = 4, `VALIDATION_HARNESS_ROUTE_LOGIN` = empty.
- **Rollback:** set the switch to `false` in the affected repo (or in every consumer), or revert the phase PR. Held projects stay `failed` with `ai:validation-failed`, so `/revalidate` works on them.

## References

- Incidents: `shubhodeep1/tele-funtoken-msg-scoring` tracking issues 3928, 4038, 4631; fix-ups #4718, #4728, #4731, #4732.
- Fixes: #7823 (credential staging, v1.38.0), PR #7868 (idle-watchdog streaming).
- Related plans: `docs/completed/synced-file-security-findings-heal-intake-plan.md` (managed-paths routing to the heal intake).
- Code: `scripts/validate_process.sh`, `scripts/orchestrate_poll_process.sh` (`mark_validation_failed`, `/revalidate`, stale-pin recovery), `scripts/workflow_failure_heal*.{py,sh}`, `.github/workflows/workflow-failure-heal-intake.yml`, `scripts/unblock_*.{py,sh}`, `scripts/build_sync_manifest.py`, `.github/workflows/issue_pr_status.yml`.
- README sections: "Runtime Validation Phase", "Manual Reset: `/revalidate`", "Unblock judge", "Workflow Failure Heal", "Validation self-healing".

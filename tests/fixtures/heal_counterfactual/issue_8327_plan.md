Implementation Plan

## Plan: Phase 7 must report a cancel-on-close run that never finished as `timeout`, not as SKIPPED/PASSED

Planning ref: `main` @ `977f58c608ca90c19f29b7c5617d5ba538d02aee` (integration branch mismatch: false).

Clarification mapping: Q1 → **A**, decided by the orchestrator as AD-1. This issue fixes only the reporting bug, inside the frozen heal scope: `.github/workflows/test-and-mark-stable.yml`, `tests/**` and `changelog.d/*.md`. The fix for the shared concurrency group goes under Deferred scope.

Scope-mode: Hold Scope
Reuse-audit: extends verify-cancel-on-close
Scope-mode justification: The scope is the one AD-1 approved for this heal issue. It covers the certain reporting bug, which is in scope. The root-cause concurrency change is in `cancel_on_pr_close.yml`, which the heal-scope marker excludes, so it is deferred and not dropped. Under the "Boil the Lake" rule this plan takes the complete in-scope option. It adds two pieces:
- the post-loop timeout branch, about 8 lines that copy the existing already-closed path at lines 3125-3130;
- the summary fix that fails closed, about 6 lines.

It also adds an executable regression test, not only string pins. Per §6 (Security > Correctness & safety > Backward compatibility > Operational clarity > Performance > Speed), correctness wins over a smaller diff here: today the gate prints "ALL PHASES PASSED" for a phase that never verified anything. No new dependency or external interface is added.

## Files likely to change
- `.github/workflows/test-and-mark-stable.yml`
- `tests/test_test_and_mark_stable_phase7_timeout_status.py` (new)
- `changelog.d/8327-phase7-cancel-timeout-status.md` (new)

## Deferred scope
- Give the cancel-on-close job a per-trigger concurrency group so a PR-close run no longer waits behind the scheduled sweep or another close: in `.github/workflows/cancel_on_pr_close.yml` job `cancel-active-runs`, set `group: cancel-on-pr-close-${{ github.repository }}-${{ github.event_name == 'schedule' && 'schedule' || github.event.pull_request.number }}` and keep `cancel-in-progress: false`. This is the root-cause fix; without it the e2e-smoke-test job can still time out in Phase 7.
- Update the concurrency-group pin in `tests/test_review_merge_train.py` near line 1860 to the new expression in the same change as the workflow edit. Changing the pin alone against the unchanged workflow would break that test, which is why it is deferred together with the e2e-smoke-test root-cause fix.
- Add an agents fragment that corrects the "Cleanup jobs share repository-scoped concurrency" text under "Conflict-safe PR-close cleanup", plus a changelog fragment for the concurrency change, so the docs match the e2e-smoke-test root-cause fix.

These items are delivered through a separate issue that is not a heal issue, because the heal-scope marker on this issue excludes `cancel_on_pr_close.yml` and `heal_scope_plan_out_of_scope` cannot be lifted by `/approved`.

## Functions or modules to implement

### 1. `.github/workflows/test-and-mark-stable.yml`, step `verify-cancel-on-close`, open-PR close path
- **Where:** after the `if [ -z "${NEW_RUN_ID}" ]; then … fi` no-run block, which ends at line 3434, and before the existing `if [ "${NEW_CONCLUSION}" != "success" ]; then` at line 3435.
- **What to insert:** one comment line, then a new branch. The comment says that a run was found but did not reach `completed` within the budget. It cites the run where a `pending` run produced `status=` empty and the summary showed SKIPPED (run 40794181365).
  ```
  if [ "${STATUS:-}" != "completed" ]; then
    echo "::error::cancel-on-close run #${NEW_RUN_ID} did not complete within ${PHASE7_WAIT_BUDGET_MINUTES}m (status=${STATUS:-?})"
    echo "status=timeout" >> "$GITHUB_OUTPUT"
    echo "cancel_run_id=${NEW_RUN_ID}" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  ```
- **Why this order:** it is the same order and wording as the already-closed path at lines 3125-3130 (error, status, run id, exit).
- **Why the guard is enough:** the no-run block above already exits when `NEW_RUN_ID` is empty, so this branch only runs when `NEW_RUN_ID` is set.
- **Why `${STATUS:-}`:** `STATUS` is set only inside the candidate branch of the loop, and the step uses `set -u`. The guard makes the branch safe under `set -u`.
- **What does not change:**
  - `PHASE7_WAIT_BUDGET_MINUTES`, the poll loop and the no-run diagnostic;
  - the existing conclusion check at lines 3435-3440 and the success path;
  - every step `id`, output name and log text.

### 2. Same file, step `verify` ("Verify all phases passed"), Cancel-PR row at lines 3623-3632
`PR_NUMBER` is already in this step's `env:` (line 3487) and comes from the same output as Phase 7's `if:` (line 3023). Replace the `elif [ -z "$CANCEL" ]` arm with a nested check:
- **`CANCEL` is empty and `PR_NUMBER` is non-empty:** print `  ✗ Cancel-PR .. FAILED (no status; pre-close unconflict=${CANCEL_UNCONFLICT:-n/a})` and set `PASSED=false`.
- **`CANCEL` is empty and `PR_NUMBER` is empty:** keep `  - Cancel-PR .. SKIPPED (no PR to close)` unchanged.

The `success` arm and the generic `else` FAILED arm stay as they are. `status=timeout` therefore shows as `✗ Cancel-PR .. FAILED (timeout; …)`.

### 3. `tests/test_test_and_mark_stable_phase7_timeout_status.py` (new)
Use `tests/test_test_and_mark_stable_cancel_on_close_single_fetch.py` as the model: tab indentation, the `_slice_between` helper, the fake `date`/`sleep` clock harness in `_run_phase7_wait`, and a `main()` runner. The helpers are copied locally rather than imported, matching the sibling files.

**Executable timeout test**
- Slice the open-PR branch, from `"# PR is still open — exercise the close path end to end."` to `"# Per-phase soft-error analysis: cancel-on-pr-close"`.
- Within it, slice from `DEADLINE=$(( $(date +%s) + (PHASE7_WAIT_BUDGET_MINUTES * 60) ))` to `if [ "${NEW_CONCLUSION}" != "success" ]; then`, then dedent. The slice now contains the new branch.
- Fake `phase7_list_cancel_runs` to return one run with `id: 200`, `event: "pull_request"`, `status: "pending"` and `conclusion: null`. Use `PRE_RUN_ID=100` and `PHASE7_WAIT_BUDGET_MINUTES=1`.
- Assert:
  - rc == 1;
  - `$GITHUB_OUTPUT` contains `status=timeout` and `cancel_run_id=200`, and no bare `status=` line;
  - stdout contains `::error::cancel-on-close run #200 did not complete within 1m (status=pending)`.

**Executable control tests**
- A run with `status: "completed"` falls through to the end of the slice: rc 0, and no `status=timeout`.
- The no-run case still writes `status=no_run`. This guards the order of the two branches.

**Static contract test**
In the `verify-cancel-on-close` step script (loaded through `yaml.safe_load` → `jobs.e2e-smoke-test.steps`), assert this order:
`if [ -z "${NEW_RUN_ID}" ]; then` < the new `did not complete within` error < `status=timeout` < `if [ "${NEW_CONCLUSION}" != "success" ]; then`.

**Executable summary test**
- From the `verify` step's `run`, slice from `CANCEL="${{ steps.verify-cancel-on-close.outputs.status }}"` to the following `echo ""`.
- Replace the two `${{ … }}` expressions with `${T_CANCEL}` and `${T_UNCONFLICT}`. Prefix `set -euo pipefail; PASSED=true` and append `echo "PASSED=$PASSED"`.
- Run it with bash for these cases:

| `T_CANCEL` | `PR_NUMBER` | Expected output |
|---|---|---|
| empty | `42` | `FAILED (no status` and `PASSED=false`; never `SKIPPED` |
| empty | empty | `SKIPPED (no PR to close)` and `PASSED=true` |
| `timeout` | `42` | `FAILED (timeout` and `PASSED=false` |
| `success` | `42` | `PASSED (` and `PASSED=true` |

**Syntax check:** `bash -n` on the full `verify` step script, as `test_phase7_step_script_is_valid_bash` does for Phase 7. Mask the `${{ }}` expressions first.

### 4. `changelog.d/8327-phase7-cancel-timeout-status.md` (new)
- Header `<!-- changelog: fixed -->`, in the same format as `changelog.d/8217-release-gate-capacity-wait.md`.
- One bullet: Phase 7 now writes `status=timeout` with an `::error::` line when a cancel-on-close run was found but did not complete, and the results table reports `FAILED (no status)` instead of SKIPPED when a smoke PR exists.
- An operator note: a stall caused by the concurrency group now shows as a Phase 7 timeout, and the concurrency-group fix comes separately.

## Data structures affected
- The `verify-cancel-on-close` step can now output `status=timeout` on the open-PR path. The already-closed path already does this, so the summary's generic FAILED arm handles it.
- `cancel_run_id` is still written on this path, so Phase 7b, the soft-error analyser, still runs for the stuck run, as it does today.
- No new env vars, step ids, output names or log prefixes.

## API or interface changes
None. No new `gh` calls: the new branch reads only `NEW_RUN_ID`/`STATUS` from the loop's existing listing (§14).

Failure modes:
- **Run registered but stuck `pending` or `queued` past the budget.**
  - Before: `status=` empty, the table shows SKIPPED, and it prints "ALL PHASES PASSED".
  - After: `::error::… did not complete within 10m (status=pending)`, `status=timeout`, the step exits 1, the table shows `FAILED (timeout; …)`, and "SOME PHASES FAILED".
  - Recovery: the deferred concurrency fix, or rerun the gate.
- **Phase 7 aborted with no output** (for example a `set -e` failure in jq, or the step skipped while a PR exists). Before: SKIPPED. After: `FAILED (no status; …)`, which fails closed.
- **No smoke PR** (`pr_number` empty). The SKIPPED row is unchanged.

## Decisions

### D1 — Where to put the timeout branch
- **Chosen:** a separate `STATUS != completed` branch between the no-run block and the conclusion check, written like lines 3125-3130.
- **Alternatives considered:**
  - Fold it into the conclusion check with `status=${NEW_CONCLUSION:-timeout}`.
  - Raise `PHASE7_WAIT_BUDGET_MINUTES`.
- **Why:**
  - A separate branch gives a precise `did not complete` error and matches the already-closed path.
  - Raising the budget would hide the stall, and the issue forbids it.
  - It also keeps the existing `if [ "${NEW_CONCLUSION}" != "success" ]` text that `tests/test_test_and_mark_stable_cancel_on_close_single_fetch.py:192` uses as a slice marker.

### D2 — How the summary tells "no status" from "no PR"
- **Chosen:** the existing `PR_NUMBER` env var of the `verify` step.
- **Alternatives considered:**
  - Add a new Phase 7 output such as `ran=true`.
  - Treat any empty `CANCEL` as a failure.
- **Why:**
  - `PR_NUMBER` comes from the same expression as Phase 7's `if:`, so it adds no new identifier.
  - Treating every empty `CANCEL` as a failure would break the real "no PR" case.

### D3 — Defer the concurrency-group fix
- **Chosen:** list it under Deferred scope, per AD-1.
- **Alternatives considered:**
  - Include it here (option B).
  - Drop it as transient (option C).
- **Why:**
  - Option B is blocked: the heal scope excludes `cancel_on_pr_close.yml`.
  - Option C goes against the evidence: BUG-007 recurs in four earlier analyses.

### D4 — Put the tests in a new sibling file
- **Chosen:** a new sibling test file.
- **Alternatives considered:** append to `tests/test_test_and_mark_stable_phase7_unconflict.py`.
- **Why:** that file is about Phase 7a unconflict. A focused file keeps the regression's purpose clear, and AD-1 allows a sibling.

## Security considerations
- **Trust boundaries:** `STATUS` comes from `jq -r '.status'` on a GitHub API run listing. It is a GitHub enum (`queued`, `pending`, `in_progress` and so on) and is echoed after an `::error::` prefix on a single line. Line 3367 already echoes the same value today, so this adds no new injection surface. `NEW_RUN_ID` is a numeric API id, already written to `$GITHUB_OUTPUT` at line 3438.
- **Authorization:** no new endpoint, token use or permission. No secrets touched.
- **Abuse case:** none new. The change only makes the gate stricter (it fails closed), so a stalled cancel run can no longer present the release gate as green. The `release` job is still gated on `needs.e2e-smoke-test.result == 'success'`.
- **Money handling:** none.

## Risks and edge cases
- **Existing slice tests:**
  - `_wait_loop_and_no_run_block` in `tests/test_test_and_mark_stable_cancel_on_close_single_fetch.py` slices up to `if [ "${NEW_CONCLUSION}" != "success" ]; then`, so it will now include the new branch. Its scenarios never find a run (all ids ≤ `PRE_RUN_ID`), so they exit in the no-run block first and are unaffected.
  - `_no_run_branch` ends at the first `exit 1`, inside the no-run block, so it is unaffected too.
  - Implement must run that file.
- **Phase 7 skipped while a PR exists** (an earlier step failed): the row now reads `FAILED (no status)` instead of SKIPPED. The job is already failing in that case, so this is only a more accurate report, not a new failure source.
- **Workflow size:** about 1 KB is added. Implement must check `wc -c .github/workflows/test-and-mark-stable.yml` stays below 480,000 (§24). `tests/test_workflow_file_size_limit.py` enforces this.
- **Conflict note:** the issue cites lines 3435-3439 and 3623-3628. These match the planning ref: the summary arm is at 3627-3628, and `PASSED` is upper-case in the code. The issue's "`passed=true`" refers to the final `$GITHUB_OUTPUT` write at line 3639.
- **Not fixed here:** the root-cause stall can still happen until the deferred concurrency fix lands.

## Testing considerations
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_test_and_mark_stable_phase7_timeout_status.py tests/test_test_and_mark_stable_cancel_on_close_single_fetch.py tests/test_test_and_mark_stable_phase7_unconflict.py tests/test_workflow_file_size_limit.py`
- Parse the workflow YAML (the tests already use `yaml.safe_load`).
- Run `bash -n` on both edited step scripts. The new tests cover this.

## Counterfactual check
The evidence names one failing job, **e2e-smoke-test**. It failed in step "Phase 7: Close PR and verify cancel_on_pr_close fires". The timeline:
1. The smoke PR closed at 03:53:20Z.
2. Event run #40815217588 was found but stayed `pending`.
3. The 10-minute budget ran out.
4. At 04:03:24Z the step logged `::error::cancel-on-close run #40815217588 concluded ` with an empty conclusion.
5. The soft-error analyser at 04:05:27Z still saw `status=pending`.

The step wrote an empty `status=` and exited 1. The summary then printed SKIPPED and "ALL PHASES PASSED", while the job result was still failure.

With this plan, at step 4 the new branch runs instead. It logs `::error::cancel-on-close run #40815217588 did not complete within 10m (status=pending)`, writes `status=timeout` and `cancel_run_id`, and exits 1. The summary reports `✗ Cancel-PR .. FAILED (timeout; …)` and "SOME PHASES FAILED".

So e2e-smoke-test still fails on the recorded run, now correctly, because the cause (the run waiting on the shared concurrency group) is not changed by this in-scope fix. The intake's "yes" answer assumed the deferred concurrency-group change. Re-derived for this plan's scope, the answer is no. Nothing in the evidence suggests the in-scope change alone would let the pending run start. The job is listed under Deferred scope.

HEAL_COUNTERFACTUAL: PARTIAL: e2e-smoke-test

## Pre-execution self-check
- The edits stay inside the heal-scope marker: the workflow file, `tests/**` and `changelog.d/*.md`.
- No identifier is renamed, no budget is raised and no check is weakened. The gate becomes stricter.
- The new branch's order (error, `status=timeout`, `cancel_run_id`, `exit 1`) matches AD-1 and lines 3125-3130.
- The summary keeps SKIPPED only when there is no PR, and fails closed otherwise.
- The existing slice-marker tests are checked against the insertion point.
- The Deferred scope bullets each contain the token `e2e-smoke-test` and have no links, issue references or mentions.

PLAN_SELF_CHECK: PASS
STATUS: CLEAR
To proceed reply:

/approved

To restart clarification reply:

/reclarify

<!-- ai:plan-proposal:v1 -->

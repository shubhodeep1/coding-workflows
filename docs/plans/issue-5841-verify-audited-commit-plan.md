# Read-result stages accept only a run that covered the project's current code

Source issue: shubhodeep1/coding-workflows#5841 (https://github.com/shubhodeep1/coding-workflows/issues/5841)
Base branch: claude/implement-plan-issue-5016-dispatch-exact-run-id
Security pass: skip (ai:security: automation-produced issue)

## Summary

When `dispatch_workflow.py` cannot name a dispatch's run exactly, `/implement-plan-claude` steps 9 and 10 accept the first completed candidate whose log shows the project branch, so a clean verdict from an audit of an older commit can be applied to code it never saw. This plan has the helper's fallback count only runs dispatched from the same workflow ref, and makes the read-result stages accept a run only when it covered the project branch's current head, reading every matching candidate and re-auditing once on a mismatch.

## Context

- The security audit of project #5016's branch (`claude/implement-plan-issue-5016-dispatch-exact-run-id`) filed this finding against `.claude/commands/implement-plan-claude.md:71` (A08:2021, high, 8/10).
- Step 9 on the base branch: when the helper returned `matched_by: new_run` or an ambiguous result, the read-result stage "checks each of them in order: a completed candidate whose log shows the project branch is this project's run". It matches the branch name only. Two audits of the same branch can cover different commits, and the first one checked wins. Step 10 applies the same rule to validation.
- The helper's fallback (`new_runs`, `workflow-templates/.claude/scripts/dispatch_workflow.py:93-101` on the base branch) counts every `workflow_dispatch` run of the workflow that did not exist before the POST, whatever ref it was dispatched from. A run dispatched from another branch's copy of the workflow file is counted as a candidate for this dispatch.
- The runs already log the commit they covered, so nothing new is needed from the workflows:
  - `security-audit.yml` "Verify audit data and resolve scope" prints `SECURITY_AUDIT_TARGET: branch <ref> range <merge-base>..<sha>` and exports `SECURITY_AUDIT_DIFF_HEAD=<sha>` for every later step. `<sha>` is the branch head the run resolved (`AUDIT_DATA_SHA`).
  - `validate.yml` "Log checkout ref" prints `HEAD commit: <sha>`. For a `target_ref` run this is the authorized final-PR head, which is the project branch head.
- A clean step 2 sync only merges default-branch commits into the project branch. The branch audit's scope is `merge-base..head`, so it excludes those commits anyway.
- Project #5016 decided against workflow, `run-name`, or consumer-wrapper changes (its AD-1 and AD-6) because consumer wrappers that predate a new input reject it.

## Goals

- G1. The helper's polling fallback counts a new run only when its `head_branch` equals the ref the helper dispatched on. A run started from another ref is never `new_run` or a `candidate_run_ids` entry.
- G2. In project mode, a security read-result stage uses a run's verdict only when the run's log shows `AUDIT_TARGET_REF_INPUT` equal to the project branch and an audited commit (`SECURITY_AUDIT_TARGET … ..<sha>` / `SECURITY_AUDIT_DIFF_HEAD`) equal to the project head at the start of that stage, before its step 2 sync. This holds for every match type, `dispatch_response` included.
- G3. A validation read-result stage applies the same rule to `VALIDATE_TARGET_REF` and `HEAD commit:`.
- G4. With several recorded candidates, the stage decides only once every one has completed. Every candidate matching both ref and commit counts: the security pass is clean only when all of them are, and validation acts on the worst verdict among them.
- G5. A read-result stage runs no step 2 sync merge (AD-7, PR #5860 review round 1: a clean sync advances the branch too), so no commit lands on the project branch between the check and the verdict. A mismatch (no candidate matches) never yields a verdict. The stage re-dispatches once in the cycle, and a second mismatch stops at `Status: BLOCKED` (#5016's existing bound).

## Non-goals

- No workflow, `run-name`, or consumer-wrapper change, and no new workflow input (#5016 AD-1/AD-6).
- No change to `check_in_status.py`, the checker prompt, or the allowlist.
- Legacy mode, which audits the default branch, keeps the ref-only check (AD-6).
- No change to which code a branch audit covers.

## Constraints

- §5: only the helper twin, the command twin, their tests, `agents.md`, and one changelog fragment.
- §6: `new_runs` gains an optional keyword `ref` (default `None`, which keeps today's behaviour). No existing output field, function, or log key is renamed. "Pre-sync head" is new prose, not an identifier.
- §15: no new API call. The filter reads `head_branch` from the run list the helper already fetches.
- §28.C protected paths, under the interim twin-first default: edit only `workflow-templates/.claude/**`, then hold and post the twin-sync blocker.
- §9 tabs in Python; §20 one fragment; §7 `agents.md` describes the fallback.

## Approach

1. Helper twin: `new_runs(repo, workflow, known_ids, ref=None)` drops runs whose `head_branch` is not `ref` when `ref` is given. `dispatch` passes its dispatched `ref`, and the error text and docstring say so.
2. Command twin, step 2: a `… — read result` stage of steps 9 and 10 records the head from the mode check's `git ls-remote --heads origin claude/implement-plan-<slug>` as the **pre-sync head** and runs no sync merge (nor the Issue Mode base-branch move) in that session (AD-7); the next stage syncs.
3. Command twin, step 9: replace "check each of them in order … is this project's run" with the rule in G2, G4, and G5, and keep the #5016 re-arm on an in-progress candidate.
4. Command twin, step 10: same rule with the validation log keys.
5. Command twin, Dispatch helper section: describe the `head_branch` filter, and say the right candidate is confirmed by target ref and audited commit.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan; the helper filter and the read-result rule are one reviewable fix to one finding.

1. **Phase 1 — same-ref fallback candidates; ref-and-commit check before any verdict in steps 9–10.**
   - protected paths: `.claude/scripts/dispatch_workflow.py`, `.claude/commands/implement-plan-claude.md` (twins: `workflow-templates/.claude/scripts/dispatch_workflow.py`, `workflow-templates/.claude/commands/implement-plan-claude.md`)
   - Files: see [Files & Modules](#files--modules).
   - Done:
     - The new helper tests pass against the twin: a run from another ref is neither `new_run` nor a candidate, and a same-ref run still is.
     - The new command tests pass against the twin: step 2 records the pre-sync head; steps 9 and 10 require ref plus audited commit, decide after every candidate completes, count all matches, and re-dispatch once.
     - Existing suites pass in a scratch copy with the twins in `.claude/`.
     - `ruff check` is clean.
   - Rollback: revert the phase PR.

## Implementation Steps

Phase 1:
1. `workflow-templates/.claude/scripts/dispatch_workflow.py`:
   - `new_runs` gains `ref: str | None = None` and keeps a run only when `run.get("head_branch") == ref`.
   - `dispatch` calls it with `ref=ref`.
   - The docstring and the ambiguous / timeout error texts mention the same-ref filter.
2. `tests/test_dispatch_workflow.py`:
   - `_run` gains `head_branch="main"`.
   - New tests load the twin module: a run from another ref is ignored in the single and the ambiguous case, and the old `find_new_run` signature is unchanged.
3. `workflow-templates/.claude/commands/implement-plan-claude.md`: step 2 sync bullet (pre-sync head), step 9 read-result paragraph, step 10 read-result sentence, and the Dispatch helper paragraph.
4. `tests/test_implement_plan_claude_command.py`: twin-reading tests for the new step 2, 9, and 10 text.
5. `agents.md`: the `dispatch_workflow.py` bullet mentions the same-ref filter and the commit check.
6. `changelog.d/5841-verify-audited-commit.md` [new] (`security`).

## Files & Modules

- `workflow-templates/.claude/scripts/dispatch_workflow.py` (twin of `.claude/scripts/dispatch_workflow.py`, which is copied at the twin sync)
- `workflow-templates/.claude/commands/implement-plan-claude.md` (twin of `.claude/commands/implement-plan-claude.md`, which is copied at the twin sync)
- `tests/test_dispatch_workflow.py`
- `tests/test_implement_plan_claude_command.py`
- `agents.md`
- `changelog.d/5841-verify-audited-commit.md` [new]

## Tests

- Unit: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_dispatch_workflow.py tests/test_implement_plan_claude_command.py -q`. On the phase branch, the two `test_template_parity` tests stay red until the twin sync, by design (§28.C interim default).
- Scratch copy with both twins copied into `.claude/`: the same two files plus `tests/test_permission_prompts.py`, `tests/test_update_workflows_guardrails.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_check_in_session_targeting.py`, and `tests/test_changelog_fragment_contract.py`.
- `ruff check` on the changed Python.

## Risks & Mitigations

- The step 2 sync merges a busy default branch on every stage, so comparing against the post-sync head would re-audit after nearly every run → compare against the pre-sync head (AD-2), and run no sync in a read-result stage, so even a clean merge never lands between the check and the verdict (AD-7).
- A legitimate move of the project branch during an audit (a late follow-up fix merge) means a mismatch and one re-dispatch. Two in a row means `Status: BLOCKED` → ACCEPTED — the branch should not move during a run wait, so two moves in a row need a human.
- Waiting for every candidate can delay a read by the difference in the runs' durations → ACCEPTED — only on the fallback path, which needs both a missing run id and a parallel dispatch (#5016 AD-7).
- A run without `head_branch` is dropped from the fallback → ACCEPTED — the field is always present on workflow runs. Its absence means a timeout and exit 2 with `dispatched: true`, never a wrong run.
- Consumer repos keep the old text until their next `.claude/` sync → ACCEPTED — the existing sync ships it.

## Rollout

Ships with the next `@stable` release through the `.claude/` sync. No flag. Rollback is a revert of the phase PR.

## References

- Issue #5841, the parent project #5016 (final PR #5051), and security tracker #3576.
- `.github/workflows/security-audit.yml` "Resolve audit target" / "Verify audit data and resolve scope"; `.github/workflows/validate.yml` "Log checkout ref".
- CLAUDE.md §23.I (helpers), §28 (issue mode, twin-first interim default).

## Auto-decisions

- AD-1 [plan, 2026-10-01] How should fallback candidates be correlated with this dispatch? — Picked: A — the helper counts only new runs whose `head_branch` is the ref it dispatched on; the read-result stage then requires the target ref and the audited commit. Alternatives: B — a correlation input echoed into each workflow's `run-name` (a workflow and consumer-wrapper change that #5016 AD-1/AD-6 rejected); C — keep the ref-only check. Why: uses data the helper already reads (§15) and blocks a run started from another ref's workflow file, with no workflow change (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-10-01] Which project head must the audited commit match? — Picked: A — the head the read-result stage finds on origin before its step 2 sync merge; a sync that needed a conflict resolution counts as a mismatch. Alternatives: B — the head after the sync; C — a head the dispatching stage records at dispatch. Why: a clean sync adds only default-branch commits, which a branch audit's merge-base range excludes, while B would re-audit nearly every time the default branch moved and C would accept code merged into the project branch after the dispatch. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-10-01] Does the commit check apply to a run GitHub named (`matched_by: dispatch_response`)? — Picked: A — yes, to every recorded run. Alternatives: B — only to unverified matches. Why: an exactly matched run still covers whatever head it resolved when it started, so one rule for every match keeps the gate simple and strict (§1). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-10-01] What happens when several recorded candidates match both ref and commit? — Picked: A — decide only after every recorded candidate has completed; the security pass is clean only when every matching run concluded `success` and opened no follow-up, and validation acts on the worst verdict among them. Alternatives: B — take the first matching candidate; C — stop at `Status: BLOCKED`. Why: B is the defect in the finding, and C stalls the project for a case the stage can settle safely. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-10-01] How is the issue's "re-audit on a mismatch" bounded? — Picked: A — reuse #5016's bound: one re-dispatch in the same cycle, and a second mismatch stops at `Status: BLOCKED`. Alternatives: B — count each re-dispatch as a new security cycle (cap 5); C — re-dispatch without a bound. Why: the project branch should not move during a run wait, so two mismatches in a row need a human, and C could loop (§28.C). Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-10-01] Should legacy mode, which audits the default branch, also require the audited commit to match? — Picked: A — no, legacy mode keeps the ref check. Alternatives: B — compare with the default branch head. Why: the default branch moves under almost every audit, so B would re-audit endlessly, and no project branch exists to pin. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5841` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Base branch `claude/implement-plan-issue-5016-dispatch-exact-run-id` is the head of open draft PR #5051 (into `main`), checked 2026-10-01.

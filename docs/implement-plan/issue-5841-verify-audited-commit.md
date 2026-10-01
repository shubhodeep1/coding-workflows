# Implement-Plan Log — Read-result stages accept only a run that covered the project's current code

- Plan: docs/plans/issue-5841-verify-audited-commit-plan.md
- Source issue: shubhodeep1/coding-workflows#5841
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-5016-dispatch-exact-run-id
- Project branch: claude/implement-plan-issue-5841-verify-audited-commit   Final PR: #5853 draft
- Status: BLOCKED
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5860: twin sync (review round 1 fix on head 952aa92)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01JWrYWJEY1JfCWroLBHCr3j (kept idle for the resume)   safety net none   hand-back none
- Last updated: 2026-10-01
- Last note: review round 1 on PR #5860 head `952aa92` (second review of the round-1 fix, ledger `6f192a89…`): fixed the persisted re-dispatch bound (AD-8: the `— re-dispatched` stage-name marker), the helper's ambiguous-result wording, and step 10's "in this cycle" twin-first; rejected the timeout-string finding; PR held again for the `[claude-twin-sync]` copy, blocker posted on issue #5841.

## Phases
1. [ ] Phase 1 — same-ref fallback candidates; ref-and-commit check before any verdict in steps 9–10
   - protected paths: `.claude/scripts/dispatch_workflow.py`, `.claude/commands/implement-plan-claude.md` (twins: `workflow-templates/.claude/scripts/dispatch_workflow.py`, `workflow-templates/.claude/commands/implement-plan-claude.md`)
   - [x] helper twin: `new_runs(..., dispatched_ref=None)` keeps only runs whose `head_branch` is the dispatched ref; `dispatch` passes it (`workflow-templates/.claude/scripts/dispatch_workflow.py:107-122`, `:271`)
   - [x] `tests/test_dispatch_workflow.py`: 5 same-ref filter tests against the twin (`dw_twin`); `_run` carries `head_branch`; the two `new_runs` fakes accept `dispatched_ref`
   - [x] command twin: step 2 pre-sync head (line 19); step 9 ref + audited-commit rule, all candidates, one re-dispatch (line 71); step 10 (line 78); Dispatch helper paragraph (line 179)
   - [x] `tests/test_implement_plan_claude_command.py`: 5 twin-reading tests for steps 2, 9, 10 and the Dispatch helper section
   - [x] `agents.md` (dispatch_workflow bullet), `changelog.d/5841-verify-audited-commit.md` (`security`)
   - review rounds: 2; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] How should fallback candidates be correlated with this dispatch? — Picked: A — the helper counts only new runs whose `head_branch` is the ref it dispatched on; the read-result stage then requires the target ref and the audited commit. Alternatives: B — a correlation input echoed into each workflow's `run-name` (a workflow and consumer-wrapper change that #5016 AD-1/AD-6 rejected); C — keep the ref-only check. Why: uses data the helper already reads (§15) and blocks a run started from another ref's workflow file, with no workflow change (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-10-01] Which project head must the audited commit match? — Picked: A — the head the read-result stage finds on origin before its step 2 sync merge; a sync that needed a conflict resolution counts as a mismatch. Alternatives: B — the head after the sync; C — a head the dispatching stage records at dispatch. Why: a clean sync adds only default-branch commits, which a branch audit's merge-base range excludes, while B would re-audit nearly every time the default branch moved and C would accept code merged into the project branch after the dispatch. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-10-01] Does the commit check apply to a run GitHub named (`matched_by: dispatch_response`)? — Picked: A — yes, to every recorded run. Alternatives: B — only to unverified matches. Why: an exactly matched run still covers whatever head it resolved when it started, so one rule for every match keeps the gate simple and strict (§1). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-10-01] What happens when several recorded candidates match both ref and commit? — Picked: A — decide only after every recorded candidate has completed; the security pass is clean only when every matching run concluded `success` and opened no follow-up, and validation acts on the worst verdict among them. Alternatives: B — take the first matching candidate; C — stop at `Status: BLOCKED`. Why: B is the defect in the finding, and C stalls the project for a case the stage can settle safely. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-10-01] How is the issue's "re-audit on a mismatch" bounded? — Picked: A — reuse #5016's bound: one re-dispatch in the same cycle, and a second mismatch stops at `Status: BLOCKED`. Alternatives: B — count each re-dispatch as a new security cycle (cap 5); C — re-dispatch without a bound. Why: the project branch should not move during a run wait, so two mismatches in a row need a human, and C could loop (§28.C). Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-10-01] Should legacy mode, which audits the default branch, also require the audited commit to match? — Picked: A — no, legacy mode keeps the ref check. Alternatives: B — compare with the default branch head. Why: the default branch moves under almost every audit, so B would re-audit endlessly, and no project branch exists to pin. Applied in: phase 1. Status: pending review
- AD-7 [phase 1/1 — review round, 2026-10-01] How should a read-result stage keep the project branch from moving after it checks a run's audited commit? (PR #5860 review round 1: a clean sync merge advances the branch too, not only a conflict-resolved one) — Picked: A — a `… — read result` stage of steps 9–10 runs no step 2 sync merge (nor the Issue Mode base-branch move) for the whole session, including the steps it continues to; the next stage syncs. Alternatives: B — keep the sync and treat any sync that advanced the branch as a mismatch; C — keep the conflict-only rule (reject the finding). Why: B re-dispatches after nearly every run on a busy default branch and hits the second-mismatch `Status: BLOCKED`, C leaves unaudited commits landing after the check (§1); A closes the gap with no extra run. Refines AD-2's conflict-resolution clause. Applied in: PR #5860. Status: pending review
- AD-8 [phase 1/1 — review round, 2026-10-01] How does a fresh read-result stage know the cycle's one re-dispatch was already used? (PR #5860 review round 1 on head `952aa92`: the bound was not persisted across stage sessions) — Picked: A — the re-dispatching stage names the next stage `security-pass <k>/5 — read result — re-dispatched` / `validation <k>/3 — read result — re-dispatched` (carried by the checker into the `— resume.` block's `Stage:` line and the session title) and records `Waiting on: run <id> (re-dispatched after a mismatch)`; a mismatch in a stage whose `Stage:` ends with `— re-dispatched` is the second one. Alternatives: B — a new `Mismatch retries:` progress-log field (the log is not committed while no PR is in flight, so a read-result stage cannot rely on it); C — a new `— resume.` block line (changes the checker prompt template every stage fills). Why: the stage name is the one value every hand-off already carries verbatim, so no template or script changes (§5). Applied in: PR #5860. Status: pending review

## Lessons
- [source:intervention] A gate that checks a run against a branch head must not let the same stage move that branch afterwards: skip the sync merge in the stage that reads the verdict instead of trying to classify which syncs are harmless. (files: .claude/commands/implement-plan-claude.md)
- [source:intervention] A retry bound in a chain of fresh stage sessions must ride a value every hand-off already carries verbatim (the next stage's name), or each session sees its first attempt and the bound never fires. (files: .claude/commands/implement-plan-claude.md)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine (`PR dispatch: #5841`, trig_01Fw4qrQgfcV7JiQApoQvLkM) in session session_01Nd8VfnooECN2NstKRn42NL, permission mode `auto`.
- Issue base `claude/implement-plan-issue-5016-dispatch-exact-run-id` (head `eb0413f`) is the head of open draft PR #5051 into `main`; no merged PR for it (checked 2026-10-01).
- Security pass: `security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`, so step 9 is skipped.
- Stale Routine sweep (2026-10-01): 100 listed, nothing to delete (33 kept, 67 not ours).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)
- Phase 1 verification (2026-10-01): on the phase branch `tests/test_dispatch_workflow.py` + `tests/test_implement_plan_claude_command.py` → 85 passed / 1 skipped, the two `test_template_parity` tests red until the twin sync (by design). Reverse check: with the twins at `HEAD~1`, all 10 new tests fail. Scratch copy with both twins in `.claude/`, every test file that names `implement-plan-claude`, `dispatch_workflow`, `changelog.d`, or `agents.md` (34 files) → 2195 passed / 1 skipped / 1 failed; the failure (`test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean`) is `scripts/review_issue_ledger.sh: gawk: command not found`, a sandbox tool gap unrelated to this change. `ruff check` clean. Twin sha256: `dispatch_workflow.py` `4c9bb4aae89c7f5ec00fce9236592978a9732b69947a9bb578610838a786dabb`, `implement-plan-claude.md` `c08f007b45dd0341c6e76d8026baea28d1b902457e2a9583eab910826286b568`.
- Sandbox note: `tests/test_ingest_implement_plan_lessons.py` and seven other suites import `yaml`, which the session's `pytest` lacks; they ran through `uv run --no-project --with pytest --with pyyaml`.
- Review round 1 (2026-10-01, PR #5860 head `dc77949`, ledger `0936ea6c…`): consensus finding 1 (critical, 6/6 reviewers, `implement-plan-claude.md` steps 2/9/10: a clean sync merge advances the branch after the verdict check) fixed twin-first as AD-7; finding 2 (low, error strings print `None`) rejected, because `dispatch()` resolves `ref` to a non-empty default branch or raises before any of those strings (`workflow-templates/.claude/scripts/dispatch_workflow.py:221-224`); finding 3 (`head_branch` missing drops a run) rejected as the intended fail-closed filter (plan Risks: a dropped run means exit 2 with `dispatched: true`, never a wrong run). Plan G5, Approach step 2, and Risks updated to AD-7.
- Review round 1, second review (2026-10-01, PR #5860 head `952aa92`, ledger `6f192a89…`; the earlier ledger on the same head, `5925238178`, flagged the partly synced `.claude/` copy, which the owner's sync had already fixed: both twins were byte-identical at `952aa92`): consensus finding 1 and the task gap (high, the one-re-dispatch bound is not persisted across fresh stage sessions) fixed twin-first as AD-8; finding 2 (the helper's ambiguous-result error says "never dispatch again") fixed: it now bans a new dispatch only while a candidate could still be this dispatch's run, and the Dispatch helper section and step 9's dispatch-time rule say the same; finding 3 (the timeout error says "never dispatch again") rejected, because that string says `check gh run list … before dispatching again` (`workflow-templates/.claude/scripts/dispatch_workflow.py:306`); finding 4 (step 10 lacks "in this cycle") fixed.

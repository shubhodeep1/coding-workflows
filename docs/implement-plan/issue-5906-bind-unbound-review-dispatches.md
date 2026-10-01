# Implement-Plan Log — Pending-checks auto-merge: bind review dispatches to their PR and count their failures

- Plan: docs/plans/issue-5906-bind-unbound-review-dispatches-plan.md
- Source issue: shubhodeep1/coding-workflows#5906
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
- Project branch: claude/implement-plan-issue-5906-bind-unbound-review-dispatches   Final PR: #5916 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5929
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_016uEWHbfeYumDLuXt13ZaAp   safety net and hand-back re-armed by the review round 3 stage (ids in its report)
- Last updated: 2026-10-01
- Last note: review round 4 on PR #5929: 1 finding fixed (a failed dispatch from a non-default, non-head ref whose run name named another PR was ignored), 2 rejected (misread test source), task gap covered; rounds 1 to 4 were one root cause, so every dispatch now goes through one rule, `_classify_review_dispatch()` (AD-8); waiting on round 5.

## Phases
1. [ ] Phase 1 — bind review dispatches to their PR and count their failures   — PR #5929 open (waiting); review rounds: 4; interventions: 0
   - `run-name` `<workflow name> [pr:<pr_number>]` on `workflow_dispatch` in `.github/workflows/review_autofix.yml`, `.github/workflows/review_rb_judge_dispatch.yml`, `workflow-templates/ai-review.yml`, `workflow-templates/review_rb_judge_dispatch.yml`
   - `check_review_runs()` in `scripts/claude_fixer_pending_checks.py`: a newer completed dispatch titled for the PR joins the bound reviews; a newer unsuccessful one with no PR binding returns `review_superseded`
   - Tests: the audit's scenario, every new path, the run-name wiring; existing #4900 / #5147 / #5148 suites green
   - `README.md`, `agents.md`, `docs/INVENTORY.md`; `changelog.d/5906-bind-review-dispatches-to-pr.md`
   - Done: the plan's phase 1 "done" condition. Protected paths: none.

## Conformance

## Security pass
- Skipped: plan header `Security pass: skip (ai:security: automation-produced issue)`

## Validation

## Completion
- Final PR #5916 draft (into the #4900 project branch)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] How are the unbound review dispatches bound to a PR? — Picked: A — a `run-name` of `<workflow name> [pr:<pr_number>]` on `workflow_dispatch` in `review_autofix.yml`, `ai-review.yml`, and both `review_rb_judge_dispatch.yml` copies, read from the listing's `display_title`. Alternatives: B — read each run's jobs or logs to find its PR (calls per run, §15); C — no binding, block on every newer unsuccessful dispatch (blocks every PR in the repo until its next review). Why: the `internal-review.yml` precedent, no new API calls. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What does a newer completed dispatch with no `[pr:<N>]` title do? — Picked: A — fail closed: if it did not conclude `success`, `review_superseded`. Alternatives: B — ignore it (today's behaviour, the finding). Why: §1; such runs only come from wrappers before the sync or refs without the run name. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How strictly is the title matched? — Picked: A — any title ending in ` [pr:<N>]` (`^.+ \[pr:([1-9][0-9]*)\]$`) in these workflows' listings. Alternatives: B — an exact title per workflow name. Why: the listing already fixes the workflow; a suffix survives a renamed workflow, and only a workflow author controls the run name. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Do active dispatches titled for another PR stop blocking? — Picked: A — no, every active dispatch of these workflows still blocks (`review_active`). Alternatives: B — block only on ones titled for this PR or untitled. Why: §5 (no relaxation in a security fix); the delay is one sweep tick. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-10-01] Also change the "latest newer review" rule so a later gate-skipped success cannot clear the failure? — Picked: A — no, that is #5904; titled dispatches join the bound set, so #5904's fix covers them too. Alternatives: B — fix it here. Why: one issue per project (`/implement-issue-claude` rules). Applied in: no code change. Status: pending review
- AD-6 [phase 1/1 — review round 1, 2026-10-01] How does `check_review_runs()` treat a finished review dispatch that did not run from the default branch, whose run name its own ref's workflow file sets? — Picked: A — its run name never binds it: it is never a bound review (a success is ignored), and when it did not succeed and its title names this PR or no PR it returns `review_superseded`; the same for `internal-review.yml` dispatches; no default branch known means no run name binds. Alternatives: B — ignore every non-default-branch dispatch (a failed review of this PR from another ref would be lost); C — trust the title on any ref (a forged success masks a failed review, the round 1 finding). Why: §1, an untrusted title may only block a merge, never allow one; keeps AD-2 fail closed. Applied in: PR #5929. Status: pending review
- AD-7 [phase 1/1 — review round 2, 2026-10-01] What does `check_review_runs()` do with a finished review workflow dispatch that the head-branch listing returns (dispatched with the PR's head branch as its ref)? — Picked: A — never a bound review; a newer one that did not conclude `success` returns `review_superseded` whatever PR its run name names. Alternatives: B — drop it from the head-branch path only, leaving it to the dispatch listings (a failed one titled for another PR, which blocked before this PR as the latest head-branch review, would then be ignored); C — apply the run-name rule (block only when titled for this PR or no PR). Why: §1, it ran the head branch's workflow file on this PR's own branch, so its run name is untrusted and it may have been a review of this PR; keeps the pre-#5906 block. Applied in: PR #5929. Status: pending review
- AD-8 [phase 1/1 — review round 4, 2026-10-01] What does `check_review_runs()` do with a finished review workflow dispatch from a ref other than the default branch whose run name names another PR? — Picked: A — its run name is never read: a newer one that did not conclude `success` returns `review_superseded` whatever PR it names, an `internal-review.yml` one still running returns `review_active`, and every dispatch from every listing goes through one rule, `_classify_review_dispatch()`; this narrows AD-6, which still let such a run be ignored when its title named another PR. Alternatives: B — patch only the unbound-workflow loop's condition (leaves the same hole in the `internal-review.yml` listing and its active check); C — keep AD-6's "names this PR or no PR" scope (the round 4 finding stands). Why: §1 and AD-6's own rationale, an untrusted title may only block a merge, never allow one; four review rounds hit the same root cause, so the rule now lives in one place. Applied in: PR #5929. Status: pending review

## Lessons
- [source:intervention] A workflow run's `display_title` (its `run-name`) is only trustworthy on a run from the default branch: a `workflow_dispatch` on any other ref runs that ref's workflow file, so any PR binding read from a title must also check `head_branch` against the default branch. (files: scripts/claude_fixer_pending_checks.py, .claude/scripts/check_in_status.py)
- [source:intervention] An `actions/runs?branch=<head>` listing also returns `workflow_dispatch` runs started with that branch as their ref, which ran the branch's own workflow file with any inputs; a trust rule applied to dispatch listings must also be applied (by `event`) wherever a branch listing is read. (files: scripts/claude_fixer_pending_checks.py)
- [source:intervention] A rule meant to cover every review workflow must not reuse a list built for another purpose: `check_in_status.FIXER_WORKFLOW_PATHS` lacks the judge wrapper `review_rb_judge_dispatch.yml`, so a guard over review dispatches checks it together with `UNBOUND_DISPATCH_REVIEW_WORKFLOWS`. (files: scripts/claude_fixer_pending_checks.py, .claude/scripts/check_in_status.py)
- [source:intervention] When a trust rule (here: only a default-branch run's name is trusted) has to hold across several listings, decide trust once per run in one classifier that every listing feeds, and never read the untrusted field at all; patching each listing's condition separately let four review rounds each find one more combination that still read it. (files: scripts/claude_fixer_pending_checks.py)

## Notes
- Issue mode; session `session_01YEZ7MVKkvpsMYW3kkWx4by` (started by the Claude issue pickup routine `PR dispatch: #5906`).
- `security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Validation: dispatched normally against the project branch (`docs/operations/master-session.md` Q17 was superseded on 2026-09-30, once #4734 let `validate.yml` authorize a final PR into another project's branch).
- Base branch check (2026-10-01): the PR whose head is the base branch, #4922 (into `main`), is open and unmerged, so the base has not moved.

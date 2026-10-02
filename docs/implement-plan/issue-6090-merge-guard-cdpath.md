# Implement-Plan Log — §21 merged-PR guard: block a guarded git call after a `cd` that CDPATH may redirect

- Plan: docs/plans/issue-6090-merge-guard-cdpath-plan.md
- Source issue: shubhodeep1/coding-workflows#6090
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-5144-merge-guard-effective-repo
- Project branch: claude/implement-plan-issue-6090-merge-guard-cdpath   Final PR: #6109 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-02
- Last note: phase 1 implemented twin-first (44f0e50): 396 guard tests pass against the twin except `test_template_copies_are_identical` (red until the `[claude-twin-sync]` copy); 478 related tests pass. Phase PR opening; twin-sync blocker follows.

## Phases
1. [ ] Phase 1 — block guarded git calls after a CDPATH-redirectable `cd` / `pushd` — protected paths: `.claude/hooks/pr_merge_status_guard.py`
   - Twin hook: `GuardTarget.unjudgeable_reason`, CDPATH state in `guard_targets`, block in `_evaluate_bash`
   - Tests in `tests/test_pr_merge_status_guard.py` (unit + e2e on `worktree_repo`) reading the twin
   - CLAUDE.md §21.B paragraph; `changelog.d/6090-merge-guard-cdpath.md`
   - Done: new tests pass against the twin; all other tests pass except `test_template_copies_are_identical` until the `[claude-twin-sync]` copy

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue, verified by `security_pass_skip.py`)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-02] How should the guard treat a `cd` whose operand Bash may look up in `CDPATH` while `CDPATH` may be set? — Picked: A — block the guarded calls after it as unjudgeable, without modelling the search. Alternatives: B — model Bash's `CDPATH` search; C — fall back to the session checkout with a warning (#6038's proposal). Why: C reproduces the incorrect allow; B adds an allow path that must match Bash exactly; A only tightens. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] Block (exit 2) or ask for an unjudgeable call? — Picked: A — block. Alternatives: B — ask. Why: the issue says deny; an ask stalls unattended sessions; the workaround is one edit. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Which `CDPATH` sources count, and can an earlier `unset CDPATH` or empty assignment clear one? — Picked: A — inherited non-empty `CDPATH`, the `cd`'s own non-empty prefix, and any earlier word containing `CDPATH` turn it on; only the `cd`'s own empty `CDPATH=` prefix turns it off for that `cd`. Alternatives: B — also honour `unset` / empty assignments in earlier segments. Why: the walker cannot prove an earlier command ran. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-02] Does the rule cover `pushd`, and a `cd` after the directory is already unknown? — Picked: A — yes to both. Alternatives: B — `cd` with a known directory only. Why: same CDPATH search, same incorrect allow under the fallback. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-02] Does the block apply to `git commit` as well as `git push`? — Picked: A — both. Alternatives: B — push only. Why: the issue says deny the write; both are §21-guarded writes. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-02] Fix the `CDPATH` row now although #6038 lists it and is deferred until #5163 merges? — Picked: A — yes, only the `CDPATH` row, on the issue's project branch. Alternatives: B — leave it to #6038 and close this as a duplicate. Why: #6090 is the #5144 project's own security follow-up, which its security pass needs fixed on the project branch. Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1, 2026-10-02] Should a `cd` / `pushd` run through `builtin` or `command` count for the CDPATH rule, although wrapper prefixes in general are #6038's scope? — Picked: A — yes, for the CDPATH rule only. Alternatives: B — no, leave every wrapper to #6038. Why: `CDPATH=.. builtin cd .git` is a direct bypass of this fix; the walker still does not follow those wrappers' directory changes otherwise (#6038). Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-02)
- Security pass: `Security pass: skip` per `security_pass_skip.py` (`ai:security` created and labelled by the issue automation).

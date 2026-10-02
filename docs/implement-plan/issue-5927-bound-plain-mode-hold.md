# Implement-Plan Log — Bound how long a hold can park the project checker

- Plan: docs/plans/issue-5927-bound-plain-mode-hold-plan.md
- Source issue: shubhodeep1/coding-workflows#5927
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5927-bound-plain-mode-hold   Final PR: #5940 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5944 review on its current head (a review round after the round-2 twin sync)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01KJT4qCwfkmhUAkLh3hXDbg   safety net none   hand-back none (the stage after /reclarify arms the wait)
- Last updated: 2026-10-02
- Last note: Review round 3 on PR #5944 (workflow round 1 on the synced head 8b4107a): this log now records the round-2 twin sync as done and the project as in progress instead of BLOCKED on it. Earlier: review round 2 on PR #5944 (head 31b27bc, workflow hand-off round 1): `_env_positive_float` now rejects non-finite values (`inf`, `nan`, `1e309`), so an infinite `CLAUDE_FIX_HOLD_MAX_HOURS` can no longer lift the bound; the same reader also stops an infinite claim lease and an `OverflowError` on an infinite hand-back cap. Tests extended, changelog headline says the limit is a configurable default, this log and the PR body no longer claim the reverted #5667 row edit. The fix changes the `check_in_status.py` twin, so the head is on hold again for a third `[claude-twin-sync]`; the project resumes on `/reclarify` after that push.

## Phases
1. [ ] Phase 1 — plain PR mode bounds a hold by age — PR #5944 open (round-2 twin sync done; review round 3 done); review rounds: 3; interventions: 0 — protected paths: `.claude/scripts/check_in_status.py`, `.claude/commands/implement-plan-claude.md` (twins only)
   - [x] `check_pr` in `workflow-templates/.claude/scripts/check_in_status.py`: a trusted hold on the current head waits only while younger than `CLAUDE_FIX_HOLD_MAX_HOURS` (`DEFAULT_FIX_HOLD_MAX_HOURS` `:161`; check `:275-288`); an older or undatable hold reports `state: blocked` (done) with `head_sha` and `claim`; module docstring (`:51-59`) and inline comment updated
   - [x] Command twin: done-waiting *PR* bullet **Held** names the bound and what a stale hold routes to (`workflow-templates/.claude/commands/implement-plan-claude.md:223`)
   - [x] Tests against the twin in `tests/test_check_in_status.py:1023-1127` (fresh / stale / env override / bad timestamp / newer hold / call budget / `--hand-back` unchanged / command twin); 18 new cases fail against the old script and pass against the twin
   - [x] Docs: CLAUDE.md §26.H (`workflow-templates/CLAUDE.md` is a symlink to it), `agents.md:1024,1067`, `README.md:92` (env var row) and `:1477`; `changelog.d/5927-bound-plain-mode-hold.md` (`security`); the edit to the #5667 fragment's "What outranks a hold" row was reverted in review round 1 (AD-8), so that fragment is unchanged
   - [x] `[claude-twin-sync]` copy of both twins into `.claude/` (human, after the twin-sync blocker) — landed as 49bb384 (2026-10-01, sha256 values verified, 523 passed / 1 skipped)
   - [x] Review round 1 (head 49bb384): `_parse_time` rejects a timestamp with no UTC offset as unreadable, so a timezone-less hold time hands back as blocked and a timezone-less claim time is not live instead of raising `TypeError` into `action: retry` (`workflow-templates/.claude/scripts/check_in_status.py:243-252`); tests `tests/test_check_in_status.py` (timezone-less cases in `test_hold_with_an_unreadable_time_is_handed_back`, new `test_claim_with_a_timezone_less_time_is_not_live`); #5667 fragment edit reverted (AD-8)
   - [x] `[claude-twin-sync]` copy of the round 1 `check_in_status.py` twin into `.claude/` (human, after the twin-sync blocker) — landed as 31b27bc (2026-10-01, human, Q2: A; sha256 c0be415c94653a11983af2d52314461f2486db4ca8ddef04e35c533436e47906 verified against the twin; 722 passed, 1 skipped reported)
   - [x] Review round 2 (head 31b27bc): `_env_positive_float` falls back to the default for non-finite values (`workflow-templates/.claude/scripts/check_in_status.py:107`, `:532-543`), covering `CLAUDE_FIX_HOLD_MAX_HOURS`, `CLAUDE_FIX_CLAIM_LEASE_HOURS`, and `CLAUDE_FIX_HAND_BACK_CAP`; tests `tests/test_check_in_status.py:1105` (`inf`, `+inf`, `Infinity`, `-inf`, `nan`, `1e309` added) and `:1117` (new `test_non_finite_env_numbers_fall_back_to_defaults`), 8 new cases fail against the old twin; `README.md:92` and the changelog fragment updated; log line for the reverted #5667 row corrected
   - [x] `[claude-twin-sync]` copy of the round 2 `check_in_status.py` twin into `.claude/` (human, after the twin-sync blocker) — landed as 8b4107a (2026-10-02, pushed by the owner's supervising session; sha256 4a12a9ad17b5ab4dd96db9f3d3b5b5f7ba3d28b5355fb40a6d6791f11f7ace20 verified equal to the twin)
   - [x] Review round 3 (head 8b4107a, workflow round 1): Status, Waiting on, Check-in, Last note, the phase row, and the round-2 sync item now reflect the synced state (no code change)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] How should plain PR mode stop a hold from parking the project checker indefinitely? — Picked: A — bound the hold by age: it waits only while younger than `CLAUDE_FIX_HOLD_MAX_HOURS` (default 24), then routes as blocked. Alternatives: B — let blocking labels outrank a hold again; C — require a matching `ai:claude-blocked` blocker on the source issue; D — trust holds only from `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`. Why: A ends the indefinite wait with no new API call and keeps #5667's fix for real twin-sync waits (§1, §5, §15). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What does a stale hold report? — Picked: A — `state: blocked` (done), routed to `hand_back` by the existing table. Alternatives: B — ignore the stale hold and route as if there were none. Why: B restarts a review-round stage every hour on a head still waiting for a twin sync; A uses the bounded blocked-PR intervention. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] Default limit for `CLAUDE_FIX_HOLD_MAX_HOURS`? — Picked: A — 24 hours. Alternatives: B — 72 hours; C — 6 hours. Why: matches the chain's 24-hour safety net and is well above the observed twin-sync waits. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Should `--hand-back` mode bound holds too? — Picked: A — no, plain mode only. Alternatives: B — bound both. Why: the finding is the project checker's plain mode; the §26.H cap hold is a documented "never expires on the same head" contract (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-10-01] Which hold's age counts? — Picked: A — the latest trusted hold on the current head, with a missing or unparseable time counted as stale. Alternatives: B — the earliest trusted hold on the head. Why: the latest claim decides the state, A needs no new parsing, and fail-closed keeps the bound. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-01] How does the changelog record the change? — Picked: A — a new `security` fragment and a one-row correction to the #5667 fragment. Alternatives: B — only the new fragment. Why: the #5667 row would otherwise state the old rule. Applied in: phase 1 PR; the #5667 row edit was reverted in review round 1 (see AD-8). Status: pending review

- AD-7 [phase 1/1, 2026-10-01] The base project (#5667, its AD-7) deferred a wording fix ("until a push moves the head" omits "or a newer trusted claim on the same head") in the `check_in_status.py` docstring and inline comment and the command's **Held** bullet to the next `.claude/` edit of these files. This phase edits exactly those lines. Include the fix? — Picked: A — yes, in the same twin edits. Alternatives: B — leave it for later. Why: the lines are rewritten here anyway, so B would leave known-stale text in the touched files at no saving (§12.B stale docs). Applied in: phase 1 PR. Status: pending review
- AD-8 [phase 1/1 — review round 1, 2026-10-01] Three reviewers flagged the edit to `changelog.d/5667-checker-waits-on-held-head.md` (AD-6 A) as a §20.B violation: if #5684 merges and a release assembles and deletes that fragment before this project lands, the edit conflicts with the deletion or restores it. Keep the edit or revert it? — Picked: A — revert it; this PR's own `changelog.d/5927-bound-plain-mode-hold.md` already states the bound. Alternatives: B — keep the one-row edit (AD-6 A). Why: one fragment per PR is what makes changelog conflicts impossible (§20.B), and the new fragment carries the correction (§5). Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:intervention] A helper that reads a positive number from the environment must reject non-finite values with `math.isfinite`: `float("inf")` and `float("1e309")` pass a `value > 0` check, turning a time bound or lease into "never expires", and `int()` of the result raises `OverflowError` (files: .claude/scripts/check_in_status.py, .claude/scripts/claude_fix_claim.py)
- [source:intervention] A helper that parses API timestamps with `datetime.fromisoformat` must reject a result with no UTC offset as unreadable (raise `ValueError`): subtracting a naive time from an aware `now` raises `TypeError`, which `except ValueError` guards miss, so the fail-closed path never runs (files: .claude/scripts/check_in_status.py)

## Notes
- Review round 2 (2026-10-01): stage session_0191RqNLS2GFrxKvB14tsaLx resumed the project at `Status: IN_PROGRESS` after the round 1 twin sync (31b27bc), under checker session_01KJT4qCwfkmhUAkLh3hXDbg with safety net trig_01CxKsQe5hKanxDbLVDatyq4 and hand-back trig_012Lb4QNtPFf5ny9NFS6wJZT (both deleted at the stage's start). Its fix changes the `check_in_status.py` twin, so the project is blocked again on a third `[claude-twin-sync]`.
- Review round 1 (2026-10-01): rejected the finding that a re-posted hold restarts the 24-hour limit. The plan accepted it as a risk (AD-5, Risks): only the PR's author or `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` can post a trusted hold, and that identity can already push, close, or merge the PR. Each reset is a new visible comment, and one stray or forged marker no longer parks the project.
- Security pass: skip — `security_pass_skip.py` verified #5927 as an automation-produced `ai:security` issue.
- Issue base `claude/implement-plan-issue-5667-checker-waits-on-held-head` is the head of open draft PR #5684 into `main`; check at every stage whether it merged (Issue Mode, "A base branch that merges moves the project").
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)

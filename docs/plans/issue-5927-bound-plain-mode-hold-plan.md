# Bound how long a hold can park the project checker

Source issue: shubhodeep1/coding-workflows#5927 (https://github.com/shubhodeep1/coding-workflows/issues/5927)
Base branch: claude/implement-plan-issue-5667-checker-waits-on-held-head
Security pass: skip (ai:security: automation-produced issue)

## Summary

Issue #5667 (this plan's base branch, final PR #5684) made the `/implement-plan-claude` project checker's plain `--pr` mode report `state: held` / `action: wait` for a trusted `hold` claim on the current head of a `claude/implement-plan-*` PR, ahead of blocking labels, hand-offs, conflicts, and checks. The security audit (#5927, `.claude/scripts/check_in_status.py:263`) points out that this wait has no end: a hold marker posted by the PR's author without a real blocker parks the checker for good, even when the PR carries `ai:needs-human` or `ai:review-blocked`, so the blocked-stage escalation never happens.

This plan bounds the plain-mode hold. A trusted hold on the current head keeps the checker waiting only while it is younger than `CLAUDE_FIX_HOLD_MAX_HOURS` (new env var, default 24). An older hold is stale: plain mode reports `state: blocked` (done), which the existing routing table already sends to `action: hand_back`, so the PR goes through the normal blocked-PR handling (the step 7 intervention, capped at 3 per PR, ending in a `Status: BLOCKED` ask).

## Context

- `check_pr` on the base branch (`.claude/scripts/check_in_status.py:241-301`, twin `workflow-templates/.claude/scripts/check_in_status.py`, byte-identical) reads the PR's comments once for a `claude/implement-plan-` head, runs `read_fix_claims(..., trusted_logins=_fix_claim_trusted_logins(pr))`, and returns `held` at `:265-267` whenever the latest trusted claim on the head is a `hold`. Only merged / closed come first.
- `read_fix_claims` (`:535-588`) already returns the deciding claim's `at` (the comment's GitHub `created_at`, which the poster cannot set) and never expires a `hold` while the head stays the same.
- `_fix_claim_trusted_logins` (`:513-532`) trusts the PR's author and `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`. Claude stage sessions post their claims as the PR's author through the session proxy, which is the same GitHub identity a human using that account has, so a script cannot tell an automation hold from a hand-written one. That is why this plan bounds the hold in time instead of trying to scope it by identity.
- Legitimate holds in plain mode come from twin-first stages (`implement-plan-claude.md` step 4) waiting for a `[claude-twin-sync]` push; #5667's evidence shows those waits lasting minutes to about two hours. Every resolution of a twin-sync blocker pushes, which moves the head and lifts the hold.
- Routing (`route_verdict`, `CHECKER_ROUTE_TABLE`): plain mode `blocked` → `hand_back`; the checker hands the PR to the stage that armed the wait, or starts a `… — blocked PR` stage when there is no hand-back trigger. The Blocked intervention claims the head (`--kind blocked`), which as the newest trusted claim replaces the hold, and is capped at 3 per PR.
- `--hand-back` mode (`check_pr_hand_back`, the CLAUDE.md §26 checker and the catch-all sweep) honours a hold as the §26.H cap's "never expires on the same head" contract. The audit finding is about plain mode.

## Goals

- Plain `--pr` mode (not `--terminal-only`, not `--hand-back`) on an open `claude/implement-plan-*` PR whose current head has a trusted hold:
  - hold age < `CLAUDE_FIX_HOLD_MAX_HOURS` → unchanged: `{"done": false, "state": "held", "head_sha", "claim", "reason"}`, `action: wait`;
  - hold age ≥ the limit, or an age that cannot be computed → `{"done": true, "state": "blocked", "head_sha", "claim", "reason"}`, `action: hand_back`; the reason names the hold's claimant, its age, the limit, and any blocking labels.
- `CLAUDE_FIX_HOLD_MAX_HOURS` defaults to 24 when unset, empty, non-numeric, or not positive (`_env_positive_float`, as `CLAUDE_FIX_CLAIM_LEASE_HOURS` does).
- No new GitHub API call: the age comes from the claim `read_fix_claims` already returns.
- Docs say the plain-mode hold is bounded: the command twin's "done waiting" *PR* bullet, CLAUDE.md §26.H (both copies), `agents.md`, `README.md`, the module docstring, and the #5667 changelog fragment's "What outranks a hold" row.

## Non-goals

- Scoping holds by identity ("accept holds only from scoped automation"): not possible while Claude sessions and humans post as the same account (AD-1).
- Automating the twin sync (the recommendation's second sentence): that is #4785.
- Changing `--hand-back` mode, `--terminal-only` mode, `read_fix_claims`, `claude_fix_claim.py`, the routing table, or the §26.H cap hold (AD-4).
- Letting blocking labels outrank a fresh hold again (AD-1 alternative B).

## Constraints

- §6: no identifier renamed or removed. New identifiers: env var `CLAUDE_FIX_HOLD_MAX_HOURS`, constant `DEFAULT_FIX_HOLD_MAX_HOURS` (both unique in the repo; `COMPREHENSIVE_PROMOTION_HOLD_MAX_SECS` is unrelated). `state: blocked` is an existing plain-mode value.
- §4: the new env var has a default (24).
- §15: no new API call.
- §28.C twin-first: `.claude/scripts/check_in_status.py` and `.claude/commands/implement-plan-claude.md` change only through their `workflow-templates/.claude/` twins plus a `[claude-twin-sync]` commit; tests run against the twin.
- §9: tabs, as the files already use. §20: one changelog fragment. §7: README / agents.md updated.

## Approach

In `check_pr`, where the hold is found (`hold_claim["state"] == "held"`), compute `hold_age = _hours_since(hold_claim.get("at"), now)` and the limit `_env_positive_float("CLAUDE_FIX_HOLD_MAX_HOURS", DEFAULT_FIX_HOLD_MAX_HOURS)`. A fresh hold returns `held` exactly as today. A stale or undatable hold returns `blocked` with `head_sha` and `claim`, before the label check, so the labels only add to its reason.

Alternatives considered: see AD-1 and AD-2.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for one standalone issue.

1. **Phase 1 — plain PR mode bounds a hold by age.** protected paths: `.claude/scripts/check_in_status.py`, `.claude/commands/implement-plan-claude.md` (twin-first: edit the `workflow-templates/.claude/` twins; the root copies arrive by `[claude-twin-sync]`).
   - Files: the two twins, `tests/test_check_in_status.py`, `CLAUDE.md`, `workflow-templates/CLAUDE.md`, `agents.md`, `README.md`, `changelog.d/5667-checker-waits-on-held-head.md`, `changelog.d/5927-bound-plain-mode-hold.md` [new].
   - Done when: the new tests pass against the twin; `tests/test_check_in_status.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_claude_pr_sweep.py`, `tests/test_check_in_session_targeting.py`, `tests/test_implement_plan_claude_command.py`, `tests/test_session_titles.py`, and `tests/test_claude_md_section_numbers.py` pass except the template-parity checks, which stay red until the twin sync; ruff is clean on the twin.
   - Rollback: revert the phase PR, or set `CLAUDE_FIX_HOLD_MAX_HOURS` very high in the checker environment.

## Implementation Steps

1. `workflow-templates/.claude/scripts/check_in_status.py`:
   - Add `DEFAULT_FIX_HOLD_MAX_HOURS = 24.0` beside `DEFAULT_FIX_CLAIM_LEASE_HOURS`.
   - `check_pr`: inside the `hold_claim["state"] == "held"` branch, compute the age and limit; fresh → the current `held` return; stale or undatable → `{"done": True, "state": "blocked", "head_sha": head_sha, "claim": hold_claim, "reason": …}`.
   - Module docstring "Done waiting" Claude-fixer bullet: the hold waits only while younger than `CLAUDE_FIX_HOLD_MAX_HOURS` (default 24); an older one reports `blocked` (issue #5927).
2. `workflow-templates/.claude/commands/implement-plan-claude.md`, Check-in Loop → "What counts as done waiting", *PR* bullet, **Held**: add the bound and what the stale hold routes to.
3. `tests/test_check_in_status.py` (against `twin_checker`): see Tests; update `test_command_twin_documents_the_held_wait` for the new wording.
4. `CLAUDE.md` and `workflow-templates/CLAUDE.md` §26.H claims bullet (the #5667 sentence): add the bound.
5. `agents.md` (claims bullet and verdict-helper bullet) and `README.md` (claims paragraph): the same rule.
6. `changelog.d/5927-bound-plain-mode-hold.md` (`security`) and the #5667 fragment's "What outranks a hold" row.

## Files & Modules

- `workflow-templates/.claude/scripts/check_in_status.py` (twin of `.claude/scripts/check_in_status.py`)
- `workflow-templates/.claude/commands/implement-plan-claude.md` (twin of `.claude/commands/implement-plan-claude.md`)
- `tests/test_check_in_status.py`
- `CLAUDE.md`, `workflow-templates/CLAUDE.md`
- `agents.md`, `README.md`
- `changelog.d/5667-checker-waits-on-held-head.md`, `changelog.d/5927-bound-plain-mode-hold.md` [new]
- `docs/implement-plan/issue-5927-bound-plain-mode-hold.md` [new] (progress log)

## Data Model / Index Changes

None.

## Tests

In `tests/test_check_in_status.py`, against the twin module:
- A hold younger than the limit plus a blocking label → `held` / `wait` (existing tests use a 2-hour-old hold and keep passing).
- A hold 24 hours or older, with and without a blocking label, a hand-off, or a conflict → `done: true`, `state: blocked`, `action: hand_back`, `head_sha` and `claim` set, and the reason names the age and the limit.
- `CLAUDE_FIX_HOLD_MAX_HOURS=48` keeps a 30-hour-old hold `held`; `0`, `-1`, and `abc` fall back to 24.
- A hold whose `created_at` is missing or unparseable → `blocked`.
- Call budget: a stale hold costs exactly `pulls/N` plus one comment listing.
- `--hand-back` mode still reports `held` for a stale hold (scope guard, AD-4).

## Risks & Mitigations

- A legitimate twin-sync wait longer than 24 hours is handed back as blocked. ACCEPTED: the blocked intervention re-raises the blocker (its claim replaces the hold, and a twin-first stage posts a fresh hold and blocker again), at most 3 times per PR before the project stops at `Status: BLOCKED`. That turns an unbounded silent wait into a bounded, visible one, which is the finding's ask.
- A trusted poster can re-post a hold to restart the 24 hours. ACCEPTED: each re-post is a new visible comment by the PR's own trusted identity; one forged marker no longer parks the project for good (AD-5).
- The fix has no effect until the `[claude-twin-sync]` copy lands and the base project merges to `main`. ACCEPTED: twin-first process.

## Rollout

Ships with this project's final PR into the base branch `claude/implement-plan-issue-5667-checker-waits-on-held-head`, then with #5684 into `main`. Checker sessions pick it up on their next checkout; consumer repos on the next `@stable` sync. Operators can raise `CLAUDE_FIX_HOLD_MAX_HOURS` in the checker environment.

## Auto-decisions

- AD-1 [plan, 2026-10-01] How should plain PR mode stop a hold from parking the project checker indefinitely? — Picked: A — bound the hold by age: it waits only while younger than `CLAUDE_FIX_HOLD_MAX_HOURS` (default 24), then routes as blocked. Alternatives: B — let blocking labels outrank a hold again (reverts #5667's AD-2, so a held twin-sync head would be intervened on at once); C — require a matching `ai:claude-blocked` blocker on the source issue (plain mode does not know the issue, and the blocker is free text by the same identity); D — trust holds only from `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` (stage sessions post as the PR author, so every real hold would be ignored). Why: A ends the indefinite wait with no new API call and keeps #5667's fix for real twin-sync waits (§1, §5, §15). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What does a stale hold report? — Picked: A — `state: blocked` (done), routed to `hand_back` by the existing table. Alternatives: B — ignore the stale hold and route as if there were none. Why: B restarts a review-round stage every hour on a head still waiting for a twin sync (the #5667 loop); A uses the bounded blocked-PR intervention (3 per PR) and needs no routing change. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] Default limit for `CLAUDE_FIX_HOLD_MAX_HOURS`? — Picked: A — 24 hours. Alternatives: B — 72 hours; C — 6 hours (the stuck window). Why: matches the chain's 24-hour safety net and is well above the observed twin-sync waits; C would hand back ordinary overnight waits. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Should `--hand-back` mode (§26 checker, catch-all sweep) bound holds too? — Picked: A — no, plain mode only. Alternatives: B — bound both. Why: the finding is the project checker's plain mode; the §26.H cap hold is a documented "never expires on the same head" contract, and bounding it would start a fresh Opus fixer for every capped PR each day (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-10-01] Which hold's age counts? — Picked: A — the latest trusted hold on the current head (the claim `read_fix_claims` already returns; its `created_at` is set by GitHub), with a missing or unparseable time counted as stale. Alternatives: B — the earliest trusted hold on the head. Why: the latest claim is the one that decides the state, A needs no new parsing, and fail-closed on a bad time keeps the bound (§1, §5). Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-01] How does the changelog record the change? — Picked: A — a new `changelog.d/5927-bound-plain-mode-hold.md` (`security`) and a one-row correction to the #5667 fragment, which is still unassembled on the base branch. Alternatives: B — only the new fragment. Why: the #5667 fragment's "merged or closed only" row would otherwise state the old rule. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Issue base: the issue's `Integration branch:` line names `claude/implement-plan-issue-5667-checker-waits-on-held-head` (open final PR #5684 into `main`).

## References

- Issue #5927; base project #5667 (final PR #5684, phase PR #5716); #4622 (claim trust), #4785 (twin sync).
- `.claude/scripts/check_in_status.py`, `.claude/scripts/claude_fix_claim.py`, `tests/test_check_in_status.py`.

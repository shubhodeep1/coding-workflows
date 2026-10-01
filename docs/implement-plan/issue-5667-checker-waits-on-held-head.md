# Implement-Plan Log — Project checker waits on a held head instead of starting another review round

- Plan: docs/plans/issue-5667-checker-waits-on-held-head-plan.md
- Source issue: shubhodeep1/coding-workflows#5667
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5667-checker-waits-on-held-head   Final PR: #5684 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5716
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_018LkNbzAgey2wPnVX3E4HT7 (reused)   safety net and hand-back (in the round 3 stage report, session_01JoMpSWi4VrkutkixEM4jXS)
- Last updated: 2026-10-01
- Last note: Review round 3 (session_01JoMpSWi4VrkutkixEM4jXS; the workflow numbered it round 1 on head `4a42d08`): all six ledger entries (2 consensus findings, 1 task gap) say the PR description's "no API call is added" missed the blocking-label, no-hold path. Valid; the PR description now gives the per-path cost the changelog already stated. No code change; this log commit moves the head.

## Phases
1. [ ] Phase 1 — plain PR mode honours a hold on a Claude-fixer head   — protected paths: `.claude/scripts/check_in_status.py`, `.claude/commands/implement-plan-claude.md` (twins only) — PR #5716 open (waiting); review rounds: 3; interventions: 0
   - [x] `check_pr` reads trusted claims once for a `claude/implement-plan-` head and returns `held` before labels / hand-off / conflict / checks (`workflow-templates/.claude/scripts/check_in_status.py:250-278`), reusing the listing for `_check_claude_fixer_pr` (`:276`); docstring rule and API budget updated
   - [x] Command twin: done-waiting *PR* bullet adds **Held** (`workflow-templates/.claude/commands/implement-plan-claude.md:223`); twin-first later-stage bullet puts the hold on the pushed head in the same step as the blocker (`:38`)
   - [x] Tests against the twin: `tests/test_check_in_status.py:861-1003` (`test_held_head_with_a_later_handoff_waits_instead_of_a_review_round` …); the 5 new core tests fail against the old script and pass against the twin
   - [x] Docs: CLAUDE.md §26.H (`CLAUDE.md:2124`), `agents.md:1020` and the verdict-helper bullet, `README.md:1470`; `changelog.d/5667-checker-waits-on-held-head.md`
   - [x] `[claude-twin-sync]` copy of both twins into `.claude/` (`d67f5fb`; template-parity tests green)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How should the project checker detect an unanswered blocker on a PR? — Picked: A — honour the trusted `hold` claim on the current head in plain `--pr` mode. Alternatives: B — also parse `<!-- ai:claude-blocked:v1 -->` comments on the source issue / final PR and compare them with later replies; C — only B. Why: every stage in the evidence posted the hold, a hold is trusted and machine-readable, and A needs no new API call. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Should a hold outrank a blocking label in plain mode? — Picked: A — yes, only merged / closed outrank a hold (as in `--hand-back`). Alternatives: B — labels still win and hand the PR back. Why: a hand-back on a held head would make the blocked stage intervene on a head that waits for a human. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Which heads does the plain-mode hold check cover? — Picked: A — only `claude/implement-plan-` heads. Alternatives: B — every `claude/*` head. Why: non-fixer PRs never read comments today, and plain mode is only used by the project checker. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Should this issue also stop the project checker from re-arming after it starts a stage? — Picked: A — no; record it as a finding. Alternatives: B — add a deterministic guard in this phase. Why: §5 minimal change; the hold fix removes the held-head case, and a re-arm guard needs its own design. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] Where do the tests for the new behaviour live while `.claude/` is not synced? — Picked: A — in `tests/test_check_in_status.py`, against the twin loaded as a second module. Alternatives: B — a new test file wired into `ci.yml`. Why: the issue names this file, and A needs no workflow edit. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1 — review round 2, 2026-10-01] Review round 2 (head `02d8a56`) has one consensus finding: the failed `review / codex-agent` check, which the OpenRouter credit outage caused, not this PR. `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is unset, so no verdict can be posted. How does the round close? — Picked: A — reject the finding in the PR reply and push a new head: main merged into the phase branch through the synced project branch, with this log update, which starts a fresh review round and fresh checks. Alternatives: B — re-dispatch `review_autofix.yml` on the same head; C — stop BLOCKED until a verdict bot exists. Why: the operator's Q1: A answer says not to dispatch review again, the base merge is due anyway, and AD-12 / AD-15 of issue-4886 closed the same state this way. Applied in: phase 1 PR (merge commit). Status: pending review

## Lessons
- [source:intervention] A review run's stale failed check stays on the PR head after the outage that caused it ends, and the next review round hands it off as a finding; a new head (a base merge) clears it, while a same-head re-review cannot. (files: .github/workflows/review_autofix.yml)
- [source:plan-deviation] When a status helper moves a read earlier so a new check can outrank an existing early return, the early-return path pays that read too; state the per-path API cost in the docstring and changelog instead of claiming zero new calls. (files: .claude/scripts/check_in_status.py, changelog.d/5667-checker-waits-on-held-head.md)
- [source:intervention] When a review round corrects a claim in the changelog or docstring (such as an API-cost row), correct the same claim in the PR description in that round; the reviewer panel reads the description and flags the stale copy in the next round. (files: changelog.d/5667-checker-waits-on-held-head.md)

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- Security pass: run (`security_pass_skip.py`: no skip label).
- 2026-09-30: phase PR #5716 opened; `hold` claim posted on its head and the twin-sync blocker on #5667 (`ai:claude-blocked`). Local run of the 61 related test files: 2903 passed; 6 template-parity failures (until the sync) and 1 environment failure (`gawk` missing in the container, `test_review_pipeline_integration_chain_module_runs_clean`).
- Finding (AD-4): project checker `session_014L62sdmdQ6xVjdSYUu2nWq` (issue-5068) reports "created review-round stage, re-armed 60m check-in" after starting a stage, although its prompt's step 5 says to end the turn without re-arming.
- 2026-09-30: review round 1 on `d67f5fb` — 1 consensus finding (5 reviewers, NIT): the changelog's "0 extra API calls" missed the blocked-label, no-hold `claude/implement-plan-*` path, which now reads comments once to rule out a hold (AD-2). Fixed in the changelog; `test_blocked_fixer_head_without_a_hold_reads_comments_once` pins the budget. The plan's goal "no new GitHub API call for a Claude-fixer PR" holds for every fixer path except that one, by design of AD-2.
- 2026-09-30 21:01Z: blocked PR (session_01WfXaPVd6fZWZ2Wx5TS9dWx): every review run on `02d8a56` failed in `Run reviewer models` with OpenRouter `Insufficient credits`, and the identical-failure cap labelled #5716 `ai:review-blocked`. No intervention commit (interventions: 0); `hold` claim on `02d8a56`; Q1 asked on #5667 (comment 5919662713).
- 2026-10-01: Q1: A answered (comment 5922471506): credits added, `ai:review-blocked` removed at 23:39Z, the 00:00Z sweep re-reviewed `02d8a56` (run 36794098183) and handed off review round 2 at 00:33:42Z. This stage posted a review claim on `02d8a56` (lifting the hold), synced the project branch with main (6050c5e), and merged it into the phase branch. Local run of 9 related test files: 614 passed, 1 skipped.
- 2026-10-01: review round 3 on `4a42d08` (session_01JoMpSWi4VrkutkixEM4jXS): 2 consensus findings (low) and 1 task gap, all about the PR description's "so no API call is added", which missed the `claude/implement-plan-*` blocking-label, no-hold path (one comment listing per check-in, AD-2). The code (`workflow-templates/.claude/scripts/check_in_status.py:255-271`) and the changelog row were already right; the PR description now lists the cost per path. Project branch synced with main (`27fd288`).

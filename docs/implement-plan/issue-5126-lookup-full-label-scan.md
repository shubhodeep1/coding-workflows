# Implement-Plan Log — Never report a permission-prompt lookup as not found because a fixed window ran out

- Plan: docs/plans/issue-5126-lookup-full-label-scan-plan.md
- Source issue: shubhodeep1/coding-workflows#5126 (https://github.com/shubhodeep1/coding-workflows/issues/5126)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4755-report-blocking-permission-prompts
- Project branch: claude/implement-plan-issue-5126-lookup-full-label-scan   Final PR: #5160 (draft)
- Status: IN_PROGRESS
- Stage: conformance 1/3 — review round
- Activation: not started
- Waiting on: PR #5547
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01KFpX9wDF5pwN8i2yd4ctth   safety net trig_01H28YAFoLwTLDMQiQ2xQz89   hand-back trig_015KzS2hPv8LGQHW1rukhBoB
- Last updated: 2026-09-30
- Last note: conformance 1/3 review round 1 on PR #5547 (session_01XaYsncubwiatBaMhdjKKvN): twin sync 3ddd20f landed (Q3: A, issue comment 5907867671); fixed 2 valid findings (a lookup test that reached live GitHub for comments; the lookup comments-read budget in CLAUDE.md §23.I and agents.md), rejected 3; wait re-armed on PR #5547 (next stage conformance 2/3).

## Phases
1. [x] Phase 1 — lookup checks every candidate   — protected paths: .claude/scripts/permission_prompts.py (twin: workflow-templates/.claude/scripts/permission_prompts.py)
   - `_lookup_candidates`: paginated search (`_gh_api_paginated_object`), `ReadError` on `incomplete_results: true`, the full labelled list in the fallback
   - `lookup`: every candidate, no comments read when `comments` is 0
   - module docstring and `LOOKUP_MAX_HITS` comment
   - tests/test_permission_prompts.py: lookup tests against the twin (G1–G5)
   - agents.md lookup bullet; changelog.d/5126-lookup-full-label-scan.md
   - Done: lookup tests pass against the twin; only test_template_parity red until the operator's [claude-twin-sync]; ruff clean
   - PR #5165 merged 2026-09-29 (into the project branch, before the twin sync; the #5316 race); review rounds: 0; interventions: 0
   - Twin sync: recovery PR #5433 (Q1: A, issue comment 5903809630) merged 2026-09-30 as 867a9b5 (Q2: A, issue comment 5905191883); review rounds: 1 (1 confidence-1 NIT rejected; no verdict bot, #4648)

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Correctness: CONCERNS) — fix PR #5547 (pre-security); twin sync 3ddd20f (Q3: A, issue comment 5907867671); review rounds: 1 (2 fixed, 3 rejected)

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should `lookup` stop missing reports outside its window? — Picked: A — check every paginated candidate (search hits, or every labelled issue in the fallback), skipping comment reads for issues with `comments: 0`. Alternatives: B — a repository-scoped session-to-report index kept by `report-now`; C — keep the window and exit 2 when it is exhausted. Why: smallest change (§5) that removes the false negative using existing paginated helpers (§15). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What should `lookup` do when the search reports `incomplete_results: true`? — Picked: A — raise `ReadError`, so `lookup` exits 2 with an `error`. Alternatives: B — scan the partial hits and return `found: false` when none matches; C — fall back to the labelled-issue scan. Why: B is the false negative the issue forbids; C misses reports outside coding-workflows. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] What happens to `LOOKUP_MAX_HITS`, which no longer caps anything? — Picked: A — keep it defined with a comment that `lookup` no longer uses it. Alternatives: B — delete it; C — reuse it as the page size. Why: §6 forbids removing or repurposing an identifier. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How do the tests cover a change made only in the `workflow-templates/.claude/` twin? — Picked: A — the lookup tests load the twin and share the main module's `check_in_status`. Alternatives: B — test the `.claude/` copy and leave the tests red until the sync. Why: the interim twin-first rule (CLAUDE.md §28.C) says tests read the twin. Applied in: phase 1 PR. Status: pending review

- AD-5 [conformance 1/3, 2026-09-30] How should `lookup` stop missing `incomplete_results: true` on a search page after the first? — Picked: A — keep the flag from any page in the shared `check_in_status._gh_api_paginated_object`. Alternatives: B — page the search inside `_lookup_candidates`; C — leave it, since one session id rarely has over 100 hits. Why: one guarded line that keeps the plan's §15 rule to reuse the shared paginated helper; check-run pages carry no such key, so the checker is unchanged; C keeps a G3 false negative in an A09 fix. Applied in: PR #5547. Status: pending review
- AD-6 [conformance 1/3, 2026-09-30] Which report should `lookup` return when several issues hold one for the session? — Picked: A — the newest by creation time, stopping at the first candidate last updated before it. Alternatives: B — scan every candidate and keep the newest; C — keep the first match (pre-existing since #4755) and document it. Why: the module docstring promises the newest marker and the poller needs the current prompt; A adds no issue reads in the common case, B always costs about one read per labelled issue, C leaves a stale command in the alert. Applied in: PR #5547. Status: pending review

## Lessons
- [source:intervention] A test that stubs one GitHub read helper must stub every read the code path can make (or give fixtures the fields that skip them, such as `comments: 0`), or it silently reaches live GitHub and passes only where `gh` is authenticated. (files: tests/test_permission_prompts.py)
- [source:conformance] A paginated read that merges pages must carry per-page status flags (such as `incomplete_results`) from every page, not only the first. (files: .claude/scripts/check_in_status.py)
- [source:conformance] A lookup that scans candidates in `updated_at` order must pick the newest match by its own timestamp; an issue bumped by an unrelated comment otherwise wins. (files: .claude/scripts/permission_prompts.py)
- [source:security] A fallback that replaces a refused search must not keep the search's result cap: scan every page of the repository-scoped list, or answer "unknown", never "not found". (files: .claude/scripts/permission_prompts.py)

## Notes
- 2026-09-30 conformance 1/3 review round 1 on PR #5547 (head 3ddd20f, ledger 03189cbe…bb59): fixed F1 `test_lookup_returns_the_exact_hostile_command` read comments from live GitHub (fixture now `comments: 0`, `gh_api_list` stubbed to fail; reproduced with a failing `gh` stub) and F4 the budget in CLAUDE.md §23.I and agents.md now says 1 comments read per 100 comments of each checked hit (the module docstring already did). Rejected: F2 a comments-read `ReadError` before the body fallback (exit 2 is the fail-closed contract; a body-only answer could be an older prompt, AD-2/AD-6); F3 body match timed by the issue's `created_at` (reports are only written into a body when `report-now` opens the issue, never by an edit); deepseek's zone-less `updated_at` (efficiency only, GitHub always sends UTC `Z` times). No `.claude/` file changed this round, so no twin sync.
- 2026-09-30: project branch synced with its base as ba0fc9e, bringing in the #5125 and #5127 fixes; up to date with the base at the conformance 1/3 stage (base 78ab4df, not merged).
- Conformance 1/3 findings (all EVIDENCE-BASED CONCERNs, reproduced by tests that failed before the fix): F1 `_gh_api_paginated_object` kept only page 1's fields, so a later page's `incomplete_results: true` gave `found: false` (G3); F2 `lookup` returned the first match in updated order, so an issue bumped by another session's comment returned an older prompt (pre-existing since #4755, the docstring promised the newest); F3 docstring, agents.md and the changelog said an incomplete search exits 2 only "without the report", but it exits 2 whatever the partial hits hold (plan risk 2, accepted); F4 CLAUDE.md §23.I's helper table still gave the old `lookup` budget ("at most 3").
- Protected-path approval for the conformance fix: phase 1's recorded twin-first approval applies (a later stage whose push changes `.claude/**` edits only the twins and posts the hold claim and twin-sync blocker).
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine `dispatch shubhodeep1/coding-workflows#5126: deliver` in session_01FNYvhuMREc42dsu1w1q3r6 (Auto mode, claude-opus-5-5).
- Security pass: skip — `security_pass_skip.py` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- Issue progress comment id 5890537114.
- The base branch's own project (#4755) waits on this issue (with #5124, #5125, #5127) as a security cycle 1 follow-up; it needs this issue closed and labelled `ai:merged` once the final PR merges into the base branch.
- Phase 1 evidence (2026-09-29): tests/test_permission_prompts.py 107 passed, 1 failed (test_template_parity, expected before the sync); overlay with the twin copied into .claude/: 469 passed, 1 skipped across the permission-prompt, check-in, command, guardrail, stale-routine, dispatch, edit-comment and hand-back suites; changelog suites 47 passed; ruff clean. Live twin lookup in this web session: search refused, fallback scanned 38 labelled issues in 40 reads, exit 0.
- Twin-sync blocker (stop 2): copy workflow-templates/.claude/scripts/permission_prompts.py → .claude/scripts/permission_prompts.py (sha256 29bc5735af1d85ec49023d1d02cc25999811eb68554f3ee59f83cf3fc589e77a) as a [claude-twin-sync] commit on claude/implement-plan-issue-5126-lookup-full-label-scan-phase-1, run tests/test_permission_prompts.py, push (lifts the hold), comment /reclarify on #5126. The resumed stage arms the wait on PR #5165 (step 7) and never re-implements the phase.
- Sibling follow-ups #5124, #5125, #5127 change the same file on their own project branches; whichever merges into the base second resolves the conflict in its own review round.
- The session started without the GitHub MCP tools and without `gh`: `gh` came from running `.claude/hooks/session-start.sh` (the repo was attached after the session started, so the SessionStart hook had not run); issue and PR writes go through `gh api` REST.

# Implement-Plan Log — Chain security follow-ups that change the same file

- Plan: docs/plans/issue-4934-chain-same-file-security-followups-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#4934   Base branch: main   Progress comment: 5883200031
- Project branch: claude/implement-plan-issue-4934-chain-same-file-security-followups   Final PR: #4996 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3 — review round
- Activation: not started
- Waiting on: PR #5055 (conformance fix 1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01WHRV2iQsVtX3tMPC7D9ENh (reused)   safety net and hand-back: see the stage report and the issue progress comment
- Last updated: 2026-09-29
- Last note: PR #5055 review round 1: one valid finding fixed (the rate-limit pattern now includes HTTP 429, matching `_is_gh_rate_limit`), four rejected with reasons on the PR (341 tests, ruff clean)

## Phases
1. [x] Phase 1 — chain same-file security follow-ups and hold dependent queue items   — PR #5002 merged 2026-09-29 (merged by the operator after the review-round-1 block, answer C on #4934); review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT (Correctness: CONCERNS) — fix PR #5055 (pre-security); review rounds: 1

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the dependency travel from the issue to the pickup? — Picked: A — the intake parses `Depends on:` from the target issue it already reads and records it as an optional `depends_on` key in the bound queue payload. Alternatives: B — the pickup reads each target issue body every wake; C — the `/implement-issue-claude` session holds itself. Why: zero new reads at intake, and the binding covers the key. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What does the pickup do when a dependency cannot be read? — Picked: A — HTTP 403/404 dispatches without the hold and notes it; any other failure holds the item this wake. Alternatives: B — always hold; C — always dispatch. Why: a hold that can never clear must not strand work, and a transient error must not skip the gate. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How does the queue watchdog treat held items? — Picked: A — judge a dependent item only when every dependency is readable (open → not stale; closed without `ai:merged` → flagged at once; all merged → age from the latest close). Alternatives: B — unchanged; C — skip dependent items entirely. Why: only alert on what can be proven. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] With three or more follow-ups on one file, what does each depend on? — Picked: A — the previous follow-up for that file (a chain). Alternatives: B — all on the first. Why: only a chain serialises every pair. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Should a new follow-up depend on an open follow-up from an earlier audit run? — Picked: A — no, same run only. Alternatives: B — also chain to earlier runs' open follow-ups. Why: the chain re-dispatches the audit only after earlier follow-ups resolve (§5). Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] How are held items reported without editing `.claude/**`? — Picked: A — as `ignored` entries with `held: …` reasons. Alternatives: B — a new `held` key plus a pickup command edit (protected path, §28.C). Why: fits the existing contract. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Should the Codex route honour `Depends on:` too? — Picked: A — no, Claude route only. Alternatives: B — add the hold to clarify / the poller. Why: the issue scopes the hold to the Claude intake and pickup (§5). Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-29] Which `Depends on:` lines count? — Picked: A — every same-repository `Depends on: #N` line, self-references dropped, deduplicated, at most 10. Alternatives: B — only the first line; C — also `owner/repo#N`. Why: never silently ignore a stated dependency; cross-repo reads are out of budget. Applied in: phase 1 PR. Status: pending review
- AD-9 [conformance 1/3, 2026-09-29] Should a dependency read that fails with a rate-limit HTTP 403 count as inaccessible (start the item without waiting)? — Picked: A — no: a rate-limit or abuse-detection 403 is transient, so it is `unavailable` and holds the item for that wake; permission 403s and 404s stay `inaccessible`. Alternatives: B — keep every 403 inaccessible, as documented in phase 1. Why: AD-2's own rule is that a transient error must not skip the gate, and a skipped gate is the parallel-conflict failure #4934 exists to prevent. Applied in: conformance fix 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] The security audit reorders findings before filing, so tests of per-finding follow-up output must read the filing order from the created issues, not assume the input order. (files: scripts/security_audit.sh, tests/test_security_audit_workflow_contract.py)
- [source:conformance] GitHub answers primary and secondary rate limits with HTTP 403, so code that reads a 403 as "can never succeed" must first exclude rate-limit messages, or a transient limit silently skips a gate. (files: scripts/claude_issue_route.py)

## Notes
- Issue mode: this session was started by the Claude issue dispatcher routine in Auto mode.
- The session had no `mcp__github__*` tools and no preinstalled `gh`; `gh` was installed by running `.claude/hooks/session-start.sh`, and GitHub writes use `gh api` routine writes (CLAUDE.md §23.H).
- 2026-09-29: resumed by session_01HQBMeq6bqy9XUqQs639dJM after `/reclarify` (answer C: the operator merged #5002 into the project branch). The project branch was synced with `main` (clean merge) and conformance 1/3 ran in this session.
- Conformance 1/3 not fixed (HYPOTHESIS, AD-3 by design): the queue watchdog reads dependencies with the workflow's `github.token`, which cannot read private consumer repositories, so a dependent consumer-repo item is never judged by the watchdog.

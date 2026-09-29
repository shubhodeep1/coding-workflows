# Implement-Plan Log — Chain security follow-ups that change the same file

- Plan: docs/plans/issue-4934-chain-same-file-security-followups-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#4934   Base branch: main   Progress comment: 5883200031
- Project branch: claude/implement-plan-issue-4934-chain-same-file-security-followups   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened; phase 1 in progress

## Phases
1. [ ] Phase 1 — chain same-file security follow-ups and hold dependent queue items

## Conformance

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

## Lessons

## Notes
- Issue mode: this session was started by the Claude issue dispatcher routine in Auto mode.
- The session had no `mcp__github__*` tools and no preinstalled `gh`; `gh` was installed by running `.claude/hooks/session-start.sh`, and GitHub writes use `gh api` routine writes (CLAUDE.md §23.H).

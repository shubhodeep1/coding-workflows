# Implement-Plan Log — Usage-limit resumes: only resume sessions the pickup's workflows started

- Plan: docs/plans/issue-6101-authorize-usage-limit-resumes-plan.md
- Source issue: shubhodeep1/coding-workflows#6101 (progress comment 5956414439)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5660-resume-usage-limit-stops
- Project branch: claude/implement-plan-issue-6101-authorize-usage-limit-resumes   Final PR: #6106 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #6108: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-02
- Last note: phase 1 PR #6108 opened twin-first (workflow-templates/.claude/ twin only); hold claim posted; twin-sync blocker posted on #6101; the stage `/reclarify` resumes arms the wait on #6108 and never re-implements the phase

## Phases
1. [ ] Phase 1 — authorize usage-limit resumes by session source, origin, and lineage   — protected paths: .claude/scripts/usage_limit_resumes.py (via workflow-templates/.claude/** twin); .claude/commands/claude-issue-pickup.md (no twin — diff in the sync blocker)   — PR #6108 open (blocked on the twin sync); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified it)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-02] How are resumes authorized? — Picked: A — every GitHub source in coding-workflows or the consumer registry, `origin` = `claude_code_mcp_seed`, and a `parent_session_id`; fail closed. Alternatives: B — also require the parent chain to reach the pickup session; C — repository check only. Why: server-set fields, no API call; B would stop resuming chains rooted at an operator session or a previous pickup and ancestors older than the 72-hour listing; C still resumes a person's own Auto-mode session in the same repository. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] Where does the allowed repository set come from? — Picked: A — optional `--registry` (default `.github/ai/consumer_repos.json`) plus `--self-repo` (default `shubhodeep1/coding-workflows`); an unreadable registry narrows to the self repo and adds an `errors` line. Alternatives: B — new required arguments; C — only the pickup's own `get_session` sources. Why: the pickup's command line keeps working; C would drop the consumer-repo sessions the pickup starts; failing narrow keeps §1. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Must the session's `environment_id` match the pickup's? — Picked: A — no. Alternatives: B — yes. Why: §5; repository, origin, and lineage close the finding. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-02] Does `.claude/commands/claude-issue-pickup.md` change? — Picked: A — one doc line in step 1a.2, as a diff in the twin-sync blocker. Alternatives: B — leave it. Why: §7 keeps the operator text accurate. Applied in: twin-sync blocker. Status: pending review
- AD-5 [plan, 2026-10-02] What does `select` do when called without the allowed set? — Picked: A — `None` authorizes nothing (fail closed). Alternatives: B — self repo only; C — no check. Why: a caller that forgets the argument must not widen access. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-10-02] A session a person opened in the app (`origin` `desktop_app`, no parent) sometimes runs a workflow itself, such as the invoking session of a manual `/implement-plan-claude` (2 of 47 such sessions on the live 100-session listing). Is it resumed? — Picked: A — no; it is skipped as `unknown_origin` and resumed by hand. Alternatives: B — resume it when its title matches a workflow title pattern. Why: §1; titles are written by sessions and cannot authorize anything, and the person who opened the session can resume it. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-02)

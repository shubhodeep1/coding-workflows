# Implement-Plan Log — Start clarify for a /reclarify on any line of a trusted comment

- Plan: docs/plans/issue-5243-reclarify-any-line-plan.md
- Source issue: shubhodeep1/coding-workflows#5243
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5243-reclarify-any-line   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from main (issue mode); plan and log committed.

## Phases
1. [ ] Phase 1 — line-anchored `/reclarify` intake with self-trigger guards
   - Job-level predicate in `clarify.yml`, `internal-clarify.yml`, `workflow-templates/ai-clarify.yml`, README example.
   - `Decide clarify route` step gate (`not_reclarify_command`), fast-path guard.
   - `plan.yml` implementation-plan comment marker `<!-- ai:implementation-plan:v1 -->`.
   - `scripts/claude_issue_route.py` `is_reclarify_command` used by `has_trusted_reclarify`.
   - Tests: predicate contract, route-step gate, intake authorization.
   - Docs: README, agents.md, docs/how-it-works.md, `changelog.d/5243-reclarify-any-line.md`.
   - Done: four predicates match with the new clause; gate/intake cases pass; named suites green.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should the job-level gate detect `/reclarify` on a later line? — Picked: A — `startsWith(…) || (contains(body, fromJson('"\n/reclarify"')) && !contains(body, '<!-- ai:'))`, line-anchored at job level. Alternatives: B — `contains(body, '/reclarify')` with the step check deciding; C — keep the gate and document "put `/reclarify` first". Why: no runner starts for inline mentions; C does not fix the incident. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Should the marker exclusion also apply when `/reclarify` is the first line? — Picked: A — no, a first-line `/reclarify` behaves exactly as today. Alternatives: B — exclude marker comments everywhere. Why: backward compatibility for stall recovery (§1, §5). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How should the `plan.yml` implementation-plan comment (ends with a bare `/reclarify` line) be kept from self-triggering? — Picked: A — append an invisible `<!-- ai:implementation-plan:v1 -->` marker as its last line. Alternatives: B — backtick the `/reclarify` line; C — special-case the heading in the gate. Why: no visible text or `implement.yml` lookup change; follows the marker convention. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should the route step also gate? — Picked: A — gate: a non-command comment skips with `reason=not_reclarify_command`. Alternatives: B — job gate only. Why: defence in depth, as the issue asks. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Build the answered-but-unrouted detection (issue fix direction 2) here? — Picked: A — no, leave it to the retire-master plan's phase 2 `claude_blocked_sweep.py`. Alternatives: B — a read-only check in `claude-issue-queue-watchdog.yml` now; C — in the pickup command. Why: B/C compete with the planned sweep in the same files (§5). Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-29] Should `has_trusted_reclarify` match case-insensitively? — Picked: A — keep case-sensitive, add the line rule. Alternatives: B — case-insensitive. Why: authorization stays at least as strict as today (§1). Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue mode: plan written by `/implement-issue-claude` (session session_01BLtfiCZr6H7G3cR79qitp5); base branch `main`; security pass runs (`security_pass_skip.py`: no skip label).

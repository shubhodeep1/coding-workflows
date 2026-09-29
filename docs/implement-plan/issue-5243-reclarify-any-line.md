# Implement-Plan Log — Start clarify for a /reclarify on any line of a trusted comment

- Plan: docs/plans/issue-5243-reclarify-any-line-plan.md
- Source issue: shubhodeep1/coding-workflows#5243
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5243-reclarify-any-line   Final PR: #5266 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented: label-scoped any-line `/reclarify` gate, route-step gate, plan-comment marker; intake unchanged (AD-8).

## Phases
1. [ ] Phase 1 — line-anchored `/reclarify` intake with self-trigger guards
   - Job-level predicate in `clarify.yml`, `internal-clarify.yml`, `workflow-templates/ai-clarify.yml`, README example.
   - `Decide clarify route` step gate (`not_reclarify_command`), fast-path guard.
   - `plan.yml` implementation-plan comment marker `<!-- ai:implementation-plan:v1 -->`.
   - Later-line form limited to `ai:claude-blocked` / `ai:claude-handoff-failed` / `ai:blocked` issues (AD-7); intake `has_trusted_reclarify` unchanged (AD-8).
   - Tests: predicate contract, route-step gate, plan-comment marker.
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
- AD-6 [plan, 2026-09-29] Should `has_trusted_reclarify` match case-insensitively? — Picked: A — keep case-sensitive, add the line rule. Alternatives: B — case-insensitive. Why: authorization stays at least as strict as today (§1). Applied in: superseded by AD-8 (no code change). Status: pending review
- AD-7 [phase 1, 2026-09-29] Where does the later-line `/reclarify` form apply? — Picked: A — only on issues labelled `ai:claude-blocked`, `ai:claude-handoff-failed` or `ai:blocked`. Alternatives: B — every issue, relying on `<!-- ai:` markers; C — every issue, plus markers on every model-text comment site (10+ files). Why: about 15 marker-less automation comments embed model text, mostly on tracking and orchestrator issues; A still fixes the incident and they can never start clarify (§1). Applied in: phase 1. Status: pending review
- AD-8 [phase 1, 2026-09-29] Should the Claude intake's `has_trusted_reclarify` accept the later-line form? — Picked: A — no, unchanged (supersedes AD-6). Alternatives: B — mirror the gate. Why: an outside author's issue already carries the first-line trusted `/reclarify` that admitted it; widening only adds an authorization path (§1, §5). Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] Automation posts issue comments as the same trusted User a human uses, and many embed model text without an `<!-- ai:` marker; any widened comment-command trigger must be scoped (for example by label) and not rely on markers alone. (files: .github/workflows/clarify.yml, .github/workflows/plan.yml)

## Notes
- Issue mode: plan written by `/implement-issue-claude` (session session_01BLtfiCZr6H7G3cR79qitp5); base branch `main`; security pass runs (`security_pass_skip.py`: no skip label).

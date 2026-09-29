# Implement-Plan Log — Bind review rejection votes to per-run finding IDs, so quoted REJECTED_FINDING text cannot demote a finding

- Plan: docs/completed/issue-4688-bind-rejection-votes-to-finding-ids-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4688
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
- Project branch: claude/implement-plan-issue-4688-bind-rejection-votes-to-finding-ids   Final PR: #4694 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason)
- Waiting on: completion PR (this PR)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_016skxAuXUH313JGNkv1KTVt (reused)   safety net and hand-back in the completion stage report
- Last updated: 2026-09-28
- Last note: validation skipped (Q1: A, operator, 2026-09-28); plan moved to docs/completed/ in the completion PR; next: final-merge 1/1.

## Phases
1. [x] Phase 1 — ID-bound rejection votes (issuer, header, gate, resume persistence, docs, tests)   — PR #4699 merged 2026-09-28; review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-28: CONFORMANT — no fixes (pre-security; security skipped). 114 footprint tests and 213 contract tests pass; tests/inventory_parity.py and tests/review_autofix_step_scripts.py pass; bash -n on the 4 edited shell scripts and ruff clean. The #4688 exploit demotes the finding on the base's gate and not on the new one.

## Security pass
- Skipped: plan header `Security pass: skip (ai:security: automation-produced issue)`.

## Validation
- Skipped — Q1: A (operator, 2026-09-28, on #4688): validate.yml binds target_ref only to a final PR into the default branch, and this issue-mode project's final PR #4694 targets claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason. The change reaches main only through #4586's final PR #4593, whose chain runs its own security pass and validation. Long-term fix tracked in #4734.

## Completion
- Completion PR (this PR) — doc moved to docs/completed/issue-4688-bind-rejection-votes-to-finding-ids-plan.md
- Final PR #4694 draft (into claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How are rejection votes bound so quoted text cannot count? — Picked: A — per-run random finding IDs, issued after pass 1 into a manifest, and only manifest IDs count. Alternatives: B — keep file/line matching and only harden parsing (column 0, no fences, required reason); C — remove the rejection path. Why: B still counts a column-0 quote in prose; C reopens #4586; A is what the finding recommends. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-28] Where does ID issuing live? — Picked: A — a new `--issue-ids` mode of `scripts/review_claude_fixer_nonblocking.py`. Alternatives: B — a new script. Why: already bootstrapped and owns the ledger parser, so issuer and gate cannot drift. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-28] What happens to legacy-format `REJECTED_FINDING` lines without an ID? — Picked: A — ignored; the entry stays blocking. Alternatives: B — accept them during a transition. Why: B keeps the exploit open. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-28] What is the vote line shape? — Picked: A — `REJECTED_FINDING: <ID> | <file>:<line> | flagged_by: <slug> | reason: <one sentence>`, only the ID and a non-empty reason required, echoed location ignored by the gate. Alternatives: B — ID and reason only. Why: A keeps the summariser's informational `rejected_by:` mapping. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-28; revised phase 1, 2026-09-28] How do IDs survive a same-head resume? — Picked: A — every rebuilt pass-2 header issues fresh IDs; the manifest is persisted with the partial-finalize artifacts only for a resume that skips the whole reviewer phase. Alternatives: B — reuse the manifest when its `ledger_sha256` matches. Why: under B an ID from an earlier run, which can be published in that run's uploaded `review_*.txt` artifacts, would count again in newly generated pass-2 output; A only drops cached votes on a rare resume, which fails toward blocking (§1). Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-28] The unreleased #4586 changelog fragment describes file/line/flagger matching. — Picked: A — correct that row and add this issue's own `security` fragment. Alternatives: B — add only this issue's fragment. Why: B ships a stale statement. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-28] Defend against a prompt-injected majority writing valid votes? — Picked: A — no; accepted residual risk. Alternatives: B — disable demotion. Why: an injected majority can already suppress a finding by not reporting it. Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-28] Which pass-1 entries get an ID? — Picked: A — `CONSENSUS FINDINGS` entries with a parseable location and exactly one flagger. Alternatives: B — every entry. Why: only single-reviewer entries can be demoted. Applied in: phase 1. Status: pending review

## Lessons
- [source:plan-deviation] A run-time token that binds reviewer output to a gate must never be reused across workflow runs: reviewer outputs are uploaded as log artifacts, so a reused token can reach later reviewer context. Issue fresh tokens per prompt build and let cached votes lapse (fail toward blocking). (files: scripts/review_claude_fixer_nonblocking.py, scripts/review_run_reviewers.sh)

## Notes
- Issue mode (CLAUDE.md §28.A); started by the Claude issue dispatcher in session session_01REL9XiCRyzMYVeALbEuUdc.
- Issue progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4688#issuecomment-5862298363
- No phase touches `.claude/**`, so no protected-path approval is needed.
- Blocked at validation 1/3 on 2026-09-28 (validate.yml cannot bind target_ref to a non-default-base final PR); operator answered Q1: A on #4688 the same day and the chain resumed from the pickup (session session_01KpcwqY8KZFhDwCstrkKtUt).

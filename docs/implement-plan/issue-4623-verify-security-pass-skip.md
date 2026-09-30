# Implement-Plan Log — Verify automation provenance before an issue-mode project skips its security pass

- Plan: docs/completed/issue-4623-verify-security-pass-skip-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4623 (https://github.com/shubhodeep1/coding-workflows/issues/4623)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4623-verify-security-pass-skip   Final PR: #4635 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: the completion PR from `claude/implement-plan-issue-4623-verify-security-pass-skip-complete`
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01FJKco7zdoJ1ez3JY6t8ui9   safety net and hand-back armed by the validation 1/3 — read result stage session session_01SARmvgqzWfhviH8nmWHwjd (ids in its report)
- Last updated: 2026-09-27
- Last note: validation cycle 1 passed (run 36356008779, 10/10 tests) after conformance run 2 was CONFORMANT with no findings; plan moved to docs/completed/ in the completion PR, final PR #4635 is marked ready once it merges

## Phases
1. [x] Phase 1 — verified security-pass skip (`.claude/scripts/security_pass_skip.py`, commands, allowlist, tests, docs, changelog)   — PR #4645 merged 2026-09-27 (1b872850d28f); review rounds: 3; interventions: 0

## Conformance
- Run 1 — 2026-09-27: CONFORMANT (Implemented COMPLETE; Correctness CONCERNS: 1 EVIDENCE-BASED concern, plan Implementation Step 3 named a `, verified` header suffix the command does not write and the phase PR did not record) — conformance fix PR #4663 (pre-security), merged by hand 2026-09-27 (squash e0e49e8) after the human answered Q1: B on #4623 and commented /reclarify
- Run 2 — 2026-09-27: CONFORMANT (Implemented COMPLETE, Correctness PASS, no findings) — no fixes (pre-security)

## Security pass
- Skipped: plan header `Security pass: skip (ai:security: automation-produced issue)` (AD-6)

## Validation
- Cycle 1 — run 36356008779 2026-09-27 (target_ref: project branch; validated the authorized head 68eec15): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 289s), no fix issues

## Completion
- Completion PR (this log update) — doc moved to docs/completed/issue-4623-verify-security-pass-skip-plan.md
- Merged PRs into the project branch: #4645 (phase 1/1, merged 2026-09-27), #4663 (conformance fix 1, merged 2026-09-27)
- Final PR #4635 draft (marked ready in stage final-merge 1/1)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Where does the verification live? — Picked: A — a deterministic `.claude/scripts/security_pass_skip.py` the command runs, mirrored to consumers. Alternatives: B — decide in `clarify.yml` and carry the verdict through payload, queue, and start prompt; C — prose rules in the command. Why: one component, testable, available in consumer checkouts, and the model no longer decides from mutable data. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Which identity counts as the designated automation? — Picked: A — author `github-actions[bot]` (Bot) or a `User` with `author_association: OWNER`, and the skip label's first `labeled` event performed by that author within 120 s of creation. Alternatives: B — a configured login list; C — any OWNER/MEMBER/COLLABORATOR. Why: both signals are set by the creating token and cannot be produced by a triage-role user or a collaborator acting as themselves. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What proves the link to a valid automation record? — Picked: A — the label's own body marker, plus for `ai:security` a `Refs #T` tracker carrying `ai:security-audit`, the tracker marker, and the same author. Alternatives: B — marker only; C — also require a tracker comment naming the finding id. Why: satisfies the recommendation within 3 REST calls (§15). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] What happens on a read error or a failed check? — Picked: A — write `Security pass: run`. Alternatives: B — stop the project as BLOCKED. Why: §1, the safe option costs only an audit run. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] What happens to the router's label-only `skip_security_pass` field? — Picked: A — keep field and computation (§6), document it as advisory. Alternatives: B — tighten the router with the same checks. Why: no component decides from it; §5 minimal change. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-27] Does this project skip its own security pass? — Picked: A — skip: #4623 passes the new rule by hand. Alternatives: B — run it. Why: the issue is an automation-produced follow-up under both the old and the new rule; conformance, validation, and the reviewer panel still run. Applied in: no code change. Status: pending review
- AD-7 [conformance 1/3, 2026-09-27] Plan Implementation Step 3 says the header is `Security pass: skip (<label>: automation-produced issue, verified)`, the merged command writes it without `, verified`; which one changes? — Picked: A — keep the code and correct the plan text, recorded as a plan deviation. Alternatives: B — add `, verified` to the command, its contract test, and the Issue Mode header grammar. Why: the plan's own §6 constraint freezes the `Security pass:` header format and the Issue Mode header block documents it without the suffix; §5 smallest change. Applied in: conformance fix PR (plan text only). Status: pending review

## Lessons
- [source:intervention] Pick the earliest GitHub event by its `created_at`, never by list position: the REST events endpoints are chronological today but reviewers treat the order as unspecified, and a position-based check breaks silently if it changes. (files: .claude/scripts/security_pass_skip.py)
- [source:plan-deviation] Read label history from `issues/<N>/events` (label and state events only), not `issues/<N>/timeline`: the timeline mixes in comments and cross-references, so a 100-item page fills faster and hides later label events. (files: .claude/scripts/security_pass_skip.py)
- [source:conformance] When a plan quotes an output format that another part of the same plan freezes under §6, reconcile the two before implementing and record the one that wins as a plan deviation, so the conformance audit does not flag the plan text. (files: docs/plans/issue-4623-verify-security-pass-skip-plan.md, .claude/commands/implement-issue-claude.md)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#4623: start`); invoking session session_01WLQwN8LRQ8ih2NrbkY1ky6.
- Phase 1 used the issue-events endpoint instead of the timeline named in the plan (plan updated in the phase PR); a non-automation author now costs one REST read instead of two.
- Draft final PR #4635 opened 2026-09-27.
- Local verification: `tests/test_orchestrate_poll_process.py` (446 tests) runs >40 min on this container's Python 3.11 and was not finished when the PR opened; CI's sharded Python 3.12 run is authoritative for it. `tests/test_workflow_retro.py` cannot be collected on Python 3.11 (pre-existing, unrelated).
- Conformance run 1 found Implementation Step 3's `, verified` header suffix unimplemented and unrecorded; the plan text now matches the command (AD-7). The check CLI verified live on 2026-09-27: #4623 → skip (`ai:security`), #4653 and #4580 → skip (`ai:workflow-heal`), #3576 → run (no skip label), a missing issue → exit 2.
- Project branch synced with main at 68eec15 on 2026-09-27 (after #4663 merged), and at ca73830 in the validation 1/3 read stage. Main's #4672, merged after validation, is a docs-only change to another plan, so the validated code is unchanged.

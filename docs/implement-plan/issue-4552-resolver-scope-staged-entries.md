# Implement-Plan Log — Resolver scope check: compare staged entries, forbid model staging

- Plan: docs/plans/issue-4552-resolver-scope-staged-entries-plan.md
- Source issue: shubhodeep1/coding-workflows#4552
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4552-resolver-scope-staged-entries   Final PR: #4555 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (opened from claude/implement-plan-issue-4552-resolver-scope-staged-entries-phase-1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-26
- Last note: phase 1 implemented and verified (25/25 in the resolver test file; 21 related suites green); phase PR opened

## Phases
1. [ ] Phase 1 — staged-entry scope check, safe ValueError reason, no-staging prompt rule
   - [x] `_resolver_scope_state.merge_state()` hashes `git ls-files -s -v -z` instead of raw index bytes; MERGE_HEAD unchanged
   - [x] `ValueError`-only `failure reason:` line; existing `::error::` line unchanged
   - [x] `prompts/conflict-resolver.txt` no-index-changes rule (≤ 150 lines)
   - [x] tests: metadata refresh accepted; `git add` in conflicted merge and assume-unchanged rejected (exit 2, reason named); prompt rule present; registered in `main()`
   - [x] `changelog.d/4552-resolver-scope-staged-entries.md` (fixed)

## Conformance

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-26] Which fix does the transcript evidence call for? — Picked: A — both: compare staged entries instead of raw index bytes, and add the no-staging prompt rule. Alternatives: B — prompt rule only; C — staged-entry comparison only. Why: both failed runs show model `git add`, and a local repro shows `git status` alone changes raw index bytes; neither weakens the fail-closed response to real staging. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-26] How should the failed-closed reason be surfaced? — Picked: A — keep the `::error::` line unchanged, add a `failure reason:` line for `ValueError` only. Alternatives: B — no new diagnostics; C — append to the existing line. Why: §8 diagnostics without touching a monitored line (§6) or printing untrusted text. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-26] Block staging at the OpenCode tool boundary too? — Picked: A — no, prompt rule only. Alternatives: B — add bash deny rules to the resolver OpenCode config. Why: §5 minimal; the check still fails closed if the prompt is ignored. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-26] Add the rule to the integration-sync resolver prompt too? — Picked: A — no. Alternatives: B — yes. Why: the issue and the evidence cover only the review resolver prompt (§5). Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] A fail-closed guard that hashes raw .git/index bytes breaks on read-only Git commands (git status refreshes stat data); compare `git ls-files -s -v -z` instead. (files: scripts/review_conflict_resolve.sh)

## Notes
- Issue mode: permission mode auto; issue base claude/quirky-wozniak-e9t88m (PR #4549 head), so activation is n/a and the final-merge stage closes the issue explicitly.

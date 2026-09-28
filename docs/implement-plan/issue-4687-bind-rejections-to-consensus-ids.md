# Implement-Plan Log — Bind reviewer rejections to pass-1 consensus ids so a nearby rejection cannot demote a distinct finding

- Plan: docs/plans/issue-4687-bind-rejections-to-consensus-ids-plan.md
- Source issue: shubhodeep1/coding-workflows#4687
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
- Project branch: claude/implement-plan-issue-4687-bind-rejections-to-consensus-ids   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: project branch opened from the #4586 project branch; phase 1 in progress.

## Phases
1. [ ] Phase 1 — bind rejections to consensus ids and keep ambiguous matches blocking
   - [ ] `scripts/review_claude_fixer_nonblocking.py`: consensus ids, `--annotate`, id-bound rejections, ambiguity rules, id-gated bullet moves, diagnostics
   - [ ] `scripts/review_run_reviewers.sh` `build_cross_pollination_summary`: annotated ledger and the new rejection instructions
   - [ ] `scripts/summarize_reviewer_consensus.sh`: `consensus_id:` copy rule
   - [ ] `scripts/review_autofix_step_claude_fixer_handoff.sh`: header comment
   - [ ] Tests: `tests/test_review_claude_fixer_nonblocking.py`, `tests/test_review_autofix_claude_fixer_mode.py`
   - [ ] Docs: `README.md`, `agents.md`, `docs/INVENTORY.md`, `changelog.d/4687-bind-rejections-to-consensus-ids.md`
   - Done when: the extended tests, the step-script contract, and the workflow-size suites pass; `bash -n` passes on the edited shell scripts.

## Conformance

## Security pass
- Skipped: ai:security: automation-produced issue (`security_pass_skip.py` verified it)

## Validation

## Completion

## Activation
- Not applicable: the base branch is `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason`, not the default branch.

## Auto-decisions
- AD-1 [plan, 2026-09-28] How is a rejection bound to the finding it is about? — Picked: A — a content-derived `consensus_id` on every pass-1 consensus entry; a rejection counts only when it cites that id (with matching file, overlapping range, and flagger), and a pass-2 entry is tied to the pass-1 entry only by the same id carried by its flagger's own output. Alternatives: B — keep proximity and require each rejection to match exactly one entry; C — exact `file:line` equality without ids. Why: the issue's recommendation, and B and C still demote a new flaw beside a rejected entry the flagger dropped (§1). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-28] What counts as an ambiguous match that must stay blocking? — Picked: A — another pass-1 consensus entry within 3 lines of the bound pass-1 entry, another pass-2 consensus entry within 3 lines of the demoted entry, or the id on more than one entry in either ledger. Alternatives: B — only duplicate ids. Why: a mis-copied id between nearby entries is the likeliest summariser error, and the issue asks for ambiguous matches to stay blocking (§1). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-28] How is the id derived? — Picked: A — `p1-` plus 12 hex digits of the SHA-256 of the pass-1 entry text, recomputed from the never-rewritten `consensus_pass1.txt`. Alternatives: B — ordinal numbers; C — write ids into `consensus_pass1.txt`. Why: a regenerated or reordered ledger invalidates content ids instead of silently rebinding ordinals, and C changes an input other steps read (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-28] What happens to `REJECTED_FINDING` lines without an id? — Picked: A — ignored, and counted in a `CLAUDE_FIXER_NONBLOCKING_LEGACY_REJECTIONS` log line. Alternatives: B — still honoured by proximity. Why: B keeps the vulnerability open (§1); the prompt and the demoter ship in the same support bundle, so no reviewer sees the old instructions with the new demoter. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-28] Which per-reviewer bullets move with a demoted entry? — Picked: A — only bullets in the flagger's or a rejecter's section whose range overlaps the entry and whose text carries the same id. Alternatives: B — every bullet within 3 lines, as today. Why: B can move a distinct flaw's bullet out of the blocking count (§1). Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-28] What is the new ledger field called, and what happens to the identifiers the proximity match used? — Picked: A — `consensus_id`, and `REJECTED_FINDING_RE`, `LINE_TOLERANCE`, `_near`, and `reviewer_rejections` stay with their meaning (location parsing, the ambiguity window, and the legacy-line count). Alternatives: B — `finding_id`, and delete the proximity helpers. Why: `finding_id` already names security-audit findings, and §6 forbids removing identifiers. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue mode: the plan was written by `/implement-issue-claude` for #4687 (security-audit follow-up, tracker #3576). This plan supersedes #4586's AD-7 (the 3-line rejection window) for binding.
- Protected paths: none (no `.claude/**` edits).

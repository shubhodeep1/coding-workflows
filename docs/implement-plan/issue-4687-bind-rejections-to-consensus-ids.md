# Implement-Plan Log — Bind reviewer rejections to pass-1 consensus ids so a nearby rejection cannot demote a distinct finding

- Plan: docs/completed/issue-4687-bind-rejections-to-consensus-ids-plan.md (moved from docs/plans/ by the completion PR)
- Source issue: shubhodeep1/coding-workflows#4687
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
- Project branch: claude/implement-plan-issue-4687-bind-rejections-to-consensus-ids   Final PR: #4695 ready — review rounds: 2
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason)
- Waiting on: PR #4695 (final PR into the base branch; the review round on the head this commit creates)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_0117txgg28BLvgvA4ZjrRbV3 (reused for every wait)   safety net and hand-back: the ids are in the final-merge — review round stage's report
- Last updated: 2026-09-28
- Last note: final-merge review round 2 on #4695 (head 24e38db1959e, workflow round 1, run 36430261246): all 6 reviewers ran; 2 low-severity consensus findings (repeated reads of `review_<slug>.txt` and a second `successful_reviewers()` scan in `main()`) were fixed in this `[claude-autofix]` commit with a regression test; minimax's legacy-count hardening note was rejected (a bound line cannot match `REJECTED_FINDING_RE`, and dropping the count contradicts AD-4).

## Phases
1. [x] Phase 1 — bind rejections to consensus ids and keep ambiguous matches blocking   — PR #4698 merged 2026-09-28; review rounds: 0 (the first reviewer run reported no findings); interventions: 0
   - [x] `scripts/review_claude_fixer_nonblocking.py`: consensus ids, `--annotate`, id-bound rejections, ambiguity rules, id-gated bullet moves, diagnostics
   - [x] `scripts/review_run_reviewers.sh` `build_cross_pollination_summary`: annotated ledger and the new rejection instructions
   - [x] `scripts/summarize_reviewer_consensus.sh`: `consensus_id:` copy rule
   - [x] `scripts/review_autofix_step_claude_fixer_handoff.sh`: header comment
   - [x] Tests: `tests/test_review_claude_fixer_nonblocking.py`, `tests/test_review_autofix_claude_fixer_mode.py`
   - [x] Docs: `README.md`, `agents.md`, `docs/INVENTORY.md`, `changelog.d/4687-bind-rejections-to-consensus-ids.md`
   - Done when: the extended tests, the step-script contract, and the workflow-size suites pass; `bash -n` passes on the edited shell scripts.

## Conformance
- Run 1 — 2026-09-28: CONFORMANT — no fix PR (pre-security). Every plan criterion traces to `scripts/review_claude_fixer_nonblocking.py`, `build_cross_pollination_summary` in `scripts/review_run_reviewers.sh`, and `scripts/summarize_reviewer_consensus.sh`; 251 tests passed across the Claude-fixer, nonblocking, step-contract, workflow-size, and reviewer-pipeline suites, and `bash -n` and ruff were clean. Before it, the base branch was merged into the project branch (20d6127).

## Security pass
- Skipped: ai:security: automation-produced issue (`security_pass_skip.py` verified it)

## Validation
- Skipped (non-default base): `validate.yml` binds `target_ref` only to an open PR into the default branch, and final PR #4695 targets the #4586 project branch, so a dispatch could not validate this code. Asked as Q1 on #4687 (a failure escalation, CLAUDE.md §28.C); the operator answered `Q1: A` on 2026-09-28: skip here, because the change reaches `main` only through #4586's final PR #4593, and #4586's chain runs its own security pass and runtime validation on a project branch that will contain this fix.

## Completion
- PR #4747 merged 2026-09-28 (squash 175b9d7, merged by the operator per `Q2: C`) — doc moved to docs/completed/issue-4687-bind-rejections-to-consensus-ids-plan.md
- Merged PRs into the project branch: #4698 (phase 1/1, merged 2026-09-28), #4747 (completion, merged 2026-09-28)
- Final PR #4695 ready — review rounds: 2 (round 1 on 175b9d78d309: 0 findings from 5 of 6 reviewers, minimax/minimax-m3 slot stalled 3 times, handed off; operator `Q3: A` → log commit 24e38db for a new head. Round 2 on 24e38db1959e: 6 of 6 reviewers, 2 low-severity consensus findings fixed in a `[claude-autofix]` commit, 1 per-reviewer note rejected)

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
- [source:security] A reviewer verdict about another reviewer's finding must be bound to that finding by an identifier both sides can see (here a content-hash consensus_id), never by file and line proximity, and any ambiguous binding must keep the finding blocking. (files: scripts/review_claude_fixer_nonblocking.py, scripts/review_run_reviewers.sh)
- [source:intervention] In Claude-fixer mode, a review with zero findings still hands off instead of auto-merging when any reviewer slot fails or another review run on the same head is still active at the pre-merge check refresh; without a configured verdict bot, only a new head or a human merge clears it. (files: scripts/review_autofix_step_claude_fixer_handoff.sh)

## Notes
- Issue mode: the plan was written by `/implement-issue-claude` for #4687 (security-audit follow-up, tracker #3576). This plan supersedes #4586's AD-7 (the 3-line rejection window) for binding.
- Protected paths: none (no `.claude/**` edits).
- Conformance run 1 noted one HYPOTHESIS concern, not fixed: the flagger-citation guard checks whether the id appears anywhere in the flagger's raw pass-2 text, so a flagger that mentions the id while withdrawing a finding, combined with a summariser that copies it onto a new flaw at an overlapping range, would still bind. The plan specifies exactly this check, and its Risks section accepts the related same-lines case.
- Validation 1/3 stopped at `Status: BLOCKED` (2026-09-28, session_01XU5G93fyGxoT3k9Ehe3LS5) and asked Q1 on #4687. Operator decision `Q1: A` (issue comment, 2026-09-28): `Validation: skipped (non-default base)`. The long-term fix is tracked in #4734 (let `validate.yml` authorize a final PR into a project branch whose own PR into `main` is open).
- Completion review round 1 on #4747 (head 5ea3be8b3403) was clean, but a push-leg `review-claude-branch-push` run still active on the same head made the pre-merge check refresh fail closed, so the workflow posted a 0-finding hand-off. With no `CLAUDE_FIXER_VERDICT_BOT_LOGIN` configured, the stage stopped and asked Q2 on #4687. Operator decision `Q2: C` (2026-09-28): the operator merged #4747 by hand (squash 175b9d7).
- Final-merge review round 1 on #4695 (head 175b9d78d309, run 36416865588) reported 0 findings and 0 task gaps from 5 reviewers; the `minimax/minimax-m3` slot was killed by the stall guard on all 3 attempts, so the clean-ledger check failed closed and handed the round off. The stage posted a `hold` claim and asked Q3 on #4687. Operator decision `Q3: A` (2026-09-28): commit this log update to the project branch so the final PR gets a new head and a full-panel review. The stalled-slot problem is filed as #4835; if the slot stalls again before #4835 lands, the stage stops BLOCKED and cites #4835 instead of asking again.
- Final-merge review round 2 on #4695 (head 24e38db1959e, run 36430261246, ledger e7b819c3…): the full panel ran (the minimax slot did not stall). Consensus findings, both NIT/low from gemini-3.1-flash-lite and minimax-m3: (1) `demote_with_diagnostics` read each `review_<slug>.txt` three times; now read once, parsed by the new private `_bound_rejections_in` / `_legacy_rejections_in`, with `reviewer_bound_rejections` / `reviewer_rejections` kept as Path wrappers (§6); (2) `main()` re-ran `successful_reviewers()` for its log line; now scanned once and passed through a new optional keyword `reviewers=`. Regression test `test_each_reviewer_output_is_read_once_and_statuses_scanned_once`. Rejected: minimax's note that the legacy `REJECTED_FINDING_RE` count could include unparsable lines (every counted line is fully parsed by that regex, a bound line never matches it, and the count only feeds a log line; its suggested removal contradicts AD-4).

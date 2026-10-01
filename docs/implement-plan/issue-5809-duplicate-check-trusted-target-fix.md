# Implement-Plan Log — Require a trusted target and a same-repository fix PR in permission_prompts.py duplicate-check

- Plan: docs/plans/issue-5809-duplicate-check-trusted-target-fix-plan.md
- Source issue: shubhodeep1/coding-workflows#5809 (https://github.com/shubhodeep1/coding-workflows/issues/5809)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4867-close-permission-prompt-duplicates
- Project branch: claude/implement-plan-issue-5809-duplicate-check-trusted-target-fix   Final PR: #5832 draft
- Status: BLOCKED
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5862: twin sync, review round 2 (copy `workflow-templates/.claude/scripts/permission_prompts.py` into `.claude/scripts/` as a `[claude-twin-sync]` commit, then `/reclarify` on #5809)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01MXGEmTa7j2hQYy3jTst7MH (reused)   safety net none   hand-back none (trig_01UPWmucYDzPUYV5dsuqB5RP and trig_014hQCH6c2tzY37M2VS8uR7G deleted at review round 2; the stage after the twin sync re-arms the wait)
- Last updated: 2026-10-01
- Last note: second twin sync landed as 05a3876 (review round 1's comment fix). Review round 2 (workflow round 1 on 05a3876): one consensus finding, valid: `decide_duplicate_close` (and its `_untrusted_author` helper) raised `AttributeError` when `security_pass_skip.py` had not loaded, guarded only by its `duplicate-check` caller; fixed in the twin with a fail-closed early return (`_SKIP_CHECK_ERROR`) and a test. The deepseek task gap reported the parity requirement satisfied (no action). Blocked again on the twin sync of that guard.

## Phases
1. [ ] Phase 1 — trusted target and same-repository fix PR in `duplicate-check`   — protected paths: `.claude/scripts/permission_prompts.py` (edited through its `workflow-templates/.claude/` twin)   — PR #5862 open (waiting on twin sync of review round 2); review rounds: 2; interventions: 0
   - `workflow-templates/.claude/scripts/permission_prompts.py`: target `author_association` trusted; fix PR same-repository head and trusted author; target evidence `null` when untrusted; docstrings
   - `tests/test_permission_prompt_duplicates.py`: trusted fixtures, failing cases for each new check, five-read budget kept
   - `CLAUDE.md` §23.I condition 2, `agents.md` "Duplicate close", `changelog.d/5809-duplicate-check-trusted-target-and-fix.md` [new]
   - Done: goals 1-6; new tests fail against the base twin and pass after; ruff clean; `tests/test_permission_prompts.py` passes after the twin sync

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verdict `skip: true`)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] What provenance must the duplicate target have? — Picked: A — its `author_association` is `OWNER`, `MEMBER`, or `COLLABORATOR` (no new read). Alternatives: B — the target must itself be pipeline-filed (a sixth read); C — both. Why: shuts out an outsider-forged target within the five-read budget and keeps §23.I condition 2's documented scope (a maintainer-filed root-cause issue such as #4858 stays valid). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What must the fix PR satisfy beyond referencing the target? — Picked: A — a same-repository head and a trusted `author_association`. Alternatives: B — same-repository head only; C — trusted author only. Why: both fields are in the one PR read and together refuse fork PRs and PRs by accounts without write access (§1). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] Keep the title/body `#<M>` reference as a binding, or require the head branch `issue-<M>-`? — Picked: A — keep both, now only on a trusted same-repository PR. Alternatives: B — head branch only. Why: a trusted PR's text is written by an account with write access; B refuses fix PRs whose branch does not carry the number (§5). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] What happens to `target_class` / `target_occurrences` for an untrusted target? — Picked: A — `null`. Alternatives: B — report them as before. Why: causes are not read from an untrusted, editable body; no new key (§6). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-01] Edit `.claude/scripts/permission_prompts.py` directly, or twin-first? — Picked: A — twin-first under the interim automatic default, with a hold claim and the twin-sync blocker on #5809. Alternatives: B — ask how to run the phase. Why: coding-workflows has `workflow-templates/.claude/` and the plan does not require a watched session. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1 twin-sync resume 2, 2026-10-01] Record the second twin sync (05a3876) and the re-armed wait in the log now, as a docs-only push to #5862, or with the next push? — Picked: A — with the next push to the project. Alternatives: B — docs-only push now. Why: review round 2 was already running on 05a3876; a push would restart it and spend a review round (§5). Applied in: no code change. Status: pending review
- AD-7 [phase 1/1 review round 2, 2026-10-01] The reviewer's only finding (`_untrusted_author` raises when `security_pass_skip` is `None`) is unreachable through `duplicate-check` today: fix or reject? — Picked: A — fix: `decide_duplicate_close` returns ineligible with `_SKIP_CHECK_ERROR` when `security_pass_skip` did not load, plus a test. Alternatives: B — reject as unreachable; C — make only `_untrusted_author` independent of `security_pass_skip`. Why: fails closed for any caller (§1), one condition and no new key (§5, §6), and no verdict bot is configured, so a rejection-only round cannot converge unattended; C would leave the same crash at the function's existing `security_pass_skip` calls. Applied in: PR #5862. Status: pending review

## Lessons
- [source:intervention] `author_association` in `OWNER`/`MEMBER`/`COLLABORATOR` is a trust policy, not proof of write access (a member or collaborator can hold read or triage rights only); describe it that way in comments and docs. (files: workflow-templates/.claude/scripts/permission_prompts.py)
- [source:intervention] A pure decision function that uses an optionally loaded module must guard the missing module itself and fail closed; a guard only in its CLI wrapper leaves direct callers with an `AttributeError`. (files: workflow-templates/.claude/scripts/permission_prompts.py, tests/test_permission_prompt_duplicates.py)

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)
- Review round 1 (2026-10-01, session_01PnXcbxf3thESKMAr71wzhB): the phase-1 twin sync 9a1bc89 resolved the first blocker; the round's comment fix edits the twin only, so a second twin sync is needed (hold claim + blocker on #5809). The project-branch sync merge of the issue base was denied by the Auto-mode classifier ([Modify Shared Resources]); it was a no-op (the project branch already contains every base commit).
- Twin sync 2 (2026-10-01 05:15Z, master session): 05a3876 copied the review-round-1 twin into `.claude/scripts/permission_prompts.py` (sha256 cf6f066e…); 480 passed, 1 skipped.
- Review round 2 (2026-10-01, session_01V9NTi6Gmhe3txyq4xPypoN): the guard edits the twin only, so a third twin sync is needed (hold claim + blocker on #5809). New twin sha256 d1d167a974319d621a269a8bce391ad174ddcea54a36ceaa362ab7e4b6e0a07c. The project branch already contained the issue base (no sync push); the base's PR #4883 is still open.
- Stage hygiene (2026-10-01, review round 2): archived session_01EVX6h1VXsY4qEnUcWDodxX and session_01PnXcbxf3thESKMAr71wzhB (its twin-sync ask was answered by 05a3876); zombie checkers archived: 0.
- Security pass: skip (ai:security: automation-produced issue) — `security_pass_skip.py` reason `ai:security: created and labelled by the issue automation`.

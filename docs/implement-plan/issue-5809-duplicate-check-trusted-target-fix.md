# Implement-Plan Log — Require a trusted target and a same-repository fix PR in permission_prompts.py duplicate-check

- Plan: docs/plans/issue-5809-duplicate-check-trusted-target-fix-plan.md
- Source issue: shubhodeep1/coding-workflows#5809 (https://github.com/shubhodeep1/coding-workflows/issues/5809)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4867-close-permission-prompt-duplicates
- Project branch: claude/implement-plan-issue-5809-duplicate-check-trusted-target-fix   Final PR: #5832 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5862: twin sync (copy `workflow-templates/.claude/scripts/permission_prompts.py` into `.claude/scripts/` as a `[claude-twin-sync]` commit, then `/reclarify` on #5809)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: phase 1 PR #5862 opened (twin only; 14 new cases fail on the base twin and pass after; hold claim posted); twin-sync blocker posted on #5809. Checker not armed: the resumed stage arms the wait on #5862.

## Phases
1. [ ] Phase 1 — trusted target and same-repository fix PR in `duplicate-check`   — protected paths: `.claude/scripts/permission_prompts.py` (edited through its `workflow-templates/.claude/` twin)   — PR #5862 open (waiting on twin sync); review rounds: 0; interventions: 0
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

## Lessons

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)
- Security pass: skip (ai:security: automation-produced issue) — `security_pass_skip.py` reason `ai:security: created and labelled by the issue automation`.

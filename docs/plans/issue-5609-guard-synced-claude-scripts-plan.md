# Treat `.claude/scripts/**` as a guard path in the Claude twin sync

Source issue: shubhodeep1/coding-workflows#5609 (https://github.com/shubhodeep1/coding-workflows/issues/5609)
Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
Security pass: skip (ai:security: automation-produced issue)

## Summary

The twin sync (`scripts/claude_twin_sync.py`, issue #4785) auto-merges a sync PR that copies `workflow-templates/.claude/scripts/**` into `.claude/scripts/**`, because its guard classifier (`is_guard_path`, `scripts/claude_twin_sync.py:156`) only covers hooks and settings. `.claude/settings.json` allowlists those scripts, so every Claude session runs them with its authenticated GitHub access and without a permission prompt or Auto-mode classifier check. This plan makes `.claude/scripts/**` a guard path, so a script change reaches `.claude/` only through a sync PR the repository owner reviews and merges.

## Context

- Security audit finding #5609 (`STRIDE: Elevation of Privilege`, high, confidence 8/10), filed by `.github/workflows/security-audit.yml` against the #4785 project branch (`Integration branch:` line). Tracker: #3576.
- `.claude/settings.json` `permissions.allow` runs seven `.claude/scripts/*.py` files (`check_in_status.py`, `stale_routines.py`, `claude_fix_claim.py`, `security_pass_skip.py`, `dispatch_workflow.py`, `edit_comment.py`, `permission_prompts.py`) with no prompt. They run in every stage, checker, and fixer session, with `GH_TOKEN` and the claude-code-remote tools in reach.
- Today a twin edit to one of them lands on `main` through an ordinary PR (reviewed by the AI reviewer panel and auto-merged), then `claude-twin-sync.yml` copies it into `.claude/scripts/` and merges the sync PR itself once checks pass. No human reviews the code sessions will execute.
- Hooks and settings already take the owner-only path: `GUARD_PATH_PREFIXES = ("hooks/",)` and `GUARD_PATH_FILES` (`settings.json`, `settings.local.json`) drive `plan_sync` (`needs_owner`), `merge_check` (refuses guard paths), `check_not_ahead` (issue #5246: a guard path changes only to the twin already on the base commit, is never deleted, and on a PR changes only from a same-repository sync PR), and the `stable` provenance rule (issue #5247).
- CLAUDE.md §1 (security first), §5 (extend the existing mechanism), §6 (no rename), §20 (changelog fragment), §28 (auto-decisions).

## Goals

- G1: `is_guard_path("scripts/<anything>")` is true; `hooks/` and the two settings files stay guards; `commands/**` stays non-guard.
- G2: A sync PR that copies or conflicts on a `.claude/scripts/**` file is `needs_owner`: labelled `ai:claude-sync-approval`, alerted, never merged by the workflow; `merge-check` refuses it.
- G3: The CI sync-state check applies the #5246 guard rule to `.claude/scripts/**` (only the base commit's twin, never deleted, on a PR only from a same-repository sync PR) and the #5247 provenance rule on `stable`.
- G4: Docs that describe the guard set (CLAUDE.md §28.C, agents.md "Claude twin sync", README workflow table, `claude-twin-sync.yml` and `ci.yml` comments, the `ai:claude-sync-approval` label description, the #4785 plan text, the `implement-plan-claude.md` twin) name scripts as guards.

## Non-goals

- Running synced scripts without session credentials, or rebuilding them as least-privilege helpers (the issue's long-form recommendation; AD-1).
- Guarding `.claude/commands/**` (AD-2).
- The consumer `@stable` sync (`update_workflows.yml`), which copies `workflow-templates/.claude/**` into consumer repos; it is not the twin sync and is out of this finding's scope.
  > **Changed 2026-10-01 (AD-14):** #5607, merged into the base branch after this plan was written, makes the consumer sync and `/seed-repo` take guard paths from the `stable` commit's `.claude/` tree and ties `update_workflows.yml`'s shell pattern to `GUARD_PATH_PREFIXES` / `GUARD_PATH_FILES` with a parity test. The project branch's sync merge therefore adds `scripts/*` to that pattern (with its tests, README, agents.md, and the `seed-repo.md` twin), so consumers get `.claude/scripts/**` from `.claude/` too.
- The CI trust boundary already documented in agents.md: a PR that also edits `ci.yml` or `scripts/claude_twin_sync.py` runs its own copies of both.

## Constraints

- §5: extend `GUARD_PATH_PREFIXES`; no new mechanism.
- §6: no identifier renamed or removed. The posted status description `No hook or settings change` is kept (AD-3).
- §15: no new GitHub API call.
- §20: one fragment, `changelog.d/5609-guard-synced-claude-scripts.md`.
- §28.C: the phase edits no `.claude/**` file; the `implement-plan-claude.md` change goes to its twin only.

## Approach

Add `"scripts/"` to `GUARD_PATH_PREFIXES`. Every rule that keys on `is_guard_path` (sync plan, merge check, CI check on `main` and `stable`) then covers scripts with no further code change. Update the owner-facing strings (PR body merge rule, deletion reason, label description) and the docs so they name scripts, and move the tests that used `scripts/s.py` as the non-guard example to a command file.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the fix is one constant plus its tests and docs.

1. **Phase 1 — guard `.claude/scripts/**` in the twin sync.** Files: see Files & Modules. Done when `tests/test_claude_twin_sync.py` covers G1–G3, the twin/command/doc suites pass, and the docs name scripts as guards. Rollback: revert the PR; scripts return to the non-guard auto-merge path.

## Implementation Steps

1. `scripts/claude_twin_sync.py`: `GUARD_PATH_PREFIXES = ("hooks/", "scripts/")`; comment and module docstring name scripts; `render_body`'s non-guard line and `guard_violation_reason`'s deletion reason say hooks, scripts, and settings; `APPROVAL_LABEL_DESCRIPTION` matches the label contract.
2. `.github/ai/label_contract.v1.json`, `scripts/label_helpers.sh`: the `ai:claude-sync-approval` description names script changes.
3. `tests/test_claude_twin_sync.py`: `scripts/…` guard parametrisation; plan, check (PR, push, deletion, provenance), merge-check, and run tests for a script; non-guard examples moved to `commands/…`.
4. Docs: CLAUDE.md §28.C bullet, agents.md "Claude twin sync", README workflow table row, `claude-twin-sync.yml` / `ci.yml` header comments, `docs/plans/issue-4785-twin-first-claude-sync-plan.md` guard lines, `workflow-templates/.claude/commands/implement-plan-claude.md` rule line.
5. `changelog.d/5609-guard-synced-claude-scripts.md` (`security`).

## Files & Modules

- `scripts/claude_twin_sync.py`
- `tests/test_claude_twin_sync.py`
- `.github/ai/label_contract.v1.json`
- `scripts/label_helpers.sh`
- `.github/workflows/claude-twin-sync.yml` (comments only)
- `.github/workflows/ci.yml` (comments only)
- `CLAUDE.md`, `agents.md`, `README.md`
- `docs/plans/issue-4785-twin-first-claude-sync-plan.md`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `changelog.d/5609-guard-synced-claude-scripts.md` [new]

## Tests

- Unit (`tests/test_claude_twin_sync.py`): `is_guard_path` for scripts; `plan_sync` marks a script copy `needs_owner`; `check_not_ahead` refuses an ordinary PR or push that edits a script with its twin, a script deletion, and allows a sync PR copying the base twin; the provenance rule covers scripts; `merge_check` refuses a script; `run_sync` labels a script sync PR and never merges it.
- Regression: the twin, label-contract, command, and doc suites (`pytest tests/test_claude_twin_sync.py tests/test_label_contract*.py tests/test_implement_plan_claude_command.py …`), `ruff`, and `claude_twin_sync.py check --base origin/main --head HEAD`.

## Risks & Mitigations

- Script syncs now wait for the owner, like hooks. ACCEPTED: that is the fix; the approval label and Telegram alert already exist.
- A PR that edits `.claude/scripts/**` directly (a watched session) now fails the CI check on `main`. ACCEPTED: edit the twin instead, as §28.C already requires.
- The parent project's in-flight conformance fix edits `scripts/claude_twin_sync.py` too. Mitigation: the step 2 sync merges its base in every stage; the change here touches constants, docstrings, and strings only.

## Rollout

Lands on the #4785 project branch and reaches `main` with that project's final PR. No flag, no migration. The next `claude-twin-sync.yml` run applies the new rule to any open sync PR (it gains the approval label and the `pending` owner status).

## Auto-decisions

- AD-1 [plan, 2026-09-30] How should synced scripts stop running with session privileges unreviewed? — Picked: A — add `scripts/` to `GUARD_PATH_PREFIXES`, so a script change reaches `.claude/` only through the owner-reviewed sync PR, with the #5246/#5247 CI rules. Alternatives: B — run synced scripts without session credentials and route writes through immutable least-privilege helpers; C — list the scripts in `UPSTREAM_ONLY_PATHS` so they are never synced. Why: A extends the existing guard (§5) and closes the auto-merge path; B is an architectural rewrite of seven scripts whose job is authenticated GitHub access; C brings back the protected-path stop for every script change. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Should `.claude/commands/**` become a guard path too? — Picked: A — no; commands stay non-guard. Alternatives: B — guard commands as well. Why: a command is instructions a session reads, at the same trust level as CLAUDE.md, and every action it asks for still passes the permission rules and the Auto-mode classifier; scripts and hooks execute without either (§5). Applied in: phase 1 (no change). Status: pending review
- AD-3 [plan, 2026-09-30] Should the posted `claude-twin-sync/owner-approval` description `No hook or settings change` change? — Picked: A — keep it; update the PR body text, label description, and docs. Alternatives: B — change it to name scripts. Why: it is only posted when no guard path changed, so it stays true, and changing it re-posts a status on every open non-guard head (§5, §15). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] How are the parent project's docs kept accurate? — Picked: A — correct the guard lines in the #4785 plan and add this project's own changelog fragment; leave the #4785 fragment as written. Alternatives: B — also edit `changelog.d/4785-twin-first-claude-sync.md`; C — change no parent doc. Why: the plan text is what the parent's activation check reads; a fragment belongs to its own PR (§20). Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- `docs/plans/retire-master-session-plan.md` describes a future guard classifier over #4785's guard set; it is left unchanged (another project's plan).

## References

- Issue #5609; audit tracker #3576; parent project #4785 (final PR #4804); #5246, #5247.

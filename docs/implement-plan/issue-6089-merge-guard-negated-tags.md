# Implement-Plan Log — §21 merged-PR guard: a later `--no-tags` cancels `git push --tags`

- Plan: docs/plans/issue-6089-merge-guard-negated-tags-plan.md
- Source issue: shubhodeep1/coding-workflows#6089
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5144-merge-guard-effective-repo
- Project branch: claude/implement-plan-issue-6089-merge-guard-negated-tags   Final PR: #6107 draft
- Status: BLOCKED until the `[claude-twin-sync]` commit for phase PR #6110 lands (twin sha256 `1b7a5be3…aca8`); IN_PROGRESS from that commit on
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #6110: twin sync, then its review rounds; next stage `conformance 1/3` once it merges
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-02
- Last note: phase 1 implemented twin-first in PR #6110 (2d8f077); 375 guard tests pass, only `test_template_copies_are_identical` red until the twin sync. Hold claim and twin-sync blocker posted on #6089; no wait armed (twin-first, step 4).

## Phases
1. [ ] Phase 1 — honour `--no-tags` in the §21 guard's push parser   — PR #6110 open (twin sync pending); review rounds: 0; interventions: 0 — protected paths: `.claude/hooks/pr_merge_status_guard.py`
   - `_PUSH_NO_TAGS_FLAG` + `_is_push_no_tags_flag`; `_push_refspec_targets` resets `pushes_tags` on it (last word wins)
   - Tests in `tests/test_pr_merge_status_guard.py` reading the twin: unit, target parsing, unresolvable-directory fallback, end-to-end block
   - CLAUDE.md §21.B wording; `changelog.d/6089-merge-guard-negated-tags.md`
   - Done: new tests pass against the twin; all other tests pass except `test_template_copies_are_identical` until the `[claude-twin-sync]` copy

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-02] Should this fix also honour `--no-delete` and the bulk-flag negations (`--no-all`, `--no-branches`, `--no-mirror`)? — Picked: A — no; only `--no-tags`. Alternatives: B — fold all negations in. Why: `--no-delete` is issue #6088's own project, and a cancelled bulk flag only adds a confirmation prompt, never a silent allow (§5, one issue per chain). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] Which spellings cancel `--tags`? — Picked: A — `--no-tags` and the prefixes git 2.43 accepts (`--no-ta`, `--no-tag`), last word wins in either order. Alternatives: B — only the exact `--no-tags`. Why: git accepts the prefixes (verified), so B leaves the same bypass for `--no-tag`. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Where does the changelog entry go? — Picked: A — a new fragment `changelog.d/6089-merge-guard-negated-tags.md`. Alternatives: B — edit the #5144 fragment. Why: §20.B one fragment per PR at a unique path; #6088 and #6090 change the same feature and could edit the same fragment. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the routine `dispatch shubhodeep1/coding-workflows#6089: start` in session session_012TKbuzqBLJ3WsxjQc8PvqE; permission mode auto.
- Progress comment: issue #6089 comment 5956490377.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-02)
- Final PR: #6107 (draft, base `claude/implement-plan-issue-5144-merge-guard-effective-repo`).
- Twin sync needed: `workflow-templates/.claude/hooks/pr_merge_status_guard.py` → `.claude/hooks/pr_merge_status_guard.py` (twin sha256 `1b7a5be35dc92fc0586d4b35841e8ef677521970b97e6b4c5fa48fb496aeaca8`; the `.claude/` copy is `d38f5eef…5ee9`, identical to the base twin); blocker posted on #6089.
- Sibling follow-ups from the same audit on the same base branch: #6088 (`--no-delete`), #6090 (`CDPATH`); each is its own project.

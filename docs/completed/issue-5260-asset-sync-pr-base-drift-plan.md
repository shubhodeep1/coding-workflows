# Check `.claude/` guard drift against a PR's own base in the Claude-asset sync

Source issue: shubhodeep1/coding-workflows#5260 (https://github.com/shubhodeep1/coding-workflows/issues/5260)
Base branch: claude/implement-plan-issue-4952-sync-claude-assets-at-session-start
Security pass: skip (ai:security: automation-produced issue)

## Summary

The Claude-asset sync added by #4952 decides whether a working branch is stale by diffing it against the default branch only. A PR head whose base (a project branch, or any other base) already holds a `.claude/hooks/**` or `.claude/settings.json` change the head lacks is reported fresh whenever the default branch has nothing new, so the fixer keeps running the older guard. This plan makes the sync check drift against the PR's own base as well, verify a synced project base, and merge the base in when only the base is ahead.

## Context

- Issue #5260 is an `ai:security` follow-up (medium, A05:2021 Security Misconfiguration) filed by `.github/workflows/security-audit.yml` against the #4952 project branch. It points at `.claude/commands/implement-plan-claude.md:182` on that branch: step 2 (**Stale?**) of `### Claude-asset sync` runs only `git diff --quiet HEAD...origin/<default> -- .claude/hooks .claude/settings.json`.
- Failure case: a PR head was cut from its project branch before that branch gained a guard change (the project's own `[claude-twin-sync]` of a hook, or a main merge the head predates only through the project branch). The default branch has no newer guard change than the head's merge base with it, so step 2 exits 0 and step 3 never runs. The session then works on the head with the old guard, which is exactly what #4952 set out to prevent.
- The same gap exists for bases the sync deliberately never merges the default branch into (`stable`, another pull request's head, a non-default `<issue base>`): a guard change on that base never reaches the PR head, although merging a PR's own base into it carries nothing unrelated.
- Callers of the procedure: `/implement-plan-claude` step 2 (the project branch itself), step 7 **Blocked**, step 7a (PR heads), `/implement-issue-claude` step 4 (through step 2), and `/fix-claude-pr` step 5 (PR head; it already fetches `<base ref>`).
- Related: #4952 (the sync, merged into its project branch as PR #5010, final PR #4995 still draft), #3576 (security audit tracker), #4785 (Actions twin sync).

## Goals

- G1. For a PR head, the sync reports staleness when **either** the default branch **or** the PR's base holds `.claude/hooks/**` / `.claude/settings.json` changes the head lacks (three-dot diffs against both).
- G2. For a PR head on a project branch that lands in the default branch, the sync syncs the project branch first (as today), then **verifies** the synced base holds every default-branch guard change before merging it into the head; a failed verification stops with the caller's blocker.
- G3. For a PR head on any other base, the sync merges `origin/<base>` into the head when the base is ahead on guards, and still never merges the default branch into it (default-only drift is recorded as `claude_assets=stale (base <base>)`, unchanged).
- G4. The sync stays local git only (no GitHub API calls, §15) and uses only git commands `.claude/settings.json` already allows.
- G5. Tests pin the new text in both copies of the command and run the documented commands in a scratch repository, reproducing the issue's scenario.

## Non-goals

- The SessionStart drift log in `.claude/hooks/session-start.sh` stays default-branch-only (AD-4): the hook cannot know a PR's base without a GitHub API call.
- Any change to step 2's project-branch merge itself, its subject, or its conflict rule.
- Syncing `.claude/commands/**` or `.claude/scripts/**` (unchanged #4952 non-goal).

## Constraints

- §5: extend the existing `### Claude-asset sync` steps; no new mechanism, no new script.
- §6: no identifier renamed. The `[claude-asset-sync]` subject, the `claude_assets=stale (base <base>)` record, and the section anchor stay as they are.
- §15: local git only; the extra work is one more `git fetch` of a ref the caller already fetches and one more `git diff`.
- §18: no manual script. Commands used are covered by the existing allow rules (`git fetch *`, `git diff *`, `git merge *`, `git log *`, `git show *`, `git push origin claude/*`); `git merge-base` is not allowlisted, so the verification uses `git diff --quiet` (AD-3).
- §20: one new `changelog.d/` fragment for this PR; #4952's fragment is not edited (AD-5).
- §21: merge commits only; never rebase or force-push.
- §28.C / protected paths: every command edit is under `.claude/**`. In coding-workflows the interim twin-first default applies: edit only the `workflow-templates/.claude/**` twins, and the twin-sync blocker stops the project until the `[claude-twin-sync]` copy.

## Approach

Rewrite steps 1–3 of `### Claude-asset sync` (the rest is unchanged):

1. **Fetch.** `git fetch origin <default>`; for a PR head also `git fetch origin <base>` (`<base>` is the PR's base ref).
2. **Stale?** Two three-dot checks, both against the merge base so a branch's own guard edits are never staleness:
   - `git diff --quiet HEAD...origin/<default> -- .claude/hooks .claude/settings.json` (default drift);
   - for a PR head whose base is not the default branch, `git diff --quiet HEAD...origin/<base> -- .claude/hooks .claude/settings.json` (base drift). The base can hold a guard change the head lacks while the default branch has nothing new (the #5260 case).
   Both exit 0 → nothing to do. The project branch itself needs no base check: step 2's merge of `origin/<issue base>` (or the default branch) already brings its base in.
3. **Which branch to merge?**
   - Base is the default branch → merge the default branch (as today).
   - Base is a `claude/implement-plan-<slug>` project branch that lands in the default branch → sync the project branch first exactly as step 2 does, then **verify the project base**: `git fetch origin <project branch>` and `git diff --quiet origin/<project branch>...origin/<default> -- .claude/hooks .claude/settings.json` must exit 0 (the pushed base holds every default-branch guard change). A non-zero exit (the sync was not pushed, or stopped) is a stop with the caller's blocker, as in step 5. Then merge `origin/<project branch>` into the PR head, whether the drift was the default branch's or the base's.
   - Any other base → never merge the default branch. With base drift, merge `origin/<base>` into the PR head: a PR lands in its base, so merging that base carries nothing unrelated into it. With default drift only, skip and record `claude_assets=stale (base <base>)` (as today).

Step 4 (`<source>` also covers `<base>`), step 5, and step 6 keep their commands. `/fix-claude-pr` step 5's stop covers the new verification failure as well as a `.claude/` conflict.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the change is one section of one command plus its tests and docs, and cannot be split into independently useful parts.

1. **Phase 1 — base-aware drift check in the Claude-asset sync.**
   - Files: `workflow-templates/.claude/commands/implement-plan-claude.md`, `workflow-templates/.claude/commands/fix-claude-pr.md` (twins; the `.claude/` copies follow via `[claude-twin-sync]`), `tests/test_claude_asset_sync_command.py`, `agents.md`, `changelog.d/5260-asset-sync-pr-base-drift.md` [new].
   - Done: the twin's `### Claude-asset sync` carries both checks, the project-base verification, and the base merge for other bases; `/fix-claude-pr` step 5's stop names the verification failure; `agents.md` describes both checks; the fragment exists; `tests/test_claude_asset_sync_command.py` passes for the `workflow-templates/.claude/commands` parametrization (and for both once the twins are copied), including the scratch-repository scenario where the default check exits 0 and the base check exits 1.
   - Rollback: revert the phase PR; the sync returns to the default-only check.

## Implementation Steps

1. Phase 1 — `workflow-templates/.claude/commands/implement-plan-claude.md` `### Claude-asset sync` steps 1–4: the fetch, the two drift checks, the project-base verification, the base merge for other bases, `<source>` covering `<base>`.
2. Phase 1 — `workflow-templates/.claude/commands/fix-claude-pr.md` step 5: the stop applies when the sync's merge conflicts under `.claude/` or its project-base verification fails.
3. Phase 1 — `tests/test_claude_asset_sync_command.py`: text assertions for the new checks and verification; an executed scenario (scratch repo: default branch, project branch with a later hook change, PR head cut before it) showing the default check exits 0, the base check exits 1, and the documented merge of the base clears it; an executed verification check (exit 0 on a synced base, non-zero on a base lacking a default-branch hook change); the fixer text.
4. Phase 1 — `agents.md` Claude-asset sync bullet; `changelog.d/5260-asset-sync-pr-base-drift.md`.

## Files & Modules

- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/fix-claude-pr.md`
- `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md` (byte-identical copies via the twin sync, not edited by this session)
- `tests/test_claude_asset_sync_command.py`
- `agents.md`
- `changelog.d/5260-asset-sync-pr-base-drift.md` [new]

## Tests

- Unit (text): `tests/test_claude_asset_sync_command.py`, both parametrizations; the `.claude/commands` one and the twin-parity checks fail until the `[claude-twin-sync]` copy, as expected under twin-first.
- Unit (executed git): the new scratch-repository tests run the commands as documented.
- Regression: `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_session_start_claude_assets_drift.py`, `tests/test_update_workflows_guardrails.py`.

## Risks & Mitigations

- A PR head merges its non-default base more often than before → only when that base holds guard changes the head lacks; the merge is the ordinary "bring the base in" merge a conflict round already does. ACCEPTED.
- The verification stops a fixer when the project-branch push failed → that is the intended fail-closed outcome; the stop names the check.
- The twin sync blocks the project until an operator copies the twins → by design (§28.C interim default).

## Rollout

Lands on the #4952 project branch, then reaches `main` with #4995 and consumers with the next `@stable` sync of `.claude/`. No flag; commands are read at use time.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which branches define staleness for a PR head? — Picked: A — both the default branch and the PR's base (two three-dot diffs). Alternatives: B — the PR's base only; C — the default branch only (today). Why: the issue asks for both; B would miss a default-branch fix the base has not merged yet. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] A PR head on a base that does not land in the default branch lacks its base's guard change: merge the base in? — Picked: A — merge `origin/<base>` into the head. Alternatives: B — skip and record `claude_assets=stale (base <base>)` as for default-only drift. Why: a PR lands in its base, so merging it carries nothing unrelated, and B leaves the old guard running. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How is the synced project base verified? — Picked: A — `git fetch origin <project branch>` then `git diff --quiet origin/<project branch>...origin/<default> -- .claude/hooks .claude/settings.json`, stop with the caller's blocker on a non-zero exit. Alternatives: B — `git merge-base --is-ancestor origin/<default> origin/<project branch>`; C — no verification. Why: A uses an allowlisted command (B's `git merge-base` would prompt in an unattended session) and checks exactly the guarded paths; the issue asks for verification. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should the SessionStart drift log also check the PR's base? — Picked: A — no, keep it default-only. Alternatives: B — compare against the branch's upstream tracking ref. Why: the hook cannot learn a PR's base without a GitHub API call (§15), the log is diagnostic only, and the command sync is where the merge happens (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] How is the changelog updated? — Picked: A — a new `changelog.d/5260-asset-sync-pr-base-drift.md`, #4952's fragment unchanged. Alternatives: B — edit `changelog.d/4952-claude-asset-sync.md`. Why: §20.B one fragment per PR; both fragments release together with #4995. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5260` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Issue base `claude/implement-plan-issue-4952-sync-claude-assets-at-session-start` is the head of the open draft final PR #4995 (base `main`), so it lands in the default branch.

## References

- #5260, #4952, PR #5010, PR #4995, #3576, #4785
- `.claude/commands/implement-plan-claude.md#claude-asset-sync` (on the #4952 project branch)

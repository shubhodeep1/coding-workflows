# Never continue a Claude fix on stale guards after a Claude-asset sync conflict

Source issue: shubhodeep1/coding-workflows#5258 (https://github.com/shubhodeep1/coding-workflows/issues/5258)
Base branch: claude/implement-plan-issue-4952-sync-claude-assets-at-session-start
Security pass: skip (ai:security: automation-produced issue)

## Summary

The Claude-asset sync (issue #4952) merges the default branch's `.claude/` guard updates into a working branch before a fixer touches it. When that merge conflicts only outside `.claude/`, `/fix-claude-pr` aborts it and keeps fixing on the unsynced head, so a privileged fix runs under the outdated guards the sync existed to replace. This plan resolves such a conflict inside the sync merge, or fails closed, and never continues on the unsynced head.

## Context

- Security audit finding `asset-sync-outside-conflict-fail-open` (high, confidence 9/10, A05:2021 Security Misconfiguration), issue #5258, filed against the #4952 project branch (`Refs #3576`, the audit tracker).
- `.claude/commands/fix-claude-pr.md:46` (step 5, on the base branch): "A conflict only outside `.claude/` aborts the sync merge and the fix continues on the unsynced head."
- `.claude/commands/implement-plan-claude.md:185` (Claude-asset sync, step 5): "A conflict only outside `.claude/` follows the caller's own conflict rule (step 2 resolves it; `/fix-claude-pr` aborts the sync merge, records `claude_assets=stale (conflict outside .claude/)`, and continues its fix)." For the PR-head callers of that command (step 7 **Blocked**, step 7a review round) no conflict rule is named at all, and `/fix-claude-pr` hands every `claude/implement-plan-*` head to those steps.
- A session runs the hooks from its working tree (`"$CLAUDE_PROJECT_DIR"/.claude/hooks/<hook>`), and hook scripts are re-read on every call. While a merge stops on conflicts, Git has already written every cleanly merged path, including the default branch's `.claude/hooks/**`, into the working tree. Resolving the conflict inside the sync merge therefore runs the whole resolution, and every later fix step, under the current guards.
- Claude-asset sync step 3 merges only when the working branch lands in the default branch, and its source is always the PR's base (the default branch, or the project branch the PR targets). A sync merge that conflicts is therefore the same conflict the PR must resolve before it can merge anyway.
- Constraints that bind: CLAUDE.md §1 (security first), §5 (minimal change), §6 (naming immutability), §12 conflict rules, §20 (changelog fragment), §21 (no rebase or force-push), §28.C (protected-path edits; failure escalations still stop).

## Goals

- After a Claude-asset sync that conflicts only outside `.claude/`, no fixer (`/fix-claude-pr`, `/implement-plan-claude` steps 7 and 7a) continues on the unsynced head.
- Such a conflict is resolved inside the sync merge under the caller's conflict rules, keeping both sides' intent, and committed with the sync merge's documented subject.
- When the right resolution is not evident, the session runs `git merge --abort` and stops the way the caller stops on an unresolvable conflict (in `/fix-claude-pr`, its hold claim, §2 question, and `PushNotification`). This is the fail-closed path the finding asks for.
- The command text, `agents.md`, and a `changelog.d/` fragment describe the new rule, and `tests/test_claude_asset_sync_command.py` pins it, including a scratch-repository test showing the default branch's hook is in the working tree while the merge is stopped on an outside conflict.

## Non-goals

- Changing the `.claude/` conflict rule (abort and stop with the caller's blocker). It is already fail-closed.
- Changing Claude-asset sync step 3, which skips the merge for a branch that does not land in the default branch (`stable`, another PR's head, a non-default issue base). That is not an aborted sync. Merging the default branch there would carry unrelated commits into that base. See AD-4.
- Overlaying `.claude/` files from the default branch without a merge (AD-1 option C).
- The SessionStart drift log (`.claude/hooks/session-start.sh`); it only reports.

## Constraints

- **§28.C protected paths.** Both command files live under `.claude/commands/`. Under the interim twin-first default (until #4785) the phase edits only the `workflow-templates/.claude/commands/` twins. The copy into `.claude/` happens through the twin-sync blocker. Until that copy lands, the template-parity tests (`test_twins_are_byte_identical`) stay red.
- **§6.** No identifier is renamed. The report phrase `claude_assets=stale (conflict outside .claude/)` is dropped because the state it described no longer occurs. It has never reached the default branch, and nothing parses it (AD-5).
- **§15.** No GitHub API calls are added: the sync stays local git only.
- **§21.** Still never rebase or force-push; the sync is a merge commit.

## Approach

Change Claude-asset sync step 5 (one shared rule for every PR-head caller) and `/fix-claude-pr` step 5 so that a conflict only outside `.claude/`:

1. is resolved inside the sync merge, keeping both sides' intent (CLAUDE.md §12 conflict rules; lockfiles and generated files regenerated with the repo's tooling), then committed with `git commit --no-edit`. That keeps the `[claude-asset-sync] merge <source> for .claude/ guard updates` subject `-m` set, which Git stores in `MERGE_MSG` (AD-3);
2. when the right resolution is not evident, is aborted (`git merge --abort`) and stops the fix the way the caller stops on an unresolvable conflict. The fix never continues on the unsynced head.

For `/fix-claude-pr` `kind=conflict`, that sync merge is the conflict fix. The `conflict` bullet's `git merge --no-edit origin/<base ref>` then finds nothing left to merge. The project branch's own sync (implement-plan step 2) keeps its existing rule: it resolves an outside conflict and aborts on a `.claude/` one.

Alternatives are recorded as AD-1.

## Phases & Merge Strategy

A single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — resolve or fail closed on an outside-`.claude/` sync conflict.** protected paths: `.claude/commands/fix-claude-pr.md`, `.claude/commands/implement-plan-claude.md` (edited through their `workflow-templates/.claude/commands/` twins).
   - Files: `workflow-templates/.claude/commands/fix-claude-pr.md`, `workflow-templates/.claude/commands/implement-plan-claude.md`, `tests/test_claude_asset_sync_command.py`, `agents.md`, `changelog.d/5258-asset-sync-outside-conflict-fail-closed.md` [new].
   - Done when: neither twin says the fix continues on the unsynced head, both carry the resolve-or-abort rule, the new tests pass against the twins, the existing asset-sync tests still pass for both directories apart from the parity tests the pending twin sync turns green, and the scratch-repository test shows the default branch's hook in the working tree during a stopped merge.
   - Rollback: revert the phase PR. The commands fall back to the #4952 text.

## Implementation Steps

1. `workflow-templates/.claude/commands/implement-plan-claude.md`, Claude-asset sync step 5: replace the sentence on conflicts only outside `.claude/` with the shared rule (resolve inside the sync merge and commit with `git commit --no-edit`; when not evident, `git merge --abort` and stop like the caller's unresolvable conflict; never continue on the unsynced head; step 2 on the project branch resolves as before). State why: the sync's source is always the PR's base (step 3), and the cleanly merged `.claude/` files are already in the working tree while the merge is stopped.
2. `workflow-templates/.claude/commands/fix-claude-pr.md`, step 5: replace "A conflict only outside `.claude/` aborts the sync merge and the fix continues on the unsynced head." with the same rule, including the hold claim, §2 question, and `PushNotification` for the not-evident case. Also say that for `kind=conflict` the sync merge is the conflict fix.
3. `tests/test_claude_asset_sync_command.py`: add twin-only contract tests (the rule text in both twins, the removed "continues on the unsynced head" wording), plus a scratch-repository test. That test builds a branch whose merge with `main` conflicts in `work.md` while `main` also changes `.claude/hooks/guard.sh`. It runs the documented merge command, asserts the hook's new content is in the working tree with `work.md` unmerged, then resolves the conflict, runs `git commit --no-edit`, and asserts the `[claude-asset-sync]` subject and two parents.
4. `agents.md`, the Claude-asset sync bullet: add the outside-conflict rule in one or two sentences.
5. `changelog.d/5258-asset-sync-outside-conflict-fail-closed.md`: a `security` fragment per §20.

## Files & Modules

- `workflow-templates/.claude/commands/fix-claude-pr.md`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `.claude/commands/fix-claude-pr.md`, `.claude/commands/implement-plan-claude.md` (byte-identical copies of the twins, applied by the twin sync, not by this phase's session)
- `tests/test_claude_asset_sync_command.py`
- `agents.md`
- `changelog.d/5258-asset-sync-outside-conflict-fail-closed.md` [new]

## Tests

- Unit and contract: `python3 -m pytest -q -p no:cacheprovider tests/test_claude_asset_sync_command.py tests/test_session_start_claude_assets_drift.py` (the existing `ci.yml` step). Before the twin sync, only `test_twins_are_byte_identical[fix-claude-pr.md]` and `[implement-plan-claude.md]` are expected to fail.
- The scratch-repository test runs real `git` in `tmp_path` with the reflog off, as the existing merge-command test does.
- Regression: `tests/test_implement_plan_claude_command.py`, `tests/test_check_in_status_hand_back.py`, and `tests/test_check_in_session_targeting.py` read these commands, so they are re-run.

## Risks & Mitigations

- A fixer resolves an outside conflict wrongly. Mitigation: the same §12 conflict rules the `conflict` kind already uses, and a not-evident conflict aborts and stops. The resolution rides the PR and gets the next reviewer round.
- A `review` or `ci` fix now also carries a conflict resolution. ACCEPTED: the conflict is with the PR's own base and blocks its merge anyway. The reviewer panel re-reviews the pushed head.
- The parity tests are red until the twin sync. ACCEPTED: the documented interim twin-first flow (#4785). The phase PR is held until the `[claude-twin-sync]` copy.

## Rollout

Commands take effect for every session that checks out the branch carrying them. The change reaches the default branch with the #4952 project's final PR (this plan's base branch), and consumer repos with the next `@stable` `.claude/` sync (§14). No flags, no data changes.

## Auto-decisions

- AD-1 [plan, 2026-09-29] How should a Claude fix handle a Claude-asset sync merge that conflicts only outside `.claude/`? — Picked: A — resolve the conflict inside the sync merge under the caller's §12 conflict rules, and when the resolution is not evident, `git merge --abort` and stop like the caller's unresolvable conflict (fail closed); never continue on the unsynced head. Alternatives: B — always fail closed: abort, hold, and wait for a human; C — overlay the default branch's `.claude/hooks` and `settings.json` on the working tree (`git checkout origin/<default> -- …`) and continue on the unsynced head. Why: A runs the whole fix under the current guards. It does not park every routine conflict PR on a human (B; §18), and it cannot discard the branch's own guard edits or slip unreviewed guard files into the fix commit (C). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Should the rule cover only `/fix-claude-pr` or every PR-head caller of the Claude-asset sync? — Picked: A — every PR-head caller, as one shared rule in the Claude-asset sync step 5 (`/implement-plan-claude` steps 7 and 7a have no outside-conflict rule, and `/fix-claude-pr` delegates `claude/implement-plan-*` heads to them). Alternatives: B — `/fix-claude-pr` only (the finding's location). Why: the same gap sits in the same flow, and one rule keeps the callers consistent. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which commit subject does a sync merge that resolved a conflict carry? — Picked: A — the sync's documented `[claude-asset-sync] merge <source> for .claude/ guard updates` (kept by `git commit --no-edit` from `MERGE_MSG`). Alternatives: B — `[claude-merge-resolve] merge <source>`. Why: one documented subject, and both subjects break the review workflow's consecutive `[claude-autofix]` count the same way (`review_autofix.yml`, "Count consecutive [ai-autofix] commits"). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should the phase also stop fixes on branches the sync skips (step 3: a base other than the default branch)? — Picked: A — no; record it as a non-goal. Alternatives: B — fail closed on every skipped sync too. Why: the finding names the aborted-merge path only, and B would stop every fix on a `stable`- or PR-based branch (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] What happens to the report phrase `claude_assets=stale (conflict outside .claude/)`? — Picked: A — drop it: the state no longer occurs, it has never reached the default branch, and nothing parses it. Alternatives: B — keep it documented as an alias. Why: keeping a phrase for a state that cannot happen would mislead readers (§7); §6 covers shipped identifiers, and this one never shipped. Applied in: phase 1 PR. Status: pending review

## References

- Issue #5258 (this finding), issue #4952 and its project branch / draft final PR #4995, audit tracker #3576.
- Interim twin-first protected-path default: CLAUDE.md §28.C, #4785.

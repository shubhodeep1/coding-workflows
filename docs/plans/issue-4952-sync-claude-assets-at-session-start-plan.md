# Sync the default branch's `.claude/` guards into long-running working branches

Source issue: shubhodeep1/coding-workflows#4952 (https://github.com/shubhodeep1/coding-workflows/issues/4952)
Base branch: main
Security pass: run

## Summary

A session runs the hooks of the branch it has checked out, so a project or PR branch cut before a guard fix keeps running the old guard (the #4619 / PR #4641 session was 43 commits behind `main` and lacked #4704). This plan makes `/implement-plan-claude` stages, `/implement-issue-claude` resumes, and `/fix-claude-pr` merge the default branch into the working branch whenever the default branch holds `.claude/hooks/**` or `.claude/settings.json` changes the branch lacks. A `.claude/**` conflict stops the session with a precise blocker. The SessionStart hook logs one diagnostic line when the checkout is stale.

## Context

- Issue #4952 (incident 2026-09-29): `claude/implement-plan-issue-4619-gh-api-guard-file-backed-fields` ran a `gh_api_write_guard.py` without #4704's quote-aware substitution scanning. The guard answered `ask` on a plain `grep`/`sed` read of `CLAUDE.md`.
- `.claude/settings.json` wires every hook as `"$CLAUDE_PROJECT_DIR"/.claude/hooks/<hook>`, so each hook call runs the file in the current checkout. `settings.json` itself is read once, at session start.
- `/implement-plan-claude` step 2 already merges `origin/<default>` into the project branch at the start of every stage (issue mode: `origin/<issue base>`). The gap is the other working branches: phase and fix PR heads checked out in step 7 (blocked intervention) and step 7a (review round), which fork from the project branch and never merge it again. `/fix-claude-pr` step 5 checks out the PR head with no sync at all.
- `.claude/hooks/session-start.sh` logs `[session-start] …` lines. Its `main()` runs only when `CLAUDE_CODE_REMOTE=true`, and the file can be sourced by tests without running `main`.
- Related: #4704 (the fix the stale branch lacked), #4619 / PR #4641 (the affected project), #4938 (session environment self-heal), #4785 (Actions sync PR for `.claude/` changes).

## Goals

- G1. A working branch whose integration line ends in the default branch picks up newer `.claude/hooks/**` and `.claude/settings.json` from the default branch before the session does other work on it, with no human action.
- G2. When that merge conflicts in `.claude/**`, the session aborts the merge and stops with a blocker that names the conflicting files and the commits on each side (a §28.C stop, never a guess).
- G3. When the merge changes `.claude/settings.json`, the session records that the new wiring applies from the next session.
- G4. `session-start.sh` logs `[session-start] claude_assets=stale behind=<n> files=<list>` when the checkout lacks default-branch changes to those paths. The check never fails the hook and never touches the network when offline.
- G5. The sync issues no GitHub API calls (§15). Only local git plus one `git fetch` through the git remote.
- G6. Tests pin the command text in all three commands and in their `workflow-templates/.claude/**` twins, and the session-start log line. A `changelog.d/` fragment ships (§20).

## Non-goals

- Syncing `.claude/commands/**` or `.claude/scripts/**`. The issue scopes staleness to hooks and `settings.json`. Commands and scripts are read by the model from the checkout it works in, and step 2 already refreshes them on the project branch.
- Re-loading `settings.json` inside a running session. The harness reads it once; G3 records the limitation.
- Landing `.claude/**` changes without a watched session. That is #4785.
- Merging the default branch into branches bound for a non-default base (see AD-2).

## Constraints

- §5: extend step 2's existing sync and `/fix-claude-pr` step 5. No new mechanism competes with them.
- §6: no identifier is renamed. The new log key `claude_assets` and its fields are new and are documented in `agents.md`'s stable log prefixes.
- §9: `session-start.sh` keeps its existing 2-space bash indentation (the file's own convention). Tests use tabs like the neighbouring test files.
- §15: local git only. No `gh api` and no MCP call is added.
- §18: no new script. The sync is command text using git commands `.claude/settings.json` already allows (`git fetch *`, `git diff *`, `git merge *`, `git log *`, `git show *`, `git push origin claude/*`). `git rev-list` appears only inside the hook, which the harness runs without a permission check.
- §21: merge commits only. Never rebase or force-push.
- §28.C: a `.claude/**` conflict is a failure escalation, reported on the issue in issue mode.
- Protected paths (§28.C, Q40/Q62/Q64 in `docs/operations/master-session.md`): every file this phase edits in `.claude/**` needs a watched session or the twin-first route. `session-start.sh` is a hook, so copying it into `.claude/` needs the operator's approval window.

## Approach

**One shared procedure, three callers.** A new `### Claude-asset sync` subsection under `## Helpers` in `implement-plan-claude.md` defines the procedure once, and the three commands cite it:

1. `git fetch origin <default>`.
2. **Stale?** `git diff --quiet HEAD...origin/<default> -- .claude/hooks .claude/settings.json`. The three-dot form compares against the merge base, so it reports only changes the default branch holds and the working branch lacks. A branch's own edits to a guard (like #4619's) are not staleness. Exit 0 → nothing to do.
3. **Lands in the default branch?** Merge only when the working branch's integration line ends in the default branch (AD-2):
   - its base (the PR's base ref, or for a project branch the plan's `Base branch:`) is the default branch; or
   - its base is a `claude/implement-plan-<slug>` project branch whose plan (`git show origin/<base>:docs/plans/<slug>-plan.md`) has no `Base branch:` line or names the default branch. Then the project branch is synced first, exactly as step 2 does, and the working branch merges `origin/<project branch>` so its PR diff stays limited to its own change (AD-3).
   
   Otherwise skip the merge, record `claude_assets=stale` in the report (and the log's `## Notes` in a stage session), and continue.
4. **Merge.** `git merge --no-edit origin/<default>` (or `origin/<project branch>`), a merge commit with subject `[claude-asset-sync] merge <source> for .claude/ guard updates`. Never rebase or force-push. Push with the push the caller already uses.
5. **Conflict.** Run `git diff --name-only --diff-filter=U`. If any conflicted path is under `.claude/`, run `git merge --abort` and stop with the caller's blocker (AD-4). Name the conflicting files and the commits on each side (`git log --oneline <merge-base>..HEAD -- <file>` and `git log --oneline <merge-base>..origin/<source> -- <file>`). A conflict only outside `.claude/` is handled by the caller's existing conflict rule: step 2's resolution in `/implement-plan-claude`, or the abort-and-continue path in `/fix-claude-pr` (AD-5).
6. **Settings.** When `git diff --name-only HEAD@{1} HEAD -- .claude/settings.json` is non-empty after the merge, record `settings.json changed by the asset sync; new hook wiring applies from the next session` in the report. Hook scripts are re-read on every call, so hook fixes apply at once.

**Callers.**
- `/implement-plan-claude` step 2 (project-branch sync): cite the procedure's conflict rule for `.claude/**` (it overrides "resolve keeping both sides' intent" for those paths only). In issue mode with a non-default `<issue base>`, apply step 3's skip-and-record.
- `/implement-plan-claude` steps 7 (Blocked intervention) and 7a (review round), right after checking out the PR head: run the procedure on the PR head.
- `/implement-issue-claude` step 4 (resume): state that the resumed chain runs the sync through `/implement-plan-claude` step 2. There is no extra step.
- `/fix-claude-pr` step 5, right after `git checkout -B <head ref> origin/<head ref>` and the HEAD check: run the procedure before any fix. Its blocker is a `hold` claim plus one PR comment starting `<!-- ai:claude-blocked:v1 -->` and one `PushNotification` (AD-4).

**Diagnostics.** A new `report_claude_assets_drift` function in `session-start.sh`, called from `main()` after `verify_token` and wrapped so it can never fail the hook (`|| true` plus internal `return 0` on every error path):
- Skip silently when the directory is not a git work tree.
- Default branch from `git symbolic-ref --quiet --short refs/remotes/origin/HEAD` (strip `origin/`), else `main`.
- One bounded refresh: `GIT_TERMINAL_PROMPT=0 timeout 15 git fetch --quiet --no-tags origin <default>`. Any failure (offline, timeout, auth) is ignored and the existing `origin/<default>` ref is used. With no ref, skip silently (AD-6).
- Stale when `git diff --quiet HEAD...origin/<default> -- .claude/hooks .claude/settings.json` exits 1. Log `[session-start] claude_assets=stale behind=<n> files=<comma-separated list>`. `behind` is `git rev-list --count HEAD..origin/<default>`; `files` is `git diff --name-only HEAD...origin/<default> -- .claude/hooks .claude/settings.json`. When the merge base is unavailable (shallow history), fall back to the two-dot diff and log `behind=unknown`.
- Inside the existing `CLAUDE_CODE_REMOTE=true` gate, so local sessions stay unaffected (AD-7).

Alternatives considered: a helper script `.claude/scripts/sync_claude_assets.py` (rejected in AD-1: it needs a new `settings.json` allow rule, which needs the Q62/Q64 approval window, while plain git commands are already allowed); always merging the default branch (rejected in AD-2).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue is one change to three commands, one hook, their twins, and their tests.

1. **Phase 1 — asset sync in the commands, drift log in the hook, tests, docs.**
   - Scope: the whole change.
   - protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/implement-issue-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/hooks/session-start.sh` (plus their `workflow-templates/.claude/**` twins, edited first per the issue and Q40).
   - Done when: the command text and twins carry the procedure and the three call sites; `session-start.sh` and its twin log the stale line; the new tests and the existing suites below pass; the fragment and `agents.md` entry exist.
   - Rollback: revert the phase PR. The commands fall back to today's behaviour, and the hook loses one log line.

## Implementation Steps

Phase 1:
1. `workflow-templates/.claude/commands/implement-plan-claude.md`: add `### Claude-asset sync` under `## Helpers`; cite it in step 2 (the `.claude/**` conflict override and the non-default-base skip), step 7 **Blocked**, and step 7a. Copy to `.claude/commands/implement-plan-claude.md` (byte-identical).
2. `workflow-templates/.claude/commands/implement-issue-claude.md` step 4: one sentence that the resume runs the asset sync through `/implement-plan-claude` step 2. Copy to `.claude/`.
3. `workflow-templates/.claude/commands/fix-claude-pr.md` step 5: run the Claude-asset sync after the checkout and HEAD check, with the hold-claim blocker. Copy to `.claude/`.
4. `workflow-templates/.claude/hooks/session-start.sh`: add `report_claude_assets_drift` and call it from `main()`. Copy to `.claude/hooks/session-start.sh`.
5. Tests (below).
6. `agents.md`: add the `[session-start] claude_assets=stale` line to `## Stable log prefixes (contractual)`, and a short paragraph on the asset sync under `## Unattended helpers and permission prompt reports (CLAUDE.md §23.I)`.
7. `changelog.d/4952-claude-asset-sync.md` (`<!-- changelog: fixed -->`).

## Files & Modules

- `.claude/commands/implement-plan-claude.md`, `workflow-templates/.claude/commands/implement-plan-claude.md`
- `.claude/commands/implement-issue-claude.md`, `workflow-templates/.claude/commands/implement-issue-claude.md`
- `.claude/commands/fix-claude-pr.md`, `workflow-templates/.claude/commands/fix-claude-pr.md`
- `.claude/hooks/session-start.sh`, `workflow-templates/.claude/hooks/session-start.sh`
- `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`
- `tests/test_claude_asset_sync_command.py` [new] (the `/fix-claude-pr` text)
- `tests/test_session_start_extract_repo_slug.py` (drift-log cases)
- `agents.md`
- `changelog.d/4952-claude-asset-sync.md` [new]

## Tests

- **Command text** (unit): assert that each command and its twin cite `Claude-asset sync`, the three-dot `git diff --quiet HEAD...origin/<default> -- .claude/hooks .claude/settings.json` trigger, the merge-commit rule (no rebase, no force-push), the `.claude/**` conflict abort with the `ai:claude-blocked:v1` blocker, the non-default-base skip, and the `settings.json`-applies-next-session note. Twin byte-parity for all three commands.
- **Session-start log** (unit, in temporary git repos, no network): a stale checkout logs `claude_assets=stale behind=<n> files=.claude/hooks/<f>`; an up-to-date checkout logs nothing; a branch that only edits its own guard logs nothing; a missing `origin/<default>` ref, a non-git directory, and a failing `git fetch` (remote pointing at a nonexistent path) all exit 0 without the line. Hook/twin byte-parity (existing).
- **Existing suites**: `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_check_in_status_hand_back.py` (fix-claude-pr parity), `tests/test_session_start_extract_repo_slug.py`, `tests/test_update_workflows_guardrails.py`. Each new test file gets a `ci.yml` step next to its neighbours.

## Risks & Mitigations

- Merging the default branch into a PR head adds default-branch commits to the PR diff when the base lags. Mitigation: AD-3 syncs the project branch first and merges the project branch into the PR head.
- The asset sync starts a new review round on the PR head, because the push is a new head. ACCEPTED: it happens only when the default branch holds newer guards, and a guard fix is worth one round.
- The hook's fetch adds latency at session start. Mitigation: at most one bounded 15-second fetch of one branch, with failures ignored.
- A `.claude/**` conflict stops a project that edits its own guard, like #4619. ACCEPTED: the issue requires this stop.
- The phase edits protected paths, so it cannot run unattended. Mitigation: the chain stops before phase 1 and asks, per §28.C (Q40 twin-first is the standing answer).

## Rollout

The change reaches consumer repos with the next `@stable` sync of `workflow-templates/.claude/**`. No feature flag: the sync is a no-op until the default branch holds newer guards. To roll back, revert the PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where does the sync live? — Picked: A — command text using git commands `.claude/settings.json` already allows. Alternatives: B — a new `.claude/scripts/sync_claude_assets.py` helper (needs a new allow rule, so a `settings.json` change and the Q62/Q64 approval window). Why: the issue asks for command text and tests on it; A adds no permission surface (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Which working branches get the default branch merged in? — Picked: A — only branches whose integration line ends in the default branch; any other base (`stable`, another PR's head, a non-default issue base) skips the merge and records `claude_assets=stale`. Alternatives: B — always merge the default branch; C — overlay the `.claude/` files without committing. Why: merging the default branch into a branch bound for `stable` or another PR's head would carry unrelated default-branch commits into that base (§1 correctness); C risks committing the overlay by accident. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] A PR head whose base is a project branch that lags the default branch? — Picked: A — sync the project branch first (step 2's merge), then merge `origin/<project branch>` into the PR head. Alternatives: B — merge the default branch straight into the PR head; C — skip PR heads on project branches. Why: A brings the guard fix to both branches while the PR diff stays limited to its own change; B shows every lagging default-branch commit in the PR under review; C leaves exactly the #4641 case unfixed. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Where does a `/fix-claude-pr` session report a `.claude/**` conflict? — Picked: A — a `hold` claim plus one PR comment starting `<!-- ai:claude-blocked:v1 -->` naming the files and commits, and one `PushNotification`. Alternatives: B — comment on the project's source issue with the `ai:claude-blocked` label. Why: the hold claim is `/fix-claude-pr`'s existing stop and keeps the sweep from starting a second fixer; a standalone `claude/*` PR may have no source issue. In `/implement-plan-claude` issue mode the blocker goes on the issue, as the issue asks. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] A sync merge that conflicts only outside `.claude/`? — Picked: A — the caller's existing conflict rule applies (step 2 resolves it; `/fix-claude-pr` aborts the sync merge, records `claude_assets=stale`, and continues its own fix). Alternatives: B — stop on any conflict. Why: the issue makes only `.claude/**` conflicts a stop; step 2 already resolves other conflicts, and a fixer should not take on a base-merge it was not asked for. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] What does "must not fetch when offline" mean for the hook? — Picked: A — at most one bounded fetch (`timeout 15`, `GIT_TERMINAL_PROMPT=0`), with every failure ignored and the existing `origin/<default>` ref used. Alternatives: B — never fetch, and compare against the local ref only. Why: a cloud clone's ref is current at clone time, but a `source_revision` clone may lack the default branch; A never blocks or fails the hook when offline. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] Does the drift log run in local sessions too? — Picked: A — only inside the existing `CLAUDE_CODE_REMOTE=true` gate. Alternatives: B — in every session. Why: the hook documents that local sessions are unaffected (§5, §6 behaviour stability). Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] Which diff decides "stale"? — Picked: A — three-dot (`HEAD...origin/<default>`): only changes the default branch holds and the checkout lacks. Alternatives: B — two-dot (`origin/<default> HEAD`, as the issue text writes it): any difference. Why: B would flag, and try to merge, on a branch's own guard edits (the #4619 case), which are not staleness; A matches the issue's stated intent ("a difference that the default branch holds and the working branch lacks"). Applied in: phase 1. Status: pending review

## References

- Issue #4952; #4704; #4619 / PR #4641; #4938; #4785
- `docs/operations/master-session.md` (Q40, Q62/Q64)
- CLAUDE.md §5, §8, §15, §18, §20, §21, §28

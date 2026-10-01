# Implement-Plan Log — Guard differential check: fail closed on settings-only guard disablement

- Plan: docs/completed/issue-5328-settings-guard-wiring-check-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5328   Progress comment: 5902373778
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-5174-guard-differential-check
- Project branch: claude/implement-plan-issue-5328-settings-guard-wiring-check   Final PR: #5356 ready (marked ready 2026-09-30)
- Status: COMPLETE
- Stage: final-merge — review round 3 (second cycle)
- Activation: n/a (base claude/implement-plan-issue-5174-guard-differential-check) — the base is not the default branch, so steps 12–13 do not run (Issue Mode)
- Waiting on: PR #5356 (final PR, next review round after the second-cycle round 3 push)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01DZdvBhT9Qf3x3SXQfb4Ugi (reused)   safety net / hand-back: see the final-merge — review round 3 (second cycle) stage report
- Last updated: 2026-10-01
- Last note: final PR #5356 review round 3 of the second cycle (head 221150f): the one finding (a git timeout in the at-ref settings read escaped as a `TimeoutExpired` traceback instead of exit 2) was valid and fixed in both git helpers, `_repo_git` and the scenario `_git`, which now turn a timed-out or unstartable git into a `SetupError`. After #5356 merges: close #5328 (completed, add ai:merged).

## Phases
1. [x] Phase 1 — settings guard-wiring check (`scripts/guard_differential.py`, tests, `ci.yml` step comment, `agents.md`, changelog)   — PR #5369 merged 2026-09-30 (bbcb6b1); review rounds: 3; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Step 3 COMPLETE, Step 4 PASS) — no fix PR (pre-security); session session_01BGKCAL9yE1P5PMAAuwVQMP

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation
- Cycle 1 — run 36678494935 2026-09-30 (target_ref: claude/implement-plan-issue-5328-settings-guard-wiring-check at ac0c3b2; authorized as a stacked target by main's validate.yml): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 269s); no fix PR

## Completion
- Completion PR #5549 merged 2026-09-30 (781dd5a) — doc moved to docs/completed/issue-5328-settings-guard-wiring-check-plan.md
- Final PR #5356 ready (into claude/implement-plan-issue-5174-guard-differential-check) — review rounds: 4 + 3 (rounds 1–4 fixed in 993d284, 6fc4da8, eaf0ba0, 6cf1a8c; the workflow then restarted its count: round 2 fixed in 221150f, round 3 fixed in this push)

## Activation
- n/a: the base branch is the #5174 project branch, so the change goes live with that project's final PR #5185 (Issue Mode)

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which settings entries are "mandatory" guard wiring? — Picked: A — every hook entry, under any event, whose command names `.claude/hooks/<name>_guard.py`, plus the `disableAllHooks` kill switch. Alternatives: B — `PreToolUse` entries only; C — every hook entry, guards or not. Why: covers the finding and the one-key kill switch of the same class without failing every harmless non-guard hook edit (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What verifies that a changed guard command still executes the guard? — Picked: A — only the exact canonical form `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<hook>.py` with that hook file present at the head, a covering matcher, no lower timeout, and all other keys equal; everything else fails closed. Alternatives: B — run the head's wired command through a shell against the corpus and accept matching outcomes; C — fail every change with no verification. Why: §1; B passes a command that runs the guard only under CI, while A is verifiable by construction. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How does an intended wiring change pass? — Picked: A — list its printed identity (`settings:<file>:<event>:<matcher>:<hook>`) under the existing `Intended loosening:` section, where it counts as loosening. Alternatives: B — no escape hatch. Why: reuses the #5174 mechanism, and the operator still sees it. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Added non-guard hooks that could auto-allow and `permissions.*` rule changes? — Picked: A — out of scope, recorded under Notes. Alternatives: B — fail every added hook entry in this PR. Why: §5; the finding names changed guard matchers and commands, and retire-master phase 3 classifies rule diffs. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] An unparseable head settings file? — Picked: A — a wiring regression (exit 1); an unparseable base contributes no wiring. Alternatives: B — a setup error (exit 2). Why: fail closed on the loosening side while keeping exit 2 for bad refs. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-09-30] A settings `env` change reaches every hook process (`CLAUDE_PR_MERGE_GUARD=off`, a `PATH` that shadows `python3`) while the guard command stays canonical. Cover it? — Picked: A — fail closed on any change to the top-level `env` object (identity `settings:<file>:env`). Alternatives: B — a denylist of sensitive keys; C — leave it out of scope. Why: §1; it is the same settings-only guard disablement class, neither settings file has ever had an `env` key, so the rule costs nothing, and a denylist is porous. Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1/1 — review round 1, 2026-09-30] The reviewer panel's consensus finding says the skip line `GUARD_DIFFERENTIAL status=skipped reason=no-hook-change` is narrower than the gate, which also covers the settings files. How to fix it? — Picked: A — keep `reason=no-hook-change` and add `checked=hooks,settings` to the line. Alternatives: B — rename the value to `reason=no-hook-or-settings-change`; C — reject the finding. Why: §6 keeps the existing log value for anyone who greps it, while the new field states the gate's full scope; C would leave a valid finding unfixed with no verdict bot configured. Applied in: PR #5369. Status: pending review
- AD-8 [phase 1/1 — review round 2, 2026-09-30] Round 2's only finding (a `reason=env` line beside the `removed` lines when a settings file is deleted, called redundant) is invalid, and no `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is configured to post the verdict. How does the round close? — Picked: A — reject it in a PR reply, pin the env-only deletion case with a test, and push a new head that carries the due project-branch sync merge, which starts round 3. Alternatives: B — post a hold claim and stop BLOCKED on the issue now; C — drop the env line when the head file is deleted. Why: C is fail-open (a deleted env-only file would pass with no regression); A follows the issue-4886 AD-12 and issue-4707 precedent, the merge is due after the step 2 sync, and a round 3 that repeats the rejected finding stops BLOCKED (B) then. Applied in: PR #5369. Status: pending review

## Lessons
- [source:security] A coverage gate that triggers on changed code files must also trigger on the config that wires that code: a settings-only change can disable a hook while every file-based check stays skipped, and a substring wiring test accepts a command that merely names the hook. (files: scripts/guard_differential.py, .claude/settings.json)
- [source:intervention] A fail-closed line that shares an input with other checks (a settings `env` change on a deleted file) reads as a duplicate to reviewers whenever it co-occurs with them; pin the case where it is the only line with a test so the next round cannot argue it away. (files: scripts/guard_differential.py, tests/test_guard_differential.py)
- [source:intervention] When a review round changes behaviour that the plan describes (a default, a fail-open case turned fail-closed), correct the plan doc in the same commit: the archived plan is reviewed with the final PR, and a stale statement there comes back as a finding. (files: docs/completed/issue-5328-settings-guard-wiring-check-plan.md)
- [source:intervention] Every `subprocess.run(..., timeout=…)` in a script with a documented error exit must catch `TimeoutExpired` (and `OSError` for a missing binary) and map it to that exit; a timeout is not a `CalledProcessError` and escapes a handler that only knows the script's own error type. (files: scripts/guard_differential.py)

## Notes
- 2026-09-30: started by the Claude issue dispatcher (`/implement-issue-claude`), session session_01JGsmyk8gEMSXFh6ocygH4q. The issue base is the #5174 project branch, so the final PR targets it and activation is n/a.
- Protected paths: none. Phase 1 edits no `.claude/**` file.
- 2026-09-30 (conformance 1/3): project branch synced with the issue base in ac0c3b2 (`[claude-merge-resolve]`; the `ci.yml` step comment conflicted with #5325's change to the same comment, both sides kept).
- 2026-09-30 (validation 1/3): `docs/INVENTORY.md` still has no entry for `scripts/guard_differential.py`, so `tests/inventory_parity.py` fails on this branch and its base. It is inherited from #5174 (PR #5187) and recorded as the #5325 project's AD-4; it surfaces on #5174's final PR #5185 into `main`, so this project does not fix it (§5).
- 2026-10-01 (final-merge — review round 4): between 2026-09-30 19:47Z and 22:02Z three review runs on head eaf0ba0 failed the same way (reviewer credits ran out) and the identical-failure cap stopped them; the 00:44Z run reviewed the head and handed round 4 over. Its `review / codex-agent` finding restated that outage and was rejected. The minimax finding on `_delete_object` was overstated (an object that is not loose makes `unlink` raise, so the test cannot pass silently), but the helper now also asserts `git cat-file -e` fails, so a blob still readable from a pack fails the test instead of weakening it.
- 2026-10-01 (final-merge — review round 2, second cycle): the reviewer panel flagged two stale statements in the archived plan (`docs/completed/issue-5328-settings-guard-wiring-check-plan.md` lines 50 and 53). Both were corrected to match the code and `agents.md`, and the plan's Notes record that AD-5's base clause was superseded by 6fc4da8; the AD-5 entry itself is unchanged.
- 2026-10-01 (final-merge — review round 3, second cycle): the consensus finding (one reviewer, low) said `read_settings_text` at a ref let `subprocess.TimeoutExpired` from `_repo_git`'s 120 s timeout escape `main()`, which only catches `SetupError`. Valid; the same gap was in every `_repo_git` caller (from #5174) and in the scenario-setup `_git`, so both helpers now raise `SetupError` on a timeout or an `OSError` (git missing). `tests/test_guard_differential.py::test_a_hung_or_missing_git_is_a_setup_error` fails without the fix.

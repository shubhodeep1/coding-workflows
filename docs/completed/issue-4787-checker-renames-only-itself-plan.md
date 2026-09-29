# §26 checker: rename and archive only the right session

Source issue: shubhodeep1/coding-workflows#4787 (https://github.com/shubhodeep1/coding-workflows/issues/4787)
Base branch: main
Security pass: run

## Summary

Two §26 check-in incidents on 2026-09-28 share one root cause: the instructions tell a session to act on "this session" or "the checker" without saying how to get the right id or check it first. The PR #4706 checker renamed its `notify` subscriber's session as well as itself, and the PR #4704 checker read a delivered hand-back as failed and started a duplicate fixer. This plan pins the session id each rename or archive may target, adds a title check before the fixer renames or archives the checker, makes archive reports truthful, and tightens what §26.C step 5 counts as a gone subscriber.

## Context

- **PR #4706 (issue body).** Checker `session_01NT65F6h1N71P2zXU58vuxn` ran §26.C step 5 and set `PR #4706 merged — handed to session_01K1rv1iZSZVU2vXnSQ2rAju` on itself **and** on the operator's master session `session_01VwSvLnEGmUoaQD42DKapiU`, a `notify` subscriber. Its summary said "checker session archived", but the checker was still `IDLE` an hour later. The fixer never ran the §26.D rename-and-archive, because its hand-back had fired while the PR was on hold.
- **PR #4704 (owner comment, 2026-09-28 10:19Z).** The checker pulled the fixer's hand-back `trig_017frfyiuTtWnso7QowgtjVQ` forward; it fired successfully into the fixer at 09:01:56Z. At 09:13Z the checker still started a fresh fixer, `session_015P3ZqbUvY5nnz5dsED3npL`. By 10:04Z the Routine no longer existed. The duplicate fixer sat on a permission prompt for about 50 minutes while the real fixer pushed `09419f7`.
- **Why the Routine was missing.** A fired one-shot Routine has `ended_reason` set, and the §26.G stale Routine sweep deletes every ended `PR #<n> hand-back` Routine. The fixer runs that sweep itself before it registers its new hand-back (§26.B step 0, via `/fix-claude-pr` step 7), so a successful hand-back routinely disappears before the checker's 10-minute check. §26.C step 5 today treats "trigger is not found" as "subscriber gone".
- **Where the rules live.**
  - `CLAUDE.md` §26.C step 5 (checker) and §26.D (pushing session). `workflow-templates/CLAUDE.md` is a symlink to the root file.
  - `/implement-plan-claude`'s project checker prompt ([Checker prompt](../../.claude/commands/implement-plan-claude.md#checker-prompt), step 4b) and the project-checker archive points (Arming the wait step 1, steps 12 LIVE and 13).
  - `/fix-claude-pr` steps 2, 7, and 8, which send the pushing or fresh fixer to §26.D and rename the session.
  - `/claude-issue-pickup` `— arm-check-in` step 5.3, which archives the checker it just created when the ready trigger fails.
  - Twins: `workflow-templates/.claude/commands/{implement-plan-claude,fix-claude-pr}.md` must stay byte-identical (`tests/test_implement_plan_claude_command.py::test_template_parity`, `tests/test_check_in_status_hand_back.py`). `claude-issue-pickup.md` has no twin.
- **Existing rule kept.** The project checker must not archive itself (`tests/test_implement_plan_claude_command.py::test_checker_does_not_archive_itself`): archiving your own session cuts the turn off mid-tool-call and the session list shows it as FAILED.

## Goals

1. §26.C step 5 tells the checker to take its own id from Bash, `echo "session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}"`, and to pass only that id to `set_session_title`. It never passes a subscriber's id to `set_session_title` or `archive_session`.
2. §26.D tells the fixer to `get_session` the checker id before renaming or archiving it, and to act only when the title is exactly `PR #<n> status check-in` or already the terminal checker title (`PR #<n> <merged | closed> — handed to …`), the session is not archived, and the id is not this session's. Otherwise it skips and says so in one line.
3. Every rule that archives a session says "archived" only after `archive_session` returned success, and reports a failed call as `archive failed: <error>`.
4. §26.C step 5 counts a hand-back as delivered when `last_run` is `SUCCEEDED` with the subscriber's `session_id`, even if the fixer has not claimed yet. Only `FAILED`, `auto_disabled_session_gone`, or a subscriber that `get_session` reports as archived (or not found) counts as gone. A missing Routine alone does not.
5. Before starting a fresh fixer, the checker re-runs `check_in_status.py --hand-back` and starts it only when `action` is still `hand_back_fixer`.
6. The same rules appear in the `/implement-plan-claude` checker prompt and archive points, `/fix-claude-pr`, and `/claude-issue-pickup`, with the twins kept byte-identical.
7. An instruction-text test pins the own-id line, the title check, the truthful-archive rule, and the delivered/gone rule in CLAUDE.md §26 and the command files. A `changelog.d/` fragment records the fix (§20).

## Non-goals

- No change to `.claude/scripts/stale_routines.py`. The sweep may keep deleting fired hand-backs; the checker stops reading a missing Routine as failure (AD-2).
- No self-archive for any checker (AD-1).
- No change to `scripts/claude_issue_route.py`. Its `ready_prompt` sends the requester to §26.B steps 3–4, which will carry the rule (AD-6).
- No change to `.claude/commands/claude-issue-dispatch.md`. Its only archive is of the session `create_session` just returned (AD-7).
- No cleanup of checkers already left `IDLE` by past incidents. They hold no pending trigger and cost nothing.

## Constraints

- §5: text edits only, confined to the rules named above; no reflow of unrelated paragraphs.
- §6: no identifier, section number, title pattern, trigger name, or marker is renamed. The titles `PR #<n> status check-in` and `PR #<n> <merged | closed> — handed to <id>` stay as they are.
- §9: Markdown and Python keep their existing indentation (tabs in Python tests).
- §20: one fragment, `changelog.d/4787-checker-renames-only-itself.md`, section `fixed`.
- §25: nothing here subscribes to PR activity or polls.
- §28.C: the phase edits `.claude/commands/**`, so it is never started unattended without a recorded `Protected-path approval: phase 1` line.

## Approach

Fix the instructions where the model decides which id to act on, and pin them with tests.

- **Own id from the environment.** Each rename or archive says where the id comes from. The checker uses Bash `CLAUDE_CODE_REMOTE_SESSION_ID`, as §26.D already does for the pushing session and the `/implement-plan-claude` checker does for its `Checker session:` line. A subscriber id is only ever passed to `update_trigger` / `get_trigger` (on its Routine) and `get_session` (to see whether it is gone).
- **Verify before touching another session.** The one place a session renames or archives *another* session under §26 is the fixer acting on the checker in §26.D. It reads the target with `get_session` first and checks the title, the archived state, and that the id is not its own. `/implement-plan-claude` gets the same check before it archives the project checker (title `implement-plan <slug> — checker`).
- **Truthful archive.** A report says "archived" only after `archive_session` succeeded.
- **Delivered vs gone.** §26.C step 5 and `/implement-plan-claude` checker step 4b stop treating "trigger not found" as failure on its own; they call `get_session` on the subscriber and treat it as gone only when it is archived or not found. The §26.D rationale ("the checker's 10-minute check reads a missing Routine as a failed hand-back") is updated to match, keeping the archive-before-delete order.
- **Re-check before a fresh fixer.** One extra `check_in_status.py --hand-back` read before `create_session`; a live claim, a hold, a moved head, or a terminal PR means no fresh fixer.

Alternative considered: stop the sweep from deleting fired hand-backs younger than a day. Rejected for this issue (AD-2): it moves a protected script and still leaves the checker trusting a heuristic, while the `get_session` check answers the question directly.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` turns one standalone issue into exactly one phase.

1. **Phase 1 — session-targeting rules for §26 checkers and fixers.**
   - Scope: the CLAUDE.md §26.B/C/D text, the three command files plus twins, `agents.md`, one new test file wired into `ci.yml`, and one changelog fragment.
   - Protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/claude-issue-pickup.md`, `workflow-templates/.claude/commands/implement-plan-claude.md`, `workflow-templates/.claude/commands/fix-claude-pr.md`.
   - Done when: every goal above is present in the text, the new test and the existing twin-parity tests pass, and `tests/test_claude_md_section_numbers.py` still passes.
   - Rollback: revert the phase PR. Text-only, no state or schema.

## Implementation Steps

Phase 1:

1. **`CLAUDE.md` §26.C step 5** (around lines 1881–1914):
   - In "delivered, terminal PR", replace "rename this session" with "rename this session, whose id you take from Bash `echo "session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}"`, never from the instructions or the subscriber list". Add: never pass a subscriber's session id to `set_session_title` or `archive_session`; the checker does not archive itself (the fixer does, §26.D).
   - Add to "delivered": a `SUCCEEDED` `last_run` whose `session_id` is the subscriber's counts as delivered even when the fixer has not claimed the head yet.
   - Replace "the trigger is not found" in the gone list: a missing Routine alone is not a gone subscriber, because the §26.G sweep deletes fired hand-backs. When `get_trigger` returns not found, `get_session` the subscriber: archived or not found → gone; otherwise treat it as delivered.
   - In "Due fix, fixer gone", before `create_session`: re-run step 1's command and start the fresh fixer only when `action` is still `hand_back_fixer`; otherwise resume step 2.
   - In "Terminal, fixer gone", make the rename use the own-id line as well.
2. **`CLAUDE.md` §26.D** (around lines 1932–1945): before renaming or archiving the checker, `get_session` that id and act only when its title is exactly `PR #<n> status check-in` or already `PR #<n> <merged | closed> — handed to …`, it is not archived, and it is not this session's id. Otherwise skip both calls and say so in one line. Say "archived" only after `archive_session` returned success. Update the order rationale: deleting the Routine first could still leave the checker without delivery evidence, so the checker goes first.
3. **`CLAUDE.md` §26.B step 3**: the instructions prompt also names the own-id rule and the delivered/gone rule of §26.C step 5.
4. **`.claude/commands/implement-plan-claude.md`** (and its twin, byte-identical):
   - Checker prompt step 4b: a not-found hand-back trigger is checked with `get_session` on the stage session before falling back to `block`.
   - Checker prompt: add one line saying the checker renames and archives no session.
   - Arming the wait step 1 ("archive it right after the new one is created"), step 12 LIVE, and step 13 ("archive the project checker"): archive only after `get_session` shows the title `implement-plan <slug> — checker` and an id that is not this session's, and report "archived" only on success.
5. **`.claude/commands/fix-claude-pr.md`** (and its twin):
   - Step 2 `hand_back_all`: continue with §26.D, including its title check.
   - Step 8: rename uses the session id from step 0.
   - Rules: add one bullet that `set_session_title` and `archive_session` take only this session's id, or the checker's after the §26.D check, and that "archived" is claimed only on success.
6. **`.claude/commands/claude-issue-pickup.md`** step 5.3: archive only the id `create_session` returned in 5.2, never the `requester`, and say "archived" only on success.
7. **`agents.md`** "Interactive post-push PR status check-in": one sentence each for the own-id rule, the title check, and the delivered/gone rule.
8. **`tests/test_check_in_session_targeting.py`** [new]: pin the new phrases in CLAUDE.md §26.C/§26.D and the three command files. Keep the twins byte-identical, reusing the `_flat` pattern of `tests/test_implement_plan_claude_command.py`. Wire it into the existing `ci.yml` step "Claude-fixer hand-back, claim and catch-all sweep tests (CLAUDE.md §26.C, §26.H)".
9. **`changelog.d/4787-checker-renames-only-itself.md`** [new], `<!-- changelog: fixed -->`, per §20.D.

## Files & Modules

- `CLAUDE.md` (the symlink `workflow-templates/CLAUDE.md` follows)
- `.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `.claude/commands/fix-claude-pr.md`
- `workflow-templates/.claude/commands/fix-claude-pr.md`
- `.claude/commands/claude-issue-pickup.md`
- `agents.md`
- `.github/workflows/ci.yml`
- `tests/test_check_in_session_targeting.py` [new]
- `changelog.d/4787-checker-renames-only-itself.md` [new]

## Tests

- New `tests/test_check_in_session_targeting.py`, unit level:
  - the own-id Bash line appears in §26.C step 5;
  - "never pass a subscriber's session id" appears in §26.C;
  - the §26.D title check names `PR #<n> status check-in` and `get_session`;
  - "archived" only after success appears in §26.D, `/implement-plan-claude`, `/fix-claude-pr`, and `/claude-issue-pickup`;
  - "a missing Routine alone" appears in §26.C step 5 and the checker prompt step 4b;
  - the fresh-fixer re-check appears;
  - the twins are byte-identical.
- Existing tests: `tests/test_implement_plan_claude_command.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_claude_issue_route.py`, `tests/test_claude_md_section_numbers.py`, and `tests/test_stale_routines.py` must still pass.
- End to end: the next §26 check-in reaching a terminal PR renames only its own session. Observed through `get_session` titles, not a test.

## Risks & Mitigations

- A title check that is too strict makes the fixer skip archiving a real checker, which stays `IDLE`. Mitigation: accept both the initial and the terminal checker titles; a skipped archive is reported in one line and costs nothing.
- `get_session` on an unknown id may error rather than return archived. Mitigation: the text treats "not found" the same as archived.
- The extra `check_in_status.py` read before a fresh fixer costs one to seven REST reads. ACCEPTED: it runs only on the fresh-fixer path (§15).
- Protected-path edits in an unattended session. Mitigation: the chain stops before phase 1 until a `Protected-path approval: phase 1` line is recorded (§28.C).

## Rollout

Text and tests only. The rules take effect for each new checker or fixer session once the final PR merges into `main`. Consumer repos receive the `CLAUDE.md` and `.claude/commands` twins through the existing `@stable` sync.

## References

- Issue #4787 and its owner comment of 2026-09-28 (PR #4704 incident)
- PR #4706, PR #4704
- CLAUDE.md §26.B–§26.D, §26.G, §28.C

## Auto-decisions

- AD-1 [plan, 2026-09-28] Should the §26 checker archive itself after the terminal hand-back (the issue says "rename and archive only that id")? — Picked: A — no; the checker renames only its own id and never calls `archive_session`, and the fixer archives it in §26.D after the title check. Alternatives: B — the checker archives its own id as its last call. Why: self-archiving cuts the turn off mid-tool-call and shows the session as FAILED (existing rule pinned by `test_checker_does_not_archive_itself`); A still satisfies "only that id". Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How should §26.C step 5 treat a hand-back Routine that `get_trigger` cannot find? — Picked: A — `get_session` the subscriber; archived or not found → gone, otherwise delivered. Alternatives: B — change `stale_routines.py` to keep fired hand-backs for 24 hours; C — keep "not found → gone". Why: the owner comment asks for A; the sweep deletes fired hand-backs by design, and B edits a protected script while still relying on a heuristic. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Should `/implement-plan-claude` checker step 4b get the same not-found rule? — Picked: A — yes. Alternatives: B — leave step 4b unchanged. Why: the same `get_trigger` logic exists there and a false "gone" starts a duplicate `block` stage session; the issue asks for the same rule in that checker's instructions. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] What does the re-check before a fresh fixer require? — Picked: A — start it only when a fresh `check_in_status.py --hand-back` still returns `action: hand_back_fixer`; otherwise resume step 2. Alternatives: B — skip only on `state: claimed`. Why: A also covers a hold, a moved head, and a PR that merged in the meantime, at the same cost. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Which checker titles pass the §26.D title check? — Picked: A — exactly `PR #<n> status check-in`, or one starting `PR #<n> merged — handed to ` / `PR #<n> closed — handed to `, with the session not archived and not this session. Alternatives: B — only the exact initial title. Why: the issue allows the terminal title, and a checker that already ran step 5 carries it. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-28] Should `scripts/claude_issue_route.py`'s `ready_prompt` repeat the rule? — Picked: A — no; it points the requester to §26.B steps 3–4, which will name the rule. Alternatives: B — append the rule text to `ready_prompt`. Why: §5 minimal change; no helper builds the §26 checker instructions themselves. Applied in: no code change. Status: pending review
- AD-7 [plan, 2026-09-28] Should `.claude/commands/claude-issue-dispatch.md` change too? — Picked: A — no. Alternatives: B — add the same wording to its archive-on-failure line. Why: that line already archives only the session `create_session` just returned, the issue names only the three other commands, and B adds two more protected files. Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-28] Where does the new test run in CI? — Picked: A — a new file in the existing `ci.yml` step "Claude-fixer hand-back, claim and catch-all sweep tests (CLAUDE.md §26.C, §26.H)". Alternatives: B — extend `tests/test_implement_plan_claude_command.py` only; C — a new `ci.yml` step. Why: the rules span CLAUDE.md and three commands, and that step already covers §26.C. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` returned `{"skip": false, "label": null, "reason": "no skip label"}`.
- The issue says edits to `.claude/commands/**` follow the protected-path rule until the twin-sync workflow lands.

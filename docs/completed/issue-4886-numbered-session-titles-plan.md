# Name every automation session with its issue and PR number first

Source issue: shubhodeep1/coding-workflows#4886 (https://github.com/shubhodeep1/coding-workflows/issues/4886)
Base branch: main
Security pass: run

## Summary

Sessions started by the Claude automation get titles that lead with their numbers: `#<issue> · PR #<pr> — <current title>` (operator decision Q59: A). The issue part appears when the work has an issue, and the PR part names the PR the session works on. A stage that opens a PR renames itself in the same step. Every title matcher accepts both the old and the new form (Q60: A), and `PR #<n> status check-in` titles stay exactly as they are.

## Context

- **The cost.** With about 26 Claude projects in flight, the operator maps sessions to work by hand while answering permission prompts. Titles carry the issue number only when it happens to be in the plan slug, never the PR number, and the useful part sits at the end, where the claude.ai sidebar truncates it.
- **Where titles are set today:**
  - `.claude/commands/implement-plan-claude.md`: stage sessions `implement-plan <slug> — <next stage>` (Stage Sessions, and checker prompt step 5), the project checker `implement-plan <slug> — checker` (Check-in Loop, Arming the wait step 1), `/deploy-activate` `implement-plan <slug> — deploy-activate` (step 13), and the hand-back rename `implement-plan <slug> — <stage> — blocked PR` (Hand-back step 2).
  - `.claude/commands/claude-issue-pickup.md` step 3 and `.claude/commands/claude-issue-dispatch.md` step 2: `issue <repo>#<N> — implement` and `PR <repo>#<N> — fix <kind>`.
  - `.claude/commands/fix-claude-pr.md` step 8: the `PR <owner>/<repo>#<N> — <fixed <kind> | on hold: <kind>>` rename.
  - CLAUDE.md §26: the checker `PR #<n> status check-in` (§26.B step 2), the fresh fixer `PR #<n> — fix <kind>` (§26.C step 5), the checker's terminal `PR #<n> <merged | closed> — handed to …` renames (§26.C step 5, §26.D), and the report titles `PR #<n> merged — …` / `PR #<n> closed — decision needed` (§26.D).
- **Where titles are matched today:**
  - Arming the wait step 1 reuses a checker only when "its title is `implement-plan <slug> — checker`" (exact).
  - Zombie-checker cleanup selects sessions whose title "starts with `implement-plan <slug> — checker` or `implement-plan <slug> — waiting:`". The operator has already prefixed the open checkers by hand, so both rules miss them today.
  - CLAUDE.md §26.B step 1b matches `PR #<n> status check-in` exactly. That title is unchanged.
  - `checker_title` in `scripts/claude_issue_route.py` (`parse_arm_check_in_request`) is the §26 checker title `PR #<n> status check-in`, and the pickup passes it to `create_session`. No code looks a session up by it; §26.B step 1b does, and the title is unchanged.
  - `.claude/scripts/check_in_status.py` and `.claude/scripts/stale_routines.py` match **Routine** names, not session titles (`grep -n title` finds nothing in either). Neither needs a change, and Routine names are unchanged.
- **Twin-first (#4785, still open).** `.claude/**` is edited only through its `workflow-templates/.claude/**` twins; the supervising session copies them (operator Q40: A, restated in this issue). `implement-plan-claude.md`, `implement-issue-claude.md`, `claude-issue-dispatch.md`, and `fix-claude-pr.md` have byte-identical twins, and parity is pinned by `tests/test_implement_plan_claude_command.py::test_template_parity` and `tests/test_implement_issue_claude_command.py::test_template_parity`. `claude-issue-pickup.md` has no twin (upstream-only), so its edit is listed for the supervising session, as #4787 did.
- **Related work in flight.** #4887 (session janitor) and #4817 (archive replaced blocked sessions) touch the same command files. #4787 (merged into its project branch) made the checker rename only itself.
- **Binding rules.** §6 (identifiers: the new form is added alongside, old titles keep matching), §5, §9, §19, §20, §25/§26 (no PR watching; the §26 checker title stays exact), §28.C (protected paths).

## Goals

1. New stage, checker, `/deploy-activate`, fixer, and issue sessions start with `#<issue> · PR #<pr> — `, or with whichever parts exist.
2. A stage renames itself (`set_session_title`) in the same step that opens its PR.
3. The checker reuse check and the zombie-checker cleanup find checkers under both the old and the new titles, and hand-prefixed ones too.
4. `PR #<n> status check-in` titles are unchanged. Fixer and §26.D report titles gain `#<issue> · ` only when an issue is known.
5. Tests pin the new title rules and cover both title forms in the matchers. They are wired into `ci.yml`. A `changelog.d/` fragment is added.

## Non-goals

- Renaming sessions that are already running. The operator renames live ones by hand (Q60: A).
- Routine names, the `PR #<n> status check-in` title, and the checker's `… — handed to …` terminal renames.
- Payload changes (`claude_pr_fix.v1`) to carry an issue number to the pickup.
- Session archiving (#4887, #4817).
- Editing `.claude/**` directly. Twins only, until #4785 lands.

## Constraints

- §5: only the files listed below; no reflow of unrelated text.
- §6: no step, section, marker, trigger name, or identifier is renamed. Old titles keep matching; `checker_title` keeps its value.
- §15: no new GitHub API call. Renames are claude-code-remote calls the sessions already make (`set_session_title` is allowlisted).
- §19: phase and completion PRs use `Refs #4886`. The final PR into `main` uses `Fixes #4886` (#4886 is not an `ai:orchestrator-tracking` issue).
- §20: one fragment, `changelog.d/4886-numbered-session-titles.md`, section `changed`.
- §28.C: the phase edits `.claude/**` only through its twins, plus one listed edit to the twinless pickup.

## Approach

- **One definition.** A new `### Session titles` subsection under Stage Sessions in `/implement-plan-claude` defines `<numbers>`, the title prefix:
  - `#<issue> · ` when the project has a `Source issue:`;
  - then `PR #<pr> — `, the PR the session works on: its own phase, review-round, conformance-fix, validation-fix, completion, or activation-fix PR, else the project's final PR (AD-1). Omit it when neither exists (legacy projects).
  - A rename first strips an existing `#<n> · ` and `PR #<m> — ` prefix, so parts never stack (AD-3).
  - Every other part of the rules refers to it.
- **Creation-time titles.**
  - Stage sessions: `<numbers>implement-plan <slug> — <next stage>`.
  - The checker: `<numbers>implement-plan <slug> — checker`, with the final PR.
  - `/deploy-activate`: `<numbers>implement-plan <slug> — deploy-activate`, with the final PR, and its PushNotification names that title.
  - The hand-back rename: `<numbers>implement-plan <slug> — <stage> — blocked PR`, with the handed-back PR.
  - The checker prompt carries three complete titles filled by the arming stage (`<title on success>`, `<title on review round>`, `<title on block>`), so the low-effort checker copies a title instead of computing one (AD-2).
- **Rename on PR open.** Steps 3a (final PR) and 6 (phase PR) say it explicitly. The Session titles rule covers the fix and completion PRs (steps 8, 10, 11, 12).
- **Matchers accept both forms (AD-7).**
  - The checker reuse check and the zombie cleanup match a title that **contains** `implement-plan <slug> — checker` (or `— waiting:`).
  - "Contains" matches old titles, new ones, and hand-prefixed ones. Because `implement-plan ` precedes the slug and ` — ` follows it, one slug never matches another.
- **Issue and fixer titles.**
  - Pickup and dispatch create issue sessions as `#<N> · issue <repo>#<N> — implement`.
  - Fixer titles stay `PR <repo>#<N> — fix <kind>`: the pickup never reads PRs (AD-5).
  - `/fix-claude-pr` learns the issue from a `claude/implement-plan-issue-<I>-…` head ref (AD-4). A fresh fixer then renames itself to `#<I> · <title>` once its claim is posted, and the step 8 rename carries the same prefix.
- **CLAUDE.md §26.**
  - §26.B step 3's checker instructions name the PR's source issue when there is one.
  - §26.C step 5's fresh fixer title and the §26.D report titles gain `#<issue> · ` when an issue is known.
  - §26.B step 1b says the `PR #<n> status check-in` title never takes a prefix, because the match is exact (AD-6).

Alternatives considered:

- Omitting the PR part until a session opens its own PR (AD-1 B). Rejected: the checker and the security, validation, and final-merge stages would then show no PR at all, although the issue asks the checker to show the integration PR.
- A matcher script with its own allow rule (rejected: it needs a `settings.json` change, which needs the operator's Q62/Q64 window).

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` turns one standalone issue into exactly one phase.

1. **Phase 1: numbered session titles and dual-form matchers.**
   - **Scope:**
     - the CLAUDE.md §26 text;
     - the twins of `implement-plan-claude.md`, `implement-issue-claude.md`, `claude-issue-dispatch.md`, and `fix-claude-pr.md`;
     - one listed edit to `.claude/commands/claude-issue-pickup.md`;
     - `agents.md`;
     - the two pinned title strings in `tests/test_implement_plan_claude_command.py`;
     - a new `tests/test_session_titles.py`, wired into `ci.yml`;
     - a changelog fragment.
   - **Protected paths:** `.claude/commands/implement-plan-claude.md`, `.claude/commands/implement-issue-claude.md`, `.claude/commands/claude-issue-dispatch.md`, `.claude/commands/fix-claude-pr.md` (edited through their `workflow-templates/.claude/commands/` twins), and `.claude/commands/claude-issue-pickup.md` (no twin; the edit is listed for the supervising session).
   - **Done when:**
     - every goal is present;
     - the new tests pass against the twins;
     - with the twins copied and the pickup edit applied, `tests/test_session_titles.py`, `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_claude_issue_route.py`, and `tests/test_claude_md_section_numbers.py` pass.
   - **Rollback:** revert the phase PR. There is no state, schema, label, or Routine change, and old titles keep matching.

## Implementation Steps

Phase 1:

1. `workflow-templates/.claude/commands/implement-plan-claude.md`:
   1. Add `### Session titles` under Stage Sessions, with the `<numbers>` definition, the rename-on-PR-open rule, the no-stacking rule, and the dual-form match rule.
   2. Stage Sessions "Started by": `title` = `<numbers>implement-plan <slug> — <next stage>`.
   3. Step 3a: rename after opening the final PR. Step 6: rename after opening the phase PR.
   4. Step 13: the `/deploy-activate` title and PushNotification.
   5. Arming the wait step 1: the reuse check matches "contains", and the new checker's title is `<numbers>implement-plan <slug> — checker`. Step 3: the three titles are filled into the checker prompt.
   6. Zombie-checker cleanup: select by "contains".
   7. Checker prompt step 5: take each next stage's title from the prompt.
   8. Hand-back step 2: the rename.
   9. Rules: one bullet.
2. `workflow-templates/.claude/commands/claude-issue-dispatch.md` step 2: the issue title `#<N> · issue <repo>#<N> — implement`, and a note that the fixer adds its issue prefix itself.
3. `.claude/commands/claude-issue-pickup.md` step 3: the same issue title (the listed edit).
4. `workflow-templates/.claude/commands/fix-claude-pr.md`:
   - step 0: derive `<issue>` from the head ref;
   - step 4: a fresh fixer renames itself once the claim is posted;
   - step 8: the rename gains `#<I> · `.
5. `workflow-templates/.claude/commands/implement-issue-claude.md` step 8: the session is named `#<N> · issue <repo>#<N> — implement`, and the chain adds the PR part.
6. CLAUDE.md:
   - §26.B step 1b: the no-prefix note;
   - §26.B step 3: the issue in the checker instructions;
   - §26.C step 5: the fresh fixer title;
   - §26.D: the report titles.
7. `agents.md`: the stage-title sentence under the check-in loop.
8. Tests:
   - `tests/test_session_titles.py` [new] loads the twins, CLAUDE.md, and the pickup. It pins the rules, and checks that the documented "contains" matcher finds the checker under the old, new, and hand-prefixed titles but not under another slug.
   - `tests/test_implement_plan_claude_command.py`: update the two pinned strings.
   - Wire the new file into the `/implement-plan-claude command contract tests` step of `.github/workflows/ci.yml`.
9. `changelog.d/4886-numbered-session-titles.md` [new].

## Files & Modules

- `workflow-templates/.claude/commands/implement-plan-claude.md` (twin of `.claude/commands/implement-plan-claude.md`)
- `workflow-templates/.claude/commands/implement-issue-claude.md` (twin)
- `workflow-templates/.claude/commands/claude-issue-dispatch.md` (twin)
- `workflow-templates/.claude/commands/fix-claude-pr.md` (twin)
- `.claude/commands/claude-issue-pickup.md` (listed edit, no twin)
- `CLAUDE.md` (also served as `workflow-templates/CLAUDE.md`, a symlink)
- `agents.md`
- `tests/test_session_titles.py` [new], `tests/test_implement_plan_claude_command.py`
- `.github/workflows/ci.yml`
- `changelog.d/4886-numbered-session-titles.md` [new]

## Data Model / Index Changes

None.

## Tests

- `tests/test_session_titles.py` [new]:
  - Stage, checker, and `/deploy-activate` titles carry `<numbers>`, and the Session titles definition is present.
  - Steps 3a and 6 rename on PR open.
  - The checker prompt carries three titles and step 5 uses them.
  - The reuse check and the zombie cleanup use "contains". An executable check applies that rule to the old, new, and hand-prefixed titles and to a near-miss slug.
  - Pickup, dispatch, and implement-issue use the issue title. Fix-claude-pr derives the issue from the head ref and renames.
  - CLAUDE.md keeps `PR #<n> status check-in` exact and prefixes the fixer and report titles.
- `tests/test_implement_plan_claude_command.py`: update the pinned checker title and zombie-cleanup strings.
- Before the twin sync, the `.claude`-reading tests fail as expected (twin parity plus the two updated strings). After it, run the suites listed under Done when.

## Risks & Mitigations

- **A matcher misses a checker, so two checkers race.** "Contains" is at least as broad as today's exact and prefix rules, and the test covers every title form.
- **A matcher is too broad.** The ` — ` after the slug and `implement-plan ` before it stop one slug from matching another; the test includes a near-miss slug.
- **The low-effort checker builds a wrong title.** It copies one of three titles the stage filled; it never computes one (AD-2).
- **Merge conflicts with #4887 and #4817**, which touch the same files. Their edits are in different paragraphs, and the project branch sync resolves the rest.
- **Rename call failures.** A failed `set_session_title` is cosmetic: report it in one line and never block on it.

## Rollout

Ships with the next `@stable` sync to consumer repos (`.github/ai/consumer_repos.json`, §14) through the `workflow-templates/.claude/**` twins and CLAUDE.md. It applies to sessions created after the merge. Revert the PR to roll back.

## References

- Issue #4886 (Q59: A, Q60: A), `docs/operations/master-session.md` (Q40: A, Q60: A)
- #4785 (twin sync), #4787 (checker renames only itself), #4887, #4817

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which PR does a session that has no PR of its own show? — Picked: A — the project's final (integration) PR in project mode; omit the part in legacy mode. Alternatives: B — omit it until the session opens its own PR. Why: every project session then shows a PR from the start, and the issue already puts the integration PR on the checker. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How does the checker title the stage it starts? — Picked: A — the arming stage fills three complete titles (success, review round, block) into the checker prompt, and the checker copies one. Alternatives: B — the checker composes the title from rules. Why: the low-effort checker never interprets (PR #4596). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] What does a rename do with an existing prefix? — Picked: A — strip a leading `#<n> · ` and `PR #<m> — ` before adding the new parts. Alternatives: B — prepend the new parts. Why: B stacks prefixes on every rename. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How does `/fix-claude-pr` know the issue? — Picked: A — from a `claude/implement-plan-issue-<I>-…` head ref only. Alternatives: B — also parse `Refs`/`Fixes #` in the PR body. Why: the ref is deterministic, while PR text is untrusted and can name several issues. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Does the pickup's fixer title gain the issue? — Picked: A — no; `PR <repo>#<N> — fix <kind>` stays, and the fixer adds `#<I> · ` after its claim. Alternatives: B — add the issue to the `claude_pr_fix.v1` payload. Why: the pickup never reads PRs, and a payload change widens the scope. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Do the §26 checker's titles gain the issue? — Picked: A — no; `PR #<n> status check-in` and its `… — handed to …` renames stay. Only fixer and §26.D report titles gain `#<issue> · `. Alternatives: B — prefix the checker's terminal renames too. Why: §26.B step 1b matches the checker exactly (Q60), and the issue scopes the prefix to fixer and report titles. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] How do matchers accept both forms? — Picked: A — the title contains `implement-plan <slug> — checker` (or `— waiting:`), for the zombie cleanup and the reuse check alike. Alternatives: B — a strict optional-prefix pattern. Why: this is the issue's rule, and it also matches hand-prefixed titles. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] Where do the new tests live? — Picked: A — a new `tests/test_session_titles.py` that loads the twins, CLAUDE.md, and the pickup, wired into the existing `/implement-plan-claude` contract CI step. Alternatives: B — extend the existing `.claude`-reading suites. Why: the twin leads under twin-first, and one file keeps every title rule together. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-29] How does the twinless `claude-issue-pickup.md` change? — Picked: A — the exact edit is listed in the blocker comment for the supervising session. Alternatives: B — leave the pickup unchanged. Why: #4787 precedent, and the issue lists the pickup. Applied in: phase 1 PR (listed edit). Status: pending review
- AD-10 [plan, 2026-09-29] Does `checker_title` in `scripts/claude_issue_route.py` change? — Picked: A — no. It is the §26 checker title, looked up exactly only by §26.B step 1b, and that title is unchanged. Alternatives: B — accept both forms in a new lookup. Why: nothing looks it up by a changed title. Applied in: no code change. Status: pending review

## Notes

- Security pass: run (`security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`).
- Protected-path approval: phase 1 — twin-first per Q40 (operator decision restated in the #4886 body: "follow the interim twin-first rule (operator Q40: A): edit the `workflow-templates/.claude/**` twins, post a hold, and stop BLOCKED") (2026-09-29).

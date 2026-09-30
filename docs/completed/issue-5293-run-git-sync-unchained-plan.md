# Run the chain's git fetch, merge, and push commands unchained

Source issue: shubhodeep1/coding-workflows#5293 (https://github.com/shubhodeep1/coding-workflows/issues/5293)
Base branch: main
Security pass: run

## Summary

An unattended `/implement-plan-claude` review-round session chained an
allowlisted `git fetch` and `git merge --no-edit` with `2>&1 | tail`
redirects and `git status` / `git log` reads. No allow rule matched the
chain, so the whole command went to the Auto-mode classifier, which denied it
as `[Modify Shared Resources]`. Tell stage and fixer sessions to run the git
commands the chain names exactly as written, with no redirect, pipe, or
chained reads.

## Context

- Issue #5293 (filed by `.claude/scripts/permission_prompts.py`, CLAUDE.md
  §23.I) records one **Auto-mode denial** at 2026-09-29T22:53:14Z in session
  `session_011pHT3xASqy1Kc6CEaaRmwa`, titled
  `implement-plan issue-5124-mask-credentials-in-prompt-reports — phase 1/1 — review round`.
  The command was
  `git fetch origin claude/implement-plan-issue-4755-… 2>&1 | tail -1 && git merge --no-edit origin/claude/implement-plan-issue-4755-… 2>&1 | tail -3; git status -sb | head -3; git log --oneline -3`.
- The reason is `[Modify Shared Resources]`, a real classifier verdict, not
  `Classifier unavailable`. So this is not part of the #4750 outage cluster
  that #4761, #4779, #4780, #4808, #5189–#5191, and #5250 were closed into.
  Those issues show that the same chained shape (`git fetch * 2>&1 | tail`
  plus `git status | head`) reaches the classifier at all: an allow-rule match
  would never have consulted it.
- Each git part is already allowlisted on its own (`.claude/settings.json:12`
  `Bash(git fetch *)`, `:13` `Bash(git checkout *)`, `:19` `Bash(git merge *)`,
  `:22` `Bash(git status *)`, `:23` `Bash(git log *)`, `:28-29` the
  `git push … claude/*` rules). Run as written, none of them reaches the
  classifier. The appended `2>&1` redirects, pipes, and status reads are what
  no rule approves (the same finding as #4798).
- The command files that name these git steps are:
  - `.claude/commands/implement-plan-claude.md`: step 2 (sync the project
    branch), step 4 (fresh phase branch), step 7 **Blocked** (merge the base
    branch), step 7a `kind=conflict` (`git merge origin/<base branch>`), and
    step 6 / step 2 pushes. Its Helpers intro (`:161`) already tells sessions
    to run helper **scripts** alone (#4798) but says nothing about git.
  - `.claude/commands/fix-claude-pr.md` step 5 (`git fetch origin <head ref>
    <base ref>`, `git checkout -B …`, and `git merge --no-edit origin/<base
    ref>` for `conflict`). The same review-round and conflict work runs here
    for PRs outside a project.
- Both command files have byte-identical twins under
  `workflow-templates/.claude/commands/`, enforced by
  `tests/test_implement_issue_claude_command.py::test_template_parity` and
  `tests/test_check_in_status_hand_back.py` (the `fix-claude-pr.md` parity
  check).

## Goals

- `implement-plan-claude.md` Helpers intro says to run the git commands the
  command names (`git fetch`, `git checkout -B`, `git merge --no-edit`,
  `git push`) exactly as written, with no `2>&1`, no pipe into `tail` /
  `head`, and no `;` / `&&` chain of `git status` / `git log` reads, and to
  check status in a separate call.
- `fix-claude-pr.md` step 5 says the same for its fetch, checkout, and merge.
- The `workflow-templates/.claude/commands/` twins carry the change. The live
  copies follow in the twin-sync (see Phases).
- A test pins both sentences in the twin copies.

## Non-goals

- No new `permissions.allow` rule and no change to `.claude/settings.json`,
  any hook, or any script.
- No change to `fix-claude-pr.md` step 6's `git push origin HEAD:<head ref>`,
  which no allow rule matches either but which #5293 does not report (AD-3).
- No change to CLAUDE.md, README.md, or agents.md (AD-4).
- No change to the #4750 classifier-outage handling.

## Constraints

- §5 minimal change set: two sentences plus their twins, one test, one
  changelog fragment.
- §6: no identifier is renamed or removed.
- §9: Markdown and Python keep their existing style (tests use tabs).
- §14: the change reaches consumer repos through the next `@stable` sync of
  `workflow-templates/.claude/commands/`; no consumer registry change.
- §20: a `changelog.d/` fragment, since unattended sessions stop fewer times.
- §28.C: the phase edits `.claude/**`, so it runs under a recorded
  `Protected-path approval: phase 1` line (the interim twin-first default,
  until #4785).

## Approach

Prose guidance in the two command files that name the git steps, next to the
standalone-call rule #4798 added for helper scripts. The alternatives are
rejected. Allowlisting `2>&1`, `tail`, `head`, and chained reads widens
permissions for shapes no command prescribes. Closing as not planned would be
wrong because the call is not an ask-first operation (issue fix order,
AD-1).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — unchained git guidance.** `protected paths:
   .claude/commands/implement-plan-claude.md,
   .claude/commands/fix-claude-pr.md`.
   - Files: the `workflow-templates/.claude/commands/` twins of the two command
     files (the live `.claude/` copies arrive in the `[claude-twin-sync]`
     commit), `tests/test_implement_issue_claude_command.py`, and a new
     `changelog.d/5293-unchained-git-sync-calls.md`.
   - Done: both sentences are in the twin copies, the new test passes, the
     full `tests/test_implement_issue_claude_command.py` and
     `tests/test_check_in_status_hand_back.py` suites pass after the twin-sync
     (the parity tests are red until then, by design).
   - Rollback: revert the PR. Nothing else depends on the wording.

## Implementation Steps

1. `workflow-templates/.claude/commands/implement-plan-claude.md`, Helpers
   intro (the paragraph ending "Run it alone all the same."): append
   "Run the git commands this command names (`git fetch`, `git checkout -B`,
   `git merge --no-edit`, `git push`) exactly as written too, with no `2>&1`,
   no pipe into `tail` or `head`, and no `;` or `&&` chain of `git status` or
   `git log` reads: read their output from the tool result and check the
   branch state in a separate call. Alone, each matches its
   `permissions.allow` rule; chained, the whole command goes to the Auto-mode
   classifier, which denied a fetch-and-merge of the project branch as
   `[Modify Shared Resources]` (issue #5293)."
2. `workflow-templates/.claude/commands/fix-claude-pr.md`, step 5, after
   "confirm `HEAD` is `<head_sha>` (otherwise step 1 again).": insert
   "Run these git commands, and the `git merge` below, exactly as written, with
   no `2>&1`, no pipe into `tail` or `head`, and no chained `git status` or
   `git log` reads; check the branch state in a separate call. Chained, the
   command matches no allow rule and goes to the Auto-mode classifier, which
   has denied it (issue #5293)."
3. Add `test_git_commands_run_unchained` to
   `tests/test_implement_issue_claude_command.py`, asserting both sentences in
   the `workflow-templates/.claude/commands/` copies (the parity tests cover
   the live copies once synced).
4. Add `changelog.d/5293-unchained-git-sync-calls.md` (`fixed`).

## Files & Modules

- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/fix-claude-pr.md`
- `.claude/commands/implement-plan-claude.md` (twin-sync)
- `.claude/commands/fix-claude-pr.md` (twin-sync)
- `tests/test_implement_issue_claude_command.py`
- `changelog.d/5293-unchained-git-sync-calls.md` [new]

## Data Model / Index Changes

None.

## Tests

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_implement_issue_claude_command.py`
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_check_in_status_hand_back.py tests/test_check_in_session_targeting.py tests/test_implement_plan_claude_command.py`
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_permission_prompts.py`
  (unchanged, confirms nothing in the filer depends on the wording).

## Risks & Mitigations

- A model can still chain commands despite the guidance. The filer then
  reports the shape again, which is the intended feedback loop.
- The classifier might deny a standalone `git merge` in some other context.
  It does not see one: `Bash(git merge *)` approves it before the classifier
  runs.
- The phase PR stays on hold until the twin-sync copies the twins into
  `.claude/` (§28.C interim default).

## Rollout

Ships with the next `@stable` sync of the `.claude/commands/` twins. No flag,
no migration.

## References

- #4798 (standalone helper calls, the precedent this extends)
- #4750 (classifier-outage cluster, which this issue is not part of)
- #4785 (the twin-sync automation that ends the interim twin-first default)

## Auto-decisions

- AD-1 [plan, 2026-09-29] How should the denial be removed? — Picked: A — tell sessions to run the chain's git commands as written, unchained (command-file guidance). Alternatives: B — add allow rules for `2>&1`, `tail`, `head`, and chained reads; C — close as not planned. Why: each git part is already allowlisted, the denial came from the chained extras, and the issue's fix order puts command-file changes first and never widens permissions for unprescribed shapes. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Which command files get the guidance? — Picked: A — `implement-plan-claude.md` (Helpers intro) and `fix-claude-pr.md` (step 5), the two that name the fetch, checkout, and merge a review round or conflict fix runs. Alternatives: B — only `implement-plan-claude.md`, where the denied session ran; C — also CLAUDE.md and every other command that mentions `git fetch`. Why: a `/fix-claude-pr` session does the same conflict merge and would hit the same denial; the other commands are interactive or do not merge. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should `fix-claude-pr.md` step 6's `git push origin HEAD:<head ref>`, which no allow rule matches, be changed as well? — Picked: A — no, leave it and record it in the plan's Notes. Alternatives: B — rewrite it to `git push origin <head ref>`; C — add a `Bash(git push origin HEAD:claude/*)` rule. Why: #5293 does not report it, the classifier has not denied it, and §5 keeps the change to what the issue shows. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-29] Is README.md, agents.md, or CLAUDE.md updated (§7)? — Picked: A — no; no env var, DB behaviour, or operational step changes. Alternatives: B — add a line to CLAUDE.md §23.I. Why: §5 minimal change set; the guidance lives in the command files the sessions read, as it did for #4798. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5293`
  returned `{"skip": false, "label": null, "reason": "no skip label"}`.
- `fix-claude-pr.md` step 6 pushes with `git push origin HEAD:<head ref>`,
  which neither `Bash(git push -u origin claude/*)` nor
  `Bash(git push origin claude/*)` matches, so it is left to the Auto-mode
  classifier. Not changed here (AD-3). If the filer reports it, a follow-up
  issue fixes it.

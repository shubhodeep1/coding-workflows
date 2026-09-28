# Allowlist the read-only orientation reads so a classifier outage cannot deny them

Source issue: shubhodeep1/coding-workflows#4749 (https://github.com/shubhodeep1/coding-workflows/issues/4749)
Base branch: main
Security pass: run

## Summary

An unattended `/implement-plan-claude` stage session had a read-only
orientation command denied in Auto mode because the classifier was
unavailable. Add exact `permissions.allow` rules for the three subcommands no
rule covers yet (`git branch --show-current`, `ls`, `head`). Allow rules
resolve before the classifier, so a classifier outage can no longer deny the
command.

## Context

- Issue #4749 was filed by `.claude/scripts/permission_prompts.py` (CLAUDE.md
  §23.I). It records one `PermissionDenied` for `Bash` at
  2026-09-28T07:36:09Z in session `session_011pBDarB1ebFsodR5wUM4YS`, with
  the reason `Classifier unavailable`. The example command is
  `git status && git branch --show-current && git log --oneline -3 && ls .claude/commands/ | head -50`.
- That session was `implement-plan issue-4687-bind-rejections-to-consensus-ids — completion`
  (Auto mode, cloud). No command file tells a session to run this command.
  It was the model's own orientation read at the start of a stage, and every
  stage session does something like it.
- Claude Code decides in this order
  (https://code.claude.com/docs/en/permission-modes.md, "How the classifier
  evaluates actions"):
  1. An action that matches an allow, ask, or deny rule resolves immediately.
  2. Read-only actions are auto-approved. The exception is a session with
     server-side classifier review, where "read-only and sandboxed shell
     commands wait for that review and are blocked if it flags them".
  3. Everything else goes to the classifier.

  So in these cloud sessions a read-only command with no matching allow
  rule still waits on the classifier, and when the classifier is
  unavailable the command is denied.
- For compound commands, "a rule must match each subcommand independently"
  across `&&`, `||`, `;`, `|`, `|&`, `&`, and newlines. A trailing ` *`
  after a space "also matches the bare command": `Bash(git log *)` matches
  `git log` (https://code.claude.com/docs/en/permissions.md, "Compound
  commands" and "Wildcard patterns").
- `.claude/settings.json` already allows `Bash(git status *)`, which also
  matches bare `git status`, and `Bash(git log *)`. The subcommands no rule
  covers are `git branch --show-current`, `ls .claude/commands/`, and
  `head -50`.
- `workflow-templates/.claude/settings.json` is byte-identical to the root
  file, and consumer repos receive it through the `.claude/` sync. Several
  tests read both copies (`tests/test_permission_prompts.py`,
  `tests/test_edit_comment.py`, `tests/test_dispatch_workflow.py`, and
  others).
- Claude Code protects the root `.claude/` directory. `workflow-templates/.claude/`
  is not protected (see the #4678 project's AD-1), so this phase is marked
  `protected paths: .claude/settings.json` (CLAUDE.md §28.C). The issue body
  expects that stop.

## Goals

- In both settings files, `permissions.allow` contains `Bash(git branch --show-current)`,
  `Bash(ls *)`, and `Bash(head *)`.
- Every subcommand of the #4749 example command matches an allow rule in
  both files, following the documented matching rules. A test in
  `tests/test_permission_prompts.py` (already in its own `ci.yml` step)
  asserts this.
- The two settings files stay byte-identical.
- `agents.md` documents the new rules, and a `changelog.d/` fragment records
  the change for consumer repos.

## Non-goals

- The sibling permission-prompt issues (#4751, #4759, #4760, #4761, #4767).
  Each one runs its own issue-mode project (AD-4).
- Any change to the §23.H `gh api` guard, to other hooks, or to CLAUDE.md.
- Any `ask` or `deny` rule, and `permissions.blockReadsOutsideWorkingDirectories`.
- Making the classifier itself more available.

## Constraints

- §1: nothing but read-only commands is added. `ls` and `head` cannot write,
  and `git branch --show-current` is an exact rule, so no branch-creating
  form of `git branch` is approved.
- §5: add only the three rules this pattern needs.
- §6: no existing rule is renamed or removed.
- §9: `settings.json` keeps its current indentation (tabs).
- §14: no new consumer repo. The rules reach consumers through the existing
  `workflow-templates/.claude/settings.json` sync.
- §20: a fragment in `changelog.d/`. Never edit `CHANGELOG.md` directly.
- §23.I: the fix only adds allow rules for reads, never for a destructive or
  administrative action.
- §28.C: the phase edits the root `.claude/settings.json` (a protected path),
  so it starts only after a `Protected-path approval: phase 1` line is
  recorded.

## Approach

Add the three rules to the Bash block of `permissions.allow` in both settings
files, next to the existing `git` read rules. Rules resolve in step 1 of the
decision order, before the read-only step and before the classifier. The
command then runs whether or not the classifier is available.

Alternatives considered (AD-2, AD-3):

- A CLAUDE.md rule that tells sessions to use Glob and Read instead of
  `ls | head`. This depends on the model following it, and it would not help
  `git branch --show-current`.
- Extending a `PreToolUse` hook to approve read-only compounds. This is a
  larger change, it also edits `.claude/**`, and it would duplicate what an
  allow rule already does.
- Allowing only exact pipe forms such as `Bash(head -50)`. That only fixes
  this exact line count.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan. The change is
three allow rules plus their test and docs, and it cannot usefully be split.

1. **Phase 1: allowlist the orientation reads.**
   - Files: `.claude/settings.json`, `workflow-templates/.claude/settings.json`,
     `tests/test_permission_prompts.py`, `agents.md`,
     `changelog.d/4749-allowlist-read-only-orientation.md` [new].
   - Protected paths: `.claude/settings.json`.
   - Done when both settings files carry the three rules and are
     byte-identical, the new test passes in both parametrisations, and the
     rest of `tests/test_permission_prompts.py`, `tests/test_edit_comment.py`,
     `tests/test_dispatch_workflow.py`, `tests/test_check_in_status.py`, and
     `tests/test_gh_api_write_guard.py` still pass.
   - Rollback: revert the PR. Sessions go back to the classifier for these
     reads.

## Implementation Steps

1. `.claude/settings.json`: after `"Bash(git rev-parse *)"`, add
   `"Bash(git branch --show-current)"`, `"Bash(ls *)"`, and `"Bash(head *)"`.
2. `workflow-templates/.claude/settings.json`: make the same edit, then check
   the two files are identical with `cmp`.
3. `tests/test_permission_prompts.py`: add a small matcher that implements
   the documented semantics. It splits a command on the shell separators.
   A subcommand is allowed when a `Bash(...)` rule equals it exactly, or
   when a rule ending in ` *` whose only `*` is the trailing one has a
   prefix that equals the subcommand or starts it followed by a space. Add a
   test, parametrised over `SETTINGS_PATHS`, that every subcommand of the
   #4749 example command is allowed. Add a negative check that
   `git branch new-name` is not allowed.
4. `agents.md`, section "Unattended helpers and permission prompt reports
   (CLAUDE.md §23.I)": add one bullet. It names the three rules, explains why
   an allow rule is needed in cloud sessions (the classifier reviews read-only
   commands there), and points to the test.
5. `changelog.d/4749-allowlist-read-only-orientation.md` [new]: a `changed`
   fragment following CLAUDE.md §20.D.

## Files & Modules

- `.claude/settings.json`
- `workflow-templates/.claude/settings.json`
- `tests/test_permission_prompts.py`
- `agents.md`
- `changelog.d/4749-allowlist-read-only-orientation.md` [new]

## Tests

- Unit: the new parametrised test in `tests/test_permission_prompts.py`,
  plus a negative case.
- Regression: `tests/test_permission_prompts.py`, `tests/test_edit_comment.py`,
  `tests/test_dispatch_workflow.py`, `tests/test_check_in_status.py`,
  `tests/test_gh_api_write_guard.py`, `tests/test_pr_merge_status_guard.py`,
  `tests/test_pr_watch_guard.py`, and `tests/test_pr_check_in_reminder.py`.
  These read the settings files.
- End to end: a later stage session's orientation read runs with no
  `PermissionDenied` entry for this shape in its permission prompt report.

## Risks & Mitigations

- `Bash(head *)` and `Bash(ls *)` also approve reads outside the working
  directory, which skips the prompt on a first read outside the working
  directories. ACCEPTED: Claude Code's own built-in read-only set already
  runs `ls` and `head` without a prompt, and `Bash(git diff *)`
  (`git diff --no-index`) and `Bash(git show *)` already approve comparable
  reads. Neither command can write or reach the network.
- Sibling projects edit the same `permissions.allow` block, so merge
  conflicts are likely. Mitigation: each change only adds lines, so the
  resolution keeps both sides' rules.
- Future Claude Code matching semantics could change. Mitigation: the test
  follows the documented rules and cites the docs.

## Rollout

Merging to `main` takes effect for every new session in this repo. Consumer
repos receive the template copy on the next `.claude/` sync. There is no flag
and no migration. Rollback is a revert.

## References

- Issue #4749; sibling issues #4751, #4759, #4760, #4761, #4767.
- https://code.claude.com/docs/en/permission-modes.md ("How the classifier
  evaluates actions")
- https://code.claude.com/docs/en/permissions.md ("Compound commands",
  "Wildcard patterns", "Read-only commands")
- Precedent: the #4678 project (`claude/implement-plan-issue-4678-edit-files-without-python-heredocs`),
  AD-1 on protected paths.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Is this prompt by design, so the issue closes as not planned? — Picked: A — no, fix it. Every subcommand is a read, and the denial came from a classifier outage that an allow rule makes irrelevant. Alternatives: B — close as not planned because the outage was transient. Why: the same outage would deny the next stage's orientation read too. Closing an issue this session did not open is also a §23.C ask-first operation. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Where does the fix live? — Picked: A — exact `permissions.allow` rules in `.claude/settings.json` and its template twin (fix step 3 in the issue). Alternatives: B — a CLAUDE.md rule to use Glob and Read instead of `ls | head`; C — extend a `PreToolUse` hook to approve read-only compounds. Why: A is deterministic and the smallest change (§5). B depends on the model and leaves `git branch` uncovered. C is larger and also edits `.claude/**`. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Which rules? — Picked: A — `Bash(git branch --show-current)` (exact), `Bash(ls *)`, `Bash(head *)`. Alternatives: B — exact pipe forms only, such as `Bash(head -50)`; C — A plus `permissions.blockReadsOutsideWorkingDirectories`. Why: A covers the pattern without approving any write. B only fixes one line count. C changes read behaviour for every session, beyond this issue (§5). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Fold the sibling permission-prompt issues (#4751, #4759, #4760, #4761, #4767) into this project? — Picked: A — no, each keeps its own issue-mode project. Alternatives: B — fold them in. Why: `/implement-issue-claude` runs one issue per chain. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-28] Mirror the rules into `workflow-templates/.claude/settings.json`? — Picked: A — yes, keep the two files byte-identical. Alternatives: B — root file only. Why: consumer sessions hit the same classifier review, and the two files are identical today. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-28] Does the change need a `changelog.d/` fragment? — Picked: A — yes, `changed`. Alternatives: B — none. Why: §20.A requires one when a change alters what consumer repos receive on the next `@stable` sync. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` printed `{"skip": false, "label": null, "reason": "no skip label"}`, so the pass runs.

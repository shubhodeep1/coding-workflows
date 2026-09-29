# Run the security-pass skip check and the chain helpers as standalone Bash calls

Source issue: shubhodeep1/coding-workflows#4798 (https://github.com/shubhodeep1/coding-workflows/issues/4798)
Base branch: main
Security pass: run

## Summary

An unattended `/implement-issue-claude` session hit a permission prompt on an
already-allowlisted call, `security_pass_skip.py`, because it chained that call
with `echo "exit=$?"`, two `grep … | head` reads, and an `ls`. Tell sessions to
run that check, and every other allowlisted helper the chain names, as its own
Bash call, exactly as written.

## Context

- Issue #4798 (filed by `.claude/scripts/permission_prompts.py`, CLAUDE.md
  §23.I) records one **permission prompt** (not an Auto-mode denial, and no
  `Classifier unavailable` reason, so it is not part of the #4750 outage
  cluster) at 2026-09-28T10:38:00Z in session
  `session_01TnvYqW1gqscFF4mBgRJHdk`. The command was:
  `PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/security_pass_skip.py --repo … --issue 4786; echo "exit=$?"; grep -n … .github/workflows/ci.yml | head; grep -n "keep \`gh api\` calls out of loops\|…" CLAUDE.md workflow-templates/CLAUDE.md 2>/dev/null | head; ls workflow-templates/ | head -30`.
- The call itself is already allowlisted: `.claude/settings.json:52-53`
  (`Bash(python3 .claude/scripts/security_pass_skip.py *)` and the
  `PYTHONDONTWRITEBYTECODE=1` form). A compound command is matched part by
  part, and the extra parts (a `$?` expansion, an escaped backtick inside a
  quoted pattern, a `2>/dev/null` redirect) are what no rule approves and
  what Claude Code's shell check asks about. So no new allow rule is needed
  (issue fix order, step 1: change the command file so the call keeps a shape
  an exact rule approves).
- The command file that produced the call is
  `.claude/commands/implement-issue-claude.md:35` (step 6), which names the
  exact command but does not say to run it alone.
- The same failure mode applies to every helper `/implement-plan-claude`
  names. Its Helpers intro (`.claude/commands/implement-plan-claude.md:143`)
  forbids hand-built pipelines, loops, `$(...)`, and heredocs, but not
  chaining an allowlisted helper with other commands.
- Both command files have byte-identical twins under
  `workflow-templates/.claude/commands/`, enforced by
  `tests/test_implement_issue_claude_command.py::test_template_parity`.

## Goals

- `implement-issue-claude.md` step 6 says to run the skip check as its own
  Bash call, exactly as written, reading the exit status from the tool
  result, and never to append `; echo "exit=$?"`, a pipe, or other commands.
- `implement-plan-claude.md` Helpers intro says the same for every helper and
  every other allowlisted script call the command names.
- The `workflow-templates/.claude/commands/` twins stay byte-identical.
- A test in `tests/test_implement_issue_claude_command.py` pins both
  sentences, in both copies.

## Non-goals

- No new `permissions.allow` rule and no change to `.claude/settings.json`
  or any hook.
- No change to `security_pass_skip.py`, `permission_prompts.py`, or the
  classifier-outage handling (#4750).
- No change to CLAUDE.md, README.md, or agents.md.

## Constraints

- §5 minimal change set: two sentences plus their twins and one test.
- §6: no identifier is renamed or removed.
- §9: Markdown and Python keep their existing style (tests use tabs).
- §20: a `changelog.d/` fragment, since unattended sessions behave
  differently (fewer prompts).
- §28.C: the phase edits `.claude/**`, so it starts only under a recorded
  `Protected-path approval: phase 1` line.

## Approach

Prose guidance in the two command files, which is where the prompt
originated. The alternative, allowlisting `echo`, `grep`, `ls`, and redirects,
widens permissions for shapes no command prescribes and is rejected (issue fix
order; AD-1).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — standalone-call guidance.** `protected paths:
   .claude/commands/implement-issue-claude.md,
   .claude/commands/implement-plan-claude.md`.
   - Files: the two command files, their `workflow-templates/.claude/commands/`
     twins, `tests/test_implement_issue_claude_command.py`, a new
     `changelog.d/4798-standalone-helper-calls.md`.
   - Done: both sentences are present in both copies, the parity and new
     tests pass, and the full `tests/test_implement_issue_claude_command.py`
     suite passes.
   - Rollback: revert the PR; nothing else depends on the wording.

## Implementation Steps

1. `.claude/commands/implement-issue-claude.md:35`: after the skip-check
   command, add: run it as its own Bash call, exactly as written; its exit
   status is in the tool result, so never append `; echo "exit=$?"`, a pipe,
   or other commands to it (issue #4798).
2. `.claude/commands/implement-plan-claude.md:143`: append to the Helpers
   intro: run each helper, and every other allowlisted script call this
   command names (`check_in_status.py`, `claude_fix_claim.py`,
   `stale_routines.py`, `security_pass_skip.py`), as its own Bash call,
   exactly as written; chaining it with `;`, `&&`, `echo "$?"`, or other
   reads makes a command no allow rule matches, and the session stops at a
   prompt (issue #4798).
3. Copy both files byte-for-byte to `workflow-templates/.claude/commands/`.
4. Add `test_allowlisted_calls_run_standalone` to
   `tests/test_implement_issue_claude_command.py`, asserting both sentences
   in the live and template copies.
5. Add `changelog.d/4798-standalone-helper-calls.md` (`fixed`).

## Files & Modules

- `.claude/commands/implement-issue-claude.md`
- `.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/implement-issue-claude.md`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `tests/test_implement_issue_claude_command.py`
- `changelog.d/4798-standalone-helper-calls.md` [new]

## Testing

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_implement_issue_claude_command.py`
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_permission_prompts.py`
  (unchanged, confirms nothing in the filer depends on the wording).

## Risks

- A model can still chain commands despite the guidance. The filer then
  reports the new shape again, which is the intended feedback loop.
- The phase is blocked on the protected-path approval (§28.C) until a human
  answers on #4798.

## Rollout

Ships with the next `@stable` sync of `.claude/commands/` twins. No flag, no
migration.

## Auto-decisions

- AD-1 [plan, 2026-09-28] How should the prompt be removed? — Picked: A — tell sessions to run the allowlisted call alone (command-file guidance). Alternatives: B — add allow rules for `echo`, `grep`, `ls`, and redirects; C — close as not planned. Why: the call is already allowlisted; the prompt came from the chained extras, and the issue's fix order puts command-file changes first and never widens permissions for unprescribed shapes. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How wide should the guidance be? — Picked: A — the skip check in `/implement-issue-claude` step 6 plus one sentence in `/implement-plan-claude`'s Helpers intro covering every helper. Alternatives: B — only step 6; C — also CLAUDE.md §23.I and agents.md. Why: every helper has the same failure mode, and the Helpers intro is where the chain already sets shell-shape rules; CLAUDE.md is synced to every consumer and is not needed for this. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Is README.md or agents.md updated (§7)? — Picked: A — no; no env var, DB behaviour, or operational step changes. Alternatives: B — add a line to agents.md's §23.I helper section. Why: §5 minimal change set; the guidance lives in the command files the sessions read. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 4798`
  returned `{"skip": false, "label": null, "reason": "no skip label"}`.
- The issue carries no skip label.

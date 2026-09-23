# Implement-Plan Progress Logs

Per-plan progress logs written by the `/implement-plan-claude` command
(`.claude/commands/implement-plan-claude.md`).

Each plan being implemented phase by phase gets one file here:
`docs/implement-plan/<slug>.md`, where `<slug>` is the plan's filename
without `-plan.md` (`docs/plans/foo-bar-plan.md` → `foo-bar.md`).

The command **reads the matching log first** before opening any PR and
**resumes from the recorded `Stage:` / `Waiting on:` lines**, re-verifying
them against GitHub, so a fresh session — a new machine, a re-cloned
container, or just a later day — continues from the phase, security-pass
cycle, or validation cycle where the previous session stopped instead of
re-implementing phases that already merged. The log rides in each phase PR,
each validation-fix PR, and the completion PR, so the copy on the default
branch can lag the live session by one step.

See the **Progress Log** section of `.claude/commands/implement-plan-claude.md`
for the full file format and the read/update/persist contract.

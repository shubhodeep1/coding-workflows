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
each conformance or activation fix PR, each validation-fix PR, and the
completion PR, so the copy on the default
branch can lag the live session by one step; each stage session's
`— resume.` prompt carries the current stage in the meantime.

Each stage (a phase, a conformance audit, a security or
validation read, the completion PR, a `/verify-activation` cycle) runs in its own session titled
`implement-plan <slug> — <stage>`, started by a Sonnet checker session once
the previous stage's wait is over. A blocked-PR fix is the exception: the
checker hands a blocked, closed, or stuck PR back to the stage session that
opened it, which fixes it in place (a fresh `… — blocked PR` session only
when the hand-back fails).

Each log ends with a `## Lessons` section: one line per surprise a stage hit
(`- [source:<source>] <lesson> (files: <path>, …)`). When a PR from a
`claude/implement-plan-*` or `claude/verify-activation-*` branch merges,
`.github/workflows/issue_pr_status.yml` runs
`scripts/ingest_implement_plan_lessons.py`, which writes each lesson once to
the `ai-memory` branch as a `lessons_learned_record.v1` record. This README
is never ingested.

See the **Progress Log** section of `.claude/commands/implement-plan-claude.md`
for the full file format and the read/update/persist contract.

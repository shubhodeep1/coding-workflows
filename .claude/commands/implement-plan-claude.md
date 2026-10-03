Hand the plan documented at the path / reference in `$ARGUMENTS` to the **AI orchestrator** for implementation, **on the Claude engine**. This is the Claude-engine counterpart of `/implement-plan-ai`: it dispatches the same orchestrator with the same reference-only `project_description`, and marks the project `ai:engine-claude` so every role of its work (clarify, plan, implement, review, judges, security pass, validation) runs `claude -p` with Claude Opus 5.5 at `high` effort inside the Actions pipelines. It does **not** implement the plan in this session and opens **no** PR of its own. `$ARGUMENTS` is free-form prose that must resolve to a single plan markdown doc, exactly as for `/implement-plan-ai`.

The session-driven `/implement-plan-claude` chain (stage sessions, checkers, hand-back Routines, Claude-fixer hand-offs) was retired on 2026-10-03 (`docs/plans/replace-claude-sessions-with-cli-engine-plan.md`). The pipelines now do that work themselves.

$ARGUMENTS

## Procedure

1. **Run `/implement-plan-ai`'s procedure.** `Read` `.claude/commands/implement-plan-ai.md` and follow its steps 1–8 exactly (resolve the plan, read the context, make the plan reachable on the remote, pick the orchestrator wrapper, compose the reference-only `project_description`, dispatch, capture the run and the tracking issue, report), with the two differences below. Its Tool Access and Rules sections apply unchanged.
2. **Say which engine.** Add one line to the `project_description` instruction preamble: `Engine: Claude (ai:engine-claude) — run every role of this project on the Claude engine.`
3. **Label the tracking issue.** Once the `ai:orchestrator-tracking` issue for the dispatched run exists, add the `ai:engine-claude` label to it (a §23.B routine label write). If it has not appeared yet when you report, say so and give the command to add the label later. Until the engine-label plumbing lands (Phase 6 of the plan above), the label is recorded but inert: the project runs on each role's configured engine.

## Output Format

`/implement-plan-ai`'s output block, with `Mode: AI orchestrator on the Claude engine (dispatched — NOT implemented in this session)` and one extra line: `Engine label: ai:engine-claude on <tracking issue url> (or: pending — tracking issue not opened yet)`.

## Rules

- **Hand off — do not implement.** Everything in `/implement-plan-ai`'s Rules applies. If you find yourself editing source files or starting other sessions, you are in the wrong command.
- **Command-invoked dispatch (CLAUDE.md §23.C).** Invoking this command is the approval for the one orchestrator dispatch it performs; do not re-ask. Every other §23.C operation still needs the §2 Q/A ask.
- **`ai:codex` wins (plan decision D2).** If the tracking issue ends up carrying `ai:codex`, the project runs on codex; never remove `ai:codex` without asking.
- **§19.** Never reference the tracking issue with `Fixes/Closes/Resolves`.

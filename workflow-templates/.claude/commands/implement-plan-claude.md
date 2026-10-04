Hand the plan documented at the path / reference in `$ARGUMENTS` to the **AI orchestrator**, requesting the Claude engine. This is the Claude-engine counterpart of `/implement-plan-ai`: it dispatches the same orchestrator with the same reference-only `project_description` and requests the `ai:engine-claude` label for the project. Until the engine-label plumbing lands in Phase 6, that label is inert and roles still use their configured engines. It does **not** implement the plan in this session and opens **no** PR of its own. `$ARGUMENTS` is free-form prose that must resolve to a single plan markdown doc, exactly as for `/implement-plan-ai`.

The session-driven `/implement-plan-claude` chain (stage sessions, checkers, hand-back Routines, Claude-fixer hand-offs) was retired on 2026-10-03 (`docs/plans/replace-claude-sessions-with-cli-engine-plan.md`). The pipelines now do that work themselves.

$ARGUMENTS

## Procedure

1. **Run `/implement-plan-ai`'s procedure.** `Read` `.claude/commands/implement-plan-ai.md` and follow its steps 1–8 exactly (resolve the plan, read the context, make the plan reachable on the remote, pick the orchestrator wrapper, compose the reference-only `project_description`, dispatch, capture the run and the tracking issue, report), with the two differences below. Its Tool Access and Rules sections apply unchanged.
2. **Say which engine.** Add one line to the `project_description` instruction preamble: `Engine requested: Claude (ai:engine-claude). Until engine-label selection is wired in Phase 6, use each role's configured engine.`
3. **Label the tracking issue.** Once the `ai:orchestrator-tracking` issue for the dispatched run exists, add the `ai:engine-claude` label to it (a §23.B routine label write) and verify it is present. If the issue has not appeared or the label write fails (including when the label has not yet been synced), report the dispatched run and the missing label explicitly; do not claim the project is bound to the Claude engine. The workflow may already be running on the configured engine. Do not dispatch a second project to retry labelling.

## Output Format

`/implement-plan-ai`'s output block, with `Mode: AI orchestrator (Claude engine requested; dispatched — NOT implemented in this session)` and one extra line: `Engine label: ai:engine-claude on <tracking issue url> (or: not applied — <tracking issue pending | label-write error>; work may proceed on the configured engine)`.

## Rules

- **Hand off — do not implement.** Everything in `/implement-plan-ai`'s Rules applies. If you find yourself editing source files or starting other sessions, you are in the wrong command.
- **Command-invoked dispatch (CLAUDE.md §23.C).** Invoking this command is the approval for the one orchestrator dispatch it performs; do not re-ask. Every other §23.C operation still needs the §2 Q/A ask.
- **`ai:codex` wins (plan decision D2).** If the tracking issue ends up carrying `ai:codex`, the project runs on codex; never remove `ai:codex` without asking.
- **§19.** Never reference the tracking issue with `Fixes/Closes/Resolves`.

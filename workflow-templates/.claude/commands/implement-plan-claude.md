Hand the plan documented at the path / reference in `$ARGUMENTS` to the **AI orchestrator** with the Claude engine label. This is the Claude-engine counterpart of `/implement-plan-ai`: it dispatches the same orchestrator with the same reference-only `project_description`, with `engine=claude`, which labels the project `ai:engine-claude`. Roles use that label once their separate Phase 5 call sites are cut over; until then they use their existing engines. It does **not** implement the plan in this session and opens **no** PR of its own. `$ARGUMENTS` is free-form prose that must resolve to a single plan markdown doc, exactly as for `/implement-plan-ai`.

The session-driven `/implement-plan-claude` chain (stage sessions, checkers, hand-back Routines, Claude-fixer hand-offs) was retired on 2026-10-03 (`docs/plans/replace-claude-sessions-with-cli-engine-plan.md`). The pipelines now do that work themselves.

$ARGUMENTS

## Procedure

1. **Run `/implement-plan-ai`'s procedure.** `Read` `.claude/commands/implement-plan-ai.md` and follow its steps 1–8 exactly (resolve the plan, read the context, make the plan reachable on the remote, pick the orchestrator wrapper, compose the reference-only `project_description`, dispatch, capture the run and the tracking issue, report), with the two differences below. Its Tool Access and Rules sections apply unchanged.
2. **Say which engine was requested.** Add one line to the `project_description` instruction preamble: `Engine requested: Claude (ai:engine-claude); roles use it after their call sites are cut over.`
3. **Dispatch with `engine=claude`.** Add `-f engine=claude` to the orchestrator dispatch. `orchestrate.yml` then labels the tracking issue and the wave-1 issues `ai:engine-claude` at creation, and the poller and the implement PR copy the label to the project's later issues and PRs. Role cutovers must supply those work-item labels to engine selection (plan Phase 6).
   - If the dispatch is refused because the repository's orchestrator wrapper has no `engine` input yet (a consumer whose `@stable` sync is older), dispatch again without it, then add `ai:engine-claude` to the tracking issue once it exists (a §23.B routine label write). Verify the label is present before reporting it as applied; if the issue has not appeared or the label write fails, report the missing label and do not dispatch another project to retry labelling. Issues created before the label was added run on each role's configured engine.

## Output Format

`/implement-plan-ai`'s output block, with `Mode: AI orchestrator with Claude engine label (dispatched — NOT implemented in this session)` and one extra line: `Engine label: ai:engine-claude via engine=claude on <tracking issue url>` (or `added after dispatch — wrapper has no engine input`, or `pending — tracking issue not opened yet`). Note that engine selection awaits the role cutovers.

## Rules

- **Hand off — do not implement.** Everything in `/implement-plan-ai`'s Rules applies. If you find yourself editing source files or starting other sessions, you are in the wrong command.
- **Command-invoked dispatch (CLAUDE.md §23.C).** Invoking this command is the approval for the one orchestrator dispatch it performs; do not re-ask. Every other §23.C operation still needs the §2 Q/A ask.
- **`ai:codex` wins (plan decision D2).** If the tracking issue ends up carrying `ai:codex`, the project runs on codex; never remove `ai:codex` without asking.
- **§19.** Never reference the tracking issue with `Fixes/Closes/Resolves`.

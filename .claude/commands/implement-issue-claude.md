Hand **one standalone GitHub issue** to the Actions pipeline **on the Claude engine**: label it `ai:engine-claude` and restart its clarify → plan → implement → review flow with `/reclarify`, so every role of that issue's work runs `claude -p` with Claude Opus 5.5 at `high` effort. This command does **not** implement the issue in this session and opens no PR. `$ARGUMENTS` names the issue: a URL (`https://github.com/<owner>/<repo>/issues/<N>`), `<owner>/<repo>#<N>`, or `#<N>` for this repo.

The session-driven issue implementer (queue, pickup, dispatcher routine, issue-mode chain) was retired on 2026-10-03 (`docs/plans/replace-claude-sessions-with-cli-engine-plan.md`). Standalone issues run through the Actions pipeline; this label only chooses the engine.

$ARGUMENTS

## Procedure

1. **Resolve the issue.** Parse `$ARGUMENTS` to `<owner>/<repo>#<N>` and read it (`mcp__github__issue_read`, or `gh api repos/<owner>/<repo>/issues/<N>`). If it is missing, is a pull request, or the reference is ambiguous, stop and ask.
2. **Check that it is a standalone issue.** If it is closed, or carries `ai:orchestrator-managed` / `ai:orchestrator-tracking` (the orchestrator owns it), stop and report instead of relabelling.
3. **Check for `ai:codex`.** `ai:codex` wins over `ai:engine-claude` (plan decision D2). If the issue carries it, stop and ask (§2 Q/A) whether to remove `ai:codex`; never remove it unasked.
4. **Label it.** Add `ai:engine-claude` (a §23.B routine label write). Confirm the label is present on the issue before continuing. If the write fails (including a missing label in an unsynced repository), stop and report the error; never post `/reclarify` without the label.
5. **Restart the pipeline.** Post one comment whose first line is `/reclarify` (clarify only starts on a comment that begins with it), followed by one line naming the requested engine, and the attribution footer.
6. **Report.**

## Output Format

```
Issue: <owner>/<repo>#<N> — <title>
Engine: Claude (ai:engine-claude added)
Restarted: /reclarify posted (<comment url>)
Note: the Actions pipeline implements the issue; this session opened no PR.
```

## Rules

- **Hand off — do not implement.** No code edits, no branches, no PRs, no other sessions.
- **Until the engine-label plumbing lands** (Phase 6 of the plan above) the label is recorded but inert: the issue runs on each role's configured engine.
- **§19.** Never reference an `ai:orchestrator-tracking` issue with `Fixes/Closes/Resolves`.

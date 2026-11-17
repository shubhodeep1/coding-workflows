Hand **one standalone GitHub issue** to the Actions pipeline with the Claude engine label: add `ai:engine-claude` and restart its clarify → plan → implement → review flow with `/reclarify`. Claude is selected only for roles whose call sites support it; review still runs on OpenCode. This command does **not** implement the issue in this session and opens no PR. `$ARGUMENTS` names the issue: a URL (`https://github.com/<owner>/<repo>/issues/<N>`), `<owner>/<repo>#<N>`, or `#<N>` for this repo.

The session-driven issue implementer (queue, pickup, dispatcher routine, issue-mode chain) was retired on 2026-10-03 (`docs/plans/replace-claude-sessions-with-cli-engine-plan.md`). Standalone issues run through the Actions pipeline; this label is carried to the issue's PR for the separate role cutovers.

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
Engine label: ai:engine-claude added (Claude for cut-over roles; review on OpenCode)
Restarted: /reclarify posted (<comment url>)
Note: the Actions pipeline implements the issue; this session opened no PR.
```

## Rules

- **Hand off — do not implement.** No code edits, no branches, no PRs, no other sessions.
- **Role cutovers are separate.** The label is propagated by Phase 6, but roles still use their existing engines until their Phase 5 call sites are cut over. A missing Claude credential falls back to codex (D1).
- **§19.** Never reference an `ai:orchestrator-tracking` issue with `Fixes/Closes/Resolves`.

# CLAUDE.md §25.D: full reference

This is the full text of CLAUDE.md §25.D. CLAUDE.md keeps §25.A–C and
§25.E in full and a shorter §25.D that points here. Both are binding;
where this file adds detail, follow it. It reaches consumer repos with the
rest of `.claude/` through the `@stable` sync.

## §25. PR Watching Is Disabled (MANDATORY)

### D) Enforcement

The rule is enforced deterministically by `.claude/hooks/pr_watch_guard.py`,
wired as a `PreToolUse` hook in `.claude/settings.json` under the matcher
`mcp__.*__subscribe_pr_activity`. This hook guards subscriptions, not the
user-requested scheduled checks governed by §25.C. Prose alone cannot enforce
this: the harness prompt actively pushes toward offering a watch, and the
instruction is furthest from the context window's live edge exactly when a
session has run long enough to open a PR. The hook blocks every
`subscribe_pr_activity` call from any MCP server, never blocks
`unsubscribe_pr_activity`, issues no
API calls (§15), and fails open with a `systemMessage` warning when the hook
payload cannot be read, is invalid or non-object JSON, or guard evaluation
raises an internal exception. There is deliberately no environment-variable
escape hatch. The hook and the settings entry ship to consumer repos
through the same `.claude/` sync as the §21 guard;
`tests/test_pr_watch_guard.py` covers the rule and the wiring.
Empty or whitespace-only hook input is treated as an empty object and allowed
silently because there is no tool payload to evaluate.

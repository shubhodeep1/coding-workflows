<!-- changelog: changed -->
- **`/deploy-activate` now runs on the `best` model tier.** The activation runbook was pinned to `opus`; it joins `/implement-plan-claude` and `/verify-activation` on `best`.

`/deploy-activate` walks an operator through taking a DORMANT-but-complete project to LIVE, one step at a time, and the steps it emits mutate live infrastructure: repo variables, secrets, consumer wrapper edits, `@stable` releases, and DigitalOcean resources. A wrong step there is executed against production rather than re-run, which is the same argument `/implement-plan-claude` and `/verify-activation` are pinned on. `best` resolves to the latest Fable model where the account has access and falls back to `opus` where it does not, so no account loses the command. The pin table in `agents.md` under `## Interactive slash-command model pins` is updated, and its tier rationale no longer describes `best` as covering only two commands.

| The numbers that matter | Value |
| --- | --- |
| Commands on `best` | 3 (was 2) |
| Commands on `opus` | 4 (was 5) |
| Commands on `sonnet` | 5 (unchanged) |
| Files changed | 2 (`.claude/commands/deploy-activate.md`, `agents.md`) |

What this means for operators: `/deploy-activate` picks up the stronger tier with no action needed, and nothing else about the command changed — the runbook body, its stop-and-wait contract, and its activation log are all untouched. Consumer repos are unaffected: `.claude/commands/` is not part of the `@stable` sync, and the template copies carry no `model:` frontmatter.

### For contributors

`context` is deliberately still unset on this command. Per the constraints recorded in `agents.md`, a bare `model:` pin lasts one turn rather than one command, so a stop-and-wait command like `/deploy-activate` runs its first turn on `best` and later steps on the session model. Holding the pin across the whole runbook would need `context: fork` with `background: false`, which would move an operator-interactive paste-back command into a forked subagent — a different shape from the three currently-forked commands, which are all read-only. That tradeoff is left for a separate change.

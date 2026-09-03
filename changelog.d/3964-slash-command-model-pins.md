<!-- changelog: added -->
- **Every interactive slash command now pins its own model.** `.claude/commands/*.md` carry `model:` frontmatter, so `/write-plan` no longer runs on whatever the session happened to be set to.

Each of the 12 commands in `.claude/commands/` now declares the model it runs on, chosen by what the command actually does rather than by whatever `/model` the operator last set. Five dispatch-only, scaffolding, and read-only commands run on `sonnet`; five that diagnose failures, write code, or mutate infrastructure run on `opus`; `/implement-plan-claude` and `/verify-activation` run on `best`, which resolves to the latest Fable model where the account has access and falls back to `opus` where it does not. Three read-only commands also gained `context: fork` with `background: false`, which runs them in a foreground subagent so the model pin holds for the whole command instead of expiring at the end of the first turn. This changes nothing about the unattended pipeline, which selects its models through repo-vars and reads `unattended_system_instructions.md`.

| The numbers that matter | Value |
| --- | --- |
| Commands pinned | 12 |
| On `sonnet` | 5 |
| On `opus` | 5 |
| On `best` | 2 (`/implement-plan-claude`, `/verify-activation`) |
| Forked with `background: false` | 3 (`/apply-url`, `/audit-plans`, `/verify-activation`) |
| Sonnet 5 vs Opus 5 per 1M tokens | $2/$10 vs $5/$25 |

What this means for operators: a `/command` costs what its workload warrants without anyone remembering to switch `/model` first, and the pin is visible in the command file rather than in session state. The session model is untouched — the override applies for the turn and the session resumes on the next prompt.

### For contributors

Four constraints are recorded in `agents.md` under `## Interactive slash-command model pins` and are easy to break by accident. The `---` must be the file's first line or the frontmatter renders as prompt prose. The pin lasts one turn, not one command, so commands that stop for CLAUDE.md §2 Q/A questions run their later turns on the session model. `agent:` is deliberately unset on the forked commands, because a forked skill loads CLAUDE.md except when the agent is `Explore` or `Plan` — naming either would silently drop §0–§24. `background: false` is required so the fork keeps the full tool set and the ask-first flow can still reach the operator.

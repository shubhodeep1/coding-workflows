<!-- changelog: removed -->
- **Interactive slash commands no longer pin a model.** The `model:` frontmatter on every `.claude/commands/*.md` file is gone, so a `/command` runs on the model the operator picked for the session.

Each of the 12 commands in `.claude/commands/` previously declared its own model tier (`sonnet`, `opus`, or `best`), and three of them (`/apply-url`, `/audit-plans`, `/verify-activation`) also ran in a forked subagent via `context: fork` with `background: false` so the pin would hold past the first turn. All of those keys are removed. The command files now start directly with their prompt body, and the model in effect is whatever the session is set to, via `/model` or the session's configured model, for every turn of the command. The unattended pipeline is unaffected: it selects its models through repo-vars and reads `unattended_system_instructions.md`.

| The numbers that matter | Value |
| --- | --- |
| Command files with `model:` removed | 12 |
| Command files with `context: fork` / `background: false` removed | 3 |
| Consumer-repo files changed | 0 (`.claude/commands/` is not synced) |

What this means for operators: pick the model once with `/model` and every slash command honours it, including the later turns of commands that stop for CLAUDE.md §2 questions. Nothing about any command's body, arguments, or behaviour changed.

### For contributors

The `## Interactive slash-command model pins` section in `agents.md` is replaced by `## Interactive slash-command model selection`, which records that the absence of a pin is deliberate. Keep the command body as the first line of each file: a leading `---` is parsed as frontmatter.

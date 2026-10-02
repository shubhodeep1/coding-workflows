<!-- changelog: changed -->
- **Permission prompt issues now say when a denied command was already allowlisted.** Issues filed by `.claude/scripts/permission_prompts.py` also show the permission mode the session ran in.

Unattended stage sessions twice hit an Auto-mode classifier denial (`[Modify Shared Resources]`) for a standalone `git merge --no-edit origin/<issue base>`, although `.claude/settings.json` allowlists `Bash(git merge *)` (issue #5899). The standard "reshape the command or add an allow rule" advice cannot fix that case. When a denied call is a single command that a `Bash(...)` allow rule matches, the issue body and each "Seen again" comment now name the rule and say so. `permission_prompts.py report` lists the matching rule as `allow_rule` for each pattern. CLAUDE.md §23.B now lists the project-branch sync merge (`origin/<default>`, the issue base, or a pull request's base merged into a `claude/*` branch) as a routine write. The Auto-mode classifier reads CLAUDE.md.

| The numbers that matter | Value |
| --- | --- |
| New report key | `allow_rule` (rule string or `null`) |
| Extra API calls | 0 (reads the local `.claude/settings.json`) |
| Pattern signatures and issue titles | unchanged |

What this means for operators: an `ai:permission-prompt` issue that says **Already allowlisted** is not fixed by a settings change. The command was already approved by an allow rule, and the classifier decided it anyway. Further occurrences of the `git merge --no-edit *` pattern comment on #5899.

<!-- changelog: changed -->
- **`permission_prompts.py file` now files permission prompts per command family instead of per exact command shape.** A new variant of a known cause becomes a comment on that cause's `ai:permission-prompt` issue instead of opening another issue.

Before this change, every exact command shape got its own signature and its own issue. Between 2026-09-28 and 2026-09-30 that produced 60 `ai:permission-prompt` issues for 60 shapes, and the master session closed 51 of them by hand as duplicates, 29 of them of #4678 (inline `python3` heredocs). Each pattern now also has a family: the event, the tool, and, for Bash, the first command word with the subcommand or script the shape keeps (`git fetch`, `gh api`, `python3 check_in_status.py`), plus any heredoc, loop, or `$(…)` substitution. Env assignments and `export`, `cd …`, and `timeout` prefixes are skipped. A pattern with no exact-signature issue goes to its family's issue: the oldest open one, else the oldest closed as completed or not planned (not reopened). An issue closed as a duplicate never takes these comments.

| The numbers that matter | Value |
| --- | --- |
| Issues filed 2026-09-28 to 09-30 (one per shape) | 60 |
| Families the same 60 issues fall into | 22 |
| Largest family (`python3` + heredoc) | 26 issues, now one issue (#4678) plus comments |
| Extra GitHub API calls | 0 (the family lookup reuses the one list read) |
| New issue markers | `<!-- ai:permission-prompt:v1 sig=<id> -->` (unchanged) and `<!-- ai:permission-prompt-family:v1 family=<id> -->` (new line) |

What this means for operators: new variants of a known prompt now appear as "Seen again with a new command shape in this family" comments on the existing issue, so there is nothing to close as a duplicate and no extra Claude implementation session is queued for them. A genuinely new family still opens one routed issue.

### For contributors

Issues filed before this change have no family marker. Their family comes from the tool, event, and example command recorded in their body, so the first run after the sync already comments on #4678 rather than filing a new family issue. The v1 signature marker and `MARKER_RE` are unchanged, and an exact signature match still wins over a family match. Non-Bash tools, and Bash commands that do not parse, keep signature granularity, so an `Edit` under `.claude/**` never absorbs other edits. The family key lives in `command_family` / `pattern_family`, the issue choice in `choose_family_issue`, and the one-read index in `index_issues`. `existing_issues` keeps its return shape. Tests are in `tests/test_permission_prompts.py`, which now loads the `workflow-templates/.claude/` twin, with `test_template_parity` pinning the root copy.

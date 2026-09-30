<!-- changelog: security -->
- **A permission prompt reported to a consumer repo, or by another session, no longer stops coding-workflows from filing it.** `permission_prompts.py file` now skips only the occurrences an immediate report already delivered to the repository it files into.

The security audit (issue #5125) found that `file` skipped a pattern when any `report-now` entry in `~/.claude/permission-prompts/immediate-state.json` named it. That directory is shared by every session on a host, so an immediate report on a consumer repo's PR, or one session's report, suppressed later occurrences everywhere, and no central `ai:permission-prompt` issue recorded them. Each `report-now` entry now records the repository it posted to (`repo`) and the logged session its prompt came from (`log_session`). `file` counts an entry only when both match what it is filing, and a report in coding-workflows marks only its own session's occurrences as filed in `filed-state.json`, so every other session's occurrences, earlier or later, are still filed. Entries written before this change count for nothing, so an upgrade can add at most one extra "Seen again" comment per pattern and never hides one.

| The numbers that matter | Value |
| --- | --- |
| Sessions whose occurrences one report covers | 1 (the session that made it) |
| Repositories one report covers | 1 (the one it posted to) |
| New API calls | none |

What this means for operators: a prompt that blocked a consumer session still reaches the coding-workflows `ai:permission-prompt` issue when a coding-workflows stage on the same host runs `file`, and a session's report no longer hides another session's prompts on a shared runner.

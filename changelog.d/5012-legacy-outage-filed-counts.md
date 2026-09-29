<!-- changelog: security -->
- **A permission-prompt state file written by an older filer can no longer hide real Auto-mode denials.** `.claude/scripts/permission_prompts.py` now records which log records it has filed instead of a running count per pattern, and migrates the old counts on first read.

Filers released before #4750 grouped classifier-outage denials into the same patterns as real denials, so the per-pattern counts they saved in `~/.claude/permission-prompts/filed-state.json` included outages. With outages filtered out, a pattern's live count could sit below its saved count, and a later real denial with the same tool and command shape produced no issue and no comment until the count caught up (security audit finding #5012, A09). The state file is now `{"version": 2, "filed": {<signature>: [<log file>:<line>, ...]}}`, holding only real records. A legacy file is converted when it is read: its count covered the first records of that pattern in logging order (each record's timestamp, so a newer session log whose name sorts first cannot take an older log's place), outages included, and only the real records among them are treated as filed.

| The numbers that matter | Value |
| --- | --- |
| State file | `filed-state.json` (name unchanged), `"version": 2` |
| Worst case after migration | 1 extra "Seen again" comment per pattern, never a hidden denial |
| New GitHub API calls | 0 |

What this means for operators: every real permission prompt or Auto-mode denial reaches its `ai:permission-prompt` issue again, including in containers whose state was written by an older filer. No action is needed.

### For contributors

`load_keyed_records` returns `(<file name>:<line index>, record)` pairs; `load_records`, `_load_state`, and `_save_state` keep their signatures. An unreadable or unknown-version state is treated as empty. `tests/test_permission_prompts.py` covers the #5012 scenario, outages that arrive after the last filing, v2 dedupe, record keys, and malformed state.

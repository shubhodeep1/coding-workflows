<!-- changelog: security -->
- **Workflow dispatch inputs no longer enter Bash source code.** Release, validation, refresh, and workflow-log analysis jobs now bind untrusted inputs through step environments and validate them before use.

Direct interpolation previously let shell metacharacters be parsed before the surrounding Bash validation ran. The affected dispatch paths in `comprehensive-test-and-release.yml`, `mark-stable.yml`, `promote-main-to-stable.yml`, `test-and-mark-stable.yml`, `validate.yml`, `validation-refresh.yml`, and `workflow-log-analysis.yml` now reject malformed repository names, numeric values, SHAs, version tags, repository-relative paths, and branch refs with actionable errors. Invalid release timeout values that previously fell back silently now fail the run, while valid inputs, defaults, names, and outputs remain unchanged.

| The numbers that matter | Value |
| --- | --- |
| Workflows hardened | 7 |
| Existing input names changed | 0 |
| Valid-input defaults changed | 0 |

What this means for operators: malformed manual or reusable-workflow dispatch inputs fail before any command or API operation uses them, and valid dispatches continue with the same arguments and outputs.

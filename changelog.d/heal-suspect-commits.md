<!-- changelog: added -->
- **Stall and timeout heal issues can now list suspect commits.** Behind `WORKFLOW_HEAL_TREND_ENABLED` (off by default) the heal intake adds a `## Suspect commits` section after the run-time trend: the commits merged since the failing workflow last passed (short SHA, subject and the PR number of a squash merge), the pipeline files that changed in that range (`scripts/`, `.github/workflows/`, `workflow-templates/`, `prompts/`), and, when the trend reports a regression, the default-branch commits merged on the first slow day. A heal plan can then start from the change that most likely made runs slower.

Commit subjects and file names are shown as untrusted data and escaped. The intake reuses the last-green lookup the reproduction step already made, other failure shapes get no section and no extra API call, a failed read never blocks the issue, and with the switch off the heal issue is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_TREND_ENABLED=false`) |
| Commits shown | newest 15 of the range, plus up to 15 from the first slow day |
| Pipeline files shown | at most 20 (aggregate for the range) |
| Extra GitHub API calls | at most 3 per stall / timeout report; at most 9 with the trend |
| Log line | `WORKFLOW_HEAL suspect_commits outcome=<ok|partial|skipped> ...` |

What this means for operators: nothing changes until the switch is on; then stall and timeout heal issues name their likely culprits, without extra calls for other failures.

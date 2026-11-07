<!-- changelog: added -->
- **Heal issues can now let the fix touch the files that changed since the workflow last passed.** Behind `WORKFLOW_HEAL_SUSPECT_SCOPE_ENABLED` (off by default) the heal intake adds up to 8 pipeline files changed between the failing workflow's last green run and the failing commit to the heal issue's frozen scope, so a regression in a script the failing workflow only calls (as in release run 40876853022, `scripts/implement_step_prepush_test_gate.sh` and `workflow-templates/.claude/hooks/prepush_affected_tests.py`) is no longer held by the plan's out-of-scope gate.

The files come from GitHub's compare data, never from logs or the diagnosis. Removed files, a renamed file's old name, anything outside `scripts/`, `.github/workflows/`, `workflow-templates/` and `prompts/`, anything under a top-level `.claude/` or `.github/ai/`, and files missing at the scope commit are left out. The largest changes come first, and the scope's 20-path cap is unchanged. Only reports of this repository from a same-repository run qualify. When the run-time trend is also on, the suspect-commits step reuses this lookup instead of repeating it.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_SUSPECT_SCOPE_ENABLED=false`) |
| Files added to the scope | at most 8, ranked by lines changed |
| Extra GitHub API calls | 2 per report (0 when suspect-commit data already exists); at most 9 with the trend when the compare succeeds |
| Log line | `WORKFLOW_HEAL suspect_scope outcome=<added|none|partial|skipped> ...` |

What this means for operators: nothing changes until the switch is on, and heal issues already filed keep their scope.

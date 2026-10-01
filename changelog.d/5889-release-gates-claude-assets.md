<!-- changelog: fixed -->
- **A change to the shipped Claude command files now starts a stable release, and both release gates test the Claude assets before tagging.** Consumers receive `CLAUDE.md` and `workflow-templates/.claude/` on the same `@stable` sync as the workflow wrappers, and the release path now treats them that way.

The daily promote cycle (`scripts/promote_main_cycle.sh`) counted root `.claude/` as code but not the `workflow-templates/.claude/` twin that `update_workflows.yml` actually copies to consumers. A release window that changed only shipped command files (`workflow-templates/.claude/commands/*.md`) logged `PROMOTE_CYCLE_SKIPPED reason=no_code_changes` and waited for an unrelated code change. The same gap in the poller's untested-commit check (`comprehensive_cycle_is_code_path` in `scripts/orchestrate_poll_process.sh`) let a bot commit touching only those files pass as non-code. Both now count any `.claude/` directory as code. Separately, `test-and-mark-stable.yml` (`validate-scripts`) and `mark-stable.yml` gain a `Claude asset tests (CLAUDE.md, .claude hooks, scripts, commands)` step running the suites `ci.yml` already runs on PRs.

| The numbers that matter | Value |
| --- | --- |
| Test files added to each release gate | 21 (20 pytest files plus `tests/test_session_start_extract_repo_slug.py`) |
| Added gate time (local run) | about 40 seconds |
| Path pattern added to both classifiers | `*/.claude/*` |

What this means for operators: edits to Claude commands, hooks, scripts, or `settings.json` reach consumers on the next daily promotion even when nothing else changed, and a regression in those files fails the release instead of shipping. Coding-workflows' own Claude automation (issue pickup, the `claude-pr-catch-all` sweep) still runs from `main`, unchanged.

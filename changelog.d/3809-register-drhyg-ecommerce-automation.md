<!-- changelog: added -->
- **`shubhodeep1/drhyg_ecommerce_automation` is now a registered consumer repo.** It receives the `@stable` `repository_dispatch` on every release.

The repo was seeded with the standard profile from `@stable` in its seed PR (`drhyg_ecommerce_automation#1`): 12 standard-profile wrapper workflows plus `ai-update-workflows.yml`, the `.claude/` command and hook assets, and the root `CLAUDE.md`. This registration adds it to `.github/ai/consumer_repos.json`, so tagging a new `@stable` release dispatches an immediate sync to it instead of leaving it to the daily 04:00 UTC cron.

| The numbers that matter | Value |
| --- | --- |
| Consumer repos registered | 13 (was 12) |
| Wrapper workflows seeded | 13 (standard profile + self-updater) |
| Seed source ref | `stable` (`f7b0aa59`) |

What this means for operators: the release `GH_PAT` must hold `repo` scope on `shubhodeep1/drhyg_ecommerce_automation`, and that repo needs `GH_PAT` and `OPENROUTER_API_KEY` secrets plus `WORKFLOW_PROFILE=standard` set before its wrappers can run.

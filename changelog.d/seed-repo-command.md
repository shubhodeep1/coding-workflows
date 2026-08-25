<!-- changelog: added -->
- **New `/seed-repo` interactive command onboards a consumer repository in one run.** `/seed-repo <owner>/<repo> [core|standard|full]` (default `standard`) replaces the manual copy-the-templates onboarding steps.

From a Claude Code session, the command seeds a new consumer repo end to end: it copies the chosen profile's wrapper workflows from `workflow-templates/` at `@stable` into the target's `.github/workflows/`, always adds `ai-update-workflows.yml` (the sync's self-updater, which `update_workflows.yml` never creates on its own), mirrors the `workflow-templates/.claude/` command and hook assets plus the root `CLAUDE.md`, and lands it all as a single seed PR in the target repo. It then sets the target's `WORKFLOW_PROFILE` repo variable after a confirmation prompt (unset, the first sync run would widen a `core`/`standard` seed to `full`) and opens a second PR registering the repo in `.github/ai/consumer_repos.json` per CLAUDE.md §14. Already-seeded repos get a report instead of a second seed. The command ships in `.claude/commands/` and in `workflow-templates/.claude/commands/`, so existing consumers receive it on their next sync.

| The numbers that matter | Value |
| --- | --- |
| Command file | `.claude/commands/seed-repo.md` (mirrored to `workflow-templates/.claude/commands/`) |
| Default profile | `standard` (12 wrappers) |
| PRs opened per seeding | 2 (seed PR in the target, registration PR here) |
| Source ref for all copied files | `stable` |

What this means for operators: onboarding a new repo no longer requires hand-copying templates or remembering the §14 registry step — run `/seed-repo`, merge the two PRs, and add the `GH_PAT` / `OPENROUTER_API_KEY` secrets the command lists; the daily sync owns everything afterwards.

### For contributors

The command never edits templates and copies byte-for-byte from `stable`, so the consumer's first `ai-update-workflows.yml` run diffs clean. Secrets are deliberately out of scope — values never transit the chat; the command only names what to add.

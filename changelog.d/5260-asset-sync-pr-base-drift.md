<!-- changelog: security -->
- **The Claude-asset sync now checks a PR head's `.claude/` guards against the PR's own base, not only against the default branch.**

Before this change, `/implement-plan-claude` review and blocked-PR stages and `/fix-claude-pr` decided whether a PR head was running an old guard by diffing `.claude/hooks/**` and `.claude/settings.json` against the default branch alone. A PR head cut from a project branch before that branch gained a guard change was reported fresh whenever `main` held nothing newer, so the session kept running the older guard (security finding #5260, `A05:2021`, medium). The sync now runs a second three-dot diff against the PR's base. On a project branch that lands in the default branch it syncs the project branch, verifies the pushed branch holds every default-branch guard change, and merges it into the PR head; a failed verification stops with the same `ai:claude-blocked:v1` blocker as a `.claude/` conflict. On any other base (`stable`, another PR's head, a non-default issue base) it merges the PR's own base when that base is ahead on guards, and still never merges the default branch there.

| The numbers that matter | Value |
| --- | --- |
| Drift checks per PR head | 2 (default branch, PR base) instead of 1 |
| GitHub API calls added | 0 (one more `git fetch` of the PR base, local `git diff`) |
| Files changed | `implement-plan-claude.md` `### Claude-asset sync` steps 1–4, `fix-claude-pr.md` step 5 |

What this means for operators: a guard fix that reached a project branch before reaching `main` now also reaches the PR heads built on that branch before a stage or fixer session works on them. The new stops are a project branch that still lacks `main`'s guards after its sync, which names the check and the differing paths, a failed `git fetch` of the default branch or the PR's base, which names the fetch and its error line instead of comparing against a stale ref, and a drift check that cannot compare at all (`git diff` exit 128, such as `no merge base`), which is no longer read as drift.

### For contributors

The SessionStart drift log (`[session-start] claude_assets=…`) still compares against the default branch only, because the hook cannot learn a PR's base without a GitHub API call. Tests in `tests/test_claude_asset_sync_command.py` run the documented commands in a scratch repository that reproduces the #5260 case.

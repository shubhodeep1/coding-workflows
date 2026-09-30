<!-- changelog: security -->
- **The Claude twin sync-state check now runs the guard script from the protected base commit, so a PR can no longer weaken `scripts/claude_twin_sync.py` to pass its own check.** Before, the CI step ran the PR checkout's copy of the script.

The CI step "Claude twin sync state (CLAUDE.md §28.C)" in `.github/workflows/ci.yml` used to run `python3 scripts/claude_twin_sync.py check` from the checkout, which on a `pull_request` run is the PR's merge commit. One PR could change a `.claude/hooks/**` file and its twin together and also edit the script so the check reported success. The step now extracts `scripts/claude_twin_sync.py` from the event's base commit (the merge commit's first parent, or `github.event.before`) into a temporary directory and runs that copy. On `stable` events `main`'s tip is the fallback. A base commit that cannot be resolved or read fails the step.

| The numbers that matter | Value |
| --- | --- |
| Copy of the guard that runs | the base commit's, then `main`'s tip on `stable` |
| Bootstrap fallback | the checkout's copy, with a `::warning::`, only while no trusted commit carries the script (`main` before #4804 merges, `stable` before its next promotion) |
| GitHub API calls added | 0 |
| Security finding | `pr-controlled-sync-guard` (#5608, A08:2021, high) |

What this means for operators: a fix to `scripts/claude_twin_sync.py` itself is checked by the old copy until it lands on the base, and the job log names the commit whose copy ran. A PR can still edit `.github/workflows/ci.yml` to skip the step, so a PR that changes a guard path together with the workflow file still needs the owner's review of it.

<!-- changelog: changed -->
- **`/deploy-activate` no longer opens with Mac setup steps.** The runbook now assumes the operator's Mac already has Homebrew, git and `gh` installed and `gh` signed in.

Before, every runbook started with a "Prereqs: Homebrew, git, gh, auth, clone + checkout" step written for a fresh Mac. Now an install or sign-in step comes back only when pasted output shows one is needed, such as `command not found: gh` or an HTTP 401 from `gh`, and it covers only the missing piece. A clone or update step for the repo shows up only right before the first step that needs up-to-date repo files locally. It runs `git fetch`, checks out the project branch and runs `git pull --ff-only`, and clones the repo first if the output shows there is no clone. Steps that only run `gh … -R <repo>` need no local copy. Both `.claude/commands/deploy-activate.md` and the consumer template `workflow-templates/.claude/commands/deploy-activate.md` change.

What this means for operators: runbooks start at the first real activation gate. Existing activation logs under `docs/deploy-activation/` keep their already-done Prereqs steps and resume as before.

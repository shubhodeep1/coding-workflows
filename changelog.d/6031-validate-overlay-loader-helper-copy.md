<!-- changelog: fixed -->
- **Validation no longer fails during setup when the target branch is older than main.** The validate job's support staging now runs the overlay loader that ships with the staging helper, not the target checkout's copy.

`.github/workflows/validate.yml` clones the default branch and runs its `scripts/stage_workflow_support.sh`. In coding-workflows itself that helper treats the target checkout as its support root, so it ran the target branch's `scripts/load_workflow_overlay.py`. Since #6135 the helper passes `--trusted-source-repo` and `--trusted-root`, and a target branch cut before #6135 rejects them with exit 2. The run is then reported as "Validation harness generation failed". This hit orchestrator project #6031 (run 37430977754). The helper now prefers the loader next to itself, which always matches its flags and never comes from the target branch.

| The numbers that matter | Value |
| --- | --- |
| Failing run | 37430977754 (`Internal: AI Validate [tracking:6031]`) |
| Loader exit status before the fix | 2 (`unrecognized arguments: --trusted-source-repo ...`) |
| Flags added by | #6135 |

What this means for operators: orchestrator projects whose branch predates a main-side change to the overlay loader can be validated again without first merging main into the project branch.

### For contributors

`STAGE_SUPPORT_HELPER_DIR` is the helper's own directory, resolved from `BASH_SOURCE` at load time. `run_overlay_loader` falls back to the relative `scripts/load_workflow_overlay.py` when no sibling copy exists. `tests/test_validate_workflow_validate_bootstrap.py` runs the function against an older target loader to cover both paths.

The merged-PR guard also asks for confirmation instead of checking the session checkout when an `env`-wrapped commit has an unresolved directory. Its live and consumer-template copies are kept in sync.

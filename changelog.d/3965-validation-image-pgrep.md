<!-- changelog: fixed -->
- **The `python-repo-checks` validation image now ships `pgrep`.** Runtime validation of this repository no longer fails its repo checks on a missing process tool.

`scripts/codex_stall_guard.sh` verifies every isolated process group it launches with `pgrep` and fails closed when it cannot. The image rendered from `workflow-templates/validation-harness/python-repo-checks/Dockerfile.app.j2` (`python:3.12-slim`) did not install `procps`, so `40_repo_checks.sh` failed on `No such file or directory: 'pgrep'` and the diagnosis classified the run as `harness_error` (project #3965, run 36288327878). The template now installs `procps`, and the golden fixture and the family test assert it.

What this means for operators: a `harness_error` whose log shows `pgrep` missing is fixed by this change; no configuration is needed.

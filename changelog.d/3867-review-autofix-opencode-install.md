<!-- changelog: fixed -->
- **`review_autofix.yml` now installs the OpenCode CLI and warms its models.dev cache before the review pipeline runs.** This fixes every PR targeting `orchestrator/project-3845` failing review with "models.dev cache is not readable".

PRs that target the opencode-cutover integration branch run the reusable workflow pinned at `review_autofix.yml@main`, but stage their support scripts from the PR's own merge ref. Since PR #3864 landed the read-side opencode cutover on that branch, `review_run_reviewers.sh` generates a per-reviewer OpenCode config via `write_opencode_config.sh`, which hard-fails when `~/.cache/opencode/models.json` is missing. Main's workflow never installed opencode, so every reviewer, summariser, and editor slot failed at config generation and the run finished as `editor_empty_noop` (first seen on the run for PR #3867). The workflow now runs the pinned `install-opencode` composite action (version `1.18.23`, overridable via the `OPENCODE_VERSION` repo var) right after the Codex CLI install, mirroring the integration branch byte-for-byte so the eventual integration merge is a no-op for these hunks.

What this means for operators: review runs on integration-branch PRs succeed again without any manual action; runs on main-target PRs gain an inert opencode install (a few seconds) and no behaviour change, since main's scripts still invoke codex only.

### For contributors

The `test_production_review_path_remains_opencode_free` guard in `tests/test_opencode_live_smoke_workflow.py` now permits exactly the install-step strings and still rejects any other opencode appearance in `review_autofix.yml`.

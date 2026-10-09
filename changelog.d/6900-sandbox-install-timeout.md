<!-- changelog: fixed -->
- **A slow dependency install no longer skips the review editor.** Each install the review sandbox runs before the editor is bounded by `DEPENDENCY_INSTALL_TIMEOUT_SECONDS` (default 600), and a dependency container that outruns its 900-second budget counts as a failed install, not a failed isolation boundary.

Before this, `scripts/review_untrusted_sandbox.sh prepare` treated any non-zero exit of the dependency container, including the `timeout` kill, as `Review dependency isolation failed`. When pip backtracked through dozens of `ruff` and `structlog` releases for a consumer's unpinned `[dev]` extras, the `Install project dependencies (best-effort)` step failed after two 15-minute budgets (the Claude prepare and the OpenCode fallback), the editor was skipped and the PR looped on "AI review/autofix produced no output, will retry". Now the install is killed at the budget and reported with `::warning::Review dependency install timed out after <n>s`, the venv and pytest bootstrap still run, and the editor proceeds with whatever installed. The repository variable `DEPENDENCY_INSTALL_TIMEOUT_SECONDS` is passed through `review_autofix.yml`; an invalid value warns and uses 600. Any other non-zero container exit stays fatal.

| The numbers that matter | Value |
| --- | --- |
| Per-command install budget (default) | 600 seconds, `DEPENDENCY_INSTALL_TIMEOUT_SECONDS` |
| Container budget (unchanged) | 900 seconds |
| Editor time lost per review run before the fix | 31 minutes (`drhyg_ecommerce_automation` PR #70, 2026-10-09) |

What this means for operators: a consumer whose dependencies resolve slowly or fail still gets an editor run with partial validation, and the warning in the run log names the command that timed out. Pin the dependency ranges, or raise the variable, when the editor needs the full environment.

### For contributors

`tests/test_review_sandbox_install_timeout.py` runs the shipped script against a fake `docker` for the timed-out, fatal and successful container exits, the budget validation and the wrapper around every install command.

<!-- changelog: changed -->
- **The plan-archival completeness lint now runs inside the `PR checks` job, and no workflow other than CI reports a check named `lint`.** `.github/workflows/lint-plan-archival.yml` is removed; its work is a third step group of the `pr-body-lint` job in `.github/workflows/pr-checks.yml`.

A pull request that adds a file under `docs/completed/` used to start a separate `Lint plan-archival completeness` run whose job was named `lint`, the same check-run name as the full CI's aggregate job that the `main` ruleset requires. On such a pull request the small lint could satisfy that requirement before CI had run. The lint now runs as steps of `pr-body-lint`, so a violation fails that check instead, and the pipeline's merge gate blocks on it as it blocked on the old `lint`. A plan moved into `docs/completed/` with `git mv` is now checked; the old diff treated a moved file as a rename and skipped it.

| The numbers that matter | Value |
| --- | --- |
| Jobs per PR event when the PR changes `docs/completed/**` | 2 before, 1 after |
| Jobs per PR event for any other PR | 1 before, 1 after |
| Workflows with a job named `lint` | 2 before (`ci.yml`, `lint-plan-archival.yml`), 1 after (`ci.yml`) |
| Checkout depth of the `pr-body-lint` job | 2 (was 1; the removed workflow fetched full history) |
| `ORCH_FINAL_MERGE_REQUIRED_CHECKS` default | unchanged |

What this means for operators: a red `pr-body-lint` can now mean either a CLAUDE.md §19 auto-close keyword violation or an incomplete plan archival. Open the run: the failed step is named after the lint that failed, and a plan-archival failure prints an error titled `Plan-archival completeness lint (not the PR-body lint)`. The workflow name `Lint plan-archival completeness` no longer appears in the Actions list. GitHub's merge button does not wait for `pr-body-lint` unless you add it to the `main` ruleset. Consumer repos never received this workflow; they get only the updated wording in the `/audit-plans` and `/implement-plan-ai` commands.

### For contributors

The old `paths: docs/completed/**` filter became the step "Enumerate added files in PR", which runs on every event and diffs the merge commit against its first parent (`git diff -z --name-status --find-renames HEAD^1 HEAD`), so the job needs `fetch-depth: 2` and no API call. A rename into `docs/completed/` from outside counts as an added file; a rename inside it does not. The step fails closed when `HEAD` is not a two-parent merge commit of the event's head, or when an added path under `docs/completed/` cannot be listed. The lint steps have no `continue-on-error`, the job still has no job-level `if:`, and its trigger and permissions are unchanged. `tests/test_pr_checks_workflow.py` runs the enumerate step's shell body against a temporary repository; `tests/test_lint_pr_body_auto_close.py` now allows a job named `lint` in `ci.yml` only.

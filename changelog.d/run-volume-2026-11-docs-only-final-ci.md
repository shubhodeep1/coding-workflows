<!-- changelog: changed -->
- **A PR that only changes docs no test reads gets a final CI run without the test jobs.** `static-checks` and the required `lint` still run, and `lint` checks the merged tree itself before it accepts the skipped jobs.

When AI review finishes with a head, `_pr_ensure_final_ci` (`scripts/pr_checks_lib.sh`) now reads the PR's changed files. If every one is on the list in `scripts/ci_docs_only_paths.sh` (plans, postmortems, triage notes, analysis reports and a few named docs pages), it dispatches `ci.yml` with `docs_only=true`. The seven test jobs skip. In that run `lint` checks out `refs/pull/<n>/merge`, confirms it is the merge of the run's head and that every changed file is on the list, and only then counts the skipped jobs as passing. Any file outside the list, an unreadable listing, or 100 or more files runs the full suite as before. Push runs on `main`, `stable` and `orchestrator/project-*` always run everything, because check-failure triage and heal read their results.

| The numbers that matter | Value |
| --- | --- |
| Test jobs skipped in a docs-only final run | 7 jobs, 36 runner jobs with their matrix shards |
| Extra API reads | 1 `GET pulls/<pr>/files?per_page=100` per final-CI dispatch |
| Largest PR that can be docs-only | 99 files |

What this means for operators: a docs-only PR gets its `lint` after a few minutes instead of after the full test suite. A `lint` that fails with `CI_DOCS_ONLY_GATE mode=verify outcome=fail` means the dispatcher's docs-only call was wrong, and the PR goes back to review as with any red final run. A branch created before this change refuses the `docs_only` input, and the dispatcher retries once without it. Consumers are unaffected, because their final CI is off (`REVIEW_FINAL_CI_WORKFLOW` is empty).

### For contributors

The list lives in `ci_docs_only_path_is_safe` in `scripts/ci_docs_only_paths.sh`. Add a path only after checking that no test job or CI step reads it. Tests: `tests/test_ci_docs_only_final_ci.py`.

<!-- changelog: fixed -->
- **A conflicted PR whose same-head review state is terminal now gets its merge conflict resolved, and reviewer slots added on main no longer fail on older PR branches.** Before this, PR #4332 was re-dispatched every poller tick for six hours without the conflict ever being touched, and PR #4323 lost two of its six reviewers before launch.

When a PR's cached same-head resume state is terminal (`resume_state=no_progress` or `round_budget_exhausted`), `review_autofix.yml` skipped `Detect merge conflicts` and both resolver steps, even though the gate keeps a conflicted PR on `codex-agent` so those steps can run. The poller's standalone conflict sweep dispatched PR #4332 again and again, and every run restored the terminal state, skipped the resolver, and exited green. Those steps now run on terminal resumes, and `Push all pending commits` pushes on that path only when the conflict was resolved. The editor and `Commit changes` still skip, so a terminal run never pushes autofix edits. Separately, `REVIEWER_MODELS` comes from the workflow ref while `scripts/codex_model_catalog.json` is staged from the PR branch, so a branch forked before the September reviewer roster refresh had no row for `z-ai/glm-5.2` or `google/gemini-3.1-flash-lite`. `Stage workflow support files` now appends the missing rows from the main snapshot and leaves the branch's own rows alone.

| The numbers that matter | Value |
| --- | --- |
| No-op review dispatches on PR #4332 (`ai/issue-4329`) | 40+ between 2026-09-23T18:00Z and 2026-09-24T00:03Z |
| Reviewer slots lost per run on PR #4323 (run 35933627432) | 2 of 6 |
| New log key | `MODEL_CATALOG_BACKFILL added=<n> slugs=<list> source=main_snapshot` |
| Extra GitHub API calls | 0 |

What this means for operators: a conflicted PR stuck in a terminal review state now gets its merge resolved on the next dispatch instead of looping silently, and a PR branch that predates a reviewer roster change keeps its full reviewer panel. A catalog read or parse failure only warns and leaves the branch catalog as it was.

### For contributors

`tests/test_review_autofix_review_pipeline_contract.py` evaluates the real `if:` predicates for a terminal, conflicted run, and runs the extracted backfill block followed by `scripts/write_opencode_config.sh` against a stale catalog. All four new tests fail against the previous workflow. The re-trigger dispatch step keeps its terminal skip, because the resolved push's `synchronize` event reviews the new head.

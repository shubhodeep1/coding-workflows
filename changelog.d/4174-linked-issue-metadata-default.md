<!-- changelog: fixed -->
- **Self-repo review runs no longer die in "Collect PR metadata" when a PR-branch helper reads `LINKED_ISSUE_METADATA_FILE`.** `review_autofix.yml` now exports the artifact path alongside `LINKED_ISSUE_CONTEXT_FILE`.

In this repository a pull request's review executes the PR-head copies of the `scripts/` helpers under `review_autofix.yml@main`, because `internal-review.yml` pins the reusable workflow to `main` while the support-ref step stages helpers from the PR's commit. PR #4174 made `scripts/review_collect_pr_metadata.sh` require `LINKED_ISSUE_METADATA_FILE` and added the export only to its own copy of the workflow, so every review run at that head failed before the reviewers started and the stall poller re-dispatched the same failure four times. `main` now exports `LINKED_ISSUE_METADATA_FILE=${RUNTIME_DIR}/linked_issue_metadata.json` in the "Initialize runtime workspace" step, `agents.md` documents the staging skew, and `unattended_system_instructions.md` §8 tells the editor that a new variable read by a staged helper must default inside the helper.

| The numbers that matter | Value |
| --- | --- |
| Workflow export added | `.github/workflows/review_autofix.yml`, "Initialize runtime workspace" |
| Failed review runs at one head | 35546298657, 35549937758, 35551938072, 35552937934 |
| Incident PR | #4174 (`ai/issue-4173`, head `662aacb`) |

What this means for operators: a review of a self-repo PR that ships this artifact no longer stalls on the env check, and the retry loop on PR #4174 ends once its branch also carries the in-helper default.

### For contributors

Nothing on `main` reads the new variable yet; the export exists so PR-head helpers that do read it find the same path the PR's workflow copy would set. The general rule is the in-helper default, not a `main`-side export per artifact.

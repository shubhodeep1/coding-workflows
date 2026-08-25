<!-- changelog: fixed -->
- **The review editor's changelog fragments now survive to commit.** The consumer-repo new-file cleanup in `scripts/review_commit_changes.sh` exempts top-level `changelog.d/*.md` fragments, so a review round whose fix is creating the required fragment commits normally instead of dead-ending on a false "Editor changes lost" alert.

Reviewers flag a missing changelog fragment per the §20 convention, and the editor's usual fix is creating one new file under `changelog.d/`. The commit step's consumer-repo cleanup deleted every editor-created untracked file before staging ("editor may not create new files"), which erased the fragment, left the tree with nothing to commit, and fired `EDITOR_CHANGES_LOST` — blocking auto-merge on a round that had actually produced the requested fix. The failure was deterministic: a retried round recreated the fragment and hit the same deletion. Observed on `tele-funtoken-msg-scoring` PR #3764 (review run 32732281452), where the editor's only claimed change was `changelog.d/3763-uniswap-comp-admin-stats-aggregator.md` and the run's own log shows the cleanup removing that exact path two steps before the changes-lost error.

| The numbers that matter | Value |
| --- | --- |
| Paths exempted | top-level `changelog.d/*.md` fragments |
| Other new-file cleanup behaviour | unchanged (strays still removed, incl. non-`.md` files and nested Markdown under `changelog.d/`) |
| Reference run | `tele-funtoken-msg-scoring` 32732281452 |
| New tests | 5 in `tests/test_review_commit_changelog_fragment_preserved.py` |

What this means for operators: fragment-only review rounds in consumer repos now commit and push like any other fix, so the "Editor changes lost (retry unavailable)" alert no longer fires for this case and auto-merge is not blocked on it. The workflow-source repo path is untouched — it already preserved editor-created files.

### For contributors

The exemption sits in the existing removal-loop `case` statement beside `.serena` / `scripts/` / `prompts/`, and the surviving fragment is staged by the existing consumer untracked-files `git add` pass — no new staging logic. This pairs with the `-X GET` probe fix (fragment `3763-changes-lost-redispatch-get-method.md`): that PR repaired the retry after a changes-lost event; this one removes the pipeline-inflicted cause of the event for fragment-only rounds.

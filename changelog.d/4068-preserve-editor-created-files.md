<!-- changelog: fixed -->
- **Editor-created files now survive to commit in consumer repos.** The review autofix commit step reconciles new files against a pre-editor snapshot instead of deleting every file the editor created, so a review round whose fix is a new file (a `db/contracts/*.yml` contract, a regression test) commits normally instead of dead-ending on "Editor changes lost".

Reviewers regularly ask for a new file, and the editor creates it, but the consumer-repo branch of `scripts/review_commit_changes.sh` removed every editor-created untracked path before staging under the old "editor may not create new files" policy (with a single carve-out for `changelog.d/*.md` fragments from the #3763 fix). The tree was clean at commit time, the run fired `EDITOR_CHANGES_LOST`, the automatic re-dispatch produced the same file and the same deletion, and once the one-shot retry budget was spent the workflow blocked auto-merge on a PR that looked green. On `tele-funtoken-msg-scoring` PR #4287 this took three consecutive editor rounds (runs 34196277121 and 34198075113) that each reported "Added `db/contracts/settings.yml`" while the run log shows the cleanup removing that exact path two steps later. The editor prompt also told the model not to create files at all, contradicting the reviewers it was applying.

Now the "Apply fixes with editor model" step records the paths that were already untracked before the editor ran (`PRE_EDITOR_UNTRACKED_FILE`, both repo kinds), and the consumer cleanup keeps any new path that is absent from that list, removes paths that were untracked beforehand (strays, leftovers) and pipeline-owned artifacts (`.ai/`, `pre_assembled_static.txt`, fetched support files) even when new, and falls back to the legacy delete-all behaviour when the snapshot is missing. Every removed path is written with its reason to `REVIEW_REMOVED_NEW_FILES_FILE`, and the "Editor changes lost" PR comment lists them, so a future stall names its cause on the PR. The editor prompt's file-creation policy now allows a new file when a reviewer finding, the PR's scope, or a documented repository convention requires it, and requires every created file to be listed by path.

| The numbers that matter | Value |
| --- | --- |
| Reference PR / runs | `tele-funtoken-msg-scoring` #4287, runs 34196277121 and 34198075113 |
| Editor rounds lost to the deletion there | 3 (one committed partially, two produced no commit) |
| New env inputs | `PRE_EDITOR_UNTRACKED_FILE` (snapshot), `REVIEW_REMOVED_NEW_FILES_FILE` (removal log) |
| Paths still removed when new | `.ai/**`, `.github/ai/**`, `.github/prompts/**`, `.github/scripts/**`, `ai-memory/**`, `.codex-workflow-src*`, `node_modules`, `pre_assembled_static.txt`, `unattended_system_instructions.md`, `ai_pipeline.md`, `agents.md` |
| New tests | 5 in `tests/test_review_commit_new_files_preserved.py` |

What this means for operators: after the next `@stable` promotion, a consumer-repo review round that creates a contract, test, or other convention-required file commits and pushes like any other fix, and the "Editor changes lost (retry unavailable)" alert no longer fires for it. When the cleanup does drop a new file, the PR comment now says which path and why, instead of only "no commit was produced". The workflow-source repo path is unchanged; it already staged only editor-touched files.

### For contributors

The reconciliation lives in the existing removal loop in `scripts/review_commit_changes.sh`; the `NEW_FILES_BEFORE_COMMIT_FILE` block boundaries are preserved so the existing fragment-preservation tests keep extracting it. Preserved files are staged by the existing consumer untracked-files `git add` pass and remain subject to the write guard and protected-path resets. The snapshot is taken in the same step that already snapshots the workflow-source repo (`PRE_EDITOR_STATE_FILE` is untouched), and the fallback keeps consumers on an older wrapper on the old behaviour rather than failing open.

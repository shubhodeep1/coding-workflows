<!-- changelog: fixed -->
- **The coding-workflows merge-conflict resolver no longer fails closed when its model stages the file it resolved.** The resolver model now works on a private copy of the merge index, so `git add` inside the model leaves the real index untouched.

In this repository, `Internal: AI Review & Autofix` failed six times on PR #5596 at `Run Codex resolver, validate, stage, commit` with `Resolver scope check failed closed (ValueError).` The model had run `git add` on the permitted conflicted file, which changed the live merge index that both attempt-scope guards in `scripts/review_conflict_resolve.sh` require to stay unchanged. Each attempt now gives the model a fresh copy of the captured index through `GIT_INDEX_FILE`, and the script still stages and commits the accepted resolution itself. A model that writes the real index anyway still stops the run, and out-of-scope edits are still restored and rejected by `check_resolver_diff.sh`. The resolver's own OpenCode config also sets `snapshot: false`, because OpenCode's snapshot git calls inherit the environment and would otherwise overwrite the model's index copy.

| The numbers that matter | Value |
| --- | --- |
| Failed review runs on PR #5596 before the fix | 6 |
| Private index path | `${RUNTIME_DIR}/resolver_model_index` |
| Repos affected | coding-workflows only (`IS_WORKFLOW_SOURCE_REPO=true`) |

What this means for operators: conflicted PRs in coding-workflows, such as `stable` forward-merges, resolve instead of dead-ending in repeated `conflict_resolver_failed` runs and workflow-heal issues. Consumer repositories see no behaviour change.

### For contributors

`tests/test_review_conflict_resolve_retry_prelude_render.py` builds a real merge conflict and runs a stub model through the retry loop's own private-index block. It covers permitted staging, out-of-scope staging, a model that bypasses the copy, a fresh copy on every attempt, and the snapshot opt-out.

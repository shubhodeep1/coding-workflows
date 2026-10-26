<!-- changelog: fixed -->
- **PRs with criss-cross merge history no longer stay conflicted forever, and a release no longer opens two forward-merge PRs.** The review pipeline now commits the clean merge when git and GitHub disagree about conflicts, and `forward-merge-stable-to-main.yml` reuses an open fallback PR for the same `stable` commit.

Forward-merge PR #8170 sat open with "merge" judge decisions it could never carry out. A release pushes to `stable`, which starts `forward-merge-stable-to-main.yml`, and the release's `sync-to-main` job dispatches it again for the same commit, so two PRs (#8169 and #8170) were opened and each resolved the same conflict separately. Once #8169 merged, #8170 and `main` had two merge bases: git merged them cleanly, so "Detect merge conflicts" pushed nothing, while GitHub merged from one base and kept reporting the PR as conflicting. Now the detect step tests each merge base on its own when there is more than one, and when GitHub would see a conflict the conflict-prepare step commits git's clean merge without a model run. The forward-merge workflow checks for an open `auto/forward-merge-stable-*` PR that already contains the `stable` tip before opening a new one, and the review-blocked judge merges those PRs with a merge commit instead of a squash.

| The numbers that matter | Value |
| --- | --- |
| Forward-merge runs per release for one `stable` commit | 2 (push + `sync-to-main` dispatch) |
| Fallback PRs opened for one `stable` commit, before / after | up to 2 / 1 |
| Extra GitHub API calls | 1 paginated REST list (one call per 100 open PRs), only on the forward-merge fallback path |
| New env var | `MERGE_CRISS_CROSS_CLEAN` (step-to-step, defaults `false`) |

What this means for operators: a PR that GitHub marks conflicting while the pipeline's own merge is clean now gets an `[ai-merge-resolve]` merge commit on its next review run instead of looping until the conflict-attempt cap. Forward-merge PRs that the judge merges land as merge commits, so `promote-main-to-stable.yml`'s ancestry pre-flight keeps passing.

### For contributors

New log prefixes: `AUTOFIX_MERGE_CRISS_CROSS`, `RB_JUDGE_MERGE_METHOD`, `FORWARD_MERGE_FALLBACK`. The single-base probe needs `git merge-tree --merge-base` (git 2.40+); on older git the probe fails open to the previous behaviour. Tests: `tests/test_review_merge_criss_cross.py` builds the #8170 history and runs the real detect and prepare scripts; `tests/test_forward_merge_fallback_dedupe.py` runs the fallback step body against a local origin.

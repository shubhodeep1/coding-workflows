<!-- changelog: fixed -->
- **The conflict resolver no longer rejects correct resolutions when `CLAUDE.md` is in conflict.** Symlinks are now compared by their link text when the resolver and the autofix editor work out which files they touched.

`workflow-templates/CLAUDE.md` is a symlink to `../CLAUDE.md`. The resolver hashed it with `git hash-object`, which follows the link, so the symlink looked edited every time `CLAUDE.md` changed. `check_resolver_diff.sh` then aborted the resolution as "edited files outside the conflicted set". Because the check fails the same way on every retry, PR #4443 hit the identical-failure cap and stayed `ai:review-blocked`. A symlink that is actually retargeted or deleted is still reported as touched.

| The numbers that matter | Value |
| --- | --- |
| Sites fixed | 4 (`review_conflict_prepare.sh`, `review_conflict_resolve.sh`, `review_commit_changes.sh`, the pre-editor snapshot in `review_autofix.yml`) |
| Identical failures on PR #4443 before the cap | 6 |

What this means for operators: PRs that conflict on `CLAUDE.md` now resolve through `review_autofix.yml` like any other conflict. No setting changes.

### For contributors

`tests/test_touched_set_symlink_aware.py` runs the extracted snapshot and compare loops against a repo that contains a symlink, and pins the two snapshot loops as identical.

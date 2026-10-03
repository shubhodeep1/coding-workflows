<!-- changelog: security -->
- **The Claude-fixer GPT judge now needs a rejection posted as the PR author or the workflow's own account to run, only trusts rulings about unchanged code, and obeys the workflow-edit policy when it commits a fix.** These were raised by the second security audit of the Claude-fixer convergence project (#6061, #6062, #6063).

A `claude_fixer_judge_head` dispatch used to run the judge even when nobody had posted a rejection for that head and round, so anyone able to dispatch the workflow could have it skip the reviewers and merge. The judge now decides nothing without an `ai:claude-fixer-rejection:v1` comment for that head and round from a collaborator posting as the PR author or the workflow's GH_PAT account, posted after the hand-off of the ledger being judged (skip reason `claude_fixer_rejection_missing`, PR labelled `ai:review-blocked`). Sticky rulings now demote a finding only while nothing in the pull request changed since the head the `invalid` ruling was made on, and the newest matching ruling (by judge run) decides, so a later `upheld` ruling or a code change keeps the finding blocking. The judge's `[judge-fix]` commit now drops `.github/workflows` edits, new untracked files included, unless `ALLOW_WORKFLOW_EDITS` is `true` and runs the write guards (`.github/ai/write_guards.v1.json`) before anything is committed or pushed.

| The numbers that matter | Value |
| --- | --- |
| New judge skip reason | `claude_fixer_rejection_missing` |
| Sticky-ruling code binding | `git diff --quiet <ruled head> HEAD` |
| Guards on the `[judge-fix]` commit | `ALLOW_WORKFLOW_EDITS`, `write_guard_check review_editor` |

What this means for operators: a bare dispatch can no longer run the judge, and any change is always re-reviewed. Fixing sessions already post the rejection comment before they dispatch, so nothing changes for them.

### For contributors

`scripts/review_autofix_step_claude_fixer_judge.sh` exits not-ready on a missing rejection and reuses the gate's resolved workflow login (`FINGERPRINT_CAP_MARKER_AUTHOR_LOGIN`) before falling back to `gh api user`; `scripts/review_claude_fixer_judge.py` gains `git_tree_unchanged` (any git doubt counts as changed, memoized per ruled head) and tags each prior ruling with its judged `head`; `scripts/review_rb_judge.sh` runs the guards in Claude mode only. Tests: `tests/test_review_rb_judge_claude_mode.py`, `tests/test_review_autofix_claude_fixer_mode.py`.

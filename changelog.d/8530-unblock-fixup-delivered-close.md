<!-- changelog: fixed -->
- **The unblock judge no longer re-plans a heal issue whose fix-up already shipped its whole fix on the default branch.**

Heal issue #8481 targeted `stable`. The unblock judge filed fix-up #8484, which merged into `main` as pull request #8486 and held the whole fix. The judge then posted `/answer`, and planning ran on `stable`, which did not have #8486 yet, and proposed the same retry again. Now, when a fix-up merges into the default branch and its pull request changed every file a pipeline-authored `ai:workflow-heal` issue lists under Affected files, and the issue targets another branch, `scripts/unblock_judge.sh` closes the issue as completed with `ai:closed` and an evidence comment instead of resuming it.

| The numbers that matter | Value |
| --- | --- |
| Extra API reads, only after every local check passes | 2 (the merged fix-up pull request, its file list) |
| Writes on a close | 3 (comment, label, close), plus the wait update |
| New kill switch | `UNBLOCK_FIXUP_DELIVERED_CLOSE_ENABLED` (default `true`; script environment only, not forwarded by `unblock_judge.yml`) |

What this means for operators: a heal issue whose fix landed on `main` closes itself and says the fix reaches `stable` with the next release, instead of coming back with a duplicate plan. Reopen it and reply `/reclarify` if work is still missing. Orchestrator items, issues that target the default branch, and issues whose Affected files list cannot be read in full resume as before.

### For contributors

The check runs after the #8470 nothing-to-implement close. `scripts/unblock_actions.py fixup-delivered-body` does the body parsing without the network. Any read failure resumes; a failed write keeps the fix-up wait open, and the marker `<!-- ai:unblock-fixup-delivered:v1 item=<n> fixup=<f> pr=<pr> -->` stops the comment from being posted twice. Log: `UNBLOCK_JUDGE ... op=fixup_delivered outcome=closed|resume|skip`.

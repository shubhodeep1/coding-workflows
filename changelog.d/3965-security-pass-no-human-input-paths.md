<!-- changelog: fixed -->
- **Security-pass projects no longer hand their fix and follow-up issues to a human.** Advisory follow-ups are filed only after the project merges, and the implement editor edits the branch's own helper scripts instead of `main`'s copies.

Project #3965 showed two ways the security pass produced issues that stopped for human input even though nothing in them needed a person. The exhaustion judge's advisory follow-ups (#4090, #4091) were filed against `main` while the code they cite existed only on `orchestrator/project-3965`, so the planner answered `BLOCKED: PR #3968 is still open` and both issues sat in `ai:blocked`. The cycle-7 fix issue (#4113) halted in `ai:needs-human` because `implement.yml` had replaced the branch's `scripts/codex_helpers.sh` with `main`'s copy before the editor ran, and the editor's change could not be merged back onto the branch's version (477 lines apart). The orchestrator now keeps accepted findings as pending waivers and files their `ai:security` follow-ups from the final-merge arms, with a body line naming the merging PR. The implement workflow now runs `scripts/implement_staged_support_workspace.sh` around the editor and repair loops, so the editor sees `HEAD`'s helpers and its edits commit as plain edits (`IMPLEMENT_STAGED_SUPPORT_EDITED_FROM_HEAD`), while untouched helpers get the staged copy back for the workflow's later steps.

| The numbers that matter | Value |
| --- | --- |
| New repo variable | `SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED` (default `true`; `false` restores judge-time filing) |
| Sites that file deferred follow-ups | every `final_merge_status = "merged"` write in `scripts/orchestrate_poll_process.sh` (5) |
| New helper | `scripts/implement_staged_support_workspace.sh restore\|reinstall`, wired at 4 points in `.github/workflows/implement.yml` |
| Issues this reproduces | #4090, #4091 (`ai:blocked`), #4113 (`ai:needs-human`, run 35072286584) |

What this means for operators: a security-pass project that reaches the exhaustion judge no longer leaves `ai:blocked` advisory issues behind while its integration branch is unmerged, and a fix issue on this repository whose edit lands in a staged helper no longer stops for a human merge. Existing pending advisories are filed the first time the project's final merge is recorded after this ships. #4113 itself needs one manual redispatch (remove `ai:needs-human`, restore `ai:awaiting-approval`) once this is on `main`; the retry then commits the editor's helper edit instead of conflicting.

### For contributors

Waiver rows gain `followup_pending`, `audited_head_sha`, and `finding` while deferred; `create_security_pass_advisory_followup` takes an optional sixth `merged_pr` argument and clears the pending fields when it records the issue. `scripts/implement_commit_changes.sh` reads `STAGED_SUPPORT_EDITOR_HEAD_LEDGER` (`${RUNTIME_DIR}/staged_support_editor_head.txt`) and its summary line adds `edited_from_head=<n>`. Consumer repos are unaffected by the implement change (no ledger means the helper is a no-op).

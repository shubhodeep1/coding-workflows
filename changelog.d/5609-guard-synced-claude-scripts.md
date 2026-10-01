<!-- changelog: security -->
- **A Claude twin sync PR that changes `.claude/scripts/**` now waits for the repository owner, like a hook or settings change.** `.claude/settings.json` lets every Claude session run those scripts with its GitHub access and no permission prompt, yet `claude-twin-sync.yml` used to merge a script sync PR on its own once CI was green.

`scripts/claude_twin_sync.py` now treats `.claude/scripts/**` as a guard path. A sync PR that copies a script twin is labelled `ai:claude-sync-approval`, alerted on Telegram, and never approved or merged by the workflow; `merge-check` refuses it. The CI step "Claude twin sync state (CLAUDE.md §28.C)" applies the guard rules to scripts too: on `main` a script may change only to the twin already on the base commit, only from a same-repository `claude/claude-twin-sync-*` PR, and is never deleted (issue #5246); on `stable` it must equal `main`'s copy (issue #5247). Consumer repos get scripts the same way they get hooks and settings (issue #5607): the "Sync .claude/ assets from upstream" step of `update_workflows.yml` and `/seed-repo` copy `.claude/scripts/**` from the `stable` commit's `.claude/` tree, not its `workflow-templates/.claude/` twin. Commands (`.claude/commands/**`) still sync from the twin and keep the automatic merge; a sync PR that lists a conflict still waits for the owner, as before.

| The numbers that matter | Value |
| --- | --- |
| Guard paths | `.claude/hooks/**`, `.claude/scripts/**`, `.claude/settings.json`, `.claude/settings.local.json` |
| Scripts sessions run with no prompt | 7 (`check_in_status.py`, `stale_routines.py`, `claude_fix_claim.py`, `security_pass_skip.py`, `dispatch_workflow.py`, `edit_comment.py`, `permission_prompts.py`) |
| Issue | #5609 (security audit finding `auto-synced-scripts-retain-session-privileges`) |

What this means for operators: a change to a session script still starts as an edit to `workflow-templates/.claude/scripts/`, but its sync PR now needs your review and merge. A PR that edits a `.claude/scripts/` file directly turns CI red, the same as a direct hook edit.

### For contributors

The twin sync's code change is `GUARD_PATH_PREFIXES = ("hooks/", "scripts/")`; every rule keyed on `is_guard_path` follows. The consumer sync is separate shell code: `scripts/*` is added to the three guard `case` patterns in `.github/workflows/update_workflows.yml` (`guard_source_is_reviewed()` and both sync loops), which `tests/test_update_workflows_guardrails.py::test_guard_pattern_matches_claude_twin_sync_guard_paths` keeps in parity with `GUARD_PATH_PREFIXES` / `GUARD_PATH_FILES`, so a future guard change updates both. The `ai:claude-sync-approval` description now reads "hook/script/settings changes" in `.github/ai/label_contract.v1.json`, `scripts/label_helpers.sh`, and `APPROVAL_LABEL_DESCRIPTION`. The posted `claude-twin-sync/owner-approval` description for a non-guard PR stays `No hook or settings change`.

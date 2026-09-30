# edit_comment.py: read input files only from the calling session's own scratchpad

Source issue: shubhodeep1/coding-workflows#5700 (https://github.com/shubhodeep1/coding-workflows/issues/5700)
Base branch: claude/implement-plan-issue-5452-edit-comment-scratchpad-only
Security pass: skip (ai:security: automation-produced issue)

## Summary

`.claude/scripts/edit_comment.py` now reads `--body-file` and `--replacements` only from a Claude Code session scratchpad (#5452), but it accepts **any** session's scratchpad. On a shared temp filesystem, a prompted unattended agent could pass a file from another session's (or another user's) scratchpad to the allowlisted command and publish it as a comment. This plan binds the check to the caller's own scratchpad, using the session id the harness exports (`CLAUDE_CODE_SESSION_ID`) and the process uid, and fails closed when that identity cannot be verified.

## Context

- #5700, filed by `.github/workflows/security-audit.yml` against the #5452 project branch (`Integration branch:` line), finding `comment-helper-cross-session-file-read`, `A01:2021-Broken Access Control`, medium, confidence 8/10, at `.claude/scripts/edit_comment.py:99`. Recommendation: "Bind accepted paths to the caller's scratchpad using trusted session metadata; fail closed when that identity cannot be verified."
- On the base branch, `is_scratchpad_path` (`edit_comment.py:87-101`) accepts `<temp root>/<claude-*>/<project>/<session>/scratchpad/<file>` for any `claude-*` directory and any `<session>`. The #5452 project deliberately chose that in its AD-7 ("another same-user session's scratchpad is not a privilege boundary"). The audit disagrees, and this issue is the follow-up its security pass waits on.
- Claude Code exports `CLAUDE_CODE_SESSION_ID` to every Bash command, and it equals the `<session>` component of the scratchpad path (this session: `CLAUDE_CODE_SESSION_ID=884f673e-…`, scratchpad `/tmp/claude-0/-home-user-coding-workflows/884f673e-…/scratchpad`, checked 2026-09-30). The first component is `claude-<uid>` (`/tmp/claude-0` is owned by uid 0, mode `0700`).
- The allow rule is `Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/edit_comment.py *)`. A command that sets `CLAUDE_CODE_SESSION_ID=…` in front of it does not match the rule and goes to the Auto-mode classifier, so inside the allowlisted path the variable is the harness's value.
- Every documented caller writes the file into its own scratchpad with the Write tool (`/implement-plan-claude` Comment helper, CLAUDE.md §23.I), so the documented flow is unaffected.

## Goals

- G1: `read_input_file` reads a file only when its resolved path is `<temp root>/claude-<uid>/<project>/<CLAUDE_CODE_SESSION_ID>/scratchpad/<file>`, with `<uid>` the calling process's `os.getuid()`, and the open file is owned by that uid (plus every #5452 check: regular file, one hard link, size cap).
- G2: Fail closed: when `CLAUDE_CODE_SESSION_ID` is unset, empty, or not a single safe path component, or `os.getuid` is unavailable, every `--body-file` / `--replacements` path exits 1 with a JSON `error` before any API call.
- G3: Another session's scratchpad, another uid's `claude-<uid>` directory, and a file owned by another uid are rejected with exit 1, no API call, and none of the file's content in the output.
- G4: The documented flow (a file the session wrote into its own scratchpad) works unchanged: same output, exit codes, one read and one PATCH.
- G5: The root and `workflow-templates/` copies stay byte-identical; CLAUDE.md §23.I, `agents.md`, and the `/implement-plan-claude` Comment helper describe the narrower rule; one `changelog.d/` fragment.

## Non-goals

- Changing `is_scratchpad_path`'s signature or behaviour (§6; AD-4). It stays the layout check; the new binding is a separate function.
- Binding the `<project>` component (AD-5).
- An environment-variable override or allow-list widening (#5452 AD-5 stands).
- Editing the #5452 project's log or its AD-7 entry (AD-3).
- Changing `.claude/settings.json` allow rules or the `gh api` guard.

## Constraints

- §1: security first; the change only narrows what the helper reads.
- §5: one added check in `read_input_file`, its tests, and the docs that describe the helper.
- §6: no identifier renamed or removed; flags, exit codes, and output keys unchanged. New names `SESSION_ID_ENV_VAR`, `SESSION_ID_RE`, `_caller_scratchpad_identity`, and `is_own_scratchpad_path` do not clash with anything in the module, in `check_in_status` (imported as a module), or in the tests (`git grep`, 2026-09-30).
- §4: `CLAUDE_CODE_SESSION_ID` is read, not introduced; it has no default on purpose, because a default would defeat the fail-closed rule the finding requires (AD-1).
- §9: tabs in Python.
- §14 / §20: the twin ships to consumers on the next `@stable` sync; one `security` fragment.
- §15: no new API call; the check runs before the one comment read.
- §28.C: the phase edits `.claude/scripts/edit_comment.py` and `.claude/commands/implement-plan-claude.md`, which are protected paths. Twin-first while #4785 is open: only the `workflow-templates/.claude/` twins are edited, and the `.claude/` copy lands as a `[claude-twin-sync]` commit.

## Approach

Add `_caller_scratchpad_identity()`, which returns `(f"claude-{os.getuid()}", session_id)` when `os.getuid` exists and `CLAUDE_CODE_SESSION_ID` matches `SESSION_ID_RE` (one path component: starts with a letter or digit, then letters, digits, `.`, `_`, `-`, at most 128 characters), else `None`. Add `is_own_scratchpad_path(resolved, roots, identity)`, which is `is_scratchpad_path(resolved, roots)` plus `parts[0] == identity[0]` and `parts[2] == identity[1]` for the root the path sits under. In `read_input_file`, after the strict resolve: `None` identity → `ValueError` naming `CLAUDE_CODE_SESSION_ID` and the MCP fallback; a path that is not the caller's own scratchpad → `ValueError` naming the expected location; and after `fstat`, `info.st_uid != os.getuid()` → `ValueError`. `main` already maps `ValueError` to exit 1 before `edit_comment` runs, so no API call is made.

Alternatives: bind the session id only (AD-1 B, rejected: leaves another user's `claude-<uid>` tree on a shared `/tmp`); derive the identity from the parent process or the harness socket (AD-1 C, rejected: not a documented contract and harder to verify than the exported variable).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes exactly one phase.

1. **Phase 1 — bind edit_comment.py input files to the caller's own session scratchpad.** protected paths: `.claude/scripts/edit_comment.py`, `.claude/commands/implement-plan-claude.md`.
   - Files: see Files & Modules.
   - Done: `tests/test_edit_comment.py` passes with the new cases (loading the twin); a file in another session's scratchpad, under another uid's `claude-<uid>`, or owned by another uid exits 1 with no API call and no content in the output; an unset or malformed `CLAUDE_CODE_SESSION_ID` exits 1; a file in the caller's own scratchpad still edits the comment; CLAUDE.md §23.I, `agents.md`, and the Comment helper describe the rule; the changelog fragment exists. `test_template_parity` and the command-twin parity checks go green with the `[claude-twin-sync]` copy.
   - Rollback: revert the phase PR; the helper accepts any session's scratchpad again (the #5452 behaviour).

## Implementation Steps

Phase 1:
1. `workflow-templates/.claude/scripts/edit_comment.py`: add `SESSION_ID_ENV_VAR`, `SESSION_ID_RE`, `_caller_scratchpad_identity`, `is_own_scratchpad_path`; call them from `read_input_file` before the open, and add the `st_uid` check after `fstat`; update the module docstring and the rejection messages (keep the phrase `session scratchpad`, which callers and tests match).
2. `tests/test_edit_comment.py`: the `scratchpad` fixture builds `claude-<os.getuid()>/…/<session>/scratchpad` and sets `CLAUDE_CODE_SESSION_ID`; add tests for another session's scratchpad, another uid's directory, a file owned by another uid, an unset / empty / traversal-shaped session id, and a unit table for `is_own_scratchpad_path`.
3. `workflow-templates/.claude/commands/implement-plan-claude.md` Comment helper, CLAUDE.md §23.I helper row, and `agents.md` helper bullet: "the session scratchpad" becomes "this session's own scratchpad", naming `CLAUDE_CODE_SESSION_ID` and the fail-closed rule.
4. `changelog.d/5700-edit-comment-own-session-scratchpad.md` [new] (`security`).
5. After the phase PR opens: the `[claude-twin-sync]` copy of both twins into `.claude/` (twin-first, §28.C).

## Files & Modules

- `workflow-templates/.claude/scripts/edit_comment.py` (then `.claude/scripts/edit_comment.py` by twin sync)
- `workflow-templates/.claude/commands/implement-plan-claude.md` (then `.claude/commands/implement-plan-claude.md` by twin sync)
- `tests/test_edit_comment.py`
- `CLAUDE.md` (§23.I helper table row; `workflow-templates/CLAUDE.md` is the same file)
- `agents.md`
- `changelog.d/5700-edit-comment-own-session-scratchpad.md` [new]

## Tests

- Unit: `tests/test_edit_comment.py`: rejection cases (other session, other uid directory, other file owner, missing / empty / `..` / `a/b` session id) each assert exit 1, no comment read, no PATCH, and no file content in the output; the existing scratchpad flow cases pass with the fixture's session id; `is_own_scratchpad_path` table; `test_template_parity`.
- Suites run before the push: `tests/test_edit_comment.py`, `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`, and the template parity suites that compare `workflow-templates/.claude/` with `.claude/` (red until the twin sync, as in #5452).
- End to end: in this cloud session, a `--dry-run` against a real comment with a file in this session's scratchpad succeeds, and the same call with a file in a fabricated sibling session directory exits 1.

## Risks & Mitigations

- A harness that does not export `CLAUDE_CODE_SESSION_ID` (an older local CLI) makes the helper refuse every file. Mitigation: exit 1 with an error that names the variable and points to `mcp__github__update_issue_comment`; the helper is a convenience, and the MCP tool edits the same comment. ACCEPTED: the finding requires failing closed.
- A resumed session whose system prompt still names an older scratchpad path than its current session id. Mitigation: same fail-closed error, which names the expected path; the caller rewrites the file where it is told. ACCEPTED.
- A platform where the temp directory is not `claude-<uid>`: every file is refused, with the same error and fallback. ACCEPTED.

## Rollout

Ships with the #5452 project: this project's final PR merges into `claude/implement-plan-issue-5452-edit-comment-scratchpad-only`, and that project's final PR (#5464) takes both into `main`. Consumer repos get it with the next `.claude/` `@stable` sync. Rollback: revert this project's merge on the #5452 branch.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which trusted metadata binds the check to the caller? — Picked: A — `CLAUDE_CODE_SESSION_ID` must equal the `<session>` component and `claude-<os.getuid()>` the first component; fail closed when either is unavailable or malformed. Alternatives: B — the session id only; C — derive the identity from the parent process or the harness socket. Why: the harness exports the variable to every Bash call and a prefixed override leaves the allow rule; the uid covers other users on a shared `/tmp`; C has no documented contract. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Also require the file to be owned by the calling uid? — Picked: A — yes, `fstat(...).st_uid == os.getuid()` on the open descriptor. Alternatives: B — no. Why: one line, and it rejects a file another account (root included) placed in the scratchpad. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How to treat #5452's AD-7 (which allowed any session's scratchpad)? — Picked: A — leave that project's log untouched and record in this plan, the PR body, and the changelog fragment that #5700 narrows it. Alternatives: B — edit #5452's log to mark AD-7 changed. Why: that log belongs to the #5452 chain, which is waiting on this issue; §28.E changes come from a human reply. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-30] Change `is_scratchpad_path` or add a new function? — Picked: A — keep `is_scratchpad_path(resolved, roots)` unchanged and add `is_own_scratchpad_path(resolved, roots, identity)`. Alternatives: B — add parameters to `is_scratchpad_path`. Why: §6 keeps an existing identifier's contract; the existing layout tests stay valid. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Bind the `<project>` component too? — Picked: A — no. Alternatives: B — require it to equal the cwd-derived project name. Why: the session id is already unique, and the cwd-to-name mapping (symlinks, subdirectories) is not a stable contract. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Changelog: new fragment or edit #5452's? — Picked: A — a new `changelog.d/5700-edit-comment-own-session-scratchpad.md` (`security`). Alternatives: B — edit `changelog.d/5452-edit-comment-scratchpad-only.md`. Why: §20 is one fragment per PR, and never reuse another PR's file. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: skip (`security_pass_skip.py`: `ai:security: created and labelled by the issue automation`).

## References

- #5700 (this finding), #5452 (the scratchpad-only restriction), PR #5464 (the #5452 final PR), PR #5465 (its phase PR), #4785 (the twin-sync automation).

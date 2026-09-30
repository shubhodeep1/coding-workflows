# Implement-Plan Log — edit_comment.py: read `--body-file` and `--replacements` only from the session scratchpad

- Plan: docs/plans/issue-5452-edit-comment-scratchpad-only-plan.md
- Source issue: shubhodeep1/coding-workflows#5452
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5452-edit-comment-scratchpad-only   Final PR: #5464 draft
- Status: BLOCKED
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5465: twin sync 3 (review round 1 fix on head 6a48797)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01EKem5RE2k4bXrwGD5FZWRy (idle, reused on resume)   safety net none (blocked)   hand-back none
- Last updated: 2026-09-30
- Last note: twin sync 2 landed as 6a48797 (Status was IN_PROGRESS from that push until this round). The review of 6a48797 (its round 1) found 1 valid defect: `read_input_file` bounded and checked the read in decoded characters, so a file that grew after `fstat` with multibyte UTF-8 could be read past `MAX_INPUT_FILE_BYTES`; fixed in the twin (binary bounded read, then the same UTF-8 / universal-newline decoding). Rejected: the `fd = -1` leak (no leak; the rewrite moves the assignment out of the `with` anyway). Task gap (stale log) fixed here. BLOCKED again on twin sync 3 (hold claim + blocker on #5452).

## Phases
1. [ ] Phase 1 — restrict edit_comment.py input files to the session scratchpad   — PR #5465 open (hold: twin sync 3 after the review of 6a48797); review rounds: 2; interventions: 0; protected paths: `.claude/scripts/edit_comment.py`, `.claude/commands/implement-plan-claude.md`
   - [x] twin `workflow-templates/.claude/scripts/edit_comment.py`: `read_input_file` / `is_scratchpad_path` / `_temp_roots`, used by `load_replacements` and `--body-file`
   - [x] `tests/test_edit_comment.py`: load the twin; scratchpad fixture; rejection and unit cases
   - [x] CLAUDE.md §23.I, `agents.md`, twin `implement-plan-claude.md` Comment helper
   - [x] `changelog.d/5452-edit-comment-scratchpad-only.md` (`security`)
   - [x] `[claude-twin-sync]` copy into `.claude/` (after the phase PR opens) — bfb3389, 2026-09-30
   - [x] review round 1 fix: `[claude-twin-sync]` copy of `workflow-templates/.claude/scripts/edit_comment.py` into `.claude/` — 6a48797, 2026-09-30
   - [ ] review of 6a48797 (workflow round 1) fix: `[claude-twin-sync]` copy of `workflow-templates/.claude/scripts/edit_comment.py` into `.claude/`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which files may `--body-file` and `--replacements` read? — Picked: A — only files resolving under a Claude Code session scratchpad (`<temp root>/claude-*/…/scratchpad/`). Alternatives: B — the scratchpad plus the git working tree; C — the whole temp directory. Why: the documented flow already uses the scratchpad; the working tree can hold ignored secret files (`.env`), and `/tmp` holds session MCP configs and logs. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] How strictly is the file itself checked? — Picked: A — resolve symlinks first, open the resolved path with `O_NOFOLLOW`, and require a regular file with one hard link on the open descriptor. Alternatives: B — resolve symlinks only. Why: a hard link in the scratchpad to a secret file passes a path check, and a FIFO or device could block or leak; the extra checks cost two lines. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Keep or drop `--body-file`? — Picked: A — keep it, restricted like `--replacements`, and point whole-body rewrites at `mcp__github__update_issue_comment` in the docs. Alternatives: B — deprecate it with a warning; C — remove it. Why: §6 forbids removing a CLI flag without the ask flow, and restricted it no longer leaks. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Which copy do the behaviour tests load while `.claude/` waits for the twin sync? — Picked: A — the `workflow-templates/.claude/scripts/` twin, with `test_template_parity` still comparing both copies. Alternatives: B — keep loading `.claude/scripts/edit_comment.py`. Why: the twin-first rule (CLAUDE.md §28.C interim) says tests read the twin so they pass before the sync; after it both copies are identical. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Add an environment variable to widen the allowed roots? — Picked: A — no. Alternatives: B — `EDIT_COMMENT_ALLOWED_DIRS`. Why: an override would reopen the path for any command that sets it, and the §23.H guard deliberately has no escape hatch either. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Exit code for a rejected path? — Picked: A — 1 (invalid argument), before any API call, with a JSON `error`. Alternatives: B — 2 (call failed). Why: the documented contract uses 1 for an invalid argument and 2 for a failed call; nothing was called. Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1/1 — review round 1, 2026-09-30] Bind the scratchpad check to this session's own id (`CLAUDE_CODE_SESSION_ID`), or only to the documented layout? — Picked: A — only the layout `<temp root>/claude-*/<project>/<session>/scratchpad/<file>` (scratchpad the fourth component), any session. Alternatives: B — also require the session component to equal `CLAUDE_CODE_SESSION_ID` when it is set. Why: AD-1 already allows any session's scratchpad; another same-user session's scratchpad is not a privilege boundary, and an env-var binding fails closed after a resume changes the session id while the prompt's scratchpad path stays. Applied in: PR #5465. Status: pending review
- AD-8 [phase 1/1 — review round 1, 2026-09-30] Size limit for `--body-file` / `--replacements` files? — Picked: A — `MAX_INPUT_FILE_BYTES = 16 * MAX_BODY_CHARS` (1,048,576 bytes), checked with `fstat` before the read and enforced again by a bounded read. Alternatives: B — `4 * MAX_BODY_CHARS` (the UTF-8 worst case for a body only). Why: a body over 65,536 characters is rejected later anyway; 16x leaves room for JSON escaping in a replacements file and still keeps a huge file out of memory. Applied in: PR #5465. Status: pending review

## Lessons
- [source:plan-deviation] A helper allowlisted in `.claude/settings.json` that reads a caller-supplied file path must confine it (resolve symlinks, reject hard links and non-regular files) before the read, or it becomes a promptless exfiltration path. (files: .claude/scripts/edit_comment.py, .claude/settings.json)
- [source:intervention] A path allowlist check must pin the exact directory depth it documents (not "a `scratchpad` somewhere below `claude-*`"), and a helper that reads a caller-supplied file must bound the read by size before loading it, not only validate the parsed result. (files: .claude/scripts/edit_comment.py)
- [source:intervention] A byte cap on a file read must be enforced on bytes: open in binary mode, read at most cap + 1 bytes, then decode; a text-mode `read(n)` counts characters and can consume up to 4n bytes of UTF-8. (files: .claude/scripts/edit_comment.py)

## Notes
- Permission mode: auto (issue mode records it; §28.A).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- Security pass: run (`security_pass_skip.py`: no skip label).
- Twin sync 1 (2026-09-30): `workflow-templates/.claude/scripts/edit_comment.py` (sha256 bff0569b53d3f6b0abbd1078eea898614f8beae44cfad73fe899b7b75e4bc06e) → `.claude/scripts/edit_comment.py`; `workflow-templates/.claude/commands/implement-plan-claude.md` (sha256 c6f55a6fd3abcdad91b90ff9337e4f7d6452fe02fb325fb86eed92732d07b8d8) → `.claude/commands/implement-plan-claude.md`. The owner answered the blocker with A and `/reclarify`; synced as bfb3389; Status back to IN_PROGRESS; `ai:claude-blocked` removed; project branch synced with main (e549756); project checker session_01EKem5RE2k4bXrwGD5FZWRy created; the blocked stage session session_01Lnqgd9mXSVtxnPBWaUdhXQ left open (session_01EbynNNTD3BbZLrdCRgrGPN).
- Review round 1 (2026-09-30, session_01Kc5NbCZmiVs2fMgUzEmeZf): fixed — `is_scratchpad_path` accepted `claude-*/scratchpad/` lookalikes and a `scratchpad` at any depth; `read_input_file` read the whole file before the body-size check. Rejected — the twin-sync task gap (already done as bfb3389). Twin sync 2: `workflow-templates/.claude/scripts/edit_comment.py` (sha256 4c34f080cf3900ba2e2f7223ff39393a2d690ef12217330c9ab9b86066e31e2e) → `.claude/scripts/edit_comment.py`; the owner answered A and `/reclarify`; synced as 6a48797 (2026-09-30).
- Review of 6a48797, workflow round 1 (2026-09-30, session_01WV8TWs4rPoLuzbL9aDyR41): fixed — `read_input_file` bounded and checked the read in decoded characters, not bytes (consensus, 6 reviewers). Rejected — the `fd = -1` handle-leak (1 reviewer, confidence 2: once `os.fdopen` succeeds the file object owns the descriptor and `with` closes it; the rewrite moves the assignment before the `with` anyway). Task gap fixed — this log (twin sync 2 recorded). Twin sync 3 pending: `workflow-templates/.claude/scripts/edit_comment.py` → `.claude/scripts/edit_comment.py` (sha256 de6413db4b36c33988d3c4b860997bd2a62c5a4891176322d68c618354d0e9f5).

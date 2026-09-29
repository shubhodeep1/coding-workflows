# Issue-mode sessions never end a turn on an in-session question

Source issue: shubhodeep1/coding-workflows#4911 (https://github.com/shubhodeep1/coding-workflows/issues/4911)
Base branch: main
Security pass: run

## Summary

Unattended issue-mode sessions (CLAUDE.md §28.A) have ended their turn on a
Q/A question, or stalled on an `AskUserQuestion` permission prompt, and nobody
read either. This change adds one deterministic hook,
`.claude/hooks/unattended_question_guard.py`, that makes both impossible in
those sessions. It blocks a `Stop` whose final message asks a question, and it
denies `AskUserQuestion`. In both cases the reason restates §28, so the
session auto-decides the question or posts it on the issue. Interactive
sessions are never affected.

## Context

- Issue #4911 (OWNER), plus its OWNER scope-addition comment (2026-09-29 01:57Z).
  - On 2026-09-29 the `#4687` and `#4750` issue sessions ended their turns on
    in-session `Q1` questions. `#4750` also claimed that §23.B routine writes
    were "blocked". Neither session posted on its issue, and a re-sent §28
    rule did not change that.
  - The `#4886` session stalled on a permission prompt for `AskUserQuestion`.
- §28.B says intent and design questions are auto-decided and recorded as
  `AD-<n>`. §28.C says failures and ask-first operations are posted on the
  source issue: a `<!-- ai:claude-blocked:v1 -->` comment, the
  `ai:claude-blocked` label, and one `PushNotification`. Both rules are prose
  only today.
- Hook conventions (`pr_watch_guard.py`, `pr_check_in_reminder.py`,
  `permission_prompt_logger.py`):
  - standalone Python, stdlib only;
  - fail open with a `systemMessage` on an unreadable, invalid, or non-object
    payload, or on an internal error;
  - allow empty input silently;
  - no GitHub API calls;
  - wired in `.claude/settings.json`, with a byte-identical twin under
    `workflow-templates/.claude/`;
  - shipped to consumer repos by the `.claude/` sync;
  - tested by a `tests/test_*.py` file that has its own `ci.yml` step.
- Claude Code's `Stop` hook input carries `session_id`, `transcript_path`,
  `stop_hook_active`, and an optional `last_assistant_message`. Printing
  `{"decision": "block", "reason": …}` makes the session continue with the
  reason as feedback. A `PreToolUse` hook denies a call with
  `hookSpecificOutput.permissionDecision: "deny"` and a
  `permissionDecisionReason`.
- `CLAUDE_CODE_REMOTE_SESSION_ID` (`cse_…`) is set in cloud sessions only. The
  commands already read it (`session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}`).
- The `session-meta` helper the issue gives as an example (#4755) is not on
  `main`. That project is blocked on its own twin sync, so this plan does not
  depend on it (AD-1).
- **Interim twin-first rule** (operator #4750 Q40: A, until #4785 lands;
  restated in this issue's Constraints: "use the interim twin-first rule
  (Q40: A)"). This stage edits `.claude/**` only through the
  `workflow-templates/.claude/**` twins. It pushes the phase PR, posts a `hold`
  claim on its head, and stops BLOCKED with a comment listing every file to
  copy. Hooks and `settings.json` need the operator's approval window
  (Q62/Q64, Q67).

## Goals

- **Marker.** `python3 .claude/hooks/unattended_question_guard.py mark --repo
  <owner>/<repo> --issue <N>` writes
  `~/.claude/unattended-issue-mode/<CLAUDE_CODE_REMOTE_SESSION_ID>.json`.
  - Without `CLAUDE_CODE_REMOTE_SESSION_ID` it writes nothing, prints
    `{"marked": false, …}`, and exits 0.
  - `/implement-issue-claude` step 0 runs it. So does every
    `/implement-plan-claude` session whose plan carries a `Source issue:` line,
    from step 1 on.
- **Stop.** In a marked session, the hook blocks a `Stop` when both hold:
  - the final assistant message contains a §2-format question or asks for
    permissions;
  - no tool call in the current turn posted an `ai:claude-blocked:v1`
    comment.

  The block reason restates §28.B and §28.C and lists the self-serve writes.
  - At most 2 blocks per session.
  - The third such stop is allowed. The hook then emits a structured
    `systemMessage` and appends one JSON line to a local log.
- **AskUserQuestion.** In a marked session the hook denies every
  `AskUserQuestion` call with the same §28 reason.
- **Interactive sessions.** A session without `CLAUDE_CODE_REMOTE_SESSION_ID`,
  or without a matching marker, is never blocked or denied, and the hook
  prints nothing for it.
- **Fail open.** An unreadable, invalid, or non-object payload, an unreadable
  marker or state file, or an internal error allows the call with a
  `systemMessage`. Empty input is allowed silently. The hook makes no GitHub
  API calls.
- **Tests** cover the four cases the issue lists, both `AskUserQuestion` paths,
  fail-open, wiring, and parity. CI runs them in their own step. The docs and
  a changelog fragment are updated.

## Non-goals

- No change to checker sessions, `/fix-claude-pr` sessions, the pickup, or any
  command other than the two issue-mode commands (AD-3).
- No GitHub read or write from the hook, including verifying the blocked
  comment against the issue.
- No change to the §28 rules themselves, only their enforcement.
- No `.claude/**` edit by this stage (interim twin-first rule).
- No dependency on #4755's `permission_prompts.py session-meta`.

## Constraints

- **§1.**
  - The hook only ever blocks or denies. It never widens a permission, and
    its reason text recommends only §23.B routine writes and command-approved
    dispatches.
  - Transcript and message text are data. They are only pattern-matched,
    never executed.
- **§5.** One new hook file, which also carries the `mark` subcommand
  (AD-2). The two command files gain one step each. There is no new script.
- **§6.**
  - New identifiers checked unique with `git grep` (no hits):
    `unattended_question_guard`, `unattended-issue-mode`.
  - No existing identifier, marker, or hook entry changes.
  - `<!-- ai:claude-blocked:v1 -->` is only read.
- **§9.** Tabs in Python and Markdown code; `settings.json` keeps its 2-space
  JSON layout.
- **§15.** No API calls.
- **§18.** Nothing manual. The hook runs on every `Stop` and `AskUserQuestion`
  event, and the marker is written by the commands' own preflight.
- **§20.** One fragment, `changelog.d/4911-unattended-question-guard.md`.
- **§21, §25.** Nothing new.
- **Twin-first (Q40: A).**
  - `.claude/hooks/unattended_question_guard.py` [new],
    `.claude/settings.json`, `.claude/commands/implement-issue-claude.md`,
    `.claude/commands/implement-plan-claude.md`, and
    `.claude/commands/seed-repo.md` change only through their twins.
  - Until the sync, the parity tests fail, along with the tests that read
    `.claude/`.

## Approach

**One hook file, three entry points.**
- **`mark` subcommand.** Writes the marker `{"version": 1, "session":
  "<cse_…>", "repo": "<owner>/<repo>", "issue": <N>, "marked_at": "<UTC>"}`.
- **`Stop` payload.**
  1. Is this session marked? It is only when the env id is set, the marker
     exists, and the marker's `session` equals the env id.
  2. Read the final assistant text: `last_assistant_message`, or, when that is
     absent, the trailing assistant text blocks of `transcript_path`.
  3. Detect a question or a permission ask.
  4. Scan `transcript_path` from the current turn's start for a tool call
     whose input contains `<!-- ai:claude-blocked:v1 -->`, with a result that
     is not an error.
  5. Read and increment the per-session block count in `<id>.state.json`.
  6. Block, or at the cap allow with a structured line.
- **`PreToolUse` payload with `tool_name: AskUserQuestion`.** In a marked
  session, deny.

**Detection** (AD-5):
- **Question:** a line matching `Q<n>:` (optionally bold or quoted) plus at
  least two lettered-choice lines (`- **A** — …`, `A) …`, `A: …`), or the
  inline form `Q<n>: A/B`.
- **Permission ask:** a fixed phrase list:
  - "awaiting/waiting for (your) permission(s)/approval";
  - "needs/requires GitHub writes / permission(s) / approval";
  - "grant/approve the permission/prompt";
  - "needs a fresh/new/watched session".
- The report line `Permission prompts: …` never matches.

**Current turn.** It starts after the last transcript `user` entry that is a
genuine prompt (string content, or content with no `tool_result` block).
Hook-feedback entries count as prompts too. That is safe: a blocked comment
posted after the feedback is still inside the window.

**Alternatives rejected:**
- a separate marker script, which means two files to sync and two formats;
- a `PostToolUse` recorder for blocked comments, which means more wiring for
  the same signal;
- reading the session title, which is not visible to hooks.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
hook, its wiring, the marker step, and their tests only work together.

1. **Phase 1 — unattended question guard.**
   - **Files:** listed below.
   - **Done when:**
     - the new test file passes with the twins synced into `.claude/`;
     - every existing suite that reads `.claude/settings.json`, the command
       files, or the hooks still passes;
     - the twins are byte-identical after the sync.
   - **Rollback:** revert the PR. With the `settings.json` entries removed the
     hook never runs, and a leftover marker file is inert.

## Implementation Steps

1. `workflow-templates/.claude/hooks/unattended_question_guard.py` [new]:
   - the `mark` CLI;
   - the `Stop` evaluation;
   - the `PreToolUse` `AskUserQuestion` deny;
   - fail-open `main`.
2. `workflow-templates/.claude/settings.json`:
   - a `PreToolUse` entry, matcher `AskUserQuestion`;
   - a `Stop` entry;
   - two allow rules for `python3 .claude/hooks/unattended_question_guard.py
     mark *`, with and without `PYTHONDONTWRITEBYTECODE=1`.
3. `workflow-templates/.claude/commands/implement-issue-claude.md` step 0, and
   `workflow-templates/.claude/commands/implement-plan-claude.md` step 1 plus
   an Issue Mode bullet: run `mark`.
4. `workflow-templates/.claude/commands/seed-repo.md`: list the new hook.
5. `CLAUDE.md`: new §28.G "Enforcement" (the file is shared with
   `workflow-templates/CLAUDE.md` through a symlink).
6. `README.md` and `agents.md`: hook docs.
7. `.github/workflows/ci.yml`: a new test step.
8. `tests/test_unattended_question_guard.py` [new].
9. `changelog.d/4911-unattended-question-guard.md` [new].

## Files & Modules

- `workflow-templates/.claude/hooks/unattended_question_guard.py` [new]
  (twin of `.claude/hooks/unattended_question_guard.py` [new, via sync])
- `workflow-templates/.claude/settings.json` (twin of `.claude/settings.json`)
- `workflow-templates/.claude/commands/implement-issue-claude.md` (twin)
- `workflow-templates/.claude/commands/implement-plan-claude.md` (twin)
- `workflow-templates/.claude/commands/seed-repo.md` (twin)
- `CLAUDE.md`
- `README.md`, `agents.md`
- `.github/workflows/ci.yml`
- `tests/test_unattended_question_guard.py` [new]
- `changelog.d/4911-unattended-question-guard.md` [new]

## Tests

`tests/test_unattended_question_guard.py`:
- **Interactive sessions are never blocked.** No env id, no marker, or a
  marker for another session: the hook allows and prints nothing, for both
  `Stop` and `AskUserQuestion`.
- **Blocking.** A marked session ending on `Q1: … A/B/C` is blocked. So is one
  ending on "awaiting permissions or fresh session". A normal final report,
  including its `Permission prompts: none` line, is allowed.
- **Posted comment.** A session whose current turn posted an
  `ai:claude-blocked:v1` comment is allowed to stop. The post counts through
  MCP `add_issue_comment` or a Bash command. A post from an earlier turn, or
  one whose result is an error, does not count.
- **Cap.** Stops 1 and 2 are blocked. Stop 3 is allowed with the structured
  `systemMessage` and a log line.
- **AskUserQuestion.** Denied in a marked session, allowed without a marker.
- **Fail-open.** Malformed payloads and an unreadable marker.
- **`mark`.** Writes the marker, and is a no-op without the env id.
- **Wiring.** `settings.json` wiring in both copies; `last_assistant_message`
  versus the transcript fallback.
- **Parity and prose.** Twin parity, the CLAUDE.md §28.G prose, the command
  steps, `seed-repo.md`, and the CI step.

Also run:
- `tests/test_implement_issue_claude_command.py`;
- `tests/test_pr_watch_guard.py`, `tests/test_gh_api_write_guard.py`,
  `tests/test_pr_check_in_reminder.py`, `tests/test_permission_prompts.py`
  (all read `settings.json`);
- `tests/test_update_workflows_guardrails.py`;
- `tests/test_claude_md_section_numbers.py`;
- `tests/test_changelog_fragment_contract.py`;
- `tests/test_workflow_file_size_limit.py`.

## Risks

- **False positive on a final report that restates a question.** The block
  reason says to drop the Q/A block when the question was already
  auto-decided. The cap bounds the cost at two extra turns.
- **Transcript format drift.** A parse failure means "no blocked comment
  found", so the question is blocked (within the cap). An unreadable
  transcript is also treated as not found. The primary text source is
  `last_assistant_message`.
- **Claude Code's own Stop-hook block limit** may cut in before the hook's
  cap. The session then stops, the same as at our cap.
- **A marked session later used interactively** (a human answering in the
  final LIVE session) cannot use `AskUserQuestion`, and gets up to two blocks
  on a Q/A ending. Under §28.A those sessions are in scope anyway.

## Rollout

- Ships through the operator's twin sync (Q40, Q62/Q64). The hook goes live
  in coding-workflows when the phase PR merges into the project branch and
  the final PR merges into `main`.
- Consumer repos get it on the next `.claude/` sync.
- Only sessions that ran `mark` are affected, so there is nothing to
  configure.

## Auto-decisions

- AD-1 [plan, 2026-09-29] How does the hook know a session is an unattended issue-mode session? — Picked: A — a local marker `~/.claude/unattended-issue-mode/<CLAUDE_CODE_REMOTE_SESSION_ID>.json`, written by the hook file's own `mark` subcommand from the issue-mode preflight; the hook requires the env id and a marker for that id. Alternatives: B — piggyback on #4755's `permission_prompts.py session-meta`; C — infer from the session title or transcript. Why: #4755 is not on `main` (blocked on its own twin sync), and titles are not visible to hooks; the issue asks for "a marker … plus `CLAUDE_CODE_REMOTE_SESSION_ID`". Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Where does the `mark` writer live? — Picked: A — in the hook file itself (`unattended_question_guard.py mark …`). Alternatives: B — a new `.claude/scripts/unattended_issue_mode.py`. Why: one marker format in one module, one file for the operator to review and sync (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which sessions write the marker? — Picked: A — `/implement-issue-claude` step 0 and every `/implement-plan-claude` session whose plan carries `Source issue:` (step 1, stage sessions included). Alternatives: B — `/implement-issue-claude` only; C — also checkers and `/fix-claude-pr`. Why: that is the §28.A issue-mode scope the issue names; checkers and fixers are outside it (§5). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How does the hook tell that the blocked comment was posted, with no API calls? — Picked: A — scan `transcript_path` from the start of the current turn for a tool call whose input contains `<!-- ai:claude-blocked:v1 -->` and whose result is not an error. Alternatives: B — a separate "posted" helper that updates the marker; C — a `PostToolUse` recorder hook. Why: nothing extra for the session to remember and no extra wiring; the check stays local. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] What counts as "a question" or "a permission ask"? — Picked: A — a `Q<n>:` line with at least two lettered-choice lines, or the inline `Q<n>: A/B` form; permission asks by a fixed phrase list (awaiting permission/approval, needs GitHub writes/permissions/approval, grant/approve the permission/prompt, needs a fresh/new/watched session). Alternatives: B — any line ending in `?`. Why: it matches the §2 format and both observed failures without blocking ordinary reports (the `Permission prompts:` report line never matches). Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] How is the cap counted, and what does the third stop log? — Picked: A — at most 2 `Stop` blocks per session, counted in `<id>.state.json` beside the marker; the third is allowed with a `systemMessage` starting `unattended-question-guard: cap reached` and one JSON line appended to `~/.claude/unattended-issue-mode/stop-guard.jsonl`. Alternatives: B — 2 blocks per distinct question. Why: the issue says "at most twice per session"; a `systemMessage` shows in the session view the poller scans, and the hook posts nothing. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Is the `AskUserQuestion` denial capped? — Picked: A — no, always deny in a marked session. Alternatives: B — the same cap of 2. Why: allowing the call stalls the session at a prompt nobody answers; a denial cannot loop forever, because the turn ends and the `Stop` cap applies. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] How is `AskUserQuestion` denied? — Picked: A — `PreToolUse` JSON output, `permissionDecision: "deny"` with a `permissionDecisionReason`. Alternatives: B — exit 2 with the reason on stderr. Why: an explicit deny, which is what the issue asks for. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: run (`security_pass_skip.py` → `{"skip": false, "label": null, "reason": "no skip label"}`).
- Protected-path approval: phase 1 — interim twin-first rule (operator #4750 Q40: A, restated in the #4911 body: "use the interim twin-first rule (Q40: A). The operator approves the hook sync once") (2026-09-29).

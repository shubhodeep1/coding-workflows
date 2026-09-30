# Unattended question guard counts only a verified blocked comment on the marker's issue

Source issue: shubhodeep1/coding-workflows#5082 (https://github.com/shubhodeep1/coding-workflows/issues/5082)
Base branch: claude/implement-plan-issue-4911-unattended-question-guard
Security pass: skip (ai:security: automation-produced issue)

## Summary

`blocked_comment_posted()` in `.claude/hooks/unattended_question_guard.py`
accepts **any** successful tool call whose input contains
`<!-- ai:claude-blocked:v1 -->`. A `Bash` `echo` of the marker, or a comment
on a different issue or repository, therefore lets an unattended issue-mode
session end its turn on a question without ever telling its source issue.
This plan makes the hook count only a verified comment posted to the
marker's exact repository and issue, starting with the marker, plus the
`ai:claude-blocked` label write on that same issue, all in the current turn.

## Context

- Security audit follow-up (`STRIDE: Spoofing`, medium, confidence 10/10),
  location `.claude/hooks/unattended_question_guard.py:267`, filed by
  `.github/workflows/security-audit.yml` for project #4911
  (`Refs #3576`, the audit tracker).
- The hook was built by the #4911 project (plan
  `docs/plans/issue-4911-unattended-question-guard-plan.md`, AD-4: "scan
  `transcript_path` … for a tool call whose input contains the marker and
  whose result is not an error"). That project's branch
  `claude/implement-plan-issue-4911-unattended-question-guard` (final PR
  #4964, draft, into `main`) is this issue's `Integration branch:`, so this
  project is built on it and merges into it.
- CLAUDE.md §28.C / §28.G: a §28.C stop in issue mode is "one comment on the
  source issue starting `<!-- ai:claude-blocked:v1 -->` … the
  `ai:claude-blocked` label, and one `PushNotification`". The hook is the
  only enforcement of that rule, so its evidence check must match it.
- Sessions post the comment either with `mcp__github__add_issue_comment` or,
  in sessions without GitHub MCP tools (seen in the #4911 and #5082
  sessions), with `gh api repos/<owner>/<repo>/issues/<N>/comments -f body=…`
  through the agent proxy. Both paths must keep working.

## Goals

- G1. A turn counts as having posted the blocker only when, in the current
  turn, a successful comment-posting tool call targets the marker's
  `repo` (case-insensitive `owner/repo`) and `issue` (exact number), its
  body starts with `<!-- ai:claude-blocked:v1 -->` (after leading
  whitespace), and its result carries that issue's comment URL.
- G2. The same turn also has a successful label write that adds
  `ai:claude-blocked` to that same issue.
- G3. A `Bash` `echo`/`printf`/`cat` of the marker, a comment on another
  issue or repository, a comment whose body only contains the marker later
  in the text, a failed call, an unverifiable result, and a compound shell
  command no longer count.
- G4. The block reason names the exact issue and the accepted forms, so a
  blocked session can post a comment that the hook will accept.
- G5. No GitHub API calls, same fail-open contract, same cap, byte-identical
  twins, same `settings.json` wiring.

## Non-goals

- No change to question detection (`question_kind`), the cap, the marker
  format, `mark`, the `AskUserQuestion` denial, or `settings.json`.
- No read-back of the comment through the GitHub API (§15, and the hook's
  documented "no API calls" contract).
- No other hook, command, or workflow.

## Constraints

- §1: security first. The pick for each open design question favours the
  stricter check that still accepts every legitimate posting path.
- §5: change only the evidence check, its reason text, its tests, and the
  prose that describes it.
- §6: no rename or removal. `blocked_comment_posted`, `BLOCKED_COMMENT_MARKER`,
  `_contains_marker`, and every other existing identifier keep their names.
  `blocked_comment_posted` gains a required `marker` argument (it is called
  in one place only). New identifiers are checked for uniqueness in the module.
- §9: tabs in Python, as in the existing file.
- §15: no API calls, since the evidence comes from the local transcript.
- §20: a `changelog.d/5082-…` fragment (`security`).
- §28.C / §28.G / Q40: `.claude/hooks/unattended_question_guard.py` is a
  protected path, so the phase edits it only through its
  `workflow-templates/.claude/` twin. The live copy is synced by the
  operator-approved twin sync (`docs/operations/master-session.md`, Q40 and
  Q62/Q64).

## Approach

Replace the "marker anywhere in any tool input" check with two
per-tool-call parsers and a turn-level join:

1. `_blocked_comment_target(name, tool_input)`: returns `(owner/repo, N)`
   when the call posts a comment whose body starts with the marker:
   - an MCP tool whose name starts with `mcp__` and ends with
     `__add_issue_comment`, with string `owner`/`repo`, an integer-like
     `issue_number`, and a string `body`;
   - a `Bash` call whose `command` is exactly one simple `gh api` command
     (tokenised with `shlex` and punctuation chars; any `;`, `&`, `|`, `(`,
     `)`, `<`, `>`, `` ` ``, or `$(` means no match). The endpoint must be
     `[/]repos/<owner>/<repo>/issues/<N>/comments`, the method POST
     (explicit `-X`/`--method POST`, or implied by a field flag), no `--input`,
     and a `body=` value from `-f`/`--raw-field`/`-F`/`--field`. An `-F body=@file`
     value is not readable, so it never counts.
2. `_blocked_label_target(name, tool_input)`: returns `(owner/repo, N)` when
   the call adds `ai:claude-blocked`:
   - an MCP tool whose name ends with `__issue_write`, with matching
     `owner`/`repo`/`issue_number` and a `labels` list containing
     `ai:claude-blocked`;
   - a single `gh api` POST to `[/]repos/<owner>/<repo>/issues/<N>/labels`
     with a `labels[]=ai:claude-blocked` field.
3. `_result_text(item)` flattens a `tool_result` content (string, or a list
   of `{"type": "text"}` items) for verification.
4. `blocked_comment_posted(turn, marker)`: collects the candidate calls by
   `tool_use.id`, pairs them with non-error results, and returns true only
   when a comment call to the marker's `repo#issue` succeeded and its result
   text contains `github.com/<owner>/<repo>/issues/<N>#issuecomment-` or
   `/repos/<owner>/<repo>/issues/<N>` (case-insensitive; this is the
   comment's `html_url` / `issue_url` in both the MCP and REST responses),
   **and** a label call to the same `repo#issue` succeeded. `evaluate()`
   passes the marker it already read.
5. `_instructions()` states the exact target (`<repo>#<N>`), that the
   comment body must start with the marker, the accepted forms
   (`mcp__github__add_issue_comment`, or one `gh api
   repos/<repo>/issues/<N>/comments -f body='…'` whose output keeps the
   comment's `html_url`), and that the label write is required too.

Alternatives are recorded under Auto-decisions.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: one issue, one
phase, one PR.

1. **Phase 1: verified blocked-comment evidence.**
   - Scope: the evidence check in the hook twin, its tests, and the prose.
   - Protected paths: `.claude/hooks/unattended_question_guard.py` (edited
     only through `workflow-templates/.claude/hooks/unattended_question_guard.py`;
     the live copy is synced by the Q40 twin sync as a `[claude-twin-sync]`
     commit on the phase branch).
   - Done: every Goal has a passing test in
     `tests/test_unattended_question_guard.py`, with the twin synced
     (`test_template_parity` green). The full file passes, and so do the
     other hook suites that load `.claude/settings.json`.
   - Rollback: revert the phase PR. The hook returns to the #4911 behaviour,
     and nothing else depends on the new helpers.

## Implementation Steps

Phase 1:

1. `workflow-templates/.claude/hooks/unattended_question_guard.py`: add
   `_BLOCKED_LABEL = "ai:claude-blocked"`, the `gh api` tokenizer helper, and
   `_blocked_comment_target`, `_blocked_label_target`, and `_result_text`.
   Rewrite `blocked_comment_posted(turn, marker)` per Approach step 4.
   Update the call in `evaluate()`, `_instructions()`, and the module
   docstring's `Stop` bullet.
2. `tests/test_unattended_question_guard.py`:
   - Update the two "posted" tests. MCP: target the marker's repo and issue,
     return a result with the comment URL, and add an `issue_write` label
     call. Bash: target `repos/shubhodeep1/coding-workflows/issues/4911/comments`
     and add the label call.
   - Add negative tests: a `Bash` `echo` of the marker; a comment on another
     issue; a comment on another repo; a body with the marker not at the
     start; a comment without the label; a label without the comment; the
     label on another issue; a result without the comment URL; a compound
     command (`echo …; gh api …`, `gh api … | head`); `-F body=@file`;
     `--input`.
   - Add positive tests: case-insensitive owner/repo, and a `list`-shaped
     tool result.
   - Assert that the reason names `<repo>#<N>` and the label requirement.
3. `.claude/hooks/unattended_question_guard.py`: byte-identical sync of the
   twin (Q40 twin sync, not written by the stage).
4. `CLAUDE.md` and `workflow-templates/CLAUDE.md` §28.G `Stop` bullet,
   `agents.md` "Unattended question guard" `Stop` bullet, and the `README.md`
   §28.G note: describe the verified-comment-plus-label evidence.
5. `changelog.d/5082-verify-blocked-comment-evidence.md`
   (`<!-- changelog: security -->`).

## Files & Modules

- `workflow-templates/.claude/hooks/unattended_question_guard.py`
- `.claude/hooks/unattended_question_guard.py` (twin sync, Q40)
- `tests/test_unattended_question_guard.py`
- `CLAUDE.md`, `workflow-templates/CLAUDE.md` (§28.G)
- `agents.md`, `README.md`
- `changelog.d/5082-verify-blocked-comment-evidence.md` [new]

## Testing & Verification

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_unattended_question_guard.py`
  (its own `ci.yml` step), in a scratch copy with the twin synced and in the
  synced phase branch.
- The hook-wiring and settings suites that read `.claude/settings.json`
  (`tests/test_gh_api_write_guard.py`, `tests/test_pr_watch_guard.py`,
  `tests/test_pr_check_in_reminder.py`, `tests/test_update_workflows_guardrails.py`).
- `ruff check` on the changed Python.

## Risks

- **A legitimate blocker no longer counts** when a session pipes the `gh api`
  output away (`--jq .id`, `| head`) or its MCP tool returns no URL. Then the
  stop is blocked, at most twice per session. The reason text names the
  accepted forms, so the session can re-post, and the cap still ends the
  turn. The #4911 sessions used both accepted forms.
- **A GitHub MCP server with a different tool name** (not
  `…__add_issue_comment` / `…__issue_write`) would not count. The names are
  the ones `.claude/settings.json` allowlists.
- **Spoofing remains possible** if a session posts a real blocked comment
  and label on its own issue and then ends on an unrelated question. That is
  the §28.C contract, not this finding.

## Rollout

- The change ships on the #4911 project branch through that project's final
  PR #4964, and reaches consumers with the next `@stable` `.claude/` sync.
- The live `.claude/` copy needs the Q40 twin sync with a Q62/Q64 approval
  window (it is a hook change).

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which tool calls can count as posting the blocked comment? — Picked: A — `mcp__*__add_issue_comment` and exactly one simple `gh api` POST to `repos/<repo>/issues/<N>/comments` in `Bash`. Alternatives: B — MCP only; C — any tool whose input names the repo, issue, and marker. Why: both forms are used by real sessions (no GitHub MCP tools in the #4911/#5082 sessions); C keeps the spoofing hole. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How is "verified" established without API calls? — Picked: A — a non-error `tool_result` whose text contains that issue's comment `html_url` or `issue_url`. Alternatives: B — a non-error result only; C — read the comment back through the API. Why: B accepts a result that proves nothing, and C breaks the hook's no-API contract (§15). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Is the `ai:claude-blocked` label write required too? — Picked: A — yes, a successful same-turn label write adding it to the same issue (`mcp__*__issue_write` with `labels`, or `gh api` POST `…/issues/<N>/labels` with `labels[]=ai:claude-blocked`). Alternatives: B — the comment alone. Why: the issue's recommendation and §28.C require both. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How are the repo and issue matched? — Picked: A — `owner/repo` case-insensitive, issue number exact. Alternatives: B — exact-case repo. Why: GitHub treats owner and repo names case-insensitively. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Where must the marker appear in the body? — Picked: A — at the start, after leading whitespace. Alternatives: B — anywhere in the body. Why: §28.C and the commands say the comment starts with it; B accepts a quoting comment. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] What `Bash` command shape counts? — Picked: A — exactly one simple `gh api` command (no `;`, `&`, `|`, subshells, backticks, or redirections), no `--input`, no `-F body=@file`. Alternatives: B — the first `gh api` found anywhere in the command. Why: with B, an `echo` or a second command can wrap the call, and `gh api` output piped away cannot be verified. Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1/1, 2026-09-29] AD-6 rejected the shape real sessions use (`cd <repo>; gh api …comments … && gh api …labels …`, including this project's own blocker). Which chains count? — Picked: A — `gh api` calls joined only by `&&`, after at most one leading `cd <plain path>` followed by `;` or `&&`. Alternatives: B — keep AD-6 as is; C — any `;`/`&&` chain whose segments are `gh api` or `cd`. Why: `&&` stops at the first failure, so a non-error result means every call succeeded, and a plain `cd` can neither post nor print. B would block every real blocker once, and C lets a failed comment hide behind a later success. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: skip (`security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Base branch check: PR #4964 (head = the base branch) is open and draft into `main`, so the base has not moved.

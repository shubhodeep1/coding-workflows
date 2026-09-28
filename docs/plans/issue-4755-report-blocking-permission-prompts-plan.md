# Report the command behind a permission prompt that blocks an unattended session

Source issue: shubhodeep1/coding-workflows#4755 (https://github.com/shubhodeep1/coding-workflows/issues/4755)
Base branch: main
Security pass: run

## Summary

When an unattended Claude Code session hits a permission prompt, report the
sanitized command right away, on the `ai:permission-prompt` issue for its
pattern (or, outside coding-workflows, on the session's own PR or issue),
instead of waiting for the end of a stage the stuck session never reaches.

## Context

- Unattended sessions run in Auto mode: the pickup, the dispatcher, the
  §26.H sweep fixers, and every `/implement-plan-claude` stage. When one of
  them hits a permission prompt, the only outside signal is
  `status_detail: "Waiting on permission: Bash"`. `get_session` does not
  expose the command.
- `.claude/hooks/permission_prompt_logger.py` (wired on `PermissionRequest`
  and `PermissionDenied` in `.claude/settings.json`) appends each prompt to
  `~/.claude/permission-prompts/<session id>.jsonl` inside the session's
  container. `.claude/scripts/permission_prompts.py file` reads that log only
  at step 14 of a stage (CLAUDE.md §23.I). A session stuck on a prompt never
  gets there, so the operator gets no command and the pattern is never filed.
- Incident: `session_01Pqd1mbhdV8mCxriki9onge` (issue #4707) waited from 04:38
  to about 08:00 UTC on 2026-09-28 on a read-only
  `for r in 36242690892 …; do gh …` loop.
- The "master poller" the issue names is the operator's supervising session.
  It does not live in this repository. It learns about a stuck session from
  `list_sessions` / `get_session` (title, `status_detail`), not from any file
  here.
- `CLAUDE_CODE_SESSION_ATTENDED` is `1` even in dispatcher-started sessions
  (checked 2026-09-28 in this issue's own session), so it cannot tell an
  unattended session from an attended one. The hook payload carries
  `permission_mode`; cloud sessions carry `CLAUDE_CODE_REMOTE_SESSION_ID`
  (the `cse_…` id, which `/fix-claude-pr` already uses as its claim label).
  No hook-visible source carries the session title.

## Goals

- G1. A `PermissionRequest` in an unattended cloud session reaches GitHub
  within seconds of the prompt appearing, with no later step in the stuck
  session.
- G2. The report holds the event, the tool, the command sanitized by the
  existing `record_example` (2,000 characters, heredoc bodies removed,
  token-like strings masked), the session id with its claude.ai link, the
  session title when it was recorded, and the §23.I pattern signature.
- G3. In coding-workflows the report lands on the `ai:permission-prompt`
  issue whose `<!-- ai:permission-prompt:v1 sig=<sig> -->` marker matches,
  open or closed, or opens one labelled `ai:permission-prompt` and
  `ai:claude`. In any other repository it goes to the session's own open PR
  or tracked issue, and to nothing when there is none.
- G4. The operator's poller can get the sanitized command and the issue link
  for a stuck session in at most two REST reads through a new read-only
  `permission_prompts.py lookup --session <id>`.
- G5. Each signature is reported at most once per session. A later
  `permission_prompts.py file` in the same session does not file a signature
  that was already reported this way.
- G6. The prompt is never blocked, delayed, or changed. The hook still
  prints nothing, and every reporting error is swallowed.

## Non-goals

- Pre-approving any command shape (for example, read-only `gh` loops). This
  issue only reports.
- Reporting `PermissionDenied` events right away. A denial does not stop the
  session, which carries on and files it at step 14 as it does today.
- Changing the poller's own prompt. It lives outside this repository (see
  AD-5 and Rollout).
- Recording titles in commands other than `/implement-plan-claude` and
  `/implement-issue-claude` (AD-4).

## Constraints

- §23.E and the issue's token-hygiene constraint: the command text is
  untrusted. Reuse `redact` / `strip_heredocs` / `record_example` unchanged,
  and apply `redact` to the title too. The only environment value that
  reaches GitHub is the remote session id, which the issue requires in the
  report and which claims already post (`by=<session id>`, CLAUDE.md §26.H).
- §23.I: filing happens only when the local checkout is `FILING_REPO`, and
  the logger stays non-deciding.
- §15: see the API budget under Approach. Zero calls when the session is not
  unattended, the signature was already reported, or the cap is reached.
- §6: no identifier is renamed or removed. `report` and `file` keep their
  flags and output keys. `file` gains an `already_reported` output key.
  `filed-state.json` keeps its format.
- §18: no new script. The helper is a new subcommand of the existing
  `permission_prompts.py`, started by the already-wired hook. No supervisor,
  no DB work, no `docs/scripts-pending-removal.md` entry.
- §14: `.claude/` assets reach every repo in `.github/ai/consumer_repos.json`
  on the next `@stable` sync. The `workflow-templates/.claude/**` twins are
  edited in the same PR and stay byte-identical (`test_template_parity`).
- §28.C: every file under `.claude/**` and `workflow-templates/.claude/**` is
  a protected path. The phase stops at `Status: BLOCKED` before it starts and
  lists the exact edits for the operator's session.
- §9: tabs in Python and Markdown code, as in the existing files.
- §20: one fragment, `changelog.d/4755-immediate-permission-prompt-report.md`.

## Approach

**Hook (`permission_prompt_logger.py`).** After `append_record` succeeds for
a `PermissionRequest`, start one detached child and return:
`subprocess.Popen([sys.executable, <repo>/.claude/scripts/permission_prompts.py, "report-now", "--log-file", <path>, "--cwd", <payload cwd>], stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True, close_fds=True)`.
The script path is resolved from the hook's own `__file__`. The hook never
waits on the child and never shares its stdout pipe, so the harness sees the
hook end at once. It still reads no environment variables, makes no API
calls, and prints nothing. A spawn failure is swallowed like any other error.
`PermissionDenied` spawns nothing.

**Helper (`permission_prompts.py report-now`).** It is always exit 0, prints
nothing, and swallows every error. The hook detaches it and nobody reads it.
1. Read the last record of `--log-file`. Continue only when its event is
   `PermissionRequest`, `CLAUDE_CODE_REMOTE_SESSION_ID` is set (a cloud
   session), and the record's `permission_mode` is `auto` or
   `bypassPermissions` (AD-1).
2. Take an exclusive `fcntl.flock` on `<log dir>/filing.lock`, the same lock
   `file` now takes. Load `immediate-state.json`
   (`{"reports": {sig: {"target": …, "ts": …}}, "count": n}`). Stop when the
   signature is already in it or `count` has reached
   `MAX_IMMEDIATE_REPORTS = 5` (AD-3).
3. Build the report from `group_patterns` over this session's log (the same
   signature, shape, and `record_example` the end-of-stage filer uses), plus
   `session_<id>`, `https://claude.ai/code/session_<id>`, the title from
   `<log dir>/session-meta.json` if present, else `not recorded`, and the
   marker `<!-- ai:permission-prompt-session:v1 session=<id> sig=<sig> -->`.
4. Resolve the slug with `git -C <cwd> config --get remote.origin.url`
   through `extract_repo_slug`.
   - **coding-workflows**: reuse `existing_issues` (one GET per 100 labelled
     issues). Comment on the matching issue, or open a new routed issue with
     `issue_body` plus the report block.
   - **Elsewhere** (AD-6): one GET `repos/<slug>/pulls?state=open&head=<owner>:<branch>`.
     Comment on that PR, else on issue `<N>` when the branch is
     `claude/implement-plan-issue-<N>-…`, else post nothing.
5. Only on a successful POST, record the signature in `immediate-state.json`
   and set `filed-state.json[sig]` to the pattern's count, so step 14 does
   not re-file those occurrences. A failed read or POST records nothing, so
   step 14 still files the pattern as today.

**`file` (end of stage).** Take the same lock. Skip every signature present
in `immediate-state.json` and list it under a new `already_reported` key.
This is what makes the report once per session (G5).

**`session-meta --title <t>`.** Writes `session-meta.json` into the log dir.
`/implement-plan-claude` step 0 and `/implement-issue-claude` step 0 run it
right after their `get_session` call, using the allowlisted
`PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/permission_prompts.py *`
shape (AD-4).

**`lookup --session <id> [--repo <slug>]`** (read-only, for the poller, AD-5):
1. One GET `search/issues?q=repo:<slug>+"<session id>"`.
2. One GET for the first hit's comments (per 100), picking the newest body
   carrying `ai:permission-prompt-session:v1 session=<id>`. Issue bodies are
   checked first.

It prints one JSON line: `found`, `issue_url`, `comment_url`, `signature`,
`event`, `tool_name`, `command`, `title`. It exits 2 on a failed read.

**§15 API budget.**

| Path | REST calls |
| --- | --- |
| Not unattended, already reported, or cap reached | 0 |
| Immediate report in coding-workflows | 1 GET per 100 `ai:permission-prompt` issues + 1 POST |
| Immediate report elsewhere | 1 GET (open PR for the branch) + 0 or 1 POST |
| Per session, worst case | 5 reports × the row above |
| `lookup` | 1 search GET + 1 comments GET per 100 |
| `file` at step 14 | unchanged, minus the signatures already reported |

The existing `existing_issues` read is reused, not duplicated. No GraphQL
calls.

**Alternatives.** The hook could post by itself. That was rejected because
it would delay the prompt by one to two API round trips, within a 10-second
hook timeout. A long-running supervisor tailing the logs was rejected under
§18.C, because the hook event already fires at the right moment.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1: immediate report, once per session, with lookup.**
   - **Files:** listed under Files & Modules.
   - **Protected paths:** `.claude/hooks/permission_prompt_logger.py`,
     `.claude/scripts/permission_prompts.py`,
     `.claude/commands/implement-plan-claude.md`,
     `.claude/commands/implement-issue-claude.md`, and their
     `workflow-templates/.claude/` twins.
   - **Done when:** the tests below pass, the twins are byte-identical, the
     docs and changelog fragment have landed, and the hook still prints
     nothing and exits 0 on every input.
   - **Rollback:** revert the PR. The hook returns to log-only behaviour, and
     `immediate-state.json` / `session-meta.json` become unused files outside
     the repository.

## Implementation Steps

Phase 1:
1. `.claude/scripts/permission_prompts.py`:
   - add `IMMEDIATE_STATE_FILE`, `SESSION_META_FILE`, `LOCK_FILE`,
     `MAX_IMMEDIATE_REPORTS`, `SESSION_MARKER_TEMPLATE` / `SESSION_MARKER_RE`,
     `_filing_lock()`, `session_label_from_env()`, `is_unattended(record)`,
     `immediate_block(...)`, `report_now(log_file, cwd)`,
     `lookup(session, slug)`, and `write_session_meta(title)`;
   - add subparsers `report-now`, `lookup`, and `session-meta`;
   - have `file_patterns` take the lock and skip the signatures in
     `immediate-state.json` (`already_reported`);
   - update the module docstring (usage, API budget).
2. `.claude/hooks/permission_prompt_logger.py`: `append_record` returns the
   path. On `PermissionRequest`, `main()` calls a new
   `spawn_reporter(path, cwd)` (a detached `Popen`, stdio `DEVNULL`,
   `start_new_session=True`). Update the docstring.
3. `.claude/commands/implement-plan-claude.md` step 0 and
   `.claude/commands/implement-issue-claude.md` step 0: add one sentence
   running `permission_prompts.py session-meta --title "<title from get_session>"`.
4. Copy the four files byte-identically to `workflow-templates/.claude/`.
5. `tests/test_permission_prompts.py`: add the tests listed below, and
   narrow `test_hook_has_no_api_calls_or_env_reads` so it still forbids
   `os.environ`, `getenv`, `urllib`, and `api.github.com` in the hook and
   allows exactly the one `subprocess.Popen` in `spawn_reporter`.
6. Docs: CLAUDE.md §23.I (the immediate report, the lookup, the budget, and
   the helper table row), agents.md (the permission-prompt bullets), README.md
   (the §23.I paragraph), and the changelog fragment.

## Files & Modules

- `.claude/hooks/permission_prompt_logger.py`
- `.claude/scripts/permission_prompts.py`
- `.claude/commands/implement-plan-claude.md`
- `.claude/commands/implement-issue-claude.md`
- `workflow-templates/.claude/hooks/permission_prompt_logger.py`
- `workflow-templates/.claude/scripts/permission_prompts.py`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/implement-issue-claude.md`
- `tests/test_permission_prompts.py`
- `CLAUDE.md`, `agents.md`, `README.md`
- `changelog.d/4755-immediate-permission-prompt-report.md` [new]

## Tests

Unit tests in `tests/test_permission_prompts.py`, which already runs in the
`ci.yml` step "Unattended helper and permission prompt report tests":

- **Detection:**
  - no report without `CLAUDE_CODE_REMOTE_SESSION_ID`;
  - no report in `default` or `plan` mode;
  - no report for `PermissionDenied`;
  - a report for `PermissionRequest` in `auto` / `bypassPermissions`.
- **Hook:**
  - spawns exactly one detached child for `PermissionRequest` and none for
    `PermissionDenied`, with `Popen` monkeypatched;
  - still prints nothing and exits 0 when `Popen` raises;
  - still has no environment reads or API calls.
- **Sanitizing:** a report built from a heredoc-plus-token command carries
  `record_example`'s output and no secret. A redacted title keeps its text.
- **Once per session:**
  - a second prompt with the same signature posts nothing;
  - the sixth distinct signature posts nothing (cap);
  - after a report, `file` skips that signature (`already_reported`) and makes
    no API call for it;
  - `filed-state.json` is updated.
- **Targets:**
  - coding-workflows comments on a matching closed issue, or opens one with
    both labels and both markers;
  - elsewhere, the open PR for the branch wins over the issue branch, and no
    target means no POST.
- **Fail open:**
  - a failed list read, POST, or git call leaves both state files unchanged
    and exits 0;
  - `file` still files that pattern afterwards.
- **Lookup:** finds the session marker in the issue body or comments, prints
  the command and link, and exits 2 on a failed read.
- **Contracts:** template parity for the four files, and the two commands
  carry the `session-meta` sentence.

End to end, the validation stage runs `report-now` against a fixture log in
dry-run form. The operator confirms the first real prompt lands on the issue.

## Risks & Mitigations

- The harness might wait for the detached child, which would delay the
  prompt. Mitigation: stdio is `DEVNULL` in a new session group, and the
  hook exits before the child makes any network call. A test asserts the
  `Popen` arguments.
- Attended Auto-mode cloud sessions also report at once, because
  `CLAUDE_CODE_SESSION_ATTENDED` cannot tell them apart.
  ACCEPTED — step 14 of those sessions files the same pattern anyway. The
  report only arrives earlier, and the once-per-session rule prevents
  duplicates.
- The container might kill orphaned children when the hook returns.
  ACCEPTED — pending the first real prompt after rollout. The helper is fail
  open, so step 14 filing still covers it.
- GitHub search indexing lag can hide a fresh report from `lookup` for about
  a minute. ACCEPTED — the poller alerts well after that.
- Concurrent helpers and `file` could race. Mitigation: one `flock` on
  `filing.lock` around every read-modify-write of the state files.

## Rollout

- The change merges into `main` through the project's final PR. Consumer
  repos get the hook and script on the next `@stable` sync (§14).
- **Operator step:** add `PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/permission_prompts.py lookup --session <id>`
  to the master poller's alert for a session whose `status_detail` reads
  `Waiting on permission: …`, and include its `command` and `issue_url`.
  The poller lives outside this repository.
- Rollback: revert the final PR.

## Auto-decisions

- AD-1 [plan, 2026-09-28] How does the report detect an unattended session?
  — Picked: A — a cloud session (`CLAUDE_CODE_REMOTE_SESSION_ID` set) whose
  record's `permission_mode` is `auto` or `bypassPermissions`, checked in the
  helper, for `PermissionRequest` only. Alternatives: B — every
  `PermissionRequest` anywhere; C — only titles matching unattended patterns
  (needs an API call the hook cannot make). Why: `CLAUDE_CODE_SESSION_ATTENDED`
  is `1` even in dispatcher sessions, every unattended session in this setup
  runs in Auto mode in the cloud, and the check makes no API call. Applied in:
  phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Does the hook post itself or hand off? — Picked: A —
  the hook spawns a detached `permission_prompts.py report-now` and returns
  at once; the hook still makes no API calls and reads no environment.
  Alternatives: B — the hook posts synchronously with a short timeout;
  C — a long-running supervisor tails the logs. Why: only A never delays the
  prompt; C adds a §18.C supervisor for an event that already fires. Applied
  in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Rate limit and once-per-session rule? — Picked: A —
  one report per signature per session, at most 5 per session, under a
  `flock`, with `file` skipping reported signatures. Alternatives: B — no
  cap; C — a time-window limit. Why: one prompt stops a session, so more than
  a few are rare; a fixed cap bounds the §15 budget. Applied in: phase 1 PR.
  Status: pending review
- AD-4 [plan, 2026-09-28] Where does the session title come from? — Picked:
  A — `/implement-plan-claude` and `/implement-issue-claude` record it at
  step 0 via `permission_prompts.py session-meta`, right after their existing
  `get_session`; other sessions report `not recorded` with the session link.
  Alternatives: B — session id and link only; C — every unattended command
  records it. Why: no hook-visible source has the title; A covers the
  incident's session type in two commands, and B misses a stated requirement.
  Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] The master poller is not in this repository; how
  does its alert get the command? — Picked: A — a per-session marker plus a
  read-only `permission_prompts.py lookup --session <id>`, documented, with
  the poller's own prompt change listed as an operator step. Alternatives:
  B — de-scope requirement 3; C — build a new in-repo poller supervisor.
  Why: A gives the poller the command and link in two REST reads without
  guessing at code outside the repo. Applied in: phase 1 PR. Status: pending
  review
- AD-6 [plan, 2026-09-28] Where does the report go outside coding-workflows?
  — Picked: A — the open PR whose head is the current branch, else the issue
  named by a `claude/implement-plan-issue-<N>-` branch, else nowhere.
  Alternatives: B — never post outside coding-workflows; C — file in
  coding-workflows from consumer repos. Why: A follows the issue's "the
  session's own tracked issue or PR" in one GET; C breaks §23.I's filing
  scope. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` printed `{"skip": false, "reason": "no skip label"}`.

## References

- Issue #4755; incident issue #4707 (`session_01Pqd1mbhdV8mCxriki9onge`).
- CLAUDE.md §15, §23.E, §23.I, §28.C.
- `docs/plans/claude-fixer-unattended-convergence-plan.md` (the master poller
  and supervising session, D16).

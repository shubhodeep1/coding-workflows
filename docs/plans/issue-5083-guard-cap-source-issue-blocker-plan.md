# Unattended question guard publishes a source-issue blocker at its cap

Source issue: shubhodeep1/coding-workflows#5083 (https://github.com/shubhodeep1/coding-workflows/issues/5083)
Base branch: claude/implement-plan-issue-4911-unattended-question-guard
Security pass: skip (ai:security: automation-produced issue)

## Summary

`.claude/hooks/unattended_question_guard.py` blocks an unattended issue-mode
session from ending its turn on an in-session question at most twice. The
third such stop is allowed with only a local log line and a `systemMessage`,
so nothing reaches the source issue and the chain stalls silently (security
finding `silent-stop-after-block-cap`, medium, `unattended_question_guard.py:367`).
This change makes the hook itself publish the `ai:claude-blocked` blocker on
the source issue at the cap. The publish is idempotent, retried within the
hook and on every later hook event in the session, and scoped to one comment
and one label on the marked issue.

## Context

- Issue #5083 (OWNER, filed by `security-audit.yml`, `Refs #3576`): "After
  two blocked stops, a further question about a failed security pass is
  allowed with only a local log entry and session message. No blocker
  reaches the source issue, leaving the unattended remediation chain
  stalled." Recommendation: "At the cap, automatically and idempotently
  publish a source-issue blocker through a narrowly scoped notifier, with
  automated retry on failure."
- The hook ships in the #4911 project (`claude/implement-plan-issue-4911-unattended-question-guard`,
  final PR #4964, not yet merged into `main`), so this project builds on
  that branch (its `Integration branch:` line).
- CLAUDE.md §28.C: a stop in issue mode is reported on the source issue as
  one comment starting `<!-- ai:claude-blocked:v1 -->`, the
  `ai:claude-blocked` label, and one `PushNotification`. The guard already
  treats a turn that posted such a comment as allowed (`blocked_comment_posted`).
- The hook today makes **no** GitHub API calls (CLAUDE.md §28.G, agents.md,
  the module docstring). The cap path is the one place that needs one.
- Session transport: `gh` through the agent proxy in cloud sessions
  (CLAUDE.md §23.A). The hook runs as a subprocess of the harness, not as a
  Bash tool call, so the §23.H `gh api` guard does not see its calls.
- Protected paths: the hook lives under `.claude/hooks/`, with a
  byte-identical twin under `workflow-templates/.claude/hooks/` pinned by
  `test_template_parity`. The interim twin-first rule (master-session Q40: A,
  until #4785 lands) edits the twin, and the operator copies it into
  `.claude/` as a `[claude-twin-sync]` commit.

## Goals

- At the cap (a `Stop` in a marked session whose final message asks a
  question or for permissions, no blocked comment in the current turn, and
  `stop_blocks >= STOP_BLOCK_CAP`), the hook publishes one comment on the
  marker's issue starting `<!-- ai:claude-blocked:v1 -->` and adds the
  `ai:claude-blocked` label, then allows the stop.
- **Idempotent:** at most one cap blocker per session per issue. The comment
  carries `<!-- ai:unattended-guard-cap:v1 session=<id> -->`. Before
  posting, the hook reads the issue's comments and skips the POST when that
  marker is already there. The local state records `cap_blocker` so a posted
  blocker is never re-read or re-posted.
- **Retried:** up to 3 attempts per hook invocation (backoff 1 s, 2 s), each
  `gh` call bounded by a timeout that keeps the whole hook under its 30 s
  wiring timeout. A publish that still fails is recorded as
  `cap_blocker: pending` and retried at the start of every later `Stop`
  event in the same marked session, including stops that are not questions.
- **Narrow scope:** only `repos/<marker repo>/issues/<marker issue>/comments`
  (GET, POST) and `.../labels` (POST), with the repo and issue taken from the
  validated marker (`_REPO_RE`, positive integer). The comment body is a
  fixed template (session id, kind, block count, what to do next). It never
  includes model-written text.
- **Observable:** every outcome appends one JSON line to `stop-guard.jsonl`
  (`cap_blocker_posted`, `cap_blocker_exists`, `cap_blocker_failed`), and the
  `systemMessage` says which one happened. The existing `cap_reached` line
  and `CAP_MESSAGE_PREFIX` stay as they are.
- **Fail open:** a missing `gh`, a failed read or write, or an internal
  error never blocks the stop and never raises.

## Non-goals

- Sending the `PushNotification` from the hook: hooks cannot call MCP
  tools. The comment and label are the durable signal; the pickup and the
  master session already react to `ai:claude-blocked`.
- Changing the cap (2), the question detection, the `AskUserQuestion`
  denial, or the marker format.
- A GitHub Actions-side notifier: an Actions job cannot see the session's
  local state.

## Constraints

- §6: no rename or removal of existing identifiers (`STOP_BLOCK_CAP`,
  `CAP_MESSAGE_PREFIX`, `CAP_LOG_NAME`, `cap_reached`, `evaluate`, the state
  file's `stop_blocks`). New names (`CAP_BLOCKER_MARKER`, `publish_cap_blocker`,
  `cap_blocker`) are checked for collisions in the module and the tests.
- §9: tabs in Python; the twin stays byte-identical.
- §15: the hook's only API calls are at the cap: one GET per 100 issue
  comments, one comment POST, one label POST, times at most 3 attempts per
  invocation. Documented in the docstring and §28.G.
- §19: `Refs #5083` on phase, fix, and completion PRs; the final PR targets
  the #4911 project branch, so the issue closes by an explicit close after
  the final merge.
- §20: one `changelog.d/5083-guard-cap-source-issue-blocker.md` fragment
  (`fixed`).
- §23.E: the hook never reads `GH_TOKEN` itself; `gh` does.
- §28.C: the `.claude/**` edit is a protected-path phase (see Phases).

## Approach

Add a small notifier inside the hook (AD-1). `evaluate` gains an injectable
`runner` (default: `subprocess.run` on `gh api`), so tests use a fake `gh`.
At the cap, `evaluate` calls `publish_cap_blocker(marker, session, kind,
blocks, directory, runner, now)`, which:

1. returns early when state already says `cap_blocker: posted`;
2. GETs the issue comments (`--paginate`) and returns `exists` when one
   carries this session's cap marker;
3. POSTs the fixed-template comment, then the label;
4. retries steps 2–3 up to 3 times with 1 s / 2 s sleeps (sleep injectable);
5. writes `cap_blocker: posted | pending` to the state file and one log line.

At the start of every `Stop` in a marked session, a `pending` state triggers
the same publish before normal evaluation, so a transient failure is retried
without a new question.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan; the change is
one hook, its twin, its tests, and its docs.

1. **Phase 1 — cap blocker publish in the unattended question guard.**
   - Files: see Files & Modules.
   - Protected paths: `.claude/hooks/unattended_question_guard.py` (through
     its `workflow-templates/.claude/hooks/` twin under Q40 when approved).
   - Done: the new tests and the whole `tests/test_unattended_question_guard.py`
     pass with the twin synced into `.claude/`; §28.G, agents.md, and README
     describe the cap publish; the changelog fragment exists.
   - Rollback: revert the phase PR; the hook returns to the log-only cap.

## Implementation Steps

Phase 1:
1. `workflow-templates/.claude/hooks/unattended_question_guard.py` (and, via
   the twin sync, `.claude/hooks/unattended_question_guard.py`): add
   `CAP_BLOCKER_MARKER`, the fixed comment template, `publish_cap_blocker`,
   the `runner` / `sleep` injection in `evaluate`, the pending-retry at the
   top of the `Stop` path, and the state and log fields. Update the module
   docstring (API calls at the cap only).
2. `tests/test_unattended_question_guard.py`: cases for posted, already
   exists (idempotent), retry succeeds on attempt 2, all attempts fail →
   pending → retried on the next `Stop`, label failure after a posted
   comment, `gh` missing, invalid marker repo (no call), and a posted state
   that makes no further calls.
3. CLAUDE.md §28.G and its `workflow-templates/CLAUDE.md` twin, agents.md,
   README.md: replace "no GitHub API calls" with the cap-publish contract.
4. `changelog.d/5083-guard-cap-source-issue-blocker.md`.

## Files & Modules

- `.claude/hooks/unattended_question_guard.py` (protected; twin sync)
- `workflow-templates/.claude/hooks/unattended_question_guard.py`
- `tests/test_unattended_question_guard.py`
- `CLAUDE.md`, `workflow-templates/CLAUDE.md`
- `agents.md`, `README.md`
- `changelog.d/5083-guard-cap-source-issue-blocker.md` [new]

## Tests

- Unit: the cases in step 2, with a fake runner and a no-op sleep; the
  existing 47 cases unchanged.
- Parity: `test_template_parity` after the twin sync.
- CI: the existing `ci.yml` step for `tests/test_unattended_question_guard.py`.

## Risks & Mitigations

- The hook's 30 s timeout is exceeded by slow `gh` calls → per-call timeout
  of 8 s and at most 3 attempts; remaining work falls to the pending retry.
- A duplicate blocker from two stops racing → the per-session marker check
  and the state flag; a race between two hook processes in one session is
  ACCEPTED (the harness runs `Stop` hooks one at a time).
- The proxy refuses the write (repo not attached) → recorded as
  `cap_blocker_failed`, stop allowed, and `cap_reached` still logged.

## Rollout

Ships to consumer repos with the `.claude/` sync after the #4911 project
merges into `main`. No flags or env vars.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where does the notifier live? — Picked: A — a function inside `unattended_question_guard.py`. Alternatives: B — a new `.claude/scripts/` helper the hook calls. Why: one file and one twin to sync, one marker format (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Which transport does the notifier use? — Picked: A — `gh api` as a subprocess. Alternatives: B — `urllib` with `GH_TOKEN`. Why: §23.E forbids committed code that reads `GH_TOKEN` from the session; `gh` works through the proxy. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How is the publish made idempotent? — Picked: A — a per-session hidden marker in the comment, checked by one paginated read before the POST, plus a local `cap_blocker` state flag. Alternatives: B — the local state flag only. Why: the state file can be lost with the container; the issue is the source of truth. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How is a failed publish retried? — Picked: A — 3 attempts in the hook (1 s, 2 s backoff), then `pending` state retried at every later `Stop` in the session. Alternatives: B — in-hook attempts only; C — an Actions sweep. Why: bounded inside the hook timeout, and a later retry needs no new question; Actions cannot see local state. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] What does the comment contain? — Picked: A — a fixed template (session id, kind, block count, how to resume). Alternatives: B — also quote the final assistant message. Why: model text is untrusted and could carry secrets (§1). Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Is the stop still allowed after the publish? — Picked: A — yes, as today. Alternatives: B — keep blocking until a publish succeeds. Why: an unbounded block loops the session; the blocker on the issue is the signal. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5083` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #5083; audit tracker #3576; base project #4911 (final PR #4964).
- `docs/operations/master-session.md` Q40 (interim twin-first), #4785, #4948.

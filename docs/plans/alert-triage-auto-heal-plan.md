# Hourly alert triage and auto-heal

## Summary

Add an hourly GitHub Actions job that collects the pipeline's WARNING and
ERROR alerts, investigates each one with the Claude engine, and fixes what it
can without a human: issues the pipeline can fix go to the existing heal
intake, and defects in the pipeline itself get a direct fix PR that
auto-merges. You are paged on Telegram only for problems it could not fix.

## Context

Today almost every WARNING/ERROR/CRITICAL alert from this repository and the 13
consumers in `.github/ai/consumer_repos.json` goes straight to Telegram
through `tg_send_msg` in `scripts/tg_helpers.sh`. The operator reads each
one and runs `/investigate-issue` on it in an interactive session. The level
convention in `tg_helpers.sh` already says WARNING and above mean "a human
has to act", so every message that reaches Telegram is, by design, work for
the operator.

Two facts shape the design:

- **Telegram cannot be read back by the bot.** The Bot API never delivers a
  bot's own messages as updates, and bots cannot see other bots' messages in
  a group. Nothing in the repository reads Telegram today (no `getUpdates`,
  webhook or user-account client). Alerts are therefore captured at the
  source, inside `tg_send_msg`, into an inbox in this repository.
- **Session-based automation was retired on 2026-10-03**
  (`docs/plans/replace-claude-sessions-with-cli-engine-plan.md`). The job runs
  in Actions with the Claude CLI engine (`scripts/ai_engine.sh`), authenticated
  through the claude-workers pool and the OIDC-gated `claude-pool-broker`
  (`.github/actions/claude-pool-token`, `scripts/claude_pool_token.sh`), with
  the existing codex fallback when the pool is unavailable.

Existing mechanisms this plan extends rather than duplicates (§5):

- **Workflow Failure Heal intake** (`.github/workflows/workflow-failure-heal-intake.yml`,
  `scripts/workflow_failure_heal_intake.sh`): fingerprints, dedup against
  `ai:workflow-heal` issues, lineage and budget caps, and routing to this repo
  or the source repo. Healable alerts are handed to it as a new report kind.
- **Standalone clarify auto-decide** (`STANDALONE_AUTO_DECIDE_ENABLED`,
  `STANDALONE_CLARIFY_RESPOND_ENABLED`, `scripts/auto_decisions.py`): answers
  clarify questions on standalone issues automatically.
- **`ai-review/head-gate`** (`scripts/review_head_gate.sh`): one of the two
  required status checks on `main` (with `lint`); the ruleset has no bypass
  actors. A fix for a broken review pipeline can never merge through that
  same pipeline, which is why a break-glass gate is needed (Phase 3).

The 2026-11-10/11 outage added a third fact. GitHub's `ubuntu-latest` moved
to `ubuntu-26.04` (Python 3.14), and `scripts/render_prompt.py` then crashed
in every consumer's clarify, plan and review runs, in every release from
v1.45.0 to v1.47.1 (fixed on `main` by #9240 and on `stable` by #9339). As
first drafted, this plan would have classified that failure `environmental`
(a `done` marker, no Telegram), and even a correct self-fix would have
reached only `main`, leaving consumers broken until the next promotion. A
failure introduced by a release needs the opposite treatment: roll `@stable`
back. Phase 6 adds two fleet routes that tell these cases apart from inbox
data this plan already captures (`source_repo`, `release_sha`), using the
rollback workflow from #9342.

### Operator decisions (clarification session, 2026-11-11)

| Q | Answer | Meaning |
|---|---|---|
| Q1 | A | Capture alerts at the source (`tg_send_msg`) into an inbox in coding-workflows |
| Q2 | A | Only the pipeline's own alerts (this repo + registered consumers); app alerts from other bots are out of scope |
| Q3 | A | Runs as a GitHub Actions cron workflow on the Claude engine (claude-workers pool, codex fallback) |
| Q4 | B | Self-fix PRs auto-merge once required checks pass |
| Q5 | A (assumed) | Healable alerts go through the existing heal intake |
| Q6 | A (assumed) | Kill switches default off; per-run alert cap |
| Q7 | A | Break-glass gate: the self-fix workflow may post `ai-review/head-gate` itself under strict conditions |
| Q8 | A (assumed) | No daily digest; only "could not fix" alerts |
| Q9 | A | WARNING/ERROR go to the inbox only (Telegram only if unfixable); CRITICAL is still sent immediately and also captured |
| Q10 | A | Clarify questions on issues opened by this system are always answered with the RECOMMENDED option and never page the operator; other issues unchanged |
| Q11 | A (assumed) | Daily watchdog in `workflow-log-analysis.yml` |
| Q12 | A | Fleet triggers live in this plan (Phase 6); the rollback workflow itself is #9342 (asked as Q5 in the rollback session) |
| Q13 | A | A rollback holds automatic re-releases with a self-clearing `ai/stable-hold` marker (#9342; asked as Q6) |
| Q14 | A | Automatic rollback: same fingerprint in at least 3 consumers within 2 hours, all on the current `stable` SHA, never seen on the previous release; one step back, at most once per 24 hours (asked as Q7) |
| Q15 | A | A fleet-wide failure that is not a release regression is fixed forward: self-fix on `main`, cherry-pick PR to `stable`, full release gate (asked as Q8) |

## Decisions

### D1 — Capture alerts at the source, not from Telegram
- **Chosen:** `tg_send_msg` dispatches each WARNING+ alert to an inbox branch in coding-workflows (Q1 A).
- **Alternatives considered:** reading the chat with a Telegram user-account client; issue-comment inbox; grepping run logs; a Cloudflare Worker store.
- **Why:** the Bot API cannot return the bot's own messages; the branch inbox needs no new secret, triggers no other workflows, and has conflict-free unique paths.

### D2 — Run in Actions on the Claude engine
- **Chosen:** an hourly cron workflow using `scripts/ai_engine.sh` and the claude-workers pool, with codex fallback (Q3 A).
- **Alternatives considered:** a Claude Code Routine; Actions plus a Routine for broken-pipeline fixes.
- **Why:** session-based automation was retired on 2026-10-03 for reliability reasons, and an unattended session would stall on CLAUDE.md's ask-first rules.

### D3 — Healable alerts go through the existing heal intake
- **Chosen:** a new `alert_triage` report kind for `workflow_failure_heal_intake.sh` (Q5 A).
- **Alternatives considered:** the triage job opens issues itself.
- **Why:** the intake already owns fingerprint dedup, lineage and budget caps (§5: extend, don't compete).

### D4 — Self-fix PRs auto-merge, with a break-glass review gate
- **Chosen:** self-fix PRs enable auto-merge (Q4 B); a strictly bounded break-glass job may post `ai-review/head-gate` when the review pipeline gives no verdict (Q7 A).
- **Alternatives considered:** human merge; a ruleset bypass actor; no bypass.
- **Why:** a fix for the review pipeline can never pass through the broken review pipeline, and the `main` ruleset has no bypass actors.

### D5 — Telegram only for unfixable problems
- **Chosen:** WARNING/ERROR go to the inbox only, CRITICAL is still sent immediately, and the triage system pages only when it cannot fix something (Q8 A, Q9 A).
- **Alternatives considered:** inbox-only for all levels; keep every alert plus "could not fix" alerts; a daily digest.
- **Why:** the operator asked to hear only about problems that need them; CRITICAL stays immediate as a safety net.

### D6 — RECOMMENDED-only clarify answers for this system's issues
- **Chosen:** issues labelled `ai:alert-triage` always take the RECOMMENDED option and never page (Q10 A).
- **Alternatives considered:** keep the clarify-respond worker; RECOMMENDED-only for every standalone issue.
- **Why:** the operator asked for RECOMMENDED answers; scoping by label leaves every other issue unchanged.

## Goals

- Every `tg_send_msg` call at WARNING, ERROR or CRITICAL, in this repo and in
  every registered consumer, writes one record to the alert inbox when capture
  is on.
- With routing on, WARNING and ERROR alerts no longer reach Telegram directly;
  if capture fails, the alert is sent to Telegram as today (fail open toward
  the human).
- An hourly job processes every unprocessed inbox record exactly once
  (idempotent per record id), within one hour of capture.
- Each alert group gets one of these outcomes, logged with a stable prefix:
  `noise`, `already_tracked`, `healable`, `pipeline_blocked`,
  `environmental`, `escalated`, and (Phase 6) `release_regression`,
  `fleet_environmental`.
- `healable` alerts produce a heal-intake report; the intake's dedup and
  budget decide whether an issue opens.
- `pipeline_blocked` alerts produce a fix PR on the default branch with
  auto-merge enabled; if the review pipeline gives no verdict in 60 minutes,
  the break-glass gate can pass it after its own checks.
- The operator gets a Telegram message only for: an alert the job could not
  fix, a heal issue or self-fix PR stuck past its threshold, a break-glass
  merge, or the job itself not running.
- A failure seen in at least `ALERT_TRIAGE_FLEET_MIN_REPOS` consumers within
  `ALERT_TRIAGE_FLEET_WINDOW_MINUTES` is never marked `done` silently
  (Phase 6).
- A fleet failure that started with the current `@stable` release is rolled
  back through `rollback-stable.yml` (#9342); a fleet failure that the
  previous release also shows is fixed forward on `stable` and released
  through the full gate (Phase 6).

## Non-goals

- Reading Telegram (no Bot API polling, no user-account client).
- Alerts sent by consumer applications with their own bots (Q2).
- The raw-`curl` Telegram senders that bypass `tg_send_msg`
  (`scripts/implement_handle_guard_block.sh`, `.github/workflows/update_workflows.yml`,
  `validation-refresh.yml`, `test-and-mark-stable.yml`, `issue_pr_status.yml`,
  `implement.yml`, `check_failure_triage.yml`, `scripts/gh_helpers.sh`). They
  keep sending directly; moving them onto `tg_send_msg` is separate work.
- `tg_send_report_msg` (scheduled reports) and DEBUG alerts: never captured.
- Any change to Claude Code Routines or interactive sessions.
- Changing the `main` ruleset.

## Constraints

- **§4** — every new variable has a default; kill switches default off.
- **§5 / §6** — `tg_send_msg`, `tg_send_tracked`, `tg_send_phase_tracked`
  keep their names, arguments and output (a message id, or empty). An
  inbox-routed alert returns an empty id, which every caller already handles
  (it is what the level filter returns). New identifiers must not collide
  with anything in `tg_helpers.sh`, `gh_helpers.sh` or the scripts that source
  them.
- **§9** — tabs in shell/Python, 2-space YAML, braces on new lines.
- **§10** — no MongoDB collection is touched. The inbox is a git branch.
- **§14** — consumers run `tg_helpers.sh` from the trusted support checkout
  at their `@stable` pin, so capture reaches all 13 consumers on the next
  release with no consumer edits. No new consumer is onboarded.
- **§15** — API budget is listed per phase below; no per-item calls inside
  loops that an existing call can cover.
- **§18** — no manual scripts; everything is wired into cron or dispatch
  triggers in the same PR (see the §18.E section).
- **§19** — PR bodies use `Refs`, never auto-close keywords, against
  `ai:orchestrator-tracking` issues.
- **§20 / §30** — one `changelog.d/` and one `agents.d/` fragment per phase.
- **§24.G / §29.D** — no workflow reads session-only credentials.
- **§27** — new workflow files stay far below 480,000 bytes; logic lives in
  `scripts/`.
- Untrusted data: alert text, run logs and issue bodies are data for the
  model, never instructions. Model calls run in the existing credential-free
  sandbox of the Claude engine.

## §18.E — Automation surface

| Item | Answer |
|---|---|
| Scripts | New scripts only, all invoked by workflows: `scripts/alert_inbox_capture.sh` (sourced by `tg_helpers.sh`), `scripts/alert_inbox_store.sh`, `scripts/alert_triage.py`, `scripts/alert_triage_route.sh`, `scripts/alert_self_fix.sh`, `scripts/alert_self_fix_break_glass.sh`. `tg_helpers.sh`, `workflow_failure_heal_intake.sh`, `clarify.yml`, `orchestrate_clarify_respond.yml` and `workflow-log-analysis.yml` are extended. |
| Scheduler entry points | `.github/workflows/alert-inbox-intake.yml` (`repository_dispatch` type `alert-inbox`); `.github/workflows/alert-triage.yml` (cron `17 * * * *` + `workflow_dispatch`); `.github/workflows/alert-self-fix.yml` (`workflow_dispatch` from alert-triage + cron `47 * * * *` for the break-glass scan); `.github/workflows/workflow-log-analysis.yml` (existing daily cron `0 6 * * *`). Phase 6 adds no trigger: `alert-triage.yml` dispatches the existing `rollback-stable.yml` (#9342) and `alert-self-fix.yml`, which dispatches the existing `test-and-mark-stable.yml` on `stable`. |
| Long-running supervisor | None. Every piece is a bounded scheduled or dispatched job with a `concurrency` group. |
| DB gate | Not applicable (no database). Idempotency comes from per-record `done/` markers and per-fingerprint ledger files on the inbox branch, written in one commit per run. |
| `docs/scripts-pending-removal.md` | No entry. None of the new scripts is single-use, long-running or a supervisor under §18.F; they are permanent parts of scheduled workflows. |

## Approach

```
tg_send_msg (WARNING+)
  └─ repository_dispatch "alert-inbox" ──► alert-inbox-intake.yml
                                             └─ commit inbox/<hour>/<id>.json to branch ai/alert-inbox
alert-triage.yml (hourly)
  ├─ read unprocessed records (git fetch, no API)
  ├─ deterministic filter + group by fingerprint
  ├─ Claude engine diagnosis per group (investigate-issue style, read-only)
  └─ route:
       healable          → repository_dispatch "workflow-failure-heal" kind=alert_triage
       pipeline_blocked  → workflow_dispatch alert-self-fix.yml
       noise / tracked / environmental → log only
       anything it cannot decide / fails → Telegram (direct)
alert-self-fix.yml
  ├─ fix on a branch from main, tests, PR, auto-merge
  └─ hourly break-glass scan: PRs with no review verdict after 60 min
```

### Inbox storage

The inbox is the branch `ai/alert-inbox` in coding-workflows, holding JSON
files at unique paths: `inbox/<YYYY>/<MM>/<DD>/<HH>/<epoch_ms>-<source_repo_slug>-<run_id>-<sha8>.json`.
Unique paths mean concurrent writes never conflict, and the existing all-branch
ruleset (no deletion, no force-push) is compatible because the branch only
moves forward. Records are written by `alert-inbox-intake.yml` with
`GITHUB_TOKEN`, so the commits trigger no other workflows. Processed records
get a marker `done/<record_id>` written by the triage job; records older than
14 days and their markers are removed by the triage job in normal commits.

Alternatives rejected: issue comments on an inbox issue (four workflows in this
repo trigger on `issue_comment`, and a single issue grows without bound);
grepping run logs across 14 repos hourly (one log download per run, against
§15); a Cloudflare Worker store (new deploy surface and an Actions-side
Cloudflare secret, §24.G).

Record schema (`alert_inbox.v1`): `id`, `captured_at`, `level`,
`source_repo`, `workflow`, `run_id`, `run_url`, `job`, `message` (capped at
4,000 bytes, UTF-8 safe via `scripts/truncate_to_utf8_byte_cap.py`),
`release_sha`, `sent_to_telegram` (bool).

### Capture and routing in `tg_send_msg`

Configured by a new file `.github/ai/alert_inbox.json`, read from the
trusted support checkout (the same way as `claude_engine.json`), so one
reviewed change switches every repo at its next `@stable` sync:

```json
{ "capture_enabled": false, "route_warning_error_to_inbox": false }
```

Behaviour of `tg_send_msg` after the level filter passes:

| Level | capture off | capture on, routing off | capture on, routing on |
|---|---|---|---|
| WARNING / ERROR | send (today) | send + capture | capture only; send if capture fails |
| CRITICAL | send (today) | send + capture | send + capture |
| DEBUG | unchanged | unchanged | unchanged |

Capture is one `repository_dispatch` POST to `shubhodeep1/coding-workflows`
using the `GH_TOKEN` already in the step's environment, bounded by
`--max-time 10`, never failing the caller. If `GH_TOKEN` is missing or cannot
dispatch (for example a consumer step running with `GITHUB_TOKEN`), capture
fails and the alert is sent to Telegram. Messages the triage system sends
about itself go through a new direct sender that bypasses capture, so it never
triages its own alerts.

### Triage

`scripts/alert_triage.py` (deterministic, no model) reads the unprocessed
records, oldest first, up to `ALERT_TRIAGE_MAX_ALERTS` (default 30; the rest
wait for the next run), and:

1. groups them by fingerprint (source repo + workflow + normalised message,
   with numbers, SHAs and URLs masked);
2. marks a group `already_tracked` when an open `ai:workflow-heal` issue or an
   open `ai:alert-self-fix` PR carries the same fingerprint (one list call each,
   reused for the whole run);
3. checks the per-fingerprint ledger on the inbox branch: after 2 fix attempts
   for one fingerprint in 7 days, the group is escalated instead of retried;
4. (Phase 6) classifies fleet groups before any model call. It counts the
   distinct consumer `source_repo` values with the group's fingerprint in the
   inbox (14-day retention, processed records included). If at least
   `ALERT_TRIAGE_FLEET_MIN_REPOS` (default 3) report it within
   `ALERT_TRIAGE_FLEET_WINDOW_MINUTES` (default 120), the group is a fleet
   group and is never marked `done`:

   | Condition (all must hold) | Classification |
   |---|---|
   | every fleet record's `release_sha` is the current `stable` commit; the fingerprint's first record is after the current release's `published_at`; no record with that fingerprint carries the previous release's `release_sha` | `release_regression` |
   | any other fleet group | `fleet_environmental` |

   A consumer that lags on an older release and still shows the failure is
   evidence against a release regression, which keeps the rule on the safe
   side. The current and previous release SHAs and `published_at` come from
   one tag-refs read and one release read per run, reused for every group
   (§15). Fleet groups skip the model's classification; the model still
   writes `summary`, `proposed_fix` and `files` for the hotfix lane.

Each remaining group goes to the Claude engine as role `ALERT_TRIAGE`
(`.github/ai/claude_engine.json`: Opus, high effort; codex fallback through
`ai_engine.sh`) with a new prompt `prompts/mode-alert-triage.txt` that follows
`/investigate-issue`'s procedure and decision rule, read-only. The host
collects the evidence before the call: the run's failed jobs and a filtered log
tail through the heal intake's existing collector
(`scripts/workflow_failure_heal_evidence.sh` and the bounded log fetch in
`workflow_failure_heal_intake.sh`), capped by
`ALERT_TRIAGE_MAX_EVIDENCE_GETS` (default 5). The model returns JSON:
`classification` (`noise | healable | pipeline_blocked | environmental |
unsure`), `fingerprint`, `summary`, `evidence`, `proposed_fix`, `files`.
Whenever the model would ask a question, it takes the RECOMMENDED option and
records it in `decisions` (Q10).

`pipeline_blocked` is decided by the host as well as by the model: a group is
`pipeline_blocked` when `files` intersects `pipeline_self_files` in
`.github/ai/alert_inbox.json` (initially `implement.yml`, `review_autofix.yml`,
`review_autofix_sweep.yml`, `clarify.yml`, `plan.yml`,
`orchestrate_clarify_respond.yml`, `workflow-failure-heal-intake.yml`,
`alert-triage.yml`, `alert-self-fix.yml`, `ci.yml`, `scripts/ai_engine.sh`,
`scripts/claude_pool_token.sh`, `scripts/review_head_gate.sh`,
`scripts/workflow_failure_heal_intake.sh`, `scripts/tg_helpers.sh`), when the
alert came from one of those workflows, or when an `ai:alert-triage` heal issue
for the same fingerprint has been open longer than `ALERT_TRIAGE_HEAL_STALL_HOURS`
(default 6).

Routing (`scripts/alert_triage_route.sh`):

| Classification | Action | Telegram |
|---|---|---|
| `noise`, `environmental` | `done` marker | none |
| `already_tracked` | occurrence noted in the ledger | none |
| `healable` | `workflow-failure-heal` dispatch, kind `alert_triage`, with the diagnosis as evidence | none |
| `pipeline_blocked` | `alert-self-fix.yml` dispatch (when `ALERT_TRIAGE_SELF_FIX_ENABLED=true`) | none; escalates if the switch is off or the dispatch fails |
| `unsure`, model failure, ledger cap, parse failure | `done` marker + escalation | one message per group |
| `release_regression` (Phase 6) | dispatch `rollback-stable.yml` (#9342; `target_version` blank = previous release, `reason` = fingerprint + repo list) when `ALERT_TRIAGE_AUTO_ROLLBACK_ENABLED=true`, no automatic rollback in the last 24 hours (ledger) and no active stable hold | CRITICAL from `rollback-stable.yml`; escalates instead if the switch is off, the cap is hit, a hold is active or the dispatch fails |
| `fleet_environmental` (Phase 6) | `alert-self-fix.yml` dispatch with `stable_hotfix=true` when `ALERT_TRIAGE_STABLE_HOTFIX_ENABLED=true` | one notice per group (fleet-wide outage); escalates if the switch is off or the dispatch fails |

A single-repo `environmental` group keeps its row above (`done`, no Telegram).

All `done` markers and ledger updates of one run are written in one commit.
Each run also re-checks open `ai:alert-triage` issues and `ai:alert-self-fix`
PRs from the same list calls and escalates, once (a ledger flag), any issue
open longer than `ALERT_TRIAGE_ISSUE_ESCALATE_HOURS` (default 24) or PR open
longer than `ALERT_TRIAGE_PR_ESCALATE_HOURS` (default 6).

### Heal intake: new kind `alert_triage`

`workflow_failure_heal_intake.sh` accepts `alert_triage` reports from this
repository only (sender verified like other self-repo kinds). It reuses
fingerprint dedup, lineage and budget, and labels the issue it opens with
`ai:workflow-heal` and `ai:alert-triage`. The triage diagnosis goes into the
intake prompt as untrusted evidence, alongside the run logs the intake
collects as usual. Routing follows the existing table (`consumer-app-defect`
goes to the source repo, and so on).

### Self-fix and break-glass

`alert-self-fix.yml` (`workflow_dispatch`, input: the group record) runs the
Claude engine implement role on a clean checkout of the default branch at a
pinned SHA, in the existing implement sandbox (no credentials in the model
process). It writes the fix on `ai/alert-self-fix-<fingerprint8>-<run_id>`,
runs the affected tests (`.claude/hooks/prepush_affected_tests.py` logic in
CI form, plus the static checks `.github/workflows/ci.yml` runs), opens a PR labelled `ai:alert-self-fix` with the
diagnosis, auto-decisions and `Refs` links, lints the body with
`scripts/lint_pr_body_auto_close.py`, and enables auto-merge (squash, match
head commit) (Q4). The normal review pipeline reviews it like any PR.

The break-glass scan (cron `47 * * * *` in the same workflow, gated by
`ALERT_TRIAGE_BREAK_GLASS_ENABLED`, default `false`) posts
`ai-review/head-gate` = `success` on a PR's head SHA only when **all** hold:

1. the PR carries `ai:alert-self-fix`, was opened by this workflow, and its
   head is the SHA this workflow pushed (no human or other bot commits);
2. no `ai-review/head-gate` status exists for that SHA after 60 minutes, or
   the latest one is `error` (the review crashed); a `failure` (the review
   ran and rejected) is respected and escalated, never overridden;
3. every changed file matches `break_glass_allowed_paths` in
   `.github/ai/alert_inbox.json` (`.github/workflows/*.yml`, `scripts/**`,
   `prompts/**`, `tests/**`, `changelog.d/*.md`, `agents.d/*.md`) and none
   matches `break_glass_denied_paths` (`.github/ai/**`, `.claude/**`,
   `CLAUDE.md`, `unattended_system_instructions.md`, `tools/**`,
   `scripts/lint_pr_body_auto_close.py`, `scripts/alert_self_fix_break_glass.sh`);
4. a separate Claude review call (role `ALERT_BREAK_GLASS_REVIEWER`, its own
   prompt, sees only the diff and the diagnosis) returns `approve`;
5. the full test suite passes on the head SHA in a credential-free job;
6. fewer than `ALERT_TRIAGE_BREAK_GLASS_MAX_PER_DAY` (default 2) break-glass
   passes in the last 24 hours.

Each break-glass pass sends one Telegram message (merge happened without the
review pipeline). Code still reaches consumers only through the existing
`@stable` release gate.

### Stable hotfix lane (Phase 6)

When `alert-self-fix.yml` is dispatched with `stable_hotfix=true`:

1. It makes the fix on the default branch as above, unless the model's
   `proposed_fix` names a commit already merged on `main` (as #9240 was
   during the 2026-11-10/11 outage); then it goes straight to step 2.
2. Once the `main` fix has merged (checked by the hourly scan, no polling
   loop), or right away for an already-merged commit, it cherry-picks that
   commit's diff onto `origin/stable` as
   `ai/alert-stable-hotfix-<fingerprint8>-<run_id>`, runs the affected tests,
   opens a PR to `stable` labelled `ai:alert-self-fix` and `ai:stable-hotfix`,
   and enables auto-merge. A cherry-pick conflict escalates; it is never
   resolved by the model. The `stable` branch requires no status checks today
   (rules: no deletion, no force push, Copilot review), so break-glass does
   not apply to this PR.
3. After that PR merges, it dispatches `test-and-mark-stable.yml` on `stable`
   with the full gate (never `skip_e2e`). A deliberately dispatched release
   is not blocked by a stable hold, and it clears the hold (#9342).
4. Cap: one stable hotfix per fingerprint per 24 hours (ledger). A failed
   release gate escalates.

### Clarify: RECOMMENDED-only for `ai:alert-triage` issues (Q10)

In `clarify.yml`'s standalone auto-decide step and in
`orchestrate_clarify_respond.yml`'s standalone gate, an issue labelled
`ai:alert-triage` skips the clarify-respond worker delegation and takes the
RECOMMENDED-only path (`scripts/auto_decisions.py`), and its "Clarification
required" Telegram alert and the loop-guard WARNING are suppressed. If a
question has no RECOMMENDED option, nothing is sent; the triage job's 24-hour
stuck-issue escalation is the backstop. Every other issue keeps today's
behaviour.

### Watchdog (Q11)

The daily `workflow-log-analysis.yml` run adds one check: when
`ALERT_TRIAGE_ENABLED=true`, read the latest `alert-triage.yml` runs (one
`actions/workflows/alert-triage.yml/runs?per_page=5` call) and send one direct
Telegram message if the last successful run is older than 3 hours, or if the
oldest unprocessed inbox record (one `git fetch` of `ai/alert-inbox`) is older
than 3 hours.

## Phases & Merge Strategy

Every phase is off by default and safe to merge in any order. A phase that
depends on another phase's output does nothing (or escalates) until it exists.

1. **Alert inbox capture.** `alert-inbox-intake.yml`, `alert_inbox_store.sh`,
   `alert_inbox_capture.sh`, the `tg_send_msg` hook, the direct sender,
   `.github/ai/alert_inbox.json` (both switches `false`), and the labels
   `ai:alert-triage` and `ai:alert-self-fix` in `label_contract.v1.json`.
   *Done:* with the switches off, `tg_send_msg` output and behaviour are
   byte-identical to today (tested); with capture on in a test, one record
   lands on the branch. *Rollback:* revert the PR; with the switches off it is
   inert already.
2. **Hourly triage.** `alert-triage.yml`, `alert_triage.py`,
   `alert_triage_route.sh`, `prompts/mode-alert-triage.txt`, the
   `ALERT_TRIAGE` role, and the heal intake's `alert_triage` kind. Gated by
   `ALERT_TRIAGE_ENABLED` (default `false`). With no inbox branch (phase 1 not
   merged) the run logs `skip reason=no_inbox`. With
   `ALERT_TRIAGE_SELF_FIX_ENABLED=false` or `alert-self-fix.yml` missing,
   `pipeline_blocked` escalates. *Done:* fixture-driven tests cover each
   classification and routing row; a dry-run mode (`ALERT_TRIAGE_DRY_RUN`,
   default `true` for the first rollout) logs decisions without dispatching.
   *Rollback:* revert, or set `ALERT_TRIAGE_ENABLED=false`.
3. **Self-fix and break-glass.** `alert-self-fix.yml`, `alert_self_fix.sh`,
   `alert_self_fix_break_glass.sh`, the `ALERT_BREAK_GLASS_REVIEWER` role and
   prompt. Self-fix runs only when dispatched (phase 2); the break-glass scan
   is gated by `ALERT_TRIAGE_BREAK_GLASS_ENABLED=false`. *Done:* tests cover
   every break-glass condition, including a `failure` status never being
   overridden and a denied path blocking the pass. *Rollback:* revert, or turn
   the switch off; open self-fix PRs fall back to normal review.
4. **Clarify RECOMMENDED-only for `ai:alert-triage`.** `clarify.yml`,
   `orchestrate_clarify_respond.yml`, tests. No-op until an issue carries the
   label. *Done:* tests show a labelled issue takes the RECOMMENDED path with
   no Telegram alert, and an unlabelled one is unchanged. *Rollback:* revert.
5. **Watchdog.** One step in `workflow-log-analysis.yml` plus its script.
   Skips when `ALERT_TRIAGE_ENABLED` is not `true` or the workflow does not
   exist. *Done:* tests for stale-run, stale-inbox and skip cases.
   *Rollback:* revert.
6. **Fleet routes.** Fleet detection in `alert_triage.py`, the
   `release_regression` and `fleet_environmental` routing rows, the
   `stable_hotfix` input of `alert-self-fix.yml`, and the label
   `ai:stable-hotfix`. New variables, all safe by default:
   `ALERT_TRIAGE_AUTO_ROLLBACK_ENABLED=false`,
   `ALERT_TRIAGE_STABLE_HOTFIX_ENABLED=false`,
   `ALERT_TRIAGE_FLEET_MIN_REPOS=3`, `ALERT_TRIAGE_FLEET_WINDOW_MINUTES=120`.
   Without phase 2 there is nothing to classify; without phase 3 or with
   `rollback-stable.yml` (#9342) not merged, the dispatch fails and the group
   escalates. *Done:* fixture tests for both classifications, including the
   lagging-consumer case, the 24-hour caps, an active hold, a cherry-pick
   conflict and the already-merged-fix shortcut. *Rollback:* revert, or turn
   both switches off.

## Implementation Steps

### Phase 1
1. `[new] .github/ai/alert_inbox.json` — switches, `pipeline_self_files`,
   break-glass path lists (used by later phases; inert here).
2. `[new] scripts/alert_inbox_capture.sh` — builds the `alert_inbox.v1`
   payload and sends one `repository_dispatch` (`--max-time 10`); returns
   non-zero on any failure; logs `ALERT_INBOX capture outcome=sent|failed reason=`.
3. `scripts/tg_helpers.sh` (`tg_send_msg`, lines ~105–135) — after the level
   filter, apply the routing table above; add the direct sender used by the
   triage system. Keep the function's output contract.
4. `[new] .github/workflows/alert-inbox-intake.yml` + `[new] scripts/alert_inbox_store.sh`
   — validate payload (schema, sizes, `source_repo` in
   `consumer_repos.json` or this repo), write the record with the Contents
   API using `GITHUB_TOKEN` (`contents: write`), retry once on 409; create
   `ai/alert-inbox` from an empty root commit if missing.
5. `.github/ai/label_contract.v1.json` — add `ai:alert-triage`,
   `ai:alert-self-fix`.
6. Tests: `tests/test_tg_helpers_alert_inbox.py` (routing table, fail-open,
   off-switch byte-identity), `tests/test_alert_inbox_store.py`.
7. README variables/config rows, `agents.d/` fragment (stable log prefix
   `ALERT_INBOX`), `changelog.d/` fragment.

### Phase 2
1. `[new] .github/workflows/alert-triage.yml` — cron `17 * * * *`,
   `workflow_dispatch`, `concurrency: alert-triage` (no cancel), Claude pool
   token step, `id-token: write`.
2. `[new] scripts/alert_triage.py` — read records, group, ledger, dedup
   against the two list calls; write the model input files.
3. `[new] prompts/mode-alert-triage.txt` — investigate-issue procedure,
   untrusted-data rules, JSON output, RECOMMENDED-only decisions.
4. `.github/ai/claude_engine.json` — `ALERT_TRIAGE` role.
5. `[new] scripts/alert_triage_route.sh` — routing table, escalation via
   the direct sender, one commit for markers + ledger.
6. `scripts/workflow_failure_heal_intake.sh` + `workflow-failure-heal-intake.yml`
   — accept kind `alert_triage` (self-repo sender only), add the
   `ai:alert-triage` label, pass the diagnosis as evidence.
7. Tests: classification fixtures, routing, ledger cap, idempotency (a
   second run on the same records does nothing), dry-run, intake kind.
8. README, `agents.d/` (`ALERT_TRIAGE` prefix), `changelog.d/`.

### Phase 3
1. `[new] .github/workflows/alert-self-fix.yml` — dispatch job (fix + PR +
   auto-merge) and cron job (break-glass scan), separate concurrency groups.
2. `[new] scripts/alert_self_fix.sh` — pinned checkout, implement role,
   tests, PR, body lint, auto-merge.
3. `[new] scripts/alert_self_fix_break_glass.sh` + `[new] prompts/mode-alert-break-glass-review.txt`
   — the six conditions; posts the status with `statuses: write`; direct
   Telegram notice.
4. `.github/ai/claude_engine.json` — `ALERT_BREAK_GLASS_REVIEWER` role.
5. Tests: each condition true/false, `failure` never overridden, foreign
   commit blocks, daily cap.
6. README, `agents.d/` (`ALERT_SELF_FIX`, `ALERT_BREAK_GLASS` prefixes),
   `changelog.d/`.

### Phase 4
1. `.github/workflows/clarify.yml` (standalone auto-decide step, ~L2051–2130)
   and `.github/workflows/orchestrate_clarify_respond.yml` (standalone gate) —
   label check, RECOMMENDED-only, alert suppression; log
   `STANDALONE_AUTO_DECIDE ... reason=alert_triage_recommended_only`.
2. Tests, README row update, `changelog.d/`.

### Phase 5
1. `[new] scripts/alert_triage_watchdog.sh` + one step in
   `.github/workflows/workflow-log-analysis.yml`.
2. Tests, README, `agents.d/` (`ALERT_TRIAGE_WATCHDOG`), `changelog.d/`.

### Phase 6
1. `scripts/alert_triage.py`: fleet grouping over the inbox window and the
   `release_regression` / `fleet_environmental` rule; one tag-refs read and
   one release read per run.
2. `scripts/alert_triage_route.sh`: the two rows, the 24-hour rollback and
   hotfix caps in the ledger, and the stable-hold check (one contents read of
   `ai/stable-hold`, from #9342).
3. `.github/workflows/alert-self-fix.yml` + `scripts/alert_self_fix.sh`:
   `stable_hotfix` input, cherry-pick PR to `stable`, release-gate dispatch.
4. `.github/ai/label_contract.v1.json`: `ai:stable-hotfix`.
5. Tests, README rows for the four variables, `agents.d/` (log prefix
   `ALERT_TRIAGE_FLEET outcome=release_regression|fleet_environmental`),
   `changelog.d/`.

## Files & Modules

- `[new] .github/ai/alert_inbox.json`
- `[new] .github/workflows/alert-inbox-intake.yml`
- `[new] .github/workflows/alert-triage.yml`
- `[new] .github/workflows/alert-self-fix.yml`
- `[new] scripts/alert_inbox_capture.sh`, `scripts/alert_inbox_store.sh`,
  `scripts/alert_triage.py`, `scripts/alert_triage_route.sh`,
  `scripts/alert_self_fix.sh`, `scripts/alert_self_fix_break_glass.sh`,
  `scripts/alert_triage_watchdog.sh`
- `[new] prompts/mode-alert-triage.txt`, `prompts/mode-alert-break-glass-review.txt`
- `scripts/tg_helpers.sh`
- `scripts/workflow_failure_heal_intake.sh`, `.github/workflows/workflow-failure-heal-intake.yml`
- `.github/workflows/clarify.yml`, `.github/workflows/orchestrate_clarify_respond.yml`
- `.github/workflows/workflow-log-analysis.yml`
- `.github/ai/claude_engine.json`, `.github/ai/label_contract.v1.json`
- `README.md` (variable and config rows)
- `[new] tests/test_*` per phase
- `[new] changelog.d/*`, `agents.d/*` per phase

## Data Model / Index Changes

None. No MongoDB collection is read or written.

## Tests

- **Unit:** routing table and fail-open in `tg_send_msg`; payload validation;
  grouping and fingerprint normalisation; ledger cap; routing rows; each
  break-glass condition; clarify label path; watchdog thresholds.
- **Fleet fixtures (Phase 6):** the 2026-11-10/11 outage replayed (3+
  consumers, failure also on the previous release → `fleet_environmental`);
  a synthetic release regression (all on the new SHA, first seen after
  `published_at` → `release_regression`); two repos only (no fleet); a
  lagging consumer (→ `fleet_environmental`).
- **Integration (Actions, dry run):** phase 2 ships with
  `ALERT_TRIAGE_DRY_RUN=true` by default; the first week of runs logs
  decisions against real alerts without dispatching, and the operator compares
  them with what they would have done.
- **Regression:** existing `tg_helpers` tests and heal-intake tests pass
  unchanged; with both inbox switches off, `tg_send_msg` behaviour is
  byte-identical.

## Risks & Mitigations

- **Alerts go silent if triage breaks while routing is on.** Capture failure
  sends to Telegram; CRITICAL is always sent; the daily watchdog pages on a
  stale run or inbox. ACCEPTED — up to 24 hours before the watchdog notices a
  dead triage job.
- **Up to one hour of delay for WARNING/ERROR alerts.** ACCEPTED (Q9).
- **Prompt injection through alert text or logs.** Model calls run without
  credentials; outputs are JSON parsed by the host; the host, not the model,
  decides `pipeline_blocked` overlap and every dispatch.
- **Break-glass merges a bad change onto `main`.** Six conditions including an
  independent review, the full test suite, a path allowlist and denylist, a
  daily cap, and never overriding a review `failure`. Consumers only get it
  after the `@stable` release gate. Each pass sends a Telegram notice.
- **Fix loops (heal intake alerting about itself, repeated failed fixes).**
  Self-sent alerts bypass capture; heal-intake and triage alerts are
  `pipeline_blocked`, never re-sent to the intake; 2 attempts per fingerprint
  per 7 days, then escalate.
- **Pool exhaustion or broker outage.** Codex fallback through `ai_engine.sh`.
  ACCEPTED — diagnoses during a pool outage use codex.
- **Contents API races on the inbox branch.** Unique paths; one retry on 409;
  a failure falls back to Telegram.
- **Consumer steps whose `GH_TOKEN` cannot dispatch upstream.** Capture fails
  and the alert is sent to Telegram as today. ACCEPTED — pending the phase-1
  dry run, which logs `ALERT_INBOX capture outcome=failed` per repo so gaps
  can be fixed one by one.
- **Workflow runs per alert.** One short `alert-inbox-intake` run per captured
  alert. ACCEPTED — WARNING+ volume is low by design.
- **An automatic rollback for a failure the release did not cause.** Three
  conditions must hold (current SHA only, onset after the release, never on
  the previous release), it goes one step back, at most once per 24 hours,
  and every rollback sends a CRITICAL notice. The switch stays off until a
  `dry_run` rollback has been rehearsed.
- **A stable hotfix ships a bad cherry-pick.** The fix already merged on
  `main` through review (or break-glass), conflicts escalate, and the full
  release gate runs before consumers see it.

## Rollout

1. Merge phases in any order; everything stays off.
2. Set `capture_enabled: true` (routing still off) and release to `@stable`.
   Watch the inbox fill for a few days alongside normal Telegram alerts.
3. Set `ALERT_TRIAGE_ENABLED=true` with `ALERT_TRIAGE_DRY_RUN=true`; review
   the logged decisions.
4. Turn off dry run. Then `ALERT_TRIAGE_SELF_FIX_ENABLED=true`, then
   `ALERT_TRIAGE_BREAK_GLASS_ENABLED=true`.
5. Set `route_warning_error_to_inbox: true` and release to `@stable`. From
   here WARNING/ERROR only reach Telegram when unfixable.
6. After step 4, set `ALERT_TRIAGE_STABLE_HOTFIX_ENABLED=true`, then
   `ALERT_TRIAGE_AUTO_ROLLBACK_ENABLED=true` once one `rollback-stable.yml`
   `dry_run=true` dispatch has been checked.

Rollback at any step: flip the switch back (repo variable, or one PR to
`alert_inbox.json` + release).

## References

- `scripts/tg_helpers.sh` (`tg_send_msg`, level convention)
- `.claude/commands/investigate-issue.md`
- `scripts/workflow_failure_heal_intake.sh`, README "Workflow Failure Heal"
- `docs/plans/replace-claude-sessions-with-cli-engine-plan.md`
- `scripts/claude_pool_token.sh`, `scripts/ai_engine.sh`, `.github/ai/claude_engine.json`
- `scripts/review_head_gate.sh`, README `REVIEW_HEAD_GATE_STATUS_ENABLED`
- README `STANDALONE_AUTO_DECIDE_ENABLED`, `STANDALONE_CLARIFY_RESPOND_ENABLED`

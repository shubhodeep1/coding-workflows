# Claude capacity wait: defer and resume instead of the codex fallback

## Summary

When every Claude pool account is at its usage gate (95%, `gate_utilization`
0.95 since PR #7176), or every account hits its usage limit during a run,
Claude work stops falling back to codex. Instead it **waits**:

- the job stops without running codex and records what was waiting;
- one alert says the pool is exhausted and work is waiting;
- once an hour the poller re-probes the pool, and an hourly alert lists what
  is still waiting and when the earliest account resets;
- when an account drops back under the gate, the poller resumes every waiting
  item and sends one "resumed N items" alert.

Failures that waiting cannot fix (broker down, rejected token, no isolation,
crash) keep #6667's behaviour: refused, exit 76, heal report.

## Context

Requested in an interactive session on 2026-10-11 (PR #7176). The answers
that shape this plan:

| Q | Answer |
|---|---|
| Q1 | Gate 0.9 → 0.95; the hourly near-cap alert follows the same key (shipped in PR #7176). |
| Q2 | Wait with no time cap, until an account is under the gate. |
| Q3 | Off switch `CLAUDE_POOL_CAPACITY_WAIT_ENABLED` (default `true`); `false` restores the codex fallback. |
| Q4 A | Defer and resume: the job exits, the poller re-dispatches later. No in-job sleeping. |
| Q5 A / Q12 A | Every role on the Claude engine waits, including the roles #6667 moves onto Claude. |
| Q6 B | The review round waits for the Claude panel slot too: no `skipped_pool` while waiting. |
| Q7 A | Triggers: `all_gated` at job start **and** `all_usage_limit` mid-run. Other reasons keep the refused fallback. |
| Q8 A | Alerts extend the existing hourly `CLAUDE_POOL_HEALTH` message; one "resumed" alert on recovery. |
| Q10 A | Build after integration PR #6667 (project #6664) merges, on top of its capacity class. |
| Q11 B | Capacity tiers plan moved to core 0.95 / standard 0.85 / utility 0.75. |

**What #6667 provides** (branch `orchestrator/project-6664`, head `244c4c49`):

- `scripts/ai_engine.sh`:
  - `ai_engine_fallback_class <reason>` returns `capacity` for `all_gated` and
    `all_usage_limit`, else `non_capacity`;
  - `ai_engine_fallback` sets `AI_ENGINE_FALLBACK_EXIT`. It is 75 (run codex)
    for capacity, or under `AI_ENGINE_FALLBACK_POLICY=always`. It is 76
    (`::error::AI_ENGINE_FALLBACK_REFUSED`, fail closed) for every other
    reason. Each fallback appends one record to
    `AI_ENGINE_FALLBACK_RECORDS_FILE`;
  - `ai_engine_no_account_reason` tells `all_gated` apart from
    `no_credential`, using `CLAUDE_POOL_REASON` or
    `${RUNNER_TEMP}/claude-pool-reason`;
  - `claude_run` returns 75 with `all_usage_limit` when every account hit
    its limit mid-run;
  - `claude_run_selected` wraps engine choice plus the codex fallback.
- `scripts/ai_engine_fallback_report.sh` is the last `if: always()` step of
  clarify, plan, orchestrate, orchestrate_clarify_respond, orchestrate_poll,
  security-audit and validate. It turns refused records into heal payloads
  and a Telegram note.
- About 30 roles default to Claude. Pool steps are added to validate,
  validation-refresh, check_failure_triage, issue_pr_status,
  workflow-failure-heal-intake and workflow-log-analysis.

**What exists on `main` and is reused:**

- `scripts/provider_outage.py`:
  - the `kind=capacity` tracker issue (label `ai:provider-outage`, title
    "AI Claude Pool Capacity");
  - `open` and `close`;
  - `resume()`, which dispatches review workflows for PRs whose newest
    trusted failure marker matches;
  - the `GitHub` client, which tests replace with a `FakeGitHub`.
- `scripts/claude_pool_health_alert.sh` and `claude_engine.py pool-health`:
  the hourly window is the poll cycle whose UTC minute is below
  `CLAUDE_POOL_HEALTH_WINDOW_MINUTES`.
- `orchestrate_poll.yml`:
  - runs every 5 minutes;
  - holds `id-token: write`, `actions: write` and `GH_PAT` from its wrappers;
  - already runs the pool step and the hourly alert.
- `replay_failed_reclarify_commands` (`scripts/orchestrate_poll_process.sh`):
  - re-posts a stored trusted command with an `[auto-requeued-by-poller]`
    tag;
  - is the pattern for stage resume.
- `orchestrate_lib.py`:
  - `PHASE_LABELS_PRIORITY`;
  - `DEDICATED_HANDLER_PHASES`, the phases stall recovery leaves alone.
- Review/autofix failure markers:
  - `<!-- review-autofix-failure:v1 ... reason=... -->`;
  - the `provider_unavailable` exclusions that keep a pause out of the
    identical-failure cap, labels and heal reports.

## Automation & wiring (CLAUDE.md §18.E)

- **Scripts:** no new standalone script. The plan extends these:
  - `scripts/ai_engine.sh`
  - `scripts/ai_engine_fallback_report.sh`
  - `scripts/provider_outage.py` (new subcommand `capacity-resume`)
  - `scripts/claude_engine.py` (`pool-health` gains wait fields)
  - `scripts/claude_pool_health_alert.sh`
  - `scripts/orchestrate_poll_process.sh`
  - `scripts/orchestrate_lib.py`
  - the role call sites listed under Approach

  The one new file is a `review_autofix` step script required by §27
  (`scripts/review_autofix_step_claude_capacity_wait.sh`). It is wiring, not
  an operator script.
- **Scheduler entry point:** `.github/workflows/orchestrate_poll.yml`, run by
  `internal-orchestrate-poll.yml` here and by
  `workflow-templates/ai-orchestrate-poll.yml` in consumers (cron
  `*/5 * * * *`). It runs the hourly re-probe and the resume pass. Deferrals
  are recorded by the deferring job itself.
- **Supervisor:** none. The 5-minute poll cron already exists, and the
  hourly cadence is a minute window inside it.
- **DB work:** none. State lives in GitHub labels, marker comments and the
  tracker issue.
  - Gate: the capacity tracker is one open issue, so a second `open` reuses
    it.
  - Gate: wait markers are idempotent per (item, run).
  - Gate: resume records a `resumed=` line in the tracker body before it
    re-dispatches, so a partial pass continues instead of repeating.
- **Removal registry (`docs/scripts-pending-removal.md`):** no entry, because
  no single-use or supervisor script is added.

## Goals

- **G1. Wait, don't fall back.** With `CLAUDE_POOL_CAPACITY_WAIT_ENABLED`
  unset or `true`, `ai_engine_fallback` maps the `capacity` class to a new
  exit 80 (`_AI_ENGINE_EXIT_WAIT`, `AI_ENGINE_FALLBACK_EXIT=80`).
  - It logs `AI_ENGINE_CAPACITY_WAIT role= reason=all_gated|all_usage_limit`.
  - It appends a record with `action=wait`.
  - With `false`, capacity gives 75 (codex), as on #6667.
  - `AI_ENGINE_FALLBACK_POLICY=always` still forces 75 for everything.
  - Non-capacity reasons are unchanged (76).
- **G2. Every caller stops cleanly on 80.** On exit 80 every call site
  neither runs codex nor counts a failure. The job:
  - sets `CLAUDE_CAPACITY_WAIT=true` in `GITHUB_ENV`;
  - skips its remaining model and output steps;
  - ends green, except review/autofix (see D4).
- **G3. Waits are recorded where resume can find them.**
  - On the work item: label `ai:claude-capacity-wait` plus a trusted
    marker.
  - On the capacity tracker: one marker comment per waiting item.
  - The first wait opens or reuses the tracker and sends the one "pool
    exhausted, work is waiting" alert.
- **G4. Stall recovery leaves waiting items alone.**
  `ai:claude-capacity-wait` joins `DEDICATED_HANDLER_PHASES` and
  `PHASE_LABELS_PRIORITY`. A clarify waiting 3 hours is not auto-answered,
  and an implement is not re-approved.
- **G5. One hourly re-probe.**
  - While the tracker is open, the poller probes the pool only in the hourly
    window.
  - Between windows, every Claude role in the poller defers without probing.
  - Jobs started by events (PR push, new issue) still probe on their own and
    defer if gated.
- **G6. Resume everything once.** In the hourly window, if the pool has a
  usable account, `provider_outage.py capacity-resume` does the following:
  - re-dispatches every waiting item, up to
    `PROVIDER_OUTAGE_RESUME_MAX_DISPATCH` (50) per pass;
  - removes the labels;
  - closes the tracker;
  - sends one "resumed N items" alert.
- **G7. Hourly alert while waiting.** The `CLAUDE_POOL_HEALTH` message gains
  a waiting section:
  - `N item(s) waiting`;
  - up to 10 links;
  - `earliest reset <YYYY-MM-DD HH:MM UTC>`.

  It is sent every hour the tracker is open, whether or not any single
  account changed.

## Non-goals

- Sleeping inside a job (Q4 B was rejected).
- A maximum wait or a timed codex fallback (Q2).
- Changing what counts as capacity. It stays `all_gated` plus
  `all_usage_limit`, as on #6667.
- Changing the gate value, the tiers, or the broker.
- Waiting on non-capacity failures.

## Constraints

- **Precondition: build only after #6667 merges into `main`.** Phase 0
  preflight: `grep -q '_AI_ENGINE_EXIT_REFUSED' scripts/ai_engine.sh` and
  `grep -q 'ai_engine_fallback_class' scripts/ai_engine.sh` must succeed. If
  they fail, stop and report; do not rebuild #6667's plumbing.
- **§6 naming.** Nothing is renamed. New identifiers (all checked unused on
  `main` and the #6667 branch on 2026-10-11):

  | Kind | Name |
  |---|---|
  | env var | `CLAUDE_POOL_CAPACITY_WAIT_ENABLED` (repo var, default `true`) |
  | job env flag | `CLAUDE_CAPACITY_WAIT` |
  | exit code | 80, constant `_AI_ENGINE_EXIT_WAIT` |
  | label | `ai:claude-capacity-wait` |
  | item marker | `<!-- ai:claude-capacity-wait:v1 stage=<s> source=<comment id\|none> run=<id> role=<ROLE> -->` |
  | tracker marker | `<!-- ai:claude-capacity-wait-item:v1 kind=<pr\|issue\|run> item=<n\|none> workflow=<file> run=<id> role=<ROLE> -->` |
  | failure reason | `claude_capacity` (review/autofix marker) |
  | replay tag | `[auto-requeued-by-capacity-wait]` |
  | log prefix | `CLAUDE_CAPACITY_WAIT` |
  | subcommand | `provider_outage.py capacity-resume` |
- **§4.** `CLAUDE_POOL_CAPACITY_WAIT_ENABLED` defaults to `true` in every
  reader.
- **§15.** The poller's tracker lookup is one
  `issues?labels=ai:provider-outage&state=open` call. That is the same call
  `find_trackers` makes, cached for the cycle. Resume batches the PR reads
  through the existing `pr_page` GraphQL helper.
- **§27.** `review_autofix.yml` is about 472 KB on #6667. New review/autofix
  logic goes into `scripts/review_autofix_step_*.sh`. The workflow file must
  not grow by more than about 2 KB. Check with `wc -c`.
- **§19.** PR bodies reference project #6664 only with `Refs`.

## Approach

### 1. The engine (`scripts/ai_engine.sh`)

- **`ai_engine_fallback`.** When the class is `capacity` and
  `CLAUDE_POOL_CAPACITY_WAIT_ENABLED` is not `false` and the policy is not
  `always`:
  - set `AI_ENGINE_FALLBACK_EXIT=80`;
  - log `AI_ENGINE_CAPACITY_WAIT role= reason=`;
  - append the record with `action=wait`;
  - send no codex-fallback Telegram note.
- **`claude_run`** returns `AI_ENGINE_FALLBACK_EXIT` as today. A capacity
  outcome therefore returns 80.
- **`claude_run_selected`** returns 80 as is and does not run the codex
  command.
- **The two sandbox runners** (`scripts/review_untrusted_sandbox.sh` and
  `scripts/clarify_isolated_run.sh`) write their own records on #6667 with
  `rec_policy=capacity`. They gain the same mapping: on capacity, exit 80
  with an `action=wait` record.

### 2. Call sites: 80 means stop, not codex

Every place that treats 75 as "run codex" gains an 80 branch. The branch
skips the codex path, writes `CLAUDE_CAPACITY_WAIT=true` to `GITHUB_ENV`
and exits the step 0. A test per site asserts that codex is never invoked
after 80.

| Workflow / job | Call sites (paths on #6667) | How the rest of the job is skipped |
|---|---|---|
| clarify | `clarify.yml` "Run Codex" → `clarify_isolated_run.sh ... CLARIFY` | later steps gated on `env.CLAUDE_CAPACITY_WAIT != 'true'` |
| plan | `run_plan_codex.sh` (`claude_run PLAN`, heal route `clarify_isolated_run.sh`) | existing `SKIP_PLAN=true` plus the flag |
| implement | `codex_thread_reuse.sh` (IMPLEMENT, IMPLEMENT_REPAIR), `heal_isolated_implement.sh`, `implement_diagnose_post_codex_failure.sh` | existing `SKIP_IMPLEMENT=true`, so "Restore awaiting approval label on skipped implementation" runs; diagnose never runs on a wait |
| orchestrate (decomposer) | `orchestrate.yml` `claude_run ORCHESTRATE` | flag; no tracking issue exists yet, so the wait is run-scoped |
| orchestrate_clarify_respond | three `clarify_isolated_run.sh ... CLARIFY_RESPOND` sites | flag; standalone mode must not post the RECOMMENDED fallback on a wait |
| unblock_judge | `unblock_judge.sh` | flag; no `invalid_verdict` marker on a wait |
| orchestrate_poll judges | `poller_judge_isolated` in `orchestrate_poll_process.sh` (WAVE, STALL, INTEGRATION, SECURITY, RB judges) | defer to the next eligible cycle through a new return path that does **not** call `_record_judge_isolation_failure`, so waiting never counts toward `JUDGE_ISOLATION_MAX_FAILURES` or adds `ai:needs-human` |
| security audit (poller pass and `security-audit.yml`) | `security_audit_try_claude` | the poller pass reports `deferred`, not `engine_unavailable`; the weekly run is run-scoped |
| roles added by #6667 | validate, validation-refresh, check_failure_triage, issue_pr_status, workflow-failure-heal-intake, workflow-log-analysis, retro fan-out, `activation_verify.sh`, `self_heal_validation.sh`, `validate_process.sh` | flag; run-scoped unless the job is tied to a tracking issue (validate), in which case the issue gets the label |
| review/autofix | see §5 | job fails with `reason=claude_capacity` (D4) |

### 3. Recording a wait (`scripts/ai_engine_fallback_report.sh`)

The report step already runs last (`if: always()`) in most of these jobs.
It is added to the ones where it is missing: implement, unblock_judge, and
the #6667 workflows that lack it. For every `action=wait` record it does the
following:

1. **The item.** When the job has a work item (issue number in the job
   context), add `ai:claude-capacity-wait` and post the item marker. The
   marker's `source=` is the trusted comment id that triggered the stage
   (`/reclarify`, `/answer`, `/approved`, "Clarification required"), or
   `none` for an `issues: opened` clarify.
2. **The tracker.** Call `provider_outage.py open --kind capacity --provider
   claude-pool --status <reason>`, then append one tracker marker per item
   (deduplicated on `item`+`run`).
   - `kind=issue` for stages;
   - `kind=pr` for review/autofix;
   - `kind=run` for run-scoped jobs (decomposer, weekly audit, #6667
     utility jobs).
3. **The alert.** Only the call that opened the tracker sends it: "Claude
   pool exhausted in `<repo>`: work is waiting, not falling back. Hourly
   re-check; tracker #N."

Refused (non-capacity) records keep their existing heal-report path.

### 4. Stall recovery (`scripts/orchestrate_lib.py`)

- Add `ai:claude-capacity-wait` to `PHASE_LABELS_PRIORITY` (right after
  `ai:needs-human`) and to `DEDICATED_HANDLER_PHASES`.
- Unlike `ai:needs-human`, it is cleared by automation (the resume pass),
  never by a human.
- When the label is removed, `status_since_ts` resets so the stage gets a
  fresh stall window.

### 5. Review/autofix

- **Deferring at the start.** A new step "Defer review on Claude capacity"
  goes right after "Record Claude pool capacity". Its body is
  `scripts/review_autofix_step_claude_capacity_wait.sh`, registered in
  `REQUIRED_BOOTSTRAP_SCRIPTS` and `tests/review_autofix_step_scripts.py`.
  - It fires when `steps.claude_pool.outputs.reason == 'all_gated'`, wait is
    enabled, and any `AI_ENGINE_RESOLVED_*` role (or the panel slot) is
    `claude`.
  - It pre-sets `AUTOFIX_FAILURE_REASON=claude_capacity` and exits 1, so
    every `success()` step skips (D4).
- **Mid-run exhaustion.**
  - Editor (`review_apply_fixes.sh`), consolidator (`review_consolidate.sh`),
    RB judge (`review_rb_judge.sh`), conflict resolver
    (`review_conflict_resolve.sh`): on 80, write
    `AUTOFIX_FAILURE_REASON=claude_capacity` and fail the step.
  - Panel slot (`review_run_reviewers.sh` `run_pool_reviewer`, Q6 B): on 80
    the slot status is `capacity_wait`, not `skipped_pool`. Any
    `capacity_wait` slot fails the reviewer step with `claude_capacity`. The
    Haiku retry is skipped on a wait, because the account pool is shared.
- **`claude_capacity` joins `provider_unavailable` in every pause
  exclusion**, through one shared constant `PAUSE_FAILURE_REASONS` in
  `scripts/workflow_failure_heal.py`, imported by both Python modules.
  - `workflow_failure_heal.py`:
    - `count_identical_failures`;
    - the host-only scan;
    - the markerless-pause skip;
    - `PROVIDER_UNAVAILABLE_COMMENT_MARKER` gains a capacity twin.
  - `provider_outage.py`: `resume_decision`, `label_marker_still_valid`,
    `superseded_after`.
  - `workflow_failure_heal_autofix_report.sh`.
  - `workflow_failure_heal_intake.sh`.
  - The four `review_autofix.yml` `!= 'provider_unavailable'` clauses (the
    linked-issue label, the forced poll, Telegram failure, the heal report)
    become `!contains(fromJSON('["provider_unavailable","claude_capacity"]'), env.AUTOFIX_FAILURE_REASON)`.
- **Run-end steps on a capacity wait.**
  - "Post review-blocked comment on PR (workflow failure)" posts a
    capacity-wait body ("**AI review paused — Claude pool at capacity**;
    resumes automatically") with the failure marker.
  - "Record model-provider outage" does **not** run, so no OpenRouter
    outage tracker is opened.
  - The capacity tracker is recorded by the new step's script.
- **"Record Claude pool capacity" stops closing the tracker** while it holds
  unresumed wait markers. Only `capacity-resume` closes it (D5).

### 6. Hourly re-probe and resume (`orchestrate_poll.yml`)

- **`find_tracking`** runs one extra `issues?labels=ai:provider-outage`
  listing, cached for the cycle, and outputs `capacity_wait=true|false`.
  The job no longer exits early when there are no tracking issues but
  `capacity_wait=true`. Only the capacity steps then run.
- **Pool step `if:`.** The step runs when
  `(has_work && any_claude) || capacity_wait`. When `capacity_wait` is true,
  it runs only in the hourly window (UTC minute below
  `CLAUDE_POOL_HEALTH_WINDOW_MINUTES`). A new pure-shell step before it
  computes `hourly_window`.
- **Outside the window** with `capacity_wait=true`, "Process each tracking
  issue" gets `CLAUDE_POOL_REASON=all_gated`, so every poller Claude role
  takes the wait path of §2 without probing.
- **New step "Resume Claude capacity waits"**, after the pool step (in the
  hourly window and when `capacity_wait`): runs `provider_outage.py
  capacity-resume --pool-available <available>`. When the pool is
  available it resumes every tracker marker:
  - `kind=pr`: the existing `resume()` path, with the reason filter set to
    `claude_capacity`. It dispatches `internal-review.yml` here and
    `ai-review.yml` in consumers (`PROVIDER_OUTAGE_REVIEW_WORKFLOW`).
  - `kind=issue`:
    - remove `ai:claude-capacity-wait`;
    - re-post the marker's `source` comment body with
      `[auto-requeued-by-capacity-wait]` appended, or `/reclarify
      [auto-requeued-by-capacity-wait]` when `source=none`.
    - This generalizes `replay_failed_reclarify_commands`. The trust gates
      of clarify, plan, implement and orchestrate_clarify_respond accept the
      new tag from the PAT bot, exactly as they accept
      `[auto-requeued-by-poller]` / `[auto-approved-by-plan]` today.
  - `kind=run`: `POST repos/<repo>/actions/runs/<id>/rerun`. This is a full
    re-run with the original inputs and event; GitHub allows it for 30
    days. An older run is reported in the resumed alert as "not re-run
    (older than 30 days)".
  - Each resumed marker is recorded in a `<!-- ai:claude-capacity-wait-resume:v1 done=<csv> -->`
    line in the tracker body before the next one starts. A partial pass
    (cap reached, API error) continues next hour.
  - When every marker is done, the pass closes the tracker and writes the
    alert "Claude pool capacity back in `<repo>`: resumed N item(s)
    (P PR(s), I issue(s), R run(s)); tracker #N closed."
- **Pool still `all_gated`:** nothing is resumed. The hourly alert (§7)
  carries the status.

### 7. Hourly alert (`claude_pool_health_alert.sh`, `claude_engine.py pool-health`)

- `pool_health` gains `next_usable_at`: the minimum over gated accounts of
  `_usable_at(probe, gate)`, rendered by `_format_reset`.
- The step receives `CLAUDE_CAPACITY_WAIT_ITEMS`, a JSON list of
  `{kind, item, workflow, run}` from the cached tracker read; no extra API
  call.
- While the list is non-empty, the hourly message:
  - opens with `Claude pool exhausted: N item(s) waiting since <opened_at>;
    earliest reset <T>.`;
  - lists up to 10 items as links, plus `… and K more`;
  - is followed by the existing per-account lines.
- It is sent every hourly window while waiting, even when the pool is
  otherwise unchanged.
- With no waiting items the message is unchanged.

### 8. Text that goes stale

These texts say "roles fall back to codex/OpenCode" and must change:

- `provider_outage.py` `tracker_body` / `open_alert_text` for
  `kind=capacity`;
- `ai_engine.sh`'s dedupe comment;
- the `review_autofix.yml` "Record Claude pool capacity" comment;
- README "Accounts and fallback" / "Token broker" / "Near-cap alert" / the
  security audit "Engine." paragraph;
- the `agents.md` capacity-tracker sentence. This one is an in-place
  correction (§30.B); new text goes in an `agents.d/` fragment.

## Decisions

### D1 — Defer and resume, not wait in the job (Q4 A)

- **Chosen:** the job exits and the hourly poll pass re-dispatches the work.
- **Alternatives considered:** sleep and re-probe inside the job (Q4 B).
- **Why:** waiting inside a job costs runner minutes the whole time. GitHub
  Actions also kills any job at 6 hours, while the 7-day window can keep the
  pool full for days.

### D2 — New exit code 80, not a new meaning of 75 or 76

- **Chosen:** a new public exit 80 (`_AI_ENGINE_EXIT_WAIT`).
- **Alternatives considered:** reuse 75 with a wait flag in the environment;
  reuse 76 (refused).
- **Why:** many callers already branch on 75 (run codex) and 76 (fail). A
  separate code makes every site opt in explicitly, and an unported site
  fails closed instead of silently running codex. Codes 77–79 are already
  used locally (77 in judge deferral and the resolver, 78 inside the
  `claude_run` subshell, 79 in `review_apply_fixes.sh` on #6667).

### D3 — State in labels, markers and the existing capacity tracker

- **Chosen:** the `ai:claude-capacity-wait` label plus item and tracker
  markers on the existing `kind=capacity` tracker.
- **Alternatives considered:** a new tracker kind; a state file in the
  repository; Actions artifacts or caches.
- **Why:** the capacity tracker already exists and dedupes its alert. Labels
  are what stall detection reads. Markers are authored by the trusted PAT
  login and survive restarts; the same trust rule as
  `ai:provider-outage-*`.

### D4 — Review/autofix fails the job; every other job ends green

- **Chosen:** review/autofix fails with `reason=claude_capacity`; every other
  job ends green with `CLAUDE_CAPACITY_WAIT=true`.
- **Alternatives considered:** a soft skip flag in review/autofix, which
  needs about 70 `if:` edits; failing every job, which fires failure
  comments and heal reports.
- **Why:** review/autofix has about 12 unguarded steps after the pool step,
  and the file has about 8 KB of room under the §27 guard. Failing with a
  named pause reason skips them all through `success()`. It reuses the
  marker that `resume()` already understands. Every other workflow already
  has a skip flag or few remaining steps, so it can end green and avoid
  failure comments and heal reports.

### D5 — Only the hourly resume pass closes the tracker

- **Chosen:** `capacity-resume` closes the tracker once every marker is
  resumed; `close --kind capacity` is a no-op while markers are pending.
- **Alternatives considered:** keep closing on the first run that gets an
  account, and resume from that run.
- **Why:** today any review run that gets an account closes the capacity
  tracker. With waiting items that would orphan their markers. The resume
  pass is the one place that knows every waiting item.

### D6 — The poller probes hourly while waiting

- **Chosen:** while the tracker is open, the poller probes only in the
  hourly window.
- **Alternatives considered:** probe on every 5-minute cycle and resume
  as soon as an account frees up.
- **Why:** it matches "retry every hour". The 5-minute cycle would
  otherwise probe every account 12 times an hour while every one is known
  to be full. Event-driven jobs still probe on their own, so a freed account
  is used by new work right away. Older waiting work resumes at the next
  hourly window.

## Phases & Merge Strategy

The plan can ship as one PR from an interactive session (Q9 A). For the
orchestrator, each phase is an independently mergeable PR, in this order,
and each phase checks the Phase 0 preflight.

1. **Engine and recording.** The `ai_engine.sh` wait class and exit 80, the
   sandbox runners, the report step's wait branch, the label and both
   markers, `orchestrate_lib.py` hold, and the tracker texts.
   - The off switch defaults to **`false` in this phase only**. A wait with
     no resume pass would strand work.
2. **Call sites.** All rows of the §2 table except review/autofix, plus the
   trust-gate tags.
3. **Review/autofix.** The new step script, the mid-run sites, the panel
   slot, and the pause-reason constant and exclusions.
4. **Hourly pass and alert.** `orchestrate_poll.yml` wiring,
   `capacity-resume`, the `pool-health` wait fields, and the alert text.
   - This phase flips `CLAUDE_POOL_CAPACITY_WAIT_ENABLED` to default
     `true`.

## Tests

| Phase | Tests |
|---|---|
| 1 | `tests/test_ai_engine.py`: capacity gives 80 with an `action=wait` record and no codex note; `false` gives 75; `always` gives 75; non-capacity still 76; mid-run `all_usage_limit` gives 80. `tests/test_review_sandbox_claude_roles.py` and the `clarify_isolated_run.sh` tests: the same mapping. The report step's wait branch (in `tests/test_ai_engine.py`, where #6667 tests `ai_engine_fallback_report.sh`): label, item marker, tracker open and marker, idempotent on re-run. `tests/test_orchestrate_lib.py`: a waiting item is never a stall. |
| 2 | Per site, 80 → no codex invocation, flag set, step exit 0. Extend `tests/test_codex_thread_reuse_core.py`, `test_plan_codex_step_extraction.py`, `test_plan_clarify_blocked_output.py`, `test_heal_isolated_claude_editor.py`, `test_unblock_judge.py`, `test_orchestrator_judges_claude_engine.py` (wait does not bump `judge_isolation_state`), `test_codex_agent_isolation_contract.py`, `test_clarify_respond_standalone.py`, `test_security_audit_workflow_contract.py`. Trust gates accept `[auto-requeued-by-capacity-wait]` only from the PAT login. |
| 3 | `tests/test_review_autofix_provider_outage_contract.py` (new step order and `if:`, capacity body, no OpenRouter outage tracker), `tests/test_review_autofix_step_scripts_contract.py`, `tests/test_workflow_failure_heal.py` (`claude_capacity` not counted toward the cap), `tests/test_review_pool_reviewer.py` (`capacity_wait`, no Haiku retry), `tests/test_workflow_file_size_limit.py`. |
| 4 | `tests/test_provider_outage.py`: `capacity-resume` with `FakeGitHub` for PR dispatch, comment replay, run re-run, partial pass continuing from the resume line, close plus alert only when done, nothing while gated, the 30-day limit. `tests/test_claude_pool_health_alert.py`: waiting section, 10-item cap, earliest reset, hourly while waiting. `tests/test_orchestrate_poll_workflow_contract.py`: no early exit when `capacity_wait`, pool step `if:`, outside-window `CLAUDE_POOL_REASON`. |

Each new test file gets its own `ci.yml` step.

## Risks & Mitigations

- **Release-path utility roles wait too (Q12 A).** With every Claude role
  waiting, a release job whose behavioural smoke or soft-error analysis runs
  on Claude also waits, possibly for days on the 7-day window.
  - Mitigation: the hourly alert names the waiting runs.
  - Mitigation: `CLAUDE_POOL_CAPACITY_WAIT_ENABLED=false` restores the
    codex fallback repo-wide.
- **Utility and standard tiers wait earlier.** If the capacity tiers plan
  lands, a utility role waits once every account passes 75%.
  - That is the tiers' intent, but expect more waiting items with low
    priority. The alert counts them.
- **Comment replay re-runs old answers.** A replayed `/answer` carries the
  answers given before the wait. If a human answered again in the meantime,
  the newest trusted command wins: replay skips a source that is older than
  another trusted stage command on the same issue.
- **Hourly resume adds up to 1 hour of latency** after capacity returns.
  - Accepted (D6): new event-driven work uses the account immediately.
- **Consumers without the poll wrapper never resume.**
  - Phase 0 checks every repo in `.github/ai/consumer_repos.json` for
    `ai-orchestrate-poll.yml`.
  - A repo without it keeps `CLAUDE_POOL_CAPACITY_WAIT_ENABLED=false` until
    seeded.
- **Re-run of a 30-day-old run fails.** It is reported in the resumed alert,
  and the item is listed for a human to dispatch.

## Rollout

- Phases 1–3 ship with the switch defaulting to `false`, so behaviour is
  #6667's.
- Phase 4 flips the default and reaches consumers with `@stable`.
- Rollback, with no code change: set the repo variable
  `CLAUDE_POOL_CAPACITY_WAIT_ENABLED=false`. Items already waiting are still
  resumed by the hourly pass, which runs whenever the tracker is open,
  whatever the switch says.

## References

- PR #7176: gate 0.9 → 0.95 and this plan.
- Integration PR #6667 and project #6664 (Refs): the capacity class, exit 76,
  fallback records.
- `docs/plans/unattended-claude-pipeline-completion-plan.md` (the #6664 plan,
  decision D1).
- `docs/plans/claude-pool-capacity-tiers-remaining-callers-plan.md`: tier
  gates now 0.95 / 0.85 / 0.75.
- `scripts/provider_outage.py` (issue #6633 outage tracker and resume).
- `scripts/claude_pool_health_alert.sh` (issue #6951 hourly alert).

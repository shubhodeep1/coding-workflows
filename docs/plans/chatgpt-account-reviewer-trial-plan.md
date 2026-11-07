# ChatGPT account reviewer trial

## Summary

Run the review panel's GPT slot on a ChatGPT Pro account (`gpt-6-sol`,
`xhigh`) instead of OpenRouter, with the login held in `shubhodeep1/claude-workers`
and served by the `claude-pool-broker` Worker, the same way the Sonnet slot
already runs on the Claude account pool. Record which reviewer raised each
finding so that, after one trial week, we can measure how much Sonnet + GPT
catch together and what only the OpenRouter reviewers catch, before planning
to remove them.

## Context

- The review panel (`REVIEWER_MODELS` in `.github/workflows/review_autofix.yml`)
  has five slots: `minimax/minimax-m3`, `anthropic/claude-sonnet-5.5`,
  `z-ai/glm-5.3-flash`, `google/gemini-3.1-flash-lite`, `openai/gpt-6-luna`.
  The Sonnet slot already runs on the Claude account pool
  (`run_pool_reviewer` in `scripts/review_run_reviewers.sh`, engine role
  `PANEL_REVIEWER`, skipped when the pool is unavailable). The other four go
  through OpenRouter via OpenCode.
- OpenRouter `/api/v1/activity`, 2026-10-07 to 2026-11-06 (account-wide):
  `minimax/minimax-m3` $1,309.83, `google/gemini-3.1-flash-lite` $547.66,
  `openai/gpt-6-luna` $387.88, `z-ai/glm-5.3-flash` $227.68, about $2,473 in
  total. `openai/gpt-6-luna` is shared with the log-summary and summariser
  fallbacks, so not all of its spend is reviews.
- The Claude pool: tokens are secrets in `shubhodeep1/claude-workers`;
  `claude-pool-key-sync.yml` there writes them to the `CLAUDE_POOL_TOKENS`
  secret of `claude-pool-broker` (`tools/claude-pool-broker`, account
  `FT_GAMES_CF`, §24). Jobs fetch them with their GitHub OIDC token through
  `.github/actions/claude-pool-token`; the broker checks repository, workflow
  file (`ALLOWED_WORKFLOW_FILES`) and ref. The broker stores nothing; it serves
  static long-lived `setup-token` tokens.
- A ChatGPT login is not a static token. Codex keeps it in `auth.json`
  (access token, single-use refresh token, `last_refresh`), refreshes it
  when `last_refresh` is older than about 8 days or on a 401, and writes the
  new file back. OpenAI's
  [CI/CD auth guide](https://developers.openai.com/codex/auth/ci-cd-auth)
  supports this as "advanced" and says to run Codex and persist the updated
  `auth.json`, not to call the refresh endpoint directly. Two jobs refreshing
  the same login invalidate each other.
- The consolidator already records attribution: every consolidated issue
  block carries `FLAGGED_BY:` (sanitised against the panel by
  `scripts/review_parse_consolidator.sh`). Raw per-reviewer output is
  uploaded only as the `reviewer-logs-<run>` artifact with
  `retention-days: 7`, and no record joins findings, reviewer status, tier
  and editor outcome. A trial-week analysis needs that record kept longer.
- Review tiers (`classify_review_tier`): diffs of 50 lines or fewer touching no
  protected path get one random reviewer (lite), 200 lines or fewer get four
  (standard), the rest the full panel; draws are seeded by PR number.
- Daily Claude usage report: `scripts/claude_pool_daily_usage.sh` (Telegram,
  wired into `orchestrate_poll.yml`); hourly near-cap alert:
  `scripts/claude_pool_health_alert.sh`.

User decisions (clarification round, 2026-11-07): one ChatGPT Pro account,
expandable to several (Q3 A); `gpt-6-sol` (Q4 A); replaces the OpenRouter
`openai/gpt-6-luna` slot (Q5 A); when the account cannot be used the slot is
skipped, the user is alerted, and ChatGPT usage is added to the daily Claude
usage report (Q6 B); one writer refreshes the login (Q7 A); the analysis uses
the full-panel and standard-tier PRs, and lite / standard tiers always include
the two account reviewers (Q8 A, Q12 A); a durable findings record kept 30
days (Q9 A); a scheduled session check-in runs the analysis about a week after
go-live (Q10 A); the `claude-workers` part is built in the interactive
session, the `coding-workflows` part by the pipeline (Q11 A); the four risks
under Risks & Mitigations are accepted (Q13 A); several phases (Q14 A); a
default-off switch for go-live (Q15 A).

## Goals

1. Review jobs in `coding-workflows` and every consumer repo can run the GPT
   slot on a ChatGPT account, with the job never holding a refresh token.
2. Exactly one process (the `claude-workers` refresh workflow) ever refreshes
   a ChatGPT login; adding an account is one new secret in `claude-workers`.
3. With the switch on, the panel's GPT slot is `gpt-6-sol` at `xhigh` on the
   account; with it off (default), behaviour is unchanged.
4. With the switch on, lite-tier PRs are reviewed by Sonnet + GPT and
   standard-tier PRs by Sonnet + GPT + two seeded-random OpenRouter
   reviewers.
5. When the GPT slot cannot run, it is skipped (no OpenRouter fallback) and
   the user gets a Telegram alert naming the account and reason.
6. Every review run that reaches the consolidator leaves a findings record
   (reviewers that ran and their status, tier, consolidated findings with
   `FLAGGED_BY`, editor outcome) kept for 30 days.
7. The daily Claude pool usage message also shows each ChatGPT account's
   usage and login state.

## Non-goals

- Removing any OpenRouter reviewer. That is planned after the trial analysis.
- Moving other roles (editor, consolidator, summariser) to ChatGPT accounts.
- The trial analysis itself; it runs from an interactive-session check-in and
  produces a report under `analysis/`.
- Changing the consolidator prompt or output format.

## Constraints

- §6: existing identifiers stay (`REVIEWER_MODELS`, `PANEL_REVIEWER`,
  `run_pool_reviewer`, `REVIEW_TIER_*`, `CLAUDE_POOL_*`, artifact names, log
  prefixes). New identifiers below were checked for collisions: no `CHATGPT`
  / `chatgpt` identifier exists in either repo today.
- §4: every new env var / repo variable has a default.
- §5: extend `claude-pool-broker`, the review sandbox and the daily usage
  report; no parallel mechanisms.
- §14: review runs in every repo in `.github/ai/consumer_repos.json` through
  the reusable `review_autofix.yml`; consumers need no change (the broker's
  repo / workflow check already covers them, and the switch is read from a
  file shipped in this repo).
- §15: no new `gh api` calls. Phase 4 writes an artifact from files the run
  already has.
- §18: the refresh runs on a schedule in `claude-workers`; alerts and reports
  ride existing `orchestrate_poll.yml` jobs. No manual scripts.
- §24.C/E: the broker deploy uses `FT_GAMES_CF` from a verified
  default-branch commit after Phase 1 merges; tokens are never logged.
- §27: `review_autofix.yml` must stay under 480,000 bytes; new logic goes in
  `scripts/`.
- Security: ChatGPT `auth.json` content is a credential. It is never logged,
  never written into the repo or artifacts, and is deleted by the action's
  post step, matching `claude-pool-token`.

## Approach

**Credential flow (single writer).**

```
claude-workers secret CHATGPT_POOL_AUTH_<NAME>  (seed auth.json, set once per login)
        │
        ▼  chatgpt-pool-refresh.yml (claude-workers, every 4h, concurrency group)
   GET  broker /v1/chatgpt/refresh/<NAME>  ← current auth.json (+ seed hash)
   run  codex exec (tiny prompt) with CODEX_HOME holding that auth.json
   PUT  broker /v1/chatgpt/refresh/<NAME>  → new auth.json + usage snapshot,
                                              compare-and-swap on generation
        │
        ▼  Durable Object per account in claude-pool-broker
   POST broker /v1/chatgpt (review_autofix.yml / orchestrate_poll.yml jobs, OIDC)
        → one account: auth.json WITHOUT refresh_token, chosen by lowest usage under the gate
```

- The broker stores the current `auth.json` per account in a Durable Object
  (atomic compare-and-swap on a generation counter, so two overlapping
  refreshes cannot both win). The write endpoints accept only the OIDC token
  of `shubhodeep1/claude-workers`'s `chatgpt-pool-refresh.yml` on
  `refs/heads/main`; the read endpoint uses the existing coding-workflows
  check (`ALLOWED_WORKFLOW_FILES`, trusted refs).
- Seeding: the refresh job sends the sha256 of the `CHATGPT_POOL_AUTH_<NAME>`
  secret. When the broker has no record, or the stored seed hash differs (a
  fresh login was saved), the broker takes the secret's file. Otherwise it
  keeps its own newer copy. Re-login is therefore "save a new secret", with no
  code change.
- Review jobs get `auth.json` with `refresh_token` removed, so a 401 inside a
  review cannot rotate the login; the job fails that slot instead. Running the
  refresh every 4 hours keeps `last_refresh` far below Codex's 8-day refresh
  point, so review jobs never need to refresh.
- Usage: the refresh call's `codex exec --json` events are read for the
  account's rate-limit percentages (5-hour and weekly windows) and stored with
  the record (see Risks: pending discovery). The read endpoint skips accounts
  at or above the gate (`GATE`, default 0.9) and returns
  `503 chatgpt_pool_empty` when none is usable.

**Reviewer slot.** New panel entry `chatgpt/gpt-6-sol` (prefix `chatgpt/`
routes to the account path, like `anthropic/claude-*` routes to the Claude
pool). When the switch is on, `scripts/review_run_reviewers.sh` replaces
`REVIEWER_CHATGPT_REPLACES` (default `openai/gpt-6-luna`) in the active panel
with `chatgpt/${REVIEWER_CHATGPT_MODEL}`. The slot runs Codex inside
`scripts/review_untrusted_sandbox.sh` (new engine `chatgpt`, same read-only
profile as the Claude panel reviewer), `xhigh` reasoning, the same reviewer
prompt and output contract as the other slots, so the consolidator sees it as
one more reviewer. A failure or an unusable account gives status
`skipped_pool` with a reason, exactly like the Sonnet slot; there is no
OpenRouter fallback (Q6 B).

**Tiers (switch on only).** Lite: `anthropic/claude-sonnet-5.5` +
`chatgpt/gpt-6-sol`. Standard: those two + two seeded-random draws from the
rest. Full: unchanged. Existing `REVIEW_TIER_LITE_REVIEWER_SLUG` /
`REVIEW_TIER_STANDARD_REVIEWER_SLUGS` overrides still win. Safety floor: if
every account slot in a lite-tier run is skipped, one seeded-random OpenRouter
reviewer runs so no PR is left unreviewed.

**Switch.** `REVIEWER_CHATGPT_SLOT_ENABLED` (repo variable, default empty):
`true` / `false` override per repo; empty reads `reviewer_slot_enabled` from
the new `.github/ai/chatgpt_pool.json` (shipped `false`). Go-live is one
reviewed PR that sets the file to `true`, which reaches every consumer on the
next `@stable` sync.

**Alerts and report.** `orchestrate_poll.yml` already fetches the Claude pool
hourly for the health alert. It also calls a new
`.github/actions/chatgpt-pool-token` in its status-only mode
(`GET /v1/chatgpt/status`: per account generation, `last_refresh`, last refresh
outcome, usage; never a token). `claude_pool_health_alert.sh` adds a ChatGPT
line when an account's last refresh failed, `last_refresh` is older than 12
hours, a login was rejected, or usage is at or above the gate.
`claude_pool_daily_usage.sh send` appends a "ChatGPT accounts" section. The
per-PR skip is visible in the review summary and the
`AI_ENGINE_FALLBACK role=PANEL_REVIEWER_CHATGPT reason=<code> action=skip`
log line; the hourly alert carries the account-level cause, so the alert
cannot flood with one message per PR across 14 repos.

**Findings record.** After the consolidator parse, a new
`scripts/review_findings_record.py` writes `review_findings_record.json`:
repo, PR, run id/attempt, head SHA, round, tier and its reason, each panel
slot's status (`ok`, `failed`, `skipped_pool`, `not_selected`), each
consolidated issue (id, file, lines, severity, classification, `FLAGGED_BY`
list, `MERGED_FROM`), and, when the editor ran, its per-issue disposition
(applied / ignored with reason). It is uploaded as
`reviewer-findings-<run_id>-<run_attempt>` with `retention-days: 30`. No
credential, prompt or untrusted file content beyond what the consolidator
already emits is stored.

Alternatives considered: OpenRouter fallback when the account is unavailable
(rejected in Q6); every review job refreshing with a broker lock (rejected in
Q7, rotation risk); running the GPT slot on OpenCode (Codex is OpenAI's
documented route for ChatGPT login); a separate broker Worker (§5).

## §18.E Automation summary

- **Scripts:** extends existing scripts (`review_run_reviewers.sh`,
  `review_untrusted_sandbox.sh`, `claude_pool_health_alert.sh`,
  `claude_pool_daily_usage.sh`, `claude_engine.py`) and adds
  `scripts/review_findings_record.py` plus `.github/actions/chatgpt-pool-token`,
  each called from an existing workflow step. No manual-invocation script.
- **Entry points:** `.github/workflows/review_autofix.yml` (existing
  `pull_request` / dispatch triggers via `internal-review.yml` and
  `workflow-templates/ai-review.yml`); `.github/workflows/orchestrate_poll.yml`
  (existing 5-minute schedule) for the alert and daily report;
  `shubhodeep1/claude-workers` `.github/workflows/chatgpt-pool-refresh.yml`
  [new] on `schedule` (every 4 hours), `push` to `main` and
  `workflow_dispatch`.
- **Supervisor:** none needed; the refresh is a scheduled workflow.
- **DB work:** none (no MongoDB). The broker's Durable Object uses a
  generation compare-and-swap as its write gate.
- **Removal registry (§18.F):** `chatgpt-pool-refresh.yml` lives in
  `claude-workers`, like `claude-pool-key-sync.yml`, which has no entry; it is
  permanent while ChatGPT accounts are in use. Phase 2 adds an entry to
  `docs/scripts-pending-removal.md` in this repo: type `long-running`,
  removal trigger "when the panel no longer has a `chatgpt/` slot", preflight
  "`REVIEWER_MODELS` and `.github/ai/chatgpt_pool.json` reference no
  `chatgpt/` slot on `main` and `stable`; broker `/v1/chatgpt/status` shows no
  read in 7 days", owner `@shubhodeep1`.

## Phases & Merge Strategy

Every phase is inert until the accounts exist and the switch is on, so the
phases can merge in any order.

1. **Broker ChatGPT store + token action.** `tools/claude-pool-broker`
   (Durable Object, `/v1/chatgpt`, `/v1/chatgpt/refresh/<NAME>`,
   `/v1/chatgpt/status`, tests), `.github/actions/chatgpt-pool-token`
   [new], `tests/test_chatgpt_pool_token.py` [new]. Done: vitest + pytest
   green; with no accounts, `/v1/chatgpt` answers `503 chatgpt_pool_empty` and
   the action outputs `available=false reason=broker_refused_chatgpt_pool_empty`;
   existing `/v1/pool` behaviour and tests unchanged. Rollback: revert; the
   new routes vanish and nothing calls them. Deploy per §24.C after merge.
2. **ChatGPT reviewer slot + tiers + switch.** `scripts/review_run_reviewers.sh`,
   `scripts/review_untrusted_sandbox.sh`, `review_autofix.yml` (env + one
   action step), `.github/ai/chatgpt_pool.json` [new], `scripts/claude_engine.py`
   (role `PANEL_REVIEWER_CHATGPT`), tests, docs. Done: with the switch off the
   panel, tiers and logs are byte-for-byte unchanged (test); with it on and
   the broker unavailable the slot reports `skipped_pool` and the lite-tier
   floor runs one OpenRouter reviewer. Rollback: revert, or set the switch
   `false`.
3. **Findings record.** `scripts/review_findings_record.py` [new], one step +
   one upload in `review_autofix.yml`, tests. Done: a fixture run produces a
   record with `FLAGGED_BY` lists and slot statuses; a failure in the step
   warns and never fails the review (`continue-on-error`). Rollback: revert.
   Works with or without Phase 2 (the GPT slot simply never appears).
4. **ChatGPT alert + daily usage section.** `scripts/claude_pool_health_alert.sh`,
   `scripts/claude_pool_daily_usage.sh`, `scripts/claude_engine.py`
   (`pool-health` / `pool-usage` take an optional ChatGPT status list),
   `orchestrate_poll.yml` (one status-only action call per existing job),
   tests. Done: with no ChatGPT accounts the messages are unchanged; with a
   fixture status list the new section / alert line renders. Rollback: revert.

Built in the interactive session, not by the pipeline (Q11):

- **claude-workers:** `.github/workflows/chatgpt-pool-refresh.yml` [new] and a
  README "ChatGPT pool" section (adding an account with
  `codex login` on a trusted machine with `cli_auth_credentials_store = "file"`,
  saving `auth.json` as `CHATGPT_POOL_AUTH_<NAME>`, failure modes). Inert
  until Phase 1 is deployed (the job logs `broker_route_missing` and exits 0).

Go-live is a separate one-line PR (`reviewer_slot_enabled: true`), opened
only after Phases 1–3, the `claude-workers` workflow and the account secret
are in place and one dispatched review shows the GPT slot `ok`.

## Implementation Steps

### Phase 1

1. `tools/claude-pool-broker/wrangler.toml`: add Durable Object binding
   `CHATGPT_ACCOUNTS` (class `ChatgptAccount`) with a migration tag; add vars
   `CHATGPT_GATE = "0.9"`, `CHATGPT_WRITER_REPOSITORY = "shubhodeep1/claude-workers"`,
   `CHATGPT_WRITER_WORKFLOW = "chatgpt-pool-refresh.yml"`.
2. `tools/claude-pool-broker/src/index.ts`: add `ChatgptAccount` (stores
   `auth_json`, `seed_sha256`, `generation`, `last_refresh`, `last_outcome`,
   `usage {five_hour, weekly, resets}`); routes:
   - `POST /v1/chatgpt` (existing coding-workflows OIDC check): pick the
     usable account with the lowest weekly usage, return `{account, auth_json}`
     with `tokens.refresh_token` removed; `503 chatgpt_pool_empty` when none.
   - `GET /v1/chatgpt/status` (same check): per account metadata, no token.
   - `GET` / `PUT /v1/chatgpt/refresh/<NAME>` (writer OIDC check only): GET
     returns the stored file and generation (or `404 not_seeded`); PUT takes
     `{seed_sha256, base_generation, auth_json?, outcome, usage}`, reseeds when
     the hash differs, otherwise compare-and-swaps on `base_generation`
     (`409 generation_conflict`). Accounts not written for 7 days drop out of
     `/v1/chatgpt`. Log lines carry reason codes and account names only.
3. `tools/claude-pool-broker/test/broker.test.ts`: cover each route, the
   writer check (wrong repo / workflow / ref → 403), refresh-token stripping,
   gate, CAS conflict, reseed.
4. `.github/actions/chatgpt-pool-token/` [new] (`action.yml`, `main.js`,
   `post.js`): modes `token` (writes `$RUNNER_TEMP/chatgpt-pool/<NAME>/auth.json`,
   mode 0600) and `status` (outputs `status` JSON, no token); outputs
   `available`, `reason`, `account`, `codex_home`; never fails the job; post
   step deletes the directory.
5. `tests/test_chatgpt_pool_token.py` [new]: action contract, and that every
   workflow using the action is in `ALLOWED_WORKFLOW_FILES`.
6. Docs: `agents.d/<pr>-chatgpt-pool-broker.md` (§30) under "Models in use";
   `changelog.d/<pr>-chatgpt-pool-broker.md` (§20).

### Phase 2

1. `.github/ai/chatgpt_pool.json` [new]: `{"reviewer_slot_enabled": false,
   "model": "gpt-6-sol", "replaces": "openai/gpt-6-luna",
   "reasoning": "xhigh"}`.
2. `review_autofix.yml` env: `REVIEWER_CHATGPT_SLOT_ENABLED: ${{ vars.REVIEWER_CHATGPT_SLOT_ENABLED || '' }}`,
   `REVIEWER_CHATGPT_MODEL: ${{ vars.REVIEWER_CHATGPT_MODEL || '' }}`,
   `REVIEWER_CHATGPT_REPLACES: ${{ vars.REVIEWER_CHATGPT_REPLACES || '' }}`
   (empty → file values); a step "ChatGPT pool token" (`mode: token`) next to
   the existing Claude pool step, run only when the resolved switch is true.
   Check `wc -c` (§27).
3. `scripts/review_run_reviewers.sh`: resolve the switch; swap the slot in
   the active panel; add `run_chatgpt_reviewer` alongside `run_pool_reviewer`
   (same status files, `skipped_pool` reasons `switch_off`,
   `chatgpt_pool_unavailable`, `auth_rejected`, `quota_exhausted`,
   `run_failed`); log `AI_ENGINE_FALLBACK role=PANEL_REVIEWER_CHATGPT
   reason=<code> action=skip`.
4. `scripts/review_run_reviewers.sh` `classify_review_tier` / panel draw:
   with the switch on, pin the account slots for lite and standard, keep the
   seeded draw for the remaining standard slots, apply the lite floor.
5. `scripts/review_untrusted_sandbox.sh`: engine `chatgpt` runs
   `codex exec` with `CODEX_HOME` from the action, `--json`, read-only
   sandbox, no network beyond the ChatGPT API host the sandbox already
   permits for codex (verify; add only that host if missing).
6. `scripts/claude_engine.py`: register `PANEL_REVIEWER_CHATGPT` with a
   `classify` mapping for Codex auth / quota errors.
7. Tests: switch-off golden test (panel, tier output, logs unchanged);
   switch-on tier composition for lite / standard / full; lite floor;
   skip reasons; `scripts/review_untrusted_sandbox.sh` engine wiring.
8. Docs: `agents.d/` fragment updating "Models in use" (reviewer rows) and
   the tier description; `changelog.d/` fragment; README env-var table rows.

### Phase 3

1. `scripts/review_findings_record.py` [new]: inputs are the parsed
   consolidator output, the per-slot status files and the editor summary
   already on disk; writes `review_findings_record.json` (schema version 1)
   under `PREVIOUS_REVIEWS_DIR`.
2. `review_autofix.yml`: step "Write review findings record" after the
   editor summary (`if: always()` with the same guards as "Upload per-reviewer
   logs", `continue-on-error: true`), and upload
   `reviewer-findings-${{ github.run_id }}-${{ github.run_attempt }}` with
   `retention-days: 30`.
3. Tests: fixture with merged issues, a skipped slot and editor dispositions;
   missing inputs produce a partial record and a warning.
4. Docs: `agents.d/` fragment under "Review pipeline consolidator + ledger
   contract"; `changelog.d/` fragment.

### Phase 4

1. `orchestrate_poll.yml`: in the job that runs the hourly health alert and
   in `claude_pool_daily_usage`, add a `chatgpt-pool-token` `mode: status`
   step; pass its output as `CHATGPT_POOL_STATUS`.
2. `scripts/claude_engine.py` `pool-health` / `pool-usage`: accept
   `--chatgpt-status`; render a "ChatGPT accounts" block (5-hour and weekly
   usage, `last_refresh` age, last outcome); alert conditions as in Approach.
3. `scripts/claude_pool_health_alert.sh`, `scripts/claude_pool_daily_usage.sh`:
   pass the status through; new env `CHATGPT_POOL_ALERT_STALE_HOURS`
   (default `12`). Log keys gain `chatgpt_accounts=N chatgpt_alerts=N`
   (appended, existing keys unchanged).
4. Tests for both scripts with and without ChatGPT status.
5. Docs: `agents.d/` fragment; `changelog.d/` fragment.

## Files & Modules

- `tools/claude-pool-broker/wrangler.toml`
- `tools/claude-pool-broker/src/index.ts`
- `tools/claude-pool-broker/test/broker.test.ts`
- `.github/actions/chatgpt-pool-token/action.yml` [new]
- `.github/actions/chatgpt-pool-token/main.js` [new]
- `.github/actions/chatgpt-pool-token/post.js` [new]
- `.github/ai/chatgpt_pool.json` [new]
- `.github/workflows/review_autofix.yml`
- `.github/workflows/orchestrate_poll.yml`
- `scripts/review_run_reviewers.sh`
- `scripts/review_untrusted_sandbox.sh`
- `scripts/review_findings_record.py` [new]
- `scripts/claude_engine.py`
- `scripts/claude_pool_health_alert.sh`
- `scripts/claude_pool_daily_usage.sh`
- `tests/test_chatgpt_pool_token.py` [new]
- tests for the reviewer slot, tiers, findings record, alert and report
  (new files under `tests/`)
- `docs/scripts-pending-removal.md`
- `README.md` (env-var rows)
- `agents.d/*.md`, `changelog.d/*.md` [new, one per phase PR]
- `shubhodeep1/claude-workers`: `.github/workflows/chatgpt-pool-refresh.yml`
  [new], `README.md` (session-built)

## Data Model / Index Changes

None. No MongoDB collection is touched. Broker state lives in a Cloudflare
Durable Object (one object per account).

## Tests

- Unit: broker vitest suite; pytest for the action, reviewer slot, tiers,
  findings record, alert and report (each phase lists its cases).
- Integration: `ci.yml` already runs the broker vitest job and the pytest
  suite; new tests join those.
- End-to-end (after go-live prerequisites): dispatch `chatgpt-pool-refresh.yml`
  and check its summary (`CHATGPT_POOL refresh account=<NAME> outcome=ok
  generation=N`); dispatch a review on a test PR with the per-repo
  `REVIEWER_CHATGPT_SLOT_ENABLED=true` variable in `coding-workflows` and
  confirm the `chatgpt/gpt-6-sol` slot status `ok` and a
  `reviewer-findings-*` artifact listing it in `FLAGGED_BY`.

## Risks & Mitigations

- OpenAI calls ChatGPT-login Codex in CI "advanced" and recommends API keys
  for most CI; plan terms may limit automated use. ACCEPTED — pending the
  user confirming the Pro plan terms allow it.
- Codex may not expose 5-hour / weekly usage in a script-readable form.
  ACCEPTED — pending discovery in Phase 1; fallback shows last refresh time,
  last outcome and any quota errors instead of percentages, and the gate then
  uses quota errors only.
- The stored login eventually expires (lifetime undocumented). ACCEPTED —
  pending discovery; the hourly alert reports the failed refresh and the
  README tells the user to save a new `CHATGPT_POOL_AUTH_<NAME>`.
- One Pro account may not cover `xhigh` reviews on every PR. ACCEPTED —
  pending trial-week usage; the daily report shows it and a second account is
  one more secret.
- Codex might refuse an `auth.json` without a refresh token. Mitigation:
  Phase 2 tests it first; if refused, the broker gives a placeholder refresh
  token so any refresh attempt fails closed (slot skipped) instead of
  rotating the real one.
- A review job's Codex refreshing on 401 could rotate the login. Mitigation:
  jobs never receive the real refresh token.
- Overlapping refresh runs. Mitigation: workflow `concurrency` group plus the
  Durable Object generation compare-and-swap.
- Lite tier now runs two reviewers when the switch is on (more account usage,
  no OpenRouter cost). ACCEPTED — Q12 A.
- Skipping the GPT slot on standard / full tiers leaves the PR with one fewer
  reviewer. ACCEPTED — Q6 B; the lite floor prevents a PR with no reviewer.

## Rollout

1. Merge Phases 1–4 in any order; deploy the broker after Phase 1 (§24.C).
2. Session: merge the `claude-workers` refresh workflow; the user runs
   `codex login` and saves `CHATGPT_POOL_AUTH_<NAME>`; dispatch the refresh
   and check the summary.
3. Test one review with the per-repo switch in `coding-workflows`.
4. Go-live PR: `.github/ai/chatgpt_pool.json` `reviewer_slot_enabled: true`;
   it reaches consumers on the next `@stable` sync.
5. The session schedules a one-time check-in about 7 days after go-live to
   run the analysis from the `reviewer-findings-*` artifacts (full and
   standard tiers) and write `analysis/reviewer-panel-trial-<date>.md`.
6. Rollback at any time: set `reviewer_slot_enabled: false` (or the repo
   variable), which restores `openai/gpt-6-luna` and the random tier draws.

## References

- `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` (Token broker)
- `docs/plans/claude-pool-capacity-tiers-remaining-callers-plan.md`
- `shubhodeep1/claude-workers` README ("Claude pool key sync")
- OpenAI: [Maintain Codex account auth in CI/CD](https://developers.openai.com/codex/auth/ci-cd-auth)

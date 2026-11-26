# Brainstorm debate workflow (Claude + ChatGPT pools)

## Summary

A new reusable workflow, `brainstorm.yml`, has a Claude seat (Fable 5.2) and
a ChatGPT seat (GPT-6 Astra) study a consumer repo's brief and data, write
ideas without seeing each other's, debate them in up to 4 rounds, and co-write
a `/write-plan`-style plan for the agreed idea. The plan opens as a PR in the
consumer repo, the owner gets a Telegram alert, and `/brainstorm-approve` on
the session issue hands the plan to the orchestrator with the Claude engine,
the same way `/implement-plan-claude` does. binance-blessings (arb and
market-making profit) is the first consumer; tele-funtoken-msg-scoring (users
and wagering) and others follow with their own briefs.

## Context

- **Claude pool.** Roles run on pooled claude.ai accounts through
  `.github/actions/claude-pool-token` and `claude_run` in
  `scripts/ai_engine.sh`. Roles are declared in `scripts/claude_engine.py`
  (`ROLES`, `READ_ROLES`) and `.github/ai/claude_engine.json`
  (`role_defaults`). The model comes from the role's model variable when it
  starts with `claude-` (`resolve_role`, D3 of
  `docs/plans/replace-claude-sessions-with-cli-engine-plan.md`), so `claude-fable-5-2` needs no
  engine change. The `read` profile has `Read,Grep,Glob` plus `WebSearch`
  through the host relay; the container has no network. Capacity wait (exit 80)
  and fallback (`AI_ENGINE_FALLBACK`, exit 75/76) are in
  `docs/plans/claude-capacity-wait-plan.md` and `scripts/ai_engine.sh`.
- **Broker.** `tools/claude-pool-broker/src/index.ts` serves tokens only to
  workflow files in `ALLOWED_WORKFLOW_FILES`, at trusted refs, for repos in
  `.github/ai/consumer_repos.json`. `tests/test_claude_pool_token.py` keeps that
  list equal to the workflows that use the action.
- **ChatGPT pool.** `docs/plans/chatgpt-account-reviewer-trial-plan.md`
  (pending implementation) adds `POST /v1/chatgpt` to the broker,
  `.github/actions/chatgpt-pool-token`, and a `chatgpt` engine in
  `scripts/review_untrusted_sandbox.sh`. The login refresh already runs in
  `shubhodeep1/claude-workers` (`chatgpt-pool-refresh.yml`). None of the
  ChatGPT pieces are on `main` today, so this plan detects them at run time.
- **No debate mechanism exists.** The review panel runs models once in
  parallel and a consolidator merges them (`scripts/review_run_reviewers.sh`).
  Judges are single-shot. No file in `docs/`, `.github`, `scripts` or
  `workflow-templates` mentions brainstorm, debate or ideation.
- **`/write-plan`** (`.claude/commands/write-plan.md`) is interactive only. Its
  plan structure and PR body template are reused here.
- **`/implement-plan-claude` → `/implement-plan-ai`** dispatches the consumer's
  `ai-orchestrate.yml` with a reference-only `project_description` (title line,
  preamble, path/branch/PR) and `engine=claude`.
- **Telegram** is send-only: `scripts/tg_helpers.sh` (`tg_send_msg` with
  levels, `tg_send_report_msg` ignores the threshold). Approvals today are issue
  comments from OWNER/MEMBER/COLLABORATOR (`workflow-templates/ai-implement.yml`,
  `/approved`).
- **Scheduled analysis precedent:** `workflow-log-analysis.yml` (cron, writes
  `analysis/` reports). Scheduled consumer wrapper precedent:
  `workflow-templates/ai-security-audit.yml`.
- **First consumer.** binance-blessings runs the `full` profile. Its strategy
  settings are DigitalOcean env vars, its fills are in Mongo (`executions`,
  `uniswap_txs`, `arb_state`), and a read-only Mongo bridge exists
  (`CLAUDE_AGENT_DB_BRIDGE_URL`). It has no PnL history yet; a separate
  binance-blessings session is planning the detailed strategy data capture.

User decisions (interactive clarification, 2026-11-11):

| Q | Decision |
|---|---|
| Q1 A | The ChatGPT pool (existing trial plan) is built first; this plan depends on its pieces at run time |
| Q2 A | At most 4 debate rounds; without agreement both final positions go to the owner |
| Q3 A | One approval, after the plan is written; plan and alert start with a short key-points summary |
| Q4 A | Telegram alert with links; approval by comment on the session issue |
| Q5 A | Data snapshot before the debate plus read-only data requests during it |
| Q6 A | No exchange API keys in v1 |
| Q7 A | Briefs live in each consumer repo under `.github/ai/brainstorm/` |
| Q8 A | binance-blessings runs weekly |
| Q9 A | Arb and MM are separate topics (separate sessions) |
| Q10 A | Ideas ledger and outcome tracking in v1 |
| Q11 A | This plan goes through `/write-plan` then `/implement-plan-claude` (§31) |
| Q12 A | The models never see a credential; no trading or withdrawal keys anywhere |
| Q13 A | Fable 5.2 vs GPT-6 Astra at high effort, set by repo variables, falling back to Opus 5.5 vs GPT-6 Sol |
| Q14 A | Session issue and plan PR live in the consumer repo |
| Q15 A | The session issues are the ledger (labels + outcome comments) |
| Q16 A | No agreement → owner replies `/brainstorm-pick A` or `B` |
| Q17 A | `/brainstorm-approve`, `/brainstorm-reject <reason>`, owner only |
| Q18 A | Data requests are written by the seats and run host-side, read-only |
| Q19 A | One consumer secret `BRAINSTORM_DATA_SECRETS` (JSON) for data steps only |
| Q20 A | Each consumer ships its own collector/query script, named in the brief |
| Q21 A | binance-blessings onboarding is a separate plan in that repo |
| Q22 A | Three phases, each off by default |
| Q23 A | ChatGPT pool or Claude capacity unavailable → defer to the next day with a Telegram notice |

## Goals

1. A consumer turns brainstorming on with one repo variable
   (`BRAINSTORM_ENABLED=true`) and one or more brief files; with the variable
   unset, the wrapper's caller job is skipped (no runner, no API call). The
   gate still checks the variable, passed by the template as the
   `enabled` input, as a second guard and exits `outcome=disabled`.
2. Each due topic produces one session issue in the consumer repo that holds
   the whole debate as comments, and a session interrupted mid-run resumes from
   its last completed turn on the next run.
3. In the ideas round each seat sees only the brief, the data snapshot, the
   repo context and the ledger, never the other seat's ideas.
4. Each idea carries the scorecard: expected $/week (or the brief's metric) with
   the data behind it, how to verify it (replay or dry-run), risk, effort.
5. Agreement means both seats independently vote the same idea first in the
   same round. Without agreement after `BRAINSTORM_MAX_ROUNDS` (default 4), the
   session posts both final positions and waits for `/brainstorm-pick A|B`.
6. The agreed idea becomes a plan in the consumer repo, `docs/plans/`, in the
   `/write-plan` structure plus a §18.E section and a key-points summary at the
   top, opened as a draft PR with `GH_PAT` so the consumer's review and
   auto-merge pipeline cannot merge it before the owner decides.
7. The owner gets one Telegram message per finished session with the key
   points, the issue link, the PR link and the commands.
8. `/brainstorm-approve` from the repo owner dispatches the consumer's
   `ai-orchestrate.yml` with `engine=claude` and a reference-only
   `project_description`; `/brainstorm-reject <reason>` records the reason and
   closes the issue and the plan PR. No other commenter can trigger either.
9. Each ideas round receives a ledger of earlier sessions for the topic (idea,
   status, outcome). Once a shipped idea's orchestrator project has been closed
   for `BRAINSTORM_OUTCOME_DELAY_DAYS` (default 14), the next session for that
   topic first posts an outcome review comparing expected and measured impact.
10. The seats never hold a credential: data collection and data requests run in
    host steps that hold only `BRAINSTORM_DATA_SECRETS`; seat runs hold neither
    it nor `GH_PAT`.

## Non-goals

- The ChatGPT pool itself (broker routes, token action, sandbox engine,
  refresh). That is `docs/plans/chatgpt-account-reviewer-trial-plan.md`.
- binance-blessings' brief files, collector, query script, secret and variable
  (separate plan in that repo, Q21), and its strategy data capture (separate
  session already running there).
- Telegram replies or buttons as an approval channel.
- Any change to a running strategy. Plans written by this workflow ship
  strategy changes default-off; switching them on is a human DigitalOcean env
  change (§22.B in the consumer repo).
- Exchange API keys of any kind (Q6).
- Moving any existing role onto Fable or Astra.

## Constraints

- **§6 / new identifiers.** No `brainstorm` / `BRAINSTORM` identifier exists in
  this repo today (checked across `scripts/`, `.github/`, `workflow-templates/`,
  `prompts/`, `tools/`, `docs/`). New names: workflow `brainstorm.yml`,
  template `ai-brainstorm.yml`, roles `BRAINSTORM` and `BRAINSTORM_CHATGPT`,
  labels `ai:brainstorm*`, the variables and secret listed under Approach, log
  prefix `BRAINSTORM`. Existing identifiers are not renamed.
- **§4.** Every new variable has a default (Approach, "Settings").
- **§5.** Reuse `claude_run`, `claude-pool-token`, the ChatGPT pool action and
  sandbox engine, `tg_helpers.sh`, the label contract and sync, the wrapper
  profile manifests and the orchestrator dispatch. No second engine wrapper,
  no second Telegram client.
- **§14.** The wrapper reaches every `full`-profile consumer in
  `.github/ai/consumer_repos.json` on the next `@stable` sync and stays inert
  there until the consumer sets `BRAINSTORM_ENABLED=true`.
- **§33 (runner concurrency).** The template's caller job carries
  `if: vars.BRAINSTORM_ENABLED == 'true'`, so an opted-out consumer takes no
  runner slot; the daily schedule still records one skipped run there, which
  is accepted because the profile is synced before the consumer opts in. Jobs
  per scheduled event: before 0; after 0 runner jobs for an opted-out
  consumer, 1 (`gate`) + one `session` job per due topic for an opted-in one.
  `gate` stays a separate job because one topic session may take up to 240
  minutes, so serial topics in one job could pass the 360-minute job limit;
  `session` legs run with `max-parallel: 1`. Comment events (Phase 3): before
  0 runs; after, one run per issue comment in each consumer that has the
  wrapper, with 0 runner jobs unless the caller job's `if:` matches (opted in,
  owner, `ai:brainstorm` issue, `/brainstorm-` body), then 1 `command` job.
  `issue_comment` has no trigger-level filter, so that skipped run per comment
  is accepted explicitly. The commands are not folded into an existing comment
  wrapper (`ai-clarify.yml`, `ai-plan.yml`, `ai-implement.yml`,
  `ai-orchestrate-clarify-respond.yml`): `/brainstorm-pick` runs both seats and
  writes the plan PR, so the job needs the brainstorm pool roles,
  `BRAINSTORM_DATA_SECRETS` and the plan-PR write path, and folding it in would
  hand those secrets and permissions to reusable workflows that do not need
  them (security isolation).
- **§15.** Per scheduled run, the gate makes one contents-listing call for the
  brief directory and one paginated
  `GET /repos/{repo}/issues?labels=ai:brainstorm&state=all&per_page=100` list
  (`gh api --paginate`, one request per 100 session issues; ledger, active
  sessions and outcome candidates all come from that result, and the ledger
  fields live in each session issue's body marker, which the list returns).
  Outcome discovery adds at most one aliased GraphQL query per run covering up
  to 10 pending sessions ("Ledger and outcomes"). The session job makes one
  more paginated list call under its concurrency group to recheck the topic's
  active session before it creates or resumes one. Resume reads the session
  issue's comments once per run (paginated). Turns are posted as they finish
  (one create call each); nothing polls. The recheck and comments read are per
  `session` job, so a run pays them once per due or resumed topic. The outcome
  step adds writes after its one query: per session read, at most one body
  edit (`checked`, plus `tracking` when found); per abandoned or reviewed
  session, one label add and one close. At the 10-session cap that is at most
  10 body edits and 20 label/close calls per run. A stale in-progress session
  ("Active sessions") adds one label write, once per session.
- **§18.** No manual script. Everything runs from the wrapper's `schedule`,
  `workflow_dispatch` and `issue_comment` triggers.
- **§19.** The hand-off references the tracking issue only with `Refs #N`; the
  plan PR body is linted with `scripts/lint_pr_body_auto_close.py` before
  `gh pr create`.
- **§23.C.** The orchestrator dispatch happens only on the owner's
  `/brainstorm-approve` comment, which is the approval for that dispatch.
- **§24.C/E.** Adding `brainstorm.yml` to `ALLOWED_WORKFLOW_FILES` needs a
  broker redeploy from a verified default-branch commit with `FT_GAMES_CF`
  (interactive session, after Phase 2 merges).
- **§27.** `brainstorm.yml` stays small; step bodies live in `scripts/`.
- **Security.** Consumer collector code runs only from the consumer's default
  branch, never from a PR ref. Collector and query output, web results and
  issue comments are untrusted data in every prompt; seats run with the `read`
  profile (no shell, no write tools) in the existing isolated containers, so a
  prompt injection can at most distort a plan the owner reviews before approval.

## Approach

### Flow

```
schedule (daily) ─► gate ──► due / resume topics (brief cadence, active sessions, enabled)
                          └► ledger + outcome-eligible sessions (one paginated issues list)
session (per topic, max-parallel 1, concurrency group per topic)
  1. resolve seats: claude-pool-token (BRAINSTORM) + chatgpt-pool-token (BRAINSTORM_CHATGPT)
       unavailable / capacity wait → defer: WARNING Telegram notice, exit 0
       (no session issue is created, so the topic stays due and the next
        daily run retries it; see "Due rule" below)
  2. recheck the topic's active session (one issues list, under the lock);
     open a new session issue (mode=new) or continue the in-progress one
     (mode=resume)  (marker <!-- ai-brainstorm:v1 topic=… -->)
  3. collect data: brief collector, default-branch checkout, BRAINSTORM_DATA_SECRETS only
  4. outcome review for eligible shipped ideas: each seat writes its own
       assessment (blind, like ideas); the host stitches both into one
       comment with seat=host stage=outcome, and the ledger outcome summary
       records both seats' verdicts
  5. ideas round: both seats in parallel, blind → 2 comments
  6. debate rounds 1..N: each seat sees both idea lists + previous rounds,
       critiques, may request data, then votes (independently)
       host runs data requests between rounds → results go into the next round
       agreement (same first vote) → stop; N = BRAINSTORM_MAX_ROUNDS reached → awaiting-pick
  7. plan: Claude drafts, ChatGPT reviews against the checklist, Claude revises (≤3 passes)
  8. plan PR in the consumer repo (GH_PAT), session issue → awaiting-approval
  9. Telegram report: key points, issue link, PR link, commands
issue_comment
  /brainstorm-pick A|B  → steps 7–9 for that position
  /brainstorm-approve   → claim label approved, mark plan PR ready,
                          dispatch ai-orchestrate.yml engine=claude
  /brainstorm-reject r  → label rejected, close issue and plan PR, record r
```

**Active sessions.** A session is *active* while its issue is open and has
none of `ai:brainstorm-approved`, `ai:brainstorm-rejected` or
`ai:brainstorm-failed`. Approved, rejected and failed sessions are finished:
they feed the ledger and outcomes but never block a new session. An active
session is *in progress* when it has no `ai:brainstorm-awaiting-*` label
(stopped by the job timeout, the soft budget or a seat deferral after it
opened) and *waiting* when it has `ai:brainstorm-awaiting-pick` or
`ai:brainstorm-awaiting-approval`. The issue is created with `ai:brainstorm`
in the same create call, so no session exists without its label. A resume
that finds zero trusted turns starts at the ideas round on the same issue. An
in-progress session whose issue was created more than 7 days ago (issue
`created_at` from the same list) is labelled `ai:brainstorm-failed` by the
gate (`BRAINSTORM gate outcome=stale_session issue=<n>`), so a session that
keeps crashing cannot block its topic for good. An `ai:brainstorm` issue
without a valid body marker is never a session ("Ledger and outcomes").

**Due rule.** For each enabled topic the gate emits at most one matrix entry:
`mode=resume` with the issue number when the topic has an in-progress session,
so the session job continues it from its last trusted turn (Goal 2); nothing
while its session is waiting on the owner; `mode=new` when the topic has no
active session and no session issue for it was created since its most recent
scheduled date (the brief's weekday for `weekly`, today for `daily`, both in
UTC). A deferral before the issue opens creates no issue, so a weekly topic
deferred on its weekday is retried on each daily run until a session opens or
the next scheduled date arrives (Q23). A failed session ends the topic's run
for that scheduled date; the next date opens a new session. The `force`
dispatch input makes the dispatched `topic` count as scheduled today; it never
overrides an active session (an in-progress one is still resumed, a waiting
one still blocks).

### Brief (consumer-owned, `.github/ai/brainstorm/<topic>.yml`)

Validated against `ai-memory/schemas/brainstorm_brief.v1.json` [new]:

```yaml
schema_version: 1
topic: arb                       # ^[a-z0-9-]{1,32}$, unique per repo
enabled: true
cadence: weekly                  # weekly | daily
weekday: mon                     # weekly only
objective: |                     # what "better" means, and the metric
  Increase net USD profit per week of the FUN arb ...
constraints: |                   # hard rules the ideas must respect
  Never weaken kill switches or ARB_HOLD_USD ...
context_paths: [README.md, agents.md, arb_worker.py]   # read by the seats
collector:
  command: python3 -I scripts/brainstorm_collect.py --topic arb
  timeout_minutes: 15
  max_output_bytes: 2000000
query:                           # optional; enables data requests (Q18)
  command: python3 -I scripts/brainstorm_query.py --topic arb
  max_requests_per_round: 5
  max_result_bytes: 200000
  allowed_requests:              # required when query is set; request allowlist
    - type: fills_by_day         # ^[a-z0-9_]{1,48}$
      params:                    # JSON Schema for the request's params object
        type: object
        additionalProperties: false
        properties: {days: {type: integer, minimum: 1, maximum: 90}}
```

The collector writes `snapshot.json` + `snapshot.md` to `$BRAINSTORM_OUT`. The
query command reads one JSON request `{type, params}` on stdin and writes one
JSON result; request content never reaches argv, a shell or the environment.
Before running it, the host validates each seat request against
`query.allowed_requests` (known `type`, `params` valid against its schema);
a request that fails is not run and is recorded as
`data_rejected reason=not_allowed`. The consumer script still decides what it
serves (binance-blessings will map requests to its read-only Mongo bridge).
Both run on the host with only `BRAINSTORM_DATA_SECRETS` exported, a clean
`HOME`, `timeout`, and output truncated to the caps.

**Secret scan.** Collector and query output passes a host-side scan before it
reaches a snapshot file, a seat prompt or a comment. Before the collector
runs, the step validates `BRAINSTORM_DATA_SECRETS`: it must be a JSON object
whose leaf values (nested objects are flattened; numbers and booleans count as
their string form) are all at least 6 characters long. Any shorter or empty
value fails the session closed (`ai:brainstorm-failed`, ERROR alert,
`BRAINSTORM step=collect outcome=secrets_invalid key=<name>`, never the value),
so no configured secret is too short to scan. Every leaf value is matched
literally, and so are its base64 and URL-encoded forms. A match fails closed: collector output
fails the session (`ai:brainstorm-failed`, ERROR alert,
`BRAINSTORM step=collect outcome=secret_in_output`), and a query result is
dropped and recorded as `data_rejected reason=secret_in_output`. Common token
shapes (`ghp_`/`github_pat_`, `sk-`, `AKIA`, PEM blocks, `Bearer …`) are
replaced with `[REDACTED]` and counted in the log line. Raw output is never
logged.

A missing or invalid brief is skipped with
`BRAINSTORM gate outcome=brief_invalid topic=<t>`.

### Seats

- **Claude seat**: role `BRAINSTORM` (`profile: read`, web search on), model
  `vars.BRAINSTORM_CLAUDE_MODEL || 'claude-fable-5-2'`, effort
  `vars.THINKING_LEVEL_BRAINSTORM || 'high'`. The model reaches
  `resolve_role` as the role's model hint (CLI-engine plan D3, see Context), so the checked-in
  `role_defaults` keeps the code default `claude_model`. If the run fails
  because the model is not available on the account (not a capacity or usage
  limit), the seat retries once on
  `vars.BRAINSTORM_CLAUDE_FALLBACK_MODEL || 'claude-opus-5-5'` and logs
  `BRAINSTORM seat=claude outcome=model_fallback`.
- **Model-unavailable signal.** `classify()` in `scripts/claude_engine.py`
  has no such outcome (`OUTCOMES` is `success`, `auth_failed`, `usage_limit`,
  `crashed`, `timeout`), and it is not changed, so other roles keep their
  behaviour. Instead `scripts/brainstorm_seat.sh` checks a `crashed` run's
  result-error text against a fixed list of model-not-found / model-not-allowed
  patterns per engine and exits 81 (`model_unavailable`). Only exit 81 triggers
  the one fallback retry; any other crash exits 1 with no retry. The pattern
  list ships with recorded fixtures and is ACCEPTED — pending the first live
  run's exact error text. An unmatched model error shows up as `seat_failed`,
  which is safe.
- **ChatGPT seat**: role `BRAINSTORM_CHATGPT`, Codex through
  `scripts/review_untrusted_sandbox.sh`'s `chatgpt` engine with the login from
  `.github/actions/chatgpt-pool-token`, read-only sandbox, live web search,
  model `vars.BRAINSTORM_CHATGPT_MODEL || 'gpt-6-astra'`, reasoning
  `vars.THINKING_LEVEL_BRAINSTORM || 'high'`, same model-unavailable retry on
  `vars.BRAINSTORM_CHATGPT_FALLBACK_MODEL || 'gpt-6-sol'`. When the action or
  the sandbox engine is missing (ChatGPT plan not merged), the seat reports
  `seat_unavailable reason=chatgpt_pool_not_installed` and the session defers.
- **ChatGPT seat interface contract.** The trial plan defines the `chatgpt`
  sandbox engine only for the review panel: fixed reviewer prompt and output,
  `xhigh` effort, `PANEL_REVIEWER_CHATGPT` classification. This plan's Phase 1b
  extends that engine with optional inputs, and the reviewer defaults stay as
  they are when the inputs are unset: `CHATGPT_SEAT_MODEL`,
  `CHATGPT_SEAT_EFFORT`, `CHATGPT_SEAT_WEB_SEARCH` (`live`/`disabled`, default
  `disabled`), `CHATGPT_SEAT_PROMPT_FILE`, `CHATGPT_SEAT_OUTPUT_FILE`. It also
  registers `BRAINSTORM_CHATGPT` with the same Codex auth / quota `classify`
  mapping the trial plan adds for `PANEL_REVIEWER_CHATGPT`. Seat detection
  checks for these inputs, not only for the engine; an engine without them
  reports `seat_unavailable reason=chatgpt_seat_interface_missing` and the
  session defers.
- Both seats get the same prompt file per stage (`prompts/mode-brainstorm-*.txt`),
  the snapshot and context as files in the workdir (read with `Read`), and must
  answer in a fenced JSON block the engine validates (ideas, critique, vote,
  data_requests, plan, review). An invalid answer gets one repair retry, then
  the seat counts as failed for that turn and the session is marked
  `ai:brainstorm-failed` with an ERROR alert.
- The ideas prompt asks for up to `BRAINSTORM_IDEAS_PER_SEAT` (default 5)
  ideas, each with the scorecard, and forbids ideas the ledger already rejected
  unless new evidence is named. The critique prompt requires each seat to say,
  per idea of the other side, `concede` or `hold` with evidence, before voting.

### Persistence on the session issue

- Issue title `Brainstorm: <topic> — <YYYY-MM-DD>`, labels `ai:brainstorm`
  (+ state label), body holds the marker in its full format from "Ledger and
  outcomes" (`topic`, `session`, `state`, `approved`, `tracking`, `checked`,
  `ledger`; fields not yet known are omitted, never written empty) and the key
  points as the session advances (edited, not appended). The engine always
  writes the marker as the body's first line, and replaces `<!--` and `-->`
  in model-authored key-points text with `&lt;!--` and `--&gt;` (rendered as
  the same visible text, never parsed as a comment) before composing the
  body, so no forged marker can precede the real one.
- **Body edits.** Anyone with write access can edit the issue body, as they
  can its labels; the comment signature does not cover the body. Such users
  are trusted the same way they are for labels. Every plain marker field is
  still pattern-checked, and decoded `ledger` text is framed as untrusted data
  in every seat prompt, so an edit can at most skew the outcome bookkeeping or
  the ledger an owner-reviewed plan sees, never reach a credential.
- Each turn is one comment beginning with
  `<!-- ai-brainstorm-turn:v1 session=<id> stage=<ideas|round|vote|data|plan|outcome> round=<n> seat=<claude|chatgpt|host> -->`,
  followed by readable markdown and a collapsed JSON block. Resume rebuilds the
  state from these markers; a turn without its marker is ignored.
- **Turn trust.** Resume and the command job accept a turn marker only when
  the comment's author is the login that opened the session issue (the job
  token's bot). When `AI_COMMENT_SIGNING_SECRET` is set, which the template
  forwards as an optional secret, turns are also signed with
  `scripts/comment_signature.py`, and a turn whose signature fails is
  rejected. A rejected or foreign marker is ignored and logged as
  `BRAINSTORM resume outcome=untrusted_turn comment=<id>`. Human comments
  never change votes, agreement or plan state; only the Phase 3 owner
  commands do.
- **Tokens.** Every write on the session issue (creating it, turn comments,
  body-marker edits, labels, and the command job's reply comments) uses the
  job's `github.token`, so every turn has the same author as the issue.
  `GH_PAT` is used only for the plan branch push, `gh pr create --draft`,
  `gh pr ready` / `gh pr ready --undo`, `gh pr close` and the orchestrator
  dispatch. Comments made with `github.token` start no `issue_comment`
  workflow, so turns never trigger the command job.
- The issue is opened with the job's `github.token` (a Bot author, so the
  existing clarify trigger never fires), and `clarify.yml`'s issue-opened skip
  list also gains `ai:brainstorm` as a second guard.

### Labels (added to `.github/ai/label_contract.v1.json`, synced by `ai-sync-labels`)

`ai:brainstorm`, `ai:brainstorm-awaiting-pick`, `ai:brainstorm-awaiting-approval`,
`ai:brainstorm-approved`, `ai:brainstorm-rejected`, `ai:brainstorm-failed`,
`ai:brainstorm-outcome-recorded`.

### Plan writing (Goal 6)

Claude drafts the plan from the agreed idea, the debate and the snapshot in the
`/write-plan` structure (Summary, Context, Goals, Non-goals, Constraints,
Approach, Phases & Merge Strategy, Implementation Steps, Files & Modules, Data
Model, Tests, Risks & Mitigations, Rollout, References) plus `## Key points`
first and a `## §18.E Automation summary`. There is no human clarification
round: anything that would have been a question becomes an
`ACCEPTED — pending owner review` risk, which the owner sees before approving.
The ChatGPT seat reviews it against a checklist (write-plan's sanity check,
the brief's constraints, default-off rollout, verification method present) and
returns blocking findings; Claude revises; at most 3 passes, remaining findings
go into a `## Reviewer notes` section. File
`docs/plans/brainstorm-<topic>-<YYYYMMDD>-<slug>-plan.md`, branch
`ai/brainstorm-<topic>-<YYYYMMDD>`, PR title `docs: plan for <idea title>`,
body = the write-plan PR template plus `Refs #<session issue>`. The PR is
opened as a draft (`gh pr create --draft`), so the consumer's review and
auto-merge pipeline (`ENABLE_AUTO_MERGE` defaults to `true`) cannot merge it
before the owner decides.

### Hand-off (Goal 8)

On `/brainstorm-approve` (author association `OWNER`, issue labelled
`ai:brainstorm-awaiting-approval`, plan PR still open), the job:

1. claims the session by swapping `ai:brainstorm-awaiting-approval` for
   `ai:brainstorm-approved` and writing `approved=<now, UTC>` into the body
   marker before dispatching (a failed dispatch removes it again). The `command` job runs in the
   per-issue concurrency group `brainstorm-cmd-${{ github.repository }}-<issue>`
   with `cancel-in-progress: false`, so a second approve sees the claimed
   label and exits with `outcome=already_approved`;
2. marks the draft plan PR ready (`gh pr ready`);
3. composes the same `project_description` as `/implement-plan-ai` (title line,
   preamble that preserves the plan's phases, path + branch + PR URL, and the
   line `Engine requested: Claude (ai:engine-claude)`) and runs
   `gh workflow run ai-orchestrate.yml -R <repo> -f project_description=… -f engine=claude -f tracking_comment='Refs #<session issue> <!-- ai-brainstorm-ref:v1 session=<id> -->'`
   with `GH_PAT`;
4. comments the dispatch and leaves the plan PR open for the orchestrator.
   The session issue stays open with `ai:brainstorm-approved`, which makes it
   finished ("Active sessions"), so the topic's next scheduled session runs and
   posts the outcome review; the issue is closed when its outcome is recorded.

If the dispatch fails, the job restores `ai:brainstorm-awaiting-approval`,
returns the PR to draft and comments the error, so a later approve can retry.
If the `approved=` marker edit fails after the label swap, the job restores
`ai:brainstorm-awaiting-approval`, comments the error and exits
`outcome=claim_failed` before it touches the PR or dispatches, so no
approved session is left without `approved`. If the plan PR is already merged or closed, approve is refused with a comment
(`outcome=pr_not_open`) and no label changes. In coding-workflows itself the
target is `internal-orchestrate.yml`.

### Ledger and outcomes (Goal 9)

The gate's paginated issues list gives every session for the repo, including
issue bodies. The ledger fields live in each session issue's body marker,
`<!-- ai-brainstorm:v1 topic=<t> session=<id> state=<s> approved=<YYYY-MM-DDTHH:MM:SSZ> tracking=<N> checked=<YYYY-MM-DD> ledger=<b64> -->`,
which the engine edits as the session advances. Only `topic` (brief-schema
pattern), `session`, `state` (fixed enum), `approved` (UTC timestamp written
by the approve step when it claims the session), `tracking` (digits) and
`checked` (date) are plain values. The free-text fields (idea title, expected impact,
outcome summary) are one JSON object in `ledger`, base64url-encoded without
padding, so model text cannot put `-->`, spaces, `=` or a second marker inside
the marker. The parser takes only the first `ai-brainstorm:v1` marker in the
body, only when the issue was opened by the trusted author ("Turn trust"),
rejects a plain field that fails its pattern, and caps the decoded `ledger` at
4 KB. A plain field that fails its pattern, an oversized or undecodable
`ledger`, an untrusted author or no marker at all makes the whole marker
invalid: the gate leaves that issue out of active sessions, the ledger and
outcomes, logs `BRAINSTORM gate outcome=marker_invalid issue=<n>`, and never
lets it block a topic; the command job answers `outcome=no_session`. For each topic the engine builds a ledger from those bodies alone
(≤ 50 entries, newest first: idea title, state, expected impact, outcome
summary if recorded).

`gh workflow run` returns no tracking issue number, so the tracking issue is
found afterwards. `orchestrate.yml`'s tracking-issue step already posts a
non-empty `tracking_comment` input on the tracking issue right after creating
it (and closes that issue and fails the run if the binding fails), and
`workflow-templates/ai-orchestrate.yml` and `internal-orchestrate.yml` already
forward the input. The hand-off's comment (`Refs #<session issue>`) therefore
records a `cross-referenced` event on the session issue. Phase 3 adds a test
that pins this forwarding.

Pending sessions are approved sessions without
`ai:brainstorm-outcome-recorded`. Each run the gate takes up to 10 of them,
least recently checked first (no `checked` first, then the oldest `checked`,
ties oldest session first), and reads them in one aliased GraphQL query: per
session, `timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], first: 100)` with
the source issue's number, labels, `createdAt` and `closedAt`, so turn
comments and label events cannot push the cross-reference off the page. For a
session without `tracking`, the gate takes the earliest source issue labelled
`ai:orchestrator-tracking` whose `createdAt` is after the marker's
`approved` timestamp, and writes
`tracking=<N>`. An approved session whose otherwise valid marker lacks
`approved` (only possible through a body edit) gets `approved=<now>` written
by the gate in the same body edit, logged as
`BRAINSTORM gate outcome=approved_backfilled issue=<n>`, so the 90-day rule
still ends it. Every session read gets `checked=<today>`, which rotates the
next run onto the others. A session becomes outcome-eligible when its
tracking issue's `closedAt` is at least `BRAINSTORM_OUTCOME_DELAY_DAYS` old.
A pending session whose tracking issue is still open or was never found
90 days after the marker's `approved` timestamp gets the outcome `abandoned`
without a review, the
label, and a closed issue, so it stops taking a slot. A failed query skips
outcomes for that run (`BRAINSTORM gate outcome=outcome_query_failed`, fail
open). After an outcome review, the engine writes the outcome summary into
`ledger`, adds `ai:brainstorm-outcome-recorded` and closes the session issue.

### Settings (repo variables, all optional)

| Variable | Default | Meaning |
|---|---|---|
| `BRAINSTORM_ENABLED` | `false` | Master switch per consumer |
| `BRAINSTORM_CLAUDE_MODEL` / `_FALLBACK_MODEL` | `claude-fable-5-2` / `claude-opus-5-5` | Claude seat model |
| `BRAINSTORM_CHATGPT_MODEL` / `_FALLBACK_MODEL` | `gpt-6-astra` / `gpt-6-sol` | ChatGPT seat model |
| `THINKING_LEVEL_BRAINSTORM` | `high` | Effort for both seats |
| `BRAINSTORM_MAX_ROUNDS` | `4` | Debate round cap, clamped to 1–4 (Q2 / D1); out-of-range values are clamped and logged |
| `BRAINSTORM_IDEAS_PER_SEAT` | `5` | Ideas per seat (1–10) |
| `BRAINSTORM_OUTCOME_DELAY_DAYS` | `14` | Days after project close before outcome review |
| `BRAINSTORM_SESSION_BUDGET_MINUTES` | `200` | Soft wall-clock budget; past it the session saves state and resumes next run |

Secret: `BRAINSTORM_DATA_SECRETS` (optional JSON object, consumer-defined
keys, every leaf value at least 6 characters, see "Secret scan"), passed only
to the collector, query and secret-scan steps. The existing
optional `AI_COMMENT_SIGNING_SECRET` is forwarded too, and only the
comment-posting and resume steps receive it ("Turn trust").

Alternatives considered: one model judging the other (rejected: the owner
asked for two peers to agree); a third tie-break model (Q2); Telegram buttons
(Q4); giving seats direct database or network access (Q18, unsafe); a ledger
file in the repo (Q15); per-repo collectors in this repo (Q20).

## §18.E Automation summary

- **Scripts:** new `scripts/brainstorm_engine.py`, `scripts/brainstorm_gate.py`,
  `scripts/brainstorm_handoff.py`, `scripts/brainstorm_seat.sh`, and
  `scripts/brainstorm_step_<slug>.sh` step bodies, each called only from
  `brainstorm.yml`. Extends `scripts/claude_engine.py`,
  `.github/ai/claude_engine.json`, `tools/claude-pool-broker/src/index.ts`. No
  manual-invocation script.
- **Entry points:** `.github/workflows/brainstorm.yml` [new, `workflow_call`],
  called by `workflow-templates/ai-brainstorm.yml` [new] on `schedule`
  (`cron: '23 2 * * *'`, daily; the brief's cadence decides what is due),
  `workflow_dispatch` (inputs `topic`, `force`) and `issue_comment` (created).
  In this repo, `.github/workflows/internal-brainstorm.yml` [new] on
  `workflow_dispatch` only, for smoke tests.
- **Supervisor:** none; a scheduled workflow with resume-from-issue state.
- **DB work:** none in this repo. Consumer data access is read-only and lives in
  the consumer's own scripts.
- **Removal registry (§18.F):** no entry; nothing is single-use, long-running or
  a supervisor. The workflow is permanent while brainstorming is in use.

## Phases & Merge Strategy

Every phase is inert until a consumer sets `BRAINSTORM_ENABLED=true`, and the
binance-blessings plan (Q21) does that only after every phase is on `@stable`
and the broker is redeployed. Merge order: Phase 1, then Phase 2 (its step
scripts call `scripts/brainstorm_engine.py` and `scripts/brainstorm_seat.sh`
from Phase 1), then Phase 3 (it edits `workflow-templates/ai-brainstorm.yml`,
`.github/workflows/brainstorm.yml` and `scripts/brainstorm_gate.py`, which
Phase 2 creates). Phase 1b depends only on Phase 1 and the ChatGPT trial
plan's Phase 2, so it can land before or after Phases 2 and 3. Each phase's
tests use stub seats and a fake GitHub API and need only the phases before
it.

1. **Debate engine and roles.** `scripts/brainstorm_engine.py` (state machine:
   ideas → rounds → votes → agreement / awaiting-pick; data-request validation
   and caps; resume from turn markers; ledger rendering; plan draft/review
   loop), `scripts/brainstorm_seat.sh` (Claude seat via `claude_run`, ChatGPT
   seat via the sandbox engine when present, model fallback),
   `prompts/mode-brainstorm-{ideas,round,vote,plan-draft,plan-review,outcome}.txt`,
   roles `BRAINSTORM` / `BRAINSTORM_CHATGPT` in `scripts/claude_engine.py` and
   `.github/ai/claude_engine.json`, `ai-memory/schemas/brainstorm_brief.v1.json`.
   Each new prompt and script gets its `docs/INVENTORY.md` entry ("Phase
   prompts", "Scripts") in the same phase, so `tests/inventory_parity.py` passes.
   Done: `tests/test_brainstorm_engine.py` green; `tests/test_claude_engine.py`
   updated only by adding `BRAINSTORM` to `CUTOVER_ROLES`, with all of its
   other assertions unchanged and green; nothing calls the engine.
   Rollback: revert.
1b. **ChatGPT seat interface.** The optional seat inputs on the `chatgpt`
   engine in `scripts/review_untrusted_sandbox.sh`, and the
   `BRAINSTORM_CHATGPT` `classify` mapping in `scripts/claude_engine.py`
   ("ChatGPT seat interface contract"). Merge order: after Phase 1 and the
   ChatGPT trial plan's Phase 2, which creates that engine. Until it merges, the seat reports
   `chatgpt_pool_not_installed` or `chatgpt_seat_interface_missing` and
   sessions defer. Done: the reviewer-slot tests from the trial plan are
   unchanged and green, and a seat test covers model, effort, web search and
   prompt/output files. Rollback: revert (the reviewer slot is unaffected).
2. **Scheduled session workflow.** `.github/workflows/brainstorm.yml` (gate +
   session jobs), `internal-brainstorm.yml`, `workflow-templates/ai-brainstorm.yml`
   (schedule + dispatch), `profiles/full.txt`, `scripts/brainstorm_gate.py`,
   `scripts/brainstorm_step_*.sh`, broker allowlist entry + broker test,
   labels, `clarify.yml` skip, docs, `docs/INVENTORY.md` entries for the new
   workflows and step scripts. Done: with the `enabled` input false
   (`BRAINSTORM_ENABLED` unset) the gate exits `outcome=disabled`; a dispatched
   `internal-brainstorm.yml` smoke run (it passes `enabled: true`,
   `brief_dir: tests/fixtures/brainstorm` and `stub_seats: true`, Phase 2
   step 7) with a fixture brief and stub seats opens a session issue, posts turns, opens a plan PR on a test
   branch and sends the report; wrapper integrity and size tests pass.
   Rollback: revert (consumers drop the wrapper on the next sync), or leave
   `BRAINSTORM_ENABLED` unset.
3. **Commands, hand-off and outcomes.** `issue_comment` trigger in the
   template, command job in `brainstorm.yml`, `scripts/brainstorm_handoff.py`
   (pick / approve / reject, author and label checks, `project_description`,
   dispatch, §19 lint), outcome-eligibility in the gate. Done:
   `tests/test_brainstorm_handoff.py` green (non-owner, wrong label, closed PR
   and `/approved` all ignored; approve composes the documented description and
   dispatch); a command on an issue without a session marker exits
   `outcome=no_session`. `docs/INVENTORY.md` gains the hand-off script.
   Rollback: revert; sessions then stop at awaiting-approval.

## Implementation Steps

### Phase 1

1. `scripts/claude_engine.py`: add `BRAINSTORM` and `BRAINSTORM_CHATGPT` to
   `ROLES`, both to `READ_ROLES`; `.github/ai/claude_engine.json`
   `role_defaults`: `BRAINSTORM` → `{engine: claude, claude_model:
   claude-opus-5-5, profile: read}`, the code default, so
   `test_checked_in_config_matches_code_defaults` and
   `test_utility_roles_use_sonnet_and_the_rest_opus` hold. Fable comes from
   `BRAINSTORM_CLAUDE_MODEL` as the model hint (CLI-engine plan D3). `BRAINSTORM_CHATGPT` →
   `{engine: codex, claude_model: claude-opus-5-5, profile: read}` (never run on
   Claude; listed so `resolve` accepts the role). `tests/test_claude_engine.py`:
   add `BRAINSTORM` to `CUTOVER_ROLES`, the only test that pins the Claude-role
   set (`assert on_claude == CUTOVER_ROLES`).
2. `ai-memory/schemas/brainstorm_brief.v1.json` [new]: the brief schema above.
3. `prompts/mode-brainstorm-*.txt` [new]: one per stage, each starting with
   `# tier: DEFAULT` (and within its 250 lines, `tests/prompt_size_budget.py`)
   and ending with the JSON output contract; shared rules (untrusted data, scorecard, no credential
   requests, default-off rollout) in `prompts/_prelude_brainstorm.txt` [new].
   Like `prompts/mode-clarify.txt`, each prompt's opening carries a
   `Role: <x>. Goal: <y>.` line (`tests/test_render_prompt_foundation.py`,
   `ROLE_GOAL_LINE_RE`), and each gets a same-named
   `prompts/_templates/mode-brainstorm-<stage>.txt` [new] that assembles to
   its active prompt's exact bytes (`tests/test_assemble_prompt.py`:
   `test_every_active_mode_prompt_has_a_template`,
   `test_assembled_templates_match_legacy_prompt_bytes`).
4. `scripts/brainstorm_engine.py` [new]: subcommands `next` (given the issue's
   comments JSON and the brief, print the next action), `render` (turn →
   comment body with marker), `validate` (seat JSON → normalised JSON or error),
   `ledger` (issue bodies JSON → ledger markdown), `agreement`, `data-requests`
   (validate against the brief's caps and `query.allowed_requests`),
   `secret-scan` (output + secrets JSON → redacted output or a fail-closed
   error, including the short-value check), `marker` (encode / parse the body
   marker, base64url `ledger` field). `next` accepts turn comments only from the trusted author, with a
   valid signature when signing is on ("Turn trust"). It also clamps
   `BRAINSTORM_MAX_ROUNDS` to 1–4. Pure functions, no network.
5. `scripts/brainstorm_seat.sh` [new]: `brainstorm_seat <claude|chatgpt> <stage>
   <prompt_file> <out_file>`; exits 0 ok, 75 unavailable (defer), 80 capacity
   wait (defer), 81 model unavailable (fallback retry once, then 1), 1 failed;
   logs `BRAINSTORM seat=<s> stage=<g> outcome=<o>`.
6. `tests/test_brainstorm_engine.py` [new]: blind ideas (the ideas prompt of one
   seat never contains the other's output), agreement in round 2, no agreement
   after N → awaiting-pick, rounds clamped to 4, resume after a missing turn,
   forged turn from another author or with a bad signature ignored, invalid
   JSON → one repair then fail, data-request caps and allowlist (unknown
   `type` and invalid `params` rejected), secret scan (a literal, base64 or
   URL-encoded secret value fails closed; a secret value under 6 characters
   fails closed; token shapes redacted), marker round-trip (an idea title
   containing `-->`, `=` or a second marker cannot change other fields), ledger
   excludes other topics, model fallback only on exit 81 (other crashes do
   not retry), key points containing a forged `<!-- ai-brainstorm:v1 … -->`
   come out as `&lt;!--` / `--&gt;` and the real marker stays the only one
   and first, a body with no marker or with one bad plain field parses as
   `marker_invalid`, and a resume with zero trusted turns starts at the ideas
   round.

### Phase 1b

1. `scripts/review_untrusted_sandbox.sh`: optional `CHATGPT_SEAT_*` inputs on
   the `chatgpt` engine (contract above); unset inputs keep the reviewer path
   byte-identical.
2. `scripts/claude_engine.py`: `BRAINSTORM_CHATGPT` `classify` mapping for
   Codex auth / quota errors, same as `PANEL_REVIEWER_CHATGPT`.
3. Tests: the seat inputs, and the reviewer-path golden test unchanged.

### Phase 2

1. `.github/workflows/brainstorm.yml` [new]: `workflow_call` inputs `topic`,
   `force`, `event_name`, `enabled` (boolean, default `false`), `brief_dir`
   (default `.github/ai/brainstorm`; repo-relative, no `..`), `stub_seats`
   (boolean, default `false`; honoured only when `github.workflow_ref` names
   this repo's `internal-brainstorm.yml`, otherwise ignored and logged);
   secrets `GH_PAT`, `TG_BOT_SECRET`, `BRAINSTORM_DATA_SECRETS` (optional) and
   `AI_COMMENT_SIGNING_SECRET` (optional), the same set the template forwards
   (step 7); permissions `contents: write`,
   `issues: write`, `pull-requests: write`, `id-token: write`. Job `gate`
   (no pool token) runs `scripts/brainstorm_gate.py` and outputs a topic matrix.
   Job `session` (matrix, `max-parallel: 1`, `timeout-minutes: 240`): checkout
   consumer default branch, install Claude/Codex, resolve both seats, detect the
   ChatGPT action/engine, then `scripts/brainstorm_step_{collect,run,plan_pr,report}.sh`.
   Concurrency group `brainstorm-${{ github.repository }}-<topic>`,
   `cancel-in-progress: false` (the job writes issues and PRs). Under that
   group the session job re-lists the topic's sessions: a `mode=new` entry
   exits `outcome=already_open` when the topic now has an active session, and a
   `mode=resume` entry exits `outcome=already_finished` when its session is no
   longer in progress, so a stale gate matrix cannot open a duplicate or resume
   a finished session.
2. `scripts/brainstorm_gate.py` [new]: reads `enabled` (second guard),
   `brief_dir` and `force`; active-session classification, due / resume
   calculation (Due rule) and ledger from the one paginated issues list;
   `outcome=disabled|none_due|due|resume`.
3. `scripts/brainstorm_step_collect.sh` [new]: runs the brief's collector with
   only `BRAINSTORM_DATA_SECRETS`, clean env, caps, then the secret scan; the
   step's `env:` names nothing else.
4. `scripts/brainstorm_step_run.sh` [new]: loop over `brainstorm_engine.py next`,
   call seats (ideas in parallel), validate data requests against the
   allowlist, run them via the brief's query command (request on stdin only,
   same isolation and secret scan as collect), post turns, stop on agreement,
   cap, failure, deferral or the soft budget.
5. `scripts/brainstorm_step_plan_pr.sh` [new]: write the plan file, branch,
   commit, lint the body with `scripts/lint_pr_body_auto_close.py`, open the PR
   as a draft with `GH_PAT`, set `ai:brainstorm-awaiting-approval`.
6. `scripts/brainstorm_step_report.sh` [new]: `tg_send_report_msg` for finished
   sessions; `tg_send_msg … WARNING` for deferrals; `ERROR` for failures.
7. `.github/workflows/internal-brainstorm.yml` [new] (`workflow_dispatch`;
   passes `enabled: true`, `brief_dir: tests/fixtures/brainstorm`,
   `stub_seats: true`) and
   `workflow-templates/ai-brainstorm.yml` [new] (schedule + dispatch,
   `permissions:` `contents: write`, `issues: write`, `pull-requests: write`,
   `id-token: write`, the same set as `brainstorm.yml`, because a caller's
   permissions cap the called workflow's token; one caller job `brainstorm` `if: vars.BRAINSTORM_ENABLED == 'true'` so an
   opted-out consumer takes no runner (§33), passes
   `enabled: ${{ vars.BRAINSTORM_ENABLED == 'true' }}`, forwards only `GH_PAT`,
   `TG_BOT_SECRET`, `BRAINSTORM_DATA_SECRETS`, `AI_COMMENT_SIGNING_SECRET`);
   add `ai-brainstorm.yml` to `workflow-templates/profiles/full.txt`.
8. `tools/claude-pool-broker/src/index.ts`: add `brainstorm.yml` to
   `ALLOWED_WORKFLOW_FILES`; update broker tests and `tests/test_claude_pool_token.py`.
9. `.github/ai/label_contract.v1.json`: the seven labels, and the same seven
   with identical colors and descriptions in `scripts/label_helpers.sh`
   (`_AI_LABEL_COLORS`, `_AI_LABEL_DESCS`), which
   `tests/test_ai_labels.py::test_contract_labels_match_helper_color_and_description_catalogs`
   keeps equal to the contract.
   `.github/workflows/clarify.yml`: add
   `!contains(toJson(github.event.issue.labels.*.name), 'ai:brainstorm')` to the
   `issues`/`opened` label-exclusion clause of the `clarify` job's `if:`
   (currently line 41, next to `ai:heal-intake-queue`).
10. Docs: `docs/brainstorm.md` [new] (brief format, onboarding, commands,
    failure modes, cost notes), README section, `agents.d/<N>-brainstorm.md`
    blocks for "Workflow architecture", "Models in use (defaults; overridable via
    repo-vars)", "Workflow install profiles", "Optional `.github/ai` operator
    surfaces", "Stable log prefixes (contractual)" (`BRAINSTORM`),
    `changelog.d/<N>-brainstorm-workflow.md`.
11. Tests: `tests/test_brainstorm_gate.py` [new] (disabled, due/not due by
    weekday, a weekly topic deferred on its weekday is due again the next day
    and not after a session opened, an in-progress session yields
    `mode=resume`, a waiting session blocks a new one, approved, rejected and
    failed sessions do not, `force` never overrides an active session, invalid
    brief skipped, the issues list followed across two pages, a
    `marker_invalid` issue blocks nothing, an in-progress session older than
    7 days is marked failed, an approved session without `approved` is
    backfilled); a stub-seat
    dry-run test of the step scripts, including the under-lock recheck exiting
    `already_open` and `already_finished`; a
    template test that the caller job carries the `BRAINSTORM_ENABLED`
    `if:`; existing wrapper
    integrity, profile manifest and workflow size tests updated.

### Phase 3

1. `workflow-templates/ai-brainstorm.yml`: add `issue_comment: types: [created]`
   and a second caller job `brainstorm-command` (same permissions and secrets,
   `event_name: issue_comment`). The Phase 2 caller job `brainstorm` gains
   `github.event_name != 'issue_comment'` in its `if:`, so schedule and
   dispatch runs, where `github.event.issue` is absent, are unaffected.
   `brainstorm-command`'s `if:` requires `github.event_name == 'issue_comment'`,
   `vars.BRAINSTORM_ENABLED == 'true'`,
   `github.event.issue.pull_request == null`, label
   `ai:brainstorm`, `github.event.comment.user.type == 'User'`,
   `github.event.comment.author_association == 'OWNER'`, and a body starting
   with `/brainstorm-`. A non-owner comment then starts no job and holds no
   secret or runner slot.
2. `.github/workflows/brainstorm.yml`: job `command` (runs when
   `event_name == 'issue_comment'`; `gate` and `session` gain
   `inputs.event_name != 'issue_comment'`, so a command comment never runs the
   due calculation or starts a session leg), concurrency group
   `brainstorm-cmd-${{ github.repository }}-<issue>` with
   `cancel-in-progress: false`: pick → reuse the session job's plan steps
   for the chosen position; approve / reject → `scripts/brainstorm_handoff.py`.
3. `scripts/brainstorm_handoff.py` [new]: parse the command; re-check
   `author_association == OWNER` (defence in depth) and the matching state
   label; for approve, follow the Hand-off order: claim the label and write
   `approved` into the marker, mark the PR
   ready, dispatch `ai-orchestrate.yml` (or `internal-orchestrate.yml`) with
   `engine=claude` and the `tracking_comment` ref, and restore the label and
   draft state on dispatch failure. Refuse when the PR is merged or closed. For
   reject, record the reason and close the PR and the issue. Logs
   `BRAINSTORM command=<c> outcome=<o>`.
4. `scripts/brainstorm_gate.py`: tracking-issue discovery and
   outcome-eligibility (Goal 9: one aliased GraphQL query per run, rotation by
   `checked`, `abandoned` after 90 days)
   and the `outcome` stage hook in `brainstorm_engine.py next`.
5. Tests `tests/test_brainstorm_handoff.py` [new]: two approvals dispatch
   once; a dispatch failure restores the label; a failed `approved=` marker
   edit restores the label and neither readies the PR nor dispatches
   (`claim_failed`); a merged plan PR is refused;
   tracking discovery from a fixture timeline; three long-open projects do not
   starve a fourth pending session; a test pins `tracking_comment` forwarding
   in `workflow-templates/ai-orchestrate.yml` and `internal-orchestrate.yml`;
   tracking discovery ignores a tracking issue created before `approved`; a
   template test that `brainstorm` skips `issue_comment` and only
   `brainstorm-command` runs on it.
   Docs and changelog fragment.

## Files & Modules

- `.github/workflows/brainstorm.yml` [new]
- `.github/workflows/internal-brainstorm.yml` [new]
- `.github/workflows/clarify.yml` (one skip condition)
- `workflow-templates/ai-brainstorm.yml` [new]
- `workflow-templates/profiles/full.txt`
- `scripts/brainstorm_engine.py` [new]
- `scripts/brainstorm_gate.py` [new]
- `scripts/brainstorm_handoff.py` [new]
- `scripts/brainstorm_seat.sh` [new]
- `scripts/brainstorm_step_collect.sh`, `_run.sh`, `_plan_pr.sh`, `_report.sh` [new]
- `scripts/claude_engine.py`
- `scripts/review_untrusted_sandbox.sh` (Phase 1b, optional `CHATGPT_SEAT_*` inputs)
- `.github/ai/claude_engine.json`
- `tests/test_claude_engine.py` (`CUTOVER_ROLES` gains `BRAINSTORM`)
- `.github/ai/label_contract.v1.json`, `scripts/label_helpers.sh` (label catalogs)
- `ai-memory/schemas/brainstorm_brief.v1.json` [new]
- `prompts/_prelude_brainstorm.txt`, `prompts/mode-brainstorm-*.txt`,
  `prompts/_templates/mode-brainstorm-*.txt` [new]
- `tools/claude-pool-broker/src/index.ts` and its tests
- `tests/test_brainstorm_engine.py`, `tests/test_brainstorm_gate.py`,
  `tests/test_brainstorm_handoff.py` [new]; `tests/test_claude_pool_token.py`
- `docs/INVENTORY.md` (Phase 1: prompts, engine and seat scripts; Phase 2:
  workflows, gate and step scripts; Phase 3: hand-off script)
- `docs/brainstorm.md` [new], `README.md`, `agents.d/<N>-brainstorm*.md` [new],
  `changelog.d/<N>-brainstorm*.md` [new]

## Data Model / Index Changes

None in this repo (no MongoDB). State lives in GitHub issues and labels.

## Tests

- Unit: engine state machine, validation, ledger, agreement, caps, resume
  (Phase 1); gate scheduling and API-call count (Phase 2); command parsing,
  author/label checks, description composition, §19 lint (Phase 3).
- Integration: `internal-brainstorm.yml` dispatch with a fixture brief in this
  repo's `tests/fixtures/brainstorm/` and stub seats (the `stub_seats: true`
  input, the only stub channel, honoured only when `github.workflow_ref` names
  this repo's `internal-brainstorm.yml`), proving issue, turns, plan PR
  and report end to end without pool usage.
- Existing: wrapper integrity, profile manifests, broker allowlist parity,
  label contract, workflow size (§27).
- Live check after rollout: the first binance-blessings session run by
  `workflow_dispatch`, read by the owner before the weekly schedule is relied on.

## Risks & Mitigations

- Fable 5.2 or GPT-6 Astra may not be available through subscription
  accounts. ACCEPTED — pending discovery on the first live run; the seats fall
  back to Opus 5.5 / GPT-6 Sol and log it, and the variables can be changed.
- Flagship models use pool quota faster than the pipelines' defaults, and the
  pool is shared with the coding pipelines. Mitigation: weekly cadence,
  capacity wait defers instead of competing, the round and idea caps, and the
  usage visible in the existing daily pool report. ACCEPTED — pending the
  first week's usage numbers.
- The two seats agree too easily. Mitigation: blind ideas, the scorecard,
  per-idea concede/hold with evidence, and independent votes.
- Prompt injection through web pages, collector output or comments.
  Mitigation: read-only seats without credentials, untrusted-data framing, owner
  approval before anything ships, owner-only commands gated in the job `if:`,
  turn markers trusted only from the bot author (signed when signing is on),
  the data-request allowlist, and the fail-closed secret scan on data output.
- A session runs longer than the job. Mitigation: per-turn persistence, the
  soft budget, resume on the next run.
- The ChatGPT plan's pieces are not merged when this ships. Mitigation: run-time
  detection and deferral (Q23); the go-live waits for both plans.
- The binance-blessings Mongo bridge may refuse GitHub-hosted runner IPs.
  ACCEPTED — pending the binance-blessings onboarding plan, which owns its
  collector.
- Plans proposing live strategy changes. Mitigation: the plan prompt and the
  ChatGPT review require default-off or dry-run rollout; turning a change on
  stays a human DigitalOcean change.

## Rollout

1. Merge Phase 1, Phase 2 and Phase 3 in that order (Phase 1b once the
   ChatGPT trial plan's Phase 2 is in); `@stable` release.
2. Interactive session: redeploy `claude-pool-broker` from the verified
   default-branch commit (§24.C) so `brainstorm.yml` can fetch tokens; confirm
   with a dispatched `internal-brainstorm.yml` stub run.
3. ChatGPT trial plan Phase 1 (+ the sandbox `chatgpt` engine from its Phase 2)
   and this plan's Phase 1b must be live before a consumer is enabled; until
   then sessions defer.
4. binance-blessings plan (Q21): briefs `arb.yml` and `mm.yml`, collector and
   query scripts, `BRAINSTORM_DATA_SECRETS`, `BRAINSTORM_ENABLED=true`, first
   session by `workflow_dispatch`.
5. Later consumers (tele-funtoken-msg-scoring: users and wagering) add their own
   briefs and collectors.

Rollback: unset `BRAINSTORM_ENABLED` in a consumer (instant), or revert the
phase PRs.

## Decisions

### D1 — Debate end condition
- **Chosen:** up to 4 rounds, agreement = same first vote, else owner picks.
- **Alternatives considered:** unlimited rounds; third-model tie-break.
- **Why:** bounded cost and time; the owner stays the decider when the seats differ.

### D2 — Data access
- **Chosen:** host-side collector and query scripts owned by each consumer.
- **Alternatives considered:** credentials or network inside the seats.
- **Why:** keeps every credential out of model reach while still allowing follow-up
  questions.

### D3 — Persistence and ledger
- **Chosen:** one GitHub issue per session, turn markers, labels as ledger.
- **Alternatives considered:** workflow artifacts; a ledger file in the repo.
- **Why:** readable by the owner, resumable, and costs one list call per run.

## References

- `docs/plans/chatgpt-account-reviewer-trial-plan.md`
- `docs/plans/replace-claude-sessions-with-cli-engine-plan.md`
- `docs/plans/claude-capacity-wait-plan.md`
- `.claude/commands/write-plan.md`, `.claude/commands/implement-plan-ai.md`,
  `.claude/commands/implement-plan-claude.md`
- `shubhodeep1/claude-workers` README (Claude and ChatGPT pools)
- `shubhodeep1/binance-blessings` `agents.md` (Agent DB bridge, DigitalOcean
  resources, FUN arb, Ladder)

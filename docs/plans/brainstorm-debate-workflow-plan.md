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
  starts with `claude-` (`resolve_role`, D3), so `claude-fable-5-2` needs no
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
   unset, the wrapper's jobs exit after the gate step and make no API call
   beyond reading the brief directory listing.
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
   top, opened as a ready PR with `GH_PAT`.
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
- **§15.** Per scheduled run, the gate makes one contents-listing call for the
  brief directory and one `GET /repos/{repo}/issues?labels=ai:brainstorm&state=all&per_page=100`
  call (ledger + open sessions + outcome eligibility from the same result).
  Resume reads the session issue's comments once per run. Turns are posted as
  they finish (one create call each); nothing polls.
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
schedule (daily) ─► gate ──► due topics (brief cadence, no open session, enabled)
                          └► ledger + outcome-eligible sessions (one issues call)
session (per topic, max-parallel 1)
  1. resolve seats: claude-pool-token (BRAINSTORM) + chatgpt-pool-token (BRAINSTORM_CHATGPT)
       unavailable / capacity wait → defer: WARNING Telegram notice, exit 0
  2. open or resume the session issue  (marker <!-- ai-brainstorm:v1 topic=… -->)
  3. collect data: brief collector, default-branch checkout, BRAINSTORM_DATA_SECRETS only
  4. outcome review for eligible shipped ideas (both seats, one joint comment)
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
  /brainstorm-approve   → dispatch ai-orchestrate.yml engine=claude, label approved
  /brainstorm-reject r  → label rejected, close issue and plan PR, record r
```

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
```

The collector writes `snapshot.json` + `snapshot.md` to `$BRAINSTORM_OUT`. The
query command reads one JSON request on stdin and writes one JSON result; the
consumer script decides what it allows (binance-blessings will map requests to
its read-only Mongo bridge). Both run on the host with only
`BRAINSTORM_DATA_SECRETS` exported, a clean `HOME`, `timeout`, and output
truncated to the caps. A missing or invalid brief is skipped with
`BRAINSTORM gate outcome=brief_invalid topic=<t>`.

### Seats

- **Claude seat**: role `BRAINSTORM` (`profile: read`, web search on), model
  `vars.BRAINSTORM_CLAUDE_MODEL || 'claude-fable-5-2'`, effort
  `vars.THINKING_LEVEL_BRAINSTORM || 'high'`. If the run fails because the
  model is not available on the account (not a capacity or usage limit), the
  seat retries once on `vars.BRAINSTORM_CLAUDE_FALLBACK_MODEL || 'claude-opus-5-5'`
  and logs `BRAINSTORM seat=claude outcome=model_fallback`.
- **ChatGPT seat**: role `BRAINSTORM_CHATGPT`, Codex through
  `scripts/review_untrusted_sandbox.sh`'s `chatgpt` engine with the login from
  `.github/actions/chatgpt-pool-token`, read-only sandbox, live web search,
  model `vars.BRAINSTORM_CHATGPT_MODEL || 'gpt-6-astra'`, reasoning
  `vars.THINKING_LEVEL_BRAINSTORM || 'high'`, same model-unavailable retry on
  `vars.BRAINSTORM_CHATGPT_FALLBACK_MODEL || 'gpt-6-sol'`. When the action or
  the sandbox engine is missing (ChatGPT plan not merged), the seat reports
  `seat_unavailable reason=chatgpt_pool_not_installed` and the session defers.
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
  (+ state label), body holds the marker
  `<!-- ai-brainstorm:v1 topic=<t> session=<id> state=<s> -->` and the key
  points as the session advances (edited, not appended).
- Each turn is one comment beginning with
  `<!-- ai-brainstorm-turn:v1 session=<id> stage=<ideas|round|vote|data|plan|outcome> round=<n> seat=<claude|chatgpt|host> -->`,
  followed by readable markdown and a collapsed JSON block. Resume rebuilds the
  state from these markers; a turn without its marker is ignored.
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
body = the write-plan PR template plus `Refs #<session issue>`.

### Hand-off (Goal 8)

On `/brainstorm-approve` (author association `OWNER`, issue labelled
`ai:brainstorm-awaiting-approval`, plan PR still open), the job composes the
same `project_description` as `/implement-plan-ai` (title line, preamble that
preserves the plan's phases, path + branch + PR URL, and the line
`Engine requested: Claude (ai:engine-claude)`) and runs
`gh workflow run ai-orchestrate.yml -R <repo> -f project_description=… -f engine=claude`
with `GH_PAT`. It comments the run link, swaps the label to
`ai:brainstorm-approved`, and leaves the plan PR open for the orchestrator. In
coding-workflows itself the target is `internal-orchestrate.yml`.

### Ledger and outcomes (Goal 9)

The gate's single issues call gives every session for the repo. For each
topic the engine builds a ledger (≤ 50 entries, newest first: idea title,
state, expected impact, outcome summary if recorded). An approved session is
outcome-eligible when the orchestrator tracking issue named in its hand-off
comment is closed for at least `BRAINSTORM_OUTCOME_DELAY_DAYS` and it has no
`ai:brainstorm-outcome-recorded` label; that one extra issue read happens only
for eligible sessions, at most 3 per run.

### Settings (repo variables, all optional)

| Variable | Default | Meaning |
|---|---|---|
| `BRAINSTORM_ENABLED` | `false` | Master switch per consumer |
| `BRAINSTORM_CLAUDE_MODEL` / `_FALLBACK_MODEL` | `claude-fable-5-2` / `claude-opus-5-5` | Claude seat model |
| `BRAINSTORM_CHATGPT_MODEL` / `_FALLBACK_MODEL` | `gpt-6-astra` / `gpt-6-sol` | ChatGPT seat model |
| `THINKING_LEVEL_BRAINSTORM` | `high` | Effort for both seats |
| `BRAINSTORM_MAX_ROUNDS` | `4` | Debate round cap (1–8) |
| `BRAINSTORM_IDEAS_PER_SEAT` | `5` | Ideas per seat (1–10) |
| `BRAINSTORM_OUTCOME_DELAY_DAYS` | `14` | Days after project close before outcome review |
| `BRAINSTORM_SESSION_BUDGET_MINUTES` | `200` | Soft wall-clock budget; past it the session saves state and resumes next run |

Secret: `BRAINSTORM_DATA_SECRETS` (optional JSON object, consumer-defined
keys), passed only to the collector and query steps.

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
binance-blessings plan (Q21) does that only after all three phases are on
`@stable` and the broker is redeployed, so the phases can merge in any order.
Each phase's tests use stub seats and a fake GitHub API and do not need the
other phases.

1. **Debate engine and roles.** `scripts/brainstorm_engine.py` (state machine:
   ideas → rounds → votes → agreement / awaiting-pick; data-request validation
   and caps; resume from turn markers; ledger rendering; plan draft/review
   loop), `scripts/brainstorm_seat.sh` (Claude seat via `claude_run`, ChatGPT
   seat via the sandbox engine when present, model fallback),
   `prompts/mode-brainstorm-{ideas,round,vote,plan-draft,plan-review,outcome}.txt`,
   roles `BRAINSTORM` / `BRAINSTORM_CHATGPT` in `scripts/claude_engine.py` and
   `.github/ai/claude_engine.json`, `ai-memory/schemas/brainstorm_brief.v1.json`.
   Done: `tests/test_brainstorm_engine.py` green, existing engine tests
   unchanged, nothing calls the engine. Rollback: revert.
2. **Scheduled session workflow.** `.github/workflows/brainstorm.yml` (gate +
   session jobs), `internal-brainstorm.yml`, `workflow-templates/ai-brainstorm.yml`
   (schedule + dispatch), `profiles/full.txt`, `scripts/brainstorm_gate.py`,
   `scripts/brainstorm_step_*.sh`, broker allowlist entry + broker test,
   labels, `clarify.yml` skip, docs. Done: with `BRAINSTORM_ENABLED` unset the
   gate exits `outcome=disabled`; a dispatched smoke run with a fixture brief
   and stub seats opens a session issue, posts turns, opens a plan PR on a test
   branch and sends the report; wrapper integrity and size tests pass.
   Rollback: revert (consumers drop the wrapper on the next sync), or leave
   `BRAINSTORM_ENABLED` unset.
3. **Commands, hand-off and outcomes.** `issue_comment` trigger in the
   template, command job in `brainstorm.yml`, `scripts/brainstorm_handoff.py`
   (pick / approve / reject, author and label checks, `project_description`,
   dispatch, §19 lint), outcome-eligibility in the gate. Done:
   `tests/test_brainstorm_handoff.py` green (non-owner, wrong label, closed PR
   and `/approved` all ignored; approve composes the documented description and
   dispatch); with Phase 2 absent the command job finds no session and exits.
   Rollback: revert; sessions then stop at awaiting-approval.

## Implementation Steps

### Phase 1

1. `scripts/claude_engine.py`: add `BRAINSTORM` and `BRAINSTORM_CHATGPT` to
   `ROLES`, both to `READ_ROLES`; `.github/ai/claude_engine.json`
   `role_defaults`: `BRAINSTORM` → `{engine: claude, claude_model:
   claude-fable-5-2, profile: read}`, `BRAINSTORM_CHATGPT` →
   `{engine: codex, claude_model: claude-opus-5-5, profile: read}` (never run on
   Claude; listed so `resolve` accepts the role).
2. `ai-memory/schemas/brainstorm_brief.v1.json` [new]: the brief schema above.
3. `prompts/mode-brainstorm-*.txt` [new]: one per stage, each ending with the
   JSON output contract; shared rules (untrusted data, scorecard, no credential
   requests, default-off rollout) in `prompts/_prelude_brainstorm.txt` [new].
4. `scripts/brainstorm_engine.py` [new]: subcommands `next` (given the issue's
   comments JSON and the brief, print the next action), `render` (turn →
   comment body with marker), `validate` (seat JSON → normalised JSON or error),
   `ledger` (issues JSON → ledger markdown), `agreement`, `data-requests`
   (validate against the brief's caps). Pure functions, no network.
5. `scripts/brainstorm_seat.sh` [new]: `brainstorm_seat <claude|chatgpt> <stage>
   <prompt_file> <out_file>`; exits 0 ok, 75 unavailable (defer), 80 capacity
   wait (defer), 1 failed; logs `BRAINSTORM seat=<s> stage=<g> outcome=<o>`.
6. `tests/test_brainstorm_engine.py` [new]: blind ideas (the ideas prompt of one
   seat never contains the other's output), agreement in round 2, no agreement
   after N → awaiting-pick, resume after a missing turn, invalid JSON → one
   repair then fail, data-request caps, ledger excludes other topics, model
   fallback only on model-unavailable errors.

### Phase 2

1. `.github/workflows/brainstorm.yml` [new]: `workflow_call` inputs `topic`,
   `force`, `event_name`; secrets `GH_PAT`, `TG_BOT_SECRET`,
   `BRAINSTORM_DATA_SECRETS` (optional); permissions `contents: write`,
   `issues: write`, `pull-requests: write`, `id-token: write`. Job `gate`
   (no pool token) runs `scripts/brainstorm_gate.py` and outputs a topic matrix.
   Job `session` (matrix, `max-parallel: 1`, `timeout-minutes: 240`): checkout
   consumer default branch, install Claude/Codex, resolve both seats, detect the
   ChatGPT action/engine, then `scripts/brainstorm_step_{collect,run,plan_pr,report}.sh`.
   Concurrency group `brainstorm-${{ github.repository }}-<topic>`.
2. `scripts/brainstorm_gate.py` [new]: due-topic calculation, open-session
   detection and ledger from the one issues call; `outcome=disabled|none_due|due`.
3. `scripts/brainstorm_step_collect.sh` [new]: runs the brief's collector with
   only `BRAINSTORM_DATA_SECRETS`, clean env, caps; the step's `env:` names
   nothing else.
4. `scripts/brainstorm_step_run.sh` [new]: loop over `brainstorm_engine.py next`,
   call seats (ideas in parallel), run data requests via the brief's query
   command (same isolation as collect), post turns, stop on agreement, cap,
   failure, deferral or the soft budget.
5. `scripts/brainstorm_step_plan_pr.sh` [new]: write the plan file, branch,
   commit, lint the body with `scripts/lint_pr_body_auto_close.py`, open the PR
   with `GH_PAT`, set `ai:brainstorm-awaiting-approval`.
6. `scripts/brainstorm_step_report.sh` [new]: `tg_send_report_msg` for finished
   sessions; `tg_send_msg … WARNING` for deferrals; `ERROR` for failures.
7. `.github/workflows/internal-brainstorm.yml` [new] (`workflow_dispatch`) and
   `workflow-templates/ai-brainstorm.yml` [new] (schedule + dispatch,
   `id-token: write`, forwards only `GH_PAT`, `TG_BOT_SECRET`,
   `BRAINSTORM_DATA_SECRETS`); add `ai-brainstorm.yml` to
   `workflow-templates/profiles/full.txt`.
8. `tools/claude-pool-broker/src/index.ts`: add `brainstorm.yml` to
   `ALLOWED_WORKFLOW_FILES`; update broker tests and `tests/test_claude_pool_token.py`.
9. `.github/ai/label_contract.v1.json`: the seven labels.
   `.github/workflows/clarify.yml` (line 32 skip list): add `ai:brainstorm`.
10. Docs: `docs/brainstorm.md` [new] (brief format, onboarding, commands,
    failure modes, cost notes), README section, `agents.d/<N>-brainstorm.md`
    blocks for "Workflow architecture", "Models in use (defaults; overridable via
    repo-vars)", "Workflow install profiles", "Optional `.github/ai` operator
    surfaces", "Stable log prefixes (contractual)" (`BRAINSTORM`),
    `changelog.d/<N>-brainstorm-workflow.md`.
11. Tests: `tests/test_brainstorm_gate.py` [new] (disabled, due/not due by
    weekday, open session blocks a new one, invalid brief skipped, one issues
    call); a stub-seat dry-run test of the step scripts; existing wrapper
    integrity, profile manifest and workflow size tests updated.

### Phase 3

1. `workflow-templates/ai-brainstorm.yml`: add `issue_comment: types: [created]`,
   with job `if:` requiring `github.event.issue.pull_request == null`, label
   `ai:brainstorm`, and a body starting with `/brainstorm-`.
2. `.github/workflows/brainstorm.yml`: job `command` (runs when
   `event_name == 'issue_comment'`): pick → reuse the session job's plan steps
   for the chosen position; approve / reject → `scripts/brainstorm_handoff.py`.
3. `scripts/brainstorm_handoff.py` [new]: parse the command; require
   `author_association == OWNER` and the matching state label; for approve,
   check the plan PR is open, compose `project_description`, dispatch
   `ai-orchestrate.yml` (or `internal-orchestrate.yml`) with `engine=claude`,
   record the dispatch and tracking-issue reference in a turn comment; for
   reject, record the reason, close the PR and the issue. Logs
   `BRAINSTORM command=<c> outcome=<o>`.
4. `scripts/brainstorm_gate.py`: outcome-eligibility (Goal 9) and the
   `outcome` stage hook in `brainstorm_engine.py next`.
5. Tests `tests/test_brainstorm_handoff.py` [new]; docs and changelog fragment.

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
- `.github/ai/claude_engine.json`
- `.github/ai/label_contract.v1.json`
- `ai-memory/schemas/brainstorm_brief.v1.json` [new]
- `prompts/_prelude_brainstorm.txt`, `prompts/mode-brainstorm-*.txt` [new]
- `tools/claude-pool-broker/src/index.ts` and its tests
- `tests/test_brainstorm_engine.py`, `tests/test_brainstorm_gate.py`,
  `tests/test_brainstorm_handoff.py` [new]; `tests/test_claude_pool_token.py`
- `docs/brainstorm.md` [new], `README.md`, `agents.d/<N>-brainstorm*.md` [new],
  `changelog.d/<N>-brainstorm*.md` [new]

## Data Model / Index Changes

None in this repo (no MongoDB). State lives in GitHub issues and labels.

## Tests

- Unit: engine state machine, validation, ledger, agreement, caps, resume
  (Phase 1); gate scheduling and API-call count (Phase 2); command parsing,
  author/label checks, description composition, §19 lint (Phase 3).
- Integration: `internal-brainstorm.yml` dispatch with a fixture brief in this
  repo's `tests/fixtures/brainstorm/` and stub seats (`BRAINSTORM_STUB_SEATS=true`
  honoured only in this repo's internal wrapper), proving issue, turns, plan PR
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
  approval before anything ships, owner-only commands.
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

1. Merge the three phases (any order); `@stable` release.
2. Interactive session: redeploy `claude-pool-broker` from the verified
   default-branch commit (§24.C) so `brainstorm.yml` can fetch tokens; confirm
   with a dispatched `internal-brainstorm.yml` stub run.
3. ChatGPT trial plan Phase 1 (+ the sandbox `chatgpt` engine from its Phase 2)
   must be live before a consumer is enabled; until then sessions defer.
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

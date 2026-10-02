# Claude worker pool in GitHub Actions: replace the long-running relay sessions

Supersedes `docs/plans/claude-multi-account-pool-plan.md` (operator decision Q5: A, 2026-10-02).

## Summary

Move every "dumb relay" job of the Claude automation (the Claude issue pickup, the CLAUDE.md §26 per-PR checkers, and the `/implement-plan-claude` project checker) out of long-lived claude.ai sessions and into GitHub Actions. All the real work then runs as headless Claude Code CLI jobs in a private runner repo, `shubhodeep1/claude-workers`, drawn from a pool of claude.ai accounts. Waiting costs no Claude usage, no context grows across wakes, and adding or removing an account is one GitHub secret.

## Context

### What runs today and why it is expensive

| Relay | Lifetime | What the model does each wake |
|---|---|---|
| Claude issue pickup (`.claude/commands/claude-issue-pickup.md`) | weeks; one per account | runs `scripts/claude_issue_route.py queue-pending`, then `create_session` + `create_trigger` + `issue_write` per item; the §26.I session janitor |
| §26 per-PR checker (Sonnet, low effort) | until the PR is terminal | runs `.claude/scripts/check_in_status.py --hand-back`, then `update_trigger` / `send_later`, or starts a fixer |
| `/implement-plan-claude` project checker (Sonnet, low effort) | the whole project | runs `check_in_status.py`, then `send_later` or starts the next stage session |
| Pushing sessions kept for the §26 hand-back | days | sleep, but each hand-back re-reads the whole conversation |

Every decision in these loops is already made by a script; the model only copies JSON fields into claude-code-remote tool calls. They are sessions only because `create_session`, `send_later` and the trigger tools exist only inside a claude.ai cloud session (a routine run does not get them, issue #4525). Each wake re-reads the session's whole context: a checker starts at 73k–103k tokens before doing anything (`CLAUDE.md` alone is 127,650 bytes), and hourly wakes sit at the edge of the 1-hour prompt cache. On 2026-09-29 one account's weekly limit stopped every relay at once and their `send_later` chains died (`docs/operations/master-session.md`, "Usage limit stops").

### The new idea

Turn #4525 around: if **Actions starts every working session**, no session ever needs to start another one, so no session needs claude-code-remote tools. GitHub Actions is the scheduler and the state machine (free on the public `coding-workflows`); a Claude Code CLI job does the real work in the private runner repo.

### Spike evidence (tested before planning, 2026-10-02)

All tests ran in `shubhodeep1/claude-workers`, branch `claude/eager-cori-7gq5zg`, workflow `.github/workflows/pool-spike.yml`, with two pool tokens `CLAUDE_POOL_TOKEN_TEST1` / `CLAUDE_POOL_TOKEN_TEST2` made with `claude setup-token`. Runs: 36956599290, 36956731182, 36957004234, 36957134545, 36961744547, 36964341979, 36964451668.

| Ref | Function | Result |
|---|---|---|
| S1 | Find pool accounts from secret names: `toJSON(secrets)` keys matching `CLAUDE_POOL_TOKEN_*` | ✅ listed `TEST1`, `TEST2`; no API call, no extra credential |
| S2 | Pick a token by name: `secrets[format('CLAUDE_POOL_TOKEN_{0}', name)]` | ✅ |
| S3 | Token clean-up | Both stored tokens had a line break at character 80 (terminal wrap on copy), so every model call failed with `Invalid Authorization header value … contains a line break`. Stripping all whitespace and re-masking (`::add-mask::`) fixed it (110 → 108 characters). The pool must always normalise tokens |
| S4 | Headless `claude -p` with `CLAUDE_CODE_OAUTH_TOKEN` (CLI 2.1.287, `npm install -g @anthropic-ai/claude-code`, Node 22) | ✅ both accounts |
| S5 | `--model claude-opus-5-5` | ✅ shown in the `init` event and `modelUsage` |
| S6 | `--effort high` vs `--effort low` | ✅ effort is not logged anywhere, so it was measured: on a multi-step problem high used about 35% more thinking tokens (247–256 vs 180–191) and took longer, same answer, replicated twice |
| S7 | Usage data | ✅ every run emits a `rate_limit_event` with `rate_limit_info.unifiedWindows.five_hour.utilization` / `seven_day.utilization` as fractions 0..1 (0.14 = 14%), `resetsAt`, `status`, `overageStatus` (`rejected`, `org_level_disabled` on these accounts) |
| S8 | Haiku usage probe (`claude-haiku-4-5-20251001`, empty dir, `--strict-mcp-config`, prompt `Reply OK`) | ✅ ~1.1 s, ~$0.016 at list price, reads S7 |
| S9 | `coding-workflows` slash commands headless | ✅ `fix-claude-pr`, `implement-issue-claude`, `implement-plan-claude`, `claude-issue-pickup`, `claude-issue-dispatch` load; the repo's SessionStart hook runs; a custom command with `$ARGUMENTS` executes |
| S10 | Repo `permissions.allow` in CI | ⚠️ ignored (`workspace has not been trusted`) until `~/.claude.json` has `projects["<checkout>"].hasTrustDialogAccepted: true`; with it the warning is gone |
| S11 | GitHub MCP in CI | ✅ remote `https://api.githubcopilot.com/mcp/` (Bearer token) and `ghcr.io/github/github-mcp-server` (Docker) both work; naming the server `github` keeps the `mcp__github__*` tool names the commands use. Tested with the runner's `GITHUB_TOKEN` only |
| S12 | Permission modes | `auto`: shell, normal edit and a `.claude/settings.json` edit all allowed, no prompt. `bypassPermissions`: all allowed. `acceptEdits`: `.claude/` edit denied. `dontAsk`: everything denied |
| S13 | `.claude/**` deny rule | `Write(./.claude/**)` also blocks `workflow-templates/.claude/**` (it matches at any depth). Anchored to the checkout root, `Edit(//<abs checkout>/.claude/**)`, `Write(…)`, `NotebookEdit(…)` in a `--settings` file deny the Write tool, the Edit tool **and** a shell redirect into `.claude/`, while writes into `workflow-templates/.claude/` still succeed |
| S14 | A repo `PreToolUse` guard that answers "ask" (`gh_api_write_guard.py`) in a headless run | ✅ becomes a denial with the guard's reason; nothing runs |
| S15 | A rejected token | exit code 1, `result` `is_error: true`, `Failed to authenticate. API Error: 401 OAuth access token is invalid.` |
| S16 | Two accounts at once | ✅ both ran concurrently; they are distinct (different `resetsAt`; 0% vs 14–15% weekly). A token does not reveal its account email (`claude auth status` → `email: null`) |
| S17 | Reusable workflow with `secrets: inherit` | ✅ inside the called workflow S1, S2 and a Claude run all work |
| S18 | Fixed start-up cost | a worker in a `coding-workflows` checkout writes ~58k tokens of cache before doing anything (~$0.21–0.24 at Sonnet list price), once per job |
| S19 | Making a token without installing anything | `npx -y @anthropic-ai/claude-code setup-token` runs Claude Code from npm's cache; tested beside a custom `claude` on `PATH`, which stayed untouched |

Platform facts from the docs (code.claude.com/docs/en/authentication): a `setup-token` token lasts one year, authenticates with the subscription, and **can only make model requests**: no claude.ai connectors, no Remote Control, no `create_session`.

### Operator decisions (this session, 2026-10-02)

| Q | Answer | Meaning |
|---|---|---|
| Q1 | A | Workers are the Claude Code CLI in GitHub Actions; one `claude setup-token` per account |
| Q2 | A | A private runner repo, `shubhodeep1/claude-workers` (created; Claude GitHub App has access; Actions "allow all"; default workflow permissions read-only) |
| Q3 | A | One secret per account, `CLAUDE_POOL_TOKEN_<NAME>`, found by name. Adding or removing an account = adding or deleting that one secret |
| Q4 | A | The least-used account is picked; any account at ≥ 90% of its 5-hour or 7-day window is skipped |
| Q5 | A | This plan replaces `docs/plans/claude-multi-account-pool-plan.md`: no per-account pickups, cloud environments or git-ref queue claims; Actions run state replaces leases. The old plan moves out of `docs/plans/` with a pointer here |
| Q6 | A | No hand-back to a pushing session; every fix or stage is a fresh worker that rebuilds its state from GitHub |
| Q7 | A | A real usage-limit rejection is verified when it first happens in production; unit tests simulate it |
| Q8 | A | Test tokens live only in the runner repo's secrets |
| Q9 | A | A throwaway test workflow was allowed in the runner repo, deleted after the tests |
| Q10 | B | The runner repo was created by the operator (the session could not) |
| Q11 | A | Workers keep the twin-first rule: a deny rule for the checkout's `.claude/**` |
| Q12 | A | Workers run `--permission-mode auto` with the checkout trusted, so the repo allow list and hooks apply |
| Q13 | A | A Haiku usage probe before each job |
| Q14 | A | Pool logic lives in `coding-workflows` (reviewed, tested); `claude-workers` holds only thin wrappers that call it with `secrets: inherit` |
| Q15 | A | The wrappers run `coding-workflows@main` |
| Q16 | A | The dispatcher runs in `coding-workflows` Actions (public, free minutes) and starts runner-repo runs with `workflow_dispatch`; account choice and the probe happen inside the runner-repo run, so tokens never leave it |
| Q17 | A | The dispatcher runs right after the intake or a sweep, plus a 15-minute backstop schedule |
| Q18 | A | `/implement-plan-claude` waits are marker comments on the project's final PR; an Actions sweep evaluates them and queues the next stage |
| Q19 | A | No §26 checker sessions; the catch-all covers fixes; Actions sends one Telegram message when a `claude/*` PR merges or closes; §26 is rewritten with its section numbers kept |
| Q20 | A | A `claude/*` PR fix is queued as soon as it is due, at the next sweep |
| Q21 | A | A worker that fails (usage limit, crash, timeout) is re-queued on another account and restarts from GitHub state; unpushed work is lost |
| Q22 | C | No per-account cap on concurrent jobs; only the 90% usage gate |
| Q23 | A | A new fine-grained token for `shubhodeep1`: `GH_PAT` in `claude-workers`, `CLAUDE_POOL_DISPATCH_TOKEN` in `coding-workflows` |
| Q24 | A | Workers get no DigitalOcean or Cloudflare credentials (CLAUDE.md §22, §24.G unchanged) |
| Q25 | A | Pool alerts go to Telegram from the `coding-workflows` dispatcher |
| Q26 | A | Each worker's transcript is an Actions artifact in `claude-workers`, kept 14 days |
| Q27 | A | Rollout one item type at a time; the pickup and checkers keep running until a type is switched; the last phase retires them |
| Q28 | A | Five phases (below) |
| Q29 | A | Four risks accepted as pending discovery (see Risks) |
| Q30 | A | The per-type switch is a committed file, `.github/ai/claude_pool.json`, read by the dispatcher and the pickup (cloud sessions cannot read repo variables, §23.A) |
| Q31 | A | Each worker run probes every pool account and picks the least-used one under 90% |

## Automation wiring (§18.E)

- **New scripts:** `scripts/claude_pool.py` (pool library and CLI), `scripts/claude_pool_dispatch.py` (dispatcher), `scripts/claude_pool_sweep.py` (wait and terminal sweep). Existing scripts are extended: `scripts/claude_issue_route.py`, `scripts/claude_pr_sweep.py`. No standalone manual script: every entry point runs from a workflow.
- **Scheduler entry points:**
  - `.github/workflows/claude-pool-dispatch.yml` [new, coding-workflows]: `workflow_run` (completed) of `Claude Issue Intake`, the review-autofix sweep workflow and `Claude Pool Sweep`, plus `schedule: '*/15 * * * *'` and `workflow_dispatch`.
  - `.github/workflows/claude-pool-sweep.yml` [new, coding-workflows]: `schedule: '*/15 * * * *'`, `workflow_dispatch`.
  - `.github/workflows/claude-pool-worker.yml` [new, coding-workflows]: `workflow_call` only, called by the runner-repo wrapper.
  - `claude-workers/.github/workflows/claude-pool-worker.yml` [new, runner repo]: `workflow_dispatch` (from the dispatcher) and `push` to `claude/**` (smoke run only).
- **Long-running supervisor:** none. Actions schedules replace the sessions; nothing runs between ticks.
- **DB work:** none (no MongoDB collection is touched; §10 does not apply).
- **`docs/scripts-pending-removal.md`:** no new entry; the new scripts are permanent. Phase 5 removes the retired pickup paths; any registry entry they had is deleted in the same PR.

## Goals

- **G1 — No idle Claude usage.** Nothing on the pool path wakes a Claude session to wait. The dispatcher, the sweep and the checks are Actions jobs with no model calls. Verified: phase 2/3 workflow files contain no `claude` invocation; the only model calls are in `claude-pool-worker.yml`.
- **G2 — One-secret accounts.** Adding an account = adding `CLAUDE_POOL_TOKEN_<NAME>` to `claude-workers`; removing = deleting it. No other file, variable, repo or environment changes. Verified: phase 1 test adds a third fixture name and the selection picks it up with no other change; the live smoke lists accounts from secret names (S1).
- **G3 — Least-used account under 90%.** Each worker run probes every pool account (S8) and runs on the one with the lowest `max(five_hour, seven_day)` utilization below `gate_utilization` (default 0.90). Verified by unit tests with fixture probe results (ties, all gated, unknown, one failed probe).
- **G4 — Failover.** A run that fails with an authentication error, a usage-limit error, a crash or a timeout is re-dispatched on another account, up to `max_attempts` (default 3), then alerted on Telegram. Verified by dispatcher unit tests with simulated run outcomes (Q7).
- **G5 — Same work, same model.** Issue implementation, PR fixes and plan stages run on `claude-opus-5-5` at `--effort high` (S5, S6), with the same slash commands as today (S9).
- **G6 — Twin-first holds.** A worker cannot write the checkout's `.claude/**` through any tool; `workflow-templates/.claude/**` stays writable (S13). Verified by the phase 1 smoke run.
- **G7 — Gradual rollout.** With `dispatch_types` empty the system behaves exactly as today. Verified by `claude_issue_route.py queue-pending` tests: no type is filtered when the file is missing or empty.
- **G8 — Tokens stay in one place.** No workflow outside `claude-workers` reads a `CLAUDE_POOL_TOKEN_*` secret, and no log prints one. Verified by a test that greps the `coding-workflows` workflows for `CLAUDE_POOL_TOKEN` and by the add-mask step (S3).
- **G9 — Relays retired.** After phase 5 the pickup stops itself, no command arms a §26 checker or a project checker, and §26.I's session janitor is gone. Verified by command-text tests and by `list_triggers` showing no `Claude issue pickup: hourly` trigger after the retire flag is set.

## Non-goals

- Changing how stage and fixer work is done: the slash commands keep their logic; only their waiting and hand-back mechanics change.
- Retiring the master poller (covered by `docs/plans/retire-master-session-plan.md`).
- The unattended codex pipelines (`unattended_system_instructions.md`).
- DigitalOcean and Cloudflare access for workers (Q24).
- Interactive human sessions' own work; only their §26 check-in changes (Q19).
- Estimating a job's usage cost before running it.
- Buying or managing accounts.

## Constraints

- **§6 naming.** Nothing is renamed or removed except retired behaviour in phase 5, which deletes command text, not identifiers other code reads. `QUEUE_PRODUCERS[*]["workflow"]` stays; a `"workflows"` tuple is added beside it and read first. New identifiers (all checked unique in the repo on 2026-10-02): `CLAUDE_POOL_TOKEN_<NAME>`, `CLAUDE_POOL_DISPATCH_TOKEN`, `CLAUDE_POOL_WORKER`, `.github/ai/claude_pool.json`, payload `claude_stage.v1`, queue item type `stage`, marker `<!-- ai:claude-pool-wait:v1 … -->`, marker `<!-- ai:claude-pool-terminal:v1 -->`, label `ai:claude-pool-waiting`, log prefix `CLAUDE_POOL`.
- **§14.** `.claude/` and `CLAUDE.md` changes reach consumers through the existing sync. No consumer wrapper is added: the dispatcher and sweep run in `coding-workflows` and read consumers with the dispatch token, as `scripts/claude_pr_sweep.py` already does. `claude-workers` is not a consumer and is not added to `.github/ai/consumer_repos.json`.
- **§15.** Every new call is budgeted in the function docstring (below). REST only; the sweep batches waiting PRs with one search call per tick.
- **§18.** No manual scripts; see Automation wiring.
- **§19.** No auto-close keywords against `ai:orchestrator-tracking` issues in any PR body.
- **§20.** One `changelog.d/<pr>-<slug>.md` fragment per phase PR.
- **§21 / §25.** Unchanged. The sweep reads PR state on a schedule; nothing subscribes to PR activity.
- **§23.** The dispatch token is an Actions secret (`secrets.CLAUDE_POOL_DISPATCH_TOKEN`); no workflow reads a session `GH_TOKEN`. Writes from workers use the same `mcp__github__*` tools and `gh api` routine writes the commands use today, under the same guard (S14).
- **§27.** New workflows stay far below 480,000 bytes; `review_autofix.yml` and `review_autofix_sweep.yml` are not edited.
- **§28.C protected paths.** Every `.claude/**` edit is made twin-first in `workflow-templates/.claude/**` and copied by the `[claude-twin-sync]` step. `claude-issue-pickup.md` has no twin; phase 5 edits it through the twin-sync hold (exact diff and sha256 in the blocker).
- **Security.** The pool tokens can only make model requests (docs). They are never echoed, are masked after normalisation, and exist only in `claude-workers`. Workers run in `auto` mode with the repo's hooks and allow list active (S10, S14) and the `.claude/**` deny rule (S13).

## Approach

### Components

```
coding-workflows (public, free minutes)                 claude-workers (private, billed)
───────────────────────────────────────                 ────────────────────────────────
producers ──► ai:claude-issue-queue items               claude-pool-worker.yml (wrapper)
  claude-issue-intake.yml   (issue)                       └─ uses: coding-workflows/
  review_autofix_sweep.yml  (pr_fix, hourly, as today)         .github/workflows/claude-pool-worker.yml@main
  claude-pool-sweep.yml     (stage, pr_fix, terminal)          secrets: inherit
                                                             job select: list CLAUDE_POOL_TOKEN_* names,
claude-pool-dispatch.yml                                       Haiku-probe each, pick least-used < 90%
  reads queue + runner-repo runs ──workflow_dispatch──►     job work: checkout target with GH_PAT,
  closes, re-dispatches, alerts (Telegram)                     claude -p "/<command> …" Opus 5.5 high,
                                                               transcript + result artifacts
```

### Configuration: `.github/ai/claude_pool.json`

One committed file, read by the dispatcher, the sweep, the worker workflow (from its `coding-workflows@main` checkout) and `claude_issue_route.py queue-pending` (the pickup):

```json
{
  "dispatch_types": [],
  "runner_repo": "shubhodeep1/claude-workers",
  "worker_workflow": "claude-pool-worker.yml",
  "gate_utilization": 0.9,
  "max_attempts": 3,
  "timeout_minutes": {"issue": 350, "stage": 350, "pr_fix": 120},
  "transcript_retention_days": 14,
  "worker_model": "claude-opus-5-5",
  "worker_effort": "high",
  "probe_model": "claude-haiku-4-5-20251001",
  "cli_version": "latest",
  "handoff_author_login": "",
  "verdict_bot_login": "",
  "retire_pickup": false
}
```

`dispatch_types` holds any of `issue`, `pr_fix`, `stage`. A missing file, unreadable JSON or an empty list means "pool off". Switching a type on or off is a one-line reviewed PR (Q30).

### Worker run (runner repo)

The wrapper is a thin `workflow_dispatch` workflow whose inputs are `queue_issue`, `item_type`, `attempt`, `exclude_accounts` and `payload_b64` (the queue item's fire text, base64). `run-name: pool ${{ inputs.item_type }} q${{ inputs.queue_issue }} a${{ inputs.attempt }}` is the contract the dispatcher reads; no other state store exists. It calls the reusable workflow with `secrets: inherit` (S17). The reusable workflow:

1. **select** (timeout 10 min): check out `coding-workflows@main` (public). Install the CLI (`cli_version`). Read account names from `toJSON(secrets)` keys matching `^CLAUDE_POOL_TOKEN_[A-Z0-9_]+$` (S1), minus `exclude_accounts`. For each: normalise the token (strip whitespace, add-mask; S3), run the Haiku probe (S8), record `utilization`, `resetsAt`, `status`, or the probe error. `scripts/claude_pool.py choose` picks the lowest `max(five_hour, seven_day)` below `gate_utilization`; ties go to the alphabetically first name. Outputs `account` or `outcome=all_gated` (with the earliest `resetsAt`) / `outcome=no_accounts`. Probe errors classify as `auth_failed` (S15 text) or `probe_failed`, and that account is skipped.
2. **work** (needs select; timeout from `timeout_minutes`): check out the target repo with `secrets.GH_PAT` (full history), mark it trusted in `~/.claude.json` (S10), write the anchored deny settings for `<checkout>/.claude/**` (S13), write the MCP config naming the server `github` with `GH_PAT` (S11), set `CLAUDE_CODE_OAUTH_TOKEN` from the chosen secret (S2, S3), `GH_TOKEN` = `GH_PAT`, `CLAUDE_POOL_WORKER=1`, `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` / `CLAUDE_FIXER_VERDICT_BOT_LOGIN` from the config file, then run:
   ```
   claude -p "<prompt built by claude_pool.py prompt>" --model <worker_model> --effort <worker_effort> \
     --permission-mode auto --settings <deny.json> --mcp-config <mcp.json> \
     --output-format stream-json --verbose > transcript.jsonl
   ```
   Prompts per type: `issue` → `/implement-issue-claude <url>`; `pr_fix` → `/fix-claude-pr <url> — kind <kind> — head <sha> — claim <claim>`; `stage` → `/implement-plan-claude <plan> — resume.` followed by the resume block from the wait marker. Each keeps today's fallback line for a consumer that has not synced the command yet.
3. **report** (always): `claude_pool.py classify transcript.jsonl` writes `claude-pool-result.json` (`queue_issue`, `attempt`, `account`, `outcome` ∈ `success | auth_failed | usage_limit | all_gated | no_accounts | crashed | timeout`, the last `rate_limit_info`, `total_cost_usd`, `duration_ms`). Uploads it as artifact `claude-pool-result` and the transcript as `claude-pool-transcript` (`transcript_retention_days`, Q26). Writes a job summary. Exits non-zero for every outcome but `success`.

`usage_limit` is recognised from the `result` text and the last `rate_limit_info.status` (`rejected`); until a real rejection is seen (Q7) the classifier also treats any `is_error` result whose last `rate_limit_info` is `rejected` or whose utilization is ≥ 1.0 as `usage_limit`.

### Dispatcher (coding-workflows)

`scripts/claude_pool_dispatch.py tick`, run by `claude-pool-dispatch.yml` (concurrency group `claude-pool-dispatch`, `cancel-in-progress: false`):

1. Read `.github/ai/claude_pool.json`; with `dispatch_types` empty, exit `pool=off`.
2. `claude_issue_route.py queue-pending` (existing binding checks, #4621) for the open queue items of the enabled types.
3. One read of the runner repo's worker runs (`GET /repos/<runner>/actions/workflows/<worker>/runs?per_page=100`), parsed by `run-name`. For each open item, its newest run decides:
   - queued or in progress → skip;
   - completed `success` → close the queue issue (`state_reason: completed`, body + `Completed: <run url> (<UTC>) by pool`), like the pickup's `Dispatched:` line;
   - completed failure → download that run's `claude-pool-result` (one list + one download). `all_gated` → no dispatch before the earliest `resetsAt`; one Telegram alert per gate episode. `auth_failed` → Telegram naming the secret, re-dispatch with that account excluded. `usage_limit`, `crashed`, `timeout`, or no result (runner lost) → re-dispatch `attempt + 1`, excluding the account for `usage_limit`. At `max_attempts`, Telegram and leave the item open for `claude-issue-queue-watchdog.yml`;
   - no run → dispatch attempt 1.
4. A dispatch is `POST /repos/<runner>/actions/workflows/<worker>/dispatches` with `CLAUDE_POOL_DISPATCH_TOKEN`. One tick dispatches at most `QUEUE_PICKUP_LIMIT` (20) items, resumes first, as the pickup does today.

Budget per tick (docstring): 1 queue read plus the existing binding reads; 1 runs read; per completed-and-failed item, 2 reads; 1 POST per dispatch; 1 PATCH per close. Fail-open: a failed runs read skips the tick (nothing dispatched twice), and the 15-minute schedule retries.

### Pickup coexistence

`claude_issue_route.py queue-pending` reads `.github/ai/claude_pool.json` from the checkout by default and leaves every item whose type is in `dispatch_types` out of `pending` and `remaining`, listing it under a new `pool` key. The pickup command does not change until phase 5: once its items are filtered out it starts nothing for those types, so the pool and the pickup never both start one item.

### Waits for `/implement-plan-claude` (Q18)

In a pool worker (`CLAUDE_POOL_WORKER=1`) the command never creates a checker, a hand-back Routine or a safety net. At the end of a stage it:

1. Posts or edits one comment on the project's final PR, starting `<!-- ai:claude-pool-wait:v1 slug=<slug> seq=<n> wait=<pr:N | run:ID | issues:a,b> state=waiting -->`, holding a fenced block with the next stage for each outcome (`success`, `review`, `block`, `hand_back`) and the `— resume.` block, exactly as the Checker prompt template fills them today.
2. Adds the label `ai:claude-pool-waiting` to the final PR.
3. Ends.

`scripts/claude_pool_sweep.py` (in `claude-pool-sweep.yml`):
- one REST search per tick for open PRs labelled `ai:claude-pool-waiting` across this repo and every repo in `.github/ai/consumer_repos.json`;
- per PR, one comments read; only a marker whose author is `handoff_author_login` with association owner, member or collaborator counts (the #4622 trust rule);
- runs `check_in_status.py` for the marker's wait and routes on its `action` (`next_stage` → that outcome's stage; `hand_back` → the `block` stage, since there is no session to hand back to, Q6; `wait` / `retry` → nothing);
- for a due stage, opens one bound `claude_stage.v1` queue item (payload `repo`, `slug`, `plan`, `stage`, `marker_comment` id, `seq`) and edits the marker to `state=queued queue=<n>`. A marker already `queued` for that `seq` is never queued again;
- the worker reads the resume block from the marker comment by id, re-checks author and `seq`, and the next stage posts a new `seq`.

### Fixes and terminal notices (Q19, Q20)

`claude-pool-sweep.yml` also runs `scripts/claude_pr_sweep.py --min-age-hours 0` every 15 minutes (the existing hourly catch-all in `review_autofix_sweep.yml` keeps running; its dedupe never queues a PR twice). For every `claude/*` PR authored by `handoff_author_login` that merged or closed since the PR had no `<!-- ai:claude-pool-terminal:v1 -->` comment, it sends one Telegram message (`PR <repo>#<n> merged|closed — <title>`) and then posts that marker, so a message is never repeated.

### Alternatives considered

- **claude.ai routines fired with `/fire`:** about six web-UI steps per account, and every new consumer repo means editing every account's routine; no effort setting. Rejected for Q1/G2.
- **Keep sessions, trim costs** (55-minute re-arms, periodic pickup restarts): still pays a large fixed context per wake and still dies at one account's limit. Rejected.
- **Dispatcher in the runner repo:** billed minutes for a job that needs no secrets of the pool. Rejected (Q16).

## Phases & Merge Strategy

Each phase is one PR in `coding-workflows` (phase 1 adds one PR in `claude-workers`). Every phase is safe with `dispatch_types` empty, and none assumes another has merged: code that needs a later piece checks for it and does nothing when it is missing.

Phases 3 and 4 both add to `scripts/claude_pool_sweep.py` and `.github/workflows/claude-pool-sweep.yml`. Whichever merges first creates them; the other adds its own subcommand and job step to the existing files, which is a merge, not an ordering requirement. Phase 4 merged alone is still safe: the existing hourly catch-all in `review_autofix_sweep.yml` keeps queuing fixes (with its 2-hour wait) until the 15-minute sweep exists.

1. **Pool core and worker.** `scripts/claude_pool.py`, the reusable `claude-pool-worker.yml`, `.github/ai/claude_pool.json` (pool off), the runner-repo wrapper (its own PR in `claude-workers`, with a `push`-to-`claude/**` smoke job), README / agents.md sections, moving the old plan.
   - Done: unit tests pass; the runner-repo smoke run (Haiku no-op through select → work → report, plus the S13 deny check) succeeds on the wrapper PR's branch; nothing dispatches.
   - Rollback: revert the PR; the wrapper is inert without a dispatcher.
2. **Dispatcher.** `scripts/claude_pool_dispatch.py`, `claude-pool-dispatch.yml`, the `queue-pending` pool filter.
   - Done: unit tests (state machine, budget, fail-open); with `dispatch_types` empty a live tick logs `pool=off`. If the worker wrapper is missing, a dispatch fails with a 404, which is alerted, and the item stays with the pickup.
   - Rollback: revert; empty `dispatch_types` already disables it.
3. **Pool-mode waits.** The `CLAUDE_POOL_WORKER=1` branches in `implement-plan-claude.md`, `implement-issue-claude.md` and `fix-claude-pr.md` (twin-first), `scripts/claude_pool_sweep.py` wait handling, `claude-pool-sweep.yml`, `QUEUE_PRODUCERS` `stage` and `workflows`.
   - Done: command-text tests; sweep unit tests; live: with no `ai:claude-pool-waiting` PR the sweep logs `waits=0`.
   - Rollback: revert; sessions without `CLAUDE_POOL_WORKER` never take the new branches.
4. **§26 rewrite, fast catch-all, terminal notices.** `CLAUDE.md` §26 (subsections kept, §6), `pr_check_in_reminder.py` text (twin-first), `claude_pool_sweep.py` catch-all and terminal handling, agents.md "Interactive post-push PR status check-in".
   - Done: tests; live: a merged `claude/*` PR gets exactly one Telegram message and one terminal marker across two ticks.
   - Rollback: revert; checkers resume being armed.
5. **Retire the relays.** Pickup self-retirement on `retire_pickup: true` (twin-sync hold for `claude-issue-pickup.md`), removal of the §26 checker, project checker, hand-back and session-janitor text from the commands, `claude_session_janitor.py` and `stale_routines.py` callers, docs.
   - Done: tests; this phase's PR sets nothing live. The operator's later one-line PR setting `retire_pickup: true` (after all three types ran on the pool for 7 days with no stale queue item) makes the pickup delete its own trigger on its next wake.
   - Rollback: revert, or set `retire_pickup: false` and restart the pickup with `/claude-issue-pickup start`.

## Implementation Steps

### Phase 1 — pool core and worker

1. `scripts/claude_pool.py` [new]: subcommands `accounts` (names from a secrets-keys JSON on stdin, minus excludes), `normalize` (token on stdin → stdout, whitespace stripped), `probe-parse` (stream-json → `{account, five_hour, seven_day, resets_at, status, error}`), `choose` (probe results + `gate_utilization` → account or gated verdict), `prompt` (payload → slash-command prompt), `classify` (transcript → result JSON), `run-name` parse/build. Docstrings state inputs, outputs and that the module makes no API calls.
2. `.github/workflows/claude-pool-worker.yml` [new]: `workflow_call` with the inputs above; jobs `select`, `work`, `report` as in Approach; every secret value goes through `env:` (§27 rule for scripts), no `run:` body echoes a token, `permissions: contents: read`.
3. `.github/ai/claude_pool.json` [new]: the default file with `dispatch_types: []`.
4. `claude-workers/.github/workflows/claude-pool-worker.yml` [new, runner repo]: `workflow_dispatch` inputs, `run-name` contract, `uses: shubhodeep1/coding-workflows/.github/workflows/claude-pool-worker.yml@main` with `secrets: inherit`; plus a `smoke` job on `push` to `claude/**` that calls the reusable workflow with `item_type: smoke` (Haiku `Reply OK`, then a deny-rule probe). Opened as a PR in `claude-workers`; merging it is an activation gate.
5. `tests/test_claude_pool.py` [new]: fixtures from the spike transcripts (S7, S15), whitespace-wrapped tokens (S3), choose cases, prompts per type, classify cases including a simulated usage-limit rejection (Q7). Register in `.github/workflows/ci.yml` as its own step.
6. README "Claude worker pool" section: architecture, `.github/ai/claude_pool.json`, secrets table, the add/remove-an-account runbook (`npx -y @anthropic-ai/claude-code setup-token` → add `CLAUDE_POOL_TOKEN_<NAME>` → done; delete the secret to remove), failure modes. agents.md contract section: run-name contract, result JSON, config keys.
7. Move `docs/plans/claude-multi-account-pool-plan.md` to `docs/completed/` with a first line `Status: superseded by docs/plans/claude-actions-worker-pool-plan.md (2026-10-02)`.
8. `changelog.d/<pr>-claude-pool-core.md`.

### Phase 2 — dispatcher

1. `scripts/claude_pool_dispatch.py` [new]: `tick` as in Approach; Telegram through `scripts/tg_helpers.sh` `tg_send_msg` (`TG_BOT_SECRET`, `TG_ADMIN_CHAT_ID`, as `claude-issue-intake.yml` uses them); budget docstring (§15).
2. `.github/workflows/claude-pool-dispatch.yml` [new]: triggers as in Automation wiring; env `GH_TOKEN: ${{ secrets.GH_PAT }}` for queue reads, `CLAUDE_POOL_DISPATCH_TOKEN` for runner-repo calls; concurrency group.
3. `scripts/claude_issue_route.py`: `queue-pending` reads `--pool-config` (default `.github/ai/claude_pool.json`) and adds the `pool` key; unchanged output when the file is missing or empty.
4. `tests/test_claude_pool_dispatch.py` [new] and `tests/test_claude_issue_route.py` updates.
5. `claude-issue-queue-watchdog.yml` message: name the pool run instead of the pickup restart when the stale item's type is pooled.
6. README / agents.md: dispatcher section; `changelog.d/<pr>-claude-pool-dispatcher.md`.

### Phase 3 — pool-mode waits

1. `workflow-templates/.claude/commands/implement-plan-claude.md` (twin; synced to `.claude/` by `[claude-twin-sync]`): a "Pool mode" section, used when `CLAUDE_POOL_WORKER=1`: Arming the wait posts the wait marker and label instead of steps 0–5; Stage Sessions, Two-step start, Hand-back, Zombie-checker cleanup and Archiving are skipped; Claims use `--by pool-run-<GITHUB_RUN_ID>`.
2. Same for `implement-issue-claude.md` (no session tools needed in pool mode) and `fix-claude-pr.md` (no §26 registration; end after the push).
3. `scripts/claude_issue_route.py`: `QUEUE_PRODUCERS["stage"]` (`claude-pool-sweep.yml`, `schedule`/`workflow_dispatch`, `Sweep run`), the `"workflows"` tuple for `pr_fix`, `build_stage_text` / `parse_stage_text` for `claude_stage.v1`.
4. `scripts/claude_pool_sweep.py` [new]: wait handling as in Approach; budget docstring.
5. `.github/workflows/claude-pool-sweep.yml` [new]: schedule and dispatch triggers; uploads the queue binding artifact like the existing producers.
6. Tests: `tests/test_claude_pool_sweep.py` [new], route tests, command-text tests for the pool-mode sections; `changelog.d/<pr>-claude-pool-waits.md`.

### Phase 4 — §26, fast catch-all, terminal notices

1. `CLAUDE.md` §26 (and `workflow-templates/CLAUDE.md`, a symlink): A–I keep their headings; A/B/C/D are rewritten so that no checker session or hand-back Routine is armed, the pool sweep covers fixes, and Actions sends the terminal notice; G and I stay valid until phase 5. §25.B/C references to "§26 status check-in" are updated to the sweep.
2. `workflow-templates/.claude/hooks/pr_check_in_reminder.py` (twin): reminder text says no check-in is needed and the pool sweep covers the PR.
3. `scripts/claude_pool_sweep.py`: run `claude_pr_sweep.py --min-age-hours 0` and the terminal notices.
4. agents.md "Interactive post-push PR status check-in" rewritten; tests for the reminder text and the terminal dedupe; `changelog.d/<pr>-claude-pool-check-in.md`.

### Phase 5 — retire the relays

1. `claude-issue-pickup.md` (no twin; twin-sync hold): on a wake with `retire_pickup: true`, delete its own `Claude issue pickup: hourly` and catch-up triggers, report once, and archive itself.
2. Remove from the command twins the checker, hand-back, safety-net, zombie-cleanup and session-janitor text that pool mode made unused; keep pool mode as the only mode.
3. Remove `scripts/claude_session_janitor.py` calls and the pickup's step 3a; delete any matching `docs/scripts-pending-removal.md` entries.
4. README / agents.md / `docs/operations/master-session.md` updates; `changelog.d/<pr>-retire-claude-relays.md`.

## Files & Modules

- `scripts/claude_pool.py` [new]
- `scripts/claude_pool_dispatch.py` [new]
- `scripts/claude_pool_sweep.py` [new]
- `scripts/claude_issue_route.py`
- `.github/ai/claude_pool.json` [new]
- `.github/workflows/claude-pool-worker.yml` [new]
- `.github/workflows/claude-pool-dispatch.yml` [new]
- `.github/workflows/claude-pool-sweep.yml` [new]
- `.github/workflows/claude-issue-queue-watchdog.yml`
- `.github/workflows/ci.yml`
- `shubhodeep1/claude-workers` → `.github/workflows/claude-pool-worker.yml` [new]
- `workflow-templates/.claude/commands/implement-plan-claude.md`, `implement-issue-claude.md`, `fix-claude-pr.md` (twins → `.claude/commands/`)
- `workflow-templates/.claude/hooks/pr_check_in_reminder.py` (twin → `.claude/hooks/`)
- `.claude/commands/claude-issue-pickup.md` (no twin; twin-sync hold)
- `CLAUDE.md` (§26, §25.B/C references)
- `README.md`, `agents.md`, `docs/operations/master-session.md`
- `docs/plans/claude-multi-account-pool-plan.md` [del] → `docs/completed/claude-multi-account-pool-plan.md` [new]
- `tests/test_claude_pool.py`, `tests/test_claude_pool_dispatch.py`, `tests/test_claude_pool_sweep.py` [new]; `tests/test_claude_issue_route.py`, command-text tests
- `changelog.d/` fragments, one per phase

## Tests

- **Unit** (pytest, each new file in its own `ci.yml` step): token normalisation; account discovery from secret keys with excludes and invalid names; probe parsing from the spike transcripts; choose (lowest max utilization, the 0.90 gate, ties, all gated, unknown utilization, failed probes); prompts per type; classify (success, the S15 auth error, simulated usage-limit rejection, crash, missing result); dispatcher state machine per newest-run state, attempts, excludes, gate wait, Telegram dedupe, API-call counts; sweep marker trust, `seq` dedupe, routing, stage payload build/parse, terminal dedupe; `queue-pending` pool filter on/off/missing file.
- **Integration (live, automated):** the runner-repo smoke job on every push to a `claude/**` branch of `claude-workers` (S1–S3, S8, S13 end to end); a dispatcher tick with the pool off; a sweep tick with no waits.
- **End to end (activation):** after the gates below, one type at a time: switch `pr_fix`, confirm a real `claude/*` PR fix runs on the pool and the queue item closes with `Completed:`; then `issue`; then `stage` on a small project.
- **Static:** `tests/test_workflow_file_size_limit.py` covers the new workflows; a test asserts no `coding-workflows` workflow references `CLAUDE_POOL_TOKEN`.

## Risks & Mitigations

- **A real usage-limit rejection may look different from the simulation.** ACCEPTED — pending the first production occurrence (Q7, Q29). The classifier treats any `is_error` with `rate_limit_info.status: rejected` or utilization ≥ 1.0 as `usage_limit`, and an unrecognised failure still re-dispatches as `crashed`.
- **GitHub-hosted jobs stop at 6 hours.** ACCEPTED — pending (Q29). `timeout_minutes` stays at 350; a stage that times out is re-dispatched (Q21) and resumes from the progress log.
- **The GitHub MCP server with the new fine-grained token** was tested only with the runner's `GITHUB_TOKEN` (S11). ACCEPTED — pending the phase 1 smoke run with `GH_PAT`; the fallback is the Docker server.
- **Terms of service for rotating personal accounts.** ACCEPTED — operator decision on 2026-09-30, reaffirmed in Q29.
- **Unpushed work is lost when a worker fails** (Q21). Mitigation: the commands already push per step and per PR; re-dispatch restarts from GitHub.
- **No per-account cap** (Q22): many parallel jobs can push one account past 90% within a single 5-hour window, since the gate is read only at job start. Mitigation: failover on `usage_limit`; operators can add accounts.
- **Billed Actions minutes** for worker jobs (the Pro allowance is used up this month; $1,000 budget, "stop usage" off). Mitigation: dispatcher and sweep run in the public repo; worker timeouts; the job summary shows duration.
- **`coding-workflows@main` breaks the worker** for every account at once (Q15). Mitigation: the smoke job and CI; empty `dispatch_types` sends everything back to the pickup until phase 5.
- **A wait marker forged by another collaborator.** Mitigation: only markers by `handoff_author_login` with owner/member/collaborator association count (#4622), and the worker re-checks author and `seq`.
- **Tokens leak in logs.** Mitigation: normalise then `::add-mask::`, values only through `env:`, never echoed; a test greps for `CLAUDE_POOL_TOKEN` outside the runner repo.
- **Double start during rollout.** Mitigation: `queue-pending` filters pooled types for the pickup; the dispatcher keys runs by queue issue and attempt.

## Rollout

**Activation gates (human, once, none per account):**
1. Create a fine-grained token for `shubhodeep1` with contents, pull requests, issues and workflows read/write, and actions read/write, on `coding-workflows`, the 13 consumer repos and `claude-workers`. Store it as `GH_PAT` in `claude-workers` and `CLAUDE_POOL_DISPATCH_TOKEN` in `coding-workflows` (Q23).
2. Merge the phase 1 wrapper PR in `claude-workers` (a default-branch merge; §23.C).
3. Add the production account secrets `CLAUDE_POOL_TOKEN_<NAME>` (made with `npx -y @anthropic-ai/claude-code setup-token`) and delete `CLAUDE_POOL_TOKEN_TEST1` / `TEST2` if those accounts are not pool members.

**Per type** (one reviewed PR each, Q27/Q30): add `pr_fix`, then `issue`, then `stage` to `dispatch_types`, each after the previous one ran for 3 days without a stale queue item.

**Retirement:** after all three types ran on the pool for 7 days with no stale queue item, a PR sets `retire_pickup: true`; the pickup removes its own trigger on its next wake.

**Rollback:** remove a type from `dispatch_types` (the pickup takes it back on its next wake); before phase 5, the old relays are untouched.

**Adding or removing an account later:** add or delete one `CLAUDE_POOL_TOKEN_<NAME>` secret in `claude-workers`. Nothing else changes (G2). Renew each token yearly; an expired token shows up as `auth_failed` on Telegram.

## References

- Issue #4525 (routine runs lack session tools), #4621 (queue binding), #4622 (trusted claim authors), #4990 (pickup throughput)
- `docs/plans/claude-multi-account-pool-plan.md` (superseded), `docs/plans/retire-master-session-plan.md`
- `.claude/commands/claude-issue-pickup.md`, `.claude/commands/implement-plan-claude.md` (Check-in Loop, Stage Sessions), `.claude/scripts/check_in_status.py`, `scripts/claude_issue_route.py`, `scripts/claude_pr_sweep.py`
- CLAUDE.md §6, §14, §15, §18, §20, §22–§28
- Spike: `shubhodeep1/claude-workers` branch `claude/eager-cori-7gq5zg`, runs listed under Spike evidence
- code.claude.com/docs/en/authentication (`setup-token`), code.claude.com/docs/en/setup (install), code.claude.com/docs/en/routines (rejected alternative)

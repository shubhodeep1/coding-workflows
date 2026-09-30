# Multi-account work pool for the Claude automation

## Summary

Let several claude.ai accounts share the Claude automation's work. Each account claims standalone issues, `/implement-plan-claude` stages and `claude/*` PR fixes (review fixes included) through atomic git-ref claims. An account stops picking up work once its 5-hour or weekly usage reaches 90%. When an account hits its limit mid-task, another account takes the work over from the last saved checkpoint. Today one account's limit stops everything (2026-09-29), and recovery is manual.

## Context

- **The 2026-09-29 outage.** On 2026-09-29 the account hit its weekly limit. Every Sonnet session (poller, pickup, checkers) and many stage sessions failed with `You've hit your weekly limit`, and their `send_later` chains died. The master then re-triggered each session by hand ("Usage limit stops", `docs/operations/master-session.md`).
- **claude.ai objects are bound to one account.** Sessions, Routines, `send_later` and `create_session` all belong to one account; nothing in one account can target another (code.claude.com/docs/en/routines, /claude-code-on-the-web).
- **GitHub state is shared.** The queue (`ai:claude-issue-queue`), fix claims (`.claude/scripts/claude_fix_claim.py`), progress logs (`docs/implement-plan/<slug>.md`) and the queue binding artifacts are readable by every account. So GitHub is the coordination plane and claude.ai sessions are disposable.
- **Existing pieces this plan extends** (§5: extend, never compete):
  - `.claude/commands/claude-issue-pickup.md`: one pickup per account, since `list_triggers` only sees its own account. Hourly wake, up to 20 starts per wake.
  - `scripts/claude_issue_route.py` (`queue-pending`, `QUEUE_PRODUCERS`, `parse_fire_text`, `build_pr_fix_text`).
  - `scripts/claude_pr_sweep.py`: the §26.H catch-all, with `--min-age-hours`.
  - `.claude/scripts/check_in_status.py` (`check_pr_hand_back`, `read_fix_claims`).
  - `.claude/commands/implement-plan-claude.md`: Stage Sessions, the `— resume.` block, the Check-in Loop and the Progress Log.
  - `.claude/commands/fix-claude-pr.md` and `.claude/commands/implement-issue-claude.md`.
- **What the platform exposes about usage:**
  - The statusline JSON carries `rate_limits.five_hour` / `seven_day` `.used_percentage` and `.resets_at` for Pro/Max plans. Whether the statusline runs in cloud sessions is unverified (code.claude.com/docs/en/statusline).
  - The `StopFailure` hook carries `error_type: rate_limit`. Whether it fires on 5-hour and weekly limits in cloud sessions is unverified (code.claude.com/docs/en/hooks).
  - `get_session` / `list_sessions` expose `post_turn_summary.status_detail`, which carried the `You've hit your weekly limit` text during the outage. This is a signal that is available today.
- **A push to `claude/**` starts a review.** A push to a `claude/**` branch with no PR starts a full reviewer panel (`internal-review.yml`, `push: branches: claude/**`). So mid-stage checkpoints must not use `claude/**` branches.
- **Model prices.** `claude-sonnet-5-5` and `claude-sonnet-5` are both $2 / $10 per MTok, with the same tokenizer. `claude-sonnet-4-6` is $3 / $15. Haiku 4.5 cannot run in Auto mode, so it cannot be an unattended checker (agents.md "Interactive slash-command model selection").

### Operator decisions (this session, 2026-09-30)

| Q | Answer | Meaning |
|---|---|---|
| Q1 | C | Several personal Pro/Max accounts. The operator accepts the terms risk (see Risks). |
| Q2 | A | Every account connects the same GitHub user (`shubhodeep1`), so the claim-trust rule (`CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`, PR author) is unchanged. |
| Q3 | B | Failover **and** round-robin with usage gating, in one project. |
| Q4 | A | Phase 1 measures what works in a cloud session; the gate uses whichever sources work. |
| Q5 | A | Claims are atomic via git ref creation and fast-forward updates. |
| Q6 | A | The handoff unit is the stage; the `— resume.` block is committed. |
| Q7 | A | The pushing session gets the first chance at review fixes; other accounts take over after 30 minutes. |
| Q9 | A | The account name comes from the env var `CLAUDE_POOL_ACCOUNT`, set in each account's cloud environment. |
| Q10 | A | Phase 1 measures `claude-sonnet-5` against `claude-sonnet-5-5`; checks use the lower. `claude-sonnet-5` stays until then. |
| Q11 | A | Unknown usage counts as under 90%. An account whose own session reported a limit error is gated until the reset. |
| Q12 | A | 90% cut-off plus a cap on active work per account (default 3). |
| Q13 | A | Work-in-progress checkpoints go to non-branch refs `refs/pool/wip/<unit>`; fallback is no checkpoint. |
| Q14 | B | Leases last 3 hours (same as `CLAUDE_FIX_CLAIM_LEASE_HOURS`), renewed at every checkpoint and checker wake. |
| Q15 | A | New `claude-pool-sweep.yml`, every 15 minutes; the existing hourly sweep keeps its 2-hour wait. |
| Q16 | A | Covers coding-workflows plus every repo in `.github/ai/consumer_repos.json`. |
| Q17 | B | On by default. `CLAUDE_POOL_ENABLED=false` (repo var) is the kill switch for the Actions side. |
| Q18 | A | Six phases, each skipping its pool steps when the pool library is missing. |
| Q19 | A | The platform unknowns are accepted risks, settled by phase 1. |
| Q20 | A | A session without `CLAUDE_POOL_ACCOUNT` stays out of the pool and behaves as today. |

Operator requirements: checks and monitoring run on the cheapest Sonnet at low effort, or in GitHub Actions (no Claude usage). As little work as possible is lost at a limit. The gate stops new work at 90% of either window.

## Goals

- **G1 — Atomic claims.** Two pickups in different accounts never start the same queue item. Claiming is a git-ref create that GitHub refuses to repeat. Verified by a unit test that simulates two claimants against a fake ref store, and by the phase-2 live check (two concurrent `claim` calls on one ref: exactly one returns `claimed`).
- **G2 — Usage gate.** A pickup whose account reads ≥ 90% on the 5-hour or the weekly window starts nothing. The same holds for an account inside a reset window taken from a limit error. Its one-line report shows `pool=gated (<reason>)`. Verified by unit tests on `claude_pool.py gate` with fixture inputs.
- **G3 — Cap per account.** No account holds more than `CLAUDE_POOL_MAX_ACTIVE` (default 3) live leases at a wake. Verified by unit tests on the gate's slot count.
- **G4 — Stage takeover.** A project stage whose lease expired (3 h without renewal) is queued as a `claude_stage.v1` item within 15 minutes of expiry. Any gated-in account resumes it from the committed `— resume.` block and the last `refs/pool/wip/…` checkpoint. Verified by sweep unit tests and one live takeover in phase 4's done condition.
- **G5 — Fast review-fix takeover.** A `claude/*` PR fix that has been due for 30 minutes with no live claim is queued by `claude-pool-sweep.yml`. The existing 2-hour hourly sweep keeps running unchanged. Verified by sweep tests with `--min-age-hours 0.5`.
- **G6 — Legacy accounts unaffected.** A session without `CLAUDE_POOL_ACCOUNT` behaves exactly as before: no lease writes, no gate, the same pickup output apart from one `pool=off` token. Verified by tests that run each changed script with the env var unset.
- **G7 — Model choice from data.** Phase 1 records measured tokens per checker wake for both Sonnets in `docs/operations/claude-pool-measurements.md`. Phase 6 sets the checker/pickup default to the lower one, or keeps `claude-sonnet-5` when the file has no result.

## Non-goals

- Moving a live session between accounts. Only committed state (leases, checkpoints, progress logs) moves.
- Estimating how much usage an item will cost (Q12 answered A; a size estimate may follow later).
- Changing how stage and fixer sessions do their work: they stay Opus 5.5 at high effort.
- Any change to the unattended codex pipelines (`unattended_system_instructions.md`).
- Reading usage through undocumented endpoints (`api/oauth/usage`).
- Buying or managing accounts, or usage credits.

## Constraints

- **§6 naming.** No identifier is renamed or removed.
  - New additions: `claude_stage.v1` payload, `trigger: takeover`, fix-claim `kind=release`, `QUEUE_PRODUCERS[*]["workflows"]` next to the existing `"workflow"` key, env vars `CLAUDE_POOL_*`, log prefix `CLAUDE_POOL`.
  - Every reader keeps accepting the old forms.
- **§15 API hygiene.** Every new call is budgeted in a docstring (below). The Actions sweep reads refs in one GraphQL query per repo (GH_PAT, no proxy). Sessions use REST only (the web proxy refuses GraphQL, §23.A).
- **§18.** No manual scripts. Everything runs from the pickup wake, stage sessions, checkers, hooks, or the scheduled `claude-pool-sweep.yml`. There is no new supervisor: the per-account pickup is the existing one, and the sweep is a scheduled workflow. There are no single-use scripts, so no `docs/scripts-pending-removal.md` entry.
- **§20.** One `changelog.d/` fragment per phase PR.
- **§21 / §25 / §26 unchanged.** Takeover is a scheduled read plus a queue item, never PR watching.
- **§23.H.** Session-side writes go through `.claude/scripts/claude_pool.py`, which calls `gh api` from Python, so the Bash guard sees one allowlisted script call. New `permissions.allow` rules widen `settings.json` and need the operator's approval window (master-session Q3/Q62).
- **§27.** New workflow `claude-pool-sweep.yml` stays far below 480,000 bytes. `review_autofix_sweep.yml` is not edited.
- **§28.C protected paths.** Every `.claude/**` edit is done twin-first in `workflow-templates/.claude/**`, followed by the `[claude-twin-sync]` copy.
  - `claude-issue-pickup.md` has no twin (coding-workflows only). It is edited in place in a watched session, or through the twin-sync hold.
  - Hooks and `settings.json` changes wait for the operator (master-session Q62/Q64).
- **§14.** Consumer repos receive the `.claude/` changes through the existing sync. No new consumer wrapper is needed: the sweep runs in coding-workflows and reads consumers with `GH_PAT`, as `claude_pr_sweep.py` does.

## Approach

**Account identity.** Each account's cloud environment sets `CLAUDE_POOL_ACCOUNT=<name>` (`^[a-z0-9-]{1,32}$`). Sessions created with `create_session` inherit the caller's environment, so the pickup, checkers, stages and fixers of one account all carry it, including sessions working in consumer repos. When it is unset, every pool step is skipped (Q20).

**Leases as git refs** (`.claude/scripts/claude_pool.py`, twin in `workflow-templates/.claude/scripts/`). A unit of work owns one ref. The ref points at a chain of empty-tree commits whose message is one JSON line:

```
{"v":1,"unit":"stage/<slug>","account":"<name>","session":"session_…","state":"working|waiting|released","expires_at":"<RFC3339>","wip":"<sha|null>","resume":"<— resume. block, stage units only>","plan":"<path>","updated_at":"<RFC3339>"}
```

- **Create:** `POST git/trees` (empty tree, idempotent), then `POST git/commits` (no parent), then `POST git/refs`. A 422 "Reference already exists" means someone else holds it: result `taken`.
- **Renew / steal:** `POST git/commits` with parent = the lease sha the caller read, then `PATCH git/refs/<ref>` with `force: false`. GitHub rejects a non-fast-forward, so exactly one of two racing updaters wins (compare-and-swap). A steal is allowed only when the read lease is `released` or past `expires_at`.
- **Release:** a compare-and-swap update to `state: released`, never a `DELETE` (a delete could remove someone else's newer lease). The sweep deletes released refs older than 24 h.
- **Namespaces** (ref prefix `CLAUDE_POOL_REF_PREFIX`, default `refs/pool/`):
  - `queue/<queue issue n>` in coding-workflows: the pickup's claim on a queue item.
  - `issue/<n>` in the target repo: `/implement-issue-claude` before its first stage lease.
  - `stage/<slug>` in the target repo: one per `/implement-plan-claude` project, held by the working stage or the waiting checker.
  - `wip/<unit>`: checkpoints, pushed with `git push origin HEAD:refs/pool/wip/<unit path>`. Not a branch, so no workflow runs.
  - `accounts/<name>` in coding-workflows: account health (`used_5h`, `used_7d`, `resets_5h`, `resets_7d`, `exhausted_until`, `heartbeat_at`).
  - `summary` in coding-workflows: written by the sweep, with live leases per account across all repos. The pickup reads it with 2 REST calls instead of reading every consumer repo.
- **PR fixes keep today's claim comments.** They add a `kind=release` marker so a fixer that hits a limit hands the head back immediately.

**Reading usage** (`claude_pool.py usage`), sources in order:

1. `~/.claude/pool/usage.json`, written by a new statusline command `.claude/hooks/pool_usage_snapshot.py` from `rate_limits`. It counts only when fresh (≤ 15 min).
2. `exhausted_until`, taken from:
   - the account's health ref, written by the new `StopFailure` hook when it fires with `error_type: rate_limit`, parsing `resets …` from the message; or
   - the pickup's own `list_sessions` (`mine: true`, first page) scan for `status_detail` containing `hit your` and `limit`.
3. Unknown, which counts as under 90% (Q11).

**The gate** (`claude_pool.py gate`) returns `slots`:
- `0` when `used_5h ≥ 90` or `used_7d ≥ 90` (`CLAUDE_POOL_GATE_PERCENT`, default 90) or `now < exhausted_until`.
- Otherwise `max(0, CLAUDE_POOL_MAX_ACTIVE − live leases held by this account)`, counted from `refs/pool/summary` plus the queue claims this wake made.

**Round-robin.** Each account runs its own pickup (hourly, anchored to the minute it was created, so accounts wake at different minutes). Each wake starts at most `min(slots, limit)` items. Before each `create_session`, the pickup claims `queue/<n>`; a `taken` result skips the item. So accounts share the queue in wake order, and none takes more than its free slots.

**Keeping lost work small.**
- Stages push a checkpoint after each finished implementation step and renew their lease with `wip=<sha>`.
- The `StopFailure` hook releases the session's leases and posts `kind=release` fix claims the moment a limit hits. When it doesn't fire, the 3-hour lease expiry is the backstop (Q14).
- Checkers renew the project lease on every hourly wake, so a waiting project never looks dead while its checker lives.

**Takeover** (`claude-pool-sweep.yml`, cron `*/15 * * * *`, coding-workflows only, `scripts/claude_pool_sweep.py`):

1. One GraphQL query per registered repo lists `refs/pool/*` with commit messages. From that the sweep writes `refs/pool/summary` and deletes released refs older than 24 h.
2. An expired `stage/<slug>` lease (not released) becomes one `claude_stage.v1` queue item. The payload carries `repo`, `slug`, `plan`, `lease` sha; the resume block stays in the lease and is not copied into the issue. An expired `issue/<n>` lease becomes one `claude_issue.v1` item with `trigger: takeover`. Both are bound with the existing `claude-issue-queue-binding` artifact.
3. It runs `scripts/claude_pr_sweep.py --min-age-hours 0.5` (Q7/Q15): PR fixes due for 30 minutes with no live claim are queued. Its existing dedupe never queues a PR twice.

`QUEUE_PRODUCERS` gains `claude-pool-sweep.yml` (event `schedule`, `workflow_dispatch`) for `pr_fix`, `stage` and `issue` items, through a new `workflows` tuple read alongside the old `workflow` key.

**Adoption.**
- The pickup starts a `claude_stage.v1` item as `/implement-plan-claude <plan> — resume.` followed by the lease's resume block and a line `Pool takeover: lease <sha> wip <sha|none>`, on Opus 5.5 at high effort.
- In step 0 the stage steals the lease (compare-and-swap from the sha named). If that fails, it stops: someone else adopted it.
- Resume hygiene treats `get_session`, `archive_session` and `delete_trigger` failures on ids from another account as not found. A `Checker session` it cannot read is "not reusable", so the existing Arming-the-wait logic creates a checker in the adopting account.
- For a `working` lease, the stage fetches `refs/pool/wip/<unit>` and continues from it.
- For a `waiting` lease, it runs `check_in_status.py` once. `wait` re-arms the wait with a new checker; anything else starts the named next stage as usual.

**Why not other designs.**
- Comment claims aren't atomic.
- Checkpoints on `claude/**` branches start reviewer runs.
- A session-side sweep would die with its account's limit.
- A single shared pickup is impossible across accounts.

## Decisions

The operator's answers are in the table under Context. These are the design choices the plan makes on top of them.

### D1 — Leases live in the target repo, accounts and queue claims in coding-workflows
- **Chosen:** `refs/pool/stage/*`, `issue/*` and `wip/*` in the repo the work happens in; `queue/*`, `accounts/*` and `summary` in coding-workflows.
- **Alternatives considered:** every ref in coding-workflows.
- **Why:** stage and fixer sessions in a consumer repo can only reach the repos attached to them. A consumer session writing to coding-workflows would need `add_repo` on every stage.

### D2 — Release is a compare-and-swap to `released`, never a delete
- **Chosen:** `state: released` through a fast-forward update; the sweep deletes released refs after 24 h.
- **Alternatives considered:** `DELETE git/refs/...` on release.
- **Why:** a delete cannot check what it removes. It could delete a lease another account took a moment earlier.

### D3 — The pickup reads a sweep-written summary instead of every consumer repo
- **Chosen:** `refs/pool/summary`, written every 15 minutes by `claude-pool-sweep.yml`.
- **Alternatives considered:** the pickup reading `refs/pool/*` in every registered repo.
- **Why:** the pickup session reaches only coding-workflows through the proxy. The summary costs it 2 REST reads, at most 15 minutes stale.

### D4 — The fast PR takeover reuses `claude_pr_sweep.py`
- **Chosen:** `claude-pool-sweep.yml` runs the existing script with `--min-age-hours 0.5`, and `QUEUE_PRODUCERS` gains the new workflow.
- **Alternatives considered:** a second PR-fix sweep implementation; changing the cron of `review_autofix_sweep.yml`.
- **Why:** one decision function (`check_pr_hand_back`) for every sweep, and the existing queue dedupe stops double queueing. Changing the cron of `review_autofix_sweep.yml` would run its other jobs every 15 minutes too.

## Phases & Merge Strategy

Every phase is its own PR into the project branch and is safe to merge in any order.
- **Library check.** Phases 2–6 check `test -f .claude/scripts/claude_pool.py` (`scripts/claude_pool_sweep.py` imports it and exits 0 with `CLAUDE_POOL skip reason=library_missing` when absent). Command-file steps say "skip this step when `.claude/scripts/claude_pool.py` is missing". Without the library every phase leaves today's behaviour intact.
- **Account check.** With `CLAUDE_POOL_ACCOUNT` unset, every session-side step is skipped (Q20).

1. **Measurements and signals.**
   - **Scope:** a statusline usage snapshot, a `StopFailure` logger, a session token-usage reporter, a live test that custom refs can be written, and the Sonnet comparison.
   - **Files:** `.claude/hooks/pool_usage_snapshot.py` [new], `.claude/hooks/pool_stop_failure.py` [new], `.claude/scripts/session_token_usage.py` [new], their twins, `settings.json` (`statusLine`, `StopFailure` hook, allow rules), `docs/operations/claude-pool-measurements.md` [new], tests.
   - **Done:** the hooks write only under `~/.claude/pool/` and never block (tests). The measurement stage has recorded, in `claude-pool-measurements.md`:
     - whether `rate_limits` appeared in a cloud session;
     - whether a push to `refs/pool/wip/test-<ts>` and a `git/refs` create under `refs/pool/` succeed through the session proxy (then deleted);
     - tokens per wake for both Sonnets.
   - **Rollback:** revert the PR. The files are additive, and the hooks write only local files.
2. **Leases and atomic queue claims.**
   - **Scope:** `claude_pool.py` (`claim`, `renew`, `steal`, `release`, `read`, `lease-json`), plus pickup step 3 claiming `queue/<n>` before `create_session`.
   - **Files:** `.claude/scripts/claude_pool.py` [new] and twin, `.claude/commands/claude-issue-pickup.md`, `settings.json` allow rules, `tests/test_claude_pool.py` [new], ci.yml step.
   - **Done:** unit tests cover create/422, fast-forward compare-and-swap, the steal rules, release, and the unset-account no-op. One live check: two concurrent `claim --unit queue/test-<ts>` calls; exactly one `claimed`, then released.
   - **Rollback:** revert. The pickup step is skipped when the library is missing.
3. **Usage gate, per-account cap, health ref.**
   - **Scope:** `claude_pool.py usage|gate|health`, plus a pickup gate step before step 3 and the health write at the end of each wake.
   - **Files:** `claude_pool.py`, `claude-issue-pickup.md`, tests.
   - **Done:** tests cover each threshold edge (89.9 / 90.0), a stale snapshot, `exhausted_until` from a status_detail string, the slot arithmetic, and output unchanged with the env var unset.
   - **Rollback:** revert. The pickup starts as today.
4. **Stage leases, checkpoints, takeover sweep, adoption.**
   - **Scope:**
     - `implement-plan-claude.md`: lease writes at step 3a and every hand-off; checker renewal; checkpoint pushes; adoption in step 0 / step 2; foreign-account tolerance.
     - `scripts/claude_pool_sweep.py` [new] and `.github/workflows/claude-pool-sweep.yml` [new].
     - `claude_issue_route.py`: `claude_stage.v1` build/parse, `workflows` producers.
     - `claude-issue-dispatch.md`: stage payload parse and start.
     - Pickup: `item_type` `stage` starts.
   - **Done:** sweep tests (expired → queued once and bound; live, released or legacy → skipped; `CLAUDE_POOL_ENABLED=false` → report-only), route tests, and command-text tests. One live takeover in coding-workflows with a test project lease forced expired: a second account's pickup starts it, and it steals and resumes.
   - **Rollback:** revert. Leases already written expire and are cleaned by nothing until re-merge; they are inert refs.
5. **Issue-start leases and fast PR-fix takeover.**
   - **Scope:**
     - `implement-issue-claude.md` writes `issue/<n>` and releases it when the stage lease exists.
     - The sweep queues expired issue leases (`trigger: takeover`) and runs `claude_pr_sweep.py --min-age-hours 0.5`.
     - `read_fix_claims` honours `kind=release`; the `StopFailure` hook posts it for heads this session claimed.
     - `claude_fix_claim.py` accepts `--kind release`.
   - **Files:** the above plus `check_in_status.py`, tests.
   - **Done:** tests for `release` (lifts only the same claimant's claim on the same head), `takeover` trigger parse/sort (after `reclarify`), and the 30-minute window.
   - **Rollback:** revert. The 2-hour hourly sweep still covers PRs.
6. **Sonnet default, docs, runbook.**
   - **Scope:** set the checker and pickup model to the measured cheaper Sonnet, or keep `claude-sonnet-5` when the measurements file has no result.
     - Files: CLAUDE.md §26.B/§26.C model lines, `implement-plan-claude.md` checker, `claude-issue-pickup.md` `arm-check-in`, agents.md, README.
     - Add a README "Multi-account pool" section and an agents.md contract section.
     - Replace the master handbook's "Usage limit stops" playbook with a pointer to the pool.
   - **Done:** tests assert that the model string matches in every changed file, and the full suite passes.
   - **Rollback:** revert. The model reverts to `claude-sonnet-5`.

## Implementation Steps

**Phase 1 — measurements and signals**
1. `.claude/hooks/pool_usage_snapshot.py` [new] (and twin).
   - Reads statusline JSON on stdin and prints a one-line status (`5h 42% · 7d 63%`, or the model name when `rate_limits` is absent).
   - Atomically writes `~/.claude/pool/usage.json` (`used_5h`, `used_7d`, `resets_5h`, `resets_7d`, `seen_at`) only when `rate_limits` is present.
   - Never raises: any error prints the fallback line and exits 0. No network.
2. `.claude/hooks/pool_stop_failure.py` [new] (and twin), wired under `StopFailure` with matcher `rate_limit`.
   - Appends the payload (`error_type`, `error_message`, `session_id`, time) to `~/.claude/pool/stop-failures.jsonl`.
   - Parses `resets <time>` into `exhausted_until`.
   - When `.claude/scripts/claude_pool.py` exists and `CLAUDE_POOL_ACCOUNT` is set, runs `claude_pool.py on-limit --until <t>` (phase 2/3/5 behaviour) with a 20 s timeout.
   - Exits 0 always.
3. `.claude/scripts/session_token_usage.py` [new] (and twin).
   - Sums `usage.input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` and `output_tokens` from the session transcript (`~/.claude/projects/*/<session>.jsonl`, located from `CLAUDE_CODE_REMOTE_SESSION_ID`), and prints one JSON line.
   - Docstring states it makes no API calls.
4. `workflow-templates/.claude/settings.json` twin gets:
   - `statusLine` = `{"type":"command","command":"python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/pool_usage_snapshot.py"}`;
   - the `StopFailure` hook;
   - allow rules for `session_token_usage.py` (plain and `PYTHONDONTWRITEBYTECODE=1` forms).
   This is operator-gated (§28.C, master-session Q62/Q64).
5. The measurement run, performed by the phase's stage session with claude-code-remote tools. No script is left behind (§18.A).
   - Create two throwaway sessions, `claude-sonnet-5` and `claude-sonnet-5-5`, each with the two-step `/effort low` start.
   - Send each the same instructions three times, one minute apart: run `check_in_status.py --repo shubhodeep1/coding-workflows --pr <this phase PR> --hand-back`, then `session_token_usage.py`, and report both lines.
   - Also ask each to report whether `~/.claude/pool/usage.json` exists, and to try `git push origin HEAD:refs/pool/wip/probe-<ts>` and then delete it (`git push origin :refs/pool/wip/probe-<ts>`).
   - Archive both sessions. Record the per-wake token totals (mean of wakes 2–3, so the first-wake cache fill does not count) and the probe results in `docs/operations/claude-pool-measurements.md`.
   - A comparison counts only when both models complete three wakes. Otherwise record `inconclusive`.
6. Tests:
   - `tests/test_pool_usage_snapshot.py` [new]: present/absent `rate_limits`, malformed stdin, atomic write.
   - `tests/test_pool_stop_failure.py` [new]: reset parsing for "resets 3:45pm", "resets Mon 12:00am" and an ISO time; library missing → no subprocess.
   - `tests/test_session_token_usage.py` [new]: fixture transcripts.
   - Each hook's test also asserts its `settings.json` wiring in both copies, the per-hook pattern of `tests/test_pr_watch_guard.py`.
   - A ci.yml step running them.
7. `changelog.d/<pr>-pool-usage-signals.md` [new].

**Phase 2 — leases and atomic queue claims**
1. `.claude/scripts/claude_pool.py` [new] (and twin), subcommands `claim`, `renew`, `steal`, `release`, `read`, `lease-json`. All take `--repo`, `--unit`; writes take `--by <session id>`.
   - The lease format is as in Approach; expiry is `now + CLAUDE_POOL_LEASE_HOURS` (default 3).
   - It exits 0 on success, 1 on `taken` / `moved` / `live`, 2 on a read/write failure, and prints one JSON line.
   - It is a no-op (exit 0, `{"pool":"off"}`) when `CLAUDE_POOL_ACCOUNT` is unset or invalid.
   - **Budget docstring (§15):** `claim` = 1 tree POST (cached per process) + 1 commit POST + 1 ref POST; `renew` / `steal` / `release` = 1 ref read + 1 commit read + 1 commit POST + 1 ref PATCH; `read` = 1 ref read + 1 commit read.
   - `gh api` is called with `-R`-free absolute paths `repos/<owner>/<repo>/git/...`.
2. Pickup step 3.0 (new, before 3.1), for each pending entry when the library exists and the account is set:
   - `claude_pool.py claim --repo shubhodeep1/coding-workflows --unit queue/<first queue issue n> --by <pickup session id>`.
   - `taken` → skip the entry this wake (`skipped_taken += 1`).
   - After a successful 3.2 close, `release`. After a failed start (3.3), also `release`, so the next wake of any account can retry the item.
   - The report line adds `pool=<account|off>` and `taken=<k>`.
3. `settings.json` twin allow rules for `claude_pool.py` (both forms).
4. `tests/test_claude_pool.py` [new] with a fake `gh` shim on `PATH` (the pattern in `tests/test_claude_pr_sweep.py`): create-then-422, compare-and-swap loser, steal of a live lease refused, steal of an expired lease allowed, release, env unset.
5. ci.yml step; `changelog.d/<pr>-pool-leases.md`.

**Phase 3 — usage gate, cap, health**
1. `claude_pool.py usage`:
   - merges the sources in Approach order;
   - `--status-details-file <f>` takes the pickup's saved `list_sessions` output (only `status_detail` and `updated_at` are read), and it parses `You've hit your (session|weekly) limit · resets <t>`;
   - reads `refs/pool/accounts/<name>` in coding-workflows (2 REST reads).
2. `claude_pool.py gate --wake-started <n>` → `{"slots":k,"reason":"…","used_5h":…,"used_7d":…}`. Live leases come from `refs/pool/summary` (2 REST reads); a missing summary counts 0.
3. `claude_pool.py health` → compare-and-swap-upserts `refs/pool/accounts/<name>` with the usage result and `heartbeat_at`.
4. Pickup step 2b (new, after step 2's queue read):
   - `list_sessions` (`mine: true`, `limit: 20`), saved to the scratchpad, then `gate`;
   - `slots = 0` → start nothing (step 3 skipped, `pool=gated (<reason>)`);
   - otherwise cap step 3 at `slots` entries.
   - Step 4 adds `health` once per wake.
   - The pickup still wakes hourly when gated: it costs one short Sonnet turn and is what notices the reset.
5. Tests for the gate edges, source precedence and parsing; changelog fragment.

**Phase 4 — stage leases, checkpoints, sweep, adoption**
1. `implement-plan-claude.md` (twin first):
   - **Step 3a:** `claude_pool.py claim --unit stage/<slug> --state working --plan <path>`.
   - **Every hand-off ("Arming the wait"):** `renew --state waiting --resume-file <scratch file with the full — resume. block>` (Q6). The same block is also written to the progress log's `Waiting on:` section, as today.
   - **Step 0 of every `— resume.` stage:** `steal` when the block names `Pool takeover: lease <sha>`, otherwise `renew --state working`. A `moved` / `live` result stops the stage with `Status: pool conflict` and no work.
   - **After each finished implementation step:** `git push origin HEAD:refs/pool/wip/stage/<slug>` as its own Bash call, then `renew --wip <sha>`. When the phase-1 probe recorded that the push is refused, it skips the push and renews without `wip`.
   - **Project end (step 12 LIVE / 13):** `release`.
   - **Checker prompt step 1b:** `renew --unit stage/<slug> --state waiting` after `check_in_status.py`, as its own Bash call.
   - **Resume hygiene:** any refusal or not-found from `get_session` / `archive_session` / `delete_trigger` on an id named in the block is recorded and ignored; a checker that cannot be read is "not reusable".
   - **Adoption branch in step 2:** fetch the `wip` ref and check it out on the phase branch when it is a descendant of the branch tip. For a `waiting` lease, run `check_in_status.py` once and re-arm or proceed.
2. `scripts/claude_pool_sweep.py` [new].
   - **Batching contract (§15):**
     - input: the registry plus this repo;
     - calls per run: 1 GraphQL query per repo (`refs(refPrefix:"refs/pool/", first:100)` with commit `message`); per new queue item 1 issue POST (queue token); 1 summary compare-and-swap (4 REST calls); 1 DELETE per released ref older than 24 h;
     - output: `CLAUDE_POOL_SWEEP` lines;
     - fail open per repo.
   - It dedupes against open queue items (the same read `claude_pr_sweep.queued_pr_fixes` does, extended to `stage` / `issue` titles) and writes the binding file.
   - `CLAUDE_POOL_ENABLED=false` → report-only.
3. `.github/workflows/claude-pool-sweep.yml` [new]:
   - `schedule: */15 * * * *` and `workflow_dispatch` (input `dry_run`); `concurrency: claude-pool-sweep` (cancel-in-progress false).
   - The env mirrors the catch-all job (`GH_TOKEN: GH_PAT`, `CLAUDE_POOL_SWEEP_QUEUE_TOKEN: github.token`, `CLAUDE_POOL_ENABLED: vars.CLAUDE_POOL_ENABLED || 'true'`, binding file path).
   - Steps: the pool sweep, then (phase 5) the PR sweep with `--min-age-hours ${{ vars.CLAUDE_POOL_PR_MIN_AGE_HOURS || '0.5' }}`, then upload the binding artifact `if: always()`.
4. `scripts/claude_issue_route.py`:
   - `build_stage_text` / `parse_stage_text` (`claude_stage.v1`: `repo`, `slug` `^[a-z0-9-]{1,80}$`, `plan` path under `docs/`, `lease` 40-hex);
   - `stage_queue_title`;
   - `QUEUE_PRODUCERS` entries gain `"workflows": (<old>, "claude-pool-sweep.yml")`, with `evaluate_producer_run` accepting any of them (the old `"workflow"` key is kept, §6);
   - `queue-pending` emits `item_type: stage`, ordered after `reclarify` resumes and before new issues.
5. `claude-issue-dispatch.md` (twin): stage payload parse rules and the start prompt `/implement-plan-claude <plan> — resume.` plus the lease's resume block (read with `claude_pool.py read`) plus the `Pool takeover:` line. Title `<numbers>implement-plan <slug> — takeover`.
6. The pickup starts `stage` entries through dispatch step 2 (gate and queue claim as in phases 2–3).
7. Tests: `tests/test_claude_pool_sweep.py` [new], `tests/test_claude_issue_route.py` additions, `tests/test_implement_plan_claude_command.py` assertions for each new step; ci.yml step; changelog fragment.

**Phase 5 — issue leases, fast PR takeover, release claims**
1. `implement-issue-claude.md` (twin): `claim --unit issue/<n>` at its first step, `release` once the stage lease exists. When the claim is `taken` by a live lease, stop (another account is on it).
2. The pool sweep queues expired `issue/<n>` leases as `claude_issue.v1` with `trigger: takeover`. `parse_fire_text` and dispatch accept `takeover`; the sort puts it after `reclarify`.
3. `claude_fix_claim.py` accepts `--kind release`. `read_fix_claims` treats a `release` by the same claimant on the same head as ending that claimant's claim. The `StopFailure` hook (`claude_pool.py on-limit`) posts `release` for each head this session claimed (recorded in `~/.claude/pool/claims.jsonl` by `claude_fix_claim.py post`).
4. `claude-pool-sweep.yml` gains the `claude_pr_sweep.py --min-age-hours` step.
5. Tests: `test_check_in_status_hand_back.py` (release semantics), `test_claude_pr_sweep.py` (0.5 h window), route tests (`takeover`); changelog fragment.

**Phase 6 — Sonnet default, docs**
1. Read `docs/operations/claude-pool-measurements.md`. When it names a winner with a ≥ 5% lower mean token total, change `claude-sonnet-5` to it in:
   - CLAUDE.md §26.B step 2;
   - `implement-plan-claude.md` Check-in Loop step 1;
   - `claude-issue-pickup.md` step 5.2;
   - agents.md;
   - README (the pickup setup line).
   Otherwise keep `claude-sonnet-5` and record `unchanged (<reason>)`.
2. README "Multi-account pool" section:
   - **Setting up an account:**
     1. Open the account's cloud environment settings and set `CLAUDE_POOL_ACCOUNT`.
     2. Connect GitHub as the same user (Q2).
     3. Run `/claude-issue-pickup start` from a new app session in Auto mode on Sonnet `/effort low`.
   - The env vars, log prefixes and failure modes.
3. agents.md "Multi-account pool contract": ref namespaces, lease JSON, the compare-and-swap rules, the payload formats, and the budget per call.
4. `docs/operations/master-session.md`: replace "Usage limit stops" steps with a pointer to the pool and to what still needs a human (an account whose pickup was never started).
5. Tests updating model-string assertions; changelog fragment.

## Files & Modules

- `.claude/hooks/pool_usage_snapshot.py` [new], `workflow-templates/.claude/hooks/pool_usage_snapshot.py` [new]
- `.claude/hooks/pool_stop_failure.py` [new], `workflow-templates/.claude/hooks/pool_stop_failure.py` [new]
- `.claude/scripts/session_token_usage.py` [new], twin [new]
- `.claude/scripts/claude_pool.py` [new], twin [new]
- `.claude/scripts/claude_fix_claim.py`, twin
- `.claude/scripts/check_in_status.py`, twin
- `.claude/settings.json`, `workflow-templates/.claude/settings.json`
- `.claude/commands/claude-issue-pickup.md` (no twin)
- `.claude/commands/claude-issue-dispatch.md`, `implement-plan-claude.md`, `implement-issue-claude.md`, and their twins
- `scripts/claude_pool_sweep.py` [new]
- `scripts/claude_issue_route.py`
- `.github/workflows/claude-pool-sweep.yml` [new]
- `.github/workflows/ci.yml` (test steps)
- `CLAUDE.md` (§26.B model line only, phase 6)
- `README.md`, `agents.md`, `docs/operations/master-session.md`
- `docs/operations/claude-pool-measurements.md` [new]
- `tests/test_pool_usage_snapshot.py` [new], `tests/test_pool_stop_failure.py` [new], `tests/test_session_token_usage.py` [new], `tests/test_claude_pool.py` [new], `tests/test_claude_pool_sweep.py` [new]
- `tests/test_claude_issue_route.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_claude_pr_sweep.py`, `tests/test_implement_plan_claude_command.py`
- `changelog.d/<pr>-*.md` [new], one per phase

## Data Model / Index Changes

None. No MongoDB collection is touched (§10 N/A). The only new persistent state is git refs under `refs/pool/` in coding-workflows and registered consumer repos, described in Approach.

## Tests

- **Unit:** every new script and hook has its own test file (listed above), run in its own ci.yml step, with `PYTHONDONTWRITEBYTECODE=1` (§13). `gh` is faked with a PATH shim, never called live in CI.
- **Command-text tests:** each changed command's new steps and the unset-account skip are asserted in the existing `test_*_command.py` pattern.
- **Live checks (in the phase stage sessions, recorded in their PRs):**
  - phase 1: the probes and the Sonnet measurement;
  - phase 2: the concurrent claim;
  - phase 4: one forced takeover between two accounts. It needs a second account's pickup running; when none is, the phase records `live takeover: pending second account` and the unit tests carry the done condition.
- **Regression:** the full existing suite with `CLAUDE_POOL_ACCOUNT` unset.

## Risks & Mitigations

- **Terms of service.** Rotating personal Pro/Max accounts around plan limits may conflict with Anthropic's Consumer Terms ("bypassing any of our systems or protective measures") and the "ordinary, individual usage" note, and could lead to suspension. ACCEPTED — operator decision Q1: C, 2026-09-30.
- **Statusline in cloud sessions.** `rate_limits` may never reach a cloud session. ACCEPTED — pending phase 1 probe. Fallback: reactive limit detection (Q11) and the per-account cap.
- **`StopFailure` coverage.** It may not fire on session or weekly limits in cloud sessions. ACCEPTED — pending phase 1. Fallback: the 3-hour lease expiry.
- **Git proxy and custom refs.** The proxy may refuse `refs/pool/*` pushes, or GitHub may refuse the namespace. ACCEPTED — pending phase 1 probe. Fallback: no WIP checkpoints (Q13 B), and `CLAUDE_POOL_REF_PREFIX` can move leases to `refs/heads/pool-lease/`. No workflow triggers on those branches (only `internal-review.yml` has a `push` trigger, limited to `claude/**`).
- **Sonnet comparison.** It may be inconclusive. ACCEPTED — pending phase 1; `claude-sonnet-5` stays.
- **Slow takeover.** A stage lost to a limit is taken over up to ~4 h later: the 3-hour lease, plus up to 15 min of sweep, plus up to 1 h of pickup. It is faster only when `StopFailure` releases the lease. ACCEPTED — Q14: B.
- **Slow session mistaken for dead.** A slow but alive stage may lose its lease after 3 h without a checkpoint. Mitigations: renewal at every checkpoint, and a compare-and-swap check before each push. A stage that finds its lease moved stops without pushing.
- **Stale sessions after reset.** Sessions in the old account may resume after its limit resets: a hand-back Routine firing into a pushing session, or a checker woken by a trigger. Mitigation: every such path already re-reads the PR and claims first, and a stage session re-checks the lease in step 0, so a stale session stops.
- **Every account runs out.** Nothing runs; leases expire; the queue waits. The watchdog alerts after 3 h as today.
- **Pickup edits collide with `retire-master-session-plan.md`.** That plan adds pickup steps 3a/3b/3c. This plan uses 2b and 3.0 and touches no other step; conflicts resolve by keeping both.
- **30-minute takeover on by default.** It applies to every repo (Q15/Q17). A pushing session whose checker wakes hourly will often be beaten by a fresh fixer, losing its context advantage. ACCEPTED — Q7/Q17; `CLAUDE_POOL_ENABLED=false` or `CLAUDE_POOL_PR_MIN_AGE_HOURS` restores it.
- **Operator approval windows.** `settings.json` and hook changes need the operator's approval window (§28.C, master-session Q62/Q64) and may hold phases 1–3.
- **Actions minutes.** `claude-pool-sweep.yml` runs 96 times a day. Each run is one short job making about 1 GraphQL call per repo.

## Rollout

- **Merge order is free.** Each phase is inert without the library, and without `CLAUDE_POOL_ACCOUNT` on the session side.
- **Consumer repos** get the `.claude/` changes on the next `@stable` sync (§14). Their sessions stay out of the pool until the account's environment sets `CLAUDE_POOL_ACCOUNT`.
- **Actions side** is on by default (Q17). `CLAUDE_POOL_ENABLED=false` turns the pool sweep into report-only and stops the 30-minute PR takeover.
- **Operator steps, one per account:**
  1. Set `CLAUDE_POOL_ACCOUNT` in the account's cloud environment.
  2. Connect GitHub as `shubhodeep1`.
  3. Start that account's pickup.
  These are account setup, not code, so they stay with the operator (§18 applies to recurring operations).
- **Rollback:** revert the phase PR. Leases left behind are inert refs and are deleted by the sweep, or by hand with `git push origin :refs/pool/...`, only if the sweep is also reverted.

## References

- `docs/operations/master-session.md` ("Usage limit stops", ownership)
- `docs/plans/retire-master-session-plan.md` (pickup steps 3a–3c)
- CLAUDE.md §15, §18, §20, §23.H, §25, §26, §27, §28
- `.claude/commands/claude-issue-pickup.md`, `claude-issue-dispatch.md`, `implement-plan-claude.md`, `fix-claude-pr.md`, `implement-issue-claude.md`
- `scripts/claude_issue_route.py`, `scripts/claude_pr_sweep.py`, `.claude/scripts/check_in_status.py`, `.claude/scripts/claude_fix_claim.py`
- Issues #4525 (routines cannot start sessions), #4621 (queue binding), #4990 (pickup limit and catch-up)
- https://code.claude.com/docs/en/statusline, /hooks, /errors, /routines, /claude-code-on-the-web, /legal-and-compliance
- https://docs.github.com/en/rest/git/refs (create, update with `force: false`)

# Stateless per-repo PR status master checker

## Summary

Replace the one-Sonnet-checker-per-PR design of CLAUDE.md §26 with **one
stateless master checker session per (claude.ai account, repository)**. On
each hourly tick it rebuilds its list of PRs from `list_triggers` and runs a
deterministic decision script, so hourly cost is F + history instead of
N·F + history. It also survives its own death: the pickup session, every
subscriber, and every arming session can each recreate it, and the new
master picks up every open PR again because it keeps no state of its own.

## Context

- **Today (CLAUDE.md §26.B/C):** every pushed PR gets its own
  `PR #<n> status check-in` Sonnet session at low effort. Each one re-arms
  itself with `send_later` every 60 minutes, runs
  `.claude/scripts/check_in_status.py --hand-back`, and pulls forward the
  pushing session's `PR #<n> hand-back` Routine when a fix is due or the PR
  is terminal. It keeps a subscriber list (trigger id, session id,
  fixer/notify role) and the "already handed back" dedup state **in its
  conversation**.
- **Cost driver:** every wake reloads the fixed context. `CLAUDE.md` alone
  is 105,878 bytes, about 26k tokens, before the harness prompt and tool
  schemas. The hourly gap outlives the prompt cache (agents.md,
  `/implement-plan-claude` stage-session notes), so each wake pays for its
  input almost uncached. With N open PRs the hourly cost is N·F + ΣHᵢ; one
  master costs F + min(ΣHᵢ, compaction cap).
- **Precedents in this repo:**
  - `/implement-plan-claude` already moved from one checker per stage to
    **one checker per project**, after hitting the 8-parent-link depth limit
    (`caller session is at lineage depth 8 (limit 8)`, 2026-09-25,
    `.claude/commands/implement-plan-claude.md` → Check-in Loop).
  - `/claude-issue-pickup` is a single long-lived session woken by a
    **recurring cron Routine bound to itself** (`Claude issue pickup:
    hourly`, `cron_expression 0 * * * *`). It stays at depth ≤3 for life,
    and its "keep exactly one pickup" step converges duplicates.
- **Hard platform limits:**
  - GitHub Actions cannot create claude.ai sessions or use the Routines API
    (issue #4525; agents.md "Stale Routine sweep"), so only a live session
    can recreate another session.
  - `list_sessions` / `list_triggers` are per claude.ai account, and
    `update_trigger` on another account's Routine fails as not-found. The
    operator uses **several claude.ai accounts on the same repos**, all
    connected to the same GitHub user (Q15: A).
  - `fire_trigger` ignores `persistent_session_id` (verified 2026-09-25), so
    hand-backs stay scheduled fires (`update_trigger` `run_once_at`), never
    manual fires.
  - Checkers asked to rewrite another session's Routine prompt refused
    (observed 2026-09-25), so the master never rewrites prompts. It changes
    only `run_once_at`, `name` and `enabled`.
- **Backstops that stay unchanged:**
  - the Actions `claude-pr-catch-all` job
    (`.github/workflows/review_autofix_sweep.yml`, cron `17 * * * *`,
    `scripts/claude_pr_sweep.py`), which is account-neutral and claim-aware;
  - the claim protocol (`.claude/scripts/claude_fix_claim.py`,
    `ai:claude-fix-claim:v1`);
  - the `CLAUDE_ISSUE_QUEUE_WATCHDOG` Telegram alert for a dead pickup.

### Decisions taken in clarification (2026-09-27)

| Q | Decision |
|---|---|
| Q1 | Master tick = a **recurring cron Routine** bound to the master; there is no `send_later` chain |
| Q2 | Recreation layers: **A** (pickup revives masters) **+ B** (each subscriber's hand-back is a short dead-man's switch; a woken subscriber recreates the master when its own depth is ≤3). The dead-man window is **1h** (not 3h) |
| Q3 | One master per **repository** (per account). It goes dormant (tick disabled) when idle, and an arming session or the pickup re-enables it |
| Q4 | Orphan PRs (no subscriber visible in this account) are left to the Actions catch-all |
| Q5 | Role goes as a token in the hand-back prompt (latest-created fixer wins). Dedup state goes in the Routine **name** (`… hand-back: sent <state> <sha7>`), and the stale sweep keeps `sent` Routines for 6h |
| Q6 | The decisions come from a new script, `.claude/scripts/check_in_master.py`; the model only executes its action list |
| Q7 | Delivery is checked on the **next hourly tick**; there is no extra 10-minute wake |
| Q8 | Terminal report with the pushing session gone: fallback next steps come from the hand-back prompt and are delivered by PushNotification |
| Q9 | The `/implement-plan-claude` project checker is unchanged |
| Q10 | Cutover by role-token discriminator plus a single flip phase; untagged (legacy) hand-backs stay with their per-PR checker |
| Q11 | Per-PR checker creation is removed after the probes pass and 14 clean days, with a `docs/scripts-pending-removal.md` entry |
| Q12 | Implemented with `/implement-plan-claude` (the probes need claude-code-remote tools) |
| Q13 | Five phases, each safe in any merge order through a runtime gate |
| Q14 | Harness assumptions are accepted as `ACCEPTED — pending P2 probe results`. A failed probe blocks the flip |
| Q15 | All accounts connect the same GitHub user |
| Q16 | Two accounts' masters may both hand back the same PR; claims already stop duplicate fixes |
| Q17 | Dead-man renewal = **now + 90 min** on every tick (1h window plus 30 min for late ticks) |

## Goals

- G1. In master mode a repo with N open PRs pushed from one account costs
  **one** master wake per hour (the cron tick), not N. Verified by the P2
  probe and by counting `last_run` fires of the tick Routine against the
  number of PR hand-backs.
- G2. The master keeps no required state in its conversation: after a
  forced `/compact` (probe P-3), or after replacing the master with a new
  session, the next tick produces the same action list for the same inputs.
  This is proven by `tests/test_check_in_master.py`, which gives the script
  only files, and by probe P-3.
- G3. A dead, archived, or deleted master is recreated or re-enabled
  without a human, by at least two independent paths: the pickup within
  ≤60 min, and any live shallow subscriber within ≤90 min. Every PR with an
  enabled role-tagged hand-back Routine is monitored again on the new
  master's first tick.
- G4. Hand-back semantics are unchanged: due fixes go only to the fixer,
  terminal verdicts go to every subscriber, a gone fixer leads to a fresh
  Opus fixer at high effort, and a gone fixer on a terminal PR leads to the
  fallback PushNotification. Only the transport and timing change (Q7:
  delivery confirmation ≤1 tick later).
- G5. Legacy per-PR checkers and their Routines keep working untouched
  until their PRs end. Master and legacy never act on the same hand-back
  Routine.
- G6. The master mode can be switched off without a code revert, by
  setting the gate file verdict to anything other than `PASS`.

## Non-goals

- Any change to the `/implement-plan-claude` project checker, its Routines
  (`implement-plan <slug>: …`), or `/implement-issue-claude` (Q9).
- Handling orphan PRs (PRs with no subscriber visible in the master's
  account) in the master. The Actions catch-all keeps that job (Q4).
- Cross-account coordination: a GitHub-side master registry, or one master
  seeing another account's Routines (Q16).
- Fixing the existing risk that two accounts could each run a
  `/claude-issue-pickup` (see Risks). It is recorded, not planned.
- Any change to `review_autofix.yml`, `claude_pr_sweep.py`,
  `claude_fix_claim.py`, or the claim, lease, and cap semantics (§26.H).
- Moving the hourly read into GitHub Actions. Actions cannot reach the
  Routines API (#4525).

## Constraints

- **§6 naming immutability.** Every existing name is kept:
  - Routine names `PR #<n> hand-back`, `PR #<n> status check-in…`;
  - the session title `PR #<n> status check-in`;
  - the hand-back prompt prefix `CLAUDE.md §26 hand-back for PR #<n> (<URL>)`;
  - script CLIs (`check_in_status.py`, `stale_routines.py`);
  - §26 subsection letters A–H.

  New behaviour is **added**: a new §26.I, new optional prompt tokens, and
  new name suffixes that keep the old prefixes. `stale_routines.py` keeps
  matching legacy names.
- **New identifier uniqueness (§6).** These new identifiers were checked
  against the repo with `grep`, and none exists today:
  - `check_in_master.py`
  - `check_in_master.json`
  - `PR status master:`
  - `Role: fixer` / `Role: notify`
  - `Via: master`
  - `hand-back: sent`
  - `test_check_in_master.py`
  - `docs/probes/`

  An implementer must re-run that check before merging each phase.
- **§15 GitHub API budget.**
  - The master adds **no** per-PR GitHub calls beyond what the per-PR
    checkers made: it runs the same `check_pr_hand_back` reads (one PR read
    plus, on a `claude/*` head, the comment and check-run pages and at most
    5 more) once per PR per hour.
  - `stale_routines.py` read budget is unchanged.
  - The pickup revival adds 0 GitHub calls. It uses claude.ai
    `list_triggers` with `enabled: true` (already called in pickup step 1),
    plus one `list_triggers` with `enabled: false` (to see dormant ticks)
    and one `list_sessions`.
- **§18.C supervisor.** The master is a long-running supervisor. Its
  lifecycle and wiring are specified in Approach §A.7, and its registry
  entry in P4.
- **§18.F.** P4 adds registry entries for the master supervisor
  (`permanent — review annually`) and for the legacy per-PR checker path
  (removal trigger = P5 preflight). P5 deletes the legacy entry.
- **§20.** Each phase that changes what consumers receive on `@stable`
  ships one `changelog.d/<n>-<slug>.md` fragment.
- **§14 / propagation.**
  - `CLAUDE.md`, `.claude/scripts/*`, `.claude/hooks/*`,
    `.claude/commands/fix-claude-pr.md` and `.claude/settings.json` each
    have a byte-identical copy under `workflow-templates/` that must stay
    identical (verified today with `cmp`).
  - They reach every repo in `.github/ai/consumer_repos.json` (13
    consumers) through the `Sync .claude/ assets from upstream` step of
    `.github/workflows/update_workflows.yml`.
  - `/claude-issue-pickup` exists only in coding-workflows (no template
    copy).
- **§25.** The master never subscribes to PR activity. It is a scheduled
  check-in, the kind §25.C allows. `pr_watch_guard.py` is untouched.
- **§27.** No workflow file grows.
- **§21.** Merged-PR guard applies to every phase branch as usual.
- **Auto mode.** The master, like today's checkers, must run in Auto mode.
  Outside it the claude-code-remote write tools prompt on every call
  (agents.md "Permissions"), and Haiku cannot run in Auto mode, so the
  master is Sonnet (`claude-sonnet-5`, `/effort low` two-step start,
  CLAUDE.md §26.B step 2).

## Approach

### A.1 Identities (all new, alongside the old)

| Object | Value |
|---|---|
| Master session title | `<owner>/<repo> PR status master` |
| Master tick Routine | name `PR status master: <owner>/<repo>` (≤60 chars; computed for all 13 repos in `.github/ai/consumer_repos.json` plus coding-workflows, the longest, `shubhodeep1/drhyg_ecommerce_automation`, gives 56; a P1 test asserts ≤60 for every listed repo), `cron_expression: 0 * * * *` (anchored by the server to the creation minute), `persistent_session_id` = master, `initiation: own_followup`, prompt `CLAUDE.md §26.I master tick for https://github.com/<owner>/<repo>. Follow CLAUDE.md §26.I in this session.` |
| Master-mode hand-back Routine | name `PR #<n> hand-back` (unchanged), `run_once_at` = now + 90 min, prompt `CLAUDE.md §26 hand-back for PR #<n> (<PR URL>). Role: <fixer\|notify>. Via: master. Fallback next steps: <one paragraph, ≤600 chars>. Continue with CLAUDE.md §26.D in this session.` The prefix up to `(<PR URL>)` is byte-identical to today's, so `stale_routines.py`'s `HAND_BACK_PROMPT_PATTERN` still matches |
| First-tick trigger | one-shot, name `PR status master: first tick` (28 chars; the repo is in its prompt, which is the tick prompt), `run_once_at` = now + 2 min. It has no `cron_expression`, so keeper election (A.4 step 1) never counts it |
| Delivered-hand-back marker | at pull-forward the master renames the Routine to `PR #<n> hand-back: sent <state> <sha7>` (e.g. `PR #4588 hand-back: sent review-round 1a2b3c4`, ≤47 chars) |
| Gate file | `.claude/check_in_master.json` = `{"verdict": "PENDING" \| "PASS" \| "FAIL" \| "DISABLED", "probes": "docs/probes/master-checker-probes.md", "updated": "<date>"}`, with a byte-identical `workflow-templates/.claude/check_in_master.json` |

A hand-back Routine is **master-owned** only when all of these hold:

- its prompt contains both `Role: fixer|notify` and `Via: master`;
- its name is exactly `PR #<n> hand-back` (or the `… hand-back: sent …`
  marker form), and `<n>` equals the pull number in the prompt's PR URL;
- that URL's `<owner>/<repo>` is the master's `--repo`;
- it has a non-empty `persistent_session_id` that is not the master's own
  session, and an empty `cron_expression`.

Anything else is ignored: a legacy hand-back (G5), an unrelated Routine
that happens to contain the tokens, or a malformed one. The trust boundary
is the account itself. `list_triggers` returns only Routines created by
sessions of the master's own claude.ai account, so a Routine from another
person can never be read, and the checks above keep an own-account Routine
for another repo or PR from being consumed or renamed.

### A.2 Master mode gate (runtime)

`python3 .claude/scripts/check_in_master.py --gate` prints
`{"master_mode": true|false, "reason": …}`. Master mode is on only when
the script exists (a missing script means a shell error, which is read as
off) **and** the gate file's `verdict` is `PASS`. Every prose branch in
§26 (arming, `/fix-claude-pr` step 7, §26.D re-arm, the hook reminder, and
pickup revival) runs `--gate` first and uses the legacy §26.B/C path when
it is off. This is what makes every phase safe in any merge order (Q13),
and it gives an operator kill switch (`"verdict": "DISABLED"`) that syncs
to consumers (G6).

### A.3 Master tick procedure (new §26.I, executed by the master)

1. Load the claude-code-remote tools. Fetch the triggers with two reads,
   saving each to a file (the harness usually saves large results itself):
   - `list_triggers` with `enabled: true`, `limit: 100`, following
     `next_cursor` up to 5 pages;
   - `list_triggers` with `enabled: false`, `include_completed: true`,
     `limit: 100`, first page only.
2. Run the script:
   `PYTHONDONTWRITEBYTECODE=1 CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=<login> CLAUDE_FIXER_VERDICT_BOT_LOGIN=<bot or empty> python3 .claude/scripts/check_in_master.py --tick --repo <owner>/<repo> --self <own session id> --enabled <file1> --ended <file2>`.
   It prints one JSON action list. Its logic is in A.4.
3. Execute the actions in the order printed, with only the listed MCP
   calls:
   - `renew` → `update_trigger` with `run_once_at` only;
   - `pull_forward` → `update_trigger` with `run_once_at` = now + 1 min and
     `name`, never the prompt;
   - `fresh_fixer` → the §26.C step 5 two-step Opus 5.5 `/effort high`
     start plus the `PR #<n> status check-in: fixer start` trigger;
   - `fallback_notify` → first `update_trigger` with only `name` = the
     item's `new_name` (`… sent <state> <sha7> fb`) on the marker Routine,
     then one `PushNotification` per item, plus a line in the reply. The
     rename comes first so a crash between the two calls can't cause a
     second notification. If the rename fails, skip that item's
     notification and list it in `errors`; the next tick retries it;
   - `dedupe` → `delete_trigger` / `archive_session` on duplicate masters;
   - `stale_delete` → `delete_trigger`, ignoring not-found;
   - `dormant: true` → `update_trigger` with `enabled: false` on its own
     tick Routine.
4. Reply in one line (`master tick <repo>: <k> PRs, renewed <r>, handed
   back <h>, fresh fixers <f>, fallbacks <b>, errors <e>`). Call
   `set_session_title` **on every tick**, with the value the script
   prints in `title`: `<owner>/<repo> PR status master — last tick <HH:MM>
   UTC: <k> PRs, <e> errors`. It is always set and never compared with an
   earlier value, so the tick carries no memory, and the timestamp is the
   liveness signal the Risks section relies on. End the turn. Never archive
   yourself, never `sleep`, never fix anything, never comment on a PR.

The procedure has no step that depends on earlier turns, so auto-compaction
cannot change its outcome (G2).

**Ticks never overlap within one master.** A session runs one turn at a
time, and a Routine that fires while the master is still mid-tick is queued
behind that turn (probe P-1 verifies this). A queued tick is simply a
second, later tick. It starts from fresh reads and fresh files, and every
action is idempotent against what the first tick did:

- a Routine the first tick pulled forward already carries the
  `… sent <state> <sha7>` name, and A.4 step 2 treats it as pending
  delivery, so it is neither renewed nor pulled forward again;
- renewals are re-computed from `next_run_at`;
- `fb` renames make `fallback_notify` one-shot.

Two masters for the same repo (created in a race) converge through keeper
election (A.4 step 1) on their next ticks. Until then they can at worst both
renew the same Routine, which is harmless, or both pull it forward, which is
at most one extra wake that the fixer's claim then absorbs (Q16 accepts the
same effect across accounts). A tick has no time limit. It makes at most
one Bash call plus one MCP call per action, so a slow tick only delays the
queued next one.

### A.4 `check_in_master.py --tick` decision rules (deterministic)

The inputs are the two trigger files, `--repo`, and `--self`. The script
imports `check_pr_hand_back` and `read_fix_claims` from `check_in_status.py`
in the same directory, so there is no second implementation of the
PR-state logic. It calls
`check_pr_hand_back(repo, n, stuck_hours=DEFAULT_STUCK_HOURS (6.0), min_age_hours=0.0, now, ignore_claim_by=())`,
the same values the per-PR checker uses today (§26.C step 1). That keeps
the 6-hour stuck window on `claude/implement-plan-*` heads and adds no age
delay. The master never claims, so it ignores no claims. The sweep's
`CLAUDE_PR_SWEEP_MIN_AGE_HOURS` (2h) is not applied here.

1. **Own tick and duplicates.** Take the enabled **recurring** Routines
   (non-empty `cron_expression`) named `PR status master: <repo>` whose
   prompt names this repo URL. The
   **keeper** is the one with the oldest `created_at`, ties broken by `id`.
   - If the keeper is not bound to `--self`, output
     `{"superseded": true}`. The master then deletes its own tick Routine,
     sets its title to `… (superseded)`, and ends.
   - Otherwise output a `dedupe` entry for every other tick (delete the
     trigger and archive its session).
2. **Subscribers.** Collect the enabled master-owned hand-back Routines
   (A.1 ownership rules), grouped by PR.
   - The **fixer** is the newest `created_at` among `Role: fixer`
     Routines, ties broken by the lexically greatest `id` (the same kind of
     tie-breaker as keeper election, so the choice is deterministic). Any
     other fixer-role Routine is treated as notify (Q5).
   - An enabled Routine already named `… hand-back: sent <state> <sha7>`
     is a hand-back **pending delivery**. A queued or duplicate tick ran
     after the pull-forward but before the fire, so it is neither renewed
     nor pulled forward again.
   - Legacy (untagged) Routines are skipped (G5).
3. **Sent markers.** Collect the ended Routines named
   `PR #<n> hand-back: sent <state> <sha7>` for this repo, with their
   `last_run` (`status`, `fired_at`, `session_id`) and
   `persistent_session_id`.
4. **Per PR with subscribers or a sent marker less than 6h old**, call
   `check_pr_hand_back` once. By the verdict's `state`:
   - `open` / `claimed` / `held` / waiting → `renew` every subscriber
     Routine whose `next_run_at` is before now + 90 min, setting it to
     exactly now + 90 min (Q17; no jitter, so the chosen window holds).
     When the master dies, its subscribers can therefore wake at about the
     same moment. `--ensure` (A.5 step 2) absorbs that stampede by treating
     a master session with the right title whose `created_at` is ≤10 min
     old as `ok`, even before its tick Routine is visible. Any duplicates
     that two exactly simultaneous creations still produce collapse through
     keeper election on their first ticks, about 2 min later thanks to the
     first-tick trigger.
   - `conflict` / `review-round` / `ci-failed` / `blocked`:
     - If a sent marker for the same `state` and `head_sha[:7]` shows
       `ROUTINE_RUN_STATUS_SUCCEEDED` into its own bound session, it was
       delivered and the fixer did not act, so it is treated as `open`
       (renew only; the Actions catch-all covers a fixer that did not
       act, as §26.C step 4 does today).
     - If that marker shows `ROUTINE_RUN_STATUS_FAILED`, or the session is
       gone, output `fresh_fixer` for `(pr, kind, head_sha)`, but only
       when no live fixer Routine exists.
     - Otherwise, if a fixer Routine exists, output `pull_forward` for it
       with the new name `PR #<n> hand-back: sent <state> <sha7>`, and
       `renew` the notify Routines.
     - With no fixer Routine at all, nothing is output (the catch-all
       covers it, Q4).
   - `merged` / `closed` → `pull_forward` every subscriber with the name
     `… sent <state> <sha7>`. On a later tick, for a terminal sent marker
     whose `last_run` is FAILED or whose session is gone and whose role is
     `fixer`, output `fallback_notify` with the `Fallback next steps` text
     parsed from that Routine's prompt (Q8). The item carries the marker's
     trigger id and `new_name` = `… sent <state> <sha7> fb`, which A.3 step
     3 applies before notifying. A marker whose name already ends in ` fb`
     is never notified again, so each PR gets its fallback once.
   - Read failure (exit-2 equivalent, for example a GitHub API outage) →
     **renew anyway**, list the failure in `errors`, and take no other
     action for that PR. The 90-min switch exists to detect a *dead
     master*. A master that is alive but can't read GitHub must not page
     every pushing session: a sustained outage would otherwise wake N
     sessions every 90 min. Persistent read errors show in the master's
     reply and title (`<e> errors`). For `claude/*` PRs the Actions
     catch-all still acts once reads recover. This departs on purpose from
     today's §26.C step 3, which withholds renewal on a read failure but has
     7 days of slack; a 90-min window has none.
5. **Dormancy.** When no subscriber is enabled for this repo and no sent
   marker is less than 6h old, output `dormant: true`.
6. **Stale sweep.** Call `stale_routines.classify` on the ended file and
   output its `delete` ids as `stale_delete`, so the master runs §26.G
   every tick with no extra reads beyond its own PR-read cache. The call
   is wrapped: if `classify` raises (for example on a malformed entry),
   the script outputs `stale_delete: []`, adds `stale sweep failed:
   <exception>` to `errors`, and still prints the rest of the action list
   with exit 0. The sweep is best-effort, the same as §26.G's "a failed
   sweep never blocks"; the renewals must never be lost because of it.

Output schema, exit codes (0 verdict / 2 unreadable input), and the API
budget are documented in the module docstring, per §15 "document the
batching contract".

### A.5 Arming in master mode (replaces §26.B steps 1–4 when the gate is on)

0. Run the stale sweep (§26.G) and keep the `list_triggers` file from it.
1. Create this session's hand-back Routine with the A.1 master-mode prompt,
   `run_once_at` = now + 90 min, and the role `fixer` when this session
   pushed, else `notify`. There is no subscriber message: the master
   discovers the Routine on its next tick.
2. **Ensure a live master** with
   `check_in_master.py --ensure --repo <r> --enabled <file> --disabled <file> --sessions <list_sessions file>`.
   The inputs come from three claude.ai reads:
   - `--enabled`: `list_triggers` with `enabled: true`, `limit: 100`;
   - `--disabled`: `list_triggers` with `enabled: false`, `limit: 100`,
     following `next_cursor` up to 5 pages. This is the only listing that
     contains a **dormant** master's tick Routine, because dormancy sets
     `enabled: false` (A.3 step 3);
   - `--sessions`: `list_sessions` with `mine: true`, `limit: 100`.

   A dormant tick is a recurring (`cron_expression` non-empty) Routine
   named `PR status master: <repo>`, with `enabled: false`, **no**
   `ended_reason`, and a `persistent_session_id` that `--sessions` shows
   as a non-archived, non-failed session. A disabled tick that has an
   `ended_reason` (for example `auto_disabled_session_gone`), or whose
   session is archived, failed, or missing, is not a dormant master. It
   counts as no master. When several dormant ticks qualify, the oldest
   `created_at` wins, ties broken by `id` (keeper rule). The script
   outputs one of:
   - `ok` — the tick is enabled and bound to a non-archived session, or a
     non-archived session titled `<owner>/<repo> PR status master` was
     created ≤10 min ago (a creation still in progress, possibly by
     another subscriber woken at the same moment);
   - `enable <trig id>` — the master exists and is dormant; `<trig id>` is
     the dormant tick's id from `--disabled`. Call `update_trigger` on it
     with `enabled: true`. If that call fails, the session reports it in
     one line; the next arming session, a subscriber's switch, or the
     pickup retries;
   - `create` — no master, or it is archived or failed. Create it in
     three calls:
     1. a master session (Sonnet, `/effort low` two-step start, title
        `<owner>/<repo> PR status master`, `permission_mode` = this
        session's mode);
     2. the tick Routine (A.1);
     3. the one-shot `PR status master: first tick` trigger (A.1), so the
        first check doesn't wait up to an hour.

     **Partial failure:**
     - If call 1 fails, stop and report it in one line.
     - If call 2 fails, retry it once. If it still fails, `archive_session`
       the new master, so no master sits without a tick, and report it.
       The next arming session, a subscriber's switch, or the pickup
       retries the whole `create`.
     - If call 3 fails, that doesn't matter: the tick Routine fires within
       60 minutes anyway, and the failure is reported in one line.

     None of these steps retries in a loop.

     A session only creates a master when its own depth is **≤3**. The
     depth is found by walking `parent_session_id` with `get_session`, the
     pickup's method, and the walk stops:
     - at the first session with no `parent_session_id` (the root; depth
       found);
     - on a `get_session` error;
     - on a repeated id (a cycle, or a parent equal to the current id);
     - after 8 calls.

     Every stop other than the root means **depth unknown**, which is
     treated as >3. When the depth is too high or unknown, the session
     does not create a master: it relies on the pickup (A.6) and says so in
     one line.
3. Report the hand-back trigger id and the master's session id.

### A.6 Recreation layers (Q2: A+B, 1h window)

- **Arming sessions:** A.5 step 2 runs on every new arm and every
  `/fix-claude-pr` re-registration.
- **Subscribers as dead-man's switches (B):** a live master renews every
  subscriber to now + 90 min on each tick. If the master is dead, the
  Routine fires within ≤90 min in the subscriber session. §26.D's master
  branch then:
  1. runs `check_in_status.py --hand-back` itself;
  2. if the PR is still open (or `claimed` / `held`), recreates its
     hand-back Routine (A.5 step 1) and runs A.5 step 2 (create or enable,
     depth ≤3);
  3. if a fix is due and this session is the fixer, it follows
     `/fix-claude-pr` in place, exactly as for a normal hand-back. That
     command's step 7 then re-arms it and ensures the master;
  4. if the PR is **terminal** (`merged` / `closed`), the wake *is* the
     terminal hand-back. The session follows §26.D's terminal branch
     unchanged: the report, self-rename, one `PushNotification`, and the
     stale sweep. It creates no hand-back and ensures no master for this
     PR, because nothing is left to watch; the next arm for another PR
     ensures the master if one is needed. So a PR that ends while the
     master is dead still gets its report within ≤90 min.

  A false fire caused by a very late tick finds `ok` and only re-arms. An
  archived subscriber fires into nothing (`auto_disabled_session_gone`),
  which is expected.
- **Pickup (A):** a new pickup step, `1b. Revive PR status masters`, reuses
  the `list_triggers` result of pickup step 1 (it lists `enabled: true`).
  It adds one `list_triggers` call with `enabled: false` (following
  `next_cursor` up to 5 pages, so dormant ticks are visible) and one
  `list_sessions` call. It then runs
  `check_in_master.py --revive-scan --enabled <file> --disabled <file> --sessions <file>`,
  which, for every repo with at least one enabled master-owned hand-back,
  outputs `ok` / `enable` / `create` (A.5 step 2 rules). The pickup is at
  depth ≤3, so a master it creates is at ≤4 and the master's fresh fixers
  are at ≤5, under the limit of 8. The pickup covers **only its own
  account**; other accounts rely on the arming-session and subscriber
  layers.
- **Backstops:** the Actions catch-all (claude/* fixes, all accounts) and
  `CLAUDE_ISSUE_QUEUE_WATCHDOG` (pickup death).

### A.7 Supervisor lifecycle (§18.C)

| Item | Value |
|---|---|
| Entry point | The master session, woken by its recurring `PR status master: <repo>` Routine; the procedure is CLAUDE.md §26.I |
| Restart policy | Recreated by A.6 (arming sessions, subscriber dead-man's switches every ≤90 min, the pickup every hour) |
| Shutdown | Dormancy (tick `enabled: false`) when idle; `archive_session` by a keeper (`dedupe`); an operator stops it by archiving the session and deleting its tick Routine, or by setting the gate to `DISABLED` |
| Crash recovery | No state to recover: the next master rebuilds from `list_triggers`. A PR whose subscribers all disappeared is covered by the Actions catch-all |
| Wiring | Created by A.5 / A.6 with no operator action; §26.I in `CLAUDE.md` is synced to consumers |

### A.8 Unchanged parts and their relationship to the master

- **`/implement-plan-claude` and `/implement-issue-claude`:** the master
  ignores `implement-plan …` Routines. `/fix-claude-pr` keeps skipping
  step 7 on `claude/implement-plan-*` heads.
- **Claims:** a fixer woken by `pull_forward` claims the head as today.
  The claim is what makes the next tick see `claimed`, and what stops a
  second account's fixer (Q16).
- **§26.D in master mode:**
  - The fixer does **not** rename or archive the checker (the master is
    shared) and does **not** delete its fired Routine. The master's sent
    marker needs it for 6h, and `stale_routines.py` removes it afterwards.
  - Everything else in §26.D stands: the report, the self-rename, one
    PushNotification, and no self-archive.

### Alternatives considered

- **Keep per-PR checkers.** Reliable, but costs N·F per hour.
- **A `send_later` master.** Rejected (Q1): a lost re-arm silently stops
  every PR's check-in.
- **One account-wide master.** Rejected (Q3): it needs `add_repo` for every
  consumer and still cannot see other accounts.
- **Store state in PR marker comments.** Rejected (Q5): it adds GitHub
  writes and comment noise.
- **Rotate the master periodically.** Rejected: each rotation adds a
  parent link, so it hits the 8-link limit.
- **The master handles orphans.** Rejected (Q4): with several accounts,
  each account's master would see the other's PRs as orphans.

## Phases & Merge Strategy

Every phase is safe in any merge order because master mode needs both
`check_in_master.py` **and** a `PASS` gate (A.2). Until both exist, every
path stays legacy. Implemented with `/implement-plan-claude` (Q12). P2 must
run in a stage session because it needs claude-code-remote tools.

1. **P1: decision script, stale-sweep rules, gate file (inert).**
   - Files: `check_in_master.py` [new] + template copy;
     `stale_routines.py` + template copy; `.claude/check_in_master.json`
     [new] (`"verdict": "PENDING"`) + template copy; tests; the `ci.yml`
     step; the `check_in_status.py` docstring (a new caller is noted; no
     code change); changelog fragment.
   - Done: all tests green; `--gate` prints `master_mode: false`; no prose
     references the script yet.
   - Rollback: revert the PR. Nothing calls it.
2. **P2: harness probes and the gate verdict.**
   - Files: `docs/probes/master-checker-probes.md` [new] (method, raw
     observations, PASS/FAIL/INCONCLUSIVE per probe, date, account);
     `.claude/check_in_master.json` + template copy (verdict `PASS` only
     if every probe is PASS, else `FAIL`; the file is created if P1 has
     not merged); changelog fragment.
   - Done: every probe in the Tests section is recorded, and the gate
     verdict matches.
   - Rollback: set the verdict to `DISABLED` or revert. With P1 absent, a
     PASS gate is inert anyway.
   - Under CLAUDE.md §28.C, an INCONCLUSIVE or FAIL probe is a
     **failure escalation**: the chain stops at `Status: BLOCKED` and
     asks. It is never auto-decided.
3. **P3: pickup revives masters.**
   - Files: `.claude/commands/claude-issue-pickup.md` (new step 1b; it
     runs `--gate` first and skips when off or when the script is missing);
     agents.md / README.md pickup paragraphs; changelog fragment.
   - Done: with the gate off, the pickup's reply and title are unchanged
     (verified by reading its next wake). With the gate on and a
     master-owned hand-back present, it creates or enables the master.
   - Rollback: revert the PR.
4. **P4: the flip (master mode prose and hook).**
   - Files:
     - `CLAUDE.md` + template copy: new §26.I; a master-mode branch in the
       §26 intro and §26.A/B/D; §26.C labelled "legacy per-PR checker
       (used when `--gate` is off)"; new names in §26.E/G.
     - `.claude/hooks/pr_check_in_reminder.py` + template copy, and its
       test.
     - `.claude/commands/fix-claude-pr.md` step 7 + template copy.
     - agents.md, README.md.
     - `docs/scripts-pending-removal.md`: two entries.
     - changelog fragment.
   - Done: `tests/test_claude_md_section_numbers.py` and the hook tests
     are green, templates are byte-identical, and the §26.I text matches
     A.3–A.6.
   - Rollback: set the gate to `DISABLED` (instant, synced), or revert.
     Master-owned Routines that already exist keep being served by their
     master until their PRs end. A new arm under the legacy path ignores
     them.
5. **P5: remove per-PR checker creation.**
   - Files: `CLAUDE.md` + template copy (the §26.B legacy creation steps
     are replaced by a pointer to §26.I; §26.C is kept only as "how
     running legacy checkers finish"); hook text; agents.md, README.md;
     delete the legacy registry entry; changelog fragment.
   - Done: the registry preflight checks pass (see Rollout).
   - Rollback: revert the PR.
   - **Independence:** P5's preflight requires the gate to be `PASS` and
     14 days of master mode, so it can never merge ahead of P4 in a
     broken state. The P5 stage re-checks the preflight daily through a
     one-shot self Routine (`implement-plan <slug>: P5 preflight`, a
     §25.C scheduled check-in) for at most 30 days, and after that stops
     at `BLOCKED`.
   - **When the 30 days run out:** nothing has been removed yet, because
     P5's PR is only opened after the preflight passes. The legacy per-PR
     path stays documented and working, and master mode keeps running.
     `BLOCKED` is a §28.C failure escalation: the stage records which
     preflight check (a)–(e) still fails, with its observed value, in the
     progress log and the report, sends one `PushNotification`, and asks in
     §2 format:
     - **A** — resume for another 30 days with a `— resume.` block
     - **B** — drop P5, keeping legacy creation as a documented alternate
       path
     - **C** — fix the failing cause first

     The chain can complete without P5 (option B) and still leave the
     system in the P4 state.

## Implementation Steps

### P1

1. Create `.claude/scripts/check_in_master.py` with the `--gate`,
   `--tick`, `--ensure` and `--revive-scan` modes (A.2, A.4, A.5, A.6).
   - Import `check_in_status` and `stale_routines` from its own directory
     (`sys.path.insert(0, os.path.dirname(__file__))`).
   - Tabs, and opening braces are N/A in Python (§9).
   - Structured one-line JSON output with the keys `renew`,
     `pull_forward`, `fresh_fixer`, `fallback_notify`, `dedupe`,
     `stale_delete`, `dormant`, `superseded`, `errors`, `title` (each
     `fallback_notify` item also carries `trigger_id` and `new_name`).
   - Exit 2 on unreadable input.
2. `stale_routines.py`:
   - (a) Add `PR status master: ` to `is_ours`.
   - (b) Keep an ended Routine named `PR #<n> hand-back: sent …` until 6h
     after its `last_run.finished_at` (falling back to `fired_at`, then
     keep if both are missing). This is a new read of the `last_run`
     field; update the docstring's field list.
   - (c) Delete an ended `PR status master: …` Routine, as today's
     ended-Routine rule already does.
   - Never delete an enabled master tick.
3. Add `.claude/check_in_master.json` (`PENDING`).
4. Copy all three byte-identically to `workflow-templates/.claude/`.
5. Add `tests/test_check_in_master.py`, covering:
   - gate on/off/missing-file/malformed;
   - keeper election and superseded;
   - role parsing, latest-fixer-wins, and the equal-`created_at`
     tie-breaker by `id`;
   - the A.1 ownership rules: legacy Routines ignored, and each of these
     rejected: a wrong repo in the URL, a name/URL PR-number mismatch, a
     tokens-only Routine with another name, the master's own session, and
     a recurring Routine;
   - a first-tick one-shot never elected keeper;
   - each state → action mapping, including the sent-marker dedup,
     FAILED → fresh_fixer, an enabled `sent` Routine (pending delivery)
     neither renewed nor pulled forward, and terminal → pull_forward-all
     then `fallback_notify` exactly once: it carries `new_name` ending in
     ` fb`, and a marker already ending in ` fb` gets no second item;
   - renew only when `next_run_at` < now + 90 min, to exactly now + 90 min;
   - `--ensure` treats a correctly titled master session created ≤10 min
     ago as `ok` (stampede absorption);
   - the `check_pr_hand_back` call arguments (`stuck_hours=6.0`,
     `min_age_hours=0.0`, no ignored claims);
   - dormancy;
   - **outage cascade**: when every PR read fails, every subscriber is
     still renewed, `errors` lists each PR, and there are no
     `pull_forward` / `fresh_fixer` / `fallback_notify` actions. A master
     that is alive therefore never trips the switches during a GitHub
     outage;
   - idempotence: running `--tick` twice on the inputs as they stand after
     the first run's actions yields no new `pull_forward` or
     `fallback_notify`;
   - `title` output format;
   - the tick Routine name ≤60 characters for every repo in
     `.github/ai/consumer_repos.json` plus coding-workflows;
   - `--ensure` / `--revive-scan` outputs, including an archived or failed
     master session → `create`;
   - dormant-master revival: a disabled recurring tick in `--disabled`
     (no `ended_reason`, live session) → `enable` with **that** tick's id,
     never `create`; a disabled tick with `ended_reason` set, or whose
     session is archived or missing → `create`; two dormant ticks → the
     keeper's id; the same dormant tick absent from `--disabled` (the old
     enabled-only input) → `create`, which documents why the input is
     required;
   - the stale-sweep pass-through, and `classify` raising → `stale_delete:
     []`, an `errors` entry, the renewals still printed, exit 0.

   `check_pr_hand_back` is stubbed; there is no network.
6. Extend `tests/test_stale_routines.py` for rules (a) to (c), and add a
   byte-identity assertion for the three template copies if no existing
   test covers it. `stale_routines.py` does **not** parse the new
   `Role:` / `Via:` / `Fallback next steps:` tokens: it treats master and
   legacy hand-backs the same way, by name and by the unchanged
   `HAND_BACK_PROMPT_PATTERN` prefix, whose non-greedy match takes the
   first PR URL, which is the Routine's own. Add a test case with a
   master-format prompt (including a fallback text that contains another
   PR URL). It must classify exactly like the legacy prompt for the same
   PR: an enabled master hand-back is deleted only when its PR finished
   more than 24h ago, the same as legacy.
7. Add a `ci.yml` step running `tests/test_check_in_master.py`, next to the
   existing `test_stale_routines.py` step (ci.yml ~L427).
8. Add a docstring line in `check_in_status.py` naming the new caller. No
   code change.
9. Add `changelog.d/<pr>-master-checker-scripts.md` (`added`).

### P2

1. In a stage session (Auto mode, claude-code-remote tools), run probes
   P-1 to P-7 (see Tests). Use disposable sessions titled
   `probe master-checker <n>` and Routines named
   `probe master-checker: …`. Delete and archive every one of them at the
   end, and record their ids.
2. Write `docs/probes/master-checker-probes.md`.
3. Set the gate verdict and its template copy.
4. Add a changelog fragment (`changed`).

### P3

1. In `claude-issue-pickup.md`, insert step `1b` after step 1:
   - save the step-1 `list_triggers` result (`enabled: true`) to a file;
   - call `list_triggers` with `enabled: false`, `limit: 100` (up to 5
     pages) and save it as the `--disabled` file;
   - call `list_sessions` (`mine: true`, `limit: 100`);
   - run `check_in_master.py --gate`, then `--revive-scan`;
   - execute `enable` / `create` (A.5 step 2, with `source_url` =
     `https://github.com/<repo>`);
   - add the counts to the step-4 one-line report (`masters: ok <a>,
     enabled <b>, created <c>`).

   The step is skipped when the gate is off or the script is missing.
2. Update the agents.md / README.md pickup paragraphs.
3. Add a changelog fragment.

### P4

1. In `CLAUDE.md`:
   - add §26.I "Master checker (master mode)" with A.1, A.3, A.5, A.6 and
     A.7;
   - in §26.A/B/D, add "when `check_in_master.py --gate` reports
     `master_mode: true`, follow §26.I instead of steps 1b–4 / the
     per-checker steps";
   - label §26.C as legacy;
   - add the new names to §26.G;
   - add the master-mode reminder to §26.E.

   Keep all letters and numbers (§6). Then run `cmp` against the
   template.
2. In `pr_check_in_reminder.py`, extend `REMINDER`: "run
   `check_in_master.py --gate`; if master mode, create a master-mode
   hand-back (90 min, Role/Via tokens, fallback next steps) and ensure the
   master per §26.I; else …existing text…". Update its test and the
   template copy.
3. In `fix-claude-pr.md` step 7, add the master-mode branch (new hand-back
   Routine plus ensure-master, and no subscriber message). Update the
   template copy.
4. Update agents.md "Interactive post-push PR status check-in" and the
   README `.claude/` sync note plus "Claude fixes every claude/* PR".
5. Add two entries to `docs/scripts-pending-removal.md`:
   - the master supervisor: `permanent — review annually`;
   - the legacy per-PR checker path (`CLAUDE.md §26.B/C`): the Rollout
     preflight.
6. Add a changelog fragment (`changed`) with the numbers table (cost F ≈
   26k tokens per wake, 90-min window, 6h marker keep).

### P5

1. Run the preflight (Rollout); if it fails, schedule the daily re-check.
2. Edit the `CLAUDE.md` legacy creation steps, the hook text and the
   docs, then the template copies.
3. Delete the legacy registry entry.
4. Add a changelog fragment (`removed`).

## Files & Modules

- `.claude/scripts/check_in_master.py` [new] and
  `workflow-templates/.claude/scripts/check_in_master.py` [new]
- `.claude/check_in_master.json` [new] and
  `workflow-templates/.claude/check_in_master.json` [new]
- `.claude/scripts/stale_routines.py` and its template copy
- `.claude/scripts/check_in_status.py` and its template copy (docstring
  only)
- `.claude/hooks/pr_check_in_reminder.py` and its template copy
- `.claude/commands/fix-claude-pr.md` and its template copy
- `.claude/commands/claude-issue-pickup.md`
- `CLAUDE.md` and `workflow-templates/CLAUDE.md`
- `agents.md`, `README.md`
- `docs/scripts-pending-removal.md`
- `docs/probes/master-checker-probes.md` [new]
- `tests/test_check_in_master.py` [new], `tests/test_stale_routines.py`,
  `tests/test_pr_check_in_reminder.py`
- `.github/workflows/ci.yml` (one test step)
- `changelog.d/<pr>-*.md` [new], one per phase

## Tests

**Unit (CI):**
- `tests/test_check_in_master.py` (P1 step 5);
- `tests/test_stale_routines.py` extensions;
- `tests/test_pr_check_in_reminder.py` (new reminder text and the
  unchanged matcher);
- `tests/test_claude_md_section_numbers.py` (section numbering intact).

**Harness probes (P2, recorded in `docs/probes/master-checker-probes.md`,
run in the implementing account):**

- **P-1 Mid-turn delivery.** Pull a Routine forward into a target session
  while that session is mid-turn on a command running ≥3 minutes (for
  example the full pytest suite). PASS when `last_run` is SUCCEEDED and the
  target processes the prompt as its next turn, with nothing dropped.
- **P-2 Recurring bound cron.** A `cron_expression: 0 * * * *` Routine
  bound to a probe session fires 3 consecutive ticks into the **same**
  session id with tools intact. The stage waits with a `send_later` of
  about 200 min into itself. Also record the delays (`fired_at` minus the
  scheduled time) to validate the 30-min grace (Q17). PASS when all 3 fire
  and the maximum delay is under 30 min.
- **P-3 Compaction.** Deliver `/compact` to a probe master between two
  ticks. PASS when the post-compaction tick produces a byte-identical
  action list for identical input files.
- **P-4 `list_triggers` shape.** Check that `persistent_session_id`,
  `created_at`, `next_run_at`, `enabled`, `ended_reason` and `last_run`
  (`status`, `fired_at`, `finished_at`, `session_id`) are present, that
  the `enabled` filter and `next_cursor` pagination work, and record the
  ordering. Also check that a recurring Routine disabled with
  `update_trigger` (`enabled: false`, a dormant tick) appears in
  `list_triggers` with `enabled: false` and no `ended_reason`, **without**
  `include_completed`, since `--ensure` and `--revive-scan` rely on that.
- **P-5 Rename and enable.** `update_trigger` with `name`, and
  `enabled: false`/`true`, works on a Routine created by **another**
  session of the same account, and on a recurring Routine. `update_trigger`
  with `name` also works on an **ended** one-shot Routine (fired,
  `ended_reason: run_once_fired`), which the `fb` rename (A.3 step 3)
  depends on. After the rename, the Routine still shows `last_run` and
  stays ended: the rename must not re-enable or re-fire it.
- **P-6 Consumer `source_url`.** `create_session` with a consumer repo's
  `source_url` from a coding-workflows session succeeds, and the new
  session can run `gh api repos/<consumer>/pulls?per_page=1`.
- **P-7 Superseded convergence.** Two probe masters with ticks for the
  same fake repo: after one tick each, exactly one tick Routine remains.

**End to end (after P4, recorded in the P4 PR body):**
- Arm on a real `claude/*` docs PR and observe that the master is created,
  the tick renews, a merge produces a terminal hand-back into the pushing
  session, and the marker is renamed `sent merged …`.
- Archive the master by hand and confirm the pickup or a subscriber
  recreates it within ≤90 min, and that the new master's first tick lists
  the PR.

## Risks & Mitigations

- **Queued vs dropped fires into a busy session.** ACCEPTED — pending P2
  probe P-1. If it fails, the flip does not ship.
- **Recurring bound cron Routines or long-lived sessions have an
  undocumented lifetime cap.** ACCEPTED — pending P2 probe P-2 for
  short-term behaviour. For the long term, the A.6 recreation layers turn
  a lifetime cap into a ≤90 min gap, and the master's title records its
  last tick.
- **Auto-compaction changes the tick behaviour.** ACCEPTED — pending P2
  probe P-3. Mitigated by construction: the script decides from files
  only.
- **`list_triggers` fields, filtering or pagination differ from what the
  plan assumes.** ACCEPTED — pending P2 probe P-4. If more than 500 enabled
  Routines exist (5 pages), the tick lists `errors` and keeps going (fail
  open, no deletes).
- **Renaming or enabling a Routine owned by another session is refused.**
  ACCEPTED — pending P2 probe P-5. If only renaming fails, the design
  cannot store dedup outside the conversation, and the flip does not ship.
- **`create_session` for a consumer repo fails from the pickup.** ACCEPTED
  — pending P2 probe P-6. The subscriber layer still covers it.
- **Late ticks cause false dead-man's-switch fires, each costing a full
  wake of a pushing session.** The 30-min grace (Q17) and P-2's delay data
  cover this. A false fire only re-arms.
- **A GitHub outage while the master is alive.** The master renews anyway
  (A.4 step 4), so subscribers aren't paged. The cost is that a PR which
  stays unreadable doesn't escalate to its pushing session the way
  §26.C step 3's 7-day switch does today. Mitigations: `<e> errors` in the
  master's title and reply on every tick, and the Actions catch-all for
  `claude/*` PRs once reads recover. ACCEPTED: a 90-min window can't also
  serve as an outage alarm without paging every subscriber.
- **A stampede of subscribers when the master dies.** Every subscriber's
  Routine carries the same now + 90 min, so they can wake together. This
  is absorbed by `--ensure`'s ≤10-min "creation in progress" rule and by
  keeper election (A.4 step 1, with a first tick about 2 min after
  creation). The worst case is a few short-lived duplicate Sonnet sessions
  per master death.
- **Several accounts.**
  - Each account has its own masters, and the pickup only revives its own
    account's masters.
  - Two masters may hand back the same PR, and claims stop duplicate fixes
    (Q16). ACCEPTED.
  - Out of scope, recorded only: two accounts running
    `/claude-issue-pickup` would both drain the queue.
- **Depth limit.**
  - The master is created only from sessions at depth ≤3 (so the master is
    ≤4 and its fixers ≤5).
  - A deeper session defers to the pickup and says so.
  - A `create_session` refusal is reported in one line and never retried
    in a loop.
- **The master session sits in the wrong permission mode.** The master
  inherits its creator's mode, and a non-Auto master stalls at a prompt.
  Mitigation: A.5 creates a master only from an Auto-mode session;
  otherwise it defers to the pickup, which requires Auto mode (pickup
  step 0).
- **A PR stays unwatched if all its subscribers vanish.** For `claude/*`
  PRs the catch-all handles fixes (Q4). A non-`claude/*` PR has nobody to
  report to anyway. ACCEPTED.
- **Hourly cost of a busy master grows up to the compaction cap.** It is
  still ≤ per-PR cost (F + min(ΣHᵢ, cap) ≤ N·F + ΣHᵢ). Each tick adds only
  one Bash call plus the action calls.
- **Renewing every subscriber adds up to N `update_trigger` calls per
  tick.** Only Routines due before now + 90 min are renewed. These are
  claude.ai calls, not GitHub calls (§15 unaffected).

## Rollout

1. P1 and P3 merge inert, because the gate is `PENDING`.
2. P2 sets the gate to `PASS` only if every probe passes.
3. P4 makes new arms use master mode wherever the gate is `PASS` and the
   script exists:
   - coding-workflows immediately on merge;
   - consumers on the next `@stable` sync (daily 04:00 UTC cron or the
     `@stable` `repository_dispatch`).

   Existing legacy checkers finish their PRs unchanged.
4. **Kill switch:** set `"verdict": "DISABLED"` in `.claude/check_in_master.json`
   and its template copy. New arms go back to legacy; running masters keep
   serving their already master-owned Routines until those PRs end.
5. **P5 preflight** (for the registry entry):
   - (a) the gate is `PASS` in coding-workflows main;
   - (b) ≥14 days since P4 merged;
   - (c) `list_triggers` (`enabled: true`) shows **no** legacy (untagged)
     `PR #<n> hand-back` Routine and no `PR #<n> status check-in` send_later
     Routine;
   - (d) no master session title in `list_sessions` ends in
     `(superseded)` for more than one tick, and no `fallback_notify` was
     caused by a master failure in the last 14 days (from the master
     titles and the P4 end-to-end record);
   - (e) the `claude-pr-catch-all` job logs show no increase in queued
     `pr_fix` items compared with the 14 days before P4.

## References

- CLAUDE.md §26 (A–H), §25, §18.C/F, §20, §21, §6, §15, §28.C
- `.claude/scripts/check_in_status.py`, `.claude/scripts/stale_routines.py`,
  `.claude/scripts/claude_fix_claim.py`, `.claude/hooks/pr_check_in_reminder.py`
- `.claude/commands/implement-plan-claude.md` (Check-in Loop, depth-limit
  incident 2026-09-25), `.claude/commands/claude-issue-pickup.md`,
  `.claude/commands/fix-claude-pr.md`
- `.github/workflows/review_autofix_sweep.yml` (`claude-pr-catch-all`),
  `scripts/claude_pr_sweep.py`, `.github/workflows/update_workflows.yml`
  (`.claude/` sync)
- Issue #4525 (routine runs cannot start sessions)
- agents.md "Interactive post-push PR status check-in"; README "Claude
  fixes every claude/* PR"

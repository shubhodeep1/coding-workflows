# Self-heal unattended sessions that lose their GitHub tools, and re-queue environment blockers automatically

Source issue: shubhodeep1/coding-workflows#4938 (https://github.com/shubhodeep1/coding-workflows/issues/4938)
Base branch: main
Security pass: run

## Summary

Unattended Claude sessions sometimes start without the `gh` CLI, without the
deferred `mcp__github__*` tools loaded, or without a checkout of their
repository. Today they give up, either with an in-session question nobody
reads or with an `ai:claude-blocked` stop that waits for a human. This plan
makes those sessions repair their environment before giving up, and makes a
stop they still cannot avoid machine-readable
(`<!-- ai:claude-blocked:v1 reason=environment-… -->`). It adds an hourly
watchdog step that re-queues such issues through the same intake path
`/reclarify` uses, at most twice per issue per 24 hours, then alerts once.
It also stops the pickup from starting sessions for issues that were closed
after they were queued (owner scope addition, #4912).

## Context

- Incident (issue body): on 2026-09-29 the #4886 session
  (`session_01PKMcfjuzNCPE2CckfGEkBg`, `worker_epoch` 2) found neither `gh`
  nor `mcp__github__*`, asked an `AskUserQuestion` nobody watched, then posted
  a plain `<!-- ai:claude-blocked:v1 -->` blocker. #4911 records the #4750
  session ending on "resume blocked: needs GitHub writes".
- Owner scope addition (issue comment 5882712301): the #4912 dispatch
  (`session_01HoTdXQBPCnYPa6tfnRidCP`) had no `sources`, so no checkout, and
  ended on a free-form question. #4912 had already been closed at 01:55Z.
  Investigation for this plan: queue item #4916 was created at 01:36:39Z,
  before the close, and stayed open. The pickup dispatched it at 02:33Z
  because `queue_pending` (`scripts/claude_issue_route.py:693`) never re-reads
  the target issue. Only the intake's `authorize_target` (`:339`, `issue_closed`)
  checks the state, at queue time.
- This session hit the same failure. It started with an empty `/home/user`
  (no checkout), no `gh`, and no `mcp__github__*` tools, not even deferred
  ones. It recovered by calling `add_repo`, cloning, and re-running
  `.claude/hooks/session-start.sh`. That is the evidence behind AD-6 and the
  gh self-heal.
- `.claude/hooks/session-start.sh:6-37` `install_gh` runs under
  `install_gh || log "gh install failed (non-fatal)"` (`:225`). Bash ignores
  `set -e` inside a function called from a `||` list, so a failed
  `apt-get install` falls through to `log "gh installed: $(gh --version …)"`,
  which prints an empty version and returns 0. The "failed" line is almost
  never written. The apt-missing and no-privilege paths also return 0.
- Command twins: `.claude/commands/{implement-issue-claude,implement-plan-claude,fix-claude-pr,claude-issue-dispatch}.md`
  and `.claude/hooks/session-start.sh` each have a byte-identical
  `workflow-templates/.claude/**` twin. `tests/test_implement_issue_claude_command.py::test_template_parity`
  and `tests/test_session_start_extract_repo_slug.py` enforce the parity.
  `.claude/commands/claude-issue-pickup.md` has no twin.
- Re-queue path: `/reclarify` → `clarify.yml` → `scripts/claude_issue_handoff.sh`
  → `repository_dispatch` `claude-issue` (payload from `claude_issue_route.py build-dispatch`)
  → `claude-issue-intake.yml` / `scripts/claude_issue_intake.sh` (authorize,
  queue item, binding artifact) → pickup. The pickup trusts `issue` items only
  from `claude-issue-intake.yml` runs (`QUEUE_PRODUCERS`, issue #4621).
- `claude-issue-queue-watchdog.yml` (hourly, `17 * * * *`,
  coding-workflows only) already owns the queue's Telegram ERROR alerts
  (`scripts/claude_issue_queue_watchdog.sh`, `tg_send_msg … "ERROR"`).
- Operator rule Q40: A (interim twin-first, `docs/operations/master-session.md`),
  also named in the issue body: a stage that must change `.claude/**` edits
  only the `workflow-templates/.claude/**` twins. It then posts a `hold` claim
  and stops `BLOCKED`. The master copies the twins into `.claude/` as a
  `[claude-twin-sync]` commit and comments `/reclarify`.

## Goals

- `/implement-issue-claude`, `/implement-plan-claude`, and `/fix-claude-pr`
  load `mcp__github__*` with ToolSearch before deciding the tools are missing,
  and re-run `.claude/hooks/session-start.sh` once when `command -v gh` fails.
  Only a check that fails after both steps counts as missing.
- A failed `gh` install writes
  `[session-start] gh_install=failed reason=<reason> exit_code=<n>` and a
  marker file in `$HOME` (removed after a successful install).
- An issue-mode session that still lacks its environment never asks in
  session and never uses `AskUserQuestion`. It posts
  `<!-- ai:claude-blocked:v1 reason=environment-tools-missing -->`,
  `environment-checkout-missing`, or `environment-remote-tools-missing`.
  Every reader of the plain marker keeps matching.
- The watchdog re-queues an open `ai:claude` + `ai:claude-blocked` issue
  whose latest trusted blocker has an `environment-*` reason. It does this
  within its next hourly run, through the intake (`trigger: reclarify`), at
  most 2 times per issue per rolling 24 hours. After that it sends one
  Telegram ERROR per issue and leaves the label in place. It re-queues that
  issue again only after a trusted `/reclarify`, which restarts the count
  ("alerts once and stops"; AD-10, conformance 1).
- A plain blocker, or any non-`environment-*` reason, is never re-queued.
- The pickup starts no session for an issue that is closed when it wakes, and
  the watchdog closes queue items whose target issue is closed.

## Non-goals

- A Stop hook or `AskUserQuestion` enforcement (#4911).
- Syncing the twins into `.claude/` (the master does it under Q40; #4785
  automates it later).
- Changing `.claude/commands/claude-issue-pickup.md`: it has no twin, so an
  unattended stage cannot edit it (AD-7).
- Re-queueing Codex-routed issues, `/implement-plan-claude` projects that are
  not in issue mode, or PR fixers (the §26.H sweep already re-queues those
  after the claim lease).

## Constraints

- §6: the `<!-- ai:claude-blocked:v1 -->` marker, the `ai:claude-blocked`
  label, every existing log key, and `install_gh`'s name stay. The reason is
  an optional suffix inside the same marker, so every reader that matches the
  `<!-- ai:claude-blocked:v1` prefix still matches. New identifiers
  (`ENV_REQUEUE_MARKER`, `env_requeue_decision`, …) were checked against the
  route module and the watchdog script for clashes.
- §8: structured `gh_install=failed reason=…` and `CLAUDE_ISSUE_QUEUE_WATCHDOG env_requeue …`
  log lines.
- §15: one search call per registry owner (1 today), one comment read per
  blocked candidate (paginated, 100 per page), one issue read per distinct
  queue target, and one target read per pending issue entry in the pickup
  (at most its limit of 10). Writes: one dispatch and one comment per
  re-queue; one comment per exhausted alert; one PATCH per closed-target
  queue item.
- §18.A/B: no new script. The re-queue is a new mode of
  `scripts/claude_issue_queue_watchdog.sh`, wired as a new step of the
  existing hourly `claude-issue-queue-watchdog.yml`. The decisions are pure
  functions in `scripts/claude_issue_route.py`.
- §19: the phase and final PRs use `Refs #4938`; the final PR into `main`
  carries `Fixes #4938` (#4938 is not `ai:orchestrator-tracking`).
- §20: one `changelog.d/4938-environment-blocker-self-heal.md` fragment.
- §27: `claude-issue-queue-watchdog.yml` stays tiny.
- §28.C / Q40: every `.claude/**` change is made in its
  `workflow-templates/.claude/**` twin only (see Rollout).

## Approach

1. **Self-heal preflight** (command twins). ToolSearch for the `mcp__github__*`
   tools the command uses. If `command -v gh` fails, run
   `bash .claude/hooks/session-start.sh` once and check `gh api repos/<owner>/<repo> --jq .full_name`.
   The GitHub transport is missing only when both fail. When only the MCP
   tools are missing, the command's GitHub writes use `gh api` REST calls
   shaped per CLAUDE.md §23.D (AD-8). A missing checkout (no git checkout of
   the target repo, or no `.claude/commands/`) is first repaired with
   `add_repo` plus one clone (AD-6).
2. **Environment blockers.** Where the preflight still fails,
   `/implement-issue-claude` posts `<!-- ai:claude-blocked:v1 reason=environment-<kind> -->`
   with the `ai:claude-blocked` label and never asks. `/implement-plan-claude`
   issue mode does the same when a stage loses its tools. `/fix-claude-pr`
   reports and ends, and the §26.H sweep re-queues it after the lease (AD-5).
   The dispatch start prompt tells a session with no checkout to self-heal or
   post `environment-checkout-missing`. On a closed issue, nothing is posted.
3. **Hook diagnostics.** `install_gh` checks each step explicitly and sets a
   reason (`apt_unavailable`, `no_privileges`, `keyring_download_failed`,
   `apt_update_failed`, `apt_install_failed`, `gh_missing_after_install`).
   `main` writes the structured line and the marker file
   `${SESSION_START_GH_MARKER_FILE:-$HOME/.claude-session-start-gh-install}`.
4. **Hourly re-queue.** A new watchdog step
   (`CLAUDE_ISSUE_WATCHDOG_MODE=env-requeue`, `GH_TOKEN` = `GH_PAT`) runs
   `claude_issue_route.py env-requeue-plan`, which does the reads and the
   pure decisions. For each `requeue` action the step builds the `/reclarify`
   dispatch (`build-dispatch --trigger reclarify`), POSTs it to
   `repos/<self>/dispatches`, then comments
   `<!-- ai:claude-env-requeue:v1 blocker=<id> reason=<reason> -->`. For each
   `alert` it comments `<!-- ai:claude-env-requeue-exhausted:v1 blocker=<id> reason=<reason> -->`
   and sends one Telegram ERROR.
5. **Closed targets.** `queue-pending --fetch-repo` drops pending issue
   entries whose target reads `closed` into `ignored` (`issue_closed`), and
   fails open on a read error. The same watchdog step runs
   `queue-closed-targets` and closes those queue items (`not_planned`) with
   the queue token, before the stale step can flag them.

Alternatives are recorded under Auto-decisions.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
plan is fixed by the issue, and `/implement-issue-claude` always writes one
phase.

1. **Phase 1: environment self-heal, environment blockers, hourly re-queue,
   closed-target refusal.** Protected paths: `.claude/commands/implement-issue-claude.md`,
   `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`,
   `.claude/commands/claude-issue-dispatch.md`, `.claude/hooks/session-start.sh`.
   Per Q40 and the issue body, the phase edits only their
   `workflow-templates/.claude/**` twins.
   - Files: see Files & Modules.
   - Done: the tests below pass locally, twins updated, and the PR is open
     with a `hold` claim and an `ai:claude-blocked` twin-sync ask on #4938.
     Parity tests go green once the master's `[claude-twin-sync]` commit
     lands.
   - Rollback: revert the phase PR. The watchdog step is additive and
     fail-open, and the new marker suffix is ignored by old readers.

## Implementation Steps

Phase 1:
1. `workflow-templates/.claude/hooks/session-start.sh`: explicit per-step
   checks and reasons in `install_gh`; `main` logs
   `gh_install=failed reason=… exit_code=…` and writes or removes the marker
   file.
2. `workflow-templates/.claude/commands/implement-issue-claude.md` step 0 and
   Tool Access: self-heal order, `environment-*` blocker reasons, no
   in-session questions or `AskUserQuestion`, closed issue → no blocker, gh
   REST fallback.
3. `workflow-templates/.claude/commands/implement-plan-claude.md`: step 0
   tools bullet, Issue Mode "Session tools are required" bullet (reason
   `environment-remote-tools-missing` / `environment-tools-missing`), and
   Tool Access self-heal.
4. `workflow-templates/.claude/commands/fix-claude-pr.md` step 0 and Tool
   Access: self-heal, then report and end (the sweep re-queues).
5. `workflow-templates/.claude/commands/claude-issue-dispatch.md` step 2 issue
   and PR start prompts: a no-checkout self-heal line and the
   `environment-checkout-missing` blocker.
6. `scripts/claude_issue_route.py`: blocker and requeue marker parsers,
   `env_requeue_candidates`, `env_requeue_decision`, `env_requeue_plan`
   (reads via an injectable `read`), `read_target_states`,
   `drop_closed_targets`, `queue_closed_targets`; CLI subcommands
   `env-requeue-plan` and `queue-closed-targets`; `queue-pending --fetch-repo`
   applies `drop_closed_targets`.
7. `scripts/claude_issue_queue_watchdog.sh`: `CLAUDE_ISSUE_WATCHDOG_MODE=env-requeue`
   branch (closed-target cleanup, then re-queue and alert), always exit 0.
8. `.github/workflows/claude-issue-queue-watchdog.yml`: a new step before the
   stale step, with `GH_PAT`, the queue token, and `continue-on-error`.
9. Tests, `README.md` (Claude issue implementer), `agents.md` (entry 15), and
   `changelog.d/4938-environment-blocker-self-heal.md`.

## Files & Modules

- `workflow-templates/.claude/hooks/session-start.sh` (twin of the protected hook)
- `workflow-templates/.claude/commands/implement-issue-claude.md`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/fix-claude-pr.md`
- `workflow-templates/.claude/commands/claude-issue-dispatch.md`
- `scripts/claude_issue_route.py`
- `scripts/claude_issue_queue_watchdog.sh`
- `.github/workflows/claude-issue-queue-watchdog.yml`
- `tests/test_claude_issue_route.py`
- `tests/test_implement_issue_claude_command.py`
- `tests/test_session_start_extract_repo_slug.py`
- `README.md`, `agents.md`
- `changelog.d/4938-environment-blocker-self-heal.md` [new]
- `docs/implement-plan/issue-4938-environment-blocker-self-heal.md` [new] (progress log)

## Tests

- Unit (`tests/test_claude_issue_route.py`): the marker parser accepts the
  plain and reason forms and rejects malformed ones. `env_requeue_decision`
  covers: plain blocker → skip; non-environment reason → skip; environment →
  requeue; already re-queued and fresh → skip; re-queued, still labelled,
  older than the stale window → requeue again; 2 re-queues in 24h → alert
  once, then skip; re-queues older than 24h don't count; untrusted authors
  ignored; label gone → skip. `env_requeue_candidates` covers the registry
  filter and the Codex route. `drop_closed_targets` and
  `queue_closed_targets` cover closed, open, and failed reads (fail open).
  The CLI round trip is covered too.
- Hook (`tests/test_session_start_extract_repo_slug.py`): `install_gh` with
  stubbed `apt-get`, `sudo`, and `curl` on an isolated `PATH`. Covers a
  failing `apt-get install` → `gh_install=failed reason=apt_install_failed`
  and the marker file, the apt-missing reason, and success removing a stale
  marker. Run against both the hook and its twin once synced; before the sync,
  against the twin.
- Commands (`tests/test_implement_issue_claude_command.py`): the preflight
  text (ToolSearch, the session-start re-run, the three reasons, no
  `AskUserQuestion`, closed → no blocker) in the issue, plan, fix, and
  dispatch twins, and that the plain marker text readers still match.
- Watchdog (`tests/test_claude_issue_route.py`, next to the existing
  watchdog stub tests, already run by `ci.yml`): runs the script in `env-requeue` mode with a fake `gh` and canned JSON.
  Asserts the dispatch body (`trigger: reclarify`), the marker comment, the
  alert comment plus one Telegram call, no action on a plain blocker, closing
  a queue item for a closed target, and exit 0 when reads fail.
- Existing suites: `tests/test_claude_issue_route.py`,
  `tests/test_implement_issue_claude_command.py`,
  `tests/test_implement_plan_claude_command.py`,
  `tests/test_update_workflows_guardrails.py`,
  `tests/test_session_start_extract_repo_slug.py`.

## Risks & Mitigations

- Twin parity tests fail on the phase PR until the master syncs →
  ACCEPTED: this is the Q40 flow. The phase PR carries a `hold` claim and the
  issue carries the twin-sync ask.
- A forged `reason=environment-*` marker from an outsider triggers
  re-queues → only OWNER/MEMBER/COLLABORATOR users and `github-actions[bot]`
  count (`is_trusted_issue_author`), and the intake re-authorizes every
  dispatch (#4620).
- Re-queue loops → capped at 2 per rolling 24 hours per issue from markers,
  one alert per issue, then no more re-queues until a trusted `/reclarify`
  (AD-10), and the label stays.
- A re-queued session dies silently (e.g. no sources and no working GitHub
  transport) → AD-4 re-queues again once the label is still present after
  `CLAUDE_ISSUE_QUEUE_STALE_HOURS`. This counts toward the cap, so it ends in
  the alert.
- The marker comment by the `GH_PAT` account starts the target repo's
  `issue_comment` workflows → ACCEPTED: they filter on `/reclarify` and exit.
  At most 2 such comments per issue per day.
- Pickup target reads for consumer repos are refused by the session proxy
  when the repo is not attached → fail open (entry stays pending). The
  session's own step 2 gate and the watchdog's hourly cleanup cover it.

## Rollout

Twin-first (Q40): the phase PR changes the `workflow-templates/.claude/**`
twins; the master copies them into `.claude/` as `[claude-twin-sync]`
(the hook needs the Q62/Q64 approval window) and comments `/reclarify`. The
script and workflow parts take effect when the final PR merges into `main`.
Consumers receive the twins with the next `@stable` sync (§14). Rollback:
revert the PRs; no data or index changes.

## References

- #4938, #4911, #4886, #4750, #4912 (queue item #4916), #4785, #4621, #4620, #4525
- `docs/operations/master-session.md` (Q40, Q62/Q64)

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which hourly job hosts the re-queue? — Picked: A — a new step in `claude-issue-queue-watchdog.yml`, with its I/O in a new mode of `scripts/claude_issue_queue_watchdog.sh` and the decisions in `scripts/claude_issue_route.py`. Alternatives: B — the `claude-pr-catch-all` job of `review_autofix_sweep.yml`; C — a new workflow. Why: the watchdog already owns the queue's Telegram ERROR channel the issue names; C breaks §18.A/B. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How is an issue re-queued? — Picked: A — send the same `claude-issue` `repository_dispatch` the `/reclarify` handoff sends (`build-dispatch --trigger reclarify`, `GH_PAT`), so the intake authorizes it, opens or reuses the queue item, and binds it. Alternatives: B — `workflow_dispatch` of the intake (trigger `manual`); C — open the queue item from the watchdog (a new producer the pickup would have to trust, weakening #4621). Why: identical to `/reclarify` and no change to the pickup's trust rules. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How are retries counted? — Picked: A — the watchdog comments `<!-- ai:claude-env-requeue:v1 blocker=<comment id> reason=<reason> -->` after each successful dispatch and `<!-- ai:claude-env-requeue-exhausted:v1 blocker=<id> reason=<reason> -->` when it alerts; only trusted authors' markers count; the window is a rolling 24 hours. Alternatives: B — count the intake's `ai:claude-issue-dispatched:v1` comments (they do not say why the issue was queued); C — keep state in a file or variable (no state store exists). Why: the issue asks for counting from markers on the issue. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] What if the re-queued session dies before it claims the issue? — Picked: A — re-queue again when the latest re-queue for the current blocker is older than `CLAUDE_ISSUE_QUEUE_STALE_HOURS` (default 3) and `ai:claude-blocked` is still on the issue (the session removes it at step 2 when it gets that far); it counts toward the cap. Alternatives: B — re-queue only on a new blocker, so a silent death waits for a human. Why: #4912-style sessions cannot post anything, and the cap still bounds it. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Which stops carry an environment reason? — Picked: A — `/implement-issue-claude` step 0 (`environment-tools-missing`, `environment-checkout-missing`, `environment-remote-tools-missing`), the dispatch start prompt's no-checkout line, and `/implement-plan-claude` issue mode's tools stops; `/fix-claude-pr` posts nothing and ends (its sweep claim lapses and the §26.H sweep re-queues it); `claude-issue-dispatch.md` step 3 (the deprecated routine) is unchanged. Alternatives: B — only the two reasons the issue names; C — also tag dispatch step 3. Why: all three are "a fresh session fixes it" failures; step 3 is a retired path. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] How does a session without a checkout self-heal? — Picked: A — when the `add_repo` tool exists, attach the target repo (`access: "push"`) and clone it once as the tool's result instructs, then re-check; only then post `environment-checkout-missing`. Alternatives: B — post the blocker at once. Why: this session recovered exactly that way on 2026-09-29. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] How are closed targets refused? — Picked: A — `queue-pending --fetch-repo` reads each pending issue target (one GET each, at most the wake limit) and moves closed ones to `ignored` (`issue_closed`), failing open on a read error; the watchdog's new step closes open queue items whose target is closed (`state_reason: not_planned`, queue token). Alternatives: B — also edit `claude-issue-pickup.md` to close them (no twin, needs a watched session); C — leave it to the session's step 2 gate. Why: covers #4912 without a protected edit that Q40 cannot route. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] What if the MCP GitHub tools stay missing but `gh` works? — Picked: A — use `gh api` REST calls shaped per CLAUDE.md §23.D (inline `-f` fields, no `--input` files or heredocs) for the labels, comments, and PRs the command would do through MCP. Alternatives: B — treat missing MCP tools as missing even with a working `gh`. Why: `gh` through the proxy covers those writes (this session used it); B would stop sessions that can work. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-29] Marker file path and content for a failed gh install? — Picked: A — `${SESSION_START_GH_MARKER_FILE:-$HOME/.claude-session-start-gh-install}`, holding the same `gh_install=failed reason=… exit_code=… at=<UTC>` line, removed after a successful install. Alternatives: B — a JSON file under `~/.claude/`; C — no marker. Why: one grep-able format for the log and the file; outside `.claude/`; the env override (default set, §4) lets tests isolate it. Applied in: phase 1 PR. Status: pending review

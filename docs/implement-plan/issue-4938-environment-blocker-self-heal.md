# Implement-Plan Log — Self-heal unattended sessions that lose their GitHub tools, and re-queue environment blockers automatically

- Plan: docs/plans/issue-4938-environment-blocker-self-heal-plan.md
- Source issue: shubhodeep1/coding-workflows#4938 (https://github.com/shubhodeep1/coding-workflows/issues/4938)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4938-environment-blocker-self-heal   Final PR: (opened after this commit)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened; phase 1 starting (twin-first per Q40)

## Phases
1. [ ] Phase 1 — environment self-heal, environment blockers, hourly re-queue, closed-target refusal   — protected paths: .claude/commands/implement-issue-claude.md, .claude/commands/implement-plan-claude.md, .claude/commands/fix-claude-pr.md, .claude/commands/claude-issue-dispatch.md, .claude/hooks/session-start.sh (edited as workflow-templates/.claude/** twins only, Q40)
   - [ ] session-start.sh twin: per-step gh install checks, `gh_install=failed reason=…` log line, marker file
   - [ ] command twins: ToolSearch + session-start re-run self-heal, add_repo checkout self-heal, `environment-*` blocker reasons, no in-session questions / AskUserQuestion, gh REST fallback
   - [ ] scripts/claude_issue_route.py: marker parsers, env re-queue decisions and plan, closed-target refusal in queue-pending, queue-closed-targets
   - [ ] scripts/claude_issue_queue_watchdog.sh env-requeue mode + claude-issue-queue-watchdog.yml step
   - [ ] tests (route, commands, hook, watchdog) + ci.yml wiring
   - [ ] README.md, agents.md, changelog.d/4938-environment-blocker-self-heal.md

## Conformance

## Security pass

## Validation

## Completion

## Activation

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

## Lessons

## Notes
- Issue mode: plan written by /implement-issue-claude for #4938; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Protected-path approval: phase 1 — twin-first per Q40 (issue #4938 body, owner-authored: "edit the `workflow-templates/.claude/**` twins first, per Q40", 2026-09-29): the phase edits only the `workflow-templates/.claude/**` twins, opens the phase PR into the project branch, posts a `hold` claim, and stops BLOCKED; the master copies the twins into `.claude/` as `[claude-twin-sync]` (the hook needs the Q62/Q64 window), pushes (lifting the hold), and comments `/reclarify`.
- Environment at start (2026-09-29, session_017rVpkNCRJsVb9D96UYxPpN): no checkout in `/home/user`, no `gh`, no `mcp__github__*` tools (not even deferred). Recovered with `add_repo` + clone + re-running `.claude/hooks/session-start.sh`; GitHub writes go through `gh api` REST (AD-8). This is the #4938 failure mode itself.
- #4912 investigation (owner scope addition): queue item #4916 was created 2026-09-29T01:36:39Z, #4912 closed at 01:55:00Z, and the pickup dispatched #4916 at 02:33Z because `queue_pending` never re-reads the target issue (only the intake's `authorize_target` checks `issue_closed`, at queue time). Fixed by AD-7.

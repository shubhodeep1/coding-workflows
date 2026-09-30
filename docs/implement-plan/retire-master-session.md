# Implement-Plan Log — Retire the master session: projects resolve their own escalations, resumes and guard syncs

- Plan: docs/plans/retire-master-session-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-retire-master-session   Final PR: #5132 draft
- Status: IN_PROGRESS
- Stage: phase 1/4 — review round
- Activation: not started
- Waiting on: PR #5164 (review of the round-2 fix; test-only, no twin sync needed)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01PxYwa7Rwnpb7RYbmsURfQ9 (safety-net and hand-back trigger ids are in the stage report and the next `— resume.` block)
- Last updated: 2026-09-30
- Last note: Review round 2 on PR #5164 (head ba70679, hand-off round=1 after the twin sync): fixed the one finding, a contract test now pins the judge's Stage values in both Stage templates; nothing under `.claude/` changed, so no twin sync.

## Phases
1. [ ] Phase 1 — Escalation judge   — protected paths: `.claude/commands/escalation-judge.md` (new), `.claude/scripts/escalation_ledger.py` (new), `.claude/commands/implement-plan-claude.md`, `.claude/settings.json` (edited only in their `workflow-templates/.claude/` twins) — PR #5164 open (twins synced in c315b2e and ba70679), waiting on review; review rounds: 2; interventions: 0
   - [x] `workflow-templates/.claude/scripts/escalation_ledger.py` (`fingerprint`, `allowed`, `record`; exit 1 bad arguments, exit 2 unreadable or malformed log), allowlisted in the `settings.json` twin in both forms — `tests/test_escalation_ledger.py` (48 tests)
   - [x] `workflow-templates/.claude/commands/escalation-judge.md` (inputs, steps 0–9, menu, never-list)
   - [x] `implement-plan-claude.md` twin: the ten stops name `escalation stop <id>` (lines 55, 66, 68, 69, 75, 76, 78, 81, 82, 89, 92, 273); new `## Escalations` section (line 292: stop-id table, human-only list, the 3-step stop procedure with the `kind=escalation stop=<id>` marker, `budget` / `descope` / `close` stages); checker step 0a (line 235) and Arming the wait step 3 for the `escalation` wait; `— resume.` template lines; `## Escalations` in the log template; Status `CLOSED`; recap, Issue Mode bullet, "Read first", Rules, Tool Access, Output Format updated
   - [x] CLAUDE.md §28.G (`CLAUDE.md:2360`) and the §28.C pointer (`CLAUDE.md:2253-2255`)
   - [x] `agents.md` (`:882` escalation judge stage; `:969` escalation waits)
   - [x] Tests: `tests/test_escalation_ledger.py`, `tests/test_escalation_judge_command.py` (own `ci.yml` steps); `tests/test_claude_md_section_numbers.py` checks §28 runs A–G
   - [x] Changelog fragment `changelog.d/5132-escalation-judge.md`
   - Done: every §28.C escalation stop hands off to the judge; `escalation_ledger.py` passes its tests; template parity passes after the twin sync
2. [ ] Phase 2 — Blocked-issue sweep and automatic resume   — protected paths: `.claude/scripts/claude_blocked_sweep.py` (new), `.claude/commands/claude-issue-pickup.md` (no twin), `.claude/commands/implement-plan-claude.md`, `.claude/commands/claude-issue-dispatch.md`, `.claude/scripts/dispatch_workflow.py`, `.claude/settings.json`
   - [ ] `claude_blocked_sweep.py` (`scan`, `decide`) with the §15 docstring contract; allowlisted
   - [ ] Pickup step 3c; `claude-issue-intake.yml` in `DISPATCHABLE_WORKFLOWS` and the `gh workflow run` allow rule
   - [ ] Q8 stops write `kind=ask-first|no-tools|depth-limit`; every blocker writes `ai:claude-blocked-session:v1`
   - [ ] Watchdog page step (`scripts/claude_issue_queue_watchdog.sh`, `claude-issue-queue-watchdog.yml`)
   - [ ] Tests (`tests/test_claude_blocked_sweep.py`, dispatch and watchdog tests) with `ci.yml` steps; `agents.md` item 15 and README; changelog fragment
   - Done: an answered blocker gives `wake`/`requeue`; a human-only blocker gives one `page`; `judge` degrades to `page` without `escalation-judge.md`
3. [ ] Phase 3 — Guard-change classifier   — precondition: `scripts/claude_twin_sync.py` on `main` (#4785)
   - [ ] `scripts/claude_guard_change_classifier.py`; call from `scripts/claude_twin_sync.py`; tests; `agents.md`; changelog fragment
   - Done: a tightening-only sync auto-merges; any loosening change keeps the approval label
4. [ ] Phase 4 — Alert policy, poller retirement, operator runbook   — protected paths: per the operator's start-up answer (confirmed against the phase's files when it starts)
   - [ ] CLAUDE.md §28.G alert list and §26 note; `docs/operations/operator-runbook.md` (new) and the stub at `docs/operations/master-session.md`; `agents.md` and README references
   - [ ] Retirement steps (only once phases 1 and 2 are on `main`; otherwise `Poller retirement: waiting on phase <n>`)
   - [ ] Tests pinning the runbook and stub; changelog fragment
   - Done: docs merged; poller archived or the wait recorded

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Escalations

## Auto-decisions
- AD-1 [phase 1/4, 2026-09-29] The plan's line list for step 3 includes line 61 (no verified verdict-bot posting path), and steps 9 and 10 stop when a consumer wrapper rejects `ref` / `target_ref`. Are these escalation stops? — Picked: A — no; they stay human stops, listed in the new section's human-only paragraph. Alternatives: B — route them to the judge under an existing stop id. Why: none is a §28.C failure escalation or one of the plan's ten stop ids; each needs a configuration or sync only a human can supply, and the judge may never post a verdict marker. Applied in: phase 1. Status: pending review
- AD-2 [phase 1/4, 2026-09-29] Which stop ids do the INCOMPLETE-with-no-fix-PR stops of step 8 (conformance) and step 12 (full-scope verify) use? — Picked: A — `conformance-cap` and `verify-activation-cap`. Alternatives: B — two new ids (`conformance-incomplete`, `verify-activation-incomplete`); C — keep them human. Why: the plan cites lines 68-69 and 89-92 under those two stops, and its stop list is fixed at ten; Q8 sends every non-human-only stop to the judge. Applied in: phase 1. Status: pending review
- AD-3 [phase 1/4, 2026-09-29] How does a `budget` round raise the cap? — Picked: A — every cap counts one extra round per `ES-<n>` entry with `choice=budget` or `choice=descope` for the same stop id (for `intervention-cap`, only entries naming the same PR); a repeat stop goes back to the judge, where the same fingerprint gets only unused choices. Alternatives: B — the capped stage recomputes the fingerprint and counts only matching entries. Why: A needs no fingerprint in the capped stage and still bounds the loop, because `allowed` refuses a second `budget` or `descope` for the same failure. Applied in: phase 1. Status: pending review
- AD-4 [phase 1/4, 2026-09-29] Where is an `ES-<n>` entry persisted when no PR is in flight? — Picked: A — like an auto-decision: the report and an `Uncommitted escalations:` line in the `— resume.` block, committed by the next PR; the judge also re-adds entries from `ai:claude-escalation:v1` comments missing from the log before calling `allowed`. Alternatives: B — the judge pushes a log commit straight to the project branch. Why: B adds a third direct push to the project branch and conflicts with the log copy in an in-flight PR; the comments make the ledger recoverable. Applied in: phase 1. Status: pending review
- AD-5 [phase 1/4, 2026-09-29] Does an issue-mode escalation stop still send its `PushNotification`? — Picked: A — no; only the judge's `close` notifies. Alternatives: B — keep the stop's notification. Why: Q12 limits alerts to ask-first operations, a `close`, and Q8 stops. Applied in: phase 1. Status: pending review
- AD-6 [phase 1/4, 2026-09-29] What does `descope` mean for `intervention-cap`, whose failing code is not on the base branch yet? — Picked: A — one `[claude-intervention] descope ES-<n>` commit on the blocked PR's own branch, then the wait is re-armed on that PR. Alternatives: B — close the PR and open a revert PR. Why: there is nothing to revert on the base branch; removing the part from the PR is the smallest change. Applied in: phase 1. Status: pending review
- AD-7 [phase 1/4, 2026-09-29] What does the log's `Status:` say after a judge `close`? — Picked: A — a new value `CLOSED (not planned, ES-<n>)`. Alternatives: B — `BLOCKED` with a note; C — `COMPLETE`. Why: B reads as waiting and C as done; no script parses the value (checked with grep). Applied in: phase 1. Status: pending review
- AD-8 [phase 1/4, 2026-09-29] How does the checker learn it has an `escalation` wait and which command to start? — Picked: A — a `Wait: escalation — start <next stage> with <start command>` line replaces the `--pr` / `--run` / `--issues` target; checker step 0a runs no script, goes to step 5, and swaps the resume prompt's first command. Alternatives: B — a new `check_in_status.py --escalation` mode. Why: there is nothing to poll, and a script change would add a second protected-path file. Applied in: phase 1. Status: pending review
- AD-9 [phase 1/4, 2026-09-29] Which evidence goes into the fingerprint? — Picked: A — `checks`, `findings`, `issues` (follow-up issue numbers), `validation_class`, `validation_status`, with any other key refused. Alternatives: B — only the plan's check names, finding ids, and validation class. Why: `security-followup-unmerged` is identified by its follow-up issues; refusing unknown keys keeps fingerprints deterministic. Applied in: phase 1. Status: pending review
- AD-10 [phase 1/4, 2026-09-29] How is evidence passed to `fingerprint`? — Picked: A — `--evidence <json>` as planned, plus `--evidence-file <path>`, which the judge uses. Alternatives: B — `--evidence` only. Why: a JSON argument in a shell command risks quoting prompts in an unwatched session; a file written with the Write tool does not. Applied in: phase 1. Status: pending review

## Lessons
- [source:intervention] A command step that posts "on the blocker's thread" or "next to the blocker" must say what happens in a mode where the blocker has no GitHub thread (legacy mode keeps it in the report only), or the step has no valid target. (files: .claude/commands/escalation-judge.md, CLAUDE.md)
- [source:intervention] When a fix adds a value to a command's log or report template, extend the contract test that anchors that template in the same commit; otherwise the next review round flags the unpinned value. (files: tests/test_escalation_judge_command.py, .claude/commands/implement-plan-claude.md)

## Notes
- Started by the master session (session_01Qt5nTTqhWxcYA4NciTC6DL) through trigger trig_013zGktPPtmZYGZbgKEos3qv, with start-up answers on the operator's behalf: Auto mode (step 0), plan `docs/plans/retire-master-session-plan.md` on `main` at f736cad (step 1), 4 phases (step 3). Operator decisions Q1–Q14 (2026-09-29) are recorded in the plan. There is no source issue: blockers go in the stage report and on the final PR.
- Phase 3: waiting on #4785 (`scripts/claude_twin_sync.py` is not on `main` at f736cad). The other phases do not wait on it.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- The `settings.json` allow rules count as a guard change under the operator's Q9: A, so their sync goes through the operator (twin-sync blocker says so).
- `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` in every status check and in the checker instructions (#5057).
- Twins to sync for phase 1 (`workflow-templates/.claude/<path>` → `.claude/<path>`), sha256 of the twin: `commands/escalation-judge.md` (new) `c06f187f51fc0e9da2f84e4f4b13f8e70a1cbec952a78caacd8b9357430865a3`; `commands/implement-plan-claude.md` `f5a811fbe7e25cb11e361ddad0ea5d36353afb702fbd20618a749295c514a47c`; `scripts/escalation_ledger.py` (new) `c989bd6b28fbe01d5fd39f9c4de20f08d97c0a9c86006fbb3e6f5beba331ac8a`; `settings.json` (two added `allow` rules; a guard change under Q9: A, so the operator syncs it) `287fc56a4bd91c32b552dad08a50f36e8919bb8f4d7eea3f993cb42295e9e961`. No `.claude/` path without a twin is changed. Before the sync, `.claude/settings.json` and `.claude/commands/implement-plan-claude.md` equal their twins on `main`, so the copy is exact.
- Test runs for phase 1 are recorded in the phase PR body.
- Final PR #5132 (draft). Phase 1 PR #5164; the master session synced its four twins in c315b2e (option A of blocker comment 5890621641 on #5132; all four sha256 values matched).
- Stage sessions make file edits with Edit/Write, not `python3` heredocs (#4858); a refused `delete_trigger` or `archive_session` call is skipped, not retried (#5068); a stage never ends its turn on an in-session question (#4911).
- Review round 1 (2026-09-29, head c315b2e, ledger `389434df…`): fixed the judge's missing legacy-mode comment target (steps 1, 3, 6, 8, 9 of `escalation-judge.md`; `implement-plan-claude.md` "Escalations"; CLAUDE.md §28.G) and added `<stage> — budget ES-<n>` to both Stage templates of `implement-plan-claude.md`. Rejected: the whitespace-only evidence omission (intended normalisation, pinned by `test_fingerprint_ignores_empty_values`; a blank value carries no information, so it cannot separate two failures) and the `UnicodeDecodeError` exit 2 (the documented fail-closed contract: a ledger that cannot be read exactly must never offer a choice already used).
- Twins to sync after review round 1 (`workflow-templates/.claude/<path>` → `.claude/<path>`), sha256 of the twin: `commands/escalation-judge.md` `46ebabe32c8018a92da627a50e29bd1cf7df5cbbb459000cb43308e0f65b9837`; `commands/implement-plan-claude.md` `a58554274c97502168b05acfd5c1fd33d997a6171e922dded67fa3289ffbd052`. No `settings.json` or no-twin `.claude/` path changes. Before this round both `.claude/` files equalled their twins at c315b2e, so the copy is exact.
- Review round 2 (2026-09-30, head ba70679, hand-off 5903666231 `round=1` because the count restarted after the twin sync, ledger `e717fed2…`): one finding (consensus task gap from five reviewers), valid and fixed. `tests/test_escalation_judge_command.py::test_stage_templates_carry_the_judge_stage_values` pins `escalation judge — <stop id>`, `<stage> — budget ES-<n>` and `descope ES-<n>` in the Stage lines of both the Progress Log and the Output Format templates. Test-only change, so no `.claude/` path and no twin sync.
- Tooling note (2026-09-30): `check_in_status.py --hand-back` reported `open` for a valid review hand-off because the 30-minute `review_autofix_sweep.yml` had a queued `internal-review.yml` dispatch for #5164 (run 36669856125, behind 21 others). With a hand-off pending, that dispatch only skips (`claude_fixer_awaiting_session`, `review_autofix.yml:815-821`), so this stage went ahead. While the dispatch queue is backed up, a checker can report `still waiting` on a hand-off that is really due.

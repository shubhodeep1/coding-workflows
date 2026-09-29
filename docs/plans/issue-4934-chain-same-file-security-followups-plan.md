# Chain security follow-ups that change the same file

Source issue: shubhodeep1/coding-workflows#4934 (https://github.com/shubhodeep1/coding-workflows/issues/4934)
Base branch: main
Security pass: run

## Summary

When one security audit run files several follow-up issues for the same file, the second follow-up now carries a `Depends on: #<first>` line and the Claude issue pickup holds it until the first is closed with `ai:merged`, so it is built on top of the first fix instead of beside it. Follow-ups for different files still dispatch together.

## Context

- #4586's security pass 1/5 filed #4687 and #4688 at once. Both rewrote the same rejection gate in `scripts/review_claude_fixer_nonblocking.py` in parallel; #4694 merged first and #4695 then conflicted in 9 files with no mechanical resolution (issue #4934).
- Follow-ups are filed by `scripts/security_audit.sh` (planning in its embedded Python block, creation in the `gh issue create` loop at the end, around lines 1741–1798). Each finding has exactly one `file`.
- Claude-routed issues reach an implementation session through `scripts/claude_issue_intake.sh` (queues one bound `ai:claude-issue-queue` item per issue, after reading the live target issue for authorization) and the pickup, which runs `scripts/claude_issue_route.py queue-pending --fetch-repo …` and starts a session for every `pending` entry (`.claude/commands/claude-issue-pickup.md` step 2–3). Entries in `ignored` are never acted on, stay open, and are listed in the pickup's report.
- `scripts/claude_issue_queue_watchdog.sh` flags queue items open longer than `CLAUDE_ISSUE_QUEUE_STALE_HOURS` (default 3) as "pickup has likely stopped".
- An issue-mode project closes its issue with `ai:merged` when its final PR merges into a non-default base (`/implement-plan-claude` Issue Mode), which is the case for security follow-ups (their base is the project branch).

## Goals

- A security audit run that files two or more follow-ups for the same file writes `- Depends on: #<previous>` into each later one, chaining them in filing order.
- The pickup does not start a queued issue whose `Depends on:` issue is open, or closed without `ai:merged`; it starts it on the first wake after the dependency is closed with `ai:merged`.
- Follow-ups for different files carry no dependency and dispatch exactly as today.
- A dependency closed without `ai:merged` holds the item and reports why (pickup `ignored` reason; watchdog alert).
- No new API calls per pickup wake beyond one read of each dependency issue of the items considered (§15).

## Non-goals

- The Codex route (`AI_ISSUE_IMPLEMENTER=codex`, `ai:codex`) ignores the line (AD-7).
- Function-level overlap detection; overlap is same file path only (issue #4934 "Deciding overlap").
- Dependencies on follow-ups filed by earlier audit cycles (AD-5).
- Any change under `.claude/**` (AD-6).

## Constraints

- §5 minimal change set; §6 no renames: new identifiers only (`depends_on` payload key, `DEPENDS_ON_*` constants, `parse_depends_on`, `dependency_state`, `fetch_dependency_states`, `--fetch-dependencies`), all checked unique in `scripts/claude_issue_route.py`.
- Queue binding (issue #4621): the queue body is re-rendered and compared exactly, so the rendering for items **without** a dependency must stay byte-identical; `depends_on` is emitted only when non-empty.
- `claude-issue-dispatch.md` parses a fixed six-key fire text: the `fire_text` in pending entries keeps that shape (no `depends_on`).
- §15: pickup and watchdog issue at most one REST GET per distinct dependency, only when dependent items exist; documented in docstrings.
- §9 tabs in Python and shell; §20 changelog fragment; §7 docs in `agents.md`.
- §18: no new script or workflow; everything extends existing automated paths.
- §28.C: the phase touches no `.claude/**` path.

## Approach

1. **Filing (security audit).** The planning block computes, for each planned follow-up, the index of the previous planned follow-up with the same `file` and writes it as a third column of the follow-up index. The creation loop captures each created issue's number from `gh issue create`'s URL output and, when a row has a dependency index, appends `- Depends on: #<n>` to that follow-up's body before creating it.
2. **Queueing (intake).** `queue-issue` gains `--target-issue-json`; the intake passes the target issue it already read for authorization. `parse_depends_on` extracts every same-repository `Depends on: #N` line (self-references dropped, sorted, at most 10), and `build_fire_text` adds a final `depends_on: a,b` line inside the bound payload only when the list is non-empty. `parse_fire_text` accepts that optional key.
3. **Holding (pickup).** `queue_pending` takes an optional `dependencies` map. For an issue entry with `depends_on`, each dependency is classified from one `GET repos/<repo>/issues/<N>`: `merged` (closed + `ai:merged`), `open`, `closed_unmerged`, `inaccessible` (HTTP 403/404), or `unavailable` (other read failure). Any `open` / `closed_unmerged` / `unavailable` dependency moves the entry to `ignored` with a `held: …` reason; `inaccessible` dependencies are skipped with a note (AD-2). Holds are applied before the start limit, so held items never take start slots. The CLI reads dependencies only for the entries of the binding scan window and defers the rest.
4. **Watchdog.** `queue-stale --fetch-dependencies` reads the same states; a dependent item is judged only when every dependency is readable: waiting on an open dependency is not stale, a `closed_unmerged` dependency flags it at once with the reason, and all-merged dependencies measure its age from the latest dependency close. The Telegram message names the reason per item.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — chain same-file security follow-ups and hold dependent queue items.** Files: see Files & Modules. Done when: the tests below pass, `ruff check --select E,F --ignore E501 scripts/*.py` is clean, and a queue item without a dependency renders byte-identically to today. Rollback: revert the PR; queue items written with a `depends_on` line would then fail the old parser as `bad_payload` and stay open for the watchdog until `/reclarify` rewrites them.

## Implementation Steps

Phase 1:
1. `scripts/claude_issue_route.py`: add `DEPENDS_ON_LINE_RE`, `DEPENDS_ON_MAX`, `MERGED_LABEL`, `FIRE_TEXT_OPTIONAL_KEYS`; `parse_depends_on(body, issue_number)`; optional `depends_on` in `build_fire_text` / `parse_fire_text`; `build_queue_issue` renders it; `dependency_state(issue)`, `fetch_dependency_states(keys, gh_read)`; `queue_pending(..., dependencies=None)` hold logic before the limit; `queue_stale(..., dependencies=None)` rules and a `reason` field; CLI `queue-issue --target-issue-json`, `queue-pending` dependency reads in `--fetch-repo` mode (and `--dependencies-json` for `--issues-json` mode), `queue-stale --fetch-dependencies` / `--dependencies-json`.
2. `scripts/claude_issue_intake.sh`: pass `--target-issue-json "${TARGET_ISSUE_FILE}"` to `queue-issue`; log the dependency list.
3. `scripts/claude_issue_queue_watchdog.sh`: call `queue-stale --fetch-dependencies`, carry the per-item reason into the log, annotation, and Telegram lines.
4. `scripts/security_audit.sh`: dependency index column in the planning block; number capture and `- Depends on:` append in the creation loop.
5. Tests, `agents.md`, and `changelog.d/4934-chain-same-file-security-followups.md`.

## Files & Modules

- `scripts/claude_issue_route.py`
- `scripts/claude_issue_intake.sh`
- `scripts/claude_issue_queue_watchdog.sh`
- `scripts/security_audit.sh`
- `tests/test_claude_issue_route.py`
- `tests/test_security_audit_workflow_contract.py`
- `agents.md`
- `changelog.d/4934-chain-same-file-security-followups.md` [new]
- `docs/plans/issue-4934-chain-same-file-security-followups-plan.md` [new]
- `docs/implement-plan/issue-4934-chain-same-file-security-followups.md` [new]

## Tests

- Route (unit): `parse_depends_on` (list-item and plain forms, several lines, self-reference, cross-repo ignored, cap); fire-text round trip with and without `depends_on`; queue body without a dependency is byte-identical to the pinned rendering; binding still verifies an item with `depends_on`; `queue_pending` holds on `open`, holds and reports on `closed_unmerged`, starts on `merged`, skips `inaccessible`, holds on `unavailable`, defers an unread dependency, and applies holds before the limit; two items on different files (no dependency) both pend; `queue_stale` rules; CLI `queue-issue --target-issue-json`.
- Security audit (script-level with the mock `gh`): two findings on the same file → the second body carries `- Depends on: #<first number>`; findings on different files → no dependency line; three on one file chain 1←2←3.
- Existing suites: `tests/test_claude_issue_route.py`, `tests/test_security_audit_workflow_contract.py`, `tests/test_security_pass_skip.py`, `tests/test_implement_issue_claude_command.py` stay green.

## Risks & Mitigations

- The web pickup reads through the agent proxy, which refuses repositories not attached to its session: a consumer-repo dependency is then `inaccessible` and the item dispatches as today (logged in the entry's `dependency_notes`). ACCEPTED — attach the consumer repo to the pickup session to enforce holds there (documented in `agents.md`).
- `gh_retry` may re-run `gh issue create` after a POST landed and file a duplicate (existing behaviour); the chain then points at whichever number was printed. ACCEPTED — unchanged from today.
- A human who can edit a follow-up could remove its `Depends on:` line; that only restores today's parallel behaviour.
- Rollback leaves queued dependent items unparseable by the old code (`bad_payload`, watchdog-visible); `/reclarify` re-queues them.

## Rollout

Ships to consumer repos with the next `@stable` sync (scripts only; no workflow input or env var changes). No flag: follow-ups without a shared file behave exactly as before.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where does the dependency travel from the issue to the pickup? — Picked: A — the intake parses `Depends on:` from the target issue it already reads and records it as an optional `depends_on` key in the bound queue payload. Alternatives: B — the pickup reads each target issue body every wake (one more call per item, and the pickup never reads target issues); C — the `/implement-issue-claude` session holds itself (needs a wake loop per held issue). Why: zero new reads at intake, and the binding covers the key. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What does the pickup do when a dependency cannot be read? — Picked: A — HTTP 403/404 (the session cannot see that repository, or no such issue) dispatches without the hold and notes it; any other failure holds the item this wake and retries next wake. Alternatives: B — always hold (strands consumer-repo items forever in a web pickup); C — always dispatch (a transient error reintroduces the parallel conflict). Why: a hold that can never clear must not strand work, and a transient error must not skip the gate. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How does the queue watchdog treat held items? — Picked: A — judge a dependent item only when every dependency is readable: open → not stale, closed without `ai:merged` → flagged at once with the reason, all merged → age from the latest close. Alternatives: B — unchanged (a false "pickup has likely stopped" alert for every chained follow-up); C — skip dependent items entirely (a dependency closed without merge is never alerted). Why: only alert on what can be proven. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] With three or more follow-ups on one file, what does each depend on? — Picked: A — the previous follow-up for that file (a chain). Alternatives: B — all on the first (the second and third would run in parallel again). Why: only a chain serialises every pair. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Should a new follow-up depend on an open follow-up from an earlier audit run? — Picked: A — no, only follow-ups filed in the same run are chained. Alternatives: B — also chain to open same-file follow-ups from earlier runs. Why: the chain's security checker re-dispatches the audit only after every earlier follow-up is resolved, so earlier-run overlap cannot occur in the chain (§5). Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] How are held items reported without editing `.claude/**`? — Picked: A — as `ignored` entries with `held: …` reasons, which the pickup already lists and never acts on or closes. Alternatives: B — a new `held` output key plus a pickup command edit (a protected path, which stops an unattended phase, §28.C). Why: fits the existing contract. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Should the Codex route honour `Depends on:` too? — Picked: A — no, Claude route only. Alternatives: B — add the hold to clarify/the orchestrator poller. Why: the issue scopes the hold to the Claude intake and pickup; Codex is the non-default implementer (§5). Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-29] Which `Depends on:` lines count? — Picked: A — every line `Depends on: #N` (optionally a list item) naming an issue of the same repository, self-references dropped, deduplicated, at most 10; the item waits for all of them. Alternatives: B — only the first line; C — also `owner/repo#N` references. Why: never silently ignore a stated dependency, and cross-repo reads are out of the budget. Applied in: phase 1 PR. Status: pending review

## Notes

- `.claude/commands/claude-issue-pickup.md` step 2 still says only pending and unavailable items are re-checked; held items are re-checked every wake as well. Left unchanged (protected path, AD-6).

## References

- Issue #4934; incident: #4586, #4687, #4688, #4694, #4695.
- Queue binding: issue #4621. Routine limits: issue #4525.

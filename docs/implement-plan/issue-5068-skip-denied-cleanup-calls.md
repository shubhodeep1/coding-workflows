# Implement-Plan Log — Unattended stages skip a denied cleanup call instead of retrying it

- Plan: docs/plans/issue-5068-skip-denied-cleanup-calls-plan.md
- Source issue: shubhodeep1/coding-workflows#5068
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5068-skip-denied-cleanup-calls   Final PR: #5076 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5097: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: Phase 1 PR #5097 opened twin-first (Q40, per the issue): only the `workflow-templates/.claude/` twins changed; a `hold` claim is on its head and the twin-sync blocker is on #5068 (`ai:claude-blocked`). After the `[claude-twin-sync]` copy and `/reclarify`, the resumed stage arms the wait on PR #5097 (step 7) and does not re-implement the phase.

## Phases
1. [ ] Phase 1 — skip denied cleanup calls   — protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/claude-issue-pickup.md` (twins only; the pickup, which has no twin, goes into the sync blocker as a diff) — PR #5097 open (twin sync pending); review rounds: 0; interventions: 0
   - [x] CLAUDE.md §26.I (`CLAUDE.md:2114-2163`) plus pointers in §26.C step 5 (`:1917`), §26.D (`:1948`, `:1981`), §26.G (sweep deletes)
   - [x] `implement-plan-claude.md` twin: resume hygiene with the `get_trigger` ownership check (`:11`), zombie-checker cleanup (`:190`), arming the wait (`:177`), two-step start (`:138`), hand-back (`:249`), steps 12–13 (`:79`, `:83`), checker prompt (`:212`), Output Format (`:358`), Rules (`:392`) (root copy: pending twin sync)
   - [x] `fix-claude-pr.md` twin: step 8 (`:62`) and Rules (`:72`) (root copy: pending twin sync)
   - [x] `claude-issue-pickup.md`: exact diff + sha256 prepared for the sync blocker (no twin; not edited by this stage)
   - [x] `agents.md` bullet (`:1036`)
   - [x] Tests: 6 new in `test_implement_plan_claude_command.py`, 1 in `test_check_in_status_hand_back.py`, 1 in `test_implement_issue_claude_command.py`
   - [x] `changelog.d/5068-skip-denied-cleanup-calls.md`
   - Done: see the phase PR body for the test evidence; the real tree fails only the four sync-dependent tests until the `[claude-twin-sync]` copy

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the rule live? — Picked: A — one definition in a new CLAUDE.md §26.I, plus a short sentence at each command's cleanup site and a Rules bullet. Alternatives: B — the full rule repeated in each command only. Why: the §26 flows are in CLAUDE.md, and one definition keeps six call sites from drifting; CLAUDE.md is not a protected path. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Which calls count as cleanup? — Picked: A — every `delete_trigger`, `archive_session`, and `set_session_title` in the listed flows, including the §26.G sweep's deletes, the checker prompt's step 4b delete, the two-step start's archive after a failed start trigger, and the end-of-project archives. Alternatives: B — only the three sites the issue names. Why: the acceptance criterion is that no flow retries a denied cleanup call. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] What does a failed cleanup call other than a denial do? — Picked: A — not found counts as done; any other failure is recorded as `cleanup skipped: <tool> failed (<error>)` and not retried, and only a denial skips the rest of the step's cleanup. Alternatives: B — leave other failures unspecified. Why: a transient failure on housekeeping is not worth a retry either, and the leftover is swept later; skipping the rest only after a denial is what the issue asks. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] When is `get_trigger` needed before a delete? — Picked: A — for a trigger named by id (the `— resume.` block, the log, a report, the checker's own hand-back trigger); a trigger picked from a `list_triggers` result in the same step is checked on the listing's `name` and `persistent_session_id`, and the §26.G sweep keeps its script-chosen list. Alternatives: B — `get_trigger` before every delete. Why: the listing already carries both fields (§15), and the sweep's script already restricts names. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] How does the ownership check apply to the pickup, whose triggers are bound to other pickup sessions by design? — Picked: A — the pickup deletes only triggers named exactly `Claude issue pickup: hourly` (its existing filter), and the skip-on-denial rule applies unchanged. Alternatives: B — require the trigger to be bound to this session. Why: B would stop `— restart` and `stop` from working at all. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Where is `cleanup skipped` recorded in flows without a progress log? — Picked: A — in the flow's report (the fix-claude-pr report, the pickup's one-line report, the checker's reply, the §26.D report). Alternatives: B — as a PR or issue comment. Why: a comment per skipped cleanup is noise, and the report is where each flow already records its outcome. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] How is `.claude/commands/claude-issue-pickup.md`, which has no twin, changed? — Picked: A — not edited by this stage; its exact diff and the resulting file's sha256 go into the twin-sync blocker, and its test reads the root file (red until the sync). Alternatives: B — add a `workflow-templates/` twin for the pickup. Why: the pickup is coding-workflows-only, and B would ship it to consumers (§5); A is the Q40 no-twin rule. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue mode: plan written by /implement-issue-claude for #5068 (session session_01Auy2sKWXHcPHqbLY2N3RmG, started by the dispatch trigger trig_014NQAzdbcy3kAeXTjXFejP9); start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Protected-path approval: phase 1 — twin-first per Q40 (issue #5068 spec, 2026-09-29). The owner-authored issue body says "Twin-first: the commands are protected paths. Edit the `workflow-templates/.claude/**` twins (Q40 / #4948)": edit only the twins, open the phase PR, post a `hold` claim on its head, and stop BLOCKED for the `[claude-twin-sync]` copy.
- `security_pass_skip.py`: `{"skip": false, "label": null, "reason": "no skip label"}` → `Security pass: run`.
- Stale Routine sweep: deleted 3 ended one-shots (`trig_012pdEMdh4XyfjbcvQbUC1Bb`, `trig_01JbsquVNzafeNQYorYjNRwg`, `trig_015mGEKqaoybeytgGwozzS5s`).
- Issue progress comment: 5885202067.
- Twins to sync: `workflow-templates/.claude/commands/implement-plan-claude.md` → `.claude/commands/implement-plan-claude.md` (sha256 `1f5fc77d043bfb77353cf5ff8083721fd7fc0e3187ce9bd9cf22b13f6e54bcaf`); `workflow-templates/.claude/commands/fix-claude-pr.md` → `.claude/commands/fix-claude-pr.md` (sha256 `621356326841dc534d2633e8ad94b741dc2a49bdeb762af3d9bbb7b712d2331d`). No-twin diff: `.claude/commands/claude-issue-pickup.md` (from sha256 `217bd00d…331f` on main to `1484d8d8a6001588e2430efa6c329071c573af8adfd9ab54c91843568565331f`); the diff is in the blocker comment on #5068.
- Local verification: the full suite times out in this container (both on main and with the change), so the 37 test files that read the changed files were run one by one in an overlay (with the sync applied) and in an `origin/main` copy. The results are identical except for the 8 new passing tests. Unrelated failures reproduce on main: `test_implement_post_codex_recovery.py` (1), `test_orchestrate_poll_promote_cycle.py` / `test_orchestrate_poll_process.py` (hang), `test_workflow_retro.py` (Python 3.12 syntax).
- A skipped hand-back delete is safe: the stale Routine sweep deletes an `implement-plan <slug>: hand-back` 24 hours after its PR finished, long before its 7-day fire (`HAND_BACK_PROMPT_PATTERN`, case-insensitive, in `.claude/scripts/stale_routines.py`).
